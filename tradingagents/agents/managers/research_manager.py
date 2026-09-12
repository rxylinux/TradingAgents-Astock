"""Research Manager: turns the bull/bear debate into a structured investment plan for the trader."""

from __future__ import annotations

from tradingagents.agents.schemas import ResearchPlan, render_research_plan
from tradingagents.agents.utils.agent_utils import build_instrument_context, get_language_instruction
from tradingagents.agents.report_quality import quality_context_for_prompt
from tradingagents.evidence.prompt_context import evidence_context_for_prompt
from tradingagents.dataflows.financial_panel import panel_context_for_prompt
from tradingagents.agents.debate_evidence import evidence_debate_summary_for_prompt
from tradingagents.evaluation.review_projection import projection_for_prompt
from tradingagents.agents.utils.structured import (
    bind_structured,
    invoke_structured_or_freetext,
)


def create_research_manager(llm):
    structured_llm = bind_structured(llm, ResearchPlan, "Research Manager")

    def research_manager_node(state) -> dict:
        instrument_context = build_instrument_context(state["company_of_interest"])
        history = state["investment_debate_state"].get("history", "")
        # N01: 质量限制直接来自结构化质量卡（不依赖辩论转述）；旧 state 无
        # 结构化记录时空段，保持原行为。
        quality_block = quality_context_for_prompt(state)
        # C1: 证据索引（旧 state 无证据账本时空段）
        evidence_block = evidence_context_for_prompt(state)
        # D2: 财务面板投影（仅 ok 数值+实际依赖；未配置时空段）
        panel_block = panel_context_for_prompt(state)
        # E: 独立初判与分歧核查受限摘要（未启用时空段）
        e_debate_block = evidence_debate_summary_for_prompt(state)
        # F2: 历史经验只读投影（未启用/无效 → 空/受控说明）
        review_block = projection_for_prompt(state)

        investment_debate_state = state["investment_debate_state"]

        prompt = f"""As the Research Manager and debate facilitator, your role is to critically evaluate this round of debate and deliver a clear, actionable investment plan for the trader.

{instrument_context}

Note: This is an A-share (China mainland) stock. Factor in regulatory policy impact, hot money / capital flow dynamics, and lockup expiry / insider reduction risks when synthesising the debate.

---

**Rating Scale** (use exactly one):
- **Buy**: Strong conviction in the bull thesis; recommend taking or growing the position
- **Overweight**: Constructive view; recommend gradually increasing exposure
- **Hold**: Balanced view; recommend maintaining the current position
- **Underweight**: Cautious view; recommend trimming exposure
- **Sell**: Strong conviction in the bear thesis; recommend exiting or avoiding the position

Commit to a clear stance whenever the debate's strongest arguments warrant one; reserve Hold for situations where the evidence on both sides is genuinely balanced.

{quality_block}{evidence_block}{panel_block}{e_debate_block}{review_block}
---

**Debate History:**
{history}""" + get_language_instruction()

        investment_plan = invoke_structured_or_freetext(
            structured_llm,
            llm,
            prompt,
            render_research_plan,
            "Research Manager",
        )

        new_investment_debate_state = {
            "judge_decision": investment_plan,
            "history": investment_debate_state.get("history", ""),
            "bear_history": investment_debate_state.get("bear_history", ""),
            "bull_history": investment_debate_state.get("bull_history", ""),
            "current_response": investment_plan,
            "count": investment_debate_state["count"],
        }

        return {
            "investment_debate_state": new_investment_debate_state,
            "investment_plan": investment_plan,
        }

    return research_manager_node
