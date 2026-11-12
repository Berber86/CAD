"""
main.py
=======
GUI (CustomTkinter) для Прототипа 1: Legal Description -> AutoCAD.

Что умеет:
  * вставка текста Legal Description или загрузка PDF (PyMuPDF);
  * выбор масштаба 1"=10'/16'/20'/30' с пересчётом высоты текста
    0.9 * (scale / 16.0);
  * переключатель между тремя LLM-провайдерами (Hydra AI / OpenRouter / RouterAI)
    и выбор модели внутри каждого провайдера (либо ввод произвольной модели);
  * отдельные поля ввода API-ключей для каждого из трёх провайдеров;
  * отображение числа ушедших (prompt) и пришедших (completion) токенов за вызов
    и накопленной статистики за сессию;
  * кнопка «Начертить участок в AutoCAD» — полилиния + подписи сторон,
    фасадная и пересекаемая улицы;
  * вывод невязки (Closure Error, ft), площади и предупреждений;
  * окно поверх всех окон (topmost).

COM-вызовы AutoCAD выполняются в отдельном потоке (AutoCAD блокирует
главный поток GUI), результат возвращается в Tkinter через очередь.

Запуск:  python main.py
"""

from __future__ import annotations

import json
import os
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
        ctypes = None  # type: ignore[assignment]

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
    CONFIG_PATH,
    ENV_PATH,
    LOCAL_CONFIG_PATH,
    PROVIDER_ORDER,
    PROVIDERS_CATALOG,
    KeyProtectionError,
    LegalDescription,
    LegalDescriptionError,
    ParseOutcome,
    TokenUsage,
    clean_provider_key,
    is_protected_key,
    load_config,
    load_dotenv_dict,
    parse_legal_description_full,
    protect_api_key,
    read_pdf_text,
    save_config,
    save_protected_env,
)

APP_TITLE = "Title Survey AI — Legal Description → AutoCAD (Prototype 1)"
APP_GEOMETRY = "1050x960"
VERSION = "0.3.0"


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


def scale_value(label: str) -> float:
    """'1\"=16\\'' -> 16.0"""
    digits = "".join(char for char in label.split("=")[-1] if char.isdigit() or char == ".")
    try:
        return float(digits) or 16.0
    except ValueError:
        return 16.0


def provider_entries(cfg: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Упорядоченный список (provider_id, provider_cfg) для переключателя в GUI."""
    raw = cfg.get("api", {}).get("providers") or PROVIDERS_CATALOG
    ordered_ids: list[str] = [pid for pid in PROVIDER_ORDER if pid in raw]
    for extra_id in raw:
        if extra_id not in ordered_ids:
            ordered_ids.append(extra_id)
    result: list[tuple[str, dict[str, Any]]] = []
    for pid in ordered_ids:
        entry = raw.get(pid)
        if isinstance(entry, dict):
            result.append((pid, entry))
    return result


def _stored_provider_keys() -> dict[str, str]:
    """Читает ключи (обычно защищённые tsai1:…), записанные в .env / config.json."""
    stored: dict[str, str] = {}
    dotenv_vars = load_dotenv_dict(ENV_PATH)
    for pid, p_meta in PROVIDERS_CATALOG.items():
        env_name = str(p_meta.get("env_key") or "").strip()
        if env_name and dotenv_vars.get(env_name):
            stored[pid] = clean_provider_key(pid, dotenv_vars[env_name])
    for path in (CONFIG_PATH, LOCAL_CONFIG_PATH):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        providers = (data.get("api") or {}).get("providers") if isinstance(data, dict) else None
        if isinstance(providers, dict):
            for pid, p_cfg in providers.items():
                if isinstance(p_cfg, dict):
                    val = clean_provider_key(pid, p_cfg.get("api_key"))
                    if val:
                        stored[pid] = val
    return stored


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


class SurveyApp(ctk.CTk):
    """Главное окно приложения."""

    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title(f"{APP_TITLE}  v{VERSION}")
        self.geometry(APP_GEOMETRY)
        self.minsize(940, 800)

        # Окно поверх остальных, но не модальное: в AutoCAD надо кликать.
        self.attributes("-topmost", True)
        self.after(400, lambda: self.attributes("-topmost", True))
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.config_data: dict[str, Any] = load_config()
        self.pdf_path: Path | None = None
        self.description: LegalDescription | None = None
        self.draw_result: DrawResult | None = None
        self.last_usage: TokenUsage | None = None
        self._session_llm_calls = 0
        self._session_prompt_tokens = 0
        self._session_completion_tokens = 0
        self._busy = False
        self._closing = False
        self._events: queue.Queue[tuple[str, Any]] = queue.Queue()

        # Провайдеры и их модели
        entries = provider_entries(self.config_data)
        self.provider_ids: list[str] = [pid for pid, _ in entries]
        self._provider_cfgs: dict[str, dict[str, Any]] = {pid: cfg for pid, cfg in entries}
        self._label_to_pid: dict[str, str] = {}
        self._pid_to_label: dict[str, str] = {}
        self._provider_models: dict[str, list[str]] = {}
        self._selected_models: dict[str, str] = {}

        for pid, p_cfg in entries:
            label = str(p_cfg.get("label") or pid)
            self._label_to_pid[label] = pid
            self._pid_to_label[pid] = label
            models = [str(m) for m in (p_cfg.get("models") or []) if str(m).strip()]
            default_m = str(p_cfg.get("selected_model") or p_cfg.get("default_model") or "")
            if default_m and default_m not in models:
                models.insert(0, default_m)
            if not models:
                models = [default_m or "glm-5.2"]
            self._provider_models[pid] = models
            self._selected_models[pid] = default_m or models[0]

        initial_pid = str(self.config_data["api"].get("provider") or "hydra").strip().lower()
        if initial_pid not in self._pid_to_label:
            initial_pid = self.provider_ids[0]
        top_model = str(self.config_data["api"].get("model") or "").strip()
        if top_model:
            self._selected_models[initial_pid] = top_model
            if top_model not in self._provider_models[initial_pid]:
                self._provider_models[initial_pid].insert(0, top_model)

        self._active_provider_id: str = initial_pid
        self.provider_labels: list[str] = [self._pid_to_label[pid] for pid in self.provider_ids]
        self.provider_label_var = ctk.StringVar(value=self._pid_to_label[initial_pid])
        self.model_var = ctk.StringVar(value=self._selected_models[initial_pid])

        stored_keys = _stored_provider_keys()
        self.provider_key_vars: dict[str, Any] = {
            pid: ctk.StringVar(value=stored_keys.get(pid, ""))
            for pid in self.provider_ids
        }
        self.provider_key_entries: dict[str, Any] = {}
        self.provider_key_buttons: dict[str, Any] = {}
        self.provider_key_labels: dict[str, Any] = {}
        # Секретный код пересчёта (PIN) живёт ТОЛЬКО в оперативной памяти — никогда не пишется на диск
        self.key_secret_var = ctk.StringVar(value="")
        self._last_valid_secret: str = ""

        self.scale_options = scale_options(self.config_data)
        self.scale_var = ctk.StringVar(
            value=default_scale(self.scale_options, self.config_data)
        )
        self.use_llm_var = ctk.BooleanVar(value=True)

        self._build_ui()
        self._refresh_height_label()
        self._refresh_provider_highlights()
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
            text="Legal Description → AutoCAD  •  Hydra AI / OpenRouter / RouterAI",
            font=ctk.CTkFont(size=13),
            text_color=("#8fa3b8", "#8fa3b8"),
        ).grid(row=1, column=0, sticky="w", padx=18, pady=(0, 12))
        self.topmost_var = ctk.BooleanVar(value=True)
        ctk.CTkSwitch(
            header,
            text="Поверх всех окон",
            variable=self.topmost_var,
            command=self._apply_topmost,
        ).grid(row=0, column=1, rowspan=2, padx=18, sticky="e")

        # Настройки ---------------------------------------------------------
        settings = ctk.CTkFrame(self)
        settings.grid(row=1, column=0, sticky="ew", padx=16, pady=(12, 0))
        settings.grid_columnconfigure(0, weight=1)

        # Строка 0: масштаб + высота текста + переключатель LLM/regex
        scale_row = ctk.CTkFrame(settings, fg_color="transparent")
        scale_row.grid(row=0, column=0, sticky="ew", padx=14, pady=(10, 6))
        scale_row.grid_columnconfigure(2, weight=1)

        ctk.CTkLabel(scale_row, text="Масштаб:").grid(row=0, column=0, padx=(0, 8), sticky="w")
        self.scale_menu = ctk.CTkOptionMenu(
            scale_row,
            values=self.scale_options,
            variable=self.scale_var,
            width=125,
            command=lambda _value: self._refresh_height_label(),
        )
        self.scale_menu.grid(row=0, column=1, padx=(0, 14), sticky="w")

        self.height_label = ctk.CTkLabel(scale_row, text="", anchor="w")
        self.height_label.grid(row=0, column=2, sticky="w")

        self.use_llm_switch = ctk.CTkSwitch(
            scale_row,
            text="Использовать LLM (иначе — локальный regex)",
            variable=self.use_llm_var,
        )
        self.use_llm_switch.grid(row=0, column=3, padx=(12, 0), sticky="e")

        # Строка 1: переключатель провайдера + выбор/ввод модели
        llm_row = ctk.CTkFrame(settings, fg_color="transparent")
        llm_row.grid(row=1, column=0, sticky="ew", padx=14, pady=6)
        llm_row.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(llm_row, text="Провайдер:").grid(row=0, column=0, padx=(0, 8), sticky="w")
        self.provider_switch = ctk.CTkSegmentedButton(
            llm_row,
            values=self.provider_labels,
            variable=self.provider_label_var,
            command=self._on_provider_changed,
        )
        self.provider_switch.grid(row=0, column=1, padx=(0, 18), sticky="w")

        ctk.CTkLabel(llm_row, text="Модель:").grid(row=0, column=2, padx=(0, 8), sticky="w")
        self.model_combo = ctk.CTkComboBox(
            llm_row,
            values=self._provider_models[self._active_provider_id],
            variable=self.model_var,
            command=self._on_model_selected,
        )
        self.model_combo.grid(row=0, column=3, sticky="ew")

        # Строка 2: поля ввода API-ключей каждого из трёх провайдеров
        keys_frame = ctk.CTkFrame(settings, fg_color="transparent")
        keys_frame.grid(row=2, column=0, sticky="ew", padx=14, pady=(4, 6))
        for col, pid in enumerate(self.provider_ids):
            keys_frame.grid_columnconfigure(col, weight=1)
            card = ctk.CTkFrame(keys_frame, fg_color="transparent")
            card.grid(
                row=0,
                column=col,
                sticky="ew",
                padx=(0 if col == 0 else 8, 0 if col == len(self.provider_ids) - 1 else 8),
            )
            card.grid_columnconfigure(0, weight=1)

            p_cfg = self._provider_cfgs[pid]
            env_name = str(p_cfg.get("env_key") or "").strip()
            has_env = bool(env_name and os.environ.get(env_name, "").strip())
            placeholder = (
                f"{env_name} (подхвачен из env)"
                if has_env
                else f"Ключ {self._pid_to_label[pid]} ({env_name or 'sk-…'})"
            )

            lbl = ctk.CTkLabel(
                card,
                text=f"API key — {self._pid_to_label[pid]}",
                anchor="w",
                font=ctk.CTkFont(size=12, weight="bold"),
            )
            lbl.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 3))
            entry = ctk.CTkEntry(
                card,
                textvariable=self.provider_key_vars[pid],
                show="•",
                placeholder_text=placeholder,
            )
            entry.grid(row=1, column=0, sticky="ew")
            btn = ctk.CTkButton(
                card,
                text="👁",
                width=32,
                command=lambda p=pid: self._toggle_key_visibility(p),
            )
            btn.grid(row=1, column=1, padx=(5, 0))

            self.provider_key_labels[pid] = lbl
            self.provider_key_entries[pid] = entry
            self.provider_key_buttons[pid] = btn

        # Строка 3: защита ключей от кражи .json / .env (пересчёт реального ключа в RAM)
        vault_row = ctk.CTkFrame(settings, fg_color="transparent")
        vault_row.grid(row=3, column=0, sticky="ew", padx=14, pady=(2, 10))
        vault_row.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(
            vault_row,
            text="🔒 Код пересчёта (PIN в RAM, опц.):",
            font=ctk.CTkFont(size=12),
        ).grid(row=0, column=0, padx=(0, 8), sticky="w")
        self.key_secret_entry = ctk.CTkEntry(
            vault_row,
            textvariable=self.key_secret_var,
            show="•",
            width=195,
            placeholder_text="не пишется в .json/.env",
        )
        self.key_secret_entry.grid(row=0, column=1, sticky="w")
        self.secret_eye_button = ctk.CTkButton(
            vault_row,
            text="👁",
            width=32,
            command=self._toggle_secret_visibility,
        )
        self.secret_eye_button.grid(row=0, column=2, padx=(5, 12), sticky="w")
        ctk.CTkLabel(
            vault_row,
            text="На диске хранится только токен tsai1:… (привязка к ПК + DPAPI + PIN)",
            anchor="w",
            font=ctk.CTkFont(size=12),
            text_color=("#8fa3b8", "#8fa3b8"),
        ).grid(row=0, column=3, sticky="w")
        self.save_env_button = ctk.CTkButton(
            vault_row,
            text="Защитить в .env",
            width=145,
            command=self._save_protected_env_clicked,
        )
        self.save_env_button.grid(row=0, column=4, padx=(10, 0), sticky="e")

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

        # Статус, невязка и токены ------------------------------------------
        status_frame = ctk.CTkFrame(self)
        status_frame.grid(row=5, column=0, sticky="ew", padx=16, pady=(10, 14))
        status_frame.grid_columnconfigure(0, weight=1)

        metrics = ctk.CTkFrame(status_frame, fg_color="transparent")
        metrics.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 8))
        metrics.grid_columnconfigure(0, weight=1)

        self.status_var = ctk.StringVar(value="Готово. Загрузите PDF или вставьте текст.")
        self.status_label = ctk.CTkLabel(
            metrics,
            textvariable=self.status_var,
            anchor="w",
            font=ctk.CTkFont(size=13),
            justify="left",
            wraplength=880,
        )
        self.status_label.grid(row=0, column=0, sticky="w")

        self.closure_var = ctk.StringVar(value="Closure Error: —")
        self.closure_label = ctk.CTkLabel(
            metrics,
            textvariable=self.closure_var,
            anchor="w",
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        self.closure_label.grid(row=1, column=0, sticky="w", pady=(5, 0))

        self.tokens_var = ctk.StringVar(value="Токены за вызов: —")
        self.tokens_label = ctk.CTkLabel(
            metrics,
            textvariable=self.tokens_var,
            anchor="w",
            font=ctk.CTkFont(size=13),
            text_color=("#7ec8e3", "#7ec8e3"),
        )
        self.tokens_label.grid(row=2, column=0, sticky="w", pady=(3, 0))

        ctk.CTkButton(
            status_frame, text="Показать лог", width=120, command=self._show_log
        ).grid(row=0, column=1, padx=10, sticky="e")

    # --------------------------------------------------- провайдер / ключи --
    @property
    def key_entry(self) -> Any:
        """Поле API-ключа активного провайдера."""
        return self.provider_key_entries[self._active_provider_id]

    @property
    def key_button(self) -> Any:
        """Кнопка показа/скрытия ключа активного провайдера."""
        return self.provider_key_buttons[self._active_provider_id]

    def current_provider_id(self) -> str:
        label = str(self.provider_label_var.get() or "")
        return self._label_to_pid.get(label, self._active_provider_id)

    def current_model(self) -> str:
        typed = str(self.model_var.get() or "").strip()
        if not typed:
            combo_val = str(self.model_combo.get() or "").strip()
            typed = combo_val
        if typed:
            return typed
        pid = self.current_provider_id()
        return self._selected_models.get(pid) or self._provider_models[pid][0]

    def select_provider(self, provider_or_label: str) -> None:
        """Переключает активного провайдера по ID ('hydra') или подписи ('Hydra AI')."""
        candidate = provider_or_label.strip()
        pid = self._label_to_pid.get(candidate) or candidate.lower()
        if pid not in self._pid_to_label:
            raise ValueError(f"неизвестный провайдер: {provider_or_label!r}")
        label = self._pid_to_label[pid]
        self.provider_label_var.set(label)
        self.provider_switch.set(label)
        self._on_provider_changed(label)

    def _on_provider_changed(self, selected_label: str) -> None:
        new_pid = self._label_to_pid.get(selected_label)
        if not new_pid:
            return
        # Сохраняем модель, выбранную/введённую у предыдущего провайдера
        prev_model = str(self.model_var.get() or "").strip()
        if prev_model:
            self._selected_models[self._active_provider_id] = prev_model
            if prev_model not in self._provider_models[self._active_provider_id]:
                self._provider_models[self._active_provider_id].insert(0, prev_model)

        self._active_provider_id = new_pid
        models = self._provider_models[new_pid]
        chosen = self._selected_models.get(new_pid) or models[0]
        self.model_combo.configure(values=models)
        self.model_var.set(chosen)
        self.model_combo.set(chosen)
        self._refresh_provider_highlights()

    def _on_model_selected(self, chosen_model: str) -> None:
        clean = str(chosen_model or "").strip()
        if clean:
            self.model_var.set(clean)
            self._selected_models[self._active_provider_id] = clean

    def _refresh_provider_highlights(self) -> None:
        active = self._active_provider_id
        for pid, lbl in self.provider_key_labels.items():
            title = self._pid_to_label[pid]
            val = str(self.provider_key_vars[pid].get() or "").strip()
            lock = " 🔒" if is_protected_key(val) else ""
            if pid == active:
                lbl.configure(
                    text=f"API key — {title}{lock}  ● активен",
                    text_color=("#5dade2", "#5dade2"),
                )
            else:
                lbl.configure(
                    text=f"API key — {title}{lock}",
                    text_color=("#8fa3b8", "#8fa3b8"),
                )

    def _toggle_key_visibility(self, provider_id: str | None = None) -> None:
        """Ключи по умолчанию скрыты; кнопка-«глаз» показывает/скрывает ключ провайдера."""
        pid = provider_id or self._active_provider_id
        entry = self.provider_key_entries[pid]
        button = self.provider_key_buttons[pid]
        hidden = bool(entry.cget("show"))
        entry.configure(show="" if hidden else "•")
        button.configure(text="🙈" if hidden else "👁")

    def _toggle_secret_visibility(self) -> None:
        """Показывает/скрывает секретный PIN пересчёта ключей."""
        hidden = bool(self.key_secret_entry.cget("show"))
        self.key_secret_entry.configure(show="" if hidden else "•")
        self.secret_eye_button.configure(text="🙈" if hidden else "👁")

    # -------------------------------------------------------------- helpers --
    def _on_close(self) -> None:
        self._closing = True
        try:
            self.destroy()
        finally:
            self.quit()

    def _apply_topmost(self) -> None:
        self.attributes("-topmost", bool(self.topmost_var.get()))

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
        self.tokens_var.set("Токены за вызов: —")
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

    def _build_runtime_config(self) -> tuple[dict[str, Any], str, str, str | None, str]:
        """
        Собирает актуальный конфиг из виджетов окна перед вызовом или сохранением.
        Возвращает (cfg, provider_id, model_name, raw_or_protected_key, key_secret).
        """
        cfg = load_config()
        active_pid = self.current_provider_id()
        active_model = self.current_model()
        key_secret = str(self.key_secret_var.get() or "")
        self._selected_models[active_pid] = active_model

        providers = cfg["api"].setdefault("providers", {})
        for pid in self.provider_ids:
            p_cfg = providers.setdefault(pid, dict(self._provider_cfgs.get(pid, {})))
            typed_key = clean_provider_key(pid, self.provider_key_vars[pid].get())
            if typed_key:
                p_cfg["api_key"] = typed_key
            sel_model = self._selected_models.get(pid)
            if sel_model:
                p_cfg["selected_model"] = sel_model
                models = [str(m) for m in (p_cfg.get("models") or [])]
                if sel_model not in models:
                    models.insert(0, sel_model)
                    p_cfg["models"] = models

        active_p_cfg = providers.get(active_pid, {})
        cfg["api"]["provider"] = active_pid
        cfg["api"]["model"] = active_model
        if active_p_cfg.get("base_url"):
            cfg["api"]["base_url"] = active_p_cfg["base_url"]

        explicit_key = clean_provider_key(active_pid, self.provider_key_vars[active_pid].get())
        return cfg, active_pid, active_model, (explicit_key or None), key_secret

    def _save_settings(self) -> None:
        cfg, active_pid, active_model, _, key_secret = self._build_runtime_config()
        # На диск записываются ТОЛЬКО защищённые токены tsai1:… (открытые ключи не хранятся)
        protected_map: dict[str, str] = {}
        try:
            for pid in self.provider_ids:
                raw_val = self.provider_key_vars[pid].get()
                protected_val = protect_api_key(
                    pid,
                    raw_val,
                    secret=key_secret,
                    unlock_secret=self._last_valid_secret,
                )
                cfg["api"]["providers"][pid]["api_key"] = protected_val
                protected_map[pid] = protected_val
        except KeyProtectionError as exc:
            self._set_status(str(exc), error=True)
            return

        cfg["api"]["api_key"] = ""
        try:
            path = save_config(cfg)
        except OSError as exc:
            self._set_status(f"Не удалось сохранить config.json: {exc}", error=True)
            return

        for pid, protected_val in protected_map.items():
            if protected_val:
                self.provider_key_vars[pid].set(protected_val)
        self._last_valid_secret = key_secret
        self.config_data = load_config()
        self._refresh_height_label()
        self._refresh_provider_highlights()
        n_locked = sum(1 for v in protected_map.values() if v)
        self._set_status(
            f"Настройки сохранены ({self._pid_to_label[active_pid]} / {active_model}; "
            f"защищено ключей tsai1: {n_locked}): {path}"
        )

    def _save_protected_env_clicked(self) -> None:
        """Шифрует введённые/хранимые ключи провайдеров в файл .env (tsai1:…)."""
        key_secret = str(self.key_secret_var.get() or "")
        raw_map = {pid: self.provider_key_vars[pid].get() for pid in self.provider_ids}
        if not any(str(v or "").strip() for v in raw_map.values()):
            self._set_status(
                "Введите хотя бы один API-ключ провайдера перед записью в .env.",
                error=True,
            )
            return
        try:
            env_path = save_protected_env(
                raw_map,
                secret=key_secret,
                unlock_secret=self._last_valid_secret,
                path=ENV_PATH,
            )
        except (KeyProtectionError, OSError) as exc:
            self._set_status(f"Не удалось записать защищённый .env: {exc}", error=True)
            return

        dotenv_after = load_dotenv_dict(env_path)
        for pid in self.provider_ids:
            env_name = str(self._provider_cfgs[pid].get("env_key") or "").strip()
            if env_name and dotenv_after.get(env_name):
                self.provider_key_vars[pid].set(dotenv_after[env_name])
        self._last_valid_secret = key_secret
        self.config_data = load_config()
        self._refresh_provider_highlights()
        self._set_status(
            f"Защищённые ключи (tsai1:…) записаны в {env_path} — открытых ключей на диске нет"
        )

    def _show_log(self) -> None:
        window = ctk.CTkToplevel(self)
        window.title("Лог")
        window.geometry("780x540")
        window.attributes("-topmost", True)
        text = ctk.CTkTextbox(window, wrap="word", font=ctk.CTkFont(family="Consolas", size=12))
        text.pack(fill="both", expand=True, padx=10, pady=10)
        text.insert("1.0", self.log_text() or "Лог пуст.")
        text.insert("end", "\n\n--- Исходное описание ---\n" + self.text_box.get("1.0", "end"))
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
        self.status_label.configure(
            text_color=("#ff8a80", "#ff8a80") if error else ("#d6e4f0", "#d6e4f0")
        )
        self._log_line(message)

    # ------------------------------------------------------- асинхронные шаги --
    def _post(self, kind: str, payload: Any) -> None:
        self._events.put((kind, payload))

    def _drain_events(self) -> None:
        """Tkinter-поток: забирает сообщения из рабочего потока."""
        if getattr(self, "_closing", False):
            return
        while not self._events.empty():
            try:
                kind, payload = self._events.get_nowait()
            except queue.Empty:
                break
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
        self.after(120, self._drain_events)

    def _finish_busy(self) -> None:
        self._busy = False
        self.progress.stop()
        self.progress.set(0)
        self.draw_button.configure(state="normal")
        self.parse_button.configure(state="normal")
        self.save_button.configure(state="normal")
        self.save_env_button.configure(state="normal")

    def _start(self, parse_only: bool) -> None:
        if self._busy:
            return
        text = self.text_box.get("1.0", "end").strip()
        if not text and not self.pdf_path:
            self._set_status("Вставьте текст Legal Description или загрузите PDF.", error=True)
            return

        cfg, provider_id, model_name, api_key, key_secret = self._build_runtime_config()
        use_llm = bool(self.use_llm_var.get())
        scale = self._current_scale()
        provider_title = self._pid_to_label.get(provider_id, provider_id)

        self._busy = True
        self.draw_button.configure(state="disabled")
        self.parse_button.configure(state="disabled")
        self.save_button.configure(state="disabled")
        self.save_env_button.configure(state="disabled")
        self.progress.start()
        if use_llm:
            self.tokens_var.set(
                f"Токены за вызов: запрос к {provider_title} / {model_name}…"
            )
        self._set_status("Разбираю описание…")
        self._log_line(
            f"--- Новый запуск (масштаб 1\"={scale:g}', "
            f"режим={'LLM: ' + provider_title + ' / ' + model_name if use_llm else 'regex'}) ---"
        )

        worker = threading.Thread(
            target=self._worker,
            args=(
                text,
                cfg,
                provider_id,
                model_name,
                api_key,
                key_secret,
                use_llm,
                scale,
                parse_only,
            ),
            daemon=True,
        )
        worker.start()

    def _worker(
        self,
        text: str,
        cfg: dict[str, Any],
        provider_id: str,
        model_name: str,
        api_key: str | None,
        key_secret: str,
        use_llm: bool,
        scale: float,
        parse_only: bool,
    ) -> None:
        try:
            outcome = parse_legal_description_full(
                text=text,
                cfg=cfg,
                use_llm=use_llm,
                provider=provider_id,
                model=model_name,
                api_key=api_key,
                key_secret=key_secret,
                allow_regex_fallback=False,
                progress=lambda message: self._post("progress", message),
            )
            if use_llm:
                self._last_valid_secret = key_secret
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
        except KeyProtectionError as exc:
            self._post("error", str(exc))
            self._post("done", None)
        except (LegalDescriptionError, AcadBuilderError) as exc:
            message = str(exc)
            if use_llm:
                message += (
                    "\n\nПодсказка: проверьте API-ключ выбранного провайдера "
                    "или переключите провайдера/модель, либо выключите «Использовать LLM» "
                    "для локального разбора (regex)."
                )
            self._post("error", message)
            self._post("done", None)
        except Exception as exc:  # noqa: BLE001
            detail = traceback.format_exc(limit=6)
            self._post("error", f"Непредвиденная ошибка: {exc}\n{detail}")
            self._post("done", None)

    # ------------------------------------------------------------- вывод --
    def _format_tokens_line(self, outcome: ParseOutcome) -> str:
        if outcome.source != "llm" or outcome.usage is None:
            return "Токены за вызов: 0 (локальный разбор regex, без обращения к API)"
        base = outcome.usage.short_report()
        if self._session_llm_calls >= 2:
            sess_total = self._session_prompt_tokens + self._session_completion_tokens
            base += (
                f"   •   Сессия ({self._session_llm_calls} выз.): "
                f"↑ {self._session_prompt_tokens:,} / "
                f"↓ {self._session_completion_tokens:,} "
                f"(всего {sess_total:,})"
            )
        return base

    def _apply_parsed(self, outcome: ParseOutcome) -> None:
        self.description = outcome.data
        self.last_usage = outcome.usage
        if outcome.source == "llm" and outcome.usage is not None:
            self._session_llm_calls += 1
            if outcome.usage.available:
                self._session_prompt_tokens += outcome.usage.prompt_tokens
                self._session_completion_tokens += outcome.usage.completion_tokens
            self._log_line(
                f"[Токены] {outcome.provider_label or outcome.provider} / {outcome.model} "
                f"({outcome.llm_mode}): ушло {outcome.usage.prompt_tokens:,}, "
                f"пришло {outcome.usage.completion_tokens:,}, "
                f"всего {outcome.usage.total_tokens:,}"
            )
        self.tokens_var.set(self._format_tokens_line(outcome))
        for warning in outcome.warnings:
            self._log_line(f"! {warning}")

    def _parsed_summary(self, outcome: ParseOutcome) -> str:
        data = outcome.data
        if outcome.source == "llm":
            tag = " · ".join(
                p for p in (outcome.provider_label or outcome.provider, outcome.model) if p
            )
            source = f"LLM: {tag}" if tag else "LLM"
        else:
            source = "локальный парсер"
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
