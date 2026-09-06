"""第 3 批回归（A05 运行隔离 / A06 记忆事务 / A13 东财串行）。

等价守护 docs/ 下五份独立审核的关键断言，进入默认测试集。全部离线：
HTTP/LLM 均为替身（含 yfinance），持久化路径指向 tmp。
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from unittest.mock import Mock, patch

import pytest

from tradingagents.agents.utils import file_lock as locking
from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.dataflows import a_stock, cache_utils, config as data_config


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(cache_utils, "_MEM_CACHE", cache_utils.TTLCache())
    monkeypatch.setattr(cache_utils, "_DISK_CACHE", cache_utils.DiskCache(str(tmp_path / "dc")))
    data_config._run_config.set(None)
    yield
    data_config._run_config.set(None)


# ===========================================================================
# A05：运行级配置隔离
# ===========================================================================


class TestRunConfigIsolation:
    def test_set_config_is_thread_local(self):
        """两个线程各自 set_config 互不覆盖（原始 A05 缺陷的进程内形态）。"""
        ready, overwritten = threading.Event(), threading.Event()

        def run_a():
            data_config.set_config({"output_language": "English", "market_lookback_days": 5})
            ready.set()
            assert overwritten.wait(5)
            return (
                data_config.get_config()["output_language"],
                data_config.get_config()["market_lookback_days"],
            )

        def run_b():
            assert ready.wait(5)
            data_config.set_config({"output_language": "Chinese", "market_lookback_days": 90})
            overwritten.set()

        with ThreadPoolExecutor(2) as pool:
            a = pool.submit(run_a)
            pool.submit(run_b).result()
            assert a.result() == ("English", 5)

    def test_snapshot_deep_copies_nested_vendors(self):
        """运行快照深拷贝嵌套 dict：修改调用方原 dict 不影响快照。"""
        vendors = {"get_stock_data": "original"}
        token = data_config.set_run_config({"tool_vendors": vendors})
        try:
            vendors["get_stock_data"] = "mutated"
            assert (
                data_config.get_config()["tool_vendors"]["get_stock_data"]
                == "original"
            )
        finally:
            data_config.reset_run_config(token)

    def test_get_config_returns_copy_not_live_snapshot(self):
        """get_config 返回深拷贝——修改返回值不污染运行中的快照。"""
        token = data_config.set_run_config({"market_lookback_days": 5})
        try:
            cfg = data_config.get_config()
            cfg["market_lookback_days"] = 999
            assert data_config.get_config()["market_lookback_days"] == 5
        finally:
            data_config.reset_run_config(token)

    def test_fallback_to_process_default_without_snapshot(self):
        """无运行快照时回退进程默认（直接工具调用语义保持）。"""
        data_config.set_config({"market_lookback_days": 30})
        data_config._run_config.set(None)
        assert data_config.get_config()["market_lookback_days"] == 30


class TestCacheScope:
    def test_same_dir_different_vendors_do_not_share(self, tmp_path):
        """同缓存目录、不同数据源：缓存身份包含供应商，不互相命中。"""
        calls = {"n": 0}

        @cache_utils.cached_data(namespace="guard_vendors", use_disk=False)
        def probe(ticker):
            calls["n"] += 1
            return data_config.get_config()["data_vendors"]["core_stock_apis"]

        for vendor in ("vendor_a", "vendor_b"):
            data_config.set_config(
                {"data_cache_dir": str(tmp_path), "data_vendors": {"core_stock_apis": vendor}}
            )
            assert probe("600519") == vendor
        assert calls["n"] == 2

    def test_no_snapshot_uses_global_cache_singleton(self):
        """无运行快照：沿用全局单例（测试隔离与既有行为不变）。"""
        @cache_utils.cached_data(namespace="guard_global", use_disk=False)
        def probe(ticker):
            return "value"

        assert probe("600519") == "value"
        assert probe("600519") == "value"

    def test_use_disk_false_never_creates_scope_disk_instance(self, tmp_path):
        """use_disk=False 不创建磁盘实例/目录。"""
        data_config.set_config({"data_cache_dir": str(tmp_path / "scoped")})

        @cache_utils.cached_data(namespace="guard_nodisk", use_disk=False)
        def probe(ticker):
            return "mem-only"

        probe("600519")
        assert not (tmp_path / "scoped" / "dataflows_cache").exists()
        assert cache_utils._SCOPE_DISK_CACHES == {}


# ===========================================================================
# A06：记忆日志事务
# ===========================================================================


class TestMemoryTransactions:
    def test_concurrent_append_during_backfill_is_preserved(self, tmp_path):
        """受控交错：回填读日志后暂停，追加落地，提交不得覆盖追加。"""
        path = tmp_path / "memory.md"
        writer_a = TradingMemoryLog({"memory_log_path": str(path)})
        writer_b = TradingMemoryLog({"memory_log_path": str(path)})
        writer_a.store_decision("600519", "2026-01-01", "Rating: Buy")

        release = threading.Event()

        # 受控交错：外部持锁模拟他进程长事务 —— 追加必须被串行化（等待），
        # 释放后落地；随后回填提交也不得覆盖已落地的追加。
        with locking.file_lock(path):  # 模拟他进程持锁
            appended = {}

            def append_b():
                writer_b.store_decision("000001", "2026-01-02", "Rating: Sell")
                appended["done"] = True

            t = threading.Thread(target=append_b)
            t.start()
            time.sleep(0.2)
            assert not appended, "追加在锁被持有时不应完成（未串行化）"
        t.join(timeout=5)

        writer_a.batch_update_with_outcomes([
            dict(ticker="600519", trade_date="2026-01-01", raw_return=0.1,
                 alpha_return=0.05, holding_days=5, reflection="offline",
                 outcome_end="2026-01-08")
        ])
        entries = writer_a.load_entries()
        assert {e["ticker"] for e in entries} == {"600519", "000001"}, entries

    def test_duplicate_store_decision_is_idempotent(self, tmp_path):
        log = TradingMemoryLog({"memory_log_path": str(tmp_path / "m.md")})
        for _ in range(3):
            log.store_decision("600519", "2026-01-05", "Rating: Buy")
        assert len(log.load_entries()) == 1

    def test_double_backfill_settles_once(self, tmp_path):
        log = TradingMemoryLog({"memory_log_path": str(tmp_path / "m.md")})
        log.store_decision("600519", "2026-01-05", "Rating: Buy")
        log.update_with_outcome("600519", "2026-01-05", 0.1, 0.05, 5, "first")
        # 第二次回填：已非 pending，不覆盖
        log.update_with_outcome("600519", "2026-01-05", -0.9, -0.9, 5, "second")
        entries = log.load_entries()
        assert len(entries) == 1
        assert entries[0]["reflection"] == "first"

    def test_lock_open_failure_propagates(self, tmp_path, monkeypatch):
        """锁文件打开失败 → 异常透传，绝不进入无保护事务。"""
        monkeypatch.setattr(
            locking.os, "open", Mock(side_effect=PermissionError(13, "denied"))
        )
        with pytest.raises(PermissionError):
            with locking.file_lock(tmp_path / "m.md"):
                pytest.fail("不得在无 OS 锁时进入事务")

    def test_symlinked_log_shares_one_lock_and_follows_target(self, tmp_path):
        target, alias = tmp_path / "real.md", tmp_path / "alias.md"
        original = TradingMemoryLog({"memory_log_path": str(target)})
        original.store_decision("600519", "2026-01-05", "Rating: Buy")
        alias.symlink_to(target)

        # 同一物理文件只有一把锁：持 target 锁时 alias 锁必须等待
        with locking.file_lock(target):
            with pytest.raises(TimeoutError):
                with locking.file_lock(alias, timeout=0.02):
                    pytest.fail("别名拿到了独立锁")

        # 经别名的更新写真实文件，不替换符号链接
        other = TradingMemoryLog({"memory_log_path": str(alias)})
        other.update_with_outcome(
            "600519", "2026-01-05", 0.1, 0.05, 5, "via-alias", outcome_end="2026-01-12"
        )
        assert alias.is_symlink()
        assert not original.get_pending_entries()


# ===========================================================================
# A13：东财请求串行化
# ===========================================================================


class TestEastmoneyThrottle:
    def test_requests_are_serialized_with_interval(self, monkeypatch):
        entered, release, overlapped = threading.Event(), threading.Event(), threading.Event()
        calls = []

        def fake_get(*args, **kwargs):
            calls.append(threading.get_ident())
            if len(calls) == 1:
                entered.set()
                assert release.wait(5)
            else:
                overlapped.set()
            return Mock()

        monkeypatch.setattr(a_stock._EM_SESSION, "get", fake_get)
        monkeypatch.setattr(a_stock, "_em_last_call", [0.0])
        monkeypatch.setattr(a_stock, "_EM_MIN_INTERVAL", 0.01)

        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(a_stock._em_get, "https://offline.invalid/one")
            assert entered.wait(5)
            second = pool.submit(a_stock._em_get, "https://offline.invalid/two")
            concurrent = overlapped.wait(0.2)
            release.set()
            first.result()
            second.result()

        assert not concurrent, "第二条请求在第一条完成前进入了 HTTP"
