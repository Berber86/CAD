"""Tests of worker -> UI handoff, using only synthetic PDFs and mocked calls."""
from dataclasses import replace

import pytest

import multiple_description_handoff as handoff
import multiple_description_drawing as drawing
import multiple_description_runtime as runtime
from test_legal_page_search import make_pdf, response
from test_multiple_description_parse import answer
from test_multiple_description_runtime import config, described, searcher


def batch(tmp_path):
    our, neighbor = tmp_path / 'OUR 6.pdf', tmp_path / 'LOT 5.pdf'
    make_pdf(our, 2)
    make_pdf(neighbor, 2)
    target = runtime.ResolvedTarget('hydra', 'grok-4.7', 'https://example.test', 'test', 10)
    result = runtime.run_batch([runtime.FileJob(our, True), runtime.FileJob(neighbor, False)], config(),
                               target=target, search_api_key='key', search_send=searcher, choose=None,
                               send=lambda *a: answer([described('5', 55), described('6', 100)]))
    return result


def test_handoff_preserves_all_descriptions_source_roles_and_duplicate_plans(tmp_path):
    result = batch(tmp_path)
    view = handoff.build_handoff(result)
    assert view.all_processed and len(view.documents) == 2
    assert [doc.is_main for doc in view.documents] == [True, False]
    assert [len(doc.descriptions) for doc in view.documents] == [2, 2]
    assert [d.description_index for d in view.descriptions] == [0, 1, 2, 3]
    assert [d.ordinal for d in view.descriptions] == [1, 2, 1, 2]
    assert [d.plan.is_neighbor for d in view.drawable] == [False, False, True, True]
    assert [d.outcome.data.lot_number for d in view.descriptions] == ['5', '6', '5', '6']
    assert all(d.plan is result.scene.plans[d.description_index].plan for d in view.drawable)
    assert view.scene is result.scene


def test_planning_failure_is_not_merged_or_relabelled_as_empty(tmp_path):
    result = batch(tmp_path)
    missing = result.scene.plans[1]
    scene = drawing.SceneResult(tuple(p for p in result.scene.plans if p is not missing),
                                (drawing.PlanFailure(missing.source, missing.description_index, 'planning_failed'),))
    view = handoff.build_handoff(replace(result, scene=scene))
    assert len(view.descriptions) == 4 and len(view.drawable) == 3
    assert view.descriptions[1].plan is None
    assert view.descriptions[1].diagnostic == 'planning_failed'
    assert view.descriptions[1].outcome.data.lot_number == '6'


def test_incomplete_failed_and_empty_document_statuses_do_not_collapse(tmp_path):
    first, second = tmp_path / 'OUR 6.pdf', tmp_path / 'LOT 5.pdf'
    make_pdf(first, 2)
    make_pdf(second, 2)
    target = runtime.ResolvedTarget('hydra', 'grok', 'https://example.test', 'test', 10)
    def send(images, context):
        if context['filename_lot'] == '6':
            return answer([described('6', 55)], complete=False)
        raise ValueError('request failure')
    result = runtime.run_batch([runtime.FileJob(first, True), runtime.FileJob(second, False)], config(),
                               target=target, search_api_key='key', search_send=searcher, choose=None, send=send)
    view = handoff.build_handoff(result)
    assert [d.status for d in view.documents] == ['parsed', 'parse_failed']
    assert not view.documents[0].complete and not view.documents[1].complete
    assert not view.all_processed and len(view.drawable) == 1


def test_search_no_candidates_is_distinct_from_failed_search(tmp_path):
    first = tmp_path / 'OUR 6.pdf'
    make_pdf(first, 2)
    target = runtime.ResolvedTarget('hydra', 'grok', 'https://example.test', 'test', 10)
    result = runtime.run_batch([runtime.FileJob(first, True)], config(), target=target, search_api_key='key',
                               search_send=lambda base, key, model, state, assets, qs: response(qs, {}, 'zero'),
                               choose=None, send=lambda *a: pytest.fail('must not parse'))
    view = handoff.build_handoff(result)
    assert view.documents[0].status == 'no_candidates' and view.documents[0].complete
    assert view.all_processed and not view.descriptions


def test_handoff_rejects_stale_or_damaged_scene_and_cancelled_batch(tmp_path):
    result = batch(tmp_path)
    plan = result.scene.plans[0]
    with pytest.raises(handoff.InvalidHandoff):
        handoff.build_handoff(replace(result, cancelled=True))
    with pytest.raises(handoff.InvalidHandoff):
        handoff.build_handoff(replace(result, scene=drawing.SceneResult(result.scene.plans[1:], ())))
    bad = replace(plan, is_main=False)
    with pytest.raises(handoff.InvalidHandoff):
        handoff.build_handoff(replace(result, scene=drawing.SceneResult((bad,) + result.scene.plans[1:], ())))
    with pytest.raises(handoff.InvalidHandoff):
        handoff.build_handoff(replace(result, scene=drawing.SceneResult(result.scene.plans + (plan,), ())))


def test_handoff_rejects_inconsistent_file_status(tmp_path):
    result = batch(tmp_path)
    invalid_file = replace(result.files[0], status='no_candidates')
    with pytest.raises(handoff.InvalidHandoff, match='status mismatch'):
        handoff.build_handoff(replace(result, files=(invalid_file,) + result.files[1:]))
    with pytest.raises(handoff.InvalidHandoff, match='missing document result'):
        handoff.build_handoff(replace(result, files=(replace(result.files[0], result=None),) + result.files[1:]))


def test_expected_jobs_prevent_applying_a_result_to_changed_roles_or_sources(tmp_path):
    result = batch(tmp_path)
    jobs = tuple(f.job for f in result.files)
    assert len(handoff.build_handoff(result, expected_jobs=jobs).documents) == 2
    with pytest.raises(handoff.InvalidHandoff, match='stale document jobs'):
        handoff.build_handoff(result, expected_jobs=(replace(jobs[0], is_main=False), jobs[1]))
    with pytest.raises(handoff.InvalidHandoff, match='stale document jobs'):
        handoff.build_handoff(result, expected_jobs=jobs[:1])


def test_handoff_rejects_plan_with_changed_recorded_side(tmp_path):
    result = batch(tmp_path)
    first = result.scene.plans[0]
    plan = first.plan
    bad_line = replace(plan.boundary_lines[0], end=(plan.boundary_lines[0].end[0] + 3, plan.boundary_lines[0].end[1]))
    bad_plan = replace(plan, boundary_lines=[bad_line, *plan.boundary_lines[1:]])
    scene = drawing.SceneResult((replace(first, plan=bad_plan),) + result.scene.plans[1:], ())
    with pytest.raises(handoff.InvalidHandoff, match='plan sides mismatch'):
        handoff.build_handoff(replace(result, scene=scene))


def test_handoff_rejects_relocated_side_even_if_its_vector_is_unchanged(tmp_path):
    result = batch(tmp_path)
    first = result.scene.plans[0]
    plan = first.plan
    edge = plan.boundary_lines[1]
    shifted = replace(edge, start=(edge.start[0] + 2, edge.start[1]),
                      end=(edge.end[0] + 2, edge.end[1]))
    damaged = replace(plan, boundary_lines=[plan.boundary_lines[0], shifted, *plan.boundary_lines[2:]])
    scene = drawing.SceneResult((replace(first, plan=damaged),) + result.scene.plans[1:], ())
    with pytest.raises(handoff.InvalidHandoff, match='plan sides mismatch'):
        handoff.build_handoff(replace(result, scene=scene))
