"""会话消息模型（基于 Pydantic v2）。"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Literal, Literal

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


ApprovalDecision = Literal["auto", "approved", "denied", "blocked"]
ExecutionStatus = Literal["not_started", "success", "failed"]


class ApprovalResult(BaseModel):
    """审批状态机：回答「是否允许执行」。

    与 ExecutionResult 拆成两个独立状态机（知识增量）：旧的 is_error 同时
    表达「审批被拒」与「执行失败」，LLM 无法区分二者。

    decision 取值：
        auto      策略自动放行（无需人工审批）
        approved  需审批且已获批准
        denied    需审批但被用户拒绝
        blocked   被策略直接拒绝（DENY）
    """

    decision: ApprovalDecision = "auto"
    required: bool = False
    source: str = "policy"
    reason_code: str = ""
    reason: str = ""


class ExecutionResult(BaseModel):
    """执行状态机：回答「执行结果如何」。

    status 取值：
        not_started  审批未通过，执行从未开始（与 failed 语义不同）
        success      已执行且成功
        failed       已执行但失败
    """

    status: ExecutionStatus = "success"
    error: str = ""


class ToolResultMessage(BaseModel):
    """工具调用结果消息。

    approval / execution（知识增量）：审批与执行是两个独立状态机；
    is_error 保留用于向后兼容（等价于 execution.status != "success"）。
    旧 JSONL 无这两个字段，缺省 None（按需回退到 is_error 语义）。
    """

    tool_call_id: str
    content: List[TextBlock]
    role: str = "tool"
    is_error: bool = False
    approval: ApprovalResult | None = None
    execution: ExecutionResult | None = None


Message = UserMessage | AssistantMessage | ToolResultMessage