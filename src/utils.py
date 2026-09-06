"""通用工具函数。"""
import uuid
from datetime import datetime


def get_uuid() -> str:
    """生成消息唯一 ID。"""
    return str(uuid.uuid4())


def get_now() -> str:
    """获取当前 ISO 格式时间字符串。"""
    return datetime.now().isoformat()
