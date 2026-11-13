"""
main.py
=======
Графический интерфейс (CustomTkinter, Dark CAD Studio UI) для Title Survey AI:
Legal Description / PDF -> 1–3 прохода LLM -> предпросмотр схемы -> AutoCAD.

Ключевые элементы интерфейса:
  * компактная шапка с выбором масштаба чертежа (1"=10'/16'/20'/30'), переключателем
    «Поверх всех окон» и сворачиваемым ящиком API-ключей / PIN-защиты (tsai1:…);
  * панель ИИ-разбора с переключением провайдера (Hydra AI / OpenRouter / RouterAI),
    выбором модели и режимом сверки 1 / 2 / 3 независимых параллельных проходов LLM;
  * двухколоночная рабочая область:
      1) слева — загрузка PDF / выбор примера / переключатель роли участка
         (Основной PROPERTY_LINE vs Соседний NEIGHBOR_LOT, пакет соседей) и редактор текста;
      2) справа — живой векторный CAD-предпросмотр контура от перекрёстка (0, 0)
         (включая улицы, POB, соседей и точку открытого гэпа) + ведомость сторон;
  * встроенная панель и детальное окно разрешения конфликтов проходов LLM
    (выбор правой модели человеком или кнопка «🔄 Попробуй разобраться ещё раз»);
  * цветовая индикация невязки (Closure Error), счётчик токенов за вызов и за сессию,
    окно полного лога.

COM-вызовы AutoCAD и HTTP-запросы к LLM выполняются в фоновых потоках, а обновление
виджетов происходит строго в главном потоке Tkinter через очередь событий.

Запуск:  python main.py
"""

from __future__ import annotations

import json
import math
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

try:
    import tkinter as tk
except ImportError:  # pragma: no cover
    tk = None  # type: ignore[assignment]

from acad_builder import (
    AcadBuilder,
    AcadBuilderError,
    AcadConnectionError,
    DrawPlan,
    DrawResult,
    LotSuspicionReport,
    NeighborVerificationReport,
    compute_pob_point,
    format_distance,
    verify_with_neighbors,
)
from llm_parser import (
    ASPENWALL_SAMPLES,
    CONFIG_PATH,
    ENV_PATH,
    IMAGE_EXTENSIONS,
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
    TypoSuspicion,
    clean_provider_key,
    inspect_pdf,
    is_image_file,
    is_protected_key,
    load_config,
    load_dotenv_dict,
    parse_legal_description_full,
    parse_page_range,
    parse_with_regex,
    prepare_survey_image_bytes,
    protect_api_key,
    recheck_consensus_discrepancies,
    recheck_typo_suspicion,
    run_multipass_consensus,
    save_config,
    save_protected_env,
)

APP_TITLE = "Title Survey AI — Legal Description → AutoCAD"
APP_GEOMETRY = "1280x900"
VERSION = "1.0.0"

# Палитра Dark CAD Studio v1.0 (высокий контраст, инженерная сетка, чистые акценты)
THEME: dict[str, str] = {
    "bg_root": "#0a111a",
    "bg_header": "#0f1926",
    "bg_card": "#131f2f",
    "bg_inset": "#0d1622",
    "bg_editor": "#080e16",
    "border_card": "#23354d",
    "border_subtle": "#1a2a3e",
    "border_active": "#3b82f6",
    "text_main": "#eaf1fb",
    "text_muted": "#93a8c1",
    "text_dim": "#637994",
    "btn_primary": "#2563eb",
    "btn_primary_hover": "#1d4ed8",
    "btn_neighbor": "#0d9488",
    "btn_neighbor_hover": "#0f766e",
    "btn_secondary": "#1c2c40",
    "btn_secondary_hover": "#283d58",
    "btn_amber": "#c26a16",
    "btn_amber_hover": "#d97706",
    "btn_emerald": "#1f6f50",
    "btn_emerald_hover": "#278762",
    "badge_v1_bg": "#0f2d24",
    "badge_v1_border": "#1f6f50",
    "badge_v1_fg": "#34d399",
    "ok": "#34d399",
    "ok_bg": "#102a22",
    "warn": "#fbbf24",
    "warn_bg": "#2c2012",
    "warn_border": "#854d0e",
    "err": "#f87171",
    "sky": "#7dd3fc",
    "cad_grid": "#132235",
    "cad_prop": "#f87171",
    "cad_neighbor": "#22d3ee",
    "cad_street": "#475569",
    "cad_tie": "#fbbf24",
    "cad_hover": "#fde047",
}


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
    """Главное окно приложения (Dark CAD Studio UI)."""

    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.configure(fg_color=THEME["bg_root"])

        self.title(f"{APP_TITLE}  v{VERSION}")
        self.geometry(APP_GEOMETRY)
        self.minsize(1100, 760)

        # Окно поверх остальных, но не модальное: в AutoCAD удобно кликать параллельно.
        self.attributes("-topmost", True)
        self.after(400, lambda: self.attributes("-topmost", True))
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.config_data: dict[str, Any] = load_config()
        self.pdf_path: Path | None = None
        self.image_paths: list[Path] = []
        self._pdf_needs_vision: bool = False
        self._pdf_total_pages: int = 0
        self._visual_rotations: list[int] = []
        self._visual_cursor: int = 0
        self._tk_photo_ref: Any | None = None
        self.pdf_pages_var = ctk.StringVar(value="")
        self.enhance_scan_var = ctk.BooleanVar(
            value=bool(self.config_data["api"].get("vision_enhance_scans", True))
        )
        # ИИ-автоповорот через LLM ВСЕГДА выключен по умолчанию (доп. платный вызов API);
        # включается только разово на 1 конкретный запуск с явным предупреждением.
        self.auto_orient_var = ctk.BooleanVar(value=False)
        self.ask_on_doubt_var = ctk.BooleanVar(
            value=bool(self.config_data.get("api", {}).get("ask_on_doubt", True))
        )
        self.pending_doubt: dict[str, Any] | None = None
        self.doubt_window: Any | None = None
        self.preview_modes = ["📐 Схема CAD", "🖼 Исходник"]
        self.preview_mode_var = ctk.StringVar(value=self.preview_modes[0])
        self.description: LegalDescription | None = None
        self.draw_result: DrawResult | None = None
        self.last_usage: TokenUsage | None = None
        self._session_llm_calls = 0
        self._session_prompt_tokens = 0
        self._session_completion_tokens = 0
        self._busy = False
        self._closing = False
        self._events: queue.Queue[tuple[str, Any]] = queue.Queue()

        # Накопленная сцена для живого CAD-предпросмотра: основной участок + соседи
        self._preview_main_plan: DrawPlan | None = None
        self._preview_neighbor_plans: list[DrawPlan] = []
        self._last_active_plan: DrawPlan | None = None
        self._neighbor_report: NeighborVerificationReport | None = None
        self._last_suspicion_report: LotSuspicionReport | None = None

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
        self.provider_key_cards: dict[str, Any] = {}
        # Секретный код пересчёта (PIN) живёт ТОЛЬКО в оперативной памяти — никогда не пишется на диск
        self.key_secret_var = ctk.StringVar(value="")
        self._last_valid_secret: str = ""
        # Ящик ключей автоматически свёрнут, если хотя бы один ключ уже настроен
        self._keys_drawer_open: bool = not self._has_any_configured_key()

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
        self.shortcuts_window: Any | None = None
        self.preview_canvas: Any | None = None
        self._canvas_zoom: float = 1.0
        self._canvas_pan_x: float = 0.0
        self._canvas_pan_y: float = 0.0
        self._drag_origin: tuple[int, int] | None = None
        self._hovered_seg_idx: int | None = None
        self._hover_world_pt: tuple[float, float] | None = None
        self._last_world_transform: tuple[float, float, float, int, int] | None = None

        self._build_ui()
        self._bind_studio_shortcuts()
        self._refresh_height_label()
        self._refresh_provider_highlights()
        self._refresh_keys_drawer()
        self._refresh_consensus_rows()
        self._render_empty_inspector()
        self.after(120, self._drain_events)

    # ------------------------------------------------------------------ UI --
    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        # 0. Верхняя шапка: бренд + масштаб чертежа + ящик ключей + поверх окон
        header = ctk.CTkFrame(
            self,
            corner_radius=0,
            fg_color=THEME["bg_header"],
            border_width=1,
            border_color=THEME["border_subtle"],
        )
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(1, weight=1)

        brand_box = ctk.CTkFrame(header, fg_color="transparent")
        brand_box.grid(row=0, column=0, sticky="w", padx=18, pady=10)
        ctk.CTkLabel(
            brand_box,
            text="Title Survey AI",
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color=THEME["text_main"],
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            brand_box,
            text=f"v{VERSION} · STUDIO",
            font=ctk.CTkFont(size=11, weight="bold"),
            fg_color=THEME["badge_v1_bg"],
            corner_radius=6,
            padx=8,
            pady=2,
            text_color=THEME["badge_v1_fg"],
        ).grid(row=0, column=1, sticky="w", padx=(8, 0))
        ctk.CTkLabel(
            brand_box,
            text="Legal Description & Survey PDF  →  AutoCAD (LINE / NEIGHBOR_LOT)",
            font=ctk.CTkFont(size=12),
            text_color=THEME["text_muted"],
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(1, 0))

        header_tools = ctk.CTkFrame(header, fg_color="transparent")
        header_tools.grid(row=0, column=2, sticky="e", padx=18, pady=10)

        # Компактная капсула выбора масштаба чертежа
        scale_pill = ctk.CTkFrame(
            header_tools,
            fg_color=THEME["bg_inset"],
            corner_radius=8,
            border_width=1,
            border_color=THEME["border_card"],
        )
        scale_pill.grid(row=0, column=0, padx=(0, 8), sticky="e")
        ctk.CTkLabel(
            scale_pill,
            text="Масштаб:",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=THEME["text_muted"],
        ).grid(row=0, column=0, padx=(10, 6), pady=5)
        self.scale_menu = ctk.CTkOptionMenu(
            scale_pill,
            values=self.scale_options,
            variable=self.scale_var,
            width=102,
            height=28,
            fg_color=THEME["btn_secondary"],
            button_color=THEME["btn_primary"],
            button_hover_color=THEME["btn_primary_hover"],
            command=lambda _value: self._refresh_height_label(),
        )
        self.scale_menu.grid(row=0, column=1, padx=(0, 8), pady=5)
        self.height_label = ctk.CTkLabel(
            scale_pill,
            text="",
            anchor="w",
            font=ctk.CTkFont(size=12),
            text_color=THEME["sky"],
        )
        self.height_label.grid(row=0, column=2, padx=(0, 10), pady=5)

        self.keys_toggle_button = ctk.CTkButton(
            header_tools,
            text="🔑 API-ключи и защита ▾",
            height=34,
            width=192,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            border_width=1,
            border_color=THEME["border_card"],
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self.toggle_keys_drawer,
        )
        self.keys_toggle_button.grid(row=0, column=1, padx=(0, 8))

        self.shortcuts_button = ctk.CTkButton(
            header_tools,
            text="⌨ Клавиши",
            height=34,
            width=102,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            border_width=1,
            border_color=THEME["border_card"],
            font=ctk.CTkFont(size=12),
            command=self._open_shortcuts_window,
        )
        self.shortcuts_button.grid(row=0, column=2, padx=(0, 10))

        self.topmost_var = ctk.BooleanVar(value=True)
        ctk.CTkSwitch(
            header_tools,
            text="Поверх окон",
            variable=self.topmost_var,
            font=ctk.CTkFont(size=12),
            command=self._apply_topmost,
        ).grid(row=0, column=3, sticky="e")

        # 1. Сворачиваемый ящик API-ключей, PIN и сохранения настроек --------
        self.keys_drawer = ctk.CTkFrame(
            self,
            fg_color=THEME["bg_card"],
            corner_radius=10,
            border_width=1,
            border_color=THEME["border_card"],
        )
        self.keys_drawer.grid(row=1, column=0, sticky="ew", padx=16, pady=(10, 0))
        self.keys_drawer.grid_columnconfigure(0, weight=1)

        keys_frame = ctk.CTkFrame(self.keys_drawer, fg_color="transparent")
        keys_frame.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 6))
        for col, pid in enumerate(self.provider_ids):
            keys_frame.grid_columnconfigure(col, weight=1)
            card = ctk.CTkFrame(
                keys_frame,
                fg_color=THEME["bg_inset"],
                corner_radius=8,
                border_width=1,
                border_color=THEME["border_subtle"],
            )
            card.grid(
                row=0,
                column=col,
                sticky="ew",
                padx=(0 if col == 0 else 6, 0 if col == len(self.provider_ids) - 1 else 6),
            )
            card.grid_columnconfigure(0, weight=1)

            p_cfg = self._provider_cfgs[pid]
            env_name = str(p_cfg.get("env_key") or "").strip()
            has_env = bool(env_name and os.environ.get(env_name, "").strip())
            placeholder = (
                f"{env_name} (из окружения)"
                if has_env
                else f"Вставьте ключ {self._pid_to_label[pid]} ({env_name or 'sk-…'})"
            )

            lbl = ctk.CTkLabel(
                card,
                text=f"API key — {self._pid_to_label[pid]}",
                anchor="w",
                font=ctk.CTkFont(size=12, weight="bold"),
            )
            lbl.grid(row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(7, 3))
            entry = ctk.CTkEntry(
                card,
                textvariable=self.provider_key_vars[pid],
                show="•",
                height=30,
                fg_color=THEME["bg_editor"],
                border_color=THEME["border_card"],
                placeholder_text=placeholder,
            )
            entry.grid(row=1, column=0, sticky="ew", padx=(10, 4), pady=(0, 8))
            btn = ctk.CTkButton(
                card,
                text="👁",
                width=32,
                height=30,
                fg_color=THEME["btn_secondary"],
                hover_color=THEME["btn_secondary_hover"],
                command=lambda p=pid: self._toggle_key_visibility(p),
            )
            btn.grid(row=1, column=1, padx=(0, 10), pady=(0, 8))

            self.provider_key_cards[pid] = card
            self.provider_key_labels[pid] = lbl
            self.provider_key_entries[pid] = entry
            self.provider_key_buttons[pid] = btn

        vault_row = ctk.CTkFrame(self.keys_drawer, fg_color="transparent")
        vault_row.grid(row=1, column=0, sticky="ew", padx=12, pady=(2, 10))
        vault_row.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(
            vault_row,
            text="🔒 PIN пересчёта (в RAM, опц.):",
            font=ctk.CTkFont(size=12),
            text_color=THEME["text_muted"],
        ).grid(row=0, column=0, padx=(2, 8), sticky="w")
        self.key_secret_entry = ctk.CTkEntry(
            vault_row,
            textvariable=self.key_secret_var,
            show="•",
            width=175,
            height=30,
            fg_color=THEME["bg_editor"],
            border_color=THEME["border_card"],
            placeholder_text="не пишется на диск",
        )
        self.key_secret_entry.grid(row=0, column=1, sticky="w")
        self.secret_eye_button = ctk.CTkButton(
            vault_row,
            text="👁",
            width=32,
            height=30,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            command=self._toggle_secret_visibility,
        )
        self.secret_eye_button.grid(row=0, column=2, padx=(5, 12), sticky="w")
        ctk.CTkLabel(
            vault_row,
            text="На диске хранится только зашифрованный токен tsai1:… (MachineGuid + DPAPI + PIN)",
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color=THEME["text_dim"],
        ).grid(row=0, column=3, sticky="w")
        self.save_env_button = ctk.CTkButton(
            vault_row,
            text="Защитить в .env",
            width=136,
            height=30,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            border_width=1,
            border_color=THEME["border_card"],
            command=self._save_protected_env_clicked,
        )
        self.save_env_button.grid(row=0, column=4, padx=(8, 6), sticky="e")
        self.save_button = ctk.CTkButton(
            vault_row,
            text="💾 Сохранить настройки",
            width=172,
            height=30,
            fg_color=THEME["btn_primary"],
            hover_color=THEME["btn_primary_hover"],
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._save_settings,
        )
        self.save_button.grid(row=0, column=5, sticky="e")

        # 2. Карточка ИИ-движка, консенсуса 1–3 проходов и политики сомнений --
        ai_card = ctk.CTkFrame(
            self,
            fg_color=THEME["bg_card"],
            corner_radius=10,
            border_width=1,
            border_color=THEME["border_card"],
        )
        ai_card.grid(row=2, column=0, sticky="ew", padx=16, pady=(10, 0))
        ai_card.grid_columnconfigure(0, weight=1)

        llm_row = ctk.CTkFrame(ai_card, fg_color="transparent")
        llm_row.grid(row=0, column=0, sticky="ew", padx=14, pady=(9, 4))
        llm_row.grid_columnconfigure(4, weight=1)

        self.use_llm_switch = ctk.CTkSwitch(
            llm_row,
            text="ИИ-разбор (LLM)",
            variable=self.use_llm_var,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._on_use_llm_toggled,
        )
        self.use_llm_switch.grid(row=0, column=0, padx=(0, 14), sticky="w")

        self.pass1_title_label = ctk.CTkLabel(
            llm_row,
            text="Проход #1:",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=THEME["text_muted"],
        )
        self.pass1_title_label.grid(row=0, column=1, padx=(0, 6), sticky="w")

        self.provider_switch = ctk.CTkSegmentedButton(
            llm_row,
            values=self.provider_labels,
            variable=self.provider_label_var,
            selected_color=THEME["btn_primary"],
            selected_hover_color=THEME["btn_primary_hover"],
            command=self._on_provider_changed,
        )
        self.provider_switch.grid(row=0, column=2, padx=(0, 12), sticky="w")

        ctk.CTkLabel(
            llm_row,
            text="Модель:",
            font=ctk.CTkFont(size=12),
            text_color=THEME["text_muted"],
        ).grid(row=0, column=3, padx=(0, 6), sticky="w")

        self.model_combo = ctk.CTkComboBox(
            llm_row,
            values=self._provider_models[self._active_provider_id],
            variable=self.model_var,
            fg_color=THEME["bg_editor"],
            border_color=THEME["border_card"],
            button_color=THEME["btn_secondary"],
            button_hover_color=THEME["btn_secondary_hover"],
            command=self._on_model_selected,
        )
        self.model_combo.grid(row=0, column=4, sticky="ew")

        # Вторая компактная полоса политики проверки и человеческого контроля
        self.llm_policy_row = ctk.CTkFrame(ai_card, fg_color="transparent")
        self.llm_policy_row.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 8))
        self.llm_policy_row.grid_columnconfigure(2, weight=1)

        ctk.CTkLabel(
            self.llm_policy_row,
            text="Режим сверки:",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=THEME["text_muted"],
        ).grid(row=0, column=0, padx=(0, 8), sticky="w")

        self.passes_switch = ctk.CTkSegmentedButton(
            self.llm_policy_row,
            values=self.passes_options,
            variable=self.passes_label_var,
            height=28,
            selected_color=THEME["btn_neighbor"],
            selected_hover_color=THEME["btn_neighbor_hover"],
            command=self._on_passes_changed,
        )
        self.passes_switch.grid(row=0, column=1, sticky="w")

        ctk.CTkLabel(
            self.llm_policy_row,
            text="Консенсус 2+ проходов защищает цифры документа от тихих автоподмен",
            font=ctk.CTkFont(size=11),
            text_color=THEME["text_dim"],
            anchor="w",
        ).grid(row=0, column=2, padx=(14, 10), sticky="w")

        self.ask_on_doubt_check = ctk.CTkCheckBox(
            self.llm_policy_row,
            text="Спрашивать при сомнении",
            variable=self.ask_on_doubt_var,
            checkbox_width=18,
            checkbox_height=18,
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=THEME["text_main"],
        )
        self.ask_on_doubt_check.grid(row=0, column=3, sticky="e")

        # Раскрываемая строка Прохода #2 (при 2 или 3 проходах)
        self.pass2_row = ctk.CTkFrame(ai_card, fg_color="transparent")
        self.pass2_row.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 6))
        self.pass2_row.grid_columnconfigure(3, weight=1)
        ctk.CTkLabel(
            self.pass2_row,
            text="↳ Проход #2 (независимая сверка):",
            text_color=THEME["sky"],
            font=ctk.CTkFont(size=12, weight="bold"),
        ).grid(row=0, column=0, padx=(0, 10), sticky="w")
        self.pass2_provider_switch = ctk.CTkSegmentedButton(
            self.pass2_row,
            values=self.provider_labels,
            variable=self.pass2_provider_label_var,
            selected_color=THEME["btn_primary"],
            selected_hover_color=THEME["btn_primary_hover"],
            command=self._on_pass2_provider_changed,
        )
        self.pass2_provider_switch.grid(row=0, column=1, padx=(0, 12), sticky="w")
        ctk.CTkLabel(
            self.pass2_row,
            text="Модель #2:",
            font=ctk.CTkFont(size=12),
            text_color=THEME["text_muted"],
        ).grid(row=0, column=2, padx=(0, 6), sticky="w")
        self.pass2_model_combo = ctk.CTkComboBox(
            self.pass2_row,
            values=self._provider_models[self._pass2_pid],
            variable=self.pass2_model_var,
            fg_color=THEME["bg_editor"],
            border_color=THEME["border_card"],
            button_color=THEME["btn_secondary"],
            button_hover_color=THEME["btn_secondary_hover"],
            command=lambda val: self.pass2_model_var.set(str(val or "").strip()),
        )
        self.pass2_model_combo.grid(row=0, column=3, sticky="ew")

        # Раскрываемая строка Прохода #3 (при 3 проходах)
        self.pass3_row = ctk.CTkFrame(ai_card, fg_color="transparent")
        self.pass3_row.grid(row=3, column=0, sticky="ew", padx=14, pady=(0, 8))
        self.pass3_row.grid_columnconfigure(3, weight=1)
        ctk.CTkLabel(
            self.pass3_row,
            text="↳ Проход #3 (арбитражный слот):",
            text_color=THEME["ok"],
            font=ctk.CTkFont(size=12, weight="bold"),
        ).grid(row=0, column=0, padx=(0, 10), sticky="w")
        self.pass3_provider_switch = ctk.CTkSegmentedButton(
            self.pass3_row,
            values=self.provider_labels,
            variable=self.pass3_provider_label_var,
            selected_color=THEME["btn_primary"],
            selected_hover_color=THEME["btn_primary_hover"],
            command=self._on_pass3_provider_changed,
        )
        self.pass3_provider_switch.grid(row=0, column=1, padx=(0, 12), sticky="w")
        ctk.CTkLabel(
            self.pass3_row,
            text="Модель #3:",
            font=ctk.CTkFont(size=12),
            text_color=THEME["text_muted"],
        ).grid(row=0, column=2, padx=(0, 6), sticky="w")
        self.pass3_model_combo = ctk.CTkComboBox(
            self.pass3_row,
            values=self._provider_models[self._pass3_pid],
            variable=self.pass3_model_var,
            fg_color=THEME["bg_editor"],
            border_color=THEME["border_card"],
            button_color=THEME["btn_secondary"],
            button_hover_color=THEME["btn_secondary_hover"],
            command=lambda val: self.pass3_model_var.set(str(val or "").strip()),
        )
        self.pass3_model_combo.grid(row=0, column=3, sticky="ew")

        # Бейдж офлайн-режима (показывается, когда ИИ-разбор выключен)
        self.regex_mode_badge = ctk.CTkLabel(
            ai_card,
            text="⚡ Включён локальный офлайн-парсер (Regex) — запросы к API не отправляются, расход токенов = 0",
            anchor="w",
            font=ctk.CTkFont(size=12),
            text_color=THEME["warn"],
        )
        self.regex_mode_badge.grid(row=4, column=0, sticky="w", padx=14, pady=(0, 8))
        self.regex_mode_badge.grid_remove()

        # 3. Двухколоночная рабочая зона: Документ слева + Схема и ведомость справа
        workspace = ctk.CTkFrame(self, fg_color="transparent")
        workspace.grid(row=3, column=0, sticky="nsew", padx=16, pady=(10, 0))
        workspace.grid_columnconfigure(0, weight=54)
        workspace.grid_columnconfigure(1, weight=46)
        workspace.grid_rowconfigure(0, weight=1)

        # --- Левая колонка: 1. Исходное описание / PDF / Фото чертежа -------
        input_frame = ctk.CTkFrame(
            workspace,
            fg_color=THEME["bg_card"],
            corner_radius=10,
            border_width=1,
            border_color=THEME["border_card"],
        )
        input_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        input_frame.grid_columnconfigure(0, weight=1)
        input_frame.grid_rowconfigure(4, weight=1)

        doc_header = ctk.CTkFrame(input_frame, fg_color="transparent")
        doc_header.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
        doc_header.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            doc_header,
            text="1. Исходный документ (PDF / Фото чертежа JPG / Текст)",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=THEME["text_main"],
        ).grid(row=0, column=0, sticky="w")
        self.pdf_label = ctk.CTkLabel(
            doc_header,
            text="PDF не загружен",
            font=ctk.CTkFont(size=12),
            text_color=THEME["text_muted"],
        )
        self.pdf_label.grid(row=0, column=1, sticky="e")

        toolbar = ctk.CTkFrame(input_frame, fg_color="transparent")
        toolbar.grid(row=1, column=0, sticky="ew", padx=12, pady=(2, 6))
        toolbar.grid_columnconfigure(2, weight=1)

        ctk.CTkButton(
            toolbar,
            text="📄 PDF…",
            width=96,
            height=32,
            fg_color=THEME["btn_primary"],
            hover_color=THEME["btn_primary_hover"],
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._load_pdf,
        ).grid(row=0, column=0, sticky="w")

        self.load_images_button = ctk.CTkButton(
            toolbar,
            text="📷 Фото (JPG)…",
            width=126,
            height=32,
            fg_color=THEME["btn_amber"],
            hover_color=THEME["btn_amber_hover"],
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._load_images,
        )
        self.load_images_button.grid(row=0, column=1, padx=(6, 0), sticky="w")

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
            height=32,
            fg_color=THEME["btn_secondary"],
            button_color=THEME["btn_secondary_hover"],
            button_hover_color=THEME["btn_primary"],
            command=self._on_sample_selected,
        )
        self.sample_menu.grid(row=0, column=3, padx=(8, 0), sticky="e")

        ctk.CTkButton(
            toolbar,
            text="Пример",
            width=68,
            height=32,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            border_width=1,
            border_color=THEME["border_card"],
            command=self._insert_example,
        ).grid(row=0, column=4, padx=(6, 0), sticky="e")

        ctk.CTkButton(
            toolbar,
            text="Очистить",
            width=76,
            height=32,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            border_width=1,
            border_color=THEME["border_card"],
            command=self._clear_text,
        ).grid(row=0, column=5, padx=(6, 0), sticky="e")

        # Панель подготовки архивных фото 80-х и повёрнутых сканов (двухрядная без переполнения)
        self.visual_bar = ctk.CTkFrame(
            input_frame,
            fg_color=THEME["warn_bg"],
            corner_radius=8,
            border_width=1,
            border_color=THEME["warn_border"],
        )
        self.visual_bar.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 6))
        self.visual_bar.grid_columnconfigure(0, weight=1)

        visual_top_row = ctk.CTkFrame(self.visual_bar, fg_color="transparent")
        visual_top_row.grid(row=0, column=0, sticky="ew", padx=8, pady=(5, 2))
        visual_top_row.grid_columnconfigure(6, weight=1)

        self.prev_visual_button = ctk.CTkButton(
            visual_top_row,
            text="◀",
            width=28,
            height=26,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            command=lambda: self.step_visual_frame(-1),
        )
        self.prev_visual_button.grid(row=0, column=0, padx=(0, 3))

        self.visual_pos_label = ctk.CTkLabel(
            visual_top_row,
            text="Кадр 1/1 · 0°",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=THEME["warn"],
        )
        self.visual_pos_label.grid(row=0, column=1, padx=3)

        self.next_visual_button = ctk.CTkButton(
            visual_top_row,
            text="▶",
            width=28,
            height=26,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            command=lambda: self.step_visual_frame(1),
        )
        self.next_visual_button.grid(row=0, column=2, padx=(3, 8))

        self.rot_ccw_button = ctk.CTkButton(
            visual_top_row,
            text="⟲ -90°",
            width=58,
            height=26,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            font=ctk.CTkFont(size=11, weight="bold"),
            command=lambda: self.rotate_visual(-90),
        )
        self.rot_ccw_button.grid(row=0, column=3, padx=2)

        self.rot_cw_button = ctk.CTkButton(
            visual_top_row,
            text="⟳ +90°",
            width=58,
            height=26,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            font=ctk.CTkFont(size=11, weight="bold"),
            command=lambda: self.rotate_visual(90),
        )
        self.rot_cw_button.grid(row=0, column=4, padx=2)

        self.rot_180_button = ctk.CTkButton(
            visual_top_row,
            text="⇅ 180°",
            width=56,
            height=26,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            font=ctk.CTkFont(size=11, weight="bold"),
            command=lambda: self.rotate_visual(180),
        )
        self.rot_180_button.grid(row=0, column=5, padx=(2, 8))

        self.pdf_pages_box = ctk.CTkFrame(visual_top_row, fg_color="transparent")
        self.pdf_pages_box.grid(row=0, column=6, sticky="e", padx=(0, 2))
        ctk.CTkLabel(
            self.pdf_pages_box,
            text="Стр.:",
            font=ctk.CTkFont(size=11),
            text_color=THEME["text_main"],
        ).grid(row=0, column=0, padx=(0, 4))
        self.pdf_pages_entry = ctk.CTkEntry(
            self.pdf_pages_box,
            textvariable=self.pdf_pages_var,
            width=84,
            height=26,
            fg_color=THEME["bg_editor"],
            border_color=THEME["border_card"],
            placeholder_text="1-8 / все",
        )
        self.pdf_pages_entry.grid(row=0, column=1)

        visual_opts_row = ctk.CTkFrame(self.visual_bar, fg_color="transparent")
        visual_opts_row.grid(row=1, column=0, sticky="ew", padx=8, pady=(1, 5))
        visual_opts_row.grid_columnconfigure(1, weight=1)

        self.enhance_scan_switch = ctk.CTkSwitch(
            visual_opts_row,
            text="Чёткость фото 80-х (CLAHE/контраст)",
            variable=self.enhance_scan_var,
            font=ctk.CTkFont(size=11),
            command=self._redraw_preview_canvas,
        )
        self.enhance_scan_switch.grid(row=0, column=0, padx=(0, 12), sticky="w")

        self.auto_orient_switch = ctk.CTkCheckBox(
            visual_opts_row,
            text="🧭 ИИ-поворот (1 раз, $)",
            variable=self.auto_orient_var,
            checkbox_width=18,
            checkbox_height=18,
            fg_color=THEME["warn"],
            hover_color="#d97706",
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self._on_auto_orient_toggled,
        )
        self.auto_orient_switch.grid(row=0, column=2, sticky="e")

        self.auto_orient_warn_label = ctk.CTkLabel(
            self.visual_bar,
            text=(
                "⚠ Включён разовый ИИ-автоповорот: выполнит ДОПОЛНИТЕЛЬНЫЙ платный запрос к LLM "
                "(расход токенов API) только для одного ближайшего запуска, после чего сам выключится. "
                "Бесплатная альтернатива: кнопки «⟲ -90° / ⟳ +90° / ⇅ 180°»."
            ),
            font=ctk.CTkFont(size=11),
            text_color=THEME["warn"],
            anchor="w",
            justify="left",
            wraplength=640,
        )
        self.auto_orient_warn_label.grid(
            row=2, column=0, sticky="ew", padx=10, pady=(0, 6)
        )
        self.auto_orient_warn_label.grid_remove()
        self.visual_bar.grid_remove()

        # Плашка выбора роли участка на чертеже + сверка и пакет соседей
        role_bar = ctk.CTkFrame(
            input_frame,
            fg_color=THEME["bg_inset"],
            corner_radius=8,
            border_width=1,
            border_color=THEME["border_subtle"],
        )
        role_bar.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 8))
        role_bar.grid_columnconfigure(2, weight=1)

        self.lot_role_var = ctk.StringVar(value="main")
        self.main_lot_radio = ctk.CTkRadioButton(
            role_bar,
            text="Основной (PROPERTY_LINE)",
            variable=self.lot_role_var,
            value="main",
            font=ctk.CTkFont(size=12),
            command=self._on_lot_role_changed,
        )
        self.main_lot_radio.grid(row=0, column=0, sticky="w", padx=(10, 10), pady=7)

        self.neighbor_lot_radio = ctk.CTkRadioButton(
            role_bar,
            text="Сосед (NEIGHBOR_LOT)",
            variable=self.lot_role_var,
            value="neighbor",
            font=ctk.CTkFont(size=12),
            command=self._on_lot_role_changed,
        )
        self.neighbor_lot_radio.grid(row=0, column=1, sticky="w", padx=(0, 8), pady=7)

        self.verify_neighbors_button = ctk.CTkButton(
            role_bar,
            text="🔍 Сверка с соседями",
            width=158,
            height=28,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._on_verify_neighbors_clicked,
        )
        self.verify_neighbors_button.grid(row=0, column=3, sticky="e", padx=(2, 4), pady=6)

        self.batch_neighbors_button = ctk.CTkButton(
            role_bar,
            text="📂 Пакет соседей…",
            width=136,
            height=28,
            fg_color=THEME["btn_neighbor"],
            hover_color=THEME["btn_neighbor_hover"],
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._load_and_draw_neighbor_files,
        )
        self.batch_neighbors_button.grid(row=0, column=4, sticky="e", padx=(2, 8), pady=6)

        self.text_box = ctk.CTkTextbox(
            input_frame,
            wrap="word",
            fg_color=THEME["bg_editor"],
            border_width=1,
            border_color=THEME["border_subtle"],
            text_color=THEME["text_main"],
            font=ctk.CTkFont(size=13),
        )
        self.text_box.grid(row=4, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self.text_box.insert("1.0", "")

        # --- Правая колонка: 2. Живой CAD-предпросмотр / Исходник + ведомость ---
        preview_card = ctk.CTkFrame(
            workspace,
            fg_color=THEME["bg_card"],
            corner_radius=10,
            border_width=1,
            border_color=THEME["border_card"],
        )
        preview_card.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        preview_card.grid_columnconfigure(0, weight=1)
        preview_card.grid_rowconfigure(1, weight=56)
        preview_card.grid_rowconfigure(2, weight=44)

        prev_header = ctk.CTkFrame(preview_card, fg_color="transparent")
        prev_header.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
        prev_header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            prev_header,
            text="2. Предпросмотр и ведомость",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=THEME["text_main"],
        ).grid(row=0, column=0, sticky="w")
        self.preview_mode_switch = ctk.CTkSegmentedButton(
            prev_header,
            values=self.preview_modes,
            variable=self.preview_mode_var,
            height=26,
            selected_color=THEME["btn_primary"],
            selected_hover_color=THEME["btn_primary_hover"],
            command=self._on_preview_mode_changed,
        )
        self.preview_mode_switch.grid(row=0, column=1, padx=(6, 4), sticky="e")

        self.zoom_reset_button = ctk.CTkButton(
            prev_header,
            text="1:1",
            width=42,
            height=26,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self.reset_viewport_zoom,
        )
        self.zoom_reset_button.grid(row=0, column=2, padx=(0, 4), sticky="e")

        self.copy_report_button = ctk.CTkButton(
            prev_header,
            text="📋 Копировать",
            width=102,
            height=26,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            font=ctk.CTkFont(size=11),
            command=self.copy_inspector_report,
        )
        self.copy_report_button.grid(row=0, column=3, padx=(0, 4), sticky="e")

        self.reset_scene_button = ctk.CTkButton(
            prev_header,
            text="Сбросить",
            width=74,
            height=26,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            font=ctk.CTkFont(size=11),
            command=self._clear_preview_scene,
        )
        self.reset_scene_button.grid(row=0, column=4, sticky="e")

        canvas_wrap = ctk.CTkFrame(
            preview_card,
            fg_color=THEME["bg_editor"],
            corner_radius=8,
            border_width=1,
            border_color=THEME["border_subtle"],
        )
        canvas_wrap.grid(row=1, column=0, sticky="nsew", padx=12, pady=(2, 6))
        canvas_wrap.grid_columnconfigure(0, weight=1)
        canvas_wrap.grid_rowconfigure(0, weight=1)

        canvas_cls = getattr(tk, "Canvas", None) if tk is not None else None
        if canvas_cls is not None:
            self.preview_canvas = canvas_cls(
                canvas_wrap,
                bg=THEME["bg_editor"],
                highlightthickness=0,
                bd=0,
            )
            self.preview_canvas.grid(row=0, column=0, sticky="nsew", padx=3, pady=3)
            bind_fn = getattr(self.preview_canvas, "bind", None)
            if callable(bind_fn):
                bind_fn("<Configure>", lambda _ev: self._redraw_preview_canvas())
                bind_fn("<MouseWheel>", self._on_canvas_mousewheel)
                bind_fn("<Button-4>", lambda ev: self._on_canvas_mousewheel(ev, step_override=1))
                bind_fn("<Button-5>", lambda ev: self._on_canvas_mousewheel(ev, step_override=-1))
                bind_fn("<ButtonPress-1>", self._on_canvas_drag_start)
                bind_fn("<B1-Motion>", self._on_canvas_drag_move)
                bind_fn("<ButtonRelease-1>", self._on_canvas_drag_end)
                bind_fn("<Double-Button-1>", lambda _ev: self.reset_viewport_zoom())
                bind_fn("<Motion>", self._on_canvas_hover)
                bind_fn("<Leave>", self._on_canvas_leave)

        self.inspector_box = ctk.CTkTextbox(
            preview_card,
            wrap="word",
            fg_color=THEME["bg_inset"],
            border_width=1,
            border_color=THEME["border_subtle"],
            text_color=THEME["text_main"],
            font=ctk.CTkFont(family="Consolas", size=12),
        )
        self.inspector_box.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 12))

        # 4. Панель главных действий -----------------------------------------
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=4, column=0, sticky="ew", padx=16, pady=(10, 0))
        actions.grid_columnconfigure(1, weight=1)

        self.parse_button = ctk.CTkButton(
            actions,
            text="🔍 Проверить и показать схему  (Ctrl+Enter)",
            width=285,
            height=42,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            border_width=1,
            border_color=THEME["border_card"],
            font=ctk.CTkFont(size=13, weight="bold"),
            command=lambda: self._start(parse_only=True),
        )
        self.parse_button.grid(row=0, column=0, sticky="w")

        self.draw_button = ctk.CTkButton(
            actions,
            text="Начертить участок в AutoCAD",
            height=44,
            fg_color=THEME["btn_primary"],
            hover_color=THEME["btn_primary_hover"],
            font=ctk.CTkFont(size=15, weight="bold"),
            command=lambda: self._start(parse_only=False),
        )
        self.draw_button.grid(row=0, column=1, padx=10, sticky="ew")

        self.add_neighbor_button = ctk.CTkButton(
            actions,
            text="Добавить соседа на чертёж",
            height=42,
            width=225,
            fg_color=THEME["btn_neighbor"],
            hover_color=THEME["btn_neighbor_hover"],
            font=ctk.CTkFont(size=13, weight="bold"),
            command=lambda: self._start(parse_only=False, force_neighbor=True),
        )
        self.add_neighbor_button.grid(row=0, column=2, sticky="e")

        # 5. Тонкий индикатор прогресса --------------------------------------
        self.progress = ctk.CTkProgressBar(
            self,
            mode="indeterminate",
            height=6,
            fg_color=THEME["bg_inset"],
            progress_color=THEME["btn_primary"],
        )
        self.progress.grid(row=5, column=0, sticky="ew", padx=16, pady=(8, 0))
        self.progress.set(0)

        # 6. Панель разрешения конфликта проходов LLM и сомнительных мест ----
        self.alerts_wrap = ctk.CTkFrame(self, fg_color="transparent")
        self.alerts_wrap.grid(row=6, column=0, sticky="ew")
        self.alerts_wrap.grid_columnconfigure(0, weight=1)

        self.conflict_bar = ctk.CTkFrame(
            self.alerts_wrap,
            fg_color=THEME["warn_bg"],
            corner_radius=10,
            border_width=1,
            border_color=THEME["warn_border"],
        )
        self.conflict_bar.grid(row=0, column=0, sticky="ew", padx=16, pady=(8, 0))
        self.conflict_bar.grid_columnconfigure(0, weight=1)

        self.conflict_title_var = ctk.StringVar(
            value="⚠ Обнаружено расхождение между проходами LLM!"
        )
        self.conflict_title_label = ctk.CTkLabel(
            self.conflict_bar,
            textvariable=self.conflict_title_var,
            anchor="w",
            justify="left",
            wraplength=1100,
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=THEME["warn"],
        )
        self.conflict_title_label.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 4))

        conflict_btns = ctk.CTkFrame(self.conflict_bar, fg_color="transparent")
        conflict_btns.grid(row=1, column=0, sticky="ew", padx=12, pady=(2, 8))

        self.retry_conflict_button = ctk.CTkButton(
            conflict_btns,
            text="🔄 Попробуй разобраться ещё раз",
            height=32,
            fg_color=THEME["btn_amber"],
            hover_color=THEME["btn_amber_hover"],
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self.retry_conflict_resolution,
        )
        self.retry_conflict_button.grid(row=0, column=0, padx=(0, 8), sticky="w")

        self.pick_pass1_button = ctk.CTkButton(
            conflict_btns,
            text="Прав Проход #1",
            height=32,
            width=165,
            fg_color=THEME["btn_emerald"],
            hover_color=THEME["btn_emerald_hover"],
            command=lambda: self.resolve_conflict_with_pass(1),
        )
        self.pick_pass1_button.grid(row=0, column=1, padx=(0, 6), sticky="w")

        self.pick_pass2_button = ctk.CTkButton(
            conflict_btns,
            text="Прав Проход #2",
            height=32,
            width=165,
            fg_color=THEME["btn_emerald"],
            hover_color=THEME["btn_emerald_hover"],
            command=lambda: self.resolve_conflict_with_pass(2),
        )
        self.pick_pass2_button.grid(row=0, column=2, padx=(0, 6), sticky="w")

        self.pick_pass3_button = ctk.CTkButton(
            conflict_btns,
            text="Прав Проход #3",
            height=32,
            width=165,
            fg_color=THEME["btn_emerald"],
            hover_color=THEME["btn_emerald_hover"],
            command=lambda: self.resolve_conflict_with_pass(3),
        )
        self.pick_pass3_button.grid(row=0, column=3, padx=(0, 8), sticky="w")

        self.open_conflict_details_button = ctk.CTkButton(
            conflict_btns,
            text="Подробное сравнение…",
            height=32,
            width=175,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            command=self._open_conflict_dialog,
        )
        self.open_conflict_details_button.grid(row=0, column=4, sticky="w")
        self.conflict_bar.grid_remove()

        # Панель проверки подозрения на опечатку / рискованного действия
        self.doubt_bar = ctk.CTkFrame(
            self.alerts_wrap,
            fg_color=THEME["warn_bg"],
            corner_radius=10,
            border_width=1,
            border_color=THEME["warn_border"],
        )
        self.doubt_bar.grid(row=1, column=0, sticky="ew", padx=16, pady=(8, 0))
        self.doubt_bar.grid_columnconfigure(0, weight=1)

        self.doubt_title_var = ctk.StringVar(
            value="🙋 Обнаружено сомнительное место / подозрение на опечатку!"
        )
        self.doubt_title_label = ctk.CTkLabel(
            self.doubt_bar,
            textvariable=self.doubt_title_var,
            anchor="w",
            justify="left",
            wraplength=1100,
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=THEME["warn"],
        )
        self.doubt_title_label.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 4))

        doubt_btns = ctk.CTkFrame(self.doubt_bar, fg_color="transparent")
        doubt_btns.grid(row=1, column=0, sticky="ew", padx=12, pady=(2, 8))

        self.retry_doubt_button = ctk.CTkButton(
            doubt_btns,
            text="🔬 Ещё раунд LLM с фокусом на зону внимания",
            height=32,
            fg_color=THEME["btn_amber"],
            hover_color=THEME["btn_amber_hover"],
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self.retry_doubt_with_focus,
        )
        self.retry_doubt_button.grid(row=0, column=0, padx=(0, 8), sticky="w")

        self.keep_verbatim_button = ctk.CTkButton(
            doubt_btns,
            text="📄 Оставить как в документе",
            height=32,
            fg_color=THEME["btn_emerald"],
            hover_color=THEME["btn_emerald_hover"],
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self.confirm_doubt_keep_verbatim,
        )
        self.keep_verbatim_button.grid(row=0, column=1, padx=(0, 8), sticky="w")

        self.apply_typo_fix_button = ctk.CTkButton(
            doubt_btns,
            text="✏️ Применить исправление",
            height=32,
            fg_color=THEME["btn_primary"],
            hover_color=THEME["btn_primary_hover"],
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self.confirm_doubt_apply_fix,
        )
        self.apply_typo_fix_button.grid(row=0, column=2, padx=(0, 8), sticky="w")

        self.open_doubt_details_button = ctk.CTkButton(
            doubt_btns,
            text="Подробности…",
            height=32,
            width=130,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            command=self._open_doubt_dialog,
        )
        self.open_doubt_details_button.grid(row=0, column=3, sticky="w")
        self.doubt_bar.grid_remove()

        # 7. Нижняя приборная панель: KPI невязки и токенов + статус + лог ---
        status_frame = ctk.CTkFrame(
            self,
            fg_color=THEME["bg_card"],
            corner_radius=10,
            border_width=1,
            border_color=THEME["border_card"],
        )
        status_frame.grid(row=7, column=0, sticky="ew", padx=16, pady=(8, 12))
        status_frame.grid_columnconfigure(0, weight=1)

        kpi_bar = ctk.CTkFrame(status_frame, fg_color="transparent")
        kpi_bar.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 4))
        kpi_bar.grid_columnconfigure(2, weight=1)

        self.closure_var = ctk.StringVar(value="Closure Error: —")
        self.closure_label = ctk.CTkLabel(
            kpi_bar,
            textvariable=self.closure_var,
            anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=THEME["text_main"],
        )
        self.closure_label.grid(row=0, column=0, sticky="w", padx=(0, 12))

        self.suspicion_badge_button = ctk.CTkButton(
            kpi_bar,
            text="🔎 Форма: —",
            width=190,
            height=26,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            border_width=1,
            border_color=THEME["border_card"],
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._open_lot_suspicion_window,
        )
        self.suspicion_badge_button.grid(row=0, column=1, sticky="w", padx=(0, 14))

        self.tokens_var = ctk.StringVar(value="Токены за вызов: —")
        self.tokens_label = ctk.CTkLabel(
            kpi_bar,
            textvariable=self.tokens_var,
            anchor="w",
            font=ctk.CTkFont(size=12),
            text_color=THEME["sky"],
        )
        self.tokens_label.grid(row=0, column=2, sticky="w")

        ctk.CTkButton(
            kpi_bar,
            text="📋 Показать лог",
            width=126,
            height=28,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            border_width=1,
            border_color=THEME["border_card"],
            command=self._show_log,
        ).grid(row=0, column=3, sticky="e")

        self.status_var = ctk.StringVar(
            value="Готово. Загрузите PDF, выберите пример или вставьте текст Legal Description."
        )
        self.status_label = ctk.CTkLabel(
            status_frame,
            textvariable=self.status_var,
            anchor="w",
            font=ctk.CTkFont(size=12),
            text_color=THEME["text_muted"],
            justify="left",
            wraplength=1110,
        )
        self.status_label.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 9))

    # --------------------------------------------------- ящик ключей и ИИ ---
    def _configured_keys_count(self) -> int:
        count = 0
        for pid in self.provider_ids:
            typed = str(self.provider_key_vars[pid].get() or "").strip()
            env_name = str(self._provider_cfgs[pid].get("env_key") or "").strip()
            has_env = bool(env_name and os.environ.get(env_name, "").strip())
            if typed or has_env:
                count += 1
        return count

    def _has_any_configured_key(self) -> bool:
        return self._configured_keys_count() > 0

    def toggle_keys_drawer(self) -> None:
        """Сворачивает или разворачивает панель ввода API-ключей и PIN."""
        self._keys_drawer_open = not self._keys_drawer_open
        self._refresh_keys_drawer()

    def _refresh_keys_drawer(self) -> None:
        n_keys = self._configured_keys_count()
        arrow = "▴" if self._keys_drawer_open else "▾"
        if n_keys > 0:
            self.keys_toggle_button.configure(
                text=f"🔑 API-ключи: готовы ({n_keys}) {arrow}",
                border_color=THEME["ok"] if not self._keys_drawer_open else THEME["border_active"],
            )
        else:
            self.keys_toggle_button.configure(
                text=f"🔑 Ввести API-ключи {arrow}",
                border_color=THEME["warn"],
            )
        if self._keys_drawer_open:
            self.keys_drawer.grid(row=1, column=0, sticky="ew", padx=16, pady=(10, 0))
        else:
            self.keys_drawer.grid_remove()

    def _on_use_llm_toggled(self) -> None:
        self._refresh_consensus_rows()

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
        use_llm = bool(self.use_llm_var.get())
        count = self.current_passes_count() if use_llm else 1
        state = "normal" if use_llm else "disabled"
        self.provider_switch.configure(state=state)
        self.model_combo.configure(state=state)
        self.passes_switch.configure(state=state)

        if not use_llm:
            self.llm_policy_row.grid_remove()
            self.pass2_row.grid_remove()
            self.pass3_row.grid_remove()
            self.regex_mode_badge.grid(row=4, column=0, sticky="w", padx=14, pady=(0, 8))
            return

        self.llm_policy_row.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 8))
        self.regex_mode_badge.grid_remove()
        if count >= 2:
            self.pass2_row.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 6))
        else:
            self.pass2_row.grid_remove()
        if count >= 3:
            self.pass3_row.grid(row=3, column=0, sticky="ew", padx=14, pady=(0, 8))
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
            card = self.provider_key_cards.get(pid)
            if pid == active:
                lbl.configure(
                    text=f"API key — {title}{lock}  ● активен",
                    text_color=THEME["sky"],
                )
                if card is not None:
                    card.configure(border_color=THEME["border_active"])
            else:
                lbl.configure(
                    text=f"API key — {title}{lock}",
                    text_color=THEME["text_muted"],
                )
                if card is not None:
                    card.configure(border_color=THEME["border_subtle"])
        if hasattr(self, "keys_toggle_button"):
            n_keys = self._configured_keys_count()
            arrow = "▴" if self._keys_drawer_open else "▾"
            self.keys_toggle_button.configure(
                text=(
                    f"🔑 API-ключи: готовы ({n_keys}) {arrow}"
                    if n_keys > 0
                    else f"🔑 Ввести API-ключи {arrow}"
                )
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

    # ---------------------------------------------- предпросмотр и ведомость --
    def _set_inspector_text(self, content: str) -> None:
        target = getattr(self.inspector_box, "_textbox", self.inspector_box)
        try:
            target.configure(state="normal")
        except Exception:  # noqa: BLE001
            pass
        self.inspector_box.delete("1.0", "end")
        self.inspector_box.insert("1.0", content)
        try:
            target.configure(state="disabled")
        except Exception:  # noqa: BLE001
            pass

    def copy_inspector_report(self) -> str:
        """Копирует текущий текст ведомости участка в буфер обмена ОС."""
        txt = str(self.inspector_box.get("1.0", "end") or "").strip()
        if not txt:
            self._set_status("Ведомость пуста — нечего копировать.")
            return ""
        clip_fn = getattr(self, "clipboard_clear", None)
        append_fn = getattr(self, "clipboard_append", None)
        if callable(clip_fn) and callable(append_fn):
            clip_fn()
            append_fn(txt)
        self._set_status("📋 Ведомость участка скопирована в буфер обмена.")
        return txt

    def _bind_studio_shortcuts(self) -> None:
        """Привязывает студийные горячие клавиши для быстрой повседневной работы."""
        bind_fn = getattr(self, "bind_all", None) or getattr(self, "bind", None)
        if not callable(bind_fn):
            return

        def _guard(action: Any) -> Any:
            def _handler(_event: Any = None) -> str | None:
                if getattr(self, "_busy", False):
                    return None
                action()
                return "break"

            return _handler

        bind_fn("<Control-Return>", _guard(lambda: self._start(parse_only=True)))
        bind_fn("<F5>", _guard(lambda: self._start(parse_only=False)))
        bind_fn("<Control-o>", _guard(self._load_pdf))
        bind_fn("<Control-O>", _guard(self._load_pdf))
        bind_fn("<Control-i>", _guard(self._load_images))
        bind_fn("<Control-I>", _guard(self._load_images))
        bind_fn("<Control-s>", _guard(self._save_settings))
        bind_fn("<Control-S>", _guard(self._save_settings))

    def _open_shortcuts_window(self) -> None:
        """Открывает компактную памятку по горячим клавишам и интерактивному холсту."""
        existing = self.shortcuts_window
        if existing is not None:
            try:
                existing.destroy()
            except Exception:  # noqa: BLE001
                pass
        win = ctk.CTkToplevel(self)
        self.shortcuts_window = win
        win.title(f"Горячие клавиши и управление холстом — Title Survey AI v{VERSION}")
        win.geometry("620x420")
        win.configure(fg_color=THEME["bg_root"])
        try:
            win.attributes("-topmost", True)
        except Exception:  # noqa: BLE001
            pass
        win.grid_columnconfigure(0, weight=1)
        win.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            win,
            text=f"⌨ Памятка управления Studio v{VERSION}",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=THEME["text_main"],
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 6))

        guide = (
            "КЛАВИАТУРА:\n"
            "  Ctrl + Enter    — 🔍 Проверить описание и построить живую схему CAD\n"
            "  F5              — 📐 Начертить участок напрямую в активном чертеже AutoCAD\n"
            "  Ctrl + O        — 📄 Открыть PDF документ (текстовый или отсканированный)\n"
            "  Ctrl + I        — 📷 Загрузить фото чертежа (JPG / PNG / WebP / TIFF)\n"
            "  Ctrl + S        — 💾 Сохранить текущие настройки и выбранные модели\n\n"
            "ИНТЕРАКТИВНЫЙ ХОЛСТ («📐 Схема CAD» и «🖼 Исходник»):\n"
            "  Колесо мыши     — Плавный зум (0.6× .. 10×) к точке под курсором\n"
            "                    (удобно для разглядывания мелких цифр 80-х на фото!)\n"
            "  Зажатая ЛКМ     — Свободное панорамирование чертежа или скана\n"
            "  Двойной клик    — Мгновенный сброс масштаба и панорамы в 1:1 (кнопка «1:1»)\n"
            "  Наведение мыши  — Живые координаты (X, Y) ft от POB и подсветка стороны #N"
        )
        box = ctk.CTkTextbox(
            win,
            wrap="word",
            fg_color=THEME["bg_inset"],
            border_width=1,
            border_color=THEME["border_card"],
            text_color=THEME["text_main"],
            font=ctk.CTkFont(family="Consolas", size=12),
        )
        box.grid(row=1, column=0, sticky="nsew", padx=16, pady=6)
        box.insert("1.0", guide)
        try:
            getattr(box, "_textbox", box).configure(state="disabled")
        except Exception:  # noqa: BLE001
            pass

        ctk.CTkButton(
            win,
            text="Закрыть",
            width=120,
            height=32,
            fg_color=THEME["btn_primary"],
            hover_color=THEME["btn_primary_hover"],
            command=win.destroy,
        ).grid(row=2, column=0, sticky="e", padx=16, pady=(4, 14))

    def _reset_viewport_state(self) -> None:
        self._canvas_zoom = 1.0
        self._canvas_pan_x = 0.0
        self._canvas_pan_y = 0.0
        self._drag_origin = None
        self._hovered_seg_idx = None
        self._hover_world_pt = None
        if hasattr(self, "zoom_reset_button"):
            self.zoom_reset_button.configure(text="1:1")

    def reset_viewport_zoom(self) -> None:
        """Сбрасывает зум и панорамирование холста к 100% (1:1)."""
        self._reset_viewport_state()
        self._redraw_preview_canvas()

    def zoom_viewport(
        self,
        factor: float,
        *,
        pivot_x: float | None = None,
        pivot_y: float | None = None,
    ) -> float:
        """Масштабирует холст предпросмотра вокруг точки курсора (0.6× .. 10.0×)."""
        old_zoom = float(self._canvas_zoom or 1.0)
        new_zoom = max(0.6, min(10.0, old_zoom * float(factor)))
        if abs(new_zoom - old_zoom) < 1e-4:
            return old_zoom

        canvas = self.preview_canvas
        if canvas is not None and pivot_x is not None and pivot_y is not None:
            w = max(int(getattr(canvas, "winfo_width", lambda: 0)() or 0), 260)
            h = max(int(getattr(canvas, "winfo_height", lambda: 0)() or 0), 190)
            dx = float(pivot_x) - (w / 2.0 + self._canvas_pan_x)
            dy = float(pivot_y) - (h / 2.0 + self._canvas_pan_y)
            ratio = new_zoom / old_zoom
            self._canvas_pan_x -= dx * (ratio - 1.0)
            self._canvas_pan_y -= dy * (ratio - 1.0)

        self._canvas_zoom = new_zoom
        if hasattr(self, "zoom_reset_button"):
            lbl = "1:1" if abs(new_zoom - 1.0) < 0.03 else f"{new_zoom:.1f}×"
            self.zoom_reset_button.configure(text=lbl)
        self._redraw_preview_canvas()
        return self._canvas_zoom

    def _on_canvas_mousewheel(self, event: Any, step_override: int | None = None) -> None:
        if step_override is not None:
            direction = 1 if step_override > 0 else -1
        else:
            delta = int(getattr(event, "delta", 0) or 0)
            if delta == 0:
                return
            direction = 1 if delta > 0 else -1
        factor = 1.22 if direction > 0 else (1.0 / 1.22)
        px = getattr(event, "x", None)
        py = getattr(event, "y", None)
        self.zoom_viewport(
            factor,
            pivot_x=float(px) if px is not None else None,
            pivot_y=float(py) if py is not None else None,
        )

    def _on_canvas_drag_start(self, event: Any) -> None:
        self._drag_origin = (int(getattr(event, "x", 0)), int(getattr(event, "y", 0)))

    def _on_canvas_drag_move(self, event: Any) -> None:
        if self._drag_origin is None:
            return
        ex = int(getattr(event, "x", 0))
        ey = int(getattr(event, "y", 0))
        ox, oy = self._drag_origin
        if ex == ox and ey == oy:
            return
        self._canvas_pan_x += float(ex - ox)
        self._canvas_pan_y += float(ey - oy)
        self._drag_origin = (ex, ey)
        self._redraw_preview_canvas()

    def _on_canvas_drag_end(self, _event: Any = None) -> None:
        self._drag_origin = None

    @staticmethod
    def _point_to_segment_dist_px(
        px: float, py: float, x1: float, y1: float, x2: float, y2: float
    ) -> float:
        vx, vy = x2 - x1, y2 - y1
        len_sq = vx * vx + vy * vy
        if len_sq <= 1e-9:
            return math.hypot(px - x1, py - y1)
        t = max(0.0, min(1.0, ((px - x1) * vx + (py - y1) * vy) / len_sq))
        proj_x = x1 + t * vx
        proj_y = y1 + t * vy
        return math.hypot(px - proj_x, py - proj_y)

    def _on_canvas_hover(self, event: Any) -> None:
        if self.preview_mode_var.get() != "📐 Схема CAD":
            return
        tr = self._last_world_transform
        if tr is None:
            return
        cx_w, cy_w, eff_scale, w, h = tr
        if eff_scale <= 1e-9:
            return
        mx = float(getattr(event, "x", 0))
        my = float(getattr(event, "y", 0))
        wx = cx_w + (mx - (w / 2.0 + self._canvas_pan_x)) / eff_scale
        wy = cy_w - (my - (h / 2.0 + self._canvas_pan_y)) / eff_scale
        self._hover_world_pt = (wx, wy)

        best_idx: int | None = None
        best_dist = 9.5
        active = self._last_active_plan or self._preview_main_plan
        if active is not None:
            for s_idx, bl in enumerate(active.boundary_lines, start=1):
                p1, p2 = bl.start, bl.end
                sx1 = w / 2.0 + self._canvas_pan_x + (p1[0] - cx_w) * eff_scale
                sy1 = h / 2.0 + self._canvas_pan_y - (p1[1] - cy_w) * eff_scale
                sx2 = w / 2.0 + self._canvas_pan_x + (p2[0] - cx_w) * eff_scale
                sy2 = h / 2.0 + self._canvas_pan_y - (p2[1] - cy_w) * eff_scale
                d_px = self._point_to_segment_dist_px(mx, my, sx1, sy1, sx2, sy2)
                if d_px < best_dist:
                    best_dist = d_px
                    best_idx = s_idx

        self._hovered_seg_idx = best_idx
        self._redraw_preview_canvas()

    def _on_canvas_leave(self, _event: Any = None) -> None:
        if self._hover_world_pt is not None or self._hovered_seg_idx is not None:
            self._hover_world_pt = None
            self._hovered_seg_idx = None
            self._redraw_preview_canvas()

    def _render_empty_inspector(self) -> None:
        hint = (
            "Ведомость участка пока пуста.\n\n"
            "1. Загрузите PDF (Ctrl+O), фото 80-х (Ctrl+I) или выберите пример слева.\n"
            "2. Нажмите «🔍 Проверить и показать схему» (Ctrl+Enter) для мгновенной\n"
            "   проверки невязки и геометрии без запуска AutoCAD,\n"
            "   либо «Начертить участок в AutoCAD» (F5).\n"
            "3. На холсте выше работают зум колесом мыши и панорама перетаскиванием."
        )
        self._set_inspector_text(hint)
        self._redraw_preview_canvas()

    def _on_preview_mode_changed(self, _mode_label: str = "") -> None:
        self._reset_viewport_state()
        self._redraw_preview_canvas()

    def _set_preview_mode(self, mode_label: str) -> None:
        if mode_label in self.preview_modes:
            if self.preview_mode_var.get() != mode_label:
                self._reset_viewport_state()
            self.preview_mode_var.set(mode_label)
            self.preview_mode_switch.set(mode_label)
            self._redraw_preview_canvas()

    def _has_visual_source(self) -> bool:
        return bool(self.image_paths) or bool(self.pdf_path and self._pdf_needs_vision)

    def _visual_frame_count(self) -> int:
        if self.image_paths:
            return len(self.image_paths)
        if self.pdf_path and self._pdf_needs_vision:
            return max(1, int(self._pdf_total_pages or 1))
        return 0

    def _current_visual_rotation(self) -> int:
        if not self._visual_rotations:
            return 0
        idx = max(0, min(self._visual_cursor, len(self._visual_rotations) - 1))
        return int(self._visual_rotations[idx]) % 360

    def rotate_visual(self, delta_clockwise_deg: int) -> None:
        """Поворачивает текущий выбранный кадр (фото или страницу PDF) на ±90° / 180°."""
        total = self._visual_frame_count()
        if total <= 0:
            return
        while len(self._visual_rotations) < total:
            self._visual_rotations.append(0)
        idx = max(0, min(self._visual_cursor, total - 1))
        self._visual_rotations[idx] = (
            int(self._visual_rotations[idx]) + int(delta_clockwise_deg)
        ) % 360
        self._refresh_visual_bar()
        self._set_preview_mode("🖼 Исходник")

    def step_visual_frame(self, delta: int) -> None:
        """Листает загруженные фотографии или страницы PDF в предпросмотре."""
        total = self._visual_frame_count()
        if total <= 1:
            return
        self._visual_cursor = (self._visual_cursor + int(delta)) % total
        self._refresh_visual_bar()
        self._set_preview_mode("🖼 Исходник")

    def _confirm_auto_orient_dialog(self) -> bool:
        """Диалог предупреждения о дополнительном платном вызове LLM при разовом ИИ-автоповороте."""
        from tkinter import messagebox

        return bool(
            messagebox.askokcancel(
                "Платный ИИ-автоповорот (только на 1 запуск)",
                "Внимание: автоопределение поворота силами LLM выполняет ДОПОЛНИТЕЛЬНЫЙ "
                "платный Vision-запрос к API перед чтением чертежа и расходует лишние токены.\n\n"
                "Бесплатная альтернатива (0 токенов):\n"
                "• смартфонный EXIF выравнивается автоматически и бесплатно;\n"
                "• любой кадр можно повернуть вручную кнопками «⟲ -90° / ⟳ +90° / ⇅ 180°» "
                "в режиме просмотра «🖼 Исходник».\n\n"
                "Включить ИИ-автоповорот ТОЛЬКО ДЛЯ ОДНОГО ближайшего запуска?\n"
                "(Сразу после запуска галочка автоматически снимется.)",
                icon="warning",
                parent=self,
            )
        )

    def _refresh_auto_orient_warning(self) -> None:
        """Показывает или скрывает янтарное предупреждение о разовом платном ИИ-повороте."""
        if not hasattr(self, "auto_orient_warn_label"):
            return
        if bool(self.auto_orient_var.get()) and self._has_visual_source():
            self.auto_orient_warn_label.grid(
                row=2, column=0, sticky="ew", padx=10, pady=(0, 6)
            )
        else:
            self.auto_orient_warn_label.grid_remove()

    def _on_auto_orient_toggled(self) -> None:
        """Обработчик клика по галочке разового ИИ-автоповорота с обязательным предупреждением."""
        if bool(self.auto_orient_var.get()):
            if not self._confirm_auto_orient_dialog():
                self.auto_orient_var.set(False)
                self._refresh_auto_orient_warning()
                self._set_status(
                    "ИИ-автоповорот отменён — дополнительные токены не расходуются. "
                    "Используйте бесплатные кнопки ⟲ / ⟳ / 180°."
                )
                return
            self._refresh_auto_orient_warning()
            self._set_status(
                "⚠ Разовый ИИ-автоповорот включён ТОЛЬКО на 1 ближайший запуск "
                "(будет выполнен доп. платный запрос к LLM, затем опция автоматически отключится).",
                warn=True,
            )
        else:
            self._refresh_auto_orient_warning()
            self._set_status("Разовый ИИ-автоповорот выключен.")

    def _consume_one_shot_auto_orient(self, has_visual: bool) -> bool:
        """Забирает разовый флаг ИИ-автоповорота на текущий запуск и немедленно сбрасывает его в False."""
        armed = bool(self.auto_orient_var.get()) and bool(has_visual)
        if bool(self.auto_orient_var.get()):
            self.auto_orient_var.set(False)
        self._refresh_auto_orient_warning()
        return armed

    def _refresh_visual_bar(self) -> None:
        if not hasattr(self, "visual_bar"):
            return
        if not self._has_visual_source():
            self._refresh_auto_orient_warning()
            self.visual_bar.grid_remove()
            return
        total = self._visual_frame_count()
        while len(self._visual_rotations) < total:
            self._visual_rotations.append(0)
        self._visual_cursor = max(0, min(self._visual_cursor, total - 1))
        rot = self._current_visual_rotation()
        kind = "Фото" if self.image_paths else "Стр."
        self.visual_pos_label.configure(
            text=f"{kind} {self._visual_cursor + 1}/{total} · +{rot}°"
        )
        nav_state = "normal" if total > 1 else "disabled"
        self.prev_visual_button.configure(state=nav_state)
        self.next_visual_button.configure(state=nav_state)
        if self.pdf_path and not self.image_paths and total > 1:
            self.pdf_pages_box.grid(row=0, column=6, sticky="e", padx=(0, 2))
        else:
            self.pdf_pages_box.grid_remove()
        self._refresh_auto_orient_warning()
        self.visual_bar.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 6))

    def _refresh_neighbor_verify_button(self) -> None:
        if not hasattr(self, "verify_neighbors_button"):
            return
        rep = self._neighbor_report
        if rep is None or not rep.pair_reports:
            self.verify_neighbors_button.configure(
                text="🔍 Сверка с соседями",
                fg_color=THEME["btn_secondary"],
                hover_color=THEME["btn_secondary_hover"],
            )
            return
        if rep.overall_status == "ok":
            self.verify_neighbors_button.configure(
                text=rep.short_badge(),
                fg_color="#059669",
                hover_color="#047857",
            )
        elif rep.overall_status == "warn":
            self.verify_neighbors_button.configure(
                text=rep.short_badge(),
                fg_color="#d97706",
                hover_color="#b45309",
            )
        else:
            self.verify_neighbors_button.configure(
                text=rep.short_badge(),
                fg_color="#dc2626",
                hover_color="#b91c1c",
            )

    def _recompute_neighbor_verification(self) -> NeighborVerificationReport | None:
        if self._preview_main_plan is None or not self._preview_neighbor_plans:
            self._neighbor_report = None
            self._refresh_neighbor_verify_button()
            return None
        self._neighbor_report = verify_with_neighbors(
            self._preview_main_plan, self._preview_neighbor_plans
        )
        self._refresh_neighbor_verify_button()
        return self._neighbor_report

    def _append_neighbor_verification_to_status(self) -> None:
        """
        Если в сцене присутствуют и основной участок, и хотя бы один сосед,
        добавляет результат сверки в статус и журнал (а при расхождении подсвечивает!).
        """
        rep = self._neighbor_report
        if rep is None or not rep.pair_reports:
            return
        cur = str(self.status_var.get() or "").strip()
        if not rep.has_issues:
            self._log_line(rep.headline())
            if rep.headline() not in cur:
                self.status_var.set(f"{cur}  |  {rep.headline()}" if cur else rep.headline())
            return

        # Собираем первую ключевую диагностическую подсказку для мгновенного показа юзеру
        first_hint = ""
        for pr in rep.pair_reports:
            if pr.status == "ok":
                continue
            for ec in pr.shared_edges:
                if ec.status != "ok" and ec.diagnoses:
                    first_hint = (
                        f"Сосед #{pr.neighbor_index} (сторона #{ec.main_seg_index} ↔ "
                        f"#{ec.neighbor_seg_index}): {ec.diagnoses[0]}"
                    )
                    break
            if not first_hint and pr.findings:
                first_hint = f"Сосед #{pr.neighbor_index}: {pr.findings[0]}"
            if first_hint:
                break

        alert_parts: list[str] = []
        if rep.headline() not in cur:
            alert_parts.append(rep.headline())
        if first_hint and first_hint not in cur:
            alert_parts.append(f"   ↳ {first_hint}")
        self._log_line(rep.format_report())
        if alert_parts:
            block = "\n".join(alert_parts)
            new_status = f"{cur}\n{block}" if cur else block
            self.status_var.set(new_status)
        self.status_label.configure(
            text_color=THEME["err"] if rep.overall_status == "error" else THEME["warn"]
        )

    def _refresh_suspicion_badge(self) -> None:
        if not hasattr(self, "suspicion_badge_button"):
            return
        rep = self._last_suspicion_report
        if rep is None:
            self.suspicion_badge_button.configure(
                text="🔎 Форма: —",
                fg_color=THEME["btn_secondary"],
                hover_color=THEME["btn_secondary_hover"],
            )
            return
        if not rep.is_suspicious:
            self.suspicion_badge_button.configure(
                text=rep.short_badge(),
                fg_color="#059669",
                hover_color="#047857",
            )
        elif rep.status == "error":
            self.suspicion_badge_button.configure(
                text=rep.short_badge(),
                fg_color="#dc2626",
                hover_color="#b91c1c",
            )
        else:
            self.suspicion_badge_button.configure(
                text=rep.short_badge(),
                fg_color="#d97706",
                hover_color="#b45309",
            )

    def _append_suspicion_to_status(self, plan: DrawPlan | None = None) -> None:
        """Если в геометрии только что построенного участка найдена подозрительность — сразу пишет юзеру."""
        target_plan = plan or self._last_active_plan
        rep = target_plan.suspicion if target_plan is not None else self._last_suspicion_report
        if rep is None or not rep.is_suspicious:
            return
        cur = str(self.status_var.get() or "").strip()
        first_it = rep.items[0]
        alert_parts: list[str] = []
        if rep.headline() not in cur:
            alert_parts.append(rep.headline())
        first_line = f"   ↳ {first_it.title}: {first_it.detail}"
        if first_it.title not in cur:
            alert_parts.append(first_line)
        self._log_line(rep.format_report())
        if alert_parts:
            block = "\n".join(alert_parts)
            self.status_var.set(f"{cur}\n{block}" if cur else block)
        self.status_label.configure(
            text_color=THEME["err"] if rep.status == "error" else THEME["warn"]
        )

    def _open_lot_suspicion_window(self) -> None:
        """Открывает модальное окно диагностики геометрии и подозрительности активного участка."""
        rep = self._last_suspicion_report
        if rep is None:
            self._set_status(
                "Сначала разберите участок, чтобы проверить его геометрию на подозрительность."
            )
            return
        win = ctk.CTkToplevel(self)
        win.title("Диагностика геометрии участка — Title Survey AI")
        win.geometry("860x560")
        win.configure(fg_color=THEME["bg_root"])
        win.attributes("-topmost", True)
        win.grid_columnconfigure(0, weight=1)
        win.grid_rowconfigure(1, weight=1)

        head_color = (
            THEME["ok"]
            if not rep.is_suspicious
            else (THEME["err"] if rep.status == "error" else THEME["warn"])
        )
        head_card = ctk.CTkFrame(
            win,
            fg_color=THEME["bg_card"],
            corner_radius=8,
            border_width=1,
            border_color=THEME["border_card"],
        )
        head_card.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        ctk.CTkLabel(
            head_card,
            text=rep.headline(),
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=head_color,
            anchor="w",
            justify="left",
            wraplength=810,
        ).pack(fill="x", padx=12, pady=10)

        box = ctk.CTkTextbox(
            win,
            wrap="word",
            fg_color=THEME["bg_editor"],
            text_color=THEME["text_main"],
            font=ctk.CTkFont(family="Consolas", size=12),
        )
        box.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 8))
        full_txt = rep.format_report()
        box.insert("1.0", full_txt)
        target = getattr(box, "_textbox", box)
        target.configure(state="disabled")

        foot = ctk.CTkFrame(win, fg_color="transparent")
        foot.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 12))
        foot.grid_columnconfigure(0, weight=1)

        def _copy_diag() -> None:
            clip_fn = getattr(self, "clipboard_clear", None)
            append_fn = getattr(self, "clipboard_append", None)
            if callable(clip_fn) and callable(append_fn):
                clip_fn()
                append_fn(full_txt)
            self._set_status("Отчёт проверки геометрии скопирован в буфер обмена.")

        ctk.CTkButton(
            foot,
            text="📋 Скопировать диагностику",
            width=195,
            height=30,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            command=_copy_diag,
        ).grid(row=0, column=1, padx=(0, 8))
        ctk.CTkButton(
            foot,
            text="Закрыть",
            width=110,
            height=30,
            fg_color=THEME["btn_primary"],
            hover_color=THEME["btn_primary_hover"],
            command=win.destroy,
        ).grid(row=0, column=2)

    def _clear_preview_scene(self) -> None:
        self._preview_main_plan = None
        self._preview_neighbor_plans.clear()
        self._last_active_plan = None
        self._neighbor_report = None
        self._last_suspicion_report = None
        self._reset_viewport_state()
        self._refresh_neighbor_verify_button()
        self._refresh_suspicion_badge()
        self._render_empty_inspector()

    def _record_plan_for_preview(self, plan: DrawPlan, *, warnings: list[str] | None = None) -> None:
        self._last_active_plan = plan
        self._last_suspicion_report = plan.suspicion
        self._refresh_suspicion_badge()
        if plan.is_neighbor:
            # Обновляем соседа с тем же POB или добавляем нового в общую сцену от (0, 0)
            replaced = False
            for idx, existing in enumerate(self._preview_neighbor_plans):
                if math.hypot(existing.pob[0] - plan.pob[0], existing.pob[1] - plan.pob[1]) < 0.25:
                    self._preview_neighbor_plans[idx] = plan
                    replaced = True
                    break
            if not replaced:
                self._preview_neighbor_plans.append(plan)
        else:
            self._preview_main_plan = plan

        self._recompute_neighbor_verification()
        self.preview_mode_var.set("📐 Схема CAD")
        self.preview_mode_switch.set("📐 Схема CAD")
        self._set_inspector_text(self._build_inspector_report(plan, extra_warnings=warnings))
        self._redraw_preview_canvas()

    def _build_inspector_report(
        self, plan: DrawPlan, *, extra_warnings: list[str] | None = None
    ) -> str:
        desc = plan.description
        role = "СОСЕДНИЙ УЧАСТОК (NEIGHBOR_LOT)" if plan.is_neighbor else "ОСНОВНОЙ УЧАСТОК (PROPERTY_LINE)"
        lines: list[str] = [f"▸ {role}"]

        if desc.block_number or desc.lot_number or desc.borough_or_city:
            meta = " ".join(
                p
                for p in (
                    f"Block {desc.block_number}" if desc.block_number else "",
                    f"Lot {desc.lot_number}" if desc.lot_number else "",
                    f"({desc.borough_or_city})" if desc.borough_or_city else "",
                )
                if p
            )
            lines.append(f"  Кадастр:   {meta}")

        street_main = desc.main_street or "—"
        if desc.main_street_note:
            street_main += f" {desc.main_street_note}"
        lines.append(f"  Фасад:     {street_main}")
        if desc.cross_street:
            if desc.distance_to_corner_ft <= 1e-6:
                lines.append(f"  Угол:      {desc.cross_street}  (POB = 0.00, 0.00)")
            else:
                tie_dir = (
                    f" по {desc.corner_to_pob_bearing.bearing_label}"
                    if desc.corner_to_pob_bearing and desc.corner_to_pob_bearing.has_bearing
                    else ""
                )
                lines.append(
                    f"  Привязка:  {desc.distance_to_corner_ft:.2f}' от {desc.cross_street}{tie_dir}"
                )
        lines.append(
            f"  POB (0,0): ({plan.pob[0]:+.2f}, {plan.pob[1]:+.2f}) ft   |   "
            f"Площадь: {plan.area_sqft:,.1f} sq ft ({plan.area_sqft / 43560.0:.4f} ac)"
        )
        lines.append(
            f"  Периметр:  {desc.perimeter_ft:,.2f} ft   |   "
            f"Невязка: {plan.closure.distance:.4f} ft"
        )
        if plan.suspicion is not None:
            lines.append("")
            lines.append(plan.suspicion.format_report())
        lines.append("")
        lines.append(f"Стороны контура ({len(plan.boundary_lines)} отр. LINE):")
        for idx, seg in enumerate(desc.segments, start=1):
            if seg.has_bearing:
                dir_txt = seg.bearing_label
            else:
                dir_txt = seg.course_note or "относительный курс"
            ang_tag = f"  [∠ {seg.interior_angle}]" if seg.interior_angle else ""
            lines.append(f"  #{idx:<2d}  {format_distance(seg.distance_ft):>8s}   {dir_txt}{ang_tag}")

        notes_all = list(plan.notes) + list(extra_warnings or []) + list(plan.warnings)
        if notes_all:
            lines.append("")
            lines.append("Геодезические пояснения:")
            for n_line in notes_all:
                lines.append(f"  • {n_line}")

        if self._neighbor_report is not None and self._neighbor_report.pair_reports:
            lines.append("")
            lines.append("═" * 46)
            lines.append("🔍 СВЕРКА С СОСЕДНИМИ УЧАСТКАМИ")
            lines.append("═" * 46)
            lines.append(
                self._neighbor_report.format_report(
                    compact=not self._neighbor_report.has_issues
                )
            )
        return "\n".join(lines)

    def _draw_source_visual_on_canvas(self, canvas: Any, w: int, h: int) -> None:
        """Рендерит загруженное фото/страницу PDF в режиме «🖼 Исходник» с зумом, панорамой, EXIF и фильтром 80-х."""
        self._last_world_transform = None
        if not self._has_visual_source():
            canvas.create_text(
                w / 2,
                h / 2,
                text="Исходное изображение не загружено.\nНажмите «📷 Фото (JPG)…» (Ctrl+I) или «📄 PDF…» (Ctrl+O)",
                fill=THEME["text_dim"],
                font=("Segoe UI", 10),
                justify="center",
            )
            return

        total = self._visual_frame_count()
        idx = max(0, min(self._visual_cursor, total - 1))
        rot_cw = self._current_visual_rotation()
        do_enhance = bool(self.enhance_scan_var.get())
        zoom = max(0.6, min(10.0, float(self._canvas_zoom or 1.0)))

        raw_source: Path | bytes | None = None
        frame_title = ""
        try:
            if self.image_paths:
                img_p = self.image_paths[idx]
                raw_source = img_p
                frame_title = f"ФОТО {idx + 1}/{total}: {img_p.name}"
            elif self.pdf_path is not None:
                try:
                    import pymupdf as fitz
                except ImportError:
                    import fitz  # type: ignore[no-redef]
                doc = fitz.open(self.pdf_path)
                try:
                    page_i = min(idx, max(0, len(doc) - 1))
                    pdf_dpi = min(260, max(130, int(round(130 * min(zoom, 2.2)))))
                    pix = doc.load_page(page_i).get_pixmap(dpi=pdf_dpi, alpha=False)
                    raw_source = pix.tobytes("png")
                    frame_title = f"PDF СТР. {page_i + 1}/{len(doc)}: {self.pdf_path.name}"
                finally:
                    doc.close()

            if raw_source is None:
                return

            base_side = max(240, min(w - 16, h - 36))
            preview_max = min(2400, max(240, int(round(base_side * zoom))))
            img_bytes, _mime, meta = prepare_survey_image_bytes(
                raw_source,
                rotation_deg=rot_cw,
                max_side_px=preview_max,
                enhance_scan=do_enhance,
                output_format="png",
            )
            rendered_bitmap = False
            create_img_fn = getattr(canvas, "create_image", None)
            if callable(create_img_fn):
                try:
                    import io as _io
                    from PIL import Image, ImageTk

                    pil_im = Image.open(_io.BytesIO(img_bytes))
                    tk_im = ImageTk.PhotoImage(pil_im)
                    self._tk_photo_ref = tk_im
                    create_img_fn(
                        w / 2 + self._canvas_pan_x,
                        h / 2 + 8 + self._canvas_pan_y,
                        image=tk_im,
                        anchor="center",
                    )
                    rendered_bitmap = True
                except Exception:  # noqa: BLE001
                    rendered_bitmap = False

            exif_tag = " · EXIF✓" if meta.get("exif_transposed") else ""
            enh_tag = "80s+" if do_enhance else "RAW"
            zoom_tag = f"  |  {int(round(zoom * 100))}%" if abs(zoom - 1.0) > 0.03 else ""
            hud = (
                f"{frame_title}  |  {meta['width']}×{meta['height']}  |  "
                f"+{rot_cw}°{exif_tag}  |  {enh_tag}{zoom_tag}"
            )
            canvas.create_text(
                8,
                8,
                text=hud,
                anchor="nw",
                fill=THEME["warn"],
                font=("Consolas", 8, "bold"),
            )
            canvas.create_text(
                w - 8,
                h - 8,
                text="Колесо: лупа · ЛКМ: панорама · 2×клик: 1:1",
                anchor="se",
                fill=THEME["text_dim"],
                font=("Consolas", 8),
            )
            if not rendered_bitmap:
                canvas.create_text(
                    w / 2 + self._canvas_pan_x,
                    h / 2 + 6 + self._canvas_pan_y,
                    text=(
                        f"{frame_title}\n"
                        f"Размер превью: {meta['width']}×{meta['height']} px, поворот +{rot_cw}°"
                    ),
                    fill=THEME["text_main"],
                    font=("Segoe UI", 10),
                    justify="center",
                )
        except Exception as exc:  # noqa: BLE001
            canvas.create_text(
                w / 2,
                h / 2,
                text=f"Не удалось построить превью кадра:\n{exc}",
                fill=THEME["err"],
                font=("Segoe UI", 10),
                justify="center",
            )

    @staticmethod
    def _pick_nice_feet_step(raw_feet: float) -> float:
        """Подбирает аккуратный геодезический шаг линейки/сетки в футах (5', 10', 25', 50', 100'…)."""
        for step in (1.0, 2.0, 5.0, 10.0, 20.0, 25.0, 50.0, 100.0, 200.0, 250.0, 500.0, 1000.0):
            if step >= raw_feet:
                return step
        return 2000.0

    def _redraw_preview_canvas(self) -> None:
        canvas = self.preview_canvas
        if canvas is None:
            return
        try:
            canvas.delete("all")
            w = int(canvas.winfo_width() or 0)
            h = int(canvas.winfo_height() or 0)
        except Exception:  # noqa: BLE001
            return
        if w < 40 or h < 40:
            w, h = 420, 250

        if str(self.preview_mode_var.get() or "") == "🖼 Исходник":
            self._draw_source_visual_on_canvas(canvas, w, h)
            return

        plans: list[DrawPlan] = []
        if self._preview_main_plan is not None:
            plans.append(self._preview_main_plan)
        plans.extend(self._preview_neighbor_plans)
        if not plans and self._last_active_plan is not None:
            plans.append(self._last_active_plan)

        if not plans:
            self._last_world_transform = None
            canvas.create_text(
                w / 2,
                h / 2,
                text=(
                    "Здесь появится интерактивная CAD-схема участка,\n"
                    "красных линий улиц, точки POB и соседних лотов от (0, 0)\n"
                    "(колесо мыши — масштаб, зажатая ЛКМ — панорама)"
                ),
                fill=THEME["text_dim"],
                font=("Segoe UI", 10),
                justify="center",
            )
            return

        # Собираем все точки сцены для автоцентрирования и масштабирования
        world_pts: list[tuple[float, float]] = [(0.0, 0.0)]
        for pl in plans:
            world_pts.append(pl.pob)
            for bl in pl.boundary_lines:
                world_pts.append(bl.start)
                world_pts.append(bl.end)
            for rl in pl.lines:
                world_pts.append(rl.start)
                world_pts.append(rl.end)

        xs = [p[0] for p in world_pts]
        ys = [p[1] for p in world_pts]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        span_x = max(max_x - min_x, 10.0)
        span_y = max(max_y - min_y, 10.0)
        pad = 34.0
        base_scale = min(max((w - 2 * pad) / span_x, 0.05), max((h - 2 * pad) / span_y, 0.05))
        zoom = max(0.6, min(10.0, float(self._canvas_zoom or 1.0)))
        scale = base_scale * zoom
        cx_w = 0.5 * (min_x + max_x)
        cy_w = 0.5 * (min_y + max_y)
        self._last_world_transform = (cx_w, cy_w, scale, w, h)

        def to_screen(pt: tuple[float, float]) -> tuple[float, float]:
            sx = w * 0.5 + self._canvas_pan_x + (pt[0] - cx_w) * scale
            sy = h * 0.5 + self._canvas_pan_y - (pt[1] - cy_w) * scale
            return sx, sy

        # 0. Тонкая инженерная координатная сетка WCS (шаг подбирается автоматически)
        grid_step = self._pick_nice_feet_step(68.0 / max(scale, 1e-6))
        vis_min_x = cx_w + (0.0 - (w * 0.5 + self._canvas_pan_x)) / scale
        vis_max_x = cx_w + (float(w) - (w * 0.5 + self._canvas_pan_x)) / scale
        vis_min_y = cy_w - (float(h) - (h * 0.5 + self._canvas_pan_y)) / scale
        vis_max_y = cy_w - (0.0 - (h * 0.5 + self._canvas_pan_y)) / scale
        gx_val = math.floor(vis_min_x / grid_step) * grid_step
        grid_guard = 0
        while gx_val <= vis_max_x and grid_guard < 40:
            sx_g, _ = to_screen((gx_val, 0.0))
            canvas.create_line(sx_g, 0, sx_g, h, fill=THEME["cad_grid"], width=1)
            gx_val += grid_step
            grid_guard += 1
        gy_val = math.floor(vis_min_y / grid_step) * grid_step
        grid_guard = 0
        while gy_val <= vis_max_y and grid_guard < 40:
            _, sy_g = to_screen((0.0, gy_val))
            canvas.create_line(0, sy_g, w, sy_g, fill=THEME["cad_grid"], width=1)
            gy_val += grid_step
            grid_guard += 1

        # 1. Пунктирные линии красных линий улиц (STREET_ROW_AI)
        for pl in plans:
            for rl in pl.lines:
                x1, y1 = to_screen(rl.start)
                x2, y2 = to_screen(rl.end)
                canvas.create_line(
                    x1, y1, x2, y2, fill=THEME["cad_street"], width=1, dash=(4, 3)
                )

        # 2. Перекрёсток улиц (0.0, 0.0)
        ox, oy = to_screen((0.0, 0.0))
        canvas.create_line(ox - 6, oy, ox + 6, oy, fill=THEME["text_muted"], width=1)
        canvas.create_line(ox, oy - 6, ox, oy + 6, fill=THEME["text_muted"], width=1)
        canvas.create_text(
            ox + 6,
            oy + 10,
            text="(0,0)",
            anchor="nw",
            fill=THEME["text_muted"],
            font=("Consolas", 8),
        )

        # 3. Соседние участки (NEIGHBOR_LOT, голубой контур)
        for nb_plan in self._preview_neighbor_plans:
            for bl in nb_plan.boundary_lines:
                x1, y1 = to_screen(bl.start)
                x2, y2 = to_screen(bl.end)
                canvas.create_line(x1, y1, x2, y2, fill=THEME["cad_neighbor"], width=2)
            ncx, ncy = to_screen(nb_plan.centroid)
            canvas.create_text(
                ncx,
                ncy,
                text="СОСЕД",
                fill=THEME["cad_neighbor"],
                font=("Segoe UI", 8, "bold"),
            )

        # 4. Основной участок (PROPERTY_LINE, контур + узлы + подсветка наведения + POB + GAP)
        focus_plan = self._preview_main_plan or self._last_active_plan
        if focus_plan is not None:
            if math.hypot(focus_plan.pob[0], focus_plan.pob[1]) > 1e-3:
                px0, py0 = to_screen(focus_plan.pob)
                canvas.create_line(
                    ox, oy, px0, py0, fill=THEME["cad_tie"], width=1, dash=(2, 3)
                )

            seg_color = (
                THEME["cad_neighbor"] if focus_plan.is_neighbor else THEME["cad_prop"]
            )
            susp_map: dict[int, str] = {}
            if focus_plan.suspicion is not None and focus_plan.suspicion.is_suspicious:
                for s_item in focus_plan.suspicion.items:
                    col = THEME["err"] if s_item.severity == "error" else THEME["warn"]
                    for s_idx in s_item.segment_indices:
                        if s_idx not in susp_map or s_item.severity == "error":
                            susp_map[s_idx] = col

            for idx, bl in enumerate(focus_plan.boundary_lines, start=1):
                x1, y1 = to_screen(bl.start)
                x2, y2 = to_screen(bl.end)
                if idx in susp_map:
                    canvas.create_line(
                        x1,
                        y1,
                        x2,
                        y2,
                        fill=susp_map[idx],
                        width=4.5,
                        dash=(5, 3),
                    )
                is_hovered = self._hovered_seg_idx == idx
                line_col = THEME["cad_hover"] if is_hovered else seg_color
                line_w = 3.6 if is_hovered else 2.4
                canvas.create_line(x1, y1, x2, y2, fill=line_col, width=line_w)
                canvas.create_oval(
                    x2 - 2.3, y2 - 2.3, x2 + 2.3, y2 + 2.3, fill=line_col, outline=""
                )
                mx, my = 0.5 * (x1 + x2), 0.5 * (y1 + y2)
                lbl_txt = f"⚠ #{idx}" if idx in susp_map else f"#{idx}"
                lbl_col = (
                    THEME["cad_hover"]
                    if is_hovered
                    else susp_map.get(idx, THEME["text_main"])
                )
                canvas.create_text(
                    mx,
                    my - 7,
                    text=lbl_txt,
                    fill=lbl_col,
                    font=("Consolas", 8, "bold"),
                )

            px, py = to_screen(focus_plan.pob)
            canvas.create_oval(
                px - 4, py - 4, px + 4, py + 4, fill=THEME["ok"], outline=""
            )
            canvas.create_text(
                px + 6,
                py - 8,
                text="POB",
                anchor="sw",
                fill=THEME["ok"],
                font=("Consolas", 8, "bold"),
            )

            if focus_plan.gap_points is not None:
                gx, gy = to_screen(focus_plan.gap_points[0])
                canvas.create_oval(
                    gx - 7,
                    gy - 7,
                    gx + 7,
                    gy + 7,
                    outline=THEME["warn"],
                    width=2,
                )
                canvas.create_text(
                    gx + 9,
                    gy,
                    text=f"GAP {focus_plan.closure.distance:.3f}'",
                    anchor="w",
                    fill=THEME["warn"],
                    font=("Consolas", 8, "bold"),
                )

            if focus_plan.description.main_street:
                canvas.create_text(
                    10,
                    10,
                    text=focus_plan.description.main_street.upper(),
                    anchor="nw",
                    fill=THEME["sky"],
                    font=("Segoe UI", 9, "bold"),
                )

        # 5. Индикация сверки общих границ с соседями поверх схемы
        if self._neighbor_report is not None and self._neighbor_report.pair_reports:
            for pr in self._neighbor_report.pair_reports:
                for ec in pr.shared_edges:
                    mx1, my1 = to_screen(ec.main_line[0])
                    mx2, my2 = to_screen(ec.main_line[1])
                    mid_x, mid_y = 0.5 * (mx1 + mx2), 0.5 * (my1 + my2)
                    if ec.status == "ok":
                        canvas.create_text(
                            mid_x,
                            mid_y + 9,
                            text="✓ межа",
                            fill=THEME["ok"],
                            font=("Segoe UI", 8, "bold"),
                        )
                    else:
                        hl_color = (
                            THEME["err"] if ec.status == "error" else THEME["warn"]
                        )
                        nx1, ny1 = to_screen(ec.neighbor_line[0])
                        nx2, ny2 = to_screen(ec.neighbor_line[1])
                        canvas.create_line(
                            nx1, ny1, nx2, ny2, fill=hl_color, width=3, dash=(5, 3)
                        )
                        tag_parts: list[str] = []
                        if ec.angle_diff_arcsec > 1.0:
                            tag_parts.append(f"Δ∠{ec.angle_diff_dms}")
                        if ec.abs_length_diff_ft > 0.015:
                            tag_parts.append(f"ΔL{ec.abs_length_diff_ft:.2f}'")
                        if not tag_parts:
                            tag_parts.append(
                                f"сдвиг {max(ec.lateral_offset_start_ft, ec.lateral_offset_end_ft):.2f}'"
                            )
                        canvas.create_text(
                            mid_x,
                            mid_y + 11,
                            text="⚠ " + " ".join(tag_parts),
                            fill=hl_color,
                            font=("Consolas", 8, "bold"),
                        )

        # 6. Динамическая масштабная линейка в футах (левый нижний угол)
        ruler_ft = self._pick_nice_feet_step(58.0 / max(scale, 1e-6))
        ruler_px = min(max(ruler_ft * scale, 28.0), max(w * 0.42, 60.0))
        rx0, ry0 = 12.0, float(h) - 14.0
        canvas.create_line(rx0, ry0, rx0 + ruler_px, ry0, fill=THEME["text_muted"], width=2)
        canvas.create_line(rx0, ry0 - 4, rx0, ry0 + 4, fill=THEME["text_muted"], width=1.5)
        canvas.create_line(
            rx0 + ruler_px, ry0 - 4, rx0 + ruler_px, ry0 + 4, fill=THEME["text_muted"], width=1.5
        )
        canvas.create_text(
            rx0 + 0.5 * ruler_px,
            ry0 - 9,
            text=f"{ruler_ft:g} ft",
            fill=THEME["text_muted"],
            font=("Consolas", 8, "bold"),
        )

        # 7. Живой HUD координат курсора и подсвеченной стороны (правый нижний угол)
        if self._hover_world_pt is not None:
            hx, hy = self._hover_world_pt
            hud_parts = [f"XY ({hx:+.2f}', {hy:+.2f}')"]
            if (
                focus_plan is not None
                and self._hovered_seg_idx is not None
                and 1 <= self._hovered_seg_idx <= len(focus_plan.description.segments)
            ):
                h_seg = focus_plan.description.segments[self._hovered_seg_idx - 1]
                h_dir = (
                    h_seg.bearing_label
                    if h_seg.has_bearing
                    else (h_seg.course_note or "отн. курс")
                )
                hud_parts.append(
                    f"#{self._hovered_seg_idx}: {format_distance(h_seg.distance_ft)} {h_dir}"
                )
            canvas.create_text(
                w - 10,
                h - 10,
                text="  ·  ".join(hud_parts),
                anchor="se",
                fill=THEME["cad_hover"] if self._hovered_seg_idx else THEME["sky"],
                font=("Consolas", 8, "bold"),
            )
        elif abs(zoom - 1.0) > 0.03:
            canvas.create_text(
                w - 10,
                h - 10,
                text=f"Масштаб вида: {int(round(zoom * 100))}%  (2×клик → 1:1)",
                anchor="se",
                fill=THEME["text_dim"],
                font=("Consolas", 8),
            )

        # 8. Стрелка на Север в правом верхнем углу схемы
        nx_c, ny_c = w - 20, 26
        canvas.create_line(nx_c, ny_c + 10, nx_c, ny_c - 8, fill=THEME["text_muted"], width=1.5)
        canvas.create_line(nx_c - 3.5, ny_c - 3, nx_c, ny_c - 8, fill=THEME["text_muted"], width=1.5)
        canvas.create_line(nx_c + 3.5, ny_c - 3, nx_c, ny_c - 8, fill=THEME["text_muted"], width=1.5)
        canvas.create_text(nx_c, ny_c - 15, text="N", fill=THEME["text_main"], font=("Consolas", 9, "bold"))

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
        self.height_label.configure(text=f"текст {height:.3f}'")

    def is_neighbor_mode(self) -> bool:
        """True, если в переключателе выбран режим «Соседний участок»."""
        return str(self.lot_role_var.get() or "main") == "neighbor"

    def _on_lot_role_changed(self) -> None:
        if self.is_neighbor_mode():
            self.draw_button.configure(
                text="Начертить соседа в AutoCAD",
                fg_color=THEME["btn_neighbor"],
                hover_color=THEME["btn_neighbor_hover"],
            )
        else:
            self.draw_button.configure(
                text="Начертить участок в AutoCAD",
                fg_color=THEME["btn_primary"],
                hover_color=THEME["btn_primary_hover"],
            )

    def _preview_text_offline_silently(self, sample_text: str) -> None:
        """Мгновенно обновляет правую схему при выборе встроенного примера."""
        try:
            desc, warns = parse_with_regex(sample_text)
            plan = AcadBuilder(self.config_data).plan(
                desc,
                scale=self._current_scale(),
                is_neighbor=self.is_neighbor_mode(),
            )
            self._record_plan_for_preview(plan, warnings=warns)
            self._update_closure_badge(plan.closure.short_report(), plan.closure.distance)
        except Exception:  # noqa: BLE001
            pass

    def _reset_visual_sources(self) -> None:
        self.pdf_path = None
        self.image_paths = []
        self._pdf_needs_vision = False
        self._pdf_total_pages = 0
        self._visual_rotations = []
        self._visual_cursor = 0
        self._tk_photo_ref = None
        self.pdf_pages_var.set("")
        self.auto_orient_var.set(False)
        self._refresh_visual_bar()

    def _on_sample_selected(self, label: str) -> None:
        sample = self._sample_presets.get(label)
        if not sample:
            return
        self.text_box.delete("1.0", "end")
        self.text_box.insert("1.0", sample)
        self._reset_visual_sources()
        if "Сосед" in label:
            self.lot_role_var.set("neighbor")
        elif "Основной" in label:
            self.lot_role_var.set("main")
        self._on_lot_role_changed()
        self.pdf_label.configure(
            text=f"Пример: {label}",
            text_color=THEME["sky"],
        )
        self._set_status(f"Подставлен пример: {label}")
        self._preview_text_offline_silently(sample)

    def _insert_example(self) -> None:
        selected = str(self.sample_var.get() or "")
        sample = self._sample_presets.get(selected, EXAMPLE_TEXT)
        self.text_box.delete("1.0", "end")
        self.text_box.insert("1.0", sample)
        self._reset_visual_sources()
        self.pdf_label.configure(
            text=f"Пример: {selected or 'базовый'}",
            text_color=THEME["sky"],
        )
        self._preview_text_offline_silently(sample)

    def _clear_text(self) -> None:
        self._dismiss_conflict_ui()
        self.text_box.delete("1.0", "end")
        self._reset_visual_sources()
        self.pdf_label.configure(
            text="PDF не загружен",
            text_color=THEME["text_muted"],
        )
        self.description = None
        self.draw_result = None
        self._last_active_plan = None
        self._last_suspicion_report = None
        self._refresh_suspicion_badge()
        self.preview_mode_var.set("📐 Схема CAD")
        self.preview_mode_switch.set("📐 Схема CAD")
        self._update_closure_badge("Closure Error: —", None)
        self.tokens_var.set("Токены за вызов: —")
        self._set_status("Поле описания и визуальные файлы очищены.")
        self._render_empty_inspector()

    def _apply_loaded_images(self, paths: list[Path]) -> None:
        """Активирует набор загруженных фотографий (.jpg/.png/.tif/.webp/.bmp) для Vision LLM."""
        if not paths:
            return
        self._reset_visual_sources()
        self.text_box.delete("1.0", "end")
        self.image_paths = list(paths)
        self._visual_rotations = [0] * len(paths)
        self._visual_cursor = 0
        self.use_llm_var.set(True)
        self._refresh_consensus_rows()
        self._refresh_visual_bar()

        if len(paths) == 1:
            badge = f"Фото Vision: {paths[0].name}"
        else:
            badge = f"Фото Vision: {len(paths)} файл. ({paths[0].name} + ещё {len(paths) - 1})"
        self.pdf_label.configure(text=badge, text_color=THEME["warn"])
        self._set_preview_mode("🖼 Исходник")
        self._set_status(
            f"Загружено фото чертежа ({len(paths)} шт.) — включён режим Vision LLM "
            "(бесплатное выравнивание EXIF и фильтр «Чёткость 80-х» активны; "
            "платный ИИ-поворот по умолчанию выключен). "
            "При необходимости поверните кадры бесплатно кнопками ⟲ / ⟳ / 180°."
        )

    def _load_images(self) -> None:
        """Выбор одной или нескольких фотографий старого чертежа (.jpg/.png/.tif/.webp/.bmp)."""
        from tkinter import filedialog

        ext_glob = " ".join(f"*{ext}" for ext in IMAGE_EXTENSIONS)
        selected = filedialog.askopenfilenames(
            title="Выберите одну или несколько фотографий чертежа (JPG / PNG / TIFF / WEBP)",
            filetypes=[
                ("Фотографии и сканы чертежей", ext_glob),
                ("PDF и изображения", f"*.pdf {ext_glob}"),
                ("Все файлы", "*.*"),
            ],
        )
        if not selected:
            return
        chosen = [Path(p) for p in selected if str(p).strip()]
        if not chosen:
            return
        img_only = [p for p in chosen if is_image_file(p)]
        if img_only:
            self._apply_loaded_images(img_only)
            return
        if chosen[0].suffix.lower() == ".pdf":
            self._open_single_pdf(chosen[0])

    def _load_pdf(self) -> None:
        from tkinter import filedialog

        ext_glob = " ".join(f"*{ext}" for ext in IMAGE_EXTENSIONS)
        path = filedialog.askopenfilename(
            title="Выберите PDF или фото с Legal Description / чертежом Survey",
            filetypes=[
                ("PDF и фото чертежей", f"*.pdf {ext_glob}"),
                ("PDF", "*.pdf"),
                ("Фотографии", ext_glob),
                ("Все файлы", "*.*"),
            ],
        )
        if not path:
            return
        chosen = Path(path)
        if is_image_file(chosen):
            self._apply_loaded_images([chosen])
            return
        self._open_single_pdf(chosen)

    def _open_single_pdf(self, chosen: Path) -> None:
        try:
            info = inspect_pdf(chosen)
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"Не удалось прочитать PDF: {exc}", error=True)
            return
        self._reset_visual_sources()
        self.pdf_path = chosen
        self._pdf_total_pages = int(info.get("pages") or 1)
        has_legal_text = bool(info.get("has_legal_text", info["has_text"]))
        if has_legal_text:
            text = str(info["text"])
            self._pdf_needs_vision = False
            self.text_box.delete("1.0", "end")
            self.text_box.insert("1.0", text)
            self._refresh_visual_bar()
            self.pdf_label.configure(
                text=f"PDF: {self.pdf_path.name} ({len(text):,} симв.)",
                text_color=THEME["ok"],
            )
            self._set_status(f"PDF загружен: {self.pdf_path.name}")
        else:
            self.text_box.delete("1.0", "end")
            self._pdf_needs_vision = True
            self._visual_rotations = [0] * max(1, self._pdf_total_pages)
            self.use_llm_var.set(True)
            self._refresh_consensus_rows()
            self._refresh_visual_bar()
            has_photos = bool(info.get("has_raster_images"))
            tag_detail = (
                "фото/скан"
                if has_photos
                else ("чертёж Survey" if info["has_text"] else "без текста")
            )
            self.pdf_label.configure(
                text=f"PDF Vision: {self.pdf_path.name} ({info['pages']} стр., {tag_detail})",
                text_color=THEME["warn"],
            )
            self._set_preview_mode("🖼 Исходник")
            pages_hint = (
                " Для выбора отдельных листов укажите диапазон в поле «Стр.:» (напр. 1-4 или все)."
                if int(info["pages"]) > 1
                else ""
            )
            self._set_status(
                f"Загружен чертёж/фото-PDF ({self.pdf_path.name}, "
                f"{info['pages']} стр., {tag_detail}) — включён режим Vision LLM.{pages_hint}"
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
        cfg["api"]["vision_enhance_scans"] = bool(self.enhance_scan_var.get())
        # Никогда не сохраняем платный ИИ-автоповорот как настройку по умолчанию
        cfg["api"]["vision_auto_orient"] = False
        cfg["api"]["ask_on_doubt"] = bool(self.ask_on_doubt_var.get())
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
        if n_locked > 0 and self._keys_drawer_open:
            self._keys_drawer_open = False
        self._refresh_keys_drawer()
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
        self._refresh_keys_drawer()
        self._set_status(
            f"Защищённые ключи (tsai1:…) записаны в {env_path} — открытых ключей на диске нет"
        )

    def _show_log(self) -> None:
        window = ctk.CTkToplevel(self)
        window.title("Журнал операций — Title Survey AI")
        window.geometry("820x560")
        window.configure(fg_color=THEME["bg_root"])
        window.attributes("-topmost", True)
        text = ctk.CTkTextbox(
            window,
            wrap="word",
            fg_color=THEME["bg_editor"],
            text_color=THEME["text_main"],
            font=ctk.CTkFont(family="Consolas", size=12),
        )
        text.pack(fill="both", expand=True, padx=12, pady=12)
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

    def _set_status(self, message: str, error: bool = False, *, warn: bool = False) -> None:
        self.status_var.set(message)
        if error:
            color = THEME["err"]
        elif warn:
            color = THEME["warn"]
        else:
            color = THEME["text_main"]
        self.status_label.configure(text_color=color)
        self._log_line(message)

    def _update_closure_badge(self, text: str, distance_ft: float | None) -> None:
        self.closure_var.set(text)
        if distance_ft is None:
            color = THEME["text_muted"]
        elif distance_ft <= 0.015:
            color = THEME["ok"]
        elif distance_ft <= 1.0:
            color = THEME["warn"]
        else:
            color = THEME["err"]
        self.closure_label.configure(text_color=color)

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
                self._update_closure_badge("Closure Error: —", None)
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
                    try:
                        plan = AcadBuilder(self.config_data).plan(
                            outcome.data,
                            scale=self._current_scale(),
                            is_neighbor=self.is_neighbor_mode(),
                        )
                        self._record_plan_for_preview(plan, warnings=outcome.warnings)
                        self._append_suspicion_to_status(plan)
                        self._append_neighbor_verification_to_status()
                        self._update_closure_badge(
                            plan.closure.short_report(), plan.closure.distance
                        )
                    except Exception:  # noqa: BLE001
                        self._update_closure_badge(self._closure_text(outcome), None)
            elif kind == "conflict":
                self._finish_busy()
                self._present_conflict(payload)
            elif kind == "doubt":
                self._finish_busy()
                self._present_doubt(payload)
            elif kind == "batch_neighbors":
                items_done, batch_errors = payload
                self._finish_busy()
                self.batch_neighbor_results = items_done
                for out, res in items_done:
                    self._apply_parsed(out)
                    if res is not None:
                        self.draw_result = res
                        self._record_plan_for_preview(res.plan, warnings=out.warnings)
                msg_parts = [
                    f"Пакет соседей: обработано {len(items_done)} уч. в слое NEIGHBOR_LOT"
                ]
                if batch_errors:
                    msg_parts.append(f"ошибок: {len(batch_errors)} (см. лог)")
                    for err_line in batch_errors:
                        self._log_line(f"! {err_line}")
                self._set_status(
                    "  |  ".join(msg_parts),
                    error=bool(batch_errors and not items_done),
                )
                for _out, res in items_done:
                    if res is not None:
                        self._append_suspicion_to_status(res.plan)
                self._append_neighbor_verification_to_status()
                if items_done:
                    last_out, last_res = items_done[-1]
                    if last_res is not None:
                        self._update_closure_badge(
                            last_res.closure.short_report(), last_res.closure.distance
                        )
                    else:
                        self._update_closure_badge(self._closure_text(last_out), None)
            elif kind == "verify_neighbors_done":
                plans_done, verify_errors, open_popup = payload
                self._finish_busy()
                for out, nb_plan in plans_done:
                    self._apply_parsed(out)
                    self._record_plan_for_preview(nb_plan, warnings=out.warnings)
                if verify_errors:
                    for err_line in verify_errors:
                        self._log_line(f"! {err_line}")
                if self._neighbor_report is not None and self._neighbor_report.pair_reports:
                    rep = self._neighbor_report
                    self._set_status(
                        rep.headline(),
                        error=(rep.overall_status == "error"),
                        warn=(rep.overall_status == "warn"),
                    )
                    if rep.has_issues:
                        # Показываем первую конкретную причину прямо в статусе
                        self._append_neighbor_verification_to_status()
                    if self._preview_main_plan is not None:
                        self._set_inspector_text(
                            self._build_inspector_report(self._preview_main_plan)
                        )
                    if open_popup:
                        self._open_neighbor_report_window()
                elif verify_errors:
                    self._set_status(
                        f"Не удалось разобрать файлы соседей ({len(verify_errors)} ошиб.): {verify_errors[0]}",
                        error=True,
                    )
            elif kind == "done":
                self._finish_busy()
        self.after(120, self._drain_events)

    def _set_controls_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.draw_button.configure(state=state)
        self.add_neighbor_button.configure(state=state)
        self.verify_neighbors_button.configure(state=state)
        self.batch_neighbors_button.configure(state=state)
        self.parse_button.configure(state=state)
        self.save_button.configure(state=state)
        self.save_env_button.configure(state=state)

    def _finish_busy(self) -> None:
        self._busy = False
        self.progress.stop()
        self.progress.set(0)
        self._set_controls_busy(False)

    def _open_neighbor_report_window(self) -> None:
        """Открывает отдельное окно подробного акта сверки с соседними участками."""
        rep = self._neighbor_report
        if rep is None or not rep.pair_reports:
            return
        win = ctk.CTkToplevel(self)
        win.title("Акт геодезической сверки с соседями — Title Survey AI")
        win.geometry("880x620")
        win.configure(fg_color=THEME["bg_root"])
        win.attributes("-topmost", True)
        win.grid_columnconfigure(0, weight=1)
        win.grid_rowconfigure(1, weight=1)

        head_color = (
            THEME["ok"]
            if rep.overall_status == "ok"
            else (THEME["err"] if rep.overall_status == "error" else THEME["warn"])
        )
        header_frame = ctk.CTkFrame(
            win,
            fg_color=THEME["bg_card"],
            corner_radius=8,
            border_width=1,
            border_color=THEME["border_card"],
        )
        header_frame.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        header_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header_frame,
            text=rep.headline(),
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=head_color,
            anchor="w",
            justify="left",
        ).grid(row=0, column=0, sticky="ew", padx=12, pady=10)

        ctk.CTkButton(
            header_frame,
            text="➕ Добавить ещё файлы соседей…",
            width=210,
            height=28,
            fg_color=THEME["btn_neighbor"],
            hover_color=THEME["btn_neighbor_hover"],
            font=ctk.CTkFont(size=12, weight="bold"),
            command=lambda: (win.destroy(), self._pick_and_verify_neighbor_files()),
        ).grid(row=0, column=1, padx=(6, 10), pady=8)

        report_box = ctk.CTkTextbox(
            win,
            wrap="word",
            fg_color=THEME["bg_editor"],
            text_color=THEME["text_main"],
            font=ctk.CTkFont(family="Consolas", size=12),
        )
        report_box.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 8))
        full_text = rep.format_report(compact=False)
        report_box.insert("1.0", full_text)
        target = getattr(report_box, "_textbox", report_box)
        target.configure(state="disabled")

        btn_bar = ctk.CTkFrame(win, fg_color="transparent")
        btn_bar.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 12))
        btn_bar.grid_columnconfigure(0, weight=1)

        def _copy_report() -> None:
            clip_fn = getattr(self, "clipboard_clear", None)
            append_fn = getattr(self, "clipboard_append", None)
            if callable(clip_fn) and callable(append_fn):
                clip_fn()
                append_fn(full_text)
            self._set_status("Акт сверки с соседями скопирован в буфер обмена.")

        ctk.CTkButton(
            btn_bar,
            text="📋 Скопировать акт сверки",
            width=190,
            height=30,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            command=_copy_report,
        ).grid(row=0, column=1, padx=(0, 8))

        ctk.CTkButton(
            btn_bar,
            text="Закрыть",
            width=110,
            height=30,
            fg_color=THEME["btn_primary"],
            hover_color=THEME["btn_primary_hover"],
            command=win.destroy,
        ).grid(row=0, column=2)

    def _on_verify_neighbors_clicked(self) -> None:
        """
        Запускает или открывает интеллектуальную сверку с соседними участками
        (без обязательного открытия AutoCAD).
        """
        if self._busy:
            return
        # Если основной участок ещё не в сцене, но в поле есть текст и мы в режиме основного лота —
        # пробуем быстро собрать его офлайн для сверки
        if self._preview_main_plan is None:
            if self.description is not None and not self.is_neighbor_mode():
                try:
                    self._preview_main_plan = AcadBuilder(self.config_data).plan(
                        self.description,
                        scale=self._current_scale(),
                        is_neighbor=False,
                    )
                except Exception:  # noqa: BLE001
                    pass
            elif not self.is_neighbor_mode():
                raw_txt = self.text_box.get("1.0", "end").strip()
                if len(raw_txt) >= 20:
                    try:
                        desc_quick, warns_quick = parse_with_regex(raw_txt)
                        plan_quick = AcadBuilder(self.config_data).plan(
                            desc_quick,
                            scale=self._current_scale(),
                            is_neighbor=False,
                        )
                        self._record_plan_for_preview(plan_quick, warnings=warns_quick)
                    except Exception:  # noqa: BLE001
                        pass

        if self._preview_main_plan is None:
            self._set_status(
                "Сначала разберите основной участок («Проверить» или «Начертить»), "
                "чтобы было с чем сверять соседние участки.",
                error=True,
            )
            return

        if not self._preview_neighbor_plans:
            self._pick_and_verify_neighbor_files()
            return

        rep = self._recompute_neighbor_verification()
        if rep is not None:
            self.preview_mode_var.set("📐 Схема CAD")
            self.preview_mode_switch.set("📐 Схема CAD")
            self._set_inspector_text(self._build_inspector_report(self._preview_main_plan))
            self._redraw_preview_canvas()
            self._set_status(
                rep.headline(),
                error=(rep.overall_status == "error"),
                warn=(rep.overall_status == "warn"),
            )
            if rep.has_issues:
                self._append_neighbor_verification_to_status()
            self._open_neighbor_report_window()

    def _pick_and_verify_neighbor_files(self) -> None:
        """Выбирает файлы соседей (PDF / фото JPG / TXT) и сверяет их с основным участком без AutoCAD."""
        if self._busy:
            return
        from tkinter import filedialog

        ext_glob = " ".join(f"*{ext}" for ext in IMAGE_EXTENSIONS)
        paths = filedialog.askopenfilenames(
            title="Выберите файлы соседних участков для сверки границ (PDF, фото JPG/PNG/TIFF или TXT)",
            filetypes=[
                ("PDF, фото чертежей и описания", f"*.pdf {ext_glob} *.txt"),
                ("PDF", "*.pdf"),
                ("Фотографии чертежей", ext_glob),
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
        if any(is_image_file(p) for p in file_list) and not use_llm:
            use_llm = True
            self.use_llm_var.set(True)
            self._refresh_consensus_rows()
        scale = self._current_scale()
        has_visual = any(
            is_image_file(p) or p.suffix.lower() == ".pdf" for p in file_list
        )
        do_auto_orient = self._consume_one_shot_auto_orient(has_visual)

        self._busy = True
        self._set_controls_busy(True)
        self.progress.start()
        self._set_status(
            f"Сверка с соседями: разбираю {len(file_list)} файл. соседних участков…"
        )
        worker = threading.Thread(
            target=self._verify_neighbors_only_worker,
            args=(
                file_list,
                cfg,
                provider_id,
                model_name,
                api_key,
                key_secret,
                use_llm,
                scale,
                do_auto_orient,
            ),
            daemon=True,
        )
        worker.start()

    def _verify_neighbors_only_worker(
        self,
        file_list: list[Path],
        cfg: dict[str, Any],
        provider_id: str,
        model_name: str,
        api_key: str | None,
        key_secret: str,
        use_llm: bool,
        scale: float,
        do_auto_orient: bool = False,
    ) -> None:
        plans_done: list[tuple[ParseOutcome, DrawPlan]] = []
        errors: list[str] = []
        builder = AcadBuilder(cfg)
        total = len(file_list)
        do_enhance = bool(cfg.get("api", {}).get("vision_enhance_scans", True))
        for idx, fpath in enumerate(file_list, start=1):
            try:
                self._post(
                    "progress",
                    f"[{idx}/{total}] Сверка с соседями — читаю {fpath.name}…",
                )
                if is_image_file(fpath):
                    outcome = parse_legal_description_full(
                        image_paths=[fpath],
                        cfg=cfg,
                        enhance_scan=do_enhance,
                        auto_orient=do_auto_orient,
                        use_llm=True,
                        provider=provider_id,
                        model=model_name,
                        api_key=api_key,
                        key_secret=key_secret,
                        allow_regex_fallback=False,
                        progress=lambda msg, i=idx: self._post("progress", f"[{i}/{total}] {msg}"),
                    )
                elif fpath.suffix.lower() == ".pdf":
                    outcome = parse_legal_description_full(
                        pdf_path=fpath,
                        cfg=cfg,
                        enhance_scan=do_enhance,
                        auto_orient=do_auto_orient,
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
                if use_llm or is_image_file(fpath):
                    self._last_valid_secret = key_secret
                nb_plan = builder.plan(outcome.data, scale=scale, is_neighbor=True)
                plans_done.append((outcome, nb_plan))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{fpath.name}: {exc}")
        self._post("verify_neighbors_done", (plans_done, errors, True))
        self._post("done", None)

    def _load_and_draw_neighbor_files(self) -> None:
        """Пакетная загрузка нескольких PDF / JPG / TXT соседних участков и дорисовка в AutoCAD от (0, 0)."""
        if self._busy:
            return
        from tkinter import filedialog

        ext_glob = " ".join(f"*{ext}" for ext in IMAGE_EXTENSIONS)
        paths = filedialog.askopenfilenames(
            title="Выберите файлы соседних участков (PDF, фото JPG/PNG/TIFF или TXT) для слоя NEIGHBOR_LOT",
            filetypes=[
                ("PDF, фото чертежей и описания", f"*.pdf {ext_glob} *.txt"),
                ("PDF", "*.pdf"),
                ("Фотографии чертежей", ext_glob),
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
        if any(is_image_file(p) for p in file_list) and not use_llm:
            use_llm = True
            self.use_llm_var.set(True)
            self._refresh_consensus_rows()
        scale = self._current_scale()
        provider_title = self._pid_to_label.get(provider_id, provider_id)
        has_visual_neighbors = any(
            is_image_file(p) or p.suffix.lower() == ".pdf" for p in file_list
        )
        do_auto_orient = self._consume_one_shot_auto_orient(has_visual_neighbors)

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
                do_auto_orient,
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
        do_auto_orient: bool = False,
    ) -> None:
        done_items: list[tuple[ParseOutcome, DrawResult | None]] = []
        errors: list[str] = []
        builder = AcadBuilder(cfg)
        total = len(file_list)
        do_enhance = bool(cfg.get("api", {}).get("vision_enhance_scans", True))
        for idx, fpath in enumerate(file_list, start=1):
            try:
                self._post(
                    "progress",
                    f"[{idx}/{total}] Разбираю соседний участок: {fpath.name}…",
                )
                if is_image_file(fpath):
                    outcome = parse_legal_description_full(
                        image_paths=[fpath],
                        cfg=cfg,
                        enhance_scan=do_enhance,
                        auto_orient=do_auto_orient,
                        use_llm=True,
                        provider=provider_id,
                        model=model_name,
                        api_key=api_key,
                        key_secret=key_secret,
                        allow_regex_fallback=False,
                        progress=lambda msg, i=idx: self._post("progress", f"[{i}/{total}] {msg}"),
                    )
                elif fpath.suffix.lower() == ".pdf":
                    outcome = parse_legal_description_full(
                        pdf_path=fpath,
                        cfg=cfg,
                        enhance_scan=do_enhance,
                        auto_orient=do_auto_orient,
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
                if use_llm or is_image_file(fpath):
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
        if not text and not self.pdf_path and not self.image_paths:
            self._set_status(
                "Вставьте текст Legal Description или загрузите PDF / фото чертежа.",
                error=True,
            )
            return

        self._dismiss_conflict_ui()
        cfg, provider_id, model_name, api_key, key_secret = self._build_runtime_config()
        use_llm = bool(self.use_llm_var.get())
        passes_count = self.current_passes_count() if use_llm else 1
        scale = self._current_scale()
        is_neighbor = bool(force_neighbor or self.is_neighbor_mode())
        provider_title = self._pid_to_label.get(provider_id, provider_id)

        vision_images = list(self.image_paths)
        pdf_pages_spec = str(self.pdf_pages_var.get() or "").strip() or None
        vision_pdf = (
            self.pdf_path
            if (self.pdf_path and (self._pdf_needs_vision or pdf_pages_spec))
            else None
        )
        do_enhance = bool(self.enhance_scan_var.get())
        do_auto_orient = self._consume_one_shot_auto_orient(
            bool(vision_images or vision_pdf)
        )

        # Сопоставляем ручные углы поворота выбранным фото или страницам PDF
        rotations: list[int] = list(self._visual_rotations)
        if vision_pdf and not vision_images and self._pdf_total_pages > 0:
            try:
                max_p = int(cfg["api"].get("vision_max_pages") or 8)
                sel_indices = parse_page_range(
                    pdf_pages_spec, self._pdf_total_pages, max_pages=max_p
                )
                rotations = [
                    (
                        self._visual_rotations[i]
                        if 0 <= i < len(self._visual_rotations)
                        else 0
                    )
                    for i in sel_indices
                ]
            except Exception as exc:  # noqa: BLE001
                self._set_status(str(exc), error=True)
                return

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

        worker = threading.Thread(
            target=self._worker,
            args=(
                text,
                vision_pdf,
                vision_images,
                pdf_pages_spec,
                rotations or 0,
                do_enhance,
                do_auto_orient,
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
        vision_images: list[Path],
        pdf_pages_spec: str | None,
        rotation_spec: int | list[int],
        do_enhance: bool,
        do_auto_orient: bool,
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
                    image_paths=vision_images or None,
                    pdf_pages=pdf_pages_spec,
                    rotation_deg=rotation_spec,
                    enhance_scan=do_enhance,
                    auto_orient=do_auto_orient,
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
                    image_paths=vision_images or None,
                    pdf_pages=pdf_pages_spec,
                    rotation_deg=rotation_spec,
                    enhance_scan=do_enhance,
                    auto_orient=do_auto_orient,
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
            ask_on_doubt = bool(cfg.get("api", {}).get("ask_on_doubt", True))
            has_typo_suspicion = bool(outcome.typo_suspicions)
            has_risky_draw_geometry = (
                (not parse_only)
                and (
                    (plan.suspicion is not None and plan.suspicion.is_suspicious)
                    or plan.closure.distance > 0.08
                )
            )
            if ask_on_doubt and (has_typo_suspicion or has_risky_draw_geometry):
                self._post(
                    "doubt",
                    {
                        "outcome": outcome,
                        "plan": plan,
                        "cfg": cfg,
                        "provider_id": provider_id,
                        "model_name": model_name,
                        "api_key": api_key,
                        "key_secret": key_secret,
                        "use_llm": use_llm,
                        "scale": scale,
                        "parse_only": parse_only,
                        "is_neighbor": is_neighbor,
                        "pre_counted": pre_counted,
                        "round_usages": round_usages,
                        "doubt_round": 1,
                    },
                )
                self._post("done", None)
                return

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
            if vision_pdf is not None or vision_images:
                message += (
                    "\n\nПодсказка: проверьте API-ключ выбранного провайдера и убедитесь, "
                    "что выбрана сильная мультимодальная Vision-модель (hydra-gemini-3-pro, "
                    "google/gemini-2.5-pro, openai/gpt-4.1 или nvidia/nemotron-3.5-super-vl:free)."
                )
            elif use_llm:
                message += (
                    "\n\nПодсказка: проверьте API-ключ выбранного провайдера "
                    "или переключите провайдера/модель, либо выключите «ИИ-разбор (LLM)» "
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

    def _dismiss_doubt_ui(self) -> None:
        self.pending_doubt = None
        try:
            self.doubt_bar.grid_remove()
        except Exception:  # noqa: BLE001
            pass
        win = getattr(self, "doubt_window", None)
        self.doubt_window = None
        if win is not None:
            try:
                win.destroy()
            except Exception:  # noqa: BLE001
                pass

    def _dismiss_conflict_ui(self) -> None:
        self._dismiss_doubt_ui()
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

    def _build_doubt_report(self, doubt_ctx: dict[str, Any]) -> str:
        outcome: ParseOutcome = doubt_ctx["outcome"]
        plan: DrawPlan = doubt_ctx["plan"]
        d_round = int(doubt_ctx.get("doubt_round") or 1)
        parse_only = bool(doubt_ctx.get("parse_only", True))

        lines: list[str] = []
        if outcome.typo_suspicions:
            lines.append(
                f"🙋 Подозрение на опечатку / несовпадение контура (проверка #{d_round}):"
            )
            for idx, susp in enumerate(outcome.typo_suspicions, start=1):
                lines.append(f"  {idx}. {susp.short_summary()}")
                lines.append(f"     • {susp.reason_ru}")
        else:
            lines.append(
                f"🙋 Подтверждение перед рискованным построением в AutoCAD (проверка #{d_round}):"
            )
            lines.append(
                f"  • Невязка контура: {plan.closure.distance:.4f} ft"
            )

        if plan.suspicion is not None and plan.suspicion.is_suspicious:
            lines.append("")
            lines.append(plan.suspicion.format_report())

        if outcome.data.anomaly_note:
            lines.append("")
            lines.append(f"💬 Ответ/наблюдение ИИ по документу: {outcome.data.anomaly_note}")

        lines.append("")
        lines.append("Автоматическая подмена цифр документа заблокирована. Выберите действие:")
        lines.append(
            "  • «🔬 Ещё раунд LLM с фокусом на зону внимания» — модели прицельно перечитают "
            "именно спорную сторону в документе без давления «подогнать ответ»;"
        )
        lines.append(
            "  • «📄 Оставить как в документе» — сохранить прочитанные из PDF/текста цифры "
            + ("и показать схему;" if parse_only else "и начертить в AutoCAD как есть;")
        )
        first_fix = next(
            (s for s in outcome.typo_suspicions if s.repaired_data is not None),
            None,
        )
        if first_fix is not None:
            lines.append(
                f"  • «✏️ Применить исправление ({first_fix.original_value} → {first_fix.suggested_value})» — "
                "заменить значение только если вы сами сверились с чертежом и уверены в опечатке."
            )
        return "\n".join(lines)

    def _present_doubt(self, doubt_ctx: dict[str, Any]) -> None:
        outcome: ParseOutcome = doubt_ctx["outcome"]
        plan: DrawPlan = doubt_ctx["plan"]
        pre_counted = bool(doubt_ctx.get("pre_counted", False))
        round_usages = doubt_ctx.get("round_usages") or []
        if round_usages:
            self._record_round_usages(round_usages)
            doubt_ctx["round_usages"] = []
            doubt_ctx["pre_counted"] = True
            pre_counted = True

        self.pending_doubt = doubt_ctx
        self._apply_parsed(outcome, already_counted=pre_counted)
        doubt_ctx["pre_counted"] = True
        self._record_plan_for_preview(plan, warnings=outcome.warnings)
        self._update_closure_badge(plan.closure.short_report(), plan.closure.distance)

        first_fix: TypoSuspicion | None = next(
            (s for s in outcome.typo_suspicions if s.repaired_data is not None),
            None,
        )
        if outcome.typo_suspicions:
            head_txt = f"🙋 Сомнительное место: {outcome.typo_suspicions[0].short_summary()}"
        elif plan.suspicion is not None and plan.suspicion.is_suspicious:
            head_txt = f"🙋 Подтвердите перед отрисовкой в AutoCAD: {plan.suspicion.headline()}"
        else:
            head_txt = (
                f"🙋 Подтвердите перед отрисовкой в AutoCAD: невязка {plan.closure.distance:.3f} ft"
            )
        self.doubt_title_var.set(head_txt)

        if first_fix is not None:
            self.keep_verbatim_button.configure(
                text=f"📄 Оставить как в документе ({first_fix.original_value})"
            )
            self.apply_typo_fix_button.configure(
                text=f"✏️ Заменить ({first_fix.original_value} → {first_fix.suggested_value})"
            )
            self.apply_typo_fix_button.grid(row=0, column=2, padx=(0, 8), sticky="w")
        else:
            parse_only = bool(doubt_ctx.get("parse_only", True))
            self.keep_verbatim_button.configure(
                text=(
                    "📄 Оставить как есть"
                    if parse_only
                    else "📐 Всё равно начертить в AutoCAD как есть"
                )
            )
            self.apply_typo_fix_button.grid_remove()

        self.doubt_bar.grid(row=1, column=0, sticky="ew", padx=16, pady=(8, 0))
        report_txt = self._build_doubt_report(doubt_ctx)
        self._set_status(report_txt, warn=True)
        self._log_line(report_txt)
        self._open_doubt_dialog()

    def _open_doubt_dialog(self) -> None:
        if not self.pending_doubt:
            return
        ctx = self.pending_doubt
        outcome: ParseOutcome = ctx["outcome"]
        d_round = int(ctx.get("doubt_round") or 1)
        parse_only = bool(ctx.get("parse_only", True))
        first_fix: TypoSuspicion | None = next(
            (s for s in outcome.typo_suspicions if s.repaired_data is not None),
            None,
        )

        old_win = getattr(self, "doubt_window", None)
        if old_win is not None:
            try:
                old_win.destroy()
            except Exception:  # noqa: BLE001
                pass

        win = ctk.CTkToplevel(self)
        win.title(f"Проверка сомнительного места — раунд #{d_round}")
        win.geometry("880x560")
        win.configure(fg_color=THEME["bg_root"])
        win.attributes("-topmost", True)
        self.doubt_window = win

        top_banner = ctk.CTkFrame(
            win,
            fg_color=THEME["warn_bg"],
            corner_radius=10,
            border_width=1,
            border_color=THEME["warn_border"],
        )
        top_banner.pack(fill="x", padx=14, pady=(12, 8))

        ctk.CTkLabel(
            top_banner,
            text=str(self.doubt_title_var.get()),
            anchor="w",
            justify="left",
            wraplength=830,
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=THEME["warn"],
        ).pack(anchor="w", padx=14, pady=(10, 4))

        ctk.CTkLabel(
            top_banner,
            text=(
                "Система НЕ меняет спорные цифры самовольно. Вы можете отправить модели на прицельную "
                "перепроверку именно этой стороны в документе, оставить прочитанные данные как есть "
                "или вручную применить исправление."
            ),
            anchor="w",
            justify="left",
            wraplength=830,
            font=ctk.CTkFont(size=12),
            text_color=THEME["text_main"],
        ).pack(anchor="w", padx=14, pady=(0, 10))

        box = ctk.CTkTextbox(
            win,
            wrap="word",
            fg_color=THEME["bg_editor"],
            border_width=1,
            border_color=THEME["border_card"],
            text_color=THEME["text_main"],
            font=ctk.CTkFont(family="Consolas", size=12),
        )
        box.pack(fill="both", expand=True, padx=14, pady=(0, 10))
        box.insert("1.0", self._build_doubt_report(ctx))
        target_tb = getattr(box, "_textbox", box)
        target_tb.configure(state="disabled")

        btn_bar = ctk.CTkFrame(win, fg_color="transparent")
        btn_bar.pack(fill="x", padx=14, pady=(0, 12))

        ctk.CTkButton(
            btn_bar,
            text="🔬 Ещё раунд LLM с фокусом на зону внимания",
            height=38,
            fg_color=THEME["btn_amber"],
            hover_color=THEME["btn_amber_hover"],
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self.retry_doubt_with_focus,
        ).pack(side="left", fill="x", expand=True, padx=(0, 8))

        keep_lbl = (
            f"📄 Оставить как в документе ({first_fix.original_value})"
            if first_fix is not None
            else (
                "📄 Оставить схему как есть"
                if parse_only
                else "📐 Всё равно начертить в AutoCAD"
            )
        )
        ctk.CTkButton(
            btn_bar,
            text=keep_lbl,
            height=38,
            fg_color=THEME["btn_emerald"],
            hover_color=THEME["btn_emerald_hover"],
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self.confirm_doubt_keep_verbatim,
        ).pack(side="left", padx=(0, 8))

        if first_fix is not None:
            ctk.CTkButton(
                btn_bar,
                text=f"✏️ Применить ({first_fix.original_value} → {first_fix.suggested_value})",
                height=38,
                fg_color=THEME["btn_primary"],
                hover_color=THEME["btn_primary_hover"],
                font=ctk.CTkFont(size=12, weight="bold"),
                command=self.confirm_doubt_apply_fix,
            ).pack(side="left", padx=(0, 8))

        if not parse_only and first_fix is None:
            ctk.CTkButton(
                btn_bar,
                text="Отмена (не чертить)",
                height=38,
                width=150,
                fg_color=THEME["btn_secondary"],
                hover_color=THEME["btn_secondary_hover"],
                command=self.cancel_doubt_draw,
            ).pack(side="left")

    def retry_doubt_with_focus(self) -> None:
        """Запускает ещё раунд проходов LLM с инъекцией промпта на подозрительную сторону/зону."""
        if self._busy or not self.pending_doubt:
            return
        ctx = dict(self.pending_doubt)
        next_round = int(ctx.get("doubt_round") or 1) + 1
        # Если до этого шёл локальный regex, подтягиваем актуальные настройки провайдера из UI
        cfg, provider_id, model_name, api_key, key_secret = self._build_runtime_config()
        ctx["cfg"] = cfg
        ctx["provider_id"] = provider_id
        ctx["model_name"] = model_name
        ctx["api_key"] = api_key
        ctx["key_secret"] = key_secret

        self._busy = True
        self._set_controls_busy(True)
        self.progress.start()
        self._set_status(
            f"Прицельная перепроверка зоны внимания (раунд #{next_round}): "
            "отправлен фокус-запрос к LLM…"
        )
        self._log_line(
            f"--- Прицельный раунд LLM по зоне сомнения (раунд #{next_round}) ---"
        )
        worker = threading.Thread(
            target=self._retry_doubt_worker,
            args=(ctx, next_round),
            daemon=True,
        )
        worker.start()

    def _retry_doubt_worker(self, ctx: dict[str, Any], next_round: int) -> None:
        prev_outcome: ParseOutcome = ctx["outcome"]
        prev_plan: DrawPlan = ctx["plan"]
        cfg: dict[str, Any] = ctx["cfg"]
        provider_id: str = ctx["provider_id"]
        model_name: str = ctx["model_name"]
        api_key: str | None = ctx["api_key"]
        key_secret: str = ctx["key_secret"]
        scale: float = ctx["scale"]
        parse_only: bool = ctx["parse_only"]
        is_neighbor: bool = ctx["is_neighbor"]

        extra_notes: list[str] = []
        if prev_plan.suspicion is not None and prev_plan.suspicion.is_suspicious:
            for s_item in prev_plan.suspicion.items:
                extra_notes.append(f"{s_item.title}: {s_item.detail}")
        try:
            res = recheck_typo_suspicion(
                prev_outcome,
                cfg,
                primary_provider=provider_id,
                primary_model=model_name,
                primary_api_key=api_key,
                key_secret=key_secret,
                extra_attention_notes=extra_notes,
                round_number=next_round,
                progress=lambda msg: self._post("progress", msg),
            )
            self._last_valid_secret = key_secret
            if isinstance(res, ConsensusResult):
                if not res.agreed:
                    self._post(
                        "conflict",
                        {
                            "consensus": res,
                            "cfg": cfg,
                            "key_secret": key_secret,
                            "scale": scale,
                            "parse_only": parse_only,
                            "is_neighbor": is_neighbor,
                        },
                    )
                    self._post("done", None)
                    return
                new_outcome = res.selected_outcome
                round_usages: list[TokenUsage | None] = [
                    o.usage for o in res.pass_outcomes
                ]
                pre_counted = True
            else:
                new_outcome = res
                round_usages = []
                pre_counted = False

            new_plan = AcadBuilder(cfg).plan(
                new_outcome.data, scale=scale, is_neighbor=is_neighbor
            )
            still_doubtful = bool(new_outcome.typo_suspicions) or (
                (not parse_only)
                and (
                    (new_plan.suspicion is not None and new_plan.suspicion.is_suspicious)
                    or new_plan.closure.distance > 0.08
                )
            )
            if still_doubtful:
                updated_ctx = dict(ctx)
                updated_ctx["outcome"] = new_outcome
                updated_ctx["plan"] = new_plan
                updated_ctx["pre_counted"] = pre_counted
                updated_ctx["round_usages"] = round_usages
                updated_ctx["doubt_round"] = next_round
                self._post("doubt", updated_ctx)
                self._post("done", None)
                return

            self._finalize_worker_outcome(
                new_outcome,
                cfg=cfg,
                scale=scale,
                parse_only=parse_only,
                is_neighbor=is_neighbor,
                pre_counted=pre_counted,
                round_usages=round_usages,
            )
        except Exception as exc:  # noqa: BLE001
            self._post("error", f"Ошибка прицельной перепроверки зоны сомнения: {exc}")
            self._post("done", None)

    def confirm_doubt_keep_verbatim(self) -> None:
        """Оставляет данные строго как в документе (без автоисправления цифр) и завершает действие."""
        if self._busy or not self.pending_doubt:
            return
        ctx = dict(self.pending_doubt)
        outcome: ParseOutcome = ctx["outcome"]
        first_fix: TypoSuspicion | None = next(
            (s for s in outcome.typo_suspicions if s.repaired_data is not None),
            None,
        )
        if first_fix is not None:
            note = (
                f"✓ Решение пользователя: оставлено исходное значение документа "
                f"«{first_fix.original_value}» (сторона #{first_fix.segment_index}) без подмены"
            )
        else:
            note = "✓ Решение пользователя: контур принят без изменений по документу"
        confirmed_outcome = ParseOutcome(
            data=outcome.data,
            source=outcome.source,
            warnings=[note, *outcome.warnings],
            usage=outcome.usage,
            provider=outcome.provider,
            provider_label=outcome.provider_label,
            model=outcome.model,
            llm_mode=outcome.llm_mode,
            typo_suspicions=[],
            prepared_text=outcome.prepared_text,
            prepared_images=outcome.prepared_images,
            base_warnings=outcome.base_warnings,
        )
        self._dismiss_doubt_ui()
        self._complete_resolved_outcome(confirmed_outcome, ctx, pre_counted=True)

    def confirm_doubt_apply_fix(self) -> None:
        """Применяет предложенное исправление опечатки ТОЛЬКО по явному нажатию пользователя."""
        if self._busy or not self.pending_doubt:
            return
        ctx = dict(self.pending_doubt)
        outcome: ParseOutcome = ctx["outcome"]
        first_fix: TypoSuspicion | None = next(
            (s for s in outcome.typo_suspicions if s.repaired_data is not None),
            None,
        )
        if first_fix is None or first_fix.repaired_data is None:
            self.confirm_doubt_keep_verbatim()
            return
        note = (
            f"✏️ По явному решению пользователя исправлена сторона #{first_fix.segment_index}: "
            f"«{first_fix.original_value}» → «{first_fix.suggested_value}» "
            f"(невязка {first_fix.closure_before_ft:.3f}' → {first_fix.closure_after_ft:.3f}')"
        )
        cleaned_warns = [
            w for w in outcome.warnings if first_fix.reason_ru not in w
        ]
        fixed_outcome = ParseOutcome(
            data=first_fix.repaired_data,
            source=outcome.source,
            warnings=[note, *cleaned_warns],
            usage=outcome.usage,
            provider=outcome.provider,
            provider_label=outcome.provider_label,
            model=outcome.model,
            llm_mode=outcome.llm_mode,
            typo_suspicions=[],
            prepared_text=outcome.prepared_text,
            prepared_images=outcome.prepared_images,
            base_warnings=outcome.base_warnings,
        )
        self._dismiss_doubt_ui()
        self._complete_resolved_outcome(fixed_outcome, ctx, pre_counted=True)

    def cancel_doubt_draw(self) -> None:
        """Отменяет рискованную отрисовку в AutoCAD, оставляя схему предпросмотра."""
        if self._busy or not self.pending_doubt:
            return
        self._dismiss_doubt_ui()
        self._set_status(
            "Отрисовка в AutoCAD отменена пользователем; схема оставлена в предпросмотре.",
            warn=True,
        )

    def _present_conflict(self, conflict_ctx: dict[str, Any]) -> None:
        self._dismiss_doubt_ui()
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

        self.conflict_bar.grid(row=0, column=0, sticky="ew", padx=16, pady=(8, 0))
        self._update_closure_badge(
            "Closure Error по проходам:  "
            + "   |   ".join(
                f"#{i}: {consensus.pass_closure_ft(i):.4f} ft"
                for i in range(1, n_passes + 1)
            ),
            0.25,
        )
        self._set_status(consensus.discrepancy_report(), warn=True)
        self._set_inspector_text(consensus.discrepancy_report())
        self._open_conflict_dialog()

    def _open_conflict_dialog(self) -> None:
        """Открывает/обновляет окно наглядного сравнения ответов моделей и выбора решения."""
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
        win.title(f"Сравнение проходов LLM — раунд #{consensus.round_number}")
        win.geometry("900x610")
        win.configure(fg_color=THEME["bg_root"])
        win.attributes("-topmost", True)
        self.conflict_window = win

        top_banner = ctk.CTkFrame(
            win,
            fg_color=THEME["warn_bg"],
            corner_radius=10,
            border_width=1,
            border_color=THEME["warn_border"],
        )
        top_banner.pack(fill="x", padx=14, pady=(12, 8))

        ctk.CTkLabel(
            top_banner,
            text=(
                f"⚠ Ответы {len(consensus.pass_outcomes)} независимых проходов разошлись "
                f"(раунд #{consensus.round_number}, отличий: {len(consensus.discrepancies)})"
            ),
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=THEME["warn"],
        ).pack(anchor="w", padx=14, pady=(10, 2))

        ctk.CTkLabel(
            top_banner,
            text=(
                "Сверьте спорные строки по исходному документу и выберите правый проход ниже, "
                "либо нажмите «🔄 Попробуй разобраться ещё раз» — модели получат задание "
                "прицельно перепроверить именно спорные места."
            ),
            anchor="w",
            justify="left",
            wraplength=840,
            font=ctk.CTkFont(size=12),
            text_color=THEME["text_main"],
        ).pack(anchor="w", padx=14, pady=(0, 10))

        # Наглядные карточки каждого прохода с подсветкой невязки
        cards_row = ctk.CTkFrame(win, fg_color="transparent")
        cards_row.pack(fill="x", padx=14, pady=(0, 8))
        n_passes = len(consensus.pass_outcomes)
        for p_idx in range(1, n_passes + 1):
            cards_row.grid_columnconfigure(p_idx - 1, weight=1)
            out = consensus.pass_outcomes[p_idx - 1]
            err_ft = consensus.pass_closure_ft(p_idx)
            is_clean = err_ft <= 0.015
            p_card = ctk.CTkFrame(
                cards_row,
                fg_color=THEME["bg_card"],
                corner_radius=8,
                border_width=1,
                border_color=THEME["ok"] if is_clean else THEME["border_card"],
            )
            p_card.grid(
                row=0,
                column=p_idx - 1,
                sticky="ew",
                padx=(0 if p_idx == 1 else 5, 0 if p_idx == n_passes else 5),
            )
            ctk.CTkLabel(
                p_card,
                text=f"Проход #{p_idx}  •  {out.provider_label or out.provider}",
                font=ctk.CTkFont(size=12, weight="bold"),
                text_color=THEME["sky"],
            ).pack(anchor="w", padx=10, pady=(8, 1))
            ctk.CTkLabel(
                p_card,
                text=str(out.model or "—"),
                font=ctk.CTkFont(size=12),
                text_color=THEME["text_main"],
            ).pack(anchor="w", padx=10)
            ctk.CTkLabel(
                p_card,
                text=(
                    f"Сторон: {len(out.data.segments)}  |  "
                    f"Невязка: {err_ft:.4f} ft"
                ),
                font=ctk.CTkFont(size=12, weight="bold"),
                text_color=THEME["ok"] if is_clean else THEME["warn"],
            ).pack(anchor="w", padx=10, pady=(4, 6))
            ctk.CTkButton(
                p_card,
                text=f"✅ Выбрать Проход #{p_idx}",
                height=30,
                fg_color=THEME["btn_emerald"],
                hover_color=THEME["btn_emerald_hover"],
                command=lambda idx=p_idx: self.resolve_conflict_with_pass(idx),
            ).pack(fill="x", padx=10, pady=(0, 10))

        box = ctk.CTkTextbox(
            win,
            wrap="word",
            fg_color=THEME["bg_editor"],
            border_width=1,
            border_color=THEME["border_card"],
            text_color=THEME["text_main"],
            font=ctk.CTkFont(family="Consolas", size=12),
        )
        box.pack(fill="both", expand=True, padx=14, pady=(0, 10))
        box.insert("1.0", consensus.discrepancy_report())
        target_tb = getattr(box, "_textbox", box)
        target_tb.configure(state="disabled")

        btn_bar = ctk.CTkFrame(win, fg_color="transparent")
        btn_bar.pack(fill="x", padx=14, pady=(0, 12))

        ctk.CTkButton(
            btn_bar,
            text="🔄 Попробуй разобраться ещё раз (прицельно проверить спорные места)",
            height=38,
            fg_color=THEME["btn_amber"],
            hover_color=THEME["btn_amber_hover"],
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self.retry_conflict_resolution,
        ).pack(side="left", fill="x", expand=True)

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
            try:
                plan = AcadBuilder(ctx["cfg"]).plan(
                    outcome.data,
                    scale=ctx["scale"],
                    is_neighbor=ctx["is_neighbor"],
                )
                self._record_plan_for_preview(plan, warnings=outcome.warnings)
                self._update_closure_badge(
                    plan.closure.short_report(), plan.closure.distance
                )
            except Exception:  # noqa: BLE001
                self._update_closure_badge(self._closure_text(outcome), None)
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
                "user_confirmed": True,
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
        user_confirmed: bool = False,
    ) -> None:
        try:
            builder = AcadBuilder(cfg)
            plan = builder.plan(outcome.data, scale=scale, is_neighbor=is_neighbor)
            if (
                not user_confirmed
                and bool(cfg.get("api", {}).get("ask_on_doubt", True))
                and bool(outcome.typo_suspicions)
            ):
                self._post(
                    "doubt",
                    {
                        "outcome": outcome,
                        "plan": plan,
                        "cfg": cfg,
                        "provider_id": str(cfg.get("api", {}).get("provider") or "hydra"),
                        "model_name": str(cfg.get("api", {}).get("model") or "glm-5.2"),
                        "api_key": None,
                        "key_secret": self._last_valid_secret,
                        "use_llm": True,
                        "scale": scale,
                        "parse_only": parse_only,
                        "is_neighbor": is_neighbor,
                        "pre_counted": pre_counted,
                        "round_usages": round_usages or [],
                        "doubt_round": 1,
                    },
                )
                self._post("done", None)
                return
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
        self._record_plan_for_preview(result.plan, warnings=outcome.warnings)
        for info_note in result.plan.notes:
            self._log_line(f"• {info_note}")
        summary_text = result.summary()
        if outcome.warnings:
            summary_text += "\n" + "\n".join(f"• {w}" for w in outcome.warnings)
        self._set_status(summary_text)
        self._append_suspicion_to_status(result.plan)
        self._append_neighbor_verification_to_status()
        self._update_closure_badge(
            result.closure.short_report(), result.closure.distance
        )


def main() -> None:
    app = SurveyApp()
    app.mainloop()


if __name__ == "__main__":
    main()
