"""执行事件与阶段定义（session 事件类型的唯一事实源）。"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict


class EventType(str, Enum):
    """会话执行事件类型（sessions/{session_id}.jsonl 每行事件的 type 字段）。"""

    TURN_START = "turn/start"
    STEP_START = "step/start"
    USER_MESSAGE = "user/message"
    ASSISTANT_MESSAGE = "assistant/message"
    TOOL_RESULT = "tool/result"
    STEP_END = "step/end"
    TURN_END = "turn/end"
    RUNTIME_ERROR = "runtime/error"


class AgentPhase(str, Enum):
    """Agent 顶层生命周期阶段。"""

    IDLE = "idle"
    RUNNING = "running"


class Phase:
    """对话阶段计数器：回合（turn）与步骤（step）编号 + 当前阶段名。"""

    def __init__(self) -> None:
        self.turn: int = 0
        self.step: int = 0
        self.stage: str = "turn"


class SessionEvent(BaseModel):
    """Session 中记录的一条事件（持久化到 JSONL 的行结构）。"""

    model_config = ConfigDict(frozen=True)

    seq: int
    type: str
    data: Any
    time: str