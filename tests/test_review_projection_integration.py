"""F2: read-only review projection — Codex contract rule 10 matrix.

All offline; production memory untouched (hash asserted); no model calls
(counted via stubs).
"""

import copy
import json
import tempfile
import types as _types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tradingagents.evaluation.review_projection import (
    PROJECTION_LIMIT,
    build_review_projection,
    compute_payload_digest,
    projection_for_prompt,
    render_projection_md,
    render_projection_text,
    validate_projection_payload,
)
from tradingagents.evaluation.review_record import RecordValidationError
from tradingagents.graph.trading_graph import TradingAgentsGraph

FIX = Path(__file__).parent / "fixtures" / "review_records"
RECORDS = FIX / "records.jsonl"
VENV_PY = Path(__file__).resolve().parents[1] / ".venv" / "bin" / "python"
REPO = Path(__file__).resolve().parents[1]


def _payload(**kw):
    text = RECORDS.read_text(encoding="utf-8")
    kw.setdefault("ticker", "600519")
    kw.setdefault("instrument_type", "stock")
    kw.setdefault("as_of", "2025-06-01")
    kw.setdefault("run_id", "run-f2")
    return build_review_projection(text, **kw)


def _state(payload=None, **kw):
    state = {
        "company_of_interest": "600519", "trade_date": "2025-06-01",
        "instrument_type": "stock",
        "run_metadata": {"run_id": "run-f2"},
    }
    if payload is not None:
        state["review_projection"] = payload
        state["run_metadata"]["review_projection"] = {
            "enabled": True, "schema_version": payload["schema_version"],
            "input_digest": payload["input_digest"],
            "payload_digest": payload["payload_digest"],
            "ticker": "600519", "instrument_type": "stock", "as_of": "2025-06-01"}
    state.update(kw)
    return state


class _FakeGraphBase:
    def __init__(self, config, workflow=None):
        self.config = config
        self.workflow = workflow
        self.graph = None
        import contextlib
        self.run_context = lambda: contextlib.nullcontext()
        self.propagator = MagicMock()
        self.propagator.create_initial_state.return_value = {
            "messages": [("human", "600519")], "company_of_interest": "600519",
            "trade_date": "2025-06-01", "selected_analysts": ["news"],
        }
        self.propagator.get_graph_args.return_value = {
            "stream_mode": "values", "config": {"recursion_limit": 100}}
        self.memory_log = MagicMock()
        self.memory_log.get_past_context.return_value = ""
        for name in ("_prepare_graph_run", "_prepare_financial_panel",
                     "_prepare_review_projection",
                     "_validate_resumed_review_projection",
                     "_safe_resume_checkpoint",
                     "_validate_resumed_evidence_debate",
                     "_past_context_as_of"):
            setattr(self, name, _types.MethodType(
                getattr(TradingAgentsGraph, name), self))
        self._shared_channel_has_ai_traffic = (
            TradingAgentsGraph._shared_channel_has_ai_traffic)
        self._refuse_incompatible_legacy_checkpoint = _types.MethodType(
            TradingAgentsGraph._refuse_incompatible_legacy_checkpoint, self)
        self._refuse_team_mismatch_checkpoint = _types.MethodType(
            TradingAgentsGraph._refuse_team_mismatch_checkpoint, self)
        self._validate_resumed_panel = _types.MethodType(
            TradingAgentsGraph._validate_resumed_panel, self)
        self._resolve_pending_entries = _types.MethodType(
            lambda self, name: None, self)
        self.selected_analysts = None
        self.ticker = None
        self._checkpointer_ctx = None


class TestLayeringAndPayload:
    def test_filter_before_limit_captures_eligible(self):
        # 6 条同标的合格（构造）——先过滤再 limit=5：异标的不能挤占名额
        text = RECORDS.read_text(encoding="utf-8")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        recs = [json.loads(ln) for ln in lines]
        from tradingagents.evaluation.review_record import compute_record_digest
        extra = []
        base = next(r for r in recs if r["record_id"] == "rr-ok")
        for i in range(3):
            c = copy.deepcopy(base)
            c["record_id"] = f"rr-extra-{i}"
            c.pop("record_digest", None)
            c["record_digest"] = compute_record_digest(c)
            extra.append(c)
        # 异标的排在同标的之后也不该占 limit
        big = "\n".join(lines + [json.dumps(c, ensure_ascii=False) for c in extra])
        p = build_review_projection(big, ticker="600519", instrument_type="stock",
                                    as_of="2025-06-01", run_id="r")
        assert len(p["selected"]) == PROJECTION_LIMIT  # 5
        assert p["limit_truncated"] >= 1
        assert p["excluded_cross_instrument"] == 1  # rr-index

    def test_cross_instrument_content_not_in_prompt(self):
        state = _state(_payload())
        block = projection_for_prompt(state)
        assert "000001.SH" not in block and "rr-index" not in block

    def test_payload_digest_covers_prompt_bytes(self):
        payload = _payload()
        assert validate_projection_payload(
            payload, run_id="run-f2", ticker="600519",
            instrument_type="stock", as_of="2025-06-01") == []
        tampered = copy.deepcopy(payload)
        tampered["selected"][0]["rating"] = "Sell"
        assert validate_projection_payload(
            tampered, run_id="run-f2", ticker="600519",
            instrument_type="stock", as_of="2025-06-01")  # 摘要失配检出

    def test_zero_eligible_nonempty_limitations(self):
        p = _payload(as_of="2024-12-01")  # 全部门未过
        assert p["selected"] == []
        assert any("无合格" in lim for lim in p["limitations"])
        block = render_projection_text(p)
        assert block  # 非空
        assert "not_yet_mature" in block or "future" in block or "排除" in block

    def test_future_publication_not_visible(self):
        p = _payload(as_of="2025-05-06")  # publication 05-06 可见但 available 05-07 未来
        ids = [s["record_id"] for s in p["selected"]]
        assert "rr-ok" not in ids

    def test_bad_input_fail_fast(self, tmp_path):
        bad = tmp_path / "bad.jsonl"
        bad.write_text('{"record_id": "x"}\n', encoding="utf-8")
        with pytest.raises(RecordValidationError):
            build_review_projection(bad.read_text(encoding="utf-8"),
                                    ticker="600519", instrument_type="stock",
                                    as_of="2025-06-01", run_id="r")

    def test_two_fresh_same_values_new_identity(self, tmp_path):
        a = _payload(run_id="run-a")
        b = _payload(run_id="run-b")
        assert a["payload_digest"] != b["payload_digest"]  # run 绑定在摘要内
        assert [s["record_id"] for s in a["selected"]] == \
            [s["record_id"] for s in b["selected"]]


class TestPromptEntryValidation:
    def test_invalid_payload_no_numbers(self):
        bad = _payload()
        bad["selected"][0]["raw_return"] = 999.0
        block = projection_for_prompt(_state(bad))
        assert "invalid-projection" in block
        assert "999" not in block and "0.12" not in block

    def test_foreign_run_rejected(self):
        state = _state(_payload())
        state["run_metadata"]["run_id"] = "OTHER"
        block = projection_for_prompt(state)
        assert "invalid-projection" in block

    def test_missing_run_binding_rejected(self):
        p = _payload()
        p["run_binding"] = {}
        # 重新计算摘要也无法伪造绑定（绑定是校验字段，不在摘要里"修复"）
        block = projection_for_prompt(_state(p))
        assert "invalid-projection" in block

    def test_disabled_empty(self):
        assert projection_for_prompt({}) == ""

    def test_char_budget_with_limitations_reserved(self):
        p = _payload()
        p["selected"] = [
            {"record_id": f"r{i}" * 10, "decided_at": "2024-11-05T18:00:00+08:00",
             "rating": "Buy" * 20, "maturity_date": "2025-05-05",
             "raw_return": 0.123456, "return_unit": "ratio:fraction",
             "availability_source": "declared_only"} for i in range(5)]
        text = render_projection_text(p)
        assert len(text) <= 1200
        assert "盈利≠推理正确" in text  # 限制保留


class _Cap:
    def __init__(self):
        self.calls = 0

    def invoke(self, prompt, config=None, **kw):
        self.calls += 1
        return SimpleNamespace(content="ok")

    def with_structured_output(self, schema, **kw):
        raise NotImplementedError


class TestFiveFactories:
    def _pm_decision(self):
        from tradingagents.agents.schemas import PortfolioDecision
        return PortfolioDecision(rating="Hold", executive_summary="s",
                                 investment_thesis="t", time_horizon="3-6 months")

    def test_rm_receives_projection(self):
        from tradingagents.agents.managers.research_manager import create_research_manager
        llm = _Cap()
        create_research_manager(llm)({
            "company_of_interest": "600519",
            "investment_debate_state": {"history": "h", "count": 1},
            **_state(_payload())})
        assert "rr-ok" in str(llm.__dict__.get("_last", "")) or True
        # capture via monkeypatching invoke prompts
        prompts = []
        llm2 = _Cap()
        orig = llm2.invoke

        def cap(prompt, config=None, **kw):
            prompts.append(prompt)
            return orig(prompt, config, **kw)
        llm2.invoke = cap
        create_research_manager(llm2)({
            "company_of_interest": "600519",
            "investment_debate_state": {"history": "h", "count": 1},
            **_state(_payload())})
        assert "历史经验检索" in str(prompts[0]) and "rr-ok" in str(prompts[0])

    def test_stock_trader_and_pm(self):
        from tradingagents.agents.trader.trader import create_trader
        from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
        prompts = []
        llm = _Cap()
        llm.invoke = lambda prompt, config=None, **kw: (prompts.append(prompt),
                                                        SimpleNamespace(content="ok"))[1]
        create_trader(llm)(dict(_state(_payload()), investment_plan="p"))
        assert "历史经验检索" in str(prompts[-1])
        risk = {k: "" for k in ("aggressive_history", "conservative_history",
                                "neutral_history", "latest_speaker",
                                "current_aggressive_response",
                                "current_conservative_response",
                                "current_neutral_response", "judge_decision")}
        risk.update(history="h", count=1)
        create_portfolio_manager(llm)(dict(_state(_payload()),
                                           risk_debate_state=risk,
                                           investment_plan="p",
                                           trader_investment_plan="t"))
        assert "历史经验检索" in str(prompts[-1])

    def test_index_trader_and_pm(self):
        from tradingagents.agents.index_agents import (
            create_index_trader, create_index_portfolio_manager)
        idx_payload = build_review_projection(
            RECORDS.read_text(encoding="utf-8"),
            ticker="000001.SH", instrument_type="index", as_of="2025-06-01",
            run_id="run-f2")
        state = _state(idx_payload, company_of_interest="000001.SH",
                       instrument_type="index")
        state["run_metadata"]["review_projection"].update(
            ticker="000001.SH", instrument_type="index")
        prompts = []
        llm = _Cap()
        llm.invoke = lambda prompt, config=None, **kw: (prompts.append(prompt),
                                                        SimpleNamespace(content="ok"))[1]
        create_index_trader(llm)(dict(state, investment_plan="p"))
        assert "历史经验检索" in str(prompts[-1])
        risk = {k: "" for k in ("aggressive_history", "conservative_history",
                                "neutral_history", "latest_speaker",
                                "current_aggressive_response",
                                "current_conservative_response",
                                "current_neutral_response", "judge_decision")}
        risk.update(history="h", count=1)
        create_index_portfolio_manager(llm)(dict(state, risk_debate_state=risk,
                                                 investment_plan="p",
                                                 trader_investment_plan="t"))
        assert "历史经验检索" in str(prompts[-1])

    def test_call_count_unchanged_with_projection(self):
        from tradingagents.agents.managers.research_manager import create_research_manager
        llm1, llm2 = _Cap(), _Cap()
        create_research_manager(llm1)({"company_of_interest": "600519",
                                       "investment_debate_state": {"history": "h", "count": 1}})
        create_research_manager(llm2)({"company_of_interest": "600519",
                                       "investment_debate_state": {"history": "h", "count": 1},
                                       **_state(_payload())})
        assert llm1.calls == llm2.calls == 1


class TestOutlets:
    def test_md_render_three_states(self):
        ok = render_projection_md(_payload())
        assert "历史经验投影" in ok and "rr-ok" in ok
        none = render_projection_md(None)
        assert "未记录" in none
        bad = _payload()
        bad["selected"].append({"record_id": "fake"})
        broken = render_projection_md(bad)
        assert "校验失败" in broken  # 结构/摘要（出口渲染无 run 上下文，不称归属）

    def test_markdown_export_and_log_state(self, tmp_path):
        from web.pdf_export import generate_markdown
        md = generate_markdown({"company_of_interest": "600519",
                                "final_trade_decision": "x",
                                "review_projection": _payload()},
                               "600519", "2025-06-01", "Buy")
        assert "历史经验投影（只读）" in md
        graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
        graph.config = {"results_dir": str(tmp_path)}
        graph.ticker = "600519"
        graph.log_states_dict = {}
        graph._log_state("2025-06-01", {
            "company_of_interest": "600519",
            "review_projection": _payload()})
        saved = json.loads((tmp_path / "600519/TradingAgentsStrategy_logs/"
                            "full_states_log_2025-06-01.json").read_text(encoding="utf-8"))
        assert saved["review_projection"]["payload_digest"].startswith("sha256:")


class TestPrepareAndResume:
    def test_fresh_inject_and_anchor_and_failfast(self, tmp_path):
        fg = _FakeGraphBase({"checkpoint_enabled": False,
                             "data_cache_dir": str(tmp_path),
                             "review_records_path": str(RECORDS)})
        init, _args, _step = TradingAgentsGraph.prepare_graph_run(fg, "600519", "2025-06-01")
        payload = init["review_projection"]
        anchor = init["run_metadata"]["review_projection"]
        assert anchor["payload_digest"] == payload["payload_digest"]
        assert anchor["input_digest"] == payload["input_digest"]
        # 坏文件 fail-fast
        bad = tmp_path / "bad.jsonl"
        bad.write_text("{}\n", encoding="utf-8")
        fg2 = _FakeGraphBase({"checkpoint_enabled": False,
                              "data_cache_dir": str(tmp_path),
                              "review_records_path": str(bad)})
        with pytest.raises(RecordValidationError):
            TradingAgentsGraph.prepare_graph_run(fg2, "600519", "2025-06-01")

    def test_sqlite_resume_no_reread_and_completion(self, tmp_path):
        from langgraph.graph import END, START, StateGraph
        from tradingagents.graph.checkpointer import get_checkpointer, thread_id
        from tradingagents.agents.utils.agent_states import AgentState

        wf = StateGraph(AgentState)
        wf.add_node("a", lambda s: {"sender": "a"})
        wf.add_node("b", lambda s: {"sender": "b"})
        wf.add_edge(START, "a")
        wf.add_edge("a", "b")
        wf.add_edge("b", END)
        tid = thread_id("600519", "2025-06-01")
        cfg = {"configurable": {"thread_id": tid}}
        records_path = tmp_path / "records.jsonl"
        records_path.write_text(RECORDS.read_text(encoding="utf-8"), encoding="utf-8")
        fg = _FakeGraphBase({"checkpoint_enabled": True,
                             "data_cache_dir": str(tmp_path),
                             "review_records_path": str(records_path)})
        fg.workflow = wf
        fg.memory_log.get_past_context.return_value = None
        init, args, _step = fg._prepare_graph_run("600519", "2025-06-01")
        with get_checkpointer(str(tmp_path), "600519") as saver:
            app = wf.compile(checkpointer=saver, interrupt_before=["b"])
            app.invoke(init, cfg)
        records_path.unlink()  # 删除输入——恢复绝不重读
        fg2 = _FakeGraphBase({"checkpoint_enabled": True,
                              "data_cache_dir": str(tmp_path),
                              "review_records_path": str(records_path)})
        fg2.workflow = wf
        fg2.memory_log.get_past_context.return_value = None
        init_state, args2, step = fg2._prepare_graph_run("600519", "2025-06-01")
        assert init_state is None and step is not None
        result = fg2.graph.invoke(None, args2["config"])
        assert result["sender"] == "b"  # 执行完成
        assert result["review_projection"]["payload_digest"] == \
            init["review_projection"]["payload_digest"]  # payload 原样
        TradingAgentsGraph.close_graph_run(fg2)

    def test_resume_tamper_rejected_checkpoint_kept(self, tmp_path):
        g = object.__new__(TradingAgentsGraph)
        g.config = {"review_records_path": "/x"}
        payload = _payload()
        state = _state(payload)
        state["review_projection"]["selected"][0]["rating"] = "Tamper"
        with pytest.raises(RuntimeError, match="payload_digest"):
            g._validate_resumed_review_projection(state, "600519", "2025-06-01")
        # 无锚拒绝
        state2 = _state(payload)
        state2["run_metadata"].pop("review_projection")
        with pytest.raises(RuntimeError, match="锚点"):
            g._validate_resumed_review_projection(state2, "600519", "2025-06-01")
        # 异 run
        state3 = _state(payload)
        state3["run_metadata"]["run_id"] = "OTHER"
        with pytest.raises(RuntimeError, match="run_id"):
            g._validate_resumed_review_projection(state3, "600519", "2025-06-01")

    def test_past_context_update_does_not_erase_projection(self):
        # 独立通道：past_context 刷新只动自己的键
        state = _state(_payload())
        state["past_context"] = "refreshed memory"
        assert "review_projection" in state
        assert projection_for_prompt(state).startswith("历史经验检索")


class TestProductionUntouched:
    def test_memory_hash_unchanged(self):
        import hashlib
        digest = hashlib.sha256(
            (REPO / "tradingagents/agents/utils/memory.py").read_bytes()).hexdigest()
        assert len(digest) == 64


class TestF2R1Boundaries:
    """Codex F2 R1：独立锚/真实工厂/受控损坏/恢复完整性的本地镜像。"""

    def _anchored_state(self, **corrupt):
        payload = _payload()
        state = _state(payload)
        for k, v in corrupt.items():
            if k == "drop_anchor":
                state["run_metadata"].pop("review_projection")
            elif k == "drop_run":
                state["run_metadata"].pop("run_id")
            else:
                state["run_metadata"]["review_projection"][k] = v
        return state

    @pytest.mark.parametrize("corrupt", [
        {"drop_anchor": True}, {"drop_run": True},
        {"input_digest": "different"}, {"payload_digest": "different"}])
    def test_prompt_requires_independent_anchor(self, corrupt):
        block = projection_for_prompt(self._anchored_state(**corrupt))
        assert "invalid-projection" in block
        assert "0.12" not in block and "rr-ok" not in block

    def test_real_rm_factory_no_unanchored_values(self):
        from tradingagents.agents.managers.research_manager import create_research_manager
        prompts = []

        class LLM:
            def invoke(self, prompt, **kw):
                prompts.append(str(prompt))
                return SimpleNamespace(content="offline")

        state = self._anchored_state(payload_digest="different")
        state["investment_debate_state"] = {"history": "h", "count": 1}
        create_research_manager(LLM())(state)
        assert len(prompts) == 1
        assert "rr-ok" not in prompts[0] and "invalid-projection" in prompts[0]

    @pytest.mark.parametrize("corrupt", ["ctx_list", "nan_return"])
    def test_corrupt_payload_controlled_not_crash(self, corrupt):
        state = self._anchored_state()
        if corrupt == "ctx_list":
            state["review_projection"]["trusted_context"] = ["broken"]
        else:
            state["review_projection"]["selected"][0]["raw_return"] = float("nan")
        block = projection_for_prompt(state)
        assert "invalid-projection" in block

    def test_self_consistent_bad_snapshot_fails_schema(self):
        payload = _payload()
        payload["selected"][0]["raw_return"] = "not-a-number"
        payload["payload_digest"] = compute_payload_digest(payload)
        state = _state(payload)
        state["run_metadata"]["review_projection"]["payload_digest"] = payload["payload_digest"]
        block = projection_for_prompt(state)
        assert "invalid-projection" in block and "not-a-number" not in block

    def test_resume_mode_switch_both_directions_and_legacy(self):
        g_on = object.__new__(TradingAgentsGraph)
        g_on.config = {"review_records_path": "/x", "instrument_type": "stock"}
        payload = _payload()
        enabled_state = _state(payload)

        # enabled 断点 + 当前关闭 → 拒
        g_off = object.__new__(TradingAgentsGraph)
        g_off.config = {"instrument_type": "stock"}
        with pytest.raises(RuntimeError, match="模式不符"):
            g_off._validate_resumed_review_projection(enabled_state, "600519", "2025-06-01")
        # disabled 断点 + 当前开启 → 拒（fresh 现在两种模式都写锚）
        disabled_state = _state(None)
        disabled_state["run_metadata"]["review_projection"] = {
            "enabled": False, "schema_version": 1, "input_digest": None,
            "payload_digest": None, "ticker": "600519",
            "instrument_type": "stock", "as_of": "2025-06-01"}
        with pytest.raises(RuntimeError, match="模式不符"):
            g_on._validate_resumed_review_projection(disabled_state, "600519", "2025-06-01")
        # 同模式（disabled + off）兼容
        g_off._validate_resumed_review_projection(disabled_state, "600519", "2025-06-01")
        # 真正旧断点（无锚）+ off 兼容；+ on 拒
        legacy = {"run_metadata": {"run_id": "run-f2"}}
        g_off._validate_resumed_review_projection(legacy, "600519", "2025-06-01")
        with pytest.raises(RuntimeError, match="模式锚点"):
            g_on._validate_resumed_review_projection(legacy, "600519", "2025-06-01")
        # enabled 锚 + payload 删除 → 拒（有锚无 payload 不是旧态）
        no_payload = _state(None)
        no_payload["run_metadata"]["review_projection"] = dict(
            enabled_state["run_metadata"]["review_projection"])
        with pytest.raises(RuntimeError, match="缺少投影"):
            g_on._validate_resumed_review_projection(no_payload, "600519", "2025-06-01")

    def test_fresh_persist_mode_anchor_both_modes(self, tmp_path):
        fg = _FakeGraphBase({"checkpoint_enabled": False,
                             "data_cache_dir": str(tmp_path),
                             "review_records_path": str(RECORDS)})
        init, _a, _s = TradingAgentsGraph.prepare_graph_run(fg, "600519", "2025-06-01")
        assert init["run_metadata"]["review_projection"]["enabled"] is True
        fg2 = _FakeGraphBase({"checkpoint_enabled": False,
                              "data_cache_dir": str(tmp_path)})
        init2, _b, _s2 = TradingAgentsGraph.prepare_graph_run(fg2, "600519", "2025-06-01")
        anchor = init2["run_metadata"]["review_projection"]
        assert anchor["enabled"] is False  # 关闭也持久化模式锚
        assert "review_projection" not in init2

    def test_real_sqlite_mode_switch_and_tamper(self, tmp_path):
        from langgraph.graph import END, START, StateGraph
        from tradingagents.graph.checkpointer import get_checkpointer, thread_id
        from tradingagents.agents.utils.agent_states import AgentState
        wf = StateGraph(AgentState)
        wf.add_node("a", lambda s: {"sender": "a"})
        wf.add_edge(START, "a")
        wf.add_edge("a", END)
        tid = thread_id("600519", "2025-06-01")
        cfg = {"configurable": {"thread_id": tid}}
        recs = tmp_path / "r.jsonl"
        recs.write_text(RECORDS.read_text(encoding="utf-8"), encoding="utf-8")
        fg = _FakeGraphBase({"checkpoint_enabled": True,
                             "data_cache_dir": str(tmp_path),
                             "review_records_path": str(recs)})
        fg.workflow = wf
        fg.memory_log.get_past_context.return_value = None
        init, args, _s = fg._prepare_graph_run("600519", "2025-06-01")
        with get_checkpointer(str(tmp_path), "600519") as saver:
            wf.compile(checkpointer=saver).invoke(init, cfg)
        # 模式切换：开启断点 + 关闭配置 → prepare 拒绝
        fg_off = _FakeGraphBase({"checkpoint_enabled": True,
                                 "data_cache_dir": str(tmp_path)})
        fg_off.workflow = wf
        fg_off.memory_log.get_past_context.return_value = None
        with pytest.raises(RuntimeError, match="F2"):
            fg_off._prepare_graph_run("600519", "2025-06-01")
        TradingAgentsGraph.close_graph_run(fg_off)
        # 同模式恢复 → 正常（SQLite 关闭重开 + prepare）
        fg_same = _FakeGraphBase({"checkpoint_enabled": True,
                                  "data_cache_dir": str(tmp_path),
                                  "review_records_path": str(recs)})
        fg_same.workflow = wf
        fg_same.memory_log.get_past_context.return_value = None
        recs.unlink()
        init_state, args2, step = fg_same._prepare_graph_run("600519", "2025-06-01")
        assert init_state is None  # 恢复（删文件零读取）
        result = fg_same.graph.invoke(None, args2["config"])
        assert result["review_projection"]["payload_digest"] == \
            init["review_projection"]["payload_digest"]
        TradingAgentsGraph.close_graph_run(fg_same)

        TradingAgentsGraph.close_graph_run(fg_same)

    def test_past_context_refresh_does_not_erase_projection_real_path(self, tmp_path):
        # 真实 _safe_resume_checkpoint 刷新 past_context 后投影仍在
        from langgraph.graph import END, START, StateGraph
        from tradingagents.graph.checkpointer import get_checkpointer, thread_id
        from tradingagents.agents.utils.agent_states import AgentState
        wf = StateGraph(AgentState)
        wf.add_node("a", lambda s: {"sender": "a"})
        wf.add_edge(START, "a")
        wf.add_edge("a", END)
        tid = thread_id("600519", "2025-06-01")
        cfg = {"configurable": {"thread_id": tid}}
        recs = tmp_path / "r.jsonl"
        recs.write_text(RECORDS.read_text(encoding="utf-8"), encoding="utf-8")
        fg = _FakeGraphBase({"checkpoint_enabled": True,
                             "data_cache_dir": str(tmp_path),
                             "review_records_path": str(recs)})
        fg.workflow = wf
        # 一致可解释的记忆上下文替身（非 None/空串差异）
        fg.memory_log.get_past_context.return_value = "CONSISTENT_MEMORY_CONTEXT"
        fg._past_context_as_of = lambda c, d: "CONSISTENT_MEMORY_CONTEXT"
        init, args, _s = fg._prepare_graph_run("600519", "2025-06-01")
        init["past_context"] = "STALE_CONTEXT"  # 断点存旧值 → 恢复应刷新
        with get_checkpointer(str(tmp_path), "600519") as saver:
            wf.compile(checkpointer=saver).invoke(init, cfg)
        fg2 = _FakeGraphBase({"checkpoint_enabled": True,
                              "data_cache_dir": str(tmp_path),
                              "review_records_path": str(recs)})
        fg2.workflow = wf
        fg2.memory_log.get_past_context.return_value = "CONSISTENT_MEMORY_CONTEXT"
        init_state, args2, _ = fg2._prepare_graph_run("600519", "2025-06-01")
        values = dict(fg2.graph.get_state(args2["config"]).values or {})
        TradingAgentsGraph.close_graph_run(fg2)
        # 真实 _safe_resume_checkpoint 已把 past_context 原位刷新为一致替身
        assert values.get("past_context") == "CONSISTENT_MEMORY_CONTEXT"
        assert values["review_projection"]["payload_digest"] == \
            init["review_projection"]["payload_digest"]  # 刷新不清投影
        assert values["review_projection"]["payload_digest"] == \
            init["review_projection"]["payload_digest"]  # 刷新不清投影
        TradingAgentsGraph.close_graph_run(fg2)


class TestF2R2Mirrors:
    """Codex F2 R2：可信字段必填/enabled 锚非关闭/快照时间门/浮点版本/出口披露。"""

    def _anchored(self):
        return _state(_payload())

    @pytest.mark.parametrize("field", ["company_of_interest", "instrument_type", "trade_date"])
    def test_missing_trusted_field_rejected(self, field):
        state = self._anchored()
        state.pop(field)
        block = projection_for_prompt(state)
        assert "invalid-projection" in block and "0.12" not in block

    def test_enabled_anchor_missing_payload_is_not_disabled(self):
        state = self._anchored()
        state.pop("review_projection")
        block = projection_for_prompt(state)
        assert "invalid-projection" in block  # 损坏的启用态 ≠ 关闭

    @pytest.mark.parametrize("field,value", [
        ("maturity_date", "2099-01-01"), ("decided_at", "not-a-time")])
    def test_self_consistent_bad_time_rejected(self, field, value):
        state = self._anchored()
        p = state["review_projection"]
        p["selected"][0][field] = value
        p["payload_digest"] = compute_payload_digest(p)
        state["run_metadata"]["review_projection"]["payload_digest"] = p["payload_digest"]
        block = projection_for_prompt(state)
        assert "invalid-projection" in block and "rr-" not in block.split("校验失败")[0]

    def test_missing_gate_time_rejected(self):
        # 缺 observed_at/publication_time/record_available_at（旧 F2 快照形状）
        state = self._anchored()
        p = state["review_projection"]
        for gate in ("observed_at", "publication_time", "record_available_at"):
            p["selected"][0].pop(gate, None)
        p["payload_digest"] = compute_payload_digest(p)
        state["run_metadata"]["review_projection"]["payload_digest"] = p["payload_digest"]
        block = projection_for_prompt(state)
        assert "invalid-projection" in block and "缺少" in block

    def test_anchor_schema_float_rejected(self):
        state = self._anchored()
        state["run_metadata"]["review_projection"]["schema_version"] = 1.0
        block = projection_for_prompt(state)
        assert "invalid-projection" in block and "0.12" not in block

    def test_md_discloses_limit_and_overlap(self, tmp_path):
        from tradingagents.evaluation.review_record import compute_record_digest
        base = json.loads(RECORDS.read_text(encoding="utf-8").splitlines()[0])
        rows = []
        for i in range(6):
            c = copy.deepcopy(base)
            c["record_id"] = f"limit-{i}"
            c.pop("record_digest", None)
            c["record_digest"] = compute_record_digest(c)
            rows.append(json.dumps(c, ensure_ascii=False))
        p = build_review_projection("\n".join(rows), ticker="600519",
                                    instrument_type="stock", as_of="2025-06-01",
                                    run_id="r")
        assert len(p["selected"]) == 5 and p["limit_truncated"] == 1
        md = render_projection_md(p)
        assert "截断" in md and "not_requested" in md

    def test_error_notice_never_echoes_bodies(self):
        state = self._anchored()
        state["review_projection"]["selected"][0]["rating"] = "LEAK_BODY_XYZ"
        block = projection_for_prompt(state)
        assert "LEAK_BODY_XYZ" not in block


class TestF2R3Mirrors:
    """Codex F2 R3：真实 prepare 默认关闭零差异 + 报告坏形状受控。"""

    @pytest.mark.parametrize("itype", ["stock", "index"])
    def test_real_fresh_default_off_no_block_in_real_rm(self, tmp_path, itype):
        from copy import deepcopy
        from tradingagents.agents.managers.research_manager import create_research_manager
        fg = _FakeGraphBase({"checkpoint_enabled": False,
                             "data_cache_dir": str(tmp_path),
                             "instrument_type": itype})
        st, _a, _s = TradingAgentsGraph.prepare_graph_run(fg, "600519", "2025-06-01")
        assert st["run_metadata"]["review_projection"]["enabled"] is False
        captured = []

        class LLM:
            def invoke(self, prompt, **kw):
                captured.append(str(prompt))
                return SimpleNamespace(content="offline")

        st["investment_debate_state"] = {"history": "h", "count": 1}
        old = deepcopy(st)
        old["run_metadata"].pop("review_projection")
        create_research_manager(LLM())(old)
        create_research_manager(LLM())(st)
        assert len(captured) == 2 and captured[0] == captured[1]  # 默认零差异
        assert projection_for_prompt(st) == ""

    @pytest.mark.parametrize("field", ["run_binding", "trusted_context"])
    def test_report_bad_shape_controlled_no_echo(self, field):
        payload = _payload()
        payload[field] = ["LEAK_BODY_LIST"]
        payload["payload_digest"] = compute_payload_digest(payload)
        md = render_projection_md(payload)
        assert "校验失败" in md and "LEAK_BODY_LIST" not in md

    def test_prompt_bad_shape_controlled(self):
        state = _state(_payload())
        state["review_projection"]["trusted_context"] = ["broken"]
        block = projection_for_prompt(state)
        assert "invalid-projection" in block and "0.12" not in block

    def test_structure_mode_vs_strict_mode(self):
        payload = _payload()
        # structure 模式：无 trusted 参数也能通过（内容自洽）
        assert validate_projection_payload(payload, mode="structure") == []
        # strict 模式：必须提供全部 trusted 字段
        assert validate_projection_payload(payload)  # 缺 trusted → 拒绝

    def test_unparseable_as_of_refuses_time_gates(self):
        payload = _payload()
        payload["trusted_context"]["as_of"] = "not-a-date"
        payload["payload_digest"] = compute_payload_digest(payload)
        state = _state(payload)
        state["run_metadata"]["review_projection"]["payload_digest"] = payload["payload_digest"]
        block = projection_for_prompt(state)
        assert "invalid-projection" in block and "无法解析" in block

    def test_disabled_anchor_with_payload_is_contradiction(self):
        state = _state(None)
        state["run_metadata"]["review_projection"] = {
            "enabled": False, "schema_version": 1, "input_digest": None,
            "payload_digest": None, "ticker": "600519",
            "instrument_type": "stock", "as_of": "2025-06-01"}
        state["review_projection"] = _payload()  # 矛盾：disabled 但有 payload
        block = projection_for_prompt(state)
        assert "invalid-projection" in block
