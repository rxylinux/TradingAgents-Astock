"""Unit tests for multi-tier cache and robust API retry utilities."""

import os
import shutil
import tempfile
import time
import pytest

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
