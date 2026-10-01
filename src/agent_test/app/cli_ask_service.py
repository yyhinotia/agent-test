"""CLI 审批服务：统一 stdin + 排空陈旧输入 + 审计日志。

与 ConsoleAskService 的差异（知识增量 5.3 / 6）：

- 必须走 StdinDispatcher：主循环与审批共享同一终端，只有后台线程独占
  input() 才能消除"审批被陈旧输入自动批准"的竞争；
- confirm / ask_user 之前先 drain_pending()：用户在审批弹出前粘贴的多行
  文本会被丢弃，不会被当成审批回答；
- 每次 confirm 记入审计日志（confirm_calls，含拒绝理由），可回放可审计；
- 读到 EOF（输入流结束）时按"拒绝"降级，避免 Agent 永久等待输入。
"""
from __future__ import annotations

from typing import Any, Sequence

from agent_test.human.service import (
    AskService,
    _render_options,
    _resolve_choice,
    _yes_no,
)
from agent_test.human.stdin_dispatcher import StdinDispatcher
from agent_test.log.runtime_log import RuntimeLog


class CLIAskService(AskService):
    """交互式 CLI 的审批/提问服务（StdinDispatcher 驱动）。"""

    def __init__(self, dispatcher: StdinDispatcher | None = None) -> None:
        self._dispatcher = (
            dispatcher if dispatcher is not None else StdinDispatcher()
        )
        self.confirm_calls: list[dict[str, Any]] = []
        self.ask_calls: list[dict[str, Any]] = []

    @property
    def dispatcher(self) -> StdinDispatcher:
        return self._dispatcher

    def drain_pending(self) -> int:
        """排空陈旧输入（主循环切入审批前调用）。"""
        return self._dispatcher.drain_pending()

    async def confirm(
        self,
        *,
        action: str,
        description: str | None = None,
    ) -> tuple[bool, str]:
        # 关键顺序：先排空再提问（否则缓冲行会被当作审批回答）
        self._dispatcher.drain_pending()
        lines = [f"[confirm] {action}"]
        if description:
            lines.append(f"  说明: {description}")
        lines.append("批准继续? (y/n): ")
        raw = await self._dispatcher.readline("\n".join(lines))
        if raw == "":
            message = "输入已结束（EOF），按拒绝处理"
            self._record(action, description, False, message)
            return False, message
        ok = _yes_no(raw)
        message = "用户已批准" if ok else "用户拒绝"
        self._record(action, description, ok, message)
        return ok, message

    async def ask_user(
        self,
        *,
        question: str,
        options: Sequence[str] | None = None,
        multi_select: bool = False,
        header: str | None = None,
    ) -> str:
        self._dispatcher.drain_pending()
        options = list(options) if options else []
        self.ask_calls.append({"question": question, "options": list(options)})
        title = f"[ask]{'(' + header + ')' if header else ''} {question}"
        lines = [title]
        if options:
            lines.append("候选（可选编号，或直接输入其他内容）：")
            lines.append(_render_options(options))
        print("\n".join(lines))
        raw = await self._dispatcher.readline("> ")
        if raw == "":
            return "(输入已结束，用户未回答)"
        if options:
            return _resolve_choice(raw, options, multi_select)
        return raw.strip() or "(用户未输入)"

    def _record(
        self,
        action: str,
        description: str | None,
        approved: bool,
        message: str,
    ) -> None:
        entry = {
            "action": action,
            "description": description,
            "approved": approved,
            "message": message,
        }
        self.confirm_calls.append(entry)
        RuntimeLog.info(
            "CLI 审批 | action=%s approved=%s message=%s",
            action,
            approved,
            message,
        )
