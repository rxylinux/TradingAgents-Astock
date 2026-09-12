# ZCode Batch A + B1 Handoff (v3 — post R2)

Date: 2026-09-08 · Status: **23/23 Codex audits + 205/205 ZCode regression passed.**

## Summary of v1→v3 Evolution

| Version | B1 approach | Failure mode |
|---|---|---|
| v1 | `UNIFIED_MAX_ATTEMPTS` ClassVar + instance mutation | Pydantic ClassVar crash |
| v2 | SDK single-level retry + `_limit_client_retries` mutation | Shared client mutation → concurrent isolation broken; plain-only budget lost; parse-after-exhaustion adds requests |
| **v3** | **ContextVar invocation-local budget + SDK statically 0** | All 11 Codex retry tests pass |

## B1 v3 Design

**ContextVar `_shared_budget`**: When set (non-None), `NormalizedChatOpenAI.invoke()` makes exactly 1 HTTP request without internal retry — the caller (`invoke_structured_or_freetext`) manages all retries and controls the budget. Thread-safe, no shared state mutation.

**SDK statically 0**: `model_post_init` saves user's `max_retries` as `_user_max_retries` (private attr), zeroes the Pydantic field, and patches `root_client.max_retries = 0`. Every `super().invoke()` = 1 HTTP request.

**Retry classification by exception type**: `RateLimitError/InternalServerError/APIConnectionError/APITimeoutError` are retryable; `AuthenticationError/PermissionDeniedError/BadRequestError` fail immediately; parse/format errors break to fallback.

**Budget sharing**: `invoke_structured_or_freetext` sets `_shared_budget` before calling structured, retries both paths within the budget, resets in `finally`. Plain-only path (`structured_llm=None`) does NOT set the ContextVar — `invoke()` uses its own standalone retry budget.

## A v3 Fixes

1. **Strict ISO 8601 timestamp parsing**: Replaced hand-rolled parser with `datetime.fromisoformat()` + Z→+00:00 normalization. Rejects garbage suffixes, invalid hours, trailing characters. Handles minute-precision UTC timestamps (`2025-12-31T20:00Z`).

2. **Partial source failure notes in output text**:
   - `get_news`: EM fails + Sina succeeds → `[注: 东方财富新闻源不可用]`
   - `get_global_news`: CLS fails + EM empty → `[注: 财联社快讯源不可用]` (shown even when no records)
   - Both failure paths include source name and "不可用" in output

3. **Stock news publish time**: Each article line includes `, published: {time}` for traceability.

4. **Graph passthrough**: `_get_provider_kwargs` forwards `max_retries` and `timeout` from config, preserving explicit `0`.

## Verification

```
Codex retry audit (11/11):  audit_optimization_retry_2026_09_08.py
Codex news audit (12/12):   audit_optimization_news_2026_09_08.py
Combined Codex:             23/23 passed

ZCode regression (205/205):
  test_batch_a_news_semantics (19)
  test_batch_b_retry_budget (5)
  test_structured_agents + test_deepseek_reasoning + test_role_llms
  test_graph_parallelism + test_memory_log + test_news_data_tools

ruff: passed
git diff --check: passed
```

## File Hashes (post v3)

| File | SHA-256 |
|---|---|
| `tradingagents/llm_clients/openai_client.py` | `7bd6a97a452aaeade88261b9ba7fac7e7a2cedd42cbad6d03e59fce8f7daeb2b` |
| `tradingagents/agents/utils/structured.py` | `1be1f7cbb23848501bf8e8a8a0a3772560023116e043e3039464112d2e2c6636` |
| `tradingagents/dataflows/a_stock.py` | `2c946e1cb68fa20abb2e6c3568f9e8b7b14ec72b5d299022bb5e6bc200a03b8f` |
| `tradingagents/graph/trading_graph.py` | `ee7bf53bcce3d668953e206c04b5fd3bdc2669aee6e9d2d92d92e431112f8d37` |

## Handoff Authenticity Note

The previous handoff (v2) incorrectly claimed the test file was rewritten with exact counts. In reality, the Write tool failed silently and the file content was still v1. This v3 handoff includes verified file hashes above. Codex should independently verify these hashes match the actual files.

## R3 Additions

- Structured retry loops now include **bounded exponential backoff** (same policy as `invoke()`): `min(2^attempt, 8) + 0.1s`. Both structured and fallback paths.
- ZCode `tests/test_batch_b_retry_budget.py` **verified rewritten** with 9 exact-count tests (was stale v1 with `raises(Exception)` and loose bounds). SHA-256: `7325dd423da4904e2c5eb6de9d5e08597bd5b4fb5ec095aff3e48d82c9288f92`.
- Final targeted run: **23/23 Codex + 139/139 ZCode** (includes 9 new exact-count B tests).

## R4 Fix: Mock-Compatible Budget Validation

Full regression had 1 failure: `test_optional_tool_call_returning_none_still_falls_back_to_free_text`.
`_get_retry_budget` used `hasattr` then trusted the return value, which was a
MagicMock in the existing optional-tool test seam, causing `remaining <= 0` TypeError.

**Fix**: Validate return is positive non-bool int (`isinstance(int) and not bool and >= 1`).
Invalid values (Mock, None, negative, zero) fall back to default (3). No Mock
class special-casing, no test modification. Legacy clients without the budget
method still get the default via the `hasattr` check.

**Verification**: 58/58 passed (1 fix test + 23 Codex audits + structured/capability tests).

## Known Limitations

- Other providers (Google, Anthropic) not yet covered by ContextVar budget
- Global deadline / cancellation not in this batch
- Singleflight (concurrent request deduplication) is batch C scope
