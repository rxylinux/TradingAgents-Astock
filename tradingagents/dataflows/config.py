"""Data-layer configuration: process default + per-run snapshot (A05).

Historical behaviour — one module-level ``_config`` mutated by every graph
construction — let two runs in the same process overwrite each other's
language, lookback window, vendors and cache paths. The fix is a **run-level
snapshot** carried in a ``ContextVar``:

- ``set_run_config(config)`` deep-copies the mapping (nested ``tool_vendors``
  included — sharing the dict would re-introduce cross-run mutation) into the
  current context and returns a reset token. ``TradingAgentsGraph`` wraps its
  whole run lifecycle (prepare → stream → finalize → close) with it, so every
  LangGraph node, tool call and memory backfill inside the run — including
  nodes executed on LangGraph worker threads, which propagate the calling
  context — observes its own graph's configuration.
- ``get_config()`` prefers the run snapshot and **returns a deep copy** so
  callers can't mutate a live run's configuration either.
- The module-level default (``set_config``) remains only as the fallback for
  direct tool usage outside any run; concurrent *runs* never read it.
"""

import copy
from contextvars import ContextVar, Token
from typing import Dict, Optional

import tradingagents.default_config as default_config

# 进程级默认：仅当当前上下文没有运行快照时（脱离图运行的直接工具调用）
# 作为回退。并发运行从不读取它 —— 各自持有独立快照。
_config: Optional[Dict] = None

# 运行级快照：随 LangGraph 的线程/任务创建传播（已实测：并行节点在
# 工作线程上正确读到调用线程的值，其他线程不受污染）。
_run_config: ContextVar[Optional[Dict]] = ContextVar(
    "tradingagents_run_config", default=None
)


def initialize_config():
    """Initialize the configuration with default values."""
    global _config
    if _config is None:
        _config = default_config.DEFAULT_CONFIG.copy()


def set_default_config(config: Dict):
    """Update only the process-default configuration (no context side effects).

    A05: graph construction uses this — building another graph must not
    mutate the *caller's* active run context; runs bind their own snapshot
    at the entry point instead.
    """
    global _config
    incoming = copy.deepcopy(config)
    if _config is None:
        _config = default_config.DEFAULT_CONFIG.copy()
    _config.update(incoming)


def set_config(config: Dict):
    """Set configuration for the **current context** (A05).

    Merges into both the process default and a thread-local run snapshot —
    two threads calling ``set_config`` no longer overwrite each other, and
    partial updates keep the surrounding keys (callers routinely pass a few
    fields, not the whole mapping). ``TradingAgentsGraph`` additionally
    re-binds its own snapshot at the run entry so construction and execution
    may happen on different threads.
    """
    global _config
    incoming = copy.deepcopy(config)
    if _config is None:
        _config = default_config.DEFAULT_CONFIG.copy()
    _config.update(incoming)
    base = _run_config.get()
    merged = copy.deepcopy(base) if base is not None else copy.deepcopy(_config)
    merged.update(incoming)
    _run_config.set(merged)


def set_run_config(config: Dict) -> Token:
    """Bind a deep-copied run snapshot to the current context (A05).

    Nested structures (``tool_vendors`` …) are copied too — sharing them
    would let one run mutate another's vendor routing. Returns the token for
    ``reset_run_config``; the whole run lifecycle must run between the two.
    """
    return _run_config.set(copy.deepcopy(config))


def reset_run_config(token: Token) -> None:
    """Restore the previous context after a run lifecycle ends (also on errors)."""
    _run_config.reset(token)


def run_cache_dir() -> Optional[str]:
    """Current run snapshot's ``data_cache_dir``; None without a snapshot.

    A05: cache scoping must distinguish "an isolated run owns this directory"
    from "ambient process default" — the latter keeps the legacy global cache
    singleton so test isolation (and direct tool usage) is unchanged.
    """
    snapshot = _run_config.get()
    if snapshot is None:
        return None
    return str(snapshot.get("data_cache_dir") or "")


def get_config() -> Dict:
    """Get the configuration: run snapshot first, process default as fallback.

    Returns a deep copy — mutating the result never affects a live run.
    """
    snapshot = _run_config.get()
    if snapshot is not None:
        return copy.deepcopy(snapshot)
    if _config is None:
        initialize_config()
    return copy.deepcopy(_config)


# Initialize with default config
initialize_config()
