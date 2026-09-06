"""agent-test：基于 OpenAI + Pydantic 的模块化 ReAct Agent。

组织结构（按功能模块）：
    exceptions/  异常体系（AgentBaseError 基类与分类异常）
    log/         运行时日志（logs/runtime.log + 控制台，带 session/turn/step 上下文）
    types/       消息 / 事件类型 / 工具 Schema 数据模型
    core/        ReAct Agent 循环、阶段控制与消息队列
    session/     会话事件记录与 JSONL 持久化（含错误事件）
    llm/         LLM 适配器与客户端注册表
    tools/       工具中心与内置工具（bash 命令执行 / ask_user 用户交互）
    policy/      命令治理策略（权限审批/危险/工作目录检测，pre_step 拦截）
    human/       用户交互服务（AskService：澄清/补充消息/批准继续）
"""
from agent_test.core.agent import ReactAgent
from agent_test.core.inbox import InBox
from agent_test.policy import CommandPolicy, PolicyAction, PolicyDecision
from agent_test.tools.bash import bash, register_bash
from agent_test.exceptions import (
    AgentBaseError,
    LlmError,
    MessageEditError,
    SessionContinuityError,
    SessionEditError,
    ToolExecutionError,
)
from agent_test.human.service import AskService, ConsoleAskService
from agent_test.log.runtime_log import RuntimeLog
from agent_test.session.session import Session
from agent_test.types.events import AgentPhase, EventType, Phase
from agent_test.types.messages import (
    AssistantMessage,
    Message,
    TextBlock,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
)
from agent_test.types.tools import ToolCenterSchema, ToolSchema

__version__ = "0.4.0"

__all__ = [
    "AgentBaseError",
    "AskService",
    "ConsoleAskService",
    "CommandPolicy",
    "PolicyAction",
    "PolicyDecision",
    "bash",
    "register_bash",
    "AgentPhase",
    "AssistantMessage",
    "EventType",
    "InBox",
    "LlmError",
    "Message",
    "MessageEditError",
    "Phase",
    "ReactAgent",
    "RuntimeLog",
    "Session",
    "SessionContinuityError",
    "SessionEditError",
    "TextBlock",
    "ToolCallBlock",
    "ToolCenterSchema",
    "ToolExecutionError",
    "ToolResultMessage",
    "ToolSchema",
    "UserMessage",
]