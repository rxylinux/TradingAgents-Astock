"""Batch A: news temporal filtering + failure vs empty semantics (RED first).

All offline: HTTP via mock responses, no network/model/market calls.
"""

import pytest
from unittest.mock import Mock, patch

from tradingagents.dataflows import a_stock, cache_utils


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


# ===========================================================================
# A-1: get_global_news temporal filtering
# ===========================================================================


def _cls_item(title, ctime="1767230400", content="news content"):
    """CLS wire item: ctime is a unix timestamp (2026-01-01 00:00 UTC)."""
    return {"title": title, "brief": content, "content": content, "ctime": ctime}


def _em_item(title, show_time="2026-01-01 10:00:00", summary="em content"):
    return {"title": title, "summary": summary, "showTime": show_time}


def _mock_responses(cls_items, em_items):
    """Build a side_effect for _requests.get (CLS) and _em_get (Eastmoney)."""
    cls_payload = {"data": {"roll_data": cls_items}}
    em_payload = {"data": {"fastNewsList": em_items}}
    return cls_payload, em_payload


class TestGlobalNewsTemporalFiltering:
    """get_global_news must filter by analysis-time window BEFORE dedup/limit."""

    def _invoke(self, cls_items, em_items, curr_date="2026-01-01", **kw):
        cls_p, em_p = _mock_responses(cls_items, em_items)
        with patch.object(a_stock._requests, "get", return_value=FakeResp(cls_p)), \
             patch.object(a_stock, "_em_get", return_value=FakeResp(em_p)):
            return a_stock.get_global_news(curr_date, **kw)

    def test_future_news_excluded(self):
        """Records with time after curr_date must NOT appear in output."""
        out = self._invoke(
            cls_items=[_cls_item("PAST_NEWS", ctime="1767230400")],  # 2026-01-01
            em_items=[_em_item("FUTURE_EVENT", show_time="2099-01-01 10:00:00")],
            curr_date="2026-01-01",
        )
        assert "FUTURE_EVENT" not in out, f"未来新闻泄漏:\n{out}"
        assert "PAST_NEWS" in out

    def test_before_window_excluded(self):
        """Records before start_date (curr_date - look_back_days) excluded."""
        out = self._invoke(
            cls_items=[_cls_item("TOO_OLD", ctime="1600000000")],  # 2020-09
            em_items=[],
            curr_date="2026-01-01",
            look_back_days=7,
        )
        assert "TOO_OLD" not in out, f"窗口外旧新闻泄漏:\n{out}"

    def test_within_window_included(self):
        out = self._invoke(
            cls_items=[_cls_item("IN_WINDOW", ctime="1767230400")],  # 2026-01-01
            em_items=[_em_item("ALSO_IN", show_time="2025-12-30 09:00:00")],
            curr_date="2026-01-01",
            look_back_days=7,
        )
        assert "IN_WINDOW" in out
        assert "ALSO_IN" in out

    def test_missing_time_marked_unknown_and_excluded(self):
        """Records with missing/unparseable time are excluded from historical
        output and their exclusion is noted."""
        out = self._invoke(
            cls_items=[_cls_item("NO_TIME", ctime="")],
            em_items=[],
            curr_date="2026-01-01",
        )
        assert "NO_TIME" not in out, "缺失时间记录未被排除"
        assert "unknown" in out.lower() or "未知" in out or "excluded" in out.lower(), \
            "排除原因未说明"

    def test_invalid_time_excluded(self):
        out = self._invoke(
            cls_items=[_cls_item("BAD_TIME", ctime="not-a-timestamp")],
            em_items=[],
            curr_date="2026-01-01",
        )
        assert "BAD_TIME" not in out, "非法时间记录未被排除"

    def test_filter_before_limit(self):
        """Filter by time window FIRST, then apply limit — not limit first."""
        # 15 valid + 5 future; limit=10 → should get 10 valid, not 10 with future mixed in
        cls = [_cls_item(f"VALID_{i}", ctime="1767230400") for i in range(15)]
        em = [_em_item(f"FUTURE_{i}", show_time="2099-01-01 10:00:00") for i in range(5)]
        out = self._invoke(cls_items=cls, em_items=em, curr_date="2026-01-01", limit=10)
        future_count = out.count("FUTURE_")
        valid_count = out.count("VALID_")
        assert future_count == 0, f"先限量后过滤导致未来新闻进入: {future_count} 条 FUTURE"
        assert valid_count == 10, f"应取 10 条有效新闻，实际 {valid_count}"

    def test_output_includes_publish_time(self):
        """Output should include the original publish time for traceability."""
        out = self._invoke(
            cls_items=[_cls_item("TIMED_NEWS", ctime="1767230400")],
            em_items=[],
            curr_date="2026-01-01",
        )
        assert "TIMED_NEWS" in out
        # Should contain some form of the timestamp (not stripped entirely)
        assert "2026" in out, "输出缺少发布时间"

    def test_empty_result_with_coverage_note(self):
        """When all records are outside the window, return coverage note."""
        out = self._invoke(
            cls_items=[_cls_item("FUTURE_ONLY", ctime="4070908800")],  # 2099
            em_items=[],
            curr_date="2026-01-01",
        )
        assert "FUTURE_ONLY" not in out
        # Should NOT say "No global news found" as if nothing exists —
        # it should say the historical coverage was insufficient
        assert "覆盖不足" in out or "coverage" in out.lower() or \
            "历史区间" in out or "outside" in out.lower() or "窗口" in out

    def test_timezone_normalized_cls_unix(self):
        """CLS ctime is unix UTC → should be converted to local time correctly."""
        # 1767230400 = 2026-01-01 00:00 UTC = 2026-01-01 08:00 Shanghai
        out = self._invoke(
            cls_items=[_cls_item("TZ_NEWS", ctime="1767230400")],
            em_items=[],
            curr_date="2026-01-01",
        )
        assert "TZ_NEWS" in out  # within 2026-01-01 Shanghai

    def test_all_sources_fail_is_data_failure(self):
        """Both sources failing → DataFailure, not "No news found"."""
        with patch.object(a_stock._requests, "get", side_effect=TimeoutError("t")), \
             patch.object(a_stock, "_em_get", side_effect=TimeoutError("t")):
            out = a_stock.get_global_news("2026-01-01")
        assert cache_utils._is_failure_output(out) or "[数据缺失" in out, \
            f"双源失败被伪装为空结果: {out}"


# ===========================================================================
# A-2: get_news failure vs empty + invalid dates
# ===========================================================================


class TestStockNewsFailureVsEmpty:
    """get_news must distinguish request failure from success-no-records."""

    def _invoke_news(self, em_articles=None, em_exc=None, sina_articles=None, sina_exc=None):
        """Mock _fetch_news_eastmoney and _fetch_news_sina."""
        def em_side(*a, **kw):
            if em_exc:
                raise em_exc
            return em_articles or []

        def sina_side(*a, **kw):
            if sina_exc:
                raise sina_exc
            return sina_articles or []

        with patch.object(a_stock, "_fetch_news_eastmoney", side_effect=em_side), \
             patch.object(a_stock, "_fetch_news_sina", side_effect=sina_side):
            return a_stock.get_news("600519", "2025-12-25", "2026-01-01")

    def test_both_fail_is_failure_not_empty(self):
        """EM + Sina both timeout → must be a failure, not 'No news found'."""
        out = self._invoke_news(em_exc=TimeoutError("t"), sina_exc=TimeoutError("t"))
        assert cache_utils._is_failure_output(out) or "[数据缺失" in out, \
            f"双源失败被输出为成功空结果: {out}"
        assert "No news found" not in out, "失败不能说'无新闻'"

    def test_both_success_empty_is_ok(self):
        """Both succeed but return no articles → success empty."""
        out = self._invoke_news(em_articles=[], sina_articles=[])
        assert "No news found" in out or "无新闻" in out, \
            f"合法空结果应输出'无新闻': {out}"
        assert not cache_utils._is_failure_output(out), "成功空结果不是失败"

    def test_primary_fail_fallback_success(self):
        """EM fails, Sina succeeds → use Sina data."""
        sina = [{"title": "SINA_NEWS", "time": "2025-12-28 10:00", "content": "c", "url": ""}]
        out = self._invoke_news(em_exc=TimeoutError("t"), sina_articles=sina)
        assert "SINA_NEWS" in out

    def test_primary_success_empty_fallback_fail(self):
        """EM returns empty, Sina fails → success empty (primary succeeded)."""
        out = self._invoke_news(em_articles=[], sina_exc=TimeoutError("t"))
        # EM succeeded with 0 records, Sina as fallback also tried and failed
        # At minimum it should NOT be a DataFailure (EM succeeded)
        # But since EM returned empty and Sina failed, we have partial info
        # The important thing: not a full failure marker
        assert "[数据缺失" not in out, \
            f"主源成功空结果不应标为数据缺失: {out}"

    def test_invalid_date_record_excluded(self):
        """Records with unparseable time must be excluded from historical output."""
        articles = [
            {"title": "BAD_DATE_NEWS", "time": "bad-date", "content": "c", "url": ""},
            {"title": "GOOD_NEWS", "time": "2025-12-28 10:00", "content": "c", "url": ""},
        ]
        out = self._invoke_news(em_articles=articles)
        assert "BAD_DATE_NEWS" not in out, "非法时间记录未被排除"
        assert "GOOD_NEWS" in out

    def test_future_record_excluded(self):
        """Records dated after end_date must be excluded."""
        articles = [
            {"title": "FUTURE_NEWS", "time": "2099-06-01 10:00", "content": "c", "url": ""},
            {"title": "WINDOW_NEWS", "time": "2025-12-28 10:00", "content": "c", "url": ""},
        ]
        out = self._invoke_news(em_articles=articles)
        assert "FUTURE_NEWS" not in out
        assert "WINDOW_NEWS" in out

    def test_exclusion_noted_when_records_dropped(self):
        """When records are dropped for bad/future time, the exclusion is noted."""
        articles = [
            {"title": "BAD_TIME", "time": "xyz", "content": "c", "url": ""},
            {"title": "FUTURE", "time": "2099-01-01 10:00", "content": "c", "url": ""},
            {"title": "VALID", "time": "2025-12-28 10:00", "content": "c", "url": ""},
        ]
        out = self._invoke_news(em_articles=articles)
        assert "VALID" in out
        # Should mention that some records were excluded
        # (not silently drop them)
        assert "排除" in out or "excluded" in out.lower() or "无效" in out, \
            "静默排除了记录而未说明"


# ===========================================================================
# A-1: Real tool routing (formal entry point)
# ===========================================================================


class TestRealToolRouting:
    """Verify via the formal @tool → invoke() entry, not just direct call."""

    def test_global_news_via_tool_invoke(self):
        """get_global_news must work through the @tool boundary."""
        from tradingagents.agents.utils.news_data_tools import get_global_news as tool_get_global_news
        cls_p = {"data": {"roll_data": [
            {"title": "TOOL_PAST", "brief": "c", "content": "c", "ctime": "1767230400"},
        ]}}
        em_p = {"data": {"fastNewsList": [
            {"title": "TOOL_FUTURE", "summary": "c", "showTime": "2099-01-01 10:00:00"},
        ]}}
        with patch.object(a_stock._requests, "get", return_value=FakeResp(cls_p)), \
             patch.object(a_stock, "_em_get", return_value=FakeResp(em_p)):
            result = tool_get_global_news.invoke({"curr_date": "2026-01-01"})
        assert "TOOL_PAST" in result
        assert "TOOL_FUTURE" not in result, \
            f"工具入口未过滤未来新闻:\n{result}"

    def test_stock_news_via_tool_invoke(self):
        """get_news must work through the @tool boundary."""
        from tradingagents.agents.utils.news_data_tools import get_news as tool_get_news
        articles = [{"title": "TOOL_NEWS", "time": "2025-12-28 10:00", "content": "c", "url": ""}]
        with patch.object(a_stock, "_fetch_news_eastmoney", return_value=articles):
            result = tool_get_news.invoke({
                "ticker": "600519", "start_date": "2025-12-25", "end_date": "2026-01-01"
            })
        assert "TOOL_NEWS" in result
