"""subagent 工具：纯内存子 Agent、工具排除、弱引用与销毁语义。"""
import asyncio
import gc
import weakref

from agent_test import create_agent
from agent_test.human.service import AutoApproveService
from agent_test.tools.center import ToolCenter
from agent_test.tools.subagent import register_span, register_subagent
from agent_test.types.messages import AssistantMessage, TextBlock


class _FakeLLM:
    """最小 LLM 桩：记录每次看到的工具 schema，返回固定文本。"""

    def __init__(self, text: str = "子任务完成：42"):
        self.text = text
        self.seen_tools: list[list[dict]] = []

    async def stream(self, messages, tools):
        self.seen_tools.append(tools or [])
        return (
            AssistantMessage(id="a1", content=[TextBlock(content=self.text)]),
            "finish",
            None,
        )


def _center_with_read() -> ToolCenter:
    center = ToolCenter()

    def read_file(file_path: str) -> str:
        return "file body"

    center.register(
        desc="读取文件",
        parameters={"file_path": {"type": "string"}},
        required=["file_path"],
        name="read",
    )(read_file)
    return center


def _tool_names(schemas: list[dict]) -> set[str]:
    return {
        item.get("function", {}).get("name", "")
        for item in schemas
    }


def _make_parent(tmp_path, center=None, text: str = "子任务完成：42"):
    center = center or _center_with_read()
    llm = _FakeLLM(text)
    agent = create_agent(
        "parent-subagent",
        str(tmp_path),
        llm_client=llm,
        tools=center,
    )
    return agent, center, llm


def test_subagent_runs_child_in_memory_and_excludes_recursive_tools(tmp_path):
    parent, center, llm = _make_parent(tmp_path)
    register_subagent(center, parent)

    result = asyncio.run(
        center.execute(
            "subagent",
            {"task_description": "计算 6*7", "context": "父任务上下文"},
        )
    )

    assert result["is_error"] is False
    assert result["content"] == "子任务完成：42"
    # 子 Agent 的工具中心排除了 subagent / span / edit
    child_tool_names = _tool_names(llm.seen_tools[-1])
    assert "read" in child_tool_names
    assert "subagent" not in child_tool_names
    assert "span" not in child_tool_names
    assert "edit" not in child_tool_names
    # 只有父 Agent 的持久化会话，没有子 Agent 的会话文件
    session_files = list(tmp_path.glob("*.jsonl"))
    assert [p.name for p in session_files] == ["parent-subagent.jsonl"]
    parent.destroy()


def test_subagent_after_parent_destroy_returns_safe_error(tmp_path):
    parent, center, _ = _make_parent(tmp_path)
    register_subagent(center, parent)
    parent.destroy()

    result = asyncio.run(
        center.execute("subagent", {"task_description": "还能执行吗"})
    )

    assert "父 Agent 已销毁" in result["content"]


def test_subagent_weakref_does_not_keep_parent_alive(tmp_path):
    parent, center, _ = _make_parent(tmp_path)
    ref = weakref.ref(parent)
    register_subagent(center, parent)
    parent.destroy()
    del parent
    gc.collect()

    assert ref() is None


def test_register_span_alias_uses_span_tool_name(tmp_path):
    parent, center, _ = _make_parent(tmp_path)
    register_span(center, parent)

    assert "span" in center.tools
    result = asyncio.run(
        center.execute("span", {"task_description": "别名调用"})
    )
    assert result["content"] == "子任务完成：42"
    parent.destroy()


def test_auto_approve_service_only_approves_confirm():
    service = AutoApproveService()

    approved, message = asyncio.run(
        service.confirm(action="执行命令: rm -rf /tmp/x")
    )
    assert approved is True
    assert "auto-approved" in message
    answer = asyncio.run(service.ask_user(question="选哪个？"))
    assert "非交互" in answer
