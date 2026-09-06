"""Thread-safe multi-tier caching and resilient HTTP retry utilities for TradingAgents.

Provides:
- TTLCache: In-memory LRU cache with expiration time (TTL)
- DiskCache: Persistent local disk cache for static financial data and historical snapshots
- robust_request: Resilient HTTP call wrapper with exponential backoff and timeout protection
"""

from __future__ import annotations

import collections
import hashlib
import json
import logging
import os
import time
import threading
from typing import Any, Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class TTLCache:
    """Thread-safe In-Memory LRU Cache with Time-To-Live (TTL) expiration."""

    def __init__(self, maxsize: int = 500, default_ttl: float = 3600.0):
        self._maxsize = maxsize
        self._default_ttl = default_ttl
        self._data: collections.OrderedDict[str, tuple[float, Any]] = collections.OrderedDict()
        self._lock = threading.RLock()

    def get(self, key: str, default: Any = None) -> Any:
        """Retrieve a value from the cache if present and not expired."""
        with self._lock:
            if key not in self._data:
                return default
            expire_at, value = self._data[key]
            if time.time() > expire_at:
                del self._data[key]
                return default
            # Move to end (LRU update)
            self._data.move_to_end(key)
            return value

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        """Store a value in the cache with a TTL (in seconds)."""
        duration = self._default_ttl if ttl is None else ttl
        expire_at = time.time() + duration
        with self._lock:
            if key in self._data:
                del self._data[key]
            elif len(self._data) >= self._maxsize:
                # Evict the oldest item
                self._data.popitem(last=False)
            self._data[key] = (expire_at, value)

    def clear(self) -> None:
        """Clear all cached entries."""
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            # Clean expired items on count
            now = time.time()
            keys_to_remove = [k for k, (exp, _) in self._data.items() if now > exp]
            for k in keys_to_remove:
                del self._data[k]
            return len(self._data)


class DiskCache:
    """Persistent local disk cache for API responses and heavy data lookups."""

    def __init__(self, cache_dir: Optional[str] = None):
        if cache_dir:
            self.cache_dir = cache_dir
        else:
            home = os.getenv("TRADINGAGENTS_CACHE_DIR") or os.path.expanduser("~/.tradingagents/cache")
            self.cache_dir = home
        os.makedirs(self.cache_dir, exist_ok=True)
        self._lock = threading.Lock()

    def _get_path(self, namespace: str, key: str) -> str:
        safe_ns = "".join(c for c in namespace if c.isalnum() or c in ("-", "_"))
        key_hash = hashlib.md5(key.encode("utf-8")).hexdigest()
        return os.path.join(self.cache_dir, f"{safe_ns}_{key_hash}.json")

    def get(self, namespace: str, key: str, max_age_seconds: Optional[float] = None) -> Optional[Any]:
        """Read cached JSON data from disk if exists and within max_age."""
        path = self._get_path(namespace, key)
        if not os.path.exists(path):
            return None
        try:
            with self._lock:
                if max_age_seconds is not None:
                    mtime = os.path.getmtime(path)
                    if time.time() - mtime > max_age_seconds:
                        return None
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logger.debug("Disk cache read error for %s/%s: %s", namespace, key, e)
            return None

    def set(self, namespace: str, key: str, value: Any) -> None:
        """Save JSON-serializable data to disk cache."""
        path = self._get_path(namespace, key)
        try:
            with self._lock:
                tmp_path = f"{path}.tmp.{os.getpid()}"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(value, f, ensure_ascii=False)
                os.replace(tmp_path, path)
        except Exception as e:
            logger.debug("Disk cache write error for %s/%s: %s", namespace, key, e)


# Global singleton cache instances
_MEM_CACHE = TTLCache(maxsize=1000, default_ttl=1800.0)  # 30 minutes memory cache
_DISK_CACHE = DiskCache()

# A05: 缓存必须按运行配置隔离——原先 key 只有函数名和参数，同参数不同配置
# （不同 data_cache_dir / 供应商）会命中对方的结果。作用域取自运行配置的
# data_cache_dir：内存 key 加作用域前缀；磁盘按目录建立独立实例（自定义
# data_cache_dir 真正生效）。作用域哈希不包含任何凭据值，也不会被日志打印。
_SCOPE_DISK_CACHES: "dict[str, DiskCache]" = {}
_SCOPE_DISK_GUARD = threading.Lock()


def _cache_scope() -> str:
    """Current run's cache scope: fingerprint of behaviour-affecting config.

    空串 = 没有运行快照（脱离图运行的直接工具调用）：沿用进程级全局缓存
    单例（保持既有行为与测试隔离）。有运行快照时，作用域指纹 = 缓存目录
    + 数据源选择（tool_vendors / data_vendors）——同参数不同配置（不同
    目录**或**不同数据源）都不命中对方结果。指纹不含任何凭据值，也不被
    日志打印。
    """
    from tradingagents.dataflows.config import get_config, run_cache_dir

    if run_cache_dir() is None:
        return ""
    cfg = get_config()
    parts = [
        str(cfg.get("data_cache_dir") or ""),
        repr(sorted((cfg.get("tool_vendors") or {}).items())),
        repr(sorted((cfg.get("data_vendors") or {}).items())),
    ]
    return hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()[:12]


def _disk_cache_for(scope: str) -> DiskCache:
    """Disk cache instance for a scope: global default, or a per-dir instance."""
    if not scope:
        return _DISK_CACHE
    with _SCOPE_DISK_GUARD:
        cache = _SCOPE_DISK_CACHES.get(scope)
        if cache is None:
            from tradingagents.dataflows.config import run_cache_dir

            scope_dir = run_cache_dir() or ""
            cache = DiskCache(cache_dir=os.path.join(scope_dir, "dataflows_cache"))
            _SCOPE_DISK_CACHES[scope] = cache
        return cache

# 缓存格式版本（R5/F1/F2/A04）：v4 起，财报三表以实际披露可知时间过滤
# （A04），v3 及更早写入的 financials 结果（按会计报告期过滤、可能包含
# 当时尚未披露的报表）留在原命名空间天然失效——不删除用户已有缓存文件，
# 升级后自动隔离。失败输出自 v2 起以 DataFailure / 失败标记表达，不进入
# 任何一层缓存。
CACHE_SCHEMA_VERSION = 4

# 已知失败输出标记（字符串兜底，用于识别绕过写入口的历史污染条目）。
# 新代码的失败输出应使用 DataFailure 类型（结构化判定，不依赖文案匹配）。
_FAILURE_OUTPUT_MARKERS = (
    "[数据缺失",  # 数据层统一的请求失败/数据缺失标记（R3 起）
    "Error retrieving",  # 财报三表（Sina）失败前缀
    "查询失败",  # 旧版中文失败文案（用于识别历史污染条目）
)


class DataFailure(str):
    """A tool output that reports a data retrieval failure.

    Behaves exactly like ``str`` downstream (ToolMessage content, report
    rendering), but ``cached_data`` refuses to persist it in either tier.
    This is the structural fix for "failure text reaches the cache because
    it is a non-None string": the failure semantics survive all the way to
    the cache boundary instead of being matched by error-message prefixes.
    """


def _is_failure_output(result: Any) -> bool:
    """True if a function result is a failure/error string rather than data.

    ``DataFailure`` instances are failures by type. Plain strings containing
    known failure markers are treated as failures too (legacy outputs and
    defensively, polluted cache entries). Success-but-empty outputs (e.g.
    ``"No data found for stock 600519"``) are NOT failures: the query
    succeeded, caching them avoids hammering the vendor for stocks that
    simply have no records.
    """
    if isinstance(result, DataFailure):
        return True
    return isinstance(result, str) and any(m in result for m in _FAILURE_OUTPUT_MARKERS)


def _versioned_namespace(namespace: str) -> str:
    return f"{namespace}-v{CACHE_SCHEMA_VERSION}"


def cached_data(
    namespace: str,
    ttl_seconds: float = 1800.0,
    use_disk: bool = True,
    max_disk_age: Optional[float] = 86400.0,  # 24 hours default for disk
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator to cache function results in Memory LRU and optional Disk.

    Failures are never cached in either tier: ``DataFailure`` outputs (by
    type) and strings with known failure markers (see
    ``_FAILURE_OUTPUT_MARKERS``). A transient outage therefore cannot shadow
    a recovering data source for hours. Successful results — including
    success-but-empty — are cached with the configured TTLs.
    """

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        def wrapper(*args, **kwargs) -> T:
            # A05: 作用域前缀使同参数不同运行配置（缓存目录隔离的并发
            # 任务）不会互相命中对方的结果。
            scope = _cache_scope()
            # Build cache key from function arguments
            key_raw = f"{scope}|{fn.__name__}:{args}:{sorted(kwargs.items())}"
            # use_disk=False 时不得创建磁盘实例/目录（连实例都不构建）
            disk_cache = _disk_cache_for(scope) if use_disk else None
            # 1. Check memory cache (guard against failure strings anyway)
            cached_val = _MEM_CACHE.get(key_raw)
            if cached_val is not None and not _is_failure_output(cached_val):
                return cached_val

            # 2. Check disk cache
            if use_disk:
                disk_val = disk_cache.get(
                    _versioned_namespace(namespace), key_raw, max_age_seconds=max_disk_age
                )
                if disk_val is not None:
                    if _is_failure_output(disk_val):
                        # 污染条目按 miss 处理，让本次调用重新取数
                        logger.warning(
                            "Ignoring polluted cache entry for %s (failure output)",
                            key_raw,
                        )
                    else:
                        _MEM_CACHE.set(key_raw, disk_val, ttl=ttl_seconds)
                        return disk_val

            # 3. Call underlying function
            result = fn(*args, **kwargs)

            # 4. Save to caches only if it is real data
            if result is not None and not _is_failure_output(result):
                _MEM_CACHE.set(key_raw, result, ttl=ttl_seconds)
                if use_disk and isinstance(result, (str, dict, list, int, float, bool)):
                    disk_cache.set(_versioned_namespace(namespace), key_raw, result)

            return result

        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        return wrapper

    return decorator


def robust_api_call(
    fn: Callable[[], T],
    max_retries: int = 2,
    base_delay: float = 0.5,
    backoff_factor: float = 2.0,
    fallback_value: Optional[T] = None,
    error_log_prefix: str = "API call failed",
) -> T:
    """Execute an API call with automatic retries, backoff, and fallback."""
    last_err = None
    delay = base_delay
    for attempt in range(1, max_retries + 1):
        try:
            return fn()
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                time.sleep(delay)
                delay *= backoff_factor
            else:
                logger.warning("%s (attempt %d/%d): %s", error_log_prefix, attempt, max_retries, e)

    if fallback_value is not None:
        return fallback_value
    raise last_err
