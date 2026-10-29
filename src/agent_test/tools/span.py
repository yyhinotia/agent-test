"""span 工具兼容入口。

`span` 是 `agent_test.tools.subagent` 的别名，用于兼容旧调用方。
"""
from agent_test.tools.subagent import (
    AutoApproveService,
    _AutoApprover,
    register_span,
    register_subagent,
)

__all__ = [
    "AutoApproveService",
    "_AutoApprover",
    "register_span",
    "register_subagent",
]
