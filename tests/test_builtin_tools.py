"""内置 find / edit 工具的基础行为测试（本地离线）。"""
import os
from pathlib import Path

from agent_test.tools import edit, find, tool_center
from agent_test.tools.center import ToolCenter


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_builtin_tools_registered():
    schemas = {s["function"]["name"] for s in (tool_center.get_schemas() or [])}
    assert {"read", "find", "edit"} <= schemas


def test_find_by_name_keyword(tmp_path):
    _write(tmp_path / "alpha.py", "a = 1\n")
    _write(tmp_path / "beta.txt", "b = 2\n")
    _write(tmp_path / "sub" / "gamma.py", "c = 3\n")

    result = find(str(tmp_path), name_keyword=".py")
    assert result["error"] == ""
    paths = [m["path"] for m in result["matches"]]
    assert any(p.endswith("alpha.py") for p in paths)
    assert any(p.endswith(os.sep + "gamma.py") for p in paths)
    assert not any(p.endswith("beta.txt") for p in paths)


def test_find_by_content_pattern(tmp_path):
    target = _write(tmp_path / "main.py", "def main():\n    return 1\n")
    _write(tmp_path / "other.py", "x = 0\n")

    result = find(str(tmp_path), content_pattern=r"def main")
    assert result["error"] == ""
    assert len(result["matches"]) == 1
    hit = result["matches"][0]
    assert hit["path"] == str(target)
    assert hit["line"] == 1
    assert "def main" in hit["text"]


def test_find_missing_dir_returns_error():
    result = find("E:/no/such/dir-xyz")
    assert result["matches"] == []
    assert result["error"]


def test_find_invalid_regex_returns_error(tmp_path):
    _write(tmp_path / "a.py", "x\n")
    result = find(str(tmp_path), content_pattern="[")
    assert result["matches"] == []
    assert "无效正则" in result["error"]


def test_edit_unique_replace(tmp_path):
    file = _write(tmp_path / "a.py", "hello world\n")
    result = edit(str(file), "world", "agent")
    assert result["replaced"] == 1
    assert file.read_text(encoding="utf-8") == "hello agent\n"


def test_edit_absent_text_returns_error(tmp_path):
    file = _write(tmp_path / "a.py", "hello world\n")
    result = edit(str(file), "nope", "x")
    assert result["replaced"] == 0
    assert "未找到" in result["error"]
    assert file.read_text(encoding="utf-8") == "hello world\n"


def test_edit_ambiguous_requires_replace_all(tmp_path):
    file = _write(tmp_path / "a.py", "x = 1\nx = 2\n")
    result = edit(str(file), "x =", "y =")
    assert result["replaced"] == 0
    assert "2 处" in result["error"]

    result = edit(str(file), "x =", "y =", replace_all=True)
    assert result["replaced"] == 2
    assert file.read_text(encoding="utf-8") == "y = 1\ny = 2\n"


def test_edit_empty_old_text_rejected(tmp_path):
    file = _write(tmp_path / "a.py", "abc\n")
    result = edit(str(file), "", "x")
    assert result["replaced"] == 0
    assert file.read_text(encoding="utf-8") == "abc\n"


def test_edit_missing_file_returns_error():
    result = edit("E:/no/such/file-xyz.txt", "a", "b")
    assert result["replaced"] == 0
    assert "不存在" in result["error"]


def test_execute_via_tool_center(tmp_path):
    """find/edit 经 ToolCenter.execute 走通 JSON 序列化协议。"""
    file = _write(tmp_path / "a.py", "print('hi')\n")
    center = ToolCenter()
    from agent_test.tools.builtin import register_builtins

    register_builtins(center)

    import asyncio
    import json

    hit = asyncio.run(
        center.execute("find", {"path": str(tmp_path), "content_pattern": "print"})
    )
    payload = json.loads(hit["content"])
    assert hit["is_error"] is False
    assert payload["matches"][0]["path"] == str(file)

    changed = asyncio.run(
        center.execute("edit", {"file_path": str(file), "old_text": "hi", "new_text": "bye"})
    )
    payload = json.loads(changed["content"])
    assert payload["replaced"] == 1
    assert file.read_text(encoding="utf-8") == "print('bye')\n"
