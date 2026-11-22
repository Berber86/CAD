"""Full responses are explicitly user-visible; they are never persisted by us."""
from __future__ import annotations

import httpx
import pytest

import developer_probability as dp


URL = 'https://api.hydraai.ru/v1'
PAGE = [{'file': 'private.pdf', 'page': 1, 'content': 'document'}]
QUESTION = [{'type': 'noul', 'text': 'Does it contain data?'}]


def fake_response(status, body, headers=()):
    request = httpx.Request('POST', URL + '/systemone')
    return httpx.Response(status, content=body, request=request,
                          headers=[('Content-Type', 'application/json; charset=utf-8'), *headers])


def test_http_error_exposes_complete_unchanged_body_and_all_headers(monkeypatch):
    body = '{\n "error": "detail", "input": "PRIVATE_DOCUMENT", "image": "IMAGE_BASE64", "token": "MOCK_KEY"\n}'
    calls = []
    def post(url, **kwargs):
        calls.append(url)
        return fake_response(422, body.encode(), [('X-Trace', 'private-trace'), ('X-Trace', 'second')])
    monkeypatch.setattr(dp.httpx, 'post', post)
    result = dp.run_model('jev-1.14', URL, 'test', [{'file': 'private.pdf', 'page': 1, 'content': b'PNG'}], 'visual', QUESTION)
    assert len(calls) == 1 and 'HTTP 422' in result['error']
    raw = result['provider_responses'][0]
    assert raw['status'] == 422 and raw['body'] == body and raw['encoding'] == 'текст'
    assert ('X-Trace', 'private-trace') in raw['headers']
    assert ('X-Trace', 'second') in raw['headers']
    assert result['usage'] == []


def test_success_exposes_full_original_response_and_keeps_normalized_answer(monkeypatch):
    body = '{\n  "answers": {"q1": {"type":"noul", "noul":0.75}},\n  "extra": "PRIVATE_TEXT", "usage":{"input_tokens":12}\n}'
    monkeypatch.setattr(dp.httpx, 'post', lambda *a, **kw: fake_response(200, body.encode(), [('X-Provider', 'test')]))
    result = dp.run_model('jev-1.14', URL, 'test', PAGE, 'ocr', QUESTION)
    assert result['entries'][0]['answer']['value'] == .75
    assert result['usage'] == [{'input_tokens': 12}]
    assert result['provider_responses'][0]['body'] == body
    assert ('X-Provider', 'test') in result['provider_responses'][0]['headers']


@pytest.mark.parametrize('body', [b'not json: PRIVATE_TEXT', b'{"error":"PRIVATE_TEXT"}'])
def test_success_status_with_invalid_response_is_also_exposed(monkeypatch, body):
    monkeypatch.setattr(dp.httpx, 'post', lambda *a, **kw: fake_response(200, body))
    result = dp.run_model('jev-1.14', URL, 'test', PAGE, 'ocr', QUESTION)
    assert 'error' in result and result['provider_responses'][0]['body'] == body.decode()
    assert result['provider_responses'][0]['status'] == 200


def test_binary_body_is_losslessly_encoded_instead_of_hidden(monkeypatch):
    body = b'\xff\x00\xfePRIVATE'
    monkeypatch.setattr(dp.httpx, 'post', lambda *a, **kw: fake_response(422, body))
    result = dp.run_model('jev-1.14', URL, 'test', PAGE, 'ocr', QUESTION)
    raw = result['provider_responses'][0]
    assert raw['encoding'] == 'hex (байты ответа)'
    assert bytes.fromhex(raw['body']) == body


def test_multiple_requests_preserve_success_then_error_in_order(monkeypatch):
    bodies = [b'{"answers":{"q1":{"type":"noul","noul":0.9}},"extra":"first"}', b'{"detail":"second failure"}']
    calls = []
    def post(url, **kwargs):
        index = len(calls)
        calls.append(index)
        return fake_response(200 if index == 0 else 422, bodies[index])
    monkeypatch.setattr(dp.httpx, 'post', post)
    result = dp.run_model('jev-1.14', URL, 'test', PAGE, 'ocr', QUESTION + [{'type': 'pages', 'text': 'Which page?'}])
    assert len(calls) == 2 and result['entries'][0]['answer']['value'] == .9
    assert [x['status'] for x in result['provider_responses']] == [200, 422]
    assert [x['body'] for x in result['provider_responses']] == [x.decode() for x in bodies]


def test_timeout_does_not_invent_a_provider_response(monkeypatch):
    def post(*a, **kw):
        raise httpx.TimeoutException('timeout')
    monkeypatch.setattr(dp.httpx, 'post', post)
    result = dp.run_model('jev-1.14', URL, 'test', PAGE, 'ocr', QUESTION)
    assert result['provider_responses'] == []
    assert 'ожидания' in result['error']


def test_two_models_keep_their_full_responses_separate(monkeypatch):
    import json
    def post(url, **kwargs):
        model = json.loads(kwargs['content'])['model']
        if model == 'jev-1.14':
            return fake_response(422, b'{"message":"jev private error"}')
        return fake_response(200, b'{"answers":{"q1":{"type":"noul","noul":0.8}},"extra":"solar private answer"}')
    monkeypatch.setattr(dp.httpx, 'post', post)
    result = dp.run_both(URL, 'test', PAGE, 'ocr', QUESTION)
    assert 'jev private error' in result[0]['provider_responses'][0]['body']
    assert 'solar private answer' in result[1]['provider_responses'][0]['body']
    assert result[0]['provider_responses'][0]['status'] == 422
    assert result[1]['provider_responses'][0]['status'] == 200
    assert result[1]['entries'][0]['answer']['value'] == .8


def test_large_provider_body_is_not_truncated(monkeypatch):
    image_data = 'IMAGE_DATA_' * 120000
    body = ('{"detail":"' + image_data + '"}').encode()
    monkeypatch.setattr(dp.httpx, 'post', lambda *a, **kw: fake_response(422, body))
    result = dp.run_model('jev-1.14', URL, 'test', PAGE, 'ocr', QUESTION)
    assert len(result['provider_responses'][0]['body']) == len(body)
    assert result['provider_responses'][0]['body'] == body.decode()
    assert result['provider_responses'][0]['http_version'].startswith('HTTP/')
    assert result['provider_responses'][0]['reason'] == 'Unprocessable Entity'


def test_deeply_nested_invalid_json_keeps_full_response(monkeypatch):
    body = (b'[' * 3000) + (b']' * 3000)
    monkeypatch.setattr(dp.httpx, 'post', lambda *a, **kw: fake_response(200, body))
    result = dp.run_model('jev-1.14', URL, 'test', PAGE, 'ocr', QUESTION)
    assert 'некорректный JSON' in result['error']
    assert result['provider_responses'][0]['body'] == body.decode()


def test_response_header_bytes_are_lossless():
    request = httpx.Request('POST', URL + '/systemone')
    resp = httpx.Response(422, request=request, content=b'failed', headers=[(b'X-\xff', b'\xfe')])
    raw = dp._capture_provider_response(resp)
    assert any(k.encode('latin-1') == b'X-\xff' and v.encode('latin-1') == b'\xfe' for k, v in raw['headers'])
