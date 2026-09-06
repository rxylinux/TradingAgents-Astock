"""第 2 批时点边界回归（A03/A04/A07/A08）。

全部离线：HTTP 经 mock 替身（含 `yf.Ticker` —— yfinance 不走
requests.Session.request，只 patch requests 拦不住它）；持久化路径指向
tmp。等价断言与 docs/audit_repros_2026_09_05.py、docs/audit_temporal_
2026_09_05.py 对齐并补充额外边界，供默认测试集长期守护。
"""

from unittest.mock import Mock, patch

import pandas as pd
import pytest

from tradingagents import market_flow
from tradingagents.dataflows import a_stock, cache_utils
from tradingagents.graph.trading_graph import TradingAgentsGraph


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(cache_utils, "_MEM_CACHE", cache_utils.TTLCache())
    monkeypatch.setattr(cache_utils, "_DISK_CACHE", cache_utils.DiskCache(str(tmp_path / "dc")))


# ===========================================================================
# A03：北向 / 行业 / 资金流报告的历史时点
# ===========================================================================


class TestNorthboundTemporal:
    def _cache(self, tmp_path, monkeypatch, days=60):
        path = tmp_path / "northbound.csv"
        dates = pd.date_range("2026-01-01", periods=days)
        path.write_text(
            "date,hgt,sgt\n"
            + "".join(f"{d:%Y-%m-%d},{i + 1},0\n" for i, d in enumerate(dates)),
            encoding="utf-8",
        )
        monkeypatch.setattr(a_stock, "_northbound_cache_path", lambda: str(path))

    def test_historical_skips_realtime_and_filters_cache(self, tmp_path, monkeypatch):
        """历史请求：不取实时分钟段，缓存先按分析日过滤再取最近 20 日。"""
        self._cache(tmp_path, monkeypatch, days=60)
        # 实时请求若被发起则炸（历史模式必须完全跳过）
        monkeypatch.setattr(
            a_stock._requests, "get",
            Mock(side_effect=AssertionError("realtime must not be fetched")),
        )

        out = a_stock.get_northbound_flow("2026-01-15", include_history=True)

        assert "2026-01-15: HGT=15.00" in out, "分析日当日的缓存行丢失"
        assert "15-day avg net flow: 8.00" in out, "均值被未来行污染或窗口截错"
        assert "2026-01-16" not in out, "分析日之后的缓存行泄漏"

    def test_historical_without_cache_says_missing_not_zero(self, tmp_path, monkeypatch):
        """无该分析日及之前的缓存 → 明确缺失，不是『净流入为零』。"""
        self._cache(tmp_path, monkeypatch, days=5)  # 1/1-1/5，分析 1/15 前无未来行？日期 1/1-1/5 <= 1/15 → 有数据
        # 改用「缓存全部晚于分析日」的场景
        path = tmp_path / "northbound.csv"
        dates = pd.date_range("2026-02-01", periods=5)
        path.write_text(
            "date,hgt,sgt\n"
            + "".join(f"{d:%Y-%m-%d},1,0\n" for d in dates),
            encoding="utf-8",
        )
        monkeypatch.setattr(a_stock, "_northbound_cache_path", lambda: str(path))

        out = a_stock.get_northbound_flow("2026-01-15", include_history=True)

        assert "缺失" in out or "没有" in out
        assert "avg net flow" not in out

    def test_current_date_keeps_realtime(self, tmp_path, monkeypatch):
        """当前日期请求保持合法实时段与快照写入。"""
        self._cache(tmp_path, monkeypatch, days=3)
        response = Mock()
        response.json.return_value = {"time": ["15:00"], "hgt": [123.0], "sgt": [1.0]}
        monkeypatch.setattr(a_stock._requests, "get", Mock(return_value=response))
        monkeypatch.setattr(a_stock, "_save_northbound_snapshot", Mock())

        out = a_stock.get_northbound_flow("2099-01-01", include_history=True)

        assert "123.00" in out, "当前请求的实时数据不应被防护误伤"


class TestIndustryTemporal:
    def test_historical_returns_missing_not_realtime(self, monkeypatch):
        """历史请求：不请求实时排名，返回明确缺失 + 禁止引用说明。"""
        monkeypatch.setattr(
            a_stock, "_em_get",
            Mock(side_effect=AssertionError("realtime rank must not be fetched")),
        )

        out = a_stock.get_industry_comparison("600519", "2026-01-15")

        assert "缺失" in out
        assert "实时快照" in out and "不得" in out
        assert "88%" not in out

    def test_current_date_fetches_rank(self, monkeypatch):
        response = Mock()
        response.json.return_value = {"data": {"diff": [{"f14": "白酒", "f3": 2}]}}
        response.raise_for_status = lambda: None
        monkeypatch.setattr(a_stock, "_em_get", Mock(return_value=response))

        out = a_stock.get_industry_comparison("600519", "2099-01-01")

        assert "白酒" in out


class TestFundflowReportTemporal:
    def test_historical_gather_marks_sector_rank_snapshot(self, monkeypatch):
        """资金流报告：历史日期的行业/概念排名带禁止引用的快照警告。"""
        from tradingagents.dataflows import index_data

        for name in ("get_index_fund_flow", "get_index_news"):
            monkeypatch.setattr(index_data, name, lambda *a, **kw: "offline")
        for name in ("get_northbound_flow", "get_hot_stocks"):
            monkeypatch.setattr(a_stock, name, lambda *a, **kw: "offline")
        response = Mock()
        response.json.return_value = {"data": {"diff": [
            {"f14": "FUTURE_SECTOR", "f62": 1, "f3": 88, "f184": 9}
        ]}}
        monkeypatch.setattr(a_stock, "_em_get", Mock(return_value=response))

        out = market_flow.gather_market_flow_data("000001.SH", "2026-01-15")

        assert "FUTURE_SECTOR" not in out or "实时快照" in out
        assert "不得" in out


# ===========================================================================
# A04：财报披露/修订可知时间
# ===========================================================================


def _sina_response(monkeypatch, rows, source="lrb"):
    response = Mock()
    response.json.return_value = {
        "result": {"status": {"code": 0}, "data": {source: rows}}
    }
    monkeypatch.setattr(a_stock._requests, "get", Mock(return_value=response))


class TestFinancialPublicationTemporal:
    @pytest.mark.parametrize(
        "row",
        [
            {"报告日": "2026-06-30", "测试金额": 1},                     # 无披露字段
            {"报告日": "2026-06-30", "公告日期": "invalid", "测试金额": 1},  # 无效披露
            {"报告日": "2026-06-30", "公告日期": "2026-08-30", "测试金额": 1},  # 未来披露
            {"公告日期": "2026-08-30", "测试金额": 1},                    # 无报告期仍有披露 → 不可证
            {"报告日": "2026-03-31", "公告日期": "2026-04-30", "修订日期": "2026-08-30", "测试金额": 1},
            {"报告日": "2026-03-31", "公告日期": "2026-04-30", "修订日期": "invalid", "测试金额": 1},
        ],
        ids=["no_pub", "invalid_pub", "future_pub", "no_period", "later_revision", "invalid_revision"],
    )
    def test_unprovable_rows_are_empty(self, monkeypatch, row):
        _sina_response(monkeypatch, [row])
        result = a_stock._get_financial_report_sina(
            "600519", "利润表", "quarterly", "2026-07-10"
        )
        assert result.empty, result.to_dict("records")

    def test_published_row_visible_after_publication(self, monkeypatch):
        _sina_response(monkeypatch, [
            {"报告日": "2026-06-30", "公告日期": "2026-08-30", "测试金额": 999999}
        ])
        result = a_stock._get_financial_report_sina(
            "600519", "利润表", "quarterly", "2026-08-31"
        )
        assert len(result) == 1
        assert result.iloc[0]["测试金额"] == 999999

    def test_revision_visible_only_after_revision_date(self, monkeypatch):
        """修订版本在修订日之后才整体可见（可知时间 = max(披露, 修订)）。"""
        _sina_response(monkeypatch, [
            {"报告日": "2026-03-31", "公告日期": "2026-04-30",
             "修订日期": "2026-08-30", "测试金额": 1}
        ])
        before = a_stock._get_financial_report_sina(
            "600519", "利润表", "quarterly", "2026-07-10"
        )
        after = a_stock._get_financial_report_sina(
            "600519", "利润表", "quarterly", "2026-09-01"
        )
        assert before.empty
        assert len(after) == 1

    def test_latest_eight_periods_after_filters(self, monkeypatch):
        _sina_response(monkeypatch, [
            {"报告日": f"{y}-12-31", "公告日期": f"{y + 1}-04-30", "测试金额": y}
            for y in reversed(range(2010, 2026))
        ])
        result = a_stock._get_financial_report_sina(
            "600519", "利润表", "annual", "2026-08-31"
        )
        assert set(result["测试金额"]) == set(range(2018, 2026))

    def test_empty_after_filter_explains_reason(self, monkeypatch):
        _sina_response(monkeypatch, [
            {"报告日": "2026-06-30", "公告日期": "2026-08-30", "测试金额": 1}
        ])
        result = a_stock._get_financial_report_sina(
            "600519", "利润表", "quarterly", "2026-07-10"
        )
        assert result.empty
        assert result.attrs.get("missing_reason"), "过滤后为空须给出可知性缺失原因"

        # 工具层把它转成显式缺失（不缓存），而非 "No data" 成功空结果
        out = a_stock.get_income_statement("600519", curr_date="2026-07-10")
        assert out.startswith("[数据缺失")

    def test_no_publication_field_historical_returns_missing(self, monkeypatch):
        """源不提供任何披露/修订字段：历史模式整体缺失（不返回实时快照）。"""
        _sina_response(monkeypatch, [
            {"报告日": "2026-06-30", "测试金额": 1}
        ])
        result = a_stock._get_financial_report_sina(
            "600519", "利润表", "quarterly", "2026-07-10"
        )
        assert result.empty
        assert "未提供披露" in (result.attrs.get("missing_reason") or "")

    def test_v3_disk_cache_cannot_bypass_asof(self, monkeypatch):
        """v3 旧缓存（未按披露过滤的输出）不得绕过新过滤命中。"""
        key = "get_income_statement:('600519',):[('curr_date', '2026-07-10')]"
        cache_utils._DISK_CACHE.set("financials-v3", key, "POISON_FUTURE_FINANCIALS")
        monkeypatch.setattr(
            a_stock, "_get_financial_report_sina", Mock(return_value=pd.DataFrame())
        )

        result = a_stock.get_income_statement("600519", curr_date="2026-07-10")

        assert "POISON_FUTURE_FINANCIALS" not in result


# ===========================================================================
# A07/A08：收益评估窗口
# ===========================================================================


def _frame(dates, closes, tz=None):
    idx = pd.to_datetime(dates)
    if tz is not None:
        idx = idx.tz_localize(tz)
    return pd.DataFrame({"Close": closes}, index=idx)


class TestAlphaDateAlignment:
    def test_native_alpha_uses_identical_dates(self, monkeypatch):
        """停牌窗口：股票 1/5→1/8，基准必须取相同日期（alpha=-20%）。"""
        stock = pd.DataFrame({
            "Date": pd.to_datetime(["2026-01-05", "2026-01-08"]),
            "Close": [100.0, 110.0],
        })
        bench = pd.DataFrame({
            "Date": pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]),
            "Close": [100.0, 101.0, 120.0, 130.0],
        })
        monkeypatch.setattr(a_stock, "get_astock_history_df", lambda *a, **kw: stock)
        monkeypatch.setattr(index_data := __import__(
            "tradingagents.dataflows.index_data", fromlist=["x"],
        ), "get_index_history_df", lambda *a, **kw: bench)

        graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
        raw, alpha, days, end = graph._fetch_returns("600519", "2026-01-05", holding_days=1)

        assert raw == pytest.approx(0.1)
        assert alpha == pytest.approx(-0.2), "基准取了不同日期（行号对齐错误）"
        assert (days, end) == (1, "2026-01-08")

    def test_yahoo_alpha_uses_matching_calendar_endpoints(self, monkeypatch):
        from tradingagents.graph import trading_graph

        stock = _frame(["2026-01-05", "2026-01-08"], [100.0, 110.0])
        bench = _frame(
            ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"],
            [100.0, 101.0, 120.0, 130.0],
        )
        monkeypatch.setattr(
            trading_graph.yf, "Ticker",
            lambda symbol: Mock(history=Mock(return_value=stock if symbol == "AAPL" else bench)),
        )

        graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
        raw, alpha, days, end = graph._fetch_returns("AAPL", "2026-01-05", holding_days=1)

        assert (raw, alpha, days, end) == pytest.approx((0.1, -0.2, 1, "2026-01-08"))

    def test_yahoo_alignment_uses_local_dates_across_timezones(self, monkeypatch):
        from tradingagents.graph import trading_graph

        stock = _frame(["2026-01-05", "2026-01-06"], [100, 110], tz="America/New_York")
        bench = _frame(["2026-01-05", "2026-01-06"], [100, 130], tz="Asia/Shanghai")
        monkeypatch.setattr(
            trading_graph.yf, "Ticker",
            lambda symbol: Mock(history=Mock(return_value=stock if symbol == "AAPL" else bench)),
        )

        graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
        result = graph._fetch_returns("AAPL", "2026-01-05", holding_days=1)

        assert result == pytest.approx((0.1, -0.2, 1, "2026-01-06"))

    def test_missing_benchmark_endpoint_keeps_pending(self, monkeypatch):
        """基准缺结束日端点 → 全 None（不静默换日期比较）。"""
        stock = pd.DataFrame({
            "Date": pd.date_range("2026-01-05", periods=6),
            "Close": [100.0, 102.0, 104.0, 103.0, 105.0, 106.0],
        })
        bench = pd.DataFrame({
            "Date": pd.date_range("2026-01-05", periods=3),
            "Close": [4000.0, 4020.0, 4030.0],
        })
        monkeypatch.setattr(a_stock, "get_astock_history_df", lambda *a, **kw: stock)
        from tradingagents.dataflows import index_data

        monkeypatch.setattr(index_data, "get_index_history_df", lambda *a, **kw: bench)

        graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
        result = graph._fetch_returns("600519", "2026-01-05")
        assert result == (None, None, None, None)


class TestFullOutcomeWindow:
    def test_short_data_keeps_entry_pending(self, monkeypatch, tmp_path):
        """窗口不满（仅 1 个交易日）→ 不结算，保持 pending。"""
        from tradingagents.agents.utils.memory import TradingMemoryLog

        prices = pd.DataFrame({
            "Date": pd.to_datetime(["2026-01-05", "2026-01-06"]),
            "Close": [100.0, 110.0],
        })
        monkeypatch.setattr(a_stock, "get_astock_history_df", lambda *a, **kw: prices)
        from tradingagents.dataflows import index_data

        monkeypatch.setattr(index_data, "get_index_history_df", lambda *a, **kw: prices)

        graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
        graph.memory_log = TradingMemoryLog(
            {"memory_log_path": str(tmp_path / "memory.md")}
        )
        graph.memory_log.store_decision("600519", "2026-01-05", "Rating: Buy")
        graph.reflector = Mock()
        graph.reflector.reflect_on_final_decision.return_value = "must not be used"

        graph._resolve_pending_entries("600519")

        assert graph.memory_log.get_pending_entries(), "窗口未满却被定稿"
        assert "must not be used" not in (tmp_path / "memory.md").read_text(encoding="utf-8")

    def test_long_suspension_eventually_resolves_full_window(self, monkeypatch):
        """长假/长期停牌：取数窗口足够宽，满 5 个交易日后按实际日期结算。"""
        from tradingagents.dataflows import index_data

        days = ["2026-01-05", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20"]
        prices = pd.DataFrame({"Date": pd.to_datetime(days), "Close": [100, 101, 102, 103, 104, 105]})

        def source(*a, start_date, end_date):
            return prices[
                (prices.Date >= pd.Timestamp(start_date))
                & (prices.Date <= pd.Timestamp(end_date))
            ]

        monkeypatch.setattr(a_stock, "get_astock_history_df", source)
        monkeypatch.setattr(index_data, "get_index_history_df", source)

        graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
        result = graph._fetch_returns("600519", "2026-01-05")

        assert result == pytest.approx((0.05, 0.0, 5, "2026-02-20"))

    def test_find_short_window_outcomes_flags_legacy_resolutions(self, tmp_path):
        """诊断（只读）：识别旧版被 min() 缩短结算的条目，不改日志。"""
        from tradingagents.agents.utils.memory import TradingMemoryLog

        log = TradingMemoryLog({"memory_log_path": str(tmp_path / "memory.md")})
        log.store_decision("600519", "2026-01-05", "Rating: Buy")
        log.update_with_outcome("600519", "2026-01-05", 0.1, 0.05, 1, "short window")
        log.store_decision("000001", "2026-01-05", "Rating: Sell")
        log.update_with_outcome("000001", "2026-01-05", -0.1, -0.05, 5, "full window")

        flagged = log.find_short_window_outcomes(min_effective_days=5)

        assert [e["ticker"] for e in flagged] == ["600519"]
        # 只读：日志未被修改
        entries = log.load_entries()
        assert len(entries) == 2
        assert not log.get_pending_entries()
