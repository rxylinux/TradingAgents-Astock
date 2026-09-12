"""Codex independent offline news audit; ZCode must not edit this file."""

from unittest.mock import patch

import pytest

from tradingagents.dataflows import a_stock


class Response:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data

    def raise_for_status(self):
        return None


def global_news(times_titles, limit=10):
    payload = {"data": {"fastNewsList": [
        {"title": title, "summary": "offline fixture", "showTime": time}
        for time, title in times_titles
    ]}}
    with patch.object(a_stock._requests, "get", return_value=Response({"data": {"roll_data": []}})), \
         patch.object(a_stock, "_em_get", return_value=Response(payload)):
        return a_stock.get_global_news("2026-01-01", look_back_days=0, limit=limit)


@pytest.mark.parametrize("time", ["2026-01-01T20:00:00Z", "2026-01-01T23:30:00-05:00"])
def test_offset_timestamp_after_shanghai_cutoff_is_excluded(time):
    assert "AFTER_CUTOFF" not in global_news([(time, "AFTER_CUTOFF")])


def test_previous_utc_date_inside_shanghai_day_is_included():
    assert "IN_SHANGHAI_DAY" in global_news([("2025-12-31T20:00:00Z", "IN_SHANGHAI_DAY")])


@pytest.mark.parametrize("time", ["2026-01-01garbage", "2026-01-01 99:99:99"])
def test_invalid_timestamp_suffix_cannot_be_silently_accepted(time):
    assert "INVALID_TIME" not in global_news([(time, "INVALID_TIME")])


def test_filter_precedes_dedup_and_limit():
    out = global_news([
        ("2099-01-01 10:00:00", "SAME_TITLE"),
        ("2099-01-01 10:00:00", "OTHER_FUTURE"),
        ("2026-01-01 10:00:00", "SAME_TITLE"),
        ("2026-01-01 11:00:00", "SECOND_VALID"),
    ], limit=2)
    assert "SAME_TITLE" in out and "SECOND_VALID" in out
    assert "OTHER_FUTURE" not in out and "2099" not in out


def test_publish_time_is_in_article_body_not_only_request_header():
    out = global_news([("2026-01-01 11:37:00", "TIMED_ARTICLE")])
    assert "11:37" in out


def test_stock_news_uses_same_timezone_cutoff():
    articles = [
        {"title": "FUTURE_STOCK", "time": "2026-01-01T20:00:00Z", "content": "c"},
        {"title": "VALID_STOCK", "time": "2025-12-31T20:00:00Z", "content": "c"},
    ]
    with patch.object(a_stock, "_fetch_news_eastmoney", return_value=articles):
        out = a_stock.get_news("600519", "2026-01-01", "2026-01-01")
    assert "FUTURE_STOCK" not in out and "VALID_STOCK" in out


def test_malformed_timezone_suffix_is_excluded():
    assert "MALFORMED_OFFSET" not in global_news([("2026-01-01T10:00:00+00:00garbage", "MALFORMED_OFFSET")])


def test_minute_precision_utc_timestamp_is_supported():
    assert "MINUTE_UTC" in global_news([("2025-12-31T20:00Z", "MINUTE_UTC")])


def test_stock_partial_source_failure_is_visible_with_valid_data():
    articles = [{"title": "VALID_ARTICLE", "time": "2026-01-01 11:37:00", "content": "c"}]
    with patch.object(a_stock, "_fetch_news_eastmoney", side_effect=TimeoutError("offline")), \
         patch.object(a_stock, "_fetch_news_sina", return_value=articles):
        out = a_stock.get_news("600519", "2026-01-01", "2026-01-01")
    assert "VALID_ARTICLE" in out
    assert any(term in out.lower() for term in ("失败", "不可用", "partial", "failed", "unavailable"))
    assert "11:37" in out


def test_global_partial_source_failure_is_visible_when_other_source_empty():
    with patch.object(a_stock._requests, "get", side_effect=TimeoutError("offline")), \
         patch.object(a_stock, "_em_get", return_value=Response({"data": {"fastNewsList": []}})):
        out = a_stock.get_global_news("2026-01-01")
    assert any(term in out.lower() for term in ("失败", "不可用", "partial", "failed", "unavailable"))
