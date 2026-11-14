"""
Smoke-тест GUI (main.py) без дисплея и без реального CustomTkinter.

CustomTkinter и tkinter подменяются лёгкими заглушками: проверяется, что всё
окно собирается, переключатель трёх провайдеров (Hydra AI / OpenRouter / RouterAI),
выбор моделей, три поля API-ключей и вывод токенов/невязки/лога работают без ошибок.
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


# CustomTkinter разрешает прозрачность только для отдельных цветовых параметров.
# Заглушка должна соблюдать этот контракт, иначе тест сборки окна пропускает
# ValueError из настоящих конструкторов виджетов.
_TRANSPARENT_COLORS = {
    "CTkFrame": {"bg_color", "fg_color"},
    "CTkLabel": {"bg_color", "fg_color"},
    "CTkButton": {"bg_color", "fg_color"},
    "CTkEntry": {"bg_color", "fg_color"},
    "CTkTextbox": {"bg_color", "fg_color"},
    "CTkOptionMenu": {"bg_color"},
    "CTkComboBox": {"bg_color"},
    "CTkSegmentedButton": {"bg_color"},
    "CTkRadioButton": {"bg_color"},
    "CTkCheckBox": {"bg_color"},
    "CTkProgressBar": {"bg_color"},
    "CTkSwitch": {"bg_color", "border_color", "progress_color"},
}


class _Font:
    def __init__(self, **kwargs):
        self._options = dict(kwargs)

    def cget(self, name):
        return self._options.get(name)

    def configure(self, **kwargs):
        self._options.update(kwargs)


class _Widget:
    def _validate_colors(self, kwargs):
        allowed = _TRANSPARENT_COLORS.get(type(self).__name__)
        if allowed is None:
            return
        for name, value in kwargs.items():
            if "color" in name and value == "transparent" and name not in allowed:
                raise ValueError(f"transparency is not allowed for {type(self).__name__}.{name}")

    def __init__(self, *args, **kwargs):
        self._validate_colors(kwargs)
        self.master = args[0] if args else kwargs.get("master")
        self._children = []
        if hasattr(self.master, "_children"):
            self.master._children.append(self)
        self._kwargs = dict(kwargs)
        self._show = kwargs.get("show", "")
        self._attributes = {}
        self._text = kwargs.get("text", "")
        self._values = list(kwargs.get("values") or [])
        var = kwargs.get("variable") or kwargs.get("textvariable")
        self._value = var.get() if var is not None else (self._values[0] if self._values else "")

    def grid(self, *args, **kwargs):
        self._gridded = True
        return self

    def grid_remove(self, *args, **kwargs):
        self._gridded = False
        return self

    def pack(self, *args, **kwargs):
        return self

    def pack_forget(self, *args, **kwargs):
        return None

    def configure(self, **kwargs):
        self._validate_colors(kwargs)
        self._kwargs.update(kwargs)
        if "show" in kwargs:
            self._show = kwargs["show"]
        if "text" in kwargs:
            self._text = kwargs["text"]
        if "values" in kwargs:
            self._values = list(kwargs["values"])

    def cget(self, name):
        if name == "show":
            return self._show
        if name == "text":
            return self._text
        if name == "values":
            return list(self._values)
        return self._kwargs.get(name)

    def insert(self, *args, **kwargs):
        if len(args) >= 2:
            self._value = str(self._value or "") + str(args[1])
        return None

    def get(self, *args, **kwargs):
        var = self._kwargs.get("variable") or self._kwargs.get("textvariable")
        if var is not None:
            return var.get()
        return self._value

    def delete(self, *args, **kwargs):
        self._value = ""
        return None

    def after(self, *args, **kwargs):
        return None

    def start(self):
        return None

    def stop(self):
        return None

    def set(self, value):
        self._value = value
        var = self._kwargs.get("variable") or self._kwargs.get("textvariable")
        if var is not None:
            var.set(value)

    def select(self):
        var = self._kwargs.get("variable")
        if var is not None and "value" in self._kwargs:
            var.set(self._kwargs["value"])
        cmd = self._kwargs.get("command")
        if callable(cmd):
            cmd()

    def title(self, *args):
        return None

    def geometry(self, *args):
        return None

    def minsize(self, *args):
        return None

    def attributes(self, *args, **kwargs):
        if len(args) >= 2:
            self._attributes[args[0]] = args[1]
        elif args:
            return self._attributes.get(args[0])
        return None

    def protocol(self, *args, **kwargs):
        return None

    def mainloop(self):
        return None

    def destroy(self):
        for child in list(self._children):
            child.destroy()
        if hasattr(self.master, "_children") and self in self.master._children:
            self.master._children.remove(self)

    def quit(self):
        return None

    def grid_columnconfigure(self, *args, **kwargs):
        return None

    def grid_rowconfigure(self, *args, **kwargs):
        return None

    def grid_propagate(self, *args, **kwargs):
        return None

    def pack_propagate(self, *args, **kwargs):
        return None

    def lift(self, *args, **kwargs):
        return None

    def lower(self, *args, **kwargs):
        return None

    def focus_set(self, *args, **kwargs):
        return None

    def winfo_children(self):
        return list(self._children)

    def winfo_width(self):
        return self._kwargs.get("width", 1440)

    def bind(self, event_name, callback, *args, **kwargs):
        if not hasattr(self, "_bindings"):
            self._bindings = {}
        self._bindings[event_name] = callback

    def bind_all(self, event_name, callback):
        if not hasattr(self, "_global_bindings"):
            self._global_bindings = {}
        self._global_bindings[event_name] = callback

    def clipboard_clear(self):
        self._clipboard = ""

    def clipboard_append(self, text):
        self._clipboard = str(getattr(self, "_clipboard", "") or "") + str(text)


class _FakeCanvas(_Widget):
    """Заглушка tkinter.Canvas для тестирования живого CAD-предпросмотра."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.items: list[tuple[str, tuple, dict]] = []
        self._bindings: dict[str, object] = {}

    def delete(self, tag):
        if tag == "all":
            self.items.clear()

    def winfo_width(self):
        return 460

    def winfo_height(self):
        return 280

    def bind(self, event_name, callback):
        self._bindings[event_name] = callback

    def create_line(self, *coords, **kwargs):
        self.items.append(("line", coords, dict(kwargs)))
        return len(self.items)

    def create_oval(self, *coords, **kwargs):
        self.items.append(("oval", coords, dict(kwargs)))
        return len(self.items)

    def create_text(self, *coords, **kwargs):
        self.items.append(("text", coords, dict(kwargs)))
        return len(self.items)


def _fake_modules() -> dict[str, types.ModuleType]:
    ctk = types.ModuleType("customtkinter")
    for name in (
        "CTk",
        "CTkFrame",
        "CTkScrollableFrame",
        "CTkLabel",
        "CTkButton",
        "CTkEntry",
        "CTkOptionMenu",
        "CTkComboBox",
        "CTkSegmentedButton",
        "CTkRadioButton",
        "CTkCheckBox",
        "CTkTextbox",
        "CTkProgressBar",
        "CTkSwitch",
        "CTkToplevel",
    ):
        setattr(ctk, name, type(name, (_Widget,), {}))
    ctk.CTkFont = _Font
    ctk.set_appearance_mode = lambda *args: None
    ctk.set_default_color_theme = lambda *args: None
    ctk.StringVar = _Var
    ctk.BooleanVar = _Var

    tkinter = types.ModuleType("tkinter")
    tkinter.Canvas = _FakeCanvas
    filedialog = types.ModuleType("tkinter.filedialog")
    filedialog.askopenfilename = lambda **kwargs: ""
    filedialog.askopenfilenames = lambda **kwargs: ()
    tkinter.filedialog = filedialog
    messagebox = types.ModuleType("tkinter.messagebox")
    messagebox.askokcancel = lambda *args, **kwargs: False
    messagebox.askyesno = lambda *args, **kwargs: False
    messagebox.askyesnocancel = lambda *args, **kwargs: None
    tkinter.messagebox = messagebox
    return {
        "customtkinter": ctk,
        "tkinter": tkinter,
        "tkinter.filedialog": filedialog,
        "tkinter.messagebox": messagebox,
    }


@pytest.fixture()
def gui(monkeypatch):
    for name, module in _fake_modules().items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.syspath_prepend(str(ROOT))
    monkeypatch.delitem(sys.modules, "main", raising=False)
    import main as gui_module  # noqa: PLC0415 - импорт после подмены модулей

    # В старых тестах выбор роли эмулируется текстовым переключателем; тесты
    # самого диалога и независимости ролей задают явный ответ отдельно.
    monkeypatch.setattr(
        gui_module.SurveyApp, "_ask_source_role",
        lambda app, source: (
            "neighbor" if source.startswith("выбранных файлах") else str(app.lot_role_var.get() or "main")
        ),
    )
    yield gui_module
    sys.modules.pop("main", None)


def test_widget_stub_rejects_unsupported_transparency():
    ctk = _fake_modules()["customtkinter"]
    with pytest.raises(ValueError, match=r"CTkButton\.hover_color"):
        ctk.CTkButton(hover_color="transparent")
    for color in ("fg_color", "button_color"):
        with pytest.raises(ValueError, match=rf"CTkOptionMenu\.{color}"):
            ctk.CTkOptionMenu(**{color: "transparent"})
    button = ctk.CTkButton(fg_color="transparent", hover=False)
    with pytest.raises(ValueError, match=r"CTkButton\.hover_color"):
        button.configure(hover_color="transparent")


def test_main_returns_failure_and_saves_traceback(gui, tmp_path, monkeypatch, capsys):
    def fail_startup():
        raise ValueError("startup test error")

    monkeypatch.setattr(gui, "SurveyApp", fail_startup)
    monkeypatch.setattr(gui.Path, "home", classmethod(lambda cls: tmp_path))

    assert gui.main() == 1
    log = tmp_path / "tsai_crash.log"
    assert "ValueError: startup test error" in log.read_text(encoding="utf-8")
    assert "startup test error" in capsys.readouterr().err


def test_main_still_reports_when_crash_log_cannot_be_written(gui, tmp_path, monkeypatch, capsys):
    def fail_startup():
        raise RuntimeError("startup without writable log")

    monkeypatch.setattr(gui, "SurveyApp", fail_startup)
    monkeypatch.setattr(gui.Path, "home", classmethod(lambda cls: tmp_path / "missing-directory"))

    assert gui.main() == 1
    stderr = capsys.readouterr().err
    assert "RuntimeError: startup without writable log" in stderr
    assert "Не удалось сохранить журнал" in stderr


def test_main_returns_success_after_mainloop(gui, monkeypatch):
    ran = []

    class FakeApp:
        def mainloop(self):
            ran.append(True)

    monkeypatch.setattr(gui, "SurveyApp", FakeApp)
    assert gui.main() == 0
    assert ran == [True]


def test_gui_builds_and_reports(gui):
    from llm_parser import (
        ParseOutcome,
        TokenUsage,
        load_config,
        parse_legal_description_full,
    )

    app = gui.SurveyApp()

    # Масштабы и высота текста
    assert app.scale_options == ['1"=10\'', '1"=16\'', '1"=20\'', '1"=30\'']
    assert app.scale_var.get() == '1"=16\''
    app.scale_var.set('1"=10\'')
    app._refresh_height_label()
    assert "0.562" in app.height_label.cget("text")

    # Три провайдера и переключение моделей между ними
    assert app.provider_ids == ["hydra", "openrouter", "routerai"]
    assert app.provider_labels == ["Hydra AI", "OpenRouter", "RouterAI"]
    assert app.current_provider_id() == "hydra"
    assert app.current_model() == "glm-5.2"
    assert "активен" in app.provider_key_labels["hydra"].cget("text")

    # Меняем модель в Hydra, переключаемся в OpenRouter и RouterAI, затем назад
    app._on_model_selected("hydra-gemini-pro")
    app.select_provider("OpenRouter")
    assert app.current_provider_id() == "openrouter"
    assert "nvidia/nemotron-3.5-super-vl:free" in app.model_combo.cget("values")
    assert "активен" in app.provider_key_labels["openrouter"].cget("text")
    assert "активен" not in app.provider_key_labels["hydra"].cget("text")

    app.select_provider("routerai")
    assert app.current_provider_id() == "routerai"
    assert "openai/gpt-4.1" in app.model_combo.cget("values")

    app.select_provider("Hydra AI")
    assert app.current_model() == "hydra-gemini-pro"

    # Три отдельных поля API-ключей и индивидуальные кнопки-«глаза»
    for pid in ("hydra", "openrouter", "routerai"):
        assert app.provider_key_entries[pid].cget("show") == "•"
    app.provider_key_vars["hydra"].set("hk-key$$")
    app.provider_key_vars["openrouter"].set("or-key")
    app.provider_key_vars["routerai"].set("rai-key")

    app._toggle_key_visibility("openrouter")
    assert app.provider_key_entries["openrouter"].cget("show") == ""
    assert app.provider_key_buttons["openrouter"].cget("text") == "🙈"
    assert app.provider_key_entries["hydra"].cget("show") == "•"
    app._toggle_key_visibility("openrouter")
    assert app.provider_key_entries["openrouter"].cget("show") == "•"

    cfg_rt, active_pid, active_model, active_key, active_secret = app._build_runtime_config()
    assert active_pid == "hydra"
    assert active_model == "hydra-gemini-pro"
    assert active_key == "hk-key"  # суффикс $$ срезан
    assert active_secret == ""
    assert cfg_rt["api"]["providers"]["openrouter"]["api_key"] == "or-key"
    assert cfg_rt["api"]["providers"]["routerai"]["api_key"] == "rai-key"

    # Переключатель видимости PIN
    assert app.key_secret_entry.cget("show") == "•"
    app._toggle_secret_visibility()
    assert app.key_secret_entry.cget("show") == ""
    app._toggle_secret_visibility()
    assert app.key_secret_entry.cget("show") == "•"

    # Разбор без LLM -> токены = 0
    outcome_regex = parse_legal_description_full(
        text=gui.EXAMPLE_TEXT, cfg=load_config(), use_llm=False
    )
    app._apply_parsed(outcome_regex)
    assert app.description is not None and len(app.description.segments) == 4
    assert "0 (" in app.tokens_var.get()

    # Вызов LLM №1 -> ушедшие и пришедшие токены за вызов
    llm_outcome_1 = ParseOutcome(
        data=outcome_regex.data,
        source="llm",
        provider="hydra",
        provider_label="Hydra AI",
        model="glm-5.2",
        llm_mode="json_schema",
        usage=TokenUsage(
            prompt_tokens=612,
            completion_tokens=194,
            total_tokens=806,
            provider="hydra",
            provider_label="Hydra AI",
            model="glm-5.2",
        ),
    )
    app._apply_parsed(llm_outcome_1)
    tok_line_1 = app.tokens_var.get()
    assert "612" in tok_line_1 and "194" in tok_line_1 and "806" in tok_line_1
    assert "Hydra AI / glm-5.2" in tok_line_1
    summary_1 = app._parsed_summary(llm_outcome_1)
    assert "Hydra AI · glm-5.2" in summary_1 and "Main Street" in summary_1

    # Вызов LLM №2 -> появляется счётчик сессии
    llm_outcome_2 = ParseOutcome(
        data=outcome_regex.data,
        source="llm",
        provider="openrouter",
        provider_label="OpenRouter",
        model="nvidia/nemotron-3.5-super-vl:free",
        llm_mode="prompt-json",
        usage=TokenUsage(
            prompt_tokens=700,
            completion_tokens=210,
            total_tokens=910,
            provider="openrouter",
            provider_label="OpenRouter",
            model="nvidia/nemotron-3.5-super-vl:free",
        ),
    )
    app._apply_parsed(llm_outcome_2)
    tok_line_2 = app.tokens_var.get()
    assert "700" in tok_line_2 and "210" in tok_line_2
    assert "Сессия (2 выз.)" in tok_line_2
    assert "1,312" in tok_line_2  # 612 + 700
    assert "[Токены]" in app.log_text()

    closure_text = app._closure_text(llm_outcome_2)
    assert closure_text.startswith("Closure Error: 0.000 ft")
    assert "периметр 411.00 ft" in closure_text

    # Лог открывается и закрывается без падения
    app._show_log()
    app._on_close()


def test_gui_save_and_reload_roundtrip(gui, tmp_path, monkeypatch):
    """
    Сохранение настроек и .env в GUI записывает ТОЛЬКО защищённые токены tsai1:…
    (открытые ключи и PIN на диск не попадают), а реальные ключи пересчитываются в RAM.
    """
    import llm_parser
    from llm_parser import KeyProtectionError, is_protected_key, resolve_api_target

    cfg_file = tmp_path / "config.json"
    local_file = tmp_path / "config.local.json"
    env_file = tmp_path / ".env"
    monkeypatch.setattr(llm_parser, "CONFIG_PATH", cfg_file)
    monkeypatch.setattr(llm_parser, "LOCAL_CONFIG_PATH", local_file)
    monkeypatch.setattr(llm_parser, "ENV_PATH", env_file)
    monkeypatch.setattr(gui, "CONFIG_PATH", cfg_file)
    monkeypatch.setattr(gui, "LOCAL_CONFIG_PATH", local_file)
    monkeypatch.setattr(gui, "ENV_PATH", env_file)

    app1 = gui.SurveyApp()
    app1.key_secret_var.set("pin-4815")
    app1.provider_key_vars["hydra"].set("hk-saved-plain$$")
    app1.provider_key_vars["openrouter"].set("or-saved-plain")
    app1.provider_key_vars["routerai"].set("rai-saved-plain")
    app1.select_provider("OpenRouter")
    app1._on_model_selected("stepfun/step-3.5-flash:free")
    app1._save_settings()
    app1._save_protected_env_clicked()
    app1._on_close()

    # Проверяем, что ни в config.json, ни в .env нет ни одного открытого ключа и PIN
    for raw_disk_text in (cfg_file.read_text("utf-8"), env_file.read_text("utf-8")):
        assert "hk-saved-plain" not in raw_disk_text
        assert "or-saved-plain" not in raw_disk_text
        assert "rai-saved-plain" not in raw_disk_text
        assert "pin-4815" not in raw_disk_text
        assert "tsai1:" in raw_disk_text

    app2 = gui.SurveyApp()
    assert app2.current_provider_id() == "openrouter"
    assert app2.current_model() == "stepfun/step-3.5-flash:free"
    for pid in ("hydra", "openrouter", "routerai"):
        assert is_protected_key(app2.provider_key_vars[pid].get())
        assert "🔒" in app2.provider_key_labels[pid].cget("text")

    # Без PIN защищённый ключ не раскрывается
    cfg_rt, _, _, _, empty_secret = app2._build_runtime_config()
    with pytest.raises(KeyProtectionError):
        resolve_api_target(cfg_rt, provider="openrouter", key_secret=empty_secret)

    # С верным PIN все три ключа пересчитываются в памяти в точные оригиналы
    app2.key_secret_var.set("pin-4815")
    cfg_rt2, _, _, _, secret2 = app2._build_runtime_config()
    assert resolve_api_target(cfg_rt2, provider="hydra", key_secret=secret2)["api_key"] == "hk-saved-plain"
    assert resolve_api_target(cfg_rt2, provider="openrouter", key_secret=secret2)["api_key"] == "or-saved-plain"
    assert resolve_api_target(cfg_rt2, provider="routerai", key_secret=secret2)["api_key"] == "rai-saved-plain"

    # Смена PIN без повторного ввода открытых ключей
    app2._last_valid_secret = "pin-4815"
    app2.key_secret_var.set("new-pin-9999")
    app2._save_settings()
    cfg_rt3, _, _, _, secret3 = app2._build_runtime_config()
    assert resolve_api_target(cfg_rt3, provider="openrouter", key_secret=secret3)["api_key"] == "or-saved-plain"
    app2._on_close()


def test_gui_nyc_presets_and_vision_pdf_flow(gui, tmp_path, monkeypatch):
    """Проверка встроенных примеров NYC и загрузки векторного PDF без текстового слоя."""
    pymupdf = pytest.importorskip("pymupdf")
    from tkinter import filedialog

    from llm_parser import ParseOutcome, TokenUsage, parse_with_regex

    vec_pdf = tmp_path / "63-01_Borden_Ave.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=1008)
    shape = page.new_shape()
    shape.draw_rect(pymupdf.Rect(100, 150, 300, 500))
    shape.finish(color=(0, 0, 0), width=1.0)
    shape.commit()
    doc.save(vec_pdf)
    doc.close()

    app = gui.SurveyApp()
    app._on_sample_selected("63-01 Borden Ave (Queens — без румбов)")
    sample_text = app.text_box.get("1.0", "end")
    assert "Borden Avenue" in sample_text
    desc, _ = parse_with_regex(sample_text)

    monkeypatch.setattr(filedialog, "askopenfilename", lambda **_: str(vec_pdf))
    app.use_llm_var.set(False)
    app._load_pdf()
    assert app._pdf_needs_vision is True
    assert app.use_llm_var.get() is True
    assert "PDF Vision:" in app.pdf_label.cget("text")
    # Старый текст примера очищен, чтобы не загрязнять Vision-запрос другого участка
    assert app.text_box.get("1.0", "end").strip() == ""

    vision_outcome = ParseOutcome(
        data=desc,
        source="llm-vision",
        usage=TokenUsage(
            prompt_tokens=1850,
            completion_tokens=240,
            total_tokens=2090,
            provider="hydra",
            provider_label="Hydra AI",
            model="hydra-gemini-pro",
        ),
        provider="hydra",
        provider_label="Hydra AI",
        model="hydra-gemini-pro",
        llm_mode="json_schema",
    )
    app._apply_parsed(vision_outcome)
    summary = app._parsed_summary(vision_outcome)
    assert "Vision LLM: Hydra AI · hydra-gemini-pro" in summary
    assert "Block 2366 Lot 59 (Queens, NY)" in summary
    assert "Угол с 63rd Street" in summary
    assert "1,850" in app.tokens_var.get()
    app._on_close()


def test_gui_neighbor_mode_and_batch_neighbor_files(gui, tmp_path, monkeypatch):
    """Сначала рассчитываем схему, затем рисуем готовые основной и соседние планы."""
    from tkinter import filedialog, messagebox
    from acad_builder import DrawResult
    from llm_parser import ASPENWALL_SAMPLES

    app = gui.SurveyApp()
    assert not app.is_neighbor_mode()
    assert "Начертить схему в AutoCAD" in app.draw_button.cget("text")
    app.neighbor_lot_radio.select()
    assert app.is_neighbor_mode()
    app._on_sample_selected("Aspenwall St — 420' (Основной участок)")
    assert not app.is_neighbor_mode()

    drawn_calls = []
    def fake_draw(_builder, plan):
        drawn_calls.append((plan.description.distance_to_corner_ft, plan.is_neighbor, plan))
        layers = {"neighbor_lot": "NEIGHBOR_LOT"} if plan.is_neighbor else {"property_line": "PROPERTY_LINE"}
        return DrawResult(plan=plan, text_count=len(plan.items), layers=layers)

    monkeypatch.setattr(gui.AcadBuilder, "draw_plan", fake_draw)
    monkeypatch.setattr(gui.sys, "platform", "win32")
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **kw: True)
    app.use_llm_var.set(False)
    main_plan = app._preview_main_plan
    app._start(parse_only=False)
    _wait_for_gui_worker(app)
    assert drawn_calls == [(420.0, False, main_plan)]

    app._on_sample_selected("Aspenwall St — 365' (Сосед слева)")
    neighbor = app._preview_neighbor_plans[0]
    app._start(parse_only=False, force_neighbor=True)
    _wait_for_gui_worker(app)
    assert drawn_calls[-1] == (365.0, True, neighbor)

    app._clear_neighbors_scene()
    f_left, f_right = tmp_path / "neighbor_365.txt", tmp_path / "neighbor_475.txt"
    f_left.write_text(ASPENWALL_SAMPLES["Aspenwall St — 365' (Сосед слева)"], encoding="utf-8")
    f_right.write_text(ASPENWALL_SAMPLES["Aspenwall St — 475' (Сосед справа)"], encoding="utf-8")
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **_: (str(f_left), str(f_right)))
    before = len(drawn_calls)
    app._load_neighbor_files()
    _wait_for_gui_worker(app)
    assert len(drawn_calls) == before  # выбор файлов и анализ не рисуют
    assert len(app._preview_neighbor_plans) == 2
    app._start(parse_only=False, force_neighbor=True)
    _wait_for_gui_worker(app)
    assert [(d, role) for d, role, _ in drawn_calls[-2:]] == [(365.0, True), (475.0, True)]
    assert "Начерчено участков: 2" in app.status_var.get()
    app._on_close()


def test_gui_multipass_consensus_conflict_and_retry_flow(gui, monkeypatch, tmp_path):
    """
    Проверяет полный цикл 2–3 независимых проходов LLM в GUI:
      1) переключение 1 / 2 / 3 проходов и выбор любых провайдеров/моделей (в т.ч. одинаковых);
      2) случай полного совпадения ответов (2/2 проходов) -> без конфликта;
      3) случай расхождения ответов -> сигнализация конфликта + окно сравнения +
         кнопка «Попробуй разобраться ещё раз» с инъекцией спорного места -> модели сходятся;
      4) ручное решение конфликта человеком («Прав Проход #2») -> отрисовка в AutoCAD.
    """
    import time
    import llm_parser
    from llm_parser import ASPENWALL_SAMPLES, TokenUsage, parse_with_regex

    cfg_file = tmp_path / "config.json"
    local_file = tmp_path / "config.local.json"
    env_file = tmp_path / ".env"
    monkeypatch.setattr(llm_parser, "CONFIG_PATH", cfg_file)
    monkeypatch.setattr(llm_parser, "LOCAL_CONFIG_PATH", local_file)
    monkeypatch.setattr(llm_parser, "ENV_PATH", env_file)
    monkeypatch.setattr(gui, "CONFIG_PATH", cfg_file)
    monkeypatch.setattr(gui, "LOCAL_CONFIG_PATH", local_file)
    monkeypatch.setattr(gui, "ENV_PATH", env_file)

    app = gui.SurveyApp()
    assert app.current_passes_count() == 1
    app.select_passes(3)
    assert app.current_passes_count() == 3
    app.select_pass2_target("OpenRouter", "nvidia/nemotron-3.5-super-vl:free")
    app.select_pass3_target("RouterAI", "openai/gpt-4.1")
    app.provider_key_vars["hydra"].set("sk-hydra-test")
    app.provider_key_vars["openrouter"].set("sk-or-test")
    app.provider_key_vars["routerai"].set("sk-rai-test")
    app._save_settings()

    saved_cfg = llm_parser.load_config()
    assert saved_cfg["api"]["consensus"]["passes"] == 3
    assert saved_cfg["api"]["consensus"]["pass2_provider"] == "openrouter"
    assert saved_cfg["api"]["consensus"]["pass3_provider"] == "routerai"

    # Переключаем в режим 2 проходов (одна и та же модель или две разные)
    app.select_passes(2)
    app.select_provider("Hydra AI")
    app.select_pass2_target("OpenRouter", "nvidia/nemotron-3.5-super-vl:free")
    sample_text = ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"]
    app.text_box.delete("1.0", "end")
    app.text_box.insert("1.0", sample_text)
    app.use_llm_var.set(True)

    clean_desc, _ = parse_with_regex(sample_text)
    # Вариант с расхождением привязки POB (425' вместо 420')
    alt_desc = clean_desc.model_copy(update={"distance_to_corner_ft": 425.0})

    call_log: list[tuple[str, str, str | None]] = []

    # --- Сценарий А: оба прохода сразу согласны ---
    def fake_extract_agree(text, cfg, api_key=None, progress=None, *, provider=None, model=None, key_secret="", image_data_urls=None, focus_instruction=None):
        call_log.append((str(provider), str(model), focus_instruction))
        u = TokenUsage(prompt_tokens=120, completion_tokens=60, total_tokens=180, provider=str(provider), provider_label=str(provider), model=str(model))
        return clean_desc, "structured-outputs", u

    monkeypatch.setattr(llm_parser, "_extract_with_llm", fake_extract_agree)
    app._start(parse_only=True)
    for _ in range(40):
        app._drain_events()
        if not app._busy:
            break
        time.sleep(0.02)

    assert app.pending_conflict is None
    assert len(call_log) == 2
    assert "Консенсус 2/2" in app.status_var.get()
    assert app._session_llm_calls == 2

    # --- Сценарий Б: проходы расходятся -> жмём «Попробуй разобраться ещё раз» ---
    call_log.clear()

    def fake_extract_conflict_then_converge(text, cfg, api_key=None, progress=None, *, provider=None, model=None, key_secret="", image_data_urls=None, focus_instruction=None):
        call_log.append((str(provider), str(model), focus_instruction))
        u = TokenUsage(prompt_tokens=150, completion_tokens=70, total_tokens=220, provider=str(provider), provider_label=str(provider), model=str(model))
        # В раунде #1 OpenRouter ошибается (425.00'), а в раунде #2 с фокусным промптом исправляется (420.00')!
        if focus_instruction is None and provider == "openrouter":
            return alt_desc, "structured-outputs", u
        return clean_desc, "structured-outputs", u

    monkeypatch.setattr(llm_parser, "_extract_with_llm", fake_extract_conflict_then_converge)
    app._start(parse_only=True)
    for _ in range(40):
        app._drain_events()
        if not app._busy:
            break
        time.sleep(0.02)

    # Конфликт зафиксирован и показан пользователю!
    assert app.pending_conflict is not None
    assert app.conflict_window is not None
    assert "Расстояние от перекрёстка улиц до POB" in app.status_var.get()
    assert "420.00'" in app.status_var.get() and "425.00'" in app.status_var.get()

    # Пользователь жмёт «Попробуй разобраться ещё раз»
    app.retry_conflict_resolution()
    for _ in range(40):
        app._drain_events()
        if not app._busy:
            break
        time.sleep(0.02)

    assert app.pending_conflict is None
    assert len(call_log) == 4  # 2 вызова в раунде #1 + 2 вызова в раунде #2
    assert call_log[2][2] is not None and "CRITICAL RE-VERIFICATION TASK" in call_log[2][2]
    assert "Расстояние от перекрёстка улиц до POB" in call_log[2][2]
    assert "Конфликт разрешён на повторной проверке" in app.status_var.get()
    assert app.description is not None
    assert app.description.distance_to_corner_ft == pytest.approx(420.0)

    # --- Сценарий В: конфликт разрешается при анализе, отрисовка отдельна ---
    from tkinter import messagebox
    from acad_builder import DrawResult
    drawn_plans = []
    def fake_draw(_builder, plan):
        drawn_plans.append(plan)
        return DrawResult(plan=plan, text_count=len(plan.items), layers={"property_line": "PROPERTY_LINE"})

    monkeypatch.setattr(gui.AcadBuilder, "draw_plan", fake_draw)
    monkeypatch.setattr(gui.sys, "platform", "win32")
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **kw: True)
    app.provider_key_vars["hydra"].set("sk-hydra-test")
    app.provider_key_vars["openrouter"].set("sk-or-test")
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    assert app.pending_conflict is not None, app.status_var.get()
    calls_before_draw = len(call_log)
    app._start(parse_only=False)
    assert drawn_plans == [] and len(call_log) == calls_before_draw
    assert "Сначала разрешите конфликт" in app.status_var.get()

    app.resolve_conflict_with_pass(1)
    _wait_for_gui_worker(app)
    assert app.pending_conflict is None and drawn_plans == []
    chosen_plan = app._preview_main_plan
    assert chosen_plan.description.distance_to_corner_ft == pytest.approx(420.0)
    app._start(parse_only=False)
    _wait_for_gui_worker(app)
    assert drawn_plans == [chosen_plan]
    assert len(call_log) == calls_before_draw
    app._on_close()


def test_gui_studio_ux_drawer_preview_canvas_and_inspector(gui, monkeypatch, tmp_path):
    """
    Проверяет UX редизайна Dark CAD Studio (v0.7.0):
      * автооткрытие и автосворачивание ящика API-ключей + счётчик ключей;
      * переключение «ИИ-разбор (LLM)» <-> локальный офлайн-парсер (Regex-бейдж);
      * живой векторный CAD-предпросмотр от (0, 0) (основной лот + сосед + GAP-маркер)
        и структурированную ведомость сторон в правой панели.
    """
    import llm_parser
    from acad_builder import AcadBuilder
    from llm_parser import LegalDescription, Segment

    cfg_file = tmp_path / "config.json"
    local_file = tmp_path / "config.local.json"
    env_file = tmp_path / ".env"
    monkeypatch.setattr(llm_parser, "CONFIG_PATH", cfg_file)
    monkeypatch.setattr(llm_parser, "LOCAL_CONFIG_PATH", local_file)
    monkeypatch.setattr(llm_parser, "ENV_PATH", env_file)
    monkeypatch.setattr(gui, "CONFIG_PATH", cfg_file)
    monkeypatch.setattr(gui, "LOCAL_CONFIG_PATH", local_file)
    monkeypatch.setattr(gui, "ENV_PATH", env_file)
    for env_var in ("HYDRA_API_KEY", "OPENROUTER_API_KEY", "ROUTERAI_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(env_var, raising=False)

    app = gui.SurveyApp()
    # При чистом запуске без ключей ящик API-ключей открыт сразу
    assert app._keys_drawer_open is True
    assert "Ввести API-ключи" in app.keys_toggle_button.cget("text")

    # После сохранения ключа ящик автоматически сворачивается, освобождая рабочее поле
    app.provider_key_vars["hydra"].set("sk-hydra-studio")
    app._save_settings()
    assert app._keys_drawer_open is False
    assert "готовы (1)" in app.keys_toggle_button.cget("text")

    app.toggle_keys_drawer()
    assert app._keys_drawer_open is True
    app.toggle_keys_drawer()
    assert app._keys_drawer_open is False

    # Выключение ИИ-разбора показывает понятный индикатор локального Regex-режима
    app.select_passes(3)
    assert app.pass2_row._gridded is True
    assert app.pass3_row._gridded is True
    app.use_llm_var.set(False)
    app._on_use_llm_toggled()
    assert app.pass2_row._gridded is False
    assert app.pass3_row._gridded is False
    assert app.regex_mode_badge._gridded is True
    app.use_llm_var.set(True)
    app._on_use_llm_toggled()
    assert app.regex_mode_badge._gridded is False
    assert app.pass3_row._gridded is True

    # Выбор примеров основного и соседнего участков мгновенно строит живую схему от (0, 0)
    app._on_sample_selected("Aspenwall St — 420' (Основной участок)")
    assert app._preview_main_plan is not None
    inspector_txt = app.inspector_box.get()
    assert "ОСНОВНОЙ УЧАСТОК" in inspector_txt
    assert "Стороны контура (4 отр. LINE)" in inspector_txt
    assert app.closure_label.cget("text_color") == gui.THEME["ok"]

    app._on_sample_selected("Aspenwall St — 365' (Сосед слева)")
    assert len(app._preview_neighbor_plans) == 1
    assert app.preview_canvas is not None
    canvas_texts = [
        item[2].get("text", "")
        for item in app.preview_canvas.items
        if item[0] == "text"
    ]
    assert "(0,0)" in canvas_texts
    assert "POB" in canvas_texts
    assert "СОСЕД" in canvas_texts
    assert "#1" in canvas_texts and "#4" in canvas_texts

    # Участок с открытым гэпом рисует маркер GAP на схеме и подсвечивает невязку янтарным цветом
    gap_desc = LegalDescription(
        main_street="Oak Ave",
        cross_street="1st St",
        distance_to_corner_ft=50.0,
        segments=[
            Segment(ns="N", degrees=90, ew="E", distance_ft=100.0),
            Segment(ns="S", degrees=0, ew="E", distance_ft=50.0),
            Segment(ns="S", degrees=90, ew="W", distance_ft=99.65),
            Segment(ns="N", degrees=0, ew="W", distance_ft=50.0),
        ],
    )
    gap_plan = AcadBuilder(app.config_data).plan(gap_desc)
    app._record_plan_for_preview(gap_plan)
    app._update_closure_badge(gap_plan.closure.short_report(), gap_plan.closure.distance)
    gap_texts = [
        item[2].get("text", "")
        for item in app.preview_canvas.items
        if item[0] == "text"
    ]
    assert any(t.startswith("GAP ") for t in gap_texts)
    assert app.closure_label.cget("text_color") == gui.THEME["warn"]

    # Сброс сцены очищает предпросмотр и ведомость
    app._clear_preview_scene()
    assert app._preview_main_plan is None
    assert app._preview_neighbor_plans == []
    assert "Ведомость участка пока пуста" in app.inspector_box.get()
    app._on_close()


def test_empty_photo_tab_offers_first_file_and_tracks_source(gui, monkeypatch, tmp_path):
    from tkinter import filedialog
    from PIL import Image
    import llm_parser

    monkeypatch.setattr(gui, "load_config", lambda: llm_parser.load_config(tmp_path / "config.json"))
    monkeypatch.setattr(gui, "_stored_provider_keys", lambda: {})
    app = gui.SurveyApp()
    app.source_mode_var.set("📷 Фото")
    app._on_source_mode_changed()
    assert app.photo_empty_card._gridded is True
    assert app.photo_load_button._gridded is True
    assert app.visual_bar._gridded is False
    assert app.pdf_mode_card._gridded is False

    # Отмена диалога не убирает кнопку; выбор файла показывает инструменты.
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **_: ())
    app.photo_load_button.cget("command")()
    assert app.photo_empty_card._gridded is True
    image = tmp_path / "survey.jpg"
    Image.new("RGB", (30, 40), "white").save(image)
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **_: (str(image),))
    app.photo_load_button.cget("command")()
    assert app.image_paths == [image]
    assert app.photo_empty_card._gridded is False
    assert app.visual_bar._gridded is True

    app.source_mode_var.set("📝 Текст")
    app._on_source_mode_changed()
    assert app.photo_empty_card._gridded is False
    assert app.visual_bar._gridded is False
    app.source_mode_var.set("📷 Фото")
    app._on_source_mode_changed()
    assert app.visual_bar._gridded is True
    app._reset_visual_sources()
    assert app.visual_bar._gridded is False
    assert app.photo_empty_card._gridded is True
    app._on_close()


def test_gui_archival_jpg_bundle_rotation_preview_and_neighbor_photos(
    gui, monkeypatch, tmp_path
):
    """
    Проверяет полный GUI-сценарий для фотографий старых чертежей 80-х годов:
      * выбор связки из 2 .jpg файлов через кнопку «📷 Фото (JPG)…»;
      * предпросмотр в режиме «🖼 Исходник», листание кадров 1/2 -> 2/2 и ручной поворот +180°;
      * передачу связки фото, индивидуальных углов поворота [0, 180], фильтра 80-х и
        ИИ-автоповорота в 2-проходный консенсус;
      * пакетную отрисовку соседнего участка из .jpg фотографии.
    """
    import time
    from tkinter import filedialog

    PIL_Image = pytest.importorskip("PIL.Image")
    import llm_parser
    from llm_parser import ASPENWALL_SAMPLES, TokenUsage, parse_with_regex

    cfg_file = tmp_path / "config.json"
    local_file = tmp_path / "config.local.json"
    env_file = tmp_path / ".env"
    monkeypatch.setattr(llm_parser, "CONFIG_PATH", cfg_file)
    monkeypatch.setattr(llm_parser, "LOCAL_CONFIG_PATH", local_file)
    monkeypatch.setattr(llm_parser, "ENV_PATH", env_file)
    monkeypatch.setattr(gui, "CONFIG_PATH", cfg_file)
    monkeypatch.setattr(gui, "LOCAL_CONFIG_PATH", local_file)
    monkeypatch.setattr(gui, "ENV_PATH", env_file)

    jpg1 = tmp_path / "survey_1983_overview.jpg"
    jpg2 = tmp_path / "survey_1983_closeup_upside_down.jpg"
    PIL_Image.new("RGB", (900, 1200), (205, 200, 180)).save(jpg1, format="JPEG")
    PIL_Image.new("RGB", (800, 600), (195, 205, 215)).save(jpg2, format="JPEG")

    app = gui.SurveyApp()
    app.provider_key_vars["hydra"].set("sk-hydra-photo")
    app.provider_key_vars["openrouter"].set("sk-or-photo")

    monkeypatch.setattr(
        filedialog, "askopenfilenames", lambda **_: (str(jpg1), str(jpg2))
    )
    app._load_images()

    assert len(app.image_paths) == 2
    assert app.visual_bar._gridded is True
    assert app.auto_orient_var.get() is False
    assert app.auto_orient_warn_label._gridded is False
    assert app.preview_mode_var.get() == "🖼 Исходник"
    assert "Фото Vision (основной): 2 файл." in app.pdf_label.cget("text")

    # Проверяем HUD предпросмотра первого кадра, затем переходим ко второму и поворачиваем на 180°
    hud_texts_1 = [
        it[2].get("text", "") for it in app.preview_canvas.items if it[0] == "text"
    ]
    assert any("ФОТО 1/2: survey_1983_overview.jpg" in t for t in hud_texts_1)

    app.step_visual_frame(1)
    app.rotate_visual(180)
    assert app._visual_rotations == [0, 180]
    hud_texts_2 = [
        it[2].get("text", "") for it in app.preview_canvas.items if it[0] == "text"
    ]
    assert any("ФОТО 2/2" in t and "+180°" in t for t in hud_texts_2)

    # По умолчанию ИИ-автоповорот ВЫКЛЮЧЕН (0 лишних платных запросов к LLM)
    app.select_passes(2)
    clean_desc, _ = parse_with_regex(ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"])
    orient_called: list[int] = []
    seen_image_counts: list[int] = []

    def fake_detect_orient(urls, cfg, **kwargs):
        orient_called.append(len(urls))
        return [0, 0], TokenUsage(prompt_tokens=180, completion_tokens=20, total_tokens=200, provider="hydra", provider_label="Hydra AI", model="glm-5.2")

    def fake_extract(text, cfg, api_key=None, progress=None, *, provider=None, model=None, key_secret="", image_data_urls=None, focus_instruction=None):
        seen_image_counts.append(len(image_data_urls or []))
        u = TokenUsage(prompt_tokens=900, completion_tokens=110, total_tokens=1010, provider=str(provider), provider_label=str(provider), model=str(model))
        return clean_desc, "structured-outputs", u

    monkeypatch.setattr(llm_parser, "detect_visual_orientations_llm", fake_detect_orient)
    monkeypatch.setattr(llm_parser, "_extract_with_llm", fake_extract)

    # 1) Запуск по умолчанию — без платного предпрохода автоповорота
    app._start(parse_only=True)
    for _ in range(40):
        app._drain_events()
        if not app._busy:
            break
        time.sleep(0.02)

    assert orient_called == []
    assert seen_image_counts == [2, 2]
    # После успешного разбора предпросмотр автоматически переключается на CAD-схему!
    assert app.preview_mode_var.get() == "📐 Схема CAD"
    assert app.description is not None and len(app.description.segments) == 4
    main_label = app._scene_plans()[0][0]
    assert jpg1.name in main_label and jpg2.name in main_label

    # 2) Клик по «🧭 ИИ-поворот (1 раз, $)» с отменой в диалоге предупреждения -> остаётся False
    from tkinter import messagebox

    monkeypatch.setattr(messagebox, "askokcancel", lambda *a, **kw: False)
    app.auto_orient_var.set(True)
    app._on_auto_orient_toggled()
    assert app.auto_orient_var.get() is False
    assert app.auto_orient_warn_label._gridded is False

    # 3) Клик с подтверждением предупреждения -> включается РОВНО НА 1 ЗАПУСК и сам гаснет
    monkeypatch.setattr(messagebox, "askokcancel", lambda *a, **kw: True)
    app.auto_orient_var.set(True)
    app._on_auto_orient_toggled()
    assert app.auto_orient_var.get() is True
    assert app.auto_orient_warn_label._gridded is True

    app._start(parse_only=True)
    # Сразу при старте расчёта разовый флаг обязан сброситься обратно в False!
    assert app.auto_orient_var.get() is False
    assert app.auto_orient_warn_label._gridded is False
    for _ in range(40):
        app._drain_events()
        if not app._busy:
            break
        time.sleep(0.02)
    assert orient_called == [2]

    # Повторный запуск снова идёт БЕЗ платного автоповорота (orient_called не растёт)
    app._start(parse_only=True)
    for _ in range(40):
        app._drain_events()
        if not app._busy:
            break
        time.sleep(0.02)
    assert orient_called == [2]

    # Проверяем пакет соседей с .jpg фотографией соседа (тоже без платного автоповорота по умолчанию)
    neighbor_jpg = tmp_path / "neighbor_1983_365.jpg"
    PIL_Image.new("RGB", (700, 900), (210, 210, 200)).save(neighbor_jpg, format="JPEG")
    neighbor_desc, _ = parse_with_regex(ASPENWALL_SAMPLES["Aspenwall St — 365' (Сосед слева)"])

    def fake_extract_neighbor(text, cfg, api_key=None, progress=None, *, provider=None, model=None, key_secret="", image_data_urls=None, focus_instruction=None):
        u = TokenUsage(prompt_tokens=800, completion_tokens=100, total_tokens=900, provider=str(provider), provider_label=str(provider), model=str(model))
        return neighbor_desc, "structured-outputs", u

    draws = []
    def fake_draw(_builder, plan):
        from acad_builder import DrawResult

        draws.append(plan)
        return DrawResult(plan=plan, text_count=len(plan.items), layers={"neighbor_lot": "NEIGHBOR_LOT"})

    monkeypatch.setattr(llm_parser, "_extract_with_llm", fake_extract_neighbor)
    monkeypatch.setattr(gui.AcadBuilder, "draw_plan", fake_draw)
    monkeypatch.setattr(gui.sys, "platform", "win32")
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **_: (str(neighbor_jpg),))

    app._load_neighbor_files()
    for _ in range(40):
        app._drain_events()
        if not app._busy:
            break
        time.sleep(0.02)

    assert len(app._preview_neighbor_plans) == 1
    assert draws == []
    assert jpg1.name in app._scene_plans()[0][0] and jpg2.name in app._scene_plans()[0][0]
    assert neighbor_jpg.name in app._scene_plans()[1][0]
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **kw: True)
    calls_before_draw = len(seen_image_counts)
    monkeypatch.setattr(llm_parser, "_extract_with_llm", lambda *a, **kw: pytest.fail("повторный запрос к ИИ"))
    app._start(parse_only=False, force_neighbor=True)
    _wait_for_gui_worker(app)
    assert draws == app._preview_neighbor_plans
    assert len(seen_image_counts) == calls_before_draw
    assert orient_called == [2]  # пакет соседей по умолчанию НЕ делает платный автоповорот
    app._on_close()


def test_gui_neighbor_verification_button_alerts_canvas_and_report_modal(
    gui, monkeypatch, tmp_path
):
    """
    Проверяет сквозную работу «🔍 Сверка с соседями» в GUI без открытого AutoCAD:
      * выбор пресета основного участка 420' -> нажатие «🔍 Сверка с соседями»;
      * выбор двух файлов соседей: левый 365' (чистый) и правый 475' с ошибкой секунд
        (40" -> 49" на общей меже) и опечаткой длины (100.00' -> 108.00' на общей меже);
      * автоматическое оповещение юзера в статусной строке, блоке правой ведомости,
        бейдже кнопки и визуальной подсветке на preview_canvas («✓ межа» и «⚠ Δ∠…»).
    """
    import time
    from tkinter import filedialog

    from llm_parser import ASPENWALL_SAMPLES

    app = gui.SurveyApp()
    app.use_llm_var.set(False)
    app._on_sample_selected("Aspenwall St — 420' (Основной участок)")
    assert app._preview_main_plan is not None
    assert app.verify_neighbors_button.cget("text") == "🔍 Сверка с соседями"

    # Левый сосед — эталонный, правый сосед — с секундной ошибкой 40"->49" и длиной 108.00'
    nb_left_txt = ASPENWALL_SAMPLES["Aspenwall St — 365' (Сосед слева)"]
    nb_right_broken_txt = (
        ASPENWALL_SAMPLES["Aspenwall St — 475' (Сосед справа)"]
        .replace('S 14°39\'40" E, 100.00 feet', 'S 14°39\'49" E, 108.00 feet')
        .replace('N 14°39\'40" W, 100.00 feet', 'N 14°39\'49" W, 108.00 feet')
    )
    f_left = tmp_path / "nb_365_clean.txt"
    f_right = tmp_path / "nb_475_typo.txt"
    f_left.write_text(nb_left_txt, encoding="utf-8")
    f_right.write_text(nb_right_broken_txt, encoding="utf-8")

    monkeypatch.setattr(
        filedialog, "askopenfilenames", lambda **_: (str(f_left), str(f_right))
    )
    app._on_verify_neighbors_clicked()
    for _ in range(40):
        app._drain_events()
        if not app._busy:
            break
        time.sleep(0.02)

    assert app._neighbor_report is not None
    assert app._neighbor_report.has_issues is True
    assert "⚠ расх." in app.verify_neighbors_button.cget("text")

    status_txt = app.status_var.get()
    assert "обнаружено расхождение" in status_txt
    assert ("0 ↔ 9" in status_txt) or ("0 ↔ 8" in status_txt) or ("секунд" in status_txt)

    insp_txt = app.inspector_box.get("1.0", "end")
    assert "🔍 СВЕРКА С СОСЕДНИМИ УЧАСТКАМИ" in insp_txt
    assert "0 ↔ 9" in insp_txt  # секунды 40" против 49"
    assert "0 ↔ 8" in insp_txt  # длина 100.00' против 108.00'

    canvas_texts = [
        it[2].get("text", "") for it in app.preview_canvas.items if it[0] == "text"
    ]
    assert any("✓ межа" in t for t in canvas_texts)
    assert any("⚠ " in t and "ΔL8.00'" in t for t in canvas_texts)

    # Повторный клик при уже загруженных соседях открывает модальный акт без перезапроса файлов
    app._on_verify_neighbors_clicked()
    app._on_close()


def test_gui_lot_suspicion_signal_on_closed_skewed_rectangle(gui) -> None:
    """GUI сразу сигнализирует о подозрительном «почти прямоугольнике», даже когда Closure Error = 0.000 ft!"""
    import time
    from llm_parser import ASPENWALL_SAMPLES

    app = gui.SurveyApp()
    app.use_llm_var.set(False)

    # 1) Эталонный прямоугольник — зелёный значок «🔎 Форма: прямоугольник ✓»
    clean_txt = ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"]
    app.text_box.delete("1.0", "end")
    app.text_box.insert("1.0", clean_txt)
    app._start(parse_only=True)
    for _ in range(40):
        app._drain_events()
        if not app._busy:
            break
        time.sleep(0.02)

    assert app._last_suspicion_report is not None
    assert app._last_suspicion_report.is_suspicious is False
    assert "Фигур: 1 · замечаний: 0" in app.suspicion_badge_button.cget("text")

    # 2) Скошенный «почти прямоугольник» с ошибкой заёма 1' (14°40'40" вместо 14°39'40" на обеих боковых межах):
    # Closure Error идеально равен 0.000 ft, но углы 89°59'00" / 90°01'00"!
    skewed_txt = clean_txt.replace("S 14°39'40\" E", "S 14°40'40\" E").replace(
        "N 14°39'40\" W", "N 14°40'40\" W"
    )
    app.text_box.delete("1.0", "end")
    app.text_box.insert("1.0", skewed_txt)
    app._start(parse_only=True)
    for _ in range(40):
        app._drain_events()
        if not app._busy:
            break
        time.sleep(0.02)

    assert "0.000 ft" in app.closure_var.get()
    assert app._last_suspicion_report is not None
    assert app._last_suspicion_report.is_suspicious is True
    assert "замечаний:" in app.suspicion_badge_button.cget("text")
    assert app.suspicion_badge_button.cget("fg_color") == "#dc2626"

    # Сигнал сразу виден и в нижней строке статуса, и в инспекторе, и на холсте предпросмотра
    status_txt = app.status_var.get()
    assert "Подозрительная геометрия участка" in status_txt
    assert "почти прямоугольник" in status_txt

    insp_txt = app.inspector_box.get("1.0", "end")
    assert "89°59'00\"" in insp_txt
    assert "заёма 1 минуты" in insp_txt

    canvas_texts = [
        it[2].get("text", "") for it in app.preview_canvas.items if it[0] == "text"
    ]
    assert "⚠ #1" in canvas_texts

    # Открытие окна диагностики геометрии работает без ошибок
    app._open_lot_suspicion_window()
    app._on_close()


def test_gui_ask_on_doubt_checkbox_prevents_silent_54_to_56_and_supports_focus_recheck(
    gui, monkeypatch
) -> None:
    """
    Сквозной GUI-тест защиты от самовольной подмены цифр (кейс «54 в PDF -> нельзя молча менять на 56»):
      1) галочка «Спрашивать при сомнении» по умолчанию включена (True);
      2) обе LLM в 2-проходном режиме читают 54.00' (при фасаде 56.00') ->
         AutoCAD НЕ чертится без спроса, открывается панель/окно сомнения;
      3) нажатие «🔬 Ещё раунд LLM с фокусом на зону внимания» отправляет инъекцию промпта на сторону #2;
      4) нажатие «📄 Оставить как в документе» чертит именно паспортные 54.00', а «✏️ Применить» — 56.00';
      5) при снятой галочке «Спрашивать при сомнении» 54.00' ВСЁ РАВНО не превращается в 56.00'!
    """
    import llm_parser
    from acad_builder import DrawResult
    from llm_parser import ASPENWALL_SAMPLES, TokenUsage, parse_with_regex

    app = gui.SurveyApp()
    assert app.ask_on_doubt_var.get() is True

    dm, _ = parse_with_regex(ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"])
    d_54 = dm.model_copy(deep=True)
    d_54.segments[1] = d_54.segments[1].model_copy(update={"distance_ft": 54.0})
    d_54.segments[3] = d_54.segments[3].model_copy(update={"distance_ft": 56.0})

    focus_seen: list[str | None] = []

    def fake_extract(
        text,
        cfg_arg,
        *,
        api_key=None,
        progress=None,
        provider=None,
        model=None,
        key_secret="",
        image_data_urls=None,
        focus_instruction=None,
    ):
        focus_seen.append(focus_instruction)
        m = d_54.model_copy(deep=True)
        if focus_instruction:
            m.anomaly_note = "В PDF чётко напечатано 54.00', это не опечатка OCR."
        return (
            m,
            "structured-outputs",
            TokenUsage(
                prompt_tokens=120,
                completion_tokens=60,
                total_tokens=180,
                provider=str(provider),
                model=str(model),
                available=True,
            ),
        )

    drawn_rear_lengths: list[float] = []

    def fake_draw(_builder, plan):
        drawn_rear_lengths.append(plan.description.segments[1].distance_ft)
        return DrawResult(
            plan=plan,
            text_count=len(plan.items),
            layers={"property_line": "PROPERTY_LINE", "boundary_text": "BOUNDARY_TEXT_AI"},
        )

    monkeypatch.setattr(llm_parser, "_extract_with_llm", fake_extract)
    monkeypatch.setattr(gui.AcadBuilder, "draw_plan", fake_draw)
    monkeypatch.setattr(gui.sys, "platform", "win32")
    from tkinter import messagebox
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **kw: True)

    app.use_llm_var.set(True)
    app.passes_label_var.set("2 прохода")
    app._on_passes_changed("2 прохода")
    app.provider_key_vars["hydra"].set("sk-hydra-test")
    app.provider_key_vars["openrouter"].set("sk-or-test")
    app.text_box.delete("1.0", "end")
    app.text_box.insert("1.0", ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"])

    # Сомнение обнаруживается при анализе, а не при нажатии отрисовки.
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    assert drawn_rear_lengths == []
    assert app.pending_doubt is not None
    assert "54.00'" in app.doubt_title_var.get() and "56.00'" in app.doubt_title_var.get()
    calls_before_draw = len(focus_seen)
    app._start(parse_only=False)
    assert app.pending_doubt is not None and len(focus_seen) == calls_before_draw

    focus_seen.clear()
    app.retry_doubt_with_focus()
    _wait_for_gui_worker(app)
    assert len(focus_seen) == 2
    assert all(f and "TARGETED ATTENTION-ZONE VERIFICATION" in f and "SIDE #2" in f for f in focus_seen)
    assert app.pending_doubt is not None
    assert "В PDF чётко напечатано 54.00'" in app.status_var.get()

    app.confirm_doubt_keep_verbatim()
    assert app.pending_doubt is None and drawn_rear_lengths == []
    calls_before_draw = len(focus_seen)
    app._start(parse_only=False)
    _wait_for_gui_worker(app)
    assert drawn_rear_lengths == [pytest.approx(54.0)]
    assert len(focus_seen) == calls_before_draw

    # Повторный анализ даёт возможность вручную применить исправление 56'.
    drawn_rear_lengths.clear()
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    assert app.pending_doubt is not None
    app.confirm_doubt_apply_fix()
    assert drawn_rear_lengths == []
    calls_before_draw = len(focus_seen)
    app._start(parse_only=False)
    _wait_for_gui_worker(app)
    assert drawn_rear_lengths == [pytest.approx(56.0)]
    assert len(focus_seen) == calls_before_draw

    # Выключение вопроса не даёт молча менять согласованное значение 54'.
    drawn_rear_lengths.clear()
    app.ask_on_doubt_var.set(False)
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    assert app.pending_doubt is None
    calls_before_draw = len(focus_seen)
    app._start(parse_only=False)
    _wait_for_gui_worker(app)
    assert drawn_rear_lengths == [pytest.approx(54.0)]
    assert len(focus_seen) == calls_before_draw
    app._on_close()


def test_gui_v1_studio_viewport_zoom_pan_hover_clipboard_and_shortcuts(
    gui, tmp_path, monkeypatch
):
    """v1.0 Studio UI: зум/панорама/наведение на холсте, масштабная линейка, буфер обмена и хоткеи."""
    import time
    from PIL import Image

    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(
        gui,
        "load_config",
        lambda path=cfg_path: __import__("llm_parser").load_config(path),
    )
    monkeypatch.setattr(
        gui,
        "save_config",
        lambda data, path=cfg_path: __import__("llm_parser").save_config(data, path),
    )

    app = gui.SurveyApp()
    assert gui.VERSION == "1.0.0"
    assert gui.APP_GEOMETRY == "1440x940"
    assert "<Control-Return>" in app._global_bindings
    assert "<F5>" in app._global_bindings
    assert "<Control-o>" in app._global_bindings
    assert "<Control-i>" in app._global_bindings
    assert "<Control-s>" in app._global_bindings

    # 1. Выбираем встроенный участок Aspenwall -> строится CAD-схема с сеткой и масштабной линейкой
    app._on_sample_selected("Aspenwall St — 420' (Основной участок)")
    assert app._last_world_transform is not None
    canvas_texts = [item[2].get("text", "") for item in app.preview_canvas.items if item[0] == "text"]
    assert any(t.endswith(" ft") for t in canvas_texts)

    # 2. Зум колесом мыши и перетаскивание ЛКМ
    ev_wheel = types.SimpleNamespace(delta=120, x=230, y=140)
    app.preview_canvas._bindings["<MouseWheel>"](ev_wheel)
    app.preview_canvas._bindings["<MouseWheel>"](ev_wheel)
    assert app._canvas_zoom > 1.4
    assert "×" in app.zoom_reset_button.cget("text")

    app.preview_canvas._bindings["<ButtonPress-1>"](types.SimpleNamespace(x=100, y=100))
    app.preview_canvas._bindings["<B1-Motion>"](types.SimpleNamespace(x=135, y=120))
    app.preview_canvas._bindings["<ButtonRelease-1>"](types.SimpleNamespace(x=135, y=120))
    assert app._canvas_pan_x != 0.0
    assert app._canvas_pan_y != 0.0

    # 3. Наведение курсора точно на середину стороны #1 -> живые координаты XY и подсветка стороны #1
    cx_w, cy_w, eff_scale, cw, ch = app._last_world_transform
    bl1 = app._preview_main_plan.boundary_lines[0]
    mid_wx = 0.5 * (bl1.start[0] + bl1.end[0])
    mid_wy = 0.5 * (bl1.start[1] + bl1.end[1])
    sx_mid = cw * 0.5 + app._canvas_pan_x + (mid_wx - cx_w) * eff_scale
    sy_mid = ch * 0.5 + app._canvas_pan_y - (mid_wy - cy_w) * eff_scale

    app.preview_canvas._bindings["<Motion>"](types.SimpleNamespace(x=sx_mid, y=sy_mid))
    assert app._hovered_seg_idx == 1
    assert app._hover_world_pt is not None
    hover_texts = [item[2].get("text", "") for item in app.preview_canvas.items if item[0] == "text"]
    assert any("XY (" in t and "#1:" in t for t in hover_texts)

    app.preview_canvas._bindings["<Leave>"](types.SimpleNamespace())
    assert app._hovered_seg_idx is None

    # Двойной клик возвращает масштаб 1:1
    app.preview_canvas._bindings["<Double-Button-1>"](types.SimpleNamespace())
    assert app._canvas_zoom == pytest.approx(1.0)
    assert app._canvas_pan_x == pytest.approx(0.0)
    assert app.zoom_reset_button.cget("text") == "1:1"

    # 4. Копирование ведомости в буфер обмена одной кнопкой
    copied = app.copy_inspector_report()
    assert "ASPENWALL" in copied.upper()
    assert getattr(app, "_clipboard", "") == copied
    assert "буфер обмена" in app.status_var.get()

    # 5. Лупа в режиме «🖼 Исходник» повышает разрешение рендера архивного фото 80-х
    photo_p = tmp_path / "scan_80s.jpg"
    Image.new("RGB", (1600, 1200), color=(235, 232, 220)).save(photo_p, format="JPEG")
    app.image_paths = [photo_p]
    app._visual_rotations = [0]
    app._refresh_visual_bar()
    app._set_preview_mode("🖼 Исходник")
    app.zoom_viewport(2.0, pivot_x=200, pivot_y=140)
    src_texts = [item[2].get("text", "") for item in app.preview_canvas.items if item[0] == "text"]
    assert any("200%" in t for t in src_texts)

    # 6. Памятка клавиш и хоткей Ctrl+Return (офлайн-проверка без обращения к LLM)
    app._open_shortcuts_window()
    assert app.shortcuts_window is not None
    app._reset_visual_sources()
    app.use_llm_var.set(False)
    app._on_use_llm_toggled()
    app._global_bindings["<Control-Return>"]()
    for _ in range(30):
        app._drain_events()
        if not app._busy:
            break
        time.sleep(0.01)
    assert app._preview_main_plan is not None
    app._on_close()


def _write_test_pdf(path, pages):
    """Небольшие настоящие PDF: текстовый слой без внешних фикстур."""
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    try:
        for content in pages:
            page = doc.new_page()
            if content:
                remaining = page.insert_textbox(pymupdf.Rect(25, 25, 575, 800), content, fontsize=9)
                assert remaining >= 0
        doc.save(path)
    finally:
        doc.close()


def _wait_for_gui_worker(app, timeout=8):
    import time

    end = time.monotonic() + timeout
    while app._busy and time.monotonic() < end:
        app._drain_events()
        time.sleep(0.01)
    app._drain_events()
    assert not app._busy, "фоновое задание не завершилось"


def test_text_pdf_defaults_to_local_and_ai_is_an_explicit_choice(gui, tmp_path, monkeypatch):
    from tkinter import filedialog
    import llm_parser
    from llm_parser import ASPENWALL_SAMPLES, ParseOutcome

    sample = ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"]
    text_pdf = tmp_path / "text.pdf"
    scan_pdf = tmp_path / "scan.pdf"
    _write_test_pdf(text_pdf, [sample])
    _write_test_pdf(scan_pdf, [None])
    app = gui.SurveyApp()
    app.select_passes(1)
    assert app.topmost_var.get() is False
    assert app.attributes("-topmost") is False
    monkeypatch.setattr(filedialog, "askopenfilename", lambda **_: str(text_pdf))
    monkeypatch.setattr(llm_parser, "_extract_with_llm", lambda *a, **k: pytest.fail("вызов API не ожидался"))

    app._load_pdf()
    assert app.pdf_path == text_pdf
    assert app.use_llm_var.get() is False
    assert "бесплатно" in app.parse_button.cget("text").lower()
    assert "с ИИ" in app.regex_link_button.cget("text")
    assert app.parse_button.cget("fg_color") == gui.THEME["btn_primary"]
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    assert app.description is not None
    assert app.description.distance_to_corner_ft == pytest.approx(420.0)
    assert "0 (локальный" in app.tokens_var.get()

    seen = []
    desc = app.description
    def fake_ai(**kwargs):
        seen.append(kwargs)
        return ParseOutcome(data=desc, source="llm")
    monkeypatch.setattr(gui, "parse_legal_description_full", fake_ai)
    app.regex_link_button.cget("command")()
    _wait_for_gui_worker(app)
    assert app.use_llm_var.get() is True
    assert len(seen) == 1 and seen[0]["use_llm"] is True
    assert "локальный" in app.regex_link_button.cget("text").lower()

    monkeypatch.setattr(filedialog, "askopenfilename", lambda **_: str(scan_pdf))
    app._load_pdf()
    assert app._pdf_needs_vision is True
    assert app.use_llm_var.get() is True
    assert "Vision" in app.pdf_label.cget("text")
    app._on_close()


def test_neighbor_pdf_batch_attaches_without_api_then_builds_distinct_plans(gui, tmp_path, monkeypatch):
    from tkinter import filedialog
    import llm_parser
    from llm_parser import ASPENWALL_SAMPLES

    sample = ASPENWALL_SAMPLES["Aspenwall St — 365' (Сосед слева)"]
    first, second = tmp_path / "left.pdf", tmp_path / "copy.pdf"
    bad = tmp_path / "broken.pdf"
    _write_test_pdf(first, [sample])
    _write_test_pdf(second, [sample])  # одинаковый POB не должен схлопывать два файла
    bad.write_bytes(b"invalid pdf")
    app = gui.SurveyApp()
    app._on_sample_selected("Aspenwall St — 420' (Основной участок)")
    app.neighbor_lot_radio.select()
    assert app.verify_neighbors_button.master.master is app.neighbors_tab_frame
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **_: (str(first), str(second), str(bad)))
    monkeypatch.setattr(llm_parser, "_extract_with_llm", lambda *a, **k: pytest.fail("API не должен вызываться"))
    monkeypatch.setattr(gui.AcadBuilder, "draw", lambda *a, **k: pytest.fail("AutoCAD не должен вызываться"))

    app.load_pdf_button.cget("command")()
    assert app.neighbor_pdf_paths == [first, second]
    assert app.pdf_path is None
    assert app.use_llm_var.get() is False
    assert "все PDF" in app.regex_link_button.cget("text")
    assert not app._busy and not app._preview_neighbor_plans
    assert "broken.pdf" in app.log_text()
    assert "left.pdf" in app.neighbor_pdf_summary.cget("text")
    app.main_lot_radio.select()  # скрытый переключатель текста не меняет роль PDF
    assert app.is_neighbor_mode()
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    assert len(app._preview_neighbor_plans) == 2
    assert len(app._neighbor_report.pair_reports) == 2
    assert set(app._neighbor_plan_sources) == {first, second}
    assert app._preview_neighbor_plans[0].pob == app._preview_neighbor_plans[1].pob
    assert bad in app._pending_neighbor_pdfs
    app._start(parse_only=False)
    assert "broken.pdf" in app.status_var.get() and "Исправьте выбор" in app.status_var.get()

    # Явный новый выбор исключает недоступный файл; повторный разбор обновляет,
    # а не добавляет второй экземпляр уже присутствующих планов.
    app.neighbor_lot_radio.select()
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **_: (str(first), str(second)))
    app._load_pdf()
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    assert len(app._preview_neighbor_plans) == 2
    app._clear_neighbors_scene()
    assert not app._preview_neighbor_plans
    assert app._last_active_plan is app._preview_main_plan
    app._on_close()


def test_mixed_neighbor_pdf_batch_reports_scan_without_silent_ai(gui, tmp_path, monkeypatch):
    from tkinter import filedialog
    import llm_parser
    from llm_parser import ASPENWALL_SAMPLES, ParseOutcome, parse_with_regex

    sample = ASPENWALL_SAMPLES["Aspenwall St — 365' (Сосед слева)"]
    text_pdf, scan_pdf = tmp_path / "text.pdf", tmp_path / "scan.pdf"
    _write_test_pdf(text_pdf, [sample])
    _write_test_pdf(scan_pdf, [None])
    app = gui.SurveyApp()
    app.select_passes(1)
    app.neighbor_lot_radio.select()
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **_: (str(text_pdf), str(scan_pdf)))
    monkeypatch.setattr(llm_parser, "_extract_with_llm", lambda *a, **k: pytest.fail("платный запрос без выбора ИИ"))
    app._load_pdf()
    assert app.use_llm_var.get() is False
    assert "потребуют ИИ" in app.status_var.get()
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    assert list(app._neighbor_plan_sources) == [text_pdf]
    assert "scan.pdf" in app.log_text() and "Vision" in app.log_text()
    assert "Ошибок: 1" in app.status_var.get()

    # Вторая кнопка — явный платный выбор; каждый PDF обрабатывается отдельно.
    desc, _ = parse_with_regex(sample)
    calls = []
    def fake_ai(**kwargs):
        calls.append(kwargs)
        return ParseOutcome(data=desc, source="llm")
    monkeypatch.setattr(gui, "parse_legal_description_full", fake_ai)
    app.regex_link_button.cget("command")()
    _wait_for_gui_worker(app)
    assert [kw["pdf_path"] for kw in calls] == [text_pdf, scan_pdf]
    assert all(kw["use_llm"] for kw in calls)
    assert len(app._preview_neighbor_plans) == 2
    app._on_close()


def test_neighbor_batch_honors_multipass_and_skips_disagreements(gui, tmp_path, monkeypatch):
    from tkinter import filedialog
    from llm_parser import ASPENWALL_SAMPLES, ParseOutcome, TokenUsage, parse_with_regex

    sample = ASPENWALL_SAMPLES["Aspenwall St — 365' (Сосед слева)"]
    paths = [tmp_path / "agree.pdf", tmp_path / "conflict.pdf"]
    for path in paths:
        _write_test_pdf(path, [sample])
    app = gui.SurveyApp()
    app.neighbor_lot_radio.select()
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **_: tuple(map(str, paths)))
    app._load_pdf()
    app.use_llm_var.set(True)
    app.select_passes(2)
    desc, _ = parse_with_regex(sample)
    usage = TokenUsage(prompt_tokens=5, completion_tokens=2, total_tokens=7, model="fake")
    out = ParseOutcome(data=desc, source="llm", usage=usage)
    seen = []
    def fake_consensus(**kwargs):
        seen.append(kwargs)
        return types.SimpleNamespace(
            agreed=kwargs["pdf_path"] == paths[0], pass_outcomes=[out, out],
            selected_outcome=out, discrepancy_report=lambda: "различие румба",
        )
    monkeypatch.setattr(gui, "run_multipass_consensus", fake_consensus)
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    assert len(seen) == 2 and all(call["passes"] == 2 for call in seen)
    assert list(app._neighbor_plan_sources) == [paths[0]]
    assert "conflict.pdf" in app.log_text() and "различие румба" in app.log_text()
    assert app._session_llm_calls == 4
    assert app._session_prompt_tokens == 20
    assert "Сессия (4 выз.)" in app.tokens_var.get()
    app._on_close()


def test_neighbor_pdf_draw_is_separate_and_uses_all_selected_files(gui, tmp_path, monkeypatch):
    from tkinter import filedialog, messagebox
    from acad_builder import DrawResult
    from llm_parser import ASPENWALL_SAMPLES

    sample = ASPENWALL_SAMPLES["Aspenwall St — 365' (Сосед слева)"]
    paths = [tmp_path / "one.pdf", tmp_path / "two.pdf"]
    for path in paths:
        _write_test_pdf(path, [sample])
    app = gui.SurveyApp()
    app.neighbor_lot_radio.select()
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **_: tuple(map(str, paths)))
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **kw: True)
    monkeypatch.setattr(gui.sys, "platform", "win32")
    draws = []
    def fake_draw(_builder, plan):
        draws.append(plan)
        return DrawResult(plan=plan, text_count=len(plan.items), layers={"neighbor_lot": "NEIGHBOR_LOT"})
    monkeypatch.setattr(gui.AcadBuilder, "draw_plan", fake_draw)
    app._load_pdf()
    app._start(parse_only=False)
    assert draws == [] and "Сначала разберите" in app.status_var.get()
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    plans = list(app._preview_neighbor_plans)
    assert len(plans) == 2 and draws == []
    monkeypatch.setattr(gui, "parse_legal_description_full", lambda *a, **k: pytest.fail("повторный разбор"))
    monkeypatch.setattr(gui, "run_multipass_consensus", lambda *a, **k: pytest.fail("повторный запрос"))
    app._start(parse_only=False)
    _wait_for_gui_worker(app)
    assert len(draws) == 2 and all(drawn is expected for drawn, expected in zip(draws, plans))
    assert "Начерчено участков: 2" in app.status_var.get()
    app._on_close()


def test_pdf_page_selection_applies_to_every_neighbor_without_vision(gui, tmp_path, monkeypatch):
    from tkinter import filedialog
    import llm_parser
    from llm_parser import ASPENWALL_SAMPLES

    left = ASPENWALL_SAMPLES["Aspenwall St — 365' (Сосед слева)"]
    right = ASPENWALL_SAMPLES["Aspenwall St — 475' (Сосед справа)"]
    files = [tmp_path / "a.pdf", tmp_path / "b.pdf"]
    for path in files:
        _write_test_pdf(path, [left, right])
    app = gui.SurveyApp()
    app._on_sample_selected("Aspenwall St — 420' (Основной участок)")
    app.neighbor_lot_radio.select()
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **_: tuple(map(str, files)))
    monkeypatch.setattr(llm_parser, "render_pdf_pages_base64", lambda *a, **k: pytest.fail("Vision не нужен"))
    app._load_pdf()
    app.pdf_pages_var.set("2")
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    assert len(app._preview_neighbor_plans) == 2
    assert all(p.description.distance_to_corner_ft == pytest.approx(475) for p in app._preview_neighbor_plans)

    app.pdf_pages_var.set("1")
    app._on_verify_neighbors_clicked()  # смена диапазона не должна показывать старую сверку
    _wait_for_gui_worker(app)
    assert len(app._preview_neighbor_plans) == 2
    assert all(p.description.distance_to_corner_ft == pytest.approx(365) for p in app._preview_neighbor_plans)
    for path in files:
        _write_test_pdf(path, [right])  # заново прикреплённый путь может содержать новые данные
    app._load_pdf()
    app._on_verify_neighbors_clicked()
    _wait_for_gui_worker(app)
    assert len(app._preview_neighbor_plans) == 2
    assert all(p.description.distance_to_corner_ft == pytest.approx(475) for p in app._preview_neighbor_plans)
    app._on_close()


def test_reset_preserves_settings_and_rejects_all_stale_worker_events(gui, tmp_path, monkeypatch):
    import threading
    import time
    from tkinter import messagebox
    from llm_parser import ASPENWALL_SAMPLES, ParseOutcome, parse_with_regex

    sample = ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"]
    desc, _ = parse_with_regex(sample)
    old_started, new_started = threading.Event(), threading.Event()
    release_old, release_new = threading.Event(), threading.Event()
    old_returned = threading.Event()
    draws = []

    def fake_parse(**kwargs):
        text = kwargs.get("text") or ""
        if "OLD-REQUEST" in text:
            old_started.set()
            assert release_old.wait(5)
            # Консенсус может сообщать прогресс из дочернего потока.
            child = threading.Thread(target=lambda: kwargs["progress"]("УСТАРЕВШИЙ ПРОГРЕСС"))
            child.start()
            child.join()
            old_returned.set()
        else:
            new_started.set()
            assert release_new.wait(5)
        return ParseOutcome(data=desc, source="regex")

    monkeypatch.setattr(gui, "parse_legal_description_full", fake_parse)
    monkeypatch.setattr(gui.sys, "platform", "win32")
    monkeypatch.setattr(gui.AcadBuilder, "draw_plan", lambda *a, **k: draws.append(True))
    app = gui.SurveyApp()
    app.provider_key_vars["hydra"].set("key-stays-in-memory")
    app.key_secret_var.set("pin-stays")
    app._last_valid_secret = "old-valid-pin"
    app.select_provider("openrouter")
    app.select_passes(1)
    app.scale_var.set('1"=20\'')
    app.use_llm_var.set(False)
    app._on_sample_selected("Aspenwall St — 420' (Основной участок)")
    app.text_box.delete("1.0", "end")
    app.text_box.insert("1.0", sample + "\nOLD-REQUEST")
    app._start(parse_only=True)
    assert old_started.wait(3)
    original_generation = app._generation
    assert app._busy

    # Отказ не должен ничего очищать, даже во время задания.
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: False)
    app._reset_session_clicked()
    assert app._generation == original_generation and app._busy
    assert "OLD-REQUEST" in app.text_box.get("1.0", "end")

    file_on_disk = tmp_path / "untouched.pdf"
    file_on_disk.write_bytes(b"not modified")
    app.neighbor_pdf_paths = [file_on_disk]
    app._session_llm_calls = 7
    app._session_prompt_tokens = 800
    app._last_consensus_label = "2/2"
    app._log_line("старый журнал")
    prompts = []
    monkeypatch.setattr(messagebox, "askyesno", lambda title, text, **kw: prompts.append(text) or True)
    app.reset_session_button.cget("command")()
    assert prompts and "AutoCAD" in prompts[0] and "нельзя прервать" in prompts[0]
    assert app._generation == original_generation + 1 and not app._busy
    assert app.description is None and app.draw_result is None and app.last_usage is None
    assert app.pdf_path is None and app.neighbor_pdf_paths == [] and app.image_paths == []
    assert app._preview_main_plan is None and app._preview_neighbor_plans == []
    assert app._neighbor_report is None and app.pending_conflict is None and app.pending_doubt is None
    assert app._session_llm_calls == 0 and app._session_prompt_tokens == 0
    assert app._last_consensus_label is None and app.log_text() == ""
    assert app.text_box.get("1.0", "end") == ""
    assert file_on_disk.read_bytes() == b"not modified"
    assert app.provider_key_vars["hydra"].get() == "key-stays-in-memory"
    assert app.key_secret_var.get() == "pin-stays" and app._last_valid_secret == "old-valid-pin"
    assert app.current_provider_id() == "openrouter" and app.scale_var.get() == '1"=20\''

    app.text_box.insert("1.0", sample + "\nNEW-REQUEST")
    app._start(parse_only=True)
    assert new_started.wait(3) and app._busy
    # Сообщения, уже оказавшиеся в очереди до сброса/между проверкой и put.
    for kind, payload in (("progress", "УСТАРЕВШИЙ СТАТУС"), ("error", "старая ошибка"),
                          ("done", None), ("conflict", {}), ("parsed", ())):
        app._events.put((original_generation, kind, payload))
    release_old.set()
    assert old_returned.wait(3)
    time.sleep(0.05)
    app._drain_events()
    assert app._busy, "старый done не должен завершать новое задание"
    assert "УСТАРЕВШ" not in app.status_var.get() and "УСТАРЕВШ" not in app.log_text()
    assert app.description is None and draws == []
    release_new.set()
    _wait_for_gui_worker(app)
    assert app.description is not None and app._preview_main_plan is not None
    assert draws == []  # сброшенное построение так и не запустило AutoCAD
    app._on_close()


def test_single_text_pdf_respects_selected_page_in_gui(gui, tmp_path, monkeypatch):
    from tkinter import filedialog
    import llm_parser
    from llm_parser import ASPENWALL_SAMPLES

    pdf = tmp_path / "pages.pdf"
    _write_test_pdf(pdf, [
        ASPENWALL_SAMPLES["Aspenwall St — 365' (Сосед слева)"],
        ASPENWALL_SAMPLES["Aspenwall St — 475' (Сосед справа)"],
    ])
    app = gui.SurveyApp()
    monkeypatch.setattr(filedialog, "askopenfilename", lambda **_: str(pdf))
    monkeypatch.setattr(llm_parser, "render_pdf_pages_base64", lambda *a, **k: pytest.fail("Vision не нужен"))
    app._load_pdf()
    assert app.use_llm_var.get() is False
    app.pdf_pages_var.set("2")
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    assert app.description.distance_to_corner_ft == pytest.approx(475.0)
    assert app._preview_main_plan.description.distance_to_corner_ft == pytest.approx(475.0)
    app._on_close()


def test_reset_during_com_draw_serializes_new_draw_and_ignores_old_result(gui, monkeypatch):
    import threading
    import time
    from acad_builder import DrawResult
    from llm_parser import ASPENWALL_SAMPLES

    first_started, second_started = threading.Event(), threading.Event()
    release_first = threading.Event()
    order = []
    def fake_draw(_builder, plan):
        distance = plan.description.distance_to_corner_ft
        order.append(("start", distance))
        if distance == pytest.approx(420.0):
            first_started.set()
            assert release_first.wait(5)
        else:
            second_started.set()
        order.append(("end", distance))
        return DrawResult(plan=plan, text_count=len(plan.items), layers={"property_line": "PROPERTY_LINE"})

    monkeypatch.setattr(gui.sys, "platform", "win32")
    monkeypatch.setattr(gui.AcadBuilder, "draw_plan", fake_draw)
    app = gui.SurveyApp()
    app.use_llm_var.set(False)
    app._on_sample_selected("Aspenwall St — 420' (Основной участок)")
    app._start(parse_only=False)
    assert first_started.wait(3)
    app._reset_session()
    app.text_box.insert("1.0", ASPENWALL_SAMPLES["Aspenwall St — 365' (Сосед слева)"])
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    app._start(parse_only=False)
    time.sleep(0.1)
    assert not second_started.is_set(), "два COM-вызова не должны выполняться одновременно"
    assert app._busy
    release_first.set()
    assert second_started.wait(3)
    _wait_for_gui_worker(app)
    assert order == [("start", 420.0), ("end", 420.0), ("start", 365.0), ("end", 365.0)]
    assert app.description.distance_to_corner_ft == pytest.approx(365.0)
    assert app.draw_result.plan.description.distance_to_corner_ft == pytest.approx(365.0)
    app._on_close()


def test_reset_cancels_remaining_neighbor_batch_files(gui, tmp_path, monkeypatch):
    import threading
    import time
    from tkinter import filedialog
    from llm_parser import ASPENWALL_SAMPLES, ParseOutcome, parse_with_regex

    sample = ASPENWALL_SAMPLES["Aspenwall St — 365' (Сосед слева)"]
    files = [tmp_path / "first.pdf", tmp_path / "second.pdf"]
    for path in files:
        _write_test_pdf(path, [sample])
    desc, _ = parse_with_regex(sample)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    seen = []
    def fake_parse(**kwargs):
        seen.append(kwargs["pdf_path"])
        started.set()
        assert release.wait(5)
        finished.set()
        return ParseOutcome(data=desc, source="regex")
    monkeypatch.setattr(gui, "parse_legal_description_full", fake_parse)
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **_: tuple(map(str, files)))
    app = gui.SurveyApp()
    app.neighbor_lot_radio.select()
    app._load_pdf()
    app._start(parse_only=True)
    assert started.wait(3)
    app._reset_session()
    release.set()
    assert finished.wait(3)
    time.sleep(0.05)
    app._drain_events()
    assert seen == [files[0]]
    assert not app._busy and not app._preview_neighbor_plans
    assert app.description is None and app.log_text() == ""
    app._on_close()


def test_suspicious_neighbor_is_not_silently_drawn_in_batch(gui, tmp_path, monkeypatch):
    from tkinter import filedialog, messagebox
    from acad_builder import DrawResult
    from llm_parser import ASPENWALL_SAMPLES, ParseOutcome, TypoSuspicion, parse_with_regex

    sample = ASPENWALL_SAMPLES["Aspenwall St — 365' (Сосед слева)"]
    path = tmp_path / "needs-review.pdf"
    second = tmp_path / "needs-review-too.pdf"
    _write_test_pdf(path, [sample])
    _write_test_pdf(second, [sample])
    desc, _ = parse_with_regex(sample)
    typo = TypoSuspicion(
        segment_index=2, kind="length_digit", original_value="54.00", suggested_value="56.00",
        digit_pair="4→6", is_lookalike=False, closure_before_ft=2.0, closure_after_ft=0.0,
        reason_ru="Проверить длину по исходнику",
    )
    outcome = ParseOutcome(data=desc, source="regex", typo_suspicions=[typo])
    calls, draws, prompts = [], [], []
    monkeypatch.setattr(gui, "parse_legal_description_full", lambda **kw: calls.append(kw) or outcome)
    def fake_draw(_builder, plan):
        draws.append(plan)
        return DrawResult(plan=plan, text_count=0, layers={"neighbor_lot": "NEIGHBOR_LOT"})
    monkeypatch.setattr(gui.AcadBuilder, "draw_plan", fake_draw)
    monkeypatch.setattr(gui.sys, "platform", "win32")
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **kw: (str(path), str(second)))
    monkeypatch.setattr(messagebox, "askyesno", lambda title, text, **kw: prompts.append(text) or False)
    app = gui.SurveyApp()
    app.ask_on_doubt_var.set(True)
    app.neighbor_lot_radio.select()
    app._load_pdf()
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    assert len(calls) == 2 and len(app._preview_neighbor_plans) == 2
    assert app.result_tab_buttons["🛡 Проверки"].cget("fg_color") == "#92400e"
    assert "замечаний: 2" in app.suspicion_badge_button.cget("text")
    assert all(any("Возможная опечатка" in item.title for item in app._plan_diagnostic_items(plan))
               for plan in app._preview_neighbor_plans)
    app._start(parse_only=False)
    assert draws == [] and prompts and "опечатка" in prompts[0]
    assert "Отрисовка отменена" in app.status_var.get()
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **kw: True)
    app._start(parse_only=False)
    _wait_for_gui_worker(app)
    assert draws == app._preview_neighbor_plans and len(calls) == 2
    app._on_close()


def test_direct_neighbor_verification_offers_free_pdf_before_any_api(gui, tmp_path, monkeypatch):
    from tkinter import filedialog, messagebox
    import llm_parser
    from llm_parser import ASPENWALL_SAMPLES

    pdf, scan = tmp_path / "text.pdf", tmp_path / "scan.pdf"
    _write_test_pdf(pdf, [ASPENWALL_SAMPLES["Aspenwall St — 365' (Сосед слева)"]])
    _write_test_pdf(scan, [None])
    app = gui.SurveyApp()
    app._on_sample_selected("Aspenwall St — 420' (Основной участок)")
    assert app.use_llm_var.get() is True  # исходный режим ИИ не должен приводить к скрытому запросу
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **kw: (str(pdf), str(scan)))
    monkeypatch.setattr(llm_parser, "_extract_with_llm", lambda *a, **kw: pytest.fail("API без согласия"))
    prompts = []
    monkeypatch.setattr(messagebox, "askyesnocancel", lambda *a, **kw: prompts.append((a, kw)) or True)
    app._on_verify_neighbors_clicked()
    _wait_for_gui_worker(app)
    assert prompts and prompts[0][1]["default"] == "yes"
    assert "сканы/фото" in prompts[0][0][1]
    assert not app.use_llm_var.get()
    assert list(app._neighbor_plan_sources) == [pdf]
    assert "scan.pdf" in app.log_text() and "Vision" in app.log_text()
    app._on_close()


def test_direct_neighbor_visual_batch_requires_confirmation(gui, tmp_path, monkeypatch):
    from tkinter import filedialog, messagebox

    scan = tmp_path / "scan.pdf"
    _write_test_pdf(scan, [None])
    app = gui.SurveyApp()
    app._on_sample_selected("Aspenwall St — 420' (Основной участок)")
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **kw: (str(scan),))
    prompts = []
    monkeypatch.setattr(messagebox, "askokcancel", lambda *a, **kw: prompts.append((a, kw)) or False)
    app._on_verify_neighbors_clicked()
    assert prompts and prompts[0][1]["default"] == "cancel"
    assert not app._busy and not app._preview_neighbor_plans
    app._on_close()


def test_topmost_starts_off_and_auxiliary_windows_follow_toggle(gui):
    app = gui.SurveyApp()
    assert app.topmost_var.get() is False and app.attributes("-topmost") is False
    app._show_log()
    assert app._session_windows[-1].attributes("-topmost") is False
    app.topmost_var.set(True)
    app._apply_topmost()
    app._show_log()
    assert app.attributes("-topmost") is True
    assert app._session_windows[-1].attributes("-topmost") is True
    app.topmost_var.set(False)
    app._apply_topmost()
    assert app.attributes("-topmost") is False
    app._on_close()


def test_switching_to_text_does_not_reparse_attached_neighbor_pdfs(gui, tmp_path, monkeypatch):
    from tkinter import filedialog
    from llm_parser import ASPENWALL_SAMPLES

    paths = [tmp_path / "first.pdf", tmp_path / "second.pdf"]
    for path in paths:
        _write_test_pdf(path, [ASPENWALL_SAMPLES["Aspenwall St — 365' (Сосед слева)"]])
    app = gui.SurveyApp()
    app.neighbor_lot_radio.select()
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **kw: tuple(map(str, paths)))
    app._load_pdf()
    assert "все PDF" in app.regex_link_button.cget("text")
    app.source_mode_var.set("📝 Текст")
    app._on_source_mode_changed()
    assert "все PDF" not in app.regex_link_button.cget("text")
    app.text_box.insert("1.0", ASPENWALL_SAMPLES["Aspenwall St — 475' (Сосед справа)"])
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    assert app.description.distance_to_corner_ft == pytest.approx(475.0)
    assert not app._neighbor_plan_sources
    assert app.neighbor_pdf_paths == paths
    app._on_close()


def test_scene_checks_aggregate_replacements_and_both_tab_severities(gui, monkeypatch, tmp_path):
    from acad_builder import AcadBuilder, LotSuspicionReport, SuspicionItem, NeighborVerificationReport, NeighborPairReport
    from llm_parser import ASPENWALL_SAMPLES, parse_with_regex

    app = gui.SurveyApp()
    builder = AcadBuilder(app.config_data)
    desc, _ = parse_with_regex(ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"])
    def plan(neighbor, status, title):
        result = builder.plan(desc, is_neighbor=neighbor)
        items = [] if status == "ok" else [SuspicionItem(title, status, title, f"Детали: {title}")]
        result.suspicion = LotSuspicionReport(shape_kind="участок", status=status, items=items)
        return result

    pair_status = ["error"]
    def verify(_main, neighbors):
        return NeighborVerificationReport(
            overall_status=pair_status[0],
            pair_reports=[NeighborPairReport(i, f"Сосед {i}", status=pair_status[0])
                          for i in range(1, len(neighbors) + 1)],
            error_count=int(pair_status[0] == "error"), warning_count=int(pair_status[0] == "warn"),
        )
    monkeypatch.setattr(gui, "verify_with_neighbors", verify)
    main = plan(False, "error", "ОШИБКА ОСНОВНОГО")
    warn_neighbor = plan(True, "warn", "ЗАМЕЧАНИЕ СОСЕДА")
    clean_neighbor = plan(True, "ok", "")
    first, second = tmp_path / "north.pdf", tmp_path / "south.pdf"
    app._record_plan_for_preview(main)
    app._record_plan_for_preview(warn_neighbor, source=first)
    app._record_plan_for_preview(clean_neighbor, source=second)

    assert [name for name, _ in app._scene_suspicion_reports()] == [
        "Основной участок: текст", "Сосед #1: north.pdf", "Сосед #2: south.pdf"
    ]
    assert "Фигур: 3 · замечаний: 2" in app.suspicion_badge_button.cget("text")
    checks = app.result_tab_buttons["🛡 Проверки"]
    neighbors = app.result_tab_buttons["🏘 Соседи"]
    assert checks.cget("fg_color") == "#991b1b" and neighbors.cget("fg_color") == "#991b1b"
    app._select_result_tab("🛡 Проверки")
    assert checks.cget("fg_color") == "#dc2626" and neighbors.cget("fg_color") == "#991b1b"
    app._select_result_tab("🏘 Соседи")
    assert checks.cget("fg_color") == "#991b1b" and neighbors.cget("fg_color") == "#dc2626"

    def texts(widget):
        return [str(widget.cget("text") or "")] + [text for child in widget.winfo_children() for text in texts(child)]
    visible = "\n".join(texts(app.checks_scroll_frame))
    assert "ОШИБКА ОСНОВНОГО" in visible and "ЗАМЕЧАНИЕ СОСЕДА" in visible
    assert "north.pdf" in visible and "south.pdf" in visible and "Замечаний к форме не найдено" in visible

    # Замена одного файла не удаляет замечания основной фигуры и соседнего файла.
    pair_status[0] = "warn"
    replacement = plan(True, "warn", "НОВОЕ ЗАМЕЧАНИЕ")
    app._record_plan_for_preview(replacement, source=second)
    visible = "\n".join(texts(app.checks_scroll_frame))
    assert all(text in visible for text in ("ОШИБКА ОСНОВНОГО", "ЗАМЕЧАНИЕ СОСЕДА", "НОВОЕ ЗАМЕЧАНИЕ"))
    assert neighbors.cget("fg_color") == "#d97706"  # вкладка выбрана
    app._record_plan_for_preview(plan(False, "ok", ""))
    assert checks.cget("fg_color") == "#92400e"  # остались предупреждения соседей
    assert "ОШИБКА ОСНОВНОГО" not in "\n".join(texts(app.checks_scroll_frame))
    app._clear_neighbors_scene()
    assert len(app._scene_suspicion_reports()) == 1
    assert checks.cget("fg_color") == "#065f46" and checks.cget("text") == "✓ Проверки"
    assert neighbors.cget("fg_color") == gui.THEME["seg_sel"]
    assert "ЗАМЕЧАНИЕ СОСЕДА" not in "\n".join(texts(app.checks_scroll_frame))
    pair_status[0] = "ok"
    app._record_plan_for_preview(plan(True, "ok", ""), source=second)
    assert checks.cget("fg_color") == "#065f46"
    assert neighbors.cget("fg_color") == "#059669" and neighbors.cget("text") == "✓ Соседи"
    pair_status[0] = "warn"
    app._record_plan_for_preview(plan(False, "error", "СБРОС ОСНОВНОГО"))
    app._record_plan_for_preview(plan(True, "warn", "СБРОС СОСЕДА"), source=first)
    assert checks.cget("fg_color") == "#991b1b" and neighbors.cget("fg_color") == "#d97706"
    app._reset_session()
    assert app._scene_suspicion_reports() == []
    assert checks.cget("fg_color") == gui.THEME["bg_field"]
    assert neighbors.cget("fg_color") == gui.THEME["bg_field"]
    app._on_close()


def test_file_role_is_chosen_before_dialog_independent_of_text_switch(gui, tmp_path, monkeypatch):
    from tkinter import filedialog
    from PIL import Image
    from llm_parser import ASPENWALL_SAMPLES, ParseOutcome, parse_with_regex

    app = gui.SurveyApp()
    assert app.source_mode_switch.cget("values") == ["📄 PDF", "📷 Фото", "📝 Текст"]
    assert app.role_bar._gridded
    events = []
    monkeypatch.setattr(app, "_ask_source_role", lambda kind: events.append(("role", kind)) or None)
    monkeypatch.setattr(filedialog, "askopenfilename", lambda **kw: events.append(("single",)) or "")
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **kw: events.append(("multi",)) or ())
    app._load_pdf()
    app._load_images()
    assert events == [("role", "PDF"), ("role", "изображениях")]
    assert app.pdf_path is None and not app.image_paths

    sample = ASPENWALL_SAMPLES["Aspenwall St — 365' (Сосед слева)"]
    pdf = tmp_path / "neighbor.pdf"
    _write_test_pdf(pdf, [sample])
    events.clear()
    monkeypatch.setattr(app, "_ask_source_role", lambda kind: events.append(("role", kind)) or "neighbor")
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **kw: events.append(("multi",)) or (str(pdf),))
    app.main_lot_radio.select()  # текстовый переключатель остаётся на «основной»
    app._load_pdf()
    assert events == [("role", "PDF"), ("multi",)]
    assert app.neighbor_pdf_paths == [pdf] and app.is_neighbor_mode() and not app.role_bar._gridded
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    assert app._preview_neighbor_plans[0].is_neighbor

    image = tmp_path / "neighbor.jpg"
    Image.new("RGB", (20, 20), "white").save(image)
    events.clear()
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **kw: events.append(("multi",)) or (str(image),))
    app._load_images()
    assert events == [("role", "изображениях"), ("multi",)]
    assert app.image_paths == [image] and app.is_neighbor_mode() and not app.role_bar._gridded
    desc, _ = parse_with_regex(sample)
    monkeypatch.setattr(gui, "parse_legal_description_full", lambda **kw: ParseOutcome(data=desc, source="llm-vision"))
    app.select_passes(1)
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    assert app._last_active_plan.is_neighbor and app._neighbor_plan_sources[image] is app._last_active_plan

    # Отмена выбора файла не меняет ни роль, ни уже прикреплённые файлы.
    monkeypatch.setattr(app, "_ask_source_role", lambda _kind: "main")
    monkeypatch.setattr(filedialog, "askopenfilename", lambda **kw: "")
    app._load_pdf()
    assert app.image_paths == [image] and app.is_neighbor_mode()
    app.source_mode_var.set("📝 Текст")
    app._on_source_mode_changed()
    assert app.role_bar._gridded and not app.is_neighbor_mode()
    app.source_mode_var.set("📄 PDF")
    app._on_source_mode_changed()
    assert not app.role_bar._gridded
    app._on_close()


def test_drawing_uses_scene_objects_and_refuses_changed_or_missing_source(gui, monkeypatch):
    from acad_builder import DrawResult
    from tkinter import messagebox

    app = gui.SurveyApp()
    monkeypatch.setattr(gui.sys, "platform", "win32")
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **kw: True)
    monkeypatch.setattr(gui, "parse_legal_description_full", lambda *a, **kw: pytest.fail("повторный разбор"))
    monkeypatch.setattr(gui, "run_multipass_consensus", lambda *a, **kw: pytest.fail("повторный запрос"))
    draws = []
    def fake_draw(_builder, plan):
        draws.append(plan)
        return DrawResult(plan=plan, text_count=len(plan.items), layers={"property_line": "PROPERTY_LINE"})
    monkeypatch.setattr(gui.AcadBuilder, "draw_plan", fake_draw)
    app._start(parse_only=False)
    assert not draws and "Нет рассчитанной схемы" in app.status_var.get()

    app._on_sample_selected("Aspenwall St — 420' (Основной участок)")
    first = app._preview_main_plan
    app._on_sample_selected("Aspenwall St — 365' (Сосед слева)")
    second = app._preview_neighbor_plans[0]
    # Новое значение масштаба не пересчитывает уже показанные планы.
    app.scale_var.set('1"=20\'')
    monkeypatch.setattr(gui.AcadBuilder, "plan", lambda *a, **kw: pytest.fail("повторный расчёт"))
    app._start(parse_only=False)
    _wait_for_gui_worker(app)
    assert len(draws) == 2 and draws[0] is first and draws[1] is second
    assert "без запроса к ИИ" in app.status_var.get()

    app.text_box.insert("end", " изменённый исходник")
    app._start(parse_only=False)
    assert len(draws) == 2 and "разберите их заново" in app.status_var.get()
    app._reset_session()
    app._start(parse_only=False)
    assert len(draws) == 2 and "Нет рассчитанной схемы" in app.status_var.get()
    app._on_close()


def test_draw_rejects_changed_pdf_pages_and_partial_batch(gui, tmp_path, monkeypatch):
    from tkinter import filedialog
    from llm_parser import ASPENWALL_SAMPLES

    sample = ASPENWALL_SAMPLES["Aspenwall St — 365' (Сосед слева)"]
    good, scan = tmp_path / "good.pdf", tmp_path / "scan.pdf"
    _write_test_pdf(good, [sample, sample])
    _write_test_pdf(scan, [None])
    app = gui.SurveyApp()
    app.neighbor_lot_radio.select()
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **kw: (str(good), str(scan)))
    app._load_pdf()
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    assert good in app._neighbor_plan_sources and scan in app._pending_neighbor_pdfs
    monkeypatch.setattr(gui.AcadBuilder, "draw_plan", lambda *a, **kw: pytest.fail("неполный пакет"))
    app._start(parse_only=False)
    assert "разберите" in app.status_var.get().lower()

    # После загрузки только исправного PDF смена страниц делает готовый план устаревшим.
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **kw: (str(good),))
    app._load_pdf()
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    app.pdf_pages_var.set("2")
    app._start(parse_only=False)
    assert "страницы изменились" in app.status_var.get()
    app._on_close()


def test_single_neighbor_pdf_pages_and_manual_conflict_keep_neighbor_role(gui, tmp_path, monkeypatch):
    from tkinter import filedialog, messagebox
    from acad_builder import DrawResult
    from llm_parser import ASPENWALL_SAMPLES, TokenUsage

    left = ASPENWALL_SAMPLES["Aspenwall St — 365' (Сосед слева)"]
    right = ASPENWALL_SAMPLES["Aspenwall St — 475' (Сосед справа)"]
    path = tmp_path / "single-neighbor.pdf"
    _write_test_pdf(path, [left, right])
    app = gui.SurveyApp()
    app._on_sample_selected("Aspenwall St — 420' (Основной участок)")
    main_plan = app._preview_main_plan
    app.neighbor_lot_radio.select()
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **_: (str(path),))
    app._load_pdf()
    app.pdf_pages_var.set("2")
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    assert app._preview_main_plan is main_plan
    assert len(app._preview_neighbor_plans) == 1
    assert app._preview_neighbor_plans[0].description.distance_to_corner_ft == pytest.approx(475.0)
    assert list(app._neighbor_plan_sources) == [path]
    assert app._neighbor_parsed_pages[path] == "2" and path not in app._pending_neighbor_pdfs

    desc = app._preview_neighbor_plans[0].description
    draws = []
    monkeypatch.setattr(gui.AcadBuilder, "draw_plan", lambda _builder, plan: (
        draws.append(plan) or DrawResult(plan=plan, text_count=0, layers={})
    ))
    monkeypatch.setattr(gui.sys, "platform", "win32")
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **kw: True)
    app._start(parse_only=False)
    _wait_for_gui_worker(app)
    assert draws == [main_plan, app._preview_neighbor_plans[0]]

    # Конфликт единственного файла показывается обычным диалогом, а выбор
    # прохода заменяет именно соседа и завершает обработку страницы PDF.
    calls = []
    def fake_extract(text, cfg, api_key=None, progress=None, *, provider=None, model=None,
                     key_secret="", image_data_urls=None, focus_instruction=None):
        calls.append(provider)
        choice = desc if provider == "hydra" else desc.model_copy(update={"distance_to_corner_ft": 480.0})
        return choice, "structured-outputs", TokenUsage(
            prompt_tokens=1, completion_tokens=1, total_tokens=2,
            provider=str(provider), provider_label=str(provider), model=str(model),
        )
    monkeypatch.setattr(gui, "run_multipass_consensus", __import__("llm_parser").run_multipass_consensus)
    monkeypatch.setattr(__import__("llm_parser"), "_extract_with_llm", fake_extract)
    app.provider_key_vars["hydra"].set("sk-test")
    app.provider_key_vars["openrouter"].set("sk-test")
    app.use_llm_var.set(True)
    app.select_passes(2)
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    assert app.pending_conflict is not None
    assert len(calls) == 2 and app._preview_main_plan is main_plan
    app.resolve_conflict_with_pass(1)
    _wait_for_gui_worker(app)
    assert app.pending_conflict is None and app._preview_main_plan is main_plan
    assert len(app._preview_neighbor_plans) == 1 and app._preview_neighbor_plans[0].is_neighbor
    assert app._neighbor_parsed_pages[path] == "2" and path not in app._pending_neighbor_pdfs
    draws.clear()
    app._start(parse_only=False)
    _wait_for_gui_worker(app)
    assert draws == [main_plan, app._preview_neighbor_plans[0]] and len(calls) == 2
    app._on_close()


def test_all_neighbor_file_dialogs_ask_role_and_handle_cancel(gui, monkeypatch):
    from tkinter import filedialog

    app = gui.SurveyApp()
    events = []
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **_: events.append("files") or ())
    for action in (app._pick_and_verify_neighbor_files, app._load_neighbor_files):
        events.clear()
        monkeypatch.setattr(app, "_ask_source_role", lambda label: events.append(("role", label)) or None)
        action()
        assert len(events) == 1 and events[0][0] == "role"
        monkeypatch.setattr(app, "_ask_source_role", lambda label: events.append(("main", label)) or "main")
        action()
        assert events[-2][0] == "main" and events[-1] == "files"
        monkeypatch.setattr(app, "_ask_source_role", lambda label: events.append(("neighbor", label)) or "neighbor")
        action()
        assert events[-2][0] == "neighbor" and events[-1] == "files"
    app._on_close()


def test_photo_dialog_rejects_mixed_files_and_multiple_main_pdfs(gui, tmp_path, monkeypatch):
    from tkinter import filedialog
    from PIL import Image
    from llm_parser import ASPENWALL_SAMPLES

    app = gui.SurveyApp()
    sample = ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"]
    pdf, pdf2 = tmp_path / "main.pdf", tmp_path / "second.pdf"
    image = tmp_path / "image.png"
    _write_test_pdf(pdf, [sample])
    _write_test_pdf(pdf2, [sample])
    Image.new("RGB", (20, 20)).save(image)
    monkeypatch.setattr(app, "_ask_source_role", lambda _label: "main")
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **_: (str(image),))
    app._load_images()
    assert app.image_paths == [image]
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **_: (str(image), str(pdf)))
    app._load_images()
    assert app.image_paths == [image] and "без смешивания" in app.status_var.get()
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **_: (str(pdf), str(pdf2)))
    app._load_images()
    assert app.image_paths == [image] and "один PDF" in app.status_var.get()
    app._on_close()


def test_pdf_edited_text_and_selected_pages_cannot_be_drawn_stale(gui, tmp_path, monkeypatch):
    from tkinter import filedialog, messagebox
    from acad_builder import DrawResult
    from llm_parser import ASPENWALL_SAMPLES

    path = tmp_path / "main.pdf"
    _write_test_pdf(path, [ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"]])
    app = gui.SurveyApp()
    monkeypatch.setattr(app, "_ask_source_role", lambda _label: "main")
    monkeypatch.setattr(filedialog, "askopenfilename", lambda **_: str(path))
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **kw: True)
    monkeypatch.setattr(gui.sys, "platform", "win32")
    draws = []
    monkeypatch.setattr(gui.AcadBuilder, "draw_plan", lambda _builder, plan: (
        draws.append(plan) or DrawResult(plan=plan, text_count=0, layers={})
    ))
    app._load_pdf()
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    assert app._preview_main_plan is not None
    app.text_box.insert("end", "\nИзменение пользователя")
    app._start(parse_only=False)
    assert draws == [] and "изменились" in app.status_var.get()
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    assert app._preview_main_plan is not None and app._ready_source_signature == app._source_signature()
    assert "main.pdf (текст изменён)" in app._scene_plans()[0][0]
    app._start(parse_only=False)
    _wait_for_gui_worker(app)
    assert draws == [app._preview_main_plan]
    app.pdf_pages_var.set("1")
    app._start(parse_only=False)
    assert len(draws) == 1 and "изменились" in app.status_var.get()
    app._start(parse_only=True)
    assert "Текст PDF изменён вручную" in app.status_var.get() and not app._busy
    app._on_close()


def test_main_role_from_neighbor_file_picker_loads_pdf_photos_and_text(gui, tmp_path, monkeypatch):
    from tkinter import filedialog
    from PIL import Image
    from llm_parser import ASPENWALL_SAMPLES

    sample = ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"]
    pdf, text_file = tmp_path / "main.pdf", tmp_path / "description.txt"
    first, second = tmp_path / "overview.jpg", tmp_path / "detail.png"
    _write_test_pdf(pdf, [sample])
    text_file.write_text(sample, encoding="utf-8-sig")
    Image.new("RGB", (20, 20), "white").save(first)
    Image.new("RGB", (20, 20), "white").save(second)
    app = gui.SurveyApp()
    app._on_sample_selected("Aspenwall St — 420' (Основной участок)")
    prior = app._preview_main_plan
    assert "пример: Aspenwall" in app._scene_plans()[0][0]
    monkeypatch.setattr(app, "_ask_source_role", lambda _label: "main")
    original_parser = gui.parse_legal_description_full
    monkeypatch.setattr(gui, "parse_legal_description_full", lambda **kw: pytest.fail("скрытый разбор"))

    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **kw: (str(pdf),))
    app._pick_and_verify_neighbor_files()
    assert app.pdf_path == pdf and not app.is_neighbor_mode()
    assert app._preview_main_plan is prior and not app._busy
    assert app.use_llm_var.get() is False

    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **kw: (str(first), str(second)))
    app._load_neighbor_files()
    assert app.image_paths == [first, second] and not app.is_neighbor_mode()
    assert app._preview_main_plan is prior and not app._busy

    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **kw: (str(first), str(pdf)))
    app._load_neighbor_files()
    assert app.image_paths == [first, second] and "без смешивания" in app.status_var.get()

    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **kw: (str(text_file),))
    app._pick_and_verify_neighbor_files()
    assert app._current_source_mode() == "📝 Текст" and not app.is_neighbor_mode()
    assert "description.txt" in app._text_source_label and not app.use_llm_var.get()
    assert app._preview_main_plan is prior and not app._busy
    # Пока новый исходник не разобран, старый план не отправляется в AutoCAD.
    app._start(parse_only=False)
    assert "изменились" in app.status_var.get()
    monkeypatch.setattr(gui, "parse_legal_description_full", original_parser)
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    assert "description.txt" in app._scene_plans()[0][0]
    app.text_box.insert("end", " дополнение")
    assert "изменённый текст" in app._current_text_source_label(app.text_box.get("1.0", "end").strip())
    app._on_close()


def test_scene_draw_reports_com_warnings_and_partial_failure(gui, monkeypatch):
    from acad_builder import DrawResult
    from tkinter import messagebox

    app = gui.SurveyApp()
    app._on_sample_selected("Aspenwall St — 420' (Основной участок)")
    app._on_sample_selected("Aspenwall St — 365' (Сосед слева)")
    monkeypatch.setattr(gui.sys, "platform", "win32")
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **kw: True)
    calls = []
    def warning_draw(_builder, plan):
        calls.append(plan)
        return DrawResult(
            plan=plan, text_count=0, layers={},
            warnings=["не удалось создать текст"] if not plan.is_neighbor else [],
        )
    monkeypatch.setattr(gui.AcadBuilder, "draw_plan", warning_draw)
    app._start(parse_only=False)
    _wait_for_gui_worker(app)
    assert len(calls) == 2
    assert "предупреждений AutoCAD: 1" in app.status_var.get()
    assert "не удалось создать текст" in app.status_var.get()
    assert "не удалось создать текст" in app.log_text()

    def failing_draw(_builder, plan):
        if plan.is_neighbor:
            raise RuntimeError("COM оборвался")
        return DrawResult(plan=plan, text_count=0, layers={})
    monkeypatch.setattr(gui.AcadBuilder, "draw_plan", failing_draw)
    app._start(parse_only=False)
    _wait_for_gui_worker(app)
    assert "COM оборвался" in app.status_var.get()
    assert "Уже начерчено участков: 1" in app.status_var.get()
    assert "могли остаться отдельные объекты" in app.status_var.get()
    assert "дубликаты" in app.status_var.get()
    app._on_close()


@pytest.mark.parametrize("count", [1, 2])
def test_failed_neighbor_pdf_reparse_keeps_old_plan_blocked_until_retry(gui, tmp_path, monkeypatch, count):
    from tkinter import filedialog
    from llm_parser import ASPENWALL_SAMPLES

    sample = ASPENWALL_SAMPLES["Aspenwall St — 365' (Сосед слева)"]
    paths = [tmp_path / f"neighbor-{i}.pdf" for i in range(count)]
    for path in paths:
        _write_test_pdf(path, [sample])
    app = gui.SurveyApp()
    app._on_sample_selected("Aspenwall St — 420' (Основной участок)")
    app.neighbor_lot_radio.select()
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **kw: tuple(map(str, paths)))
    app._load_pdf()
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    assert not app._pending_neighbor_pdfs
    old_plan = app._neighbor_plan_sources[paths[-1]]

    original_parser = gui.parse_legal_description_full
    def fail_one(**kwargs):
        if kwargs.get("pdf_path") == paths[-1]:
            raise RuntimeError("ошибка повторного разбора")
        return original_parser(**kwargs)
    monkeypatch.setattr(gui, "parse_legal_description_full", fail_one)
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    assert app._neighbor_plan_sources[paths[-1]] is old_plan
    assert app._pending_neighbor_pdfs == {paths[-1]}
    monkeypatch.setattr(gui.AcadBuilder, "draw_plan", lambda *a, **kw: pytest.fail("устаревший план"))
    app._start(parse_only=False)
    assert "разберите" in app.status_var.get().lower()

    monkeypatch.setattr(gui, "parse_legal_description_full", original_parser)
    app._on_verify_neighbors_clicked()
    _wait_for_gui_worker(app)
    assert not app._pending_neighbor_pdfs
    assert app._neighbor_plan_sources[paths[-1]] is not old_plan
    assert app._ready_source_signature == app._source_signature()
    app._on_close()


def test_photo_rotation_requires_new_analysis_but_draw_never_calls_ai(gui, tmp_path, monkeypatch):
    from tkinter import filedialog, messagebox
    from PIL import Image
    from acad_builder import DrawResult
    from llm_parser import ASPENWALL_SAMPLES, ParseOutcome, parse_with_regex

    path = tmp_path / "survey.jpg"
    Image.new("RGB", (30, 40), "white").save(path)
    desc, _ = parse_with_regex(ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"])
    calls, draws = [], []
    app = gui.SurveyApp()
    monkeypatch.setattr(app, "_ask_source_role", lambda _label: "main")
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **kw: (str(path),))
    monkeypatch.setattr(gui, "parse_legal_description_full", lambda **kw: (
        calls.append(kw) or ParseOutcome(data=desc, source="llm-vision")
    ))
    monkeypatch.setattr(gui.AcadBuilder, "draw_plan", lambda _builder, plan: (
        draws.append(plan) or DrawResult(plan=plan, text_count=0, layers={})
    ))
    monkeypatch.setattr(gui.sys, "platform", "win32")
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **kw: True)
    app.select_passes(1)
    app._load_images()
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    first = app._preview_main_plan
    assert len(calls) == 1 and calls[0]["rotation_deg"] == [0]
    app.rotate_visual(90)
    app._start(parse_only=False)
    assert not draws and len(calls) == 1 and "разберите" in app.status_var.get().lower()
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    current = app._preview_main_plan
    assert current is not first and len(calls) == 2 and calls[1]["rotation_deg"] == [90]
    app._start(parse_only=False)
    _wait_for_gui_worker(app)
    assert draws == [current] and len(calls) == 2
    app._on_close()


def test_checks_include_each_sources_typos_and_plan_warnings(gui, tmp_path, monkeypatch):
    from acad_builder import AcadBuilder, LotSuspicionReport, NeighborVerificationReport, NeighborPairReport
    from llm_parser import ASPENWALL_SAMPLES, ParseOutcome, TypoSuspicion, parse_with_regex

    app = gui.SurveyApp()
    desc, _ = parse_with_regex(ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"])
    builder = AcadBuilder(app.config_data)
    main = builder.plan(desc)
    neighbor = builder.plan(desc, is_neighbor=True)
    main.suspicion = LotSuspicionReport(shape_kind="основной", status="ok", items=[])
    neighbor.suspicion = LotSuspicionReport(shape_kind="сосед", status="ok", items=[])
    main.warnings = []
    neighbor.warnings = ["подпись стороны слишком длинная"]
    typo = TypoSuspicion(
        segment_index=2, kind="length_digit", original_value="54.00", suggested_value="56.00",
        digit_pair="4→6", is_lookalike=False, closure_before_ft=2.0, closure_after_ft=0.0,
        reason_ru="Сверить цифры на второй стороне",
    )
    outcome = ParseOutcome(data=desc, source="regex", typo_suspicions=[typo])
    monkeypatch.setattr(gui, "verify_with_neighbors", lambda *_: NeighborVerificationReport(
        overall_status="ok", pair_reports=[NeighborPairReport(1, "сосед", status="ok")],
    ))
    app._record_plan_for_preview(main, source_label="original.pdf", outcome=outcome)
    app._record_plan_for_preview(neighbor, source=tmp_path / "neighbor.pdf")
    app._select_result_tab("🏘 Соседи")
    assert app.result_tab_buttons["🛡 Проверки"].cget("fg_color") == "#92400e"
    assert app.result_tab_buttons["🏘 Соседи"].cget("fg_color") == "#059669"
    assert "Фигур: 2 · замечаний: 2" in app.suspicion_badge_button.cget("text")

    def texts(widget):
        return [str(widget.cget("text") or ""), str(widget.get() or "")] + [
            text for child in widget.winfo_children() for text in texts(child)
        ]

    visible = "\n".join(texts(app.checks_scroll_frame))
    assert all(text in visible for text in ("original.pdf", "neighbor.pdf", "54.00", "подпись стороны"))
    # Диагностика в отдельном окне и копируемый отчёт тоже содержат оба источника.
    app._open_lot_suspicion_window()
    report = "\n".join(texts(app._session_windows[-1]))
    assert all(text in report for text in ("original.pdf", "neighbor.pdf", "54.00", "подпись стороны"))
    assert main.suspicion.status == "ok" and main.suspicion.items == []  # отчёт геометрии не изменён

    # После замены обеих фигур старые замечания и привязки не остаются на схеме.
    replacement_main = builder.plan(desc)
    replacement_main.suspicion = LotSuspicionReport(shape_kind="основной", status="ok", items=[])
    replacement_main.warnings = []
    replacement_neighbor = builder.plan(desc, is_neighbor=True)
    replacement_neighbor.suspicion = LotSuspicionReport(shape_kind="сосед", status="ok", items=[])
    replacement_neighbor.warnings = []
    app._record_plan_for_preview(replacement_main, source_label="updated.pdf", outcome=ParseOutcome(data=desc, source="regex"))
    app._record_plan_for_preview(replacement_neighbor, source=tmp_path / "neighbor.pdf")
    visible = "\n".join(texts(app.checks_scroll_frame))
    assert "54.00" not in visible and "подпись стороны" not in visible
    assert id(main) not in app._plan_outcomes
    assert app.result_tab_buttons["🛡 Проверки"].cget("fg_color") == "#065f46"
    app._on_close()


def test_direct_neighbor_partial_results_and_failed_reparse_block_draw(gui, tmp_path, monkeypatch):
    from tkinter import filedialog, messagebox
    from acad_builder import DrawResult
    from llm_parser import ASPENWALL_SAMPLES

    sample = ASPENWALL_SAMPLES["Aspenwall St — 365' (Сосед слева)"]
    good, bad = tmp_path / "good.txt", tmp_path / "bad.txt"
    good.write_text(sample, encoding="utf-8")
    bad.write_text("неполный текст", encoding="utf-8")
    app = gui.SurveyApp()
    app._on_sample_selected("Aspenwall St — 420' (Основной участок)")
    app.use_llm_var.set(False)
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **_: (str(good), str(bad)))
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **kw: True)
    monkeypatch.setattr(gui.sys, "platform", "win32")
    drawn = []
    monkeypatch.setattr(gui.AcadBuilder, "draw_plan", lambda _builder, plan: (
        drawn.append(plan) or DrawResult(plan=plan, text_count=0, layers={})
    ))
    app._load_neighbor_files()
    _wait_for_gui_worker(app)
    assert good in app._neighbor_plan_sources and bad in app._pending_neighbor_files
    app._start(parse_only=False)
    assert not drawn and "bad.txt" in app.status_var.get()

    # Переключение на другой ввод не превращает ошибку пакета в успешный разбор.
    app._on_sample_selected("Aspenwall St — 420' (Основной участок)")
    app._start(parse_only=False)
    assert not drawn and "bad.txt" in app.status_var.get()
    bad.write_text(sample, encoding="utf-8")
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **_: (str(bad),))
    app._load_neighbor_files()
    _wait_for_gui_worker(app)
    assert not app._pending_neighbor_files
    app._start(parse_only=False)
    _wait_for_gui_worker(app)
    assert drawn == [app._preview_main_plan, *app._preview_neighbor_plans]

    # Неудачный повтор того же файла не должен разрешать отрисовку старого плана,
    # даже если затем успешно разобран другой источник.
    drawn.clear()
    old = app._neighbor_plan_sources[good]
    good.write_text("повтор не удался", encoding="utf-8")
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **_: (str(good),))
    app._load_neighbor_files()
    _wait_for_gui_worker(app)
    assert app._neighbor_plan_sources[good] is old and good in app._pending_neighbor_files
    app._on_sample_selected("Aspenwall St — 420' (Основной участок)")
    app._start(parse_only=False)
    assert not drawn and "good.txt" in app.status_var.get()
    good.write_text(sample, encoding="utf-8")
    app._load_neighbor_files()
    _wait_for_gui_worker(app)
    assert app._neighbor_plan_sources[good] is not old and not app._pending_neighbor_files
    app._start(parse_only=False)
    _wait_for_gui_worker(app)
    assert drawn == [app._preview_main_plan, *app._preview_neighbor_plans]
    app._on_close()


def test_empty_or_failed_main_source_cannot_be_bypassed_by_other_input(gui, monkeypatch):
    from tkinter import messagebox
    from acad_builder import DrawResult

    app = gui.SurveyApp()
    app._on_sample_selected("Aspenwall St — 420' (Основной участок)")
    monkeypatch.setattr(gui.sys, "platform", "win32")
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **kw: True)
    drawn = []
    monkeypatch.setattr(gui.AcadBuilder, "draw_plan", lambda _builder, plan: (
        drawn.append(plan) or DrawResult(plan=plan, text_count=0, layers={})
    ))

    # Полное удаление текста не должно обходить сравнение сигнатур через None.
    app.text_box.delete("1.0", "end")
    app._start(parse_only=False)
    assert not drawn and "разберите" in app.status_var.get().lower()
    app._clear_text()
    app._start(parse_only=False)
    assert not drawn and "разберите" in app.status_var.get().lower()

    app._on_sample_selected("Aspenwall St — 420' (Основной участок)")
    old_main = app._preview_main_plan
    monkeypatch.setattr(gui, "parse_legal_description_full", lambda **_: (_ for _ in ()).throw(RuntimeError("сбой анализа")))
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    assert app._preview_main_plan is old_main and app._main_analysis_pending
    app.source_mode_var.set("📷 Фото")  # пустой режим не снимает блокировку
    app._on_source_mode_changed()
    app._start(parse_only=False)
    assert not drawn and "разберите" in app.status_var.get().lower()

    app._on_sample_selected("Aspenwall St — 365' (Сосед слева)")
    assert app._preview_neighbor_plans and app._main_analysis_pending
    app._start(parse_only=False)
    assert not drawn and "разберите" in app.status_var.get().lower()
    # Явный успешный новый разбор основного участка снимает блокировку.
    app._on_sample_selected("Aspenwall St — 420' (Основной участок)")
    assert not app._main_analysis_pending
    app._start(parse_only=False)
    _wait_for_gui_worker(app)
    assert drawn == [app._preview_main_plan, *app._preview_neighbor_plans]
    app._on_close()


def test_neighbor_actions_do_not_silently_dismiss_unresolved_conflict(gui, tmp_path, monkeypatch):
    app = gui.SurveyApp()
    app.text_box.insert("1.0", "A sufficiently long main legal description")
    monkeypatch.setattr(app, "_ask_source_role", lambda *_: pytest.fail("диалог при конфликте"))
    for field in ("pending_conflict", "pending_doubt"):
        marker = {"unresolved": True}
        setattr(app, field, marker)
        app._on_verify_neighbors_clicked()
        assert app._preview_main_plan is None and getattr(app, field) is marker
        app._pick_and_verify_neighbor_files()
        app._start_neighbor_batch([tmp_path / "other.txt"], draw=False)
        assert getattr(app, field) is marker and not app._busy
        assert "разрешите" in app.status_var.get().lower()
        setattr(app, field, None)
    app._on_close()


def test_failed_text_batch_can_be_retried_singly_and_conflict_resolved(gui, tmp_path, monkeypatch):
    from tkinter import filedialog, messagebox
    import llm_parser
    from acad_builder import DrawResult
    from llm_parser import ASPENWALL_SAMPLES, TokenUsage, parse_with_regex

    sample = ASPENWALL_SAMPLES["Aspenwall St — 365' (Сосед слева)"]
    good, conflict_file = tmp_path / "agreed.txt", tmp_path / "conflict.txt"
    good.write_text(sample + "\nAGREED", encoding="utf-8")
    conflict_file.write_text(sample + "\nDISPUTED", encoding="utf-8")
    desc, _ = parse_with_regex(sample)
    different = desc.model_copy(update={"distance_to_corner_ft": 370.0})
    calls = []

    def extract(text, cfg, api_key=None, progress=None, *, provider=None, model=None,
                key_secret="", image_data_urls=None, focus_instruction=None):
        calls.append((provider, "DISPUTED" in text))
        choice = different if "DISPUTED" in text and provider == "openrouter" else desc
        return choice, "structured-outputs", TokenUsage(
            prompt_tokens=1, completion_tokens=1, total_tokens=2, provider=str(provider), model=str(model),
        )

    monkeypatch.setattr(llm_parser, "_extract_with_llm", extract)
    app = gui.SurveyApp()
    app._on_sample_selected("Aspenwall St — 420' (Основной участок)")
    app.use_llm_var.set(True)
    app.select_passes(2)
    app.provider_key_vars["hydra"].set("sk-test")
    app.provider_key_vars["openrouter"].set("sk-test")
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **_: (str(good), str(conflict_file)))
    app._load_neighbor_files()
    _wait_for_gui_worker(app)
    assert good in app._neighbor_plan_sources and conflict_file in app._pending_neighbor_files
    assert "разошлись" in app.log_text()

    # Повтор одним TXT открывает обычный конфликт: источник остаётся привязан
    # к тому же файлу и только решение человека снимает блокировку.
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **_: (str(conflict_file),))
    reports = []
    monkeypatch.setattr(app, "_open_neighbor_report_window", lambda: reports.append(app._neighbor_report))
    app._pick_and_verify_neighbor_files()
    _wait_for_gui_worker(app)
    assert app.pending_conflict is not None and conflict_file in app._pending_neighbor_files
    assert not reports
    app.resolve_conflict_with_pass(1)
    _wait_for_gui_worker(app)
    assert app.pending_conflict is None and not app._pending_neighbor_files
    assert len(reports) == 1 and len(reports[0].pair_reports) == 2
    assert app._neighbor_plan_sources[conflict_file].is_neighbor
    assert "conflict.txt" in app._plan_source_labels[id(app._neighbor_plan_sources[conflict_file])]

    drawn = []
    monkeypatch.setattr(gui.sys, "platform", "win32")
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **kw: True)
    monkeypatch.setattr(gui.AcadBuilder, "draw_plan", lambda _builder, plan: (
        drawn.append(plan) or DrawResult(plan=plan, text_count=0, layers={})
    ))
    before = len(calls)
    app._start(parse_only=False)
    _wait_for_gui_worker(app)
    assert drawn == [app._preview_main_plan, *app._preview_neighbor_plans]
    assert len(calls) == before  # отрисовка никогда не повторяет проходы ИИ
    app._on_close()


def test_direct_batch_cannot_replace_an_attached_pdf_with_different_pages_silently(gui, tmp_path, monkeypatch):
    from tkinter import filedialog
    from llm_parser import ASPENWALL_SAMPLES

    first_page = ASPENWALL_SAMPLES["Aspenwall St — 365' (Сосед слева)"]
    second_page = ASPENWALL_SAMPLES["Aspenwall St — 475' (Сосед справа)"]
    attached = [tmp_path / "a.pdf", tmp_path / "b.pdf"]
    extra = tmp_path / "extra.pdf"
    for path in [*attached, extra]:
        _write_test_pdf(path, [first_page, second_page])
    app = gui.SurveyApp()
    app._on_sample_selected("Aspenwall St — 420' (Основной участок)")
    app.neighbor_lot_radio.select()
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **_: tuple(map(str, attached)))
    app._load_pdf()
    app.pdf_pages_var.set("2")
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    assert app._neighbor_plan_sources[attached[0]].description.distance_to_corner_ft == pytest.approx(475.0)

    # Прямой выбор соседей без диапазона повторно читает один прикреплённый PDF
    # целиком. Даже при совпавшей сигнатуре видимого поля это не прежняя страница 2.
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **_: (str(attached[0]), str(extra)))
    app._load_neighbor_files()
    _wait_for_gui_worker(app)
    assert app._neighbor_parsed_pages[attached[0]] is None
    assert app._source_signature() == app._ready_source_signature
    monkeypatch.setattr(gui.AcadBuilder, "draw_plan", lambda *a, **kw: pytest.fail("устаревшие страницы"))
    app._start(parse_only=False)
    assert "PDF соседей" in app.status_var.get()

    app._on_verify_neighbors_clicked()
    _wait_for_gui_worker(app)
    assert app._neighbor_parsed_pages[attached[0]] == "2"
    assert app._neighbor_plan_sources[attached[0]].description.distance_to_corner_ft == pytest.approx(475.0)
    app._on_close()


def test_switching_to_empty_source_tab_does_not_invalidate_unchanged_ready_plan(gui, monkeypatch):
    from tkinter import messagebox
    from acad_builder import DrawResult

    app = gui.SurveyApp()
    app._on_sample_selected("Aspenwall St — 420' (Основной участок)")
    ready = app._preview_main_plan
    monkeypatch.setattr(gui.sys, "platform", "win32")
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **kw: True)
    monkeypatch.setattr(gui, "parse_legal_description_full", lambda **_: pytest.fail("повторный разбор"))
    drawn = []
    monkeypatch.setattr(gui.AcadBuilder, "draw_plan", lambda _builder, plan: (
        drawn.append(plan) or DrawResult(plan=plan, text_count=0, layers={})
    ))
    app.source_mode_var.set("📷 Фото")  # источник в этой вкладке ещё не выбран
    app._on_source_mode_changed()
    app._start(parse_only=False)
    _wait_for_gui_worker(app)
    assert drawn == [ready]
    app.text_box.delete("1.0", "end")  # прежний источник больше не совпадает
    app._start(parse_only=False)
    assert drawn == [ready] and "разберите" in app.status_var.get().lower()
    app._on_close()


def test_unreadable_reselected_neighbor_pdf_keeps_old_plan_blocked(gui, tmp_path, monkeypatch):
    from tkinter import filedialog
    from llm_parser import ASPENWALL_SAMPLES

    path = tmp_path / "reopened.pdf"
    _write_test_pdf(path, [ASPENWALL_SAMPLES["Aspenwall St — 365' (Сосед слева)"]])
    valid_bytes = path.read_bytes()
    app = gui.SurveyApp()
    app._on_sample_selected("Aspenwall St — 420' (Основной участок)")
    app.neighbor_lot_radio.select()
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **_: (str(path),))
    app._load_pdf()
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    old = app._neighbor_plan_sources[path]

    path.write_bytes(b"invalid PDF")
    app._load_pdf()
    assert app._neighbor_plan_sources[path] is old and path in app._pending_neighbor_files
    app.source_mode_var.set("📝 Текст")
    app._on_source_mode_changed()
    monkeypatch.setattr(gui.AcadBuilder, "draw_plan", lambda *a, **kw: pytest.fail("нечитабельный источник"))
    app._start(parse_only=False)
    assert "reopened.pdf" in app.status_var.get()

    path.write_bytes(valid_bytes)
    app._load_pdf()
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    assert app._neighbor_plan_sources[path] is not old
    assert path not in app._pending_neighbor_files
    app._on_close()


# ------------------------------------- Шаг 2: вторая глобальная вкладка окна --

def _step2_result(skip_existing=True):
    """Готовый FeatureLabelResult на синтетическом чертеже (без AutoCAD)."""
    import acad_builder as ab
    import feature_labeler as fl
    from feature_labeler import DrawingLine, DrawingText
    from test_feature_labeling import HOUSE, build_scene

    snap = build_scene(with_existing=True)
    # Заметка полевика с выноской к террасе: WOOD PORCH -> FRAME DECK.
    snap.texts.append(DrawingText("ENTRANCE_TEXT", "WOOD PORCH", (-14.0, 66.0), height=1.3))
    snap.lines.append(DrawingLine("ENTRANCE_TEXT", ((-4.0, 66.6), (15.0, 67.0))))
    boundary = fl.find_property_polygon(snap, {})
    recognition = fl.recognize_features(snap, boundary, {}, HOUSE)
    plan = ab.plan_feature_labels(recognition, snap, {}, text_height=0.9, skip_existing=skip_existing)
    return ab.FeatureLabelResult(
        plan=plan, layer=plan.output_layer, created_labels=len(plan.items),
        created_texts=plan.count_placement("along"),
        created_mtexts=len(plan.items) - plan.count_placement("along"),
        created_leaders=plan.count_placement("leader"),
    )


def _wait_idle(app):
    import time

    for _ in range(100):
        app._drain_events()
        if not app._busy:
            return
        time.sleep(0.02)
    raise AssertionError("фоновое задание не завершилось")


def test_step2_tab_is_second_global_tab_and_keeps_step1(gui):
    app = gui.SurveyApp()

    assert gui.STEP_TABS == (
        "Шаг 1. Граница участка (Legal Description)",
        "Шаг 2. Автоподписание объектов (D-TOPO-TEXT)",
    )
    assert list(app.step_tab_buttons) == list(gui.STEP_TABS)
    # По умолчанию открыт Шаг 1: рабочая зона границы и её кнопки на месте.
    assert app.step_tab_var.get() == gui.STEP_TAB_BOUNDARY
    assert app.main_wrap._gridded is True
    assert app.step2_page._gridded is False
    assert app.draw_button._gridded is True

    # Элементы вкладки Шага 2 из постановки задачи.
    assert app.house_text_entry.cget("placeholder_text") == "например: 2 STY. BRK. & FR."
    assert app.skip_existing_labels_check.cget("text") == (
        "Не дублировать уже существующие на чертеже надписи"
    )
    assert app.skip_existing_labels_var.get() is True
    assert app.hide_field_notes_check.cget("text") == (
        "Выключить слои полевых заметок после подписания"
    )
    assert app.hide_field_notes_var.get() is True
    assert "FENCE_NOTE" in app.step2_notes_hint.cget("text")
    assert "Автопоиск участка и подписание объектов" in app.feature_auto_button.cget("text")
    assert "Указать границу участка в AutoCAD и подписать объекты" in app.feature_pick_button.cget("text")
    assert "0.900 ft" in app.step2_height_label.cget("text")

    app._select_step_tab(gui.STEP_TAB_LABELS)
    assert app.main_wrap._gridded is False
    assert app.step2_page._gridded is True
    assert app.draw_button._gridded is False
    assert app.add_neighbor_button._gridded is False
    assert app.step_tab_buttons[gui.STEP_TAB_LABELS].cget("fg_color") == gui.THEME["btn_primary"]
    assert app.step_tab_buttons[gui.STEP_TAB_BOUNDARY].cget("fg_color") == gui.THEME["bg_field"]

    # Клавиши Шага 1 на вкладке Шага 2 не запускают разбор и отрисовку границы.
    started = []
    app._start = lambda **kwargs: started.append(kwargs)
    assert app._global_bindings["<F5>"]() is None
    assert app._global_bindings["<Control-Return>"]() is None
    assert started == []

    # Возврат на Шаг 1 восстанавливает рабочую зону и клавиши.
    app._select_step_tab(gui.STEP_TAB_BOUNDARY)
    assert app.main_wrap._gridded is True
    assert app.step2_page._gridded is False
    assert app.draw_button._gridded is True
    assert app.add_neighbor_button._gridded is True
    assert app._global_bindings["<F5>"]() == "break"
    assert started == [{"parse_only": False}]

    # Высота текста подписей следует за масштабом из шапки.
    app.scale_var.set('1"=20\'')
    app._refresh_height_label()
    assert "1.125 ft" in app.step2_height_label.cget("text")


def test_step2_house_text_normalization(gui):
    assert gui.normalize_house_text("  2 STY.   BRK. & FR. ") == "2 STY. BRK. & FR."
    assert gui.normalize_house_text("2 STY. FRAME / No. 87") == "2 STY. FRAME\nNo. 87"
    assert gui.normalize_house_text("2 STY. FRAME | DWELLING") == "2 STY. FRAME\nDWELLING"
    assert gui.normalize_house_text("1 1/2 STY. FRAME") == "1 1/2 STY. FRAME"  # дробь не разрывается
    assert gui.normalize_house_text("   ") == ""
    assert gui.normalize_house_text(None) == ""


@pytest.mark.parametrize("mode", ["auto", "pick"])
def test_step2_buttons_run_labeling_and_report_to_log(gui, monkeypatch, mode):
    monkeypatch.setattr(gui.sys, "platform", "win32")
    calls = []
    prepared = _step2_result()

    def fake_label_features(builder, **kwargs):
        calls.append(kwargs)
        kwargs["progress"]("Читаю объекты активного чертежа AutoCAD…")
        assert kwargs["should_continue"]() is True
        return prepared

    monkeypatch.setattr(gui.AcadBuilder, "label_features", fake_label_features)

    app = gui.SurveyApp()
    app._select_step_tab(gui.STEP_TAB_LABELS)
    app.house_text_var.set("2 STY. BRK. & FR. / No. 87")
    app.scale_var.set('1"=20\'')
    button = app.feature_auto_button if mode == "auto" else app.feature_pick_button
    button.cget("command")()
    assert app._busy is True
    assert app.feature_auto_button.cget("state") == "disabled"
    assert app.feature_pick_button.cget("state") == "disabled"
    _wait_idle(app)

    assert len(calls) == 1
    assert calls[0]["mode"] == mode
    assert calls[0]["house_text"] == "2 STY. BRK. & FR.\nNo. 87"
    assert calls[0]["skip_existing"] is True
    assert calls[0]["hide_field_notes"] is True
    assert calls[0]["scale"] == pytest.approx(20.0)
    assert app.feature_auto_button.cget("state") == "normal"
    assert app.feature_pick_button.cget("state") == "normal"
    assert app.feature_label_result is prepared

    # Счётчики и подробный отчёт на вкладке Шага 2.
    assert app.step2_stat_vars["notes"].get() == "1"
    assert app.step2_stat_vars["codes"].get() == "2"
    assert app.step2_stat_vars["layers"].get() == "14"
    assert app.step2_stat_vars["labeled"].get() == "15"
    assert app.step2_stat_vars["skipped"].get() == "3"
    report = app.step2_report_box.get("1.0", "end")
    assert "по заметкам полевого чертёжника (перевод в формулировки заказчика): 1" in report
    assert "по кодам точек (POINT_LABELS): 2" in report
    assert "по именам слоёв: 14" in report
    assert "ПОДПИСАНО: 15" in report
    assert "полевая заметка «WOOD PORCH»" in report
    # Тот же отчёт — в журнале операций (лог-панель).
    log = "\n".join(app._log)
    assert "Шаг 2 · • по кодам точек (POINT_LABELS): 2" in log
    assert "Шаг 2 · • по именам слоёв: 14" in log
    assert "Шаг 2 · ПОДПИСАНО: 15" in log
    assert "Подписано объектов: 15 в слое D-TOPO-TEXT" in app.status_var.get()

    # Отчёт копируется и очищается.
    app._copy_step2_report()
    assert "ПОДПИСАНО: 15" in app._clipboard
    app._clear_step2_report()
    assert app.feature_label_result is None
    assert app.step2_stat_vars["labeled"].get() == "—"
    assert app.step2_report_box.get("1.0", "end") == gui.STEP2_EMPTY_REPORT


def test_step2_passes_dedup_checkbox_state(gui, monkeypatch):
    monkeypatch.setattr(gui.sys, "platform", "win32")
    calls = []
    monkeypatch.setattr(
        gui.AcadBuilder, "label_features",
        lambda builder, **kwargs: calls.append(kwargs) or _step2_result(skip_existing=False),
    )
    app = gui.SurveyApp()
    app.skip_existing_labels_var.set(False)
    app.hide_field_notes_var.set(False)
    app._start_feature_labeling("auto")
    _wait_idle(app)
    assert calls[0]["skip_existing"] is False
    assert calls[0]["hide_field_notes"] is False
    assert calls[0]["house_text"] == ""
    assert app.step2_stat_vars["skipped"].get() == "0"
    assert app.step2_stat_vars["labeled"].get() == "18"


def test_step2_error_is_shown_in_report_and_status(gui, monkeypatch):
    monkeypatch.setattr(gui.sys, "platform", "win32")

    def fail(builder, **kwargs):
        raise gui.AcadBuilderError("в слоях границы не найден замкнутый контур участка")

    monkeypatch.setattr(gui.AcadBuilder, "label_features", fail)
    app = gui.SurveyApp()
    app._start_feature_labeling("auto")
    _wait_idle(app)
    report = app.step2_report_box.get("1.0", "end")
    assert report.startswith("ШАГ 2 НЕ ВЫПОЛНЕН")
    assert "не найден замкнутый контур участка" in report
    assert "Указать границу участка в AutoCAD" in report  # подсказка для автопоиска
    assert "Шаг 2 не выполнен" in app.status_var.get()
    assert app.feature_label_result is None
    assert app._busy is False
    assert app.feature_auto_button.cget("state") == "normal"


def test_step2_requires_windows_autocad(gui, monkeypatch):
    monkeypatch.setattr(gui.sys, "platform", "linux")
    app = gui.SurveyApp()
    app._start_feature_labeling("pick")
    _wait_idle(app)
    assert "только в Windows" in app.step2_report_box.get("1.0", "end")
    assert "Шаг 2 не выполнен" in app.status_var.get()
