"""Structured output helpers with invocation-local shared retry budget.

The structured call and free-text fallback consume from a single budget
(1 + user max_retries total HTTP requests) via a ContextVar. When the
budget is active, NormalizedChatOpenAI.invoke() makes exactly 1 HTTP
request without internal retry — this helper manages all retries.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_NON_RETRYABLE_TYPES: tuple = ()
_RETRYABLE_TYPES: tuple = ()
try:
    from openai import (
        APIConnectionError,
        APITimeoutError,
        AuthenticationError,
        BadRequestError,
        InternalServerError,
        PermissionDeniedError,
        RateLimitError,
    )
    _NON_RETRYABLE_TYPES = (AuthenticationError, PermissionDeniedError, BadRequestError)
    _RETRYABLE_TYPES = (RateLimitError, InternalServerError, APIConnectionError, APITimeoutError)
except ImportError:
    pass


def _get_retry_budget(llm: Any) -> int:
    """Total HTTP attempts (1 + user max_retries).

    Validates the return value is a positive non-bool int; falls back to
    default for clients/seams that don't implement the budget capability
    correctly (legacy mocks, older client versions). Real invalid config
    is rejected by NormalizedChatOpenAI at construction, not here.
    """
    if hasattr(llm, "_get_retry_budget"):
        try:
            budget = llm._get_retry_budget()
            if isinstance(budget, int) and not isinstance(budget, bool) and budget >= 1:
                return budget
        except Exception:
            pass
    return 3  # Default: 1 + 2 retries (matches NormalizedChatOpenAI default)


def bind_structured(llm: Any, schema: type[T], agent_name: str) -> Optional[Any]:
    """Return ``llm.with_structured_output(schema)`` or ``None`` if unsupported."""
    try:
        return llm.with_structured_output(schema)
    except (NotImplementedError, AttributeError) as exc:
        logger.warning(
            "%s: provider does not support with_structured_output (%s); "
            "falling back to free-text generation",
            agent_name, exc,
        )
        return None


def invoke_structured_or_freetext(
    structured_llm: Optional[Any],
    plain_llm: Any,
    prompt: Any,
    render: Callable[[T], str],
    agent_name: str,
) -> str:
    """Run structured call, fall back to free-text with shared retry budget.

    Budget: total = 1 + user's max_retries HTTP requests across BOTH paths.
    Uses a ContextVar to disable invoke()'s internal retry — this helper
    is the sole retry authority. Each invoke() makes exactly 1 request.
    """
    from tradingagents.llm_clients.openai_client import _shared_budget

    # Plain-only: no shared budget — invoke() handles its own retries
    if structured_llm is None:
        response = plain_llm.invoke(prompt)
        return response.content

    total_budget = _get_retry_budget(plain_llm)
    remaining = total_budget
    last_parse_exc: Optional[Exception] = None

    # Activate shared budget — invoke() will make 1 request per call
    token = _shared_budget.set(total_budget)
    try:
        # Structured retries (each attempt = 1 HTTP request)
        for attempt in range(total_budget):
            remaining = total_budget - attempt - 1
            _shared_budget.set(remaining + 1)  # Budget BEFORE this attempt
            try:
                result = structured_llm.invoke(prompt)
                return render(result)
            except _NON_RETRYABLE_TYPES:
                logger.warning(
                    "%s: structured-output auth/param error; no fallback", agent_name)
                raise
            except Exception as exc:
                if isinstance(exc, _RETRYABLE_TYPES):
                    if remaining > 0:
                        # Bounded exponential backoff (same policy as invoke())
                        time.sleep(min(2 ** attempt, 8) + 0.1)
                        continue
                    logger.warning(
                        "%s: retry budget exhausted (%d); no fallback",
                        agent_name, total_budget)
                    raise
                else:
                    # Parse/format failure — try fallback if budget remains
                    last_parse_exc = exc
                    logger.warning(
                        "%s: structured parse failure; %d remaining for fallback",
                        agent_name, remaining)
                    break

        # Fallback with remaining budget (each attempt = 1 HTTP request)
        if remaining <= 0:
            if last_parse_exc is not None:
                raise last_parse_exc
            raise RuntimeError(f"{agent_name}: retry budget exhausted")

        for attempt in range(remaining):
            _shared_budget.set(remaining - attempt)
            try:
                response = plain_llm.invoke(prompt)
                return response.content
            except _NON_RETRYABLE_TYPES:
                raise
            except Exception as exc:
                if isinstance(exc, _RETRYABLE_TYPES) and attempt < remaining - 1:
                    # Bounded exponential backoff (same policy as invoke())
                    time.sleep(min(2 ** attempt, 8) + 0.1)
                    continue
                raise

        raise RuntimeError(f"{agent_name}: retry budget exhausted")
    finally:
        _shared_budget.reset(token)
