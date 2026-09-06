"""会话相关异常。"""
from __future__ import annotations

from agent_test.exceptions.base import AgentBaseError


class SessionEditError(AgentBaseError):
    """对会话（Session）进行编辑/追加失败时抛出。

    例如：事件类型非法、事件数据无法序列化、持久化文件写入失败等。
    调用方应把完整堆栈写入日志文件，并把该异常的
    location / error_type / message 摘要写入 session 的 runtime/error 事件。
    """


class SessionContinuityError(AgentBaseError):
    """从持久化文件恢复会话时发现事件序号（seq）不连续。

    说明持久化文件可能被外部修改、截断或损坏，回放结果不可信。
    """