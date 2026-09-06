"""基于 ReAct 循环的对话 Agent。

交互契约：
- main 把用户消息放入本 Agent 自带 inbox 的 turn 队列；
- turn() 负责：设置 phase.stage 为 'turn' -> 循环获取当前阶段对应的
  inbox 队列消息 -> 将消息持久化到 session -> 执行 step；
- step() 只从 session 组装 LLM 输入，调用 openai.stream 得到 LLM 消息与
  工具调用（func-call），执行工具拿到 func-res，并把 LLM 消息与工具结果
  写回 inbox.step 队列，交由 turn 的下一轮循环持久化；
- 直到模型直接给出最终回答（finish / max_token / error）。

每个 ReactAgent 初始化时实例化独立的 inbox 与 session：
- inbox: 本 Agent 私有的消息队列（turn / step 分区）；
- session: 本 Agent 私有的会话（自动生成 session_id 与 {session_id}.jsonl
  持久化文件，append 时同步落盘）。
"""
from typing import List

from src.inbox import InBox
from src.llm_adapter import LLM_CLIENT
from src.session import Session
from src.tools import tool_center
from src.types import (
    AssistantMessage,
    Phase,
    TextBlock,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
)


def _event_type_for(message) -> str:
    """根据消息类型确定写入 session 的事件类型。"""
    if isinstance(message, UserMessage):
        return "user/message"
    if isinstance(message, AssistantMessage):
        return "assistant/message"
    if isinstance(message, ToolResultMessage):
        return "tool/result"
    return "assistant/message"


class ReactAgent:
    def __init__(self, session_id: str | None = None, persist_dir: str = "sessions"):
        """初始化 Agent：实例化本 Agent 私有的 inbox 与 session。

        session 会自动创建 {persist_dir}/{session_id}.jsonl 持久化文件；
        不传 session_id 时自动生成 UUID。
        """
        self.tool_center = tool_center
        self.llm_client = LLM_CLIENT["openai"]
        self.inbox = InBox()
        self.session = Session(session_id=session_id, persist_dir=persist_dir)
        self.phase = Phase()

    async def turn(self) -> bool:
        """执行一轮对话。

        turn 只负责：设置阶段、获取 inbox 消息、持久化到 session，
        并驱动 step 循环直到 LLM 给出最终回答。
        """
        self.phase.turn += 1
        self.phase.stage = "turn"
        self.session.append(event_type="turn/start", data={"turn": self.phase.turn})

        while True:
            # 1. 获取当前阶段对应 inbox 队列中的消息
            queued = self.inbox.claim(self.phase.stage)
            if not queued:
                return False

            # 2. 将消息持久化到 session（供 derive_messages 组装 LLM 历史）
            for message in queued:
                self.session.append(event_type=_event_type_for(message), data=message)

            # 3. 执行一步；step 只从 session 获取 LLM 输入
            end_reason = await self._step()
            if end_reason in ("max_token", "finish", "error"):
                break

            # 4. 模型发出了工具调用，进入 step 阶段继续循环
            self.phase.stage = "step"

        # 5. 最后一轮 step 产生的 LLM 消息仍在 inbox 中，补记到 session
        for message in self.inbox.claim("step"):
            self.session.append(event_type=_event_type_for(message), data=message)

        self.session.append(event_type="turn/finish")
        return True

    async def _step(self) -> str:
        """执行一步：仅从 session 组装 LLM 输入，调用 LLM 并执行工具。"""
        self.phase.step += 1
        self.session.append(
            event_type="step/start", data={"step_idx": self.phase.step}
        )

        all_messages = self.session.derive_messages()
        assistant_message, end_reason = await self.llm_client.stream(
            all_messages, self.tool_center.get_schemas()
        )

        # 将 LLM 消息写回 inbox.step 队列，由 turn 下一轮循环持久化
        self.inbox.append("step", assistant_message)

        tool_calls = [
            block
            for block in assistant_message.content
            if isinstance(block, ToolCallBlock)
        ]
        for tc in tool_calls:
            args = tc.args_dict
            result = await self.tool_center.execute(tc.name, args)
            tool_result = ToolResultMessage(
                tool_call_id=tc.id,
                content=[TextBlock(content=result["content"])],
                is_error=result["is_error"],
            )
            self.inbox.append("step", tool_result)
        return end_reason
