"""消息队列: turn 消息与 step 消息分区存放。

每个 ReactAgent 初始化时实例化一个独立的 InBox，避免全局状态在多轮/多
Agent 之间串扰。重构后 Agent 采用“产出即持久化”，step 消息不再回写
inbox，但 InBox 的 turn/step 分区 API 保留，便于外部向队列注入消息。
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