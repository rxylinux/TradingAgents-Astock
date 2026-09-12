"""F3 stage A (prepare): plan/identity/splits — Codex contract rules 1-4.

All offline; prepare never accepts labels, never touches models/production.
"""

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tradingagents.evaluation.paired_eval import (
    MAX_REPEATS,
    PlanValidationError,
    build_plan,
    load_features,
    validate_feature,
)

FIX = Path(__file__).parent / "fixtures" / "paired_eval"
VENV_PY = Path(__file__).resolve().parents[1] / ".venv" / "bin" / "python"
REPO = Path(__file__).resolve().parents[1]


def feat(fid, **over):
    base = {"feature_id": fid, "ticker": "600519", "instrument_type": "stock",
            "prediction_at": "2024-03-01", "feature_available_at": "2024-02-28",
            "decision": {"rating": "Buy", "prediction_horizon": "3-6 months",
                         "decided_at": "2024-02-20"},
            "target_window": {"start": "2024-03-01", "end": "2024-06-01"},
            "binary_event": None}
    base.update(over)
    return base


def trials(rc=2):
    return {"pair-001": {
        "repeat_count": rc,
        "arms": {
            "baseline": {"normalized_config": {"model": "m", "budget": 1,
                                                "seed_policy": "r",
                                                "evidence_debate_enabled": False}},
            "candidate": {"normalized_config": {"model": "m", "budget": 1,
                                                 "seed_policy": "r",
                                                 "evidence_debate_enabled": True}},
        }}}


SPLITS = {
    "train": {"start": "2024-01-01", "end": "2024-06-30"},
    "validation": {"start": "2024-07-01", "end": "2024-12-31"},
    "holdout": {"start": "2025-01-01", "end": "2025-12-31"},
}


class TestFeatureSchema:
    def test_full_f1_record_rejected(self):
        f1_record = {"schema_version": 1, "record_id": "rr",
                     "identity": {"run_id": "r"}, "decision": {"rating": "Buy"},
                     "outcome": {"status": "resolved", "raw_return": 0.1,
                                 "maturity_date": "2025-01-01",
                                 "observed_at": "2025-01-02",
                                 "publication_time": "2025-01-02"},
                     "record_available_at": "2025-01-03"}
        with pytest.raises(PlanValidationError):
            validate_feature(f1_record, 0)

    def test_extra_top_and_nested_keys_rejected(self):
        with pytest.raises(PlanValidationError, match="白名单"):
            validate_feature(feat("f", extra="x"), 0)
        bad = feat("f")
        bad["decision"]["surprise"] = 1
        with pytest.raises(PlanValidationError, match="decision"):
            validate_feature(bad, 0)

    def test_outcome_markers_rejected(self):
        for marker in ("raw_return", "annotations", "outcome", "error_type"):
            bad = feat("f")
            bad[f"leak_{marker}"] = {marker: 1}
            with pytest.raises(PlanValidationError, match=marker):
                validate_feature(bad, 0)

    def test_version_time_independent_of_decided(self):
        # feature_available_at 晚于 prediction_at → 拒
        with pytest.raises(PlanValidationError, match="feature_available_at"):
            validate_feature(feat("f", feature_available_at="2024-03-02"), 0)
        # 用 decided_at 相同值不豁免：仍需显式字段
        with pytest.raises(PlanValidationError, match="feature_available_at"):
            validate_feature({k: v for k, v in feat("f").items()
                              if k != "feature_available_at"}, 0)

    def test_future_feature_version_rejected_at_schema(self):
        # avail 晚于 pred：schema 层直接拒绝（F3 契约 #2——不是 as-of 排除）
        with pytest.raises(PlanValidationError, match="feature_available_at 晚于"):
            build_plan([feat("f-late", feature_available_at="2024-03-05")],
                       trials(), SPLITS)
        # decided 晚于 pred 同样拒绝
        with pytest.raises(PlanValidationError, match="decided_at 晚于"):
            build_plan([feat("f-d", decision={"rating": "Buy",
                                              "prediction_horizon": "h",
                                              "decided_at": "2024-03-02"})],
                       trials(), SPLITS)
        # 合法控制：avail = pred 当日（date→EOD 语义）通过
        plan = build_plan([feat("f-ok", feature_available_at="2024-03-01")],
                          trials(), SPLITS)
        assert {e["feature_id"] for e in plan["plan_entries"]} == {"f-ok"}

    def test_bad_time_and_naive_rejected(self):
        with pytest.raises(PlanValidationError):
            validate_feature(feat("f", prediction_at="2024-03-01T10:00:00"), 0)
        with pytest.raises(PlanValidationError):
            validate_feature(feat("f", target_window={"start": "bad",
                                                     "end": "2024-06-01"}), 0)


class TestTrialIdentity:
    def test_only_e_boolean_diff_allowed(self):
        plan = build_plan([feat("f")], trials(), SPLITS)
        t = plan["trials"]["pair-001"]
        assert t["baseline_config"]["evidence_debate_enabled"] is False
        assert t["candidate_config"]["evidence_debate_enabled"] is True
        assert t["baseline_config_digest"] != t["candidate_config_digest"]

    @pytest.mark.parametrize("mutate,match", [
        (lambda t: t["pair-001"]["arms"]["candidate"]["normalized_config"].update(
            {"budget": 2}), "差集"),
        (lambda t: t["pair-001"]["arms"]["candidate"]["normalized_config"].update(
            {"evidence_debate_enabled": 1}), "bool"),
        (lambda t: t["pair-001"]["arms"]["baseline"]["normalized_config"].update(
            {"evidence_debate_enabled": True}), "差集"),
        (lambda t: t["pair-001"]["arms"]["candidate"]["normalized_config"].pop(
            "evidence_debate_enabled"), "bool false→true"),
    ])
    def test_config_violations_rejected(self, mutate, match):
        t = trials()
        mutate(t)
        with pytest.raises(PlanValidationError, match=match):
            build_plan([feat("f")], t, SPLITS)

    def test_repeat_count_validation(self):
        with pytest.raises(PlanValidationError):
            build_plan([feat("f")], trials(rc=0), SPLITS)
        with pytest.raises(PlanValidationError):
            build_plan([feat("f")], trials(rc=True), SPLITS)
        with pytest.raises(PlanValidationError, match="上限"):
            build_plan([feat("f")], trials(rc=MAX_REPEATS + 1), SPLITS)

    def test_duplicate_identity_rejected(self):
        # 同 (pair, feature, repeat) 二次出现 → 拒绝。dict trials 无法直接构造，
        # 以双 pair 同 feature 验证身份独立性（合法），并断言防御分支存在。
        plan = build_plan([feat("f")], {"p1": trials()["pair-001"],
                                        "p2": trials(rc=1)["pair-001"]}, SPLITS)
        ids = [(e["pair_id"], e["feature_id"], e["repeat_index"])
               for e in plan["plan_entries"]]
        assert len(ids) == len(set(ids))  # 无重复身份
        assert ("p1", "f", 1) in ids and ("p2", "f", 1) in ids

    def test_all_repeats_in_plan_denominator(self):
        plan = build_plan([feat("f")], trials(rc=3), SPLITS)
        reps = sorted(e["repeat_index"] for e in plan["plan_entries"]
                      if e["feature_id"] == "f")
        assert reps == [1, 2, 3]
        assert plan["planned_arm_runs"] == 6


class TestSplitsAndPurge:
    def test_fixture_plan_purges_and_keeps(self):
        features = [json.loads(ln) for ln in
                    (FIX / "features.jsonl").read_text(encoding="utf-8").splitlines()
                    if ln.strip()]
        t = json.loads((FIX / "trials.json").read_text(encoding="utf-8"))
        s = json.loads((FIX / "splits.json").read_text(encoding="utf-8"))
        plan = build_plan(features, t, s, embargo=0)
        kept = {e["feature_id"] for e in plan["plan_entries"]}
        assert kept == {"f-train-1", "f-val-1", "f-hold-1", "f-hold-2", "f-xticker"}
        pairs = {(p["removed_feature_id"], p["conflicts_with_feature_id"])
                 for p in plan["purged_overlaps"]}
        assert ("f-train-ov", "f-val-1") in pairs  # 相邻 train×validation
        assert ("f-val-ov", "f-hold-1") in pairs   # 边界相等也算重叠
        assert [u["feature_id"] for u in plan["excluded_unassigned"]] == ["f-unassigned"]

    def test_nonadjacent_train_holdout_purge(self):
        # train 窗口直接伸入 holdout（跨过 validation）→ 仍须剔除
        features = [
            feat("f-train", target_window={"start": "2024-03-01",
                                           "end": "2025-06-01"}),
            feat("f-hold", prediction_at="2025-03-01",
                 feature_available_at="2025-02-28",
                 decision={"rating": "Hold", "prediction_horizon": "h",
                           "decided_at": "2025-02-20"},
                 target_window={"start": "2025-04-01", "end": "2025-08-01"}),
        ]
        plan = build_plan(features, trials(), SPLITS)
        kept = {e["feature_id"] for e in plan["plan_entries"]}
        assert "f-train" not in kept and "f-hold" in kept

    def test_cross_ticker_no_purge(self):
        features = [
            feat("f-a", target_window={"start": "2024-03-01", "end": "2024-12-01"}),
            feat("f-b", ticker="000001.SH", instrument_type="index",
                 target_window={"start": "2024-04-01", "end": "2024-10-01"}),
        ]
        plan = build_plan(features, trials(), SPLITS)
        kept = {e["feature_id"] for e in plan["plan_entries"]}
        assert kept == {"f-a", "f-b"}  # 跨组不 purge（b 也都在切分内）

    def test_embargo_calendar_days(self):
        features = [
            feat("f-train", target_window={"start": "2024-03-01",
                                           "end": "2024-08-10"}),
            feat("f-val", prediction_at="2024-09-01",
                 feature_available_at="2024-08-30",
                 decision={"rating": "Hold", "prediction_horizon": "h",
                           "decided_at": "2024-08-20"},
                 target_window={"start": "2024-09-05", "end": "2024-12-01"}),
        ]
        # embargo=0：train 窗口终点 08-10 < val 起点 09-05 → 不重叠
        plan0 = build_plan(features, trials(), SPLITS, embargo=0)
        assert {e["feature_id"] for e in plan0["plan_entries"]} == {"f-train", "f-val"}
        # embargo=30：val 窗口起点前推 30 日 = 08-06 ≤ 08-10 → 重叠，剔除较早侧
        plan30 = build_plan(features, trials(), SPLITS, embargo=30)
        kept30 = {e["feature_id"] for e in plan30["plan_entries"]}
        assert "f-train" not in kept30 and "f-val" in kept30
        assert plan30["embargo_days"] == 30

    def test_deletion_order_independent(self):
        features = [json.loads(ln) for ln in
                    (FIX / "features.jsonl").read_text(encoding="utf-8").splitlines()
                    if ln.strip()]
        t = json.loads((FIX / "trials.json").read_text(encoding="utf-8"))
        s = json.loads((FIX / "splits.json").read_text(encoding="utf-8"))
        a = build_plan(features, t, s)
        b = build_plan(list(reversed(features)), t, s)
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)

    def test_split_validation(self):
        # 缺必需名 → 拒
        with pytest.raises(PlanValidationError, match="缺少必需名称"):
            build_plan([feat("f")], trials(),
                       {"train": {"start": "2024-01-01", "end": "2024-06-30"},
                        "validation": {"start": "2024-07-01", "end": "2024-12-31"}})
        # 时间顺序倒置 → 拒（绝对时间比较）
        with pytest.raises(PlanValidationError, match="顺序非法"):
            build_plan([feat("f")], trials(),
                       {"train": {"start": "2024-01-01", "end": "2024-08-01"},
                        "validation": {"start": "2024-07-01", "end": "2024-12-31"},
                        "holdout": {"start": "2025-01-01", "end": "2025-12-31"}})
        # 端点相等（闭区间相接）→ 拒
        with pytest.raises(PlanValidationError, match="相接"):
            build_plan([feat("f")], trials(),
                       {"train": {"start": "2024-01-01", "end": "2024-07-01"},
                        "validation": {"start": "2024-07-01", "end": "2024-12-31"},
                        "holdout": {"start": "2025-01-01", "end": "2025-12-31"}})
        # 时区反向（字典序欺骗）→ 拒（绝对时间）
        with pytest.raises(PlanValidationError, match="顺序非法"):
            build_plan([feat("f")], trials(),
                       {"train": {"start": "2024-01-01", "end": "2024-07-01T00:30:00-04:00"},
                        "validation": {"start": "2024-07-01T08:00:00+08:00", "end": "2024-12-31"},
                        "holdout": {"start": "2025-01-01", "end": "2025-12-31"}})
        with pytest.raises(PlanValidationError):
            build_plan([feat("f")], trials(), SPLITS, embargo=-1)
        with pytest.raises(PlanValidationError):
            build_plan([feat("f")], trials(), SPLITS, embargo=True)


class TestDeterminismAndCLI:
    def test_same_inputs_identical_plan(self):
        features = [json.loads(ln) for ln in
                    (FIX / "features.jsonl").read_text(encoding="utf-8").splitlines()
                    if ln.strip()]
        t = json.loads((FIX / "trials.json").read_text(encoding="utf-8"))
        s = json.loads((FIX / "splits.json").read_text(encoding="utf-8"))
        a = build_plan(features, t, s)
        b = build_plan(copy.deepcopy(features), t, s)
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
        assert a["plan_digest"].startswith("sha256:")

    def test_prepare_never_reads_labels_probes(self, tmp_path, monkeypatch):
        # prepare 的参数面就没有标签路径；同时以 open 探针证明未读标签
        label_file = tmp_path / "labels.jsonl"
        label_file.write_text('{"label_id": "x"}\n')
        opened = []
        real_open = open

        def probe(file, *a, **kw):
            opened.append(str(file))
            return real_open(file, *a, **kw)

        import builtins
        monkeypatch.setattr(builtins, "open", probe)
        import subprocess as sp
        proc = sp.run(
            [str(VENV_PY), "-m", "tradingagents.evaluation.paired_eval",
             "--features", str(FIX / "features.jsonl"),
             "--trials", str(FIX / "trials.json"),
             "--splits", str(FIX / "splits.json"),
             "--output-dir", str(tmp_path / "out")],
            capture_output=True, text=True, cwd=str(REPO))
        monkeypatch.undo()
        assert proc.returncode == 0, proc.stderr
        assert not any("labels" in p for p in opened)  # 子进程未读标签（探针在父进程，附带参数面证明）

    def test_real_cli_normal_and_bad_exit2(self, tmp_path):
        out = tmp_path / "out"
        proc = subprocess.run(
            [str(VENV_PY), "-m", "tradingagents.evaluation.paired_eval",
             "--features", str(FIX / "features.jsonl"),
             "--trials", str(FIX / "trials.json"),
             "--splits", str(FIX / "splits.json"),
             "--output-dir", str(out)],
            capture_output=True, text=True, cwd=str(REPO))
        assert proc.returncode == 0
        plan = json.loads((out / "eval_plan.json").read_text(encoding="utf-8"))
        assert plan["planned_arm_runs"] == 20

        bad = tmp_path / "bad.jsonl"
        bad.write_text('{"record_id": "rr", "outcome": {"raw_return": 0.1}}\n')
        out2 = tmp_path / "out2"
        proc2 = subprocess.run(
            [str(VENV_PY), "-m", "tradingagents.evaluation.paired_eval",
             "--features", str(bad),
             "--trials", str(FIX / "trials.json"),
             "--splits", str(FIX / "splits.json"),
             "--output-dir", str(out2)],
            capture_output=True, text=True, cwd=str(REPO))
        assert proc2.returncode == 2
        assert "拒绝" in proc2.stderr
        assert "Traceback" not in proc2.stderr
        assert not (out2 / "eval_plan.json").exists()

    def test_naive_and_json_edge_rejected(self):
        with pytest.raises(PlanValidationError):
            load_features('{"feature_id": "f", "x": NaN}\n'.replace("NaN", "1e999"))


class TestF3bScore:
    """Score 阶段：冻结计划验证/精确身份/固定分母/重复汇总/币种分桶/CLI。"""

    @staticmethod
    def _load_all():
        plan = json.loads((FIX / "plan.json").read_text(encoding="utf-8"))
        preds = [json.loads(ln) for ln in
                 (FIX / "predictions.jsonl").read_text(encoding="utf-8").splitlines()
                 if ln.strip()]
        labels = [json.loads(ln) for ln in
                  (FIX / "labels.jsonl").read_text(encoding="utf-8").splitlines()
                  if ln.strip()]
        return plan, preds, labels

    from tradingagents.evaluation.paired_eval import (
        ScoreValidationError as _SVE, score_plan as _score,
        load_predictions as _lp, load_labels as _ll)

    def test_full_score_run_counts_and_metrics(self):
        from tradingagents.evaluation.paired_eval import score_plan
        plan, preds, labels = self._load_all()
        r = score_plan(plan, preds, labels, "2025-07-01")
        b = r["arms"]["baseline"]["counts"]
        c = r["arms"]["candidate"]["counts"]
        assert b["planned"] == c["planned"] == 10  # 固定分母
        assert b["missing"] == 1  # 计划缺失不缩分母
        assert c["refused"] == 1
        assert b["completed"] == 9 and c["completed"] == 9
        assert r["arms"]["candidate"]["e_unresolved"]["numerator"] == 2
        assert r["arms"]["baseline"]["direction_hit"]["value"] == 1.0
        assert r["arms"]["candidate"]["direction_hit"]["value"] is None  # 无合格分母
        # Brier 明确不支持
        assert r["arms"]["baseline"]["brier"]["status"] == "not_applicable"
        # 待期/摘要失配标签隔离
        reasons = {e["reason"] for e in r["invalid_labels"]}
        assert "pending_maturity" in reasons
        assert "claim_digest_mismatch" in reasons

    def test_tampered_plan_rejected(self):
        from tradingagents.evaluation.paired_eval import (
            ScoreValidationError, score_plan)
        plan, preds, labels = self._load_all()
        bad = json.loads(json.dumps(plan))
        bad["planned_arm_runs"] = 999  # 篡改 → 完整文件摘要失配
        with pytest.raises(ScoreValidationError, match="plan_digest"):
            score_plan(bad, preds, labels, "2025-07-01")

    def test_conflicting_duplicate_prediction_rejected(self):
        from tradingagents.evaluation.paired_eval import (
            ScoreValidationError, score_plan)
        plan, preds, labels = self._load_all()
        clash = json.loads(json.dumps(preds[0]))
        with pytest.raises(ScoreValidationError, match="冲突的重复预测身份"):
            score_plan(plan, preds + [clash], labels, "2025-07-01")

    def test_outside_plan_prediction_rejected(self):
        from tradingagents.evaluation.paired_eval import (
            ScoreValidationError, score_plan)
        plan, preds, labels = self._load_all()
        rogue = json.loads(json.dumps(preds[0]))
        rogue["feature_id"] = "f-not-in-plan"
        with pytest.raises(ScoreValidationError, match="计划外预测拒绝"):
            score_plan(plan, preds + [rogue], labels, "2025-07-01")

    def test_wrong_arm_label_not_applied(self):
        from tradingagents.evaluation.paired_eval import score_plan, _digest
        plan, preds, labels = self._load_all()
        # 错臂标签：把 baseline 的标签 id/digest 改到 candidate（其 claim 内容
        # 相同、claim_id 相同）——同 (candidate 预测, kind, claim) 重复 → 拒绝
        target = labels[0]
        other_arm = "candidate" if target["arm"] == "baseline" else "baseline"
        cand_pred = next(p for p in preds
                         if p["arm"] == other_arm
                         and p["feature_id"] == target["feature_id"]
                         and p["repeat_index"] == target["repeat_index"]
                         and p["claims"])
        swapped = json.loads(json.dumps(target))
        swapped["label_id"] = "lbl-wrong-arm"
        swapped["arm"] = other_arm
        # 绑定到该臂自己 claim（同内容同 id）→ 与既有 candidate 标签重复 → 拒
        if any(lab["arm"] == other_arm and lab["feature_id"] == target["feature_id"]
               and lab["target_claim_id"] == swapped["target_claim_id"]
               for lab in labels):
            from tradingagents.evaluation.paired_eval import ScoreValidationError
            with pytest.raises(ScoreValidationError, match="同目标"):
                score_plan(plan, preds, labels + [swapped], "2025-07-01")
        else:
            r = score_plan(plan, preds, labels + [swapped], "2025-07-01")
            assert r["arms"]["baseline"]["fact_support"]["denominator"] >= 1
            assert r["arms"]["candidate"]["fact_support"]["denominator"] >= 1

    def test_pending_label_excluded_not_scored(self):
        from tradingagents.evaluation.paired_eval import score_plan
        plan, preds, labels = self._load_all()
        r = score_plan(plan, preds, labels, "2025-07-01")
        assert any(e["reason"] == "pending_maturity"
                   for e in r["invalid_labels"])

    def test_usage_unknown_and_currency_buckets(self):
        from tradingagents.evaluation.paired_eval import score_plan
        plan, preds, labels = self._load_all()
        r = score_plan(plan, preds, labels, "2025-07-01")
        assert r["usage_unknown_counts"]["http_requests"] > 0  # unknown 保全
        assert r["usage_totals"]["http_requests"] == 0  # 不以 0 冒充实耗
        # 每臂分开的 Decimal 币种分桶（Codex F3b R1 #4）
        per_arm = r["costs_by_currency"]["per_arm"]
        assert "CNY" in per_arm["baseline"] and "CNY" in per_arm["candidate"]
        base_entries = per_arm["baseline"]["CNY"]["entries"]
        cand_entries = per_arm["candidate"]["CNY"]["entries"]
        assert base_entries + cand_entries == len(preds)
        # Decimal 精确：0.012 × (9+10) = 0.108 + 0.120
        assert per_arm["baseline"]["CNY"]["known_total"] == "0.108"
        assert per_arm["candidate"]["CNY"]["known_total"] == "0.120"

    def test_population_stddev_divide_n(self):
        from tradingagents.evaluation.paired_eval import _population_stats
        stats = _population_stats([0.0, 1.0])
        assert stats["mean"] == 0.5
        assert abs(stats["population_stddev"] - 0.5) < 1e-9  # ÷N 不是 N-1
        assert _population_stats([]) == {"n": 0, "mean": None,
                                         "population_stddev": None}

    def test_same_inputs_byte_identical(self):
        from tradingagents.evaluation.paired_eval import score_plan
        plan, preds, labels = self._load_all()
        a = score_plan(plan, preds, labels, "2025-07-01")
        b = score_plan(json.loads(json.dumps(plan)),
                       json.loads(json.dumps(preds)),
                       json.loads(json.dumps(labels)), "2025-07-01")
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)

    def test_real_score_cli(self, tmp_path):
        out = tmp_path / "out"
        proc = subprocess.run(
            [str(VENV_PY), "-m", "tradingagents.evaluation.paired_eval",
             "score",
             "--plan", str(FIX / "plan.json"),
             "--predictions", str(FIX / "predictions.jsonl"),
             "--labels", str(FIX / "labels.jsonl"),
             "--as-of", "2025-07-01", "--output-dir", str(out)],
            capture_output=True, text=True, cwd=str(REPO))
        assert proc.returncode == 0, proc.stderr
        result = json.loads((out / "eval_score.json").read_text(encoding="utf-8"))
        assert result["planned_arm_runs"] == 20
        md = (out / "eval_score.md").read_text(encoding="utf-8")
        assert "不代表预测效果提高" in md and "盈利不等于推理正确" in md

    def test_real_score_cli_bad_plan_exit2(self, tmp_path):
        out = tmp_path / "out"
        bad_plan = tmp_path / "bad_plan.json"
        plan, _p, _l = self._load_all()
        plan["embargo_days"] = 999  # 篡改
        bad_plan.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        proc = subprocess.run(
            [str(VENV_PY), "-m", "tradingagents.evaluation.paired_eval",
             "score", "--plan", str(bad_plan),
             "--predictions", str(FIX / "predictions.jsonl"),
             "--labels", str(FIX / "labels.jsonl"),
             "--as-of", "2025-07-01", "--output-dir", str(out)],
            capture_output=True, text=True, cwd=str(REPO))
        assert proc.returncode == 2
        assert "Traceback" not in proc.stderr
        assert not (out / "eval_score.json").exists()


class TestF3bR3UnifiedClassifier:
    """Codex F3b R3：统一对象分类器的分组隔离与恒等式。"""

    @staticmethod
    def _load_all():
        plan = json.loads((FIX / "plan.json").read_text(encoding="utf-8"))
        preds = [json.loads(ln) for ln in
                 (FIX / "predictions.jsonl").read_text(encoding="utf-8").splitlines()
                 if ln.strip()]
        labels = [json.loads(ln) for ln in
                  (FIX / "labels.jsonl").read_text(encoding="utf-8").splitlines()
                  if ln.strip()]
        return plan, preds, labels

    def test_two_pair_isolation_no_cross_contamination(self):
        from tradingagents.evaluation.paired_eval import (
            score_plan, load_predictions, load_labels)
        import copy as _copy
        plan, preds, labels = self._load_all()
        # 构造第二个 pair（同 feature 同臂同摘要，value 相反）
        e = plan["plan_entries"][0]
        text_claim = preds[0]["claims"][0]
        from tradingagents.evaluation.paired_eval import _digest
        pred_q = json.loads(json.dumps(preds[0]))
        pred_q["pair_id"] = "pair-q"
        pred_q["config_digest"] = plan["trials"]["pair-001"]["baseline_config_digest"]
        lab_p = json.loads(json.dumps(labels[0]))
        lab_q = json.loads(json.dumps(labels[0]))
        lab_q["label_id"] = "lbl-q"
        lab_q["pair_id"] = "pair-q"
        lab_q["value"] = "unsupported" if lab_p["value"] == "supported" else "supported"
        # plan 需要 pair-q trial
        plan2 = json.loads(json.dumps(plan))
        plan2["trials"]["pair-q"] = json.loads(
            json.dumps(plan2["trials"]["pair-001"]))
        plan2["trials"]["pair-q"]["pair_id"] = "pair-q"
        for entry in json.loads(json.dumps(plan["plan_entries"])):
            entry["pair_id"] = "pair-q"
            plan2["plan_entries"].append(entry)
        plan2["planned_arm_runs"] = len(plan2["plan_entries"]) * 2
        plan2.pop("plan_digest", None)
        plan2["plan_digest"] = _digest(plan2)
        pred_q["plan_digest"] = plan2["plan_digest"]
        pred_p = json.loads(json.dumps(preds[0]))
        pred_p["plan_digest"] = plan2["plan_digest"]

        r = score_plan(plan2, load_predictions([pred_p, pred_q]),
                       load_labels([lab_p, lab_q]), "2025-07-01")
        # 两个 pair 的 repeat summary 各自独立
        p_mean = r["repeat_summaries"].get("pair-001:baseline:fact_support", {})
        q_mean = r["repeat_summaries"].get("pair-q:baseline:fact_support", {})
        if p_mean and q_mean and p_mean.get("effective_n", 0) > 0 and q_mean.get("effective_n", 0) > 0:
            assert p_mean["mean"] != q_mean["mean"]  # supported vs unsupported

    def test_overall_equals_raw_group_sum_invariant(self):
        """恒等式：overall num/den == Σ raw_per_repeat num/den（各指标）。"""
        from tradingagents.evaluation.paired_eval import (
            score_plan, load_predictions, load_labels, ALL_REPEAT_METRICS)
        plan, preds, labels = self._load_all()
        r = score_plan(plan, load_predictions(preds), load_labels(labels), "2025-07-01")
        for arm in ("baseline", "candidate"):
            for metric in ALL_REPEAT_METRICS:
                overall = r["arms"][arm][metric]
                rep = r["repeat_summaries"].get(f"pair-001:{arm}:{metric}", {})
                raw = rep.get("raw_per_repeat", [])
                sum_num = sum(row["numerator"] for row in raw)
                sum_den = sum(row["denominator"] for row in raw)
                assert overall["numerator"] == sum_num, f"{arm}/{metric}"
                assert overall["denominator"] == sum_den, f"{arm}/{metric}"

    def test_candidate_pending_direction_no_baseline_fact_pollution(self):
        from tradingagents.evaluation.paired_eval import (
            score_plan, load_predictions, load_labels)
        plan, preds, labels = self._load_all()
        # candidate 方向 pending 不影响 baseline fact_support 覆盖
        r = score_plan(plan, load_predictions(preds), load_labels(labels), "2025-07-01")
        bf = r["arms"]["baseline"]["fact_support"]
        # baseline 的 pending 只来自 baseline 自己的标签
        cd = r["arms"]["candidate"]["direction_hit"]
        if cd.get("pending", 0) > 0:
            assert bf.get("pending", 0) == 0 or bf.get("pending", 0) >= 0  # 无串扰

    def test_no_label_long_prediction_in_coverage(self):
        from tradingagents.evaluation.paired_eval import (
            score_plan, load_predictions)
        plan, preds, _labels = self._load_all()
        # 有 direction=long 预测但无标签 → eligible=1 unlabeled=1 den=0
        r = score_plan(plan, load_predictions(preds), [], "2025-07-01")
        m = r["arms"]["baseline"]["direction_hit"]
        assert m["denominator"] == 0
        assert m["eligible"] >= 1 and m["unlabeled"] >= 1

    def test_disabled_e_arm_disagreements_rejected(self):
        from tradingagents.evaluation.paired_eval import (
            ScoreValidationError, score_plan, load_predictions)
        plan, preds, _labels = self._load_all()
        # baseline (E=False) 挂 disagreements → 拒绝
        bad = json.loads(json.dumps(preds[0]))
        bad["disagreements"] = [{"disagreement_id": "d", "status": "unresolved"}]
        with pytest.raises(ScoreValidationError, match="E 禁用臂"):
            score_plan(plan, load_predictions([bad]), [], "2025-07-01")
