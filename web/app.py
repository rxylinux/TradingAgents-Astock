"""TradingAgents A股分析 — Streamlit Web UI."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# override=True：让 .env 的值优先于进程里可能残留的空/旧环境变量（#66）。
# 注意：load_dotenv 仅在进程启动时执行一次，启动后修改 .env 仍需重启 Web 服务才生效。
load_dotenv(_PROJECT_ROOT / ".env", override=True)

from tradingagents.default_config import DEFAULT_CONFIG  # noqa: E402

from web.components.progress_panel import render_progress  # noqa: E402
from web.components.report_viewer import render_report  # noqa: E402
from web.components.sidebar import render_sidebar  # noqa: E402
from web.history import clear_incomplete_task, extract_signal, load_analysis  # noqa: E402
from web.progress import ProgressTracker  # noqa: E402
from web.runner import run_analysis_in_thread  # noqa: E402

# ── Page config ──────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="TradingAgents-Astock A股分析",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Custom CSS ───────────────────────────────────────────────────────────────

st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;900&display=swap');

    /* Hide Streamlit chrome for clean video recording.
       IMPORTANT: do NOT `display:none` the whole header OR the whole toolbar.
       In Streamlit >= 1.36 the "expand sidebar" button lives *inside* the
       toolbar (header > stToolbar > stExpandSidebarButton), so hiding either
       one makes a collapsed sidebar impossible to reopen (issue #36). Instead
       keep the header/toolbar in the DOM, make the header transparent, and
       hide only the individual chrome widgets we don't want on camera. */
    #MainMenu,
    footer,
    div[data-testid="stDecoration"],
    div[data-testid="stStatusWidget"],
    div[data-testid="stToolbarActions"],
    div[data-testid="stAppDeployButton"],
    span[data-testid="stMainMenu"] { display: none !important; }
    header[data-testid="stHeader"] {
        background: transparent !important;
        box-shadow: none !important;
    }
    /* Keep the sidebar collapse / expand controls always visible & clickable.
       Selector list spans multiple Streamlit versions. */
    button[data-testid="stExpandSidebarButton"],
    button[data-testid="stSidebarCollapseButton"],
    button[data-testid="collapsedControl"],
    [data-testid="stSidebarCollapsedControl"] {
        display: flex !important;
        visibility: visible !important;
        opacity: 1 !important;
    }

    html, body, [class*="css"] {
        font-family: 'Inter', -apple-system, sans-serif;
    }
    .stApp {
        background: #ffffff;
        color: #0f172a;
    }
    section[data-testid="stSidebar"] {
        background: #f8fafc;
        border-right: 1px solid #e2e8f0;
    }
    .stMetric label { color: #64748b !important; font-size: 0.8rem !important; }
    .stMetric [data-testid="stMetricValue"] {
        color: #ff5a1f !important;
        font-weight: 700 !important;
    }
    .stProgress > div > div > div {
        background: linear-gradient(90deg, #ff5a1f, #ff8c42) !important;
    }
    button[kind="primary"] {
        background: linear-gradient(135deg, #ff5a1f, #ff8c42) !important;
        border: none !important;
        color: #ffffff !important;
        font-weight: 700 !important;
        letter-spacing: 0.05em !important;
        box-shadow: 0 4px 12px rgba(255,90,31,0.25) !important;
        transition: all 0.2s ease !important;
    }
    button[kind="primary"]:hover {
        background: linear-gradient(135deg, #e04d15, #ff5a1f) !important;
        box-shadow: 0 6px 18px rgba(255,90,31,0.35) !important;
        transform: translateY(-1px) !important;
    }
    /* Secondary buttons (history items) */
    button[kind="secondary"] {
        background: #ffffff !important;
        border: 1px solid #cbd5e1 !important;
        color: #334155 !important;
        transition: all 0.2s ease !important;
    }
    button[kind="secondary"]:hover {
        background: #f8fafc !important;
        border-color: #ff5a1f !important;
        color: #ff5a1f !important;
    }
    .stExpander {
        border: 1px solid #e2e8f0 !important;
        border-radius: 8px !important;
        background: #ffffff !important;
    }
    .stTabs [data-baseweb="tab"] {
        color: #64748b !important;
    }
    .stTabs [aria-selected="true"] {
        color: #ff5a1f !important;
        border-bottom-color: #ff5a1f !important;
    }
    div[data-testid="stDownloadButton"] button {
        background: #f1f5f9 !important;
        border: 1px solid #ff5a1f !important;
        color: #ff5a1f !important;
    }
    /* Text input styling */
    input[data-testid="stTextInputRootElement"] input,
    .stTextInput input {
        background: #ffffff !important;
        border-color: #cbd5e1 !important;
        color: #0f172a !important;
    }
    .stTextInput input:focus {
        border-color: #ff5a1f !important;
        box-shadow: 0 0 0 1px #ff5a1f !important;
    }
    /* Date input styling */
    .stDateInput input {
        background: #ffffff !important;
        border-color: #cbd5e1 !important;
        color: #0f172a !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ── Build config ─────────────────────────────────────────────────────────────

def _build_config() -> dict:
    config = DEFAULT_CONFIG.copy()
    config["llm_provider"] = st.session_state.get("llm_provider", "deepseek")
    config["deep_think_llm"] = st.session_state.get("deep_think_llm", "deepseek-v4-pro")
    config["quick_think_llm"] = st.session_state.get("quick_think_llm", "deepseek-v4-flash")
    # Optional third-party / proxy endpoint. Sidebar input wins, else .env BACKEND_URL.
    backend_url = (st.session_state.get("llm_base_url") or os.getenv("BACKEND_URL") or "").strip()
    config["backend_url"] = backend_url or None
    config["data_vendors"] = {
        "core_stock_apis": "a_stock",
        "technical_indicators": "a_stock",
        "fundamental_data": "a_stock",
        "news_data": "a_stock",
        "signal_data": "a_stock",
    }
    # Analysis window (#16): start-date input in the sidebar → look-back days.
    config["market_lookback_days"] = st.session_state.get("market_lookback_days")
    config["max_debate_rounds"] = 1
    config["max_risk_discuss_rounds"] = 1
    config["checkpoint_enabled"] = True
    config["output_language"] = "Chinese"
    # 分析类型：指数走指数图（指数版分析师/辩论/决策 prompt），个股=原行为
    config["instrument_type"] = (
        "index" if st.session_state.get("analysis_type") == "指数" else "stock"
    )
    config["selected_analysts"] = st.session_state.get(
        "selected_analysts",
        ["market", "social", "news", "fundamentals", "policy", "hot_money", "lockup"],
    )
    # Optional: route nodes through a personal Claude Pro/Max subscription (Agent
    # SDK). Scope: "deep" = Research/Portfolio only; "all" = + the 7 analysts.
    # Leaving the fallback keys None makes the graph fall back to the
    # sidebar-selected llm_provider + models on quota/failure.
    scope = st.session_state.get("subscription_scope", "off")
    # 侧栏那个输入框只配**深度节点**的模型。不要把它同时赋给 quick——
    # quick 节点有 7 个分析师 + 多空/交易员/风险辩手，把深度节点的 opus 复制过去
    # 会让订阅额度烧得极快，也与 README / 侧栏提示所说的「quick 默认 sonnet」矛盾。
    # quick 的模型交给 DEFAULT_CONFIG（默认 sonnet），需要时在 config 层单独覆盖。
    sub_model = st.session_state.get("agent_sdk_model")
    if scope in ("deep", "all"):
        config["deep_think_provider_override"] = "claude_agent_sdk"
        if sub_model:
            config["agent_sdk_model"] = sub_model
    if scope == "all":
        config["quick_think_provider_override"] = "claude_agent_sdk"
    return config


# ── Sidebar ──────────────────────────────────────────────────────────────────

with st.sidebar:
    render_sidebar()


# ── Handle "Start Analysis" trigger ──────────────────────────────────────────

start_req = st.session_state.pop("start_analysis", None)
if start_req:
    # 新任务开始时清掉上一份资金流报告的展示状态
    st.session_state.pop("fundflow_report", None)

    a_type = start_req.get("analysis_type", "个股")

    if a_type == "资金流向":
        # 独立资金流报告：拉数据 + 1 次模型调用（10-30 秒），同步生成即可，
        # 不走 12 阶段后台流水线。
        from tradingagents.market_flow import generate_market_flow_report

        try:
            with st.spinner("正在拉取市场资金数据并生成报告（约 10-30 秒）…"):
                report_md = generate_market_flow_report(
                    config=_build_config(),
                    index_id=start_req["ticker"],
                    curr_date=start_req["trade_date"],
                )
            st.session_state["fundflow_report"] = {
                "ticker": start_req["ticker"],
                "trade_date": start_req["trade_date"],
                "report": report_md,
            }
        except Exception as exc:
            st.error(f"资金流报告生成失败: {exc}")
        st.rerun()

    if start_req.get("fresh"):
        from tradingagents.graph.checkpointer import clear_checkpoint

        clear_incomplete_task(start_req["ticker"], start_req["trade_date"])
        clear_checkpoint(
            DEFAULT_CONFIG["data_cache_dir"],
            start_req["ticker"],
            start_req["trade_date"],
        )

    # 动态构建本次运行的 stage_ids
    from web.progress import PIPELINE_STAGES

    if a_type == "指数":
        stage_ids = [
            s["id"] for s in PIPELINE_STAGES
            if s["id"] not in ("fundamentals", "lockup")
        ]
    else:
        selected = _build_config().get("selected_analysts") or [
            "market", "social", "news", "fundamentals", "policy", "hot_money", "lockup"
        ]
        downstream = {"quality_gate", "debate", "trader", "risk", "pm"}
        stage_ids = [
            s["id"] for s in PIPELINE_STAGES
            if s["id"] in selected or s["id"] in downstream
        ]

    tracker = ProgressTracker(
        ticker=start_req["ticker"],
        trade_date=start_req["trade_date"],
        stage_ids=stage_ids,
    )
    st.session_state["tracker"] = tracker
    st.session_state["viewing_history"] = None
    run_analysis_in_thread(
        ticker=start_req["ticker"],
        trade_date=start_req["trade_date"],
        config=_build_config(),
        tracker=tracker,
    )


# ── Main area state machine ─────────────────────────────────────────────────

tracker: ProgressTracker | None = st.session_state.get("tracker")
viewing_history: str | None = st.session_state.get("viewing_history")

# State 1: Viewing a historical analysis
if viewing_history:
    try:
        state = load_analysis(viewing_history)
        signal = extract_signal(state)
        ticker = Path(viewing_history).parent.parent.name
        trade_date = Path(viewing_history).stem.replace("full_states_log_", "")
        render_report(state, ticker, trade_date, signal)
    except Exception as exc:
        st.error(f"加载失败: {exc}")

# State 2: Analysis running
elif tracker and tracker.is_running:
    render_progress(tracker)
    time.sleep(2)
    st.rerun()

# State 3: Analysis complete
elif tracker and tracker.is_complete:
    render_report(
        tracker.final_state,
        tracker.ticker,
        tracker.trade_date,
        tracker.signal,
        elapsed=tracker.elapsed,
    )

# State 4: Analysis errored
elif tracker and tracker.error:
    st.error(f"分析失败: {tracker.error}")
    st.caption("已完成阶段会保存在本地断点中；修复模型额度或配置后，可以继续未完成的部分。")
    if st.button("继续未完成任务", type="primary"):
        st.session_state["start_analysis"] = {
            "ticker": tracker.ticker,
            "trade_date": tracker.trade_date,
        }
        st.session_state["viewing_history"] = None
        st.rerun()

# State 5: Standalone fund-flow report（独立「资金流向」模式的结果展示）
elif st.session_state.get("fundflow_report"):
    ff = st.session_state["fundflow_report"]
    st.markdown(ff["report"])
    st.download_button(
        "⬇️ 下载 Markdown",
        data=ff["report"],
        file_name=f"fundflow_{ff['ticker'].replace('.', '_')}_{ff['trade_date']}.md",
        mime="text/markdown",
    )

# State 0: Idle — welcome screen
else:
    st.markdown(
        """<div style="text-align: center; margin-top: 1.5rem; margin-bottom: 2rem;">
<div style="font-size: 3rem; margin-bottom: 0.5rem;">📈</div>
<div style="font-size: 2.3rem; font-weight: 900; margin-bottom: 0.4rem;">
<span style="color: #ff5a1f;">Trading</span><span style="color: #0f172a;">Agents</span><span style="color: #0f172a;">-</span><span style="color: #ff5a1f;">Astock</span>
</div>
<div style="color: #475569; font-size: 1.05rem;">
A股深度特化多 Agent 投研决策体系
</div>
<div style="color: #64748b; font-size: 0.85rem; margin-top: 0.3rem;">
7位垂直领域分析师 · 数据质量门控 · Bull/Bear 多空博弈 · 三方风险辩论 · 资产经理最终决策
</div>
</div>

<div style="max-width: 1080px; margin: 0 auto;">
<div style="font-size: 1.05rem; font-weight: 700; color: #0f172a; margin-bottom: 0.8rem; border-left: 3px solid #ff5a1f; padding-left: 8px;">
👥 7 位专职 AI 投研分析师团队
</div>
<div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; margin-bottom: 1.5rem;">
<div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 1.1rem; box-shadow: 0 1px 3px rgba(0,0,0,0.04);">
<div style="font-size: 1.05rem; font-weight: 700; color: #ea580c;">📊 技术分析师</div>
<div style="font-size: 0.82rem; color: #64748b; margin-top: 0.4rem; line-height: 1.5;">量价异动、均线多空排列、MACD/布林带指标研判、支撑阻力位测算</div>
</div>
<div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 1.1rem; box-shadow: 0 1px 3px rgba(0,0,0,0.04);">
<div style="font-size: 1.05rem; font-weight: 700; color: #ea580c;">💬 情绪分析师</div>
<div style="font-size: 0.82rem; color: #64748b; margin-top: 0.4rem; line-height: 1.5;">股吧/雪球讨论热度、散户多空情绪倾向、市场恐慌与狂热周期捕捉</div>
</div>
<div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 1.1rem; box-shadow: 0 1px 3px rgba(0,0,0,0.04);">
<div style="font-size: 1.05rem; font-weight: 700; color: #ea580c;">📰 新闻分析师</div>
<div style="font-size: 0.82rem; color: #64748b; margin-top: 0.4rem; line-height: 1.5;">上市公司重大公告、行业突发新闻、财联社全球快讯实时过滤与归因</div>
</div>
<div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 1.1rem; box-shadow: 0 1px 3px rgba(0,0,0,0.04);">
<div style="font-size: 1.05rem; font-weight: 700; color: #ea580c;">📋 基本面分析师</div>
<div style="font-size: 0.82rem; color: #64748b; margin-top: 0.4rem; line-height: 1.5;">财报三表快照、PE/PB 估值分位、同花顺卖方 EPS 一致预期与行业对比</div>
</div>
<div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 1.1rem; box-shadow: 0 1px 3px rgba(0,0,0,0.04);">
<div style="font-size: 1.05rem; font-weight: 700; color: #ea580c;">🏛️ 政策分析师 <span style="font-size:0.7rem; background:#fff7ed; color:#ea580c; border:1px solid #ffedd5; padding:1px 5px; border-radius:4px;">A股特化</span></div>
<div style="font-size: 0.82rem; color: #64748b; margin-top: 0.4rem; line-height: 1.5;">A股政策市顶层研判：国家战略、产业扶持、货币/财政/监管新规影响</div>
</div>
<div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 1.1rem; box-shadow: 0 1px 3px rgba(0,0,0,0.04);">
<div style="font-size: 1.05rem; font-weight: 700; color: #ea580c;">🔥 游资追踪师 <span style="font-size:0.7rem; background:#fff7ed; color:#ea580c; border:1px solid #ffedd5; padding:1px 5px; border-radius:4px;">A股特化</span></div>
<div style="font-size: 0.82rem; color: #64748b; margin-top: 0.4rem; line-height: 1.5;">主力/超大单资金流向、龙虎榜知名游资席位动向、同花顺涨停题材归因与北向资金</div>
</div>
<div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 1.1rem; box-shadow: 0 1px 3px rgba(0,0,0,0.04);">
<div style="font-size: 1.05rem; font-weight: 700; color: #ea580c;">🔒 解禁监控师 <span style="font-size:0.7rem; background:#fff7ed; color:#ea580c; border:1px solid #ffedd5; padding:1px 5px; border-radius:4px;">A股特化</span></div>
<div style="font-size: 0.82rem; color: #64748b; margin-top: 0.4rem; line-height: 1.5;">限售解禁日历、减持新规破发/破净/分红红线核查、大股东与高管减持抛压预警</div>
</div>
<div style="background: #f0f9ff; border: 1px solid #bae6fd; border-radius: 10px; padding: 1.1rem; box-shadow: 0 1px 3px rgba(0,0,0,0.04);">
<div style="font-size: 1.05rem; font-weight: 700; color: #0284c7;">⚖️ 决策与风控中枢</div>
<div style="font-size: 0.82rem; color: #475569; margin-top: 0.4rem; line-height: 1.5;">两道质量门控 → Bull/Bear 辩论 → 激进/保守/中立风控评估 → 投资经理定级</div>
</div>
</div>

<div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 12px; padding: 1.2rem 1.8rem; text-align: center; margin-bottom: 1.5rem; box-shadow: 0 1px 2px rgba(0,0,0,0.03);">
<div style="font-size: 1.05rem; color: #0f172a; font-weight: 600;">👈 快速开始：在左侧侧边栏输入股票代码（如 600519 或 贵州茅台）并点击「开始分析」</div>
<div style="font-size: 0.8rem; color: #64748b; margin-top: 0.3rem;">支持自定义分析师勾选、起始日期（按月回溯）、模型参数与个人 Claude 订阅配置</div>
</div>

<div style="text-align: center; color: #94a3b8; font-size: 0.75rem; line-height: 1.6; border-top: 1px solid #f1f5f9; padding-top: 1.2rem;">
⚠️ 本项目仅供学习研究与技术演示，不构成任何投资建议。<br>
投资决策请咨询持牌专业机构。作者不对使用本工具产生的任何损失承担责任。
</div>
</div>""",
        unsafe_allow_html=True,
    )
