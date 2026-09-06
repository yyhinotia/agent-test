"""ReAct Agent 核心：回合循环、步骤执行与错误统一处理。

重构要点
--------
- 依赖注入：llm_client / tools / inbox / session 均可由外部传入
  （默认仍使用全局 LLM_CLIENT 注册表与 tool_center 单例），便于测试与复用；
- 产出即持久化：step 产生的 LLM 消息与工具结果**立即**写入 session，
  消除旧实现“先入 inbox.step 再兜底补记”导致的最终回答丢失窗口；
- 错误统一处理：任何运行时异常 -> 完整堆栈写入日志文件（RuntimeLog），
  location + error_type + message 摘要写入 session（runtime/error 事件），
  二者通过 session_id 关联，方便回放与排查。
"""
from __future__ import annotations

from agent_test.core.inbox import InBox
from agent_test.llm.registry import LLM_CLIENT
from agent_test.log.runtime_log import RuntimeLog
from agent_test.policy import PolicyAction, PolicyDecision
from agent_test.session.session import Session
from agent_test.tools import tool_center
from agent_test.types.events import EventType, Phase
from agent_test.types.messages import (
    AssistantMessage,
    Message,
    TextBlock,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
)


def _event_type_for(message: Message):
    """根据消息类型确定写入 session 的事件类型。"""
    if isinstance(message, UserMessage):
        return EventType.USER_MESSAGE
    if isinstance(message, AssistantMessage):
        return EventType.ASSISTANT_MESSAGE
    if isinstance(message, ToolResultMessage):
        return EventType.TOOL_RESULT
    return EventType.ASSISTANT_MESSAGE


class ReactAgent:
    """ReAct Agent：初始化私有 inbox 与 session，驱动 turn -> step 循环。"""

    def __init__(
        self,
        session_id: str | None = None,
        persist_dir: str = "sessions",
        *,
        llm_client=None,
        tools=None,
        inbox: InBox | None = None,
        session: Session | None = None,
    ):
        """初始化 Agent。

        不传依赖时使用默认值：
        - llm_client: LLM_CLIENT["openai"]（懒构建，需要 API_KEY 等环境变量）
        - tools:      全局 tool_center 单例（含内置 read/find/grep/edit/list/write 与 bash 命令执行工具）
        - inbox / session: 新建本 Agent 私有实例
        """
        self.tool_center = tools if tools is not None else tool_center
        self.llm_client = (
            llm_client if llm_client is not None else LLM_CLIENT["openai"]
        )
        self.inbox = inbox if inbox is not None else InBox()
        self.session = (
            session
            if session is not None
            else Session(session_id=session_id, persist_dir=persist_dir)
        )
        self.phase = Phase()
        # 让 LLM 适配器能感知当前 session（用于错误事实记录）
        if hasattr(self.llm_client, "session"):
            self.llm_client.session = self.session

    async def turn(self) -> bool:
        """执行一轮对话。

        返回 True 表示本轮已执行（无论是否报错，错误见 session/日志）；
        返回 False 表示 inbox 中没有待处理消息。
        """
        self.phase.turn += 1
        self.phase.stage = "turn"
        tokens = RuntimeLog.bind(
            session_id=self.session.session_id, turn=self.phase.turn
        )
        try:
            self.session.append(
                EventType.TURN_START, data={"turn": self.phase.turn}
            )

            # 1. 取出并持久化本轮用户/回合消息
            queued = self.inbox.claim("turn")
            if not queued:
                return False
            for message in queued:
                self.session.append(_event_type_for(message), data=message)

            # 2. 循环执行 step：从 session 组装 LLM 历史，
            #    直到模型给出最终回答（finish / max_token）或出错
            while True:
                end_reason = await self._step()
                if end_reason in ("max_token", "finish", "error"):
                    break
                self.phase.stage = "step"

            self.session.append(
                EventType.TURN_END, data={"turn": self.phase.turn}
            )
            return True
        except Exception:
            # 回合级意外异常：堆栈进日志文件（session 可能已损坏无法写入）
            RuntimeLog.capture_exception(
                exc=None,
                location=f"ReactAgent.turn[turn={self.phase.turn}]",
                detail={"session_id": self.session.session_id},
            )
            raise
        finally:
            RuntimeLog.unbind(tokens)

    async def _step(self) -> str:
        """执行一步：从 session 组装 LLM 输入，调用 LLM 并执行工具。

        返回 end_reason：''（需要继续工具循环）/ finish / max_token / error。
        """
        self.phase.step += 1
        step_idx = self.phase.step
        self.session.append(EventType.STEP_START, data={"step_idx": step_idx})
        try:
            all_messages = self.session.derive_messages()
            assistant_message, end_reason = await self.llm_client.stream(
                all_messages, self.tool_center.get_schemas()
            )

            # 产出即持久化：LLM 消息立即写入 session
            if assistant_message is not None:
                self.session.append(
                    _event_type_for(assistant_message), data=assistant_message
                )

            # 执行模型请求的工具，结果立即写入 session
            tool_calls = [
                block
                for block in assistant_message.content
                if isinstance(block, ToolCallBlock)
            ]
            # pre_step：整步工具调用先做一次“全量预检”（审批/危险/
            # 工作目录检测），决策先于任何副作用；未放行的调用不再执行。
            decisions = self._pre_step(tool_calls)
            for tc in tool_calls:
                decision = decisions.get(tc.id)
                if (
                    decision is not None
                    and decision.action is not PolicyAction.EXECUTE
                ):
                    result = {
                        "content": decision.to_message(),
                        "is_error": True,
                        "blocked": True,
                        "policy_action": decision.action.value,
                    }
                else:
                    result = await self.tool_center.execute(
                        tc.name, tc.args_dict
                    )
                tool_result = ToolResultMessage(
                    tool_call_id=tc.id,
                    content=[TextBlock(content=result["content"])],
                    is_error=result["is_error"],
                )
                self.session.append(
                    _event_type_for(tool_result), data=tool_result
                )

            self.session.append(EventType.STEP_END, data={"step_idx": step_idx})
            return end_reason
        except Exception as exc:
            # 错误统一处理：堆栈进日志文件，事实进 session
            summary = RuntimeLog.capture_exception(
                exc,
                location=f"ReactAgent._step[turn={self.phase.turn}, step={step_idx}]",
                detail={"turn": self.phase.turn, "step": step_idx},
            )
            try:
                self.session.append_error(
                    location=summary["location"],
                    error_type=summary["error_type"],
                    message=summary["message"],
                    detail={"turn": self.phase.turn, "step": step_idx},
                )
            except Exception:  # noqa: BLE001
                # session 本身不可写时不再追加，避免掩盖原始错误
                RuntimeLog.exception(
                    "无法将错误事件写入 session: %s",
                    self.session.file_path,
                )
            return "error"

    def _pre_step(
        self, tool_calls: list[ToolCallBlock]
    ) -> dict[str, PolicyDecision]:
        """pre_step 阶段：对整步工具调用做统一预检。

        预检只读、无副作用，返回 {tool_call_id: PolicyDecision}。
        审批/危险/工作目录检测抽象在 agent_test.policy（跨工具复用），
        bash 是首个接入的高危工具；ToolCenter.execute 内置同一策略作为
        硬闸门（防绕过直接 execute），此处负责“决策先于副作用”的全量评审。
        """
        policy = getattr(self.tool_center, "policy", None)
        decisions: dict[str, PolicyDecision] = {}
        for tc in tool_calls:
            if policy is None:
                decisions[tc.id] = PolicyDecision(
                    action=PolicyAction.EXECUTE, tool_name=tc.name
                )
                continue
            decisions[tc.id] = policy.decide_tool(tc.name, tc.args_dict)
        for decision in decisions.values():
            if decision.action is not PolicyAction.EXECUTE:
                RuntimeLog.warning(
                    "pre_step 拦截 tool=%s action=%s reasons=%s",
                    decision.tool_name,
                    decision.action.value,
                    "; ".join(decision.reasons),
                )
        return decisions
