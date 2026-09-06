"""Independent CLI entrypoint verification; no network/LLM/user data."""
from contextlib import nullcontext
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from cli import main
from cli.models import AnalystType
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.propagation import Propagator


@pytest.fixture
def cli_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "DEFAULT_CONFIG", {**DEFAULT_CONFIG,
        "results_dir": str(tmp_path / "logs"), "data_cache_dir": str(tmp_path / "cache"),
        "memory_log_path": str(tmp_path / "memory.md")})
    monkeypatch.setattr(main, "message_buffer", main.MessageBuffer())
    monkeypatch.setattr(main, "create_layout", Mock())
    monkeypatch.setattr(main, "update_display", Mock())
    monkeypatch.setattr(main, "Live", lambda *a, **kw: nullcontext())
    monkeypatch.setattr(main, "console", Mock())
    monkeypatch.setattr(main.typer, "prompt", lambda *a, **kw: "N")
    return tmp_path


def selection(ticker):
    return dict(ticker=ticker, analysis_date="2026-09-04", research_depth=1,
        shallow_thinker="offline", deep_thinker="offline", backend_url=None,
        llm_provider="glm", analysts=[AnalystType.MARKET], analysis_type="个股")


def fake_graph(ticker):
    state = Propagator().create_initial_state(ticker, "2026-09-04")
    state.update(final_trade_decision="Rating: Buy", trader_investment_plan="plan")
    state["market_messages"] = [AIMessage(content="", tool_calls=[{
        "name": "get_stock_data", "args": {"ticker": ticker}, "id": "branch-test"}]),
        ToolMessage(content="OFFLINE_DATA_FROM_BRANCH", tool_call_id="branch-test", name="get_stock_data")]
    graph = Mock()
    graph.propagator = Propagator()
    graph.prepare_graph_run.return_value = (state, {}, None)
    graph.graph.stream.return_value = iter([state])
    graph.finalize_graph_run.return_value = "Buy"
    return graph


def test_prepare_error_closes_run(cli_environment, monkeypatch):
    graph = fake_graph("600519")
    graph.prepare_graph_run.side_effect = RuntimeError("unsafe checkpoint")
    monkeypatch.setattr(main, "TradingAgentsGraph", Mock(return_value=graph))
    monkeypatch.setattr(main, "get_user_selections", lambda **kw: selection("600519"))
    with pytest.raises(RuntimeError, match="unsafe checkpoint"):
        main.run_analysis(checkpoint=True)
    graph.close_graph_run.assert_called_once()


def test_stream_error_closes_and_does_not_finalize(cli_environment, monkeypatch):
    graph = fake_graph("600519")
    def broken_stream(*a, **kw):
        raise TimeoutError("node timeout")
        yield
    graph.graph.stream.side_effect = broken_stream
    monkeypatch.setattr(main, "TradingAgentsGraph", Mock(return_value=graph))
    monkeypatch.setattr(main, "get_user_selections", lambda **kw: selection("600519"))
    with pytest.raises(TimeoutError, match="node timeout"):
        main.run_analysis(checkpoint=True)
    graph.close_graph_run.assert_called_once()
    graph.finalize_graph_run.assert_not_called()


def test_branch_messages_reach_cli_log(cli_environment, monkeypatch):
    graph = fake_graph("600519")
    monkeypatch.setattr(main, "TradingAgentsGraph", Mock(return_value=graph))
    monkeypatch.setattr(main, "get_user_selections", lambda **kw: selection("600519"))
    main.run_analysis(checkpoint=True)
    text = (cli_environment / "logs/600519/2026-09-04/message_tool.log").read_text()
    assert "OFFLINE_DATA_FROM_BRANCH" in text and "get_stock_data" in text


def test_repeated_cli_invocation_does_not_rewrite_previous_logs(cli_environment, monkeypatch):
    monkeypatch.setattr(main, "TradingAgentsGraph", Mock(side_effect=[fake_graph("600519"), fake_graph("000001")]))
    monkeypatch.setattr(main, "get_user_selections", Mock(side_effect=[selection("600519"), selection("000001")]))
    main.run_analysis(checkpoint=True)
    path = cli_environment / "logs/600519/2026-09-04/message_tool.log"
    previous = path.read_text()
    main.run_analysis(checkpoint=True)
    assert path.read_text() == previous, "Second run wrote into first run's log"
