"""Project durable progress and steering into the Portal conversation format."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from itertools import islice
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from sca.api.authz import GuestPrincipal
from sca.api.guest_history import history_payload
from sca.core.live_timing import (
    OPERATION_EVENTS,
    TIMING_CONTEXT,
    TaskTimings,
    event_is_start,
    event_scope,
    number,
    seconds,
)
from sca.core.models import Event, Principal, Task, TaskState
from sca.core.task_dag import (
    TASK_MILESTONE_CONTEXT,
    TASK_MILESTONES,
    DAGNode,
    task_conversation_nodes,
)
from sca.db.repo import Store
from sca.eval.trajectory import is_errored, overall_passed
from sca.evolve.dag import EvolutionHistory, build_evolution_dag
from sca.evolve.progress import evaluation_text, finish_text, gate_text, one_line
from sca.evolve.steering import (
    ACTION_LABELS,
    ACTIVE_EVOLUTION_STATES,
    GUIDANCE_MODES,
    MESSAGE_EVENT,
)
from sca.history.privacy import clean_text


class FileEdit(BaseModel):
    path: str
    additions: int | None = None
    deletions: int | None = None


class Operation(BaseModel):
    kind: Literal['command', 'tool']
    name: str
    summary: str | None = None
    exit_code: int | None = None
    output_tail: str | None = None
    failed: bool = False
    duration_s: float | None = None


class TurnActivity(BaseModel):
    turn_seq: int
    operations: list[Operation] = Field(default_factory=list)
    files: list[FileEdit] = Field(default_factory=list)


class ProposalSummary(BaseModel):
    hypothesis: str
    rationale: str | None
    overlay_changes: list[str] | None


class EvaluationSummary(BaseModel):
    split: str
    role: str
    passed: int
    total: int
    cost_usd: float
    status: str
    reason: str | None = None


class ConversationItem(BaseModel):
    id: str
    kind: str
    text: str
    created_at: datetime
    dag_node_id: str
    actor: str | None = None
    mode: str | None = None
    message_id: str | None = None
    status: str | None = None
    backend: str | None = None
    scope: str | None = None
    agent_lane: int | None = None
    agent_label: str | None = None
    activity: TurnActivity | None = None
    proposal: ProposalSummary | None = None
    evaluation: EvaluationSummary | None = None
    duration_s: float | None = None
    cost_usd: float | None = None
    command: str | None = None
    output_tail: str | None = None


class ConversationPage(BaseModel):
    items: list[ConversationItem]
    cursor: str
    has_more: bool


def _display(user: Principal, value: object, limit: int, *, tail: bool = False) -> str:
    if not isinstance(value, str):
        return ''
    value = history_payload(user, clean_text(value)).strip()
    if len(value) <= limit:
        return value
    return '…' + value[-limit + 1:] if tail else value[:limit - 1] + '…'


def _object(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def evolution_message(row: dict, *, status: str = 'pending') -> dict:
    payload = row['payload']
    return {
        'id': payload['item_id'], 'evolution_id': payload['evolution_id'], 'mode': payload['mode'],
        'text': payload.get('text'), 'remaining_budget_usd': payload.get('remaining_budget_usd'),
        'actor': row['actor'], 'created_at': row['created_at'], 'status': status,
    }


def evolution_item(row: dict, user: Principal) -> ConversationItem:
    item, at = row['payload'], row['created_at']
    if row['type'] != MESSAGE_EVENT:
        return ConversationItem(id=item['item_id'], kind=item['kind'], text=_display(user, item['text'], 4000),
                                created_at=at,
                                dag_node_id=item['dag_node_id'], message_id=item.get('message_id'),
                                status=item.get('status'))
    mode, text = item['mode'], item.get('text')
    if mode not in GUIDANCE_MODES:
        label = ACTION_LABELS[mode]
        if mode == 'budget':
            label += f' · ${item["remaining_budget_usd"]:.6g}'
        text = f'{label} · {text}' if text else label
    return ConversationItem(id=item['item_id'], kind='message', text=_display(user, text, 4000),
                            mode=mode, actor=row['actor'],
                            message_id=item['item_id'], status='pending',
                            created_at=at, dag_node_id=f'evolution:{item["evolution_id"]}')


def legacy_evolution_items(history: EvolutionHistory) -> list[ConversationItem]:
    row = history['evolution']
    root = f'evolution:{row["id"]}'
    items = [ConversationItem(id='started', kind='start', text='开始演进',
                              created_at=row['created_at'], dag_node_id=root)]
    results = defaultdict(list)
    for result in history['results']:
        results[str(result['run_id'])].append(result)
    completions = {}
    for node in build_evolution_dag(history).nodes:
        at = node.started_at
        if node.kind == 'generation':
            kind, label = 'generation', f'{node.label}开始'
        elif node.kind == 'candidate':
            kind, label = 'candidate', '候选提出 · ' + one_line(node.meta['hypothesis'])
        elif node.kind == 'champion':
            kind, label = 'champion', '产生' + node.label
        elif node.kind == 'eval_run' and node.meta['state'] not in ACTIVE_EVOLUTION_STATES:
            kind, at = 'evaluation', node.finished_at or at
            label = evaluation_text(node.meta['role'], node.meta['split'] or '',
                                    results[node.meta['run_id']], node.meta['state'], node.meta.get('reason'))
            completions[node.meta['run_id']] = at
        else:
            continue
        if at is not None:
            items.append(ConversationItem(id=node.id, kind=kind, text=label, created_at=at, dag_node_id=node.id))
    for candidate in history['candidates']:
        decision, reason = candidate['decision'], candidate.get('reason')
        if decision == 'pending' and reason != 'awaiting approval':
            continue
        node = f'candidate:{candidate["id"]}'
        kind = 'approval' if decision == 'pending' else 'gate'
        at = max(filter(None, (candidate['created_at'], completions.get(str(candidate.get('dev_run_id'))),
                               completions.get(str(candidate.get('holdout_run_id'))))))
        items.append(ConversationItem(id=f'{kind}:{candidate["id"]}', kind=kind, text=gate_text(decision, reason),
                                      created_at=at, dag_node_id=node))
    if row['status'] not in ACTIVE_EVOLUTION_STATES and row.get('finished_at'):
        items.append(evolution_finish_item(row))
    order = ('start', 'generation', 'candidate', 'evaluation', 'approval', 'gate', 'champion', 'finish')
    return sorted(items, key=lambda item: (item.created_at, order.index(item.kind), item.id))


def evolution_finish_item(row: dict) -> ConversationItem:
    reason = row['config'].get('reason')
    if reason == 'imported while running':
        reason = '仅导入历史记录'
    return ConversationItem(id='finished', kind='finish', text=finish_text(row['status'], reason),
                            created_at=row['finished_at'], dag_node_id=f'evolution:{row["id"]}',
                            duration_s=seconds(row['created_at'], row['finished_at']))


def _overlay_changes(before: dict, after: dict, user: Principal) -> list[str]:
    labels = {'system_prompt': '系统提示词', 'tool_guidance': '工具指引'}
    changes = [label for key, label in labels.items() if before.get(key) != after.get(key)]
    old, new = (_object(overlay.get('tool_descriptions')) for overlay in (before, after))
    descriptions = sorted(name for name in old.keys() | new.keys() if old.get(name) != new.get(name))
    if descriptions:
        names = '、'.join(_display(user, name, 50) for name in descriptions[:6])
        changes.append(f'工具描述 · {len(descriptions)} 项（{names}{"…" if len(descriptions) > 6 else ""}）')
    return changes


def _add_evolution_evidence(items: list[ConversationItem], history: EvolutionHistory, user: Principal) -> None:
    candidates = {str(row['id']): row for row in history['candidates']}
    runs = {str(row['id']): row for row in history['runs']}
    results: dict[str, list[dict]] = defaultdict(list)
    for row in history['results']:
        results[str(row['run_id'])].append(row)
    baseline = next((run for run in runs.values()
                     if (run['variant'].get('evolution_step') or {}).get('role') == 'baseline'), None)
    if baseline is None and candidates:
        first_candidate = min(candidate['created_at'] for candidate in candidates.values())
        baseline = min((run for run in runs.values() if run['variant'].get('split') == 'dev'
                        and run['created_at'] <= first_candidate), key=lambda run: run['created_at'], default=None)
    backend = history['evolution']['config'].get('agent_backend', 'opencode')
    for item in items:
        _, _, target = item.dag_node_id.partition(':')
        if item.kind == 'candidate' and (candidate := candidates.get(target)):
            parent_id = candidate.get('parent_id')
            previous = candidates.get(str(parent_id))
            parent = {} if parent_id is None else None
            if previous is not None:
                parent = previous['overlay']
            elif parent_id is not None and baseline is not None:
                parent = baseline['variant'].get('variant', {}).get('agent_overlay') or {}
            diagnosis = _object(candidate.get('diagnosis')).get('diagnoses')
            rationale = ' · '.join(part for entry in diagnosis[:2]
                                   if (part := _display(user, entry, 250))) if isinstance(diagnosis, list) else ''
            item.backend = _display(user, backend, 60)
            item.scope = f'第 {candidate["generation"]} 代 · 候选提议'
            item.proposal = ProposalSummary(
                hypothesis=_display(user, candidate['hypothesis'], 1200),
                rationale=rationale or None,
                overlay_changes=_overlay_changes(parent, candidate['overlay'], user) if parent is not None else None,
            )
        if item.kind == 'evaluation' and (run := runs.get(target)):
            variant, rows = run['variant'], results[target]
            item.evaluation = EvaluationSummary(
                split=_display(user, variant.get('split'), 40),
                role=_display(user, (variant.get('evolution_step') or {}).get('role'), 40),
                passed=sum(not is_errored(row) and overall_passed(row) for row in rows),
                total=len(rows), cost_usd=sum(float(row['cost_usd']) for row in rows),
                status=run['status'], reason=_display(user, variant.get('reason'), 220) or None,
            )
            if run.get('finished_at') is not None:
                item.duration_s = seconds(run['created_at'], run['finished_at'])


async def evolution_page(
    store: Store, evolution_id: UUID, cursor: str | None, limit: int, user: Principal,
) -> ConversationPage:
    phase, position = cursor.split(':') if cursor else ('h', '0')
    offset = int(position)
    row, legacy, entries = await store.read_evolution_conversation(
        evolution_id, offset if phase != 'h' else 0, limit + 1, check_legacy=phase == 'h',
    )
    imported_stop = (row['config'].get('history_imported') and row['config'].get('reason') == 'imported while running'
                     and row.get('finished_at'))
    if legacy and row['status'] != 'queued' and not any(entry['payload']['item_id'] == 'started' for entry in entries):
        history = await store.get_evolution_dag_history(evolution_id)
        if len(entries) > limit:
            _, _, entries = await store.read_evolution_conversation(evolution_id)
        transcript = sorted([*legacy_evolution_items(history), *(evolution_item(entry, user) for entry in entries)],
                            key=lambda item: item.created_at)
        items = transcript[offset:offset + limit]
        _add_evolution_evidence(items, history, user)
        more = offset + len(items) < len(transcript)
        if more or row['status'] in ACTIVE_EVOLUTION_STATES:
            return ConversationPage(items=items, cursor=f'h:{offset + len(items)}', has_more=more)
        last_id = entries[-1]['id'] if entries else 0
        return ConversationPage(items=items, cursor=f'{"x" if imported_stop else "e"}:{last_id}', has_more=False)
    page = entries[:limit]
    items = [evolution_item(entry, user) for entry in page]
    if any(item.kind in ('candidate', 'evaluation') for item in items):
        _add_evolution_evidence(items, await store.get_evolution_dag_history(evolution_id), user)
    last_id = page[-1]['id'] if page else offset if phase != 'h' else 0
    more = len(entries) > limit
    next_phase = 'x' if phase == 'x' else 'e'
    if imported_stop and phase != 'x' and not more:
        if len(items) < limit:
            items.append(evolution_finish_item(row))
            next_phase = 'x'
        else:
            more = True
    return ConversationPage(items=items, cursor=f'{next_phase}:{last_id}', has_more=more)


TASK_STATES = {
    TaskState.QUEUED: '排队中', TaskState.PROVISIONING: '准备执行环境', TaskState.RUNNING: '执行中',
    TaskState.VERIFYING: '验证中', TaskState.AWAITING_REVIEW: '等待人工审核', TaskState.PAUSED: '已暂停',
    TaskState.SUCCEEDED: '任务完成', TaskState.FAILED: '任务失败', TaskState.CANCELLED: '任务已取消',
}
REVIEW_DECISIONS = {'approve': '已批准', 'request_changes': '已要求修改', 'reject': '已拒绝'}
CHAT_EVENTS = TASK_MILESTONES | OPERATION_EVENTS | {
    'agent.message', 'agent.error', 'model.provider_error', 'review.submitted',
    'workspace.prepared', 'sandbox.created', 'sandbox.restored', 'setup.completed',
    'admission.waiting', 'admission.granted', 'subtask.queued', 'subtasks.merged', 'agent.activity',
}
CHAT_CONTEXT = TASK_MILESTONE_CONTEXT | TIMING_CONTEXT | CHAT_EVENTS
REPLY_LIMIT = 8000


@dataclass(frozen=True)
class AgentOutputAdapter:
    """Select path and argument fields from the payloads persisted by each backend."""

    path_keys: tuple[str, ...] = ('path',)
    metadata_files: bool = False

    def edits(self, payload: dict, user: Principal) -> list[FileEdit]:
        if _failed(payload):
            return []
        metadata = _object(payload.get('metadata'))
        inputs = _object(payload.get('input'))
        changes = payload.get('changes')
        if self.metadata_files and not changes:
            changes = metadata.get('files')
        if not changes:
            path = next((inputs[key] for key in self.path_keys if isinstance(inputs.get(key), str)), None)
            changes = [{'path': path, 'patch': metadata.get('diff')}] if path else []
        if not isinstance(changes, list):
            return []
        return [edit for entry in islice(changes, 40) if (edit := _file_edit(entry, user))]

    def tool_summary(self, payload: dict, user: Principal) -> str | None:
        inputs = _object(payload.get('input'))
        fields = (*self.path_keys, 'pattern', 'query', 'offset', 'limit', 'start_line', 'end_line')
        values = []
        for name in fields:
            value = inputs.get(name)
            if type(value) is int or (value := _display(user, value, 96)):
                values.append(f'{name}={value}')
        return ' · '.join(values[:3]) or None


_STANDARD = AgentOutputAdapter()
_OPENCODE = AgentOutputAdapter(('path', 'filePath', 'file_path'), metadata_files=True)
AGENT_ADAPTERS = {
    'codex': _STANDARD, 'fake': _STANDARD, 'pi': _STANDARD, 'dsh': AgentOutputAdapter(('file_path', 'path')),
    'opencode': _OPENCODE, 'opencode-brain': _OPENCODE,
}


def _failed(payload: dict) -> bool:
    return (payload.get('is_error') is True or payload.get('status') in ('error', 'failed')
            or payload.get('exit_code') not in (None, 0))


def _file_edit(entry: object, user: Principal) -> FileEdit | None:
    data = _object(entry)
    name = entry if isinstance(entry, str) else data.get('path') or data.get('file')
    if not (path := _display(user, name, 300)):
        return None
    added, removed = data.get('additions'), data.get('deletions')
    patch = data.get('patch')
    if added is None and removed is None and isinstance(patch, str) and len(patch) <= 100_000:
        added, removed = 0, 0
        for line in patch.splitlines():
            added += line.startswith('+') and not line.startswith('+++ ')
            removed += line.startswith('-') and not line.startswith('--- ')
    return FileEdit(path=path, additions=added if type(added) is int and added >= 0 else None,
                    deletions=removed if type(removed) is int and removed >= 0 else None)


def _operation(
    event: Event, adapter: AgentOutputAdapter, user: Principal, duration_s: float | None,
) -> Operation | None:
    p = event.payload
    if event.type == 'agent.command':
        exit_code = p.get('exit_code')
        return Operation(kind='command', name=_display(user, p.get('command'), 800) or '命令',
                         exit_code=exit_code if type(exit_code) is int else None,
                         output_tail=_display(user, p.get('output_tail'), 1600, tail=True) or None,
                         failed=_failed(p), duration_s=duration_s)
    if event.type == 'agent.tool' and not p.get('name') and not _failed(p):
        return None
    return Operation(kind='tool', name=_display(user, p.get('name'), 100)
                     or ('文件编辑' if event.type == 'agent.file_change' else '工具调用'),
                     summary=adapter.tool_summary(p, user), failed=_failed(p), duration_s=duration_s)


def _reply_text(event: Event) -> str:
    p = event.payload
    if event.type == 'agent.turn_completed':
        text = p.get('final_message')
    elif p.get('delta') or p.get('streaming') or p.get('role', 'assistant') != 'assistant' or p.get('status') in (
        'started', 'streaming', 'in_progress', 'delta',
    ):
        return ''
    else:
        text = p.get('text') or p.get('message')
    return text.strip() if isinstance(text, str) else ''


def _content_key(event: Event, turn: int) -> tuple[int, str, str] | None:
    if (event.type in OPERATION_EVENTS and not event_is_start(event)
            and isinstance(call_id := event.payload.get('call_id'), str)):
        return turn, 'operation', call_id
    if event.type in ('agent.message', 'agent.turn_completed') and (text := _reply_text(event)):
        return turn, 'reply', text
    if event.type in ('agent.error', 'model.provider_error'):
        p = event.payload
        if isinstance(message := p.get('message'), str):
            return turn, 'error', message
    return None


def _turn_seq(event: Event, node: DAGNode | None) -> int:
    return int(node.id.rsplit(':', 1)[-1]) if node and node.kind in ('turn', 'fix') else event.seq


def _agent_identity(event: Event, task: Task, node: DAGNode | None, user: Principal) -> dict:
    meta = node.meta if node else {}
    backend = meta.get('backend') or event.payload.get('backend') or task.spec.get('agent_backend')
    scope = _display(user, event.payload.get('subtask'), 100)
    attempt = event.payload.get('best_of_attempt', event.payload.get('best_of_candidate'))
    if type(attempt) is int:
        scope = ' · '.join(filter(None, (scope, f'候选 {attempt}')))
    agent_lane = event.payload.get('agent_lane', meta.get('agent_lane'))
    agent_label = event.payload.get('agent_label', meta.get('agent_label'))
    return {'backend': _display(user, backend, 60) or None, 'scope': scope or None,
            'agent_lane': agent_lane if type(agent_lane) is int else None,
            'agent_label': _display(user, agent_label, 100) or None}


def _task_control_receipt(event: Event) -> str:
    p = event.payload
    place = (f'第 {p["plan_attempt"]} 次规划' if p.get('plan_attempt') is not None
             else f'第 {p["turn"]} 轮' if p.get('turn') is not None else '执行步骤')
    if event.type == 'user.message.applied':
        return f'{place}已完成'
    if p.get('interrupted') is True:
        return f'已打断 · {place}已按此重新开始'
    return f'已送达 · {place}按此执行'


def _turn_text(event: Event, node: DAGNode | None) -> str:
    status = {'agent.turn_started': '开始', 'agent.turn_completed': '完成', 'agent.turn_interrupted': '已打断'}
    number = event.payload.get('turn', node.meta.get('turn', 1) if node else 1)
    text = f'第 {number} 轮{status[event.type]}'
    attempt = event.payload.get('attempt')
    if event.type == 'agent.turn_started' and type(attempt) is int and attempt > 1:
        text += f' · 第 {attempt} 次尝试'
    return text


def _task_progress(
    event: Event, task: Task, node: DAGNode | None, user: Principal,
) -> str | None:
    p = event.payload
    match event.type:
        case 'task.created':
            subtasks = task.spec.get('subtasks') or []
            return f'任务已提交 · 已规划 {len(subtasks)} 个子任务' if subtasks else '任务已提交'
        case 'task.state_changed':
            label = TASK_STATES.get(p.get('to'))
            reason = one_line(_display(user, p.get('reason'), 220))
            return f'{label} · {reason}' if label and reason else label
        case 'workspace.prepared':
            if p.get('phase') != 'after_setup':
                return '仓库已准备' if p.get('mirror_only') else '工作区已准备'
        case 'sandbox.created' | 'sandbox.restored':
            return '沙箱已创建' if event.type == 'sandbox.created' else '沙箱已恢复'
        case 'setup.completed':
            if p.get('skipped'):
                return '环境已从快照恢复'
            if p.get('exit_code') != 0:
                return '环境准备失败'
            return '依赖与环境准备完成' if p.get('command') else '环境准备完成'
        case 'admission.waiting':
            return '等待模型网关限流恢复' if p.get('reason') == 'gateway_pressure' else '等待并发名额'
        case 'admission.granted':
            if (number(p.get('wait_seconds')) or 0) >= 1:
                return '已获得并发名额'
        case 'agent.turn_started' | 'agent.turn_completed' | 'agent.turn_interrupted':
            return _turn_text(event, node)
        case 'plan.started':
            return '开始规划子任务'
        case 'plan.completed':
            count = len(p.get('subtasks') or [])
            if not count:
                return '规划完成 · 采用单任务执行'
            cap = p.get('max_parallel_agents')
            return f'规划完成 · {count} 个子任务' + (f'，最多 {cap} 个并发' if type(cap) is int else '')
        case 'subtask.queued' | 'subtask.started' | 'subtask.completed':
            name = one_line(_display(user, p.get('subtask'), 50))
            if event.type == 'subtask.queued':
                return f'子任务 {name} · 等待并发名额' if name else None
            if event.type == 'subtask.started':
                status = '开始'
            else:
                status = '已阻塞' if p.get('status') == 'blocked' else ('完成' if p.get('passed') else '失败')
            return f'子任务 {name} {status}' if name else None
        case 'subtasks.merged':
            return '子任务合并完成'
        case 'agent.activity':
            if (p.get('operation') == 'settle' and p.get('status') == 'completed'
                    and (number(p.get('duration_s')) or 0) >= 1):
                return '费用结算完成'
            if p.get('operation') in ('model_retry', 'model_rate_limit'):
                label = '等待模型限流恢复' if p['operation'] == 'model_rate_limit' else '重试模型请求'
                attempt = p.get('retry_attempt')
                return f'{label} · 第 {attempt} 次尝试' if type(attempt) is int else label
        case 'plan.rejected':
            reason = one_line(_display(user, p.get('reason'), 220))
            return '子任务规划未通过' + (f' · {reason}' if reason else '')
        case 'verification.completed' | 'acceptance.completed' | 'ci.completed':
            prefix = {'verification.completed': '验证', 'acceptance.completed': '验收', 'ci.completed': 'CI'}
            if p.get('skipped'):
                return prefix[event.type] + '已跳过'
            if isinstance(p.get('passed'), bool):
                return prefix[event.type] + ('通过' if p['passed'] else '失败')
        case 'best_of.started':
            return f'开始比较 {len(p.get("attempts") or [])} 个候选'
        case 'best_of.attempt_completed':
            passed = (p.get('verification') or {}).get('passed') is True
            return f'候选 {p.get("attempt", "—")} 完成 · 验证' + ('通过' if passed else '未通过')
        case 'best_of.selected':
            return f'比较完成 · 选择候选 {p.get("attempt", "—")}'
        case 'review.requested':
            if p.get('subtask') or p.get('best_of_candidate') is not None:
                return None
            return '进入规划审核' if 'plan' in p else '进入人工审核'
        case 'review.submitted':
            return REVIEW_DECISIONS.get(p.get('decision'))
        case 'output.written':
            directory = ('本机目录' if isinstance(user, GuestPrincipal)
                         else _display(user, p.get('output_dir'), 32767))
            commit = _display(user, p.get('output_commit'), 64)[:7]
            branch = _display(user, p.get('output_branch'), 256)
            text = f'代码已写入 {directory}（提交 {commit}）'
            return f'{text} · 结果分支 {branch}' if p.get('output_on_side_branch') else text
        case 'user.message.delivered' | 'user.message.applied':
            return _task_control_receipt(event)
    return None


def _add_operation(
    activity: TurnActivity, event: Event, adapter: AgentOutputAdapter, user: Principal,
    files: dict[str, FileEdit], duration_s: float | None,
) -> None:
    if event.type == 'agent.file_change' and (edits := adapter.edits(event.payload, user)):
        for edit in edits:
            if (existing := files.get(edit.path)) is None:
                files[edit.path] = edit
                activity.files.append(edit)
                continue
            for name in ('additions', 'deletions'):
                if (count := getattr(edit, name)) is not None:
                    setattr(existing, name, (getattr(existing, name) or 0) + count)
    elif operation := _operation(event, adapter, user, duration_s):
        activity.operations.append(operation)


def _milestone_details(event: Event, timings: TaskTimings, user: Principal) -> dict:
    p = event.payload
    details = {'duration_s': timings.durations.get(event.seq), 'cost_usd': timings.costs.get(event.seq)}
    failed_check = event.type == 'verification.completed' and p.get('passed') is False
    if failed_check or event.type == 'setup.completed':
        details['command'] = _display(user, p.get('command'), 800) or None
        if failed_check or p.get('exit_code') not in (None, 0):
            details['output_tail'] = _display(user, p.get('output_tail'), 2400, tail=True) or None
    return details


def _error_text(event: Event, user: Principal) -> str:
    p = event.payload
    kind = _object(p.get('model_provider_error')).get('kind') or p.get('kind')
    if kind == 'rate_limit':
        return '模型服务限流 · 等待重试'
    if kind == 'unavailable':
        return '模型服务暂不可用 · 等待重试'
    return _display(user, p.get('message') or kind, 600) or '执行出错'


async def task_page(store: Store, task_id: UUID, user: Principal, after: int, limit: int) -> ConversationPage:
    task, page, context = await store.task_conversation_events(
        task_id, after, limit + 1, CHAT_EVENTS, CHAT_CONTEXT,
    )
    if not page:
        return ConversationPage(items=[], cursor=str(after), has_more=False)
    shown = page[:limit]
    position = shown[-1].seq
    history = [event for event in context if event.seq <= position]
    focus, _ = task_conversation_nodes(task, [event for event in history if event.type in TASK_MILESTONE_CONTEXT])
    timings = TaskTimings(history)
    keys = {key for event in context if event.seq <= after
            if (key := _content_key(event, _turn_seq(event, focus.get(event.seq)))) is not None}
    items: list[ConversationItem] = []
    activity = None
    files: dict[str, FileEdit] = {}
    activity_scope = None
    root = f'task:{task_id}'
    for event in shown:
        node, p = focus.get(event.seq), event.payload
        turn = _turn_seq(event, node)
        key = _content_key(event, turn)
        duplicate = key is not None and key in keys
        if key is not None:
            keys.add(key)
        dag_id = node.id if node else root
        if history_payload(user, dag_id) != dag_id:
            dag_id = root
        common = {'id': f'event:{event.seq}', 'created_at': event.created_at, 'dag_node_id': dag_id}
        if event.type in OPERATION_EVENTS:
            if duplicate or event_is_start(event):
                continue
            identity = _agent_identity(event, task, node, user)
            adapter = AGENT_ADAPTERS.get(identity['backend'], _STANDARD)
            scope = event_scope(p)
            duration_s = timings.durations.get(event.seq)
            if activity is not None and activity.turn_seq == turn and activity_scope == scope:
                _add_operation(activity, event, adapter, user, files, duration_s)
            else:
                current, changed = TurnActivity(turn_seq=turn), {}
                _add_operation(current, event, adapter, user, changed, duration_s)
                if current.operations or current.files:
                    activity, files, activity_scope = current, changed, scope
                    items.append(ConversationItem(kind='operations', text='执行详情', activity=activity,
                                                  **identity, **common))
            continue
        if (event.type in ('agent.message', 'agent.turn_completed') and not duplicate
                and (text := _display(user, _reply_text(event), REPLY_LIMIT))):
            items.append(ConversationItem(kind='reply', text=text, **_agent_identity(event, task, node, user),
                                          **{**common, 'id': f'event:{event.seq}:reply'}))
            activity = None
        if event.type in ('agent.error', 'model.provider_error') and not duplicate:
            text = _error_text(event, user)
            items.append(ConversationItem(kind='error', text=text, **_agent_identity(event, task, node, user), **common))
            activity = None
        if event.type == 'user.message':
            source = ConversationItem(kind='message', text=_display(user, p.get('text'), 4000),
                                      mode=p.get('mode'), actor=p.get('actor'), message_id=p.get('message_id'),
                                      status=p.get('status', 'pending'), **common)
        elif text := _task_progress(event, task, node, user):
            scope = _display(user, p.get('subtask'), 50)
            if scope and event.type.startswith(('agent.', 'verification.', 'acceptance.', 'best_of.')):
                text = one_line(scope, 50) + ' · ' + text
            kind = 'receipt' if event.type.startswith('user.message.') else event.type
            status = event.type.rsplit('.', 1)[-1] if kind == 'receipt' else None
            identity = _agent_identity(event, task, node, user)
            if identity['agent_lane'] is None:
                identity = {'backend': identity['backend']} if kind.startswith('subtask.') else {}
            source = ConversationItem(kind=kind, text=text, message_id=p.get('message_id'),
                                      status=p.get('status', status), **identity, **common,
                                      **_milestone_details(event, timings, user))
        else:
            continue
        items.append(source)
        activity = None
    items = [ConversationItem.model_validate(history_payload(user, item.model_dump(exclude_none=True)))
             for item in items]
    return ConversationPage(items=items, cursor=str(position), has_more=len(page) > limit)
