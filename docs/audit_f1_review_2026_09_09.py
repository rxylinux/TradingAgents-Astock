"""Codex independent F1 audit: declared metadata is not validation evidence."""
import copy
import json
import subprocess
import sys

import pytest

from tradingagents.evaluation import review_record as rr


def record(rid='a'):
    return {'schema_version': 1, 'record_id': rid,
            'identity': {'run_id': 'run-a', 'ticker': '600519', 'instrument_type': 'stock',
                         'window': {'start': '2024-11-05', 'end': '2025-05-05'}, 'version': 1},
            'decision': {'rating': 'Buy', 'prediction_horizon': 'explicit window',
                         'decided_at': '2024-11-05T18:00:00+08:00'},
            'outcome': {'status': 'resolved', 'maturity_date': '2025-05-05',
                        'observed_at': '2025-05-06', 'publication_time': '2025-05-06',
                        'raw_return': .12, 'alpha_return': .05, 'max_adverse_excursion': -.08,
                        'fees_assumption': None},
            'record_available_at': '2025-05-07', 'availability_source': 'declared_only'}


def retrieve(items):
    return rr.retrieve_as_of(items, ticker='600519', instrument_type='stock',
                             as_of='2025-06-01', limit=10)


@pytest.mark.parametrize('bad', ['unknown_schema', 'blank_run', 'bad_window', 'unknown_record_ranking'])
def test_load_rejects_unverifiable_identity_and_versions(bad):
    r = record()
    if bad == 'unknown_schema':
        r['schema_version'] = 999
    elif bad == 'blank_run':
        r['identity']['run_id'] = ''
    elif bad == 'bad_window':
        r['identity']['window'] = ['broken']
    else:
        r['retrieval_meta'] = {'ranking_version': 999}
    with pytest.raises(rr.RecordValidationError):
        rr.load_records([r])


@pytest.mark.parametrize('bps', [float('nan'), float('inf')])
def test_fees_must_be_finite(bps):
    r = record()
    r['outcome']['fees_assumption'] = {'bps': bps, 'declared_in': 'explicit-fixture'}
    with pytest.raises(rr.RecordValidationError):
        rr.load_records([r])


def test_future_annotation_not_visible_in_historical_result():
    r = record()
    r['annotations'] = {'error_type': 'reasoning', 'correct_risk_flags': ['FUTURE_LABEL_SENTINEL'],
                        'source': 'manual', 'annotated_at': '2099-01-01'}
    try:
        loaded = rr.load_records([r])
    except rr.RecordValidationError:
        return  # rejecting inconsistent record-version availability is also safe
    result = retrieve(loaded)
    assert 'FUTURE_LABEL_SENTINEL' not in json.dumps(result)


def test_declared_verified_external_snapshot_digest_must_match():
    r = record()
    r['decision']['thesis_digest'] = {'digest': 'sha256:' + '0' * 64,
                                     'snapshot': {'thesis': 'offline content'},
                                     'verification': 'verified_snapshot'}
    with pytest.raises(rr.RecordValidationError):
        rr.load_records([r])


def test_dedup_does_not_invalidate_own_record_digest():
    r = record()
    loaded = rr.load_records([r, copy.deepcopy(r)])
    assert len(loaded) == 1
    assert loaded[0]['record_digest'] == rr.compute_record_digest(loaded[0])
    assert rr.load_records(loaded) == loaded


def test_reversed_input_has_identical_exclusion_report():
    a, b = record('a'), record('b')
    a['outcome']['publication_time'] = '2099-01-01'
    b['record_available_at'] = '2099-01-01'
    assert retrieve(rr.load_records([a, b])) == retrieve(rr.load_records([b, a]))


def test_actual_cli_rejects_unknown_schema_without_success_reports(tmp_path):
    r = record()
    r['schema_version'] = 999
    path = tmp_path / 'bad.jsonl'
    path.write_text(json.dumps(r) + '\n')
    output = tmp_path / 'out'
    proc = subprocess.run([sys.executable, '-m', 'tradingagents.evaluation.review_record',
                           '--file', str(path), '--as-of', '2025-06-01', '--ticker', '600519',
                           '--instrument-type', 'stock', '--limit', '5', '--output-dir', str(output)],
                          capture_output=True, text=True)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert not (output / 'review_retrieval.json').exists()
    assert not (output / 'review_retrieval.md').exists()


def test_valid_control_preserves_deep_input_and_nulls():
    r = record()
    original = copy.deepcopy(r)
    loaded = rr.load_records([r])
    output = retrieve(loaded)
    assert len(output['records']) == 1
    assert output['records'][0]['outcome']['fees_assumption'] is None
    output['records'][0]['identity']['ticker'] = 'changed'
    assert retrieve(loaded)['records'][0]['identity']['ticker'] == '600519'
    assert r == original


@pytest.mark.parametrize('field', ['schema_version', 'ranking_argument'])
def test_bool_cannot_alias_version_one(field):
    r = record()
    with pytest.raises(rr.RecordValidationError):
        if field == 'schema_version':
            r['schema_version'] = True
            rr.load_records([r])
        else:
            rr.retrieve_as_of(rr.load_records([r]), ticker='600519', instrument_type='stock',
                              as_of='2025-06-01', limit=10, ranking_version=True)


def test_overlap_only_applies_to_same_instrument_and_type():
    r = record()
    r['identity']['ticker'] = '000001'
    result = rr.retrieve_as_of(rr.load_records([r]), ticker='600519', instrument_type='stock',
                               as_of='2025-06-01', limit=10,
                               query_window={'start': '2024-11-05', 'end': '2025-05-05'})
    assert [item['record_id'] for item in result['records']] == ['a']


@pytest.mark.parametrize('verification', [None, 'external_unverified'])
def test_supplied_snapshot_is_checked_regardless_of_claimed_verification(verification):
    r = record()
    r['decision']['thesis_digest'] = {'digest': 'sha256:' + '0' * 64,
                                     'snapshot': {'thesis': 'offline content'},
                                     'verification': verification}
    with pytest.raises(rr.RecordValidationError):
        rr.load_records([r])


def test_actual_cli_nonfinite_json_has_controlled_rejection(tmp_path):
    r = record()
    r['extra_annotation'] = float('nan')
    path = tmp_path / 'bad.jsonl'
    path.write_text(json.dumps(r) + '\n')
    output = tmp_path / 'out'
    proc = subprocess.run([sys.executable, '-m', 'tradingagents.evaluation.review_record',
                           '--file', str(path), '--as-of', '2025-06-01', '--ticker', '600519',
                           '--instrument-type', 'stock', '--limit', '5', '--output-dir', str(output)],
                          capture_output=True, text=True)
    assert proc.returncode == 2, proc.stderr
    assert 'Traceback' not in proc.stderr
    assert not (output / 'review_retrieval.json').exists()
