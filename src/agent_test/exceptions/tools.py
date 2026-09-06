"""工具相关异常。"""
from __future__ import annotations

from agent_test.exceptions.base import AgentBaseError


class ToolExecutionError(AgentBaseError):
    """工具未注册、不可用或执行失败时抛出。

    注意：ToolCenter.execute 会把该异常转换为
    {"content": str, "is_error": True} 返回给 Agent（符合 LLM 工具结果
    协议），同时把完整堆栈写入日志文件。
    """