"""Offline tests of the disabled-by-default all-description page search."""
import json
from pathlib import Path

import httpx
import pymupdf
import pytest

import developer_probability as dp
import file_metadata
import legal_page_search as search
import llm_parser

ON = {"multiple_description_search": {"enabled": True}}


def make_pdf(path, count):
    with pymupdf.open() as doc:
        for n in range(count):
            page = doc.new_page(width=320, height=440)
            page.insert_text((20, 30), f'Page {n + 1}')
        doc.save(path)


def response(qs, probabilities=None, count='one'):
    probabilities = probabilities or {}
    return {'answers': {k: ({'type': 'choice', 'choice': count} if k == 'count' else
                            {'type': 'noul', 'noul': probabilities.get(int(k[5:]), 0.1)})
                        for k in qs}}


def test_default_disabled_and_no_side_effects(tmp_path, monkeypatch):
    assert llm_parser.DEFAULT_CONFIG['multiple_description_search']['enabled'] is False
    assert json.loads(Path('config.json').read_text())['multiple_description_search']['enabled'] is False
    assert not search.enabled({'multiple_description_search': {'enabled': 'true'}})
    monkeypatch.setattr(dp, 'prepare_documents', lambda *a, **k: pytest.fail('render'))
    monkeypatch.setattr(dp, 'post_routerai', lambda *a: pytest.fail('network'))
    with pytest.raises(search.SearchDisabled):
        search.search_pdf(tmp_path / 'missing.pdf', {}, api_key='')
    assert list(tmp_path.iterdir()) == []


def test_primary_overlap_max_count_diagnostics_and_cache(tmp_path):
    pdf = tmp_path / 'OUR 6 NEW.pdf'
    make_pdf(pdf, 15)
    calls = []
    def send(base, key, model, state, assets, qs):
        calls.append((model, state, assets, qs))
        assert 'OUR 6' not in str(state)
        assert len(assets) == 8
        if len(calls) == 1:
            return response(qs, {7: .8, 8: .1}, 'two')
        return response(qs, {8: .8, 9: .9, 10: .3}, 'one')
    run = search.search_pdf(pdf, ON, api_key='private-key', send=send)
    assert len(calls) == 2 and all(c[0] == search.PRIMARY for c in calls)
    assert run['candidate_pages'] == [7, 8, 9, 10]
    assert run['pages'][7]['max_probability'] == .8
    assert run['pages'][7]['incomplete'] is False
    assert run['coverage_complete'] and run['count_scope'] == 'batch_only'
    assert [b['count']['value'] for b in run['batches']] == ['two', 'one']
    sidecar = search.diagnostics_path(pdf)
    saved = sidecar.read_text(encoding='utf8')
    assert 'private-key' not in saved and 'data_base64' not in saved
    assert json.loads(saved)['runs'][0]['pdf_sha256'] == file_metadata._digest(pdf)
    # Search sidecar is not a passport or accidental match when a real passport is added.
    passport, _ = file_metadata._passport(pdf)
    assert passport == pdf.with_suffix('.json')
    again = search.search_pdf(pdf, ON, api_key='private-key', send=lambda *a: pytest.fail('cache miss'))
    assert again['candidate_pages'] == run['candidate_pages']
    assert all(a['source'] == 'cache' for a in again['attempts'])
    assert len(json.loads(sidecar.read_text())['runs']) == 2


def test_retries_failed_pages_only_then_fallback_and_no_count_retry(tmp_path):
    pdf = tmp_path / 'test.pdf'
    make_pdf(pdf, 3)
    calls = []
    def send(base, key, model, state, assets, qs):
        calls.append((model, list(qs), len(assets)))
        if len(calls) == 1:
            data = response(qs, {1: .5, 2: .2})
            del data['answers']['count']
            del data['answers']['page_3']
            return data
        if model == search.PRIMARY:
            raise httpx.TimeoutException('do not log key')
        return response(qs, {3: .8})
    run = search.search_pdf(pdf, ON, api_key='secret', send=send)
    assert calls == [(search.PRIMARY, ['page_1', 'page_2', 'page_3', 'count'], 3),
                     (search.PRIMARY, ['page_3'], 1), (search.PRIMARY, ['page_3'], 1),
                     (search.FALLBACK, ['page_3'], 1)]
    assert run['candidate_pages'] == [1, 3]
    assert run['batches'][0]['count']['status'] == 'unknown'
    assert run['pages'][2]['observations'][0]['model'] == search.FALLBACK
    assert 'secret' not in json.dumps(run) and 'do not log' not in json.dumps(run)


def test_all_failures_unknown_included_and_retry_limit(tmp_path):
    pdf = tmp_path / 'failure.pdf'
    make_pdf(pdf, 2)
    calls = []
    def fail(base, key, model, state, assets, qs):
        calls.append((model, len(assets), list(qs)))
        raise httpx.TimeoutException('secret')
    run = search.search_pdf(pdf, ON, api_key='secret', send=fail)
    assert [x[0] for x in calls] == [search.PRIMARY] * 3 + [search.FALLBACK] * 3
    assert len(calls[0][2]) == 3 and all('count' not in c[2] for c in calls[1:])
    assert run['candidate_pages'] == [1, 2] and not run['coverage_complete']
    assert all(p['incomplete'] and p['max_probability'] is None for p in run['pages'])
    assert 'secret' not in json.dumps(run)


def test_overlap_failure_in_one_batch_is_included_despite_low_other_score(tmp_path):
    pdf = tmp_path / 'overlap.pdf'
    make_pdf(pdf, 9)
    def send(base, key, model, state, assets, qs):
        if 'page_1' in qs:
            return response(qs, {8: .05})
        raise httpx.TimeoutException('fail')
    run = search.search_pdf(pdf, ON, api_key='key', send=send)
    assert run['pages'][7]['incomplete'] and run['pages'][7]['max_probability'] == .05
    assert run['candidate_pages'] == [8, 9]
    assert not run['coverage_complete']
    assert len(run['pages'][7]['observations']) == 2


def test_render_failure_does_not_block_other_pages(tmp_path, monkeypatch):
    pdf = tmp_path / 'corrupt-page.pdf'
    make_pdf(pdf, 3)
    original = dp.prepare_documents
    def render(files, *args, **kwargs):
        if files[0][1] == '2':
            raise ValueError('private')
        return original(files, *args, **kwargs)
    monkeypatch.setattr(dp, 'prepare_documents', render)
    calls = []
    def send(base, key, model, state, assets, qs):
        calls.append(list(qs))
        return response(qs, {3: .9})
    run = search.search_pdf(pdf, ON, api_key='key', send=send)
    assert calls == [['page_1', 'page_3']]
    assert run['render_errors'] == [2] and not run['pages'][1]['image_available']
    assert run['pages'][1]['incomplete'] and run['candidate_pages'] == [3]
    assert run['batches'][0]['count']['status'] == 'unknown'


def test_strict_fingerprint_and_jev_cache_matching(tmp_path):
    pdf = tmp_path / 'test.pdf'
    make_pdf(pdf, 2)
    pages = dp.prepare_documents([(str(pdf), '1-2')], 'visual', provider='routerai')
    pages = [{**p, 'file': 'document.pdf'} for p in pages]
    questions = search.questions_for([1, 2], count=True)
    fp = search._fingerprint(file_metadata._digest(pdf), pages, questions, search.FALLBACK,
                             dp.PDF_IMAGE_MAX_SIDE, search.BASE_URL)
    cached = {'schema': search.SCHEMA, 'runs': [{'attempts': [{'fingerprint': fp, 'model': search.FALLBACK,
                        'answers': {'page_1': {'status': 'ok', 'value': .7},
                                    'page_2': {'status': 'ok', 'value': .1},
                                    'count': {'status': 'ok', 'value': 'one'}}}]}]}
    search.diagnostics_path(pdf).write_text(json.dumps(cached), encoding='utf8')
    run = search.search_pdf(pdf, ON, api_key='key', send=lambda *a: pytest.fail('should reuse Jev'))
    assert run['candidate_pages'] == [1]
    assert run['pages'][0]['observations'][0]['model'] == search.FALLBACK
    assert run['attempts'][0]['source'] == 'cache'
    # Changing render settings or exact question invalidates the fingerprint.
    assert fp != search._fingerprint(file_metadata._digest(pdf), pages, questions, search.FALLBACK, 1200, search.BASE_URL)
    altered = search.questions_for([1, 2], count=True)
    altered['page_1']['instructions'] += ' revised'
    assert fp != search._fingerprint(file_metadata._digest(pdf), pages, altered, search.FALLBACK, 1568, search.BASE_URL)
    assert fp != search._fingerprint('0'*64, pages, questions, search.FALLBACK, 1568, search.BASE_URL)
    assert fp != search._fingerprint(file_metadata._digest(pdf), pages, questions, search.PRIMARY, 1568, search.BASE_URL)


def test_sidecar_collision_rejected_before_network(tmp_path):
    pdf = tmp_path / 'deed.pdf'
    make_pdf(pdf, 1)
    path = search.diagnostics_path(pdf)
    path.write_text('{"user": "data"}')
    with pytest.raises(search.SearchError):
        search.search_pdf(pdf, ON, api_key='key', send=lambda *a: pytest.fail('network'))
    assert path.read_text() == '{"user": "data"}'


def test_cap_200_with_stubbed_render(tmp_path, monkeypatch):
    pdf = tmp_path / 'big.pdf'
    make_pdf(pdf, 201)
    monkeypatch.setattr(dp, 'prepare_documents', lambda files, *a, **kw:
                        [{'page': int(files[0][1]), 'file': 'source.pdf', 'content': b'png'}])
    calls = []
    def send(base, key, model, state, assets, qs):
        calls.append(1)
        return response(qs)
    run = search.search_pdf(pdf, ON, api_key='key', send=send)
    assert run['covered_pages'] == 200 and run['uncovered_pages'] == 1
    assert run['count_scope'] == 'batch_only' and not run['coverage_complete']
    assert run['candidate_pages'] == [] and len(calls) == len(search.batch_numbers(200))


def test_count_only_failure_never_causes_retry(tmp_path):
    pdf = tmp_path / 'no-count.pdf'
    make_pdf(pdf, 2)
    calls = []
    def send(base, key, model, state, assets, qs):
        calls.append(1)
        data = response(qs, {1: .9})
        del data['answers']['count']
        data['usage'] = {'input_tokens': 200, 'cost': 0.02, 'secret': 'secret', 'output_tokens': -5}
        return data
    run = search.search_pdf(pdf, ON, api_key='key', send=send)
    assert len(calls) == 1 and run['candidate_pages'] == [1]
    assert run['batches'][0]['count'] == {'status': 'unknown', 'value': None}
    assert run['attempts'][0]['usage'] == {'input_tokens': 200, 'cost': 0.02}


def test_bad_answers_retry_only_those_and_fallback_twice(tmp_path):
    pdf = tmp_path / 'partial.pdf'
    make_pdf(pdf, 2)
    calls = []
    def send(base, key, model, state, assets, qs):
        calls.append((model, list(qs)))
        if model == search.FALLBACK and sum(c[0] == search.FALLBACK for c in calls) == 3:
            return response(qs, {2: .3})
        data = response(qs, {1: .9})
        data['answers']['page_2'] = {'type': 'noul', 'noul': float('nan')}
        return data
    run = search.search_pdf(pdf, ON, api_key='key', send=send)
    assert len(calls) == 6
    assert all(keys == ['page_2'] for _, keys in calls[1:])
    assert run['candidate_pages'] == [1, 2] and run['coverage_complete']


def test_changed_pdf_cannot_reuse_and_history_remains(tmp_path):
    pdf = tmp_path / 'mutable.pdf'
    make_pdf(pdf, 1)
    calls = []
    def send(base, key, model, state, assets, qs):
        calls.append(1)
        return response(qs, {1: .7})
    one = search.search_pdf(pdf, ON, api_key='key', send=send)
    # Save a genuinely different PDF, preserving the same filename and page count.
    with pymupdf.open(pdf) as doc:
        doc[0].insert_text((30, 50), 'change')
        replacement = doc.tobytes()
    pdf.write_bytes(replacement)
    two = search.search_pdf(pdf, ON, api_key='key', send=send)
    assert len(calls) == 2 and one['pdf_sha256'] != two['pdf_sha256']
    assert len(json.loads(search.diagnostics_path(pdf).read_text())['runs']) == 2


def test_fingerprint_includes_question_order_and_page_context(tmp_path):
    pdf = tmp_path / 'order.pdf'
    make_pdf(pdf, 2)
    pages = dp.prepare_documents([(str(pdf), '1-2')], 'visual', provider='routerai')
    qs = search.questions_for([1, 2], count=True)
    digest = file_metadata._digest(pdf)
    def fp(p, q):
        return search._fingerprint(digest, p, q, search.PRIMARY, 1568, search.BASE_URL)
    assert fp(pages, qs) != fp(pages, dict(reversed(list(qs.items()))))
    assert fp(pages, qs) != fp(list(reversed(pages)), qs)
    assert fp(pages, qs) != fp(pages[:1], search.questions_for([1], count=True))


def test_existing_passport_not_modified(tmp_path):
    pdf = tmp_path / 'a.pdf'
    make_pdf(pdf, 1)
    passport = pdf.with_suffix('.json')
    passport.write_text('{"file":"a.pdf","identity":{"lot":[]}}', encoding='utf8')
    before = passport.read_bytes()
    search.search_pdf(pdf, ON, api_key='key', send=lambda b,k,m,s,a,q: response(q))
    assert passport.read_bytes() == before
    assert file_metadata._passport(pdf)[0] == passport


def test_developer_tool_requires_live_and_switch_before_credentials(tmp_path, monkeypatch, capsys):
    from tools import legal_page_search_stage2 as cli
    pdf = tmp_path / 'test.pdf'
    make_pdf(pdf, 1)
    monkeypatch.setattr(cli.lp, 'load_config', lambda: {})
    monkeypatch.setattr(cli.common, 'load_provider_key', lambda *a: pytest.fail('key requested'))
    monkeypatch.setattr(cli.search, 'search_pdf', lambda *a, **k: pytest.fail('search executed'))
    assert cli.main([str(pdf)]) == 0
    assert json.loads(capsys.readouterr().out)['developer_switch_enabled'] is False
    assert cli.main([str(pdf), '--live']) == 2
    assert 'No requests sent' in capsys.readouterr().err


def test_developer_tool_live_invokes_search_only_when_enabled(tmp_path, monkeypatch, capsys):
    from tools import legal_page_search_stage2 as cli
    pdf = tmp_path / 'test.pdf'
    monkeypatch.setattr(cli.lp, 'load_config', lambda: ON)
    monkeypatch.setattr(cli.common, 'load_provider_key', lambda *a: 'secret')
    called = []
    def search_fake(path, config, *, api_key):
        called.append((path, config, api_key))
        return {'pdf_pages': 2, 'covered_pages': 2, 'uncovered_pages': 0,
                'coverage_complete': True, 'candidate_pages': [2]}
    monkeypatch.setattr(cli.search, 'search_pdf', search_fake)
    assert cli.main([str(pdf), '--live']) == 0
    assert len(called) == 1
    assert 'secret' not in capsys.readouterr().out


def test_cached_partial_keeps_context_and_asks_only_missing(tmp_path):
    pdf = tmp_path / 'partial-cache.pdf'
    make_pdf(pdf, 3)
    calls = []
    def first(base, key, model, state, assets, qs):
        calls.append(list(qs))
        if len(calls) == 1:
            data = response(qs, {1: .9, 2: .1})
            del data['answers']['page_3']
            return data
        raise httpx.TimeoutException('failed')
    one = search.search_pdf(pdf, ON, api_key='key', send=first)
    assert one['candidate_pages'] == [1, 3]
    calls.clear()
    def second(base, key, model, state, assets, qs):
        calls.append((model, list(qs), len(assets)))
        return response(qs, {3: .2})
    two = search.search_pdf(pdf, ON, api_key='key', send=second)
    assert calls == [(search.PRIMARY, ['page_3'], 1)]
    assert two['candidate_pages'] == [1]
    assert two['pages'][0]['observations'][0]['source'] == 'cache'
    assert two['batches'][0]['count']['source'] == 'cache'


def test_matching_cache_rejects_wrong_model_and_malformed_values():
    q = search.questions_for([1], count=True)
    history = {'runs': [{'attempts': [{'model': search.PRIMARY, 'fingerprint': 'fp',
                  'answers': {'page_1': {'status': 'ok', 'value': True},
                              'count': {'status': 'ok', 'value': 'secret'}}}]}]}
    assert search._cached(history, 'fp', q, search.FALLBACK) == {}
    assert search._cached(history, 'fp', q, search.PRIMARY) == {}


def test_document_mutated_during_render_never_sends_or_saves(tmp_path, monkeypatch):
    pdf = tmp_path / 'mutable.pdf'
    make_pdf(pdf, 1)
    original = dp.prepare_documents
    def render(*a, **kw):
        result = original(*a, **kw)
        with pdf.open('ab') as f:
            f.write(b'changed')
        return result
    monkeypatch.setattr(dp, 'prepare_documents', render)
    with pytest.raises(search.SearchError, match='changed while preparing'):
        search.search_pdf(pdf, ON, api_key='key', send=lambda *a: pytest.fail('sent'))
    assert not search.diagnostics_path(pdf).exists()


def test_real_transport_format_with_mocked_http_and_sanitized_errors(tmp_path, monkeypatch):
    pdf = tmp_path / 'realistic.pdf'
    make_pdf(pdf, 2)
    calls = []
    def post(url, **kwargs):
        body = json.loads(kwargs['content'])
        calls.append((url, body))
        request = httpx.Request('POST', url)
        if len(calls) <= 3:
            return httpx.Response(503, request=request, text='private server response')
        return httpx.Response(200, request=request,
                              json={**response(body['questions'], {1: .8, 2: .1}),
                                    'usage': {'input_tokens': 100}})
    monkeypatch.setattr(dp.httpx, 'post', post)
    result = search.search_pdf(pdf, ON, api_key='secret')
    assert len(calls) == 4
    assert [call[1]['model'] for call in calls] == [search.PRIMARY] * 3 + [search.FALLBACK]
    for _, body in calls:
        assert len([p for p in body['state'] if p['type'] == 'image_url']) == 2
        assert all('realistic.pdf' not in str(p) for p in body['state'])
    assert result['candidate_pages'] == [1] and result['coverage_complete']
    assert result['attempts'][0]['error_kind'] == 'http_503'
    assert result['attempts'][-1]['usage'] == {'input_tokens': 100}
    recorded = search.diagnostics_path(pdf).read_text()
    assert 'private server response' not in recorded and 'secret' not in recorded
