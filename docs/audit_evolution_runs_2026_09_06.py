"""Codex N02: actual graph lifecycle, report archives and public metadata."""
import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
import requests
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph import trading_graph
from web import history


class OfflineChat(BaseChatModel):
    def _generate(self, messages, **kwargs):
        text = "离线研究内容" * 60 + "\n| 项目 | 结论 |\n|---|---|\n| 综合 | 完整 |"
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])

    @property
    def _llm_type(self):
        return "offline-run-archive-audit"

    def bind_tools(self, tools, **kwargs):
        return self


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(requests.Session, "request", Mock(side_effect=AssertionError("Unexpected HTTP")))
    monkeypatch.setattr(trading_graph.yf, "Ticker", Mock(side_effect=AssertionError("Unexpected Yahoo")))
    monkeypatch.setattr(trading_graph, "create_llm_client", Mock(return_value=Mock(
        get_llm=Mock(return_value=OfflineChat()))))
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg.update(results_dir=str(tmp_path / "reports"), data_cache_dir=str(tmp_path / "cache"),
               memory_log_path=None, checkpoint_enabled=False)
    monkeypatch.setitem(history.DEFAULT_CONFIG, "results_dir", cfg["results_dir"])
    return cfg, tmp_path


def prepare(cfg, roles=("market",)):
    graph = trading_graph.TradingAgentsGraph(list(roles), config=cfg)
    state, args, _ = graph.prepare_graph_run("600519", "2026-01-15")
    if not isinstance(state.get("run_metadata"), dict):
        graph.close_graph_run()
    assert isinstance(state.get("run_metadata"), dict), "Fresh graph state has no run metadata"
    return graph, state, args


def test_same_day_runs_are_preserved_once_each_with_latest_compatibility(env):
    cfg, tmp = env
    graph, first, _ = prepare(cfg)
    try:
        first["final_trade_decision"] = "Rating: Buy\nAUDIT_RUN_ONE"
        graph.finalize_graph_run("600519", "2026-01-15", first)
        # Repeating completion must not produce a third historical item.
        graph.finalize_graph_run("600519", "2026-01-15", first)
        second, _, _ = graph.prepare_graph_run("600519", "2026-01-15")
        second["final_trade_decision"] = "Rating: Sell\nAUDIT_RUN_TWO"
        assert first["run_metadata"]["run_id"] != second["run_metadata"]["run_id"]
        graph.finalize_graph_run("600519", "2026-01-15", second)
    finally:
        graph.close_graph_run()
    rows = history.get_history()
    assert len(rows) == 2, rows
    bodies = [history.load_analysis(r["path"])["final_trade_decision"] for r in rows]
    assert any("AUDIT_RUN_ONE" in b for b in bodies)
    assert any("AUDIT_RUN_TWO" in b for b in bodies)
    assert {r["ticker"] for r in rows} == {"600519"}
    latest = tmp / "reports/600519/TradingAgentsStrategy_logs/full_states_log_2026-01-15.json"
    assert "AUDIT_RUN_TWO" in json.loads(latest.read_text())["final_trade_decision"]


def test_retry_finishes_latest_publish_after_archive_was_saved(env, monkeypatch):
    cfg, tmp = env
    graph, state, _ = prepare(cfg)
    try:
        state["final_trade_decision"] = "Rating: Buy\nAUDIT_RETRY_PUBLISH"
        write = graph._atomic_write_json

        def fail_latest(path, payload, **kwargs):
            if "_runs" not in path.parts:
                raise OSError("AUDIT_LATEST_FAILURE")
            return write(path, payload, **kwargs)

        monkeypatch.setattr(graph, "_atomic_write_json", fail_latest)
        with pytest.raises(OSError, match="AUDIT_LATEST_FAILURE"):
            graph._log_state("2026-01-15", state)
        monkeypatch.setattr(graph, "_atomic_write_json", write)
        graph._log_state("2026-01-15", state)
        latest = tmp / "reports/600519/TradingAgentsStrategy_logs/full_states_log_2026-01-15.json"
        assert latest.exists(), "Retry left the compatible latest entry unpublished"
        assert "AUDIT_RETRY_PUBLISH" in latest.read_text()
    finally:
        graph.close_graph_run()


def test_concurrent_conflicting_writers_cannot_both_publish_same_run(env, monkeypatch):
    cfg, _ = env
    first, state_a, _ = prepare(cfg)
    second, state_b, _ = prepare(cfg)
    state_b["run_metadata"] = copy.deepcopy(state_a["run_metadata"])
    state_a["final_trade_decision"] = "Rating: Buy\nAUDIT_WRITER_A"
    state_b["final_trade_decision"] = "Rating: Sell\nAUDIT_WRITER_B"
    entered_a, entered_b, written_a = threading.Event(), threading.Event(), threading.Event()
    write_a, write_b = first._atomic_write_json, second._atomic_write_json

    def slow_a(path, payload, **kwargs):
        if "_runs" in path.parts:
            entered_a.set()
            entered_b.wait(0.5)  # A correct shared lock prevents B entering until A completes.
        result = write_a(path, payload, **kwargs)
        if "_runs" in path.parts:
            written_a.set()
        return result

    def slow_b(path, payload, **kwargs):
        if "_runs" in path.parts:
            entered_b.set()
            assert written_a.wait(3)
        return write_b(path, payload, **kwargs)

    monkeypatch.setattr(first, "_atomic_write_json", slow_a)
    monkeypatch.setattr(second, "_atomic_write_json", slow_b)

    def save(graph, state):
        try:
            graph._log_state("2026-01-15", state)
            return "published"
        except (RuntimeError, ValueError, FileExistsError):
            return "conflict"

    try:
        with ThreadPoolExecutor(2) as pool:
            a = pool.submit(save, first, state_a)
            assert entered_a.wait(3)
            b = pool.submit(save, second, state_b)
            outcomes = [a.result(timeout=5), b.result(timeout=5)]
        assert sorted(outcomes) == ["conflict", "published"], outcomes
    finally:
        first.close_graph_run()
        second.close_graph_run()


@pytest.mark.parametrize("legacy", [False, True])
def test_sqlite_resume_preserves_original_metadata_or_legacy_unknown(env, legacy):
    cfg, _ = env
    cfg["checkpoint_enabled"] = True
    graph, state, args = prepare(cfg)
    original = copy.deepcopy(state["run_metadata"])
    try:
        if legacy:
            state.pop("run_metadata")
        list(graph.graph.stream(state, **args, interrupt_before=["Quality Gate"]))
    finally:
        graph.close_graph_run()
    changed = {**cfg, "market_lookback_days": 91}
    resumed = trading_graph.TradingAgentsGraph(["market"], config=changed)
    try:
        state, args, step = resumed.prepare_graph_run("600519", "2026-01-15")
        assert state is None and step is not None
        metadata = resumed.graph.get_state(args["config"]).values.get("run_metadata")
        if legacy:
            assert not metadata or not metadata.get("run_id"), "Legacy origin was fabricated on resume"
        else:
            assert metadata == original, "Resume replaced the original config or identity"
    finally:
        resumed.close_graph_run()


def test_metadata_is_deep_public_snapshot_without_nested_secrets(env):
    cfg, _ = env
    cfg.update(api_key="AUDIT_PRIVATE_KEY", backend_url="https://u:AUDIT_URL_PASS@example.invalid/api?token=AUDIT_QUERY_TOKEN",
               extra_settings={"credential": "AUDIT_EXTRA_SECRET"},
               role_llms={"bull": {"provider": "glm", "model": "audit-model",
                                  "api_key": "AUDIT_ROLE_KEY", "headers": {"Authorization": "AUDIT_HEADER"}}})
    graph, state, _ = prepare(cfg)
    try:
        metadata = copy.deepcopy(state["run_metadata"])
        encoded = json.dumps(metadata, ensure_ascii=False)
        for secret in ("AUDIT_PRIVATE_KEY", "AUDIT_URL_PASS", "AUDIT_QUERY_TOKEN",
                       "AUDIT_EXTRA_SECRET", "AUDIT_ROLE_KEY", "AUDIT_HEADER"):
            assert secret not in encoded
        cfg["role_llms"]["bull"]["model"] = "changed-after-prepare"
        assert state["run_metadata"] == metadata
    finally:
        graph.close_graph_run()


def test_fingerprint_ignores_order_but_detects_research_configuration_changes(env):
    cfg, _ = env
    graphs = []
    try:
        a, state_a, _ = prepare(cfg, ("market", "news"))
        graphs.append(a)
        b, state_b, _ = prepare(dict(reversed(list(cfg.items()))), ("news", "market"))
        graphs.append(b)
        c, state_c, _ = prepare({**cfg, "market_lookback_days": 91}, ("market", "news"))
        graphs.append(c)
        d, state_d, _ = prepare(cfg, ("market",))
        graphs.append(d)
        assert state_a["run_metadata"]["config_snapshot"].get("selected_analysts") == ["market", "news"]
        assert state_a["run_metadata"]["config_fingerprint"] == state_b["run_metadata"]["config_fingerprint"]
        assert state_a["run_metadata"]["config_fingerprint"] != state_c["run_metadata"]["config_fingerprint"]
        assert state_a["run_metadata"]["config_fingerprint"] != state_d["run_metadata"]["config_fingerprint"]
    finally:
        for graph in graphs:
            graph.close_graph_run()


def test_snapshot_does_not_copy_arbitrary_nested_vendor_secrets():
    from tradingagents.run_records import build_config_snapshot
    config = {
        "data_vendors": {"core_stock_apis": "a_stock", "private": {"Authorization": "AUDIT_VENDOR_SECRET"}},
        "tool_vendors": {"get_stock_data": "a_stock", "api_key": "AUDIT_TOOL_SECRET"},
        "role_llms": {"bull": {"provider": "glm", "model": "public", "api_key": "AUDIT_ROLE_SECRET"}},
    }
    snapshot = build_config_snapshot(config)
    text = json.dumps(snapshot)
    assert "AUDIT_VENDOR_SECRET" not in text
    assert "AUDIT_TOOL_SECRET" not in text
    assert "AUDIT_ROLE_SECRET" not in text
    assert snapshot["data_vendors"]["core_stock_apis"] == "a_stock"
    assert snapshot["tool_vendors"]["get_stock_data"] == "a_stock"


@pytest.mark.parametrize("key,value", [
    ("deep_think_provider_override", "claude_agent_sdk"),
    ("quick_think_provider_override", "claude_agent_sdk"),
    ("agent_sdk_model", "sonnet"),
    ("agent_sdk_fallback_model", "audit-fallback-model"),
    ("openai_reasoning_effort", "high"),
    ("enable_debate_early_stopping", False),
])
def test_fingerprint_includes_effective_public_model_configuration(key, value):
    from tradingagents.run_records import build_config_snapshot, config_fingerprint
    public_keys = ("llm_provider", "deep_think_llm", "quick_think_llm", "agent_sdk_model",
                   "enable_debate_early_stopping", "market_lookback_days", key)
    original = {k: copy.deepcopy(DEFAULT_CONFIG[k]) for k in public_keys if k in DEFAULT_CONFIG}
    changed = {**original, key: value}
    assert original.get(key) != value
    assert config_fingerprint(build_config_snapshot(original)) != config_fingerprint(build_config_snapshot(changed)), key


def test_snapshot_rejects_unknown_routes_and_non_scalar_model_fields():
    from tradingagents.run_records import build_config_snapshot
    config = {
        "data_vendors": {"core_stock_apis": "a_stock", "custom_connection": "AUDIT_PRIVATE_ROUTE"},
        "tool_vendors": {"get_stock_data": "a_stock", "get_private_data": "AUDIT_PRIVATE_TOOL"},
        "role_llms": {"bull": {"provider": "glm", "model": {"nested": "AUDIT_PRIVATE_MODEL"}}},
    }
    snap = build_config_snapshot(config)
    encoded = json.dumps(snap)
    assert "AUDIT_PRIVATE" not in encoded
    assert snap["data_vendors"]["core_stock_apis"] == "a_stock"
    assert snap["tool_vendors"]["get_stock_data"] == "a_stock"
    saved = copy.deepcopy(snap)
    config["role_llms"]["bull"]["model"]["nested"] = "changed"
    assert snap == saved


def test_conflicting_repeat_cannot_diverge_archive_and_latest(env):
    cfg, tmp = env
    graph, state, _ = prepare(cfg)
    try:
        state["final_trade_decision"] = "Rating: Buy\nAUDIT_ACCEPTED_VERSION"
        graph._log_state("2026-01-15", state)
        before = {p: p.read_bytes() for p in (tmp / "reports").rglob("full_states_log_*.json")}
        state["final_trade_decision"] = "Rating: Sell\nAUDIT_CONFLICTING_VERSION"
        try:
            graph._log_state("2026-01-15", state)
        except (ValueError, RuntimeError, FileExistsError):
            pass  # Explicit conflict rejection is also valid.
        assert all(p.read_bytes() == body for p, body in before.items()), "Conflicting run content changed a published record"
    finally:
        graph.close_graph_run()


def test_archive_failure_cannot_replace_last_completed_latest(env, monkeypatch):
    cfg, tmp = env
    graph, state, _ = prepare(cfg)
    try:
        state["final_trade_decision"] = "Rating: Buy\nAUDIT_COMPLETED"
        graph._log_state("2026-01-15", state)
        latest = tmp / "reports/600519/TradingAgentsStrategy_logs/full_states_log_2026-01-15.json"
        original = latest.read_bytes()
        second, _, _ = graph.prepare_graph_run("600519", "2026-01-15")
        second["final_trade_decision"] = "Rating: Sell\nAUDIT_FAILED_SAVE"
        write = graph._atomic_write_json

        def fail_version(path, payload, **kwargs):
            if "_runs" in path.parts:
                raise OSError("AUDIT_VERSION_WRITE_FAILURE")
            return write(path, payload, **kwargs)

        monkeypatch.setattr(graph, "_atomic_write_json", fail_version)
        with pytest.raises(OSError, match="AUDIT_VERSION_WRITE_FAILURE"):
            graph._log_state("2026-01-15", second)
        assert latest.read_bytes() == original, "Failed archive replaced the last successful latest report"
    finally:
        graph.close_graph_run()
