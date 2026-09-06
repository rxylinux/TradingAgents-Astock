"""Tests for analyst parallel execution topology, state aggregation, and checkpoint resume.

Covers:
1. 7 Analysts full selection topology (START fan-out -> 7 analysts, all Msg Clear fan-in -> Quality Gate).
2. Analyst subsets topology (1 analyst, 3 analysts, arbitrary subsets).
3. TradingAgentsIndexGraph 5-analyst parallel topology.
4. Parallel graph execution simulation & state aggregation at Quality Gate.
5. Checkpoint / resume compatibility under parallel graph topology.
6. R1: per-branch message/tool-call isolation in parallel tool loops (real ToolNode execution).
7. R2: Quality Gate join barrier — it must not run until every selected analyst finished.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from tradingagents.agents import create_msg_delete
from tradingagents.agents.index_agents import INDEX_NODE_FACTORIES
from tradingagents.agents.quality_gate import ANALYST_NAMES, REPORT_FIELDS, create_quality_gate
from tradingagents.agents.utils.agent_states import AgentState
from tradingagents.graph.checkpointer import (
    checkpoint_step,
    clear_checkpoint,
    get_checkpointer,
    has_checkpoint,
    thread_id,
)
from tradingagents.graph.conditional_logic import ConditionalLogic
from tradingagents.graph.index_graph import INDEX_ANALYSTS, TradingAgentsIndexGraph
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.setup import GraphSetup
from tradingagents.graph.trading_graph import TradingAgentsGraph


ALL_7_ANALYSTS = [
    "market",
    "social",
    "news",
    "fundamentals",
    "policy",
    "hot_money",
    "lockup",
]


class MockLLM:
    """Mock LLM that returns pre-configured responses."""

    def __init__(self, content: str = "mocked response"):
        self.content = content

    def invoke(self, *args, **kwargs):
        return AIMessage(content=self.content)

    def bind_tools(self, tools, **kwargs):
        return self


class MockLLMClient:
    """Mock LLM Client for TradingAgentsGraph initialization."""

    def __init__(self, **kwargs):
        self.llm = MockLLM()

    def get_llm(self):
        return self.llm


def make_graph_setup(
    selected_analysts: Optional[List[str]] = None,
    node_factories: Optional[Dict[str, Any]] = None,
    resolve_llm=None,
) -> tuple[GraphSetup, StateGraph]:
    """Helper to instantiate GraphSetup and build workflow without live APIs."""
    analysts = selected_analysts if selected_analysts is not None else ALL_7_ANALYSTS
    tool_nodes = {k: MagicMock() for k in ALL_7_ANALYSTS}
    cond_logic = ConditionalLogic(max_debate_rounds=1, max_risk_discuss_rounds=1)
    setup = GraphSetup(
        quick_thinking_llm=MockLLM("quick"),
        deep_thinking_llm=MockLLM("deep"),
        tool_nodes=tool_nodes,
        conditional_logic=cond_logic,
        resolve_llm=resolve_llm,
        node_factories=node_factories,
    )
    workflow = setup.setup_graph(analysts)
    return setup, workflow


# ===========================================================================
# 1. 7 位分析师全选拓扑结构测试
# ===========================================================================


class TestSevenAnalystsTopology:
    """验证 7 位分析师全部选中时的并行拓扑结构：

    - START 直接分支到所有 7 个分析师节点（fan-out）；
    - 所有 7 个 Msg Clear 节点均直接汇聚到 Quality Gate（fan-in）；
    - Quality Gate 连接到 Bull Researcher；
    - 分析师之间无串行依赖；
    - 各分析师的 tool loop 与下游辩论/风控拓扑保持完整。
    """

    def test_start_branches_to_all_seven_analysts(self):
        _, workflow = make_graph_setup(ALL_7_ANALYSTS)
        compiled = workflow.compile()
        graph_edges = [(e.source, e.target) for e in compiled.get_graph().edges]

        # 找到所有从 START 出发的边
        start_targets = {target for src, target in graph_edges if src == "__start__"}

        expected_analysts = {f"{a.capitalize()} Analyst" for a in ALL_7_ANALYSTS}
        assert start_targets == expected_analysts, (
            f"START 必须且仅分支到所有 7 个分析师节点，实际分支到: {start_targets}"
        )

    def test_all_seven_msg_clear_converge_to_quality_gate(self):
        _, workflow = make_graph_setup(ALL_7_ANALYSTS)
        compiled = workflow.compile()
        graph_edges = [(e.source, e.target) for e in compiled.get_graph().edges]

        # 找到所有汇聚到 Quality Gate 的来源节点
        qg_sources = {src for src, target in graph_edges if target == "Quality Gate"}

        expected_clear_nodes = {f"Msg Clear {a.capitalize()}" for a in ALL_7_ANALYSTS}
        assert qg_sources == expected_clear_nodes, (
            f"Quality Gate 的入边必须来自且仅来自所有 7 个 Msg Clear 节点，实际为: {qg_sources}"
        )

    def test_quality_gate_connects_to_bull_researcher(self):
        _, workflow = make_graph_setup(ALL_7_ANALYSTS)
        compiled = workflow.compile()
        graph_edges = [(e.source, e.target) for e in compiled.get_graph().edges]

        qg_targets = {target for src, target in graph_edges if src == "Quality Gate"}
        assert qg_targets == {"Bull Researcher"}, (
            f"Quality Gate 的出边必须直接指向 Bull Researcher，实际指向: {qg_targets}"
        )

    def test_no_sequential_edges_between_analysts(self):
        """分析师之间不能有串行边（例如 Msg Clear Market -> Social Analyst）。"""
        _, workflow = make_graph_setup(ALL_7_ANALYSTS)
        compiled = workflow.compile()
        graph_edges = [(e.source, e.target) for e in compiled.get_graph().edges]

        analyst_nodes = {f"{a.capitalize()} Analyst" for a in ALL_7_ANALYSTS}
        clear_nodes = {f"Msg Clear {a.capitalize()}" for a in ALL_7_ANALYSTS}

        for src, target in graph_edges:
            if src in clear_nodes:
                assert target not in analyst_nodes, (
                    f"发现串行连接: {src} -> {target}，分析师必须并行执行而不是串行链接"
                )

    def test_analyst_tool_bidirectional_loops(self):
        """每个分析师节点和对应 tools 节点之间存在双向回路。"""
        _, workflow = make_graph_setup(ALL_7_ANALYSTS)
        compiled = workflow.compile()
        graph_edges = set((e.source, e.target) for e in compiled.get_graph().edges)

        for a in ALL_7_ANALYSTS:
            analyst = f"{a.capitalize()} Analyst"
            tools = f"tools_{a}"
            clear = f"Msg Clear {a.capitalize()}"

            # tools -> analyst 边
            assert (tools, analyst) in graph_edges, f"{tools} 必须有回边指向 {analyst}"
            # analyst -> tools 条件分支
            assert (analyst, tools) in graph_edges, f"{analyst} 必须能流转到 {tools}"
            # analyst -> clear 条件分支
            assert (analyst, clear) in graph_edges, f"{analyst} 必须能流转到 {clear}"

    def test_downstream_debate_and_risk_topology_intact(self):
        """多空辩论、Trader、风险评估三方及 PM 的拓扑连接保持完整。"""
        _, workflow = make_graph_setup(ALL_7_ANALYSTS)
        compiled = workflow.compile()
        graph_edges = set((e.source, e.target) for e in compiled.get_graph().edges)

        # Quality Gate -> Bull Researcher
        assert ("Quality Gate", "Bull Researcher") in graph_edges
        # Research Manager -> Trader
        assert ("Research Manager", "Trader") in graph_edges
        # Trader -> Aggressive Analyst
        assert ("Trader", "Aggressive Analyst") in graph_edges
        # Portfolio Manager -> END
        assert ("Portfolio Manager", "__end__") in graph_edges


# ===========================================================================
# 2. 分析师子集（如 1 个、3 个分析师）并行拓扑测试
# ===========================================================================


class TestAnalystSubsetsTopology:
    """验证各类分析师子集的并行拓扑有效性。"""

    @pytest.mark.parametrize("single_analyst", ALL_7_ANALYSTS)
    def test_single_analyst_parallel_topology(self, single_analyst: str):
        """单分析师子集：START -> 唯一分析师 -> Msg Clear -> Quality Gate -> Bull Researcher。"""
        _, workflow = make_graph_setup([single_analyst])
        compiled = workflow.compile()
        graph_edges = [(e.source, e.target) for e in compiled.get_graph().edges]

        start_targets = {target for src, target in graph_edges if src == "__start__"}
        assert start_targets == {f"{single_analyst.capitalize()} Analyst"}

        qg_sources = {src for src, target in graph_edges if target == "Quality Gate"}
        assert qg_sources == {f"Msg Clear {single_analyst.capitalize()}"}

        # 确保其他未选分析师节点不在图中
        all_nodes = set(compiled.get_graph().nodes.keys())
        for other in ALL_7_ANALYSTS:
            if other != single_analyst:
                assert f"{other.capitalize()} Analyst" not in all_nodes
                assert f"Msg Clear {other.capitalize()}" not in all_nodes
                assert f"tools_{other}" not in all_nodes

    def test_three_analysts_topology(self):
        """3 个分析师子集 (market, news, policy)：START 分支到 3 者，Msg Clear 汇聚到 Quality Gate。"""
        subset = ["market", "news", "policy"]
        _, workflow = make_graph_setup(subset)
        compiled = workflow.compile()
        graph_edges = [(e.source, e.target) for e in compiled.get_graph().edges]

        start_targets = {target for src, target in graph_edges if src == "__start__"}
        assert start_targets == {"Market Analyst", "News Analyst", "Policy Analyst"}

        qg_sources = {src for src, target in graph_edges if target == "Quality Gate"}
        assert qg_sources == {"Msg Clear Market", "Msg Clear News", "Msg Clear Policy"}

        # 验证未选中的 4 个分析师未在图中构建
        all_nodes = set(compiled.get_graph().nodes.keys())
        for unselected in ["social", "fundamentals", "hot_money", "lockup"]:
            assert f"{unselected.capitalize()} Analyst" not in all_nodes
            assert f"Msg Clear {unselected.capitalize()}" not in all_nodes

    def test_two_astock_specific_analysts_topology(self):
        """2 个 A股专有分析师子集 (hot_money, lockup)。"""
        subset = ["hot_money", "lockup"]
        _, workflow = make_graph_setup(subset)
        compiled = workflow.compile()
        graph_edges = [(e.source, e.target) for e in compiled.get_graph().edges]

        start_targets = {target for src, target in graph_edges if src == "__start__"}
        assert start_targets == {"Hot_money Analyst", "Lockup Analyst"}

        qg_sources = {src for src, target in graph_edges if target == "Quality Gate"}
        assert qg_sources == {"Msg Clear Hot_money", "Msg Clear Lockup"}

    def test_empty_analyst_selection_raises_error(self):
        """未选中任何分析师时必须抛出明确的 ValueError。"""
        tool_nodes = {k: MagicMock() for k in ALL_7_ANALYSTS}
        cond_logic = ConditionalLogic(1, 1)
        setup = GraphSetup(MockLLM(), MockLLM(), tool_nodes, cond_logic)
        with pytest.raises(ValueError, match="no analysts selected"):
            setup.setup_graph([])


# ===========================================================================
# 3. 指数分析图 (TradingAgentsIndexGraph) 5 分析师并行拓扑测试
# ===========================================================================


class TestIndexGraphTopology:
    """验证 TradingAgentsIndexGraph 的 5 分析师并行拓扑有效性。"""

    def test_default_index_graph_has_five_parallel_analysts(self):
        """默认指数图包含 5 位分析师（剔除 fundamentals / lockup），并行拓扑正确。"""
        with patch("tradingagents.graph.trading_graph.create_llm_client", return_value=MockLLMClient()):
            index_graph = TradingAgentsIndexGraph()

        edges = [(e.source, e.target) for e in index_graph.graph.get_graph().edges]
        nodes = set(index_graph.graph.get_graph().nodes.keys())

        # 1. 验证 START 分支到 5 个指数分析师
        start_targets = {target for src, target in edges if src == "__start__"}
        expected_analysts = {f"{a.capitalize()} Analyst" for a in INDEX_ANALYSTS}
        assert start_targets == expected_analysts
        assert len(start_targets) == 5

        # 2. 验证 5 个 Msg Clear 均汇聚到 Quality Gate
        qg_sources = {src for src, target in edges if target == "Quality Gate"}
        expected_clears = {f"Msg Clear {a.capitalize()}" for a in INDEX_ANALYSTS}
        assert qg_sources == expected_clears

        # 3. 验证个股专属分析师（fundamentals / lockup）不在指数图中
        assert "Fundamentals Analyst" not in nodes
        assert "Msg Clear Fundamentals" not in nodes
        assert "Lockup Analyst" not in nodes
        assert "Msg Clear Lockup" not in nodes

        # 4. 验证 Quality Gate -> Bull Researcher 下游链路完整
        assert ("Quality Gate", "Bull Researcher") in edges

    def test_index_graph_custom_analyst_subset(self):
        """指数图也可以自定义分析师子集，依然保持并行拓扑。"""
        custom_subset = ["market", "policy"]
        with patch("tradingagents.graph.trading_graph.create_llm_client", return_value=MockLLMClient()):
            index_graph = TradingAgentsIndexGraph(selected_analysts=custom_subset)

        edges = [(e.source, e.target) for e in index_graph.graph.get_graph().edges]
        start_targets = {target for src, target in edges if src == "__start__"}
        assert start_targets == {"Market Analyst", "Policy Analyst"}

        qg_sources = {src for src, target in edges if target == "Quality Gate"}
        assert qg_sources == {"Msg Clear Market", "Msg Clear Policy"}

    def test_index_graph_uses_injected_index_factories(self):
        """指数图必须注入 INDEX_NODE_FACTORIES。"""
        with patch("tradingagents.graph.trading_graph.create_llm_client", return_value=MockLLMClient()):
            index_graph = TradingAgentsIndexGraph()

        for role, factory in INDEX_NODE_FACTORIES.items():
            assert index_graph.graph_setup._node_factories.get(role) is factory


# ===========================================================================
# 4. 模拟执行并行图并验证状态聚合
# ===========================================================================


class TestParallelStateAggregation:
    """模拟执行并行图，验证所有分析师生成的独立 report 字段均能正确汇聚到 Quality Gate。"""

    def test_seven_analysts_report_aggregation_at_quality_gate(self):
        """7 位分析师并行执行后，Quality Gate 能够完整读取所有 7 个独立的 report 字段。"""
        captured_qg_input: dict[str, Any] = {}

        def make_analyst_node(field_name: str, report_text: str):
            def _node(state: dict) -> dict:
                return {field_name: report_text}
            return _node

        def mock_clear_node(state: dict) -> dict:
            return {}

        def mock_quality_gate(state: dict) -> dict:
            # 捕获进入 Quality Gate 时的 state
            captured_qg_input.update({k: state.get(k) for k in REPORT_FIELDS.values()})
            captured_qg_input["company_of_interest"] = state.get("company_of_interest")
            captured_qg_input["trade_date"] = state.get("trade_date")

            # 组装汇聚摘要
            collected = [f"{k}={state.get(v)}" for k, v in REPORT_FIELDS.items()]
            return {"data_quality_summary": "AGGREGATED: " + "; ".join(collected)}

        # 构造轻量 StateGraph
        builder = StateGraph(AgentState)

        sample_reports = {
            "market_report": "【技术分析报告】均线呈多头排列，MACD金叉，RSI指标健康，建议积极关注。\n| 指标 | 数值 |\n|---|---|\n| MA20 | 1850 |",
            "sentiment_report": "【情绪分析报告】社交平台讨论热度上升，多头情绪占比 75%，市场情绪乐观。\n| 情绪 | 得分 |\n|---|---|\n| 综合 | 80 |",
            "news_report": "【新闻舆情报告】行业利好政策密集发布，最新季度业绩指引超预期，无重大负面报道。\n| 新闻 | 评级 |\n|---|---|\n| 行业 | 积极 |",
            "fundamentals_report": "【基本面报告】营收同比增长 18%，净利润稳定增长，ROE 处于行业领先水平。\n| 财务 | 指标 |\n|---|---|\n| ROE | 22% |",
            "policy_report": "【政策分析报告】产业扶持规划落地，税收优惠细则出台，属于政策鼓励支持方向。\n| 政策 | 影响 |\n|---|---|\n| 规划 | 正向 |",
            "hot_money_report": "【游资追踪报告】龙虎榜机构净买入超 2 亿元，知名游资席位现身买一，主力资金大幅净流入。\n| 席位 | 净买入 |\n|---|---|\n| 机构 | +2.1亿 |",
            "lockup_report": "【解禁监控报告】未来 6 个月内无大额限售股解禁，近期亦无重要股东减持计划公告。\n| 事项 | 说明 |\n|---|---|\n| 解禁 | 无风险 |",
        }

        for analyst_type, report_field in REPORT_FIELDS.items():
            analyst_name = f"{analyst_type.capitalize()} Analyst"
            clear_name = f"Msg Clear {analyst_type.capitalize()}"

            builder.add_node(analyst_name, make_analyst_node(report_field, sample_reports[report_field]))
            builder.add_node(clear_name, mock_clear_node)

            builder.add_edge(START, analyst_name)
            builder.add_edge(analyst_name, clear_name)
            builder.add_edge(clear_name, "Quality Gate")

        builder.add_node("Quality Gate", mock_quality_gate)
        builder.add_node("Bull Researcher", lambda s: {"investment_plan": "Bullish based on all reports"})
        builder.add_edge("Quality Gate", "Bull Researcher")
        builder.add_edge("Bull Researcher", END)

        compiled_graph = builder.compile()

        init_state = {
            "company_of_interest": "600519",
            "trade_date": "2026-04-20",
            "messages": [HumanMessage(content="Start Analysis")],
        }

        final_result = compiled_graph.invoke(init_state)

        # 1. 验证 Quality Gate 捕获到了所有 7 个 report 字段
        for field in REPORT_FIELDS.values():
            assert captured_qg_input[field] == sample_reports[field], (
                f"Quality Gate 未正确接收到 {field}"
            )

        # 2. 验证 Quality Gate 生成的 data_quality_summary 正确写入 state
        assert "data_quality_summary" in final_result
        assert "AGGREGATED" in final_result["data_quality_summary"]

        # 3. 验证下游节点 Bull Researcher 也能获取到最终状态
        assert final_result["investment_plan"] == "Bullish based on all reports"
        for field in REPORT_FIELDS.values():
            assert final_result[field] == sample_reports[field]

    def test_subset_analysts_state_aggregation(self):
        """测试 3 位分析师 (market, news, policy) 并行执行时的状态汇聚。"""
        captured_reports: dict[str, Any] = {}

        builder = StateGraph(AgentState)
        subset = ["market", "news", "policy"]

        reports_data = {
            "market_report": "Market Report Content",
            "news_report": "News Report Content",
            "policy_report": "Policy Report Content",
        }

        for a in subset:
            field = REPORT_FIELDS[a]
            builder.add_node(f"{a.capitalize()} Analyst", lambda s, f=field, r=reports_data[field]: {f: r})
            builder.add_node(f"Msg Clear {a.capitalize()}", lambda s: {})
            builder.add_edge(START, f"{a.capitalize()} Analyst")
            builder.add_edge(f"{a.capitalize()} Analyst", f"Msg Clear {a.capitalize()}")
            builder.add_edge(f"Msg Clear {a.capitalize()}", "Quality Gate")

        def qg_node(state):
            captured_reports["market"] = state.get("market_report")
            captured_reports["news"] = state.get("news_report")
            captured_reports["policy"] = state.get("policy_report")
            captured_reports["social"] = state.get("sentiment_report")
            return {"data_quality_summary": "QG done"}

        builder.add_node("Quality Gate", qg_node)
        builder.add_edge("Quality Gate", END)

        graph = builder.compile()
        res = graph.invoke({"company_of_interest": "000001", "trade_date": "2026-04-20", "messages": []})

        assert captured_reports["market"] == "Market Report Content"
        assert captured_reports["news"] == "News Report Content"
        assert captured_reports["policy"] == "Policy Report Content"
        assert captured_reports["social"] is None or captured_reports["social"] == ""
        assert res["data_quality_summary"] == "QG done"

    def test_real_quality_gate_factory_with_parallel_reports(self):
        """使用实际的 create_quality_gate 工厂函数，验证其在并行状态汇聚后的硬检查与LLM复审。"""
        fake_llm = MockLLM("## 数据质量审核报告\n\n**整体评级**: A\n**数据可信度**: 高")
        qg_node = create_quality_gate(fake_llm)

        # 模拟 7 个分析师生成的完整报告（均包含表格与足够长度 > 200 chars）
        valid_report = "【分析报告】" + "详细内容" * 60 + "\n\n| 指标 | 结果 |\n|---|---|\n| 状态 | 正常 |"
        state = {
            "company_of_interest": "600519",
            "trade_date": "2026-04-20",
            "market_report": valid_report,
            "sentiment_report": valid_report,
            "news_report": valid_report,
            "fundamentals_report": valid_report,
            "policy_report": valid_report,
            "hot_money_report": valid_report,
            "lockup_report": valid_report,
        }

        result = qg_node(state)
        assert "data_quality_summary" in result
        summary = result["data_quality_summary"]

        # 验证所有 7 位分析师均在硬检查结果中
        for name in ANALYST_NAMES.values():
            assert name in summary, f"质量门控摘要应包含 {name}"

        assert "600519" in summary
        assert "2026-04-20" in summary
        assert "数据质量审核报告" in summary


# ===========================================================================
# 5. 断点续跑 (checkpoint / resume) 在并行图下的兼容性测试
# ===========================================================================


class TestCheckpointResumeParallelism:
    """验证 checkpoint / resume 在并行图拓扑下的兼容性。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.ticker = "600519"
        self.date = "2026-04-20"

    def test_crash_at_quality_gate_and_resume_with_parallel_reports(self):
        """场景：7 位分析师并行执行完毕后，在 Quality Gate 节点发生崩溃；

        续跑时从 Quality Gate 继续执行，所有 7 位分析师已缓存的 report 字段无损保留。
        """
        tmpdir = tempfile.mkdtemp()
        ticker = "600519"
        date = "2026-04-20"
        tid = thread_id(ticker, date)
        cfg = {"configurable": {"thread_id": tid}}

        should_crash = True

        def make_analyst_node(field_name: str, val: str):
            return lambda state: {field_name: val}

        def quality_gate_node(state: dict) -> dict:
            if should_crash:
                raise RuntimeError("simulated crash at Quality Gate")
            return {"data_quality_summary": "QG passed"}

        def bull_node(state: dict) -> dict:
            return {"investment_plan": "Bullish investment plan"}

        builder = StateGraph(AgentState)
        for a in ALL_7_ANALYSTS:
            field = REPORT_FIELDS[a]
            builder.add_node(f"{a.capitalize()} Analyst", make_analyst_node(field, f"report_{a}"))
            builder.add_node(f"Msg Clear {a.capitalize()}", lambda s: {})
            builder.add_edge(START, f"{a.capitalize()} Analyst")
            builder.add_edge(f"{a.capitalize()} Analyst", f"Msg Clear {a.capitalize()}")
            builder.add_edge(f"Msg Clear {a.capitalize()}", "Quality Gate")

        builder.add_node("Quality Gate", quality_gate_node)
        builder.add_node("Bull Researcher", bull_node)
        builder.add_edge("Quality Gate", "Bull Researcher")
        builder.add_edge("Bull Researcher", END)

        # 第 1 次运行：在 Quality Gate 节点崩溃
        should_crash = True
        with get_checkpointer(tmpdir, ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            with pytest.raises(RuntimeError, match="simulated crash at Quality Gate"):
                graph.invoke(
                    {
                        "company_of_interest": ticker,
                        "trade_date": date,
                        "messages": [],
                    },
                    config=cfg,
                )

        # 验证产生了 checkpoint
        assert has_checkpoint(tmpdir, ticker, date) is True
        step = checkpoint_step(tmpdir, ticker, date)
        assert step is not None and step >= 1

        # 第 2 次运行：修复后断点续跑 (input=None)
        should_crash = False
        with get_checkpointer(tmpdir, ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            resumed_result = graph.invoke(None, config=cfg)

        # 验证所有 7 个并行分析师的报告均被正确保留，且下游节点成功完成
        for a in ALL_7_ANALYSTS:
            field = REPORT_FIELDS[a]
            assert resumed_result.get(field) == f"report_{a}", f"续跑后 {field} 丢失"

        assert resumed_result["data_quality_summary"] == "QG passed"
        assert resumed_result["investment_plan"] == "Bullish investment plan"

    def test_crash_at_downstream_trader_and_resume(self):
        """场景：Quality Gate 之后在 Trader 节点崩溃，续跑依然保留全部 7 个分析师报告与 QG 结果。"""
        tmpdir = tempfile.mkdtemp()
        ticker = "000858"
        date = "2026-04-20"
        tid = thread_id(ticker, date)
        cfg = {"configurable": {"thread_id": tid}}

        should_crash = True

        def trader_node(state: dict) -> dict:
            if should_crash:
                raise RuntimeError("simulated crash at Trader")
            return {"trader_investment_plan": "Buy 1000 shares"}

        builder = StateGraph(AgentState)
        for a in ALL_7_ANALYSTS:
            field = REPORT_FIELDS[a]
            builder.add_node(f"{a.capitalize()} Analyst", lambda s, f=field, val=f"report_{a}": {f: val})
            builder.add_node(f"Msg Clear {a.capitalize()}", lambda s: {})
            builder.add_edge(START, f"{a.capitalize()} Analyst")
            builder.add_edge(f"{a.capitalize()} Analyst", f"Msg Clear {a.capitalize()}")
            builder.add_edge(f"Msg Clear {a.capitalize()}", "Quality Gate")

        builder.add_node("Quality Gate", lambda s: {"data_quality_summary": "QG ok"})
        builder.add_node("Bull Researcher", lambda s: {"investment_plan": "Debate ok"})
        builder.add_node("Trader", trader_node)

        builder.add_edge("Quality Gate", "Bull Researcher")
        builder.add_edge("Bull Researcher", "Trader")
        builder.add_edge("Trader", END)

        # Run 1: Crash at Trader
        should_crash = True
        with get_checkpointer(tmpdir, ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            with pytest.raises(RuntimeError, match="simulated crash at Trader"):
                graph.invoke(_init_state(ticker, date), config=cfg)

        assert has_checkpoint(tmpdir, ticker, date) is True

        # Run 2: Resume
        should_crash = False
        with get_checkpointer(tmpdir, ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            res = graph.invoke(None, config=cfg)

        assert res["trader_investment_plan"] == "Buy 1000 shares"
        assert res["data_quality_summary"] == "QG ok"
        for a in ALL_7_ANALYSTS:
            assert res[REPORT_FIELDS[a]] == f"report_{a}"

    def test_clear_checkpoint_resets_parallel_run(self):
        """清理 checkpoint 后重新运行，图从头完整执行。"""
        tmpdir = tempfile.mkdtemp()
        ticker = "TEST_CLEAR"
        date = "2026-04-20"
        tid = thread_id(ticker, date)
        cfg = {"configurable": {"thread_id": tid}}

        should_crash = True

        def qg_node(state):
            if should_crash:
                raise RuntimeError("crash")
            return {"data_quality_summary": "clean"}

        builder = StateGraph(AgentState)
        builder.add_node("Market Analyst", lambda s: {"market_report": "m"})
        builder.add_node("Msg Clear Market", lambda s: {})
        builder.add_node("Quality Gate", qg_node)
        builder.add_edge(START, "Market Analyst")
        builder.add_edge("Market Analyst", "Msg Clear Market")
        builder.add_edge("Msg Clear Market", "Quality Gate")
        builder.add_edge("Quality Gate", END)

        # 触发崩溃并保存 checkpoint
        should_crash = True
        with get_checkpointer(tmpdir, ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            with pytest.raises(RuntimeError):
                graph.invoke({"company_of_interest": ticker, "trade_date": date, "messages": []}, config=cfg)

        assert has_checkpoint(tmpdir, ticker, date) is True

        # 清除 checkpoint
        clear_checkpoint(tmpdir, ticker, date)
        assert has_checkpoint(tmpdir, ticker, date) is False

        # 从头重新运行
        should_crash = False
        with get_checkpointer(tmpdir, ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            res = graph.invoke(
                {"company_of_interest": ticker, "trade_date": date, "messages": []},
                config=cfg,
            )

        assert res["market_report"] == "m"
        assert res["data_quality_summary"] == "clean"

    def test_prepare_graph_run_with_parallel_checkpoint(self):
        """验证 TradingAgentsGraph.prepare_graph_run 针对并行图 checkpoint 的初始化逻辑。"""
        tmpdir = tempfile.mkdtemp()
        ticker = "600519"
        date = "2026-04-20"
        tid = thread_id(ticker, date)
        cfg = {"configurable": {"thread_id": tid}}

        # 先制造一个 checkpoint
        builder = StateGraph(AgentState)
        builder.add_node("Market Analyst", lambda s: {"market_report": "m"})
        builder.add_node("Quality Gate", lambda s: (_ for _ in ()).throw(RuntimeError("crash")))
        builder.add_edge(START, "Market Analyst")
        builder.add_edge("Market Analyst", "Quality Gate")
        builder.add_edge("Quality Gate", END)

        with get_checkpointer(tmpdir, ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            with pytest.raises(RuntimeError):
                graph.invoke({"company_of_interest": ticker, "trade_date": date, "messages": []}, config=cfg)

        # 模拟 TradingAgentsGraph 实例
        fake_graph = MagicMock()
        # A05: prepare_graph_run 经由 run_context/_prepare_graph_run，
        # mock 上指回真实内部实现
        fake_graph.run_context = lambda: __import__("contextlib").nullcontext()
        import types as _t
        fake_graph._prepare_graph_run = _t.MethodType(
            TradingAgentsGraph._prepare_graph_run, fake_graph
        )
        fake_graph.config = {
            "checkpoint_enabled": True,
            "data_cache_dir": tmpdir,
        }
        fake_graph.workflow = builder
        fake_graph._checkpointer_ctx = None
        fake_graph.propagator.get_graph_args.return_value = {
            "stream_mode": "values",
            "config": {"recursion_limit": 100},
        }

        init_state, run_args, step = TradingAgentsGraph.prepare_graph_run(
            fake_graph,
            ticker,
            date,
        )

        # 续跑时 init_state 必须为 None
        assert init_state is None
        assert step >= 1
        assert run_args["config"]["configurable"]["thread_id"] == tid
        fake_graph.propagator.create_initial_state.assert_not_called()

        TradingAgentsGraph.close_graph_run(fake_graph)


# ===========================================================================
# 6. 端到端完整编译工作流执行测试
# ===========================================================================


class TestFullGraphSetupExecution:
    """验证由 GraphSetup.setup_graph 真实构建并编译的完整图可端到端成功执行无异常。"""

    @pytest.fixture
    def mock_chat_llm(self):
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.outputs import ChatGeneration, ChatResult

        class _FakeChat(BaseChatModel):
            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                text = "【研究报告】" + "详细分析内容" * 20 + "\n\n| 指标 | 结论 |\n|---|---|\n| 综合 | 看多 |\n"
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])

            @property
            def _llm_type(self) -> str:
                return "fake-chat"

            def bind_tools(self, tools, **kwargs):
                return self

        return _FakeChat()

    def test_full_7_analysts_workflow_execution(self, mock_chat_llm):
        """7 位分析师完整图端到端执行，验证 Fan-Out/Fan-In 聚合无运行时异常或消息冲突。"""
        from langgraph.prebuilt import ToolNode
        from tradingagents.graph.propagation import Propagator

        tool_nodes = {k: ToolNode([]) for k in ALL_7_ANALYSTS}
        cond_logic = ConditionalLogic(max_debate_rounds=1, max_risk_discuss_rounds=1)
        setup = GraphSetup(mock_chat_llm, mock_chat_llm, tool_nodes, cond_logic)
        workflow = setup.setup_graph(ALL_7_ANALYSTS)
        graph = workflow.compile()

        prop = Propagator()
        init_state = prop.create_initial_state("600519", "2026-08-01")

        result = graph.invoke(init_state)

        # 验证所有 7 位分析师均产出了报告
        for a in ALL_7_ANALYSTS:
            field = REPORT_FIELDS[a]
            assert result.get(field), f"分析师报告缺失: {field}"

        # 验证质量门控执行
        assert result.get("data_quality_summary"), "质量门控未产出摘要"

        # 验证下游辩论与决策
        assert result.get("final_trade_decision"), "未生成最终交易决策"

    def test_index_graph_5_analysts_workflow_execution(self, mock_chat_llm):
        """指数模式 5 位分析师完整图端到端执行。"""
        from langgraph.prebuilt import ToolNode
        from tradingagents.graph.propagation import Propagator

        tool_nodes = {k: ToolNode([]) for k in ALL_7_ANALYSTS}
        cond_logic = ConditionalLogic(max_debate_rounds=1, max_risk_discuss_rounds=1)
        setup = GraphSetup(
            mock_chat_llm,
            mock_chat_llm,
            tool_nodes,
            cond_logic,
            node_factories=INDEX_NODE_FACTORIES,
        )
        workflow = setup.setup_graph(INDEX_ANALYSTS)
        graph = workflow.compile()

        prop = Propagator()
        init_state = prop.create_initial_state("000300.SH", "2026-08-01")

        result = graph.invoke(init_state)

        for a in INDEX_ANALYSTS:
            field = REPORT_FIELDS[a]
            assert result.get(field), f"指数分析师报告缺失: {field}"

        # 未选中的个股专属报告应为空
        assert not result.get("fundamentals_report")
        assert not result.get("lockup_report")

        assert result.get("data_quality_summary")
        assert result.get("final_trade_decision")

    @pytest.mark.parametrize("subset", [
        ["market"],
        ["policy", "hot_money"],
        ["social", "news", "fundamentals"],
    ])
    def test_arbitrary_subset_workflow_execution(self, mock_chat_llm, subset):
        """任意分析师子集图端到端执行。"""
        from langgraph.prebuilt import ToolNode
        from tradingagents.graph.propagation import Propagator

        tool_nodes = {k: ToolNode([]) for k in ALL_7_ANALYSTS}
        cond_logic = ConditionalLogic(max_debate_rounds=1, max_risk_discuss_rounds=1)
        setup = GraphSetup(mock_chat_llm, mock_chat_llm, tool_nodes, cond_logic)
        workflow = setup.setup_graph(subset)
        graph = workflow.compile()

        prop = Propagator()
        init_state = prop.create_initial_state("600519", "2026-08-01")

        result = graph.invoke(init_state)

        for a in subset:
            field = REPORT_FIELDS[a]
            assert result.get(field), f"选中分析师报告缺失: {field}"

        assert result.get("data_quality_summary")
        assert result.get("final_trade_decision")


# ===========================================================================
# 7. R1：并行分支的消息与工具调用隔离（真实 GraphSetup + 真实 ToolNode）
# ===========================================================================


def _make_tool(name: str, result_text: str, counter: Dict[str, int]):
    """Build a real langchain tool that records executions into ``counter``."""

    def _impl(ticker: str) -> str:
        counter[name] = counter.get(name, 0) + 1
        return result_text

    _impl.__name__ = name
    _impl.__doc__ = f"Fake tool {name} for parallel isolation tests."
    return tool(_impl)


def _tool_call_msg(name: str, args: Dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )



def _init_state(ticker: str = "600519", date: str = "2026-08-01") -> Dict[str, Any]:
    """完整初始 state（与真实入口 Propagator.create_initial_state 一致）。"""
    from tradingagents.graph.propagation import Propagator

    return Propagator().create_initial_state(ticker, date)


def _scripted_analyst_factory(field: str, script: List[AIMessage]) -> Any:
    """node_factories entry: an analyst node returning scripted AIMessages.

    First invocations return tool-call requests (driving the real ToolNode
    loop), the final invocation returns the report. The node reads/writes the
    shared ``messages`` convention — whatever mapping the graph applies to the
    real factory nodes must apply here too.
    """
    state_ = {"calls": 0}

    def factory(llm):
        def node(state):
            idx = min(state_["calls"], len(script) - 1)
            msg = script[idx]
            state_["calls"] += 1
            report = "" if getattr(msg, "tool_calls", None) else msg.content
            return {"messages": [msg], field: report}

        return node

    factory.state = state_
    return factory


def _branch_tool_message_values(result: Dict[str, Any], role: str) -> List[ToolMessage]:
    msgs = result.get(f"{role}_messages") or result.get("messages") or []
    return [m for m in msgs if isinstance(m, ToolMessage)]


class TestParallelToolIsolation:
    """R1: 各分析师分支的工具请求/响应必须彼此隔离（行为级断言，不依赖实现）。"""

    def _build(self, node_factories, tool_impls, selected):
        from langgraph.prebuilt import ToolNode

        tool_nodes = {role: ToolNode(tools) for role, tools in tool_impls.items()}
        cond_logic = ConditionalLogic(max_debate_rounds=1, max_risk_discuss_rounds=1)
        setup = GraphSetup(
            MockLLM(), MockLLM(), tool_nodes, cond_logic, node_factories=node_factories
        )
        return setup.setup_graph(selected).compile()

    def test_two_analysts_different_tools_both_execute(self):
        """market/news 各自请求自己的工具：两个工具各执行一次，无 invalid tool 错误。"""
        counter: Dict[str, int] = {}
        market_tool = _make_tool("market_data", "MARKET_DATA_OK", counter)
        news_tool = _make_tool("news_data", "NEWS_DATA_OK", counter)

        market_factory = _scripted_analyst_factory(
            "market_report",
            [
                _tool_call_msg("market_data", {"ticker": "600519"}, "call_m1"),
                AIMessage(content="market report done"),
            ],
        )
        news_factory = _scripted_analyst_factory(
            "news_report",
            [
                _tool_call_msg("news_data", {"ticker": "600519"}, "call_n1"),
                AIMessage(content="news report done"),
            ],
        )

        graph = self._build(
            {"market": market_factory, "news": news_factory},
            {"market": [market_tool], "news": [news_tool]},
            ["market", "news"],
        )
        result = graph.invoke(_init_state())

        # 两个工具各执行一次
        assert counter.get("market_data") == 1, f"market_data 执行次数: {counter}"
        assert counter.get("news_data") == 1, f"news_data 执行次数: {counter}"

        # 两个报告都完整产出
        assert result.get("market_report") == "market report done"
        assert result.get("news_report") == "news report done"

        # 没有任何分支收到 "is not a valid tool" 错误
        for role in ("market", "news"):
            for tm in _branch_tool_message_values(result, role):
                assert "is not a valid tool" not in str(tm.content), (
                    f"{role} 分支收到无效工具错误: {tm.content}"
                )

    def test_same_tool_name_different_args_each_branch_gets_own_response(self):
        """两个分支共用同名工具但参数/请求 ID 不同：各自只收到自己的响应。"""
        counter: Dict[str, int] = {}

        def shared_impl(ticker: str) -> str:
            """Shared probe tool."""
            counter["shared_probe"] = counter.get("shared_probe", 0) + 1
            return f"SHARED_FOR_{ticker}"

        shared_probe = tool(shared_impl)
        shared_probe.name = "shared_probe"

        market_factory = _scripted_analyst_factory(
            "market_report",
            [
                _tool_call_msg("shared_probe", {"ticker": "AAA"}, "call_m_shared"),
                AIMessage(content="market shared report"),
            ],
        )
        news_factory = _scripted_analyst_factory(
            "news_report",
            [
                _tool_call_msg("shared_probe", {"ticker": "BBB"}, "call_n_shared"),
                AIMessage(content="news shared report"),
            ],
        )

        graph = self._build(
            {"market": market_factory, "news": news_factory},
            {"market": [shared_probe], "news": [shared_probe]},
            ["market", "news"],
        )
        result = graph.invoke(_init_state())

        assert counter.get("shared_probe") == 2
        assert result.get("market_report") == "market shared report"
        assert result.get("news_report") == "news shared report"

        market_tools = _branch_tool_message_values(result, "market")
        news_tools = _branch_tool_message_values(result, "news")
        assert len(market_tools) == 1 and len(news_tools) == 1
        assert market_tools[0].tool_call_id == "call_m_shared"
        assert news_tools[0].tool_call_id == "call_n_shared"
        assert market_tools[0].content == "SHARED_FOR_AAA"
        assert news_tools[0].content == "SHARED_FOR_BBB"

    def test_multiple_tool_calls_in_one_message(self):
        """一条 AIMessage 内多个工具调用：全部执行并逐个配对。"""
        counter: Dict[str, int] = {}
        t1 = _make_tool("multi_one", "R1_OK", counter)
        t2 = _make_tool("multi_two", "R2_OK", counter)

        factory = _scripted_analyst_factory(
            "market_report",
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "multi_one", "args": {"ticker": "X"}, "id": "c1", "type": "tool_call"},
                        {"name": "multi_two", "args": {"ticker": "Y"}, "id": "c2", "type": "tool_call"},
                    ],
                ),
                AIMessage(content="multi-call report"),
            ],
        )

        graph = self._build(
            {"market": factory}, {"market": [t1, t2]}, ["market"]
        )
        result = graph.invoke(_init_state())

        assert counter.get("multi_one") == 1
        assert counter.get("multi_two") == 1
        assert result.get("market_report") == "multi-call report"

        tool_msgs = {tm.tool_call_id: tm.content for tm in _branch_tool_message_values(result, "market")}
        assert tool_msgs.get("c1") == "R1_OK"
        assert tool_msgs.get("c2") == "R2_OK"

    def test_multi_round_tool_loop(self):
        """连续多轮工具循环：每轮请求都被执行，请求与响应按轮次配对。"""
        counter: Dict[str, int] = {}
        probe = _make_tool("loop_probe", "LOOP_OK", counter)

        factory = _scripted_analyst_factory(
            "market_report",
            [
                _tool_call_msg("loop_probe", {"ticker": "600519"}, "loop_1"),
                _tool_call_msg("loop_probe", {"ticker": "600519"}, "loop_2"),
                _tool_call_msg("loop_probe", {"ticker": "600519"}, "loop_3"),
                AIMessage(content="loop report"),
            ],
        )

        graph = self._build(
            {"market": factory}, {"market": [probe]}, ["market"]
        )
        result = graph.invoke(_init_state())

        assert counter.get("loop_probe") == 3
        assert result.get("market_report") == "loop report"
        assert len(_branch_tool_message_values(result, "market")) == 3

    def test_fast_branch_report_not_clobbered(self):
        """market 直接完成、news 先走工具：已完成分支的报告不会被覆盖为空。"""
        counter: Dict[str, int] = {}
        news_tool = _make_tool("news_probe", "NEWS_OK", counter)

        market_factory = _scripted_analyst_factory(
            "market_report", [AIMessage(content="instant market report")]
        )
        news_factory = _scripted_analyst_factory(
            "news_report",
            [
                _tool_call_msg("news_probe", {"ticker": "600519"}, "call_news"),
                AIMessage(content="news report after tool"),
            ],
        )

        graph = self._build(
            {"market": market_factory, "news": news_factory},
            {"market": [], "news": [news_tool]},
            ["market", "news"],
        )
        result = graph.invoke(_init_state())

        assert result.get("market_report") == "instant market report"
        assert result.get("news_report") == "news report after tool"
        assert counter.get("news_probe") == 1

    def test_checkpoint_resume_does_not_replay_completed_tool_calls(self):
        """Quality Gate 之后崩溃并恢复：工具不重复执行，消息请求/响应仍配对。"""
        from langgraph.prebuilt import ToolNode

        counter: Dict[str, int] = {}
        market_tool = _make_tool("market_data", "MARKET_DATA_OK", counter)
        news_tool = _make_tool("news_data", "NEWS_DATA_OK", counter)

        market_factory = _scripted_analyst_factory(
            "market_report",
            [
                _tool_call_msg("market_data", {"ticker": "600519"}, "call_m1"),
                AIMessage(content="market report done"),
            ],
        )
        news_factory = _scripted_analyst_factory(
            "news_report",
            [
                _tool_call_msg("news_data", {"ticker": "600519"}, "call_n1"),
                AIMessage(content="news report done"),
            ],
        )

        crash = {"on": True}

        def bull_node(state):
            if crash["on"]:
                raise RuntimeError("simulated crash after quality gate")
            debate = state["investment_debate_state"]
            argument = "Bull Analyst: argument"
            return {
                "investment_debate_state": {
                    **debate,
                    "current_response": argument,
                    "count": debate["count"] + 1,
                }
            }

        tool_nodes = {
            "market": ToolNode([market_tool]),
            "news": ToolNode([news_tool]),
        }
        cond_logic = ConditionalLogic(max_debate_rounds=1, max_risk_discuss_rounds=1)
        setup = GraphSetup(
            MockLLM(),
            MockLLM(),
            tool_nodes,
            cond_logic,
            node_factories={
                "market": market_factory,
                "news": news_factory,
                "bull": lambda llm: bull_node,
            },
        )
        workflow = setup.setup_graph(["market", "news"])

        tmpdir = tempfile.mkdtemp()
        ticker, date = "600519", "2026-08-01"
        cfg = {"configurable": {"thread_id": thread_id(ticker, date)}}

        with get_checkpointer(tmpdir, ticker) as saver:
            graph = workflow.compile(checkpointer=saver)
            with pytest.raises(RuntimeError, match="simulated crash"):
                graph.invoke(_init_state(ticker, date), config=cfg)

        assert counter.get("market_data") == 1
        assert counter.get("news_data") == 1

        crash["on"] = False
        with get_checkpointer(tmpdir, ticker) as saver:
            graph = workflow.compile(checkpointer=saver)
            resumed = graph.invoke(None, config=cfg)

        # 恢复后不重复执行已完成的工具
        assert counter.get("market_data") == 1
        assert counter.get("news_data") == 1
        # 报告与消息配对仍然完整
        assert resumed.get("market_report") == "market report done"
        assert resumed.get("news_report") == "news report done"
        assert resumed.get("investment_debate_state", {}).get("count", 0) >= 1
        assert resumed.get("final_trade_decision")
        for role in ("market", "news"):
            tool_msgs = _branch_tool_message_values(resumed, role)
            assert len(tool_msgs) == 1, f"{role} 分支 ToolMessage 配对异常: {tool_msgs}"

    @pytest.mark.parametrize("selected", [
        ["market", "social", "news", "fundamentals", "policy", "hot_money", "lockup"],
        ["market", "news", "policy"],
        ["market"],
    ])
    def test_full_selection_tool_loops_execute(self, selected):
        """全选/子集/单分析师：每个分支走真实工具循环且全部产出报告。"""
        counter: Dict[str, int] = {}
        probe = _make_tool("branch_probe", "PROBE_OK", counter)

        factories = {}
        tool_impls = {}
        for role in selected:
            field = REPORT_FIELDS[role]
            factories[role] = _scripted_analyst_factory(
                field,
                [
                    _tool_call_msg("branch_probe", {"ticker": "600519"}, f"call_{role}"),
                    AIMessage(content=f"{role} report"),
                ],
            )
            tool_impls[role] = [probe]

        graph = self._build(factories, tool_impls, selected)
        result = graph.invoke(_init_state())

        assert counter.get("branch_probe") == len(selected)
        for role in selected:
            assert result.get(REPORT_FIELDS[role]) == f"{role} report", (
                f"{role} 报告缺失或错误"
            )
        assert result.get("data_quality_summary")
        assert result.get("final_trade_decision")


# ===========================================================================
# 8. R2：Quality Gate 汇合屏障（等全部选中分析师完成才执行）
# ===========================================================================


class _ReviewCapturingLLM:
    """QG 用假 LLM：记录每次收到的复审 prompt 与调用次数。"""

    def __init__(self):
        self.prompts: List[str] = []

    def invoke(self, prompt, *args, **kwargs):
        self.prompts.append(str(prompt))
        return AIMessage(content="## 数据质量审核报告\n\n**整体评级**: A")


def _long_report(role: str) -> str:
    """Long enough to pass the quality gate's hard checks (length + table)."""
    return (
        f"{role} final report: " + "详细分析内容" * 40
        + "\n| 指标 | 结论 |\n|---|---|\n| 综合 | 通过 |"
    )


class TestQualityGateBarrier:
    """R2: Quality Gate 必须等所有选中分析师完成；完整执行中只运行一次。"""

    def _run_mixed_rounds(self, selected, rounds: Dict[str, int], ticker="600519"):
        """Run the real graph where each analyst does `rounds[role]` tool loops."""
        from langgraph.prebuilt import ToolNode

        counter: Dict[str, int] = {}
        probe = _make_tool("gate_probe", "GATE_PROBE_OK", counter)

        factories = {}
        tool_impls = {}
        for role in selected:
            script: List[AIMessage] = []
            for i in range(rounds.get(role, 0)):
                script.append(
                    _tool_call_msg("gate_probe", {"ticker": ticker}, f"call_{role}_{i}")
                )
            script.append(AIMessage(content=_long_report(role)))
            factories[role] = _scripted_analyst_factory(REPORT_FIELDS[role], script)
            tool_impls[role] = [probe]

        qg_llm = _ReviewCapturingLLM()

        def resolve_llm(role):
            return qg_llm if role == "quality_gate" else None

        tool_nodes = {role: ToolNode(tools) for role, tools in tool_impls.items()}
        cond_logic = ConditionalLogic(max_debate_rounds=1, max_risk_discuss_rounds=1)
        setup = GraphSetup(
            MockLLM(),
            MockLLM(),
            tool_nodes,
            cond_logic,
            resolve_llm=resolve_llm,
            node_factories=factories,
        )
        graph = setup.setup_graph(selected).compile()
        result = graph.invoke(
            Propagator().create_initial_state(ticker, "2026-08-01")
        )
        return result, qg_llm, counter

    def test_gate_runs_once_with_all_reports_when_loops_differ(self):
        """0/1/3 轮工具循环混合（全 7 分析师）：QG 首次执行时报告已齐全，且只执行一次。"""
        result, qg_llm, _ = self._run_mixed_rounds(
            ALL_7_ANALYSTS, {"market": 0, "news": 1, "policy": 3}
        )

        # QG 的 LLM 复审只被调用一次
        assert len(qg_llm.prompts) == 1, (
            f"Quality Gate 执行了 {len(qg_llm.prompts)} 次（期望 1 次），"
            f"各次 prompt 长度: {[len(p) for p in qg_llm.prompts]}"
        )

        # 首次执行时全部报告都已写入（prompt 中不应有"报告为空"占位）
        first_prompt = qg_llm.prompts[0]
        for role in ALL_7_ANALYSTS:
            assert f"{role} final report" in first_prompt, (
                f"QG 首次执行时 {role} 的报告尚未生成"
            )
        assert "（报告为空）" not in first_prompt

        assert result.get("data_quality_summary")

    def test_gate_and_downstream_run_once_in_full_success_run(self):
        """完整成功执行：Quality Gate 触发下游一次，Portfolio Manager（最终决策）只执行一次。"""
        from langgraph.prebuilt import ToolNode

        pm_calls = {"n": 0}
        bull_calls = {"n": 0}

        market_factory = _scripted_analyst_factory(
            "market_report",
            [
                _tool_call_msg("gate_probe", {"ticker": "600519"}, "cm"),
                AIMessage(content=_long_report("market")),
            ],
        )
        news_factory = _scripted_analyst_factory(
            "news_report", [AIMessage(content=_long_report("news"))]
        )

        counter: Dict[str, int] = {}
        probe = _make_tool("gate_probe", "GATE_PROBE_OK", counter)

        def bull_node(state):
            bull_calls["n"] += 1
            return {
                "investment_debate_state": {
                    **state["investment_debate_state"],
                    "current_response": f"Bull Analyst: argument #{bull_calls['n']}",
                    "count": bull_calls["n"],
                }
            }

        def pm_node(state):
            pm_calls["n"] += 1
            return {"final_trade_decision": f"DECISION #{pm_calls['n']}"}

        qg_llm = _ReviewCapturingLLM()
        tool_nodes = {
            "market": ToolNode([probe]),
            "news": ToolNode([]),
        }
        cond_logic = ConditionalLogic(max_debate_rounds=1, max_risk_discuss_rounds=1)
        setup = GraphSetup(
            MockLLM(),
            MockLLM(),
            tool_nodes,
            cond_logic,
            resolve_llm=lambda role: qg_llm if role == "quality_gate" else None,
            node_factories={
                "market": market_factory,
                "news": news_factory,
                "bull": lambda llm: bull_node,
                "portfolio_manager": lambda llm: pm_node,
            },
        )
        graph = setup.setup_graph(["market", "news"]).compile()
        result = graph.invoke(Propagator().create_initial_state("600519", "2026-08-01"))

        assert bull_calls["n"] == 1, (
            f"Bull Researcher 被触发了 {bull_calls['n']} 次（期望 1 次）——"
            "Quality Gate 屏障未生效，下游被重复启动"
        )
        assert pm_calls["n"] == 1, f"Portfolio Manager 执行了 {pm_calls['n']} 次（期望 1 次）"
        assert result.get("final_trade_decision") == "DECISION #1"
        assert "market final report" in result.get("market_report", "")
        assert "news final report" in result.get("news_report", "")

    def test_single_analyst_and_index_default_do_not_deadlock(self):
        """单分析师与指数默认 5 分析师在屏障下完整执行，不死锁。"""
        result, qg_llm, counter = self._run_mixed_rounds(
            ["market", "social", "news", "policy", "hot_money"],
            {"market": 1, "social": 0, "news": 2, "policy": 0, "hot_money": 1},
            ticker="000300.SH",
        )
        assert len(qg_llm.prompts) == 1
        assert counter.get("gate_probe") == 4
        for role in ["market", "social", "news", "policy", "hot_money"]:
            assert f"{role} final report" in result.get(REPORT_FIELDS[role], "")
        assert result.get("final_trade_decision")

        single_result, _, _ = self._run_mixed_rounds(["lockup"], {"lockup": 2})
        assert "lockup final report" in single_result.get("lockup_report", "")
        assert single_result.get("final_trade_decision")

    def test_failed_branch_prevents_downstream_success_path(self):
        """分支抛异常时异常传播，Quality Gate 与最终决策不会执行。"""
        from langgraph.prebuilt import ToolNode

        def failing_market(state):
            raise RuntimeError("market analyst exploded")

        news_factory = _scripted_analyst_factory("news_report", [AIMessage(content="news ok")])

        pm_calls = {"n": 0}

        def pm_node(state):
            pm_calls["n"] += 1
            return {"final_trade_decision": "SHOULD_NOT_RUN"}

        qg_llm = _ReviewCapturingLLM()
        tool_nodes = {"market": ToolNode([]), "news": ToolNode([])}
        cond_logic = ConditionalLogic(max_debate_rounds=1, max_risk_discuss_rounds=1)
        setup = GraphSetup(
            MockLLM(),
            MockLLM(),
            tool_nodes,
            cond_logic,
            resolve_llm=lambda role: qg_llm if role == "quality_gate" else None,
            node_factories={
                "market": lambda llm: failing_market,
                "news": news_factory,
                "portfolio_manager": lambda llm: pm_node,
            },
        )
        graph = setup.setup_graph(["market", "news"]).compile()

        with pytest.raises(RuntimeError, match="market analyst exploded"):
            graph.invoke(_init_state())

        assert not qg_llm.prompts, "分支失败后 Quality Gate 不应执行"
        assert pm_calls["n"] == 0, "分支失败后最终决策不应执行"

    def test_partial_completion_then_failure_resume_keeps_reports(self):
        """部分分支完成后失败并恢复：最终报告齐全且 Quality Gate 无重复执行。"""
        from langgraph.prebuilt import ToolNode

        counter: Dict[str, int] = {}
        news_tool = _make_tool("resume_probe", "RESUME_OK", counter)

        market_factory = _scripted_analyst_factory(
            "market_report", [AIMessage(content=_long_report("market"))]
        )
        news_factory = _scripted_analyst_factory(
            "news_report",
            [
                _tool_call_msg("resume_probe", {"ticker": "600519"}, "call_resume"),
                AIMessage(content=_long_report("news")),
            ],
        )
        # 其余 5 个分析师直接完成（0 轮工具），凑齐全部报告以驱动 QG 的 LLM 复审
        fast_factories = {
            role: _scripted_analyst_factory(
                REPORT_FIELDS[role], [AIMessage(content=_long_report(role))]
            )
            for role in ("social", "fundamentals", "policy", "hot_money", "lockup")
        }

        crash = {"on": True}

        def trader_node(state):
            if crash["on"]:
                raise RuntimeError("simulated crash at trader")
            return {"trader_investment_plan": "plan"}

        qg_llm = _ReviewCapturingLLM()
        tool_nodes = {
            "market": ToolNode([]),
            "news": ToolNode([news_tool]),
            **{role: ToolNode([]) for role in fast_factories},
        }
        cond_logic = ConditionalLogic(max_debate_rounds=1, max_risk_discuss_rounds=1)
        setup = GraphSetup(
            MockLLM(),
            MockLLM(),
            tool_nodes,
            cond_logic,
            resolve_llm=lambda role: qg_llm if role == "quality_gate" else None,
            node_factories={
                "market": market_factory,
                "news": news_factory,
                "trader": lambda llm: trader_node,
                **fast_factories,
            },
        )
        workflow = setup.setup_graph(ALL_7_ANALYSTS)

        tmpdir = tempfile.mkdtemp()
        ticker, date = "600519", "2026-08-01"
        cfg = {"configurable": {"thread_id": thread_id(ticker, date)}}

        with get_checkpointer(tmpdir, ticker) as saver:
            graph = workflow.compile(checkpointer=saver)
            with pytest.raises(RuntimeError, match="simulated crash at trader"):
                graph.invoke(_init_state(ticker, date), config=cfg)

        prompts_before_resume = len(qg_llm.prompts)
        assert prompts_before_resume == 1, "崩溃前 Quality Gate 应已恰好执行一次"

        crash["on"] = False
        qg_llm.prompts.clear()
        with get_checkpointer(tmpdir, ticker) as saver:
            graph = workflow.compile(checkpointer=saver)
            resumed = graph.invoke(None, config=cfg)

        # 恢复后：报告齐全、QG 不重复执行、下游完成
        assert "market final report" in resumed.get("market_report", "")
        assert "news final report" in resumed.get("news_report", "")
        for role in ("social", "fundamentals", "policy", "hot_money", "lockup"):
            assert f"{role} final report" in resumed.get(REPORT_FIELDS[role], "")
        assert not qg_llm.prompts, "恢复后 Quality Gate 不应重复执行"
        assert resumed.get("trader_investment_plan") == "plan"
        assert counter.get("resume_probe") == 1
