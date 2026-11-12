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


class _Widget:
    def __init__(self, *args, **kwargs):
        self._kwargs = dict(kwargs)
        self._show = kwargs.get("show", "")
        self._text = kwargs.get("text", "")
        self._values = list(kwargs.get("values") or [])
        var = kwargs.get("variable") or kwargs.get("textvariable")
        self._value = var.get() if var is not None else (self._values[0] if self._values else "")

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
        return None

    def get(self, *args, **kwargs):
        var = self._kwargs.get("variable") or self._kwargs.get("textvariable")
        if var is not None:
            return var.get()
        return self._value

    def delete(self, *args, **kwargs):
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

    def title(self, *args):
        return None

    def geometry(self, *args):
        return None

    def minsize(self, *args):
        return None

    def attributes(self, *args, **kwargs):
        return None

    def protocol(self, *args, **kwargs):
        return None

    def mainloop(self):
        return None

    def destroy(self):
        return None

    def quit(self):
        return None

    def grid_columnconfigure(self, *args, **kwargs):
        return None

    def grid_rowconfigure(self, *args, **kwargs):
        return None

    def winfo_children(self):
        return []


def _fake_modules() -> dict[str, types.ModuleType]:
    ctk = types.ModuleType("customtkinter")
    for name in (
        "CTk",
        "CTkFrame",
        "CTkLabel",
        "CTkButton",
        "CTkEntry",
        "CTkOptionMenu",
        "CTkComboBox",
        "CTkSegmentedButton",
        "CTkTextbox",
        "CTkProgressBar",
        "CTkSwitch",
        "CTkToplevel",
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

