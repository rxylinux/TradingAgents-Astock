"""Sidebar: stock input, LLM config, and history list."""

from __future__ import annotations

import os
from datetime import date

import streamlit as st

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.checkpointer import clear_checkpoint
from tradingagents.llm_clients.model_catalog import MODEL_OPTIONS
from web.history import (
    clear_incomplete_task,
    get_history,
    get_incomplete_history,
    record_incomplete_task,
)

# Provider display names in recommended order
_PROVIDERS: list[tuple[str, str]] = [
    ("智谱 GLM（推荐·Coding 套餐）", "glm"),
    ("MiniMax（国内直连）", "minimax"),
    ("DeepSeek", "deepseek"),
    ("通义千问 Qwen", "qwen"),
    ("OpenAI", "openai"),
    ("Anthropic", "anthropic"),
    ("Google Gemini", "google"),
    ("xAI Grok", "xai"),
    ("OpenRouter（聚合·填 vendor/model 形式 ID）", "openrouter"),
    ("OpenAI 兼容（自定义 base_url·9Router/AI Router/自建代理）", "openai_compatible"),
    ("Ollama（本地）", "ollama"),
]

_PROVIDER_DISPLAY = [name for name, _ in _PROVIDERS]
_PROVIDER_KEYS = [key for _, key in _PROVIDERS]


def _resolve_user_input(raw: str, analysis_type: str = "个股") -> tuple[str, str | None]:
    """Resolve raw user input to (ticker_code, error_msg).

    个股: 6-digit codes or Chinese stock names（原行为不变）
    指数: 中文名 / 带交易所后缀代码（000001.SH）/ 指数号段裸码（实测探测）
    资金流向: 同指数，留空默认上证指数
    """
    if analysis_type in ("指数", "资金流向"):
        from tradingagents.dataflows.index_data import resolve_index_input

        if not raw.strip() and analysis_type == "资金流向":
            return "000001.SH", None  # 资金流报告默认看上证指数
        try:
            return resolve_index_input(raw).ticker, None
        except ValueError as e:
            return "", str(e)

    from tradingagents.dataflows.a_stock import resolve_ticker

    try:
        code = resolve_ticker(raw)
        return code, None
    except ValueError as e:
        return "", str(e)


def _clear_analysis_artifacts(ticker: str, trade_date: str) -> None:
    clear_incomplete_task(ticker, trade_date)
    clear_checkpoint(DEFAULT_CONFIG["data_cache_dir"], ticker, trade_date)


def _render_analysis_controls(raw_ticker: str, trade_date_value: date) -> None:
    tracker = st.session_state.get("tracker")
    is_running = tracker is not None and tracker.is_running
    trade_date = trade_date_value.strftime("%Y-%m-%d")

    pause_col, resume_col, stop_col = st.columns(3)

    pause_disabled = not is_running or tracker.is_paused or tracker.stop_requested
    if pause_col.button(
        "暂停",
        key="sidebar_pause_analysis",
        use_container_width=True,
        disabled=pause_disabled,
    ):
        if tracker.pause():
            record_incomplete_task(
                tracker.ticker,
                tracker.trade_date,
                status="paused",
                completed_stages=tracker.completed_stages,
            )
        st.rerun()

    resume_disabled = not is_running or not tracker.is_paused or tracker.stop_requested
    if resume_col.button(
        "恢复",
        key="sidebar_resume_analysis",
        use_container_width=True,
        disabled=resume_disabled,
    ):
        if tracker.resume():
            record_incomplete_task(
                tracker.ticker,
                tracker.trade_date,
                status="running",
                completed_stages=tracker.completed_stages,
            )
        st.rerun()

    can_stop = tracker is not None or bool(raw_ticker.strip())
    if stop_col.button(
        "停止",
        key="sidebar_stop_analysis",
        use_container_width=True,
        disabled=not can_stop,
    ):
        target_ticker = tracker.ticker if tracker is not None and tracker.ticker else ""
        target_date = (
            tracker.trade_date
            if tracker is not None and tracker.trade_date
            else trade_date
        )

        if not target_ticker:
            target_ticker, err = _resolve_user_input(raw_ticker)
            if err:
                st.error(f"❌ {err}")
                return

        if tracker is not None and tracker.is_running:
            tracker.request_stop()
            clear_incomplete_task(target_ticker, target_date)
        else:
            if tracker is not None:
                tracker.mark_stopped()
                st.session_state["tracker"] = None
            _clear_analysis_artifacts(target_ticker, target_date)

        st.session_state["viewing_history"] = None
        st.success("已清空当前进度；下一次开始分析会从头生成。")
        st.rerun()

    if tracker is not None and tracker.stop_requested:
        st.caption("正在停止并清空，收尾完成后可重新开始。")


def _render_llm_config() -> None:
    """Render LLM provider and model selection controls."""

    default_provider = DEFAULT_CONFIG.get("llm_provider", "glm")
    provider_default_idx = _PROVIDER_KEYS.index(default_provider) if default_provider in _PROVIDER_KEYS else 0

    provider_idx = st.selectbox(
        "LLM 供应商",
        range(len(_PROVIDERS)),
        format_func=lambda i: _PROVIDER_DISPLAY[i],
        key="llm_provider_idx",
        index=provider_default_idx,
        help="选择你配置了 API Key 的供应商",
    )
    provider_key = _PROVIDER_KEYS[provider_idx]
    st.session_state["llm_provider"] = provider_key

    if provider_key in MODEL_OPTIONS:
        quick_options = MODEL_OPTIONS[provider_key]["quick"]
        deep_options = MODEL_OPTIONS[provider_key]["deep"]

        quick_labels = [label for label, _ in quick_options]
        quick_values = [value for _, value in quick_options]
        deep_labels = [label for label, _ in deep_options]
        deep_values = [value for _, value in deep_options]

        default_quick = DEFAULT_CONFIG.get("quick_think_llm", "glm-5.2")
        quick_default_idx = quick_values.index(default_quick) if default_quick in quick_values else 0
        quick_idx = st.selectbox(
            "快速思考模型",
            range(len(quick_options)),
            format_func=lambda i: quick_labels[i],
            key="quick_model_idx",
            index=quick_default_idx,
            help="用于常规分析任务，速度优先",
        )
        st.session_state["quick_think_llm"] = quick_values[quick_idx]

        default_deep = DEFAULT_CONFIG.get("deep_think_llm", "glm-5.3")
        deep_default_idx = deep_values.index(default_deep) if default_deep in deep_values else 0
        deep_idx = st.selectbox(
            "深度思考模型",
            range(len(deep_options)),
            format_func=lambda i: deep_labels[i],
            key="deep_model_idx",
            index=deep_default_idx,
            help="用于辩论/决策等需要深度推理的任务",
        )
        st.session_state["deep_think_llm"] = deep_values[deep_idx]
    else:
        custom_quick = st.text_input("快速思考模型 ID", key="custom_quick_model")
        custom_deep = st.text_input("深度思考模型 ID", key="custom_deep_model")
        st.session_state["quick_think_llm"] = custom_quick
        st.session_state["deep_think_llm"] = custom_deep

    base_url_required = provider_key == "openai_compatible"
    st.text_input(
        "API Base URL（第三方/代理" + ("·必填" if base_url_required else "，可选") + "）",
        key="llm_base_url",
        placeholder="例: https://your-relay.example/v1",
        help=(
            "通过第三方中转/代理访问模型时填写网关地址；留空则用所选供应商的官方地址。"
            "API Key 仍从 .env 读取，每个供应商用各自的环境变量——"
            "OpenAI=OPENAI_API_KEY、DeepSeek=DEEPSEEK_API_KEY、"
            "通义=DASHSCOPE_API_KEY、智谱=ZHIPU_API_KEY、MiniMax=MINIMAX_API_KEY、"
            "Claude=ANTHROPIC_API_KEY、OpenRouter=OPENROUTER_API_KEY、xAI=XAI_API_KEY、"
            "OpenAI 兼容（自定义）=OPENAI_COMPATIBLE_API_KEY（也接受 OPENAI_API_KEY）。"
            "也可在 .env 里设 BACKEND_URL 代替此处。"
        ),
    )
    if base_url_required:
        st.caption(
            "已选「OpenAI 兼容（自定义）」：**Base URL 必填**（你的网关，走标准 Chat "
            "Completions），模型 ID 手动填写，Key 在 .env 设 `OPENAI_COMPATIBLE_API_KEY`。"
        )

    # ── 个人 Claude 订阅额度（可选，仅个人自用）────────────────────────
    _scope_labels = [
        "关闭（走上面选的供应商）",
        "仅深度节点（Research/Portfolio）",
        "所有节点（含 7 个工具分析师）",
    ]
    _scope_values = ["off", "deep", "all"]
    scope_idx = st.selectbox(
        "个人 Claude 订阅覆盖 (Agent SDK)",
        range(len(_scope_labels)),
        format_func=lambda i: _scope_labels[i],
        key="subscription_scope_idx",
        help=(
            "让部分/全部节点经 Claude Agent SDK 走你个人 Pro/Max 订阅额度，"
            "而非按 token 计费。「所有节点」含 7 个工具分析师（其工具调用已桥接到订阅）。"
            "需装 [agentsdk] 依赖，且本机 claude 已登录（或设 CLAUDE_CODE_OAUTH_TOKEN）。"
        ),
    )
    scope = _scope_values[scope_idx]
    st.session_state["subscription_scope"] = scope
    if scope != "off":
        # 用别名而非写死版本号：claude CLI 的 opus/sonnet 恒指向最新模型。
        st.session_state.setdefault("agent_sdk_model", "opus")
        st.text_input(
            "订阅使用的 Claude 模型",
            key="agent_sdk_model",
            help=(
                "填别名 opus / sonnet（恒指向最新模型，推荐）或完整模型 id。"
                "撞额度/失败时自动降级到上面选的供应商 + 对应模型。"
            ),
        )
        if scope == "all":
            st.caption(
                "⚠️ 「所有节点」会把 7 个分析师 + 多空/交易员/风险辩手全部压到订阅上，"
                "订阅是按额度限流的，跑几轮就可能撞上限。可在 config 里把 "
                "`agent_sdk_quick_model` 设为 `sonnet` 降低消耗（默认已是）。"
            )
        if os.getenv("ANTHROPIC_API_KEY"):
            st.info(
                "检测到 ANTHROPIC_API_KEY。它**不会**泄进 Agent SDK 子进程"
                "（已在子进程环境显式置空），所以订阅额度照常生效；"
                "父进程保留它，是为了让 `anthropic` 仍能作为撞额度后的降级 provider。"
                "如果你并不打算保留付费降级，可在 .env 里清掉它。"
            )


def render_sidebar() -> None:
    """Render the sidebar with input controls and history."""

    st.markdown(
        """
        <div style="text-align:center; margin-bottom:1.5rem;">
            <span style="font-size:2rem; font-weight:800; color:#ff5a1f;">Trading</span><span style="font-size:2rem; font-weight:800; color:#0f172a;">Agents</span><span style="font-size:2rem; font-weight:800; color:#0f172a;">-</span><span style="font-size:2rem; font-weight:800; color:#ff5a1f;">Astock</span>
            <div style="font-size:0.85rem; color:#475569; margin-top:0.2rem; font-weight:500;">
                A股多Agent投研系统
            </div>
            <div style="font-size:0.75rem; color:#94a3b8; margin-top:0.3rem;">
                by <a href="https://github.com/simonlin1212" style="color:#ff5a1f; text-decoration:none; font-weight:600;">simonlin1212</a>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown("---")
    st.markdown("#### 新建分析")

    analysis_type = st.selectbox(
        "分析类型",
        ["个股", "指数", "资金流向"],
        key="analysis_type",
        help=(
            "个股=6位代码完整多Agent分析；指数=大盘指数（上证指数/沪深300/创业板指"
            "等）完整分析；资金流向=轻量快速的大盘资金流报告（1次模型调用）"
        ),
    )

    if analysis_type == "指数":
        ticker_help = (
            "支持：指数中文名（上证指数/沪深300/创业板指/深证成指/科创50/中证500/"
            "中证1000）、带交易所后缀代码（000001.SH / 399006.SZ）、或指数号段 6 位"
            "代码（000/880/999/399 开头，自动探测校验）"
        )
        ticker_placeholder = "例: 000001.SH 或 上证指数"
    elif analysis_type == "资金流向":
        ticker_help = "可选：默认上证指数。支持中文名或 000001.SH 形式"
        ticker_placeholder = "默认 上证指数，可填其他指数"
    else:
        ticker_help = "输入6位A股代码或中文股票全称"
        ticker_placeholder = "例: 300750 或 宁德时代"

    ticker = st.text_input(
        "股票代码",
        placeholder=ticker_placeholder,
        key="input_ticker",
        help=ticker_help,
    )

    trade_date = st.date_input(
        "分析日期",
        value=date.today(),
        key="input_date",
    )

    start_date = st.date_input(
        "数据起始日期",
        value=trade_date.replace(day=1),   # 默认本月第一天
        key="input_start_date",
        help="技术分析回溯到该日期（默认本月第一天）。分析区间 = 起始日期 → 分析日期，"
             "用于「按月」或自定义时段分析；留默认即分析当月至今。",
    )
    # 分析窗口天数 → market_lookback_days（下限 5 天，保证指标有意义）
    st.session_state["market_lookback_days"] = max((trade_date - start_date).days, 5)
    _ALL_ANALYSTS = [
        ("market", "📊 技术分析师", "量价异动、均线趋势、MACD/K线形态"),
        ("social", "💬 情绪分析师", "股吧/雪球讨论热度、散户情绪"),
        ("news", "📰 新闻分析师", "个股公告、行业动态、财经快讯"),
        ("fundamentals", "📋 基本面分析师", "财报三表、PE/PB估值、盈利预测"),
        ("policy", "🏛️ 政策分析师", "宏观政策、产业扶持、监管新规"),
        ("hot_money", "🔥 游资追踪师", "主力资金流向、龙虎榜席位、北向资金"),
        ("lockup", "🔒 解禁监控师", "限售股解禁日历、大股东减持预警"),
    ]

    with st.expander("👥 分析师团队配置 (7位)", expanded=False):
        st.caption("勾选参与本轮分析的 AI 分析师角色：")
        selected_analysts = []
        for key, name, desc in _ALL_ANALYSTS:
            # 指数模式下基本面与解禁不适用
            default_val = not (analysis_type == "指数" and key in ("fundamentals", "lockup"))
            if st.checkbox(
                f"{name}",
                value=default_val,
                key=f"sidebar_analyst_{key}",
                help=desc,
            ):
                selected_analysts.append(key)
        if not selected_analysts:
            st.warning("⚠️ 请至少选择 1 位分析师，已自动勾选技术分析师。")
            selected_analysts = ["market"]
        st.session_state["selected_analysts"] = selected_analysts

    with st.expander("⚙️ 模型配置", expanded=False):
        _render_llm_config()

    st.toggle(
        "🐞 调试模式 (Agent状态监控)",
        key="debug_mode",
        value=st.session_state.get("debug_mode", False),
        help="开启后在运行中及完成后实时展示 7 位 Agent 各自的执行状态、工具调用明细与中间数据",
    )

    tracker = st.session_state.get("tracker")
    is_busy = tracker is not None and tracker.is_running
    is_stopping = is_busy and tracker.stop_requested

    if analysis_type == "资金流向":
        button_label = "生成资金流报告"
    elif is_stopping:
        button_label = "停止中..."
    elif is_busy:
        button_label = "分析进行中..."
    else:
        button_label = "开始分析"

    if st.button(
        button_label,
        use_container_width=True,
        disabled=is_busy or (not ticker and analysis_type != "资金流向"),
        type="primary",
    ):
        resolved_code, err = _resolve_user_input(ticker, analysis_type)
        if err:
            st.error(f"❌ {err}")
        else:
            if resolved_code != ticker.strip():
                st.success(f"✅ {ticker.strip() or '(默认)'} → {resolved_code}")
            st.session_state["start_analysis"] = {
                "ticker": resolved_code,
                "trade_date": trade_date.strftime("%Y-%m-%d"),
                "fresh": True,
                "analysis_type": analysis_type,
            }
            st.session_state["viewing_history"] = None

    _render_analysis_controls(ticker, trade_date)

    st.markdown("---")
    st.markdown("#### 未完成任务")

    incomplete = get_incomplete_history()
    if not incomplete:
        st.caption("暂无未完成任务")
    else:
        for entry in incomplete[:10]:
            t, d = entry["ticker"], entry["trade_date"]
            status_label = {
                "error": "出错",
                "paused": "已暂停",
                "running": "进行中",
            }.get(entry.get("status"), "可继续")
            step = entry.get("checkpoint_step")
            step_label = f" · step {step}" if step is not None else ""
            label = f"{t}  ·  {d}  ·  {status_label}{step_label}"
            if st.button(
                label,
                key=f"resume_{t}_{d}",
                use_container_width=True,
                disabled=is_busy,
            ):
                # 从断点续跑：带 .SH/.SZ 后缀的是指数任务，恢复为指数模式
                from tradingagents.dataflows.index_registry import parse_index_ticker

                st.session_state["start_analysis"] = {
                    "ticker": t,
                    "trade_date": d,
                    "analysis_type": "指数" if parse_index_ticker(t) else "个股",
                }
                st.session_state["viewing_history"] = None

    st.markdown("---")
    st.markdown("#### 历史记录")

    history = get_history()
    if not history:
        st.caption("暂无历史记录")
        return

    for entry in history[:20]:
        t, d = entry["ticker"], entry["date"]
        label = f"{t}  ·  {d}"
        if st.button(label, key=f"hist_{t}_{d}", use_container_width=True):
            st.session_state["viewing_history"] = entry["path"]
            st.session_state["start_analysis"] = None

    st.markdown("---")
    st.caption("⚠️ 仅供学习研究，不构成投资建议")
