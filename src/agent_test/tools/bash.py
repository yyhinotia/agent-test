"""Bash 工具：异步执行一条 shell/cmd 命令，捕获成功/错误回显。

设计
----
- **执行层保持纯净**：bash() 不做权限判断——审批/危险检测/工作目录
  检测由 agent_test.policy 在 ToolCenter.execute 硬闸门与 Agent
  pre_step 阶段统一拦截；本函数只负责解析并校验工作目录、运行命令、
  收集 stdout/stderr/退出码/超时；
- **跨平台**：Windows 走 cmd.exe（COMSPEC），POSIX 走 /bin/sh，
  所以工具名叫 bash、语义是“执行一条命令”；
- **平台感知解码**：输出编码缺省按平台自动选——POSIX 用 utf-8，Windows
  取控制台输出代码页（`chcp 65001` -> utf-8，默认中文控制台 -> cp936/gbk）；
  自动模式下首选解码出现替换字符时再按候选兜底，避免「GBK 回显按 UTF-8
  解码」或反过来的乱码；显式传 `encoding=` 则以参数为准；
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
import codecs
import ctypes
import locale
import os
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
        "description": (
            "输出解码编码；缺省按平台自动选择（Windows 取控制台/系统代码页，"
            "中文系统为 cp936/gbk；POSIX 为 utf-8），只在需要强制指定时传入"
        ),
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


def _windows_codepages() -> list[int]:
    """Windows 代码页探测：控制台输出 CP -> OEM CP -> ANSI CP。"""
    try:
        kernel32 = ctypes.windll.kernel32
        return [
            int(kernel32.GetConsoleOutputCP()),
            int(kernel32.GetOEMCP()),
            int(kernel32.GetACP()),
        ]
    except Exception:  # noqa: BLE001 - 非 Windows / 无 ctypes：交给 locale 兜底
        return []


def _codepage_codec(codepage: int) -> str | None:
    """代码页号 -> Python 编码名（未知/无效返回 None）。"""
    if codepage <= 0:
        return None
    if codepage == 65001:  # UTF-8 代码页：cp65001 是 utf-8 的别名，显式写出更好读
        return "utf-8"
    name = f"cp{codepage}"
    try:
        codecs.lookup(name)
    except LookupError:
        return None
    return name


def _platform_encodings() -> list[str]:
    """当前平台的输出解码候选（首个为默认，其余用于自动兜底）。

    实测（中文 Windows）：`chcp 65001` 的控制台里 cmd 内置命令输出 UTF-8，
    `chcp 936`（默认）输出 GBK，而本进程的 locale 始终报 cp936——所以必须
    优先读**控制台输出代码页**，不能用 locale 猜。POSIX 固定 utf-8。
    """
    if os.name != "nt":
        return ["utf-8"]
    names: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        """按正则编码名去重（cp936 与 gbk 是同一个编解码器）。"""
        try:
            canonical = codecs.lookup(name).name
        except LookupError:
            return
        if canonical in seen:
            return
        seen.add(canonical)
        names.append(name)

    for codepage in _windows_codepages():
        name = _codepage_codec(codepage)
        if name:
            add(name)
    add("utf-8")
    try:
        add(locale.getpreferredencoding(False))
    except Exception:  # noqa: BLE001
        pass
    return names or ["utf-8"]


def default_encoding() -> str:
    """bash 的缺省输出编码（平台感知，可用 encoding 参数覆盖）。"""
    return _platform_encodings()[0]


def _resolve_encoding(
    payloads: list[bytes], primary: str, candidates: list[str] | None
) -> str:
    """选出一个能把所有输出流都解干净的编码（无替换字符）。

    自动模式（candidates 非空）下按候选顺序尝试，优先 primary：控制台报
    UTF-8 而子进程吐 GBK（或反之）时，能自动切到对的那个，而不是留下一串
    `\ufffd`。显式指定（candidates=None）时不参与兜底，直接返回 primary，
    编码名拼错由调用方按 LookupError 降级为 ToolExecutionError。

    局限：单字节代码页（cp1252/cp437 等）对任意字节都不产生替换字符，
    该启发式判别不了它们之间的乱码；本项目目标环境是中文 Windows
    （控制台 936 与 65001 两个方向都能判别）。
    """
    if not candidates:
        return primary
    for name in [primary] + [c for c in candidates if c != primary]:
        try:
            if all("\ufffd" not in payload.decode(name) for payload in payloads):
                return name
        except (UnicodeDecodeError, LookupError):
            continue
    return primary


async def bash(
    command: str,
    workdir: str | None = None,
    timeout: int = 60,
    encoding: str | None = None,
    max_output_chars: int = 20000,
) -> Dict[str, Any]:
    """异步执行一条命令并返回回显。

    返回 dict：command / cwd / exit_code / stdout / stderr / timed_out /
              truncated / encoding（本次实际使用的解码编码）。
    encoding 缺省（None）按平台自动选择并在候选间兜底；显式传入则以参数
    为准（拼错编码抛 ToolExecutionError）。
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
            # stdin 隔离：子进程不继承父进程 stdin，避免与
            # StdinDispatcher 竞争同一终端行缓冲区（知识增量）。
            stdin=asyncio.subprocess.DEVNULL,
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
            try:
                # 超时保护：kill 后 communicate 仍可能挂起（子进程未回收），
                # 再兜一层 wait_for，避免整个 Agent 卡死在工具调用里。
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=5
                )
            except asyncio.TimeoutError:
                stdout_b, stderr_b = b"", b""
    except OSError as exc:
        raise ToolExecutionError(
            f"命令启动失败: {exc}",
            location="BashTool.bash",
            detail={"command": command[:_LOG_TRUNCATE], "cwd": str(cwd)},
        ) from exc

    auto_encoding = encoding is None
    used_encoding = _resolve_encoding(
        [stdout_b, stderr_b],
        default_encoding() if auto_encoding else encoding,
        _platform_encodings() if auto_encoding else None,
    )
    try:
        stdout_text = stdout_b.decode(used_encoding, errors="replace")
        stderr_text = stderr_b.decode(used_encoding, errors="replace")
    except LookupError as exc:
        raise ToolExecutionError(
            f"未知编码: {used_encoding}",
            location="BashTool.bash",
            detail={"encoding": used_encoding},
        ) from exc

    stdout_show, stdout_cut = _truncate(stdout_text, max_output_chars)
    stderr_show, stderr_cut = _truncate(stderr_text, max_output_chars)
    exit_code = proc.returncode if proc.returncode is not None else -1
    RuntimeLog.info(
        "bash 完成 | exit=%s timed_out=%s cwd=%s encoding=%s "
        "stdout_chars=%s stderr_chars=%s",
        exit_code,
        timed_out,
        str(cwd),
        used_encoding,
        len(stdout_text),
        len(stderr_text),
    )
    return {
        "command": command,
        "cwd": str(cwd),
        "exit_code": exit_code,
        "encoding": used_encoding,
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