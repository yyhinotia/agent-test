"""ReAct Agent 核心：回合循环、步骤执行与统一错误处理。

核心机制
--------
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
import copy
import json
import re
import time
from pathlib import Path
from typing import Any, Iterator

from agent_test.core.inbox import InBox
from agent_test.core.prompts import task_node_wrap_up_prompt
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
from agent_test.utils import get_uuid


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
        session_file: str | Path | None = None,
        persist: bool = True,
        max_turns: int | None = None,
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
        - session_file: 可选完整会话文件路径（如
          `.agent-test/sessions/<id>/session.jsonl` 或
          `.agent-test/sessions/<graph_id>/<node_session>.jsonl`）；
          缺省时沿用 `{persist_dir}/{session_id}.jsonl`
        - persist:    Session 持久化模式（False = 纯内存，供 subagent 与
                      DAG 节点一次性 Agent 使用；默认 True 保持历史行为）
        - max_turns:  run_task 的最大回合数（None 时由调用方/默认 10 决定）
        - token_meter: 按 max_context_tokens / threshold_ratio 新建；
        - compactor: 按 remain_turns / tool_head / tool_tail 新建
          （测试可用 max_context_tokens=10000 验证小窗口压缩）
        """
        self.tool_center = tools if tools is not None else tool_center
        self.llm_client = (
            llm_client if llm_client is not None else LLM_CLIENT["openai"]
        )
        self.inbox = inbox if inbox is not None else InBox()
        self.session = (
            session
            if session is not None
            else Session(
                session_id=session_id,
                persist_dir=persist_dir,
                persist=persist,
                session_file=session_file,
            )
        )
        self.persist = persist
        self.max_turns = max_turns
        self.destroyed = False
        # 最近一次 turn 的结束原因（finish / max_token / error），供 run_task
        # 判断「模型是否给出了最终答复」。
        self.last_turn_end_reason: str | None = None
        self.phase = Phase()
        # 本次进程内的累计指标（供 Evaluator 读取）
        self.usage_totals = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        self.llm_calls = 0
        self.estimated_usage_calls = 0
        self.turns_executed = 0
        self.steps_executed = 0
        self.tool_calls_executed = 0
        self.elapsed_seconds = 0.0
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
        session_file: str | Path | None = None,
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
        session = Session.resume(
            session_id,
            persist_dir,
            strict=strict,
            session_file=session_file,
        )
        return cls(session=session, **kwargs)

    @classmethod
    def from_agent(
        cls,
        source: "ReactAgent",
        *,
        session_id: str | None = None,
        exclude_tools: tuple[str, ...] | list[str] = ("subagent", "span", "edit"),
        max_turns: int | None = None,
        persist: bool = False,
        auto_approve: bool | None = None,
    ) -> "ReactAgent":
        """从现有 Agent 派生一个（默认纯内存的）子 Agent。

        设计要点：
        - 工具中心使用 `ToolCenter.clone` 复制 schema，并用 **排除法**
          过滤掉 `subagent` / `span` 等闭包引用父 Agent 的工具（彻底断开
          `Agent -> ToolCenter -> 闭包 -> Agent` 引用链）；
        - 策略对象深拷贝，审批服务共享；`auto_approve=True` 时注入
          `AutoApproveService`（只放行 REQUIRE_APPROVAL，DENY 仍由策略拦截）；
        - LLM 客户端做浅拷贝，避免子 Agent 构造时把 `llm_client.session`
          改成子会话、破坏父 Agent 的错误事实记录；
        - 子 Agent 默认 `persist=False`，执行完由调用方 `destroy()`。

        其余运行参数（上下文窗口、压缩保留步数、tool head/tail）继承父
        Agent 的设置。
        """
        source_tools = getattr(source, "tool_center", None)
        if getattr(source, "destroyed", False):
            raise RuntimeError("源 Agent 已销毁，无法派生子 Agent")
        if source_tools is not None:
            child_tools = source_tools.clone(exclude=set(exclude_tools))
        else:
            child_tools = None
        if auto_approve:
            if child_tools is None:
                raise ValueError("auto_approve=True 需要源 Agent 具有 tool_center")
            from agent_test.human.service import AutoApproveService

            child_tools.approver = AutoApproveService()

        child_llm = _clone_llm_client(getattr(source, "llm_client", None))
        token_meter = getattr(source, "token_meter", None)
        compactor = getattr(source, "compactor", None)
        return cls(
            session_id=session_id,
            persist=persist,
            tools=child_tools,
            llm_client=child_llm,
            inbox=InBox(),
            max_turns=max_turns if max_turns is not None else getattr(
                source, "max_turns", None
            ),
            max_context_tokens=getattr(
                token_meter, "max_context_tokens", 128000
            ),
            threshold_ratio=getattr(token_meter, "threshold_ratio", 0.8),
            remain_turns=getattr(compactor, "remain_turns", 2),
            tool_head=getattr(compactor, "tool_head", 1024),
            tool_tail=getattr(compactor, "tool_tail", 1024),
        )

    def destroy(self) -> None:
        """显式销毁 Agent：释放 Session/inbox/tool_center/llm 引用。

        行为约定：`destroy` 后可被 GC 回收；subagent 的弱引用会
        失效并返回安全的错误消息。持久化会话的磁盘文件保留，仍可
        `resume`；纯内存会话的事件被清空。
        """
        session = getattr(self, "session", None)
        if session is not None:
            try:
                session.destroy()
            except Exception:  # noqa: BLE001
                RuntimeLog.exception("Session.destroy 失败（继续释放其余引用）")
        llm = getattr(self, "llm_client", None)
        if llm is not None and getattr(llm, "session", None) is session:
            try:
                llm.session = None
            except Exception:  # noqa: BLE001
                pass
        self.inbox = None
        self.tool_center = None
        self.llm_client = None
        self.compactor = None
        self.token_meter = None
        self.session = None
        self.phase.stage = AgentPhase.IDLE
        self.destroyed = True

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
        if self.destroyed or self.inbox is None:
            raise RuntimeError("Agent 已销毁，无法执行新的回合")
        if not self.inbox.has_pending():
            return False

        started = time.perf_counter()
        self.phase.turn += 1
        self.turns_executed += 1
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
            self.elapsed_seconds += time.perf_counter() - started
            self.phase.stage = AgentPhase.IDLE
            RuntimeLog.unbind(tokens)

    # ------------------------------------------------------------------
    # 任务执行接口（DAG 调度节点 / 结构化结果）
    # ------------------------------------------------------------------

    async def run_task(
        self,
        goal: str,
        acceptance_criteria: str = "",
        context_input: dict | None = None,
        tool_scope: list[str] | None = None,
        restored_memory: dict | None = None,
        final_wrap_up_prompt: str | None = None,
    ) -> dict[str, Any]:
        """把 Agent 作为 DAG 节点执行单元运行，返回结构化 StepResult 兼容 dict。

        与普通 `turn()` 的差异：
        - 自动注入任务目标 / 验收标准 / 上游依赖产出 / JSON 输出约束；
        - 最多执行 `max_turns` 个回合（默认 10），最后一个回合追加
          收尾提示词，要求模型自检并输出 StepResult JSON；
        - 结果解析优先使用模型 JSON；缺失时按保守规则降级为
          `partial + acceptance_failed` 或 `failed + round_exhausted`。

        tool_scope 非空时，为本 Agent 克隆一个只包含这些工具的工具中心
        （不改动共享 ToolCenter，避免并发节点互相影响）。
        restored_memory 为上次失败/部分完成的记忆摘要，会注入任务提示词。
        """
        max_rounds = self.max_turns if self.max_turns is not None else 10
        if max_rounds <= 0:
            raise ValueError("max_turns 必须为正整数")
        if self.destroyed or self.inbox is None:
            raise RuntimeError("Agent 已销毁，无法执行任务")

        original_tools = self.tool_center
        if tool_scope and original_tools is not None:
            self.tool_center = original_tools.clone(include=set(tool_scope))

        prompt = self._build_task_prompt(
            goal=goal,
            acceptance_criteria=acceptance_criteria,
            context_input=context_input or {},
            tool_scope=tool_scope or [],
            restored_memory=restored_memory,
        )
        wrap_up = final_wrap_up_prompt or task_node_wrap_up_prompt
        rounds_used = 0
        self.last_turn_end_reason = None
        try:
            for round_no in range(1, max_rounds + 1):
                if round_no == 1:
                    turn_prompt = prompt
                else:
                    turn_prompt = (
                        "上一回合已经结束，但没有给出合法的 StepResult JSON。"
                        "不要重复执行已完成的工作；请基于已有产出，仅输出 "
                        "StepResult JSON。\n\n"
                        f"# 任务目标\n{goal}\n"
                    )
                if round_no == max_rounds:
                    turn_prompt = (
                        f"{turn_prompt}\n\n{wrap_up}"
                    )
                self.inbox.append(
                    "turn",
                    UserMessage(
                        id=get_uuid(),
                        content=[TextBlock(content=turn_prompt)],
                    ),
                )
                ran = await self.turn()
                if not ran:
                    break
                rounds_used += 1
                # 只要本回合出现过合法 StepResult 就收尾；即使模型以
                # max_token/error 结束，也已拿到可评估结果。
                if self._find_structured_result() is not None:
                    break
                if self.last_turn_end_reason == "finish":
                    # finish 但没有合法 StepResult：继续下一轮，最后一次
                    # 会追加 task_node_wrap_up_prompt 强制收尾。
                    RuntimeLog.warning(
                        "run_task finish 但未给出合法 StepResult，"
                        "继续第 %s/%s 轮：session=%s",
                        round_no,
                        max_rounds,
                        self.session.session_id if self.session else "-",
                    )
                    continue
            return self._extract_task_result(
                rounds_used=rounds_used, max_rounds=max_rounds
            )
        finally:
            self.tool_center = original_tools

    def _build_task_prompt(
        self,
        goal: str,
        acceptance_criteria: str,
        context_input: dict,
        tool_scope: list[str],
        restored_memory: dict | None = None,
    ) -> str:
        """组装 DAG 节点任务提示词（含上游上下文与 JSON 输出 schema）。"""
        if context_input:
            context_text = json.dumps(
                context_input, ensure_ascii=False, indent=2
            )
            if len(context_text) > 12000:
                context_text = context_text[:12000] + "\n…（上游上下文已截断）"
        else:
            context_text = "（无上游依赖产出）"
        scope_text = (
            "、".join(tool_scope)
            if tool_scope
            else "当前工具中心内全部可用工具"
        )
        scope_note = ""
        if tool_scope and "bash" not in tool_scope:
            scope_note = (
                "\n注意：本节点未启用 bash 工具，请跳过系统提示词第 1 节的"
                "首轮环境检查，直接使用允许的工具执行任务。\n"
            )
        memory_text = ""
        if restored_memory:
            summary = str(restored_memory.get("summary") or "").strip()
            failure = str(
                restored_memory.get("failure_reason") or ""
            ).strip()
            if summary or failure:
                memory_text = (
                    "\n# 上次尝试记忆（只读参考）\n"
                    "本节点上一次执行未完成；以下内容用于恢复状态，"
                    "不要重复已完成的工作。\n"
                    + (f"{summary}\n" if summary else "")
                    + (f"上次失败原因：{failure}\n" if failure else "")
                )
        return (
            "# 任务目标\n"
            f"{goal}\n\n"
            "# 验收标准\n"
            f"{acceptance_criteria or '完成任务并给出可验证的结果'}\n\n"
            "# 上游依赖产出（只读上下文）\n"
            f"{context_text}\n"
            f"{memory_text}\n"
            "# 工具范围\n"
            f"{scope_text}\n"
            "不要调用范围之外的工具。\n"
            f"{scope_note}\n"
            "# 输出要求（必须以 JSON 收尾）\n"
            "完成工作后，必须在最后一条 assistant 消息中输出以下 JSON "
            "（不要 Markdown 围栏，不要在 JSON 后追加解释）：\n"
            '{\n'
            '  "status": "success | partial | failed",\n'
            '  "output": {"任意键": "任务产出的结构化结果"},\n'
            '  "evidence": ["文件路径/命令回显/行号等可核验证据"],\n'
            '  "self_check": true,\n'
            '  "failure_type": null,\n'
            '  "failure_reason": null\n'
            '}\n'
            "若自检未通过，请将 status 设为 partial 并填写 failure_reason。"
        )

    def _extract_task_result(
        self, *, rounds_used: int, max_rounds: int
    ) -> dict[str, Any]:
        """从本回合 assistant 文本提取结构化结果，失败时保守降级。"""
        last_text = self._last_assistant_text()
        parsed = self._find_structured_result()
        if parsed is not None:
            parsed["rounds_used"] = rounds_used
            parsed["self_check"] = bool(parsed.get("self_check", True))
            if parsed["status"] == "success":
                parsed["failure_type"] = None
                parsed["failure_reason"] = None
            elif not parsed.get("failure_type"):
                parsed["failure_type"] = (
                    "acceptance_failed"
                    if parsed["status"] == "partial"
                    else "agent_reported_failure"
                )
            return parsed

        if self.last_turn_end_reason == "finish":
            status = "partial"
            failure_type = "acceptance_failed"
            failure_reason = (
                "模型已结束，但本回合 assistant 消息未输出符合 StepResult "
                "schema 的 JSON"
            )
        elif rounds_used >= max_rounds:
            status = "failed"
            failure_type = "round_exhausted"
            failure_reason = f"达到最大回合数 {max_rounds} 仍未给出结构化结果"
        else:
            status = "failed"
            failure_type = "round_exhausted"
            failure_reason = (
                f"回合异常结束（reason={self.last_turn_end_reason}），"
                "未获得最终结果"
            )
        output: dict[str, Any] = {}
        if last_text.strip():
            # 保留足够长的原文供事后复盘（尤其是 JSON 被正文前缀干扰时）；
            # 12k 足以覆盖一个完整 StepResult，又不会让 checkpoint 无限膨胀。
            raw_summary = last_text.strip()
            output["summary"] = raw_summary[:12000]
            if len(raw_summary) > 12000:
                output["summary_truncated"] = True
                output["summary_chars"] = len(raw_summary)
        return {
            "status": status,
            "output": output,
            "evidence": [],
            "rounds_used": rounds_used,
            "self_check": False,
            "failure_type": failure_type,
            "failure_reason": failure_reason,
        }

    def _current_turn_assistant_texts(self) -> list[str]:
        """返回当前回合（最后一条 user/message 之后）的 assistant 文本列表。"""
        if self.session is None:
            return []
        texts: list[str] = []
        for event in reversed(self.session.events):
            if event.type == EventType.USER_MESSAGE.value:
                break
            if event.type != EventType.ASSISTANT_MESSAGE.value:
                continue
            blocks = getattr(event.data, "content", None) or []
            text = "".join(
                block.content
                for block in blocks
                if isinstance(block, TextBlock)
            )
            if text.strip():
                texts.append(text)
        texts.reverse()
        return texts

    def _find_structured_result(self) -> dict[str, Any] | None:
        """在当前回合的 assistant 消息中，从后往前找最近一个合法 StepResult。

        不能只看最后一条 assistant：模型可能先输出 StepResult JSON，再追加
        一段自然语言总结；此时最后一条文本不含 JSON，但 JSON 仍然有效。
        """
        for text in reversed(self._current_turn_assistant_texts()):
            parsed = self._parse_structured_result(text)
            if parsed is not None:
                return parsed
        return None

    def _last_assistant_text(self) -> str:
        """读取 session 中最后一条 assistant/message 的文本内容。"""
        if self.session is None:
            return ""
        for event in reversed(self.session.events):
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

    @staticmethod
    def _parse_structured_result(text: str) -> dict[str, Any] | None:
        """解析 StepResult JSON：纯 JSON / Markdown 围栏 / 嵌入 JSON 块。"""
        if not text or not text.strip():
            return None
        cleaned = text.strip()
        candidates = [cleaned]
        # 1) Markdown 围栏：只作为额外候选，不能替换原文——正文里可能先出现
        #    一个普通代码块（例如 README 片段），真正的 JSON 在它后面。
        if "```" in cleaned:
            for fence in re.finditer(
                r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL
            ):
                block = fence.group(1).strip()
                if block and block not in candidates:
                    candidates.append(block)
        # 2) 扫描文本中所有平衡花括号块（应对 JSON 前后的解释文本）。
        #    不能只取第一个：正文里的 `^{commit}` / `{path}` 会先形成伪块，
        #    真正的 StepResult JSON 往往在其后。
        for embedded in ReactAgent._iter_json_objects(cleaned):
            if embedded != cleaned and embedded not in candidates:
                candidates.append(embedded)
            if len(candidates) >= 64:  # 防御超长文本产生过多候选
                break

        for candidate in candidates:
            try:
                data = json.loads(candidate)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(data, dict) or "status" not in data:
                continue
            status = str(data.get("status", "")).strip().lower()
            status = {
                "completed": "success",
                "done": "success",
                "ok": "success",
                "partially_done": "partial",
                "error": "failed",
            }.get(status, status)
            if status not in ("success", "partial", "failed"):
                continue
            output = data.get("output")
            if not isinstance(output, dict):
                output = {"result": output} if output is not None else {}
            evidence = data.get("evidence")
            if not isinstance(evidence, list):
                evidence = [str(evidence)] if evidence else []
            return {
                "status": status,
                "output": output,
                "evidence": [str(item) for item in evidence],
                "self_check": bool(data.get("self_check", status == "success")),
                "failure_type": data.get("failure_type"),
                "failure_reason": data.get("failure_reason"),
            }
        return None

    @staticmethod
    def _iter_json_objects(text: str) -> Iterator[str]:
        """迭代文本中所有花括号平衡的对象（字符串内花括号忽略）。

        从每个 `{` 起点尝试平衡扫描，命中后从起点 +1 继续，因此嵌套对象
        也会被枚举；调用方用 `json.loads` 自行筛选有效 JSON。
        """
        cursor = 0
        while cursor < len(text):
            start = text.find("{", cursor)
            if start < 0:
                return
            obj = ReactAgent._scan_json_object(text, start)
            if obj is not None:
                yield obj
            cursor = start + 1

    @staticmethod
    def _scan_json_object(text: str, start: int) -> str | None:
        """从 start 处的 `{` 扫描一个平衡对象；未闭合返回 None。"""
        depth = 0
        in_string = False
        escaped = False
        for idx in range(start, len(text)):
            char = text[idx]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : idx + 1]
        return None

    @staticmethod
    def _extract_json_object(text: str) -> str | None:
        """兼容入口：返回文本中第一个花括号平衡的对象。"""
        return next(ReactAgent._iter_json_objects(text), None)

    def _append_turn_end(self, turn_no: int, reason: str) -> None:
        """写入回合收尾事件 turn/end（reason: finish / max_token / error）。"""
        self.last_turn_end_reason = reason
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

    def _record_usage(self, usage: dict | None) -> None:
        """把单次 LLM usage 累加到 Agent 级指标。"""
        if not usage:
            return
        prompt = int(usage.get("prompt_tokens", 0) or 0)
        completion = int(usage.get("completion_tokens", 0) or 0)
        total = int(usage.get("total_tokens", 0) or 0)
        if total <= 0:
            total = prompt + completion
        self.usage_totals["prompt_tokens"] += prompt
        self.usage_totals["completion_tokens"] += completion
        self.usage_totals["total_tokens"] += total
        if usage.get("estimated"):
            self.estimated_usage_calls += 1

    async def _step(self) -> str:
        """执行一步：claim step 消息 -> 组装上下文 -> 调用 LLM -> 执行工具。

        工具阶段分成两步：
        - _pre_step：串行预检 + 串行审批（allowlist 级联依赖顺序）；
        - _exec_one：asyncio.gather 并行执行（审批已结案，无副作用依赖）。

        返回 end_reason：''（需要继续工具循环）/ finish / max_token / error。
        """
        self.phase.step += 1
        self.steps_executed += 1
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
            self.llm_calls += 1
            self._record_usage(usage)

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
            self.tool_calls_executed += len(tool_calls)

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
        compactor = self.compactor
        before_usage = dict(getattr(compactor, "usage_totals", {}) or {})
        before_calls = int(getattr(compactor, "llm_calls", 0) or 0)
        before_estimated = int(
            getattr(compactor, "estimated_usage_calls", 0) or 0
        )
        summary = await compactor.compact()
        after_usage = dict(getattr(compactor, "usage_totals", {}) or {})
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            self.usage_totals[key] += int(after_usage.get(key, 0) or 0) - int(
                before_usage.get(key, 0) or 0
            )
        self.llm_calls += int(getattr(compactor, "llm_calls", 0) or 0) - (
            before_calls
        )
        self.estimated_usage_calls += int(
            getattr(compactor, "estimated_usage_calls", 0) or 0
        ) - before_estimated
        return summary

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


def _clone_llm_client(client):
    """浅拷贝 LLM 客户端，避免子 Agent 覆盖父 Agent 的 `session` 引用。

    适配器通常只持有 client / model / session 等简单属性，浅拷贝即可；
    无法拷贝时退化为共享实例（功能不受影响，仅错误事实可能记录到子会话）。
    """
    if client is None:
        return None
    try:
        return copy.copy(client)
    except Exception:  # noqa: BLE001
        return client


def create_agent(
    session_id: str | None = None,
    persist_dir: str = "sessions",
    *,
    resume: bool = False,
    strict: bool = False,
    persist: bool = True,
    max_turns: int | None = None,
    session_file: str | Path | None = None,
    **kwargs,
) -> ReactAgent:
    """Agent 统一构造入口（生命周期工厂）。

    新建（默认）：
        create_agent()：自动生成 session_id 的新 Agent；
        create_agent(session_id="s-1")：指定 id 的新会话——若该文件已存在
        且非空，说明这是续聊场景，直接抛 SessionEditError 提示改用 resume，
        否则新 Agent 会从 seq 0 / turn 1 重新编号并与磁盘历史冲突。
        create_agent(persist=False)：纯内存 Agent（subagent / DAG 节点），
        跳过磁盘冲突检测，不创建会话文件。

    续聊（resume=True）：
        create_agent(session_id="s-1", resume=True)：窗口化恢复（等价
        ReactAgent.resume(...)）——只加载最近一次压缩之后的窗口，并自动
        续接已有 turn/step 编号与全量 seq。

    其余关键字参数原样透传给 ReactAgent（llm_client / tools / inbox /
    session_file / persist / max_turns / max_context_tokens / threshold_ratio /
    remain_turns / tool_head / tool_tail / token_meter / compactor）。
    """
    if resume:
        if session_id is None:
            raise ValueError("resume=True 必须提供 session_id")
        if "session" in kwargs:
            raise ValueError("resume=True 不接受 session=（请改用 session_id）")
        return ReactAgent.resume(
            session_id,
            persist_dir,
            strict=strict,
            session_file=session_file,
            **kwargs,
        )

    if persist:
        if session_file is not None:
            existing = Path(session_file)
        elif session_id is not None:
            existing = Path(persist_dir) / f"{session_id}.jsonl"
        else:
            existing = None
        if existing is not None and existing.exists() and existing.stat().st_size > 0:
            raise SessionEditError(
                f"会话已存在且非空，续聊请用 resume=True 或 ReactAgent.resume: "
                f"{existing}",
                location="create_agent",
                detail={"session_id": session_id, "file_path": str(existing)},
            )
    return ReactAgent(
        session_id=session_id,
        persist_dir=persist_dir,
        persist=persist,
        max_turns=max_turns,
        session_file=session_file,
        **kwargs,
    )
