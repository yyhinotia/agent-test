"""LLM 调用相关异常。"""
from __future__ import annotations

from agent_test.exceptions.base import AgentBaseError


class LlmError(AgentBaseError):
    """LLM 调用失败（网络、鉴权、协议或模型返回异常）。

    retryable 标记该错误是否值得重试：
    - True  ：网络抖动、服务端 5xx、超时等瞬时问题，可退避重试；
    - False ：鉴权失败、参数错误等确定性问题，重试无意义。
    """

    def __init__(
        self,
        message: str,
        *,
        location: str | None = None,
        detail: dict | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message, location=location, detail=detail)
        self.retryable = retryable