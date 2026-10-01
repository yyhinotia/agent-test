"""知识增量：统一 stdin 分发器（StdinDispatcher）+ 排空陈旧输入 + EOF 降级。"""
import asyncio
import io

from agent_test.app.application import Application, parse_args
from agent_test.app.cli_ask_service import CLIAskService
from agent_test.human.service import ConsoleAskService
from agent_test.human.stdin_dispatcher import StdinDispatcher


class FakeStdin:
    """StdinDispatcher 替身：脚本化行 + 记录 drain/close 调用。"""

    def __init__(self, lines=()) -> None:
        self.lines = list(lines)
        self.drained = 0
        self.prompts: list[str] = []
        self.closed = False

    async def readline(self, prompt: str = "") -> str:
        self.prompts.append(prompt)
        return self.lines.pop(0) if self.lines else ""

    def drain_pending(self) -> int:
        # 只记录调用：脚本里的行代表"排空之后"用户真正输入的内容
        self.drained += 1
        return 0

    def close(self) -> None:
        self.closed = True


# ---------------- StdinDispatcher 本体 ----------------

def _scripted_input(lines):
    """脚本化读取函数（模拟后台线程从终端读到的行）。"""
    iterator = iter(lines)

    def fake_input(prompt=""):
        try:
            return next(iterator)
        except StopIteration:
            raise EOFError

    return fake_input


def _patch_stdin(monkeypatch, lines):
    """把 builtins.input 替换为脚本化读取（供 ConsoleAskService 回退路径）。"""
    monkeypatch.setattr("builtins.input", _scripted_input(lines))


def test_dispatcher_readline_then_eof():
    """读到最后一行后即使立刻 EOF，也不能丢掉那一行（顺序竞态回归）。"""

    async def scenario():
        dispatcher = StdinDispatcher(input_func=_scripted_input(["hello"]))
        try:
            assert await dispatcher.readline("> ") == "hello"
            # 线程读到 EOF -> 投递哨兵 -> readline 返回空串（不阻塞）
            assert await asyncio.wait_for(dispatcher.readline(), timeout=2) == ""
        finally:
            dispatcher.close()

    asyncio.run(scenario())


def test_dispatcher_drain_pending_discards_stale_lines():
    """粘贴的多行文本必须在审批前被排空（否则会被当成审批回答）。"""

    async def scenario():
        dispatcher = StdinDispatcher(
            input_func=_scripted_input(["stale-1", "stale-2", "stale-3"])
        )
        try:
            assert await dispatcher.readline() == "stale-1"
            for _ in range(200):
                if dispatcher._queue.qsize() >= 2:
                    break
                await asyncio.sleep(0.005)
            assert dispatcher.drain_pending() == 2
            assert dispatcher.drain_pending() == 0
            # 哨兵不被 drain 吃掉：仍能读到 EOF
            assert await asyncio.wait_for(dispatcher.readline(), timeout=2) == ""
        finally:
            dispatcher.close()

    asyncio.run(scenario())


# ---------------- ConsoleAskService 接入 ----------------

def test_console_confirm_drains_before_reading():
    fake = FakeStdin(["y"])
    svc = ConsoleAskService(dispatcher=fake)
    approved, message = asyncio.run(svc.confirm(action="执行命令: git push"))
    assert approved is True
    assert message == "用户已批准"
    assert fake.drained == 1, "审批前必须排空陈旧输入"


def test_console_ask_user_uses_dispatcher():
    fake = FakeStdin(["2"])
    svc = ConsoleAskService(dispatcher=fake)
    answer = asyncio.run(
        svc.ask_user(question="选一个?", options=["MySQL", "PostgreSQL"])
    )
    assert answer == "PostgreSQL"
    assert fake.drained == 1


def test_console_without_dispatcher_still_uses_input(monkeypatch):
    """未注入分发器时退化为 to_thread(input)，保持既有行为。"""
    _patch_stdin(monkeypatch, ["自由输入", "n"])

    async def scenario():
        svc = ConsoleAskService()
        assert await svc.ask_user(question="补充?") == "自由输入"
        approved, message = await svc.confirm(action="继续?")
        assert approved is False and "拒绝" in message

    asyncio.run(scenario())


# ---------------- CLIAskService ----------------

def test_cli_ask_service_confirm_audit_log():
    fake = FakeStdin(["y"])
    svc = CLIAskService(fake)
    approved, message = asyncio.run(svc.confirm(action="执行命令: git push"))
    assert approved is True
    assert fake.drained == 1
    assert svc.confirm_calls == [
        {
            "action": "执行命令: git push",
            "description": None,
            "approved": True,
            "message": "用户已批准",
        }
    ]


def test_cli_ask_service_denied_records_reason():
    svc = CLIAskService(FakeStdin(["n"]))
    approved, message = asyncio.run(svc.confirm(action="执行命令: rm -rf /"))
    assert approved is False
    assert message == "用户拒绝"
    assert svc.confirm_calls[0]["approved"] is False


def test_cli_ask_service_eof_falls_back_to_deny():
    svc = CLIAskService(FakeStdin([]))
    approved, message = asyncio.run(svc.confirm(action="执行命令: x"))
    assert approved is False
    assert "EOF" in message
    assert svc.confirm_calls[0]["approved"] is False


def test_cli_ask_service_ask_user_choice_and_eof():
    svc = CLIAskService(FakeStdin(["2"]))
    answer = asyncio.run(
        svc.ask_user(question="选一个?", options=["A", "B"])
    )
    assert answer == "B"
    assert svc.ask_calls[0]["question"] == "选一个?"

    eof_svc = CLIAskService(FakeStdin([]))
    assert "输入已结束" in asyncio.run(eof_svc.ask_user(question="?"))


# ---------------- Application ----------------

def test_application_reads_multiline_continuation(tmp_path):
    app = Application(
        persist_dir=str(tmp_path),
        dispatcher=FakeStdin(["第一行 \\", "第二行"]),
    )
    assert asyncio.run(app._read_user_input()) == "第一行\n第二行"


def test_application_dedups_repeated_input(tmp_path):
    app = Application(persist_dir=str(tmp_path), dispatcher=FakeStdin())
    assert app._accept_input("同一句") is True
    assert app._accept_input("同一句") is False
    assert app._accept_input("另一句") is True


def test_application_wires_policy_and_approver(tmp_path):
    app = Application(persist_dir=str(tmp_path), dispatcher=FakeStdin())
    assert app.tool_center.policy is not None
    assert app.tool_center.approver is app.ask_service
    names = {s["function"]["name"] for s in (app.tool_center.get_schemas() or [])}
    assert {"bash", "ask_user", "confirm"} <= names


# ---------------- drain_policy（真实会话复盘后新增） ----------------

def test_drain_policy_auto_disabled_when_stdin_is_not_tty(monkeypatch):
    """管道/重定向下不排空：否则会吞掉脚本自己的行（含 y/n 回答）。"""
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    dispatcher = StdinDispatcher()
    assert dispatcher.drain_policy == "auto"
    assert dispatcher.drain_enabled is False


def test_drain_policy_auto_enabled_for_injected_input():
    """注入脚本化读取 = 模拟终端粘贴，auto 仍应排空（旧行为不回退）。"""
    dispatcher = StdinDispatcher(input_func=_scripted_input([]))
    assert dispatcher.drain_enabled is True


def test_drain_policy_always_enabled_without_tty(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert StdinDispatcher(drain_policy="always").drain_enabled is True


def test_drain_policy_never_keeps_buffered_lines():
    """never：脚本化运行时每一行都是真实输入，不能被排空。"""

    async def scenario():
        dispatcher = StdinDispatcher(
            input_func=_scripted_input(["line-1", "line-2"]),
            drain_policy="never",
        )
        try:
            assert dispatcher.drain_enabled is False
            assert await dispatcher.readline() == "line-1"
            for _ in range(200):
                if dispatcher._queue.qsize() >= 1:
                    break
                await asyncio.sleep(0.005)
            assert dispatcher.drain_pending() == 0
            assert (
                await asyncio.wait_for(dispatcher.readline(), timeout=2)
                == "line-2"
            )
        finally:
            dispatcher.close()

    asyncio.run(scenario())


def test_drain_policy_rejects_unknown_value():
    try:
        StdinDispatcher(drain_policy="sometimes")
    except ValueError as exc:
        assert "drain_policy" in str(exc)
    else:  # pragma: no cover - 必须抛错
        raise AssertionError("非法 drain_policy 应当抛 ValueError")


def test_application_passes_drain_policy_to_dispatcher(tmp_path):
    app = Application(persist_dir=str(tmp_path), drain_policy="never")
    assert app.dispatcher.drain_policy == "never"
    assert app.dispatcher.drain_enabled is False


def test_cli_parses_drain_policy_flag():
    assert parse_args([]).drain_policy == "auto"
    assert parse_args(["--drain-policy", "never"]).drain_policy == "never"
    assert parse_args(["--drain-policy", "always"]).drain_policy == "always"
