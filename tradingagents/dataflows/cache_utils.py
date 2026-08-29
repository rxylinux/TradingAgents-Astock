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


def cached_data(
    namespace: str,
    ttl_seconds: float = 1800.0,
    use_disk: bool = True,
    max_disk_age: Optional[float] = 86400.0,  # 24 hours default for disk
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator to cache function results in Memory LRU and optional Disk."""

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        def wrapper(*args, **kwargs) -> T:
            # Build cache key from function arguments
            key_raw = f"{fn.__name__}:{args}:{sorted(kwargs.items())}"
            # 1. Check memory cache
            cached_val = _MEM_CACHE.get(key_raw)
            if cached_val is not None:
                return cached_val

            # 2. Check disk cache
            if use_disk:
                disk_val = _DISK_CACHE.get(namespace, key_raw, max_age_seconds=max_disk_age)
                if disk_val is not None:
                    _MEM_CACHE.set(key_raw, disk_val, ttl=ttl_seconds)
                    return disk_val

            # 3. Call underlying function
            result = fn(*args, **kwargs)

            # 4. Save to caches if valid
            if result is not None:
                _MEM_CACHE.set(key_raw, result, ttl=ttl_seconds)
                if use_disk and isinstance(result, (str, dict, list, int, float, bool)):
                    _DISK_CACHE.set(namespace, key_raw, result)

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
