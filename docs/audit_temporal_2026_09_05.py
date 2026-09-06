"""Independent temporal boundary checks; offline and outside default testpaths."""
from unittest.mock import Mock

import pandas as pd
import pytest
import requests

from tradingagents import market_flow
from tradingagents.dataflows import a_stock, cache_utils
from tradingagents.graph.trading_graph import TradingAgentsGraph


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(requests.Session, "request", Mock(side_effect=AssertionError("Unexpected network")))
    from tradingagents.graph import trading_graph
    monkeypatch.setattr(trading_graph.yf, "Ticker", Mock(side_effect=AssertionError("Unexpected Yahoo network")))
    monkeypatch.setattr(cache_utils, "_MEM_CACHE", cache_utils.TTLCache())
    monkeypatch.setattr(cache_utils, "_DISK_CACHE", cache_utils.DiskCache(str(tmp_path)))
    from tradingagents.dataflows import config as data_config
    from tradingagents.default_config import DEFAULT_CONFIG
    monkeypatch.setattr(data_config, "_config", {**DEFAULT_CONFIG, "data_cache_dir": str(tmp_path / "scoped-cache")})
    if hasattr(cache_utils, "_SCOPE_DISK_CACHES"):
        monkeypatch.setattr(cache_utils, "_SCOPE_DISK_CACHES", {})
    run_var = getattr(data_config, "_run_config", None)
    token = run_var.set(None) if run_var is not None else None
    try:
        yield
    finally:
        if run_var is not None:
            run_var.reset(token)


@pytest.mark.parametrize("publication", [None, "invalid", "2026-08-30"])
def test_financial_unknown_or_future_publication_is_unavailable(monkeypatch, publication):
    row = {"报告日": "2026-06-30", "测试金额": 999999}
    if publication is not None:
        row["公告日期"] = publication
    response = Mock()
    response.json.return_value = {"result": {"status": {"code": 0}, "data": {"lrb": [row]}}}
    monkeypatch.setattr(a_stock._requests, "get", Mock(return_value=response))
    result = a_stock._get_financial_report_sina("600519", "利润表", "quarterly", "2026-07-10")
    assert result.empty, result.to_dict("records")


def test_financial_known_published_row_is_available(monkeypatch):
    response = Mock()
    response.json.return_value = {"result": {"status": {"code": 0}, "data": {"lrb": [
        {"报告日": "2026-06-30", "公告日期": "2026-08-30", "测试金额": 999999}
    ]}}}
    monkeypatch.setattr(a_stock._requests, "get", Mock(return_value=response))
    result = a_stock._get_financial_report_sina("600519", "利润表", "quarterly", "2026-08-31")
    assert len(result) == 1 and result.iloc[0]["测试金额"] == 999999


def test_old_financial_disk_cache_does_not_bypass_asof(monkeypatch):
    key = "get_income_statement:('600519',):[('curr_date', '2026-07-10')]"
    cache_utils._DISK_CACHE.set("financials-v3", key, "POISON_FUTURE_FINANCIALS")
    if hasattr(cache_utils, "_cache_scope"):
        scope = cache_utils._cache_scope()
        cache_utils._disk_cache_for(scope).set("financials-v3", f"{scope}|{key}", "POISON_FUTURE_FINANCIALS")
    monkeypatch.setattr(a_stock, "_get_financial_report_sina", Mock(return_value=pd.DataFrame()))
    result = a_stock.get_income_statement("600519", curr_date="2026-07-10")
    assert "POISON_FUTURE_FINANCIALS" not in result


def test_fundflow_aggregate_does_not_leak_current_sector_rank(monkeypatch):
    from tradingagents.dataflows import index_data
    for name in ("get_index_fund_flow", "get_index_news"):
        monkeypatch.setattr(index_data, name, lambda *a, **kw: "offline historical data")
    for name in ("get_northbound_flow", "get_hot_stocks"):
        monkeypatch.setattr(a_stock, name, lambda *a, **kw: "offline historical data")
    response = Mock()
    response.json.return_value = {"data": {"diff": [
        {"f14": "FUTURE_SECTOR_SENTINEL", "f62": 123456789, "f3": 88, "f184": 99}
    ]}}
    monkeypatch.setattr(a_stock, "_em_get", Mock(return_value=response))
    result = market_flow.gather_market_flow_data("000001.SH", "2026-01-15")
    assert "FUTURE_SECTOR_SENTINEL" not in result or "实时快照" in result


def test_yahoo_returns_use_matching_calendar_endpoints(monkeypatch):
    from tradingagents.graph import trading_graph
    stock = pd.DataFrame({"Close": [100, 110]}, index=pd.to_datetime(["2026-01-05", "2026-01-08"]))
    bench = pd.DataFrame({"Close": [100, 101, 120, 130]}, index=pd.to_datetime(
        ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]))
    monkeypatch.setattr(trading_graph.yf, "Ticker", lambda symbol: Mock(history=Mock(return_value=stock if symbol == "AAPL" else bench)))
    result = TradingAgentsGraph.__new__(TradingAgentsGraph)._fetch_returns("AAPL", "2026-01-05", holding_days=1)
    assert result == pytest.approx((.1, -.2, 1, "2026-01-08"))


def test_northbound_filters_before_selecting_last_twenty(monkeypatch, tmp_path):
    path = tmp_path / "northbound.csv"
    dates = pd.date_range("2026-01-01", periods=60)
    path.write_text("date,hgt,sgt\n" + "".join(
        f"{day:%Y-%m-%d},{i + 1},0\n" for i, day in enumerate(dates)))
    monkeypatch.setattr(a_stock, "_northbound_cache_path", lambda: str(path))
    result = a_stock.get_northbound_flow("2026-01-15", include_history=True)
    assert "2026-01-15: HGT=15.00" in result
    assert "15-day avg net flow: 8.00" in result


@pytest.mark.parametrize("row", [
    {"公告日期": "2026-08-30", "测试金额": 999999},
    {"报告日": "2026-03-31", "公告日期": "2026-04-30", "修订日期": "2026-08-30", "测试金额": 999999},
    {"报告日": "2026-03-31", "公告日期": "2026-04-30", "修订日期": "invalid", "测试金额": 999999},
])
def test_missing_period_or_later_revision_cannot_bypass_filter(monkeypatch, row):
    response = Mock()
    response.json.return_value = {"result": {"status": {"code": 0}, "data": {"lrb": [row]}}}
    monkeypatch.setattr(a_stock._requests, "get", Mock(return_value=response))
    result = a_stock._get_financial_report_sina("600519", "利润表", "quarterly", "2026-07-10")
    assert result.empty, result.to_dict("records")


def test_financial_latest_eight_periods_are_retained(monkeypatch):
    response = Mock()
    response.json.return_value = {"result": {"status": {"code": 0}, "data": {"lrb": [
        {"报告日": f"{year}-12-31", "公告日期": f"{year + 1}-04-30", "测试金额": year}
        for year in reversed(range(2010, 2026))
    ]}}}
    monkeypatch.setattr(a_stock._requests, "get", Mock(return_value=response))
    result = a_stock._get_financial_report_sina("600519", "利润表", "annual", "2026-08-31")
    assert set(result["测试金额"]) == set(range(2018, 2026))


def test_yahoo_alignment_uses_local_dates_across_timezones(monkeypatch):
    from tradingagents.graph import trading_graph
    stock = pd.DataFrame({"Close": [100, 110]}, index=pd.to_datetime(
        ["2026-01-05", "2026-01-06"]).tz_localize("America/New_York"))
    bench = pd.DataFrame({"Close": [100, 130]}, index=pd.to_datetime(
        ["2026-01-05", "2026-01-06"]).tz_localize("Asia/Shanghai"))
    monkeypatch.setattr(trading_graph.yf, "Ticker", lambda symbol: Mock(history=Mock(return_value=stock if symbol == "AAPL" else bench)))
    result = TradingAgentsGraph.__new__(TradingAgentsGraph)._fetch_returns("AAPL", "2026-01-05", holding_days=1)
    assert result == pytest.approx((.1, -.2, 1, "2026-01-06"))


@pytest.mark.parametrize("resume_month", ["02", "07"])
def test_long_suspension_eventually_resolves_full_window(monkeypatch, resume_month):
    from tradingagents.dataflows import index_data
    days = ["2026-01-05"] + [f"2026-{resume_month}-{day}" for day in range(16, 21)]
    prices = pd.DataFrame({"Date": pd.to_datetime(days), "Close": [100, 101, 102, 103, 104, 105]})
    def source(*a, start_date, end_date):
        return prices[(prices.Date >= pd.Timestamp(start_date)) & (prices.Date <= pd.Timestamp(end_date))]
    monkeypatch.setattr(a_stock, "get_astock_history_df", source)
    monkeypatch.setattr(index_data, "get_index_history_df", source)
    result = TradingAgentsGraph.__new__(TradingAgentsGraph)._fetch_returns("600519", "2026-01-05")
    assert result == pytest.approx((.05, 0, 5, f"2026-{resume_month}-20"))
