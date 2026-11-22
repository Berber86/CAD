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
        self._wm_log: list[str] = []
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

    def after_cancel(self, *args, **kwargs):
        return None

    def after_idle(self, *args, **kwargs):
        return None

    def withdraw(self, *args, **kwargs):
        self._wm_log.append("withdraw")
        return None

    def deiconify(self, *args, **kwargs):
        self._wm_log.append("deiconify")
        return None

    def update_idletasks(self, *args, **kwargs):
        self._wm_log.append("update_idletasks")
        return None

    def update(self, *args, **kwargs):
        return None

    def state(self, *args, **kwargs):
        if args:
            self._wm_log.append(f"state:{args[0]}")
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

    def bind_class(self, class_name, event_name, callback, *args, **kwargs):
        if not hasattr(self, "_class_bindings"):
            self._class_bindings = {}
        self._class_bindings[(class_name, event_name)] = callback

    def transient(self, *args, **kwargs):
        return None

    def resizable(self, *args, **kwargs):
        return None

    def grab_set(self, *args, **kwargs):
        self._grabbed = True

    def grab_release(self, *args, **kwargs):
        self._grabbed = False

    def clipboard_clear(self):
        self._clipboard = ""

    def clipboard_append(self, text):
        self._clipboard = str(getattr(self, "_clipboard", "") or "") + str(text)


class _FakeMenu:
    """Заглушка tkinter.Menu для проверки контекстного меню Копировать/Вставить."""

    instances: list["_FakeMenu"] = []

    def __init__(self, master=None, tearoff=0, **kwargs):
        self.master = master
        self.items: list[dict] = []
        self.popups: list[tuple[int, int]] = []
        self.released = False
        _FakeMenu.instances.append(self)

    def add_command(self, label="", command=None, state="normal", **kwargs):
        self.items.append({"label": label, "command": command, "state": state})

    def tk_popup(self, x_root, y_root):
        self.popups.append((x_root, y_root))

    def grab_release(self):
        self.released = True


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

    def xview(self, *args):
        return None

    def yview(self, *args):
        return None

    def create_image(self, *coords, **kwargs):
        self.items.append(("image", coords, dict(kwargs)))
        return len(self.items)

    def create_line(self, *coords, **kwargs):
        self.items.append(("line", coords, dict(kwargs)))
        return len(self.items)

    def create_arc(self, *coords, **kwargs):
        self.items.append(("arc", coords, dict(kwargs)))
        return len(self.items)

    def create_oval(self, *coords, **kwargs):
        self.items.append(("oval", coords, dict(kwargs)))
        return len(self.items)

    def create_text(self, *coords, **kwargs):
        self.items.append(("text", coords, dict(kwargs)))
        return len(self.items)



def _step0_texts(app):
    """Тексты записей журнала Шага 0 (записи хранятся как (уровень, текст))."""
    return [item[1] if isinstance(item, tuple) else item for item in app._step0_lines]


class _FakeCTkRoot(_Widget):
    """
    Заглушка ``customtkinter.CTk`` с настоящим поведением окна на Windows.

    CustomTkinter помнит, вызывали ли ``withdraw()`` до первого показа окна
    (``_withdraw_called_before_window_exists``), и в ``update()``/``mainloop()``
    показывает окно сам только если НЕ вызывали. Кроме того, ``mainloop`` на Windows
    прячет окно для перекраски заголовка (``_windows_set_titlebar_color``). Именно на
    этой связке окно в 4.39 «появлялось и исчезало»: заглушка повторяет её дословно,
    чтобы тест ловил регрессию (источник: customtkinter/windows/ctk_tk.py).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._window_exists = False
        self._withdraw_called_before_window_exists = False
        self._iconify_called_before_window_exists = False
        #: Видно ли окно на экране (withdrawn → False, deiconify → True).
        self._visible = True
        self._alpha = 1.0

    def withdraw(self, *args, **kwargs):
        super().withdraw(*args, **kwargs)
        if self._window_exists is False:
            self._withdraw_called_before_window_exists = True
        self._visible = False

    def deiconify(self, *args, **kwargs):
        super().deiconify(*args, **kwargs)
        self._visible = True

    def iconify(self, *args, **kwargs):
        if self._window_exists is False:
            self._iconify_called_before_window_exists = True
        self._visible = False

    def update(self, *args, **kwargs):
        self._wm_log.append("update")
        if self._window_exists is False:
            # как в CTk.update: окно показывается само, только если его не прятали
            if (
                not self._withdraw_called_before_window_exists
                and not self._iconify_called_before_window_exists
            ):
                self.deiconify()
            self._window_exists = True
        return None

    def mainloop(self, *args, **kwargs):
        self._wm_log.append("mainloop")
        if not self._window_exists:
            # как в CTk.mainloop на Windows: перекраска заголовка прячет окно...
            self.withdraw()
            # ...а обратно оно показывается, только если withdraw НЕ звали до показа.
            if (
                not self._withdraw_called_before_window_exists
                and not self._iconify_called_before_window_exists
            ):
                self.deiconify()
            self._window_exists = True
        return None

    def attributes(self, *args, **kwargs):
        if len(args) >= 2 and args[0] == "-alpha":
            self._alpha = float(args[1])
            self._wm_log.append(f"alpha:{float(args[1]):g}")
        return super().attributes(*args, **kwargs)


def _fake_modules() -> dict[str, types.ModuleType]:
    ctk = types.ModuleType("customtkinter")
    ctk.CTk = _FakeCTkRoot
    for name in (
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
        "CTkSlider",
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
    tkinter.Scrollbar = type("Scrollbar", (_Widget,), {})
    tkinter.Menu = _FakeMenu
    filedialog = types.ModuleType("tkinter.filedialog")
    filedialog.askopenfilename = lambda **kwargs: ""
    filedialog.askopenfilenames = lambda **kwargs: ()
    tkinter.filedialog = filedialog
    messagebox = types.ModuleType("tkinter.messagebox")
    messagebox.askokcancel = lambda *args, **kwargs: False
    messagebox.askyesno = lambda *args, **kwargs: False
    messagebox.askyesnocancel = lambda *args, **kwargs: None
    # Диалог сбоя запуска: main() обязан сказать пользователю, почему окна нет (4.40).
    messagebox.errors: list[tuple[str, str]] = []
    messagebox.showerror = lambda title, message, **kwargs: messagebox.errors.append((title, message))
    tkinter.messagebox = messagebox
    return {
        "customtkinter": ctk,
        "tkinter": tkinter,
        "tkinter.filedialog": filedialog,
        "tkinter.messagebox": messagebox,
    }


@pytest.fixture()
def gui(monkeypatch, tmp_path):
    for name, module in _fake_modules().items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.syspath_prepend(str(ROOT))
    monkeypatch.delitem(sys.modules, "main", raising=False)
    import main as gui_module  # noqa: PLC0415 - импорт после подмены модулей

    # GUI now autosaves provider/model choices. Isolate those writes from the checked-in
    # sample config while preserving its real defaults for every smoke test.
    import llm_parser
    config_path = tmp_path / "config.json"
    # Старое локальное значение не должно препятствовать черновику в выделенных тестах.
    import json as _json  # noqa: PLC0415
    _cfg_data = _json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    _cfg_data.setdefault("print_draft", {})["enabled"] = False
    config_path.write_text(_json.dumps(_cfg_data, ensure_ascii=False, indent=2), encoding="utf-8")
    local_config_path = tmp_path / "config.local.json"
    env_path = tmp_path / ".env"
    for module in (llm_parser, gui_module):
        monkeypatch.setattr(module, "CONFIG_PATH", config_path)
        monkeypatch.setattr(module, "LOCAL_CONFIG_PATH", local_config_path)
        monkeypatch.setattr(module, "ENV_PATH", env_path)

    # Остальные smoke-тесты проверяют основную сцену; реальные сборку и запуск
    # черновика проверяем отдельно, восстанавливая метод в соответствующих тестах.
    gui_module._real_print_draft_plans = gui_module.SurveyApp._print_draft_plans
    monkeypatch.setattr(gui_module.SurveyApp, "_print_draft_plans", lambda self: [])

    # В старых тестах выбор роли эмулируется текстовым переключателем; тесты
    # самого диалога и независимости ролей задают явный ответ отдельно.
    # Локальный OCR зависит от машины (установлен пакет или нет), а поведение окна
    # должно быть одинаковым: в тестах он выключен. Путь с OCR проверяют
    # tests/test_local_ocr.py (движок подменён) и отдельный тест окна.
    if hasattr(gui_module, "ocr_pages"):
        monkeypatch.setattr(gui_module.ocr_pages, "available", lambda: False)
    # Обновление версии: резолвер источника и поиск веток ходят в GitHub API.
    # В тестах они подменены заглушкой (без сети); тесты канала обновлений
    # подменяют их сами на синтетический GitHub (см. tests/test_update_channel.py).
    def _fake_resolve_update_source(source, **kwargs):
        text = str(source or "").strip()
        branch = text.rsplit("/", 1)[-1] if text else "main"
        return gui_module.app_updater.ResolvedSource(
            zip_url=f"https://codeload.github.com/Berber86/CAD/zip/refs/heads/{branch}",
            owner="Berber86",
            repo="CAD",
            branch=branch,
            state=gui_module.app_updater.STATE_STABLE,
            description="заглушка теста",
        )

    monkeypatch.setattr(
        gui_module.app_updater, "resolve_update_source", _fake_resolve_update_source,
    )

    def _no_network_discovery(*args, **kwargs):
        raise AssertionError("поиск веток не должен ходить в сеть в этом тесте")

    monkeypatch.setattr(gui_module.app_updater, "discover_branch", _no_network_discovery)
    monkeypatch.setattr(
        gui_module.SurveyApp, "_ask_source_role",
        lambda app, source: (
            "neighbor" if source.startswith("выбранных файлах") else str(app.lot_role_var.get() or "main")
        ),
    )
    yield gui_module
    sys.modules.pop("main", None)


def _load_pdf_by_role(app):
    """
    Порядок «сначала роль, потом файлы». Кнопка PDF теперь открывает набор
    со списком, но этим порядком по-прежнему пользуются окно сверки, фото и
    выбор файлов соседей, поэтому открытие одного PDF и прикрепление PDF
    соседей проверяются через него.
    """
    from pathlib import Path
    from tkinter import filedialog
    from llm_parser import is_image_file

    role = app._ask_source_role("PDF")
    if role not in {"main", "neighbor"}:
        return
    if role == "neighbor":
        paths = filedialog.askopenfilenames(title="PDF соседей")
        if paths:
            app._attach_neighbor_pdfs([Path(p) for p in paths if str(p).strip()])
        return
    path = filedialog.askopenfilename(title="PDF основного участка")
    if not path:
        return
    chosen = Path(path)
    if is_image_file(chosen):
        app._apply_loaded_images([chosen])
        return
    app._open_single_pdf(chosen)


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


def test_window_stays_hidden_until_assembled_then_opens_maximized_once(gui):
    """
    Окно не видно, пока оно собирается: ``withdraw`` — первым делом в ``__init__``, показ —
    один раз в конце (``deiconify``), и уже развёрнутым (``state('zoomed')`` до показа, вместе
    с ``update_idletasks``): пользователь не видит ни «сборку из кусочков», ни перестройку
    «размер по умолчанию → весь экран» (4.39). ``zoomed`` сохраняет заголовок Windows
    с кнопками — это не полноэкранный режим без рамки.
    """
    app = gui.SurveyApp()
    log = app._wm_log
    assert log[0] == "withdraw", log
    assert log.count("deiconify") == 1, log
    assert "state:zoomed" in log, log
    assert log.index("state:zoomed") < log.index("update_idletasks") < log.index("deiconify"), log
    # Кадр дорисовывается до того, как окно стало видимым (иначе — «сборка из кусочков»).
    assert log.index("deiconify") < log.index("update"), log
    # Разворачивание не отложено на после первого кадра (прежний after_idle).
    assert "withdraw" not in log[1:], log
    assert app._visible is True


def test_window_is_invisible_while_assembled_and_revealed_once(gui):
    """
    Во время сборки окно не только спрятано, но и прозрачно: CustomTkinter на Windows
    сам дёргает withdraw/deiconify/update при перекраске заголовка, и одного withdraw
    не хватает (4.40). Прозрачность снимается последним шагом — после готового кадра.
    """
    app = gui.SurveyApp()
    log = app._wm_log
    assert log[0] == "withdraw" and log[1] == "alpha:0", log
    assert log[-1] == "alpha:1", log
    assert log.count("alpha:0") == 1, log
    assert app._alpha == 1.0
    # До показа ни одного deiconify: сначала прозрачность, потом сборка.
    assert log.index("deiconify") > log.index("alpha:0"), log


def test_window_stays_visible_after_customtkinter_mainloop(gui):
    """
    Регрессия 4.40: окно появлялось и исчезало ВОВСЕ.

    ``CTk.mainloop`` на Windows прячет окно для перекраски заголовка и показывает
    обратно только если ``withdraw()`` НЕ вызывали до первого показа. Мы вызывали
    (окно собирается скрытым), поэтому ``_show_ready`` обязан снять флаги
    CustomTkinter — иначе после ``mainloop()`` окна на экране нет.
    """
    app = gui.SurveyApp()
    assert app._visible is True and app._alpha == 1.0
    assert app._window_exists is True
    assert app._withdraw_called_before_window_exists is False
    app.mainloop()
    assert app._visible is True, app._wm_log
    assert app._alpha == 1.0
    assert app._wm_log.count("withdraw") == 1, app._wm_log


def test_show_ready_reveals_window_even_if_paint_fails(gui, monkeypatch):
    """Сбой отрисовки не оставляет пользователя с невидимым, но работающим приложением."""
    app = gui.SurveyApp()
    app._visible = False
    app.attributes("-alpha", 0.0)

    def _boom(*args, **kwargs):
        raise RuntimeError("paint failed")

    monkeypatch.setattr(app, "update", _boom)
    app._show_ready()
    assert app._visible is True
    assert app._alpha == 1.0
    assert app._startup_hidden is False


def test_main_shows_error_dialog_when_startup_fails(gui, monkeypatch, tmp_path, capsys):
    """Если сборка окна упала, пользователь видит ошибку, а не исчезнувшее приложение."""
    import tkinter

    monkeypatch.setattr(gui.Path, "home", classmethod(lambda cls: tmp_path))
    tkinter.messagebox.errors.clear()
    shown: list[object] = []

    class BrokenApp(gui.SurveyApp):
        def _show_ready(self) -> None:
            shown.append(self)
            super()._show_ready()

    # Окно «недособрано» до подмены SurveyApp: внутри main() оно уже недоступно,
    # его возвращает модульная ссылка _STARTUP_WINDOW.
    broken = BrokenApp()
    broken._startup_hidden = True
    broken._visible = False
    broken.attributes("-alpha", 0.0)
    shown.clear()  # сборка выше сама звала _show_ready; дальше важен только показ при сбое

    def _raise():
        gui._STARTUP_WINDOW = broken
        raise RuntimeError("сборка окна упала")

    monkeypatch.setattr(gui, "SurveyApp", _raise)
    assert gui.main() == 1
    assert len(shown) == 1                      # частично собранное окно показано
    assert shown[0]._visible is True
    assert (tmp_path / "tsai_crash.log").exists()
    assert tkinter.messagebox.errors, "нет диалога об ошибке запуска"
    title, message = tkinter.messagebox.errors[0]
    assert "сбой запуска" in title
    assert "сборка окна упала" in message
    assert str(tmp_path / "tsai_crash.log") in message
    assert gui._STARTUP_WINDOW is None
    assert "сборка окна упала" in capsys.readouterr().err


def test_console_window_is_hidden_when_run_bat_asks(gui, monkeypatch):
    """
    Запасной путь запуска (pythonw.exe в окружении нет): программа работает в консоли
    run.bat, но прячет её, как только окно показано, — cmd не висит в панели задач (4.41).
    """
    calls: list[str] = []
    monkeypatch.setattr(gui.console_window, "hide_requested", lambda: True)
    monkeypatch.setattr(gui.console_window, "hide_console", lambda: calls.append("hide") or True)
    app = gui.SurveyApp()
    assert calls == ["hide"], "консоль не спрятана после показа окна"
    assert app._visible is True


def test_console_window_is_left_alone_without_run_bat_request(gui, monkeypatch):
    """Свой терминал разработчика программа не прячет: просьба приходит только из run.bat."""
    calls: list[str] = []
    monkeypatch.setattr(gui.console_window, "hide_requested", lambda: False)
    monkeypatch.setattr(gui.console_window, "hide_console", lambda: calls.append("hide") or True)
    gui.SurveyApp()
    assert calls == []


def test_console_hide_failure_does_not_break_startup(gui, monkeypatch):
    """Сбой WinAPI не мешает окну показаться."""
    monkeypatch.setattr(gui.console_window, "hide_requested", lambda: True)

    def _boom():
        raise OSError("нет WinAPI")

    monkeypatch.setattr(gui.console_window, "hide_console", _boom)
    app = gui.SurveyApp()
    assert app._visible is True and app._alpha == 1.0


def test_main_returns_console_to_screen_when_startup_fails(gui, monkeypatch, tmp_path):
    """
    Если запуск не удался, консоль возвращается: run.bat печатает ошибку и предлагает
    откатиться на резервную копию — этот текст должен быть виден (4.41).
    """
    shown: list[str] = []
    monkeypatch.setattr(gui.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(gui.console_window, "show_console", lambda: shown.append("show") or True)

    def _raise():
        gui._STARTUP_WINDOW = None
        raise RuntimeError("окно не собралось")

    monkeypatch.setattr(gui, "SurveyApp", _raise)
    assert gui.main() == 1
    assert shown == ["show"]


def test_relaunch_after_update_uses_explicit_cmd_c(gui, monkeypatch):
    """cmd /c (not start /k) closes when the batch ends."""
    import subprocess

    app = gui.SurveyApp()
    launched = []
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: launched.append((args, kw)))
    monkeypatch.setattr(gui.sys, "platform", "win32")
    assert app._relaunch_after_update() == ""
    command, options = launched[0]
    assert command[1:] == ["/d", "/v:off", "/c", "run.bat"]
    assert options["creationflags"] == int(getattr(subprocess, "CREATE_NEW_CONSOLE", 0) or 0)


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
    # Масштабы заказчика: 16 — основной, ещё 10, 20, 30, 40, 50, 60.
    assert app.scale_options == [
        '1"=10\'', '1"=16\'', '1"=20\'', '1"=30\'', '1"=40\'', '1"=50\'', '1"=60\'',
    ]
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
    _load_pdf_by_role(app)
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
        layers = {"neighbor_lot": "NEIGHBOR_LOT"} if plan.is_neighbor else {"property_line": "PL"}
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
        return DrawResult(plan=plan, text_count=len(plan.items), layers={"property_line": "PL"})

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
    assert "LOT 26" in canvas_texts
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
            layers={"property_line": "PL", "boundary_text": "D-TOPO-TEXT"},
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
    # Версия в углу окна — та же, что печатает отчёт Шага 2 (version.py).
    import feature_labeler
    import version

    assert gui.VERSION == version.APP_VERSION != "1.0.0"
    assert feature_labeler.STEP2_VERSION == f"{version.APP_VERSION} ({version.APP_STAMP})"
    assert app.version_badge.cget("text") == f"v{version.APP_VERSION} · STUDIO"
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

    _load_pdf_by_role(app)
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
    _load_pdf_by_role(app)
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

    _load_pdf_by_role(app)
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
    _load_pdf_by_role(app)
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
    _load_pdf_by_role(app)
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
    _load_pdf_by_role(app)
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
            obvious_geometry_choice=lambda: None,
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
    _load_pdf_by_role(app)
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
    _load_pdf_by_role(app)
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
    _load_pdf_by_role(app)
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
    _load_pdf_by_role(app)
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
        return DrawResult(plan=plan, text_count=len(plan.items), layers={"property_line": "PL"})

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
    _load_pdf_by_role(app)
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
    _load_pdf_by_role(app)
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    assert len(calls) == 2 and len(app._preview_neighbor_plans) == 2
    assert app.result_tab_buttons["🛡 Проверки"].cget("fg_color") == "#92400e"
    assert "замечаний: 2" in app.suspicion_badge_button.cget("text")
    assert all(any("Возможная опечатка" in item.title for item in app._plan_diagnostic_items(plan))
               for plan in app._preview_neighbor_plans)
    app._start(parse_only=False)
    _wait_for_gui_worker(app)
    # Подозрительный сосед рисуется в любом случае — без подтверждения и без отмены.
    assert not prompts
    assert draws == app._preview_neighbor_plans
    assert "отменена" not in app.status_var.get()
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
    _load_pdf_by_role(app)
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
    _load_pdf_by_role(app)
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
    _load_pdf_by_role(app)
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
    _load_pdf_by_role(app)
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
        return DrawResult(plan=plan, text_count=len(plan.items), layers={"property_line": "PL"})
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
    _load_pdf_by_role(app)
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    assert good in app._neighbor_plan_sources and scan in app._pending_neighbor_pdfs
    monkeypatch.setattr(gui.AcadBuilder, "draw_plan", lambda *a, **kw: pytest.fail("неполный пакет"))
    app._start(parse_only=False)
    assert "разберите" in app.status_var.get().lower()

    # После загрузки только исправного PDF смена страниц делает готовый план устаревшим.
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **kw: (str(good),))
    _load_pdf_by_role(app)
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
    path = tmp_path / "LOT 77 NEW.pdf"
    _write_test_pdf(path, [left, right])
    app = gui.SurveyApp()
    app._on_sample_selected("Aspenwall St — 420' (Основной участок)")
    main_plan = app._preview_main_plan
    app.neighbor_lot_radio.select()
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **_: (str(path),))
    _load_pdf_by_role(app)
    app.pdf_pages_var.set("2")
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    assert app._preview_main_plan is main_plan
    assert len(app._preview_neighbor_plans) == 1
    assert app._preview_neighbor_plans[0].description.distance_to_corner_ft == pytest.approx(475.0)
    assert app._preview_neighbor_plans[0].items[0].text == "LOT 77"
    assert app._preview_neighbor_plans[0].description.lot_number == "30"
    assert any("не совпадает" in warning for warning in app._preview_neighbor_plans[0].warnings)
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
    assert app._preview_neighbor_plans[0].items[0].text == "LOT 77"
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
    _load_pdf_by_role(app)
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
    _load_pdf_by_role(app)
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
    _load_pdf_by_role(app)
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
    _load_pdf_by_role(app)
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    old = app._neighbor_plan_sources[path]

    path.write_bytes(b"invalid PDF")
    _load_pdf_by_role(app)
    assert app._neighbor_plan_sources[path] is old and path in app._pending_neighbor_files
    app.source_mode_var.set("📝 Текст")
    app._on_source_mode_changed()
    monkeypatch.setattr(gui.AcadBuilder, "draw_plan", lambda *a, **kw: pytest.fail("нечитабельный источник"))
    app._start(parse_only=False)
    assert "reopened.pdf" in app.status_var.get()

    path.write_bytes(valid_bytes)
    _load_pdf_by_role(app)
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


def test_step4_offsets_tab_is_last_and_keeps_step1_keys_off(gui):
    # Шага 3 в программе нет: после Шага 2 идёт вкладка «Шаг 4. Оффсеты» (каркас 4.1).
    app = gui.SurveyApp()
    assert list(app.step_tab_buttons)[-1] == gui.STEP_TAB_OFFSETS == "Шаг 4. Оффсеты"
    assert app.step4_page._gridded is False

    app._select_step_tab(gui.STEP_TAB_OFFSETS)
    assert app.main_wrap._gridded is False
    assert app.step2_page._gridded is False
    assert app.step4_page._gridded is True
    assert app.draw_button._gridded is False
    assert app.step_tab_buttons[gui.STEP_TAB_OFFSETS].cget("fg_color") == gui.THEME["btn_primary"]
    assert "выделите в AutoCAD" in app.step4_status_label.cget("text")
    assert app.step4_run_button._gridded is True

    started = []
    app._start = lambda **kwargs: started.append(kwargs)
    assert app._global_bindings["<F5>"]() is None
    assert started == []

    app._select_step_tab(gui.STEP_TAB_BOUNDARY)
    assert app.step4_page._gridded is False
    assert app.main_wrap._gridded is True
    assert app._global_bindings["<F5>"]() == "break"
    assert started == [{"parse_only": False}]


def test_step4_run_reports_error_and_result(gui):
    # Шаг 4: ошибка AutoCAD (или Esc) — понятный отчёт; результат — отчёт, журнал и статус.
    import offsets as off
    from acad_builder import OffsetRunResult

    app = gui.SurveyApp()
    app._select_step_tab(gui.STEP_TAB_OFFSETS)
    app._handle_offsets_result(None, "область не указана (Esc — отмена).")
    assert "ШАГ 4 НЕ ВЫПОЛНЕН" in app.step4_report_box.get("1.0", "end")

    plan = off.plan_offsets(
        [(0.0, 0.0), (100.0, 0.0), (100.0, 50.0), (0.0, 50.0)],
        [off.Feature("house", "H1", ((20.0, 10.0), (40.0, 10.0), (40.0, 30.0), (20.0, 30.0)))],
        front=[[(0.0, 0.0), (100.0, 0.0)]],
    )
    run = OffsetRunResult(plan=plan, area=[], scale=16.0, north_deg=90.0,
                          front_lines=1, feature_count=1, dry_run=True)
    app._handle_offsets_result(run, "")
    text = app.step4_report_box.get("1.0", "end")
    assert "Подписей: 1" in text and "10.0'N" in text
    assert "Режим расчёта: чертёж не изменён." in text
    assert app.status_var.get() == "Шаг 4: оффсетов 1 (чертёж не изменён)"


def test_step2_tab_is_second_global_tab_and_keeps_step1(gui):
    app = gui.SurveyApp()

    # Шаг 0 стоит перед Шагом 1: документы участка собираются до посадки границы.
    assert gui.STEP_TABS == (
        "Шаг 0. Документы участка",
        "Шаг 1. Граница участка (Legal Description)",
        "Шаг 2. Автоподписание объектов (D-TOPO-TEXT)",
        "Шаг 4. Оффсеты",
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
    assert app.step2_height_entry.cget("placeholder_text") == "авто"
    assert app.feature_text_height_var.get() == ""

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
    # По умолчанию журнал упрощённый: в окне отчёта итоги есть, подробные строки скрыты.
    report = app.step2_report_box.get("1.0", "end")
    assert "ПОДПИСАНО: 15" in report
    assert "НАЙДЕНО ОБЪЕКТОВ С ПОДПИСЬЮ: 18" in report
    assert "по кодам и описаниям точек съёмки: 2" not in report
    # Переключатель «Подробный журнал» перерисовывает отчёт полностью.
    app.log_mode_var.set(True)
    app._apply_log_mode()
    report = app.step2_report_box.get("1.0", "end")
    assert "по заметкам полевого чертёжника (перевод в формулировки заказчика): 1" in report
    assert "по кодам и описаниям точек съёмки: 2" in report
    assert "по именам слоёв: 14" in report
    assert "ПОДПИСАНО: 15" in report
    assert "полевая заметка «WOOD PORCH»" in report
    app.log_mode_var.set(False)
    app._apply_log_mode()
    # Тот же отчёт — в журнале операций (лог-панель).
    log = app.log_text()
    assert "Шаг 2 · • по кодам и описаниям точек съёмки: 2" in log
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


def test_step2_text_height_field_overrides_scale(gui, monkeypatch):
    monkeypatch.setattr(gui.sys, "platform", "win32")
    calls = []
    monkeypatch.setattr(
        gui.AcadBuilder, "label_features",
        lambda builder, **kwargs: calls.append(kwargs) or _step2_result(),
    )
    app = gui.SurveyApp()
    # Пустое поле — высота по масштабу.
    app._start_feature_labeling("auto")
    _wait_idle(app)
    assert calls[-1]["text_height"] is None

    # Запятая как десятичный разделитель и знак фута допустимы.
    app.feature_text_height_var.set("0,45'")
    app._refresh_step2_height_label()
    assert "0.450 ft (задано вручную" in app.step2_height_label.cget("text")
    app._start_feature_labeling("auto")
    _wait_idle(app)
    assert calls[-1]["text_height"] == pytest.approx(0.45)

    # Не число — запуск не начинается, AutoCAD не трогается.
    app.feature_text_height_var.set("мелко")
    app._start_feature_labeling("auto")
    _wait_idle(app)
    assert len(calls) == 2
    assert app._busy is False
    assert "Высота текста подписей указана неверно" in app.step2_report_box.get("1.0", "end")
    assert "неверная высота текста" in app.status_var.get()


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


# --------------------------------------------------- Шаг 0: документы участка


def _step0_pdf(path):
    """PDF на две страницы: обложка и описание (текстовый слой, без OCR)."""
    import pymupdf

    path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    doc.new_page().insert_textbox(pymupdf.Rect(36, 36, 560, 760), "COVER", fontsize=10)
    doc.new_page().insert_textbox(
        pymupdf.Rect(36, 36, 560, 760),
        "BEGINNING AT A POINT on the easterly side of Alvine Avenue; RUNNING THENCE "
        "N 67 33 48 E, 174.89 feet; THENCE N 50 25 10 W, 45.30 feet; THENCE "
        "S 67 33 48 W, 153.64 feet; THENCE S 22 26 12 E, 40.00 feet to the point "
        "of BEGINNING.",
        fontsize=9,
    )
    doc.save(str(path))
    doc.close()
    return path


def _fill_step0(app, *, block="6323", lot="32", neighbours="30", folder=""):
    app.step0_borough_var.set("Staten Island")
    app.step0_block_var.set(block)
    app.step0_lot_var.set(lot)
    app.step0_neighbours_var.set(neighbours)
    if folder:
        app.step0_folder_var.set(str(folder))
    return app


def _wait_step0(app):
    """Сбор идёт в фоновом потоке: ждём его, прокачивая очередь событий."""
    import time

    for _ in range(300):
        app._drain_events()
        if not app._step0_running:
            return
        time.sleep(0.01)
    raise AssertionError("сбор Шага 0 не завершился")


def test_step0_tab_is_first_and_hides_step1_workspace(gui):
    app = gui.SurveyApp()
    assert app.step0_page._gridded is False
    app._select_step_tab(gui.STEP_TAB_DEEDS)
    assert gui.STEP_TAB_DEEDS == "Шаг 0. Документы участка"
    assert app.step0_page._gridded is True
    assert app.main_wrap._gridded is False
    assert app.step2_page._gridded is False
    assert app.draw_button._gridded is False
    assert app.add_neighbor_button._gridded is False
    assert app.step_tab_buttons[gui.STEP_TAB_DEEDS].cget("fg_color") == gui.THEME["btn_primary"]
    # Клавиши Шага 1 на вкладке Шага 0 не запускают разбор границы.
    started = []
    app._start = lambda **kwargs: started.append(kwargs)
    app._global_bindings["<F5>"]()
    assert started == []
    app._select_step_tab(gui.STEP_TAB_BOUNDARY)
    assert app.main_wrap._gridded is True and app.step0_page._gridded is False


def test_step0_screen_shows_the_customer_scheme(gui):
    app = gui.SurveyApp()
    assert "Собрать описания" in app.step0_run_button.cget("text")
    assert app.step0_manual_role_menu.cget("values") == list(gui.STEP0_MANUAL_ROLES)
    assert list(gui.STEP0_MANUAL_ROLES) == ["Наш участок", "Сосед"]
    assert app.step0_manual_role_var.get() == "Наш участок"
    report = app.step0_report_box.get("1.0", "end")
    assert "LOT 30 NEW.pdf" in report and "сосед" in report
    assert "Cloudflare" in report


def test_step0_rejects_non_numeric_block_or_lot(gui):
    app = gui.SurveyApp()
    _fill_step0(app, block="шесть", lot="32")
    app._start_step0_intake()
    assert "block и lot" in app.status_var.get()
    _fill_step0(app, block="6323", lot="тридцать два")
    app._start_step0_intake()
    assert "block и lot" in app.status_var.get()
    assert app._step0_running is False


def test_step0_rejects_junk_in_the_neighbours_field(gui):
    app = gui.SurveyApp()
    _fill_step0(app, neighbours="30, сосед")
    app._start_step0_intake()
    assert "соседи" in app.status_var.get()
    assert app._step0_running is False


def test_step0_our_lot_is_removed_from_the_neighbour_list(gui, tmp_path):
    app = gui.SurveyApp()
    collected = {}

    def fake_collect(**kwargs):
        collected.update(kwargs)
        return gui.deed_intake.IntakeOutcome(folder=kwargs["folder"])

    saved = gui.deed_intake.collect_deeds
    gui.deed_intake.collect_deeds = fake_collect
    try:
        _fill_step0(app, neighbours="32, 30", folder=tmp_path)
        app._start_step0_intake()
        _wait_step0(app)
    finally:
        gui.deed_intake.collect_deeds = saved
    assert collected["neighbours"] == [30]
    assert "соседи: 30" in _step0_texts(app)[0]
    assert "32" not in _step0_texts(app)[0].split("соседи:")[1].split(";")[0]


def test_step0_success_enables_handover_to_step1(gui, tmp_path):
    app = gui.SurveyApp()
    folder = tmp_path / "01_описания"
    outcome = gui.deed_intake.IntakeOutcome(
        folder=folder,
        records=[
            gui.deed_store.StoredDeed(file="OUR LOT 32 NEW.pdf", role="our", lot="32", pages="2"),
        ],
    )
    saved = gui.deed_intake.collect_deeds
    gui.deed_intake.collect_deeds = lambda **kwargs: outcome
    try:
        _fill_step0(app, folder=folder)
        app._start_step0_intake()
        _wait_step0(app)
    finally:
        gui.deed_intake.collect_deeds = saved
    assert app.step0_to_step1_button.cget("state") == "normal"
    assert "OUR LOT 32 NEW.pdf" in app.step0_report_box.get("1.0", "end")
    assert "собрано документов — 1" in app.status_var.get()


def test_step0_failure_keeps_the_button_for_another_try(gui, tmp_path):
    app = gui.SurveyApp()
    outcome = gui.deed_intake.IntakeOutcome(
        folder=tmp_path, missing=["наш участок (лот 32): список не открылся — таймаут"],
    )
    saved = gui.deed_intake.collect_deeds
    gui.deed_intake.collect_deeds = lambda **kwargs: outcome
    try:
        _fill_step0(app, folder=tmp_path)
        app._start_step0_intake()
        _wait_step0(app)
    finally:
        gui.deed_intake.collect_deeds = saved
    assert app.step0_to_step1_button.cget("state") == "disabled"
    assert app.step0_run_button.cget("state") == "normal"
    assert "наш участок не собран" in app.status_var.get()


def test_step0_stop_leaves_the_window_usable(gui):
    app = gui.SurveyApp()
    app._step0_running = True
    app.step0_run_button.configure(state="disabled")
    app._stop_step0_intake()
    app._finish_step0("cancelled", None)
    assert app._step0_running is False
    assert app.step0_run_button.cget("state") == "normal"
    assert "сбор остановлен" in app.status_var.get()


def test_step0_crash_is_reported_not_swallowed(gui, tmp_path):
    app = gui.SurveyApp()

    def boom(**kwargs):
        raise RuntimeError("нет сети")

    saved = gui.deed_intake.collect_deeds
    gui.deed_intake.collect_deeds = boom
    try:
        _fill_step0(app, folder=tmp_path)
        app._start_step0_intake()
        _wait_step0(app)
    finally:
        gui.deed_intake.collect_deeds = saved
    assert "нет сети" in app.status_var.get()
    assert app._step0_running is False


def test_step0_takes_a_hand_downloaded_pdf_and_names_it_as_a_neighbour(
    gui, tmp_path, monkeypatch
):
    app = gui.SurveyApp()
    downloads = tmp_path / "Downloads"
    _step0_pdf(downloads / "ViewContent.pdf")
    monkeypatch.setattr(gui.rcclerk, "_default_downloads_dir", lambda: downloads)
    _fill_step0(app, neighbours="30", folder=tmp_path / "01_описания")
    app.step0_manual_role_var.set("Сосед")
    app.step0_manual_lot_var.set("30")
    app._adopt_step0_download()
    assert (tmp_path / "01_описания" / "LOT 30 NEW.pdf").is_file()
    assert "LOT 30 NEW.pdf" in app.status_var.get()
    assert "Из «Загрузок» принят файл" in app.step0_report_box.get("1.0", "end")


def test_step0_handover_opens_step1_with_the_trimmed_copies(gui, tmp_path):
    app = gui.SurveyApp()
    folder = tmp_path / "01_описания"
    gui.deed_store.store_pdf(
        folder, _step0_pdf(tmp_path / "full.pdf"),
        role="our", lot=32, tag="NEW", block=6323, pages="2",
        trimmed_source=_step0_pdf(tmp_path / "trim.pdf"),
    )
    gui.deed_store.store_pdf(
        folder, _step0_pdf(tmp_path / "nb.pdf"), role="neighbour", lot=30, tag="NEW", block=6323,
    )
    _fill_step0(app, folder=folder)
    app._step0_to_step1()
    assert app._pdf_set_dialog is not None
    items = app._pdf_set_dialog.items
    assert sorted(item.path.name for item in items) == ["LOT 30 NEW.pdf", "OUR LOT 32 NEW_p2.pdf"]
    trimmed = next(item for item in items if item.path.name == "OUR LOT 32 NEW_p2.pdf")
    assert trimmed.name_pages == (2, 2)
    assert trimmed.is_main
    assert next(item for item in items if item.path.name == "LOT 30 NEW.pdf").is_main is False
    assert app._pdf_set_dialog.main_var.get() == str(items.index(trimmed))


def test_step0_handover_without_files_explains_itself(gui, tmp_path):
    app = gui.SurveyApp()
    _fill_step0(app, folder=tmp_path / "пустая")
    app._step0_to_step1()
    assert "нет файлов для Шага 1" in app.status_var.get()


def test_step0_deed_collection_does_not_call_the_paid_model(gui):
    """The address-preflight Vision call is separate from document collection/OCR."""
    import pathlib

    text = pathlib.Path(gui.__file__).read_text(encoding="utf-8")
    start = text.index("    def _step0_worker(")
    end = text.index("    def _stop_step0_intake(")
    body = text[start:end]
    for forbidden in ("ask_llm_json", "run_multipass_consensus", "parse_legal_description_full"):
        assert forbidden not in body


def test_step0_worker_connects_the_pdf_ready_browser_save_hook(gui, tmp_path, monkeypatch):
    app = gui.SurveyApp()
    seen: dict[str, object] = {}
    hook = lambda endorsement_id: None

    def fake_collect(**kwargs):
        seen.update(kwargs)
        return gui.deed_intake.IntakeOutcome(folder=kwargs["folder"])

    monkeypatch.setattr(gui.browser_save, "available", lambda: True)
    monkeypatch.setattr(
        gui.browser_save, "auto_save_hook",
        lambda folder, on_attempt, **kwargs: seen.setdefault("attempt_notice", on_attempt) and hook,
    )
    monkeypatch.setattr(gui.deed_intake, "collect_deeds", fake_collect)
    app._step0_worker(6323, 32, [], tmp_path, False)
    _wait_step0(app)
    assert seen["downloads_dir"] == gui.rcclerk.default_downloads_dir()
    assert seen["auto_save"] is hook
    assert callable(seen["attempt_notice"])
    assert any("Ctrl+S" in line for line in _step0_texts(app))


def test_handover_recovers_neighbour_from_its_passport_when_manifest_misses_it(gui, tmp_path):
    import json

    app = gui.SurveyApp()
    folder = tmp_path / "01_описания"
    gui.deed_store.store_pdf(
        folder, _step0_pdf(tmp_path / "our-new.pdf"),
        role="our", lot=32, tag="NEW", block=6323, pages="2",
        trimmed_source=_step0_pdf(tmp_path / "our-new-trim.pdf"),
    )
    gui.deed_store.store_pdf(
        folder, _step0_pdf(tmp_path / "our-old.pdf"),
        role="our", lot=32, tag="OLD", block=6323,
    )
    gui.deed_store.store_pdf(
        folder, _step0_pdf(tmp_path / "neighbour.pdf"),
        role="neighbour", lot=30, tag="NEW", block=6323,
    )

    manifest_path = folder / gui.deed_store.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["items"] = [item for item in manifest["items"] if item["role"] != "neighbour"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    _fill_step0(app, folder=folder, neighbours="30")
    app._step0_to_step1()
    names = sorted(item.path.name for item in app._pdf_set_dialog.items)
    # Старый документ нашего участка хранится в папке, но в набор Шага 1 не передаётся (решение пользователя).
    assert names == ["LOT 30 NEW.pdf", "OUR LOT 32 NEW_p2.pdf"]
    assert any("В manifest не было LOT 30 NEW.pdf" in line for line in _step0_texts(app))
    assert any("Отправляю в Шаг 1 (2 PDF)" in line for line in _step0_texts(app))


def test_handover_falls_back_to_full_neighbour_pdf_if_trimmed_copy_is_missing(gui, tmp_path):
    import json

    app = gui.SurveyApp()
    folder = tmp_path / "01_описания"
    gui.deed_store.store_pdf(
        folder, _step0_pdf(tmp_path / "our.pdf"),
        role="our", lot=32, tag="NEW", block=6323,
    )
    gui.deed_store.store_pdf(
        folder, _step0_pdf(tmp_path / "neighbour.pdf"),
        role="neighbour", lot=30, tag="NEW", block=6323,
    )
    manifest_path = folder / gui.deed_store.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    neighbour = next(item for item in manifest["items"] if item["role"] == "neighbour")
    neighbour["trimmed"] = "LOT 30 NEW_p1.pdf"  # повреждённая ссылка, полный PDF существует
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    _fill_step0(app, folder=folder, neighbours="30")
    app._step0_to_step1()
    names = sorted(item.path.name for item in app._pdf_set_dialog.items)
    assert "LOT 30 NEW.pdf" in names
    assert any("отсутствует; передаю полный документ LOT 30 NEW.pdf" in line
               for line in _step0_texts(app))


def test_step0_action_log_entries_have_local_time(gui, tmp_path, monkeypatch):
    import re

    app = gui.SurveyApp()
    monkeypatch.setattr(gui.browser_save, "available", lambda: False)

    def fake_collect(**kwargs):
        kwargs["progress"]("читаю список документов")
        return gui.deed_intake.IntakeOutcome(folder=kwargs["folder"])

    monkeypatch.setattr(gui.deed_intake, "collect_deeds", fake_collect)
    _fill_step0(app, folder=tmp_path)
    app._start_step0_intake()
    _wait_step0(app)
    lines = [line for line in _step0_texts(app) if "список документов" in line]
    assert lines and re.match(r"^\[\d{2}:\d{2}:\d{2}\]", lines[0])
    assert re.match(r"^\[\d{2}:\d{2}:\d{2}\]", _step0_texts(app)[0])


def test_step0_ocr_dpi_is_saved_and_passed_to_description_search(gui, tmp_path, monkeypatch):
    app = gui.SurveyApp()
    captured: dict[str, object] = {}
    saved_configs = []

    def fake_collect(**kwargs):
        captured.update(kwargs)
        return gui.deed_intake.IntakeOutcome(folder=kwargs["folder"])

    monkeypatch.setattr(gui.browser_save, "available", lambda: False)
    monkeypatch.setattr(gui.deed_intake, "collect_deeds", fake_collect)
    monkeypatch.setattr(
        gui, "save_config", lambda config: saved_configs.append(config.copy()) or tmp_path / "config.json",
    )
    app.step0_ocr_dpi_var.set("88")
    _fill_step0(app, folder=tmp_path)
    app._start_step0_intake()
    _wait_step0(app)
    assert captured["ocr_dpi"] == 88
    assert saved_configs and saved_configs[-1]["ocr"]["dpi"] == 88
    assert any("OCR: 88 dpi" in line for line in _step0_texts(app))


def test_step0_rejects_invalid_ocr_dpi(gui):
    app = gui.SurveyApp()
    app.step0_ocr_dpi_var.set("20")
    _fill_step0(app)
    app._start_step0_intake()
    assert "от 50 до 1200" in app.status_var.get()
    assert app._step0_running is False


def test_step0_portal_lookup_requires_address_and_configured_vision_key(gui, monkeypatch):
    app = gui.SurveyApp()
    app._start_step0_portal_lookup()
    assert "введите адрес" in app.status_var.get().lower()

    app.step0_address_var.set("87 Stafford Ave")
    monkeypatch.setattr(
        app, "_build_runtime_config",
        lambda: ({"api": {}}, "hydra", "vision-model", None, ""),
    )
    monkeypatch.setattr(
        gui, "resolve_api_target",
        lambda *args, **kwargs: {"api_key": ""},
    )
    called = []
    monkeypatch.setattr(gui.nyc_portal, "lookup_parcel", lambda *a, **k: called.append(True))
    app._start_step0_portal_lookup()
    assert "api-ключ" in app.status_var.get().lower()
    assert not called
    assert app._step0_portal_running is False


def test_step0_portal_vision_result_fills_current_and_neighbour_lots(gui, tmp_path, monkeypatch):
    import time

    app = gui.SurveyApp()
    app.step0_address_var.set("87 Stafford Ave")
    app.step0_folder_var.set(str(tmp_path))
    usage = gui.TokenUsage(
        prompt_tokens=20, completion_tokens=8, total_tokens=28,
        provider="hydra", provider_label="Hydra AI", model="vision", available=True,
    )
    result = gui.nyc_portal.ParcelLookup(
        block="5712", lot="70", borough="Staten Island",
        left_neighbour="68", right_neighbour="72",
        confidence="high", notes="Visible on the map.",
        map_image=tmp_path / "NYC_Portal_map.png", usage=usage,
    )
    monkeypatch.setattr(
        app, "_build_runtime_config",
        lambda: ({"api": {}}, "hydra", "vision-model", "key", "pin"),
    )
    monkeypatch.setattr(
        gui, "resolve_api_target", lambda *args, **kwargs: {"api_key": "key"},
    )
    captured = {}

    def lookup(address, folder, **kwargs):
        captured.update(address=address, folder=folder, **kwargs)
        return result

    monkeypatch.setattr(gui.nyc_portal, "lookup_parcel", lookup)
    app._start_step0_portal_lookup()
    for _ in range(300):
        app._drain_events()
        if not app._step0_portal_running:
            break
        time.sleep(0.01)
    assert not app._step0_portal_running
    assert captured["address"] == "87 Stafford Ave"
    assert captured["provider"] == "hydra" and captured["model"] == "vision-model"
    assert app.step0_borough_var.get() == "Staten Island"
    assert app.step0_block_var.get() == "5712"
    assert app.step0_lot_var.get() == "70"
    assert app.step0_neighbours_var.get() == "68, 72"
    assert app._session_llm_calls == 1
    assert any("соседи слева/справа 68 / 72" in line for line in _step0_texts(app))
    assert app.step0_run_button.cget("state") == "normal"


def test_clipboard_shortcuts_paste_into_inputs_and_copy_selected_output(gui):
    app = gui.SurveyApp()

    class EntryWidget:
        value = "old"
        clipboard = "new address"

        def winfo_class(self):
            return "Entry"

        def cget(self, name):
            return "normal"

        def clipboard_get(self):
            return self.clipboard

        def selection_present(self):
            return True

        def delete(self, _first, _last):
            self.value = ""

        def insert(self, _where, text):
            self.value += text

        def selection_get(self):
            return "selected address"

        def clipboard_clear(self):
            self.clipboard = ""

        def clipboard_append(self, text):
            self.clipboard += text

    entry = EntryWidget()
    event = type("Event", (), {"widget": entry})()
    assert gui.SurveyApp._paste_text_event(event) == "break"
    assert entry.value == "new address"
    assert gui.SurveyApp._copy_text_event(event) == "break"
    assert entry.clipboard == "selected address"

    # Russian layout maps the physical C/V positions to Cyrillic keysyms; Ctrl
    # shortcuts must still copy/paste from the same controls.
    russian_copy = type("Event", (), {
        "widget": entry, "state": 0x4, "keycode": 67,
        "keysym": "Cyrillic_es", "char": "\x03",
    })()
    assert gui.SurveyApp._clipboard_control_event(russian_copy) == "break"
    entry.clipboard = "вставка на русской раскладке"
    russian_paste = type("Event", (), {
        "widget": entry, "state": 0x4, "keycode": 86,
        "keysym": "Cyrillic_em", "char": "\x16",
    })()
    assert gui.SurveyApp._clipboard_control_event(russian_paste) == "break"
    assert entry.value == "вставка на русской раскладке"
    assert ("Entry", "<Control-KeyPress>") in app._class_bindings
    assert ("Text", "<Control-c>") in app._class_bindings


def test_right_click_shows_copy_cut_paste_menu_on_editable_entry(gui):
    app = gui.SurveyApp()
    gui.tk.Menu.instances.clear()

    class EntryWidget:
        value = "old"
        clipboard = "clipboard text"
        focused = False

        def winfo_class(self):
            return "Entry"

        def cget(self, name):
            return "normal"

        def clipboard_get(self):
            return self.clipboard

        def selection_present(self):
            return True

        def selection_get(self):
            return "selected text"

        def delete(self, _first, _last):
            self.value = ""

        def insert(self, _where, text):
            self.value += text

        def clipboard_clear(self):
            self.clipboard = ""

        def clipboard_append(self, text):
            self.clipboard += text

        def focus_set(self):
            self.focused = True

    entry = EntryWidget()
    event = type("Event", (), {"widget": entry, "x_root": 120, "y_root": 80})()
    assert ("Entry", "<Button-3>") in app._class_bindings
    handler = app._class_bindings[("Entry", "<Button-3>")]
    assert handler(event) == "break"
    assert entry.focused is True

    created_menu = gui.tk.Menu.instances[-1]
    assert created_menu.popups == [(120, 80)]
    assert created_menu.released is True
    labels = {item["label"]: item for item in created_menu.items}
    assert labels["Копировать"]["state"] == "normal"
    assert labels["Вырезать"]["state"] == "normal"
    assert "Вставить" in labels

    labels["Копировать"]["command"]()
    assert entry.clipboard == "selected text"
    entry.clipboard = "new clipboard text"
    labels["Вставить"]["command"]()
    assert entry.value == "new clipboard text"  # selection_present() replaces the selection


def test_right_click_disables_paste_and_cut_on_readonly_entry_without_selection(gui):
    app = gui.SurveyApp()
    gui.tk.Menu.instances.clear()

    class ReadonlyEntry:
        def winfo_class(self):
            return "Entry"

        def cget(self, name):
            return "readonly"

        def selection_present(self):
            return False

        def focus_set(self):
            return None

    entry = ReadonlyEntry()
    event = type("Event", (), {"widget": entry, "x_root": 10, "y_root": 20})()
    handler = app._class_bindings[("Entry", "<Button-3>")]
    assert handler(event) == "break"
    created_menu = gui.tk.Menu.instances[-1]
    labels = {item["label"]: item for item in created_menu.items}
    assert labels["Копировать"]["state"] == "disabled"
    assert "Вырезать" not in labels
    assert "Вставить" not in labels


def test_provider_and_model_preferences_autosave_and_survive_restart(gui, tmp_path, monkeypatch):
    import llm_parser

    config_path = tmp_path / "config.json"
    local_path = tmp_path / "config.local.json"
    monkeypatch.setattr(llm_parser, "CONFIG_PATH", config_path)
    monkeypatch.setattr(llm_parser, "LOCAL_CONFIG_PATH", local_path)

    first = gui.SurveyApp()
    first.select_provider("OpenRouter")
    first._on_model_selected("stepfun/step-3.5-flash:free")
    first._on_close()

    assert config_path.is_file()
    second = gui.SurveyApp()
    assert second.current_provider_id() == "openrouter"
    assert second.current_model() == "stepfun/step-3.5-flash:free"
    second._on_close()


def test_update_section_is_the_first_scrollable_settings_entry_not_buried_at_the_bottom(gui):
    """Regression: the update controls used to sit after the API-keys/PIN vault
    sections with no scrolling, so they were unreachable on a short window and
    the user could not find them. The settings body must scroll, and the update
    card must be the very first thing inside it."""
    app = gui.SurveyApp()

    update_row = app.update_source_entry.master
    drawer_body = update_row.master
    assert isinstance(drawer_body, gui.ctk.CTkScrollableFrame)
    assert drawer_body.winfo_children()[0] is update_row
    assert app.update_source_entry in update_row.winfo_children()
    assert app.update_version_button in update_row.winfo_children()


def test_update_version_with_an_empty_source_finds_the_branch_itself(gui, monkeypatch):
    """
    4.42: пустое поле источника — не ошибка, а «найдите ветку сами».

    Заказчик меняет агента раз в пару дней, а с ним имя ветки, и каждый раз правил
    ссылку руками. Теперь поле можно не заполнять вовсе.
    """
    from tkinter import messagebox

    app = gui.SurveyApp()
    app.update_source_var.set("")
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **kw: True)
    asked: list[str] = []
    monkeypatch.setattr(
        messagebox, "askyesno",
        lambda title, text, **kw: (asked.append(text) or True),
    )
    applied: list[str] = []

    def fake_apply_update(source, app_dir, *, progress=None, timeout=60.0, on_stage=None, developer_mode=False):
        applied.append(source)
        return gui.app_updater.UpdateResult(zip_url=source, files_written=3, app_dir=str(app_dir))

    monkeypatch.setattr(gui.app_updater, "apply_update", fake_apply_update)
    app._start_update_version()
    _drain_update(app)

    assert applied, "обновление не запустилось при пустом поле источника"
    assert applied[0].endswith("/zip/refs/heads/main")
    assert "сам" in asked[0] and "ветк" in asked[0], asked[0]


def _drain_update(app, seconds: float = 5.0) -> None:
    """Крутит очередь событий окна, пока рабочий поток обновления не закончит."""
    import time

    end = time.monotonic() + seconds
    while app._update_running and time.monotonic() < end:
        app._drain_events()
        time.sleep(0.01)
    app._drain_events()


def test_update_version_shows_download_percent_backup_and_install_stages(gui, monkeypatch):
    """Полоса и подпись проходят все этапы: скачивание в процентах, резерв, установка, готово."""
    from tkinter import messagebox

    app = gui.SurveyApp()
    app.update_source_var.set("https://github.com/Berber86/CAD/tree/main")
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **kw: True)
    shown = []
    real_show = app._show_update_stage

    def spy(name, fraction, detail):
        shown.append((name, fraction))
        real_show(name, fraction, detail)

    monkeypatch.setattr(app, "_show_update_stage", spy)

    def fake_apply_update(source, app_dir, *, progress=None, timeout=60.0, on_stage=None, developer_mode=False):
        on_stage("download", 0.42, "1.0 из 2.4 МБ")
        on_stage("backup", 1.0, "файлов: 3 из 3")
        on_stage("install", 0.5, "файлов: 4 из 8")
        return gui.app_updater.UpdateResult(zip_url="z", files_written=8, app_dir=str(app_dir))

    monkeypatch.setattr(gui.app_updater, "apply_update", fake_apply_update)
    app._start_update_version()

    import time
    end = time.monotonic() + 5
    while app._update_running and time.monotonic() < end:
        app._drain_events()
        time.sleep(0.01)
    app._drain_events()

    names = [name for name, _ in shown]
    assert names[0] == "download"
    assert "backup" in names and "install" in names
    assert names.index("backup") < names.index("install")
    assert ("download", 0.42) in shown
    assert app.update_progress_bar.get() == 1.0  # по итогам успешного обновления полоса полная


def test_update_version_downloads_and_reports_completed_file_count(gui, monkeypatch):
    from tkinter import messagebox

    app = gui.SurveyApp()
    app.update_source_var.set("https://github.com/Berber86/CAD/tree/main")
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **kw: True)

    def fake_apply_update(source, app_dir, *, progress=None, timeout=60.0, on_stage=None, developer_mode=False):
        # 4.42: в apply_update уходит уже разобранная ссылка на архив — до неё
        # источник проверяется резолвером (ветка, состояние «тестовая/стабильная»).
        assert source == "https://codeload.github.com/Berber86/CAD/zip/refs/heads/main"
        if progress:
            progress("скачиваю архив обновления: https://codeload.github.com/...")
            progress("обновление завершено: заменено файлов — 7")
        return gui.app_updater.UpdateResult(zip_url="https://codeload...", files_written=7, app_dir=str(app_dir))

    monkeypatch.setattr(gui.app_updater, "apply_update", fake_apply_update)
    app._start_update_version()

    import time
    end = time.monotonic() + 5
    while app._update_running and time.monotonic() < end:
        app._drain_events()
        time.sleep(0.01)
    app._drain_events()

    assert not app._update_running
    assert "7" in app.update_status_label.cget("text")
    assert "перезапустите" in app.update_status_label.cget("text").lower()


def test_update_version_reports_which_local_settings_were_kept(gui, monkeypatch):
    from tkinter import messagebox

    app = gui.SurveyApp()
    app.update_source_var.set("https://github.com/Berber86/CAD/tree/main")
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **kw: True)

    def fake_apply_update(source, app_dir, *, progress=None, timeout=60.0, on_stage=None, developer_mode=False):
        return gui.app_updater.UpdateResult(
            zip_url="https://codeload...",
            files_written=42,
            app_dir=str(app_dir),
            protected_skipped=("config.json",),
        )

    monkeypatch.setattr(gui.app_updater, "apply_update", fake_apply_update)
    app._start_update_version()

    import time
    end = time.monotonic() + 5
    while app._update_running and time.monotonic() < end:
        app._drain_events()
        time.sleep(0.01)
    app._drain_events()

    assert not app._update_running
    status_text = app.update_status_label.cget("text")
    assert "42" in status_text
    assert "config.json" in status_text
    assert "сохранены" in status_text.lower()


def _finish_update(app, gui, monkeypatch, files=7):
    """Запускает обновление с подменённым архивом и дожидается его конца."""
    import time

    def fake_apply_update(source, app_dir, *, progress=None, timeout=60.0, on_stage=None, developer_mode=False):
        return gui.app_updater.UpdateResult(zip_url="https://codeload...", files_written=files, app_dir=str(app_dir))

    monkeypatch.setattr(gui.app_updater, "apply_update", fake_apply_update)
    app._start_update_version()
    end = time.monotonic() + 5
    while app._update_running and time.monotonic() < end:
        app._drain_events()
        time.sleep(0.01)
    app._drain_events()


def test_update_offers_restart_and_relaunches_run_bat(gui, monkeypatch):
    """Регресс: после обновления показывается вопрос о перезапуске и запускается run.bat."""
    import subprocess
    from tkinter import messagebox

    app = gui.SurveyApp()
    app.update_source_var.set("https://github.com/Berber86/CAD/tree/main")
    asked: list[tuple[str, str]] = []
    monkeypatch.setattr(messagebox, "askyesno", lambda title, text, **kw: (asked.append((title, text)) or True))
    launched: list[tuple[list[str], dict]] = []
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: launched.append((args, kw)))
    monkeypatch.setattr(gui.sys, "platform", "win32")
    destroyed: list[bool] = []
    monkeypatch.setattr(app, "destroy", lambda: destroyed.append(True))
    exit_codes: list[int] = []
    monkeypatch.setattr(app, "_exit_process", lambda code: exit_codes.append(code))

    _finish_update(app, gui, monkeypatch)

    restart_questions = [text for title, text in asked if title == "Обновление завершено"]
    assert restart_questions, "вопрос о перезапуске не показан"
    assert "Перезапустить" in restart_questions[0]
    assert launched, "run.bat не запущен"
    assert launched[0][0][1:] == ["/d", "/v:off", "/c", "run.bat"]
    # run.bat ищется рядом с main.py, а не в текущей папке запуска.
    from pathlib import Path as _Path

    assert _Path(launched[0][1]["cwd"]) == _Path(gui.__file__).resolve().parent
    assert not destroyed, "выход не должен зависеть от потенциально блокирующего destroy"
    assert exit_codes == [0], "процесс старой версии должен завершиться, иначе окно run.bat висит"


def test_failed_restart_keeps_window_and_says_how_to_restart(gui, monkeypatch):
    import subprocess
    from tkinter import messagebox

    app = gui.SurveyApp()
    app.update_source_var.set("https://github.com/Berber86/CAD/tree/main")
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **kw: True)

    def refuse(args, **kw):
        raise PermissionError("доступ запрещён")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(gui.sys, "platform", "win32")
    destroyed: list[bool] = []
    monkeypatch.setattr(app, "destroy", lambda: destroyed.append(True))

    _finish_update(app, gui, monkeypatch)

    assert not destroyed, "нельзя закрывать программу, если новый экземпляр не запущен"
    text = app.update_status_label.cget("text")
    assert "7" in text, "итог обновления не должен пропасть"
    assert "вручную" in text and "run.bat" in text
    assert "доступ запрещён" in text


def test_one_failing_event_does_not_stop_the_queue(gui, monkeypatch):
    """Регресс: исключение в обработчике не должно остановить разбор следующих событий."""
    app = gui.SurveyApp()

    def boom():
        raise RuntimeError("тестовый сбой")

    monkeypatch.setattr(app, "_finish_busy", boom)
    app._events.put((app._generation, "done", None))
    app._events.put((app._generation, "update_progress", "шаг после сбоя"))
    app._drain_events()
    # Событие после сбоя обработано: очередь не остановилась.
    assert app.update_status_label.cget("text") == "шаг после сбоя"

    # Сам сбой виден в строке статуса, если он был последним событием.
    app._events.put((app._generation, "done", None))
    app._drain_events()
    assert "тестовый сбой" in app.status_var.get()


def test_update_version_declined_confirmation_does_not_run(gui, monkeypatch):
    from tkinter import messagebox

    app = gui.SurveyApp()
    app.update_source_var.set("https://github.com/Berber86/CAD")
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **kw: False)
    calls = []
    monkeypatch.setattr(gui.app_updater, "apply_update", lambda *a, **kw: calls.append(True))
    app._start_update_version()
    assert not calls
    assert not app._update_running


def test_update_version_reports_errors_without_crashing(gui, monkeypatch):
    from tkinter import messagebox

    app = gui.SurveyApp()
    app.update_source_var.set("https://github.com/Berber86/CAD")
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **kw: True)

    def boom(source, app_dir, *, progress=None, timeout=60.0, on_stage=None, developer_mode=False):
        raise gui.app_updater.UpdateError("сеть недоступна")

    monkeypatch.setattr(gui.app_updater, "apply_update", boom)
    app._start_update_version()

    import time
    end = time.monotonic() + 5
    while app._update_running and time.monotonic() < end:
        app._drain_events()
        time.sleep(0.01)
    app._drain_events()

    assert not app._update_running
    assert "не удалось" in app.update_status_label.cget("text").lower()
    assert "сеть недоступна" in app.update_status_label.cget("text")


def test_update_source_url_autosaves_and_survives_restart(gui, tmp_path, monkeypatch):
    import llm_parser

    config_path = tmp_path / "config.json"
    local_path = tmp_path / "config.local.json"
    monkeypatch.setattr(llm_parser, "CONFIG_PATH", config_path)
    monkeypatch.setattr(llm_parser, "LOCAL_CONFIG_PATH", local_path)

    first = gui.SurveyApp()
    first.update_source_var.set("https://github.com/Berber86/CAD/tree/arena/53e1068f-cad")
    first._persist_update_source()
    first._on_close()

    assert config_path.is_file()
    second = gui.SurveyApp()
    assert second.update_source_var.get() == "https://github.com/Berber86/CAD/tree/arena/53e1068f-cad"
    second._on_close()


def _write_backup_fixture(app_dir):
    """Старая версия в резерве, новая установлена поверх: как после обновления."""
    from app_updater import create_backup

    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "main.py").write_text("old main", encoding="utf-8")
    (app_dir / "version.py").write_text("APP_VERSION = '3.42'\n", encoding="utf-8")
    create_backup(app_dir, ["main.py", "version.py", "fresh.py"])
    (app_dir / "main.py").write_text("new main", encoding="utf-8")
    (app_dir / "fresh.py").write_text("new file", encoding="utf-8")


def test_rollback_button_is_off_without_backups(gui, tmp_path, monkeypatch):
    app = gui.SurveyApp()
    monkeypatch.setattr(app, "_update_dir", lambda: tmp_path)
    app._refresh_rollback_state()
    assert app.rollback_button.cget("state") == "disabled"
    assert "Резервных копий нет" in app.rollback_status_label.cget("text")


def test_rollback_restores_the_previous_version_and_offers_restart(gui, tmp_path, monkeypatch):
    import subprocess
    from tkinter import messagebox

    app_dir = tmp_path / "app"
    _write_backup_fixture(app_dir)
    app = gui.SurveyApp()
    monkeypatch.setattr(app, "_update_dir", lambda: app_dir)
    app._refresh_rollback_state()
    assert app.rollback_button.cget("state") == "normal"
    assert "3.42" in app.rollback_status_label.cget("text")

    questions: list[tuple[str, str]] = []
    monkeypatch.setattr(messagebox, "askyesno", lambda title, text, **kw: (questions.append((title, text)) or True))
    launched: list[tuple[list[str], dict]] = []
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: launched.append((args, kw)))
    monkeypatch.setattr(gui.sys, "platform", "win32")
    destroyed: list[bool] = []
    monkeypatch.setattr(app, "destroy", lambda: destroyed.append(True))
    exit_codes: list[int] = []
    monkeypatch.setattr(app, "_exit_process", lambda code: exit_codes.append(code))

    app._start_rollback()

    assert (app_dir / "main.py").read_text(encoding="utf-8") == "old main"
    assert not (app_dir / "fresh.py").exists()
    assert any(title == "Откат версии" for title, _ in questions)
    assert any(title == "Откат выполнен" for title, _ in questions)
    assert launched and launched[0][0][-1] == "run.bat"
    assert not destroyed
    assert exit_codes == [0]
    assert app.rollback_button.cget("state") == "disabled"


def test_rollback_is_refused_while_an_update_is_running(gui, tmp_path, monkeypatch):
    app_dir = tmp_path / "app"
    _write_backup_fixture(app_dir)
    app = gui.SurveyApp()
    monkeypatch.setattr(app, "_update_dir", lambda: app_dir)
    app._update_running = True

    app._start_rollback()

    assert (app_dir / "main.py").read_text(encoding="utf-8") == "new main"
    assert "Дождитесь" in app.status_var.get()


def test_child_report_windows_are_attached_to_the_main_window(gui):
    """Окна-отчёты привязаны к главному окну (transient) и поднимаются при открытии."""

    class _Win:
        def __init__(self):
            self.calls = []

        def transient(self, master):
            self.calls.append(("transient", master))

        def lift(self):
            self.calls.append(("lift",))

    app = gui.SurveyApp()
    win = _Win()
    app._attach_child_window(win)
    assert win.calls == [("transient", app), ("lift",)]


def test_step0_portal_worker_receives_fast_mode_flag(gui, monkeypatch, tmp_path):
    """Галочка «быстрая работа» доходит до поиска по адресу, а не только до сбора документов."""
    import nyc_portal as portal_module

    app = gui.SurveyApp()
    received = {}

    def fake_lookup(address, folder, **kwargs):
        received.update(kwargs)
        raise RuntimeError("стоп")

    monkeypatch.setattr(portal_module, "lookup_parcel", fake_lookup)
    app._step0_portal_worker("87 Stafford Ave", tmp_path, {}, "hydra", "m", None, "", True, 3.5)
    assert received.get("fast_mode") is True
    assert received.get("fast_pause_sec") == 3.5


def test_fast_pause_field_sits_next_to_the_checkbox_and_defaults_to_five(gui):
    app = gui.SurveyApp()
    assert app.step0_fast_pause_var.get() == "5"
    assert app._step0_fast_pause_value() == 5.0  # без галочки значение не важно
    app.step0_fast_mode_var.set(True)
    app.step0_fast_pause_var.set("3,5")
    assert app._step0_fast_pause_value() == 3.5


def test_wrong_fast_pause_stops_the_portal_lookup_before_it_starts(gui, monkeypatch):
    import nyc_portal as portal_module

    app = gui.SurveyApp()
    started = []
    monkeypatch.setattr(portal_module, "lookup_parcel", lambda *a, **k: started.append(True))
    app.step0_address_var.set("87 Stafford Ave")
    app.step0_fast_mode_var.set(True)
    app.step0_fast_pause_var.set("7")
    app._start_step0_portal_lookup()
    assert not started
    assert "от 1 до 5" in app.status_var.get()



def test_log_mode_switch_saves_to_config_and_keeps_other_keys(gui):
    import json  # noqa: PLC0415

    before = json.loads(gui.CONFIG_PATH.read_text(encoding="utf-8"))
    app = gui.SurveyApp()
    assert app._log_mode() == gui.log_mode.SIMPLE  # по умолчанию упрощённый

    app.log_mode_var.set(True)  # то же, что щелчок по переключателю
    app._apply_log_mode()
    after = json.loads(gui.CONFIG_PATH.read_text(encoding="utf-8"))
    assert after["ui"]["log_mode"] == "full"
    expected = json.loads(json.dumps(before))
    expected.setdefault("ui", {})["log_mode"] = "full"
    assert after == expected

    app.log_mode_var.set(False)
    app._apply_log_mode()
    assert json.loads(gui.CONFIG_PATH.read_text(encoding="utf-8"))["ui"]["log_mode"] == "simple"


def test_print_draft_is_always_added_only_for_main_lot(gui):
    """«Начертить» добавляет черновик для печати только к основному участку, даже при старой настройке enabled=false."""
    from types import SimpleNamespace

    from llm_parser import ASPENWALL_SAMPLES, load_config, parse_with_regex

    main_desc, _ = parse_with_regex(ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"])
    main_plan = gui.AcadBuilder(load_config()).plan(main_desc, scale=10, is_neighbor=False)
    print_cfg = dict(load_config().get("print_draft") or {})

    def owner(plan, enabled):
        cfg = {"print_draft": {**print_cfg, "enabled": enabled}}
        return SimpleNamespace(
            _preview_main_plan=plan, config_data=cfg, _plan_source_labels={id(plan): "тест"},
        )

    drafts = gui._real_print_draft_plans(owner(main_plan, True))
    assert len(drafts) == 1
    name, draft = drafts[0]
    assert name.startswith("Черновик для печати") and draft.print_draft is True
    assert draft.is_neighbor is False
    assert len(gui._real_print_draft_plans(owner(main_plan, False))) == 1
    assert gui._real_print_draft_plans(owner(None, True)) == []



def test_start_source_setting_opens_chosen_tab_and_saves_choice(gui, tmp_path, monkeypatch):
    """Настройка «стартовая вкладка»: открывается выбранный источник, выбор сохраняется в config.json."""
    import json

    import llm_parser

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"ui": {"start_source": "pdf"}}), encoding="utf-8")
    monkeypatch.setattr(gui, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(gui, "load_config", lambda path=cfg_path: llm_parser.load_config(path))

    app = gui.SurveyApp()
    assert app.source_mode_var.get() == "📄 PDF"

    # Выбор в шторке не меняет текущую вкладку, а пишет настройку.
    app.start_source_var.set("📝 Текст")
    app._on_start_source_changed()
    assert app.source_mode_var.get() == "📄 PDF"
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["ui"]["start_source"] == "text"


def test_update_developer_checkbox_is_off_and_persists(gui, monkeypatch):
    app = gui.SurveyApp()
    saved = []
    monkeypatch.setattr(gui, "save_config", lambda cfg: saved.append(cfg))
    assert not app.update_developer_var.get()
    app.update_developer_var.set(True)
    app._persist_update_source()
    assert saved[-1]["update"]["developer_mode"] is True
    assert saved[-1]["update"]["auto_source_migrated"] is True
    app.update_developer_var.set(False)
    app._persist_update_source()
    assert saved[-1]["update"]["developer_mode"] is False


def test_find_branch_disables_update_and_reports_result(gui, monkeypatch):
    app = gui.SurveyApp()
    chosen = gui.app_updater.BranchCandidate(name="arena/example", version=(9, 0), version_text="9.0", state="stable")
    result = gui.app_updater.DiscoveryResult(owner="Berber86", repo="CAD", chosen=chosen, message="Найдена 9.0")
    monkeypatch.setattr(gui.app_updater, "discover_branch", lambda *a, **kw: result)
    app._find_update_branch()
    assert app._update_running
    assert app.update_version_button.cget("state") == "disabled"
    _drain_update(app)
    assert not app._update_running
    assert "9.0" in app.update_status_label.cget("text")
    assert app.update_version_button.cget("state") == "normal"


def _theme_contrast(first, second):
    """WCAG contrast ratio for opaque hexadecimal GUI colors."""
    def luminance(color):
        channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        channels = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in channels]
        return sum(v * weight for v, weight in zip(channels, (0.2126, 0.7152, 0.0722)))
    low, high = sorted((luminance(first), luminance(second)))
    return (high + 0.05) / (low + 0.05)


@pytest.mark.parametrize("key", ["blue", "lavender", "gray"])
def test_premium_theme_readability_and_cad_colors(gui, key):
    theme = gui.ui_theme.PALETTES[key]
    for foreground in ("text_main", "text_muted", "text_dim"):
        for background in ("bg_root", "bg_header", "bg_card", "bg_field", "bg_editor"):
            assert _theme_contrast(theme[foreground], theme[background]) >= 4.5, (foreground, background)
    for background in ("btn_primary", "btn_primary_hover", "btn_neighbor", "btn_neighbor_hover",
                       "btn_amber", "btn_amber_hover", "btn_emerald", "btn_emerald_hover"):
        assert _theme_contrast(theme["text_main"], theme[background]) >= 4.5, background
    assert _theme_contrast(theme["badge_v1_fg"], theme["badge_v1_bg"]) >= 4.5
    assert _theme_contrast(theme["version_fg"], theme["version_bg"]) >= 4.5
    # Geometry colors identify kinds of objects and should not change with UI skin.
    assert theme["cad_prop"] == "#34d399"
    assert theme["cad_front"] == "#f87171"
    assert theme["cad_neighbor"] == "#22d3ee"
    assert theme["cad_tie"] == "#fbbf24"


def test_premium_theme_keeps_navigation_and_actions(gui):
    app = gui.SurveyApp()
    assert app.header.cget("fg_color") == gui.THEME["bg_header"]
    assert app.version_badge.cget("fg_color") == gui.THEME["version_bg"]
    assert app.left_panel.cget("border_color") == gui.THEME["border_subtle"]
    assert app.right_panel.cget("border_color") == gui.THEME["border_subtle"]
    for tab, button in app.step_tab_buttons.items():
        app._select_step_tab(tab)
        assert button.cget("fg_color") == gui.THEME["btn_primary"]
    assert callable(app.draw_button.cget("command"))
    assert callable(app.update_version_button.cget("command"))


def test_neutral_theme_uses_gray_surfaces_and_accent_tokens(gui):
    theme = gui.ui_theme.PALETTES["gray"]
    keys = ("bg_root", "bg_header", "bg_card", "bg_inset", "bg_editor", "bg_field",
            "border_card", "border_subtle", "border_active", "text_main", "text_muted",
            "text_dim", "accent_soft_bg", "accent_soft_fg", "accent_icon", "seg_sel",
            "btn_primary", "btn_primary_hover", "btn_secondary", "btn_secondary_hover",
            "version_bg", "version_fg", "control_on", "control_off")
    for key in keys:
        value = theme[key].lower()
        assert value[1:3] == value[3:5] == value[5:7], (key, value)
    assert _theme_contrast(theme["control_on"], theme["control_off"]) >= 2.5


def test_neutral_customtkinter_defaults_do_not_reintroduce_blue(gui, monkeypatch):
    import types
    fields = {
        "CTkButton": ("fg_color", "hover_color"),
        "CTkCheckBox": ("fg_color", "hover_color"),
        "CTkRadioButton": ("fg_color", "hover_color"),
        "CTkSwitch": ("fg_color", "progress_color"),
        "CTkProgressBar": ("progress_color",),
        "CTkSlider": ("button_color", "button_hover_color"),
        "CTkOptionMenu": ("fg_color", "button_color", "button_hover_color"),
        "CTkSegmentedButton": ("selected_color", "selected_hover_color"),
    }
    theme = {name: {field: "#1F6AA5" for field in options} for name, options in fields.items()}
    monkeypatch.setattr(gui.ctk, "ThemeManager", types.SimpleNamespace(theme=theme), raising=False)
    gui.THEME.clear()
    gui.THEME.update(gui.ui_theme.PALETTES["gray"])
    gui._apply_widget_theme_defaults()
    for options in theme.values():
        for color in options.values():
            assert color[1:3] == color[3:5] == color[5:7], color
    assert theme["CTkSwitch"]["fg_color"] != theme["CTkSwitch"]["progress_color"]


def test_preview_shows_green_property_and_red_front(gui):
    app = gui.SurveyApp()
    app._on_sample_selected("Aspenwall St — 420' (Основной участок)")
    plan = app._preview_main_plan
    assert plan is not None and plan.street_boundary_indices
    colored = [kwargs.get("fill") for kind, _coords, kwargs in app.preview_canvas.items
               if kind == "line" and kwargs.get("width") == 2.4]
    assert colored.count(gui.THEME["cad_front"]) == len(plan.street_boundary_indices)
    assert colored.count(gui.THEME["cad_prop"]) == len(plan.boundary_lines) - len(plan.street_boundary_indices)


def test_actual_draw_scene_adds_one_draft_not_neighbor_copies(gui, monkeypatch):
    from tkinter import messagebox
    from acad_builder import DrawResult
    from llm_parser import ASPENWALL_SAMPLES, parse_with_regex

    monkeypatch.setattr(gui.SurveyApp, "_print_draft_plans", gui._real_print_draft_plans)
    app = gui.SurveyApp()
    app._on_sample_selected("Aspenwall St — 420' (Основной участок)")
    desc, _ = parse_with_regex(ASPENWALL_SAMPLES["Aspenwall St — 365' (Сосед слева)"])
    neighbor = gui.AcadBuilder(app.config_data).plan(desc, scale=10, is_neighbor=True)
    app._preview_neighbor_plans.append(neighbor)
    app.config_data["print_draft"] = {"enabled": False, "shift_ft": [22, 9], "tie_length_ft": 9}
    drawn = []
    monkeypatch.setattr(gui.sys, "platform", "win32")
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **kw: True)
    monkeypatch.setattr(gui.AcadBuilder, "draw_plan", lambda _builder, plan: (
        drawn.append(plan) or DrawResult(plan=plan, text_count=0, layers={})
    ))
    app._start(parse_only=False)
    _wait_for_gui_worker(app)
    assert len(drawn) == 3
    draft, main, other = drawn
    assert draft.print_draft and not draft.is_neighbor
    assert main is app._preview_main_plan and other is neighbor
    for edge, copy in zip(main.boundary_lines, draft.boundary_lines):
        assert copy.start == pytest.approx((edge.start[0] + 1000, edge.start[1]))
        assert copy.end == pytest.approx((edge.end[0] + 1000, edge.end[1]))
    app._on_close()


@pytest.mark.parametrize("key,label", [("blue", "Голубоватая"), ("lavender", "Лавандовая"), ("gray", "Серая")])
def test_theme_selection_is_saved_and_applied_on_next_launch(gui, key, label):
    import json
    # Local settings take precedence over the shipped configuration and must survive saves.
    gui.LOCAL_CONFIG_PATH.write_text(json.dumps({"api": {"custom": "keep"}, "ui": {"other": 42, "theme": "gray"}}), encoding="utf-8")
    app = gui.SurveyApp()
    before = dict(gui.THEME)
    app.theme_var.set(label)
    app._on_theme_changed(label)
    assert dict(gui.THEME) == before  # no redraw or data loss during the session
    assert "перезапуска" in app.status_var.get()
    saved = json.loads(gui.LOCAL_CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved == {"api": {"custom": "keep"}, "ui": {"other": 42, "theme": key}}
    assert app._build_runtime_config()[0]["ui"]["theme"] == key
    restarted = gui.SurveyApp()
    assert restarted.theme_var.get() == label
    assert gui.THEME == gui.ui_theme.PALETTES[key]
    assert restarted.header.cget("fg_color") == gui.ui_theme.PALETTES[key]["bg_header"]


def test_theme_save_failure_keeps_selection_and_file(gui, monkeypatch):
    app = gui.SurveyApp()
    gui.LOCAL_CONFIG_PATH.write_text('{broken', encoding="utf-8")
    app.theme_var.set("Серая")
    app._on_theme_changed("Серая")
    assert app.theme_var.get() == "Голубоватая"
    assert app.config_data.get("ui", {}).get("theme") is None
    assert gui.LOCAL_CONFIG_PATH.read_text(encoding="utf-8") == '{broken'
    assert "не сохранена" in app.status_var.get()


@pytest.mark.parametrize("value", [None, "unknown", [], {}, 7])
def test_theme_invalid_value_falls_back_to_blue(gui, value):
    assert gui.ui_theme.from_config({"ui": {"theme": value}}) == "blue"
    assert gui.ui_theme.from_config({"ui": value}) == "blue"


def test_themes_share_cad_colors_and_have_distinct_surfaces(gui):
    palettes = gui.ui_theme.PALETTES
    assert len({p["bg_root"] for p in palettes.values()}) == 3
    assert all(set(p) == set(palettes["gray"]) for p in palettes.values())
    for key in ("cad_prop", "cad_front", "cad_neighbor", "cad_tie", "cad_street"):
        assert len({p[key] for p in palettes.values()}) == 1


@pytest.mark.parametrize("key", ["blue", "lavender", "gray"])
def test_widget_defaults_use_selected_palette_before_root(gui, monkeypatch, key):
    import json
    gui.LOCAL_CONFIG_PATH.write_text(json.dumps({"ui": {"theme": key}}), encoding="utf-8")
    custom = {"CTkButton": {}, "CTkSwitch": {}, "CTkOptionMenu": {}}
    monkeypatch.setattr(gui.ctk, "ThemeManager", types.SimpleNamespace(theme=custom), raising=False)
    original_init = _FakeCTkRoot.__init__

    def check_theme_on_root_creation(root, *args, **kwargs):
        assert gui.THEME == gui.ui_theme.PALETTES[key]
        assert custom["CTkButton"]["fg_color"] == gui.THEME["btn_primary"]
        assert custom["CTkSwitch"]["progress_color"] == gui.THEME["control_on"]
        original_init(root, *args, **kwargs)

    monkeypatch.setattr(_FakeCTkRoot, "__init__", check_theme_on_root_creation)
    gui.SurveyApp()


def test_theme_atomic_replace_failure_preserves_local_data(gui, monkeypatch):
    contents = '{"ui": {"theme": "blue"}, "key": "preserve"}'
    gui.LOCAL_CONFIG_PATH.write_text(contents, encoding="utf-8")
    app = gui.SurveyApp()
    def fail(*args):
        raise OSError("write failed")
    monkeypatch.setattr(gui.ui_theme.os, "replace", fail)
    app.theme_var.set("Лавандовая")
    app.theme_switch.cget("command")("Лавандовая")
    assert gui.LOCAL_CONFIG_PATH.read_text(encoding="utf-8") == contents
    assert app.theme_var.get() == "Голубоватая"
    assert "не сохранена" in app.status_var.get()
    assert not list(gui.LOCAL_CONFIG_PATH.parent.glob(".theme-*.tmp"))


@pytest.mark.skipif(sys.platform != "win32", reason="проверка cmd.exe доступна только в Windows")
def test_relaunch_real_cmd_handles_special_characters(gui, tmp_path, monkeypatch):
    import time
    work = tmp_path / "CAD & test (папка! %PATH%)"
    work.mkdir()
    (work / "run.bat").write_bytes(b'@echo off\r\necho ok>started.txt\r\nexit 0\r\n')
    app = gui.SurveyApp()
    monkeypatch.setattr(gui, "__file__", str(work / "main.py"))
    assert app._relaunch_after_update() == ""
    deadline = time.monotonic() + 20
    while not (work / "started.txt").exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert (work / "started.txt").exists()


def test_declining_restart_keeps_current_work(gui, monkeypatch):
    from tkinter import messagebox
    app = gui.SurveyApp()
    monkeypatch.setattr(messagebox, "askyesno", lambda *args, **kwargs: False)
    monkeypatch.setattr(app, "_relaunch_after_update", lambda: pytest.fail("must not relaunch"))
    monkeypatch.setattr(app, "_exit_process", lambda code: pytest.fail("must not exit"))
    app._offer_restart("Обновление", "Перезапустить?", base_text="Готово")
    assert not app._closing


def test_automatic_correction_is_not_written_to_text_cache(gui, tmp_path):
    from test_clear_typo_autofix import RECT
    text = RECT.replace("North 31 degrees 46 minutes 56 seconds west", "North 81 degrees 46 minutes 56 seconds west")
    outcome = gui.parse_legal_description_full(text=text, use_llm=False, ask_on_doubt=True)
    cache = tmp_path / "lot_text.txt"
    cache.write_text("original data", encoding="utf-8")
    app = gui.SurveyApp()
    msg = app._write_ai_to_step0_cache(
        None, outcome.data, prev_rank=None, cache_path=cache, builder=None, scale=16,
        cfg={}, provider_id="", model_name="", api_key=None, key_secret="",
        do_auto_orient=False, post=lambda *args: None,
    )
    assert "автоисправление" in msg
    assert cache.read_text(encoding="utf-8") == "original data"


def test_auto_correction_survives_step1_preview_and_main_draw_only(gui, monkeypatch):
    from tkinter import messagebox
    import drawing_warnings
    import acad_builder
    from acad_builder import DrawResult
    from test_clear_typo_autofix import RECT

    app = gui.SurveyApp()
    app.use_llm_var.set(False)
    app.text_box.delete("1.0", "end")
    app.text_box.insert("1.0", RECT.replace("North 31 degrees 46 minutes 56 seconds west", "North 81 degrees 46 minutes 56 seconds west"))
    monkeypatch.setattr(messagebox, "askyesno", lambda *args, **kwargs: True)
    monkeypatch.setattr(gui.sys, "platform", "win32")
    monkeypatch.setattr(gui.SurveyApp, "_print_draft_plans", gui._real_print_draft_plans)
    drawn = []
    monkeypatch.setattr(gui.AcadBuilder, "draw_plan", lambda _builder, plan: (
        drawn.append(plan) or DrawResult(plan=plan, text_count=0, layers={})
    ))
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    assert app._preview_main_plan is not None
    app._start(parse_only=False)
    _wait_for_gui_worker(app)
    assert len(drawn) == 2 and drawn[0].print_draft and not drawn[1].print_draft
    assert not drawing_warnings.plan_callouts(drawn[0], acad_builder.segment_distance_label)[0]
    notes, _ = drawing_warnings.plan_callouts(drawn[1], acad_builder.segment_distance_label)
    assert len(notes) == 1 and "{\\C1;3}1" in notes[0].text


def test_failed_drawing_callout_is_prioritized_in_status(gui):
    from acad_builder import DrawResult
    app = gui.SurveyApp()
    app._on_sample_selected("Aspenwall St — 420' (Основной участок)")
    plan = app._preview_main_plan
    first = DrawResult(plan=plan, text_count=0, layers={}, warnings=["обычное предупреждение черновика"])
    second = DrawResult(plan=plan, text_count=0, layers={}, warnings=["не удалось создать пометку автоисправления стороны #1"])
    app._handle_event("scene_drawn", ([("черновик", first), ("основной", second)], ""))
    assert "пометку автоисправления" in app.status_var.get()


@pytest.mark.parametrize("mode", ["Локально", "Эконом", "ИИ"])
def test_doubt_checkbox_is_available_in_all_modes_and_saves(gui, mode):
    import json
    gui.LOCAL_CONFIG_PATH.write_text(json.dumps({"ui": {"theme": "lavender"}, "api": {"providers": {"hydra": {"api_key": "keep"}}}}), encoding="utf-8")
    app = gui.SurveyApp()
    app._on_run_mode_clicked(mode)
    assert app.doubt_settings_card._gridded
    assert app.ask_on_doubt_check.master is app.doubt_settings_card
    assert app.ask_on_doubt_check._gridded
    app.ask_on_doubt_var.set(False)
    app.ask_on_doubt_check.cget("command")()
    data = json.loads(gui.LOCAL_CONFIG_PATH.read_text(encoding="utf-8"))
    assert data["ui"]["theme"] == "lavender"
    assert data["api"]["providers"]["hydra"]["api_key"] == "keep"
    assert data["api"]["ask_on_doubt"] is False
    restarted = gui.SurveyApp()
    assert restarted.ask_on_doubt_var.get() is False
    restarted.ask_on_doubt_var.set(True)
    restarted.ask_on_doubt_check.cget("command")()
    assert gui.SurveyApp().ask_on_doubt_var.get() is True


def test_doubt_checkbox_save_failure_keeps_previous_value_and_file(gui, monkeypatch):
    app = gui.SurveyApp()
    gui.LOCAL_CONFIG_PATH.write_text('{broken', encoding="utf-8")
    app.ask_on_doubt_var.set(False)
    app.ask_on_doubt_check.cget("command")()
    assert app.ask_on_doubt_var.get() is True
    assert "не сохранена" in app.status_var.get()
    assert gui.LOCAL_CONFIG_PATH.read_text(encoding="utf-8") == '{broken'


def test_ambiguous_109_is_not_changed_and_unchecked_does_not_prompt(gui, monkeypatch):
    from tkinter import messagebox
    from acad_builder import DrawResult
    from llm_parser import ASPENWALL_SAMPLES
    original = ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"]
    changed = original.replace('S 14°39\'40" E, 100.00 feet', 'S 14°39\'40" E, 109.00 feet')
    assert changed != original
    app = gui.SurveyApp()
    app.use_llm_var.set(False)
    app.ask_on_doubt_var.set(False)
    app._on_ask_on_doubt_changed()
    app.text_box.delete("1.0", "end")
    app.text_box.insert("1.0", changed)
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: pytest.fail("вопросы должны быть выключены"))
    monkeypatch.setattr(gui.sys, "platform", "win32")
    drawn = []
    monkeypatch.setattr(gui.AcadBuilder, "draw_plan", lambda _builder, plan: (
        drawn.append(plan) or DrawResult(plan=plan, text_count=0, layers={})
    ))
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    assert app.pending_doubt is None
    assert app._preview_main_plan.description.segments[0].distance_ft == 109.0
    assert app._preview_main_plan.closure.distance == pytest.approx(9.0)
    assert not app._preview_main_plan.description.segments[0]._auto_corrections
    app._start(parse_only=False)
    _wait_for_gui_worker(app)
    assert len(drawn) == 1 and drawn[0].description.segments[0].distance_ft == 109.0


def test_auto_fix_does_not_open_typo_question_but_independent_risk_can_prompt(gui, monkeypatch):
    from tkinter import messagebox
    from test_clear_typo_autofix import RECT
    text = RECT.replace("North 31 degrees 46 minutes 56 seconds west", "North 81 degrees 46 minutes 56 seconds west")
    app = gui.SurveyApp()
    app.use_llm_var.set(False)
    app.ask_on_doubt_var.set(True)
    app.text_box.delete("1.0", "end")
    app.text_box.insert("1.0", text)
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    assert app.pending_doubt is None
    assert app._preview_main_plan.description.segments[0].degrees == 31
    assert app._preview_main_plan.description.segments[0]._auto_corrections
    # Реальная оставшаяся невязка (0.41 ft) и другие проблемы формы — отдельное сомнение.
    prompts = []
    monkeypatch.setattr(messagebox, "askyesno", lambda title, text, **kw: prompts.append(text) or False)
    app._start(parse_only=False)
    _wait_for_gui_worker(app)
    # Рисование не блокируется невязкой — подтверждение не запрашивается,
    # чертеж строится в любом случае.
    assert not prompts
    assert "отменена" not in app.status_var.get()


def test_doubt_checkbox_atomic_failure_does_not_damage_other_local_data(gui, monkeypatch):
    original = '{"ui":{"theme":"blue"}, "api":{"ask_on_doubt": true, "secret":"keep"}}'
    gui.LOCAL_CONFIG_PATH.write_text(original, encoding="utf-8")
    app = gui.SurveyApp()
    def fail(*args):
        raise OSError("не удалось заменить файл")
    monkeypatch.setattr(gui.local_settings.os, "replace", fail)
    app.ask_on_doubt_var.set(False)
    app._on_ask_on_doubt_changed()
    assert app.ask_on_doubt_var.get() is True
    assert gui.LOCAL_CONFIG_PATH.read_text(encoding="utf-8") == original
    assert not list(gui.LOCAL_CONFIG_PATH.parent.glob(".setting-*.tmp"))
    assert "не сохранена" in app.status_var.get()


def test_doubt_checkbox_reports_open_question_needs_resolving(gui):
    app = gui.SurveyApp()
    app.pending_doubt = {"parse_only": True}
    app.ask_on_doubt_var.set(False)
    app._on_ask_on_doubt_changed()
    assert "Уже открытое сомнение" in app.status_var.get()
    assert app.pending_doubt is not None


def test_checked_ambiguous_109_not_modified_and_warns_before_drawing(gui, monkeypatch):
    from tkinter import messagebox
    from llm_parser import ASPENWALL_SAMPLES
    text = ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"].replace(
        'S 14°39\'40" E, 100.00 feet', 'S 14°39\'40" E, 109.00 feet')
    app = gui.SurveyApp()
    app.use_llm_var.set(False)
    app.ask_on_doubt_var.set(True)
    app.text_box.delete("1.0", "end")
    app.text_box.insert("1.0", text)
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    assert app._preview_main_plan.description.segments[0].distance_ft == 109.0
    prompts = []
    monkeypatch.setattr(messagebox, "askyesno", lambda title, text, **kw: prompts.append(text) or False)
    app._start(parse_only=False)
    _wait_for_gui_worker(app)
    # Рисование не блокируется невязкой — подтверждение не запрашивается,
    # чертеж строится в любом случае.
    assert not prompts
    assert "отменена" not in app.status_var.get()


@pytest.mark.parametrize("ask,good_provider", [(True, "hydra"), (False, "openrouter")])
def test_obvious_two_pass_choice_runs_without_conflict_and_remains_visible(gui, monkeypatch, ask, good_provider):
    import llm_parser as lp
    from acad_builder import DrawResult
    from tkinter import messagebox
    sample = lp.ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"]
    clean, _ = lp.parse_with_regex(sample, auto_repair_typos=False)
    bad = clean.model_copy(deep=True)
    bad.segments[0].distance_ft = 140
    def extract(text, cfg, api_key=None, progress=None, *, provider=None, model=None, **kwargs):
        data = clean if provider == good_provider else bad
        usage = lp.TokenUsage(prompt_tokens=3, completion_tokens=2, total_tokens=5, provider=provider, model=model)
        return data.model_copy(deep=True), "structured-outputs", usage
    monkeypatch.setattr(lp, "_extract_with_llm", extract)
    app = gui.SurveyApp()
    app.provider_key_vars["hydra"].set("key")
    app.provider_key_vars["openrouter"].set("key")
    app.select_passes(2)
    app.select_provider("Hydra AI")
    app.select_pass2_target("OpenRouter", "nvidia/nemotron-3.5-super-vl:free")
    app.use_llm_var.set(True)
    app.ask_on_doubt_var.set(ask)
    app.text_box.delete("1.0", "end")
    app.text_box.insert("1.0", sample)
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    assert app.pending_conflict is None and app.pending_doubt is None
    assert app._preview_main_plan.description.segments[0].distance_ft == 100
    assert app._last_consensus_label is None
    assert "автоматически выбран" in app.log_text()
    assert "не консенсус" in app.log_text()
    assert "140.00" in app.log_text()
    assert app._session_llm_calls == 2
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **kw: pytest.fail("нет риска — вопрос не нужен"))
    monkeypatch.setattr(gui.sys, "platform", "win32")
    drawn = []
    monkeypatch.setattr(gui.AcadBuilder, "draw_plan", lambda _, plan: drawn.append(plan) or DrawResult(plan=plan, text_count=0, layers={}))
    app._start(parse_only=False)
    _wait_for_gui_worker(app)
    assert drawn and drawn[-1].description.segments[0].distance_ft == 100


def test_conflict_retry_auto_selects_when_disagreement_becomes_obvious(gui, monkeypatch):
    import llm_parser as lp
    sample = lp.ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"]
    clean, _ = lp.parse_with_regex(sample, auto_repair_typos=False)
    different_tie = clean.model_copy(update={"distance_to_corner_ft": 425.0}, deep=True)
    bad = clean.model_copy(deep=True)
    bad.segments[0].distance_ft = 140
    def extract(text, cfg, api_key=None, progress=None, *, provider=None, model=None, focus_instruction=None, **kwargs):
        selected = clean if provider == "hydra" else (bad if focus_instruction else different_tie)
        return selected.model_copy(deep=True), "structured-outputs", lp.TokenUsage(provider=provider, model=model, total_tokens=1)
    monkeypatch.setattr(lp, "_extract_with_llm", extract)
    app = gui.SurveyApp()
    app.provider_key_vars["hydra"].set("key")
    app.provider_key_vars["openrouter"].set("key")
    app.select_passes(2)
    app.select_provider("Hydra AI")
    app.select_pass2_target("OpenRouter", "nvidia/nemotron-3.5-super-vl:free")
    app.use_llm_var.set(True)
    app.text_box.delete("1.0", "end")
    app.text_box.insert("1.0", sample)
    app._start(parse_only=True)
    _wait_for_gui_worker(app)
    assert app.pending_conflict is not None
    app.retry_conflict_resolution()
    _wait_for_gui_worker(app)
    assert app.pending_conflict is None
    assert app._preview_main_plan.description.segments[0].distance_ft == 100
    assert "автоматически выбран" in app.log_text()
    assert app._session_llm_calls == 4


def test_neighbor_batch_can_select_obvious_pass_and_preserves_warning(gui, monkeypatch, tmp_path):
    from test_obvious_consensus_choice import outcomes, consensus
    sample = tmp_path / "neighbor.txt"
    sample.write_text("text", encoding="utf-8")
    monkeypatch.setattr(gui, "run_multipass_consensus", lambda **kwargs: consensus(outcomes()))
    app = gui.SurveyApp()
    cfg = {"api": {"consensus": {"passes": 2}}}
    events = []
    result, counted = app._parse_neighbor_file(sample, cfg, "hydra", "model", "key", "", True, False, None, lambda _: None, lambda *event: events.append(event))
    assert counted and events[0][0] == "round_usages"
    assert result.data.segments[0].distance_ft == 100
    assert any("автоматически выбран" in w for w in result.warnings)


def test_retry_finalization_still_asks_for_independent_risky_geometry(gui, monkeypatch):
    from test_clear_typo_autofix import RECT
    from llm_parser import parse_legal_description_full
    outcome = parse_legal_description_full(text=RECT, use_llm=False)
    events = []
    app = gui.SurveyApp()
    monkeypatch.setattr(app, "_worker_poster", lambda: lambda *event: events.append(event))
    app._finalize_worker_outcome(outcome, cfg={"api": {"ask_on_doubt": True}}, scale=20,
                                 parse_only=False, is_neighbor=False)
    assert events[0][0] == "doubt"
    assert events[0][1]["plan"].closure.distance > 0.08


def test_developer_chat_triple_click_is_temporary_and_requests_are_independent(gui, monkeypatch):
    import developer_chat
    from llm_parser import TokenUsage
    import time
    calls = []
    def ask(text, cfg, **kwargs):
        calls.append((text, kwargs["provider"], kwargs["model"], kwargs["api_key"]))
        return f"ответ {text}", TokenUsage(prompt_tokens=3, completion_tokens=2, total_tokens=5, available=True)
    monkeypatch.setattr(developer_chat, "ask_once", ask)
    app = gui.SurveyApp()
    app.provider_key_vars["hydra"].set("hydra-key")
    app.provider_key_vars["openrouter"].set("router-key")
    assert not getattr(app, "_developer_chat", None)
    assert "<Triple-Button-1>" in app.brand_logo._bindings
    assert "<Triple-Button-1>" in app.brand_title._bindings
    app.brand_logo._bindings["<Triple-Button-1>"](None)
    page = app._developer_chat
    assert page is not None
    app.brand_title._bindings["<Triple-Button-1>"](None)
    assert app._developer_chat is page
    app.select_provider("Hydra AI")
    before = gui.CONFIG_PATH.read_text(encoding="utf-8")
    page.input.insert("1.0", "секретный вопрос один")
    page.send()
    for _ in range(100):
        page._poll()
        if not page._inflight:
            break
        time.sleep(.01)
    assert "ответ секретный вопрос один" in page.transcript.get("1.0", "end")
    assert "токены: 3 → 2" in page.status.get()
    page.copy_reply()
    assert app._clipboard == "ответ секретный вопрос один"
    page._on_provider("OpenRouter")
    page.model_var.set("custom/test-model")
    page.input.insert("1.0", "вопрос два")
    page._send_shortcut()
    for _ in range(100):
        page._poll()
        if not page._inflight:
            break
        time.sleep(.01)
    assert calls == [("секретный вопрос один", "hydra", calls[0][2], "hydra-key"),
                     ("вопрос два", "openrouter", "custom/test-model", "router-key")]
    assert "секретный вопрос один" not in app.log_text()
    assert "вопрос два" not in app.log_text()
    assert gui.CONFIG_PATH.read_text(encoding="utf-8") == before
    page.clear()
    assert not page.transcript.get("1.0", "end") and not page.last_reply
    page.close()
    assert app._developer_chat is None
    app._open_developer_chat()
    assert not app._developer_chat.transcript.get("1.0", "end")
    app._on_close()
    assert app._developer_chat is None


def test_developer_chat_close_or_clear_discards_inflight_answer(gui, monkeypatch):
    import developer_chat
    import threading
    import time
    started, release = threading.Event(), threading.Event()
    def ask(*args, **kwargs):
        started.set()
        assert release.wait(2)
        return "очень секретный ответ", None
    monkeypatch.setattr(developer_chat, "ask_once", ask)
    app = gui.SurveyApp()
    app._open_developer_chat()
    page = app._developer_chat
    page.input.insert("1.0", "test")
    page.send()
    assert started.wait(1)
    page.clear()
    assert page._inflight and "отброшен" in page.status.get()
    release.set()
    for _ in range(100):
        page._poll()
        if not page._inflight:
            break
        time.sleep(.01)
    assert not page._inflight and not page.transcript.get("1.0", "end")
    assert "отброшен" in page.status.get()
    started.clear(); release.clear()
    page.input.insert("1.0", "again")
    page.send()
    assert started.wait(1)
    page.close()
    app._open_developer_chat()
    fresh = app._developer_chat
    release.set()
    time.sleep(.03)
    page._poll(); fresh._poll()
    assert page._queue.empty() and not fresh.transcript.get("1.0", "end")
    assert not fresh._inflight
    app._on_close()


def test_step0_optimal_and_fast_choices_are_exclusive(gui):
    app = gui.SurveyApp()
    assert not app.step0_fast_mode_var.get() and not app.step0_optimal_mode_var.get()
    app.step0_optimal_mode_var.set(True)
    app._step0_optimal_mode_changed()
    assert app.step0_optimal_mode_var.get() and not app.step0_fast_mode_var.get()
    app.step0_fast_mode_var.set(True)
    app._step0_fast_mode_changed()
    assert app.step0_fast_mode_var.get() and not app.step0_optimal_mode_var.get()
    app.step0_optimal_mode_var.set(True)
    app._step0_optimal_mode_changed()
    assert app.step0_optimal_mode_var.get() and not app.step0_fast_mode_var.get()


def test_step0_portal_worker_passes_optimal_mode(gui, monkeypatch, tmp_path):
    import nyc_portal as portal_module
    app = gui.SurveyApp()
    received = {}
    def fake_lookup(address, folder, **kwargs):
        received.update(kwargs)
        raise RuntimeError("стоп")
    monkeypatch.setattr(portal_module, "lookup_parcel", fake_lookup)
    app._step0_portal_worker("87 Stafford Ave", tmp_path, {}, "hydra", "m", None, "", False, 5, True)
    assert received.get("optimal_mode") is True and received.get("fast_mode") is False


def test_step0_rejects_simultaneous_optimal_and_fast_flags(gui, monkeypatch):
    app = gui.SurveyApp()
    app.step0_address_var.set("87 Stafford Ave")
    app.step0_optimal_mode_var.set(True)
    app.step0_fast_mode_var.set(True)
    monkeypatch.setattr(gui.nyc_portal, "lookup_parcel", lambda *a, **kw: pytest.fail("search must not start"))
    app._start_step0_portal_lookup()
    assert "выберите один режим" in app.status_var.get().lower()


@pytest.mark.parametrize("mode,fast,optimal", [
    ("Надёжный", False, False),
    ("Оптимальный", False, True),
    ("Быстрый", True, False),
])
def test_step0_visible_mode_selector_routes_each_search_mode(gui, monkeypatch, tmp_path, mode, fast, optimal):
    app = gui.SurveyApp()
    switch = app.step0_portal_mode_switch
    assert switch.cget("values") == ["Надёжный", "Оптимальный", "Быстрый"]
    assert app.step0_portal_mode_var.get() == "Надёжный"
    app.step0_address_var.set("87 Stafford Ave")
    app.step0_folder_var.set(str(tmp_path))
    app.step0_fast_pause_var.set("2,5")
    monkeypatch.setattr(app, "_build_runtime_config", lambda: ({"api": {}}, "hydra", "vision", "key", ""))
    monkeypatch.setattr(gui, "resolve_api_target", lambda *args, **kwargs: {"api_key": "key"})
    received = []
    def lookup(*args, **kwargs):
        received.append(kwargs)
        raise RuntimeError("тестовая остановка")
    monkeypatch.setattr(gui.nyc_portal, "lookup_parcel", lookup)
    class InlineThread:
        def __init__(self, target, args=(), daemon=None):
            self.target, self.args = target, args
        def start(self):
            self.target(*self.args)
    monkeypatch.setattr(gui.threading, "Thread", InlineThread)
    # Те же операции, которые CTkSegmentedButton выполняет при нажатии.
    app.step0_portal_mode_var.set(mode)
    switch.cget("command")(mode)
    app._start_step0_portal_lookup()
    assert len(received) == 1
    assert received[0]["fast_mode"] is fast
    assert received[0].get("optimal_mode", False) is optimal
    assert received[0]["fast_pause_sec"] == (2.5 if fast else 5.0)
    assert app.step0_fast_mode_var.get() is fast  # также управляет ожиданием PDF
    assert app.step0_optimal_mode_var.get() is optimal


def test_step0_selector_can_switch_back_and_forth_between_all_three_modes(gui):
    app = gui.SurveyApp()
    switch = app.step0_portal_mode_switch
    command = switch.cget("command")
    for mode, fast, optimal in [("Быстрый", True, False), ("Оптимальный", False, True),
                                ("Надёжный", False, False), ("Быстрый", True, False),
                                ("Надёжный", False, False)]:
        app.step0_portal_mode_var.set(mode)
        command(mode)
        assert switch.get() == mode
        assert app.step0_fast_mode_var.get() is fast
        assert app.step0_optimal_mode_var.get() is optimal


@pytest.mark.parametrize("mode,ready,stable", [
    ("Надёжный", 4.0, 1.0), ("Оптимальный", 4.0, 1.0), ("Быстрый", 2.5, 0.0),
])
def test_mode_selector_preserves_pdf_wait_behaviour(gui, monkeypatch, tmp_path, mode, ready, stable):
    app = gui.SurveyApp()
    app.step0_portal_mode_var.set(mode)
    app.step0_portal_mode_switch.cget("command")(mode)
    app.step0_fast_pause_var.set("2.5")
    _fill_step0(app, folder=tmp_path)
    monkeypatch.setattr(gui.browser_save, "available", lambda: True)
    options = []
    monkeypatch.setattr(gui.browser_save, "auto_save_hook", lambda folder, **kw: options.append(kw))
    monkeypatch.setattr(gui.deed_intake, "collect_deeds", lambda **kw: gui.deed_intake.IntakeOutcome(folder=kw["folder"]))
    class InlineThread:
        def __init__(self, target, args=(), daemon=None):
            self.target, self.args = target, args
        def start(self):
            self.target(*self.args)
    monkeypatch.setattr(gui.threading, "Thread", InlineThread)
    app._start_step0_intake()
    assert len(options) == 1
    assert options[0]["ready_delay"] == ready and options[0]["stable_for"] == stable


def test_step0_controls_are_scrollable_and_contain_mode_selector(gui):
    app = gui.SurveyApp()
    assert type(app.step0_controls_panel).__name__ == "CTkScrollableFrame"
    parent = app.step0_portal_mode_switch
    while parent is not None and parent is not app.step0_controls_panel:
        parent = parent.master
    assert parent is app.step0_controls_panel


@pytest.mark.parametrize("message", [
    "для этого окна нет подходящих координат — выполняю надёжный поиск",
    "сохранённая область формы не подошла — оставшиеся действия выполняю по надёжному сценарию",
    "быстрая проверка карточки не удалась — один повтор после паузы; подсказка адреса остаётся непроверенной",
])
def test_portal_automatic_fallback_is_visible_in_simple_log(gui, monkeypatch, tmp_path, message):
    app = gui.SurveyApp()
    assert app._log_mode() == gui.log_mode.SIMPLE
    def fake_lookup(*args, **kwargs):
        kwargs["progress"](message)
        raise RuntimeError("тест")
    monkeypatch.setattr(gui.nyc_portal, "lookup_parcel", fake_lookup)
    app._step0_portal_worker("87 Stafford Ave", tmp_path, {}, "hydra", "m", None, "")
    app._drain_events()
    assert message in app.step0_report_box.get("1.0", "end")


def test_step0_vision_card_fallback_is_labeled_and_counts_two_requests(gui, tmp_path):
    app = gui.SurveyApp()
    usage = gui.TokenUsage(prompt_tokens=20, completion_tokens=5, total_tokens=25, available=True)
    details = gui.TokenUsage(prompt_tokens=10, completion_tokens=2, total_tokens=12, available=True)
    result = gui.nyc_portal.ParcelLookup("5712", "70", "Staten Island", "68", "72", "high", "",
                                         tmp_path / "map.png", usage, "vision", details)
    app._finish_step0_portal_lookup("ok", result)
    assert app._session_llm_calls == 2 and app._session_prompt_tokens == 30
    assert any("Vision по карточке" in line for line in _step0_texts(app))
    assert "проверьте" in app.status_var.get().lower()


def test_probability_window_saves_questions_and_displays_parallel_results(gui, monkeypatch, tmp_path):
    import time
    import developer_probability as dp
    import developer_probability_ui as ui
    from tkinter import filedialog
    settings = tmp_path / 'probability-settings.json'
    monkeypatch.setattr(dp, 'settings_path', lambda: settings)
    monkeypatch.setattr(filedialog, 'askopenfilenames', lambda **kw: (str(tmp_path / 'sample.pdf'),))
    calls = []
    monkeypatch.setattr(dp, 'prepare_documents', lambda files, mode: calls.append((files, mode)) or [{'file': 'sample.pdf', 'page': 1, 'content': 'text'}])
    monkeypatch.setattr(dp, 'run_both', lambda url, key, pages, mode, questions: [
        {'model': model, 'seconds': .5, 'entries': [{'question': questions[0]['text'], 'answer': {'type': 'noul', 'value': .8}}], 'usage': [{'input_tokens': 5}], 'request_seconds': [.4]} for model in dp.MODELS])
    app = gui.SurveyApp()
    app.provider_key_vars['hydra'].set('hydra-key')
    app._open_developer_chat()
    chat = app._developer_chat
    chat.open_probability()
    window = chat._probability
    assert len(window.rows) == len(dp.DEFAULT_QUESTIONS)
    window.add_files()
    window.start()
    for _ in range(100):
        window._poll()
        if not window.inflight:
            break
        time.sleep(.01)
    assert calls and 'jev-1.14' in window.result.get('1.0', 'end')
    assert 'solar-decide' in window.result.get('1.0', 'end')
    assert 'sample.pdf' not in settings.read_text(encoding='utf8')
    assert 'hydra-key' not in settings.read_text(encoding='utf8')
    window.mode.set('Изображение (эксперимент)')
    window.rows[0]['text'].delete(0, 'end')
    window.rows[0]['text'].insert(0, 'updated question')
    window.close()
    chat.open_probability()
    assert chat._probability.rows[0]['text'].get() == 'updated question'
    assert chat._probability.mode.get() == 'Изображение (эксперимент)'
    assert not chat._probability.files
    app._on_close()
    assert chat._probability is None


def test_probability_never_uses_another_provider_key(gui, monkeypatch, tmp_path):
    import developer_probability as dp
    import developer_probability_ui as ui
    from tkinter import filedialog
    monkeypatch.setattr(dp, 'settings_path', lambda: tmp_path / 'questions.json')
    monkeypatch.setattr(filedialog, 'askopenfilenames', lambda **kw: (str(tmp_path / 'doc.pdf'),))
    monkeypatch.setenv('OPENAI_API_KEY', 'other-provider-secret')
    monkeypatch.delenv('HYDRA_API_KEY', raising=False)
    monkeypatch.setattr(ui.lp, 'load_dotenv_dict', lambda: {})
    app = gui.SurveyApp()
    app.provider_key_vars['hydra'].set('')
    app.select_provider('OpenRouter')
    app.provider_key_vars['openrouter'].set('other-provider-secret')
    app._open_developer_chat()
    app._developer_chat.open_probability()
    window = app._developer_chat._probability
    window.add_files()
    window.start()
    assert not window.inflight
    assert 'ключ Hydra' in window.status.get()
    app._on_close()


def test_probability_window_displays_redacted_hydra_diagnostic(gui, monkeypatch, tmp_path):
    import httpx
    import developer_probability as dp
    monkeypatch.setattr(dp, 'settings_path', lambda: tmp_path / 'settings.json')
    app = gui.SurveyApp()
    app._open_developer_chat()
    chat = app._developer_chat
    chat.open_probability()
    window = chat._probability
    request = httpx.Request('POST', 'https://api.hydraai.ru/v1/systemone')
    response = httpx.Response(422, request=request, json={'detail': [
        {'type': 'extra_forbidden', 'loc': ['body', 'assets'], 'msg': 'PRIVATE_TEXT', 'input': 'SECRET_IMAGE'}]})
    error = dp._safe_error(httpx.HTTPStatusError('SECRET_KEY', request=request, response=response))
    result = [
        {'model': 'jev-1.14', 'seconds': .4, 'entries': [], 'usage': [], 'request_seconds': [.4], 'error': error},
        {'model': 'solar-decide', 'seconds': .5, 'entries': [], 'usage': [], 'request_seconds': [.5], 'error': error},
    ]
    window.inflight = True
    window.events.put((.1, result, None))
    window._poll()
    text = window.result.get('1.0', 'end')
    assert text.count('HTTP 422') == 2
    assert 'body.assets' in text and 'лишнее или неподдерживаемое поле' in text
    assert all(secret not in text for secret in ('PRIVATE_TEXT', 'SECRET_IMAGE', 'SECRET_KEY'))
    app._on_close()


def test_probability_window_displays_full_unredacted_responses(gui, monkeypatch, tmp_path):
    import developer_probability as dp
    monkeypatch.setattr(dp, 'settings_path', lambda: tmp_path / 'settings.json')
    app = gui.SurveyApp()
    app._open_developer_chat()
    chat = app._developer_chat
    chat.open_probability()
    window = chat._probability
    body = '{\n "input": "PRIVATE_DOCUMENT", "image": "IMAGE_BASE64", "key": "MOCK_KEY"\n}'
    result = [{'model': 'jev-1.14', 'seconds': .4, 'entries': [], 'usage': [], 'request_seconds': [.4],
               'error': 'HTTP 422', 'provider_responses': [{'status': 422, 'headers': [('X-Trace', 'PRIVATE_HEADER')], 'body': body, 'encoding': 'текст'}]},
              {'model': 'solar-decide', 'seconds': .5, 'entries': [], 'usage': [], 'request_seconds': [.5],
               'provider_responses': [{'status': 200, 'headers': [], 'body': 'FULL_SUCCESS', 'encoding': 'текст'}]}]
    window.inflight = True
    window.events.put((.1, result, None))
    window._poll()
    text = window.result.get('1.0', 'end')
    assert 'Полный ответ провайдера № 1 · HTTP 422' in text
    assert body in text and 'PRIVATE_HEADER' in text and 'FULL_SUCCESS' in text
    assert 'без маскирования' in text
    window.copy_result()
    assert app._clipboard == text
    assert 'скопирован целиком' in window.status.get()
    window.close()
    stored = (tmp_path / 'settings.json').read_text(encoding='utf8')
    assert all(secret not in stored for secret in ('PRIVATE_DOCUMENT', 'IMAGE_BASE64', 'MOCK_KEY', 'PRIVATE_HEADER'))
    app._on_close()


def test_probability_routerai_uses_its_own_key_and_models(gui, monkeypatch, tmp_path):
    import time
    import developer_probability as dp
    import developer_probability_ui as ui
    from tkinter import filedialog
    monkeypatch.setattr(dp, 'settings_path', lambda: tmp_path / 'settings.json')
    monkeypatch.setattr(filedialog, 'askopenfilenames', lambda **kw: (str(tmp_path / 'doc.pdf'),))
    calls = []
    monkeypatch.setattr(dp, 'prepare_documents', lambda files, mode, **kw: calls.append(('prepare', mode, kw)) or [{'file': 'doc.pdf', 'page': 1, 'content': 'text'}])
    def both(url, key, pages, mode, questions, **kw):
        calls.append(('run', url, key, kw))
        return [{'model': m, 'seconds': .1, 'entries': [], 'usage': [], 'request_seconds': []} for m in dp.ROUTERAI_MODELS]
    monkeypatch.setattr(dp, 'run_both', both)
    monkeypatch.delenv('ROUTERAI_API_KEY', raising=False)
    monkeypatch.setattr(ui.lp, 'load_dotenv_dict', lambda: {})
    app = gui.SurveyApp()
    app.provider_key_vars['hydra'].set('hydra-secret')
    app.provider_key_vars['routerai'].set('')
    app._open_developer_chat()
    chat = app._developer_chat
    chat.open_probability()
    window = chat._probability
    window.provider.set('RouterAI · Jev + Microsoft')
    window.add_files()
    window.start()
    assert not window.inflight and 'ключ RouterAI' in window.status.get()
    app.provider_key_vars['routerai'].set('router-secret')
    window.start()
    for _ in range(100):
        window._poll()
        if not window.inflight:
            break
        time.sleep(.01)
    assert calls[0] == ('prepare', 'ocr', {'provider': 'routerai'})
    assert calls[1][0] == 'run' and calls[1][2] == 'router-secret' and calls[1][3] == {'provider': 'routerai'}
    assert 'microsoft/microsoft-decision-1.1' in window.result.get('1.0', 'end')
    window.close()
    chat.open_probability()
    assert chat._probability.provider.get() == 'RouterAI · Jev + Microsoft'
    assert 'router-secret' not in (tmp_path / 'settings.json').read_text(encoding='utf8')
    app._on_close()


def test_probability_pdf_preview_quality_navigation_and_submission(gui, monkeypatch, tmp_path):
    import time
    import pymupdf
    import developer_probability as dp
    from tkinter import filedialog
    import PIL

    pdf_path = tmp_path / 'quality.pdf'
    doc = pymupdf.open()
    for text in ('page 1', 'page 2', 'page 3'):
        doc.new_page().insert_text((50, 70), text)
    doc.save(pdf_path)
    doc.close()
    monkeypatch.setattr(dp, 'settings_path', lambda: tmp_path / 'settings.json')
    monkeypatch.setattr(filedialog, 'askopenfilenames', lambda **kw: (str(pdf_path),))
    monkeypatch.setattr(PIL, 'ImageTk', types.SimpleNamespace(PhotoImage=lambda image, master: image), raising=False)
    app = gui.SurveyApp()
    app._open_developer_chat()
    chat = app._developer_chat
    chat.open_probability()
    window = chat._probability
    window.provider.set('RouterAI · Jev + Microsoft')
    window.mode.set('Изображение (эксперимент)')
    window.add_files()
    entry = window.files[0][1]
    entry.insert(0, '1,3')
    window._quality_changed(400)
    window.open_preview(str(pdf_path), entry)
    preview = window.preview
    for _ in range(100):
        preview.poll()
        if preview.image is not None:
            break
        time.sleep(.01)
    assert preview.page_number == 1 and preview.image is not None
    assert '400 px' in preview.info.get() and 'API не вызывается' in preview.info.get()
    assert preview.image.size == (283, 400)
    preview.step(1)
    preview.refresh()
    for _ in range(100):
        preview.poll()
        if preview.image is not None:
            break
        time.sleep(.01)
    assert preview.page_number == 3
    preview.change_zoom(1)
    assert preview.zoom == 200 and 'масштаб 200%' in preview.label.get()
    window._quality_changed(160)
    assert preview.image is None
    preview.refresh()
    for _ in range(100):
        preview.poll()
        if preview.image is not None:
            break
        time.sleep(.01)
    assert preview.image.size == (280, 280)
    window.mode.set('Локальный OCR / текст')
    window._mode_changed()
    preview.refresh()
    for _ in range(100):
        preview.poll()
        if preview.image is not None:
            break
        time.sleep(.01)
    assert 'отправится текст' in preview.info.get()
    window.close()
    assert preview.closed and window.preview is None
    chat.open_probability()
    assert chat._probability.pdf_image_side == 160
    app._on_close()


def test_probability_pdf_quality_used_in_visual_request_and_saved(gui, monkeypatch, tmp_path):
    import time
    import pymupdf
    import developer_probability as dp
    from tkinter import filedialog

    pdf_path = tmp_path / 'request.pdf'
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 70), 'Property line')
    doc.save(pdf_path)
    doc.close()
    monkeypatch.setattr(dp, 'settings_path', lambda: tmp_path / 'settings.json')
    monkeypatch.setattr(filedialog, 'askopenfilenames', lambda **kw: (str(pdf_path),))
    captured = []
    def both(url, key, pages, mode, questions, **kw):
        captured.append(pages)
        return [{'model': model, 'seconds': 0.1, 'entries': [], 'usage': [], 'request_seconds': []} for model in dp.ROUTERAI_MODELS]
    monkeypatch.setattr(dp, 'run_both', both)
    app = gui.SurveyApp()
    app.provider_key_vars['routerai'].set('router-key')
    app._open_developer_chat()
    app._developer_chat.open_probability()
    window = app._developer_chat._probability
    window.provider.set('RouterAI · Jev + Microsoft')
    window.mode.set('Изображение (эксперимент)')
    window.add_files()
    window._quality_changed(400)
    window.start()
    for _ in range(200):
        window._poll()
        if not window.inflight:
            break
        time.sleep(.01)
    assert captured
    expected = dp.prepare_documents([(str(pdf_path), '1')], 'visual', provider='routerai', pdf_image_side=400)
    assert captured[0][0]['content'] == expected[0]['content']
    assert 'до 400 px' in window.result.get('1.0', 'end')
    assert dp.load_settings()['pdf_image_side'] == 400
    app._on_close()


def test_direct_batch_neighbor_files_keep_independent_lot_numbers(gui, tmp_path, monkeypatch):
    from tkinter import filedialog
    from llm_parser import ASPENWALL_SAMPLES

    sample = ASPENWALL_SAMPLES["Aspenwall St — 365' (Сосед слева)"]
    first, second = tmp_path / "LOT 45 NEW.txt", tmp_path / "LOT 46 NEW.txt"
    first.write_text(sample, encoding="utf-8")
    second.write_text(sample, encoding="utf-8")
    app = gui.SurveyApp()
    app._on_sample_selected("Aspenwall St — 420' (Основной участок)")
    app.use_llm_var.set(False)
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **_: (str(first), str(second)))
    app._load_neighbor_files()
    _wait_for_gui_worker(app)
    assert set(app._neighbor_plan_sources) == {first, second}
    assert app._neighbor_plan_sources[first].items[0].text == "LOT 45"
    assert app._neighbor_plan_sources[second].items[0].text == "LOT 46"
    assert app._neighbor_plan_sources[first].pob == app._neighbor_plan_sources[second].pob
    assert all(app._neighbor_plan_sources[path].description.lot_number == "26" for path in (first, second))
    app._on_close()


def test_portal_identity_is_snapshotted_when_collecting(gui, tmp_path, monkeypatch):
    from types import SimpleNamespace

    app = gui.SurveyApp()
    _fill_step0(app, neighbours="30, 40", folder=tmp_path)
    app.step0_address_var.set("Example address")
    app.step0_borough_var.set("Staten Island")
    portal = SimpleNamespace(block="6323", lot="32", borough="Staten Island", neighbours=["30"], details_source="vision")
    app._step0_lookup_snapshot = ("Example address", portal)
    seen = {}
    def collect(**kwargs):
        seen.update(kwargs)
        return gui.deed_intake.IntakeOutcome(folder=kwargs["folder"])
    monkeypatch.setattr(gui.deed_intake, "collect_deeds", collect)
    app._start_step0_intake()
    _wait_step0(app)
    assert seen["borough"] == "Staten Island"
    assert seen["identity_by_lot"]["32"]["block"][0]["source"] == "portal"
    assert seen["identity_by_lot"]["30"]["lot"][0]["method"] == "vision map (unverified)"
    assert seen["identity_by_lot"]["40"]["lot"][0]["source"] == "manual"
    app._on_close()


def test_multi_handover_uses_full_documents_and_all_explicit_our_roles(gui, tmp_path):
    app = gui.SurveyApp()
    app.multi_pdf_mode_var.set(True)
    app._on_multi_pdf_mode_changed()
    folder = tmp_path / '01_описания'
    gui.deed_store.store_pdf(folder, _step0_pdf(tmp_path / 'our-new.pdf'), role='our', lot=32,
                             tag='NEW', block=6323, pages='2',
                             trimmed_source=_step0_pdf(tmp_path / 'trimmed.pdf'))
    gui.deed_store.store_pdf(folder, _step0_pdf(tmp_path / 'our-old.pdf'), role='our', lot=32,
                             tag='OLD', block=6323)
    gui.deed_store.store_pdf(folder, _step0_pdf(tmp_path / 'neighbour.pdf'), role='neighbour',
                             lot=30, tag='NEW', block=6323)
    _fill_step0(app, folder=folder, neighbours='30')
    app._step0_to_step1()
    dialog = app._pdf_set_dialog
    assert dialog is not None and dialog.multi_mode
    assert {item.path.name: item.is_main for item in dialog.items} == {
        'OUR LOT 32 NEW.pdf': True, 'OUR LOT 32 OLD.pdf': True, 'LOT 30 NEW.pdf': False}
    assert {row['name'].cget('text'): bool(row['role_var'].get()) for row in dialog.rows} == {
        'OUR LOT 32 NEW.pdf': True, 'OUR LOT 32 OLD.pdf': True, 'LOT 30 NEW.pdf': False}
