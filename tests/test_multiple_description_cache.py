"""Strict, offline caching for the experimental all-descriptions parser."""
import json
import hashlib

import pytest

import file_metadata
import multiple_description_cache as cache
import multiple_description_parse as md
from test_multiple_description_parse import ON, answer, item, pages


def context(pdf, **changes):
    args = dict(pdf_sha256=file_metadata._digest(pdf), pages=pages(2),
                image_settings={'format': 'png', 'max_side': 1568}, provider='hydra',
                model='grok-4.7', filename_lot='6', search_complete=True, image_limit=10)
    args.update(changes)
    return cache.cache_context(**args)


def parse(*, response=None, search_complete=True):
    return md.parse_pages(ON, pages(2), image_limit=10, filename_lot='6', portal_lot='7',
                          search_complete=search_complete, send=lambda *a: response or answer([item('5')]))


def test_cache_reuses_incomplete_result_and_keeps_original_portal_context(tmp_path):
    pdf = tmp_path / 'OUR 6.pdf'
    pdf.write_bytes(b'pdf contents')
    ctx = context(pdf)
    result = parse(response=answer([item('5', complete=False)], complete=False))
    cache.save_result(pdf, ctx, result, portal_lot='7')
    restored, original_portal = cache.load_result(pdf, ctx)
    assert original_portal == '7' and restored.selection == 'cache'
    assert not restored.complete and restored.items[0].data.lot_number == '5'
    raw = cache.diagnostics_path(pdf).read_text(encoding='utf8')
    assert 'data:image/' not in raw and 'pdf contents' not in raw and 'ZmFrZQ' not in raw
    assert 'image_sha256' in raw


@pytest.mark.parametrize('changes', [
    {'provider': 'routerai'}, {'model': 'other'}, {'filename_lot': '8'},
    {'image_settings': {'format': 'png', 'max_side': 1024}},
    {'pages': pages(1)}, {'image_limit': 8}, {'search_complete': False},
    {'instruction': 'new instruction'}, {'response_format': 'v2'},
])
def test_each_changed_condition_misses(tmp_path, changes):
    pdf = tmp_path / 'test.pdf'
    pdf.write_bytes(b'pdf')
    cache.save_result(pdf, context(pdf), parse(), portal_lot=None)
    assert cache.load_result(pdf, context(pdf, **changes)) is None


def test_changed_image_bytes_also_miss(tmp_path):
    pdf = tmp_path / 'test.pdf'
    pdf.write_bytes(b'pdf')
    ctx = context(pdf)
    cache.save_result(pdf, ctx, parse(), portal_lot=None)
    different = [md.PageImage(1, 'data:image/png;base64,Qg'), pages(2)[1]]
    assert cache.load_result(pdf, context(pdf, pages=different)) is None


def test_changed_pdf_cannot_use_or_save_old_context(tmp_path):
    pdf = tmp_path / 'test.pdf'
    pdf.write_bytes(b'old pdf')
    ctx = context(pdf)
    cache.save_result(pdf, ctx, parse(), portal_lot=None)
    pdf.write_bytes(b'new pdf')
    assert cache.load_result(pdf, ctx) is None
    with pytest.raises(cache.CacheError):
        cache.save_result(pdf, ctx, parse(), portal_lot=None)
    assert len(json.loads(cache.diagnostics_path(pdf).read_text())['runs']) == 1


def test_failed_result_is_diagnostic_not_cache(tmp_path):
    pdf = tmp_path / 'test.pdf'
    pdf.write_bytes(b'pdf')
    def failure(*a):
        raise TimeoutError('sensitive body')
    result = md.parse_pages(ON, pages(2), image_limit=10, filename_lot='6', portal_lot='7',
                            search_complete=True, send=failure)
    cache.save_result(pdf, context(pdf), result, portal_lot='7')
    assert cache.load_result(pdf, context(pdf)) is None
    assert 'sensitive body' not in cache.diagnostics_path(pdf).read_text()


def test_size_reduced_prefix_can_be_cached(tmp_path):
    pdf = tmp_path / 'test.pdf'
    pdf.write_bytes(b'pdf')
    def send(imgs, ctx):
        if len(imgs) == 2:
            raise md.ImageSizeRejected(1)
        return answer([item()])
    result = md.parse_pages(ON, pages(2), image_limit=10, filename_lot='6', portal_lot=None,
                            search_complete=True, send=send)
    assert result.pages == (1,) and result.unparsed_pages == (2,) and not result.complete
    cache.save_result(pdf, context(pdf), result, portal_lot=None)
    cached, _ = cache.load_result(pdf, context(pdf))
    assert cached.pages == (1,) and cached.unparsed_pages == (2,) and not cached.complete


def test_corrupt_cache_is_never_overwritten(tmp_path):
    pdf = tmp_path / 'test.pdf'
    pdf.write_bytes(b'pdf')
    target = cache.diagnostics_path(pdf)
    target.write_text('bad data', encoding='utf8')
    with pytest.raises(cache.CacheError):
        cache.save_result(pdf, context(pdf), parse(), portal_lot=None)
    assert target.read_text() == 'bad data'
    with pytest.raises(cache.CacheError):
        cache.load_result(pdf, context(pdf))


def test_invalid_saved_item_is_not_reused(tmp_path):
    pdf = tmp_path / 'test.pdf'
    pdf.write_bytes(b'pdf')
    ctx = context(pdf)
    cache.save_result(pdf, ctx, parse(), portal_lot=None)
    path = cache.diagnostics_path(pdf)
    data = json.loads(path.read_text())
    data['runs'][0]['items'][0]['data']['segments'] = []
    path.write_text(json.dumps(data))
    assert cache.load_result(pdf, ctx) is None


def test_partial_single_side_survives_cache_validation(tmp_path):
    pdf = tmp_path / 'test.pdf'
    pdf.write_bytes(b'pdf')
    partial = item('5', complete=False)
    partial['data']['segments'] = partial['data']['segments'][:1]
    result = parse(response=answer([partial], complete=False))
    assert len(result.items[0].data.segments) == 1
    cache.save_result(pdf, context(pdf), result, portal_lot=None)
    cached, _ = cache.load_result(pdf, context(pdf))
    assert not cached.complete and len(cached.items[0].data.segments) == 1


def test_cache_fingerprints_exact_model_and_selector_instructions(tmp_path):
    pdf = tmp_path / 'test.pdf'
    pdf.write_bytes(b'pdf')
    ctx = context(pdf)
    assert ctx['instruction_sha256'] == hashlib.sha256(md.system_instruction().encode()).hexdigest()
    assert ctx['comparison_instruction_sha256'] == hashlib.sha256(md.COMPARISON_INSTRUCTION.encode()).hexdigest()
    cache.save_result(pdf, ctx, parse(), portal_lot=None)
    assert cache.load_result(pdf, context(pdf, comparison_instruction='different comparison')) is None


def test_comparison_failure_is_recorded_without_exception_details(tmp_path):
    pdf = tmp_path / 'test.pdf'
    pdf.write_bytes(b'pdf')
    result = md.parse_pages(ON, pages(2), image_limit=10, filename_lot='6', portal_lot=None,
                            search_complete=True, send=lambda *a: answer([item(), {}]),
                            choose=lambda *a: (_ for _ in ()).throw(TimeoutError('secret-response')))
    cache.save_result(pdf, context(pdf), result, portal_lot=None)
    content = cache.diagnostics_path(pdf).read_text()
    run = json.loads(content)['runs'][-1]
    assert run['comparison'] == {'status': 'request_failed', 'attempt_indices': [0, 1, 2], 'pages': [1, 2]}
    assert run['context']['provider'] == 'hydra' and run['context']['model'] == 'grok-4.7'
    assert run['selected_attempt'] == 0 and 'secret-response' not in content
    assert cache.load_result(pdf, context(pdf)) is not None


def test_failed_attempt_history_has_only_safe_category_and_timing(tmp_path):
    pdf = tmp_path / 'test.pdf'
    pdf.write_bytes(b'pdf')
    def fail(*a):
        raise TimeoutError('SECRET response')
    result = md.parse_pages(ON, pages(2), image_limit=10, filename_lot='6', portal_lot=None,
                            search_complete=True, send=fail)
    cache.save_result(pdf, context(pdf), result, portal_lot=None)
    text = cache.diagnostics_path(pdf).read_text()
    attempts = json.loads(text)['runs'][-1]['attempts']
    assert all(a['failure_kind'] == 'timeout' and a['elapsed_seconds'] >= 0 for a in attempts)
    assert 'SECRET' not in text


def test_offline_restore_rejects_changed_or_incompatible_history(tmp_path):
    pdf = tmp_path / 'OUR 6.pdf'
    pdf.write_bytes(b'original')
    ctx = context(pdf)
    result = parse()
    cache.save_result(pdf, ctx, result, portal_lot=None)
    original = cache.diagnostics_path(pdf).read_bytes()
    found = cache.load_latest_offline(pdf)
    assert found and found[0].complete and found[1] == ctx
    assert cache.diagnostics_path(pdf).read_bytes() == original
    pdf.write_bytes(b'changed')
    assert cache.load_latest_offline(pdf) is None
    pdf.write_bytes(b'original')
    bad = json.loads(original)
    bad['runs'][0]['context']['instruction_sha256'] = '0' * 64
    cache.diagnostics_path(pdf).write_text(json.dumps(bad))
    assert cache.load_latest_offline(pdf) is None


def test_offline_restore_rejects_incomplete_and_mismatched_filename(tmp_path):
    pdf = tmp_path / 'OUR 6.pdf'
    pdf.write_bytes(b'original')
    ctx = context(pdf)
    result = parse(response=answer([item('5', complete=False)], complete=False))
    cache.save_result(pdf, ctx, result, portal_lot=None)
    assert cache.load_latest_offline(pdf) is None
    other = tmp_path / 'OUR 7.pdf'
    other.write_bytes(b'original')
    cache.diagnostics_path(other).write_bytes(cache.diagnostics_path(pdf).read_bytes())
    assert cache.load_latest_offline(other) is None


def test_old_known_prompt_only_restored_offline_not_as_current_parse(tmp_path):
    pdf = tmp_path / 'OUR 6.pdf'
    pdf.write_bytes(b'original')
    ctx = context(pdf)
    ctx['instruction_sha256'] = '8d57be14196feb1e104ac5c60219f40e071bcc9d6007e81a4fc5a008bd4a4769'
    ctx['response_format'] = 'multiple-legal-descriptions-v3'
    ctx['description_schema_sha256'] = '5ef4cdb9afa8ad6ff57784f183da8f0496a5763af91d7b673ebec74670d61bcc'
    cache.save_result(pdf, ctx, parse(), portal_lot=None)
    found = cache.load_latest_offline(pdf)
    assert found is not None and found[1]['instruction_sha256'] == ctx['instruction_sha256']
    assert cache.load_result(pdf, context(pdf)) is None


@pytest.mark.parametrize('old_prompt', [
    '8d57be14196feb1e104ac5c60219f40e071bcc9d6007e81a4fc5a008bd4a4769',
    '1e05287cfcb562ac81cae64b7e3c95c82047ed850424fe38a920533297de0502',
])
def test_historic_v3_without_new_fields_and_pair_validation(tmp_path, old_prompt):
    pdf = tmp_path / 'OUR 6.pdf'
    pdf.write_bytes(b'original')
    ctx = context(pdf)
    ctx.update(response_format='multiple-legal-descriptions-v3', instruction_sha256=old_prompt,
               description_schema_sha256='5ef4cdb9afa8ad6ff57784f183da8f0496a5763af91d7b673ebec74670d61bcc')
    cache.save_result(pdf, ctx, parse(), portal_lot=None)
    path = cache.diagnostics_path(pdf)
    body = json.loads(path.read_text(encoding='utf-8'))
    for item in body['runs'][-1]['items']:
        item['data'].pop('main_street_corrected', None)
        item['data'].pop('cross_street_corrected', None)
    path.write_text(json.dumps(body), encoding='utf-8')
    restored = cache.load_latest_offline(pdf)
    assert restored is not None and restored[0].items[0].data.main_street_corrected is None
    assert cache.load_result(pdf, context(pdf)) is None
    # A known prompt paired with a different schema is not a known history.
    ctx['description_schema_sha256'] = '0' * 64
    cache.save_result(pdf, ctx, parse(), portal_lot=None)
    body = json.loads(path.read_text(encoding='utf-8'))
    body['runs'] = body['runs'][-1:]
    path.write_text(json.dumps(body), encoding='utf-8')
    assert cache.load_latest_offline(pdf) is None
