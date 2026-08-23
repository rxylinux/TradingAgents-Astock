"""Thread-safe progress tracker shared between the background runner and Streamlit UI."""

from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional


PIPELINE_STAGES: list[dict[str, str]] = [
    {"id": "market", "name": "技术分析", "icon": "📊", "report_key": "market_report"},
    {"id": "social", "name": "情绪分析", "icon": "💬", "report_key": "sentiment_report"},
    {"id": "news", "name": "新闻舆情", "icon": "📰", "report_key": "news_report"},
    {"id": "fundamentals", "name": "基本面", "icon": "📋", "report_key": "fundamentals_report"},
    {"id": "policy", "name": "政策分析", "icon": "🏛️", "report_key": "policy_report"},
    {"id": "hot_money", "name": "游资追踪", "icon": "🔥", "report_key": "hot_money_report"},
    {"id": "lockup", "name": "解禁监控", "icon": "🔒", "report_key": "lockup_report"},
    {"id": "quality_gate", "name": "质量门控", "icon": "✅", "report_key": "data_quality_summary"},
    {"id": "debate", "name": "多空辩论", "icon": "⚔️", "report_key": "investment_plan"},
    {"id": "trader", "name": "交易决策", "icon": "💹", "report_key": "trader_investment_plan"},
    {"id": "risk", "name": "风控评估", "icon": "🛡️", "report_key": "risk_debate_state"},
    {"id": "pm", "name": "最终决策", "icon": "👔", "report_key": "final_trade_decision"},
]

STAGE_IDS = [s["id"] for s in PIPELINE_STAGES]

ANALYST_AGENTS: list[dict[str, str]] = [
    {"id": "market", "name": "技术分析师", "icon": "📊", "report_key": "market_report", "desc": "量价异动、均线趋势、MACD/K线形态"},
    {"id": "social", "name": "情绪分析师", "icon": "💬", "report_key": "sentiment_report", "desc": "股吧/雪球讨论热度、散户多空情绪倾向"},
    {"id": "news", "name": "新闻分析师", "icon": "📰", "report_key": "news_report", "desc": "个股公告、行业动态、全球快讯实时过滤"},
    {"id": "fundamentals", "name": "基本面分析师", "icon": "📋", "report_key": "fundamentals_report", "desc": "财报三表快照、PE/PB估值与盈利预测"},
    {"id": "policy", "name": "政策分析师", "icon": "🏛️", "report_key": "policy_report", "desc": "宏观政策、产业扶持、监管新规研判"},
    {"id": "hot_money", "name": "游资追踪师", "icon": "🔥", "report_key": "hot_money_report", "desc": "主力资金流向、龙虎榜席位与北向资金"},
    {"id": "lockup", "name": "解禁监控师", "icon": "🔒", "report_key": "lockup_report", "desc": "限售解禁日历、大股东减持预警与抛压分析"},
]

ANALYST_AGENT_IDS = [a["id"] for a in ANALYST_AGENTS]
ANALYST_MAP = {a["id"]: a for a in ANALYST_AGENTS}

# 默认系统 Prompt 模板预览库，供未执行或处于等待状态的 Agent 查看人设与规则预设
DEFAULT_AGENT_PROMPTS: dict[str, str] = {
    "market": (
        "你是一位专注于 A 股市场的技术分析师。你的任务是从以下技术指标中选择最多 8 个最相关的指标，"
        "为给定的 A 股标的提供技术面分析。选择时应注重指标间的互补性，避免冗余。\n\n"
        "⚠️ A 股市场特殊规则：\n"
        "- 涨跌停制度：主板 ±10%，科创板/创业板 ±20%，北交所 ±30%。ST/*ST 股主板已调为 ±10%，次新股前 5 日无涨跌幅限制。\n"
        "- T+1 交易制度：当日买入次日才能卖出，短线策略执行受限。\n"
        "- 北向资金：沪深港通流向是重要风向标。\n"
        "- 换手率与量价关系：量在价先，放量突破与缩量回调为核心信号。\n\n"
        "可选指标：close_50_sma, close_200_sma, close_10_ema, macd, macds, macdh, rsi, boll, boll_ub, boll_lb, atr, vwma\n"
        "工具依赖：get_stock_data, get_indicators"
    ),
    "social": (
        "你是一位专注于 A 股市场的市场情绪分析师。你的任务是通过分析公司相关新闻、市场讨论和公众情绪，"
        "判断市场对目标公司的整体态度和情绪走向。\n\n"
        "⚠️ A 股情绪分析框架：\n"
        "- 散户情绪权重高：A 股散户占比超过 60%，情绪波动更剧烈。\n"
        "- 舆论阵地：东方财富股吧、雪球、同花顺社区。\n"
        "- 分析纪律：先看资金（主力/超大单），再看新闻。必须检查资金面与消息面是否背离。\n\n"
        "工具依赖：get_news, get_fund_flow, get_hot_stocks, get_stock_data"
    ),
    "news": (
        "你是一位专注于 A 股市场的新闻与政策分析师。你的任务是分析近期新闻动态，评估其对目标公司和 A 股市场的影响。\n\n"
        "⚠️ A 股新闻分析框架：\n"
        "- 政策敏感度：政策市，重点关注国务院/证监会/央行/发改委政策发布。\n"
        "- 消息来源权重：财联社快讯 > 新华财经/证券时报 > 东方财富/同花顺。\n"
        "- 关注事件驱动与产业链行业轮动。\n\n"
        "工具依赖：get_news, get_global_news, get_insider_transactions"
    ),
    "fundamentals": (
        "你是一位专注于 A 股市场的基本面分析师。你的任务是全面分析目标公司的基本面信息，为投资决策提供扎实的数据支撑。\n\n"
        "⚠️ A 股基本面分析要点：\n"
        "- 财务准则：采用中国会计准则（CAS）。\n"
        "- 估值参照系：PE/PB 横向对标同行业 A 股公司。\n"
        "- 核心指标：营收增长率、归母净利润、扣非净利润、ROE、毛利率、经营性现金流匹配度。\n\n"
        "工具依赖：get_fundamentals, get_balance_sheet, get_cashflow, get_income_statement, get_profit_forecast, get_industry_comparison"
    ),
    "policy": (
        "你是一位专注于 A 股市场的政策分析师。你的核心任务是追踪和解读影响目标公司及所在行业的政策动态，评估影响方向和力度。\n\n"
        "⚠️ 政策分析框架：\n"
        "- 宏观政策层（货币/财政）、监管政策层（证监会/发改委）、产业政策层（扶持/限制/新质生产力）。\n"
        "- 评估政策力度级别与影响时间窗口（短期脉冲 vs 中期趋势 vs 长期结构性）。\n\n"
        "工具依赖：get_news, get_global_news"
    ),
    "hot_money": (
        "你是一位专注于 A 股市场的游资与资金流向追踪分析师。你的核心任务是通过分析成交量异动、股东变化和市场新闻，追踪主力资金和游资动向。\n\n"
        "⚠️ A 股游资分析框架：\n"
        "- 量价异动识别：突然放量、换手率飙升（>10%）、连板博弈特征。\n"
        "- 龙虎榜席位：知名游资席位买卖、机构参与情况、题材归因标签。\n"
        "- 判断博弈格局：主力吸筹 / 主力出货 / 游资接力 / 散户主导。\n\n"
        "工具依赖：get_dragon_tiger_board, get_northbound_flow, get_concept_blocks, get_fund_flow, get_hot_stocks, get_stock_data, get_news"
    ),
    "lockup": (
        "你是一位专注于 A 股市场的解禁与减持监控分析师。你的核心任务是追踪限售股解禁计划、大股东减持动态和股权结构变化，评估供给端抛压。\n\n"
        "⚠️ A 股解禁/减持分析框架：\n"
        "- 限售股类型（首发/定增/股权激励）与解禁规模评估。\n"
        "- 2024 年减持新规约束与控股股东禁止减持情形（破发、破净、分红不达标严禁集中竞价/大宗减持）。\n\n"
        "工具依赖：get_lockup_expiry, get_insider_transactions, get_fundamentals, get_news"
    ),
    "quality_gate": (
        "数据质量审核员：对 7 位分析师报告进行硬检查（字数、缺失项、汇总表）与 LLM 复审，评估数据完整性与可信度评级（A/B/C/D/F）。"
    ),
    "debate": (
        "多空辩论阶段：包含看多分析师 (Bull Analyst)、看空分析师 (Bear Analyst) 针对标的展开中国市场特色辩论，由研究总监 (Research Manager) 裁决并生成投资计划。"
    ),
    "trader": (
        "交易策略师 (Trader)：根据研究总监的投资计划与政策/游资/解禁专项报告，在 A 股 T+1、涨跌停、最小申报单位等约束下制定具体交易执行方案。"
    ),
    "risk": (
        "三方风控评估阶段：激进风控师 (Aggressive)、保守风控师 (Conservative)、中立风控师 (Neutral) 围绕交易计划展开多轮攻防博弈，评估极限回撤与潜在风险。"
    ),
    "pm": (
        "投资决策委员会 / 基金经理 (Portfolio Manager)：综合多空辩论、交易计划、风控辩论与历史反思，输出最终交易决策（Buy/Overweight/Hold/Underweight/Sell）与仓位管理建议。"
    ),
}


def get_default_prompt_template(agent_id: str) -> str:
    """Return the default system prompt template for an agent."""
    if agent_id in DEFAULT_AGENT_PROMPTS:
        return DEFAULT_AGENT_PROMPTS[agent_id]
    # Alias lookup
    if agent_id in ("bull_researcher", "bear_researcher", "research_manager"):
        return DEFAULT_AGENT_PROMPTS["debate"]
    if agent_id in ("aggressive_debator", "conservative_debator", "neutral_debator"):
        return DEFAULT_AGENT_PROMPTS["risk"]
    if agent_id in ("portfolio_manager",):
        return DEFAULT_AGENT_PROMPTS["pm"]
    return "暂无该 Agent 的默认 Prompt 模板"


@dataclass
class ProgressTracker:
    """Mutable state container updated by the runner thread, read by the UI."""

    ticker: str = ""
    trade_date: str = ""
    start_time: float = field(default_factory=time.time)
    stage_ids: Optional[list[str]] = None

    is_running: bool = False
    is_complete: bool = False
    is_paused: bool = False
    stop_requested: bool = False
    error: Optional[str] = None

    current_stage: str = ""
    completed_stages: list[str] = field(default_factory=list)
    stage_reports: dict[str, str] = field(default_factory=dict)

    # 7 个 Agent 独立状态与调试追踪
    agent_statuses: dict[str, str] = field(default_factory=dict)
    agent_details: dict[str, str] = field(default_factory=dict)
    agent_tool_calls: dict[str, list[str]] = field(default_factory=dict)
    agent_updated_at: dict[str, float] = field(default_factory=dict)

    # 增强调试遥测字段
    agent_prompts: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    agent_tool_details: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    agent_raw_responses: dict[str, list[str]] = field(default_factory=dict)
    agent_metrics: dict[str, dict[str, Any]] = field(default_factory=dict)

    final_state: dict[str, Any] = field(default_factory=dict)
    signal: str = ""

    llm_calls: int = 0
    tool_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _pause_gate: threading.Event = field(
        default_factory=threading.Event,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        self._pause_gate.set()
        now = time.time()
        for agent in ANALYST_AGENTS:
            aid = agent["id"]
            if aid not in self.agent_statuses:
                self.agent_statuses[aid] = "pending"
            if aid not in self.agent_details:
                self.agent_details[aid] = "等待启动..."
            if aid not in self.agent_tool_calls:
                self.agent_tool_calls[aid] = []
            if aid not in self.agent_updated_at:
                self.agent_updated_at[aid] = now
            if aid not in self.agent_prompts:
                self.agent_prompts[aid] = []
            if aid not in self.agent_tool_details:
                self.agent_tool_details[aid] = []
            if aid not in self.agent_raw_responses:
                self.agent_raw_responses[aid] = []
            if aid not in self.agent_metrics:
                self.agent_metrics[aid] = {
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "total_tokens": 0,
                    "llm_calls": 0,
                    "tool_calls": 0,
                    "total_duration": 0.0,
                }

    def stages(self) -> list[dict[str, str]]:
        """本次运行有效的阶段列表（按 stage_ids 裁剪 PIPELINE_STAGES）。"""
        if not self.stage_ids:
            return PIPELINE_STAGES
        wanted = set(self.stage_ids)
        return [s for s in PIPELINE_STAGES if s["id"] in wanted]

    def pause(self) -> bool:
        """Pause pipeline advancement after the current streamed step finishes."""
        with self._lock:
            if (
                not self.is_running
                or self.is_complete
                or self.error
                or self.is_paused
                or self.stop_requested
            ):
                return False
            self.is_paused = True
            self._pause_gate.clear()
            return True

    def resume(self) -> bool:
        """Allow the runner thread to continue to the next streamed step."""
        with self._lock:
            if not self.is_paused or self.stop_requested:
                return False
            self.is_paused = False
            self._pause_gate.set()
            return True

    def request_stop(self) -> bool:
        """Request cancellation and clear user-visible progress immediately."""
        with self._lock:
            if not self.is_running or self.is_complete or self.error or self.stop_requested:
                return False
            self.stop_requested = True
            self.is_paused = False
            self.current_stage = ""
            self.completed_stages.clear()
            self.stage_reports.clear()
            self.agent_prompts.clear()
            self.agent_tool_details.clear()
            self.agent_raw_responses.clear()
            self.agent_metrics.clear()
            now = time.time()
            for agent in ANALYST_AGENTS:
                aid = agent["id"]
                self.agent_statuses[aid] = "pending"
                self.agent_details[aid] = "已停止"
                self.agent_tool_calls[aid] = []
                self.agent_updated_at[aid] = now
                self.agent_prompts[aid] = []
                self.agent_tool_details[aid] = []
                self.agent_raw_responses[aid] = []
                self.agent_metrics[aid] = {
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "total_tokens": 0,
                    "llm_calls": 0,
                    "tool_calls": 0,
                    "total_duration": 0.0,
                }
            self.final_state = {}
            self.signal = ""
            self.llm_calls = 0
            self.tool_calls = 0
            self.tokens_in = 0
            self.tokens_out = 0
            self._pause_gate.set()
            return True

    def wait_if_paused(self) -> None:
        self._pause_gate.wait()

    def mark_stopped(self) -> None:
        with self._lock:
            self.is_running = False
            self.is_complete = False
            self.is_paused = False
            self.stop_requested = False
            self.error = None
            self.current_stage = ""
            self.completed_stages.clear()
            self.stage_reports.clear()
            self.agent_prompts.clear()
            self.agent_tool_details.clear()
            self.agent_raw_responses.clear()
            self.agent_metrics.clear()
            now = time.time()
            for agent in ANALYST_AGENTS:
                aid = agent["id"]
                self.agent_statuses[aid] = "pending"
                self.agent_details[aid] = "等待启动..."
                self.agent_tool_calls[aid] = []
                self.agent_updated_at[aid] = now
                self.agent_prompts[aid] = []
                self.agent_tool_details[aid] = []
                self.agent_raw_responses[aid] = []
                self.agent_metrics[aid] = {
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "total_tokens": 0,
                    "llm_calls": 0,
                    "tool_calls": 0,
                    "total_duration": 0.0,
                }
            self.final_state = {}
            self.signal = ""
            self.llm_calls = 0
            self.tool_calls = 0
            self.tokens_in = 0
            self.tokens_out = 0
            self._pause_gate.set()

    def mark_stage_active(self, stage_id: str) -> None:
        with self._lock:
            if self.stop_requested:
                return
            self.current_stage = stage_id
            if stage_id in self.agent_statuses and self.agent_statuses[stage_id] != "done":
                self.agent_statuses[stage_id] = "running"
                self.agent_details[stage_id] = "正在分析数据与生成报告..."
                self.agent_updated_at[stage_id] = time.time()

    def mark_stage_done(self, stage_id: str, report: str = "") -> None:
        with self._lock:
            if self.stop_requested:
                return
            if stage_id not in self.completed_stages:
                self.completed_stages.append(stage_id)
            if report:
                self.stage_reports[stage_id] = report
            if stage_id in self.agent_statuses:
                self.agent_statuses[stage_id] = "done"
                count_str = f" ({len(report)}字)" if report else ""
                self.agent_details[stage_id] = f"报告已生成{count_str}"
                self.agent_updated_at[stage_id] = time.time()
            self.current_stage = ""

    def set_agent_status(self, agent_id: str, status: str, detail: str = "") -> None:
        """Update the status and optional detail for an individual agent in a thread-safe way."""
        with self._lock:
            if self.stop_requested:
                return
            self.agent_statuses[agent_id] = status
            if detail:
                self.agent_details[agent_id] = detail
            self.agent_updated_at[agent_id] = time.time()

    def record_agent_tool(self, agent_id: str, tool_name: str) -> None:
        """Record a tool call for an individual agent in a thread-safe way."""
        with self._lock:
            if self.stop_requested:
                return
            calls = self.agent_tool_calls.setdefault(agent_id, [])
            if tool_name not in calls:
                calls.append(tool_name)
            if self.agent_statuses.get(agent_id) != "done":
                self.agent_statuses[agent_id] = "tool_calling"
                self.agent_details[agent_id] = f"正在调用 {tool_name}..."
            self.agent_updated_at[agent_id] = time.time()

    def record_agent_prompt(self, agent_id: str, messages: list[dict[str, Any]]) -> None:
        """Record or update prompt messages sent to LLM for a specific agent."""
        with self._lock:
            if self.stop_requested:
                return
            self.agent_prompts[agent_id] = list(messages)
            self.agent_updated_at[agent_id] = time.time()

    def record_agent_tool_detail(
        self,
        agent_id: str,
        tool_detail: Optional[dict[str, Any]] = None,
        tool_name: Optional[str] = None,
        args: Any = None,
        payload: Any = None,
        status: str = "success",
    ) -> None:
        """Record detailed tool call payload and execution metrics."""
        with self._lock:
            if self.stop_requested:
                return
            details = self.agent_tool_details.setdefault(agent_id, [])
            if isinstance(tool_detail, dict):
                entry = dict(tool_detail)
                name = entry.get("tool_name", "")
            else:
                name = tool_name or (str(tool_detail) if tool_detail else "unknown_tool")
                entry = {
                    "tool_name": name,
                    "args": args,
                    "payload": payload,
                    "status": status,
                    "time": time.strftime("%H:%M:%S"),
                }
            details.append(entry)
            if name:
                calls = self.agent_tool_calls.setdefault(agent_id, [])
                if name not in calls:
                    calls.append(name)
            if self.agent_statuses.get(agent_id) != "done":
                self.agent_statuses[agent_id] = "tool_calling"
                status_text = (
                    f"正在调用 {name}..."
                    if entry.get("status") != "error"
                    else f"工具 {name} 调用异常"
                )
                self.agent_details[agent_id] = status_text
            self.agent_updated_at[agent_id] = time.time()

    def get_agent_tool_details(self, agent_id: str) -> list[dict[str, Any]]:
        """Get the list of structured tool executions for an agent."""
        with self._lock:
            return list(self.agent_tool_details.get(agent_id, []))


    def record_agent_response(self, agent_id: str, raw_response: str, thinking: str = "") -> None:
        """Record LLM raw response and optional thinking process."""
        with self._lock:
            if self.stop_requested:
                return
            responses = self.agent_raw_responses.setdefault(agent_id, [])
            if thinking and "<think>" not in raw_response:
                formatted = f"<think>\n{thinking}\n</think>\n\n{raw_response}"
            else:
                formatted = raw_response
            responses.append(formatted)
            self.agent_updated_at[agent_id] = time.time()

    def record_agent_metrics(
        self,
        agent_id: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
        duration: float = 0.0,
        is_llm: bool = False,
        is_tool: bool = False,
    ) -> None:
        """Accumulate token consumption and duration metrics for an agent."""
        with self._lock:
            if self.stop_requested:
                return
            m = self.agent_metrics.setdefault(
                agent_id,
                {
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "total_tokens": 0,
                    "llm_calls": 0,
                    "tool_calls": 0,
                    "total_duration": 0.0,
                },
            )
            m["tokens_in"] += tokens_in
            m["tokens_out"] += tokens_out
            m["total_tokens"] += tokens_in + tokens_out
            if is_llm:
                m["llm_calls"] += 1
            if is_tool:
                m["tool_calls"] += 1
            m["total_duration"] = round(m["total_duration"] + max(0.0, duration), 3)
            self.agent_updated_at[agent_id] = time.time()

    def get_agent_status(self, agent_id: str) -> str:
        """Get the current status of an agent."""
        with self._lock:
            return self.agent_statuses.get(agent_id, "pending")

    def get_agent_detail(self, agent_id: str) -> str:
        """Get the current detail description of an agent."""
        with self._lock:
            return self.agent_details.get(agent_id, "")

    def get_agent_tool_calls(self, agent_id: str) -> list[str]:
        """Get the list of tools called by an agent."""
        with self._lock:
            return list(self.agent_tool_calls.get(agent_id, []))

    def get_agent_debug_info(self, agent_id: str) -> dict[str, Any]:
        """Return an atomic snapshot of debug telemetry for a specific agent."""
        with self._lock:
            agent_meta = ANALYST_MAP.get(agent_id, {})
            name = agent_meta.get("name", agent_id)
            icon = agent_meta.get("icon", "🤖")
            desc = agent_meta.get("desc", "")
            status = self.agent_statuses.get(agent_id, "pending")
            detail = self.agent_details.get(agent_id, "等待启动...")
            tool_calls = list(self.agent_tool_calls.get(agent_id, []))
            prompts = copy.deepcopy(self.agent_prompts.get(agent_id, []))
            tool_details = copy.deepcopy(self.agent_tool_details.get(agent_id, []))
            raw_responses = copy.deepcopy(self.agent_raw_responses.get(agent_id, []))
            metrics = copy.deepcopy(
                self.agent_metrics.get(
                    agent_id,
                    {
                        "tokens_in": 0,
                        "tokens_out": 0,
                        "total_tokens": 0,
                        "llm_calls": 0,
                        "tool_calls": 0,
                        "total_duration": 0.0,
                    },
                )
            )
            report = self.stage_reports.get(agent_id, "")
            default_template = get_default_prompt_template(agent_id)

            return {
                "agent_id": agent_id,
                "name": name,
                "icon": icon,
                "desc": desc,
                "status": status,
                "detail": detail,
                "tool_calls": tool_calls,
                "prompts": prompts,
                "default_prompt_template": default_template,
                "tool_details": tool_details,
                "raw_responses": raw_responses,
                "metrics": metrics,
                "report": report,
                "updated_at": self.agent_updated_at.get(agent_id, self.start_time),
            }

    def get_all_agent_debug_info(self) -> dict[str, dict[str, Any]]:
        """Return an atomic snapshot of debug telemetry for all agents."""
        with self._lock:
            all_ids = list(ANALYST_AGENT_IDS)
            for k in list(self.agent_statuses.keys()) + list(self.agent_prompts.keys()) + list(STAGE_IDS):
                if k not in all_ids:
                    all_ids.append(k)

            result = {}
            for aid in all_ids:
                agent_meta = ANALYST_MAP.get(aid, {})
                name = agent_meta.get("name", aid)
                icon = agent_meta.get("icon", "🤖")
                desc = agent_meta.get("desc", "")
                status = self.agent_statuses.get(aid, "pending")
                detail = self.agent_details.get(aid, "等待启动...")
                tool_calls = list(self.agent_tool_calls.get(aid, []))
                prompts = copy.deepcopy(self.agent_prompts.get(aid, []))
                tool_details = copy.deepcopy(self.agent_tool_details.get(aid, []))
                raw_responses = copy.deepcopy(self.agent_raw_responses.get(aid, []))
                metrics = copy.deepcopy(
                    self.agent_metrics.get(
                        aid,
                        {
                            "tokens_in": 0,
                            "tokens_out": 0,
                            "total_tokens": 0,
                            "llm_calls": 0,
                            "tool_calls": 0,
                            "total_duration": 0.0,
                        },
                    )
                )
                report = self.stage_reports.get(aid, "")
                default_template = get_default_prompt_template(aid)

                result[aid] = {
                    "agent_id": aid,
                    "name": name,
                    "icon": icon,
                    "desc": desc,
                    "status": status,
                    "detail": detail,
                    "tool_calls": tool_calls,
                    "prompts": prompts,
                    "default_prompt_template": default_template,
                    "tool_details": tool_details,
                    "raw_responses": raw_responses,
                    "metrics": metrics,
                    "report": report,
                    "updated_at": self.agent_updated_at.get(aid, self.start_time),
                }
            return result

    def get_all_agent_states(self) -> dict[str, dict[str, Any]]:
        """Get a complete snapshot of all 7 agents' states with debug telemetry."""
        from web.agent_debug import parse_agent_response

        with self._lock:
            snapshot = {}
            for agent in ANALYST_AGENTS:
                aid = agent["id"]
                report = self.stage_reports.get(aid, "")
                raw_responses = self.agent_raw_responses.get(aid, [])
                raw_out = raw_responses[-1] if raw_responses else report
                raw_txt, think_txt, clean_txt = parse_agent_response(raw_out)
                tool_details = copy.deepcopy(self.agent_tool_details.get(aid, []))
                prompts = copy.deepcopy(self.agent_prompts.get(aid, []))
                metrics = copy.deepcopy(self.agent_metrics.get(aid, {}))

                snapshot[aid] = {
                    "id": aid,
                    "name": agent["name"],
                    "icon": agent["icon"],
                    "desc": agent.get("desc", ""),
                    "report_key": agent["report_key"],
                    "status": self.agent_statuses.get(aid, "pending"),
                    "detail": self.agent_details.get(aid, ""),
                    "tool_calls": list(self.agent_tool_calls.get(aid, [])),
                    "tool_details": tool_details,
                    "prompts": prompts,
                    "raw_output": raw_txt,
                    "raw_responses": raw_responses,
                    "think_content": think_txt,
                    "clean_output": clean_txt,
                    "metrics": metrics,
                    "updated_at": self.agent_updated_at.get(aid, self.start_time),
                    "report": report,
                }
            return snapshot

    def mark_complete(self, final_state: dict, signal: str) -> None:
        with self._lock:
            self.final_state = final_state
            self.signal = signal
            self.is_running = False
            self.is_complete = True
            self.is_paused = False
            self.stop_requested = False
            self._pause_gate.set()

    def mark_error(self, err: str) -> None:
        with self._lock:
            self.error = err
            self.is_running = False
            self.is_paused = False
            self.stop_requested = False
            self._pause_gate.set()
            if self.current_stage and self.current_stage in self.agent_statuses:
                self.agent_statuses[self.current_stage] = "error"
                self.agent_details[self.current_stage] = f"执行出错: {err[:50]}"

    def update_stats(self, llm: int, tool: int, tok_in: int, tok_out: int) -> None:
        with self._lock:
            if self.stop_requested:
                return
            self.llm_calls = llm
            self.tool_calls = tool
            self.tokens_in = tok_in
            self.tokens_out = tok_out

    @property
    def elapsed(self) -> float:
        return time.time() - self.start_time

    def stage_status(self, stage_id: str) -> str:
        with self._lock:
            if stage_id in self.completed_stages:
                return "done"
            if stage_id == self.current_stage:
                return "active"
            return "pending"
