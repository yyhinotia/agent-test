"""内置 grep 工具的基础行为测试（本地离线）。"""
import json
from pathlib import Path

from agent_test.tools import grep, tool_center


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _paths(result) -> list:
    return [m["path"] for m in result["matches"]]


def test_grep_registered():
    schemas = {s["function"]["name"] for s in (tool_center.get_schemas() or [])}
    assert "grep" in schemas


def test_grep_basic_hits_with_line_numbers(tmp_path):
    target = _write(
        tmp_path / "demo.py",
        "a = 1\n# TODO: fix\nb = 2\n# TODO: again\n",
    )
    result = grep(str(target), r"TODO")
    assert result["error"] == ""
    assert result["count"] == 2
    assert result["matches"] == [
        {"path": str(target), "line": 2, "text": "# TODO: fix"},
        {"path": str(target), "line": 4, "text": "# TODO: again"},
    ]
    assert result["truncated"] is False


def test_grep_offset_limit_pagination(tmp_path):
    lines = "\n".join(f"item {i}" for i in range(10))
    target = _write(tmp_path / "data.txt", lines)

    first = grep(str(target), r"item", offset=0, limit=3)
    assert first["count"] == 3
    assert first["truncated"] is True
    assert [m["line"] for m in first["matches"]] == [1, 2, 3]

    second = grep(str(target), r"item", offset=3, limit=3)
    assert second["count"] == 3
    assert [m["line"] for m in second["matches"]] == [4, 5, 6]

    last = grep(str(target), r"item", offset=9, limit=10)
    assert last["count"] == 1
    assert [m["line"] for m in last["matches"]] == [10]
    assert last["truncated"] is False


def test_grep_offset_beyond_all_returns_empty(tmp_path):
    target = _write(tmp_path / "a.txt", "x\nx\n")
    result = grep(str(target), "x", offset=5, limit=2)
    assert result["count"] == 0
    assert result["matches"] == []
    assert result["truncated"] is False


def test_grep_directory_recursive_cross_file_paging(tmp_path):
    f1 = _write(tmp_path / "a.py", "hit 1\nnope\n")
    f2 = _write(tmp_path / "sub" / "b.py", "nope\nhit 2\nhit 3\n")

    result = grep(str(tmp_path), "hit", offset=1, limit=2)
    # 跨文件累计：窗口 [1,3) -> b.py 的 hit2 / hit3
    assert result["count"] == 2
    assert result["matches"] == [
        {"path": str(f2), "line": 2, "text": "hit 2"},
        {"path": str(f2), "line": 3, "text": "hit 3"},
    ]

    again = grep(str(tmp_path), "hit", offset=1, limit=2)
    # 目录遍历按字典序排序，翻页结果稳定
    assert again["matches"] == result["matches"]


def test_grep_ignores_noise_dirs(tmp_path):
    _write(tmp_path / "real.py", "needle\n")
    _write(tmp_path / ".venv" / "lib.py", "needle\n")
    _write(tmp_path / "__pycache__" / "cache.py", "needle\n")

    result = grep(str(tmp_path), "needle")
    paths = _paths(result)
    assert len(paths) == 1
    assert paths[0].endswith("real.py")


def test_grep_long_line_truncated(tmp_path):
    long_line = "x" * 500
    target = _write(tmp_path / "long.txt", long_line)

    result = grep(str(target), "x")
    assert result["count"] == 1
    text = result["matches"][0]["text"]
    assert len(text) <= 200
    assert text.endswith("...")
    assert text.startswith("x" * (200 - 3))


def test_grep_case_sensitivity(tmp_path):
    target = _write(tmp_path / "a.py", "Hello World\n")

    insensitive = grep(str(target), "hello")
    assert insensitive["count"] == 1

    sensitive = grep(str(target), "hello", case_sensitive=True)
    assert sensitive["count"] == 0


def test_grep_invalid_regex_returns_error(tmp_path):
    _write(tmp_path / "a.py", "x\n")
    result = grep(str(tmp_path), "[")
    assert result["matches"] == []
    assert "无效正则" in result["error"]


def test_grep_missing_path_returns_error():
    result = grep("E:/no/such/dir-xyz", "x")
    assert result["matches"] == []
    assert "路径不存在" in result["error"]


def test_grep_invalid_offset_limit_returns_error(tmp_path):
    _write(tmp_path / "a.py", "x\n")
    result = grep(str(tmp_path), "x", offset=-1)
    assert "offset" in result["error"]
    result = grep(str(tmp_path), "x", limit=0)
    assert "limit" in result["error"]


def test_grep_skips_unreadable_file_but_keeps_others(tmp_path):
    _write(tmp_path / "ok.txt", "needle\n")
    bad = tmp_path / "bad.bin"
    bad.write_bytes(b"\xff\xfe\x00\x01needle")

    result = grep(str(tmp_path), "needle")
    assert len(_paths(result)) == 1
    assert "bad.bin" in result["error"]


def test_grep_via_tool_center(tmp_path):
    """grep 经 ToolCenter.execute 走通 JSON 序列化协议。"""
    import asyncio

    target = _write(tmp_path / "a.py", "print('hi')\nprint('hi')\n")

    async def _run():
        return await tool_center.execute(
            "grep", {"path": str(target), "pattern": "hi", "limit": 1}
        )

    out = asyncio.run(_run())
    payload = json.loads(out["content"])
    assert out["is_error"] is False
    assert payload["count"] == 1
    assert payload["matches"][0]["path"] == str(target)
    assert payload["matches"][0]["line"] == 1
