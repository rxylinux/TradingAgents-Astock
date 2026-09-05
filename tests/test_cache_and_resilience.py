"""Unit tests for multi-tier cache and robust API retry utilities."""

import os
import shutil
import tempfile
import time
import pytest

from tradingagents.dataflows import cache_utils
from tradingagents.dataflows.cache_utils import (
    TTLCache,
    DiskCache,
    cached_data,
    robust_api_call,
)


@pytest.mark.unit
def test_ttl_cache_basic_and_expiry():
    cache = TTLCache(maxsize=3, default_ttl=0.1)
    cache.set("a", 1)
    cache.set("b", 2)

    assert cache.get("a") == 1
    assert cache.get("b") == 2
    assert cache.get("c") is None

    # Wait for TTL expiry
    time.sleep(0.15)
    assert cache.get("a") is None
    assert cache.get("b") is None


@pytest.mark.unit
def test_ttl_cache_lru_eviction():
    cache = TTLCache(maxsize=2, default_ttl=10.0)
    cache.set("k1", "v1")
    cache.set("k2", "v2")

    # Access k1 to make k2 the oldest
    _ = cache.get("k1")

    # Insert k3, should evict k2
    cache.set("k3", "v3")
    assert cache.get("k1") == "v1"
    assert cache.get("k2") is None
    assert cache.get("k3") == "v3"


@pytest.mark.unit
def test_disk_cache_operations():
    temp_dir = tempfile.mkdtemp()
    try:
        disk_cache = DiskCache(cache_dir=temp_dir)
        disk_cache.set("test_ns", "key_1", {"foo": "bar", "val": 123})

        data = disk_cache.get("test_ns", "key_1")
        assert data == {"foo": "bar", "val": 123}

        # Check non-existent
        assert disk_cache.get("test_ns", "non_existent") is None
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.unit
def test_cached_data_decorator():
    call_count = [0]

    @cached_data(namespace="test_fn", ttl_seconds=10.0, use_disk=False)
    def compute(x: int, y: int) -> int:
        call_count[0] += 1
        return x + y

    assert compute(2, 3) == 5
    assert call_count[0] == 1

    # Second call with same args hits cache
    assert compute(2, 3) == 5
    assert call_count[0] == 1

    # Different args computes again
    assert compute(3, 4) == 7
    assert call_count[0] == 2


@pytest.mark.unit
def test_robust_api_call_retry_success():
    attempts = [0]

    def flaky_api():
        attempts[0] += 1
        if attempts[0] < 2:
            raise ConnectionResetError("network glitch")
        return "success"

    res = robust_api_call(flaky_api, max_retries=3, base_delay=0.01)
    assert res == "success"
    assert attempts[0] == 2


@pytest.mark.unit
def test_robust_api_call_fallback_on_failure():
    def broken_api():
        raise TimeoutError("timeout")

    res = robust_api_call(
        broken_api,
        max_retries=2,
        base_delay=0.01,
        fallback_value="fallback_data",
    )
    assert res == "fallback_data"


# ===========================================================================
# R5: 失败输出不得进入长期缓存
# ===========================================================================


@pytest.fixture
def isolated_global_cache(monkeypatch, tmp_path):
    """把全局内存/磁盘缓存单例替换为临时实例。"""
    monkeypatch.setattr(cache_utils, "_MEM_CACHE", TTLCache(maxsize=100, default_ttl=60))
    monkeypatch.setattr(
        cache_utils, "_DISK_CACHE", DiskCache(cache_dir=str(tmp_path / "dc"))
    )
    return tmp_path


@pytest.mark.unit
@pytest.mark.parametrize(
    "failure_text",
    [
        "[数据缺失: 解禁查询失败] TimeoutError",
        "Error retrieving balance sheet for 600519: timeout",
        "行业对比查询失败: connection reset",
    ],
    ids=["missing_marker", "english_error", "legacy_chinese_error"],
)
def test_failure_outputs_are_never_cached(isolated_global_cache, failure_text):
    calls = [0]

    @cached_data(namespace="r5_guard", ttl_seconds=3600.0)
    def fetch(x: int) -> str:
        calls[0] += 1
        return failure_text

    fetch(1)
    fetch(1)

    assert calls[0] == 2, "失败输出不得命中缓存，第二次必须重新执行"


@pytest.mark.unit
def test_failure_then_recovery_refetches_and_caches_success(isolated_global_cache):
    """首次错误、随后数据源恢复：相同参数能再次取数并返回成功，成功可命中缓存。"""
    state = {"fail": True}
    calls = [0]

    @cached_data(namespace="r5_recover", ttl_seconds=3600.0)
    def fetch(x: int) -> str:
        calls[0] += 1
        if state["fail"]:
            return "Error retrieving data for 600519: timeout"
        return "GOOD_DATA"

    first = fetch(1)
    assert "Error retrieving" in first

    state["fail"] = False
    second = fetch(1)
    assert second == "GOOD_DATA", "数据源恢复后，相同参数不得再返回旧错误"
    assert calls[0] == 2

    third = fetch(1)
    assert third == "GOOD_DATA"
    assert calls[0] == 3 - 1, "成功结果应正常命中缓存"


@pytest.mark.unit
def test_no_stale_failure_from_disk_after_memory_restart(isolated_global_cache, monkeypatch):
    """清空内存（模拟进程重启）后，不会从磁盘恢复长期失败结果。"""
    state = {"fail": True}
    calls = [0]

    @cached_data(namespace="r5_disk", ttl_seconds=3600.0)
    def fetch(x: int) -> str:
        calls[0] += 1
        if state["fail"]:
            return "[数据缺失: 查询失败] timeout"
        return "RECOVERED"

    fetch(1)

    # 模拟进程重启：全新的内存缓存，磁盘缓存保留
    monkeypatch.setattr(cache_utils, "_MEM_CACHE", TTLCache(maxsize=100, default_ttl=60))

    state["fail"] = False
    out = fetch(1)

    assert out == "RECOVERED", "重启后不得从磁盘恢复旧的失败结果"
    assert calls[0] == 2


@pytest.mark.unit
def test_success_cache_hit_and_ttl_expiry_with_fake_clock(isolated_global_cache, monkeypatch):
    """成功结果仍可命中内存与磁盘；过期后能重新取数（固定时钟，不实际等待）。"""
    now = [1000.0]
    monkeypatch.setattr(cache_utils.time, "time", lambda: now[0])

    calls = [0]

    @cached_data(namespace="r5_ttl", ttl_seconds=10.0, use_disk=False)
    def fetch(x: int) -> int:
        calls[0] += 1
        return x * 2

    assert fetch(2) == 4
    assert fetch(2) == 4
    assert calls[0] == 1, "TTL 内应命中内存缓存"

    now[0] += 11  # 内存 TTL 过期
    assert fetch(2) == 4
    assert calls[0] == 2, "内存过期后应重新取数"


@pytest.mark.unit
def test_disk_hit_and_max_age_expiry_with_fake_clock(isolated_global_cache, monkeypatch):
    """磁盘命中：内存重启后从磁盘恢复；max_disk_age 过期后重新取数。"""
    now = [time.time()]
    monkeypatch.setattr(cache_utils.time, "time", lambda: now[0])

    calls = [0]

    @cached_data(namespace="r5_disk_ttl", ttl_seconds=3600.0, use_disk=True, max_disk_age=60.0)
    def fetch(x: int) -> int:
        calls[0] += 1
        return x + 1

    assert fetch(1) == 2
    # 内存重启，磁盘保留
    monkeypatch.setattr(cache_utils, "_MEM_CACHE", TTLCache(maxsize=100, default_ttl=60))

    assert fetch(1) == 2
    assert calls[0] == 1, "磁盘命中不应重新执行"

    now[0] += 61  # 磁盘 max_age 过期
    monkeypatch.setattr(cache_utils, "_MEM_CACHE", TTLCache(maxsize=100, default_ttl=60))
    assert fetch(1) == 2
    assert calls[0] == 2, "磁盘过期后应重新取数"


@pytest.mark.unit
def test_success_empty_result_is_cached_and_differs_from_failure(isolated_global_cache):
    """成功空结果（如 'No data found'）与请求故障行为不同：空结果可缓存。"""
    calls = [0]

    @cached_data(namespace="r5_empty", ttl_seconds=3600.0)
    def fetch_empty(x: int) -> str:
        calls[0] += 1
        return f"No data found for stock {x}"

    fetch_empty(1)
    fetch_empty(1)
    assert calls[0] == 1, "成功空结果应命中缓存（与故障输出不同）"


@pytest.mark.unit
def test_legacy_polluted_cache_does_not_block_recovery(isolated_global_cache):
    """旧格式（v1 命名空间）的污染缓存不会继续阻塞数据恢复。"""
    # 直接手工写入旧命名空间（无版本后缀）的失败字符串，模拟历史污染
    legacy_ns = "r5_legacy"
    cache_utils._DISK_CACHE.set(legacy_ns, "fetch:1", "[数据缺失: 查询失败] old junk")
    # 同样在当前命名空间写入失败字符串，模拟绕过写入口的污染
    current_ns = f"{legacy_ns}-v{cache_utils.CACHE_SCHEMA_VERSION}"
    cache_utils._DISK_CACHE.set(current_ns, "fetch:1", "Error retrieving: poisoned")

    calls = [0]

    @cached_data(namespace=legacy_ns, ttl_seconds=3600.0)
    def fetch(x: int) -> str:
        calls[0] += 1
        return "FRESH_DATA"

    out = fetch(1)

    assert out == "FRESH_DATA", "旧污染缓存不得阻塞新数据"
    assert calls[0] == 1


@pytest.mark.unit
def test_failure_output_in_current_namespace_treated_as_miss(isolated_global_cache):
    """当前命名空间下读到的失败字符串（读取时验证兜底）按 miss 处理。"""
    ns = "r5_readguard"
    current_ns = f"{ns}-v{cache_utils.CACHE_SCHEMA_VERSION}"
    cache_utils._DISK_CACHE.set(current_ns, "fetch:7", "[数据缺失: 查询失败] junk")

    calls = [0]

    @cached_data(namespace=ns, ttl_seconds=3600.0)
    def fetch(x: int) -> str:
        calls[0] += 1
        return "CLEAN"

    assert fetch(7) == "CLEAN"
    assert calls[0] == 1


# ---------------------------------------------------------------------------
# 集成：经过真实财报函数的离线验证
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_balance_sheet_failure_not_persisted_then_recovers(isolated_global_cache, monkeypatch):
    """真实 get_balance_sheet：首次源故障返回错误串且不落缓存，恢复后重新取数。"""
    import pandas as pd

    from tradingagents.dataflows import a_stock

    state = {"fail": True}
    calls = [0]

    def fake_sina(code, source_type, freq, curr_date):
        calls[0] += 1
        if state["fail"]:
            raise TimeoutError("sina timeout")
        return pd.DataFrame({"报告日": ["2026-06-30"], "货币资金": [100]})

    monkeypatch.setattr(a_stock, "_get_financial_report_sina", fake_sina)

    first = a_stock.get_balance_sheet("600519", curr_date="2026-09-04")
    assert first.startswith("Error retrieving balance sheet"), first

    # 模拟进程重启（内存缓存清空，磁盘保留）
    monkeypatch.setattr(cache_utils, "_MEM_CACHE", TTLCache(maxsize=100, default_ttl=60))

    state["fail"] = False
    second = a_stock.get_balance_sheet("600519", curr_date="2026-09-04")

    assert "货币资金" in second, f"数据源恢复后应重新取数，实际: {second[:120]}"
    assert calls[0] == 2, "首次的错误结果不得被缓存（应再次调用数据源）"

    third = a_stock.get_balance_sheet("600519", curr_date="2026-09-04")
    assert "货币资金" in third
    assert calls[0] == 2, "成功结果应正常命中缓存"
