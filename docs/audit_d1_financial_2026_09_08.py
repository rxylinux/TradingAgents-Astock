"""Codex independent D1 period, scope, precision and selection contracts."""

import copy

import pytest

from tradingagents.dataflows.financial_panel import ManifestError, compute_financial_panel


def flow(year=2024):
    return {"kind": "annual", "window": "FY", "fiscal_year": year,
            "period_start": f"{year}-01-01", "period_end": f"{year}-12-31"}


def item(metric, value, *, identity=None, scope="consolidated", period=None,
         disclosed="2025-01-30", unit="CNY:yuan"):
    return {"input_id": identity or metric, "metric": metric, "value": value,
            "unit": unit, "currency": "CNY", "scope": scope,
            "period": flow() if period is None else period, "disclosed_at": disclosed,
            "restated": False, "supersedes": None, "provenance": {"offline_ref": "fixture:annual"}}


def compute(items):
    return compute_financial_panel({"inputs": items}, instrument="600519",
                                  instrument_type="stock", analysis_date="2025-02-01")


def test_consolidated_and_parent_metrics_do_not_overwrite():
    items = [item("revenue", 100), item("net_profit_total", 20),
             item("revenue", 50, identity="parent-revenue", scope="parent_only"),
             item("net_profit_total", 5, identity="parent-profit", scope="parent_only")]
    card = compute(items)
    margins = [m for m in card["metrics"].values() if m.get("status") == "ok"
               and m.get("derivation", {}).get("rule") == "margin_net_profit_total"]
    assert len(margins) == 2, card
    assert {m["scope"] for m in margins} == {"consolidated", "parent_only"}


def test_metric_result_independent_of_manifest_order():
    items = [item("revenue", 100), item("net_profit_total", 20),
             item("revenue", 50, identity="parent-revenue", scope="parent_only"),
             item("net_profit_total", 5, identity="parent-profit", scope="parent_only")]
    a, b = compute(items), compute(list(reversed(items)))
    assert a["manifest_digest"] == b["manifest_digest"]
    assert a["metrics"] == b["metrics"]


def test_ttm_rejects_partial_first_month():
    period = {"kind": "ttm", "window": "TTM", "fiscal_year": 2024,
              "period_start": "2024-01-15", "period_end": "2024-12-31"}
    with pytest.raises(ManifestError):
        compute([item("eps_ttm", 2, period=period, unit="CNY:yuan_per_share")])


def test_eps_ttm_rejects_single_quarter_window():
    period = {"kind": "single_quarter", "window": "Q4s", "fiscal_year": 2024,
              "period_start": "2024-10-01", "period_end": "2024-12-31"}
    with pytest.raises(ManifestError):
        compute([item("eps_ttm", 2, period=period, unit="CNY:yuan_per_share")])


def test_snapshot_does_not_silently_relabel_flow_kind():
    with pytest.raises(ManifestError):
        compute([item("cash", 100, period={"kind": "cumulative", "period_end": "2024-12-31"})])


def test_latest_date_only_disclosure_does_not_gain_invented_time():
    card = compute([item("revenue", 100, disclosed="2025-01-29T15:00:00+08:00"),
                    item("net_profit_total", 20, disclosed="2025-01-30")])
    margin = next(m for m in card["metrics"].values() if m.get("derivation", {}).get("rule") == "margin_net_profit_total")
    assert margin["available_date_precision"] == "date", margin
    assert margin["available_date"] == "2025-01-30", margin


def test_nonpositive_yoy_base_returns_not_applicable_without_crash():
    card = compute([item("revenue", 100), item("revenue", 0, identity="prior-revenue", period=flow(2023))])
    assert any(m["status"] == "not_applicable" for m in card["metrics"].values()), card


def test_scope_results_do_not_mutate_inputs():
    items = [item("revenue", 100), item("net_profit_total", 20)]
    before = copy.deepcopy(items)
    compute(items)
    assert items == before


def ttm(start, end):
    return {"kind": "ttm", "window": "TTM", "fiscal_year": int(end[:4]),
            "period_start": start, "period_end": end}


def test_yoy_requires_matching_ttm_end_month_not_just_year():
    card = compute([
        item("revenue", 200, period=ttm("2023-10-01", "2024-09-30")),
        item("revenue", 100, identity="prior", period=ttm("2023-01-01", "2023-12-31")),
    ])
    yoy = [m for m in card["metrics"].values() if "同比" in m["label"]]
    assert yoy and all(m["status"] != "ok" for m in yoy), card


def test_two_ttm_yoy_windows_are_independent_and_order_invariant():
    items = [
        item("revenue", 200, identity="sep24", period=ttm("2023-10-01", "2024-09-30")),
        item("revenue", 100, identity="sep23", period=ttm("2022-10-01", "2023-09-30")),
        item("revenue", 240, identity="dec24", period=ttm("2024-01-01", "2024-12-31")),
        item("revenue", 200, identity="dec23", period=ttm("2023-01-01", "2023-12-31")),
    ]
    a, b = compute(items), compute(list(reversed(items)))
    yoy = [m for m in a["metrics"].values() if m.get("derivation", {}).get("rule") == "yoy_growth"]
    assert len(yoy) == 2, a
    assert {m["value"] for m in yoy} == {1.0, 0.2}
    assert a["metrics"] == b["metrics"]


def test_future_year_does_not_displace_available_yoy():
    historical = [item("revenue", 120), item("revenue", 100, identity="prior", period=flow(2023))]
    a = compute(historical)
    b = compute(historical + [item("revenue", 999, identity="future", period=flow(2025), disclosed="2026-01-30")])
    known = {k: m for k, m in a["metrics"].items() if m.get("status") == "ok"}
    assert known
    for key, entry in known.items():
        assert b["metrics"].get(key) == entry, b
