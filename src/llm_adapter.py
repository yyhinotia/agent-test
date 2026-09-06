"""OpenAI LLM 适配器。

通过 OpenAI Chat Completions 流式调用模型，解析增量返回的 content 与
tool_calls（OpenAI 流式工具调用按 index 分片，需要累积拼接）。
原 txt 中错误的 `completions.create` 端点已修正为 `chat.completions.create`。
"""
import os
from typing import Dict, List, Tuple

from dotenv import load_dotenv
from openai import AsyncOpenAI

from src.types import (
    AssistantMessage,
    LLMBaseAdapter,
    Message,
    TextBlock,
    ToolCallBlock,
)
from src.utils import get_uuid

load_dotenv()


class OPENAIAdapter(LLMBaseAdapter):
    def __init__(self):
        super().__init__()
        self.client = AsyncOpenAI(
            api_key=os.environ["API_KEY"],
            base_url=os.environ["BASE_URI"],
        )
        self.model_name = os.environ["MODEL_NAME"]
        self.STATUS = {"stop": "finish", "length": "max_token", "tool_calls": ""}

    async def stream(
        self, messages: List[Message], tools: List[Dict]
    ) -> Tuple[AssistantMessage, str]:
        """流式调用 LLM，返回（助手消息, 结束原因）。

        end_reason:
            'finish'     模型正常结束（给出最终回答）
            'max_token'  达到最大 token 上限
            'error'      调用出错
            ''           模型请求调用工具（Agent 应继续下一步）
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
                    idx = tc.index if tc.index is not None else len(tool_call_fragments)
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

        except Exception as e:  # noqa: BLE001
            end_reason = "error"
        finally:
            if stream is not None:
                try:
                    await stream.close()
                except Exception:  # noqa: BLE001
                    pass
            return assistant_message, end_reason


class _LLMRegistry(dict):
    """LLM 客户端注册表：按需构建，避免 import 时因缺少环境变量而失败。"""

    def __missing__(self, key: str):
        if key != "openai":
            raise KeyError(f"未知 LLM 客户端: {key}")
        client = OPENAIAdapter()
        self[key] = client
        return client


LLM_CLIENT: Dict[str, OPENAIAdapter] = _LLMRegistry()
