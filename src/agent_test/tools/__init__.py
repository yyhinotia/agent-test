"""工具模块：工具中心 + 内置工具 + 全局单例。

注意：目录浏览工具的函数名是 list_dir，注册名是 list
（避免遮蔽 Python 内置 list）。
"""
from agent_test.tools.builtin import (
    edit,
    find,
    grep,
    list_dir,
    read,
    register_builtins,
    tool_center,
    write,
)
from agent_test.tools.bash import bash, register_bash
from agent_test.tools.center import ToolCenter

__all__ = [
    "tool_center",
    "ToolCenter",
    "bash",
    "register_bash",
    "register_builtins",
    "read",
    "find",
    "edit",
    "grep",
    "list_dir",
    "write",
]