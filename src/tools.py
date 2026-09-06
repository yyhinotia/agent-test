"""工具中心: 注册、管理并执行工具。"""
import inspect
import json
import os.path
from typing import Any, Dict, List

from src.types import ToolCenterSchema, ToolSchema


class ToolCenter:
    def __init__(self):
        self.tools: Dict[str, ToolCenterSchema] = {}

    def register(self, desc: str, parameters: Dict, required: List[str] = None):
        """工具注册装饰器。"""

        def wrap(func: Any):
            func_name = func.__name__
            if func_name in self.tools:
                raise Exception(f"工具{func_name}重复注册")
            center_schema = ToolCenterSchema(
                tool_schema=ToolSchema(
                    name=func_name,
                    description=desc,
                    parameters=parameters,
                    required=required or [],
                ),
                func=func,
                usable=True,
            )
            self.tools[func_name] = center_schema

            def wrap_in(*args, **kwargs):
                return func(*args, **kwargs)

            return wrap_in

        return wrap

    def unregister(self, tool_name: str) -> None:
        """工具取消注册（仅标记不可用）。"""
        if tool_name not in self.tools:
            return
        self.tools[tool_name].usable = False

    def get_schemas(self) -> List[Dict[str, Any]] | None:
        """获取全部可用工具的 OpenAI function calling schema。"""
        schemas = []
        for item in self.tools.values():
            if not item.usable:
                continue
            schemas.append(item.tool_schema.to_openai_schema())
        return schemas if schemas else None

    async def execute(self, func_name: str, func_args: Dict) -> Dict[str, Any]:
        """执行指定工具，返回 {"content": str, "is_error": bool}。"""
        return_data: Dict[str, Any] = {"content": "", "is_error": False}
        try:
            if func_name not in self.tools:
                raise Exception("工具未注册")
            if not self.tools[func_name].usable:
                raise Exception(f"工具{func_name}不可执行")
            func = self.tools[func_name].func
            if inspect.iscoroutinefunction(func):
                tool_return = await func(**func_args)
            else:
                tool_return = func(**func_args)
            if isinstance(tool_return, str):
                return_data["content"] = tool_return
            else:
                return_data["content"] = json.dumps(tool_return, ensure_ascii=False)
        except Exception as e:
            return_data = {"content": f"工具执行失败: {e}", "is_error": True}
        finally:
            return return_data


tool_center = ToolCenter()


@tool_center.register(
    desc="文件阅读工具",
    parameters={
        "file_path": {"type": "string", "description": "文件路径"},
        "offset": {"type": "integer", "description": "文件阅读偏移行号"},
        "limit": {"type": "integer", "description": "文件阅读最大行数"},
        "encoding": {"type": "string", "description": "文件编码格式"},
    },
    required=["file_path"],
)
def read(file_path: str, offset: int = 0, limit: int = 500, encoding: str = "utf-8"):
    """按行读取文件。

    offset: 起始行号（从 0 开始）
    limit:  最大读取行数
    """
    if not os.path.exists(file_path):
        return {
            "content": "",
            "start_idx": offset,
            "end_idx": offset,
            "has_more": False,
            "error": f"文件不存在: {file_path}",
        }

    content = []

    try:
        with open(file_path, "r", encoding=encoding) as f:
            for idx, line in enumerate(f):
                if idx < offset:
                    continue
                if len(content) >= limit:
                    break
                content.append(line.rstrip("\n"))

        end_idx = offset + len(content) - 1

        return {
            "content": "\n".join(content),
            "start_idx": offset,
            "end_idx": end_idx,
            "has_more": len(content) == limit,
        }
    except UnicodeDecodeError:
        return {
            "content": "",
            "start_idx": offset,
            "end_idx": offset,
            "has_more": False,
            "error": "文件编码错误",
        }
    except Exception as e:
        return {
            "content": "",
            "start_idx": offset,
            "end_idx": offset,
            "has_more": False,
            "error": str(e),
        }
