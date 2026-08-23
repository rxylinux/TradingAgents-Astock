"""指数版 Agent 工厂：与个股版 agent 文件完全隔离的指数 prompt 与节点。

设计原则（对应架构评审要求）：
- **个股版 agent 文件零改动**。本模块提供与个股版同签名 ``(llm) -> node_fn``
  的指数工厂，经 `GraphSetup(node_factories=...)` 依赖注入替换对应节点；
- 图拓扑、条件边、质量门控、记忆系统全部复用原实现——只有 7 个分析师中
  指数适用的 5 个（market/social/news/policy/hot_money）与
  bull/bear/trader/portfolio_manager 换用指数版；
- risk 三方辩手 / research_manager / quality_gate 复用原版（其框架与标的
  类型无关或天然市场级）。

指数分析视角总纲：分析对象是市场指数而非个股——指数没有财报/解禁/龙虎榜/
涨跌停/T+1 等个股概念，评级表达的是**对市场整体的方向性观点**（可通过对应
指数 ETF / 股指期货落地）。
"""

from __future__ import annotations

import functools

from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.schemas import (
    PortfolioDecision,
    TraderProposal,
    render_pm_decision,
    render_trader_proposal,
)
from tradingagents.agents.utils.agent_utils import (
    filter_analyst_messages,
    get_language_instruction,
)
from tradingagents.agents.utils.core_stock_tools import get_stock_data
from tradingagents.agents.utils.news_data_tools import get_global_news, get_news
from tradingagents.agents.utils.structured import (
    bind_structured,
    invoke_structured_or_freetext,
)
from tradingagents.agents.utils.technical_indicators_tools import get_indicators
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.index_registry import parse_index_ticker
from tradingagents.agents.utils.signal_data_tools import (
    get_fund_flow,
    get_hot_stocks,
    get_industry_comparison,
    get_northbound_flow,
)


def build_index_context(ticker: str) -> str:
    """指数版 instrument context：要求工具调用严格传指数标识（000001.SH 形式）。"""
    return (
        f"The instrument to analyze is `{ticker}`, a China A-share market INDEX "
        "(e.g. 上证指数/沪深300/创业板指), NOT an individual stock. "
        "Use this exact index id in every tool call, report, and recommendation. "
        "When a tool argument is named `ticker`, pass only this value; do not pass "
        "company names, sectors, concepts, or search keywords. Ratings express a "
        "directional view on the overall market (executable via index ETFs / "
        "index futures), not on a single company."
    )


# ---------------------------------------------------------------------------
# 1. 市场（技术面）分析师 · 指数版
# ---------------------------------------------------------------------------

_MARKET_INDEX_PROMPT = """你是一位专注于 A 股市场的指数技术分析师。当前分析对象是**市场指数**（如上证指数、沪深300、创业板指），不是个股——指数没有涨跌停、T+1、最小手数、换手率（相对流通盘）等个股概念，你的任务是研判**市场整体的技术状态与趋势结构**。从以下技术指标中选择最多 **8 个**最相关的指标，注重互补性、避免冗余。

指数技术分析要点：
- **趋势结构**：指数与 50/200 日均线的位置关系、均线多空排列、斜率变化
- **量能**：指数成交额/成交量的放大与萎缩是行情性质（真实突破 vs 缩量反弹）的核心判据
- **波动率状态**：ATR/布林带宽度收缩后的方向选择、极端波动后的均值回归
- **关键点位**：整数关口、前期高低点、密集成交区构成的支撑/阻力
- **动量与背离**：RSI/MACD 与指数走势的背离常先于趋势转折

可选技术指标（调用 get_indicators 时必须使用下列英文标识符作为参数名）：
- close_50_sma：50 日简单均线 - 中期趋势方向
- close_200_sma：200 日简单均线 - 长期趋势基准（牛熊分界参考）
- close_10_ema：10 日指数均线 - 短期动量
- macd / macds / macdh：MACD 主线/信号线/柱状图 - 趋势动量与背离
- rsi：RSI 相对强弱 - 超买超卖（指数环境阈值比个股更有效）
- boll / boll_ub / boll_lb：布林带中轨/上轨/下轨 - 波动率与相对位置
- atr：ATR 平均真实波幅 - 市场波动状态
- vwma：量价加权均线 - 量价配合验证

操作要求：
1. **必须**先调用 get_stock_data 获取指数 K 线（ticker 传指数标识，如 000001.SH）
2. 再调用 get_indicators 获取选定指标（参数名使用上述英文标识符）
3. 调用 get_stock_data 和 get_indicators 时，**look_back_days 参数一律传 {lookback}**（用户指定的分析回溯区间）
4. 撰写详细的指数技术分析报告，包含具体点位数值和信号研判结论（仅供研究参考，不构成投资建议）
5. 报告末尾附 Markdown 表格汇总关键技术信号和结论

📋 必采清单 — 以下数据点必须出现在报告中，无法获取时标注 [数据缺失: xxx]：
1. 最新收盘点位、日期、当日涨跌幅
2. 近 {lookback} 日累计涨跌幅
3. 近 5 日平均成交量 vs 近 20 日平均成交量（放量/缩量判断）
4. 至少 3 个技术指标的当前数值和多空信号
5. 关键支撑位和阻力位"""


def create_index_market_analyst(llm):
    def market_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = build_index_context(state["company_of_interest"])
        lookback = get_config().get("market_lookback_days") or 30

        tools = [get_stock_data, get_indicators]

        system_message = (
            _MARKET_INDEX_PROMPT.format(lookback=lookback)
            + get_language_instruction()
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " If you or any other assistant has the FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** or deliverable,"
                    " prefix your response with FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** so the team knows to stop."
                    " You have access to the following tools: {tool_names}.\n{system_message}"
                    "For your reference, the current date is {current_date}. {instrument_context}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in tools]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        chain = prompt | llm.bind_tools(tools)
        filtered_messages = filter_analyst_messages(
            state.get("messages", []), tools, state.get("company_of_interest", "")
        )
        result = chain.invoke(filtered_messages)

        report = "" if len(result.tool_calls) else result.content
        return {"messages": [result], "market_report": report}

    return market_analyst_node


# ---------------------------------------------------------------------------
# 2. 情绪分析师 · 指数版（全市场赚钱效应视角）
# ---------------------------------------------------------------------------

_SOCIAL_INDEX_PROMPT = (
    "你是一位专注于 A 股市场的整体情绪分析师。当前分析对象是**市场指数**"
    "（如上证指数、沪深300、创业板指），你要判断的是**全市场情绪与赚钱效应**，"
    "不是某只个股的舆情。"
    "\n\n⚠️ 指数情绪分析框架："
    "\n- **赚钱效应**：涨停家数与连板高度、炸板率、强势题材的持续性，是散户参与意愿的直接驱动"
    "\n- **资金面情绪**：指数主力资金净流入/流出、北向资金方向，是机构态度的硬证据"
    "\n- **量价情绪**：指数放量上涨=情绪升温，缩量反弹=参与意愿不足，放量下跌=恐慌宣泄"
    "\n- **反向指标**：全场一致看多（涨停潮+开户热）往往是阶段性顶部信号，反之亦然"
    "\n- **时间维度**：区分单日事件驱动的短期波动与 1-4 周的情绪趋势"
    "\n\n🔧 工具与取数顺序（ticker 一律传指数标识，如 000001.SH）："
    "\n1. `get_fund_flow(ticker, curr_date)` — 指数主力/超大单/大单资金净流入。**情绪最硬的证据。**"
    "\n2. `get_stock_data(ticker, start_date, end_date)` — 指数量价，判断情绪强度与持续性。"
    "\n3. `get_hot_stocks(curr_date)` — 当日涨停股与题材归因榜：涨停家数、题材集中度、赚钱效应。"
    "\n4. `get_news(ticker, start_date, end_date)` — 指数相关新闻，解释情绪成因。"
    "\n\n⚠️ 分析纪律 —— 情绪判断必须落在数据上，不能只凭新闻语气推断："
    "\n- **先看资金与赚钱效应，再看新闻**。"
    "\n- **背离必须写出来**：消息面偏暖但主力资金持续净流出（或相反）是最有价值的信号。"
    "\n- 数据取不到时如实标注，**不要编造数字**。"
    "\n\n撰写详细的指数情绪分析报告，给出全市场情绪评分（极度悲观/悲观/中性/乐观/极度乐观）与趋势判断。报告末尾附 Markdown 表格汇总情绪信号和结论。"
    "\n\n📋 必采清单 — 以下数据点必须出现在报告中，无法获取时标注 [数据缺失: xxx]："
    "\n1. 指数主力资金当日净流入、近 20 日累计方向"
    "\n2. 当日涨停/强势股家数与主流题材（来自 get_hot_stocks）"
    "\n3. 指数近期量能变化（放量/缩量）"
    "\n4. 新闻检索条数与正负面比例"
    "\n5. **资金面与消息面是否背离**（一致/背离，背离时说明方向）"
    "\n6. 全市场情绪评分（极度悲观/悲观/中性/乐观/极度乐观）"
    "\n7. 情绪趋势变化方向（升温/降温/平稳）"
)


def create_index_social_analyst(llm):
    def social_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = build_index_context(state["company_of_interest"])

        tools = [get_news, get_fund_flow, get_hot_stocks, get_stock_data]

        system_message = _SOCIAL_INDEX_PROMPT + get_language_instruction()

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " If you or any other assistant has the FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** or deliverable,"
                    " prefix your response with FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** so the team knows to stop."
                    " You have access to the following tools: {tool_names}.\n{system_message}"
                    "For your reference, the current date is {current_date}. {instrument_context}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in tools]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        chain = prompt | llm.bind_tools(tools)
        filtered_messages = filter_analyst_messages(
            state.get("messages", []), tools, state.get("company_of_interest", "")
        )
        result = chain.invoke(filtered_messages)

        report = "" if len(result.tool_calls) else result.content
        return {"messages": [result], "sentiment_report": report}

    return social_analyst_node


# ---------------------------------------------------------------------------
# 3. 新闻分析师 · 指数版（系统性影响视角）
# ---------------------------------------------------------------------------

_NEWS_INDEX_PROMPT = (
    "你是一位专注于 A 股市场的宏观与市场新闻分析师。当前分析对象是**市场指数**"
    "（如上证指数、沪深300、创业板指），你的任务是评估近期新闻动态对大盘/该指数的**系统性影响**。"
    "\n\n⚠️ 指数新闻分析框架："
    "\n- **宏观驱动**：货币（降准降息/流动性投放）、财政、经济数据（PMI/CPI/社融）是指数走向的核心变量"
    "\n- **资金面事件**：央行动作、IPO/再融资节奏、重要股东减持高峰、新基金发行——影响市场供需"
    "\n- **外围市场**：美股/港股表现、人民币汇率、大宗商品对 A 股的开盘与情绪传导"
    "\n- **监管与地缘**：证监会表态、中美关系、地缘事件对风险偏好的冲击"
    "\n- **消息来源权重**：财联社快讯（最快）> 新华财经/证券时报（权威）> 门户转载；区分官方消息与传闻"
    "\n\n请使用以下工具："
    "\n- `get_news(ticker, start_date, end_date)`：获取指数相关新闻，ticker 传指数标识（如 000001.SH）"
    "\n- `get_global_news(curr_date, look_back_days, limit)`：宏观与市场整体新闻"
    "\n\n撰写全面的指数新闻分析报告，区分利好/利空/中性消息，评估系统性影响程度与持续时间。报告末尾附 Markdown 表格汇总关键事件及其影响评级。"
    "\n\n📋 必采清单 — 以下数据点必须出现在报告中，无法获取时标注 [数据缺失: xxx]："
    "\n1. 指数新闻与宏观新闻条数、时间范围"
    "\n2. 关键事件时间线（至少 3 个重要事件及日期）"
    "\n3. 利好/利空/中性事件分类统计"
    "\n4. 系统性风险事件清单（如有）"
    "\n5. 对指数的方向性影响评估"
)


def create_index_news_analyst(llm):
    def news_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = build_index_context(state["company_of_interest"])

        tools = [get_news, get_global_news]

        system_message = _NEWS_INDEX_PROMPT + get_language_instruction()

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " If you or any other assistant has the FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** or deliverable,"
                    " prefix your response with FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** so the team knows to stop."
                    " You have access to the following tools: {tool_names}.\n{system_message}"
                    "For your reference, the current date is {current_date}. {instrument_context}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in tools]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        chain = prompt | llm.bind_tools(tools)
        filtered_messages = filter_analyst_messages(
            state.get("messages", []), tools, state.get("company_of_interest", "")
        )
        result = chain.invoke(filtered_messages)

        report = "" if len(result.tool_calls) else result.content
        return {"messages": [result], "news_report": report}

    return news_analyst_node


# ---------------------------------------------------------------------------
# 4. 政策分析师 · 指数版（风险偏好/流动性/增量资金传导）
# ---------------------------------------------------------------------------

_POLICY_INDEX_PROMPT = (
    "你是一位专注于 A 股市场的政策分析师。当前分析对象是**市场指数**"
    "（如上证指数、沪深300、创业板指），你的核心任务是解读政策动态对**市场整体**"
    "的影响——通过风险偏好、流动性与增量资金三条传导链。"
    "\n\n⚠️ 政策分析框架（指数视角）："
    "\n- **宏观政策层**：货币（降准/降息/MLF/LPR）、财政（专项债/减税）、汇率——直接影响市场流动性与风险偏好"
    "\n- **监管政策层**：证监会（IPO 节奏/再融资/减持新规/退市/交易制度）、银保监信贷、发改委——影响资金供需与市场结构"
    "\n- **产业政策层**：重点扶持方向（新质生产力/半导体/新能源等）决定指数内的板块轮动主线，宽基指数看权重行业受益方向，行业指数看对应产业政策"
    "\n- **国际政策层**：中美关系、出口管制、关税、美联储政策——影响外资流向与全球风险偏好"
    "\n\n分析方法："
    "\n1. 识别近期政策事件，评估力度级别：指导意见（弱）< 部委通知（中）< 国务院文件（强）< 法律法规（最强）"
    "\n2. 判断影响窗口：短期脉冲（1-2 周）vs 中期趋势（1-3 月）vs 长期结构性（半年以上）"
    "\n3. 分析传导链：政策 → 流动性/风险偏好/增量资金 → 指数方向"
    "\n\n请使用以下工具："
    "\n- `get_news(ticker, start_date, end_date)`：搜索指数/宏观相关政策新闻，ticker 传指数标识（如 000001.SH）"
    "\n- `get_global_news(curr_date, look_back_days, limit)`：宏观经济和政策面新闻"
    "\n\n撰写详细的政策分析报告，明确给出政策面对该指数的总体评级（重大利好/利好/中性/利空/重大利空）。报告末尾附 Markdown 表格列出关键政策事件、影响方向和持续时间。"
    "\n\n📋 必采清单 — 以下数据点必须出现在报告中，无法获取时标注 [数据缺失: xxx]："
    "\n1. 近期相关政策事件清单（含发布日期和发布机构）"
    "\n2. 政策对流动性与风险偏好的方向判断"
    "\n3. 政策影响力度评级（强/中/弱）"
    "\n4. 政策影响时间窗口估算"
    "\n5. 政策面对该指数的总体评级"
)


def create_index_policy_analyst(llm):
    def policy_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = build_index_context(state["company_of_interest"])

        tools = [get_news, get_global_news]

        system_message = _POLICY_INDEX_PROMPT + get_language_instruction()

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " If you or any other assistant has the FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** or deliverable,"
                    " prefix your response with FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** so the team knows to stop."
                    " You have access to the following tools: {tool_names}.\n{system_message}"
                    "For your reference, the current date is {current_date}. {instrument_context}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in tools]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        chain = prompt | llm.bind_tools(tools)
        filtered_messages = filter_analyst_messages(
            state.get("messages", []), tools, state.get("company_of_interest", "")
        )
        result = chain.invoke(filtered_messages)

        report = "" if len(result.tool_calls) else result.content
        return {"messages": [result], "policy_report": report}

    return policy_analyst_node


# ---------------------------------------------------------------------------
# 5. 大盘资金流分析师（hot_money 的指数版——本功能的核心增量）
# ---------------------------------------------------------------------------

_HOT_MONEY_INDEX_PROMPT = (
    "你是一位专注于 A 股市场的**大盘资金流分析师**。当前分析对象是市场指数"
    "（如上证指数/沪深300/创业板指），你的核心任务是刻画市场整体的资金格局："
    "内资主力在进还是出、外资方向如何、资金正聚集在哪些方向。"
    "\n\n⚠️ 大盘资金流分析框架："
    "\n- **指数级资金**：指数本身的主力/超大单/大单净流入（分钟级当日 + 近 20 日趋势），反映权重股上的大资金态度"
    "\n- **外资**：北向资金（沪股通+深股通）当日净流入与近 20 日均值对比，是 A 股最重要的边际资金之一"
    "\n- **板块轮动**：全行业涨跌幅排名与领涨股，判断资金聚集/撤离的行业方向（宽基指数看权重行业，行业指数看对应板块）"
    "\n- **赚钱效应**：涨停家数、题材集中度（热门股榜），反映存量博弈下散户资金的活跃度"
    "\n- **量能验证**：指数成交量放大/萎缩验证资金判断（放量流入 > 缩量流入 > 放量流出 > 缩量流出）"
    "\n\n🔧 工具与取数顺序（ticker 一律传指数标识，如 000001.SH）："
    "\n1. `get_fund_flow(ticker, curr_date)` — 指数主力资金净流入（当日分钟 + 近 20 日），**核心数据**"
    "\n2. `get_northbound_flow(curr_date, include_history=True)` — 北向资金净流入与历史对比"
    "\n3. `get_industry_comparison(ticker, trade_date)` — 全行业涨跌幅排名，判断资金聚集方向"
    "\n4. `get_hot_stocks(curr_date)` — 涨停/强势股题材归因，赚钱效应与热点分布"
    "\n5. `get_stock_data(ticker, start_date, end_date)` — 指数量价，验证资金与量能是否一致"
    "\n6. `get_news(ticker, start_date, end_date)` — 资金面相关新闻（增量资金/IPO/减持等供需事件）"
    "\n\n⚠️ 对指数分析，`get_insider_transactions` / `get_concept_blocks` / `get_dragon_tiger_board` "
    "不适用（会返回「指数无此数据」的提示），**不要调用这三个工具**。"
    "\n\n撰写详细的大盘资金流报告，给出资金面总体判断（主力流入/主力流出/存量博弈/无明显信号）与对指数的含义（仅供研究参考，不构成投资建议）。报告末尾附 Markdown 表格汇总各维度资金信号和结论。"
    "\n\n📋 必采清单 — 以下数据点必须出现在报告中，无法获取时标注 [数据缺失: xxx]："
    "\n1. 指数主力资金当日净流入金额、近 20 日累计方向"
    "\n2. 北向资金当日净流入（沪股通 + 深股通）"
    "\n3. 领涨行业 TOP3 与领跌行业 TOP3（资金聚集/撤离方向）"
    "\n4. 当日涨停/强势股家数与题材集中度"
    "\n5. 指数量能变化（放量/缩量）"
    "\n6. 资金面总体判断（主力流入/主力流出/存量博弈/无明显信号）"
)


def create_index_hot_money_tracker(llm):
    def hot_money_tracker_node(state):
        current_date = state["trade_date"]
        instrument_context = build_index_context(state["company_of_interest"])

        tools = [
            get_stock_data,
            get_news,
            get_hot_stocks,
            get_northbound_flow,
            get_fund_flow,
            get_industry_comparison,
        ]

        system_message = _HOT_MONEY_INDEX_PROMPT + get_language_instruction()

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " If you or any other assistant has the FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** or deliverable,"
                    " prefix your response with FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** so the team knows to stop."
                    " You have access to the following tools: {tool_names}.\n{system_message}"
                    "For your reference, the current date is {current_date}. {instrument_context}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in tools]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        chain = prompt | llm.bind_tools(tools)
        filtered_messages = filter_analyst_messages(
            state.get("messages", []), tools, state.get("company_of_interest", "")
        )
        result = chain.invoke(filtered_messages)

        report = "" if len(result.tool_calls) else result.content
        return {"messages": [result], "hot_money_report": report}

    return hot_money_tracker_node


# ---------------------------------------------------------------------------
# 6. Bull / Bear · 指数版辩论框架
# ---------------------------------------------------------------------------


def create_index_bull_researcher(llm):
    def bull_node(state) -> dict:
        debate = state["investment_debate_state"]
        history = debate.get("history", "")
        bull_history = debate.get("bull_history", "")
        current_response = debate.get("current_response", "")
        data_quality_summary = state.get("data_quality_summary", "")

        reports = "\n\n".join(
            part
            for part in [
                f"Index technical report: {state['market_report']}",
                f"Market sentiment report: {state['sentiment_report']}",
                f"Index news report: {state['news_report']}",
                f"Policy report: {state.get('policy_report', '')}",
                f"Market fund flow report: {state.get('hot_money_report', '')}",
            ]
            if part.split(": ", 1)[-1]
        )

        prompt = f"""You are a Bull Analyst advocating a constructive view on this China A-share market INDEX (e.g. 上证指数/沪深300/创业板指). Build a strong, evidence-based case for increasing overall market exposure. Leverage the analysts' reports and counter bearish arguments effectively.

Index Bull Framework — prioritize these market-level bullish drivers:
- Liquidity Tailwinds: monetary easing (RRR/rate cuts), abundant interbank liquidity, new fund issuance — rising tide lifts the index
- Policy Put: supportive regulatory signals (slower IPO pace, margin rules, buyback encouragement) that lift risk appetite
- Northbound Capital: sustained foreign net inflow via Stock Connect confirms institutional conviction
- Fund Flow Structure: index main-force net inflow with volume confirmation; sector rotation broadening (not narrowing) supports further upside
- Technical Structure: index above rising 50/200-day averages, orderly pullbacks on shrinking volume, support levels holding
- Valuation Context: index-level valuation percentile below historical average leaves room for re-rating

General bull points:
- Trend & Momentum: cite specific index levels, moving averages and indicator values from the technical report
- Breadth: advancing breadth, sustained limit-up counts and theme persistence from the sentiment/hot-money reports
- Bear Counterpoints: address the bear's specific objections with data from the reports
- Engagement: argue conversationally against the bear's last point

Reports available:
{reports}
Data quality assessment: {data_quality_summary}
Conversation history of the debate: {history}
Last bear argument: {current_response}

⚠️ If the data quality assessment flags any report as low-confidence (grade C/D/F), reduce your reliance on that report and note the limitation.

Deliver a compelling bull argument for the overall market. Refute the bear's concerns and demonstrate why increasing index exposure holds stronger merit. Remember: the instrument is an index — speak of market exposure (e.g. via index ETFs), never of a single company's shares.{get_language_instruction()}
"""

        response = llm.invoke(prompt)
        argument = f"Bull Analyst: {response.content}"

        new_debate_state = {
            "history": history + "\n" + argument,
            "bull_history": bull_history + "\n" + argument,
            "bear_history": debate.get("bear_history", ""),
            "current_response": argument,
            "count": debate["count"] + 1,
        }
        return {"investment_debate_state": new_debate_state}

    return bull_node


def create_index_bear_researcher(llm):
    def bear_node(state) -> dict:
        debate = state["investment_debate_state"]
        history = debate.get("history", "")
        bear_history = debate.get("bear_history", "")
        current_response = debate.get("current_response", "")
        data_quality_summary = state.get("data_quality_summary", "")

        reports = "\n\n".join(
            part
            for part in [
                f"Index technical report: {state['market_report']}",
                f"Market sentiment report: {state['sentiment_report']}",
                f"Index news report: {state['news_report']}",
                f"Policy report: {state.get('policy_report', '')}",
                f"Market fund flow report: {state.get('hot_money_report', '')}",
            ]
            if part.split(": ", 1)[-1]
        )

        prompt = f"""You are a Bear Analyst arguing for caution on this China A-share market INDEX (e.g. 上证指数/沪深300/创业板指). Build a rigorous, evidence-based case for reducing overall market exposure or staying defensive.

Index Bear Framework — prioritize these market-level bearish risks:
- Liquidity Withdrawal: tightening signals, IPO/refinancing acceleration, major shareholder reduction waves draining market supply-demand
- Policy Risk: regulatory U-turns or window guidance that crush risk appetite; policy expectations already priced in
- Northbound Retreat: sustained foreign net outflow via Stock Connect — foreign institutions voting with feet
- Fund Flow Deterioration: index main-force net outflow, narrowing breadth, theme concentration (last leg of a rally), volume divergence on new highs
- Technical Breakdown: index below key moving averages, bearish MACD/RSI divergences, failure at resistance on weak volume
- External Shocks: US rate path, RMB depreciation pressure, geopolitical escalation — all transmit to the index overnight

General bear points:
- Downside Scenarios: cite specific index levels, supports likely to break, and indicator values from the technical report
- Crowding & Sentiment: one-sided optimism (limit-up mania, uniform bullishness) as a contrarian top signal
- Bull Counterpoints: rebut the bull's arguments with data from the reports
- Engagement: argue conversationally against the bull's last point

Reports available:
{reports}
Data quality assessment: {data_quality_summary}
Conversation history of the debate: {history}
Last bull argument: {current_response}

⚠️ If the data quality assessment flags any report as low-confidence (grade C/D/F), reduce your reliance on that report and note the limitation.

Deliver a rigorous bear argument on the overall market. Expose the weaknesses in the bull's thesis and explain why reducing index exposure (or staying defensive) is the sounder stance. Remember: the instrument is an index — speak of market exposure (e.g. via index ETFs), never of a single company's shares.{get_language_instruction()}
"""

        response = llm.invoke(prompt)
        argument = f"Bear Analyst: {response.content}"

        new_debate_state = {
            "history": history + "\n" + argument,
            "bear_history": bear_history + "\n" + argument,
            "bull_history": debate.get("bull_history", ""),
            "current_response": argument,
            "count": debate["count"] + 1,
        }
        return {"investment_debate_state": new_debate_state}

    return bear_node


# ---------------------------------------------------------------------------
# 7. Trader · 指数版（不可直接交易指数 → ETF/期指方向/仓位取向）
# ---------------------------------------------------------------------------

_NO_LEVELS_INSTRUCTION = (
    "Explain the reasoning behind the direction. Do NOT state entry prices, "
    "stop-loss levels, target prices or position sizes."
)

_INDEX_TRADER_SYSTEM = (
    "You are a trading agent translating the Research Manager's plan into a "
    "structured directional view for a China A-share market INDEX. The index "
    "itself is NOT directly tradable: express the view as a directional stance on "
    "the overall market — e.g. via the corresponding index ETF or stock-index "
    "futures, or as a recommended overall equity allocation stance. Do NOT apply "
    "single-stock T+1/price-limit/lot-size constraints to the index itself; index "
    "ETFs are T+1 (like ordinary A-share ETFs) unless the product states "
    "otherwise. "
    f"{_NO_LEVELS_INSTRUCTION} "
    "（以上参数仅供技术研究参考，不构成投资建议）"
)


def create_index_trader(llm):
    structured_llm = bind_structured(llm, TraderProposal, "Trader")

    def trader_node(state, name):
        company_name = state["company_of_interest"]
        instrument_context = build_index_context(company_name)
        investment_plan = state["investment_plan"]

        astock_context_parts = []
        if state.get("policy_report", ""):
            astock_context_parts.append(
                f"Policy Analysis Report:\n{state['policy_report']}"
            )
        if state.get("hot_money_report", ""):
            astock_context_parts.append(
                f"Market Fund Flow Report:\n{state['hot_money_report']}"
            )
        astock_context = "\n\n".join(astock_context_parts)

        messages = [
            {"role": "system", "content": _INDEX_TRADER_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"Based on a comprehensive index analysis by a team of analysts "
                    f"(technical, sentiment, news, policy, and market fund-flow "
                    f"specialists), here is an investment plan for {company_name}.\n\n"
                    f"{instrument_context}\n\n"
                    f"Proposed Investment Plan:\n{investment_plan}\n\n"
                    + (
                        f"Additional Analyst Context:\n{astock_context}\n\n"
                        if astock_context
                        else ""
                    )
                    + "Leverage these insights to craft the directional view."
                    + get_language_instruction()
                ),
            },
        ]

        trader_plan = invoke_structured_or_freetext(
            structured_llm, llm, messages, render_trader_proposal, "Trader"
        )

        return {
            "messages": [AIMessage(content=trader_plan)],
            "trader_investment_plan": trader_plan,
            "sender": name,
        }

    return functools.partial(trader_node, name="Trader")


# ---------------------------------------------------------------------------
# 8. Portfolio Manager · 指数版
# ---------------------------------------------------------------------------

_INDEX_PM_CONSTRAINTS = """**Instrument Type** (must factor into your decision):
- The instrument is a market INDEX (e.g. 上证指数/沪深300/创业板指), not a single stock.
- The index itself is not directly tradable: interpret the rating as a directional view on
  overall market exposure — executable via index ETFs / stock-index futures, or as an
  overall equity allocation stance.
- Buy/Overweight ⇒ increase overall market exposure; Underweight/Sell ⇒ reduce it.
- Do NOT apply single-stock T+1/price-limit/lot-size constraints to the index."""


def create_index_portfolio_manager(llm):
    structured_llm = bind_structured(llm, PortfolioDecision, "Portfolio Manager")

    def portfolio_manager_node(state) -> dict:
        instrument_context = build_index_context(state["company_of_interest"])

        history = state["risk_debate_state"]["history"]
        risk_debate_state = state["risk_debate_state"]
        research_plan = state["investment_plan"]
        trader_plan = state["trader_investment_plan"]

        past_context = state.get("past_context", "")
        lessons_line = (
            f"- Lessons from prior decisions and outcomes:\n{past_context}\n"
            if past_context
            else ""
        )

        no_levels_rule = (
            "\n- Do NOT state entry prices, stop-loss levels, target prices or "
            "position sizes; give the rating and the reasoning."
        )

        prompt = f"""As the Portfolio Manager, synthesize the risk analysts' debate and deliver the final decision for this market INDEX.

{instrument_context}

---

{_INDEX_PM_CONSTRAINTS}

---

**Rating Scale** (use exactly one — interpret as overall market exposure):
- **Buy**: Strong conviction to increase overall market exposure
- **Overweight**: Favorable outlook, gradually increase exposure
- **Hold**: Maintain current exposure, no action needed
- **Underweight**: Reduce exposure, take partial profits
- **Sell**: Exit / sharply reduce overall market exposure

**Context:**
- Research Manager's investment plan: **{research_plan}**
- Trader's directional proposal: **{trader_plan}**
{lessons_line}
**Risk Analysts Debate History:**
{history}

---

Be decisive and ground every conclusion in specific evidence from the analysts.{no_levels_rule}{get_language_instruction()}"""

        final_trade_decision = invoke_structured_or_freetext(
            structured_llm, llm, prompt, render_pm_decision, "Portfolio Manager"
        )

        new_risk_debate_state = {
            "judge_decision": final_trade_decision,
            "history": risk_debate_state["history"],
            "aggressive_history": risk_debate_state["aggressive_history"],
            "conservative_history": risk_debate_state["conservative_history"],
            "neutral_history": risk_debate_state["neutral_history"],
            "latest_speaker": "Judge",
            "current_aggressive_response": risk_debate_state["current_aggressive_response"],
            "current_conservative_response": risk_debate_state["current_conservative_response"],
            "current_neutral_response": risk_debate_state["current_neutral_response"],
            "count": risk_debate_state["count"],
        }

        return {
            "risk_debate_state": new_risk_debate_state,
            "final_trade_decision": final_trade_decision,
        }

    return portfolio_manager_node


# ---------------------------------------------------------------------------
# 注入表：GraphSetup(node_factories=INDEX_NODE_FACTORIES) 使用
# ---------------------------------------------------------------------------

INDEX_NODE_FACTORIES = {
    "market": create_index_market_analyst,
    "social": create_index_social_analyst,
    "news": create_index_news_analyst,
    "policy": create_index_policy_analyst,
    "hot_money": create_index_hot_money_tracker,
    "bull": create_index_bull_researcher,
    "bear": create_index_bear_researcher,
    "trader": create_index_trader,
    "portfolio_manager": create_index_portfolio_manager,
}
