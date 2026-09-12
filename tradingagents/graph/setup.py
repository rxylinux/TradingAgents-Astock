# TradingAgents/graph/setup.py

from typing import Any, Dict
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from tradingagents.agents import *
from tradingagents.agents.utils.agent_states import AgentState
from tradingagents.evidence.cutoff import trusted_cutoff
from tradingagents.evidence.ledger import collect_tool_message_delta

from .conditional_logic import ConditionalLogic


# 需要综合全局信息做决策的两个节点走 deep 档，其余走 quick 档。
# 这是**没有单独配置角色模型时**的默认分档，与原行为一致。
DEEP_ROLES = frozenset({"research_manager", "portfolio_manager"})

# 可以单独指定模型的角色（config["role_llms"] 的合法键）。
# 单列出来是为了把配错的角色名当场报出来，而不是静默忽略、让人以为配置生效了。
ROLE_KEYS = (
    "market", "social", "news", "fundamentals", "policy", "hot_money", "lockup",
    "quality_gate", "bull", "bear", "research_manager", "trader",
    "risk_aggressive", "risk_neutral", "risk_conservative", "portfolio_manager",
)


def _branch_messages_key(role: str) -> str:
    return f"{role}_messages"


def _branch_isolated_analyst_node(role: str, node_fn):
    """Wrap an analyst node so its LLM loop runs on a branch-private channel.

    The node keeps reading/writing the ``messages`` key as before; the wrapper
    feeds it only this branch's messages (``{role}_messages``) and publishes
    its output back to that channel. Without this isolation the parallel
    branches share one ``messages`` channel, and the shared ToolNode executes
    whichever branch's AIMessage happens to be last — cross-branch tool
    requests/responses get mismatched (R1).
    """

    key = _branch_messages_key(role)

    def wrapped(state):
        branch_state = dict(state)
        branch_state["messages"] = state.get(key) or []
        out = node_fn(branch_state) or {}
        result = dict(out)
        msgs = result.pop("messages", None)
        if msgs is not None:
            result[key] = msgs
        return result

    return wrapped


def _branch_isolated_tool_node(role: str, tool_node):
    """Wrap a ToolNode so it executes on this branch's private channel.

    The ToolNode itself stays stock (no ``messages_key`` needed); the wrapper
    maps ``{role}_messages`` in/out, so tool requests and responses pair up
    per branch and per ``tool_call_id``.

    C1: after mapping, the wrapper also collects THIS execution's
    ``ToolMessage.artifact`` payloads (evidence-capable news tools emit them
    via ``response_format="content_and_artifact"``) and publishes them as one
    delta on the ``evidence_bundle`` channel. Run identity comes from state
    (``run_metadata.run_id``) — never from the model — and the reducer merges
    deltas immutably/idempotently, so parallel branches and checkpoint
    replays cannot double-count or cross-contaminate.

    Trusted cutoff (Codex C1 audit R1): the run's ``trade_date`` from state
    is opened as a call-scoped trusted cutoff around this single ToolNode
    invocation; evidence tools clamp model-requested end/curr dates to it
    BEFORE the vendor fetch, so future articles can reach neither the
    ToolMessage text nor the evidence index.
    """

    key = _branch_messages_key(role)

    def wrapped(state):
        branch_state = dict(state)
        branch_state["messages"] = state.get(key) or []
        with trusted_cutoff(state.get("trade_date")):
            out = tool_node.invoke(branch_state)
        msgs = out.get("messages") if isinstance(out, dict) else None

        result = {}
        if msgs is not None:
            result[key] = msgs
        delta = collect_tool_message_delta(
            msgs or [],
            role=role,
            run_id=((state.get("run_metadata") or {}).get("run_id") or ""),
            trade_date=(state.get("trade_date") or ""),
        )
        if delta is not None:
            result["evidence_bundle"] = delta
        return result

    return wrapped


class GraphSetup:
    """Handles the setup and configuration of the agent graph."""

    def __init__(
        self,
        quick_thinking_llm: Any,
        deep_thinking_llm: Any,
        tool_nodes: Dict[str, ToolNode],
        conditional_logic: ConditionalLogic,
        resolve_llm=None,
        node_factories=None,
        evidence_debate_enabled: bool = False,
    ):
        """Initialize with required components.

        resolve_llm: 可选，`role -> llm | None` 的查表函数。返回 None 表示该角色
        没有单独配置，回落到 quick/deep 两档——不传就是完全的原行为（#39）。

        node_factories: 可选，`role -> (llm) -> node_fn` 的依赖注入接缝（如指数
        分析注入 agents/index_agents.INDEX_NODE_FACTORIES）。不传 = 完全的原行为，
        全部节点用本文件导入的个股版工厂。只覆盖 7 个分析师与
        bull/bear/trader/portfolio_manager；quality_gate/research_manager/
        风险三方与标的类型无关，始终用原版。
        """
        self.quick_thinking_llm = quick_thinking_llm
        self.deep_thinking_llm = deep_thinking_llm
        self.tool_nodes = tool_nodes
        self.conditional_logic = conditional_logic
        self._resolve_llm = resolve_llm
        self._node_factories = node_factories or {}
        self._evidence_debate_enabled = bool(evidence_debate_enabled)

    def _evidence_recheck_tools(self, selected_analysts):
        """E 补查工具 = 选中分析师已注册工具中名为 get_news/get_global_news
        的真实对象（保持注册面不扩大；无可用工具 → 空列表，规划/补查保留
        缺口）。"""
        wanted = {"get_news", "get_global_news"}
        found = {}
        for role in selected_analysts:
            node = self.tool_nodes.get(role)
            by_name = getattr(node, "tools_by_name", None) or {}
            for name, tool in by_name.items():
                if name in wanted and name not in found:
                    found[name] = tool
        return [found[name] for name in ("get_news", "get_global_news") if name in found]

    def _node_factory(self, role: str, default_factory):
        """取某个角色的节点工厂：有注入用注入的，否则用原版。"""
        return self._node_factories.get(role, default_factory)

    def llm_for(self, role: str) -> Any:
        """取某个角色该用的 LLM。没单独配就回落到 quick/deep 两档。"""
        if self._resolve_llm is not None:
            llm = self._resolve_llm(role)
            if llm is not None:
                return llm
        return self.deep_thinking_llm if role in DEEP_ROLES else self.quick_thinking_llm

    def setup_graph(
        self, selected_analysts=["market", "social", "news", "fundamentals", "policy", "hot_money", "lockup"]
    ):
        """Set up and compile the agent workflow graph.

        Args:
            selected_analysts (list): List of analyst types to include. Options are:
                - "market": Market analyst (technical analysis)
                - "social": Social media / sentiment analyst
                - "news": News analyst
                - "fundamentals": Fundamentals analyst
                - "policy": Policy analyst (A-stock specific)
                - "hot_money": Hot money / capital flow tracker (A-stock specific)
                - "lockup": Lockup expiry / reduction watcher (A-stock specific)
        """
        if len(selected_analysts) == 0:
            raise ValueError("Trading Agents Graph Setup Error: no analysts selected!")

        # Create analyst nodes
        analyst_nodes = {}
        delete_nodes = {}
        tool_nodes = {}

        if "market" in selected_analysts:
            analyst_nodes["market"] = self._node_factory("market", create_market_analyst)(self.llm_for("market"))
            delete_nodes["market"] = create_msg_delete()
            tool_nodes["market"] = self.tool_nodes["market"]

        if "social" in selected_analysts:
            analyst_nodes["social"] = self._node_factory("social", create_social_media_analyst)(self.llm_for("social"))
            delete_nodes["social"] = create_msg_delete()
            tool_nodes["social"] = self.tool_nodes["social"]

        if "news" in selected_analysts:
            analyst_nodes["news"] = self._node_factory("news", create_news_analyst)(self.llm_for("news"))
            delete_nodes["news"] = create_msg_delete()
            tool_nodes["news"] = self.tool_nodes["news"]

        if "fundamentals" in selected_analysts:
            analyst_nodes["fundamentals"] = self._node_factory("fundamentals", create_fundamentals_analyst)(self.llm_for("fundamentals"))
            delete_nodes["fundamentals"] = create_msg_delete()
            tool_nodes["fundamentals"] = self.tool_nodes["fundamentals"]

        if "policy" in selected_analysts:
            analyst_nodes["policy"] = self._node_factory("policy", create_policy_analyst)(self.llm_for("policy"))
            delete_nodes["policy"] = create_msg_delete()
            tool_nodes["policy"] = self.tool_nodes["policy"]

        if "hot_money" in selected_analysts:
            analyst_nodes["hot_money"] = self._node_factory("hot_money", create_hot_money_tracker)(self.llm_for("hot_money"))
            delete_nodes["hot_money"] = create_msg_delete()
            tool_nodes["hot_money"] = self.tool_nodes["hot_money"]

        if "lockup" in selected_analysts:
            analyst_nodes["lockup"] = self._node_factory("lockup", create_lockup_watcher)(self.llm_for("lockup"))
            delete_nodes["lockup"] = create_msg_delete()
            tool_nodes["lockup"] = self.tool_nodes["lockup"]

        # Create quality gate node. A10/Codex 退回：旧断点（分支隔离已存在
        # 但无 selected_analysts 元数据）恢复时，质量门控取**当前实际图**
        # 的启用集合，而不是默认七位——否则单 market 恢复会凭空评出 6 个
        # F、指数恢复评出 2 个 F。新断点则优先使用 state 里的集合。
        quality_gate_node = create_quality_gate(
            self.llm_for("quality_gate"),
            active_analysts_fallback=lambda: list(analyst_nodes.keys()),
        )

        # Create researcher and manager nodes
        bull_researcher_node = self._node_factory("bull", create_bull_researcher)(self.llm_for("bull"))
        bear_researcher_node = self._node_factory("bear", create_bear_researcher)(self.llm_for("bear"))
        research_manager_node = create_research_manager(self.llm_for("research_manager"))
        trader_node = self._node_factory("trader", create_trader)(self.llm_for("trader"))

        # Create risk analysis nodes
        aggressive_analyst = create_aggressive_debator(self.llm_for("risk_aggressive"))
        neutral_analyst = create_neutral_debator(self.llm_for("risk_neutral"))
        conservative_analyst = create_conservative_debator(self.llm_for("risk_conservative"))
        portfolio_manager_node = self._node_factory("portfolio_manager", create_portfolio_manager)(self.llm_for("portfolio_manager"))

        # Create workflow
        workflow = StateGraph(AgentState)

        # Add analyst nodes to the graph. Analyst LLM loops and tool nodes are
        # wrapped onto branch-private message channels (R1) so parallel
        # branches cannot see or execute each other's tool calls.
        for analyst_type, node in analyst_nodes.items():
            workflow.add_node(
                f"{analyst_type.capitalize()} Analyst",
                _branch_isolated_analyst_node(analyst_type, node),
            )
            workflow.add_node(
                f"Msg Clear {analyst_type.capitalize()}", delete_nodes[analyst_type]
            )
            workflow.add_node(
                f"tools_{analyst_type}",
                _branch_isolated_tool_node(analyst_type, tool_nodes[analyst_type]),
            )

        # Add quality gate + other nodes
        workflow.add_node("Quality Gate", quality_gate_node)
        workflow.add_node("Bull Researcher", bull_researcher_node)
        workflow.add_node("Bear Researcher", bear_researcher_node)
        workflow.add_node("Research Manager", research_manager_node)
        workflow.add_node("Trader", trader_node)
        workflow.add_node("Aggressive Analyst", aggressive_analyst)
        workflow.add_node("Neutral Analyst", neutral_analyst)
        workflow.add_node("Conservative Analyst", conservative_analyst)
        workflow.add_node("Portfolio Manager", portfolio_manager_node)

        # Define edges
        # Connect START to each selected analyst (parallel execution)
        for analyst_type in selected_analysts:
            current_analyst = f"{analyst_type.capitalize()} Analyst"
            current_tools = f"tools_{analyst_type}"

            workflow.add_edge(START, current_analyst)

            # Add conditional edges for current analyst (routed on the
            # branch-private message channel, see ConditionalLogic)
            workflow.add_conditional_edges(
                current_analyst,
                getattr(self.conditional_logic, f"should_continue_{analyst_type}"),
                [current_tools, f"Msg Clear {analyst_type.capitalize()}"],
            )
            workflow.add_edge(current_tools, current_analyst)

        # R2: real join barrier before Quality Gate. Adding one edge per branch
        # inside the loop is OR semantics — the first finishing branch would
        # start the quality gate (and downstream) early, and later branches
        # would re-trigger it, running the debate pipeline twice in one run.
        # The list form waits for every selected analyst's completion node.
        barrier_sources = [
            f"Msg Clear {analyst_type.capitalize()}"
            for analyst_type in dict.fromkeys(selected_analysts)
        ]
        workflow.add_edge(barrier_sources, "Quality Gate")

        # E（Codex 实施契约 docs/E_CODEX_IMPLEMENTATION_CONTRACT_2026-09-09.md）：
        # 默认关闭 → 拓扑与历史版本完全一致（Quality Gate 直连辩论）；开启才
        # 注册互盲初判(并行)→分歧规划→两次单调用补查，之后进入**原**辩论
        # 机制（轮数不增）。新增逻辑调用上限 3（初判×2+规划×1），工具调用
        # 上限 2（每问题恰好 1 次，仅新闻 artifact 工具）。
        if self._evidence_debate_enabled:
            from tradingagents.agents.debate_evidence import (
                create_disagreement_planner_node,
                create_initial_view_node,
                create_recheck_node,
            )

            workflow.add_node("Bull Initial View",
                              create_initial_view_node("bull", self.llm_for("bull")))
            workflow.add_node("Bear Initial View",
                              create_initial_view_node("bear", self.llm_for("bear")))
            # 工具白名单来自**当前选中分析师实际注册且支持 C1 artifact**
            # 的新闻工具交集（E 契约规则 4/5；Codex E R1：不得因 E 扩大注
            # 册面，也不能以全局常量可导入证明已启用）。
            recheck_tools = self._evidence_recheck_tools(selected_analysts)
            workflow.add_node("Disagreement Planner",
                              create_disagreement_planner_node(
                                  self.llm_for("research_manager"),
                                  allowed_tools=[t.name for t in recheck_tools]))
            workflow.add_node("Recheck Q1", create_recheck_node(0, recheck_tools))
            workflow.add_node("Recheck Q2", create_recheck_node(1, recheck_tools))

            def _slot_pending(state, slot):
                ed = state.get("evidence_debate") or {}
                return any(q.get("slot") == slot and q.get("status") == "pending"
                           for q in ed.get("recheck_questions") or [])

            def _route_q1(state):
                return "Recheck Q1" if _slot_pending(state, 0) else "Bull Researcher"

            def _route_q2(state):
                return "Recheck Q2" if _slot_pending(state, 1) else "Bull Researcher"

            workflow.add_edge("Quality Gate", "Bull Initial View")
            workflow.add_edge("Quality Gate", "Bear Initial View")
            workflow.add_edge(["Bull Initial View", "Bear Initial View"],
                              "Disagreement Planner")
            workflow.add_conditional_edges("Disagreement Planner", _route_q1,
                                           ["Recheck Q1", "Bull Researcher"])
            workflow.add_conditional_edges("Recheck Q1", _route_q2,
                                           ["Recheck Q2", "Bull Researcher"])
            workflow.add_edge("Recheck Q2", "Bull Researcher")
        else:
            workflow.add_edge("Quality Gate", "Bull Researcher")

        # Add remaining edges
        workflow.add_conditional_edges(
            "Bull Researcher",
            self.conditional_logic.should_continue_debate,
            {
                "Bear Researcher": "Bear Researcher",
                "Research Manager": "Research Manager",
            },
        )
        workflow.add_conditional_edges(
            "Bear Researcher",
            self.conditional_logic.should_continue_debate,
            {
                "Bull Researcher": "Bull Researcher",
                "Research Manager": "Research Manager",
            },
        )
        workflow.add_edge("Research Manager", "Trader")
        workflow.add_edge("Trader", "Aggressive Analyst")
        workflow.add_conditional_edges(
            "Aggressive Analyst",
            self.conditional_logic.should_continue_risk_analysis,
            {
                "Conservative Analyst": "Conservative Analyst",
                "Portfolio Manager": "Portfolio Manager",
            },
        )
        workflow.add_conditional_edges(
            "Conservative Analyst",
            self.conditional_logic.should_continue_risk_analysis,
            {
                "Neutral Analyst": "Neutral Analyst",
                "Portfolio Manager": "Portfolio Manager",
            },
        )
        workflow.add_conditional_edges(
            "Neutral Analyst",
            self.conditional_logic.should_continue_risk_analysis,
            {
                "Aggressive Analyst": "Aggressive Analyst",
                "Portfolio Manager": "Portfolio Manager",
            },
        )

        workflow.add_edge("Portfolio Manager", END)

        return workflow
