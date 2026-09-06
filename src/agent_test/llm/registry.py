"""LLM 客户端注册表：按需构建，避免 import 时因缺少环境变量而失败。"""
from __future__ import annotations

from typing import Dict

from agent_test.llm.adapter import OPENAIAdapter


class _LLMRegistry(dict):
    """LLM 客户端注册表。

    LLM 客户端无状态可复用，进程内共享单例。`LLM_CLIENT.clear()` 可在
    测试中重置，避免跨用例复用已绑定的环境。
    """

    def __missing__(self, key: str):
        if key != "openai":
            raise KeyError(f"未知 LLM 客户端: {key}")
        client = OPENAIAdapter()
        self[key] = client
        return client


LLM_CLIENT: Dict[str, OPENAIAdapter] = _LLMRegistry()