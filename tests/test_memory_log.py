"""Tests for TradingMemoryLog — storage, deferred reflection, PM injection, legacy removal."""

import re

import pytest
import pandas as pd
from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.agents.utils.agent_states import AgentState
from tradingagents.agents.schemas import PortfolioDecision, PortfolioRating
from tradingagents.graph.checkpointer import (
    get_checkpointer,
    has_checkpoint,
    thread_id,
)
from tradingagents.graph.conditional_logic import ConditionalLogic
from tradingagents.graph.reflection import Reflector
from tradingagents.graph.setup import GraphSetup
from tradingagents.graph.trading_graph import (
    TradingAgentsGraph,
    _normalize_yfinance_ticker,
    _extract_a_stock_code,
)
from tradingagents.graph.index_graph import TradingAgentsIndexGraph
from tradingagents.graph.propagation import Propagator
from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager

_SEP = TradingMemoryLog._SEPARATOR

DECISION_BUY = "Rating: Buy\nEnter at $189-192, 6% portfolio cap."
DECISION_OVERWEIGHT = (
    "Rating: Overweight\n"
    "Executive Summary: Moderate position, await confirmation.\n"
    "Investment Thesis: Strong fundamentals but near-term headwinds."
)
DECISION_SELL = "Rating: Sell\nExit position immediately."
DECISION_NO_RATING = (
    "Executive Summary: Complex situation with multiple competing factors.\n"
    "Investment Thesis: No clear directional signal at this time."
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def make_log(tmp_path, filename="trading_memory.md"):
    config = {"memory_log_path": str(tmp_path / filename)}
    return TradingMemoryLog(config)


def _seed_completed(tmp_path, ticker, date, decision_text, reflection_text, filename="trading_memory.md"):
    """Write a completed entry directly to file, bypassing the API."""
    entry = (
        f"[{date} | {ticker} | Buy | +1.0% | +0.5% | 5d]\n\n"
        f"DECISION:\n{decision_text}\n\n"
        f"REFLECTION:\n{reflection_text}"
        + _SEP
    )
    with open(tmp_path / filename, "a", encoding="utf-8") as f:
        f.write(entry)


def _resolve_entry(log, ticker, date, decision, reflection="Good call."):
    """Store a decision then immediately resolve it via the API."""
    log.store_decision(ticker, date, decision)
    log.update_with_outcome(ticker, date, 0.05, 0.02, 5, reflection)


def _price_df(prices, start_date="2026-01-05"):
    """Minimal DataFrame matching yfinance .history() output shape.

    yfinance 的 history() 以 DatetimeIndex 返回（A07 起收益计算按索引日期
    对齐股票与基准，fixture 需具备与真实输出一致的形状）。
    """
    dates = pd.date_range(start=start_date, periods=len(prices), freq="D")
    return pd.DataFrame({"Close": prices}, index=dates)


def _native_kline_df(prices, start_date="2026-01-05"):
    """Minimal DataFrame matching native A-stock / index get_history_df output shape."""
    dates = pd.date_range(start=start_date, periods=len(prices), freq="D")
    return pd.DataFrame({
        "Date": dates,
        "Open": prices,
        "High": prices,
        "Low": prices,
        "Close": prices,
        "Volume": [1000] * len(prices),
    })


def _make_pm_state(past_context=""):
    """Minimal AgentState dict for portfolio_manager_node."""
    return {
        "company_of_interest": "NVDA",
        "past_context": past_context,
        "risk_debate_state": {
            "history": "Risk debate history.",
            "aggressive_history": "",
            "conservative_history": "",
            "neutral_history": "",
            "judge_decision": "",
            "current_aggressive_response": "",
            "current_conservative_response": "",
            "current_neutral_response": "",
            "count": 1,
        },
        "market_report": "Market report.",
        "sentiment_report": "Sentiment report.",
        "news_report": "News report.",
        "fundamentals_report": "Fundamentals report.",
        "investment_plan": "Research plan.",
        "trader_investment_plan": "Trader plan.",
    }


def _structured_pm_llm(captured: dict, decision: PortfolioDecision | None = None):
    """Build a MagicMock LLM whose with_structured_output binding captures the
    prompt and returns a real PortfolioDecision (so render_pm_decision works).
    """
    if decision is None:
        decision = PortfolioDecision(
            rating=PortfolioRating.HOLD,
            executive_summary="Hold the position; await catalyst.",
            investment_thesis="Balanced view; neither side carried the debate.",
        )
    structured = MagicMock()
    structured.invoke.side_effect = lambda prompt: (
        captured.__setitem__("prompt", prompt) or decision
    )
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm


# ---------------------------------------------------------------------------
# Core: storage and read path
# ---------------------------------------------------------------------------

class TestTradingMemoryLogCore:

    def test_store_creates_file(self, tmp_path):
        log = make_log(tmp_path)
        assert not (tmp_path / "trading_memory.md").exists()
        log.store_decision("NVDA", "2026-01-10", DECISION_BUY)
        assert (tmp_path / "trading_memory.md").exists()

    def test_store_appends_not_overwrites(self, tmp_path):
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", DECISION_BUY)
        log.store_decision("AAPL", "2026-01-11", DECISION_OVERWEIGHT)
        entries = log.load_entries()
        assert len(entries) == 2
        assert entries[0]["ticker"] == "NVDA"
        assert entries[1]["ticker"] == "AAPL"

    def test_store_decision_idempotent(self, tmp_path):
        """Calling store_decision twice with same (ticker, date) stores only one entry."""
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", DECISION_BUY)
        log.store_decision("NVDA", "2026-01-10", DECISION_BUY)
        assert len(log.load_entries()) == 1

    def test_batch_update_resolves_multiple_entries(self, tmp_path):
        """batch_update_with_outcomes resolves multiple pending entries in one write."""
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-05", DECISION_BUY)
        log.store_decision("NVDA", "2026-01-12", DECISION_SELL)

        updates = [
            {"ticker": "NVDA", "trade_date": "2026-01-05",
             "raw_return": 0.05, "alpha_return": 0.02, "holding_days": 5,
             "reflection": "First correct."},
            {"ticker": "NVDA", "trade_date": "2026-01-12",
             "raw_return": -0.03, "alpha_return": -0.01, "holding_days": 5,
             "reflection": "Second correct."},
        ]
        log.batch_update_with_outcomes(updates)

        entries = log.load_entries()
        assert len(entries) == 2
        assert all(not e["pending"] for e in entries)
        assert entries[0]["reflection"] == "First correct."
        assert entries[1]["reflection"] == "Second correct."

    def test_pending_tag_format(self, tmp_path):
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", DECISION_BUY)
        text = (tmp_path / "trading_memory.md").read_text(encoding="utf-8")
        assert "[2026-01-10 | NVDA | Buy | pending]" in text

    # Rating parsing

    def test_rating_parsed_buy(self, tmp_path):
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", DECISION_BUY)
        assert log.load_entries()[0]["rating"] == "Buy"

    def test_rating_parsed_overweight(self, tmp_path):
        log = make_log(tmp_path)
        log.store_decision("AAPL", "2026-01-11", DECISION_OVERWEIGHT)
        assert log.load_entries()[0]["rating"] == "Overweight"

    def test_rating_fallback_hold(self, tmp_path):
        log = make_log(tmp_path)
        log.store_decision("MSFT", "2026-01-12", DECISION_NO_RATING)
        assert log.load_entries()[0]["rating"] == "Hold"

    def test_rating_priority_over_prose(self, tmp_path):
        """'Rating: X' label wins even when an opposing rating word appears earlier in prose."""
        decision = (
            "The sell thesis is weak. The hold case is marginal.\n\n"
            "Rating: Buy\n\n"
            "Executive Summary: Strong fundamentals support the position."
        )
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", decision)
        assert log.load_entries()[0]["rating"] == "Buy"

    # Delimiter robustness

    def test_decision_with_markdown_separator(self, tmp_path):
        """LLM decision containing '---' must not corrupt the entry."""
        decision = "Rating: Buy\n\n---\n\nRisk: elevated volatility."
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", decision)
        entries = log.load_entries()
        assert len(entries) == 1
        assert "Risk: elevated volatility" in entries[0]["decision"]

    # load_entries

    def test_load_entries_empty_file(self, tmp_path):
        log = make_log(tmp_path)
        assert log.load_entries() == []

    def test_load_entries_single(self, tmp_path):
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", DECISION_BUY)
        entries = log.load_entries()
        assert len(entries) == 1
        e = entries[0]
        assert e["date"] == "2026-01-10"
        assert e["ticker"] == "NVDA"
        assert e["rating"] == "Buy"
        assert e["pending"] is True
        assert e["raw"] is None

    def test_load_entries_multiple(self, tmp_path):
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", DECISION_BUY)
        log.store_decision("AAPL", "2026-01-11", DECISION_OVERWEIGHT)
        log.store_decision("MSFT", "2026-01-12", DECISION_NO_RATING)
        entries = log.load_entries()
        assert len(entries) == 3
        assert [e["ticker"] for e in entries] == ["NVDA", "AAPL", "MSFT"]

    def test_decision_content_preserved(self, tmp_path):
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", DECISION_BUY)
        assert log.load_entries()[0]["decision"] == DECISION_BUY.strip()

    # get_pending_entries

    def test_get_pending_returns_pending_only(self, tmp_path):
        log = make_log(tmp_path)
        _seed_completed(tmp_path, "NVDA", "2026-01-05", "Buy NVDA.", "Correct.")
        log.store_decision("NVDA", "2026-01-10", DECISION_BUY)
        pending = log.get_pending_entries()
        assert len(pending) == 1
        assert pending[0]["ticker"] == "NVDA"
        assert pending[0]["date"] == "2026-01-10"

    # get_past_context

    def test_get_past_context_empty(self, tmp_path):
        log = make_log(tmp_path)
        assert log.get_past_context("NVDA") == ""

    def test_get_past_context_pending_excluded(self, tmp_path):
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", DECISION_BUY)
        assert log.get_past_context("NVDA") == ""

    def test_get_past_context_same_ticker(self, tmp_path):
        log = make_log(tmp_path)
        _seed_completed(tmp_path, "NVDA", "2026-01-05", "Buy NVDA — AI capex thesis intact.", "Directionally correct.")
        ctx = log.get_past_context("NVDA")
        assert "Past analyses of NVDA" in ctx
        assert "Buy NVDA" in ctx

    def test_get_past_context_cross_ticker(self, tmp_path):
        log = make_log(tmp_path)
        _seed_completed(tmp_path, "AAPL", "2026-01-05", "Buy AAPL — Services growth.", "Correct.")
        ctx = log.get_past_context("NVDA")
        assert "Recent cross-ticker lessons" in ctx
        assert "Past analyses of NVDA" not in ctx

    def test_n_same_limit_respected(self, tmp_path):
        """Only the n_same most recent same-ticker entries are included."""
        log = make_log(tmp_path)
        for i in range(6):
            _seed_completed(tmp_path, "NVDA", f"2026-01-{i+1:02d}", f"Buy entry {i}.", "Correct.")
        ctx = log.get_past_context("NVDA", n_same=5)
        assert "Buy entry 0" not in ctx
        assert "Buy entry 5" in ctx

    def test_n_cross_limit_respected(self, tmp_path):
        """Only the n_cross most recent cross-ticker entries are included."""
        log = make_log(tmp_path)
        for i, ticker in enumerate(["AAPL", "MSFT", "GOOG", "META"]):
            _seed_completed(tmp_path, ticker, f"2026-01-{i+1:02d}", f"Buy {ticker}.", "Correct.")
        ctx = log.get_past_context("NVDA", n_cross=3)
        assert "AAPL" not in ctx
        assert "META" in ctx

    # No-op when config is None

    def test_no_log_path_is_noop(self):
        log = TradingMemoryLog(config=None)
        log.store_decision("NVDA", "2026-01-10", DECISION_BUY)
        assert log.load_entries() == []
        assert log.get_past_context("NVDA") == ""

    # Rotation: opt-in cap on resolved entries

    def test_rotation_disabled_by_default(self, tmp_path):
        """Without max_entries, all resolved entries are kept."""
        log = make_log(tmp_path)
        for i in range(7):
            _resolve_entry(log, "NVDA", f"2026-01-{i+1:02d}", DECISION_BUY, f"Lesson {i}.")
        assert len(log.load_entries()) == 7

    def test_rotation_prunes_oldest_resolved(self, tmp_path):
        """When max_entries is set and exceeded, oldest resolved entries are pruned."""
        log = TradingMemoryLog({
            "memory_log_path": str(tmp_path / "trading_memory.md"),
            "memory_log_max_entries": 3,
        })
        # Resolve 5 entries; rotation should keep only the 3 most recent.
        for i in range(5):
            _resolve_entry(log, "NVDA", f"2026-01-{i+1:02d}", DECISION_BUY, f"Lesson {i}.")
        entries = log.load_entries()
        assert len(entries) == 3
        # Confirm the OLDEST were dropped, not the newest.
        dates = [e["date"] for e in entries]
        assert dates == ["2026-01-03", "2026-01-04", "2026-01-05"]

    def test_rotation_never_prunes_pending(self, tmp_path):
        """Pending entries (unresolved) are kept regardless of the cap."""
        log = TradingMemoryLog({
            "memory_log_path": str(tmp_path / "trading_memory.md"),
            "memory_log_max_entries": 2,
        })
        # 3 resolved + 2 pending. With cap=2, only 2 resolved survive; both pending stay.
        for i in range(3):
            _resolve_entry(log, "NVDA", f"2026-01-{i+1:02d}", DECISION_BUY, f"Resolved {i}.")
        log.store_decision("NVDA", "2026-02-01", DECISION_BUY)
        log.store_decision("NVDA", "2026-02-02", DECISION_OVERWEIGHT)
        # Trigger rotation by resolving one more entry — pending entries must stay.
        _resolve_entry(log, "NVDA", "2026-01-04", DECISION_BUY, "Resolved 3.")
        entries = log.load_entries()
        pending = [e for e in entries if e["pending"]]
        resolved = [e for e in entries if not e["pending"]]
        assert len(pending) == 2, "pending entries must never be pruned"
        assert len(resolved) == 2, f"expected 2 resolved after rotation, got {len(resolved)}"

    def test_rotation_under_cap_is_noop(self, tmp_path):
        """No rotation when resolved count <= max_entries."""
        log = TradingMemoryLog({
            "memory_log_path": str(tmp_path / "trading_memory.md"),
            "memory_log_max_entries": 10,
        })
        for i in range(3):
            _resolve_entry(log, "NVDA", f"2026-01-{i+1:02d}", DECISION_BUY, f"Lesson {i}.")
        assert len(log.load_entries()) == 3

    # Rating parsing: markdown bold and numbered list formats

    def test_rating_parsed_from_bold_markdown(self, tmp_path):
        """**Rating**: Buy — markdown bold around the label must not prevent parsing."""
        decision = "**Rating**: Buy\nEnter at $190."
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", decision)
        assert log.load_entries()[0]["rating"] == "Buy"

    def test_rating_parsed_from_bold_value(self, tmp_path):
        """Rating: **Sell** — markdown bold around the value must not prevent parsing."""
        decision = "Rating: **Sell**\nExit immediately."
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", decision)
        assert log.load_entries()[0]["rating"] == "Sell"

    def test_rating_label_wins_over_prose_with_markdown(self, tmp_path):
        """Rating: **Sell** must win even when prose contains a conflicting rating word."""
        decision = (
            "The buy thesis is weakened by guidance.\n"
            "Rating: **Sell**\n"
            "Exit before earnings."
        )
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", decision)
        assert log.load_entries()[0]["rating"] == "Sell"

    def test_rating_parsed_from_numbered_list(self, tmp_path):
        """1. Rating: Buy — numbered list prefix must not prevent parsing."""
        decision = "1. Rating: Buy\nEnter at $190."
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", decision)
        assert log.load_entries()[0]["rating"] == "Buy"


# ---------------------------------------------------------------------------
# Deferred reflection: update_with_outcome, Reflector, _fetch_returns
# ---------------------------------------------------------------------------

class TestDeferredReflection:

    # Yahoo Finance ticker normalization

    def test_normalize_yfinance_ticker_adds_mainland_exchange_suffix(self):
        assert _normalize_yfinance_ticker("600519") == "600519.SS"
        assert _normalize_yfinance_ticker("000001") == "000001.SZ"
        assert _normalize_yfinance_ticker("688017") == "688017.SS"

    def test_normalize_yfinance_ticker_preserves_qualified_symbols(self):
        assert _normalize_yfinance_ticker("600519.SS") == "600519.SS"
        assert _normalize_yfinance_ticker("600519.SH") == "600519.SS"
        assert _normalize_yfinance_ticker("SH600519") == "600519.SS"
        assert _normalize_yfinance_ticker("NVDA") == "NVDA"

    def test_normalize_yfinance_ticker_does_not_invent_beijing_suffix(self):
        assert _normalize_yfinance_ticker("830799") == "830799"
        assert _normalize_yfinance_ticker("920002") == "920002"
        assert _normalize_yfinance_ticker("BJ830799") == "830799"
        assert _normalize_yfinance_ticker("830799.BJ") == "830799"

    def test_extract_a_stock_code(self):
        assert _extract_a_stock_code("600519") == "600519"
        assert _extract_a_stock_code("688017") == "688017"
        assert _extract_a_stock_code("000001") == "000001"
        assert _extract_a_stock_code("300750") == "300750"
        assert _extract_a_stock_code("830799") == "830799"
        assert _extract_a_stock_code("920002") == "920002"
        assert _extract_a_stock_code("430047") == "430047"
        assert _extract_a_stock_code("600519.SH") == "600519"
        assert _extract_a_stock_code("600519.SS") == "600519"
        assert _extract_a_stock_code("SH600519") == "600519"
        assert _extract_a_stock_code("000001.SZ") == "000001"
        assert _extract_a_stock_code("SZ000001") == "000001"
        assert _extract_a_stock_code("830799.BJ") == "830799"
        assert _extract_a_stock_code("BJ830799") == "830799"
        assert _extract_a_stock_code("NVDA") is None
        assert _extract_a_stock_code("AAPL") is None
        assert _extract_a_stock_code("00700.HK") is None
        assert _extract_a_stock_code("XXXXXFAKE") is None
        assert _extract_a_stock_code("12345") is None

    # update_with_outcome

    def test_update_replaces_pending_tag(self, tmp_path):
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", DECISION_BUY)
        log.update_with_outcome("NVDA", "2026-01-10", 0.042, 0.021, 5, "Momentum confirmed.")
        text = (tmp_path / "trading_memory.md").read_text(encoding="utf-8")
        assert "[2026-01-10 | NVDA | Buy | pending]" not in text
        assert "+4.2%" in text
        assert "+2.1%" in text
        assert "5d" in text

    def test_update_appends_reflection(self, tmp_path):
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", DECISION_BUY)
        log.update_with_outcome("NVDA", "2026-01-10", 0.042, 0.021, 5, "Momentum confirmed.")
        entries = log.load_entries()
        assert len(entries) == 1
        e = entries[0]
        assert e["pending"] is False
        assert e["reflection"] == "Momentum confirmed."
        assert e["decision"] == DECISION_BUY.strip()

    def test_update_preserves_other_entries(self, tmp_path):
        """Only the matching entry is modified; all other entries remain unchanged."""
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", DECISION_BUY)
        log.store_decision("AAPL", "2026-01-11", "Rating: Hold\nHold AAPL.")
        log.store_decision("MSFT", "2026-01-12", DECISION_SELL)
        log.update_with_outcome("AAPL", "2026-01-11", 0.01, -0.01, 5, "Neutral result.")
        entries = log.load_entries()
        assert len(entries) == 3
        nvda, aapl, msft = entries
        assert nvda["ticker"] == "NVDA" and nvda["pending"] is True
        assert aapl["ticker"] == "AAPL" and aapl["pending"] is False
        assert aapl["reflection"] == "Neutral result."
        assert msft["ticker"] == "MSFT" and msft["pending"] is True

    def test_update_atomic_write(self, tmp_path):
        """原子写：唯一临时文件（pid+tid 后缀）写后即被 replace，无残留。

        A06 起 .tmp.{pid}.{tid} 的唯一命名取代固定 .tmp——stale 的固定名
        文件不再被触碰（也不与新事务冲突）；断言相应改为「事务自身不遗留
        临时文件 + 更新正确」。
        """
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", DECISION_BUY)
        stale_tmp = tmp_path / "trading_memory.tmp"
        stale_tmp.write_text("GARBAGE CONTENT — should be overwritten", encoding="utf-8")
        log.update_with_outcome("NVDA", "2026-01-10", 0.042, 0.021, 5, "Correct.")
        leftovers = [
            p for p in tmp_path.iterdir()
            if p.name.startswith("trading_memory.tmp")
            and p.suffix not in ("", ".md", ".lock")
            and ".tmp." in p.name
        ]
        assert not leftovers, f"事务遗留了临时文件: {leftovers}"
        entries = log.load_entries()
        assert len(entries) == 1
        assert entries[0]["reflection"] == "Correct."
        assert entries[0]["pending"] is False

    def test_update_noop_when_no_log_path(self):
        log = TradingMemoryLog(config=None)
        log.update_with_outcome("NVDA", "2026-01-10", 0.05, 0.02, 5, "Reflection")

    def test_formatting_roundtrip_after_update(self, tmp_path):
        """All fields intact and blank line between tag and DECISION preserved after update."""
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", DECISION_BUY)
        log.update_with_outcome("NVDA", "2026-01-10", 0.042, 0.021, 5, "Momentum confirmed.")
        entries = log.load_entries()
        assert len(entries) == 1
        e = entries[0]
        assert e["pending"] is False
        assert e["decision"] == DECISION_BUY.strip()
        assert e["reflection"] == "Momentum confirmed."
        assert e["raw"] == "+4.2%"
        assert e["alpha"] == "+2.1%"
        assert e["holding"] == "5d"
        raw_text = (tmp_path / "trading_memory.md").read_text(encoding="utf-8")
        # R4 起 resolved tag 追加回填日期元数据（无 outcome_end 时省略 end=）
        assert re.search(
            r"\[2026-01-10 \| NVDA \| Buy \| \+4\.2% \| \+2\.1% \| 5d \| resolved=\d{4}-\d{2}-\d{2}\]\n\nDECISION:",
            raw_text,
        )

    # Reflector.reflect_on_final_decision

    def test_reflect_on_final_decision_returns_llm_output(self):
        mock_llm = MagicMock()
        mock_llm.invoke.return_value.content = "Directionally correct. Thesis confirmed."
        reflector = Reflector(mock_llm)
        result = reflector.reflect_on_final_decision(
            final_decision=DECISION_BUY, raw_return=0.042, alpha_return=0.021
        )
        assert result == "Directionally correct. Thesis confirmed."
        mock_llm.invoke.assert_called_once()

    def test_reflect_on_final_decision_includes_returns_in_prompt(self):
        """Return figures are present in the human message sent to the LLM."""
        mock_llm = MagicMock()
        mock_llm.invoke.return_value.content = "Incorrect call."
        reflector = Reflector(mock_llm)
        reflector.reflect_on_final_decision(
            final_decision=DECISION_SELL, raw_return=-0.08, alpha_return=-0.05
        )
        messages = mock_llm.invoke.call_args[0][0]
        human_content = next(content for role, content in messages if role == "human")
        assert "-8.0%" in human_content
        assert "-5.0%" in human_content
        assert "Exit position immediately." in human_content

    # TradingAgentsGraph._fetch_returns

    def test_fetch_returns_valid_astock_native(self):
        stock_prices = [100.0, 102.0, 104.0, 103.0, 105.0, 106.0]
        bench_prices = [4000.0, 4020.0, 4040.0, 4030.0, 4050.0, 4060.0]
        mock_graph = MagicMock(spec=TradingAgentsGraph)
        with patch("tradingagents.dataflows.a_stock.get_astock_history_df", return_value=_native_kline_df(stock_prices)), \
             patch("tradingagents.dataflows.index_data.get_index_history_df", return_value=_native_kline_df(bench_prices)), \
             patch("yfinance.Ticker") as mock_yf:
            raw, alpha, days, window_end = TradingAgentsGraph._fetch_returns(mock_graph, "688017", "2026-01-05")
            mock_yf.assert_not_called()
        assert raw is not None and alpha is not None and days is not None
        assert isinstance(raw, float) and isinstance(alpha, float) and isinstance(days, int)
        assert days == 5
        assert round(raw, 4) == 0.06
        assert round(bench_prices[-1] / bench_prices[0] - 1, 4) == 0.015
        assert round(alpha, 4) == round(0.06 - 0.015, 4)

    def test_fetch_returns_beijing_stock_exchange_supported(self):
        """Beijing Stock Exchange stocks (920xxx, 83xxxx, 43xxxx, etc.) resolve via native pipeline."""
        stock_prices = [10.0, 10.5, 11.0, 11.5, 12.0, 12.5]
        bench_prices = [4000.0, 4020.0, 4040.0, 4030.0, 4050.0, 4060.0]
        mock_graph = MagicMock(spec=TradingAgentsGraph)
        with patch("tradingagents.dataflows.a_stock.get_astock_history_df", return_value=_native_kline_df(stock_prices)), \
             patch("tradingagents.dataflows.index_data.get_index_history_df", return_value=_native_kline_df(bench_prices)):
            for bse_ticker in ("920002", "830799", "430047", "830799.BJ", "BJ920002"):
                raw, alpha, days, window_end = TradingAgentsGraph._fetch_returns(mock_graph, bse_ticker, "2026-01-05")
                assert raw is not None, f"BSE ticker {bse_ticker} must resolve outcome"
                assert days == 5
                assert round(raw, 4) == 0.25

    def test_fetch_returns_bse_returns_none_when_no_native_data(self):
        """BSE stocks return (None, None, None, None) safely if native data is unavailable."""
        mock_graph = MagicMock(spec=TradingAgentsGraph)
        with patch("tradingagents.dataflows.a_stock.get_astock_history_df", return_value=pd.DataFrame()), \
             patch("tradingagents.dataflows.index_data.get_index_history_df", return_value=pd.DataFrame()):
            raw, alpha, days, window_end = TradingAgentsGraph._fetch_returns(mock_graph, "920002", "2026-01-05")
            assert raw is None and alpha is None and days is None and window_end is None

    def test_fetch_returns_uses_exchange_qualified_astock_symbol_on_yfinance_fallback(self):
        mock_graph = MagicMock(spec=TradingAgentsGraph)
        bench_prices = [4000.0, 4020.0, 4040.0, 4030.0, 4050.0, 4060.0]
        stock_prices = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
        with patch("tradingagents.dataflows.a_stock.get_astock_history_df", return_value=pd.DataFrame()), \
             patch("tradingagents.dataflows.index_data.get_index_history_df", return_value=pd.DataFrame()), \
             patch("yfinance.Ticker") as mock_ticker_cls:
            def _make_ticker(sym):
                m = MagicMock()
                m.history.return_value = _price_df(bench_prices if sym == "000300.SS" else stock_prices)
                return m
            mock_ticker_cls.side_effect = _make_ticker
            raw, alpha, days, window_end = TradingAgentsGraph._fetch_returns(mock_graph, "600519", "2026-01-05")

        assert mock_ticker_cls.call_args_list[0].args == ("600519.SS",)
        assert raw is not None and alpha is not None and days == 5

    def test_fetch_returns_too_recent(self):
        """Only 1 data point available → returns all-None tuple, no crash."""
        mock_graph = MagicMock(spec=TradingAgentsGraph)
        with patch("yfinance.Ticker") as mock_ticker_cls:
            m = MagicMock()
            m.history.return_value = _price_df([100.0])
            mock_ticker_cls.return_value = m
            raw, alpha, days, window_end = TradingAgentsGraph._fetch_returns(mock_graph, "NVDA", "2026-04-19")
        assert raw is None and alpha is None and days is None and window_end is None

    def test_fetch_returns_delisted(self):
        """Empty DataFrame → returns all-None tuple, no crash."""
        mock_graph = MagicMock(spec=TradingAgentsGraph)
        with patch("yfinance.Ticker") as mock_ticker_cls:
            m = MagicMock()
            m.history.return_value = pd.DataFrame({"Close": []})
            mock_ticker_cls.return_value = m
            raw, alpha, days, window_end = TradingAgentsGraph._fetch_returns(mock_graph, "XXXXXFAKE", "2026-01-10")
        assert raw is None and alpha is None and days is None and window_end is None

    def test_fetch_returns_benchmark_shorter_than_stock(self):
        """A07/A08：基准缺少对齐端点 → 全 None 保持 pending，绝不缩短窗口。

        旧行为（min() 缩短到 2 日并立即结算）已按审核 A07/A08 修正为正确
        行为：目标证券窗口已满 5 日，但基准在结束日（2026-01-10）无数据，
        无法按相同日期对齐 → 保持 pending。
        """
        stock_prices = [100.0, 102.0, 104.0, 103.0, 105.0, 106.0]
        bench_prices = [4000.0, 4020.0, 4030.0]
        mock_graph = MagicMock(spec=TradingAgentsGraph)
        with patch("tradingagents.dataflows.a_stock.get_astock_history_df", return_value=_native_kline_df(stock_prices)), \
             patch("tradingagents.dataflows.index_data.get_index_history_df", return_value=_native_kline_df(bench_prices)):
            raw, alpha, days, window_end = TradingAgentsGraph._fetch_returns(mock_graph, "688017", "2026-01-05")
        assert raw is None and alpha is None and days is None and window_end is None

    def test_fetch_returns_index_graph_switches_benchmark_for_csi300(self):
        """Index graph analyzing 000300.SH uses 000001.SH as benchmark."""
        index_prices = [4000.0, 4020.0, 4040.0, 4030.0, 4050.0, 4060.0]
        sse_prices = [3200.0, 3210.0, 3220.0, 3215.0, 3225.0, 3230.0]
        mock_index_graph = MagicMock(spec=TradingAgentsIndexGraph)

        def mock_get_index(ticker, **kwargs):
            if "000001" in str(ticker):
                return _native_kline_df(sse_prices)
            return _native_kline_df(index_prices)

        with patch("tradingagents.dataflows.index_data.get_index_history_df", side_effect=mock_get_index):
            raw, alpha, days, window_end = TradingAgentsIndexGraph._fetch_returns(mock_index_graph, "000300.SH", "2026-01-05")
        assert raw is not None and alpha is not None and days == 5
        assert round(raw, 4) == 0.015
        assert round(alpha, 4) == round(0.015 - (3230.0 / 3200.0 - 1), 4)

    def test_fetch_returns_index_graph_uses_csi300_for_other_indices(self):
        """Index graph analyzing other indices (399006.SZ) uses 000300.SH as benchmark."""
        chinext_prices = [2000.0, 2020.0, 2040.0, 2030.0, 2050.0, 2060.0]
        csi300_prices = [4000.0, 4010.0, 4020.0, 4015.0, 4025.0, 4030.0]
        mock_index_graph = MagicMock(spec=TradingAgentsIndexGraph)

        def mock_get_index(ticker, **kwargs):
            if "000300" in str(ticker):
                return _native_kline_df(csi300_prices)
            return _native_kline_df(chinext_prices)

        with patch("tradingagents.dataflows.index_data.get_index_history_df", side_effect=mock_get_index):
            raw, alpha, days, window_end = TradingAgentsIndexGraph._fetch_returns(mock_index_graph, "399006.SZ", "2026-01-05")
        assert raw is not None and alpha is not None and days == 5
        assert round(raw, 4) == 0.03
        assert round(alpha, 4) == round(0.03 - (4030.0 / 4000.0 - 1), 4)

    # TradingAgentsGraph._resolve_pending_entries

    def test_resolve_skips_other_tickers(self, tmp_path):
        """Pending AAPL entry is not resolved when the run is for NVDA."""
        log = make_log(tmp_path)
        log.store_decision("AAPL", "2026-01-10", DECISION_BUY)
        mock_graph = MagicMock(spec=TradingAgentsGraph)
        mock_graph.memory_log = log
        mock_graph._fetch_returns = MagicMock(return_value=(0.05, 0.02, 5, "2026-01-12"))
        TradingAgentsGraph._resolve_pending_entries(mock_graph, "NVDA")
        mock_graph._fetch_returns.assert_not_called()
        assert len(log.get_pending_entries()) == 1

    def test_resolve_marks_entry_completed(self, tmp_path):
        """After resolve, get_pending_entries() is empty and the entry has a REFLECTION."""
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-05", DECISION_BUY)
        mock_reflector = MagicMock()
        mock_reflector.reflect_on_final_decision.return_value = "Momentum confirmed."
        mock_graph = MagicMock(spec=TradingAgentsGraph)
        mock_graph.memory_log = log
        mock_graph.reflector = mock_reflector
        mock_graph._fetch_returns = MagicMock(return_value=(0.05, 0.02, 5, "2026-01-12"))
        TradingAgentsGraph._resolve_pending_entries(mock_graph, "NVDA")
        assert log.get_pending_entries() == []
        entries = log.load_entries()
        assert len(entries) == 1
        assert entries[0]["pending"] is False
        assert entries[0]["reflection"] == "Momentum confirmed."
        assert "+5.0%" in entries[0]["raw"]
        assert "+2.0%" in entries[0]["alpha"]


# ---------------------------------------------------------------------------
# Portfolio Manager injection: past_context in state and prompt
# ---------------------------------------------------------------------------

class TestPortfolioManagerInjection:

    # past_context in initial state

    def test_past_context_in_initial_state(self):
        propagator = Propagator()
        state = propagator.create_initial_state("NVDA", "2026-01-10", past_context="some context")
        assert "past_context" in state
        assert state["past_context"] == "some context"

    def test_past_context_defaults_to_empty(self):
        propagator = Propagator()
        state = propagator.create_initial_state("NVDA", "2026-01-10")
        assert state["past_context"] == ""

    # PM prompt

    def test_pm_prompt_includes_past_context(self):
        captured = {}
        llm = _structured_pm_llm(captured)
        pm_node = create_portfolio_manager(llm)
        state = _make_pm_state(past_context="[2026-01-05 | NVDA | Buy | +5.0% | +2.0% | 5d]\nGreat call.")
        pm_node(state)
        assert "Lessons from prior decisions and outcomes" in captured["prompt"]
        assert "Great call." in captured["prompt"]

    def test_pm_no_past_context_no_section(self):
        """PM prompt omits the lessons section entirely when past_context is empty."""
        captured = {}
        llm = _structured_pm_llm(captured)
        pm_node = create_portfolio_manager(llm)
        state = _make_pm_state(past_context="")
        pm_node(state)
        assert "Lessons from prior decisions" not in captured["prompt"]

    def test_pm_returns_rendered_markdown_with_rating(self):
        """The structured PortfolioDecision is rendered to markdown that
        downstream consumers (memory log, signal processor, CLI display)
        can parse without any extra LLM call."""
        captured = {}
        decision = PortfolioDecision(
            rating=PortfolioRating.OVERWEIGHT,
            executive_summary="Build position gradually over the next two weeks.",
            investment_thesis="AI capex cycle remains intact; institutional flows constructive.",
            time_horizon="3-6 months",
        )
        llm = _structured_pm_llm(captured, decision)
        pm_node = create_portfolio_manager(llm)
        result = pm_node(_make_pm_state())
        md = result["final_trade_decision"]
        assert "**Rating**: Overweight" in md
        assert "**Executive Summary**: Build position gradually" in md
        assert "**Investment Thesis**: AI capex cycle" in md
        # 框架不产出目标价——渲染里永远不该出现这一节。
        assert "Price Target" not in md
        assert "**Time Horizon**: 3-6 months" in md

    def test_pm_falls_back_to_freetext_when_structured_unavailable(self):
        """If a provider does not support with_structured_output, the agent
        falls back to a plain invoke and returns whatever prose the model
        produced, so the pipeline never blocks."""
        plain_response = "**Rating**: Sell\n\nExit ahead of guidance."
        llm = MagicMock()
        llm.with_structured_output.side_effect = NotImplementedError("provider unsupported")
        llm.invoke.return_value = MagicMock(content=plain_response)
        pm_node = create_portfolio_manager(llm)
        result = pm_node(_make_pm_state())
        assert result["final_trade_decision"] == plain_response

    # get_past_context ordering and limits

    def test_same_ticker_prioritised(self, tmp_path):
        """Same-ticker entries in same-ticker section; cross-ticker entries in cross-ticker section."""
        log = make_log(tmp_path)
        _resolve_entry(log, "NVDA", "2026-01-05", DECISION_BUY, "Momentum confirmed.")
        _resolve_entry(log, "AAPL", "2026-01-06", DECISION_SELL, "Overvalued.")
        result = log.get_past_context("NVDA")
        assert "Past analyses of NVDA" in result
        assert "Recent cross-ticker lessons" in result
        same_block, cross_block = result.split("Recent cross-ticker lessons")
        assert "NVDA" in same_block
        assert "AAPL" in cross_block

    def test_cross_ticker_reflection_only(self, tmp_path):
        """Cross-ticker entries show only the REFLECTION text, not the full DECISION."""
        log = make_log(tmp_path)
        _resolve_entry(log, "AAPL", "2026-01-06", DECISION_SELL, "Overvalued correction.")
        result = log.get_past_context("NVDA")
        assert "Overvalued correction." in result
        assert "Exit position immediately." not in result

    def test_n_same_limit_respected(self, tmp_path):
        """More than 5 same-ticker completed entries → only 5 injected."""
        log = make_log(tmp_path)
        for i in range(7):
            _resolve_entry(log, "NVDA", f"2026-01-{i+1:02d}", DECISION_BUY, f"Lesson {i}.")
        result = log.get_past_context("NVDA", n_same=5)
        lessons_present = sum(1 for i in range(7) if f"Lesson {i}." in result)
        assert lessons_present == 5

    def test_n_cross_limit_respected(self, tmp_path):
        """More than 3 cross-ticker completed entries → only 3 injected."""
        log = make_log(tmp_path)
        tickers = ["AAPL", "MSFT", "TSLA", "AMZN", "GOOG"]
        for i, ticker in enumerate(tickers):
            _resolve_entry(log, ticker, f"2026-01-{i+1:02d}", DECISION_BUY, f"{ticker} lesson.")
        result = log.get_past_context("NVDA", n_cross=3)
        cross_count = sum(result.count(f"{t} lesson.") for t in tickers)
        assert cross_count == 3

    # Full A→B→C integration cycle

    def test_full_cycle_store_resolve_inject(self, tmp_path):
        """store pending → resolve with outcome → past_context non-empty for PM."""
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-05", DECISION_BUY)
        assert len(log.get_pending_entries()) == 1
        assert log.get_past_context("NVDA") == ""
        log.update_with_outcome("NVDA", "2026-01-05", 0.05, 0.02, 5, "Correct call.")
        assert log.get_pending_entries() == []
        past_ctx = log.get_past_context("NVDA")
        assert past_ctx != ""
        assert "NVDA" in past_ctx
        assert "Correct call." in past_ctx
        assert "DECISION:" in past_ctx
        assert "REFLECTION:" in past_ctx


# ---------------------------------------------------------------------------
# R4: as_of（分析时点）过滤——历史分析不得使用未来记忆
# ---------------------------------------------------------------------------


class TestAsOfContextFiltering:
    """get_past_context(as_of=...) 必须只注入分析时点当时已可知的内容。

    时间语义：as_of 视为该交易日收盘后的时点——当日决策与当日收盘已可知的
    收益（outcome end == as_of）都可见；跨过 as_of 的收益窗口属于未来信息。
    """

    def _seed_resolved(self, tmp_path, ticker, date, decision, reflection,
                       raw="+20.0%", alpha="+5.0%", holding="5d",
                       end=None, resolved=None, filename="trading_memory.md"):
        meta = ""
        if end:
            meta += f" | end={end}"
        if resolved:
            meta += f" | resolved={resolved}"
        entry = (
            f"[{date} | {ticker} | Buy | {raw} | {alpha} | {holding}{meta}]\n\n"
            f"DECISION:\n{decision}\n\n"
            f"REFLECTION:\n{reflection}"
            + _SEP
        )
        with open(tmp_path / filename, "a", encoding="utf-8") as f:
            f.write(entry)

    def test_as_of_excludes_future_decision_and_outcome(self, tmp_path):
        """分析一月份时，不返回八月份的决策、收益和复盘。"""
        log = make_log(tmp_path)
        self._seed_resolved(
            tmp_path, "600519", "2026-08-01", "August buy decision.",
            "August reflection lesson.", end="2026-08-10", resolved="2026-09-01",
        )

        ctx = log.get_past_context("600519", as_of="2026-01-15")

        assert ctx == "", f"分析 2026-01-15 不应看到 8 月记忆，实际:\n{ctx}"

    def test_as_of_includes_provably_available_history(self, tmp_path):
        """有充分时间证据（end ≤ as_of）且当时已可用的经验仍能正常注入。"""
        log = make_log(tmp_path)
        self._seed_resolved(
            tmp_path, "600519", "2025-12-01", "December buy decision.",
            "December reflection lesson.", end="2025-12-10", resolved="2025-12-11",
        )

        ctx = log.get_past_context("600519", as_of="2026-01-15")

        assert "December buy decision." in ctx
        assert "December reflection lesson." in ctx
        assert "+20.0%" in ctx, "已可知的收益应完整注入"

    def test_decision_visible_but_future_outcome_hidden(self, tmp_path):
        """决策在分析日前、收益结束日在分析日后：不泄漏未来收益或复盘。"""
        log = make_log(tmp_path)
        self._seed_resolved(
            tmp_path, "600519", "2026-01-10", "Mid-January decision.",
            "Future reflection must not leak.", end="2026-01-20", resolved="2026-01-21",
        )

        ctx = log.get_past_context("600519", as_of="2026-01-15")

        assert "Mid-January decision." in ctx, "决策本身在分析日前，应可见"
        assert "Future reflection must not leak." not in ctx, "未来复盘泄漏"
        assert "+20.0%" not in ctx, "未来收益泄漏"

    def test_same_day_boundary_counts_as_known(self, tmp_path):
        """收益窗口在 as_of 当日结束（收盘后语义）：收益与复盘可见。"""
        log = make_log(tmp_path)
        self._seed_resolved(
            tmp_path, "600519", "2026-01-08", "Decision text.",
            "Same-day reflection.", end="2026-01-15", resolved="2026-01-15",
        )

        ctx = log.get_past_context("600519", as_of="2026-01-15")

        assert "Decision text." in ctx
        assert "Same-day reflection." in ctx
        assert "+20.0%" in ctx

    def test_cross_ticker_respects_as_of(self, tmp_path):
        """跨标的经验同样遵守时点限制。"""
        log = make_log(tmp_path)
        self._seed_resolved(
            tmp_path, "000858", "2026-08-01", "Other ticker august decision.",
            "Other ticker august lesson.", end="2026-08-10", resolved="2026-09-01",
        )
        self._seed_resolved(
            tmp_path, "000858", "2025-11-01", "Other ticker old decision.",
            "Other ticker old lesson.", end="2025-11-10", resolved="2025-11-11",
        )

        ctx = log.get_past_context("600519", as_of="2026-01-15")

        assert "Other ticker old lesson." in ctx
        assert "august" not in ctx.lower(), "8 月跨标的记忆泄漏进 1 月分析"

    def test_legacy_entry_degraded_in_as_of_mode(self, tmp_path):
        """旧格式（无时间元数据）resolved 条目：决策可注入，收益/复盘保守剔除。"""
        log = make_log(tmp_path)
        _seed_completed(
            tmp_path, "600519", "2025-12-01",
            "Legacy decision without outcome metadata.",
            "Legacy reflection that cannot be proven available.",
        )

        ctx = log.get_past_context("600519", as_of="2026-01-15")

        assert "Legacy decision without outcome metadata." in ctx
        assert "Legacy reflection that cannot be proven available." not in ctx, (
            "无法证明当时可用的复盘必须剔除（保守行为）"
        )
        assert "+1.0%" not in ctx, "无法证明当时可用的收益必须剔除"

        # as_of=None（实时分析）保持原行为：完整注入
        live_ctx = log.get_past_context("600519")
        assert "Legacy reflection that cannot be proven available." in live_ctx

    def test_unparseable_entry_date_excluded_in_as_of_mode(self, tmp_path):
        """日期解析失败的条目在历史模式下保守排除；实时模式保持原行为。"""
        log = make_log(tmp_path)
        self._seed_resolved(
            tmp_path, "600519", "not-a-date", "Garbage date decision.",
            "Garbage date reflection.",
        )

        assert log.get_past_context("600519", as_of="2026-01-15") == ""
        assert "Garbage date decision." in log.get_past_context("600519")

    def test_legacy_and_new_entries_coexist(self, tmp_path):
        """旧格式与新格式条目共存时读写正常：解析互不干扰。"""
        log = make_log(tmp_path)
        _seed_completed(tmp_path, "600519", "2025-06-01", "Old entry.", "Old reflection.")
        self._seed_resolved(
            tmp_path, "600519", "2025-12-01", "New entry.", "New reflection.",
            end="2025-12-10", resolved="2025-12-11",
        )

        entries = log.load_entries()
        by_date = {e["date"]: e for e in entries}
        assert by_date["2025-06-01"].get("outcome_end") is None
        assert by_date["2025-12-01"].get("outcome_end") == "2025-12-10"
        assert by_date["2025-12-01"].get("resolved_at") == "2025-12-11"

        # 追加新决策不受影响
        log.store_decision("600519", "2026-01-05", DECISION_BUY)
        assert len(log.get_pending_entries()) == 1

    def test_update_with_outcome_writes_time_metadata(self, tmp_path):
        """回填写入 end/resolved 元数据并完整 roundtrip。"""
        log = make_log(tmp_path)
        log.store_decision("600519", "2026-01-05", DECISION_BUY)

        with patch("tradingagents.agents.utils.memory.datetime") as mock_dt:
            mock_dt.now.return_value.strftime.return_value = "2026-01-20"
            log.update_with_outcome(
                "600519", "2026-01-05", 0.05, 0.02, 5, "Lesson.",
                outcome_end="2026-01-12",
            )

        entries = log.load_entries()
        assert entries[0]["outcome_end"] == "2026-01-12"
        assert entries[0]["resolved_at"] == "2026-01-20"
        raw = (tmp_path / "trading_memory.md").read_text(encoding="utf-8")
        assert "end=2026-01-12" in raw
        assert "resolved=2026-01-20" in raw

    def test_outcome_visible_but_late_reflection_withheld(self, tmp_path):
        """F3：收益窗口已结束（end ≤ as_of）但复盘晚生成（resolved > as_of）。

        收益数字是当时已可知的市场事实，可以注入；复盘文本是后来才写的
        经验总结，不得回溯注入到更早的分析日期。
        """
        log = make_log(tmp_path)
        self._seed_resolved(
            tmp_path, "600519", "2026-01-01", "January decision.",
            "SEPTEMBER_GENERATED_LESSON",
            end="2026-01-08", resolved="2026-09-05",
        )

        ctx = log.get_past_context("600519", as_of="2026-01-15")

        assert "January decision." in ctx, "决策可见"
        assert "+20.0%" in ctx, "收益窗口已在 as_of 前结束，收益数字可见"
        assert "SEPTEMBER_GENERATED_LESSON" not in ctx, "晚生成的复盘泄漏"
        assert "复盘生成时间晚于分析时点" in ctx, "应注明复盘为何缺失"

    def test_reflection_visible_only_after_resolved_date(self, tmp_path):
        """F3：resolved ≤ as_of 时复盘才完整注入（跨过回填日即可见）。"""
        log = make_log(tmp_path)
        self._seed_resolved(
            tmp_path, "600519", "2026-01-01", "January decision.",
            "EARLY_GENERATED_LESSON",
            end="2026-01-08", resolved="2026-01-10",
        )

        ctx = log.get_past_context("600519", as_of="2026-01-15")

        assert "EARLY_GENERATED_LESSON" in ctx
        assert "REFLECTION:" in ctx

    def test_cross_ticker_late_reflection_also_withheld(self, tmp_path):
        """F3：跨标的经验同样按复盘生成时间过滤。"""
        log = make_log(tmp_path)
        self._seed_resolved(
            tmp_path, "000858", "2026-01-01", "Cross decision.",
            "CROSS_SEPTEMBER_LESSON",
            end="2026-01-08", resolved="2026-09-05",
        )

        ctx = log.get_past_context("600519", as_of="2026-01-15")

        assert "CROSS_SEPTEMBER_LESSON" not in ctx, "跨标的晚生成复盘泄漏"
        # 收益窗口已结束：跨标的 tag 中的收益数字可见，复盘被注明剔除
        assert "+20.0%" in ctx
        assert "复盘生成时间晚于分析时点" in ctx

    def test_as_of_accepts_datetime_like_string(self, tmp_path):
        """带时分秒的 as_of（如 "2026-01-15 15:30:00"）同样被识别。"""
        log = make_log(tmp_path)
        self._seed_resolved(
            tmp_path, "600519", "2026-08-01", "Future decision.",
            "Future lesson.", end="2026-08-10", resolved="2026-09-01",
        )
        ctx = log.get_past_context("600519", as_of="2026-01-15 15:30:00")
        assert ctx == ""


# ---------------------------------------------------------------------------
# R4/F4/F5: 图运行入口的时点边界（真实 SQLite 断点恢复路径）
# ---------------------------------------------------------------------------



def _nullcontext():
    from contextlib import nullcontext

    return nullcontext()

def _checkpoint_runner(tmp_path, workflow):
    """构造仅含恢复路径所需属性的真实方法 runner（不经过 __init__）。"""
    from tradingagents.graph.checkpointer import get_checkpointer  # noqa: F401

    runner = TradingAgentsGraph.__new__(TradingAgentsGraph)
    runner.config = {"checkpoint_enabled": True, "data_cache_dir": str(tmp_path)}
    runner.workflow = workflow
    # A10 恢复检查：__new__ 桩不带 selected_analysts → 生产侧归入
    # 「无法验证」边界放行（真实 __init__ 必设非空集合）。
    runner.propagator = Propagator()
    runner.memory_log = TradingMemoryLog(
        {"memory_log_path": str(tmp_path / "memory.md")}
    )
    runner._resolve_pending_entries = MagicMock()
    runner._checkpointer_ctx = None
    return runner


def _is_refusal(error) -> bool:
    import re as _re
    return bool(_re.search(
        r"不兼容|incompatible|unsafe|cannot safely|无法安全|拒绝恢复|需要重新开始",
        str(error), _re.IGNORECASE,
    ))


class TestPointInTimeGraphIntegration:
    """历史分析不使用未来记忆——从运行入口到恢复路径全程生效。"""

    def _fake_graph(self, tmp_path, checkpoint_enabled=False):
        log = TradingMemoryLog(
            {"memory_log_path": str(tmp_path / "trading_memory.md")}
        )
        # 不用 spec=：propagator/graph 等是实例属性，spec 模式禁止赋值
        graph = MagicMock()
        graph.memory_log = log
        graph.config = {
            "checkpoint_enabled": checkpoint_enabled,
            "data_cache_dir": str(tmp_path),
        }
        graph.propagator.get_graph_args.return_value = {
            "stream_mode": "values",
            "config": {"recursion_limit": 100},
        }
        # 让 prepare_graph_run 的真实逻辑拿到真实的记忆过滤结果
        graph.propagator.create_initial_state.side_effect = (
            lambda company, date, past_context="", selected_analysts=None: {"past_context": past_context}
        )
        graph._past_context_as_of.side_effect = (
            lambda company, date: log.get_past_context(company, as_of=date)
        )
        # F5 之后恢复安全检查是实例方法；mock 上必须指回真实实现，
        # 否则整段检查会被 MagicMock 静默吞掉（等于没有测试它）。
        import types as _types

        graph._safe_resume_checkpoint = _types.MethodType(
            TradingAgentsGraph._safe_resume_checkpoint, graph
        )
        graph._refuse_incompatible_legacy_checkpoint = _types.MethodType(
            TradingAgentsGraph._refuse_incompatible_legacy_checkpoint, graph
        )
        # A05: prepare_graph_run 现经由 run_context + _prepare_graph_run；
        # mock 上指回真实内部实现，否则整段逻辑被 MagicMock 吞掉。
        graph.run_context = _nullcontext
        graph._prepare_graph_run = _types.MethodType(
            TradingAgentsGraph._prepare_graph_run, graph
        )
        return graph, log

    def test_fresh_historical_run_filters_future_memory(self, tmp_path):
        """prepare_graph_run 以 trade_date 为 as_of：8 月决策不进 1 月分析的初始 state。"""
        graph, log = self._fake_graph(tmp_path)
        log.store_decision("600519", "2026-08-01", "August future decision text.")
        log.update_with_outcome(
            "600519", "2026-08-01", 0.20, 0.05, 5,
            "August future lesson.", outcome_end="2026-08-10",
        )

        init_state, _, _ = TradingAgentsGraph.prepare_graph_run(
            graph, "600519", "2026-01-15"
        )

        assert init_state is not None
        assert "August" not in init_state["past_context"], (
            "历史运行的初始 state 泄漏了未来记忆"
        )

        # as_of 晚于该条目时（如 2999 年）完全可见——过滤不能误伤正常注入
        live_graph, _ = self._fake_graph(tmp_path)
        TradingAgentsGraph.prepare_graph_run(live_graph, "600519", "2999-01-01")
        created = live_graph.propagator.create_initial_state.call_args
        assert "August future decision text." in created.kwargs["past_context"]

    def test_resolve_pending_then_historical_run_excludes_future(self, tmp_path):
        """历史运行触发收益回填后，刚取得的未来结果不注入当前历史运行。"""
        graph, log = self._fake_graph(tmp_path)
        log.store_decision("600519", "2026-08-01", "August pending decision.")
        graph.reflector = MagicMock()
        graph.reflector.reflect_on_final_decision.return_value = "August lesson."
        graph._fetch_returns = MagicMock(return_value=(0.20, 0.05, 5, "2026-08-10"))

        TradingAgentsGraph._resolve_pending_entries(graph, "600519")

        # 回填确实完成（含时间元数据）
        assert log.get_pending_entries() == []
        entries = log.load_entries()
        assert entries[0]["outcome_end"] == "2026-08-10"

        # 但 as_of=2026-01-15 的历史分析读不到它
        TradingAgentsGraph.prepare_graph_run(graph, "600519", "2026-01-15")
        created = graph.propagator.create_initial_state.call_args
        assert "August" not in created.kwargs["past_context"]

    def _qg_workflow(self, seen):
        """单 writer 图：START → Market Analyst → Quality Gate → END。"""
        workflow = StateGraph(AgentState)
        workflow.add_node("Market Analyst", lambda _: {"market_report": "done"})

        def quality_gate(state):
            seen.append(state.get("past_context", ""))
            return {"data_quality_summary": "done"}

        workflow.add_node("Quality Gate", quality_gate)
        workflow.add_edge(START, "Market Analyst")
        workflow.add_edge("Market Analyst", "Quality Gate")
        workflow.add_edge("Quality Gate", END)
        return workflow

    def test_resume_refreshes_stale_past_context(self, tmp_path):
        """恢复路径（真实 SQLite 断点）：未过滤 past_context 被原位刷新。

        单一最后写入者场景 update_state 无歧义：刷新成功后恢复执行，质量
        检查恰好运行一次且看到的是按时间边界重算的干净上下文。
        """
        seen = []
        workflow = self._qg_workflow(seen)
        config = {"configurable": {"thread_id": thread_id("600519", "2026-01-15")}}
        with get_checkpointer(str(tmp_path), "600519") as saver:
            graph = workflow.compile(checkpointer=saver, interrupt_before=["Quality Gate"])
            graph.invoke(
                Propagator().create_initial_state("600519", "2026-01-15", "FUTURE_POLLUTION"),
                config,
            )

        runner = _checkpoint_runner(tmp_path, workflow)
        try:
            initial, args, _ = runner.prepare_graph_run("600519", "2026-01-15")
            assert initial is None, "存在 checkpoint 时应走恢复路径"
            runner.graph.invoke(initial, **args)
        finally:
            runner.close_graph_run()

        assert seen == [""], (
            f"恢复后质量检查应恰好执行一次且看到刷新后的干净上下文，实际: {seen}"
        )

    def test_resume_keeps_context_when_already_consistent(self, tmp_path):
        """断点上下文与重算一致时不做刷新，恢复正常完成。"""
        seen = []
        workflow = self._qg_workflow(seen)
        config = {"configurable": {"thread_id": thread_id("600519", "2026-01-15")}}
        with get_checkpointer(str(tmp_path), "600519") as saver:
            graph = workflow.compile(checkpointer=saver, interrupt_before=["Quality Gate"])
            graph.invoke(
                Propagator().create_initial_state("600519", "2026-01-15", ""),
                config,
            )

        runner = _checkpoint_runner(tmp_path, workflow)
        try:
            initial, args, _ = runner.prepare_graph_run("600519", "2026-01-15")
            runner.graph.invoke(initial, **args)
        finally:
            runner.close_graph_run()

        assert seen == [""]

    def test_new_graph_first_call_failure_retry_not_refused(self, tmp_path):
        """F4 边界：新图首次调用失败（未产出消息）后重试，不得误报不兼容。

        分析师节点第一次抛 TimeoutError：断点待执行 Market Analyst、分支
        通道与共享通道都没有执行流量——恢复等价于从头执行该分支，是合法
        重试，必须放行。
        """
        calls = {"n": 0}

        def analyst_node(state):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("LLM first call timeout")
            return {"messages": [AIMessage(content="market report")], "market_report": "DONE"}

        setup = GraphSetup(
            None, None, {"market": ToolNode([])}, ConditionalLogic(),
            node_factories={"market": lambda llm: analyst_node},
        )
        workflow = setup.setup_graph(["market"])
        config = {"configurable": {"thread_id": thread_id("600519", "2026-01-15")}}
        with get_checkpointer(str(tmp_path), "600519") as saver:
            graph = workflow.compile(checkpointer=saver)
            with pytest.raises(TimeoutError, match="LLM first call timeout"):
                graph.invoke(
                    Propagator().create_initial_state("600519", "2026-01-15"), config
                )

        # 断点保留且确实停在分支节点、无任何分支/共享执行流量
        assert has_checkpoint(str(tmp_path), "600519", "2026-01-15")
        with get_checkpointer(str(tmp_path), "600519") as saver:
            graph = workflow.compile(checkpointer=saver)
            snap = graph.get_state(config)
        assert snap.next == ("Market Analyst",)

        runner = _checkpoint_runner(tmp_path, workflow)
        try:
            initial, args, _ = runner.prepare_graph_run("600519", "2026-01-15")
            assert initial is None
            # 只验证分支恢复执行，不进入下游 LLM 节点
            list(runner.graph.stream(
                initial, **args, interrupt_before=["Quality Gate"]
            ))
            assert (
                runner.graph.get_state(args["config"]).values.get("market_report")
                == "DONE"
            ), "合法重试被误拒或未完成"
        finally:
            runner.close_graph_run()

    def test_legacy_or_join_pending_quality_gate_refused(self, tmp_path):
        """F4 边界：旧 OR 拓扑停在汇合屏障（Quality Gate 待执行）也必须拒绝。

        旧图的汇合触发状态（首个完成分支即可启动质量检查）与新 AND 屏障
        不等价；共享通道存在旧版执行流量是可靠证据。
        """
        workflow = StateGraph(AgentState)
        # 旧拓扑：分析师把报告消息写进共享 messages 通道（R1 之前的行为）
        workflow.add_node(
            "Market Analyst",
            lambda _: {"messages": [AIMessage(content="legacy market report")],
                       "market_report": "legacy market report"},
        )
        workflow.add_node("Quality Gate", lambda _: {"data_quality_summary": "qg"})
        workflow.add_edge(START, "Market Analyst")
        workflow.add_edge("Market Analyst", "Quality Gate")
        workflow.add_edge("Quality Gate", END)

        config = {"configurable": {"thread_id": thread_id("600519", "2026-01-15")}}
        with get_checkpointer(str(tmp_path), "600519") as saver:
            graph = workflow.compile(checkpointer=saver, interrupt_before=["Quality Gate"])
            graph.invoke(
                Propagator().create_initial_state("600519", "2026-01-15"), config
            )
            assert graph.get_state(config).next == ("Quality Gate",)

        runner = _checkpoint_runner(tmp_path, workflow)
        try:
            with pytest.raises(RuntimeError) as exc_info:
                runner.prepare_graph_run("600519", "2026-01-15")
            assert _is_refusal(exc_info.value), str(exc_info.value)
        finally:
            runner.close_graph_run()

        # 拒绝后断点保留（不清除用户数据）
        assert has_checkpoint(str(tmp_path), "600519", "2026-01-15")

    def test_legacy_post_gate_partial_reports_resume_refused(self, tmp_path):
        """F4 最终收口：已越过旧 OR Quality Gate 的下游断点也必须拒绝。

        旧拓扑下质量检查可能在部分分析师未完成时提前运行（news_report 仍
        为空），断点停在 Bull Researcher 前——不能因为下游节点不读取
        messages 通道就放行：不完整的报告会直接进入多空辩论。
        """
        # 旧拓扑图：Market Analyst 把报告消息写进共享 messages（R1 之前）
        workflow = StateGraph(AgentState)
        workflow.add_node(
            "Market Analyst",
            lambda _: {"messages": [AIMessage(content="legacy market report")],
                       "market_report": "legacy market report"},
        )
        workflow.add_node(
            "Quality Gate",
            lambda _: {"data_quality_summary": "checked partial reports"},
        )
        workflow.add_node("Bull Researcher", lambda _: {"investment_plan": "should not run"})
        workflow.add_edge(START, "Market Analyst")
        workflow.add_edge("Market Analyst", "Quality Gate")
        workflow.add_edge("Quality Gate", "Bull Researcher")
        workflow.add_edge("Bull Researcher", END)

        config = {"configurable": {"thread_id": thread_id("600519", "2026-01-15")}}
        with get_checkpointer(str(tmp_path), "600519") as saver:
            graph = workflow.compile(
                checkpointer=saver, interrupt_before=["Bull Researcher"]
            )
            graph.invoke(
                Propagator().create_initial_state("600519", "2026-01-15"), config
            )
            snap = graph.get_state(config)
        assert snap.next == ("Bull Researcher",)
        assert snap.values.get("news_report", "") == ""
        assert snap.values.get("data_quality_summary") == "checked partial reports"

        # 用当前 GraphSetup（market+news，AND 屏障拓扑）尝试恢复
        setup = GraphSetup(
            MagicMock(), MagicMock(),
            {"market": ToolNode([]), "news": ToolNode([])},
            ConditionalLogic(),
        )
        runner = _checkpoint_runner(tmp_path, setup.setup_graph(["market", "news"]))
        try:
            with pytest.raises(RuntimeError) as exc_info:
                runner.prepare_graph_run("600519", "2026-01-15")
            assert _is_refusal(exc_info.value), str(exc_info.value)
        finally:
            runner.close_graph_run()

        # 拒绝后断点保留，Bull 未执行
        assert has_checkpoint(str(tmp_path), "600519", "2026-01-15")
        with get_checkpointer(str(tmp_path), "600519") as saver:
            graph = workflow.compile(checkpointer=saver)
            resumed_values = graph.get_state(config).values
        assert not resumed_values.get("investment_plan"), (
            "旧 OR 断点的不完整报告不得流入下游辩论"
        )

    def test_new_isolated_graph_resumes_after_trader(self, tmp_path):
        """F4 最终收口：新图 Trader 之后的断点正常恢复，不得误拒。

        新版图的 Trader/辩论节点也向共享 messages 写 AIMessage——判别必须
        结合分支通道内容：新图分析师阶段运行过后分支通道必有内容，此时
        共享通道的 AI 流量来自下游节点，属合法状态。
        """
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.outputs import ChatGeneration, ChatResult

        class MockChat(BaseChatModel):
            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                return ChatResult(
                    generations=[
                        ChatGeneration(
                            message=AIMessage(content="mocked downstream response")
                        )
                    ]
                )

            @property
            def _llm_type(self):
                return "mock-chat"

            def bind_tools(self, tools, **kwargs):
                return self

        mock_llm = MockChat()
        setup = GraphSetup(
            mock_llm, mock_llm,
            {"market": ToolNode([]), "news": ToolNode([])},
            ConditionalLogic(max_debate_rounds=1, max_risk_discuss_rounds=1),
        )
        workflow = setup.setup_graph(["market", "news"])
        config = {"configurable": {"thread_id": thread_id("600519", "2026-01-15")}}
        with get_checkpointer(str(tmp_path), "600519") as saver:
            graph = workflow.compile(
                checkpointer=saver, interrupt_before=["Aggressive Analyst"]
            )
            graph.invoke(
                Propagator().create_initial_state("600519", "2026-01-15"), config
            )
            snap = graph.get_state(config)
        # 断点确实停在 Trader 之后：Trader 已写共享 AIMessage，分析师报告齐全
        assert snap.next == ("Aggressive Analyst",)
        assert snap.values.get("trader_investment_plan")
        assert self._has_shared_ai_traffic(snap.values)

        runner = _checkpoint_runner(tmp_path, workflow)
        try:
            initial, args, _ = runner.prepare_graph_run("600519", "2026-01-15")
            assert initial is None, "新图下游断点应正常恢复，不得误拒"
            final = runner.graph.invoke(initial, **args)
        finally:
            runner.close_graph_run()

        assert final.get("final_trade_decision"), "恢复后应完成剩余风控与决策"
        assert final.get("market_report") and final.get("news_report")

    @staticmethod
    def _has_shared_ai_traffic(values):
        return TradingAgentsGraph._shared_channel_has_ai_traffic(values)

    def test_polluted_final_decision_refused_not_returned(self, tmp_path):
        """F5 边界：已生成最终决策且记忆上下文污染 → 明确拒绝，不得当正常结果返回。

        只擦 past_context 清理不掉已混入决策文本的未来信息；也不能只打
        warning 就把旧决策作为成功分析交回。
        """
        returned = []

        def decision_node(state):
            return {
                "market_report": "done",
                "final_trade_decision": "BUY (influenced by FUTURE_POLLUTION)",
            }

        def tail_node(state):
            returned.append(state.get("final_trade_decision", ""))
            return {}

        workflow = StateGraph(AgentState)
        workflow.add_node("Market Analyst", decision_node)
        workflow.add_node("Tail", tail_node)
        workflow.add_edge(START, "Market Analyst")
        workflow.add_edge("Market Analyst", "Tail")
        workflow.add_edge("Tail", END)

        config = {"configurable": {"thread_id": thread_id("600519", "2026-01-15")}}
        with get_checkpointer(str(tmp_path), "600519") as saver:
            graph = workflow.compile(checkpointer=saver, interrupt_before=["Tail"])
            graph.invoke(
                Propagator().create_initial_state("600519", "2026-01-15", "FUTURE_POLLUTION"),
                config,
            )
            snap = graph.get_state(config)
        assert snap.values.get("final_trade_decision")
        assert snap.next == ("Tail",)

        runner = _checkpoint_runner(tmp_path, workflow)
        try:
            with pytest.raises(RuntimeError) as exc_info:
                runner.prepare_graph_run("600519", "2026-01-15")
            assert _is_refusal(exc_info.value), str(exc_info.value)
        finally:
            runner.close_graph_run()

        assert not returned, "污染决策不得作为正常分析结果继续流转"
        assert has_checkpoint(str(tmp_path), "600519", "2026-01-15")

    def test_fetch_returns_reports_window_end_date(self):
        """_fetch_returns 返回收益窗口的实际结束日（native 分支）。"""
        stock_prices = [100.0, 102.0, 104.0, 103.0, 105.0, 106.0]
        bench_prices = [4000.0, 4020.0, 4040.0, 4030.0, 4050.0, 4060.0]
        mock_graph = MagicMock(spec=TradingAgentsGraph)
        with patch("tradingagents.dataflows.a_stock.get_astock_history_df", return_value=_native_kline_df(stock_prices)), \
             patch("tradingagents.dataflows.index_data.get_index_history_df", return_value=_native_kline_df(bench_prices)):
            raw, alpha, days, window_end = TradingAgentsGraph._fetch_returns(
                mock_graph, "688017", "2026-01-05"
            )
        assert raw is not None
        assert days == 5
        # _native_kline_df 从 2026-01-05 连续 6 天 → 第 5 个交易日为 2026-01-10
        assert window_end == "2026-01-10", (
            "收益窗口结束日必须来自实际行情日期，而非日历推算"
        )


# ---------------------------------------------------------------------------
# Legacy removal: BM25 / FinancialSituationMemory fully gone
# ---------------------------------------------------------------------------

class TestLegacyRemoval:

    def test_financial_situation_memory_removed(self):
        """FinancialSituationMemory must not be importable from the memory module."""
        import tradingagents.agents.utils.memory as m
        assert not hasattr(m, "FinancialSituationMemory")

    def test_bm25_not_imported(self):
        """rank_bm25 must not be present in the memory module namespace."""
        import tradingagents.agents.utils.memory as m
        assert not hasattr(m, "BM25Okapi")

    def test_reflect_and_remember_removed(self):
        """TradingAgentsGraph must not expose reflect_and_remember."""
        assert not hasattr(TradingAgentsGraph, "reflect_and_remember")

    def test_portfolio_manager_no_memory_param(self):
        """create_portfolio_manager accepts only llm; passing memory= raises TypeError."""
        mock_llm = MagicMock()
        create_portfolio_manager(mock_llm)
        with pytest.raises(TypeError):
            create_portfolio_manager(mock_llm, memory=MagicMock())

    def test_full_pipeline_no_regression(self, tmp_path):
        """propagate() completes and stores the decision after the redesign."""
        import functools

        fake_state = {
            "final_trade_decision": "Rating: Buy\nBuy NVDA.",
            "company_of_interest": "NVDA",
            "trade_date": "2026-01-10",
            "market_report": "",
            "sentiment_report": "",
            "news_report": "",
            "fundamentals_report": "",
            "investment_debate_state": {
                "bull_history": "", "bear_history": "", "history": "",
                "current_response": "", "judge_decision": "",
            },
            "investment_plan": "",
            "trader_investment_plan": "",
            "risk_debate_state": {
                "aggressive_history": "", "conservative_history": "",
                "neutral_history": "", "history": "", "judge_decision": "",
                "current_aggressive_response": "", "current_conservative_response": "",
                "current_neutral_response": "", "count": 1, "latest_speaker": "",
            },
        }
        mock_graph = MagicMock()
        mock_graph.memory_log = TradingMemoryLog({"memory_log_path": str(tmp_path / "mem.md")})
        mock_graph.log_states_dict = {}
        mock_graph.debug = False
        mock_graph._checkpointer_ctx = None
        mock_graph.config = {"results_dir": str(tmp_path)}
        mock_graph.graph.invoke.return_value = fake_state
        mock_graph.propagator.create_initial_state.return_value = fake_state
        mock_graph.propagator.get_graph_args.return_value = {}
        mock_graph.signal_processor.process_signal.return_value = "Buy"
        # Bind the real _run_graph so propagate's call to self._run_graph executes
        # the actual write path instead of the auto-MagicMock.
        mock_graph._run_graph = functools.partial(
            TradingAgentsGraph._run_graph, mock_graph
        )
        # A05: 生命周期方法经由 run_context/_prepare_graph_run 等内部方法，
        # mock 上指回真实实现，否则被 MagicMock 吞掉（返回值不可解包）。
        import contextlib as _ctxlib

        mock_graph.run_context = _ctxlib.nullcontext
        mock_graph._prepare_graph_run = functools.partial(
            TradingAgentsGraph._prepare_graph_run, mock_graph
        )
        mock_graph.prepare_graph_run = functools.partial(
            TradingAgentsGraph.prepare_graph_run, mock_graph
        )
        mock_graph._finalize_graph_run = functools.partial(
            TradingAgentsGraph._finalize_graph_run, mock_graph
        )
        mock_graph.finalize_graph_run = functools.partial(
            TradingAgentsGraph.finalize_graph_run, mock_graph
        )
        mock_graph.close_graph_run = functools.partial(
            TradingAgentsGraph.close_graph_run, mock_graph
        )
        TradingAgentsGraph.propagate(mock_graph, "NVDA", "2026-01-10")
        entries = mock_graph.memory_log.load_entries()
        assert len(entries) == 1
        assert entries[0]["ticker"] == "NVDA"
        assert entries[0]["pending"] is True
