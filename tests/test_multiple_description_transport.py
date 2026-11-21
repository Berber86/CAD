"""Ensure each experimental transport callback sends exactly one request."""
import json
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
