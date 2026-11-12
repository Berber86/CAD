"""
Smoke-тест GUI (main.py) без дисплея и без реального CustomTkinter.

CustomTkinter и tkinter подменяются лёгкими заглушками: проверяется, что всё
окно собирается, колбэки работают, а статус/невязка/лог обновляются без ошибок.
Это ловит ошибки вроде попытки передать state= в CTkTextbox.configure().
"""

from __future__ import annotations

import sys
import types

import pytest

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


class _Var:
    """Замена tkinter.Variable / StringVar / BooleanVar."""

    def __init__(self, value=None):
        self._value = value

    def get(self):
        return self._value

    def set(self, value):
        self._value = value


class _Widget:
    def __init__(self, *args, **kwargs):
        self._kwargs = dict(kwargs)
        self._show = kwargs.get("show", "")
        self._text = kwargs.get("text", "")

    def grid(self, *args, **kwargs):
        return self

    def pack(self, *args, **kwargs):
        return self

    def configure(self, **kwargs):
        self._kwargs.update(kwargs)
        if "show" in kwargs:
            self._show = kwargs["show"]
        if "text" in kwargs:
            self._text = kwargs["text"]

    def cget(self, name):
        if name == "show":
            return self._show
        if name == "text":
            return self._text
        return self._kwargs.get(name)

    def insert(self, *args, **kwargs):
        pass

    def get(self, *args, **kwargs):
        return ""

    def delete(self, *args, **kwargs):
        pass

    def after(self, *args, **kwargs):
        return None

    def start(self):
        pass

    def stop(self):
        pass

    def set(self, value):
        pass

    def title(self, *args):
        pass

    def geometry(self, *args):
        pass

    def minsize(self, *args):
        pass

    def attributes(self, *args, **kwargs):
        pass

    def protocol(self, *args, **kwargs):
        pass

    def mainloop(self):
        pass

    def destroy(self):
        pass

    def quit(self):
        pass

    def grid_columnconfigure(self, *args, **kwargs):
        pass

    def grid_rowconfigure(self, *args, **kwargs):
        pass

    def winfo_children(self):
        return []


def _fake_modules() -> dict[str, types.ModuleType]:
    ctk = types.ModuleType("customtkinter")
    for name in (
        "CTk", "CTkFrame", "CTkLabel", "CTkButton", "CTkEntry", "CTkOptionMenu",
        "CTkTextbox", "CTkProgressBar", "CTkSwitch", "CTkToplevel",
    ):
        setattr(ctk, name, type(name, (_Widget,), {}))
    ctk.CTkFont = lambda **kwargs: None
    ctk.set_appearance_mode = lambda *args: None
    ctk.set_default_color_theme = lambda *args: None
    ctk.StringVar = _Var
    ctk.BooleanVar = _Var

    tkinter = types.ModuleType("tkinter")
    filedialog = types.ModuleType("tkinter.filedialog")
    filedialog.askopenfilename = lambda **kwargs: ""
    tkinter.filedialog = filedialog
    return {"customtkinter": ctk, "tkinter": tkinter, "tkinter.filedialog": filedialog}


@pytest.fixture()
def gui(monkeypatch):
    for name, module in _fake_modules().items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.syspath_prepend(str(ROOT))
    monkeypatch.delitem(sys.modules, "main", raising=False)
    import main as gui_module  # noqa: PLC0415 - импорт после подмены модулей

    yield gui_module
    sys.modules.pop("main", None)


def test_gui_builds_and_reports(gui, capsys):
    from llm_parser import load_config, parse_legal_description_full

    app = gui.SurveyApp()

    # Масштабы и высота текста
    assert app.scale_options == ['1"=10\'', '1"=16\'', '1"=20\'', '1"=30\'']
    assert app.scale_var.get() == '1"=16\''
    app.scale_var.set('1"=10\'')
    app._refresh_height_label()
    assert "0.562" in app.height_label.cget("text")

    # Кнопка-«глаз» переключает скрытие ключа и свою иконку
    assert app.key_entry.cget("show") == "•"
    app._toggle_key_visibility()
    assert app.key_entry.cget("show") == ""
    assert app.key_button.cget("text") == "🙈"
    app._toggle_key_visibility()
    assert app.key_entry.cget("show") == "•"

    # Разбор без LLM + отображение статуса/невязки
    outcome = parse_legal_description_full(
        text=gui.EXAMPLE_TEXT, cfg=load_config(), use_llm=False
    )
    app._apply_parsed(outcome)
    assert app.description is not None and len(app.description.segments) == 4
    summary = app._parsed_summary(outcome)
    assert "Main Street" in summary and "411.00" in summary
    closure_text = app._closure_text(outcome)
    assert closure_text.startswith("Closure Error: 0.000 ft")
    assert "периметр 411.00 ft" in closure_text

    # Лог открывается и закрывается без падения
    app._show_log()
    app._on_close()
