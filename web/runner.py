"""Background thread runner for TradingAgentsGraph pipeline."""

from __future__ import annotations

import re
import threading
import traceback
from typing import Any

from web.history import clear_incomplete_task, record_incomplete_task
from web.progress import PIPELINE_STAGES, ProgressTracker
from web.stock_display import normalize_report_state_mentions, normalize_stock_mentions


_REPORT_KEY_TO_STAGE = {s["report_key"]: s["id"] for s in PIPELINE_STAGES}

_ANALYST_REPORT_KEYS = [
    "market_report", "sentiment_report", "news_report",
    "fundamentals_report", "policy_report", "hot_money_report", "lockup_report",
]

_ANALYST_NODE_MAP = {
    "Market Analyst": "market",
    "Social Analyst": "social",
    "News Analyst": "news",
    "Fundamentals Analyst": "fundamentals",
    "Policy Analyst": "policy",
    "Hot_money Analyst": "hot_money",
    "Lockup Analyst": "lockup",
}

_TOOL_NODE_MAP = {
    "tools_market": "market",
    "tools_social": "social",
    "tools_news": "news",
    "tools_fundamentals": "fundamentals",
    "tools_policy": "policy",
    "tools_hot_money": "hot_money",
    "tools_lockup": "lockup",
}

_KNOWN_TOOL_AGENT_MAP = {
    "get_stock_data": "market",
    "get_indicators": "market",
    "get_balance_sheet": "fundamentals",
    "get_cashflow": "fundamentals",
    "get_income_statement": "fundamentals",
    "get_profit_forecast": "fundamentals",
    "get_industry_comparison": "fundamentals",
    "get_fundamentals": "fundamentals",
    "get_lockup_expiry": "lockup",
    "get_dragon_tiger_board": "hot_money",
    "get_northbound_flow": "hot_money",
    "get_concept_blocks": "hot_money",
    "get_fund_flow": "hot_money",
    "get_global_news": "news",
    "get_news": "news",
    "get_hot_stocks": "hot_money",
    "get_insider_transactions": "lockup",
}


def _extract_tools_from_messages(messages: Any) -> list[str]:
    """Extract tool names from message list or object."""
    if not isinstance(messages, (list, tuple)):
        return []
    tools = []
    for msg in messages:
        name = getattr(msg, "name", None)
        if not name and isinstance(msg, dict):
            name = msg.get("name")
        if name:
            tools.append(str(name))

        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls and isinstance(msg, dict):
            tool_calls = msg.get("tool_calls")
        if tool_calls and isinstance(tool_calls, (list, tuple)):
            for tc in tool_calls:
                tc_name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
                if tc_name:
                    tools.append(str(tc_name))
    return tools


def _discard_stopped_run(
    ticker: str,
    trade_date: str,
    config: dict,
    tracker: ProgressTracker,
) -> None:
    """Clear resumable artifacts for a user-stopped run."""
    from tradingagents.graph.checkpointer import clear_checkpoint

    clear_incomplete_task(ticker, trade_date)
    clear_checkpoint(config["data_cache_dir"], ticker, trade_date)
    tracker.mark_stopped()


def _strip_think_tags(text: str) -> str:
    """Remove <think>...</think> blocks from LLM output."""
    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()


def _detect_completed_stages(
    chunk: dict[str, Any],
    tracker: ProgressTracker,
) -> None:
    """Check the streamed chunk for newly completed stages and agent events."""
    if not isinstance(chunk, dict):
        return

    # 1. 检查节点级别事件（LangGraph updates 模式下 chunk 键为节点名）
    for node_name, node_output in chunk.items():
        if node_name in _ANALYST_NODE_MAP:
            aid = _ANALYST_NODE_MAP[node_name]
            if tracker.get_agent_status(aid) not in ("done", "error"):
                tracker.set_agent_status(aid, "running", "正在分析数据与生成报告...")
                if isinstance(node_output, dict):
                    msgs = node_output.get("messages", [])
                    for tool_name in _extract_tools_from_messages(msgs):
                        tracker.record_agent_tool(aid, tool_name)
        elif node_name in _TOOL_NODE_MAP:
            aid = _TOOL_NODE_MAP[node_name]
            tools = []
            if isinstance(node_output, dict):
                tools = _extract_tools_from_messages(node_output.get("messages", []))
            elif isinstance(node_output, list):
                tools = _extract_tools_from_messages(node_output)

            if tools:
                for tool_name in tools:
                    tracker.record_agent_tool(aid, tool_name)
            elif tracker.get_agent_status(aid) != "done":
                tracker.set_agent_status(aid, "tool_calling", "正在执行工具查询...")

    # 2. 检查 messages（LangGraph values 模式）
    messages = chunk.get("messages", [])
    if isinstance(messages, (list, tuple)) and messages:
        recent_msgs = messages[-5:]
        for msg in recent_msgs:
            name = getattr(msg, "name", None) or (isinstance(msg, dict) and msg.get("name"))
            if name and str(name) in _KNOWN_TOOL_AGENT_MAP:
                aid = _KNOWN_TOOL_AGENT_MAP[str(name)]
                if tracker.get_agent_status(aid) != "done":
                    tracker.record_agent_tool(aid, str(name))

            tcs = getattr(msg, "tool_calls", None) or (isinstance(msg, dict) and msg.get("tool_calls"))
            if tcs and isinstance(tcs, (list, tuple)):
                for tc in tcs:
                    tc_name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
                    if tc_name and str(tc_name) in _KNOWN_TOOL_AGENT_MAP:
                        aid = _KNOWN_TOOL_AGENT_MAP[str(tc_name)]
                        if tracker.get_agent_status(aid) != "done":
                            tracker.record_agent_tool(aid, str(tc_name))

    # 3. 检查 7 位分析师报告产出
    for report_key in _ANALYST_REPORT_KEYS:
        stage_id = _REPORT_KEY_TO_STAGE[report_key]
        content = chunk.get(report_key, "")
        if not content and isinstance(chunk, dict):
            for v in chunk.values():
                if isinstance(v, dict) and report_key in v:
                    content = v[report_key]
                    break
        if content and tracker.stage_status(stage_id) != "done":
            report = normalize_stock_mentions(str(content), tracker.ticker, chunk)
            clean_report = _strip_think_tags(report)
            if hasattr(tracker, "record_agent_response"):
                tracker.record_agent_response(stage_id, str(content))
            tracker.mark_stage_done(stage_id, clean_report)

    # 4. 后续流水线节点检查
    dqs = chunk.get("data_quality_summary", "")
    if not dqs:
        for v in chunk.values():
            if isinstance(v, dict) and "data_quality_summary" in v:
                dqs = v["data_quality_summary"]
                break
    if dqs and tracker.stage_status("quality_gate") != "done":
        tracker.mark_stage_done("quality_gate", normalize_stock_mentions(str(dqs), tracker.ticker, chunk))

    debate = chunk.get("investment_debate_state")
    if not debate:
        for v in chunk.values():
            if isinstance(v, dict) and "investment_debate_state" in v:
                debate = v["investment_debate_state"]
                break
    if debate and isinstance(debate, dict):
        judge = debate.get("judge_decision", "")
        if judge and tracker.stage_status("debate") != "done":
            tracker.mark_stage_done("debate", normalize_stock_mentions(str(judge), tracker.ticker, chunk))

    trader_plan = chunk.get("trader_investment_plan", "")
    if not trader_plan:
        for v in chunk.values():
            if isinstance(v, dict) and "trader_investment_plan" in v:
                trader_plan = v["trader_investment_plan"]
                break
    if trader_plan and tracker.stage_status("trader") != "done":
        report = normalize_stock_mentions(str(trader_plan), tracker.ticker, chunk)
        tracker.mark_stage_done("trader", _strip_think_tags(report))

    risk = chunk.get("risk_debate_state")
    if not risk:
        for v in chunk.values():
            if isinstance(v, dict) and "risk_debate_state" in v:
                risk = v["risk_debate_state"]
                break
    if risk and isinstance(risk, dict):
        risk_judge = risk.get("judge_decision", "")
        if risk_judge and tracker.stage_status("risk") != "done":
            tracker.mark_stage_done("risk", normalize_stock_mentions(str(risk_judge), tracker.ticker, chunk))

    final = chunk.get("final_trade_decision", "")
    if not final:
        for v in chunk.values():
            if isinstance(v, dict) and "final_trade_decision" in v:
                final = v["final_trade_decision"]
                break
    if final and tracker.stage_status("pm") != "done":
        report = normalize_stock_mentions(str(final), tracker.ticker, chunk)
        tracker.mark_stage_done("pm", _strip_think_tags(report))


def _infer_active_stage(tracker: ProgressTracker) -> None:
    """Set the current_stage to the first non-completed stage."""
    for stage in tracker.stages():
        if tracker.stage_status(stage["id"]) == "pending":
            tracker.mark_stage_active(stage["id"])
            return


def _run(ticker: str, trade_date: str, config: dict, tracker: ProgressTracker) -> None:
    """Execute the full pipeline in the current thread."""
    from cli.stats_handler import StatsCallbackHandler
    from web.debug_handler import AgentDebugCallbackHandler
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    stats = StatsCallbackHandler()
    debug_callback = AgentDebugCallbackHandler(tracker)
    callbacks = [debug_callback, stats]

    # 指数模式换指数图（指数版分析师/辩论/决策 prompt + 分析师预设），
    # 个股模式与原来完全一致。
    if config.get("instrument_type") == "index":
        from tradingagents.graph.index_graph import TradingAgentsIndexGraph

        graph_cls = TradingAgentsIndexGraph
    else:
        graph_cls = TradingAgentsGraph

    selected_analysts = config.get(
        "selected_analysts",
        ["market", "social", "news", "fundamentals", "policy", "hot_money", "lockup"],
    )
    if config.get("instrument_type") == "index":
        graph = graph_cls(
            debug=True,
            config=config,
            callbacks=callbacks,
        )
    else:
        graph = graph_cls(
            selected_analysts=selected_analysts,
            debug=True,
            config=config,
            callbacks=callbacks,
        )

    init_state, args, _ = graph.prepare_graph_run(
        ticker,
        trade_date,
        callbacks=callbacks,
    )

    last_chunk: dict[str, Any] = {}

    try:
        def _close_and_discard() -> None:
            graph.close_graph_run()
            _discard_stopped_run(ticker, trade_date, config, tracker)

        if tracker.stop_requested:
            _close_and_discard()
            return

        stream = graph.graph.stream(init_state, **args)
        while True:
            tracker.wait_if_paused()
            if tracker.stop_requested:
                _close_and_discard()
                return
            try:
                chunk = next(stream)
            except StopIteration:
                break

            if tracker.stop_requested:
                _close_and_discard()
                return

            last_chunk = chunk
            _detect_completed_stages(chunk, tracker)
            _infer_active_stage(tracker)
            record_incomplete_task(
                ticker,
                trade_date,
                status="paused" if tracker.is_paused else "running",
                completed_stages=tracker.completed_stages,
            )

            s = stats.get_stats()
            tracker.update_stats(s["llm_calls"], s["tool_calls"], s["tokens_in"], s["tokens_out"])

        if tracker.stop_requested:
            _close_and_discard()
            return

        if not last_chunk:
            raise RuntimeError("分析没有返回任何结果，请清理断点后重试。")

        # #55: 报告标的统一显示为「代码+名称」，须在 finalize 落盘前归一化 last_chunk
        normalize_report_state_mentions(last_chunk, ticker)

        signal = graph.finalize_graph_run(ticker, trade_date, last_chunk)
        if tracker.stop_requested:
            _close_and_discard()
            return

        tracker.mark_complete(last_chunk, signal)
        clear_incomplete_task(ticker, trade_date)
    finally:
        graph.close_graph_run()


def run_analysis_in_thread(
    ticker: str,
    trade_date: str,
    config: dict,
    tracker: ProgressTracker,
) -> threading.Thread:
    """Launch the pipeline in a daemon thread. Returns the thread handle."""
    tracker.ticker = ticker
    tracker.trade_date = trade_date
    tracker.is_running = True
    selected = config.get(
        "selected_analysts",
        ["market", "social", "news", "fundamentals", "policy", "hot_money", "lockup"],
    )
    for aid in selected:
        if aid in tracker.agent_statuses:
            tracker.set_agent_status(aid, "running", "准备启动分析...")
    tracker.mark_stage_active("market")
    record_incomplete_task(
        ticker,
        trade_date,
        status="running",
        completed_stages=tracker.completed_stages,
    )

    def _target() -> None:
        try:
            _run(ticker, trade_date, config, tracker)
        except Exception as exc:
            if tracker.stop_requested:
                try:
                    _discard_stopped_run(ticker, trade_date, config, tracker)
                except Exception:
                    traceback.print_exc()
                return
            traceback.print_exc()
            record_incomplete_task(
                ticker,
                trade_date,
                status="error",
                error=str(exc),
                completed_stages=tracker.completed_stages,
            )
            tracker.mark_error(str(exc))

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    return t
