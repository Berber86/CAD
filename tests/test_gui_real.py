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
