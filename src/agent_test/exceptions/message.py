"""消息相关异常。"""
from __future__ import annotations

from agent_test.exceptions.base import AgentBaseError


class MessageEditError(AgentBaseError):
    """构造/编辑会话消息（Message）失败时抛出。

    例如：事件类型与消息角色不匹配、内容块类型非法、
    工具调用参数 JSON 无法解析等。
    """