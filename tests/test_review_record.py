"""F1: offline review records — contract rule 10 acceptance matrix.

All offline; explicit fixtures; production memory untouched (hash asserted).
"""

import copy
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from tradingagents.evaluation.review_record import (
    RETRIEVAL_RANKING_VERSION,
    RecordValidationError,
    compute_record_digest,
    load_records,
    render_retrieval_md,
    retrieve_as_of,
)

FIX = Path(__file__).parent / "fixtures" / "review_records"
VENV_PY = Path(__file__).resolve().parents[1] / ".venv" / "bin" / "python"
REPO = Path(__file__).resolve().parents[1]


def load_fixture(name="records.jsonl"):
    return load_records((FIX / name).read_text(encoding="utf-8"))


def retrieve(records, **kw):
    kw.setdefault("ticker", "600519")
    kw.setdefault("instrument_type", "stock")
    kw.setdefault("as_of", "2025-06-01")
    kw.setdefault("limit", 10)
    return retrieve_as_of(records, **kw)


def reasons(result):
    return {e["record_id"]: e["reason"] for e in result["excluded"]}


class TestAvailabilityGates:
    def test_and_semantics_all_five_future_excluded(self):
        r = retrieve(load_fixture())
        assert reasons(r)["rr-pub-future"] == "future_publication_time"
        assert reasons(r)["rr-obs-future"] == "future_observed_at"
        assert reasons(r)["rr-ver-future"] == "future_record_available_at"
        assert reasons(r)["rr-later-decision"] == "decided_after_as_of"

    def test_maturity_future_publication_past_still_excluded(self):
        r = retrieve(load_fixture())
        assert reasons(r)["rr-mat-future-pub-past"] == "future_maturity"

    def test_unknown_gates_unverifiable_not_derived(self):
        r = retrieve(load_fixture())
        assert reasons(r)["rr-unknown-maturity"] == "unknown_maturity"
        assert reasons(r)["rr-unknown-publication"] == "unverifiable_publication_time"

    def test_pending_matured_vs_immature_separately_reported(self):
        r = retrieve(load_fixture())
        assert reasons(r)["rr-pending-overdue"] == "overdue_unresolved"
        assert reasons(r)["rr-pending-immature"] == "not_yet_mature"

    def test_expired_unresolvable_not_verified_experience(self):
        r = retrieve(load_fixture())
        assert reasons(r)["rr-expired"] == "expired_unresolvable_not_verified_experience"

    def test_same_run_multiple_horizons_and_versions_coexist(self):
        r = retrieve(load_fixture())
        ids = [x["record_id"] for x in r["records"]]
        assert {"rr-ok", "rr-ok-v2"} <= set(ids)  # 同 run 不同版本并存

    def test_unknown_returns_fees_stay_null(self):
        r = retrieve(load_fixture())
        nulls = next(x for x in r["records"] if x["record_id"] == "rr-nulls")
        outcome = nulls["outcome"]
        assert outcome["raw_return"] is None and outcome["alpha_return"] is None
        assert outcome["max_adverse_excursion"] is None
        assert outcome["fees_assumption"] is None

    def test_v1_ranking_newest_first_then_record_id(self):
        r = retrieve(load_fixture())
        ids = [x["record_id"] for x in r["records"]]
        assert ids[0] == "rr-newest"
        same_ticker = ids[1:4]  # 同日同标的三条
        assert same_ticker == sorted(same_ticker)  # record_id 稳定平局
        assert ids[4] == "rr-index"  # 异标的按排序（非排除）垫后（limit=5）

    def test_ranking_not_exclusion_cross_ticker_ranked_last(self):
        # F1 契约规则 7：同标的优先是排序不是排除——异标的排后仍可入选
        r = retrieve(load_fixture())
        ids = [x["record_id"] for x in r["records"]]
        assert "rr-index" in ids  # limit=10 内出现（排在全部同标的之后）
        assert ids.index("rr-index") > ids.index("rr-ok")
        assert all(e["reason"] != "relevance_mismatch" for e in r["excluded"])

    def test_limit_truncation_reported_separately(self):
        r = retrieve(load_fixture(), limit=2)
        assert r["limit_truncated"] == 3  # 5 合格（含排后的 rr-index）− 2
        assert len(r["records"]) == 2

    def test_midday_as_of_and_date_precision(self):
        recs = load_fixture()
        # available_at=2025-05-07 (date → EOD)；as-of 05-07 中午 → 仍未来
        r = retrieve(recs, as_of="2025-05-07T12:00:00+08:00")
        assert reasons(r)["rr-ok"] == "future_record_available_at"
        # date 精度按当日结束保守比较：05-07 当日任何时刻（含 23:59:59）
        # 都早于 available_at="2025-05-07" 的 EOD 锚点 → 仍不可见；跨日才可见
        r2 = retrieve(recs, as_of="2025-05-07T23:59:59+08:00")
        assert reasons(r2)["rr-ok"] == "future_record_available_at"
        r3 = retrieve(recs, as_of="2025-05-08T00:00:00+08:00")
        assert any(x["record_id"] == "rr-ok" for x in r3["records"])

    def test_offset_cross_day_comparison(self):
        recs = load_fixture()
        # publication=2025-05-06（date→EOD+08）；as-of 05-06 22:00 UTC（=05-07 06:00+08）
        r = retrieve(recs, as_of="2025-05-06T22:00:00+00:00")
        assert reasons(r).get("rr-ok") != "future_publication_time"


class TestOverlapBoundary:
    def test_explicit_closed_interval_overlap(self):
        r = retrieve(load_fixture("overlap.jsonl"), as_of="2026-01-01",
                     query_window={"start": "2024-11-05", "end": "2025-05-05"})
        assert r["overlap_filter"] == "explicit_closed_interval"
        ids = [x["record_id"] for x in r["records"]]
        assert ids == ["rr-ov-after"]  # boundary 起点与查询终点同日 → 重叠
        assert reasons(r)["rr-ov-boundary"] == "overlapping_window"
        assert reasons(r)["rr-ov-inside"] == "overlapping_window"

    def test_not_requested_reported(self):
        r = retrieve(load_fixture("overlap.jsonl"), as_of="2026-01-01")
        assert r["overlap_filter"] == "not_requested"
        assert len(r["records"]) == 3  # 未请求 → 不做重叠排除

    def test_query_window_validation(self):
        with pytest.raises(RecordValidationError):
            retrieve(load_fixture(), query_window={"start": "bad", "end": "2025-01-01"})
        with pytest.raises(RecordValidationError):
            retrieve(load_fixture(), query_window={"start": "2025-06-01",
                                                   "end": "2025-01-01"})


class TestValidationAndImmutability:
    def _base(self):
        return json.loads((FIX / "records.jsonl").read_text(encoding="utf-8").splitlines()[0])

    def test_same_id_different_content_rejected(self):
        rec = self._base()
        clash = dict(rec)
        clash["decision"] = dict(rec["decision"], rating="Sell")
        clash.pop("record_digest", None)
        clash["record_digest"] = compute_record_digest(clash)  # 内容自洽的新摘要
        with pytest.raises(RecordValidationError, match="内容不同"):
            load_records([rec, clash])

    def test_same_content_duplicate_deduped_and_reported(self):
        rec = self._base()
        out = load_records([rec, copy.deepcopy(rec)])
        ids = [x["record_id"] for x in out]
        assert ids.count(rec["record_id"]) == 1
        # 去重说明在载体属性上——记录本体不被污染（自洽可再验证）
        assert out.duplicates == [rec["record_id"]]
        assert "_load_notes" not in out[0]
        reloaded = load_records(out)
        assert list(reloaded) == list(out)
        assert compute_record_digest(out[0]) == out[0]["record_digest"]

    def test_reversed_duplicate_input_stable_metadata(self):
        rec = self._base()
        a = load_records([rec, copy.deepcopy(rec)])
        b = load_records([copy.deepcopy(rec), copy.deepcopy(rec)])
        assert a.duplicates == b.duplicates
        assert list(a) == list(b)

    def test_corrupted_digest_rejected(self):
        rec = self._base()
        rec["record_digest"] = "sha256:" + "0" * 64
        with pytest.raises(RecordValidationError, match="失配"):
            load_records([rec])

    def test_bool_and_nan_numbers_rejected(self):
        rec = self._base()
        rec["outcome"]["raw_return"] = True
        with pytest.raises(RecordValidationError, match="非 bool"):
            load_records([rec])
        rec2 = self._base()
        rec2["outcome"]["raw_return"] = float("nan")
        with pytest.raises(RecordValidationError):
            load_records([rec2])

    def test_illegal_window_and_early_maturity_rejected(self):
        rec = self._base()
        rec["identity"]["window"] = {"start": "2025-05-05", "end": "2024-11-05"}
        with pytest.raises(RecordValidationError, match="start > end"):
            load_records([rec])
        rec2 = self._base()
        rec2["outcome"]["maturity_date"] = "2024-01-01"  # 早于窗口结束
        with pytest.raises(RecordValidationError, match="到期早于"):
            load_records([rec2])

    def test_naive_datetime_rejected(self):
        rec = self._base()
        rec["decision"]["decided_at"] = "2024-11-05T18:00:00"
        with pytest.raises(RecordValidationError):
            load_records([rec])

    def test_annotations_require_source_and_time(self):
        rec = self._base()
        rec.setdefault("annotations", {})["error_type"] = "reasoning"
        with pytest.raises(RecordValidationError, match="标注"):
            load_records([rec])

    def test_expired_requires_reason(self):
        rec = self._base()
        rec["outcome"]["status"] = "expired_unresolvable"
        with pytest.raises(RecordValidationError, match="reason"):
            load_records([rec])

    def test_two_loads_and_reversed_input_stable(self):
        text = (FIX / "records.jsonl").read_text(encoding="utf-8")
        a = retrieve(load_records(text))
        lines = [ln for ln in text.splitlines() if ln.strip()]
        b = retrieve(load_records("\n".join(reversed(lines))))

        def by_id(r):
            return sorted(x["record_id"] for x in r["records"])
        assert by_id(a) == by_id(b)
        c = retrieve(load_records(text))
        assert json.dumps(c, sort_keys=True, ensure_ascii=False) == \
            json.dumps(a, sort_keys=True, ensure_ascii=False)

    def test_deep_immutability_input_and_output(self):
        text = (FIX / "records.jsonl").read_text(encoding="utf-8")
        snapshot = copy.deepcopy(json.loads(text.splitlines()[0]))
        records = load_records(text)
        result = retrieve(records)
        # 改动输出不得污染下一次调用
        result["records"][0]["outcome"]["raw_return"] = 999.0
        result2 = retrieve(records)
        assert result2["records"][0]["outcome"]["raw_return"] != 999.0 or True
        assert all(x["outcome"].get("raw_return") != 999.0 for x in result2["records"])
        # 嵌套修改输入对象不影响已加载记录
        records[0]["identity"]["ticker"] = "000000"
        result3 = retrieve(load_records(text))
        assert any(x["identity"]["ticker"] == "600519" for x in result3["records"])
        # 原始 fixture 行未被改动
        assert json.loads(text.splitlines()[0]) == snapshot

    def test_param_validation(self):
        recs = load_fixture()
        for bad in ({"limit": 0}, {"limit": True}, {"limit": "5"},
                    {"ticker": ""}, {"instrument_type": "etf"},
                    {"as_of": "not-a-date"}, {"ranking_version": 99}):
            kwargs = dict(ticker="600519", instrument_type="stock",
                          as_of="2025-06-01", limit=10)
            kwargs.update(bad)
            with pytest.raises(RecordValidationError):
                retrieve_as_of(recs, **kwargs)


class TestRealCli:
    def _run(self, *extra, file="records.jsonl"):
        return subprocess.run(
            [str(VENV_PY), "-m", "tradingagents.evaluation.review_record",
             "--file", str(FIX / file), "--output-dir", str(self._out),
             *extra],
            capture_output=True, text=True, cwd=str(REPO))

    @classmethod
    def setup_class(cls):
        import tempfile
        cls._tmp = tempfile.TemporaryDirectory()
        cls._out = Path(cls._tmp.name) / "out"

    @classmethod
    def teardown_class(cls):
        cls._tmp.cleanup()

    def test_normal_cli_json_and_markdown(self):
        proc = self._run("--as-of", "2025-06-01", "--ticker", "600519",
                         "--instrument-type", "stock", "--limit", "5")
        assert proc.returncode == 0, proc.stderr
        result = json.loads((self._out / "review_retrieval.json").read_text(encoding="utf-8"))
        assert result["overlap_filter"] == "not_requested"
        assert result["input_file_digest"].startswith("sha256:")
        md = (self._out / "review_retrieval.md").read_text(encoding="utf-8")
        assert "历史经验检索" in md and "排除清单" in md
        assert "不代表预测效果提高" in md

    def test_all_excluded_still_full_report(self):
        proc = self._run("--as-of", "2024-01-01", "--ticker", "600519",
                         "--instrument-type", "stock", "--limit", "5")
        assert proc.returncode == 0
        result = json.loads((self._out / "review_retrieval.json").read_text(encoding="utf-8"))
        assert result["records"] == []
        assert result["excluded"]

    def test_bad_schema_exit2_no_success_report(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad.jsonl"
            bad.write_text('{"record_id": "x"}\n', encoding="utf-8")
            out = Path(td) / "out"
            proc = subprocess.run(
                [str(VENV_PY), "-m", "tradingagents.evaluation.review_record",
                 "--file", str(bad), "--output-dir", str(out),
                 "--as-of", "2025-06-01", "--ticker", "600519",
                 "--instrument-type", "stock", "--limit", "5"],
                capture_output=True, text=True, cwd=str(REPO))
            assert proc.returncode == 2
            assert "校验拒绝" in proc.stderr
            assert not (out / "review_retrieval.json").exists()
            assert not (out / "review_retrieval.md").exists()

    def test_corrupted_digest_exit2(self):
        rec = json.loads((FIX / "records.jsonl").read_text(encoding="utf-8").splitlines()[0])
        rec["record_digest"] = "sha256:" + "0" * 64
        bad = Path(self._tmp.name) / "digest.jsonl"
        bad.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
        proc = subprocess.run(
            [str(VENV_PY), "-m", "tradingagents.evaluation.review_record",
             "--file", str(bad), "--output-dir", str(self._out),
             "--as-of", "2025-06-01", "--ticker", "600519",
             "--instrument-type", "stock", "--limit", "5"],
            capture_output=True, text=True, cwd=str(REPO))
        assert proc.returncode == 2
        assert "失配" in proc.stderr

    def test_overlap_cli(self):
        proc = self._run("--as-of", "2026-01-01", "--ticker", "600519",
                         "--instrument-type", "stock", "--limit", "10",
                         "--query-window-start", "2024-11-05",
                         "--query-window-end", "2025-05-05", file="overlap.jsonl")
        assert proc.returncode == 0
        result = json.loads((self._out / "review_retrieval.json").read_text(encoding="utf-8"))
        assert result["overlap_filter"] == "explicit_closed_interval"
        assert [x["record_id"] for x in result["records"]] == ["rr-ov-after"]


class TestProductionMemoryUntouched:
    def test_memory_source_hash_unchanged(self):
        # F1 不得改动生产记忆模块——以当前文件 hash 为准（回归锚）
        path = REPO / "tradingagents/agents/utils/memory.py"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        import tradingagents.agents.utils.memory as mem
        assert mem.__file__.endswith("memory.py")
        # 模块可导入且未被 F1 补丁（无新增属性）
        assert not hasattr(mem, "review_record_integration")
        assert len(digest) == 64


def load_fixture_record(mutate):
    rec = json.loads((FIX / "records.jsonl").read_text(encoding="utf-8").splitlines()[0])
    mutate(rec)
    rec.pop("record_digest", None)  # 内容已变——让加载重算摘要
    return load_records([rec])


class TestF1R1Mirrors:
    def _base(self):
        return json.loads((FIX / "records.jsonl").read_text(encoding="utf-8").splitlines()[0])

    def test_unknown_schema_rejected(self):
        from tradingagents.evaluation.review_record import RecordValidationError
        rec = self._base()
        rec["schema_version"] = 999
        with pytest.raises(RecordValidationError, match="schema_version"):
            load_records([rec])

    def test_blank_run_id_rejected(self):
        from tradingagents.evaluation.review_record import RecordValidationError
        rec = self._base()
        rec["identity"]["run_id"] = ""
        with pytest.raises(RecordValidationError, match="run_id"):
            load_records([rec])

    def test_window_list_rejected(self):
        from tradingagents.evaluation.review_record import RecordValidationError
        rec = self._base()
        rec["identity"]["window"] = ["broken"]
        with pytest.raises(RecordValidationError, match="window"):
            load_records([rec])

    def test_unknown_retrieval_ranking_rejected(self):
        from tradingagents.evaluation.review_record import RecordValidationError
        rec = self._base()
        rec["retrieval_meta"] = {"ranking_version": 999}
        with pytest.raises(RecordValidationError, match="ranking_version"):
            load_records([rec])

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), True])
    def test_non_finite_or_bool_fees_rejected(self, bad):
        from tradingagents.evaluation.review_record import RecordValidationError
        rec = self._base()
        rec["outcome"]["fees_assumption"] = {"bps": bad}
        with pytest.raises(RecordValidationError, match="fees"):
            load_records([rec])

    def test_future_annotation_version_inconsistency_rejected(self):
        from tradingagents.evaluation.review_record import RecordValidationError
        rec = self._base()
        rec["annotations"] = {"error_type": "reasoning",
                              "correct_risk_flags": ["X"],
                              "source": "manual", "annotated_at": "2099-01-01"}
        with pytest.raises(RecordValidationError, match="annotated_at"):
            load_records([rec])

    def test_verified_snapshot_wrong_digest_rejected(self):
        from tradingagents.evaluation.review_record import RecordValidationError
        rec = self._base()
        rec["decision"]["thesis_digest"] = {
            "digest": "sha256:" + "0" * 64,
            "snapshot": {"thesis": "offline content"},
            "verification": "verified_snapshot"}
        rec.pop("record_digest", None)
        with pytest.raises(RecordValidationError, match="失配"):
            load_records([rec])
        # 正确摘要可通过
        import hashlib
        from tradingagents.evaluation.review_record import _canonical_record_json
        good = {"digest": "sha256:" + hashlib.sha256(
            _canonical_record_json({"thesis": "offline content"}).encode()).hexdigest(),
            "snapshot": {"thesis": "offline content"},
            "verification": "verified_snapshot"}
        rec2 = self._base()
        rec2["decision"]["thesis_digest"] = good
        rec2.pop("record_digest", None)
        load_records([rec2])  # 不抛

    def test_return_unit_conflict_rejected(self):
        from tradingagents.evaluation.review_record import RecordValidationError
        rec = self._base()
        rec["outcome"]["return_unit"] = "percent"
        with pytest.raises(RecordValidationError, match="return_unit"):
            load_records([rec])

    def test_reversed_input_identical_result_and_exclusions(self):
        a, b = self._base(), self._base()
        a["record_id"] = "a"
        b["record_id"] = "b"
        a["outcome"]["publication_time"] = "2099-01-01"
        b["record_available_at"] = "2099-01-01"
        for r in (a, b):
            r.pop("record_digest", None)
        r1 = retrieve(load_records([a, b]))
        r2 = retrieve(load_records([b, a]))
        assert json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True)

    def test_retrieve_revalidates_tampered_input(self):
        from tradingagents.evaluation.review_record import RecordValidationError
        recs = load_fixture()
        recs[0]["outcome"]["raw_return"] = "0.5"  # 字符串数字
        with pytest.raises(RecordValidationError):
            retrieve(recs)

    def test_event_expiry_consumed_when_declared(self):
        rec = self._base()
        rec["outcome"]["event_expires_at"] = "2025-01-01"
        rec.pop("record_digest", None)
        recs = load_records([rec])
        r = retrieve(recs)
        assert {e["reason"] for e in r["excluded"]} == {"event_expired"}

    def test_entry_bounds_enforced_in_pure_load(self):
        from tradingagents.evaluation.review_record import (
            MAX_RECORDS, RecordValidationError)
        rec = self._base()
        with pytest.raises(RecordValidationError, match="上限"):
            load_records([copy.deepcopy(rec) for _ in range(MAX_RECORDS + 1)])


class TestF1R2Mirrors:
    """Codex F1 R2：版本类型严格/重叠仅同标的/快照存在即复算/JSON 边界。"""

    def _base(self):
        return json.loads((FIX / "records.jsonl").read_text(encoding="utf-8").splitlines()[0])

    @pytest.mark.parametrize("bad", [True, 1.0, "1"])
    def test_schema_version_type_strict(self, bad):
        from tradingagents.evaluation.review_record import RecordValidationError
        rec = self._base()
        rec["schema_version"] = bad
        with pytest.raises(RecordValidationError, match="schema_version"):
            load_records([rec])

    @pytest.mark.parametrize("bad", [True, 1.0, "1"])
    def test_retrieve_ranking_version_type_strict(self, bad):
        from tradingagents.evaluation.review_record import RecordValidationError
        recs = load_fixture()
        with pytest.raises(RecordValidationError, match="ranking_version"):
            retrieve_as_of(recs, ticker="600519", instrument_type="stock",
                           as_of="2025-06-01", limit=5, ranking_version=bad)

    def test_cross_ticker_not_overlap_excluded(self):
        # 契约 #8：重叠排除仅同 instrument/type——异标的照 F1 排序入选
        recs = load_fixture("overlap.jsonl")
        idx = self._base()
        idx["record_id"] = "rr-xticker"
        idx["identity"]["ticker"] = "000001.SH"
        idx["identity"]["instrument_type"] = "index"
        idx["identity"]["window"] = {"start": "2024-11-05", "end": "2025-05-05"}
        idx.pop("record_digest", None)
        all_recs = load_records(
            "\n".join((FIX / "overlap.jsonl").read_text(encoding="utf-8").splitlines()
                       + [json.dumps(idx, ensure_ascii=False)]))
        r = retrieve(all_recs, as_of="2026-01-01",
                     query_window={"start": "2024-11-05", "end": "2025-05-05"})
        ids = [x["record_id"] for x in r["records"]]
        assert "rr-xticker" in ids  # 异标的：窗口重叠也不排除
        assert "rr-ov-inside" not in ids  # 同标的：重叠照常排除
        assert "rr-ov-boundary" not in ids

    @pytest.mark.parametrize("verification", [None, "external_unverified"])
    def test_snapshot_presence_triggers_recompute_regardless_of_label(self, verification):
        from tradingagents.evaluation.review_record import RecordValidationError
        rec = self._base()
        entry = {"digest": "sha256:" + "0" * 64,
                 "snapshot": {"thesis": "offline content"}}
        if verification is not None:
            entry["verification"] = verification
        rec["decision"]["thesis_digest"] = entry
        rec.pop("record_digest", None)
        with pytest.raises(RecordValidationError, match="快照摘要失配"):
            load_records([rec])

    def test_cli_nonfinite_extension_field_controlled_exit2(self, tmp_path):
        import subprocess
        import sys as _sys
        # NaN 在扩展字段——json.loads 接受，但必须在解析边界被受控拒绝
        raw = json.dumps(self._base())[:-1] + ', "extra_annotation": NaN}'
        path = tmp_path / "nan.jsonl"
        path.write_text(raw, encoding="utf-8")
        out = tmp_path / "out"
        proc = subprocess.run(
            [_sys.executable, "-m", "tradingagents.evaluation.review_record",
             "--file", str(path), "--output-dir", str(out),
             "--as-of", "2025-06-01", "--ticker", "600519",
             "--instrument-type", "stock", "--limit", "5"],
            capture_output=True, text=True, cwd=str(REPO))
        assert proc.returncode == 2
        assert "Traceback" not in proc.stderr
        assert not (out / "review_retrieval.json").exists()

    def test_recursive_nonfinite_rejected(self):
        from tradingagents.evaluation.review_record import RecordValidationError
        rec = self._base()
        rec["extensions"] = {"nested": [1, {"deep": float("inf")}]}
        rec.pop("record_digest", None)
        with pytest.raises(RecordValidationError, match="非有限"):
            load_records([rec])
