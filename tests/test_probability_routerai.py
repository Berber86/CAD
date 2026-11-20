import base64
import io
import json
import threading

import httpx
import pytest
from PIL import Image

import developer_probability as dp


def test_routerai_images_are_inline_and_text_mode_is_unchanged(monkeypatch):
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, request=httpx.Request('POST', url), json={'answers': {'q1': {'type': 'noul', 'noul': .5}}})
    monkeypatch.setattr(dp.httpx, 'post', post)
    pages = [{'file': 'sample.pdf', 'page': i, 'content': b'PNG' + bytes([i])} for i in (1, 2)]
    state, assets = dp.make_state(pages, 'visual')
    qs = {'q1': {'type': 'noul', 'instructions': 'Is there a boundary?'}}
    response = dp.post_routerai('https://routerai.ru/api/v1', 'router-key', dp.ROUTERAI_MODELS[0], state, assets, qs)
    url, kw = calls[0]
    body = json.loads(kw['content'])
    assert url == 'https://routerai.ru/api/v1/decisions'
    assert kw['headers']['Authorization'] == 'Bearer router-key'
    assert kw['follow_redirects'] is False
    assert 'assets' not in body and 'TypeSafe-Danger-Zone-Images' not in kw['headers']
    assert body['questions'] == qs and body['model'] == dp.ROUTERAI_MODELS[0]
    assert [x['type'] for x in body['state']] == ['text', 'image_url', 'text', 'image_url']
    assert body['state'][0]['text'] == 'sample.pdf — page 1'
    for item, page in zip(body['state'][1::2], pages):
        assert base64.b64decode(item['image_url']['url'].split(',', 1)[1]) == page['content']
    assert response['answers']['q1']['noul'] == .5 and response.http_response.status_code == 200
    dp.post_routerai('https://routerai.ru/api/v1', 'router-key', dp.ROUTERAI_MODELS[1], {'page_1': 'OCR'}, {}, qs)
    assert json.loads(calls[1][1]['content'])['state'] == {'page_1': 'OCR'}
    with pytest.raises(ValueError):
        dp.post_routerai('http://unsafe/v1', 'key', 'm', state, assets, qs)
    with pytest.raises(ValueError):
        dp.post_routerai('https://routerai.ru/api/v1', 'key', 'm', {}, assets, qs)


def test_routerai_two_models_same_input_parallel_and_independent():
    barrier = threading.Barrier(2)
    seen = []
    def send(url, key, model, state, assets, questions):
        seen.append((model, state, assets, questions))
        barrier.wait(timeout=5)
        if model == dp.ROUTERAI_MODELS[1]:
            raise httpx.HTTPStatusError('secret', request=httpx.Request('POST', 'https://routerai.ru'), response=httpx.Response(400))
        return {'answers': {'q1': {'type': 'noul', 'noul': .8}}}
    result = dp.run_both('https://routerai.ru/api/v1', 'key', [{'file': 'x.png', 'page': None, 'content': b'png'}], 'visual', [{'type': 'noul', 'text': 'q'}], send, provider='routerai')
    assert [r['model'] for r in result] == list(dp.ROUTERAI_MODELS)
    assert result[0]['entries'][0]['answer']['value'] == .8
    assert 'RouterAI' in result[1]['error'] and 'secret' not in result[1]['error']
    assert all(base64.b64decode(next(iter(item[2].values()))['data_base64']) == b'png' for item in seen)
    assert seen[0][3] == seen[1][3]


def test_routerai_image_constraints_small_image_is_padded_not_stretched(tmp_path):
    path = tmp_path / 'small.png'
    Image.new('RGB', (30, 20), 'black').save(path)
    page = dp.prepare_documents([(str(path), '')], 'visual', provider='routerai')[0]
    image = Image.open(io.BytesIO(page['content']))
    assert image.size == (280, 280)
    assert image.getpixel((0, 0)) == (255, 255, 255)
    assert image.getpixel((140, 140)) == (0, 0, 0)
    assert dp.prepare_documents([(str(path), '')], 'visual')[0]['content'] != page['content']


def test_routerai_visual_page_search_groups_and_reduces_100_pages():
    pages = [{'file': 'doc.pdf', 'page': i, 'content': str(i).encode()} for i in range(1, 101)]
    calls = []
    def send(url, key, model, state, assets, qs):
        calls.append((state, assets, qs))
        assert len(assets) <= 8
        if 'page' in qs:
            return {'answers': {'page': {'type': 'choice', 'choice': 'multiple' if len(assets) > 1 else 'p97'}}}
        numbers = sorted(int(k[1:]) for k in qs['first']['criteria'] if k != 'none')
        return {'answers': {'first': {'type': 'choice', 'choice': f'p{numbers[0]}'}, 'last': {'type': 'choice', 'choice': f'p{numbers[-1]}'}}}
    # Last group has four pages; each returns its first/last.
    result = dp.run_model('m', 'url', 'key', pages, 'visual', [{'type': 'pages', 'text': 'q'}], send, provider='routerai')
    assert 'error' not in result
    assert (result['entries'][0]['answer']['first'], result['entries'][0]['answer']['last']) == (1, 100)
    assert len(calls) > 26


def test_routerai_visual_normal_questions_over_limit_never_send():
    pages = [{'file': 'doc.pdf', 'page': i, 'content': b'image'} for i in range(1, 10)]
    def send(*args):
        pytest.fail('must reject before any network request')
    result = dp.run_model('m', 'url', 'key', pages, 'visual', [{'type': 'noul', 'text': 'q'}], send, provider='routerai')
    assert 'не более 8' in result['error'] and not result['entries']


def test_routerai_settings_and_migration(tmp_path):
    path = tmp_path / 'settings.json'
    path.write_text('{"mode":"visual","questions":[]}', encoding='utf-8')
    assert dp.load_settings(path)['provider'] == 'hydra'
    dp.save_settings({'provider': 'routerai', 'mode': 'visual', 'questions': [], 'api_key': 'secret'}, path)
    assert dp.load_settings(path)['provider'] == 'routerai'
    assert 'secret' not in path.read_text(encoding='utf-8')


def test_routerai_full_raw_http_error_is_retained_without_ocr(monkeypatch):
    calls = []
    def post(url, **kw):
        calls.append(json.loads(kw['content']))
        return httpx.Response(400, request=httpx.Request('POST', url),
                              headers={'x-test': 'test-header'}, json={'error': 'private provider body'})
    monkeypatch.setattr(dp.httpx, 'post', post)
    page = [{'file': 'doc.png', 'page': None, 'content': b'png'}]
    result = dp.run_model(dp.ROUTERAI_MODELS[0], 'https://routerai.ru/api/v1', 'key', page, 'visual',
                          [{'type': 'noul', 'text': 'q'}], dp.post_routerai, provider='routerai')
    assert len(calls) == 1 and 'image_url' in json.dumps(calls[0])
    assert 'HTTP 400' in result['error'] and 'OCR не подставлен' in result['error']
    assert result['provider_responses'][0]['status'] == 400
    assert 'private provider body' in result['provider_responses'][0]['body']
    assert ('x-test', 'test-header') in result['provider_responses'][0]['headers']
