"""演示入口：向 Agent 发送一条消息，执行 ReAct 循环。

用法:
    python main.py "你好，请介绍一下你自己"
    python main.py "请读取 E:\\workspace\\agent-test\\README.md 的前 30 行"

需要环境变量（或项目根目录下的 .env 文件）:
    API_KEY / BASE_URI / MODEL_NAME

说明：每次运行会新建一个 ReactAgent，其私有 Session 会自动在 sessions/
目录下生成 {session_id}.jsonl 持久化文件，记录本轮全部执行事件。
"""
import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from src.agent import ReactAgent
from src.types import TextBlock, UserMessage
from src.utils import get_uuid


def ensure_env() -> None:
    missing = [k for k in ("API_KEY", "BASE_URI", "MODEL_NAME") if not os.getenv(k)]
    if missing:
        raise SystemExit(
            "缺少环境变量: " + ", ".join(missing)
            + "\n请复制 .env.example 为 .env 并填写。"
        )


async def run(prompt: str) -> None:
    ensure_env()
    agent = ReactAgent()

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


def _quiet_asyncgen_close(_loop, context) -> None:
    """忽略 llama.cpp/OpenAI 流式连接在事件循环关闭时的无害告警。"""
    message = context.get("message", "")
    if message.startswith("an error occurred during closing of asynchronous generator"):
        return
    asyncio.get_event_loop().default_exception_handler(context)


def main() -> None:
    prompt = sys.argv[1] if len(sys.argv) > 1 else "你好，请介绍一下你自己"
    runner = asyncio.Runner()
    runner.get_loop().set_exception_handler(_quiet_asyncgen_close)
    try:
        runner.run(run(prompt))
    finally:
        runner.close()


if __name__ == "__main__":
    main()
