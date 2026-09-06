"""Independent active-team checks through real graphs and SQLite checkpoints."""
from unittest.mock import Mock

import pytest
import requests
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph import trading_graph
from tradingagents.graph.index_graph import INDEX_ANALYSTS, TradingAgentsIndexGraph


class OfflineChat(BaseChatModel):
    def _generate(self, messages, **kwargs):
        text = "AUDIT_GOOD_REPORT " * 30 + "\n| metric | value |\n|---|---|\n| x | y |"
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])

    @property
    def _llm_type(self):
        return "offline-quality-audit"

    def bind_tools(self, tools, **kwargs):
        return self


@pytest.fixture
def graph_factory(monkeypatch, tmp_path):
    monkeypatch.setattr(requests.Session, "request", Mock(side_effect=AssertionError("Unexpected network")))
    monkeypatch.setattr(trading_graph.yf, "Ticker", Mock(side_effect=AssertionError("Unexpected Yahoo")))
    monkeypatch.setattr(trading_graph, "create_llm_client", Mock(return_value=Mock(
        get_llm=Mock(return_value=OfflineChat()))))

    def make(roles, index=False):
        cls = TradingAgentsIndexGraph if index else trading_graph.TradingAgentsGraph
        return cls(roles, config={**DEFAULT_CONFIG,
            "data_cache_dir": str(tmp_path / "cache"), "results_dir": str(tmp_path / "reports"),
            "memory_log_path": None, "checkpoint_enabled": True})
    return make


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("index", [False, True])
def test_quality_gate_keeps_active_team_across_sqlite_resume(graph_factory, legacy, index):
    roles = INDEX_ANALYSTS if index else ["market"]
    ticker = "000001.SH" if index else "600519"
    graph = graph_factory(roles, index)
    try:
        state, args, _ = graph.prepare_graph_run(ticker, "2026-01-15")
        assert state["selected_analysts"] == roles
        if legacy:
            # Previous branch-isolated checkpoints have no new team metadata.
            state.pop("selected_analysts")
        list(graph.graph.stream(state, **args, interrupt_before=["Quality Gate"]))
        snap = graph.graph.get_state(args["config"])
        assert snap.next == ("Quality Gate",)
    finally:
        graph.close_graph_run()

    resumed = graph_factory(roles, index)
    try:
        state, args, step = resumed.prepare_graph_run(ticker, "2026-01-15")
        assert state is None and step is not None
        list(resumed.graph.stream(state, **args, interrupt_after=["Quality Gate"]))
        summary = resumed.graph.get_state(args["config"]).values["data_quality_summary"]
        assert "[F]" not in summary, summary
        assert f"{len(roles)} 位" in summary, summary
    finally:
        resumed.close_graph_run()


def test_changed_team_cannot_silently_resume_another_topology(graph_factory):
    graph = graph_factory(["market"])
    try:
        state, args, _ = graph.prepare_graph_run("600519", "2026-01-15")
        list(graph.graph.stream(state, **args, interrupt_before=["Quality Gate"]))
    finally:
        graph.close_graph_run()
    resumed = graph_factory(["market", "news"])
    try:
        with pytest.raises(RuntimeError, match="分析师|analyst|配置|topology"):
            resumed.prepare_graph_run("600519", "2026-01-15")
        snap = resumed.graph.get_state(args["config"])
        assert snap.values["selected_analysts"] == ["market"], "Rejected resume rewrote original team"
    finally:
        resumed.close_graph_run()
    # LangGraph derives `next` using the compiled topology. Verify retained
    # pending work with the ORIGINAL team, not the deliberately incompatible graph.
    original = graph_factory(["market"])
    try:
        state, args, step = original.prepare_graph_run("600519", "2026-01-15")
        assert state is None and step is not None, "Rejected resume removed checkpoint"
        assert original.graph.get_state(args["config"]).next == ("Quality Gate",)
    finally:
        original.close_graph_run()
