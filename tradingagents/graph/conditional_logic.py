# TradingAgents/graph/conditional_logic.py

from tradingagents.agents.utils.agent_states import AgentState


class ConditionalLogic:
    """Handles conditional logic for determining graph flow."""

    def __init__(
        self,
        max_debate_rounds=1,
        max_risk_discuss_rounds=1,
        enable_early_stopping=False,
    ):
        """Initialize with configuration parameters."""
        self.max_debate_rounds = max_debate_rounds
        self.max_risk_discuss_rounds = max_risk_discuss_rounds
        self.enable_early_stopping = enable_early_stopping

    def should_continue_market(self, state: AgentState):
        """Determine if market analysis should continue."""
        messages = state.get("messages", [])
        if not messages:
            return "Msg Clear Market"
        last_message = messages[-1]
        if getattr(last_message, "tool_calls", None):
            return "tools_market"
        return "Msg Clear Market"

    def should_continue_social(self, state: AgentState):
        """Determine if social media analysis should continue."""
        messages = state.get("messages", [])
        if not messages:
            return "Msg Clear Social"
        last_message = messages[-1]
        if getattr(last_message, "tool_calls", None):
            return "tools_social"
        return "Msg Clear Social"

    def should_continue_news(self, state: AgentState):
        """Determine if news analysis should continue."""
        messages = state.get("messages", [])
        if not messages:
            return "Msg Clear News"
        last_message = messages[-1]
        if getattr(last_message, "tool_calls", None):
            return "tools_news"
        return "Msg Clear News"

    def should_continue_fundamentals(self, state: AgentState):
        """Determine if fundamentals analysis should continue."""
        messages = state.get("messages", [])
        if not messages:
            return "Msg Clear Fundamentals"
        last_message = messages[-1]
        if getattr(last_message, "tool_calls", None):
            return "tools_fundamentals"
        return "Msg Clear Fundamentals"

    def should_continue_policy(self, state: AgentState):
        """Determine if policy analysis should continue."""
        messages = state.get("messages", [])
        if not messages:
            return "Msg Clear Policy"
        last_message = messages[-1]
        if getattr(last_message, "tool_calls", None):
            return "tools_policy"
        return "Msg Clear Policy"

    def should_continue_hot_money(self, state: AgentState):
        """Determine if hot money tracking should continue."""
        messages = state.get("messages", [])
        if not messages:
            return "Msg Clear Hot_money"
        last_message = messages[-1]
        if getattr(last_message, "tool_calls", None):
            return "tools_hot_money"
        return "Msg Clear Hot_money"

    def should_continue_lockup(self, state: AgentState):
        """Determine if lockup/reduction analysis should continue."""
        messages = state.get("messages", [])
        if not messages:
            return "Msg Clear Lockup"
        last_message = messages[-1]
        if getattr(last_message, "tool_calls", None):
            return "tools_lockup"
        return "Msg Clear Lockup"

    def should_continue_debate(self, state: AgentState) -> str:
        """Determine if debate should continue."""
        debate_state = state.get("investment_debate_state", {})
        count = debate_state.get("count", 0)

        # 1. Reach max debate rounds
        if count >= 2 * self.max_debate_rounds:
            return "Research Manager"

        # 2. Early stopping check
        if self.enable_early_stopping:
            if debate_state.get("early_stop") or state.get("early_stop_debate"):
                return "Research Manager"
            curr_resp = debate_state.get("current_response", "")
            if count >= 2 and any(tag in curr_resp for tag in ("[CONSENSUS]", "[AGREE]", "[CONVERGED]", "达成共识")):
                return "Research Manager"

        if debate_state.get("current_response", "").startswith("Bull"):
            return "Bear Researcher"
        return "Bull Researcher"

    def should_continue_risk_analysis(self, state: AgentState) -> str:
        """Determine if risk analysis should continue."""
        risk_state = state.get("risk_debate_state", {})
        count = risk_state.get("count", 0)

        # 1. Reach max risk discussion rounds
        if count >= 3 * self.max_risk_discuss_rounds:
            return "Portfolio Manager"

        # 2. Early stopping check
        if self.enable_early_stopping:
            if risk_state.get("early_stop") or state.get("early_stop_risk"):
                return "Portfolio Manager"
            if count >= 3:
                responses = [
                    risk_state.get("current_aggressive_response", ""),
                    risk_state.get("current_conservative_response", ""),
                    risk_state.get("current_neutral_response", ""),
                ]
                if any(any(tag in r for tag in ("[CONSENSUS]", "[AGREE]", "[CONVERGED]", "达成共识")) for r in responses if r):
                    return "Portfolio Manager"

        speaker = risk_state.get("latest_speaker", "")
        if speaker.startswith("Aggressive"):
            return "Conservative Analyst"
        if speaker.startswith("Conservative"):
            return "Neutral Analyst"
        return "Aggressive Analyst"

