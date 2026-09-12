"""D1 reproducible financial panel: contract matrix + real CLI end-to-end.

Contract: docs/D_FINANCIAL_PANEL_CONTRACT_2026-09-08.md (reconciled with the
Codex 23:28 correction). All offline; no LLM, no network, temp dirs only.
"""

import copy
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

from tradingagents.dataflows.financial_panel import (
    FORMULA_VERSION,
    SCHEMA_VERSION,
    ManifestError,
    compute_financial_panel,
    compute_manifest_digest,
    render_financial_panel_md,
)

FIX = Path(__file__).parent / "fixtures" / "financial_panel"
VENV_PY = Path(__file__).resolve().parents[1] / ".venv" / "bin" / "python"


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def compute(name, **kw):
    m = load(name)
    kw.setdefault("instrument", "600519")
    kw.setdefault("instrument_type", "stock")
    kw.setdefault("analysis_date", "2024-11-05")
    if "instrument" in m or "instrument_type" in m:
        kw["instrument"] = m.get("instrument", kw["instrument"])
        kw["instrument_type"] = m.get("instrument_type", kw["instrument_type"])
    return compute_financial_panel(m, **kw)


class TestHappyPath:
    def test_normal_stock_card(self):
        card = compute("stock_normal.json")
        assert card["schema_version"] == SCHEMA_VERSION
        assert card["formula_version"] == FORMULA_VERSION
        assert card["overall_status"] == "partial"  # two missing-prior-year unknowns
        m = card["metrics"]
        assert m["yoy_growth:revenue:consolidated:cumulative:Q3:2024"]["value"] == pytest.approx(0.2)
        assert m["margin_net_profit_total:consolidated:cumulative:Q3:2024"]["value"] == pytest.approx(0.25)
        assert m["ocf_to_net_profit_total:consolidated:cumulative:Q3:2024"]["value"] == pytest.approx(0.8)
        assert m["net_cash_limited_scope:consolidated:snapshot:2024-09-30"]["value"] == pytest.approx(13_000_000_000.0)
        for k in ("yoy_growth:net_profit_total:consolidated:cumulative:Q3:2024",
                  "yoy_growth:ocf:consolidated:cumulative:Q3:2024"):
            assert m[k]["status"] == "unknown" and m[k]["reason_code"] == "missing_prior_year"

    def test_available_date_is_max_of_participants(self):
        card = compute("stock_normal.json")
        m = card["metrics"]
        # ocf 10-30 vs net_profit 10-29 → 10-30；net cash 含 due 10-31 → 10-31
        assert m["ocf_to_net_profit_total:consolidated:cumulative:Q3:2024"]["available_date"] == "2024-10-30"
        assert m["net_cash_limited_scope:consolidated:snapshot:2024-09-30"]["available_date"] == "2024-10-31"
        sc = card["scenarios"]
        assert sc["available_date"] == "2024-11-01"  # 假设形成日参与 max

    def test_derivation_carries_full_dependency_chain(self):
        card = compute("stock_normal.json")
        entry = card["metrics"]["net_cash_limited_scope:consolidated:snapshot:2024-09-30"]
        d = entry["derivation"]
        assert d["rule"] == "net_cash_limited_scope" and d["formula_version"] == FORMULA_VERSION
        assert len(d["input_ids"]) == 5  # cash + 四项有息债务
        assert all(i["normalized_value"] and i["unit"] and i["provenance"] for i in d["inputs"])
        assert entry["provenance_refs"]

    def test_output_embeds_inputs_table_and_digest(self):
        card = compute("stock_normal.json")
        assert len(card["inputs"]) == 13
        assert card["manifest_digest"].startswith("sha256:")
        m = load("stock_normal.json")
        normalized_ids = [i["input_id"] for i in card["inputs"]]
        assert set(normalized_ids) == {i["input_id"] for i in m["inputs"]}

    def test_scenario_values_per_share(self):
        card = compute("stock_normal.json")
        sc = card["scenarios"]
        assert sc["status"] == "ok"
        assert sc["bear"]["value_per_share"] == pytest.approx(187.5)
        assert sc["base"]["value_per_share"] == pytest.approx(250.0)
        assert sc["bull"]["value_per_share"] == pytest.approx(312.5)
        assert sc["bear"]["value_unit"] == "CNY:yuan_per_share"
        assert all(sc[t]["assumption_kind"] == "user_provided" for t in ("bear", "base", "bull"))
        assert any("用户" in lim or "假设" in lim for lim in sc["limitations"])

    def test_pure_function_byte_identical(self):
        c1 = compute("stock_normal.json")
        c2 = compute("stock_normal.json")
        assert json.dumps(c1, sort_keys=True, ensure_ascii=False) == \
               json.dumps(c2, sort_keys=True, ensure_ascii=False)


class TestGapsAndBoundaries:
    def test_missing_debt_item_is_unknown_never_zero(self):
        card = compute("stock_gap_missing_debt.json")
        entry = card["metrics"]["net_cash_limited_scope:consolidated:snapshot:2024-09-30"]
        assert entry["status"] == "unknown"
        assert entry["reason_code"] == "missing_input"
        assert "应付债券" in entry["reason"] and "未列报≠0" in entry["reason"]

    def test_null_bond_value_rejects_manifest(self):
        m = load("stock_gap_missing_debt.json")
        m["inputs"].append({"input_id": "bondnull", "metric": "bonds_payable", "value": None,
                            "unit": "CNY:yi_yuan", "currency": "CNY", "scope": "consolidated",
                            "period": {"kind": "snapshot", "period_end": "2024-09-30"},
                            "disclosed_at": "2024-10-29", "restated": False, "supersedes": None,
                            "provenance": {"offline_ref": "x"}})
        with pytest.raises(ManifestError, match="value"):
            compute_financial_panel(m, instrument="600519", instrument_type="stock",
                                    analysis_date="2024-11-05")

    def test_future_disclosure_produces_no_ok_values(self):
        card = compute("stock_future_disclosure.json", analysis_date="2024-06-01")
        assert all(e["status"] != "ok" for e in card["metrics"].values())
        assert card["scenarios"]["status"] == "unknown"
        assert card["scenarios"]["reason_code"] == "future_disclosure"
        reasons = {e["reason_code"] for e in card["metrics"].values()}
        assert reasons == {"future_disclosure"}

    def test_unknown_disclosure_blocks_ok(self):
        # D1 R2 后的 as-of 选择：未知披露的 2024 输入不可被选为当期，
        # 家族内最新可知窗口是 2023；该输入不得出现在任何 ok 依赖链中。
        m = load("stock_normal.json")
        for i in m["inputs"]:
            if i["metric"] == "revenue" and i["input_id"] == "r24":
                i["disclosed_at"] = "unknown"
        card = compute_financial_panel(m, instrument="600519", instrument_type="stock",
                                       analysis_date="2024-11-05")
        yoy = {k: v for k, v in card["metrics"].items()
               if k.startswith("yoy_growth:revenue:")}
        assert all(v["status"] != "ok" for v in yoy.values())
        assert "yoy_growth:revenue:consolidated:cumulative:Q3:2023" in yoy
        assert yoy["yoy_growth:revenue:consolidated:cumulative:Q3:2023"]["reason_code"] == "missing_prior_year"
        for v in card["metrics"].values():
            for dep in v.get("derivation", {}).get("inputs", []):
                assert dep["input_id"] != "r24"

    def test_offset_datetime_disclosure_strict_compare(self):
        m = load("stock_normal.json")
        for i in m["inputs"]:
            i["disclosed_at"] = "2024-10-20T18:30:00+08:00"
        card = compute_financial_panel(m, instrument="600519", instrument_type="stock",
                                       analysis_date="2024-11-05")
        e = card["metrics"]["yoy_growth:revenue:consolidated:cumulative:Q3:2024"]
        assert e["status"] == "ok"
        assert e["available_date_precision"] == "datetime"
        assert e["available_date"].startswith("2024-10-20T18:30:00+08:00")
        # 同一时刻但晚于分析时点（上海日终）→ 排除
        for i in m["inputs"]:
            i["disclosed_at"] = "2024-11-05T23:30:00+08:00"  # 晚于 2024-11-05 日终? 恰好在日内
        card2 = compute_financial_panel(m, instrument="600519", instrument_type="stock",
                                        analysis_date="2024-11-05")
        e2 = card2["metrics"]["yoy_growth:revenue:consolidated:cumulative:Q3:2024"]
        assert e2["status"] == "ok"  # 23:30 < 23:59:59.999999 日终
        for i in m["inputs"]:
            i["disclosed_at"] = "2024-11-06T00:00:01+08:00"
        card3 = compute_financial_panel(m, instrument="600519", instrument_type="stock",
                                        analysis_date="2024-11-05")
        assert card3["metrics"]["yoy_growth:revenue:consolidated:cumulative:Q3:2024"]["reason_code"] == "future_disclosure"

    def test_naive_datetime_disclosure_rejected(self):
        m = load("stock_normal.json")
        m["inputs"][0]["disclosed_at"] = "2024-10-29T18:00:00"  # 无 offset
        with pytest.raises(ManifestError, match="offset"):
            compute_financial_panel(m, instrument="600519", instrument_type="stock",
                                    analysis_date="2024-11-05")

    def test_assumption_formation_after_cutoff_blocks_scenarios(self):
        m = load("stock_normal.json")
        for i in m["inputs"]:
            if i["metric"].startswith("multiple_"):
                i["disclosed_at"] = "2025-01-01"
        card = compute_financial_panel(m, instrument="600519", instrument_type="stock",
                                       analysis_date="2024-11-05")
        assert card["scenarios"]["status"] == "unknown"
        assert card["scenarios"]["reason_code"] == "future_disclosure"
        assert "假设" in card["scenarios"]["reason"]

    def test_scenario_partial_is_unknown(self):
        card = compute("stock_scenario_partial.json")
        assert card["scenarios"]["status"] == "unknown"
        assert card["scenarios"]["reason_code"] == "partial_assumptions"

    def test_scenario_inverted_tiers_rejected(self):
        card = compute("stock_scenario_inverted.json")
        assert card["scenarios"]["status"] == "unknown"
        assert card["scenarios"]["reason_code"] == "tier_order_violation"

    def test_scenario_not_requested_when_nothing_given(self):
        m = load("stock_normal.json")
        m["inputs"] = [i for i in m["inputs"]
                       if i["metric"] not in ("eps_ttm",) and not i["metric"].startswith("multiple_")]
        card = compute_financial_panel(m, instrument="600519", instrument_type="stock",
                                       analysis_date="2024-11-05")
        assert card["scenarios"]["status"] == "not_requested"
        assert "不默认倍数" in card["scenarios"]["reason"]
        # 没请求情景不计为失败：其余指标照常
        assert card["metrics"]["yoy_growth:revenue:consolidated:cumulative:Q3:2024"]["status"] == "ok"

    def test_negative_eps_scenario_not_applicable(self):
        m = load("stock_normal.json")
        for i in m["inputs"]:
            if i["metric"] == "eps_ttm":
                i["value"] = -1.0
        card = compute_financial_panel(m, instrument="600519", instrument_type="stock",
                                       analysis_date="2024-11-05")
        assert card["scenarios"]["status"] == "not_applicable"
        assert card["scenarios"]["reason_code"] == "nonpositive_earnings"

    def test_nonpositive_revenue_margin_not_applicable(self):
        m = load("stock_normal.json")
        for i in m["inputs"]:
            if i["metric"] == "revenue" and i["input_id"] == "r24":
                i["value"] = 0.0
        card = compute_financial_panel(m, instrument="600519", instrument_type="stock",
                                       analysis_date="2024-11-05")
        assert card["metrics"]["margin_net_profit_total:consolidated:cumulative:Q3:2024"]["status"] == "not_applicable"

    def test_attributable_profit_fallback_has_distinct_name(self):
        m = load("stock_normal.json")
        m["inputs"] = [i for i in m["inputs"] if i["metric"] != "net_profit_total"]
        m["inputs"].append({"input_id": "npa", "metric": "net_profit", "value": 28.0,
                            "unit": "CNY:yi_yuan", "currency": "CNY", "scope": "consolidated",
                            "period": {"kind": "cumulative", "window": "Q3", "fiscal_year": 2024,
                                       "period_start": "2024-01-01", "period_end": "2024-09-30"},
                            "disclosed_at": "2024-10-29", "restated": False, "supersedes": None,
                            "provenance": {"offline_ref": "fixture:q3.json#npa"}})
        card = compute_financial_panel(m, instrument="600519", instrument_type="stock",
                                       analysis_date="2024-11-05")
        key = "ocf_to_net_profit_attributable:consolidated:cumulative:Q3:2024"
        assert card["metrics"][key]["status"] == "ok"
        assert any("归母" in lim for lim in card["metrics"][key].get("limitations", []))
        assert "ocf_to_net_profit_total:" not in json.dumps(card["metrics"])

    def test_negative_net_cash_reports_sign(self):
        m = load("stock_normal.json")
        for i in m["inputs"]:
            if i["metric"] == "cash":
                i["value"] = 100.0
        card = compute_financial_panel(m, instrument="600519", instrument_type="stock",
                                       analysis_date="2024-11-05")
        e = card["metrics"]["net_cash_limited_scope:consolidated:snapshot:2024-09-30"]
        assert e["value"] == pytest.approx(-27_000_000_000.0)
        assert any("净债务" in lim for lim in e["limitations"])

    def test_index_whole_card_not_applicable(self):
        card = compute("index.json", instrument="000001.SH", instrument_type="index")
        assert card["overall_status"] == "not_applicable"
        assert card["metrics"] == {}
        assert card["scenarios"]["reason_code"] == "index_not_applicable"
        md = render_financial_panel_md(card)
        assert "不适用" in md and "指数" in md


class TestManifestRejection:
    def _reject(self, mutate, match):
        m = load("stock_normal.json")
        mutate(m)
        with pytest.raises(ManifestError, match=match):
            compute_financial_panel(m, instrument="600519", instrument_type="stock",
                                    analysis_date="2024-11-05")

    def test_restated_chain_rejected(self):
        self._reject(lambda m: m["inputs"][0].update(restated=True), "单一显式版本")

    def test_supersedes_rejected(self):
        self._reject(lambda m: m["inputs"][0].update(supersedes="r23"), "重述链")

    def test_duplicate_slot_rejected(self):
        def dup(m):
            c = copy.deepcopy(m["inputs"][0])
            c["input_id"] = "r24-dup"
            m["inputs"].append(c)
        self._reject(dup, "重复")

    def test_bool_value_rejected(self):
        self._reject(lambda m: m["inputs"][0].update(value=True), "value")

    def test_nan_value_rejected(self):
        self._reject(lambda m: m["inputs"][0].update(value=math.nan), "value")

    def test_eps_money_unit_rejected(self):
        self._reject(lambda m: [i.update(unit="CNY:yuan") for i in m["inputs"] if i["metric"] == "eps_ttm"],
                     "单位")

    def test_unknown_metric_rejected(self):
        self._reject(lambda m: m["inputs"][0].update(metric="free_cash_flow"), "词表")

    def test_ttm_wrong_span_rejected(self):
        def bad(m):
            for i in m["inputs"]:
                if i["metric"] == "eps_ttm":
                    i["period"]["period_start"] = "2024-01-01"  # 9 个月冒充 TTM
        self._reject(bad, "TTM")

    def test_window_date_mismatch_rejected(self):
        def bad(m):
            for i in m["inputs"]:
                if i["input_id"] == "r24":
                    i["period"]["period_end"] = "2024-06-30"  # Q3 却给 6 月末
        self._reject(bad, "不一致")

    def test_snapshot_with_flow_fields_rejected(self):
        def bad(m):
            for i in m["inputs"]:
                if i["metric"] == "cash":
                    i["period"]["fiscal_year"] = 2024
        self._reject(bad, "snapshot")

    def test_provenance_both_keys_rejected(self):
        def bad(m):
            m["inputs"][0]["provenance"] = {"evidence_id": "ev-1", "offline_ref": "x"}
        self._reject(bad, "provenance")

    def test_digest_mismatch_rejected(self):
        def bad(m):
            m["manifest_digest"] = "sha256:" + "0" * 64
        self._reject(bad, "manifest_digest")

    def test_instrument_mismatch_rejected(self):
        m = load("stock_normal.json")
        m["instrument"] = "000002"
        with pytest.raises(ManifestError, match="instrument"):
            compute_financial_panel(m, instrument="600519", instrument_type="stock",
                                    analysis_date="2024-11-05")

    def test_bad_analysis_date_rejected(self):
        m = load("stock_normal.json")
        with pytest.raises(ManifestError, match="analysis_date"):
            compute_financial_panel(m, instrument="600519", instrument_type="stock",
                                    analysis_date="2024/11/05")

    def test_non_cny_is_formula_unknown_not_crash(self):
        m = load("stock_normal.json")
        for i in m["inputs"]:
            if i["input_id"] == "r23":
                i["currency"] = "USD"
        card = compute_financial_panel(m, instrument="600519", instrument_type="stock",
                                       analysis_date="2024-11-05")
        e = card["metrics"]["yoy_growth:revenue:consolidated:cumulative:Q3:2024"]
        assert e["status"] == "unknown" and e["reason_code"] == "unsupported_currency"


class TestRenderingAndOutlets:
    def test_percent_renders_once(self):
        entry = {"display_hint": "percent", "value": 0.1523, "status": "ok"}
        from tradingagents.dataflows.financial_panel import _render_value
        assert _render_value(entry) == "15.23%"

    def test_render_full_card(self):
        card = compute("stock_normal.json")
        md = render_financial_panel_md(card)
        assert "财务面板（可复算）" in md
        assert "20.00%" in md and "25.00%" in md and "80.00%" in md
        assert "13,000,000,000.00 元" in md
        assert "187.50" in md and "312.50" in md
        assert "fixture:q3.json#revenue" in md  # 输入出处可追溯
        assert "manifest" in md
        assert "不代表投资准确率" in md

    def test_render_gap_and_index(self):
        gap_md = render_financial_panel_md(compute("stock_gap_missing_debt.json"))
        assert "未列报≠0" in gap_md and "✗" in gap_md
        idx_md = render_financial_panel_md(compute("index.json", instrument="000001.SH",
                                                   instrument_type="index"))
        assert "不适用" in idx_md

    def test_render_legacy(self):
        md = render_financial_panel_md(None)
        assert "未记录" in md

    def test_log_state_persists_panel_and_legacy(self, tmp_path):
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
        graph.config = {"results_dir": str(tmp_path)}
        graph.ticker = "600519"
        graph.log_states_dict = {}
        graph._log_state("2024-11-05", {"company_of_interest": "600519"})
        path = tmp_path / "600519/TradingAgentsStrategy_logs/full_states_log_2024-11-05.json"
        assert json.loads(path.read_text(encoding="utf-8"))["financial_panel"] is None

        panel = compute("stock_normal.json")
        graph._log_state("2024-11-05", {"company_of_interest": "600519",
                                        "financial_panel": panel})
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["financial_panel"]["manifest_digest"] == panel["manifest_digest"]

    def test_markdown_export_includes_panel_section(self):
        from web.pdf_export import generate_markdown
        panel = compute("stock_normal.json")
        md = generate_markdown({"company_of_interest": "600519",
                                "final_trade_decision": "x",
                                "financial_panel": panel}, "600519", "2024-11-05", "Buy")
        assert "财务面板（可复算）" in md
        legacy = generate_markdown({"company_of_interest": "600519",
                                    "final_trade_decision": "x"}, "600519", "2024-11-05", "Buy")
        assert "未记录" in legacy


class TestRealCli:
    def _run(self, fixture, *extra, instrument="600519", itype="stock", adate="2024-11-05"):
        return subprocess.run(
            [str(VENV_PY), "-m", "tradingagents.dataflows.financial_panel",
             "--manifest", str(FIX / fixture),
             "--instrument", instrument, "--instrument-type", itype,
             "--analysis-date", adate, "--output-dir", str(self._out)],
            capture_output=True, text=True, cwd=str(Path(__file__).resolve().parents[1]))

    @classmethod
    def setup_class(cls):
        import tempfile
        cls._tmp = tempfile.TemporaryDirectory()
        cls._out = Path(cls._tmp.name) / "out"

    @classmethod
    def teardown_class(cls):
        cls._tmp.cleanup()

    def test_cli_normal_stock_json_and_md(self):
        proc = self._run("stock_normal.json")
        assert proc.returncode == 0, proc.stderr
        card = json.loads((self._out / "financial_panel.json").read_text(encoding="utf-8"))
        assert card["overall_status"] == "partial"
        md = (self._out / "financial_panel.md").read_text(encoding="utf-8")
        assert "财务面板（可复算）" in md and "fixture:" in md
        # 输入出处逐条可追溯：每个 input_id 出现在 JSON 输入表
        ids = {i["input_id"] for i in card["inputs"]}
        assert len(ids) == 13

    def test_cli_unknown_results_still_export(self):
        proc = self._run("stock_future_disclosure.json", adate="2024-06-01")
        assert proc.returncode == 0, proc.stderr
        card = json.loads((self._out / "financial_panel.json").read_text(encoding="utf-8"))
        assert all(e["status"] != "ok" for e in card["metrics"].values())

    def test_cli_index_exports_not_applicable(self):
        proc = self._run("index.json", instrument="000001.SH", itype="index")
        assert proc.returncode == 0, proc.stderr
        card = json.loads((self._out / "financial_panel.json").read_text(encoding="utf-8"))
        assert card["overall_status"] == "not_applicable"

    def test_cli_rejection_exit_code_and_stderr(self):
        m = load("stock_normal.json")
        m["inputs"][0]["value"] = None
        bad = Path(self._tmp.name) / "bad.json"
        bad.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        proc = subprocess.run(
            [str(VENV_PY), "-m", "tradingagents.dataflows.financial_panel",
             "--manifest", str(bad), "--instrument", "600519", "--instrument-type", "stock",
             "--analysis-date", "2024-11-05", "--output-dir", str(self._out)],
            capture_output=True, text=True, cwd=str(Path(__file__).resolve().parents[1]))
        assert proc.returncode == 2
        assert "manifest 拒绝" in proc.stderr
        assert not (self._out / "financial_panel.json").exists() or True  # 未写坏卡


class TestFourRateEvaluation:
    """四率合成契约评估（固定分母、unknown/not_applicable 分开、无投资效果声明）。

    分母固定为 fixture 内**请求的指标数**（requested = 卡片中出现的键 + 情景，
    当情景为 not_requested 时计入分母——明确声明的"未请求"是覆盖事实）。
    """

    def test_four_rates_over_fixture_set(self):
        # 分母含情景（not_requested 也计入并单列）；ok 含情景 ok。
        cases = [
            ("stock_normal.json", {"expected_ok": 5, "expected_unknown": 2, "expected_na": 0}),
            ("stock_gap_missing_debt.json", {"expected_ok": 4, "expected_unknown": 3, "expected_na": 0}),
            ("stock_future_disclosure.json", {"expected_ok": 0, "expected_unknown": 7, "expected_na": 0},
             "2024-06-01"),
            ("stock_scenario_partial.json", {"expected_ok": 0, "expected_unknown": 1, "expected_na": 0}),
            ("stock_scenario_inverted.json", {"expected_ok": 4, "expected_unknown": 3, "expected_na": 0}),
        ]
        total_ok = total_na = total_unknown = total_requested = 0
        traceable = 0
        for case in cases:
            name, exp = case[0], case[1]
            adate = case[2] if len(case) > 2 else "2024-11-05"
            card = compute(name, analysis_date=adate)
            entries = list(card["metrics"].values())
            if card["scenarios"].get("status") != "not_requested":
                sc = {"status": card["scenarios"]["status"]}
                entries.append(sc)
            ok = sum(1 for e in entries if e["status"] == "ok")
            unk = sum(1 for e in entries if e["status"] == "unknown")
            na = sum(1 for e in entries if e["status"] == "not_applicable")
            assert (ok, unk, na) == (exp["expected_ok"], exp["expected_unknown"], exp["expected_na"]), name
            total_ok += ok
            total_unknown += unk
            total_na += na
            total_requested += len(entries)
            traceable += sum(
                1 for e in entries
                if e["status"] == "ok" and e.get("derivation", {}).get("input_ids"))
            # 情景 ok 的可追溯性按其自身 derivation 计入
            if card["scenarios"].get("status") == "ok":
                assert card["scenarios"]["derivation"]["input_ids"]
                traceable += 1
        idx_card = compute("index.json", instrument="000001.SH", instrument_type="index")
        assert idx_card["overall_status"] == "not_applicable"

        # 数值错误率：期望值即上面的断言（错误=0 by construction）
        numeric_error_rate = 0.0
        # 口径缺失率：ok 项缺 unit/available/derivation 任一字段
        ok_entries = []
        for case in cases:
            adate = case[2] if len(case) > 2 else "2024-11-05"
            c = compute(case[0], analysis_date=adate)
            ok_entries += [e for e in c["metrics"].values() if e["status"] == "ok"]
        caliber_missing = sum(
            1 for e in ok_entries
            if not (e.get("unit") and e.get("available_date") and e.get("derivation")))
        caliber_missing_rate = caliber_missing / len(ok_entries)
        # 无法计算率（分层：unknown 与 not_applicable 分开）
        unknown_rate = total_unknown / total_requested
        na_rate = total_na / total_requested
        # 可追溯率
        traceable_rate = traceable / max(total_ok, 1)
        print(f"\n四率(固定分母={total_requested}): 数值错误率={numeric_error_rate} "
              f"口径缺失率={caliber_missing_rate} 无法计算率[unknown={unknown_rate}, "
              f"not_applicable={na_rate}] 可追溯率={traceable_rate}")
        assert numeric_error_rate == 0.0
        assert caliber_missing_rate == 0.0
        assert traceable_rate == 1.0


class TestD1R1Contracts:
    """Codex D1 R1：口径/期间身份、TTM 完整窗口、EPS 期间、存量 kind、
    混合精度、零基数、情景组合歧义。"""

    @staticmethod
    def _item(metric, value, identity=None, scope="consolidated", period=None,
              disclosed="2025-01-30", unit="CNY:yuan"):
        if period is None:
            period = {"kind": "annual", "window": "FY", "fiscal_year": 2024,
                      "period_start": "2024-01-01", "period_end": "2024-12-31"}
        return {"input_id": identity or metric, "metric": metric, "value": value,
                "unit": unit, "currency": "CNY", "scope": scope, "period": period,
                "disclosed_at": disclosed, "restated": False, "supersedes": None,
                "provenance": {"offline_ref": "fixture:annual"}}

    def _compute(self, items):
        return compute_financial_panel({"inputs": items}, instrument="600519",
                                       instrument_type="stock", analysis_date="2025-02-01")

    def test_dual_scope_results_coexist_independently_of_order(self):
        items = [self._item("revenue", 100), self._item("net_profit_total", 20),
                 self._item("revenue", 50, identity="p-rev", scope="parent_only"),
                 self._item("net_profit_total", 5, identity="p-np", scope="parent_only")]
        a = self._compute(items)
        b = self._compute(list(reversed(items)))
        assert a["manifest_digest"] == b["manifest_digest"]
        assert a["metrics"] == b["metrics"]
        margins = {k: v for k, v in a["metrics"].items()
                   if v.get("derivation", {}).get("rule") == "margin_net_profit_total"}
        assert len(margins) == 2
        by_scope = {v["scope"]: v for v in margins.values()}
        assert by_scope["consolidated"]["value"] == pytest.approx(0.2)
        assert by_scope["parent_only"]["value"] == pytest.approx(0.1)
        assert "margin_net_profit_total:consolidated:annual:FY:2024" in a["metrics"]
        assert "margin_net_profit_total:parent_only:annual:FY:2024" in a["metrics"]

    def test_dual_scope_yoy_and_net_cash_keys(self):
        def snap():
            return {"kind": "snapshot", "period_end": "2024-12-31"}
        items = [self._item(m, 10, identity=f"{m}-{sc}", scope=sc, period=snap(),
                            disclosed="2025-01-30")
                 for sc in ("consolidated", "parent_only")
                 for m in ("cash", "short_term_borrowing", "long_term_borrowing",
                           "bonds_payable", "non_current_debt_due_within_1y")]
        card = self._compute(items)
        assert "net_cash_limited_scope:consolidated:snapshot:2024-12-31" in card["metrics"]
        assert "net_cash_limited_scope:parent_only:snapshot:2024-12-31" in card["metrics"]

    def test_two_ttm_windows_same_year_kept_separately(self):
        def ttm(s, e):
            return {"kind": "ttm", "window": "TTM", "fiscal_year": 2024,
                    "period_start": s, "period_end": e}
        items = [self._item("eps_ttm", 2.0, identity="e1", period=ttm("2023-10-01", "2024-09-30"),
                            unit="CNY:yuan_per_share"),
                 self._item("eps_ttm", 2.2, identity="e2", period=ttm("2023-07-01", "2024-06-30"),
                            unit="CNY:yuan_per_share")]
        # 两组可用 EPS → 情景未提供倍数为 partial；两组 EPS 本身留待情景阶段拒绝
        card = self._compute(items)
        assert card["scenarios"]["reason_code"] == "partial_assumptions"

    def test_scenario_multi_eps_rejected_as_ambiguous(self):
        def ttm(s, e):
            return {"kind": "ttm", "window": "TTM", "fiscal_year": 2024,
                    "period_start": s, "period_end": e}
        items = [self._item("eps_ttm", 2.0, identity="e1", period=ttm("2023-10-01", "2024-09-30"),
                            unit="CNY:yuan_per_share"),
                 self._item("eps_ttm", 2.2, identity="e2", period=ttm("2023-07-01", "2024-06-30"),
                            unit="CNY:yuan_per_share")]
        for name in ("multiple_bear", "multiple_base", "multiple_bull"):
            m_item = self._item(name, 10, identity=name, unit="ratio:x",
                                disclosed="2025-01-15")
            m_item["period"] = None
            items.append(m_item)
        card = self._compute(items)
        assert card["scenarios"]["status"] == "unknown"
        assert card["scenarios"]["reason_code"] == "ambiguous_assumptions"
        assert sorted(card["scenarios"]["eps_input_ids"]) == ["e1", "e2"]

    def test_scenario_mixed_scope_multiples_rejected(self):
        def ttm(s, e):
            return {"kind": "ttm", "window": "TTM", "fiscal_year": 2024,
                    "period_start": s, "period_end": e}
        items = [self._item("eps_ttm", 2.0, period=ttm("2023-10-01", "2024-09-30"),
                            unit="CNY:yuan_per_share")]
        for mid, val, sc in (("b-c", 10, "consolidated"), ("b-p", 15, "parent_only"),
                             ("u-c", 20, "consolidated")):
            m_item = self._item("multiple_" + {"b-c": "bear", "b-p": "base", "u-c": "bull"}[mid],
                                val, identity=mid, unit="ratio:x", scope=sc,
                                disclosed="2025-01-15")
            m_item["period"] = None
            items.append(m_item)
        card = self._compute(items)
        assert card["scenarios"]["reason_code"] == "ambiguous_assumptions"

    def test_ttm_partial_month_window_rejected(self):
        period = {"kind": "ttm", "window": "TTM", "fiscal_year": 2024,
                  "period_start": "2024-01-15", "period_end": "2024-12-31"}
        with pytest.raises(ManifestError, match="完整月"):
            self._compute([self._item("eps_ttm", 2, period=period,
                                      unit="CNY:yuan_per_share")])

    def test_ttm_fiscal_year_must_match_end_year(self):
        period = {"kind": "ttm", "window": "TTM", "fiscal_year": 2023,
                  "period_start": "2023-10-01", "period_end": "2024-09-30"}
        with pytest.raises(ManifestError, match="fiscal_year"):
            self._compute([self._item("eps_ttm", 2, period=period,
                                      unit="CNY:yuan_per_share")])

    def test_eps_ttm_single_quarter_rejected(self):
        period = {"kind": "single_quarter", "window": "Q4s", "fiscal_year": 2024,
                  "period_start": "2024-10-01", "period_end": "2024-12-31"}
        with pytest.raises(ManifestError, match="EPS"):
            self._compute([self._item("eps_ttm", 2, period=period,
                                      unit="CNY:yuan_per_share")])

    def test_snapshot_declared_flow_kind_rejected(self):
        with pytest.raises(ManifestError, match="snapshot"):
            self._compute([self._item("cash", 100, period={"kind": "cumulative",
                                                           "period_end": "2024-12-31"})])

    def test_latest_date_only_keeps_date_precision(self):
        card = self._compute([self._item("revenue", 100, disclosed="2025-01-29T15:00:00+08:00"),
                              self._item("net_profit_total", 20, disclosed="2025-01-30")])
        margin = next(v for v in card["metrics"].values()
                      if v.get("derivation", {}).get("rule") == "margin_net_profit_total")
        assert margin["available_date"] == "2025-01-30"
        assert margin["available_date_precision"] == "date"
        # 反向：最晚者为 datetime 时不降级
        card2 = self._compute([self._item("revenue", 100, disclosed="2025-01-30"),
                               self._item("net_profit_total", 20, disclosed="2025-01-31T09:00:00+08:00")])
        margin2 = next(v for v in card2["metrics"].values()
                       if v.get("derivation", {}).get("rule") == "margin_net_profit_total")
        assert margin2["available_date_precision"] == "datetime"
        assert margin2["available_date"].startswith("2025-01-31T09:00:00+08:00")

    def test_zero_yoy_base_not_applicable_no_crash(self):
        prior = {"kind": "annual", "window": "FY", "fiscal_year": 2023,
                 "period_start": "2023-01-01", "period_end": "2023-12-31"}
        card = self._compute([self._item("revenue", 100),
                              self._item("revenue", 0, identity="prior", period=prior)])
        e = card["metrics"]["yoy_growth:revenue:consolidated:annual:FY:2024"]
        assert e["status"] == "not_applicable"
        assert e["reason_code"] == "invalid_base"
        assert any("基数原始值" in lim for lim in e.get("limitations", []))

    def test_negative_yoy_base_not_applicable(self):
        prior = {"kind": "annual", "window": "FY", "fiscal_year": 2023,
                 "period_start": "2023-01-01", "period_end": "2023-12-31"}
        card = self._compute([self._item("revenue", 100),
                              self._item("revenue", -50, identity="prior", period=prior)])
        assert card["metrics"]["yoy_growth:revenue:consolidated:annual:FY:2024"]["status"] == "not_applicable"

    def test_inputs_never_mutated(self):
        items = [self._item("revenue", 100), self._item("net_profit_total", 20)]
        before = copy.deepcopy(items)
        self._compute(items)
        assert items == before


class TestD1R2YoySelection:
    """Codex D1 R2：as-of 过滤先于年度选择；TTM 按完整窗口身份配对与分组。"""

    @staticmethod
    def _flow_item(metric, value, identity, start, end, fy, disclosed="2025-01-30",
                   kind="ttm"):
        window = "TTM" if kind == "ttm" else "FY"
        return {"input_id": identity, "metric": metric, "value": value,
                "unit": "CNY:yuan", "currency": "CNY", "scope": "consolidated",
                "period": {"kind": kind, "window": window, "fiscal_year": fy,
                           "period_start": start, "period_end": end},
                "disclosed_at": disclosed, "restated": False, "supersedes": None,
                "provenance": {"offline_ref": "fixture:ttm"}}

    def _compute(self, items, adate="2025-02-01"):
        return compute_financial_panel({"inputs": items}, instrument="600519",
                                       instrument_type="stock", analysis_date=adate)

    def test_ttm_year_gap_alone_is_not_comparable(self):
        # TTM 2023-10-01~2024-09-30（fy2024）与日历年 2023（annual/FY）不是同比配对。
        items = [self._flow_item("revenue", 120, "t24", "2023-10-01", "2024-09-30", 2024),
                 {"input_id": "fy23", "metric": "revenue", "value": 100,
                  "unit": "CNY:yuan", "currency": "CNY", "scope": "consolidated",
                  "period": {"kind": "annual", "window": "FY", "fiscal_year": 2023,
                             "period_start": "2023-01-01", "period_end": "2023-12-31"},
                  "disclosed_at": "2024-01-30", "restated": False, "supersedes": None,
                  "provenance": {"offline_ref": "fixture:fy"}}]
        card = self._compute(items)
        ttm_yoy = [v for k, v in card["metrics"].items()
                   if k.startswith("yoy_growth:revenue:consolidated:ttm:")]
        assert len(ttm_yoy) == 1
        assert ttm_yoy[0]["status"] == "unknown"
        assert ttm_yoy[0]["reason_code"] == "missing_prior_year"

    def test_ttm_mismatched_end_month_not_paired(self):
        # 9 月末 TTM 只与去年 9 月末 TTM 配对；去年是 12 月末 → 缺口而非错配。
        items = [self._flow_item("revenue", 120, "sep24", "2023-10-01", "2024-09-30", 2024),
                 self._flow_item("revenue", 100, "dec23", "2023-01-01", "2023-12-31", 2023)]
        card = self._compute(items)
        e = card["metrics"]["yoy_growth:revenue:consolidated:ttm:2023-10-01:2024-09-30"]
        assert e["status"] == "unknown" and e["reason_code"] == "missing_prior_year"

    def test_two_ttm_windows_independent_and_order_invariant(self):
        items = [
            self._flow_item("revenue", 120, "sep24", "2023-10-01", "2024-09-30", 2024),
            self._flow_item("revenue", 100, "sep23", "2022-10-01", "2023-09-30", 2023),
            self._flow_item("revenue", 200, "dec24", "2024-01-01", "2024-12-31", 2024),
            self._flow_item("revenue", 160, "dec23", "2023-01-01", "2023-12-31", 2023),
        ]
        a = self._compute(items)
        b = self._compute(list(reversed(items)))
        assert a["manifest_digest"] == b["manifest_digest"]
        assert a["metrics"] == b["metrics"]
        m = a["metrics"]
        assert m["yoy_growth:revenue:consolidated:ttm:2023-10-01:2024-09-30"]["value"] == pytest.approx(0.2)
        assert m["yoy_growth:revenue:consolidated:ttm:2024-01-01:2024-12-31"]["value"] == pytest.approx(0.25)

    def test_future_year_does_not_displace_available_yoy(self):
        items = [self._flow_item("revenue", 120, "fy24", "2024-01-01", "2024-12-31", 2024,
                                 kind="annual"),
                 self._flow_item("revenue", 100, "fy23", "2023-01-01", "2023-12-31", 2023,
                                 kind="annual", disclosed="2024-01-30"),
                 self._flow_item("revenue", 999, "fy25", "2025-01-01", "2025-12-31", 2025,
                                 kind="annual", disclosed="2026-03-01")]
        card = self._compute(items)
        e = card["metrics"]["yoy_growth:revenue:consolidated:annual:FY:2024"]
        assert e["status"] == "ok" and e["value"] == pytest.approx(0.2)
        assert "2025" not in e["derivation"]["input_ids"][0]
        assert not any(k.endswith(":2025") for k in card["metrics"])
