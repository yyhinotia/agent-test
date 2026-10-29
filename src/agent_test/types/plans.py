"""计划类型：Plan / PlanStep 及状态枚举（调度器的上游数据契约）。

分层关系::

    Plan（用户目标 + 线性/无环步骤）
      └─ PlanStep（单步目标 + 验收标准 + 依赖 + 工具范围）
            └─ Scheduler.TaskNode（DAG 执行单元）

`Plan.to_dag_dict()` 把计划转换为 Scheduler 可直接 submit 的 DAG 字典；
`Plan.from_dag_dict()` 反向把 LLM 生成的 DAG 计划收敛为 Plan，便于展示、
持久化或人工审阅。
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from agent_test.utils import get_uuid


class PlanStatus(str, Enum):
    """计划整体生命周期。"""

    DRAFT = "draft"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(str, Enum):
    """计划步骤生命周期（与调度器 NodeStatus 映射见 to_node_status）。"""

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class PlanStep(BaseModel):
    """计划中的单步任务定义。"""

    step_id: str
    title: str = ""
    objective: str
    acceptance_criteria: str = ""
    depends_on: list[str] = Field(default_factory=list)
    tool_scope: list[str] = Field(default_factory=list)
    max_rounds: int = 5
    priority: int = 0
    status: StepStatus = StepStatus.PENDING
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("depends_on")
    @classmethod
    def _deduplicate_deps(cls, value: list[str]) -> list[str]:
        """去重依赖，避免拓扑排序入度计算翻倍。"""
        return list(dict.fromkeys(value))

    def to_task_node(self) -> dict[str, Any]:
        """转换为 Scheduler.TaskNode 兼容字典。"""
        return {
            "node_id": self.step_id,
            "goal": self.objective,
            "acceptance_criteria": self.acceptance_criteria,
            "depends_on": list(self.depends_on),
            "tool_scope": list(self.tool_scope),
            "max_rounds": self.max_rounds,
            "priority": self.priority,
            "metadata": dict(self.metadata),
        }


class Plan(BaseModel):
    """一次计划：目标 + 步骤集合（DAG 由 steps.depends_on 表达）。"""

    plan_id: str = Field(default_factory=lambda: f"plan_{get_uuid()[:8]}")
    goal: str
    steps: list[PlanStep]
    status: PlanStatus = PlanStatus.READY
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("steps")
    @classmethod
    def _non_empty(cls, value: list[PlanStep]) -> list[PlanStep]:
        if not value:
            raise ValueError("Plan 至少包含一个步骤")
        ids = [step.step_id for step in value]
        if len(ids) != len(set(ids)):
            raise ValueError("Plan 中存在重复的 step_id")
        known = set(ids)
        for step in value:
            missing = [dep for dep in step.depends_on if dep not in known]
            if missing:
                raise ValueError(
                    f"步骤 {step.step_id} 依赖了不存在的步骤: {missing}"
                )
        return value

    def to_dag_dict(self, *, graph_id: str | None = None) -> dict[str, Any]:
        """转换为 Scheduler 可提交的 DAG 字典。"""
        return {
            "graph_id": graph_id or self.plan_id,
            "global_goal": self.goal,
            "nodes": [step.to_task_node() for step in self.steps],
        }

    @classmethod
    def from_dag_dict(cls, plan_data: dict[str, Any]) -> "Plan":
        """把 DAG 计划字典收敛为 Plan（用于展示 / 审阅 / 持久化）。"""
        steps: list[PlanStep] = []
        for node in plan_data.get("nodes", []):
            steps.append(
                PlanStep(
                    step_id=node["node_id"],
                    title=node.get("metadata", {}).get("title", ""),
                    objective=node.get("goal", ""),
                    acceptance_criteria=node.get("acceptance_criteria", ""),
                    depends_on=node.get("depends_on", []),
                    tool_scope=node.get("tool_scope", []),
                    max_rounds=node.get("max_rounds", 5),
                    priority=node.get("priority", 0),
                    metadata=node.get("metadata", {}),
                )
            )
        return cls(
            plan_id=plan_data.get("graph_id") or f"plan_{get_uuid()[:8]}",
            goal=plan_data.get("global_goal", ""),
            steps=steps,
        )


__all__ = ["Plan", "PlanStatus", "PlanStep", "StepStatus"]
