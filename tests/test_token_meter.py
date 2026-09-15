"""TokenMeter 单元测试（上下文窗口 10000 / 阈值 0.8 为重点场景）。"""
from agent_test.llm.token_meter import TokenMeter


def test_default_window_and_threshold():
    meter = TokenMeter()
    assert meter.max_context_tokens == 128000
    assert meter.threshold_ratio == 0.8
    assert meter.threshold_tokens == 102400


def test_window_10000_threshold_8000():
    """上下文窗口设置为 10000 时，阈值应为 8000。"""
    meter = TokenMeter(max_context_tokens=10000, threshold_ratio=0.8)
    assert meter.max_context_tokens == 10000
    assert meter.threshold_tokens == 8000
    assert meter.usage_ratio == 0.0
    assert meter.total_tokens == 0


def test_update_is_overwrite_not_accumulate():
    """update 为覆盖式赋值（usage.total_tokens 是完整上下文大小，非增量）。"""
    meter = TokenMeter(max_context_tokens=10000, threshold_ratio=0.8)
    meter.update({"total_tokens": 5000})
    meter.update({"total_tokens": 7000})
    assert meter.total_tokens == 7000  # 覆盖而非 12000
    assert abs(meter.usage_ratio - 0.7) < 1e-9
    assert not meter.is_over_threshold()


def test_over_threshold_and_reset():
    meter = TokenMeter(max_context_tokens=10000, threshold_ratio=0.8)
    assert not meter.is_over_threshold()  # 无记录不算超阈值

    meter.update({"total_tokens": 7999})
    assert not meter.is_over_threshold()  # 7999 <= 8000 未超

    meter.update({"total_tokens": 8001})
    assert meter.is_over_threshold()  # 8001 > 8000 超阈值

    meter.reset()
    assert not meter.is_over_threshold()
    assert meter.usage_ratio == 0.0


def test_total_tokens_missing_key_counts_as_zero():
    meter = TokenMeter(max_context_tokens=10000, threshold_ratio=0.8)
    meter.update({"prompt_tokens": 123})
    assert meter.total_tokens == 0
    assert not meter.is_over_threshold()