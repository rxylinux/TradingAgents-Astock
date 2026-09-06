"""Independent configuration isolation through real graph and ToolNode execution."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock

import pytest
import requests
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from tradingagents.dataflows import cache_utils, config as data_config, interface
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph import trading_graph


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(requests.Session, "request", Mock(side_effect=AssertionError("Unexpected network")))
    monkeypatch.setattr(trading_graph.yf, "Ticker", Mock(side_effect=AssertionError("Unexpected Yahoo network")))
    monkeypatch.setattr(cache_utils, "_MEM_CACHE", cache_utils.TTLCache())
    monkeypatch.setattr(cache_utils, "_DISK_CACHE", cache_utils.DiskCache(str(tmp_path / "cache")))
    monkeypatch.setattr(data_config, "_config", {**DEFAULT_CONFIG, "data_cache_dir": str(tmp_path / "scoped-cache")})
    if hasattr(cache_utils, "_SCOPE_DISK_CACHES"):
        monkeypatch.setattr(cache_utils, "_SCOPE_DISK_CACHES", {})
    run_var = getattr(data_config, "_run_config", None)
    token = run_var.set(None) if run_var is not None else None
    try:
        yield
    finally:
        if run_var is not None:
            run_var.reset(token)


@pytest.mark.parametrize("concurrent", [False, True])
def test_two_graphs_keep_prompts_and_real_tool_routing_isolated(monkeypatch, tmp_path, concurrent):
    prompts, routed = {}, {}
    barrier = Barrier(2) if concurrent else None

    class OfflineChat(BaseChatModel):
        def _generate(self, messages, **kwargs):
            text = "\n".join(str(m.content) for m in messages)
            if "look_back_days 参数一律传" in text:
                ticker = "600519" if "600519" in text else "000001"
                if not any(isinstance(m, ToolMessage) for m in messages):
                    prompts[ticker] = text
                    if barrier:
                        barrier.wait(timeout=10)
                    reply = AIMessage(content="", tool_calls=[{
                        "name": "get_stock_data", "args": {"symbol": ticker,
                        "start_date": "2026-01-01", "end_date": "2026-01-15"},
                        "id": "tool-" + ticker}])
                else:
                    reply = AIMessage(content="合格分析" * 100)
            else:
                reply = AIMessage(content="Rating: Buy\n" + "合格分析" * 100)
            return ChatResult(generations=[ChatGeneration(message=reply)])

        @property
        def _llm_type(self):
            return "offline-isolation-audit"

        def bind_tools(self, tools, **kwargs):
            return self

    def vendor(label):
        def call(symbol, *args, **kwargs):
            routed[symbol] = (label, data_config.get_config()["market_lookback_days"])
            return "offline prices"
        return call

    monkeypatch.setitem(interface.VENDOR_METHODS, "get_stock_data", {"audit_a": vendor("a"), "audit_b": vendor("b")})
    monkeypatch.setattr(trading_graph, "create_llm_client", Mock(return_value=Mock(get_llm=Mock(return_value=OfflineChat()))))

    def make(label, lookback, language):
        return trading_graph.TradingAgentsGraph(["market"], config={**DEFAULT_CONFIG,
            "data_cache_dir": str(tmp_path / label / "cache"),
            "results_dir": str(tmp_path / label / "reports"), "memory_log_path": None,
            "market_lookback_days": lookback, "output_language": language,
            "tool_vendors": {"get_stock_data": "audit_" + label},
            "max_debate_rounds": 1, "max_risk_discuss_rounds": 1})

    a, b = make("a", 5, "English"), make("b", 90, "Chinese")
    if concurrent:
        with ThreadPoolExecutor(2) as pool:
            runs = [pool.submit(a.propagate, "600519", "2026-01-15"),
                    pool.submit(b.propagate, "000001", "2026-01-15")]
            for run in runs:
                run.result(timeout=20)
    else:
        a.propagate("600519", "2026-01-15")
        b.propagate("000001", "2026-01-15")
    assert "look_back_days 参数一律传 5" in prompts["600519"]
    assert "look_back_days 参数一律传 90" in prompts["000001"]
    assert routed == {"600519": ("a", 5), "000001": ("b", 90)}


def test_cached_outputs_are_scoped_to_configuration(monkeypatch, tmp_path):
    @cache_utils.cached_data(namespace="audit_config", use_disk=False)
    def probe(ticker):
        return data_config.get_config()["data_cache_dir"]

    data_config.set_config({"data_cache_dir": str(tmp_path / "a")})
    assert probe("600519") == str(tmp_path / "a")
    data_config.set_config({"data_cache_dir": str(tmp_path / "b")})
    assert probe("600519") == str(tmp_path / "b")


def test_sdk_thread_bridge_preserves_contextvars_without_installed_sdk(monkeypatch):
    import asyncio
    from contextvars import ContextVar
    from langchain_core.tools import tool
    from tradingagents.llm_clients import claude_agent_sdk_client as sdk_client

    marker = ContextVar("audit_run_marker", default="MISSING_RUN_CONTEXT")

    @tool
    def context_probe() -> str:
        """Read the isolated audit run marker."""
        return marker.get()

    monkeypatch.setattr(sdk_client, "_sdk_tool", lambda *args: lambda fn: fn)
    handler = sdk_client._sdk_tools_from_langchain([context_probe])[0]

    async def caller():
        token = marker.set("CORRECT_RUN_CONTEXT")
        try:
            # The real SDK bridge must survive BOTH its new-loop thread and
            # the asyncio.to_thread hop used to invoke the LangChain tool.
            return sdk_client._run_async(handler({}))
        finally:
            marker.reset(token)

    result = asyncio.run(caller())
    assert result["content"][0]["text"] == "CORRECT_RUN_CONTEXT"


def test_graph_configuration_does_not_share_callers_nested_dict(monkeypatch, tmp_path):
    monkeypatch.setattr(trading_graph, "create_llm_client", Mock(return_value=Mock()))
    cfg = {**DEFAULT_CONFIG, "data_cache_dir": str(tmp_path / "cache"),
           "results_dir": str(tmp_path / "reports"), "memory_log_path": None,
           "tool_vendors": {"get_stock_data": "original"}}
    graph = trading_graph.TradingAgentsGraph(["market"], config=cfg)
    cfg["tool_vendors"]["get_stock_data"] = "mutated_by_caller"
    assert graph.config["tool_vendors"]["get_stock_data"] == "original"


def test_same_cache_directory_different_vendors_do_not_share_results(tmp_path):
    @cache_utils.cached_data(namespace="audit_vendor", use_disk=False)
    def probe(ticker):
        return data_config.get_config()["data_vendors"]["core_stock_apis"]

    for vendor in ("vendor_a", "vendor_b"):
        data_config.set_config({"data_cache_dir": str(tmp_path),
                                "data_vendors": {"core_stock_apis": vendor}})
        assert probe("600519") == vendor


def test_partial_config_update_keeps_previous_settings():
    data_config.set_config({"data_vendors": {"core_stock_apis": "chosen_vendor"}})
    data_config.set_config({"output_language": "Chinese"})
    observed = data_config.get_config()
    assert observed.get("data_vendors", {}).get("core_stock_apis") == "chosen_vendor"
    assert "data_cache_dir" in observed


def test_constructing_another_graph_does_not_change_callers_context(monkeypatch, tmp_path):
    monkeypatch.setattr(trading_graph, "create_llm_client", Mock(return_value=Mock()))
    token = data_config.set_run_config({"market_lookback_days": 5})
    try:
        trading_graph.TradingAgentsGraph(["market"], config={**DEFAULT_CONFIG,
            "market_lookback_days": 90, "data_cache_dir": str(tmp_path / "cache"),
            "results_dir": str(tmp_path / "reports"), "memory_log_path": None})
        assert data_config.get_config()["market_lookback_days"] == 5
    finally:
        data_config.reset_run_config(token)
