"""Compactor / TokenMeter 集成测试：上下文窗口 10000 下的正常、准确压缩。

按知识包（docs/knowledge.txt）验收：
- 单 turn 多 step 场景按 step 粒度排除最近 remain_turns 个 step（非按 turn）；
- compact_session 输出 "[turn=N step=S] Role: content"，ToolResult 超窗口裁剪；
- 两级压缩：一级保留近期 step，无可压缩块时降级二级全量；
- 端到端：max_context_tokens=10000、阈值 0.8 时 usage 超 8000 触发压缩，
  压缩正常且准确（旧回合被摘要替代、最近回合保留、roundtrip seq 连续）。
"""
import asyncio

from agent_test import ReactAgent
from agent_test.llm.compactor import Compactor
from agent_test.session.session import Session
from agent_test.types.events import EventType
from agent_test.types.messages import (
    AssistantMessage,
    TextBlock,
    ToolResultMessage,
    UserMessage,
)
from agent_test.utils import get_uuid


def _user(text: str) -> UserMessage:
    return UserMessage(id=get_uuid(), content=[TextBlock(content=text)])


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(id=get_uuid(), content=[TextBlock(content=text)])


def _tool(text: str, *, is_error: bool = False) -> ToolResultMessage:
    return ToolResultMessage(
        tool_call_id="tc-1",
        content=[TextBlock(content=text)],
        is_error=is_error,
    )


# ---------- 单元：按 (turn,step) 排除近期（step 粒度，非 turn） ----------


def test_compactor_single_turn_multi_step_keeps_recent_steps(tmp_path):
    """单 turn 多 step：排除最近 remain_turns 个 step，压缩更早的 step。

    知识包验收标准：单 turn 多 step 场景压缩正确排除最近 N 个 step
    （非按 turn 排除——若按 turn 会把整个 turn 保留、一个都压不掉）。
    """
    session = Session(persist_dir=str(tmp_path))
    session.append("user/message", data=_user("问题"), turn=1, step=0)
    # 同一 turn 内 5 次 LLM 调用（step 全局递增 1..5）
    for step in range(1, 6):
        session.append(
            "assistant/message",
            data=_assistant(f"回复{step}"),
            turn=1,
            step=step,
        )
        session.append(
            "tool/result", data=_tool(f"结果{step}"), turn=1, step=step
        )

    compactor = Compactor(session=session, summarize=lambda text: "SUM")
    summary = asyncio.run(compactor.compact())  # remain_turns 默认 2

    assert summary == "SUM"
    events = session.events
    summaries = [e for e in events if e.type == EventType.COMPACT.value]
    assert len(summaries) == 1

    # 最近 2 个 step（4、5）保留，更早的 step（0..3，含用户消息）被压缩
    # 内存窗口化：被压缩旧事件从 events 移除，
    # 只保留 compact 摘要 + 最近 step(4/5) 的 4 条事件
    assert not any(e.compacted for e in events), "内存不应残留 compacted 事件"
    assert len(events) == 5  # compact 摘要 + step4/5 的 assistant/tool 各 2 条
    assert all(e.type == EventType.COMPACT.value or e.step >= 4 for e in events)

    msgs = session.derive_messages()
    assert any(
        isinstance(m, UserMessage) and m.content[0].content == "SUM"
        for m in msgs
    )
    assert any(
        isinstance(m, AssistantMessage) and m.content[0].content == "回复4"
        for m in msgs
    )
    assert any(
        isinstance(m, AssistantMessage) and m.content[0].content == "回复5"
        for m in msgs
    )
    assert not any(
        isinstance(m, AssistantMessage) and m.content[0].content == "回复1"
        for m in msgs
    )

    # 持久化 roundtrip：磁盘是全量归档（含 7 条 compacted），
    # seq 连续，derive 与内存窗口一致（compacted 事件被跳过）
    restored = Session.from_file(session.session_id, persist_dir=str(tmp_path))
    assert len([e for e in restored.events if e.compacted]) == 7
    assert sorted(e.seq for e in restored.events) == list(
        range(len(restored.events))
    )
    assert restored.derive_messages() == session.derive_messages()


def test_compactor_multi_turn_keeps_recent_steps(tmp_path):
    """多 turn：全局 (turn,step) 排序，保留最近 remain_turns 个 step。"""
    session = Session(persist_dir=str(tmp_path))
    # turn1: step1-2；turn2: step3-4；turn3: step5-6（step 全局递增）
    for turn, (s1, s2) in ((1, (1, 2)), (2, (3, 4)), (3, (5, 6))):
        session.append(
            "user/message", data=_user(f"Q{turn}"), turn=turn, step=0
        )
        session.append(
            "assistant/message",
            data=_assistant(f"A{turn}-{s1}"),
            turn=turn,
            step=s1,
        )
        session.append(
            "assistant/message",
            data=_assistant(f"A{turn}-{s2}"),
            turn=turn,
            step=s2,
        )

    compactor = Compactor(
        session=session, summarize=lambda text: f"SUM({len(text)})"
    )
    summary = asyncio.run(compactor.compact())

    assert summary is not None and summary.startswith("SUM(")
    msgs = session.derive_messages()
    # 最近 2 个 step（5、6，均在 turn3）完整保留
    assert any(
        isinstance(m, AssistantMessage) and m.content[0].content == "A3-5"
        for m in msgs
    )
    assert any(
        isinstance(m, AssistantMessage) and m.content[0].content == "A3-6"
        for m in msgs
    )
    # turn1/turn2 全部被压缩吸收
    assert not any(
        isinstance(m, AssistantMessage) and m.content[0].content.startswith("A1")
        for m in msgs
    )
    assert not any(
        isinstance(m, AssistantMessage) and m.content[0].content.startswith("A2")
        for m in msgs
    )


def test_compact_session_clips_tool_result_and_formats_lines(tmp_path):
    """compact_session 输出 [turn=N step=S] Role: content；ToolResult 裁剪。"""
    session = Session(persist_dir=str(tmp_path))
    session.append("user/message", data=_user("你好"), turn=1, step=0)
    session.append("assistant/message", data=_assistant("回复"), turn=1, step=1)
    session.append(
        "tool/result", data=_tool("x" * 3000, is_error=True), turn=1, step=1
    )

    compactor = Compactor(session=session, tool_head=5, tool_tail=5)
    lines = compactor.compact_session(session.events)

    assert lines[0] == "[turn=1 step=0] user: 你好"
    assert lines[1] == "[turn=1 step=1] assistant: 回复"
    t = lines[2]
    assert t.startswith("[turn=1 step=1] tool:tc-1:error: xxxxx")
    assert "省略" in t
    assert t.endswith("xxxxx")
    assert len(lines) == 3


# ---------- 单元：两级压缩降级 ----------


def test_compactor_fallback_second_level_compacts_all(tmp_path):
    """一级无可压缩块时（只有 1 个 step），compact 降级二级全量压缩。"""
    session = Session(persist_dir=str(tmp_path))
    session.append("user/message", data=_user("x"), turn=1, step=0)
    session.append("assistant/message", data=_assistant("y"), turn=1, step=1)

    compactor = Compactor(session=session, summarize=lambda text: "SUMMARY")
    # 一级 remain_turns=2：2 个 (turn,step) 对 <= 2，无可压缩 → 二级全量
    summary = asyncio.run(compactor.compact())

    assert summary == "SUMMARY"
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
    assert sorted(e.seq for e in restored.events) == list(
        range(len(restored.events))
    )
    assert restored.events == session.events


# ---------- 端到端：上下文窗口 10000 下 TokenMeter 触发 Compactor ----------


def test_agent_context_window_10000_compacts_normally(tmp_path):
    """max_context_tokens=10000、阈值 0.8 时，usage 超过 8000 触发压缩。

    验证「正常 + 准确」：
    - 压缩确实发生（存在 compacted 事件与 compact/summary 摘要事件）；
    - 被压缩的旧消息不再进入上下文，摘要以 UserMessage 进入；
    - 最近回合完整保留；
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
    summaries = [e for e in events if e.type == EventType.COMPACT.value]
    assert len(summaries) >= 1, "上下文窗口 10000 下应触发压缩"
    # 内存窗口化：events 即上下文窗口，无 compacted 残留
    assert not any(e.compacted for e in events), "内存不应残留 compacted 事件"

    msgs = agent.session.derive_messages()
    # 最近回合（turn5）完整保留：用户问题与最终回答都在
    assert any(
        isinstance(m, UserMessage) and m.content[0].content == "问题5"
        for m in msgs
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
    # 被压缩的旧消息不再以原始内容出现在上下文（"问题1" 应已被摘要吸收）
    assert not any(
        isinstance(m, UserMessage) and m.content[0].content.startswith("问题1")
        for m in msgs
    )

    # 正常：磁盘保持全量归档（含被压缩事件），
    # roundtrip seq 连续、derive 与内存窗口一致
    restored = Session.from_file(
        agent.session.session_id, persist_dir=str(tmp_path / "sessions")
    )
    seqs = [e.seq for e in restored.events]
    assert sorted(seqs) == list(range(len(seqs)))
    assert any(e.compacted for e in restored.events), "磁盘应保留被压缩事件"
    assert restored.derive_messages() == agent.session.derive_messages()

# ---------- 单元：usage 降级（非流式兜底请求，知识包 3.4） ----------


class _NS:
    """极简命名空间：避免引入额外 import。"""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _fallback_adapter(on_create):
    """构造最小 OPENAIAdapter（绕过 __init__ 对 env 的依赖）。"""
    from agent_test.llm.adapter import OPENAIAdapter

    adapter = object.__new__(OPENAIAdapter)
    adapter.model_name = "test-model"
    adapter.client = _NS(chat=_NS(completions=_NS(create=on_create)))
    return adapter


def test_fetch_usage_fallback_sends_non_streaming_request():
    """流式无 usage 时：降级发 stream=False / max_tokens=1 请求取准确 usage。"""
    recorded = {}

    async def fake_create(**kwargs):
        recorded["kwargs"] = kwargs
        return _NS(
            usage=_NS(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        )

    adapter = _fallback_adapter(fake_create)
    tools = [{"type": "function", "function": {"name": "bash", "parameters": {}}}]
    usage = asyncio.run(
        adapter._fetch_usage_fallback([{"role": "user", "content": "hi"}], tools)
    )

    assert usage == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }
    kwargs = recorded["kwargs"]
    assert kwargs["stream"] is False
    assert kwargs["max_tokens"] == 1
    assert kwargs["model"] == "test-model"
    assert kwargs["tools"] == tools


def test_fetch_usage_fallback_failure_returns_none():
    """兜底请求抛异常或响应无 usage 时返回 None，不阻断主流程。"""

    async def boom(**kwargs):
        raise RuntimeError("network down")

    adapter = _fallback_adapter(boom)
    assert asyncio.run(adapter._fetch_usage_fallback([], None)) is None

    async def no_usage(**kwargs):
        return _NS(usage=None)

    adapter2 = _fallback_adapter(no_usage)
    assert asyncio.run(adapter2._fetch_usage_fallback([], None)) is None

# ---------- 单元：内存窗口化（events 只作上下文窗口，磁盘保全量） ----------


def test_compactor_memory_window_keeps_disk_full(tmp_path):
    """压缩后内存只保留上下文窗口，磁盘保持全量历史。

    窗口化优化 + 知识包契约：
    - 内存 events 不再残留 compacted 事件（只含 compact 摘要 + 最近事件）；
    - 磁盘 JSONL 始终是全量 0..N-1 序列（被压缩事件完整持久化）；
    - 窗口化后 append 的 seq 仍全局连续；from_file 恢复全量且 derive 一致；
    - 二次压缩稳定：磁盘全量不丢、seq 保持连续。
    """
    session = Session(persist_dir=str(tmp_path))
    for turn in (1, 2, 3):
        session.append(
            "user/message", data=_user(f"Q{turn}"), turn=turn, step=0
        )
        session.append(
            "assistant/message",
            data=_assistant(f"A{turn}-1"),
            turn=turn,
            step=(turn - 1) * 2 + 1,
        )
        session.append(
            "tool/result", data=_tool(f"R{turn}"), turn=turn, step=(turn - 1) * 2 + 1
        )
        session.append(
            "assistant/message",
            data=_assistant(f"A{turn}-2"),
            turn=turn,
            step=(turn - 1) * 2 + 2,
        )
    compact_total = len(session.events)  # 12

    compactor = Compactor(session=session, summarize=lambda text: "SUMMARY")
    asyncio.run(compactor.compact())

    # 内存 = 上下文窗口：compact 摘要 + 最近 step(5/6) 的 3 条，无 compacted 残留
    assert not any(e.compacted for e in session.events)
    assert any(e.type == EventType.COMPACT.value for e in session.events)
    assert len(session.events) == 4  # 摘要 + A3-1/R3/A3-2

    # 磁盘全量：12 条原始事件 + 摘要，其中 9 条 compacted=True 完整落盘
    restored = Session.from_file(session.session_id, persist_dir=str(tmp_path))
    assert len(restored.events) == compact_total + 1
    assert len([e for e in restored.events if e.compacted]) == 9
    assert sorted(e.seq for e in restored.events) == list(
        range(len(restored.events))
    )
    assert restored.derive_messages() == session.derive_messages()

    # 窗口化后 append：seq 全局连续，磁盘继续追加
    before_max = max(e.seq for e in session.events)
    session.append("user/message", data=_user("Q4"), turn=4, step=0)
    assert session.events[-1].seq == before_max + 1

    # 二次压缩稳定：内存窗口化 + 磁盘全量不丢
    asyncio.run(compactor.compact())
    restored2 = Session.from_file(session.session_id, persist_dir=str(tmp_path))
    assert sorted(e.seq for e in restored2.events) == list(
        range(len(restored2.events))
    )
    assert len(restored2.events) >= compact_total
    assert restored2.derive_messages() == session.derive_messages()
