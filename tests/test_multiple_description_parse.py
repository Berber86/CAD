"""Network-free tests for the experimental stage 3 parsing policy."""
import json
import math

import pytest

import multiple_description_parse as md

ON = {"multiple_description_search": {"enabled": True}}


def pages(n):
    return [md.PageImage(i, f"data:image/png;base64,ZmFrZQ{i}") for i in range(1, n + 1)]


def item(lot="1", *, complete=True, delta=0):
    def seg(ns, degrees, ew, distance):
        return dict(ns=ns, degrees=degrees, minutes=0, seconds=0, ew=ew, distance_ft=distance)
    return {"complete": complete, "data": {"main_street": "Main St", "lot_number": lot,
        "segments": [seg('N', 0, 'E', 10), seg('N', 90, 'E', 10),
                     seg('S', 0, 'E', 10), seg('S', 90, 'W', 10 + delta)]}}


def answer(items, complete=True):
    return json.dumps({"descriptions": items, "complete": complete})


def call(send, n=2, **kwargs):
    params = dict(image_limit=50, filename_lot="6", portal_lot="7", search_complete=True, send=send)
    params.update(kwargs)
    return md.parse_pages(ON, pages(n), **params)


def test_disabled_without_side_effects():
    with pytest.raises(md.ParseDisabled):
        md.parse_pages({}, [], image_limit=10, filename_lot=None, portal_lot=None,
                       search_complete=True, send=lambda *a: pytest.fail('sent'))


def test_full_multiple_descriptions_and_duplicate_preserved():
    seen = []
    def send(imgs, context):
        seen.append(([p.number for p in imgs], dict(context)))
        return answer([item('5'), item('6'), item('6')])
    result = call(send)
    assert seen == [([1, 2], {'filename_lot': '6', 'portal_lot': '7'})]
    assert [i.data.lot_number for i in result.items] == ['5', '6', '6']
    assert result.complete and result.pages == (1, 2) and result.unparsed_pages == ()
    assert result.selected_attempt == 0 and result.error is None


def test_limiting_page_order_and_incomplete_search():
    result = call(lambda *a: answer([item()]), n=55, image_limit=10)
    assert result.pages == tuple(range(1, 11))
    assert result.unparsed_pages == tuple(range(11, 56))
    assert not result.complete
    result = call(lambda *a: answer([item()]), search_complete=False)
    assert not result.complete


def test_maximum_fifty_even_with_larger_provider_limit():
    result = call(lambda *a: answer([]), n=51, image_limit=99)
    assert len(result.pages) == 50 and result.unparsed_pages == (51,)
    assert not result.complete


def test_bad_images_and_duplicate_or_disordered_pages_are_rejected():
    for values in ([md.PageImage(2, 'data:image/png;base64,WA'), md.PageImage(1, 'data:image/png;base64,WA')],
                   [md.PageImage(1, 'data:image/png;base64,WA')] * 2,
                   [md.PageImage(1, 'https://example.com/private')]):
        with pytest.raises(ValueError):
            md.parse_pages(ON, values, image_limit=10, filename_lot=None, portal_lot=None,
                           search_complete=True, send=lambda *a: pytest.fail('sent'))


def test_two_retries_same_images_and_no_secret_in_attempt():
    calls = []
    def send(imgs, context):
        calls.append(tuple(p.number for p in imgs))
        raise RuntimeError('secret provider response')
    result = call(send, n=3)
    assert calls == [(1, 2, 3)] * 3
    assert not result.items and result.error == 'request_failed'
    assert result.unparsed_pages == (1, 2, 3)
    assert 'secret' not in repr(result)


def test_size_rejection_reduces_once_and_shares_budget():
    calls = []
    def send(imgs, ctx):
        calls.append(tuple(p.number for p in imgs))
        if len(calls) == 1:
            raise md.ImageSizeRejected()
        if len(calls) < 4:
            raise TimeoutError()
        return answer([item()])
    result = call(send, n=10)
    assert calls == [tuple(range(1, 11)), tuple(range(1, 8)), tuple(range(1, 8)), tuple(range(1, 8))]
    assert result.pages == tuple(range(1, 8)) and result.unparsed_pages == (8, 9, 10)
    assert not result.complete


def test_size_rejection_known_limit_and_repeated_size_failure():
    calls = []
    def send(imgs, ctx):
        calls.append(len(imgs))
        raise md.ImageSizeRejected(3)
    result = call(send, n=10)
    assert calls == [10, 3] and result.error == 'size_rejected'
    assert not result.items


def test_single_page_size_failure_cannot_reduce():
    calls = []
    def send(imgs, ctx):
        calls.append(1)
        raise md.ImageSizeRejected()
    assert call(send, n=1).error == 'size_rejected'
    assert len(calls) == 1


def test_invalid_items_preserved_individually_after_retries():
    calls = []
    def send(imgs, ctx):
        calls.append(1)
        return answer([item('5'), {'complete': True, 'data': {}}, item('6')])
    result = call(send)
    assert len(calls) == 3
    assert [x.data.lot_number for x in result.items] == ['5', '6']
    assert not result.complete and result.error == 'invalid_item'
    assert result.selection == 'closure_fallback_uncertain'


def test_recover_only_unambiguous_top_level_prefix():
    first = json.dumps(item('5'))
    text = '{"descriptions": [' + first + ', {"complete": true, "data": '
    attempt = md.read_response(text, [2, 3])
    assert [i.data.lot_number for i in attempt.items] == ['5']
    assert attempt.issue == 'invalid_json'
    assert not md.read_response('{"other":[], "descriptions": [' + first + ', ', [1]).items
    assert not md.read_response('{"descriptions": [' + first, [1]).items
    # Embedded braces inside strings cannot be mistaken for standalone items.
    assert not md.read_response('{"descriptions": ["' + first + '", ', [1]).items


def test_partial_answer_with_incomplete_item_is_usable_but_incomplete():
    result = call(lambda *a: answer([item(complete=False)], complete=False))
    assert len(result.items) == 1 and not result.items[0].complete and not result.complete
    assert len(result.attempts) == 1


def test_empty_incomplete_answer_is_not_a_no_descriptions_result():
    result = call(lambda *a: answer([], complete=False))
    assert len(result.attempts) == 3 and not result.items
    assert result.error == 'no_usable_result'


def test_empty_complete_answer_is_valid_but_not_a_geometry():
    result = call(lambda *a: answer([]))
    assert result.complete and not result.items and result.error is None


def test_choose_most_items_without_merging_attempts():
    responses = iter([answer([item('A'), {}]), answer([item('B'), item('C'), {}]),
                      answer([item('D'), {}])])
    result = call(lambda *a: next(responses))
    assert [i.data.lot_number for i in result.items] == ['B', 'C']
    assert result.selected_attempt == 1 and result.selection == 'most_items'


def test_tie_comparison_and_geometric_fallback():
    values = [answer([item('bad', delta=2), {}]), answer([item('best', delta=.01), {}]),
              answer([item('mid', delta=.5), {}])]
    seen = []
    def choose(imgs, attempts):
        seen.append(([p.number for p in imgs], [a.items[0].data.lot_number for a in attempts]))
        return 2
    result = call(lambda *a: values.pop(0), choose=choose)
    assert result.items[0].data.lot_number == 'mid' and result.selection == 'comparison'
    assert seen == [([1, 2], ['bad', 'best', 'mid'])]
    values = [answer([item('bad', delta=2), {}]), answer([item('best', delta=.01), {}]),
              answer([item('mid', delta=.5), {}])]
    def failed(*a):
        raise TimeoutError()
    result = call(lambda *a: values.pop(0), choose=failed)
    assert result.items[0].data.lot_number == 'best' and result.selection == 'closure_fallback'


def test_geometric_tie_prefers_measurable_then_first():
    values = [answer([item('partial', complete=False), {}], False),
              answer([item('complete', delta=3), {}]),
              answer([item('complete2', delta=3), {}])]
    result = call(lambda *a: values.pop(0))
    assert result.items[0].data.lot_number == 'complete' and result.selected_attempt == 1
    assert result.selection == 'closure_fallback_uncertain'


def test_damaged_item_preserves_only_continuous_valid_prefix_of_sides():
    damaged = item('5')
    damaged['data']['segments'][2]['distance_ft'] = 'unreadable'
    calls = []
    def send(*args):
        calls.append(1)
        return answer([damaged, item('6')])
    result = call(send)
    assert len(calls) == 3
    assert [i.data.lot_number for i in result.items] == ['5', '6']
    assert len(result.items[0].data.segments) == 2
    assert not result.items[0].complete and not result.complete


def test_no_side_before_damage_means_no_contour_or_number():
    damaged = item('5')
    damaged['data']['segments'][0]['distance_ft'] = 'unreadable'
    result = call(lambda *args: answer([damaged, item('6')]))
    assert [i.data.lot_number for i in result.items] == ['6']


def test_empty_side_is_not_silently_dropped_and_later_sides_not_joined():
    broken = item('5')
    broken['data']['segments'].insert(1, None)
    result = call(lambda *args: answer([broken]))
    assert len(result.items) == 1 and not result.items[0].complete
    assert len(result.items[0].data.segments) == 1
    assert result.error == 'invalid_item'


def test_explicit_partial_description_may_contain_only_one_side():
    incomplete = item('5', complete=False)
    incomplete['data']['segments'] = incomplete['data']['segments'][:1]
    result = call(lambda *args: answer([incomplete], complete=False))
    assert len(result.items) == 1 and len(result.items[0].data.segments) == 1
    assert not result.complete and result.error is None


def test_selector_gets_longest_successfully_returned_page_set():
    calls = []
    truncated = '{"descriptions": [' + json.dumps(item('A')) + ', '
    def send(imgs, ctx):
        calls.append([p.number for p in imgs])
        if len(calls) == 1:
            return truncated
        if len(calls) == 2:
            raise md.ImageSizeRejected(1)
        return '{"descriptions": [' + json.dumps(item('B')) + ', '
    seen = []
    def selector(imgs, attempts):
        seen.append(([p.number for p in imgs], [a.pages for a in attempts]))
        return 1
    result = call(send, choose=selector)
    assert seen == [([1, 2], [(1, 2), (1,), (1,)])]
    assert result.pages == (1,) and result.unparsed_pages == (2,)
    assert result.items[0].data.lot_number == 'B'
    assert calls == [[1, 2], [1, 2], [1], [1]]


def test_short_complete_item_keeps_known_sides_as_incomplete_after_retries():
    short = item('5')
    short['data']['segments'] = short['data']['segments'][:2]
    result = call(lambda *a: answer([short]))
    assert len(result.items) == 1 and len(result.items[0].data.segments) == 2
    assert not result.items[0].complete and not result.complete


def test_nonfinite_side_never_produces_nonfinite_geometry():
    value = item('5')
    value['data']['segments'][1]['distance_ft'] = float('inf')
    result = call(lambda *a: answer([value]))
    assert len(result.items) == 1 and not result.items[0].complete
    assert len(result.items[0].data.segments) == 1
    assert all(math.isfinite(x) for x in result.items[0].data.segments[0].vector)


def test_legacy_inferred_tie_is_not_passed_off_as_explicit_model_output():
    value = item('5')
    value['data']['distance_to_corner_ft'] = 55.42
    value['data']['cross_street'] = 'Cross Street'
    result = call(lambda *a: answer([value]))
    assert result.items[0].data.distance_to_corner_ft == pytest.approx(55.42)
    assert result.items[0].data.corner_to_pob_bearing is None


def test_explicit_tie_from_model_is_preserved():
    value = item('5')
    value['data']['distance_to_corner_ft'] = 55.42
    value['data']['cross_street'] = 'Cross Street'
    value['data']['corner_to_pob_bearing'] = dict(ns='N', degrees=90, minutes=0, seconds=0,
                                                  ew='E', distance_ft=55.42)
    result = call(lambda *a: answer([value]))
    assert result.items[0].data.corner_to_pob_bearing is not None
    assert result.items[0].data.corner_to_pob_bearing.distance_ft == pytest.approx(55.42)


def test_nominal_or_closure_solved_bearing_does_not_fabricate_measurable_zero():
    unknown = item('unknown')
    unknown['data']['segments'][0]['has_bearing'] = False
    solved = item('solved')
    solved['data']['segments'][0]['bearing_solved'] = True
    actual = item('actual', delta=3)
    values = [answer([unknown, {}]), answer([solved, {}]), answer([actual, {}])]
    result = call(lambda *a: values.pop(0))
    assert result.items[0].data.lot_number == 'actual'
    assert result.selection == 'closure_fallback'


def test_all_unknown_closures_choose_first_and_record_uncertainty():
    def unknown(lot):
        data = item(lot)
        data['data']['segments'][0]['has_bearing'] = False
        return answer([data, {}])
    values = [unknown('first'), unknown('second'), unknown('third')]
    result = call(lambda *a: values.pop(0))
    assert result.items[0].data.lot_number == 'first'
    assert result.selection == 'closure_fallback_uncertain'


def test_missing_street_name_does_not_discard_a_valid_partial_traverse():
    value = item('5', complete=False)
    value['data'].pop('main_street')
    value['data']['segments'] = value['data']['segments'][:2]
    result = call(lambda *args: answer([value], complete=False))
    assert len(result.items) == 1 and len(result.items[0].data.segments) == 2
    assert result.items[0].data.main_street == '' and not result.complete


@pytest.mark.parametrize('choice,status', [
    (lambda *a: 1, 'selected'),
    (lambda *a: None, 'invalid_selection'),
    (lambda *a: (_ for _ in ()).throw(ValueError('secret')), 'invalid_selection'),
    (lambda *a: (_ for _ in ()).throw(md.ImageSizeRejected()), 'size_rejected'),
    (lambda *a: (_ for _ in ()).throw(TimeoutError('secret')), 'request_failed'),
    (None, 'unavailable'),
])
def test_comparison_diagnostics_are_sanitized_and_describe_attempts(choice, status):
    values = [answer([item('a'), {}]), answer([item('b'), {}]), answer([item('c'), {}])]
    result = call(lambda *a: values.pop(0), choose=choice)
    assert result.comparison.status == status
    assert result.comparison.attempt_indices == (0, 1, 2)
    assert result.comparison.pages == (() if choice is None else (1, 2))
    assert 'secret' not in repr(result.comparison)


def test_failure_diagnostics_include_safe_status_and_elapsed_without_provider_message():
    class ProviderError(Exception):
        status_code = 429
    messages = []
    def send(*a):
        raise ProviderError('SECRET TOKEN AND DOCUMENT')
    result = call(send, progress=messages.append)
    assert len(result.attempts) == 3
    assert all(a.issue == 'request_failed' and a.failure_kind == 'http_429' and
               isinstance(a.elapsed_seconds, float) and a.elapsed_seconds >= 0 for a in result.attempts)
    assert 'HTTP 429' in ' '.join(messages)
    assert 'SECRET' not in repr(result) + ' '.join(messages)


def test_size_rejection_does_not_invent_http_status():
    result = call(lambda *a: (_ for _ in ()).throw(md.ImageSizeRejected()), n=2)
    assert all(a.failure_kind == 'size_rejected' for a in result.attempts)


@pytest.mark.parametrize('wrapper', [
    lambda s: '```json\n' + s + '\n```',
    lambda s: '<think>planning {"ignore": true}</think>\n```json\n' + s + '\n```\nDone.',
    lambda s: 'Unclosed thoughts</think>\nHere is the JSON:\n' + s + '\nThanks!',
])
def test_read_response_accepts_fenced_think_and_prose_wrapped_json(wrapper):
    payload = answer([item('5'), item('6')])
    attempt = md.read_response(wrapper(payload), [1, 2])
    assert attempt.valid_response and attempt.complete
    assert [i.data.lot_number for i in attempt.items] == ['5', '6']


def test_read_response_recovers_truncated_prefix_inside_unclosed_fence_or_preamble():
    first = json.dumps(item('5'))
    truncated = 'Here is the JSON:\n```json\n{"descriptions": [' + first + ', {"complete": true, "data": '
    attempt = md.read_response(truncated, [2, 3])
    assert [i.data.lot_number for i in attempt.items] == ['5']
    assert attempt.issue == 'invalid_json'
    bad = 'Here:\n```json\n{"other":[], "descriptions": [' + first + ', '
    assert not md.read_response(bad, [1]).items


@pytest.mark.parametrize('status', [401, 403])
def test_parse_pages_stops_retrying_on_auth_error(status):
    class AuthError(Exception):
        status_code = status

    calls = []
    def send(*a):
        calls.append(1)
        raise AuthError('SECRET KEY')

    result = call(send)
    assert len(calls) == 1
    assert len(result.attempts) == 1
    assert result.attempts[0].failure_kind == f'http_{status}'

