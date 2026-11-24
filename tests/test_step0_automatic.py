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
        "OUR LOT 32 NEW.pdf": True, "OUR LOT 32 OLD.pdf": True, "LOT 30 NEW.pdf": False}
    for item in app.pdf_set:
        with pymupdf.open(str(item.path)) as pdf:
            assert len(pdf) == 3
    assert calls['search'] == 3 and calls['parse'] == 3
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


def test_auto_stops_on_partial_collection(gui, tmp_path, monkeypatch):
    app = gui.SurveyApp()
    app.step0_address_var.set("Address")
    app.step0_folder_var.set(str(tmp_path))
    fake_portal(app, monkeypatch)
    actual_collect = gui.deed_intake.collect_deeds
    def collect(**kw):
        kw.update(fetch_list=intake._fetch(fail_lots=[30]), downloader=intake._downloader(tmp_path))
        return actual_collect(**kw)
    monkeypatch.setattr(gui.deed_intake, "collect_deeds", collect)
    monkeypatch.setattr(app, "_submit_pdf_set", lambda *a, **kw: pytest.fail("partial run"))
    app._start_step0_auto()
    app._finish_step0_portal_lookup("ok", parcel(gui))
    wait_all(app)
    assert app._step0_auto_stage is None and "неполный" in app.status_var.get()


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


def test_auto_low_confidence_and_fast_mode_do_not_collect(gui, monkeypatch):
    app = gui.SurveyApp()
    app.step0_address_var.set("Address")
    fake_portal(app, monkeypatch)
    monkeypatch.setattr(app, "_start_step0_intake", lambda: pytest.fail("uncertain map"))
    app.step0_fast_mode_var.set(True)
    app._start_step0_auto()
    assert app._step0_auto_stage is None and "быстрый" in app.status_var.get().lower()
    app.step0_fast_mode_var.set(False)
    app._start_step0_auto()
    no_neighbours = gui.nyc_portal.ParcelLookup(
        block="6323", lot="32", borough="Staten Island", left_neighbour=None,
        right_neighbour=None, confidence="low", notes="", map_image=Path("map.png"),
        usage=None,
    )
    app._finish_step0_portal_lookup("ok", no_neighbours)
    assert app._step0_auto_stage is None and "уверенность" in app.status_var.get().lower()


def test_auto_low_confidence_with_known_neighbours_continues(gui, monkeypatch):
    """Как 3868 Fresh Pond Road: стороны могли перепутаться, но оба соседа названы."""
    app = gui.SurveyApp()
    app.step0_address_var.set("Address")
    fake_portal(app, monkeypatch)
    started = []
    monkeypatch.setattr(app, "_start_step0_intake", lambda: started.append(True))
    app._start_step0_auto()
    app._finish_step0_portal_lookup("ok", parcel(gui, confidence="low"))
    assert started == [True]
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
