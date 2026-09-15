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
    TOOL_CALL = "tool/call"
    STEP_END = "step/end"
    TURN_END = "turn/end"
    RUNTIME_ERROR = "runtime/error"
    COMPACT = "compact/summary"  # Compactor 生成的摘要事件


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
    """Session 中记录的一条事件（持久化到 JSONL 的行结构）。

    知识增量新增三个字段（Compactor / TokenMeter 依赖）：
    - turn:      事件所属回合编号。turn() 开始时 self.phase.turn += 1，
                 同一 turn 内可能有多个 step；
    - step:      turn 内每次调用 LLM 的编号。_step() 开始时
                 self.phase.step += 1，step 是全局递增计数器；
    - compacted: 已被压缩摘要替代（Compactor mark_compacted 打标），
                 derive_messages 跳过此类事件。

    turn=0 / step=0 是旧 JSONL 的兼容默认值，from_file 恢复时自动填充。
    """

    model_config = ConfigDict(frozen=True)

    seq: int
    type: str
    data: Any
    time: str
    turn: int = 0
    step: int = 0
    compacted: bool = False