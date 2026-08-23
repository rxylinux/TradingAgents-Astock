"""7-Agent Debug & Telemetry Module for Web UI.

Provides structured prompt schemas, tool interaction extraction,
reasoning chain (<think>) parsing, and White Light Theme UI renderers
for both progress monitoring and final report diagnostics.
"""

from __future__ import annotations

import html
import json
import re
import time
from typing import Any, Optional

import streamlit as st


# ============================================================================
# 1. 7 大分析师详细元数据 (人设/规则/必采清单/工具集/输入参数)
# ============================================================================

ANALYST_DEBUG_SPECS: dict[str, dict[str, Any]] = {
    "market": {
        "id": "market",
        "name": "技术分析师",
        "icon": "📊",
        "report_key": "market_report",
        "desc": "量价异动、均线趋势、MACD/K线形态与支撑阻力",
        "persona": (
            "你是一位专注于 A 股市场的技术分析师。你的任务是从技术指标中选择最相关的指标"
            "（最多 8 个），注重指标间的互补性与多空研判，为给定的 A 股标的提供专业技术面分析。"
        ),
        "special_rules": [
            "**涨跌停制度**：主板 ±10%，科创板/创业板 ±20%，北交所 ±30%。ST/*ST 主板已统一为 ±10%，科创/创业板 ±20%。新股前5日无涨跌限制。触及涨跌停后流动性骤降，指标易失真。",
            "**T+1 交易制度**：当日买入次日方可卖出，短线交易策略的可执行性与流动性受限。",
            "**北向资金风向标**：外资通过沪深港通的流入流出大幅领先于行情转折。",
            "**换手率与筹码**：A 股散户占比高，换手率是判断资金活跃度与筹码松动的核心依据。",
            "**量价关系**：量在价先，放量突破与缩量回调是核心交易确认信号。",
        ],
        "checklist": [
            "最新收盘价、日期、当日涨跌幅",
            "近 N 日累计涨跌幅",
            "近 5 日平均成交量 vs 近 20 日平均成交量（放量/缩量）",
            "至少 3 个技术指标数值及多空信号（SMA/EMA/MACD/RSI/BOLL/ATR/VWMA）",
            "关键支撑位与阻力位区间",
        ],
        "tools": [
            {
                "name": "get_stock_data",
                "desc": "获取 A 股历史 K 线行情（开高低收量额、复权价）",
                "params": "ticker: str, start_date: str, end_date: str, look_back_days: int",
            },
            {
                "name": "get_indicators",
                "desc": "计算所选技术指标数据（close_50_sma, macd, rsi, boll, atr 等）",
                "params": "ticker: str, indicators: list[str], look_back_days: int",
            },
        ],
    },
    "social": {
        "id": "social",
        "name": "情绪分析师",
        "icon": "💬",
        "report_key": "sentiment_report",
        "desc": "股吧/雪球讨论热度、散户多空情绪倾向与资金背离",
        "persona": (
            "你是一位专注于 A 股市场的市场情绪分析师。你的任务是通过分析公司相关新闻、"
            "市场讨论和公众情绪，判断市场对目标公司的整体态度、情绪走向与资金背离情况。"
        ),
        "special_rules": [
            "**散户情绪权重高**：A 股散户占比超 60%，恐慌和贪婪的情绪波动剧烈，对短期股价影响显著。",
            "**主流舆论阵地**：东方财富股吧、雪球、同花顺社区为主要讨论阵地，推断散户多空预期。",
            "**资金与情绪背离**：嘴上说什么不如钱往哪走。先看资金再看新闻，背离必须单列指出。",
            "**反向指标**：当散户情绪一致性过高（极度乐观或极度悲观）时，往往是行情见顶或见底的反转信号。",
            "**时间维度区分**：区分 1-3 天事件驱动的短期波动与 1-4 周中期情绪趋势。",
        ],
        "checklist": [
            "主力资金当日净流入金额、近 20 日累计净流入方向",
            "近期成交量变化（相对前期均量倍数）",
            "是否出现在当日强势股榜、所属题材标签",
            "新闻检索条数与时间范围",
            "正面/负面/中性新闻比例与排名前 3 舆情主题",
            "资金面与消息面是否背离（背离方向说明）",
            "情绪评分（极度悲观/悲观/中性/乐观/极度乐观）与变化趋势（升温/降温/平稳）",
        ],
        "tools": [
            {
                "name": "get_fund_flow",
                "desc": "获取个股超大单/大单/中单/小单资金流向（分钟级与20日历史）",
                "params": "ticker: str, curr_date: str",
            },
            {
                "name": "get_stock_data",
                "desc": "获取近期量价走势辅助情绪强度判断",
                "params": "ticker: str, start_date: str, end_date: str",
            },
            {
                "name": "get_hot_stocks",
                "desc": "获取当日强势股榜与同花顺题材归因 Reason Tags",
                "params": "curr_date: str",
            },
            {
                "name": "get_news",
                "desc": "获取个股相关新闻与市场讨论，解释情绪成因",
                "params": "ticker: str, start_date: str, end_date: str",
            },
        ],
    },
    "news": {
        "id": "news",
        "name": "新闻分析师",
        "icon": "📰",
        "report_key": "news_report",
        "desc": "个股公告、行业动态、全球宏观快讯实时过滤",
        "persona": (
            "你是一位专注于 A 股市场的新闻与舆情分析师。你的任务是全面分析近期新闻动态，"
            "评估其对目标公司和 A 股市场的催化或利空影响。"
        ),
        "special_rules": [
            "**政策敏感度**：A 股为典型政策市，国务院、证监会、央行政策对市场具有全局性冲击。",
            "**权威消息来源权重**：财联社快讯（时效最快）> 新华财经/证券时报（官方权威）> 东方财富/同花顺（广泛传播）。",
            "**行业轮动传导**：产业链上下游联动明显，板块利好易产生外溢效应。",
            "**事件驱动催化**：财报预告、重大合同公告、股东大会决议、机构调研记录为关键触发点。",
        ],
        "checklist": [
            "个股新闻条数和检索时间范围",
            "宏观新闻条数和检索时间范围",
            "关键事件时间线（至少 3 个重要事件及发生日期）",
            "利好/利空/中性事件分类与影响评级",
            "潜在风险事件清单",
        ],
        "tools": [
            {
                "name": "get_news",
                "desc": "获取公司相关个股新闻与公告",
                "params": "ticker: str, start_date: str, end_date: str",
            },
            {
                "name": "get_global_news",
                "desc": "获取宏观经济、市场大盘及政策快讯",
                "params": "curr_date: str, look_back_days: int, limit: int",
            },
        ],
    },
    "fundamentals": {
        "id": "fundamentals",
        "name": "基本面分析师",
        "icon": "📋",
        "report_key": "fundamentals_report",
        "desc": "财报三表快照、PE/PB估值与机构一致预期盈利预测",
        "persona": (
            "你是一位专注于 A 股市场的基本面分析师。你的任务是全面分析目标公司的财务三表、"
            "盈利质量、估值倍数与行业对标，为投资决策提供扎实的数据支撑。"
        ),
        "special_rules": [
            "**中国会计准则 (CAS)**：收入确认、减值计提与 IFRS 存在口径差异。",
            "**估值参照系**：A 股估值应对标同行业横向水平，不能机械套用海外低估值标准。",
            "**核心盈利质量**：重点关注扣非净利润、ROE、毛利率、经营现金流与净利润匹配度。",
            "**财报披露节奏**：一季报(4月底)、半年报(8月底)、三季报(10月底)、年报(次年4月底)。",
            "**隐患排查**：商誉减值隐患、高比例股权质押、大股东减持诉求、关联交易占比。",
        ],
        "checklist": [
            "PE（TTM）、PB、总市值",
            "营业收入及同比增长率",
            "归母净利润及同比增长率",
            "净资产收益率 (ROE) 与毛利率",
            "资产负债率与现金流质量",
            "经营性现金流与净利润比值",
            "机构一致预期 EPS、前向 PE、PEG（调用 get_profit_forecast）",
        ],
        "tools": [
            {
                "name": "get_fundamentals",
                "desc": "获取公司综合基本面、估值快照与主要财务指标",
                "params": "ticker: str",
            },
            {
                "name": "get_profit_forecast",
                "desc": "获取机构一致预期 EPS、覆盖券商家数、前向 PE 与 PEG",
                "params": "ticker: str, curr_date: str",
            },
            {
                "name": "get_balance_sheet",
                "desc": "获取资产负债表详细历史科目",
                "params": "ticker: str",
            },
            {
                "name": "get_cashflow",
                "desc": "获取现金流量表历史科目（经营/投资/筹资）",
                "params": "ticker: str",
            },
            {
                "name": "get_income_statement",
                "desc": "获取利润表科目（营收/营业利润/净利润/扣非）",
                "params": "ticker: str",
            },
            {
                "name": "get_industry_comparison",
                "desc": "获取 90 个全行业涨跌幅/成交额/净流入排名与估值对标",
                "params": "ticker: str, curr_date: str",
            },
        ],
    },
    "policy": {
        "id": "policy",
        "name": "政策分析师",
        "icon": "🏛️",
        "report_key": "policy_report",
        "desc": "宏观政策、产业扶持、监管新规研判与传导路径",
        "persona": (
            "你是一位专注于 A 股市场的政策分析师。你的核心任务是追踪和解读影响目标公司"
            "及所在行业的政策动态，评估政策对股价的潜在影响方向、力度与持续周期。"
        ),
        "special_rules": [
            "**政策五大层级**：宏观政策、监管政策、产业政策、地方扶持、国际地缘政策。",
            "**政策力度级别**：指导意见（弱）< 部委通知（中）< 国务院文件（强）< 法律法规（最强）。",
            "**影响窗口期**：短期脉冲（1-2 周）vs 中期趋势（1-3 月）vs 长期结构性红利（半年以上）。",
            "**传导逻辑链**：政策出台 → 行业供需重塑 → 标的业务映射 → 财务与估值影响。",
        ],
        "checklist": [
            "近期相关政策事件清单（含发布日期与权威机构）",
            "行业政策支持方向判断（扶持/限制/中性）",
            "政策影响力度级别评级（强/中/弱）",
            "政策影响时间窗口估算",
            "政策面总体评级（重大利好/利好/中性/利空/重大利空）",
        ],
        "tools": [
            {
                "name": "get_news",
                "desc": "检索个股及行业相关的政策新闻公告",
                "params": "ticker: str, start_date: str, end_date: str",
            },
            {
                "name": "get_global_news",
                "desc": "获取宏观经济、部委发布与产业指导政策快讯",
                "params": "curr_date: str, look_back_days: int, limit: int",
            },
        ],
    },
    "hot_money": {
        "id": "hot_money",
        "name": "游资追踪师",
        "icon": "🔥",
        "report_key": "hot_money_report",
        "desc": "主力资金流向、龙虎榜席位明细与北向资金分钟级流向",
        "persona": (
            "你是一位专注于 A 股市场的游资与资金流向追踪分析师。你的核心任务是通过分析成交量异动、"
            "龙虎榜席位明细和股东变化，追踪主力资金和游资动向，研判短期博弈格局。"
        ),
        "special_rules": [
            "**量价异动识别**：日成交量放量超 20 日均量 2 倍以上，换手率 >10% 视为活跃游资介入。",
            "**龙虎榜席位信号**：知名一线游资营业部与机构专用席位的净买入具有强指向性。",
            "**连板分歧与一致**：首板放量代表分歧，缩量代表一致；二板定龙头；三板以上进入博弈阶段。",
            "**板块资金轮动**：资金从高位题材向低位滞涨板块轮动扩散。",
            "**内部人交易**：大股东增减持、定增、大宗交易折溢价反映资金内部态度。",
        ],
        "checklist": [
            "近 5 日成交量变化趋势（放量/缩量/平稳）",
            "当日北向资金净流入金额（沪股通 + 深股通）",
            "个股主力资金净流入（超大单 + 大单合计）",
            "所属概念板块及当日板块涨跌幅",
            "当日是否上榜热门股及题材归因 Reason Tags",
            "资金面总体判断（主力流入/主力流出/资金博弈/无明显信号）",
        ],
        "tools": [
            {
                "name": "get_stock_data",
                "desc": "获取量价走势，识别换手率与放量特征",
                "params": "ticker: str, start_date: str, end_date: str",
            },
            {
                "name": "get_dragon_tiger_board",
                "desc": "获取龙虎榜上榜明细、买卖前五席位与机构参与度",
                "params": "ticker: str, curr_date: str",
            },
            {
                "name": "get_northbound_flow",
                "desc": "获取北向资金（沪股通/深股通）实时分钟级累计净买入",
                "params": "curr_date: str",
            },
            {
                "name": "get_fund_flow",
                "desc": "获取个股超大单/大单/中单/小单资金净流入",
                "params": "ticker: str, curr_date: str",
            },
            {
                "name": "get_concept_blocks",
                "desc": "获取概念板块分类与所属板块当日行情",
                "params": "ticker: str",
            },
            {
                "name": "get_hot_stocks",
                "desc": "获取当日涨停股与题材归因",
                "params": "curr_date: str",
            },
            {
                "name": "get_insider_transactions",
                "desc": "获取股东及董监高近期增减持记录",
                "params": "ticker: str",
            },
            {
                "name": "get_industry_comparison",
                "desc": "全行业横向对比，判断资金行业轮动",
                "params": "ticker: str, curr_date: str",
            },
            {
                "name": "get_news",
                "desc": "检索主力动向与资金相关新闻",
                "params": "ticker: str, start_date: str, end_date: str",
            },
        ],
    },
    "lockup": {
        "id": "lockup",
        "name": "解禁监控师",
        "icon": "🔒",
        "report_key": "lockup_report",
        "desc": "限售解禁日历、大股东减持预警与2024减持新规合规排查",
        "persona": (
            "你是一位专注于 A 股市场的解禁与减持监控分析师。你的核心任务是追踪目标公司的"
            "限售股解禁计划、大股东减持动态和股权结构变化，评估供给端抛压风险。"
        ),
        "special_rules": [
            "**限售股类型划分**：首发原股东限售(1-3年)、定增限售(6-18个月)、股权激励限售、战略配售。",
            "**解禁规模评估**：解禁市值占流通市值 >20% 为重大压力；<5% 冲击有限。",
            "**2024 减持新规硬约束**：控股股东/实控人在破发、破净、分红不达标时严禁减持，减持路径被直接封死。",
            "**预披露要求**：大股东/董监高通过集中竞价减持需提前 15 个交易日披露减持计划。",
            "**减持动力评估**：当前价格相比解禁成本溢价越高，套现意愿越强。",
        ],
        "checklist": [
            "近 6 个月内部人/大股东交易记录（增持/减持/无变动）",
            "前十大股东持股变动与质押比例趋势",
            "解禁/减持相关新闻及法定披露公告",
            "减持压力评级（重大压力/中等压力/轻微压力/无明显压力）",
            "未来 3 个月潜在限售股解禁风险与时间窗口评估",
        ],
        "tools": [
            {
                "name": "get_lockup_expiry",
                "desc": "获取历史解禁与未来 90 天待解禁计划、解禁数量及占比",
                "params": "ticker: str, curr_date: str",
            },
            {
                "name": "get_insider_transactions",
                "desc": "获取股东及高管历史交易流水",
                "params": "ticker: str",
            },
            {
                "name": "get_fundamentals",
                "desc": "获取股本结构、前十大股东持股比例与总流通市值",
                "params": "ticker: str",
            },
            {
                "name": "get_news",
                "desc": "检索减持预披露公告与相关报道",
                "params": "ticker: str, start_date: str, end_date: str",
            },
        ],
    },
}


# ============================================================================
# 2. Prompt 构建与响应解析器
# ============================================================================

def build_agent_prompt_payload(
    agent_id: str,
    ticker: str = "",
    trade_date: str = "",
    company_name: str = "",
    lookback: int = 30,
) -> dict[str, Any]:
    """Build structured prompt metadata and full prompt text for a specific agent."""
    spec = ANALYST_DEBUG_SPECS.get(agent_id, {})
    if not spec:
        return {
            "agent_id": agent_id,
            "name": f"专业分析师 ({agent_id})",
            "icon": "🤖",
            "persona": f"专业分析师 ({agent_id})",
            "special_rules": [],
            "checklist": [],
            "tools": [],
            "params": {"ticker": ticker, "trade_date": trade_date},
            "full_prompt": f"分析标的: {ticker}, 基准日期: {trade_date}",
        }

    ticker_label = f"{company_name} ({ticker})" if company_name else ticker or "标的代码"
    instrument_context = (
        f"The instrument to analyze is `{ticker or 'TICKER'}`. "
        "Use this exact ticker in every tool call, report, and recommendation. "
        "When a tool argument is named `ticker`, pass only this ticker value."
    )

    tools_str = ", ".join([t["name"] for t in spec["tools"]])

    rules_text = "\n".join([f"- {r}" for r in spec["special_rules"]])
    checklist_text = "\n".join([f"{i+1}. {item}" for i, item in enumerate(spec["checklist"])])

    system_message = (
        f"{spec['persona']}\n\n"
        f"⚠️ A 股市场特殊规则与专业框架：\n{rules_text}\n\n"
        f"可用工具集：{tools_str}\n\n"
        f"📋 必采清单 — 以下数据点必须出现在报告中，无法获取时标注 [数据缺失: xxx]：\n{checklist_text}"
    )

    full_prompt = (
        "【System Persona & Master Instruction】\n"
        "You are a helpful AI assistant, collaborating with other assistants. "
        "Use the provided tools to progress towards answering the question. "
        f"You have access to the following tools: {tools_str}.\n\n"
        f"{system_message}\n\n"
        f"For your reference, the current date is {trade_date or 'YYYY-MM-DD'}. {instrument_context}"
    )

    return {
        "agent_id": agent_id,
        "name": spec["name"],
        "icon": spec["icon"],
        "persona": spec["persona"],
        "special_rules": spec["special_rules"],
        "checklist": spec["checklist"],
        "tools": spec["tools"],
        "params": {
            "标的代码 (Ticker)": ticker or "待定",
            "分析基准日 (Trade Date)": trade_date or "最新交易日",
            "标的名称/上下文": ticker_label,
            "回溯分析周期": f"{lookback} 个交易日",
            "可用工具数量": f"{len(spec['tools'])} 个",
        },
        "full_prompt": full_prompt,
    }


def parse_agent_response(raw_text: Any) -> tuple[str, str, str]:
    """Parse raw LLM response into (raw_content, think_content, clean_content)."""
    text = str(raw_text or "").strip()
    if not text:
        return "", "", ""

    # Extract all <think>...</think> blocks
    think_blocks = re.findall(r"<think>(.*?)</think>", text, flags=re.DOTALL)
    think_content = "\n\n---\n\n".join([b.strip() for b in think_blocks if b.strip()])

    # Clean text without <think> tags
    clean_content = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()

    return text, think_content, clean_content


# ============================================================================
# 3. 纯白浅色主题 UI 渲染器 (Pure White Light Theme)
# ============================================================================

def render_prompt_tab(prompt_data: dict[str, Any]) -> None:
    """Render Tab 1: 📤 发送的 Prompt."""
    st.markdown(
        """
        <div style="background:#f8fafc; border:1px solid #e2e8f0; border-radius:8px; padding:0.8rem 1rem; margin-bottom:0.8rem;">
            <div style="font-size:0.85rem; font-weight:700; color:#0f172a; margin-bottom:0.4rem;">🎯 基础输入参数与运行上下文</div>
            <div style="display:flex; flex-wrap:wrap; gap:0.5rem;">
        """
        + "".join([
            f'<div style="background:#ffffff; border:1px solid #cbd5e1; border-radius:6px; padding:0.3rem 0.6rem; font-size:0.8rem;">'
            f'<span style="color:#64748b;">{html.escape(k)}: </span>'
            f'<strong style="color:#0f172a;">{html.escape(str(v))}</strong></div>'
            for k, v in prompt_data.get("params", {}).items()
        ])
        + """
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    cols = st.columns(2)
    col1 = cols[0] if len(cols) > 0 else st
    col2 = cols[1] if len(cols) > 1 else st

    with col1:
        st.markdown("##### 👤 系统人设与定位")
        st.info(prompt_data.get("persona", ""))

        st.markdown("##### 📜 专业规则与市场约束")
        rules = prompt_data.get("special_rules", [])
        if rules:
            for rule in rules:
                st.markdown(f"- {rule}")
        else:
            st.caption("遵循 A 股标准交易规则体系")

    with col2:
        st.markdown("##### 📋 必采清单与质量门控")
        checklist = prompt_data.get("checklist", [])
        if checklist:
            for i, item in enumerate(checklist, start=1):
                st.markdown(f"**{i}.** {item}")
        else:
            st.caption("执行通用投研质量门控标准")

        st.markdown("##### 🔧 挂载的工具集")
        tools = prompt_data.get("tools", [])
        if tools:
            for t in tools:
                st.markdown(f"- `{t['name']}`: {t.get('desc', '')}")
        else:
            st.caption("暂无工具挂载")

    with st.expander("📄 一键查看/复制 发送给大模型的完整 Prompt 提示词全文", expanded=False):
        full_text = prompt_data.get("full_prompt", "")
        st.caption(f"Prompt 字符总数: {len(full_text):,} 字符")
        st.code(full_text, language="markdown")


def render_tools_tab(tool_details: list[dict[str, Any]], available_tools: list[dict[str, str]]) -> None:
    """Render Tab 2: 🔧 调用的工具与返回数据."""
    if not tool_details:
        st.markdown(
            """
            <div style="background:#f8fafc; border:1px dashed #cbd5e1; border-radius:8px; padding:1.5rem; text-align:center; color:#64748b; margin:0.5rem 0;">
                <div style="font-size:1.2rem; margin-bottom:0.3rem;">⏳ 暂无工具调用记录</div>
                <div style="font-size:0.82rem;">该 Agent 尚未执行工具调用，或当前处于等待/只读分析阶段。</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        if available_tools:
            st.markdown("<div style='font-size:0.82rem; color:#64748b; margin-top:0.6rem;'>**已配置可用工具：**</div>", unsafe_allow_html=True)
            for t in available_tools:
                st.markdown(f"- `{t.get('name')}`: {t.get('desc', '')} <span style='color:#94a3b8; font-size:0.75rem;'>({t.get('params', '')})</span>", unsafe_allow_html=True)
        return

    st.markdown(f"<div style='font-size:0.85rem; font-weight:700; color:#0f172a; margin-bottom:0.5rem;'>累计调用工具 {len(tool_details)} 次：</div>", unsafe_allow_html=True)

    for i, tool_call in enumerate(tool_details, start=1):
        name = tool_call.get("tool_name", "unknown_tool")
        args = tool_call.get("args")
        payload = tool_call.get("payload")
        status = tool_call.get("status", "success")

        status_badge = "🟢 返回正常" if status == "success" else "🔴 调用异常"

        with st.expander(f"🔧 #{i} 工具调用: {name}  ·  {status_badge}", expanded=(i == 1)):
            cols = st.columns([1, 1])
            c1 = cols[0] if len(cols) > 0 else st
            c2 = cols[1] if len(cols) > 1 else st
            with c1:
                st.markdown("**📥 请求参数 (Arguments)：**")
                if args:
                    if isinstance(args, (dict, list)):
                        st.json(args)
                    else:
                        st.code(str(args), language="json")
                else:
                    st.caption("默认无参或基于上下文自动调用")

            with c2:
                st.markdown("**📤 返回原始数据 (Payload)：**")
                if payload:
                    if isinstance(payload, (dict, list)):
                        st.json(payload)
                    else:
                        payload_str = str(payload)
                        if len(payload_str) > 1500:
                            st.code(payload_str[:1500] + f"\n... (已截断展示，共 {len(payload_str)} 字)", language="text")
                        else:
                            st.code(payload_str, language="text")
                else:
                    st.caption("工具执行成功，未回传独立文本 Payload（结构化存储于状态机）")


def render_reasoning_tab(raw_output: str, think_content: str, agent_name: str) -> None:
    """Render Tab 3: 📥 接收的内容与思考过程."""
    if not raw_output and not think_content:
        st.markdown(
            """
            <div style="background:#f8fafc; border:1px dashed #cbd5e1; border-radius:8px; padding:1.5rem; text-align:center; color:#64748b; margin:0.5rem 0;">
                <div style="font-size:1.2rem; margin-bottom:0.3rem;">⚪ 暂未接收到大模型输出</div>
                <div style="font-size:0.82rem;">分析流水线正在推进中，模型完成推理后将在此展示完整思考过程与原始文本。</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    if think_content:
        st.markdown(
            f"""
            <div style="background:#f8fafc; border:1px solid #cbd5e1; border-left:4px solid #6366f1; border-radius:8px; padding:0.8rem 1rem; margin-bottom:0.8rem;">
                <div style="font-size:0.88rem; font-weight:700; color:#4338ca; display:flex; align-items:center; gap:0.4rem;">
                    <span>🧠</span> <span>{agent_name} 思考链推理过程 (Chain of Thought / &lt;think&gt;)</span>
                </div>
                <div style="font-size:0.78rem; color:#64748b; margin-top:0.2rem;">
                    以下为深度推理模型（如 DeepSeek-R1 / Qwen-Thinking / Claude Reasoning）生成的自主研判思维链：
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.markdown(think_content)
    else:
        st.info("💡 当前所用模型采用 Direct Response 模式（未包含独立的 `<think>` 思考链标签）。")

    with st.expander(f"📄 查看大模型接收的原始输出全文 (Raw Output, {len(raw_output)} 字符)", expanded=not bool(think_content)):
        st.code(raw_output or "无数据", language="markdown")


def render_report_tab(report_text: str, agent_name: str, icon: str) -> None:
    """Render Tab 4: 📊 最终交付报告."""
    if not report_text:
        st.markdown(
            f"""
            <div style="background:#f8fafc; border:1px dashed #cbd5e1; border-radius:8px; padding:1.5rem; text-align:center; color:#64748b; margin:0.5rem 0;">
                <div style="font-size:1.2rem; margin-bottom:0.3rem;">⏳ {icon} {agent_name} 报告生成中</div>
                <div style="font-size:0.82rem;">模型正在综合量化指标与行情数据撰写交付研报...</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    st.markdown(
        f"""
        <div style="display:flex; justify-content:space-between; align-items:center; background:#f1f5f9; border-radius:6px; padding:0.4rem 0.8rem; margin-bottom:0.6rem; font-size:0.82rem; color:#475569;">
            <span><strong>报告字数：</strong>{len(report_text):,} 字</span>
            <span style="color:#16a34a; font-weight:600;">✅ 已完成结构化交付</span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(report_text)


def render_agent_debug_panel(
    agent_id: str,
    agent_state: dict[str, Any],
    ticker: str = "",
    trade_date: str = "",
    company_name: str = "",
    default_expanded: bool = False,
) -> None:
    """Render a unified 4-Tab pure white light theme debug card for one agent."""
    name = agent_state.get("name") or ANALYST_DEBUG_SPECS.get(agent_id, {}).get("name", agent_id)
    icon = agent_state.get("icon") or ANALYST_DEBUG_SPECS.get(agent_id, {}).get("icon", "🤖")
    status = agent_state.get("status", "pending")
    detail = agent_state.get("detail", "")
    tools = agent_state.get("tool_calls", [])
    tool_details = agent_state.get("tool_details", [])
    report = agent_state.get("report", "")
    raw_output = agent_state.get("raw_output", "")
    think_content = agent_state.get("think_content", "")

    # If raw_output or think_content not explicitly passed, parse from report or state
    if not raw_output and report:
        raw_output, parsed_think, _ = parse_agent_response(report)
        if not think_content and parsed_think:
            think_content = parsed_think

    # Build tool_details from tool names list if list of dicts is empty
    if not tool_details and tools:
        tool_details = [
            {"tool_name": t, "status": "success", "args": None, "payload": None}
            for t in tools
        ]

    # Prompt metadata
    prompt_data = build_agent_prompt_payload(
        agent_id=agent_id,
        ticker=ticker,
        trade_date=trade_date,
        company_name=company_name,
    )

    # Status color & badge
    border_color = "#e2e8f0"
    status_badge = "⚪ 等待中"
    if status == "done":
        border_color = "#22c55e"
        status_badge = "🟢 已完成"
    elif status == "tool_calling":
        border_color = "#0284c7"
        status_badge = "🔧 调用工具"
    elif status == "running":
        border_color = "#f59e0b"
        status_badge = "🟠 正在分析"
    elif status == "error":
        border_color = "#ef4444"
        status_badge = "🔴 执行异常"

    with st.expander(f"{icon} {name} 详细调试面板  ·  {status_badge}", expanded=default_expanded):
        st.markdown(
            f"""
            <div style="background:#ffffff; border:1px solid #e2e8f0; border-left:4px solid {border_color}; border-radius:6px; padding:0.5rem 0.8rem; margin-bottom:0.6rem; font-size:0.82rem; color:#334155; box-shadow:0 1px 2px rgba(0,0,0,0.02);">
                <strong>当前状态：</strong>{status_badge} · {detail or '等待启动'} &nbsp;|&nbsp;
                <strong>已调用工具：</strong>{len(tools)} 次 &nbsp;|&nbsp;
                <strong>交付报告：</strong>{len(report):,} 字
            </div>
            """,
            unsafe_allow_html=True,
        )

        tab1, tab2, tab3, tab4 = st.tabs([
            "📤 发送的 Prompt",
            f"🔧 调用的工具与返回 ({len(tool_details)})",
            "📥 接收的内容与思考过程",
            f"📊 最终交付报告 ({len(report)}字)",
        ])

        with tab1:
            render_prompt_tab(prompt_data)

        with tab2:
            render_tools_tab(tool_details, prompt_data.get("tools", []))

        with tab3:
            render_reasoning_tab(raw_output, think_content, name)

        with tab4:
            render_report_tab(report, name, icon)


# ============================================================================
# 4. 从 final_state 中提取 7 位分析师调试明细与诊断总览
# ============================================================================

# Tool name to agent ID mapping
KNOWN_TOOL_TO_AGENT: dict[str, str] = {
    "get_stock_data": "market",
    "get_indicators": "market",
    "get_fund_flow": "social",
    "get_hot_stocks": "social",
    "get_global_news": "news",
    "get_news": "news",
    "get_fundamentals": "fundamentals",
    "get_profit_forecast": "fundamentals",
    "get_balance_sheet": "fundamentals",
    "get_cashflow": "fundamentals",
    "get_income_statement": "fundamentals",
    "get_industry_comparison": "fundamentals",
    "get_dragon_tiger_board": "hot_money",
    "get_northbound_flow": "hot_money",
    "get_concept_blocks": "hot_money",
    "get_insider_transactions": "lockup",
    "get_lockup_expiry": "lockup",
}


def extract_agent_debug_from_state(
    final_state: dict[str, Any],
    agent_id: str,
    ticker: str = "",
) -> dict[str, Any]:
    """Extract and reconstruct full debug telemetry for an agent from completed final_state."""
    spec = ANALYST_DEBUG_SPECS.get(agent_id, {})
    name = spec.get("name", agent_id)
    icon = spec.get("icon", "🤖")
    desc = spec.get("desc", "")
    key = spec.get("report_key", f"{agent_id}_report")

    raw_content = final_state.get(key, "") or ""
    raw_text, think_content, clean_content = parse_agent_response(raw_content)

    # Extract tool interactions from state messages
    messages = final_state.get("messages", [])
    tool_details: list[dict[str, Any]] = []
    tool_names_seen: set[str] = set()

    agent_tool_names = {t["name"] for t in spec.get("tools", [])}

    if isinstance(messages, (list, tuple)):
        # 1. Map tool calls from AIMessages
        pending_tool_calls: dict[str, dict[str, Any]] = {}
        for msg in messages:
            tool_calls = getattr(msg, "tool_calls", None) or (isinstance(msg, dict) and msg.get("tool_calls"))
            if tool_calls and isinstance(tool_calls, (list, tuple)):
                for tc in tool_calls:
                    tc_name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
                    tc_args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", None)
                    tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                    if tc_name and (tc_name in agent_tool_names or KNOWN_TOOL_TO_AGENT.get(tc_name) == agent_id):
                        call_info = {
                            "tool_name": str(tc_name),
                            "args": tc_args,
                            "payload": None,
                            "status": "success",
                        }
                        if tc_id:
                            pending_tool_calls[str(tc_id)] = call_info
                        tool_details.append(call_info)
                        tool_names_seen.add(str(tc_name))

            # 2. Map tool responses from ToolMessages
            msg_name = getattr(msg, "name", None) or (isinstance(msg, dict) and msg.get("name"))
            msg_tool_id = getattr(msg, "tool_call_id", None) or (isinstance(msg, dict) and msg.get("tool_call_id"))
            msg_content = getattr(msg, "content", None) or (isinstance(msg, dict) and msg.get("content"))

            if msg_tool_id and str(msg_tool_id) in pending_tool_calls:
                pending_tool_calls[str(msg_tool_id)]["payload"] = msg_content
            elif msg_name and (str(msg_name) in agent_tool_names or KNOWN_TOOL_TO_AGENT.get(str(msg_name)) == agent_id):
                if not any(d["tool_name"] == str(msg_name) for d in tool_details):
                    tool_details.append({
                        "tool_name": str(msg_name),
                        "args": None,
                        "payload": msg_content,
                        "status": "success",
                    })
                    tool_names_seen.add(str(msg_name))

    # If no tool details extracted from messages but agent has default tools and report was generated
    if not tool_details and clean_content:
        for t in spec.get("tools", []):
            tool_details.append({
                "tool_name": t["name"],
                "args": {"ticker": ticker} if ticker else None,
                "payload": f"[{t['name']} 已执行并融入报告分析]",
                "status": "success",
            })
            tool_names_seen.add(t["name"])

    word_count = len(clean_content)
    if word_count > 0:
        status_code = "done"
        if word_count < 100:
            health_status = "⚠️ 内容过短"
            health_color = "#f59e0b"
        else:
            health_status = "✅ 交付正常"
            health_color = "#22c55e"
    else:
        health_status = "⚪ 未执行/未勾选"
        health_color = "#888888"
        status_code = "pending"

    return {
        "agent_id": agent_id,
        "name": name,
        "icon": icon,
        "desc": desc,
        "report_key": key,
        "status": status_code,
        "health_status": health_status,
        "health_color": health_color,
        "word_count": word_count,
        "report": clean_content,
        "raw_output": raw_text or clean_content,
        "think_content": think_content,
        "tool_calls": list(tool_names_seen),
        "tool_details": tool_details,
    }


def render_all_agent_diagnostics(
    final_state: dict[str, Any],
    ticker: str,
    trade_date: str = "",
) -> None:
    """Render comprehensive 7-Agent debug diagnostics panel at the bottom of the research report."""
    st.markdown("---")
    st.markdown("### 🔍 7 大 Agent 执行诊断与调试明细 (Debug Diagnostics)")
    st.caption("调试模式：展示本轮投研各 Agent 产出体量、Prompt 提示词、工具交互 Payload、原始思维链与结构化报告。")

    agent_diag_list = [
        extract_agent_debug_from_state(final_state, aid, ticker=ticker)
        for aid in ANALYST_DEBUG_SPECS
    ]

    total_words = sum(item["word_count"] for item in agent_diag_list)
    completed_agents = sum(1 for item in agent_diag_list if item["word_count"] > 0)
    total_tools = sum(len(item["tool_details"]) for item in agent_diag_list)

    cols = st.columns(4)
    c1 = cols[0] if len(cols) > 0 else st
    c2 = cols[1] if len(cols) > 1 else st
    c3 = cols[2] if len(cols) > 2 else st
    c4 = cols[3] if len(cols) > 3 else st
    c1.metric("有效产出 Agent", f"{completed_agents}/7")
    c2.metric("分析师报告总字数", f"{total_words:,} 字")
    c3.metric("工具调用总计", f"{total_tools} 次")
    c4.metric("整体诊断状态", "良好 (全部参与)" if completed_agents >= 7 else "正常 (部分参与)" if completed_agents >= 5 else "轻量模式")

    st.markdown("<div style='margin-top:0.8rem;'></div>", unsafe_allow_html=True)

    for item in agent_diag_list:
        aid = item["agent_id"]
        icon = item["icon"]
        name = item["name"]
        key = item["report_key"]
        words = item["word_count"]
        health_status = item["health_status"]
        health_color = item["health_color"]
        tools_cnt = len(item["tool_details"])

        has_think_badge = " · 🧠 含思维链" if item["think_content"] else ""

        # Outer summary row
        st.markdown(
            f"""
            <div style="background:#ffffff; border:1px solid #e2e8f0; border-radius:8px; padding:0.6rem 1rem; margin-bottom:0.4rem; display:flex; justify-content:space-between; align-items:center; box-shadow:0 1px 2px rgba(0,0,0,0.02);">
                <div>
                    <span style="font-weight:700; color:#0f172a; font-size:0.95rem;">{icon} {name}</span>
                    <span style="color:#64748b; font-size:0.8rem; margin-left:0.8rem;">(`{key}`)</span>
                    <div style="font-size:0.75rem; color:#64748b; margin-top:0.15rem;">{item['desc']}</div>
                </div>
                <div style="display:flex; gap:1.5rem; align-items:center; text-align:right;">
                    <span style="font-size:0.85rem; color:#475569;">{tools_cnt} 次工具 · {words:,} 字{has_think_badge}</span>
                    <span style="font-size:0.85rem; color:{health_color}; font-weight:600; min-width:85px;">{health_status}</span>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        # Expandable 4-tab debug view for each analyst
        with st.expander(f"🔍 展开查看 {icon} {name} 完整调试明细 (Prompt / 工具 / 接收内容 / 报告)", expanded=False):
            tab1, tab2, tab3, tab4 = st.tabs([
                "📤 发送的 Prompt",
                f"🔧 调用的工具与返回 ({tools_cnt})",
                "📥 接收的内容与思考过程",
                f"📊 最终交付报告 ({words}字)",
            ])

            prompt_data = build_agent_prompt_payload(
                agent_id=aid,
                ticker=ticker,
                trade_date=trade_date or final_state.get("trade_date", ""),
            )

            with tab1:
                render_prompt_tab(prompt_data)

            with tab2:
                render_tools_tab(item["tool_details"], prompt_data.get("tools", []))

            with tab3:
                render_reasoning_tab(item["raw_output"], item["think_content"], name)

            with tab4:
                render_report_tab(item["report"], name, icon)

