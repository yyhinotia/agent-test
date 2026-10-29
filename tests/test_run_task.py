"""run_task 结构化结果解析：正文伪花括号不得挡住真正的 StepResult JSON。"""
import asyncio

from agent_test import Scheduler, create_agent
from agent_test.core.agent import ReactAgent
from agent_test.tools.center import ToolCenter
from agent_test.types.messages import AssistantMessage, TextBlock


def test_parse_structured_result_skips_prose_braces():
    text = (
        '核验完成：`git cat-file -e "<hash>^{commit}"` 与 `{path}` 均正常。\n\n'
        '{"status":"success","output":{"ok":true},'
        '"evidence":["hash HIT"],"self_check":true}'
    )

    parsed = ReactAgent._parse_structured_result(text)

    assert parsed is not None
    assert parsed["status"] == "success"
    assert parsed["output"] == {"ok": True}
    assert parsed["evidence"] == ["hash HIT"]


def test_parse_structured_result_skips_invalid_brace_candidates():
    text = (
        "先输出 {commit}，再输出 {path}，最后是结果：\n"
        '{"status":"partial","output":{"step":1},'
        '"evidence":[],"self_check":false,"failure_reason":"未完成"}'
    )

    parsed = ReactAgent._parse_structured_result(text)

    assert parsed is not None
    assert parsed["status"] == "partial"
    assert parsed["failure_reason"] == "未完成"


def test_parse_structured_result_keeps_json_after_markdown_code_block():
    """正文先出现普通代码块时，不能把代码块内容当成整段文本而丢掉 JSON。"""
    text = (
        "README 前几行如下：\n"
        "```text\n"
        "# agent-test\n"
        "模块化 ReAct Agent\n"
        "```\n\n"
        '{"status":"success","output":{"lines":3},'
        '"evidence":["read"],"self_check":true}'
    )

    parsed = ReactAgent._parse_structured_result(text)

    assert parsed is not None
    assert parsed["status"] == "success"
    assert parsed["output"] == {"lines": 3}


class _ProseBraceLLM:
    def __init__(self, text: str):
        self.session = None
        self.text = text
        self.calls = 0
        self.prompts: list[str] = []

    async def stream(self, messages, tools):
        self.calls += 1
        # 记录最后一条 user 消息（即本轮任务提示词）
        user_texts = []
        for message in messages:
            if getattr(message, "role", "") == "user":
                blocks = getattr(message, "content", None) or []
                user_texts.append(
                    "".join(
                        b.content for b in blocks if isinstance(b, TextBlock)
                    )
                )
        if user_texts:
            self.prompts.append(user_texts[-1])
        return (
            AssistantMessage(id="a", content=[TextBlock(content=self.text)]),
            "finish",
            None,
        )


def test_run_task_parses_json_after_prose_braces():
    text = (
        '命令 `git cat-file -e "<hash>^{commit}"` 成功，路径 `{path}` 也已核验。\n'
        '{"status":"success","output":{"artifact":"docs/x.md"},'
        '"evidence":["exit 0"],"self_check":true}'
    )
    llm = _ProseBraceLLM(text)
    agent = create_agent(
        persist=False,
        llm_client=llm,
        tools=ToolCenter(),
        max_turns=1,
    )
    try:
        result = asyncio.run(
            agent.run_task(goal="核验报告", acceptance_criteria="给出核验证据")
        )
    finally:
        agent.destroy()

    assert result["status"] == "success"
    assert result["output"] == {"artifact": "docs/x.md"}
    assert result["failure_type"] is None
    assert llm.calls == 1


def test_scheduler_parse_plan_json_skips_prose_braces():
    text = (
        '先检查 `^{commit}` 与 `{path}`，再输出计划：\n'
        '{"graph_id":"g","global_goal":"goal",'
        '"nodes":[{"node_id":"a","goal":"A"}]}'
    )

    plan = Scheduler._parse_plan_json(text)

    assert plan is not None
    assert plan["graph_id"] == "g"
    assert plan["nodes"][0]["node_id"] == "a"


def test_scheduler_parse_plan_json_keeps_json_after_code_block():
    text = (
        "参考格式：\n```json\n示例不是计划，只是片段\n```\n\n"
        '{"graph_id":"g2","global_goal":"goal",'
        '"nodes":[{"node_id":"b","goal":"B"}]}'
    )

    plan = Scheduler._parse_plan_json(text)

    assert plan is not None
    assert plan["graph_id"] == "g2"
    assert plan["nodes"][0]["node_id"] == "b"


class _SequenceLLM:
    """按顺序返回多条 finish 文本，用于验证 retry / wrap-up 行为。"""

    def __init__(self, texts: list[str]):
        self.session = None
        self.texts = texts
        self.calls = 0
        self.prompts: list[str] = []

    async def stream(self, messages, tools):
        self.calls += 1
        user_texts = []
        for message in messages:
            if getattr(message, "role", "") == "user":
                blocks = getattr(message, "content", None) or []
                user_texts.append(
                    "".join(
                        b.content for b in blocks if isinstance(b, TextBlock)
                    )
                )
        if user_texts:
            self.prompts.append(user_texts[-1])
        text = self.texts[min(self.calls - 1, len(self.texts) - 1)]
        return (
            AssistantMessage(id=f"a{self.calls}", content=[TextBlock(content=text)]),
            "finish",
            None,
        )


def test_run_task_retries_finish_without_step_result():
    """finish 但无 StepResult 时不能直接收尾，应继续下一轮追要 JSON。"""
    llm = _SequenceLLM(
        [
            "任务做完了，这里是自然语言总结，没有 JSON。",
            '{"status":"success","output":{"ok":true},'
            '"evidence":["retry"],"self_check":true}',
        ]
    )
    agent = create_agent(
        persist=False,
        llm_client=llm,
        tools=ToolCenter(),
        max_turns=3,
    )
    try:
        result = asyncio.run(agent.run_task(goal="测试 retry"))
    finally:
        agent.destroy()

    assert result["status"] == "success"
    assert result["output"] == {"ok": True}
    assert llm.calls == 2
    # 第二轮提示词应明确要求只输出 StepResult，而不是继续重复工作
    assert "没有给出合法的 StepResult" in llm.prompts[1]
    assert "StepResult JSON" in llm.prompts[1]


def test_run_task_final_round_appends_wrap_up_prompt():
    """始终不输出 JSON 时，最后一轮应追加节点收尾/自检提示词。"""
    llm = _SequenceLLM(["没有 JSON 的第一轮", "仍然没有 JSON 的第二轮"])
    agent = create_agent(
        persist=False,
        llm_client=llm,
        tools=ToolCenter(),
        max_turns=2,
    )
    try:
        result = asyncio.run(agent.run_task(goal="测试收尾提示词"))
    finally:
        agent.destroy()

    assert result["status"] == "partial"
    assert result["failure_type"] == "acceptance_failed"
    assert llm.calls == 2
    assert "节点收尾与自检（必须执行）" in llm.prompts[1]
    assert "status" in llm.prompts[1]


def test_find_structured_result_scans_current_turn_backwards():
    """JSON 在较早 assistant 消息、最后一条是 prose 时，仍应找到 StepResult。"""
    llm = _SequenceLLM(["unused"])
    agent = create_agent(
        persist=False,
        llm_client=llm,
        tools=ToolCenter(),
        max_turns=1,
    )
    try:
        from agent_test.types.events import EventType
        from agent_test.types.messages import UserMessage

        agent.session.append(
            EventType.USER_MESSAGE,
            data=UserMessage(id="u1", content=[TextBlock(content="任务")]),
        )
        agent.session.append(
            EventType.ASSISTANT_MESSAGE,
            data=AssistantMessage(
                id="a1",
                content=[
                    TextBlock(
                        content=(
                            '{"status":"success","output":{"x":1},'
                            '"evidence":["e"],"self_check":true}'
                        )
                    )
                ],
            ),
        )
        # 最后一条 assistant 是自然语言总结，不应掩盖前面的合法 StepResult
        agent.session.append(
            EventType.ASSISTANT_MESSAGE,
            data=AssistantMessage(
                id="a2",
                content=[TextBlock(content="以上是总结，没有 JSON。")],
            ),
        )

        parsed = agent._find_structured_result()
    finally:
        agent.destroy()

    assert parsed is not None
    assert parsed["status"] == "success"
    assert parsed["output"] == {"x": 1}
