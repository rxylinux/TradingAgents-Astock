"""Codex D2 independent state identity, provenance and projection limits."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradingagents.dataflows.financial_panel import (
    PANEL_CONTEXT_MAX_TOTAL_CHARS,
    bind_run,
    compute_financial_panel,
    panel_context_for_prompt,
)
from tradingagents.graph.trading_graph import TradingAgentsGraph


FIXTURE = Path(__file__).resolve().parents[1] / "tests/fixtures/financial_panel/stock_normal.json"


def card_and_state():
    manifest = json.loads(FIXTURE.read_text())
    card = bind_run(compute_financial_panel(manifest, instrument="600519",
                                            instrument_type="stock", analysis_date="2024-11-05"), "run-one")
    state = {"financial_panel": card, "company_of_interest": "600519",
             "trade_date": "2024-11-05", "instrument_type": "stock",
             "run_metadata": {"run_id": "run-one", "financial_panel": {
                 "manifest_digest": card["manifest_digest"], "overall_status": card["overall_status"]}}}
    return card, state


def test_valid_panel_projection_still_contains_computed_values():
    _, state = card_and_state()
    assert "20.00%" in panel_context_for_prompt(state)


def test_prompt_helper_rejects_cross_run_card():
    _, state = card_and_state()
    state["run_metadata"]["run_id"] = "run-other"
    block = panel_context_for_prompt(state)
    assert "invalid-panel" in block, block
    assert "20.00%" not in block


def test_prompt_helper_rejects_input_digest_corruption():
    card, state = card_and_state()
    card["inputs"][0]["normalized_value"] = "654321"
    block = panel_context_for_prompt(state)
    assert "invalid-panel" in block, block
    assert "20.00%" not in block


def test_resume_validates_independent_metadata_digest_anchor():
    _, state = card_and_state()
    state["run_metadata"]["financial_panel"]["manifest_digest"] = "sha256:DIFFERENT"
    graph = SimpleNamespace(config={"instrument_type": "stock"})
    before = copy.deepcopy(state)
    with pytest.raises(RuntimeError):
        TradingAgentsGraph._validate_resumed_panel(graph, state, "600519", "2024-11-05")
    assert state == before


def test_real_pm_factory_does_not_receive_cross_run_values():
    from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager

    class CaptureLLM:
        def __init__(self):
            self.prompts = []

        def with_structured_output(self, schema):
            raise NotImplementedError

        def invoke(self, prompt, config=None):
            self.prompts.append(prompt)
            return SimpleNamespace(content="Hold")

    _, state = card_and_state()
    state["run_metadata"]["run_id"] = "run-other"
    risk = {k: "" for k in ("aggressive_history", "conservative_history", "neutral_history",
                            "latest_speaker", "current_aggressive_response", "current_conservative_response",
                            "current_neutral_response", "judge_decision")}
    risk.update(history="risk", count=1)
    state.update(risk_debate_state=risk,
                 investment_plan="plan", trader_investment_plan="plan", past_context="")
    llm = CaptureLLM()
    create_portfolio_manager(llm)(state)
    assert len(llm.prompts) == 1
    assert "20.00%" not in str(llm.prompts[0]), str(llm.prompts[0])[-2000:]
    assert "invalid-panel" in str(llm.prompts[0])


def test_projection_total_limit_includes_truncation_notice():
    manifest = json.loads(FIXTURE.read_text())
    template = next(i for i in manifest["inputs"] if i["metric"] == "revenue")
    for year in range(2014, 2024):
        for metric in ("revenue", "net_profit_total"):
            inp = copy.deepcopy(template)
            inp.update(input_id=f"{year}-{metric}-" + "a" * 150, metric=metric, value=100,
                       disclosed_at="2024-11-01",
                       period={"kind": "annual", "window": "FY", "fiscal_year": year,
                               "period_start": f"{year}-01-01", "period_end": f"{year}-12-31"})
            manifest["inputs"].append(inp)
    card, state = card_and_state()
    card = bind_run(compute_financial_panel(manifest, instrument="600519",
                                            instrument_type="stock", analysis_date="2024-11-05"), "run-one")
    state["financial_panel"] = card
    state["run_metadata"]["financial_panel"]["manifest_digest"] = card["manifest_digest"]
    state["run_metadata"]["financial_panel"]["overall_status"] = card["overall_status"]
    block = panel_context_for_prompt(state)
    assert len(block) <= PANEL_CONTEXT_MAX_TOTAL_CHARS, (len(block), block[-160:])
