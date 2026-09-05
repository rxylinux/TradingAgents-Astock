"""Web 进度检测对分析师分支消息通道的适配（R1）。

分析师工具循环运行在 ``{role}_messages`` 分支通道上后，共享 ``messages``
通道只剩初始输入。``_detect_completed_stages`` 必须扫描分支通道，否则
Web 进度面板看不到任何工具调用记录。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from web.progress import ProgressTracker
from web.runner import _detect_completed_stages


def _chunk_with_branch_messages(**branch_msgs):
    chunk = {"company_of_interest": "600519", "trade_date": "2026-08-01"}
    chunk.update(branch_msgs)
    return chunk


def test_branch_channel_tool_calls_are_recorded():
    tracker = ProgressTracker(ticker="600519", trade_date="2026-08-01")

    chunk = _chunk_with_branch_messages(
        messages=[("human", "600519")],
        market_messages=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "get_stock_data",
                        "args": {"ticker": "600519"},
                        "id": "c1",
                        "type": "tool_call",
                    }
                ],
            )
        ],
        news_messages=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "get_global_news",
                        "args": {"ticker": "600519"},
                        "id": "c2",
                        "type": "tool_call",
                    }
                ],
            )
        ],
    )

    _detect_completed_stages(chunk, tracker)

    market_tools = tracker.agent_tool_calls.get("market", [])
    news_tools = tracker.agent_tool_calls.get("news", [])
    assert "get_stock_data" in market_tools, f"market 工具记录缺失: {market_tools}"
    assert "get_global_news" in news_tools, f"news 工具记录缺失: {news_tools}"


def test_shared_channel_still_scanned_for_tool_names():
    tracker = ProgressTracker(ticker="600519", trade_date="2026-08-01")

    chunk = _chunk_with_branch_messages(
        messages=[("human", "600519")],
    )

    _detect_completed_stages(chunk, tracker)
    # 共享通道只有初始输入，不应产生工具记录，也不应报错
    assert not tracker.agent_tool_calls.get("market")
