"""会话与执行事件记录（含错误事件与持久化游标）。

设计要点（重构后 + 知识增量）
------------------------------
- **持久化游标**：append 的 seq 使用自维护的 `_seq` 游标，不再依赖
  `len(self.events)`，保证 from_file 恢复后继续追加的事件序号连续；
- **错误事件**：`append_error` 只记录错误“事实” —— 执行位置 + 报错类型
  + 消息摘要（runtime/error 事件），完整堆栈由 RuntimeLog 写入日志文件，
  二者通过 session_id 关联，既满足回放需求又不污染会话正文；
- **连续性校验**：from_file 恢复时按「seq 集合校验」`sorted(seqs) == range(N)`
  检查事件序号是否恰好为 0..N-1（知识增量）；
- **事件唯一事实源**：事件携带 turn/step/compacted 元数据（知识增量）：
  - Compactor 按 turn 分块，把历史回合摘要化为 compact/summary 事件；
  - derive_messages 跳过 compacted=True 事件，并把 COMPACT 事件的
    data(str) 包装为 UserMessage 重新进入上下文；
  - mark_compacted / insert_after / _persist_all 支持“追加 + 全量重写”：
    压缩时对旧事件打标、插入摘要事件并全量重写 JSONL，保证磁盘与
    内存一致。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from agent_test.exceptions.session import SessionContinuityError, SessionEditError
from agent_test.log.runtime_log import RuntimeLog
from agent_test.types.events import EventType, SessionEvent
from agent_test.types.messages import (
    AssistantMessage,
    Message,
    TextBlock,
    ToolResultMessage,
    UserMessage,
)
from agent_test.utils import get_now, get_uuid

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

    def __init__(
        self, session_id: str | None = None, persist_dir: str = "sessions"
    ):
        """创建会话，并自动生成 {session_id}.jsonl 持久化文件（默认目录 sessions/）。"""
        self.session_id = session_id or get_uuid()
        self.persist_dir = Path(persist_dir)
        self.file_path = self.persist_dir / f"{self.session_id}.jsonl"
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self.file_path.touch(exist_ok=True)  # 自动创建持久化文件
        self.events: List[SessionEvent] = []
        self._seq: int = 0  # 持久化游标：下一条事件的 seq

    # ---------- 事件追加 ----------

    @staticmethod
    def _validate_type(event_type: str | EventType) -> str:
        """校验并规范化事件类型字符串；非法时抛出 SessionEditError。"""
        if isinstance(event_type, EventType):
            type_value: str = event_type.value
        else:
            type_value = event_type
        try:
            EventType(type_value)
        except ValueError as exc:
            # 记录原始 ValueError 的真实堆栈；对外抛出统一领域异常
            RuntimeLog.capture_exception(
                exc,
                location="Session.append",
                detail={"event_type": repr(event_type)},
            )
            raise SessionEditError(
                f"非法事件类型: {event_type!r}",
                location="Session.append",
                detail={"event_type": repr(event_type)},
            ) from exc
        return type_value

    def append(
        self,
        event_type: str | EventType,
        data: Any = None,
        *,
        turn: int = 0,
        step: int = 0,
    ) -> SessionEvent:
        """追加一条执行事件并同步持久化（一行一条 JSON）。

        知识增量：追加时可通过 turn / step 标注事件所属回合与步骤，
        Compactor 按 turn 分块、按 step 排除近期。

        事件类型不合法时抛出 SessionEditError；返回写入的事件对象。
        """
        type_value = self._validate_type(event_type)

        event = SessionEvent(
            seq=self._seq,
            type=type_value,
            data=data,
            time=get_now(),
            turn=turn,
            step=step,
        )
        self.events.append(event)
        self._seq += 1
        try:
            with self.file_path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(event.model_dump_json() + "\n")
        except OSError as exc:
            RuntimeLog.capture_exception(
                exc,
                location="Session.append",
                detail={
                    "file_path": str(self.file_path),
                    "seq": event.seq,
                    "type": event.type,
                },
            )
            raise SessionEditError(
                f"事件持久化写入失败: {self.file_path}",
                location="Session.append",
                detail={"seq": event.seq, "type": event.type},
            ) from exc
        return event

    def append_error(
        self,
        location: str,
        error_type: str,
        message: str,
        *,
        detail: Dict[str, Any] | None = None,
        turn: int = 0,
        step: int = 0,
    ) -> SessionEvent:
        """记录一次运行时错误（事实进 session，堆栈进日志文件）。

        写入 runtime/error 事件，data 字段只含可回放的事实摘要：
            location / error_type / message / detail
        返回写入的事件对象。
        """
        return self.append(
            EventType.RUNTIME_ERROR,
            data={
                "location": location,
                "error_type": error_type,
                "message": message,
                "detail": detail or {},
            },
            turn=turn,
            step=step,
        )

    # ---------- 读取 / 恢复 ----------

    def derive_messages(self) -> List[Message]:
        """从事件中提取完整的 LLM 历史消息列表。

        知识增量：
        - 跳过 compacted=True 的旧事件（已被摘要替代）；
        - COMPACT 事件的 data(str) 摘要包装为 UserMessage 重新进入上下文。
        """
        messages: List[Message] = []
        for event in self.events:
            if event.compacted:
                continue
            data = event.data
            if event.type == EventType.COMPACT.value and isinstance(data, str):
                messages.append(
                    UserMessage(
                        id=f"compact-{event.seq}",
                        content=[TextBlock(content=data)],
                    )
                )
                continue
            if isinstance(data, Message):
                messages.append(data)
        return messages

    @classmethod
    def from_file(
        cls,
        session_id: str,
        persist_dir: str = "sessions",
        *,
        strict: bool = False,
    ) -> "Session":
        """从持久化文件恢复会话（消息自动还原为 Message 模型）。

        知识增量：恢复完成后做「seq 集合校验」`sorted(seqs) == range(N)`。
        strict=False（默认）：seq 集合不连续时仅记告警日志；
        strict=True：不连续直接抛 SessionContinuityError。
        """
        session = cls(session_id=session_id, persist_dir=persist_dir)
        session.events.clear()
        session._seq = 0
        try:
            with session.file_path.open("r", encoding="utf-8") as f:
                for line_no, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                        raw["data"] = _restore_data(raw.get("data"))
                        event = SessionEvent(**raw)
                    except Exception as exc:  # noqa: BLE001
                        # 行级解析失败：完整堆栈进日志，错误事实（含行号）
                        # 以 SessionEditError 形式向上抛出，便于定位与回放。
                        RuntimeLog.capture_exception(
                            exc,
                            location="Session.from_file",
                            detail={
                                "session_id": session_id,
                                "line_no": line_no,
                                "line": line[:200],
                            },
                        )
                        raise SessionEditError(
                            f"会话文件第 {line_no} 行解析失败: {session.file_path}",
                            location="Session.from_file",
                            detail={
                                "session_id": session_id,
                                "line_no": line_no,
                            },
                        ) from exc
                    session.events.append(event)
        except OSError as exc:
            # 文件打开/读取阶段失败（文件缺失、权限、IO 等）
            RuntimeLog.capture_exception(
                exc,
                location="Session.from_file",
                detail={
                    "session_id": session_id,
                    "file_path": str(session.file_path),
                },
            )
            raise SessionEditError(
                f"无法读取会话文件: {session.file_path}",
                location="Session.from_file",
                detail={"session_id": session_id},
            ) from exc

        # seq 集合校验: sorted(seqs) == range(N)
        session._seq = len(session.events)
        seqs = [event.seq for event in session.events]
        if seqs and sorted(seqs) != list(range(len(seqs))):
            msg = (
                f"会话 {session_id} 事件 seq 集合不连续: "
                f"{sorted(seqs)} != {list(range(len(seqs)))}"
            )
            if strict:
                raise SessionContinuityError(msg, location="Session.from_file")
            RuntimeLog.warning(msg)
        return session

    # ---------- 压缩 / 改写（知识增量） ----------

    def mark_compacted(self, seq_start: int, seq_end: int) -> int:
        """把 [seq_start, seq_end) 区间事件标记为 compacted 并全量重写。

        返回区间内最后一个事件的列表索引（供 insert_after 使用）。
        """
        if seq_start < 0 or seq_end <= seq_start:
            raise SessionEditError(
                "mark_compacted 区间非法",
                location="Session.mark_compacted",
                detail={"seq_start": seq_start, "seq_end": seq_end},
            )
        last_index = -1
        for i, event in enumerate(self.events):
            if seq_start <= event.seq < seq_end:
                if not event.compacted:
                    self.events[i] = event.model_copy(update={"compacted": True})
                last_index = i
        if last_index < 0:
            raise SessionEditError(
                "mark_compacted 区间内没有事件",
                location="Session.mark_compacted",
                detail={"seq_start": seq_start, "seq_end": seq_end},
            )
        self._persist_all()
        return last_index

    def insert_after(
        self,
        index: int,
        event_type: str | EventType,
        data: Any,
        *,
        turn: int = 0,
        step: int = 0,
    ) -> SessionEvent:
        """在 index 后插入一条事件（compact/summary 用）并全量重写。

        插入后对所有事件重新编号（seq = 0..N-1），保持磁盘 seq 集合
        连续（from_file 的 sorted(seqs) == range(N) 校验依赖此不变式）。
        """
        if not (0 <= index < len(self.events)):
            raise SessionEditError(
                "insert_after 索引越界",
                location="Session.insert_after",
                detail={"index": index, "len": len(self.events)},
            )
        type_value = self._validate_type(event_type)
        event = SessionEvent(
            seq=len(self.events) + 1,  # 插入后统一重新编号
            type=type_value,
            data=data,
            time=get_now(),
            turn=turn,
            step=step,
        )
        self.events.insert(index + 1, event)
        self._renumber()
        self._persist_all()
        return self.events[index + 1]

    def _renumber(self) -> None:
        """把全部事件 seq 重排为 0..N-1，并同步追加游标。"""
        for i, event in enumerate(self.events):
            if event.seq != i:
                self.events[i] = event.model_copy(update={"seq": i})
        self._seq = len(self.events)

    def _persist_all(self) -> None:
        """全量重写 JSONL，使磁盘与内存一致（压缩打标 / 插入摘要后调用）。"""
        try:
            text = "".join(event.model_dump_json() + "\n" for event in self.events)
            with self.file_path.open("w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        except OSError as exc:
            RuntimeLog.capture_exception(
                exc,
                location="Session._persist_all",
                detail={"file_path": str(self.file_path)},
            )
            raise SessionEditError(
                f"事件全量重写失败: {self.file_path}",
                location="Session._persist_all",
            ) from exc