# ZCode Batch B1 Handoff: Unified Retry Limit (v2 — post R1 rejection)

Date: 2026-09-08 · Baseline: `d269ba4` + Batch A + A-fix (uncommitted)
Status: **v1 REJECTED → v2 GREEN**, Codex 6/6 + ZCode 5/5 + combined 103/103.

## v1 Rejection & Root Cause

v1 used `UNIFIED_MAX_ATTEMPTS` ClassVar + `model_post_init` instance mutation.
The Pydantic ClassVar assignment crashed on `invoke_structured_or_freetext`
even for plain-only calls. The `max_retries=0` override lost the user's
configured budget. The structured fallback's "halve + append" wasn't a shared
budget. `_get_provider_kwargs` didn't forward timeout/retries.

## v2 Design

**Single retry authority**: The OpenAI SDK's `max_retries` is the only retry
mechanism. `NormalizedChatOpenAI.invoke()` is a pass-through (no outer retry
loop). `max_retries=N` → exactly `1+N` HTTP requests per invoke.

**Structured fallback budget sharing**: 
- Rate-limit exhausted (SDK used all retries) → fallback skipped (budget spent)
- Auth/permission/bad-request (by exception type, not string matching) → re-raised
- Parse failure → fallback with SDK retries capped at 0 (1 additional request)

**Graph passthrough**: `_get_provider_kwargs` forwards `max_retries` and
`timeout` from config. Explicit `0` is preserved (not treated as falsy).

## Files Changed

| File | SHA-256 |
|---|---|
| `tradingagents/llm_clients/openai_client.py` | `6d06d565179497d5f23720622e6bd58a3287b6581b05c0725f688ae5deaeac0a` |
| `tradingagents/agents/utils/structured.py` | `9bc1c9612d5bc6340b808033365f5a3a7bfc570caf1f6a87ba825fabcb2cf555` |
| `tradingagents/graph/trading_graph.py` | `ee7bf53bcce3d668953e206c04b5fd3bdc2669aee6e9d2d92d92e431112f8d37` |
| `tests/test_batch_b_retry_budget.py` | `f5f8e0d75651f82aead6af8c483cc0108344d72b2353a5283bb872e44a680b37` |

## Tests

Codex `audit_optimization_retry_2026_09_08.py` (6/6):
| Test | Scenario | Expected | Result |
|---|---|---|---|
| plain_only_helper | real Pydantic client, no structured | 1 req | ✓ |
| retries=0 exact | sustained 429, max_retries=0 | 1 req | ✓ |
| retries=2 exact | sustained 429, max_retries=2 | 3 req | ✓ |
| structured same budget | 429, structured+fallback | 3 req (no fallback) | ✓ |
| parse failure fallback | bad JSON → plain | 2 req | ✓ |
| graph passthrough | max_retries=0, timeout=2.5 | forwarded | ✓ |

ZCode `tests/test_batch_b_retry_budget.py` (5/5): exact totals for
retries=0/2/5, 401 fast-fail (1 req), transient 429 recovery, graph
passthrough (explicit 0, nonzero, absent).

Combined targeted (A+B1+existing): **103/103 passed**.

## Known Limitations

- Other providers (Google, Anthropic, etc.) not yet covered — their retry
  semantics may differ from the OpenAI SDK
- Global deadline / cancellation / Web stop-signal not in this batch
- Singleflight (concurrent request deduplication) is batch C scope
- The fallback cap (1 request) is a policy choice; a longer fallback chain
  could be configured if needed in future
