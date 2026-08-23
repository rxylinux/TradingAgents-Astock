"""Index registry: market indices (上证指数/沪深300/创业板指 ...) 的全链路标识。

指数与个股共用 6 位数字代码空间（上证指数 000001 与平安银行同码、科创50
000688 与国城矿业同码），而数据层历史上只认「裸 6 位 = 个股」——指数代码会被
静默路由到同号个股，拿到一份**看起来完全正常、实际全错**的数据，报告里毫无
痕迹（全链路五处：mootdx K 线 / 腾讯行情 / 东财 secid / 新浪财报 / yfinance
结算，000001 全部落到平安银行）。

本模块用**带交易所后缀的规范标识**（``000001.SH`` / ``399006.SZ``）显式区分：

- 裸 6 位代码语义**完全不变**，仍然是个股（``000001`` = 平安银行）——现有
  调用方与测试零影响；
- 满足以下任一条件的输入才被识别为指数：
  * 代码 + 显式交易所标注（``399006.SZ`` / ``sz399006`` 等），且落在指数
    号段：``399xxx``（深市/国证指数段）、``000xxx`` / ``880xxx`` / ``999xxx``
    （沪市指数段）。注意 ``600519.SH`` 这类个股后缀形式**不**在指数号段，
    仍走个股路径（后缀照旧被剥掉）；
  * 中文名命中下方注册表或别名表（「上证指数」「沪深300」「创业板指」…）。
- 规范标识贯穿记忆日志 / 结果目录 / checkpoint，与同号个股天然隔离。

数据源 ID 约定：

- 东财 push2 secid：沪市指数 ``1.000001``、深市指数 ``0.399006``（市场号
  1=沪 / 0=深，与个股同规则）；
- yfinance 符号：沪市指数 ``000001.SS``、深市指数 ``399001.SZ``。部分国证
  指数 Yahoo 无覆盖，结算时按北交所先例明确短路，不静默挂起。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 指数号段：决定「带交易所标注的代码」是否可能为指数。
# 深市 399xxx（深证/国证指数）；沪市 000xxx（上证/中证指数）、880xxx、999xxx。
_SZ_INDEX_PREFIXES = ("399",)
_SH_INDEX_PREFIXES = ("000", "880", "999")

# 6 位数字 + .SH/.SZ 后缀，或 SH/SZ 前缀（大小写不敏感）。
_INDEX_TICKER_RE = re.compile(
    r"^(?:SH(?P<shp>\d{6})|SZ(?P<szp>\d{6})|(?P<shs>\d{6})\.SH|(?P<szs>\d{6})\.SZ)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class IndexSpec:
    """一个市场指数的完整标识。

    ticker: 规范标识（``000001.SH``），全链路（记忆/目录/checkpoint）使用
    code:   6 位数字代码
    exchange: "SH" | "SZ"
    name:   中文名（未知指数探测后尽力回填，兜底 ``指数{code}``）
    secid:  东财 push2 secid（``1.000001`` / ``0.399006``）
    yahoo:  yfinance 符号（``000001.SS`` / ``399001.SZ``）
    """

    ticker: str
    code: str
    exchange: str
    name: str
    secid: str
    yahoo: str


def _make(code: str, exchange: str, name: str) -> IndexSpec:
    secid = f"{'1' if exchange == 'SH' else '0'}.{code}"
    yahoo = f"{code}{'.SS' if exchange == 'SH' else '.SZ'}"
    return IndexSpec(
        ticker=f"{code}.{exchange}", code=code, exchange=exchange,
        name=name, secid=secid, yahoo=yahoo,
    )


# 7 只主流宽基（人工核对过 secid / Yahoo 符号）。
_KNOWN: dict[str, IndexSpec] = {
    spec.ticker: spec
    for spec in (
        _make("000001", "SH", "上证指数"),
        _make("399001", "SZ", "深证成指"),
        _make("399006", "SZ", "创业板指"),
        _make("000300", "SH", "沪深300"),
        _make("000688", "SH", "科创50"),
        _make("000905", "SH", "中证500"),
        _make("000852", "SH", "中证1000"),
    )
}

# 中文名 / 常见别名 → 规范 ticker。
_NAME_ALIASES: dict[str, str] = {
    "上证指数": "000001.SH",
    "上证综指": "000001.SH",
    "上证": "000001.SH",
    "大盘": "000001.SH",
    "沪市": "000001.SH",
    "深证成指": "399001.SZ",
    "深成指": "399001.SZ",
    "深市": "399001.SZ",
    "创业板指": "399006.SZ",
    "创业板": "399006.SZ",
    "沪深300": "000300.SH",
    "沪深300指数": "000300.SH",
    "科创50": "000688.SH",
    "科创板50": "000688.SH",
    "中证500": "000905.SH",
    "中证1000": "000852.SH",
}


def is_index_symbol(symbol: str) -> bool:
    """输入是否为指数规范/前缀标识（不含中文名，也不含裸码）。"""
    return parse_index_ticker(symbol) is not None


def parse_index_ticker(symbol: str) -> IndexSpec | None:
    """把带交易所标注的代码解析为 IndexSpec；不是指数标识时返回 None。

    接受 ``000001.SH`` / ``sh000001`` 等形式（大小写不敏感）。代码必须落在
    指数号段（沪 000/880/999，深 399），否则返回 None 走个股路径——
    ``600519.SH`` 依然是贵州茅台。已知宽基直接命中注册表（含准确名称），
    号段内未知代码构造占位 spec（名称 ``指数{code}``，由入口层尽力回填）。
    """
    if not symbol:
        return None
    m = _INDEX_TICKER_RE.match(symbol.strip())
    if not m:
        return None
    if m.group("shp") is not None or m.group("shs") is not None:
        code, exchange = m.group("shp") or m.group("shs"), "SH"
    else:
        code, exchange = m.group("szp") or m.group("szs"), "SZ"
    if exchange == "SZ":
        if not code.startswith(_SZ_INDEX_PREFIXES):
            return None
    elif not code.startswith(_SH_INDEX_PREFIXES):
        return None
    ticker = f"{code}.{exchange}"
    return _KNOWN.get(ticker) or _make(code, exchange, f"指数{code}")


def lookup_index_name(name: str) -> IndexSpec | None:
    """中文名/别名 → IndexSpec（精确匹配，含「大盘→上证指数」类别名）。"""
    if not name:
        return None
    key = name.strip()
    ticker = _NAME_ALIASES.get(key)
    if ticker:
        return _KNOWN[ticker]
    for spec in _KNOWN.values():
        if spec.name == key:
            return spec
    return None


def index_market_for_bare_code(code: str) -> str | None:
    """裸 6 位代码若按指数解释，应属哪个市场；不在指数号段则 None。

    仅在入口层、用户已显式选择「指数」分析类型时使用——裸码本身歧义
    （000001 按个股是平安银行），不得用于 vendor 层猜测。
    """
    if code.startswith(_SZ_INDEX_PREFIXES):
        return "SZ"
    if code.startswith(_SH_INDEX_PREFIXES):
        return "SH"
    return None


def make_index_spec(code: str, exchange: str, name: str | None = None) -> IndexSpec:
    """构造任意指数的 IndexSpec（已知宽基返回注册表实例以复用准确名称）。"""
    ticker = f"{code}.{exchange}"
    if ticker in _KNOWN:
        return _KNOWN[ticker]
    return _make(code, exchange, name or f"指数{code}")


def known_indices() -> list[IndexSpec]:
    """7 只宽基清单（入口层提示/下拉用）。"""
    return list(_KNOWN.values())
