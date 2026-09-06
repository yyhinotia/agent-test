"""命令风险静态检测规则（纯字符串分析，与执行环境解耦）。

职责：只回答“这条命令文本有什么风险信号”，不做权限决策；
决策（放行 / 需审批 / 拒绝）由 policy.CommandPolicy 依据本模块
产出的 findings 与 allowlist 等工作区上下文完成。

规则分三档（RiskSeverity）：
- deny    破坏性/不可逆命令，默认直接拒绝（递归强制删除、磁盘格式化等）；
- approve 影响面较大的命令，默认需人工/上级审批（推送、安装、网络等）；
- warn    提示性信息（如 cd .. 跳出当前目录），默认不阻断。

注意：本模块刻意保持“无副作用、无 IO、纯正则”，便于单测与复用。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable


class RiskSeverity(str, Enum):
    DENY = "deny"
    APPROVE = "approve"
    WARN = "warn"


# 严重度排序：deny > approve > warn，用于取“最高”风险
_SEVERITY_RANK = {
    RiskSeverity.DENY: 3,
    RiskSeverity.APPROVE: 2,
    RiskSeverity.WARN: 1,
}


@dataclass(frozen=True)
class RiskRule:
    """一条风险规则：正则命中即产出对应 finding。"""

    rule_id: str
    severity: RiskSeverity
    pattern: str
    description: str
    _compiled: re.Pattern[str] = field(repr=False, compare=False)

    def __init__(
        self, rule_id: str, severity: RiskSeverity, pattern: str, description: str
    ) -> None:
        object.__setattr__(self, "rule_id", rule_id)
        object.__setattr__(self, "severity", RiskSeverity(severity))
        object.__setattr__(self, "pattern", pattern)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "_compiled", re.compile(pattern, re.IGNORECASE))

    def search(self, command: str) -> re.Match[str] | None:
        return self._compiled.search(command)


@dataclass(frozen=True)
class RiskFinding:
    """一条命中记录：规则 + 命中片段。"""

    rule_id: str
    severity: RiskSeverity
    description: str
    matched: str

    def format(self) -> str:
        return (
            f"[{self.severity.value}] {self.rule_id}: {self.description}"
            f" (命中: {self.matched!r})"
        )


# ---- deny：破坏性 / 不可逆 ----
_DENY_RULES: tuple[RiskRule, ...] = (
    RiskRule(
        "rm_recursive",
        RiskSeverity.DENY,
        r"\brm\s+-\S*[rf]",
        "递归/强制删除文件（rm -r / -f 类）",
    ),
    RiskRule(
        "remove_item",
        RiskSeverity.DENY,
        r"\bRemove-Item\b",
        "PowerShell Remove-Item 删除",
    ),
    RiskRule(
        "rmdir_recursive",
        RiskSeverity.DENY,
        r"\b(?:rmdir|rd)\b[^\n]*\s/s",
        "递归删除目录（rmdir / rd /s）",
    ),
    RiskRule(
        "del_recursive",
        RiskSeverity.DENY,
        r"\b(?:del|erase)\b[^\n]*\s/(?:f|s|q)",
        "批量删除文件（del / erase 带 /s /f /q）",
    ),
    RiskRule(
        "format_disk",
        RiskSeverity.DENY,
        r"\bformat\s+[a-zA-Z]:",
        "格式化磁盘分区",
    ),
    RiskRule(
        "disk_tool",
        RiskSeverity.DENY,
        r"\b(?:diskpart|mkfs|fdisk|mbr2gpt|gdisk)\b",
        "磁盘分区/文件系统工具",
    ),
    RiskRule(
        "dd_write",
        RiskSeverity.DENY,
        r"\bdd\s+[^\n]*\bof=",
        "dd 直接写入块设备/文件",
    ),
    RiskRule(
        "clear_recycle",
        RiskSeverity.DENY,
        r"\bClear-RecycleBin\b",
        "清空回收站",
    ),
    RiskRule(
        "vssadmin_delete",
        RiskSeverity.DENY,
        r"\bvssadmin\s+delete\b",
        "删除卷影副本（恢复点）",
    ),
)

# ---- approve：影响面大，需审批 ----
_APPROVE_RULES: tuple[RiskRule, ...] = (
    RiskRule(
        "git_push",
        RiskSeverity.APPROVE,
        r"\bgit\s+push\b",
        "推送代码到远端（git push）",
    ),
    RiskRule(
        "git_hard_reset",
        RiskSeverity.APPROVE,
        r"\bgit\s+(?:reset\s+--hard|clean\s+-\S*f|checkout\s+--\s*\.)",
        "丢弃本地改动（reset --hard / clean -f / checkout -- .）",
    ),
    RiskRule(
        "network_fetch",
        RiskSeverity.APPROVE,
        r"\b(?:curl|wget|Invoke-WebRequest|Invoke-RestMethod|iwr)\b",
        "发起网络请求/下载",
    ),
    RiskRule(
        "package_install",
        RiskSeverity.APPROVE,
        r"\b(?:pip|npm|pnpm|yarn|uv|gem)\b[^\n]*\b(?:install|add|uninstall|remove|publish)\b",
        "安装/发布依赖包",
    ),
    RiskRule(
        "process_kill",
        RiskSeverity.APPROVE,
        r"\b(?:taskkill|Stop-Process|kill\s+-9)\b",
        "强制结束进程",
    ),
    RiskRule(
        "registry_edit",
        RiskSeverity.APPROVE,
        r"\breg\s+(?:add|delete|import)\b",
        "修改 Windows 注册表",
    ),
    RiskRule(
        "service_change",
        RiskSeverity.APPROVE,
        r"\b(?:net\s+(?:use|stop|start)|sc\s+(?:start|stop|delete)|shutdown)\b",
        "服务/共享/关机等系统级变更",
    ),
    RiskRule(
        "acl_change",
        RiskSeverity.APPROVE,
        r"\b(?:icacls|takeown|attrib)\b",
        "修改文件权限/所有权",
    ),
)

# ---- warn：提示性 ----
_WARN_RULES: tuple[RiskRule, ...] = (
    RiskRule(
        "cd_escape",
        RiskSeverity.WARN,
        r"\bcd\b[^\n]*\.\.",
        "cd .. 跳出当前目录（若仍处于工作区内则无碍）",
    ),
)


def analyze_command(command: str) -> list[RiskFinding]:
    """扫描命令文本，返回全部命中（deny/approve/warn）的规则。"""
    findings: list[RiskFinding] = []
    for rule in (*_DENY_RULES, *_APPROVE_RULES, *_WARN_RULES):
        match = rule.search(command)
        if match:
            findings.append(
                RiskFinding(
                    rule_id=rule.rule_id,
                    severity=rule.severity,
                    description=rule.description,
                    matched=match.group(0),
                )
            )
    return findings


def highest_severity(findings: Iterable[RiskFinding]) -> RiskSeverity | None:
    """返回 findings 中的最高严重度（deny > approve > warn），无命中返回 None。"""
    best: RiskSeverity | None = None
    best_rank = 0
    for finding in findings:
        rank = _SEVERITY_RANK[finding.severity]
        if rank > best_rank:
            best = finding.severity
            best_rank = rank
    return best