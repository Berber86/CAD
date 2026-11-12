"""
main.py
=======
GUI (CustomTkinter) для Прототипа 1: Legal Description -> AutoCAD.

Что умеет:
  * вставка текста Legal Description или загрузка PDF (PyMuPDF);
  * выбор масштаба 1"=10'/16'/20'/30' с пересчётом высоты текста
    0.9 * (scale / 16.0);
  * ввод API-ключа / модели прямо в окне (ключ не пишется в config.json);
  * кнопка «Начертить участок в AutoCAD» — полилиния + подписи сторон,
    фасадная и пересекаемая улицы;
  * вывод невязки (Closure Error, ft), площади и предупреждений;
  * окно поверх всех окон (topmost).

COM-вызовы AutoCAD выполняются в отдельном потоке (AutoCAD блокирует
главный поток GUI), результат возвращается в Tkinter через очередь.

Запуск:  python main.py
"""

from __future__ import annotations

import queue
import sys
import threading
import traceback
from pathlib import Path
from typing import Any

# --- Windows DPI awareness: иначе CustomTkinter выглядит размыто -----------
if sys.platform == "win32":  # pragma: no cover - Windows only
    try:
        import ctypes

        try:
            # PROCESS_PER_MONITOR_DPI_AWARE (Windows 8.1+)
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:  # noqa: BLE001
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass

try:
    import customtkinter as ctk
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Не установлен customtkinter. Выполните: pip install customtkinter"
    ) from exc

from acad_builder import (
    AcadBuilder,
    AcadBuilderError,
    AcadConnectionError,
    DrawResult,
)
from llm_parser import (
    LegalDescription,
    LegalDescriptionError,
    ParseOutcome,
    load_config,
    parse_legal_description_full,
    read_pdf_text,
    save_config,
)

APP_TITLE = "Title Survey AI — Legal Description → AutoCAD (Prototype 1)"
APP_GEOMETRY = "1000x880"
VERSION = "0.1.0"


def scale_options(cfg: dict[str, Any]) -> list[str]:
    """Список подписей масштаба из конфига -> ['1"=10\'', …]."""
    values = cfg.get("drawing", {}).get("available_scales") or [10, 16, 20, 30]
    return [f'1"={float(value):g}\'' for value in values]


def default_scale(menu_values: list[str], cfg: dict[str, Any]) -> str:
    """Подпись базового масштаба (в конфиге 16.0) либо первый пункт списка."""
    base = float(cfg.get("drawing", {}).get("base_scale", 16.0))
    for label in menu_values:
        if scale_value(label) == base:
            return label
    return menu_values[0]

EXAMPLE_TEXT = (
    "BEING a tract of land situated in the City of Austin, Travis County, Texas, "
    "being Lot 12, Block A, of the Smith Addition, according to the map or plat "
    "thereof recorded in Volume 5, Page 120, Plat Records of Travis County, Texas, "
    "and being more particularly described by metes and bounds as follows:\n\n"
    "COMMENCING at a point in the East right-of-way line of Oak Avenue, being "
    "150.00 feet East of the East line of Oak Avenue;\n\n"
    "THENCE N 75°20'20\" E, a distance of 120.00 feet to a point for corner;\n"
    "THENCE S 14°39'40\" E, a distance of 85.50 feet to a point for corner;\n"
    "THENCE S 75°20'20\" W, a distance of 120.00 feet to a point for corner in the "
    "East right-of-way line of Oak Avenue;\n"
    "THENCE N 14°39'40\" W, a distance of 85.50 feet to the POINT OF BEGINNING.\n\n"
    "Said tract fronts on the South side of Main Street and contains 10,260 square "
    "feet (0.2355 acres) of land, more or less."
)


def scale_value(label: str) -> float:
    """'1\"=16\\'' -> 16.0"""
    digits = "".join(char for char in label.split("=")[-1] if char.isdigit() or char == ".")
    try:
        return float(digits) or 16.0
    except ValueError:
        return 16.0


class SurveyApp(ctk.CTk):
    """Главное окно приложения."""

    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title(f"{APP_TITLE}  v{VERSION}")
        self.geometry(APP_GEOMETRY)
        self.minsize(900, 760)

        # Окно поверх остальных, но не модальное: в AutoCAD надо кликать.
        self.attributes("-topmost", True)
        self.after(400, lambda: self.attributes("-topmost", True))
        # Кнопка закрытия: гасим фоновые события и завершаем процесс.
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.config_data: dict[str, Any] = load_config()
        self.pdf_path: Path | None = None
        self.description: LegalDescription | None = None
        self.draw_result: DrawResult | None = None
        self._busy = False
        self._events: queue.Queue[tuple[str, Any]] = queue.Queue()

        self.scale_options = scale_options(self.config_data)
        self.scale_var = ctk.StringVar(
            value=default_scale(self.scale_options, self.config_data)
        )
        self.key_var = ctk.StringVar(value="")
        self.model_var = ctk.StringVar(value=str(self.config_data["api"].get("model", "gpt-4.1")))
        self.use_llm_var = ctk.BooleanVar(value=True)

        self._build_ui()
        self._refresh_height_label()
        self.after(120, self._drain_events)

    # ------------------------------------------------------------------ UI --
    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # Заголовок ---------------------------------------------------------
        header = ctk.CTkFrame(self, corner_radius=0)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            header,
            text="Title Survey AI",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=18, pady=(12, 0))
        ctk.CTkLabel(
            header,
            text="Legal Description → AutoCAD  •  Prototype 1",
            font=ctk.CTkFont(size=13),
            text_color=("#8fa3b8", "#8fa3b8"),
        ).grid(row=1, column=0, sticky="w", padx=18, pady=(0, 12))
        self.topmost_var = ctk.BooleanVar(value=True)
        ctk.CTkSwitch(
            header, text="Поверх всех окон", variable=self.topmost_var,
            command=self._apply_topmost,
        ).grid(row=0, column=1, rowspan=2, padx=18, sticky="e")

        # Настройки ---------------------------------------------------------
        settings = ctk.CTkFrame(self)
        settings.grid(row=1, column=0, sticky="ew", padx=16, pady=(12, 0))
        settings.grid_columnconfigure(1, weight=1)
        settings.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(settings, text="Масштаб:").grid(row=0, column=0, padx=(14, 8), pady=10, sticky="w")
        self.scale_menu = ctk.CTkOptionMenu(
            settings,
            values=self.scale_options,
            variable=self.scale_var,
            width=130,
            command=lambda _value: self._refresh_height_label(),
        )
        self.scale_menu.grid(row=0, column=1, padx=(0, 16), pady=10, sticky="w")

        self.height_label = ctk.CTkLabel(settings, text="", anchor="w")
        self.height_label.grid(row=0, column=2, columnspan=2, padx=(0, 14), pady=10, sticky="w")

        ctk.CTkLabel(settings, text="Модель LLM:").grid(row=1, column=0, padx=(14, 8), pady=(0, 10), sticky="w")
        ctk.CTkEntry(settings, textvariable=self.model_var).grid(
            row=1, column=1, padx=(0, 16), pady=(0, 10), sticky="ew"
        )

        ctk.CTkLabel(settings, text="API key:").grid(row=1, column=2, padx=(0, 8), pady=(0, 10), sticky="e")
        key_frame = ctk.CTkFrame(settings, fg_color="transparent")
        key_frame.grid(row=1, column=3, padx=(0, 14), pady=(0, 10), sticky="ew")
        key_frame.grid_columnconfigure(0, weight=1)
        self.key_entry = ctk.CTkEntry(
            key_frame, textvariable=self.key_var, show="•", placeholder_text="sk-… (или в config.json)"
        )
        self.key_entry.grid(row=0, column=0, sticky="ew")
        self.key_button = ctk.CTkButton(
            key_frame, text="👁", width=34, command=self._toggle_key_visibility
        )
        self.key_button.grid(row=0, column=1, padx=(6, 0))

        self.use_llm_switch = ctk.CTkSwitch(
            settings, text="Использовать LLM (иначе — локальный разбор regex)",
            variable=self.use_llm_var,
        )
        self.use_llm_switch.grid(row=2, column=0, columnspan=4, padx=14, pady=(0, 12), sticky="w")

        # Ввод описания -----------------------------------------------------
        input_frame = ctk.CTkFrame(self)
        input_frame.grid(row=2, column=0, sticky="nsew", padx=16, pady=(12, 0))
        input_frame.grid_columnconfigure(0, weight=1)
        input_frame.grid_rowconfigure(2, weight=1)

        toolbar = ctk.CTkFrame(input_frame, fg_color="transparent")
        toolbar.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        toolbar.grid_columnconfigure(2, weight=1)
        ctk.CTkLabel(
            toolbar, text="Legal Description", font=ctk.CTkFont(size=14, weight="bold")
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(toolbar, text="Загрузить PDF", width=120, command=self._load_pdf).grid(
            row=0, column=1, padx=(12, 0)
        )
        ctk.CTkButton(toolbar, text="Пример", width=90, command=self._insert_example).grid(
            row=0, column=3, padx=(8, 0)
        )
        ctk.CTkButton(toolbar, text="Очистить", width=90, command=self._clear_text).grid(
            row=0, column=4, padx=(8, 0)
        )
        self.pdf_label = ctk.CTkLabel(
            toolbar, text="PDF не загружен", text_color=("#8fa3b8", "#8fa3b8")
        )
        self.pdf_label.grid(row=0, column=2, sticky="e", padx=12)

        self.text_box = ctk.CTkTextbox(input_frame, wrap="word", font=ctk.CTkFont(size=13))
        self.text_box.grid(row=2, column=0, sticky="nsew", padx=8, pady=(0, 8))
        self.text_box.insert("1.0", "")

        # Кнопки действий ---------------------------------------------------
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=3, column=0, sticky="ew", padx=16, pady=(10, 0))
        actions.grid_columnconfigure(1, weight=1)
        self.parse_button = ctk.CTkButton(
            actions,
            text="Разобрать описание",
            width=190,
            command=lambda: self._start(parse_only=True),
        )
        self.parse_button.grid(row=0, column=0, sticky="w")
        self.draw_button = ctk.CTkButton(
            actions,
            text="Начертить участок в AutoCAD",
            height=42,
            font=ctk.CTkFont(size=15, weight="bold"),
            command=lambda: self._start(parse_only=False),
        )
        self.draw_button.grid(row=0, column=1, padx=12, sticky="ew")
        self.save_button = ctk.CTkButton(
            actions, text="Сохранить настройки", width=170, command=self._save_settings
        )
        self.save_button.grid(row=0, column=2, sticky="e")

        self.progress = ctk.CTkProgressBar(self, mode="indeterminate")
        self.progress.grid(row=4, column=0, sticky="ew", padx=16, pady=(8, 0))
        self.progress.set(0)

        # Статус ------------------------------------------------------------
        status_frame = ctk.CTkFrame(self)
        status_frame.grid(row=5, column=0, sticky="ew", padx=16, pady=(10, 14))
        status_frame.grid_columnconfigure(0, weight=1)

        metrics = ctk.CTkFrame(status_frame, fg_color="transparent")
        metrics.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 0))
        metrics.grid_columnconfigure(3, weight=1)
        self.status_var = ctk.StringVar(value="Готово. Загрузите PDF или вставьте текст.")
        self.status_label = ctk.CTkLabel(
            metrics,
            textvariable=self.status_var,
            anchor="w",
            font=ctk.CTkFont(size=13),
            justify="left",
            wraplength=920,
        )
        self.status_label.grid(row=0, column=0, columnspan=4, sticky="w")

        self.closure_var = ctk.StringVar(value="Closure Error: —")
        self.closure_label = ctk.CTkLabel(
            metrics,
            textvariable=self.closure_var,
            anchor="w",
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        self.closure_label.grid(row=1, column=0, sticky="w", pady=(6, 8))

        ctk.CTkButton(
            status_frame, text="Показать лог", width=120, command=self._show_log
        ).grid(row=0, column=1, rowspan=2, padx=10, sticky="e")

    # -------------------------------------------------------------- helpers --
    def _on_close(self) -> None:
        self._closing = True
        try:
            self.destroy()
        finally:
            self.quit()

    def _apply_topmost(self) -> None:
        self.attributes("-topmost", bool(self.topmost_var.get()))

    def _toggle_key_visibility(self) -> None:
        """Ключ по умолчанию скрыт; кнопка-«глаз» показывает/скрывает его."""
        hidden = bool(self.key_entry.cget("show"))
        self.key_entry.configure(show="" if hidden else "•")
        self.key_button.configure(text="🙈" if hidden else "👁")

    def _current_scale(self) -> float:
        return scale_value(self.scale_var.get())

    def _refresh_height_label(self) -> None:
        scale = self._current_scale()
        height = AcadBuilder.text_height_for_scale(scale, self.config_data)
        base = float(self.config_data["drawing"].get("base_text_height", 0.9))
        self.height_label.configure(
            text=f"высота текста = {base:g} × ({scale:g} / 16) = {height:.3f}"
        )

    def _insert_example(self) -> None:
        self.text_box.delete("1.0", "end")
        self.text_box.insert("1.0", EXAMPLE_TEXT)
        self.pdf_path = None
        self.pdf_label.configure(text="Пример из буфера (PDF не загружен)")

    def _clear_text(self) -> None:
        self.text_box.delete("1.0", "end")
        self.pdf_path = None
        self.pdf_label.configure(text="PDF не загружен")
        self.description = None
        self.draw_result = None
        self.closure_var.set("Closure Error: —")
        self.status_var.set("Очищено.")

    def _load_pdf(self) -> None:
        from tkinter import filedialog

        path = filedialog.askopenfilename(
            title="Выберите PDF с Legal Description",
            filetypes=[("PDF", "*.pdf"), ("Все файлы", "*.*")],
        )
        if not path:
            return
        try:
            text = read_pdf_text(path)
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"Не удалось прочитать PDF: {exc}", error=True)
            return
        self.pdf_path = Path(path)
        self.text_box.delete("1.0", "end")
        self.text_box.insert("1.0", text)
        self.pdf_label.configure(text=f"PDF: {self.pdf_path.name} ({len(text):,} символов)")
        self._set_status(f"PDF загружен: {self.pdf_path.name}")

    def _save_settings(self) -> None:
        cfg = load_config()
        cfg["api"]["model"] = self.model_var.get().strip() or cfg["api"].get("model")
        api_key = self.key_var.get().strip()
        if api_key:
            cfg["api"]["api_key"] = api_key
        try:
            path = save_config(cfg)
        except OSError as exc:
            self._set_status(f"Не удалось сохранить config.json: {exc}", error=True)
            return
        self.config_data = cfg
        self._refresh_height_label()
        self._set_status(f"Настройки сохранены: {path}")

    def _show_log(self) -> None:
        window = ctk.CTkToplevel(self)
        window.title("Лог")
        window.geometry("760x520")
        window.attributes("-topmost", True)
        text = ctk.CTkTextbox(window, wrap="word", font=ctk.CTkFont(family="Consolas", size=12))
        text.pack(fill="both", expand=True, padx=10, pady=10)
        text.insert("1.0", self.log_text() or "Лог пуст.")
        text.insert("end", "\n\n--- Исходное описание ---\n" + self.text_box.get("1.0", "end"))
        # CTkTextbox не принимает state через configure — переключаем внутренний Tk-виджет
        target = getattr(text, "_textbox", text)
        target.configure(state="disabled")

    def log_text(self) -> str:
        return "\n".join(getattr(self, "_log", []))

    def _log_line(self, message: str) -> None:
        if not hasattr(self, "_log"):
            self._log = []
        self._log.append(message)
        del self._log[:-400]

    def _set_status(self, message: str, error: bool = False) -> None:
        self.status_var.set(message)
        self.status_label.configure(text_color=("#ff8a80", "#ff8a80") if error else ("#d6e4f0", "#d6e4f0"))
        self._log_line(message)

    # ------------------------------------------------------- асинхронные шаги --
    def _post(self, kind: str, payload: Any) -> None:
        self._events.put((kind, payload))

    def _drain_events(self) -> None:
        """Tkinter-поток: забирает сообщения из рабочего потока."""
        if getattr(self, "_closing", False):
            return
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "progress":
                    self._set_status(str(payload))
                elif kind == "error":
                    self._finish_busy()
                    self._set_status(str(payload), error=True)
                    self.closure_var.set("Closure Error: —")
                elif kind == "parsed":
                    outcome, draw = payload
                    if draw:
                        self._handle_draw_result(outcome, draw)
                    else:
                        self._finish_busy()
                        self._apply_parsed(outcome)
                        self._set_status(self._parsed_summary(outcome))
                        self.closure_var.set(self._closure_text(outcome))
                elif kind == "done":
                    self._finish_busy()
        except queue.Empty:
            pass
        self.after(120, self._drain_events)

    def _finish_busy(self) -> None:
        self._busy = False
        self.progress.stop()
        self.progress.set(0)
        self.draw_button.configure(state="normal")
        self.parse_button.configure(state="normal")
        self.save_button.configure(state="normal")

    def _start(self, parse_only: bool) -> None:
        if self._busy:
            return
        text = self.text_box.get("1.0", "end").strip()
        if not text and not self.pdf_path:
            self._set_status("Вставьте текст Legal Description или загрузите PDF.", error=True)
            return

        cfg = load_config()
        cfg["api"]["model"] = self.model_var.get().strip() or cfg["api"].get("model")
        api_key = self.key_var.get().strip() or None
        use_llm = bool(self.use_llm_var.get())
        scale = self._current_scale()

        self._busy = True
        self.draw_button.configure(state="disabled")
        self.parse_button.configure(state="disabled")
        self.save_button.configure(state="disabled")
        self.progress.start()
        self._set_status("Разбираю описание…")
        self._log_line(f"--- Новый запуск (масштаб 1\"={scale:g}', LLM={use_llm}) ---")

        # Текст из поля уже содержит содержимое PDF (если он загружался),
        # поэтому pdf_path сюда не передаём, чтобы не дублировать.
        worker = threading.Thread(
            target=self._worker,
            args=(text, cfg, api_key, use_llm, scale, parse_only),
            daemon=True,
        )
        worker.start()

    def _worker(
        self,
        text: str,
        cfg: dict[str, Any],
        api_key: str | None,
        use_llm: bool,
        scale: float,
        parse_only: bool,
    ) -> None:
        try:
            outcome = parse_legal_description_full(
                text=text,
                cfg=cfg,
                use_llm=use_llm,
                api_key=api_key,
                allow_regex_fallback=False,
                progress=lambda message: self._post("progress", message),
            )
            builder = AcadBuilder(cfg)
            plan = builder.plan(outcome.data, scale=scale)
            self._post(
                "progress",
                f"Готово ({outcome.source}): сторон {len(outcome.data.segments)}, "
                f"Closure Error {plan.closure.distance:.3f} ft",
            )
            if parse_only:
                self._post("parsed", (outcome, None))
                self._post("done", None)
                return

            self._post("progress", "Рисую в AutoCAD…")
            if sys.platform != "win32":
                raise AcadConnectionError(
                    "Отрисовка возможна только в Windows с запущенным AutoCAD "
                    f"(текущая ОС: {sys.platform})"
                )
            result = builder.draw(outcome.data, scale=scale)
            self._post("parsed", (outcome, result))
            self._post("done", None)
        except (LegalDescriptionError, AcadBuilderError) as exc:
            message = str(exc)
            if use_llm:
                message += (
                    "\n\nПодсказка: выключите выключатель «Использовать LLM», "
                    "чтобы разобрать описание локально (regex), без обращения к API."
                )
            self._post("error", message)
            self._post("done", None)
        except Exception as exc:  # noqa: BLE001
            detail = traceback.format_exc(limit=6)
            self._post("error", f"Непредвиденная ошибка: {exc}\n{detail}")
            self._post("done", None)

    # ------------------------------------------------------------- вывод --
    def _apply_parsed(self, outcome: ParseOutcome) -> None:
        self.description = outcome.data
        for warning in outcome.warnings:
            self._log_line(f"! {warning}")

    def _parsed_summary(self, outcome: ParseOutcome) -> str:
        data = outcome.data
        source = "LLM" if outcome.source == "llm" else "локальный парсер"
        parts = [
            f"Разобрано ({source}): сторон {len(data.segments)}, "
            f"периметр {data.perimeter_ft:,.2f} ft",
            f"Фасад: {data.main_street or '—'}",
        ]
        if data.cross_street:
            distance = data.distance_to_corner_ft
            parts.append(
                f"Пересечение: {data.cross_street}"
                + (f", {distance:.2f} ft до угла" if distance is not None else "")
            )
        if outcome.warnings:
            parts.append(f"предупреждений: {len(outcome.warnings)} (см. лог)")
        return "  |  ".join(parts)

    def _closure_text(self, outcome: ParseOutcome) -> str:
        try:
            builder = AcadBuilder(self.config_data)
            plan = builder.plan(outcome.data, scale=self._current_scale())
        except Exception:  # noqa: BLE001
            return "Closure Error: —"
        return plan.closure.short_report()

    def _handle_draw_result(self, outcome: ParseOutcome, result: DrawResult) -> None:
        self.draw_result = result
        self._apply_parsed(outcome)
        self._set_status(result.summary())
        self.closure_var.set(result.closure.short_report())


def main() -> None:
    app = SurveyApp()
    app.mainloop()


if __name__ == "__main__":
    main()
