"""可观测指标：Agent / Task 用量与执行次数测评。"""
from dataclasses import asdict, dataclass, field
import time
from typing import Any, Dict


METRIC_KEYS = (
    "turns",
    "steps",
    "llm_calls",
    "tool_calls",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "estimated_calls",
)


def zero_metrics() -> Dict[str, int]:
    """返回一份全零指标，供 Agent / Scheduler 累计使用。"""
    return {key: 0 for key in METRIC_KEYS}


def add_metrics(target: Dict[str, int], source: Dict[str, int]) -> None:
    """把 source 累加到 target（缺失键按 0 处理）。"""
    for key in METRIC_KEYS:
        target[key] = int(target.get(key, 0)) + int(source.get(key, 0) or 0)


def agent_metric_values(agent: Any) -> Dict[str, int]:
    """从 ReactAgent 读取累计指标；兼容旧对象/测试替身。"""
    usage = getattr(agent, "usage_totals", None) or {}
    phase = getattr(agent, "phase", None)
    turns_raw = getattr(agent, "turns_executed", None)
    turns = int(turns_raw) if turns_raw is not None else int(
        getattr(phase, "turn", 0) or 0
    )
    values = zero_metrics()
    values.update(
        {
            "turns": turns,
            "steps": int(getattr(agent, "steps_executed", 0) or 0),
            "llm_calls": int(getattr(agent, "llm_calls", 0) or 0),
            "tool_calls": int(
                getattr(agent, "tool_calls_executed", 0) or 0
            ),
            "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(
                usage.get("completion_tokens", 0) or 0
            ),
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
            "estimated_calls": int(
                getattr(agent, "estimated_usage_calls", 0) or 0
            ),
        }
    )
    if values["total_tokens"] <= 0:
        values["total_tokens"] = (
            values["prompt_tokens"] + values["completion_tokens"]
        )
    return values


def result_metric_values(result: Any) -> Dict[str, int]:
    """从 StepResult（或 executor 返回的 dict）读取本次执行指标。"""
    values = zero_metrics()
    steps = int(getattr(result, "steps", 0) or 0)
    prompt = int(getattr(result, "prompt_tokens", 0) or 0)
    completion = int(getattr(result, "completion_tokens", 0) or 0)
    total = int(getattr(result, "total_tokens", 0) or 0)
    values.update(
        {
            "turns": int(
                getattr(result, "turns", 0)
                or getattr(result, "rounds_used", 0)
                or 0
            ),
            "steps": steps,
            "llm_calls": int(
                getattr(result, "llm_calls", 0) or steps
            ),
            "tool_calls": int(getattr(result, "tool_calls", 0) or 0),
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total or (prompt + completion),
            "estimated_calls": int(
                getattr(result, "estimated_calls", 0) or 0
            ),
        }
    )
    return values


@dataclass(frozen=True)
class AgentMetrics:
    """单个 ReactAgent 的累计指标。"""

    session_id: str = ""
    turns: int = 0
    steps: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_calls: int = 0
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class NodeMetrics:
    """Task 中单个节点的累计指标。"""

    node_id: str
    executions: int = 0
    turns: int = 0
    steps: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_calls: int = 0
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TaskMetrics:
    """一次 Task（Scheduler 进程内）的聚合指标。

    - total_tokens: planner + 所有节点执行的总 token；
    - total_turns: planner + 所有节点执行的总 turn 数；
    - node_executions: 节点实际执行次数（复用/跳过不计）；
    - average_node_executions: node_executions / 最终 DAG 节点数；
    - average_node_turns: node_turns / 最终 DAG 节点数。
    """

    graph_id: str = ""
    node_count: int = 0
    node_executions: int = 0
    average_node_executions: float = 0.0
    node_turns: int = 0
    average_node_turns: float = 0.0
    planner_turns: int = 0
    total_turns: int = 0
    node_tokens: int = 0
    planner_tokens: int = 0
    total_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_calls: int = 0
    elapsed_seconds: float = 0.0
    node_elapsed_seconds: float = 0.0
    reused_node_count: int = 0
    replanned_node_count: int = 0
    node_reuse_rate: float = 0.0
    critical_path_turns: int = 0
    nodes: Dict[str, NodeMetrics] = field(default_factory=dict)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["nodes"] = {
            node_id: node.to_dict() for node_id, node in self.nodes.items()
        }
        return data


class Evaluator:
    """从 ReactAgent / Scheduler 收集并汇总指标。"""

    @staticmethod
    def from_agent(agent: Any) -> AgentMetrics:
        values = agent_metric_values(agent)
        session = getattr(agent, "session", None)
        return AgentMetrics(
            session_id=str(getattr(session, "session_id", "") or ""),
            elapsed_seconds=float(
                getattr(agent, "elapsed_seconds", 0.0) or 0.0
            ),
            **values,
        )

    @classmethod
    def from_task(cls, scheduler: Any) -> TaskMetrics:
        dag = getattr(scheduler, "task_dag", None)
        node_count = len(getattr(dag, "nodes", []) or [])
        counts: Dict[str, int] = dict(
            getattr(scheduler, "_node_execution_counts", {}) or {}
        )
        usage: Dict[str, Dict[str, int]] = dict(
            getattr(scheduler, "_node_usage", {}) or {}
        )
        planner: Dict[str, int] = dict(
            getattr(scheduler, "_planner_usage", {}) or {}
        )
        elapsed_map: Dict[str, float] = dict(
            getattr(scheduler, "_node_elapsed", {}) or {}
        )
        node_ids = sorted(
            set(getattr(dag, "node_ids", lambda: set())())
            | set(counts)
            | set(usage)
        )
        nodes: Dict[str, NodeMetrics] = {}
        for node_id in node_ids:
            values = dict(usage.get(node_id) or zero_metrics())
            nodes[node_id] = NodeMetrics(
                node_id=node_id,
                executions=int(counts.get(node_id, 0) or 0),
                elapsed_seconds=float(elapsed_map.get(node_id, 0.0) or 0.0),
                **values,
            )

        node_turns = sum(node.turns for node in nodes.values())
        node_tokens = sum(node.total_tokens for node in nodes.values())
        prompt_tokens = sum(node.prompt_tokens for node in nodes.values()) + int(
            planner.get("prompt_tokens", 0) or 0
        )
        completion_tokens = sum(
            node.completion_tokens for node in nodes.values()
        ) + int(planner.get("completion_tokens", 0) or 0)
        estimated_calls = sum(
            node.estimated_calls for node in nodes.values()
        ) + int(planner.get("estimated_calls", 0) or 0)
        node_executions = sum(counts.values())
        planner_turns = int(planner.get("turns", 0) or 0)
        planner_tokens = int(planner.get("total_tokens", 0) or 0)
        node_elapsed_seconds = sum(
            node.elapsed_seconds for node in nodes.values()
        )

        reused_by_attempt: Dict[int, set] = dict(
            getattr(scheduler, "_reused_by_attempt", {}) or {}
        )
        attempt_counts: Dict[int, int] = dict(
            getattr(scheduler, "_attempt_node_counts", {}) or {}
        )
        reused_node_count = sum(len(value) for value in reused_by_attempt.values())
        replanned_node_count = sum(attempt_counts.values())
        node_reuse_rate = (
            reused_node_count / replanned_node_count
            if replanned_node_count
            else 0.0
        )

        attempt_layers: Dict[int, list] = dict(
            getattr(scheduler, "_attempt_layers", {}) or {}
        )
        attempt_turns: Dict[int, Dict[str, int]] = dict(
            getattr(scheduler, "_attempt_turns", {}) or {}
        )
        planner_turns_by_attempt: Dict[int, int] = dict(
            getattr(scheduler, "_planner_turns_by_attempt", {}) or {}
        )
        if attempt_layers:
            critical_path_turns = 0
            for attempt in (
                set(attempt_layers)
                | set(attempt_turns)
                | set(planner_turns_by_attempt)
            ):
                critical_path_turns += int(
                    planner_turns_by_attempt.get(attempt, 0) or 0
                )
                turns = attempt_turns.get(attempt, {})
                for layer in attempt_layers.get(attempt, []):
                    critical_path_turns += max(
                        (
                            int(turns.get(node_id, 0) or 0)
                            for node_id in layer
                        ),
                        default=0,
                    )
        else:
            critical_path_turns = planner_turns
            for layer in getattr(dag, "layers", []) or []:
                critical_path_turns += max(
                    (
                        int(nodes[node.node_id].turns)
                        for node in layer
                        if node.node_id in nodes
                    ),
                    default=0,
                )

        elapsed_seconds = getattr(scheduler, "task_elapsed_seconds", None)
        if elapsed_seconds is None:
            started = getattr(scheduler, "_task_started", None)
            elapsed_seconds = (
                time.perf_counter() - started if started is not None else 0.0
            )

        return TaskMetrics(
            graph_id=str(getattr(dag, "graph_id", "") or ""),
            node_count=node_count,
            node_executions=node_executions,
            average_node_executions=(
                node_executions / node_count if node_count else 0.0
            ),
            node_turns=node_turns,
            average_node_turns=(
                node_turns / node_count if node_count else 0.0
            ),
            planner_turns=planner_turns,
            total_turns=planner_turns + node_turns,
            node_tokens=node_tokens,
            planner_tokens=planner_tokens,
            total_tokens=planner_tokens + node_tokens,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            estimated_calls=estimated_calls,
            elapsed_seconds=float(elapsed_seconds or 0.0),
            node_elapsed_seconds=node_elapsed_seconds,
            reused_node_count=reused_node_count,
            replanned_node_count=replanned_node_count,
            node_reuse_rate=node_reuse_rate,
            critical_path_turns=critical_path_turns,
            nodes=nodes,
        )

    @classmethod
    def snapshot(
        cls,
        *,
        agent: Any | None = None,
        scheduler: Any | None = None,
    ) -> dict:
        """一次性收集 Agent / Task 指标，返回可 JSON 序列化的 dict。"""
        data: dict = {}
        if agent is not None:
            data["agent"] = cls.from_agent(agent).to_dict()
        if scheduler is not None:
            data["task"] = cls.from_task(scheduler).to_dict()
        return data


__all__ = [
    "AgentMetrics",
    "Evaluator",
    "NodeMetrics",
    "TaskMetrics",
    "add_metrics",
    "agent_metric_values",
    "result_metric_values",
    "zero_metrics",
]
