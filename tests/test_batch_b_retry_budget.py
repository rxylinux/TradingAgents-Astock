"""Batch B: unified retry limit + graph timeout/retry passthrough.

Offline: httpx.MockTransport, exact transport-level request counts.
"""

import time
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from langchain_core.messages import HumanMessage
from pydantic import BaseModel

from tradingagents.llm_clients.openai_client import NormalizedChatOpenAI


class _Answer(BaseModel):
    answer: str


class _Tracker:
    def __init__(self, statuses):
        self.statuses = statuses
        self.request_count = 0

    def __call__(self, request):
        self.request_count += 1
        item = self.statuses[min(self.request_count - 1, len(self.statuses) - 1)]
        if isinstance(item, int):
            return httpx.Response(
                item, json={"error": {"message": "offline", "type": "rate_limit_error"}},
                headers={"Retry-After": "0"}, request=request)
        return httpx.Response(200, json={
            "id": "offline", "object": "chat.completion", "created": 0,
            "model": "offline-test",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": item},
                          "finish_reason": "stop"}]}, request=request)


def _make_client(tracker, **kw):
    d = dict(model="offline-test", api_key="offline-key", max_retries=2, timeout=1,
             use_responses_api=False)
    d.update(kw)
    return NormalizedChatOpenAI(
        http_client=httpx.Client(transport=httpx.MockTransport(tracker)), **d)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)


class TestSDKRetrySemantics:
    @pytest.mark.parametrize("retries,expected", [(0, 1), (2, 3), (5, 6)])
    def test_exact_total_attempts(self, retries, expected):
        t = _Tracker([429])
        c = _make_client(t, max_retries=retries)
        with pytest.raises(Exception):
            c.invoke([HumanMessage(content="x")])
        assert t.request_count == expected

    def test_401_fails_fast(self):
        t = _Tracker([401])
        c = _make_client(t, max_retries=5)
        with pytest.raises(Exception):
            c.invoke([HumanMessage(content="x")])
        assert t.request_count == 1

    def test_transient_429_then_success(self):
        t = _Tracker([429, 429, "ok"])
        c = _make_client(t, max_retries=5)
        result = c.invoke([HumanMessage(content="x")])
        assert result is not None
        assert t.request_count == 3


class TestStructuredFallbackBudget:
    def test_rate_limit_exhausted_no_fallback(self):
        from tradingagents.agents.utils.structured import invoke_structured_or_freetext
        t = _Tracker([429])
        c = _make_client(t, max_retries=2)
        s = c.with_structured_output(_Answer, method="json_mode")
        with pytest.raises(Exception):
            invoke_structured_or_freetext(s, c, "x", lambda v: v.answer, "test")
        assert t.request_count == 3

    def test_parse_failure_fallback_within_budget(self):
        from tradingagents.agents.utils.structured import invoke_structured_or_freetext
        t = _Tracker(["not-json", "fallback-ok"])
        c = _make_client(t, max_retries=2)
        s = c.with_structured_output(_Answer, method="json_mode")
        result = invoke_structured_or_freetext(s, c, "x", lambda v: v.answer, "test")
        assert result == "fallback-ok"
        assert t.request_count == 2

    def test_plain_only_full_budget(self):
        from tradingagents.agents.utils.structured import invoke_structured_or_freetext
        t = _Tracker(["ok"])
        c = _make_client(t, max_retries=0)
        result = invoke_structured_or_freetext(None, c, "x", str, "test")
        assert result == "ok"
        assert t.request_count == 1


class TestGraphTimeoutRetryPassthrough:
    def test_explicit_zero_forwarded(self):
        from tradingagents.graph.trading_graph import TradingAgentsGraph
        g = SimpleNamespace(config={"llm_provider": "openai", "max_retries": 0, "timeout": 2.5})
        kw = TradingAgentsGraph._get_provider_kwargs(g)
        assert kw.get("max_retries") == 0
        assert kw.get("timeout") == 2.5

    def test_absent_omitted(self):
        from tradingagents.graph.trading_graph import TradingAgentsGraph
        g = SimpleNamespace(config={"llm_provider": "openai"})
        kw = TradingAgentsGraph._get_provider_kwargs(g)
        assert "max_retries" not in kw
        assert "timeout" not in kw

    def test_nonzero_forwarded(self):
        from tradingagents.graph.trading_graph import TradingAgentsGraph
        g = SimpleNamespace(config={"llm_provider": "openai", "max_retries": 3, "timeout": 30})
        kw = TradingAgentsGraph._get_provider_kwargs(g)
        assert kw.get("max_retries") == 3
        assert kw.get("timeout") == 30
