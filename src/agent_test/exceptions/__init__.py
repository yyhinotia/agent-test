"""异常体系：统一基类 + 分类异常。

错误信息分层落两处：
- session（sessions/*.jsonl）：只记录“错误事实” —— 执行位置 + 报错类型 +
  消息摘要（runtime/error 事件，见 Session.append_error）；
- 日志文件（logs/runtime.log）：完整堆栈（见 RuntimeLog.capture_exception）。
二者通过 session_id 关联。
"""
from agent_test.exceptions.base import AgentBaseError
from agent_test.exceptions.llm import LlmError
from agent_test.exceptions.message import MessageEditError
from agent_test.exceptions.session import SessionContinuityError, SessionEditError
from agent_test.exceptions.tools import ToolExecutionError

__all__ = [
    "AgentBaseError",
    "LlmError",
    "MessageEditError",
    "SessionContinuityError",
    "SessionEditError",
    "ToolExecutionError",
]