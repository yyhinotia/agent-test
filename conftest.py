"""pytest 根配置：测试间隔离运行时日志。"""
import pytest

from agent_test.log.runtime_log import RuntimeLog


@pytest.fixture(autouse=True)
def _reset_runtime_log():
    """每个测试结束后清空日志 handler，避免跨测试串扰目录/上下文。"""
    yield
    RuntimeLog.reset()