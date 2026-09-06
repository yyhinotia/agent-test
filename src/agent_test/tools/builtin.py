"""内置工具：文件读取/查找/编辑，以及进程内共享的 tool_center 单例。"""
from __future__ import annotations

import os
import re
from typing import Any, Dict

from agent_test.tools.center import ToolCenter

# 遍历目录时跳过的噪音目录（隐藏目录 + 常见缓存/依赖目录）
_IGNORED_DIRS = {
    ".git",
    ".idea",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
}

_READ_PARAMETERS: Dict[str, Any] = {
    "file_path": {"type": "string", "description": "文件路径"},
    "offset": {"type": "integer", "description": "文件阅读偏移行号"},
    "limit": {"type": "integer", "description": "文件阅读最大行数"},
    "encoding": {"type": "string", "description": "文件编码格式"},
}


def read(
    file_path: str, offset: int = 0, limit: int = 500, encoding: str = "utf-8"
):
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
    except Exception as e:  # noqa: BLE001
        return {
            "content": "",
            "start_idx": offset,
            "end_idx": offset,
            "has_more": False,
            "error": str(e),
        }


_FIND_PARAMETERS: Dict[str, Any] = {
    "path": {"type": "string", "description": "要搜索的根目录"},
    "name_keyword": {"type": "string", "description": "文件名包含的关键字（大小写不敏感）"},
    "content_pattern": {"type": "string", "description": "文件内容正则，返回第一个命中行（含行号与文本）"},
    "case_sensitive": {"type": "boolean", "description": "关键字/正则是否大小写敏感，默认 False"},
    "limit": {"type": "integer", "description": "最多返回的匹配文件数，默认 50"},
    "encoding": {"type": "string", "description": "文件编码格式"},
}


def find(
    path: str,
    name_keyword: str = "",
    content_pattern: str = "",
    case_sensitive: bool = False,
    limit: int = 50,
    encoding: str = "utf-8",
):
    """在目录树中查找文件。

    两种定位方式（可组合）：
    - name_keyword:     文件名包含关键字；
    - content_pattern:  文件内容正则，命中后给出第一个匹配行。
    未指定任何筛选条件时等价于列出目录下全部文件。
    """
    if not os.path.isdir(path):
        return {
            "matches": [],
            "scanned": 0,
            "truncated": False,
            "error": f"目录不存在: {path}",
        }

    errors = []
    scanned = 0
    matches = []
    stop = False

    flags = 0 if case_sensitive else re.IGNORECASE

    compiled = None
    if content_pattern:
        try:
            compiled = re.compile(content_pattern, flags)
        except re.error as e:
            return {
                "matches": [],
                "scanned": 0,
                "truncated": False,
                "error": f"无效正则: {e}",
            }

    keyword = name_keyword if case_sensitive else name_keyword.lower()

    for root, dirs, files in os.walk(path):
        # 原地过滤跳过噪音目录，避免无效递归
        dirs[:] = [d for d in dirs if d not in _IGNORED_DIRS]

        for file_name in files:
            if stop:
                break
            # 文件名关键字过滤
            if keyword:
                probe = file_name if case_sensitive else file_name.lower()
                if keyword not in probe:
                    continue

            full_path = os.path.join(root, file_name)
            scanned += 1

            # 内容正则过滤：记录第一个命中行
            if compiled is not None:
                try:
                    with open(full_path, "r", encoding=encoding) as f:
                        for line_no, line in enumerate(f, 1):
                            if compiled.search(line):
                                matches.append(
                                    {
                                        "path": full_path,
                                        "line": line_no,
                                        "text": line.rstrip("\n"),
                                    }
                                )
                                break
                except UnicodeDecodeError:
                    errors.append(f"无法按 {encoding} 解码: {full_path}")
                except OSError as e:
                    errors.append(f"读取失败: {full_path}: {e}")
            else:
                matches.append({"path": full_path, "line": None, "text": None})

            if len(matches) >= limit:
                stop = True

    return {
        "matches": matches,
        "scanned": scanned,
        "truncated": len(matches) >= limit,
        "error": "; ".join(errors) if errors else "",
    }


_EDIT_PARAMETERS: Dict[str, Any] = {
    "file_path": {"type": "string", "description": "要编辑的文件路径"},
    "old_text": {"type": "string", "description": "被替换的原文，必须与文件内容完全一致"},
    "new_text": {"type": "string", "description": "替换后的新文本"},
    "replace_all": {"type": "boolean", "description": "若 old_text 出现多次，是否全部替换，默认 False（单次替换）"},
    "encoding": {"type": "string", "description": "文件编码格式"},
}


def edit(
    file_path: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
    encoding: str = "utf-8",
):
    """对文件做精确文本替换（读-改-写）。

    规则：
    - old_text 必须与文件内容逐字符一致；
    - old_text 出现 0 次 -> error；出现多次且 replace_all=False -> error；
    - 单次（或 replace_all=True）时执行替换，返回替换次数。
    """
    if not os.path.exists(file_path):
        return {"error": f"文件不存在: {file_path}", "replaced": 0}
    if not old_text:
        return {"error": "old_text 不能为空", "replaced": 0}

    try:
        with open(file_path, "r", encoding=encoding) as f:
            content = f.read()
    except UnicodeDecodeError:
        return {"error": f"无法按 {encoding} 解码文件: {file_path}", "replaced": 0}
    except OSError as e:
        return {"error": f"读取失败: {e}", "replaced": 0}

    count = content.count(old_text)
    if count == 0:
        return {"error": f"未找到匹配文本: {old_text!r}", "replaced": 0}
    if count > 1 and not replace_all:
        return {
            "error": f"匹配到 {count} 处，请用更大上下文，或设置 replace_all=True",
            "replaced": 0,
        }

    new_content = content.replace(old_text, new_text) if replace_all else content.replace(old_text, new_text, 1)
    try:
        with open(file_path, "w", encoding=encoding) as f:
            f.write(new_content)
    except OSError as e:
        return {"error": f"写入失败: {e}", "replaced": 0}

    return {"replaced": count if replace_all else 1}


# 单条命中行文本的最大长度（超出截断，避免超长行撑爆上下文）
_GREP_MAX_TEXT_LEN = 200

_GREP_PARAMETERS: Dict[str, Any] = {
    "path": {"type": "string", "description": "要搜索的文件路径，或目录路径（目录会递归）"},
    "pattern": {"type": "string", "description": "正则表达式"},
    "offset": {"type": "integer", "description": "跳过前 offset 个命中行（跨文件累计），默认 0"},
    "limit": {"type": "integer", "description": "本次最多返回的命中行数，默认 50"},
    "case_sensitive": {"type": "boolean", "description": "是否大小写敏感，默认 False"},
    "encoding": {"type": "string", "description": "文件编码格式"},
}


def grep(
    path: str,
    pattern: str,
    offset: int = 0,
    limit: int = 50,
    case_sensitive: bool = False,
    encoding: str = "utf-8",
):
    """按正则搜索文件内容，支持 offset/limit 跨文件分页。

    约定：
    - 结果单位为「命中行」：一行只记一条，匹配内容为 {path, line, text}；
    - offset/limit 作用于累计命中行窗口 [offset, offset+limit)；
    - text 为整行文本，超过 _GREP_MAX_TEXT_LEN 字符时截断并追加 ...；
    - truncated=True 表示达到窗口上界即停止（可能还有更多），
      请用 offset += count 继续翻页，直至返回空 matches；
    - 目录遍历按字典序排序，保证同参数下翻页结果稳定；
    - 无法解码/读取的文件会跳过并聚合进 error，不影响整体搜索。
    """
    if offset < 0 or limit <= 0:
        return {
            "matches": [],
            "count": 0,
            "truncated": False,
            "error": "offset 必须 >= 0，limit 必须 > 0",
        }
    if not pattern:
        return {"matches": [], "count": 0, "truncated": False, "error": "pattern 不能为空"}

    if os.path.isfile(path):
        file_paths = [path]
    elif os.path.isdir(path):
        # 排序收集文件：dirs/files 均按字典序，保证分页遍历稳定
        file_paths = []
        for root, dirs, files in os.walk(path):
            dirs[:] = sorted(d for d in dirs if d not in _IGNORED_DIRS)
            for name in sorted(files):
                file_paths.append(os.path.join(root, name))
    else:
        return {
            "matches": [],
            "count": 0,
            "truncated": False,
            "error": f"路径不存在: {path}",
        }

    try:
        compiled = re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
    except re.error as e:
        return {"matches": [], "count": 0, "truncated": False, "error": f"无效正则: {e}"}

    matches = []
    errors = []
    seen = 0  # 已扫描到的累计命中数（含被 offset 跳过的）
    window_end = offset + limit

    for file_path in file_paths:
        if seen >= window_end:
            break
        try:
            with open(file_path, "r", encoding=encoding) as f:
                for line_no, line in enumerate(f, 1):
                    if not compiled.search(line):
                        continue
                    if offset <= seen < window_end:
                        text = line.rstrip("\n")
                        if len(text) > _GREP_MAX_TEXT_LEN:
                            text = text[: _GREP_MAX_TEXT_LEN - 3] + "..."
                        matches.append(
                            {"path": file_path, "line": line_no, "text": text}
                        )
                    seen += 1
                    if seen >= window_end:
                        break
        except UnicodeDecodeError:
            errors.append(f"无法按 {encoding} 解码: {file_path}")
        except OSError as e:
            errors.append(f"读取失败: {file_path}: {e}")

    return {
        "matches": matches,
        "count": len(matches),
        "truncated": seen >= window_end,
        "error": "; ".join(errors) if errors else "",
    }


_LIST_PARAMETERS: Dict[str, Any] = {
    "path": {"type": "string", "description": "要浏览的目录路径"},
    "depth": {"type": "integer", "description": "递归展示的最大层级深度，默认 1（仅直接子项）"},
    "show_hidden": {"type": "boolean", "description": "是否显示 . 开头的隐藏文件/目录，默认 False"},
}


def list_dir(
    path: str,
    depth: int = 1,
    show_hidden: bool = False,
):
    """格式化显示目录结构（缩进树，目录以 / 结尾）。

    - 遍历结果按名称字典序稳定排序；
    - 噪音目录（_IGNORED_DIRS）永远跳过，隐藏项由 show_hidden 控制；
    - depth 限制递归层级，避免一次输出过大。
    """
    if not os.path.isdir(path):
        return {"content": "", "error": f"目录不存在: {path}"}
    if depth < 1:
        depth = 1

    lines: list[str] = []
    errors: list[str] = []

    def _walk(current: str, level: int) -> None:
        try:
            names = os.listdir(current)
        except OSError as e:
            errors.append(f"无法读取 {current}: {e}")
            return
        for name in sorted(names):
            if name in _IGNORED_DIRS:
                continue
            if name.startswith(".") and not show_hidden:
                continue
            full = os.path.join(current, name)
            is_dir = os.path.isdir(full)
            lines.append("  " * level + name + ("/" if is_dir else ""))
            if is_dir and level < depth - 1:
                _walk(full, level + 1)

    _walk(path, 0)
    return {
        "content": "\n".join(lines),
        "error": "; ".join(errors) if errors else "",
    }


_WRITE_PARAMETERS: Dict[str, Any] = {
    "file_path": {"type": "string", "description": "要创建或覆盖的文件路径"},
    "content": {"type": "string", "description": "写入的完整新内容（覆盖已有文件）"},
    "encoding": {"type": "string", "description": "文件编码格式"},
}


def write(file_path: str, content: str, encoding: str = "utf-8"):
    """创建新文件或整体覆盖已有文件（自动创建缺失的父目录）。

    写入以二进制进行，先编码校验，避免写一半失败。
    """
    if os.path.isdir(file_path):
        return {"error": f"路径是目录，不能写入: {file_path}", "written": 0}

    try:
        payload = content.encode(encoding)
    except UnicodeEncodeError as e:
        return {"error": f"内容无法按 {encoding} 编码: {e}", "written": 0}

    parent = os.path.dirname(file_path)
    try:
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(file_path, "wb") as f:
            f.write(payload)
    except OSError as e:
        return {"error": f"写入失败: {e}", "written": 0}

    return {"written": len(payload)}


def register_builtins(center: ToolCenter) -> None:
    """把全部内置工具注册到指定 ToolCenter。"""
    center.register(
        desc="文件阅读工具",
        parameters=_READ_PARAMETERS,
        required=["file_path"],
    )(read)
    center.register(
        desc="文件查找工具：按文件名/内容正则在目录树中搜索文件",
        parameters=_FIND_PARAMETERS,
        required=["path"],
    )(find)
    center.register(
        desc="文件编辑工具：对文件做精确文本替换",
        parameters=_EDIT_PARAMETERS,
        required=["file_path", "old_text", "new_text"],
    )(edit)
    center.register(
        desc="内容搜索工具：按正则在文件/目录内搜索，支持 offset/limit 分页返回所有命中行",
        parameters=_GREP_PARAMETERS,
        required=["path", "pattern"],
    )(grep)
    center.register(
        desc="目录浏览工具：格式化显示目录结构（缩进树）",
        parameters=_LIST_PARAMETERS,
        required=["path"],
        name="list",
    )(list_dir)
    center.register(
        desc="文件写入工具：创建新文件或整体覆盖已有文件内容",
        parameters=_WRITE_PARAMETERS,
        required=["file_path", "content"],
    )(write)


# 进程内共享的工具中心单例（包含内置 read/find/grep/edit/list/write）
tool_center = ToolCenter()
register_builtins(tool_center)