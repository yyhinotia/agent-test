"""Token 用量计量器：根据 LLM usage 判断是否接近/超过上下文窗口阈值。

知识增量契约
--------------
- update: 覆盖式赋值（非累加），因为 usage.total_tokens 是本次请求的
  完整上下文大小（prompt + 历史 + 输出），不是增量；
- is_over_threshold: total_tokens > max_context_tokens * threshold_ratio；
- reset: 清零，压缩成功后调用；
- threshold_tokens / usage_ratio 为只读属性。
"""
from __future__ import annotations

from typing import Any, Dict


class TokenMeter:
    """按最近一次 LLM usage 度量上下文占用。"""

    def __init__(
        self, max_context_tokens: int = 128000, threshold_ratio: float = 0.8
    ):
        """初始化计量器。

        max_context_tokens: 模型上下文窗口大小（测试小窗口时设为 10000）；
        threshold_ratio:    触发压缩的阈值比例（默认 0.8）。
        """
        if max_context_tokens <= 0:
            raise ValueError("max_context_tokens 必须为正整数")
        if not 0 < threshold_ratio <= 1:
            raise ValueError("threshold_ratio 必须在 (0, 1] 区间内")
        self.max_context_tokens = max_context_tokens
        self.threshold_ratio = threshold_ratio
        self._usage: Dict[str, Any] | None = None

    def update(self, usage: Dict[str, Any] | None) -> None:
        """覆盖式记录最近一次 usage（None 表示本次调用无用量信息）。"""
        self._usage = usage

    def is_over_threshold(self) -> bool:
        """total_tokens 是否超过阈值（threshold_tokens）。"""
        if not self._usage:
            return False
        return self._usage.get("total_tokens", 0) > self.threshold_tokens

    def reset(self) -> None:
        """清零用量记录（压缩成功后调用）。"""
        self._usage = None

    @property
    def threshold_tokens(self) -> int:
        """触发压缩的 token 阈值：int(max_context_tokens * threshold_ratio)。"""
        return int(self.max_context_tokens * self.threshold_ratio)

    @property
    def usage_ratio(self) -> float:
        """当前上下文占用比例：total_tokens / max_context_tokens。"""
        if not self._usage:
            return 0.0
        return self._usage.get("total_tokens", 0) / self.max_context_tokens

    @property
    def total_tokens(self) -> int:
        """最近一次 usage 的 total_tokens；无记录时返回 0。"""
        if not self._usage:
            return 0
        return self._usage.get("total_tokens", 0)