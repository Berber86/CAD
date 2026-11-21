"""Offline tests for the optional simplified page-search comparison."""
import json

import httpx
import pymupdf
import pytest

import developer_probability as dp
from tools import decision_all_pages_probe as probe
from tools import make_lot_boundary_pdf as fixture


def blank_pdf(path, count):
    with pymupdf.open() as doc:
        for _ in range(count):
            doc.new_page()
        doc.save(path)


def test_batches_cover_without_extra_tail_and_overlap():
    for count in (1, 8, 9, 15, 16, 200):
        groups = probe.batch_numbers(count)
        assert set().union(*map(set, groups)) == set(range(1, count + 1))
        assert all(1 <= len(g) <= 8 for g in groups)
        assert all(a[-1] == b[0] for a, b in zip(groups, groups[1:]))
    assert probe.batch_numbers(15) == [list(range(1, 9)), list(range(8, 16))]
    assert probe.batch_numbers(8) == [list(range(1, 9))]


def test_questions_no_lot_or_description_boundaries():
    questions = probe.questions_for([{"page": 8}, {"page": 9}])
    assert set(questions) == {"page_8", "page_9", "count"}
    assert "PDF page 8" in questions["page_8"]["instructions"]
    assert "supplied pages" in questions["count"]["instructions"]


def test_dry_run_cap_and_no_network_or_key(tmp_path, capsys, monkeypatch):
    path = tmp_path / 'sensitive.pdf'
    blank_pdf(path, 201)
    monkeypatch.setattr(probe.common, 'load_provider_key', lambda *a: pytest.fail('key'))
    monkeypatch.setattr(dp, 'post_routerai', lambda *a: pytest.fail('network'))
    assert probe.main([str(path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['covered_pages'] == 200 and out['uncovered_pages'] == 1
    assert out['planned_requests'] == len(out['batch_pages']) * 2
    assert 'sensitive' not in json.dumps(out)


def test_live_identical_images_questions_overlap_usage_and_failure(tmp_path, capsys, monkeypatch):
    path = tmp_path / 'private.pdf'
    assert fixture.main([str(path), '--start-before-overlap']) == 0
    fixture_info = json.loads(capsys.readouterr().out)
    assert fixture_info['lots']['6'] == [7, 8, 9, 10]
    with pymupdf.open(path) as doc:
        assert 'LOT 6' in doc[6].get_text() and 'LOT 6' not in doc[7].get_text()
        assert 'THENCE' in doc[7].get_text()
    monkeypatch.setattr(probe.common, 'load_provider_key', lambda *_: 'private-key')
    calls = []
    def send(url, key, model, state, assets, questions):
        calls.append((model, state, assets, questions))
        if len(calls) == 2:
            raise httpx.TimeoutException('private information')
        answers = {k: ({'type': 'noul', 'noul': 0.1} if q['type'] == 'noul' else
                       {'type': 'choice', 'choice': 'one'}) for k, q in questions.items()}
        return {'answers': answers, 'usage': {'input_tokens': 123, 'cost': 0.01,
                                              'private': 'private-key', 'total_tokens': True}}
    monkeypatch.setattr(dp, 'post_routerai', send)
    assert probe.main([str(path), '--live']) == 1
    out = json.loads(capsys.readouterr().out)
    assert len(calls) == 4
    assert [b['status'] for r in out['results'] for b in r['batches']] == ['ok', 'error', 'ok', 'ok']
    assert out['results'][0]['batches'][0]['usage'] == {'input_tokens': 123, 'cost': 0.01}
    assert out['results'][0]['batches'][1]['usage'] is None
    assert out['results'][0]['pages'][7]['decision'] == 'unknown'  # overlap with one failed batch
    assert out['results'][0]['pages'][8]['decision'] == 'unknown'
    for left, right in ((calls[0], calls[2]), (calls[1], calls[3])):
        assert left[3] == right[3]
        for i in range(1, 9):
            label = f'page_{i}'
            assert left[1][label + '_description'] == right[1][label + '_description']
            assert left[1][label + '_description'].startswith('document.pdf')
            assert left[2][left[1][label]] == right[2][right[1][label]]
    assert calls[0][2][calls[0][1]['page_8']] == calls[1][2][calls[1][1]['page_1']]
    assert 'private' not in json.dumps(out)


def test_partial_max_and_unknown():
    batches = [dict(pages=[1, 2], answers={'page_1': {'status': 'ok', 'value': .5},
                                            'page_2': {'status': 'ok', 'value': .4}}),
               dict(pages=[2, 3], answers={'page_2': {'status': 'error', 'value': None},
                                            'page_3': {'status': 'ok', 'value': .2}})]
    pages = probe.aggregate(3, batches)
    assert pages[1] == {'page': 2, 'observations': [.4, None], 'max_probability': .4,
                        'decision': 'selected', 'incomplete': True}
    assert pages[2]['decision'] == 'not_selected'
    batches[0]['answers']['page_2']['value'] = .1
    assert probe.aggregate(3, batches)[1]['decision'] == 'unknown'


def test_usage_numeric_allowlist_and_unavailable():
    assert probe.safe_usage({'usage': {'input_tokens': 4, 'cost': float('nan'), 'output_tokens': -2,
                                      'secret': 'key'}}) == {'input_tokens': 4}
    assert probe.safe_usage({'usage': {'cost': 'unknown'}}) is None
    assert probe.safe_usage({}) is None
