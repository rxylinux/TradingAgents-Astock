"""C2 research thesis card: deterministic build, reference validation,
assessment derivation, persistence and outlets — all offline."""

import json
from types import SimpleNamespace

import pytest

from tradingagents.agents.schemas import PortfolioDecision, ThesisCondition
from tradingagents.agents.thesis import (
    ASSESSABLE,
    INSUFFICIENT,
    LIMITED,
    UNKNOWN,
    append_thesis_status,
    build_thesis_card,
    is_thesis_card,
    render_thesis_md,
    render_thesis_status_line,
)
from tradingagents.evidence import ledger


def _bundle(record_pub="2024-11-05T11:00:00+08:00", title="thesis fact",
            source="东方财富", source_status="successful"):
    rec = {
        "source": source, "title": title, "content": "body", "url": "",
        "published_at": record_pub, "time_precision": "datetime",
    }
    artifact = {
        "schema": ledger.SCHEMA_ARTIFACT, "tool": "get_news",
        "provenance": "a_stock", "status": "successful",
        "requested_window": None, "records": [rec],
        "source_statuses": [{"source": source, "status": source_status,
                             "record_count": 1}],
        "exclusions": {}, "coverage_notes": [],
    }
    entry = ledger.build_fetch_event("news", "call_t", artifact, trade_date="2024-11-05")
    return ledger.evidence_reducer(None, {
        "schema": ledger.SCHEMA_DELTA, "run_id": "run-c2",
        "events": {"news:call_t": entry},
    })


def _rid(bundle, index=0):
    return bundle["records"][index]["evidence_id"]


def _quality(status="complete", **extra):
    return {"schema_version": 1, "status": status, "selected_analysts": ["news"],
            "analysts": [], "active_count": 1, "fail_count": 0, "limitations": [],
            **extra}


def _state(bundle=None, quality=None, **kw):
    state = {
        "company_of_interest": "600519",
        "trade_date": "2024-11-05",
        "run_metadata": {"run_id": "run-c2", "created_at": "2024-11-05T09:00:00+08:00"},
    }
    if bundle is not None:
        state["evidence_bundle"] = bundle
    if quality is not None:
        state["data_quality"] = quality
    state.update(kw)
    return state


def _decision(**kw):
    base = dict(
        rating="Overweight",
        executive_summary="summary",
        investment_thesis="cash-flow driven thesis",
        time_horizon="3-6 months",
        supporting_evidence_ids=[],
        contradicting_evidence_ids=[],
        hypotheses_to_verify=["northbound flow persistence"],
        catalysts=["quarterly report"],
        invalidation_conditions=[ThesisCondition(
            description="经营现金流同比转负",
            indicator="经营现金流", comparator="同比", threshold="0",
            period="下一已披露季度", source="季报")],
        next_check_trigger="季报披露后",
    )
    base.update(kw)
    return PortfolioDecision(**base)


class TestBuildThesisCard:
    def test_code_fields_from_state_no_fabrication(self):
        card = build_thesis_card(_decision(), _state())
        assert card["instrument"] == "600519"
        assert card["formed_at"] == "2024-11-05T09:00:00+08:00"
        assert card["analysis_date"] == "2024-11-05"
        assert card["run_id"] == "run-c2"
        assert card["rating"] == "Overweight"
        assert card["prediction_horizon"] == "3-6 months"
        assert card["confidence"] == "uncalibrated"

    def test_schema_defaults_are_unknown_not_invented(self):
        bare = PortfolioDecision(rating="Hold", executive_summary="s",
                                 investment_thesis="t")
        card = build_thesis_card(bare, _state())
        assert card["supporting_evidence_ids"] == []
        assert card["hypotheses_to_verify"] == []
        assert card["invalidation_conditions"] == []
        assert card["next_check_trigger"] == "unknown"
        # 不凭空生成价格/概率/日期
        assert "0." not in json.dumps(card["invalidation_conditions"])

    def test_dangling_reference(self):
        bundle = _bundle()
        card = build_thesis_card(
            _decision(supporting_evidence_ids=[_rid(bundle), "ev-x-nope"]),
            _state(bundle=bundle, quality=_quality()))
        chk = card["evidence_reference_check"]
        assert chk["dangling"] == ["ev-x-nope"]
        assert card["assessment_status"] == LIMITED
        assert any("悬空" in r for r in card["assessment_reasons"])

    def test_future_reference(self):
        bundle = _bundle(record_pub="2024-11-20T10:00:00+08:00")
        card = build_thesis_card(
            _decision(supporting_evidence_ids=[_rid(bundle)]),
            _state(bundle=bundle, quality=_quality()))
        assert card["evidence_reference_check"]["future"] == [_rid(bundle)]
        assert card["assessment_status"] == LIMITED

    def test_conflicting_reference(self):
        bundle = _bundle()
        rid = _rid(bundle)
        card = build_thesis_card(
            _decision(supporting_evidence_ids=[rid],
                      contradicting_evidence_ids=[rid]),
            _state(bundle=bundle, quality=_quality()))
        assert card["evidence_reference_check"]["conflicting"] == [rid]
        assert any("冲突" in r for r in card["assessment_reasons"])

    def test_bull_bear_evidence_coexist_without_conflict(self):
        b1 = _bundle(title="bull fact", source="CLS Wire")
        delta_bear = {"schema": ledger.SCHEMA_DELTA, "run_id": "run-c2",
                      "events": {"social:c2": ledger.build_fetch_event(
                          "social", "call_s", {
                              "schema": ledger.SCHEMA_ARTIFACT, "tool": "get_news",
                              "provenance": "a_stock", "status": "successful",
                              "requested_window": None,
                              "records": [{"source": "新浪财经", "title": "bear fact",
                                           "content": "b", "url": "",
                                           "published_at": "2024-11-04T10:00:00+08:00",
                                           "time_precision": "datetime"}],
                              "source_statuses": [], "exclusions": {}, "coverage_notes": [],
                          }, trade_date="2024-11-05")}}
        merged = ledger.evidence_reducer(b1, delta_bear)
        rid_sup = next(r["evidence_id"] for r in merged["records"] if r["title"] == "bull fact")
        rid_con = next(r["evidence_id"] for r in merged["records"] if r["title"] == "bear fact")
        card = build_thesis_card(
            _decision(supporting_evidence_ids=[rid_sup],
                      contradicting_evidence_ids=[rid_con]),
            _state(bundle=merged, quality=_quality()))
        chk = card["evidence_reference_check"]
        assert chk["valid_supporting"] == 1 and chk["valid_contradicting"] == 1
        assert chk["conflicting"] == []
        assert card["assessment_status"] == ASSESSABLE

    def test_full_card_assessable(self):
        bundle = _bundle()
        card = build_thesis_card(
            _decision(supporting_evidence_ids=[_rid(bundle)]),
            _state(bundle=bundle, quality=_quality("complete")))
        assert card["assessment_status"] == ASSESSABLE
        assert card["assessment_reasons"] == []

    def test_insufficient_quality_overrides(self):
        bundle = _bundle()
        card = build_thesis_card(
            _decision(supporting_evidence_ids=[_rid(bundle)]),
            _state(bundle=bundle, quality=_quality("insufficient", fail_count=1)))
        assert card["assessment_status"] == INSUFFICIENT

    def test_unknown_without_observable_inputs(self):
        card = build_thesis_card(_decision(), _state())
        assert card["assessment_status"] == UNKNOWN
        assert build_thesis_card(None, _state())["assessment_status"] == UNKNOWN

    def test_freetext_fallback_limited_not_fabricated(self):
        bundle = _bundle()
        card = build_thesis_card(None, _state(bundle=bundle, quality=_quality()))
        assert card["structured_output"] is False
        assert card["rating"] == "unknown"
        assert card["main_thesis"] == ""
        assert card["assessment_status"] == LIMITED
        assert any("未产出结构化假设卡" in r for r in card["assessment_reasons"])

    def test_failed_evidence_source_is_gap(self):
        bundle = _bundle(source_status="failed")
        card = build_thesis_card(
            _decision(supporting_evidence_ids=[_rid(bundle)]),
            _state(bundle=bundle, quality=_quality()))
        assert card["assessment_status"] == LIMITED
        assert any("失败" in r for r in card["assessment_reasons"])

    def test_different_horizons_independent(self):
        bundle = _bundle()
        short = build_thesis_card(
            _decision(time_horizon="days"), _state(bundle=bundle, quality=_quality()))
        long_ = build_thesis_card(
            _decision(time_horizon="6-12 months"), _state(bundle=bundle, quality=_quality()))
        assert short["prediction_horizon"] != long_["prediction_horizon"]
        assert short is not long_ and short != long_

    def test_condition_observability(self):
        full = ThesisCondition(description="d", indicator="i", comparator="<",
                               threshold="0", period="next quarter", source="季报")
        no_source = ThesisCondition(description="no observation source",
                                    indicator="i", comparator="<",
                                    threshold="0", period="next quarter")
        partial = ThesisCondition(description="free text only")
        card = build_thesis_card(
            _decision(invalidation_conditions=[full, no_source, partial]),
            _state(quality=_quality()))
        conds = card["invalidation_conditions"]
        assert conds[0]["observable"] is True and conds[0]["manual_review"] is False
        # C2 R1: missing observation source → manual review even with the
        # other four parts present.
        assert conds[1]["observable"] is False and conds[1]["manual_review"] is True
        assert conds[2]["observable"] is False and conds[2]["manual_review"] is True
        assert any("待人工判断" in r for r in card["assessment_reasons"])
        assert any(g["code"] == "condition_manual_review" for g in card["assessment_gaps"])

    def test_placeholder_condition_parts_are_missing(self):
        # C2 R1: "unknown" placeholders in every field ≠ observable.
        placeholder = ThesisCondition(
            description="not established", indicator="unknown", comparator="未知",
            threshold="n/a", period="unknown", source="unknown")
        card = build_thesis_card(
            _decision(invalidation_conditions=[placeholder]),
            _state(quality=_quality()))
        cond = card["invalidation_conditions"][0]
        assert cond["indicator"] is None and cond["source"] is None
        assert cond["observable"] is False and cond["manual_review"] is True
        assert card["assessment_status"] == LIMITED

    @pytest.mark.parametrize("horizon", ["unknown", "未知", " N/A ", "待定"])
    def test_placeholder_horizon_is_not_known(self, horizon):
        # C2 R1: a non-empty placeholder horizon is not a declared horizon.
        bundle = _bundle()
        card = build_thesis_card(
            _decision(time_horizon=horizon,
                      supporting_evidence_ids=[_rid(bundle)]),
            _state(bundle=bundle, quality=_quality()))
        assert card["assessment_status"] == LIMITED
        assert any(g["code"] == "horizon_placeholder" for g in card["assessment_gaps"])
        # 原文保留供审计，不臆造
        assert card["prediction_horizon_raw"] == horizon

    def test_condition_evidence_reference_validated(self):
        # C2 R1: condition refs are optional but validated once provided.
        bundle = _bundle()
        cond = ThesisCondition(
            description="cash flow turns negative", indicator="ocf",
            comparator="<", threshold="0", period="next quarter",
            source="季报", evidence_id="ev-does-not-exist")
        card = build_thesis_card(
            _decision(supporting_evidence_ids=[_rid(bundle)],
                      invalidation_conditions=[cond]),
            _state(bundle=bundle, quality=_quality()))
        chk = card["evidence_reference_check"]
        assert "ev-does-not-exist" in chk["dangling"]
        assert "ev-does-not-exist" in chk["by_position"]["condition"]["dangling"]
        assert card["assessment_status"] == LIMITED
        assert any(g["code"] == "condition_ref_dangling" for g in card["assessment_gaps"])

    def test_unknown_time_reference_classified_structurally(self):
        rec = {"source": "s", "title": "no time", "content": "c",
               "published_at": "", "time_precision": "unknown"}
        entry = ledger.build_fetch_event("news", "c9", {
            "schema": ledger.SCHEMA_ARTIFACT, "tool": "get_news",
            "provenance": "a_stock", "status": "successful",
            "requested_window": None, "records": [rec],
            "source_statuses": [], "exclusions": {}, "coverage_notes": [],
        }, trade_date="2024-11-05")
        bundle = ledger.evidence_reducer(None, {
            "schema": ledger.SCHEMA_DELTA, "run_id": "run-c2",
            "events": {"news:c9": entry}})
        rid = bundle["records"][0]["evidence_id"]
        card = build_thesis_card(
            _decision(supporting_evidence_ids=[rid]),
            _state(bundle=bundle, quality=_quality()))
        chk = card["evidence_reference_check"]
        assert rid in chk["unknown_time"]
        assert any(g["code"] == "supporting_ref_unknown_time" for g in card["assessment_gaps"])
        assert card["assessment_status"] == LIMITED

    def test_coverage_notes_survive_into_assessment(self):
        # C2 R1: explicit evidence-bundle coverage gaps must not vanish.
        entry = ledger.build_fetch_event("news", "cn", {
            "schema": ledger.SCHEMA_ARTIFACT, "tool": "get_news",
            "provenance": "a_stock", "status": "successful",
            "requested_window": None, "records": [],
            "source_statuses": [], "exclusions": {},
            "coverage_notes": ["HISTORICAL_COVERAGE_GAP: archival source unavailable"],
        }, trade_date="2024-11-05")
        bundle = ledger.evidence_reducer(None, {
            "schema": ledger.SCHEMA_DELTA, "run_id": "run-c2",
            "events": {"news:cn": entry}})
        other = _bundle()
        merged = ledger.evidence_reducer(other, {
            "schema": ledger.SCHEMA_DELTA, "run_id": "run-c2",
            "events": {"news:cn2": entry}})
        rid = _rid(other)
        card = build_thesis_card(
            _decision(supporting_evidence_ids=[rid]),
            _state(bundle=merged, quality=_quality()))
        assert card["assessment_status"] != ASSESSABLE
        assert any("HISTORICAL_COVERAGE_GAP" in r for r in card["assessment_reasons"])
        assert any(g["code"] == "evidence_coverage_note" for g in card["assessment_gaps"])

    def test_failed_event_status_is_gap_even_without_statuses(self):
        entry = ledger.build_fetch_event("news", "cf", {
            "schema": ledger.SCHEMA_ARTIFACT, "tool": "get_news",
            "provenance": "a_stock", "status": "failed",
            "requested_window": None, "records": [],
            "source_statuses": [], "exclusions": {}, "coverage_notes": [],
        }, trade_date="2024-11-05")
        bundle = ledger.evidence_reducer(None, {
            "schema": ledger.SCHEMA_DELTA, "run_id": "run-c2",
            "events": {"news:cf": entry}})
        card = build_thesis_card(
            _decision(supporting_evidence_ids=[]),
            _state(bundle=bundle, quality=_quality()))
        assert any(g["code"] == "failed_evidence_event" for g in card["assessment_gaps"])
        assert card["assessment_status"] == LIMITED

    def test_missing_analysis_date_blocks_assessable(self):
        # C2 R1: no trade_date → no temporal anchor → not assessable.
        bundle = _bundle()
        state = _state(bundle=bundle, quality=_quality())
        state.pop("trade_date")
        card = build_thesis_card(
            _decision(supporting_evidence_ids=[_rid(bundle)]), state)
        assert card["analysis_date"] == "unknown"
        assert card["assessment_status"] != ASSESSABLE
        assert any(g["code"] == "analysis_date_unverifiable" for g in card["assessment_gaps"])
        # 引用时点未验证（不是"通过"）
        chk = card["evidence_reference_check"]
        assert _rid(bundle) in chk["unverifiable_cutoff"]

    def test_invalid_analysis_date_same_as_missing(self):
        bundle = _bundle()
        state = _state(bundle=bundle, quality=_quality(), trade_date="not-a-date")
        card = build_thesis_card(
            _decision(supporting_evidence_ids=[_rid(bundle)]), state)
        assert any(g["code"] == "analysis_date_unverifiable" for g in card["assessment_gaps"])
        assert card["assessment_status"] == LIMITED

    def test_complete_explicit_control_stays_assessable(self):
        # 禁止一刀切降级：完整显式输入仍然 assessable。
        bundle = _bundle()
        rid = _rid(bundle)
        card = build_thesis_card(
            _decision(supporting_evidence_ids=[rid],
                      invalidation_conditions=[ThesisCondition(
                          description="ocf negative", indicator="ocf",
                          comparator="<", threshold="0",
                          period="next disclosed quarter", source="季报",
                          evidence_id=rid)]),
            _state(bundle=bundle, quality=_quality()))
        assert card["assessment_status"] == ASSESSABLE
        assert card["assessment_gaps"] == []


class TestStatusLineAndRender:
    def _card(self):
        bundle = _bundle()
        return build_thesis_card(
            _decision(supporting_evidence_ids=["ev-x-nope"]),
            _state(bundle=bundle, quality=_quality()))

    def test_status_line_matches_card(self):
        card = self._card()
        line = render_thesis_status_line(card)
        assert card["assessment_status"] in line
        assert "uncalibrated" in line
        assert "胜率" in line and "不构成投资胜率" in line

    def test_append_idempotent_and_survives(self):
        card = self._card()
        text = append_thesis_status("decision body", card)
        once = append_thesis_status(text, card)
        assert once == text
        assert text.count("🧪 研究假设卡评估") == 1

    def test_append_empty_text(self):
        card = self._card()
        assert append_thesis_status("", card) == render_thesis_status_line(card)

    def test_render_thesis_md_card_and_legacy(self):
        card = self._card()
        md = render_thesis_md(card)
        assert "研究假设卡" in md and "600519" in md
        assert "悬空" in md  # 引用校验呈现
        assert "uncalibrated" in md and "不代表投资准确率" in md
        legacy = render_thesis_md(None)
        assert "未记录" in legacy
        assert not is_thesis_card({"schema_version": 1})


class _FakeStructured:
    def __init__(self, decision):
        self.decision = decision
        self.calls = 0

    def invoke(self, prompt, config=None):
        self.calls += 1
        return self.decision


class _FakePM:
    """Structured-capable fake: ONE structured call produces text + card."""

    def __init__(self, decision):
        self._s = _FakeStructured(decision)
        self.calls = 0

    def with_structured_output(self, schema):
        return self._s

    def invoke(self, prompt, config=None):
        self.calls += 1
        return SimpleNamespace(content="fallback text")


class _FakeFreeTextPM:
    def __init__(self):
        self.calls = 0

    def invoke(self, prompt, config=None):
        self.calls += 1
        return SimpleNamespace(content="free text decision")

    def with_structured_output(self, schema):
        raise NotImplementedError


_RISK_STATE = {
    "history": "risk debate", "count": 1, "aggressive_history": "",
    "conservative_history": "", "neutral_history": "", "latest_speaker": "",
    "current_aggressive_response": "", "current_conservative_response": "",
    "current_neutral_response": "", "judge_decision": "",
}


class TestPMNodeIntegration:
    def _state(self, **kw):
        state = _state(bundle=_bundle(), quality=_quality())
        state.update({
            "risk_debate_state": dict(_RISK_STATE),
            "investment_plan": "plan",
            "trader_investment_plan": "trader plan",
            "past_context": "",
        })
        state.update(kw)
        return state

    def test_single_structured_call_builds_card(self):
        from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
        decision = _decision(supporting_evidence_ids=[_rid(_bundle())])
        llm = _FakePM(decision)
        node = create_portfolio_manager(llm)
        out = node(self._state())
        assert llm._s.calls == 1 and llm.calls == 0  # 沿用同一调用，格式不加调用
        assert is_thesis_card(out["thesis_card"])
        assert out["thesis_card"]["structured_output"] is True
        assert out["thesis_card"]["assessment_status"] == ASSESSABLE
        assert "🧪 研究假设卡评估" in out["final_trade_decision"]
        assert "**Rating**: Overweight" in out["final_trade_decision"]

    def test_freetext_path_builds_unknown_card_with_status(self):
        from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
        llm = _FakeFreeTextPM()
        node = create_portfolio_manager(llm)
        out = node(self._state())
        card = out["thesis_card"]
        assert card["structured_output"] is False
        assert card["assessment_status"] == LIMITED
        assert "🧪 研究假设卡评估" in out["final_trade_decision"]


class TestPersistenceAndOutlets:
    def test_log_state_saves_card_and_legacy(self, tmp_path):
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
        graph.config = {"results_dir": str(tmp_path)}
        graph.ticker = "600519"
        graph.log_states_dict = {}
        graph._log_state("2024-11-05", {"company_of_interest": "600519"})
        path = tmp_path / "600519/TradingAgentsStrategy_logs/full_states_log_2024-11-05.json"
        assert json.loads(path.read_text(encoding="utf-8"))["thesis_card"] is None

        card = build_thesis_card(_decision(), _state())
        graph._log_state("2024-11-05", {"company_of_interest": "600519",
                                        "thesis_card": card})
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["thesis_card"]["instrument"] == "600519"

    def test_markdown_export_includes_thesis_section(self):
        from web.pdf_export import generate_markdown
        card = build_thesis_card(_decision(), _state())
        md = generate_markdown(
            {"company_of_interest": "600519", "final_trade_decision": "x",
             "thesis_card": card}, "600519", "2024-11-05", "Buy")
        assert "研究假设卡" in md
        legacy = generate_markdown(
            {"company_of_interest": "600519", "final_trade_decision": "x"},
            "600519", "2024-11-05", "Buy")
        assert "未记录" in legacy

    def test_card_json_serializable(self):
        card = build_thesis_card(_decision(), _state(bundle=_bundle(), quality=_quality()))
        assert json.loads(json.dumps(card, ensure_ascii=False)) == card


class TestIndexPath:
    """Codex C1 R2: index trader / index portfolio manager factories must
    receive the evidence index and produce thesis cards on the index path
    (same contract, no single-stock valuation semantics)."""

    def _state(self, **kw):
        state = _state(bundle=_bundle(), quality=_quality())
        state.update({
            "company_of_interest": "000001.SH",  # index id, not a stock code
            "risk_debate_state": dict(_RISK_STATE),
            "investment_plan": "index plan",
            "trader_investment_plan": "directional plan",
            "past_context": "",
        })
        state.update(kw)
        return state

    def test_index_trader_prompt_receives_evidence(self):
        from tradingagents.agents.index_agents import create_index_trader

        class _Capture:
            def __init__(self):
                self.prompts = []

            def invoke(self, prompt, config=None):
                self.prompts.append(prompt)
                return SimpleNamespace(content="directional view")

            def with_structured_output(self, schema):
                raise NotImplementedError

        llm = _Capture()
        create_index_trader(llm)(self._state())
        assert "证据索引" in str(llm.prompts[0])
        assert _bundle()["records"][0]["evidence_id"] in str(llm.prompts[0])
        # 旧 state（无证据账本）不凭空生成
        llm2 = _Capture()
        create_index_trader(llm2)(self._state(evidence_bundle=None))
        assert "证据索引" not in str(llm2.prompts[0])

    def test_index_pm_builds_card_single_call(self):
        from tradingagents.agents.index_agents import create_index_portfolio_manager
        rid = _rid(_bundle())
        decision = _decision(supporting_evidence_ids=[rid])
        llm = _FakePM(decision)
        node = create_index_portfolio_manager(llm)
        out = node(self._state())
        assert llm._s.calls == 1 and llm.calls == 0
        card = out["thesis_card"]
        assert is_thesis_card(card)
        assert card["structured_output"] is True
        assert card["instrument"] == "000001.SH"
        assert card["assessment_status"] == ASSESSABLE
        assert "🧪 研究假设卡评估" in out["final_trade_decision"]
        # 指数语义：不携带个股估值/仓位表述
        assert "T+1" not in out["thesis_card"]["main_thesis"]

    def test_index_pm_freetext_card(self):
        from tradingagents.agents.index_agents import create_index_portfolio_manager
        llm = _FakeFreeTextPM()
        out = create_index_portfolio_manager(llm)(self._state())
        assert out["thesis_card"]["structured_output"] is False
        assert out["thesis_card"]["rating"] == "unknown"
        assert "🧪 研究假设卡评估" in out["final_trade_decision"]
