"""E: independent initial views + bounded news recheck (offline, real nodes).

Contract: docs/E_CODEX_IMPLEMENTATION_CONTRACT_2026-09-09.md rule 10 matrix.
"""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from tradingagents.agents.debate_evidence import (
    ALLOWED_RECHECK_TOOLS,
    MAX_CLAIMS,
    InitialViewModel,
    DisagreementPlanModel,
    QuestionPlan,
    DisagreementItem,
    ViewClaim,
    aggregate_usage,
    create_disagreement_planner_node,
    create_initial_view_node,
    create_recheck_node,
    evidence_debate_summary_for_prompt,
    render_evidence_debate_md,
    validate_plan,
    validate_view,
)
from tradingagents.agents.utils.agent_states import AgentState
from tradingagents.graph.conditional_logic import ConditionalLogic
from tradingagents.dataflows import a_stock, interface
from tradingagents.evidence import graph_tools, ledger
from tradingagents.evidence.ledger import evidence_reducer

SHArticles = [
    {"title": "E-In-window", "content": "e body", "time": "2024-11-05 11:37:00",
     "source": "东方财富", "url": "https://em/e"},
]


def _patch_news(monkeypatch, articles=None, fail=False, calls=None):
    def fetch(code, page_size=20):
        if calls is not None:
            calls.append(code)
        if fail:
            raise ConnectionError("vendor down")
        return list(articles if articles is not None else SHArticles)

    monkeypatch.setattr(a_stock, "_fetch_news_eastmoney", fetch)

    def _sina(code, page_size=20):
        if fail:
            raise ConnectionError("sina down")
        return []
    monkeypatch.setattr(a_stock, "_fetch_news_sina", _sina)
    monkeypatch.setattr(interface, "get_vendor", lambda c, method=None: "a_stock")


def _bundle_with(articles=None, run_id="run-e"):
    from tests.test_c1_evidence_graph import _patch_stock_news  # noqa: F401
    art = articles if articles is not None else SHArticles
    entry = ledger.build_fetch_event("news", "seed-call", {
        "schema": ledger.SCHEMA_ARTIFACT, "tool": "get_news",
        "provenance": "a_stock", "status": "successful", "requested_window": None,
        "records": [{"source": a.get("source"), "title": a["title"],
                     "content": a.get("content", ""), "url": a.get("url", ""),
                     "published_at": "2024-11-05T11:37:00+08:00",
                     "time_precision": "datetime"} for a in art],
        "source_statuses": [{"source": "东方财富", "status": "successful",
                             "record_count": len(art)}],
        "exclusions": {}, "coverage_notes": [],
    }, trade_date="2024-11-05")
    return evidence_reducer(None, {"schema": ledger.SCHEMA_DELTA, "run_id": run_id,
                                   "events": {"news:seed": entry}})


def _base_state(ed_bundle=None, run_id="run-e"):
    state = {
        "messages": [("human", "600519")], "company_of_interest": "600519",
        "trade_date": "2024-11-05", "selected_analysts": ["news"],
        "run_metadata": {"run_id": run_id},
        "market_report": "m", "sentiment_report": "s", "news_report": "n",
        "fundamentals_report": "f", "policy_report": "", "hot_money_report": "",
        "lockup_report": "", "data_quality_summary": "",
    }
    if ed_bundle is not None:
        state["evidence_bundle"] = ed_bundle
    return state


class _StubStructured:
    """Structured-capable stub; returns scripted model objects, captures prompts."""

    def __init__(self, decisions):
        self.queue = list(decisions)
        self.prompts = []

    def invoke(self, prompt, config=None, **kwargs):
        self.prompts.append(prompt)
        return SimpleNamespace(content="stubbed")

    def with_structured_output(self, schema, **kwargs):
        outer = self

        class _S:
            def invoke(self, prompt, config=None, **kw):
                outer.prompts.append(prompt)
                item = outer.queue.pop(0) if outer.queue else None
                if isinstance(item, Exception):
                    raise item
                return item

        return _S()


class TestValidateView:
    def _view(self, claims):
        return InitialViewModel(direction="long", top_claims=claims)

    def test_invalid_refs_kept_with_verdicts(self):
        bundle = _bundle_with()
        rid = bundle["records"][0]["evidence_id"]
        view = self._view([ViewClaim(claim="ok", evidence_ids=[rid]),
                           ViewClaim(claim="bad", evidence_ids=["ev-nope"]),
                           ViewClaim(claim="none", evidence_ids=[])])
        out = validate_view(view, _base_state(bundle), "bull")
        assert [c["status"] for c in out["claims"]] == \
            ["all_refs_valid", "has_invalid_refs", "no_refs"]
        assert out["claims"][1]["claim"] == "bad"  # 不删除制造合规率
        assert out["claims"][1]["invalid_refs"] == ["ev-nope"]
        assert all(c["confidence_calibration"] == "uncalibrated" for c in out["claims"])

    def test_caps_and_truncation_notes(self):
        view = self._view([ViewClaim(claim=f"c{i}", evidence_ids=[]) for i in range(12)])
        out = validate_view(view, _base_state(), "bull")
        assert len(out["claims"]) == MAX_CLAIMS
        assert out["stats"]["claims_truncated"] == 2
        assert out.get("limitations")


class TestValidatePlan:
    def _plan(self, questions):
        disagreements = [DisagreementItem(
            topic=f"t{i}", decision_impact=f"impact{i}",
            conflict_kind="fact") for i in range(3)]
        return DisagreementPlanModel(
            disagreements=disagreements, recheck_questions=questions)

    def _q(self, **kw):
        base = dict(question="q", disagreement_index=0, decision_impact="d",
                    tool_name="get_news",
                    tool_args={"ticker": "600519", "start_date": "2024-10-01",
                               "end_date": "2024-11-05"})
        base.update(kw)
        return QuestionPlan(**base)

    def test_five_questions_only_two_eligible(self):
        plan = self._plan([self._q(question=f"q{i}") for i in range(5)])
        out = validate_plan(plan)
        pending = [q for q in out["recheck_questions"] if q["status"] == "pending"]
        assert [q["slot"] for q in pending] == [0, 1]
        assert sum(1 for q in out["recheck_questions"] if q["status"] == "not_rechecked") == 3

    def test_invalid_variants_unresolved(self):
        plan = self._plan([
            self._q(decision_impact=""),
            self._q(disagreement_index=99),
            self._q(tool_name="get_stock_data"),
            self._q(tool_args={"ticker": "600519"}),
        ])
        out = validate_plan(plan)
        statuses = [q["status"] for q in out["recheck_questions"]]
        assert statuses == ["unresolved(empty_decision_impact)",
                            "unresolved(disagreement_index_out_of_bounds)",
                            "unresolved(tool_not_allowed)",
                            "unresolved(invalid_tool_args)"]


class TestE1Nodes:
    def test_mutual_blindness_and_usage(self):
        bundle = _bundle_with()
        rid = bundle["records"][0]["evidence_id"]
        bear_view = validate_view(
            InitialViewModel(direction="short", top_claims=[
                ViewClaim(claim="BEAR_SECRET_CLAIM", evidence_ids=[rid])]),
            _base_state(bundle), "bear")
        state = _base_state(bundle)
        state["initial_view_bear"] = bear_view
        stub = _StubStructured([InitialViewModel(
            direction="long", top_claims=[ViewClaim(claim="BULL_CLAIM", evidence_ids=[rid])])])
        out = create_initial_view_node("bull", stub)(state)
        bull_prompt = str(stub.prompts[0])
        assert "BEAR_SECRET_CLAIM" not in bull_prompt  # 互盲
        assert "E-In-window" in bull_prompt  # 证据索引投影在
        view = out["initial_view_bull"]
        assert view["claims"][0]["status"] == "all_refs_valid"
        assert view["usage"]["actual_requests"] == 1
        assert view["output_mode"] == "structured"

    def test_freetext_fallback_no_fake_claims(self):
        class _Free:
            def __init__(self):
                self.prompts = []

            def invoke(self, prompt, config=None, **kwargs):
                self.prompts.append(prompt)
                return SimpleNamespace(content="free text")

            def with_structured_output(self, schema, **kwargs):
                raise NotImplementedError

        out = create_initial_view_node("bull", _Free())(_base_state(_bundle_with()))
        view = out["initial_view_bull"]
        assert view["output_mode"] == "freetext_fallback"
        assert view["claims"] == []
        assert any("不伪造" in lim for lim in view["limitations"])

    def test_planner_reads_views_and_gates(self):
        bundle = _bundle_with()
        rid = bundle["records"][0]["evidence_id"]
        state = _base_state(bundle)
        state["initial_view_bull"] = validate_view(
            InitialViewModel(direction="long", top_claims=[ViewClaim(claim="b", evidence_ids=[rid])]),
            state, "bull")
        state["initial_view_bear"] = validate_view(
            InitialViewModel(direction="short", top_claims=[ViewClaim(claim="r", evidence_ids=[])])
            , state, "bear")
        qs = [QuestionPlan(question=f"q{i}", disagreement_index=0,
                           decision_impact="d", tool_name="get_news",
                           tool_args={"ticker": "600519", "start_date": "2024-10-01",
                                      "end_date": "2024-11-05"}) for i in range(3)]
        stub = _StubStructured([DisagreementPlanModel(
            disagreements=[DisagreementItem(topic="t", decision_impact="i", conflict_kind="fact")],
            recheck_questions=qs)])
        out = create_disagreement_planner_node(stub)(state)
        ed = out["evidence_debate"]
        assert ed["eligible_questions"] == 2
        assert ed["run_id"] == "run-e"
        usage = ed["usage"]
        assert usage["logical_call_count"] == 3
        assert usage["tool_invokes"] == 0
        assert usage["known_tokens"] == "unknown"  # usage 缺失记 unknown 而非 0

    def test_usage_unknown_not_zero(self):
        u = aggregate_usage(None, None, planner_counter=None)
        assert u["known_tokens"] == "unknown"
        assert u["actual_request_count"] == "unknown"




def _run_node(node_fn, state):
    """Run one E node through a real compiled graph (ambient config for
    ToolNode — identical to production in-graph execution)."""
    wf = StateGraph(AgentState)
    wf.add_node("e_node", node_fn)
    wf.add_edge(START, "e_node")
    wf.add_edge("e_node", END)
    return wf.compile().invoke(state)

class TestE2Recheck:
    def _question_state(self, monkeypatch, questions, bundle=None, calls=None,
                        articles=None, fail=False):
        _patch_news(monkeypatch, articles=articles, fail=fail, calls=calls)
        state = _base_state(bundle if bundle is not None else _bundle_with())
        state["evidence_debate"] = validate_plan(DisagreementPlanModel(
            disagreements=[DisagreementItem(topic="t", decision_impact="i", conflict_kind="fact")],
            recheck_questions=questions))
        return state

    def _q(self, **kw):
        base = dict(question="recheck q", disagreement_index=0, decision_impact="d",
                    tool_name="get_news",
                    tool_args={"ticker": "600519", "start_date": "2024-10-01",
                               "end_date": "2024-11-05"})
        base.update(kw)
        return QuestionPlan(**base)

    def test_single_invoke_and_c1_artifact_path(self, monkeypatch):
        calls = []
        state = self._question_state(monkeypatch, [self._q()], calls=calls)
        out = _run_node(create_recheck_node(0, [graph_tools.get_news, graph_tools.get_global_news]), state)
        assert calls == ["600519"]  # 恰好一次实际工具调用
        ed = out["evidence_debate"]
        r = ed["recheck_questions"][0]["recheck"]
        assert r["status"] == "retrieved"
        assert r["evidence_ids"]
        assert "resolved_side" not in r and "resolved" not in r
        assert r["human_review"] is True
        delta = out["evidence_bundle"]
        assert f"recheck_q0:" in str(delta["events"])
        assert delta["run_id"] == "run-e"
        assert ed["usage"]["tool_invokes"] == 1

    def test_future_date_clamped_before_invoke(self, monkeypatch):
        state = self._question_state(
            monkeypatch,
            [self._q(tool_args={"ticker": "600519", "start_date": "2026-01-01",
                                "end_date": "2027-01-01"})])
        out = _run_node(create_recheck_node(0, [graph_tools.get_news, graph_tools.get_global_news]), state)
        r = out["evidence_debate"]["recheck_questions"][0]["recheck"]
        # end 收窄到 2024-11-05 后 start(2026-01-01) > end → 前置拒绝，零调用
        assert r["status"] == "unresolved"
        assert "reversed_window" in r["note"]

    def test_invalid_ticker_no_invoke(self, monkeypatch):
        calls = []
        state = self._question_state(
            monkeypatch,
            [self._q(tool_args={"ticker": "not-a-code", "start_date": "2024-10-01",
                                "end_date": "2024-11-05"})], calls=calls)
        out = _run_node(create_recheck_node(0, [graph_tools.get_news]), state)
        assert calls == []  # 未发起调用
        r = out["evidence_debate"]["recheck_questions"][0]["recheck"]
        assert r["status"] == "unresolved"
        # 失配标的在格式校验之前就被可信标的绑定拦下（两种原因都零调用）
        assert "unresolved(" in r["note"]

    def test_vendor_failure_is_error_status(self, monkeypatch):
        state = self._question_state(monkeypatch, [self._q()], fail=True)
        out = _run_node(create_recheck_node(0, [graph_tools.get_news, graph_tools.get_global_news]), state)
        r = out["evidence_debate"]["recheck_questions"][0]["recheck"]
        assert r["status"] == "error"

    def test_empty_fetch_is_inconclusive(self, monkeypatch):
        state = self._question_state(monkeypatch, [self._q()], articles=[])
        out = _run_node(create_recheck_node(0, [graph_tools.get_news, graph_tools.get_global_news]), state)
        r = out["evidence_debate"]["recheck_questions"][0]["recheck"]
        assert r["status"] == "inconclusive"

    def test_repost_marked_not_voted(self, monkeypatch):
        # 已有同内容记录（不同来源/不同 ID）→ 标记重复、不投票
        existing = _bundle_with()
        state = self._question_state(monkeypatch, [self._q()], bundle=existing)
        out = _run_node(create_recheck_node(0, [graph_tools.get_news, graph_tools.get_global_news]), state)
        r = out["evidence_debate"]["recheck_questions"][0]["recheck"]
        assert r["repost_duplicates"], r  # 同 content_digest 已在账本
        assert "不投票" in r["note"]

    def test_consumed_slot_never_rerun(self, monkeypatch):
        calls = []
        state = self._question_state(monkeypatch, [self._q()], calls=calls)
        node = create_recheck_node(0, [graph_tools.get_news, graph_tools.get_global_news])
        first = _run_node(node, state)
        assert calls == ["600519"]
        # 恢复窗口守卫：已完成槽位再次进入节点 → 不再调用
        second = _run_node(node, first)
        assert calls == ["600519"]
        assert second.get("evidence_debate", first["evidence_debate"]) == first["evidence_debate"]

    def test_param_ceiling_clamped(self, monkeypatch):
        calls = []
        state = self._question_state(
            monkeypatch,
            [QuestionPlan(question="g", disagreement_index=0, decision_impact="d",
                          tool_name="get_global_news",
                          tool_args={"curr_date": "2024-11-05", "look_back_days": 500,
                                     "limit": 999})], calls=calls)
        out = _run_node(create_recheck_node(0, [graph_tools.get_news, graph_tools.get_global_news]), state)
        r = out["evidence_debate"]["recheck_questions"][0]["recheck"]
        assert r["effective_args"]["look_back_days"] == 30
        assert r["effective_args"]["limit"] == 20


class TestRenderAndSummary:
    def _ed_state(self):
        bundle = _bundle_with()
        rid = bundle["records"][0]["evidence_id"]
        state = _base_state(bundle)
        state["initial_view_bull"] = validate_view(
            InitialViewModel(direction="long", top_claims=[ViewClaim(claim="b", evidence_ids=[rid])]),
            state, "bull")
        state["initial_view_bear"] = validate_view(
            InitialViewModel(direction="short", top_claims=[ViewClaim(claim="r", evidence_ids=[])]),
            state, "bear")
        state["evidence_debate"] = validate_plan(DisagreementPlanModel(
            disagreements=[DisagreementItem(topic="t", decision_impact="i", conflict_kind="fact")],
            recheck_questions=[QuestionPlan(
                question="q", disagreement_index=0, decision_impact="d",
                tool_name="get_news",
                tool_args={"ticker": "600519", "start_date": "2024-10-01",
                           "end_date": "2024-11-05"})]))
        return state

    def test_summary_and_render(self):
        state = self._ed_state()
        summary = evidence_debate_summary_for_prompt(state)
        assert "独立初判" in summary and "uncalibrated" in summary
        assert "≠语义支持" in summary
        md = render_evidence_debate_md(state)
        assert "独立初判与分歧核查" in md and "看多初判" in md and "用量" in md
        assert "resolved_side" not in md

    def test_legacy_renders_unrecorded(self):
        assert "未记录" in render_evidence_debate_md({})
        assert evidence_debate_summary_for_prompt({}) == ""

    def test_markdown_export_includes_section(self):
        from web.pdf_export import generate_markdown
        md = generate_markdown(dict(self._ed_state(), final_trade_decision="x"),
                               "600519", "2024-11-05", "Buy")
        assert "独立初判与分歧核查" in md
        legacy = generate_markdown({"company_of_interest": "600519",
                                    "final_trade_decision": "x"}, "600519", "2024-11-05", "Buy")
        assert "未记录" in legacy


class TestGraphTopology:
    def _graph_setup(self, enabled):
        from tradingagents.graph.setup import GraphSetup
        from tradingagents.graph.conditional_logic import ConditionalLogic
        return GraphSetup(
            quick_thinking_llM := _StubStructured([]),  # noqa: F841 — 仅构建，不调用
            deep_thinking_llm=_StubStructured([]),
            tool_nodes={"news": ToolNode([graph_tools.get_news])},
            conditional_logic=ConditionalLogic(),
            evidence_debate_enabled=enabled,
        )

    def test_default_off_topology_unchanged(self):
        setup = self._graph_setup(False)
        app = setup.setup_graph(["news"]).compile()
        nodes = set(app.get_graph().nodes)
        for e_node in ("Bull Initial View", "Bear Initial View",
                       "Disagreement Planner", "Recheck Q1", "Recheck Q2"):
            assert e_node not in nodes
        # 原有关键边：Quality Gate 直连 Bull Researcher（拓扑等价于 E 之前）
        edges = {(e[0], e[1]) for e in app.get_graph().edges}
        assert ("Quality Gate", "Bull Researcher") in edges

    def test_enabled_registers_e_chain(self):
        setup = self._graph_setup(True)
        app = setup.setup_graph(["news"]).compile()
        nodes = set(app.get_graph().nodes)
        assert {"Bull Initial View", "Bear Initial View", "Disagreement Planner",
                "Recheck Q1", "Recheck Q2"} <= nodes
        edges = {(e[0], e[1]) for e in app.get_graph().edges}
        assert ("Quality Gate", "Bull Researcher") not in edges
        assert ("Quality Gate", "Bull Initial View") in edges
        assert ("Recheck Q2", "Bull Researcher") in edges


def _full_e_graph(stub_bull, stub_bear, stub_planner):
    """Focused production-shaped E chain on AgentState (real E nodes)."""
    wf = StateGraph(AgentState)
    wf.add_node("bull_view", create_initial_view_node("bull", stub_bull))
    wf.add_node("bear_view", create_initial_view_node("bear", stub_bear))
    wf.add_node("planner", create_disagreement_planner_node(stub_planner))
    recheck_tools = [graph_tools.get_news, graph_tools.get_global_news]
    wf.add_node("recheck_q1", create_recheck_node(0, recheck_tools))
    wf.add_node("recheck_q2", create_recheck_node(1, recheck_tools))
    wf.add_node("debate_marker", lambda state: {"sender": "debate"})
    wf.add_edge(START, "bull_view")
    wf.add_edge(START, "bear_view")
    wf.add_edge(["bull_view", "bear_view"], "planner")

    def _route(slot):
        def route(state):
            ed = state.get("evidence_debate") or {}
            if any(q.get("slot") == slot and q.get("status") == "pending"
                   for q in ed.get("recheck_questions") or []):
                return f"recheck_q{slot + 1}"
            return "debate_marker"
        return route

    wf.add_conditional_edges("planner", _route(0), ["recheck_q1", "debate_marker"])
    wf.add_conditional_edges("recheck_q1", _route(1), ["recheck_q2", "debate_marker"])
    wf.add_edge("recheck_q2", "debate_marker")
    wf.add_edge("debate_marker", END)
    return wf


def _plan_questions(n):
    return [QuestionPlan(question=f"q{i}", disagreement_index=0, decision_impact="d",
                         tool_name="get_news",
                         tool_args={"ticker": "600519", "start_date": "2024-10-01",
                                    "end_date": "2024-11-05"}) for i in range(n)]


def _view_model(claim, refs):
    ids = [refs] if isinstance(refs, str) else list(refs)
    return InitialViewModel(direction="long",
                            top_claims=[ViewClaim(claim=claim, evidence_ids=ids)])


class TestFullChain:
    def test_end_to_end_two_invokes_budget_and_blindness(self, monkeypatch):
        calls = []
        _patch_news(monkeypatch, calls=calls)
        bundle = _bundle_with()
        rid = bundle["records"][0]["evidence_id"]
        stub_bull = _StubStructured([_view_model("BULL_X", rid)])
        stub_bear = _StubStructured([_view_model("BEAR_Y", [])])
        stub_planner = _StubStructured([DisagreementPlanModel(
            disagreements=[DisagreementItem(topic="t", decision_impact="i",
                                            conflict_kind="fact")],
            recheck_questions=_plan_questions(5))])  # 5 问只补 2
        app = _full_e_graph(stub_bull, stub_bear, stub_planner).compile()
        result = app.invoke(_base_state(bundle))
        assert calls == ["600519", "600519"]  # 总计恰好 2 次实际工具调用
        ed = result["evidence_debate"]
        assert ed["eligible_questions"] == 2
        statuses = [q["status"] for q in ed["recheck_questions"]]
        assert statuses.count("done") == 2 and statuses.count("not_rechecked") == 3
        assert ed["usage"]["tool_invokes"] == 2
        assert ed["usage"]["logical_call_count"] == 3
        # 互盲：bull 的提示词不含 bear 初判内容
        assert "BEAR_Y" not in str(stub_bull.prompts[0])
        # 补查证据进入账本（C1 delta 通路，role=recheck_q*）
        final_bundle = result["evidence_bundle"]
        assert any(k.startswith("recheck_q") for k in final_bundle["events"])
        # RM/辩论消费的受限摘要存在且不含 resolved 语言
        summary = evidence_debate_summary_for_prompt(result)
        assert "独立初判" in summary and "resolved" not in summary

    def test_concurrent_runs_isolated(self, monkeypatch):
        calls = []
        _patch_news(monkeypatch, calls=calls)
        bundle = _bundle_with(run_id="run-e1")
        rid = bundle["records"][0]["evidence_id"]
        results = []
        for run_id in ("run-e1", "run-e2"):
            b = _bundle_with(run_id=run_id)
            stub_bull = _StubStructured([_view_model("B", rid)])
            stub_bear = _StubStructured([_view_model("R", [])])
            stub_planner = _StubStructured([DisagreementPlanModel(
                disagreements=[DisagreementItem(topic="t", decision_impact="i",
                                                conflict_kind="fact")],
                recheck_questions=_plan_questions(1))])
            app = _full_e_graph(stub_bull, stub_bear, stub_planner).compile()
            results.append(app.invoke(_base_state(b, run_id=run_id)))
        assert [r["evidence_debate"]["run_id"] for r in results] == ["run-e1", "run-e2"]
        for r in results:
            recheck_roles = {k.split(":")[0] for k in r["evidence_bundle"]["events"]}
            assert "recheck_q0" in recheck_roles


class TestResumeAndMismatch:
    def test_interrupt_resume_no_rerun_and_budget_persisted(self, monkeypatch):
        from langgraph.checkpoint.memory import MemorySaver
        calls = []
        _patch_news(monkeypatch, calls=calls)
        bundle = _bundle_with()
        rid = bundle["records"][0]["evidence_id"]
        stub_bull = _StubStructured([_view_model("B", rid)])
        stub_bear = _StubStructured([_view_model("R", [])])
        stub_planner = _StubStructured([DisagreementPlanModel(
            disagreements=[DisagreementItem(topic="t", decision_impact="i",
                                            conflict_kind="fact")],
            recheck_questions=_plan_questions(2))])
        app = _full_e_graph(stub_bull, stub_bear, stub_planner).compile(
            checkpointer=MemorySaver(), interrupt_before=["recheck_q2"])
        cfg = {"configurable": {"thread_id": "e-resume"}}
        state = app.invoke(_base_state(bundle), cfg)
        assert calls == ["600519"]  # q1 已消费
        snapshot = app.get_state(cfg)
        assert snapshot.next == ("recheck_q2",)
        ed_before = json.dumps(state["evidence_debate"], sort_keys=True,
                               ensure_ascii=False, default=str)
        usage_before = state["evidence_debate"]["usage"]["tool_invokes"]
        resumed = app.invoke(None, cfg)  # 真正续跑
        assert resumed["sender"] == "debate"
        assert calls == ["600519", "600519"]  # 只补跑 q2，q1 未重跑
        assert resumed["evidence_debate"]["usage"]["tool_invokes"] == usage_before + 1
        # 已完成槽位（q1）的补查结果在恢复前后逐字节一致（不重算）；
        # 未消费槽位（q2）由恢复路径继续消费。
        qs_before = json.loads(ed_before)["recheck_questions"]
        q1_before = next(q for q in qs_before if q.get("slot") == 0)
        q1_after = next(q for q in resumed["evidence_debate"]["recheck_questions"]
                        if q.get("slot") == 0)
        assert json.dumps(q1_after, sort_keys=True, default=str) == \
            json.dumps(q1_before, sort_keys=True, default=str)
        assert [q["status"] for q in resumed["evidence_debate"]["recheck_questions"]] == \
            ["done", "done"]

    def _check(self, values, enabled=False):
        from tradingagents.graph.trading_graph import TradingAgentsGraph
        g = object.__new__(TradingAgentsGraph)
        g.config = {"evidence_debate_enabled": enabled}
        return TradingAgentsGraph._validate_resumed_evidence_debate(g, values)

    def test_mismatch_refused_both_directions(self):
        # 断点含 E 字段 + 当前关闭 → 拒绝
        with pytest.raises(RuntimeError, match="拓扑不一致"):
            self._check({"evidence_debate": {"schema_version": 1}}, enabled=False)
        # 无锚旧态 + 当前开启（即使辩论未开始）→ 无法证明阶段兼容 → 拒绝
        with pytest.raises(RuntimeError, match="锚点"):
            self._check({"investment_debate_state": {"count": 1}}, enabled=True)
        # 合法：关闭 + 无字段（旧态同模式兼容）
        self._check({})
        # 合法：开启 + 匹配锚点 + 完好 E 字段
        ok = {"run_metadata": {"run_id": "r1", "evidence_debate": {
                  "enabled": True, "schema_version": 1}},
              "evidence_debate": {"schema_version": 1, "run_id": "r1",
                                  "recheck_questions": [], "usage": {}}}
        self._check(ok, enabled=True)


class TestLogState:
    def test_log_state_persists_e_fields_and_legacy(self, tmp_path):
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
        graph.config = {"results_dir": str(tmp_path)}
        graph.ticker = "600519"
        graph.log_states_dict = {}
        graph._log_state("2024-11-05", {"company_of_interest": "600519"})
        path = tmp_path / "600519/TradingAgentsStrategy_logs/full_states_log_2024-11-05.json"
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["evidence_debate"] is None
        assert saved["initial_view_bull"] is None

        graph._log_state("2024-11-05", {
            "company_of_interest": "600519",
            "evidence_debate": {"schema_version": 1, "usage": {"tool_invokes": 2}}})
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["evidence_debate"]["usage"]["tool_invokes"] == 2


import httpx as _httpx


class TestER1Boundaries:
    """Codex E R1 六组边界的本地镜像（真实 HTTP transport / 真实图路径）。"""

    @staticmethod
    def _offline_client(handler, retries=0, max_tokens=9000):
        import httpx
        from tradingagents.llm_clients.openai_client import NormalizedChatOpenAI
        return NormalizedChatOpenAI(
            model="gpt-4o", api_key="offline-test", base_url="https://offline.invalid/v1",
            max_retries=retries, max_tokens=max_tokens,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    @staticmethod
    def _success(req):
        body = json.loads(req.content)
        name = body["tools"][0]["function"]["name"]
        return _httpx.Response(200, json={
            "id": "offline", "object": "chat.completion", "created": 0, "model": "gpt-4o",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": None,
                         "tool_calls": [{"id": "t", "type": "function", "function": {
                             "name": name,
                             "arguments": json.dumps({"direction": "long", "top_claims": []})}}]},
                         "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

    def test_output_cap_reaches_http_and_shared_client_unchanged(self):
        requests = []

        def send(req):
            requests.append(json.loads(req.content))
            return self._success(req)

        llm = self._offline_client(send)
        create_initial_view_node("bull", llm)(_base_state(_bundle_with()))
        assert len(requests) == 1
        assert requests[0].get("max_completion_tokens",
                               requests[0].get("max_tokens")) <= 2048
        assert llm.max_tokens == 9000  # 共享客户端未被改写

    def test_explicit_zero_retry_single_http_request(self, monkeypatch):
        requests = []

        def send(req):
            requests.append(req)
            return _httpx.Response(429, json={"error": {"message": "limit",
                                                        "type": "rate_limit_error"}})

        monkeypatch.setattr("tradingagents.agents.utils.structured.time.sleep",
                            lambda _: None)
        try:
            create_initial_view_node("bull", self._offline_client(send, retries=0))(
                _base_state(_bundle_with()))
        except Exception:
            pass  # 结果可控失败或抛出均可；计数独立可观测
        assert len(requests) == 1

    def test_cross_instrument_never_reaches_vendor(self, monkeypatch):
        fetched = []
        _patch_news(monkeypatch, calls=fetched)
        from tradingagents.agents.debate_evidence import DisagreementPlanModel, DisagreementItem, validate_plan
        state = _base_state(_bundle_with())
        state["evidence_debate"] = validate_plan(DisagreementPlanModel(
            disagreements=[DisagreementItem(topic="t", decision_impact="i", conflict_kind="fact")],
            recheck_questions=[QuestionPlan(
                question="q", disagreement_index=0, decision_impact="d",
                tool_name="get_news",
                tool_args={"ticker": "000001", "start_date": "2024-10-01",
                           "end_date": "2024-11-05"})]))
        out = _run_node(create_recheck_node(0, [graph_tools.get_news]), state)
        assert fetched == []  # 可信标的 600519；000001 未到供应商
        r = out["evidence_debate"]["recheck_questions"][0]["recheck"]
        assert "instrument_mismatch" in r["note"]

    def test_planner_whitelist_gates_unused_tool(self):
        # 规划白名单只含 get_news → get_global_news 问题 unresolved(tool_not_allowed)
        plan = DisagreementPlanModel(
            disagreements=[DisagreementItem(topic="t", decision_impact="i", conflict_kind="fact")],
            recheck_questions=[QuestionPlan(
                question="g", disagreement_index=0, decision_impact="d",
                tool_name="get_global_news",
                tool_args={"curr_date": "2024-11-05"})])
        out = validate_plan(plan, allowed_tools=("get_news",))
        assert out["recheck_questions"][0]["status"] == "unresolved(tool_not_allowed)"

    def test_bool_limit_rejected(self):
        plan = DisagreementPlanModel(
            disagreements=[DisagreementItem(topic="t", decision_impact="i", conflict_kind="fact")],
            recheck_questions=[QuestionPlan(
                question="g", disagreement_index=0, decision_impact="d",
                tool_name="get_global_news",
                tool_args={"curr_date": "2024-11-05", "limit": True})])
        out = validate_plan(plan)
        assert out["recheck_questions"][0]["status"] == "unresolved(invalid_tool_args)"

    def test_stock_window_finite_lookback_narrowed(self, monkeypatch):
        fetched = []
        _patch_news(monkeypatch, calls=fetched)
        state = _base_state(_bundle_with())
        from tradingagents.agents.debate_evidence import DisagreementPlanModel, DisagreementItem, validate_plan
        state["evidence_debate"] = validate_plan(DisagreementPlanModel(
            disagreements=[DisagreementItem(topic="t", decision_impact="i", conflict_kind="fact")],
            recheck_questions=[QuestionPlan(
                question="q", disagreement_index=0, decision_impact="d",
                tool_name="get_news",
                tool_args={"ticker": "600519", "start_date": "2018-01-01",
                           "end_date": "2024-11-05"})]))
        out = _run_node(create_recheck_node(0, [graph_tools.get_news]), state)
        r = out["evidence_debate"]["recheck_questions"][0]["recheck"]
        assert "start_date_narrowed_to_finite_window" in r["hardening_note"]
        assert r["effective_args"]["start_date"] == "2023-11-06"  # 366 天窗口起点

    def test_summary_budget_includes_trailer(self):
        # 绕过 validate_plan 的字段级截断，直接构造超预算分歧清单
        state = _base_state(_bundle_with())
        state["evidence_debate"] = {
            "schema_version": 1,
            "disagreements": [{"topic": "x" * 500, "conflict_kind": "fact",
                               "decision_impact": "i" * 300} for _ in range(30)],
            "recheck_questions": [], "usage": {}}
        block = evidence_debate_summary_for_prompt(state, max_chars=1800)
        assert len(block) <= 1800
        assert "截断明示" in block


class TestER2Boundaries:
    """Codex E R2 七项边界 + 相邻路径（真实 prepare/SQLite 恢复/下游摘要）。"""

    def test_planner_empty_whitelist_stays_empty(self):
        stub = _StubStructured([DisagreementPlanModel(
            disagreements=[DisagreementItem(topic="t", decision_impact="i", conflict_kind="fact")],
            recheck_questions=[QuestionPlan(question="q", disagreement_index=0,
                                            decision_impact="d",
                                            tool_name="get_global_news",
                                            tool_args={"curr_date": "2024-11-05"})])])
        ed = create_disagreement_planner_node(stub, allowed_tools=[])(
            _base_state(_bundle_with()))["evidence_debate"]
        assert ed["eligible_questions"] == 0
        assert not any(q.get("status") == "pending" for q in ed["recheck_questions"])

    def test_planner_restricted_whitelist_gates(self):
        stub = _StubStructured([DisagreementPlanModel(
            disagreements=[DisagreementItem(topic="t", decision_impact="i", conflict_kind="fact")],
            recheck_questions=[QuestionPlan(question="q", disagreement_index=0,
                                            decision_impact="d",
                                            tool_name="get_global_news",
                                            tool_args={"curr_date": "2024-11-05"})])])
        ed = create_disagreement_planner_node(stub, allowed_tools=["get_news"])(
            _base_state(_bundle_with()))["evidence_debate"]
        assert ed["eligible_questions"] == 0
        assert ed["recheck_questions"][0]["status"] == "unresolved(tool_not_allowed)"

    def test_unsupported_cap_refuses_before_request(self):
        invokes = []

        class NoCap:
            def with_structured_output(self, schema, **kwargs):
                if kwargs:
                    raise TypeError("cap unsupported")
                return self

            def invoke(self, prompt, **kwargs):
                invokes.append(kwargs)
                return InitialViewModel(direction="long")

        view = create_initial_view_node("bull", NoCap())(
            _base_state(_bundle_with()))["initial_view_bull"]
        assert invokes == []  # 无界请求被拒绝
        assert view["claims"] == []
        assert any("上限" in lim for lim in view["limitations"])
        assert view["usage"]["cap_enforceable"] is False
        assert view["usage"]["actual_requests"] == 0

    def test_empty_structured_response_limited_record(self):
        import httpx
        from tradingagents.llm_clients.openai_client import NormalizedChatOpenAI

        def send(req):
            return httpx.Response(200, json={
                "id": "o", "object": "chat.completion", "created": 0, "model": "gpt-4o",
                "choices": [{"index": 0, "message": {"role": "assistant",
                             "content": "no structured answer"},
                             "finish_reason": "stop"}]})

        llm = NormalizedChatOpenAI(model="gpt-4o", api_key="k",
                                   base_url="https://off.invalid/v1", max_retries=1,
                                   max_tokens=9000,
                                   http_client=httpx.Client(transport=httpx.MockTransport(send)))
        view = create_initial_view_node("bull", llm)(_base_state(_bundle_with()))["initial_view_bull"]
        assert not view["claims"]
        assert view["limitations"]

    def test_views_bound_to_run(self):
        stub = _StubStructured([_view_model("B", [])])
        view = create_initial_view_node("bull", stub)(_base_state(_bundle_with()))["initial_view_bull"]
        assert view["run_id"] == "run-e"

    def test_cross_run_e_state_rejected_on_real_prepare(self, tmp_path):
        # 真实 SqliteSaver + 真实 _prepare_graph_run：异 run 的 E 状态拒恢复
        import types as _t
        from unittest.mock import patch as _patch
        from tradingagents.graph.checkpointer import get_checkpointer, thread_id
        from tradingagents.graph.trading_graph import TradingAgentsGraph
        import tests.test_financial_panel_integration as fpi

        wf = _full_e_graph(_StubStructured([_view_model("B", [])]),
                           _StubStructured([_view_model("R", [])]),
                           _StubStructured([DisagreementPlanModel(
                               disagreements=[DisagreementItem(topic="t", decision_impact="i",
                                                               conflict_kind="fact")],
                               recheck_questions=[])]))
        tid = thread_id("600519", "2024-11-05")
        cfg = {"configurable": {"thread_id": tid}}
        state = _base_state(_bundle_with())
        state["evidence_debate"] = {"schema_version": 1, "run_id": "OTHER-RUN",
                                    "disagreements": [], "recheck_questions": [],
                                    "usage": {}}
        with get_checkpointer(str(tmp_path), "600519") as saver:
            wf.compile(checkpointer=saver, interrupt_before=["planner"]).invoke(state, cfg)
        fg = fpi._FakeGraphBase({"checkpoint_enabled": True, "data_cache_dir": str(tmp_path),
                                 "evidence_debate_enabled": True})
        fg.workflow = wf
        fg.memory_log.get_past_context.return_value = None
        with _patch("tradingagents.graph.trading_graph.checkpoint_step", return_value=2):
            with pytest.raises(RuntimeError, match="异 run"):
                fg._prepare_graph_run("600519", "2024-11-05")
        TradingAgentsGraph.close_graph_run(fg)

    def test_claim_text_hard_limit(self):
        huge = "x" * 1_000_000
        out = validate_view(InitialViewModel(direction="long", top_claims=[
            ViewClaim(claim=huge)]), _base_state(), "bull")
        assert len(out["claims"][0]["claim"]) < len(huge)
        assert ("truncated" in json.dumps(out, ensure_ascii=False)
                or "截断" in json.dumps(out, ensure_ascii=False))


    def test_request_count_independent_of_tokens(self):
        view = {"output_mode": "structured",
                "usage": {"actual_requests": 1, "known_tokens": "unknown"}}
        counter = __import__("tradingagents.agents.debate_evidence", fromlist=["x"])._CountingLLM(object())
        counter.invoke_count = 1
        counter.tokens_known = False
        usage = __import__("tradingagents.agents.debate_evidence",
                           fromlist=["x"]).aggregate_usage(view, view, planner_counter=counter)
        assert usage["actual_request_count"] == 3
        assert usage["known_tokens"] == "unknown"
        assert usage["http_request_count"] == "unknown"  # 诚实命名：传输层未观测

    def test_summary_includes_claims_impact_evidence(self):
        bundle = _bundle_with()
        rid = bundle["records"][0]["evidence_id"]
        state = _base_state(bundle)
        state["initial_view_bull"] = validate_view(
            InitialViewModel(direction="long", top_claims=[
                ViewClaim(claim="现金流改善论断", evidence_ids=[rid])]), state, "bull")
        state["initial_view_bear"] = validate_view(
            InitialViewModel(direction="short", top_claims=[
                ViewClaim(claim="需求走弱论断", evidence_ids=[])]), state, "bear")
        plan = DisagreementPlanModel(
            disagreements=[DisagreementItem(topic="现金流方向", bull_claim="b", bear_claim="r",
                                            conflict_kind="fact",
                                            decision_impact="决定评级方向")],
            recheck_questions=[QuestionPlan(
                question="最新现金流报道", disagreement_index=0, decision_impact="d",
                tool_name="get_news",
                tool_args={"ticker": "600519", "start_date": "2024-10-01",
                           "end_date": "2024-11-05"})])
        state["evidence_debate"] = validate_plan(plan)
        state["evidence_debate"]["recheck_questions"][0]["recheck"] = {
            "status": "retrieved", "note": "新增证据 1 条",
            "tool_response_excerpt": "E-In-window e body"}
        summary = evidence_debate_summary_for_prompt(state)
        assert "现金流改善论断" in summary and "需求走弱论断" in summary
        assert "决定评级方向" in summary
        assert "E-In-window" in summary


class TestER3Restoration:
    """Codex E R3：真实生产 GraphSetup + SQLite 的阶段切换与损坏结构。"""

    @staticmethod
    def _workflow(enabled):
        from tradingagents.graph.setup import GraphSetup
        return GraphSetup(object(), object(),
                          {"news": ToolNode([graph_tools.get_news])},
                          ConditionalLogic(), evidence_debate_enabled=enabled
                          ).setup_graph(["news"])

    @staticmethod
    def _fake(config, tmp_path):
        import tests.test_financial_panel_integration as fpi
        fg = fpi._FakeGraphBase(dict(config, data_cache_dir=str(tmp_path),
                                     checkpoint_enabled=True))
        fg.memory_log.get_past_context.return_value = None
        return fg

    def test_e_off_checkpoint_refuses_e_on_resume_real_graph(self, tmp_path):
        from tradingagents.graph.trading_graph import TradingAgentsGraph
        cfg = {"data_cache_dir": str(tmp_path), "evidence_debate_enabled": False}
        old = self._fake(cfg, tmp_path)
        old.workflow = self._workflow(False)
        initial, args, _step = old._prepare_graph_run("600519", "2024-11-05")
        # 断点锚点随 fresh 初态持久化
        assert initial["run_metadata"]["evidence_debate"] == {
            "enabled": False, "schema_version": 1}
        old.graph.update_state(args["config"], initial, as_node="Quality Gate")
        assert old.graph.get_state(args["config"]).next == ("Bull Researcher",)
        TradingAgentsGraph.close_graph_run(old)
        new = self._fake(dict(cfg, evidence_debate_enabled=True), tmp_path)
        new.workflow = self._workflow(True)
        with pytest.raises(RuntimeError, match="锚点不符"):
            new._prepare_graph_run("600519", "2024-11-05")
        TradingAgentsGraph.close_graph_run(new)

    def test_e_on_checkpoint_refuses_e_off_resume_real_graph(self, tmp_path):
        from tradingagents.graph.trading_graph import TradingAgentsGraph
        cfg = {"data_cache_dir": str(tmp_path), "evidence_debate_enabled": True}
        old = self._fake(cfg, tmp_path)
        old.workflow = self._workflow(True)
        initial, args, _step = old._prepare_graph_run("600519", "2024-11-05")
        assert initial["run_metadata"]["evidence_debate"]["enabled"] is True
        old.graph.update_state(args["config"], initial, as_node="Quality Gate")
        TradingAgentsGraph.close_graph_run(old)
        new = self._fake(dict(cfg, evidence_debate_enabled=False), tmp_path)
        new.workflow = self._workflow(False)
        with pytest.raises(RuntimeError, match="锚点不符"):
            new._prepare_graph_run("600519", "2024-11-05")
        TradingAgentsGraph.close_graph_run(new)

    def test_same_mode_resume_accepted_real_graph(self, tmp_path):
        from tradingagents.graph.trading_graph import TradingAgentsGraph
        cfg = {"data_cache_dir": str(tmp_path), "evidence_debate_enabled": False}
        old = self._fake(cfg, tmp_path)
        old.workflow = self._workflow(False)
        initial, args, _step = old._prepare_graph_run("600519", "2024-11-05")
        old.graph.update_state(args["config"], initial, as_node="Quality Gate")
        TradingAgentsGraph.close_graph_run(old)
        same = self._fake(cfg, tmp_path)
        same.workflow = self._workflow(False)
        init_state, args2, step = same._prepare_graph_run("600519", "2024-11-05")
        assert init_state is None and step is not None  # 同模式恢复正常接受
        TradingAgentsGraph.close_graph_run(same)

    def test_corrupt_shapes_and_versions_refused(self):
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        def _check(values):
            g = object.__new__(TradingAgentsGraph)
            g.config = {"evidence_debate_enabled": True}
            TradingAgentsGraph._validate_resumed_evidence_debate(g, values)

        base_meta = {"run_id": "r1", "evidence_debate": {"enabled": True,
                                                         "schema_version": 1}}
        with pytest.raises(RuntimeError, match="结构损坏"):
            _check({"run_metadata": base_meta, "evidence_debate": ["broken"]})
        with pytest.raises(RuntimeError, match="结构损坏"):
            _check({"run_metadata": base_meta, "initial_view_bull": "broken"})
        with pytest.raises(RuntimeError, match="schema_version 未知"):
            _check({"run_metadata": base_meta,
                    "evidence_debate": {"schema_version": 999, "run_id": "r1"}})
        with pytest.raises(RuntimeError, match="缺少可信 run_id"):
            _check({"run_metadata": {"evidence_debate": {"enabled": True,
                                                         "schema_version": 1}},
                    "evidence_debate": {"schema_version": 1, "run_id": "r1"}})
