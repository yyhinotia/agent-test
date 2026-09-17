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
- **窗口化重载**：`reload()` 从文件尾部倒序读取，遇到第一条
  compact/summary 事件即停止（含该事件），只把「摘要 + 摘要之后的事件」
  载入 `events`（续聊所需的最小上下文窗口）；被跳过的磁盘前缀不载入
  内存但记账为 `_prefix_events`，`_persist_all` 重写时按行回填，保证
  磁盘仍是全量历史（知识增量）。
- **残缺尾部容错**：恢复（from_file / reload）时，末尾无换行结尾
  且解析失败的行视为「写一半」（上次写入被截断）：记告警、跳过
  并清理磁盘残片；中间行损坏仍抛 SessionEditError。append 前
  也会先清掉残片，避免新事件与残片拼成无法解析的行。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, BinaryIO, Dict, Iterator, List, Tuple

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


def _iter_lines_reverse(path: Path, block_size: int = 8192) -> Iterator[bytes]:
    """按行倒序迭代文件（最后一行 -> 第一行，产出不含换行符的 bytes）。

    从文件尾部按块向前读取，因此调用方可以在命中目标行后立即停止，
    不必把整份 JSONL 载入内存（Session.reload 遇到第一条
    compact 事件即停止）。空行原样产出，由调用方决定是否跳过；
    行尾换行符（含 CRLF 末尾的 CR 字符）一并去掉，且文件末行之后的
    多余空片段不计入倒数行号，使 Session.reload 的「倒数第 N 行」
    与真实文件行对齐。
    """
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        position = f.tell()
        pending = b""
        first_chunk = True
        while position > 0:
            size = min(block_size, position)
            position -= size
            f.seek(position)
            pending = f.read(size) + pending
            lines = pending.split(b"\n")
            if first_chunk:
                # 文件以换行结尾时，末尾会多出一个空片段（不是真实行），
                # 丢掉它，倒数行号才与真实行对齐。
                if lines and lines[-1] == b"":
                    lines.pop()
                first_chunk = False
            pending = lines[0]  # 首行可能被块边界截断，留到下一块处理
            for line in reversed(lines[1:]):
                yield line[:-1] if line.endswith(b"\r") else line
    if pending:
        yield pending[:-1] if pending.endswith(b"\r") else pending


def _file_ends_with_newline(path: Path) -> bool:
    """文件是否以换行结尾（空文件视为 True）。

    用于区分「写一半」的末尾残行：正常写出的 JSONL 每行都带换行，
    所以「末行无换行 + 解析失败」才能判定为被截断。
    """
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        if f.tell() == 0:
            return True
        f.seek(-1, os.SEEK_END)
        return f.read(1) == b"\n"


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
        self._archived: List[SessionEvent] = []  # 已压缩事件归档副本（内存窗口化后仍保有全量）
        self._seq: int = 0  # 持久化游标：下一条事件的 seq
        # 窗口化重载记账：磁盘上位于内存窗口之前的（已被摘要覆盖的）
        # 前缀事件数。非 0 时 _persist_all 先回填前缀再写窗口，避免
        # 用窗口覆盖全量历史（见 reload / _rewrite_with_prefix）。
        self._prefix_events: int = 0

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
            with self.file_path.open("a+b") as f:
                self._trim_torn_tail(f)
                f.write(event.model_dump_json().encode("utf-8") + b"\n")
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

    @staticmethod
    def _trim_torn_tail(f: BinaryIO) -> None:
        """追加前清掉末尾被截断的残片（JSONL 每行都应以换行结尾）。

        最后 1 字节不是换行 => 上次写入被截断：回退到最后一个换行之后并
        truncate，保证新事件自成一行；整个文件都没有换行则视为全残片清空。

        以二进制读写句柄操作：回退查找不涉及解码，不会踩到 UTF-8
        多字节字符边界。
        """
        f.seek(0, os.SEEK_END)
        size = f.tell()
        if size == 0:
            return
        f.seek(-1, os.SEEK_END)
        if f.read(1) == b"\n":
            return  # 正常结尾
        position = size
        while position > 0:
            block = min(4096, position)
            position -= block
            f.seek(position)
            index = f.read(block).rfind(b"\n")
            if index >= 0:
                f.truncate(position + index + 1)
                return
        f.truncate(0)

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

    def _parse_event_line(
        self,
        raw_line: str | bytes,
        *,
        location: str,
        where: str,
        detail: Dict[str, Any],
    ) -> SessionEvent:
        """把一行 JSONL 文本解析为 SessionEvent；失败抛 SessionEditError。

        from_file / reload 共用：行级解析失败时完整堆栈进日志，
        错误事实（含位置描述 where，如「第 2 行」/ 「倒数第 1 行」）
        随 SessionEditError 一并上抛。
        """
        text = (
            raw_line.decode("utf-8", errors="replace")
            if isinstance(raw_line, bytes)
            else raw_line
        )
        try:
            raw = json.loads(text)
            raw["data"] = _restore_data(raw.get("data"))
            return SessionEvent(**raw)
        except Exception as exc:  # noqa: BLE001
            RuntimeLog.capture_exception(
                exc,
                location=location,
                detail={
                    "session_id": self.session_id,
                    **detail,
                    "line": text.strip()[:200],
                },
            )
            raise SessionEditError(
                f"会话文件{where}解析失败: {self.file_path}",
                location=location,
                detail={"session_id": self.session_id, **detail},
            ) from exc

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
        torn_tail = False
        try:
            with session.file_path.open("r", encoding="utf-8") as f:
                for line_no, line in enumerate(f, 1):
                    if not line.strip():
                        continue
                    try:
                        # 行级解析失败：完整堆栈进日志，错误事实（含行号）
                        # 以 SessionEditError 形式向上抛出，便于定位与回放。
                        event = session._parse_event_line(
                            line,
                            location="Session.from_file",
                            where=f"第 {line_no} 行",
                            detail={"line_no": line_no},
                        )
                    except SessionEditError:
                        # 仅容忍末尾「写一半」的残行：文件不以换行结尾时，
                        # 迭代出的最后一行必定不带 "\n"（正常写入每行都带），
                        # 其余损坏行照旧抛错。
                        if line.endswith("\n"):
                            raise
                        torn_tail = True
                        RuntimeLog.warning(
                            "忽略会话文件末尾残缺行（疑似写一半）: %s 第 %s 行",
                            session.file_path,
                            line_no,
                        )
                        continue
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

        if torn_tail:
            # 清理磁盘上的残缺尾巴：否则后续 append 会与残片拼成坏行
            try:
                session._persist_all()
            except Exception:  # noqa: BLE001
                RuntimeLog.exception(
                    "残缺尾部清理失败（会话仍可用）: %s", session.file_path
                )

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

    def reload(self, *, strict: bool = False) -> "Session":
        """窗口化重载：倒序读取 JSONL，只加载最近一次压缩之后的上下文窗口。

        语义
        ----
        从文件尾部向头部**倒序**逐行读取，遇到第一条 compact/summary
        事件即停止（该事件计入窗口），再把收集到的事件按正序写入
        self.events。被压缩的历史已由摘要事件替代，「摘要 + 摘要之后的
        事件」正是继续对话所需的最小上下文窗口，因此长会话不必把
        全量历史读进内存。

        与 from_file 的差异
        -------------------
        - from_file：正序读全量事件（events = 磁盘全量），用于回放/审计；
        - reload  ：倒序读窗口（events = 摘要 + 近期事件），用于续聊；
        - 文件里没有 compact/summary 事件时退化为加载全部事件；
        - 被跳过的磁盘前缀（已被摘要覆盖的旧历史）不载入内存，但记账到
          self._prefix_events，后续 _persist_all 会先按行回填该前缀，保证
          磁盘始终是全量历史；
        - self._seq 取最后一条事件的 seq + 1（磁盘全局连续编号的下一条），
          重载后继续 append 不会与磁盘既有 seq 冲突。

        strict=True 时窗口内 seq 不连续抛 SessionContinuityError；
        strict=False（默认）仅记告警日志。返回 self。
        """
        window: List[SessionEvent] = []
        line_from_end = 0  # 倒数行号：1 = 文件最后一行
        torn_tail = False
        try:
            ends_with_newline = _file_ends_with_newline(self.file_path)
            for raw_line in _iter_lines_reverse(self.file_path):
                line_from_end += 1
                if not raw_line.strip():
                    continue
                try:
                    event = self._parse_event_line(
                        raw_line,
                        location="Session.reload",
                        where=f"倒数第 {line_from_end} 行",
                        detail={"line_from_end": line_from_end},
                    )
                except SessionEditError:
                    # 倒数第 1 行 + 文件不以换行结尾 => 写一半的残行；
                    # 其余损坏行照旧抛错。
                    if line_from_end == 1 and not ends_with_newline:
                        torn_tail = True
                        RuntimeLog.warning(
                            "忽略会话文件末尾残缺行（疑似写一半）: %s",
                            self.file_path,
                        )
                        continue
                    raise
                window.append(event)
                if event.type == EventType.COMPACT.value:
                    break  # 命中第一条 compact 事件：窗口起点已确定
        except OSError as exc:
            RuntimeLog.capture_exception(
                exc,
                location="Session.reload",
                detail={
                    "session_id": self.session_id,
                    "file_path": str(self.file_path),
                },
            )
            raise SessionEditError(
                f"无法读取会话文件: {self.file_path}",
                location="Session.reload",
                detail={"session_id": self.session_id},
            ) from exc

        window.reverse()  # 倒序读取 -> 正序窗口

        # 先校验再提交：strict 失败时不改动 self 原有状态
        seqs = [event.seq for event in window]
        if seqs and seqs != list(range(seqs[0], seqs[0] + len(seqs))):
            msg = (
                f"会话 {self.session_id} 重载窗口 seq 不连续: "
                f"{seqs} != {list(range(seqs[0], seqs[0] + len(seqs)))}"
            )
            if strict:
                raise SessionContinuityError(msg, location="Session.reload")
            RuntimeLog.warning(msg)

        self.events = window
        self._archived = []
        self._prefix_events = window[0].seq if window else 0
        self._seq = window[-1].seq + 1 if window else 0

        if torn_tail:
            # 清理磁盘残片（保留前缀与窗口）
            try:
                self._persist_all()
            except Exception:  # noqa: BLE001
                RuntimeLog.exception(
                    "残缺尾部清理失败（会话仍可用）: %s", self.file_path
                )
        return self

    @classmethod
    def resume(
        cls,
        session_id: str,
        persist_dir: str = "sessions",
        *,
        strict: bool = False,
    ) -> "Session":
        """窗口化恢复会话（from_file 的窗口版对偶）。

        等价于 Session(session_id, persist_dir).reload(strict=strict)：只加载
        「最近一次压缩之后」的上下文窗口（摘要 + 之后的事件），用于续聊；
        需要全量历史回放/审计时用 from_file。会话文件不存在时会被创建。
        """
        return cls(session_id=session_id, persist_dir=persist_dir).reload(
            strict=strict
        )

    def max_turn_step(self) -> Tuple[int, int]:
        """返回全量事件（_archived 归档 + 内存窗口）中最大的 turn / step。

        续聊恢复用：Agent 构造时据此续接 turn / step 编号，避免与历史事件
        重号（step 是全局递增计数器；重号会破坏 Compactor 的 (turn, step)
        全局排序）。无事件时返回 (0, 0)。

        注意：窗口化重载（reload）只把上下文窗口读进内存，返回的是窗口内
        的最大值——窗口起点通常是 compact/summary 事件，而该事件携带被压
        缩区间的 (turn, step) 上界（见 Compactor），所以编号上界不丢。
        """
        events = self._archived + self.events
        if not events:
            return (0, 0)
        return (
            max(event.turn for event in events),
            max(event.step for event in events),
        )

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

    def cut_to_context_window(self) -> None:
        """压缩后调用：内存 events 只保留上下文窗口（可进上下文的非压缩事件）。

        被压缩（compacted=True）的旧事件从内存移除并转入 _archived 归档；
        磁盘全量历史由压缩流程中的 _persist_all 保证，本方法只裁剪内存、
        不触发落盘，避免用窗口覆盖磁盘全量。后续 append 仍按全局 _seq
        游标追加并写盘，磁盘始终是全量 0..N-1 连续序列。
        """
        new_archived = [e for e in self.events if e.compacted]
        self._archived = sorted(
            self._archived + new_archived, key=lambda e: e.seq
        )
        self.events = [e for e in self.events if not e.compacted]

    def _renumber(self) -> None:
        """把全部事件（含归档副本）seq 重排为 0..N-1，并同步追加游标。

        内存窗口化后，全量 = _archived（历史归档）+ events（上下文窗口），
        重排必须作用于全量，否则磁盘 seq 会与窗口裁剪产生缺口。
        窗口化重载（reload）跳过了磁盘前缀，因此重排从
        self._prefix_events 起编号，与未载入的前缀 seq 不冲突。
        """
        all_events = self._archived + self.events
        for i, event in enumerate(all_events):
            target = self._prefix_events + i
            if event.seq != target:
                all_events[i] = event.model_copy(update={"seq": target})
        n_archived = len(self._archived)
        self._archived = all_events[:n_archived]
        self.events = all_events[n_archived:]
        self._seq = self._prefix_events + len(all_events)

    def _persist_all(self) -> None:
        """全量重写 JSONL：磁盘前缀 + 归档副本 + 上下文窗口。

        压缩打标 / 插入摘要 / 窗口化后调用，保证磁盘与内存全量一致
        （内存只保留上下文窗口，归档部分存于 _archived）。若本会话来自
        reload 的窗口化重载，会先按行回填未载入内存的磁盘前缀，避免用
        内存窗口覆盖全量历史。
        """
        all_events = sorted(self._archived + self.events, key=lambda e: e.seq)
        text = "".join(event.model_dump_json() + "\n" for event in all_events)
        try:
            if self._prefix_events:
                # 窗口化重载：先回填未载入内存的磁盘前缀，再写窗口
                self._rewrite_with_prefix(text)
            else:
                with self.file_path.open(
                    "w", encoding="utf-8", newline="\n"
                ) as f:
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

    def _rewrite_with_prefix(self, text: str) -> None:
        """写盘时回填未载入内存的磁盘前缀，再写入内存窗口事件。

        窗口化重载（reload）只把上下文窗口读进内存，磁盘上被摘要覆盖的
        前缀仍在文件里；全量重写必须先把该前缀按行原样搬过来，否则会用
        窗口覆盖全量历史。为保证失败时原文件完好，先写同目录临时
        文件，回填行数与 _prefix_events 不符时拒绝替换并删除临时文件。
        """
        tmp_path = self.file_path.with_name(self.file_path.name + ".tmp")
        copied = 0
        try:
            with self.file_path.open("r", encoding="utf-8") as src, tmp_path.open(
                "w", encoding="utf-8", newline="\n"
            ) as dst:
                for line in src:
                    if copied >= self._prefix_events:
                        break
                    if not line.strip():
                        continue
                    dst.write(line if line.endswith("\n") else line + "\n")
                    copied += 1
                if copied != self._prefix_events:
                    raise SessionEditError(
                        f"磁盘前缀事件数不符（期望 {self._prefix_events}，"
                        f"实际 {copied}），拒绝重写以免丢失历史: {self.file_path}",
                        location="Session._persist_all",
                        detail={
                            "session_id": self.session_id,
                            "expected_prefix": self._prefix_events,
                            "actual_prefix": copied,
                        },
                    )
                dst.write(text)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
        os.replace(tmp_path, self.file_path)

