"""C1: Evidence ledger unit tests — records, artifacts, deltas, reducer.

These cover the pure functions. End-to-end (real StateGraph → ToolNode →
state channel) coverage lives in tests/test_c1_evidence_graph.py.
"""

import copy
import json

import pytest
from langchain_core.messages import ToolMessage

from tradingagents.evidence import ledger as L
from tradingagents.evidence.ledger import (
    SCHEMA_ARTIFACT,
    SCHEMA_BUNDLE,
    SCHEMA_DELTA,
    collect_tool_message_delta,
    evidence_reducer,
    normalize_artifact,
    normalize_record,
    render_source_summary,
    validate_reference,
)


def artifact(records=None, statuses=None, exclusions=None, notes=None,
             tool="get_news", status="successful", provenance="a_stock",
             window=None):
    return {
        "schema": SCHEMA_ARTIFACT,
        "tool": tool,
        "provenance": provenance,
        "status": status,
        "requested_window": window or {"ticker": "600379", "start": "2024-11-01", "end": "2024-11-05"},
        "records": records or [],
        "source_statuses": statuses or [],
        "exclusions": exclusions or {},
        "coverage_notes": notes or [],
    }


def rec(title="T", content="c", pub="2024-11-05T11:37:00+08:00", source="CLS Wire"):
    return {
        "source": source, "title": title, "content": content,
        "url": "https://example.com", "published_at": pub,
        "time_precision": "datetime", "availability": "publication_time_only",
    }


def delta(run_id="run-1", events=None):
    return {"schema": SCHEMA_DELTA, "run_id": run_id, "events": events or {}}


def ev(role="news", tcid="call_1", **artifact_kwargs):
    a = artifact(**artifact_kwargs)
    return L.build_fetch_event(role, tcid, a, trade_date="2024-11-05")


class TestNormalizeRecord:
    def test_stable_id_from_same_content(self):
        r1 = normalize_record("CLS Wire", "News", content="body", published_at="2024-11-05T11:37:00+08:00")
        r2 = normalize_record("CLS Wire", "News", content="body", published_at="2024-11-05T11:37:00+08:00")
        assert r1["evidence_id"] == r2["evidence_id"]

    def test_different_source_different_id(self):
        r1 = normalize_record("CLS Wire", "News", content="b", published_at="2024-11-05T11:37:00+08:00")
        r2 = normalize_record("Eastmoney Global", "News", content="b", published_at="2024-11-05T11:37:00+08:00")
        assert r1["evidence_id"] != r2["evidence_id"]

    def test_different_publish_time_different_id(self):
        # Draft bug: same title/summary at different event times must NOT merge.
        r1 = normalize_record("CLS Wire", "News", content="b", published_at="2024-11-05T11:37:00+08:00")
        r2 = normalize_record("CLS Wire", "News", content="b", published_at="2024-11-06T09:00:00+08:00")
        assert r1["evidence_id"] != r2["evidence_id"]

    def test_digest_computed_before_truncation(self):
        long_content = "x" * 900
        r_full = normalize_record("S", "T", content=long_content, published_at="2024-11-05T11:37:00+08:00")
        r_trunc = normalize_record("S", "T", content=long_content[:500], published_at="2024-11-05T11:37:00+08:00")
        assert r_full["excerpt_truncated"] is True
        assert len(r_full["excerpt"]) == 500
        assert r_full["content_digest"] != r_trunc["content_digest"]
        assert r_trunc["excerpt_truncated"] is False

    def test_published_at_kept_full_iso_with_offset(self):
        # Draft bug: time[:19] dropped the timezone — cutoff comparison then lied.
        pub = "2024-11-05T11:37:00+08:00"
        r = normalize_record("S", "T", content="b", published_at=pub)
        assert r["published_at"] == pub


class TestNormalizeArtifact:
    def test_rejects_foreign_artifacts(self):
        assert normalize_artifact(None) is None
        assert normalize_artifact("text") is None
        assert normalize_artifact({"records": []}) is None  # no schema marker
        assert normalize_artifact({"schema": "other/1"}) is None

    def test_normalizes_raw_records(self):
        a = normalize_artifact(artifact(records=[rec()]))
        assert a is not None
        assert len(a["records"]) == 1
        r = a["records"][0]
        assert r["evidence_id"].startswith("ev-")
        assert r["retrieved_at"]

    def test_passes_through_already_normalized_records(self):
        normalized = normalize_record("S", "T", content="b", published_at="2024-11-05T11:37:00+08:00")
        a = normalize_artifact(artifact(records=[normalized]))
        assert a["records"][0] == normalized

    def test_invalid_status_and_exclusions_sanitized(self):
        a = normalize_artifact(artifact(status="bogus", exclusions={"x": 0, "y": True, "z": 2}))
        assert a["status"] == "unknown"
        assert a["exclusions"] == {"z": 2}


class TestCollectDelta:
    def _tool_msg(self, tcid="call_1", art=None):
        return ToolMessage(content="text", tool_call_id=tcid, artifact=art)

    def test_collects_only_schema_artifacts(self):
        good = self._tool_msg("call_1", artifact())
        foreign = self._tool_msg("call_2", {"something": "else"})
        no_artifact = self._tool_msg("call_3", None)
        d = collect_tool_message_delta([good, foreign, no_artifact], role="news", run_id="r1")
        assert d["schema"] == SCHEMA_DELTA
        assert d["run_id"] == "r1"
        assert list(d["events"]) == ["news:call_1"]

    def test_skips_messages_without_tool_call_id(self):
        m = ToolMessage(content="text", tool_call_id="", artifact=artifact())
        assert collect_tool_message_delta([m], role="news", run_id="r1") is None

    def test_duplicate_key_first_wins(self):
        m1 = self._tool_msg("call_1", artifact())
        m2 = self._tool_msg("call_1", artifact(tool="get_global_news"))
        d = collect_tool_message_delta([m1, m2], role="news", run_id="r1")
        assert len(d["events"]) == 1
        assert d["events"]["news:call_1"]["event"]["tool"] == "get_news"

    def test_event_meta_carries_role_and_trade_date(self):
        m = self._tool_msg("call_1", artifact())
        d = collect_tool_message_delta([m], role="policy", run_id="r1", trade_date="2024-11-05")
        meta = d["events"]["policy:call_1"]["event"]
        assert meta["role"] == "policy"
        assert meta["run_trade_date"] == "2024-11-05"
        assert meta["provenance"] == "a_stock"


class TestEvidenceReducer:
    def test_init_from_delta(self):
        e = ev(records=[rec()], statuses=[{"source": "CLS Wire", "status": "successful", "record_count": 1}])
        b = evidence_reducer(None, delta(events={"news:call_1": e}))
        assert b["schema"] == SCHEMA_BUNDLE
        assert b["run_id"] == "run-1"
        assert bundle_event_count(b) == 1
        assert len(b["records"]) == 1
        assert b["source_statuses"][0]["event"] == "news:call_1"

    def test_replay_same_delta_no_double_count(self):
        e = ev(records=[rec()], statuses=[{"source": "CLS Wire", "status": "successful", "record_count": 1}],
               exclusions={"发布时间在窗口外": 2})
        d = delta(events={"news:call_1": e})
        b1 = evidence_reducer(None, d)
        b2 = evidence_reducer(b1, d)
        assert b2 == b1
        assert b2["exclusions"]["发布时间在窗口外"] == 2
        assert len(b2["source_statuses"]) == 1

    def test_two_events_same_record_dedup(self):
        # Two different tool calls fetched the same article: one record,
        # two fetch events, two source statuses.
        e1 = ev(role="news", tcid="c1", records=[rec()], statuses=[{"source": "CLS Wire", "status": "successful", "record_count": 1}])
        e2 = ev(role="social", tcid="c2", records=[rec()], statuses=[{"source": "CLS Wire", "status": "successful", "record_count": 1}])
        b = evidence_reducer(evidence_reducer(None, delta(events={"news:c1": e1})), delta(events={"social:c2": e2}))
        assert bundle_event_count(b) == 2
        assert len(b["records"]) == 1
        assert len(b["source_statuses"]) == 2

    def test_mismatched_run_id_rejected(self):
        b = evidence_reducer(None, delta(events={"news:c1": ev()}))
        rejected = evidence_reducer(b, delta(run_id="run-2", events={"social:c9": ev()}))
        assert rejected == b
        assert bundle_event_count(rejected) == 1

    def test_empty_vs_nonempty_run_id_is_mismatch(self):
        b = evidence_reducer(None, delta(run_id="", events={"news:c1": ev()}))
        assert evidence_reducer(b, delta(run_id="run-9", events={"news:c2": ev()})) == b

    def test_inputs_never_mutated(self):
        d = delta(events={"news:c1": ev(records=[rec()], exclusions={"x": 1})})
        d_snapshot = copy.deepcopy(d)
        b1 = evidence_reducer(None, d)
        b1_snapshot = copy.deepcopy(b1)
        evidence_reducer(b1, delta(events={"social:c2": ev(records=[rec("T2")], exclusions={"x": 1})}))
        assert d == d_snapshot
        assert b1 == b1_snapshot

    def test_merge_order_independent(self):
        ea = ev(role="news", tcid="c1", records=[rec("A")],
                statuses=[{"source": "东方财富", "status": "successful", "record_count": 1}])
        eb = ev(role="social", tcid="c2", records=[rec("B")],
                statuses=[{"source": "新浪财经", "status": "successful", "record_count": 1}])
        da, db = delta(events={"news:c1": ea}), delta(events={"social:c2": eb})
        b_a_then_b = evidence_reducer(evidence_reducer(None, da), db)
        b_b_then_a = evidence_reducer(evidence_reducer(None, db), da)
        assert json.dumps(b_a_then_b, sort_keys=True, ensure_ascii=False) == \
               json.dumps(b_b_then_a, sort_keys=True, ensure_ascii=False)

    def test_garbage_delta_ignored(self):
        b = evidence_reducer(None, delta(events={"news:c1": ev()}))
        assert evidence_reducer(b, None) == b
        assert evidence_reducer(b, {"records": ["junk"]}) == b
        assert evidence_reducer(b, "junk") == b
        assert evidence_reducer(b, {"schema": SCHEMA_DELTA, "run_id": "run-1", "events": {"bad": {"no-event-key": 1}}}) == b

    def test_old_non_bundle_shape_returned_as_is(self):
        assert evidence_reducer("legacy", delta()) == "legacy"

    def test_exclusion_counts_sum_across_events(self):
        e1 = ev(tcid="c1", exclusions={"发布时间在窗口外": 2})
        e2 = ev(tcid="c2", exclusions={"发布时间在窗口外": 3, "发布时间缺失/无效": 1})
        b = evidence_reducer(
            evidence_reducer(None, delta(events={"news:c1": e1})),
            delta(events={"news:c2": e2}),
        )
        assert b["exclusions"] == {"发布时间缺失/无效": 1, "发布时间在窗口外": 5}


def bundle_event_count(b):
    from tradingagents.evidence.ledger import bundle_event_count as _c
    return _c(b)


class TestRenderAndValidate:
    def _bundle(self):
        e = ev(records=[rec()], statuses=[{"source": "CLS Wire", "status": "successful", "record_count": 1}],
               exclusions={"发布时间在窗口外": 2}, notes=["limit 截断"])
        return evidence_reducer(None, delta(events={"news:call_1": e}))

    def test_render_source_summary(self):
        s = render_source_summary(self._bundle())
        assert "数据来源状态" in s
        assert "CLS Wire" in s and "successful" in s
        assert "排除记录" in s and "发布时间在窗口外: 2 条" in s
        assert "覆盖说明" in s and "limit 截断" in s

    def test_render_empty_for_missing_or_legacy(self):
        assert render_source_summary(None) == ""
        assert render_source_summary({}) == ""
        assert render_source_summary({"schema": "other"}) == ""

    def test_validate_reference_valid(self):
        b = self._bundle()
        rid = b["records"][0]["evidence_id"]
        assert validate_reference(b, rid)["valid"] is True

    def test_validate_reference_unknown_id(self):
        assert validate_reference(self._bundle(), "ev-x-nope")["valid"] is False

    def test_validate_reference_future_vs_cutoff_tz_aware(self):
        # 11:37+08:00 == 03:37Z — string compare would wrongly say "after".
        e = ev(records=[rec(pub="2024-11-05T11:37:00+08:00")])
        b = evidence_reducer(None, delta(events={"news:c1": e}))
        rid = b["records"][0]["evidence_id"]
        # 11:37+08:00 == 03:37Z; 12:40+09:00 == 03:40Z ≥ record, but a naive
        # string compare would flag "11:..." > "12:..." and wrongly reject.
        assert validate_reference(b, rid, "2024-11-05T12:40:00+09:00")["valid"] is True
        assert validate_reference(b, rid, "2024-11-05T03:36:00+00:00")["valid"] is False

    def test_validate_reference_unparseable_cutoff_rejected(self):
        # Codex C1 audit R1: an invalid cutoff must not silently pass.
        b = self._bundle()
        rid = b["records"][0]["evidence_id"]
        out = validate_reference(b, rid, "not-a-date")
        assert out["valid"] is False
        assert "无法解析" in out["reason"]

    def test_validate_reference_no_cutoff_given(self):
        b = self._bundle()
        rid = b["records"][0]["evidence_id"]
        out = validate_reference(b, rid)
        assert out["valid"] is True
        assert "未验证" in out["reason"]

    def test_validate_reference_unknown_publish_time_fails_joint_check(self):
        # Codex C1 audit R1: unknown time is NOT time-compliant.
        e = ev(records=[dict(rec(pub=""), time_precision="unknown")])
        b = evidence_reducer(None, delta(events={"news:c9": e}))
        rid = b["records"][0]["evidence_id"]
        out = validate_reference(b, rid, "2024-11-05T23:59:59+08:00")
        assert out["valid"] is False
        assert "发布时间未知" in out["reason"]

    def test_validate_reference_date_only_cutoff_end_of_day_shanghai(self):
        # Same-day record is compliant with a date-only cutoff (end-of-day
        # Shanghai, matching the vendor filter boundary); next day is not.
        e = ev(records=[rec(pub="2024-11-05T23:30:00+08:00")])
        b = evidence_reducer(None, delta(events={"news:c9": e}))
        rid = b["records"][0]["evidence_id"]
        assert validate_reference(b, rid, "2024-11-05")["valid"] is True
        assert validate_reference(b, rid, "2024-11-04")["valid"] is False

    def test_merge_same_article_different_retrieved_times_deterministic(self):
        # Codex C1 audit R1: same evidence_id fetched at 12:00 and 13:00 —
        # merge order must not decide which record survives.
        common = dict(source="official", title="same event", content="same",
                      published_at="2024-11-05T11:00:00+08:00", time_precision="datetime")
        r1 = normalize_record(**common, retrieved_at="2024-11-05T12:00:00+08:00")
        r2 = normalize_record(**common, retrieved_at="2024-11-05T13:00:00+08:00")
        from langchain_core.messages import ToolMessage as TM
        def mk(rec, role):
            art = artifact(records=[rec])
            m = TM(content="offline", tool_call_id=role + "-call", artifact=art)
            from tradingagents.evidence.ledger import collect_tool_message_delta as c
            return c([m], role=role, run_id="run-1", trade_date="2024-11-05")
        left, right = mk(r1, "news"), mk(r2, "policy")
        a = evidence_reducer(evidence_reducer(None, left), right)
        b2 = evidence_reducer(evidence_reducer(None, right), left)
        assert a == b2
        assert a["records"][0]["retrieved_at"] == "2024-11-05T12:00:00+08:00"

    def test_merge_conflicting_digests_flagged(self):
        # Same evidence_id (hand-forged) with different digests: canonical
        # record kept deterministically + conflict note raised.
        base = dict(source="official", title="t", published_at="2024-11-05T11:00:00+08:00")
        r1 = normalize_record(**base, content="one", retrieved_at="2024-11-05T12:00:00+08:00")
        r2 = dict(r1)
        r2["content_digest"] = "0" * 64  # forged: same id, different digest
        from langchain_core.messages import ToolMessage as TM
        from tradingagents.evidence.ledger import collect_tool_message_delta as c
        def mk(rec, role):
            m = TM(content="offline", tool_call_id=role + "-call", artifact=artifact(records=[rec]))
            return c([m], role=role, run_id="run-1")
        a = evidence_reducer(evidence_reducer(None, mk(r1, "news")), mk(r2, "policy"))
        b2 = evidence_reducer(evidence_reducer(None, mk(r2, "policy")), mk(r1, "news"))
        assert a == b2
        assert any("证据 ID 冲突" in n for n in a["coverage_notes"])
        # Canonical pick = min under the record rank; identical digest tiebreak
        # makes it independent of arrival order.
        expected = min(r1["content_digest"], r2["content_digest"])
        assert a["records"][0]["content_digest"] == expected

    def test_validate_reference_empty_id(self):
        assert validate_reference(self._bundle(), "")["valid"] is False
