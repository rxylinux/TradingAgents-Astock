"""Independent lifecycle cleanup checks using real ContextVar tokens."""
from unittest.mock import Mock

import pytest

from tradingagents.dataflows import config as data_config
from tradingagents.graph import trading_graph
from web import runner


@pytest.fixture
def graph_env(monkeypatch, tmp_path):
    graph = trading_graph.TradingAgentsGraph.__new__(trading_graph.TradingAgentsGraph)
    graph.config = {"market_lookback_days": 90, "selected_analysts": ["market"],
                    "data_cache_dir": str(tmp_path)}
    graph.close_graph_run = Mock()
    graph.prepare_graph_run = Mock(return_value=({}, {}, None))
    monkeypatch.setattr(trading_graph, "TradingAgentsGraph", Mock(return_value=graph))
    monkeypatch.setattr(runner, "_discard_stopped_run", Mock())
    token = data_config.set_run_config({"market_lookback_days": 5})
    try:
        yield graph
    finally:
        data_config.reset_run_config(token)


def test_web_prepare_error_restores_context_and_closes_run(graph_env):
    graph = graph_env
    graph.prepare_graph_run.side_effect = RuntimeError("offline prepare failure")
    with pytest.raises(RuntimeError, match="offline prepare failure"):
        runner._run("600519", "2026-01-15", graph.config, Mock(stop_requested=False))
    assert data_config.get_config()["market_lookback_days"] == 5
    graph.close_graph_run.assert_called_once()


def test_web_stop_does_not_release_context_token_twice(graph_env):
    graph = graph_env
    runner._run("600519", "2026-01-15", graph.config, Mock(stop_requested=True))
    assert data_config.get_config()["market_lookback_days"] == 5


def test_python_api_prepare_failure_still_closes_run(graph_env):
    graph = graph_env
    graph.prepare_graph_run.side_effect = RuntimeError("offline prepare failure")
    with pytest.raises(RuntimeError, match="offline prepare failure"):
        graph.propagate("600519", "2026-01-15")
    assert data_config.get_config()["market_lookback_days"] == 5
    graph.close_graph_run.assert_called_once()
