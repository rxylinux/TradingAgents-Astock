# TradingAgents/graph/trading_graph.py

import logging
import os
import re
import threading
from contextlib import contextmanager
from pathlib import Path
import copy
import json
from datetime import datetime, timedelta
from typing import Dict, Any, Tuple, List, Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

from langgraph.prebuilt import ToolNode

from tradingagents.llm_clients import create_llm_client

from tradingagents.agents import *
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.dataflows.utils import safe_ticker_component
from tradingagents.agents.utils.agent_states import (
    AgentState,
    InvestDebateState,
    RiskDebateState,
)
from tradingagents.dataflows.config import reset_run_config, set_run_config

# Import the new abstract tool methods from agent_utils
from tradingagents.agents.utils.agent_utils import (
    get_stock_data,
    get_indicators,
    get_fundamentals,
    get_balance_sheet,
    get_cashflow,
    get_income_statement,
    get_news,
    get_insider_transactions,
    get_global_news,
    get_profit_forecast,
    get_hot_stocks,
    get_northbound_flow,
    get_concept_blocks,
    get_fund_flow,
    get_dragon_tiger_board,
    get_lockup_expiry,
    get_industry_comparison,
)

from .checkpointer import checkpoint_step, clear_checkpoint, get_checkpointer, thread_id
from tradingagents.agents.utils.file_lock import file_lock
from tradingagents.run_records import (
    is_run_metadata,
    new_run_metadata,
    validate_run_id,
)
from .conditional_logic import BRANCH_MESSAGE_KEYS, ConditionalLogic
from .setup import ROLE_KEYS, GraphSetup
from .propagation import Propagator
from .reflection import Reflector
from .signal_processing import SignalProcessor

# 分析师分支消息通道名（R1 隔离）：debug 流式输出与状态读取需要遍历它们。
_ANALYST_MESSAGE_KEYS = tuple(BRANCH_MESSAGE_KEYS.values())

# 七个分析师角色——它们受 `selected_analysts` 控制，没选中就不会进图。
_ANALYST_ROLES = frozenset({
    "market", "social", "news", "fundamentals", "policy", "hot_money", "lockup",
})

# 各家 provider 私有的参数：换了 provider 就不能带过去（别家可能直接拒收）。
_PROVIDER_SPECIFIC_KWARGS = frozenset({
    "reasoning_effort",   # openai
    "thinking_level",     # google
    "effort",             # anthropic
})


def _extract_a_stock_code(ticker: str) -> Optional[str]:
    """Extract pure 6-digit code if ticker is A-share, else None."""
    s = str(ticker).strip().upper()
    for suffix in (".SH", ".SZ", ".BJ", ".SS"):
        if s.endswith(suffix):
            s = s[:-len(suffix)]
            break
    for prefix in ("SH", "SZ", "BJ"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    if len(s) == 6 and s.isdigit():
        return s
    return None


def _normalize_yfinance_ticker(ticker: str) -> str:
    """Return the Yahoo Finance symbol for a ticker used by the graph.

    A-stock decisions are stored with their six-digit code (for example
    ``600519``), while Yahoo Finance requires an exchange suffix for mainland
    China listings (``600519.SS``).  Without the suffix ``Ticker.history``
    usually returns an empty frame, so deferred memory outcomes remain pending
    indefinitely.  Common A-share prefixes/suffixes are normalized as well;
    non-A-share symbols remain unchanged so the graph remains usable for other
    markets too.
    """
    symbol = str(ticker).strip().upper()

    # The A-stock layer accepts SH/SZ/BJ prefixes and uses .SH for Shanghai,
    # whereas Yahoo uses .SS.  Normalize those forms before handling bare
    # six-digit codes. Yahoo has no Beijing exchange suffix, so keep BJ codes
    # unqualified instead of inventing a symbol that cannot return data.
    if (
        len(symbol) == 9
        and symbol[:6].isdigit()
        and symbol[6:] in (".SH", ".SZ", ".BJ", ".SS")
    ):
        code, exchange = symbol[:6], symbol[7:]
        if exchange in ("SH", "SS"):
            return f"{code}.SS"
        if exchange == "SZ":
            return f"{code}.SZ"
        return code
    if (
        len(symbol) == 8
        and symbol[:2] in ("SH", "SZ", "BJ")
        and symbol[2:].isdigit()
    ):
        code, exchange = symbol[2:], symbol[:2]
        if exchange == "SH":
            return f"{code}.SS"
        if exchange == "SZ":
            return f"{code}.SZ"
        return code

    if len(symbol) != 6 or not symbol.isdigit():
        return symbol

    # The 920xxx range is Beijing-listed; Yahoo has no supported suffix for it.
    if symbol.startswith("92"):
        return symbol
    # Shanghai-listed A shares, B shares and ETFs use the .SS suffix on Yahoo.
    if symbol.startswith(("5", "6", "9")):
        return f"{symbol}.SS"
    # Other Beijing-listed six-digit ranges are not covered by Yahoo either.
    if symbol.startswith(("4", "8")):
        return symbol
    # Shenzhen-listed stocks (000/001/002/003/300/301, etc.).
    return f"{symbol}.SZ"


def _is_unsupported_by_yfinance(symbol: str) -> bool:
    """True for codes Yahoo Finance has no coverage for at all.

    Beijing Stock Exchange listings (920xxx current, 43x/83x/87x legacy) are
    absent from Yahoo under every suffix — verified 2026-07-31: ``920002``,
    ``920002.BJ``, ``920002.SS`` and ``920002.SZ`` all return an empty frame.
    Retrying them every run only burns a request and leaves the memory entry
    pending forever with no stated reason, so short-circuit and say why once.
    """
    return (
        len(symbol) == 6
        and symbol.isdigit()
        and (symbol.startswith("92") or symbol[:2] in ("43", "83", "87") or symbol.startswith(("4", "8")))
    )


class TradingAgentsGraph:
    """Main class that orchestrates the trading agents framework."""

    def __init__(
        self,
        selected_analysts=["market", "social", "news", "fundamentals", "policy", "hot_money", "lockup"],
        debug=False,
        config: Dict[str, Any] = None,
        callbacks: Optional[List] = None,
    ):
        """Initialize the trading agents graph and components.

        Args:
            selected_analysts: List of analyst types to include
            debug: Whether to run in debug mode
            config: Configuration dictionary. If None, uses default config
            callbacks: Optional list of callback handlers (e.g., for tracking LLM/tool stats)
        """
        self.debug = debug
        # A05: 深拷贝调用方配置——嵌套 dict（如 tool_vendors）共享会让
        # 调用方构造后的修改串进本图运行（并发任务互换数据源）。
        self.config = copy.deepcopy(config or DEFAULT_CONFIG)
        self.callbacks = callbacks or []

        # A10: 记住本次选中的分析师（顺序去重），运行入口写入 state 供
        # 质量门控只检查启用集合。
        self.selected_analysts = list(dict.fromkeys(selected_analysts))

        # A05（最终收口）: 构造对调用方上下文与进程默认都**零永久修改**
        # ——并发构造另一个图既不能改掉调用方正在进行的运行快照，也不能
        # 改写无快照上下文回退到的进程默认。运行期由 run_context 显式绑
        # 定本图快照；构造链路本身不读取 get_config（如有新增读取需求，
        # 须临时绑定快照并在 finally 恢复）。
        # Create necessary directories
        os.makedirs(self.config["data_cache_dir"], exist_ok=True)
        os.makedirs(self.config["results_dir"], exist_ok=True)

        # Initialize LLMs with provider-specific thinking configuration
        llm_kwargs = self._get_provider_kwargs()

        # Add callbacks to kwargs if provided (passed to LLM constructor)
        if self.callbacks:
            llm_kwargs["callbacks"] = self.callbacks

        # Optional: route nodes through a personal Claude Pro/Max subscription
        # via the Claude Agent SDK. `deep_think_provider_override` covers the
        # deep nodes (Research / Portfolio Manager); `quick_think_provider_override`
        # covers the quick nodes (7 tool-using analysts + Bull/Bear / trader /
        # risk debaters). Both on ⇒ every node runs on the subscription. Off by
        # default — behaviour is unchanged when both are None.
        deep_on = self.config.get("deep_think_provider_override") == "claude_agent_sdk"
        quick_on = self.config.get("quick_think_provider_override") == "claude_agent_sdk"

        # F-004 guardrail: ANTHROPIC_API_KEY outranks the subscription OAuth token
        # and would silently bill the pay-per-token API instead of the subscription.
        # Refuse to start rather than surprise-bill the user.
        if (deep_on or quick_on) and os.getenv("ANTHROPIC_API_KEY"):
            # 该 key 优先级高于订阅凭据，泄进 Agent SDK 子进程就会悄悄走按 token
            # 计费的 API。客户端已在子进程环境里把它显式置空，所以这里**不再一律
            # 中止**——否则把 anthropic 用作降级 provider 就成了死结：留着 key 启动
            # 被拦，删掉 key 又会在撞额度真要降级时认证失败。
            logger.warning(
                "ANTHROPIC_API_KEY is set while the claude_agent_sdk override is on. "
                "It is stripped from the Agent SDK subprocess so subscription quota is "
                "used, and kept in this process only so an `anthropic` fallback can "
                "still authenticate. If you did not intend to keep a paid Anthropic "
                "fallback, unset it."
            )

        # 降级配置必须成对给：只改 provider 不改 model，会把主 provider 的模型名
        # 配到另一家去（如 AnthropicClient(model="deepseek-chat")），而这条路径
        # **恰好在撞额度、最需要它工作的时候才被走到**——那时再炸就太晚了。
        # 启动时就校验，而不是留到运行中。
        _fb_provider = self.config.get("agent_sdk_fallback_provider")
        _fb_model = self.config.get("agent_sdk_fallback_model")
        if (deep_on or quick_on) and bool(_fb_provider) != bool(_fb_model):
            missing = "agent_sdk_fallback_model" if _fb_provider else "agent_sdk_fallback_provider"
            given = "agent_sdk_fallback_provider" if _fb_provider else "agent_sdk_fallback_model"
            raise ValueError(
                f"{given} is set but {missing} is not — the two must be configured "
                f"together. Otherwise the fallback pairs one provider with another "
                f"provider's model name and fails exactly when the subscription hits "
                f"its quota. Set both, or leave both unset to fall back to "
                f"llm_provider + its own model."
            )

        def _make_client(override_on, sdk_model_key, fallback_model_key):
            """Build a subscription-backed client when overridden, else the normal
            llm_provider client. Fallback rejoins the paid provider on quota/failure."""
            if override_on:
                # backend_url 是为 llm_provider 配的端点。显式指定了**另一家**
                # provider 做降级时不能把它带过去（例如把 anthropic 降级请求发到
                # MiniMax 网关），否则同样是撞额度那一刻才炸。None ⇒ 该 provider
                # 用自己的默认端点。
                cross_provider = bool(_fb_provider) and _fb_provider != self.config["llm_provider"]
                fallback_spec = {
                    "provider": _fb_provider or self.config["llm_provider"],
                    "model": _fb_model or self.config[fallback_model_key],
                    "base_url": None if cross_provider else self.config.get("backend_url"),
                    # 带上 callbacks：降级意味着**开始计费**，此时统计/成本回调
                    # 反而看不到这些调用的话，恰好在花钱的时候统计是瞎的。
                    **({"callbacks": self.callbacks} if self.callbacks else {}),
                    # 用户显式配的输出上限也要带过去。否则撞额度降级之后，降级
                    # provider 用它自己的默认上限，报告照样被截断——而这正是
                    # 用户配 max_tokens 想避免的事（#91）。
                    **({"max_tokens": self.config["max_tokens"]}
                       if self.config.get("max_tokens") else {}),
                }
                return create_llm_client(
                    provider="claude_agent_sdk",
                    model=self.config[sdk_model_key],
                    base_url=self.config.get("backend_url"),
                    fallback_spec=fallback_spec,
                )
            return create_llm_client(
                provider=self.config["llm_provider"],
                model=self.config[fallback_model_key],
                base_url=self.config.get("backend_url"),
                **llm_kwargs,
            )

        deep_client = _make_client(deep_on, "agent_sdk_model", "deep_think_llm")
        quick_client = _make_client(quick_on, "agent_sdk_quick_model", "quick_think_llm")

        self.deep_thinking_llm = deep_client.get_llm()
        self.quick_thinking_llm = quick_client.get_llm()

        # 可选：给单个角色指定模型（#39）。不配 = 完全维持 quick/deep 两档的原行为。
        self.role_llms = self._build_role_llms(
            llm_kwargs, deep_on or quick_on, selected_analysts
        )

        self.memory_log = TradingMemoryLog(self.config)

        # Create tool nodes
        self.tool_nodes = self._create_tool_nodes()

        # Initialize components
        self.conditional_logic = ConditionalLogic(
            max_debate_rounds=self.config["max_debate_rounds"],
            max_risk_discuss_rounds=self.config["max_risk_discuss_rounds"],
            enable_early_stopping=self.config.get("enable_debate_early_stopping", False),
        )
        self.graph_setup = GraphSetup(
            self.quick_thinking_llm,
            self.deep_thinking_llm,
            self.tool_nodes,
            self.conditional_logic,
            resolve_llm=self.role_llms.get,
        )

        self.propagator = Propagator()
        self.reflector = Reflector(self.quick_thinking_llm)
        self.signal_processor = SignalProcessor(self.quick_thinking_llm)

        # State tracking
        self.curr_state = None
        self.ticker = None
        self.log_states_dict = {}  # date to full state dict

        # Set up the graph: keep the workflow for recompilation with a checkpointer.
        self.workflow = self.graph_setup.setup_graph(selected_analysts)
        self.graph = self.workflow.compile()
        self._checkpointer_ctx = None

    def _build_role_llms(
        self,
        llm_kwargs: Dict[str, Any],
        subscription_on: bool,
        selected_analysts=None,
    ) -> Dict[str, Any]:
        """按 `config["role_llms"]` 给单个角色单独建 LLM（#39）。

        默认是空表 —— 不配任何角色时行为与以前完全一致（全部走 quick/deep 两档）。
        配了才有意义：让多空辩手用不同厂商的模型，避免同源模型互相不反驳。

        相同 (provider, model, endpoint) 的角色复用同一个实例，不会因为写了 7 个
        角色就建 7 条连接。
        """
        specs = self.config.get("role_llms") or {}
        if not specs:
            return {}

        unknown = sorted(set(specs) - set(ROLE_KEYS))
        if unknown:
            raise ValueError(
                f"role_llms 里有无法识别的角色名：{unknown}。"
                f"合法角色：{', '.join(ROLE_KEYS)}。"
                f"（写错的角色名如果被静默忽略，你会以为配置生效了，实际没有。）"
            )

        # 没被选中的分析师不会进图，就别为它建模型：那会让一个**永远不执行**的节点
        # 因为缺 API key 或缺可选依赖，把一次本来完全正常的分析在启动时就打断。
        if selected_analysts is not None:
            active = set(selected_analysts)
            skipped = [r for r in specs if r in _ANALYST_ROLES and r not in active]
            if skipped:
                specs = {k: v for k, v in specs.items() if k not in skipped}
                logger.info(
                    "role_llms: 跳过未选中的分析师角色 %s（不建模型）",
                    ", ".join(sorted(skipped)),
                )
            if not specs:
                return {}

        if subscription_on:
            # 订阅覆盖是为了不产生 API 账单，这里显式点名哪些角色会绕开它去计费，
            # 不能让人以为"全部走订阅"却在某几个角色上悄悄花钱。
            logger.warning(
                "role_llms 为以下角色单独指定了模型，它们会绕开 claude_agent_sdk "
                "订阅覆盖、按 token 计费：%s", ", ".join(sorted(specs)),
            )

        main_provider = self.config["llm_provider"]
        cache: Dict[tuple, Any] = {}
        resolved: Dict[str, Any] = {}
        for role, spec in specs.items():
            if not isinstance(spec, dict) or not spec.get("model"):
                raise ValueError(
                    f"role_llms['{role}'] 必须是带 model 的字典，"
                    f'例如 {{"provider": "deepseek", "model": "deepseek-chat"}}。'
                )
            provider = spec.get("provider") or main_provider
            # backend_url 是给主 provider 配的端点。换了厂商还把它带过去，请求就会
            # 发到另一家的网关（和 agent_sdk 降级那里同一个坑）。None = 用该
            # provider 自己的默认端点。
            if "backend_url" in spec:
                base_url = spec["backend_url"]
            elif provider.lower() == str(main_provider).lower():
                base_url = self.config.get("backend_url")
            else:
                base_url = None

            # 主 provider 的**专属**参数不能带给另一家：openai 的
            # `reasoning_effort`、google 的 `thinking_level`、anthropic 的 `effort`
            # 都是各家私有的，塞进 qwen / glm / 自建网关的请求体里可能直接被拒。
            # 通用参数（max_tokens / callbacks 等）保留。
            role_kwargs = (
                llm_kwargs if provider.lower() == str(main_provider).lower()
                else {k: v for k, v in llm_kwargs.items()
                      if k not in _PROVIDER_SPECIFIC_KWARGS}
            )

            key = (provider.lower(), spec["model"], base_url)
            if key not in cache:
                cache[key] = create_llm_client(
                    provider=provider,
                    model=spec["model"],
                    base_url=base_url,
                    **role_kwargs,
                ).get_llm()
            resolved[role] = cache[key]

        logger.info(
            "role_llms: %d 个角色单独配置，实际建了 %d 个模型实例",
            len(resolved), len(cache),
        )
        return resolved

    def _get_provider_kwargs(self) -> Dict[str, Any]:
        """Get provider-specific kwargs for LLM client creation."""
        kwargs = {}
        provider = self.config.get("llm_provider", "").lower()

        # 与 provider 无关：单次回复的输出上限。撞上它就是报告写一半被截断（#91）。
        max_tokens = self.config.get("max_tokens")
        if max_tokens:
            kwargs["max_tokens"] = max_tokens

        if provider == "google":
            thinking_level = self.config.get("google_thinking_level")
            if thinking_level:
                kwargs["thinking_level"] = thinking_level

        elif provider == "openai":
            reasoning_effort = self.config.get("openai_reasoning_effort")
            if reasoning_effort:
                kwargs["reasoning_effort"] = reasoning_effort

        elif provider == "anthropic":
            effort = self.config.get("anthropic_effort")
            if effort:
                kwargs["effort"] = effort

        return kwargs

    def _create_tool_nodes(self) -> Dict[str, ToolNode]:
        """Create tool nodes for different data sources using abstract methods."""
        return {
            "market": ToolNode(
                [
                    # Core stock data tools
                    get_stock_data,
                    # Technical indicators
                    get_indicators,
                ]
            ),
            "social": ToolNode(
                [
                    # 情绪分析不只读新闻：资金流是最硬的情绪证据，量价给强度，
                    # 强势股榜给热度归因，新闻负责解释成因（#61）。
                    get_news,
                    get_fund_flow,
                    get_hot_stocks,
                    get_stock_data,
                ]
            ),
            "news": ToolNode(
                [
                    # News and insider information
                    get_news,
                    get_global_news,
                    get_insider_transactions,
                ]
            ),
            "fundamentals": ToolNode(
                [
                    get_fundamentals,
                    get_balance_sheet,
                    get_cashflow,
                    get_income_statement,
                    get_profit_forecast,
                    get_industry_comparison,
                ]
            ),
            "policy": ToolNode(
                [
                    get_news,
                    get_global_news,
                ]
            ),
            "hot_money": ToolNode(
                [
                    get_stock_data,
                    get_news,
                    get_insider_transactions,
                    get_hot_stocks,
                    get_northbound_flow,
                    get_concept_blocks,
                    get_fund_flow,
                    get_dragon_tiger_board,
                    get_industry_comparison,
                ]
            ),
            "lockup": ToolNode(
                [
                    get_insider_transactions,
                    get_news,
                    get_fundamentals,
                    get_lockup_expiry,
                ]
            ),
        }

    def _fetch_returns(
        self,
        ticker: str,
        trade_date: str,
        holding_days: int = 5,
        benchmark_ticker: str = "000300.SH",
    ) -> Tuple[Optional[float], Optional[float], Optional[int], Optional[str]]:
        """Fetch raw and alpha return for ticker over holding_days from trade_date.

        For A-share stocks (6-digit numeric or with SH/SZ/BJ/SS prefix/suffix,
        including Beijing Stock Exchange 920xxx / 83xxxx / 43xxxx) and market indices,
        prioritizes the native A-share data pipeline (mootdx / Sina history) to calculate
        stock/index and CSI 300 benchmark returns. Yahoo Finance is retained as a fallback
        for non-A-share symbols or if native data is unavailable.

        Evaluation window (A07/A08):

        - The window is measured in the **target security's own trading days**:
          the entry is its first trading day on/after trade_date, the exit is
          its ``holding_days``-th trading day after that. Suspension days of
          the target simply extend the calendar span.
        - The benchmark MUST have rows on exactly those two dates; alpha is
          computed start-date-to-end-date on both legs. A missing benchmark
          endpoint (or a shortened window: too few target trading days, long
          holidays, insufficient listing history) yields all-None and the
          memory entry stays **pending** — the window is never silently
          shortened and finalized via ``min()``.

        Returns ``(raw_return, alpha_return, holding_days, window_end_date)``
        where ``window_end_date`` is the exit trading date actually used (from
        market data — not calendar arithmetic); it is what makes a resolved
        memory entry provably knowable at a given analysis point-in-time (R4).
        """
        try:
            start = datetime.strptime(trade_date, "%Y-%m-%d")
            # A08: 取数范围覆盖到「今天」——窗口是否足够由拿到的实际交易日
            # 行数决定，而不是任何固定自然日上限（Codex 边审：复牌远晚于
            # 任何常数的长停牌，只要已复牌且交易日满就必须能结算；仍不足
            # 才保持 pending）。native 的 end 含当日；Yahoo 的 end 是排他
            # 语义，含当日需 +1 天。
            today = datetime.now().strftime("%Y-%m-%d")
            end_str = today
            yahoo_end_str = (
                datetime.strptime(today, "%Y-%m-%d") + timedelta(days=1)
            ).strftime("%Y-%m-%d")

            # 1. Check if target is an index or A-share stock, and try native data pipeline first
            from tradingagents.dataflows.index_registry import is_index_symbol

            is_index = is_index_symbol(ticker)
            code = None if is_index else _extract_a_stock_code(ticker)

            if is_index or code is not None:
                try:
                    from tradingagents.dataflows.a_stock import get_astock_history_df
                    from tradingagents.dataflows.index_data import get_index_history_df

                    if is_index:
                        stock_df = get_index_history_df(ticker, start_date=trade_date, end_date=end_str)
                    else:
                        stock_df = get_astock_history_df(code, start_date=trade_date, end_date=end_str)

                    bench_df = get_index_history_df(benchmark_ticker, start_date=trade_date, end_date=end_str)

                    if stock_df is not None and not stock_df.empty and bench_df is not None and not bench_df.empty:
                        stock_df = stock_df[stock_df["Date"] >= pd.to_datetime(trade_date)].sort_values("Date").reset_index(drop=True)
                        bench_df = bench_df[bench_df["Date"] >= pd.to_datetime(trade_date)].sort_values("Date").reset_index(drop=True)

                        # A08: native 已有该标的数据但窗口未满（新股/长假/
                        # 停牌）：交易日历对所有数据源一致，换源补不出更多
                        # 交易日——保持 pending，也不落到其他源重复取数。
                        if len(stock_df) - 1 < holding_days:
                            return None, None, None, None

                        start_date = pd.to_datetime(stock_df["Date"].iloc[0])
                        end_date = pd.to_datetime(stock_df["Date"].iloc[holding_days])
                        bench_close = (
                            bench_df.assign(Date=pd.to_datetime(bench_df["Date"]))
                            .drop_duplicates(subset="Date")
                            .set_index("Date")["Close"]
                        )
                        # A07: 基准必须有完全相同的起止日期行——各自按
                        # 行号取值会在停牌/缺行时比较不同日期。指数与个股
                        # 共用同一交易日历，native 缺该日期换源也不会有。
                        if start_date not in bench_close.index or end_date not in bench_close.index:
                            return None, None, None, None

                        s0 = float(stock_df["Close"].iloc[0])
                        s1 = float(stock_df["Close"].iloc[holding_days])
                        b0 = float(bench_close[start_date])
                        b1 = float(bench_close[end_date])
                        raw = (s1 - s0) / s0
                        bench_ret = (b1 - b0) / b0
                        alpha = raw - bench_ret
                        return (
                            raw,
                            alpha,
                            holding_days,
                            end_date.strftime("%Y-%m-%d"),
                        )
                except Exception as e:
                    logger.debug("Native return fetch failed for %s: %s", ticker, e)

            # 2. Fallback to Yahoo Finance (for non-A shares or when native pipeline is unavailable)
            yf_symbol = _normalize_yfinance_ticker(ticker)
            if _is_unsupported_by_yfinance(yf_symbol):
                # Beijing Stock Exchange has no Yahoo Finance coverage under any suffix
                return None, None, None, None

            yf_bench = _normalize_yfinance_ticker(benchmark_ticker)
            if _is_unsupported_by_yfinance(yf_bench):
                yf_bench = "000300.SS"

            stock = yf.Ticker(yf_symbol).history(start=trade_date, end=yahoo_end_str)
            benchmark = yf.Ticker(yf_bench).history(start=trade_date, end=yahoo_end_str)

            stock = stock[~stock.index.duplicated(keep="last")]
            benchmark = benchmark[~benchmark.index.duplicated(keep="last")]

            if len(stock) - 1 < holding_days:
                return None, None, None, None

            # yfinance 的索引带交易所本地时区：对齐按**本地日历日**比较，
            # 先剥离时区再取日期——跨时区的 normalize 若不剥 tz，同一交易
            # 日在两条腿上是不相等的绝对时刻（Codex 边审）。
            stock_days = pd.DatetimeIndex(
                pd.to_datetime(stock.index).tz_localize(None)
            ).normalize()
            bench_days = pd.DatetimeIndex(
                pd.to_datetime(benchmark.index).tz_localize(None)
            ).normalize()
            start_date = stock_days[0]
            end_date = stock_days[holding_days]
            bench_close = pd.Series(benchmark["Close"].values, index=bench_days)
            if start_date not in bench_close.index or end_date not in bench_close.index:
                # 基准端点日期缺失：无法对齐，保持 pending
                return None, None, None, None

            s0 = float(stock["Close"].iloc[0])
            s1 = float(stock["Close"].iloc[holding_days])
            b0 = float(bench_close[start_date])
            b1 = float(bench_close[end_date])
            raw = (s1 - s0) / s0
            bench_ret = (b1 - b0) / b0
            alpha = raw - bench_ret
            return raw, alpha, holding_days, end_date.strftime("%Y-%m-%d")
        except Exception as e:
            logger.warning(
                "Could not resolve outcome for %s on %s (will retry next run): %s",
                ticker, trade_date, e,
            )
            return None, None, None, None

    def _resolve_pending_entries(self, ticker: str) -> None:
        """Resolve pending log entries for ticker at the start of a new run.

        Fetches returns for each same-ticker pending entry, generates reflections,
        then writes all updates in a single atomic batch write to avoid redundant I/O.
        Skips entries whose price data is not yet available (too recent or delisted).

        Trade-off: only same-ticker entries are resolved per run.  Entries for
        other tickers accumulate until that ticker is run again.
        """
        pending = [e for e in self.memory_log.get_pending_entries() if e["ticker"] == ticker]
        if not pending:
            return

        updates = []
        for entry in pending:
            raw, alpha, days, window_end = self._fetch_returns(ticker, entry["date"])
            if raw is None:
                continue  # price not available yet — try again next run
            reflection = self.reflector.reflect_on_final_decision(
                final_decision=entry.get("decision", ""),
                raw_return=raw,
                alpha_return=alpha,
            )
            updates.append({
                "ticker": ticker,
                "trade_date": entry["date"],
                "raw_return": raw,
                "alpha_return": alpha,
                "holding_days": days,
                "reflection": reflection,
                # 实际收益窗口结束日：历史分析据此判断该收益在何时可知（R4）
                "outcome_end": window_end,
            })

        if updates:
            self.memory_log.batch_update_with_outcomes(updates)

    def bind_run_context(self):
        """绑定本图配置快照到当前上下文，返回释放句柄（A05）。

        分步调用方（CLI/Web：prepare → invoke → finalize → close 不在同
        一函数内）用本方法 + ``release_run_context`` 覆盖整个序列；
        ``run_context`` 上下文管理器是其 with 形式。
        """
        return set_run_config(self.config)

    def release_run_context(self, token) -> None:
        """释放 bind_run_context 的快照（异常路径也必须调用）。"""
        reset_run_config(token)

    @contextmanager
    def run_context(self):
        """绑定本图配置的运行快照（A05），全程覆盖 prepare→invoke→finalize。

        构造线程的 ``set_config`` 读不到运行线程（ContextVar 按上下文隔
        离），因此必须在**运行入口线程**重新绑定本图自己的配置快照；
        finally 恢复，异常路径同样释放。propagate 内部已自行包裹；
        prepare/finalize/close 各自也包裹（幂等，支持单独调用）。
        """
        token = self.bind_run_context()
        try:
            yield
        finally:
            self.release_run_context(token)

    def propagate(self, company_name, trade_date):
        """Run the trading agents graph for a company on a specific date.

        When ``checkpoint_enabled`` is set in config, the graph is recompiled
        with a per-ticker SqliteSaver so a crashed run can resume from the last
        successful node on a subsequent invocation with the same ticker+date.
        """
        with self.run_context():
            return self._run_graph(company_name, trade_date)

    def prepare_graph_run(
        self,
        company_name,
        trade_date,
        callbacks: Optional[List] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any], Optional[int]]:
        """Prepare graph input/args for a fresh or resumed run.

        Returns ``(initial_state, args, checkpoint_step)``. When a checkpoint
        already exists, ``initial_state`` is ``None`` so LangGraph resumes the
        existing thread instead of replaying completed nodes.
        """
        with self.run_context():
            return self._prepare_graph_run(company_name, trade_date, callbacks)

    def _prepare_graph_run(
        self,
        company_name,
        trade_date,
        callbacks: Optional[List] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any], Optional[int]]:
        self.ticker = company_name

        # Resolve any pending memory-log entries for this ticker before the pipeline runs.
        self._resolve_pending_entries(company_name)

        checkpoint_enabled = self.config.get("checkpoint_enabled")
        resume_step = None

        # Recompile with a checkpointer if the user opted in.
        if checkpoint_enabled:
            self._checkpointer_ctx = get_checkpointer(
                self.config["data_cache_dir"], company_name
            )
            saver = self._checkpointer_ctx.__enter__()
            self.graph = self.workflow.compile(checkpointer=saver)

            resume_step = checkpoint_step(
                self.config["data_cache_dir"], company_name, str(trade_date)
            )
            if resume_step is not None:
                logger.info(
                    "Resuming from step %d for %s on %s",
                    resume_step,
                    company_name,
                    trade_date,
                )
            else:
                logger.info("Starting fresh for %s on %s", company_name, trade_date)

        args = self.propagator.get_graph_args(callbacks=callbacks)

        # Inject thread_id so same ticker+date resumes, different date starts fresh.
        if checkpoint_enabled:
            tid = thread_id(company_name, str(trade_date))
            args.setdefault("config", {}).setdefault("configurable", {})["thread_id"] = tid

        if checkpoint_enabled and resume_step is not None:
            self._safe_resume_checkpoint(args["config"], company_name, str(trade_date))
            return None, args, resume_step

        # Initialize state only for fresh runs. Passing a new initial state to
        # LangGraph would start a new run and replay completed nodes.
        past_context = self._past_context_as_of(company_name, str(trade_date))
        init_agent_state = self.propagator.create_initial_state(
            company_name,
            trade_date,
            past_context=past_context,
            selected_analysts=getattr(self, "selected_analysts", None),
        )
        # N02: fresh 运行生成身份（每次必然新 run_id）。恢复路径不经过此
        # 处（initial_state=None）——同一 run 从 SQLite 恢复沿用 checkpoint
        # 里的原 ID/配置/创建时间；不改变任何既有 checkpoint key。
        init_agent_state["run_metadata"] = new_run_metadata(
            self.config,
            company_name,
            str(trade_date),
            instrument_type=self.config.get("instrument_type"),
            selected_analysts=getattr(self, "selected_analysts", None),
        )
        return init_agent_state, args, resume_step

    @staticmethod
    def _shared_channel_has_ai_traffic(values: dict) -> bool:
        """共享 messages 通道是否存在 AI/Tool 执行流量（不含初始 human 输入）。

        注意这不是"旧版断点"的充分证据：新版图的辩论/交易/风控节点同样
        把 AIMessage 写进共享通道。它必须与「分支通道是否全空」结合使用
        （见 `_refuse_incompatible_legacy_checkpoint`）。
        """
        for m in values.get("messages") or []:
            msg_type = getattr(m, "type", None)
            if msg_type in ("ai", "tool"):
                return True
            if getattr(m, "tool_calls", None):
                return True
        return False

    def _refuse_incompatible_legacy_checkpoint(
        self, snap, company_name: str, trade_date: str
    ) -> None:
        """拒绝无法证明完整且兼容的旧版执行状态（F4，最终收口）。

        判定依据是拓扑证据，不猜版本号：旧版图（分支消息通道引入之前）
        把分析师的工具调用与报告**全部**写进共享 ``messages`` 通道，分支
        通道必然全空；新版图只要分析师阶段真正运行过，至少一个选中分支
        的 ``{role}_messages`` 通道就有内容（下游辩论/交易/风控节点写共享
        通道不影响这一判据）。因此：

        - 分支通道全空 + 共享通道有 AI/Tool 流量 → 旧版断点，**无论停在
          哪个阶段**——待执行分支节点、汇合屏障（旧 OR 拓扑可能提前触
          发）、已越过汇合点的辩论/交易/风控、乃至执行完毕的终态——其
          报告完整性与屏障触发状态都无法证明与当前 AND 拓扑兼容，一律
          明确拒绝并保留断点；
        - 两侧通道都没有流量 → 新图首次调用在产出任何消息前失败（例如
          LLM 超时）：恢复等价于从头执行该分支，合法重试，必须放行；
        - 任一分支通道有内容 → 新版断点：下游阶段不依赖分支消息，正常
          恢复。
        """
        values = snap.values or {}
        shared_traffic = self._shared_channel_has_ai_traffic(values)
        any_branch_msgs = any(
            values.get(key) for key in BRANCH_MESSAGE_KEYS.values()
        )

        if shared_traffic and not any_branch_msgs:
            pending = ", ".join(repr(n) for n in (snap.next or ())) or "（执行完毕的终态）"
            raise RuntimeError(
                f"检测到不兼容的旧版断点：{company_name} {trade_date} 的执行"
                f"状态（待执行: {pending}）由旧版图写入——分析师消息全部在"
                f"共享 messages 通道、分支通道为空，无法证明各分析师报告"
                f"完整（旧 OR 拓扑下质量检查可能在部分分支未完成时提前"
                f"运行）且汇合屏障状态与当前 AND 拓扑兼容。强行恢复会导致"
                f"工具请求错配或不完整报告直接进入下游决策。已拒绝恢复并"
                f"保留断点；请清除该日期的断点后重新开始分析。"
            )

    def _refuse_team_mismatch_checkpoint(
        self, snap, company_name: str, trade_date: str
    ) -> None:
        """A10/Codex 退回：断点记录的分析师团队与当前图不一致 → 拒绝恢复。

        新断点在初始 state 记录了 ``selected_analysts``；恢复图的团队若与
        之不同（如原 market 恢复成 market+news），静默混用拓扑会让质量门
        控、进度阶段与实际执行的分析师互相矛盾。明确拒绝并保留断点，不
        重跑、不删除、不调用任何节点。

        边界：旧断点（无该字段）无法可靠验证原团队——此时按当前图集合
        执行（质量门控经工厂闭包取当前图启用角色），不声称实现了完整配
        置指纹校验；见第 4 批交接「边界声明」。
        """
        recorded = (snap.values or {}).get("selected_analysts")
        if not recorded:
            return  # 旧断点无元数据：无法验证，按当前图执行（见边界声明）
        current = getattr(self, "selected_analysts", None) or None
        if current is None:
            # 实例未声明启用集合（绕过 __init__ 的桩/异常构造）：无法验证
            # 当前团队，归入同一边界——放行并依赖质量门控的工厂闭包。
            return
        if set(recorded) != set(current):
            raise RuntimeError(
                f"检测到分析师团队不兼容：{company_name} {trade_date} 的断点"
                f"记录的团队为 {sorted(recorded)}，与当前图启用的 "
                f"{sorted(current)} 不一致。静默混用会导致质量门控与实际执"
                f"行互相矛盾；已拒绝恢复并保留断点。请用与原运行相同的分"
                f"析师集合恢复，或清除该日期断点后重新开始。"
            )

    def _safe_resume_checkpoint(self, config, company_name: str, trade_date: str) -> None:
        """恢复前的兼容与安全检查（F4/F5）。

        1. 读取断点状态失败 → 拒绝恢复（无法证明状态安全）；
        2. 旧版执行状态（分支通道全空而共享 messages 通道有 AI/Tool 流
           量——无论停在分支节点、汇合屏障还是其后的辩论/交易/风控/
           终态，都无法证明报告完整且屏障状态兼容）→ 明确拒绝（F4）；
        3. 断点中的 past_context 与按当前时间边界重算的不一致：
           - 决策已生成 → 拒绝恢复：旧决策可能已受未来记忆影响，不能当
             正常结果返回（只擦 past_context 清理不了已混入决策文本的
             内容）；
           - 决策未生成 → 原位刷新；刷新失败（如并行最后写入者导致
             LangGraph 无法推断 as_node）→ 拒绝——带着污染记忆继续就是
             把未来信息喂给尚未执行的决策。
        拒绝路径全部保留断点数据，绝不删除或谎称已自动恢复。
        """
        try:
            snap = self.graph.get_state(config)
        except Exception as e:
            raise RuntimeError(
                f"无法安全恢复 {company_name} {trade_date} 的断点：读取检查点"
                f"状态失败（{type(e).__name__}: {e}）。断点已保留；请检查数据"
                f"目录或清除该日期的断点后重新开始分析。"
            ) from e

        self._refuse_incompatible_legacy_checkpoint(snap, company_name, trade_date)

        values = snap.values or {}
        self._refuse_team_mismatch_checkpoint(snap, company_name, trade_date)

        stale_ctx = values.get("past_context")
        fresh_ctx = self._past_context_as_of(company_name, trade_date)
        if stale_ctx == fresh_ctx:
            return

        if values.get("final_trade_decision"):
            # 决策已生成，且其记忆上下文与按当前时间边界的重算不一致：
            # 该决策可能使用了分析时点之后的记忆。不能只擦 past_context
            # 就把旧决策当正常结果返回（决策文本里已混入未来信息），也
            # 不能静默丢掉。明确拒绝恢复并保留断点，由用户决定重新开始。
            raise RuntimeError(
                f"无法安全恢复 {company_name} {trade_date} 的断点：断点中的最终"
                f"决策生成于旧版记忆规则（其 past_context 含按当前时间边界应"
                f"过滤的内容），该决策可能已受未来记忆影响，不能作为正常分析"
                f"结果返回。已保留断点；请清除该日期的断点后重新开始分析。"
            )

        try:
            self.graph.update_state(config, {"past_context": fresh_ctx})
        except Exception as e:
            raise RuntimeError(
                f"无法安全恢复 {company_name} {trade_date} 的断点：断点中的历史"
                f"记忆上下文包含按当前时间边界规则应过滤的内容，且原位刷新失败"
                f"（{type(e).__name__}: {e}）。为避免历史分析使用未来记忆，已"
                f"保留断点并停止恢复；请清除该日期的断点后重新开始分析。"
            ) from e
        logger.info(
            "Refreshed past_context on resumed checkpoint (time-bound filter) "
            "for %s %s", company_name, trade_date,
        )

    def _past_context_as_of(self, company_name: str, trade_date: str) -> str:
        """Memory context limited to what was knowable at the analysis time.

        ``trade_date`` is interpreted as *after that day's close*: same-day
        decisions and outcomes whose window closed that day are visible.
        Passing ``as_of`` only filters; the memory log itself is never
        rewritten (R4).
        """
        return self.memory_log.get_past_context(company_name, as_of=trade_date)

    def finalize_graph_run(self, company_name, trade_date, final_state):
        """Persist a completed run and clear its checkpoint."""
        with self.run_context():
            return self._finalize_graph_run(company_name, trade_date, final_state)

    def _finalize_graph_run(self, company_name, trade_date, final_state):
        self.curr_state = final_state

        # Log state to disk.
        self._log_state(trade_date, final_state)

        # Store decision for deferred reflection on the next same-ticker run.
        self.memory_log.store_decision(
            ticker=company_name,
            trade_date=trade_date,
            final_trade_decision=final_state["final_trade_decision"],
        )

        # Clear checkpoint on successful completion to avoid stale state.
        if self.config.get("checkpoint_enabled"):
            clear_checkpoint(
                self.config["data_cache_dir"], company_name, str(trade_date)
            )

        return self.process_signal(final_state["final_trade_decision"])

    def close_graph_run(self) -> None:
        """Close the active checkpointer context, if any."""
        with self.run_context():
            if self._checkpointer_ctx is not None:
                self._checkpointer_ctx.__exit__(None, None, None)
                self._checkpointer_ctx = None
                self.graph = self.workflow.compile()

    def _run_graph(self, company_name, trade_date):
        """Execute the graph and write the resulting state to disk and memory log."""
        # A05: prepare（含 checkpoint 重编译与 SqliteSaver 上下文获取）也在
        # 关闭保护范围内——prepare 抛错同样必须 close，避免连接泄漏。
        try:
            init_agent_state, args, _ = self.prepare_graph_run(
                company_name, trade_date
            )
            if self.debug:
                final_state = None
                # 分析师工具循环的消息在按角色隔离的分支通道上（R1），
                # debug 输出改为增量打印各通道新消息；共享 messages 通道
                # 只保留初始输入，不再驱动打印。
                printed = {}
                for chunk in self.graph.stream(init_agent_state, **args):
                    final_state = chunk
                    for key in ("messages", *_ANALYST_MESSAGE_KEYS):
                        msgs = chunk.get(key) or []
                        start = printed.get(key, 0)
                        for m in msgs[start:]:
                            m.pretty_print()
                        if len(msgs) > start:
                            printed[key] = len(msgs)
                if final_state is None:
                    final_state = self.graph.get_state(args.get("config", {})).values
            else:
                final_state = self.graph.invoke(init_agent_state, **args)

            signal = self.finalize_graph_run(company_name, trade_date, final_state)
            return final_state, signal
        finally:
            self.close_graph_run()

    def _log_state(self, trade_date, final_state):
        """Log the final state to a JSON file.

        A14: 全部字段容错读取（部分完成的恢复路径可能缺字段）；交易员
        计划以 canonical 字段 + legacy 别名双写（旧 JSON 读者兼容），展示/
        导出经 ``web.report_fields.trader_plan`` 统一读取（canonical 优先、
        只展示一次）；质量结论随统一 JSON 落盘，历史重载与实时一致。
        """
        debate = final_state.get("investment_debate_state") or {}
        risk = final_state.get("risk_debate_state") or {}
        trader_text = final_state.get("trader_investment_plan", "")
        self.log_states_dict[str(trade_date)] = {
            "company_of_interest": final_state.get("company_of_interest", ""),
            "trade_date": final_state.get("trade_date", str(trade_date)),
            "market_report": final_state.get("market_report", ""),
            "sentiment_report": final_state.get("sentiment_report", ""),
            "news_report": final_state.get("news_report", ""),
            "fundamentals_report": final_state.get("fundamentals_report", ""),
            "policy_report": final_state.get("policy_report", ""),
            "hot_money_report": final_state.get("hot_money_report", ""),
            "lockup_report": final_state.get("lockup_report", ""),
            "investment_debate_state": {
                "bull_history": debate.get("bull_history", ""),
                "bear_history": debate.get("bear_history", ""),
                "history": debate.get("history", ""),
                "current_response": debate.get("current_response", ""),
                "judge_decision": debate.get("judge_decision", ""),
            },
            "trader_investment_plan": trader_text,
            "trader_investment_decision": trader_text,
            "data_quality_summary": final_state.get("data_quality_summary", ""),
            # N01: 结构化质量卡随统一 JSON 保存（旧报告无此键 → 加载端显示
            # 未记录，不凭空生成）。
            "data_quality": final_state.get("data_quality"),
            "risk_debate_state": {
                "aggressive_history": risk.get("aggressive_history", ""),
                "conservative_history": risk.get("conservative_history", ""),
                "neutral_history": risk.get("neutral_history", ""),
                "history": risk.get("history", ""),
                "judge_decision": risk.get("judge_decision", ""),
            },
            "investment_plan": final_state.get("investment_plan", ""),
            "final_trade_decision": final_state.get("final_trade_decision", ""),
            # N02: 运行档案随报告保存（旧断点/旧流程无档案 → null = 未记录，
            # 加载端不得把恢复时的当前配置伪装成原配置）。
            "run_metadata": final_state.get("run_metadata"),
        }

        # Save to file. Reject ticker values that would escape the
        # results directory when joined as a path component.
        safe_ticker = safe_ticker_component(self.ticker)
        # 日期是文件名组件：严格 YYYY-MM-DD，拒绝任何穿越/畸形形态。
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(trade_date)):
            raise ValueError(
                f"非法的分析日期 {trade_date!r}：必须为 YYYY-MM-DD。"
            )
        payload = self.log_states_dict[str(trade_date)]

        legacy_dir = (
            Path(self.config["results_dir"]) / safe_ticker / "TradingAgentsStrategy_logs"
        )
        legacy_dir.mkdir(parents=True, exist_ok=True)
        latest_path = legacy_dir / f"full_states_log_{trade_date}.json"

        # N02 发布事务（Codex 冻结审计语义）：以同 ticker/date 的发布范围
        # （latest 路径为锚的跨实例/跨进程锁）保护「版本冲突检查 → 版本
        # 写入 → latest 更新」整体——exists+replace 无互斥会让并发写者都
        # 发布成功。锁内规则：
        #   - 版本已存在且内容冲突 → 显式拒绝（历史不可变），latest 不动；
        #   - 版本已存在且内容相同（重试）→ 必须补发布 latest（此前 latest
        #     写失败不能永久漏发布），但 latest 已是**较新**运行时不倒退；
        #   - 版本不存在 → 先写版本，成功后按同一不倒退规则写 latest
        #     （版本写失败异常传播，不触碰 latest）。
        meta = final_state.get("run_metadata")
        has_archive = is_run_metadata(meta) and validate_run_id(meta.get("run_id", ""))
        version_path = None
        if has_archive:
            run_dir = (
                Path(self.config["results_dir"]) / "_runs" / meta["run_id"]
                / safe_ticker / "TradingAgentsStrategy_logs"
            )
            run_dir.mkdir(parents=True, exist_ok=True)
            version_path = run_dir / f"full_states_log_{trade_date}.json"

        with file_lock(latest_path):
            if version_path is not None:
                if version_path.exists():
                    existing = json.loads(version_path.read_text(encoding="utf-8"))
                    if json.dumps(existing, sort_keys=True, ensure_ascii=False) != json.dumps(
                        payload, sort_keys=True, ensure_ascii=False
                    ):
                        raise RuntimeError(
                            f"运行 {meta['run_id']} 在 {trade_date} 的报告已存档且"
                            f"内容不同，拒绝覆盖（历史记录不可变）。"
                        )
                    # 相同版本重试：走 latest 补发布（不重写已发布版本）
                else:
                    self._atomic_write_json(version_path, payload)
            self._publish_latest_without_regression(latest_path, payload)

    def _publish_latest_without_regression(self, latest_path: Path, payload: dict) -> None:
        """更新最新兼容入口；已存在**较新**运行时不倒退。

        Codex 冻结语义：相同版本重试必须修复未发布的 latest；但 latest
        当前内容若属于创建时间更晚的另一个 run，保持较新者。latest 缺失/
        畸形/无档案（旧格式）→ 视作可发布。
        """
        current_created = ""
        cur_meta = payload.get("run_metadata")
        if isinstance(cur_meta, dict):
            current_created = str(cur_meta.get("created_at") or "")
        if latest_path.exists():
            try:
                existing = json.loads(latest_path.read_text(encoding="utf-8"))
                old_meta = existing.get("run_metadata") if isinstance(existing, dict) else None
                old_created = (
                    str(old_meta.get("created_at") or "")
                    if isinstance(old_meta, dict) else ""
                )
                if old_created and current_created and old_created > current_created:
                    return  # latest 已是较新运行：不倒退
            except (OSError, json.JSONDecodeError):
                pass  # 畸形 latest：覆盖修复
        self._atomic_write_json(latest_path, payload)

    @staticmethod
    def _atomic_write_json(path: Path, payload: dict) -> None:
        """原子写 JSON：唯一临时文件 + os.replace。

        任何失败（序列化/IO）都清理临时文件——不留半份可见 JSON，也不留
        孤儿 .tmp。
        """
        tmp_path = path.with_suffix(
            f".tmp.{os.getpid()}.{threading.get_ident()}.json"
        )
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=4, ensure_ascii=False)
            os.replace(tmp_path, path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    def process_signal(self, full_signal):
        """Process a signal to extract the core decision."""
        return self.signal_processor.process_signal(full_signal)
