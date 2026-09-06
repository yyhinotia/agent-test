"""LLM 适配器：基类 + OpenAI 流式实现。

重构后不再“吞异常”：OpenAI 流式调用失败会抛出 LlmError（保留原始
异常链），完整堆栈由 ReactAgent 统一写入日志文件，调用位置在 session
中记录 runtime/error 事件。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Tuple

from dotenv import load_dotenv
from openai import AsyncOpenAI

from agent_test.exceptions.llm import LlmError
from agent_test.types.messages import (
    AssistantMessage,
    Message,
    TextBlock,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
)
from agent_test.utils import get_uuid

load_dotenv()


class LLMBaseAdapter:
    """LLM 适配器基类：负责把内部 Message 列表组装为 OpenAI 消息格式。"""

    def __init__(self) -> None:
        self.client = None
        self.model_name = None
        self.system_prompt = "你是一个有用的AI助手"
        self.session = None

    def assemble_messages(self, messages: List[Message]) -> List[Dict[str, Any]]:
        """将内部 Message 列表转换为 OpenAI Chat API 的消息列表。"""
        openai_messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt}
        ]
        for message in messages:
            if isinstance(message, UserMessage):
                content = "\n".join(block.content for block in message.content)
                openai_messages.append({"role": message.role, "content": content})

            elif isinstance(message, AssistantMessage):
                assistant_message: Dict[str, Any] = {
                    "role": message.role,
                    "content": [],
                }
                tool_calls = []
                for block in message.content:
                    if isinstance(block, TextBlock):
                        assistant_message["content"].append(block.content)
                    elif isinstance(block, ToolCallBlock):
                        tool_calls.append(
                            {
                                "id": block.id,
                                "type": "function",
                                "function": {
                                    "name": block.name,
                                    "arguments": block.args,
                                },
                            }
                        )
                assistant_message["content"] = (
                    "\n".join(assistant_message["content"])
                    if assistant_message["content"]
                    else None
                )
                if tool_calls:
                    assistant_message["tool_calls"] = tool_calls
                openai_messages.append(assistant_message)

            elif isinstance(message, ToolResultMessage):
                text = "\n".join(block.content for block in message.content)
                openai_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": message.tool_call_id,
                        "content": text,
                    }
                )

        return openai_messages


class OPENAIAdapter(LLMBaseAdapter):
    """OpenAI Chat Completions 流式适配器。"""

    def __init__(self) -> None:
        super().__init__()
        self.client = AsyncOpenAI(
            api_key=os.environ["API_KEY"],
            base_url=os.environ["BASE_URI"],
        )
        self.model_name = os.environ["MODEL_NAME"]
        self.STATUS = {"stop": "finish", "length": "max_token", "tool_calls": ""}

    async def stream(
        self, messages: List[Message], tools: List[Dict] | None
    ) -> Tuple[AssistantMessage, str]:
        """流式调用 LLM，返回（助手消息, 结束原因）。

        end_reason:
            'finish'     模型正常结束（给出最终回答）
            'max_token'  达到最大 token 上限
            ''           模型请求调用工具（Agent 应继续下一步）

        重构后不再吞异常：调用失败抛出 LlmError（含原始异常链），完整
        堆栈由 ReactAgent 统一写入日志文件，调用位置在 session 记录
        runtime/error 事件。
        """
        assistant_message = AssistantMessage(id=get_uuid(), content=[])
        end_reason = ""
        stream = None
        try:
            openai_message = self.assemble_messages(messages)
            stream = await self.client.chat.completions.create(
                model=self.model_name,
                messages=openai_message,
                stream=True,
                tools=tools or None,
            )

            # OpenAI 流式返回工具调用时按 index 分片，需要累积拼接
            tool_call_fragments: Dict[int, Dict[str, str]] = {}
            async for chunk in stream:
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta

                if choice.finish_reason:
                    end_reason = self.STATUS.get(choice.finish_reason, "")

                if delta.content:
                    assistant_message.content.append(TextBlock(content=delta.content))

                for tc in delta.tool_calls or []:
                    idx = (
                        tc.index
                        if tc.index is not None
                        else len(tool_call_fragments)
                    )
                    frag = tool_call_fragments.setdefault(
                        idx, {"id": "", "name": "", "arguments": ""}
                    )
                    if tc.id:
                        frag["id"] = tc.id
                    if tc.function and tc.function.name:
                        frag["name"] = tc.function.name
                    if tc.function and tc.function.arguments:
                        frag["arguments"] += tc.function.arguments

            if tool_call_fragments:
                tool_calls = [
                    ToolCallBlock(
                        id=frag["id"],
                        name=frag["name"],
                        args=frag["arguments"],
                    )
                    for frag in tool_call_fragments.values()
                ]
                assistant_message.content = tool_calls

        except Exception as exc:  # noqa: BLE001
            raise LlmError(
                f"OpenAI 流式调用失败: {exc}",
                location="OPENAIAdapter.stream",
                detail={"model": self.model_name},
                retryable=True,
            ) from exc
        finally:
            if stream is not None:
                try:
                    await stream.close()
                except Exception:  # noqa: BLE001
                    pass
        return assistant_message, end_reason