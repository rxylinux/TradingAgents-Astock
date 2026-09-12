"""Codex independent HTTP-level retry checks; implementation agent must not edit."""

from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import json

import httpx
import pytest
from openai import InternalServerError, RateLimitError
from langchain_core.exceptions import OutputParserException
from pydantic import BaseModel

from tradingagents.agents.utils.structured import invoke_structured_or_freetext
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.llm_clients.openai_client import NormalizedChatOpenAI


class Answer(BaseModel):
    answer: str


def make_client(statuses, retries):
    requests = []

    def handler(request):
        requests.append(request)
        item = statuses[min(len(requests) - 1, len(statuses) - 1)]
        if isinstance(item, int):
            return httpx.Response(item, json={"error": {"message": "offline failure"}}, request=request)
        return httpx.Response(200, json={
            "id": "offline", "object": "chat.completion", "created": 0,
            "model": "offline-test", "choices": [{"index": 0,
                "message": {"role": "assistant", "content": item}, "finish_reason": "stop"}],
        }, request=request)

    client = NormalizedChatOpenAI(
        model="offline-test", api_key="offline-placeholder", max_retries=retries,
        timeout=1, http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        use_responses_api=False,
    )
    return client, requests


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)


def test_plain_only_helper_can_call_real_pydantic_client():
    client, requests = make_client(["plain-ok"], 0)
    assert invoke_structured_or_freetext(None, client, "test", str, "audit") == "plain-ok"
    assert len(requests) == 1


@pytest.mark.parametrize("retries", [0, 2])
def test_configured_retries_define_exact_total_http_attempts(retries):
    client, requests = make_client([429], retries)
    with pytest.raises(RateLimitError):
        client.invoke("test")
    assert len(requests) == 1 + retries


def test_actual_bound_structured_call_uses_same_budget_without_reset():
    client, requests = make_client([429], 2)
    structured = client.with_structured_output(Answer, method="json_mode")
    with pytest.raises(RateLimitError):
        invoke_structured_or_freetext(structured, client, "test", lambda value: value.answer, "audit")
    assert len(requests) == 3


def test_actual_parser_failure_can_fallback_with_remaining_budget():
    client, requests = make_client(["invalid-json", "plain-ok"], 2)
    structured = client.with_structured_output(Answer, method="json_mode")
    assert invoke_structured_or_freetext(
        structured, client, "test", lambda value: value.answer, "audit"
    ) == "plain-ok"
    assert len(requests) == 2


def test_graph_forwards_explicit_zero_retries_and_timeout():
    graph = SimpleNamespace(config={"llm_provider": "openai", "max_retries": 0, "timeout": 2.5})
    kwargs = TradingAgentsGraph._get_provider_kwargs(graph)
    assert kwargs.get("max_retries") == 0
    assert kwargs.get("timeout") == 2.5


def test_plain_only_helper_preserves_configured_retry_budget():
    client, requests = make_client([429], 2)
    with pytest.raises(RateLimitError):
        invoke_structured_or_freetext(None, client, "test", str, "audit")
    assert len(requests) == 3


@pytest.mark.parametrize("statuses,retries", [(["invalid-json"], 0), ([429, 429, "invalid-json"], 2)])
def test_parser_failure_after_all_attempts_does_not_add_request(statuses, retries):
    client, requests = make_client(statuses, retries)
    structured = client.with_structured_output(Answer, method="json_mode")
    with pytest.raises(OutputParserException):
        invoke_structured_or_freetext(structured, client, "test", lambda value: value.answer, "audit")
    assert len(requests) == 1 + retries


def test_exhausted_server_error_does_not_start_fallback_chain():
    client, requests = make_client([500], 2)
    structured = client.with_structured_output(Answer, method="json_mode")
    with pytest.raises(InternalServerError):
        invoke_structured_or_freetext(structured, client, "test", lambda value: value.answer, "audit")
    assert len(requests) == 3


def test_concurrent_fallback_cannot_reduce_other_calls_retries():
    fallback_entered = Event()
    release_fallback = Event()
    calls = {"parse": 0, "other": 0}

    def handler(request):
        tag = json.loads(request.content)["messages"][0]["content"]
        calls[tag] += 1
        if tag == "other":
            return httpx.Response(429, json={"error": {"message": "offline"}}, request=request)
        if calls[tag] == 2:
            fallback_entered.set()
            assert release_fallback.wait(5), "independent concurrent request did not finish"
        content = "invalid-json" if calls[tag] == 1 else "plain-ok"
        return httpx.Response(200, json={
            "id": "offline", "object": "chat.completion", "created": 0,
            "model": "offline-test", "choices": [{"index": 0,
                "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        }, request=request)

    client = NormalizedChatOpenAI(
        model="offline-test", api_key="offline-placeholder", max_retries=2,
        timeout=1, http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        use_responses_api=False,
    )
    structured = client.with_structured_output(Answer, method="json_mode")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(invoke_structured_or_freetext, structured, client, "parse", str, "audit")
        try:
            assert fallback_entered.wait(5), "parse fallback did not start"
            with pytest.raises(RateLimitError):
                client.invoke("other")
        finally:
            release_fallback.set()
        assert future.result(timeout=5) == "plain-ok"
    assert calls["other"] == 3
