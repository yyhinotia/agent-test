"""内置 list / write 工具的基础行为测试（本地离线）。"""
import json
from pathlib import Path

from agent_test.tools import tool_center, write
from agent_test.tools.builtin import list_dir


def test_list_registered_as_name():
    """函数名 list_dir，注册名 list（与内置 list 解耦）。"""
    schemas = {s["function"]["name"] for s in (tool_center.get_schemas() or [])}
    assert "list" in schemas
    assert "write" in schemas


def _mkdir_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _build_tree(tmp_path: Path) -> None:
    _mkdir_write(tmp_path / "a.py", "a\n")
    (tmp_path / "b").mkdir()
    _mkdir_write(tmp_path / "b" / "c.py", "c\n")
    (tmp_path / "b" / "sub").mkdir()
    _mkdir_write(tmp_path / "b" / "sub" / "d.txt", "d\n")
    _mkdir_write(tmp_path / "node_modules" / "x.js", "x\n")
    _mkdir_write(tmp_path / "__pycache__" / "y.pyc", "y")
    _mkdir_write(tmp_path / ".venv" / "lib.py", "z\n")


def test_list_dir_depth_one(tmp_path):
    _build_tree(tmp_path)
    result = list_dir(str(tmp_path), depth=1)
    assert result["error"] == ""
    lines = result["content"].splitlines()
    assert "a.py" in lines
    assert "b/" in lines
    # depth=1 不展开子目录内部
    assert not any("c.py" in line for line in lines)
    # 噪音目录与隐藏目录被跳过
    assert not any("node_modules" in line for line in lines)
    assert not any("__pycache__" in line for line in lines)
    assert not any(".venv" in line for line in lines)


def test_list_dir_depth_recursive(tmp_path):
    _build_tree(tmp_path)
    result = list_dir(str(tmp_path), depth=3)
    lines = result["content"].splitlines()
    assert "b/" in lines
    assert any("  c.py" in line for line in lines)
    assert any("  sub/" in line for line in lines)
    assert any("    d.txt" in line for line in lines)


def test_list_dir_show_hidden(tmp_path):
    _build_tree(tmp_path)
    result = list_dir(str(tmp_path), show_hidden=True)
    lines = result["content"].splitlines()
    # .venv 同时属于噪音目录（_IGNORED_DIRS），show_hidden 也不会显示
    assert not any(".venv" in line for line in lines)
    # 普通隐藏文件（非噪音目录成员）会显示
    (tmp_path / ".env.example").write_text("k\n", encoding="utf-8")
    result = list_dir(str(tmp_path), show_hidden=True)
    lines = result["content"].splitlines()
    assert any(".env.example" in line for line in lines)
    # 默认不显示隐藏文件
    result = list_dir(str(tmp_path))
    assert not any(".env.example" in line for line in result["content"].splitlines())


def test_list_dir_sorted_stable(tmp_path):
    _build_tree(tmp_path)
    r1 = list_dir(str(tmp_path)).get("content")
    r2 = list_dir(str(tmp_path)).get("content")
    assert r1 == r2
    # 字典序：a.py 在 b/ 之前
    assert r1.splitlines() == sorted(r1.splitlines())


def test_list_dir_missing_dir_returns_error():
    result = list_dir("E:/no/such/dir-xyz")
    assert result["content"] == ""
    assert result["error"]


def test_list_dir_accepts_file_path_error(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x\n", encoding="utf-8")
    result = list_dir(str(f))
    assert result["error"]


def test_write_create_new_file(tmp_path):
    target = tmp_path / "new" / "deep" / "a.py"  # 父目录不存在
    result = write(str(target), "print('hi')\n")
    assert result["written"] > 0
    assert target.read_text(encoding="utf-8") == "print('hi')\n"


def test_write_overwrite_existing(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("old\n", encoding="utf-8")
    result = write(str(target), "new content")
    assert target.read_text(encoding="utf-8") == "new content"
    assert result["written"] == len("new content".encode("utf-8"))


def test_write_to_dir_returns_error(tmp_path):
    result = write(str(tmp_path), "x")
    assert result["written"] == 0
    assert "目录" in result["error"]


def test_write_bad_encoding_returns_error(tmp_path):
    target = tmp_path / "a.txt"
    result = write(str(target), "中文", encoding="ascii")
    assert result["written"] == 0
    assert "编码" in result["error"]
    assert not target.exists()


def test_write_via_tool_center(tmp_path):
    """write/list 经 ToolCenter.execute 走通 JSON 序列化协议（list 用注册名）。"""
    import asyncio

    target = tmp_path / "gen.py"

    async def _run_write():
        return await tool_center.execute(
            "write", {"file_path": str(target), "content": "value = 42\n"}
        )

    async def _run_list():
        return await tool_center.execute(
            "list", {"path": str(tmp_path), "depth": 1}
        )

    out = asyncio.run(_run_write())
    assert out["is_error"] is False
    payload = json.loads(out["content"])
    assert payload["written"] == len("value = 42\n".encode("utf-8"))

    out = asyncio.run(_run_list())
    assert out["is_error"] is False
    payload = json.loads(out["content"])
    assert "gen.py" in payload["content"]
