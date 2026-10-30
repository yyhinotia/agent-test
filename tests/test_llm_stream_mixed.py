import asyncio

from agent_test.llm.adapter import OPENAIAdapter
from agent_test.types.messages import TextBlock, ToolCallBlock, UserMessage


class _NS:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _Stream:
    def __init__(self, chunks):
        self._chunks = iter(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration

    async def close(self):
        return None


def _chunk(delta, finish_reason=None, usage=None, choices=True):
    choice = _NS(delta=delta, finish_reason=finish_reason)
    return _NS(choices=[choice] if choices else [], usage=usage)


def _adapter(chunks):
    async def create(**kwargs):
        return _Stream(chunks)

    adapter = object.__new__(OPENAIAdapter)
    adapter.model_name = "test"
    adapter.system_prompt = "system"
    adapter.client = _NS(
        chat=_NS(completions=_NS(create=create))
    )
    adapter.STATUS = {
        "stop": "finish",
        "length": "max_token",
        "tool_calls": "",
    }
    adapter.usage_fallback_max_prompt_tokens = 8000
    return adapter


def test_stream_keeps_text_when_tool_call_is_returned():
    """同一 assistant 消息同时含文本与 tool_call 时，文本不得丢失。"""
    tc = _NS(
        index=0,
        id="call-1",
        function=_NS(
            name="read",
            arguments='{"file_path": "a.txt"}',
        ),
    )
    chunks = [
        _chunk(_NS(content="我先读取文件，再给出结论。", tool_calls=None)),
        _chunk(_NS(content=None, tool_calls=[tc]), finish_reason="tool_calls"),
        _chunk(
            _NS(content=None, tool_calls=None),
            choices=False,
            usage=_NS(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        ),
    ]
    adapter = _adapter(chunks)

    message, end_reason, usage = asyncio.run(
        adapter.stream(
            [
                UserMessage(
                    id="u1",
                    content=[TextBlock(content="读文件")],
                )
            ],
            None,
        )
    )

    assert end_reason == ""
    assert usage == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }
    texts = [b for b in message.content if isinstance(b, TextBlock)]
    calls = [b for b in message.content if isinstance(b, ToolCallBlock)]
    assert [t.content for t in texts] == ["我先读取文件，再给出结论。"]
    assert [c.name for c in calls] == ["read"]

    # 回传 LLM 时也必须同时保留文本与 tool_calls
    assembled = adapter.assemble_messages([message])[-1]
    assert assembled["content"] == "我先读取文件，再给出结论。"
    assert assembled["tool_calls"][0]["function"]["name"] == "read"
