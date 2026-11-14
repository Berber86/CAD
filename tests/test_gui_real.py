"""Проверка настоящих Tk/CustomTkinter-виджетов (нужен дисплей).

На Linux запускайте под Xvfb. На Windows тест создаёт реальное окно и закрывает
его самостоятельно. REQUIRE_REAL_TK=1 превращает отсутствие GUI в ошибку CI.
"""

from __future__ import annotations

import os
import traceback

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
    for env in ("HYDRA_API_KEY", "OPENROUTER_API_KEY", "ROUTERAI_API_KEY", "OPENAI_API_KEY"):
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

        # Настройки должны открываться и закрываться как с ключом, так и без него.
        app.toggle_keys_drawer()
        app.update()
        assert bool(app.settings_drawer_shell.winfo_ismapped()) is bool(stored_key)
        if app._keys_drawer_open:
            app.toggle_keys_drawer()
            app.update()

        # Показать PDF и вернуть редактор, затем переключить вкладки результата.
        app.source_mode_var.set("📄 PDF")
        app._on_source_mode_changed()
        app.update()
        assert app.pdf_mode_card.winfo_ismapped()
        app.source_mode_var.set("📝 Текст")
        app._on_source_mode_changed()
        app.update()
        assert app.text_editor_card.winfo_ismapped()

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

        # Выполнить отложенные Tk-события, а не только создать набор виджетов.
        app.after(250, app.quit)
        app.mainloop()
        assert not callback_errors, "\n".join(callback_errors)
    finally:
        app.destroy()
