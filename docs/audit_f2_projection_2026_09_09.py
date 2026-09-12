"""Codex F2 independent integration audit. Offline, real helpers/factory."""
import json
from types import SimpleNamespace

import pytest

from docs.audit_f1_review_2026_09_09 import record
from tradingagents.evaluation import review_projection as rp
from tradingagents.graph.trading_graph import TradingAgentsGraph


MARKER = 'AUDIT_F2_VALUE'


def state():
    r = record()
    r['decision']['rating'] = MARKER
    payload = rp.build_review_projection(json.dumps(r), ticker='600519', instrument_type='stock',
                                         as_of='2025-06-01', run_id='audit-f2')
    return {'company_of_interest': '600519', 'trade_date': '2025-06-01', 'instrument_type': 'stock',
            'review_projection': payload,
            'run_metadata': {'run_id': 'audit-f2', 'review_projection': {
                'enabled': True, 'schema_version': 1, 'input_digest': payload['input_digest'],
                'payload_digest': payload['payload_digest'], 'ticker': '600519',
                'instrument_type': 'stock', 'as_of': '2025-06-01'}}}


@pytest.mark.parametrize('corrupt', ['missing_anchor', 'wrong_input_digest', 'wrong_payload_digest', 'missing_trusted_run'])
def test_direct_prompt_requires_independent_metadata_anchor(corrupt):
    st = state()
    if corrupt == 'missing_anchor':
        st['run_metadata'].pop('review_projection')
    elif corrupt == 'missing_trusted_run':
        st['run_metadata'].pop('run_id')
    else:
        key = 'input_digest' if corrupt == 'wrong_input_digest' else 'payload_digest'
        st['run_metadata']['review_projection'][key] = 'different'
    text = rp.projection_for_prompt(st)
    assert 'invalid-projection' in text and MARKER not in text


def test_actual_research_manager_does_not_receive_unanchored_values():
    from tradingagents.agents.managers.research_manager import create_research_manager
    prompts = []

    class LLM:
        def invoke(self, prompt, **kwargs):
            prompts.append(str(prompt))
            return SimpleNamespace(content='offline decision')

    st = state()
    st['run_metadata']['review_projection']['payload_digest'] = 'different'
    st['investment_debate_state'] = {'history': 'offline', 'count': 1}
    create_research_manager(LLM())(st)
    assert len(prompts) == 1
    assert MARKER not in prompts[0] and 'invalid-projection' in prompts[0]


@pytest.mark.parametrize('corrupt', ['bad_context_shape', 'nonfinite_snapshot'])
def test_corrupt_direct_prompt_is_controlled_notice(corrupt):
    st = state()
    if corrupt == 'bad_context_shape':
        st['review_projection']['trusted_context'] = ['broken']
    else:
        st['review_projection']['selected'][0]['raw_return'] = float('nan')
    text = rp.projection_for_prompt(st)
    assert 'invalid-projection' in text and MARKER not in text


@pytest.mark.parametrize('corrupt', ['missing_payload', 'missing_trusted_run', 'wrong_anchor_schema', 'wrong_anchor_context', 'mode_disabled'])
def test_resume_requires_complete_consistent_anchors(corrupt):
    st = state()
    config = {'instrument_type': 'stock', 'review_records_path': '/offline/unused'}
    if corrupt == 'missing_payload':
        st.pop('review_projection')
    elif corrupt == 'missing_trusted_run':
        st['run_metadata'].pop('run_id')
    elif corrupt == 'wrong_anchor_schema':
        st['run_metadata']['review_projection']['schema_version'] = 999
    elif corrupt == 'wrong_anchor_context':
        st['run_metadata']['review_projection']['ticker'] = '000001'
    else:
        config.pop('review_records_path')
    with pytest.raises((ValueError, RuntimeError)):
        TradingAgentsGraph._validate_resumed_review_projection(
            SimpleNamespace(config=config), st, '600519', '2025-06-01')


def test_self_consistent_bad_snapshot_still_fails_schema():
    st = state()
    p = st['review_projection']
    p['selected'][0]['raw_return'] = 'not-a-number'
    p['payload_digest'] = rp.compute_payload_digest(p)
    st['run_metadata']['review_projection']['payload_digest'] = p['payload_digest']
    text = rp.projection_for_prompt(st)
    assert 'invalid-projection' in text and 'not-a-number' not in text


def test_valid_projection_and_missing_projection_controls():
    text = rp.projection_for_prompt(state())
    assert MARKER in text and len(text) <= rp.TOTAL_CHAR_LIMIT
    assert rp.projection_for_prompt({}) == ''


@pytest.mark.parametrize('field', ['company_of_interest', 'instrument_type', 'trade_date'])
def test_prompt_rejects_missing_trusted_context(field):
    st = state()
    st.pop(field)
    text = rp.projection_for_prompt(st)
    assert 'invalid-projection' in text and MARKER not in text


def test_enabled_anchor_missing_payload_is_not_disabled_prompt():
    st = state()
    st.pop('review_projection')
    assert 'invalid-projection' in rp.projection_for_prompt(st)


@pytest.mark.parametrize('field,value', [
    ('maturity_date', '2099-01-01'), ('decided_at', 'not-a-time'),
])
def test_self_consistent_snapshot_time_must_be_eligible(field, value):
    st = state()
    p = st['review_projection']
    p['selected'][0][field] = value
    p['payload_digest'] = rp.compute_payload_digest(p)
    st['run_metadata']['review_projection']['payload_digest'] = p['payload_digest']
    text = rp.projection_for_prompt(st)
    assert 'invalid-projection' in text and MARKER not in text


def test_anchor_schema_float_not_integer_version():
    st = state()
    st['run_metadata']['review_projection']['schema_version'] = 1.0
    text = rp.projection_for_prompt(st)
    assert 'invalid-projection' in text and MARKER not in text


def test_markdown_preserves_limit_and_overlap_audit_information():
    rows = []
    for i in range(6):
        r = record()
        r['record_id'] = f'audit-limit-{i}'
        rows.append(json.dumps(r))
    p = rp.build_review_projection('\n'.join(rows), ticker='600519',
                                   instrument_type='stock', as_of='2025-06-01',
                                   run_id='audit-limit')
    assert len(p['selected']) == 5 and p['limit_truncated'] == 1
    text = rp.render_projection_md(p)
    assert 'limit' in text and ('截断' in text or 'truncated' in text)
    assert 'not_requested' in text


@pytest.mark.parametrize('instrument_type', ['stock', 'index'])
def test_real_fresh_default_off_has_no_projection_block(tmp_path, instrument_type):
    # Reuse only object scaffolding; execute production prepare and actual RM.
    from tests.test_review_projection_integration import _FakeGraphBase
    from tradingagents.agents.managers.research_manager import create_research_manager
    from copy import deepcopy

    fg = _FakeGraphBase({'checkpoint_enabled': False,
                         'data_cache_dir': str(tmp_path),
                         'instrument_type': instrument_type})
    st, _, _ = TradingAgentsGraph.prepare_graph_run(fg, '600519', '2025-06-01')
    assert st['run_metadata']['review_projection']['enabled'] is False
    captured = []

    class LLM:
        def invoke(self, prompt, **kwargs):
            captured.append(str(prompt))
            return SimpleNamespace(content='offline')

    st['investment_debate_state'] = {'history': 'offline', 'count': 1}
    old = deepcopy(st)
    old['run_metadata'].pop('review_projection')
    create_research_manager(LLM())(old)
    create_research_manager(LLM())(st)
    assert len(captured) == 2 and captured[0] == captured[1]
    assert rp.projection_for_prompt(st) == ''


@pytest.mark.parametrize('field', ['run_binding', 'trusted_context'])
def test_report_bad_object_shape_is_controlled(field):
    p = state()['review_projection']
    p[field] = ['AUDIT_BAD_SHAPE_BODY']
    p['payload_digest'] = rp.compute_payload_digest(p)
    text = rp.render_projection_md(p)
    assert '校验失败' in text and 'AUDIT_BAD_SHAPE_BODY' not in text
