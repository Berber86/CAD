"""End-to-end GUI orchestration with synthetic PDFs and mocked models/AutoCAD."""
import contextlib
import json
import threading
import types

import pytest

import developer_probability as dp
import multiple_description_runtime as runtime
from test_legal_page_search import make_pdf, response
from test_multiple_description_parse import answer, item
from test_multiple_description_runtime import described
from test_print_draft import fake_autocad  # noqa: F401
import test_gui_smoke as smoke

gui = smoke.gui


def setup_app(gui, monkeypatch, tmp_path, *, parse=None, search=None):
    app = gui.SurveyApp()
    app.multi_pdf_mode_var.set(True)
    app._on_multi_pdf_mode_changed()
    target = runtime.ResolvedTarget('hydra', 'grok-4.7', 'https://example.test', 'fake', 10, 'yes')
    router = runtime.ResolvedTarget('routerai', 'decision', 'https://routerai.test/api/v1', 'fake-router')
    calls = {'parse': 0, 'search': 0, 'targets': []}
    def prepare(*args, **kwargs):
        calls['targets'].append('prepared')
        return target, router.api_key
    def sender(images, context):
        calls['parse'] += 1
        if parse is not None:
            return parse(images, context)
        return answer([described('5', 55.42), described('6', 100.11)])
    def searcher(base, key, model, state, assets, questions):
        calls['search'] += 1
        assert base == router.base_url and key == router.api_key
        if search is not None:
            return search(base, key, model, state, assets, questions)
        return response(questions, {1: .9, 2: .8}, 'two')
    @contextlib.contextmanager
    def callbacks(target_, cfg):
        assert target_ is target
        yield sender, None
    monkeypatch.setattr(runtime, 'prepare_runtime', prepare)
    monkeypatch.setattr(runtime, 'provider_target', lambda *a, **k: router)
    monkeypatch.setattr(runtime, 'open_runtime_callbacks', callbacks)
    monkeypatch.setattr(dp, 'post_routerai', searcher)
    monkeypatch.setattr(app, '_pdf_set_worker', lambda *a, **k: pytest.fail('legacy worker'))
    monkeypatch.setattr(gui.SurveyApp, '_print_draft_plans', gui._real_print_draft_plans)
    return app, calls


def submit(app, gui, tmp_path):
    our, neighbor = tmp_path / 'OUR 6.pdf', tmp_path / 'LOT 5.pdf'
    make_pdf(our, 2)
    make_pdf(neighbor, 2)
    items, errors = app._make_pdf_set_items([our, neighbor])
    assert not errors
    assert [entry.is_main for entry in items] == [True, False]
    app._submit_pdf_set(items, scene_mode='update', fresh=True)
    smoke._wait_for_gui_worker(app)
    return our, neighbor


def test_background_pdf_to_scene_reuse_and_fake_autocad(gui, tmp_path, monkeypatch, fake_autocad):
    app, calls = setup_app(gui, monkeypatch, tmp_path)
    our, neighbor = submit(app, gui, tmp_path)
    assert len(app._scene_plans()) == 4 and [len(i.multi_outcomes) for i in app.pdf_set] == [2, 2]
    assert [p.is_neighbor for _, p in app._scene_plans()] == [False, False, True, True]
    assert app._ready_source_signature and not app._input_incomplete
    assert calls['parse'] == 2 and calls['search'] == 2
    app._start(parse_only=True)  # ordinary repeat reuses even an incomplete cached result
    smoke._wait_for_gui_worker(app)
    assert calls['parse'] == 2 and calls['search'] == 2
    assert len(app._scene_plans()) == 4

    assert app._source_signature(mode='📄 PDF') == app._ready_source_signature, (app._source_signature(mode='📄 PDF'), app._ready_source_signature, app._input_incomplete)
    builder, doc = fake_autocad(None)
    monkeypatch.setattr(gui, 'sys', types.SimpleNamespace(platform='win32'))
    monkeypatch.setattr(gui, 'AcadBuilder', lambda cfg: builder)
    from tkinter import messagebox
    monkeypatch.setattr(messagebox, 'askyesno', lambda *a, **k: pytest.fail('unexpected warning'))
    app._start_draw_scene()
    smoke._wait_for_gui_worker(app)
    texts = [t.TextString for t in doc.ModelSpace.texts]
    assert texts.count('LOT 6') == 2 and texts.count('LOT 5') == 2, app.status_var.get()
    assert 'Отрисовка завершена' in app.status_var.get()
    assert calls['parse'] == 2 and calls['search'] == 2


def test_one_failed_pdf_does_not_hide_successful_contours(gui, tmp_path, monkeypatch):
    def parse(images, context):
        if context['filename_lot'] == '5':
            raise TimeoutError('sensitive error body')
        return answer([described('5', 55), described('6', 100)])
    app, calls = setup_app(gui, monkeypatch, tmp_path, parse=parse)
    our, neighbor = submit(app, gui, tmp_path)
    assert calls['parse'] == 4  # successful file once, failed file exactly three attempts
    assert [d.status for d in app._multi_scene_handoff.documents] == ['parsed', 'parse_failed']
    assert len(app._scene_plans()) == 2 and not app._input_incomplete
    diagnostics = (tmp_path / (neighbor.name + '.multi-parse.json')).read_text()
    assert 'sensitive error body' not in diagnostics
    assert json.loads(diagnostics)['runs'][-1]['error'] == 'request_failed'


def test_no_candidates_does_not_hide_contours_from_other_file(gui, tmp_path, monkeypatch):
    searches = iter([response_value for response_value in ('yes', 'no')])
    def search(base, key, model, state, assets, questions):
        return response(questions, {1: .9, 2: .8} if next(searches) == 'yes' else {}, 'zero')
    app, calls = setup_app(gui, monkeypatch, tmp_path, search=search)
    submit(app, gui, tmp_path)
    assert [d.status for d in app._multi_scene_handoff.documents] == ['parsed', 'no_candidates']
    assert len(app._scene_plans()) == 2 and not app._input_incomplete


def test_file_mutation_after_parse_blocks_drawing_without_ai(gui, tmp_path, monkeypatch):
    app, calls = setup_app(gui, monkeypatch, tmp_path)
    our, neighbor = submit(app, gui, tmp_path)
    with our.open('ab') as handle:
        handle.write(b'changed')
    monkeypatch.setattr(app, '_start_background', lambda *a, **k: pytest.fail('stale scene sent to AutoCAD'))
    app._start_draw_scene()
    assert app._input_incomplete and app._ready_source_signature is None
    assert calls['parse'] == 2


def test_reset_in_flight_stops_retries_and_does_not_publish_scene(gui, tmp_path, monkeypatch):
    started, resume, finished = threading.Event(), threading.Event(), threading.Event()
    def parse(images, context):
        started.set()
        assert resume.wait(5)
        finished.set()
        raise TimeoutError('request already sent')
    app, calls = setup_app(gui, monkeypatch, tmp_path, parse=parse)
    our = tmp_path / 'OUR 6.pdf'
    make_pdf(our, 2)
    items, _ = app._make_pdf_set_items([our])
    items[0].is_main = True
    app._submit_pdf_set(items, scene_mode='update', fresh=True)
    assert started.wait(5)
    app._reset_session()
    resume.set()
    assert finished.wait(5)
    # Join the actual worker rather than sleeping/polling; it may still be
    # unwinding the cancellation check after the in-flight response.
    for thread in threading.enumerate():
        if thread.name.startswith('Thread-') and thread is not threading.current_thread():
            thread.join(timeout=5)
    app._drain_events()
    assert calls['parse'] == 1 and not app._busy and not app._scene_plans()
    assert not (tmp_path / (our.name + '.multi-parse.json')).exists()


def test_incomplete_result_reused_after_portal_change(gui, tmp_path, monkeypatch):
    value = item('5', complete=False)
    value['data']['segments'] = value['data']['segments'][:2]
    contexts = []
    def parse(images, context):
        contexts.append(dict(context))
        return answer([value], complete=False)
    app, calls = setup_app(gui, monkeypatch, tmp_path, parse=parse)
    our, neighbor = submit(app, gui, tmp_path)
    assert all(not d.complete for d in app._multi_scene_handoff.documents)
    assert len(app._scene_plans()) == 2 and all(p.open_traverse for _, p in app._scene_plans())
    passport = our.with_suffix('.json')
    data = json.loads(passport.read_text())
    data.setdefault('identity', {}).setdefault('lot', []).append({'source': 'portal', 'value': '7'})
    passport.write_text(json.dumps(data))
    app._start(parse_only=True)
    smoke._wait_for_gui_worker(app)
    assert calls['parse'] == 2 and calls['search'] == 2
    diagnostic = json.loads((tmp_path / (our.name + '.multi-parse.json')).read_text())
    assert diagnostic['portal_observations'][-1]['lot'] == '7'
    assert contexts[0]['portal_lot'] is None


def test_runtime_failure_is_noncacheable_and_does_not_leak_provider_error(gui, tmp_path, monkeypatch):
    app, calls = setup_app(gui, monkeypatch, tmp_path)
    def failing(*args, **kwargs):
        raise runtime.RuntimeUnavailable('private key or provider message')
    monkeypatch.setattr(runtime, 'prepare_runtime', failing)
    our = tmp_path / 'OUR 6.pdf'
    make_pdf(our, 2)
    items, _ = app._make_pdf_set_items([our])
    items[0].is_main = True
    app._submit_pdf_set(items, scene_mode='update', fresh=True)
    smoke._wait_for_gui_worker(app)
    assert not app._busy and app._input_incomplete and not app._scene_plans()
    data = (tmp_path / (our.name + '.multi-parse.json')).read_text()
    assert 'private key' not in data and json.loads(data)['runs'][-1]['error'] == 'runtime_unavailable'
    assert 'private key' not in app.status_var.get()
    assert calls['parse'] == 0 and calls['search'] == 0


def test_add_to_multi_scene_reuses_old_documents_and_keeps_every_contour(gui, tmp_path, monkeypatch):
    app, calls = setup_app(gui, monkeypatch, tmp_path)
    our, neighbor = submit(app, gui, tmp_path)
    assert app._multi_scene_can_add()
    third = tmp_path / 'LOT 3.pdf'
    make_pdf(third, 2)
    items, _ = app._make_pdf_set_items([third])
    app._submit_pdf_set(items, scene_mode='add', fresh=True)
    smoke._wait_for_gui_worker(app)
    assert [i.path for i in app.pdf_set] == [our, neighbor, third]
    assert len(app._scene_plans()) == 6 and calls['parse'] == 3 and calls['search'] == 3
    assert [len(i.multi_outcomes) for i in app.pdf_set] == [2, 2, 2]


def test_add_rejects_stale_or_unrelated_scene_without_losing_it(gui, tmp_path, monkeypatch):
    app, calls = setup_app(gui, monkeypatch, tmp_path)
    submit(app, gui, tmp_path)
    before = app._scene_plans()
    app._ready_source_signature = None
    third = tmp_path / 'LOT 3.pdf'
    make_pdf(third, 2)
    items, _ = app._make_pdf_set_items([third])
    app._submit_pdf_set(items, scene_mode='add', fresh=True)
    assert not app._busy and app._scene_plans() == before and calls['parse'] == 2


def test_switch_is_session_only_and_clears_old_pdf_scene_without_requests(gui, tmp_path, monkeypatch):
    app, calls = setup_app(gui, monkeypatch, tmp_path)
    submit(app, gui, tmp_path)
    original = (calls['parse'], calls['search'])
    assert app._multi_pdf_enabled() and not app.config_data['multiple_description_search']['enabled']
    app.multi_pdf_mode_var.set(False)
    app._on_multi_pdf_mode_changed()
    assert not app._multi_pdf_enabled() and app._multi_scene_handoff is None
    assert not app._scene_plans() and len(app.pdf_set) == 2
    assert all(not i.multi_outcomes and i.outcome is None and i.parsed_key is None for i in app.pdf_set)
    assert (calls['parse'], calls['search']) == original
    app.multi_pdf_mode_var.set(True)
    app._on_multi_pdf_mode_changed()
    saved = []
    monkeypatch.setattr(gui, 'save_config', lambda config: saved.append(config.copy()))
    assert app._persist_ai_preferences()
    assert saved and not saved[0]['multiple_description_search']['enabled']
    assert app._multi_pdf_enabled() and (calls['parse'], calls['search']) == original
    assert 'ИИ' in app.parse_button.cget('text') and 'бесплатно' not in app.parse_button.cget('text')
    app._busy = True
    app.multi_pdf_mode_var.set(False)
    app._on_multi_pdf_mode_changed()
    assert app._multi_pdf_enabled() and app.multi_pdf_mode_var.get()
    app._busy = False


def test_inflight_parse_reports_progress_in_simple_journal(gui, tmp_path, monkeypatch):
    started, resume = threading.Event(), threading.Event()
    def parse(images, context):
        started.set()
        assert resume.wait(5)
        return answer([described('6', 55)])
    app, calls = setup_app(gui, monkeypatch, tmp_path, parse=parse)
    path = tmp_path / 'OUR 6.pdf'
    make_pdf(path, 2)
    items, errors = app._make_pdf_set_items([path])
    assert not errors
    items[0].is_main = True
    app._submit_pdf_set(items, scene_mode='update', fresh=True)
    try:
        assert started.wait(5)
        app._drain_events()
        visible = app._log_view_text()
        assert 'Поиск: запрос' in visible and 'Разбор: запрос' in visible
        assert 'ожидаем ответ' in visible.lower()
    finally:
        resume.set()
    smoke._wait_for_gui_worker(app)
    assert calls['parse'] == 1 and 'описаний 1' in app.log_text()


def test_new_session_never_enables_experiment_from_persisted_flag(gui, monkeypatch):
    import copy
    cfg = gui.load_config()
    cfg['multiple_description_search']['enabled'] = True
    monkeypatch.setattr(gui, 'load_config', lambda: copy.deepcopy(cfg))
    app = gui.SurveyApp()
    assert not app._multi_pdf_enabled() and not app.multi_pdf_mode_var.get()
    assert app.config_data['multiple_description_search']['enabled'] is True


def test_parse_http_status_and_timing_appear_in_visible_log_without_response_body(gui, tmp_path, monkeypatch):
    class Error(Exception):
        status_code = 429
    def fail(*a):
        raise Error('SECRET provider response')
    app, calls = setup_app(gui, monkeypatch, tmp_path, parse=fail)
    our = tmp_path / 'OUR 6.pdf'
    make_pdf(our, 2)
    items, _ = app._make_pdf_set_items([our])
    items[0].is_main = True
    app._submit_pdf_set(items, scene_mode='update', fresh=True)
    smoke._wait_for_gui_worker(app)
    assert calls['parse'] == 3
    assert 'HTTP 429' in app._log_view_text()
    assert 'SECRET' not in app.log_text()
    data = json.loads((tmp_path / (our.name + '.multi-parse.json')).read_text())
    attempts = data['runs'][-1]['attempts']
    assert len(attempts) == 3 and all(a['failure_kind'] == 'http_429' and a['elapsed_seconds'] >= 0 for a in attempts)


def test_unlocated_scene_warns_user_without_changing_cached_requests(gui, tmp_path, monkeypatch):
    app, calls = setup_app(gui, monkeypatch, tmp_path,
                           parse=lambda *a: answer([described('5', 55, tie=False),
                                                    described('6', 100, tie=False)]))
    submit(app, gui, tmp_path)
    assert 'Отдельно размещено: 4' in app.status_var.get()
    assert calls['parse'] == 2 and calls['search'] == 2
    app._start(parse_only=True)
    smoke._wait_for_gui_worker(app)
    assert calls['parse'] == 2 and calls['search'] == 2
    assert 'Отдельно размещено: 4' in app.status_var.get()


def test_deed_row_is_placed_automatically_without_prompt(gui, tmp_path, monkeypatch):
    from tkinter import messagebox
    from test_multiple_description_drawing import deed_example_items
    payload = [{'complete': i.complete, 'data': i.data.model_dump(mode='json')} for i in deed_example_items()]
    app, calls = setup_app(gui, monkeypatch, tmp_path, parse=lambda *a: answer(payload))
    monkeypatch.setattr(messagebox, 'askyesno', lambda *a, **k: pytest.fail('unexpected decision'))
    our = tmp_path / 'OUR 6 NEW.pdf'
    make_pdf(our, 2)
    jobs, _ = app._make_pdf_set_items([our])
    jobs[0].is_main = True
    app._submit_pdf_set(jobs, scene_mode='update', fresh=True)
    smoke._wait_for_gui_worker(app)
    assert calls['parse'] == 1 and calls['search'] == 1
    view = app._multi_scene_handoff
    assert all(d.placement_known for d in view.drawable)
    assert all(d.outcome.data.corner_to_pob_bearing is None for d in view.drawable)
    assert view.drawable[1].outcome.data.cross_street == 'Lindbergh Avenue'
    from multiple_description_drawing import make_scene_print_drafts
    draft = make_scene_print_drafts(view.scene, builder_cfg=app.config_data)
    assert draft.diagnostic is None and len(draft.plans) == 2
    assert any(t.kind == 'cross-street' for p in view.scene.plans for t in p.plan.items)
    assert app._ready_source_signature and not app._input_incomplete


def test_source_changed_after_installation_disables_drawing(gui, tmp_path, monkeypatch):
    from test_multiple_description_drawing import deed_example_items
    payload = [{'complete': i.complete, 'data': i.data.model_dump(mode='json')} for i in deed_example_items()]
    app, _ = setup_app(gui, monkeypatch, tmp_path, parse=lambda *a: answer(payload))
    our = tmp_path / 'OUR 6 NEW.pdf'
    make_pdf(our, 2)
    original = app._install_multi_scene
    def mutate_after_install(view):
        original(view)
        with our.open('ab') as file:
            file.write(b'changed')
    monkeypatch.setattr(app, '_install_multi_scene', mutate_after_install)
    jobs, _ = app._make_pdf_set_items([our])
    jobs[0].is_main = True
    app._submit_pdf_set(jobs, scene_mode='update', fresh=True)
    smoke._wait_for_gui_worker(app)
    assert app._ready_source_signature is None and app._input_incomplete
    assert 'изменился' in app.status_var.get()


def test_explicit_cache_restore_is_offline_and_automatically_positions_supported_row(gui, tmp_path, monkeypatch):
    from tkinter import filedialog, messagebox
    import file_metadata
    import multiple_description_cache as cache
    import multiple_description_parse as parse
    from test_multiple_description_drawing import deed_example_items
    from test_multiple_description_parse import pages
    pdf = tmp_path / 'OUR 6 NEW.pdf'
    make_pdf(pdf, 2)
    payload = [{'complete': i.complete, 'data': i.data.model_dump(mode='json')} for i in deed_example_items()]
    parse_result = parse.parse_pages({'multiple_description_search': {'enabled': True}}, pages(2),
                                     image_limit=50, filename_lot='6', portal_lot=None, search_complete=True,
                                     send=lambda *a: answer(payload))
    context = cache.cache_context(pdf_sha256=file_metadata._digest(pdf), pages=pages(2), image_settings={},
                                  provider='openrouter', model='nvidia/nemotron-3.5-super-vl:free',
                                  filename_lot='6', search_complete=True, image_limit=50)
    # Actual 4.80.3–4.80.6 prompt hash, as in the user's saved cache.
    context['instruction_sha256'] = '8d57be14196feb1e104ac5c60219f40e071bcc9d6007e81a4fc5a008bd4a4769'
    context['response_format'] = 'multiple-legal-descriptions-v3'
    context['description_schema_sha256'] = '5ef4cdb9afa8ad6ff57784f183da8f0496a5763af91d7b673ebec74670d61bcc'
    cache.save_result(pdf, context, parse_result, portal_lot=None)
    original_bytes = cache.diagnostics_path(pdf).read_bytes()
    app, calls = setup_app(gui, monkeypatch, tmp_path)
    monkeypatch.setattr(runtime, 'prepare_runtime', lambda *a, **k: pytest.fail('network/key access'))
    monkeypatch.setattr(runtime, 'open_runtime_callbacks', lambda *a, **k: pytest.fail('network access'))
    monkeypatch.setattr(filedialog, 'askopenfilename', lambda **k: str(pdf))
    monkeypatch.setattr(messagebox, 'askyesno', lambda *a, **k: pytest.fail('unexpected decision'))
    app._restore_multi_cache()
    smoke._wait_for_gui_worker(app)
    assert calls['parse'] == calls['search'] == 0
    assert cache.diagnostics_path(pdf).read_bytes() == original_bytes
    assert app._ready_source_signature and not app._input_incomplete
    assert len(app._scene_plans()) == 2
    assert all(d.placement_known for d in app._multi_scene_handoff.drawable)
    assert app._multi_scene_handoff.documents[0].cached
    assert app._multi_scene_handoff.drawable[0].outcome.provider == 'openrouter'


def test_offline_restore_missing_cache_never_falls_back_to_network(gui, tmp_path, monkeypatch):
    from tkinter import filedialog
    pdf = tmp_path / 'OUR 6 NEW.pdf'
    make_pdf(pdf, 2)
    app, calls = setup_app(gui, monkeypatch, tmp_path)
    monkeypatch.setattr(filedialog, 'askopenfilename', lambda **k: str(pdf))
    monkeypatch.setattr(runtime, 'prepare_runtime', lambda *a, **k: pytest.fail('network/key access'))
    app._restore_multi_cache()
    smoke._wait_for_gui_worker(app)
    assert calls['parse'] == calls['search'] == 0
    assert app._ready_source_signature is None and app._input_incomplete
    assert not cache_path_exists(pdf)


def cache_path_exists(pdf):
    import multiple_description_cache as cache
    return cache.diagnostics_path(pdf).exists()
