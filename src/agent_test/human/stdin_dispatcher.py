"""统一 stdin 读取器：后台 daemon 线程独占 input()，按行投递到 asyncio 队列。

问题（知识增量 5.3）
------------------
异步 CLI 应用里主循环用 input() 读用户输入，审批服务用
asyncio.to_thread(input) 读审批回答。两者共享同一个 OS 终端行缓冲区，
读取顺序不确定：用户在审批弹出前粘贴的多行文本，会被审批 input() 读到，
导致"没有手动输入却被自动批准/拒绝"。

做法
----
- 一个后台 daemon 线程独占 input()，每行经 call_soon_threadsafe 投递到
  asyncio.Queue（线程安全：只有队列操作被调度回事件循环）；
- 所有读取路径（主循环 / confirm / ask_user）统一走 readline()；
- 审批/提问前先 drain_pending() 排空队列中的陈旧缓冲行；
- 读到 EOF 时投递哨兵，readline() 返回空串，调用方据此降级/退出。

排空策略（drain_policy，真实会话复盘后新增）
------------------------------------------
drain_pending() 只在“用户可能在终端里提前粘贴了多行文本”时才应该生效：
交互式终端下有这个竞态，而管道 / 重定向（脚本化运行）下每一行都是真实
输入，排空会把脚本自己的行（含 y/n 回答）吞掉。因此：

- auto（缺省）：stdin 是交互终端（或注入了 input_func）才排空；
- always：总是排空（等价旧行为）；
- never：从不排空（脚本化运行推荐，回答按顺序被消费）。

约束：daemon 线程无法优雅停止（input() 阻塞在系统调用上），
随进程退出即可；这是本方案的已知取舍。
"""
from __future__ import annotations

import asyncio
import sys
import threading


_DRAIN_POLICIES = ("auto", "always", "never")


def stdin_is_tty() -> bool:
    """stdin 是否为交互式终端（管道 / 重定向返回 False）。"""
    stdin = getattr(sys, "stdin", None)
    try:
        return bool(stdin is not None and stdin.isatty())
    except (AttributeError, ValueError):
        return False


class StdinDispatcher:
    """stdin 唯一读取入口：后台线程 + 队列分发 + drain_pending。"""

    def __init__(
        self,
        *,
        prompt: str = "",
        input_func=None,
        drain_policy: str = "auto",
    ) -> None:
        # input_func: 可选注入的读取函数（缺省 builtins.input）。测试/嵌入方
        # 传入脚本化读取即可，无需 monkeypatch 全局内置函数。
        # drain_policy: auto / always / never，见模块 docstring。
        if drain_policy not in _DRAIN_POLICIES:
            raise ValueError(
                f"drain_policy 必须是 {_DRAIN_POLICIES} 之一，收到: {drain_policy!r}"
            )
        self._prompt = prompt
        self._input = input_func
        self._drain_policy = drain_policy
        self._queue: asyncio.Queue[str | None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._closed = False
        self._eof = False

    # ---------- 生命周期 ----------

    def _start(self, loop: asyncio.AbstractEventLoop) -> asyncio.Queue:
        """懒启动：首次 readline 时才创建队列与后台线程。"""
        if self._queue is None:
            self._queue = asyncio.Queue()
            self._loop = loop
            self._thread = threading.Thread(
                target=self._read_loop, name="stdin-dispatcher", daemon=True
            )
            self._thread.start()
        return self._queue

    def _read_loop(self) -> None:
        """后台线程主体：独占 input()，逐行投递（EOF 投递哨兵）。"""
        while not self._closed:
            try:
                line = self._read_one()
            except EOFError:
                if not self._closed:
                    self._push(None)
                return
            except Exception:  # noqa: BLE001
                # stdin 不可用（非交互环境/已关闭）等同 EOF，避免线程静默死掉
                if not self._closed:
                    self._push(None)
                return
            if self._closed:
                return
            self._push(line)

    def _read_one(self) -> str:
        """读一行：优先注入的 input_func，否则走 builtins.input。"""
        if self._input is not None:
            return self._input()
        return input()

    def _push(self, line: str | None) -> None:
        """把一行投递回事件循环。

        投递顺序很关键：EOF 标记必须在哨兵真正入队之后才置位，
        否则「线程已读到 EOF，但行还在事件循环的待执行回调里」时，
        readline 会看到 _eof=True + 队列空而误判 EOF，把最后一行丢掉。
        因此置位与入队合并到同一个回调里执行。
        """
        loop, queue = self._loop, self._queue
        if loop is None or queue is None:
            return
        try:
            if line is None:
                loop.call_soon_threadsafe(self._mark_eof, queue)
            else:
                loop.call_soon_threadsafe(queue.put_nowait, line)
        except RuntimeError:
            # 事件循环已关闭（进程退出路径）：丢弃，无需告警
            pass

    def _mark_eof(self, queue: asyncio.Queue) -> None:
        """在事件循环内标记 EOF 并投递哨兵（保证顺序）。"""
        self._eof = True
        queue.put_nowait(None)

    # ---------- 读取 ----------

    async def readline(self, prompt: str = "") -> str:
        """异步读取一行；EOF 返回空串。

        prompt 非空时先输出提示（不换行），保证提示与等待成对出现。
        """
        queue = self._start(asyncio.get_running_loop())
        if prompt:
            print(prompt, end="", flush=True)
        if self._eof and queue.empty():
            return ""
        line = await queue.get()
        return "" if line is None else line

    @property
    def drain_policy(self) -> str:
        return self._drain_policy

    @property
    def drain_enabled(self) -> bool:
        """当前是否会真的排空陈旧输入（供 CLI 启动信息/审计使用）。"""
        if self._drain_policy == "always":
            return True
        if self._drain_policy == "never":
            return False
        # auto：只有交互式终端（或测试注入的脚本化读取）才有“提前粘贴”竞态
        return self._input is not None or stdin_is_tty()

    def drain_pending(self) -> int:
        """排空队列中已缓冲的陈旧行（非阻塞），返回丢弃行数。

        审批/提问前必须调用：否则粘贴的多行文本会被当作审批回答。
        管道 / 重定向下（drain_enabled=False）不排空，避免吞掉脚本自己的行。
        EOF 哨兵不丢弃（保留 _eof 标记），避免后续 readline 永久阻塞。
        """
        if not self.drain_enabled:
            return 0
        queue = self._queue
        if queue is None:
            return 0
        dropped = 0
        while True:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                return dropped
            if item is None:
                self._eof = True
                continue
            dropped += 1

    def close(self) -> None:
        """尽力关闭（后台线程阻塞在 input() 上，只能标记停止）。"""
        self._closed = True
