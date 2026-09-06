"""Independent round-two review regressions: real I/O boundaries and SQLite resume.

No paid LLM/network calls or user data. See docs/REVIEW_ROUND2_2026-09-05.md.
"""

import re
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from tradingagents.agents.utils.agent_states import AgentState
from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.dataflows import a_stock, cache_utils
from tradingagents.graph.checkpointer import get_checkpointer, thread_id
from tradingagents.graph.conditional_logic import ConditionalLogic
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.setup import GraphSetup
from tradingagents.graph.trading_graph import TradingAgentsGraph


@pytest.fixture
def isolated_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(cache_utils, "_MEM_CACHE", cache_utils.TTLCache())
    monkeypatch.setattr(cache_utils, "_DISK_CACHE", cache_utils.DiskCache(str(tmp_path / "cache")))
    monkeypatch.setattr(cache_utils.time, "sleep", lambda _: None)


@pytest.mark.parametrize("method,source", [
    ("get_balance_sheet", "fzb"),
    ("get_cashflow", "llb"),
    ("get_income_statement", "lrb"),
])
def test_actual_sina_timeout_is_not_cached_as_empty(monkeypatch, isolated_cache, method, source):
    request = Mock(side_effect=TimeoutError("review simulated timeout"))
    monkeypatch.setattr(a_stock._requests, "get", request)
    fn = getattr(a_stock, method)
    first = fn("600519", curr_date="2026-09-04")
    cache_utils._MEM_CACHE.clear()
    request.side_effect = None
    response = Mock()
    # 公告日期 2026-08-30 早于分析日 2026-09-04：该报表在分析时点已披露，
    # 属合法历史数据（A04 披露可知过滤应放行；夹具完善，断言不变）。
    response.json.return_value = {
        "result": {"status": {"code": 0}, "data": {source: [
            {"报告日": "2026-06-30", "公告日期": "2026-08-30", "测试金额": 12345}
        ]}}
    }
    request.return_value = response
    before = request.call_count
    second = fn("600519", curr_date="2026-09-04")
    assert request.call_count > before, (first, second)
    assert "12345" in second
    assert "No " not in first, "Network failure must not claim successful absence of data"


@pytest.mark.parametrize("failure", ["timeout", "business"])
def test_concept_failure_not_cached(monkeypatch, isolated_cache, failure):
    response = Mock()
    response.json.return_value = {"ResultCode": 500, "ResultMsg": "temporary failure"}
    request = Mock(return_value=response)
    if failure == "timeout":
        request.side_effect = TimeoutError("review simulated timeout")
    monkeypatch.setattr(a_stock._requests, "get", request)
    first = a_stock.get_concept_blocks("600519")
    cache_utils._MEM_CACHE.clear()
    request.side_effect = None
    response.json.return_value = {"ResultCode": 0, "Result": {"600519": []}}
    before = request.call_count
    second = a_stock.get_concept_blocks("600519")
    assert request.call_count > before, (first, second)
    assert second != first


def test_late_reflection_not_visible_at_earlier_analysis_date(tmp_path):
    path = tmp_path / "memory.md"
    log = TradingMemoryLog({"memory_log_path": str(path)})
    path.write_text(
        "[2026-01-01 | 600519 | Buy | +20% | +10% | 5d | end=2026-01-08 | resolved=2026-09-05]"
        "\n\nDECISION:\nJanuary decision\n\nREFLECTION:\nSEPTEMBER_GENERATED_LESSON"
        + log._SEPARATOR,
        encoding="utf-8",
    )
    context = log.get_past_context("600519", as_of="2026-01-15")
    assert "SEPTEMBER_GENERATED_LESSON" not in context


def _runner(tmp_path, workflow):
    runner = TradingAgentsGraph.__new__(TradingAgentsGraph)
    runner.config = {"checkpoint_enabled": True, "data_cache_dir": str(tmp_path)}
    runner.workflow = workflow
    runner.propagator = Propagator()
    runner.memory_log = TradingMemoryLog({"memory_log_path": str(tmp_path / "memory.md")})
    runner._resolve_pending_entries = Mock()
    runner._checkpointer_ctx = None
    return runner


def _is_explicit_safe_refusal(error):
    return bool(re.search(
        r"不兼容|incompatible|unsafe|cannot safely|无法安全|拒绝恢复|需要重新开始",
        str(error), re.IGNORECASE,
    ))


def test_legacy_tool_checkpoint_migrates_or_is_explicitly_refused(tmp_path):
    @tool
    def market_data() -> str:
        """Return offline test data."""
        return "market data"

    old = StateGraph(AgentState)
    old.add_node("Market Analyst", lambda _: {"messages": [AIMessage(content="", tool_calls=[
        {"name": "market_data", "args": {}, "id": "legacy-c1"},
    ])]})
    old.add_node("tools_market", ToolNode([market_data]))
    old.add_edge(START, "Market Analyst")
    old.add_edge("Market Analyst", "tools_market")
    old.add_edge("tools_market", END)
    config = {"configurable": {"thread_id": thread_id("600519", "2026-01-15")}}
    with get_checkpointer(str(tmp_path), "600519") as saver:
        old_graph = old.compile(checkpointer=saver, interrupt_before=["tools_market"])
        old_graph.invoke(Propagator().create_initial_state("600519", "2026-01-15"), config)
        assert old_graph.get_state(config).next == ("tools_market",)

    setup = GraphSetup(None, None, {"market": ToolNode([market_data])}, ConditionalLogic(),
                       node_factories={"market": lambda _: lambda state: {"market_report": "DONE"}})
    runner = _runner(tmp_path, setup.setup_graph(["market"]))
    try:
        try:
            initial, args, _ = runner.prepare_graph_run("600519", "2026-01-15")
        except (ValueError, RuntimeError) as exc:
            assert _is_explicit_safe_refusal(exc), str(exc)
            return
        # Only need to verify the tool/analyst path, never call downstream LLMs.
        list(runner.graph.stream(initial, **args, interrupt_before=["Quality Gate"]))
        assert runner.graph.get_state(args["config"]).values["market_report"] == "DONE"
    finally:
        runner.close_graph_run()


def test_parallel_checkpoint_refresh_never_uses_polluted_context(tmp_path):
    seen = []
    workflow = StateGraph(AgentState)
    workflow.add_node("Market Analyst", lambda _: {"market_report": "done"})
    workflow.add_node("News Analyst", lambda _: {"news_report": "done"})

    def quality_gate(state):
        seen.append(state.get("past_context", ""))
        return {"data_quality_summary": "done"}

    workflow.add_node("Quality Gate", quality_gate)
    workflow.add_edge(START, "Market Analyst")
    workflow.add_edge(START, "News Analyst")
    workflow.add_edge(["Market Analyst", "News Analyst"], "Quality Gate")
    workflow.add_edge("Quality Gate", END)
    config = {"configurable": {"thread_id": thread_id("600519", "2026-01-15")}}
    with get_checkpointer(str(tmp_path), "600519") as saver:
        graph = workflow.compile(checkpointer=saver, interrupt_before=["Quality Gate"])
        initial = Propagator().create_initial_state("600519", "2026-01-15", "FUTURE_POLLUTION")
        graph.invoke(initial, config)

    runner = _runner(tmp_path, workflow)
    try:
        try:
            initial, args, _ = runner.prepare_graph_run("600519", "2026-01-15")
        except (ValueError, RuntimeError) as exc:
            assert _is_explicit_safe_refusal(exc), str(exc)
            assert not seen
            return
        runner.graph.invoke(initial, **args)
        assert seen == [""], "Refresh must not silently keep poisoned state or lose pending work"
    finally:
        runner.close_graph_run()
