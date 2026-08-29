"""Unit tests for debate early stopping and convergence logic."""

import pytest
from tradingagents.graph.conditional_logic import ConditionalLogic


@pytest.mark.unit
def test_conditional_logic_debate_standard_flow():
    cond = ConditionalLogic(max_debate_rounds=2, enable_early_stopping=False)

    # Round 0: starts with Bull
    state_0 = {"investment_debate_state": {"count": 0, "current_response": ""}}
    assert cond.should_continue_debate(state_0) == "Bull Researcher"

    # Bull speaks -> Bear responds
    state_1 = {"investment_debate_state": {"count": 1, "current_response": "Bull Analyst: strong growth"}}
    assert cond.should_continue_debate(state_1) == "Bear Researcher"

    # Reach max rounds (2 * 2 = 4)
    state_4 = {"investment_debate_state": {"count": 4, "current_response": "Bear Analyst: risk noted"}}
    assert cond.should_continue_debate(state_4) == "Research Manager"


@pytest.mark.unit
def test_conditional_logic_debate_early_stopping():
    cond = ConditionalLogic(max_debate_rounds=5, enable_early_stopping=True)

    # Round 2 with consensus marker triggers early stopping
    state_consensus = {
        "investment_debate_state": {
            "count": 2,
            "current_response": "Bear Analyst: [CONSENSUS] I agree with the valuation assessment.",
        }
    }
    assert cond.should_continue_debate(state_consensus) == "Research Manager"

    # Explicit early_stop flag triggers early stopping
    state_flag = {
        "investment_debate_state": {
            "count": 1,
            "early_stop": True,
            "current_response": "Bull Analyst: clear consensus",
        }
    }
    assert cond.should_continue_debate(state_flag) == "Research Manager"


@pytest.mark.unit
def test_conditional_logic_risk_standard_and_early_stopping():
    cond = ConditionalLogic(max_risk_discuss_rounds=3, enable_early_stopping=True)

    # Standard progression
    state_agg = {"risk_debate_state": {"count": 1, "latest_speaker": "Aggressive Analyst"}}
    assert cond.should_continue_risk_analysis(state_agg) == "Conservative Analyst"

    state_cons = {"risk_debate_state": {"count": 2, "latest_speaker": "Conservative Analyst"}}
    assert cond.should_continue_risk_analysis(state_cons) == "Neutral Analyst"

    # Early stop when consensus is reached after full cycle
    state_risk_consensus = {
        "risk_debate_state": {
            "count": 3,
            "latest_speaker": "Neutral Analyst",
            "current_neutral_response": "Neutral Analyst: [AGREE] Position size 30% is agreed upon.",
        }
    }
    assert cond.should_continue_risk_analysis(state_risk_consensus) == "Portfolio Manager"
