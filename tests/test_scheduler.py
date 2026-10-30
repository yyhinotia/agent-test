"""DAG 调度器：拓扑分层、子图、Checkpoint、PAE 重规划与计划服务。"""
import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_test.app.scheduler import (
    CheckpointRecord,
    GraphState,
    NodeState,
    NodeStatus,
    PAEPhase,
    Scheduler,
    StepResult,
    TaskDAG,
    TaskNode,
)
from agent_test.app.application import parse_args
from agent_test.core.plan_service import PlanService
from agent_test.tools.center import ToolCenter
from agent_test.types.messages import AssistantMessage, TextBlock, UserMessage
from agent_test.types.plans import Plan, PlanStatus, PlanStep


def _make_dag(nodes: list[dict], graph_id: str = "g") -> TaskDAG:
    dag = TaskDAG(
        graph_id=graph_id,
        global_goal="goal",
        nodes=[TaskNode(**node) for node in nodes],
    )
    dag.init_dag()
    return dag


def _plan(graph_id: str, nodes: list[dict]) -> dict:
    return {"graph_id": graph_id, "global_goal": "goal", "nodes": nodes}


def _success(node, ctx, memory, graph_id):
    return {
        "node_id": node.node_id,
        "status": "success",
        "output": {"node": node.node_id},
        "evidence": [node.node_id],
        "self_check": True,
        "rounds_used": 1,
    }


# ---------------------------------------------------------------------
# DAG 拓扑与子图
# ---------------------------------------------------------------------


def test_topo_sort_layers_and_priority_order():
    dag = _make_dag(
        [
            {"node_id": "a", "goal": "A", "depends_on": [], "priority": 0},
            {"node_id": "b", "goal": "B", "depends_on": [], "priority": 5},
            {"node_id": "c", "goal": "C", "depends_on": ["a"]},
            {"node_id": "d", "goal": "D", "depends_on": ["b", "c"]},
        ]
    )

    assert [[n.node_id for n in layer] for layer in dag.layers] == [
        ["b", "a"],
        ["c"],
        ["d"],
    ]


def test_depends_on_deduplicates():
    node = TaskNode(node_id="a", goal="A", depends_on=["b", "b", "c"])
    assert node.depends_on == ["b", "c"]


@pytest.mark.parametrize(
    "nodes, message",
    [
        ([{"node_id": "a", "goal": "A", "depends_on": ["a"]}], "自环"),
        ([{"node_id": "a", "goal": "A", "depends_on": ["x"]}], "不存在"),
        (
            [
                {"node_id": "a", "goal": "A", "depends_on": ["b"]},
                {"node_id": "b", "goal": "B", "depends_on": ["a"]},
            ],
            "环",
        ),
        (
            [
                {"node_id": "a", "goal": "A"},
                {"node_id": "a", "goal": "A2"},
            ],
            "重复",
        ),
    ],
)
def test_topo_sort_rejects_invalid_dag(nodes, message):
    with pytest.raises(ValueError, match=message):
        _make_dag(nodes)


def test_dag_requires_non_empty_nodes():
    with pytest.raises(ValidationError):
        TaskDAG(graph_id="g", global_goal="goal", nodes=[])


def test_subgraph_includes_upstream_and_downstream_closure():
    dag = _make_dag(
        [
            {"node_id": "a", "goal": "A"},
            {"node_id": "b", "goal": "B", "depends_on": ["a"]},
            {"node_id": "c", "goal": "C", "depends_on": ["b"]},
            {"node_id": "d", "goal": "D"},
        ]
    )

    upstream = dag.subgraph(["c"])
    assert upstream.node_ids() == {"a", "b", "c"}
    assert [[n.node_id for n in layer] for layer in upstream.layers] == [
        ["a"],
        ["b"],
        ["c"],
    ]

    only_d = dag.subgraph(["d"], include_upstream=False)
    assert only_d.node_ids() == {"d"}

    downstream = dag.subgraph(["a"], include_downstream=True)
    assert downstream.node_ids() == {"a", "b", "c"}

    with pytest.raises(ValueError, match="不存在"):
        dag.subgraph(["missing"])


# ---------------------------------------------------------------------
# NodeState 状态派生
# ---------------------------------------------------------------------


def test_node_state_update_by_step_result():
    state = NodeState(node_id="a")

    state.update_by_step_res(
        StepResult(node_id="a", status="success", self_check=False)
    )
    assert state.status is NodeStatus.PARTIAL
    assert state.failure_type == "acceptance_failed"

    state = NodeState(node_id="b")
    state.update_by_step_res(
        StepResult(
            node_id="b",
            status="failed",
            failure_type="rate_limit",
            failure_reason="限流",
        )
    )
    assert state.status is NodeStatus.FAILED_RECOVERABLE
    assert state.is_fail() is True

    state = NodeState(node_id="c")
    state.update_by_step_res(
        StepResult(
            node_id="c", status="failed", failure_type="unhandled_exception"
        )
    )
    assert state.status is NodeStatus.FAILED_FATAL

    state = NodeState(node_id="d", status=NodeStatus.BLOCKED)
    assert state.is_fail() is True
    assert state.needs_replan() is True


# ---------------------------------------------------------------------
# PAE 主循环
# ---------------------------------------------------------------------


def test_pae_replans_and_reuses_success_nodes(tmp_path):
    runs: list[tuple[str, str]] = []

    def planner(goal, ctx):
        if not ctx.get("failed_nodes"):
            return _plan(
                "g1",
                [
                    {"node_id": "a", "goal": "A1", "depends_on": []},
                    {"node_id": "b", "goal": "B", "depends_on": []},
                    {"node_id": "c", "goal": "C", "depends_on": ["a"]},
                ],
            )
        return _plan(
            "g2",
            [
                {"node_id": "a", "goal": "A2", "depends_on": []},
                {"node_id": "b", "goal": "B", "depends_on": []},
                {"node_id": "d", "goal": "D", "depends_on": ["a", "b"]},
            ],
        )

    async def executor(node, ctx, memory, graph_id):
        runs.append((node.node_id, node.goal))
        if node.goal == "A1":
            return {
                "node_id": node.node_id,
                "status": "failed",
                "failure_type": "boom",
                "failure_reason": "第一版失败",
            }
        return _success(node, ctx, memory, graph_id)

    async def run():
        scheduler = Scheduler(
            checkpoint_file=str(tmp_path / "ck.jsonl"),
            max_concurrency=2,
            max_attempt=3,
            planner=planner,
            node_executor=executor,
        )
        state = await scheduler.plan_and_execute("goal")
        return scheduler, state

    scheduler, state = asyncio.run(run())

    assert state.global_status is NodeStatus.SUCCESS
    assert scheduler.plan_attempt == 1
    assert scheduler.task_dag.version == 2
    assert scheduler.pae_state.phase is PAEPhase.DONE
    assert scheduler.pae_state.exhausted is False
    # b 在两版计划中签名一致：第一次成功后被复用，只应执行一次
    assert [node_id for node_id, _ in runs] == ["a", "b", "a", "d"]
    assert [node_id for node_id, _ in runs].count("b") == 1
    # 每层一个 Checkpoint，内存栈与 JSONL 一致
    lines = [
        line
        for line in Path(tmp_path / "ck.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert len(lines) == len(scheduler.check_point_stack) > 0
    assert [e["type"] for e in scheduler.events].count("plan/start") == 2
    assert scheduler.events[0]["type"] == "plan/start"
    assert scheduler.events[-1]["type"] == "plan/end"


def test_scheduler_blocks_downstream_and_exhausts_attempts(tmp_path):
    def planner(goal, ctx):
        return _plan(
            "g",
            [
                {"node_id": "a", "goal": "A", "depends_on": []},
                {"node_id": "b", "goal": "B", "depends_on": ["a"]},
            ],
        )

    async def executor(node, ctx, memory, graph_id):
        if node.node_id == "a":
            return {
                "node_id": "a",
                "status": "failed",
                "failure_type": "unhandled_exception",
                "failure_reason": "boom",
            }
        return _success(node, ctx, memory, graph_id)

    async def run():
        scheduler = Scheduler(
            checkpoint_file=str(tmp_path / "ck.jsonl"),
            max_attempt=1,
            planner=planner,
            node_executor=executor,
        )
        return scheduler, await scheduler.plan_and_execute("goal")

    scheduler, state = asyncio.run(run())

    assert state.global_status is NodeStatus.FAILED_FATAL
    assert state.node_states["a"].status is NodeStatus.FAILED_FATAL
    assert state.node_states["b"].status is NodeStatus.BLOCKED
    assert state.node_states["b"].failure_type == "upstream_blocked"
    assert scheduler.plan_exhausted is True
    assert scheduler.pae_state.exhausted is True


def test_submit_subgraph_leaves_unselected_nodes_pending_then_continues(tmp_path):
    runs: list[str] = []

    async def executor(node, ctx, memory, graph_id):
        runs.append(node.node_id)
        return _success(node, ctx, memory, graph_id)

    async def run():
        scheduler = Scheduler(
            checkpoint_file=str(tmp_path / "ck.jsonl"),
            max_attempt=1,
            node_executor=executor,
        )
        plan = _plan(
            "g",
            [
                {"node_id": "a", "goal": "A"},
                {"node_id": "b", "goal": "B", "depends_on": ["a"]},
                {"node_id": "c", "goal": "C"},
            ],
        )
        await scheduler.submit(plan, node_ids=["b"])
        first = {
            nid: st.status.value
            for nid, st in scheduler.dag_state.node_states.items()
        }
        await scheduler.execute_subgraph(["c"])
        second = {
            nid: st.status.value
            for nid, st in scheduler.dag_state.node_states.items()
        }
        return scheduler, first, second

    scheduler, first, second = asyncio.run(run())

    assert runs == ["a", "b", "c"]
    assert first == {"a": "success", "b": "success", "c": "pending"}
    assert second == {"a": "success", "b": "success", "c": "success"}
    # 子图执行不裁剪完整 DAG
    assert scheduler.task_dag.node_ids() == {"a", "b", "c"}


def test_checkpoint_rollback_and_resume_from_jsonl(tmp_path):
    runs: list[str] = []

    async def executor(node, ctx, memory, graph_id):
        runs.append(node.node_id)
        return _success(node, ctx, memory, graph_id)

    async def run():
        scheduler = Scheduler(
            checkpoint_file=str(tmp_path / "ck.jsonl"),
            max_attempt=2,
            node_executor=executor,
        )
        plan = _plan(
            "g",
            [
                {"node_id": "a", "goal": "A"},
                {"node_id": "b", "goal": "B", "depends_on": ["a"]},
            ],
        )
        await scheduler.submit(plan)
        rolled = scheduler.rollback_to_checkpoint(steps=1)
        lines_after_rollback = len(
            [
                line
                for line in Path(tmp_path / "ck.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
        )
        await scheduler.resume_from_jsonl()
        return scheduler, rolled, lines_after_rollback

    scheduler, rolled, lines_after_rollback = asyncio.run(run())

    assert rolled.layer_index == 0
    assert lines_after_rollback == 1
    assert scheduler.dag_state.global_status is NodeStatus.SUCCESS
    assert runs == ["a", "b", "b"]


@pytest.mark.parametrize("persist_sessions", [True, False])
def test_scheduler_node_agent_persistence(tmp_path, persist_sessions):
    """不注入 node_executor：走 TaskNode.run -> create_agent -> run_task 完整链路。

    persist_sessions=True（默认）时，每个节点按 attempt 落盘独立会话；
    False 时保持纯内存。两种模式都必须能完成 PAE。
    """

    class _NodeFakeLLM:
        def __init__(self):
            self.session = None
            # 浅拷贝会共享同一个 list，便于断言原始实例看到的调用次数
            self.calls: list[int] = []

        async def stream(self, messages, tools):
            self.calls.append(1)
            return (
                AssistantMessage(
                    id="a",
                    content=[
                        TextBlock(
                            content=(
                                '{"status":"success","output":{"ok":true},'
                                '"evidence":["node-a"],"self_check":true}'
                            )
                        )
                    ],
                ),
                "finish",
                None,
            )

    fake = _NodeFakeLLM()
    sessions_dir = tmp_path / "node-sessions"

    async def run():
        scheduler = Scheduler(
            checkpoint_file=str(tmp_path / "ck.jsonl"),
            max_attempt=1,
            tool_center=ToolCenter(),
            sessions_dir=str(sessions_dir),
            persist_sessions=persist_sessions,
            planner=lambda goal, ctx: _plan(
                "real-g", [{"node_id": "a", "goal": "A"}]
            ),
            llm_client=fake,
        )
        return scheduler, await scheduler.plan_and_execute("goal")

    scheduler, state = asyncio.run(run())

    assert state.global_status is NodeStatus.SUCCESS
    assert state.node_states["a"].output == {"ok": True}
    assert fake.calls == [1]
    if persist_sessions:
        session_file = sessions_dir / "real-g" / "real-g_a_att0.jsonl"
        assert session_file.exists()
        rows = [
            json.loads(line)
            for line in session_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert rows[0]["type"] == "turn/start"
        assert any(row["type"] == "assistant/message" for row in rows)
        assert rows[-1]["type"] == "turn/end"
    else:
        # 节点 Agent 纯内存：不产生会话文件（只有 checkpoint JSONL）
        assert not sessions_dir.exists()


def test_scheduler_keeps_per_attempt_node_sessions(tmp_path):
    """重规划时失败节点按 attempt 生成独立会话，不覆盖上一轮对话。"""

    class _FailThenSuccessLLM:
        def __init__(self):
            self.session = None
            self.calls: list[int] = []

        async def stream(self, messages, tools):
            self.calls.append(1)
            if len(self.calls) == 1:
                return (
                    AssistantMessage(
                        id="a1",
                        content=[
                            TextBlock(
                                content=(
                                    '{"status":"failed","output":{},'
                                    '"evidence":[],"self_check":false,'
                                    '"failure_type":"boom",'
                                    '"failure_reason":"first attempt"}'
                                )
                            )
                        ],
                    ),
                    "finish",
                    None,
                )
            return (
                AssistantMessage(
                    id="a2",
                    content=[
                        TextBlock(
                            content=(
                                '{"status":"success","output":{"ok":true},'
                                '"evidence":["retry"],"self_check":true}'
                            )
                        )
                    ],
                ),
                "finish",
                None,
            )

    fake = _FailThenSuccessLLM()
    sessions_dir = tmp_path / "node-sessions"

    async def run():
        scheduler = Scheduler(
            checkpoint_file=str(tmp_path / "ck.jsonl"),
            max_attempt=3,
            tool_center=ToolCenter(),
            sessions_dir=str(sessions_dir),
            persist_sessions=True,
            planner=lambda goal, ctx: _plan(
                "g", [{"node_id": "a", "goal": "A"}]
            ),
            llm_client=fake,
        )
        return scheduler, await scheduler.plan_and_execute("goal")

    scheduler, state = asyncio.run(run())

    assert state.global_status is NodeStatus.SUCCESS
    assert scheduler.plan_attempt == 1
    assert fake.calls == [1, 1]
    assert (sessions_dir / "g" / "g_a_att0.jsonl").exists()
    assert (sessions_dir / "g" / "g_a_att1.jsonl").exists()


def test_same_signature_tolerates_omitted_planner_fields():
    """重规划计划省略 acceptance/tool_scope 时，不应阻断成功节点复用。"""
    old = TaskNode(
        node_id="a",
        goal="A",
        acceptance_criteria="旧验收",
        tool_scope=["read", "write"],
        depends_on=[],
    )
    omitted = TaskNode(
        node_id="a",
        goal="A",
        acceptance_criteria="",
        tool_scope=[],
        depends_on=[],
    )
    assert Scheduler._same_signature(old, omitted) is True

    changed_acceptance = TaskNode(
        node_id="a",
        goal="A",
        acceptance_criteria="新验收",
        tool_scope=[],
        depends_on=[],
    )
    assert Scheduler._same_signature(old, changed_acceptance) is False

    narrowed_scope = TaskNode(
        node_id="a",
        goal="A",
        acceptance_criteria="旧验收",
        tool_scope=["read"],
        depends_on=[],
    )
    assert Scheduler._same_signature(old, narrowed_scope) is False

    added_dep = TaskNode(
        node_id="a",
        goal="A",
        acceptance_criteria="旧验收",
        tool_scope=["read", "write"],
        depends_on=["new_dep"],
    )
    assert Scheduler._same_signature(old, added_dep) is False


def test_replan_reuses_success_when_optional_fields_omitted(tmp_path):
    """重规划只重跑失败节点；成功节点在新计划省略字段时仍应复用。"""
    runs: list[str] = []

    def planner(goal, ctx):
        if not ctx.get("failed_nodes"):
            return _plan(
                "g1",
                [
                    {
                        "node_id": "a",
                        "goal": "A",
                        "acceptance_criteria": "a-ok",
                        "tool_scope": ["read"],
                    },
                    {
                        "node_id": "b",
                        "goal": "B",
                        "acceptance_criteria": "b-ok",
                        "tool_scope": ["read"],
                    },
                ],
            )
        # 模拟真实 planner：省略 acceptance_criteria / tool_scope
        return _plan(
            "g2",
            [
                {"node_id": "a", "goal": "A"},
                {"node_id": "b", "goal": "B"},
            ],
        )

    async def executor(node, ctx, memory, graph_id):
        runs.append(node.node_id)
        if node.node_id == "b" and runs.count("b") == 1:
            return {
                "node_id": "b",
                "status": "failed",
                "failure_type": "boom",
                "failure_reason": "first b failure",
            }
        return _success(node, ctx, memory, graph_id)

    async def run():
        scheduler = Scheduler(
            checkpoint_file=str(tmp_path / "ck.jsonl"),
            max_attempt=3,
            planner=planner,
            node_executor=executor,
        )
        return scheduler, await scheduler.plan_and_execute("goal")

    scheduler, state = asyncio.run(run())

    assert state.global_status is NodeStatus.SUCCESS
    assert scheduler.plan_attempt == 1
    # a 只执行一次；b 在 attempt0 失败、attempt1 重跑
    assert runs.count("a") == 1
    assert runs.count("b") == 2


def test_replan_invalidates_downstream_when_upstream_changes(tmp_path):
    """上游重跑后，下游旧 success 必须失效并随新输入重跑。"""
    runs: list[str] = []

    def planner(goal, ctx):
        if not ctx.get("failed_nodes"):
            return _plan(
                "g1",
                [
                    {"node_id": "a", "goal": "A0"},
                    {"node_id": "b", "goal": "B", "depends_on": ["a"]},
                    {"node_id": "c", "goal": "C", "depends_on": ["b"]},
                ],
            )
        return _plan(
            "g2",
            [
                {"node_id": "a", "goal": "A1"},
                {"node_id": "b", "goal": "B", "depends_on": ["a"]},
                {"node_id": "c", "goal": "C", "depends_on": ["b"]},
            ],
        )

    async def executor(node, ctx, memory, graph_id):
        runs.append(node.node_id)
        if node.node_id == "a":
            return {
                "node_id": "a",
                "status": "success",
                "output": {"v": node.goal},
                "self_check": True,
            }
        if node.node_id == "b":
            return {
                "node_id": "b",
                "status": "success",
                "output": {"seen": ctx["a"]["output"]["v"]},
                "self_check": True,
            }
        if ctx["b"]["output"]["seen"] == "A0":
            return {
                "node_id": "c",
                "status": "failed",
                "failure_type": "acceptance_failed",
                "failure_reason": "上游仍是旧产出",
            }
        return {
            "node_id": "c",
            "status": "success",
            "output": {"seen": ctx["b"]["output"]["seen"]},
            "self_check": True,
        }

    async def run():
        scheduler = Scheduler(
            checkpoint_file=str(tmp_path / "ck.jsonl"),
            max_attempt=3,
            planner=planner,
            node_executor=executor,
        )
        return scheduler, await scheduler.plan_and_execute("goal")

    scheduler, state = asyncio.run(run())

    assert state.global_status is NodeStatus.SUCCESS
    assert scheduler.plan_attempt == 1
    # b 首次成功；a 在第二次计划中 goal 变化，b 必须失效并重跑
    assert runs == ["a", "b", "c", "a", "b", "c"]
    assert state.node_states["b"].output == {"seen": "A1"}
    assert state.node_states["c"].output == {"seen": "A1"}


def test_replan_keeps_unrelated_success_nodes(tmp_path):
    """传递失效只影响上游变化的链路，旁支成功节点仍应复用。"""
    runs: list[str] = []

    def planner(goal, ctx):
        if not ctx.get("failed_nodes"):
            return _plan(
                "g1",
                [
                    {"node_id": "a", "goal": "A0"},
                    {"node_id": "side", "goal": "SIDE"},
                    {"node_id": "b", "goal": "B", "depends_on": ["a"]},
                    {"node_id": "c", "goal": "C", "depends_on": ["b"]},
                ],
            )
        return _plan(
            "g2",
            [
                {"node_id": "a", "goal": "A1"},
                {"node_id": "side", "goal": "SIDE"},
                {"node_id": "b", "goal": "B", "depends_on": ["a"]},
                {"node_id": "c", "goal": "C", "depends_on": ["b"]},
            ],
        )

    async def executor(node, ctx, memory, graph_id):
        runs.append(node.node_id)
        if node.node_id == "c":
            seen = ctx["b"]["output"]["seen"]
            if seen == "A0":
                return {
                    "node_id": "c",
                    "status": "failed",
                    "failure_type": "acceptance_failed",
                    "failure_reason": "上游仍是旧产出",
                }
            return {
                "node_id": "c",
                "status": "success",
                "output": {},
                "self_check": True,
            }
        if node.node_id == "b":
            return {
                "node_id": "b",
                "status": "success",
                "output": {"seen": ctx["a"]["output"]["v"]},
                "self_check": True,
            }
        if node.node_id == "a":
            return {
                "node_id": "a",
                "status": "success",
                "output": {"v": node.goal},
                "self_check": True,
            }
        return {
            "node_id": node.node_id,
            "status": "success",
            "output": {},
            "self_check": True,
        }

    async def run():
        scheduler = Scheduler(
            checkpoint_file=str(tmp_path / "ck.jsonl"),
            max_attempt=3,
            planner=planner,
            node_executor=executor,
        )
        return scheduler, await scheduler.plan_and_execute("goal")

    scheduler, state = asyncio.run(run())

    assert state.global_status is NodeStatus.SUCCESS
    assert runs.count("side") == 1
    assert runs.count("a") == 2
    assert runs.count("b") == 2


def test_resume_recovers_prior_successes_from_older_checkpoint(tmp_path):
    """恢复时应从旧 checkpoint 回收与当前 DAG 兼容的成功节点。"""
    old_dag = _make_dag(
        [
            {"node_id": "a", "goal": "A", "acceptance_criteria": "a-ok"},
            {"node_id": "b", "goal": "B", "acceptance_criteria": "b-ok"},
        ],
        graph_id="v2",
    )
    old_state = GraphState(
        graph_id="v2",
        dag_version=1,
        node_states={
            "a": NodeState(
                node_id="a",
                status=NodeStatus.SUCCESS,
                output={"a": 1},
                evidence=["old-a"],
            ),
            "b": NodeState(
                node_id="b",
                status=NodeStatus.FAILED_FATAL,
                failure_type="boom",
            ),
        },
    )
    latest_dag = _make_dag(
        [
            # 模拟 planner 省略 acceptance_criteria / tool_scope
            {"node_id": "a", "goal": "A"},
            {"node_id": "b", "goal": "B"},
        ],
        graph_id="v3",
    )
    latest_state = GraphState(
        graph_id="v3",
        dag_version=2,
        node_states={
            "a": NodeState(node_id="a"),
            "b": NodeState(node_id="b"),
        },
    )
    old_rec = CheckpointRecord(
        checkpoint_id="old",
        plan_attempt=1,
        dag_version=1,
        layer_index=0,
        task_dag=old_dag,
        dag_state=old_state,
    )
    latest_rec = CheckpointRecord(
        checkpoint_id="latest",
        plan_attempt=2,
        dag_version=2,
        layer_index=0,
        task_dag=latest_dag,
        dag_state=latest_state,
    )
    scheduler = Scheduler(checkpoint_file=str(tmp_path / "ck.jsonl"))
    scheduler.task_dag = latest_dag
    scheduler.dag_state = latest_state
    scheduler.plan_attempt = 2
    scheduler.check_point_stack = [old_rec, latest_rec]

    recovered = scheduler._recover_prior_successes()

    assert recovered == 1
    assert scheduler.dag_state.node_states["a"].status is NodeStatus.SUCCESS
    assert scheduler.dag_state.node_states["a"].output == {"a": 1}
    assert scheduler.dag_state.node_states["b"].status is NodeStatus.PENDING


def test_resume_does_not_recover_success_if_acceptance_changed(tmp_path):
    old_node = TaskNode(
        node_id="a", goal="A", acceptance_criteria="old", tool_scope=["read"]
    )
    new_node = TaskNode(
        node_id="a", goal="A", acceptance_criteria="new", tool_scope=["read"]
    )
    old_dag = TaskDAG(graph_id="v2", global_goal="g", nodes=[old_node])
    old_dag.init_dag()
    latest_dag = TaskDAG(graph_id="v3", global_goal="g", nodes=[new_node])
    latest_dag.init_dag()
    old_state = GraphState(
        graph_id="v2",
        node_states={
            "a": NodeState(
                node_id="a", status=NodeStatus.SUCCESS, output={"old": True}
            )
        },
    )
    latest_state = GraphState(
        graph_id="v3",
        node_states={"a": NodeState(node_id="a")},
    )
    scheduler = Scheduler(checkpoint_file=str(tmp_path / "ck.jsonl"))
    scheduler.task_dag = latest_dag
    scheduler.dag_state = latest_state
    scheduler.check_point_stack = [
        CheckpointRecord(
            checkpoint_id="old",
            plan_attempt=1,
            dag_version=1,
            layer_index=0,
            task_dag=old_dag,
            dag_state=old_state,
        ),
        CheckpointRecord(
            checkpoint_id="latest",
            plan_attempt=2,
            dag_version=2,
            layer_index=0,
            task_dag=latest_dag,
            dag_state=latest_state,
        ),
    ]

    assert scheduler._recover_prior_successes() == 0
    assert scheduler.dag_state.node_states["a"].status is NodeStatus.PENDING


def _snapshot(value: int, goal: str = "A") -> dict:
    return {
        "a": {
            "goal": goal,
            "status": "success",
            "output": {"v": value},
            "failure_type": None,
            "failure_reason": None,
        }
    }


def test_resume_does_not_recover_downstream_when_input_changed(tmp_path):
    """上游产出已变化时，旧 checkpoint 中的下游成功节点不得回收。"""
    old_dag = _make_dag(
        [
            {"node_id": "a", "goal": "A"},
            {"node_id": "b", "goal": "B", "depends_on": ["a"]},
        ],
        graph_id="v2",
    )
    latest_dag = _make_dag(
        [
            {"node_id": "a", "goal": "A"},
            {"node_id": "b", "goal": "B", "depends_on": ["a"]},
        ],
        graph_id="v3",
    )
    old_state = GraphState(
        graph_id="v2",
        node_states={
            "a": NodeState(
                node_id="a", status=NodeStatus.SUCCESS, output={"v": 0}
            ),
            "b": NodeState(
                node_id="b",
                status=NodeStatus.SUCCESS,
                output={"seen": 0},
                input_snapshot=_snapshot(0),
            ),
        },
    )
    latest_state = GraphState(
        graph_id="v3",
        node_states={
            "a": NodeState(
                node_id="a", status=NodeStatus.SUCCESS, output={"v": 1}
            ),
            "b": NodeState(node_id="b"),
        },
    )
    scheduler = Scheduler(checkpoint_file=str(tmp_path / "ck.jsonl"))
    scheduler.task_dag = latest_dag
    scheduler.dag_state = latest_state
    scheduler.plan_attempt = 2
    scheduler.check_point_stack = [
        CheckpointRecord(
            checkpoint_id="old",
            plan_attempt=1,
            dag_version=1,
            layer_index=0,
            task_dag=old_dag,
            dag_state=old_state,
        ),
        CheckpointRecord(
            checkpoint_id="latest",
            plan_attempt=2,
            dag_version=2,
            layer_index=0,
            task_dag=latest_dag,
            dag_state=latest_state,
        ),
    ]

    assert scheduler._recover_prior_successes() == 0
    assert scheduler.dag_state.node_states["b"].status is NodeStatus.PENDING
    assert scheduler.dag_state.node_states["a"].output == {"v": 1}


def test_resume_recovers_downstream_when_input_matches(tmp_path):
    """上游产出与旧输入快照一致时，下游成功节点仍可回收。"""
    old_dag = _make_dag(
        [
            {"node_id": "a", "goal": "A"},
            {"node_id": "b", "goal": "B", "depends_on": ["a"]},
        ],
        graph_id="v2",
    )
    latest_dag = _make_dag(
        [
            {"node_id": "a", "goal": "A"},
            {"node_id": "b", "goal": "B", "depends_on": ["a"]},
        ],
        graph_id="v3",
    )
    old_state = GraphState(
        graph_id="v2",
        node_states={
            "a": NodeState(
                node_id="a", status=NodeStatus.SUCCESS, output={"v": 1}
            ),
            "b": NodeState(
                node_id="b",
                status=NodeStatus.SUCCESS,
                output={"seen": 1},
                input_snapshot=_snapshot(1),
            ),
        },
    )
    latest_state = GraphState(
        graph_id="v3",
        node_states={
            "a": NodeState(
                node_id="a", status=NodeStatus.SUCCESS, output={"v": 1}
            ),
            "b": NodeState(node_id="b"),
        },
    )
    scheduler = Scheduler(checkpoint_file=str(tmp_path / "ck.jsonl"))
    scheduler.task_dag = latest_dag
    scheduler.dag_state = latest_state
    scheduler.plan_attempt = 2
    scheduler.check_point_stack = [
        CheckpointRecord(
            checkpoint_id="old",
            plan_attempt=1,
            dag_version=1,
            layer_index=0,
            task_dag=old_dag,
            dag_state=old_state,
        ),
        CheckpointRecord(
            checkpoint_id="latest",
            plan_attempt=2,
            dag_version=2,
            layer_index=0,
            task_dag=latest_dag,
            dag_state=latest_state,
        ),
    ]

    assert scheduler._recover_prior_successes() == 1
    assert scheduler.dag_state.node_states["b"].status is NodeStatus.SUCCESS
    assert scheduler.dag_state.node_states["b"].output == {"seen": 1}


def test_isolated_tool_center_always_clones_policy(tmp_path):
    """即使无 tool_scope / 无 auto_approve，节点也必须拿到独立 policy。"""
    from agent_test.app.scheduler import _isolated_tool_center
    from agent_test.policy import CommandPolicy

    base = ToolCenter(policy=CommandPolicy(allowed_roots=[tmp_path]))
    one = _isolated_tool_center(base, [], False)
    two = _isolated_tool_center(base, [], False)

    assert one is not base and two is not base and one is not two
    assert one.policy is not base.policy
    assert one.policy is not two.policy
    assert _isolated_tool_center(base, [], True).approver is not None


def test_task_node_exports_memory_on_failure(monkeypatch):
    """失败节点必须导出摘要与失败事实，并把 restored_memory 传下去。"""
    from agent_test.app import scheduler as scheduler_module

    class _Session:
        session_id = "sess-1"

        def derive_messages(self):
            return [
                UserMessage(id="u", content=[TextBlock(content="读取 a.txt")]),
                AssistantMessage(
                    id="a", content=[TextBlock(content="文件不存在")]
                ),
            ]

    class _Agent:
        def __init__(self):
            self.session = _Session()
            self.kwargs = {}

        async def run_task(self, **kwargs):
            self.kwargs = kwargs
            return {
                "status": "failed",
                "failure_type": "acceptance_failed",
                "failure_reason": "missing b.txt",
                "output": {"missing": "b.txt"},
                "evidence": [],
                "self_check": False,
            }

        def destroy(self):
            return None

    fake = _Agent()
    monkeypatch.setattr(
        scheduler_module, "create_agent", lambda **kwargs: fake
    )
    node = TaskNode(node_id="n1", goal="读取文件", tool_scope=["read"])
    result = asyncio.run(
        node.run(
            context_input={},
            restored_memory={"summary": "上一轮摘要"},
            tool_center=ToolCenter(),
            persist_sessions=False,
        )
    )

    assert fake.kwargs["restored_memory"] == {"summary": "上一轮摘要"}
    assert result.status == "failed"
    assert result.agent_memory_dump["status"] == "failed"
    assert result.agent_memory_dump["failure_reason"] == "missing b.txt"
    assert "读取 a.txt" in result.agent_memory_dump["summary"]


def test_reconcile_preserves_memory_on_fatal_failure(tmp_path):
    """fatal 失败也会进入重规划，重试前不应丢掉已导出的记忆摘要。"""
    old_dag = _make_dag([{"node_id": "a", "goal": "A"}])
    new_dag = _make_dag([{"node_id": "a", "goal": "A"}])
    scheduler = Scheduler(checkpoint_file=str(tmp_path / "ck.jsonl"))
    scheduler.task_dag = old_dag
    scheduler.dag_state = GraphState(
        graph_id="g",
        node_states={
            "a": NodeState(
                node_id="a",
                status=NodeStatus.FAILED_FATAL,
                failure_type="acceptance_failed",
                agent_memory_dump={"summary": "上次失败摘要"},
            )
        },
    )

    scheduler._reconcile_state(new_dag)

    state = scheduler.dag_state.node_states["a"]
    assert state.status is NodeStatus.PENDING
    assert state.agent_memory_dump == {"summary": "上次失败摘要"}


def test_final_layer_checkpoint_has_evaluated_global_status(tmp_path):
    """最后一层 Checkpoint 应记录 success/failed，而不是永远 running。"""

    async def executor(node, ctx, memory, graph_id):
        return _success(node, ctx, memory, graph_id)

    async def run():
        scheduler = Scheduler(
            checkpoint_file=str(tmp_path / "ck.jsonl"),
            max_attempt=1,
            node_executor=executor,
        )
        await scheduler.submit(
            _plan("g", [{"node_id": "a", "goal": "A"}])
        )
        return scheduler

    scheduler = asyncio.run(run())

    assert scheduler.dag_state.global_status is NodeStatus.SUCCESS
    assert (
        scheduler.check_point_stack[-1].dag_state.global_status
        is NodeStatus.SUCCESS
    )


def test_scheduler_max_turns_is_a_floor(tmp_path):
    """--max-turns 只提高节点 max_rounds，不降低 planner 的较高值。"""

    async def executor(node, ctx, memory, graph_id):
        return _success(node, ctx, memory, graph_id)

    async def run():
        scheduler = Scheduler(
            checkpoint_file=None,
            checkpoint_dir=str(tmp_path / "checkpoints"),
            max_attempt=1,
            node_executor=executor,
            max_turns=12,
        )
        await scheduler.submit(
            _plan(
                "g",
                [
                    {"node_id": "a", "goal": "A", "max_rounds": 3},
                    {"node_id": "b", "goal": "B", "max_rounds": 20},
                ],
            )
        )
        return scheduler

    scheduler = asyncio.run(run())

    nodes = scheduler.task_dag.node_map()
    assert nodes["a"].max_rounds == 12
    assert nodes["b"].max_rounds == 20


def test_max_turns_applies_to_restored_checkpoint_dag(tmp_path):
    dag = _make_dag(
        [{"node_id": "a", "goal": "A", "max_rounds": 3}],
        graph_id="g",
    )
    state = GraphState(
        graph_id="g", node_states={"a": NodeState(node_id="a")}
    )
    record = CheckpointRecord(
        checkpoint_id="c1",
        plan_attempt=0,
        dag_version=1,
        layer_index=0,
        task_dag=dag,
        dag_state=state,
    )
    scheduler = Scheduler(
        checkpoint_file=str(tmp_path / "ck.jsonl"),
        max_turns=11,
    )
    scheduler._restore_record(record)

    assert scheduler.task_dag.node_map()["a"].max_rounds == 11


def test_pae_fixture_plan_source(tmp_path):
    fixture = {
        "fixtures": [
            {
                "dag": _plan(
                    "fixture-g",
                    [{"node_id": "a", "goal": "A"}],
                )
            }
        ]
    }
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

    async def executor(node, ctx, memory, graph_id):
        return _success(node, ctx, memory, graph_id)

    async def run():
        scheduler = Scheduler(
            checkpoint_file=str(tmp_path / "ck.jsonl"),
            fixture_path=str(fixture_path),
            max_attempt=1,
            node_executor=executor,
        )
        return scheduler, await scheduler.plan_and_execute("goal")

    scheduler, state = asyncio.run(run())

    assert state.global_status is NodeStatus.SUCCESS
    assert scheduler.task_dag.graph_id == "fixture-g"


def test_execute_plan_and_execute_dag_entrypoints(tmp_path):
    async def executor(node, ctx, memory, graph_id):
        return _success(node, ctx, memory, graph_id)

    plan = PlanService.create_plan(
        "goal", [{"step_id": "a", "objective": "A"}]
    )
    dag = _make_dag([{"node_id": "x", "goal": "X"}], graph_id="direct")

    async def run():
        by_plan = Scheduler(
            checkpoint_file=str(tmp_path / "p.jsonl"),
            max_attempt=1,
            node_executor=executor,
        )
        plan_state = await by_plan.execute_plan(plan)
        by_dag = Scheduler(
            checkpoint_file=str(tmp_path / "d.jsonl"),
            max_attempt=1,
            node_executor=executor,
        )
        dag_state = await by_dag.execute_dag(dag)
        return plan_state, dag_state

    plan_state, dag_state = asyncio.run(run())

    assert plan_state.global_status is NodeStatus.SUCCESS
    assert plan_state.success_nodes == ["a"]
    assert dag_state.global_status is NodeStatus.SUCCESS
    assert dag_state.success_nodes == ["x"]


def test_blocked_node_can_resume_after_upstream_is_rerun(tmp_path):
    attempts = {"a": 0}

    async def executor(node, ctx, memory, graph_id):
        if node.node_id == "a":
            attempts["a"] += 1
            if attempts["a"] == 1:
                return {
                    "node_id": "a",
                    "status": "failed",
                    "failure_type": "unhandled_exception",
                    "failure_reason": "first failure",
                }
        return _success(node, ctx, memory, graph_id)

    async def run():
        scheduler = Scheduler(
            checkpoint_file=str(tmp_path / "ck.jsonl"),
            max_attempt=1,
            node_executor=executor,
        )
        plan = _plan(
            "g",
            [
                {"node_id": "a", "goal": "A"},
                {"node_id": "b", "goal": "B", "depends_on": ["a"]},
            ],
        )
        await scheduler.submit(plan)
        blocked = scheduler.dag_state.node_states["b"].status
        await scheduler.execute_subgraph(["a"])
        await scheduler.execute_subgraph(["b"])
        return scheduler, blocked

    scheduler, blocked = asyncio.run(run())

    assert blocked is NodeStatus.BLOCKED
    assert scheduler.dag_state.node_states["a"].status is NodeStatus.SUCCESS
    assert scheduler.dag_state.node_states["b"].status is NodeStatus.SUCCESS


# ---------------------------------------------------------------------
# 计划类型 / PlanService / CLI
# ---------------------------------------------------------------------


def test_plan_service_create_and_convert():
    plan = PlanService.create_plan(
        "完成报告",
        [
            {"step_id": "a", "objective": "收集资料"},
            {"step_id": "b", "objective": "写报告", "depends_on": ["a"]},
        ],
    )
    assert isinstance(plan, Plan)
    assert plan.status is PlanStatus.READY
    assert [step.step_id for step in plan.steps] == ["a", "b"]

    dag = plan.to_dag_dict()
    assert dag["graph_id"] == plan.plan_id
    assert dag["global_goal"] == "完成报告"
    assert dag["nodes"][1]["depends_on"] == ["a"]

    restored = Plan.from_dag_dict(dag)
    assert [step.step_id for step in restored.steps] == ["a", "b"]

    normalized = PlanService.normalize_plan_dict(
        {"nodes": [{"node_id": "x"}]}, goal="g"
    )
    assert normalized["nodes"][0]["acceptance_criteria"] == ""
    assert normalized["global_goal"] == "g"

    with pytest.raises(ValueError, match="重复"):
        PlanService.normalize_plan_dict(
            {"nodes": [{"node_id": "x"}, {"node_id": "x"}]}
        )


def test_plan_requires_steps_and_valid_dependencies():
    with pytest.raises(ValidationError):
        Plan(goal="g", steps=[])
    with pytest.raises(ValidationError, match="不存在"):
        Plan(
            goal="g",
            steps=[PlanStep(step_id="a", objective="A", depends_on=["x"])],
        )


def test_cli_parses_task_mode_flags():
    args = parse_args(
        [
            "--task",
            "生成报告",
            "--max-concurrency",
            "2",
            "--max-plan-attempt",
            "4",
            "--auto-approve",
            "--task-persist",
        ]
    )
    assert args.task == "生成报告"
    assert args.max_concurrency == 2
    assert args.max_plan_attempt == 4
    assert args.auto_approve is True
    assert args.task_persist is True
    # 默认持久化，显式 --no-task-persist 才回到纯内存
    assert parse_args(["--task", "x"]).task_persist is True
    assert parse_args(["--task", "x", "--no-task-persist"]).task_persist is False
    # 断点恢复：不要求 --task，从 checkpoint 继续
    resume_args = parse_args(["--resume-checkpoint"])
    assert resume_args.resume_checkpoint is True
    assert resume_args.task is None

    max_turns_args = parse_args(
        ["--task", "x", "--max-turns", "20"]
    )
    assert max_turns_args.max_turns == 20
    assert parse_args(["--task", "x", "--max-turn", "15"]).max_turns == 15
