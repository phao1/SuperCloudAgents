"""Deterministic evolution search; activities own all external state."""

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, ChildWorkflowError, FailureError

from sca.eval.errors import eval_failure_reason
from sca.orchestration.provider_errors import patched_provider_account_error

with workflow.unsafe.imports_passed_through():
    from sca.evolve.checks import dev_basis, gate, invalid_run_reason, metrics, select_best


class EvalRunFailed(Exception):
    pass


def activity_failure_reason(error: ActivityError | ChildWorkflowError) -> str:
    category, name = (("活动", error.activity_type) if isinstance(error, ActivityError)
                      else ("子工作流", error.workflow_type))
    cause: BaseException = error
    while isinstance(cause, FailureError) and cause.cause is not None:
        cause = cause.cause
    kind = cause.type if isinstance(cause, ApplicationError) and cause.type else type(cause).__name__
    return f"演进{category}执行失败（{name}，{kind}）"


@workflow.defn
class EvolutionWorkflow:
    def __init__(self) -> None:
        self.stopped = False
        self.approval: bool | None = None
        self.waiting_candidate: str | None = None

    @workflow.signal
    def stop(self) -> None:
        self.stopped = True

    @workflow.signal
    def approve(self, candidate_id: str) -> None:
        if candidate_id == self.waiting_candidate:
            self.approval = True

    @workflow.signal
    def reject(self, candidate_id: str) -> None:
        if candidate_id == self.waiting_candidate:
            self.approval = False

    async def act(self, name: str, extra: dict | None = None) -> Any:
        return await workflow.execute_activity(
            'evolve_' + name, {**self.request, **(extra or {})},
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=3))

    async def evaluate(self, overlay: dict, split: str) -> dict | None:
        remaining = round(self.config['budget_usd'] - self.spent, 9)
        if self.stopped or (not self.config.get('model_unpriced') and remaining < self.config['case_budget']):
            return None
        run_id = str(workflow.uuid4())
        payload = await self.act('prepare_run', {'run_id': run_id, 'overlay': overlay,
                                                 'split': split, 'remaining': remaining})
        await workflow.execute_child_workflow('EvalRunWorkflow', payload, id='eval-' + run_id)
        run = await self.act('read_run', {'run_id': run_id})
        self.spent += metrics(run)['cost_usd']
        if (run['status'] == 'failed' and not self.enforce_eval_integrity
                and workflow.patched("eval-suite-change-nonretryable")):
            reason = run.get('reason') or '请查看评测记录'
            raise EvalRunFailed(f'评测 {run_id} 失败：{reason}')
        if run['status'] != 'failed' or not self.enforce_eval_integrity:
            await self.act('status', {'status': 'running', 'spent_usd': self.spent})
        return run

    def budget_exhausted(self) -> bool:
        return (not self.config.get('model_unpriced')
                and self.config['budget_usd'] - self.spent < self.config['case_budget'])

    def evaluation_failure(self, run: dict | None, *, candidate: bool = False) -> str | None:
        if not self.enforce_eval_integrity or run is None:
            return None
        if candidate and run['status'] == 'budget_exhausted':
            return f"评测 {run['id']} 因预算耗尽而未完成"
        return invalid_run_reason(run)

    async def record(self, candidate: dict) -> None:
        await self.act('record', {'candidate': candidate})

    @workflow.run
    async def run(self, request: dict) -> str:
        self.request, self.config, self.spent = request, request['config'], 0.0
        self.enforce_eval_integrity = workflow.patched("evolution-eval-integrity")
        status = 'completed'
        try:
            champion = await self.act('initialize')
            dev_run, holdout_run, stale = None, None, 0
            for generation in range(1, self.config['generations'] + 1):
                if self.stopped:
                    status = 'stopped'
                    break
                if self.budget_exhausted():
                    status = 'budget_exhausted'
                    break
                dev_run = dev_run or await self.evaluate(champion['overlay'], 'dev')
                if reason := self.evaluation_failure(dev_run):
                    raise EvalRunFailed(reason)
                if self.stopped:
                    status = 'stopped'
                    break
                if not dev_run or dev_run['status'] != 'completed':
                    status = 'budget_exhausted'
                    break
                if self.budget_exhausted():
                    status = 'budget_exhausted'
                    break
                proposals = await self.act('propose', {'overlay': champion['overlay'],
                                                       'dev_run_id': str(dev_run['id'])})
                candidates, runs = [], []
                for proposal in proposals:
                    candidate = {**proposal, 'id': str(workflow.uuid4()), 'evolution_id': request['id'],
                                 'generation': generation, 'parent_id': champion.get('candidate_id'),
                                 'decision': 'pending', 'reason': None}
                    await self.record(candidate)
                    if not candidate['static_check']['valid']:
                        candidate.update(decision='invalid', reason='static checks failed')
                    else:
                        run = await self.evaluate(candidate['overlay'], 'dev')
                        if run:
                            candidate.update(dev_run_id=str(run['id']), dev_metrics=metrics(run))
                            if reason := self.evaluation_failure(run, candidate=True):
                                candidate.update(decision='rejected', reason=reason)
                            else:
                                runs.append(run)
                        else:
                            candidate.update(decision='rejected', reason='evaluation stopped')
                    candidates.append(candidate)
                    await self.record(candidate)
                best = select_best(runs)
                winner = next((c for c in candidates if best and c.get('dev_run_id') == str(best['id'])), None)
                promoted = False
                basis = dev_basis(dev_run, best, self.config.get('min_cost_saving', .15),
                                  legacy=not self.enforce_eval_integrity) if winner else None
                if winner and basis and not self.stopped:
                    holdout_run = holdout_run or await self.evaluate(champion['overlay'], 'holdout')
                    if reason := self.evaluation_failure(holdout_run):
                        winner.update(decision='rejected', reason=reason)
                        await self.record(winner)
                        raise EvalRunFailed(reason)
                    can_evaluate = holdout_run and (
                        not self.enforce_eval_integrity or holdout_run['status'] == 'completed')
                    challenger = await self.evaluate(winner['overlay'], 'holdout') if can_evaluate else None
                    if challenger:
                        winner.update(holdout_run_id=str(challenger['id']), holdout_metrics=metrics(challenger))
                    # An efficiency win must not cost more on holdout; a quality win may cost a bit more.
                    allowance = self.config['max_cost_increase'] if basis == 'quality' else 0.0
                    if challenger and gate(holdout_run, challenger, allowance,
                                           legacy=not self.enforce_eval_integrity):
                        approved = True
                        if self.config['require_approval']:
                            self.approval, self.waiting_candidate = None, winner['id']
                            winner['reason'] = 'awaiting approval'
                            await self.record(winner)
                            await self.act('status', {'status': 'awaiting_approval', 'spent_usd': self.spent})
                            await workflow.wait_condition(lambda: self.approval is not None or self.stopped)
                            approved = self.approval is True
                            self.waiting_candidate = None
                            await self.act('status', {'status': 'running', 'spent_usd': self.spent})
                        if approved and not self.stopped:
                            champion = await self.act('promote', {'candidate': winner,
                                'champion_id': str(workflow.uuid4()), 'parent_champion_id': champion['id']})
                            winner.update(decision='promoted', reason=f'holdout gate passed ({basis})')
                            dev_run, holdout_run, stale, promoted = best, challenger, 0, True
                    if not promoted:
                        reason = self.evaluation_failure(challenger, candidate=True)
                        winner.update(decision='rejected', reason=reason or
                                      f'{basis} win on dev; holdout gate or approval rejected')
                for candidate in candidates:
                    if candidate['decision'] == 'pending':
                        candidate.update(decision='rejected', reason='not selected or dev gate failed')
                    await self.record(candidate)
                if not promoted:
                    stale += 1
                if self.stopped:
                    status = 'stopped'
                    break
                if self.budget_exhausted():
                    status = 'budget_exhausted'
                    break
                if stale >= self.config['patience']:
                    status = 'patience_exhausted'
                    break
        except Exception as exc:
            if provider := patched_provider_account_error(exc):
                await self.act('status', {'status': 'failed', 'spent_usd': self.spent, 'reason': provider.reason})
                return 'failed'
            reason = None
            if isinstance(exc, EvalRunFailed):
                reason = str(exc)
            elif isinstance(exc, ActivityError):
                reason = eval_failure_reason(exc)
            if reason is not None and (self.enforce_eval_integrity or workflow.patched("eval-suite-change-nonretryable")):
                await self.act('status', {'status': 'failed', 'spent_usd': self.spent, 'reason': reason})
                return 'failed'
            if isinstance(exc, (ActivityError, ChildWorkflowError)) and workflow.patched("evolution-activity-reason"):
                await self.act('status', {'status': 'failed', 'spent_usd': self.spent,
                                          'reason': activity_failure_reason(exc)})
                return 'failed'
            await self.act('status', {'status': 'failed', 'spent_usd': self.spent})
            raise
        await self.act('status', {'status': status, 'spent_usd': self.spent})
        return status
