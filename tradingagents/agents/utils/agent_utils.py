from langchain_core.messages import HumanMessage, RemoveMessage

# Import tools from separate utility files
from tradingagents.agents.utils.core_stock_tools import (
    get_stock_data
)
from tradingagents.agents.utils.technical_indicators_tools import (
    get_indicators
)
from tradingagents.agents.utils.fundamental_data_tools import (
    get_fundamentals,
    get_balance_sheet,
    get_cashflow,
    get_income_statement
)
from tradingagents.agents.utils.news_data_tools import (
    get_news,
    get_insider_transactions,
    get_global_news
)
from tradingagents.agents.utils.signal_data_tools import (
    get_profit_forecast,
    get_hot_stocks,
    get_northbound_flow,
    get_concept_blocks,
    get_fund_flow,
    get_dragon_tiger_board,
    get_lockup_expiry,
    get_industry_comparison,
)


def get_language_instruction() -> str:
    """Return a prompt instruction for the configured output language.

    Returns empty string when English (default), so no extra tokens are used.
    Only applied to user-facing agents (analysts, portfolio manager).
    Internal debate agents stay in English for reasoning quality.
    """
    from tradingagents.dataflows.config import get_config
    lang = get_config().get("output_language", "English")
    if lang.strip().lower() == "english":
        return ""
    return f" Write your entire response in {lang}."


def build_instrument_context(ticker: str) -> str:
    """Describe the exact instrument so agents preserve exchange-qualified tickers."""
    return (
        f"The instrument to analyze is `{ticker}`. "
        "Use this exact ticker in every tool call, report, and recommendation, "
        "preserving any exchange suffix (e.g. `.TO`, `.L`, `.HK`, `.T`). "
        "When a tool argument is named `ticker`, pass only this ticker value; "
        "do not pass company names, sectors, concepts, or search keywords."
    )

def create_msg_delete():
    def delete_messages(state):
        """Pass-through clean state for parallel Fan-Out/Fan-In topology.

        In the parallel topology, multiple analyst branches complete concurrently.
        Returning an empty dict avoids concurrent RemoveMessage operations on the shared
        messages channel that cause message ID deletion conflicts.
        """
        return {}

    return delete_messages


def filter_analyst_messages(messages, tools=None, company_of_interest=""):
    """Filter messages for an analyst in parallel Fan-Out execution.

    Prevents cross-branch message collision and OpenAI BadRequestError (400)
    when another concurrent branch's AIMessage with tool_calls has not yet received
    its ToolMessages.
    """
    valid_tool_names = {t.name for t in tools} if tools else set()
    filtered = []

    # 1. First find or construct initial HumanMessage
    human_msg = None
    if messages:
        for m in messages:
            if getattr(m, "type", None) == "human" or m.__class__.__name__ == "HumanMessage":
                human_msg = m
                break
    if human_msg is None:
        human_msg = HumanMessage(content=str(company_of_interest or "Analyze"))
    filtered.append(human_msg)

    # 2. Build index of ToolMessages by tool_call_id
    tool_msgs_by_id = {}
    if messages:
        for m in messages:
            if getattr(m, "type", None) == "tool" or m.__class__.__name__ == "ToolMessage":
                tc_id = getattr(m, "tool_call_id", None)
                if tc_id:
                    tool_msgs_by_id[tc_id] = m

    # 3. Only keep AIMessages whose tool_calls belong to this analyst's tools
    # AND where every tool_call_id has a corresponding ToolMessage
    if messages:
        for m in messages:
            if getattr(m, "type", None) == "ai" or m.__class__.__name__ == "AIMessage":
                tcs = getattr(m, "tool_calls", None) or []
                if tcs:
                    if valid_tool_names and all(tc.get("name") in valid_tool_names for tc in tcs):
                        if all(tc.get("id") in tool_msgs_by_id for tc in tcs):
                            filtered.append(m)
                            for tc in tcs:
                                filtered.append(tool_msgs_by_id[tc["id"]])

    return filtered



        
