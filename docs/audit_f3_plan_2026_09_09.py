"""Codex independent F3 plan audit: offline actual plan API and CLI."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from tradingagents.evaluation import paired_eval as pe


def feature(fid='f'):
    return {'feature_id': fid, 'ticker': '600519', 'instrument_type': 'stock',
            'prediction_at': '2024-03-01', 'feature_available_at': '2024-02-28',
            'decision': {'rating': 'Buy', 'prediction_horizon': 'explicit',
                         'decided_at': '2024-02-27'},
            'target_window': {'start': '2024-03-02', 'end': '2024-06-01'}}


def trials():
    return {'p': {'repeat_count': 1, 'arms': {
        'baseline': {'normalized_config': {'model': 'offline', 'evidence_debate_enabled': False}},
        'candidate': {'normalized_config': {'model': 'offline', 'evidence_debate_enabled': True}}}}}


def splits():
    return {'train': {'start': '2024-01-01', 'end': '2024-06-30'},
            'validation': {'start': '2024-07-01', 'end': '2024-12-31'},
            'holdout': {'start': '2025-01-01', 'end': '2025-12-31'}}


def digest(obj):
    return 'sha256:' + hashlib.sha256(json.dumps(
        obj, sort_keys=True, ensure_ascii=False, separators=(',', ':'),
        allow_nan=False).encode()).hexdigest()


def test_binary_event_nested_extra_cannot_leak_labels():
    f = feature()
    f['binary_event'] = {'event': 'explicit binary event', 'resolve_by': '2024-06-01',
                         'future_answer': 'AUDIT_HOLDOUT_ANSWER'}
    with pytest.raises(pe.PlanValidationError):
        pe.build_plan([f], trials(), splits())


@pytest.mark.parametrize('case', ['missing_vs_null', 'boolean_vs_integer', 'false_claimed_digest'])
def test_configuration_identity_uses_exact_typed_content(case):
    t = trials()
    b = t['p']['arms']['baseline']
    c = t['p']['arms']['candidate']
    if case == 'missing_vs_null':
        c['normalized_config']['extra'] = None
    elif case == 'boolean_vs_integer':
        b['normalized_config']['setting'] = True
        c['normalized_config']['setting'] = 1
    else:
        b['config_digest'] = 'sha256:' + '0' * 64
    with pytest.raises(pe.PlanValidationError):
        pe.build_plan([feature()], t, splits())


@pytest.mark.parametrize('case', ['equal_endpoint', 'offset_lexical_order'])
def test_overlapping_split_intervals_are_rejected(case):
    s = splits()
    if case == 'equal_endpoint':
        s['train']['end'] = s['validation']['start']
    else:
        s['train']['end'] = '2024-07-01T00:30:00-04:00'
        s['validation']['start'] = '2024-07-01T08:00:00+08:00'
    with pytest.raises(pe.PlanValidationError):
        pe.build_plan([feature()], trials(), s)


def test_reverse_unassigned_input_keeps_plan_digest_identical():
    a, b = feature('unassigned-a'), feature('unassigned-b')
    a['prediction_at'] = b['prediction_at'] = '2026-01-01'
    assert pe.build_plan([a, b], trials(), splits()) == pe.build_plan([b, a], trials(), splits())


def test_duplicate_feature_ids_rejected_even_if_not_assigned():
    a = feature('duplicate')
    a['prediction_at'] = '2026-01-01'
    b = deepcopy(a)
    b['ticker'] = '000001'
    with pytest.raises(pe.PlanValidationError):
        pe.build_plan([a, b], trials(), splits())


def test_plan_budget_rejected_before_snapshot_expansion(monkeypatch):
    calls = []
    original = pe.feature_snapshot
    monkeypatch.setattr(pe, 'MAX_PLANNED_ARM_RUNS', 2)

    def counted(f):
        calls.append(f['feature_id'])
        return original(f)

    monkeypatch.setattr(pe, 'feature_snapshot', counted)
    with pytest.raises(pe.PlanValidationError):
        pe.build_plan([feature('a'), feature('b')], trials(), splits())
    assert calls == []


def test_actual_cli_emits_self_consistent_plan_digest(tmp_path):
    for name, data in [('features', feature()), ('trials', trials()), ('splits', splits())]:
        (tmp_path / f'{name}.json').write_text(json.dumps(data), encoding='utf-8')
    out = tmp_path / 'out'
    proc = subprocess.run([sys.executable, '-m', 'tradingagents.evaluation.paired_eval',
                           '--features', str(tmp_path / 'features.json'),
                           '--trials', str(tmp_path / 'trials.json'),
                           '--splits', str(tmp_path / 'splits.json'),
                           '--output-dir', str(out)], capture_output=True, text=True,
                          cwd=Path(__file__).resolve().parents[1])
    assert proc.returncode == 0, proc.stderr
    plan = json.loads((out / 'eval_plan.json').read_text())
    assert plan['plan_digest'] == digest({k: v for k, v in plan.items() if k != 'plan_digest'})


def test_valid_api_plan_is_self_consistent_control():
    p = pe.build_plan([feature()], trials(), splits())
    assert p['planned_arm_runs'] == 2
    assert p['plan_digest'] == digest({k: v for k, v in p.items() if k != 'plan_digest'})
