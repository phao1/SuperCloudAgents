"""Evolution conversation messages and the workflow's steering state."""

from dataclasses import dataclass, field
from typing import Literal, NotRequired, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

ACTIVE_EVOLUTION_STATES = frozenset({'queued', 'running', 'paused', 'awaiting_approval'})
MESSAGE_EVENT = 'conversation.message'
PROGRESS_EVENT = 'conversation.progress'
GUIDANCE_MODES = frozenset({'guidance', 'next', 'now'})

EvolutionMessageMode = Literal['guidance', 'next', 'now', 'pause', 'resume', 'stop', 'skip', 'budget']
ReceiptStatus = Literal['applied', 'rejected', 'superseded']
ACTION_LABELS = {
    'pause': '暂停演进', 'resume': '恢复演进', 'stop': '本代结束后停止',
    'skip': '跳过当前候选', 'budget': '调整剩余预算',
}


class EvolutionMessageInput(BaseModel):
    model_config = ConfigDict(extra='forbid')

    text: str | None = Field(default=None, max_length=4000)
    mode: EvolutionMessageMode = 'guidance'
    remaining_budget_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode='after')
    def validate_message(self) -> 'EvolutionMessageInput':
        if self.text is not None and (not self.text.strip() or '\x00' in self.text):
            raise ValueError('请输入有效的演进消息')
        if self.mode in GUIDANCE_MODES and self.text is None:
            raise ValueError('请输入演进指引')
        if (self.mode == 'budget') != (self.remaining_budget_usd is not None):
            raise ValueError('请仅在调整预算时提供有效的剩余预算')
        return self


class SteeringMessage(TypedDict):
    id: str
    mode: EvolutionMessageMode
    seq: NotRequired[int]
    text: NotRequired[str]
    remaining_budget_usd: NotRequired[float]


class JournalItem(TypedDict):
    item_id: str
    kind: str
    text: str
    dag_node_id: str
    message_id: NotRequired[str]
    status: NotRequired[ReceiptStatus]
    generation: NotRequired[int]
    total_budget_usd: NotRequired[float]


def receipt(message: SteeringMessage, text: str, node_id: str, *,
            status: ReceiptStatus = 'applied') -> JournalItem:
    return {'item_id': f'receipt:{message["id"]}', 'kind': 'receipt', 'text': text,
            'dag_node_id': node_id, 'message_id': message['id'], 'status': status}


@dataclass
class SteeringState:
    seen: set[str] = field(default_factory=set)
    pending: list[SteeringMessage] = field(default_factory=list)
    guidance: SteeringMessage | None = None
    guidance_applied: bool = False
    paused: bool = False
    stop_after_generation: bool = False

    def receive(self, message: SteeringMessage) -> bool:
        if message['id'] in self.seen:
            return False
        self.seen.add(message['id'])
        return True
