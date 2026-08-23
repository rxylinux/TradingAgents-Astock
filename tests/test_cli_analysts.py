"""Tests for CLI 7-Agent alignment, model definitions, utilities, and status tracking."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from cli.models import AnalystType
from cli.utils import ANALYST_CHOICES, ANALYST_ORDER, select_analysts
from cli.main import (
    MessageBuffer,
    ANALYST_AGENT_NAMES,
    ANALYST_REPORT_MAP,
    update_analyst_statuses,
    save_report_to_disk,
    display_complete_report,
)


def test_analyst_type_enum_all_7():
    """Verify AnalystType has all 7 analyst members with expected values."""
    expected = {
        "MARKET": "market",
        "SOCIAL": "social",
        "NEWS": "news",
        "FUNDAMENTALS": "fundamentals",
        "POLICY": "policy",
        "HOT_MONEY": "hot_money",
        "LOCKUP": "lockup",
    }
    assert len(AnalystType) == 7
    for name, value in expected.items():
        assert getattr(AnalystType, name).value == value


def test_analyst_choices_and_order_all_7():
    """Verify ANALYST_CHOICES and ANALYST_ORDER cover all 7 analysts with correct labels."""
    assert len(ANALYST_CHOICES) == 7
    assert ANALYST_ORDER == ANALYST_CHOICES

    choice_values = [val for _, val in ANALYST_CHOICES]
    assert choice_values == [
        AnalystType.MARKET,
        AnalystType.SOCIAL,
        AnalystType.NEWS,
        AnalystType.FUNDAMENTALS,
        AnalystType.POLICY,
        AnalystType.HOT_MONEY,
        AnalystType.LOCKUP,
    ]

    labels = [label for label, _ in ANALYST_CHOICES]
    assert any("政策分析师" in label for label in labels)
    assert any("游资追踪师" in label for label in labels)
    assert any("解禁监控师" in label for label in labels)


def test_select_analysts_defaults_all_checked():
    """Verify select_analysts pre-checks all 7 analysts by default."""
    with patch("questionary.checkbox") as mock_checkbox:
        mock_ask = MagicMock(return_value=[val for _, val in ANALYST_CHOICES])
        mock_checkbox.return_value.ask = mock_ask

        result = select_analysts()

        mock_checkbox.assert_called_once()
        _, kwargs = mock_checkbox.call_args
        choices = kwargs.get("choices", [])
        assert len(choices) == 7
        for choice in choices:
            assert getattr(choice, "checked", False) is True
        assert len(result) == 7


def test_message_buffer_mappings():
    """Verify ANALYST_MAPPING and REPORT_SECTIONS cover all 7 analysts."""
    expected_mapping = {
        "market": "Market Analyst",
        "social": "Social Analyst",
        "news": "News Analyst",
        "fundamentals": "Fundamentals Analyst",
        "policy": "Policy Analyst",
        "hot_money": "Hot Money Tracker",
        "lockup": "Lockup Watcher",
    }
    assert MessageBuffer.ANALYST_MAPPING == expected_mapping

    expected_reports = {
        "market_report": ("market", "Market Analyst"),
        "sentiment_report": ("social", "Social Analyst"),
        "news_report": ("news", "News Analyst"),
        "fundamentals_report": ("fundamentals", "Fundamentals Analyst"),
        "policy_report": ("policy", "Policy Analyst"),
        "hot_money_report": ("hot_money", "Hot Money Tracker"),
        "lockup_report": ("lockup", "Lockup Watcher"),
        "investment_plan": (None, "Research Manager"),
        "trader_investment_plan": (None, "Trader"),
        "final_trade_decision": (None, "Portfolio Manager"),
    }
    assert MessageBuffer.REPORT_SECTIONS == expected_reports


def test_message_buffer_init_for_all_7_analysts():
    """Verify MessageBuffer initializes status and sections for all 7 analysts."""
    buffer = MessageBuffer()
    all_7_keys = ["market", "social", "news", "fundamentals", "policy", "hot_money", "lockup"]
    buffer.init_for_analysis(all_7_keys)

    # 7 analysts + 3 research + 1 trader + 3 risk + 1 pm = 15 agents
    expected_agents = [
        "Market Analyst",
        "Social Analyst",
        "News Analyst",
        "Fundamentals Analyst",
        "Policy Analyst",
        "Hot Money Tracker",
        "Lockup Watcher",
        "Bull Researcher",
        "Bear Researcher",
        "Research Manager",
        "Trader",
        "Aggressive Analyst",
        "Neutral Analyst",
        "Conservative Analyst",
        "Portfolio Manager",
    ]
    for agent in expected_agents:
        assert agent in buffer.agent_status
        assert buffer.agent_status[agent] == "pending"

    # All 7 analyst report sections + 3 downstream sections = 10 sections
    expected_sections = [
        "market_report",
        "sentiment_report",
        "news_report",
        "fundamentals_report",
        "policy_report",
        "hot_money_report",
        "lockup_report",
        "investment_plan",
        "trader_investment_plan",
        "final_trade_decision",
    ]
    for section in expected_sections:
        assert section in buffer.report_sections
        assert buffer.report_sections[section] is None


def test_message_buffer_subset_selection():
    """Verify MessageBuffer only sets up sections and status for selected subset."""
    buffer = MessageBuffer()
    subset = ["policy", "lockup"]
    buffer.init_for_analysis(subset)

    assert "Policy Analyst" in buffer.agent_status
    assert "Lockup Watcher" in buffer.agent_status
    assert "Market Analyst" not in buffer.agent_status
    assert "Fundamentals Analyst" not in buffer.agent_status

    assert "policy_report" in buffer.report_sections
    assert "lockup_report" in buffer.report_sections
    assert "market_report" not in buffer.report_sections
    assert "fundamentals_report" not in buffer.report_sections
    # Fixed downstream sections are always present
    assert "investment_plan" in buffer.report_sections
    assert "final_trade_decision" in buffer.report_sections


def test_message_buffer_completed_reports_count():
    """Verify get_completed_reports_count respects content AND finalizing agent status."""
    buffer = MessageBuffer()
    buffer.init_for_analysis(["policy", "hot_money", "lockup"])

    # Initially 0
    assert buffer.get_completed_reports_count() == 0

    # Content added but agent not completed -> still 0
    buffer.update_report_section("policy_report", "Policy text")
    assert buffer.get_completed_reports_count() == 0

    # Agent completed -> now 1
    buffer.update_agent_status("Policy Analyst", "completed")
    assert buffer.get_completed_reports_count() == 1

    # Add hot money
    buffer.update_report_section("hot_money_report", "Hot money text")
    buffer.update_agent_status("Hot Money Tracker", "completed")
    assert buffer.get_completed_reports_count() == 2

    # Add lockup
    buffer.update_report_section("lockup_report", "Lockup text")
    buffer.update_agent_status("Lockup Watcher", "completed")
    assert buffer.get_completed_reports_count() == 3


def test_update_analyst_statuses_7_agents():
    """Verify update_analyst_statuses drives status progression across all 7 analysts."""
    buffer = MessageBuffer()
    all_7 = ["market", "social", "news", "fundamentals", "policy", "hot_money", "lockup"]
    buffer.init_for_analysis(all_7)

    # First update: no reports yet -> Market Analyst becomes in_progress
    update_analyst_statuses(buffer, {})
    assert buffer.agent_status["Market Analyst"] == "in_progress"
    assert buffer.agent_status["Social Analyst"] == "pending"

    # Market report arrives -> Market Analyst completed, Social Analyst in_progress
    update_analyst_statuses(buffer, {"market_report": "market analysis content"})
    assert buffer.agent_status["Market Analyst"] == "completed"
    assert buffer.agent_status["Social Analyst"] == "in_progress"

    # Provide remaining reports up to lockup
    update_analyst_statuses(buffer, {
        "sentiment_report": "sentiment content",
        "news_report": "news content",
        "fundamentals_report": "fundamentals content",
        "policy_report": "policy content",
        "hot_money_report": "hot money content",
    })
    assert buffer.agent_status["Hot Money Tracker"] == "completed"
    assert buffer.agent_status["Lockup Watcher"] == "in_progress"

    # Lockup arrives -> all 7 completed, Bull Researcher becomes in_progress
    update_analyst_statuses(buffer, {"lockup_report": "lockup content"})
    assert buffer.agent_status["Lockup Watcher"] == "completed"
    assert buffer.agent_status["Bull Researcher"] == "in_progress"


def test_message_buffer_report_generation():
    """Verify _update_current_report and _update_final_report handle 3 new A-share analysts."""
    buffer = MessageBuffer()
    buffer.init_for_analysis(["policy", "hot_money", "lockup"])

    buffer.update_report_section("policy_report", "国家出台半导体扶持政策")
    assert "Policy Analysis" in buffer.current_report
    assert "半导体扶持政策" in buffer.current_report

    buffer.update_report_section("hot_money_report", "章盟主席位净买入 5000 万")
    assert "Hot Money Tracking" in buffer.current_report
    assert "章盟主" in buffer.current_report

    buffer.update_report_section("lockup_report", "下周解禁占流通盘 0.2%")
    assert "Lockup Expiry Monitoring" in buffer.current_report
    assert "0.2%" in buffer.current_report

    # Check final report contains all 3 sections
    assert "## Analyst Team Reports" in buffer.final_report
    assert "### Policy Analysis" in buffer.final_report
    assert "### Hot Money Tracking" in buffer.final_report
    assert "### Lockup Expiry Monitoring" in buffer.final_report


def test_save_report_to_disk_and_display(tmp_path):
    """Verify save_report_to_disk saves markdown files for all 7 analysts and display works."""
    state = {
        "market_report": "Market data",
        "sentiment_report": "Social data",
        "news_report": "News data",
        "fundamentals_report": "Fundamentals data",
        "policy_report": "Policy data",
        "hot_money_report": "Hot Money data",
        "lockup_report": "Lockup data",
        "investment_debate_state": {
            "bull_history": "Bull text",
            "bear_history": "Bear text",
            "judge_decision": "Decision text",
        },
        "trader_investment_plan": "Trading plan",
        "risk_debate_state": {
            "aggressive_history": "Aggressive text",
            "conservative_history": "Conservative text",
            "neutral_history": "Neutral text",
            "judge_decision": "Risk decision",
        },
    }

    report_file = save_report_to_disk(state, "600519", tmp_path)
    assert report_file.exists()

    analysts_dir = tmp_path / "1_analysts"
    assert (analysts_dir / "market.md").read_text(encoding="utf-8") == "Market data"
    assert (analysts_dir / "sentiment.md").read_text(encoding="utf-8") == "Social data"
    assert (analysts_dir / "news.md").read_text(encoding="utf-8") == "News data"
    assert (analysts_dir / "fundamentals.md").read_text(encoding="utf-8") == "Fundamentals data"
    assert (analysts_dir / "policy.md").read_text(encoding="utf-8") == "Policy data"
    assert (analysts_dir / "hot_money.md").read_text(encoding="utf-8") == "Hot Money data"
    assert (analysts_dir / "lockup.md").read_text(encoding="utf-8") == "Lockup data"

    # Verify display_complete_report runs without error
    display_complete_report(state)
