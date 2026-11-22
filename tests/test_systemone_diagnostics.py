from __future__ import annotations

import json

import httpx
import pytest

import developer_probability as dp
import systemone_diagnostics as diag


SECRETS = ['sk-hydra-ai-SUPERSECRET', 'PRIVATE_DOCUMENT_TEXT', 'iVBORw0KGgoBASE64PRIVATE', 'personal-file.pdf']


def response(body=None, *, status=422, content=None):
    request = httpx.Request('POST', 'https://api.hydraai.ru/v1/systemone')
    if content is not None:
        return httpx.Response(status, content=content, request=request)
    return httpx.Response(status, json=body, request=request)


def assert_no_secrets(text):
    assert all(secret not in text for secret in SECRETS)


def test_fastapi_validation_loc_type_and_all_request_content_are_redacted():
    body = {'detail': [
        {'type': 'extra_forbidden', 'loc': ['body', 'assets', SECRETS[2], 'data_base64'],
         'msg': 'Extra inputs are not permitted ' + SECRETS[0], 'input': SECRETS[2], 'ctx': {'name': SECRETS[1]}},
        {'type': 'string_type', 'loc': ['body', 'state', 'page_1'], 'msg': SECRETS[1], 'input': SECRETS[3]},
    ], 'request': {'authorization': SECRETS[0]}}
    summary = diag.summarize_validation_error(response(body))
    assert 'body.assets.<элемент>.data_base64' in summary
    assert 'body.state.page_1' in summary
    assert 'лишнее или неподдерживаемое поле' in summary and 'ожидалась строка' in summary
    assert_no_secrets(summary)


def test_error_object_and_sanitized_param_path():
    body = {'error': {'message': 'Images are not supported: ' + SECRETS[1],
                      'param': 'body.assets.' + SECRETS[3] + '.format', 'code': 'invalid_request_error'}}
    text = diag.summarize_validation_error(response(body))
    assert 'body.assets.<элемент>.<элемент>.format' in text
    assert 'изображение не поддерживается' in text
    assert_no_secrets(text)


@pytest.mark.parametrize('body,expected', [
    ({'detail': [{'type': 'missing', 'loc': ['body', 'assets'], 'input': SECRETS[0]}]}, 'обязательное поле отсутствует'),
    ({'error': {'message': 'Image dimensions exceed maximum width'}}, 'размером изображения'),
    ({'error': {'message': 'Invalid image format for ' + SECRETS[3]}}, 'форматом или содержимым изображения'),
    ({'detail': 'TypeSafe-Danger-Zone-Images header is required'}, 'заголовка для экспериментальных изображений'),
    ({'error': {'code': 'image_not_supported', 'message': SECRETS[1]}}, 'изображение не поддерживается'),
    ({'detail': 'context length exceeds maximum'}, 'лимит контекста'),
])
def test_known_categories_only(body, expected):
    text = diag.summarize_validation_error(response(body))
    assert expected in text
    assert_no_secrets(text)


def test_unknown_content_never_echoes_even_if_it_looks_like_an_error_code():
    body = {'error': {'message': SECRETS[1], 'param': SECRETS[3], 'type': SECRETS[0], 'code': 'some_new_code'},
            'input': SECRETS[2], 'detail_unused': SECRETS}
    text = diag.summarize_validation_error(response(body))
    assert 'распознанных безопасных подробностей' in text
    assert_no_secrets(text)


def test_unknown_field_names_numbers_and_excessive_locations_are_not_exposed():
    body = {'detail': [{'type': 'missing', 'loc': ['body', 'questions', SECRETS[1], 15257, 'instructions']},
                       {'type': 'missing', 'loc': 'x' * 5000}]}
    text = diag.summarize_validation_error(response(body))
    assert 'body.questions.<элемент>.<элемент>.instructions' in text
    assert '15257' not in text
    assert_no_secrets(text)


def test_invalid_and_oversized_responses_and_detail_limits(monkeypatch):
    assert 'отсутствуют' in diag.summarize_validation_error(response(content=b'bad json'))
    with monkeypatch.context() as patch:
        patch.setattr(diag, 'MAX_ERROR_BODY', 1024)
        text = diag.summarize_validation_error(response(content=b'x' * (diag.MAX_ERROR_BODY + 1)))
        assert 'слишком велики' in text
    body = {'detail': [{'type': 'missing', 'loc': ['body', 'questions', f'q{i}']} for i in range(30)]}
    text = diag.summarize_validation_error(response(body))
    assert 'q3' in text and 'q4' not in text


def test_422_through_real_adapter_preserves_no_request_payload_or_secrets(monkeypatch):
    calls = []
    def post(url, **kwargs):
        calls.append(json.loads(kwargs['content']))
        return response({'detail': [{'type': 'extra_forbidden', 'loc': ['body', 'assets'],
                                     'input': SECRETS[2], 'msg': SECRETS[1]}]})
    monkeypatch.setattr(dp.httpx, 'post', post)
    pages = [{'file': SECRETS[3], 'page': None, 'content': b'fake image'}]
    result = dp.run_model('jev-1.14', 'https://api.hydraai.ru/v1', SECRETS[0], pages, 'visual', [{'type': 'noul', 'text': SECRETS[1]}])
    assert len(calls) == 1  # no retries, OCR substitution or extra paid requests
    assert 'HTTP 422' in result['error'] and 'body.assets' in result['error']
    assert 'лишнее или неподдерживаемое поле' in result['error']
    assert len(result['request_seconds']) == 1 and result['usage'] == []
    assert_no_secrets(result['error'])


def test_http_failure_without_details_stays_safe_and_independent():
    resp = response({'error': {'message': SECRETS[1]}}, status=500)
    exc = httpx.HTTPStatusError(SECRETS[0], request=resp.request, response=resp)
    text = dp._safe_error(exc)
    assert 'HTTP 500' in text
    assert_no_secrets(text)


def test_visual_error_displays_only_safe_dimensions_and_request_counts(monkeypatch):
    import io
    from PIL import Image
    buffer = io.BytesIO()
    Image.new('RGB', (800, 600), 'white').save(buffer, format='PNG')
    def post(url, **kwargs):
        return response({'detail': [{'type': 'extra_forbidden', 'loc': ['body', 'assets']}]})
    monkeypatch.setattr(dp.httpx, 'post', post)
    pages = [{'file': SECRETS[3], 'page': 1, 'content': buffer.getvalue()}]
    result = dp.run_model('jev-1.14', 'https://api.hydraai.ru/v1', SECRETS[0], pages, 'visual',
                          [{'type': 'noul', 'text': SECRETS[1]}])
    text = result['error']
    assert 'PNG-изображений 1, вопросов 1' in text
    assert '800×600 пикс.' in text and f'{len(buffer.getvalue())} байт' in text
    assert_no_secrets(text)


def test_common_provider_code_and_safe_field_mention_without_raw_message():
    body = {'error': {'type': 'invalid_request_error', 'message': 'Unsupported parameter: assets; ' + SECRETS[1]}}
    text = diag.summarize_validation_error(response(body))
    assert 'код: invalid_request_error' in text
    assert 'В сообщении упомянуто поле: assets' in text
    assert 'лишнее или неподдерживаемое поле' in text
    assert_no_secrets(text)


def test_large_echoed_image_can_be_parsed_but_never_echoed():
    encoded_image = 'PRIVATE_IMAGE_' * 100000  # exceeds the former 1 MiB cap
    body = {'detail': [{'type': 'extra_forbidden', 'loc': ['body', 'assets'], 'input': encoded_image}]}
    text = diag.summarize_validation_error(response(body))
    assert 'body.assets' in text and 'extra_forbidden' in text
    assert 'PRIVATE_IMAGE_' not in text


def test_plain_detail_message_can_provide_safe_field_hint():
    text = diag.summarize_validation_error(response({'detail': 'Unsupported parameter: assets. ' + SECRETS[1]}))
    assert 'В сообщении упомянуто поле: assets' in text
    assert 'лишнее или неподдерживаемое поле' in text
    assert_no_secrets(text)
