"""大盘资金流报告：聚合市场级资金数据 → 单次 LLM 整合成中文报告。

独立「资金流向」分析类型的数据层与生成器（与完整 12 阶段流水线无关，
1 次 LLM 调用出报告，成本约为完整分析的 1/20）。

数据面（全部免费源，东财请求走 `_em_get` 节流）：
- 指数资金流（默认上证指数）：主力/大中小单，当日分钟 + 近 20 日
- 北向资金（同花顺 hsgtApi + 本地缓存）
- 行业板块主力净流入排名（东财 clist，fs=m:90+t:2）
- 概念板块主力净流入排名（东财 clist，fs=m:90+t:3）
- 涨停/强势股题材归因（同花顺 getharden）
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict

logger = logging.getLogger(__name__)


def get_sector_fund_flow_rank(fs_type: str = "m:90+t:2", top_n: int = 10) -> str:
    """板块主力净流入排名（东财 push2 clist）。

    fs_type: "m:90+t:2" 行业板块 / "m:90+t:3" 概念板块。
    字段实测（2026-08-16）：f12 板块代码 f14 板块名 f3 涨跌幅 f62 主力净流入(元)
    f184 主力净占比(%) f204 领涨股 f205 领涨股代码。
    """
    from .dataflows.a_stock import _em_get

    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1", "pz": "100", "po": "1", "np": "1",
        "fltt": "2", "invt": "2",
        "fs": fs_type,
        "fields": "f12,f14,f3,f62,f184,f204,f205",
    }
    kind = "行业" if "t:2" in fs_type else "概念"
    try:
        r = _em_get(url, params=params, timeout=15)
        items = r.json().get("data", {}).get("diff", [])
        if not items:
            return f"{kind}板块资金流数据获取为空。"

        ranked = sorted(items, key=lambda x: (x.get("f62") or 0), reverse=True)

        def _fmt(item: dict) -> str:
            flow = (item.get("f62") or 0) / 1e8
            pct = item.get("f184")
            pct_s = f"{pct:+.2f}%" if isinstance(pct, (int, float)) else "N/A"
            chg = item.get("f3")
            chg_s = f"{chg:+.2f}%" if isinstance(chg, (int, float)) else "N/A"
            leader = item.get("f204") or ""
            return (
                f"  {item.get('f14', '')}: 主力净流入 {flow:+.1f} 亿 "
                f"(净占比 {pct_s}, 板块涨跌 {chg_s}"
                + (f", 领涨 {leader}" if leader else "")
                + ")"
            )

        lines = [f"## {kind}板块主力净流入 TOP{top_n}"]
        lines.extend(_fmt(i) for i in ranked[:top_n])
        lines.append(f"\n## {kind}板块主力净流出 BOTTOM5")
        lines.extend(_fmt(i) for i in ranked[-5:][::-1])
        return "\n".join(lines)
    except Exception as e:
        return f"{kind}板块资金流排名查询失败: {e}"


def gather_market_flow_data(
    index_id: str = "000001.SH", curr_date: str = ""
) -> str:
    """聚合全部市场级资金数据为一段原始素材（供 LLM 整合）。"""
    from .dataflows.a_stock import get_hot_stocks, get_northbound_flow
    from .dataflows.index_data import get_index_fund_flow, get_index_news
    from .dataflows.index_registry import parse_index_ticker

    spec = parse_index_ticker(index_id)
    if spec is None:
        raise ValueError(f"{index_id!r} 不是指数标识（如 000001.SH / 399006.SZ）")
    if not curr_date:
        curr_date = datetime.now().strftime("%Y-%m-%d")

    # start 窗口：近 7 天新闻足够解释当日资金面
    start = datetime.strptime(curr_date, "%Y-%m-%d")
    from datetime import timedelta

    start_str = (start - timedelta(days=7)).strftime("%Y-%m-%d")

    parts = [
        f"# 大盘资金流原始数据 · {spec.name} ({spec.ticker}) · {curr_date}",
        "",
        get_index_fund_flow(spec.ticker, curr_date, include_history=True),
        "",
        get_northbound_flow(curr_date, include_history=True),
        "",
        get_sector_fund_flow_rank("m:90+t:2", top_n=10),
        "",
        get_sector_fund_flow_rank("m:90+t:3", top_n=8),
        "",
        get_hot_stocks(curr_date),
        "",
        get_index_news(spec.ticker, start_str, curr_date),
    ]
    return "\n\n".join(parts)


_REPORT_PROMPT = """你是 A 股市场的大盘资金流分析师。基于下面的原始数据，写一份**结构化的中文大盘资金流报告**。

报告结构（Markdown）：
1. **资金面总览** — 一句话结论：主力在进还是出、外资方向、市场处于什么资金格局（主力流入/流出/存量博弈/无明显信号）
2. **指数资金流** — {index_name} 主力净流入（当日 + 近 20 日趋势），大中小单结构
3. **北向资金** — 当日净流入与近 20 日均值对比，外资态度
4. **板块资金动向** — 行业主力净流入 TOP 与净流出 BOTTOM，资金在聚集/撤离哪些方向；概念板块亮点
5. **赚钱效应** — 涨停家数、题材集中度、情绪温度
6. **综合研判** — 资金面对短期市场的含义（2-4 句，落在大盘层面，不谈个股）
7. **数据质量说明** — 哪些数据缺失或异常，如何影响结论

要求：
- 每个结论都要引用具体数字（金额/家数/排名）
- 数据缺失就明说，不要编造
- 末尾附免责声明一行（仅供研究参考，不构成投资建议）
- 全文中文，800-1500 字

── 原始数据 ──
{raw_data}
── 原始数据结束 ──
"""


def generate_market_flow_report(
    config: Dict[str, Any],
    index_id: str = "000001.SH",
    curr_date: str = "",
) -> str:
    """拉齐数据 → 一次 quick LLM 调用 → 中文资金流报告（Markdown）。"""
    from .dataflows.index_registry import parse_index_ticker
    from .llm_clients import create_llm_client

    spec = parse_index_ticker(index_id)
    if spec is None:
        raise ValueError(f"{index_id!r} 不是指数标识")

    raw = gather_market_flow_data(spec.ticker, curr_date)

    # quick 档模型：单次整合调用，无需 deep
    client = create_llm_client(
        provider=config.get("llm_provider", "openai"),
        model=config.get("quick_think_llm"),
        base_url=config.get("backend_url"),
    )
    llm = client.get_llm()

    prompt = _REPORT_PROMPT.format(index_name=spec.name, raw_data=raw)
    response = llm.invoke(prompt)
    content = response.content if hasattr(response, "content") else str(response)

    import re

    content = re.sub(r"<think>.*?</think>\s*", "", content, flags=re.DOTALL).strip()

    header = (
        f"# 大盘资金流报告 · {spec.name} ({spec.ticker}) · "
        f"{curr_date or datetime.now().strftime('%Y-%m-%d')}\n\n"
    )
    return header + content
