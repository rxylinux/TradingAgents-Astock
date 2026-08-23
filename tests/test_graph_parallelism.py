"""Tests for analyst parallel execution topology, state aggregation, and checkpoint resume.

Covers:
1. 7 Analysts full selection topology (START fan-out -> 7 analysts, all Msg Clear fan-in -> Quality Gate).
2. Analyst subsets topology (1 analyst, 3 analysts, arbitrary subsets).
3. TradingAgentsIndexGraph 5-analyst parallel topology.
4. Parallel graph execution simulation & state aggregation at Quality Gate.
5. Checkpoint / resume compatibility under parallel graph topology.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage
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
                graph.invoke(
                    {"company_of_interest": ticker, "trade_date": date, "messages": []},
                    config=cfg,
                )

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
