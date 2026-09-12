"""D2a integration: preparation-layer injection, pure-read resume, prompt
projection through the REAL decision factories.

Contract: docs/D2_CODEX_IMPLEMENTATION_CONTRACT_2026-09-09.md (nine rules).
All offline; real checkpointer interrupt→resume (no mock-only proofs).
"""

import copy
import json
import tempfile
import types as _types
import unittest.mock as _mock
from pathlib import Path
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from tradingagents.agents.utils.agent_states import AgentState
from tradingagents.dataflows.financial_panel import (
    ManifestError as PanelManifestError,
    bind_run,
    compute_financial_panel,
    panel_context_for_prompt,
)
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.graph.checkpointer import get_checkpointer

FIX = Path(__file__).parent / "fixtures" / "financial_panel"


def _manifest(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def _card(name="stock_normal.json", run_id="run-test", instrument="600519",
          itype="stock", adate="2024-11-05"):
    return bind_run(compute_financial_panel(_manifest(name), instrument=instrument,
                                            instrument_type=itype, analysis_date=adate),
                    run_id)


class _FakeGraphBase:
    """Mirrors tests/test_checkpoint_resume.py's real-method binding harness."""

    def __init__(self, config, workflow=None, graph=None):
        self.config = config
        self.workflow = workflow
        self.graph = graph
        import contextlib
        self.run_context = lambda: contextlib.nullcontext()
        self.propagator = _mock.MagicMock()
        self.propagator.create_initial_state.return_value = {
            "messages": [("human", "600519")], "company_of_interest": "600519",
            "trade_date": "2024-11-05", "selected_analysts": ["news"],
        }
        self.propagator.get_graph_args.return_value = {
            "stream_mode": "values", "config": {"recursion_limit": 100}}
        self.memory_log = _mock.MagicMock()
        self.memory_log.get_past_context.return_value = ""
        self._prepare_graph_run = _types.MethodType(
            TradingAgentsGraph._prepare_graph_run, self)
        self._prepare_financial_panel = _types.MethodType(
            TradingAgentsGraph._prepare_financial_panel, self)
        self._validate_resumed_panel = _types.MethodType(
            TradingAgentsGraph._validate_resumed_panel, self)
        self._validate_resumed_evidence_debate = _types.MethodType(
            TradingAgentsGraph._validate_resumed_evidence_debate, self)
        self._prepare_review_projection = _types.MethodType(
            TradingAgentsGraph._prepare_review_projection, self)
        self._validate_resumed_review_projection = _types.MethodType(
            TradingAgentsGraph._validate_resumed_review_projection, self)
        self._safe_resume_checkpoint = _types.MethodType(
            TradingAgentsGraph._safe_resume_checkpoint, self)
        self._refuse_incompatible_legacy_checkpoint = _types.MethodType(
            TradingAgentsGraph._refuse_incompatible_legacy_checkpoint, self)
        self._refuse_team_mismatch_checkpoint = _types.MethodType(
            TradingAgentsGraph._refuse_team_mismatch_checkpoint, self)
        self._shared_channel_has_ai_traffic = (
            TradingAgentsGraph._shared_channel_has_ai_traffic)
        self._past_context_as_of = _types.MethodType(
            TradingAgentsGraph._past_context_as_of, self)
        self._resolve_pending_entries = _types.MethodType(
            lambda self, name: None, self)
        self.selected_analysts = None
        self.ticker = None
        self._checkpointer_ctx = None


class TestFreshPreparation:
    def _prepare(self, tmp, manifest_name=None, extra_config=None):
        cfg = {"checkpoint_enabled": False, "data_cache_dir": str(tmp)}
        if manifest_name:
            cfg["financial_panel_manifest"] = str(Path(tmp) / "m.json")
            Path(tmp, "m.json").write_text(
                json.dumps(_manifest(manifest_name), ensure_ascii=False),
                encoding="utf-8")
        cfg.update(extra_config or {})
        fg = _FakeGraphBase(cfg)
        return TradingAgentsGraph.prepare_graph_run(fg, "600519", "2024-11-05")

    def test_default_absent_no_card_stock_and_index(self, tmp_path):
        init, _args, step = self._prepare(tmp_path)
        assert step is None
        assert "financial_panel" not in init
        assert "financial_panel" not in init["run_metadata"]
        init_idx, _a, _s = self._prepare(tmp_path, extra_config={"instrument_type": "index"})
        assert "financial_panel" not in init_idx

    def test_fresh_with_manifest_binds_run(self, tmp_path):
        init, _args, _step = self._prepare(tmp_path, "stock_normal.json")
        card = init["financial_panel"]
        assert card["run_binding"]["run_id"] == init["run_metadata"]["run_id"]
        assert init["run_metadata"]["financial_panel"]["manifest_digest"] == card["manifest_digest"]
        assert "financial_panel_manifest" not in json.dumps(card)  # 路径不进卡
        assert panel_context_for_prompt(init).startswith("财务面板")

    def test_two_fresh_runs_same_values_new_identity(self, tmp_path):
        init1, _a, _ = self._prepare(tmp_path, "stock_normal.json")
        init2, _b, _ = self._prepare(tmp_path, "stock_normal.json")
        c1, c2 = init1["financial_panel"], init2["financial_panel"]
        assert c1["manifest_digest"] == c2["manifest_digest"]
        assert c1["metrics"] == c2["metrics"]
        assert c1["run_binding"]["run_id"] != c2["run_binding"]["run_id"]

    def test_explicit_index_manifest_not_applicable_no_dummy(self, tmp_path):
        m = _manifest("index.json")
        Path(tmp_path, "m.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        cfg = {"checkpoint_enabled": False, "data_cache_dir": str(tmp_path),
               "financial_panel_manifest": str(Path(tmp_path) / "m.json"),
               "instrument_type": "index"}
        fg = _FakeGraphBase(cfg)
        init, _args, _step = TradingAgentsGraph.prepare_graph_run(fg, "000001.SH", "2024-11-05")
        card = init["financial_panel"]
        assert card["overall_status"] == "not_applicable"
        assert card["metrics"] == {}  # 无 dummy 财务计算
        assert "不适用" in card["scenarios"]["reason"]

    def test_invalid_manifest_fails_fast_before_any_model(self, tmp_path):
        m = _manifest("stock_normal.json")
        m["inputs"][0]["value"] = None
        Path(tmp_path, "m.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        cfg = {"checkpoint_enabled": False, "data_cache_dir": str(tmp_path),
               "financial_panel_manifest": str(Path(tmp_path) / "m.json")}
        fg = _FakeGraphBase(cfg)
        with pytest.raises(PanelManifestError):
            TradingAgentsGraph.prepare_graph_run(fg, "600519", "2024-11-05")

    def test_instrument_mismatch_fails_fast(self, tmp_path):
        m = _manifest("stock_normal.json")
        m["instrument"] = "000002"
        Path(tmp_path, "m.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        cfg = {"checkpoint_enabled": False, "data_cache_dir": str(tmp_path),
               "financial_panel_manifest": str(Path(tmp_path) / "m.json")}
        fg = _FakeGraphBase(cfg)
        with pytest.raises(PanelManifestError, match="instrument"):
            TradingAgentsGraph.prepare_graph_run(fg, "600519", "2024-11-05")

    def test_file_limits(self, tmp_path):
        big = Path(tmp_path) / "m.json"
        big.write_text("x" * (2 * 1024 * 1024 + 10), encoding="utf-8")
        cfg = {"checkpoint_enabled": False, "data_cache_dir": str(tmp_path),
               "financial_panel_manifest": str(big)}
        fg = _FakeGraphBase(cfg)
        with pytest.raises(PanelManifestError, match="字节上限"):
            TradingAgentsGraph.prepare_graph_run(fg, "600519", "2024-11-05")
        bad = Path(tmp_path) / "m2.json"
        bad.write_bytes(b"\xff\xfe not utf8")
        cfg["financial_panel_manifest"] = str(bad)
        with pytest.raises(PanelManifestError, match="UTF-8"):
            TradingAgentsGraph.prepare_graph_run(_FakeGraphBase(cfg), "600519", "2024-11-05")


def _panel_state_graph():
    """Mini production-shaped graph on AgentState with an interrupt point."""
    def node_a(state):
        return {"sender": "a"}

    def node_b(state):
        return {"sender": "b"}

    wf = StateGraph(AgentState)
    wf.add_node("node_a", node_a)
    wf.add_node("node_b", node_b)
    wf.add_edge(START, "node_a")
    wf.add_edge("node_a", "node_b")
    wf.add_edge("node_b", END)
    return wf


class TestResumePureRead:
    """Real SqliteSaver interrupt→resume through the production prepare path."""

    def _pause(self, tmp_path, with_card, run_id="run-resume"):
        from tradingagents.graph.checkpointer import get_checkpointer, thread_id

        wf = _panel_state_graph()
        tid = thread_id("600519", "2024-11-05")
        cfg = {"configurable": {"thread_id": tid}}
        state = {
            "messages": [("human", "600519")],
            "company_of_interest": "600519",
            "trade_date": "2024-11-05",
            "selected_analysts": ["news"],
            "run_metadata": {"run_id": run_id},
        }
        if with_card is not None:
            state["financial_panel"] = with_card
            state["run_metadata"]["financial_panel"] = {
                "manifest_digest": with_card.get("manifest_digest", ""),
                "overall_status": with_card.get("overall_status", ""),
            }
        with get_checkpointer(str(tmp_path), "600519") as saver:
            app = wf.compile(checkpointer=saver, interrupt_before=["node_b"])
            app.invoke(state, cfg)
        return wf, cfg

    def _fake(self, tmp_path, wf, manifest_path=None):
        fg = _FakeGraphBase({"checkpoint_enabled": True,
                             "data_cache_dir": str(tmp_path)})
        if manifest_path:
            fg.config["financial_panel_manifest"] = str(manifest_path)
        fg.workflow = wf
        fg.memory_log.get_past_context.return_value = None
        return fg

    def test_resume_never_rereads_manifest(self, tmp_path):
        m_path = Path(tmp_path) / "m.json"
        m_path.write_text(json.dumps(_manifest("stock_normal.json"), ensure_ascii=False),
                          encoding="utf-8")
        card = _card(run_id="run-resume")
        wf, cfg = self._pause(tmp_path, card)
        with get_checkpointer(str(tmp_path), "600519") as saver:
            before = json.dumps(
                wf.compile(checkpointer=saver).get_state(cfg).values.get("financial_panel"),
                sort_keys=True, ensure_ascii=False)
        m_path.unlink()  # 恢复前删除 manifest：任何重读都会立刻暴露
        fg = self._fake(tmp_path, wf, manifest_path=m_path)
        init_state, args, step = fg._prepare_graph_run("600519", "2024-11-05")
        assert init_state is None and step == 1  # 恢复路径，不产 fresh 初态
        values = fg.graph.get_state(args["config"]).values
        after = json.dumps(values.get("financial_panel"),
                           sort_keys=True, ensure_ascii=False)
        assert after == before  # 卡逐字节不变
        assert fg.graph.get_state(args["config"]).next == ("node_b",)  # 原节点不重跑
        # 真正续跑：invoke(None) 完成 node_b，节点未重放，终态卡仍逐字节不变
        result = fg.graph.invoke(None, args["config"])
        assert result["sender"] == "b"
        final_card = json.dumps(result.get("financial_panel"),
                                sort_keys=True, ensure_ascii=False)
        assert final_card == before
        assert not fg.graph.get_state(args["config"]).next  # 运行完结
        TradingAgentsGraph.close_graph_run(fg)

    def test_old_cardless_checkpoint_not_backfilled(self, tmp_path):
        m_path = Path(tmp_path) / "m.json"
        m_path.write_text(json.dumps(_manifest("stock_normal.json"), ensure_ascii=False),
                          encoding="utf-8")
        wf, cfg = self._pause(tmp_path, None)  # 无卡断点
        fg = self._fake(tmp_path, wf, manifest_path=m_path)  # 恢复时才配 manifest
        init_state, args, step = fg._prepare_graph_run("600519", "2024-11-05")
        assert init_state is None
        assert "financial_panel" not in (fg.graph.get_state(args["config"]).values or {})
        TradingAgentsGraph.close_graph_run(fg)

    def test_corrupted_card_refused_and_checkpoint_kept(self, tmp_path):
        card = _card(run_id="run-OTHER")  # run_id 失配
        wf, cfg = self._pause(tmp_path, card)
        with get_checkpointer(str(tmp_path), "600519") as saver:
            values_before = dict(wf.compile(checkpointer=saver).get_state(cfg).values or {})
        fg = self._fake(tmp_path, wf)
        with pytest.raises(RuntimeError, match="run_id 不符"):
            fg._prepare_graph_run("600519", "2024-11-05")
        assert dict(fg.graph.get_state(cfg).values or {}) == values_before  # 断点保留
        TradingAgentsGraph.close_graph_run(fg)

    def test_digest_tamper_refused(self, tmp_path):
        card = _card(run_id="run-resume")
        card["inputs"][0]["normalized_value"] = "999"  # 内嵌表与 digest 不再自洽
        wf, cfg = self._pause(tmp_path, card)
        fg = self._fake(tmp_path, wf)
        with pytest.raises(RuntimeError, match="digest"):
            fg._prepare_graph_run("600519", "2024-11-05")
        TradingAgentsGraph.close_graph_run(fg)


class _CapLLM:
    """Structured-capable fake that counts EVERY invoke."""

    def __init__(self, decision):
        self.decision = decision
        self.calls = 0

        class _S:
            def __init__(s, d):
                s.d = d

            def invoke(s, prompt, config=None):
                self.calls += 1
                self.prompts = getattr(self, "prompts", [])
                self.prompts.append(prompt)
                return s.d

        self._s = _S(decision)
        self.prompts = []

    def with_structured_output(self, schema):
        return self._s

    def invoke(self, prompt, config=None):
        self.calls += 1
        self.prompts.append(prompt)
        return SimpleNamespace(content="free")


class TestPromptProjectionInRealFactories:
    def _state(self, panel=None):
        # 生产 fresh state 的可信身份：run_metadata 带 financial_panel 摘要锚定
        anchor = ({"manifest_digest": panel["manifest_digest"],
                   "overall_status": panel["overall_status"]}
                  if isinstance(panel, dict) else None)
        state = {
            "company_of_interest": "600519", "trade_date": "2024-11-05",
            "instrument_type": "stock",
            "run_metadata": ({"run_id": "run-p", "financial_panel": anchor}
                             if anchor else {"run_id": "run-p"}),
            "risk_debate_state": {"history": "h", "count": 1,
                                  "aggressive_history": "", "conservative_history": "",
                                  "neutral_history": "", "latest_speaker": "",
                                  "current_aggressive_response": "",
                                  "current_conservative_response": "",
                                  "current_neutral_response": "", "judge_decision": ""},
            "investment_debate_state": {"history": "d", "count": 1},
            "investment_plan": "plan", "trader_investment_plan": "tplan",
            "past_context": "",
        }
        if panel is not None:
            state["financial_panel"] = panel
        return state

    def _pm_decision(self):
        from tradingagents.agents.schemas import PortfolioDecision, ThesisCondition
        return PortfolioDecision(
            rating="Hold", executive_summary="s", investment_thesis="t",
            time_horizon="3-6 months",
            invalidation_conditions=[ThesisCondition(
                description="d", indicator="i", comparator="<", threshold="0",
                period="next quarter", source="季报")])

    def test_future_sentinel_never_reaches_pm_prompt(self):
        from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
        m = _manifest("stock_normal.json")
        # 给一个 future-excluded 的输入塞唯一 sentinel（标题/出处/值）
        fut = copy.deepcopy(m["inputs"][1])  # r23 变 future
        fut["input_id"] = "fut-sentinel"
        fut["disclosed_at"] = "2030-01-01"
        fut["value"] = 777777.0
        fut["provenance"] = {"offline_ref": "FUTURE_SENTINEL_PROV_XYZ"}
        m["inputs"][1] = fut
        card = bind_run(compute_financial_panel(m, instrument="600519",
                                                 instrument_type="stock",
                                                 analysis_date="2024-11-05"), "run-p")
        # 卡的审计输入表保留 sentinel（离线复核），提示词投影绝不包含
        assert "FUTURE_SENTINEL_PROV_XYZ" in json.dumps(card["inputs"])
        llm = _CapLLM(self._pm_decision())
        create_portfolio_manager(llm)(self._state(panel=card))
        prompt = str(llm.prompts[0])
        for banned in ("FUTURE_SENTINEL_PROV_XYZ", "fut-sentinel", "777777"):
            assert banned not in prompt, banned
        assert "25.00%" in prompt  # ok 数值在（margin；YoY 因 prior 被排除而未知）
        assert "照抄" in prompt

    def test_call_count_identical_with_and_without_panel(self):
        from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
        llm1 = _CapLLM(self._pm_decision())
        create_portfolio_manager(llm1)(self._state())
        llm2 = _CapLLM(self._pm_decision())
        create_portfolio_manager(llm2)(self._state(panel=_card(run_id="run-p")))
        assert llm1.calls == llm2.calls == 1

    def test_stock_rm_trader_and_index_factories_covered(self):
        from tradingagents.agents.managers.research_manager import create_research_manager
        from tradingagents.agents.trader.trader import create_trader
        from tradingagents.agents.index_agents import (
            create_index_trader, create_index_portfolio_manager)

        class _Free:
            def __init__(self):
                self.prompts = []

            def invoke(self, prompt, config=None):
                self.prompts.append(prompt)
                return SimpleNamespace(content="ok")

            def with_structured_output(self, schema):
                raise NotImplementedError

        card = _card(run_id="run-p")
        ident = {"company_of_interest": "600519", "trade_date": "2024-11-05",
                 "run_metadata": {"run_id": "run-p", "financial_panel": {
                     "manifest_digest": card["manifest_digest"],
                     "overall_status": card["overall_status"]}}}
        # stock RM
        rm_llm = _Free()
        create_research_manager(rm_llm)(dict(ident, investment_debate_state={"history": "h", "count": 1},
                                             financial_panel=card))
        assert "财务面板" in str(rm_llm.prompts[0]) and "20.00%" in str(rm_llm.prompts[0])
        # stock Trader
        tr_llm = _Free()
        create_trader(tr_llm)(dict(ident, investment_plan="p", financial_panel=card))
        assert "20.00%" in str(tr_llm.prompts[0])
        # index Trader / PM
        idx_card = bind_run(compute_financial_panel(
            _manifest("index.json"), instrument="000001.SH",
            instrument_type="index", analysis_date="2024-11-05"), "run-p")
        idx_ident = {"company_of_interest": "000001.SH", "trade_date": "2024-11-05",
                     "instrument_type": "index",
                     "run_metadata": {"run_id": "run-p", "financial_panel": {
                         "manifest_digest": idx_card["manifest_digest"],
                         "overall_status": idx_card["overall_status"]}}}
        itr_llm = _Free()
        create_index_trader(itr_llm)(dict(idx_ident, investment_plan="p",
                                          financial_panel=idx_card))
        # 指数运行只可能持有指数卡（not_applicable）——单行声明、无数字
        assert "财务面板" in str(itr_llm.prompts[0])
        assert "不适用" in str(itr_llm.prompts[0]) and "20.00%" not in str(itr_llm.prompts[0])
        ipm_llm = _CapLLM(self._pm_decision())
        idx_state = dict(self._state(panel=_card(run_id="run-p", instrument="000001.SH",
                                                 itype="index", name="index.json")),
                         company_of_interest="000001.SH", instrument_type="index")
        create_index_portfolio_manager(ipm_llm)(idx_state)
        assert "不适用" in str(ipm_llm.prompts[0]) and "20.00%" not in str(ipm_llm.prompts[0])

    def test_zero_ok_panel_keeps_limitations_not_empty(self):
        m = _manifest("stock_future_disclosure.json")
        card = bind_run(compute_financial_panel(m, instrument="600519",
                                                 instrument_type="stock",
                                                 analysis_date="2024-06-01"), "run-p")
        state = {"financial_panel": card, "company_of_interest": "600519",
                 "trade_date": "2024-06-01", "instrument_type": "stock",
                 "run_metadata": {"run_id": "run-p", "financial_panel": {
                     "manifest_digest": card["manifest_digest"],
                     "overall_status": card["overall_status"]}}}
        block = panel_context_for_prompt(state)
        assert block  # 非空
        assert "future_disclosure" in block
        assert "缺口" in block

    def test_invalid_card_direct_helper_hides_numbers(self):
        bad = _card(run_id="run-p")
        bad["schema_version"] = 99
        block = panel_context_for_prompt({"financial_panel": bad})
        assert "invalid-panel" in block
        assert "20.00%" not in block and "13,000,000,000" not in block

    def test_real_pm_factory_rejects_cross_run_card(self):
        from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
        state = self._state(panel=_card(run_id="run-p"))
        state["run_metadata"]["run_id"] = "run-OTHER"  # 卡与运行身份不符
        llm = _CapLLM(self._pm_decision())
        create_portfolio_manager(llm)(state)
        prompt = str(llm.prompts[0])
        assert "invalid-panel" in prompt
        assert "20.00%" not in prompt and "13,000,000,000" not in prompt
        assert llm.calls == 1  # 单次调用，无额外请求

    def test_missing_metadata_anchor_invalid_not_self_certified(self):
        state = self._state(panel=_card(run_id="run-p"))
        state["run_metadata"] = {"run_id": "run-p"}  # 有卡但锚定摘要缺失
        block = panel_context_for_prompt(state)
        assert "invalid-panel" in block and "20.00%" not in block

    def test_tampered_card_inputs_invalid_in_helper(self):
        state = self._state(panel=_card(run_id="run-p"))
        state["financial_panel"]["inputs"][0]["normalized_value"] = "654321"
        block = panel_context_for_prompt(state)
        assert "invalid-panel" in block and "20.00%" not in block

    def test_outlet_consistency(self):
        from web.pdf_export import generate_markdown
        card = _card(run_id="run-p")
        md = generate_markdown({"company_of_interest": "600519",
                                "final_trade_decision": "x",
                                "financial_panel": card}, "600519", "2024-11-05", "Buy")
        assert "财务面板（可复算）" in md and "20.00%" in md
        assert "未记录" in generate_markdown(
            {"company_of_interest": "600519", "final_trade_decision": "x"},
            "600519", "2024-11-05", "Buy")
