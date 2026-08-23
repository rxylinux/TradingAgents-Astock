import time

import streamlit as st

from web.progress import ANALYST_AGENTS, PIPELINE_STAGES, ProgressTracker


def _status_badge(status: str) -> str:
    if status == "done":
        return '<span style="color:#16a34a; font-size:1.3rem;">●</span>'
    if status == "active":
        return '<span style="color:#ff5a1f; font-size:1.3rem;">◉</span>'
    return '<span style="color:#cbd5e1; font-size:1.3rem;">○</span>'


def _agent_status_badge(status: str) -> tuple[str, str, str]:
    """Return (badge_html, text_label, border_color)."""
    if status == "done":
        return '<span style="color:#22c55e; font-weight:700;">🟢 已完成</span>', "已完成", "#22c55e"
    elif status == "tool_calling":
        return '<span style="color:#38bdf8; font-weight:700;">🔧 正在调用工具</span>', "正在调用工具", "#38bdf8"
    elif status == "running":
        return '<span style="color:#f59e0b; font-weight:700;">🟠 正在分析</span>', "正在分析", "#f59e0b"
    elif status == "error":
        return '<span style="color:#ef4444; font-weight:700;">🔴 异常</span>', "执行异常", "#ef4444"
    else:  # pending
        return '<span style="color:#888888; font-weight:700;">⚪ 等待中</span>', "等待中", "#444444"


def _format_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def render_debug_monitor(tracker: ProgressTracker, default_expanded: bool = True) -> None:
    """Render the 7-Agent independent status and debug monitoring panel."""
    from web.agent_debug import render_agent_debug_panel

    agent_states = tracker.get_all_agent_states()

    with st.expander("🔬 7 大 Agent 独立工作状态监控 (Debug Monitor)", expanded=default_expanded):
        done_count = sum(1 for a in agent_states.values() if a["status"] == "done")
        tool_count = sum(1 for a in agent_states.values() if a["status"] == "tool_calling")
        running_count = sum(1 for a in agent_states.values() if a["status"] == "running")
        total_tool_calls = sum(len(a.get("tool_details") or a.get("tool_calls", [])) for a in agent_states.values())

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("已就绪 Agent", f"{done_count}/7")
        m2.metric("分析中 Agent", f"{running_count}")
        m3.metric("工具调用中", f"{tool_count}")
        m4.metric("累计调用工具", f"{total_tool_calls} 次")

        st.markdown("<hr style='margin:0.6rem 0; border-color:#e2e8f0;'>", unsafe_allow_html=True)

        for aid, data in agent_states.items():
            # If agent is active or completed with content, expand for immediate insight
            is_active = data["status"] in ("running", "tool_calling")
            render_agent_debug_panel(
                agent_id=aid,
                agent_state=data,
                ticker=tracker.ticker,
                trade_date=tracker.trade_date,
                default_expanded=is_active,
            )



def render_progress(tracker: ProgressTracker) -> None:
    """Render the pipeline progress panel."""

    st.markdown(
        f"""
        <div style="text-align:center; margin:1rem 0 0.5rem;">
            <span style="font-size:1.6rem; font-weight:700; color:#0f172a;">
                分析进行中
            </span>
            <span style="font-size:1.1rem; color:#64748b; margin-left:0.8rem;">
                {tracker.ticker}
            </span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if tracker.stop_requested:
        st.caption("正在停止当前分析并清空内容；收尾完成后可重新开始。")
        return

    if tracker.is_paused:
        st.caption("当前分析已暂停。")

    # 指数模式的阶段列表被裁剪（无基本面/解禁），以 tracker 为准
    stages = tracker.stages()
    completed = len(tracker.completed_stages)
    total = len(stages)
    pct = completed / total if total else 0
    st.progress(pct, text=f"{completed}/{total} 阶段完成  ·  {_format_time(tracker.elapsed)}")

    _ANALYST_IDS = {s["id"] for s in PIPELINE_STAGES[:7]}
    analyst_stages = [s for s in stages if s["id"] in _ANALYST_IDS]
    post_stages = [s for s in stages if s["id"] not in _ANALYST_IDS]

    st.markdown(
        '<div style="margin:0.5rem 0 0.3rem; font-size:0.85rem; color:#64748b; font-weight:600;">ANALYSTS</div>',
        unsafe_allow_html=True,
    )

    cols = st.columns(len(analyst_stages))
    for col, stage in zip(cols, analyst_stages):
        status = tracker.stage_status(stage["id"])
        badge = _status_badge(status)
        label_color = "#ff5a1f" if status == "active" else "#94a3b8" if status == "pending" else "#16a34a"
        col.markdown(
            f"""
            <div style="text-align:center; padding:0.5rem 0;">
                {badge}<br>
                <span style="font-size:0.75rem; color:{label_color}; font-weight:600;">{stage['name']}</span>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.markdown(
        '<div style="margin:0.8rem 0 0.3rem; font-size:0.85rem; color:#64748b; font-weight:600;">PIPELINE</div>',
        unsafe_allow_html=True,
    )

    cols2 = st.columns(len(post_stages))
    for col, stage in zip(cols2, post_stages):
        status = tracker.stage_status(stage["id"])
        badge = _status_badge(status)
        label_color = "#ff5a1f" if status == "active" else "#94a3b8" if status == "pending" else "#16a34a"
        col.markdown(
            f"""
            <div style="text-align:center; padding:0.5rem 0;">
                {badge}<br>
                <span style="font-size:0.75rem; color:{label_color}; font-weight:600;">{stage['name']}</span>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.markdown("---")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("LLM 调用", tracker.llm_calls)
    c2.metric("工具调用", tracker.tool_calls)
    c3.metric("输入 Tokens", f"{tracker.tokens_in:,}")
    c4.metric("输出 Tokens", f"{tracker.tokens_out:,}")

    # 🔬 7 大 Agent 独立监控面板
    debug_mode = st.session_state.get("debug_mode", False)
    render_debug_monitor(tracker, default_expanded=debug_mode)

    if tracker.error:
        st.error(f"错误: {tracker.error}")

    completed_reports = [
        (stage["name"], stage["icon"], tracker.stage_reports[stage["id"]])
        for stage in stages
        if stage["id"] in tracker.stage_reports
    ]

    if completed_reports:
        st.markdown(
            '<div style="margin:0.5rem 0 0.3rem; font-size:0.85rem; color:#888;">'
            f"REPORTS ({len(completed_reports)})</div>",
            unsafe_allow_html=True,
        )
        for name, icon, report in reversed(completed_reports):
            is_latest = (name == completed_reports[-1][0])
            with st.expander(f"{icon} {name}", expanded=is_latest):
                st.markdown(report[:3000])
