"""指数数据层（市场指数专用 vendor），与个股 vendor `a_stock.py` 完全隔离。

设计原则（对应架构评审要求）：
- **不改 `a_stock.py` 的任何行为**。指数请求在 `interface.route_to_vendor` 的
  接缝处分流到这里，裸 6 位代码永远不会进入本模块；
- 个股专属接口（财报/解禁/龙虎榜等）对指数标识返回明确的错误说明，而不是
  静默查询同号个股（000001.SH=上证指数 vs 000001=平安银行，见 index_registry）；
- 复用 `a_stock` 的三个无个股假设的构件：`_em_get`（东财节流）、`_mootdx_call`
  （服务器自愈）、`_merge_ohlcv`（OHLCV 合并）——节流入口必须全局唯一，绝不能
  另起一套。

数据源：
- K 线：mootdx ``index_bars``（000/880/999→沪、399→深）主源，新浪 HTTP 降级
- 行情：腾讯 qt.gtimg.cn（sh000001 / sz399006 形式，与个股同格式）
- 资金流：东财 push2 fflow（指数 secid 如 1.000001），口径与个股一致——
  **指数资金流就是大盘资金流**；历史时点截断规则与个股版相同（防未来函数）
- 新闻：东财搜索，关键词用指数中文名（纯数字代码搜不到指数资讯）
"""

from __future__ import annotations

import logging
import os
import urllib.request
from datetime import datetime

import pandas as pd

from .a_stock import (
    _em_get,
    _is_historical,
    _market_today,
    _merge_ohlcv,
    _mootdx_call,
    _needs_sina_supplement,
    _normalize_ohlcv_dates,
)
from .index_registry import (
    IndexSpec,
    index_market_for_bare_code,
    lookup_index_name,
    make_index_spec,
    parse_index_ticker,
)

logger = logging.getLogger(__name__)


def _index_prefix(spec: IndexSpec) -> str:
    return "sh" if spec.exchange == "SH" else "sz"


# ---------------------------------------------------------------------------
# 指数 K 线：mootdx index_bars 主源 + 新浪降级 + 独立缓存
# ---------------------------------------------------------------------------


def _index_kline_mootdx(spec: IndexSpec) -> pd.DataFrame:
    """指数日线：mootdx ``index_bars``（市场判定 000/880/999→沪、399→深）。"""
    df = _mootdx_call("index_bars", symbol=spec.code, frequency=9, offset=800)
    if df is None or df.empty:
        raise ValueError(f"No index K-line data from mootdx for {spec.ticker}")
    df = df.drop(
        columns=["datetime", "year", "month", "day", "hour", "minute"],
        errors="ignore",
    )
    df = df.reset_index()
    df = df.rename(
        columns={
            "datetime": "Date",
            "open": "Open",
            "close": "Close",
            "high": "High",
            "low": "Low",
            "volume": "Volume",
        }
    )
    df = df[["Date", "Open", "High", "Low", "Close", "Volume"]]
    return _normalize_ohlcv_dates(df)


def _sina_index_kline(
    spec: IndexSpec, start_date: str = None, end_date: str = None
) -> pd.DataFrame:
    """新浪指数日 K（sh000001 / sz399006），mootdx 降级源。"""
    import requests as _requests

    url = (
        "http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData"
    )
    params = {
        "symbol": f"{_index_prefix(spec)}{spec.code}",
        "scale": "240",
        "ma": "no",
        "datalen": "800",
    }
    r = _requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    import json as _json

    data = _json.loads(r.text)
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(
        {
            "Date": pd.to_datetime([item["day"] for item in data]),
            "Open": [float(item["open"]) for item in data],
            "High": [float(item["high"]) for item in data],
            "Low": [float(item["low"]) for item in data],
            "Close": [float(item["close"]) for item in data],
            "Volume": [int(item["volume"]) for item in data],
        }
    )
    if start_date:
        df = df[df["Date"] >= pd.to_datetime(start_date)]
    if end_date:
        df = df[df["Date"] <= pd.to_datetime(end_date)]
    return df


def _supplement_index_kline_with_sina(
    spec: IndexSpec, df: pd.DataFrame, target_date: str | None
) -> pd.DataFrame:
    """缓存/K 线数据早于分析日时，用新浪补齐缺口（防「数据停在昨天」）。"""
    if not _needs_sina_supplement(df, target_date):
        return df
    try:
        sina_df = _sina_index_kline(spec, None, target_date)
    except Exception as e:
        logger.warning("sina index K-line supplement failed for %s: %s", spec.ticker, e)
        return df
    if sina_df.empty:
        return df
    return _merge_ohlcv(df, sina_df)


def _fetch_raw_index_ohlcv(spec: IndexSpec, target_date: str | None = None) -> pd.DataFrame:
    """Fetch full index OHLCV via cache -> mootdx -> Sina fallback (unfiltered)."""
    from .config import get_config

    config = get_config()
    cache_dir = config.get(
        "data_cache_dir", os.path.expanduser("~/.tradingagents/cache")
    )
    os.makedirs(cache_dir, exist_ok=True)
    cache_file = os.path.join(cache_dir, f"{spec.ticker}-index-daily.csv")

    if os.path.exists(cache_file):
        mtime = datetime.fromtimestamp(os.path.getmtime(cache_file))
        if mtime.date() == datetime.now().date():
            data = pd.read_csv(cache_file, on_bad_lines="skip", encoding="utf-8")
            data = _normalize_ohlcv_dates(data)
            data = _supplement_index_kline_with_sina(spec, data, target_date)
            data.to_csv(cache_file, index=False, encoding="utf-8")
            return data

    try:
        df = _index_kline_mootdx(spec)
    except Exception as e:
        logger.warning(
            "mootdx index K-line failed for %s: %s, trying sina HTTP fallback",
            spec.ticker, e,
        )
        try:
            df = _sina_index_kline(spec)
            if df.empty:
                raise ValueError(f"No index K-line from sina for {spec.ticker}")
        except Exception:
            raise ValueError(
                f"No index K-line data from mootdx/sina for {spec.ticker}"
            )

    df = _supplement_index_kline_with_sina(spec, df, target_date)
    df.to_csv(cache_file, index=False, encoding="utf-8")
    return df


def _load_index_ohlcv(spec: IndexSpec, curr_date: str) -> pd.DataFrame:
    """指数版缓存加载：文件名带 index 标记，绝不与同号个股缓存互踩。"""
    df = _fetch_raw_index_ohlcv(spec, target_date=curr_date)
    cutoff = pd.to_datetime(curr_date)
    return df[df["Date"] <= cutoff]


def get_index_history_df(
    spec_or_ticker: IndexSpec | str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> pd.DataFrame:
    """Get full daily OHLCV DataFrame for an index across date range."""
    spec = parse_index_ticker(spec_or_ticker) if isinstance(spec_or_ticker, str) else spec_or_ticker
    if spec is None:
        return pd.DataFrame(columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    df = _fetch_raw_index_ohlcv(spec, target_date=end_date)
    if df is None or df.empty:
        return pd.DataFrame(columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    df = df.copy()
    if start_date:
        df = df[df["Date"] >= pd.to_datetime(start_date)]
    if end_date:
        df = df[df["Date"] <= pd.to_datetime(end_date)]
    return df.sort_values("Date").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Vendor 方法（供 interface 接缝调用的指数实现）
# ---------------------------------------------------------------------------


def get_index_stock_data(ticker: str, start_date: str, end_date: str) -> str:
    """指数 K 线（OHLCV），输出格式与个股 get_stock_data 一致。"""
    spec = parse_index_ticker(ticker)
    if spec is None:
        raise ValueError(f"{ticker!r} 不是指数标识（应为 000001.SH / 399006.SZ 形式）")

    data_source = "mootdx index_bars (TCP)"
    try:
        df = _index_kline_mootdx(spec)
    except Exception as e:
        logger.warning(
            "mootdx index K-line failed for %s: %s, trying sina HTTP fallback",
            spec.ticker, e,
        )
        try:
            df = _sina_index_kline(spec, start_date, end_date)
            if df.empty:
                return "指数K线数据获取失败：mootdx和新浪备用源均不可用，请检查网络连接"
            data_source = "sina HTTP (fallback)"
        except Exception:
            return "指数K线数据获取失败：mootdx和新浪备用源均不可用，请检查网络连接"

    df = _supplement_index_kline_with_sina(spec, df, end_date)

    start_dt = pd.to_datetime(start_date)
    end_dt = pd.to_datetime(end_date)
    df = df[(df["Date"] >= start_dt) & (df["Date"] <= end_dt)]

    if df.empty:
        return (
            f"No data found for index '{spec.name} ({spec.ticker})' "
            f"between {start_date} and {end_date}"
        )

    for col in ["Open", "High", "Low", "Close"]:
        if col in df.columns:
            df[col] = df[col].round(2)

    df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
    csv_out = df[["Date", "Open", "High", "Low", "Close", "Volume"]].to_csv(
        index=False
    )

    header = (
        f"# Index data for {spec.name} ({spec.ticker}) "
        f"from {start_date} to {end_date}\n"
    )
    header += f"# Total records: {len(df)}\n"
    header += f"# Data source: {data_source}\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + csv_out


def get_index_indicators(
    ticker: str, indicator: str, curr_date: str, look_back_days: int
) -> str:
    """指数技术指标：stockstats 作用于指数 K 线（与个股版同清单）。"""
    from dateutil.relativedelta import relativedelta
    from stockstats import wrap

    from .a_stock import _INDICATOR_DESCRIPTIONS

    spec = parse_index_ticker(ticker)
    if spec is None:
        raise ValueError(f"{ticker!r} 不是指数标识")

    if indicator not in _INDICATOR_DESCRIPTIONS:
        raise ValueError(
            f"Indicator {indicator} not supported. "
            f"Choose from: {list(_INDICATOR_DESCRIPTIONS.keys())}"
        )

    try:
        data = _load_index_ohlcv(spec, curr_date)
        df = wrap(data)
        df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
        df[indicator]

        ind_dict = {}
        for _, row in df.iterrows():
            v = row[indicator]
            ind_dict[row["Date"]] = "N/A" if pd.isna(v) else str(round(float(v), 4))

        curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
        before = curr_dt - relativedelta(days=look_back_days)

        lines = []
        dt = curr_dt
        while dt >= before:
            ds = dt.strftime("%Y-%m-%d")
            val = ind_dict.get(ds, "N/A: Not a trading day (weekend or holiday)")
            lines.append(f"{ds}: {val}")
            dt -= relativedelta(days=1)

        return (
            f"## {indicator} values for {spec.name} ({spec.ticker}) "
            f"from {before.strftime('%Y-%m-%d')} to {curr_date}:\n\n"
            + "\n".join(lines)
            + "\n\n"
            + _INDICATOR_DESCRIPTIONS.get(indicator, "")
        )
    except Exception as e:
        return f"Error calculating {indicator} for {spec.ticker}: {str(e)}"


def get_index_fund_flow(
    ticker: str, curr_date: str, include_history: bool = True
) -> str:
    """指数/大盘资金流：东财 push2 fflow + 指数 secid（如上证 1.000001）。

    与个股版同口径（主力/大中小单、分钟实时 + 20 日历史），并继承同样的
    未来函数规则：复盘历史日期时跳过实时分钟段、历史行按分析日截断。
    """
    spec = parse_index_ticker(ticker)
    if spec is None:
        raise ValueError(f"{ticker!r} 不是指数标识")

    secid = spec.secid
    lines = [
        f"# Index/Market Fund Flow for {spec.name} ({spec.ticker})",
        f"# Source: 东财 push2 (Eastmoney)",
        f"# Retrieved: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]

    historical = _is_historical(curr_date)
    if historical:
        lines.append(
            f"（分析日期 {curr_date} 早于今天，已略去实时分钟资金流——"
            f"那是今天的盘中数据，不是 {curr_date} 当天的。）\n"
        )

    try:
        url_rt = "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
        params_rt = {
            "secid": secid, "klt": 1,
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57",
        }
        klines = []
        if not historical:
            r = _em_get(url_rt, params=params_rt, timeout=10)
            klines = r.json().get("data", {}).get("klines", [])

        if klines:
            lines.append(
                "## Realtime Minute Flow (主力/小单/中单/大单/超大单 净流入, 元)"
            )
            for line in klines[-10:]:
                parts = line.split(",")
                if len(parts) >= 6:
                    lines.append(
                        f"  {parts[0]}: "
                        f"主力={float(parts[1])/1e4:.0f}万 "
                        f"大单={float(parts[4])/1e4:.0f}万 "
                        f"超大单={float(parts[5])/1e4:.0f}万"
                    )
            last_parts = klines[-1].split(",")
            if len(last_parts) >= 2:
                main_net = float(last_parts[1])
                lines.append(f"\nClose: 主力净流入={main_net/1e4:.0f}万元")
                if main_net > 0:
                    lines.append("Signal: Net main force INFLOW (bullish)")
                elif main_net < 0:
                    lines.append("Signal: Net main force OUTFLOW (bearish)")
        else:
            lines.append("No realtime fund flow (non-trading hours or holiday)")

        if include_history:
            url_hist = (
                "https://push2his.eastmoney.com"
                "/api/qt/stock/fflow/daykline/get"
            )
            # 与个股版同规则：窗口按分析日间隔放大（上限 500），过滤后裁回 20 行。
            hist_limit = 20
            if historical:
                gap_days = (_market_today() - datetime.strptime(
                    str(curr_date)[:10], "%Y-%m-%d").date()).days
                hist_limit = min(500, 20 + int(gap_days * 0.7) + 20)
            params_hist = {
                "secid": secid, "lmt": hist_limit, "klt": 101,
                "fields1": "f1,f2,f3,f7",
                "fields2": "f51,f52,f53,f54,f55,f56,f57",
            }
            rh = _em_get(url_hist, params=params_hist, timeout=10)
            hist_klines = rh.json().get("data", {}).get("klines", [])

            if historical:
                cutoff = str(curr_date)[:10]
                hist_klines = [
                    k for k in hist_klines if k.split(",")[0][:10] <= cutoff
                ]
                hist_klines = hist_klines[-20:]

            if historical and not hist_klines:
                lines.append(
                    f"\n## Historical Daily Fund Flow\n"
                    f"（{str(curr_date)[:10]} 及之前的资金流未能取到：该接口只提供"
                    f"从今天回溯的窗口，分析日过早时可能已超出可回溯范围。）"
                )
            elif hist_klines:
                lines.append(
                    f"\n## Historical Daily Fund Flow "
                    f"(last {len(hist_klines)} trading days"
                    + (f", 截至 {str(curr_date)[:10]}" if historical else "")
                    + ")"
                )
                lines.append(
                    "Date | 主力净流入(万) | 大单(万) | 中单(万) | 小单(万) | 超大单(万)"
                )
                for line in hist_klines:
                    parts = line.split(",")
                    if len(parts) >= 6:
                        lines.append(
                            f"  {parts[0]} "
                            f"| main={float(parts[1])/1e4:.0f} "
                            f"| large={float(parts[4])/1e4:.0f} "
                            f"| mid={float(parts[3])/1e4:.0f} "
                            f"| small={float(parts[2])/1e4:.0f} "
                            f"| super={float(parts[5])/1e4:.0f}"
                        )

        return "\n".join(lines)
    except Exception as e:
        return f"Error fetching index fund flow for {spec.ticker}: {str(e)}"


def get_index_news(ticker: str, start_date: str, end_date: str) -> str:
    """指数新闻：东财搜索用指数中文名作关键词（数字代码搜不到指数资讯）。"""
    from .a_stock import _fetch_news_eastmoney

    spec = parse_index_ticker(ticker)
    if spec is None:
        raise ValueError(f"{ticker!r} 不是指数标识")

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    try:
        articles = _fetch_news_eastmoney(spec.name)
    except Exception as e:
        logger.warning("East Money index news fetch failed for %s: %s", spec.ticker, e)
        articles = []

    news_str = ""
    count = 0
    for art in articles:
        pub_time = art.get("time", "")
        try:
            pub_dt = datetime.strptime(pub_time[:10], "%Y-%m-%d")
            if pub_dt < start_dt or pub_dt > end_dt:
                continue
        except (ValueError, IndexError):
            pass

        title = art["title"]
        content = art.get("content", "")
        link = art.get("url", "")

        news_str += f"### {title} (source: {art.get('source', '东方财富')})\n"
        if content:
            snippet = content[:300] + "..." if len(content) > 300 else content
            news_str += f"{snippet}\n"
        if link and link != "nan":
            news_str += f"Link: {link}\n"
        news_str += "\n"
        count += 1

    if count == 0:
        return (
            f"No news found for index '{spec.name} ({spec.ticker})' "
            f"between {start_date} and {end_date}"
        )
    return (
        f"## {spec.name} ({spec.ticker}) Index News, "
        f"from {start_date} to {end_date}:\n\n" + news_str
    )


def get_index_industry_comparison(
    ticker: str, trade_date: str, top_n: int = 20
) -> str:
    """行业横向对比（市场级数据）：直接复用个股实现，仅把标题里的代码换为指数标识。"""
    from .a_stock import get_industry_comparison as _astock_cmp

    spec = parse_index_ticker(ticker)
    if spec is None:
        raise ValueError(f"{ticker!r} 不是指数标识")
    return _astock_cmp(spec.ticker, trade_date, top_n)


# ---------------------------------------------------------------------------
# 个股专属接口的指数拒绝
# ---------------------------------------------------------------------------

_STOCK_ONLY_METHODS = {
    "get_fundamentals": "财务与估值数据",
    "get_balance_sheet": "资产负债表",
    "get_cashflow": "现金流量表",
    "get_income_statement": "利润表",
    "get_insider_transactions": "股东/内部人交易数据",
    "get_profit_forecast": "分析师盈利一致预期",
    "get_concept_blocks": "概念/板块归属",
    "get_dragon_tiger_board": "龙虎榜数据",
    "get_lockup_expiry": "限售解禁数据",
}


def _index_unsupported_msg(spec: IndexSpec, what: str) -> str:
    return (
        f"Error: '{spec.name}（{spec.ticker}）' 是市场指数，{what} 仅适用于个股，"
        f"指数没有对应数据。指数分析请改用：get_stock_data（指数K线）、"
        f"get_indicators（技术指标）、get_fund_flow（指数/大盘资金流）、"
        f"get_news（指数新闻）、get_northbound_flow（北向资金）等工具。"
    )


# ---------------------------------------------------------------------------
# 接口层分流入口
# ---------------------------------------------------------------------------

_INDEX_METHODS = {
    "get_stock_data": get_index_stock_data,
    "get_indicators": get_index_indicators,
    "get_fund_flow": get_index_fund_flow,
    "get_news": get_index_news,
    "get_industry_comparison": get_index_industry_comparison,
}


def _first_index_arg(args, kwargs) -> IndexSpec | None:
    """从调用的第一个参数（ticker 位置）识别指数标识；裸代码一律返回 None。"""
    for value in (list(args[:1]) + [kwargs.get("ticker")]):
        if isinstance(value, str):
            spec = parse_index_ticker(value)
            if spec is not None:
                return spec
    return None


def try_route_index(method: str, *args, **kwargs):
    """interface.route_to_vendor 的指数接缝：是指数调用就 (True, 结果)，否则 (False, None)。"""
    spec = _first_index_arg(args, kwargs)
    if spec is None:
        return False, None

    if method in _INDEX_METHODS:
        return True, _INDEX_METHODS[method](*args, **kwargs)
    if method in _STOCK_ONLY_METHODS:
        return True, _index_unsupported_msg(spec, _STOCK_ONLY_METHODS[method])
    return False, None


# ---------------------------------------------------------------------------
# 入口层解析（Web/CLI 在用户显式选择「指数」类型后调用）
# ---------------------------------------------------------------------------


def _tencent_index_quote(spec: IndexSpec) -> dict:
    """腾讯指数实时行情。指数没有 PE/市值/涨跌停，只解析点位/涨跌幅/高低。"""
    prefixed = f"{_index_prefix(spec)}{spec.code}"
    url = "https://qt.gtimg.cn/q=" + prefixed
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    resp = urllib.request.urlopen(req, timeout=10)
    raw = resp.read().decode("gbk")

    for line in raw.strip().split(";"):
        if not line.strip() or "=" not in line or '"' not in line:
            continue
        vals = line.split('"')[1].split("~")
        if len(vals) < 35:
            continue

        def _f(idx: int) -> float:
            try:
                return float(vals[idx]) if vals[idx] else 0.0
            except (ValueError, IndexError):
                return 0.0

        return {
            "name": vals[1],
            "price": _f(3),
            "last_close": _f(4),
            "open": _f(5),
            "change_pct": _f(32),
            "high": _f(33),
            "low": _f(34),
        }
    raise ValueError(f"Tencent index quote empty for {spec.ticker}")


def _probe_bare_index(code: str, exchange: str) -> IndexSpec:
    """实测探测裸 6 位代码是否为有效指数（依赖真实取数，不靠号段推断）。"""
    spec = make_index_spec(code, exchange)
    df = _mootdx_call("index_bars", symbol=code, frequency=9, offset=3)
    if df is None or df.empty:
        from .index_registry import known_indices

        known = "、".join(s.ticker for s in known_indices())
        raise ValueError(
            f"'{code}' 在{'沪' if exchange == 'SH' else '深'}市指数列表中探测不到"
            f" K 线数据，不是有效指数。已知宽基：{known}。"
            f"若想分析的是个股，请切回「个股」类型直接输入 6 位股票代码。"
        )
    try:
        name = (_tencent_index_quote(spec).get("name") or "").strip()
        # 腾讯对无效代码会返回「无这类数据」之类的占位名，不算真名
        if name and "无" not in name:
            return make_index_spec(code, exchange, name)
    except Exception:
        pass
    return spec


def resolve_index_input(user_input: str) -> IndexSpec:
    """入口层把用户原始输入解析为指数（中文名 / 带标注代码 / 裸码探测）。

    裸码歧义（000001 既可是上证指数也可按个股解析成平安银行）由调用方
    的「用户已选择指数类型」这一意图消解。
    """
    s = user_input.strip()
    if not s:
        raise ValueError("输入不能为空")

    spec = parse_index_ticker(s)
    if spec is not None:
        return spec

    spec = lookup_index_name(s)
    if spec is not None:
        return spec

    if s.isdigit() and len(s) == 6:
        exchange = index_market_for_bare_code(s)
        if exchange is None:
            raise ValueError(
                f"'{s}' 不在指数号段（沪市指数 000/880/999 开头、深市指数 399 "
                f"开头）。若想分析个股，请切回「个股」类型直接输入该 6 位股票代码。"
            )
        return _probe_bare_index(s, exchange)

    raise ValueError(
        f"'{user_input}' 无法识别为指数。支持：指数中文名（上证指数/沪深300/"
        f"创业板指/深证成指/科创50/中证500/中证1000）、带交易所标注的代码"
        f"（000001.SH / 399006.SZ）、或指数号段的 6 位代码。"
    )
