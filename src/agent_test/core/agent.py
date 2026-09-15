"""ReAct Agent 核心：回合循环、步骤执行与统一错误处理。

知识增量（2026-09-09 知识包）
------------------------------
- 消息流：LLM 返回/工具结果不再直接 session.append，而是先入
  inbox.step，由下一步 pre_step claim 写入 session；回合收尾时
  刷空残留 step 消息，保证最终答复不丢失；
- 上下文管理：TokenMeter 按 LLM usage 做阈值检测，超过阈值由
  Compactor 两级压缩（历史 turn 摘要化为 compact/summary 事件）；
- 事件溯源：所有 session.append 携带 turn/step 上下文，Compactor
  按 turn 分块并排除近期；
- 错误统一处理：任何运行时异常 -> 完整堆栈写入日志文件（RuntimeLog），
  location + error_type + message 摘要写入 session（runtime/error 事件），
  二者通过 session_id 关联，方便回放与排查。
"""
from __future__ import annotations

from agent_test.core.inbox import InBox
from agent_test.llm.compactor import Compactor
from agent_test.llm.registry import LLM_CLIENT
from agent_test.llm.token_meter import TokenMeter
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
        max_context_tokens: int = 128000,
        threshold_ratio: float = 0.8,
        token_meter: TokenMeter | None = None,
        compactor: Compactor | None = None,
    ):
        """初始化 Agent。

        不传依赖时使用默认值：
        - llm_client: LLM_CLIENT["openai"]（懒构建，需要 API_KEY 等环境变量）
        - tools:      全局 tool_center 单例（含内置 read/find/grep/edit/list/write 与 bash 命令执行工具）
        - inbox / session: 新建本 Agent 私有实例
        - token_meter / compactor: 按 max_context_tokens / threshold_ratio 新建
          （知识增量；测试可用 max_context_tokens=10000 验证小窗口压缩）
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
        self.token_meter = (
            token_meter
            if token_meter is not None
            else TokenMeter(
                max_context_tokens=max_context_tokens,
                threshold_ratio=threshold_ratio,
            )
        )
        self.compactor = (
            compactor
            if compactor is not None
            else Compactor(session=self.session, llm_client=self.llm_client)
        )
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
            turn_no = self.phase.turn
            self.session.append(
                EventType.TURN_START, data={"turn": turn_no}, turn=turn_no
            )

            # 1. 取出并持久化本轮用户/回合消息
            queued = self.inbox.claim("turn")
            if not queued:
                return False
            for message in queued:
                self.session.append(
                    _event_type_for(message), data=message, turn=turn_no
                )

            # 2. 循环执行 step：LLM 输出与工具结果先进 inbox.step，
            #    由下一步 pre_step claim 写 session；直到模型给出最终
            #    回答（finish / max_token）或出错
            while True:
                end_reason = await self._step()
                if end_reason in ("max_token", "finish", "error"):
                    break
                self.phase.stage = "step"

            # 3. 收尾：刷空残留在 inbox.step 的最终答复/工具结果
            self._flush_step_messages()

            self.session.append(
                EventType.TURN_END, data={"turn": turn_no}, turn=turn_no
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
        """执行一步：claim step 消息 -> 组装上下文 -> 调用 LLM -> 执行工具。

        返回 end_reason：''（需要继续工具循环）/ finish / max_token / error。
        """
        self.phase.step += 1
        step_idx = self.phase.step
        self.session.append(
            EventType.STEP_START,
            data={"step_idx": step_idx},
            turn=self.phase.turn,
            step=step_idx,
        )
        try:
            # ① pre_step claim：把上一步产出的 step 消息写入 session
            self._flush_step_messages()

            # ② 组装当前上下文并调用 LLM（三元组：消息 / 结束原因 / usage）
            all_messages = self.session.derive_messages()
            assistant_message, end_reason, usage = await self.llm_client.stream(
                all_messages, self.tool_center.get_schemas()
            )

            # ③ TokenMeter 阈值检测：超阈值 -> Compactor 两级压缩 -> 复位
            self.token_meter.update(usage)
            if self.token_meter.is_over_threshold():
                await self._compact_context()
                self.token_meter.reset()

            # ④ 本步 LLM 产出先进 inbox.step（下步 claim 或回合收尾落 session）
            if assistant_message is not None:
                self.inbox.append("step", assistant_message)

            # 执行模型请求的工具，结果同样先进 inbox.step
            tool_calls = (
                [
                    block
                    for block in assistant_message.content
                    if isinstance(block, ToolCallBlock)
                ]
                if assistant_message is not None
                else []
            )
            # pre_step：整步工具调用先做一次“全量预检”。硬拒绝（DENY）
            # 在副作用前拦截；REQUIRE_APPROVAL 在未配置 approver 时也
            # 在此拦截，配置了 approver 则交由 ToolCenter 闸门交互审批
            # （批准 -> 记住前缀 -> 执行；拒绝 -> 原因回传 LLM）。
            decisions = self._pre_step(tool_calls)
            for tc in tool_calls:
                decision = decisions.get(tc.id)
                blocked = decision is not None and (
                    decision.action is PolicyAction.DENY
                    or (
                        decision.action is PolicyAction.REQUIRE_APPROVAL
                        and getattr(self.tool_center, "approver", None)
                        is None
                    )
                )
                if blocked:
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
                self.inbox.append("step", tool_result)

            self.session.append(
                EventType.STEP_END,
                data={"step_idx": step_idx},
                turn=self.phase.turn,
                step=step_idx,
            )
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
                    turn=self.phase.turn,
                    step=step_idx,
                )
            except Exception:  # noqa: BLE001
                # session 本身不可写时不再追加，避免掩盖原始错误
                RuntimeLog.exception(
                    "无法将错误事件写入 session: %s",
                    self.session.file_path,
                )
            return "error"

    def _flush_step_messages(self) -> None:
        """把 inbox.step 中待写消息落 session（pre_step claim / 回合收尾）。"""
        if not self.inbox.has_step_pending():
            return
        for message in self.inbox.claim("step"):
            self.session.append(
                _event_type_for(message),
                data=message,
                turn=self.phase.turn,
                step=self.phase.step,
            )

    async def _compact_context(self) -> int:
        """上下文压缩：一级保留最近一轮；无可压缩块时降级二级全量压缩。"""
        count = await self.compactor.compact()
        if count == 0:
            count = await self.compactor.compact(recent_turns=0)
        return count

    def _pre_step(
        self, tool_calls: list[ToolCallBlock]
    ) -> dict[str, PolicyDecision]:
        """pre_step 阶段：对整步工具调用做统一预检。

        预检只读、无副作用，返回 {tool_call_id: PolicyDecision}。
        危险/工作目录/审批决策抽象在 agent_test.policy（跨工具复用），
        bash 是首个接入的高危工具；ToolCenter.execute 内置同一策略作为
        硬闸门。职责分工：
        - DENY（破坏性/越界）：在 pre_step 直接拦截，决策先于任何副作用；
        - REQUIRE_APPROVAL：未配置 approver 时在此拦截返回“需审批”；
          配置了 approver 则放行到 ToolCenter 闸门做交互审批
          （批准 -> 记住前缀 -> 执行；拒绝 -> 原因回传 LLM）。
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
            if decision.action is PolicyAction.DENY:
                RuntimeLog.warning(
                    "pre_step 拒绝 tool=%s reasons=%s",
                    decision.tool_name,
                    "; ".join(decision.reasons),
                )
            elif decision.action is PolicyAction.REQUIRE_APPROVAL:
                RuntimeLog.info(
                    "pre_step 需审批 tool=%s（待 ToolCenter/approver 处理）",
                    decision.tool_name,
                )
        return decisions