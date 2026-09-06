"""第 4 批回归（A09/A10/A11/A12/A14）：恢复类型、质量门控、恢复索引、
历史目录与报告字段契约。全部离线，持久化路径指向 tmp。"""

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage

from tradingagents.agents.quality_gate import (
    ANALYST_NAMES,
    create_quality_gate,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.propagation import Propagator
from web import history
from web.pdf_export import generate_markdown
from web.report_fields import quality_summary, trader_plan


# ===========================================================================
# A09：恢复类型来自任务请求
# ===========================================================================


def _build_config_fn(ns_extra: dict):
    """AST 执行 web/app.py 的 _build_config，返回可调用函数（不启动 UI）。"""
    path = Path("web/app.py")
    node = next(
        n for n in ast.parse(path.read_text(encoding="utf-8")).body
        if isinstance(n, ast.FunctionDef) and n.name == "_build_config"
    )
    ns = {
        "DEFAULT_CONFIG": DEFAULT_CONFIG,
        "os": __import__("os"),
        "st": SimpleNamespace(session_state={}),
    }
    ns.update(ns_extra)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), ns)
    return ns["_build_config"]


def _exec_build_config(ns_extra: dict) -> dict:
    """求值便捷形式：无参调用（回退到注入的 start_req/侧栏状态）。"""
    return _build_config_fn(ns_extra)()


class TestResumeTypeFromTask:
    def test_index_request_overrides_stock_sidebar(self):
        """侧栏=个股、任务请求=指数 → 实际运行指数图与指数分析师预设。"""
        cfg = _exec_build_config({
            "start_req": {
                "ticker": "000001.SH",
                "trade_date": "2026-01-15",
                "analysis_type": "指数",
            },
            "st": SimpleNamespace(session_state={"analysis_type": "个股"}),
        })
        assert cfg["instrument_type"] == "index"
        assert "fundamentals" not in cfg["selected_analysts"]
        assert "lockup" not in cfg["selected_analysts"]

    def test_stock_request_overrides_index_sidebar(self):
        cfg = _exec_build_config({
            "start_req": {
                "ticker": "600519",
                "trade_date": "2026-01-15",
                "analysis_type": "个股",
            },
            "st": SimpleNamespace(session_state={"analysis_type": "指数"}),
        })
        assert cfg["instrument_type"] == "stock"
        assert "fundamentals" in cfg["selected_analysts"]

    def test_explicit_request_beats_globals(self):
        """显式传入 run_request 时不受模块级 start_req 影响。"""
        fn = _build_config_fn({"start_req": {"analysis_type": "个股"}})
        cfg = fn({"analysis_type": "指数"})
        assert cfg["instrument_type"] == "index"

    def test_sidebar_used_when_no_request(self):
        cfg = _exec_build_config({
            "st": SimpleNamespace(session_state={"analysis_type": "指数"}),
        })
        assert cfg["instrument_type"] == "index"


# ===========================================================================
# A10：质量门控只检查启用集合
# ===========================================================================


def _llm():
    llm = Mock()
    llm.invoke.return_value = AIMessage(content="## 数据质量审核报告\n**整体评级**: A")
    return llm


def _state(analysts, reports=None, empty_roles=()):
    state = Propagator().create_initial_state(
        "600519", "2026-01-15", selected_analysts=analysts
    )
    state.update(reports or {})
    for role in empty_roles:
        state[f"{role}_report"] = ""
    return state


GOOD = "【合格报告】" + "详细分析内容" * 60 + "\n| 指标 | 结论 |\n|---|---|\n| 综合 | 通过 |"


class TestQualityGateActiveSet:
    def test_single_market_no_false_failures(self):
        """单 market：未启用角色不给 F，LLM 复审照常执行。"""
        llm = _llm()
        state = _state(["market"], {"market_report": GOOD})
        summary = create_quality_gate(llm)(state)["data_quality_summary"]
        assert "[F]" not in summary
        assert llm.invoke.call_count == 1
        # 提示词只含 1 位分析师（硬检查与 LLM 人数一致）
        prompt_text = llm.invoke.call_args.args[0]
        assert "1 位分析师" in prompt_text
        assert ANALYST_NAMES["market"] in prompt_text
        assert ANALYST_NAMES["news"] not in prompt_text

    def test_mixed_subset_checked(self):
        llm = _llm()
        state = _state(
            ["market", "news"],
            {"market_report": GOOD, "news_report": GOOD},
        )
        summary = create_quality_gate(llm)(state)["data_quality_summary"]
        assert "[F]" not in summary
        assert "参与分析师**: 2 位" in summary

    def test_index_five_analysts(self):
        llm = _llm()
        from tradingagents.agents.quality_gate import REPORT_FIELDS

        reports = {REPORT_FIELDS[r]: GOOD for r in
                   ["market", "social", "news", "policy", "hot_money"]}
        state = _state(["market", "social", "news", "policy", "hot_money"], reports)
        summary = create_quality_gate(llm)(state)["data_quality_summary"]
        assert "[F]" not in summary
        assert "5 位" in summary

    def test_enabled_but_empty_report_fails(self):
        """已启用但空报告必须 F（不能因集合收缩放过真空报告）。"""
        llm = _llm()
        state = _state(["market", "news"], {"market_report": GOOD}, empty_roles=("news",))
        summary = create_quality_gate(llm)(state)["data_quality_summary"]
        assert summary.count("[F]") == 1

    @pytest.mark.parametrize("team_size,fails,llm_should_run", [
        (1, 1, False),   # 单人失败：严格多数=1 → 跳过复审
        (2, 1, True),    # 两人一失败：恰半数不算多数 → 复审仍执行
        (4, 2, True),    # 四人两失败：同上
        (4, 3, False),   # 四人三失败：严格多数=3 → 跳过
        (7, 4, False),   # 七人四失败：与原始行为一致
    ])
    def test_strict_majority_skip_threshold(self, team_size, fails, llm_should_run):
        """跳过 LLM 复审的阈值是严格多数（n//2+1），不是 ceil(n/2)。

        偶数团队恰好一半失败不算多数（Codex 退回修正）。
        """
        roles = ["market", "social", "news", "fundamentals", "policy", "hot_money", "lockup"][:team_size]
        fields = {"market": "market_report", "social": "sentiment_report"}
        reports = {}
        for i, r in enumerate(roles):
            reports[fields.get(r, f"{r}_report")] = "" if i < fails else GOOD

        llm = _llm()
        state = _state(roles, reports)
        create_quality_gate(llm)(state)
        assert (llm.invoke.call_count == 1) is llm_should_run, (
            f"团队 {team_size} 人 / 失败 {fails} 人：LLM 复审应"
            f"{'执行' if llm_should_run else '跳过'}"
        )

    def test_factory_fallback_for_legacy_state(self):
        """旧 state 无集合元数据：工厂闭包给出当前图集合（不当七位）。"""
        llm = _llm()
        state = Propagator().create_initial_state("600519", "2026-01-15")
        state.pop("selected_analysts", None)  # 模拟旧断点
        state["market_report"] = GOOD
        node = create_quality_gate(
            llm, active_analysts_fallback=lambda: ["market"]
        )
        summary = node(state)["data_quality_summary"]
        assert "[F]" not in summary
        assert "1 位" in summary

    def test_default_full_team_when_state_silent(self):
        """state 未声明集合时按个股全集检查（兼容旧 checkpoint）。"""
        llm = _llm()
        state = Propagator().create_initial_state("600519", "2026-01-15")
        state["market_report"] = GOOD
        summary = create_quality_gate(llm)(state)["data_quality_summary"]
        assert summary.count("[F]") >= 6  # 其余六位为空 → F


# ===========================================================================
# A11：旧报告不隐藏新失败任务
# ===========================================================================


class TestIncompleteIndexIntegrity:
    def test_old_report_does_not_hide_resumable_new_run(self, monkeypatch, tmp_path):
        logs = tmp_path / "logs"
        path = logs / "600519" / "TradingAgentsStrategy_logs" / "full_states_log_2026-01-15.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"final_trade_decision": "old completed run"}', encoding="utf-8")
        monkeypatch.setattr(history, "_results_dir", lambda: logs)
        monkeypatch.setattr(history, "_checkpoint_step", lambda *a: 8)
        monkeypatch.setattr(
            history, "_INCOMPLETE_TASKS_FILE", tmp_path / "incomplete.json"
        )

        history.record_incomplete_task(
            "600519", "2026-01-15", status="error", error="NEW run failed"
        )
        rows = history.get_incomplete_history()

        assert rows, "有有效断点的新失败任务被旧报告隐藏"
        assert rows[0]["error"] == "NEW run failed"

    def test_stale_entry_without_checkpoint_pruned_by_report(self, monkeypatch, tmp_path):
        """无断点且已有完成报告 → 陈旧条目合理过滤。"""
        logs = tmp_path / "logs"
        path = logs / "600519" / "TradingAgentsStrategy_logs" / "full_states_log_2026-01-15.json"
        path.parent.mkdir(parents=True)
        path.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(history, "_results_dir", lambda: logs)
        monkeypatch.setattr(history, "_checkpoint_step", lambda *a: None)
        monkeypatch.setattr(
            history, "_INCOMPLETE_TASKS_FILE", tmp_path / "incomplete.json"
        )

        history.record_incomplete_task("600519", "2026-01-15", status="error")
        assert history.get_incomplete_history() == []

    def test_completed_run_clears_its_entry(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            history, "_INCOMPLETE_TASKS_FILE", tmp_path / "incomplete.json"
        )
        history.record_incomplete_task("600519", "2026-01-15", status="running")
        history.clear_incomplete_task("600519", "2026-01-15")
        assert history.get_incomplete_history() == []


# ===========================================================================
# A12：历史目录与写入配置一致
# ===========================================================================


class TestHistoryDirectoryContract:
    def test_custom_results_dir_visible(self, monkeypatch, tmp_path):
        logs = tmp_path / "custom-results"
        path = logs / "600519" / "TradingAgentsStrategy_logs" / "full_states_log_2026-01-15.json"
        path.parent.mkdir(parents=True)
        path.write_text("{}", encoding="utf-8")
        monkeypatch.setitem(history.DEFAULT_CONFIG, "results_dir", str(logs))

        rows = history.get_history()
        assert any(row["path"] == str(path) for row in rows)

    def test_saved_json_roundtrip(self, monkeypatch, tmp_path):
        """真实 _log_state 保存的自定义目录 JSON 可被历史列表找到并加载。"""
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
        graph.config = {"results_dir": str(tmp_path / "logs2")}
        graph.ticker = "600519"
        graph.log_states_dict = {}
        state = Propagator().create_initial_state("600519", "2026-01-15")
        state["final_trade_decision"] = "Rating: Buy"
        graph._log_state("2026-01-15", state)

        monkeypatch.setitem(history.DEFAULT_CONFIG, "results_dir", str(tmp_path / "logs2"))
        rows = history.get_history()
        assert rows, "自定义目录中的真实保存未进入历史列表"
        loaded = history.load_analysis(rows[0]["path"])
        assert loaded["final_trade_decision"] == "Rating: Buy"


# ===========================================================================
# A14：报告字段契约
# ===========================================================================


class TestReportFieldContract:
    def test_trader_plan_canonical_first(self):
        state = {
            "trader_investment_plan": "CANONICAL",
            "trader_investment_decision": "LEGACY",
        }
        assert trader_plan(state) == "CANONICAL"  # 两者都有：canonical 优先且只取一次

    def test_trader_plan_legacy_fallback(self):
        assert trader_plan({"trader_investment_decision": "LEGACY"}) == "LEGACY"

    def test_markdown_export_uses_canonical(self):
        state = Propagator().create_initial_state("600519", "2026-01-15")
        state["trader_investment_plan"] = "UNIQUE_TRADER_PLAN"
        text = generate_markdown(state, "600519", "2026-01-15", "Buy")
        assert "UNIQUE_TRADER_PLAN" in text

    def test_log_state_persists_quality_and_both_fields(self, tmp_path):
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
        graph.config = {"results_dir": str(tmp_path)}
        graph.ticker = "600519"
        graph.log_states_dict = {}
        state = Propagator().create_initial_state("600519", "2026-01-15")
        state["trader_investment_plan"] = "TRADER_PLAN"
        state["data_quality_summary"] = "CRITICAL_QUALITY_WARNING"
        graph._log_state("2026-01-15", state)

        saved = json.loads(
            (tmp_path / "600519/TradingAgentsStrategy_logs/full_states_log_2026-01-15.json")
            .read_text(encoding="utf-8")
        )
        assert saved["data_quality_summary"] == "CRITICAL_QUALITY_WARNING"
        assert saved["trader_investment_plan"] == "TRADER_PLAN"
        assert saved["trader_investment_decision"] == "TRADER_PLAN"  # legacy 别名
        # 共享访问器对落盘 JSON 的读取与实时一致
        assert quality_summary(saved) == "CRITICAL_QUALITY_WARNING"
        assert trader_plan(saved) == "TRADER_PLAN"

    def test_log_state_tolerates_partial_state(self, tmp_path):
        """部分完成的 state（缺辩论/决策字段）也能安全落盘。"""
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
        graph.config = {"results_dir": str(tmp_path)}
        graph.ticker = "600519"
        graph.log_states_dict = {}
        graph._log_state("2026-01-15", {"company_of_interest": "600519"})
        saved = json.loads(
            (tmp_path / "600519/TradingAgentsStrategy_logs/full_states_log_2026-01-15.json")
            .read_text(encoding="utf-8")
        )
        assert saved["final_trade_decision"] == ""
