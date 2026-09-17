"""模块 6（生命周期）：create_agent 工厂 + reload session 恢复入口。

覆盖点：
- create_agent() 新建：自动 id、依赖可注入（llm_client / tools / persist_dir）；
- create_agent(session_id=...) 命中已存在且非空的会话文件时拒绝（防 seq 与
  编号冲突），错误提示指向 resume；
- create_agent(resume=True) / ReactAgent.resume：窗口化重载 + 续接 turn/step
  编号 + 磁盘全量 seq 连续；
- Session.resume 等价于 Session(...).reload()（from_file 的窗口版对偶）；
- main.py 的 --resume CLI 参数解析。
"""
import asyncio
import json

import pytest

from agent_test import (
    Compactor,
    EventType,
    ReactAgent,
    Session,
    SessionEditError,
    create_agent,
)
from agent_test.tools.center import ToolCenter
from agent_test.types.messages import AssistantMessage, TextBlock, UserMessage

SESSION_ID = "s-factory"


def _user(text: str) -> UserMessage:
    return UserMessage(id=f"u-{text}", content=[TextBlock(content=text)])


class _StubLLM:
    """最小 LLM 桩：记录看到的上下文，返回 finish 与极小 usage。"""

    def __init__(self):
        self.seen = []

    async def stream(self, messages, tools):
        self.seen.append(list(messages))
        return (
            AssistantMessage(id="a1", content=[TextBlock(content="ok")]),
            "finish",
            {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )


def _rows(session: Session) -> list:
    return [
        json.loads(line)
        for line in session.file_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _build_session(persist_dir: str) -> ReactAgent:
    """造一个「3 轮对话 + 一次压缩」的会话，磁盘上留下 compact 事件。"""
    agent = create_agent(
        SESSION_ID, persist_dir, llm_client=_StubLLM(), tools=ToolCenter()
    )
    agent.compactor = Compactor(session=agent.session, summarize=lambda text: "摘要")
    for i in range(1, 4):
        agent.inbox.append("turn", _user(f"q{i}"))
        assert asyncio.run(agent.turn()) is True
    asyncio.run(agent.compactor.compact(remain_turns=1))
    return agent


def test_create_agent_new_session(tmp_path):
    """create_agent()：自动 id 的新会话；指定 id 也走新建。"""
    agent = create_agent(
        persist_dir=str(tmp_path), llm_client=_StubLLM(), tools=ToolCenter()
    )
    assert isinstance(agent, ReactAgent)
    assert agent.session.session_id
    assert agent.session.file_path.parent == tmp_path
    assert (agent.phase.turn, agent.phase.step) == (0, 0)

    fixed = create_agent(
        "s-fixed", str(tmp_path), llm_client=_StubLLM(), tools=ToolCenter()
    )
    assert fixed.session.session_id == "s-fixed"


def test_create_agent_rejects_existing_non_empty_session(tmp_path):
    """已存在且非空的会话：拒绝新建，提示改用 resume（防编号/seq 冲突）。"""
    _build_session(str(tmp_path))

    with pytest.raises(SessionEditError) as exc_info:
        create_agent(
            SESSION_ID, str(tmp_path), llm_client=_StubLLM(), tools=ToolCenter()
        )

    error = exc_info.value
    assert error.location == "create_agent"
    assert "resume" in str(error)
    assert error.detail["session_id"] == SESSION_ID


def test_create_agent_resume_continues_window_numbering_and_seq(tmp_path):
    """resume=True：只加载窗口、续接编号、磁盘仍保留全量历史。"""
    _build_session(str(tmp_path))
    llm = _StubLLM()

    agent = create_agent(
        SESSION_ID, str(tmp_path), resume=True, llm_client=llm, tools=ToolCenter()
    )

    # 窗口 = 摘要 + 之后的事件；编号续接历史（3 轮 -> 下一轮为 4）
    assert agent.session.events[0].type == EventType.COMPACT.value
    assert (agent.phase.turn, agent.phase.step) == (3, 3)
    # 续聊上下文以摘要开头（被压缩的历史不再以原文进入）
    assert llm.seen == []
    assert agent.session.file_path.exists()

    agent.inbox.append("turn", _user("q4"))
    assert asyncio.run(agent.turn()) is True
    assert (agent.phase.turn, agent.phase.step) == (4, 4)
    ids = [m.id for m in llm.seen[0]]
    assert ids[0].startswith("compact-")  # 摘要以 UserMessage 进入上下文

    rows = _rows(agent.session)
    assert [row["seq"] for row in rows] == list(range(len(rows)))  # 全量 seq 连续
    assert len(rows) > len(agent.session.events)  # 磁盘含未载入内存的前缀
    assert rows[0]["type"] == EventType.TURN_START.value  # 第 1 轮起点仍在
    assert any(row["compacted"] for row in rows)  # 被压缩的旧事件保留在磁盘


def test_react_agent_resume_classmethod(tmp_path):
    """ReactAgent.resume 与 create_agent(resume=True) 等价。"""
    _build_session(str(tmp_path))

    agent = ReactAgent.resume(
        SESSION_ID, str(tmp_path), llm_client=_StubLLM(), tools=ToolCenter()
    )

    assert (agent.phase.turn, agent.phase.step) == (3, 3)
    assert agent.session.events[0].type == EventType.COMPACT.value


def test_session_resume_equals_reload(tmp_path):
    """Session.resume(...) == Session(...).reload()。"""
    _build_session(str(tmp_path))

    a = Session.resume(SESSION_ID, str(tmp_path))
    b = Session(SESSION_ID, str(tmp_path)).reload()

    assert [e.seq for e in a.events] == [e.seq for e in b.events]
    assert a._prefix_events == b._prefix_events
    assert a._prefix_events > 0  # 确实只加载了窗口
    assert a.events[0].type == EventType.COMPACT.value


def test_create_agent_resume_argument_validation(tmp_path):
    """参数校验：resume=True 必须给 session_id，且不接受 session=。"""
    with pytest.raises(ValueError, match="session_id"):
        create_agent(persist_dir=str(tmp_path), resume=True)

    with pytest.raises(ValueError, match="session"):
        create_agent(
            SESSION_ID,
            str(tmp_path),
            resume=True,
            session=Session(session_id="s-other", persist_dir=str(tmp_path)),
        )


def test_main_cli_resume_flag():
    """演示入口支持 --resume/-r 指定会话续聊。"""
    import main

    args = main.parse_args(["--resume", "s-9", "继续"])
    assert args.resume == "s-9"
    assert args.prompt == "继续"

    short = main.parse_args(["-r", "s-9"])
    assert short.resume == "s-9"
    assert short.prompt  # 有默认 prompt

    plain = main.parse_args([])
    assert plain.resume is None