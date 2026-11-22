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
    from sca.evolve.progress import evaluation_text, finish_text, gate_text, one_line, progress
    from sca.evolve.steering import JournalItem, SteeringMessage, SteeringState, receipt


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
        self.steering = SteeringState()
        self.current_generation: int | None = None
        self.evaluating_candidate = False
        self.active_candidate: workflow.ChildWorkflowHandle | None = None
        self.skip_message: SteeringMessage | None = None

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

    async def evaluate(self, overlay: dict, split: str, *, candidate_id: str | None = None,
                       role: str = 'candidate') -> dict | None:
        remaining = round(self.budget_usd - self.spent, 9)
        if self.stopped or (not self.config.get('model_unpriced') and remaining < self.config['case_budget']):
            return None
        run_id = str(workflow.uuid4())
        params = {'run_id': run_id, 'overlay': overlay, 'split': split, 'remaining': remaining}
        steered = self.journal_enabled or bool(self.steering.seen)
        if steered:
            params['evolution_step'] = {'generation': self.current_generation, 'role': role,
                                        'candidate_id': candidate_id}
        payload = await self.act('prepare_run', params)
        skip = None
        if steered and candidate_id:
            skip = await self.evaluate_candidate(payload, run_id)
        else:
            await workflow.execute_child_workflow('EvalRunWorkflow', payload, id='eval-' + run_id)
        run = await self.act('read_run', {'run_id': run_id})
        self.spent += metrics(run)['cost_usd']
        if skip is not None:
            run = {**run, 'status': 'cancelled', 'reason': '操作者已跳过候选'}
            await self.conversation(receipt(skip, '已跳过当前候选', f'candidate:{candidate_id}'))
        if self.journal_enabled:
            await self.conversation(progress(f'eval:{run_id}', 'evaluation', evaluation_text(
                role, split, run['results'], run['status'], run.get('reason'),
            ), f'eval:{run_id}'))
        if (run['status'] == 'failed' and not self.enforce_eval_integrity
                and workflow.patched("eval-suite-change-nonretryable")):
            reason = run.get('reason') or '请查看评测记录'
            raise EvalRunFailed(f'评测 {run_id} 失败：{reason}')
        if run['status'] != 'failed' or not self.enforce_eval_integrity:
            await self.act('status', {'status': 'running', 'spent_usd': self.spent})
        return run

    def budget_exhausted(self) -> bool:
        return (not self.config.get('model_unpriced')
                and self.budget_usd - self.spent < self.config['case_budget'])

    def evaluation_failure(self, run: dict | None, *, candidate: bool = False) -> str | None:
        if run is not None and candidate and run['status'] == 'cancelled':
            return run.get('reason') or '候选评测已取消'
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
        self.budget_usd = self.config['budget_usd']
        self.enforce_eval_integrity = workflow.patched("evolution-eval-integrity")
        self.journal_enabled = workflow.patched('evolution-conversation')
        status = 'completed'
        try:
            if self.journal_enabled:
                await self.conversation(progress('started', 'start', '开始演进', self.current_node))
            champion = await self.act('initialize')
            dev_run, holdout_run, stale = None, None, 0
            for generation in range(1, self.config['generations'] + 1):
                await self.step_boundary()
                if self.stopped:
                    status = 'stopped'
                    break
                if self.budget_exhausted():
                    status = 'budget_exhausted'
                    break
                dev_run = dev_run or await self.evaluate(champion['overlay'], 'dev', role='baseline')
                if reason := self.evaluation_failure(dev_run):
                    raise EvalRunFailed(reason)
                await self.step_boundary()
                if self.stopped:
                    status = 'stopped'
                    break
                if not dev_run or dev_run['status'] != 'completed':
                    status = 'budget_exhausted'
                    break
                if self.budget_exhausted():
                    status = 'budget_exhausted'
                    break
                self.current_generation = generation
                if self.journal_enabled:
                    item = progress(f'generation:{generation}', 'generation', f'第 {generation} 代开始',
                                    self.current_node)
                    item['generation'] = generation
                    await self.conversation(item)
                await self.step_boundary()
                if self.stopped:
                    status = 'stopped'
                    break
                proposals = await self.propose(champion['overlay'], str(dev_run['id']), generation)
                if self.journal_enabled:
                    proposals = [await self.register_candidate(proposal, generation, champion, number)
                                 for number, proposal in enumerate(proposals, start=1)]
                candidates, runs = [], []
                for proposal in proposals:
                    candidate = proposal if self.journal_enabled else await self.register_candidate(
                        proposal, generation, champion,
                    )
                    if not candidate['static_check']['valid']:
                        candidate.update(decision='invalid', reason='static checks failed')
                    else:
                        await self.step_boundary()
                        run = await self.evaluate(candidate['overlay'], 'dev', candidate_id=candidate['id'])
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
                await self.step_boundary()
                if winner and basis and not self.stopped:
                    holdout_run = holdout_run or await self.evaluate(champion['overlay'], 'holdout', role='comparison')
                    if reason := self.evaluation_failure(holdout_run):
                        winner.update(decision='rejected', reason=reason)
                        await self.record(winner)
                        await self.note_gate(winner)
                        raise EvalRunFailed(reason)
                    can_evaluate = holdout_run and (
                        not self.enforce_eval_integrity or holdout_run['status'] == 'completed')
                    await self.step_boundary()
                    challenger = await self.evaluate(winner['overlay'], 'holdout',
                                                     candidate_id=winner['id']) if can_evaluate else None
                    if challenger:
                        winner.update(holdout_run_id=str(challenger['id']), holdout_metrics=metrics(challenger))
                    await self.step_boundary()
                    # An efficiency win must not cost more on holdout; a quality win may cost a bit more.
                    allowance = self.config['max_cost_increase'] if basis == 'quality' else 0.0
                    if challenger and gate(holdout_run, challenger, allowance,
                                           legacy=not self.enforce_eval_integrity):
                        approved = True
                        if self.config['require_approval']:
                            self.approval, self.waiting_candidate = None, winner['id']
                            winner['reason'] = 'awaiting approval'
                            await self.record(winner)
                            await self.note_gate(winner, pending=True)
                            await self.act('status', {'status': 'awaiting_approval', 'spent_usd': self.spent})
                            while self.approval is None and not self.stopped:
                                await workflow.wait_condition(lambda: self.approval is not None or self.stopped
                                                              or bool(self.steering.pending))
                                await self.step_boundary(resume_status='awaiting_approval')
                            approved = self.approval is True
                            self.waiting_candidate = None
                            await self.act('status', {'status': 'running', 'spent_usd': self.spent})
                        if approved and not self.stopped:
                            champion = await self.act('promote', {'candidate': winner,
                                'champion_id': str(workflow.uuid4()), 'parent_champion_id': champion['id']})
                            winner.update(decision='promoted', reason=f'holdout gate passed ({basis})')
                            dev_run, holdout_run, stale, promoted = best, challenger, 0, True
                            await self.note_gate(winner)
                            if self.journal_enabled:
                                key = f'champion:{champion["id"]}'
                                await self.conversation(progress(key, 'champion',
                                    f'产生冠军 · {str(champion["id"])[:8]}', key))
                    if not promoted:
                        reason = self.evaluation_failure(challenger, candidate=True)
                        winner.update(decision='rejected', reason=reason or
                                      f'{basis} win on dev; holdout gate or approval rejected')
                for candidate in candidates:
                    if candidate['decision'] == 'pending':
                        candidate.update(decision='rejected', reason='not selected or dev gate failed')
                    await self.record(candidate)
                    await self.note_gate(candidate)
                if not promoted:
                    stale += 1
                self.current_generation = None
                await self.step_boundary()
                if self.stopped or self.steering.stop_after_generation:
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
                await self.finish('failed', provider.reason)
                return 'failed'
            reason = None
            if isinstance(exc, EvalRunFailed):
                reason = str(exc)
            elif isinstance(exc, ActivityError):
                reason = eval_failure_reason(exc)
            if reason is not None and (self.enforce_eval_integrity or workflow.patched("eval-suite-change-nonretryable")):
                await self.finish('failed', reason)
                return 'failed'
            if isinstance(exc, (ActivityError, ChildWorkflowError)) and workflow.patched("evolution-activity-reason"):
                await self.finish('failed', activity_failure_reason(exc))
                return 'failed'
            await self.finish('failed')
            raise
        await self.finish(status)
        return status

    # Evolution conversation

    @workflow.signal
    def user_message(self, message: SteeringMessage) -> None:
        if not workflow.patched('evolution-steering') or not self.steering.receive(message):
            return
        active = self.active_candidate
        if (message['mode'] == 'skip' and self.evaluating_candidate
                and (active is None or not active.done()) and self.skip_message is None):
            self.skip_message = message
        else:
            self.steering.pending.append(message)

    @property
    def current_node(self) -> str:
        if self.current_generation is not None:
            return f'generation:{self.current_generation}'
        return f'evolution:{self.request["id"]}'

    async def conversation(self, *items: JournalItem, status: dict | None = None) -> None:
        payload: dict = {'items': list(items)}
        if status is not None:
            payload['evolution_status'] = status
        await self.act('conversation', payload)

    async def evaluate_candidate(self, payload: dict, run_id: str) -> SteeringMessage | None:
        self.evaluating_candidate = True
        try:
            child = await workflow.start_child_workflow('EvalRunWorkflow', payload, id='eval-' + run_id)
            self.active_candidate = child
            await workflow.wait_condition(lambda: child.done() or self.skip_message is not None)
            if self.skip_message is not None and not child.done():
                try:
                    await child.signal('skip_evolution_candidate')
                except ApplicationError:
                    # The child can finish before the signal arrives. Its result is still discarded.
                    pass
            await child
            return self.skip_message
        finally:
            self.evaluating_candidate = False
            self.active_candidate = self.skip_message = None

    async def propose(self, overlay: dict, run_id: str, generation: int) -> list[dict]:
        guidance = self.steering.guidance
        payload = {'overlay': overlay, 'dev_run_id': run_id}
        if guidance is not None:
            payload['operator_guidance'] = guidance['text']
        proposals = await self.act('propose', payload)
        if guidance is not None and not self.steering.guidance_applied:
            item = receipt(guidance, f'已应用于第 {generation} 代提议', self.current_node)
            item['generation'] = generation
            await self.conversation(item)
            self.steering.guidance_applied = True
        return proposals

    async def register_candidate(self, proposal: dict, generation: int, champion: dict,
                                 number: int | None = None) -> dict:
        candidate = {**proposal, 'id': str(workflow.uuid4()), 'evolution_id': self.request['id'],
                     'generation': generation, 'parent_id': champion.get('candidate_id'),
                     'decision': 'pending', 'reason': None}
        await self.record(candidate)
        if number is not None:
            key = f'candidate:{candidate["id"]}'
            await self.conversation(progress(key, 'candidate',
                f'候选 {number} 提出 · {one_line(candidate["hypothesis"])}', key))
        return candidate

    async def step_boundary(self, *, resume_status: str = 'running') -> None:
        while self.steering.pending or (self.steering.paused and not self.stopped):
            if not self.steering.pending:
                await workflow.wait_condition(lambda: bool(self.steering.pending) or self.stopped)
                continue
            await self.apply_message(self.steering.pending.pop(0), resume_status)

    async def apply_message(self, message: SteeringMessage, resume_status: str) -> None:
        state = self.steering
        mode, node = message['mode'], self.current_node
        if mode == 'guidance':
            if state.guidance is not None and message.get('seq', 0) < state.guidance.get('seq', 0):
                await self.conversation(receipt(message, '新指引已取代此条', node, status='superseded'))
                return
            if state.guidance is not None and not state.guidance_applied:
                await self.conversation(receipt(state.guidance, '新指引已取代此条', node, status='superseded'))
            state.guidance, state.guidance_applied = message, False
            return
        if mode == 'budget':
            total = await self.act('conversation_budget', {
                'message': message, 'spent_usd': self.spent, 'dag_node_id': node,
            })
            if total is not None:
                self.budget_usd = total
            return
        before = state.paused
        match mode:
            case 'skip':
                await self.conversation(receipt(message, '当前没有可跳过的候选评测', node, status='rejected'))
                return
            case 'pause':
                if state.stop_after_generation:
                    await self.conversation(receipt(message, '已安排停止，当前一代将继续完成', node, status='rejected'))
                    return
                state.paused = True
                note = '演进已暂停'
            case 'resume':
                state.paused = False
                note = '演进已恢复' if before else '演进已在运行'
            case 'stop':
                state.stop_after_generation = True
                state.paused = False
                if self.current_generation is None:
                    self.stopped = True
                note = '已安排本代结束后停止' if not self.stopped else '已安排停止演进'
        status = {'status': 'paused' if state.paused else resume_status,
                  'spent_usd': self.spent} if state.paused != before else None
        await self.conversation(receipt(message, note, node), status=status)

    async def note_gate(self, candidate: dict, *, pending: bool = False) -> None:
        if not self.journal_enabled:
            return
        kind = 'approval' if pending else 'gate'
        await self.conversation(progress(f'{kind}:{candidate["id"]}', kind,
            gate_text(candidate['decision'], candidate.get('reason')), f'candidate:{candidate["id"]}'))

    async def finish(self, status: str, reason: str | None = None) -> None:
        values: dict = {'status': status, 'spent_usd': self.spent}
        if reason is not None:
            values['reason'] = reason
        if self.journal_enabled:
            target = f'evolution:{self.request["id"]}'
            if status == 'failed':
                target += '/finish'
            values['status_progress'] = progress('finished', 'finish', finish_text(status, reason), target)
        guidance = self.steering.guidance
        if guidance is not None and not self.steering.guidance_applied:
            await self.conversation(receipt(guidance, '演进结束前没有下一代提议', self.current_node, status='rejected'))
        for message in self.steering.pending:
            await self.conversation(receipt(message, '演进已结束', self.current_node, status='rejected'))
        await self.act('status', values)
