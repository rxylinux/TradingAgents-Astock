"""Codex independent C2 observable-gap contracts; ZCode must not edit."""

import pytest

from tradingagents.agents.schemas import PortfolioDecision, ThesisCondition
from tradingagents.agents.thesis import build_thesis_card
from tradingagents.evidence import ledger


def inputs(notes=()):
    record = ledger.normalize_record(
        source="official", title="cash flow disclosure", content="reported cash flow",
        published_at="2026-01-01T10:00:00+08:00", time_precision="datetime",
    )
    artifact = {
        "schema": ledger.SCHEMA_ARTIFACT, "tool": "get_news", "status": "successful",
        "provenance": "a_stock", "records": [record], "source_statuses": [],
        "exclusions": {}, "coverage_notes": list(notes),
    }
    event = ledger.build_fetch_event("news", "call-one", artifact, trade_date="2026-01-01")
    bundle = ledger.evidence_reducer(None, {
        "schema": ledger.SCHEMA_DELTA, "run_id": "run-one", "events": {"news:one": event},
    })
    state = {
        "trade_date": "2026-01-01", "company_of_interest": "600519",
        "run_metadata": {"run_id": "run-one"}, "evidence_bundle": bundle,
        "data_quality": {"schema_version": 1, "status": "complete"},
    }
    decision = PortfolioDecision(
        rating="Buy", executive_summary="summary", investment_thesis="cash flow thesis",
        time_horizon="next quarter", supporting_evidence_ids=[record["evidence_id"]],
        invalidation_conditions=[ThesisCondition(
            description="cash flow turns negative", indicator="operating cash flow",
            comparator="<", threshold="0", period="next disclosed quarter",
            source="quarterly report", evidence_id=record["evidence_id"],
        )],
    )
    return decision, state


def test_complete_explicit_card_is_assessable():
    decision, state = inputs()
    assert build_thesis_card(decision, state)["assessment_status"] == "assessable"


@pytest.mark.parametrize("horizon", ["unknown", "未知"])
def test_placeholder_prediction_horizon_is_not_known(horizon):
    decision, state = inputs()
    decision.time_horizon = horizon
    card = build_thesis_card(decision, state)
    assert card["assessment_status"] != "assessable", card
    assert card["assessment_reasons"]


def test_placeholder_condition_parts_require_manual_review():
    decision, state = inputs()
    decision.invalidation_conditions = [ThesisCondition(
        description="not established", indicator="unknown", comparator="unknown",
        threshold="unknown", period="unknown", source="unknown",
    )]
    card = build_thesis_card(decision, state)
    assert card["invalidation_conditions"][0]["manual_review"] is True, card
    assert card["assessment_status"] != "assessable"


def test_missing_condition_observation_source_requires_manual_review():
    decision, state = inputs()
    decision.invalidation_conditions[0].source = None
    card = build_thesis_card(decision, state)
    assert card["invalidation_conditions"][0]["manual_review"] is True, card


def test_condition_evidence_reference_is_checked_too():
    decision, state = inputs()
    decision.invalidation_conditions[0].evidence_id = "ev-does-not-exist"
    card = build_thesis_card(decision, state)
    assert card["assessment_status"] != "assessable", card
    assert "ev-does-not-exist" in str(card["evidence_reference_check"]), card


def test_known_coverage_gap_survives_into_card_assessment():
    decision, state = inputs(notes=["HISTORICAL_COVERAGE_GAP: archival source unavailable"])
    card = build_thesis_card(decision, state)
    assert card["assessment_status"] != "assessable", card
    assert "HISTORICAL_COVERAGE_GAP" in str(card["assessment_reasons"]), card


def test_missing_analysis_date_does_not_claim_assessable():
    decision, state = inputs()
    state.pop("trade_date")
    card = build_thesis_card(decision, state)
    assert card["analysis_date"] == "unknown"
    assert card["assessment_status"] != "assessable", card
