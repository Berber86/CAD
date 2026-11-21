"""End-to-end orchestration with synthetic PDFs and mock transports only."""
import json

import pymupdf
import pytest

import multiple_description_pipeline as pipeline
from multiple_description_parse import ImageSizeRejected, ParseDisabled
from test_multiple_description_parse import ON, answer, item
from test_legal_page_search import make_pdf, response


def searcher(base, key, model, state, assets, qs):
    return response(qs, {1: .9, 2: .8}, 'two')


def test_disabled_before_open_render_or_network(tmp_path):
    def no(*args, **kwargs):
        pytest.fail('unexpected side effect')
    with pytest.raises(ParseDisabled):
        pipeline.parse_pdf(tmp_path / 'missing.pdf', {}, search_api_key='x', provider='hydra',
                           model='grok-4.7', image_limit=10, portal_lot=None, send=no,
                           search_send=no, render=no)
    assert not list(tmp_path.iterdir())


def test_search_parse_and_cache_reuse_with_changed_portal(tmp_path):
    pdf = tmp_path / 'OUR 6 NEW.pdf'
    make_pdf(pdf, 2)
    calls = []
    def send(images, context):
        calls.append(([p.number for p in images], dict(context)))
        return answer([item('5'), item('6')])
    args = dict(search_api_key='key', provider='hydra', model='grok-4.7', image_limit=10,
                send=send, search_send=searcher)
    result = pipeline.parse_pdf(pdf, ON, portal_lot='8', **args)
    assert result.status == 'parsed' and not result.cached and result.parse.complete
    assert [i.data.lot_number for i in result.parse.items] == ['5', '6']
    assert calls == [([1, 2], {'filename_lot': '6', 'portal_lot': '8'})]
    result2 = pipeline.parse_pdf(pdf, ON, portal_lot='9', **args)
    assert result2.cached and result2.original_portal_lot == '8' and result2.current_portal_lot == '9'
    assert len(calls) == 1
    history = json.loads((tmp_path / 'OUR 6 NEW.pdf.multi-parse.json').read_text())
    assert [entry['lot'] for entry in history['portal_observations']] == ['8', '9']
    for name in ('OUR 6 NEW.pdf.multi-parse.json', 'OUR 6 NEW.pdf.page-search.json'):
        text = (tmp_path / name).read_text(encoding='utf8')
        assert 'data:image' not in text and 'key' not in text


def test_missing_rerender_is_incomplete_and_does_not_hide_other_candidates(tmp_path):
    pdf = tmp_path / 'OUR 6 NEW.pdf'
    make_pdf(pdf, 2)
    def render(path, **kwargs):
        if kwargs['pages'] == [1]:
            raise ValueError('unreadable')
        return ['data:image/png;base64,ZmFrZQ==']
    result = pipeline.parse_pdf(pdf, ON, search_api_key='key', provider='hydra', model='grok-4.7',
                                image_limit=10, portal_lot=None, search_send=searcher, render=render,
                                send=lambda imgs, ctx: answer([item('6')]))
    assert result.render_errors == (1,) and result.parse.pages == (2,)
    assert result.parse.unparsed_pages == (1,) and not result.parse.complete
    again = pipeline.parse_pdf(pdf, ON, search_api_key='key', provider='hydra', model='grok-4.7',
                               image_limit=10, portal_lot=None, search_send=searcher, render=render,
                               send=lambda *a: pytest.fail('expected cache'))
    assert again.cached and again.parse.unparsed_pages == (1,)


def test_limit_does_not_render_excess_pages_but_marks_them_unparsed(tmp_path):
    pdf = tmp_path / 'test.pdf'
    make_pdf(pdf, 2)
    rendered = []
    def render(path, **kwargs):
        rendered.extend(kwargs['pages'])
        return ['data:image/png;base64,ZmFrZQ==']
    result = pipeline.parse_pdf(pdf, ON, search_api_key='key', provider='hydra', model='grok-4.7',
                                image_limit=1, portal_lot=None, search_send=searcher, render=render,
                                send=lambda *a: answer([item()]))
    assert rendered == [1] and result.parse.unparsed_pages == (2,) and not result.parse.complete


def test_no_pages_is_not_sent_to_parsing_model(tmp_path):
    pdf = tmp_path / 'test.pdf'
    make_pdf(pdf, 2)
    def no_candidates(base, key, model, state, assets, qs):
        return response(qs, {}, 'zero')
    result = pipeline.parse_pdf(pdf, ON, search_api_key='key', provider='hydra', model='grok-4.7',
                                image_limit=10, portal_lot=None, search_send=no_candidates,
                                send=lambda *a: pytest.fail('must not parse'))
    assert result.status == 'no_candidates' and result.parse is None


def test_size_failure_and_reuse_incomplete_result(tmp_path):
    pdf = tmp_path / 'test.pdf'
    make_pdf(pdf, 2)
    calls = []
    def send(imgs, ctx):
        calls.append(len(imgs))
        if len(imgs) == 2:
            raise ImageSizeRejected(1)
        return answer([item()])
    kwargs = dict(search_api_key='key', provider='hydra', model='grok-4.7', image_limit=10,
                  portal_lot=None, search_send=searcher, send=send)
    result = pipeline.parse_pdf(pdf, ON, **kwargs)
    assert calls == [2, 1] and not result.parse.complete
    assert result.parse.unparsed_pages == (2,)
    assert pipeline.parse_pdf(pdf, ON, **kwargs).cached
    assert calls == [2, 1]


def test_all_images_unavailable_records_non_cacheable_failure(tmp_path):
    pdf = tmp_path / 'test.pdf'
    make_pdf(pdf, 2)
    def broken(*args, **kwargs):
        raise ValueError('private image details')
    args = dict(search_api_key='key', provider='hydra', model='grok-4.7', image_limit=10,
                portal_lot=None, search_send=searcher, render=broken,
                send=lambda *a: pytest.fail('no available images'))
    result = pipeline.parse_pdf(pdf, ON, **args)
    assert result.status == 'images_unavailable' and result.render_errors == (1, 2)
    text = (tmp_path / 'test.pdf.multi-parse.json').read_text()
    run = json.loads(text)['runs'][-1]
    assert run['error'] == 'images_unavailable' and run['unparsed_pages'] == [1, 2]
    assert not run['cacheable'] and 'private image details' not in text


def test_vision_fallback_only_on_confirmed_absence():
    selected = pipeline.VisionTarget('hydra', 'configured-model', 50)
    fallback = pipeline.VisionTarget('hydra', 'grok-4.7', 10)
    assert pipeline.choose_vision_target(selected, 'yes', fallback) == selected
    assert pipeline.choose_vision_target(selected, 'unknown', fallback) == selected
    assert pipeline.choose_vision_target(selected, 'no', fallback) == fallback
    with pytest.raises(pipeline.VisionUnavailable):
        pipeline.choose_vision_target(selected, 'no', None)
    with pytest.raises(ValueError):
        pipeline.choose_vision_target(selected, 'timeout', fallback)
