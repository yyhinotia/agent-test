"""项目级异常基类。

命名说明
--------
方案中称为 `baseException`，这里落地为 `AgentBaseError(Exception)`，而
不是直接继承内置的 `BaseException`：

- 内置 `BaseException` 是所有异常与系统信号（KeyboardInterrupt、
  SystemExit、GeneratorExit 等）的共同基类；
- 业务代码若直接继承/抛出 `BaseException`，会绕过 Python 的异常清理机制
  （finally / with / async 任务取消），并吞掉 Ctrl+C 等系统信号；
- 业务异常应继承 `Exception`（其最终基类才是 `BaseException`），既保证
  `except Exception` 能捕获，又不干扰系统级退出流程。

所有 agent-test 内已知业务异常统一继承 `AgentBaseError`，提供
`location`（执行位置）与 `detail`（附加上下文）两个结构化字段，
便于把“事实”写入 session、把完整堆栈写入日志文件。
"""
from __future__ import annotations

from typing import Any


class AgentBaseError(Exception):
    """agent-test 内所有已知业务异常的统一基类。"""

    def __init__(
        self,
        message: str,
        *,
        location: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.location = location
        self.detail = detail or {}

    def to_summary(self) -> dict[str, Any]:
        """返回可安全写入 session 的错误摘要（不含堆栈细节）。"""
        return {
            "location": self.location,
            "error_type": type(self).__name__,
            "message": str(self),
            "detail": dict(self.detail),
        }