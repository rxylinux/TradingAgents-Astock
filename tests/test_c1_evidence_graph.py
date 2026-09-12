"""C1 end-to-end: real StateGraph → branch-isolated wrappers → ToolNode →
evidence tools → evidence_bundle channel → checkpoint/JSON persistence.

No network, no model: vendor fetchers are replaced with offline fixtures at
the lowest level (same seam the A-batch tests use); everything above the
fetcher — routing, artifact flow, branch isolation, reducer, checkpoints,
log persistence — is the production code path.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from tradingagents.agents.utils.agent_states import AgentState
from tradingagents.dataflows import a_stock, interface
from tradingagents.evidence import graph_tools, ledger
from tradingagents.evidence.cutoff import clamp_end_date, get_trusted_cutoff, trusted_cutoff
from tradingagents.evidence.ledger import (
    SCHEMA_BUNDLE,
    bundle_event_count,
    collect_tool_message_delta,
    evidence_reducer,
    render_source_summary,
)
from tradingagents.graph.setup import (
    _branch_isolated_analyst_node,
    _branch_isolated_tool_node,
)

SH = timezone(timedelta(hours=8))


# ---------------------------------------------------------------------------
# Offline vendor fixtures (lowest seam — same pattern as test_batch_a)
# ---------------------------------------------------------------------------

EM_ARTICLES = [
    {"title": "In-window A", "content": "body A", "time": "2024-11-05 11:37:00",
     "source": "东方财富", "url": "https://em/a"},
    {"title": "In-window B", "content": "body B", "time": "2024-11-04 09:05:00",
     "source": "东方财富", "url": "https://em/b"},
    {"title": "Future C", "content": "future body", "time": "2027-01-01 11:00:00",
     "source": "东方财富", "url": "https://em/c"},
    {"title": "Invalid D", "content": "bad time", "time": "not-a-time",
     "source": "东方财富", "url": ""},
]


def _patch_stock_news(monkeypatch, em=None, em_raises=False, sina=None, sina_raises=False):
    if em_raises:
        monkeypatch.setattr(a_stock, "_fetch_news_eastmoney",
                            lambda code, page_size=20: (_ for _ in ()).throw(ConnectionError("EM down")))
    else:
        monkeypatch.setattr(a_stock, "_fetch_news_eastmoney",
                            lambda code, page_size=20: list(em if em is not None else EM_ARTICLES))
    if sina_raises:
        monkeypatch.setattr(a_stock, "_fetch_news_sina",
                            lambda code, page_size=20: (_ for _ in ()).throw(ConnectionError("Sina down")))
    else:
        monkeypatch.setattr(a_stock, "_fetch_news_sina",
                            lambda code, page_size=20: list(sina or []))
    monkeypatch.setattr(interface, "get_vendor", lambda category, method=None: "a_stock")


class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


def _patch_global_news(monkeypatch, cls_items=(), em_items=(), cls_raises=False, em_raises=False):
    cls_payload = {"data": {"roll_data": list(cls_items)}}
    em_payload = {"data": {"fastNewsList": list(em_items)}}

    if cls_raises:
        monkeypatch.setattr(a_stock, "_requests", SimpleNamespace(
            get=lambda *a, **k: (_ for _ in ()).throw(ConnectionError("CLS down"))))
    else:
        monkeypatch.setattr(a_stock, "_requests", SimpleNamespace(
            get=lambda *a, **k: _FakeResp(cls_payload)))
    if em_raises:
        monkeypatch.setattr(a_stock, "_em_get",
                            lambda *a, **k: (_ for _ in ()).throw(ConnectionError("EMG down")))
    else:
        monkeypatch.setattr(a_stock, "_em_get", lambda *a, **k: _FakeResp(em_payload))
    monkeypatch.setattr(interface, "get_vendor", lambda category, method=None: "a_stock")


# ---------------------------------------------------------------------------
# Mini production-shaped graph: two parallel analyst branches with the real
# branch-isolation wrappers and real evidence tools.
# ---------------------------------------------------------------------------

def _analyst_node(calls):
    """Stand-in for the LLM analyst: emit tool calls once, then finish."""
    def node(state):
        msgs = state.get("messages") or []
        if any(getattr(m, "type", "") == "tool" for m in msgs):
            return {"messages": []}
        return {"messages": [AIMessage(content="", tool_calls=list(calls))]}
    return node


def _branch_route(role):
    key = f"{role}_messages"
    tools_node = f"tools_{role}"

    def route(state):
        msgs = state.get(key) or []
        last = msgs[-1] if msgs else None
        if getattr(last, "tool_calls", None):
            return tools_node
        return END
    return route


def _two_branch_graph():
    wf = StateGraph(AgentState)
    for role in ("news", "social"):
        wf.add_node(f"{role}_analyst",
                    _branch_isolated_analyst_node(role, _analyst_node(
                        [{"name": "get_news", "args": {"ticker": "600519", "start_date": "2024-11-01",
                                                       "end_date": "2024-11-05"}, "id": f"call_{role}_1"}])))
        wf.add_node(f"tools_{role}",
                    _branch_isolated_tool_node(role, ToolNode([graph_tools.get_news])))
        wf.add_edge(START, f"{role}_analyst")
        wf.add_conditional_edges(f"{role}_analyst", _branch_route(role), [f"tools_{role}", END])
        wf.add_edge(f"tools_{role}", f"{role}_analyst")
    return wf


def _init_state(run_id="run-e2e-1", trade_date="2024-11-05"):
    return {
        "messages": [("human", "600519")],
        "company_of_interest": "600519",
        "trade_date": trade_date,
        "selected_analysts": ["news", "social"],
        "run_metadata": {"run_id": run_id},
        # 生产 fresh 运行会预置 bundle（trading_graph._prepare_graph_run），
        # 这里以 None 等价预置：确保通道首轮工具写入走 reducer 而非直存。
        "evidence_bundle": None,
    }


class TestRealGraphTwoBranches:
    def test_two_branches_merge_into_one_bundle(self, monkeypatch):
        """Codex C1 point-6 demo: two branches, different tool_call_ids,
        same articles fetched twice → one merged bundle, records deduped,
        both fetch events kept, saved JSON roundtrips."""
        _patch_stock_news(monkeypatch)
        result = _two_branch_graph().compile().invoke(_init_state())

        bundle = result["evidence_bundle"]
        assert bundle["schema"] == SCHEMA_BUNDLE
        assert bundle["run_id"] == "run-e2e-1"
        assert set(bundle["events"]) == {"news:call_news_1", "social:call_social_1"}
        # Both branches fetched the same 2 in-window articles → deduped
        assert len(bundle["records"]) == 2
        titles = {r["title"] for r in bundle["records"]}
        assert titles == {"In-window A", "In-window B"}
        # Two fetch events → two source statuses, each tagged with its event
        assert len(bundle["source_statuses"]) == 2
        assert {s["event"] for s in bundle["source_statuses"]} == set(bundle["events"])
        # Exclusions counted once per fetch event (2 events × 2 excluded)
        assert bundle["exclusions"] == {"发布时间缺失/无效": 2, "发布时间在窗口外": 2}
        # JSON save roundtrip (what _log_state persists)
        assert json.loads(json.dumps(bundle, ensure_ascii=False)) == bundle

        # Model-visible text keeps future/invalid records OUT
        for role in ("news", "social"):
            branch = result[f"{role}_messages"]
            tool_text = str([m for m in branch if getattr(m, "type", "") == "tool"][0].content)
            assert "In-window A" in tool_text
            assert "Future C" not in tool_text and "future body" not in tool_text

    def test_two_tools_same_superstep_two_events(self, monkeypatch):
        _patch_global_news(
            monkeypatch,
            cls_items=[{"title": "CLS item", "content": "cls body",
                        "ctime": int(datetime(2024, 11, 5, 10, 0, tzinfo=SH).timestamp())}],
            em_items=[{"title": "EMG item", "summary": "emg body", "showTime": "2024-11-05 09:30:00"}],
        )
        calls = [
            {"name": "get_global_news", "args": {"curr_date": "2024-11-05", "look_back_days": 7, "limit": 5},
             "id": "call_g1"},
            {"name": "get_news", "args": {"ticker": "600519", "start_date": "2024-11-01",
                                          "end_date": "2024-11-05"}, "id": "call_g2"},
        ]
        wf = StateGraph(AgentState)
        wf.add_node("news_analyst", _branch_isolated_analyst_node("news", _analyst_node(calls)))
        wf.add_node("tools_news", _branch_isolated_tool_node(
            "news", ToolNode([graph_tools.get_news, graph_tools.get_global_news])))
        wf.add_edge(START, "news_analyst")
        wf.add_conditional_edges("news_analyst", _branch_route("news"), ["tools_news", END])
        wf.add_edge("tools_news", "news_analyst")

        _patch_stock_news(monkeypatch, em=[EM_ARTICLES[0]])
        result = wf.compile().invoke(_init_state())

        bundle = result["evidence_bundle"]
        assert set(bundle["events"]) == {"news:call_g1", "news:call_g2"}
        titles = {r["title"] for r in bundle["records"]}
        assert titles == {"CLS item", "EMG item", "In-window A"}
        # CLS publish time kept as full ISO with +08:00 offset
        cls_rec = next(r for r in bundle["records"] if r["title"] == "CLS item")
        assert cls_rec["published_at"].startswith("2024-11-05T10:00:00+08:00")

    @staticmethod
    def _tool_only_graph(role="news"):
        wf = StateGraph(AgentState)
        wf.add_node(f"tools_{role}", _branch_isolated_tool_node(
            role, ToolNode([graph_tools.get_news])))
        wf.add_edge(START, f"tools_{role}")
        wf.add_edge(f"tools_{role}", END)
        return wf.compile()

    def test_replay_same_superstep_no_double_count(self, monkeypatch):
        _patch_stock_news(monkeypatch, em=[EM_ARTICLES[0]])
        app = self._tool_only_graph()
        state = _init_state()
        state["news_messages"] = [AIMessage(content="", tool_calls=[
            {"name": "get_news", "args": {"ticker": "600519", "start_date": "2024-11-01",
                                          "end_date": "2024-11-05"}, "id": "call_x"}])]

        out1 = app.invoke(state)
        out2 = app.invoke(state)  # re-executed superstep (same tool_call_id)
        # Graph level: each execution yields exactly one event in the bundle.
        assert bundle_event_count(out1["evidence_bundle"]) == 1
        assert out1["evidence_bundle"]["events"] == out2["evidence_bundle"]["events"]
        # Reducer level: re-merging the re-executed delta must not double-count.
        def _delta_of(out):
            msgs = [m for m in out["news_messages"]
                    if getattr(m, "tool_call_id", "") == "call_x"]
            return collect_tool_message_delta(
                msgs, role="news", run_id="run-e2e-1", trade_date="2024-11-05")
        merged = evidence_reducer(evidence_reducer(None, _delta_of(out1)), _delta_of(out2))
        assert merged == evidence_reducer(None, _delta_of(out1))
        assert len(merged["source_statuses"]) == 1

    def test_checkpoint_persists_bundle(self, monkeypatch):
        _patch_stock_news(monkeypatch, em=[EM_ARTICLES[0]])
        app = _two_branch_graph().compile(checkpointer=MemorySaver())
        cfg = {"configurable": {"thread_id": "t-c1"}}
        app.invoke(_init_state(run_id="run-ckpt"), cfg)
        snap = app.get_state(cfg)
        assert snap.values["evidence_bundle"]["run_id"] == "run-ckpt"
        assert set(snap.values["evidence_bundle"]["events"]) == {
            "news:call_news_1", "social:call_social_1"}

    def test_restore_same_run_absorbs_new_events_other_run_rejected(self, monkeypatch):
        _patch_stock_news(monkeypatch, em=[EM_ARTICLES[0]])
        app = _two_branch_graph().compile(checkpointer=MemorySaver())
        cfg = {"configurable": {"thread_id": "t-restore"}}
        app.invoke(_init_state(run_id="run-restore"), cfg)
        bundle = app.get_state(cfg).values["evidence_bundle"]

        # Resumed run (same run_id) produces a NEW tool event → absorbed.
        new_state = dict(app.get_state(cfg).values)
        new_state["news_messages"] = [AIMessage(content="", tool_calls=[
            {"name": "get_news", "args": {"ticker": "600519", "start_date": "2024-11-04",
                                          "end_date": "2024-11-05"}, "id": "call_after_restore"}])]
        tool_app = self._tool_only_graph()
        out_new = tool_app.invoke(new_state)
        new_msgs = [m for m in out_new["news_messages"]
                    if getattr(m, "tool_call_id", "") == "call_after_restore"]
        delta = collect_tool_message_delta(
            new_msgs, role="news", run_id="run-restore", trade_date="2024-11-05")
        merged = evidence_reducer(bundle, delta)
        assert set(merged["events"]) == {"news:call_after_restore", "news:call_news_1",
                                         "social:call_social_1"}
        # A delta from a DIFFERENT run must be rejected outright.
        foreign = dict(delta)
        foreign["run_id"] = "run-other"
        assert evidence_reducer(merged, foreign) == merged

    def test_two_concurrent_runs_do_not_cross_contaminate(self, monkeypatch):
        _patch_stock_news(monkeypatch, em=[EM_ARTICLES[0]])
        apps = [_two_branch_graph().compile(checkpointer=MemorySaver()),
                _two_branch_graph().compile(checkpointer=MemorySaver())]
        cfgs = [{"configurable": {"thread_id": "conc-1"}},
                {"configurable": {"thread_id": "conc-2"}}]
        inits = [_init_state(run_id="run-conc-1"), _init_state(run_id="run-conc-2")]

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(app.invoke, init, cfg)
                       for app, init, cfg in zip(apps, inits, cfgs)]
            results = [f.result() for f in futures]

        for res, run_id in zip(results, ("run-conc-1", "run-conc-2")):
            assert res["evidence_bundle"]["run_id"] == run_id
            assert set(res["evidence_bundle"]["events"]) == {
                "news:call_news_1", "social:call_social_1"}


class TestFailureAndBoundaries:
    def test_dual_source_failure_is_failed_event_not_success(self, monkeypatch):
        _patch_stock_news(monkeypatch, em_raises=True, sina_raises=True)
        result = _two_branch_graph().compile().invoke(_init_state())
        bundle = result["evidence_bundle"]
        assert all(e["status"] == "failed" for e in bundle["events"].values())
        assert bundle["records"] == []
        tool_text = str([m for m in result["news_messages"]
                         if getattr(m, "type", "") == "tool"][0].content)
        assert "数据缺失" in tool_text

    def test_partial_failure_marks_partial_with_statuses(self, monkeypatch):
        _patch_stock_news(monkeypatch, em_raises=True,
                          sina=[{"title": "Sina news", "content": "s body",
                                 "time": "2024-11-05 08:00:00", "source": "新浪财经",
                                 "url": "https://s/1"}])
        result = _two_branch_graph().compile().invoke(_init_state())
        bundle = result["evidence_bundle"]
        assert all(e["status"] == "partial" for e in bundle["events"].values())
        by_source = {s["source"]: s for s in bundle["source_statuses"]}
        assert by_source["东方财富"]["status"] == "failed"
        assert by_source["新浪财经"]["status"] == "successful"
        assert len(bundle["records"]) == 1

    def test_trusted_cutoff_blocks_model_future_request(self, monkeypatch):
        """Codex C1 audit R1 graph case: model asks for 2027 while the run's
        trusted date is 2024 — future records reach neither text nor bundle."""
        _patch_stock_news(monkeypatch, em=[EM_ARTICLES[2]])  # the 2027 article
        calls = [{"name": "get_news", "args": {"ticker": "600519", "start_date": "2026-01-01",
                                               "end_date": "2027-01-01"}, "id": "call_future"}]
        wf = StateGraph(AgentState)
        wf.add_node("news_analyst", _branch_isolated_analyst_node("news", _analyst_node(calls)))
        wf.add_node("tools_news", _branch_isolated_tool_node("news", ToolNode([graph_tools.get_news])))
        wf.add_edge(START, "news_analyst")
        wf.add_conditional_edges("news_analyst", _branch_route("news"), ["tools_news", END])
        wf.add_edge("tools_news", "news_analyst")

        result = wf.compile().invoke(_init_state(trade_date="2026-01-01"))
        tool_text = str([m for m in result["news_messages"]
                         if getattr(m, "type", "") == "tool"][0].content)
        assert "Future C" not in tool_text and "future body" not in tool_text
        assert "Future C" not in str(result.get("evidence_bundle"))
        # The clamp is visible to the model and recorded in the artifact.
        assert "已按分析时点截断" in tool_text
        event = result["evidence_bundle"]["events"]["news:call_future"]
        assert event["requested_window"]["effective_end"] == "2026-01-01"
        assert event["requested_window"]["model_requested_end"] == "2027-01-01"

    def test_trusted_cutoff_clamps_global_news_curr_date(self, monkeypatch):
        _patch_global_news(
            monkeypatch,
            cls_items=[{"title": "recent cls", "content": "c",
                        "ctime": int(datetime(2024, 11, 4, 15, 0, tzinfo=SH).timestamp())}],
        )
        calls = [{"name": "get_global_news",
                  "args": {"curr_date": "2025-06-01", "look_back_days": 30, "limit": 5},
                  "id": "call_gc"}]
        wf = StateGraph(AgentState)
        wf.add_node("policy_analyst", _branch_isolated_analyst_node("policy", _analyst_node(calls)))
        wf.add_node("tools_policy", _branch_isolated_tool_node(
            "policy", ToolNode([graph_tools.get_global_news])))
        wf.add_edge(START, "policy_analyst")
        wf.add_conditional_edges("policy_analyst", _branch_route("policy"), ["tools_policy", END])
        wf.add_edge("tools_policy", "policy_analyst")

        result = wf.compile().invoke(_init_state(trade_date="2024-11-05"))
        event = result["evidence_bundle"]["events"]["policy:call_gc"]
        assert event["requested_window"]["effective_end"] == "2024-11-05"
        assert event["requested_window"]["model_requested_end"] == "2025-06-01"
        assert event["requested_window"]["start"] == "2024-10-06"  # clamp respected by vendor window

    def test_no_news_branch_still_collects_from_other_roles(self, monkeypatch):
        """Policy/social also call news tools — collection must not depend on
        a `news` branch existing."""
        _patch_stock_news(monkeypatch, em=[EM_ARTICLES[0]])
        calls = [{"name": "get_news", "args": {"ticker": "600519", "start_date": "2024-11-01",
                                               "end_date": "2024-11-05"}, "id": "call_p1"}]
        wf = StateGraph(AgentState)
        wf.add_node("policy_analyst", _branch_isolated_analyst_node("policy", _analyst_node(calls)))
        wf.add_node("tools_policy", _branch_isolated_tool_node("policy", ToolNode([graph_tools.get_news])))
        wf.add_edge(START, "policy_analyst")
        wf.add_conditional_edges("policy_analyst", _branch_route("policy"), ["tools_policy", END])
        wf.add_edge("tools_policy", "policy_analyst")
        result = wf.compile().invoke(_init_state())
        assert set(result["evidence_bundle"]["events"]) == {"policy:call_p1"}


class TestOldVendorAndLegacyReports:
    def test_string_vendor_routes_same_chain_provenance_unknown(self, monkeypatch):
        """Configured vendor without an evidence twin: same routing, text
        unchanged, artifact provenance=unknown with NO guessed records."""
        _patch_stock_news(monkeypatch, em=[EM_ARTICLES[0]])
        monkeypatch.setitem(interface.VENDOR_METHODS_WITH_EVIDENCE, "get_news", {})

        plain = graph_tools.get_news.invoke(
            {"ticker": "600519", "start_date": "2024-11-01", "end_date": "2024-11-05"})
        text, payload = graph_tools.get_news.func("600519", "2024-11-01", "2024-11-05")
        assert isinstance(plain, str)
        assert "In-window A" in text
        assert payload["provenance"] == "unknown"
        assert payload["records"] == []
        assert any("provenance unknown" in n for n in payload["coverage_notes"])

    def test_legacy_state_without_bundle_renders_empty(self):
        assert render_source_summary(None) == ""
        assert render_source_summary({}) == ""

    def test_log_state_saves_evidence_and_tolerates_legacy(self, tmp_path):
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
        graph.config = {"results_dir": str(tmp_path)}
        graph.ticker = "600519"
        graph.log_states_dict = {}

        state = {"company_of_interest": "600519", "trade_date": "2024-11-05"}
        graph._log_state("2024-11-05", state)
        path = tmp_path / "600519/TradingAgentsStrategy_logs/full_states_log_2024-11-05.json"
        assert json.loads(path.read_text(encoding="utf-8"))["evidence_bundle"] is None

        state["evidence_bundle"] = {
            "schema": SCHEMA_BUNDLE, "run_id": "run-ls", "events": {"news:c1": {}},
            "records": [], "source_statuses": [], "exclusions": {}, "coverage_notes": [],
        }
        graph._log_state("2024-11-05", state)
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["evidence_bundle"]["run_id"] == "run-ls"


class TestVendorTwinPayloads:
    def test_text_identical_between_plain_and_twin(self, monkeypatch):
        _patch_stock_news(monkeypatch)
        for args in [("600519", "2024-11-01", "2024-11-05"),
                     ("600519", "2027-01-01", "2027-02-01")]:
            assert a_stock.get_news(*args) == a_stock.get_news_with_evidence(*args)[0]

    def test_payload_carries_only_filtered_records_full_iso(self, monkeypatch):
        _patch_stock_news(monkeypatch)
        text, payload = a_stock.get_news_with_evidence("600519", "2024-11-01", "2024-11-05")
        assert payload["schema"] == ledger.SCHEMA_ARTIFACT
        assert payload["status"] == "successful"
        assert [r["title"] for r in payload["records"]] == ["In-window A", "In-window B"]
        assert all(r["published_at"].endswith("+08:00") for r in payload["records"])
        assert payload["exclusions"] == {"发布时间缺失/无效": 1, "发布时间在窗口外": 1}

    def test_empty_result_is_empty_not_successful(self, monkeypatch):
        _patch_stock_news(monkeypatch, em=[], sina=[])
        text, payload = a_stock.get_news_with_evidence("600519", "2024-11-01", "2024-11-05")
        assert text == "No news found for A-stock '600519'"
        assert payload["status"] == "empty"
        assert payload["records"] == []
        assert {s["source"]: s["status"] for s in payload["source_statuses"]} == {
            "东方财富": "empty", "新浪财经": "empty"}

    def test_dual_failure_text_and_payload(self, monkeypatch):
        _patch_stock_news(monkeypatch, em_raises=True, sina_raises=True)
        text, payload = a_stock.get_news_with_evidence("600519", "2024-11-01", "2024-11-05")
        assert isinstance(text, a_stock.DataFailure)
        assert payload["status"] == "failed"
        assert all(s["status"] == "failed" for s in payload["source_statuses"])

    def test_global_news_dedup_and_limit_accounting(self, monkeypatch):
        same_time = int(datetime(2024, 11, 5, 10, 0, tzinfo=SH).timestamp())
        _patch_global_news(
            monkeypatch,
            cls_items=[
                {"title": "shared title", "content": "cls copy", "ctime": same_time},
                {"title": "unknown time", "content": "u", "ctime": "garbage"},
            ],
            em_items=[
                {"title": "shared title", "summary": "emg copy", "showTime": "2024-11-05 10:00:00"},
                {"title": "out of window", "summary": "o", "showTime": "2024-01-01 10:00:00"},
                {"title": "kept extra", "summary": "k", "showTime": "2024-11-05 08:00:00"},
            ],
        )
        text, payload = a_stock.get_global_news_with_evidence("2024-11-05", 7, 5)
        # Dedup across sources keeps one record; out-of-window and unknown
        # times never reach records.
        assert [r["title"] for r in payload["records"]] == ["shared title", "kept extra"]
        assert payload["exclusions"] == {
            "发布时间缺失/无效": 1, "发布时间在窗口外": 1, "跨来源标题去重": 1}
        assert payload["status"] == "successful"
        assert a_stock.get_global_news("2024-11-05", 7, 5) == text

    def test_global_news_limit_truncation_note(self, monkeypatch):
        items = [{"title": f"news {i}", "summary": "s", "showTime": "2024-11-05 10:00:00"}
                 for i in range(4)]
        _patch_global_news(monkeypatch, em_items=items)
        text, payload = a_stock.get_global_news_with_evidence("2024-11-05", 7, 2)
        assert len(payload["records"]) == 2
        assert payload["exclusions"].get("超出 limit 截断") == 2
        assert any("limit=2" in n for n in payload["coverage_notes"])

    def test_global_news_dual_failure(self, monkeypatch):
        _patch_global_news(monkeypatch, cls_raises=True, em_raises=True)
        text, payload = a_stock.get_global_news_with_evidence("2024-11-05")
        assert isinstance(text, a_stock.DataFailure)
        assert payload["status"] == "failed"


class TestTrustedCutoffScope:
    def test_no_cutoff_outside_graph(self):
        assert get_trusted_cutoff() is None
        date, clamped = clamp_end_date("2099-01-01")
        assert (date, clamped) == ("2099-01-01", False)

    def test_scope_set_and_reset(self):
        with trusted_cutoff("2026-01-01"):
            assert get_trusted_cutoff() == "2026-01-01"
            assert clamp_end_date("2027-01-01") == ("2026-01-01", True)
            assert clamp_end_date("2025-01-01") == ("2025-01-01", False)
        assert get_trusted_cutoff() is None

    def test_invalid_trade_date_means_no_cutoff(self):
        with trusted_cutoff("not-a-date"):
            assert get_trusted_cutoff() is None
        with trusted_cutoff(""):
            assert get_trusted_cutoff() is None

    def test_malformed_request_left_for_vendor(self):
        with trusted_cutoff("2026-01-01"):
            assert clamp_end_date("2027-13-99") == ("2027-13-99", False)


class _FakeLLM:
    """Free-text-only LLM that captures every prompt it is given."""

    def __init__(self):
        self.prompts = []

    def invoke(self, prompt, config=None):
        self.prompts.append(prompt)
        return SimpleNamespace(content="ok")

    def with_structured_output(self, schema):
        raise NotImplementedError


def _bundle_for_prompt():
    e = ledger.build_fetch_event("news", "call_p", {
        "schema": ledger.SCHEMA_ARTIFACT, "tool": "get_news",
        "provenance": "a_stock", "status": "successful",
        "requested_window": None,
        "records": [rec := {
            "source": "东方财富", "title": "Prompt-index article",
            "content": "body", "url": "", "published_at": "2024-11-05T11:37:00+08:00",
            "time_precision": "datetime",
        }],
        "source_statuses": [{"source": "东方财富", "status": "successful", "record_count": 1}],
        "exclusions": {}, "coverage_notes": [],
    }, trade_date="2024-11-05")
    return evidence_reducer(None, {"schema": ledger.SCHEMA_DELTA,
                                   "run_id": "run-p", "events": {"news:call_p": e}})


class TestDecisionMakerIndexAndDisplay:
    def test_evidence_context_for_prompt(self):
        from tradingagents.evidence.prompt_context import evidence_context_for_prompt
        bundle = _bundle_for_prompt()
        block = evidence_context_for_prompt({"evidence_bundle": bundle})
        assert "证据索引" in block
        assert "Prompt-index article" in block
        assert "ev-东方财富" in block
        assert "来源抓取状态" in block
        assert "引用有效不等于" in block
        # Old state / no bundle → nothing fabricated
        assert evidence_context_for_prompt({}) == ""
        assert evidence_context_for_prompt({"evidence_bundle": None}) == ""

    def test_evidence_context_bounded(self):
        from tradingagents.evidence.prompt_context import evidence_context_for_prompt
        records = [{"source": "s", "title": f"t{i}", "content": "c",
                    "published_at": "2024-11-05T10:00:00+08:00"} for i in range(40)]
        e = ledger.build_fetch_event("news", "call_many", {
            "schema": ledger.SCHEMA_ARTIFACT, "tool": "get_news", "provenance": "a_stock",
            "status": "successful", "requested_window": None, "records": records,
            "source_statuses": [], "exclusions": {}, "coverage_notes": [],
        })
        bundle = evidence_reducer(None, {"schema": ledger.SCHEMA_DELTA, "run_id": "r",
                                         "events": {"news:c": e}})
        block = evidence_context_for_prompt({"evidence_bundle": bundle})
        assert block.count("- [ev-") == 30
        assert "另有 10 条记录未列出" in block

    def test_rm_prompt_receives_evidence_index(self):
        from tradingagents.agents.managers.research_manager import create_research_manager
        llm = _FakeLLM()
        node = create_research_manager(llm)
        base_state = {
            "company_of_interest": "600519",
            "investment_debate_state": {"history": "debate", "count": 1},
        }
        node(dict(base_state))
        assert "证据索引" not in str(llm.prompts[0])
        node(dict(base_state, evidence_bundle=_bundle_for_prompt()))
        assert "Prompt-index article" in str(llm.prompts[1])
        assert "ev-东方财富" in str(llm.prompts[1])

    def test_trader_prompt_receives_evidence_index(self):
        from tradingagents.agents.trader.trader import create_trader
        llm = _FakeLLM()
        node = create_trader(llm)
        base_state = {
            "company_of_interest": "600519",
            "investment_plan": "plan",
        }
        node(dict(base_state))
        assert "证据索引" not in str(llm.prompts[0])
        node(dict(base_state, evidence_bundle=_bundle_for_prompt()))
        assert "Prompt-index article" in str(llm.prompts[1])

    def test_pm_prompt_receives_evidence_index(self):
        from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
        llm = _FakeLLM()
        node = create_portfolio_manager(llm)
        base_state = {
            "company_of_interest": "600519",
            "risk_debate_state": {"history": "risk", "count": 1,
                                  "aggressive_history": "", "conservative_history": "",
                                  "neutral_history": "", "latest_speaker": "",
                                  "current_aggressive_response": "",
                                  "current_conservative_response": "",
                                  "current_neutral_response": "", "judge_decision": ""},
            "investment_plan": "plan",
            "trader_investment_plan": "trader plan",
            "past_context": "",
        }
        node(dict(base_state))
        assert "证据索引" not in str(llm.prompts[0])
        node(dict(base_state, evidence_bundle=_bundle_for_prompt()))
        assert "Prompt-index article" in str(llm.prompts[1])

    def test_render_evidence_md_bundle_and_legacy(self):
        from tradingagents.evidence.display import render_evidence_md
        md = render_evidence_md(_bundle_for_prompt())
        assert "数据来源与证据" in md and "东方财富" in md and "不代表投资准确率" in md
        legacy = render_evidence_md(None)
        assert "未记录来源证据" in legacy

    def test_markdown_export_includes_evidence_section(self):
        from web.pdf_export import generate_markdown
        state = {
            "company_of_interest": "600519",
            "final_trade_decision": "Rating: Buy",
            "evidence_bundle": _bundle_for_prompt(),
        }
        md = generate_markdown(state, "600519", "2024-11-05", "Buy")
        assert "数据来源与证据" in md
        legacy = generate_markdown({"company_of_interest": "600519",
                                    "final_trade_decision": "x"}, "600519", "2024-11-05", "Buy")
        assert "未记录来源证据" in legacy


class TestR2DisplayAndPromptGaps:
    """Codex R2: record list must be inspectable end-to-end; prompt helper
    must keep limitations even when a legacy/failed vendor returned no
    records or statuses."""

    @staticmethod
    def _bundle_with(records=None, statuses=None, exclusions=None, notes=None):
        artifact = {
            "schema": ledger.SCHEMA_ARTIFACT, "tool": "get_news",
            "provenance": "a_stock", "status": "successful",
            "requested_window": None,
            "records": records or [],
            "source_statuses": statuses or [],
            "exclusions": exclusions or {},
            "coverage_notes": notes or [],
        }
        entry = ledger.build_fetch_event("news", "call_r2", artifact, trade_date="2024-11-05")
        return ledger.evidence_reducer(None, {
            "schema": ledger.SCHEMA_DELTA, "run_id": "run-r2",
            "events": {"news:call_r2": entry},
        })

    def test_display_resolves_full_provenance_per_record(self):
        from tradingagents.evidence.display import render_evidence_md
        rec = ledger.normalize_record(
            source="official", title="TRACEABLE", content="excerpt body " * 30,
            url="https://example.test/filing/one",
            published_at="2024-11-05T11:37:00+08:00",
            time_precision="datetime",
            retrieved_at="2024-11-05T12:00:00+08:00")
        md = render_evidence_md(self._bundle_with(records=[rec]))
        for value in [rec["evidence_id"], "TRACEABLE", "official",
                      "https://example.test/filing/one", "11:37", "摘要", "采集", "12:00"]:
            assert value in md, value
        assert "仅发布时点" in md  # publication_time_only 可得性限制

    def test_display_marks_missing_parts_unknown_and_blocks_non_http(self):
        from tradingagents.evidence.display import render_evidence_md
        good = ledger.normalize_record(source="s1", title="has link",
                                       url="https://ok.test/a",
                                       published_at="2024-11-05T10:00:00+08:00")
        nan_url = ledger.normalize_record(source="s2", title="nan url",
                                          url="nan", published_at="2024-11-05T10:00:00+08:00")
        ftp = ledger.normalize_record(source="s3", title="ftp url",
                                      url="ftp://evil.test/x",
                                      published_at="2024-11-05T10:00:00+08:00")
        no_time = ledger.normalize_record(source="s4", title="no time",
                                          url="", published_at="")
        md = render_evidence_md(self._bundle_with(
            records=[good, nan_url, ftp, no_time]))
        assert "https://ok.test/a" in md
        assert "nan" not in md.replace("nan url", "")  # 原始 nan 值不得作为链接出现
        assert "ftp://" not in md
        assert "链接: 未知" in md
        assert "发布时间未知" in md

    def test_prompt_keeps_exclusions_and_notes_without_records_or_statuses(self):
        from tradingagents.evidence.prompt_context import evidence_context_for_prompt
        bundle = self._bundle_with(
            exclusions={"发布时间缺失/无效": 3},
            notes=["SOURCE_PROVENANCE_UNVERIFIABLE"])
        assert bundle["records"] == [] and bundle["source_statuses"] == []
        prompt = evidence_context_for_prompt({"evidence_bundle": bundle})
        assert "SOURCE_PROVENANCE_UNVERIFIABLE" in prompt
        assert "发布时间缺失/无效=3" in prompt

    def test_prompt_keeps_failed_event_status(self):
        from tradingagents.evidence.prompt_context import evidence_context_for_prompt
        bundle = self._bundle_with(
            statuses=[{"source": "CLS Wire", "status": "failed", "record_count": 0}],
            notes=["财联社快讯源不可用"])
        prompt = evidence_context_for_prompt({"evidence_bundle": bundle})
        assert "CLS Wire=failed(0)" in prompt
        assert "财联社快讯源不可用" in prompt
