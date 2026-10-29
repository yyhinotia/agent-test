"""pytest 根配置：测试间隔离运行时日志 + 代理环境兼容。"""
import os

import pytest

from agent_test.log.runtime_log import RuntimeLog


@pytest.fixture(autouse=True)
def _sanitize_no_proxy():
    """清理 NO_PROXY 中 httpx 无法解析的 `[::1]` 条目。

    Windows 开发机上 NO_PROXY 常包含 `::1,[::1]`；httpx 把 `[::1]` 当作
    带端口 URL 解析时会抛 `Invalid port: ':1]'`，导致所有构造 OpenAI 客户端
    的测试失败。本 fixture 只移除该括号形式，保留 127.0.0.1 / ::1 等有效项。
    """
    value = os.environ.get("NO_PROXY") or os.environ.get("no_proxy")
    if value and "[::1]" in value:
        cleaned = ",".join(
            part for part in value.split(",") if part.strip() != "[::1]"
        )
        os.environ["NO_PROXY"] = cleaned
        os.environ["no_proxy"] = cleaned
    yield


@pytest.fixture(autouse=True)
def _isolate_runtime_log(tmp_path_factory):
    """每个测试把 RuntimeLog 重定向到独立临时目录，避免污染仓库 logs/runtime.log。

    RuntimeLog 未配置时会懒加载到默认 `logs/`；测试里的 Scheduler/Agent
    会产生大量 node start/end 日志，不隔离会让根日志混入测试噪声。
    使用 tmp_path_factory 而不是测试自身的 tmp_path，避免给「纯内存不落盘」
    这类断言引入额外的 logs/ 目录。
    """
    log_dir = tmp_path_factory.mktemp("runtime-logs")
    RuntimeLog.configure(log_dir, console=False)
    yield
    RuntimeLog.reset()