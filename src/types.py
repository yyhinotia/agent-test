"""核心数据类型定义（基于 Pydantic v2）。

消息模型、工具 Schema 与 LLM 适配器基类都定义在这里。
原 txt 中该文件顶部 `from src.session import ...` 与 session.py 构成循环导入，
还原时已移除，session 由运行时注入。
"""
import json
from collections.abc import Callable
from enum import Enum
from typing import Any, Dict, List

from pydantic import BaseModel, ConfigDict


class TextBlock(BaseModel):
    """文本内容块。"""

    content: str


class ToolCallBlock(BaseModel):
    """工具调用请求。"""

    id: str
    name: str
    args: str

    @property
    def args_dict(self) -> Dict[str, Any]:
        """解析 args JSON 字符串为参数字典。"""
        return json.loads(self.args)


ContentBlock = TextBlock | ToolCallBlock


class ToolSchema(BaseModel):
    """工具定义。"""

    name: str
    description: str
    parameters: Dict[str, Dict[str, Any]]
    required: List[str] = []

    def to_openai_schema(self) -> Dict[str, Any]:
        """转换为 OpenAI function calling 的 JSON Schema。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.parameters,
                    "additionalProperties": False,
                    "required": self.required,
                },
                # 注意: strict=True 时 OpenAI 要求所有字段都进入 required，
                # 本脚手架允许可选参数，故默认关闭。
                "strict": False,
            },
        }


class ToolCenterSchema(BaseModel):
    """工具中心注册条目。

    字段避免命名为 `schema`（与 pydantic BaseModel 的旧方法重名，
    v2 会产生 shadowing 警告，v3 将移除）。
    """

    tool_schema: ToolSchema
    func: Callable
    usable: bool = True


class UserMessage(BaseModel):
    """用户消息。"""

    id: str
    role: str = "user"
    content: List[TextBlock]


class AssistantMessage(BaseModel):
    """助手（模型输出）消息。"""

    id: str
    role: str = "assistant"
    content: List[ContentBlock]


class ToolResultMessage(BaseModel):
    """工具调用结果消息。"""

    tool_call_id: str
    content: List[TextBlock]
    role: str = "tool"
    is_error: bool = False


Message = UserMessage | AssistantMessage | ToolResultMessage


class SessionEvent(BaseModel):
    """Session 中记录的一条事件。"""

    model_config = ConfigDict(frozen=True)

    seq: int
    type: str
    data: Any
    time: str


class EventType(str, Enum):
    """事件类型枚举。"""

    USER_MSG = "user/message"
    ASSISTANT_MSG = "assistant/message"
    TOOL_CALL = "tool/call"
    TOOL_RESULT = "tool/result"


class AgentPhase(str, Enum):
    """Agent 运行阶段。"""

    IDLE = "idle"
    RUNNING = "running"


class LLMBaseAdapter:
    """LLM 适配器基类: 负责把内部 Message 列表组装为 OpenAI 消息格式。"""

    def __init__(self):
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


class Phase:
    """对话阶段计数器。

    turn / step: 轮次与步数
    stage:       inbox 当前阶段（'turn' / 'step'），由 ReactAgent.turn 管理
    """

    turn: int = 0
    step: int = 0
    stage: str = "turn"
