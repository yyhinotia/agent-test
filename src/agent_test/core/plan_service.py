"""计划服务：把用户目标 / LLM 输出规范化为 Plan 或 Scheduler DAG 字典。

职责边界（轻量封装，便于替换规划器）：
- 输入可能是 Plan、PlanStep 列表、LLM 输出的 DAG 字典或 fixture 字典；
- 输出统一为「至少包含 nodes 的 DAG 字典」或 Plan；
- 不直接调用 LLM；LLM 规划仍由 Scheduler._llm_plan 负责（可注入 planner）。
"""
from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable, Iterable

from agent_test.types.plans import Plan, PlanStatus, PlanStep
from agent_test.utils import get_uuid

Planner = Callable[[str, dict], dict | Awaitable[dict]]


class PlanService:
    """计划创建与规范化入口。"""

    @staticmethod
    def create_plan(
        goal: str,
        steps: Iterable[PlanStep | dict[str, Any]],
        *,
        plan_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Plan:
        """由目标与步骤创建 Plan（步骤可为 PlanStep 或字典）。"""
        parsed: list[PlanStep] = []
        for index, step in enumerate(steps, 1):
            if isinstance(step, PlanStep):
                parsed.append(step)
                continue
            data = dict(step)
            # 兼容 DAG 风格字段（node_id / goal）与计划风格字段（step_id / objective）
            data.setdefault(
                "step_id", data.get("node_id") or f"step_{index}"
            )
            data.setdefault("objective", data.get("goal", ""))
            data.setdefault("title", "")
            parsed.append(PlanStep(**data))
        return Plan(
            plan_id=plan_id or f"plan_{get_uuid()[:8]}",
            goal=goal,
            steps=parsed,
            metadata=metadata or {},
        )

    @staticmethod
    def normalize_plan_dict(
        plan_data: dict[str, Any] | Plan,
        *,
        goal: str | None = None,
    ) -> dict[str, Any]:
        """补全 DAG 字典的缺省字段，供 Scheduler.submit 直接消费。

        兼容 Plan.to_dag_dict()、LLM 输出与 fixture 三种来源；不改变
        调用方传入的 node goal / acceptance_criteria。
        """
        if isinstance(plan_data, Plan):
            plan_data = plan_data.to_dag_dict()
        if not isinstance(plan_data, dict):
            raise ValueError("计划必须是 dict 或 Plan")
        nodes = plan_data.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            raise ValueError("计划必须包含非空 nodes 列表")

        normalized_nodes: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, raw in enumerate(nodes, 1):
            if not isinstance(raw, dict):
                raise ValueError(f"节点 {index} 必须是 dict")
            node = dict(raw)
            node_id = str(node.get("node_id") or f"step_{index}")
            if node_id in seen:
                raise ValueError(f"计划中存在重复 node_id: {node_id}")
            seen.add(node_id)
            node["node_id"] = node_id
            node.setdefault("goal", "")
            node.setdefault("acceptance_criteria", "")
            node.setdefault("depends_on", [])
            node.setdefault("tool_scope", [])
            node.setdefault("max_rounds", 5)
            node.setdefault("priority", 0)
            node.setdefault("metadata", {})
            # LLM 有时会显式输出 null：统一收敛为容器类型，避免 Pydantic 校验失败
            if node["depends_on"] is None:
                node["depends_on"] = []
            if node["tool_scope"] is None:
                node["tool_scope"] = []
            if node["metadata"] is None:
                node["metadata"] = {}
            normalized_nodes.append(node)

        return {
            "graph_id": str(
                plan_data.get("graph_id") or f"dag_{get_uuid()[:8]}"
            ),
            "global_goal": str(
                plan_data.get("global_goal") or goal or ""
            ),
            "nodes": normalized_nodes,
        }

    @staticmethod
    def plan_from_dag(
        plan_data: dict[str, Any], *, status: PlanStatus = PlanStatus.READY
    ) -> Plan:
        """把 DAG 字典收敛为 Plan。"""
        normalized = PlanService.normalize_plan_dict(plan_data)
        plan = Plan.from_dag_dict(normalized)
        plan.status = status
        return plan

    @staticmethod
    async def plan_with(
        planner: Planner,
        goal: str,
        replan_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """调用注入的规划器（同步或异步）并规范化其输出。"""
        result = planner(goal, replan_context or {})
        if inspect.isawaitable(result):
            result = await result
        return PlanService.normalize_plan_dict(result, goal=goal)


__all__ = ["PlanService", "Planner"]
