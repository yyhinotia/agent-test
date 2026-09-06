"""运行时日志模块。

对外主要使用 `RuntimeLog`：
- 配置：RuntimeLog.configure(log_dir)          # 默认 logs/，懒加载也可
- 上下文：RuntimeLog.bind(session_id=..., turn=..., step=...) / unbind(tokens)
- 异常：RuntimeLog.capture_exception(exc, location=...) -> 错误摘要
"""
from agent_test.log.runtime_log import RuntimeLog

__all__ = ["RuntimeLog"]