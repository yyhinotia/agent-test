"""纯内存 Session 与 Agent 生命周期（subagent / DAG 节点的基础）。"""
import asyncio

from agent_test import create_agent
from agent_test.session.session import Session
from agent_test.tools.center import ToolCenter
from agent_test.types.messages import AssistantMessage, TextBlock, UserMessage


class _StubLLM:
    def __init__(self):
        self.session = None

    async def stream(self, messages, tools):
        return (
            AssistantMessage(id="a", content=[TextBlock(content="ok")]),
            "finish",
            {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )


def _center() -> ToolCenter:
    center = ToolCenter()

    def read_file(file_path: str) -> str:
        return "body"

    center.register(
        desc="read",
        parameters={"file_path": {"type": "string"}},
        required=["file_path"],
        name="read",
    )(read_file)
    return center


def test_session_pure_memory_has_no_disk_state(tmp_path):
    session = Session("mem", persist_dir=str(tmp_path), persist=False)

    assert session.persist is False
    assert session.file_path is None
    assert session.persist_dir is None
    assert session.session_dir is None
    assert list(tmp_path.iterdir()) == []

    msg = UserMessage(id="u1", content=[TextBlock(content="hi")])
    event = session.append("user/message", data=msg)
    assert event.seq == 0
    assert session.derive_messages() == [msg]
    assert session.max_turn_step() == (0, 0)

    session.reload()
    assert session.derive_messages() == [msg]
    assert list(tmp_path.iterdir()) == []

    session.destroy()
    assert session.events == []
    assert session._archived == []
    assert session._seq == 0


def test_create_agent_persist_false_skips_disk_and_conflict(tmp_path):
    # 先放一个同名且非空的会话文件：persist=False 应跳过冲突检测
    existing = tmp_path / "same-id.jsonl"
    existing.write_text('{"seq":0}\n', encoding="utf-8")

    agent = create_agent(
        "same-id",
        str(tmp_path),
        llm_client=_StubLLM(),
        tools=_center(),
        persist=False,
    )

    assert agent.persist is False
    assert agent.session.persist is False
    assert agent.session.file_path is None
    assert existing.read_text(encoding="utf-8") == '{"seq":0}\n'

    agent.inbox.append(
        "turn", UserMessage(id="u1", content=[TextBlock(content="hi")])
    )
    assert asyncio.run(agent.turn()) is True
    # 纯内存 Agent 不追加任何会话文件
    assert [p.name for p in tmp_path.iterdir()] == ["same-id.jsonl"]
    agent.destroy()
    assert agent.destroyed is True


def test_from_agent_excludes_tools_and_uses_pure_memory(tmp_path):
    from agent_test.human.service import AutoApproveService
    from agent_test.tools.subagent import register_subagent

    center = _center()
    parent = create_agent(
        "parent",
        str(tmp_path),
        llm_client=_StubLLM(),
        tools=center,
    )
    register_subagent(center, parent)
    parent_llm = parent.llm_client

    child = parent.from_agent(
        parent,
        session_id="child",
        exclude_tools=("subagent", "span", "edit"),
        persist=False,
        auto_approve=True,
    )

    assert child.persist is False
    assert child.session.file_path is None
    assert "read" in child.tool_center.tools
    assert "subagent" not in child.tool_center.tools
    assert "span" not in child.tool_center.tools
    assert "edit" not in child.tool_center.tools
    assert isinstance(child.tool_center.approver, AutoApproveService)
    # LLM 客户端是浅拷贝：子 Agent 构造不会覆盖父 Agent 的 session 绑定
    assert child.llm_client is not parent_llm
    assert parent_llm.session is parent.session
    assert child.llm_client.session is child.session

    child.destroy()
    assert child.destroyed is True
    assert child.session is None
    parent.destroy()
