"""ask_user / confirm 工具与“批准继续”机制测试。"""
import asyncio
import json

from agent_test import ReactAgent
from agent_test.human.service import AskService, ConsoleAskService
from agent_test.policy import CommandPolicy
from agent_test.tools import register_ask
from agent_test.tools.bash import register_bash
from agent_test.tools.center import ToolCenter
from agent_test.types.messages import (
    AssistantMessage,
    TextBlock,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
)


class FakeAskService(AskService):
    """脚本化 AskService：预置回答队列，记录调用。"""

    def __init__(self) -> None:
        self.ask_calls: list[dict] = []
        self.confirm_calls: list[dict] = []
        self.ask_responses: list[str] = []
        self.confirm_responses: list[tuple[bool, str]] = []

    async def ask_user(self, **kwargs) -> str:
        self.ask_calls.append(kwargs)
        return self.ask_responses.pop(0) if self.ask_responses else "默认回答"

    async def confirm(self, **kwargs) -> tuple[bool, str]:
        self.confirm_calls.append(kwargs)
        if self.confirm_responses:
            return self.confirm_responses.pop(0)
        return True, "默认批准"


def test_register_ask_schema():
    center = ToolCenter()
    register_ask(center, FakeAskService())
    names = {s["function"]["name"] for s in center.get_schemas() or []}
    assert {"ask_user", "confirm"} <= names


def test_ask_user_returns_answer():
    svc = FakeAskService()
    svc.ask_responses = ["PostgreSQL"]
    center = ToolCenter()
    register_ask(center, svc)
    result = asyncio.run(
        center.execute(
            "ask_user",
            {"question": "用哪个数据库?", "options": ["MySQL", "PostgreSQL"]},
        )
    )
    assert result["is_error"] is False
    assert result["content"] == "PostgreSQL"
    assert svc.ask_calls[0]["question"] == "用哪个数据库?"
    assert svc.ask_calls[0]["options"] == ["MySQL", "PostgreSQL"]


def test_ask_user_free_text():
    svc = FakeAskService()
    svc.ask_responses = ["我需要支持事务"]
    center = ToolCenter()
    register_ask(center, svc)
    result = asyncio.run(
        center.execute("ask_user", {"question": "有什么补充要求?"})
    )
    assert result["is_error"] is False
    assert result["content"] == "我需要支持事务"


def test_confirm_approved_and_denied():
    svc = FakeAskService()
    svc.confirm_responses = [(True, "用户已批准"), (False, "用户拒绝")]
    center = ToolCenter()
    register_ask(center, svc)
    ok = asyncio.run(
        center.execute("confirm", {"action": "删除临时目录 tmp/x"})
    )
    assert ok["is_error"] is False
    assert ok["content"].startswith("approved")
    bad = asyncio.run(
        center.execute("confirm", {"action": "删除临时目录 tmp/x"})
    )
    assert bad["is_error"] is False
    assert bad["content"].startswith("denied")
    assert "用户拒绝" in bad["content"]


def test_console_ask_service_choice(monkeypatch):
    svc = ConsoleAskService()
    monkeypatch.setattr("builtins.input", lambda _prompt="": "2")
    answer = asyncio.run(
        svc.ask_user(
            question="选一个?", options=["MySQL", "PostgreSQL", "SQLite"]
        )
    )
    assert answer == "PostgreSQL"


def test_console_ask_service_multi_select(monkeypatch):
    svc = ConsoleAskService()
    monkeypatch.setattr("builtins.input", lambda _prompt="": "1,3")
    answer = asyncio.run(
        svc.ask_user(
            question="多选?", options=["A", "B", "C"], multi_select=True
        )
    )
    assert answer == "A, C"


def test_console_ask_service_free_text(monkeypatch):
    svc = ConsoleAskService()
    monkeypatch.setattr("builtins.input", lambda _prompt="": "自定义补充")
    answer = asyncio.run(svc.ask_user(question="补充内容?"))
    assert answer == "自定义补充"


def test_console_confirm(monkeypatch):
    svc = ConsoleAskService()
    monkeypatch.setattr("builtins.input", lambda _prompt="": "n")
    approved, message = asyncio.run(
        svc.confirm(action="执行破坏性操作")
    )
    assert approved is False
    assert "拒绝" in message


# ---------- Agent 端到端：ask_user 的回答作为 tool/result 落 session ----------

class _AskFakeLLM:
    def __init__(self) -> None:
        self.n = 0

    async def stream(self, messages, tools):
        self.n += 1
        if self.n == 1:
            return (
                AssistantMessage(
                    id="a1",
                    content=[
                        ToolCallBlock(
                            id="c1",
                            name="ask_user",
                            args=json.dumps(
                                {
                                    "question": "用哪个数据库?",
                                    "options": ["MySQL", "PostgreSQL"],
                                }
                            ),
                        )
                    ],
                ),
                "",
                None,
            )
        return (
            AssistantMessage(id="a2", content=[TextBlock(content="好，用 PostgreSQL")]),
            "finish",
            None,
        )


def test_agent_ask_user_answer_persisted(tmp_path):
    svc = FakeAskService()
    svc.ask_responses = ["PostgreSQL"]
    center = ToolCenter()
    register_ask(center, svc)
    agent = ReactAgent(
        persist_dir=str(tmp_path), tools=center, llm_client=_AskFakeLLM()
    )
    agent.inbox.append(
        "turn", UserMessage(id="u1", content=[TextBlock(content="选数据库")])
    )
    assert asyncio.run(agent.turn()) is True
    results = [
        e.data for e in agent.session.events if isinstance(e.data, ToolResultMessage)
    ]
    assert len(results) == 1
    assert results[0].is_error is False
    assert results[0].content[0].content == "PostgreSQL"
    # 回答已持久化到 jsonl（可回放）
    raw = agent.session.file_path.read_text(encoding="utf-8")
    assert "PostgreSQL" in raw


# ---------- 批准继续：ToolCenter 闸门 + approver ----------

def test_requires_approval_without_approver(tmp_path):
    center = ToolCenter(policy=CommandPolicy(allowed_roots=[tmp_path]))
    register_bash(center)
    result = asyncio.run(
        center.execute("bash", {"command": "git push origin main"})
    )
    assert result["is_error"] is True
    assert result["policy_action"] == "require_approval"
    assert "需要审批" in result["content"]


def test_approver_approves_then_remembers(tmp_path):
    svc = FakeAskService()
    svc.confirm_responses = [(True, "用户已批准")]
    policy = CommandPolicy(allowed_roots=[tmp_path])
    center = ToolCenter(policy=policy, approver=svc)
    register_bash(center)
    cmd = "attrib +r file.txt"  # 命中 approve 级规则（acl_change），无网络副作用
    first = asyncio.run(
        center.execute("bash", {"command": cmd, "workdir": str(tmp_path)})
    )
    assert first["is_error"] is False
    assert svc.confirm_calls, "approver 应被调用一次"
    assert svc.confirm_calls[0]["action"].startswith("执行命令")
    assert ("attrib", "+r") in policy.allowlist_prefixes
    second = asyncio.run(
        center.execute("bash", {"command": cmd, "workdir": str(tmp_path)})
    )
    assert second["is_error"] is False
    assert len(svc.confirm_calls) == 1, "记住前缀后同类命令不再询问"


def test_approver_denies_returns_reason(tmp_path):
    svc = FakeAskService()
    svc.confirm_responses = [(False, "我不允许")]
    center = ToolCenter(
        policy=CommandPolicy(allowed_roots=[tmp_path]), approver=svc
    )
    register_bash(center)
    result = asyncio.run(
        center.execute("bash", {"command": "git push origin main"})
    )
    assert result["is_error"] is True
    assert result["policy_action"] == "deny_by_user"
    assert "审批被用户拒绝" in result["content"]
    assert "我不允许" in result["content"]
    assert svc.confirm_calls[0]["action"].startswith("执行命令")