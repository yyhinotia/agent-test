"""CLI 应用包：交互式入口（主循环 + 统一 stdin + 审批服务）。"""
from agent_test.app.application import Application, main
from agent_test.app.cli_ask_service import CLIAskService

__all__ = ["Application", "CLIAskService", "main"]
