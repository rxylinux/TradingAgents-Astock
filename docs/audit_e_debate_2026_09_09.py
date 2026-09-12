"""Codex independent E boundary audits. Offline transports and actual E nodes."""
import json

import httpx
import pytest
from langgraph.prebuilt import ToolNode
from langgraph.graph import END, START, StateGraph
from tradingagents.agents.utils.agent_states import AgentState

from tradingagents.agents import debate_evidence as de
from tradingagents.dataflows import a_stock, interface
from tradingagents.evidence import graph_tools
from tradingagents.graph.conditional_logic import ConditionalLogic
from tradingagents.graph.setup import GraphSetup
from tradingagents.llm_clients.openai_client import NormalizedChatOpenAI


def state(**args):
    result = {'company_of_interest': '600519', 'trade_date': '2024-11-05',
              'run_metadata': {'run_id': 'audit-e', 'evidence_debate': {'enabled': True, 'schema_version': 1}}}
    params = {'ticker': '600519', 'start_date': '2024-10-01', 'end_date': '2024-11-05'}
    params.update(args)
    plan = de.DisagreementPlanModel(
        disagreements=[de.DisagreementItem(topic='fact', conflict_kind='fact', decision_impact='changes thesis')],
        recheck_questions=[de.QuestionPlan(question='verify fact', disagreement_index=0,
                                          decision_impact='changes thesis', tool_name='get_news', tool_args=params)])
    result['evidence_debate'] = de.validate_plan(plan)
    return result


def client(handler, retries=0):
    return NormalizedChatOpenAI(model='gpt-4o', api_key='offline-test',
                                base_url='https://offline.invalid/v1', max_retries=retries,
                                max_tokens=9000, http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def success(req):
    body = json.loads(req.content)
    name = body['tools'][0]['function']['name']
    return httpx.Response(200, json={
        'id': 'offline', 'object': 'chat.completion', 'created': 0, 'model': 'gpt-4o',
        'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': None,
                     'tool_calls': [{'id': 't', 'type': 'function', 'function': {
                         'name': name, 'arguments': json.dumps({'direction': 'long', 'top_claims': []})}}]},
                     'finish_reason': 'tool_calls'}],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}})


def test_output_cap_reaches_actual_http_request():
    requests = []

    def send(req):
        requests.append(json.loads(req.content))
        return success(req)

    llm = client(send)
    de.create_initial_view_node('bull', llm)(state())
    assert len(requests) == 1
    assert requests[0].get('max_completion_tokens', requests[0].get('max_tokens')) <= 2048
    assert llm.max_tokens == 9000, 'shared client must remain unchanged'


def test_explicit_zero_retry_budget_reaches_http(monkeypatch):
    requests = []

    def send(req):
        requests.append(req)
        return httpx.Response(429, json={'error': {'message': 'offline limit', 'type': 'rate_limit_error'}})

    monkeypatch.setattr('tradingagents.agents.utils.structured.time.sleep', lambda _: None)
    try:
        de.create_initial_view_node('bull', client(send, retries=0))(state())
    except Exception:
        pass  # outcome may be controlled failure or raised; count is independently observable
    assert len(requests) == 1


@pytest.mark.parametrize('ticker', ['600519', '000001'])
def test_recheck_cannot_fetch_another_stock(monkeypatch, ticker):
    fetched = []
    monkeypatch.setattr(a_stock, '_fetch_news_eastmoney', lambda code, page_size=20: fetched.append(code) or [])
    monkeypatch.setattr(a_stock, '_fetch_news_sina', lambda code, page_size=20: [])
    monkeypatch.setattr(interface, 'get_vendor', lambda *a, **k: 'a_stock')
    wf = StateGraph(AgentState)
    wf.add_node('recheck', de.create_recheck_node(0, [graph_tools.get_news]))
    wf.add_edge(START, 'recheck')
    wf.add_edge('recheck', END)
    wf.compile().invoke(state(ticker=ticker))
    if ticker == '600519':
        assert fetched == ['600519'], 'valid-control actual vendor route must execute'
    else:
        assert not fetched or set(fetched) == {'600519'}, 'planner ticker escaped trusted run instrument'


@pytest.mark.parametrize('change', [{'start_date': 'not-a-date'}, {'end_date': 'not-a-date'},
                                   {'start_date': '2024-12-01'}])
def test_invalid_date_or_reversed_window_rejected_before_tool(monkeypatch, change):
    invokes = []
    monkeypatch.setattr(ToolNode, 'invoke', lambda self, data, **kwargs: invokes.append(data) or {'messages': []})
    de.create_recheck_node(0, [graph_tools.get_news])(state(**change))
    assert not invokes, 'invalid time window consumed actual ToolNode invoke'


def test_missing_trusted_date_does_not_dispatch_tool(monkeypatch):
    invokes = []
    monkeypatch.setattr(ToolNode, 'invoke', lambda self, data, **kwargs: invokes.append(data) or {'messages': []})
    st = state(end_date='2099-01-01')
    st['trade_date'] = ''
    de.create_recheck_node(0, [graph_tools.get_news])(st)
    assert not invokes


def test_production_graph_does_not_expand_registered_tool_set(monkeypatch):
    registered = []
    monkeypatch.setattr(de, 'create_recheck_node',
                        lambda slot, evidence_tools: registered.append([t.name for t in evidence_tools]) or (lambda state: {}))
    setup = GraphSetup(object(), object(), {'news': ToolNode([graph_tools.get_news])},
                       ConditionalLogic(), evidence_debate_enabled=True)
    setup.setup_graph(['news'])
    assert registered and all(set(names) <= {'get_news'} for names in registered)


def test_summary_character_budget_includes_trailer():
    st = state()
    st['evidence_debate']['disagreements'][0]['topic'] = 'x' * 5000
    assert len(de.evidence_debate_summary_for_prompt(st, max_chars=2400)) <= 2400


def test_completed_question_control_never_replays(monkeypatch):
    invokes = []
    monkeypatch.setattr(ToolNode, 'invoke', lambda self, data, **kwargs: invokes.append(data) or {'messages': []})
    st = state()
    st['evidence_debate']['recheck_questions'][0].update(status='done', recheck={'status': 'inconclusive'})
    assert de.create_recheck_node(0, [graph_tools.get_news])(st) == {}
    assert not invokes


class PlanStub:
    def __init__(self):
        self.prompts = []

    def with_structured_output(self, schema, **kwargs):
        return self

    def invoke(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return de.DisagreementPlanModel(
            disagreements=[de.DisagreementItem(topic='fact', conflict_kind='fact', decision_impact='changes thesis')],
            recheck_questions=[de.QuestionPlan(question='verify', disagreement_index=0,
                                              decision_impact='changes thesis', tool_name='get_global_news',
                                              tool_args={'curr_date': '2024-11-05'})])


@pytest.mark.parametrize('allowed', [[], ['get_news']])
def test_actual_planner_honors_empty_and_restricted_whitelist(allowed):
    llm = PlanStub()
    result = de.create_disagreement_planner_node(llm, allowed_tools=allowed)(state())['evidence_debate']
    assert result['eligible_questions'] == 0
    assert not any(q.get('status') == 'pending' for q in result['recheck_questions'])


def test_unsupported_cap_never_silently_invokes_uncapped_provider():
    invokes = []

    class NoCap:
        def with_structured_output(self, schema, **kwargs):
            if kwargs:
                raise TypeError('cap unsupported')
            return self

        def invoke(self, prompt, **kwargs):
            invokes.append(kwargs)
            return de.InitialViewModel(direction='long')

    try:
        de.create_initial_view_node('bull', NoCap())(state())
    except Exception:
        pass  # refusing before unbounded call is acceptable
    assert not invokes, 'provider silently invoked with no enforceable cap'


def test_actual_empty_structured_response_produces_limited_record():
    def send(req):
        return httpx.Response(200, json={
            'id': 'offline', 'object': 'chat.completion', 'created': 0, 'model': 'gpt-4o',
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'no structured answer'},
                         'finish_reason': 'stop'}]})

    result = de.create_initial_view_node('bull', client(send, retries=1))(state())['initial_view_bull']
    assert result.get('limitations') and not result.get('claims')


def test_resumed_e_state_rejects_foreign_run():
    from types import SimpleNamespace
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    st = state()
    st['evidence_debate']['run_id'] = 'other-run'
    obj = SimpleNamespace(config={'evidence_debate_enabled': True})
    with pytest.raises((ValueError, RuntimeError)):
        TradingAgentsGraph._validate_resumed_evidence_debate(obj, st)


def test_individual_claim_has_finite_text_limit():
    huge = 'x' * 1_000_000
    out = de.validate_view(de.InitialViewModel(direction='long', top_claims=[de.ViewClaim(claim=huge)]),
                           state(), 'bull')
    assert len(out['claims'][0]['claim']) < len(huge)
    assert 'truncat' in json.dumps(out).lower() or '截断' in json.dumps(out, ensure_ascii=False)


def test_known_request_count_is_independent_of_unknown_token_usage():
    view = {'output_mode': 'structured', 'usage': {'actual_requests': 1, 'known_tokens': 'unknown'}}
    counter = de._CountingLLM(object())
    counter.invoke_count = 1
    counter.tokens_known = False
    usage = de.aggregate_usage(view, view, planner_counter=counter)
    assert usage['actual_request_count'] == 3
    assert usage['known_tokens'] == 'unknown'


@pytest.mark.parametrize('corruption', ['missing_trusted_run', 'unknown_schema', 'malformed_plan', 'malformed_view'])
def test_resume_rejects_unverifiable_e_shape_and_version(corruption):
    from types import SimpleNamespace
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    st = state()
    st['evidence_debate']['run_id'] = 'audit-e'
    if corruption == 'missing_trusted_run':
        st['run_metadata'] = {}
    elif corruption == 'unknown_schema':
        st['evidence_debate']['schema_version'] = 999
    elif corruption == 'malformed_plan':
        st['evidence_debate'] = ['broken']
    else:
        st['initial_view_bull'] = 'broken'
    obj = SimpleNamespace(config={'evidence_debate_enabled': True})
    with pytest.raises((ValueError, RuntimeError)):
        TradingAgentsGraph._validate_resumed_evidence_debate(obj, st)


def test_real_production_quality_gate_checkpoint_cannot_skip_newly_enabled_e(tmp_path):
    from tests.test_financial_panel_integration import _FakeGraphBase
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    def workflow(enabled):
        return GraphSetup(object(), object(), {'news': ToolNode([graph_tools.get_news])},
                          ConditionalLogic(), evidence_debate_enabled=enabled).setup_graph(['news'])

    config = {'checkpoint_enabled': True, 'data_cache_dir': str(tmp_path), 'evidence_debate_enabled': False}
    old = _FakeGraphBase(config, workflow=workflow(False))
    try:
        initial, args, _ = old._prepare_graph_run('600519', '2024-11-05')
        old.graph.update_state(args['config'], initial, as_node='Quality Gate')
        assert old.graph.get_state(args['config']).next == ('Bull Researcher',)
    finally:
        TradingAgentsGraph.close_graph_run(old)
    new = _FakeGraphBase(dict(config, evidence_debate_enabled=True), workflow=workflow(True))
    try:
        with pytest.raises((ValueError, RuntimeError)):
            new._prepare_graph_run('600519', '2024-11-05')
    finally:
        TradingAgentsGraph.close_graph_run(new)


def test_real_production_e_on_sqlite_resume_completes_without_replaying_views(tmp_path):
    from tests.test_financial_panel_integration import _FakeGraphBase
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    calls = []

    class Models:
        def with_structured_output(self, schema, **kwargs):
            class Bound:
                def invoke(self, prompt, **kwargs):
                    calls.append(schema.__name__)
                    if schema is de.InitialViewModel:
                        return schema(direction='neutral', top_claims=[])
                    if schema is de.DisagreementPlanModel:
                        return schema(disagreements=[], recheck_questions=[])
                    raise AssertionError('downstream models must not execute during E control')
            return Bound()

    def workflow():
        model = Models()
        return GraphSetup(model, model, {'news': ToolNode([graph_tools.get_news])},
                          ConditionalLogic(), evidence_debate_enabled=True).setup_graph(['news'])

    config = {'checkpoint_enabled': True, 'data_cache_dir': str(tmp_path), 'evidence_debate_enabled': True}
    old = _FakeGraphBase(config, workflow=workflow())
    old.memory_log.get_past_context.return_value = None
    try:
        initial, args, _ = old._prepare_graph_run('600519', '2024-11-05')
        old.graph.update_state(args['config'], initial, as_node='Quality Gate')
        old.graph.invoke(None, args['config'], interrupt_before=['Disagreement Planner'])
        before = old.graph.get_state(args['config'])
        assert before.next == ('Disagreement Planner',)
        saved_views = {k: before.values[k] for k in ('initial_view_bull', 'initial_view_bear')}
        assert calls == ['InitialViewModel', 'InitialViewModel']
    finally:
        TradingAgentsGraph.close_graph_run(old)
    new = _FakeGraphBase(config, workflow=workflow())
    new.memory_log.get_past_context.return_value = None
    try:
        initial2, args2, step = new._prepare_graph_run('600519', '2024-11-05')
        assert initial2 is None and step is not None
        result = new.graph.invoke(None, args2['config'], interrupt_before=['Bull Researcher'])
        assert calls == ['InitialViewModel', 'InitialViewModel', 'DisagreementPlanModel']
        assert new.graph.get_state(args2['config']).next == ('Bull Researcher',)
        assert {k: result[k] for k in saved_views} == saved_views
        assert result['evidence_debate']['usage']['logical_call_count'] == 3
        assert result['evidence_debate']['usage']['tool_invokes'] == 0
    finally:
        TradingAgentsGraph.close_graph_run(new)
