import base64
import io
import json

import httpx
import pymupdf
import pytest
from PIL import Image

import developer_probability as dp


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / 'sample.pdf'
    doc = pymupdf.open()
    for text in ('first page', 'different second page', 'third page'):
        page = doc.new_page(width=612, height=792)
        page.insert_text((75, 90), text, fontsize=18)
        page.draw_rect(pymupdf.Rect(40, 40, 560, 700), color=(0, 0, 0))
    doc.save(path)
    doc.close()
    return path


def test_resolution_is_shared_by_preview_and_sent_payload(pdf, monkeypatch):
    opts = {'provider': 'routerai', 'pdf_image_side': 400}
    preview = dp.prepare_documents([(str(pdf), '2')], 'visual', **opts)[0]
    sent = dp.prepare_documents([(str(pdf), '1-3')], 'visual', **opts)[1]
    assert preview['content'] == sent['content']
    img = Image.open(io.BytesIO(sent['content']))
    assert img.size == (309, 400)
    state, assets = dp.make_state([sent], 'visual')
    calls = []
    def post(url, **kw):
        calls.append(kw)
        return httpx.Response(200, json={'answers': {}}, request=httpx.Request('POST', url))
    monkeypatch.setattr(dp.httpx, 'post', post)
    dp.post_routerai('https://routerai.ru/api/v1', 'key', dp.ROUTERAI_MODELS[0], state, assets, {'q': {'type': 'noul', 'instructions': 'q'}})
    body = json.loads(calls[0]['content'])
    payload_image = body['state'][1]['image_url']['url'].split(',', 1)[1]
    assert base64.b64decode(payload_image) == preview['content']


@pytest.mark.parametrize('side', [96, 160, 280, 400, 800, 1568])
def test_quality_degrades_pdf_before_minimum_padding(pdf, side):
    router = dp.prepare_documents([(str(pdf), '1')], 'visual', provider='routerai', pdf_image_side=side)[0]['content']
    hydra = dp.prepare_documents([(str(pdf), '1')], 'visual', provider='hydra', pdf_image_side=side)[0]['content']
    image1, image2 = Image.open(io.BytesIO(router)), Image.open(io.BytesIO(hydra))
    assert max(image2.size) <= side
    assert image1.width == max(280, image2.width)
    assert image1.height == max(280, image2.height)
    x, y = (image1.width - image2.width) // 2, (image1.height - image2.height) // 2
    assert image1.crop((x, y, x + image2.width, y + image2.height)).tobytes() == image2.tobytes()


def test_quality_only_affects_pdf_visual_not_ocr_or_photos(pdf, tmp_path):
    img = tmp_path / 'photo.jpg'
    Image.new('RGB', (500, 600), 'white').save(img)
    default = dp.prepare_documents([(str(img), '')], 'visual', provider='routerai')
    low = dp.prepare_documents([(str(img), '')], 'visual', provider='routerai', pdf_image_side=96)
    assert default == low
    assert dp.prepare_documents([(str(pdf), '1')], 'ocr', pdf_image_side=96) == dp.prepare_documents([(str(pdf), '1')], 'ocr')


def test_settings_persist_quality_and_invalid_or_old_migrates(tmp_path):
    path = tmp_path / 'settings.json'
    path.write_text('{"mode": "visual", "questions": []}', encoding='utf8')
    assert dp.load_settings(path)['pdf_image_side'] == 1568
    dp.save_settings({'questions': [], 'pdf_image_side': 160, 'document': 'private'}, path)
    assert dp.load_settings(path)['pdf_image_side'] == 160
    assert 'private' not in path.read_text(encoding='utf8')
    path.write_text('{"mode": "visual", "questions": [], "pdf_image_side": 999999}', encoding='utf8')
    assert dp.load_settings(path)['pdf_image_side'] == 1568
    for bad in [True, 0, 95, 1569, 4.2, '800']:
        with pytest.raises(ValueError):
            dp.valid_pdf_image_side(bad)


def test_default_is_previous_raster(pdf):
    assert dp.prepare_documents([(str(pdf), '1')], 'visual') == dp.prepare_documents([(str(pdf), '1')], 'visual', pdf_image_side=1568)
