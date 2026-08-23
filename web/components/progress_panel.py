import html
import textwrap
import time
from typing import Any

import streamlit as st

from web.progress import ANALYST_AGENTS, PIPELINE_STAGES, ProgressTracker


_TOOL_LABELS: dict[str, str] = {
    "get_stock_data": "日K线行情",
    "get_indicators": "技术指标计算",
    "get_balance_sheet": "资产负债表",
    "get_income_statement": "利润表",
    "get_cashflow": "现金流量表",
    "get_fundamentals": "财务核心指标",
    "get_profit_forecast": "机构盈利预测",
    "get_industry_comparison": "行业横向对标",
    "get_news": "个股权威资讯",
    "get_global_news": "宏观/产业要闻",
    "get_fund_flow": "资金流向",
    "get_dragon_tiger_board": "龙虎榜席位",
    "get_northbound_flow": "北向资金",
    "get_concept_blocks": "所属概念题材",
    "get_hot_stocks": "强势股榜单",
    "get_lockup_expiry": "限售解禁日历",
    "get_insider_transactions": "大股东减持",
}


def _format_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def _get_agent_progress_pct(status: str, tool_count: int) -> int:
    """Estimate a percentage progress for an individual agent."""
    if status == "done":
        return 100
    elif status == "tool_calling":
        return min(70, 30 + tool_count * 15)
    elif status == "running":
        return 80 if tool_count > 0 else 40
    elif status == "error":
        return 100
    else:  # pending
        return 5


def _render_agent_card(
    agent_id: str,
    name: str,
    icon: str,
    desc: str,
    status: str,
    detail: str,
    tool_calls: list[str],
    metrics: dict[str, Any],
) -> None:
    """Render an individual live status card for an analyst agent."""
    pct = _get_agent_progress_pct(status, len(tool_calls))

    # Color & badge schemes
    if status == "done":
        border_color = "#16a34a"
        bg_color = "#f0fdf4"
        badge_bg = "#dcfce7"
        badge_color = "#15803d"
        badge_text = "🟢 报告已完成"
        bar_color = "#16a34a"
    elif status == "tool_calling":
        border_color = "#0284c7"
        bg_color = "#f0f9ff"
        badge_bg = "#e0f2fe"
        badge_color = "#0369a1"
        badge_text = "🔧 正在调取数据"
        bar_color = "#0284c7"
    elif status == "running":
        border_color = "#ea580c"
        bg_color = "#fff7ed"
        badge_bg = "#ffedd5"
        badge_color = "#c2410c"
        badge_text = "🧠 正在研判分析"
        bar_color = "#ea580c"
    elif status == "error":
        border_color = "#dc2626"
        bg_color = "#fef2f2"
        badge_bg = "#fee2e2"
        badge_color = "#b91c1c"
        badge_text = "🔴 执行异常"
        bar_color = "#dc2626"
    else:  # pending
        border_color = "#e2e8f0"
        bg_color = "#f8fafc"
        badge_bg = "#f1f5f9"
        badge_color = "#64748b"
        badge_text = "⏳ 等待启动"
        bar_color = "#cbd5e1"

    # Escape detail text
    safe_detail = html.escape(detail or ("等待调度..." if status == "pending" else "正在处理..."))
    safe_desc = html.escape(desc)

    # Render tool badges HTML
    tool_badges_html = ""
    if tool_calls:
        badges = []
        for tc in tool_calls[:4]:
            t_label = _TOOL_LABELS.get(tc, tc)
            badges.append(
                f'<span style="display:inline-block; background:#ffffff; border:1px solid #cbd5e1; '
                f'border-radius:4px; padding:1px 6px; font-size:0.7rem; color:#334155; margin-right:4px; margin-top:3px;">'
                f'🛠️ {html.escape(t_label)}</span>'
            )
        tool_badges_html = "".join(badges)
        if len(tool_calls) > 4:
            tool_badges_html += f'<span style="font-size:0.7rem; color:#64748b; margin-top:3px;">+{len(tool_calls)-4}</span>'

    # Metrics line
    tok_in = metrics.get("tokens_in", 0)
    tok_out = metrics.get("tokens_out", 0)
    duration = metrics.get("total_duration", 0.0)
    metric_text = ""
    if duration > 0 or tok_in + tok_out > 0:
        metric_text = f"⏱️ {duration:.1f}s · 📝 {tok_in + tok_out:,} tok"

    card_html = textwrap.dedent(f"""
    <div style="background:{bg_color}; border:1.5px solid {border_color}; border-radius:8px; padding:12px 14px; margin-bottom:12px; box-shadow:0 1px 3px rgba(0,0,0,0.04);">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
            <div style="font-weight:700; font-size:0.95rem; color:#0f172a; display:flex; align-items:center; gap:6px;">
                <span>{icon}</span> <span>{name}</span>
            </div>
            <span style="background:{badge_bg}; color:{badge_color}; font-size:0.72rem; font-weight:700; padding:2px 8px; border-radius:12px; border:1px solid {border_color};">
                {badge_text}
            </span>
        </div>
        <div style="font-size:0.75rem; color:#64748b; margin-bottom:8px; line-height:1.3;">
            {safe_desc}
        </div>
        <div style="background:#e2e8f0; border-radius:4px; height:6px; width:100%; overflow:hidden; margin-bottom:8px;">
            <div style="background:{bar_color}; height:100%; width:{pct}%;"></div>
        </div>
        <div style="font-size:0.8rem; color:#1e293b; font-weight:500; margin-bottom:6px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">
            📌 {safe_detail}
        </div>
        <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; font-size:0.72rem; color:#64748b;">
            <div style="display:flex; flex-wrap:wrap; align-items:center;">
                {tool_badges_html}
            </div>
            <div style="font-size:0.7rem; color:#64748b; margin-top:3px;">
                {metric_text}
            </div>
        </div>
    </div>
    """).strip()
    st.markdown(card_html, unsafe_allow_html=True)


def render_progress(tracker: ProgressTracker) -> None:
    """Render the comprehensive pipeline progress panel with live 7-Agent matrix."""

    stages = tracker.stages()
    _ANALYST_IDS = {s["id"] for s in PIPELINE_STAGES[:7]}
    analyst_stages = [s for s in stages if s["id"] in _ANALYST_IDS]
    post_stages = [s for s in stages if s["id"] not in _ANALYST_IDS]

    completed = len(tracker.completed_stages)
    total = len(stages)
    pct = completed / total if total else 0

    # Header
    header_html = textwrap.dedent(f"""
    <div style="display:flex; justify-content:space-between; align-items:flex-end; margin:0.8rem 0 0.4rem; padding-bottom:0.6rem; border-bottom:1px solid #e2e8f0;">
        <div>
            <span style="font-size:1.5rem; font-weight:800; color:#0f172a;">⚡ A股投研决策流水线</span>
            <span style="font-size:1.1rem; font-weight:600; color:#ff5a1f; margin-left:0.8rem;">{tracker.ticker}</span>
            <span style="font-size:0.9rem; color:#64748b; margin-left:0.5rem;">({tracker.trade_date})</span>
        </div>
        <div style="font-size:0.95rem; font-weight:700; color:#0284c7;">
            ⏱️ 耗时 {_format_time(tracker.elapsed)}
        </div>
    </div>
    """).strip()
    st.markdown(header_html, unsafe_allow_html=True)

    if tracker.stop_requested:
        st.warning("⚠️ 正在停止当前分析并清空内容；收尾完成后可重新开始。")
        return

    if tracker.is_paused:
        st.info("⏸️ 当前分析已暂停。")

    # Global Progress Bar
    current_stage_name = "7位垂直领域分析师并发调研"
    if tracker.current_stage:
        for s in stages:
            if s["id"] == tracker.current_stage:
                current_stage_name = s["name"]
                break
    elif completed >= total:
        current_stage_name = "分析完成"

    st.progress(pct, text=f"全流程进度：{completed}/{total} 阶段已就绪 ({int(pct*100)}%)  ·  当前阶段: {current_stage_name}")

    # Top Metrics Row
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("LLM 智能推理", f"{tracker.llm_calls} 次")
    c2.metric("金融数据工具调用", f"{tracker.tool_calls} 次")
    c3.metric("输入 Tokens", f"{tracker.tokens_in:,}")
    c4.metric("输出 Tokens", f"{tracker.tokens_out:,}")

    st.markdown("<hr style='margin:0.8rem 0; border-color:#e2e8f0;'>", unsafe_allow_html=True)

    # ========================================================================
    # 🌟 7 大分析师独立实时作业看板 (Live Agent Matrix)
    # ========================================================================
    analyst_states = tracker.get_all_agent_states()
    done_analysts = sum(1 for s in analyst_stages if tracker.stage_status(s["id"]) == "done")
    total_analysts = len(analyst_stages)

    matrix_title_html = textwrap.dedent(f"""
    <div style="display:flex; justify-content:space-between; align-items:center; margin:0.6rem 0 0.8rem;">
        <div style="font-size:1.1rem; font-weight:800; color:#0f172a;">
            🤖 7 位 AI 分析师实时作业矩阵
        </div>
        <div style="font-size:0.85rem; font-weight:700; color:{'#16a34a' if done_analysts == total_analysts else '#ea580c'};">
            已就绪 {done_analysts}/{total_analysts} 位分析师 ({int((done_analysts/total_analysts)*100 if total_analysts else 0)}%)
        </div>
    </div>
    """).strip()
    st.markdown(matrix_title_html, unsafe_allow_html=True)

    # 2-column grid for clear visibility of each agent's active sub-progress
    col_left, col_right = st.columns(2)
    for idx, stage in enumerate(analyst_stages):
        aid = stage["id"]
        meta = analyst_states.get(aid, {})
        status = meta.get("status", tracker.stage_status(aid) or "pending")
        if tracker.stage_status(aid) == "done":
            status = "done"
        detail = meta.get("detail", tracker.get_agent_detail(aid))
        tool_calls = meta.get("tool_calls", tracker.get_agent_tool_calls(aid))
        metrics = meta.get("metrics", {})

        target_col = col_left if idx % 2 == 0 else col_right
        with target_col:
            _render_agent_card(
                agent_id=aid,
                name=stage["name"],
                icon=stage.get("icon", "📊"),
                desc=ANALYST_AGENTS[idx]["desc"] if idx < len(ANALYST_AGENTS) else "",
                status=status,
                detail=detail,
                tool_calls=tool_calls,
                metrics=metrics,
            )

    st.markdown("<hr style='margin:0.8rem 0; border-color:#e2e8f0;'>", unsafe_allow_html=True)

    # ========================================================================
    # ⚔️ 下游决策流水线阶段 (Post-Analyst Pipeline)
    # ========================================================================
    st.markdown(
        '<div style="font-size:1.05rem; font-weight:800; color:#0f172a; margin-bottom:0.6rem;">'
        '🏛️ 投研决策流水线阶段 (Pipeline Flow)</div>',
        unsafe_allow_html=True,
    )

    cols_pipe = st.columns(len(post_stages))
    for col, stage in zip(cols_pipe, post_stages):
        sid = stage["id"]
        status = tracker.stage_status(sid)
        is_active = (tracker.current_stage == sid) or (status == "active")
        is_done = (status == "done")

        if is_done:
            badge_icon = "🟢"
            badge_txt = "已就绪"
            b_color = "#16a34a"
            bg = "#f0fdf4"
        elif is_active:
            badge_icon = "🟠"
            badge_txt = "进行中"
            b_color = "#ea580c"
            bg = "#fff7ed"
        else:
            badge_icon = "⚪"
            badge_txt = "待开始"
            b_color = "#94a3b8"
            bg = "#f8fafc"

        pipe_card_html = textwrap.dedent(f"""
        <div style="background:{bg}; border:1.5px solid {b_color}; border-radius:8px; padding:8px 4px; text-align:center; box-shadow:0 1px 2px rgba(0,0,0,0.03);">
            <div style="font-size:1.3rem; margin-bottom:2px;">{stage.get('icon', '📌')}</div>
            <div style="font-size:0.85rem; font-weight:700; color:#0f172a;">{stage['name']}</div>
            <div style="font-size:0.7rem; font-weight:700; color:{b_color}; margin-top:2px;">
                {badge_icon} {badge_txt}
            </div>
        </div>
        """).strip()
        col.markdown(pipe_card_html, unsafe_allow_html=True)

    # Telemetry Monitor
    st.markdown("<div style='margin-top:1rem;'></div>", unsafe_allow_html=True)
    with st.expander("🔬 底层 Agent 详细遥测与 Prompt/Tool 交互记录 (Debug Details)", expanded=False):
        from web.agent_debug import render_agent_debug_panel
        for aid, data in analyst_states.items():
            render_agent_debug_panel(
                agent_id=aid,
                agent_state=data,
                ticker=tracker.ticker,
                trade_date=tracker.trade_date,
                default_expanded=False,
            )

    if tracker.error:
        st.error(f"❌ 运行异常: {tracker.error}")

    # ========================================================================
    # 📑 已产出报告实时预览 (Live Reports)
    # ========================================================================
    completed_reports = [
        (stage["name"], stage.get("icon", "📄"), tracker.stage_reports[stage["id"]])
        for stage in stages
        if stage["id"] in tracker.stage_reports
    ]

    if completed_reports:
        st.markdown("<hr style='margin:1rem 0; border-color:#e2e8f0;'>", unsafe_allow_html=True)
        st.markdown(
            f'<div style="font-size:1.05rem; font-weight:800; color:#0f172a; margin-bottom:0.6rem;">'
            f'📑 已就绪分析报告 ({len(completed_reports)} 份)</div>',
            unsafe_allow_html=True,
        )
        for name, icon, report in reversed(completed_reports):
            is_latest = (name == completed_reports[-1][0])
            with st.expander(f"{icon} {name} 报告（已生成）", expanded=is_latest):
                st.markdown(report[:4000])

