"""session 错误事件测试：事实进 session，堆栈进日志。"""
import asyncio
from pathlib import Path

from agent_test import ReactAgent
from agent_test.log.runtime_log import RuntimeLog
from agent_test.session.session import Session
from agent_test.types.messages import (
    AssistantMessage,
    TextBlock,
    ToolCallBlock,
    UserMessage,
)


def test_append_error_records_error_event(tmp_path):
    session = Session(session_id="s-err", persist_dir=str(tmp_path))
    event = session.append_error(
        location="ReactAgent._step",
        error_type="LlmError",
        message="OpenAI 流式调用失败: timeout",
        detail={"turn": 1, "step": 1},
    )
    assert event.type == "runtime/error"
    assert event.data["location"] == "ReactAgent._step"
    assert event.data["error_type"] == "LlmError"
    assert event.data["message"] == "OpenAI 流式调用失败: timeout"
    assert "traceback" not in event.data  # 堆栈只进日志文件


def test_error_event_roundtrip_via_from_file(tmp_path):
    session = Session(session_id="s-err2", persist_dir=str(tmp_path))
    session.append_error(location="LLM", error_type="LlmError", message="m")
    restored = Session.from_file("s-err2", persist_dir=str(tmp_path))
    assert restored.events == session.events
    assert restored.events[0].type == "runtime/error"
    # 错误事件不参与 LLM 历史
    assert restored.derive_messages() == []


def test_agent_error_flow_logs_stack_and_records_session(tmp_path):
    """端到端：FakeLLM 抛错 -> 堆栈进日志文件，事实进 session 事件。"""
    log_file = RuntimeLog.configure(str(tmp_path / "logs"))

    class RaisingLLM:
        async def stream(self, messages, tools):
            raise RuntimeError("llm-crash")

    agent = ReactAgent(persist_dir=str(tmp_path / "sessions"))
    agent.llm_client = RaisingLLM()
    agent.inbox.append(
        "turn", UserMessage(id="u1", content=[TextBlock(content="hi")])
    )

    ok = asyncio.run(agent.turn())
    assert ok is True  # 本轮已执行，错误通过事件/日志暴露

    error_events = [e for e in agent.session.events if e.type == "runtime/error"]
    assert len(error_events) == 1
    assert error_events[0].data["error_type"] == "RuntimeError"
    assert "llm-crash" in error_events[0].data["message"]
    assert error_events[0].data["location"].startswith("ReactAgent._step")

    # 完整堆栈已写入日志文件，且能通过 session_id 关联到会话
    text = Path(log_file).read_text(encoding="utf-8")
    assert "Traceback (most recent call last)" in text
    assert "RuntimeError: llm-crash" in text
    assert agent.session.session_id in text

    # 会话 JSONL 中同样可回放该错误事件
    restored = Session.from_file(
        agent.session.session_id, persist_dir=str(tmp_path / "sessions")
    )
    assert any(e.type == "runtime/error" for e in restored.events)


def test_agent_records_message_edit_error_event(tmp_path):
    """端到端：工具调用参数 JSON 损坏 -> MessageEditError 事实进 session。"""
    log_file = RuntimeLog.configure(str(tmp_path / "logs"))

    class BadArgsLLM:
        async def stream(self, messages, tools):
            return (
                AssistantMessage(
                    id="a1",
                    content=[ToolCallBlock(id="c1", name="read", args="{bad-json")],
                ),
                "",
                None,
            )

    agent = ReactAgent(persist_dir=str(tmp_path / "sessions"))
    agent.llm_client = BadArgsLLM()
    agent.inbox.append(
        "turn", UserMessage(id="u1", content=[TextBlock(content="hi")])
    )

    ok = asyncio.run(agent.turn())
    assert ok is True

    error_events = [e for e in agent.session.events if e.type == "runtime/error"]
    assert len(error_events) == 1
    assert error_events[0].data["error_type"] == "MessageEditError"
    assert error_events[0].data["location"].startswith("ReactAgent._step")

    # 完整堆栈与异常类型进入日志文件
    text = Path(log_file).read_text(encoding="utf-8")
    assert "Traceback (most recent call last)" in text
    assert "MessageEditError" in text
