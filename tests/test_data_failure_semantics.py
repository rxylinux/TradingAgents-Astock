"""数据请求失败与"成功但没有记录"必须区分（R3）。

`_eastmoney_datacenter()` 重试耗尽后返回 ``[]``，与查询成功但无记录完全相
同——下游据此输出确定性的"没有事件"结论（"无待解禁"/"未上龙虎榜"），接口
故障被伪装成无风险事件。修复后：请求失败以 ``[数据缺失: ...]`` 显式标注，
成功空结果才允许输出"无记录"。
"""

from __future__ import annotations

import pytest

from tradingagents.dataflows import a_stock
from tradingagents.dataflows import cache_utils
from tradingagents.dataflows.cache_utils import DiskCache, TTLCache


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        """2xx 假响应：HTTP 状态验证直接通过（F1 起 _fetch 会调用它）。"""
        return None


def _ok_response(rows):
    """东财 datacenter 正常响应（success=true，可能无记录）。"""
    return {
        "version": "test",
        "result": {"pages": "1", "data": rows, "count": str(len(rows))},
        "success": True,
        "message": "ok",
        "code": 0,
    }


def _empty_ok_response():
    """查询成功但无记录：success=true 且 result=null。"""
    return {"version": "test", "result": None, "success": True, "message": "ok", "code": 0}


@pytest.fixture
def isolated_cache(monkeypatch, tmp_path):
    """把全局内存/磁盘缓存换成临时实例，测试之间互不串扰。"""
    monkeypatch.setattr(cache_utils, "_MEM_CACHE", TTLCache(maxsize=100, default_ttl=60))
    monkeypatch.setattr(
        cache_utils, "_DISK_CACHE", DiskCache(cache_dir=str(tmp_path / "diskcache"))
    )
    return tmp_path


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """禁用重试等待，加速测试。"""
    monkeypatch.setattr("tradingagents.dataflows.cache_utils.time.sleep", lambda s: None)


# ---------------------------------------------------------------------------
# 解禁日历：失败不得落入"无待解禁"成功分支
# ---------------------------------------------------------------------------


def test_lockup_timeout_shows_data_missing_not_no_events(isolated_cache, no_retry_sleep, monkeypatch):
    def boom(*a, **k):
        raise TimeoutError("connect timeout")

    monkeypatch.setattr(a_stock, "_em_get", boom)

    out = a_stock.get_lockup_expiry("600519", "2026-09-04")

    assert "[数据缺失" in out, f"超时必须显式标注数据缺失，实际输出:\n{out}"
    assert "无待解禁" not in out, "接口超时被解释成了『未来无解禁』"
    assert "无历史解禁记录" not in out, "接口超时被解释成了『无历史解禁』"


def test_lockup_http_error_shows_data_missing(isolated_cache, no_retry_sleep, monkeypatch):
    class BadJson:
        def json(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

        def raise_for_status(self):
            return None

    monkeypatch.setattr(a_stock, "_em_get", lambda *a, **k: BadJson())

    out = a_stock.get_lockup_expiry("600519", "2026-09-04")

    assert "[数据缺失" in out
    assert "无待解禁" not in out


def test_lockup_http_500_status_shows_data_missing(isolated_cache, no_retry_sleep, monkeypatch):
    """真实 HTTP 5xx（响应体可为任意内容）：必须按失败处理，不能当成功解析。

    F1 复审指出原 "HTTP error" 测试只验证 JSON 解析失败；网关错误页也可能是
    可解析 JSON，二者不能等同。
    """

    class ServerError:
        status_code = 502

        def raise_for_status(self):
            raise a_stock._requests.HTTPError("502 Server Error: bad gateway")

        def json(self):
            # 网关错误页也可能带可解析 JSON —— 不因可解析就当成功
            return {"success": True, "result": {"data": [{"FREE_DATE": "2099-01-01"}]}}

    monkeypatch.setattr(a_stock, "_em_get", lambda *a, **k: ServerError())

    out = a_stock.get_lockup_expiry("600519", "2026-09-04")

    assert "[数据缺失" in out, "HTTP 5xx 必须按请求失败处理"
    assert "无待解禁" not in out
    assert "2099-01-01" not in out, "HTTP 失败响应体里的载荷不得被采信"


def test_lockup_vendor_business_error_shows_data_missing(isolated_cache, no_retry_sleep, monkeypatch):
    """供应商 JSON 可解析但业务失败（success=false）：不能因 JSON 可解析就当成功。"""
    monkeypatch.setattr(
        a_stock,
        "_em_get",
        lambda *a, **k: FakeResp({"success": False, "message": "查询过于频繁", "code": 500}),
    )

    out = a_stock.get_lockup_expiry("600519", "2026-09-04")

    assert "[数据缺失" in out
    assert "无待解禁" not in out


def test_lockup_success_no_records_still_says_no_events(isolated_cache, monkeypatch):
    """确认成功且没有记录时，仍可以输出没有事件。"""
    monkeypatch.setattr(
        a_stock, "_em_get", lambda *a, **k: FakeResp(_empty_ok_response())
    )

    out = a_stock.get_lockup_expiry("600519", "2026-09-04")

    assert "无历史解禁记录" in out, "成功空结果应正常输出『无历史解禁记录』"
    assert "无待解禁" in out, "成功空结果应正常输出『未来无待解禁』"
    assert "[数据缺失" not in out


def test_lockup_success_with_records_renders_rows(isolated_cache, monkeypatch):
    """成功且有记录：正常渲染（防止失败判定误伤成功路径）。"""
    rows = [
        {
            "SECURITY_CODE": "600519",
            "FREE_DATE": "2026-10-15 00:00:00",
            "LIMITED_STOCK_TYPE": "定向增发机构配售股份",
            "FREE_SHARES_NUM": 1234567,
            "FREE_RATIO": "2.5%",
        }
    ]
    monkeypatch.setattr(a_stock, "_em_get", lambda *a, **k: FakeResp(_ok_response(rows)))

    out = a_stock.get_lockup_expiry("600519", "2026-09-04")

    assert "2026-10-15" in out
    assert "定向增发" in out
    assert "[数据缺失" not in out


def test_lockup_retry_then_success_returns_data(isolated_cache, no_retry_sleep, monkeypatch):
    """首次失败、重试成功：返回正常结果，重试次数有上限。"""
    calls = {"n": 0}
    rows = [
        {
            "SECURITY_CODE": "600519",
            "FREE_DATE": "2026-10-15 00:00:00",
            "LIMITED_STOCK_TYPE": "股权激励限售股",
            "FREE_SHARES_NUM": 100,
            "FREE_RATIO": "0.1%",
        }
    ]

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("first attempt timeout")
        return FakeResp(_ok_response(rows))

    monkeypatch.setattr(a_stock, "_em_get", flaky)

    out = a_stock.get_lockup_expiry("600519", "2026-09-04")

    assert "2026-10-15" in out
    assert "[数据缺失" not in out
    # 历史查询 2 次尝试（首败+重试成功）+ 未来日历查询 1 次成功
    assert calls["n"] == 3, f"实际调用 {calls['n']} 次"


def test_lockup_retry_has_upper_bound(isolated_cache, no_retry_sleep, monkeypatch):
    """持续失败时重试有上限：每个查询最多 max_retries 次，不无限循环。"""
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise TimeoutError("always timeout")

    monkeypatch.setattr(a_stock, "_em_get", boom)

    a_stock.get_lockup_expiry("600519", "2026-09-04")

    # 2 个查询（历史 + 未来）× max_retries=2 次尝试 = 4 次
    assert calls["n"] == 4, f"重试应有上限，实际调用了 {calls['n']} 次"


def test_lockup_partial_failure_keeps_confirmed_part(isolated_cache, no_retry_sleep, monkeypatch):
    """历史查询成功、未来日历查询失败：保留已确认结果并单独指出缺失部分。"""
    rows = [
        {
            "SECURITY_CODE": "600519",
            "FREE_DATE": "2025-06-01 00:00:00",
            "LIMITED_STOCK_TYPE": "首发原股东限售股份",
            "FREE_SHARES_NUM": 999,
            "FREE_RATIO": "1.0%",
        }
    ]
    calls = {"n": 0}

    def alternating(*a, **k):
        calls["n"] += 1
        if calls["n"] <= 1:
            return FakeResp(_ok_response(rows))
        raise TimeoutError("calendar query timeout")

    monkeypatch.setattr(a_stock, "_em_get", alternating)

    out = a_stock.get_lockup_expiry("600519", "2026-09-04")

    # 已确认的历史记录保留
    assert "2025-06-01" in out, "部分失败不能让已成功的数据消失"
    # 失败部分单独标注，而不是宣称完整
    assert "[数据缺失" in out
    assert "无待解禁" not in out


# ---------------------------------------------------------------------------
# 龙虎榜：同样不得把失败解释成"未上榜"
# ---------------------------------------------------------------------------


def test_dragon_tiger_failure_shows_data_missing(isolated_cache, no_retry_sleep, monkeypatch):
    def boom(*a, **k):
        raise TimeoutError("connect timeout")

    monkeypatch.setattr(a_stock, "_em_get", boom)

    out = a_stock.get_dragon_tiger_board("000858", "2026-09-04")

    assert "[数据缺失" in out, f"龙虎榜查询失败必须显式标注，实际输出:\n{out}"
    assert "未上龙虎榜" not in out, "接口故障被解释成了『未上榜』"


def test_dragon_tiger_success_no_records_says_not_listed(isolated_cache, monkeypatch):
    monkeypatch.setattr(
        a_stock, "_em_get", lambda *a, **k: FakeResp(_empty_ok_response())
    )

    out = a_stock.get_dragon_tiger_board("000858", "2026-09-04")

    assert "未上龙虎榜" in out
    assert "[数据缺失" not in out


def test_dragon_tiger_success_with_rows_renders(isolated_cache, monkeypatch):
    rows = [
        {
            "TRADE_DATE": "2026-09-01 00:00:00",
            "EXPLANATION": "日涨幅偏离值达7%的证券",
            "BILLBOARD_NET_AMT": 56000000,
            "TURNOVERRATE": "3.21",
        }
    ]
    monkeypatch.setattr(a_stock, "_em_get", lambda *a, **k: FakeResp(_ok_response(rows)))

    out = a_stock.get_dragon_tiger_board("000858", "2026-09-04")

    assert "2026-09-01" in out
    assert "日涨幅偏离值" in out
    assert "[数据缺失" not in out


# ---------------------------------------------------------------------------
# 质量检查能识别缺失提示
# ---------------------------------------------------------------------------


def test_quality_gate_flags_missing_data_marker():
    from tradingagents.agents.quality_gate import _hard_check_report

    report = (
        "【解禁监控报告】" + "详细内容" * 60
        + "\n\n[数据缺失: 解禁查询失败]\n\n| 事项 | 说明 |\n|---|---|\n| x | y |"
    )
    grade, detail = _hard_check_report("lockup", report)
    assert grade in ("B", "C"), (
        f"含 [数据缺失] 标记的报告不应评 A，实际 {grade}: {detail}"
    )
    assert "数据缺失" in detail


# ---------------------------------------------------------------------------
# F1：新浪财报从真实 HTTP 边界的失败/空数据语义
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_sina_business_error_propagates_not_empty(monkeypatch, tmp_path):
    """F1：新浪业务失败（status.code != 0）从真实 HTTP 边界传播为失败输出。"""
    monkeypatch.setattr(cache_utils, "_MEM_CACHE", TTLCache(maxsize=100, default_ttl=60))
    monkeypatch.setattr(
        cache_utils, "_DISK_CACHE", DiskCache(cache_dir=str(tmp_path / "dc"))
    )

    class BizError:
        def raise_for_status(self):
            return None

        def json(self):
            return {"result": {"status": {"code": 1002, "msg": "paperCode invalid"}}}

    monkeypatch.setattr(a_stock._requests, "get", lambda *a, **k: BizError())

    out = a_stock.get_balance_sheet("600519", curr_date="2026-09-04")

    assert out.startswith("Error retrieving balance sheet"), out
    assert "No " not in out, "业务失败不得伪装成成功空数据"


@pytest.mark.integration
def test_sina_success_empty_report_is_legitimate_empty(monkeypatch, tmp_path):
    """F1：确认成功但无报表（code=0 无条目）时，"No data" 是合法空结果且可缓存。"""
    monkeypatch.setattr(cache_utils, "_MEM_CACHE", TTLCache(maxsize=100, default_ttl=60))
    monkeypatch.setattr(
        cache_utils, "_DISK_CACHE", DiskCache(cache_dir=str(tmp_path / "dc"))
    )

    calls = {"n": 0}

    class OkEmpty:
        def raise_for_status(self):
            return None

        def json(self):
            calls["n"] += 1
            return {"result": {"status": {"code": 0}, "data": {"fzb": []}}}

    monkeypatch.setattr(a_stock._requests, "get", lambda *a, **k: OkEmpty())

    first = a_stock.get_balance_sheet("600519", curr_date="2026-09-04")
    second = a_stock.get_balance_sheet("600519", curr_date="2026-09-04")

    assert "No balance sheet data found" in first
    assert calls["n"] == 1, "合法空结果应命中缓存（与故障输出行为不同）"
    assert second == first
