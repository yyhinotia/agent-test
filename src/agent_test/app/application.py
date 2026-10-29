"""CLI 应用入口：交互式多轮会话 + 计划-执行（PAE）任务模式。

用法::

    uv run python -m agent_test.app
    uv run python -m agent_test.app --resume <session_id>
    uv run python -m agent_test.app --allowed-root E:/workspace
    uv run python -m agent_test.app --max-context-tokens 2000   # 易触发压缩
    uv run python -m agent_test.app --drain-policy never        # 脚本化运行（管道）
    uv run python -m agent_test.app --task "分析项目并生成报告" \
        --max-concurrency 3 --max-plan-attempt 3 --auto-approve

设计要点：

- 所有 stdin 读取统一走 StdinDispatcher（主循环 / 审批 / ask_user 共用），
  粘贴多行文本不会被审批误当回答；排空策略 drain_policy 可配（auto 只在
  交互终端排空，never 供管道 / 脚本化运行，见 --drain-policy）；
- 多行输入：行尾 `\\` 续行；空行忽略；连续重复输入去重（终端 workaround）；
- 工具中心装配 CommandPolicy + CLIAskService，Agent 的 _pre_step 串行
  审批、_step 并行执行（审批串行 / 执行并行两阶段分离）；
- 交互模式注册 subagent 工具，LLM 可在一次委托中启一次性纯内存子 Agent；
- `--task` 走 Scheduler 的 PAE 闭环：规划 DAG -> 拓扑分层执行 ->
  Evaluate -> 失败重规划；节点 Agent 会话默认落盘（--no-task-persist 可切回纯内存）；
- /exit /quit 退出，Ctrl-D（EOF）退出；不内置 /help、/allowlist 等
  交互命令，提示信息只在启动时打印。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from agent_test.app.cli_ask_service import CLIAskService
from agent_test.core.agent import create_agent
from agent_test.human.service import AutoApproveService, ConsoleAskService
from agent_test.human.stdin_dispatcher import StdinDispatcher
from agent_test.log.runtime_log import RuntimeLog
from agent_test.policy import CommandPolicy
from agent_test.tools.builtin import register_builtins
from agent_test.tools.center import ToolCenter
from agent_test.tools.subagent import register_subagent
from agent_test.types.events import EventType
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


def _quiet_asyncgen_close(_loop, context) -> None:
    """忽略 llama.cpp/OpenAI 流式连接在事件循环关闭时的无害告警。"""
    message = context.get("message", "")
    if message.startswith(
        "an error occurred during closing of asynchronous generator"
    ):
        return
    _loop.default_exception_handler(context)


def _new_runner() -> asyncio.Runner:
    """创建 Runner 并安装流式连接退出告警过滤器。"""
    runner = asyncio.Runner()
    runner.get_loop().set_exception_handler(_quiet_asyncgen_close)
    return runner


DEFAULT_WORKSPACE = ".agent-test"


class Application:
    """交互式 CLI 应用：装配工具中心/审批服务/Agent，驱动多轮会话。

    默认运行工作区为 `cwd/.agent-test`：
    - 普通对话：`.agent-test/sessions/{session_id}/session.jsonl`
    - 任务模式：`.agent-test/sessions/{graph_id}/{node_session_id}.jsonl`
    - Checkpoint：`.agent-test/checkpoints/{graph_id}.jsonl`
    - 日志：`.agent-test/logs/runtime.log`
    """

    def __init__(
        self,
        *,
        session_id: str | None = None,
        workspace_dir: str = DEFAULT_WORKSPACE,
        persist_dir: str | None = None,
        resume: bool = False,
        log_dir: str | None = None,
        max_context_tokens: int = 128000,
        remain_turns: int = 2,
        allowed_roots: list[str] | None = None,
        tool_center: ToolCenter | None = None,
        ask_service: CLIAskService | None = None,
        dispatcher: StdinDispatcher | None = None,
        drain_policy: str = "auto",
    ) -> None:
        self.session_id = session_id
        self.workspace_dir = Path(workspace_dir)
        self.sessions_root = (
            Path(persist_dir)
            if persist_dir is not None
            else self.workspace_dir / "sessions"
        )
        self.persist_dir = str(self.sessions_root)
        self.resume = resume
        self.log_dir = (
            str(log_dir)
            if log_dir is not None
            else str(self.workspace_dir / "logs")
        )
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
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
        if self.resume:
            if not self.session_id:
                raise SystemExit("--resume 需要同时提供 session_id")
            session_id = self.session_id
        else:
            session_id = self.session_id or get_uuid()
            self.session_id = session_id
        session_file = (
            self.sessions_root / session_id / "session.jsonl"
        )
        kwargs = {
            "persist_dir": self.persist_dir,
            "session_file": str(session_file),
            "tools": self.tool_center,
            "max_context_tokens": self.max_context_tokens,
            "remain_turns": self.remain_turns,
        }
        if self.resume:
            return create_agent(
                resume=True, session_id=session_id, **kwargs
            )
        return create_agent(session_id=session_id, **kwargs)

    # ---------- 主循环 ----------

    async def run(self) -> None:
        """启动交互式会话，直到 /exit 或 EOF。"""
        RuntimeLog.configure(self.log_dir)
        self.agent = self._make_agent()
        # 交互模式：注册 subagent 工具（weakref 捕获 Agent，断开会话与工具闭包循环）
        if (
            self.agent.tool_center is not None
            and "subagent" not in self.agent.tool_center.tools
        ):
            register_subagent(self.agent.tool_center, self.agent)

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
                self._print_last_assistant(self.agent.phase.turn)
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

    def _print_last_assistant(self, turn_no: int | None = None) -> None:
        """打印当前回合最后一条助手文本回复（不重复打印历史消息）。"""
        if self.agent is None or self.agent.session is None:
            return
        if turn_no is None:
            turn_no = self.agent.phase.turn
        last_text = ""
        for event in self.agent.session.events:
            if event.type != EventType.ASSISTANT_MESSAGE.value:
                continue
            if turn_no and event.turn != turn_no:
                continue
            blocks = getattr(event.data, "content", None) or []
            texts = [
                block.content
                for block in blocks
                if isinstance(block, TextBlock)
            ]
            if texts:
                last_text = "".join(texts)
        if last_text:
            print("[assistant]", last_text)


def _build_task_tool_center(args: argparse.Namespace) -> ToolCenter:
    """为 --task 模式装配工具中心（默认不落盘、默认不自动批准）。"""
    roots = args.allowed_root or [str(Path.cwd())]
    approver = AutoApproveService() if args.auto_approve else None
    center = ToolCenter(
        policy=CommandPolicy(allowed_roots=roots),
        approver=approver,
    )
    # 非交互（或显式 --auto-approve）时 ask_user/confirm 工具不阻塞 stdin；
    # 策略层的 REQUIRE_APPROVAL 仍由 approver 控制，二者互不替代。
    ask_service = (
        AutoApproveService()
        if args.auto_approve or not sys.stdin.isatty()
        else ConsoleAskService()
    )
    register_builtins(center, ask_service=ask_service)
    return center


async def run_task_mode(args: argparse.Namespace) -> int:
    """计划-执行（PAE）模式：提交高层目标，由 Scheduler 规划并执行 DAG。

    返回 0 表示 DAG 全部节点成功，1 表示重规划耗尽后仍未达成。
    """
    # 延迟导入：交互模式不必加载调度器，也避免与 app 包循环导入
    from agent_test.app.scheduler import (
        NodeStatus,
        Scheduler,
        _safe_session_id,
    )

    workspace = Path(args.workspace_dir)
    sessions_root = (
        Path(args.persist_dir)
        if args.persist_dir is not None
        else workspace / "sessions"
    )
    checkpoint_dir = (
        Path(args.checkpoint_dir)
        if args.checkpoint_dir is not None
        else workspace / "checkpoints"
    )
    log_dir = (
        Path(args.log_dir)
        if args.log_dir is not None
        else workspace / "logs"
    )
    workspace.mkdir(parents=True, exist_ok=True)
    RuntimeLog.configure(str(log_dir))
    center = _build_task_tool_center(args)
    scheduler = Scheduler(
        checkpoint_file=args.checkpoint_file,
        checkpoint_dir=str(checkpoint_dir),
        max_concurrency=args.max_concurrency,
        max_attempt=args.max_plan_attempt,
        fixture_path=args.fixture,
        tool_center=center,
        persist_sessions=args.task_persist,
        sessions_dir=str(sessions_root),
        max_turns=args.max_turns,
        auto_approve=args.auto_approve,
    )
    if args.resume_checkpoint and args.checkpoint_file is None:
        if args.graph_id:
            checkpoint_dir.mkdir(parents=True, exist_ok=True)
            scheduler.checkpoint_file = (
                checkpoint_dir / f"{_safe_session_id(args.graph_id)}.jsonl"
            )
        # 否则 Scheduler.resume_from_jsonl 自动选择 checkpoint_dir 下最新文件
    print(
        f"[scheduler] workspace={workspace} "
        f"persist_sessions={args.task_persist}"
    )
    print(
        f"[scheduler] sessions_dir={sessions_root} "
        f"checkpoint_dir={checkpoint_dir}"
    )
    if args.resume_checkpoint:
        # 从最新 Layer Checkpoint 继续：不重新规划，直接跑剩余层；
        # 若剩余层仍有未达成节点，resume_from_jsonl 会自动衔接 PAE 重规划。
        state = await scheduler.resume_from_jsonl()
    else:
        state = await scheduler.plan_and_execute(args.task)
    dag = scheduler.task_dag
    print(f"[scheduler] checkpoint_file: {scheduler.checkpoint_file}")
    # plan_attempt 是“已用尝试次数”的游标：成功时最后一次 attempt 也要算 1 次；
    # 耗尽时游标已等于 max，不能直接 +1（否则会打印 4/3）。
    used_attempts = (
        scheduler.plan_attempt
        if scheduler.plan_exhausted
        else min(scheduler.plan_attempt + 1, scheduler.max_plan_attempt)
    )
    print(f"[scheduler] graph_id: {dag.graph_id if dag else '-'}")
    print(f"[scheduler] 最终状态: {state.global_status.value}")
    print(
        f"[scheduler] 尝试次数: {used_attempts}"
        f" / {scheduler.max_plan_attempt}，"
        f"Checkpoint 数: {len(scheduler.check_point_stack)}"
    )
    if dag is not None:
        for node in dag.nodes:
            node_state = state.node_states.get(node.node_id)
            if node_state is None:
                continue
            output = json.dumps(
                node_state.output, ensure_ascii=False, default=str
            )
            if len(output) > 1200:
                output = output[:1200] + "…"
            print(
                f"[node] {node.node_id} status={node_state.status.value} "
                f"rounds={node_state.rounds_used} output={output}"
            )
    return 0 if state.global_status == NodeStatus.SUCCESS else 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    """解析 CLI 参数。"""
    parser = argparse.ArgumentParser(
        prog="agent_test.app",
        description="agent-test 交互式 CLI / DAG 任务调度",
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
        "--workspace-dir",
        default=DEFAULT_WORKSPACE,
        help=(
            "运行工作区（默认 cwd/.agent-test）：sessions/checkpoints/logs "
            "等中间过程文件均存于此"
        ),
    )
    parser.add_argument(
        "--persist-dir",
        default=None,
        help="覆盖会话目录（默认 {workspace}/sessions）",
    )
    parser.add_argument(
        "--log-dir",
        default=None,
        help="覆盖日志目录（默认 {workspace}/logs）",
    )
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
    # ---------- 计划-执行（PAE）模式 ----------
    parser.add_argument(
        "--task",
        default=None,
        help="DAG 调度模式：提交一个高层目标，由 Scheduler 规划并执行",
    )
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=3,
        help="--task 模式下同层并行执行的节点数上限（默认 3）",
    )
    parser.add_argument(
        "--max-plan-attempt",
        type=int,
        default=3,
        help="--task 模式下 PAE 最大规划/重规划次数（默认 3）",
    )
    parser.add_argument(
        "--max-turns",
        "--max-turn",
        dest="max_turns",
        type=int,
        default=None,
        help=(
            "--task 模式每个节点 Agent 的最大回合数下限（只增不减）；"
            "缺省使用 planner 的节点 max_rounds"
        ),
    )
    parser.add_argument(
        "--checkpoint-file",
        default=None,
        help=(
            "显式指定 Layer Checkpoint JSONL；缺省按 "
            "{checkpoint-dir}/{graph_id}.jsonl 自动创建"
        ),
    )
    parser.add_argument(
        "--checkpoint-dir",
        default=None,
        help="Checkpoint 目录（默认 {workspace}/checkpoints）",
    )
    parser.add_argument(
        "--graph-id",
        default=None,
        help="--resume-checkpoint 时指定要恢复的 graph_id",
    )
    parser.add_argument(
        "--resume-checkpoint",
        action="store_true",
        help=(
            "不重新规划，从 --checkpoint-file 或 {checkpoint-dir}/ 下最新 "
            "Checkpoint 继续执行；剩余层若仍有未达成节点，会自动进入 PAE 重规划"
        ),
    )
    parser.add_argument(
        "--fixture",
        default=None,
        help="测试/回放用 DAG fixture JSON 路径（优先于 LLM 规划）",
    )
    parser.add_argument(
        "--task-persist",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "--task 模式节点 Agent 会话落盘（默认开启，便于观察 scheduler/"
            "agent 交互；使用 --no-task-persist 切换为纯内存）"
        ),
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="--task 模式自动批准 REQUIRE_APPROVAL 命令（DENY 仍拦截）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int | None:
    """CLI 入口。"""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    ensure_env()
    if args.task or args.resume_checkpoint:
        runner = _new_runner()
        try:
            return runner.run(run_task_mode(args))
        except (KeyboardInterrupt, EOFError):
            print("\n[info] 已中断。")
            return 1
        finally:
            runner.close()

    app = Application(
        session_id=args.session_id,
        workspace_dir=args.workspace_dir,
        persist_dir=args.persist_dir,
        resume=args.resume,
        log_dir=args.log_dir,
        max_context_tokens=args.max_context_tokens,
        remain_turns=args.remain_turns,
        allowed_roots=args.allowed_root,
        drain_policy=args.drain_policy,
    )
    runner = _new_runner()
    try:
        runner.run(app.run())
    except (KeyboardInterrupt, EOFError):
        print("\n[info] 已中断。")
    finally:
        runner.close()
    return None


if __name__ == "__main__":
    raise SystemExit(main())
