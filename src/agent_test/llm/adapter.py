"""LLM 适配器：基类 + OpenAI 流式实现。

重构后不再“吞异常”：OpenAI 流式调用失败会抛出 LlmError（保留原始
异常链），完整堆栈由 ReactAgent 统一写入日志文件，调用位置在 session
中记录 runtime/error 事件。

知识增量：
- stream() 返回三元组 (AssistantMessage, end_reason, usage)；
- 调用时携带 stream_options={"include_usage": True} 请求用量，
  服务端缺省时用 _fetch_usage_fallback 做字符粗估兜底；
- text delta 累积为单个 TextBlock（不再每个 delta 独立一条）。
"""
from __future__ import annotations

import json
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
    ) -> Tuple[AssistantMessage, str, Dict[str, Any] | None]:
        """流式调用 LLM，返回（助手消息, 结束原因, usage）。

        end_reason:
            'finish'     模型正常结束（给出最终回答）
            'max_token'  达到最大 token 上限
            ''           模型请求调用工具（Agent 应继续下一步）

        usage（知识增量）:
            {"prompt_tokens": int, "completion_tokens": int,
             "total_tokens": int} 或 None；服务端未返回时用
            _fetch_usage_fallback 粗估兜底。

        重构后不再吞异常：调用失败抛出 LlmError（含原始异常链），完整
        堆栈由 ReactAgent 统一写入日志文件，调用位置在 session 记录
        runtime/error 事件。
        """
        assistant_message = AssistantMessage(id=get_uuid(), content=[])
        end_reason = ""
        usage: Dict[str, Any] | None = None
        stream = None
        try:
            openai_message = self.assemble_messages(messages)
            stream = await self.client.chat.completions.create(
                model=self.model_name,
                messages=openai_message,
                stream=True,
                tools=tools or None,
                stream_options={"include_usage": True},
            )

            # OpenAI 流式返回工具调用时按 index 分片，需要累积拼接；
            # text delta 累积为单个 TextBlock（知识增量）
            tool_call_fragments: Dict[int, Dict[str, str]] = {}
            content_parts: List[str] = []
            async for chunk in stream:
                # 携带 include_usage 时末块会带 usage（choices 为空）
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    usage = {
                        "prompt_tokens": getattr(chunk_usage, "prompt_tokens", 0)
                        or 0,
                        "completion_tokens": getattr(
                            chunk_usage, "completion_tokens", 0
                        )
                        or 0,
                        "total_tokens": getattr(chunk_usage, "total_tokens", 0) or 0,
                    }
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta

                if choice.finish_reason:
                    end_reason = self.STATUS.get(choice.finish_reason, "")

                if delta.content:
                    content_parts.append(delta.content)

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
            elif content_parts:
                # 知识增量：text delta 累积为单个 TextBlock
                assistant_message.content = [TextBlock(content="".join(content_parts))]

            if usage is None:
                usage = self._fetch_usage_fallback(openai_message, tools)

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
        return assistant_message, end_reason, usage

    def _fetch_usage_fallback(
        self, openai_message: List[Dict[str, Any]], tools: List[Dict] | None
    ) -> Dict[str, Any]:
        """服务端未返回 usage 时的估算兜底：按字符数粗估 token。

        以 prompt（对话历史 + 工具 schema）为主估算 total_tokens，
        completion 按 prompt 的 1/4 粗估（流式输出典型比例）。真实计数
        以服务端 usage 为准，此回退仅保证 TokenMeter 阈值判断可用。
        """
        prompt_text = " ".join(
            str(message.get("content", "")) for message in openai_message
        )
        tools_text = json.dumps(tools or [], ensure_ascii=False)
        prompt_tokens = max(1, (len(prompt_text) + len(tools_text)) // 3)
        completion_tokens = max(1, prompt_tokens // 4)
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }