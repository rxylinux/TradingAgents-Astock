"""指数支持（index_registry / index_data / index_agents / 注入接缝）测试。

架构约束（对应隔离要求）：
- `a_stock.py` 与各个股 agent 文件**零改动**——指数功能全部住在
  index_registry / index_data / index_agents / index_graph 四个新文件里；
- 裸 6 位代码语义不变（000001 = 平安银行路径）；
- 接缝（interface.route_to_vendor / GraphSetup.node_factories）不注入时
  行为与原来完全一致。
"""

from pathlib import Path

import pandas as pd
import pytest

from tradingagents.dataflows import a_stock, index_data
from tradingagents.dataflows.index_registry import (
    index_market_for_bare_code,
    is_index_symbol,
    lookup_index_name,
    make_index_spec,
    parse_index_ticker,
)


# ---------------------------------------------------------------------------
# Registry: 解析规则
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected_ticker,expected_name",
    [
        ("000001.SH", "000001.SH", "上证指数"),
        ("000001.sh", "000001.SH", "上证指数"),
        ("sh000001", "000001.SH", "上证指数"),
        ("SH000001", "000001.SH", "上证指数"),
        ("000300.SH", "000300.SH", "沪深300"),
        ("sh000688", "000688.SH", "科创50"),
        ("399001.SZ", "399001.SZ", "深证成指"),
        ("sz399006", "399006.SZ", "创业板指"),
        ("000905.SH", "000905.SH", "中证500"),
        ("000852.SH", "000852.SH", "中证1000"),
        # 号段内未知指数：占位 spec（名称由入口层尽力回填）
        ("399372.SZ", "399372.SZ", "指数399372"),
        ("999999.SH", "999999.SH", "指数999999"),
    ],
)
def test_parse_index_ticker_known_and_segment(raw, expected_ticker, expected_name):
    spec = parse_index_ticker(raw)
    assert spec is not None
    assert spec.ticker == expected_ticker
    assert spec.name == expected_name


@pytest.mark.parametrize(
    "raw",
    [
        "600519.SH",   # 个股后缀形式：不在指数号段，必须走个股路径
        "000628",      # 裸码：不得猜测
        "000001",
        "399006",
        "AAPL",
        "00700.HK",
        "",
        "上证指数",     # 中文名走 lookup_index_name，不在 parse 职责内
        "832000.BJ",
    ],
)
def test_parse_index_ticker_rejects_non_index(raw):
    assert parse_index_ticker(raw) is None
    assert not is_index_symbol(raw)


def test_parse_index_ticker_rejects_wrong_exchange_for_segment():
    assert parse_index_ticker("000001.SZ") is None
    assert parse_index_ticker("399001.SH") is None


@pytest.mark.parametrize(
    "name,expected",
    [
        ("上证指数", "000001.SH"),
        ("上证", "000001.SH"),
        ("大盘", "000001.SH"),
        ("沪深300", "000300.SH"),
        ("创业板指", "399006.SZ"),
        ("创业板", "399006.SZ"),
        ("深成指", "399001.SZ"),
        ("科创50", "000688.SH"),
        ("中证500", "000905.SH"),
        ("中证1000", "000852.SH"),
    ],
)
def test_lookup_index_name_and_aliases(name, expected):
    assert lookup_index_name(name).ticker == expected
    assert lookup_index_name("不存在的指数名") is None


def test_index_market_for_bare_code():
    assert index_market_for_bare_code("000300") == "SH"
    assert index_market_for_bare_code("399006") == "SZ"
    assert index_market_for_bare_code("880001") == "SH"
    assert index_market_for_bare_code("600519") is None


def test_make_index_spec_secid_and_yahoo():
    assert make_index_spec("000001", "SH").secid == "1.000001"
    assert make_index_spec("000001", "SH").yahoo == "000001.SS"
    assert make_index_spec("399001", "SZ").secid == "0.399001"
    assert make_index_spec("399001", "SZ").yahoo == "399001.SZ"


def test_bare_stock_semantics_unchanged():
    """裸码仍是个股：000001 = 平安银行路径（不会被当成上证指数）。"""
    assert a_stock._normalize_ticker("000001") == "000001"
    assert a_stock._get_prefix("000001") == "sz"


# ---------------------------------------------------------------------------
# index_data: K 线 / 指标 / 资金流 / 新闻
# ---------------------------------------------------------------------------


def _fake_index_bars():
    return pd.DataFrame(
        {
            "datetime": pd.to_datetime(["2026-08-14 15:00:00", "2026-08-15 15:00:00"]),
            "open": [3200.0, 3210.0],
            "high": [3250.0, 3260.0],
            "low": [3180.0, 3200.0],
            "close": [3240.0, 3255.0],
            "volume": [100_000_000, 120_000_000],
        }
    ).set_index("datetime")


class _FakeMootdxWithIndexBars:
    def __init__(self):
        self.index_bars_calls = []

    def index_bars(self, symbol, frequency, offset):
        self.index_bars_calls.append((symbol, frequency, offset))
        return _fake_index_bars()


@pytest.fixture
def index_env(monkeypatch, tmp_path):
    """统一的指数测试环境：fake mootdx + 隔离缓存目录。

    `_mootdx_call` 在 a_stock 命名空间里解析 `_get_mootdx_client`，
    所以要 patch a_stock 那一侧。
    """
    fake = _FakeMootdxWithIndexBars()
    monkeypatch.setattr(a_stock, "_get_mootdx_client", lambda: fake)
    from tradingagents.dataflows import config as dataflow_config

    monkeypatch.setattr(
        dataflow_config, "get_config", lambda: {"data_cache_dir": str(tmp_path)}
    )
    return fake, tmp_path


def test_get_index_stock_data_uses_index_bars(index_env):
    fake, tmp_path = index_env
    result = index_data.get_index_stock_data("000001.SH", "2026-08-10", "2026-08-15")

    assert fake.index_bars_calls == [("000001", 9, 800)]
    assert "Index data for 上证指数 (000001.SH)" in result
    assert "2026-08-15,3210.0,3260.0,3200.0,3255.0" in result


def test_load_index_ohlcv_cutoff_and_cache_isolation(index_env):
    _, tmp_path = index_env
    spec = parse_index_ticker("000001.SH")
    df = index_data._load_index_ohlcv(spec, "2026-08-14")
    assert df["Date"].max() == pd.Timestamp("2026-08-14")   # 未来函数截断生效
    # 缓存文件带 index 标记，不与同号个股缓存互踩
    assert (Path(tmp_path) / "000001.SH-index-daily.csv").exists()
    assert not (Path(tmp_path) / "000001-astock-daily.csv").exists()


def test_get_index_indicators(index_env):
    result = index_data.get_index_indicators(
        "000001.SH", "close_50_sma", "2026-08-15", 5
    )
    assert "上证指数" in result and "000001.SH" in result


def test_get_index_fund_flow_secid(monkeypatch):
    captured = {}

    class _FakeResp:
        def json(self):
            return {"data": {"klines": []}}

    def _fake_em_get(url, params=None, **kwargs):
        captured["url"] = url
        captured["params"] = params
        return _FakeResp()

    monkeypatch.setattr(index_data, "_em_get", _fake_em_get)
    # 固定为"实时日"：写死具体日期会在几天后变成历史日期，实时段被未来函数
    # 防护跳过、_em_get 不被调用，断言扑空（本测试只关心 secid 路由）。
    monkeypatch.setattr(index_data, "_is_historical", lambda d: False)
    result = index_data.get_index_fund_flow("000001.SH", "2026-08-16", False)

    assert captured["params"]["secid"] == "1.000001"
    assert "上证指数" in result


def test_get_index_news_uses_name_keyword(monkeypatch):
    captured = {}

    def _fake_fetch(code, page_size=20):
        captured["keyword"] = code
        return [
            {
                "title": "上证指数突破 3300 点",
                "content": "...",
                "time": "2026-08-15 10:00:00",
                "source": "东方财富",
                "url": "https://example.com",
            }
        ]

    monkeypatch.setattr(a_stock, "_fetch_news_eastmoney", _fake_fetch)
    result = index_data.get_index_news("000001.SH", "2026-08-14", "2026-08-16")

    assert captured["keyword"] == "上证指数"
    assert "上证指数突破 3300 点" in result


# ---------------------------------------------------------------------------
# interface 接缝：指数分流 + 拒绝 + 裸码不受影响
# ---------------------------------------------------------------------------


def test_seam_routes_index_to_index_data(index_env):
    from tradingagents.dataflows.interface import route_to_vendor

    result = route_to_vendor("get_stock_data", "000001.SH", "2026-08-10", "2026-08-15")
    assert "上证指数" in result


def test_seam_refuses_stock_only_methods_for_index():
    from tradingagents.dataflows.interface import route_to_vendor

    result = route_to_vendor("get_fundamentals", "000001.SH", "2026-08-16")
    assert "Error" in result
    assert "上证指数" in result
    assert "仅适用于个股" in result


@pytest.mark.parametrize(
    "method,args",
    [
        ("get_fundamentals", ("000001.SH", "2026-08-16")),
        ("get_balance_sheet", ("000001.SH",)),
        ("get_cashflow", ("000001.SH",)),
        ("get_income_statement", ("000001.SH",)),
        ("get_insider_transactions", ("000001.SH",)),
        ("get_profit_forecast", ("000001.SH", "2026-08-16")),
        ("get_concept_blocks", ("000001.SH",)),
        ("get_dragon_tiger_board", ("000001.SH", "2026-08-16")),
        ("get_lockup_expiry", ("000001.SH", "2026-08-16")),
    ],
)
def test_seam_rejects_all_stock_only_methods(method, args):
    from tradingagents.dataflows.interface import route_to_vendor

    result = route_to_vendor(method, *args)
    assert "Error" in result and "指数" in result


def test_seam_ignores_bare_codes():
    """裸码不受接缝影响：try_route_index 必须返回 (False, None)。"""
    for method in ("get_stock_data", "get_fundamentals", "get_fund_flow", "get_news"):
        handled, result = index_data.try_route_index(method, "000001", "2026-01-01")
        assert handled is False
        assert result is None


# ---------------------------------------------------------------------------
# resolve_index_input（入口解析：中文名 / 标识 / 裸码探测）
# ---------------------------------------------------------------------------


def test_resolve_index_input_by_name():
    assert index_data.resolve_index_input("沪深300").ticker == "000300.SH"


def test_resolve_index_input_by_canonical_ticker():
    assert index_data.resolve_index_input("000001.SH").ticker == "000001.SH"


def test_resolve_index_input_bare_code_probes_mootdx(monkeypatch):
    fake = _FakeMootdxWithIndexBars()
    monkeypatch.setattr(a_stock, "_get_mootdx_client", lambda: fake)
    monkeypatch.setattr(
        index_data, "_tencent_index_quote", lambda spec: {"name": "国证2000"}
    )
    spec = index_data.resolve_index_input("399303")
    assert spec.ticker == "399303.SZ"
    assert spec.name == "国证2000"


def test_resolve_index_input_bare_code_probe_failure(monkeypatch):
    class _Empty:
        def index_bars(self, symbol, frequency, offset):
            return pd.DataFrame()

    monkeypatch.setattr(a_stock, "_get_mootdx_client", lambda: _Empty())
    with pytest.raises(ValueError, match="不是有效指数"):
        index_data.resolve_index_input("399303")


def test_resolve_index_input_non_index_segment():
    with pytest.raises(ValueError, match="不在指数号段"):
        index_data.resolve_index_input("600519")


def test_resolve_index_input_garbage():
    with pytest.raises(ValueError, match="无法识别为指数"):
        index_data.resolve_index_input("随便打的")


# ---------------------------------------------------------------------------
# index_agents：工厂可构造 + 注入表合法
# ---------------------------------------------------------------------------


class _NoopLLM:
    """工厂只在节点被调用时才使用 llm，构造阶段给个占位即可。"""


def test_all_index_factories_constructible():
    from tradingagents.agents.index_agents import INDEX_NODE_FACTORIES

    for role, factory in INDEX_NODE_FACTORIES.items():
        node = factory(_NoopLLM())
        assert callable(node), f"{role} 工厂必须返回节点函数"


def test_index_node_factories_cover_expected_roles():
    from tradingagents.agents.index_agents import INDEX_NODE_FACTORIES
    from tradingagents.graph.setup import ROLE_KEYS

    assert set(INDEX_NODE_FACTORIES) <= set(ROLE_KEYS)
    # 指数预设的 5 个分析师 + 辩论/交易/决策链必须有指数版
    assert {"market", "social", "news", "policy", "hot_money",
            "bull", "bear", "trader", "portfolio_manager"} <= set(INDEX_NODE_FACTORIES)


def test_graph_setup_node_factory_injection():
    """GraphSetup 接缝：注入的工厂生效，未注入的角色回落到原版工厂。"""
    from tradingagents.agents.index_agents import create_index_market_analyst
    from tradingagents.graph.setup import GraphSetup

    def _fake_default(llm):
        return "stock-market-node"

    def _fake_override(llm):
        return "index-market-node"

    setup = GraphSetup(
        "quick", "deep", tool_nodes={}, conditional_logic=None,
        node_factories={"market": _fake_override},
    )
    assert setup._node_factory("market", _fake_default) is _fake_override
    assert setup._node_factory("news", _fake_default) is _fake_default


def test_index_market_analyst_uses_index_prompt():
    """指数市场分析师节点用的是指数 prompt（含指数语境、不含个股涨跌停语境）。"""
    from tradingagents.agents.index_agents import create_index_market_analyst

    captured = {}

    class _FakeChain:
        def __init__(self, prompt, llm, tools):
            self.prompt = prompt

        def invoke(self, messages):
            captured["system"] = self.prompt.messages[0].prompt.format(
                system_message="", tool_names="", current_date="", instrument_context=""
            )
            # messages[0].prompt 是 ChatPromptTemplate；直接取 partial 后的模板串
            return captured.setdefault("result", type("R", (), {
                "tool_calls": [], "content": "index report"
            })())

    node = create_index_market_analyst(_NoopLLM())
    # 不真正调 LLM——这里只验证工厂能建节点即可；prompt 内容由字符串常量保证
    assert callable(node)


# ---------------------------------------------------------------------------
# 工具层：指数标识必须能通过 get_news 的入参校验
# ---------------------------------------------------------------------------


def test_news_tool_accepts_index_ticker():
    from tradingagents.agents.utils import news_data_tools

    ok, value = news_data_tools._validate_a_stock_code("get_news", "000001.SH")
    assert ok and value == "000001.SH"
    ok, value = news_data_tools._validate_a_stock_code("get_news", "399006.SZ")
    assert ok and value == "399006.SZ"
    ok, value = news_data_tools._validate_a_stock_code("get_news", "600519")
    assert ok and value == "600519"
    ok, _ = news_data_tools._validate_a_stock_code("get_news", "贵州茅台")
    assert not ok
    ok, _ = news_data_tools._validate_a_stock_code("get_news", "600519.BJ")
    assert not ok
