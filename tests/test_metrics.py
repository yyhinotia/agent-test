import asyncio

from agent_test import Evaluator, Scheduler, create_agent
from agent_test.tools.center import ToolCenter
from agent_test.types.messages import AssistantMessage, TextBlock, UserMessage


class _UsageLLM:
    def __init__(self, usages):
        self.session = None
        self.calls = 0
        self.usages = list(usages)

    async def stream(self, messages, tools):
        usage = self.usages[min(self.calls, len(self.usages) - 1)]
        self.calls += 1
        return (
            AssistantMessage(
                id=f"a{self.calls}",
                content=[TextBlock(content="done")],
            ),
            "finish",
            usage,
        )


def test_evaluator_from_agent_accumulates_tokens_and_turns():
    llm = _UsageLLM(
        [
            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            {"prompt_tokens": 20, "completion_tokens": 7, "total_tokens": 27},
        ]
    )
    agent = create_agent(
        persist=False,
        llm_client=llm,
        tools=ToolCenter(),
        max_turns=1,
    )
    try:
        agent.inbox.append(
            "turn", UserMessage(id="u1", content=[TextBlock(content="hi")])
        )
        assert asyncio.run(agent.turn()) is True
        agent.inbox.append(
            "turn", UserMessage(id="u2", content=[TextBlock(content="again")])
        )
        assert asyncio.run(agent.turn()) is True

        metrics = Evaluator.from_agent(agent)
    finally:
        agent.destroy()

    assert metrics.turns == 2
    assert metrics.steps == 2
    assert metrics.llm_calls == 2
    assert metrics.prompt_tokens == 30
    assert metrics.completion_tokens == 12
    assert metrics.total_tokens == 42
    assert metrics.elapsed_seconds >= 0.0


class _PlanLLM:
    def __init__(self, text, usage):
        self.session = None
        self.text = text
        self.usage = usage
        self.calls = 0

    async def stream(self, messages, tools):
        self.calls += 1
        return (
            AssistantMessage(
                id="plan", content=[TextBlock(content=self.text)]
            ),
            "finish",
            self.usage,
        )


def _success(node_id: str, tokens: int = 12) -> dict:
    return {
        "node_id": node_id,
        "status": "success",
        "output": {},
        "self_check": True,
        "turns": 1,
        "steps": 1,
        "llm_calls": 1,
        "tool_calls": 0,
        "prompt_tokens": tokens - 2,
        "completion_tokens": 2,
        "total_tokens": tokens,
    }


def test_evaluator_from_task_counts_replans_and_average(tmp_path):
    runs: dict[str, int] = {}

    def planner(goal, ctx):
        return {
            "graph_id": "metrics-g",
            "global_goal": goal,
            "nodes": [
                {"node_id": "a", "goal": "A"},
                {"node_id": "b", "goal": "B"},
            ],
        }

    async def executor(node, ctx, memory, graph_id):
        runs[node.node_id] = runs.get(node.node_id, 0) + 1
        if node.node_id == "a" and runs[node.node_id] == 1:
            result = _success("a", 12)
            result.update(
                {
                    "status": "failed",
                    "failure_type": "boom",
                    "failure_reason": "first attempt",
                }
            )
            return result
        return _success(node.node_id, 12)

    async def run():
        scheduler = Scheduler(
            checkpoint_file=str(tmp_path / "ck.jsonl"),
            max_attempt=2,
            planner=planner,
            node_executor=executor,
        )
        state = await scheduler.plan_and_execute("goal")
        return scheduler, state

    scheduler, state = asyncio.run(run())
    metrics = Evaluator.from_task(scheduler)

    assert state.global_status.value == "success"
    assert metrics.node_count == 2
    assert metrics.node_executions == 3
    assert metrics.average_node_executions == 1.5
    assert metrics.node_turns == 3
    assert metrics.total_turns == 3
    assert metrics.total_tokens == 36
    assert metrics.reused_node_count == 1
    assert metrics.replanned_node_count == 2
    assert metrics.node_reuse_rate == 0.5
    assert metrics.critical_path_turns == 2
    assert metrics.elapsed_seconds >= 0.0
    assert metrics.nodes["a"].executions == 2
    assert metrics.nodes["b"].executions == 1


def test_agent_metrics_merges_compactor_usage():
    class _Compactor:
        def __init__(self):
            self.usage_totals = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }
            self.llm_calls = 0
            self.estimated_usage_calls = 0

        async def compact(self):
            self.usage_totals.update(
                {
                    "prompt_tokens": 40,
                    "completion_tokens": 10,
                    "total_tokens": 50,
                }
            )
            self.llm_calls = 1
            return "summary"

    agent = create_agent(
        persist=False,
        llm_client=_UsageLLM(
            [{"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}]
        ),
        tools=ToolCenter(),
    )
    agent.compactor = _Compactor()
    try:
        asyncio.run(agent._compact_context())
        metrics = Evaluator.from_agent(agent)
    finally:
        agent.destroy()

    assert metrics.prompt_tokens == 40
    assert metrics.completion_tokens == 10
    assert metrics.total_tokens == 50
    assert metrics.llm_calls == 1


def test_subagent_usage_is_merged_into_parent():
    from agent_test.tools.subagent import register_subagent

    llm = _UsageLLM(
        [{"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}]
    )
    center = ToolCenter()
    parent = create_agent(
        persist=False,
        llm_client=llm,
        tools=center,
    )
    register_subagent(center, parent)
    try:
        result = asyncio.run(
            center.execute(
                "subagent", {"task_description": "计算 6*7"}
            )
        )
        metrics = Evaluator.from_agent(parent)
    finally:
        parent.destroy()

    assert result["is_error"] is False
    assert metrics.total_tokens == 15
    assert metrics.llm_calls == 1


def test_evaluator_task_includes_planner_usage(tmp_path):
    plan_text = (
        '{"graph_id":"planner-g","global_goal":"goal",'
        '"nodes":[{"node_id":"a","goal":"A"}]}'
    )
    planner_llm = _PlanLLM(
        plan_text,
        {"prompt_tokens": 30, "completion_tokens": 5, "total_tokens": 35},
    )
    planner_agent = create_agent(
        persist=False,
        llm_client=planner_llm,
        tools=ToolCenter(),
        max_turns=1,
    )

    async def executor(node, ctx, memory, graph_id):
        return _success(node.node_id, 12)

    async def run():
        scheduler = Scheduler(
            checkpoint_file=str(tmp_path / "ck.jsonl"),
            max_attempt=1,
            planner_agent=planner_agent,
            node_executor=executor,
        )
        state = await scheduler.plan_and_execute("goal")
        return scheduler, state

    scheduler, state = asyncio.run(run())
    metrics = Evaluator.from_task(scheduler)

    assert state.global_status.value == "success"
    assert metrics.planner_turns == 1
    assert metrics.planner_tokens == 35
    assert metrics.total_tokens == 47
    assert metrics.total_turns == 2
    assert metrics.critical_path_turns == 2
    assert metrics.reused_node_count == 0
    assert metrics.node_reuse_rate == 0.0
