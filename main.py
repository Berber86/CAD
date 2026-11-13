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
    compute_pob_point,
)
from llm_parser import (
    ASPENWALL_SAMPLES,
    CONFIG_PATH,
    ENV_PATH,
    LOCAL_CONFIG_PATH,
    NYC_SAMPLES,
    PROVIDER_ORDER,
    PROVIDERS_CATALOG,
    ConsensusResult,
    KeyProtectionError,
    LLMParseError,
    LegalDescription,
    LegalDescriptionError,
    ParseOutcome,
    TokenUsage,
    clean_provider_key,
    inspect_pdf,
    is_protected_key,
    load_config,
    load_dotenv_dict,
    parse_legal_description_full,
    protect_api_key,
    recheck_consensus_discrepancies,
    run_multipass_consensus,
    save_config,
    save_protected_env,
)

APP_TITLE = "Title Survey AI — Legal Description → AutoCAD (Prototype 1)"
APP_GEOMETRY = "1080x1020"
VERSION = "0.6.0"


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
        self._pdf_needs_vision: bool = False
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

        # Настройки 2–3 независимых проходов LLM (консенсус / арбитраж)
        cons_cfg = self.config_data["api"].get("consensus") or {}
        init_passes = max(1, min(3, int(cons_cfg.get("passes") or 1)))
        self.passes_options = ["1 проход", "2 прохода", "3 прохода"]
        self.passes_label_var = ctk.StringVar(value=self.passes_options[init_passes - 1])

        p2_pid = str(cons_cfg.get("pass2_provider") or "openrouter").strip().lower()
        if p2_pid not in self._pid_to_label:
            p2_pid = self.provider_ids[min(1, len(self.provider_ids) - 1)]
        p2_model = str(
            cons_cfg.get("pass2_model") or self._selected_models[p2_pid]
        ).strip()
        if p2_model and p2_model not in self._provider_models[p2_pid]:
            self._provider_models[p2_pid].insert(0, p2_model)
        self._pass2_pid: str = p2_pid
        self.pass2_provider_label_var = ctk.StringVar(value=self._pid_to_label[p2_pid])
        self.pass2_model_var = ctk.StringVar(value=p2_model or self._provider_models[p2_pid][0])

        p3_pid = str(cons_cfg.get("pass3_provider") or "routerai").strip().lower()
        if p3_pid not in self._pid_to_label:
            p3_pid = self.provider_ids[min(2, len(self.provider_ids) - 1)]
        p3_model = str(
            cons_cfg.get("pass3_model") or self._selected_models[p3_pid]
        ).strip()
        if p3_model and p3_model not in self._provider_models[p3_pid]:
            self._provider_models[p3_pid].insert(0, p3_model)
        self._pass3_pid: str = p3_pid
        self.pass3_provider_label_var = ctk.StringVar(value=self._pid_to_label[p3_pid])
        self.pass3_model_var = ctk.StringVar(value=p3_model or self._provider_models[p3_pid][0])

        self.pending_conflict: dict[str, Any] | None = None
        self.conflict_window: Any | None = None

        self._build_ui()
        self._refresh_height_label()
        self._refresh_provider_highlights()
        self._refresh_consensus_rows()
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

        # Строка 1: переключатель провайдера + выбор/ввод модели (Проход #1)
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

        # Строка 2: переключатель режима 1 / 2 / 3 независимых проходов LLM
        consensus_row = ctk.CTkFrame(settings, fg_color="transparent")
        consensus_row.grid(row=2, column=0, sticky="ew", padx=14, pady=(2, 4))
        consensus_row.grid_columnconfigure(2, weight=1)

        ctk.CTkLabel(
            consensus_row,
            text="Режим сверки LLM:",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).grid(row=0, column=0, padx=(0, 8), sticky="w")
        self.passes_switch = ctk.CTkSegmentedButton(
            consensus_row,
            values=self.passes_options,
            variable=self.passes_label_var,
            command=self._on_passes_changed,
        )
        self.passes_switch.grid(row=0, column=1, padx=(0, 14), sticky="w")
        self.consensus_hint_label = ctk.CTkLabel(
            consensus_row,
            text="1 проход — одиночный вызов; 2–3 прохода — параллельная сверка моделей и арбитраж",
            anchor="w",
            font=ctk.CTkFont(size=12),
            text_color=("#8fa3b8", "#8fa3b8"),
        )
        self.consensus_hint_label.grid(row=0, column=2, sticky="w")

        # Строка 3: независимый Проход #2 (показывается при 2 или 3 проходах)
        self.pass2_row = ctk.CTkFrame(settings, fg_color="transparent")
        self.pass2_row.grid(row=3, column=0, sticky="ew", padx=14, pady=(2, 4))
        self.pass2_row.grid_columnconfigure(3, weight=1)
        ctk.CTkLabel(
            self.pass2_row,
            text="Проход #2:",
            text_color=("#5dade2", "#5dade2"),
            font=ctk.CTkFont(size=12, weight="bold"),
        ).grid(row=0, column=0, padx=(0, 8), sticky="w")
        self.pass2_provider_switch = ctk.CTkSegmentedButton(
            self.pass2_row,
            values=self.provider_labels,
            variable=self.pass2_provider_label_var,
            command=self._on_pass2_provider_changed,
        )
        self.pass2_provider_switch.grid(row=0, column=1, padx=(0, 18), sticky="w")
        ctk.CTkLabel(self.pass2_row, text="Модель #2:").grid(
            row=0, column=2, padx=(0, 8), sticky="w"
        )
        self.pass2_model_combo = ctk.CTkComboBox(
            self.pass2_row,
            values=self._provider_models[self._pass2_pid],
            variable=self.pass2_model_var,
            command=lambda val: self.pass2_model_var.set(str(val or "").strip()),
        )
        self.pass2_model_combo.grid(row=0, column=3, sticky="ew")

        # Строка 4: независимый Проход #3 (показывается при 3 проходах)
        self.pass3_row = ctk.CTkFrame(settings, fg_color="transparent")
        self.pass3_row.grid(row=4, column=0, sticky="ew", padx=14, pady=(2, 4))
        self.pass3_row.grid_columnconfigure(3, weight=1)
        ctk.CTkLabel(
            self.pass3_row,
            text="Проход #3:",
            text_color=("#48c9b0", "#48c9b0"),
            font=ctk.CTkFont(size=12, weight="bold"),
        ).grid(row=0, column=0, padx=(0, 8), sticky="w")
        self.pass3_provider_switch = ctk.CTkSegmentedButton(
            self.pass3_row,
            values=self.provider_labels,
            variable=self.pass3_provider_label_var,
            command=self._on_pass3_provider_changed,
        )
        self.pass3_provider_switch.grid(row=0, column=1, padx=(0, 18), sticky="w")
        ctk.CTkLabel(self.pass3_row, text="Модель #3:").grid(
            row=0, column=2, padx=(0, 8), sticky="w"
        )
        self.pass3_model_combo = ctk.CTkComboBox(
            self.pass3_row,
            values=self._provider_models[self._pass3_pid],
            variable=self.pass3_model_var,
            command=lambda val: self.pass3_model_var.set(str(val or "").strip()),
        )
        self.pass3_model_combo.grid(row=0, column=3, sticky="ew")

        # Строка 5: поля ввода API-ключей каждого из трёх провайдеров
        keys_frame = ctk.CTkFrame(settings, fg_color="transparent")
        keys_frame.grid(row=5, column=0, sticky="ew", padx=14, pady=(4, 6))
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

        # Строка 6: защита ключей от кражи .json / .env (пересчёт реального ключа в RAM)
        vault_row = ctk.CTkFrame(settings, fg_color="transparent")
        vault_row.grid(row=6, column=0, sticky="ew", padx=14, pady=(2, 10))
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
        toolbar.grid_columnconfigure(3, weight=1)
        ctk.CTkLabel(
            toolbar, text="Legal Description", font=ctk.CTkFont(size=14, weight="bold")
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(toolbar, text="Загрузить PDF", width=118, command=self._load_pdf).grid(
            row=0, column=1, padx=(10, 0)
        )
        self.batch_neighbors_button = ctk.CTkButton(
            toolbar,
            text="Пакет соседей…",
            width=132,
            fg_color=("#1b6b7a", "#1b6b7a"),
            hover_color=("#238799", "#238799"),
            command=self._load_and_draw_neighbor_files,
        )
        self.batch_neighbors_button.grid(row=0, column=2, padx=(6, 0))
        self._sample_presets: dict[str, str] = {
            "Базовый пример (TX)": EXAMPLE_TEXT,
            **NYC_SAMPLES,
            **ASPENWALL_SAMPLES,
        }
        self.sample_var = ctk.StringVar(value="Базовый пример (TX)")
        self.sample_menu = ctk.CTkOptionMenu(
            toolbar,
            values=list(self._sample_presets.keys()),
            variable=self.sample_var,
            width=215,
            command=self._on_sample_selected,
        )
        self.sample_menu.grid(row=0, column=4, padx=(8, 0))
        ctk.CTkButton(toolbar, text="Пример", width=80, command=self._insert_example).grid(
            row=0, column=5, padx=(6, 0)
        )
        ctk.CTkButton(toolbar, text="Очистить", width=80, command=self._clear_text).grid(
            row=0, column=6, padx=(6, 0)
        )
        self.pdf_label = ctk.CTkLabel(
            toolbar, text="PDF не загружен", text_color=("#8fa3b8", "#8fa3b8")
        )
        self.pdf_label.grid(row=0, column=3, sticky="e", padx=10)

        # Переключатель режима: Основной участок vs Соседний участок (NEIGHBOR_LOT)
        role_bar = ctk.CTkFrame(input_frame, fg_color="transparent")
        role_bar.grid(row=1, column=0, sticky="ew", padx=10, pady=(2, 6))
        self.lot_role_var = ctk.StringVar(value="main")
        self.main_lot_radio = ctk.CTkRadioButton(
            role_bar,
            text="Основной участок (с подписями границ)",
            variable=self.lot_role_var,
            value="main",
            command=self._on_lot_role_changed,
        )
        self.main_lot_radio.grid(row=0, column=0, sticky="w", padx=(0, 18))
        self.neighbor_lot_radio = ctk.CTkRadioButton(
            role_bar,
            text="Соседний участок (только контур в слое NEIGHBOR_LOT)",
            variable=self.lot_role_var,
            value="neighbor",
            command=self._on_lot_role_changed,
        )
        self.neighbor_lot_radio.grid(row=0, column=1, sticky="w")

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
            width=175,
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
        self.draw_button.grid(row=0, column=1, padx=(10, 6), sticky="ew")
        self.add_neighbor_button = ctk.CTkButton(
            actions,
            text="Добавить соседа на чертёж",
            height=42,
            width=215,
            fg_color=("#1b6b7a", "#1b6b7a"),
            hover_color=("#238799", "#238799"),
            font=ctk.CTkFont(size=13, weight="bold"),
            command=lambda: self._start(parse_only=False, force_neighbor=True),
        )
        self.add_neighbor_button.grid(row=0, column=2, padx=(6, 10), sticky="e")
        self.save_button = ctk.CTkButton(
            actions, text="Сохранить настройки", width=165, command=self._save_settings
        )
        self.save_button.grid(row=0, column=3, sticky="e")

        self.progress = ctk.CTkProgressBar(self, mode="indeterminate")
        self.progress.grid(row=4, column=0, sticky="ew", padx=16, pady=(8, 0))
        self.progress.set(0)

        # Панель разрешения конфликта между проходами LLM (показывается при расхождении)
        self.conflict_bar = ctk.CTkFrame(self, fg_color=("#3d2b1f", "#3d2b1f"))
        self.conflict_bar.grid(row=5, column=0, sticky="ew", padx=16, pady=(8, 0))
        self.conflict_bar.grid_columnconfigure(0, weight=1)

        self.conflict_title_var = ctk.StringVar(
            value="⚠ Обнаружено расхождение между проходами LLM!"
        )
        self.conflict_title_label = ctk.CTkLabel(
            self.conflict_bar,
            textvariable=self.conflict_title_var,
            anchor="w",
            justify="left",
            wraplength=1010,
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=("#ffcc80", "#ffcc80"),
        )
        self.conflict_title_label.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 4))

        conflict_btns = ctk.CTkFrame(self.conflict_bar, fg_color="transparent")
        conflict_btns.grid(row=1, column=0, sticky="ew", padx=12, pady=(2, 8))

        self.retry_conflict_button = ctk.CTkButton(
            conflict_btns,
            text="🔄 Попробуй разобраться ещё раз",
            fg_color=("#c06c1a", "#c06c1a"),
            hover_color=("#db7f23", "#db7f23"),
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self.retry_conflict_resolution,
        )
        self.retry_conflict_button.grid(row=0, column=0, padx=(0, 8), sticky="w")

        self.pick_pass1_button = ctk.CTkButton(
            conflict_btns,
            text="Прав Проход #1",
            width=155,
            fg_color=("#246b4b", "#246b4b"),
            hover_color=("#2d865e", "#2d865e"),
            command=lambda: self.resolve_conflict_with_pass(1),
        )
        self.pick_pass1_button.grid(row=0, column=1, padx=(0, 6), sticky="w")

        self.pick_pass2_button = ctk.CTkButton(
            conflict_btns,
            text="Прав Проход #2",
            width=155,
            fg_color=("#246b4b", "#246b4b"),
            hover_color=("#2d865e", "#2d865e"),
            command=lambda: self.resolve_conflict_with_pass(2),
        )
        self.pick_pass2_button.grid(row=0, column=2, padx=(0, 6), sticky="w")

        self.pick_pass3_button = ctk.CTkButton(
            conflict_btns,
            text="Прав Проход #3",
            width=155,
            fg_color=("#246b4b", "#246b4b"),
            hover_color=("#2d865e", "#2d865e"),
            command=lambda: self.resolve_conflict_with_pass(3),
        )
        self.pick_pass3_button.grid(row=0, column=3, padx=(0, 8), sticky="w")

        self.open_conflict_details_button = ctk.CTkButton(
            conflict_btns,
            text="Подробное сравнение…",
            width=170,
            command=self._open_conflict_dialog,
        )
        self.open_conflict_details_button.grid(row=0, column=4, sticky="w")
        self.conflict_bar.grid_remove()

        # Статус, невязка и токены ------------------------------------------
        status_frame = ctk.CTkFrame(self)
        status_frame.grid(row=6, column=0, sticky="ew", padx=16, pady=(10, 14))
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

    def current_passes_count(self) -> int:
        """Возвращает выбранное число независимых проходов LLM: 1, 2 или 3."""
        raw = str(self.passes_label_var.get() or "1 проход").strip()
        for ch in raw:
            if ch in ("1", "2", "3"):
                return int(ch)
        return 1

    def select_passes(self, count: int) -> None:
        """Программно переключает режим 1 / 2 / 3 проходов LLM."""
        clamped = max(1, min(3, int(count)))
        label = self.passes_options[clamped - 1]
        self.passes_label_var.set(label)
        self.passes_switch.set(label)
        self._on_passes_changed(label)

    def _on_passes_changed(self, _selected_label: str = "") -> None:
        self._refresh_consensus_rows()

    def _refresh_consensus_rows(self) -> None:
        count = self.current_passes_count()
        if count >= 2:
            self.pass2_row.grid(row=3, column=0, sticky="ew", padx=14, pady=(2, 4))
        else:
            self.pass2_row.grid_remove()
        if count >= 3:
            self.pass3_row.grid(row=4, column=0, sticky="ew", padx=14, pady=(2, 4))
        else:
            self.pass3_row.grid_remove()

    def current_pass2_provider_id(self) -> str:
        lbl = str(self.pass2_provider_label_var.get() or "")
        return self._label_to_pid.get(lbl, self._pass2_pid)

    def current_pass2_model(self) -> str:
        typed = str(self.pass2_model_var.get() or "").strip()
        if not typed:
            typed = str(self.pass2_model_combo.get() or "").strip()
        pid = self.current_pass2_provider_id()
        return typed or self._selected_models.get(pid) or self._provider_models[pid][0]

    def current_pass3_provider_id(self) -> str:
        lbl = str(self.pass3_provider_label_var.get() or "")
        return self._label_to_pid.get(lbl, self._pass3_pid)

    def current_pass3_model(self) -> str:
        typed = str(self.pass3_model_var.get() or "").strip()
        if not typed:
            typed = str(self.pass3_model_combo.get() or "").strip()
        pid = self.current_pass3_provider_id()
        return typed or self._selected_models.get(pid) or self._provider_models[pid][0]

    def _on_pass2_provider_changed(self, selected_label: str) -> None:
        new_pid = self._label_to_pid.get(selected_label)
        if not new_pid:
            return
        self._pass2_pid = new_pid
        models = self._provider_models[new_pid]
        chosen = self._selected_models.get(new_pid) or models[0]
        self.pass2_model_combo.configure(values=models)
        self.pass2_model_var.set(chosen)
        self.pass2_model_combo.set(chosen)

    def _on_pass3_provider_changed(self, selected_label: str) -> None:
        new_pid = self._label_to_pid.get(selected_label)
        if not new_pid:
            return
        self._pass3_pid = new_pid
        models = self._provider_models[new_pid]
        chosen = self._selected_models.get(new_pid) or models[0]
        self.pass3_model_combo.configure(values=models)
        self.pass3_model_var.set(chosen)
        self.pass3_model_combo.set(chosen)

    def select_pass2_target(
        self, provider_or_label: str, model: str | None = None
    ) -> None:
        cand = provider_or_label.strip()
        pid = self._label_to_pid.get(cand) or cand.lower()
        if pid not in self._pid_to_label:
            raise ValueError(f"неизвестный провайдер: {provider_or_label!r}")
        lbl = self._pid_to_label[pid]
        self.pass2_provider_label_var.set(lbl)
        self.pass2_provider_switch.set(lbl)
        self._on_pass2_provider_changed(lbl)
        if model and model.strip():
            m = model.strip()
            self.pass2_model_var.set(m)
            self.pass2_model_combo.set(m)

    def select_pass3_target(
        self, provider_or_label: str, model: str | None = None
    ) -> None:
        cand = provider_or_label.strip()
        pid = self._label_to_pid.get(cand) or cand.lower()
        if pid not in self._pid_to_label:
            raise ValueError(f"неизвестный провайдер: {provider_or_label!r}")
        lbl = self._pid_to_label[pid]
        self.pass3_provider_label_var.set(lbl)
        self.pass3_provider_switch.set(lbl)
        self._on_pass3_provider_changed(lbl)
        if model and model.strip():
            m = model.strip()
            self.pass3_model_var.set(m)
            self.pass3_model_combo.set(m)

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

    def is_neighbor_mode(self) -> bool:
        """True, если в переключателе выбран режим «Соседний участок»."""
        return str(self.lot_role_var.get() or "main") == "neighbor"

    def _on_lot_role_changed(self) -> None:
        if self.is_neighbor_mode():
            self.draw_button.configure(text="Начертить соседа в AutoCAD")
        else:
            self.draw_button.configure(text="Начертить участок в AutoCAD")

    def _on_sample_selected(self, label: str) -> None:
        sample = self._sample_presets.get(label)
        if not sample:
            return
        self.text_box.delete("1.0", "end")
        self.text_box.insert("1.0", sample)
        self.pdf_path = None
        self._pdf_needs_vision = False
        if "Сосед" in label:
            self.lot_role_var.set("neighbor")
        elif "Основной" in label:
            self.lot_role_var.set("main")
        self._on_lot_role_changed()
        self.pdf_label.configure(
            text=f"Пример: {label}",
            text_color=("#8fa3b8", "#8fa3b8"),
        )
        self._set_status(f"Подставлен пример: {label}")

    def _insert_example(self) -> None:
        selected = str(self.sample_var.get() or "")
        sample = self._sample_presets.get(selected, EXAMPLE_TEXT)
        self.text_box.delete("1.0", "end")
        self.text_box.insert("1.0", sample)
        self.pdf_path = None
        self._pdf_needs_vision = False
        self.pdf_label.configure(
            text="Пример из буфера (PDF не загружен)",
            text_color=("#8fa3b8", "#8fa3b8"),
        )

    def _clear_text(self) -> None:
        self.text_box.delete("1.0", "end")
        self.pdf_path = None
        self._pdf_needs_vision = False
        self.pdf_label.configure(
            text="PDF не загружен",
            text_color=("#8fa3b8", "#8fa3b8"),
        )
        self.description = None
        self.draw_result = None
        self.closure_var.set("Closure Error: —")
        self.tokens_var.set("Токены за вызов: —")
        self.status_var.set("Очищено.")

    def _load_pdf(self) -> None:
        from tkinter import filedialog

        path = filedialog.askopenfilename(
            title="Выберите PDF с Legal Description или чертёж Survey",
            filetypes=[("PDF", "*.pdf"), ("Все файлы", "*.*")],
        )
        if not path:
            return
        try:
            info = inspect_pdf(path)
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"Не удалось прочитать PDF: {exc}", error=True)
            return
        self.pdf_path = Path(path)
        has_legal_text = bool(info.get("has_legal_text", info["has_text"]))
        if has_legal_text:
            text = str(info["text"])
            self._pdf_needs_vision = False
            self.text_box.delete("1.0", "end")
            self.text_box.insert("1.0", text)
            self.pdf_label.configure(
                text=f"PDF: {self.pdf_path.name} ({len(text):,} симв.)",
                text_color=("#8fa3b8", "#8fa3b8"),
            )
            self._set_status(f"PDF загружен: {self.pdf_path.name}")
        else:
            # Обязательно очищаем поле текста от предыдущего примера/документа,
            # чтобы чужое описание не примешивалось к Vision-запросу нового PDF.
            self.text_box.delete("1.0", "end")
            self._pdf_needs_vision = True
            self.use_llm_var.set(True)
            tag_detail = "чертёж Survey" if info["has_text"] else "без текста"
            self.pdf_label.configure(
                text=f"PDF Vision: {self.pdf_path.name} ({info['pages']} стр., {tag_detail})",
                text_color=("#f5b041", "#f5b041"),
            )
            self._set_status(
                f"Загружен чертёж/скан PDF ({self.pdf_path.name}, "
                f"{info['pages']} стр.) — включён режим Vision LLM. "
                "Выберите мультимодальную модель (hydra-gemini-pro, "
                "nvidia/nemotron-3.5-super-vl:free или openai/gpt-4.1)."
            )

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

        p2_pid = self.current_pass2_provider_id()
        p2_model = self.current_pass2_model()
        p3_pid = self.current_pass3_provider_id()
        p3_model = self.current_pass3_model()
        for extra_pid, extra_m in ((p2_pid, p2_model), (p3_pid, p3_model)):
            if extra_pid in providers and extra_m:
                m_list = [str(m) for m in (providers[extra_pid].get("models") or [])]
                if extra_m not in m_list:
                    m_list.insert(0, extra_m)
                    providers[extra_pid]["models"] = m_list
        cfg["api"]["consensus"] = {
            "passes": self.current_passes_count(),
            "pass2_provider": p2_pid,
            "pass2_model": p2_model,
            "pass3_provider": p3_pid,
            "pass3_model": p3_model,
        }

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
                self._dismiss_conflict_ui()
                if len(payload) == 4:
                    outcome, draw, pre_counted, round_usages = payload
                    if round_usages:
                        self._record_round_usages(round_usages)
                elif len(payload) == 3:
                    outcome, draw, pre_counted = payload
                else:
                    outcome, draw = payload
                    pre_counted = False
                if draw:
                    self._handle_draw_result(outcome, draw, already_counted=pre_counted)
                else:
                    self._finish_busy()
                    self._apply_parsed(outcome, already_counted=pre_counted)
                    self._set_status(self._parsed_summary(outcome))
                    self.closure_var.set(self._closure_text(outcome))
            elif kind == "conflict":
                self._finish_busy()
                self._present_conflict(payload)
            elif kind == "batch_neighbors":
                items_done, batch_errors = payload
                self._finish_busy()
                self.batch_neighbor_results = items_done
                for out, res in items_done:
                    self._apply_parsed(out)
                    if res is not None:
                        self.draw_result = res
                msg_parts = [
                    f"Пакет соседей: обработано {len(items_done)} уч. в слое NEIGHBOR_LOT"
                ]
                if batch_errors:
                    msg_parts.append(f"ошибок: {len(batch_errors)} (см. лог)")
                    for err_line in batch_errors:
                        self._log_line(f"! {err_line}")
                self._set_status("  |  ".join(msg_parts), error=bool(batch_errors and not items_done))
                if items_done:
                    last_out, last_res = items_done[-1]
                    self.closure_var.set(
                        last_res.closure.short_report()
                        if last_res is not None
                        else self._closure_text(last_out)
                    )
            elif kind == "done":
                self._finish_busy()
        self.after(120, self._drain_events)

    def _set_controls_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.draw_button.configure(state=state)
        self.add_neighbor_button.configure(state=state)
        self.batch_neighbors_button.configure(state=state)
        self.parse_button.configure(state=state)
        self.save_button.configure(state=state)
        self.save_env_button.configure(state=state)

    def _finish_busy(self) -> None:
        self._busy = False
        self.progress.stop()
        self.progress.set(0)
        self._set_controls_busy(False)

    def _load_and_draw_neighbor_files(self) -> None:
        """Пакетная загрузка нескольких PDF/TXT соседних участков и дорисовка в AutoCAD от (0, 0)."""
        if self._busy:
            return
        from tkinter import filedialog

        paths = filedialog.askopenfilenames(
            title="Выберите файлы соседних участков (PDF или TXT) для слоя NEIGHBOR_LOT",
            filetypes=[
                ("PDF и текстовые описания", "*.pdf *.txt"),
                ("PDF", "*.pdf"),
                ("Текст", "*.txt"),
                ("Все файлы", "*.*"),
            ],
        )
        if not paths:
            return
        file_list = [Path(p) for p in paths if str(p).strip()]
        if not file_list:
            return

        cfg, provider_id, model_name, api_key, key_secret = self._build_runtime_config()
        use_llm = bool(self.use_llm_var.get())
        scale = self._current_scale()
        provider_title = self._pid_to_label.get(provider_id, provider_id)

        self._busy = True
        self._set_controls_busy(True)
        self.progress.start()
        self._set_status(
            f"Пакетная отрисовка соседей: выбрано файлов — {len(file_list)}…"
        )
        self._log_line(
            f"--- Пакет соседей ({len(file_list)} файл., масштаб 1\"={scale:g}', "
            f"режим={'LLM: ' + provider_title + ' / ' + model_name if use_llm else 'regex'}) ---"
        )
        worker = threading.Thread(
            target=self._batch_neighbors_worker,
            args=(
                file_list,
                cfg,
                provider_id,
                model_name,
                api_key,
                key_secret,
                use_llm,
                scale,
            ),
            daemon=True,
        )
        worker.start()

    def _batch_neighbors_worker(
        self,
        file_list: list[Path],
        cfg: dict[str, Any],
        provider_id: str,
        model_name: str,
        api_key: str | None,
        key_secret: str,
        use_llm: bool,
        scale: float,
    ) -> None:
        done_items: list[tuple[ParseOutcome, DrawResult | None]] = []
        errors: list[str] = []
        builder = AcadBuilder(cfg)
        total = len(file_list)
        for idx, fpath in enumerate(file_list, start=1):
            try:
                self._post(
                    "progress",
                    f"[{idx}/{total}] Разбираю соседний участок: {fpath.name}…",
                )
                if fpath.suffix.lower() == ".pdf":
                    outcome = parse_legal_description_full(
                        pdf_path=fpath,
                        cfg=cfg,
                        use_llm=use_llm,
                        provider=provider_id,
                        model=model_name,
                        api_key=api_key,
                        key_secret=key_secret,
                        allow_regex_fallback=False,
                        progress=lambda msg, i=idx: self._post("progress", f"[{i}/{total}] {msg}"),
                    )
                else:
                    raw_txt = fpath.read_text(encoding="utf-8", errors="replace")
                    outcome = parse_legal_description_full(
                        text=raw_txt,
                        cfg=cfg,
                        use_llm=use_llm,
                        provider=provider_id,
                        model=model_name,
                        api_key=api_key,
                        key_secret=key_secret,
                        allow_regex_fallback=False,
                        progress=lambda msg, i=idx: self._post("progress", f"[{i}/{total}] {msg}"),
                    )
                if use_llm:
                    self._last_valid_secret = key_secret

                if sys.platform != "win32":
                    raise AcadConnectionError(
                        "Отрисовка возможна только в Windows с запущенным AutoCAD "
                        f"(текущая ОС: {sys.platform})"
                    )
                self._post(
                    "progress",
                    f"[{idx}/{total}] Рисую соседа «{fpath.name}» в слое NEIGHBOR_LOT…",
                )
                draw_res = builder.draw(
                    outcome.data, scale=scale, is_neighbor=True
                )
                done_items.append((outcome, draw_res))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{fpath.name}: {exc}")
        self._post("batch_neighbors", (done_items, errors))
        self._post("done", None)

    def _start(self, parse_only: bool, *, force_neighbor: bool = False) -> None:
        if self._busy:
            return
        text = self.text_box.get("1.0", "end").strip()
        if not text and not self.pdf_path:
            self._set_status("Вставьте текст Legal Description или загрузите PDF.", error=True)
            return

        self._dismiss_conflict_ui()
        cfg, provider_id, model_name, api_key, key_secret = self._build_runtime_config()
        use_llm = bool(self.use_llm_var.get())
        passes_count = self.current_passes_count() if use_llm else 1
        scale = self._current_scale()
        is_neighbor = bool(force_neighbor or self.is_neighbor_mode())
        provider_title = self._pid_to_label.get(provider_id, provider_id)

        self._busy = True
        self._set_controls_busy(True)
        self.progress.start()
        if use_llm:
            if passes_count >= 2:
                self.tokens_var.set(
                    f"Токены за вызов: параллельный запрос ({passes_count} независимых прохода LLM)…"
                )
            else:
                self.tokens_var.set(
                    f"Токены за вызов: запрос к {provider_title} / {model_name}…"
                )
        role_note = "соседний участок (NEIGHBOR_LOT)" if is_neighbor else "основной участок"
        mode_desc = (
            f"LLM ({passes_count} прох., базовый: {provider_title} / {model_name})"
            if (use_llm and passes_count >= 2)
            else (f"LLM: {provider_title} / {model_name}" if use_llm else "regex")
        )
        self._set_status(f"Разбираю описание ({role_note}, {mode_desc})…")
        self._log_line(
            f"--- Новый запуск ({role_note}, масштаб 1\"={scale:g}', режим={mode_desc}) ---"
        )

        vision_pdf = self.pdf_path if self._pdf_needs_vision else None
        worker = threading.Thread(
            target=self._worker,
            args=(
                text,
                vision_pdf,
                cfg,
                provider_id,
                model_name,
                api_key,
                key_secret,
                use_llm,
                scale,
                parse_only,
                is_neighbor,
            ),
            daemon=True,
        )
        worker.start()

    def _worker(
        self,
        text: str,
        vision_pdf: Path | None,
        cfg: dict[str, Any],
        provider_id: str,
        model_name: str,
        api_key: str | None,
        key_secret: str,
        use_llm: bool,
        scale: float,
        parse_only: bool,
        is_neighbor: bool = False,
    ) -> None:
        try:
            passes_count = (
                int((cfg.get("api", {}).get("consensus") or {}).get("passes") or 1)
                if use_llm
                else 1
            )
            if use_llm and passes_count >= 2:
                consensus = run_multipass_consensus(
                    text=text,
                    pdf_path=vision_pdf,
                    cfg=cfg,
                    passes=passes_count,
                    primary_provider=provider_id,
                    primary_model=model_name,
                    primary_api_key=api_key,
                    key_secret=key_secret,
                    progress=lambda message: self._post("progress", message),
                )
                self._last_valid_secret = key_secret
                if not consensus.agreed:
                    self._post(
                        "conflict",
                        {
                            "consensus": consensus,
                            "cfg": cfg,
                            "key_secret": key_secret,
                            "scale": scale,
                            "parse_only": parse_only,
                            "is_neighbor": is_neighbor,
                        },
                    )
                    self._post("done", None)
                    return
                outcome = consensus.selected_outcome
                round_usages: list[TokenUsage | None] = [
                    o.usage for o in consensus.pass_outcomes
                ]
                pre_counted = True
            else:
                outcome = parse_legal_description_full(
                    text=text,
                    pdf_path=vision_pdf,
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
                round_usages = []
                pre_counted = False

            builder = AcadBuilder(cfg)
            plan = builder.plan(
                outcome.data, scale=scale, is_neighbor=is_neighbor
            )
            self._post(
                "progress",
                f"Готово ({outcome.source}): сторон {len(outcome.data.segments)}, "
                f"POB ({plan.pob[0]:+.2f}, {plan.pob[1]:+.2f}) ft, "
                f"Closure Error {plan.closure.distance:.3f} ft",
            )
            if parse_only:
                self._post("parsed", (outcome, None, pre_counted, round_usages))
                self._post("done", None)
                return

            self._post(
                "progress",
                "Добавляю соседа в AutoCAD (слой NEIGHBOR_LOT)…"
                if is_neighbor
                else "Рисую основной участок в AutoCAD…",
            )
            if sys.platform != "win32":
                raise AcadConnectionError(
                    "Отрисовка возможна только в Windows с запущенным AutoCAD "
                    f"(текущая ОС: {sys.platform})"
                )
            result = builder.draw(
                outcome.data, scale=scale, is_neighbor=is_neighbor
            )
            self._post("parsed", (outcome, result, pre_counted, round_usages))
            self._post("done", None)
        except KeyProtectionError as exc:
            self._post("error", str(exc))
            self._post("done", None)
        except LLMParseError as exc:
            message = str(exc)
            if vision_pdf is not None:
                message += (
                    "\n\nПодсказка: проверьте API-ключ выбранного провайдера и убедитесь, "
                    "что выбрана мультимодальная Vision-модель (hydra-gemini-pro, "
                    "nvidia/nemotron-3.5-super-vl:free или openai/gpt-4.1)."
                )
            elif use_llm:
                message += (
                    "\n\nПодсказка: проверьте API-ключ выбранного провайдера "
                    "или переключите провайдера/модель, либо выключите «Использовать LLM» "
                    "для локального разбора (regex)."
                )
            self._post("error", message)
            self._post("done", None)
        except (LegalDescriptionError, AcadBuilderError) as exc:
            self._post("error", str(exc))
            self._post("done", None)
        except Exception as exc:  # noqa: BLE001
            detail = traceback.format_exc(limit=6)
            self._post("error", f"Непредвиденная ошибка: {exc}\n{detail}")
            self._post("done", None)

    # ---------------------------------------- разрешение конфликта проходов --
    def _record_round_usages(self, usages: list[TokenUsage | None]) -> None:
        for u in usages:
            if u is None:
                continue
            self._session_llm_calls += 1
            if u.available:
                self._session_prompt_tokens += u.prompt_tokens
                self._session_completion_tokens += u.completion_tokens
            self._log_line(
                f"[Токены прохода] {u.provider_label or u.provider} / {u.model}: "
                f"ушло {u.prompt_tokens:,}, пришло {u.completion_tokens:,}, "
                f"всего {u.total_tokens:,}"
            )

    def _dismiss_conflict_ui(self) -> None:
        self.pending_conflict = None
        try:
            self.conflict_bar.grid_remove()
        except Exception:  # noqa: BLE001
            pass
        win = getattr(self, "conflict_window", None)
        self.conflict_window = None
        if win is not None:
            try:
                win.destroy()
            except Exception:  # noqa: BLE001
                pass

    def _present_conflict(self, conflict_ctx: dict[str, Any]) -> None:
        consensus: ConsensusResult = conflict_ctx["consensus"]
        self.pending_conflict = conflict_ctx
        self._record_round_usages([o.usage for o in consensus.pass_outcomes])
        self.last_usage = consensus.total_usage
        self.tokens_var.set(self._format_tokens_line(consensus.selected_outcome))

        n_passes = len(consensus.pass_outcomes)
        labels = consensus.pass_labels_map()
        short_diffs = "  |  ".join(
            d.summary(labels) for d in consensus.discrepancies[:2]
        )
        if len(consensus.discrepancies) > 2:
            short_diffs += f"  (+ещё {len(consensus.discrepancies) - 2})"
        self.conflict_title_var.set(
            f"⚠ Ответы {n_passes} нейросетей разошлись (раунд #{consensus.round_number}): "
            f"{short_diffs}"
        )
        self.pick_pass1_button.configure(
            text=f"✅ Прав Проход #1 ({consensus.pass_outcomes[0].model})"
        )
        if n_passes >= 2:
            self.pick_pass2_button.configure(
                text=f"✅ Прав Проход #2 ({consensus.pass_outcomes[1].model})"
            )
            self.pick_pass2_button.grid(row=0, column=2, padx=(0, 6), sticky="w")
        if n_passes >= 3:
            self.pick_pass3_button.configure(
                text=f"✅ Прав Проход #3 ({consensus.pass_outcomes[2].model})"
            )
            self.pick_pass3_button.grid(row=0, column=3, padx=(0, 8), sticky="w")
        else:
            self.pick_pass3_button.grid_remove()

        self.conflict_bar.grid(row=5, column=0, sticky="ew", padx=16, pady=(8, 0))
        self.closure_var.set(
            "Closure Error по проходам:  "
            + "   |   ".join(
                f"#{i}: {consensus.pass_closure_ft(i):.4f} ft"
                for i in range(1, n_passes + 1)
            )
        )
        self._set_status(consensus.discrepancy_report(), error=True)
        self._open_conflict_dialog()

    def _open_conflict_dialog(self) -> None:
        """Открывает/обновляет окно детального сравнения ответов моделей и выбора решения."""
        if not self.pending_conflict:
            return
        consensus: ConsensusResult = self.pending_conflict["consensus"]
        old_win = getattr(self, "conflict_window", None)
        if old_win is not None:
            try:
                old_win.destroy()
            except Exception:  # noqa: BLE001
                pass

        win = ctk.CTkToplevel(self)
        win.title(f"Сверка ответов LLM — раунд #{consensus.round_number}")
        win.geometry("860x560")
        win.attributes("-topmost", True)
        self.conflict_window = win

        ctk.CTkLabel(
            win,
            text=(
                f"Ответы {len(consensus.pass_outcomes)} независимых проходов разошлись "
                f"(раунд #{consensus.round_number})"
            ),
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=("#ffcc80", "#ffcc80"),
        ).pack(anchor="w", padx=14, pady=(12, 4))

        ctk.CTkLabel(
            win,
            text=(
                "Проверьте спорный фрагмент в документе и укажите, какая модель права, "
                "или нажмите «Попробуй разобраться ещё раз» — модели получат задание "
                "прицельно перепроверить именно спорные места."
            ),
            anchor="w",
            justify="left",
            wraplength=820,
            font=ctk.CTkFont(size=12),
        ).pack(anchor="w", padx=14, pady=(0, 8))

        box = ctk.CTkTextbox(win, wrap="word", font=ctk.CTkFont(family="Consolas", size=12))
        box.pack(fill="both", expand=True, padx=14, pady=(0, 10))
        box.insert("1.0", consensus.discrepancy_report())
        target_tb = getattr(box, "_textbox", box)
        target_tb.configure(state="disabled")

        btn_bar = ctk.CTkFrame(win, fg_color="transparent")
        btn_bar.pack(fill="x", padx=14, pady=(0, 12))

        ctk.CTkButton(
            btn_bar,
            text="🔄 Попробуй разобраться ещё раз",
            fg_color=("#c06c1a", "#c06c1a"),
            hover_color=("#db7f23", "#db7f23"),
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self.retry_conflict_resolution,
        ).pack(side="left", padx=(0, 10))

        for p_idx in range(1, len(consensus.pass_outcomes) + 1):
            err_ft = consensus.pass_closure_ft(p_idx)
            mdl = consensus.pass_outcomes[p_idx - 1].model
            ctk.CTkButton(
                btn_bar,
                text=f"✅ Прав Проход #{p_idx} ({mdl}, {err_ft:.3f}')",
                fg_color=("#246b4b", "#246b4b"),
                hover_color=("#2d865e", "#2d865e"),
                command=lambda idx=p_idx: self.resolve_conflict_with_pass(idx),
            ).pack(side="left", padx=(0, 6))

    def resolve_conflict_with_pass(self, pass_index: int) -> None:
        """Человек вручную выбирает, какой из проходов (#1, #2 или #3) прав."""
        if self._busy or not self.pending_conflict:
            return
        ctx = dict(self.pending_conflict)
        consensus: ConsensusResult = ctx["consensus"]
        outcome = consensus.resolve_with_pass(pass_index)
        self._dismiss_conflict_ui()
        self._complete_resolved_outcome(outcome, ctx, pre_counted=True)

    def retry_conflict_resolution(self) -> None:
        """Кнопка «Попробуй разобраться ещё раз»: повторный вызов с фокусом на спорных местах."""
        if self._busy or not self.pending_conflict:
            return
        ctx = dict(self.pending_conflict)
        consensus: ConsensusResult = ctx["consensus"]
        next_round = consensus.round_number + 1

        self._busy = True
        self._set_controls_busy(True)
        self.progress.start()
        self._set_status(
            f"Повторная проверка спорных мест (раунд #{next_round}): "
            "отправлен прицельный запрос моделям…"
        )
        self._log_line(
            f"--- Повторный арбитраж спорных мест (раунд #{next_round}, "
            f"проходов: {len(consensus.pass_outcomes)}) ---"
        )
        worker = threading.Thread(
            target=self._retry_conflict_worker,
            args=(ctx,),
            daemon=True,
        )
        worker.start()

    def _retry_conflict_worker(self, ctx: dict[str, Any]) -> None:
        consensus: ConsensusResult = ctx["consensus"]
        cfg: dict[str, Any] = ctx["cfg"]
        key_secret: str = ctx["key_secret"]
        scale: float = ctx["scale"]
        parse_only: bool = ctx["parse_only"]
        is_neighbor: bool = ctx["is_neighbor"]
        try:
            new_consensus = recheck_consensus_discrepancies(
                consensus,
                cfg,
                key_secret=key_secret,
                progress=lambda msg: self._post("progress", msg),
            )
            if not new_consensus.agreed:
                updated_ctx = dict(ctx)
                updated_ctx["consensus"] = new_consensus
                self._post("conflict", updated_ctx)
                self._post("done", None)
                return

            # Модели пришли к единому мнению!
            round_usages = [o.usage for o in new_consensus.pass_outcomes]
            self._post("progress", "✓ Модели сошлись во мнении! Завершаю построение…")
            outcome = new_consensus.selected_outcome
            self._finalize_worker_outcome(
                outcome,
                cfg=cfg,
                scale=scale,
                parse_only=parse_only,
                is_neighbor=is_neighbor,
                pre_counted=True,
                round_usages=round_usages,
            )
        except Exception as exc:  # noqa: BLE001
            self._post("error", f"Ошибка повторного разбора спорных мест: {exc}")
            self._post("done", None)

    def _complete_resolved_outcome(
        self,
        outcome: ParseOutcome,
        ctx: dict[str, Any],
        *,
        pre_counted: bool = True,
    ) -> None:
        parse_only = bool(ctx["parse_only"])
        if parse_only:
            self._apply_parsed(outcome, already_counted=pre_counted)
            self._set_status(self._parsed_summary(outcome))
            self.closure_var.set(self._closure_text(outcome))
            return

        self._busy = True
        self._set_controls_busy(True)
        self.progress.start()
        worker = threading.Thread(
            target=self._finalize_worker_outcome,
            args=(outcome,),
            kwargs={
                "cfg": ctx["cfg"],
                "scale": ctx["scale"],
                "parse_only": False,
                "is_neighbor": ctx["is_neighbor"],
                "pre_counted": pre_counted,
                "round_usages": None,
            },
            daemon=True,
        )
        worker.start()

    def _finalize_worker_outcome(
        self,
        outcome: ParseOutcome,
        *,
        cfg: dict[str, Any],
        scale: float,
        parse_only: bool,
        is_neighbor: bool,
        pre_counted: bool = True,
        round_usages: list[TokenUsage | None] | None = None,
    ) -> None:
        try:
            builder = AcadBuilder(cfg)
            builder.plan(outcome.data, scale=scale, is_neighbor=is_neighbor)
            if parse_only:
                self._post("parsed", (outcome, None, pre_counted, round_usages or []))
                self._post("done", None)
                return
            if sys.platform != "win32":
                raise AcadConnectionError(
                    "Отрисовка возможна только в Windows с запущенным AutoCAD "
                    f"(текущая ОС: {sys.platform})"
                )
            result = builder.draw(outcome.data, scale=scale, is_neighbor=is_neighbor)
            self._post("parsed", (outcome, result, pre_counted, round_usages or []))
            self._post("done", None)
        except (LegalDescriptionError, AcadBuilderError) as exc:
            self._post("error", str(exc))
            self._post("done", None)
        except Exception as exc:  # noqa: BLE001
            detail = traceback.format_exc(limit=6)
            self._post("error", f"Непредвиденная ошибка: {exc}\n{detail}")
            self._post("done", None)

    # ------------------------------------------------------------- вывод --
    def _format_tokens_line(self, outcome: ParseOutcome) -> str:
        if outcome.source not in {"llm", "llm-vision"} or outcome.usage is None:
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

    def _apply_parsed(
        self, outcome: ParseOutcome, *, already_counted: bool = False
    ) -> None:
        self.description = outcome.data
        self.last_usage = outcome.usage
        if (
            not already_counted
            and outcome.source in {"llm", "llm-vision"}
            and outcome.usage is not None
        ):
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
        if outcome.source in {"llm", "llm-vision"}:
            prefix = "Vision LLM" if outcome.source == "llm-vision" else "LLM"
            tag = " · ".join(
                p for p in (outcome.provider_label or outcome.provider, outcome.model) if p
            )
            source = f"{prefix}: {tag}" if tag else prefix
        else:
            source = "локальный парсер"
        street_part = data.main_street or "—"
        if data.main_street_note:
            street_part += f" {data.main_street_note}"
        parts = [
            f"Разобрано ({source}): сторон {len(data.segments)}, "
            f"периметр {data.perimeter_ft:,.2f} ft",
            f"Фасад: {street_part}",
        ]
        if data.block_number or data.lot_number:
            bl = " ".join(
                p
                for p in (
                    f"Block {data.block_number}" if data.block_number else "",
                    f"Lot {data.lot_number}" if data.lot_number else "",
                )
                if p
            )
            if data.borough_or_city:
                bl += f" ({data.borough_or_city})"
            parts.append(bl)
        if data.cross_street:
            distance = data.distance_to_corner_ft
            if distance <= 1e-6:
                parts.append(f"Угол с {data.cross_street} (POB 0.00, 0.00)")
            else:
                pob_x, pob_y = compute_pob_point(data)
                tie_lbl = (
                    f" по {data.corner_to_pob_bearing.bearing_label}"
                    if data.corner_to_pob_bearing is not None
                    and data.corner_to_pob_bearing.has_bearing
                    else ""
                )
                parts.append(
                    f"Пересечение: {data.cross_street}, {distance:.2f} ft{tie_lbl} "
                    f"→ POB ({pob_x:+.2f}, {pob_y:+.2f})"
                )
        if outcome.warnings:
            parts.append(f"пояснений/исправлений: {len(outcome.warnings)}")
        header = "  |  ".join(parts)
        extra_lines: list[str] = [f"• {w}" for w in outcome.warnings]
        try:
            plan = AcadBuilder(self.config_data).plan(data, scale=self._current_scale())
            for info_note in plan.notes:
                extra_lines.append(f"• {info_note}")
                self._log_line(f"• {info_note}")
            for plan_warn in plan.warnings:
                extra_lines.append(f"! {plan_warn}")
        except Exception:  # noqa: BLE001
            pass
        if extra_lines:
            return header + "\n" + "\n".join(extra_lines)
        return header

    def _closure_text(self, outcome: ParseOutcome) -> str:
        try:
            builder = AcadBuilder(self.config_data)
            plan = builder.plan(outcome.data, scale=self._current_scale())
        except Exception:  # noqa: BLE001
            return "Closure Error: —"
        return plan.closure.short_report()

    def _handle_draw_result(
        self,
        outcome: ParseOutcome,
        result: DrawResult,
        *,
        already_counted: bool = False,
    ) -> None:
        self.draw_result = result
        self._apply_parsed(outcome, already_counted=already_counted)
        for info_note in result.plan.notes:
            self._log_line(f"• {info_note}")
        summary_text = result.summary()
        if outcome.warnings:
            summary_text += "\n" + "\n".join(f"• {w}" for w in outcome.warnings)
        self._set_status(summary_text)
        self.closure_var.set(result.closure.short_report())


def main() -> None:
    app = SurveyApp()
    app.mainloop()


if __name__ == "__main__":
    main()
