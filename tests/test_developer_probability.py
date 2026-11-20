import base64
import json
import threading
from pathlib import Path

import httpx
import pytest
import pymupdf
from PIL import Image

import developer_probability as dp


def test_settings_are_atomic_and_do_not_save_sensitive_data(tmp_path):
    path = tmp_path / 'private' / 'settings.json'
    settings = {'mode': 'visual', 'questions': [{'type': 'score', 'text': 'q', 'low': 'bad', 'high': 'good', 'active': False, 'document': 'secret'}], 'document': 'secret.pdf', 'result': 'private'}
    dp.save_settings(settings, path)
    raw = path.read_text(encoding='utf8')
    assert 'secret' not in raw and 'private' not in raw
    loaded = dp.load_settings(path)
    assert loaded['mode'] == 'visual' and loaded['questions'][0]['active'] is False
    dp.save_settings({'mode': 'ocr', 'questions': []}, path)
    assert dp.load_settings(path)['questions'] == []
    assert not list(path.parent.glob('*.tmp'))


@pytest.mark.parametrize('spec,expected', [('', [1, 2, 3, 4, 5]), ('1-3,5,2', [1, 2, 3, 5]), ('3', [3])])
def test_pages(spec, expected):
    assert dp.parse_pages(spec, 5) == expected


@pytest.mark.parametrize('spec', ['0', '2-1', '1-6', '1,,2', '-2', '1-a'])
def test_pages_reject_invalid(spec):
    with pytest.raises(ValueError):
        dp.parse_pages(spec, 5)


def test_questions_score_validation_and_weighted_result():
    qs = [{'type': 'score', 'text': 'quality', 'levels': 'bad | medium | good'}, {'type': 'noul', 'text': 'present'}]
    built = dp.build_questions(qs)
    assert built['q1']['criteria'] == ['bad', 'medium', 'good']
    result = dp.normalize_answer({'type': 'score', 'probabilities': {'0': .0, '1': .7, '2': .3}}, built['q1'])
    assert result['value'] == pytest.approx(.65)
    with pytest.raises(ValueError):
        dp.normalize_answer({'type': 'score', 'score': 2}, built['q1'])
    with pytest.raises(ValueError):
        dp.normalize_answer({'type': 'noul', 'noul': float('nan')}, built['q2'])
    with pytest.raises(ValueError):
        dp.build_questions([{'type': 'score', 'text': 'q', 'levels': '|'.join('abcdefghijk')}])
    assert dp.build_questions([{'type': 'score', 'text': 'q', 'low': 'none', 'high': 'all'}])['q1']['criteria'] == ['none', 'all']


def test_pdf_text_image_visual_and_local_ocr(tmp_path, monkeypatch):
    pdf_path = tmp_path / 'test.pdf'
    pdf = pymupdf.open()
    for text in ['first', 'second']:
        page = pdf.new_page()
        page.insert_text((50, 70), text)
    pdf.save(pdf_path)
    pdf.close()
    image = tmp_path / 'photo.jpg'
    Image.new('RGB', (50, 30), 'white').save(image)
    captured = []
    monkeypatch.setattr(dp, '_ocr', lambda img: captured.append(img.size) or 'locally recognised')
    ocr = dp.prepare_documents([(str(pdf_path), '2'), (str(image), '')], 'ocr')
    assert ocr[0]['content'].strip() == 'second' and ocr[1]['content'] == 'locally recognised'
    assert len(captured) == 1
    visual = dp.prepare_documents([(str(pdf_path), '1-2'), (str(image), '')], 'visual')
    assert len(visual) == 3 and all(p['content'].startswith(b'\x89PNG') for p in visual)
    assert len(captured) == 1  # visual mode never runs OCR
    state, assets = dp.make_state(visual, 'visual')
    assert len(assets) == 3 and state['page_1_description'].endswith('page 1')
    assert base64.b64decode(assets[state['page_1']]['data_base64']).startswith(b'\x89PNG')
    with pytest.raises(ValueError):
        dp.prepare_documents([(str(image), '1')], 'visual')


def test_http_payload_auth_asset_and_no_redirect(monkeypatch):
    seen = []
    def post(url, **kwargs):
        seen.append((url, kwargs))
        request = httpx.Request('POST', url)
        return httpx.Response(200, request=request, json={'answers': {'q1': {'type': 'noul', 'noul': .8}}})
    monkeypatch.setattr(dp.httpx, 'post', post)
    pages = [{'file': 'scan.png', 'page': None, 'content': b'png'}]
    state, assets = dp.make_state(pages, 'visual')
    qs = dp.build_questions([{'type': 'noul', 'text': 'q'}])
    dp.post_systemone('https://api.hydraai.ru/v1', 'secret', 'jev-1.14', state, assets, qs)
    url, options = seen[0]
    assert url.endswith('/v1/systemone') and options['follow_redirects'] is False
    assert options['headers']['Authorization'] == 'Bearer secret'
    assert options['headers']['TypeSafe-Danger-Zone-Images'] == 'true'
    body = json.loads(options['content'])
    assert body['assets'] == assets and body['questions'] == qs
    dp.post_systemone('https://api.hydraai.ru/v1', 'secret', 'solar-decide', {'x': 'text'}, {}, qs)
    assert 'assets' not in json.loads(seen[1][1]['content'])
    assert 'TypeSafe-Danger-Zone-Images' not in seen[1][1]['headers']
    with pytest.raises(ValueError):
        dp.post_systemone('http://unsafe/v1', 'secret', 'jev', {}, {}, qs)


def test_page_chunks_multiple_followup_uses_first_and_last():
    pages = [{'file': 'doc.pdf', 'page': i, 'content': f'page {i}'} for i in range(1, 31)]
    calls = []
    def send(url, key, model, state, assets, questions):
        calls.append((state, questions))
        if 'page' in questions:
            selected = 'multiple' if 'p1' in questions['page']['criteria'] else 'p28'
            answer = {'page': {'type': 'choice', 'choice': selected, 'probabilities': {selected: 1.0}}}
        else:
            assert set(questions) == {'first', 'last'}
            last = 'p28' if 'p28' in questions['last']['criteria'] else 'p20'
            answer = {'first': {'type': 'choice', 'choice': 'p3'}, 'last': {'type': 'choice', 'choice': last}}
        return {'answers': answer, 'usage': {'input_tokens': 5}}
    result = dp.run_model('jev-1.14', 'url', 'key', pages, 'ocr', [{'type': 'pages', 'text': 'Which page?'}], send)
    assert 'error' not in result and len(calls) == 4 and len(result['request_seconds']) == 4
    answer = result['entries'][0]['answer']
    assert (answer['first'], answer['last']) == (3, 28)
    assert [len(c[1]['page']['criteria']) for c in (calls[0], calls[2])] == [26, 8]


def test_failed_model_does_not_hide_other_and_no_ocr_fallback():
    barrier = threading.Barrier(2)
    seen = []
    def send(url, key, model, state, assets, questions):
        seen.append((model, len(assets)))
        barrier.wait(timeout=3)
        if model == 'jev-1.14':
            raise httpx.HTTPStatusError('PRIVATE', request=httpx.Request('POST', 'https://example.com'), response=httpx.Response(400))
        return {'answers': {'q1': {'type': 'noul', 'noul': .7}}}
    pages = [{'file': 'x.jpg', 'page': None, 'content': b'png'}]
    result = dp.run_both('https://api.hydraai.ru/v1', 'key', pages, 'visual', [{'type': 'noul', 'text': 'q'}], send)
    assert len(seen) == 2 and all(n == 1 for _, n in seen)
    assert 'HTTP 400' in result[0]['error'] and 'PRIVATE' not in result[0]['error']
    assert result[1]['entries'][0]['answer']['value'] == .7


def test_inconsistent_page_response_does_not_fabricate_range():
    pages = [{'file': 'doc.pdf', 'page': i, 'content': 'text'} for i in range(1, 3)]
    def send(url, key, model, state, assets, qs):
        if 'page' in qs:
            return {'answers': {'page': {'type': 'choice', 'choice': 'multiple'}}}
        return {'answers': {'first': {'type': 'choice', 'choice': 'p1'}, 'last': {'type': 'choice', 'choice': 'p1'}}}
    result = dp.run_model('jev', 'url', 'key', pages, 'ocr', [{'type': 'pages', 'text': 'q'}], send)
    assert 'error' in result and not result['entries']


def test_mixed_pdf_text_and_scan_runs_ocr_for_image_content(tmp_path, monkeypatch):
    pdf_path = tmp_path / 'mixed.pdf'
    png_path = tmp_path / 'scan.png'
    Image.new('RGB', (40, 40), 'black').save(png_path)
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((50, 70), 'watermark')
    page.insert_image(pymupdf.Rect(100, 100, 200, 200), filename=str(png_path))
    pdf.save(pdf_path)
    pdf.close()
    calls = []
    monkeypatch.setattr(dp, '_ocr', lambda img: calls.append(True) or 'scan contents')
    result = dp.prepare_documents([(str(pdf_path), '')], 'ocr')
    assert calls == [True]
    assert 'watermark' in result[0]['content'] and 'scan contents' in result[0]['content']


def test_real_local_ocr_when_installed():
    import ocr_pages
    from PIL import ImageDraw, ImageFont
    font = Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
    if not ocr_pages.available() or not font.exists():
        pytest.skip('RapidOCR or test font unavailable')
    image = Image.new('RGB', (1200, 250), 'white')
    ImageDraw.Draw(image).text((30, 40), 'PROPERTY LINE 100 FEET', fill='black', font=ImageFont.truetype(str(font), 65))
    result = dp._ocr(image).upper()
    assert 'PROPERTY' in result and 'FEET' in result


def test_normal_answer_survives_page_search_failure():
    pages = [{'file': 'doc.pdf', 'page': 1, 'content': 'text'}]
    def send(url, key, model, state, assets, questions):
        if 'q1' in questions:
            return {'answers': {'q1': {'type': 'noul', 'noul': .9}}}
        raise httpx.TimeoutException('secret input')
    result = dp.run_model('jev', 'url', 'key', pages, 'ocr', [
        {'type': 'noul', 'text': 'yes?'}, {'type': 'pages', 'text': 'which page?'}], send)
    assert result['entries'][0]['answer']['value'] == .9
    assert 'ожидания' in result['error'] and 'secret input' not in result['error']
    assert len(result['request_seconds']) == 2
