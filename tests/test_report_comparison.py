"""N03 回归：历史报告对比（纯计算 + Markdown + CLI + Web 渲染）。

全部离线：无网络、无模型调用（LLM 均 Mock）、CLI 经子进程/直接调用。
"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage

from tradingagents.agents.quality_gate import REPORT_FIELDS, create_quality_gate
from tradingagents.graph.propagation import Propagator
from tradingagents.report_comparison import (
    MismatchedReportsError,
    _main,
    compare_reports,
    render_comparison_markdown,
)
from tradingagents.run_records import new_run_metadata
from tradingagents.default_config import DEFAULT_CONFIG

GOOD = "离线研究报告内容 " * 60 + "\n| 指标 | 结论 |\n|---|---|\n| 综合 | 通过 |"


def _make_report(
    ticker="600519",
    date="2026-01-15",
    roles=("market", "news"),
    decision="Rating: Buy\n买入理由充足。",
    trader="逢低分批建仓",
    quality_overrides=None,
    metadata=True,
    instrument_type="stock",
):
    state = Propagator().create_initial_state(ticker, date, selected_analysts=list(roles))
    for r in roles:
        state[REPORT_FIELDS[r]] = GOOD
    llm = Mock()
    llm.invoke.return_value = AIMessage(content="reviewed")
    state.update(create_quality_gate(llm)(state))
    if quality_overrides:
        state["data_quality"].update(quality_overrides)
    state["final_trade_decision"] = decision
    state["trader_investment_plan"] = trader
    state["trader_investment_decision"] = trader  # legacy 别名同步
    if metadata:
        state["run_metadata"] = new_run_metadata(
            DEFAULT_CONFIG, ticker, date, instrument_type=instrument_type,
            selected_analysts=list(roles),
        )
    return state


class TestComparisonCore:
    def test_same_day_different_config(self):
        """同日不同配置（模型不同）：比较成功，配置差异明确标注。"""
        a = _make_report()
        b = _make_report()
        b["run_metadata"]["config_snapshot"]["deep_think_llm"] = "other-model"
        b["run_metadata"]["config_fingerprint"] = "different"
        c = compare_reports(a, b)
        assert c["same_date"] is True
        assert c["config_diff"]["comparable"] is True
        assert "deep_think_llm" in c["config_diff"]["changed_keys"]

    def test_different_days_same_config(self):
        """不同日同配置：允许比较。"""
        a = _make_report(date="2026-01-10")
        b = _make_report(date="2026-01-15")
        c = compare_reports(a, b)
        assert c["same_date"] is False
        assert c["ticker"] == "600519"

    def test_different_ticker_rejected(self):
        a = _make_report(ticker="600519")
        b = _make_report(ticker="000001")
        with pytest.raises(MismatchedReportsError, match="标的不同"):
            compare_reports(a, b)

    def test_stock_vs_index_rejected(self):
        """同标的但一侧 stock 一侧 index → 拒绝（ticker 先行检查不触发）。"""
        a = _make_report(ticker="000300.SH", instrument_type="stock")
        b = _make_report(ticker="000300.SH", instrument_type="index")
        with pytest.raises(MismatchedReportsError, match="instrument_type"):
            compare_reports(a, b)

    def test_different_ticker_also_rejected_for_cross_type(self):
        a = _make_report()
        b = _make_report(ticker="000300.SH", instrument_type="index")
        with pytest.raises(MismatchedReportsError, match="标的不同"):
            compare_reports(a, b)

    def test_all_fields_same(self):
        a = _make_report()
        b = _make_report()
        c = compare_reports(a, b)
        assert not c["rating_changed"]
        assert not c["trader"]["changed"]
        assert not c["final_decision"]["changed"]
        assert not c["analyst_changes"]
        md = render_comparison_markdown(c)
        assert "无变化" in md

    def test_rating_change_no_hold_default(self):
        """评级缺失不默认 Hold。"""
        a = _make_report(decision="Rating: Buy")
        b = _make_report(decision="无明确评级倾向")  # 无可解析评级
        c = compare_reports(a, b)
        assert "Buy" in c["left"]["rating"]
        assert "未记录" in c["right"]["rating"]
        assert c["rating_changed"]

    def test_trader_canonical_priority_single_comparison(self):
        """交易员 canonical 优先、legacy 回退，只比较一次。"""
        a = _make_report(trader="plan A")
        b = _make_report(trader="plan B")
        b.pop("trader_investment_plan")  # 只留 legacy
        b["trader_investment_decision"] = "plan B"
        c = compare_reports(a, b)
        assert c["trader"]["changed"] is True

    def test_role_not_run_vs_empty_report(self):
        """角色未运行 vs 已启用但空白可区分。"""
        a = _make_report(roles=("market",))  # news 未启用
        b = _make_report(roles=("market", "news"))
        b[REPORT_FIELDS["news"]] = ""  # news 已启用但空白
        c = compare_reports(a, b)
        news_changes = [x for x in c["analyst_changes"] if x["role"] == "news"]
        assert news_changes, "news 差异未检出"
        assert news_changes[0]["left_label"] == "未运行"
        assert news_changes[0]["right_label"] == "已启用但报告空白"

    def test_legacy_report_quality_and_config_unknown(self):
        """旧格式（无 data_quality/run_metadata）：不默认为相同，标为未知。"""
        a = _make_report(metadata=False)
        a.pop("data_quality", None)
        b = _make_report(metadata=False)
        b.pop("data_quality", None)
        c = compare_reports(a, b)
        assert c["config_diff"]["comparable"] is False
        assert c["quality"]["left"]["known"] is False
        md = render_comparison_markdown(c)
        assert "无法确认" in md
        assert "未记录" in md

    def test_no_capability_judgment_in_markdown(self):
        """不输出模型能力/收益判断。"""
        a = _make_report(decision="Rating: Buy\nold")
        b = _make_report(decision="Rating: Sell\nnew")
        md = render_comparison_markdown(compare_reports(a, b))
        for banned in ("更准确", "收益提升", "模型能力提升", "更好"):
            assert banned not in md, f"不当判断: {banned}"

    def test_quality_limitation_changes_tracked(self):
        """质量限制变化被追踪（左侧 insufficient → 右侧 complete）。"""
        a = _make_report(roles=("market",))
        # 让左侧 market 报告空白并重算质量卡 → insufficient
        a[REPORT_FIELDS["market"]] = ""
        llm = Mock()
        llm.invoke.return_value = AIMessage(content="reviewed")
        a.update(create_quality_gate(llm)(a))
        b = _make_report(roles=("market",))
        c = compare_reports(a, b)
        assert c["quality"]["diff"]["comparable"]
        assert c["quality"]["diff"]["status_changed"]
        assert c["quality"]["diff"]["resolved_limitations"]

    def test_text_change_ratio_not_score(self):
        """变化比例不作为模型分数输出。"""
        a = _make_report()
        b = _make_report(decision="Rating: Buy\ncompletely different decision text")
        md = render_comparison_markdown(compare_reports(a, b))
        assert "%" not in md.split("## 最终决策正文")[1].split("---")[0].replace("100%", "")


class TestComparisonCLI:
    def test_cli_subprocess_success(self, tmp_path):
        left = tmp_path / "l.json"
        right = tmp_path / "r.json"
        left.write_text(json.dumps(_make_report(), ensure_ascii=False), encoding="utf-8")
        right.write_text(
            json.dumps(_make_report(decision="Rating: Sell"), ensure_ascii=False),
            encoding="utf-8",
        )
        out = tmp_path / "cmp.md"
        result = subprocess.run(
            [sys.executable, "-m", "tradingagents.report_comparison",
             str(left), str(right), "--output", str(out)],
            capture_output=True, text=True, cwd=".",
        )
        assert result.returncode == 0, result.stderr
        assert out.exists()
        md = out.read_text(encoding="utf-8")
        assert "历史报告对比" in md

    def test_cli_mismatch_exit_code(self, tmp_path):
        left = tmp_path / "l.json"
        right = tmp_path / "r.json"
        left.write_text(json.dumps(_make_report()), encoding="utf-8")
        right.write_text(json.dumps(_make_report(ticker="000001")), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "tradingagents.report_comparison",
             str(left), str(right)],
            capture_output=True, text=True, cwd=".",
        )
        assert result.returncode == 1
        assert "标的不同" in result.stderr

    def test_cli_bad_json_exit_code(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        good = tmp_path / "g.json"
        good.write_text(json.dumps(_make_report()), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "tradingagents.report_comparison",
             str(bad), str(good)],
            capture_output=True, text=True, cwd=".",
        )
        assert result.returncode == 2

    def test_cli_direct_invocation(self, tmp_path):
        left = tmp_path / "l.json"
        right = tmp_path / "r.json"
        left.write_text(json.dumps(_make_report()), encoding="utf-8")
        right.write_text(json.dumps(_make_report()), encoding="utf-8")
        assert _main([str(left), str(right)]) == 0


class TestComparisonWebRendering:
    def test_markdown_contains_core_sections(self):
        a = _make_report()
        b = _make_report(decision="Rating: Sell\nsell reasoning")
        md = render_comparison_markdown(compare_reports(a, b))
        for section in ("评级对比", "公开配置差异", "结构化质量状态",
                        "分析师报告变化", "交易员计划", "最终决策正文"):
            assert section in md, f"缺少 {section}"

    def test_condition_caveats_when_config_differs(self):
        a = _make_report()
        b = _make_report()
        b["run_metadata"]["config_snapshot"]["llm_provider"] = "openai"
        md = render_comparison_markdown(compare_reports(a, b))
        assert "配置不同" in md or "配置存在差异" in md
        assert "不表示任何一方更优" in md

    def test_zero_network_zero_model(self):
        """compare + render 全路径不触及网络或模型（纯 dict 计算）。"""
        import requests as req

        original = req.Session.request
        req.Session.request = Mock(side_effect=AssertionError("network in comparison"))
        try:
            a = _make_report()
            b = _make_report()
            md = render_comparison_markdown(compare_reports(a, b))
            assert md
        finally:
            req.Session.request = original
