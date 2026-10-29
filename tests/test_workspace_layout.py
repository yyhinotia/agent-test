"""`.agent-test` 工作区布局：普通 session、task session、graph checkpoint。"""
import asyncio
import json
from pathlib import Path

import pytest

from agent_test import Session, create_agent
from agent_test.app.application import Application, parse_args
from agent_test.app.scheduler import Scheduler
from agent_test.llm.registry import LLM_CLIENT
from agent_test.log.runtime_log import RuntimeLog
from agent_test.tools.builtin import tool_center as builtin_tool_center
from agent_test.tools.center import ToolCenter
from agent_test.types.messages import (
    AssistantMessage,
    TextBlock,
    UserMessage,
)


class _FakeLLM:
    def __init__(self):
        self.session = None

    async def stream(self, messages, tools):
        return (
            AssistantMessage(
                id="a",
                content=[
                    TextBlock(
                        content=(
                            '{"status":"success","output":{"ok":true},'
                            '"evidence":["ok"],"self_check":true}'
                        )
                    )
                ],
            ),
            "finish",
            None,
        )


def test_session_custom_session_file_and_resume(tmp_path):
    sessions_root = tmp_path / ".agent-test" / "sessions"
    session_file = sessions_root / "s-1" / "session.jsonl"

    session = Session(
        "s-1",
        session_file=str(session_file),
    )
    user = UserMessage(id="u1", content=[TextBlock(content="hi")])
    session.append("user/message", data=user)

    assert session.file_path == session_file
    assert session_file.exists()
    assert session.session_dir == session_file.parent

    restored = Session.resume("s-1", session_file=str(session_file))
    assert restored.derive_messages() == [user]
    assert restored.file_path == session_file


def test_create_agent_custom_session_file_conflict(tmp_path):
    from agent_test.exceptions.session import SessionEditError

    session_file = tmp_path / ".agent-test" / "sessions" / "s-1" / "session.jsonl"
    session_file.parent.mkdir(parents=True)
    session_file.write_text('{"seq":0}\n', encoding="utf-8")

    with pytest.raises(SessionEditError):
        create_agent(
            "s-1",
            llm_client=_FakeLLM(),
            tools=ToolCenter(),
            session_file=str(session_file),
        )

    # persist=False 时跳过磁盘冲突检测
    agent = create_agent(
        "s-1",
        llm_client=_FakeLLM(),
        tools=ToolCenter(),
        session_file=str(session_file),
        persist=False,
    )
    assert agent.session.file_path is None
    agent.destroy()


def test_scheduler_graph_session_dir_and_per_graph_checkpoint(tmp_path):
    sessions_root = tmp_path / ".agent-test" / "sessions"
    checkpoint_dir = tmp_path / ".agent-test" / "checkpoints"

    async def run():
        scheduler = Scheduler(
            checkpoint_file=None,
            checkpoint_dir=str(checkpoint_dir),
            sessions_dir=str(sessions_root),
            persist_sessions=True,
            max_attempt=1,
            tool_center=ToolCenter(),
            planner=lambda goal, ctx: {
                "graph_id": "g-layout",
                "global_goal": goal,
                "nodes": [{"node_id": "a", "goal": "A"}],
            },
            llm_client=_FakeLLM(),
        )
        return scheduler, await scheduler.plan_and_execute("goal")

    scheduler, state = asyncio.run(run())

    assert state.global_status.value == "success"
    # task 模式：graph_id/sessionxxx.jsonl
    assert (
        sessions_root / "g-layout" / "g-layout_a_att0.jsonl"
    ).exists()
    # checkpoint：{graph_id}.jsonl
    assert (checkpoint_dir / "g-layout.jsonl").exists()


def test_application_default_workspace_and_session_layout(
    tmp_path, monkeypatch
):
    LLM_CLIENT.clear()
    monkeypatch.setenv("API_KEY", "sk-test")
    monkeypatch.setenv("BASE_URI", "https://api.openai.com/v1")
    monkeypatch.setenv("MODEL_NAME", "gpt-4o-mini")
    RuntimeLog.reset()

    workspace = tmp_path / ".agent-test"
    app = Application(workspace_dir=str(workspace))
    agent = app._make_agent()
    try:
        assert (
            agent.session.file_path
            == workspace
            / "sessions"
            / agent.session.session_id
            / "session.jsonl"
        )
        assert agent.session.file_path.exists()
        assert agent.session.file_path.parent.name == agent.session.session_id
        assert agent.session.file_path.name == "session.jsonl"
    finally:
        agent.destroy()


def test_builtin_has_edit_tool_and_scope_can_exclude_it():
    """edit 工具本身存在；日志里的“未注册”是 tool_scope 排除导致。"""
    assert "edit" in builtin_tool_center.tools
    assert builtin_tool_center.tools["edit"].usable is True

    scoped = builtin_tool_center.clone(
        include={"read", "write", "bash", "grep"}
    )
    assert "edit" not in scoped.tools


def test_cli_workspace_defaults_and_overrides():
    args = parse_args([])
    assert args.workspace_dir == ".agent-test"
    assert args.persist_dir is None
    assert args.log_dir is None
    assert args.checkpoint_file is None
    assert args.checkpoint_dir is None

    custom = parse_args(
        [
            "--workspace-dir",
            "D:/runs",
            "--checkpoint-dir",
            "D:/runs/cp",
            "--graph-id",
            "g-1",
        ]
    )
    assert custom.workspace_dir == "D:/runs"
    assert custom.checkpoint_dir == "D:/runs/cp"
    assert custom.graph_id == "g-1"
