"""工作目录检测：解析请求的工作目录并校验其位于允许工作区（沙箱根）内。

与具体工具解耦：bash / 未来 write / 删除类工具都可通过本模块做
“工作目录检测”，产出 CwdCheck 供 policy 决策或直接返回给调用方。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class CwdCheck:
    """工作目录检测结果。ok=False 时 error 给出可读原因。"""

    ok: bool
    cwd: Path | None = None
    requested: str | None = None
    error: str = ""
    note: str = ""


def is_within(child: Path, root: Path) -> bool:
    """child 是否位于 root 之内（含相等，按真实路径比较）。"""
    try:
        child.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def resolve_workdir(
    requested: str | None,
    *,
    allowed_roots: Sequence[Path],
    default_cwd: Path,
) -> CwdCheck:
    """解析命令工作目录。

    - requested 为空：返回 default_cwd（调用方/策略默认目录）；
    - requested 为相对路径：相对 default_cwd 解析；
    - 目录必须存在且为目录；
    - 解析后的绝对路径必须位于某个 allowed_root 之内（工作区越界检测）。
    """
    base = default_cwd.resolve()
    if requested is None or requested == "":
        target = base
    else:
        raw = Path(requested).expanduser()
        target = raw if raw.is_absolute() else (base / raw)
    if not target.exists():
        return CwdCheck(False, requested=requested, error=f"工作目录不存在: {requested}")
    if not target.is_dir():
        return CwdCheck(False, requested=requested, error=f"工作目录不是目录: {requested}")
    resolved = target.resolve()
    roots = [Path(r).resolve() for r in allowed_roots]
    if roots and not any(is_within(resolved, root) for root in roots):
        return CwdCheck(
            False,
            requested=requested,
            error=(
                f"工作目录超出允许工作区: {resolved}"
                f"（允许根: {', '.join(str(r) for r in roots)}）"
            ),
        )
    note = f"cwd 解析为 {resolved}"
    if requested:
        note += f"（来自请求 {requested!r}）"
    return CwdCheck(True, cwd=resolved, requested=requested, note=note)