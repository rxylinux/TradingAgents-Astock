"""Agent debugging callback handler for tracking LLM prompts, tool invocations, and raw responses."""

from __future__ import annotations

import copy
import json
import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional, Union
from uuid import UUID, uuid4

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    FunctionMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import LLMResult

logger = logging.getLogger(__name__)

# Node and stage identification mappings
_NODE_TO_AGENT = {
    # 7 分析师节点
    "Market Analyst": "market",
    "market_analyst": "market",
    "market": "market",
    "Social Analyst": "social",
    "social_analyst": "social",
    "social": "social",
    "Social Media Analyst": "social",
    "News Analyst": "news",
    "news_analyst": "news",
    "news": "news",
    "Fundamentals Analyst": "fundamentals",
    "fundamentals_analyst": "fundamentals",
    "fundamentals": "fundamentals",
    "Policy Analyst": "policy",
    "policy_analyst": "policy",
    "policy": "policy",
    "Hot_money Analyst": "hot_money",
    "hot_money_tracker": "hot_money",
    "hot_money": "hot_money",
    "Hot Money Analyst": "hot_money",
    "Lockup Analyst": "lockup",
    "lockup_watcher": "lockup",
    "lockup": "lockup",
    "Lockup Watcher": "lockup",
    # Tool 节点
    "tools_market": "market",
    "tools_social": "social",
    "tools_news": "news",
    "tools_fundamentals": "fundamentals",
    "tools_policy": "policy",
    "tools_hot_money": "hot_money",
    "tools_lockup": "lockup",
    # 下游节点
    "quality_gate": "quality_gate",
    "Data Quality Gate": "quality_gate",
    "Bull Analyst": "debate",
    "bull_researcher": "debate",
    "Bear Analyst": "debate",
    "bear_researcher": "debate",
    "Research Manager": "debate",
    "research_manager": "debate",
    "debate": "debate",
    "Trader": "trader",
    "trader": "trader",
    "Aggressive Analyst": "risk",
    "aggressive_debator": "risk",
    "Conservative Analyst": "risk",
    "conservative_debator": "risk",
    "Neutral Analyst": "risk",
    "neutral_debator": "risk",
    "risk": "risk",
    "Portfolio Manager": "pm",
    "portfolio_manager": "pm",
    "pm": "pm",
}

# 已知工具与 Agent 的归属映射
_KNOWN_TOOL_AGENT_MAP = {
    "get_stock_data": "market",
    "get_indicators": "market",
    "get_balance_sheet": "fundamentals",
    "get_cashflow": "fundamentals",
    "get_income_statement": "fundamentals",
    "get_profit_forecast": "fundamentals",
    "get_industry_comparison": "fundamentals",
    "get_fundamentals": "fundamentals",
    "get_lockup_expiry": "lockup",
    "get_dragon_tiger_board": "hot_money",
    "get_northbound_flow": "hot_money",
    "get_concept_blocks": "hot_money",
    "get_fund_flow": "hot_money",
    "get_global_news": "news",
    "get_news": "news",
    "get_hot_stocks": "hot_money",
    "get_insider_transactions": "lockup",
}

# 关键词特征库，用于根据 System / Human Prompt 智能推断所属 Agent
_ANALYST_KEYWORDS: dict[str, list[str]] = {
    "market": [
        "技术分析师", "技术指标", "50 日简单均线", "close_50_sma", "close_200_sma",
        "close_10_ema", "macd", "boll", "rsi", "vwma", "Technical Analyst",
        "均线类", "动量类", "波动率类", "成交量类",
    ],
    "social": [
        "市场情绪分析师", "情绪分析师", "散户情绪", "全市场情绪",
        "股吧", "雪球", "多空情绪", "Social Media Analyst", "Sentiment",
        "先看资金，再看新闻", "背离必须写出来",
    ],
    "news": [
        "新闻与政策分析师", "新闻分析师", "系统性新闻分析师", "News Analyst",
        "消息来源权重", "权威性筛选", "个股新闻", "宏观新闻",
    ],
    "fundamentals": [
        "基本面分析师", "CAS", "中国会计准则", "Fundamentals Analyst",
        "财务三表", "资产负债表", "利润表", "现金流量表", "PE/PB", "盈利预测",
        "营业收入", "归母净利润", "ROE",
    ],
    "policy": [
        "政策分析师", "政策市", "Policy Analyst", "宏观政策",
        "产业政策", "监管政策", "新质生产力", "证监会", "产业扶持",
    ],
    "hot_money": [
        "游资与资金流向追踪分析师", "游资追踪师", "主力资金", "龙虎榜",
        "游资席位", "北向资金", "Hot Money", "连板分析", "资金博弈",
        "大盘资金流",
    ],
    "lockup": [
        "解禁与减持监控分析师", "解禁监控师", "限售股", "减持新规",
        "上市公司股东减持股份管理暂行办法", "Lockup", "大股东减持", "解禁日历",
        "限售解禁", "破发", "破净",
    ],
    "quality_gate": [
        "数据质量审核员", "数据质量审核报告", "数据质量门控", "硬检查结果", "LLM 复审",
    ],
    "debate": [
        "Bull Analyst", "Bear Analyst",
        "A-Share Bull Framework", "A-Share Bear Framework",
        "debate facilitator", "critically evaluate this round of debate",
        "advocating for investing", "making the case against",
        "Last bear argument", "Last bull argument", "Debate History:",
    ],
    "trader": [
        "specialising in A-share", "trading agent specialising",
        "Translate the Research Manager", "transaction proposal", "transaction view",
        "Proposed Investment Plan", "Trader", "交易策略师", "交易计划",
    ],
    "risk": [
        "Aggressive Risk Analyst", "Conservative Risk Analyst", "Neutral Risk Analyst",
        "Aggressive Analyst", "Conservative Analyst", "Neutral Analyst",
        "A-Share Aggressive Framework", "A-Share Conservative Framework", "A-Share Neutral Framework",
        "三方风控", "风控评估",
    ],
    "pm": [
        "Portfolio Manager", "synthesize the risk analysts' debate",
        "final trading decision", "投资总监", "基金经理", "投资决策委员会",
    ],
}


def _format_message(msg: Any) -> dict[str, Any]:
    """Format a LangChain BaseMessage or dict into a standard telemetry dict."""
    if isinstance(msg, dict):
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        name = msg.get("name")
        tool_calls = msg.get("tool_calls")
        tool_call_id = msg.get("tool_call_id")
    else:
        if isinstance(msg, SystemMessage) or getattr(msg, "type", "") == "system":
            role = "system"
        elif isinstance(msg, HumanMessage) or getattr(msg, "type", "") == "human":
            role = "human"
        elif isinstance(msg, AIMessage) or getattr(msg, "type", "") == "ai":
            role = "ai"
        elif isinstance(msg, ToolMessage) or getattr(msg, "type", "") == "tool":
            role = "tool"
        elif isinstance(msg, FunctionMessage) or getattr(msg, "type", "") == "function":
            role = "function"
        else:
            role = getattr(msg, "type", "unknown")

        content = getattr(msg, "content", "")
        if isinstance(content, list):
            content = json.dumps(content, ensure_ascii=False)
        else:
            content = str(content) if content is not None else ""

        name = getattr(msg, "name", None)
        tool_calls = getattr(msg, "tool_calls", None)
        tool_call_id = getattr(msg, "tool_call_id", None)

    record: dict[str, Any] = {
        "role": role,
        "content": content,
        "timestamp": time.time(),
    }
    if name:
        record["name"] = str(name)
    if tool_calls:
        record["tool_calls"] = tool_calls
    if tool_call_id:
        record["tool_call_id"] = str(tool_call_id)
    return record


class AgentDebugCallbackHandler(BaseCallbackHandler):
    """Callback handler capturing full Agent telemetry: Prompts, Tools, and LLM responses."""

    def __init__(self, tracker: Optional[Any] = None) -> None:
        super().__init__()
        self.tracker = tracker
        self._lock = threading.Lock()

        # Active executions: run_id -> dict
        self._active_llms: dict[Union[str, UUID], dict[str, Any]] = {}
        self._active_tools: dict[Union[str, UUID], dict[str, Any]] = {}
        self._run_to_agent: dict[Union[str, UUID], str] = {}

        # Local fallback telemetry store
        self._prompts: dict[str, list[dict[str, Any]]] = {}
        self._tool_details: dict[str, list[dict[str, Any]]] = {}
        self._raw_responses: dict[str, list[str]] = {}
        self._metrics: dict[str, dict[str, Any]] = {}

    def _identify_agent(
        self,
        messages_or_prompts: Union[List[BaseMessage], List[str], str],
        serialized: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        tags: Optional[List[str]] = None,
        parent_run_id: Optional[Union[str, UUID]] = None,
    ) -> str:
        """Intelligently identify agent by metadata, tags, parent run, tool bindings, or prompt text."""
        # 1. Metadata check
        if metadata:
            for key in ("langgraph_node", "node_name", "agent_name", "agent", "name"):
                val = metadata.get(key)
                if val and str(val) in _NODE_TO_AGENT:
                    return _NODE_TO_AGENT[str(val)]

        # 2. Tags check
        if tags:
            for tag in tags:
                if str(tag) in _NODE_TO_AGENT:
                    return _NODE_TO_AGENT[str(tag)]

        # 3. Parent run lookup
        if parent_run_id and parent_run_id in self._run_to_agent:
            return self._run_to_agent[parent_run_id]

        # 4. Text content keyword matching
        full_text = ""
        if isinstance(messages_or_prompts, str):
            full_text = messages_or_prompts
        elif isinstance(messages_or_prompts, list):
            parts = []
            for item in messages_or_prompts:
                if isinstance(item, str):
                    parts.append(item)
                elif hasattr(item, "content"):
                    parts.append(str(item.content))
                elif isinstance(item, dict):
                    parts.append(str(item.get("content", "")))
            full_text = "\n".join(parts)

        best_agent = ""
        max_matches = 0

        for aid, keywords in _ANALYST_KEYWORDS.items():
            count = sum(1 for kw in keywords if kw in full_text)
            if count > max_matches:
                max_matches = count
                best_agent = aid

        if best_agent and max_matches > 0:
            return best_agent

        # 5. Tracker active stage fallback
        if self.tracker and getattr(self.tracker, "current_stage", None):
            return self.tracker.current_stage

        return "market"

    def _identify_tool_agent(
        self,
        tool_name: str,
        parent_run_id: Optional[Union[str, UUID]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        tags: Optional[List[str]] = None,
    ) -> str:
        """Identify which agent executed the tool."""
        if parent_run_id and parent_run_id in self._run_to_agent:
            return self._run_to_agent[parent_run_id]

        if metadata:
            for key in ("langgraph_node", "node_name", "agent_name", "agent"):
                val = metadata.get(key)
                if val and str(val) in _NODE_TO_AGENT:
                    return _NODE_TO_AGENT[str(val)]

        if tags:
            for tag in tags:
                if str(tag) in _NODE_TO_AGENT:
                    return _NODE_TO_AGENT[str(tag)]

        if tool_name in _KNOWN_TOOL_AGENT_MAP:
            return _KNOWN_TOOL_AGENT_MAP[tool_name]

        if self.tracker and getattr(self.tracker, "current_stage", None):
            return self.tracker.current_stage

        return "market"

    def on_chat_model_start(
        self,
        serialized: Dict[str, Any],
        messages: List[List[BaseMessage]],
        *,
        run_id: Optional[UUID] = None,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        """Extract full Prompt messages and map LLM call to its Agent."""
        if not messages or not messages[0]:
            return

        rid = run_id or uuid4()
        batch_messages = messages[0]
        agent_id = self._identify_agent(
            batch_messages,
            serialized=serialized,
            metadata=metadata,
            tags=tags,
            parent_run_id=parent_run_id,
        )

        with self._lock:
            self._run_to_agent[rid] = agent_id
            if parent_run_id:
                self._run_to_agent[parent_run_id] = agent_id

            formatted_messages = [_format_message(m) for m in batch_messages]
            self._active_llms[rid] = {
                "agent_id": agent_id,
                "start_time": time.time(),
                "messages": formatted_messages,
            }
            self._prompts[agent_id] = formatted_messages

        if self.tracker:
            self.tracker.record_agent_prompt(agent_id, formatted_messages)
            if self.tracker.get_agent_status(agent_id) not in ("done", "error"):
                self.tracker.set_agent_status(agent_id, "running", "正在执行大模型推理...")

    def on_llm_start(
        self,
        serialized: Dict[str, Any],
        prompts: List[str],
        *,
        run_id: Optional[UUID] = None,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        """Extract prompt strings for non-chat LLM models."""
        if not prompts:
            return

        rid = run_id or uuid4()
        agent_id = self._identify_agent(
            prompts,
            serialized=serialized,
            metadata=metadata,
            tags=tags,
            parent_run_id=parent_run_id,
        )

        with self._lock:
            self._run_to_agent[rid] = agent_id
            if parent_run_id:
                self._run_to_agent[parent_run_id] = agent_id

            formatted_messages = [
                {"role": "user", "content": p, "timestamp": time.time()} for p in prompts
            ]
            self._active_llms[rid] = {
                "agent_id": agent_id,
                "start_time": time.time(),
                "messages": formatted_messages,
            }
            self._prompts[agent_id] = formatted_messages

        if self.tracker:
            self.tracker.record_agent_prompt(agent_id, formatted_messages)
            if self.tracker.get_agent_status(agent_id) not in ("done", "error"):
                self.tracker.set_agent_status(agent_id, "running", "正在执行大模型推理...")

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: Optional[UUID] = None,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        """Record raw response, thinking process (<think> tags / reasoning_content), and tokens."""
        rid = run_id
        with self._lock:
            info = self._active_llms.pop(rid, None) if rid else None

        if info:
            agent_id = info["agent_id"]
            start_time = info["start_time"]
            duration = max(0.0, time.time() - start_time)
        else:
            agent_id = self._run_to_agent.get(rid) or self._run_to_agent.get(parent_run_id) or "market"
            duration = 0.0

        raw_text = ""
        thinking_content = ""
        tok_in = 0
        tok_out = 0

        try:
            if response.generations and response.generations[0]:
                generation = response.generations[0][0]
                raw_text = generation.text or ""
                msg = getattr(generation, "message", None)
                if msg:
                    if not raw_text and hasattr(msg, "content"):
                        raw_text = str(msg.content)

                    # Extract reasoning content (DeepSeek / OpenAI reasoning)
                    reasoning = getattr(msg, "reasoning_content", None)
                    if not reasoning and hasattr(msg, "additional_kwargs") and isinstance(msg.additional_kwargs, dict):
                        reasoning = msg.additional_kwargs.get("reasoning_content") or msg.additional_kwargs.get("thinking")
                    if not reasoning and hasattr(msg, "response_metadata") and isinstance(msg.response_metadata, dict):
                        reasoning = msg.response_metadata.get("reasoning_content") or msg.response_metadata.get("thinking")
                    if reasoning:
                        thinking_content = str(reasoning)

                    # Extract token metadata
                    if hasattr(msg, "usage_metadata") and isinstance(msg.usage_metadata, dict):
                        tok_in = msg.usage_metadata.get("input_tokens", 0)
                        tok_out = msg.usage_metadata.get("output_tokens", 0)
        except Exception as exc:
            logger.debug("Error parsing LLM response in debug callback: %s", exc)

        # Regex extraction of <think>...</think> in raw text
        if "<think>" in raw_text:
            think_match = re.search(r"<think>(.*?)</think>", raw_text, flags=re.DOTALL)
            if think_match:
                extracted_think = think_match.group(1).strip()
                if thinking_content:
                    thinking_content = f"{thinking_content}\n{extracted_think}"
                else:
                    thinking_content = extracted_think

        # Fallback to response.llm_output for token usage if usage_metadata was empty
        if tok_in == 0 and tok_out == 0 and response.llm_output:
            token_usage = response.llm_output.get("token_usage") or response.llm_output.get("usage") or {}
            tok_in = token_usage.get("prompt_tokens") or token_usage.get("input_tokens") or 0
            tok_out = token_usage.get("completion_tokens") or token_usage.get("output_tokens") or 0

        with self._lock:
            responses = self._raw_responses.setdefault(agent_id, [])
            formatted_resp = (
                f"<think>\n{thinking_content}\n</think>\n\n{raw_text}"
                if thinking_content and "<think>" not in raw_text
                else raw_text
            )
            responses.append(formatted_resp)

            m = self._metrics.setdefault(
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
            m["tokens_in"] += tok_in
            m["tokens_out"] += tok_out
            m["total_tokens"] += tok_in + tok_out
            m["llm_calls"] += 1
            m["total_duration"] = round(m["total_duration"] + duration, 3)

        if self.tracker:
            self.tracker.record_agent_response(agent_id, raw_response=raw_text, thinking=thinking_content)
            self.tracker.record_agent_metrics(
                agent_id,
                tokens_in=tok_in,
                tokens_out=tok_out,
                duration=duration,
                is_llm=True,
            )

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: Optional[UUID] = None,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        """Handle LLM errors."""
        rid = run_id
        with self._lock:
            info = self._active_llms.pop(rid, None) if rid else None

        agent_id = info["agent_id"] if info else (self._run_to_agent.get(rid) or self._run_to_agent.get(parent_run_id) or "unknown")
        if self.tracker:
            self.tracker.set_agent_status(agent_id, "error", f"LLM 错误: {str(error)[:50]}")

    def on_tool_start(
        self,
        serialized: Dict[str, Any],
        input_str: str,
        *,
        run_id: Optional[UUID] = None,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        inputs: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        """Record tool name, arguments, and start timestamp."""
        rid = run_id or uuid4()
        tool_name = (
            serialized.get("name")
            or (metadata.get("tool_name") if metadata else None)
            or kwargs.get("name")
            or "unknown_tool"
        )
        agent_id = self._identify_tool_agent(tool_name, parent_run_id, metadata, tags)

        args = inputs if inputs is not None else input_str
        if isinstance(args, str):
            try:
                parsed = json.loads(args)
                if isinstance(parsed, dict):
                    args = parsed
            except Exception:
                pass

        with self._lock:
            self._run_to_agent[rid] = agent_id
            self._active_tools[rid] = {
                "agent_id": agent_id,
                "tool_name": tool_name,
                "args": args,
                "start_time": time.time(),
            }

        if self.tracker:
            self.tracker.record_agent_tool(agent_id, tool_name)

    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: Optional[UUID] = None,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        """Record tool returned raw payload snapshot and execution duration."""
        rid = run_id
        with self._lock:
            info = self._active_tools.pop(rid, None) if rid else None

        if info:
            agent_id = info["agent_id"]
            tool_name = info["tool_name"]
            args = info["args"]
            start_time = info["start_time"]
            duration = max(0.0, time.time() - start_time)
        else:
            agent_id = self._run_to_agent.get(rid) or self._run_to_agent.get(parent_run_id) or "market"
            tool_name = "tool"
            args = {}
            duration = 0.0

        # Extract payload snapshot
        raw_payload = output
        if hasattr(output, "content"):
            raw_payload = output.content
        elif hasattr(output, "to_dict"):
            try:
                raw_payload = output.to_dict(orient="records")
            except Exception:
                raw_payload = str(output)
        elif not isinstance(output, (dict, list, str, int, float, bool)):
            raw_payload = str(output)

        detail_record = {
            "tool_name": tool_name,
            "args": args,
            "output": raw_payload,
            "duration": round(duration, 3),
            "timestamp": time.time(),
            "status": "success",
        }

        with self._lock:
            self._tool_details.setdefault(agent_id, []).append(detail_record)
            m = self._metrics.setdefault(
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
            m["tool_calls"] += 1
            m["total_duration"] = round(m["total_duration"] + duration, 3)

        if self.tracker:
            self.tracker.record_agent_tool_detail(agent_id, detail_record)
            self.tracker.record_agent_metrics(agent_id, duration=duration, is_tool=True)

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: Optional[UUID] = None,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        """Record tool execution error."""
        rid = run_id
        with self._lock:
            info = self._active_tools.pop(rid, None) if rid else None

        if info:
            agent_id = info["agent_id"]
            tool_name = info["tool_name"]
            args = info["args"]
            start_time = info["start_time"]
            duration = max(0.0, time.time() - start_time)
        else:
            agent_id = self._run_to_agent.get(rid) or self._run_to_agent.get(parent_run_id) or "market"
            tool_name = "tool"
            args = {}
            duration = 0.0

        detail_record = {
            "tool_name": tool_name,
            "args": args,
            "output": f"ERROR: {type(error).__name__}: {str(error)}",
            "duration": round(duration, 3),
            "timestamp": time.time(),
            "status": "error",
        }

        with self._lock:
            self._tool_details.setdefault(agent_id, []).append(detail_record)
            m = self._metrics.setdefault(
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
            m["tool_calls"] += 1
            m["total_duration"] = round(m["total_duration"] + duration, 3)

        if self.tracker:
            self.tracker.record_agent_tool_detail(agent_id, detail_record)
            self.tracker.record_agent_metrics(agent_id, duration=duration, is_tool=True)
