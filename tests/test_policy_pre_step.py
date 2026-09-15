"""policy 治理层测试：风险检测 / 工作目录检测 / 审批决策 / pre_step 拦截。"""
import asyncio
from pathlib import Path

from agent_test import ReactAgent
from agent_test.policy import (
    CommandPolicy,
    PolicyAction,
    RiskSeverity,
    analyze_command,
    highest_severity,
)
from agent_test.tools.bash import register_bash
from agent_test.tools.center import ToolCenter
from agent_test.types.messages import (
    AssistantMessage,
    TextBlock,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
)


def _sev(command: str) -> RiskSeverity | None:
    return highest_severity(analyze_command(command))


# ---------- 静态风险检测（rules） ----------

def test_analyze_denies_destructive():
    assert _sev("rm -rf /tmp/x") is RiskSeverity.DENY
    assert _sev("del /s /q C:\\temp\\x") is RiskSeverity.DENY
    assert _sev("Remove-Item -Recurse -Force C:\\x") is RiskSeverity.DENY
    assert _sev("rd /s /q C:\\x") is RiskSeverity.DENY
    assert _sev("format C:") is RiskSeverity.DENY


def test_analyze_approves_influential():
    assert _sev("git push origin main") is RiskSeverity.APPROVE
    assert _sev("pip install requests") is RiskSeverity.APPROVE
    assert _sev("curl https://example.com") is RiskSeverity.APPROVE


def test_analyze_harmless_returns_none():
    assert _sev("echo hello") is None
    assert _sev("git status") is None
    assert _sev("uv run pytest -q") is None


# ---------- 决策层（CommandPolicy） ----------

def test_decide_execute_harmless(tmp_path):
    policy = CommandPolicy(allowed_roots=[tmp_path])
    decision = policy.decide("echo hello", default_cwd=tmp_path)
    assert decision.action is PolicyAction.EXECUTE
    assert decision.detail["cwd"] == str(tmp_path.resolve())


def test_decide_deny_destructive(tmp_path):
    policy = CommandPolicy(allowed_roots=[tmp_path])
    decision = policy.decide("rm -rf somewhere", default_cwd=tmp_path)
    assert decision.action is PolicyAction.DENY
    assert any("rm_recursive" in reason for reason in decision.reasons)


def test_decide_require_approval_git_push(tmp_path):
    policy = CommandPolicy(allowed_roots=[tmp_path])
    decision = policy.decide("git push origin main", default_cwd=tmp_path)
    assert decision.action is PolicyAction.REQUIRE_APPROVAL
    assert "需要审批" in decision.to_message()


def test_allowlist_prefix_overrides_approval(tmp_path):
    policy = CommandPolicy(
        allowed_roots=[tmp_path], allowlist_prefixes=[["git", "push"]]
    )
    assert (
        policy.decide("git push origin main", default_cwd=tmp_path).action
        is PolicyAction.EXECUTE
    )
    assert (
        policy.decide("git push --force origin main", default_cwd=tmp_path).action
        is PolicyAction.EXECUTE
    )


def test_allowlist_not_matching_still_requires_approval(tmp_path):
    policy = CommandPolicy(
        allowed_roots=[tmp_path], allowlist_prefixes=[["git", "pull"]]
    )
    assert (
        policy.decide("git push origin main", default_cwd=tmp_path).action
        is PolicyAction.REQUIRE_APPROVAL
    )


# ---------- 工作目录检测 ----------

def test_cwd_detection_missing_dir(tmp_path):
    policy = CommandPolicy(allowed_roots=[tmp_path])
    decision = policy.decide(
        "echo hi", workdir=str(tmp_path / "no-such-dir"), default_cwd=tmp_path
    )
    assert decision.action is PolicyAction.DENY
    assert "工作目录检测失败" in decision.reasons[0]


def test_cwd_detection_outside_workspace(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir(exist_ok=True)
    policy = CommandPolicy(allowed_roots=[tmp_path])
    decision = policy.decide(
        "echo hi", workdir=str(outside), default_cwd=tmp_path
    )
    assert decision.action is PolicyAction.DENY
    assert "超出允许工作区" in decision.reasons[0]


def test_cwd_relative_to_default(tmp_path):
    (tmp_path / "sub").mkdir(exist_ok=True)
    policy = CommandPolicy(allowed_roots=[tmp_path])
    decision = policy.decide("echo hi", workdir="sub", default_cwd=tmp_path)
    assert decision.action is PolicyAction.EXECUTE
    assert decision.detail["cwd"] == str((tmp_path / "sub").resolve())


def test_outside_path_requires_approval(tmp_path):
    policy = CommandPolicy(allowed_roots=[tmp_path])
    decision = policy.decide("type C:\\Windows\\win.ini", default_cwd=tmp_path)
    assert decision.action is PolicyAction.REQUIRE_APPROVAL


# ---------- ToolCenter 硬闸门 ----------

def test_tool_center_gate_blocks_denied_bash(tmp_path):
    center = ToolCenter(policy=CommandPolicy(allowed_roots=[tmp_path]))
    register_bash(center)
    result = asyncio.run(center.execute("bash", {"command": "rm -rf /tmp/x"}))
    assert result["is_error"] is True
    assert result["blocked"] is True
    assert result["policy_action"] == "deny"
    assert "被策略拒绝" in result["content"]


def test_tool_center_gate_requires_approval(tmp_path):
    center = ToolCenter(policy=CommandPolicy(allowed_roots=[tmp_path]))
    register_bash(center)
    result = asyncio.run(
        center.execute("bash", {"command": "git push origin main"})
    )
    assert result["is_error"] is True
    assert result["policy_action"] == "require_approval"
    assert "需要审批" in result["content"]


# ---------- Agent pre_step 阶段 ----------

class _FakeLLM:
    """假 LLM：第一轮请求工具，第二轮给出最终回答。"""

    def __init__(self, tool_args: dict) -> None:
        self.n = 0
        self.tool_args = tool_args

    async def stream(self, messages, tools):
        self.n += 1
        if self.n == 1:
            import json

            return (
                AssistantMessage(
                    id="a1",
                    content=[
                        ToolCallBlock(
                            id="c1", name="bash", args=json.dumps(self.tool_args)
                        )
                    ],
                ),
                "",
                None,
            )
        return (
            AssistantMessage(id="a2", content=[TextBlock(content="done")]),
            "finish",
            None,
        )


def _run_pre_step_agent(tmp_path, tool_args: dict):
    center = ToolCenter(policy=CommandPolicy(allowed_roots=[tmp_path]))
    register_bash(center)
    agent = ReactAgent(
        persist_dir=str(tmp_path), tools=center, llm_client=_FakeLLM(tool_args)
    )
    agent.inbox.append(
        "turn", UserMessage(id="u1", content=[TextBlock(content="run")])
    )
    assert asyncio.run(agent.turn()) is True
    return agent


def test_agent_pre_step_denies_destructive_bash(tmp_path):
    """破坏性 bash 调用在 pre_step 被拒：不进 execute、以 is_error 结果落 session。"""
    agent = _run_pre_step_agent(tmp_path, {"command": "rm -rf /tmp/x"})
    results = [
        e.data for e in agent.session.events if isinstance(e.data, ToolResultMessage)
    ]
    assert len(results) == 1
    assert results[0].is_error is True
    assert "被策略拒绝" in results[0].content[0].content


def test_agent_pre_step_requires_approval_bash(tmp_path):
    """需审批命令在 pre_step 被拦下：返回可回传 LLM 的“需要审批”结果。"""
    agent = _run_pre_step_agent(tmp_path, {"command": "git push origin main"})
    results = [
        e.data for e in agent.session.events if isinstance(e.data, ToolResultMessage)
    ]
    assert len(results) == 1
    assert results[0].is_error is True
    assert "需要审批" in results[0].content[0].content