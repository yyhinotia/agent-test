"""工具中心：注册、管理并执行工具。

工具内部异常不向上抛（要回传给 LLM 做下一步决策）：
- 完整堆栈写入日志文件（RuntimeLog.capture_exception）；
- 错误事实通过返回的 {"content": str, "is_error": True} 进入
  tool/result 事件，供 LLM 与回放使用。
"""
from __future__ import annotations

import inspect
import json
import logging
from typing import Any, Callable, Dict, List

from agent_test.exceptions.tools import ToolExecutionError
from agent_test.log.runtime_log import RuntimeLog
from agent_test.policy import CommandPolicy, PolicyAction
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

    async def execute(self, func_name: str, func_args: Dict) -> Dict[str, Any]:
        """执行指定工具，返回 {"content": str, "is_error": bool}。"""
        return_data: Dict[str, Any] = {"content": "", "is_error": False}
        # 策略硬闸门：除 Agent pre_step 外，任何直接 execute 也受管控
        if self.policy is not None:
            decision = self.policy.decide_tool(func_name, func_args)
            if decision.action is PolicyAction.DENY:
                RuntimeLog.warning(
                    "ToolCenter 策略拒绝 tool=%s reasons=%s",
                    func_name,
                    "; ".join(decision.reasons),
                )
                return {
                    "content": decision.to_message(),
                    "is_error": True,
                    "blocked": True,
                    "policy_action": decision.action.value,
                }
            if decision.action is PolicyAction.REQUIRE_APPROVAL:
                if self.approver is None:
                    RuntimeLog.warning(
                        "ToolCenter 需审批（未配置 approver）tool=%s",
                        func_name,
                    )
                    return {
                        "content": decision.to_message(),
                        "is_error": True,
                        "blocked": True,
                        "policy_action": decision.action.value,
                    }
                # 批准继续进行：征求用户批准 -> 记住前缀 -> 正常执行
                command = (decision.detail or {}).get("command", "")
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
                    return {
                        "content": (
                            f"审批被用户拒绝：{message}\n\n"
                            f"{decision.to_message()}"
                        ),
                        "is_error": True,
                        "blocked": True,
                        "policy_action": "deny_by_user",
                    }
                if command:
                    self.policy.add_allowlist_for_command(command)
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
        except Exception as exc:  # noqa: BLE001
            RuntimeLog.capture_exception(
                exc,
                location=f"ToolCenter.execute({func_name})",
                level=logging.WARNING,
            )
            return_data = {"content": f"工具执行失败: {exc}", "is_error": True}
        finally:
            return return_data