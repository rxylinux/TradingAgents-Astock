"""Graph-specific news tools with evidence artifacts (C1).

These keep the public contract of ``agents.utils.news_data_tools`` —
identical tool name, args, docstring, and plain-text ``.invoke()`` result —
but are built with ``response_format="content_and_artifact"`` so a real
LangGraph ToolNode emits ``ToolMessage.artifact`` carrying the structured
fetch result. Text and artifact come from the SAME vendor fetch via
``route_to_vendor_with_evidence`` (which respects the configured vendor
chain — old string vendors yield provenance-unknown artifacts).

The model never controls evidence collection: there is no ``return_evidence``
parameter, and run identity is attached later by the branch-isolated tool
node wrapper from graph state.
"""

from typing import Annotated

from langchain_core.tools import tool

from tradingagents.agents.utils.news_data_tools import _validate_a_stock_code
from tradingagents.dataflows.interface import route_to_vendor_with_evidence
from tradingagents.evidence.cutoff import clamp_end_date
from tradingagents.evidence.ledger import SCHEMA_ARTIFACT


def _error_artifact(tool_name: str, exc: Exception) -> dict:
    return {
        "schema": SCHEMA_ARTIFACT,
        "tool": tool_name,
        "provenance": "unknown",
        "status": "failed",
        "requested_window": None,
        "records": [],
        "source_statuses": [],
        "exclusions": {},
        "coverage_notes": [f"工具执行异常：{type(exc).__name__}: {exc}"],
    }


def _clamp_note(requested: str, effective: str) -> str:
    return (
        f"请求的日期 {requested} 超过本次分析时点 {effective}，"
        f"已按分析时点截断（防止未来数据进入分析）"
    )


def _apply_clamp(text: str, payload: dict, requested: str, effective: str) -> tuple[str, dict]:
    """Surface a date clamp in BOTH the model-visible text and the artifact."""
    note = _clamp_note(requested, effective)
    text = f"{text}\n---\n[注: {note}]\n" if text else f"[注: {note}]"
    payload = dict(payload)
    payload["coverage_notes"] = list(payload.get("coverage_notes") or []) + [note]
    payload["requested_window"] = dict(payload.get("requested_window") or {})
    payload["requested_window"]["model_requested_end"] = requested
    payload["requested_window"]["effective_end"] = effective
    return text, payload


@tool(response_format="content_and_artifact")
def get_news(
    ticker: Annotated[str, "6-digit A-stock code (e.g. 600379). Must be numeric, NOT company name or Chinese text"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> tuple[str, object]:
    """
    Retrieve news data for a given stock code.
    Uses the configured news_data vendor.
    Args:
        ticker (str): 6-digit A-stock code, e.g. 600379, 300750. Must be the numeric code, not the company name.
        start_date (str): Start date in yyyy-mm-dd format
        end_date (str): End date in yyyy-mm-dd format
    Returns:
        str: A formatted string containing news data
    """
    ok, code_or_message = _validate_a_stock_code("get_news", ticker)
    if not ok:
        # Invalid ticker: no fetch happened → no evidence artifact (None).
        return code_or_message, None
    requested_end = end_date
    end_date, clamped = clamp_end_date(end_date)
    try:
        text, payload = route_to_vendor_with_evidence(
            "get_news", code_or_message, start_date, end_date
        )
    except Exception as exc:
        # Same failure visibility the stock ToolNode error handler gives the
        # model, plus a failed-status artifact so the fetch failure is
        # recorded in the evidence ledger instead of vanishing.
        return (
            f"Error retrieving news data for {code_or_message}: {exc}",
            _error_artifact("get_news", exc),
        )
    if clamped:
        text, payload = _apply_clamp(text, payload, requested_end, end_date)
    return text, payload


@tool(response_format="content_and_artifact")
def get_global_news(
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format"],
    look_back_days: Annotated[int, "Number of days to look back"] = 7,
    limit: Annotated[int, "Maximum number of articles to return"] = 5,
) -> tuple[str, object]:
    """
    Retrieve global news data.
    Uses the configured news_data vendor.
    Args:
        curr_date (str): Current date in yyyy-mm-dd format
        look_back_days (int): Number of days to look back (default 7)
        limit (int): Maximum number of articles to return (default 5)
    Returns:
        str: A formatted string containing global news data
    """
    requested_curr = curr_date
    curr_date, clamped = clamp_end_date(curr_date)
    try:
        text, payload = route_to_vendor_with_evidence(
            "get_global_news", curr_date, look_back_days, limit
        )
    except Exception as exc:
        return (
            f"Error retrieving global news data: {exc}",
            _error_artifact("get_global_news", exc),
        )
    if clamped:
        text, payload = _apply_clamp(text, payload, requested_curr, curr_date)
    return text, payload
