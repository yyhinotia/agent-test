"""命令执行策略：权限审批 + 危险检测 + 工作目录检测的统一决策层。

设计：策略是“跨工具的 pre_step 拦截”，与具体工具解耦：
- ToolCenter.execute 内嵌策略硬闸门（防止绕过，任何执行入口都被拦截）；
- ReactAgent._step 的 pre_step 阶段先对整步所有工具调用做一次
  “全量预检”（决策先于副作用，避免先执行一部分再拦截另一部分）；
- bash 是第一个接入的高危工具，后续 write/删除/网络类工具可复用本层。

决策动作（PolicyAction）：
- EXECUTE           放行（含命中已审批 allowlist 前缀）
- REQUIRE_APPROVAL  需要人工/上级审批（默认不执行）
- DENY              直接拒绝（破坏性 / 越界）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Sequence

from agent_test.policy.cwd import CwdCheck, resolve_workdir
from agent_test.policy.rules import (
    RiskSeverity,
    analyze_command,
    highest_severity,
)

# 匹配 Windows 绝对路径（C:\...）与类 Unix 绝对路径（/a/b/c）
_PATH_PATTERN = re.compile(
    r"[A-Za-z]:[\\/][^\s\"'|;&<>]+|/(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+"
)

# 虚拟路径 / 设备文件：没有真实工作区语义，不参与「工作区之外」判定，
# 否则 /dev/null、nul 这类重定向目标会把无害命令误判为越界需审批。
_VIRTUAL_PATHS = frozenset(
    {
        "/dev/null",
        "/dev/zero",
        "/dev/stdin",
        "/dev/stdout",
        "/dev/stderr",
        "/dev/random",
        "/dev/urandom",
        "/dev/tty",
        "nul",
        "con",
        "aux",
        "prn",
    }
)


def _is_virtual_path(token: str) -> bool:
    """虚拟路径/设备文件判定（大小写不敏感，含 Windows 保留名）。"""
    lowered = token.lower()
    if lowered in _VIRTUAL_PATHS:
        return True
    return Path(token).name.lower() in _VIRTUAL_PATHS


class PolicyAction(str, Enum):
    EXECUTE = "execute"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


@dataclass(frozen=True)
class PolicyDecision:
    """一次工具调用的策略决策（供 pre_step / ToolCenter 消费）。"""

    action: PolicyAction
    tool_name: str
    reasons: tuple[str, ...] = ()
    detail: dict = field(default_factory=dict)

    def to_message(self) -> str:
        """转成可回传给 LLM / 写入 tool/result 的可读消息。"""
        if self.action is PolicyAction.DENY:
            head = f"工具调用被策略拒绝：{self.tool_name}"
        elif self.action is PolicyAction.REQUIRE_APPROVAL:
            head = f"工具调用需要审批（当前未授权执行）：{self.tool_name}"
        else:
            head = f"工具调用已放行：{self.tool_name}"
        lines = [head]
        lines.extend(f"- {reason}" for reason in self.reasons)
        if self.detail:
            lines.append(f"detail: {self.detail}")
        return "\n".join(lines)


class CommandPolicy:
    """命令策略：以允许工作区为沙箱边界，静态规则 + 审批前缀做决策。"""

    def __init__(
        self,
        *,
        allowed_roots: Sequence[str | Path] | None = None,
        allowlist_prefixes: Sequence[Sequence[str]] | None = None,
        detect_outside_paths: bool = True,
    ) -> None:
        roots = [Path(p) for p in (allowed_roots or [Path.cwd()])]
        self.allowed_roots: list[Path] = [r.resolve() for r in roots]
        self.allowlist_prefixes: list[tuple[str, ...]] = [
            tuple(p) for p in (allowlist_prefixes or [])
        ]
        self.detect_outside_paths = detect_outside_paths

    # ---------- 工作目录 ----------

    def resolve_workdir(
        self, requested: str | None = None, default_cwd: Path | None = None
    ) -> CwdCheck:
        """工作目录检测：解析 + 存在性/类型 + 工作区越界。"""
        base = default_cwd or (
            self.allowed_roots[0] if self.allowed_roots else Path.cwd()
        )
        return resolve_workdir(
            requested, allowed_roots=self.allowed_roots, default_cwd=base
        )

    # ---------- 决策 ----------

    def decide(
        self,
        command: str,
        *,
        tool_name: str = "bash",
        workdir: str | None = None,
        default_cwd: Path | None = None,
    ) -> PolicyDecision:
        """对一条命令做完整决策：工作目录检测 -> allowlist -> 静态规则 -> 越界。

        越界的两种形态都是「保守要求审批」而不是直接拒绝：命令里出现工作区
        之外的文件路径，或 workdir 指向允许工作区之外的目录（后者批准后可
        经 add_allowed_root 记住）。
        """
        detail: dict = {"tool": tool_name, "command": command}

        # 1) 工作目录检测（含工作区越界）
        check = self.resolve_workdir(workdir, default_cwd=default_cwd)
        detail["cwd"] = str(check.cwd) if check.ok else None
        detail["requested_workdir"] = workdir
        if not check.ok and not check.outside:
            # 不存在 / 不是目录：不可审批的参数错误，直接拒绝
            return PolicyDecision(
                PolicyAction.DENY,
                tool_name,
                reasons=(f"工作目录检测失败: {check.error}",),
                detail=detail,
            )

        if check.ok:
            reasons: list[str] = [f"工作目录检测: {check.note}"]
        else:
            # 工作目录越界：语义对齐「工作区之外的文件路径」——目录本身可用，
            # 只是超出沙箱，保守要求审批；批准后由 _approve 记入允许根，
            # 本会话内同一目录不再逐条打扰。
            detail["outside_workdir"] = str(check.cwd)
            reasons = [f"工作目录超出允许工作区，需审批: {check.cwd}"]

        # 2) 已审批命令前缀（allowlist）-> 直接放行
        if self._match_allowlist(command):
            reasons.append("命中已审批命令前缀 allowlist，跳过逐条审批")
            return PolicyDecision(
                PolicyAction.EXECUTE, tool_name, tuple(reasons), detail
            )

        # 3) 静态风险检测
        findings = analyze_command(command)
        for finding in findings:
            reasons.append(finding.format())
        severity = highest_severity(findings)

        # 4) 越界检测（保守：越界至少要求审批）——工作区外的文件路径，
        #    以及超出允许工作区的工作目录
        outside = self._outside_paths(command)
        if self.detect_outside_paths and outside and severity is None:
            reasons.append(
                "命令涉及允许工作区之外的路径，需审批: "
                + ", ".join(sorted(outside)[:5])
            )
            severity = RiskSeverity.APPROVE
        if detail.get("outside_workdir") and severity is None:
            severity = RiskSeverity.APPROVE

        if severity is RiskSeverity.DENY:
            return PolicyDecision(
                PolicyAction.DENY, tool_name, tuple(reasons), detail
            )
        if severity is RiskSeverity.APPROVE:
            return PolicyDecision(
                PolicyAction.REQUIRE_APPROVAL, tool_name, tuple(reasons), detail
            )
        return PolicyDecision(
            PolicyAction.EXECUTE, tool_name, tuple(reasons), detail
        )

    def decide_tool(
        self,
        tool_name: str,
        args: dict,
        *,
        default_cwd: Path | None = None,
    ) -> PolicyDecision:
        """按工具分派策略。

        目前高危工具为 bash；其余工具默认放行。后续给 write/edit 等
        增加文件路径越界检测时在本方法扩展即可，Agent/ToolCenter 无需改动。
        """
        if tool_name == "bash":
            command = str(args.get("command", ""))
            return self.decide(
                command,
                tool_name=tool_name,
                workdir=args.get("workdir"),
                default_cwd=default_cwd,
            )
        return PolicyDecision(PolicyAction.EXECUTE, tool_name)

    def add_allowlist_prefix(self, prefix: Sequence[str]) -> None:
        """添加一条已审批命令前缀（会话级 remember）。"""
        key = tuple(prefix)
        if key and key not in self.allowlist_prefixes:
            self.allowlist_prefixes.append(key)

    def add_allowlist_for_command(self, command: str) -> None:
        """把命令的“审批记忆前缀”加入 allowlist。

        默认取命令前两个 token（如 git push origin main ->
        ["git", "push"]），与 Claude Code “approve and remember” /
        Codex “allow-and-remember” 的会话级放行语义一致：
        同类命令后续不再逐条打扰用户。
        """
        tokens = command.strip().split()
        if not tokens:
            return
        prefix = tokens if len(tokens) < 2 else tokens[:2]
        self.add_allowlist_prefix(prefix)

    def add_allowed_root(self, path: str | Path) -> bool:
        """把一条已审批的越界路径加入允许根（会话级记忆）。

        返回是否新增：已位于现有允许根之内（含相等）时不重复添加。
        """
        try:
            resolved = Path(path).expanduser().resolve()
        except OSError:
            return False
        if any(_is_inside(resolved, root) for root in self.allowed_roots):
            return False
        self.allowed_roots.append(resolved)
        return True

    def remember_approval(self, command: str, detail: dict | None = None) -> None:
        """记住一次已获准的调用：命令前缀 + 越界路径 + 越界工作目录。

        审批放行后的统一记忆入口，Agent 的 _pre_step（core/agent._approve）
        与 ToolCenter.execute 的直接审批路径共用，避免两处逻辑漂移。
        """
        if command:
            self.add_allowlist_for_command(command)
            self.add_outside_paths_for_command(command)
        outside_workdir = (detail or {}).get("outside_workdir")
        if outside_workdir:
            self.add_allowed_root(outside_workdir)

    def add_outside_paths_for_command(self, command: str) -> list[Path]:
        """把命令中本次已审批的越界路径纳入允许根。

        语义与命令前缀 allowlist 对齐：用户批准过一次
        `type C:\\Windows\\win.ini`，后续涉及同一路径的命令不再逐条审批。
        """
        added: list[Path] = []
        for token in sorted(self._outside_paths(command)):
            resolved = Path(token).expanduser().resolve()
            if self.add_allowed_root(resolved):
                added.append(resolved)
        return added

    # ---------- 内部实现 ----------

    def _match_allowlist(self, command: str) -> bool:
        """命令头部 token 是否命中某条已审批前缀（如 ["git", "push"]）。"""
        tokens = command.strip().split()
        if not tokens:
            return False
        for prefix in self.allowlist_prefixes:
            if len(prefix) > len(tokens):
                continue
            if all(a == b for a, b in zip(prefix, tokens)):
                return True
        return False

    def _outside_paths(self, command: str) -> set[str]:
        """找出命令中位于所有允许工作区之外的绝对路径 token。"""
        outside: set[str] = set()
        for match in _PATH_PATTERN.finditer(command):
            token = match.group(0)
            if _is_virtual_path(token):
                continue
            path = Path(token)
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path.absolute()
            if not any(_is_inside(resolved, root) for root in self.allowed_roots):
                outside.add(token)
        return outside


def _is_inside(child: Path, root: Path) -> bool:
    try:
        child.relative_to(root)
        return True
    except ValueError:
        return False