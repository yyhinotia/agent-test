"""ReAct Agent 核心：回合循环、步骤执行与统一错误处理。

知识增量（2026-09-09 知识包）
------------------------------
- 消息流：LLM 返回/工具结果不再直接 session.append，而是先入
  inbox.step，由下一步 pre_step claim 写入 session；回合收尾时
  刷空残留 step 消息，保证最终答复不丢失；
- 上下文管理：TokenMeter 按 LLM usage 做阈值检测，超过阈值由
  Compactor 两级压缩（历史上下文摘要化为 compact/summary 事件）；
- 事件溯源：所有 session.append 携带 turn/step 上下文，Compactor
  按 (turn,step) 全局排序、以 step 粒度排除近期（非按 turn）；
- 错误统一处理：任何运行时异常 -> 完整堆栈写入日志文件（RuntimeLog），
  location + error_type + message 摘要写入 session（runtime/error 事件），
  二者通过 session_id 关联，方便回放与排查。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from agent_test.core.inbox import InBox
from agent_test.exceptions.session import SessionEditError
from agent_test.llm.compactor import Compactor
from agent_test.llm.registry import LLM_CLIENT
from agent_test.llm.token_meter import TokenMeter
from agent_test.log.runtime_log import RuntimeLog
from agent_test.policy import PolicyAction, PolicyDecision
from agent_test.session.session import Session
from agent_test.tools import tool_center
from agent_test.types.events import AgentPhase, EventType, Phase
from agent_test.types.messages import (
    ApprovalResult,
    AssistantMessage,
    ExecutionResult,
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
        remain_turns: int = 2,
        tool_head: int = 1024,
        tool_tail: int = 1024,
        token_meter: TokenMeter | None = None,
        compactor: Compactor | None = None,
    ):
        """初始化 Agent。

        不传依赖时使用默认值：
        - llm_client: LLM_CLIENT["openai"]（懒构建，需要 API_KEY 等环境变量）
        - tools:      全局 tool_center 单例（含内置 read/find/grep/edit/list/write 与 bash 命令执行工具）
        - inbox / session: 新建本 Agent 私有实例
        - token_meter: 按 max_context_tokens / threshold_ratio 新建；
        - compactor: 按 remain_turns / tool_head / tool_tail 新建
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
        # 恢复已有编号（注入已有 session / from_file 全量 / reload 窗口）：
        # turn 与 step 续接 session 中已用过的最大编号，续聊不会与历史事件
        # 重号（step 是全局递增计数器）。空 session 为 (0, 0)，行为不变。
        self.phase.turn, self.phase.step = self.session.max_turn_step()
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
            else Compactor(
                session=self.session,
                llm_client=self.llm_client,
                remain_turns=remain_turns,
                tool_head=tool_head,
                tool_tail=tool_tail,
            )
        )
        # 让 LLM 适配器能感知当前 session（用于错误事实记录）
        if hasattr(self.llm_client, "session"):
            self.llm_client.session = self.session

    @classmethod
    def resume(
        cls,
        session_id: str,
        persist_dir: str = "sessions",
        *,
        strict: bool = False,
        **kwargs,
    ) -> "ReactAgent":
        """窗口化恢复已有会话并构造 Agent（续聊入口）。

        一条调用完成三件事：
        1. Session.resume(...)：从 JSONL 尾部倒序读取，只加载「最近一次
           压缩之后」的上下文窗口（见 Session.reload）；
        2. 续接编号：构造时 phase.turn / phase.step 取会话已有事件的最大
           编号（见 Session.max_turn_step），新回合不与历史重号；
        3. 继续追加：session._seq 取磁盘全局下一条 seq，新事件接在全量
           历史之后，磁盘 seq 仍连续。

        其余关键字参数原样透传给构造函数（llm_client / tools / inbox /
        max_context_tokens / threshold_ratio / remain_turns / tool_head /
        tool_tail / token_meter / compactor）。会话文件不存在时会被创建，
        等价于在该 session_id 下开一段新会话。
        """
        session = Session.resume(session_id, persist_dir, strict=strict)
        return cls(session=session, **kwargs)

    async def turn(self) -> bool:
        """执行一轮对话。

        返回 True 表示本轮已执行（无论是否报错，错误见 session/日志）；
        返回 False 表示 inbox 中没有待处理消息——此时不递增回合编号、
        不写入任何事件（先探空再编号，否则会白耗一个 turn 号并留下
        无内容的 turn/start 事件）。

        回合日志闭环：turn/start 一旦写出，本轮必定以 turn/end 收尾，
        data 携带 reason=finish / max_token / error；回合内出现意外异常
        时也会尽力补写 turn/end(reason=error)，不出现「开了没关」的回合。
        phase.stage 在回合内为 RUNNING，回合结束（含异常）复位 IDLE。
        """
        if not self.inbox.has_pending():
            return False

        self.phase.turn += 1
        self.phase.stage = AgentPhase.RUNNING
        turn_no = self.phase.turn
        tokens = RuntimeLog.bind(
            session_id=self.session.session_id, turn=turn_no
        )
        end_reason = "error"  # 未走到 break 即异常 -> error
        try:
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
                # end_reason == ''：本步只是工具调用，继续下一步

            # 3. 收尾：刷空残留在 inbox.step 的最终答复/工具结果
            self._flush_step_messages()

            self._append_turn_end(turn_no, end_reason)
            return True
        except Exception:
            # 回合级意外异常：先尽力补写 turn/end(reason=error) 闭合回合
            # 日志，再记完整堆栈并向上抛（session 可能已不可写）
            RuntimeLog.capture_exception(
                exc=None,
                location=f"ReactAgent.turn[turn={turn_no}]",
                detail={"session_id": self.session.session_id},
            )
            self._close_turn_best_effort(turn_no)
            raise
        finally:
            self.phase.stage = AgentPhase.IDLE
            RuntimeLog.unbind(tokens)

    def _append_turn_end(self, turn_no: int, reason: str) -> None:
        """写入回合收尾事件 turn/end（reason: finish / max_token / error）。"""
        self.session.append(
            EventType.TURN_END,
            data={"turn": turn_no, "reason": reason},
            turn=turn_no,
        )

    def _close_turn_best_effort(self, turn_no: int) -> None:
        """异常路径下尽力补写 turn/end(reason=error)。

        写失败只记日志：调用方真正需要看到的是原始异常，不能被「日志写入
        失败」掩盖。
        """
        try:
            self._append_turn_end(turn_no, "error")
        except Exception:  # noqa: BLE001
            RuntimeLog.exception(
                "异常路径 turn/end 写入失败（session 可能不可写）: %s",
                self.session.file_path,
            )

    async def _step(self) -> str:
        """执行一步：claim step 消息 -> 组装上下文 -> 调用 LLM -> 执行工具。

        工具阶段分成两步（知识增量 5.1）：
        - _pre_step：串行预检 + 串行审批（allowlist 级联依赖顺序）；
        - _exec_one：asyncio.gather 并行执行（审批已结案，无副作用依赖）。

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
            # ① 串行审批（pre_step）：对整步工具调用逐个决策 + 交互审批
            #    —— allowlist 级联必须串行，且决策先于任何副作用；
            #    批准 -> 记住前缀/越界路径/越界工作目录；拒绝 -> 原因回传 LLM。
            decisions = await self._pre_step(tool_calls)
            # 并行执行：审批已全部结案，放行的工具之间无副作用依赖，
            # 用 asyncio.gather 并行执行（单工具异常不取消其他工具）。
            tool_results = await asyncio.gather(
                *[self._exec_one(tc, decisions.get(tc.id)) for tc in tool_calls]
            )
            for tool_result in tool_results:
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

    async def _compact_context(self) -> str | None:
        """上下文压缩：两级压缩——一级保留最近 remain_turns 个 step，
        无可压缩事件时降级二级全量；返回摘要文本或 None。"""
        return await self.compactor.compact()

    async def _pre_step(
        self, tool_calls: list[ToolCallBlock]
    ) -> dict[str, PolicyDecision]:
        """pre_step 阶段（异步）：对整步工具调用做串行预检 + 串行审批。

        预检只读、无副作用，返回 {tool_call_id: PolicyDecision}。
        审批必须串行：allowlist 级联依赖前一个审批结果更新策略状态
        （批准 `pip install flask` 后 `pip install *` 不再打扰用户），
        并发审批会重复弹窗；执行阶段才并行（见 _step / _exec_one）。

        审批状态经 decision.detail（frozen dataclass 的可变 dict 字段）传递，
        避免给 frozen 属性赋值触发 FrozenInstanceError：
        - _resolved:          审批阶段是否已结案
        - _approved:          是否放行执行
        - _approval_decision: auto / approved / denied / blocked
        - _approval_source:   policy / user / no_approver
        - _denied_reason:     拒绝原因（随工具结果回传 LLM 供其调整方案）

        职责分工：DENY（破坏性/越界）与「需审批但无审批服务」在此拦截，
        决策先于任何副作用；批准后记住命令前缀与越界路径，供后续同类调用
        自动放行（allowlist 级联）。
        """
        policy = getattr(self.tool_center, "policy", None)
        approver = getattr(self.tool_center, "approver", None)
        decisions: dict[str, PolicyDecision] = {}
        for tc in tool_calls:
            if policy is None:
                decision = PolicyDecision(
                    action=PolicyAction.EXECUTE, tool_name=tc.name
                )
            else:
                decision = policy.decide_tool(tc.name, tc.args_dict)
            detail = decision.detail if isinstance(decision.detail, dict) else {}
            detail.setdefault("_resolved", True)
            await _approve(decision, detail, tc, policy, approver)
            decisions[tc.id] = decision
        return decisions

    async def _exec_one(
        self, tool_call: ToolCallBlock, decision: PolicyDecision | None
    ) -> ToolResultMessage:
        """执行单个工具调用（并行单元）。

        审批前置：本方法不再询问用户——审批已在 _pre_step 串行完成，
        这里只按审批结论放行或返回 blocked 结果；放行时把 decision 作为
        pre_decision 传给 ToolCenter（跳过其重复决策，防二次审批）。
        """
        detail = (
            decision.detail
            if decision is not None and isinstance(decision.detail, dict)
            else {}
        )
        try:
            if decision is not None and not detail.get("_approved", True):
                return self._blocked_tool_result(tool_call, decision, detail)
            result = await self.tool_center.execute(
                tool_call.name,
                tool_call.args_dict,
                pre_decision=decision,
            )
            return ToolResultMessage(
                tool_call_id=tool_call.id,
                content=[TextBlock(content=result["content"])],
                is_error=result["is_error"],
                approval=result.get("approval"),
                execution=result.get("execution"),
            )
        except Exception as exc:  # noqa: BLE001
            # 单个工具失败不得取消其他并行工具（gather 语义），
            # 也不得让异常逃逸出本步
            summary = RuntimeLog.capture_exception(
                exc,
                location=f"ReactAgent._exec_one[tool={tool_call.name}]",
                detail={"tool_call_id": tool_call.id},
            )
            return ToolResultMessage(
                tool_call_id=tool_call.id,
                content=[
                    TextBlock(
                        content=(
                            f"工具执行失败: {summary['error_type']}: "
                            f"{summary['message']}"
                        )
                    )
                ],
                is_error=True,
                # 执行阶段报错：审批结论保持真实值（auto/approved），
                # 失败只体现在 execution.status 上
                approval=ApprovalResult(
                    decision=str(detail.get("_approval_decision", "auto")),
                    required=bool(
                        decision is not None
                        and decision.action is PolicyAction.REQUIRE_APPROVAL
                    ),
                    source=str(detail.get("_approval_source", "policy")),
                    reason_code="execution_error",
                    reason=(
                        "; ".join(decision.reasons) if decision is not None else ""
                    ),
                ),
                execution=ExecutionResult(
                    status="failed", error=summary["message"]
                ),
            )

    def _blocked_tool_result(
        self,
        tool_call: ToolCallBlock,
        decision: PolicyDecision,
        detail: dict,
    ) -> ToolResultMessage:
        """审批未放行：构造 blocked 结果（execution=not_started，无副作用）。"""
        approval_decision = str(detail.get("_approval_decision", "blocked"))
        denied_reason = str(detail.get("_denied_reason", ""))
        source = str(detail.get("_approval_source", "policy"))
        content = decision.to_message()
        if approval_decision == "denied" and denied_reason:
            content = f"审批被用户拒绝：{denied_reason}\n\n{content}"
        if source == "no_approver":
            reason_code = "no_approver"
        elif approval_decision == "denied":
            reason_code = "user_denied"
        else:
            reason_code = decision.action.value
        return ToolResultMessage(
            tool_call_id=tool_call.id,
            content=[TextBlock(content=content)],
            is_error=True,
            approval=ApprovalResult(
                decision=(
                    "denied" if approval_decision == "denied" else "blocked"
                ),
                required=True,
                source=source,
                reason_code=reason_code,
                reason=denied_reason or "; ".join(decision.reasons),
            ),
            execution=ExecutionResult(
                status="not_started", error=denied_reason
            ),
        )


async def _approve(
    decision: PolicyDecision,
    detail: dict,
    tool_call: ToolCallBlock,
    policy,
    approver,
) -> None:
    """串行审批核心：把审批结论写入 decision.detail。

    四种分支（对应审批状态机）：
    - DENY                       -> blocked（策略直接拦截）
    - REQUIRE_APPROVAL / 无审批服务 -> blocked（no_approver）
    - REQUIRE_APPROVAL / 有审批服务 -> denied / approved（用户决定，
      批准后记住命令前缀、越界路径与越界工作目录，实现 allowlist 级联）
    - EXECUTE                    -> auto（策略自动放行）

    串行性：本函数在 _pre_step 中按 tool_calls 顺序 await，前一个审批的
    结果（allowlist / allowed_roots 更新）对后一个决策立即生效。
    """
    if decision.action is PolicyAction.DENY:
        detail["_approved"] = False
        detail["_approval_decision"] = "blocked"
        detail["_approval_source"] = "policy"
        detail["_denied_reason"] = "; ".join(decision.reasons)
        RuntimeLog.warning(
            "pre_step 拒绝 tool=%s reasons=%s",
            decision.tool_name,
            "; ".join(decision.reasons),
        )
        return

    if decision.action is not PolicyAction.REQUIRE_APPROVAL:
        detail["_approved"] = True
        detail["_approval_decision"] = "auto"
        detail["_approval_source"] = "policy"
        detail["_denied_reason"] = ""
        return

    if approver is None:
        detail["_approved"] = False
        detail["_approval_decision"] = "blocked"
        detail["_approval_source"] = "no_approver"
        detail["_denied_reason"] = "未配置审批服务，已阻断执行"
        RuntimeLog.info(
            "pre_step 需审批（未配置 approver）tool=%s", decision.tool_name
        )
        return

    command = str(detail.get("command", ""))
    approved, message = await approver.confirm(
        action=(
            f"执行命令: {command}"
            if command
            else f"执行工具 {tool_call.name}"
        ),
        description="; ".join(decision.reasons),
    )
    detail["_approval_source"] = "user"
    if not approved:
        detail["_approved"] = False
        detail["_approval_decision"] = "denied"
        detail["_denied_reason"] = message
        RuntimeLog.warning(
            "pre_step 审批被用户拒绝 tool=%s reason=%s",
            decision.tool_name,
            message,
        )
        return
    detail["_approved"] = True
    detail["_approval_decision"] = "approved"
    detail["_denied_reason"] = ""
    if policy is not None:
        policy.remember_approval(command, detail)
    RuntimeLog.info(
        "pre_step 审批通过并记住前缀/路径/工作目录 tool=%s outside_workdir=%s command=%s",
        decision.tool_name,
        detail.get("outside_workdir") or "",
        command[:300],
    )


def create_agent(
    session_id: str | None = None,
    persist_dir: str = "sessions",
    *,
    resume: bool = False,
    strict: bool = False,
    **kwargs,
) -> ReactAgent:
    """Agent 统一构造入口（生命周期工厂）。

    新建（默认）：
        create_agent()：自动生成 session_id 的新 Agent；
        create_agent(session_id="s-1")：指定 id 的新会话——若该文件已存在
        且非空，说明这是续聊场景，直接抛 SessionEditError 提示改用 resume，
        否则新 Agent 会从 seq 0 / turn 1 重新编号并与磁盘历史冲突。

    续聊（resume=True）：
        create_agent(session_id="s-1", resume=True)：窗口化恢复（等价
        ReactAgent.resume(...)）——只加载最近一次压缩之后的窗口，并自动
        续接已有 turn/step 编号与全量 seq。

    其余关键字参数原样透传给 ReactAgent（llm_client / tools / inbox /
    max_context_tokens / threshold_ratio / remain_turns / tool_head /
    tool_tail / token_meter / compactor）。
    """
    if resume:
        if session_id is None:
            raise ValueError("resume=True 必须提供 session_id")
        if "session" in kwargs:
            raise ValueError("resume=True 不接受 session=（请改用 session_id）")
        return ReactAgent.resume(
            session_id, persist_dir, strict=strict, **kwargs
        )

    if session_id is not None:
        existing = Path(persist_dir) / f"{session_id}.jsonl"
        if existing.exists() and existing.stat().st_size > 0:
            raise SessionEditError(
                f"会话已存在且非空，续聊请用 resume=True 或 ReactAgent.resume: "
                f"{existing}",
                location="create_agent",
                detail={"session_id": session_id, "file_path": str(existing)},
            )
    return ReactAgent(session_id=session_id, persist_dir=persist_dir, **kwargs)
