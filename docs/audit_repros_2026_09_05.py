"""Offline audit regressions; deliberately outside the normal tests/ suite.

Run: .venv/bin/python -m pytest docs/audit_repros_2026_09_05.py -q
Assertions describe required behaviour and fail on the audited worktree.
All market data, model calls, persistent paths and HTTP are isolated.
"""

import ast
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest
import requests
from langchain_core.messages import AIMessage

from tradingagents.agents.quality_gate import create_quality_gate
from tradingagents.agents.utils.agent_utils import get_language_instruction
from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.dataflows import a_stock, cache_utils, config as data_config, index_data
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.trading_graph import TradingAgentsGraph
from web import history


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(requests.Session, "request", Mock(side_effect=AssertionError("Unexpected network")))
    # yfinance uses curl_cffi, so blocking requests alone is insufficient.
    from tradingagents.graph import trading_graph
    monkeypatch.setattr(trading_graph.yf, "Ticker", Mock(side_effect=AssertionError("Unexpected Yahoo network")))
    monkeypatch.setattr(cache_utils, "_MEM_CACHE", cache_utils.TTLCache())
    monkeypatch.setattr(cache_utils, "_DISK_CACHE", cache_utils.DiskCache(str(tmp_path / "cache")))
    monkeypatch.setattr(data_config, "_config", {**DEFAULT_CONFIG, "data_cache_dir": str(tmp_path / "scoped-cache")})
    if hasattr(cache_utils, "_SCOPE_DISK_CACHES"):
        monkeypatch.setattr(cache_utils, "_SCOPE_DISK_CACHES", {})
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(history, "_INCOMPLETE_TASKS_FILE", tmp_path / "incomplete.json")
    run_var = getattr(data_config, "_run_config", None)
    token = run_var.set(None) if run_var is not None else None
    try:
        yield
    finally:
        if run_var is not None:
            run_var.reset(token)


def _web_config_builder():
    """Execute the actual function without launching Streamlit's top-level UI."""
    path = Path(__file__).resolve().parents[1] / "web" / "app.py"
    node = next(n for n in ast.parse(path.read_text()).body
                if isinstance(n, ast.FunctionDef) and n.name == "_build_config")
    namespace = {"DEFAULT_CONFIG": DEFAULT_CONFIG, "os": __import__("os"),
                 "st": SimpleNamespace(session_state={})}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    return namespace


def test_a01_cli_uses_run_lifecycle(monkeypatch, tmp_path):
    from cli import main
    from cli.models import AnalystType

    selections = dict(ticker="600519", analysis_date="2026-09-04", research_depth=1,
                      shallow_thinker="offline", deep_thinker="offline", backend_url=None,
                      llm_provider="glm", analysts=[AnalystType.MARKET], analysis_type="个股")
    state = Propagator().create_initial_state("600519", "2026-09-04")
    state["final_trade_decision"] = "Rating: Buy"
    graph = Mock()
    graph.graph.stream.return_value = iter([state])
    graph.propagator = Propagator()
    graph.prepare_graph_run.return_value = (state, {}, None)
    monkeypatch.setattr(main, "DEFAULT_CONFIG", {**DEFAULT_CONFIG, "results_dir": str(tmp_path / "logs")})
    monkeypatch.setattr(main, "TradingAgentsGraph", Mock(return_value=graph))
    monkeypatch.setattr(main, "get_user_selections", lambda **kw: selections)
    monkeypatch.setattr(main, "message_buffer", main.MessageBuffer())
    monkeypatch.setattr(main, "create_layout", Mock())
    monkeypatch.setattr(main, "update_display", Mock())
    monkeypatch.setattr(main, "Live", lambda *a, **kw: nullcontext())
    monkeypatch.setattr(main, "console", Mock())
    monkeypatch.setattr(main.typer, "prompt", lambda *a, **kw: "N")
    main.run_analysis(checkpoint=True)
    counts = {name: getattr(graph, name).call_count for name in
              ("prepare_graph_run", "finalize_graph_run", "close_graph_run")}
    assert all(counts.values()), counts


def test_a02_web_provider_blank_url_uses_provider_default(monkeypatch):
    monkeypatch.delenv("BACKEND_URL", raising=False)
    # Reload only the actual assignment with dotenv/env influence excluded.
    tree = ast.parse((Path(__file__).resolve().parents[1] / "tradingagents/default_config.py").read_text())
    defaults = next(n.value for n in tree.body if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "DEFAULT_CONFIG" for t in n.targets))
    expr = next(v for k, v in zip(defaults.keys, defaults.values) if k.value == "backend_url")
    url = eval(compile(ast.Expression(expr), "default_config.py", "eval"), {"os": __import__("os")})
    ns = _web_config_builder()
    ns["DEFAULT_CONFIG"] = {**DEFAULT_CONFIG, "backend_url": url}
    ns["st"].session_state = {"llm_provider": "deepseek", "llm_base_url": ""}
    config = ns["_build_config"]()
    assert config["backend_url"] is None, config["backend_url"]


def test_a03_northbound_does_not_leak_future_snapshot(monkeypatch):
    response = Mock()
    response.json.return_value = {"time": ["15:00"], "hgt": [123456], "sgt": [1]}
    monkeypatch.setattr(a_stock._requests, "get", Mock(return_value=response))
    monkeypatch.setattr(a_stock, "_save_northbound_snapshot", Mock())
    monkeypatch.setattr(a_stock, "_load_northbound_history", lambda n: [("2099-01-01", 8, 9)])
    output = a_stock.get_northbound_flow("2026-01-15", include_history=True)
    assert "2099-01-01" not in output and "123456" not in output, output


def test_a03_industry_snapshot_is_not_presented_as_historical(monkeypatch):
    response = Mock()
    response.json.return_value = {"data": {"diff": [{"f14": "FUTURE_SECTOR", "f3": 88}]}}
    monkeypatch.setattr(a_stock, "_em_get", Mock(return_value=response))
    output = a_stock.get_industry_comparison("600519", "2026-01-15")
    assert "FUTURE_SECTOR" not in output or "实时快照" in output, output


@pytest.mark.parametrize("report,source", [("资产负债表", "fzb"), ("利润表", "lrb"), ("现金流量表", "llb")])
def test_a04_financials_require_publication_date(monkeypatch, report, source):
    response = Mock()
    response.json.return_value = {"result": {"status": {"code": 0}, "data": {source: [
        {"报告日": "2026-06-30", "公告日期": "2026-08-30", "审计测试金额": 987654321}
    ]}}}
    monkeypatch.setattr(a_stock._requests, "get", Mock(return_value=response))
    result = a_stock._get_financial_report_sina("600519", report, "quarterly", "2026-07-10")
    assert result.empty, result.to_dict("records")


def test_a05_independent_run_configs_do_not_overwrite_each_other():
    ready, overwritten = Event(), Event()

    def run_a():
        data_config.set_config({"output_language": "English", "market_lookback_days": 5})
        ready.set()
        assert overwritten.wait(5)
        return get_language_instruction(), data_config.get_config()["market_lookback_days"]

    def run_b():
        assert ready.wait(5)
        data_config.set_config({"output_language": "Chinese", "market_lookback_days": 90})
        overwritten.set()

    with ThreadPoolExecutor(2) as pool:
        a, b = pool.submit(run_a), pool.submit(run_b)
        observed = a.result()
        b.result()
    assert observed == ("", 5), observed


def test_a06_memory_update_preserves_concurrent_append(monkeypatch, tmp_path):
    path = tmp_path / "memory.md"
    writer_a = TradingMemoryLog({"memory_log_path": str(path)})
    writer_b = TradingMemoryLog({"memory_log_path": str(path)})
    writer_a.store_decision("600519", "2026-01-01", "Rating: Buy")
    real_read = Path.read_text
    reader_id = []
    read_done, release, append_done = Event(), Event(), Event()

    def interleave(p, *args, **kwargs):
        text = real_read(p, *args, **kwargs)
        if p == path and reader_id == [get_ident()]:
            read_done.set()
            assert release.wait(5)
        return text

    def update():
        reader_id.append(get_ident())
        writer_a.batch_update_with_outcomes([dict(ticker="600519", trade_date="2026-01-01",
            raw_return=.1, alpha_return=.05, holding_days=5, reflection="offline", outcome_end="2026-01-08")])

    def append():
        writer_b.store_decision("000001", "2026-01-02", "Rating: Sell")
        append_done.set()

    monkeypatch.setattr(Path, "read_text", interleave)
    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(update)
        assert read_done.wait(5)
        b = pool.submit(append)
        # A correct transaction lock may block B; release A in either case.
        append_done.wait(.2)
        release.set()
        a.result()
        b.result()
    tickers = {e["ticker"] for e in writer_a.load_entries()}
    assert tickers == {"600519", "000001"}, tickers


def _frame(dates, closes):
    return pd.DataFrame({"Date": pd.to_datetime(dates), "Close": closes})


def test_a07_alpha_compares_identical_dates(monkeypatch):
    stock = _frame(["2026-01-05", "2026-01-08"], [100, 110])
    benchmark = _frame(["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"], [100, 101, 120, 130])
    monkeypatch.setattr(a_stock, "get_astock_history_df", lambda *a, **kw: stock)
    monkeypatch.setattr(index_data, "get_index_history_df", lambda *a, **kw: benchmark)
    graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
    raw, alpha, days, end = graph._fetch_returns("600519", "2026-01-05", holding_days=1)
    assert alpha == pytest.approx(-.2), (raw, alpha, days, end)


def test_a08_wait_for_full_outcome_window(monkeypatch, tmp_path):
    prices = _frame(["2026-01-05", "2026-01-06"], [100, 110])
    monkeypatch.setattr(a_stock, "get_astock_history_df", lambda *a, **kw: prices)
    monkeypatch.setattr(index_data, "get_index_history_df", lambda *a, **kw: prices)
    graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
    graph.memory_log = TradingMemoryLog({"memory_log_path": str(tmp_path / "memory.md")})
    graph.memory_log.store_decision("600519", "2026-01-05", "Rating: Buy")
    graph.reflector = Mock()
    graph.reflector.reflect_on_final_decision.return_value = "one-day lesson"
    graph._resolve_pending_entries("600519")
    assert graph.memory_log.get_pending_entries(), graph.memory_log.load_entries()


def test_a09_resume_mode_comes_from_task():
    ns = _web_config_builder()
    ns["st"].session_state = {"analysis_type": "个股"}
    # Same variables as web/app.py after popping the pending index resume request.
    ns["start_req"] = {"ticker": "000001.SH", "trade_date": "2026-01-15", "analysis_type": "指数"}
    ns["a_type"] = "指数"
    assert ns["_build_config"]()["instrument_type"] == "index"


def test_a10_disabled_analysts_are_not_quality_failures():
    llm = Mock()
    llm.invoke.return_value = AIMessage(content="reviewed")
    state = Propagator().create_initial_state("600519", "2026-01-15")
    state["selected_analysts"] = ["market"]
    state["market_report"] = "合格技术分析。" * 50 + "\n|日期|收盘|\n|---|---|\n|2026-01-15|100|"
    summary = create_quality_gate(llm)(state)["data_quality_summary"]
    assert "[F]" not in summary and llm.invoke.call_count == 1, summary


def test_a11_old_completed_report_does_not_hide_new_failed_run(monkeypatch, tmp_path):
    logs = tmp_path / "logs"
    path = logs / "600519" / "TradingAgentsStrategy_logs" / "full_states_log_2026-01-15.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"final_trade_decision": "old completed run"}')
    monkeypatch.setattr(history, "_results_dir", lambda: logs)
    monkeypatch.setattr(history, "_checkpoint_step", lambda *a: 8)
    history.record_incomplete_task("600519", "2026-01-15", status="error", error="NEW run failed")
    assert history.get_incomplete_history(), "New resumable run was removed by an older report"


def test_a12_history_reads_configured_results_directory(monkeypatch, tmp_path):
    logs = tmp_path / "custom-results"
    path = logs / "600519" / "TradingAgentsStrategy_logs" / "full_states_log_2026-01-15.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    monkeypatch.setitem(history.DEFAULT_CONFIG, "results_dir", str(logs))
    assert any(row["path"] == str(path) for row in history.get_history())


def test_a13_eastmoney_requests_are_serialized(monkeypatch):
    entered, release, overlapped = Event(), Event(), Event()
    calls = []

    def fake_get(*args, **kw):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        else:
            overlapped.set()
        return Mock()

    monkeypatch.setattr(a_stock._EM_SESSION, "get", fake_get)
    monkeypatch.setattr(a_stock, "_em_last_call", [0.0])
    monkeypatch.setattr(a_stock, "_EM_MIN_INTERVAL", .01)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(a_stock._em_get, "https://offline.invalid/one")
        assert entered.wait(5)
        second = pool.submit(a_stock._em_get, "https://offline.invalid/two")
        concurrent = overlapped.wait(.2)
        release.set()
        first.result()
        second.result()
    assert not concurrent, "Second HTTP request entered before the first completed"


def test_a14_persist_quality_summary_for_history(tmp_path):
    graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
    graph.config = {"results_dir": str(tmp_path)}
    graph.ticker = "600519"
    graph.log_states_dict = {}
    state = Propagator().create_initial_state("600519", "2026-01-15")
    state["trader_investment_plan"] = "TRADER_PLAN"
    state["data_quality_summary"] = "CRITICAL_QUALITY_WARNING"
    graph._log_state("2026-01-15", state)
    path = tmp_path / "600519/TradingAgentsStrategy_logs/full_states_log_2026-01-15.json"
    assert history.load_analysis(str(path)).get("data_quality_summary") == "CRITICAL_QUALITY_WARNING"


def test_a14_live_export_includes_trader_plan():
    from web.pdf_export import generate_markdown

    state = Propagator().create_initial_state("600519", "2026-01-15")
    state["trader_investment_plan"] = "UNIQUE_TRADER_PLAN"
    text = generate_markdown(state, "600519", "2026-01-15", "Buy")
    assert "UNIQUE_TRADER_PLAN" in text, text


@pytest.mark.parametrize("with_tools", [False, True])
def test_a15_sdk_disables_unneeded_builtin_tools(monkeypatch, with_tools):
    from tradingagents.llm_clients import claude_agent_sdk_client as sdk

    monkeypatch.setattr(sdk, "ClaudeAgentOptions", lambda **kw: kw)
    monkeypatch.setattr(sdk, "create_sdk_mcp_server", lambda *a, **kw: "offline-server")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    opts = sdk.ClaudeAgentSDKClient("offline")._build_options(
        "offline", sdk_tools=[object()] if with_tools else None,
        tool_names=["get_stock_data"] if with_tools else None,
    )
    # allowed_tools pre-approves calls; it does not remove other built-ins.
    # This assertion captures the intended no-builtins design. No SDK process runs.
    assert opts.get("tools") == [], {k: opts.get(k) for k in ("tools", "allowed_tools", "permission_mode")}
