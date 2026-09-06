"""日志模块测试：文件输出、上下文绑定与异常堆栈记录。"""
import logging
from pathlib import Path

from agent_test.log.runtime_log import RuntimeLog


def test_configure_creates_log_file(tmp_path):
    log_file = RuntimeLog.configure(str(tmp_path / "logs"))
    assert Path(log_file).exists()
    assert RuntimeLog.log_file_path() == tmp_path / "logs" / "runtime.log"


def test_context_bind_appears_in_log_line(tmp_path):
    log_file = RuntimeLog.configure(str(tmp_path / "logs"))
    tokens = RuntimeLog.bind(session_id="s-ctx", turn=3, step=2)
    try:
        RuntimeLog.info("context check")
    finally:
        RuntimeLog.unbind(tokens)
    text = Path(log_file).read_text(encoding="utf-8")
    assert "s-ctx" in text
    assert "turn=3 step=2" in text


def test_capture_exception_writes_full_traceback(tmp_path):
    log_file = RuntimeLog.configure(str(tmp_path / "logs"))
    try:
        raise ValueError("boom-value")
    except ValueError as exc:
        summary = RuntimeLog.capture_exception(
            exc, location="test_runtime_log.boom"
        )

    text = Path(log_file).read_text(encoding="utf-8")
    # 摘要字段
    assert summary["location"] == "test_runtime_log.boom"
    assert summary["error_type"] == "ValueError"
    assert summary["message"] == "boom-value"
    # 完整堆栈进入日志文件
    assert "Traceback (most recent call last)" in text
    assert "ValueError: boom-value" in text
    assert "test_runtime_log.py" in text


def test_error_log_level_respected(tmp_path):
    log_file = RuntimeLog.configure(str(tmp_path / "logs"), level=logging.WARNING)
    RuntimeLog.info("should-not-appear")
    RuntimeLog.warning("should-appear")
    text = Path(log_file).read_text(encoding="utf-8")
    assert "should-not-appear" not in text
    assert "should-appear" in text