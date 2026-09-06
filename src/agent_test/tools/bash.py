"""Bash 工具：异步执行一条 shell/cmd 命令，捕获成功/错误回显。

设计
----
- **执行层保持纯净**：bash() 不做权限判断——审批/危险检测/工作目录
  检测由 agent_test.policy 在 ToolCenter.execute 硬闸门与 Agent
  pre_step 阶段统一拦截；本函数只负责解析并校验工作目录、运行命令、
  收集 stdout/stderr/退出码/超时；
- **跨平台**：Windows 走 cmd.exe（COMSPEC），POSIX 走 /bin/sh，
  所以工具名叫 bash、语义是“执行一条命令”；
- **回显**：stdout（成功回显）与 stderr（err 回显）分开返回，并附
  exit_code / timed_out / 解析后的 cwd，供 LLM 与 session 回放使用；
- **审计日志**：每次执行把 command / cwd / exit_code / 输出长度写入
  logs/runtime.log（RuntimeLog），命令体与输出在日志中截断存储；
- **参数级错误**（空命令、非法工作目录、未知编码、启动失败）抛
  ToolExecutionError，由 ToolCenter.execute 统一降级为 is_error=True
  的工具结果，完整堆栈进日志文件。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict

from agent_test.exceptions.tools import ToolExecutionError
from agent_test.log.runtime_log import RuntimeLog
from agent_test.tools.center import ToolCenter

_BASH_PARAMETERS: Dict[str, Any] = {
    "command": {
        "type": "string",
        "description": "要执行的完整命令（一条 shell/cmd 命令）",
    },
    "workdir": {
        "type": "string",
        "description": "可选：命令工作目录（缺省用策略默认/允许根目录）",
    },
    "timeout": {
        "type": "integer",
        "description": "超时秒数，默认 60；超时返回 timed_out=True",
    },
    "encoding": {
        "type": "string",
        "description": "输出解码编码，默认 utf-8",
    },
    "max_output_chars": {
        "type": "integer",
        "description": "单流（stdout/stderr）最多返回字符数，默认 20000",
    },
}

_LOG_TRUNCATE = 1000


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    """截断过长的输出，返回 (文本, 是否截断)。"""
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n...[已截断，共 {len(text)} 字符]", True


async def bash(
    command: str,
    workdir: str | None = None,
    timeout: int = 60,
    encoding: str = "utf-8",
    max_output_chars: int = 20000,
) -> Dict[str, Any]:
    """异步执行一条命令并返回回显。

    返回 dict：command / cwd / exit_code / stdout / stderr /
              timed_out / truncated。
    参数级错误（空命令、工作目录非法、未知编码、启动失败）抛
    ToolExecutionError。
    """
    if not command or not command.strip():
        raise ToolExecutionError(
            "bash 命令为空",
            location="BashTool.bash",
            detail={"workdir": workdir},
        )

    # 工作目录基础检测（存在性/类型）；越界等策略判断在 pre_step 完成
    cwd: Path
    if workdir:
        requested = Path(workdir).expanduser()
        if not requested.is_absolute():
            requested = (Path.cwd() / requested).resolve()
        if not requested.exists():
            raise ToolExecutionError(
                f"工作目录不存在: {workdir}",
                location="BashTool.bash",
                detail={"workdir": workdir},
            )
        if not requested.is_dir():
            raise ToolExecutionError(
                f"工作目录不是目录: {workdir}",
                location="BashTool.bash",
                detail={"workdir": workdir},
            )
        cwd = requested.resolve()
    else:
        cwd = Path.cwd().resolve()

    RuntimeLog.info(
        "bash 执行 | cmd=%s cwd=%s timeout=%s",
        command[:_LOG_TRUNCATE],
        str(cwd),
        timeout,
    )
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            timed_out = False
        except asyncio.TimeoutError:
            timed_out = True
            proc.kill()
            stdout_b, stderr_b = await proc.communicate()
    except OSError as exc:
        raise ToolExecutionError(
            f"命令启动失败: {exc}",
            location="BashTool.bash",
            detail={"command": command[:_LOG_TRUNCATE], "cwd": str(cwd)},
        ) from exc

    try:
        stdout_text = stdout_b.decode(encoding, errors="replace")
        stderr_text = stderr_b.decode(encoding, errors="replace")
    except LookupError as exc:
        raise ToolExecutionError(
            f"未知编码: {encoding}",
            location="BashTool.bash",
            detail={"encoding": encoding},
        ) from exc

    stdout_show, stdout_cut = _truncate(stdout_text, max_output_chars)
    stderr_show, stderr_cut = _truncate(stderr_text, max_output_chars)
    exit_code = proc.returncode if proc.returncode is not None else -1
    RuntimeLog.info(
        "bash 完成 | exit=%s timed_out=%s cwd=%s stdout_chars=%s stderr_chars=%s",
        exit_code,
        timed_out,
        str(cwd),
        len(stdout_text),
        len(stderr_text),
    )
    return {
        "command": command,
        "cwd": str(cwd),
        "exit_code": exit_code,
        "stdout": stdout_show,
        "stderr": stderr_show,
        "timed_out": timed_out,
        "truncated": stdout_cut or stderr_cut,
    }


def register_bash(center: ToolCenter, *, name: str = "bash") -> None:
    """把 bash 命令执行工具注册到指定 ToolCenter。"""
    center.register(
        desc=(
            "命令执行工具：执行一条 shell/cmd 命令，返回 stdout/stderr/"
            "退出码（受策略审批与危险/工作目录检测管控）"
        ),
        parameters=_BASH_PARAMETERS,
        required=["command"],
        name=name,
    )(bash)