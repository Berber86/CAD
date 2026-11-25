"""Offline runtime/credential/batch tests; no live provider or AutoCAD access."""
import copy
import json
from pathlib import Path

import pytest

import llm_parser as lp
import multiple_description_pipeline as pipeline
import multiple_description_parse as md
import multiple_description_runtime as runtime
from test_legal_page_search import make_pdf, response
from test_multiple_description_parse import ON, answer, item


def config():
    cfg = copy.deepcopy(lp.DEFAULT_CONFIG)
    cfg['multiple_description_search']['enabled'] = True
    cfg['api']['provider'] = 'hydra'
    cfg['api']['api_key'] = 'HYDRA_LEGACY_SECRET'
    cfg['api']['providers']['hydra']['api_key'] = 'HYDRA_SECRET'
    cfg['api']['providers']['routerai']['api_key'] = 'ROUTER_SECRET'
    return cfg


def catalog(glm=('text',), grok=('text', 'image')):
    return {'data': [{'id': 'glm-5.2', 'input_modalities': list(glm)},
                     {'id': 'grok-4.7', 'input_modalities': list(grok), 'max_file_count': 10}]}


def test_disabled_before_catalog_or_key(monkeypatch):
    cfg = config()
    cfg['multiple_description_search']['enabled'] = False
    monkeypatch.setattr(runtime, 'provider_target', lambda *a, **k: pytest.fail('key access'))
    with pytest.raises(pipeline.ParseDisabled):
        runtime.prepare_runtime(cfg, selected_provider='hydra', selected_model='glm-5.2',
                                catalog_fetch=lambda: pytest.fail('network'))


def test_confirmed_no_vision_selects_hydra_grok_with_its_own_limit():
    target, router_key = runtime.prepare_runtime(config(), selected_provider='hydra',
                                                  selected_model='glm-5.2', catalog_fetch=catalog)
    assert (target.provider, target.model, target.image_limit, target.capability) == ('hydra', 'grok-4.7', 10, 'yes')
    assert target.api_key == 'HYDRA_SECRET' and router_key == 'ROUTER_SECRET'
    assert 'HYDRA_SECRET' not in repr(target)


def test_catalog_failure_or_unclear_model_tries_selected_not_reserve():
    def unavailable():
        raise TimeoutError('unknown does not mean text only')
    target, key = runtime.prepare_runtime(config(), selected_provider='hydra', selected_model='glm-5.2',
                                           catalog_fetch=unavailable)
    assert (target.model, target.capability, target.image_limit) == ('glm-5.2', 'unknown', 50)
    assert key == 'ROUTER_SECRET'
    assert runtime.model_capability({'data': [{'id': 'x'}]}, 'x') == ('unknown', None)
    assert runtime.model_capability({'data': [{'id': 'x', 'input_modalities': []}]}, 'x') == ('unknown', None)


def test_model_limit_and_confirmed_nonvision_reserve():
    cfg = config()
    value = {'data': [{'id': 'glm-5.2', 'input_modalities': ['image'], 'max_file_count': 4}]}
    target, _ = runtime.prepare_runtime(cfg, selected_provider='hydra', selected_model='glm-5.2',
                                        catalog_fetch=lambda: value)
    assert target.model == 'glm-5.2' and target.image_limit == 4
    with pytest.raises(runtime.RuntimeUnavailable, match='vision_reserve_unavailable'):
        runtime.prepare_runtime(cfg, selected_provider='hydra', selected_model='glm-5.2',
                                catalog_fetch=lambda: catalog(grok=('text',)))


def test_no_wrong_provider_key_fallback(monkeypatch):
    cfg = config()
    cfg['api']['providers']['routerai']['api_key'] = ''
    monkeypatch.delenv('ROUTERAI_API_KEY', raising=False)
    monkeypatch.setattr(lp, 'load_dotenv_dict', lambda: {})
    with pytest.raises(runtime.RuntimeUnavailable, match='provider_key_unavailable'):
        runtime.prepare_runtime(cfg, selected_provider='hydra', selected_model='glm-5.2',
                                catalog_fetch=lambda: pytest.fail('no key, no catalog'))


def test_an_active_router_key_never_becomes_hydra_fallback_key(monkeypatch):
    cfg = config()
    cfg['api']['provider'] = 'routerai'
    cfg['api']['api_key'] = 'ROUTER_ONLY'
    cfg['api']['providers']['hydra']['api_key'] = ''
    monkeypatch.delenv('HYDRA_API_KEY', raising=False)
    monkeypatch.setattr(lp, 'load_dotenv_dict', lambda: {})
    with pytest.raises(runtime.RuntimeUnavailable, match='provider_key_unavailable'):
        runtime.provider_target(cfg, 'hydra', 'grok-4.7')


def test_non_https_provider_endpoint_fails_before_key_use():
    cfg = config()
    cfg['api']['providers']['hydra']['base_url'] = 'http://localhost:1234'
    with pytest.raises(runtime.RuntimeUnavailable, match='invalid_provider_endpoint'):
        runtime.provider_target(cfg, 'hydra', 'grok-4.7')


def searcher(base, key, model, state, assets, qs):
    return response(qs, {1: .9, 2: .8}, 'two')


def described(lot, distance, *, tie=True):
    result = item(lot)
    result['data'].update({'main_street': 'Main Street', 'cross_street': 'Cross Street',
                           'borough_or_city': 'Staten Island', 'distance_to_corner_ft': distance})
    if tie:
        result['data']['corner_to_pob_bearing'] = dict(ns='N', degrees=90, minutes=0, seconds=0,
                                                       ew='E', distance_ft=distance)
    return result


def test_batch_multiple_files_all_descriptions_roles_and_sources(tmp_path):
    our, neighbour = tmp_path / 'OUR 6 NEW.pdf', tmp_path / 'LOT 5 NEW.pdf'
    make_pdf(our, 2)
    make_pdf(neighbour, 2)
    calls = []
    def send(images, context):
        calls.append(dict(context))
        return answer([described('5', 55.42), described('6', 100.11)])
    target = runtime.ResolvedTarget('hydra', 'grok-4.7', 'https://example.test', 'secret', 10, 'yes')
    jobs = [runtime.FileJob(our, True), runtime.FileJob(neighbour, False)]
    result = runtime.run_batch(jobs, config(), target=target, search_api_key='router-key',
                               send=send, choose=None, search_send=searcher)
    assert [f.status for f in result.files] == ['parsed', 'parsed']
    assert len(result.scene.plans) == 4 and not result.scene.failures
    assert [p.plan.is_neighbor for p in result.scene.plans] == [False, False, True, True]
    assert [next(t.text for t in p.plan.items if 'lot-label' in t.kind or t.kind == 'neighbor-label')
            for p in result.scene.plans] == ['LOT 6', 'LOT 6', 'LOT 5', 'LOT 5']
    assert all(p.placement_known for p in result.scene.plans)
    assert result.scene.plans[0].group == result.scene.plans[1].group
    assert result.scene.plans[0].group != result.scene.plans[2].group
    assert result.scene.plans[1].plan.pob[0] - result.scene.plans[0].plan.pob[0] == pytest.approx(44.69)
    assert len(calls) == 2 and calls[0]['filename_lot'] == '6' and calls[1]['filename_lot'] == '5'
    passport = json.loads(our.with_suffix('.json').read_text())
    lots = passport['identity']['lot']
    assert {(x['source'], x['value']) for x in lots} >= {('filename', '6'), ('ai', '5'), ('ai', '6')}
    assert 'secret' not in our.with_suffix('.json').read_text()


def test_no_explicit_tie_never_treats_legacy_inference_as_location(tmp_path):
    pdf = tmp_path / 'OUR 6 NEW.pdf'
    make_pdf(pdf, 2)
    target = runtime.ResolvedTarget('hydra', 'grok', 'https://example.test', 'secret', 10)
    result = runtime.run_batch([runtime.FileJob(pdf, True)], config(), target=target,
                               search_api_key='router', send=lambda *a: answer([described('5', 55, tie=False)]),
                               choose=None, search_send=searcher)
    assert len(result.scene.plans) == 1
    assert not result.scene.plans[0].placement_known
    assert result.scene.plans[0].diagnostic == 'unresolved_position'


def test_failed_file_does_not_hide_other_files_and_stores_safe_failure(tmp_path):
    bad, good = tmp_path / 'missing.pdf', tmp_path / 'OUR 6.pdf'
    make_pdf(good, 2)
    target = runtime.ResolvedTarget('hydra', 'grok', 'https://example.test', 'secret', 10)
    result = runtime.run_batch([runtime.FileJob(bad, True), runtime.FileJob(good, True)], config(),
                               target=target, search_api_key='router',
                               send=lambda *a: answer([described('6', 55)]), choose=None, search_send=searcher)
    assert [f.status for f in result.files] == ['processing_failed', 'parsed']
    assert len(result.scene.plans) == 1


def test_cancellation_does_not_publish_partial_scene(tmp_path):
    pdf = tmp_path / 'OUR 6.pdf'
    make_pdf(pdf, 2)
    target = runtime.ResolvedTarget('hydra', 'grok', 'https://example.test', 'secret', 10)
    checks = iter([True, False])
    result = runtime.run_batch([runtime.FileJob(pdf, True)], config(), target=target, search_api_key='router',
                               send=lambda *a: answer([described('6', 55)]), choose=None,
                               search_send=searcher, current=lambda: next(checks))
    assert result.cancelled and not result.scene.plans


def test_portal_context_only_from_unambiguous_portal_identity(tmp_path):
    pdf = tmp_path / 'OUR 6.pdf'
    make_pdf(pdf, 1)
    passport = pdf.with_suffix('.json')
    passport.write_text(json.dumps({'file': pdf.name, 'identity': {'lot': [
        {'value': '6', 'source': 'filename'}, {'value': '7', 'source': 'portal'}]}}))
    assert runtime.portal_lot_from_passport(pdf) == '7'
    data = json.loads(passport.read_text())
    data['identity']['lot'].append({'value': '8', 'source': 'portal'})
    passport.write_text(json.dumps(data))
    assert runtime.portal_lot_from_passport(pdf) is None


def test_multi_result_is_never_reused_as_legacy_single_lot(tmp_path):
    import pdf_set
    pdf = tmp_path / 'OUR 6.pdf'
    make_pdf(pdf, 1)
    item_ = pdf_set.PdfSetItem(path=pdf, page_texts=['x' * 30], outcome='first', multi_outcomes=['first', 'second'])
    item_.parsed_key = item_.parse_key()
    assert item_.has_fresh_result()
    assert pdf_set.make_job(item_).cached_outcome is None
    item_.forget_result()
    assert item_.multi_outcomes == [] and item_.outcome is None


def test_cancel_during_search_does_not_retry_or_parse(tmp_path):
    pdf = tmp_path / 'OUR 6.pdf'
    make_pdf(pdf, 10)
    target = runtime.ResolvedTarget('hydra', 'grok', 'https://example.test', 'secret', 10)
    state = {'active': True, 'search': 0, 'parse': 0}
    def search(*args):
        state['search'] += 1
        state['active'] = False
        raise TimeoutError('already in flight')
    def send(*args):
        state['parse'] += 1
        return answer([item('5')])
    result = runtime.run_batch([runtime.FileJob(pdf, True)], config(), target=target, search_api_key='router',
                               send=send, choose=None, search_send=search, current=lambda: state['active'])
    assert result.cancelled and not result.scene.plans
    assert state['search'] == 1 and state['parse'] == 0
    assert not (tmp_path / (pdf.name + '.multi-parse.json')).exists()


def test_cancel_during_parse_does_not_retry_or_cache_failure(tmp_path):
    pdf = tmp_path / 'OUR 6.pdf'
    make_pdf(pdf, 2)
    target = runtime.ResolvedTarget('hydra', 'grok', 'https://example.test', 'secret', 10)
    state = {'active': True, 'parse': 0}
    def send(*args):
        state['parse'] += 1
        state['active'] = False
        raise TimeoutError('already in flight')
    result = runtime.run_batch([runtime.FileJob(pdf, True)], config(), target=target, search_api_key='router',
                               send=send, choose=None, search_send=searcher, current=lambda: state['active'])
    assert result.cancelled and not result.scene.plans and state['parse'] == 1
    sidecar = tmp_path / (pdf.name + '.multi-parse.json')
    assert not sidecar.exists()


def test_unlocated_planning_reason_recorded_without_pdf_text_or_key(tmp_path):
    pdf = tmp_path / 'OUR 6.pdf'
    make_pdf(pdf, 2)
    target = runtime.ResolvedTarget('hydra', 'grok', 'https://example.test', 'private-key', 10)
    result = runtime.run_batch([runtime.FileJob(pdf, True)], config(), target=target, search_api_key='router',
                               send=lambda *a: answer([described('5', 55, tie=False)]), choose=None,
                               search_send=searcher)
    assert result.scene.plans[0].diagnostic == 'unresolved_position'
    data = json.loads((tmp_path / (pdf.name + '.multi-parse.json')).read_text())
    assert data['scene_history'][-1]['descriptions'] == [
        {'ordinal': 1, 'placement_known': False, 'reason': 'unresolved_position'}]
    assert 'private-key' not in json.dumps(data['scene_history'])
    assert 'Main Street' not in json.dumps(data['scene_history'])


def test_hydra_grok_runtime_uses_routerai_grok_as_hedge(monkeypatch):
    from types import SimpleNamespace
    from multiple_description_parse import PageImage
    clients = []
    calls = []

    class HedgeClient:
        def __init__(self, key):
            self.key = key
            self.closed = False
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def create(self, *, model, **kwargs):
            calls.append((model, self.key))
            if self.key == 'hydra-key':
                import time
                time.sleep(0.05)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content='{"selected": 0}', refusal=None))])

        def close(self):
            self.closed = True

    def fake_client(target, *, timeout):
        client = HedgeClient(target['api_key'])
        clients.append(client)
        return client

    monkeypatch.setattr(runtime.transport, 'create_client', fake_client)
    target = runtime.ResolvedTarget('hydra', runtime.HYDRA_GROK_MODEL,
                                    'https://hydra.test/v1', 'hydra-key', 10)
    hedge = runtime.ResolvedTarget('routerai', runtime.ROUTERAI_GROK_MODEL,
                                   'https://routerai.test/v1', 'router-key', 10)
    with runtime.open_runtime_callbacks(target, {}, hedge_target=hedge, hedge_delay_sec=0.0) as (send, _choose):
        send([PageImage(1, 'data:image/png;base64,AA==')], {'filename_lot': '6'})
    assert [call[0] for call in calls] == [runtime.HYDRA_GROK_MODEL, runtime.ROUTERAI_GROK_MODEL]
    assert [call[1] for call in calls] == ['hydra-key', 'router-key']
    assert all(client.closed for client in clients)


def test_other_model_runtime_has_no_hedge(monkeypatch):
    from types import SimpleNamespace
    from multiple_description_parse import PageImage
    calls = []

    class PlainClient:
        def __init__(self, key):
            self.key = key
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def create(self, *, model, **kwargs):
            calls.append((model, self.key))
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content='{}', refusal=None))])

        def close(self):
            pass

    monkeypatch.setattr(runtime.transport, 'create_client',
                        lambda target, *, timeout: PlainClient(target['api_key']))
    target = runtime.ResolvedTarget('hydra', 'gpt-4.1', 'https://hydra.test/v1', 'hydra-key', 10)
    hedge = runtime.ResolvedTarget('routerai', runtime.ROUTERAI_GROK_MODEL,
                                   'https://routerai.test/v1', 'router-key', 10)
    with runtime.open_runtime_callbacks(target, {}, hedge_target=hedge, hedge_delay_sec=0.0) as (send, _choose):
        send([PageImage(1, 'data:image/png;base64,AA==')], {})
    assert calls == [('gpt-4.1', 'hydra-key')]


def test_streaming_batch_processes_each_yielded_file_and_finishes(tmp_path):
    pdf = tmp_path / 'OUR 6 NEW.pdf'
    make_pdf(pdf, 2)
    target = runtime.ResolvedTarget('hydra', 'grok-4.7', 'https://example.test', 'secret', 10)
    seen = []

    def jobs():
        seen.append('yielded')
        yield runtime.FileJob(pdf, True)
        seen.append('ended')

    result = runtime.run_batch_streaming(
        jobs(), config(), target=target, search_api_key='router',
        send=lambda *a: answer([described('6', 55)]), choose=None, search_send=searcher)
    assert seen == ['yielded', 'ended']
    assert [f.status for f in result.files] == ['parsed']
    assert len(result.scene.plans) == 1


def test_runtime_client_is_closed_on_normal_and_exceptional_exit(monkeypatch):
    class Client:
        closed = False
        def close(self):
            self.closed = True
    clients = []
    def create(*a, **kw):
        client = Client()
        clients.append(client)
        return client
    monkeypatch.setattr(runtime.transport, 'create_client', create)
    monkeypatch.setattr(runtime.transport, 'make_sender', lambda *a: 'sender')
    monkeypatch.setattr(runtime.transport, 'make_selector', lambda *a: 'selector')
    target = runtime.ResolvedTarget('hydra', 'grok', 'https://example.test', 'test', 10)
    with runtime.open_runtime_callbacks(target, config()) as callbacks:
        assert callbacks == ('sender', 'selector')
    assert clients[-1].closed
    with pytest.raises(RuntimeError):
        with runtime.open_runtime_callbacks(target, config()):
            raise RuntimeError('cancelled')
    assert clients[-1].closed


def test_cancel_during_catalog_lookup_stops_before_resolving_model_target(monkeypatch):
    cfg = config()
    state = {'active': True}
    original = runtime.provider_target
    targets = []
    def resolve(*args, **kwargs):
        targets.append(args[1])
        return original(*args, **kwargs)
    monkeypatch.setattr(runtime, 'provider_target', resolve)
    def catalog_fetch():
        state['active'] = False
        return catalog()
    with pytest.raises(runtime.Cancelled):
        runtime.prepare_runtime(cfg, selected_provider='hydra', selected_model='glm-5.2',
                                catalog_fetch=catalog_fetch, current=lambda: state['active'])
    assert targets == ['routerai']


def test_unproven_corner_and_nominal_ties_are_not_anchors(tmp_path):
    for raw in [described('corner', 0), described('nominal', 55), described('solved', 55)]:
        if raw['data']['distance_to_corner_ft'] == 0:
            raw['data']['cross_street_side_index'] = 1
        elif raw['data']['lot_number'] == 'nominal':
            raw['data']['corner_to_pob_bearing']['has_bearing'] = False
        else:
            raw['data']['corner_to_pob_bearing']['bearing_solved'] = True
        parsed = md.read_response(answer([raw]), [1])
        assert len(parsed.items) == 1
        assert runtime.anchor_for(tmp_path / 'our.pdf', parsed.items[0]) is None


def test_changed_pdf_after_parse_never_assigns_old_ai_observations_to_new_file(tmp_path, monkeypatch):
    pdf = tmp_path / 'OUR 6.pdf'
    make_pdf(pdf, 2)
    original = pipeline.parse_pdf
    def parse_then_replace(*args, **kwargs):
        result = original(*args, **kwargs)
        pdf.write_bytes(b'replaced PDF')
        return result
    monkeypatch.setattr(pipeline, 'parse_pdf', parse_then_replace)
    target = runtime.ResolvedTarget('hydra', 'grok', 'https://example.test', 'test', 10)
    batch = runtime.run_batch([runtime.FileJob(pdf, True)], config(), target=target,
                              search_api_key='key', search_send=searcher, choose=None,
                              send=lambda *a: answer([described('5', 55)]))
    assert len(batch.scene.plans) == 1
    assert not pdf.with_suffix('.json').exists()
    assert batch.files[0].result.parse.items[0].data._source_file_sha256 == ''
    sidecar = json.loads((tmp_path / (pdf.name + '.multi-parse.json')).read_text())
    assert all(run.get('error') != 'metadata_unavailable' for run in sidecar['runs'])


def test_unlocated_placement_is_reported_to_user(tmp_path):
    pdf = tmp_path / 'OUR 6 NEW.pdf'
    make_pdf(pdf, 2)
    messages = []
    target = runtime.ResolvedTarget('hydra', 'grok', 'https://example.test', 'secret', 10)
    result = runtime.run_batch([runtime.FileJob(pdf, True)], config(), target=target,
                               search_api_key='router', send=lambda *a: answer([described('5', 55, tie=False)]),
                               choose=None, search_send=searcher, detail=messages.append)
    assert not result.scene.plans[0].placement_known
    assert any('без явной привязки' in message and 'как в обычном режиме' in message
               for message in messages)


def test_two_pdf_cross_borough_safe_auto_row_uses_no_additional_model_calls(tmp_path):
    from test_multiple_description_drawing import deed_example_items
    first, second = tmp_path / 'OUR 6.pdf', tmp_path / 'LOT 5.pdf'
    make_pdf(first, 2)
    make_pdf(second, 2)
    values = deed_example_items()
    raw = []
    for value in values:
        data = value.data.model_copy(deep=True, update={'borough_or_city': 'Staten Island, NY'})
        raw.append({'complete': True, 'data': data.model_dump(mode='json')})
    calls = []
    def send(images, context):
        calls.append(context['filename_lot'])
        return answer([raw[0] if context['filename_lot'] == '6' else raw[1]])
    target = runtime.ResolvedTarget('hydra', 'grok', 'https://example.test', 'secret', 10)
    jobs = [runtime.FileJob(first, True), runtime.FileJob(second, False)]
    batch = runtime.run_batch(jobs, config(), target=target, search_api_key='router', send=send,
                              choose=None, search_send=searcher)
    assert calls == ['6', '5']
    assert len(batch.scene.plans) == 2 and all(p.placement_known for p in batch.scene.plans)
    assert batch.scene.plans[0].group == batch.scene.plans[1].group
    assert [p.plan.is_neighbor for p in batch.scene.plans] == [False, True]
    assert batch.files[1].result.parse.items[0].data.cross_street == 'Lindbergh Avenue'
