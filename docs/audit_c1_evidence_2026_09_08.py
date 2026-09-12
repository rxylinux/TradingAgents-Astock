"""Codex independent C1 boundary tests; implementation agent must not edit."""

from typing import Annotated
from typing_extensions import TypedDict
from unittest.mock import patch

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import END, START, StateGraph, add_messages
from langgraph.prebuilt import ToolNode
from langgraph.checkpoint.memory import MemorySaver

from tradingagents.dataflows import a_stock, interface
from tradingagents.evidence import graph_tools, ledger
from tradingagents.graph.setup import _branch_isolated_tool_node


def make_artifact(record):
    return {"schema": ledger.SCHEMA_ARTIFACT, "tool": "get_news", "status": "successful",
            "provenance": "publication_time_only", "records": [record],
            "source_statuses": [], "exclusions": {}, "coverage_notes": []}


def make_delta(record, role):
    message = ToolMessage(content="offline", tool_call_id=role + "-call", artifact=make_artifact(record))
    return ledger.collect_tool_message_delta([message], role=role, run_id="run-one", trade_date="2026-01-01")


def test_merge_order_independent_when_same_article_retrieved_twice():
    common = dict(source="official", title="same event", content="same content",
                  published_at="2026-01-01T11:00:00+08:00", time_precision="datetime")
    first = ledger.normalize_record(**common, retrieved_at="2026-01-01T12:00:00+08:00")
    second = ledger.normalize_record(**common, retrieved_at="2026-01-01T13:00:00+08:00")
    left, right = make_delta(first, "news"), make_delta(second, "policy")
    a = ledger.evidence_reducer(ledger.evidence_reducer(None, left), right)
    b = ledger.evidence_reducer(ledger.evidence_reducer(None, right), left)
    assert a == b


def test_unknown_timestamp_cannot_be_called_time_compliant():
    record = ledger.normalize_record(source="official", title="unknown-time", published_at="", time_precision="unknown")
    bundle = ledger.evidence_reducer(None, make_delta(record, "news"))
    verdict = ledger.validate_reference(bundle, record["evidence_id"], "2026-01-01T23:59:59+08:00")
    assert not verdict["valid"], verdict


def test_date_only_run_cutoff_compares_without_naive_aware_crash():
    record = ledger.normalize_record(source="official", title="same-day", published_at="2026-01-01T11:00:00+08:00", time_precision="datetime")
    bundle = ledger.evidence_reducer(None, make_delta(record, "news"))
    verdict = ledger.validate_reference(bundle, record["evidence_id"], "2026-01-01")
    assert verdict["valid"], verdict


class NewsState(TypedDict):
    news_messages: Annotated[list, add_messages]
    evidence_bundle: Annotated[dict | None, ledger.evidence_reducer]
    run_metadata: dict
    trade_date: str


def test_real_tool_model_date_cannot_bypass_trusted_run_cutoff():
    graph = StateGraph(NewsState)
    graph.add_node("news_tools", _branch_isolated_tool_node("news", ToolNode([graph_tools.get_news])))
    graph.add_edge(START, "news_tools")
    graph.add_edge("news_tools", END)
    message = AIMessage(content="", tool_calls=[{
        "name": "get_news", "id": "future-call",
        "args": {"ticker": "600519", "start_date": "2026-01-01", "end_date": "2027-01-01"},
    }])
    articles = [{"title": "FUTURE_ONLY", "time": "2027-01-01 11:00:00", "content": "FUTURE_BODY"}]
    with patch.object(a_stock, "_fetch_news_eastmoney", return_value=articles), \
         patch.object(a_stock, "_fetch_news_sina", return_value=[]), \
         patch.object(interface, "get_vendor", return_value="a_stock"):
        result = graph.compile().invoke({"news_messages": [message], "evidence_bundle": None,
                                         "run_metadata": {"run_id": "run-one"}, "trade_date": "2026-01-01"})
    reply = result["news_messages"][-1]
    assert "FUTURE_ONLY" not in str(reply.content)
    assert "FUTURE_BODY" not in str(reply.content)
    assert "FUTURE_ONLY" not in str(result.get("evidence_bundle"))


def test_real_checkpoint_resume_keeps_first_event_and_adds_second():
    tool_node = _branch_isolated_tool_node("news", ToolNode([graph_tools.get_news]))

    def call(identifier):
        return AIMessage(content="", tool_calls=[{
            "name": "get_news", "id": identifier,
            "args": {"ticker": "600519", "start_date": "2026-01-01", "end_date": "2026-01-01"},
        }])

    graph = StateGraph(NewsState)
    graph.add_node("first_fetch", tool_node)
    graph.add_node("next_request", lambda state: {"news_messages": [call("second-call")]})
    graph.add_node("second_fetch", tool_node)
    graph.add_edge(START, "first_fetch")
    graph.add_edge("first_fetch", "next_request")
    graph.add_edge("next_request", "second_fetch")
    graph.add_edge("second_fetch", END)
    app = graph.compile(checkpointer=MemorySaver(), interrupt_before=["second_fetch"])
    config = {"configurable": {"thread_id": "offline-resume"}}
    articles = [{"title": "IN_WINDOW", "time": "2026-01-01 11:00:00", "content": "same source fact"}]
    with patch.object(a_stock, "_fetch_news_eastmoney", return_value=articles), \
         patch.object(a_stock, "_fetch_news_sina", return_value=[]), \
         patch.object(interface, "get_vendor", return_value="a_stock"):
        paused = app.invoke({"news_messages": [call("first-call")], "evidence_bundle": None,
                             "run_metadata": {"run_id": "resume-run"}, "trade_date": "2026-01-01"}, config)
        assert app.get_state(config).next == ("second_fetch",)
        assert set(paused["evidence_bundle"]["events"]) == {"news:first-call"}
        resumed = app.invoke(None, config)
    bundle = resumed["evidence_bundle"]
    assert set(bundle["events"]) == {"news:first-call", "news:second-call"}
    assert bundle["run_id"] == "resume-run"
    assert len(bundle["records"]) == 1
    assert len(bundle["source_statuses"]) == 2
    assert not app.get_state(config).next


def test_shared_display_resolves_evidence_id_to_source_article():
    from tradingagents.evidence.display import render_evidence_md
    record = ledger.normalize_record(
        source="official", title="TRACEABLE_ARTICLE", content="specific fact",
        url="https://example.test/filing/one", published_at="2026-01-01T11:37:00+08:00",
        time_precision="datetime", retrieved_at="2026-01-01T12:00:00+08:00",
    )
    bundle = ledger.evidence_reducer(None, make_delta(record, "news"))
    rendered = render_evidence_md(bundle)
    for value in [record["evidence_id"], record["title"], record["url"], "11:37"]:
        assert value in rendered


def test_unknown_vendor_limitation_reaches_decision_prompt():
    from tradingagents.evidence.prompt_context import evidence_context_for_prompt
    artifact = {"schema": ledger.SCHEMA_ARTIFACT, "tool": "get_news", "status": "successful",
                "provenance": "unknown", "records": [], "source_statuses": [], "exclusions": {},
                "coverage_notes": ["SOURCE_PROVENANCE_UNVERIFIABLE"]}
    message = ToolMessage(content="legacy vendor text", tool_call_id="legacy-call", artifact=artifact)
    delta = ledger.collect_tool_message_delta([message], role="news", run_id="run-one", trade_date="2026-01-01")
    bundle = ledger.evidence_reducer(None, delta)
    prompt = evidence_context_for_prompt({"evidence_bundle": bundle, "trade_date": "2026-01-01"})
    assert "SOURCE_PROVENANCE_UNVERIFIABLE" in prompt
