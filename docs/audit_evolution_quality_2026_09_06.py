"""Codex independent N01 checks: completeness is not factual confidence."""
import json
from contextlib import nullcontext
from importlib import import_module
from unittest.mock import MagicMock, Mock

import pytest
import requests
from langchain_core.messages import AIMessage

from tradingagents.agents.quality_gate import REPORT_FIELDS, create_quality_gate
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.trading_graph import TradingAgentsGraph
from web import history

GOOD = "离线报告内容" * 70 + "\n| 项目 | 结果 |\n|---|---|\n| 内容 | 已提供 |"


def quality_state(roles, empty=()):
    state = Propagator().create_initial_state("600519", "2026-01-15", selected_analysts=roles)
    for role in roles:
        state[REPORT_FIELDS[role]] = "" if role in empty else GOOD
    llm = Mock()
    llm.invoke.return_value = AIMessage(content="模型声称全部 A，置信度 100%")
    state.update(create_quality_gate(llm)(state))
    return state


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(requests.Session, "request", Mock(side_effect=AssertionError("Unexpected network")))


@pytest.mark.parametrize("roles,empty,status", [
    (["market"], [], "complete"),
    (["market"], ["market"], "insufficient"),
    (["market", "news"], ["news"], "limited"),
    (["market", "news", "social", "policy"], ["news", "social"], "limited"),
    (["market", "news", "social", "policy"], ["news", "social", "policy"], "insufficient"),
    (["market", "social", "news", "policy", "hot_money"], [], "complete"),
])
def test_quality_status_comes_from_actual_active_reports(roles, empty, status):
    state = quality_state(roles, empty)
    quality = state.get("data_quality")
    assert isinstance(quality, dict), "Quality gate did not return structured assessment"
    assert quality["schema_version"] == 1
    assert quality["status"] == status
    assert set(quality["selected_analysts"]) == set(roles)
    assert "100%" not in json.dumps(quality, ensure_ascii=False), "LLM claim overwrote hard-check assessment"


@pytest.mark.parametrize("module,factory,final", [
    ("tradingagents.agents.managers.research_manager", "create_research_manager", False),
    ("tradingagents.agents.trader.trader", "create_trader", False),
    ("tradingagents.agents.managers.portfolio_manager", "create_portfolio_manager", True),
    ("tradingagents.agents.index_agents", "create_index_trader", False),
    ("tradingagents.agents.index_agents", "create_index_portfolio_manager", True),
])
def test_decision_nodes_receive_quality_and_final_notice_is_deterministic(module, factory, final):
    state = quality_state(["market"], ["market"])
    # These downstream nodes require the preceding graph stages' outputs.
    state.update(investment_plan="AUDIT_RESEARCH_PLAN", trader_investment_plan="AUDIT_TRADER_PLAN")
    if "index" in factory:
        state["company_of_interest"] = "000001.SH"
    assert "data_quality" in state
    llm = Mock()
    llm.with_structured_output.side_effect = NotImplementedError("offline free text")
    llm.invoke.return_value = AIMessage(content="**Rating**: Buy\nAUDIT_MODEL_IGNORES_LIMITATIONS")
    node = getattr(import_module(module), factory)(llm)
    result = node(state)
    prompt = "\n".join(str(c.args) for c in llm.invoke.call_args_list)
    assert any(marker in prompt for marker in ("insufficient", "资料不足", "报告不足", "完整性", "质量")), prompt
    if final:
        text = result["final_trade_decision"]
        assert "AUDIT_MODEL_IGNORES_LIMITATIONS" in text
        assert text != llm.invoke.return_value.content, "Insufficient-quality notice relied only on the LLM"
        assert any(marker in text for marker in ("受限", "不足", "insufficient", "完整性")), text


def test_quality_structure_roundtrips_in_actual_saved_report(tmp_path):
    state = quality_state(["market", "news"], ["news"])
    assert state.get("data_quality")
    graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
    graph.config = {"results_dir": str(tmp_path)}
    graph.ticker, graph.log_states_dict = "600519", {}
    graph._log_state("2026-01-15", state)
    loaded = history.load_analysis(str(next(tmp_path.rglob("full_states_log_*.json"))))
    assert loaded["data_quality"] == state["data_quality"]


def test_unknown_quality_never_becomes_a_passed_prompt():
    from tradingagents.agents.report_quality import assess_reports, quality_context_for_prompt
    assessment = assess_reports({}, [])
    assert assessment["status"] == "unknown"
    text = quality_context_for_prompt({"data_quality": assessment})
    assert "全部启用分析师报告通过" not in text, text
    assert "检查通过" not in text, text


@pytest.mark.parametrize("previous", ["marker_only", "stale_notice"])
def test_model_marker_or_stale_notice_cannot_suppress_current_limitations(previous):
    from tradingagents.agents.report_quality import (
        append_limitation_notice, limitation_notice_tag, render_limitation_notice,
    )
    current = quality_state(["market"], ["market"])["data_quality"]
    old = limitation_notice_tag()
    if previous == "stale_notice":
        old = render_limitation_notice(quality_state(["market", "news"], ["news"])["data_quality"])
    original = "**Rating**: Buy\nAUDIT_REASONING\n" + old
    text = append_limitation_notice(original, current)
    expected = render_limitation_notice(current)
    assert expected in text, "Untrusted/generated marker suppressed the actual current restrictions"
    assert text.count(expected) == 1
    assert "AUDIT_REASONING" in text and "**Rating**: Buy" in text
    assert append_limitation_notice(text, current) == text


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("surface", ["web", "markdown", "pdf_sections"])
def test_structured_quality_is_visible_across_report_surfaces(monkeypatch, legacy, surface):
    from web import pdf_export
    from web.components import report_viewer
    state = quality_state(["market"], ["market"])
    if legacy:
        state.pop("data_quality")
    else:
        state["data_quality"]["limitations"].append("AUDIT_SPECIFIC_DATA_GAP")
    if surface == "markdown":
        text = pdf_export.generate_markdown(state, "600519", "2026-01-15", "Buy")
    elif surface == "pdf_sections":
        renderer = MagicMock()
        renderer.output.return_value = b"offline"
        monkeypatch.setattr(pdf_export, "_ReportPDF", MagicMock(return_value=renderer))
        pdf_export.generate_pdf(state, "600519", "2026-01-15", "Buy")
        text = "\n".join(str(c.args) for c in renderer.add_section.call_args_list)
    else:
        ui = MagicMock()
        ui.columns.side_effect = lambda cols: [nullcontext() for _ in cols]
        ui.tabs.side_effect = lambda tabs: [nullcontext() for _ in tabs]
        ui.expander.side_effect = lambda *a, **kw: nullcontext()
        monkeypatch.setattr(report_viewer, "st", ui)
        monkeypatch.setattr(report_viewer, "_cached_generate_pdf", lambda *a: b"offline")
        monkeypatch.setattr(report_viewer, "_cached_generate_markdown", pdf_export.generate_markdown)
        report_viewer.render_report(state, "600519", "2026-01-15", "Buy")
        text = "\n".join(str(c.args) for c in ui.markdown.call_args_list)
    assert "完整性" in text
    if legacy:
        assert "未记录结构化质量检查" in text
    else:
        assert "insufficient" in text and "AUDIT_SPECIFIC_DATA_GAP" in text
