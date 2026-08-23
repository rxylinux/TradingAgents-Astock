"""大盘资金流报告（market_flow）与 Web 指数接线的测试。"""

import pandas as pd
import pytest

from tradingagents import market_flow


# ---------------------------------------------------------------------------
# 板块资金流排名（fake _em_get，字段按 2026-08-16 实测）
# ---------------------------------------------------------------------------


class _FakeEMResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_sector_fund_flow_rank_formats_industry(monkeypatch):
    from tradingagents.dataflows import a_stock

    captured = {}

    def _fake_em_get(url, params=None, **kwargs):
        captured["fs"] = params["fs"]
        return _FakeEMResponse({
            "data": {
                "diff": [
                    {"f12": "BK0475", "f14": "银行", "f3": 1.2, "f62": 5.0e9, "f184": 2.5, "f204": "工商银行"},
                    {"f12": "BK0420", "f14": "航空机场", "f3": -0.8, "f62": -4.2e7, "f184": -1.85, "f204": "中国国航"},
                ]
            }
        })

    monkeypatch.setattr(a_stock, "_em_get", _fake_em_get)
    result = market_flow.get_sector_fund_flow_rank("m:90+t:2", top_n=1)

    assert captured["fs"] == "m:90+t:2"
    assert "行业板块主力净流入 TOP1" in result
    assert "银行" in result and "+50.0 亿" in result
    assert "航空机场" in result  # BOTTOM5 也展示


def test_sector_fund_flow_rank_concept_and_error(monkeypatch):
    from tradingagents.dataflows import a_stock

    def _boom(url, params=None, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(a_stock, "_em_get", _boom)
    result = market_flow.get_sector_fund_flow_rank("m:90+t:3", top_n=5)
    assert "概念板块资金流排名查询失败" in result


# ---------------------------------------------------------------------------
# gather_market_flow_data（各源 fake，验证聚合与指数路由）
# ---------------------------------------------------------------------------


def test_gather_market_flow_data_aggregates(monkeypatch):
    from tradingagents.dataflows import a_stock, index_data

    monkeypatch.setattr(
        index_data, "get_index_fund_flow",
        lambda t, d, include_history=True: f"[指数资金流 {t}]",
    )
    monkeypatch.setattr(
        a_stock, "get_northbound_flow", lambda d, include_history=True: "[北向]"
    )
    monkeypatch.setattr(
        market_flow, "get_sector_fund_flow_rank",
        lambda fs, top_n=10: f"[板块排名 {fs}]",
    )
    monkeypatch.setattr(a_stock, "get_hot_stocks", lambda d: "[热门股]")
    monkeypatch.setattr(
        index_data, "get_index_news",
        lambda t, s, e: "[指数新闻]",
    )

    raw = market_flow.gather_market_flow_data("000001.SH", "2026-08-16")

    assert "上证指数" in raw
    assert "[指数资金流 000001.SH]" in raw
    assert "[北向]" in raw
    assert "[热门股]" in raw
    assert "[指数新闻]" in raw
    assert raw.count("[板块排名") == 2   # 行业 + 概念


def test_gather_rejects_non_index():
    with pytest.raises(ValueError, match="不是指数标识"):
        market_flow.gather_market_flow_data("600519")


# ---------------------------------------------------------------------------
# 报告生成（fake LLM）
# ---------------------------------------------------------------------------


class _FakeLLM:
    def invoke(self, prompt):
        assert "上证指数" in prompt and "原始数据" in prompt
        return type("R", (), {"content": "<think>hidden</think>大盘资金流报告正文"})()


def test_generate_market_flow_report(monkeypatch):
    import tradingagents.market_flow as mf
    from tradingagents.llm_clients import create_llm_client

    monkeypatch.setattr(
        mf, "gather_market_flow_data", lambda i, c: "原始数据素材"
    )

    class _FakeClient:
        def get_llm(self):
            return _FakeLLM()

    monkeypatch.setattr(
        "tradingagents.llm_clients.create_llm_client",
        lambda **kwargs: _FakeClient(),
    )

    report = mf.generate_market_flow_report(
        config={"llm_provider": "openai", "quick_think_llm": "gpt"}, index_id="000001.SH"
    )

    assert "大盘资金流报告" in report
    assert "上证指数 (000001.SH)" in report
    assert "<think>" not in report      # think 块被剥离
    assert "报告正文" in report


# ---------------------------------------------------------------------------
# Web：指数解析 + 阶段裁剪
# ---------------------------------------------------------------------------


def test_sidebar_resolves_index_input():
    from web.components.sidebar import _resolve_user_input

    code, err = _resolve_user_input("上证指数", "指数")
    assert err is None and code == "000001.SH"

    code, err = _resolve_user_input("600519", "指数")
    assert code == "" and "不在指数号段" in err

    # 资金流向：留空默认上证指数
    code, err = _resolve_user_input("", "资金流向")
    assert err is None and code == "000001.SH"

    # 个股：原行为不变
    code, err = _resolve_user_input("600519", "个股")
    assert err is None and code == "600519"


def test_progress_tracker_stage_filtering():
    from web.progress import PIPELINE_STAGES, ProgressTracker

    stock = ProgressTracker(ticker="600519", trade_date="2026-08-16")
    assert len(stock.stages()) == 12

    index_ids = [
        s["id"] for s in PIPELINE_STAGES
        if s["id"] not in ("fundamentals", "lockup")
    ]
    index = ProgressTracker(ticker="000001.SH", trade_date="2026-08-16", stage_ids=index_ids)
    ids = [s["id"] for s in index.stages()]
    assert len(ids) == 10
    assert "fundamentals" not in ids and "lockup" not in ids
    assert "hot_money" in ids and "pm" in ids


def test_runner_infers_active_stage_within_filtered_stages():
    """指数模式的活动阶段推断不得落在被裁掉的 fundamentals 上。"""
    from web.progress import PIPELINE_STAGES, ProgressTracker
    from web.runner import _infer_active_stage

    index_ids = [
        s["id"] for s in PIPELINE_STAGES
        if s["id"] not in ("fundamentals", "lockup")
    ]
    tracker = ProgressTracker(ticker="000001.SH", trade_date="2026-08-16", stage_ids=index_ids)
    for sid in ("market", "social", "news"):
        tracker.mark_stage_done(sid)
    _infer_active_stage(tracker)
    assert tracker.current_stage == "policy"   # 跳过不存在的 fundamentals，直接到 policy
