"""上下文压缩器：按 turn 分块，把历史回合摘要化为 compact/summary 事件。

依赖 Session + EventType.COMPACT（知识增量）：
- plan_blocks: 按 turn 分块，排除已 compacted 事件与最近 recent_turns 个回合；
- compact: 两级压缩
    - 一级：压缩除最近一轮外的全部历史回合；
    - 二级：若一级无可压缩块（如会话只有一轮），降级为全量压缩
      （recent_turns=0），保证阈值触发时必然能腾出空间；
- 摘要文本可外部注入（测试/确定性环境），默认走 LLM 客户端，
  无 LLM 或调用失败时保守截断，不阻塞压缩。
"""
from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable, Dict, List, Tuple

from agent_test.session.session import Session
from agent_test.types.events import EventType
from agent_test.types.messages import (
    AssistantMessage,
    Message,
    TextBlock,
    ToolResultMessage,
    UserMessage,
)
from agent_test.utils import get_uuid

# 每轮压缩的提示词前缀
_SUMMARIZE_PROMPT = (
    "请把以下对话历史压缩为简洁的中文摘要，保留关键事实、用户意图、"
    "已完成的动作与任务结论，不要遗漏重要信息：\n\n"
)

Block = Tuple[int, int, int]  # (seq_start, seq_end, turn)
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
    """基于 Session 的多回合上下文压缩器。"""

    def __init__(
        self,
        session: Session,
        *,
        summarize: Summarizer | None = None,
        llm_client: Any | None = None,
    ):
        """初始化。

        session:   目标会话（内部事件即唯一事实源）；
        summarize: 可选摘要函数（str -> str 或 async str），用于测试/
                   确定性场景；缺省时尝试 llm_client，再退化为截断。
        llm_client: 默认摘要用的 LLM 客户端（需提供 stream 方法）。
        """
        self.session = session
        self._summarize = summarize
        self.llm_client = llm_client

    # ---------- 分块规划 ----------

    def plan_blocks(self, recent_turns: int = 1) -> List[Block]:
        """按 turn 分块，返回可压缩区块 [(seq_start, seq_end, turn)]。

        只考虑未 compacted 的消息事件；排除最近 recent_turns 个回合
        （recent_turns=0 表示全量可压缩）。
        """
        live_by_turn: Dict[int, List[Tuple[int, int]]] = {}
        for event in self.session.events:
            if event.compacted:
                continue
            if not isinstance(event.data, Message):
                continue
            live_by_turn.setdefault(event.turn, []).append(event.seq)

        turns = sorted(live_by_turn)
        if recent_turns <= 0:
            compactable = turns
        elif len(turns) > recent_turns:
            compactable = turns[:-recent_turns]
        else:
            compactable = []

        blocks: List[Block] = []
        for turn in compactable:
            seqs = live_by_turn[turn]
            # 事件按 seq 顺序追加，同一 turn 的事件在 seq 上连续
            blocks.append((min(seqs), max(seqs) + 1, turn))
        return blocks

    # ---------- 摘要 ----------

    def _block_text(self, seq_start: int, seq_end: int) -> str:
        """提取 [seq_start, seq_end) 区间内消息事件的纯文本。"""
        parts: List[str] = []
        for event in self.session.events:
            if not (seq_start <= event.seq < seq_end):
                continue
            data = event.data
            if isinstance(data, UserMessage):
                parts.append(f"[user] {_text_of(data)}")
            elif isinstance(data, AssistantMessage):
                parts.append(f"[assistant] {_text_of(data)}")
            elif isinstance(data, ToolResultMessage):
                marker = "error" if data.is_error else "ok"
                parts.append(f"[tool:{data.tool_call_id}:{marker}] {_text_of(data)}")
        return "\n".join(parts)

    async def _summarize_text(self, text: str) -> str:
        """生成摘要：注入函数 > LLM 客户端 > 保守截断。"""
        if self._summarize is not None:
            result = self._summarize(text)
            if inspect.isawaitable(result):
                result = await result
            return result

        if self.llm_client is not None and hasattr(self.llm_client, "stream"):
            prompt = UserMessage(
                id=get_uuid(), content=[TextBlock(content=_SUMMARIZE_PROMPT + text)]
            )
            try:
                assistant, _, _ = await self.llm_client.stream([prompt], None)
                return _text_of(assistant)
            except Exception:  # noqa: BLE001
                pass
        return _truncate(text)

    # ---------- 压缩执行 ----------

    async def compact(self, recent_turns: int | None = None) -> int:
        """执行两级压缩，返回压缩的 turn 块数。

        recent_turns=None（默认）：一级压缩保留最近一轮；若一级无可压缩
        块，自动降级二级全量压缩（recent_turns=0）。
        recent_turns=N：仅压缩除最近 N 轮外的历史回合，不做降级。
        """
        if recent_turns is None:
            count = await self._compact_pass(1)
            if count == 0:
                count += await self._compact_pass(0)
            return count
        return await self._compact_pass(recent_turns)

    async def _compact_pass(self, recent_turns: int) -> int:
        """单轮压缩：逐块摘要在原地打标 + 插入摘要事件（块间重新规划，避免 seq 漂移）。"""
        count = 0
        while True:
            blocks = self.plan_blocks(recent_turns=recent_turns)
            if not blocks:
                break
            seq_start, seq_end, turn = blocks[0]
            text = self._block_text(seq_start, seq_end)
            summary = await self._summarize_text(text)

            last_index = self.session.mark_compacted(seq_start, seq_end)
            self.session.insert_after(
                last_index, EventType.COMPACT, summary, turn=turn, step=0
            )
            count += 1
        return count