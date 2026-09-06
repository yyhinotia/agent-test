"""会话消息模型（基于 Pydantic v2）。"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from pydantic import BaseModel

from agent_test.exceptions.message import MessageEditError


class TextBlock(BaseModel):
    """文本内容块。"""

    content: str


class ToolCallBlock(BaseModel):
    """工具调用请求块。"""

    id: str
    name: str
    args: str

    @property
    def args_dict(self) -> Dict[str, Any]:
        """解析 args JSON 字符串为参数字典。

        解析失败抛出 MessageEditError：错误类型与 location 会进入 session 的
        runtime/error 事件便于回放，完整堆栈由上层统一写入日志文件。
        """
        try:
            return json.loads(self.args)
        except ValueError as exc:
            raise MessageEditError(
                f"工具调用参数 JSON 解析失败: {exc}",
                location="ToolCallBlock.args_dict",
                detail={"tool_call_id": self.id, "name": self.name},
            ) from exc


ContentBlock = TextBlock | ToolCallBlock


class UserMessage(BaseModel):
    """用户消息。"""

    id: str
    role: str = "user"
    content: List[TextBlock]


class AssistantMessage(BaseModel):
    """助手（模型输出）消息。"""

    id: str
    role: str = "assistant"
    content: List[ContentBlock]


class ToolResultMessage(BaseModel):
    """工具调用结果消息。"""

    tool_call_id: str
    content: List[TextBlock]
    role: str = "tool"
    is_error: bool = False


Message = UserMessage | AssistantMessage | ToolResultMessage