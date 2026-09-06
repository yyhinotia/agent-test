"""用户交互工具：ask_user（澄清/补充消息）与 confirm（批准继续进行）。

与 bash 工具同构：执行层不持有 UI——具体交互通过注入的 AskService
（默认 ConsoleAskService）完成，便于测试与嵌入真实 UI（网页/MCP 等）。

设计要点
--------
- ask_user：Agent 拿不准方向 / 需要用户补充信息时调用。支持候选选项
  （用户可选项或输入其他内容）与 multi_select，运行在用户回答前暂停；
- confirm：Agent 请求用户批准后继续（如删除临时目录、提交变更）。
  批准结果作为 tool/result 回传 LLM 与写入 session，可回放可审计；
- 审批继续（REQUIRE_APPROVAL -> 批准 -> 记住 -> 执行）由 ToolCenter
  策略闸门 + approver 完成，不占这两个工具名额（见 center.py / policy.py）。
"""
from __future__ import annotations

from typing import Any, Dict, Sequence

from agent_test.human.service import AskService, ConsoleAskService
from agent_test.tools.center import ToolCenter

_ASK_PARAMETERS: Dict[str, Any] = {
    "question": {
        "type": "string",
        "description": "要向用户提出的问题（建议以 ? 结尾）",
    },
    "header": {
        "type": "string",
        "description": "短标签（建议 ≤12 字符），如 数据库 / 清理方式",
    },
    "options": {
        "type": "array",
        "items": {"type": "string"},
        "description": "2-4 个候选选项；用户可选择编号或输入其他内容",
    },
    "multi_select": {
        "type": "boolean",
        "description": "是否允许多选（逗号分隔），默认 False",
    },
}

_CONFIRM_PARAMETERS: Dict[str, Any] = {
    "action": {
        "type": "string",
        "description": "获准后将执行的动作描述（如：删除临时目录 tmp/x）",
    },
    "description": {
        "type": "string",
        "description": "补充说明（背景、影响面、回滚方式等）",
    },
}


def register_ask(
    center: ToolCenter,
    service: AskService | None = None,
    *,
    name_ask: str = "ask_user",
    name_confirm: str = "confirm",
) -> None:
    """把 ask_user / confirm 工具注册到指定 ToolCenter。

    service 缺省使用 ConsoleAskService（CLI 交互）；
    测试/嵌入方传入自定义 AskService 即可脚本化回答。
    """
    svc: AskService = service if service is not None else ConsoleAskService()

    async def ask_user(
        question: str,
        header: str | None = None,
        options: Sequence[str] | None = None,
        multi_select: bool = False,
    ) -> str:
        """向用户提一个问题，等待其补充消息/选择，返回回答文本。"""
        return await svc.ask_user(
            question=question,
            options=list(options) if options else None,
            multi_select=multi_select,
            header=header,
        )

    async def confirm(
        action: str,
        description: str | None = None,
    ) -> str:
        """请用户批准后继续：返回 approved / denied（含原因）。"""
        approved, message = await svc.confirm(action=action, description=description)
        prefix = "approved" if approved else "denied"
        return f"{prefix}: {message}"

    center.register(
        desc=(
            "用户交互工具：向用户提问以澄清任务/收集补充消息；"
            "支持 2-4 个候选选项或自由输入，等待用户回答后返回其内容"
        ),
        parameters=_ASK_PARAMETERS,
        required=["question"],
        name=name_ask,
    )(ask_user)
    center.register(
        desc=(
            "用户审批工具：在执行影响面较大的动作（删除/提交/继续）前"
            "请用户批准；返回 approved 或 denied 及原因"
        ),
        parameters=_CONFIRM_PARAMETERS,
        required=["action"],
        name=name_confirm,
    )(confirm)