"""
Шаг 1, набор PDF: наш участок и соседи выбираются одним разом.

Проверяется то, о чём договорились с заказчиком:

* роль не спрашивается до выбора файлов — после выбора показан список, в
  котором отмечается наш участок, остальные файлы считаются соседями;
* у каждого файла свои страницы «с … по …»; они подставляются из имени
  (``…_p17``, ``…_p2_5``) и остаются правимыми;
* в парсер и в ИИ уходят только выбранные страницы;
* все участки строятся вместе, ошибки показаны по каждому участку и по
  нашему участку относительно соседей.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import test_gui_smoke as smoke

gui = smoke.gui  # фикстура окна с подменённым tkinter
_write_test_pdf = smoke._write_test_pdf
_wait = smoke._wait_for_gui_worker

MAIN = "Aspenwall St — 420' (Основной участок)"
LEFT = "Aspenwall St — 365' (Сосед слева)"
RIGHT = "Aspenwall St — 475' (Сосед справа)"
FILLER = "RECORDING AND ENDORSEMENT COVER PAGE. This page is part of the instrument."


def _samples():
    from llm_parser import ASPENWALL_SAMPLES

    return ASPENWALL_SAMPLES


def _pick(monkeypatch, *paths):
    from tkinter import filedialog

    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **_: tuple(str(p) for p in paths))


def _open_set(app, monkeypatch, *paths):
    """Кнопка «Выберите PDF»: выбор файлов и появившийся список."""
    _pick(monkeypatch, *paths)
    app._load_pdf()
    dialog = app._pdf_set_dialog
    assert dialog is not None, app.status_var.get()
    return dialog


def _three_lots(tmp_path):
    samples = _samples()
    main = tmp_path / "bruklin250_p2.pdf"
    left = tmp_path / "left.pdf"
    right = tmp_path / "right.pdf"
    _write_test_pdf(main, [FILLER, samples[MAIN], FILLER])
    _write_test_pdf(left, [samples[LEFT]])
    _write_test_pdf(right, [samples[RIGHT]])
    return main, left, right


# ---------------------------------------------------------------- без окна


@pytest.mark.parametrize(
    "name, expected",
    [
        ("deed_p17.pdf", (17, 17)),
        ("bruklin250_p2_5.pdf", (2, 5)),
        ("LOT_12_P3.PDF", (3, 3)),
        ("a_p5_2.pdf", (2, 5)),  # перепутанный порядок исправляется
        # Шаг 0 кладёт копию описания как OUR 32 NEW_p4-6.pdf — дефис тоже читается.
        ("OUR 32 NEW_p4-6.pdf", (4, 6)),
        ("LOT 30 OLD_p2-2.pdf", (2, 2)),
        ("lot 12_p4 .pdf", (4, 4)),
        ("plain.pdf", None),
        ("p17.pdf", None),  # нужен именно «_p»
        ("lot_p0.pdf", None),
        ("map_p.pdf", None),
        ("lot_p17 (1).pdf", None),  # окончание должно стоять в самом конце имени
        ("lot_page3.pdf", None),
    ],
)
def test_pages_are_read_from_the_end_of_the_pdf_name(name, expected):
    from llm_parser import pages_from_pdf_name

    assert pages_from_pdf_name(name) == expected
    assert pages_from_pdf_name(Path("C:/deeds") / name) == expected


def test_page_boxes_are_checked_per_file():
    from pdf_set import read_page_boxes

    assert read_page_boxes("3", "4", 8) == (3, 4)
    assert read_page_boxes(" 1 ", "1", 8) == (1, 1)
    assert read_page_boxes("5", "", 8) == (5, 5)  # пустое «по» — одна страница
    assert read_page_boxes("", "2", 8) == (1, 2)
    for first, last, hint in [
        ("", "", "не указаны"),
        ("a", "2", "целыми числами"),
        ("0", "2", "начинается с 1"),
        ("4", "3", "больше последней"),
        ("2", "9", "всего 8"),
    ]:
        with pytest.raises(ValueError, match=hint):
            read_page_boxes(first, last, 8)


def test_item_takes_pages_from_name_and_feeds_only_them(tmp_path):
    from llm_parser import PDFError
    from pdf_set import make_item

    samples = _samples()
    pdf = tmp_path / "lot_p3.pdf"
    _write_test_pdf(pdf, [FILLER, samples[LEFT], samples[MAIN], FILLER, None])
    item = make_item(pdf)
    assert (item.first_page, item.last_page, item.total_pages) == (3, 3, 5)
    assert item.pages_label() == "стр. 3 из 5" and item.pages_spec() == "3"
    assert "из имени" in item.pages_note
    assert item.use_llm is False and item.has_legal_text()
    source = item.parse_source()
    # В парсер идёт текст только выбранной страницы: ни обложки, ни чужого участка.
    assert set(source) == {"text"}
    assert "420" in source["text"] and "365" not in source["text"]
    assert "COVER PAGE" not in source["text"]

    # Страница без обхода: пока способ не выбран вручную, он подстраивается.
    item.set_pages(1, 1)
    assert item.use_llm is True
    assert item.parse_source() == {"pdf_path": pdf, "pdf_pages": "1"}
    item.set_pages(2, 3)
    assert item.use_llm is False and item.pages_spec() == "2-3"
    # ИИ по текстовым страницам получает их текст, а не изображения.
    item.set_mode(True)
    assert set(item.parse_source()) == {"text"}
    # Ручной выбор больше не меняется сам.
    item.set_mode(False)
    item.set_pages(5, 5)
    assert item.use_llm is False
    with pytest.raises(PDFError, match="нет текстового слоя"):
        item.parse_source()


def test_item_defaults_when_the_name_has_no_usable_pages(tmp_path):
    from pdf_set import make_item

    samples = _samples()
    over = tmp_path / "deed_p2_9.pdf"
    _write_test_pdf(over, [FILLER, samples[MAIN], FILLER])
    item = make_item(over)
    assert (item.first_page, item.last_page) == (2, 3)
    assert "взято до 3-й" in item.pages_note

    wrong = tmp_path / "deed_p17.pdf"
    _write_test_pdf(wrong, [samples[MAIN], FILLER])
    item = make_item(wrong)
    assert (item.first_page, item.last_page) == (1, 2)
    assert "проверьте страницы" in item.pages_note

    text = tmp_path / "long_text.pdf"
    _write_test_pdf(text, [FILLER] * 9 + [samples[MAIN]])
    item = make_item(text)
    # Текстовый PDF без страниц в имени читается целиком и бесплатно.
    assert (item.first_page, item.last_page, item.use_llm) == (1, 10, False)

    scan = tmp_path / "scan.pdf"
    _write_test_pdf(scan, [None] * 12)
    item = make_item(scan, vision_max_pages=8)
    # Скан: в ИИ по умолчанию уходит не больше лимита изображений.
    assert (item.first_page, item.last_page, item.use_llm) == (1, 8, True)
    assert "первые 8 из 12" in item.pages_note
    assert item.parse_source() == {"pdf_path": scan, "pdf_pages": "1-8"}


# ------------------------------------------------------------------- окно


def test_pdf_button_opens_the_list_and_builds_all_lots_together(gui, tmp_path, monkeypatch):
    import llm_parser
    from acad_builder import DrawResult
    from tkinter import messagebox

    main, left, right = _three_lots(tmp_path)
    app = gui.SurveyApp()
    app.log_mode_var.set(True)  # проверяются подробные строки отчёта
    monkeypatch.setattr(app, "_ask_source_role", lambda *_: pytest.fail("роль не спрашивается до выбора PDF"))
    monkeypatch.setattr(llm_parser, "_extract_with_llm", lambda *a, **k: pytest.fail("платный запрос без выбора ИИ"))
    assert "наш участок и соседи" in app.load_pdf_button.cget("text")
    assert not app.pdf_pages_row._gridded  # общего поля страниц для набора нет

    dialog = _open_set(app, monkeypatch, main, left, right)
    # Список показан, разбор ещё не начат, роль не назначена сама.
    assert not app._busy and not app.pdf_set and app._preview_main_plan is None
    assert [row["name"].cget("text") for row in dialog.rows] == [main.name, left.name, right.name]
    assert dialog.main_var.get() == ""
    assert not dialog.scene_frame._gridded  # схема пуста — вопроса о замене нет
    # Страницы: из имени «…_p2» у нашего файла, все страницы у остальных.
    assert [(row["first"].get(), row["last"].get()) for row in dialog.rows] == [("2", "2"), ("1", "1"), ("1", "1")]
    assert [row["mode_var"].get() for row in dialog.rows] == ["Локально"] * 3
    assert "из имени файла" in dialog.rows[0]["note"].cget("text")
    assert "бесплатно" in dialog.submit_button.cget("text")
    # Пока список открыт, горячие клавиши разбора не запускают старый путь.
    app._start(parse_only=True)
    assert not app._busy

    dialog.submit()
    assert app._pdf_set_dialog is dialog and "отметьте наш участок" in dialog.message.cget("text")
    dialog.rows[0]["radio"].select()
    app._select_result_tab("📋 Ведомость")  # перед стартом разбора вкладка другая
    dialog.submit()
    assert app._pdf_set_dialog is None and app._source_role_dialog is None and dialog.closed
    _wait(app)
    # Разбор начат — сразу журнал, а не ведомость.
    assert app.result_tab_var.get() == "🧾 Журнал"
    # Набор без ошибок и расхождений: окно итога не открывается само, только журнал и строка состояния.
    assert app._pdf_set_report_window is None
    assert "ИТОГ РАЗБОРА НАБОРА PDF" in app.log_text()

    assert [item.is_main for item in app.pdf_set] == [True, False, False]
    assert app._preview_main_plan is not None and not app._preview_main_plan.is_neighbor
    assert app._preview_main_plan.description.distance_to_corner_ft == 420.0
    assert set(app._neighbor_plan_sources) == {left, right}
    assert all(plan.is_neighbor for plan in app._preview_neighbor_plans)
    assert app._last_active_plan is app._preview_main_plan
    assert len(app._neighbor_report.pair_reports) == 2
    assert app.is_neighbor_mode() is False

    # Итог: по каждому участку и по нашему относительно соседей.
    app._open_pdf_set_report_window()  # чистый набор окно не открывает само — открываем кнопкой
    report = app._pdf_set_report_window._report_text
    assert f"▸ НАШ УЧАСТОК — {main.name}" in report and "стр. 2 из 3 · локально" in report
    assert f"▸ СОСЕД #1 — {left.name}" in report and f"▸ СОСЕД #2 — {right.name}" in report
    assert "построено: 3" in report and "не разобрано: 0" in report
    assert "НАШ УЧАСТОК ОТНОСИТЕЛЬНО СОСЕДЕЙ" in report
    assert app._neighbor_report.headline() in report
    assert "ИТОГ РАЗБОРА НАБОРА PDF" in app.log_text()
    assert "построено 3 из 3" in app.status_var.get()

    # Левая панель показывает набор, а не общее поле страниц.
    assert "Набор PDF: 3 файл." in app.load_pdf_button.cget("text")
    summary = app.neighbor_pdf_summary.cget("text")
    assert f"наш: {main.name}" in summary and "стр. 2 из 3" in summary and f"сосед: {left.name}" in summary
    assert app.pdf_set_buttons._gridded and not app.pdf_pages_row._gridded
    assert "Разобрать набор заново (бесплатно)" in app.parse_button.cget("text")
    assert app.regex_link_button.cget("command") == app._edit_pdf_set

    # Отрисовка — отдельной кнопкой и сразу всей рассчитанной схемой.
    drawn = []
    monkeypatch.setattr(
        gui.AcadBuilder, "draw_plan",
        lambda _b, plan: drawn.append(plan) or DrawResult(plan=plan, text_count=0, layers={}),
    )
    monkeypatch.setattr(gui.sys, "platform", "win32")
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **kw: True)
    app._start(parse_only=False)
    _wait(app)
    assert drawn == [app._preview_main_plan, *app._preview_neighbor_plans]
    app._on_close()


def test_only_the_chosen_pages_are_parsed_and_edits_reparse_one_file(gui, tmp_path, monkeypatch):
    samples = _samples()
    both = tmp_path / "two_lots_p2.pdf"  # стр. 1 — другой участок, стр. 2 — наш
    left = tmp_path / "left.pdf"
    _write_test_pdf(both, [samples[RIGHT], samples[MAIN]])
    _write_test_pdf(left, [samples[LEFT]])
    app = gui.SurveyApp()
    app.log_mode_var.set(True)  # проверяются подробные строки отчёта
    real = gui.parse_legal_description_full
    calls = []

    def spy(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(gui, "parse_legal_description_full", spy)
    dialog = _open_set(app, monkeypatch, both, left)
    dialog.rows[0]["radio"].select()
    dialog.submit()
    _wait(app)
    assert len(calls) == 2 and all(call["use_llm"] is False for call in calls)
    assert "475" not in calls[0]["text"] and "420" in calls[0]["text"]
    assert all("pdf_path" not in call for call in calls)
    assert app._preview_main_plan.description.distance_to_corner_ft == 420.0
    first_main = app._preview_main_plan
    signature = app._ready_source_signature
    assert signature is not None and signature == app._source_signature()

    # Отмена правки ничего не меняет.
    app._edit_pdf_set()
    dialog = app._pdf_set_dialog
    assert dialog.main_var.get() == "0" and not dialog.scene_frame._gridded
    dialog.rows[0]["first"].set("1")
    dialog.rows[0]["last"].set("1")
    dialog.pages_edited(0)
    dialog.cancel()
    assert app._pdf_set_dialog is None and app._source_role_dialog is None
    assert (app.pdf_set[0].first_page, app.pdf_set[0].last_page) == (2, 2)
    assert app._source_signature() == signature and app._preview_main_plan is first_main

    # Поля остаются правимыми: другая страница — заново разбирается только этот файл.
    app._edit_pdf_set()
    dialog = app._pdf_set_dialog
    dialog.rows[0]["first"].set("1")
    dialog.rows[0]["last"].set("1")
    dialog.pages_edited(0)
    assert "бесплатный локальный разбор" in dialog.rows[0]["note"].cget("text")
    dialog.submit()
    _wait(app)
    assert len(calls) == 3
    assert "475" in calls[2]["text"] and "420" not in calls[2]["text"]
    assert app._preview_main_plan.description.distance_to_corner_ft == 475.0
    assert app._preview_main_plan is not first_main
    assert len(app._preview_neighbor_plans) == 1 and set(app._neighbor_plan_sources) == {left}
    assert "стр. 1 из 2" in app._pdf_set_report_window._report_text

    # Неверные страницы не отправляются.
    app._edit_pdf_set()
    dialog = app._pdf_set_dialog
    dialog.rows[0]["last"].set("7")
    dialog.submit()
    assert app._pdf_set_dialog is dialog and not app._busy
    assert "всего 2" in dialog.message.cget("text") and "Страницы" in dialog.rows[0]["note"].cget("text")
    dialog.cancel()

    # Кнопка разбора перечитывает весь набор.
    app._start(parse_only=True)
    _wait(app)
    assert len(calls) == 5
    app._on_close()


def test_each_file_has_its_own_parser_and_scans_send_only_their_pages(gui, tmp_path, monkeypatch):
    from llm_parser import ParseOutcome, parse_with_regex

    samples = _samples()
    text_pdf = tmp_path / "main.pdf"
    scan = tmp_path / "survey_p2_3.pdf"
    _write_test_pdf(text_pdf, [samples[MAIN]])
    _write_test_pdf(scan, [None, None, None, None])
    app = gui.SurveyApp()
    app.log_mode_var.set(True)  # проверяются подробные строки отчёта
    app.select_passes(1)
    real = gui.parse_legal_description_full
    left_desc, _ = parse_with_regex(samples[LEFT])
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        if kwargs["use_llm"]:
            return ParseOutcome(data=left_desc, source="llm-vision" if kwargs.get("pdf_path") else "llm")
        return real(**kwargs)

    monkeypatch.setattr(gui, "parse_legal_description_full", fake)
    dialog = _open_set(app, monkeypatch, text_pdf, scan)
    assert [row["mode_var"].get() for row in dialog.rows] == ["Локально", "ИИ"]
    assert (dialog.rows[1]["first"].get(), dialog.rows[1]["last"].get()) == ("2", "3")
    assert "изображения: 2 стр." in dialog.rows[1]["note"].cget("text")
    assert "ИИ: 1 файл." in dialog.submit_button.cget("text")
    # Переключатель у файла: текстовый PDF можно отправить в ИИ — уйдёт текст страниц.
    dialog.set_mode(0, True)
    assert "ИИ получит текст стр. 1" in dialog.rows[0]["note"].cget("text")
    assert "ИИ: 2 файл." in dialog.submit_button.cget("text")
    dialog.set_mode(0, False)
    # Локальный разбор скана невозможен — об этом сказано в строке файла.
    dialog.set_mode(1, False)
    assert "локально не разобрать" in dialog.rows[1]["note"].cget("text")
    dialog.set_mode(1, True)
    dialog.rows[0]["radio"].select()
    dialog.submit()
    _wait(app)

    # Один вызов ИИ по скану; остальные — локальные разборы текста (в том числе проверка
    # ответа ИИ перед записью в кеш Шага 0).
    llm_calls = [call for call in calls if call["use_llm"]]
    local_calls = [call for call in calls if not call["use_llm"]]
    assert len(llm_calls) == 1
    assert local_calls and all("text" in call and "pdf_path" not in call for call in local_calls)
    by_llm = {True: llm_calls[0]}
    assert by_llm[True]["pdf_path"] == scan and by_llm[True]["pdf_pages"] == "2-3"
    assert not by_llm[True].get("text")
    assert app._preview_main_plan is not None and set(app._neighbor_plan_sources) == {scan}
    app._open_pdf_set_report_window()  # чистый набор окно не открывает само — открываем кнопкой
    report = app._pdf_set_report_window._report_text
    assert "стр. 2–3 из 4 · ИИ / Vision по изображению страниц" in report
    assert "ИИ: 1 файл., платно" in app.parse_button.cget("text")
    app._on_close()


def test_disagreeing_ai_passes_keep_the_lot_and_flag_it(gui, tmp_path, monkeypatch):
    from tkinter import messagebox
    from llm_parser import ConsensusResult, ParseOutcome, TokenUsage, parse_with_regex

    samples = _samples()
    text_pdf = tmp_path / "main.pdf"
    scan = tmp_path / "neighbor_scan.pdf"
    _write_test_pdf(text_pdf, [samples[MAIN]])
    _write_test_pdf(scan, [None])
    good, _ = parse_with_regex(samples[LEFT])
    bad = good.model_copy(deep=True)
    bad.segments[0].distance_ft += 7.0  # один проход прочитал длину иначе — контур не замыкается
    app = gui.SurveyApp()
    app.log_mode_var.set(True)  # этот тест проверяет подробный текст отчёта
    app.select_passes(2)
    seen = []

    def fake_consensus(**kwargs):
        seen.append(kwargs)
        outcomes = [ParseOutcome(data=bad, source="llm-vision"), ParseOutcome(data=good, source="llm-vision")]
        return ConsensusResult(
            agreed=False, pass_outcomes=outcomes, pass_targets=[], discrepancies=[],
            selected_outcome=outcomes[0], total_usage=TokenUsage(),
        )

    monkeypatch.setattr(gui, "run_multipass_consensus", fake_consensus)
    dialog = _open_set(app, monkeypatch, text_pdf, scan)
    dialog.rows[0]["radio"].select()
    dialog.submit()
    _wait(app)

    assert seen and seen[0]["pdf_path"] == scan and seen[0]["pdf_pages"] == "1"
    # Участок не выброшен: взят проход с лучшим замыканием и помечен.
    plan = app._neighbor_plan_sources[scan]
    assert plan.closure.distance < 0.08
    assert any("проходы ИИ разошлись" in warning for warning in plan.warnings)
    item = app.pdf_set[1]
    assert item.on_scene and not item.error and "Проход #2" in item.issues[0]
    app._open_pdf_set_report_window()  # чистый набор окно не открывает само — открываем кнопкой
    report = app._pdf_set_report_window._report_text
    assert "построен с замечаниями" in report and "проходы ИИ разошлись" in report
    assert "с замечаниями: 1" in app.status_var.get()
    assert "⚠ сосед" in app.neighbor_pdf_summary.cget("text")
    # Перед отрисовкой программа спрашивает подтверждение и называет замечание.
    asked = []
    monkeypatch.setattr(messagebox, "askyesno", lambda _title, text, **kw: asked.append(text) or False)
    app._start(parse_only=False)
    assert asked and "neighbor_scan.pdf" in asked[0] and "Отрисовка отменена" in app.status_var.get()
    app._on_close()


def test_failed_file_is_reported_and_blocks_drawing_until_fixed_or_removed(gui, tmp_path, monkeypatch):
    main, left, _right = _three_lots(tmp_path)
    scan = tmp_path / "scan.pdf"
    _write_test_pdf(scan, [None])
    app = gui.SurveyApp()
    app.log_mode_var.set(True)  # проверяются подробные строки отчёта
    real = gui.parse_legal_description_full
    calls = []
    monkeypatch.setattr(gui, "parse_legal_description_full", lambda **kw: calls.append(kw) or real(**kw))
    dialog = _open_set(app, monkeypatch, main, left, scan)
    dialog.rows[0]["radio"].select()
    dialog.set_mode(2, False)  # скан отправлен в локальный разбор — он не сработает
    dialog.submit()
    _wait(app)

    assert len(calls) == 2  # скан до парсера не дошёл
    assert app._preview_main_plan is not None and set(app._neighbor_plan_sources) == {left}
    failed = app.pdf_set[2]
    assert not failed.on_scene and "нет текстового слоя" in failed.error
    report = app._pdf_set_report_window._report_text
    assert "▸ СОСЕД (не построен) — scan.pdf" in report and "✗ не разобран" in report
    assert "построено: 2" in report and "не разобрано: 1" in report
    assert "В AutoCAD уйдёт только полный набор" in report
    assert "не разобрано: 1 (scan.pdf)" in app.status_var.get()
    assert "✗ сосед: scan.pdf" in app.neighbor_pdf_summary.cget("text")
    assert app._ready_source_signature is None
    app._start(parse_only=False)
    assert not app._busy and "Не все файлы набора разобраны: scan.pdf" in app.status_var.get()

    # Убираем файл из набора: готовые участки не разбираются заново.
    app._edit_pdf_set()
    dialog = app._pdf_set_dialog
    assert "прошлый разбор" in dialog.rows[2]["note"].cget("text")
    dialog.remove(2)
    assert len(dialog.rows) == 2 and dialog.main_var.get() == "0"
    dialog.submit()
    _wait(app)
    assert len(calls) == 2
    assert [item.path for item in app.pdf_set] == [main, left]
    assert app._ready_source_signature == app._source_signature()
    assert scan not in app._pending_neighbor_files
    assert "построено 2 из 2" in app.status_var.get()
    app._start(parse_only=False)
    assert "Не все файлы" not in app.status_var.get()
    _wait(app)
    app._on_close()


def test_new_set_asks_about_the_existing_scene_and_may_have_no_main(gui, tmp_path, monkeypatch):
    main, left, right = _three_lots(tmp_path)
    app = gui.SurveyApp()
    app.log_mode_var.set(True)  # проверяются подробные строки отчёта
    app._on_sample_selected(MAIN)  # на схеме уже есть наш участок из текста
    old_main = app._preview_main_plan
    assert old_main is not None

    dialog = _open_set(app, monkeypatch, left, right)
    assert dialog.scene_frame._gridded and dialog.main_var.get() == ""
    dialog.no_main_radio.select()
    dialog.submit()
    assert app._pdf_set_dialog is dialog and "заменить схему" in dialog.message.cget("text")
    dialog.scene_add_radio.select()
    dialog.submit()
    _wait(app)
    # «Добавить»: наш участок остался, оба файла — соседи, сверка выполнена.
    assert app._preview_main_plan is old_main
    assert all(not item.is_main for item in app.pdf_set)
    assert set(app._neighbor_plan_sources) == {left, right}
    assert len(app._neighbor_report.pair_reports) == 2
    assert "Нашего участка в наборе нет" in app.neighbor_pdf_summary.cget("text")
    app._open_pdf_set_report_window()  # чистый набор окно не открывает само — открываем кнопкой
    report = app._pdf_set_report_window._report_text
    assert "НАШ УЧАСТОК —" not in report and app._neighbor_report.headline() in report

    # Один новый файл при готовом нашем участке не объявляется нашим сам.
    dialog = _open_set(app, monkeypatch, main)
    assert dialog.scene_frame._gridded and dialog.main_var.get() == ""
    dialog.rows[0]["radio"].select()
    dialog.scene_replace_radio.select()
    dialog.submit()
    _wait(app)
    # «Заменить»: прежние участки убраны, на схеме только новый набор.
    assert app._preview_main_plan is not old_main and app._preview_main_plan is not None
    assert not app._preview_neighbor_plans and app._neighbor_report is None
    assert [item.path for item in app.pdf_set] == [main]
    app._open_pdf_set_report_window()  # без нашего участка окно не открывается само
    assert "сверять не с чем" in app._pdf_set_report_window._report_text

    # Пустая схема и один файл: он предложен как наш, вопроса о схеме нет.
    app._reset_session()
    assert not app.pdf_set and app._pdf_set_dialog is None
    dialog = _open_set(app, monkeypatch, main)
    assert dialog.main_var.get() == "0" and not dialog.scene_frame._gridded
    dialog.cancel()
    assert not app.pdf_set and app._preview_main_plan is None
    app._on_close()


def test_role_change_and_clearing_neighbours_keep_the_scene_consistent(gui, tmp_path, monkeypatch):
    main, left, right = _three_lots(tmp_path)
    app = gui.SurveyApp()
    dialog = _open_set(app, monkeypatch, main, left, right)
    dialog.rows[0]["radio"].select()
    dialog.submit()
    _wait(app)
    assert app._preview_main_plan.description.distance_to_corner_ft == 420.0

    # Нашим отмечен другой файл: прежний наш становится соседом, дублей нет.
    app._edit_pdf_set()
    dialog = app._pdf_set_dialog
    dialog.rows[1]["radio"].select()
    dialog.submit()
    _wait(app)
    assert [item.is_main for item in app.pdf_set] == [False, True, False]
    assert app._preview_main_plan.description.distance_to_corner_ft == 365.0
    assert set(app._neighbor_plan_sources) == {main, right}
    assert len(app._preview_neighbor_plans) == 2
    assert app._ready_source_signature == app._source_signature()

    # «Убрать соседей» оставляет в наборе только наш участок, готовый к отрисовке.
    app._clear_neighbors_scene()
    assert [item.path for item in app.pdf_set] == [left] and not app._preview_neighbor_plans
    assert app._ready_source_signature == app._source_signature()
    assert "Набор PDF: 1 файл." in app.load_pdf_button.cget("text")

    # В список можно добавить файлы; повторно выбранный файл не дублируется.
    app._edit_pdf_set()
    dialog = app._pdf_set_dialog
    _pick(monkeypatch, left, right)
    dialog.add_files()
    assert [row["name"].cget("text") for row in dialog.rows] == [left.name, right.name]
    assert dialog.main_var.get() == "0"
    dialog.submit()
    _wait(app)
    assert set(app._neighbor_plan_sources) == {right} and len(app._neighbor_report.pair_reports) == 1
    app._on_close()


def test_unreadable_and_mixed_selections_are_reported(gui, tmp_path, monkeypatch):
    from PIL import Image

    main, _left, _right = _three_lots(tmp_path)
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a pdf")
    image = tmp_path / "photo.jpg"
    Image.new("RGB", (20, 20), "white").save(image)
    app = gui.SurveyApp()

    _pick(monkeypatch, broken)
    app._load_pdf()
    assert app._pdf_set_dialog is None and "Не удалось открыть PDF: broken.pdf" in app.status_var.get()

    _pick(monkeypatch, main, image)
    app._load_pdf()
    assert app._pdf_set_dialog is None and "без смешивания" in app.status_var.get()

    # Фотографии работают по-прежнему: роль спрашивается, набор не создаётся.
    asked = []
    monkeypatch.setattr(app, "_ask_source_role", lambda kind: asked.append(kind) or "neighbor")
    _pick(monkeypatch, image)
    app._load_pdf()
    assert asked == ["изображениях"] and app.image_paths == [image] and not app.pdf_set

    dialog = _open_set(app, monkeypatch, main, broken)
    assert len(dialog.rows) == 1 and "broken.pdf" in dialog.message.cget("text")
    dialog.cancel()
    # Отмена списка не трогает уже загруженные фотографии.
    assert app.image_paths == [image]
    app._on_close()


def test_vision_receives_exactly_the_chosen_scan_pages(tmp_path):
    """Сквозная проверка без окна: источник скана рендерит только страницы из полей."""
    import llm_parser
    from pdf_set import make_item

    scan = tmp_path / "survey_p2_3.pdf"
    _write_test_pdf(scan, [None, None, None, None, None])
    item = make_item(scan)
    source = item.parse_source()
    assert source == {"pdf_path": scan, "pdf_pages": "2-3"}
    cfg = llm_parser.load_config(tmp_path / "config.json")
    text, images, notes, _usage = llm_parser._prepare_parse_input(
        None, source["pdf_path"], cfg, use_llm=True, pdf_pages=source["pdf_pages"],
    )
    assert text == "" and len(images) == 2
    assert any("2 стр." in note for note in notes)
    item.set_pages(4, 4)
    source = item.parse_source()
    _text, images, _notes, _usage = llm_parser._prepare_parse_input(
        None, source["pdf_path"], cfg, use_llm=True, pdf_pages=source["pdf_pages"],
    )
    assert len(images) == 1


def test_file_lot_numbers_stay_with_each_plan_and_cached_result(gui, tmp_path, monkeypatch):
    import lot_identity

    samples = _samples()
    paths = [tmp_path / "OUR 28 NEW.pdf", tmp_path / "LOT 45 NEW.pdf", tmp_path / "LOT 30 OLD.pdf"]
    for path, key in zip(paths, [MAIN, LEFT, RIGHT]):
        _write_test_pdf(path, [samples[key]])
    app = gui.SurveyApp()
    dialog = _open_set(app, monkeypatch, *paths)
    dialog.rows[0]["radio"].select()
    dialog.submit()
    _wait(app)
    assert app._preview_main_plan.description._source_lot_number == "28"
    assert next(i.text for i in app._preview_main_plan.items if i.kind == "main-lot-label") == "LOT 28"
    canvas_text = [item[2].get("text", "") for item in app.preview_canvas.items if item[0] == "text"]
    assert "LOT 28" in canvas_text and "LOT 45" in canvas_text
    assert app._neighbor_plan_sources[paths[1]].items[0].text == "LOT 45"
    assert app._neighbor_plan_sources[paths[2]].items[0].text == "LOT 30"
    assert app._neighbor_plan_sources[paths[1]].description.lot_number == "26"
    assert any("не совпадает" in w for w in app._neighbor_plan_sources[paths[1]].warnings)
    assert [lot_identity.source_number(item.outcome.data) for item in app.pdf_set] == ["28", "45", "30"]
    # Changing the role rebuilds from cached reading without reassigning identities.
    app._edit_pdf_set()
    dialog = app._pdf_set_dialog
    dialog.rows[1]["radio"].select()
    dialog.submit()
    _wait(app)
    assert app._preview_main_plan.description._source_lot_number == "45"
    assert next(i.text for i in app._preview_main_plan.items if i.kind == "main-lot-label") == "LOT 45"
    inspector = app._build_inspector_report(app._preview_main_plan)
    assert "Lot 45" in inspector and "из имени файла 45; в результате разбора 26" in inspector
    assert app._neighbor_plan_sources[paths[0]].items[0].text == "LOT 28"
    assert app._neighbor_plan_sources[paths[2]].items[0].text == "LOT 30"
    app._on_close()


def test_passport_collision_is_reported_without_losing_the_plan(gui, tmp_path, monkeypatch):
    path = tmp_path / "OUR 28 NEW.pdf"
    _write_test_pdf(path, [_samples()[MAIN]])
    passport = path.with_suffix(".json")
    passport.write_text('{"other": true}', encoding="utf-8")
    app = gui.SurveyApp()
    dialog = _open_set(app, monkeypatch, path)
    dialog.rows[0]["radio"].select()
    dialog.submit()
    _wait(app)
    assert app._preview_main_plan is not None
    assert any("не удалось сохранить паспорт" in warning for warning in app._preview_main_plan.warnings)
    assert passport.read_text(encoding="utf-8") == '{"other": true}'
    app._on_close()


def test_multi_document_roles_are_independent_and_survive_list_edits(gui, tmp_path, monkeypatch):
    """The experimental UI selects document roles, never individual descriptions."""
    main, left, right = _three_lots(tmp_path)
    app = gui.SurveyApp()
    app.config_data['multiple_description_search']['enabled'] = True  # test process only
    dialog = _open_set(app, monkeypatch, main, left, right)
    assert dialog.multi_mode
    assert not dialog.no_main_radio._gridded
    assert all(not row['role_var'].get() for row in dialog.rows)
    dialog.rows[0]['role_var'].set(True)
    dialog.rows[1]['role_var'].set(True)
    dialog.remove(2)  # rebuild must not collapse the two selections into one
    assert [row['role_var'].get() for row in dialog.rows] == [True, True]
    captured = []
    monkeypatch.setattr(app, '_submit_pdf_set', lambda items, **kw: captured.append((items, kw)))
    dialog.submit()
    assert [item.is_main for item in captured[0][0]] == [True, True]


def test_multi_roles_are_prepopulated_only_from_explicit_passport(gui, tmp_path, monkeypatch):
    import json
    app = gui.SurveyApp()
    app.config_data['multiple_description_search']['enabled'] = True
    first, second, named_our = (tmp_path / name for name in ('lot-a.pdf', 'lot-b.pdf', 'OUR 77.pdf'))
    for path in (first, second, named_our):
        _write_test_pdf(path, [FILLER])
    first.with_suffix('.json').write_text(json.dumps({'file': first.name, 'role': 'our'}))
    second.with_suffix('.json').write_text(json.dumps({'file': second.name, 'role': 'neighbour'}))
    dialog = _open_set(app, monkeypatch, first, second, named_our)
    assert [row['role_var'].get() for row in dialog.rows] == [True, False, False]
    dialog.rows[0]['role_var'].set(False)
    dialog.rows[2]['role_var'].set(True)
    captured = []
    monkeypatch.setattr(app, '_submit_pdf_set', lambda items, **kw: captured.append(items))
    dialog.submit()
    assert [item.is_main for item in captured[0]] == [False, False, True]


def test_multi_start_dispatches_only_new_worker_and_is_not_ready_yet(gui, tmp_path, monkeypatch):
    app = gui.SurveyApp()
    app.config_data['multiple_description_search']['enabled'] = True
    path = tmp_path / 'OUR 6.pdf'
    _write_test_pdf(path, [_samples()[MAIN]])
    dialog = _open_set(app, monkeypatch, path)
    dialog.rows[0]['role_var'].set(True)
    monkeypatch.setattr(app, '_pdf_set_worker', lambda *a, **k: pytest.fail('legacy parser ran'))
    starts = []
    monkeypatch.setattr(app, '_start_background', lambda target, **kwargs: starts.append((target, kwargs)))
    dialog.submit()
    assert app._busy and len(app.pdf_set) == 1
    assert app._ready_source_signature is None and app._input_incomplete
    assert len(starts) == 1 and starts[0][0].__name__ == '_multi_pdf_worker'
