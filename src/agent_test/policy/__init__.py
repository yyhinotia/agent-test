"""命令治理策略包：权限审批 + 危险检测 + 工作目录检测（跨工具 pre_step 拦截）。

与具体工具解耦的治理层，供 ToolCenter.execute 硬闸门与
ReactAgent._step 的 pre_step 阶段复用：
- cwd.py    工作目录解析与工作区（allowed roots）越界检测
- rules.py  纯静态风险规则（deny / approve / warn 三档命中）
- policy.py 决策层 CommandPolicy：allowlist + 规则 + 越界 -> PolicyDecision
"""
from agent_test.policy.cwd import CwdCheck, is_within, resolve_workdir
from agent_test.policy.policy import CommandPolicy, PolicyAction, PolicyDecision
from agent_test.policy.rules import (
    RiskFinding,
    RiskRule,
    RiskSeverity,
    analyze_command,
    highest_severity,
)

__all__ = [
    "CommandPolicy",
    "CwdCheck",
    "PolicyAction",
    "PolicyDecision",
    "RiskFinding",
    "RiskRule",
    "RiskSeverity",
    "analyze_command",
    "highest_severity",
    "is_within",
    "resolve_workdir",
]