"""agent-test：基于 OpenAI + Pydantic 的模块化 ReAct Agent。

组织结构（按功能模块）：
    exceptions/  异常体系（AgentBaseError 基类与分类异常）
    log/         运行时日志（logs/runtime.log + 控制台，带 session/turn/step 上下文）
    types/       消息 / 事件类型 / 工具 Schema 数据模型
    core/        ReAct Agent 循环、阶段控制与消息队列
    session/     会话事件记录与 JSONL 持久化（含错误事件、压缩打标与摘要插入）
    llm/         LLM 适配器、客户端注册表、TokenMeter 用量计量与 Compactor 压缩
    tools/       工具中心与内置工具（bash 命令执行 / ask_user 用户交互）
    policy/      命令治理策略（权限审批/危险/工作目录检测，pre_step 拦截）
    human/       用户交互服务（AskService + StdinDispatcher 统一 stdin）
    app/         CLI 应用入口（交互主循环 / 审批服务 / 多行输入）
"""
from agent_test.core.agent import ReactAgent, create_agent
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
from agent_test.human.stdin_dispatcher import StdinDispatcher
from agent_test.llm.compactor import Compactor
from agent_test.llm.token_meter import TokenMeter
from agent_test.log.runtime_log import RuntimeLog
from agent_test.session.session import Session
from agent_test.types.events import AgentPhase, EventType, Phase
from agent_test.types.messages import (
    ApprovalResult,
    AssistantMessage,
    ExecutionResult,
    Message,
    TextBlock,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
)
from agent_test.types.tools import ToolCenterSchema, ToolSchema
from agent_test.app.application import Application, main
from agent_test.app.cli_ask_service import CLIAskService

__version__ = "0.5.1"

__all__ = [
    "AgentBaseError",
    "Application",
    "ApprovalResult",
    "AskService",
    "CLIAskService",
    "ConsoleAskService",
    "ExecutionResult",
    "StdinDispatcher",
    "main",
    "CommandPolicy",
    "PolicyAction",
    "PolicyDecision",
    "bash",
    "register_bash",
    "AgentPhase",
    "AssistantMessage",
    "Compactor",
    "create_agent",
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
    "TokenMeter",
    "ToolCallBlock",
    "ToolCenterSchema",
    "ToolExecutionError",
    "ToolResultMessage",
    "ToolSchema",
    "UserMessage",
]