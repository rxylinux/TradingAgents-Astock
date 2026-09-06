"""A01: CLI 真实运行生命周期集成回归（真实图 + 真实 SQLite 断点）。

`run_analysis(checkpoint=True)` 必须经 `prepare_graph_run` / `stream` /
`finalize_graph_run` / `finally close_graph_run`：中途失败保留断点，第二次
执行从真实 SQLite 断点恢复且已完成的分析师不重复调用 LLM，成功后写入交
易记忆、保存统一 JSON 报告并清理断点。交互 UI 与 LLM 全部离线（MockChat
不联网，崩溃经回调注入）。
"""

import json
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from cli import main
from cli.models import AnalystType
from cli.stats_handler import StatsCallbackHandler
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.checkpointer import has_checkpoint


# LLM 调用计数（跨运行累计，用于验证恢复后不重复执行分析师）
_MOCK_CHAT_CALLS = {"n": 0}


class _MockChat(BaseChatModel):
    """离线 LLM：直接返回固定回复，不联网。"""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        _MOCK_CHAT_CALLS["n"] += 1
        text = "【离线报告】" + "详细分析内容" * 30 + "\n| 指标 | 结论 |\n|---|---|\n| 综合 | 通过 |"
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])

    @property
    def _llm_type(self):
        return "mock-chat"

    def bind_tools(self, tools, **kwargs):
        return self


class _MockLLMClient:
    def __init__(self, **kwargs):
        self._llm = _MockChat()

    def get_llm(self):
        return self._llm


class _CrashAtLLMHandler(StatsCallbackHandler):
    """透传统计 handler（保留原生计数；崩溃注入改由节点工厂完成——
    LangChain 会吞掉回调异常，不能靠 on_llm_start 打断真实图）。"""


def _selections():
    return dict(
        ticker="600519",
        analysis_date="2026-01-15",
        research_depth=1,
        shallow_thinker="offline-model",
        deep_thinker="offline-model",
        backend_url=None,
        llm_provider="glm",
        analysts=[AnalystType.MARKET, AnalystType.NEWS],
        analysis_type="个股",
    )


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    """隔离所有持久化路径与交互 UI；图用真实 TradingAgentsGraph + 离线 LLM。"""
    crash_qg = {"on": False}

    def make_handler():
        return _CrashAtLLMHandler()

    monkeypatch.setattr(main, "DEFAULT_CONFIG", {
        **DEFAULT_CONFIG,
        "results_dir": str(tmp_path / "logs"),
        "data_cache_dir": str(tmp_path / "cache"),
        "memory_log_path": str(tmp_path / "memory" / "trading_memory.md"),
    })
    monkeypatch.setattr(main, "StatsCallbackHandler", make_handler)
    monkeypatch.setattr(main, "get_user_selections", lambda **kw: _selections())
    monkeypatch.setattr(main, "create_layout", Mock())
    monkeypatch.setattr(main, "update_display", Mock())
    monkeypatch.setattr(main, "Live", lambda *a, **kw: nullcontext())
    monkeypatch.setattr(main, "console", Mock())
    monkeypatch.setattr(main.typer, "prompt", lambda *a, **kw: "N")

    _MOCK_CHAT_CALLS["n"] = 0
    return {"tmp_path": tmp_path, "crash_qg": crash_qg}


def test_cli_crash_then_resume_from_real_sqlite_checkpoint(cli_env):
    """中途失败保留断点；第二次执行真实恢复、不重复分析师、正确收尾。"""
    tmp_path = cli_env["tmp_path"]
    crash_qg = cli_env["crash_qg"]
    ticker, date = "600519", "2026-01-15"

    from tradingagents.agents.quality_gate import create_quality_gate as real_qg

    def flaky_quality_gate(llm, **kwargs):  # kwargs: active_analysts_fallback 等新签名参数
        if crash_qg["on"]:
            def qg_node(state):
                raise RuntimeError("simulated crash mid-run")
            return qg_node
        return real_qg(llm)

    def _switch_dirs(tag):
        """每个阶段独立的 cache/memory 目录（断点与记忆互不串扰）。"""
        main.DEFAULT_CONFIG.update({
            "data_cache_dir": str(tmp_path / f"cache-{tag}"),
            "memory_log_path": str(tmp_path / f"memory-{tag}" / "trading_memory.md"),
            "results_dir": str(tmp_path / f"logs-{tag}"),
        })
        _MOCK_CHAT_CALLS["n"] = 0

    with patch(
        "tradingagents.graph.trading_graph.create_llm_client",
        return_value=_MockLLMClient(),
    ), patch(
        "tradingagents.graph.setup.create_quality_gate", flaky_quality_gate,
    ):
        # ── 基准：一次完整运行（不崩溃）的 LLM 调用总数 ──
        # structured 节点（Research Manager/Trader/PM）失败后会以自由文本
        # 重试一次，逐节点计数依赖实现细节——用对照基线代替手算。
        crash_qg["on"] = False
        _switch_dirs("baseline")
        main.run_analysis(checkpoint=True)
        baseline_calls = _MOCK_CHAT_CALLS["n"]
        assert baseline_calls > 2, baseline_calls
        assert has_checkpoint(
            str(tmp_path / "cache-baseline"), ticker, date
        ) is False, "成功完成后必须清理断点"

        # ── 第一次执行：在质量门控中途崩溃（两位分析师已完成）──
        crash_qg["on"] = True
        _switch_dirs("crash")
        with pytest.raises(RuntimeError, match="simulated crash mid-run"):
            main.run_analysis(checkpoint=True)

        cache_dir = str(tmp_path / "cache-crash")
        assert has_checkpoint(cache_dir, ticker, date) is True, (
            "中途失败必须保留 SQLite 断点供下次恢复"
        )
        llm_calls_after_crash = _MOCK_CHAT_CALLS["n"]
        assert llm_calls_after_crash == 2, (
            f"崩溃前应恰好完成两位分析师（2 次 LLM 调用），实际 {llm_calls_after_crash}"
        )
        # 尚未写入决策
        memory_path = tmp_path / "memory-crash" / "trading_memory.md"
        assert not memory_path.exists() or "600519" not in memory_path.read_text(encoding="utf-8")

        # ── 第二次执行：从真实 SQLite 断点恢复（同一 cache/memory 目录）──
        crash_qg["on"] = False
        calls_before_resume = _MOCK_CHAT_CALLS["n"]
        main.run_analysis(checkpoint=True)

    # 1. 成功后断点被清理
    assert has_checkpoint(cache_dir, ticker, date) is False

    # 2. 统一 JSON 状态报告已落盘（Web 历史列表识别的格式）
    json_path = (
        tmp_path / "logs-crash" / ticker / "TradingAgentsStrategy_logs"
        / f"full_states_log_{date}.json"
    )
    assert json_path.exists(), "finalize_graph_run 未保存统一 JSON 报告"
    saved = json.loads(json_path.read_text(encoding="utf-8"))
    assert saved.get("final_trade_decision"), "统一报告缺少最终决策"

    # 3. 交易记忆写入一笔决策（供后续回填）
    memory_text = memory_path.read_text(encoding="utf-8")
    assert f"[{date} | {ticker} |" in memory_text and "pending]" in memory_text

    # 4. 已完成的分析师没有重复执行：恢复阶段补跑的 LLM 调用数恰好等于
    #    完整运行减去两位分析师（崩溃前 2 次）。
    resume_calls = _MOCK_CHAT_CALLS["n"] - calls_before_resume
    assert resume_calls == baseline_calls - 2, (
        f"恢复阶段调用了 {resume_calls} 次 LLM，期望 {baseline_calls - 2} "
        f"（完整运行 {baseline_calls} 次减两位分析师）——已完成分析师被重复执行？"
    )


def test_cli_message_log_includes_branch_channel_tools(cli_env, tmp_path):
    """分支通道（{role}_messages）中的分析师工具调用消息必须进入消息日志。

    R1 隔离后共享 messages 只剩初始输入；只读共享通道的日志会漏掉分析师
    的工具调用。此处直接验证 CLI 的消息采集函数覆盖分支通道。
    """
    from langchain_core.messages import AIMessage, HumanMessage

    chunk = {
        "messages": [HumanMessage(content="600519")],
        "market_messages": [
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "get_stock_data",
                    "args": {"ticker": "600519"},
                    "id": "c1",
                    "type": "tool_call",
                }],
            )
        ],
        "news_messages": [AIMessage(content="news report")],
    }

    names = [m.__class__.__name__ for m in main.iter_analysis_messages(chunk)]
    # 共享 1 条 + market 1 条 + news 1 条
    assert names.count("AIMessage") == 2, names
    tools = [
        tc["name"]
        for m in main.iter_analysis_messages(chunk)
        for tc in (getattr(m, "tool_calls", None) or [])
    ]
    assert "get_stock_data" in tools, "分支通道中的工具调用未被采集"


def test_repeated_runs_do_not_pollute_previous_logs(cli_env):
    """同进程两次 run_analysis：日志装饰在运行结束（含异常）后恢复，
    第二次运行不得把新标的写进旧标的 message_tool.log。"""
    tmp_path = cli_env["tmp_path"]
    date = "2026-01-15"

    def selections_for(ticker):
        sel = _selections()
        sel["ticker"] = ticker
        return sel

    selection_calls = iter([selections_for("600519"), selections_for("000001")])
    main.get_user_selections = lambda **kw: next(selection_calls)

    graphs = iter([_offline_graph(), _offline_graph()])
    with patch.object(main, "TradingAgentsGraph",
                      side_effect=lambda *a, **kw: next(graphs)):
        main.run_analysis(checkpoint=True)
        first_log = (
            tmp_path / "logs" / "600519" / date / "message_tool.log"
        )
        previous = first_log.read_text(encoding="utf-8")

        main.run_analysis(checkpoint=True)

    assert "600519" in previous
    assert first_log.read_text(encoding="utf-8") == previous, (
        "第二次运行把新标的的日志写进了旧标的文件（装饰闭包未恢复）"
    )
    second_log = tmp_path / "logs" / "000001" / date / "message_tool.log"
    assert second_log.exists(), "第二次运行未生成自己的日志"


class _OfflineGraph:
    """最小离线图替身：走真实生命周期方法签名，不依赖 LangGraph。"""

    def __init__(self):
        from tradingagents.graph.propagation import Propagator

        self.propagator = Propagator()
        self.closed = 0
        self.finalized = 0

    def prepare_graph_run(self, ticker, date, callbacks=None):
        state = self.propagator.create_initial_state(ticker, date)
        state["final_trade_decision"] = "Rating: Buy"
        return state, {}, None

    class _G:
        def stream(self, state, **kwargs):
            return iter([state])

    graph = _G()

    def finalize_graph_run(self, ticker, date, final_state):
        self.finalized += 1
        return "Buy"

    def close_graph_run(self):
        self.closed += 1

    def bind_run_context(self):
        return object()  # 测试桩：CLI 生命周期需要该接缝（A05）

    def release_run_context(self, token):
        pass


def _offline_graph():
    return _OfflineGraph()
