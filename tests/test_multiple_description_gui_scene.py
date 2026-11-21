"""Exercise the experimental GUI scene with synthetic batches, no network/CAD."""
from dataclasses import replace

import pytest

import multiple_description_drawing as drawing
import multiple_description_handoff as handoff
from pdf_set import PdfSetItem
from test_multiple_description_handoff import batch
from test_multiple_description_parse import answer, item
from test_multiple_description_runtime import config, described, searcher
import multiple_description_runtime as runtime
import test_gui_smoke as smoke
from test_print_draft import fake_autocad  # noqa: F401

gui = smoke.gui


def install(gui, tmp_path):
    app = gui.SurveyApp()
    app.config_data['multiple_description_search']['enabled'] = True
    result = batch(tmp_path)
    app.pdf_set = [PdfSetItem(path=file.job.path, page_texts=['', ''], is_main=file.job.is_main)
                   for file in result.files]
    view = handoff.build_handoff(result, expected_jobs=[file.job for file in result.files])
    app._install_multi_scene(view)
    return app, result, view


def test_scene_handoff_shows_every_main_and_neighbor_and_draft(gui, tmp_path):
    app, result, view = install(gui, tmp_path)
    plans = app._scene_plans()
    assert len(plans) == 4
    assert [p for _, p in plans] == [d.plan for d in view.drawable]
    assert [p.is_neighbor for _, p in plans] == [False, False, True, True]
    assert [len(item.multi_outcomes) for item in app.pdf_set] == [2, 2]
    assert len(app._preview_neighbor_plans) == 2
    labels = [args['text'] for typ, _, args in app.preview_canvas.items if typ == 'text' and 'text' in args]
    assert labels.count('LOT 6') == 2 and labels.count('LOT 5') == 2
    # Preview should contain exactly the planned segments, without a legacy
    # guessed line from (0,0), automatic joins, or artificial closing edges.
    lines = [v for v in app.preview_canvas.items if v[0] == 'line']
    assert len(lines) == sum(len(p.lines) + len(p.boundary_lines) for _, p in plans)
    drafts = gui._real_print_draft_plans(app)
    assert len(drafts) == 2
    assert all(p.print_draft and not p.is_neighbor for _, p in drafts)
    assert all(not any(t.kind in {'main-lot-label', 'neighbor-label'} for t in p.items) for _, p in drafts)
    assert app._input_incomplete and app._ready_source_signature is None  # worker is still guarded


def test_neighbor_clear_keeps_all_our_contours_and_no_stale_print_drafts(gui, tmp_path):
    app, result, view = install(gui, tmp_path)
    app._clear_neighbors_scene()
    assert len(app.pdf_set) == 1 and app.pdf_set[0].is_main
    assert len(app._scene_plans()) == 2
    assert all(not p.is_neighbor for _, p in app._scene_plans())
    assert len(gui._real_print_draft_plans(app)) == 2
    labels = [args['text'] for typ, _, args in app.preview_canvas.items if typ == 'text' and 'text' in args]
    assert labels.count('LOT 6') == 2 and 'LOT 5' not in labels
    app._clear_preview_scene()
    assert app._multi_scene_handoff is None and not app._scene_plans()


def test_stale_scene_handoff_does_not_replace_existing_gui_scene(gui, tmp_path):
    app, result, view = install(gui, tmp_path)
    original = app._scene_plans()
    app.pdf_set[0].is_main = False
    with pytest.raises(handoff.InvalidHandoff, match='stale'):
        app._install_multi_scene(view)
    assert app._scene_plans() == original and app._multi_scene_handoff is view


def test_scene_disables_legacy_auto_slide_and_neighbor_verification(gui, tmp_path, monkeypatch):
    app, result, view = install(gui, tmp_path)
    before = [(d.plan.pob, tuple((edge.start, edge.end) for edge in d.plan.boundary_lines)) for d in view.drawable]
    monkeypatch.setattr(gui, 'slide_off_overlap', lambda *a, **k: pytest.fail('guessed adjacency'))
    monkeypatch.setattr(gui, 'verify_with_neighbors', lambda *a, **k: pytest.fail('legacy verification'))
    app._slide_overlapping_neighbours()
    assert app._recompute_neighbor_verification() is None
    assert before == [(d.plan.pob, tuple((edge.start, edge.end) for edge in d.plan.boundary_lines)) for d in view.drawable]


def test_preview_partial_chain_has_no_invented_closing_line(gui, tmp_path):
    app = gui.SurveyApp()
    app.config_data['multiple_description_search']['enabled'] = True
    original = batch(tmp_path)
    # Reuse the synthetic PDF inputs; each mock request returns a genuinely
    # incomplete description, not a closed description with a replaced plan.
    value = item('5', complete=False)
    value['data']['segments'] = value['data']['segments'][:2]
    target = runtime.ResolvedTarget('hydra', 'grok', 'https://example.test', 'test', 10)
    result = runtime.run_batch([f.job for f in original.files], config(), target=target,
                               search_api_key='key', search_send=searcher, choose=None, force_parse=True,
                               send=lambda *a: answer([value], complete=False))
    app.pdf_set = [PdfSetItem(path=f.job.path, page_texts=['', ''], is_main=f.job.is_main) for f in result.files]
    view = handoff.build_handoff(result)
    app._install_multi_scene(view)
    preview_lines = [v for v in app.preview_canvas.items if v[0] == 'line']
    assert len(preview_lines) == sum(len(d.plan.lines) + len(d.plan.boundary_lines) for d in view.drawable)
    assert view.drawable[0].plan.open_traverse and len(view.drawable[0].plan.boundary_lines) == 2


def test_changed_pdf_rejects_scene_without_replacing_current_state(gui, tmp_path):
    app, result, view = install(gui, tmp_path)
    old_scene = app._scene_plans()
    path = result.files[0].job.path
    with path.open('ab') as f:
        f.write(b'changed')
    with pytest.raises(handoff.InvalidHandoff, match='source changed'):
        app._install_multi_scene(view)
    assert app._scene_plans() == old_scene and app._multi_scene_handoff is view


def test_disabled_switch_does_not_install_scene(gui, tmp_path):
    result = batch(tmp_path)
    app = gui.SurveyApp()
    app.pdf_set = [PdfSetItem(path=f.job.path, page_texts=['', ''], is_main=f.job.is_main) for f in result.files]
    with pytest.raises(handoff.InvalidHandoff, match='disabled'):
        app._install_multi_scene(handoff.build_handoff(result))
    assert app._multi_scene_handoff is None


def test_gui_scene_draft_and_all_main_plans_draw_in_fake_autocad(gui, tmp_path, fake_autocad):
    app, result, view = install(gui, tmp_path)
    builder, doc = fake_autocad(None)
    drafts = gui._real_print_draft_plans(app)
    for _, plan in drafts:
        builder.draw_plan(plan)
    draft_texts = [t.TextString for t in doc.ModelSpace.texts]
    assert 'LOT 6' not in draft_texts and 'LOT 5' not in draft_texts
    for _, plan in app._scene_plans():
        builder.draw_plan(plan)
    all_texts = [t.TextString for t in doc.ModelSpace.texts]
    assert all_texts.count('LOT 6') == 2 and all_texts.count('LOT 5') == 2
    assert len(drafts) == 2 and len(app._scene_plans()) == 4


def test_multi_preview_includes_street_extent_and_counterclockwise_arc(gui, tmp_path):
    import math
    from types import SimpleNamespace
    from acad_builder import ArcItem, LineItem
    app, _, view = install(gui, tmp_path)
    original = view.drawable[0].plan
    plan = replace(original, lines=[LineItem((0, 0), (500, 0), 'main-street-row-1')],
                   arcs=[ArcItem((100, 100), 10, 3 * math.pi / 2, math.pi / 2)])
    app._multi_scene_handoff = SimpleNamespace(drawable=[SimpleNamespace(plan=plan)])
    canvas = app.preview_canvas
    canvas.delete('all')
    app._draw_multi_preview_canvas(canvas, 460, 280)
    cx, cy, scale, width, height = app._last_world_transform
    assert 34 <= width / 2 + (500 - cx) * scale <= width - 34
    arcs = [v for v in canvas.items if v[0] == 'arc']
    assert len(arcs) == 1
    assert arcs[0][2]['start'] == pytest.approx(270)
    assert arcs[0][2]['extent'] == pytest.approx(180)
    assert arcs[0][2]['style'] == 'arc'
