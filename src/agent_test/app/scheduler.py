"""DAG 调度器：Plan-Act-Evaluate/Replan（PAE）闭环 + 子图执行 + Checkpoint。

核心能力
--------
- DAG 拓扑分层（Kahn + priority 降序），同层并发、失败上游标记 BLOCKED；
- 计划三级降级：fixture -> 注入 planner -> LLM planner -> 内置兜底 DAG；
- Layer 级 Checkpoint（内存栈 + JSONL 双写），支持 rollback / resume；
- 重规划状态对齐：成功节点按依赖闭包与输入快照复用，失效节点重置；
- 子图执行：`TaskDAG.subgraph` / `Scheduler.execute_subgraph` 只跑指定节点的
  上游闭包，不触碰图外节点状态；
- PAE 状态机：`PAEPhase` + `PAEState` 记录每次 PLAN/ACT/EVALUATE/REPLAN。
"""
from __future__ import annotations

import asyncio
import copy
import inspect
import json
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Literal

from pydantic import BaseModel, Field, field_validator

from agent_test.core.agent import ReactAgent, create_agent
from agent_test.core.plan_service import PlanService
from agent_test.core.prompts import dag_planner_prompt
from agent_test.human.service import AutoApproveService
from agent_test.metrics.evaluator import (
    add_metrics,
    agent_metric_values,
    result_metric_values,
    zero_metrics,
)
from agent_test.log.runtime_log import RuntimeLog
from agent_test.tools.center import ToolCenter
from agent_test.types.events import EventType
from agent_test.types.messages import TextBlock, UserMessage
from agent_test.types.plans import Plan
from agent_test.utils import get_uuid

_LOG = "scheduler"

# 可恢复失败类型：重规划时保留节点专属记忆，重置为 PENDING 后重试。
_RECOVERABLE_FAILURES = frozenset(
    {"tool_timeout", "rate_limit", "transient_error"}
)

# 会话 id / 文件名安全字符（Windows 不允许 : * ? " < > | / \）
_SAFE_ID_PATTERN = re.compile(r"[^0-9A-Za-z_.-]+")


def _safe_session_id(value: str) -> str:
    """把 graph_id/node_id 组合规范化为安全的会话文件名。"""
    return _SAFE_ID_PATTERN.sub("_", value).strip("._") or "dag"


def build_node_session_id(
    graph_id: str, node_id: str, plan_attempt: int = 0
) -> str:
    """节点 Agent 的持久化会话 id：{graph}_{node}_att{N}。

    带 attempt 后缀是为了在重规划时保留每次尝试的完整对话，不覆盖前一版
    失败节点的会话；runtime.log 的 session 字段可直接对照到 planner/attempt。
    """
    return _safe_session_id(f"{graph_id}_{node_id}_att{plan_attempt}")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# =====================================================================
# 1. 核心状态与返回模型
# =====================================================================


class StepResult(BaseModel):
    """执行单元返回给调度器的结构化结果，NodeState 由此派生。"""

    node_id: str
    status: Literal["success", "partial", "failed"]
    output: dict = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)
    rounds_used: int = 0
    turns: int = 0
    steps: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_calls: int = 0
    elapsed_seconds: float = 0.0
    self_check: bool = False
    failure_type: str | None = None
    failure_reason: str | None = None
    agent_memory_dump: dict = Field(default_factory=dict)


class NodeStatus(str, Enum):
    """单个节点 / 整图的运行时状态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED_RECOVERABLE = "failed_recoverable"
    FAILED_FATAL = "failed_fatal"
    BLOCKED = "blocked"


class NodeState(BaseModel):
    """单个节点的运行时状态（含专属 ReactAgent 的内存快照）。"""

    node_id: str
    status: NodeStatus = NodeStatus.PENDING

    input_snapshot: dict = Field(default_factory=dict)
    output: dict = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)
    agent_memory_dump: dict = Field(default_factory=dict)

    rounds_used: int = 0
    failure_type: str | None = None
    failure_reason: str | None = None

    started_at: datetime | None = None
    finished_at: datetime | None = None

    def update_by_step_res(self, res: StepResult) -> None:
        """根据 StepResult 更新节点状态（失败类型决定可恢复性）。"""
        if self.node_id != res.node_id:
            raise ValueError(
                f"节点 ID ({self.node_id}) 与结果 ID ({res.node_id}) 冲突"
            )

        if res.status == "success":
            # 业务强约束：LLM 自称成功但自检未通过 -> PARTIAL
            if res.self_check:
                self.status = NodeStatus.SUCCESS
            else:
                self.status = NodeStatus.PARTIAL
                if not res.failure_reason:
                    res.failure_type = "acceptance_failed"
                    res.failure_reason = (
                        "未通过节点自检标准 (self_check=False)"
                    )
        elif res.status == "partial":
            self.status = NodeStatus.PARTIAL
            if not res.failure_type:
                res.failure_type = "acceptance_failed"
        else:
            if res.failure_type in _RECOVERABLE_FAILURES:
                self.status = NodeStatus.FAILED_RECOVERABLE
            else:
                self.status = NodeStatus.FAILED_FATAL

        self.output = dict(res.output or {})
        self.evidence = list(res.evidence or [])
        self.rounds_used += int(res.rounds_used or 0)
        self.agent_memory_dump = dict(res.agent_memory_dump or {})
        if self.status == NodeStatus.SUCCESS:
            self.failure_type = None
            self.failure_reason = None
        else:
            self.failure_type = res.failure_type
            self.failure_reason = res.failure_reason
        self.finished_at = _utcnow()

    def is_fail(self) -> bool:
        """是否为「依赖阻断」意义上的失败（PARTIAL 不阻断下游）。"""
        return self.status in (
            NodeStatus.FAILED_RECOVERABLE,
            NodeStatus.FAILED_FATAL,
            NodeStatus.BLOCKED,
        )

    def needs_replan(self) -> bool:
        """是否需要 PAE 重规划（成功之外的状态都算未达成）。"""
        return self.status != NodeStatus.SUCCESS

    def reset_for_retry(self) -> None:
        """保留 memory/rounds，状态回到 PENDING（重规划复用节点）。"""
        self.status = NodeStatus.PENDING
        self.failure_type = None
        self.failure_reason = None
        self.started_at = None
        self.finished_at = None


# =====================================================================
# 2. 任务节点与 DAG 拓扑结构
# =====================================================================


class TaskNode(BaseModel):
    """DAG 中的单个任务节点定义。"""

    node_id: str
    goal: str
    acceptance_criteria: str = ""
    tool_scope: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    max_rounds: int = 10
    priority: int = 0
    metadata: dict = Field(default_factory=dict)

    @field_validator("depends_on")
    @classmethod
    def _deduplicate_deps(cls, v: list[str]) -> list[str]:
        """去重依赖项，避免 Kahn 拓扑排序入度计算翻倍。"""
        return list(dict.fromkeys(v))

    async def run(
        self,
        context_input: dict,
        restored_memory: dict | None = None,
        tool_center: ToolCenter | None = None,
        graph_id: str = "dag",
        *,
        plan_attempt: int = 0,
        persist_sessions: bool = True,
        sessions_dir: str = "sessions",
        llm_client: Any | None = None,
        auto_approve: bool = False,
        export_memory: bool = False,
    ) -> StepResult:
        """启动具有独立上下文空间的 ReactAgent 执行单元。

        Args:
            context_input: 上游依赖节点产出 ``{node_id: {goal, output, ...}}``；
            restored_memory: 上次失败/部分完成的记忆摘要；会注入任务提示词；
            tool_center: 可共享的工具中心；每个节点都会克隆出独立的
                          ToolCenter/CommandPolicy，避免并发节点互相污染；
                          ``tool_scope`` 非空时只保留指定工具；
            graph_id: 所属 DAG，用于构造节点会话 id；
            plan_attempt: 当前 PAE 尝试序号；会话 id 为
                         ``{graph_id}_{node_id}_att{plan_attempt}``，
                         重规划时保留每次尝试的对话；
            persist_sessions: 节点会话是否落盘（默认 True，便于观察
                         scheduler 与 Agent 的交互；显式 False 可临时纯内存）；
            sessions_dir: 落盘目录；
            llm_client: 可注入的 LLM 客户端（测试/嵌入方）；
            auto_approve: 是否需要给节点 Agent 注入自动审批；
            export_memory: 成功节点是否导出记忆；失败/部分完成节点始终导出。
        """
        session_id = build_node_session_id(
            graph_id, self.node_id, plan_attempt
        )
        session_file: Path | None = None
        if persist_sessions:
            # task 模式：{sessions_dir}/{graph_id}/{node_session_id}.jsonl
            graph_dir = Path(sessions_dir) / _safe_session_id(graph_id)
            session_file = graph_dir / f"{session_id}.jsonl"
            graph_dir.mkdir(parents=True, exist_ok=True)
            if session_file.exists():
                # 同一 attempt 重跑时清理旧的同名会话，避免 seq 冲突
                session_file.unlink()

        center = _isolated_tool_center(
            tool_center, self.tool_scope, auto_approve
        )

        agent_kwargs: dict[str, Any] = {
            "persist": persist_sessions,
            "session_id": session_id,
            "persist_dir": sessions_dir,
            "max_turns": max(1, self.max_rounds),
        }
        if session_file is not None:
            agent_kwargs["session_file"] = str(session_file)
        if center is not None:
            agent_kwargs["tools"] = center
        if llm_client is not None:
            # 每个节点独立浅拷贝 LLM 客户端，避免并发节点互相覆盖
            # llm_client.session（错误事实记录绑定）
            try:
                agent_kwargs["llm_client"] = copy.copy(llm_client)
            except Exception:  # noqa: BLE001
                agent_kwargs["llm_client"] = llm_client

        agent = create_agent(**agent_kwargs)
        try:
            raw_res = await agent.run_task(
                goal=self.goal,
                acceptance_criteria=self.acceptance_criteria,
                context_input=context_input,
                tool_scope=self.tool_scope,
                restored_memory=restored_memory,
            )
            should_export = (
                export_memory or raw_res.get("status") != "success"
            )
            memory = (
                _export_agent_memory(agent, raw_res)
                if should_export and agent.session is not None
                else {}
            )
            metrics = agent_metric_values(agent)
        finally:
            agent.destroy()

        return StepResult(
            node_id=self.node_id,
            status=raw_res["status"],
            output=raw_res.get("output", {}),
            evidence=raw_res.get("evidence", []),
            rounds_used=raw_res.get("rounds_used", 1),
            turns=metrics["turns"],
            steps=metrics["steps"],
            llm_calls=metrics["llm_calls"],
            tool_calls=metrics["tool_calls"],
            prompt_tokens=metrics["prompt_tokens"],
            completion_tokens=metrics["completion_tokens"],
            total_tokens=metrics["total_tokens"],
            estimated_calls=metrics["estimated_calls"],
            elapsed_seconds=float(
                getattr(agent, "elapsed_seconds", 0.0) or 0.0
            ),
            self_check=raw_res.get("self_check", True),
            failure_type=raw_res.get("failure_type"),
            failure_reason=raw_res.get("failure_reason"),
            agent_memory_dump=memory,
        )


def _isolated_tool_center(
    tool_center: ToolCenter | None,
    tool_scope: list[str],
    auto_approve: bool,
) -> ToolCenter:
    """为单个 DAG 节点克隆独立的 ToolCenter 与 CommandPolicy。

    即使节点没有 tool_scope、也没有开启 auto_approve，也不能直接共享
    调度器级 ToolCenter：并发节点的审批记忆会写入同一份 policy，
    造成 allowlist / allowed_roots 串台。
    """
    from agent_test.tools import tool_center as global_tool_center

    base = tool_center if tool_center is not None else global_tool_center
    include = set(tool_scope) if tool_scope else None
    center = base.clone(include=include)
    if auto_approve and center.approver is None:
        center.approver = AutoApproveService()
    return center


def _memory_summary(messages: list, *, limit: int = 4000) -> str:
    """把节点最后几条消息压成可注入提示词的短摘要。"""
    lines: list[str] = []
    for message in messages[-8:]:
        try:
            data = message.model_dump(mode="json")
        except Exception:  # noqa: BLE001
            continue
        role = str(data.get("role") or "")
        content = data.get("content")
        parts: list[str] = []
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                text = block.get("content")
                if isinstance(text, str):
                    parts.append(text)
                elif block.get("name"):
                    parts.append(
                        f"[{block['name']} {str(block.get('args', ''))[:200]}]"
                    )
        text = " ".join(part.strip() for part in parts if part.strip())
        if not text:
            continue
        # 节点任务提示词会在重试时重新注入，记忆里不再重复整段目标/验收标准。
        if role == "user" and (
            "# 任务目标" in text or "# 验收标准" in text
        ):
            continue
        lines.append(f"{role}: {text[:600]}")
    return "\n".join(lines)[:limit]


def _export_agent_memory(
    agent: ReactAgent, result: dict[str, Any] | None = None
) -> dict:
    """导出节点记忆：最近对话摘要 + 最后一次失败事实。"""
    try:
        messages = agent.session.derive_messages()
        payload: dict[str, Any] = {
            "session_id": agent.session.session_id,
            "message_count": len(messages),
            "summary": _memory_summary(messages),
        }
        if result:
            payload.update(
                {
                    "status": result.get("status"),
                    "failure_type": result.get("failure_type"),
                    "failure_reason": result.get("failure_reason"),
                    "output": result.get("output") or {},
                }
            )
        return payload
    except Exception:  # noqa: BLE001
        return {}


def _last_assistant_text(session) -> str:
    """读取任意 Session 中最后一条 assistant/message 的文本。"""
    if session is None:
        return ""
    for event in reversed(session.events):
        if event.type != EventType.ASSISTANT_MESSAGE.value:
            continue
        blocks = getattr(event.data, "content", None) or []
        texts = [
            block.content
            for block in blocks
            if isinstance(block, TextBlock)
        ]
        if texts:
            return "".join(texts)
    return ""


class TaskDAG(BaseModel):
    """整张任务图（DAG）。"""

    graph_id: str
    global_goal: str
    nodes: list[TaskNode]
    layers: list[list[TaskNode]] = Field(default_factory=list)
    version: int = 1
    created_at: datetime = Field(default_factory=_utcnow)

    @field_validator("nodes")
    @classmethod
    def _non_empty(cls, v: list[TaskNode]) -> list[TaskNode]:
        if not v:
            raise ValueError("DAG 至少包含一个节点")
        return v

    def init_dag(self) -> None:
        """校验无悬空依赖、无环、无重复 node_id，并生成分层结构。"""
        self.layers = self.topo_sort()

    def topo_sort(self) -> list[list[TaskNode]]:
        """Kahn 拓扑排序，返回按层分组（同层 priority 降序）的节点。"""
        by_id = {n.node_id: n for n in self.nodes}
        if len(by_id) != len(self.nodes):
            raise ValueError("DAG 中存在重复的 node_id")

        indeg: dict[str, int] = defaultdict(int)
        adj: dict[str, list[str]] = defaultdict(list)
        for node in self.nodes:
            if node.node_id in node.depends_on:
                raise ValueError(f"节点 {node.node_id} 存在自环")
            indeg[node.node_id] = len(node.depends_on)
            for dep in node.depends_on:
                if dep not in by_id:
                    raise ValueError(
                        f"节点 {node.node_id} 依赖了不存在的节点 {dep}"
                    )
                adj[dep].append(node.node_id)

        current = [n.node_id for n in self.nodes if indeg[n.node_id] == 0]
        levels: list[list[TaskNode]] = []
        processed = 0
        while current:
            layer_nodes = sorted(
                (by_id[nid] for nid in current),
                key=lambda n: n.priority,
                reverse=True,
            )
            levels.append(layer_nodes)
            processed += len(current)
            next_level: list[str] = []
            for nid in current:
                for nxt in adj[nid]:
                    indeg[nxt] -= 1
                    if indeg[nxt] == 0:
                        next_level.append(nxt)
            current = next_level

        if processed != len(self.nodes):
            raise ValueError("DAG 中存在环")
        return levels

    def node_map(self) -> dict[str, TaskNode]:
        return {n.node_id: n for n in self.nodes}

    def subgraph(
        self,
        node_ids: Iterable[str],
        *,
        include_upstream: bool = True,
        include_downstream: bool = False,
        graph_id: str | None = None,
        version: int | None = None,
    ) -> "TaskDAG":
        """按节点集合抽取子图（默认包含全部上游依赖闭包）。

        子图内的 depends_on 只保留仍在新图中的依赖；被裁掉的外部依赖
        视为已满足（调用方负责保证其状态可用）。
        """
        selected = set(node_ids)
        by_id = self.node_map()
        unknown = selected - set(by_id)
        if unknown:
            raise ValueError(f"子图包含不存在的节点: {sorted(unknown)}")
        if not selected:
            raise ValueError("子图节点集合不能为空")

        by_dep: dict[str, list[str]] = defaultdict(list)
        for node in self.nodes:
            for dep in node.depends_on:
                by_dep[dep].append(node.node_id)

        if include_upstream:
            stack = list(selected)
            while stack:
                nid = stack.pop()
                for dep in by_id[nid].depends_on:
                    if dep not in selected:
                        selected.add(dep)
                        stack.append(dep)
        if include_downstream:
            stack = list(selected)
            while stack:
                nid = stack.pop()
                for child in by_dep.get(nid, []):
                    if child not in selected:
                        selected.add(child)
                        stack.append(child)

        new_nodes: list[TaskNode] = []
        for node in self.nodes:
            if node.node_id not in selected:
                continue
            cloned = node.model_copy(deep=True)
            cloned.depends_on = [
                dep for dep in cloned.depends_on if dep in selected
            ]
            new_nodes.append(cloned)
        sub = TaskDAG(
            graph_id=graph_id or self.graph_id,
            global_goal=self.global_goal,
            nodes=new_nodes,
            version=self.version if version is None else version,
        )
        sub.init_dag()
        return sub

    def node_ids(self) -> set[str]:
        return {n.node_id for n in self.nodes}


class GraphState(BaseModel):
    """整张图的运行时状态。"""

    graph_id: str
    dag_version: int = 1
    current_layer_index: int = -1
    node_states: dict[str, NodeState] = Field(default_factory=dict)
    global_status: NodeStatus = NodeStatus.PENDING
    replan_count: int = 0
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    @property
    def failed_nodes(self) -> list[str]:
        return [
            s.node_id
            for s in self.node_states.values()
            if s.status
            in (
                NodeStatus.FAILED_RECOVERABLE,
                NodeStatus.FAILED_FATAL,
                NodeStatus.BLOCKED,
            )
        ]

    @property
    def incomplete_nodes(self) -> list[str]:
        return [
            s.node_id
            for s in self.node_states.values()
            if s.status != NodeStatus.SUCCESS
        ]

    @property
    def success_nodes(self) -> list[str]:
        return [
            s.node_id
            for s in self.node_states.values()
            if s.status == NodeStatus.SUCCESS
        ]

    def all_success(self, only: set[str] | None = None) -> bool:
        """指定节点是否全部 SUCCESS（only=None 表示全部节点）。"""
        states = [
            state
            for nid, state in self.node_states.items()
            if only is None or nid in only
        ]
        return bool(states) and all(
            state.status == NodeStatus.SUCCESS for state in states
        )

    def summary(self) -> dict[str, Any]:
        return {
            "graph_id": self.graph_id,
            "dag_version": self.dag_version,
            "global_status": self.global_status.value,
            "current_layer_index": self.current_layer_index,
            "replan_count": self.replan_count,
            "success_nodes": self.success_nodes,
            "failed_nodes": self.failed_nodes,
            "incomplete_nodes": self.incomplete_nodes,
        }


# =====================================================================
# 3. PAE 状态机与 Checkpoint 数据契约
# =====================================================================


class PAEPhase(str, Enum):
    """Plan-Act-Evaluate 循环的阶段。"""

    PLAN = "plan"
    ACT = "act"
    EVALUATE = "evaluate"
    REPLAN = "replan"
    DONE = "done"


class PAEState(BaseModel):
    """PAE 主循环的可观测状态。"""

    phase: PAEPhase = PAEPhase.PLAN
    attempt: int = 0
    max_attempt: int = 5
    last_status: NodeStatus | None = None
    last_plan_version: int = 0
    exhausted: bool = False
    updated_at: datetime = Field(default_factory=_utcnow)

    def advance(self, phase: PAEPhase, **updates: Any) -> None:
        self.phase = phase
        for key, value in updates.items():
            if hasattr(self, key):
                setattr(self, key, value)
        self.updated_at = _utcnow()


class CheckpointRecord(BaseModel):
    """单次 Layer 执行完成后的完整快照记录（JSONL 一行）。"""

    checkpoint_id: str
    timestamp: datetime = Field(default_factory=_utcnow)
    plan_attempt: int
    dag_version: int
    layer_index: int
    task_dag: TaskDAG
    dag_state: GraphState


# =====================================================================
# 4. 调度器
# =====================================================================

NodeExecutor = Callable[..., Any]
Planner = Callable[..., Any]


class Scheduler:
    """DAG 调度器：按拓扑层并行执行、Checkpoint、重规划与子图执行。

    默认 `persist_sessions=True`：planner 与每个节点 Agent 都写会话文件
    （节点会话 id 为 ``{graph_id}_{node_id}_att{plan_attempt}``），便于把
    scheduler 的 attempt/layer 与 Agent 的完整对话、runtime.log、checkpoint
    对照观察；显式传 `persist_sessions=False` 可切回纯内存。
    """

    def __init__(
        self,
        checkpoint_file: str | None = None,
        max_concurrency: int = 5,
        max_attempt: int = 5,
        fixture_path: str | None = None,
        tool_center: ToolCenter | None = None,
        planner_agent: ReactAgent | None = None,
        *,
        checkpoint_dir: str = "./checkpoints",
        persist_sessions: bool = True,
        sessions_dir: str = "sessions",
        max_turns: int | None = None,
        planner: Planner | None = None,
        node_executor: NodeExecutor | None = None,
        auto_approve: bool = False,
        llm_client: Any | None = None,
        event_sink: Callable[..., Any] | None = None,
    ):
        if max_concurrency <= 0:
            raise ValueError("max_concurrency 必须为正整数")
        if max_attempt <= 0:
            raise ValueError("max_attempt 必须为正整数")
        if max_turns is not None and max_turns <= 0:
            raise ValueError("max_turns 必须为正整数")

        self.task_dag: TaskDAG | None = None
        self.dag_state: GraphState | None = None
        self.max_concurrency = max_concurrency
        # Semaphore 在 _execute_layers 内按运行创建，避免跨事件循环复用
        self._semaphore: asyncio.Semaphore | None = None

        self.plan_attempt = 0
        self.max_plan_attempt = max_attempt
        self.pae_state = PAEState(max_attempt=max_attempt)

        # 自动审批：克隆工具中心，避免污染调用方共享实例
        if tool_center is not None and auto_approve:
            self.tool_center = tool_center.clone()
            if self.tool_center.approver is None:
                self.tool_center.approver = AutoApproveService()
        else:
            self.tool_center = tool_center
        self.auto_approve = auto_approve

        self._planner_agent: ReactAgent | None = planner_agent
        self.planner: Planner | None = planner
        self.node_executor: NodeExecutor | None = node_executor
        self.llm_client = llm_client

        self.persist_sessions = persist_sessions
        self.sessions_dir = sessions_dir
        # 全局节点回合下限；None 表示完全使用 planner 的 max_rounds
        self.max_turns = max_turns

        self.check_point_stack: list[CheckpointRecord] = []
        self.checkpoint_file = (
            Path(checkpoint_file) if checkpoint_file is not None else None
        )
        self.checkpoint_dir = Path(checkpoint_dir)
        if self.checkpoint_file is not None:
            self.checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
        self._checkpoint_seq = 0

        self.fixture_path = fixture_path
        self._fixture_cache: list[dict] | None = None

        self.events: list[dict[str, Any]] = []
        self.event_sink = event_sink
        self.plan_exhausted = False
        self.last_subgraph_nodes: set[str] | None = None
        self._current_layer_index: int = -1

        # 本次进程内的 Task 累计指标（跨 attempt 保留）
        self._node_execution_counts: dict[str, int] = {}
        self._node_usage: dict[str, dict[str, int]] = {}
        self._planner_usage: dict[str, int] = zero_metrics()
        self._node_elapsed: dict[str, float] = {}
        self._reused_by_attempt: dict[int, set[str]] = {}
        self._attempt_node_counts: dict[int, int] = {}
        self._attempt_layers: dict[int, list[list[str]]] = {}
        self._attempt_turns: dict[int, dict[str, int]] = {}
        self._planner_turns_by_attempt: dict[int, int] = {}
        self._task_started: float | None = None
        self.task_elapsed_seconds: float | None = None

    # -----------------------------------------------------------------
    # 事件与工具
    # -----------------------------------------------------------------

    async def _emit_event(self, event_type: str | EventType, data: dict) -> None:
        """记录计划生命周期事件（内存列表 + 可选 event_sink）。

        event_sink 支持两种签名：``sink(event_type, data)`` 或 ``sink(record)``。
        """
        type_value = (
            event_type.value
            if isinstance(event_type, EventType)
            else str(event_type)
        )
        record = {"type": type_value, "time": _utcnow().isoformat(), "data": data}
        self.events.append(record)
        # 计划生命周期写入 runtime.log，便于事后按日志复盘 PAE 的每次 attempt
        log_fields = {
            key: data.get(key)
            for key in (
                "attempt",
                "graph_id",
                "dag_version",
                "status",
                "exhausted",
                "is_resuming",
            )
            if key in data
        }
        RuntimeLog.info(
            "[scheduler] event=%s %s",
            type_value,
            json.dumps(log_fields, ensure_ascii=False),
        )
        if self.event_sink is None:
            return
        try:
            signature = inspect.signature(self.event_sink)
            positional = [
                p
                for p in signature.parameters.values()
                if p.kind
                in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                )
            ]
            has_varargs = any(
                p.kind is inspect.Parameter.VAR_POSITIONAL
                for p in signature.parameters.values()
            )
        except (TypeError, ValueError):
            positional, has_varargs = [], True
        if has_varargs or len(positional) >= 2:
            result = self.event_sink(type_value, data)
        elif len(positional) == 1:
            result = self.event_sink(record)
        else:
            result = self.event_sink()
        if inspect.isawaitable(result):
            await result

    def _require_graph(self) -> tuple[TaskDAG, GraphState]:
        if self.task_dag is None or self.dag_state is None:
            raise RuntimeError("调度器尚未载入任何 DAG")
        return self.task_dag, self.dag_state

    # -----------------------------------------------------------------
    # Checkpoint 核心机制：入栈、落盘、断点恢复与回退
    # -----------------------------------------------------------------

    def _ensure_checkpoint_file(self, graph_id: str | None = None) -> Path:
        """确定本次图的 checkpoint 文件。

        - 构造时显式传入 checkpoint_file：沿用该文件；
        - 否则按 `{checkpoint_dir}/{graph_id}.jsonl` 创建，实现每图独立恢复。
        """
        if self.checkpoint_file is not None:
            self.checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
            return self.checkpoint_file
        gid = graph_id or (
            self.task_dag.graph_id if self.task_dag is not None else "dag"
        )
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_file = (
            self.checkpoint_dir / f"{_safe_session_id(gid)}.jsonl"
        )
        return self.checkpoint_file

    def _discover_checkpoint_file(self) -> Path | None:
        """未指定文件时，从 checkpoint_dir 选择最新的 graphid.jsonl。"""
        if self.checkpoint_file is not None:
            return self.checkpoint_file
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        candidates = sorted(
            self.checkpoint_dir.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            return None
        self.checkpoint_file = candidates[0]
        return self.checkpoint_file

    def _save_layer_checkpoint(
        self, layer_idx: int, node_ids: set[str] | None = None
    ) -> CheckpointRecord:
        """一层执行完毕后生成深拷贝快照入栈，并追加写入 JSONL。"""
        task_dag, dag_state = self._require_graph()
        self._ensure_checkpoint_file(task_dag.graph_id)
        # 复用既有 JSONL 时先同步内存栈/序号，避免 checkpoint_id 重号
        if (
            self._checkpoint_seq == 0
            and self.checkpoint_file is not None
            and self.checkpoint_file.exists()
            and self.checkpoint_file.stat().st_size > 0
        ):
            self.load_checkpoints_from_jsonl()
        dag_state.current_layer_index = layer_idx
        dag_state.updated_at = _utcnow()
        self._checkpoint_seq += 1
        ckpt_id = (
            f"{task_dag.graph_id}_v{task_dag.version}"
            f"_att{self.plan_attempt}_L{layer_idx}_c{self._checkpoint_seq}"
        )
        record = CheckpointRecord(
            checkpoint_id=ckpt_id,
            plan_attempt=self.plan_attempt,
            dag_version=task_dag.version,
            layer_index=layer_idx,
            task_dag=task_dag.model_copy(deep=True),
            dag_state=dag_state.model_copy(deep=True),
        )
        self.check_point_stack.append(record)
        with open(self.checkpoint_file, mode="a", encoding="utf-8") as f:
            f.write(record.model_dump_json() + "\n")
        return record

    def load_checkpoints_from_jsonl(self) -> int:
        """从磁盘 JSONL 加载全部历史 Checkpoint 到内存栈。"""
        self.check_point_stack.clear()
        if self.checkpoint_file is None:
            self._discover_checkpoint_file()
        if self.checkpoint_file is None or not self.checkpoint_file.exists():
            return 0
        with open(self.checkpoint_file, mode="r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                text = line.strip()
                if not text:
                    continue
                try:
                    self.check_point_stack.append(
                        CheckpointRecord.model_validate_json(text)
                    )
                except Exception as exc:  # noqa: BLE001
                    RuntimeLog.warning(
                        "[scheduler] 跳过损坏的 Checkpoint 行 %s: %s",
                        line_no,
                        exc,
                    )
        # 续跑时 checkpoint_id 的 _c{seq} 必须继续递增，避免与历史记录重号
        seqs = [
            int(match.group(1))
            for record in self.check_point_stack
            if (match := re.search(r"_c(\d+)$", record.checkpoint_id))
        ]
        self._checkpoint_seq = max(
            self._checkpoint_seq,
            max(seqs, default=len(self.check_point_stack)),
        )
        return len(self.check_point_stack)

    def _rewrite_checkpoint_file(self) -> None:
        """全量重写 JSONL（回滚后截断废除分支）。"""
        if self.checkpoint_file is None:
            return
        with open(self.checkpoint_file, mode="w", encoding="utf-8") as f:
            for record in self.check_point_stack:
                f.write(record.model_dump_json() + "\n")

    def _restore_record(self, record: CheckpointRecord) -> None:
        self.task_dag = record.task_dag.model_copy(deep=True)
        self.task_dag = self._apply_max_turns_to_dag(self.task_dag)
        self.task_dag.init_dag()
        self.dag_state = record.dag_state.model_copy(deep=True)
        self.plan_attempt = record.plan_attempt

    def _recover_prior_successes(self) -> int:
        """断点恢复时从更早的 Checkpoint 回收可复用的成功节点。

        当前状态中的成功节点优先；更早 checkpoint 仅补齐缺失节点。
        候选节点必须满足签名兼容、上游闭包可复用、输入快照与当前
        上游产出一致，否则不会回收。
        """
        task_dag, dag_state = self._require_graph()
        current_nodes = task_dag.node_map()
        candidate_nodes: dict[str, TaskNode] = {}
        candidate_states: dict[str, NodeState] = {}

        for nid, state in dag_state.node_states.items():
            if state.status == NodeStatus.SUCCESS and nid in current_nodes:
                candidate_nodes[nid] = current_nodes[nid]
                candidate_states[nid] = state.model_copy(deep=True)

        for record in reversed(self.check_point_stack):
            old_nodes = record.task_dag.node_map()
            for nid, old_state in record.dag_state.node_states.items():
                if nid in candidate_nodes:
                    continue
                if old_state.status != NodeStatus.SUCCESS:
                    continue
                if nid not in current_nodes:
                    continue
                old_node = old_nodes.get(nid)
                if old_node is None:
                    continue
                if not self._same_signature(old_node, current_nodes[nid]):
                    continue
                candidate_nodes[nid] = old_node
                candidate_states[nid] = old_state.model_copy(deep=True)

        reusable = self._reusable_successes(
            task_dag, candidate_nodes, candidate_states
        )

        recovered = 0
        changed = False
        for nid, state in list(dag_state.node_states.items()):
            if nid not in current_nodes:
                continue
            if nid in reusable:
                if state.status != NodeStatus.SUCCESS:
                    dag_state.node_states[nid] = candidate_states[
                        nid
                    ].model_copy(deep=True)
                    recovered += 1
                    changed = True
                    RuntimeLog.info(
                        "[scheduler] resume reuse success node=%s attempt=%s",
                        nid,
                        self.plan_attempt,
                    )
                continue
            if state.status == NodeStatus.SUCCESS:
                dag_state.node_states[nid] = NodeState(node_id=nid)
                changed = True
                RuntimeLog.info(
                    "[scheduler] resume drop success node=%s attempt=%s "
                    "reason=dependency_or_input_changed",
                    nid,
                    self.plan_attempt,
                )

        for nid in reusable:
            if nid not in dag_state.node_states:
                dag_state.node_states[nid] = candidate_states[
                    nid
                ].model_copy(deep=True)
                recovered += 1
                changed = True

        if changed:
            dag_state.updated_at = _utcnow()
        return recovered

    def rollback_to_checkpoint(
        self,
        steps: int = 1,
        target_checkpoint_id: str | None = None,
    ) -> CheckpointRecord:
        """状态回退：按步数弹出栈顶，或按 checkpoint_id 精准回退。"""
        if not self.check_point_stack:
            self.load_checkpoints_from_jsonl()
        if not self.check_point_stack:
            raise RuntimeError("没有任何可用的 Checkpoint 供回退")
        if steps < 0:
            raise ValueError("steps 必须 >= 0")

        if target_checkpoint_id:
            target_idx = next(
                (
                    i
                    for i, rec in enumerate(self.check_point_stack)
                    if rec.checkpoint_id == target_checkpoint_id
                ),
                -1,
            )
            if target_idx < 0:
                raise ValueError(
                    f"未找到目标 checkpoint_id: {target_checkpoint_id}"
                )
            self.check_point_stack = self.check_point_stack[: target_idx + 1]
        else:
            if steps >= len(self.check_point_stack):
                self.check_point_stack = [self.check_point_stack[0]]
            elif steps > 0:
                self.check_point_stack = self.check_point_stack[:-steps]

        target = self.check_point_stack[-1]
        self._restore_record(target)
        self._rewrite_checkpoint_file()
        return target

    async def resume_from_jsonl(self) -> GraphState:
        """断点恢复：从最新 Layer 快照的下一层继续执行。

        若恢复后仍有失败/部分节点，自动进入 PAE 重规划闭环。
        """
        count = self.load_checkpoints_from_jsonl()
        if count == 0:
            raise FileNotFoundError(
                f"未找到有效的 Checkpoint 记录: {self.checkpoint_file}"
            )
        latest = self.check_point_stack[-1]
        self._restore_record(latest)
        recovered = self._recover_prior_successes()
        RuntimeLog.info(
            "[scheduler] resume start checkpoint=%s attempt=%s layer=%s "
            "recovered_successes=%s",
            latest.checkpoint_id,
            self.plan_attempt,
            latest.layer_index,
            recovered,
        )

        next_layer = latest.layer_index + 1
        if next_layer < len(self.task_dag.layers):  # type: ignore[union-attr]
            await self._execute_layers(start_layer=next_layer)
        else:
            self._evaluate_global_status()

        if self.dag_state is None:  # pragma: no cover - 防御
            raise RuntimeError("断点恢复后状态为空")
        RuntimeLog.info(
            "[scheduler] resume end status=%s attempt=%s",
            self.dag_state.global_status.value,
            self.plan_attempt,
        )
        if (
            self.dag_state.global_status != NodeStatus.SUCCESS
            and self.plan_attempt + 1 < self.max_plan_attempt
        ):
            self.plan_attempt += 1
            if self.task_dag is not None:
                await self.plan_and_execute(
                    self.task_dag.global_goal, is_resuming=True
                )
        return self.dag_state

    # -----------------------------------------------------------------
    # Plan & Replan 上下文收集与状态对齐
    # -----------------------------------------------------------------

    def _build_replan_context(self) -> dict[str, Any]:
        """收集失败/部分成功节点详情与成功节点摘要，供 Planner 决策。"""
        if self.task_dag is None or self.dag_state is None:
            return {}
        node_map = self.task_dag.node_map()
        failed_details: dict[str, dict] = {}
        completed_summary: dict[str, dict] = {}
        for nid, state in self.dag_state.node_states.items():
            node_cfg = node_map.get(nid)
            if state.status in (
                NodeStatus.FAILED_FATAL,
                NodeStatus.FAILED_RECOVERABLE,
                NodeStatus.PARTIAL,
                NodeStatus.BLOCKED,
            ):
                failed_details[nid] = {
                    "goal": node_cfg.goal if node_cfg else "",
                    "status": state.status.value,
                    "failure_type": state.failure_type,
                    "failure_reason": state.failure_reason,
                    "rounds_used": state.rounds_used,
                    "evidence": state.evidence,
                }
            elif state.status == NodeStatus.SUCCESS:
                completed_summary[nid] = {
                    "goal": node_cfg.goal if node_cfg else "",
                    "output": state.output,
                }
        return {
            "graph_id": self.task_dag.graph_id,
            "dag_version": self.task_dag.version,
            "replan_attempt": self.plan_attempt,
            "failed_nodes": failed_details,
            "completed_nodes": completed_summary,
            "blocked_nodes": [
                nid
                for nid, s in self.dag_state.node_states.items()
                if s.status == NodeStatus.BLOCKED
            ],
        }

    def _load_fixture(self) -> list[dict]:
        """加载 fixture（兼容 fixtures 包装 / 单计划 / 计划列表）。"""
        if self._fixture_cache is not None:
            return self._fixture_cache
        if not self.fixture_path or not os.path.exists(self.fixture_path):
            self._fixture_cache = []
            return self._fixture_cache
        with open(self.fixture_path, encoding="utf-8", mode="r") as f:
            data = json.load(f)
        fixtures: list[dict] = []
        if isinstance(data, dict) and "fixtures" in data:
            raw_items = data.get("fixtures") or []
        elif isinstance(data, dict) and "nodes" in data:
            raw_items = [data]
        elif isinstance(data, list):
            raw_items = data
        else:
            raw_items = []
        for item in raw_items:
            if isinstance(item, dict) and "dag" in item:
                fixtures.append(item["dag"])
            elif isinstance(item, dict):
                fixtures.append(item)
        self._fixture_cache = fixtures
        return fixtures

    def _apply_max_turns(self, normalized: dict[str, Any]) -> dict[str, Any]:
        """把 Scheduler.max_turns 作为节点 max_rounds 的下限（只增不减）。"""
        if self.max_turns is not None:
            for node in normalized["nodes"]:
                current = int(node.get("max_rounds") or 5)
                node["max_rounds"] = max(current, self.max_turns)
        return normalized

    def _apply_max_turns_to_dag(self, dag: TaskDAG) -> TaskDAG:
        """恢复的 DAG / 直接提交的 DAG 也应用 max_turns 下限。"""
        if self.max_turns is not None:
            for node in dag.nodes:
                node.max_rounds = max(node.max_rounds, self.max_turns)
        return dag

    def _normalize_and_validate_plan(
        self, plan_data: dict, goal: str
    ) -> dict[str, Any]:
        """规范化计划并预校验 DAG（重复节点/悬空依赖/环会立即失败）。

        预校验让无效 LLM 计划在 `_plan` 内落回下一级降级，而不是在
        `submit` 时才抛异常中断 PAE 循环。
        """
        normalized = PlanService.normalize_plan_dict(plan_data, goal=goal)
        normalized = self._apply_max_turns(normalized)
        probe = TaskDAG(
            graph_id=normalized["graph_id"],
            global_goal=normalized["global_goal"],
            nodes=normalized["nodes"],
        )
        probe.init_dag()
        return normalized

    def _plan_ready(self, source: str, plan_data: dict[str, Any]) -> dict[str, Any]:
        """记录本次计划的来源与规模，便于日志对照 planner/attempt。"""
        RuntimeLog.info(
            "[scheduler] plan source=%s attempt=%s graph=%s nodes=%s",
            source,
            self.plan_attempt,
            plan_data.get("graph_id", ""),
            len(plan_data.get("nodes", [])),
        )
        return plan_data

    async def _plan(
        self, goal: str, replan_context: dict[str, Any]
    ) -> dict[str, Any]:
        """规划：fixture -> 注入 planner -> LLM -> 内置兜底 DAG。"""
        # 1) fixture（测试 / 回放）
        try:
            fixtures = self._load_fixture()
            if fixtures:
                idx = min(self.plan_attempt, len(fixtures) - 1)
                plan_data = copy.deepcopy(fixtures[idx])
                plan_data.setdefault("global_goal", goal)
                return self._plan_ready(
                    "fixture",
                    self._normalize_and_validate_plan(plan_data, goal),
                )
        except Exception as exc:  # noqa: BLE001
            RuntimeLog.warning("[scheduler] fixture 读取失败: %s", exc)

        # 2) 注入 planner（离线 / 自定义规划器）
        if self.planner is not None:
            try:
                normalized = await PlanService.plan_with(
                    self.planner, goal, replan_context
                )
                return self._plan_ready(
                    "planner",
                    self._normalize_and_validate_plan(normalized, goal),
                )
            except Exception as exc:  # noqa: BLE001
                RuntimeLog.warning("[scheduler] 注入 planner 失败: %s", exc)

        # 3) LLM planner
        try:
            plan_data = await self._llm_plan(goal, replan_context)
            if plan_data:
                return self._plan_ready(
                    "llm",
                    self._normalize_and_validate_plan(plan_data, goal),
                )
        except Exception as exc:  # noqa: BLE001
            RuntimeLog.warning("[scheduler] LLM 规划失败，回退兜底 DAG: %s", exc)

        # 4) 兜底
        return self._plan_ready(
            "fallback",
            self._normalize_and_validate_plan(
                self._fallback_dag(goal, replan_context), goal
            ),
        )

    async def _llm_plan(
        self, goal: str, replan_context: dict[str, Any]
    ) -> dict | None:
        """使用 ReactAgent + dag_planner_prompt 调 LLM 生成 DAG 字典。

        若构造时注入 `planner_agent`，复用它（不销毁，由调用方管理生命周期）；
        否则按需创建一次性 planner Agent，用完即 destroy。
        """
        injected = self._planner_agent
        owns_agent = injected is None or getattr(injected, "destroyed", False)
        if owns_agent:
            planner_sid = _safe_session_id(
                f"planner_att{self.plan_attempt}_{get_uuid()[:8]}"
            )
            session_file: Path | None = None
            if self.persist_sessions:
                planner_dir = Path(self.sessions_dir) / "_planner"
                planner_dir.mkdir(parents=True, exist_ok=True)
                session_file = planner_dir / f"{planner_sid}.jsonl"
                if session_file.exists():
                    session_file.unlink()
            agent_kwargs: dict[str, Any] = {
                "persist": self.persist_sessions,
                "session_id": planner_sid,
                "persist_dir": self.sessions_dir,
                "max_turns": 3,
            }
            if session_file is not None:
                agent_kwargs["session_file"] = str(session_file)
            if self.tool_center is not None:
                agent_kwargs["tools"] = self.tool_center
            if self.llm_client is not None:
                agent_kwargs["llm_client"] = self.llm_client
            agent = create_agent(**agent_kwargs)
        else:
            agent = injected  # type: ignore[assignment]

        prompt_parts = [dag_planner_prompt, f"\n# 用户目标\n{goal}"]
        if replan_context and replan_context.get("failed_nodes"):
            prompt_parts.append(
                "\n# 重规划上下文\n"
                f"这是第 {self.plan_attempt + 1} 次规划。之前 DAG 执行中有节点未达成：\n"
                f"{json.dumps(replan_context, ensure_ascii=False, indent=2)}\n"
                "请基于失败原因调整计划：**completed_nodes 的 node_id / goal / "
                "acceptance_criteria / depends_on / tool_scope 必须原样保留**"
                "（调度器据此复用成功节点，重命名或省略字段会导致重复执行）；"
                "只替换或新增失败相关节点，不要重复已完成的工作。"
            )
        prompt_parts.append("\n请只输出符合 schema 的 JSON（不要 Markdown 围栏）：")
        before = agent_metric_values(agent)
        try:
            agent.inbox.append(
                "turn",
                UserMessage(
                    id=get_uuid(),
                    content=[TextBlock(content="\n".join(prompt_parts))],
                ),
            )
            await agent.turn()
            last_text = _last_assistant_text(agent.session)
        finally:
            after = agent_metric_values(agent)
            delta = {
                key: int(after.get(key, 0)) - int(before.get(key, 0))
                for key in self._planner_usage
            }
            add_metrics(self._planner_usage, delta)
            self._planner_turns_by_attempt[self.plan_attempt] = (
                self._planner_turns_by_attempt.get(self.plan_attempt, 0)
                + int(delta.get("turns", 0) or 0)
            )
            if owns_agent:
                agent.destroy()
        return self._parse_plan_json(last_text)

    @staticmethod
    def _parse_plan_json(text: str) -> dict | None:
        """从 LLM 回复中解析 DAG JSON（围栏 / 嵌入块 / 纯 JSON）。"""
        if not text or not text.strip():
            return None
        cleaned = text.strip()
        candidates = [cleaned]
        if "```" in cleaned:
            # 围栏只是额外候选，不能替换原文（前面可能是普通代码块）
            for fence in re.finditer(
                r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL
            ):
                block = fence.group(1).strip()
                if block and block not in candidates:
                    candidates.append(block)
        # 正文里的伪花括号（如 `^{commit}`）不能挡住真正的 DAG JSON
        for embedded in ReactAgent._iter_json_objects(cleaned):
            if embedded != cleaned and embedded not in candidates:
                candidates.append(embedded)
            if len(candidates) >= 64:
                break
        for candidate in candidates:
            try:
                data = json.loads(candidate)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(data, dict) and data.get("nodes"):
                return data
        return None

    def _fallback_dag(
        self, goal: str, replan_context: dict[str, Any]
    ) -> dict[str, Any]:
        """内置兜底 DAG：LLM / planner 不可用时的最小可执行计划。"""
        return {
            "graph_id": f"fallback_{get_uuid()[:8]}",
            "global_goal": goal,
            "nodes": [
                {
                    "node_id": "step_1",
                    "goal": f"分析目标并探索环境：{goal}",
                    "acceptance_criteria": "输出对目标和环境的理解摘要",
                    "depends_on": [],
                    "max_rounds": 5,
                    "priority": 0,
                    "metadata": {"fallback": True},
                },
                {
                    "node_id": "step_2",
                    "goal": f"执行核心任务：{goal}",
                    "acceptance_criteria": "完成任务并输出结果",
                    "depends_on": ["step_1"],
                    "max_rounds": 5,
                    "priority": 0,
                    "metadata": {"fallback": True},
                },
            ],
        }

    @staticmethod
    def _same_signature(old: TaskNode, new: TaskNode) -> bool:
        """判断旧节点成功产出能否在新 DAG 中复用。

        planner 重规划时可能省略 ``acceptance_criteria`` / ``tool_scope``，
        或收窄依赖；若按字段严格相等比较，会把仍可复用的成功节点判为
        “定义已变”而整图重跑。

        兼容规则：
        - goal 必须一致；
        - 新 acceptance_criteria 为空视为“继承旧标准”，非空且不同才拒绝复用；
        - 新 depends_on 必须是旧 depends_on 的子集（新增输入才需要重跑）；
        - 新 tool_scope 为空表示“全部工具”，否则旧工具集必须被新工具集覆盖
          （收窄工具范围可能让旧产出违反新约束，不复用）。
        """
        if old.goal != new.goal:
            return False
        if (
            new.acceptance_criteria
            and new.acceptance_criteria != old.acceptance_criteria
        ):
            return False
        if not set(new.depends_on) <= set(old.depends_on):
            return False
        if new.tool_scope and not set(old.tool_scope) <= set(new.tool_scope):
            return False
        return True

    @staticmethod
    def _dependency_snapshot(
        node: TaskNode,
        node_map: dict[str, TaskNode],
        state_map: dict[str, NodeState],
    ) -> dict[str, dict[str, Any]] | None:
        """构造节点在当前状态下的上游输入快照；依赖缺失时返回 None。"""
        snapshot: dict[str, dict[str, Any]] = {}
        for dep in node.depends_on:
            dep_node = node_map.get(dep)
            dep_state = state_map.get(dep)
            if dep_node is None or dep_state is None:
                return None
            snapshot[dep] = {
                "goal": dep_node.goal,
                "status": dep_state.status.value,
                "output": dep_state.output,
                "failure_type": dep_state.failure_type,
                "failure_reason": dep_state.failure_reason,
            }
        return snapshot

    @classmethod
    def _input_snapshot_matches(
        cls,
        node: TaskNode,
        old_state: NodeState,
        node_map: dict[str, TaskNode],
        state_map: dict[str, NodeState],
    ) -> bool:
        """旧成功节点记录的输入是否仍与当前依赖产出一致。

        旧 checkpoint 可能没有记录输入快照；此时退回“依赖全部可复用”
        的保守判断，由传递闭包保证不会复用失效依赖。
        """
        if not node.depends_on:
            return True
        recorded = old_state.input_snapshot or {}
        if not recorded:
            return True
        current = cls._dependency_snapshot(node, node_map, state_map)
        if current is None:
            return False
        return all(
            recorded.get(dep) == current.get(dep) for dep in node.depends_on
        )

    def _reusable_successes(
        self,
        dag: TaskDAG,
        candidate_nodes: dict[str, TaskNode],
        candidate_states: dict[str, NodeState],
    ) -> set[str]:
        """按拓扑序计算可复用的成功节点集合。

        成功节点只有同时满足以下条件才可复用：节点签名兼容、全部上游
        可复用、记录的输入快照与当前上游产出一致。任一上游不可复用，
        下游成功节点都会被传递失效。
        """
        reusable: set[str] = set()
        node_map = dag.node_map()
        layers = dag.layers or dag.topo_sort()
        for layer in layers:
            for node in layer:
                nid = node.node_id
                old_node = candidate_nodes.get(nid)
                old_state = candidate_states.get(nid)
                if old_node is None or old_state is None:
                    continue
                if old_state.status != NodeStatus.SUCCESS:
                    continue
                if not self._same_signature(old_node, node):
                    continue
                if any(dep not in reusable for dep in node.depends_on):
                    continue
                if not self._input_snapshot_matches(
                    node, old_state, node_map, candidate_states
                ):
                    continue
                reusable.add(nid)
        return reusable

    def _reuse_block_reason(
        self,
        node: TaskNode,
        old_node: TaskNode,
        reusable: set[str],
    ) -> str:
        """给出成功节点不能被复用的原因，便于日志核对。"""
        if not self._same_signature(old_node, node):
            return "signature_changed"
        blocked = [dep for dep in node.depends_on if dep not in reusable]
        if blocked:
            return f"dependency_changed({','.join(blocked)})"
        return "input_changed"

    def _reconcile_state(self, new_dag: TaskDAG) -> None:
        """新旧 DAG 状态对齐（重规划复用成功节点、保留可恢复记忆）。"""
        layers = new_dag.layers or new_dag.topo_sort()
        self._attempt_layers[self.plan_attempt] = [
            [node.node_id for node in layer] for layer in layers
        ]
        if self.plan_attempt > 0:
            self._attempt_node_counts[self.plan_attempt] = len(
                new_dag.nodes
            )
        if self.dag_state is None:
            self.dag_state = GraphState(
                graph_id=new_dag.graph_id,
                dag_version=new_dag.version,
                global_status=NodeStatus.RUNNING,
                node_states={
                    node.node_id: NodeState(node_id=node.node_id)
                    for node in new_dag.nodes
                },
            )
            return

        old_node_map = (
            self.task_dag.node_map() if self.task_dag is not None else {}
        )
        old_states = dict(self.dag_state.node_states)
        reusable = self._reusable_successes(
            new_dag, old_node_map, old_states
        )
        new_states: dict[str, NodeState] = {}
        for new_node in new_dag.nodes:
            nid = new_node.node_id
            old_state = old_states.get(nid)
            old_node = old_node_map.get(nid)
            if old_state is not None and old_node is not None:
                same = self._same_signature(old_node, new_node)
                if (
                    same
                    and old_state.status == NodeStatus.SUCCESS
                    and nid in reusable
                ):
                    new_states[nid] = old_state.model_copy(deep=True)
                    if self.plan_attempt > 0:
                        self._reused_by_attempt.setdefault(
                            self.plan_attempt, set()
                        ).add(nid)
                    RuntimeLog.info(
                        "[scheduler] reuse success attempt=%s node=%s "
                        "old_version=%s new_version=%s",
                        self.plan_attempt,
                        nid,
                        self.task_dag.version if self.task_dag else "-",
                        new_dag.version,
                    )
                    continue
                if same and old_state.status in (
                    NodeStatus.FAILED_RECOVERABLE,
                    NodeStatus.FAILED_FATAL,
                    NodeStatus.PARTIAL,
                ):
                    new_states[nid] = NodeState(
                        node_id=nid,
                        status=NodeStatus.PENDING,
                        agent_memory_dump=dict(old_state.agent_memory_dump),
                        rounds_used=old_state.rounds_used,
                    )
                    continue
                if old_state.status == NodeStatus.SUCCESS:
                    RuntimeLog.info(
                        "[scheduler] drop success attempt=%s node=%s "
                        "reason=%s old_version=%s new_version=%s",
                        self.plan_attempt,
                        nid,
                        self._reuse_block_reason(new_node, old_node, reusable),
                        self.task_dag.version if self.task_dag else "-",
                        new_dag.version,
                    )
            new_states[nid] = NodeState(node_id=nid)

        self.dag_state.graph_id = new_dag.graph_id
        self.dag_state.dag_version = new_dag.version
        self.dag_state.current_layer_index = -1
        self.dag_state.replan_count = self.plan_attempt
        self.dag_state.global_status = NodeStatus.RUNNING
        self.dag_state.node_states = new_states
        self.dag_state.updated_at = _utcnow()

    # -----------------------------------------------------------------
    # DAG 分层执行 / 子图执行
    # -----------------------------------------------------------------

    async def submit(
        self, plan_dict: dict, node_ids: Iterable[str] | None = None
    ) -> GraphState:
        """提交单次 DAG 计划并按层推进执行（可按 node_ids 只跑子图）。

        传入 node_ids 时仍保留完整 DAG 与全部节点状态，只执行「指定节点 +
        其上游闭包」；未选中的节点保持 PENDING，后续可再次
        `execute_subgraph` 继续执行。
        """
        normalized = PlanService.normalize_plan_dict(plan_dict)
        normalized = self._apply_max_turns(normalized)
        new_version = (
            (self.task_dag.version + 1) if self.task_dag is not None else 1
        )
        new_dag = TaskDAG(
            graph_id=normalized["graph_id"],
            global_goal=normalized["global_goal"],
            nodes=normalized["nodes"],
            version=new_version,
        )
        new_dag.init_dag()
        self._ensure_checkpoint_file(new_dag.graph_id)

        self._reconcile_state(new_dag)
        self.task_dag = new_dag

        selected: set[str] | None = None
        if node_ids is not None:
            selected = new_dag.subgraph(
                node_ids, include_upstream=True
            ).node_ids()
        self.last_subgraph_nodes = selected
        await self._execute_layers(start_layer=0, node_ids=selected)
        return self.dag_state  # type: ignore[return-value]

    async def execute_subgraph(
        self,
        node_ids: Iterable[str],
        *,
        include_upstream: bool = True,
    ) -> GraphState:
        """在当前 DAG 上只执行指定节点（默认含上游闭包）。

        图外节点保持原状态不变；执行完成后按「选中节点集合」评估全局状态。
        """
        task_dag, dag_state = self._require_graph()
        selected = set(node_ids)
        sub = task_dag.subgraph(
            selected,
            include_upstream=include_upstream,
        )
        # 确保子图节点都有状态
        for node in sub.nodes:
            dag_state.node_states.setdefault(
                node.node_id, NodeState(node_id=node.node_id)
            )
        self.last_subgraph_nodes = sub.node_ids()
        await self._execute_layers(start_layer=0, node_ids=self.last_subgraph_nodes)
        return dag_state

    async def execute_plan(
        self,
        plan: "Plan | dict",
        node_ids: Iterable[str] | None = None,
    ) -> GraphState:
        """按 Plan / DAG 字典执行（可直接指定要执行的子图节点）。"""
        plan_dict = (
            plan.to_dag_dict() if isinstance(plan, Plan) else plan
        )
        return await self.submit(plan_dict, node_ids=node_ids)

    async def execute_dag(self, dag: TaskDAG) -> GraphState:
        """直接提交一张已构造好的 TaskDAG 并执行。"""
        dag = self._apply_max_turns_to_dag(dag)
        dag.init_dag()
        self._ensure_checkpoint_file(dag.graph_id)
        self._reconcile_state(dag)
        self.task_dag = dag
        self.last_subgraph_nodes = None
        await self._execute_layers(start_layer=0)
        return self.dag_state  # type: ignore[return-value]

    async def _execute_layers(
        self,
        start_layer: int = 0,
        node_ids: set[str] | None = None,
    ) -> None:
        """从指定 Layer 开始逐层并发执行，每层完成后写 Checkpoint。"""
        task_dag, dag_state = self._require_graph()
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        dag_state.global_status = NodeStatus.RUNNING

        for idx in range(start_layer, len(task_dag.layers)):
            self._current_layer_index = idx
            layer = task_dag.layers[idx]
            runnable: list[TaskNode] = []
            for node in layer:
                if node_ids is not None and node.node_id not in node_ids:
                    continue
                state = dag_state.node_states.setdefault(
                    node.node_id, NodeState(node_id=node.node_id)
                )
                if state.status == NodeStatus.SUCCESS:
                    RuntimeLog.info(
                        "[scheduler] node skip(success) attempt=%s layer=%s node=%s",
                        self.plan_attempt,
                        idx,
                        node.node_id,
                    )
                    continue
                if state.status == NodeStatus.BLOCKED:
                    # 上游已被单独重跑成功时，允许解除阻塞并重试；
                    # 仍有失败上游则继续保持 BLOCKED。
                    if set(node.depends_on) & set(dag_state.failed_nodes):
                        RuntimeLog.info(
                            "[scheduler] node skip(blocked) attempt=%s layer=%s "
                            "node=%s failed_deps=%s",
                            self.plan_attempt,
                            idx,
                            node.node_id,
                            sorted(set(node.depends_on) & set(dag_state.failed_nodes)),
                        )
                        continue
                    state.status = NodeStatus.PENDING
                    state.failure_type = None
                    state.failure_reason = None
                if state.status == NodeStatus.RUNNING:
                    # 崩溃恢复残留：回到 PENDING 重新执行
                    state.status = NodeStatus.PENDING

                failed_deps = set(node.depends_on) & set(
                    dag_state.failed_nodes
                )
                if failed_deps:
                    state.status = NodeStatus.BLOCKED
                    state.failure_type = "upstream_blocked"
                    state.failure_reason = (
                        f"因上游依赖节点失败而阻塞: {sorted(failed_deps)}"
                    )
                    state.finished_at = _utcnow()
                    RuntimeLog.info(
                        "[scheduler] node blocked attempt=%s layer=%s node=%s "
                        "failed_deps=%s",
                        self.plan_attempt,
                        idx,
                        node.node_id,
                        sorted(failed_deps),
                    )
                    continue
                runnable.append(node)

            if runnable:
                await asyncio.gather(
                    *[self._batch_run(node) for node in runnable]
                )

            # 最后一层在落盘前先评估全局状态，保证最终 Checkpoint 记录的是
            # success/failed，而不是永远停留在 running。
            if idx == len(task_dag.layers) - 1:
                self._evaluate_global_status(only=node_ids)
            self._save_layer_checkpoint(idx, node_ids)

        self._evaluate_global_status(only=node_ids)

    def _resolve_executor(self):
        return self.node_executor

    async def _call_node_executor(
        self,
        node: TaskNode,
        context_input: dict,
        restored_memory: dict | None,
    ) -> Any:
        """调用注入的 node_executor，兼容 2/3/4 参数签名。"""
        executor = self._resolve_executor()
        if executor is None:
            return None
        graph_id = self.task_dag.graph_id if self.task_dag else "dag"
        try:
            signature = inspect.signature(executor)
            params = [
                p
                for p in signature.parameters.values()
                if p.kind
                in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                )
            ]
            has_varargs = any(
                p.kind is inspect.Parameter.VAR_POSITIONAL
                for p in signature.parameters.values()
            )
            count = len(params)
        except (TypeError, ValueError):
            count = 4
            has_varargs = True
        if has_varargs or count >= 4:
            result = executor(node, context_input, restored_memory, graph_id)
        elif count == 3:
            result = executor(node, context_input, restored_memory)
        elif count == 2:
            result = executor(node, context_input)
        else:
            result = executor(node)
        if inspect.isawaitable(result):
            result = await result
        return result

    async def _batch_run(self, node: TaskNode) -> None:
        """单节点上下文组装、信号量受控执行与状态更新。"""
        task_dag, dag_state = self._require_graph()
        node_map = task_dag.node_map()
        depend_input: dict[str, dict] = {}
        for dep_id in node.depends_on:
            dep_node = node_map[dep_id]
            dep_state = dag_state.node_states.setdefault(
                dep_id, NodeState(node_id=dep_id)
            )
            depend_input[dep_id] = {
                "goal": dep_node.goal,
                "status": dep_state.status.value,
                "output": dep_state.output,
                "failure_type": dep_state.failure_type,
                "failure_reason": dep_state.failure_reason,
            }

        target = dag_state.node_states[node.node_id]
        target.status = NodeStatus.RUNNING
        target.started_at = _utcnow()
        target.input_snapshot = depend_input
        self._node_execution_counts[node.node_id] = (
            self._node_execution_counts.get(node.node_id, 0) + 1
        )
        restored_memory = target.agent_memory_dump or None
        node_session_id = build_node_session_id(
            task_dag.graph_id, node.node_id, self.plan_attempt
        )

        async with self._semaphore:  # type: ignore[union-attr]
            RuntimeLog.info(
                "[scheduler] node start attempt=%s layer=%s graph=%s node=%s "
                "session=%s persist=%s",
                self.plan_attempt,
                self._current_layer_index,
                task_dag.graph_id,
                node.node_id,
                node_session_id,
                self.persist_sessions,
            )
            tokens = RuntimeLog.bind(session_id=node_session_id)
            try:
                try:
                    if self.node_executor is not None:
                        raw = await self._call_node_executor(
                            node, depend_input, restored_memory
                        )
                        result = self._normalize_step_result(
                            node.node_id, raw
                        )
                    else:
                        result = await node.run(
                            context_input=depend_input,
                            restored_memory=restored_memory,
                            tool_center=self.tool_center,
                            graph_id=task_dag.graph_id,
                            plan_attempt=self.plan_attempt,
                            persist_sessions=self.persist_sessions,
                            sessions_dir=self.sessions_dir,
                            llm_client=self.llm_client,
                            auto_approve=self.auto_approve,
                        )
                except Exception as exc:  # noqa: BLE001
                    RuntimeLog.capture_exception(
                        exc,
                        location=f"Scheduler._batch_run[{node.node_id}]",
                        detail={"graph_id": task_dag.graph_id},
                    )
                    result = StepResult(
                        node_id=node.node_id,
                        status="failed",
                        failure_type="unhandled_exception",
                        failure_reason=f"{type(exc).__name__}: {exc}",
                        agent_memory_dump=restored_memory or {},
                    )
                target.update_by_step_res(result)
                metrics = self._node_usage.setdefault(
                    node.node_id, zero_metrics()
                )
                add_metrics(metrics, result_metric_values(result))
                attempt_turns = self._attempt_turns.setdefault(
                    self.plan_attempt, {}
                )
                attempt_turns[node.node_id] = attempt_turns.get(
                    node.node_id, 0
                ) + int(getattr(result, "turns", 0) or 0)
                self._node_elapsed[node.node_id] = self._node_elapsed.get(
                    node.node_id, 0.0
                ) + float(getattr(result, "elapsed_seconds", 0.0) or 0.0)
                RuntimeLog.info(
                    "[scheduler] node end attempt=%s layer=%s node=%s "
                    "status=%s rounds=%s failure_type=%s",
                    self.plan_attempt,
                    self._current_layer_index,
                    node.node_id,
                    target.status.value,
                    target.rounds_used,
                    target.failure_type,
                )
            finally:
                RuntimeLog.unbind(tokens)

    @staticmethod
    def _normalize_step_result(node_id: str, raw: Any) -> StepResult:
        """把 node_executor 返回值规范化为 StepResult。"""
        if isinstance(raw, StepResult):
            if raw.node_id != node_id:
                raw = raw.model_copy(update={"node_id": node_id})
            return raw
        if isinstance(raw, dict):
            data = dict(raw)
            data.setdefault("node_id", node_id)
            data.setdefault("status", "failed")
            data.setdefault("self_check", data.get("status") == "success")
            return StepResult(**data)
        return StepResult(
            node_id=node_id,
            status="failed",
            failure_type="invalid_result",
            failure_reason=(
                "node_executor 返回了不支持的类型: "
                f"{type(raw).__name__}"
            ),
        )

    def _evaluate_global_status(self, only: set[str] | None = None) -> None:
        """按（可选的）节点集合评估整图状态。"""
        _, dag_state = self._require_graph()
        states = [
            state
            for nid, state in dag_state.node_states.items()
            if only is None or nid in only
        ]
        if not states:
            dag_state.global_status = NodeStatus.PENDING
            return
        if dag_state.all_success(only=only):
            dag_state.global_status = NodeStatus.SUCCESS
            return
        statuses = {state.status for state in states}
        if NodeStatus.RUNNING in statuses:
            dag_state.global_status = NodeStatus.RUNNING
        elif NodeStatus.FAILED_FATAL in statuses or NodeStatus.BLOCKED in statuses:
            dag_state.global_status = NodeStatus.FAILED_FATAL
        else:
            dag_state.global_status = NodeStatus.FAILED_RECOVERABLE
        dag_state.updated_at = _utcnow()

    # -----------------------------------------------------------------
    # PAE 顶层主循环
    # -----------------------------------------------------------------

    async def plan_and_execute(
        self, goal: str, is_resuming: bool = False
    ) -> GraphState:
        """PAE (Plan-Act-Evaluate/Replan) 顶层主循环。"""
        self.pae_state = PAEState(
            phase=PAEPhase.PLAN,
            attempt=self.plan_attempt,
            max_attempt=self.max_plan_attempt,
        )
        self.plan_exhausted = False
        self._task_started = time.perf_counter()
        self.task_elapsed_seconds = None
        while self.plan_attempt < self.max_plan_attempt:
            self.pae_state.advance(
                PAEPhase.PLAN, attempt=self.plan_attempt
            )
            await self._emit_event(
                EventType.PLAN_START,
                {
                    "attempt": self.plan_attempt,
                    "goal": goal,
                    "is_resuming": is_resuming,
                },
            )
            replan_context = self._build_replan_context()
            plan_dict = await self._plan(goal, replan_context)

            self.pae_state.advance(PAEPhase.ACT)
            await self.submit(plan_dict)
            self.pae_state.last_plan_version = (
                self.task_dag.version if self.task_dag else 0
            )

            self.pae_state.advance(PAEPhase.EVALUATE)
            status = self.dag_state.global_status
            self.pae_state.last_status = status
            await self._emit_event(
                EventType.PLAN_UPDATE,
                {
                    "attempt": self.plan_attempt,
                    "graph_id": self.task_dag.graph_id
                    if self.task_dag
                    else "",
                    "dag_version": self.pae_state.last_plan_version,
                    "status": status.value,
                    "state": self.dag_state.summary(),
                },
            )
            if status == NodeStatus.SUCCESS:
                break

            # 未达成：进入重规划
            self.plan_attempt += 1
            self.pae_state.advance(
                PAEPhase.REPLAN, attempt=self.plan_attempt
            )
            if self.plan_attempt >= self.max_plan_attempt:
                self.plan_exhausted = True
                break

        if self.dag_state is None:
            raise RuntimeError("PAE 循环结束但未生成任何 DAG 状态")
        if self.dag_state.global_status != NodeStatus.SUCCESS:
            self.plan_exhausted = True
        self.pae_state.advance(
            PAEPhase.DONE,
            exhausted=self.plan_exhausted,
            last_status=self.dag_state.global_status,
        )
        await self._emit_event(
            EventType.PLAN_END,
            {
                "attempt": self.plan_attempt,
                "exhausted": self.plan_exhausted,
                "status": self.dag_state.global_status.value,
                "state": self.dag_state.summary(),
            },
        )
        self.task_elapsed_seconds = (
            time.perf_counter() - self._task_started
            if self._task_started is not None
            else None
        )
        return self.dag_state

    # 语义别名，便于嵌入方按不同命名调用
    async def run(self, goal: str, is_resuming: bool = False) -> GraphState:
        return await self.plan_and_execute(goal, is_resuming=is_resuming)

    async def pae(self, goal: str, is_resuming: bool = False) -> GraphState:
        return await self.plan_and_execute(goal, is_resuming=is_resuming)


__all__ = [
    "CheckpointRecord",
    "GraphState",
    "NodeExecutor",
    "NodeState",
    "NodeStatus",
    "PAEPhase",
    "PAEState",
    "Scheduler",
    "StepResult",
    "TaskDAG",
    "TaskNode",
]
