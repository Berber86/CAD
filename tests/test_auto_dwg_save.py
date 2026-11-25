"""Safe DWG persistence: no AutoCAD, network, or real user drawings required."""
from pathlib import Path
import threading

import pytest

import auto_dwg_save
import test_gui_smoke as smoke
from test_multiple_description_gui_flow import setup_app
from test_multiple_description_parse import answer
from test_multiple_description_runtime import described
from test_print_draft import fake_autocad  # noqa: F401
import test_step0_intake_flow as intake
from test_step0_automatic import fake_portal, parcel, wait_all

gui = smoke.gui


class Doc:
    def __init__(self):
        self.calls = []
    def SaveAs(self, path):
        self.calls.append(path)
        Path(path).write_bytes(b"DWG mock")


def test_unique_dwg_files_in_descriptions_folder_preserve_prior_files(tmp_path):
    doc = Doc()
    old = tmp_path / "lot_30.dwg"
    old.write_bytes(b"old")
    first = auto_dwg_save.save_new_dwg(doc, tmp_path, "0030")
    second = auto_dwg_save.save_new_dwg(doc, tmp_path, "30")
    assert first != second and first.parent == second.parent == tmp_path
    assert first.name.startswith("lot_30_") and first.suffix == ".dwg"
    assert old.read_bytes() == b"old" and first.read_bytes() == second.read_bytes() == b"DWG mock"


@pytest.mark.parametrize("lot", ["", "0", "-1", "../30", "30 and 31", "30/31"])
def test_unknown_lot_still_saves_without_using_unsafe_name(tmp_path, lot):
    result = auto_dwg_save.save_new_dwg(Doc(), tmp_path, lot)
    assert result.parent == tmp_path and result.name.startswith("lot_unknown_")


def test_existing_dwg_and_reservation_are_never_overwritten(tmp_path, monkeypatch):
    from types import SimpleNamespace
    ids = iter(["existing", "locked", "fresh"])
    monkeypatch.setattr(auto_dwg_save, "uuid4", lambda: SimpleNamespace(hex=next(ids)))
    target = tmp_path / "lot_30_existing.dwg"
    target.write_bytes(b"untouched")
    lock = tmp_path / ".lot_30_locked.dwg.reserve"
    lock.write_bytes(b"someone else's lock")
    doc = Doc()
    result = auto_dwg_save.save_new_dwg(doc, tmp_path, "30")
    assert target.read_bytes() == b"untouched" and lock.read_bytes() == b"someone else's lock"
    assert result.name == "lot_30_fresh.dwg" and len(doc.calls) == 1
    assert not (tmp_path / ".lot_30_existing.dwg.reserve").exists()
    assert not (tmp_path / ".lot_30_fresh.dwg.reserve").exists()


def test_failed_save_reports_location_and_preserves_partial_data(tmp_path):
    class Failed:
        def SaveAs(self, path):
            Path(path).write_bytes(b"partial")
            raise RuntimeError("disk problem")
    with pytest.raises(auto_dwg_save.AutoDwgSaveError, match="disk problem") as err:
        auto_dwg_save.save_new_dwg(Failed(), tmp_path, "30")
    partial, = tmp_path.glob("lot_30_*.dwg")
    assert partial.read_bytes() == b"partial" and str(partial) in str(err.value)


def test_save_confirmation_requires_a_nonempty_file(tmp_path):
    class NoFile:
        def SaveAs(self, path):
            pass
    with pytest.raises(auto_dwg_save.AutoDwgSaveError, match="не создал"):
        auto_dwg_save.save_new_dwg(NoFile(), tmp_path, "30")


def test_pinned_com_session_keeps_the_document_across_draws(fake_autocad, monkeypatch):
    import acad_builder
    builder, doc = fake_autocad(None)
    calls = []
    builder._com_owned = True
    builder._com_thread_id = threading.get_ident()
    monkeypatch.setattr(acad_builder, "_co_uninitialize", lambda: calls.append("release"))
    with builder.drawing_session() as (app, current):
        assert current is doc
        app.doc = Doc()  # user switched the active AutoCAD tab
        assert builder.connect()[1] is doc
        assert calls == []
    assert calls == ["release"] and builder._doc is None


def prepare_auto(gui, tmp_path, monkeypatch, *, parsed_lot="32", complete=True):
    app, calls = setup_app(gui, monkeypatch, tmp_path,
                           parse=lambda images, context: answer([described(parsed_lot, 55.42)], complete=complete))
    app.multi_pdf_mode_var.set(False)
    app._on_multi_pdf_mode_changed()
    app.step0_address_var.set("Address")
    app.step0_folder_var.set(str(tmp_path / "project" / "01_описания"))
    fake_portal(app, monkeypatch)
    actual = gui.deed_intake.collect_deeds
    def collect(**kwargs):
        kwargs.update(fetch_list=intake._fetch(), downloader=intake._downloader(tmp_path))
        return actual(**kwargs)
    monkeypatch.setattr(gui.deed_intake, "collect_deeds", collect)
    app._start_step0_auto()
    app._finish_step0_portal_lookup("ok", parcel(gui))
    wait_all(app)
    return app


def test_auto_pipeline_manually_draws_then_saves_same_document(gui, tmp_path, monkeypatch, fake_autocad):
    app = prepare_auto(gui, tmp_path, monkeypatch)
    assert app._auto_dwg_context
    builder, doc = fake_autocad(None)
    doc.SaveAs = lambda path: Path(path).write_bytes(b"DWG simulated")
    monkeypatch.setattr(gui, "AcadBuilder", lambda cfg: builder)
    monkeypatch.setattr(gui.sys, "platform", "win32")
    app._start_draw_scene()
    smoke._wait_for_gui_worker(app)
    result, = (tmp_path / "project" / "01_описания").glob("lot_32_*.dwg")
    assert result.read_bytes() == b"DWG simulated" and len(doc.ModelSpace.lines) > 0
    assert str(result) in app.status_var.get() and "сохранён" in app.status_var.get()
    # Автосохранение одноразовое: контекст автопотока израсходован первой
    # отрисовкой. Повторное ручное построение DWG само не сохраняет —
    # Николай: после автопотока человек достраивает лоты вручную и не ждёт,
    # что программа без спроса плодит файлы в папке автопотока.
    assert app._auto_dwg_context is None
    app._start_draw_scene()
    smoke._wait_for_gui_worker(app)
    assert len(list((tmp_path / "project" / "01_описания").glob("lot_32_*.dwg"))) == 1


def test_full_reset_drops_auto_dwg_context(gui, tmp_path, monkeypatch):
    app = prepare_auto(gui, tmp_path, monkeypatch)
    assert app._auto_dwg_context
    app._reset_session()
    assert app._auto_dwg_context is None


def test_mismatched_lot_still_saves_using_portal_number(gui, tmp_path, monkeypatch, fake_autocad):
    app = prepare_auto(gui, tmp_path, monkeypatch, parsed_lot="5")
    builder, doc = fake_autocad(None)
    doc.SaveAs = lambda path: Path(path).write_bytes(b"DWG simulated")
    monkeypatch.setattr(gui, "AcadBuilder", lambda cfg: builder)
    monkeypatch.setattr(gui.sys, "platform", "win32")
    app._start_draw_scene()
    smoke._wait_for_gui_worker(app)
    files = list((tmp_path / "project" / "01_описания").glob("lot_32_*.dwg"))
    assert len(files) == 1 and "сохранён" in app.status_var.get()


def test_drawing_failure_still_saves_partial_document(gui, tmp_path, monkeypatch, fake_autocad):
    app = prepare_auto(gui, tmp_path, monkeypatch)
    builder, doc = fake_autocad(None)
    def partial_draw(plan):
        doc.ModelSpace.AddLine((0, 0, 0), (1, 1, 0))
        raise RuntimeError("CAD failed")
    doc.SaveAs = lambda path: Path(path).write_bytes(b"partial DWG")
    monkeypatch.setattr(builder, "draw_plan", partial_draw)
    monkeypatch.setattr(gui, "AcadBuilder", lambda cfg: builder)
    monkeypatch.setattr(gui.sys, "platform", "win32")
    app._start_draw_scene()
    smoke._wait_for_gui_worker(app)
    result, = (tmp_path / "project" / "01_описания").glob("lot_32_*.dwg")
    assert result.read_bytes() == b"partial DWG"
    assert "неполная" in app.status_var.get() and str(result) in app.status_var.get()


def test_save_error_is_not_reported_as_drawing_error(gui, tmp_path, monkeypatch, fake_autocad):
    app = prepare_auto(gui, tmp_path, monkeypatch)
    builder, doc = fake_autocad(None)
    def fail(path):
        raise RuntimeError("save failed")
    doc.SaveAs = fail
    monkeypatch.setattr(gui, "AcadBuilder", lambda cfg: builder)
    monkeypatch.setattr(gui.sys, "platform", "win32")
    app._start_draw_scene()
    smoke._wait_for_gui_worker(app)
    assert "Отрисовка завершена" in app.status_var.get()
    assert "не сохранён" in app.status_var.get() and "save failed" in app.status_var.get()


def test_edited_portal_field_keeps_original_lot_for_result(gui, tmp_path, monkeypatch, fake_autocad):
    app = prepare_auto(gui, tmp_path, monkeypatch)
    app.step0_lot_var.set("31")
    builder, doc = fake_autocad(None)
    doc.SaveAs = lambda path: Path(path).write_bytes(b"DWG simulated")
    monkeypatch.setattr(gui, "AcadBuilder", lambda cfg: builder)
    monkeypatch.setattr(gui.sys, "platform", "win32")
    app._start_draw_scene()
    smoke._wait_for_gui_worker(app)
    assert len(list((tmp_path / "project" / "01_описания").glob("lot_32_*.dwg"))) == 1


def test_reset_during_drawing_prevents_not_yet_started_save(gui, tmp_path, monkeypatch, fake_autocad):
    app = prepare_auto(gui, tmp_path, monkeypatch)
    builder, doc = fake_autocad(None)
    doc.SaveAs = lambda path: pytest.fail("saved after reset")
    started, resume, finished = threading.Event(), threading.Event(), threading.Event()
    real_draw = builder.draw_plan
    def slow(plan):
        started.set()
        assert resume.wait(5)
        try:
            return real_draw(plan)
        finally:
            finished.set()
    monkeypatch.setattr(builder, "draw_plan", slow)
    monkeypatch.setattr(gui, "AcadBuilder", lambda cfg: builder)
    monkeypatch.setattr(gui.sys, "platform", "win32")
    app._start_draw_scene()
    assert started.wait(5)
    app._reset_session()
    resume.set()
    assert finished.wait(5)
    assert not list((tmp_path / "project" / "01_описания").glob("lot_32_*.dwg"))


def test_reported_entity_creation_failure_still_saves(gui, tmp_path, monkeypatch, fake_autocad):
    app = prepare_auto(gui, tmp_path, monkeypatch)
    builder, doc = fake_autocad(None)
    doc.SaveAs = lambda path: Path(path).write_bytes(b"DWG simulated")
    real_draw = builder.draw_plan
    def draw_with_warning(plan):
        result = real_draw(plan)
        result.warnings.append("не удалось создать текст: test failure")
        return result
    monkeypatch.setattr(builder, "draw_plan", draw_with_warning)
    monkeypatch.setattr(gui, "AcadBuilder", lambda cfg: builder)
    monkeypatch.setattr(gui.sys, "platform", "win32")
    app._start_draw_scene()
    smoke._wait_for_gui_worker(app)
    assert len(list((tmp_path / "project" / "01_описания").glob("lot_32_*.dwg"))) == 1
    assert "сохранён" in app.status_var.get()


def test_saves_partial_parse_result(gui, tmp_path, monkeypatch, fake_autocad):
    app = prepare_auto(gui, tmp_path, monkeypatch, complete=False)
    assert any(not doc.complete for doc in app._multi_scene_handoff.documents)
    builder, doc = fake_autocad(None)
    doc.SaveAs = lambda path: Path(path).write_bytes(b"partial parse DWG")
    monkeypatch.setattr(gui, "AcadBuilder", lambda cfg: builder)
    monkeypatch.setattr(gui.sys, "platform", "win32")
    app._start_draw_scene()
    smoke._wait_for_gui_worker(app)
    result, = (tmp_path / "project" / "01_описания").glob("lot_32_*.dwg")
    assert result.read_bytes() == b"partial parse DWG"


def test_draft_failure_continues_drawing_and_saves(gui, tmp_path, monkeypatch, fake_autocad):
    app = prepare_auto(gui, tmp_path, monkeypatch)
    builder, doc = fake_autocad(None)
    doc.SaveAs = lambda path: Path(path).write_bytes(b"no draft")
    monkeypatch.setattr(app, "_print_draft_plans", lambda: (_ for _ in ()).throw(RuntimeError("draft failed")))
    monkeypatch.setattr(gui, "AcadBuilder", lambda cfg: builder)
    monkeypatch.setattr(gui.sys, "platform", "win32")
    app._start_draw_scene()
    smoke._wait_for_gui_worker(app)
    result, = (tmp_path / "project" / "01_описания").glob("lot_32_*.dwg")
    assert doc.ModelSpace.lines and result.read_bytes() == b"no draft"
    assert "неполная" in app.status_var.get() and str(result) in app.status_var.get()


def test_reports_both_drawing_and_save_failure(gui, tmp_path, monkeypatch, fake_autocad):
    app = prepare_auto(gui, tmp_path, monkeypatch)
    builder, doc = fake_autocad(None)
    monkeypatch.setattr(builder, "draw_plan", lambda p: (_ for _ in ()).throw(RuntimeError("draw failure")))
    def save(path):
        Path(path).write_bytes(b"partial")
        raise RuntimeError("save failure")
    doc.SaveAs = save
    monkeypatch.setattr(gui, "AcadBuilder", lambda cfg: builder)
    monkeypatch.setattr(gui.sys, "platform", "win32")
    app._start_draw_scene()
    smoke._wait_for_gui_worker(app)
    status = app.status_var.get()
    partial, = (tmp_path / "project" / "01_описания").glob("lot_32_*.dwg")
    assert "draw failure" in status and "save failure" in status and str(partial) in status
    assert partial.read_bytes() == b"partial"


def test_one_failed_plan_does_not_stop_remaining_plans(gui, tmp_path, monkeypatch, fake_autocad):
    # Живой случай 25.11.2026: первый план (печатный черновик) лёг, и основной
    # план с участком и соседом даже не начинался. Теперь планы независимы:
    # остальные рисуются, документ сохраняется, упавший план назван в итоге.
    app = prepare_auto(gui, tmp_path, monkeypatch)
    builder, doc = fake_autocad(None)
    doc.SaveAs = lambda path: Path(path).write_bytes(b"DWG simulated")
    real_draw = builder.draw_plan
    calls = []
    def flaky(plan):
        calls.append(plan)
        if len(calls) == 1:
            raise RuntimeError("Property 'AddLine.Color' can not be set")
        return real_draw(plan)
    monkeypatch.setattr(builder, "draw_plan", flaky)
    monkeypatch.setattr(gui, "AcadBuilder", lambda cfg: builder)
    monkeypatch.setattr(gui.sys, "platform", "win32")
    app._start_draw_scene()
    smoke._wait_for_gui_worker(app)
    result, = (tmp_path / "project" / "01_описания").glob("lot_32_*.dwg")
    assert result.read_bytes() == b"DWG simulated"
    assert len(calls) >= 2 and doc.ModelSpace.lines
    status = app.status_var.get()
    assert "не нарисован" in status and "завершено планов: 1" not in status


def test_parallel_saves_use_different_names_in_same_folder(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(lambda _: auto_dwg_save.save_new_dwg(Doc(), tmp_path, "30"), range(24)))
    assert len(set(paths)) == 24
    assert all(p.parent == tmp_path and p.read_bytes() == b"DWG mock" for p in paths)
    assert len(list(tmp_path.iterdir())) == 24


class _BusySaveError(Exception):
    hresult = -2147418111


def test_busy_autocad_saveas_is_retried_until_it_saves(tmp_path):
    # Ночной сбой 3870 (24–25.11.2026): SaveAs один раз получил -2147418111
    # («вызов отклонён») и полностью нарисованный чертёж не сохранился.
    calls = []

    class BusyThenOk:
        def SaveAs(self, path):
            calls.append(path)
            if len(calls) < 3:
                raise _BusySaveError("(-2147418111, 'Вызов был отклонен получателем')")
            Path(path).write_bytes(b"DWG after busy")

    result = auto_dwg_save.save_new_dwg(BusyThenOk(), tmp_path, "30", sleep=lambda _s: None)
    assert len(calls) == 3 and result.read_bytes() == b"DWG after busy"


def test_busy_autocad_saveas_gives_up_honestly_after_attempts(tmp_path):
    calls = []

    class AlwaysBusy:
        def SaveAs(self, path):
            calls.append(path)
            raise RuntimeError("RPC_E_CALL_REJECTED (-2147418111)")

    with pytest.raises(auto_dwg_save.AutoDwgSaveError, match="не подтвердил") as err:
        auto_dwg_save.save_new_dwg(
            AlwaysBusy(), tmp_path, "30", sleep=lambda _s: None, save_attempts=3)
    assert len(calls) == 3 and "-2147418111" in str(err.value)


def test_non_busy_saveas_error_is_retried_then_reported_honestly(tmp_path):
    # Живой случай 25.11.2026: SaveAs только что созданного документа умер с
    # «<unknown>.SaveAs» — не «занятость», но и не приговор: полностью
    # нарисованный чертёж нельзя терять из-за одного отклонённого вызова.
    # Постоянная ошибка после всех попыток всё равно честно сообщается.
    calls = []

    class DiskError:
        def SaveAs(self, path):
            calls.append(path)
            raise RuntimeError("disk problem")

    with pytest.raises(auto_dwg_save.AutoDwgSaveError, match="disk problem"):
        auto_dwg_save.save_new_dwg(
            DiskError(), tmp_path, "30", sleep=lambda _s: None, save_attempts=3)
    assert len(calls) == 3


def test_transient_unknown_saveas_error_recovers_on_retry(tmp_path):
    calls = []

    class Flaky:
        def SaveAs(self, path):
            calls.append(path)
            if len(calls) == 1:
                raise RuntimeError("<unknown>.SaveAs")
            Path(path).write_bytes(b"DWG after hiccup")

    result = auto_dwg_save.save_new_dwg(Flaky(), tmp_path, "30", sleep=lambda _s: None)
    assert len(calls) == 2 and result.read_bytes() == b"DWG after hiccup"


def test_warnings_file_named_after_dwg_with_all_plan_warnings(tmp_path):
    dwg = tmp_path / "lot_41_ee2e30a5c6b143ea86cf5e20c96aa2c8.dwg"
    dwg.write_bytes(b"DWG")
    target = auto_dwg_save.write_dwg_warnings(
        dwg,
        [("наш участок", ["контур не замкнулся на 0.42 ft", "номер лота из описания: 41"]),
         ("сосед LOT 43", ["контур выходит за окно листа"]),
         ("черновик", [])],
        stamp="2026-11-24 17:40:00",
    )
    assert target is not None and target.name == "lot_41_ee2e30a5c6b143ea86cf5e20c96aa2c8.warnings.txt"
    text = target.read_text(encoding="utf-8")
    assert dwg.name in text and "2026-11-24 17:40:00" in text
    assert "== наш участок ==" in text and "- контур не замкнулся на 0.42 ft" in text
    assert "== сосед LOT 43 ==" in text and "черновик" not in text


def test_warnings_file_records_absence_and_draw_error(tmp_path):
    dwg = tmp_path / "lot_7_x.dwg"
    dwg.write_bytes(b"DWG")
    clean = auto_dwg_save.write_dwg_warnings(dwg, [("наш участок", [])], stamp="s")
    assert clean is not None and "Предупреждений не было." in clean.read_text(encoding="utf-8")
    partial = auto_dwg_save.write_dwg_warnings(
        dwg, [("наш участок", ["одно предупреждение"])], error="третий план не построен", stamp="s")
    assert partial is not None
    text = partial.read_text(encoding="utf-8")
    assert "Ошибка отрисовки: третий план не построен" in text and "- одно предупреждение" in text


def test_draw_warnings_are_written_next_to_saved_dwg(gui, tmp_path, monkeypatch, fake_autocad):
    app = prepare_auto(gui, tmp_path, monkeypatch)
    builder, doc = fake_autocad(None)
    doc.SaveAs = lambda path: Path(path).write_bytes(b"DWG simulated")
    real_draw = builder.draw_plan

    def draw_with_warning(plan):
        result = real_draw(plan)
        result.warnings.append("тестовое предупреждение отрисовки")
        return result

    monkeypatch.setattr(builder, "draw_plan", draw_with_warning)
    monkeypatch.setattr(gui, "AcadBuilder", lambda cfg: builder)
    monkeypatch.setattr(gui.sys, "platform", "win32")
    app._start_draw_scene()
    smoke._wait_for_gui_worker(app)
    data_dir = tmp_path / "project" / "01_описания"
    dwg, = data_dir.glob("lot_32_*.dwg")
    warnings_file = data_dir / f"{dwg.stem}.warnings.txt"
    text = warnings_file.read_text(encoding="utf-8")
    assert dwg.name in text and "тестовое предупреждение отрисовки" in text


def test_unavailable_autocad_reports_unsaved_file(gui, tmp_path, monkeypatch):
    from contextlib import contextmanager
    app = prepare_auto(gui, tmp_path, monkeypatch)
    class Unavailable:
        @contextmanager
        def drawing_session(self):
            raise RuntimeError("AutoCAD unavailable")
            yield
    monkeypatch.setattr(gui, "AcadBuilder", lambda cfg: Unavailable())
    monkeypatch.setattr(gui.sys, "platform", "win32")
    app._start_draw_scene()
    smoke._wait_for_gui_worker(app)
    assert "AutoCAD unavailable" in app.status_var.get() and "DWG не сохранён" in app.status_var.get()
    assert not list((tmp_path / "project" / "01_описания").glob("*.dwg"))
