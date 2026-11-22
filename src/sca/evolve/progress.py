"""Short descriptions shared by evolution activities and historical projections."""

from sca.eval.trajectory import is_errored, overall_passed
from sca.evolve.steering import JournalItem
from sca.history.privacy import clean_text

SPLIT_NAMES = {'dev': '开发集', 'holdout': '留出集'}
GATE_REASONS = {
    'awaiting approval': '等待人工审批',
    'static checks failed': '静态检查未通过',
    'evaluation stopped': '评测已停止',
    'not selected or dev gate failed': '未入选或未通过开发集门禁',
    'holdout gate passed (quality)': '质量提升，通过留出集门禁',
    'holdout gate passed (efficiency)': '效率提升，通过留出集门禁',
    'quality win on dev; holdout gate or approval rejected': '开发集质量提升，留出集门禁或审批未通过',
    'efficiency win on dev; holdout gate or approval rejected': '开发集效率提升，留出集门禁或审批未通过',
}
FINISH_LABELS = {
    'completed': '演进完成', 'succeeded': '演进完成', 'failed': '演进失败',
    'stopped': '演进已停止', 'cancelled': '演进已取消',
    'budget_exhausted': '演进停止，预算已用尽', 'patience_exhausted': '演进停止，连续多代未晋升',
}


def one_line(text: str, length: int = 220) -> str:
    line = ' '.join(clean_text(text).split())
    return line if len(line) <= length else line[:length - 1] + '…'


def progress(id_: str, kind: str, text: str, node_id: str) -> JournalItem:
    return {'item_id': id_, 'kind': kind, 'text': text, 'dag_node_id': node_id}


def evaluation_text(role: str, split: str, rows: list[dict], status: str, reason: str | None = None) -> str:
    title = {'baseline': '基线评测结果', 'comparison': '当代冠军评测结果'}.get(role, '候选评测结果')
    split_name = SPLIT_NAMES.get(split, split)
    passed = sum(not is_errored(row) and overall_passed(row) for row in rows)
    cost = sum(float(row['cost_usd']) for row in rows)
    suffix = {'failed': ' · 评测失败', 'cancelled': ' · 已跳过', 'stopped': ' · 已停止',
              'budget_exhausted': ' · 预算用尽'}.get(status, '')
    if reason:
        suffix += f' · {one_line(reason)}'
    return f'{title}（{split_name}） · {passed}/{len(rows)} 通过 · ${cost:.6g}{suffix}'


def gate_text(decision: str, reason: str | None) -> str:
    label = {'promoted': '通过', 'invalid': '未通过', 'rejected': '未通过', 'pending': '待审批'}[decision]
    note = GATE_REASONS.get(reason, reason) if reason else None
    return f'门禁{label}' + (f' · {one_line(note)}' if note else '')


def finish_text(status: str, reason: str | None) -> str:
    return FINISH_LABELS.get(status, '演进已结束') + (f' · {one_line(reason)}' if reason else '')
