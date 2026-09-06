"""异常体系的层级、结构化字段与摘要测试。"""
from pathlib import Path

import pytest

from agent_test.exceptions import (
    AgentBaseError,
    LlmError,
    MessageEditError,
    SessionContinuityError,
    SessionEditError,
    ToolExecutionError,
)
from agent_test.log.runtime_log import RuntimeLog
from agent_test.session.session import Session
from agent_test.types.messages import ToolCallBlock


def test_base_error_derives_from_exception_not_baseexception():
    # 关键约束：业务基类必须继承 Exception，不能直接继承内置 BaseException，
    # 否则会吞掉 KeyboardInterrupt / SystemExit 等系统信号
    assert AgentBaseError.__mro__[1] is Exception
    assert issubclass(AgentBaseError, BaseException)


def test_all_business_errors_share_base():
    for exc_type in (
        SessionEditError,
        MessageEditError,
        LlmError,
        ToolExecutionError,
        SessionContinuityError,
    ):
        assert issubclass(exc_type, AgentBaseError)


def test_error_summary_fields():
    exc = SessionEditError(
        "事件类型非法", location="Session.append", detail={"seq": 1}
    )
    summary = exc.to_summary()
    assert summary["location"] == "Session.append"
    assert summary["error_type"] == "SessionEditError"
    assert summary["message"] == "事件类型非法"
    assert summary["detail"] == {"seq": 1}


def test_llm_error_retryable_flag():
    transient = LlmError(
        "timeout", location="OPENAIAdapter.stream", retryable=True
    )
    permanent = LlmError("bad key", location="OPENAIAdapter.__init__")
    assert transient.retryable is True
    assert permanent.retryable is False


def test_session_edit_error_raised_on_invalid_event_type(tmp_path):
    session = Session(session_id="s-invalid", persist_dir=str(tmp_path))
    with pytest.raises(SessionEditError):
        session.append("not/an/event")


def test_session_continuity_error_strict_mode(tmp_path):
    # 手动制造 seq 断层文件，strict=True 应抛 SessionContinuityError
    (tmp_path / "s-gap.jsonl").write_text(
        '{"seq": 0, "type": "turn/start", "data": {"turn": 1}, "time": "t0"}\n'
        '{"seq": 5, "type": "turn/end", "data": null, "time": "t1"}\n',
        encoding="utf-8",
    )
    with pytest.raises(SessionContinuityError):
        Session.from_file("s-gap", persist_dir=str(tmp_path), strict=True)


def test_tool_call_args_invalid_json_raises_message_edit_error():
    """工具调用参数 JSON 无法解析 -> MessageEditError（含 location 与原因链）。"""
    block = ToolCallBlock(id="call_1", name="read", args="{not-json")
    with pytest.raises(MessageEditError) as exc_info:
        block.args_dict
    error = exc_info.value
    assert error.location == "ToolCallBlock.args_dict"
    assert isinstance(error.__cause__, ValueError)  # 保留原始解析异常链


def test_session_append_invalid_type_logs_original_error(tmp_path):
    """Session.append 拒绝非法事件类型：日志记录原始 ValueError 完整堆栈。"""
    log_file = RuntimeLog.configure(str(tmp_path / "logs"))
    session = Session(session_id="s-log", persist_dir=str(tmp_path))
    with pytest.raises(SessionEditError):
        session.append("not/an/event")
    text = Path(log_file).read_text(encoding="utf-8")
    assert "ValueError" in text
    assert "not/an/event" in text


def test_session_from_file_corrupt_line_raises_session_edit_error(tmp_path):
    """会话文件某行 JSON 损坏 -> SessionEditError（含行号），完整堆栈进日志。"""
    log_file = RuntimeLog.configure(str(tmp_path / "logs"))
    (tmp_path / "s-bad.jsonl").write_text(
        '{"seq": 0, "type": "turn/start", "data": null, "time": "t0"}\n'
        "not-json-line\n",
        encoding="utf-8",
    )
    with pytest.raises(SessionEditError) as exc_info:
        Session.from_file("s-bad", persist_dir=str(tmp_path))
    error = exc_info.value
    assert error.location == "Session.from_file"
    assert error.detail["line_no"] == 2
    text = Path(log_file).read_text(encoding="utf-8")
    assert "Traceback (most recent call last)" in text
    assert "JSONDecodeError" in text
