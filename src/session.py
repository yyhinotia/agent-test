"""会话与执行事件记录。

每个 ReactAgent 初始化时实例化一个独立 Session：
- __init__ 自动创建 `{persist_dir}/{session_id}.jsonl` 作为持久化文件；
- append 在内存记录事件的同时，把事件 JSON 追加写入持久化文件；
- from_file 可从 jsonl 恢复 session（消息自动还原为对应 Message 模型）。
"""
import json
from pathlib import Path
from typing import Any, Dict, List

from src.types import (
    AssistantMessage,
    Message,
    SessionEvent,
    ToolResultMessage,
    UserMessage,
)
from src.utils import get_now, get_uuid


# role 字段 -> 具体消息模型。
# 注意: 不能用 TypeAdapter(Message) 做 union 判别，UserMessage / AssistantMessage
# 字段均可互相通过校验（role 无字面量约束），会导致 AssistantMessage 被还原成
# UserMessage，这里按 role 显式选择类型。
_MESSAGE_TYPES_BY_ROLE: Dict[str, Any] = {
    "user": UserMessage,
    "assistant": AssistantMessage,
    "tool": ToolResultMessage,
}


def _restore_data(data: Any) -> Any:
    """把 jsonl 中的 data 还原为对应的 Message 模型；非消息数据原样返回。"""
    if isinstance(data, dict) and data.get("role") in _MESSAGE_TYPES_BY_ROLE:
        try:
            return _MESSAGE_TYPES_BY_ROLE[data["role"]].model_validate(data)
        except Exception:
            return data
    return data


class Session:
    """会话事件记录，附带 JSONL 文件持久化。"""

    def __init__(self, session_id: str | None = None, persist_dir: str = "sessions"):
        """创建会话，并自动生成 {session_id}.jsonl 持久化文件（默认目录 sessions/）。"""
        self.session_id = session_id or get_uuid()
        self.persist_dir = Path(persist_dir)
        self.file_path = self.persist_dir / f"{self.session_id}.jsonl"
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self.file_path.touch(exist_ok=True)  # 自动创建持久化文件
        self.events: List[SessionEvent] = []

    def append(self, event_type: str, data: Any = None) -> None:
        """追加一条执行事件，并同步持久化到 {session_id}.jsonl（一行一条 JSON）。"""
        event = SessionEvent(
            seq=len(self.events),
            data=data,
            type=event_type,
            time=get_now(),
        )
        self.events.append(event)
        with self.file_path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(event.model_dump_json() + "\n")

    def derive_messages(self) -> List[Message]:
        """从事件中提取完整的 LLM 历史消息列表。"""
        return [event.data for event in self.events if isinstance(event.data, Message)]

    @classmethod
    def from_file(cls, session_id: str, persist_dir: str = "sessions") -> "Session":
        """从持久化文件恢复会话（消息自动还原为 Message 模型）。"""
        session = cls(session_id=session_id, persist_dir=persist_dir)
        session.events.clear()
        with session.file_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                raw["data"] = _restore_data(raw.get("data"))
                session.events.append(SessionEvent(**raw))
        return session
