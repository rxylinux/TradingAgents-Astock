# TradingAgents/graph/conditional_logic.py

from tradingagents.agents.utils.agent_states import AgentState

# 各分析师工具循环读取的分支消息通道（R1 隔离）：条件路由必须与分析师节点、
# 工具节点看到同一份消息，否则路由判断的是别的分支的输出。
BRANCH_MESSAGE_KEYS = {
    "market": "market_messages",
    "social": "social_messages",
    "news": "news_messages",
    "fundamentals": "fundamentals_messages",
    "policy": "policy_messages",
    "hot_money": "hot_money_messages",
    "lockup": "lockup_messages",
}


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

    def _should_continue_branch(self, state: AgentState, role: str) -> str:
        """Route one analyst branch on its own message channel.

        The last message is produced by this branch's own LLM loop (isolated
        via ``{role}_messages``), so a tool_calls marker here can only belong
        to this branch.
        """
        messages = state.get(BRANCH_MESSAGE_KEYS[role], [])
        if not messages:
            return f"Msg Clear {role.capitalize()}"
        last_message = messages[-1]
        if getattr(last_message, "tool_calls", None):
            return f"tools_{role}"
        return f"Msg Clear {role.capitalize()}"

    def should_continue_market(self, state: AgentState):
        """Determine if market analysis should continue."""
        return self._should_continue_branch(state, "market")

    def should_continue_social(self, state: AgentState):
        """Determine if social media analysis should continue."""
        return self._should_continue_branch(state, "social")

    def should_continue_news(self, state: AgentState):
        """Determine if news analysis should continue."""
        return self._should_continue_branch(state, "news")

    def should_continue_fundamentals(self, state: AgentState):
        """Determine if fundamentals analysis should continue."""
        return self._should_continue_branch(state, "fundamentals")

    def should_continue_policy(self, state: AgentState):
        """Determine if policy analysis should continue."""
        return self._should_continue_branch(state, "policy")

    def should_continue_hot_money(self, state: AgentState):
        """Determine if hot money tracking should continue."""
        return self._should_continue_branch(state, "hot_money")

    def should_continue_lockup(self, state: AgentState):
        """Determine if lockup/reduction analysis should continue."""
        return self._should_continue_branch(state, "lockup")

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

