"""agent-runtime 运行时日志模块。

背景
----
旧实现中 agent-runtime 的错误要么被吞掉（llm_adapter 里
`except Exception: end_reason = "error"`），要么只存在于内存/会话文件，
缺少可检索的完整堆栈。重构后错误信息分层落两处：

* 日志文件（logs/runtime.log）：完整细节 —— 异常类型、消息、完整
  traceback，以及 session_id / turn / step 上下文，供事后排查；
* 会话文件（sessions/{session_id}.jsonl）：仅错误“事实摘要” —— 执行
  位置 + 报错类型 + 消息（见 Session.append_error 写入的 runtime/error
  事件），便于按时间线回放。

用法
----
    from agent_test.log.runtime_log import RuntimeLog

    RuntimeLog.configure("logs")                        # 显式配置（也支持懒加载）
    tokens = RuntimeLog.bind(session_id="s1", turn=1)   # 绑定上下文（async 任务自动传播）
    try:
        do_something()
    except Exception as exc:
        summary = RuntimeLog.capture_exception(exc, location="do_something")
        session.append_error(
            location=summary["location"],
            error_type=summary["error_type"],
            message=summary["message"],
        )
    finally:
        RuntimeLog.unbind(tokens)
"""
from __future__ import annotations

import logging
import sys
import threading
import traceback
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any

LOGGER_NAME = "agent_test.runtime"

# 上下文变量：在 async 任务内自动传播，无需手动传递
SESSION_ID: ContextVar[str | None] = ContextVar("session_id", default=None)
TURN: ContextVar[int | None] = ContextVar("turn", default=None)
STEP: ContextVar[int | None] = ContextVar("step", default=None)

_FORMAT = (
    "%(asctime)s | %(levelname)-8s | session=%(session_id)s "
    "turn=%(turn)s step=%(step)s | %(name)s | %(message)s"
)


class _ContextFilter(logging.Filter):
    """为每条日志记录附加 session/turn/step 上下文字段。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.session_id = SESSION_ID.get() or "-"
        turn = TURN.get()
        step = STEP.get()
        record.turn = turn if turn is not None else "-"
        record.step = step if step is not None else "-"
        return True


class RuntimeLog:
    """运行时日志门面：懒配置 + 上下文绑定 + 异常捕获。

    说明：本类刻意保持“单一责任” —— 只负责把 agent-runtime 的所有
    日志（含完整异常堆栈）写入日志文件（同时输出到 stderr），
    不负责"写 session"；错误事实由调用方通过 Session.append_error 落盘。
    """

    _lock = threading.Lock()
    _configured = False
    _log_dir: Path | None = None
    _file_path: Path | None = None

    # ---------- 配置 ----------

    @classmethod
    def configure(
        cls,
        log_dir: str | Path = "logs",
        *,
        level: int = logging.INFO,
        console: bool = True,
    ) -> Path:
        """配置运行时日志，返回日志文件路径。

        重复调用会清空旧 handler 并切换目录（幂等，测试可随时重定向）。
        """
        log_dir = Path(log_dir)
        with cls._lock:
            root = logging.getLogger(LOGGER_NAME)
            root.setLevel(level)
            for handler in list(root.handlers):
                root.removeHandler(handler)
                handler.close()
            log_dir.mkdir(parents=True, exist_ok=True)
            file_path = log_dir / "runtime.log"
            file_handler = logging.FileHandler(file_path, encoding="utf-8")
            file_handler.setFormatter(logging.Formatter(_FORMAT))
            file_handler.addFilter(_ContextFilter())
            root.addHandler(file_handler)
            if console:
                stream_handler = logging.StreamHandler(sys.stderr)
                stream_handler.setFormatter(logging.Formatter(_FORMAT))
                stream_handler.addFilter(_ContextFilter())
                root.addHandler(stream_handler)
            root.propagate = False
            cls._configured = True
            cls._log_dir = log_dir
            cls._file_path = file_path
            return file_path

    @classmethod
    def reset(cls) -> None:
        """清空 handler 与配置状态（测试隔离用）。"""
        with cls._lock:
            root = logging.getLogger(LOGGER_NAME)
            for handler in list(root.handlers):
                root.removeHandler(handler)
                handler.close()
            cls._configured = False
            cls._log_dir = None
            cls._file_path = None

    @classmethod
    def _logger(cls) -> logging.Logger:
        if not cls._configured:
            cls.configure()
        return logging.getLogger(LOGGER_NAME)

    @classmethod
    def log_file_path(cls) -> Path | None:
        """当前日志文件路径（未配置时为 None）。"""
        return cls._file_path

    # ---------- 上下文绑定 ----------

    @staticmethod
    def bind(
        session_id: str | None = None,
        turn: int | None = None,
        step: int | None = None,
    ) -> list[tuple[ContextVar, Token]]:
        """绑定运行时上下文，返回用于 unbind 的 token 列表。"""
        tokens: list[tuple[ContextVar, Token]] = []
        for var, value in ((SESSION_ID, session_id), (TURN, turn), (STEP, step)):
            if value is not None:
                tokens.append((var, var.set(value)))
        return tokens

    @staticmethod
    def unbind(tokens: list[tuple[ContextVar, Token]]) -> None:
        """恢复 bind 之前的上下文。"""
        for var, token in tokens:
            var.reset(token)

    # ---------- 日志方法 ----------

    @classmethod
    def debug(cls, message: str, *args: Any) -> None:
        cls._logger().debug(message, *args)

    @classmethod
    def info(cls, message: str, *args: Any) -> None:
        cls._logger().info(message, *args)

    @classmethod
    def warning(cls, message: str, *args: Any) -> None:
        cls._logger().warning(message, *args)

    @classmethod
    def error(cls, message: str, *args: Any, exc_info: Any = None) -> None:
        cls._logger().error(message, *args, exc_info=exc_info)

    @classmethod
    def exception(cls, message: str, *args: Any) -> None:
        """必须在 except 块内调用：记录消息 + 当前异常完整堆栈。"""
        cls._logger().exception(message, *args)

    # ---------- 异常捕获 ----------

    @classmethod
    def capture_exception(
        cls,
        exc: BaseException | None = None,
        *,
        location: str | None = None,
        detail: dict[str, Any] | None = None,
        level: int = logging.ERROR,
    ) -> dict[str, Any]:
        """把异常完整堆栈写入日志，返回可写入 session 的错误摘要。

        返回摘要字段（错误事实）：
            location    执行位置（如 “ReactAgent._step”）
            error_type  异常类型名（如 “LlmError”）
            message     异常消息
            detail      附加上下文（turn/step/工具名等）

        完整 traceback 只进日志文件，不进 session：
        由调用方把摘要通过 Session.append_error 写入 runtime/error 事件。
        """
        if exc is None:
            exc = sys.exc_info()[1]
        if exc is None:
            exc = RuntimeError("capture_exception() 在非异常上下文调用且未传 exc")
        location = location or f"{type(exc).__module__}.{type(exc).__qualname__}"
        summary = {
            "location": location,
            "error_type": type(exc).__name__,
            "message": str(exc),
            "detail": detail or {},
        }
        cls._logger().log(
            level,
            "runtime error | location=%s | type=%s | message=%s | detail=%s",
            location,
            summary["error_type"],
            summary["message"],
            summary["detail"],
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return summary