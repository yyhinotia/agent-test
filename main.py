"""演示入口：向 Agent 发送一条消息，执行 ReAct 循环。

用法:
    uv run python main.py "你好，请介绍一下你自己"
    uv run python main.py "请读取 E:\\workspace\\agent-test\\README.md 的前 30 行"
    uv run python main.py --resume <session_id> "接着刚才的问题继续"

需要环境变量（或项目根目录下的 .env 文件）:
    API_KEY / BASE_URI / MODEL_NAME

说明：
- 默认运行工作区为 cwd/.agent-test；普通会话路径为
  .agent-test/sessions/{session_id}/session.jsonl；
- --resume <session_id>：从上述路径窗口化恢复并续接编号；
- 所有 agent-runtime 日志写入 .agent-test/logs/runtime.log；
- 演示入口自动注册 subagent 工具。
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from agent_test import create_agent
from agent_test.log.runtime_log import RuntimeLog
from agent_test.tools.subagent import register_subagent
from agent_test.types.messages import TextBlock, UserMessage
from agent_test.utils import get_uuid


def ensure_env() -> None:
    missing = [k for k in ("API_KEY", "BASE_URI", "MODEL_NAME") if not os.getenv(k)]
    if missing:
        raise SystemExit(
            "缺少环境变量: "
            + ", ".join(missing)
            + "\n请复制 .env.example 为 .env 并填写。"
        )


async def run(prompt: str, resume: str | None = None) -> None:
    ensure_env()
    workspace = Path(".agent-test")
    sessions_root = workspace / "sessions"
    RuntimeLog.configure(workspace / "logs")
    session_id = resume or get_uuid()
    session_file = sessions_root / session_id / "session.jsonl"
    agent = (
        create_agent(
            resume=True,
            session_id=session_id,
            session_file=str(session_file),
        )
        if resume
        else create_agent(
            session_id=session_id,
            session_file=str(session_file),
        )
    )
    # 演示入口同样启用 subagent 子 Agent 委托（weakref 捕获，不阻塞 GC）
    if agent.tool_center is not None and "subagent" not in agent.tool_center.tools:
        register_subagent(agent.tool_center, agent)
    tokens = RuntimeLog.bind(session_id=agent.session.session_id)
    try:
        # 1. 把用户消息放入本 Agent 的 inbox turn 队列
        agent.inbox.append(
            "turn",
            UserMessage(id=get_uuid(), content=[TextBlock(content=prompt)]),
        )

        # 2. 执行一轮（内部可能包含多步工具调用）
        ok = await agent.turn()
        print(f"[info] turn 执行完成: {ok}")
        print(f"[info] session_id: {agent.session.session_id}")
        print(f"[info] 持久化文件: {agent.session.file_path}")
        print(
            "[info] 续聊: uv run python main.py --resume "
            f"{agent.session.session_id} \"<你的下一条消息>\""
        )
        print(f"[info] 日志文件: {RuntimeLog.log_file_path()}")

        # 3. 打印助手最终的文本回复
        for event in agent.session.events:
            if event.type != "assistant/message":
                continue
            data = event.data
            blocks = getattr(data, "content", None)
            if not blocks:
                continue
            texts = [block.content for block in blocks if isinstance(block, TextBlock)]
            if texts:
                print("[assistant]", "".join(texts))
    finally:
        RuntimeLog.unbind(tokens)
        agent.destroy()


def _quiet_asyncgen_close(_loop, context) -> None:
    """忽略 llama.cpp/OpenAI 流式连接在事件循环关闭时的无害告警。"""
    message = context.get("message", "")
    if message.startswith("an error occurred during closing of asynchronous generator"):
        return
    asyncio.get_event_loop().default_exception_handler(context)


def parse_args(argv: list[str]) -> argparse.Namespace:
    """解析演示入口参数（prompt 可选，--resume 指定续聊会话）。"""
    parser = argparse.ArgumentParser(description="agent-test 演示入口（ReAct Agent）")
    parser.add_argument(
        "prompt",
        nargs="?",
        default="你好，请介绍一下你自己",
        help="用户消息",
    )
    parser.add_argument(
        "-r",
        "--resume",
        metavar="SESSION_ID",
        help="窗口化重载该会话后续聊（session-reload + 续接编号）",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args(sys.argv[1:])
    runner = asyncio.Runner()
    runner.get_loop().set_exception_handler(_quiet_asyncgen_close)
    try:
        runner.run(run(args.prompt, args.resume))
    finally:
        runner.close()


if __name__ == "__main__":
    main()