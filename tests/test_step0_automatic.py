"""Offline integration of the opt-in Staten Island address → full PDF → multi pipeline."""
from pathlib import Path
import time

import pytest
import pymupdf
import deed_pages

import test_gui_smoke as smoke
import test_step0_intake_flow as intake
from test_multiple_description_gui_flow import setup_app

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


def test_auto_stops_when_our_lot_is_missing(gui, tmp_path, monkeypatch):
    # Полная остановка в автопотоке допустима только в одном случае:
    # не собран наш собственный участок — дальше идти просто не с чем.
    app = gui.SurveyApp()
    app.step0_address_var.set("Address")
    app.step0_folder_var.set(str(tmp_path))
    fake_portal(app, monkeypatch)
    actual_collect = gui.deed_intake.collect_deeds
    def collect(**kw):
        kw.update(fetch_list=intake._fetch(fail_lots=[32]), downloader=intake._downloader(tmp_path))
        return actual_collect(**kw)
    monkeypatch.setattr(gui.deed_intake, "collect_deeds", collect)
    monkeypatch.setattr(app, "_submit_pdf_set", lambda *a, **kw: pytest.fail("nothing to parse"))
    app._start_step0_auto()
    app._finish_step0_portal_lookup("ok", parcel(gui))
    wait_all(app)
    assert app._step0_auto_stage is None and "наш участок не собран" in app.status_var.get()


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
