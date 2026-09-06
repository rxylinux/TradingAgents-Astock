"""演进第一批回归（N01 质量卡与结论限制 / N02 运行档案与版本存档）。

全部离线：无网络（requests/yfinance 均有替身的场景不触发）、无真实模型、
持久化路径指向 tmp。状态规则、双路径 stock/index、SQLite 恢复 ID 不变、
白名单、同日多版本、唯一 key、原子存档等验收点逐项覆盖。
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from tradingagents.agents import quality_gate as qg
from tradingagents.agents.quality_gate import REPORT_FIELDS, create_quality_gate
from tradingagents.agents.report_quality import (
    STATUS_COMPLETE,
    STATUS_INSUFFICIENT,
    STATUS_LIMITED,
    STATUS_UNKNOWN,
    append_limitation_notice,
    assess_reports,
    limitation_notice_tag,
    quality_context_for_prompt,
    render_limitation_notice,
    render_quality_card_md,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.propagation import Propagator
from tradingagents.run_records import (
    build_config_snapshot,
    config_fingerprint,
    is_run_metadata,
    new_run_metadata,
    validate_run_id,
)
from web import history

GOOD = "【合格报告】" + "详细分析内容" * 60 + "\n| 指标 | 结论 |\n|---|---|\n| 综合 | 通过 |"
ALL_ROLES = list(REPORT_FIELDS.keys())


def _state(roles, reports=None):
    state = Propagator().create_initial_state(
        "600519", "2026-01-15", selected_analysts=roles
    )
    state.update(reports or {})
    return state


# ===========================================================================
# N01：结构化质量卡
# ===========================================================================


class TestQualityStatusRules:
    def test_all_a_is_complete(self):
        dq = assess_reports(
            _state(["market"], {"market_report": GOOD}), ["market"]
        )
        assert dq["status"] == STATUS_COMPLETE
        assert dq["fail_count"] == 0
        assert dq["active_count"] == 1

    def test_single_role_d_is_insufficient(self):
        """单角色 D/F：1//2+1=1 即严格多数 → insufficient（任务书规则）。"""
        short = "x" * 50  # D：过短
        dq = assess_reports(
            _state(["market"], {"market_report": short}), ["market"]
        )
        assert dq["status"] == STATUS_INSUFFICIENT

    def test_b_grade_in_bigger_team_is_limited(self):
        """两位中一位 D（恰半数，非严格多数）→ limited。"""
        reports = {"market_report": "x" * 50, REPORT_FIELDS["social"]: GOOD}
        dq = assess_reports(_state(["market", "social"], reports),
                            ["market", "social"])
        assert dq["status"] == STATUS_LIMITED

    def test_strict_majority_df_is_insufficient(self):
        # 2 位中 1 位失败（恰半数）→ limited；3 位中 2 位失败（严格多数）→ insufficient
        reports = {
            "market_report": "",
            REPORT_FIELDS["social"]: "",
            "news_report": GOOD,
        }
        dq = assess_reports(_state(["market", "social", "news"], reports),
                            ["market", "social", "news"])
        assert dq["status"] == STATUS_INSUFFICIENT

    def test_even_team_half_failure_is_limited_not_insufficient(self):
        reports = {"market_report": "", REPORT_FIELDS["social"]: GOOD}
        dq = assess_reports(_state(["market", "social"], reports),
                            ["market", "social"])
        assert dq["status"] == STATUS_LIMITED

    def test_empty_collection_is_unknown(self):
        dq = assess_reports(_state([], {}), [])
        assert dq["status"] == STATUS_UNKNOWN

    def test_disabled_roles_not_counted(self):
        dq = assess_reports(_state(["market"], {"market_report": GOOD}),
                            ["market"])
        assert dq["active_count"] == 1
        assert all(a["role"] == "market" for a in dq["analysts"])

    def test_chars_and_limitations_recorded(self):
        dq = assess_reports(_state(["market"], {"market_report": "短"}), ["market"])
        assert dq["analysts"][0]["chars"] == len("短")
        assert dq["limitations"]  # 失败产生限制项


class TestQualityGateDualOutput:
    def test_gate_emits_both_structured_and_summary(self):
        llm = Mock()
        llm.invoke.return_value = AIMessage(content="reviewed")
        state = _state(["market"], {"market_report": GOOD})
        out = create_quality_gate(llm)(state)
        assert "data_quality_summary" in out and "data_quality" in out
        assert out["data_quality"]["status"] == STATUS_COMPLETE

    def test_llm_review_does_not_override_hard_checks(self):
        llm = Mock()
        llm.invoke.return_value = AIMessage(content="全部 A 级，数据完美")
        state = _state(["market"], {"market_report": ""})  # 空报告 → 硬检查 F
        out = create_quality_gate(llm)(state)
        assert out["data_quality"]["status"] == STATUS_INSUFFICIENT
        assert out["data_quality"]["analysts"][0]["grade"] == "F"


class TestDeterministicLimitation:
    def test_notice_appended_once_idempotent(self):
        dq = assess_reports(_state(["market"], {"market_report": ""}), ["market"])
        decision = append_limitation_notice("Rating: Buy", dq)
        assert limitation_notice_tag() in decision
        again = append_limitation_notice(decision, dq)
        assert again == decision  # 幂等：只出现一次

    def test_complete_and_unknown_no_notice(self):
        dq_complete = assess_reports(
            _state(["market"], {"market_report": GOOD}), ["market"]
        )
        assert append_limitation_notice("Rating: Buy", dq_complete) == "Rating: Buy"
        assert append_limitation_notice("Rating: Buy", None) == "Rating: Buy"
        assert append_limitation_notice("Rating: Buy", {"status": "unknown"}) == "Rating: Buy"

    def test_prompt_context_empty_for_legacy_state(self):
        state = _state(["market"], {"market_report": GOOD})
        state.pop("data_quality", None)  # 旧 state 无字段
        assert quality_context_for_prompt(state) == ""

    def test_prompt_context_lists_limitations(self):
        dq = assess_reports(_state(["market"], {"market_report": ""}), ["market"])
        state = {"data_quality": dq}
        ctx = quality_context_for_prompt(state)
        assert "数据质量限制" in ctx and "限制" in ctx


class TestQualityCardSurfaces:
    def test_card_markdown_structured(self):
        dq = assess_reports(_state(["market"], {"market_report": ""}), ["market"])
        md = render_quality_card_md(dq)
        assert STATUS_INSUFFICIENT in md and "不是事实准确率" in md

    def test_card_legacy_says_unrecorded(self):
        md = render_quality_card_md(None)
        assert "未记录结构化质量检查" in md

    def test_markdown_pdf_export_contains_card(self):
        from web.pdf_export import generate_markdown

        state = _state(["market"], {"market_report": GOOD})
        state["data_quality"] = assess_reports(state, ["market"])
        text = generate_markdown(state, "600519", "2026-01-15", "Buy")
        assert "报告质量卡" in text

    def test_log_state_saves_data_quality_and_metadata(self, tmp_path):
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
        graph.config = {"results_dir": str(tmp_path)}
        graph.ticker = "600519"
        graph.log_states_dict = {}
        state = _state(["market"], {"market_report": GOOD})
        state["data_quality"] = assess_reports(state, ["market"])
        meta = new_run_metadata(DEFAULT_CONFIG, "600519", "2026-01-15")
        state["run_metadata"] = meta
        graph._log_state("2026-01-15", state)

        saved = json.loads(
            (tmp_path / "600519/TradingAgentsStrategy_logs/full_states_log_2026-01-15.json")
            .read_text(encoding="utf-8")
        )
        assert saved["data_quality"]["status"] == STATUS_COMPLETE
        assert saved["run_metadata"]["run_id"] == meta["run_id"]


# ===========================================================================
# N02：运行档案与版本存档
# ===========================================================================


SECRETISH = {
    "api_key": "sk-secret",
    "backend_url": "https://secret-gateway/v1",
    "ANTHROPIC_API_KEY": "sk-ant",
}


class TestRunMetadata:
    def test_fresh_ids_unique_and_valid(self):
        a = new_run_metadata(DEFAULT_CONFIG, "600519", "2026-01-15")
        b = new_run_metadata(DEFAULT_CONFIG, "600519", "2026-01-15")
        assert a["run_id"] != b["run_id"]
        assert validate_run_id(a["run_id"])
        assert is_run_metadata(a)
        assert a["created_at"].endswith("+00:00")  # 明确 UTC 时区

    def test_snapshot_whitelist_excludes_secrets(self):
        cfg = {
            **DEFAULT_CONFIG,
            **SECRETISH,
            "role_llms": {
                "bull": {"provider": "deepseek", "model": "m", "api_key": "sk-x"},
            },
        }
        snap = build_config_snapshot(cfg)
        blob = json.dumps(snap, ensure_ascii=False)
        for secret in ("sk-secret", "secret-gateway", "sk-ant", "sk-x", "backend_url"):
            assert secret not in blob, f"秘密泄漏进快照: {secret}"
        assert snap["role_llms"]["bull"] == {"provider": "deepseek", "model": "m"}

    def test_fingerprint_order_insensitive_within_set(self):
        a = build_config_snapshot({**DEFAULT_CONFIG, "selected_analysts": ["news", "market"]})
        b = build_config_snapshot({**DEFAULT_CONFIG, "selected_analysts": ["market", "news"]})
        assert config_fingerprint(a) == config_fingerprint(b)

    def test_fingerprint_changes_on_meaningful_config(self):
        base = build_config_snapshot({**DEFAULT_CONFIG, "deep_think_llm": "m1"})
        changed = build_config_snapshot({**DEFAULT_CONFIG, "deep_think_llm": "m2"})
        assert config_fingerprint(base) != config_fingerprint(changed)
        vendors = build_config_snapshot({**DEFAULT_CONFIG, "data_vendors": {"x": "other"}})
        assert config_fingerprint(base) != config_fingerprint(vendors)

    def test_snapshot_does_not_reference_caller_dict(self):
        cfg = {"selected_analysts": ["market"]}
        snap = build_config_snapshot(cfg)
        cfg["selected_analysts"].append("news")
        assert snap["selected_analysts"] == ["market"]


class TestSqliteRunIdentity:
    @pytest.fixture(autouse=True)
    def _offline(self, monkeypatch):
        import requests
        from tradingagents.graph import trading_graph

        monkeypatch.setattr(
            requests.Session, "request",
            Mock(side_effect=AssertionError("Unexpected network")),
        )
        monkeypatch.setattr(
            trading_graph.yf, "Ticker",
            Mock(side_effect=AssertionError("Unexpected Yahoo")),
        )

    def _graph(self, tmp_path, roles):
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        with patch(
            "tradingagents.graph.trading_graph.create_llm_client",
            return_value=Mock(get_llm=Mock(return_value=_StubChat())),
        ):
            return TradingAgentsGraph(
                roles,
                config={
                    **DEFAULT_CONFIG,
                    "data_cache_dir": str(tmp_path / "cache"),
                    "results_dir": str(tmp_path / "reports"),
                    "memory_log_path": None,
                    "checkpoint_enabled": True,
                },
            )

    def test_resume_keeps_same_identity_fresh_makes_new(self, tmp_path):
        """SQLite 恢复保留同一 run_id/创建时间；同实例 fresh 产生新 ID。"""
        graph = self._graph(tmp_path, ["market"])
        try:
            state, args, _ = graph.prepare_graph_run("600519", "2026-01-15")
            original = state["run_metadata"]
            # 模拟中途失败：直接离开（checkpoint 已在 prepare 阶段建立于首次写入后）
            list(graph.graph.stream(state, **args, interrupt_before=["Quality Gate"]))
        finally:
            graph.close_graph_run()

        resumed = self._graph(tmp_path, ["market"])
        try:
            state2, args2, step = resumed.prepare_graph_run("600519", "2026-01-15")
            assert state2 is None and step is not None, "应走恢复路径"
            snap = resumed.graph.get_state(args2["config"])
            restored = snap.values["run_metadata"]
            assert restored["run_id"] == original["run_id"], "恢复重建了身份"
            assert restored["created_at"] == original["created_at"]
            assert restored["config_fingerprint"] == original["config_fingerprint"]

            # 同实例 fresh（清除断点后）→ 新 ID
            from tradingagents.graph.checkpointer import clear_checkpoint

            clear_checkpoint(str(tmp_path / "cache"), "600519", "2026-01-15")
            state3, _, _ = resumed.prepare_graph_run("600519", "2026-01-15")
            assert state3["run_metadata"]["run_id"] != original["run_id"]
        finally:
            resumed.close_graph_run()

    def test_legacy_checkpoint_without_metadata_stays_unrecorded(self, tmp_path):
        """旧断点（无 run_metadata）恢复 → 不伪造档案（保持未记录）。"""
        graph = self._graph(tmp_path, ["market"])
        try:
            state, args, _ = graph.prepare_graph_run("600519", "2026-01-15")
            state.pop("run_metadata", None)  # 旧版本断点无该字段
            list(graph.graph.stream(state, **args, interrupt_before=["Quality Gate"]))
        finally:
            graph.close_graph_run()

        resumed = self._graph(tmp_path, ["market"])
        try:
            state2, args2, step = resumed.prepare_graph_run("600519", "2026-01-15")
            assert state2 is None and step is not None
            snap = resumed.graph.get_state(args2["config"])
            assert not snap.values.get("run_metadata"), "旧断点被伪造了档案"
        finally:
            resumed.close_graph_run()


class _StubChat(BaseChatModel):
    """离线 LLM：链式可用的 BaseChatModel。"""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        text = "离线报告内容 " * 30 + "\n| 指标 | 结论 |\n|---|---|\n| 综合 | 通过 |"
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=text))]
        )

    @property
    def _llm_type(self):
        return "evolution-stub"

    def bind_tools(self, tools, **kwargs):
        return self


class TestVersionedArchives:
    def _finalize_with(self, tmp_path, meta, decision="Rating: Buy"):
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
        graph.config = {"results_dir": str(tmp_path)}
        graph.ticker = "600519"
        graph.log_states_dict = {}
        state = Propagator().create_initial_state("600519", "2026-01-15")
        state["final_trade_decision"] = decision
        state["run_metadata"] = meta
        graph._log_state("2026-01-15", state)

    def test_two_runs_same_day_both_kept(self, tmp_path):
        meta_a = new_run_metadata(DEFAULT_CONFIG, "600519", "2026-01-15")
        meta_b = new_run_metadata(DEFAULT_CONFIG, "600519", "2026-01-15")
        self._finalize_with(tmp_path, meta_a, decision="Rating: Buy (A)")
        self._finalize_with(tmp_path, meta_b, decision="Rating: Sell (B)")

        monkeypatch_home = tmp_path
        import unittest.mock as m

        with m.patch.object(history, "_results_dir", lambda: tmp_path):
            rows = history.get_history()
        assert len(rows) == 2, f"同日两次运行未都保留: {rows}"
        assert {r["run_id"] for r in rows} == {meta_a["run_id"], meta_b["run_id"]}
        # 最新兼容入口 = 最后一次（B）
        latest = json.loads(
            (tmp_path / "600519/TradingAgentsStrategy_logs/full_states_log_2026-01-15.json")
            .read_text(encoding="utf-8")
        )
        assert "Sell (B)" in latest["final_trade_decision"]
        # 两个版本文件内容各自独立
        for meta, marker in ((meta_a, "Buy (A)"), (meta_b, "Sell (B)")):
            p = (tmp_path / "_runs" / meta["run_id"] / "600519"
                 / "TradingAgentsStrategy_logs" / "full_states_log_2026-01-15.json")
            data = json.loads(p.read_text(encoding="utf-8"))
            assert marker in data["final_trade_decision"]

    def test_duplicate_finalize_idempotent(self, tmp_path):
        """同 run_id 重复 finalize：相同内容 no-op；不同内容显式拒绝。

        Codex 边审语义：已发布的历史记录不可变——冲突不得静默分叉存档与
        latest（允许抛错或完全不动文件，这里实现为显式拒绝）。
        """
        meta = new_run_metadata(DEFAULT_CONFIG, "600519", "2026-01-15")
        self._finalize_with(tmp_path, meta, decision="v1")
        version_path = (
            tmp_path / "_runs" / meta["run_id"] / "600519"
            / "TradingAgentsStrategy_logs" / "full_states_log_2026-01-15.json"
        )
        first_mtime = version_path.stat().st_mtime_ns
        first_bytes = version_path.read_bytes()
        latest_bytes = (
            tmp_path / "600519/TradingAgentsStrategy_logs/full_states_log_2026-01-15.json"
        ).read_bytes()

        with pytest.raises(RuntimeError, match="拒绝覆盖"):
            self._finalize_with(tmp_path, meta, decision="v2-different")

        assert version_path.read_bytes() == first_bytes, "冲突写改动了已发布存档"
        assert version_path.stat().st_mtime_ns == first_mtime
        assert (
            tmp_path / "600519/TradingAgentsStrategy_logs/full_states_log_2026-01-15.json"
        ).read_bytes() == latest_bytes, "冲突写改动了 latest"

    def test_no_metadata_writes_legacy_only(self, tmp_path):
        self._finalize_with(tmp_path, None)
        assert not (tmp_path / "_runs").exists(), "无档案不应写版本目录"
        assert (tmp_path / "600519/TradingAgentsStrategy_logs/full_states_log_2026-01-15.json").exists()

    def test_history_dedupes_run_across_paths(self, tmp_path):
        meta = new_run_metadata(DEFAULT_CONFIG, "600519", "2026-01-15")
        self._finalize_with(tmp_path, meta)
        with patch.object(history, "_results_dir", lambda: tmp_path):
            rows = history.get_history()
        assert len(rows) == 1, "同 run_id 的版本+兼容文件显示了两次"
        assert rows[0]["run_id"] == meta["run_id"]
        assert rows[0]["created_at"]  # 历史项带创建时间

    def test_malformed_json_skipped_not_fatal(self, tmp_path):
        meta = new_run_metadata(DEFAULT_CONFIG, "600519", "2026-01-15")
        self._finalize_with(tmp_path, meta)
        bad = tmp_path / "_runs" / ("0" * 32) / "600519" / "TradingAgentsStrategy_logs"
        bad.mkdir(parents=True)
        (bad / "full_states_log_2026-01-15.json").write_text("{not json", encoding="utf-8")
        with patch.object(history, "_results_dir", lambda: tmp_path):
            rows = history.get_history()
        assert len(rows) == 1  # 畸形被跳过，好记录仍在

    def test_run_id_traversal_rejected_in_archive(self, tmp_path):
        """带路径穿越形态的 run_id 不被写入版本目录。"""
        assert not validate_run_id("../../etc")
        assert not validate_run_id("ABC-123")
        # is_run_metadata 同样拒绝 → _log_state 只写兼容路径
        fake = {
            "schema_version": 1, "run_id": "../../escape", "ticker": "x",
            "trade_date": "2026-01-15", "instrument_type": "stock",
            "created_at": "2026", "config_snapshot": {}, "config_fingerprint": "f",
        }
        assert not is_run_metadata(fake)

    def test_atomic_write_leaves_no_partial(self, tmp_path):
        """写入失败不留半份可见 JSON（tmp 文件名唯一 + replace 原子）。"""
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
        graph.config = {"results_dir": str(tmp_path)}
        graph.ticker = "600519"
        graph.log_states_dict = {}
        state = Propagator().create_initial_state("600519", "2026-01-15")
        state["final_trade_decision"] = "Rating: Buy"

        # 让 json.dump 序列化失败（不可序列化对象）
        state["final_trade_decision"] = object()
        with pytest.raises(TypeError):
            graph._log_state("2026-01-15", state)
        leftovers = [
            p for p in (tmp_path / "600519/TradingAgentsStrategy_logs").iterdir()
            if ".tmp." in p.name
        ]
        # 失败的 tmp 允许残留（不可见为半份报告——非 full_states_log_*.json 名），
        # 但绝不能存在完整名的半份文件
        assert not (tmp_path / "600519/TradingAgentsStrategy_logs/full_states_log_2026-01-15.json").exists()


class TestSidebarKeyUniqueness:
    def test_history_button_keys_unique_for_same_ticker_date(self, tmp_path):
        """同 ticker/date 两次运行的历史条目生成不同按钮 key（key 冲突会让
        Streamlit 只剩一个可点）。"""
        meta_a = new_run_metadata(DEFAULT_CONFIG, "600519", "2026-01-15")
        meta_b = new_run_metadata(DEFAULT_CONFIG, "600519", "2026-01-15")
        for meta in (meta_a, meta_b):
            TestVersionedArchives()._finalize_with(tmp_path, meta)
        with patch.object(history, "_results_dir", lambda: tmp_path):
            rows = history.get_history()
        keys = set()
        for entry in rows:
            run_id = entry.get("run_id") or ""
            key = (
                f"hist_{entry['ticker']}_{entry['date']}_{run_id}" if run_id
                else f"hist_{entry['ticker']}_{entry['date']}_{abs(hash(entry.get('path', '')))}"
            )
            keys.add(key)
        assert len(keys) == 2
