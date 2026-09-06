"""bash 工具执行层测试：回显 / 退出码 / 工作目录 / 超时 / 参数错误。"""
import asyncio
import sys
from pathlib import Path

import pytest

from agent_test.exceptions.tools import ToolExecutionError
from agent_test.tools import tool_center
from agent_test.tools.bash import bash, register_bash
from agent_test.tools.center import ToolCenter

IS_WIN = sys.platform.startswith("win")


def test_bash_registered_in_global_center():
    names = {s["function"]["name"] for s in (tool_center.get_schemas() or [])}
    assert "bash" in names


def test_register_bash_schema():
    center = ToolCenter()
    register_bash(center)
    names = {s["function"]["name"] for s in (center.get_schemas() or [])}
    assert "bash" in names


def test_bash_captures_stdout():
    result = asyncio.run(bash("echo hello-agent-echo"))
    assert result["exit_code"] == 0
    assert "hello-agent-echo" in result["stdout"]
    assert result["timed_out"] is False
    assert result["stderr"] == "" or result["stderr"] is None


def test_bash_captures_stderr_and_exit_code():
    cmd = (
        "echo err-line-agent 1>&2 & exit /b 7"
        if IS_WIN
        else "echo err-line-agent 1>&2; exit 7"
    )
    result = asyncio.run(bash(cmd))
    assert result["exit_code"] == 7
    assert "err-line-agent" in result["stderr"]


def test_bash_workdir_detection(tmp_path):
    code = "import os;print(os.getcwd())"
    cmd = f'"{sys.executable}" -c "{code}"'
    result = asyncio.run(bash(cmd, workdir=str(tmp_path)))
    assert result["exit_code"] == 0
    assert Path(result["stdout"].strip()).resolve() == tmp_path.resolve()
    assert result["cwd"] == str(tmp_path.resolve())


def test_bash_timeout(tmp_path):
    cmd = "ping -n 4 127.0.0.1 >nul" if IS_WIN else "sleep 3"
    result = asyncio.run(bash(cmd, timeout=1, workdir=str(tmp_path)))
    assert result["timed_out"] is True


def test_bash_invalid_workdir_raises(tmp_path):
    with pytest.raises(ToolExecutionError):
        asyncio.run(bash("echo hi", workdir=str(tmp_path / "no-such-dir")))


def test_bash_file_as_workdir_raises(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(ToolExecutionError):
        asyncio.run(bash("echo hi", workdir=str(target)))


def test_bash_empty_command_rejected():
    with pytest.raises(ToolExecutionError):
        asyncio.run(bash("   "))


def test_bash_unknown_encoding_rejected():
    with pytest.raises(ToolExecutionError):
        asyncio.run(bash("echo hi", encoding="no-such-codec"))


def test_bash_truncates_huge_output():
    cmd = (
        "for /l %i in (1,1,500) do @echo line-%i"
        if IS_WIN
        else "for i in $(seq 1 500); do echo line-$i; done"
    )
    result = asyncio.run(bash(cmd, max_output_chars=200))
    assert result["exit_code"] == 0
    assert result["truncated"] is True
    assert len(result["stdout"]) <= 300