"""知识增量：串行审批 + 并行执行、双状态机、pre_decision 旁路。

覆盖 docs/增量知识.md 的 MUST_REPLICATE 与验收标准：
- ApprovalResult / ExecutionResult 双状态机（not_started vs failed）；
- ToolCenter.execute 的 pre_decision 旁路与安全兜底（互斥）；
- Agent._pre_step 串行审批（allowlist 级联）→ _step asyncio.gather 并行执行；
- 审批被拒 / 策略拦截的工具为 execution=not_started（无副作用）；
- 越界路径审批记忆与虚拟路径排除；
- LLM 适配器审批/执行标签渲染与结构化提示词注入；
- 压缩不得把「审批被拒」退化为「执行失败」。
"""
import asyncio
import json
import time
from typing import Any, Sequence

from agent_test.core.agent import ReactAgent
from agent_test.core.prompts import build_system_prompt, system_prompt
from agent_test.human.service import AskService
from agent_test.llm.adapter import LLMBaseAdapter
from agent_test.llm.compactor import Compactor
from agent_test.policy import CommandPolicy, PolicyAction, PolicyDecision
from agent_test.session.session import Session
from agent_test.tools.center import ToolCenter
from agent_test.types.messages import (
    ApprovalResult,
    AssistantMessage,
    ExecutionResult,
    TextBlock,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
)


# ---------------- 测试替身 ----------------

class ScriptedAskService(AskService):
    """脚本化审批：按顺序返回预设回答，并记录每次询问。"""

    def __init__(self, answers: Sequence[tuple[bool, str]] = ()) -> None:
        self.answers = list(answers)
        self.confirm_calls: list[dict] = []
        self.ask_calls: list[dict] = []

    async def ask_user(self, *, question, options=None, multi_select=False, header=None):
        self.ask_calls.append({"question": question, "options": options})
        return "ok"

    async def confirm(self, *, action, description=None):
        self.confirm_calls.append({"action": action, "description": description})
        if self.answers:
            return self.answers.pop(0)
        return False, "脚本未提供审批回答"


class GatedPolicy(CommandPolicy):
    """把 run_task 工具接到与 bash 相同的审批链路（命令文本仅用于决策）。

    这样测试无需真的执行 git / rm 等命令，就能覆盖审批与 allowlist 级联。
    """

    def decide_tool(self, tool_name, args, *, default_cwd=None):
        if tool_name == "run_task":
            return self.decide(
                str(args.get("command", "")),
                tool_name=tool_name,
                workdir=args.get("workdir"),
                default_cwd=default_cwd,
            )
        return PolicyDecision(PolicyAction.EXECUTE, tool_name)


class ScriptedLLM:
    """按脚本返回多步响应：steps 为 [(tool_calls, end_reason), ...]。"""

    def __init__(self, steps: Sequence[tuple[list[dict], str]]) -> None:
        self.steps = list(steps)
        self.calls = 0

    async def stream(self, messages, tools):
        self.calls += 1
        if self.steps:
            calls, end_reason = self.steps.pop(0)
        else:
            calls, end_reason = [], "finish"
        if calls:
            blocks = [
                ToolCallBlock(
                    id=c["id"], name=c["name"], args=json.dumps(c["args"])
                )
                for c in calls
            ]
            return (
                AssistantMessage(id=f"a{self.calls}", content=blocks),
                end_reason,
                None,
            )
        return (
            AssistantMessage(
                id=f"a{self.calls}", content=[TextBlock(content="done")]
            ),
            end_reason or "finish",
            None,
        )


class ExplodingCenter:
    """部分工具抛异常的 ToolCenter 替身（验证 gather 不被单个异常打断）。"""

    def __init__(self) -> None:
        self.policy = None
        self.approver = None
        self.executed: list[str] = []

    def get_schemas(self):
        return None

    async def execute(self, func_name, func_args, *, pre_decision=None):
        self.executed.append(func_name)
        if func_name == "boom":
            raise RuntimeError("boom")
        return {
            "content": f"ok:{func_name}",
            "is_error": False,
            "approval": ApprovalResult(),
            "execution": ExecutionResult(),
        }


def _register_run_task(center: ToolCenter, counter: dict | None = None) -> None:
    @center.register(
        desc="测试任务（不真的执行命令，仅用于审批链路测试）",
        parameters={
            "command": {"type": "string"},
            "workdir": {"type": "string"},
        },
        required=["command"],
        name="run_task",
    )
    def run_task(command: str, workdir: str | None = None) -> str:
        if counter is not None:
            counter["n"] = counter.get("n", 0) + 1
        return f"ran:{command}"


def _run_agent(tmp_path, llm, tools) -> ReactAgent:
    agent = ReactAgent(persist_dir=str(tmp_path), tools=tools, llm_client=llm)
    agent.inbox.append(
        "turn", UserMessage(id="u1", content=[TextBlock(content="go")])
    )
    assert asyncio.run(agent.turn()) is True
    return agent


def _tool_results(agent: ReactAgent) -> list[ToolResultMessage]:
    return [
        e.data
        for e in agent.session.events
        if isinstance(e.data, ToolResultMessage)
    ]


# ---------------- 双状态机模型 ----------------

def test_tool_result_defaults_are_backward_compatible():
    """旧 JSONL 无 approval/execution 字段 -> None；is_error 语义不变。"""
    msg = ToolResultMessage(tool_call_id="c1", content=[TextBlock(content="x")])
    assert msg.approval is None and msg.execution is None
    assert msg.is_error is False
    assert ApprovalResult().decision == "auto"
    assert ExecutionResult().status == "success"


def test_approval_and_execution_are_independent_state_machines():
    """denied -> not_started（不执行）与 failed（执行报错）语义不同。"""
    denied = ToolResultMessage(
        tool_call_id="c1",
        content=[TextBlock(content="用户拒绝")],
        is_error=True,
        approval=ApprovalResult(
            decision="denied", required=True, reason_code="user_denied"
        ),
        execution=ExecutionResult(status="not_started"),
    )
    failed = ToolResultMessage(
        tool_call_id="c2",
        content=[TextBlock(content="命令报错")],
        is_error=True,
        approval=ApprovalResult(decision="approved", required=True),
        execution=ExecutionResult(status="failed", error="exit 1"),
    )
    assert denied.approval.decision != failed.approval.decision
    assert denied.execution.status != failed.execution.status


# ---------------- ToolCenter：pre_decision 旁路 ----------------

def test_pre_decision_bypass_and_safety_net(tmp_path):
    counter: dict[str, int] = {}
    svc = ScriptedAskService()
    center = ToolCenter(
        policy=GatedPolicy(allowed_roots=[tmp_path]), approver=svc
    )
    _register_run_task(center, counter)

    # 1) pre_decision=EXECUTE：跳过策略（否则会被 DENY），且不二次询问审批
    allowed = PolicyDecision(PolicyAction.EXECUTE, "run_task", ("测试放行",))
    result = asyncio.run(
        center.execute(
            "run_task", {"command": "rm -rf /tmp/x"}, pre_decision=allowed
        )
    )
    assert result["is_error"] is False
    assert counter["n"] == 1
    assert result["approval"].decision == "auto"
    assert result["approval"].source == "pre_step"
    assert result["execution"].status == "success"
    assert svc.confirm_calls == [], "pre_decision 已放行，不得二次审批"

    # 2) pre_decision=DENY：拦截，工具不执行，execution=not_started
    deny = PolicyDecision(PolicyAction.DENY, "run_task", ("测试拒绝",))
    blocked = asyncio.run(
        center.execute("run_task", {"command": "x"}, pre_decision=deny)
    )
    assert blocked["is_error"] is True
    assert blocked["blocked"] is True
    assert counter["n"] == 1, "被拦截的工具不得执行"
    assert blocked["approval"].decision == "blocked"
    assert blocked["execution"].status == "not_started"

    # 3) 不传 pre_decision：策略硬闸门仍然生效（防绕过）
    gate = asyncio.run(center.execute("run_task", {"command": "rm -rf /tmp/x"}))
    assert gate["is_error"] is True
    assert gate["policy_action"] == "deny"
    assert counter["n"] == 1


def test_pre_decision_require_approval_not_resolved_is_blocked(tmp_path):
    """pre_decision 已结案但未放行 -> 不再询问用户，直接 blocked。"""
    svc = ScriptedAskService([(True, "不该被调用")])
    center = ToolCenter(
        policy=GatedPolicy(allowed_roots=[tmp_path]), approver=svc
    )
    _register_run_task(center, {})
    decision = PolicyDecision(
        PolicyAction.REQUIRE_APPROVAL,
        "run_task",
        ("测试需审批",),
        {"command": "git push origin main", "_resolved": True, "_approved": False,
         "_approval_decision": "denied", "_approval_source": "user",
         "_denied_reason": "用户不同意"},
    )
    result = asyncio.run(
        center.execute(
            "run_task", {"command": "git push origin main"}, pre_decision=decision
        )
    )
    assert result["is_error"] is True
    assert result["execution"].status == "not_started"
    assert svc.confirm_calls == [], "已结案的审批不得重复询问"


# ---------------- Agent：串行审批 + 并行执行 ----------------

def test_serial_approval_cascades_allowlist(tmp_path):
    """批准一次后记住前缀：同类命令不再询问（allowlist 级联）。"""
    svc = ScriptedAskService([(True, "用户已批准")])
    center = ToolCenter(
        policy=GatedPolicy(allowed_roots=[tmp_path]), approver=svc
    )
    _register_run_task(center)
    llm = ScriptedLLM(
        [
            (
                [
                    {"id": "c1", "name": "run_task",
                     "args": {"command": "git push origin main"}},
                    {"id": "c2", "name": "run_task",
                     "args": {"command": "git push origin dev"}},
                ],
                "",
            )
        ]
    )
    agent = _run_agent(tmp_path, llm, center)

    assert len(svc.confirm_calls) == 1, "第二条命中 allowlist，不应再询问"
    results = _tool_results(agent)
    assert [r.tool_call_id for r in results] == ["c1", "c2"]
    assert results[0].approval.decision == "approved"
    assert results[0].approval.source == "user"
    assert results[1].approval.decision == "auto"
    assert all(r.execution.status == "success" for r in results)
    assert all(r.is_error is False for r in results)


def test_denied_and_blocked_results_are_not_started(tmp_path):
    """同一批工具：拒绝 / 策略拦截 / 放行三种结果并存且语义分明。"""
    svc = ScriptedAskService([(False, "我不同意推送")])
    center = ToolCenter(
        policy=GatedPolicy(allowed_roots=[tmp_path]), approver=svc
    )
    _register_run_task(center)
    llm = ScriptedLLM(
        [
            (
                [
                    {"id": "c1", "name": "run_task",
                     "args": {"command": "rm -rf /tmp/x"}},
                    {"id": "c2", "name": "run_task",
                     "args": {"command": "git push origin main"}},
                    {"id": "c3", "name": "run_task",
                     "args": {"command": "echo hi"}},
                ],
                "",
            )
        ]
    )
    agent = _run_agent(tmp_path, llm, center)
    results = {r.tool_call_id: r for r in _tool_results(agent)}

    blocked = results["c1"]
    assert blocked.approval.decision == "blocked"
    assert blocked.execution.status == "not_started"
    assert blocked.is_error is True

    denied = results["c2"]
    assert denied.approval.decision == "denied"
    assert denied.approval.reason_code == "user_denied"
    assert denied.execution.status == "not_started"
    assert "审批被用户拒绝" in denied.content[0].content
    assert "我不同意推送" in denied.content[0].content

    ok = results["c3"]
    assert ok.approval.decision == "auto"
    assert ok.execution.status == "success"
    assert ok.is_error is False


def test_tools_execute_in_parallel(tmp_path):
    """三个各睡 0.2s 的工具应并行完成（总耗时远小于串行的 0.6s）。"""
    center = ToolCenter()

    @center.register(
        desc="异步睡眠工具",
        parameters={"task": {"type": "string"}},
        required=["task"],
        name="sleepy",
    )
    async def sleepy(task: str) -> str:
        await asyncio.sleep(0.2)
        return f"slept:{task}"

    llm = ScriptedLLM(
        [
            (
                [
                    {"id": "c1", "name": "sleepy", "args": {"task": "a"}},
                    {"id": "c2", "name": "sleepy", "args": {"task": "b"}},
                    {"id": "c3", "name": "sleepy", "args": {"task": "c"}},
                ],
                "",
            )
        ]
    )
    start = time.perf_counter()
    agent = _run_agent(tmp_path, llm, center)
    elapsed = time.perf_counter() - start

    assert len(_tool_results(agent)) == 3
    assert elapsed < 0.5, f"工具未并行执行，耗时 {elapsed:.2f}s"


def test_single_tool_exception_does_not_cancel_others(tmp_path):
    """单个工具抛异常不取消其他工具（gather 语义 + _exec_one 兜底）。"""
    center = ExplodingCenter()
    llm = ScriptedLLM(
        [
            (
                [
                    {"id": "c1", "name": "boom", "args": {}},
                    {"id": "c2", "name": "fine", "args": {}},
                ],
                "",
            )
        ]
    )
    agent = _run_agent(tmp_path, llm, center)
    results = {r.tool_call_id: r for r in _tool_results(agent)}

    assert results["c1"].is_error is True
    assert results["c1"].execution.status == "failed"
    assert results["c2"].is_error is False
    assert results["c2"].execution.status == "success"
    assert set(center.executed) == {"boom", "fine"}


def test_policy_none_path_still_executes(tmp_path):
    """无策略时 pre_step 产出 EXECUTE，工具照常执行。"""
    center = ToolCenter()
    _register_run_task(center)
    llm = ScriptedLLM(
        [([{"id": "c1", "name": "run_task", "args": {"command": "echo hi"}}], "")]
    )
    agent = _run_agent(tmp_path, llm, center)
    (result,) = _tool_results(agent)
    assert result.is_error is False
    assert result.execution.status == "success"
    assert result.approval.decision == "auto"


def test_approval_metadata_persisted_and_restored(tmp_path):
    """审批/执行元数据写入 JSONL，并能 from_file 还原（可回放可审计）。"""
    svc = ScriptedAskService([(False, "我不同意")])
    center = ToolCenter(
        policy=GatedPolicy(allowed_roots=[tmp_path]), approver=svc
    )
    _register_run_task(center)
    llm = ScriptedLLM(
        [
            (
                [
                    {"id": "c1", "name": "run_task",
                     "args": {"command": "git push origin main"}}
                ],
                "",
            )
        ]
    )
    agent = _run_agent(tmp_path, llm, center)

    raw = agent.session.file_path.read_text(encoding="utf-8")
    assert '"approval"' in raw and '"execution"' in raw
    assert "not_started" in raw
    assert "user_denied" in raw

    restored = Session.from_file(agent.session.session_id, str(tmp_path))
    (result,) = [
        e.data
        for e in restored.events
        if isinstance(e.data, ToolResultMessage)
    ]
    assert result.approval is not None
    assert result.approval.decision == "denied"
    assert result.approval.reason_code == "user_denied"
    assert result.execution is not None
    assert result.execution.status == "not_started"


# ---------------- 策略：越界路径记忆 + 虚拟路径 ----------------

def test_approved_outside_path_is_remembered(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "note.txt"
    command = f"type {target}"

    policy = CommandPolicy(allowed_roots=[sandbox])
    assert policy.decide(command).action is PolicyAction.REQUIRE_APPROVAL

    added = policy.add_outside_paths_for_command(command)
    assert added, "审批通过后应记住越界路径"

    assert policy.decide(command).action is PolicyAction.EXECUTE, "同一路径不再审批"


def test_outside_workdir_approval_cascades_to_next_call(tmp_path):
    """越界 workdir：第 1 条审批放行，第 2 条命中记住的允许根走 auto。

    对应真实会话里「模型把路径塞进命令文本绕开 workdir 审批」的修复：
    越界工作目录现在也可审批，批准后记入允许根，无需再绕过。
    """
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    svc = ScriptedAskService([(True, "用户已批准")])
    center = ToolCenter(
        policy=GatedPolicy(allowed_roots=[sandbox]), approver=svc
    )
    _register_run_task(center)
    llm = ScriptedLLM(
        [
            (
                [
                    {
                        "id": "c1",
                        "name": "run_task",
                        "args": {"command": "echo hi", "workdir": str(outside)},
                    }
                ],
                "",
            ),
            (
                [
                    {
                        "id": "c2",
                        "name": "run_task",
                        "args": {"command": "echo again", "workdir": str(outside)},
                    }
                ],
                "",
            ),
        ]
    )
    agent = _run_agent(tmp_path, llm, center)

    assert len(svc.confirm_calls) == 1, "同一越界目录批准一次后不再询问"
    assert str(outside) in svc.confirm_calls[0]["description"]
    results = {r.tool_call_id: r for r in _tool_results(agent)}
    assert results["c1"].approval.decision == "approved"
    assert results["c1"].execution.status == "success"
    assert results["c2"].approval.decision == "auto"
    assert results["c2"].execution.status == "success"


def test_virtual_paths_are_not_treated_as_outside(tmp_path):
    policy = CommandPolicy(allowed_roots=[tmp_path])
    assert policy.decide("echo hi > /dev/null").action is PolicyAction.EXECUTE
    assert policy.decide("echo hi > nul").action is PolicyAction.EXECUTE


# ---------------- LLM 适配器：标签与提示词 ----------------

class _StubAdapter(LLMBaseAdapter):
    """仅用于验证消息组装（不发起真实请求）。"""


def test_default_system_prompt_is_structured_prompt():
    adapter = _StubAdapter()
    assert adapter.system_prompt == system_prompt
    assert ";" in adapter.system_prompt
    assert "环境检查" in adapter.system_prompt
    custom = _StubAdapter(system_prompt="自定义")
    assert custom.system_prompt == "自定义"


def test_approval_and_execution_labels_rendered():
    adapter = _StubAdapter(system_prompt="sys")
    message = ToolResultMessage(
        tool_call_id="c1",
        content=[TextBlock(content="命令未执行")],
        is_error=True,
        approval=ApprovalResult(
            decision="denied", required=True, reason_code="user_denied"
        ),
        execution=ExecutionResult(status="not_started"),
    )
    (system, tool) = adapter.assemble_messages([message])
    assert system["role"] == "system"
    assert tool["role"] == "tool"
    assert "[审批:用户拒绝执行](user_denied)" in tool["content"]
    assert "[执行:未开始(审批未通过)]" in tool["content"]
    assert "命令未执行" in tool["content"]


def test_legacy_tool_result_without_state_has_no_labels():
    adapter = _StubAdapter(system_prompt="sys")
    message = ToolResultMessage(
        tool_call_id="c1", content=[TextBlock(content="raw")], is_error=True
    )
    (_, tool) = adapter.assemble_messages([message])
    assert tool["content"] == "raw"


# ---------------- 压缩：保留审批语义 ----------------

def test_compaction_keeps_approval_semantics(tmp_path):
    session = Session(persist_dir=str(tmp_path))
    session.append(
        "user/message",
        data=UserMessage(id="u1", content=[TextBlock(content="跑一下")]),
        turn=1,
        step=0,
    )
    session.append(
        "tool/result",
        data=ToolResultMessage(
            tool_call_id="c1",
            content=[TextBlock(content="审批被用户拒绝：不允许推送")],
            is_error=True,
            approval=ApprovalResult(
                decision="denied", required=True, reason_code="user_denied"
            ),
            execution=ExecutionResult(status="not_started"),
        ),
        turn=1,
        step=1,
    )
    compactor = Compactor(session=session)
    lines = compactor.compact_session(session.events)
    tool_line = [line for line in lines if "tool:c1" in line][0]
    # 不能退化成一个含糊的 error/ok
    assert "not_started" in tool_line
    assert "approval=denied" in tool_line
    assert "reason=user_denied" in tool_line


def test_system_prompt_is_platform_aware():
    """真实会话复盘：Windows 上先跑 uname 白费一次调用，改为按平台生成。"""
    windows = build_system_prompt("windows")
    assert windows.index("ver && cd") < windows.index("uname -s && pwd")
    assert "Windows" in windows

    posix = build_system_prompt("posix")
    assert posix.index("uname -s && pwd") < posix.index("ver && cd")

    for text in (windows, posix):
        assert "__ENV_" not in text, "平台占位符必须全部替换"


def test_system_prompt_forbids_duplicate_confirmation():
    """真实会话复盘：模型 confirm 一次 + 策略闸门再一次 = 用户回答两遍。"""
    assert "两次征询" in system_prompt
    assert "confirm" in system_prompt
    assert "不要先创建文件/目录再删除" in system_prompt
