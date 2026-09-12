"""Pydantic schemas used by agents that produce structured output.

The framework's primary artifact is still prose: each agent's natural-language
reasoning is what users read in the saved markdown reports and what the
downstream agents read as context.  Structured output is layered onto the
three decision-making agents (Research Manager, Trader, Portfolio Manager)
so that:

- Their outputs follow consistent section headers across runs and providers
- Each provider's native structured-output mode is used (json_schema for
  OpenAI/xAI, response_schema for Gemini, tool-use for Anthropic)
- Schema field descriptions become the model's output instructions, freeing
  the prompt body to focus on context and the rating-scale guidance
- A render helper turns the parsed Pydantic instance back into the same
  markdown shape the rest of the system already consumes, so display,
  memory log, and saved reports keep working unchanged
"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Shared rating types
# ---------------------------------------------------------------------------


class PortfolioRating(str, Enum):
    """5-tier rating used by the Research Manager and Portfolio Manager."""

    BUY = "Buy"
    OVERWEIGHT = "Overweight"
    HOLD = "Hold"
    UNDERWEIGHT = "Underweight"
    SELL = "Sell"


class TraderAction(str, Enum):
    """3-tier transaction direction used by the Trader.

    The Trader's job is to translate the Research Manager's investment plan
    into a concrete transaction proposal: should the desk execute a Buy, a
    Sell, or sit on Hold this round.  Position sizing and the nuanced
    Overweight / Underweight calls happen later at the Portfolio Manager.
    """

    BUY = "Buy"
    HOLD = "Hold"
    SELL = "Sell"


# ---------------------------------------------------------------------------
# Research Manager
# ---------------------------------------------------------------------------


class ResearchPlan(BaseModel):
    """Structured investment plan produced by the Research Manager.

    Hand-off to the Trader: the recommendation pins the directional view,
    the rationale captures which side of the bull/bear debate carried the
    argument, and the strategic actions translate that into concrete
    instructions the trader can execute against.
    """

    recommendation: PortfolioRating = Field(
        description=(
            "The investment recommendation. Exactly one of Buy / Overweight / "
            "Hold / Underweight / Sell. Reserve Hold for situations where the "
            "evidence on both sides is genuinely balanced; otherwise commit to "
            "the side with the stronger arguments."
        ),
    )
    rationale: str = Field(
        description=(
            "Conversational summary of the key points from both sides of the "
            "debate, ending with which arguments led to the recommendation. "
            "Speak naturally, as if to a teammate."
        ),
    )
    strategic_actions: str = Field(
        description=(
            "Concrete steps for the trader to implement the recommendation, "
            "consistent with the rating."
        ),
    )


def render_research_plan(plan: ResearchPlan) -> str:
    """Render a ResearchPlan to markdown for storage and the trader's prompt context."""
    return "\n".join([
        f"**Recommendation**: {plan.recommendation.value}",
        "",
        f"**Rationale**: {plan.rationale}",
        "",
        f"**Strategic Actions**: {plan.strategic_actions}",
    ])


# ---------------------------------------------------------------------------
# Trader
# ---------------------------------------------------------------------------


class TraderProposal(BaseModel):
    """Structured transaction proposal produced by the Trader.

    The trader reads the Research Manager's investment plan and the analyst
    reports, then states a direction and the reasoning behind it.

    It deliberately carries **no executable price levels** — no entry price,
    no stop-loss, no position size. This project is a research and education
    implementation of the upstream TradingAgents framework, and concrete trade
    levels for a named security are what turn a research tool into an
    investment-advisory product. The capability is not shipped here; a
    downstream fork that wants it can add it under its own responsibility.
    """

    action: TraderAction = Field(
        description="The transaction direction. Exactly one of Buy / Hold / Sell.",
    )
    reasoning: str = Field(
        description=(
            "The case for this action, anchored in the analysts' reports and "
            "the research plan. Two to four sentences. Do not quote specific "
            "entry, stop-loss or position-size levels."
        ),
    )


def render_trader_proposal(proposal: TraderProposal) -> str:
    """Render a TraderProposal to markdown.

    The trailing ``FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL**`` line is
    preserved for backward compatibility with the analyst stop-signal text
    and any external code that greps for it.
    """
    return "\n".join([
        f"**Action**: {proposal.action.value}",
        "",
        f"**Reasoning**: {proposal.reasoning}",
        "",
        f"FINAL TRANSACTION PROPOSAL: **{proposal.action.value.upper()}**",
    ])


# ---------------------------------------------------------------------------
# Portfolio Manager
# ---------------------------------------------------------------------------


class PortfolioDecision(BaseModel):
    """Structured output produced by the Portfolio Manager.

    The model fills every field as part of its primary LLM call; no separate
    extraction pass is required. Field descriptions double as the model's
    output instructions, so the prompt body only needs to convey context and
    the rating-scale guidance.

    Like :class:`TraderProposal`, this carries no price target and no other
    executable level — see that class for why.
    """

    rating: PortfolioRating = Field(
        description=(
            "The final position rating. Exactly one of Buy / Overweight / Hold / "
            "Underweight / Sell, picked based on the analysts' debate."
        ),
    )
    executive_summary: str = Field(
        description=(
            "A concise summary of what drove the rating and the main "
            "considerations on each side. Two to four sentences. Do not quote "
            "specific entry, stop-loss, position-size or target-price levels."
        ),
    )
    investment_thesis: str = Field(
        description=(
            "Detailed reasoning anchored in specific evidence from the analysts' "
            "debate. If prior lessons are referenced in the prompt context, "
            "incorporate them; otherwise rely solely on the current analysis."
        ),
    )
    time_horizon: Optional[str] = Field(
        default=None,
        description="Optional analysis horizon, e.g. '3-6 months'.",
    )

    # ---- C2 research thesis card -----------------------------------------
    # All optional with empty defaults: a provider/model that omits them
    # yields "unknown" card fields — nothing is ever fabricated by code.
    # These ride the Portfolio Manager's EXISTING structured-output call;
    # no extra LLM call is added for the format.
    supporting_evidence_ids: List[str] = Field(
        default_factory=list,
        description=(
            "Evidence IDs from the 证据索引 block in the prompt that SUPPORT "
            "the thesis. Only cite IDs that appear there verbatim; if the "
            "block is absent or none apply, return an empty list."
        ),
    )
    contradicting_evidence_ids: List[str] = Field(
        default_factory=list,
        description=(
            "Evidence IDs from the 证据索引 block that CONTRADICT or weaken "
            "the thesis (bear-side facts). Only cite IDs that appear there "
            "verbatim; empty list if none."
        ),
    )
    hypotheses_to_verify: List[str] = Field(
        default_factory=list,
        description=(
            "Assumptions the thesis depends on that are NOT yet verified by "
            "the evidence above. Empty list if none identified."
        ),
    )
    catalysts: List[str] = Field(
        default_factory=list,
        description=(
            "Observable upcoming events that would materially move the "
            "thesis. Do NOT invent specific earnings dates, prices, or "
            "probabilities — only events already named in the analysts' "
            "reports or evidence."
        ),
    )
    invalidation_conditions: List["ThesisCondition"] = Field(
        default_factory=list,
        description=(
            "Conditions under which this thesis should be abandoned. Prefer "
            "observable metric conditions (indicator + comparator + "
            "threshold + period, e.g. '经营现金流 同比 < 0 下一已披露季度'); "
            "free-text-only conditions are allowed but will be marked for "
            "manual review. Empty list if none."
        ),
    )
    next_check_trigger: Optional[str] = Field(
        default=None,
        description=(
            "When this thesis should be re-examined, anchored to an "
            "observable trigger already named in the analysis (e.g. an "
            "event or report); 'unknown' if nothing observable is available."
        ),
    )


class ThesisCondition(BaseModel):
    """One thesis-invalidation condition (C2).

    ``observable``/``manual_review`` are NOT model fields — code derives
    them from whether the measurable parts are present.
    """

    description: str = Field(description="The condition in one sentence.")
    indicator: Optional[str] = Field(
        default=None, description="Metric to watch, e.g. 经营现金流.")
    comparator: Optional[str] = Field(
        default=None, description="Comparison, e.g. 同比转负 / < / ≥.")
    threshold: Optional[str] = Field(
        default=None, description="Threshold value, e.g. 0 / -20%.")
    period: Optional[str] = Field(
        default=None, description="Period or trigger event, e.g. 下一已披露季度.")
    source: Optional[str] = Field(
        default=None, description="Where this will be observable, e.g. 季报.")
    evidence_id: Optional[str] = Field(
        default=None, description="Supporting evidence ID from 证据索引, if any.")


def render_pm_decision(decision: PortfolioDecision) -> str:
    """Render a PortfolioDecision back to the markdown shape the rest of the system expects.

    Memory log, CLI display, and saved report files all read this markdown,
    so the rendered output preserves the exact section headers (``**Rating**``,
    ``**Executive Summary**``, ``**Investment Thesis**``) that downstream
    parsers and the report writers already handle.
    """
    parts = [
        f"**Rating**: {decision.rating.value}",
        "",
        f"**Executive Summary**: {decision.executive_summary}",
        "",
        f"**Investment Thesis**: {decision.investment_thesis}",
    ]
    if decision.time_horizon:
        parts.extend(["", f"**Time Horizon**: {decision.time_horizon}"])
    return "\n".join(parts)
