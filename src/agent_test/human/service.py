"""用户交互服务（Human-in-the-Loop）：ask_user / confirm 的统一抽象。

设计依据（现行优秀 agent 工具，详见 docs/hitl-tool-design-research.md）：
- Claude Code AskUserQuestion / Agno UserFeedbackTools / Timbal ask_user：
  澄清类问题带 2-4 个候选选项，用户可选项或自由输入，运行暂停、拿到回答后续跑；
- Claude Code canUseTool / Timbal confirm / LangGraph interrupt：
  批准类交互把“不可逆动作”挡在门后——批准返回 bool，拒绝返回理由；
- 运行暂停在拿到用户输入前不产生任何该动作的副作用（对应本仓库 pre_step
  决策先于副作用的原则）。

本层是可注入的服务边界：
- 默认 ConsoleAskService：CLI 里用 input() 交互；
- 测试/嵌入方实现 AskService 注入即可（如脚本化回答、网页表单、MCP）。
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Sequence

# 批准回答的肯定集合（大小写不敏感）
_YES = {"y", "yes", "1", "是", "同意", "批准", "继续"}


def _yes_no(text: str) -> bool:
    return text.strip().lower() in _YES


def _render_options(options: Sequence[str]) -> str:
    return "\n".join(f"  {i + 1}. {label}" for i, label in enumerate(options))


class AskService(ABC):
    """用户交互服务抽象。所有方法均为 async，便于嵌入方对接真实 UI。"""

    @abstractmethod
    async def ask_user(
        self,
        *,
        question: str,
        options: Sequence[str] | None = None,
        multi_select: bool = False,
        header: str | None = None,
    ) -> str:
        """向用户提一个澄清/补充消息的问题，返回其回答文本。

        options 非空时用户可选项（label）或输入其他内容；
        multi_select=True 时允许多选（逗号分隔），返回逗号拼接文本。
        """

    @abstractmethod
    async def confirm(
        self,
        *,
        action: str,
        description: str | None = None,
    ) -> tuple[bool, str]:
        """请用户批准一个动作（如执行某命令）。

        返回 (是否批准, 消息)：批准消息可作审计备注；
        拒绝消息会回传给 LLM 供其调整方案。
        """


class ConsoleAskService(AskService):
    """CLI 交互实现：通过标准输入收集用户回答。"""

    async def ask_user(
        self,
        *,
        question: str,
        options: Sequence[str] | None = None,
        multi_select: bool = False,
        header: str | None = None,
    ) -> str:
        options = list(options) if options else []
        title = f"[ask]{'(' + header + ')' if header else ''} {question}"
        lines = [title]
        if options:
            lines.append("候选（可选编号，或直接输入其他内容）：")
            lines.append(_render_options(options))
        print("\n".join(lines))
        if options:
            raw = await asyncio.to_thread(input, "> ")
            return _resolve_choice(raw, options, multi_select)
        raw = await asyncio.to_thread(input, "> ")
        return raw.strip() or "(用户未输入)"

    async def confirm(
        self,
        *,
        action: str,
        description: str | None = None,
    ) -> tuple[bool, str]:
        lines = [f"[confirm] {action}"]
        if description:
            lines.append(f"  说明: {description}")
        lines.append("批准继续? (y/n): ")
        raw = await asyncio.to_thread(input, "\n".join(lines))
        ok = _yes_no(raw)
        return ok, ("用户已批准" if ok else "用户拒绝")


def _resolve_choice(
    raw: str, options: list[str], multi_select: bool
) -> str:
    """解析编号选择或自由文本：单选返回 label/文本，多选返回逗号拼接。"""
    text = raw.strip()
    if not text:
        return "(用户未选择)"
    picked: list[str] = []
    rest: list[str] = []
    for part in text.split(","):
        token = part.strip()
        if token.isdigit():
            idx = int(token) - 1
            if 0 <= idx < len(options):
                picked.append(options[idx])
                continue
        if token:
            rest.append(token)
    if picked or rest:
        return ", ".join(picked + rest)
    return text