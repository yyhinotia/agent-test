"""Compactor / TokenMeter 集成测试：上下文窗口 10000 下的正常、准确压缩。"""
import asyncio

from agent_test import ReactAgent
from agent_test.llm.compactor import Compactor
from agent_test.llm.token_meter import TokenMeter
from agent_test.session.session import Session
from agent_test.types.events import EventType
from agent_test.types.messages import (
    AssistantMessage,
    Message,
    TextBlock,
    ToolResultMessage,
    UserMessage,
)
from agent_test.utils import get_uuid


def _user(text: str) -> UserMessage:
    return UserMessage(id=get_uuid(), content=[TextBlock(content=text)])


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(id=get_uuid(), content=[TextBlock(content=text)])


# ---------- Compactor 单元：按 turn 分块 / 排除近期 / 摘要准确 ----------


def test_compactor_compacts_old_turns_keeps_recent(tmp_path):
    """plan_blocks 按 turn 分块并排除最近一轮；compact 只压缩历史回合。"""
    session = Session(persist_dir=str(tmp_path))
    for turn, texts in ((1, ("a", "b")), (2, ("c", "d")), (3, ("e", "f"))):
        for idx, text in enumerate(texts):
            session.append("user/message", data=_user(text), turn=turn, step=idx + 1)
            session.append(
                "assistant/message", data=_assistant(text), turn=turn, step=idx + 1
            )

    compactor = Compactor(
        session=session, summarize=lambda text: f"SUM({len(text)})"
    )
    blocks = compactor.plan_blocks(recent_turns=1)
    assert [b[2] for b in blocks] == [1, 2]  # turn3（最近一轮）保留

    count = asyncio.run(compactor.compact())
    assert count == 2  # turn1 + turn2 各压成一条摘要

    events = session.events
    assert sum(1 for e in events if e.compacted) == 8  # turn1+2 的 8 条消息事件
    summaries = [e for e in events if e.type == EventType.COMPACT.value]
    assert len(summaries) == 2
    assert {e.turn for e in summaries} == {1, 2}

    # 摘要 data 与注入的 summarize 输出一致（准确性）
    for e in summaries:
        assert e.data.startswith("SUM(")  # 摘要来自注入的 summarize（重编号后不回溯原文本）

    # derive_messages：跳过 compacted 事件；摘要包装为 UserMessage；最近回合完整保留
    msgs = session.derive_messages()
    assert len(msgs) == 6  # turn3 的 4 条消息 + 2 条摘要
    assert sum(1 for m in msgs if isinstance(m, UserMessage)) == 4  # 2 摘要 + turn3 的 2 条用户消息
    assert sum(
        1
        for m in msgs
        if isinstance(m, UserMessage) and m.content[0].content.startswith("SUM(")
    ) == 2
    assert any(
        isinstance(m, UserMessage) and m.content[0].content == "e" for m in msgs
    )
    assert any(
        isinstance(m, AssistantMessage) and m.content[0].content == "f" for m in msgs
    )

    # 持久化 roundtrip：seq 集合连续、事件与消息一致
    restored = Session.from_file(session.session_id, persist_dir=str(tmp_path))
    seqs = [e.seq for e in restored.events]
    assert sorted(seqs) == list(range(len(seqs)))
    assert restored.events == session.events
    assert restored.derive_messages() == session.derive_messages()


def test_compactor_fallback_second_level_compacts_all(tmp_path):
    """一级无可压缩块时（只有一轮），compact 降级二级全量压缩。"""
    session = Session(persist_dir=str(tmp_path))
    session.append("user/message", data=_user("x"), turn=1, step=1)
    session.append("assistant/message", data=_assistant("y"), turn=1, step=1)

    compactor = Compactor(session=session, summarize=lambda text: "SUMMARY")
    assert compactor.plan_blocks(recent_turns=1) == []  # 只有一轮，一级无块

    count = asyncio.run(compactor.compact())  # 自动降级二级
    assert count == 1
    assert any(e.type == EventType.COMPACT.value for e in session.events)
    msgs = session.derive_messages()
    # 全量压成一条摘要 + 无残留原始消息
    assert len(msgs) == 1
    assert msgs[0].content[0].content == "SUMMARY"


def test_session_mark_and_insert_renumber(tmp_path):
    """mark_compacted + insert_after 后 seq 集合仍为 0..N-1。"""
    session = Session(persist_dir=str(tmp_path))
    session.append("user/message", data=_user("a"), turn=1, step=1)
    session.append("assistant/message", data=_assistant("b"), turn=1, step=1)
    session.append("user/message", data=_user("c"), turn=2, step=1)
    session.append("assistant/message", data=_assistant("d"), turn=2, step=1)

    last_index = session.mark_compacted(0, 2)
    assert last_index == 1
    inserted = session.insert_after(
        last_index, EventType.COMPACT, "摘要", turn=1, step=0
    )
    assert inserted.seq == 2
    seqs = [e.seq for e in session.events]
    assert seqs == list(range(len(session.events)))

    restored = Session.from_file(session.session_id, persist_dir=str(tmp_path))
    assert restored.events == session.events


# ---------- 端到端：上下文窗口 10000 下 TokenMeter 触发 Compactor ----------


def test_agent_context_window_10000_compacts_normally(tmp_path):
    """max_context_tokens=10000、阈值 0.8 时，usage 超过 8000 触发压缩。

    验证「正常 + 准确」：
    - 压缩确实发生（存在 compacted 事件与 compact/summary 摘要事件）；
    - 被压缩的旧消息不再进入上下文，摘要以 UserMessage 进入；
    - 最近一轮消息完整保留；
    - 持久化 roundtrip 后 seq 连续、derive_messages 一致。
    """
    calls = {"n": 0}

    class GrowingUsageLLM:
        """每次调用返回递增 usage：第 5 次起 total_tokens > 8000。"""

        async def stream(self, messages, tools):
            calls["n"] += 1
            total = 2000 + calls["n"] * 1500  # 3500, 5000, 6500, 8000, 9500, ...
            return (
                _assistant(f"回复{calls['n']}"),
                "finish",
                {
                    "prompt_tokens": total // 2,
                    "completion_tokens": total - total // 2,
                    "total_tokens": total,
                },
            )

    agent = ReactAgent(
        persist_dir=str(tmp_path / "sessions"),
        max_context_tokens=10000,
        threshold_ratio=0.8,
    )
    assert agent.token_meter.max_context_tokens == 10000
    assert agent.token_meter.threshold_tokens == 8000
    agent.llm_client = GrowingUsageLLM()
    # 注入确定性摘要，保证「准确」可断言
    agent.compactor = Compactor(
        session=agent.session, summarize=lambda text: "摘要:" + text[:20]
    )

    for i in range(1, 6):
        agent.inbox.append("turn", _user(f"问题{i}"))
        assert asyncio.run(agent.turn()) is True

    events = agent.session.events
    assert any(e.compacted for e in events), "上下文窗口 10000 下应触发压缩"
    summaries = [e for e in events if e.type == EventType.COMPACT.value]
    assert len(summaries) >= 1

    msgs = agent.session.derive_messages()
    # 最近回合（turn5）完整保留：用户问题与最终回答都在
    assert any(
        isinstance(m, UserMessage) and m.content[0].content == "问题5" for m in msgs
    )
    assert any(
        isinstance(m, AssistantMessage) and m.content[0].content == "回复5"
        for m in msgs
    )
    # 旧回合被摘要替代，摘要进入上下文
    assert any(
        isinstance(m, UserMessage) and m.content[0].content.startswith("摘要:")
        for m in msgs
    )
    # 被压缩的旧消息不再以原始内容出现在上下文（“问题1”应已被摘要吸收）
    assert not any(
        isinstance(m, UserMessage) and m.content[0].content.startswith("问题1")
        for m in msgs
    )

    # 正常：持久化 roundtrip 后 seq 连续、消息一致
    restored = Session.from_file(
        agent.session.session_id, persist_dir=str(tmp_path / "sessions")
    )
    seqs = [e.seq for e in restored.events]
    assert sorted(seqs) == list(range(len(seqs)))
    assert restored.derive_messages() == agent.session.derive_messages()