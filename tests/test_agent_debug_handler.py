"""Tests for AgentDebugCallbackHandler, ProgressTracker debug telemetry, and prompt/tool/response capture."""

from __future__ import annotations

import threading
import time
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from web.debug_handler import AgentDebugCallbackHandler
from web.progress import ANALYST_AGENTS, ProgressTracker, get_default_prompt_template


def test_default_prompt_templates_for_all_analysts() -> None:
    """Ensure all 7 analysts and downstream stages have valid default prompt previews."""
    expected_agents = [
        "market", "social", "news", "fundamentals", "policy", "hot_money", "lockup",
        "quality_gate", "debate", "trader", "risk", "pm",
    ]
    for aid in expected_agents:
        template = get_default_prompt_template(aid)
        assert template and len(template) > 20
        assert "暂无" not in template

    # Test aliases
    assert get_default_prompt_template("bull_researcher") == get_default_prompt_template("debate")
    assert get_default_prompt_template("aggressive_debator") == get_default_prompt_template("risk")
    assert get_default_prompt_template("portfolio_manager") == get_default_prompt_template("pm")


def test_progress_tracker_initialization_and_debug_methods() -> None:
    """Test ProgressTracker debug fields initialization and atomic getters."""
    tracker = ProgressTracker(ticker="600519", trade_date="2026-08-16")

    # Verify initialized fields
    for a in ANALYST_AGENTS:
        aid = a["id"]
        assert aid in tracker.agent_prompts
        assert aid in tracker.agent_tool_details
        assert aid in tracker.agent_raw_responses
        assert aid in tracker.agent_metrics
        assert tracker.agent_metrics[aid]["tokens_in"] == 0
        assert tracker.agent_metrics[aid]["tokens_out"] == 0
        assert tracker.agent_metrics[aid]["llm_calls"] == 0
        assert tracker.agent_metrics[aid]["tool_calls"] == 0

    # Test single debug info
    market_info = tracker.get_agent_debug_info("market")
    assert market_info["agent_id"] == "market"
    assert market_info["name"] == "技术分析师"
    assert market_info["icon"] == "📊"
    assert market_info["status"] == "pending"
    assert market_info["prompts"] == []
    assert market_info["tool_details"] == []
    assert market_info["raw_responses"] == []
    assert len(market_info["default_prompt_template"]) > 20

    # Test all debug info
    all_info = tracker.get_all_agent_debug_info()
    assert len(all_info) >= 7
    for aid in ("market", "social", "news", "fundamentals", "policy", "hot_money", "lockup"):
        assert aid in all_info
        assert all_info[aid]["agent_id"] == aid


def test_agent_debug_callback_chat_prompt_capture() -> None:
    """Test on_chat_model_start correctly extracts messages and identifies agents."""
    tracker = ProgressTracker(ticker="600519", trade_date="2026-08-16")
    handler = AgentDebugCallbackHandler(tracker)

    run_id = uuid4()
    sys_msg = SystemMessage(
        content="你是一位专注于 A 股市场的技术分析师。你的任务是从以下技术指标中选择指标。"
    )
    user_msg = HumanMessage(content="请分析 600519 在 2026-08-16 的技术走势。")
    messages = [[sys_msg, user_msg]]

    handler.on_chat_model_start(
        serialized={"name": "ChatOpenAI"},
        messages=messages,
        run_id=run_id,
        metadata={"langgraph_node": "Market Analyst"},
    )

    debug_info = tracker.get_agent_debug_info("market")
    prompts = debug_info["prompts"]
    assert len(prompts) == 2
    assert prompts[0]["role"] == "system"
    assert "技术分析师" in prompts[0]["content"]
    assert prompts[1]["role"] == "human"
    assert "600519" in prompts[1]["content"]
    assert tracker.get_agent_status("market") == "running"


def test_agent_identification_by_keywords_for_all_analysts() -> None:
    """Test keyword-based agent identification across all 7 analysts and downstream stages."""
    handler = AgentDebugCallbackHandler()

    test_cases = [
        ("market", "你是一位专注于 A 股市场的技术分析师。可选技术指标：close_50_sma, macd, rsi。"),
        ("social", "你是一位专注于 A 股市场的市场情绪分析师。散户情绪权重高，先看资金，再看新闻。"),
        ("news", "你是一位专注于 A 股市场的新闻与政策分析师。消息来源权重：财联社快讯。"),
        ("fundamentals", "你是一位专注于 A 股市场的基本面分析师。中国会计准则（CAS），资产负债表与利润表。"),
        ("policy", "你是一位专注于 A 股市场的政策分析师。全球最典型的「政策市」，宏观政策与监管政策。"),
        ("hot_money", "你是一位专注于 A 股市场的游资与资金流向追踪分析师。龙虎榜席位与主力资金。"),
        ("lockup", "你是一位专注于 A 股市场的解禁与减持监控分析师。限售股解禁日历与大股东减持新规。"),
        ("quality_gate", "你是数据质量审核员。对 7 位分析师报告进行硬检查结果与 LLM 复审。"),
        ("debate", "You are a Bull Analyst advocating for investing under the A-Share Bull Framework."),
        ("trader", "You are a trading agent specialising in A-share stocks. Translate the Research Manager's investment plan."),
        ("risk", "As the Aggressive Risk Analyst evaluating an A-share stock, A-Share Aggressive Framework."),
        ("pm", "As the Portfolio Manager, synthesize the risk analysts' debate and deliver the final trading decision."),
    ]

    for expected_agent, text in test_cases:
        identified = handler._identify_agent([SystemMessage(content=text)])
        assert identified == expected_agent, f"Expected {expected_agent}, got {identified} for text: {text[:40]}"


def test_agent_debug_callback_tool_capture() -> None:
    """Test tool invocation start, end, duration, and return payload capture."""
    tracker = ProgressTracker(ticker="600519", trade_date="2026-08-16")
    handler = AgentDebugCallbackHandler(tracker)

    tool_run_id = uuid4()
    handler.on_tool_start(
        serialized={"name": "get_stock_data"},
        input_str='{"ticker": "600519", "look_back_days": 30}',
        run_id=tool_run_id,
        metadata={"langgraph_node": "tools_market"},
    )

    # Status should reflect tool calling
    assert tracker.get_agent_status("market") == "tool_calling"
    assert "get_stock_data" in tracker.get_agent_tool_calls("market")

    time.sleep(0.01)  # small duration

    mock_output = {"Close": [1800.0, 1820.0, 1850.0], "Volume": [20000, 25000, 30000]}
    handler.on_tool_end(output=mock_output, run_id=tool_run_id)

    debug_info = tracker.get_agent_debug_info("market")
    tool_details = debug_info["tool_details"]
    assert len(tool_details) == 1
    assert tool_details[0]["tool_name"] == "get_stock_data"
    assert tool_details[0]["args"] == {"ticker": "600519", "look_back_days": 30}
    # 键名 payload：与 agent_debug.render_tools_tab 的读取侧一致（旧名 output 会让
    # 运行中面板的工具返回恒显示空态）
    assert tool_details[0]["payload"] == mock_output
    assert tool_details[0]["status"] == "success"
    assert tool_details[0]["duration"] >= 0.01

    # Check metrics
    metrics = debug_info["metrics"]
    assert metrics["tool_calls"] == 1
    assert metrics["total_duration"] >= 0.01


def test_agent_debug_callback_llm_end_with_thinking_and_tokens() -> None:
    """Test LLM response capture with <think> reasoning tags and token metadata."""
    tracker = ProgressTracker(ticker="600519", trade_date="2026-08-16")
    handler = AgentDebugCallbackHandler(tracker)

    run_id = uuid4()
    handler.on_chat_model_start(
        serialized={"name": "ChatOpenAI"},
        messages=[[SystemMessage(content="你是一位专注于 A 股市场的游资与资金流向追踪分析师。")]],
        run_id=run_id,
    )

    raw_response_content = "<think>\n分析主力资金净流入 5000 万，龙虎榜显示机构席位大买。\n</think>\n\n### 游资追踪报告\n资金面表现强势。"
    generation = ChatGeneration(
        message=AIMessage(
            content=raw_response_content,
            usage_metadata={"input_tokens": 150, "output_tokens": 80, "total_tokens": 230},
        )
    )
    llm_result = LLMResult(generations=[[generation]])

    handler.on_llm_end(response=llm_result, run_id=run_id)

    debug_info = tracker.get_agent_debug_info("hot_money")
    raw_responses = debug_info["raw_responses"]
    assert len(raw_responses) == 1
    assert "游资追踪报告" in raw_responses[0]
    assert "<think>" in raw_responses[0]
    assert "分析主力资金" in raw_responses[0]

    metrics = debug_info["metrics"]
    assert metrics["tokens_in"] == 150
    assert metrics["tokens_out"] == 80
    assert metrics["total_tokens"] == 230
    assert metrics["llm_calls"] == 1


def test_agent_debug_callback_llm_end_with_reasoning_content_attribute() -> None:
    """Test LLM response capture when reasoning_content is in additional_kwargs (DeepSeek style)."""
    tracker = ProgressTracker(ticker="600519", trade_date="2026-08-16")
    handler = AgentDebugCallbackHandler(tracker)

    run_id = uuid4()
    handler.on_chat_model_start(
        serialized={"name": "ChatOpenAI"},
        messages=[[SystemMessage(content="你是一位专注于 A 股市场的基本面分析师。CAS准则。")]],
        run_id=run_id,
    )

    ai_msg = AIMessage(
        content="### 基本面报告\n公司 ROE 为 30%。",
        additional_kwargs={"reasoning_content": "首先检查三表数据，现金流非常充沛。"},
        usage_metadata={"input_tokens": 200, "output_tokens": 100, "total_tokens": 300},
    )
    generation = ChatGeneration(message=ai_msg)
    llm_result = LLMResult(generations=[[generation]])

    handler.on_llm_end(response=llm_result, run_id=run_id)

    debug_info = tracker.get_agent_debug_info("fundamentals")
    raw_responses = debug_info["raw_responses"]
    assert len(raw_responses) == 1
    assert "首先检查三表数据" in raw_responses[0]
    assert "基本面报告" in raw_responses[0]


def test_agent_debug_callback_tool_error() -> None:
    """Test tool error recording in debug info."""
    tracker = ProgressTracker(ticker="600519", trade_date="2026-08-16")
    handler = AgentDebugCallbackHandler(tracker)

    tool_run_id = uuid4()
    handler.on_tool_start(
        serialized={"name": "get_lockup_expiry"},
        input_str='{"ticker": "600519"}',
        run_id=tool_run_id,
    )

    handler.on_tool_error(
        error=ValueError("Network timeout while fetching lockup calendar"),
        run_id=tool_run_id,
    )

    debug_info = tracker.get_agent_debug_info("lockup")
    tool_details = debug_info["tool_details"]
    assert len(tool_details) == 1
    assert tool_details[0]["status"] == "error"
    assert "Network timeout" in tool_details[0]["payload"]


def test_tracker_stop_request_and_mark_stopped_clears_debug_info() -> None:
    """Ensure stop requests and mark_stopped cleanly reset debug telemetry."""
    tracker = ProgressTracker(ticker="600519", trade_date="2026-08-16")
    tracker.is_running = True
    handler = AgentDebugCallbackHandler(tracker)

    run_id = uuid4()
    handler.on_chat_model_start(
        serialized={},
        messages=[[SystemMessage(content="你是一位专注于 A 股市场的政策分析师。")]],
        run_id=run_id,
    )

    assert len(tracker.get_agent_debug_info("policy")["prompts"]) == 1

    assert tracker.request_stop()
    assert tracker.get_agent_debug_info("policy")["prompts"] == []
    assert tracker.get_agent_debug_info("policy")["tool_details"] == []
    assert tracker.get_agent_debug_info("policy")["raw_responses"] == []

    tracker.mark_stopped()
    assert tracker.get_agent_debug_info("policy")["prompts"] == []


def test_thread_safety_under_concurrent_updates() -> None:
    """Test concurrent prompt and tool updates from multiple threads."""
    tracker = ProgressTracker(ticker="600519", trade_date="2026-08-16")
    handler = AgentDebugCallbackHandler(tracker)

    def worker(agent_idx: int) -> None:
        agent_names = ["market", "social", "news", "fundamentals", "policy", "hot_money", "lockup"]
        aid = agent_names[agent_idx % len(agent_names)]
        for i in range(20):
            rid = uuid4()
            handler.on_chat_model_start(
                serialized={},
                messages=[[SystemMessage(content=f"你是{aid}分析师测试 {i}")]],
                run_id=rid,
                metadata={"langgraph_node": aid},
            )
            tool_rid = uuid4()
            handler.on_tool_start(
                serialized={"name": f"test_tool_{aid}"},
                input_str=f'{{"step": {i}}}',
                run_id=tool_rid,
                metadata={"langgraph_node": aid},
            )
            handler.on_tool_end(output=f"result_{i}", run_id=tool_rid)
            gen = ChatGeneration(
                message=AIMessage(
                    content=f"Report {i}",
                    usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                )
            )
            handler.on_llm_end(response=LLMResult(generations=[[gen]]), run_id=rid)
            # Concurrent read
            tracker.get_all_agent_debug_info()
            tracker.get_agent_debug_info(aid)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    all_debug = tracker.get_all_agent_debug_info()
    assert len(all_debug) >= 7
