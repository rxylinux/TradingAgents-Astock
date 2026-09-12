# ZCode Batch A Handoff: News Temporal Filtering + Failure Semantics

Date: 2026-09-08 · Baseline: `d269ba4` (1009 passed / 14 skipped / 52 subtests / 5 warnings)
Status: **RED → GREEN complete + Codex A-fix corrections applied**.
- ZCode tests: 19/19 passed
- Codex audit `audit_optimization_news_2026_09_08.py`: **8/8 passed** (was 2/6)
- Combined targeted (A+B+existing regression): 156/156 passed

## Scope

Batch A from `docs/OPTIMIZATION_REVIEW_2026-09-08.md`:
1. **A-1** `get_global_news`: temporal filtering (future/missing/invalid/cross-timezone), filter-before-limit, publish time in output, coverage note
2. **A-2** `get_news`: failure vs success-empty separation (DataFailure), invalid date exclusion, exclusion notes
3. **Real tool routing**: verified via `@tool → invoke()` entry

## Changes

### `tradingagents/dataflows/a_stock.py`

**`get_global_news(curr_date, look_back_days, limit)`**:
- CLS ctime (unix UTC) → normalized to Shanghai timezone (UTC+8) before filtering
- Eastmoney `showTime` → parsed to date via `_parse_news_time()`
- Filter by window `[start_date, curr_date]` **BEFORE** dedup and limit
- Records with missing/invalid time → excluded, counted as `unknown`, exclusion note in output
- Output includes `published: {time}` per record for traceability
- All records outside window → `[历史覆盖不足]` note (not "No news found")
- Both sources fail → `DataFailure` (not success empty)
- New helper: `_parse_news_time(raw) → date | None`

**`get_news(ticker, start_date, end_date)`**:
- Both sources fail → `DataFailure` with explicit message (not "No news found")
- Records with unparseable time → **excluded** (previously `except: pass` let them through)
- Records outside window → excluded (unchanged behavior, but now with exclusion count)
- Output notes exclusion count when records are dropped
- Primary succeeds with 0 records + fallback fails → still success empty (not failure)

## Tests

New file `tests/test_batch_a_news_semantics.py` (19 tests):

| Class | Tests | Coverage |
|---|---|---|
| `TestGlobalNewsTemporalFiltering` | 10 | future excluded, before window excluded, within window included, missing time marked unknown + excluded, invalid time excluded, filter-before-limit (15 valid + 5 future, limit=10), output includes publish time, empty coverage note, timezone normalized (CLS unix→Shanghai), all-sources-fail = DataFailure |
| `TestStockNewsFailureVsEmpty` | 7 | both fail = failure, both success empty = OK, primary fail + fallback success, primary empty + fallback fail, invalid date excluded, future record excluded, exclusion noted |
| `TestRealToolRouting` | 2 | `get_global_news.invoke()` filters future, `get_news.invoke()` returns data |

## Verification Results

```
Targeted (new): 19 passed (was 11 failed / 8 passed RED)
Existing regression: 83 passed (test_news_data_tools + test_lookahead_guard
                     + test_data_failure_semantics + test_cache_and_resilience
                     + test_temporal_boundaries)
```

## File Hashes (post-fix)

| File | SHA-256 |
|---|---|
| `tradingagents/dataflows/a_stock.py` | `84c0212af8d588ad198e26b45088f49970d69946ce4eaa496e24c3aefac5a30f` |
| `tests/test_batch_a_news_semantics.py` | *(updated — see below)* |

## Codex A-Fix Corrections (2026-09-08)

Codex audit found 6 failures in the initial implementation:
1. UTC/offset timestamps not converted to Shanghai day boundary (e.g. `2026-01-01T20:00:00Z` = Jan 2 Shanghai)
2. Garbage suffix after date accepted (e.g. `2026-01-01garbage`)
3. Invalid hours accepted (e.g. `99:99:99`)
4. First-10-chars parser leaked next Shanghai day news into historical window
5. `get_news` didn't use the same strict timezone rules
6. Duplicate CLS `pub_date` append caused `NameError` on some items

**Fixes applied**:
- New `_parse_news_time()`: strict ISO 8601 parser with Z/offset → Shanghai conversion,
  garbage suffix / invalid hours → `None` (excluded)
- `get_global_news` CLS ctime: unix UTC → Shanghai datetime
- `get_global_news` EM showTime: parsed via `_parse_news_time()`
- `get_news`: same `_parse_news_time()` for all articles
- Both functions: `start_dt`/`end_dt` now timezone-aware Shanghai with end-of-day boundary
- Removed stale duplicate CLS append block

## Partial Source Failure Notes (A-fix addition)

Both `get_global_news` and `get_news` now track per-source status:
- CLS + EM both fail → `DataFailure` (not "no news")
- One source fails, other succeeds → data from working source + warning note
- One source fails, other returns empty → success-empty + note about failed source
- Notes are embedded in output text (analyst-visible), not just logged

## Known Limitations

- `get_global_news` CLS timezone conversion assumes UTC+8 (Shanghai) — other markets' sources may need different normalization if added
- `_parse_news_time` only handles `YYYY-MM-DD...` prefix format; other formats are treated as unknown (conservative exclusion)
- The review notes this function currently has NO `cached_data` decorator — failure caching pollution is not an issue here (confirmed)
- Exclusion notes count total drops; per-record exclusion reasons are not individually surfaced (deemed sufficient for the analyst)
