"""subagent 工具：把子任务委托给一次性纯内存子 Agent 同步执行。

设计要点
--------
- **弱引用**：注册时用 `weakref.ref(agent)` 捕获父 Agent，避免形成
  `Agent -> ToolCenter -> tools["subagent"].func -> 闭包 -> Agent`
  的强引用循环，父 Agent 可以被正常 GC / destroy；
- **纯内存**：子 Agent 通过 `ReactAgent.from_agent(..., persist=False)`
  创建，不产生会话文件，执行完立即 `destroy()`；
- **引用链断开**：子 Agent 的工具中心用排除法移除 subagent / span / edit，
  防止孙 Agent 无限递归，也避免继承闭包引用；
- **自动审批**：默认给子 Agent 注入 `AutoApproveService`（只放行
  REQUIRE_APPROVAL，DENY 仍由策略硬拦截），让无人值守的委托可以执行；
- **安全失败**：父 Agent 已销毁时，弱引用解引用为 None，工具返回错误
  文本而不是抛异常，避免破坏父 Agent 的回合。

`register_span` 是同一实现的兼容别名（兼容旧调用方命名）。
"""
from __future__ import annotations

import weakref
from typing import Any, Set

from agent_test.human.service import AutoApproveService
from agent_test.log.runtime_log import RuntimeLog
from agent_test.tools.center import ToolCenter
from agent_test.types.events import EventType
from agent_test.types.messages import TextBlock, UserMessage
from agent_test.utils import get_uuid

# 兼容旧名称
_AutoApprover = AutoApproveService

_SUBAGENT_PARAMETERS: dict[str, Any] = {
    "task_description": {
        "type": "string",
        "description": "交给子 Agent 的完整子任务描述（目标、范围、期望产出）",
    },
    "context": {
        "type": "string",
        "description": "可选的共享上下文（父任务背景、上游结论、约束等）",
    },
}


def _last_assistant_text(session) -> str:
    """读取子 Agent 会话中最后一条 assistant/message 的文本。"""
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


def _build_subagent_prompt(task_description: str, context: str | None) -> str:
    """组装子 Agent 的任务提示词。"""
    parts = [
        "# 子任务（由父 Agent 委托）",
        task_description.strip(),
    ]
    if context and context.strip():
        parts.extend(
            [
                "",
                "# 共享上下文",
                context.strip(),
            ]
        )
    parts.extend(
        [
            "",
            "# 执行要求",
            "- 独立完成上述子任务，不要再次调用 subagent 工具（已禁用）；",
            "- 基于真实工具回显作答，禁止编造文件内容或命令输出；",
            "- 最后用简洁文本给出可直接回填父任务的结论、证据与未决事项。",
        ]
    )
    return "\n".join(parts)


def register_subagent(
    center: ToolCenter,
    agent: Any,
    *,
    tool_name: str = "subagent",
    exclude_tools: Set[str] | None = None,
    auto_approve: bool = True,
    max_turns: int = 10,
) -> None:
    """把 subagent 工具注册到指定 ToolCenter。

    Args:
        center: 父 Agent 的工具中心。
        agent: 父 Agent（用 weakref 捕获，不持有强引用）。
        tool_name: 注册名，默认 "subagent"；兼容别名可用 "span"。
        exclude_tools: 子 Agent 额外排除的工具；subagent/span/edit 始终排除。
        auto_approve: 是否给子 Agent 注入自动审批服务。
        max_turns: 子 Agent 默认最大回合数。
    """
    agent_ref = weakref.ref(agent)
    excluded = {"subagent", "span", "edit"} | set(exclude_tools or ())
    default_max_turns = max_turns

    async def subagent(
        task_description: str,
        context: str | None = None,
    ) -> str:
        """把一个子任务委托给一次性纯内存子 Agent，返回其最终文本结论。"""
        parent = agent_ref()
        if parent is None or getattr(parent, "destroyed", False):
            return "子 Agent 委托失败：父 Agent 已销毁（weakref 失效）"
        if not task_description or not task_description.strip():
            return "子 Agent 委托失败：task_description 不能为空"

        # 延迟导入，避免 core.agent -> tools -> subagent -> core.agent 循环导入
        from agent_test.core.agent import ReactAgent

        child = None
        try:
            limit = default_max_turns
            child = ReactAgent.from_agent(
                parent,
                session_id=f"subagent_{get_uuid()}",
                exclude_tools=excluded,
                max_turns=limit,
                persist=False,
                auto_approve=auto_approve,
            )
            child.inbox.append(
                "turn",
                UserMessage(
                    id=get_uuid(),
                    content=[
                        TextBlock(
                            content=_build_subagent_prompt(
                                task_description, context
                            )
                        )
                    ],
                ),
            )
            await child.turn()
            text = _last_assistant_text(child.session).strip()
            if not text:
                return "子 Agent 已执行，但未返回文本结果"
            return text
        except Exception as exc:  # noqa: BLE001
            RuntimeLog.capture_exception(
                exc,
                location="subagent.execute",
                detail={
                    "task_description": task_description[:200],
                },
            )
            return f"子 Agent 执行失败: {type(exc).__name__}: {exc}"
        finally:
            if child is not None:
                try:
                    child.destroy()
                except Exception:  # noqa: BLE001
                    RuntimeLog.exception("subagent 子 Agent 销毁失败")

    center.register(
        desc=(
            "子 Agent 委托工具：把一个独立的子任务交给一次性纯内存子 Agent "
            "同步执行，返回其最终文本结论。适合分解复杂任务、并行探索或"
            "需要独立上下文的工作；子 Agent 不会持久化会话，也不可再调用 "
            "subagent（防递归）。"
        ),
        parameters=_SUBAGENT_PARAMETERS,
        required=["task_description"],
        name=tool_name,
    )(subagent)


def register_span(center: ToolCenter, agent: Any, **kwargs) -> None:
    """span 兼容入口：以 span 为工具名注册同一套子 Agent 机制。"""
    kwargs.setdefault("tool_name", "span")
    register_subagent(center, agent, **kwargs)


__all__ = [
    "AutoApproveService",
    "_AutoApprover",
    "register_span",
    "register_subagent",
]
