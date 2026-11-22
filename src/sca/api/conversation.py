"""Project durable progress and steering into the Portal conversation format."""

from collections import defaultdict
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from sca.api.guest_history import history_payload
from sca.core.models import Event, Principal, Task, TaskState
from sca.core.task_dag import (
    TASK_MILESTONE_CONTEXT,
    TASK_MILESTONES,
    DAGNode,
    task_conversation_nodes,
)
from sca.db.repo import Store
from sca.evolve.dag import EvolutionHistory, build_evolution_dag
from sca.evolve.progress import evaluation_text, finish_text, gate_text, one_line
from sca.evolve.steering import ACTION_LABELS, ACTIVE_EVOLUTION_STATES, MESSAGE_EVENT


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


class ConversationPage(BaseModel):
    items: list[ConversationItem]
    cursor: str
    has_more: bool


def evolution_message(row: dict, *, status: str = 'pending') -> dict:
    payload = row['payload']
    return {
        'id': payload['item_id'], 'evolution_id': payload['evolution_id'], 'mode': payload['mode'],
        'text': payload.get('text'), 'remaining_budget_usd': payload.get('remaining_budget_usd'),
        'actor': row['actor'], 'created_at': row['created_at'], 'status': status,
    }


def evolution_item(row: dict) -> ConversationItem:
    item, at = row['payload'], row['created_at']
    if row['type'] != MESSAGE_EVENT:
        return ConversationItem(id=item['item_id'], kind=item['kind'], text=item['text'], created_at=at,
                                dag_node_id=item['dag_node_id'], message_id=item.get('message_id'),
                                status=item.get('status'))
    mode, text = item['mode'], item.get('text')
    if mode != 'guidance':
        label = ACTION_LABELS[mode]
        if mode == 'budget':
            label += f' · ${item["remaining_budget_usd"]:.6g}'
        text = f'{label} · {text}' if text else label
    return ConversationItem(id=item['item_id'], kind='message', text=text, mode=mode, actor=row['actor'],
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
                            created_at=row['finished_at'], dag_node_id=f'evolution:{row["id"]}')


async def evolution_page(store: Store, evolution_id: UUID, cursor: str | None, limit: int) -> ConversationPage:
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
        transcript = sorted([*legacy_evolution_items(history), *map(evolution_item, entries)],
                            key=lambda item: item.created_at)
        items = transcript[offset:offset + limit]
        more = offset + len(items) < len(transcript)
        if more or row['status'] in ACTIVE_EVOLUTION_STATES:
            return ConversationPage(items=items, cursor=f'h:{offset + len(items)}', has_more=more)
        last_id = entries[-1]['id'] if entries else 0
        return ConversationPage(items=items, cursor=f'{"x" if imported_stop else "e"}:{last_id}', has_more=False)
    page = entries[:limit]
    items = [evolution_item(entry) for entry in page]
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


TASK_ENDS = {TaskState.SUCCEEDED: '任务完成', TaskState.FAILED: '任务失败', TaskState.CANCELLED: '任务已取消'}


def _task_control_receipt(event: Event) -> str:
    p = event.payload
    place = (f'第 {p["plan_attempt"]} 次规划' if p.get('plan_attempt') is not None
             else f'第 {p["turn"]} 轮' if p.get('turn') is not None else '执行步骤')
    return ('插话已应用于' if event.type == 'user.message.applied' else '插话已送达') + place


def _turn_text(event: Event, node: DAGNode | None, nodes: dict[str, DAGNode]) -> str:
    status = {'agent.turn_started': '开始', 'agent.turn_completed': '完成', 'agent.turn_interrupted': '已打断'}
    number = event.payload.get('turn', node.meta.get('turn', 1) if node else 1)
    label = f'第 {number} 轮{status[event.type]}'
    if node and event.type != 'agent.turn_started' and (group := nodes.get(f'{node.id}/tool_group')):
        tools = group.meta['tools']
        label += f' · 工具调用 {len(tools)} 次'
        top_tools = sorted(group.meta['counts'].items(), key=lambda item: (-item[1], item[0]))[:2]
        if top_tools:
            label += ' · ' + '、'.join(f'{one_line(name, 24)} × {count}' for name, count in top_tools)
        if failed := sum(tool['failed'] for tool in tools):
            label += f'（{failed} 失败）'
    return label


def _task_progress(event: Event, task: Task, node: DAGNode | None, nodes: dict[str, DAGNode]) -> str | None:
    p = event.payload
    match event.type:
        case 'task.created':
            subtasks = task.spec.get('subtasks') or []
            return f'任务已提交 · 已规划 {len(subtasks)} 个子任务' if subtasks else '任务已提交'
        case 'task.state_changed':
            label = TASK_ENDS.get(p.get('to'))
            return f'{label} · {one_line(str(p["reason"]))}' if label and p.get('reason') else label
        case 'agent.turn_started' | 'agent.turn_completed' | 'agent.turn_interrupted':
            return _turn_text(event, node, nodes)
        case 'plan.started':
            return '开始规划子任务'
        case 'plan.completed':
            count = len(p.get('subtasks') or [])
            return f'规划完成 · {count} 个子任务' if count else '规划完成 · 采用单任务执行'
        case 'plan.rejected':
            return '子任务规划未通过' + (f' · {one_line(str(p["reason"]))}' if p.get('reason') else '')
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
            return '进入规划审核' if 'plan' in p else '进入人工审核'
        case 'user.message.delivered' | 'user.message.applied':
            return _task_control_receipt(event)
    return None


async def task_page(store: Store, task_id: UUID, user: Principal, after: int, limit: int) -> ConversationPage:
    task, page, context = await store.task_conversation_events(
        task_id, after, limit + 1, TASK_MILESTONES, TASK_MILESTONE_CONTEXT,
    )
    if not page:
        return ConversationPage(items=[], cursor=str(after), has_more=False)
    shown = page[:limit]
    position = shown[-1].seq
    focus, nodes = task_conversation_nodes(task, [event for event in context if event.seq <= position])
    items = []
    for event in shown:
        node, p = focus.get(event.seq), event.payload
        root = f'task:{task_id}'
        dag_id = node.id if node else root
        if history_payload(user, dag_id) != dag_id:
            dag_id = root
        if event.type == 'user.message':
            source = ConversationItem(id=f'event:{event.seq}', kind='message', text=str(p.get('text', '')),
                                      created_at=event.created_at, dag_node_id=dag_id, mode=p.get('mode'),
                                      actor=p.get('actor'), message_id=p.get('message_id'))
        elif text := _task_progress(event, task, node, nodes):
            scope = p.get('subtask')
            if scope and event.type.startswith(('agent.', 'verification.', 'acceptance.', 'best_of.')):
                text = one_line(str(scope), 50) + ' · ' + text
            kind = 'receipt' if event.type.startswith('user.message.') else event.type
            source = ConversationItem(id=f'event:{event.seq}', kind=kind, text=text, created_at=event.created_at,
                                      dag_node_id=dag_id, message_id=p.get('message_id'),
                                      status=p.get('status') if kind == 'receipt' else None)
        else:
            continue
        items.append(ConversationItem.model_validate(history_payload(user, source.model_dump(exclude_none=True))))
    return ConversationPage(items=items, cursor=str(position), has_more=len(page) > limit)
