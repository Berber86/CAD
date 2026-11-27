"""Ensure each experimental transport callback sends exactly one request."""
import json
import time
from types import SimpleNamespace

import pytest

import multiple_description_parse as md
import multiple_description_transport as transport
from test_multiple_description_parse import answer, item, pages


class FakeClient:
    def __init__(self, result):
        self.requests = []
        self.result = result
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
    def create(self, **kwargs):
        self.requests.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.result, refusal=None))])


def test_sender_sends_images_and_lot_context_but_not_search_data():
    client = FakeClient(answer([item()]))
    send = transport.make_sender(client, 'grok-4.7')
    raw = send(pages(2), {'filename_lot': '6', 'portal_lot': '7', 'search_answer': 'PRIVATE-SEARCH'})
    assert raw == answer([item()])
    assert len(client.requests) == 1
    request = client.requests[0]
    assert request['model'] == 'grok-4.7'
    assert request['messages'][0]['content'] == md.system_instruction()
    assert 'PRIVATE-SEARCH' not in repr(request)
    assert 'filename_lot' in repr(request) and 'portal_lot' in repr(request)
    images = [p for p in request['messages'][1]['content'] if p['type'] == 'image_url']
    assert [p['image_url']['url'] for p in images] == [p.data_url for p in pages(2)]


def test_size_rejection_only_on_http_413():
    class Error(Exception):
        def __init__(self, status): self.status_code = status
    for status in (400, 413):
        client = FakeClient(Error(status))
        send = transport.make_sender(client, 'test')
        with pytest.raises(md.ImageSizeRejected if status == 413 else Error):
            send(pages(2), {})
        assert len(client.requests) == 1


def test_selector_includes_separate_candidate_page_sets_and_sends_one_call():
    client = FakeClient('{"selected": 1}')
    selector = transport.make_selector(client, 'grok-4.7')
    attempts = [md.read_response(answer([item('a'), {}]), [1, 2]),
                md.read_response(answer([item('b'), {}]), [1])]
    assert selector(pages(2), attempts) == 1
    assert len(client.requests) == 1
    assert client.requests[0]['messages'][0]['content'] == md.COMPARISON_INSTRUCTION
    options = json.loads(client.requests[0]['messages'][1]['content'][0]['text'])
    assert [o['pages'] for o in options] == [[1, 2], [1]]
    assert [o['descriptions'][0]['data']['lot_number'] for o in options] == ['a', 'b']


@pytest.mark.parametrize('raw', ['{"selected": null}', '{"selected": true}', '{"selected": 5}', 'bad'])
def test_selector_rejects_invalid_selection(raw):
    client = FakeClient(raw)
    selector = transport.make_selector(client, 'model')
    with pytest.raises((ValueError, json.JSONDecodeError)):
        selector(pages(1), [md.read_response(answer([item(), {}]), [1])])
    assert len(client.requests) == 1


def test_transport_status_code_and_response_categories_are_preserved_safely():
    from multiple_description_errors import ProviderResponseError, classify_exception
    class Error(Exception):
        status_code = 413
    with pytest.raises(md.ImageSizeRejected) as rejection:
        transport.make_sender(FakeClient(Error('secret')), 'test')(pages(1), {})
    assert classify_exception(rejection.value) == 'http_413'
    client = FakeClient('')
    with pytest.raises(ProviderResponseError) as empty:
        transport.make_sender(client, 'test')(pages(1), {})
    assert classify_exception(empty.value) == 'empty_response'
    client.chat.completions.create = lambda **k: SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='', refusal='private refusal'))])
    with pytest.raises(ProviderResponseError) as refused:
        transport.make_sender(client, 'test')(pages(1), {})
    assert classify_exception(refused.value) == 'provider_refusal'
    assert 'private refusal' not in str(refused.value)


def test_hedge_launches_routerai_after_delay_and_first_success_wins():
    import threading
    calls = []
    release_primary = threading.Event()

    def slow_primary(pages_, context):
        calls.append('primary')
        release_primary.wait(2)
        return 'primary-answer'

    def fast_secondary(pages_, context):
        calls.append('secondary')
        return 'routerai-answer'

    send = transport.make_hedged_sender(slow_primary, fast_secondary, delay=0.05)
    assert send(pages(1), {}) == 'routerai-answer'
    assert calls == ['primary', 'secondary']
    release_primary.set()


def test_hedge_fast_primary_never_launches_secondary():
    calls = []
    send = transport.make_hedged_sender(
        lambda p, c: calls.append('primary') or 'primary-answer',
        lambda p, c: calls.append('secondary') or 'routerai-answer',
        delay=0.5)
    assert send(pages(1), {}) == 'primary-answer'
    assert calls == ['primary']


def test_hedge_primary_error_after_secondary_launch_returns_secondary():
    import threading

    def failing_primary(pages_, context):
        time.sleep(0.12)
        raise TimeoutError('hydra timeout')

    def fast_secondary(pages_, context):
        return 'routerai-answer'

    send = transport.make_hedged_sender(failing_primary, fast_secondary, delay=0.02)
    assert send(pages(1), {}) == 'routerai-answer'


def test_hedge_both_errors_prefers_primary_error():
    import threading

    def failing_primary(pages_, context):
        time.sleep(0.12)
        raise TimeoutError('primary-error')

    def failing_secondary(pages_, context):
        raise RuntimeError('secondary-error')

    send = transport.make_hedged_sender(failing_primary, failing_secondary, delay=0.02)
    with pytest.raises(TimeoutError, match='primary-error'):
        send(pages(1), {})


def test_content_accepts_list_text_blocks_and_selector_unwraps_fence():
    blocks = [{'type': 'text', 'text': '<think>x</think>\n```json\n{"selected": 1}\n```'}]
    client = FakeClient(blocks)
    selector = transport.make_selector(client, 'claude-haiku-5.5')
    attempts = [md.read_response(answer([item('a'), {}]), [1, 2]),
                md.read_response(answer([item('b'), {}]), [1])]
    assert selector(pages(2), attempts) == 1


def test_call_sends_temperature_and_json_mode_and_strips_only_explicit_400_rejection():
    class RejectJsonFormat(Exception):
        status_code = 400

        def __str__(self):
            return 'Unsupported parameter: response_format json_object is not supported'

    seen = []

    class AdaptiveClient:
        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def create(self, **kwargs):
            seen.append(dict(kwargs))
            if 'response_format' in kwargs:
                raise RejectJsonFormat()
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=answer([item()]), refusal=None))])

    raw = transport.make_sender(AdaptiveClient(), 'custom-model')(pages(1), {})
    assert raw == answer([item()])
    assert len(seen) == 2
    assert seen[0]['temperature'] == 0.0 and seen[0]['response_format'] == {'type': 'json_object'}
    assert 'response_format' not in seen[1] and seen[1]['temperature'] == 0.0

