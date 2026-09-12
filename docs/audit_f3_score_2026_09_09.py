"""Codex F3 score audit: independent small arithmetic and frozen identities."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from docs.audit_f3_plan_2026_09_09 import digest, feature, splits, trials
from tradingagents.evaluation import paired_eval as pe


def plan():
    return pe.build_plan([feature()], trials(), splits())


def prediction(p, arm='baseline'):
    e = p['plan_entries'][0]
    text = 'AUDIT factual claim'
    return {'plan_digest': p['plan_digest'], 'pair_id': 'p', 'feature_id': 'f',
            'arm': arm, 'repeat_index': 1,
            'feature_snapshot_digest': e['feature_snapshot_digest'],
            'config_digest': e[f'{arm}_config_digest'],
            'generated_at': '2024-03-02', 'status': 'completed',
            'claims': [{'claim_id': 'claim-' + digest({'text': text})[:16], 'text': text}],
            'numeric_references': [], 'disagreements': [], 'direction': None,
            'usage': {}, 'costs': {}}


def label(pr, lid='l'):
    c = pr['claims'][0]
    return {'label_id': lid, 'pair_id': 'p', 'feature_id': 'f', 'arm': pr['arm'],
            'repeat_index': 1, 'label_kind': 'fact_support',
            'target_claim_id': c['claim_id'], 'target_claim_digest': digest({'text': c['text']}),
            'value': 'supported', 'labeled_by': 'independent-audit',
            'label_available_at': '2024-03-03'}


def score(p, ps, ls):
    return pe.score_plan(p, pe.load_predictions(ps), pe.load_labels(ls), '2025-07-01')


def test_prediction_config_digest_must_match_exact_arm():
    p = plan()
    pr = prediction(p)
    pr['config_digest'] = p['plan_entries'][0]['candidate_config_digest']
    with pytest.raises(pe.ScoreValidationError):
        score(p, [pr], [])


def test_fact_label_requires_target_identity():
    p = plan()
    pr = prediction(p)
    lab = label(pr)
    lab.pop('target_claim_id')
    lab.pop('target_claim_digest')
    try:
        r = score(p, [pr], [lab])
    except pe.ScoreValidationError:
        return
    assert r['arms']['baseline']['fact_support']['denominator'] == 0


def test_conflicting_labels_same_target_are_not_two_observations():
    p = plan()
    pr = prediction(p)
    a, b = label(pr, 'a'), label(pr, 'b')
    b['value'] = 'unsupported'
    with pytest.raises(pe.ScoreValidationError):
        score(p, [pr], [a, b])


def test_temporal_violation_denominator_counts_runs_not_labels():
    p = plan()
    pr = prediction(p)
    text = 'second independent fact'
    pr['claims'].append({'text': text, 'claim_id': 'claim-' + digest({'text': text})[:16]})
    a, b = label(pr, 'a'), label(pr, 'b')
    for lab in (a, b):
        lab.update(label_kind='temporal_audit', value='violation')
    b.update(target_claim_id=pr['claims'][1]['claim_id'], target_claim_digest=digest({'text': text}))
    r = score(p, [pr], [a, b])
    assert r['arms']['baseline']['temporal_violation']['denominator'] == 1
    assert r['arms']['baseline']['temporal_violation']['numerator'] == 1


def test_numeric_label_cannot_invent_a_reference():
    p = plan()
    pr = prediction(p)
    lab = label(pr)
    lab.update(label_kind='numeric_verification', value='incorrect')
    try:
        r = score(p, [pr], [lab])
    except pe.ScoreValidationError:
        return
    assert r['arms']['baseline']['numeric_error']['denominator'] == 0


def test_direction_label_without_prediction_is_unassessable():
    p = plan()
    pr = prediction(p)
    lab = label(pr)
    lab.update(label_kind='direction', value='long', outcome_maturity='2024-06-01',
               outcome_observed_at='2024-06-02', outcome_publication_time='2024-06-03')
    r = score(p, [pr], [lab])
    assert r['arms']['baseline']['direction_hit']['denominator'] == 0


def test_absent_usage_including_missing_prediction_is_unknown():
    p = plan()
    r = score(p, [prediction(p)], [])
    assert r['arms']['candidate']['counts']['missing'] == 1
    assert all(r['usage_unknown_counts'][k] == 2 for k in
               ('llm_calls', 'http_requests', 'tool_invocations', 'tokens'))


def test_self_consistent_plan_still_revalidates_feature_snapshot():
    p = plan()
    p['plan_entries'][0]['feature_snapshot']['feature_available_at'] = '2099-01-01'
    p['plan_digest'] = digest({k: v for k, v in p.items() if k != 'plan_digest'})
    with pytest.raises(pe.ScoreValidationError):
        score(p, [], [])


@pytest.mark.parametrize('amount', ['NaN', 'Infinity'])
def test_decimal_string_cost_must_be_finite(amount):
    p = plan()
    pr = prediction(p)
    pr['costs'] = {'CNY': amount}
    with pytest.raises(pe.ScoreValidationError):
        score(p, [pr], [])


def test_score_public_api_revalidates_prediction_schema():
    p = plan()
    pr = prediction(p)
    pr['status'] = 'bogus'
    with pytest.raises(pe.ScoreValidationError):
        pe.score_plan(p, [pr], [], '2025-07-01')


def test_result_identity_covers_actual_label_input():
    p = plan()
    pr = prediction(p)
    a = label(pr)
    b = deepcopy(a)
    b['labeled_by'] = 'different-independent-source'
    ra, rb = score(p, [pr], [a]), score(p, [pr], [b])
    assert ra['result_digest'] != rb['result_digest']


def test_metrics_report_unlabeled_coverage():
    p = plan()
    r = score(p, [prediction(p)], [])
    m = r['arms']['baseline']['fact_support']
    assert m['value'] is None
    assert m.get('unlabeled') == 1 and m.get('eligible') == 1


def test_valid_labeled_baseline_and_missing_candidate_control():
    p = plan()
    pr = prediction(p)
    r = score(p, [pr], [label(pr)])
    assert r['arms']['baseline']['fact_support']['value'] == 1.0
    assert r['arms']['candidate']['counts']['missing'] == 1
    assert r['planned_arm_runs'] == 2


def test_same_day_generation_cannot_precede_feature_availability():
    f = feature()
    f['feature_available_at'] = '2024-03-01T20:00:00+08:00'
    p = pe.build_plan([f], trials(), splits())
    pr = prediction(p)
    pr['generated_at'] = '2024-03-01T08:00:00+08:00'
    with pytest.raises(pe.ScoreValidationError):
        score(p, [pr], [])


def test_repeat_summary_aggregates_features_inside_each_repeat():
    t = trials()
    t['p']['repeat_count'] = 2
    p = pe.build_plan([feature('f'), feature('g')], t, splits())
    ps, labs = [], []
    for i, e in enumerate(p['plan_entries']):
        pr = prediction(p)
        pr.update(feature_id=e['feature_id'], repeat_index=e['repeat_index'],
                  feature_snapshot_digest=e['feature_snapshot_digest'])
        lab = label(pr, f'label-{i}')
        lab.update(feature_id=pr['feature_id'], repeat_index=pr['repeat_index'])
        ps.append(pr)
        labs.append(lab)
    r = score(p, ps, labs)
    s = r['repeat_summaries']['p:baseline:fact_support']
    assert s['planned_n'] == 2 and s['effective_n'] == 2
    assert s['mean'] == 1 and s['population_stddev'] == 0


def test_empty_completed_prediction_does_not_count_as_conclusion():
    p = plan()
    pr = prediction(p)
    pr['claims'] = []
    try:
        r = score(p, [pr], [])
    except pe.ScoreValidationError:
        return
    assert r['arms']['baseline']['counts']['completed'] == 0


def test_pending_direction_keeps_metric_coverage():
    p = plan()
    pr = prediction(p)
    pr['direction'] = 'long'
    lab = label(pr)
    lab.update(label_kind='direction', value='long', outcome_maturity='2099-01-01',
               outcome_observed_at='2025-01-01', outcome_publication_time='2025-01-01')
    r = score(p, [pr], [lab])
    m = r['arms']['baseline']['direction_hit']
    assert m['denominator'] == 0 and m['eligible'] == 1
    assert m.get('pending') == 1


def test_markdown_contains_actual_currency_cost_and_coverage():
    p = plan()
    pr = prediction(p)
    pr['costs'] = {'CNY': '0.123456789'}
    md = pe.render_score_md(score(p, [pr], []))
    assert 'CNY' in md and '0.123456789' in md
    assert 'unlabeled' in md or '未标注' in md


def test_score_plan_boolean_schema_is_not_version_one():
    p = plan()
    p['schema_version'] = True
    p['plan_digest'] = digest({k: v for k, v in p.items() if k != 'plan_digest'})
    with pytest.raises(pe.ScoreValidationError):
        score(p, [], [])


def test_score_entry_identity_must_match_snapshot_identity():
    p = plan()
    e = p['plan_entries'][0]
    e['feature_snapshot']['feature_id'] = 'different-feature'
    e['feature_snapshot_digest'] = digest(e['feature_snapshot'])
    p['plan_digest'] = digest({k: v for k, v in p.items() if k != 'plan_digest'})
    with pytest.raises(pe.ScoreValidationError):
        score(p, [], [])


def test_actual_score_cli_final_file_digest_is_self_consistent(tmp_path):
    p = plan()
    pr = prediction(p)
    for name, obj in [('plan', p), ('predictions', pr), ('labels', label(pr))]:
        (tmp_path / f'{name}.json').write_text(json.dumps(obj))
    proc = subprocess.run([
        sys.executable, '-m', 'tradingagents.evaluation.paired_eval', 'score',
        '--plan', str(tmp_path / 'plan.json'), '--predictions', str(tmp_path / 'predictions.json'),
        '--labels', str(tmp_path / 'labels.json'), '--as-of', '2025-07-01',
        '--output-dir', str(tmp_path / 'out')], capture_output=True, text=True,
        cwd=Path(__file__).resolve().parents[1])
    assert proc.returncode == 0, proc.stderr
    r = json.loads((tmp_path / 'out/eval_score.json').read_text())
    assert r['result_digest'] == digest({k: v for k, v in r.items() if k != 'result_digest'})


def test_repeat_aggregation_does_not_mix_trial_pairs():
    ts = trials()
    ts['q'] = deepcopy(ts['p'])
    p = pe.build_plan([feature()], ts, splits())
    a, b = prediction(p), prediction(p)
    b['pair_id'] = 'q'
    la, lb = label(a, 'p-label'), label(b, 'q-label')
    lb.update(pair_id='q', value='unsupported')
    r = score(p, [a, b], [la, lb])
    assert r['repeat_summaries']['p:baseline:fact_support']['mean'] == 1.0
    assert r['repeat_summaries']['q:baseline:fact_support']['mean'] == 0.0


def test_candidate_direction_pending_does_not_change_baseline_fact_coverage():
    p = plan()
    a, b = prediction(p), prediction(p, 'candidate')
    b['direction'] = 'long'
    lab = label(b)
    lab.update(label_kind='direction', value='long', outcome_maturity='2099-01-01',
               outcome_observed_at='2025-01-01', outcome_publication_time='2025-01-01')
    r = score(p, [a, b], [lab])
    m = r['arms']['baseline']['fact_support']
    assert m.get('pending', 0) == 0 and m.get('unverifiable', 0) == 0


def test_invalid_direction_target_never_enters_scored_denominator():
    p = plan()
    pr = prediction(p)
    pr['direction'] = 'long'
    lab = label(pr)
    lab.update(label_kind='direction', value='long', target_claim_digest='wrong',
               outcome_maturity='2024-06-01', outcome_observed_at='2024-06-02',
               outcome_publication_time='2024-06-03')
    r = score(p, [pr], [lab])
    assert r['arms']['baseline']['direction_hit']['denominator'] == 0


def test_direction_without_labels_is_still_in_coverage():
    p = plan()
    pr = prediction(p)
    pr['direction'] = 'long'
    m = score(p, [pr], [])['arms']['baseline']['direction_hit']
    assert m['denominator'] == 0
    assert m['eligible'] == 1 and m['unlabeled'] == 1


def test_disabled_e_arm_cannot_score_e_disagreements():
    p = plan()
    pr = prediction(p)
    pr['disagreements'] = [{'disagreement_id': 'd', 'status': 'unresolved'}]
    try:
        r = score(p, [pr], [])
    except pe.ScoreValidationError:
        return
    assert r['arms']['baseline']['e_unresolved']['denominator'] == 0


@pytest.mark.parametrize('metric', ['e_unresolved', 'e_inconclusive'])
def test_e_status_rate_denominator_is_all_listed_disagreements(metric):
    p = plan()
    pr = prediction(p, 'candidate')
    pr['disagreements'] = [
        {'disagreement_id': 'd-unresolved', 'status': 'unresolved'},
        {'disagreement_id': 'd-inconclusive', 'status': 'inconclusive'},
        {'disagreement_id': 'd-resolved', 'status': 'resolved'},
    ]
    r = score(p, [pr], [])
    m = r['arms']['candidate'][metric]
    assert m['numerator'] == 1 and m['denominator'] == 3
    assert m['value'] == pytest.approx(1 / 3)
    row = r['repeat_summaries'][f'p:candidate:{metric}']['raw_per_repeat'][0]
    assert row['numerator'] == 1 and row['denominator'] == 3


@pytest.mark.parametrize('corrupt', ['special_string_digest', 'wrong_target_id'])
def test_direction_target_identity_has_no_magic_bypass(corrupt):
    p = plan()
    pr = prediction(p)
    pr['direction'] = 'long'
    lab = label(pr)
    lab.update(label_kind='direction', value='long', outcome_maturity='2024-06-01',
               outcome_observed_at='2024-06-02', outcome_publication_time='2024-06-03')
    if corrupt == 'special_string_digest':
        lab['target_claim_digest'] = 'wrong-placeholder'
    else:
        lab['target_claim_id'] = 'does-not-exist'
    r = score(p, [pr], [lab])
    assert r['arms']['baseline']['direction_hit']['denominator'] == 0
