"""CLI 应用入口：交互式多轮会话（主循环 + 统一 stdin + 审批服务）。

用法::

    uv run python -m agent_test.app
    uv run python -m agent_test.app --resume <session_id>
    uv run python -m agent_test.app --allowed-root E:/workspace
    uv run python -m agent_test.app --max-context-tokens 2000   # 易触发压缩
    uv run python -m agent_test.app --drain-policy never        # 脚本化运行（管道）

设计要点（docs/增量知识.md）：

- 所有 stdin 读取统一走 StdinDispatcher（主循环 / 审批 / ask_user 共用），
  粘贴多行文本不会被审批误当回答；排空策略 drain_policy 可配（auto 只在
  交互终端排空，never 供管道 / 脚本化运行，见 --drain-policy）；
- 多行输入：行尾 `\\` 续行；空行忽略；连续重复输入去重（终端 workaround）；
- 工具中心装配 CommandPolicy + CLIAskService，Agent 的 _pre_step 串行
  审批、_step 并行执行（审批串行 / 执行并行两阶段分离）；
- /exit /quit 退出，Ctrl-D（EOF）退出（不内置 /help、/allowlist 等
  交互命令——按增量知识包 DO_NOT_REPLICATE 约定，提示信息只在启动时打印）。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from agent_test.app.cli_ask_service import CLIAskService
from agent_test.core.agent import create_agent
from agent_test.human.stdin_dispatcher import StdinDispatcher
from agent_test.log.runtime_log import RuntimeLog
from agent_test.policy import CommandPolicy
from agent_test.tools.builtin import register_builtins
from agent_test.tools.center import ToolCenter
from agent_test.types.messages import TextBlock, UserMessage
from agent_test.utils import get_uuid

_EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit", ":q"}
_BANNER = (
    "agent-test CLI（ReAct Agent）。/exit 或 Ctrl-D 退出。\n"
    "多行输入：行尾 `\\` 续行（一行 = 一个回合）；"
    "交互终端下审批前请勿粘贴多行文本。"
)


def ensure_env() -> None:
    """校验 LLM 连接所需环境变量。"""
    missing = [k for k in ("API_KEY", "BASE_URI", "MODEL_NAME") if not os.getenv(k)]
    if missing:
        raise SystemExit(
            "缺少环境变量: "
            + ", ".join(missing)
            + "\n请复制 .env.example 为 .env 并填写。"
        )


class Application:
    """交互式 CLI 应用：装配工具中心/审批服务/Agent，驱动多轮会话。"""

    def __init__(
        self,
        *,
        session_id: str | None = None,
        persist_dir: str = "sessions",
        resume: bool = False,
        log_dir: str = "logs",
        max_context_tokens: int = 128000,
        remain_turns: int = 2,
        allowed_roots: list[str] | None = None,
        tool_center: ToolCenter | None = None,
        ask_service: CLIAskService | None = None,
        dispatcher: StdinDispatcher | None = None,
        drain_policy: str = "auto",
    ) -> None:
        self.session_id = session_id
        self.persist_dir = persist_dir
        self.resume = resume
        self.log_dir = log_dir
        # 上下文窗口：max_context_tokens 调小可在手工测试中确定性触发压缩
        # （threshold = max_context_tokens * 0.8，见 TokenMeter）
        self.max_context_tokens = max_context_tokens
        self.remain_turns = remain_turns
        self.drain_policy = drain_policy
        self.dispatcher = (
            dispatcher
            if dispatcher is not None
            else StdinDispatcher(drain_policy=drain_policy)
        )
        self.ask_service = (
            ask_service
            if ask_service is not None
            else CLIAskService(self.dispatcher)
        )
        self.allowed_roots = (
            allowed_roots if allowed_roots else [str(Path.cwd())]
        )
        self.tool_center = (
            tool_center
            if tool_center is not None
            else self.build_tool_center(self.allowed_roots)
        )
        self.agent = None
        self._last_input: str | None = None

    def build_tool_center(self, allowed_roots: list[str]) -> ToolCenter:
        """装配工具中心：策略闸门 + CLI 审批服务 + 内置工具。"""
        center = ToolCenter(
            policy=CommandPolicy(allowed_roots=allowed_roots),
            approver=self.ask_service,
        )
        register_builtins(center, ask_service=self.ask_service)
        return center

    # ---------- Agent ----------

    def _make_agent(self):
        kwargs = {
            "persist_dir": self.persist_dir,
            "tools": self.tool_center,
            "max_context_tokens": self.max_context_tokens,
            "remain_turns": self.remain_turns,
        }
        if self.resume:
            if not self.session_id:
                raise SystemExit("--resume 需要同时提供 session_id")
            return create_agent(resume=True, session_id=self.session_id, **kwargs)
        return create_agent(session_id=self.session_id, **kwargs)

    # ---------- 主循环 ----------

    async def run(self) -> None:
        """启动交互式会话，直到 /exit 或 EOF。"""
        RuntimeLog.configure(self.log_dir)
        self.agent = self._make_agent()

        print(_BANNER)
        print(f"[info] session_id: {self.agent.session.session_id}")
        print(f"[info] 持久化文件: {self.agent.session.file_path}")
        print(
            "[info] 上下文窗口: max_context_tokens="
            f"{self.max_context_tokens} remain_turns={self.remain_turns}"
        )
        policy = getattr(self.dispatcher, "drain_policy", self.drain_policy)
        enabled = getattr(self.dispatcher, "drain_enabled", None)
        print(
            f"[info] drain 策略: {policy}"
            f"（排空陈旧输入: {'开' if enabled else '关'}）"
        )

        tokens = RuntimeLog.bind(session_id=self.agent.session.session_id)
        try:
            while True:
                text = await self._read_user_input()
                if text is None:
                    print("\n[info] 输入已结束（EOF），退出。")
                    return
                stripped = text.strip()
                if not stripped:
                    continue
                lowered = stripped.lower()
                if lowered in _EXIT_COMMANDS:
                    print("[info] 退出。")
                    return
                if not self._accept_input(stripped):
                    print("[warn] 与上一条输入重复，已忽略。")
                    continue

                self.agent.inbox.append(
                    "turn",
                    UserMessage(
                        id=get_uuid(), content=[TextBlock(content=stripped)]
                    ),
                )
                await self.agent.turn()
                self._print_last_assistant()
        finally:
            RuntimeLog.unbind(tokens)
            self.dispatcher.close()

    async def _read_user_input(self) -> str | None:
        """读取一条用户输入（支持 `\\` 续行），EOF 返回 None。"""
        first = await self.dispatcher.readline("> ")
        if first == "":
            return None
        lines = [first]
        while lines[-1].rstrip().endswith("\\"):
            lines[-1] = lines[-1].rstrip()[:-1].rstrip()
            cont = await self.dispatcher.readline("... ")
            if cont == "":
                break
            lines.append(cont)
        return "\n".join(lines)

    def _accept_input(self, text: str) -> bool:
        """多行去重保护：终端重复回显/重复粘贴的同一条输入只处理一次。"""
        if text == self._last_input:
            return False
        self._last_input = text
        return True

    def _print_last_assistant(self) -> None:
        """打印本回合最后的助手文本回复（真实落 session 的内容）。"""
        for event in self.agent.session.events:
            if event.type != "assistant/message":
                continue
            blocks = getattr(event.data, "content", None) or []
            texts = [
                block.content
                for block in blocks
                if isinstance(block, TextBlock)
            ]
            if texts:
                print("[assistant]", "".join(texts))


def parse_args(argv: list[str]) -> argparse.Namespace:
    """解析 CLI 参数。"""
    parser = argparse.ArgumentParser(
        prog="agent_test.app", description="agent-test 交互式 CLI"
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="指定会话 id（缺省自动生成）",
    )
    parser.add_argument(
        "-r",
        "--resume",
        action="store_true",
        help="窗口化重载 --session-id 指定的会话后续聊",
    )
    parser.add_argument(
        "--persist-dir", default="sessions", help="会话 JSONL 持久化目录"
    )
    parser.add_argument("--log-dir", default="logs", help="运行时日志目录")
    parser.add_argument(
        "--max-context-tokens",
        type=int,
        default=128000,
        help="上下文窗口 token 上限（调小可触发压缩，便于手工验证）",
    )
    parser.add_argument(
        "--remain-turns",
        type=int,
        default=2,
        help="压缩时按 step 粒度保留的近期步数（默认 2）",
    )
    parser.add_argument(
        "--drain-policy",
        choices=("auto", "always", "never"),
        default="auto",
        help=(
            "审批前是否排空陈旧输入：auto=仅交互终端排空（缺省）；"
            "always=总是排空；never=从不排空（管道/脚本化运行推荐）"
        ),
    )
    parser.add_argument(
        "--allowed-root",
        action="append",
        default=None,
        help="允许的工作区根（可重复；缺省为当前目录）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """CLI 入口。"""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    ensure_env()
    app = Application(
        session_id=args.session_id,
        persist_dir=args.persist_dir,
        resume=args.resume,
        log_dir=args.log_dir,
        max_context_tokens=args.max_context_tokens,
        remain_turns=args.remain_turns,
        allowed_roots=args.allowed_root,
        drain_policy=args.drain_policy,
    )
    runner = asyncio.Runner()
    try:
        runner.run(app.run())
    except (KeyboardInterrupt, EOFError):
        print("\n[info] 已中断。")
    finally:
        runner.close()


if __name__ == "__main__":
    main()
