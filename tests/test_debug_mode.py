"""Tests for Web UI Debug Mode and 7-Agent independent status monitoring."""

from __future__ import annotations

import threading
import time
import unittest
from typing import Any
from unittest.mock import MagicMock, patch

from web.progress import (
    ANALYST_AGENTS,
    ANALYST_AGENT_IDS,
    ANALYST_MAP,
    ProgressTracker,
)
from web.runner import _detect_completed_stages


class TestDebugModeTracker(unittest.TestCase):
    """Test 7-Agent tracking capabilities of ProgressTracker."""

    def test_analyst_agents_constants(self) -> None:
        """Verify the 7 analyst agents constants and definitions."""
        self.assertEqual(len(ANALYST_AGENTS), 7)
        self.assertEqual(len(ANALYST_AGENT_IDS), 7)
        expected_ids = [
            "market", "social", "news", "fundamentals", "policy", "hot_money", "lockup"
        ]
        self.assertEqual(ANALYST_AGENT_IDS, expected_ids)
        for agent in ANALYST_AGENTS:
            self.assertIn("id", agent)
            self.assertIn("name", agent)
            self.assertIn("icon", agent)
            self.assertIn("report_key", agent)
            self.assertIn("desc", agent)
            self.assertEqual(ANALYST_MAP[agent["id"]], agent)

    def test_tracker_initial_agent_states(self) -> None:
        """Verify initial state of all 7 agents in ProgressTracker."""
        tracker = ProgressTracker(ticker="600519", trade_date="2026-08-16")
        self.assertEqual(len(tracker.agent_statuses), 7)
        for aid in ANALYST_AGENT_IDS:
            self.assertEqual(tracker.get_agent_status(aid), "pending")
            self.assertIn("等待", tracker.get_agent_detail(aid))
            self.assertEqual(tracker.get_agent_tool_calls(aid), [])

    def test_tracker_set_status_and_tool(self) -> None:
        """Verify setting status and recording tool calls."""
        tracker = ProgressTracker(ticker="600519", trade_date="2026-08-16")
        tracker.is_running = True

        tracker.set_agent_status("market", "running", "正在分析K线...")
        self.assertEqual(tracker.get_agent_status("market"), "running")
        self.assertEqual(tracker.get_agent_detail("market"), "正在分析K线...")

        tracker.record_agent_tool("market", "get_stock_data")
        self.assertEqual(tracker.get_agent_status("market"), "tool_calling")
        self.assertIn("get_stock_data", tracker.get_agent_tool_calls("market"))
        self.assertIn("get_stock_data", tracker.get_agent_detail("market"))

        # Duplicate tool call should not duplicate in list
        tracker.record_agent_tool("market", "get_stock_data")
        self.assertEqual(tracker.get_agent_tool_calls("market"), ["get_stock_data"])

        # Second distinct tool
        tracker.record_agent_tool("market", "get_indicators")
        self.assertEqual(
            tracker.get_agent_tool_calls("market"),
            ["get_stock_data", "get_indicators"],
        )

    def test_tracker_mark_stage_done_updates_agent(self) -> None:
        """Verify mark_stage_done marks agent as done with word count detail."""
        tracker = ProgressTracker(ticker="600519", trade_date="2026-08-16")
        tracker.is_running = True

        test_report = "这是一份测试的技术分析报告，包含均线、MACD和量价分析。" * 5
        tracker.mark_stage_done("market", test_report)

        self.assertEqual(tracker.get_agent_status("market"), "done")
        self.assertIn("报告已生成", tracker.get_agent_detail("market"))
        self.assertIn(str(len(test_report)), tracker.get_agent_detail("market"))

        snapshot = tracker.get_all_agent_states()
        self.assertEqual(len(snapshot), 7)
        self.assertEqual(snapshot["market"]["status"], "done")
        self.assertEqual(snapshot["market"]["report"], test_report)
        self.assertEqual(snapshot["social"]["status"], "pending")

    def test_tracker_stop_and_reset(self) -> None:
        """Verify request_stop and mark_stopped cleanly reset agent tracking."""
        tracker = ProgressTracker(ticker="600519", trade_date="2026-08-16")
        tracker.is_running = True
        tracker.set_agent_status("hot_money", "running", "追踪龙虎榜...")
        tracker.record_agent_tool("hot_money", "get_dragon_tiger_board")

        self.assertEqual(tracker.get_agent_status("hot_money"), "tool_calling")
        self.assertTrue(tracker.request_stop())
        self.assertEqual(tracker.get_agent_status("hot_money"), "pending")
        self.assertEqual(tracker.get_agent_tool_calls("hot_money"), [])

        tracker.mark_stopped()
        self.assertEqual(tracker.get_agent_status("hot_money"), "pending")
        self.assertIn("等待启动", tracker.get_agent_detail("hot_money"))

    def test_tracker_thread_safety(self) -> None:
        """Verify thread-safe updates under concurrent multi-agent executions."""
        tracker = ProgressTracker(ticker="600519", trade_date="2026-08-16")
        tracker.is_running = True

        def worker(aid: str) -> None:
            for i in range(20):
                tracker.set_agent_status(aid, "running", f"step {i}")
                tracker.record_agent_tool(aid, f"tool_{aid}_{i}")
                time.sleep(0.001)
            tracker.mark_stage_done(aid, f"final report for {aid}")

        threads = [
            threading.Thread(target=worker, args=(aid,))
            for aid in ANALYST_AGENT_IDS
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for aid in ANALYST_AGENT_IDS:
            self.assertEqual(tracker.get_agent_status(aid), "done")
            self.assertEqual(len(tracker.get_agent_tool_calls(aid)), 20)
            self.assertIn("报告已生成", tracker.get_agent_detail(aid))


class TestRunnerEventDetection(unittest.TestCase):
    """Test LangGraph node and message event detection in web/runner.py."""

    def setUp(self) -> None:
        self.tracker = ProgressTracker(ticker="300750", trade_date="2026-08-16")
        self.tracker.is_running = True

    def test_node_updates_analyst_and_tool_trigger(self) -> None:
        """Verify chunk with node names updates agent status and tool calls."""
        # 1. Analyst node trigger
        chunk1 = {"Market Analyst": {"messages": []}}
        _detect_completed_stages(chunk1, self.tracker)
        self.assertEqual(self.tracker.get_agent_status("market"), "running")

        # 2. Tool node trigger
        class DummyMsg:
            def __init__(self, name: str) -> None:
                self.name = name

        chunk2 = {"tools_market": {"messages": [DummyMsg("get_stock_data")]}}
        _detect_completed_stages(chunk2, self.tracker)
        self.assertEqual(self.tracker.get_agent_status("market"), "tool_calling")
        self.assertIn("get_stock_data", self.tracker.get_agent_tool_calls("market"))

        # 3. Hot money analyst & tool
        chunk3 = {"Hot_money Analyst": {}}
        _detect_completed_stages(chunk3, self.tracker)
        self.assertEqual(self.tracker.get_agent_status("hot_money"), "running")

        chunk4 = {"tools_hot_money": {"messages": [{"name": "get_dragon_tiger_board"}]}}
        _detect_completed_stages(chunk4, self.tracker)
        self.assertEqual(self.tracker.get_agent_status("hot_money"), "tool_calling")
        self.assertIn("get_dragon_tiger_board", self.tracker.get_agent_tool_calls("hot_money"))

    def test_report_completion_marks_agent_done(self) -> None:
        """Verify report output chunk marks corresponding stage and agent done."""
        chunk = {
            "market_report": "### 技术面报告\n均线多头排列，量价配合良好。",
            "policy_report": "### 政策分析\n新能源产业扶持政策持续发力。",
        }
        _detect_completed_stages(chunk, self.tracker)

        self.assertEqual(self.tracker.stage_status("market"), "done")
        self.assertEqual(self.tracker.get_agent_status("market"), "done")
        self.assertIn("报告已生成", self.tracker.get_agent_detail("market"))

        self.assertEqual(self.tracker.stage_status("policy"), "done")
        self.assertEqual(self.tracker.get_agent_status("policy"), "done")

    def test_values_mode_message_tool_detection(self) -> None:
        """Verify LangGraph values stream mode tool call extraction."""
        class MockAIMsg:
            def __init__(self, tool_names: list[str]) -> None:
                self.tool_calls = [{"name": n} for n in tool_names]

        chunk = {
            "messages": [
                MockAIMsg(["get_balance_sheet", "get_cashflow"]),
            ]
        }
        _detect_completed_stages(chunk, self.tracker)

        self.assertEqual(self.tracker.get_agent_status("fundamentals"), "tool_calling")
        self.assertIn("get_balance_sheet", self.tracker.get_agent_tool_calls("fundamentals"))
        self.assertIn("get_cashflow", self.tracker.get_agent_tool_calls("fundamentals"))


class TestDebugModeUIComponents(unittest.TestCase):
    """Test UI rendering functions for the live agent dashboard and diagnostics.

    v0.6.1 重构后旧的 `_agent_status_badge` / `render_debug_monitor` 已并入
    `_render_agent_card`（内联配色/徽章方案）与 `render_progress`（内含遥测
    expander），这里按新 API 断言等价行为。
    """

    def test_agent_progress_pct(self) -> None:
        """进度估算：done=100、error=100（终态）、pending 起步 5。"""
        from web.components.progress_panel import _get_agent_progress_pct

        self.assertEqual(_get_agent_progress_pct("done", 0), 100)
        self.assertEqual(_get_agent_progress_pct("error", 0), 100)
        self.assertEqual(_get_agent_progress_pct("pending", 0), 5)
        self.assertEqual(_get_agent_progress_pct("running", 0), 40)
        self.assertEqual(_get_agent_progress_pct("running", 2), 80)
        self.assertEqual(_get_agent_progress_pct("tool_calling", 0), 30)
        self.assertEqual(_get_agent_progress_pct("tool_calling", 10), 70)

    @patch("streamlit.markdown")
    def test_agent_card_badge_schemes(self, mock_md: MagicMock) -> None:
        """每种状态的卡片徽章文案与配色方案必须落在渲染出的 HTML 里。"""
        from web.components.progress_panel import _render_agent_card

        def _rendered_html(status: str) -> str:
            _render_agent_card(
                agent_id="market",
                name="市场分析师",
                icon="📊",
                desc="技术面",
                status=status,
                detail="测试详情",
                tool_calls=[],
                metrics={},
            )
            self.assertTrue(mock_md.called)
            args, _ = mock_md.call_args
            return args[0]

        cases = [
            ("done", "报告已完成", "#16a34a"),
            ("tool_calling", "正在调取数据", "#0284c7"),
            ("running", "正在研判分析", "#ea580c"),
            ("error", "执行异常", "#dc2626"),
            ("pending", "等待启动", "#e2e8f0"),
        ]
        for status, badge, color in cases:
            with self.subTest(status=status):
                mock_md.reset_mock()
                html_out = _rendered_html(status)
                self.assertIn(badge, html_out)
                self.assertIn(color, html_out)

    @patch("streamlit.expander")
    @patch("streamlit.progress")
    @patch("streamlit.columns")
    @patch("streamlit.markdown")
    def test_render_progress_renders_agent_matrix_and_telemetry(
        self, mock_md: MagicMock, mock_cols: MagicMock,
        mock_progress: MagicMock, mock_exp: MagicMock,
    ) -> None:
        """render_progress 输出 7 分析师矩阵 + 遥测 expander（原 debug monitor 并入此处）。"""
        from web.components.progress_panel import render_progress

        mock_cols.side_effect = lambda n=2, **kw: [MagicMock() for _ in range(n)]
        mock_exp.return_value.__enter__ = MagicMock()
        mock_exp.return_value.__exit__ = MagicMock()

        tracker = ProgressTracker(ticker="600519", trade_date="2026-08-16")
        tracker.set_agent_status("market", "done", "报告已生成 (500字)")
        tracker.record_agent_tool("social", "get_hot_stocks")

        render_progress(tracker)

        all_html = "\n".join(
            str(c.args[0]) for c in mock_md.call_args_list if c.args
        )
        self.assertIn("AI 分析师实时作业矩阵", all_html)
        self.assertIn("报告已完成", all_html)          # market 已 done 的卡片徽章
        mock_exp.assert_called()                       # 底层遥测 expander 存在
        mock_progress.assert_called()

    @patch("streamlit.columns")
    @patch("streamlit.markdown")
    @patch("streamlit.caption")
    def test_render_agent_diagnostics(
        self, mock_caption: MagicMock, mock_md: MagicMock, mock_cols: MagicMock
    ) -> None:
        """Verify render_agent_diagnostics parses final_state and renders diagnostics overview."""
        from web.components.report_viewer import render_agent_diagnostics

        mock_cols.side_effect = lambda n: [MagicMock() for _ in range(n if isinstance(n, int) else len(n))]

        final_state: dict[str, Any] = {
            "market_report": "技术面多头分析..." * 20,
            "sentiment_report": "情绪分析报告..." * 20,
            "news_report": "新闻舆情报告..." * 20,
            "policy_report": "政策研判报告..." * 20,
            "hot_money_report": "游资动向报告..." * 20,
            # fundamentals and lockup omitted (e.g. index analysis)
        }

        render_agent_diagnostics(final_state, ticker="000001.SH")
        self.assertTrue(mock_md.called)
        self.assertTrue(mock_cols.called)
        # Check metrics called on columns
        for col in mock_cols.return_value:
            self.assertTrue(col.metric.called)


class TestAgentDebugModule(unittest.TestCase):
    """Test web/agent_debug.py schemas, prompt builders, parsers, and UI components."""

    def test_analyst_specs_coverage(self) -> None:
        """Verify that all 7 analysts have complete debug specifications."""
        from web.agent_debug import ANALYST_DEBUG_SPECS

        self.assertEqual(len(ANALYST_DEBUG_SPECS), 7)
        for aid in ANALYST_AGENT_IDS:
            self.assertIn(aid, ANALYST_DEBUG_SPECS)
            spec = ANALYST_DEBUG_SPECS[aid]
            self.assertEqual(spec["id"], aid)
            self.assertTrue(len(spec["name"]) > 0)
            self.assertTrue(len(spec["persona"]) > 0)
            self.assertTrue(len(spec["special_rules"]) >= 3)
            self.assertTrue(len(spec["checklist"]) >= 4)
            self.assertTrue(len(spec["tools"]) >= 1)

    def test_build_agent_prompt_payload(self) -> None:
        """Verify prompt payload construction includes persona, rules, checklist, and params."""
        from web.agent_debug import build_agent_prompt_payload

        for aid in ANALYST_AGENT_IDS:
            payload = build_agent_prompt_payload(
                agent_id=aid,
                ticker="600519",
                trade_date="2026-08-16",
                company_name="贵州茅台",
                lookback=30,
            )
            self.assertEqual(payload["agent_id"], aid)
            self.assertIn("贵州茅台", payload["params"]["标的名称/上下文"])
            self.assertIn("600519", payload["full_prompt"])
            self.assertIn("2026-08-16", payload["full_prompt"])
            self.assertTrue(len(payload["checklist"]) >= 4)
            self.assertTrue(len(payload["special_rules"]) >= 3)

    def test_parse_agent_response_with_think_tags(self) -> None:
        """Verify parse_agent_response extracts chain of thought and clean content."""
        from web.agent_debug import parse_agent_response

        raw_llm = (
            "<think>\n"
            "首先检查茅台量价结构，5日均线金叉10日均线。\n"
            "MACD柱状图连续红柱放大，多头占优。\n"
            "</think>\n\n"
            "### 技术分析报告\n"
            "贵州茅台目前处于明确的上升通道中，建议逢低布局。"
        )

        raw, think, clean = parse_agent_response(raw_llm)
        self.assertEqual(raw, raw_llm)
        self.assertIn("首先检查茅台量价结构", think)
        self.assertIn("MACD柱状图连续红柱放大", think)
        self.assertNotIn("<think>", clean)
        self.assertNotIn("</think>", clean)
        self.assertIn("### 技术分析报告", clean)

    def test_parse_agent_response_direct_mode(self) -> None:
        """Verify parse_agent_response handles direct output without think tags."""
        from web.agent_debug import parse_agent_response

        raw_llm = "### 情绪分析报告\n散户看多情绪高涨，主力资金净流入 5.2 亿元。"
        raw, think, clean = parse_agent_response(raw_llm)
        self.assertEqual(raw, raw_llm)
        self.assertEqual(think, "")
        self.assertEqual(clean, raw_llm)

    def test_extract_agent_debug_from_state(self) -> None:
        """Verify extraction of tool calls, payload, and thinking from final_state."""
        from web.agent_debug import extract_agent_debug_from_state

        class MockAIMsg:
            def __init__(self, tool_calls: list[dict[str, Any]]) -> None:
                self.tool_calls = tool_calls

        class MockToolMsg:
            def __init__(self, name: str, tool_call_id: str, content: str) -> None:
                self.name = name
                self.tool_call_id = tool_call_id
                self.content = content

        final_state: dict[str, Any] = {
            "trade_date": "2026-08-16",
            "market_report": "<think>分析K线形态</think>### 技术分析\n均线多头排列。",
            "messages": [
                MockAIMsg([{"name": "get_stock_data", "id": "call_1", "args": {"ticker": "600519"}}]),
                MockToolMsg("get_stock_data", "call_1", '{"close": 1850.0, "volume": 25000}'),
            ],
        }

        diag = extract_agent_debug_from_state(final_state, "market", ticker="600519")
        self.assertEqual(diag["agent_id"], "market")
        self.assertEqual(diag["status"], "done")
        self.assertEqual(diag["think_content"], "分析K线形态")
        self.assertIn("### 技术分析", diag["report"])
        self.assertEqual(len(diag["tool_details"]), 1)
        self.assertEqual(diag["tool_details"][0]["tool_name"], "get_stock_data")
        self.assertEqual(diag["tool_details"][0]["args"], {"ticker": "600519"})
        self.assertIn("1850.0", diag["tool_details"][0]["payload"])

    def test_tracker_record_tool_detail_and_raw_output(self) -> None:
        """Verify ProgressTracker detailed tool payloads and raw responses tracking."""
        tracker = ProgressTracker(ticker="600519", trade_date="2026-08-16")
        tracker.is_running = True

        # 1. Record structured tool details
        tracker.record_agent_tool_detail(
            agent_id="hot_money",
            tool_name="get_dragon_tiger_board",
            args={"ticker": "600519", "curr_date": "2026-08-16"},
            payload={"buy_seats": ["机构专用", "中信证券上海分公司"], "net_buy": 120000000},
        )

        details = tracker.get_agent_tool_details("hot_money")
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0]["tool_name"], "get_dragon_tiger_board")
        self.assertEqual(details[0]["args"]["curr_date"], "2026-08-16")
        self.assertEqual(details[0]["payload"]["net_buy"], 120000000)

        # 2. Record raw output with think reasoning
        raw_output_text = "<think>\n主力持续买入\n</think>\n游资主力净流入明显。"
        tracker.record_agent_response("hot_money", raw_output_text)
        tracker.mark_stage_done("hot_money", "游资主力净流入明显。")

        snapshot = tracker.get_all_agent_states()
        self.assertEqual(snapshot["hot_money"]["status"], "done")
        self.assertIn("主力持续买入", snapshot["hot_money"]["think_content"])
        self.assertIn("游资主力净流入明显", snapshot["hot_money"]["clean_output"])
        self.assertEqual(len(snapshot["hot_money"]["tool_details"]), 1)

    @patch("streamlit.expander")
    @patch("streamlit.columns")
    @patch("streamlit.markdown")
    @patch("streamlit.tabs")
    @patch("streamlit.info")
    @patch("streamlit.code")
    @patch("streamlit.json")
    def test_render_agent_debug_panel(
        self,
        mock_json: MagicMock,
        mock_code: MagicMock,
        mock_info: MagicMock,
        mock_tabs: MagicMock,
        mock_md: MagicMock,
        mock_cols: MagicMock,
        mock_exp: MagicMock,
    ) -> None:
        """Verify render_agent_debug_panel executes all 4 tabs seamlessly."""
        from web.agent_debug import render_agent_debug_panel

        mock_cols.side_effect = lambda n: [MagicMock() for _ in range(n if isinstance(n, int) else len(n))]
        mock_tabs.return_value = [MagicMock(), MagicMock(), MagicMock(), MagicMock()]
        mock_exp.return_value.__enter__ = MagicMock()
        mock_exp.return_value.__exit__ = MagicMock()

        agent_state = {
            "name": "基本面分析师",
            "icon": "📋",
            "status": "done",
            "detail": "报告已生成 (1200字)",
            "tool_calls": ["get_fundamentals", "get_profit_forecast"],
            "tool_details": [
                {
                    "tool_name": "get_fundamentals",
                    "args": {"ticker": "600519"},
                    "payload": {"pe_ttm": 28.5, "roe": 0.31},
                    "status": "success",
                }
            ],
            "raw_output": "<think>估值处于历史中枢下方</think>### 基本面研报\n茅台盈利能力极强。",
            "report": "### 基本面研报\n茅台盈利能力极强。",
        }

        render_agent_debug_panel(
            agent_id="fundamentals",
            agent_state=agent_state,
            ticker="600519",
            trade_date="2026-08-16",
            company_name="贵州茅台",
            default_expanded=True,
        )

        mock_exp.assert_called()
        self.assertTrue(mock_tabs.called)
        self.assertTrue(mock_md.called)


if __name__ == "__main__":
    unittest.main()
