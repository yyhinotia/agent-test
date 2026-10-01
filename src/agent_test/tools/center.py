"""工具中心：注册、管理并执行工具。

工具内部异常不向上抛（要回传给 LLM 做下一步决策）：
- 完整堆栈写入日志文件（RuntimeLog.capture_exception）；
- 错误事实通过返回的 {"content": str, "is_error": True} 进入
  tool/result 事件，供 LLM 与回放使用。

知识增量（双状态机 + pre_decision）：
- 返回值除 content/is_error 外还带 approval（ApprovalResult）与
  execution（ExecutionResult）——审批回答“是否允许执行”，执行回答
  “执行结果如何”，not_started 用于区分“审批被拒”与“执行失败”；
- execute(pre_decision=...) 由 Agent 的 _pre_step 传入审批结论时跳过
  本层重复决策（pre_decision 与独立决策互斥）；不传时本层自行做完整
  策略决策，作为任何直接执行入口的安全兜底，防绕过。
"""
from __future__ import annotations

import inspect
import json
import logging
from typing import Any, Callable, Dict, List

from agent_test.exceptions.tools import ToolExecutionError
from agent_test.log.runtime_log import RuntimeLog
from agent_test.policy import CommandPolicy, PolicyAction
from agent_test.types.messages import ApprovalResult, ExecutionResult
from agent_test.types.tools import ToolCenterSchema, ToolSchema


class ToolCenter:
    def __init__(
        self,
        policy: "CommandPolicy | None" = None,
        approver: "AskService | None" = None,
    ) -> None:
        """初始化工具中心。

        policy: 可选命令治理策略（agent_test.policy.CommandPolicy）。
                传入后 execute 在调用工具前先做策略决策：DENY /
                REQUIRE_APPROVAL 的命令不会执行，直接以 is_error=True
                的工具结果返回（任何执行入口都受管控，防绕过）。
        approver: 可选用户交互服务（agent_test.human.AskService）。
                提供后，REQUIRE_APPROVAL 的命令会先征求用户批准：
                批准 -> 记住命令前缀（会话级 allowlist）并执行；
                拒绝 -> 以 is_error=True 的结果回传用户拒绝原因。
                Agent 场景下审批已在 ReactAgent._pre_step 串行完成，
                execute 收到带 _approved 的 pre_decision 后不再询问
                （避免二次审批）；approver 仍保留给直接调用 execute 的
                入口使用。
        """
        self.tools: Dict[str, ToolCenterSchema] = {}
        self.policy = policy
        self.approver = approver

    def register(
        self,
        desc: str,
        parameters: Dict,
        required: List[str] | None = None,
        name: str | None = None,
    ):
        """工具注册装饰器。

        同名工具若仍可用（usable=True）则抛 ToolExecutionError；
        已被 unregister 标记不可用的同名工具允许重新注册。

        name: 可选，显式指定注册名（工具 schema 名）；缺省用函数名。
            用于需要与 Python 函数名解耦的场景（如工具名与 builtins
            冲突时，函数叫 list_dir、注册名为 list）。
        """

        def wrap(func: Callable):
            func_name = name or func.__name__
            existing = self.tools.get(func_name)
            if existing is not None and existing.usable:
                raise ToolExecutionError(
                    f"工具 {func_name} 重复注册",
                    location="ToolCenter.register",
                )
            center_schema = ToolCenterSchema(
                tool_schema=ToolSchema(
                    name=func_name,
                    description=desc,
                    parameters=parameters,
                    required=required or [],
                ),
                func=func,
                usable=True,
            )
            self.tools[func_name] = center_schema

            def wrap_in(*args, **kwargs):
                return func(*args, **kwargs)

            return wrap_in

        return wrap

    def unregister(self, tool_name: str) -> None:
        """工具取消注册（标记不可用；保留条目以便可再次注册）。"""
        if tool_name not in self.tools:
            return
        self.tools[tool_name].usable = False

    def get_schemas(self) -> List[Dict[str, Any]] | None:
        """获取全部可用工具的 OpenAI function calling schema。"""
        schemas = []
        for item in self.tools.values():
            if not item.usable:
                continue
            schemas.append(item.tool_schema.to_openai_schema())
        return schemas if schemas else None

    async def execute(
        self,
        func_name: str,
        func_args: Dict,
        *,
        pre_decision: "PolicyDecision | None" = None,
    ) -> Dict[str, Any]:
        """执行指定工具。

        返回 {"content": str, "is_error": bool, "approval": ApprovalResult,
              "execution": ExecutionResult, "blocked": bool, "policy_action": str}；
        is_error 保留向后兼容（等价于 execution.status != "success"）。

        pre_decision（知识增量）：Agent 的 pre_step 串行审批结论。
          - None：本方法自行做完整策略决策 —— 安全兜底：即使 Agent 已完成
            preflight，任何直接 execute 入口仍受管控，防绕过；
          - 传入：跳过重复决策（pre_decision 与独立决策互斥，避免二次
            审批或安全绕过）。
        """
        return_data: Dict[str, Any] = {"content": "", "is_error": False}
        approval = ApprovalResult()
        execution = ExecutionResult()

        decision = pre_decision
        if decision is None and self.policy is not None:
            decision = self.policy.decide_tool(func_name, func_args)

        if decision is not None:
            approval = self._approval_from_decision(decision, pre_decision)
            if decision.action is PolicyAction.DENY:
                RuntimeLog.warning(
                    "ToolCenter 策略拒绝 tool=%s reasons=%s",
                    func_name,
                    "; ".join(decision.reasons),
                )
                return self._blocked_result(decision, approval)
            if decision.action is PolicyAction.REQUIRE_APPROVAL:
                detail = decision.detail if isinstance(decision.detail, dict) else {}
                if detail.get("_approved"):
                    approval = ApprovalResult(
                        decision="approved",
                        required=True,
                        source=str(detail.get("_approval_source", "pre_step")),
                        reason_code="approved",
                        reason="; ".join(decision.reasons),
                    )
                elif detail.get("_resolved") and pre_decision is not None:
                    # pre_step 已结案（用户拒绝 / 未配置 approver）：不再询问
                    approval = ApprovalResult(
                        decision=str(detail.get("_approval_decision", "denied")),
                        required=True,
                        source=str(detail.get("_approval_source", "pre_step")),
                        reason_code="not_approved",
                        reason=str(detail.get("_denied_reason", "")),
                    )
                    RuntimeLog.warning(
                        "ToolCenter 拒绝执行（pre_step 未放行）tool=%s reason=%s",
                        func_name,
                        approval.reason,
                    )
                    return self._blocked_result(decision, approval)
                elif self.approver is None:
                    RuntimeLog.warning(
                        "ToolCenter 需审批（未配置 approver）tool=%s",
                        func_name,
                    )
                    approval = ApprovalResult(
                        decision="blocked",
                        required=True,
                        source="no_approver",
                        reason_code="require_approval",
                        reason="; ".join(decision.reasons),
                    )
                    return self._blocked_result(decision, approval)
                else:
                    # 批准继续进行：征求用户批准 -> 记住前缀/路径/工作目录 -> 执行
                    command = detail.get("command", "")
                    approved, message = await self.approver.confirm(
                        action=(
                            f"执行命令: {command}"
                            if command
                            else f"执行工具 {func_name}"
                        ),
                        description="; ".join(decision.reasons),
                    )
                    if not approved:
                        RuntimeLog.warning(
                            "ToolCenter 审批被用户拒绝 tool=%s reason=%s",
                            func_name,
                            message,
                        )
                        approval = ApprovalResult(
                            decision="denied",
                            required=True,
                            source="user",
                            reason_code="user_denied",
                            reason=message,
                        )
                        return {
                            "content": (
                                f"审批被用户拒绝：{message}\n\n"
                                f"{decision.to_message()}"
                            ),
                            "is_error": True,
                            "blocked": True,
                            "policy_action": "deny_by_user",
                            "approval": approval,
                            "execution": ExecutionResult(status="not_started"),
                        }
                    if command or detail.get("outside_workdir"):
                        self.policy.remember_approval(command, detail)
                    approval = ApprovalResult(
                        decision="approved",
                        required=True,
                        source="user",
                        reason_code="user_approved",
                        reason=message,
                    )
                    RuntimeLog.info(
                        "ToolCenter 审批通过并记住前缀 tool=%s command=%s",
                        func_name,
                        command[:300],
                    )
        # bash 未指定 workdir 时，默认工作目录 = 策略允许根（沙箱），
        # 与策略决策口径（default_cwd = allowed_roots[0]）保持一致
        if (
            self.policy is not None
            and func_name == "bash"
            and not func_args.get("workdir")
            and self.policy.allowed_roots
        ):
            func_args = {**func_args, "workdir": str(self.policy.allowed_roots[0])}
        try:
            if func_name not in self.tools:
                raise ToolExecutionError(
                    f"工具未注册: {func_name}", location="ToolCenter.execute"
                )
            if not self.tools[func_name].usable:
                raise ToolExecutionError(
                    f"工具 {func_name} 不可执行", location="ToolCenter.execute"
                )
            func = self.tools[func_name].func
            if inspect.iscoroutinefunction(func):
                tool_return = await func(**func_args)
            else:
                tool_return = func(**func_args)
            if isinstance(tool_return, str):
                return_data["content"] = tool_return
            else:
                return_data["content"] = json.dumps(tool_return, ensure_ascii=False)
        except ToolExecutionError as exc:
            RuntimeLog.capture_exception(
                exc,
                location=exc.location or "ToolCenter.execute",
                level=logging.WARNING,
            )
            return_data = {"content": str(exc), "is_error": True}
            execution = ExecutionResult(status="failed", error=str(exc))
        except Exception as exc:  # noqa: BLE001
            RuntimeLog.capture_exception(
                exc,
                location=f"ToolCenter.execute({func_name})",
                level=logging.WARNING,
            )
            return_data = {"content": f"工具执行失败: {exc}", "is_error": True}
            execution = ExecutionResult(status="failed", error=str(exc))
        finally:
            return_data.setdefault("approval", approval)
            return_data.setdefault("execution", execution)
            return_data["is_error"] = execution.status != "success"
            return return_data

    def _approval_from_decision(
        self,
        decision: "PolicyDecision",
        pre_decision: "PolicyDecision | None",
    ) -> ApprovalResult:
        """把策略决策映射为审批状态（auto / blocked）。"""
        source = "pre_step" if pre_decision is not None else "policy"
        if decision.action is PolicyAction.EXECUTE:
            return ApprovalResult(
                decision="auto",
                required=False,
                source=source,
                reason_code="execute",
                reason="; ".join(decision.reasons),
            )
        return ApprovalResult(
            decision="blocked",
            required=True,
            source=source,
            reason_code=decision.action.value,
            reason="; ".join(decision.reasons),
        )

    def _blocked_result(
        self, decision: "PolicyDecision", approval: ApprovalResult
    ) -> Dict[str, Any]:
        """构造被拦截的工具结果（execution=not_started，未产生副作用）。"""
        return {
            "content": decision.to_message(),
            "is_error": True,
            "blocked": True,
            "policy_action": decision.action.value,
            "approval": approval,
            "execution": ExecutionResult(status="not_started", error=approval.reason),
        }