"""Проверка настоящих Tk/CustomTkinter-виджетов (нужен дисплей).

На Linux запускайте под Xvfb. На Windows тест создаёт реальное окно и закрывает
его самостоятельно. REQUIRE_REAL_TK=1 превращает отсутствие GUI в ошибку CI.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

import pytest


def _unavailable(reason: str) -> None:
    if os.environ.get("REQUIRE_REAL_TK") == "1":
        pytest.fail(reason, pytrace=False)
    pytest.skip(reason)


def _require_gui():
    try:
        import tkinter as tk
        import customtkinter as ctk
    except ImportError as exc:
        _unavailable(f"Tk/CustomTkinter недоступен: {exc}")

    try:
        probe = tk.Tk()
        probe.withdraw()
        probe.update_idletasks()
        probe.destroy()
    except tk.TclError as exc:
        _unavailable(f"Нет доступного дисплея Tk: {exc}")
    return tk, ctk


@pytest.mark.parametrize("stored_key", ["", "test-key"])
def test_real_window_starts_and_handles_basic_actions(stored_key, tmp_path, monkeypatch):
    _tk, ctk = _require_gui()
    import llm_parser
    import main

    # Не читать и не изменять настоящие пользовательские настройки и API-ключи.
    for env in (
        "HYDRA_API_KEY", "OPENROUTER_API_KEY", "ROUTERAI_API_KEY",
        "OPENAI_API_KEY", "CAD_LLM_API_KEY",
    ):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(main, "load_config", lambda: llm_parser.load_config(tmp_path / "config.json"))
    monkeypatch.setattr(main, "_stored_provider_keys", lambda: {"hydra": stored_key} if stored_key else {})

    app = main.SurveyApp()
    callback_errors = []
    app.report_callback_exception = lambda typ, value, tb: callback_errors.append(
        "".join(traceback.format_exception(typ, value, tb))
    )
    try:
        app.update()
        assert app.winfo_ismapped()
        assert isinstance(app.sample_menu, ctk.CTkOptionMenu)
        assert app.settings_dimmer.cget("hover") is False
        assert app.sample_menu.cget("fg_color") == main.THEME["bg_card"]
        assert app.sample_menu.cget("button_color") == main.THEME["bg_card"]
        assert app._keys_drawer_open is (not bool(stored_key))

        # Настройки и строки дополнительных проходов видимы в открытом ящике.
        if not app._keys_drawer_open:
            app.toggle_keys_drawer()
            app.update()
        assert app.settings_drawer_shell.winfo_ismapped()
        for count, second, third in ((1, False, False), (2, True, False), (3, True, True)):
            app.select_passes(count)
            app.update()
            assert bool(app.pass2_row.winfo_ismapped()) is second
            assert bool(app.pass3_row.winfo_ismapped()) is third
        for provider in ("hydra", "openrouter", "routerai"):
            app.select_provider(provider)
            app.update()
            assert app.current_provider_id() == provider
        app.toggle_keys_drawer()
        app.update()
        assert not app.settings_drawer_shell.winfo_ismapped()

        # Показать PDF и вернуть редактор, затем переключить вкладки результата.
        app.source_mode_var.set("📄 PDF")
        app._on_source_mode_changed()
        app.update()
        assert app.pdf_mode_card.winfo_ismapped()
        app.source_mode_var.set("📷 Фото")
        app._on_source_mode_changed()
        app.update()
        assert app.photo_load_button.winfo_ismapped()
        assert not app.visual_bar.winfo_ismapped()

        # Открытие файла имитируется, но виджеты и обработчик загрузки настоящие.
        from PIL import Image
        from tkinter import filedialog
        image = tmp_path / "survey.jpg"
        Image.new("RGB", (30, 40), "white").save(image)
        monkeypatch.setattr(filedialog, "askopenfilenames", lambda **_: (str(image),))
        monkeypatch.setattr(app, "_ask_source_role", lambda _kind: "main")
        app.photo_load_button.invoke()
        app.update()
        assert app.image_paths == [image]
        assert app.visual_bar.winfo_ismapped()
        assert not app.photo_empty_card.winfo_ismapped()
        app.rotate_visual(90)
        app.update()
        assert app._current_visual_rotation() == 90
        app._reset_visual_sources()
        app.update()
        assert app.photo_load_button.winfo_ismapped()

        app.source_mode_var.set("📝 Текст")
        app._on_source_mode_changed()
        app.update()
        assert app.text_editor_card.winfo_ismapped()
        assert not app.photo_empty_card.winfo_ismapped()

        for label, frame in (
            ("📋 Ведомость", app.sheet_tab_frame),
            ("🛡 Проверки", app.checks_tab_frame),
            ("🏘 Соседи", app.neighbors_tab_frame),
            ("🧾 Журнал", app.log_tab_frame),
        ):
            app.result_tab_var.set(label)
            app._switch_result_tab()
            app.update()
            assert frame.winfo_ismapped(), label

        # Офлайн-пример проверяет первые обновления и отрисовку после старта.
        app._on_sample_selected("Aspenwall St — 420' (Основной участок)")
        app.update()
        assert app._preview_main_plan is not None
        assert app.stat_area_var.get() != "—"
        assert "ОСНОВНОЙ УЧАСТОК" in app.inspector_box.get("1.0", "end")
        app._on_sample_selected("Aspenwall St — 365' (Сосед слева)")
        app.update()
        assert len(app._preview_neighbor_plans) == 1

        # Выполнить отложенные Tk-события, а не только создать набор виджетов.
        app.after(250, app.quit)
        app.mainloop()
        assert not callback_errors, "\n".join(callback_errors)
    finally:
        app.destroy()


def test_real_startup_failure_exits_nonzero_and_writes_log(tmp_path):
    _require_gui()
    source = Path(__file__).resolve().parents[1]
    work = tmp_path / "isolated-app"
    work.mkdir()
    for name in ("main.py", "llm_parser.py", "acad_builder.py"):
        shutil.copy2(source / name, work / name)
    (work / "config.json").write_text("{invalid json", encoding="utf-8")

    env = os.environ.copy()
    env.update(HOME=str(work), USERPROFILE=str(work), PYTHONIOENCODING="utf-8")
    for name in (
        "HYDRA_API_KEY", "OPENROUTER_API_KEY", "ROUTERAI_API_KEY",
        "OPENAI_API_KEY", "CAD_LLM_API_KEY",
    ):
        env.pop(name, None)
    result = subprocess.run(
        [sys.executable, str(work / "main.py")],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
    )
    assert result.returncode == 1
    assert "ConfigError" in result.stderr
    assert "ConfigError" in (work / "tsai_crash.log").read_text(encoding="utf-8")


def test_real_resizable_panel_topmost_neighbor_tab_and_full_reset(tmp_path, monkeypatch):
    tk, _ctk = _require_gui()
    import time
    import llm_parser
    import main
    from tkinter import filedialog, messagebox
    from llm_parser import ASPENWALL_SAMPLES

    pymupdf = pytest.importorskip("pymupdf")
    monkeypatch.setattr(main, "load_config", lambda: llm_parser.load_config(tmp_path / "config.json"))
    monkeypatch.setattr(main, "_stored_provider_keys", lambda: {})
    files = [tmp_path / "left.pdf", tmp_path / "right.pdf"]
    for path, sample in zip(files, (
        ASPENWALL_SAMPLES["Aspenwall St — 365' (Сосед слева)"],
        ASPENWALL_SAMPLES["Aspenwall St — 475' (Сосед справа)"],
    )):
        doc = pymupdf.open()
        page = doc.new_page()
        assert page.insert_textbox(pymupdf.Rect(25, 25, 575, 800), sample, fontsize=9) >= 0
        doc.save(path)
        doc.close()
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **_: tuple(map(str, files)))
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **kw: True)

    app = main.SurveyApp()
    monkeypatch.setattr(app, "_ask_source_role", lambda _kind: "neighbor")
    callback_errors = []
    app.report_callback_exception = lambda typ, value, tb: callback_errors.append(
        "".join(traceback.format_exception(typ, value, tb))
    )
    try:
        app.update()
        if app._keys_drawer_open:
            app.toggle_keys_drawer()
        app.update()
        assert not app.topmost_var.get()
        assert not bool(app.attributes("-topmost"))
        app.after(550, app.quit)
        app.mainloop()
        assert not app.topmost_var.get() and not bool(app.attributes("-topmost"))

        before_right = app.right_panel.winfo_width()
        before_center = app.center_col.winfo_width()
        mono_size = app._result_fonts["mono"].cget("size")
        text_font = str(app.inspector_box._textbox.cget("font"))
        log_font = str(app.log_box._textbox.cget("font"))
        grip = app.result_splitter_grip
        root_x, root_y = grip.winfo_rootx() + 5, grip.winfo_rooty() + 5
        # Настоящие события на внутреннем tk.Label, не прямой вызов обработчика.
        grip._label.event_generate("<ButtonPress-1>", x=5, y=5, rootx=root_x, rooty=root_y)
        grip._label.event_generate("<B1-Motion>", x=5, y=5, rootx=root_x - 200,
                                   rooty=root_y, state=0x100)
        app.update()
        assert app.right_panel.winfo_width() > before_right + 150
        assert app.center_col.winfo_width() < before_center - 150
        assert app._result_fonts["mono"].cget("size") > mono_size
        assert str(app.inspector_box._textbox.cget("font")) != text_font
        assert str(app.log_box._textbox.cget("font")) != log_font
        expanded = app._result_panel_width
        app.geometry("1100x760")
        app.update()
        assert app._result_logical_pixels(app.center_col.winfo_width()) >= main.CENTER_PANEL_MIN_WIDTH - 2
        assert app._result_panel_width < expanded
        app.geometry("1440x940")
        app.update()
        assert app._result_panel_width == expanded
        app._set_result_panel_width(100)
        app.update()
        assert app._result_panel_width == main.RESULT_PANEL_MIN_WIDTH
        assert app._result_fonts["mono"].cget("size") == mono_size
        widths = [button.winfo_width() for button in app.result_tab_buttons.values()]
        assert max(widths) - min(widths) <= 1 and min(widths) >= 70

        # Кнопка именно внутри вкладки и доступна при минимальной ширине панели.
        app.result_tab_var.set("🏘 Соседи")
        app._switch_result_tab()
        app.update()
        assert app.verify_neighbors_button.winfo_ismapped()
        assert app.verify_neighbors_button.master.master is app.neighbors_tab_frame
        assert app.verify_neighbors_button.winfo_rootx() >= app.right_panel.winfo_rootx()
        assert (app.verify_neighbors_button.winfo_rootx() + app.verify_neighbors_button.winfo_width()
                <= app.right_panel.winfo_rootx() + app.right_panel.winfo_width())

        app._on_sample_selected("Aspenwall St — 420' (Основной участок)")
        app.lot_role_var.set("neighbor")
        app._on_lot_role_changed()
        app.source_mode_var.set("📄 PDF")
        app._on_source_mode_changed()
        app.load_pdf_button.invoke()
        app.geometry("1100x760")
        app.update()
        assert app.neighbor_pdf_paths == files and not app.use_llm_var.get()
        assert app.neighbor_pdf_summary.winfo_ismapped()
        assert app.reset_session_button.winfo_ismapped()
        assert app.regex_mode_badge.master is app.engine_model_label.master
        assert (app.regex_mode_badge.winfo_rooty() + app.regex_mode_badge.winfo_height()
                <= app.reset_session_button.winfo_rooty())
        app.parse_button.invoke()
        deadline = time.monotonic() + 8
        while app._busy and time.monotonic() < deadline:
            app.update()
            time.sleep(0.01)
        app.update()
        assert not app._busy
        assert len(app._preview_neighbor_plans) == 2
        assert len(app._neighbor_report.pair_reports) == 2
        assert len(app.neighbors_scroll_frame.winfo_children()) == 2
        app.geometry("1440x940")
        app.update()
        app._set_result_panel_width(700)
        app.update()
        assert app._result_fonts["body"].cget("size") > 11
        assert len(app.neighbors_scroll_frame.winfo_children()) == 2

        app.provider_key_vars["hydra"].set("not-cleared")
        app.reset_session_button.invoke()
        app.update()
        assert app.neighbor_pdf_paths == [] and app._preview_main_plan is None
        assert app._preview_neighbor_plans == [] and app.description is None
        assert app.provider_key_vars["hydra"].get() == "not-cleared"
        assert all(path.exists() for path in files)
        assert not callback_errors, "\n".join(callback_errors)
    finally:
        app.destroy()


def test_real_file_role_dialog_and_persistent_severity_tabs(tmp_path, monkeypatch):
    """Настоящие модальный выбор роли и независимые цвета вкладок при переключении."""
    _tk, ctk = _require_gui()
    import main
    import llm_parser
    from tkinter import filedialog
    from acad_builder import AcadBuilder, LotSuspicionReport, SuspicionItem, NeighborPairReport, NeighborVerificationReport
    from llm_parser import ASPENWALL_SAMPLES, parse_with_regex

    monkeypatch.setattr(main, "load_config", lambda: llm_parser.load_config(tmp_path / "config.json"))
    monkeypatch.setattr(main, "_stored_provider_keys", lambda: {})
    app = main.SurveyApp()
    errors = []
    app.report_callback_exception = lambda typ, value, tb: errors.append(
        "".join(traceback.format_exception(typ, value, tb))
    )
    try:
        app.update()
        if app._keys_drawer_open:
            app.toggle_keys_drawer()
        app.update()
        assert app.source_mode_switch.cget("values") == ["📄 PDF", "📷 Фото", "📝 Текст"]
        assert app.role_bar.winfo_ismapped()
        app.source_mode_var.set("📷 Фото")
        app._on_source_mode_changed()
        app.update()
        assert not app.role_bar.winfo_ismapped()

        events = []
        monkeypatch.setattr(filedialog, "askopenfilenames", lambda **kw: events.append("files") or ())
        def cancel_role():
            win = app._source_role_dialog
            events.append(("role", win._role_buttons["neighbor"].cget("text"), bool(win.grab_current())))
            win._role_buttons["cancel"].invoke()
        app.after(100, cancel_role)
        app._load_images()
        assert events == [("role", "Соседний участок / участки", True)]
        assert app._source_role_dialog is None
        app.after(100, cancel_role)
        app._pick_and_verify_neighbor_files()
        assert events[-1][0] == "role" and "files" not in events
        app.after(100, cancel_role)
        app._load_neighbor_files()
        assert events[-1][0] == "role" and "files" not in events

        # При выборе соседей файловый диалог открывается строго после выбора роли.
        def choose_neighbor():
            events.append("choose-neighbor")
            app._source_role_dialog._role_buttons["neighbor"].invoke()
        app.after(100, choose_neighbor)
        app._load_pdf()
        assert events[-2:] == ["choose-neighbor", "files"]
        assert app._source_role_dialog is None

        main_text = tmp_path / "main.txt"
        main_text.write_text(ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"], encoding="utf-8")
        monkeypatch.setattr(filedialog, "askopenfilenames", lambda **kw: events.append("files-main") or (str(main_text),))
        def choose_main():
            events.append("choose-main")
            app._source_role_dialog._role_buttons["main"].invoke()
        app.after(100, choose_main)
        app._pick_and_verify_neighbor_files()
        assert events[-2:] == ["choose-main", "files-main"]
        assert app._current_source_mode() == "📝 Текст" and not app.is_neighbor_mode()
        assert main_text.name in app._text_source_label and not app._busy
        app.source_mode_var.set("📝 Текст")
        app._on_source_mode_changed()
        app.update()
        assert app.role_bar.winfo_ismapped()

        desc, _ = parse_with_regex(ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"])
        builder = AcadBuilder(app.config_data)
        main_plan = builder.plan(desc)
        main_plan.suspicion = LotSuspicionReport(
            shape_kind="основной", status="error",
            items=[SuspicionItem("main-error", "error", "Ошибка основного", "Проверить документ")],
        )
        neighbor = builder.plan(desc, is_neighbor=True)
        neighbor.suspicion = LotSuspicionReport(shape_kind="сосед", status="ok", items=[])
        pair_status = ["warn"]
        monkeypatch.setattr(main, "verify_with_neighbors", lambda *_: NeighborVerificationReport(
            overall_status=pair_status[0], warning_count=int(pair_status[0] == "warn"),
            pair_reports=[NeighborPairReport(1, "сосед", status=pair_status[0])],
        ))
        app._record_plan_for_preview(neighbor, source=tmp_path / "clean.pdf")
        app.update()
        assert app.result_tab_buttons["🏘 Соседи"].cget("fg_color") == main.THEME["bg_field"]
        assert app.neighbors_scroll_frame.winfo_children()[0].cget("fg_color") == main.THEME["bg_inset"]
        app._record_plan_for_preview(main_plan)
        app._record_plan_for_preview(neighbor, source=tmp_path / "clean.pdf")
        app._select_result_tab("🛡 Проверки")
        app.update()
        checks = app.result_tab_buttons["🛡 Проверки"]
        neighbors = app.result_tab_buttons["🏘 Соседи"]
        assert checks.cget("fg_color") == "#dc2626"
        assert neighbors.cget("fg_color") == "#92400e"
        assert checks.cget("text") == "⛔ Проверки" and neighbors.cget("text") == "⚠ Соседи"

        def labels(widget):
            own = [str(widget.cget("text"))] if isinstance(widget, ctk.CTkLabel) else []
            return own + [text for child in widget.winfo_children() for text in labels(child)]
        visible = "\n".join(labels(app.checks_scroll_frame))
        assert "Ошибка основного" in visible and "clean.pdf" in visible
        assert "Замечаний к форме не найдено" in visible
        app._select_result_tab("🏘 Соседи")
        app.update()
        assert checks.cget("fg_color") == "#991b1b" and neighbors.cget("fg_color") == "#d97706"
        app._clear_neighbors_scene()
        app.update()
        assert neighbors.cget("fg_color") == main.THEME["seg_sel"]
        assert checks.cget("fg_color") == "#991b1b"
        pair_status[0] = "ok"
        main_plan.suspicion = LotSuspicionReport(shape_kind="основной", status="ok", items=[])
        app._record_plan_for_preview(main_plan)
        app._record_plan_for_preview(neighbor, source=tmp_path / "clean.pdf")
        app._select_result_tab("🛡 Проверки")
        app.update()
        assert checks.cget("fg_color") == "#059669" and checks.cget("text") == "✓ Проверки"
        assert neighbors.cget("fg_color") == "#065f46" and neighbors.cget("text") == "✓ Соседи"
        assert not errors, "\n".join(errors)
    finally:
        app.destroy()
