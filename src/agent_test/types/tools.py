"""工具 Schema 数据模型。"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Dict, List

from pydantic import BaseModel


class ToolSchema(BaseModel):
    """工具定义（名称、描述、参数 JSON Schema）。"""

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