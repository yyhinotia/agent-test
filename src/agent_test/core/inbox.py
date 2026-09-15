"""消息队列: turn 消息与 step 消息分区存放。

每个 ReactAgent 初始化时实例化一个独立的 InBox，避免全局状态在多轮/多
Agent 之间串扰。知识增量后 Agent 采用「先入 inbox.step -> 下一步 pre_step
claim -> 写 session」的消息流：LLM 返回与工具结果先进 step 队列，由下一步
的 pre_step claim 或回合收尾统一落 session（见 ReactAgent._flush_step_messages）。
"""
from __future__ import annotations

from typing import List

from agent_test.types.messages import Message


class InBox:
    def __init__(self) -> None:
        self.turns: List[Message] = []
        self.steps: List[Message] = []

    def append(self, flag: str, message: Message) -> None:
        """inbox 追加待处理消息（'turn' 进 turn 队列，其余进 step 队列）。"""
        if flag == "turn":
            self.turns.append(message)
        else:
            self.steps.append(message)

    def claim(self, flag: str) -> List[Message]:
        """获取特定阶段（'turn' / 其他）的全部消息并清空。"""
        messages: List[Message] = []
        if flag == "turn":
            messages.extend(self.turns)
            self.turns.clear()
        else:
            messages.extend(self.steps)
            self.steps.clear()
        return messages

    def has_pending(self) -> bool:
        """判断 turn 队列是否存在等待消息。"""
        return len(self.turns) > 0

    def has_step_pending(self) -> bool:
        """判断 step 队列是否存在等待写入 session 的消息（知识增量）。"""
        return len(self.steps) > 0