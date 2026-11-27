"""Offline integration of the opt-in Staten Island address → full PDF → multi pipeline."""
from pathlib import Path
import time

import pytest
import pymupdf
import deed_pages

import test_gui_smoke as smoke
import test_step0_intake_flow as intake
from test_multiple_description_gui_flow import setup_app
from test_print_draft import fake_autocad  # noqa: F401

gui = smoke.gui


def parcel(gui, borough="Staten Island", confidence="high"):
    return gui.nyc_portal.ParcelLookup(
        block="6323", lot="32", borough=borough, left_neighbour="30", right_neighbour=None,
        confidence=confidence, notes="", map_image=Path("map.png"), usage=None,
    )


def fake_portal(app, monkeypatch):
    def start():
        app._step0_lookup_address = str(app.step0_address_var.get()).strip()
        app._step0_portal_running = True
    monkeypatch.setattr(app, "_start_step0_portal_lookup", start)


def wait_all(app):
    for _ in range(1000):
        app._drain_events()
        if not app._step0_running and not app._busy:
            return
        time.sleep(.01)
    pytest.fail("automatic pipeline did not finish")


def test_auto_complete_uses_collected_full_pdfs_only_and_never_draws(gui, tmp_path, monkeypatch):
    app, calls = setup_app(gui, monkeypatch, tmp_path)
    app.multi_pdf_mode_var.set(False)
    app._on_multi_pdf_mode_changed()
    app.step0_address_var.set("Staten Island test address")
    app.step0_folder_var.set(str(tmp_path / "docs"))
    fake_portal(app, monkeypatch)
    actual_collect = gui.deed_intake.collect_deeds
    seen = []
    def collect(**kwargs):
        seen.append(kwargs)
        kwargs.update(fetch_list=intake._fetch(), downloader=intake._downloader(tmp_path),
                      page_finder=lambda _: pytest.fail("legacy page finder"))
        return actual_collect(**kwargs)
    monkeypatch.setattr(gui.deed_intake, "collect_deeds", collect)
    monkeypatch.setattr(deed_pages, "trim_pdf", lambda *a, **k: pytest.fail("legacy crop"))
    monkeypatch.setattr(app, "_start_draw_scene", lambda: pytest.fail("automatic draw"))
    app._start_step0_auto()
    assert app._step0_auto_stage == "portal"
    app._finish_step0_portal_lookup("ok", parcel(gui))
    wait_all(app)
    assert seen[0]["borough"] == "Staten Island" and seen[0]["search_pages"] is False
    assert {item.path.name: item.is_main for item in app.pdf_set} == {
        "OUR LOT 32 NEW.pdf": True, "LOT 30 NEW.pdf": False}
    for item in app.pdf_set:
        with pymupdf.open(str(item.path)) as pdf:
            assert len(pdf) == 3
    # Разбор по мере скачивания: OUR NEW и сосед разобран один раз во время
    # сбора; OUR OLD только скачан и вообще не входил в набор.
    assert calls['search'] == 2 and calls['parse'] == 2
    assert app._step0_auto_stage is None and app._multi_scene_handoff is not None


@pytest.mark.parametrize("borough", [None, "Staten Island Queens"])
def test_auto_never_collects_other_or_unknown_borough(gui, tmp_path, monkeypatch, borough):
    app = gui.SurveyApp()
    app.step0_address_var.set("Address")
    fake_portal(app, monkeypatch)
    monkeypatch.setattr(gui.deed_intake, "collect_deeds", lambda **kw: pytest.fail("wrong site"))
    app._start_step0_auto()
    app._finish_step0_portal_lookup("ok", parcel(gui, borough))
    assert not app._step0_running and app._step0_auto_stage is None
    assert "район" in app.status_var.get().lower()


def test_manual_collection_rejects_unknown_borough(gui, tmp_path, monkeypatch):
    app = gui.SurveyApp()
    smoke._fill_step0(app, folder=tmp_path)
    app.step0_borough_var.set("Westchester")
    monkeypatch.setattr(gui.deed_intake, "collect_deeds", lambda **kw: pytest.fail("wrong site"))
    app._start_step0_intake()
    assert not app._step0_running and "ACRIS" in app.status_var.get()


def test_auto_continues_when_only_a_neighbour_is_missing(gui, tmp_path, monkeypatch):
    # Живой случай 3876 (24.11.2026): у соседа свежий CRFN, которого ещё нет
    # в NYC Open Data, — document_id не получить. Наш участок собран, поэтому
    # задание не встаёт: недостача соседа — предупреждение, автопоток идёт дальше.
    app = gui.SurveyApp()
    app.step0_address_var.set("Address")
    app.step0_folder_var.set(str(tmp_path))
    fake_portal(app, monkeypatch)
    actual_collect = gui.deed_intake.collect_deeds
    def collect(**kw):
        kw.update(fetch_list=intake._fetch(fail_lots=[30]), downloader=intake._downloader(tmp_path))
        return actual_collect(**kw)
    monkeypatch.setattr(gui.deed_intake, "collect_deeds", collect)
    submitted = []
    def submit(items, **kw):
        submitted.append(items)
        app._busy = True
    monkeypatch.setattr(app, "_submit_pdf_set", submit)
    app._start_step0_auto()
    app._finish_step0_portal_lookup("ok", parcel(gui))
    for _ in range(1000):
        app._drain_events()
        if app._step0_auto_stage == "parse":
            break
        time.sleep(.01)
    assert submitted and app._step0_auto_stage == "parse"
    assert any("не хватает" in line for _lvl, line in app._step0_lines)


def test_auto_draws_a_neighbour_even_when_our_lot_is_missing(gui, tmp_path, monkeypatch):
    # Указание пользователя (5.32): автопоток обязан дойти до черчения, если
    # можно начертить хотя бы одного соседа, пусть и криво. Несобранный наш
    # участок — предупреждение, а не остановка (до 5.32 было наоборот).
    app = gui.SurveyApp()
    app.step0_address_var.set("Address")
    app.step0_folder_var.set(str(tmp_path))
    fake_portal(app, monkeypatch)
    actual_collect = gui.deed_intake.collect_deeds
    def collect(**kw):
        kw.update(fetch_list=intake._fetch(fail_lots=[32]), downloader=intake._downloader(tmp_path))
        return actual_collect(**kw)
    monkeypatch.setattr(gui.deed_intake, "collect_deeds", collect)
    submitted = []
    def submit(items, **kw):
        submitted.append(items)
        app._busy = True
    monkeypatch.setattr(app, "_submit_pdf_set", submit)
    app._start_step0_auto()
    app._finish_step0_portal_lookup("ok", parcel(gui))
    for _ in range(1000):
        app._drain_events()
        if app._step0_auto_stage == "parse":
            break
        time.sleep(.01)
    assert submitted and app._step0_auto_stage == "parse"
    assert [(item.path.name, item.is_main) for item in submitted[0]] == [("LOT 30 NEW.pdf", False)]
    assert any("хотя бы сосед" in line for _lvl, line in app._step0_lines)


def test_auto_stops_only_when_nothing_is_collected(gui, tmp_path, monkeypatch):
    # Полная остановка допустима в одном случае: не собралось вообще
    # ничего — начертить нечего.
    app = gui.SurveyApp()
    app.step0_address_var.set("Address")
    app.step0_folder_var.set(str(tmp_path))
    fake_portal(app, monkeypatch)
    actual_collect = gui.deed_intake.collect_deeds
    def collect(**kw):
        kw.update(fetch_list=intake._fetch(fail_lots=[32, 30]), downloader=intake._downloader(tmp_path))
        return actual_collect(**kw)
    monkeypatch.setattr(gui.deed_intake, "collect_deeds", collect)
    monkeypatch.setattr(app, "_submit_pdf_set", lambda *a, **kw: pytest.fail("nothing to parse"))
    app._start_step0_auto()
    app._finish_step0_portal_lookup("ok", parcel(gui))
    wait_all(app)
    assert app._step0_auto_stage is None and "нечего" in app.status_var.get()


def test_changed_address_and_stop_during_portal_prevent_collection(gui, monkeypatch):
    app = gui.SurveyApp()
    fake_portal(app, monkeypatch)
    monkeypatch.setattr(app, "_start_step0_intake", lambda: pytest.fail("stale lookup"))
    app.step0_address_var.set("Old")
    app._start_step0_auto()
    app.step0_address_var.set("New")
    app._finish_step0_portal_lookup("ok", parcel(gui))
    assert app._step0_auto_stage is None and app.step0_block_var.get() == ""
    app._start_step0_auto()
    app._stop_step0_intake()
    app._finish_step0_portal_lookup("ok", parcel(gui))
    assert app._step0_auto_stage is None and app.step0_block_var.get() == ""


def test_auto_fast_mode_does_not_collect_but_low_confidence_never_stops(gui, monkeypatch):
    app = gui.SurveyApp()
    app.step0_address_var.set("Address")
    fake_portal(app, monkeypatch)
    started = []
    monkeypatch.setattr(app, "_start_step0_intake", lambda: started.append(True))
    app.step0_fast_mode_var.set(True)
    app._start_step0_auto()
    assert app._step0_auto_stage is None and "быстрый" in app.status_var.get().lower()
    app.step0_fast_mode_var.set(False)

    # Полной остановки из-за соседей в автопотоке нет: даже когда не назван никто.
    no_neighbours = gui.nyc_portal.ParcelLookup(
        block="6323", lot="32", borough="Staten Island", left_neighbour=None,
        right_neighbour=None, confidence="low", notes="", map_image=Path("map.png"),
        usage=None,
    )
    app._start_step0_auto()
    app._finish_step0_portal_lookup("ok", no_neighbours)
    assert started == [True]

    # Как 3868 Fresh Pond Road: стороны могли перепутаться, но оба соседа названы.
    app._start_step0_auto()
    app._finish_step0_portal_lookup("ok", parcel(gui, confidence="low"))
    assert started == [True, True]
    assert app.step0_neighbours_var.get() == "30"


def test_auto_stops_if_identity_changes_during_collection(gui, tmp_path, monkeypatch):
    app = gui.SurveyApp()
    app.step0_address_var.set("Address")
    app.step0_folder_var.set(str(tmp_path))
    fake_portal(app, monkeypatch)
    monkeypatch.setattr(app, "_start_step0_intake", lambda: setattr(app, "_step0_running", True))
    monkeypatch.setattr(app, "_submit_pdf_set", lambda *a, **kw: pytest.fail("stale identity"))
    app._start_step0_auto()
    app._finish_step0_portal_lookup("ok", parcel(gui))
    app.step0_lot_var.set("99")
    app._finish_step0("ok", gui.deed_intake.IntakeOutcome(folder=tmp_path))
    assert app._step0_auto_stage is None and "изменились" in app.status_var.get()


def test_reset_discards_old_portal_result_and_releases_lock(gui, monkeypatch):
    app = gui.SurveyApp()
    app.step0_address_var.set("Address")
    fake_portal(app, monkeypatch)
    app._start_step0_auto()
    old_generation = app._generation
    app._reset_session()
    app._post("step0_portal_done", ("ok", parcel(gui)), generation=old_generation)
    app._drain_events()
    assert not app._step0_portal_running and app._step0_auto_stage is None
    assert app.step0_block_var.get() == ""


def test_reset_releases_lock_if_completion_already_in_queue(gui, monkeypatch):
    app = gui.SurveyApp()
    app.step0_address_var.set("Address")
    fake_portal(app, monkeypatch)
    app._start_step0_auto()
    app._post("step0_portal_done", ("ok", parcel(gui)), generation=app._generation)
    app._reset_session()
    assert not app._step0_portal_running and app._step0_auto_stage is None
    assert app.step0_block_var.get() == ""


# ------------------------------------- ранний сбор: наш участок до соседей


def _drain_until(app, predicate, tries=3000):
    for _ in range(tries):
        app._drain_events()
        if predicate():
            return
        time.sleep(.01)
    pytest.fail("automatic pipeline did not reach the expected state")


def _collect_spy(gui, monkeypatch, tmp_path, seen, **fetch_kwargs):
    actual_collect = gui.deed_intake.collect_deeds

    def collect(**kwargs):
        seen.append(dict(kwargs))
        kwargs.update(fetch_list=intake._fetch(**fetch_kwargs),
                      downloader=intake._downloader(tmp_path),
                      page_finder=intake._finder())
        return actual_collect(**kwargs)

    monkeypatch.setattr(gui.deed_intake, "collect_deeds", collect)


def _submit_spy(app, monkeypatch, submitted):
    def submit(items, **kwargs):
        submitted.append(items)
        app._busy = True

    monkeypatch.setattr(app, "_submit_pdf_set", submit)


def test_auto_downloads_our_lot_before_neighbours_are_known(gui, tmp_path, monkeypatch):
    app = gui.SurveyApp()
    app.step0_address_var.set("Staten Island test address")
    app.step0_folder_var.set(str(tmp_path))
    fake_portal(app, monkeypatch)
    seen, submitted = [], []
    _collect_spy(gui, monkeypatch, tmp_path, seen)
    _submit_spy(app, monkeypatch, submitted)
    app._start_step0_auto()
    app._handle_step0_portal_identified(
        ("Staten Island test address", "Staten Island", "6323", "32", "ocr", "map.png"))
    assert app._step0_running  # наш участок качается, нейронки ещё не ответили
    _drain_until(app, lambda: not app._step0_running
                 and app._step0_our_outcome is not None)
    assert len(seen) == 1 and seen[0]["neighbours"] == [] and seen[0]["include_our"] is True
    app._finish_step0_portal_lookup("ok", parcel(gui))
    _drain_until(app, lambda: bool(submitted))
    assert len(seen) == 2 and seen[1]["include_our"] is False
    assert list(seen[1]["neighbours"]) == [30]
    assert {item.path.name for item in submitted[0]} == {
        "OUR LOT 32 NEW.pdf", "LOT 30 NEW.pdf"}


def test_auto_collects_neighbours_after_our_when_answer_comes_mid_download(
        gui, tmp_path, monkeypatch):
    import threading

    app = gui.SurveyApp()
    app.step0_address_var.set("Staten Island test address")
    app.step0_folder_var.set(str(tmp_path))
    fake_portal(app, monkeypatch)
    seen, submitted = [], []
    gate, entered = threading.Event(), threading.Event()
    actual_collect = gui.deed_intake.collect_deeds

    def collect(**kwargs):
        seen.append(dict(kwargs))
        if len(seen) == 1:
            entered.set()
            gate.wait(15)
        kwargs.update(fetch_list=intake._fetch(),
                      downloader=intake._downloader(tmp_path),
                      page_finder=intake._finder())
        return actual_collect(**kwargs)

    monkeypatch.setattr(gui.deed_intake, "collect_deeds", collect)
    _submit_spy(app, monkeypatch, submitted)
    app._start_step0_auto()
    app._handle_step0_portal_identified(
        ("Staten Island test address", "Staten Island", "6323", "32", "ocr", "map.png"))
    assert entered.wait(10)
    app._finish_step0_portal_lookup("ok", parcel(gui))
    assert len(seen) == 1  # соседи известны, но второй проход ждёт наш участок
    gate.set()
    _drain_until(app, lambda: bool(submitted))
    assert len(seen) == 2 and seen[1]["include_our"] is False
    assert {item.path.name for item in submitted[0]} == {
        "OUR LOT 32 NEW.pdf", "LOT 30 NEW.pdf"}


def test_auto_continues_without_neighbours_when_neighbour_answer_fails(
        gui, tmp_path, monkeypatch):
    app = gui.SurveyApp()
    app.step0_address_var.set("Staten Island test address")
    app.step0_folder_var.set(str(tmp_path))
    fake_portal(app, monkeypatch)
    seen, submitted = [], []
    _collect_spy(gui, monkeypatch, tmp_path, seen)
    _submit_spy(app, monkeypatch, submitted)
    app._start_step0_auto()
    app._handle_step0_portal_identified(
        ("Staten Island test address", "Staten Island", "6323", "32", "ocr", "map.png"))
    _drain_until(app, lambda: not app._step0_running
                 and app._step0_our_outcome is not None)
    app._finish_step0_portal_lookup("error", "PortalLookupError: обе модели молчат")
    _drain_until(app, lambda: bool(submitted))
    assert len(seen) == 1
    assert {item.path.name for item in submitted[0]} == {"OUR LOT 32 NEW.pdf"}
    assert any("без соседей" in line for _level, line in app._step0_lines)


def test_auto_early_our_failure_still_draws_the_neighbour(gui, tmp_path, monkeypatch):
    # Ранний сбор: наш участок не скачался до ответа про соседей — автопоток
    # всё равно докачивает соседа и уходит на разбор с ним одним.
    app = gui.SurveyApp()
    app.step0_address_var.set("Staten Island test address")
    app.step0_folder_var.set(str(tmp_path))
    fake_portal(app, monkeypatch)
    seen, submitted = [], []
    _collect_spy(gui, monkeypatch, tmp_path, seen, fail_lots=[32])
    _submit_spy(app, monkeypatch, submitted)
    app._start_step0_auto()
    app._handle_step0_portal_identified(
        ("Staten Island test address", "Staten Island", "6323", "32", "ocr", "map.png"))
    _drain_until(app, lambda: not app._step0_running
                 and app._step0_our_outcome is not None)
    assert not app._step0_our_outcome.ok
    app._finish_step0_portal_lookup("ok", parcel(gui))
    _drain_until(app, lambda: bool(submitted))
    assert [(item.path.name, item.is_main) for item in submitted[0]] == [
        ("LOT 30 NEW.pdf", False)]


def test_auto_early_plus_neighbours_keeps_single_stream_and_never_reruns_step1(
        gui, tmp_path, monkeypatch):
    import threading

    our_parse_entered = threading.Event()
    release_our_parse = threading.Event()
    phase2_collect_started = threading.Event()

    def slow_parse(images, context):
        if context.get("filename_lot") == "32":
            our_parse_entered.set()
            assert release_our_parse.wait(5)
        from test_multiple_description_parse import answer
        from test_multiple_description_runtime import described
        return answer([described(context.get("filename_lot") or "32", 55.42)], complete=True)

    app, calls = setup_app(gui, monkeypatch, tmp_path, parse=slow_parse)
    app.step0_address_var.set("Staten Island test address")
    app.step0_folder_var.set(str(tmp_path / "docs"))
    fake_portal(app, monkeypatch)
    seen = []
    actual_collect = gui.deed_intake.collect_deeds

    def collect(**kwargs):
        seen.append(dict(kwargs))
        if len(seen) == 2:
            phase2_collect_started.set()
        kwargs.update(fetch_list=intake._fetch(),
                      downloader=intake._downloader(tmp_path),
                      page_finder=lambda _: pytest.fail("legacy page finder"))
        return actual_collect(**kwargs)

    monkeypatch.setattr(gui.deed_intake, "collect_deeds", collect)
    monkeypatch.setattr(app, "_submit_pdf_set", lambda *a, **kw: pytest.fail("must not rerun Step 1"))
    app._start_step0_auto()
    app._handle_step0_portal_identified(
        ("Staten Island test address", "Staten Island", "6323", "32", "ocr", "map.png"))
    # Phase 1 AI parsing starts in background while Phase 1 download finishes without blocking Phase 2.
    assert our_parse_entered.wait(5)
    _drain_until(app, lambda: not app._step0_running and app._step0_our_outcome is not None)
    app._finish_step0_portal_lookup("ok", parcel(gui))
    # Phase 2 starts downloading immediately while Phase 1 AI is STILL blocked on release_our_parse!
    assert phase2_collect_started.wait(5)
    release_our_parse.set()
    wait_all(app)
    assert len(seen) == 2
    assert {item.path.name: item.is_main for item in app.pdf_set} == {
        "OUR LOT 32 NEW.pdf": True, "LOT 30 NEW.pdf": False}
    assert calls["search"] == 2 and calls["parse"] == 2
    assert app._step0_auto_stage is None and app._multi_scene_handoff is not None
    assert not any("не подошёл" in line for _lvl, line in app._step0_lines)


def test_auto_early_without_neighbours_waits_for_running_stream_without_rerun(
        gui, tmp_path, monkeypatch):
    import threading

    our_parse_entered = threading.Event()
    release_our_parse = threading.Event()

    def slow_parse(images, context):
        our_parse_entered.set()
        assert release_our_parse.wait(5)
        from test_multiple_description_parse import answer
        from test_multiple_description_runtime import described
        return answer([described("32", 55.42)], complete=True)

    app, calls = setup_app(gui, monkeypatch, tmp_path, parse=slow_parse)
    app.step0_address_var.set("Staten Island test address")
    app.step0_folder_var.set(str(tmp_path / "docs"))
    fake_portal(app, monkeypatch)
    seen = []
    _collect_spy(gui, monkeypatch, tmp_path, seen)
    monkeypatch.setattr(app, "_submit_pdf_set", lambda *a, **kw: pytest.fail("must not rerun Step 1"))
    app._start_step0_auto()
    app._handle_step0_portal_identified(
        ("Staten Island test address", "Staten Island", "6323", "32", "ocr", "map.png"))
    assert our_parse_entered.wait(5)
    _drain_until(app, lambda: not app._step0_running and app._step0_our_outcome is not None)
    app._finish_step0_portal_lookup("error", "PortalLookupError: failed")
    assert app._step0_auto_stage == "parse" and app._busy
    release_our_parse.set()
    wait_all(app)
    assert calls["search"] == 1 and calls["parse"] == 1
    assert app._multi_scene_handoff is not None and len(app._scene_plans()) == 1


def test_auto_falls_back_to_our_old_when_our_new_fails_and_writes_old_label_to_dwg(
        gui, tmp_path, monkeypatch, fake_autocad):
    import types
    from test_multiple_description_parse import answer
    from test_multiple_description_runtime import described

    parse_calls = []

    def parse(images, context):
        parse_calls.append(context.get("filename_lot"))
        # First call for OUR NEW fails badly (empty descriptions); OUR OLD & neighbour succeed!
        if len(parse_calls) == 1:
            return answer([], complete=True)
        return answer([described(context.get("filename_lot") or "30", 55.42)], complete=True)

    app, calls = setup_app(gui, monkeypatch, tmp_path, parse=parse)
    app.step0_address_var.set("Staten Island test address")
    app.step0_folder_var.set(str(tmp_path / "docs"))
    fake_portal(app, monkeypatch)
    _collect_spy(gui, monkeypatch, tmp_path, [])
    app._start_step0_auto()
    app._finish_step0_portal_lookup("ok", parcel(gui))
    wait_all(app)
    assert {item.path.name: item.is_main for item in app.pdf_set} == {
        "OUR LOT 32 OLD.pdf": True, "LOT 30 NEW.pdf": False}
    # Neighbour LOT 30 NEW.pdf came from cache on the fallback pass!
    assert len(parse_calls) == 3  # OUR NEW (1) + LOT 30 (2) + OUR OLD (3)
    assert app._auto_dwg_context is not None

    builder, doc = fake_autocad(None)
    doc.SaveAs = lambda path: Path(path).write_bytes(b"DWG simulated")
    monkeypatch.setattr(gui, "AcadBuilder", lambda cfg: builder)
    monkeypatch.setattr(gui.sys, "platform", "win32")
    app._start_draw_scene()
    smoke._wait_for_gui_worker(app)
    texts = [t.TextString for t in doc.ModelSpace.texts]
    assert "OLD 32" in texts and "LOT 30" in texts
    dwg, = (tmp_path / "docs").glob("lot_32_*.dwg")
    warnings_text = gui.auto_dwg_save.warnings_path_for(dwg).read_text(encoding="utf-8")
    assert "OUR OLD" in warnings_text and "OLD 32" in warnings_text


def test_auto_promotes_our_old_when_our_new_missing_from_download(gui, tmp_path, monkeypatch):
    app, calls = setup_app(gui, monkeypatch, tmp_path)
    app.step0_address_var.set("Staten Island test address")
    app.step0_folder_var.set(str(tmp_path / "docs"))
    fake_portal(app, monkeypatch)

    actual_collect = gui.deed_intake.collect_deeds
    def collect(**kwargs):
        kwargs.update(fetch_list=intake._fetch(),
                      downloader=intake._downloader(tmp_path, fail_ids=(str(intake.OUR_NEW),)),
                      page_finder=lambda _: pytest.fail("legacy page finder"))
        return actual_collect(**kwargs)
    monkeypatch.setattr(gui.deed_intake, "collect_deeds", collect)
    app._start_step0_auto()
    app._finish_step0_portal_lookup("ok", parcel(gui))
    wait_all(app)
    assert {item.path.name: item.is_main for item in app.pdf_set} == {
        "OUR LOT 32 OLD.pdf": True, "LOT 30 NEW.pdf": False}
    assert any("OLD 32" in line for _lvl, line in app._step0_lines)


def test_two_phase_autoflow_never_logs_false_missing_our_or_mode_prompt(
        gui, tmp_path, monkeypatch, fake_autocad):
    app, _calls = setup_app(gui, monkeypatch, tmp_path)
    app.step0_address_var.set("Staten Island test address")
    app.step0_folder_var.set(str(tmp_path / "docs"))
    fake_portal(app, monkeypatch)
    _collect_spy(gui, monkeypatch, tmp_path, [])
    seen_logs: list[str] = []
    orig_log = app._log_line
    monkeypatch.setattr(app, "_log_line", lambda text, **kw: (seen_logs.append(text), orig_log(text, **kw)))

    app._start_step0_auto()
    app._handle_step0_portal_identified(
        ("Staten Island test address", "Staten Island", "6323", "32", "ocr", "map.png"))
    _drain_until(app, lambda: not app._step0_running and app._step0_our_outcome is not None)
    app._finish_step0_portal_lookup("ok", parcel(gui))
    wait_all(app)

    assert not any("наш участок не собран" in line for line in seen_logs)
    assert not any("Для обработки выберите PDF или запустите разбор заново" in line for line in seen_logs)
    assert not any("наш участок не собран" in text for _lvl, text in app._step0_lines)

    builder, doc = fake_autocad(None)
    doc.SaveAs = lambda path: Path(path).write_bytes(b"DWG simulated")
    monkeypatch.setattr(gui, "AcadBuilder", lambda cfg: builder)
    monkeypatch.setattr(gui.sys, "platform", "win32")
    seen_logs.clear()
    app._start_draw_scene()
    smoke._wait_for_gui_worker(app)
    dwg_saved_lines = [line for line in seen_logs if "DWG сохранён:" in line]
    assert len(dwg_saved_lines) == 1


