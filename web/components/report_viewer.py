"""Render the completed analysis report with expandable sections and PDF download."""

from __future__ import annotations

import re
from typing import Any

import streamlit as st

from web.pdf_export import generate_markdown, generate_pdf
from web.stock_display import normalize_stock_mentions, stock_display_label


def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()


def _signal_style(signal: str) -> tuple[str, str]:
    s = signal.upper()
    if "BUY" in s:
        return "#22c55e", "买入"
    if "SELL" in s:
        return "#ef4444", "卖出"
    return "#fbbf24", "持有"


_ANALYST_SECTIONS = [
    ("market_report", "📊 技术分析"),
    ("sentiment_report", "💬 市场情绪"),
    ("news_report", "📰 新闻舆情"),
    ("fundamentals_report", "📋 基本面"),
    ("policy_report", "🏛️ 政策分析"),
    ("hot_money_report", "🔥 游资追踪"),
    ("lockup_report", "🔒 解禁/减持"),
]


def _safe_filename_label(label: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", label).strip("_")
    return cleaned or "report"


def _display_report_text(text: Any, ticker: str, final_state: dict[str, Any]) -> str:
    cleaned = _strip_think(str(text))
    return normalize_stock_mentions(cleaned, ticker, final_state)


@st.cache_data(show_spinner=False)
def _cached_generate_pdf(
    final_state: dict[str, Any],
    ticker: str,
    trade_date: str,
    signal: str,
) -> bytes:
    return generate_pdf(final_state, ticker, trade_date, signal)


@st.cache_data(show_spinner=False)
def _cached_generate_markdown(
    final_state: dict[str, Any],
    ticker: str,
    trade_date: str,
    signal: str,
) -> str:
    return generate_markdown(final_state, ticker, trade_date, signal)


def render_report(
    final_state: dict[str, Any],
    ticker: str,
    trade_date: str,
    signal: str,
    elapsed: float | None = None,
) -> None:
    """Render the full analysis report."""

    color, cn_signal = _signal_style(signal)
    ticker_label = stock_display_label(ticker, final_state)

    stats_html = ""
    if elapsed is not None:
        m, s = divmod(int(elapsed), 60)
        stats_html = f'<div style="font-size:0.9rem; color:#888; margin-top:0.3rem;">耗时 {m}:{s:02d}</div>'

    st.markdown(
        f"""
        <div style="
            background: linear-gradient(135deg, #ffffff 0%, #f8fafc 100%);
            border: 1px solid #e2e8f0;
            border-radius: 16px;
            padding: 2rem;
            text-align: center;
            margin: 1rem 0 2rem;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.05);
        ">
            <div style="font-size:0.85rem; color:#64748b; font-weight:700; letter-spacing:2px;">TRADING SIGNAL</div>
            <div style="font-size:3.5rem; font-weight:900; color:{color}; margin:0.3rem 0;">
                {signal.upper()}
            </div>
            <div style="font-size:1.2rem; color:#0f172a; font-weight:600;">
                {ticker_label} · {trade_date}
            </div>
            {stats_html}
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.caption("⚠️ 本报告由 AI 自动生成，仅供学习研究，不构成投资建议。")

    # Markdown export always works (no font dependency); PDF generation is
    # cached with @st.cache_data so page re-renders don't recompute PDF bytes.
    col_md, col_pdf, col_spacer = st.columns([1, 1, 2])
    with col_md:
        md_text = _cached_generate_markdown(final_state, ticker, trade_date, signal)
        st.download_button(
            "📥 下载 Markdown",
            data=md_text.encode("utf-8"),
            file_name=f"TradingAgents-Astock_{_safe_filename_label(ticker_label)}_{trade_date}.md",
            mime="text/markdown",
            use_container_width=True,
        )
    with col_pdf:
        try:
            pdf_bytes = _cached_generate_pdf(final_state, ticker, trade_date, signal)
            st.download_button(
                "📄 下载 PDF",
                data=pdf_bytes,
                file_name=f"TradingAgents-Astock_{_safe_filename_label(ticker_label)}_{trade_date}.pdf",
                mime="application/pdf",
                use_container_width=True,
            )
        except Exception as exc:  # noqa: BLE001 — never let PDF crash the page
            st.button(
                "📄 PDF 不可用",
                disabled=True,
                use_container_width=True,
                help=f"PDF 生成失败，请改用 Markdown 导出。原因：{exc}",
            )

    st.markdown("---")

    # N01: 结构化质量卡（报告前部；与 MD/PDF 同一渲染；旧报告显示未记录，
    # 原 data_quality_summary 文字仍在下方数据质量 expander 可查看）
    from tradingagents.agents.report_quality import render_quality_card_md

    with st.expander("🧾 报告质量卡（完整性）", expanded=True):
        st.markdown(
            _display_report_text(
                render_quality_card_md(final_state.get("data_quality")),
                ticker,
                final_state,
            )
        )

    # C1: 数据来源与证据（与 MD/PDF 同一渲染；旧报告显示未记录）
    from tradingagents.evidence.display import render_evidence_md

    with st.expander("🗂️ 数据来源与证据（抓取/过滤事实）", expanded=False):
        st.markdown(
            _display_report_text(
                render_evidence_md(final_state.get("evidence_bundle")),
                ticker,
                final_state,
            )
        )

    # C2: 研究假设卡（与 MD/PDF 同一渲染；旧报告显示未记录）
    from tradingagents.agents.thesis import render_thesis_md

    with st.expander("🧪 研究假设卡（结论结构化记录）", expanded=False):
        st.markdown(
            _display_report_text(
                render_thesis_md(final_state.get("thesis_card")),
                ticker,
                final_state,
            )
        )

    # D1: 可复算财务面板（与 MD/PDF 同一渲染；旧报告显示未记录）
    from tradingagents.dataflows.financial_panel import render_financial_panel_md

    with st.expander("🧮 财务面板（可复算）", expanded=False):
        st.markdown(
            _display_report_text(
                render_financial_panel_md(final_state.get("financial_panel")),
                ticker,
                final_state,
            )
        )

    # E: 独立初判与分歧核查（与 MD/PDF 同一渲染；旧报告显示未记录）
    from tradingagents.agents.debate_evidence import render_evidence_debate_md

    with st.expander("⚖️ 独立初判与分歧核查", expanded=False):
        st.markdown(
            _display_report_text(
                render_evidence_debate_md(final_state),
                ticker,
                final_state,
            )
        )

    # F2: 历史经验投影（与 MD/PDF 同一渲染；三态显示）
    from tradingagents.evaluation.review_projection import render_projection_md

    with st.expander("📚 历史经验投影（只读）", expanded=False):
        st.markdown(
            _display_report_text(
                render_projection_md(final_state.get("review_projection")),
                ticker,
                final_state,
            )
        )

    inv_plan = final_state.get("investment_plan", "")
    if inv_plan:
        st.markdown("### 👔 最终投资建议（研究经理）")
        st.markdown(_display_report_text(inv_plan, ticker, final_state))
        st.markdown("---")

    # A14: 组合经理的最终交易决策正文必须展示——investment_plan 只是研究
    # 经理的意见，不能替代 final_trade_decision（组合层面的最终结论）。
    final_decision = final_state.get("final_trade_decision", "")
    if final_decision:
        st.markdown("### 🎯 最终交易决策（组合经理）")
        st.markdown(_display_report_text(final_decision, ticker, final_state))
        st.markdown("---")

    st.markdown("### 📊 分析师报告")

    for key, title in _ANALYST_SECTIONS:
        content = final_state.get(key, "")
        if not content:
            continue
        with st.expander(title, expanded=False):
            st.markdown(_display_report_text(content, ticker, final_state))

    debate = final_state.get("investment_debate_state")
    if debate and isinstance(debate, dict):
        st.markdown("### ⚔️ 多空辩论")
        tab_bull, tab_bear, tab_judge = st.tabs(["多方", "空方", "研究经理"])
        with tab_bull:
            st.markdown(_display_report_text(debate.get("bull_history", "") or "无数据", ticker, final_state))
        with tab_bear:
            st.markdown(_display_report_text(debate.get("bear_history", "") or "无数据", ticker, final_state))
        with tab_judge:
            st.markdown(_display_report_text(debate.get("judge_decision", "") or "无数据", ticker, final_state))

    # A14: 共享字段访问器——canonical 优先、legacy 兼容、只展示一次
    from web.report_fields import trader_plan

    trader_decision = trader_plan(final_state) or ""
    if trader_decision:
        with st.expander("💹 交易员决策", expanded=False):
            st.markdown(_display_report_text(trader_decision, ticker, final_state))

    risk = final_state.get("risk_debate_state")
    if risk and isinstance(risk, dict):
        st.markdown("### 🛡️ 风控评估")
        tab_agg, tab_con, tab_neu, tab_rj = st.tabs(["激进", "保守", "中性", "风控决策"])
        with tab_agg:
            st.markdown(_display_report_text(risk.get("aggressive_history", "") or "无数据", ticker, final_state))
        with tab_con:
            st.markdown(_display_report_text(risk.get("conservative_history", "") or "无数据", ticker, final_state))
        with tab_neu:
            st.markdown(_display_report_text(risk.get("neutral_history", "") or "无数据", ticker, final_state))
        with tab_rj:
            st.markdown(_display_report_text(risk.get("judge_decision", "") or "无数据", ticker, final_state))

    dqs = final_state.get("data_quality_summary", "")
    if dqs:
        with st.expander("✅ 数据质量", expanded=False):
            st.markdown(_display_report_text(dqs, ticker, final_state))

    if st.session_state.get("debug_mode", False):
        render_agent_diagnostics(final_state, ticker)


def render_agent_diagnostics(final_state: dict[str, Any], ticker: str) -> None:
    """Render 7-Agent execution diagnostics overview at the bottom of the report."""
    from web.agent_debug import render_all_agent_diagnostics

    render_all_agent_diagnostics(final_state, ticker, trade_date=str(final_state.get("trade_date", "")))

