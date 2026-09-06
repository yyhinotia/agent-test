"""核心组件的本地冒烟测试（不依赖网络 / API Key）。"""
import asyncio

from agent_test.core.inbox import InBox
from agent_test.llm.adapter import LLMBaseAdapter
from agent_test.llm.registry import LLM_CLIENT
from agent_test.session.session import Session
from agent_test.tools import ToolCenter
from agent_test.types.messages import (
    AssistantMessage,
    Message,
    TextBlock,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
)
from agent_test.types.tools import ToolSchema
from agent_test.utils import get_now, get_uuid


def test_uuid_and_now():
    assert get_uuid()
    assert get_now()


def test_tool_call_block_args_dict():
    block = ToolCallBlock(
        id="call_1", name="read", args='{"file_path": "a.txt"}'
    )
    assert block.args_dict == {"file_path": "a.txt"}


def test_tool_schema_to_openai_schema():
    schema = ToolSchema(
        name="read",
        description="read file",
        parameters={"file_path": {"type": "string", "description": "path"}},
        required=["file_path"],
    )
    data = schema.to_openai_schema()
    assert data["type"] == "function"
    assert data["function"]["name"] == "read"
    assert data["function"]["parameters"]["properties"]["file_path"]["type"] == "string"
    assert data["function"]["parameters"]["required"] == ["file_path"]


def test_assemble_messages_roundtrip():
    base = LLMBaseAdapter()
    messages: List[Message] = [
        UserMessage(id="u1", content=[TextBlock(content="hello")]),
        AssistantMessage(
            id="a1",
            content=[ToolCallBlock(id="c1", name="read", args='{"file_path": "x"}')],
        ),
        ToolResultMessage(
            tool_call_id="c1",
            content=[TextBlock(content="file content")],
            is_error=False,
        ),
    ]
    out = base.assemble_messages(messages)
    assert out[0] == {"role": "system", "content": base.system_prompt}
    assert out[1] == {"role": "user", "content": "hello"}
    assert out[2]["role"] == "assistant"
    assert out[2]["tool_calls"][0]["function"]["name"] == "read"
    assert out[3]["role"] == "tool"
    assert out[3]["tool_call_id"] == "c1"
    assert out[3]["content"] == "file content"


def test_inbox_turn_and_step():
    box = InBox()
    msg = UserMessage(id="u1", content=[TextBlock(content="hi")])
    box.append("turn", msg)
    assert box.has_pending()
    claimed = box.claim("turn")
    assert claimed == [msg]
    assert not box.has_pending()


def test_session_derive_messages(tmp_path):
    session = Session(persist_dir=str(tmp_path))
    # __init__ 自动创建 {session_id}.jsonl 持久化文件
    assert session.file_path.exists()
    assert session.file_path.suffix == ".jsonl"
    assert session.session_id

    session.append("turn/start", data={"turn": 1})
    user_msg = UserMessage(id="u1", content=[TextBlock(content="hi")])
    session.append("user/message", data=user_msg)
    assert session.derive_messages() == [user_msg]

    # append 同步持久化：一行一条事件
    lines = [
        line
        for line in session.file_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == 2


def test_session_persist_roundtrip(tmp_path):
    """事件 JSONL 落盘后可 from_file 恢复，消息还原为 Message 模型。"""
    session = Session(session_id="s-001", persist_dir=str(tmp_path))
    user_msg = UserMessage(id="u1", content=[TextBlock(content="hi")])
    tool_result = ToolResultMessage(
        tool_call_id="c1", content=[TextBlock(content="ok")], is_error=False
    )
    assistant_msg = AssistantMessage(id="a1", content=[TextBlock(content="done")])
    session.append("user/message", data=user_msg)
    session.append("tool/result", data=tool_result)
    session.append("assistant/message", data=assistant_msg)

    assert (tmp_path / "s-001.jsonl").exists()

    restored = Session.from_file("s-001", persist_dir=str(tmp_path))
    assert restored.session_id == "s-001"
    assert restored.events == session.events
    assert restored.derive_messages() == session.derive_messages()
    assert restored.derive_messages() == [user_msg, tool_result, assistant_msg]


def test_session_seq_continues_after_from_file(tmp_path):
    """持久化游标：from_file 恢复后继续 append，seq 依然连续。"""
    session = Session(session_id="s-002", persist_dir=str(tmp_path))
    session.append("turn/start", data={"turn": 1})
    restored = Session.from_file("s-002", persist_dir=str(tmp_path))
    restored.append("turn/end", data={"turn": 1})
    seqs = [e.seq for e in restored.events]
    assert seqs == [0, 1]


def test_tool_center_register_and_execute():
    center = ToolCenter()

    @center.register(
        desc="加法",
        parameters={"a": {"type": "int"}, "b": {"type": "int"}},
        required=["a", "b"],
    )
    def add(a: int, b: int) -> int:
        return a + b

    schemas = center.get_schemas()
    assert schemas[0]["function"]["name"] == "add"
    result = asyncio.run(center.execute("add", {"a": 1, "b": 2}))
    assert result["content"] == "3"
    assert result["is_error"] is False


def test_tool_center_execute_error():
    center = ToolCenter()

    @center.register(desc="出错工具", parameters={})
    def boom():
        raise RuntimeError("boom")

    result = asyncio.run(center.execute("boom", {}))
    assert result["is_error"] is True
    assert "boom" in result["content"]


def test_agent_constructs_with_env(monkeypatch, tmp_path):
    LLM_CLIENT.clear()
    monkeypatch.setenv("API_KEY", "sk-test")
    monkeypatch.setenv("BASE_URI", "https://api.openai.com/v1")
    monkeypatch.setenv("MODEL_NAME", "gpt-4o-mini")
    from agent_test import ReactAgent

    agent = ReactAgent(persist_dir=str(tmp_path))
    assert agent.phase.turn == 0
    assert agent.llm_client.model_name == "gpt-4o-mini"
    # Agent 持有私有 inbox 与 session（自动创建 jsonl 文件）
    assert agent.inbox is not None
    assert agent.session.session_id
    assert agent.session.file_path.parent == tmp_path
    assert (tmp_path / f"{agent.session.session_id}.jsonl").exists()


def test_react_agent_loop_with_fake_llm(monkeypatch, tmp_path):
    """用假 LLM 驱动完整 ReAct 循环：工具调用 -> 工具结果 -> 最终回答。"""
    from agent_test import ReactAgent
    from agent_test.tools import tool_center as shared_tool_center

    LLM_CLIENT.clear()
    monkeypatch.setenv("API_KEY", "sk-test")
    monkeypatch.setenv("BASE_URI", "https://api.openai.com/v1")
    monkeypatch.setenv("MODEL_NAME", "gpt-4o-mini")

    calls = {"n": 0, "values": None}
    shared_tool_center.unregister("add_calc")  # 避免跨用例重复注册

    @shared_tool_center.register(
        desc="测试加法",
        parameters={"a": {"type": "int"}, "b": {"type": "int"}},
        required=["a", "b"],
    )
    def add_calc(a: int, b: int) -> int:
        calls["values"] = (a, b)
        return a + b

    class FakeLLM:
        async def stream(self, messages, tools):
            calls["n"] += 1
            if calls["n"] == 1:
                # 第一轮: 请求调用工具
                return (
                    AssistantMessage(
                        id="a1",
                        content=[
                            ToolCallBlock(
                                id="c1", name="add_calc", args='{"a": 1, "b": 2}'
                            )
                        ],
                    ),
                    "",
                )
            # 第二轮: 给出最终回答
            return (
                AssistantMessage(id="a2", content=[TextBlock(content="结果是 3")]),
                "finish",
            )

    agent = ReactAgent(persist_dir=str(tmp_path))
    agent.llm_client = FakeLLM()  # 替换真实 OpenAI 客户端
    agent.inbox.append(
        "turn", UserMessage(id="u1", content=[TextBlock(content="1+2=?")])
    )

    ok = asyncio.run(agent.turn())

    assert ok is True
    assert calls["n"] == 2  # 第一轮工具调用，第二轮最终回答
    assert calls["values"] == (1, 2)
    # 私有 session 中包含工具调用助手消息与工具结果消息
    assert any(isinstance(e.data, ToolResultMessage) for e in agent.session.events)
    tool_call_messages = [
        e.data
        for e in agent.session.events
        if isinstance(e.data, AssistantMessage)
        and any(isinstance(b, ToolCallBlock) for b in e.data.content)
    ]
    assert len(tool_call_messages) >= 1
    # 全部事件已持久化到 JSONL 文件
    lines = [
        line
        for line in agent.session.file_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == len(agent.session.events)