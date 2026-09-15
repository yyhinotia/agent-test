"""上下文压缩器：按 (turn,step) 全局排序、以 step 粒度排除近期，把历史
上下文摘要化为 compact/summary 事件。

知识包契约（docs/knowledge.txt）：
- compact_session(events)：输出 "[turn=N step=S] Role: content" 文本行，
  ToolResult 超过 tool_head + tool_tail 时 head/tail 裁剪；
- compact()：收集 (turn,step) 对 -> 全局排序 -> 排除最近 remain_turns 个
  step（按 step 粒度，非按 turn——按 turn 排除在单 turn 多 step 场景
  静默失效）-> 筛选 compacted=False 且 (turn,step) < cutoff 的事件 ->
  compact_session 提取文本 -> 摘要生成（注入 summarize > LLM 客户端 >
  保守截断）-> mark_compacted + insert_after + _persist_all；
- 两级压缩：一级按 remain_turns 保留近期 step；无可压缩块时降级二级
  全量压缩（remain_turns=0），保证阈值触发时必然能腾出空间。
"""
from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable, List, Sequence, Tuple

from agent_test.session.session import Session
from agent_test.types.events import EventType, SessionEvent
from agent_test.types.messages import (
    AssistantMessage,
    Message,
    TextBlock,
    ToolResultMessage,
    UserMessage,
)
from agent_test.utils import get_uuid

# 摘要提示词：固定 8 章节结构（知识包 COMPACT_PROMPT 输出格式）
_COMPACT_PROMPT = (
    "请把以下对话历史压缩为结构化摘要，严格按固定 8 章节输出：\n"
    "Primary Request / Key Technical Concepts / Files and Code / "
    "Errors and Fixes / Pending Jobs / Current Work / Next Step / "
    "Critical Context\n\n"
)

Summarizer = Callable[[str], str | Awaitable[str]]


def _text_of(message: Message) -> str:
    """提取消息的纯文本内容。"""
    blocks = getattr(message, "content", None) or []
    return "".join(
        block.content for block in blocks if isinstance(block, TextBlock)
    )


def _truncate(text: str, limit: int = 800) -> str:
    """保守降级摘要：保留头尾，中间用省略标记。"""
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + "\n……（中间省略）……\n" + text[-half:]


class Compactor:
    """基于 Session 的上下文压缩器（依赖 Session + EventType.COMPACT）。"""

    def __init__(
        self,
        session: Session,
        *,
        tool_head: int = 1024,
        tool_tail: int = 1024,
        remain_turns: int = 2,
        summarize: Summarizer | None = None,
        llm_client: Any | None = None,
    ):
        """初始化。

        session:      目标会话（内部事件即唯一事实源）；
        tool_head:    ToolResult 文本保留头部字符数（默认 1024，可配）；
        tool_tail:    ToolResult 文本保留尾部字符数（默认 1024，可配）；
        remain_turns: 保留最近 N 个 step（按 (turn,step) 粒度，非按 turn；
                      默认 2，可配）；
        summarize:    可选摘要函数（str -> str 或 async str），用于测试/
                      确定性场景；缺省时尝试 llm_client，再退化为截断；
        llm_client:   默认摘要用的 LLM 客户端（需提供 stream 方法）。
        """
        self.session = session
        self.tool_head = tool_head
        self.tool_tail = tool_tail
        self.remain_turns = remain_turns
        self._summarize = summarize
        self.llm_client = llm_client

    # ---------- 文本提取 ----------

    def _clip_tool_result(self, text: str) -> str:
        """ToolResult 文本超窗口时 head/tail 裁剪（知识包 tool_head/tool_tail）。"""
        limit = self.tool_head + self.tool_tail
        if len(text) <= limit:
            return text
        omitted = len(text) - limit
        return (
            text[: self.tool_head]
            + f"\n…[中间省略 {omitted} 字符]…\n"
            + text[-self.tool_tail :]
        )

    def _line_for(self, event: SessionEvent) -> str | None:
        """把一条消息事件格式化为 "[turn=N step=S] Role: content" 文本行。"""
        data = event.data
        if isinstance(data, UserMessage):
            return f"[turn={event.turn} step={event.step}] user: {_text_of(data)}"
        if isinstance(data, AssistantMessage):
            return (
                f"[turn={event.turn} step={event.step}] assistant: "
                f"{_text_of(data)}"
            )
        if isinstance(data, ToolResultMessage):
            marker = "error" if data.is_error else "ok"
            return (
                f"[turn={event.turn} step={event.step}] "
                f"tool:{data.tool_call_id}:{marker}: "
                f"{self._clip_tool_result(_text_of(data))}"
            )
        return None

    def compact_session(self, events: Sequence[SessionEvent]) -> List[str]:
        """契约方法：把待压缩事件提取为文本行列表（ToolResult 裁剪）。

        输入为已筛选（compacted=False 且 (turn,step) < cutoff）的事件；
        输出每行 "[turn=N step=S] Role: content"。
        """
        lines: List[str] = []
        for event in sorted(events, key=lambda e: (e.turn, e.step, e.seq)):
            line = self._line_for(event)
            if line is not None:
                lines.append(line)
        return lines

    # ---------- 可压缩事件筛选（按 step 粒度排除） ----------

    def _eligible_events(self, remain_turns: int) -> List[SessionEvent]:
        """收集 (turn,step) 全局排序，排除最近 remain_turns 个 step。

        remain_turns=0 表示全量可压缩（二级降级）。按 step 粒度排除而
        非按 turn：单 turn 多 step 场景下按 turn 会静默失效。
        """
        messages = [
            e
            for e in self.session.events
            if not e.compacted and isinstance(e.data, Message)
        ]
        pairs = sorted({(e.turn, e.step) for e in messages})
        if not pairs or len(pairs) <= remain_turns:
            return []
        cutoff = pairs[len(pairs) - remain_turns] if remain_turns > 0 else None
        if cutoff is None:
            return messages
        return [e for e in messages if (e.turn, e.step) < cutoff]

    # ---------- 摘要 ----------

    async def _summarize_text(self, text: str) -> str:
        """生成摘要：注入函数 > LLM 客户端 > 保守截断。"""
        if self._summarize is not None:
            result = self._summarize(text)
            if inspect.isawaitable(result):
                result = await result
            return result

        if self.llm_client is not None and hasattr(self.llm_client, "stream"):
            prompt = UserMessage(
                id=get_uuid(), content=[TextBlock(content=_COMPACT_PROMPT + text)]
            )
            try:
                assistant, _, _ = await self.llm_client.stream([prompt], None)
                return _text_of(assistant)
            except Exception:  # noqa: BLE001
                pass
        return _truncate(text)

    # ---------- 压缩执行 ----------

    async def compact(self, remain_turns: int | None = None) -> str | None:
        """执行压缩，返回摘要文本；无需压缩时返回 None。

        remain_turns=None（默认）：一级按 self.remain_turns 保留近期
        step；若无可压缩事件，自动降级二级全量压缩（remain_turns=0）。
        """
        if remain_turns is None:
            summary = await self._compact_pass(self.remain_turns)
            if summary is None:
                summary = await self._compact_pass(0)
            return summary
        return await self._compact_pass(remain_turns)

    async def _compact_pass(self, remain_turns: int) -> str | None:
        """单轮压缩：筛选 -> 提取 -> 摘要 -> mark_compacted + insert_after。"""
        eligible = self._eligible_events(remain_turns)
        if not eligible:
            return None

        lines = self.compact_session(eligible)
        text = "\n".join(lines)
        summary = await self._summarize_text(text)

        seq_start = min(e.seq for e in eligible)
        seq_end = max(e.seq for e in eligible) + 1
        last_index = self.session.mark_compacted(seq_start, seq_end)
        last_compacted = max(eligible, key=lambda e: e.seq)
        self.session.insert_after(
            last_index,
            EventType.COMPACT,
            summary,
            turn=last_compacted.turn,
            step=0,
        )
        return summary