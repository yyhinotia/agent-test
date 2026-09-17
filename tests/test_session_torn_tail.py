"""会话文件「写一半」尾部（torn tail）的容错与清理。

覆盖点：
- from_file / reload 容忍末尾残缺行（无换行结尾 + 解析失败）；
- 中间行损坏仍抛 SessionEditError（不放过真损坏）；
- 恢复后清理磁盘残片：全量历史（前缀）不丢、seq 仍连续；
- 直接 append 到无换行结尾的文件时先清残片，不产生拼接坏行。
"""
import json

import pytest

from agent_test import EventType, Session, SessionEditError
from agent_test.types.messages import TextBlock, UserMessage

TORN = '{"seq": 1, "type": "user/mess'  # 模拟被截断的一行（无结尾换行）


def _user(text: str) -> UserMessage:
    return UserMessage(id=f"u-{text}", content=[TextBlock(content=text)])


def _rows(session: Session) -> list:
    """返回磁盘上可解析的事件行（残片若残留会在这里报错）。"""
    return [
        json.loads(line)
        for line in session.file_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _append_raw(session: Session, text: str) -> None:
    session.file_path.write_text(text, encoding="utf-8")


def test_from_file_tolerates_torn_tail(tmp_path):
    """末行无换行且解析失败 => 视为写一半：跳过并清理磁盘。"""
    session = Session(session_id="s-torn", persist_dir=str(tmp_path))
    session.append(EventType.USER_MESSAGE, data=_user("1"))
    _append_raw(session, session.file_path.read_text(encoding="utf-8") + TORN)

    restored = Session.from_file("s-torn", persist_dir=str(tmp_path))

    assert [e.seq for e in restored.events] == [0]
    assert [row["seq"] for row in _rows(restored)] == [0]  # 残片已清理
    restored.append(EventType.USER_MESSAGE, data=_user("2"))
    assert [row["seq"] for row in _rows(restored)] == [0, 1]


def test_from_file_still_raises_on_corrupt_middle_line(tmp_path):
    """中间的坏行（有换行结尾）不是 torn tail，仍抛 SessionEditError。"""
    session = Session(session_id="s-bad", persist_dir=str(tmp_path))
    session.append(EventType.USER_MESSAGE, data=_user("1"))
    session.append(EventType.USER_MESSAGE, data=_user("2"))
    lines = session.file_path.read_text(encoding="utf-8").splitlines()
    _append_raw(session, lines[0] + "\n{broken}\n" + lines[1] + "\n")

    with pytest.raises(SessionEditError):
        Session.from_file("s-bad", persist_dir=str(tmp_path))


def test_reload_tolerates_torn_tail_and_keeps_prefix(tmp_path):
    """reload 容忍 torn tail；清理残片时磁盘前缀（全量历史）必须保留。"""
    session = Session(session_id="s-torn2", persist_dir=str(tmp_path))
    session.append(EventType.USER_MESSAGE, data=_user("1"))
    session.append(EventType.USER_MESSAGE, data=_user("2"))
    last_index = session.mark_compacted(0, 1)
    session.insert_after(last_index, EventType.COMPACT, "摘要")
    prefix_row = _rows(session)[0]
    _append_raw(session, session.file_path.read_text(encoding="utf-8") + TORN)

    reloaded = Session(session_id="s-torn2", persist_dir=str(tmp_path)).reload()

    assert [e.seq for e in reloaded.events] == [1, 2]  # 摘要 + 未压缩事件
    rows = _rows(reloaded)
    assert [row["seq"] for row in rows] == [0, 1, 2]  # 残片已清理、seq 连续
    assert rows[0] == prefix_row  # 前缀原样保留


def test_append_trims_torn_tail_before_writing(tmp_path):
    """直接 append 到无换行结尾的文件：先清残片，不产生拼接坏行。"""
    session = Session(session_id="s-torn3", persist_dir=str(tmp_path))
    session.append(EventType.USER_MESSAGE, data=_user("1"))
    _append_raw(session, session.file_path.read_text(encoding="utf-8") + TORN)

    session.append(EventType.USER_MESSAGE, data=_user("2"))

    rows = _rows(session)
    assert [row["seq"] for row in rows] == [0, 1]
    assert rows[1]["data"]["id"] == "u-2"
    # 文件可被再次完整恢复：残片被清掉而不是留在中间
    restored = Session.from_file("s-torn3", persist_dir=str(tmp_path))
    assert [e.seq for e in restored.events] == [0, 1]