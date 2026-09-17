"""Session.reload：倒序读取 JSONL，加载「最近一次压缩之后」的上下文窗口。

覆盖点：
- 倒序读取在第一条 compact/summary 事件处停止（含该事件）；
- 无 compact 事件时退化为加载全量；
- 重载后继续 append：内存窗口与磁盘全量 seq 都保持连续；
- 窗口化重载后的 mark_compacted + insert_after（_persist_all）不会用
  内存窗口覆盖磁盘全量历史；
- 前缀不再解析（倒序读取的证明）、窗口内损坏行报错、空文件、seq 断层。
"""
import asyncio
import json

import pytest

from agent_test import (
    Compactor,
    EventType,
    ReactAgent,
    Session,
    SessionContinuityError,
    SessionEditError,
)
from agent_test.session.session import _iter_lines_reverse
from agent_test.tools.center import ToolCenter
from agent_test.types.messages import AssistantMessage, TextBlock, UserMessage

SESSION_ID = "s-reload"


def _user(text: str) -> UserMessage:
    return UserMessage(id=f"u-{text}", content=[TextBlock(content=text)])


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(id=f"a-{text}", content=[TextBlock(content=text)])


def _lines(session: Session) -> list:
    """返回磁盘上的非空 JSONL 行（原样文本）。"""
    return [
        line
        for line in session.file_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class _StubLLM:
    """最小 LLM 桩：记录每次看到的上下文，返回 finish 与极小 usage。"""

    def __init__(self):
        self.seen = []

    async def stream(self, messages, tools):
        self.seen.append(list(messages))
        return (
            _assistant(f"reply-{len(self.seen)}"),
            "finish",
            {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )


def _build_compacted_session(persist_dir: str) -> Session:
    """构造「历史 + 摘要 + 近期事件」会话，磁盘 seq 为 0..6。

    seq 0..3：历史消息（被标记 compacted）
    seq 4   ：compact/summary 摘要事件
    seq 5..6：摘要之后的近期消息
    """
    session = Session(session_id=SESSION_ID, persist_dir=persist_dir)
    session.append(EventType.USER_MESSAGE, data=_user("1"))
    session.append(EventType.ASSISTANT_MESSAGE, data=_assistant("1"))
    session.append(EventType.USER_MESSAGE, data=_user("2"))
    session.append(EventType.ASSISTANT_MESSAGE, data=_assistant("2"))
    session.mark_compacted(0, 4)
    session.insert_after(3, EventType.COMPACT, "第一轮摘要")
    session.cut_to_context_window()
    session.append(EventType.USER_MESSAGE, data=_user("3"))
    session.append(EventType.ASSISTANT_MESSAGE, data=_assistant("3"))
    return session


def test_reload_loads_window_from_latest_compact(tmp_path):
    """倒序读到第一条 compact/summary 即停止，窗口 = 摘要 + 之后的事件。"""
    _build_compacted_session(str(tmp_path))

    restored = Session(session_id=SESSION_ID, persist_dir=str(tmp_path)).reload()

    assert [e.seq for e in restored.events] == [4, 5, 6]
    assert restored.events[0].type == EventType.COMPACT.value
    assert restored.events[0].data == "第一轮摘要"
    assert [e.type for e in restored.events[1:]] == [
        EventType.USER_MESSAGE.value,
        EventType.ASSISTANT_MESSAGE.value,
    ]


def test_reload_derive_messages_starts_with_compact_summary(tmp_path):
    """窗口内 derive_messages：摘要包装为 UserMessage + 之后的近期消息。"""
    _build_compacted_session(str(tmp_path))

    restored = Session(session_id=SESSION_ID, persist_dir=str(tmp_path)).reload()
    messages = restored.derive_messages()

    assert [m.id for m in messages] == ["compact-4", "u-3", "a-3"]
    assert messages[0].content[0].content == "第一轮摘要"


def test_reload_records_skipped_prefix(tmp_path):
    """被跳过的磁盘前缀记账为 _prefix_events（供 _persist_all 回填）。"""
    _build_compacted_session(str(tmp_path))

    restored = Session(session_id=SESSION_ID, persist_dir=str(tmp_path)).reload()

    assert restored._prefix_events == 4
    assert restored._seq == 7  # 最后一条事件 seq(6) + 1


def test_reload_without_compact_loads_all_events(tmp_path):
    """没有 compact 事件时退化为加载全部事件（前缀为 0）。"""
    session = Session(session_id="s-plain", persist_dir=str(tmp_path))
    session.append(EventType.USER_MESSAGE, data=_user("1"))
    session.append(EventType.ASSISTANT_MESSAGE, data=_assistant("1"))

    restored = Session(session_id="s-plain", persist_dir=str(tmp_path)).reload()

    assert [e.seq for e in restored.events] == [0, 1]
    assert restored._prefix_events == 0
    assert restored._seq == 2


def test_reload_appends_stay_contiguous_on_disk(tmp_path):
    """重载后继续 append：内存窗口与磁盘全量 seq 都保持连续。"""
    _build_compacted_session(str(tmp_path))
    restored = Session(session_id=SESSION_ID, persist_dir=str(tmp_path)).reload()

    restored.append(EventType.USER_MESSAGE, data=_user("4"))

    assert restored.events[-1].seq == 7
    assert [json.loads(line)["seq"] for line in _lines(restored)] == list(range(8))


def test_reload_does_not_parse_skipped_prefix(tmp_path):
    """倒序读取在 compact 处停止：损坏的前缀行不会被解析，也不报错。"""
    session = _build_compacted_session(str(tmp_path))
    lines = _lines(session)
    lines[0] = "{ 这一行故意损坏，且位于 compact 事件之前"
    session.file_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    restored = Session(session_id=SESSION_ID, persist_dir=str(tmp_path)).reload()

    assert [e.seq for e in restored.events] == [4, 5, 6]


def test_persist_all_after_reload_preserves_prefix_history(tmp_path):
    """窗口化重载后再压缩：磁盘前缀按行回填，全量历史不被窗口覆盖。"""
    session = _build_compacted_session(str(tmp_path))
    original_prefix = _lines(session)[:4]
    restored = Session(session_id=SESSION_ID, persist_dir=str(tmp_path)).reload()

    # 对窗口内 seq 5..6 再压缩一次（mark_compacted + insert_after -> _persist_all）
    last_index = restored.mark_compacted(5, 7)
    restored.insert_after(last_index, EventType.COMPACT, "第二轮摘要")

    lines = _lines(restored)
    assert [json.loads(line)["seq"] for line in lines] == list(range(8))
    assert lines[:4] == original_prefix  # 前缀原样保留
    assert json.loads(lines[-1])["type"] == EventType.COMPACT.value
    assert json.loads(lines[-1])["data"] == "第二轮摘要"


def test_reload_corrupt_line_in_window_raises(tmp_path):
    """窗口内某行损坏 -> SessionEditError（含倒数行号），完整堆栈进日志。"""
    session = _build_compacted_session(str(tmp_path))
    session.file_path.write_text(
        session.file_path.read_text(encoding="utf-8") + "not-json-line\n",
        encoding="utf-8",
    )

    with pytest.raises(SessionEditError) as exc_info:
        Session(session_id=SESSION_ID, persist_dir=str(tmp_path)).reload()

    error = exc_info.value
    assert error.location == "Session.reload"
    assert error.detail["line_from_end"] == 1
    assert isinstance(error.__cause__, ValueError)  # 保留原始解析异常链


def test_reload_empty_file_yields_empty_events(tmp_path):
    """空文件：events 为空、游标为 0，之后 append 从 seq 0 开始。"""
    Session(session_id="s-empty", persist_dir=str(tmp_path))

    restored = Session(session_id="s-empty", persist_dir=str(tmp_path)).reload()

    assert restored.events == []
    assert restored._seq == 0
    restored.append(EventType.USER_MESSAGE, data=_user("1"))
    assert restored.events[0].seq == 0


def test_reload_detects_non_contiguous_window(tmp_path):
    """窗口内 seq 断层：strict=True 抛 SessionContinuityError。"""
    session = Session(session_id="s-gap", persist_dir=str(tmp_path))
    session.append(EventType.USER_MESSAGE, data=_user("1"))
    session.append(EventType.ASSISTANT_MESSAGE, data=_assistant("1"))
    lines = _lines(session)
    broken = json.loads(lines[1])
    broken["seq"] = 5
    session.file_path.write_text(
        lines[0] + "\n" + json.dumps(broken, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(SessionContinuityError):
        Session(session_id="s-gap", persist_dir=str(tmp_path)).reload(strict=True)


def test_iter_lines_reverse_matches_forward_order(tmp_path):
    """倒序迭代器：跨块边界的行、无尾换行、多字节字符都要正确。"""
    path = tmp_path / "lines.txt"
    lines = [f"第 {i} 行 ascii-{i}" for i in range(1, 41)]
    path.write_text("\n".join(lines), encoding="utf-8")  # 无结尾换行

    got = [
        line.decode("utf-8")
        for line in _iter_lines_reverse(path, block_size=7)
        if line.strip()
    ]

    assert got == list(reversed(lines))


def test_reload_window_resumes_agent_conversation(tmp_path):
    """端到端：reload 出的窗口可直接作为新 Agent 的 session 继续对话。

    续聊时 LLM 看到的上下文应是「摘要 + 摘要之后的近期事件 + 新问题」，
    被摘要吸收的旧消息不再进入；磁盘全量历史与 seq 连续性都不受影响。
    """
    persist_dir = str(tmp_path / "sessions")
    llm = _StubLLM()

    session = _build_compacted_session(persist_dir)
    total_before = len(_lines(session))

    reloaded = Session(session_id=SESSION_ID, persist_dir=persist_dir).reload()
    agent = ReactAgent(session=reloaded, llm_client=llm, tools=ToolCenter())
    agent.inbox.append("turn", _user("新问题"))
    assert asyncio.run(agent.turn()) is True

    # 首次 LLM 调用的上下文 = 摘要 + 摘要之后的近期事件 + 新问题
    assert [m.id for m in llm.seen[0]] == [
        "compact-4",
        "u-3",
        "a-3",
        "u-新问题",
    ]
    # 磁盘仍是全量历史（前缀 + 窗口 + 新一轮），seq 全局连续
    lines = _lines(reloaded)
    assert lines[0] == _lines(session)[0]
    assert len(lines) > total_before
    assert [json.loads(line)["seq"] for line in lines] == list(range(len(lines)))
    assert any(
        json.loads(line)["data"] == "第一轮摘要" for line in lines
    )


# ---------- 恢复编号：续聊续接 session 已有 turn / step ----------

NUMBERED_ID = "s-numbered"


def _build_numbered_session(persist_dir: str) -> Session:
    """构造带 turn/step 编号的会话：turn1(step1) / turn2(step2, step3)。"""
    session = Session(session_id=NUMBERED_ID, persist_dir=persist_dir)
    session.append(EventType.USER_MESSAGE, data=_user("1"), turn=1, step=1)
    session.append(EventType.ASSISTANT_MESSAGE, data=_assistant("1"), turn=1, step=1)
    session.append(EventType.USER_MESSAGE, data=_user("2"), turn=2, step=2)
    session.append(EventType.ASSISTANT_MESSAGE, data=_assistant("2"), turn=2, step=2)
    session.append(EventType.USER_MESSAGE, data=_user("3"), turn=2, step=3)
    return session


def test_max_turn_step_reads_all_events(tmp_path):
    """max_turn_step：空会话 (0, 0)；有事件时取 turn / step 的最大值。"""
    empty = Session(session_id="s-none", persist_dir=str(tmp_path))
    assert empty.max_turn_step() == (0, 0)

    session = _build_numbered_session(str(tmp_path))
    assert session.max_turn_step() == (2, 3)


def test_agent_resumes_existing_numbering_after_from_file(tmp_path):
    """from_file 全量恢复后，Agent 续接已有 turn / step 而非从 0 重来。"""
    _build_numbered_session(str(tmp_path))
    restored = Session.from_file(NUMBERED_ID, persist_dir=str(tmp_path))

    agent = ReactAgent(session=restored, llm_client=_StubLLM(), tools=ToolCenter())
    assert (agent.phase.turn, agent.phase.step) == (2, 3)

    # 空 inbox 调 turn()：不消耗刚恢复的编号，也不写事件
    assert asyncio.run(agent.turn()) is False
    assert (agent.phase.turn, agent.phase.step) == (2, 3)

    agent.inbox.append("turn", _user("4"))
    assert asyncio.run(agent.turn()) is True

    assert agent.phase.turn == 3
    assert agent.phase.step == 4  # step 是全局递增计数器
    turn_starts = [
        e for e in restored.events if e.type == EventType.TURN_START.value
    ]
    assert turn_starts[-1].turn == 3
    step_events = [e for e in restored.events if e.turn == 3 and e.step]
    assert step_events and all(e.step == 4 for e in step_events)


def test_agent_resumes_numbering_from_reload_window(tmp_path):
    """reload 窗口续聊：编号上界由窗口（含摘要事件）保留。"""
    persist_dir = str(tmp_path / "sessions")
    session = Session(session_id="s-window", persist_dir=persist_dir)
    for turn in (1, 2, 3):
        session.append(
            EventType.USER_MESSAGE,
            data=_user(str(turn)),
            turn=turn,
            step=turn,
        )

    summary_text = asyncio.run(
        Compactor(session=session, summarize=lambda text: "摘要").compact(
            remain_turns=1
        )
    )
    assert summary_text == "摘要"

    summary = [
        e for e in session.events if e.type == EventType.COMPACT.value
    ][0]
    # 摘要事件携带被压缩区间 turn1..turn2 的 (turn, step) 上界
    assert (summary.turn, summary.step) == (2, 2)

    reloaded = Session(session_id="s-window", persist_dir=persist_dir).reload()
    assert reloaded.max_turn_step() == (3, 3)

    agent = ReactAgent(session=reloaded, llm_client=_StubLLM(), tools=ToolCenter())
    assert (agent.phase.turn, agent.phase.step) == (3, 3)

    agent.inbox.append("turn", _user("next"))
    assert asyncio.run(agent.turn()) is True
    assert (agent.phase.turn, agent.phase.step) == (4, 4)


def test_reload_window_of_full_compaction_keeps_numbering(tmp_path):
    """二级全量压缩后窗口只剩摘要：编号上界仍由摘要事件带回。"""
    persist_dir = str(tmp_path / "sessions")
    session = Session(session_id="s-full", persist_dir=persist_dir)
    session.append(EventType.USER_MESSAGE, data=_user("1"), turn=1, step=1)
    session.append(EventType.ASSISTANT_MESSAGE, data=_assistant("1"), turn=1, step=5)

    summary_text = asyncio.run(
        Compactor(session=session, summarize=lambda text: "全量摘要").compact()
    )
    assert summary_text == "全量摘要"
    # 一级（保留最近 2 个 step）无可压缩块 -> 降级二级全量，窗口只剩摘要
    assert [e.type for e in session.events] == [EventType.COMPACT.value]

    reloaded = Session(session_id="s-full", persist_dir=persist_dir).reload()
    assert [e.type for e in reloaded.events] == [EventType.COMPACT.value]
    assert reloaded.max_turn_step() == (1, 5)

    agent = ReactAgent(session=reloaded, llm_client=_StubLLM(), tools=ToolCenter())
    assert (agent.phase.turn, agent.phase.step) == (1, 5)


def test_compact_summary_carries_range_upper_bound(tmp_path):
    """摘要事件带被压缩区间的 (turn, step) 上界（区间最大值，非末条事件值）。"""
    session = Session(session_id="s-range", persist_dir=str(tmp_path))
    session.append(EventType.ASSISTANT_MESSAGE, data=_assistant("1"), turn=1, step=1)
    session.append(EventType.ASSISTANT_MESSAGE, data=_assistant("2"), turn=2, step=2)
    # 末条可压缩事件是 turn 级（step=0）事件：只取末条会低估上界
    session.append(EventType.USER_MESSAGE, data=_user("3"), turn=3, step=0)
    session.append(EventType.ASSISTANT_MESSAGE, data=_assistant("4"), turn=4, step=4)

    asyncio.run(
        Compactor(session=session, summarize=lambda text: "区间摘要").compact(
            remain_turns=1
        )
    )

    summary = [
        e for e in session.events if e.type == EventType.COMPACT.value
    ][0]
    assert (summary.turn, summary.step) == (3, 2)


def test_resume_numbering_invariants_over_repeated_compaction(tmp_path):
    """多次压缩 + reload 续聊后的编号不变量。

    长会话反复压缩、跨会话续聊后仍应满足：磁盘全量 seq 连续；每轮的最大
    step 随 turn 单调不减；续聊新增事件的 step 严格大于续聊前的最大 step
    （即 step 是全局递增计数器，跨会话不重号）。
    """
    persist_dir = str(tmp_path / "sessions")
    agent = ReactAgent(
        persist_dir=persist_dir, llm_client=_StubLLM(), tools=ToolCenter()
    )
    agent.compactor = Compactor(session=agent.session, summarize=lambda text: "SUM")

    for i in range(1, 5):
        agent.inbox.append("turn", _user(f"q{i}"))
        assert asyncio.run(agent.turn()) is True
        asyncio.run(agent.compactor.compact(remain_turns=1))

    sid = agent.session.session_id
    window = Session(session_id=sid, persist_dir=persist_dir).reload()
    resumed = ReactAgent(
        session=window, llm_client=_StubLLM(), tools=ToolCenter()
    )
    resumed.compactor = Compactor(session=window, summarize=lambda text: "SUM2")
    resume_turn, resume_step = resumed.phase.turn, resumed.phase.step
    assert (resume_turn, resume_step) == agent.session.max_turn_step()

    for i in range(5, 7):
        resumed.inbox.append("turn", _user(f"q{i}"))
        assert asyncio.run(resumed.turn()) is True
        asyncio.run(resumed.compactor.compact(remain_turns=1))

    rows = [json.loads(line) for line in _lines(window)]
    # 不变量 1：磁盘全量 seq 连续（窗口化重载后仍未被窗口覆盖）
    assert [row["seq"] for row in rows] == list(range(len(rows)))
    # 不变量 2：每轮最大 step 随 turn 单调不减（排除 turn 级 step=0 事件）
    per_turn_max = {}
    for row in rows:
        key = row["turn"]
        per_turn_max[key] = max(per_turn_max.get(key, 0), row["step"])
    ordered = [per_turn_max[turn] for turn in sorted(per_turn_max)]
    assert ordered == sorted(ordered)
    # 不变量 3：续聊后的 step 级事件严格大于续聊前的最大 step
    # （跨会话不重号）。摘要事件的 (turn, step) 是「被压缩区间上界」而非
    # 它自身的位置，故按设计排除在编号比较之外。
    before = max(row["step"] for row in rows if row["turn"] <= resume_turn)
    after = [
        row["step"]
        for row in rows
        if row["turn"] > resume_turn
        and row["step"]
        and row["type"] != EventType.COMPACT.value
    ]
    assert after and min(after) > before
    # 不变量 4：窗口仍是「摘要 + 摘要之后」的上下文
    assert any(row["type"] == EventType.COMPACT.value for row in rows)
    assert window.events[0].type == EventType.COMPACT.value or any(
        e.type == EventType.COMPACT.value for e in window.events
    )
