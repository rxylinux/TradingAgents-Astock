"""指数分析图：`TradingAgentsIndexGraph`，与个股图 `TradingAgentsGraph` 隔离。

组合方式（不改父类一行代码）：
- 分析师预设为指数适用集合（剔除纯个股的 fundamentals/lockup）；
- `GraphSetup(node_factories=INDEX_NODE_FACTORIES)` 依赖注入换上指数版
  agent（见 agents/index_agents.py），图拓扑/条件边/质量门控/记忆全部复用；
- `_fetch_returns` 覆写：分析对象本身是沪深300 时，基准换上证指数——
  否则 alpha 对自己恒为 0，方向正确率退化成绝对涨跌。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import yfinance as yf

from tradingagents.agents.index_agents import INDEX_NODE_FACTORIES
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.setup import GraphSetup
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.graph.trading_graph import (
    _is_unsupported_by_yfinance,
    _normalize_yfinance_ticker,
)

logger = logging.getLogger(__name__)

# 指数模式默认分析师：market（指数技术面）+ social（全市场情绪）+ news（系统性
# 新闻）+ policy（政策传导）+ hot_money（大盘资金流）。config 传自定义集合可覆盖。
INDEX_ANALYSTS = ["market", "social", "news", "policy", "hot_money"]


class TradingAgentsIndexGraph(TradingAgentsGraph):
    """市场指数分析图。签名与父类一致，`selected_analysts` 缺省为指数预设。"""

    def __init__(
        self,
        selected_analysts: Optional[List[str]] = None,
        debug: bool = False,
        config: Dict[str, Any] = None,
        callbacks: Optional[List] = None,
    ):
        cfg = dict(config or DEFAULT_CONFIG)
        cfg["instrument_type"] = "index"
        if selected_analysts is None:
            selected_analysts = list(INDEX_ANALYSTS)

        # 父类先用个股工厂把图建起来（保证 __init__ 全部副作用照常发生），
        # 紧接着用指数工厂重建 workflow——工厂调用本身零成本。
        super().__init__(
            selected_analysts=selected_analysts,
            debug=debug,
            config=cfg,
            callbacks=callbacks,
        )
        self.graph_setup = GraphSetup(
            self.quick_thinking_llm,
            self.deep_thinking_llm,
            self.tool_nodes,
            self.conditional_logic,
            resolve_llm=self.role_llms.get,
            node_factories=INDEX_NODE_FACTORIES,
        )
        self.workflow = self.graph_setup.setup_graph(selected_analysts)
        self.graph = self.workflow.compile()
        logger.info(
            "TradingAgentsIndexGraph ready: analysts=%s, index factories injected",
            selected_analysts,
        )

    def _fetch_returns(
        self, ticker: str, trade_date: str, holding_days: int = 5
    ) -> Tuple[Optional[float], Optional[float], Optional[int]]:
        """指数版收益结算：基准恒为沪深300，唯一例外是分析对象=沪深300 本身。

        分析对象就是 000300.SH 时对自身算 alpha 恒为 0，改用上证指数并注明。
        优先使用原生指数 K 线链路，Yahoo Finance 作为兜底。
        """
        benchmark = (
            "000001.SH"
            if str(ticker).strip().upper() in ("000300.SH", "000300.SS", "000300")
            else "000300.SH"
        )
        if benchmark == "000001.SH":
            logger.info(
                "Instrument is CSI300 itself: benchmark switched to "
                "000001.SH (SSE Composite) for alpha."
            )
        return super()._fetch_returns(
            ticker, trade_date, holding_days=holding_days, benchmark_ticker=benchmark
        )
