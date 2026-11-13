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
    ctk.CTkFont = lambda **kwargs: None
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
    """
    Проверяет:
      * переключатель CTkRadioButton «Основной участок» ↔ «Соседний участок»;
      * кнопку «Добавить соседа на чертёж» (force_neighbor=True);
      * пакетную загрузку нескольких файлов соседей (askopenfilenames) в единую СК от (0, 0).
    """
    import time
    from tkinter import filedialog

    from llm_parser import ASPENWALL_SAMPLES

    app = gui.SurveyApp()
    assert app.is_neighbor_mode() is False
    assert app.draw_button.cget("text") == "Начертить участок в AutoCAD"

    # Переключаем радиокнопку в режим «Соседний участок»
    app.neighbor_lot_radio.select()
    assert app.is_neighbor_mode() is True
    assert app.draw_button.cget("text") == "Начертить соседа в AutoCAD"

    # Возврат на основной участок по выбору пресета Aspenwall 420'
    app._on_sample_selected("Aspenwall St — 420' (Основной участок)")
    assert app.is_neighbor_mode() is False
    assert app.draw_button.cget("text") == "Начертить участок в AutoCAD"

    # Подменяем отрисовку AutoCAD для проверки force_neighbor и пакетного режима на Linux
    drawn_calls: list[tuple[float, bool]] = []

    def fake_draw(self_builder, description, scale=None, text_height=None, *, is_neighbor=False, neighbor_label=None):
        from acad_builder import DrawResult

        plan = self_builder.plan(
            description, scale=scale, text_height=text_height, is_neighbor=is_neighbor, neighbor_label=neighbor_label
        )
        drawn_calls.append((description.distance_to_corner_ft, is_neighbor))
        layers = {"neighbor_lot": "NEIGHBOR_LOT"} if is_neighbor else {"property_line": "PROPERTY_LINE", "boundary_text": "BOUNDARY_TEXT_AI"}
        return DrawResult(plan=plan, text_count=len(plan.items), layers=layers)

    monkeypatch.setattr(gui.AcadBuilder, "draw", fake_draw)
    monkeypatch.setattr(gui.sys, "platform", "win32")

    # 1) Нажимаем «Начертить участок в AutoCAD» (основной 420', regex)
    app.use_llm_var.set(False)
    app._start(parse_only=False)
    for _ in range(40):
        app._drain_events()
        if not app._busy:
            break
        time.sleep(0.02)
    assert drawn_calls == [(420.0, False)]

    # 2) Выбираем текст соседа и жмём «Добавить соседа на чертёж» (force_neighbor=True)
    app.lot_role_var.set("main")
    app.text_box.delete("1.0", "end")
    app.text_box.insert("1.0", ASPENWALL_SAMPLES["Aspenwall St — 365' (Сосед слева)"])
    app._start(parse_only=False, force_neighbor=True)
    for _ in range(40):
        app._drain_events()
        if not app._busy:
            break
        time.sleep(0.02)
    assert drawn_calls[-1] == (365.0, True)
    assert "NEIGHBOR_LOT" in app.status_var.get()

    # 3) Пакетная загрузка 2 файлов соседей (365' и 475')
    f_left = tmp_path / "neighbor_365.txt"
    f_right = tmp_path / "neighbor_475.txt"
    f_left.write_text(ASPENWALL_SAMPLES["Aspenwall St — 365' (Сосед слева)"], encoding="utf-8")
    f_right.write_text(ASPENWALL_SAMPLES["Aspenwall St — 475' (Сосед справа)"], encoding="utf-8")
    monkeypatch.setattr(
        filedialog, "askopenfilenames", lambda **_: (str(f_left), str(f_right))
    )
    app._load_and_draw_neighbor_files()
    for _ in range(50):
        app._drain_events()
        if not app._busy:
            break
        time.sleep(0.02)

    assert drawn_calls[-2:] == [(365.0, True), (475.0, True)]
    assert "обработано 2 уч. в слое NEIGHBOR_LOT" in app.status_var.get()
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

    # --- Сценарий В: конфликт при нажатии «Начертить в AutoCAD» -> человек выбирает Проход #1 ---
    drawn_distances: list[float] = []

    def fake_draw(self_builder, description, scale=None, text_height=None, *, is_neighbor=False, neighbor_label=None):
        from acad_builder import DrawResult

        plan = self_builder.plan(description, scale=scale)
        drawn_distances.append(description.distance_to_corner_ft)
        return DrawResult(plan=plan, text_count=len(plan.items), layers={"property_line": "PROPERTY_LINE", "boundary_text": "BOUNDARY_TEXT_AI"})

    monkeypatch.setattr(gui.AcadBuilder, "draw", fake_draw)
    monkeypatch.setattr(gui.sys, "platform", "win32")
    app.provider_key_vars["hydra"].set("sk-hydra-test")
    app.provider_key_vars["openrouter"].set("sk-or-test")

    app._start(parse_only=False)
    for _ in range(40):
        app._drain_events()
        if not app._busy:
            break
        time.sleep(0.02)

    # Пока конфликт не разрешён — в AutoCAD ничего не нарисовано!
    assert drawn_distances == []
    assert app.pending_conflict is not None

    # Человек посмотрел в документ и нажал «Прав Проход #1» (420.00')
    app.resolve_conflict_with_pass(1)
    for _ in range(40):
        app._drain_events()
        if not app._busy:
            break
        time.sleep(0.02)

    assert app.pending_conflict is None
    assert drawn_distances == [pytest.approx(420.0)]
    assert "Конфликт проходов разрешён человеком" in app.status_var.get()
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





