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
  * три колонки Spectrum: слева источники и роль участка, в центре живой CAD-холст,
    справа расширяемая ведомость, проверки, соседи и журнал;
  * встроенная панель и детальное окно разрешения конфликтов проходов LLM
    (выбор правой модели человеком или кнопка «🔄 Попробуй разобраться ещё раз»);
  * цветовая индикация невязки (Closure Error), счётчик токенов за вызов и за сессию,
    окно полного лога.

COM-вызовы AutoCAD и HTTP-запросы к LLM выполняются в фоновых потоках, а обновление
виджетов происходит строго в главном потоке Tkinter через очередь событий.

Запуск:  python main.py
"""

from __future__ import annotations

import base64
import copy
import datetime as _dt
import dataclasses
from datetime import datetime
import json
import math
import os
import queue
import re
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

import app_updater
from neighbour_slide import slide_off_overlap

#: Как найден сдвиг соседа — для журнала развода.
_SLIDE_METHOD_TEXT = {
    "3.88": "обычный сдвиг, как в 3.88",
    "corner": "соединение углов",
    "side": "сдвиг вдоль стороны соседа",
}
from acad_builder import (
    AcadBuilder,
    AcadBuilderError,
    AcadConnectionError,
    DrawPlan,
    make_print_draft,
    DrawResult,
    FeatureLabelResult,
    LotSuspicionReport,
    NeighborVerificationReport,
    SuspicionItem,
    compute_pob_point,
    format_distance,
    house_label_height,
    verify_with_neighbors,
    translate_plan_in_place,
)
from conflict_resolver import (
    AdvisorReply,
    ConflictAnalysis,
    LotInput,
    analyse_conflicts,
    apply_changes,
    fix_changes,
)
from feature_labeler import feature_settings
from version import APP_STAMP, APP_VERSION
import log_mode
import start_source
from llm_parser import (
    ASPENWALL_SAMPLES,
    legal_to_step0_text,
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
    PDFError,
    ParseOutcome,
    TokenUsage,
    TypoSuspicion,
    ask_llm_json,
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
    resolve_api_target,
    run_multipass_consensus,
    save_config,
    save_protected_env,
)
import browser_save
import deed_intake
import deed_store
import nyc_portal
import ocr_pages
import rcclerk
from description_check import (
    STATUS_FAILED, STATUS_OK, STATUS_WARNING, accepted_without_ai, figure_line, reading_summary,
)
from pdf_set import (
    CLOSURE_LIMIT_FT,
    PdfSetItem,
    PdfSetJob,
    PdfSetResult,
    describe_exc,
    format_pages,
    make_item,
    make_job,
    pages_from_spec,
    read_page_boxes,
    ocr_scan_once,
    SCAN_OCR_DPI,
    set_signature,
)

APP_TITLE = "Title Survey AI — Legal Description → AutoCAD"
APP_GEOMETRY = "1440x940"
#: Версия программы — из version.py (одна на окно, отчёт Шага 2 и ведомость).
VERSION = APP_VERSION
RESULT_PANEL_DEFAULT_WIDTH = 384
#: Размер шрифта журнала и отчёта Шага 2 (остальные тексты правой панели — 10–11).
LOG_FONT_SIZE = 13
RESULT_PANEL_MIN_WIDTH = 340
CENTER_PANEL_MIN_WIDTH = 260
RESULT_SPLITTER_WIDTH = 12

# Глобальные вкладки окна: шаги технологической цепочки Title Survey.
# Шаг 0 стоит перед Шагом 1: сначала документы участка, потом посадка границы.
STEP_TAB_DEEDS = "Шаг 0. Документы участка"
STEP_TAB_BOUNDARY = "Шаг 1. Граница участка (Legal Description)"
STEP_TAB_LABELS = "Шаг 2. Автоподписание объектов (D-TOPO-TEXT)"
STEP_TABS = (STEP_TAB_DEEDS, STEP_TAB_BOUNDARY, STEP_TAB_LABELS)
#: Роль файла, который человек докачал сам: наш участок или сосед.
STEP0_MANUAL_ROLES = ("Наш участок", "Сосед")
STEP0_EMPTY_REPORT = (
    "Отчёт появится после сбора.\n\n"
    "1. Введите адрес и нажмите «Найти по адресу + анализ только карты»: портал откроется "
    "в текущем окне Chrome/Edge; локальный OCR прочитает Borough, Block и Lot, а Vision получит "
    "только обрезку tax map для поиска соседей.\n"
    "2. Проверьте Block, Lot и соседей слева/справа; поля можно исправить вручную. "
    "Каждый сосед получает роль «сосед» и файл, например LOT 30 NEW.pdf. "
    "Если портал/ИИ недоступен, значения можно ввести самостоятельно.\n"
    "3. Нажмите «Собрать описания»: программа откроет список документов Richmond "
    "County Clerk, возьмёт Deed (новее 2015 года; иначе свежую Mortgage с 2015 года), скачает свежий и старый "
    "документ нашего участка, найдёт страницы описания и положит всё в папку "
    "01_описания вместе с паспортами и _manifest.json.\n\n"
    "Если сайт закрыт проверкой Cloudflare, программа открывает ссылку в браузере "
    "и ждёт скачанный файл; последняя ступень — положить PDF самому и нажать "
    "«Забрать свежий PDF из «Загрузок»»."
)
STEP2_EMPTY_REPORT = (
    "Отчёт появится после запуска.\n\n"
    "1. Откройте в AutoCAD чертёж, в котором уже посажена граница участка (Шаг 1).\n"
    "2. При необходимости введите текст для основного дома.\n"
    "3. Нажмите «Автопоиск участка и подписание объектов» либо укажите границу в AutoCAD.\n\n"
    "Заметки полевого чертёжника переводятся в формулировки заказчика (WOOD PORCH → FRAME DECK, "
    "PAVERS → DECO. PAVERS, CHAIN LINK GATE → C.L.F. / GATE) и ставятся в слой D-TOPO-TEXT.\n"
    "В отчёте будет показано, сколько объектов найдено по заметкам, по кодам точек "
    "(POINT_LABELS), по именам слоёв и сколько подписано."
)


def normalize_house_text(value: Any) -> str:
    """
    Текст дома из поля ввода: « / » и «|» разделяют строки MText,
    лишние пробелы убираются. Пустая строка — дом не подписывается.
    """
    raw = str(value or "").replace("\r", "\n").replace("\\P", "\n").replace("|", "\n")
    raw = re.sub(r"\s+/\s+", "\n", raw)
    lines = [" ".join(line.split()) for line in raw.split("\n")]
    return "\n".join(line for line in lines if line)

# Палитра Dark CAD Studio v1.0 (Spectrum-редизайн, slate-плоскости, инженерная сетка)
THEME: dict[str, str] = {
    "bg_root": "#0a111a",
    "bg_header": "#0f1926",
    "bg_card": "#0d1622",
    "bg_inset": "#0d1622",
    "bg_editor": "#080e16",
    "bg_field": "#020617",
    "border_card": "#23354d",
    "border_subtle": "#1e293b",
    "border_active": "#3b82f6",
    "text_main": "#eaf1fb",
    "text_muted": "#94a3b8",
    "text_dim": "#475569",
    "blue_soft_bg": "#0c1c33",
    "blue_soft_fg": "#bfdbfe",
    "seg_sel": "#334155",
    "side_label": "#a7b6c9",
    "grid_axis": "#4d6381",
    "btn_primary": "#2563eb",
    "btn_primary_hover": "#1d4ed8",
    "btn_neighbor": "#0d9488",
    "btn_neighbor_hover": "#0f766e",
    "btn_secondary": "#1c2c40",
    "btn_secondary_hover": "#283d58",
    "btn_amber": "#b45309",
    "btn_amber_hover": "#d97706",
    "btn_emerald": "#059669",
    "btn_emerald_hover": "#10b981",
    "badge_v1_bg": "#05251b",
    "badge_v1_border": "#14543c",
    "badge_v1_fg": "#6ee7b7",
    "ok": "#34d399",
    "ok_bg": "#05251b",
    "warn": "#fbbf24",
    "warn_bg": "#231708",
    "warn_border": "#92400e",
    "err": "#f87171",
    "err_border": "#b91c1c",
    "sky": "#7dd3fc",
    "cad_grid": "#132235",
    "cad_grid_minor": "#101c2c",
    "cad_prop": "#f87171",
    "cad_neighbor": "#22d3ee",
    "cad_street": "#475569",
    "cad_street_text": "#7dd3fc",
    "cad_tie": "#fbbf24",
    "cad_hover": "#fde047",
    "cad_origin": "#94a3b8",
    "icon_purple": "#a78bfa",
    "teal_soft_bg": "#042f2e",
    "teal_soft_fg": "#5eead4",
}


def scale_options(cfg: dict[str, Any]) -> list[str]:
    """Список подписей масштаба из конфига -> ['1"=10\'', …]."""
    values = cfg.get("drawing", {}).get("available_scales") or [10, 16, 20, 30, 40, 50, 60]
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


def ocr_closure_note(plan: Any, limit: float = CLOSURE_LIMIT_FT) -> tuple[str, str]:
    """
    Что сказать про участок, чей текст получен локальным OCR.

    Возвращает ``(куда, текст)``: ``"issue"`` — повод проверить распознавание,
    ``"note"`` — просто пояснение, ``""`` — говорить нечего.

    Тонкость, на которой мы уже обожглись: большая невязка не всегда означает
    ошибку чтения цифр. Если в описании есть сторона без румба («southerly along
    the easterly line of …»), контур и не должен замыкаться — это документ так
    написан, и румб закрывает правило 1 «румб из замыкания». Гнать человека в
    платный Vision в таком случае бессмысленно: цифры прочитаны верно.
    """
    distance = float(getattr(getattr(plan, "closure", None), "distance", 0.0) or 0.0)
    if distance <= limit:
        return "", ""
    description = getattr(plan, "description", None)
    segments = list(getattr(description, "segments", ()) or ())
    gaps = [index + 1 for index, seg in enumerate(segments) if not getattr(seg, "has_bearing", True)]
    if gaps:
        sides = ", ".join(f"#{number}" for number in gaps)
        return "note", (
            f"распознано локальным OCR; невязка {distance:.2f} ft — от стороны без румба "
            f"({sides}): так написано в документе, её закрывает правило «румб из замыкания». "
            "Распознавание здесь ни при чём, платный ИИ не нужен"
        )
    return "issue", (
        f"распознано локальным OCR, невязка {distance:.2f} ft (больше {limit} ft) — "
        "вероятно, ошибка чтения цифр; переключите файл на ИИ (Vision) и сравните страницы"
    )


_NO_DISTANCE_WARNING = "расстояние от угла до точки начала не найдено"


def _lacks_corner_distance(warnings: Any) -> bool:
    """Парсер не нашёл расстояние до угла: участок построен от угла улицы (ошибка, не шум)."""
    return any(_NO_DISTANCE_WARNING in str(item) for item in (warnings or ()))


def _ai_key_status(
    cfg: dict[str, Any], provider_id: str, model_name: str, api_key: str | None, key_secret: str,
) -> tuple[bool, str]:
    """
    Есть ли ключ, которым ИИ реально сможет ответить: тот же путь, что у самого вызова.

    Ключ берётся из поля окна, из сохранённых настроек провайдера, из окружения и .env
    (``resolve_api_target``). Раньше гейт смотрел только на поле и пароль, и ключ из настроек
    не запускал ИИ, хотя запрос бы прошёл. Возвращает (есть, причина_если_нет).
    """
    try:
        target = resolve_api_target(
            cfg, provider=provider_id, model=model_name, api_key=api_key, key_secret=key_secret,
        )
    except Exception as exc:  # noqa: BLE001 - защищённый ключ не открылся (нет пароля или он неверный)
        return False, f"ключ провайдера не открылся: {describe_exc(exc)}"
    if str(target.get("api_key") or "").strip():
        return True, ""
    return False, "не задан API-ключ выбранного провайдера (ни в поле, ни в настройках, ни в окружении)"


#: Ранг статуса проверки описания: больше — лучше. Сначала сравнивается статус, потом фигура.
_STATUS_RANKS = {STATUS_OK: 2, STATUS_WARNING: 1, STATUS_FAILED: 0}
_STATUS_OK_RANK = _STATUS_RANKS[STATUS_OK]


def _check_status_rank(outcome: Any, plan: Any = None) -> int:
    """
    Ранг итога проверки. Описание без расстояния до угла — не ``ok``, даже если стороны сошлись:
    такой кеш или текст перераспознаётся (правило из c4df930).
    """
    check = getattr(outcome, "check", None)
    rank = _STATUS_RANKS.get(getattr(check, "status", STATUS_FAILED), 0)
    warnings = list(getattr(outcome, "warnings", None) or ()) + list(getattr(plan, "warnings", None) or ())
    if _lacks_corner_distance(warnings):
        rank = min(rank, _STATUS_RANKS[STATUS_WARNING])
    return rank


def _accepted_without_ai(candidate: Any) -> bool:
    """Вариант скана принят без ИИ: статус ok или не хватает только расстояния до угла."""
    if candidate is None:
        return False
    return candidate.status_rank >= _STATUS_OK_RANK or accepted_without_ai(
        getattr(candidate.outcome, "check", None)
    )


@dataclasses.dataclass
class _ScanCandidate:
    """Один вариант текста скана (кеш, OCR или ответ ИИ) с итогом проверки."""

    outcome: Any
    plan: Any
    issues: list[str]
    pre_counted: bool
    shape_rank: tuple
    status_rank: int

    @property
    def rank(self) -> tuple:
        return (self.status_rank,) + tuple(self.shape_rank)


class SurveyApp(ctk.CTk):
    """Главное окно приложения (Dark CAD Studio UI)."""

    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.configure(fg_color=THEME["bg_root"])

        self.title(f"{APP_TITLE}  v{VERSION} ({APP_STAMP})")
        self.geometry(APP_GEOMETRY)
        self.minsize(1100, 760)
        # Открываемся на весь экран (окно развёрнуто; кнопки свернуть/закрыть остаются).
        self.after_idle(self._open_maximized)

        # По умолчанию окно не закреплено поверх AutoCAD и других приложений.
        self.attributes("-topmost", False)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.config_data: dict[str, Any] = load_config()
        self.pdf_path: Path | None = None
        self.neighbor_pdf_paths: list[Path] = []
        self._pending_neighbor_pdfs: set[Path] = set()
        # Ошибки пакета и повторного разбора переживают переключение источника:
        # старый план на схеме нельзя принять за успешно обновлённый.
        self._pending_neighbor_files: set[Path] = set()
        self._neighbor_parsed_pages: dict[Path, str | None] = {}
        # Пустой ввод (None) не означает успешный расчёт. Ошибка основного
        # участка не снимается разбором другого, соседнего источника.
        self._input_incomplete = False
        self._main_analysis_pending = False
        self._neighbor_text_pending = False
        self.image_paths: list[Path] = []
        # Роль файлов выбирается отдельно от переключателя роли текстового ввода.
        self._visual_source_role: str | None = None
        self._visual_source_revision = 0
        # Набор PDF: наш участок и соседи одним выбором, страницы по каждому файлу.
        self.pdf_set: list[PdfSetItem] = []
        self._pdf_set_dialog: Any | None = None
        # Какие участки на схеме поставлены набором (их набор и обновляет).
        self._pdf_set_main_plan: DrawPlan | None = None
        self._pdf_set_scene_neighbors: set[Path] = set()
        self._pdf_set_main_pending = False
        self._pdf_set_report_ready = False
        self._pdf_set_report_window: Any | None = None
        # Сверка описаний между собой: находки по данным соседей и ответ ИИ-советника.
        self._pdf_set_analysis: ConflictAnalysis | None = None
        self._pdf_set_advice: dict[str, AdvisorReply] = {}
        #: Склейка по карте: режим ("turn" — перенос и поворот, "shift" — только перенос),
        #: приложенная карта TAX_MAP (имя, data URL) и соединения, которые применятся после разбора.
        self._pdf_set_join_mode: str = "turn"
        self._pdf_set_tax_map: tuple[str, str] | None = None
        self._pdf_set_tax_button: Any | None = None
        self._pending_join_accept: list[Any] = []
        self._ready_source_signature: tuple[Any, ...] | None = None
        self._running_source_signature: tuple[Any, ...] | None = None
        self._running_source_path: Path | None = None
        self._running_source_label: str | None = None
        self._running_batch_attached = False
        self._open_neighbor_report_after_parse = False
        self._text_source_label = "текст"
        self._text_source_snapshot: str | None = None
        self._text_source_path: Path | None = None
        self._text_source_role: str | None = None
        self._running_pdf_pages: str | None = None
        self._pdf_needs_vision: bool = False
        self._loaded_pdf_text: str = ""
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
        self.batch_neighbor_results: list[Any] = []
        self._session_llm_calls = 0
        self._session_prompt_tokens = 0
        self._session_completion_tokens = 0
        self._busy = False
        self._closing = False
        self._session_windows: list[Any] = []
        # Поколение отделяет события старых заданий после полного сброса.
        self._generation = 0
        self._worker_context = threading.local()
        self._draw_lock = threading.Lock()
        # Шаг 2: автоподписание объектов (вторая глобальная вкладка)
        self.step_tab_var = ctk.StringVar(value=STEP_TAB_BOUNDARY)
        # Шаг 0: документы участка. Соседи вводятся руками и получают роль «сосед».
        self.step0_address_var = ctk.StringVar(value="")
        self.step0_borough_var = ctk.StringVar(value="")
        self.step0_block_var = ctk.StringVar(value="")
        self.step0_lot_var = ctk.StringVar(value="")
        self.step0_neighbours_var = ctk.StringVar(value="")
        self.step0_folder_var = ctk.StringVar(value="")
        self.step0_old_neighbour_var = ctk.BooleanVar(value=False)
        self.step0_fast_mode_var = ctk.BooleanVar(value=False)
        #: Пауза «быстрой работы» в секундах, от 1 до 5 (поле рядом с галочкой).
        self.step0_fast_pause_var = ctk.StringVar(value=str(int(nyc_portal.FAST_SCREEN_CHECK_PAUSE_SEC)))
        self.step0_ocr_dpi_var = ctk.StringVar(
            value=str((self.config_data.get("ocr") or {}).get("dpi", ocr_pages.DEFAULT_DPI))
        )
        self.step0_manual_role_var = ctk.StringVar(value=STEP0_MANUAL_ROLES[0])
        self.step0_manual_lot_var = ctk.StringVar(value="")
        self._step0_lines: list[tuple[str, str]] = []
        self._step0_portal_running = False
        self._step0_portal_restore_topmost: bool | None = None
        self._step0_running = False
        self._step0_outcome: deed_intake.IntakeOutcome | None = None
        self._step0_stop = threading.Event()
        self.house_text_var = ctk.StringVar(value="")
        self.feature_text_height_var = ctk.StringVar(value="")
        self.skip_existing_labels_var = ctk.BooleanVar(
            value=bool(feature_settings(self.config_data).get("skip_existing_labels", True))
        )
        self.hide_field_notes_var = ctk.BooleanVar(
            value=bool(
                feature_settings(self.config_data).get("hide_field_notes_after_labeling", True)
            )
        )
        self.feature_label_result: FeatureLabelResult | None = None

        self.update_source_var = ctk.StringVar(
            value=str((self.config_data.get("update") or {}).get("source_url") or "")
        )
        self._update_running = False
        self._step2_report_text = ""
        self._events: queue.Queue[tuple[int, str, Any]] = queue.Queue()

        # Накопленная сцена для живого CAD-предпросмотра: основной участок + соседи
        self._preview_main_plan: DrawPlan | None = None
        self._preview_neighbor_plans: list[DrawPlan] = []
        # Файлы соседей остаются разными участками даже при одинаковой точке POB.
        self._neighbor_plan_sources: dict[Path, DrawPlan] = {}
        # Подписи источников и исходы принадлежат самим планам, а не текущему полю ввода.
        self._plan_source_labels: dict[int, str] = {}
        self._plan_outcomes: dict[int, ParseOutcome] = {}
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
        # Эконом: локальный разбор; ИИ подключается, если локальный результат не прошёл проверку.
        self.economy_var = ctk.BooleanVar(value=False)

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
        self._last_consensus_label: str | None = None
        self.shortcuts_window: Any | None = None
        self.preview_canvas: Any | None = None
        self._canvas_zoom: float = 1.0
        self._canvas_pan_x: float = 0.0
        self._canvas_pan_y: float = 0.0
        self._drag_origin: tuple[int, int] | None = None
        self._hovered_seg_idx: int | None = None
        self._hover_world_pt: tuple[float, float] | None = None
        self._last_world_transform: tuple[float, float, float, int, int] | None = None

        # Общие шрифты правой панели масштабируются вместе с её шириной.
        self._result_panel_width = RESULT_PANEL_DEFAULT_WIDTH
        self._desired_result_panel_width = RESULT_PANEL_DEFAULT_WIDTH
        self._splitter_drag: tuple[int, int] | None = None
        self._result_font_base = {
            "body": 11, "body_bold": 11, "small": 10, "small_bold": 10,
            "mono": 11, "mono_small": 10, "stat": 15,
            # Журнал и отчёт Шага 2 читают подолгу: шрифт крупнее остальных.
            "log": LOG_FONT_SIZE,
        }
        self._result_fonts = {
            "body": ctk.CTkFont(size=11),
            "body_bold": ctk.CTkFont(size=11, weight="bold"),
            "small": ctk.CTkFont(size=10),
            "small_bold": ctk.CTkFont(size=10, weight="bold"),
            "mono": ctk.CTkFont(family="Consolas", size=11),
            "mono_small": ctk.CTkFont(family="Consolas", size=10),
            "stat": ctk.CTkFont(family="Consolas", size=15, weight="bold"),
            "log": ctk.CTkFont(family="Consolas", size=LOG_FONT_SIZE),
        }

        self._build_ui()
        self._bind_text_clipboard_shortcuts()
        self._bind_text_context_menu()
        self._bind_model_preference_autosave()
        self._bind_update_source_autosave()
        self._bind_studio_shortcuts()
        self._refresh_height_label()
        self._refresh_provider_highlights()
        self._refresh_keys_drawer()
        self._refresh_consensus_rows()
        self._on_source_mode_changed()
        self._refresh_result_stats(None)
        self._refresh_engine_card()
        self._refresh_stepper()
        self._render_empty_inspector()
        self.after(120, self._drain_events)

    # ------------------------------------------------------------------ UI --
    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # 0. Верхняя шапка: бренд + степпер сценария + масштаб + клавиши/настройки
        self.header = ctk.CTkFrame(
            self,
            corner_radius=0,
            fg_color=THEME["bg_header"],
            border_width=0,
        )
        self.header.grid(row=0, column=0, sticky="ew")
        self.header.grid_columnconfigure(0, weight=0)
        self.header.grid_columnconfigure(1, weight=1)
        self.header.grid_columnconfigure(2, weight=0)

        brand_box = ctk.CTkFrame(self.header, fg_color="transparent")
        brand_box.grid(row=0, column=0, sticky="w", padx=(16, 8), pady=9)
        ctk.CTkLabel(
            brand_box,
            text="📐",
            width=30,
            height=30,
            corner_radius=8,
            fg_color=THEME["btn_primary"],
            text_color="#ffffff",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 10))
        brand_titles = ctk.CTkFrame(brand_box, fg_color="transparent")
        brand_titles.grid(row=0, column=1, sticky="w")
        title_row = ctk.CTkFrame(brand_titles, fg_color="transparent")
        title_row.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            title_row,
            text="Title Survey AI",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color="#ffffff",
        ).grid(row=0, column=0, sticky="w")
        self.version_badge = ctk.CTkLabel(
            title_row,
            text=f"v{VERSION} · STUDIO",
            font=ctk.CTkFont(size=10, weight="bold"),
            fg_color=THEME["badge_v1_bg"],
            corner_radius=5,
            padx=7,
            pady=1,
            text_color=THEME["badge_v1_fg"],
        )
        self.version_badge.grid(row=0, column=1, sticky="w", padx=(8, 0))
        ctk.CTkLabel(
            brand_titles,
            text="Legal Description → AutoCAD",
            font=ctk.CTkFont(size=10),
            text_color=THEME["text_dim"],
        ).grid(row=1, column=0, sticky="w", pady=(1, 0))

        # Степпер из 4 шагов рабочего сценария геодезиста
        self.stepper_frame = ctk.CTkFrame(self.header, fg_color="transparent")
        self.stepper_frame.grid(row=0, column=1, sticky="", pady=9)
        self._step_meta: list[tuple[str, str]] = [
            ("Источник", "todo"),
            ("Разбор", "todo"),
            ("Проверка", "todo"),
            ("AutoCAD", "todo"),
        ]
        self._step_chips: list[Any] = []
        self._step_dots: list[Any] = []
        self._step_links: list[Any] = []
        for idx, (label, _state) in enumerate(self._step_meta):
            chip = ctk.CTkFrame(
                self.stepper_frame,
                fg_color="transparent",
                corner_radius=999,
            )
            chip.grid(row=0, column=idx * 2, padx=(0 if idx == 0 else 4, 0))
            dot = ctk.CTkLabel(
                chip,
                text=str(idx + 1),
                width=18,
                height=18,
                corner_radius=9,
                fg_color=THEME["border_subtle"],
                text_color=THEME["text_dim"],
                font=ctk.CTkFont(size=10, weight="bold"),
            )
            dot.grid(row=0, column=0, padx=(8, 6), pady=4)
            lbl = ctk.CTkLabel(
                chip,
                text=label,
                text_color=THEME["text_dim"],
                font=ctk.CTkFont(size=11, weight="bold"),
            )
            lbl.grid(row=0, column=1, padx=(0, 9), pady=4)
            self._step_chips.append((chip, lbl))
            self._step_dots.append(dot)
            if idx < len(self._step_meta) - 1:
                link = ctk.CTkFrame(
                    self.stepper_frame,
                    width=20,
                    height=1,
                    fg_color=THEME["border_subtle"],
                )
                link.grid(row=0, column=idx * 2 + 1, pady=0)
                self._step_links.append(link)

        header_tools = ctk.CTkFrame(self.header, fg_color="transparent")
        header_tools.grid(row=0, column=2, sticky="e", padx=(8, 16), pady=9)

        # Компактная капсула выбора масштаба чертежа
        scale_group = ctk.CTkFrame(header_tools, fg_color="transparent")
        scale_group.grid(row=0, column=0, padx=(0, 10), sticky="e")
        ctk.CTkLabel(
            scale_group,
            text="Масштаб",
            font=ctk.CTkFont(size=11),
            text_color=THEME["text_dim"],
        ).grid(row=0, column=0, padx=(0, 6), sticky="w")
        scale_pill = ctk.CTkFrame(
            scale_group,
            fg_color=THEME["bg_field"],
            corner_radius=8,
            border_width=1,
            border_color=THEME["border_subtle"],
        )
        scale_pill.grid(row=0, column=1, sticky="e")
        self.scale_menu = ctk.CTkOptionMenu(
            scale_pill,
            values=self.scale_options,
            variable=self.scale_var,
            width=98,
            height=26,
            fg_color=THEME["bg_field"],
            button_color=THEME["seg_sel"],
            button_hover_color=THEME["btn_secondary_hover"],
            font=ctk.CTkFont(size=11),
            command=lambda _value: self._refresh_height_label(),
        )
        self.scale_menu.grid(row=0, column=0, padx=(3, 3), pady=3)
        self.height_label = ctk.CTkLabel(
            scale_pill,
            text="",
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color=THEME["sky"],
        )
        self.height_label.grid(row=0, column=1, padx=(0, 9), pady=3)

        self.shortcuts_button = ctk.CTkButton(
            header_tools,
            text="⌨",
            width=32,
            height=32,
            fg_color="transparent",
            hover_color=THEME["btn_secondary"],
            font=ctk.CTkFont(size=15),
            command=self._open_shortcuts_window,
        )
        self.shortcuts_button.grid(row=0, column=1, padx=(0, 6))

        self.keys_toggle_button = ctk.CTkButton(
            header_tools,
            text="⚙ Настройки  🔑 0",
            height=32,
            width=168,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            border_width=1,
            border_color=THEME["border_card"],
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self.toggle_keys_drawer,
        )
        self.keys_toggle_button.grid(row=0, column=2, padx=(0, 8))

        self.topmost_var = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(
            header_tools,
            text="Поверх окон",
            variable=self.topmost_var,
            font=ctk.CTkFont(size=11),
            width=36,
            command=self._apply_topmost,
        ).grid(row=0, column=3, sticky="e")
        self.log_mode_var = ctk.BooleanVar(
            value=log_mode.mode_from_config(self.config_data) == log_mode.FULL,
        )
        ctk.CTkSwitch(
            header_tools,
            text="Подробный журнал",
            variable=self.log_mode_var,
            font=ctk.CTkFont(size=11),
            width=36,
            command=self._apply_log_mode,
        ).grid(row=0, column=4, sticky="e", padx=(12, 0))

        # 1. Глобальные вкладки шагов: «Шаг 1. Граница участка» | «Шаг 2. Автоподписание»
        self._build_step_tab_bar()

        # Трёхколоночная рабочая зона Spectrum: Источник | Холст+Проверки | Результат
        main_wrap = ctk.CTkFrame(self, fg_color=THEME["bg_root"])
        main_wrap.grid(row=2, column=0, sticky="nsew")
        self.main_wrap = main_wrap
        main_wrap.grid_columnconfigure(0, weight=0)
        main_wrap.grid_columnconfigure(1, weight=1)
        main_wrap.grid_columnconfigure(2, weight=0)
        main_wrap.grid_columnconfigure(3, weight=0)
        main_wrap.grid_rowconfigure(0, weight=1)

        # Левая колонка — «1 Источник» (фиксированная ширина 360, скролл-контент)
        self.left_panel = ctk.CTkFrame(
            main_wrap,
            fg_color=THEME["bg_card"],
            corner_radius=0,
            border_width=0,
            width=360,
        )
        self.left_panel.grid(row=0, column=0, sticky="nsew")
        self.left_panel.grid_propagate(False)
        self.left_panel.grid_columnconfigure(0, weight=1)
        self.left_panel.grid_rowconfigure(0, weight=1)

        # Центральная колонка — холст предпросмотра + алерты конфликтов
        self.center_col = ctk.CTkFrame(
            main_wrap,
            fg_color=THEME["bg_root"],
            corner_radius=0,
            border_width=0,
        )
        self.center_col.grid(row=0, column=1, sticky="nsew")
        self.center_col.grid_columnconfigure(0, weight=1)
        self.center_col.grid_rowconfigure(2, weight=1)

        # Разделитель перетаскивается мышью; стрелки на нём тоже меняют ширину.
        self.result_splitter = ctk.CTkFrame(
            main_wrap, width=RESULT_SPLITTER_WIDTH, corner_radius=0,
            fg_color=THEME["border_subtle"], cursor="sb_h_double_arrow",
        )
        self.result_splitter.grid(row=0, column=2, sticky="ns")
        self.result_splitter.grid_propagate(False)
        self.result_splitter.grid_rowconfigure(0, weight=1)
        self.result_splitter.grid_columnconfigure(0, weight=1)
        self.result_splitter_grip = ctk.CTkLabel(
            self.result_splitter, text="⋮", width=RESULT_SPLITTER_WIDTH,
            text_color=THEME["text_muted"], font=ctk.CTkFont(size=14, weight="bold"),
            cursor="sb_h_double_arrow", takefocus=True,
        )
        self.result_splitter_grip.grid(row=0, column=0)
        for widget in (self.result_splitter, self.result_splitter_grip):
            widget.bind("<Button-1>", self._on_result_splitter_press)
            widget.bind("<B1-Motion>", self._on_result_splitter_drag)
            widget.bind("<Double-Button-1>", self._reset_result_panel_width)
        self.result_splitter_grip.bind("<Left>", lambda _e: self._adjust_result_panel_width(30))
        self.result_splitter_grip.bind("<Right>", lambda _e: self._adjust_result_panel_width(-30))

        # Правая колонка расширяется за счёт центрального холста.
        self.right_panel = ctk.CTkFrame(
            main_wrap,
            fg_color=THEME["bg_card"],
            corner_radius=0,
            border_width=0,
            width=RESULT_PANEL_DEFAULT_WIDTH,
        )
        self.right_panel.grid(row=0, column=3, sticky="nsew")
        self.right_panel.grid_propagate(False)
        self.right_panel.grid_columnconfigure(0, weight=1)
        self.right_panel.grid_rowconfigure(2, weight=1)
        main_wrap.bind("<Configure>", self._on_workspace_resized)

        # Выезжающая шторка «Настройки»: тёмная подложка на весь экран + панель 470px справа
        self.settings_drawer_shell = ctk.CTkFrame(self, fg_color="#05080d", corner_radius=0)
        self.settings_drawer_shell.grid_columnconfigure(1, weight=0)
        self.settings_drawer_shell.grid_rowconfigure(0, weight=1)

        dimmer_cls = getattr(ctk, "CTkButton", None)
        self.settings_dimmer = dimmer_cls(
            self.settings_drawer_shell,
            text="",
            fg_color="transparent",
            hover=False,
            corner_radius=0,
            command=self.toggle_keys_drawer,
        )
        self.settings_dimmer.grid(row=0, column=0, sticky="nsew")
        self.settings_drawer_shell.grid_columnconfigure(0, weight=1)

        # 1. Ящик настроек: ИИ-движок, режим сверки, API-ключи, PIN и сохранение
        self.keys_drawer = ctk.CTkFrame(
            self.settings_drawer_shell,
            fg_color=THEME["bg_card"],
            corner_radius=0,
            border_width=1,
            border_color=THEME["border_card"],
            width=470,
        )
        self.keys_drawer.grid(row=0, column=1, sticky="nsew")
        self.keys_drawer.grid_propagate(False)
        self.keys_drawer.grid_columnconfigure(0, weight=1)
        self.keys_drawer.grid_rowconfigure(1, weight=1)

        drawer_head = ctk.CTkFrame(self.keys_drawer, fg_color="transparent")
        drawer_head.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 8))
        drawer_head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            drawer_head,
            text="Настройки",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color="#ffffff",
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            drawer_head,
            text="ИИ-движок, режим сверки и API-ключи",
            font=ctk.CTkFont(size=11),
            text_color=THEME["text_dim"],
            anchor="w",
        ).grid(row=1, column=0, sticky="w", pady=(1, 0))
        ctk.CTkButton(
            drawer_head,
            text="✕",
            width=30,
            height=30,
            fg_color="transparent",
            hover_color=THEME["btn_secondary"],
            font=ctk.CTkFont(size=13),
            text_color=THEME["text_muted"],
            command=self.toggle_keys_drawer,
        ).grid(row=0, column=1, rowspan=2, sticky="ne")

        # Прокручиваемый контейнер: без него нижние разделы (PIN-сейф,
        # «Обновить версию») были недостижимы при невысоком окне — пользователь
        # долго искал «Обновить версию» и не находил её ниже границы экрана.
        drawer_body = ctk.CTkScrollableFrame(self.keys_drawer, fg_color="transparent")
        drawer_body.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 8))
        drawer_body.grid_columnconfigure(0, weight=1)

        # Карточка обновления — самый первый пункт настроек (а не внизу под всем
        # остальным), с явной рамкой, чтобы её невозможно было не заметить/не найти.
        update_row = ctk.CTkFrame(
            drawer_body,
            fg_color=THEME["bg_field"],
            corner_radius=8,
            border_width=1,
            border_color=THEME["sky"],
        )
        update_row.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        update_row.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            update_row,
            text="🔄 ОБНОВЛЕНИЕ ПРОГРАММЫ",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=THEME["sky"],
            anchor="w",
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(10, 4))
        ctk.CTkLabel(
            update_row,
            text="Ссылка GitHub (репозиторий, ветка или .zip), куда падают актуальные пуши",
            anchor="w",
            justify="left",
            wraplength=394,
            font=ctk.CTkFont(size=11),
            text_color=THEME["text_muted"],
        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=10, pady=(0, 4))
        self.update_source_entry = ctk.CTkEntry(
            update_row,
            textvariable=self.update_source_var,
            height=30,
            fg_color=THEME["bg_editor"],
            border_color=THEME["border_card"],
            placeholder_text="https://github.com/owner/repo/tree/main",
        )
        self.update_source_entry.grid(row=2, column=0, sticky="ew", padx=(10, 4), pady=(0, 6))
        self.update_version_button = ctk.CTkButton(
            update_row,
            text="🔄 Обновить версию",
            width=150,
            height=30,
            fg_color=THEME["btn_primary"],
            hover_color=THEME["btn_primary_hover"],
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._start_update_version,
        )
        self.update_version_button.grid(row=2, column=1, padx=(0, 10), pady=(0, 6), sticky="e")
        self.update_status_label = ctk.CTkLabel(
            update_row,
            text=f"Текущая версия: {APP_VERSION} ({APP_STAMP})",
            anchor="w",
            justify="left",
            wraplength=394,
            font=ctk.CTkFont(size=11),
            text_color=THEME["text_dim"],
        )
        self.update_status_label.grid(row=3, column=0, columnspan=2, sticky="w", padx=10, pady=(0, 2))
        self.update_progress_bar = ctk.CTkProgressBar(update_row, height=8)
        self.update_progress_bar.set(0)
        self.update_progress_bar.grid(row=4, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 4))
        self.update_progress_bar.grid_remove()  # показываем только во время обновления
        ctk.CTkLabel(
            update_row,
            text=(
                "Архив скачивается и принудительно разворачивается в папку программы "
                "(совпадающие файлы заменяются); config.json, config.local.json и .env "
                "не трогаются — ваш выбор провайдера/моделей и ключи сохранятся. "
                "После обновления перезапустите программу."
            ),
            anchor="w",
            justify="left",
            wraplength=394,
            font=ctk.CTkFont(size=10),
            text_color=THEME["text_dim"],
        ).grid(row=5, column=0, columnspan=2, sticky="w", padx=10, pady=(0, 10))

        # Откат на резервную копию предыдущей версии (на случай критического бага).
        self.rollback_status_label = ctk.CTkLabel(
            update_row,
            text="",
            anchor="w",
            justify="left",
            wraplength=394,
            font=ctk.CTkFont(size=11),
            text_color=THEME["text_dim"],
        )
        self.rollback_status_label.grid(row=6, column=0, sticky="w", padx=10, pady=(0, 4))
        self.rollback_button = ctk.CTkButton(
            update_row,
            text="↩ Откатиться на старую версию",
            width=230,
            height=30,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._start_rollback,
        )
        self.rollback_button.grid(row=6, column=1, padx=(0, 10), pady=(0, 4), sticky="e")
        self._refresh_rollback_state()

        ctk.CTkLabel(
            drawer_body,
            text="API-КЛЮЧИ (НА ДИСК ПОПАДАЮТ ТОЛЬКО ШИФРОВАННЫЕ ТОКЕНЫ TSAI1:…)",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=THEME["text_dim"],
            anchor="w",
        ).grid(row=2, column=0, sticky="w", pady=(4, 4))

        keys_frame = ctk.CTkFrame(drawer_body, fg_color="transparent")
        keys_frame.grid(row=3, column=0, sticky="ew", pady=(2, 6))
        keys_frame.grid_columnconfigure(0, weight=1)
        for col, pid in enumerate(self.provider_ids):
            card = ctk.CTkFrame(
                keys_frame,
                fg_color=THEME["bg_field"],
                corner_radius=8,
                border_width=1,
                border_color=THEME["border_subtle"],
            )
            card.grid(
                row=col,
                column=0,
                sticky="ew",
                pady=(0 if col == 0 else 6, 0),
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

        vault_row = ctk.CTkFrame(drawer_body, fg_color="transparent")
        vault_row.grid(row=4, column=0, sticky="ew", pady=(10, 4))
        vault_row.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            vault_row,
            text="🔒 PIN пересчёта ключей (только в RAM)",
            font=ctk.CTkFont(size=12),
            text_color=THEME["text_muted"],
        ).grid(row=0, column=0, columnspan=2, padx=(2, 8), sticky="w")
        self.key_secret_entry = ctk.CTkEntry(
            vault_row,
            textvariable=self.key_secret_var,
            show="•",
            height=30,
            fg_color=THEME["bg_editor"],
            border_color=THEME["border_card"],
            placeholder_text="не пишется на диск",
        )
        self.key_secret_entry.grid(row=1, column=0, sticky="ew", pady=(3, 0))
        self.secret_eye_button = ctk.CTkButton(
            vault_row,
            text="👁",
            width=32,
            height=30,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            command=self._toggle_secret_visibility,
        )
        self.secret_eye_button.grid(row=1, column=1, padx=(5, 2), pady=(3, 0), sticky="w")
        ctk.CTkLabel(
            vault_row,
            text="На диске хранится только зашифрованный токен tsai1:… (MachineGuid + DPAPI + PIN)",
            anchor="w",
            justify="left",
            wraplength=410,
            font=ctk.CTkFont(size=11),
            text_color=THEME["text_dim"],
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))


        drawer_foot = ctk.CTkFrame(self.keys_drawer, fg_color=THEME["bg_header"])
        drawer_foot.grid(row=2, column=0, sticky="ew")
        drawer_foot.grid_columnconfigure(0, weight=1)
        self.save_env_button = ctk.CTkButton(
            drawer_foot,
            text="Защитить в .env",
            width=132,
            height=32,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            border_width=1,
            border_color=THEME["border_card"],
            font=ctk.CTkFont(size=12),
            command=self._save_protected_env_clicked,
        )
        self.save_env_button.grid(row=0, column=0, padx=(12, 6), pady=10, sticky="w")
        self.save_button = ctk.CTkButton(
            drawer_foot,
            text="💾 Сохранить  (Ctrl+S)",
            width=190,
            height=32,
            fg_color=THEME["btn_primary"],
            hover_color=THEME["btn_primary_hover"],
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._save_settings,
        )
        self.save_button.grid(row=0, column=1, padx=(0, 12), pady=10, sticky="e")

        # 1b. Стартовая вкладка Шага 1: текст или PDF (сохраняется в config.json, ui.start_source).
        start_card = ctk.CTkFrame(
            drawer_body,
            fg_color=THEME["bg_field"],
            corner_radius=8,
            border_width=1,
            border_color=THEME["border_subtle"],
        )
        start_card.grid(row=5, column=0, sticky="ew", pady=(10, 4))
        start_card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            start_card,
            text="СТАРТОВАЯ ВКЛАДКА ШАГА 1",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=THEME["text_dim"],
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=10, pady=(8, 2))
        self.start_source_var = ctk.StringVar(
            value=start_source.label_for(start_source.start_source_from_config(self.config_data))
        )
        self.start_source_switch = ctk.CTkSegmentedButton(
            start_card,
            values=[start_source.LABELS[start_source.TEXT], start_source.LABELS[start_source.PDF]],
            variable=self.start_source_var,
            height=28,
            fg_color=THEME["bg_editor"],
            unselected_color=THEME["bg_editor"],
            selected_color=THEME["btn_primary"],
            selected_hover_color=THEME["btn_primary_hover"],
            command=self._on_start_source_changed,
        )
        self.start_source_switch.grid(row=1, column=0, sticky="ew", padx=10, pady=(2, 4))
        ctk.CTkLabel(
            start_card,
            text="Что открывать при запуске программы. Действует со следующего запуска.",
            font=ctk.CTkFont(size=10),
            text_color=THEME["text_dim"],
            anchor="w",
            justify="left",
            wraplength=400,
        ).grid(row=2, column=0, sticky="w", padx=10, pady=(0, 8))

        # 2. Блок ИИ-движка в шторке настроек: режим 1–3 проходов + слоты моделей
        ai_card = ctk.CTkFrame(drawer_body, fg_color="transparent")
        ai_card.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        ai_card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            ai_card,
            text="РЕЖИМ СВЕРКИ LLM",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=THEME["text_dim"],
            anchor="w",
        ).grid(row=0, column=0, sticky="w", pady=(0, 4))

        self.llm_policy_row = ctk.CTkFrame(ai_card, fg_color="transparent")
        self.llm_policy_row.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        self.llm_policy_row.grid_columnconfigure(0, weight=1)

        self.use_llm_switch = ctk.CTkSwitch(
            self.llm_policy_row,
            text="ИИ-разбор (LLM)",
            variable=self.use_llm_var,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._on_use_llm_toggled,
        )
        self.use_llm_switch.grid(row=0, column=0, sticky="w", pady=(0, 6))

        self.passes_switch = ctk.CTkSegmentedButton(
            self.llm_policy_row,
            values=self.passes_options,
            variable=self.passes_label_var,
            height=30,
            fg_color=THEME["bg_field"],
            unselected_color=THEME["bg_field"],
            selected_color=THEME["seg_sel"],
            selected_hover_color=THEME["btn_secondary_hover"],
            command=self._on_passes_changed,
        )
        self.passes_switch.grid(row=1, column=0, sticky="ew", pady=(0, 4))

        ctk.CTkLabel(
            self.llm_policy_row,
            text=(
                "Независимые запросы идут параллельно. Расхождение откроет панель выбора, "
                "совпадение — построит участок сразу. Консенсус 2+ проходов защищает цифры "
                "документа от тихих автоподмен."
            ),
            font=ctk.CTkFont(size=11),
            text_color=THEME["text_dim"],
            anchor="w",
            justify="left",
            wraplength=430,
        ).grid(row=2, column=0, sticky="w", pady=(0, 6))

        toggles_card = ctk.CTkFrame(
            self.llm_policy_row,
            fg_color=THEME["bg_field"],
            corner_radius=8,
            border_width=1,
            border_color=THEME["border_subtle"],
        )
        toggles_card.grid(row=3, column=0, sticky="ew", pady=(0, 2))
        toggles_card.grid_columnconfigure(0, weight=1)
        self.ask_on_doubt_check = ctk.CTkCheckBox(
            toggles_card,
            text="Спрашивать при сомнении — стоп и выбор человека при подозрении на опечатку",
            variable=self.ask_on_doubt_var,
            checkbox_width=18,
            checkbox_height=18,
            fg_color=THEME["btn_primary"],
            hover_color=THEME["btn_primary_hover"],
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=THEME["text_main"],
        )
        self.ask_on_doubt_check.grid(row=0, column=0, sticky="w", padx=10, pady=(8, 8))

        def _slot_card(row_i: int, title: str, hint: str, hint_color: str) -> Any:
            card = ctk.CTkFrame(
                ai_card,
                fg_color=THEME["bg_field"],
                corner_radius=8,
                border_width=1,
                border_color=THEME["border_subtle"],
            )
            card.grid(row=row_i, column=0, sticky="ew", pady=(0, 6))
            card.grid_columnconfigure(0, weight=1)
            head = ctk.CTkFrame(card, fg_color="transparent")
            head.grid(row=0, column=0, sticky="ew", padx=10, pady=(7, 5))
            head.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(
                head,
                text=title,
                font=ctk.CTkFont(size=12, weight="bold"),
                text_color=THEME["text_main"],
                anchor="w",
            ).grid(row=0, column=0, sticky="w")
            ctk.CTkLabel(
                head,
                text=hint,
                font=ctk.CTkFont(size=10),
                text_color=hint_color,
                anchor="e",
            ).grid(row=0, column=1, sticky="e")
            return card

        # Слот Прохода #1 (основной)
        pass1_card = _slot_card(2, "Проход #1", "основной", THEME["sky"])
        self.pass1_title_label = ctk.CTkLabel(
            pass1_card,
            text="Провайдер:",
            font=ctk.CTkFont(size=10),
            text_color=THEME["text_dim"],
            anchor="w",
        )
        self.pass1_title_label.grid(row=1, column=0, sticky="w", padx=10)
        self.provider_switch = ctk.CTkSegmentedButton(
            pass1_card,
            values=self.provider_labels,
            variable=self.provider_label_var,
            height=28,
            fg_color=THEME["bg_editor"],
            unselected_color=THEME["bg_editor"],
            selected_color=THEME["btn_primary"],
            selected_hover_color=THEME["btn_primary_hover"],
            command=self._on_provider_changed,
        )
        self.provider_switch.grid(row=2, column=0, sticky="ew", padx=10, pady=(2, 6))
        ctk.CTkLabel(
            pass1_card,
            text="Модель:",
            font=ctk.CTkFont(size=10),
            text_color=THEME["text_dim"],
            anchor="w",
        ).grid(row=3, column=0, sticky="w", padx=10)
        self.model_combo = ctk.CTkComboBox(
            pass1_card,
            values=self._provider_models[self._active_provider_id],
            variable=self.model_var,
            height=30,
            fg_color=THEME["bg_editor"],
            border_color=THEME["border_card"],
            button_color=THEME["btn_secondary"],
            button_hover_color=THEME["btn_secondary_hover"],
            font=ctk.CTkFont(family="Consolas", size=11),
            command=self._on_model_selected,
        )
        self.model_combo.grid(row=4, column=0, sticky="ew", padx=10, pady=(2, 9))

        # Слот Прохода #2 (независимая сверка)
        self.pass2_row = _slot_card(3, "Проход #2", "независимая сверка", THEME["sky"])
        self.pass2_provider_switch = ctk.CTkSegmentedButton(
            self.pass2_row,
            values=self.provider_labels,
            variable=self.pass2_provider_label_var,
            height=26,
            fg_color=THEME["bg_editor"],
            unselected_color=THEME["bg_editor"],
            selected_color=THEME["btn_primary"],
            selected_hover_color=THEME["btn_primary_hover"],
            command=self._on_pass2_provider_changed,
        )
        self.pass2_provider_switch.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 6))
        self.pass2_model_combo = ctk.CTkComboBox(
            self.pass2_row,
            values=self._provider_models[self._pass2_pid],
            variable=self.pass2_model_var,
            height=28,
            fg_color=THEME["bg_editor"],
            border_color=THEME["border_card"],
            button_color=THEME["btn_secondary"],
            button_hover_color=THEME["btn_secondary_hover"],
            font=ctk.CTkFont(family="Consolas", size=11),
            command=lambda val: self._on_pass_model_selected(2, val),
        )
        self.pass2_model_combo.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 9))

        # Слот Прохода #3 (арбитражный)
        self.pass3_row = _slot_card(4, "Проход #3", "арбитражный слот", THEME["ok"])
        self.pass3_provider_switch = ctk.CTkSegmentedButton(
            self.pass3_row,
            values=self.provider_labels,
            variable=self.pass3_provider_label_var,
            height=26,
            fg_color=THEME["bg_editor"],
            unselected_color=THEME["bg_editor"],
            selected_color=THEME["btn_primary"],
            selected_hover_color=THEME["btn_primary_hover"],
            command=self._on_pass3_provider_changed,
        )
        self.pass3_provider_switch.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 6))
        self.pass3_model_combo = ctk.CTkComboBox(
            self.pass3_row,
            values=self._provider_models[self._pass3_pid],
            variable=self.pass3_model_var,
            height=28,
            fg_color=THEME["bg_editor"],
            border_color=THEME["border_card"],
            button_color=THEME["btn_secondary"],
            button_hover_color=THEME["btn_secondary_hover"],
            font=ctk.CTkFont(family="Consolas", size=11),
            command=lambda val: self._on_pass_model_selected(3, val),
        )
        self.pass3_model_combo.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 9))

        # 3. Левая колонка «1 Источник»: шапка панели + вкладки Текст / PDF / Фото
        workspace = self.left_panel
        workspace.grid_rowconfigure(4, weight=1)

        # --- Левая колонка «1 Источник»: шапка, вкладки Текст/PDF/Фото -------
        input_frame = ctk.CTkFrame(
            workspace,
            fg_color=THEME["bg_card"],
            corner_radius=0,
            border_width=0,
        )
        input_frame.grid(row=0, column=0, rowspan=9, sticky="nsew")
        input_frame.grid_columnconfigure(0, weight=1)
        input_frame.grid_rowconfigure(3, weight=1)
        self.panel_input_frame = input_frame

        doc_header = ctk.CTkFrame(input_frame, fg_color="transparent")
        doc_header.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 8))
        doc_header.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            doc_header,
            text="1",
            width=24,
            height=24,
            corner_radius=6,
            fg_color=THEME["blue_soft_bg"],
            text_color=THEME["blue_soft_fg"],
            font=ctk.CTkFont(size=12, weight="bold"),
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            doc_header,
            text="Исходные данные",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color="#ffffff",
        ).grid(row=0, column=1, sticky="w", padx=(10, 0))
        self.pdf_label = ctk.CTkLabel(
            doc_header,
            text="PDF не загружен",
            font=ctk.CTkFont(size=11),
            text_color=THEME["text_dim"],
        )
        self.pdf_label.grid(row=0, column=2, sticky="e")

        # Сегментированный переключатель источника данных
        # Стартовая вкладка источника: настройка ui.start_source (по умолчанию текст).
        self.source_mode_var = ctk.StringVar(
            value=start_source.label_for(start_source.start_source_from_config(self.config_data))
        )
        self.source_mode_switch = ctk.CTkSegmentedButton(
            input_frame,
            values=["📄 PDF", "📷 Фото", "📝 Текст"],
            variable=self.source_mode_var,
            height=32,
            fg_color=THEME["bg_field"],
            unselected_color=THEME["bg_field"],
            selected_color=THEME["seg_sel"],
            selected_hover_color=THEME["btn_secondary_hover"],
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._on_source_mode_changed,
        )
        self.source_mode_switch.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 10))

        # Карточка текстового редактора (режим «📝 Текст»)
        self.text_editor_card = ctk.CTkFrame(input_frame, fg_color="transparent")
        self.text_editor_card.grid(row=2, column=0, rowspan=2, sticky="nsew", padx=16)
        self.text_editor_card.grid_columnconfigure(0, weight=1)
        self.text_editor_card.grid_rowconfigure(1, weight=1)

        toolbar = ctk.CTkFrame(self.text_editor_card, fg_color="transparent")
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        toolbar.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            toolbar,
            text="LEGAL DESCRIPTION",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=THEME["text_dim"],
        ).grid(row=0, column=0, sticky="w")

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
            width=170,
            height=24,
            fg_color=THEME["bg_card"],
            button_color=THEME["bg_card"],
            button_hover_color=THEME["btn_secondary"],
            text_color=THEME["text_muted"],
            font=ctk.CTkFont(size=11),
            command=self._on_sample_selected,
        )
        self.sample_menu.grid(row=0, column=1, padx=(4, 0), sticky="e")

        ctk.CTkButton(
            toolbar,
            text="🧹 Очистить",
            width=84,
            height=24,
            fg_color="transparent",
            hover_color=THEME["btn_secondary"],
            text_color=THEME["text_muted"],
            font=ctk.CTkFont(size=11),
            command=self._clear_text,
        ).grid(row=0, column=2, padx=(2, 0), sticky="e")

        self.text_box = ctk.CTkTextbox(
            self.text_editor_card,
            wrap="word",
            fg_color=THEME["bg_editor"],
            border_width=1,
            border_color=THEME["border_subtle"],
            text_color=THEME["text_main"],
            corner_radius=8,
            font=ctk.CTkFont(family="Consolas", size=12),
        )
        self.text_box.grid(row=1, column=0, sticky="nsew")
        self.text_box.insert("1.0", "")
        inner_editor = getattr(self.text_box, "_textbox", None)
        bind_editor = getattr(inner_editor, "bind", None) if inner_editor is not None else None
        if callable(bind_editor):
            bind_editor("<KeyRelease>", lambda _ev: self._refresh_text_counter())

        # Карточка PDF (режим «📄 PDF»): зона загрузки + выбор страниц
        self.pdf_mode_card = ctk.CTkFrame(input_frame, fg_color="transparent")
        self.pdf_mode_card.grid(row=2, column=0, rowspan=2, sticky="new", padx=16)
        self.pdf_mode_card.grid_columnconfigure(0, weight=1)

        self.load_pdf_button = ctk.CTkButton(
            self.pdf_mode_card,
            text="⭱ Выберите PDF: наш участок и соседи сразу\nТекст, чертёж или скан · Ctrl+O",
            height=64,
            fg_color=THEME["bg_field"],
            hover_color=THEME["blue_soft_bg"],
            border_width=1,
            border_color=THEME["border_subtle"],
            corner_radius=10,
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=THEME["text_muted"],
            command=self._load_pdf,
        )
        self.load_pdf_button.grid(row=0, column=0, sticky="ew")

        pdfpages_row = ctk.CTkFrame(
            self.pdf_mode_card,
            fg_color=THEME["bg_field"],
            corner_radius=8,
            border_width=1,
            border_color=THEME["border_subtle"],
        )
        pdfpages_row.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        pdfpages_row.grid_columnconfigure(0, weight=1)
        self.pdf_pages_row = pdfpages_row
        ctk.CTkLabel(
            pdfpages_row,
            text="Страницы",
            font=ctk.CTkFont(size=12),
            text_color=THEME["text_muted"],
        ).grid(row=0, column=0, sticky="w", padx=10, pady=7)
        self.pdf_pages_entry = ctk.CTkEntry(
            pdfpages_row,
            textvariable=self.pdf_pages_var,
            width=96,
            height=26,
            fg_color=THEME["bg_editor"],
            border_color=THEME["border_card"],
            justify="right",
            placeholder_text="1-8 / все",
        )
        self.pdf_pages_entry.grid(row=0, column=1, sticky="e", padx=10, pady=7)
        self.pdf_info_label = ctk.CTkLabel(
            self.pdf_mode_card,
            text=(
                "После выбора откроется список: отметьте наш участок и страницы с границами. "
                "Текст обхода — бесплатно и локально, скан — через ИИ/Vision."
            ),
            font=ctk.CTkFont(size=11),
            text_color=THEME["text_dim"],
            anchor="w",
            justify="left",
            wraplength=320,
        )
        self.pdf_info_label.grid(row=2, column=0, sticky="ew", pady=(4, 0))

        self.load_images_button = ctk.CTkButton(
            self.pdf_mode_card,
            text="📷 Загрузить фото чертежа · Ctrl+I",
            height=30,
            fg_color=THEME["bg_field"],
            hover_color=THEME["blue_soft_bg"],
            border_width=1,
            border_color=THEME["border_subtle"],
            corner_radius=10,
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=THEME["text_muted"],
            command=self._load_images,
        )
        self.load_images_button.grid(row=3, column=0, sticky="ew", pady=(10, 0))

        self.neighbor_pdf_summary = ctk.CTkLabel(
            self.pdf_mode_card,
            text="",
            font=ctk.CTkFont(size=11),
            text_color=THEME["text_muted"],
            anchor="w",
            justify="left",
            wraplength=320,
        )
        self.neighbor_pdf_summary.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        self.neighbor_pdf_summary.grid_remove()

        # Набор PDF: правка ролей и страниц, итог разбора по каждому участку.
        self.pdf_set_buttons = ctk.CTkFrame(self.pdf_mode_card, fg_color="transparent")
        self.pdf_set_buttons.grid(row=5, column=0, sticky="ew", pady=(8, 0))
        self.pdf_set_buttons.grid_columnconfigure((0, 1), weight=1)
        self.pdf_set_edit_button = ctk.CTkButton(
            self.pdf_set_buttons,
            text="☰ Роли и страницы",
            height=30,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._edit_pdf_set,
        )
        self.pdf_set_edit_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.pdf_set_report_button = ctk.CTkButton(
            self.pdf_set_buttons,
            text="📋 Итог разбора",
            height=30,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._open_pdf_set_report_window,
        )
        self.pdf_set_report_button.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        self.pdf_set_buttons.grid_remove()

        # Пустой режим «📷 Фото» тоже должен позволять выбрать первый файл.
        self.photo_empty_card = ctk.CTkFrame(input_frame, fg_color="transparent")
        self.photo_empty_card.grid(row=2, column=0, rowspan=2, sticky="new", padx=16)
        self.photo_empty_card.grid_columnconfigure(0, weight=1)
        self.photo_load_button = ctk.CTkButton(
            self.photo_empty_card,
            text="📷  Выберите фото чертежа\nJPG · PNG · TIFF · несколько кадров · Ctrl+I",
            height=132,
            fg_color=THEME["bg_field"],
            hover_color=THEME["blue_soft_bg"],
            border_width=1,
            border_color=THEME["border_subtle"],
            corner_radius=10,
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=THEME["text_muted"],
            command=self._load_images,
        )
        self.photo_load_button.grid(row=0, column=0, sticky="ew")
        self.photo_empty_card.grid_remove()

        # Панель режима «📷 Фото»: кадр, навигация/поворот и фильтры сканов 80-х
        self.visual_bar = ctk.CTkFrame(
            input_frame,
            fg_color="transparent",
            corner_radius=0,
            border_width=0,
        )
        self.visual_bar.grid(row=2, column=0, rowspan=2, sticky="new", padx=16)
        self.visual_bar.grid_columnconfigure(0, weight=1)

        # Миниатюра текущего кадра (превью из центрального холста дублируется здесь ярлыком)
        self.visual_thumb_card = ctk.CTkFrame(
            self.visual_bar,
            fg_color=THEME["bg_editor"],
            corner_radius=8,
            border_width=1,
            border_color=THEME["border_subtle"],
            height=150,
        )
        self.visual_thumb_card.grid(row=0, column=0, sticky="ew")
        self.visual_thumb_card.grid_columnconfigure(0, weight=1)
        self.visual_thumb_card.grid_rowconfigure(0, weight=1)
        self.visual_thumb_label = ctk.CTkLabel(
            self.visual_thumb_card,
            text="Кадр 1/1 · 0°",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=THEME["warn"],
        )
        self.visual_thumb_label.grid(row=0, column=0, padx=10, pady=10)
        self.visual_pos_label = self.visual_thumb_label

        visual_top_row = ctk.CTkFrame(self.visual_bar, fg_color="transparent")
        visual_top_row.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        visual_top_row.grid_columnconfigure(3, weight=1)

        self.prev_visual_button = ctk.CTkButton(
            visual_top_row,
            text="◀",
            width=30,
            height=28,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            border_width=1,
            border_color=THEME["border_card"],
            command=lambda: self.step_visual_frame(-1),
        )
        self.prev_visual_button.grid(row=0, column=0, padx=(0, 4))

        self.next_visual_button = ctk.CTkButton(
            visual_top_row,
            text="▶",
            width=30,
            height=28,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            border_width=1,
            border_color=THEME["border_card"],
            command=lambda: self.step_visual_frame(1),
        )
        self.next_visual_button.grid(row=0, column=1, padx=(0, 8))

        self.rot_ccw_button = ctk.CTkButton(
            visual_top_row,
            text="⟲ -90°",
            width=58,
            height=28,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            border_width=1,
            border_color=THEME["border_card"],
            font=ctk.CTkFont(size=11, weight="bold"),
            command=lambda: self.rotate_visual(-90),
        )
        self.rot_ccw_button.grid(row=0, column=2, padx=(0, 4))

        self.rot_cw_button = ctk.CTkButton(
            visual_top_row,
            text="⟳ +90°",
            width=58,
            height=28,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            border_width=1,
            border_color=THEME["border_card"],
            font=ctk.CTkFont(size=11, weight="bold"),
            command=lambda: self.rotate_visual(90),
        )
        self.rot_cw_button.grid(row=0, column=3, padx=(0, 4))

        self.rot_180_button = ctk.CTkButton(
            visual_top_row,
            text="⇅ 180°",
            width=56,
            height=28,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            border_width=1,
            border_color=THEME["border_card"],
            font=ctk.CTkFont(size=11, weight="bold"),
            command=lambda: self.rotate_visual(180),
        )
        self.rot_180_button.grid(row=0, column=4, padx=(0, 4))

        self.add_more_photos_button = ctk.CTkButton(
            visual_top_row,
            text="Ещё…",
            width=60,
            height=28,
            fg_color="transparent",
            hover_color=THEME["btn_secondary"],
            text_color=THEME["text_muted"],
            font=ctk.CTkFont(size=11),
            command=self._load_images,
        )
        self.add_more_photos_button.grid(row=0, column=5, sticky="e")

        visual_opts_card = ctk.CTkFrame(
            self.visual_bar,
            fg_color=THEME["bg_field"],
            corner_radius=8,
            border_width=1,
            border_color=THEME["border_subtle"],
        )
        visual_opts_card.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        visual_opts_card.grid_columnconfigure(0, weight=1)

        self.enhance_scan_switch = ctk.CTkSwitch(
            visual_opts_card,
            text="Чёткость фото 80-х  (контраст и резкость без искажения цветов)",
            variable=self.enhance_scan_var,
            font=ctk.CTkFont(size=11),
            command=self._redraw_preview_canvas,
        )
        self.enhance_scan_switch.grid(row=0, column=0, padx=10, pady=(9, 5), sticky="w")

        self.auto_orient_switch = ctk.CTkCheckBox(
            visual_opts_card,
            text="🧭 ИИ-поворот  (платно · 1 раз; ручной поворот бесплатен)",
            variable=self.auto_orient_var,
            checkbox_width=18,
            checkbox_height=18,
            fg_color=THEME["warn"],
            hover_color="#d97706",
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self._on_auto_orient_toggled,
        )
        self.auto_orient_switch.grid(row=1, column=0, padx=10, pady=(4, 9), sticky="w")

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
            wraplength=320,
        )
        self.auto_orient_warn_label.grid(
            row=3, column=0, sticky="ew", pady=(6, 0)
        )
        self.auto_orient_warn_label.grid_remove()
        self.visual_bar.grid_remove()

        # Плашка выбора роли участка на чертеже + сверка и пакет соседей
        self.role_bar = ctk.CTkFrame(
            input_frame,
            fg_color=THEME["bg_inset"],
            corner_radius=8,
            border_width=1,
            border_color=THEME["border_subtle"],
        )
        self.role_bar.grid(row=4, column=0, sticky="ew", padx=16, pady=(10, 0))
        self.role_bar.grid_columnconfigure(2, weight=1)

        self.lot_role_var = ctk.StringVar(value="main")
        self.main_lot_radio = ctk.CTkRadioButton(
            self.role_bar,
            text="Основной участок",
            variable=self.lot_role_var,
            value="main",
            font=ctk.CTkFont(size=12),
            command=self._on_lot_role_changed,
        )
        self.main_lot_radio.grid(row=0, column=0, sticky="w", padx=(10, 10), pady=7)

        self.neighbor_lot_radio = ctk.CTkRadioButton(
            self.role_bar,
            text="Соседний участок",
            variable=self.lot_role_var,
            value="neighbor",
            font=ctk.CTkFont(size=12),
            command=self._on_lot_role_changed,
        )
        self.neighbor_lot_radio.grid(row=0, column=1, sticky="w", padx=(0, 8), pady=7)

        # Счётчик символов описания (внизу карточки редактора)
        self.text_counter_label = ctk.CTkLabel(
            self.text_editor_card,
            text="0 симв.",
            font=ctk.CTkFont(size=10),
            text_color=THEME["text_dim"],
            anchor="e",
        )
        self.text_counter_label.grid(row=2, column=0, sticky="e", pady=(3, 0))

        # Тонкий индикатор прогресса параллельных проходов ИИ (внизу источника)
        self.progress = ctk.CTkProgressBar(
            input_frame,
            mode="indeterminate",
            height=5,
            corner_radius=3,
            fg_color=THEME["bg_field"],
            progress_color=THEME["btn_primary"],
        )
        self.progress.grid(row=5, column=0, sticky="ew", padx=16, pady=(8, 0))
        self.progress.set(0)

        # Мини-карточка ИИ-движка (Spectrum SourcePanel): активная модель + режим сверки
        engine_card = ctk.CTkFrame(
            input_frame,
            fg_color=THEME["bg_field"],
            corner_radius=8,
            border_width=1,
            border_color=THEME["border_subtle"],
        )
        engine_card.grid(row=6, column=0, sticky="ew", padx=16, pady=(10, 0))
        engine_card.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            engine_card,
            text="РЕЖИМ РАЗБОРА",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=THEME["text_dim"],
            anchor="w",
        ).grid(row=0, column=0, columnspan=4, sticky="w", padx=10, pady=(8, 5))

        ctk.CTkLabel(
            engine_card,
            text="✨",
            width=24,
            height=24,
            corner_radius=6,
            fg_color="#1e1b3a",
            text_color=THEME["icon_purple"],
            font=ctk.CTkFont(size=12),
        ).grid(row=1, column=0, padx=(10, 8), pady=(0, 6))

        self.engine_model_var = ctk.StringVar(value="")
        self.engine_model_label = ctk.CTkLabel(
            engine_card,
            textvariable=self.engine_model_var,
            anchor="w",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=THEME["text_main"],
        )
        self.engine_model_label.grid(row=1, column=1, sticky="w")
        self.regex_mode_badge = ctk.CTkLabel(
            engine_card,
            text="⚡ Локально · бесплатно",
            anchor="w",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=THEME["warn"],
        )
        self.regex_mode_badge.grid_remove()

        self.ask_on_doubt_badge = ctk.CTkLabel(
            engine_card,
            text="спрашивать",
            font=ctk.CTkFont(size=9, weight="bold"),
            fg_color=THEME["blue_soft_bg"],
            text_color=THEME["blue_soft_fg"],
            corner_radius=4,
            padx=5,
            pady=1,
        )
        self.ask_on_doubt_badge.grid(row=1, column=2, padx=(6, 0), pady=(0, 6))

        ctk.CTkButton(
            engine_card,
            text="⚙ Настроить",
            height=24,
            fg_color="transparent",
            hover_color=THEME["blue_soft_bg"],
            text_color=THEME["sky"],
            font=ctk.CTkFont(size=11),
            command=self.toggle_keys_drawer,
        ).grid(row=1, column=3, sticky="e", padx=(4, 8), pady=(0, 6))

        ctk.CTkLabel(
            engine_card,
            text="Сверка",
            font=ctk.CTkFont(size=10),
            text_color=THEME["text_dim"],
        ).grid(row=2, column=0, sticky="w", padx=10, pady=(0, 8))
        self.passes_switch_mini = ctk.CTkSegmentedButton(
            engine_card,
            values=self.passes_options,
            variable=self.passes_label_var,
            height=24,
            fg_color=THEME["bg_editor"],
            unselected_color=THEME["bg_editor"],
            selected_color=THEME["seg_sel"],
            selected_hover_color=THEME["btn_secondary_hover"],
            font=ctk.CTkFont(size=11),
            command=self._on_passes_changed,
        )
        self.passes_switch_mini.grid(row=2, column=1, columnspan=3, sticky="ew", padx=(0, 10), pady=(0, 8))

        # Блок главного действия в источнике (Spectrum: «Разобрать и проверить» + regex)
        run_box = ctk.CTkFrame(input_frame, fg_color=THEME["bg_header"], corner_radius=0)
        run_box.grid(row=7, column=0, sticky="ew", pady=(10, 0))
        run_box.grid_columnconfigure(0, weight=1)

        self.parse_button = ctk.CTkButton(
            run_box,
            text="✨ Разобрать с ИИ · Ctrl+Enter",
            height=44,
            fg_color=THEME["btn_primary"],
            hover_color=THEME["btn_primary_hover"],
            font=ctk.CTkFont(size=13, weight="bold"),
            command=lambda: self._start(parse_only=True),
        )
        self.parse_button.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 2))

        self.run_mode_seg = ctk.CTkSegmentedButton(
            run_box,
            values=self.RUN_MODE_LABELS,
            height=28,
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self._on_run_mode_clicked,
        )
        self.run_mode_seg.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 4))

        self.regex_link_button = ctk.CTkButton(
            run_box,
            text="⚡ Быстрый разбор без ИИ (regex, бесплатно)",
            height=26,
            fg_color="transparent",
            hover_color=THEME["btn_secondary"],
            text_color=THEME["text_dim"],
            font=ctk.CTkFont(size=11),
            command=self._run_regex_only,
        )
        self.regex_link_button.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 4))

        self.reset_session_button = ctk.CTkButton(
            run_box,
            text="↺ Новый расчёт · полный сброс",
            height=30,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            text_color=THEME["text_muted"],
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self._reset_session_clicked,
        )
        self.reset_session_button.grid(row=3, column=0, sticky="ew", padx=14, pady=(0, 10))

        # --- Центральная колонка: шапка холста + живой CAD-предпросмотр ------
        center_head = ctk.CTkFrame(
            self.center_col,
            fg_color=THEME["bg_card"],
            corner_radius=0,
            border_width=0,
            height=48,
        )
        center_head.grid(row=0, column=0, sticky="ew")
        center_head.grid_propagate(False)
        center_head.grid_columnconfigure(1, weight=1)

        self.preview_mode_switch = ctk.CTkSegmentedButton(
            center_head,
            values=self.preview_modes,
            variable=self.preview_mode_var,
            height=30,
            fg_color=THEME["bg_field"],
            unselected_color=THEME["bg_field"],
            selected_color=THEME["seg_sel"],
            selected_hover_color=THEME["btn_secondary_hover"],
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._on_preview_mode_changed,
        )
        self.preview_mode_switch.grid(row=0, column=0, sticky="w", padx=(12, 8))

        center_badges = ctk.CTkFrame(center_head, fg_color="transparent")
        center_badges.grid(row=0, column=1, sticky="e", padx=(0, 12))
        self.consensus_badge = ctk.CTkLabel(
            center_badges,
            text="",
            font=ctk.CTkFont(size=10, weight="bold"),
            fg_color=THEME["badge_v1_bg"],
            text_color=THEME["badge_v1_fg"],
            corner_radius=5,
            padx=7,
            pady=1,
        )
        self.center_closure_badge = ctk.CTkLabel(
            center_badges,
            text="",
            font=ctk.CTkFont(size=10, weight="bold"),
            fg_color=THEME["badge_v1_bg"],
            text_color=THEME["badge_v1_fg"],
            corner_radius=5,
            padx=7,
            pady=1,
        )

        self.copy_report_button = ctk.CTkButton(
            center_badges,
            text="📋 Копировать",
            width=104,
            height=26,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            border_width=1,
            border_color=THEME["border_card"],
            font=ctk.CTkFont(size=11),
            command=self.copy_inspector_report,
        )
        self.copy_report_button.grid(row=0, column=0, padx=(0, 6), sticky="e")

        self.reset_scene_button = ctk.CTkButton(
            center_badges,
            text="Сбросить",
            width=76,
            height=26,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            border_width=1,
            border_color=THEME["border_card"],
            font=ctk.CTkFont(size=11),
            command=self._clear_preview_scene,
        )
        self.reset_scene_button.grid(row=0, column=1, padx=(0, 8), sticky="e")

        slide_tools = ctk.CTkFrame(center_head, fg_color="transparent")
        slide_tools.grid(row=0, column=2, sticky="e", padx=(0, 12))
        self.slide_overlaps_button = ctk.CTkButton(
            slide_tools,
            text="⇲ Развести наложения",
            width=168,
            height=26,
            fg_color=THEME["btn_amber"],
            hover_color=THEME["btn_amber_hover"],
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self._slide_overlapping_neighbours,
        )
        self.slide_overlaps_button.grid(row=0, column=0, padx=(0, 6), sticky="e")
        self.slide_undo_button = ctk.CTkButton(
            slide_tools,
            text="↶ Вернуть",
            width=84,
            height=26,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            border_width=1,
            border_color=THEME["border_card"],
            font=ctk.CTkFont(size=11),
            command=self._undo_slide_neighbours,
        )
        self.slide_undo_button.grid(row=0, column=1, sticky="e")

        # Панели конфликта проходов и сомнительных мест (между шапкой и холстом)
        self.alerts_wrap = ctk.CTkFrame(self.center_col, fg_color="transparent")
        self.alerts_wrap.grid(row=1, column=0, sticky="ew")
        self.alerts_wrap.grid_columnconfigure(0, weight=1)

        # Холст предпросмотра с оверлеями (зум-контролы, HUD)
        canvas_wrap = ctk.CTkFrame(
            self.center_col,
            fg_color=THEME["bg_editor"],
            corner_radius=0,
            border_width=0,
        )
        canvas_wrap.grid(row=2, column=0, sticky="nsew")
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
            self.preview_canvas.grid(row=0, column=0, sticky="nsew")
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

        # Оверлеи холста: зум-контролы справа + чип масштаба/подсказка снизу
        zoom_box = ctk.CTkFrame(
            canvas_wrap,
            fg_color=THEME["bg_header"],
            corner_radius=8,
            border_width=1,
            border_color=THEME["border_card"],
        )
        zoom_box.grid(row=0, column=0, sticky="ne", padx=12, pady=84)
        self.zoom_in_button = ctk.CTkButton(
            zoom_box,
            text="+",
            width=150,
            height=30,
            fg_color="transparent",
            hover_color=THEME["btn_secondary"],
            font=ctk.CTkFont(size=15, weight="bold"),
            command=lambda: self.zoom_viewport(1.3),
        )
        self.zoom_in_button.grid(row=0, column=0)
        ctk.CTkFrame(zoom_box, height=1, fg_color=THEME["border_subtle"]).grid(
            row=1, column=0, sticky="ew"
        )
        self.zoom_reset_button = ctk.CTkButton(
            zoom_box,
            text="1:1",
            width=150,
            height=34,
            fg_color="transparent",
            hover_color=THEME["btn_secondary"],
            text_color=THEME["text_muted"],
            font=ctk.CTkFont(family="Consolas", size=10, weight="bold"),
            command=self.reset_viewport_zoom,
        )
        self.zoom_reset_button.grid(row=2, column=0)
        ctk.CTkFrame(zoom_box, height=1, fg_color=THEME["border_subtle"]).grid(
            row=3, column=0, sticky="ew"
        )
        self.zoom_out_button = ctk.CTkButton(
            zoom_box,
            text="−",
            width=150,
            height=30,
            fg_color="transparent",
            hover_color=THEME["btn_secondary"],
            font=ctk.CTkFont(size=15, weight="bold"),
            command=lambda: self.zoom_viewport(1.0 / 1.3),
        )
        self.zoom_out_button.grid(row=4, column=0)

        self.canvas_hint_label = ctk.CTkLabel(
            canvas_wrap,
            text="Колесо — зум · ЛКМ — панорама · 2× клик — вписать",
            font=ctk.CTkFont(size=10),
            fg_color=THEME["bg_header"],
            text_color=THEME["text_dim"],
            corner_radius=999,
            padx=12,
            pady=3,
        )
        self.canvas_hint_label.grid(row=0, column=0, sticky="s", pady=(0, 12))
        for overlay in (zoom_box, self.canvas_hint_label):
            try:
                overlay.lift()
            except Exception:  # noqa: BLE001
                pass

        # --- Правая колонка «2 Результат»: вкладки Ведомость/Проверки/Соседи/Журнал
        result_head = ctk.CTkFrame(self.right_panel, fg_color="transparent")
        result_head.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 4))
        result_head.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            result_head,
            text="2",
            width=24,
            height=24,
            corner_radius=6,
            fg_color=THEME["blue_soft_bg"],
            text_color=THEME["blue_soft_fg"],
            font=ctk.CTkFont(size=12, weight="bold"),
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            result_head,
            text="Результат",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color="#ffffff",
        ).grid(row=0, column=1, sticky="w", padx=(10, 0))
        self.result_copy_button = ctk.CTkButton(
            result_head,
            text="📋 Копировать",
            width=104,
            height=26,
            fg_color="transparent",
            hover_color=THEME["btn_secondary"],
            text_color=THEME["text_muted"],
            font=ctk.CTkFont(size=11),
            command=self.copy_inspector_report,
        )
        self.result_copy_button.grid(row=0, column=2, sticky="e")

        # Панель разрешения конфликта проходов LLM и сомнительных мест --------
        self.conflict_bar = ctk.CTkFrame(
            self.alerts_wrap,
            fg_color=THEME["warn_bg"],
            corner_radius=10,
            border_width=1,
            border_color=THEME["warn_border"],
        )
        self.conflict_bar.grid(row=0, column=0, sticky="ew", padx=12, pady=(6, 0))
        self.conflict_bar.grid_columnconfigure(0, weight=1)

        self.conflict_title_var = ctk.StringVar(
            value="⚠ Обнаружено расхождение между проходами LLM!"
        )
        self.conflict_title_label = ctk.CTkLabel(
            self.conflict_bar,
            textvariable=self.conflict_title_var,
            anchor="w",
            justify="left",
            wraplength=860,
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
        self.doubt_bar.grid(row=1, column=0, sticky="ew", padx=12, pady=(6, 0))
        self.doubt_bar.grid_columnconfigure(0, weight=1)

        self.doubt_title_var = ctk.StringVar(
            value="🙋 Обнаружено сомнительное место / подозрение на опечатку!"
        )
        self.doubt_title_label = ctk.CTkLabel(
            self.doubt_bar,
            textvariable=self.doubt_title_var,
            anchor="w",
            justify="left",
            wraplength=860,
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

        # --- Тело правой колонки: 4 вкладки результатов ---------------------
        self.result_tab_var = ctk.StringVar(value="📋 Ведомость")
        # Отдельные кнопки позволяют сохранять цвета каждой вкладки при выборе другой:
        # CTkSegmentedButton переопределяет цвета сегментов при каждом переключении.
        self.result_tabs = ctk.CTkFrame(self.right_panel, fg_color=THEME["bg_field"], corner_radius=8)
        self.result_tabs.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 8))
        self.result_tab_buttons: dict[str, Any] = {}
        for column, label in enumerate(("📋 Ведомость", "🛡 Проверки", "🏘 Соседи", "🧾 Журнал")):
            self.result_tabs.grid_columnconfigure(column, weight=1, uniform="result_tabs")
            button = ctk.CTkButton(
                self.result_tabs, text=label, width=0, height=30, corner_radius=6,
                font=ctk.CTkFont(size=11, weight="bold"),
                command=lambda selected=label: self._select_result_tab(selected),
            )
            button.grid(row=0, column=column, sticky="ew")
            self.result_tab_buttons[label] = button

        result_body = ctk.CTkFrame(self.right_panel, fg_color="transparent")
        result_body.grid(row=2, column=0, sticky="nsew")
        result_body.grid_columnconfigure(0, weight=1)
        result_body.grid_rowconfigure(0, weight=1)

        # — Вкладка «📋 Ведомость»: KPI-карточки + привязка + расписание сторон
        self.sheet_tab_frame = ctk.CTkFrame(result_body, fg_color="transparent")
        self.sheet_tab_frame.grid_columnconfigure(0, weight=1)
        self.sheet_tab_frame.grid_rowconfigure(3, weight=1)

        stats_grid = ctk.CTkFrame(self.sheet_tab_frame, fg_color="transparent")
        stats_grid.grid(row=0, column=0, sticky="ew", padx=12, pady=(2, 8))
        stats_grid.grid_columnconfigure(0, weight=1)
        stats_grid.grid_columnconfigure(1, weight=1)

        def _stat_card(row: int, col: int, title: str, var: Any, accent: str) -> Any:
            card = ctk.CTkFrame(
                stats_grid,
                fg_color=THEME["bg_field"],
                corner_radius=8,
                border_width=1,
                border_color=THEME["border_subtle"],
            )
            card.grid(row=row, column=col, sticky="ew", padx=(0 if col == 0 else 5, 5 if col == 0 else 0), pady=3)
            ctk.CTkLabel(
                card,
                text=title,
                font=self._result_fonts["small"],
                text_color=THEME["text_dim"],
                anchor="w",
            ).grid(row=0, column=0, sticky="w", padx=10, pady=(7, 0))
            value_label = ctk.CTkLabel(
                card,
                textvariable=var,
                font=self._result_fonts["stat"],
                text_color=accent,
                anchor="w",
            )
            value_label.grid(row=1, column=0, sticky="w", padx=10, pady=(0, 7))
            return value_label

        self.closure_var = ctk.StringVar(value="Closure Error: —")
        self.stat_closure_var = ctk.StringVar(value="—")
        self.tokens_var = ctk.StringVar(value="Токены за вызов: —")
        self.closure_label = _stat_card(0, 0, "Невязка (Closure)", self.stat_closure_var, THEME["ok"])
        self.stat_area_var = ctk.StringVar(value="—")
        _stat_card(0, 1, "Площадь", self.stat_area_var, THEME["text_main"])
        self.stat_perimeter_var = ctk.StringVar(value="—")
        _stat_card(1, 0, "Периметр", self.stat_perimeter_var, THEME["text_main"])
        self.stat_text_height_var = ctk.StringVar(value="—")
        _stat_card(1, 1, "Высота текста", self.stat_text_height_var, THEME["sky"])

        self.tie_info_var = ctk.StringVar(value="")
        tie_box = ctk.CTkFrame(
            self.sheet_tab_frame,
            fg_color=THEME["bg_field"],
            corner_radius=8,
            border_width=1,
            border_color=THEME["border_subtle"],
        )
        tie_box.grid(row=1, column=0, sticky="ew", padx=12, pady=(2, 8))
        tie_box.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            tie_box,
            text="ПРИВЯЗКА К ПЕРЕКРЁСТКУ (0.0, 0.0)",
            font=self._result_fonts["small_bold"],
            text_color=THEME["text_dim"],
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=10, pady=(7, 2))
        self.tie_info_label = ctk.CTkLabel(
            tie_box,
            textvariable=self.tie_info_var,
            font=self._result_fonts["mono"],
            text_color=THEME["text_muted"],
            anchor="w",
            justify="left",
            wraplength=self._result_wrap_length(),
        )
        self.tie_info_label.grid(row=1, column=0, sticky="w", padx=10, pady=(0, 8))
        self.tie_info_var.set("Фасад: — · Угол: —\nPOB: (0.00, 0.00) ft")

        self.sides_title_label = ctk.CTkLabel(
            self.sheet_tab_frame,
            text="СТОРОНЫ КОНТУРА (наведите мышью на схеме — подсветка синхронна)",
            font=self._result_fonts["small_bold"],
            wraplength=self._result_wrap_length(),
            justify="left",
            text_color=THEME["text_dim"],
            anchor="w",
        )
        self.sides_title_label.grid(row=2, column=0, sticky="w", padx=14, pady=(0, 2))
        self.inspector_box = ctk.CTkTextbox(
            self.sheet_tab_frame,
            wrap="word",
            fg_color=THEME["bg_editor"],
            border_width=1,
            border_color=THEME["border_subtle"],
            corner_radius=8,
            text_color=THEME["text_main"],
            font=self._result_fonts["mono"],
        )
        self.inspector_box.grid(row=3, column=0, sticky="nsew", padx=12, pady=(0, 12))

        # — Вкладка «🛡 Проверки»: бейдж подозрительности + карточки находок
        self.checks_tab_frame = ctk.CTkFrame(result_body, fg_color="transparent")
        self.checks_tab_frame.grid_columnconfigure(0, weight=1)
        self.checks_tab_frame.grid_rowconfigure(1, weight=1)

        susp_head = ctk.CTkFrame(
            self.checks_tab_frame,
            fg_color=THEME["bg_field"],
            corner_radius=8,
            border_width=1,
            border_color=THEME["border_subtle"],
        )
        susp_head.grid(row=0, column=0, sticky="ew", padx=12, pady=(2, 8))
        susp_head.grid_columnconfigure(0, weight=1)
        self.suspicion_badge_button = ctk.CTkButton(
            susp_head,
            text="🔎 Форма: —",
            height=30,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            border_width=1,
            border_color=THEME["border_card"],
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._open_lot_suspicion_window,
        )
        self.suspicion_badge_button.grid(row=0, column=0, sticky="ew", padx=8, pady=8)

        self.checks_scroll_frame = ctk.CTkScrollableFrame(self.checks_tab_frame, fg_color="transparent")
        self.checks_scroll_frame.grid(row=1, column=0, sticky="nsew", padx=12)

        # — Вкладка «🏘 Соседи»: карточки соседних участков + акт сверки
        self.neighbors_tab_frame = ctk.CTkFrame(result_body, fg_color="transparent")
        self.neighbors_tab_frame.grid_columnconfigure(0, weight=1)
        self.neighbors_tab_frame.grid_rowconfigure(1, weight=1)

        nb_actions = ctk.CTkFrame(self.neighbors_tab_frame, fg_color="transparent")
        nb_actions.grid(row=0, column=0, sticky="ew", padx=12, pady=(2, 6))
        nb_actions.grid_columnconfigure(0, weight=1)
        nb_actions.grid_columnconfigure(1, weight=1)
        self.verify_neighbors_button = ctk.CTkButton(
            nb_actions,
            text="🔍 Сверка с соседями",
            height=34,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._on_verify_neighbors_clicked,
        )
        self.verify_neighbors_button.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self.batch_neighbors_button = ctk.CTkButton(
            nb_actions,
            text="📂 Разобрать файлы…",
            height=28,
            fg_color=THEME["btn_neighbor"],
            hover_color=THEME["btn_neighbor_hover"],
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self._load_neighbor_files,
        )
        self.batch_neighbors_button.grid(row=1, column=0, sticky="ew", padx=(0, 4))
        self.clear_neighbors_button = ctk.CTkButton(
            nb_actions,
            text="🧹 Убрать всех",
            height=26,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            font=ctk.CTkFont(size=11),
            command=self._clear_neighbors_scene,
        )
        self.clear_neighbors_button.grid(row=1, column=1, sticky="ew", padx=(4, 0))

        self.neighbors_scroll_frame = ctk.CTkScrollableFrame(self.neighbors_tab_frame, fg_color="transparent")
        self.neighbors_scroll_frame.grid(row=1, column=0, sticky="nsew", padx=12)

        self.copy_act_button = ctk.CTkButton(
            self.neighbors_tab_frame,
            text="📄 Скопировать акт сверки",
            height=30,
            fg_color=THEME["btn_neighbor"],
            hover_color=THEME["btn_neighbor_hover"],
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._copy_neighbor_act,
        )
        self.copy_act_button.grid(row=2, column=0, sticky="ew", padx=12, pady=(6, 12))

        # — Вкладка «🧾 Журнал»: токены сессии + лента операций
        self.log_tab_frame = ctk.CTkFrame(result_body, fg_color="transparent")
        self.log_tab_frame.grid_columnconfigure(0, weight=1)
        self.log_tab_frame.grid_rowconfigure(1, weight=1)

        log_head = ctk.CTkFrame(self.log_tab_frame, fg_color="transparent")
        log_head.grid(row=0, column=0, sticky="ew", padx=12, pady=(2, 6))
        log_head.grid_columnconfigure(0, weight=1)
        self.tokens_label = ctk.CTkLabel(
            log_head,
            text="",
            anchor="w",
            font=self._result_fonts["mono"],
            text_color=THEME["sky"],
            textvariable=self.tokens_var,
        )
        self.tokens_label.grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            log_head,
            text="Очистить",
            height=24,
            width=80,
            fg_color="transparent",
            hover_color=THEME["btn_secondary"],
            text_color=THEME["text_dim"],
            font=ctk.CTkFont(size=11),
            command=self._clear_log,
        ).grid(row=0, column=1, sticky="e")

        self.log_box = ctk.CTkTextbox(
            self.log_tab_frame,
            wrap="word",
            fg_color=THEME["bg_editor"],
            border_width=1,
            border_color=THEME["border_subtle"],
            corner_radius=8,
            text_color=THEME["text_main"],
            font=self._result_fonts["log"],
        )
        self.log_box.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))

        self._switch_result_tab()

        # 4. Нижняя панель действий: статус + сверка + большая кнопка AutoCAD -
        footer = ctk.CTkFrame(
            self,
            fg_color=THEME["bg_header"],
            corner_radius=0,
            border_width=0,
            height=64,
        )
        footer.grid(row=3, column=0, sticky="ew")
        footer.grid_propagate(False)
        footer.grid_columnconfigure(1, weight=1)

        self.status_dot = ctk.CTkLabel(
            footer,
            text="●",
            font=ctk.CTkFont(size=12),
            text_color=THEME["ok"],
        )
        self.status_dot.grid(row=0, column=0, padx=(16, 8))

        self.status_var = ctk.StringVar(
            value="Готово. Загрузите PDF, выберите пример или вставьте текст Legal Description."
        )
        self.status_label = ctk.CTkLabel(
            footer,
            textvariable=self.status_var,
            anchor="w",
            font=ctk.CTkFont(size=12),
            text_color=THEME["text_muted"],
            justify="left",
        )
        self.status_label.grid(row=0, column=1, sticky="w")

        self.add_neighbor_button = ctk.CTkButton(
            footer,
            text="🏘 Начертить рассчитанных соседей",
            height=38,
            width=225,
            fg_color=THEME["btn_neighbor"],
            hover_color=THEME["btn_neighbor_hover"],
            font=ctk.CTkFont(size=12, weight="bold"),
            command=lambda: self._start(parse_only=False, force_neighbor=True),
        )
        self.add_neighbor_button.grid(row=0, column=2, padx=(8, 6))

        self.draw_button = ctk.CTkButton(
            footer,
            text="▶  Начертить схему в AutoCAD   F5",
            height=42,
            width=300,
            fg_color=THEME["btn_primary"],
            hover_color=THEME["btn_primary_hover"],
            font=ctk.CTkFont(size=14, weight="bold"),
            command=lambda: self._start(parse_only=False),
        )
        self.draw_button.grid(row=0, column=3, padx=(6, 16), pady=11, sticky="e")

        # 5. Страницы остальных глобальных вкладок (Шаг 0 и Шаг 2) — поверх той же строки
        self._build_step0_page()
        self._build_step2_page()
        self._switch_step_tab()

    # ------------------------------------------- глобальные вкладки шагов ---
    def _build_step_tab_bar(self) -> None:
        """Полоса глобальных вкладок: Шаг 1 (граница участка) | Шаг 2 (подписи объектов)."""
        bar = ctk.CTkFrame(self, corner_radius=0, fg_color=THEME["bg_root"], border_width=0)
        bar.grid(row=1, column=0, sticky="ew")
        bar.grid_columnconfigure(len(STEP_TABS), weight=1)
        self.step_tab_bar = bar
        self.step_tab_buttons: dict[str, Any] = {}
        for column, label in enumerate(STEP_TABS):
            button = ctk.CTkButton(
                bar,
                text=label,
                height=32,
                width=0,
                corner_radius=7,
                border_width=1,
                font=ctk.CTkFont(size=12, weight="bold"),
                command=lambda selected=label: self._select_step_tab(selected),
            )
            button.grid(
                row=0, column=column, sticky="w",
                padx=(16 if column == 0 else 6, 0), pady=(8, 8),
            )
            self.step_tab_buttons[label] = button
        ctk.CTkLabel(
            bar,
            text=(
                "Порядок работы: 0 — документы участка → 1 — посадка границы → "
                "2 — подписи объектов → оффсеты"
            ),
            font=ctk.CTkFont(size=11),
            text_color=THEME["text_dim"],
            anchor="e",
        ).grid(row=0, column=len(STEP_TABS), sticky="e", padx=(8, 16))

    # ----------------------------------------------- Шаг 0: документы участка ---
    def _build_step0_page(self) -> None:
        """Страница «Шаг 0. Документы участка»: block/lot → папка описаний."""
        page = ctk.CTkFrame(self, fg_color=THEME["bg_root"], corner_radius=0)
        page.grid(row=2, column=0, sticky="nsew")
        page.grid_columnconfigure(0, weight=0)
        page.grid_columnconfigure(1, weight=1)
        page.grid_rowconfigure(0, weight=1)
        self.step0_page = page

        panel = ctk.CTkFrame(
            page,
            width=470,
            fg_color=THEME["bg_card"],
            corner_radius=10,
            border_width=1,
            border_color=THEME["border_card"],
        )
        panel.grid(row=0, column=0, sticky="nsw", padx=(16, 8), pady=(4, 12))
        panel.grid_propagate(False)
        panel.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            panel,
            text=STEP_TAB_DEEDS,
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=THEME["text_main"],
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 2))
        ctk.CTkLabel(
            panel,
            text=(
                "Staten Island — Richmond County: список документов участка, Deed новее 2015 года, "
                "иначе свежая Mortgage с 2015 года. Наш участок — самый свежий и самый старый "
                "документ (старый Deed), сосед — только свежий. "
                "Имена: OUR 32 NEW.pdf, OUR 32 OLD.pdf, LOT 30 NEW.pdf. "
                "Block/Lot и Borough читает локальный OCR; выбранной Vision-модели отправляется "
                "только обрезка карты для поиска двух соседних лотов."
            ),
            font=ctk.CTkFont(size=11),
            text_color=THEME["text_dim"],
            anchor="w",
            justify="left",
            wraplength=430,
        ).grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 10))

        fields = ctk.CTkFrame(panel, fg_color="transparent")
        fields.grid(row=2, column=0, sticky="ew", padx=16)
        fields.grid_columnconfigure(0, weight=1)

        def field(row: int, label: str, var: Any, placeholder: str = "", width: int = 0) -> Any:
            ctk.CTkLabel(
                fields,
                text=label,
                font=ctk.CTkFont(size=11),
                text_color=THEME["text_muted"],
                anchor="w",
            ).grid(row=row * 2, column=0, sticky="ew", pady=(6, 0))
            entry = ctk.CTkEntry(
                fields,
                textvariable=var,
                placeholder_text=placeholder,
                height=32,
                fg_color=THEME["bg_field"],
                border_color=THEME["border_card"],
                font=ctk.CTkFont(size=12),
            )
            entry.grid(row=row * 2 + 1, column=0, sticky="ew", pady=(2, 0))
            return entry

        field(0, "Адрес объекта (как на Property Information Portal)", self.step0_address_var,
              "87 Stafford Ave")
        field(1, "Borough (район Нью-Йорка)", self.step0_borough_var, "Staten Island")
        self.step0_portal_button = ctk.CTkButton(
            fields, text="🌐 Найти по адресу + анализ только карты", height=32,
            fg_color=THEME["btn_secondary"], hover_color=THEME["btn_secondary_hover"],
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self._start_step0_portal_lookup,
        )
        self.step0_portal_button.grid(row=4, column=0, sticky="ew", pady=(7, 2))
        numbers = ctk.CTkFrame(fields, fg_color="transparent")
        numbers.grid(row=5, column=0, sticky="ew")
        numbers.grid_columnconfigure(0, weight=1)
        numbers.grid_columnconfigure(1, weight=1)
        numbers.grid_columnconfigure(2, weight=1)
        for column, (label, var, placeholder) in enumerate(
            (
                ("Block", self.step0_block_var, "6323"),
                ("Lot", self.step0_lot_var, "32"),
                ("OCR dpi (50–1200)", self.step0_ocr_dpi_var, "400"),
            )
        ):
            ctk.CTkLabel(
                numbers, text=label, font=ctk.CTkFont(size=11),
                text_color=THEME["text_muted"], anchor="w",
            ).grid(row=0, column=column, sticky="ew", padx=(0, 8) if column == 0 else 0)
            ctk.CTkEntry(
                numbers, textvariable=var, placeholder_text=placeholder,
                height=32, width=120, fg_color=THEME["bg_field"],
                border_color=THEME["border_card"], font=ctk.CTkFont(size=12),
            ).grid(row=1, column=column, sticky="ew", padx=(0, 8) if column == 0 else 0, pady=(2, 0))

        field(3, "Соседи слева и справа на карте (проверьте значения)",
              self.step0_neighbours_var, "например: 68, 72")
        checks = ctk.CTkFrame(panel, fg_color="transparent")
        checks.grid(row=3, column=0, sticky="ew", padx=16, pady=(10, 4))
        ctk.CTkCheckBox(
            checks,
            text="докачать старый документ соседей (если межа соседа спорная)",
            variable=self.step0_old_neighbour_var,
            font=ctk.CTkFont(size=11),
            text_color=THEME["text_muted"],
            checkbox_width=18,
            checkbox_height=18,
        ).grid(row=0, column=0, sticky="w")
        fast_row = ctk.CTkFrame(checks, fg_color="transparent")
        fast_row.grid(row=1, column=0, sticky="w", pady=(4, 0))
        ctk.CTkCheckBox(
            fast_row,
            text="быстрая работа (меньше проверок экрана), пауза",
            variable=self.step0_fast_mode_var,
            font=ctk.CTkFont(size=11),
            text_color=THEME["text_muted"],
            checkbox_width=18,
            checkbox_height=18,
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkEntry(
            fast_row,
            textvariable=self.step0_fast_pause_var,
            width=48,
            justify="center",
            font=ctk.CTkFont(size=11),
        ).grid(row=0, column=1, sticky="w", padx=(6, 4))
        ctk.CTkLabel(
            fast_row,
            text=f"с (от {nyc_portal.FAST_PAUSE_MIN_SEC:g} до {nyc_portal.FAST_PAUSE_MAX_SEC:g})",
            font=ctk.CTkFont(size=11),
            text_color=THEME["text_muted"],
        ).grid(row=0, column=2, sticky="w")
        folder_row = ctk.CTkFrame(panel, fg_color="transparent")
        folder_row.grid(row=4, column=0, sticky="ew", padx=16, pady=(4, 0))
        folder_row.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            folder_row, text="Папка описаний", font=ctk.CTkFont(size=11),
            text_color=THEME["text_muted"], anchor="w",
        ).grid(row=0, column=0, sticky="ew")
        ctk.CTkEntry(
            folder_row, textvariable=self.step0_folder_var, height=30,
            placeholder_text="пусто — рядом с программой",
            fg_color=THEME["bg_field"], border_color=THEME["border_card"],
            font=ctk.CTkFont(size=11),
        ).grid(row=1, column=0, sticky="ew", pady=(2, 0))
        ctk.CTkButton(
            folder_row, text="…", width=36, height=30, fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"], command=self._pick_step0_folder,
        ).grid(row=1, column=1, padx=(6, 0), pady=(2, 0))

        self.step0_run_button = ctk.CTkButton(
            panel,
            text="📥 Собрать описания",
            height=38,
            fg_color=THEME["btn_primary"],
            hover_color=THEME["btn_primary_hover"],
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._start_step0_intake,
        )
        self.step0_run_button.grid(row=5, column=0, sticky="ew", padx=16, pady=(12, 4))

        run_row = ctk.CTkFrame(panel, fg_color="transparent")
        run_row.grid(row=6, column=0, sticky="ew", padx=16)
        run_row.grid_columnconfigure(0, weight=1)
        run_row.grid_columnconfigure(1, weight=1)
        self.step0_stop_button = ctk.CTkButton(
            run_row, text="⏹ Стоп", height=30, fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"], state="disabled",
            command=self._stop_step0_intake,
        )
        self.step0_stop_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.step0_open_button = ctk.CTkButton(
            run_row, text="📂 Открыть папку", height=30, fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"], command=self._open_step0_folder,
        )
        self.step0_open_button.grid(row=0, column=1, sticky="ew", padx=(4, 0))

        # Ручная докачка: файл уже скачан в браузере — остаётся сказать, чей он.
        manual = ctk.CTkFrame(
            panel, fg_color=THEME["bg_field"], corner_radius=8,
            border_width=1, border_color=THEME["border_subtle"],
        )
        manual.grid(row=7, column=0, sticky="ew", padx=16, pady=(12, 14))
        manual.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            manual, text="Файл скачан вручную", font=ctk.CTkFont(size=11, weight="bold"),
            text_color=THEME["text_main"], anchor="w",
        ).grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(8, 2))
        ctk.CTkLabel(
            manual,
            text="PDF уже лежит в «Загрузках» — программа назовёт его по схеме и добавит в папку.",
            font=ctk.CTkFont(size=10), text_color=THEME["text_dim"],
            anchor="w", justify="left", wraplength=400,
        ).grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 6))
        manual_row = ctk.CTkFrame(manual, fg_color="transparent")
        manual_row.grid(row=2, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 6))
        manual_row.grid_columnconfigure(2, weight=1)
        self.step0_manual_role_menu = ctk.CTkOptionMenu(
            manual_row, values=list(STEP0_MANUAL_ROLES),
            variable=self.step0_manual_role_var, width=130, height=28,
            fg_color=THEME["bg_editor"], button_color=THEME["seg_sel"],
            font=ctk.CTkFont(size=11),
        )
        self.step0_manual_role_menu.grid(row=0, column=0, padx=(0, 6))
        ctk.CTkLabel(
            manual_row, text="лот", font=ctk.CTkFont(size=11),
            text_color=THEME["text_muted"],
        ).grid(row=0, column=1, padx=(0, 4))
        ctk.CTkEntry(
            manual_row, textvariable=self.step0_manual_lot_var, width=70, height=28,
            placeholder_text="30", fg_color=THEME["bg_editor"],
            border_color=THEME["border_card"], font=ctk.CTkFont(size=11),
        ).grid(row=0, column=2, sticky="w")
        ctk.CTkButton(
            manual, text="📂 Забрать свежий PDF из «Загрузок»", height=30,
            fg_color=THEME["btn_secondary"], hover_color=THEME["btn_secondary_hover"],
            command=self._adopt_step0_download,
        ).grid(row=3, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 10))

        # Правая колонка: отчёт сбора
        report_wrap = ctk.CTkFrame(
            page, fg_color=THEME["bg_card"], corner_radius=10,
            border_width=1, border_color=THEME["border_card"],
        )
        report_wrap.grid(row=0, column=1, sticky="nsew", padx=(8, 16), pady=(4, 12))
        report_wrap.grid_columnconfigure(0, weight=1)
        report_wrap.grid_rowconfigure(1, weight=1)
        head = ctk.CTkFrame(report_wrap, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 6))
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            head, text="Ход сбора и результат", font=ctk.CTkFont(size=13, weight="bold"),
            text_color=THEME["text_main"], anchor="w",
        ).grid(row=0, column=0, sticky="w")
        self.step0_to_step1_button = ctk.CTkButton(
            head, text="→ Передать в Шаг 1", height=28, width=170,
            fg_color=THEME["btn_secondary"], hover_color=THEME["btn_secondary_hover"],
            font=ctk.CTkFont(size=11), state="disabled",
            command=self._step0_to_step1,
        )
        self.step0_to_step1_button.grid(row=0, column=1, sticky="e")
        self.step0_report_box = ctk.CTkTextbox(
            report_wrap,
            fg_color=THEME["bg_editor"],
            border_width=0,
            font=ctk.CTkFont(family="Consolas", size=12),
            wrap="word",
        )
        self.step0_report_box.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 14))
        self._set_step0_report(STEP0_EMPTY_REPORT)

    # --- Шаг 0: работа с полями и запуск -------------------------------------
    def _is_step0_tab(self) -> bool:
        var = getattr(self, "step_tab_var", None)
        return var is not None and str(var.get()) == STEP_TAB_DEEDS

    def _set_step0_report(self, content: str) -> None:
        box = getattr(self, "step0_report_box", None)
        if box is None:
            return
        target = getattr(box, "_textbox", box)
        try:
            target.configure(state="normal")
        except Exception:  # noqa: BLE001
            pass
        box.delete("1.0", "end")
        box.insert("1.0", content)
        try:
            target.configure(state="disabled")
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _step0_timestamp(message: str) -> str:
        """Метка местного времени для каждого события в журнале Шага 0."""
        return f"[{datetime.now().strftime('%H:%M:%S')}] {message}"

    def _step0_log(self, message: str, level: str = log_mode.LEVEL_INFO) -> None:
        self._step0_lines.append((level, self._step0_timestamp(message)))
        self._render_step0_log()

    def _render_step0_log(self) -> None:
        visible = log_mode.filter_entries(self._step0_lines, self._log_mode())
        self._set_step0_report("\n".join(visible) or STEP0_EMPTY_REPORT)

    def _step0_folder(self) -> Path:
        text = str(self.step0_folder_var.get() or "").strip()
        if text:
            return Path(text)
        return deed_intake.build_folder_for(str(self.step0_address_var.get() or ""))

    def _pick_step0_folder(self) -> None:
        from tkinter import filedialog

        chosen = filedialog.askdirectory(title="Папка описаний")
        if chosen:
            self.step0_folder_var.set(str(chosen))

    def _step0_fast_pause_value(self) -> float | None:
        """
        Пауза «быстрой работы» из поля. Без галочки значение не используется — возвращаем 5 с.
        Если галочка стоит, а число неверное, выводит сообщение и возвращает None: запуск не начинаем.
        """
        if not self.step0_fast_mode_var.get():
            return nyc_portal.FAST_SCREEN_CHECK_PAUSE_SEC
        value = nyc_portal.parse_fast_pause(self.step0_fast_pause_var.get())
        if value is None:
            self._set_status(
                f"Шаг 0: пауза «быстрой работы» — число от {nyc_portal.FAST_PAUSE_MIN_SEC:g} "
                f"до {nyc_portal.FAST_PAUSE_MAX_SEC:g} секунд.",
                error=True,
            )
        return value

    def _start_step0_portal_lookup(self) -> None:
        """Address → NYC Portal → one user-initiated Vision extraction call."""
        if self._step0_portal_running or self._step0_running:
            self._set_status("Шаг 0 уже выполняет поиск или сбор; дождитесь завершения.", warn=True)
            return
        if self._busy:
            self._set_status("Дождитесь окончания текущего разбора перед поиском участка.", warn=True)
            return
        address = str(self.step0_address_var.get() or "").strip()
        if not address:
            self._set_status("Шаг 0: сначала введите адрес (номер дома и улицу).", error=True)
            return
        fast_pause = self._step0_fast_pause_value()
        if fast_pause is None:
            return
        try:
            cfg, provider, model, api_key, key_secret = self._build_runtime_config()
            target = resolve_api_target(
                cfg, provider=provider, model=model, api_key=api_key, key_secret=key_secret,
            )
            if not target.get("api_key"):
                raise LLMParseError(
                    "Для чтения снимка нужен API-ключ выбранного Vision-провайдера. "
                    "Откройте «Настройки», задайте ключ и повторите."
                )
        except Exception as exc:  # noqa: BLE001 - protected/missing provider key or bad settings
            self._set_status(f"Шаг 0: не удалось подготовить Vision: {exc}", error=True)
            return

        folder = self._step0_folder()
        self.step0_folder_var.set(str(folder))
        self._step0_portal_restore_topmost = bool(self.topmost_var.get())
        if self._step0_portal_restore_topmost:
            # The CAD window must not cover the browser while we capture its screen.
            self.attributes("-topmost", False)
        self._step0_portal_running = True
        self.step0_portal_button.configure(state="disabled", text="⏳ Ищу адрес…")
        self.step0_run_button.configure(state="disabled")
        self._step0_lines = [(log_mode.LEVEL_KEY, self._step0_timestamp(
            f"Ищу {address!r} в текущем окне NYC Portal. Block/Lot/Borough читает OCR; один Vision-запрос получит только карту."
        ))]
        self._render_step0_log()
        self._set_status("Шаг 0: ищу адрес в текущем окне браузера…")
        worker = threading.Thread(
            target=self._step0_portal_worker,
            args=(
                address, folder, cfg, provider, model, target["api_key"], key_secret,
                bool(self.step0_fast_mode_var.get()), fast_pause,
            ),
            daemon=True,
        )
        worker.start()

    def _step0_portal_worker(
        self,
        address: str,
        folder: Path,
        cfg: dict[str, Any],
        provider: str,
        model: str,
        api_key: str,
        key_secret: str,
        fast_mode: bool = False,
        fast_pause: float = nyc_portal.FAST_SCREEN_CHECK_PAUSE_SEC,
    ) -> None:
        """Browser UI, screenshot and Vision run off the Tk thread."""
        self._worker_context.generation = self._generation

        def progress(line: str) -> None:
            self._post("step0_progress", self._step0_timestamp(line))

        try:
            result = nyc_portal.lookup_parcel(
                address,
                folder,
                cfg=cfg,
                provider=provider,
                model=model,
                api_key=api_key,
                key_secret=key_secret,
                progress=progress,
                fast_mode=fast_mode,
                fast_pause_sec=fast_pause,
            )
        except Exception as exc:  # noqa: BLE001 - browser, OCR and provider failures
            self._post("step0_portal_done", ("error", f"{type(exc).__name__}: {exc}"))
            return
        self._post("step0_portal_done", ("ok", result))

    def _finish_step0_portal_lookup(self, status: str, payload: Any) -> None:
        self._step0_portal_running = False
        if self._step0_portal_restore_topmost is not None:
            self.attributes("-topmost", self._step0_portal_restore_topmost)
            self._step0_portal_restore_topmost = None
        self.step0_portal_button.configure(
            state="normal", text="🌐 Найти по адресу + анализ только карты",
        )
        self.step0_run_button.configure(state="normal")
        try:
            self.lift()
        except Exception:  # noqa: BLE001 - a closing window needs no focus restoration
            pass
        if status != "ok":
            self._step0_log(f"Поиск участка не завершён: {payload}", level=log_mode.LEVEL_ERROR)
            self._set_status(f"Шаг 0: поиск не завершён — {payload}", error=True)
            return
        result = payload
        self.step0_borough_var.set(result.borough or "")
        self.step0_block_var.set(result.block)
        self.step0_lot_var.set(result.lot)
        self.step0_neighbours_var.set(", ".join(result.neighbours))
        self._record_round_usages([result.usage])
        self._step0_log(
            f"Локальный OCR: Borough {result.borough or 'не распознан'}, "
            f"Block {result.block}, Lot {result.lot}. Vision только по карте: "
            f"соседи слева/справа {result.left_neighbour or '—'} / "
            f"{result.right_neighbour or '—'}; уверенность: {result.confidence}.",
            level=log_mode.LEVEL_KEY,
        )
        if result.notes:
            self._step0_log(f"Примечание Vision: {result.notes}")
        self._step0_log(f"Vision получал только карту: {result.map_image}")
        self._set_status(
            f"Шаг 0: {result.borough or 'район не распознан'}, Block {result.block}, Lot {result.lot}; "
            "проверьте соседей и затем нажмите «Собрать описания».",
            warn=result.confidence.lower() in {"low", "низкая"},
            key=True,
        )

    def _start_step0_intake(self) -> None:
        """Собирает описания в фоне: окно не замирает на Cloudflare и OCR."""
        if self._step0_portal_running:
            self._set_status("Сначала дождитесь завершения поиска адреса на портале.", warn=True)
            return
        if self._step0_running:
            self._set_status("Шаг 0: сбор уже идёт — дождитесь конца или нажмите «Стоп».")
            return
        try:
            block = int(str(self.step0_block_var.get() or "").strip())
            lot = int(str(self.step0_lot_var.get() or "").strip())
        except (TypeError, ValueError):
            self._set_status("Шаг 0: впишите block и lot цифрами (например, 6323 и 32).", error=True)
            return
        try:
            neighbours = deed_intake.parse_lot_list(str(self.step0_neighbours_var.get() or ""))
        except ValueError as exc:
            self._set_status(
                f"Шаг 0: соседи — {exc}. Впишите номера лотов через запятую, например «30, 28».",
                error=True,
            )
            return
        try:
            ocr_dpi = int(str(self.step0_ocr_dpi_var.get() or "").strip())
        except (TypeError, ValueError):
            self._set_status("Шаг 0: DPI OCR задаётся целым числом от 50 до 1200.", error=True)
            return
        if not ocr_pages.MIN_DPI <= ocr_dpi <= ocr_pages.MAX_DPI:
            self._set_status(
                f"Шаг 0: DPI OCR должен быть от {ocr_pages.MIN_DPI} до {ocr_pages.MAX_DPI}.",
                error=True,
            )
            return
        fast_pause = self._step0_fast_pause_value()
        if fast_pause is None:
            return
        if lot in neighbours:
            neighbours = [value for value in neighbours if value != lot]
            self._set_status("Шаг 0: наш лот убран из списка соседей — он собирается как наш участок.", warn=True)
        folder = self._step0_folder()
        self.step0_folder_var.set(str(folder))
        previous_dpi = (self.config_data.get("ocr") or {}).get("dpi", ocr_pages.DEFAULT_DPI)
        self.config_data.setdefault("ocr", {})["dpi"] = ocr_dpi
        dpi_save_error = ""
        if str(previous_dpi).strip() != str(ocr_dpi):
            try:
                save_config(self.config_data)
            except OSError as exc:
                dpi_save_error = f"не удалось сохранить DPI в config.json: {exc}"
        self._step0_stop.clear()
        self._step0_running = True
        self._step0_outcome = None
        self.step0_portal_button.configure(state="disabled")
        self.step0_to_step1_button.configure(state="disabled")
        self.step0_run_button.configure(state="disabled", text="⏳ Собираю…")
        self.step0_stop_button.configure(state="normal")
        self._step0_lines = [(log_mode.LEVEL_KEY, self._step0_timestamp(
            f"Начинаю сбор: блок {block}, лот {lot}"
            + (f"; соседи: {', '.join(str(value) for value in neighbours)}" if neighbours else "")
            + f"; OCR: {ocr_dpi} dpi; папка: {folder}"
        ))]
        if dpi_save_error:
            self._step0_lines.append((log_mode.LEVEL_ERROR, self._step0_timestamp(dpi_save_error)))
        self._render_step0_log()
        self._set_status("Шаг 0: собираю описания участка…")
        worker = threading.Thread(
            target=self._step0_worker,
            args=(
                block, lot, neighbours, folder,
                bool(self.step0_old_neighbour_var.get()), ocr_dpi,
                bool(self.step0_fast_mode_var.get()), fast_pause,
            ),
            daemon=True,
        )
        worker.start()

    def _step0_worker(
        self,
        block: int,
        lot: int,
        neighbours: list[int],
        folder: Path,
        with_old: bool,
        ocr_dpi: int = ocr_pages.DEFAULT_DPI,
        fast_mode: bool = False,
        fast_pause: float = nyc_portal.FAST_SCREEN_CHECK_PAUSE_SEC,
    ) -> None:
        """Фоновый сбор: сеть и OCR. Результат возвращается через очередь событий."""
        self._worker_context.generation = self._generation

        def progress(line: str) -> None:
            self._post("step0_progress", self._step0_timestamp(line))

        downloads = rcclerk.default_downloads_dir()
        auto_save = None
        if browser_save.available():
            auto_save = browser_save.auto_save_hook(
                downloads,
                ready_delay=fast_pause if fast_mode else 4.0,
                stable_for=0.0 if fast_mode else 1.0,
                on_attempt=lambda title: progress(
                    f"PDF уже загружен в окне браузера «{title}» — нажимаю Ctrl+S один раз"
                ),
            )
            mode_note = f" (быстрая работа: пауза {fast_pause:g} с)" if fast_mode else ""
            progress(f"браузер: Ctrl+S сработает только после загрузки PDF; Cloudflare не трогаю{mode_note}")
        else:
            progress("браузер: авто-Ctrl+S доступен только в Windows; если нужно, сохраните PDF вручную")
        progress(f"поиск начала/конца описания: OCR настроен на {ocr_dpi} dpi")

        try:
            outcome = deed_intake.collect_deeds(
                block=block,
                lot=lot,
                neighbours=neighbours,
                folder=folder,
                with_old_for_neighbour=with_old,
                progress=progress,
                should_stop=self._step0_stop.is_set,
                auto_save=auto_save,
                ocr_dpi=ocr_dpi,
                downloads_dir=downloads,
            )
        except deed_intake.IntakeCancelled:
            self._post("step0_done", ("cancelled", None))
            return
        except Exception as exc:  # noqa: BLE001 - сеть, файлы, PDF — всё возможно
            self._post("step0_done", ("error", f"{type(exc).__name__}: {exc}"))
            return
        self._post("step0_done", ("ok", outcome))

    def _stop_step0_intake(self) -> None:
        if not self._step0_running:
            return
        self._step0_stop.set()
        self._step0_log("Запрошена остановка; завершу текущий документ и сохраню собранное.", level=log_mode.LEVEL_KEY)
        self._set_status("Шаг 0: останавливаю после текущего документа…")

    def _finish_step0(self, status: str, payload: Any) -> None:
        self._step0_running = False
        self.step0_portal_button.configure(state="normal")
        self.step0_run_button.configure(state="normal", text="📥 Собрать описания")
        self.step0_stop_button.configure(state="disabled")
        if status == "cancelled":
            self._step0_log("Сбор остановлен — собранное осталось в папке.", level=log_mode.LEVEL_KEY)
            self._set_status("Шаг 0: сбор остановлен.", warn=True)
            return
        if status == "error":
            self._step0_log(f"Ошибка: {payload}", level=log_mode.LEVEL_ERROR)
            self._set_status(f"Шаг 0: ошибка — {payload}", error=True)
            return
        outcome = payload
        self._step0_outcome = outcome
        self._step0_log(f"Сбор завершён: документов в результате — {len(outcome.records)}", level=log_mode.LEVEL_KEY)
        self._step0_lines.append((
            log_mode.LEVEL_ERROR if outcome.missing else log_mode.LEVEL_KEY,
            outcome.report(include_notes=False),
        ))
        self._render_step0_log()
        if outcome.ok:
            self.step0_to_step1_button.configure(state="normal")
            self._set_status(
                f"Шаг 0: собрано документов — {len(outcome.records)}. Папка: {outcome.folder}",
                key=True,
            )
        else:
            self._set_status(
                "Шаг 0: наш участок не собран — смотрите отчёт и «Чего не хватает».",
                error=True,
            )

    def _open_step0_folder(self) -> None:
        folder = self._step0_folder()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._set_status(f"Шаг 0: папку не создать — {exc}", error=True)
            return
        try:
            if hasattr(os, "startfile"):
                os.startfile(str(folder))  # type: ignore[attr-defined]
            else:
                import subprocess

                subprocess.Popen(
                    ["xdg-open", str(folder)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
        except Exception as exc:  # noqa: BLE001 - проводник может быть недоступен
            self._set_status(f"Шаг 0: не удалось открыть папку {folder} — {exc}", error=True)

    def _step0_manual_role(self) -> str:
        value = str(self.step0_manual_role_var.get() or "")
        return "our" if value == STEP0_MANUAL_ROLES[0] else "neighbour"

    def _adopt_step0_download(self) -> None:
        """Забирает свежий PDF из «Загрузок» под нужным именем (ступень 3 лестницы)."""
        self._step0_log("Забираю вручную скачанный PDF из «Загрузок»…")
        role = self._step0_manual_role()
        text = str(self.step0_manual_lot_var.get() or "").strip()
        if not text:
            text = str(self.step0_lot_var.get() or "").strip()
        try:
            lot = int(text)
        except (TypeError, ValueError):
            self._set_status("Шаг 0: впишите номер лота для скачанного файла.", error=True)
            return
        folder = self._step0_folder()
        try:
            record = deed_intake.adopt_downloads(
                folder, role=role, lot=lot, tag=rcclerk.NEW_TAG,
                block=str(self.step0_block_var.get() or "").strip(),
            )
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"Шаг 0: файл не принят — {exc}", error=True)
            return
        if record is None:
            self._set_status(
                "Шаг 0: в «Загрузках» нет нового PDF. Скачайте документ в браузере и повторите.",
                warn=True,
            )
            return
        self._step0_log(f"Из «Загрузок» принят файл: {record.file}", level=log_mode.LEVEL_KEY)
        self._set_status(f"Шаг 0: файл «{record.file}» добавлен в папку описаний.")

    def _step0_to_step1(self) -> None:
        """Открывает собранные описания в Шаге 1: наш участок и соседи одним набором."""
        self._step0_log("Передаю документы в Шаг 1: проверяю manifest и паспорта файлов…")
        folder = self._step0_folder()
        try:
            manifest = deed_store.read_manifest(folder)
        except deed_store.DeedStoreError as exc:
            self._step0_log(f"Manifest не читается ({exc}); проверяю паспорта .json рядом с PDF")
            manifest = {}
        raw_items = manifest.get("items") or []
        items_meta = [dict(item) for item in raw_items if isinstance(item, dict)]

        # Если общий список неполон/устарел, восстановим ожидаемые документы по
        # паспортам. Так сосед не потеряется лишь из-за отсутствующей записи manifest.
        expected: set[tuple[str, str, str]] = set()
        try:
            our_lot = str(int(str(self.step0_lot_var.get() or "").strip()))
            expected.update({("our", our_lot, "NEW"), ("our", our_lot, "OLD")})
            neighbour_lots = deed_intake.parse_lot_list(str(self.step0_neighbours_var.get() or ""))
            neighbour_tags = ["NEW"] + (["OLD"] if self.step0_old_neighbour_var.get() else [])
            expected.update(
                ("neighbour", str(lot), tag)
                for lot in neighbour_lots for tag in neighbour_tags
            )
        except (TypeError, ValueError):
            neighbour_lots = []

        def item_key(item: dict[str, Any]) -> tuple[str, str, str]:
            return (
                str(item.get("role") or "").lower(),
                str(item.get("lot") or ""),
                str(item.get("tag") or "NEW").upper(),
            )

        try:
            sidecars = deed_store.read_records(folder)
        except Exception as exc:  # noqa: BLE001 - повреждённый паспорт не мешает остальным
            sidecars = []
            self._step0_log(f"Не удалось прочитать паспорта файлов: {exc}", level=log_mode.LEVEL_ERROR)
        by_key = {item_key(item): item for item in items_meta}
        for record in sidecars:
            record_data = dataclasses.asdict(record)
            key = item_key(record_data)
            if key not in expected:
                continue
            existing = by_key.get(key)
            if existing is None:
                items_meta.append(record_data)
                by_key[key] = record_data
                self._step0_log(
                    f"В manifest не было {record.file}; восстановил запись по паспорту документа"
                )
                continue
            # Manifest and sidecar are written together; if one reference went stale,
            # use an existing file from the passport rather than silently dropping it.
            for field_name in ("file", "trimmed"):
                recovered = str(record_data.get(field_name) or "").strip()
                current = str(existing.get(field_name) or "").strip()
                if (recovered and (not current or not (folder / current).is_file())
                        and (folder / recovered).is_file()):
                    existing[field_name] = recovered
                    self._step0_log(
                        f"Обновил путь {field_name} для {record.file} по паспорту документа"
                    )

        paths: list[Path] = []
        our_new_paths: set[Path] = set()
        for item in items_meta:
            role = str(item.get("role") or "").lower()
            tag = str(item.get("tag") or "NEW").upper()
            if role == "our" and tag == "OLD":
                continue
            full_name = str(item.get("file") or "").strip()
            trimmed_name = str(item.get("trimmed") or "").strip()
            trimmed_path = folder / trimmed_name if trimmed_name else None
            full_path = folder / full_name if full_name else None
            if trimmed_path is not None and trimmed_path.is_file():
                chosen = trimmed_path
            elif full_path is not None and full_path.is_file():
                chosen = full_path
                if trimmed_name:
                    self._step0_log(
                        f"{trimmed_name} отсутствует; передаю полный документ {full_name}"
                    )
            else:
                expected_name = trimmed_name or full_name or "(имя в manifest не указано)"
                self._step0_log(f"Файл из manifest не найден и пропущен: {expected_name}", level=log_mode.LEVEL_ERROR)
                continue
            if chosen not in paths:
                paths.append(chosen)
            if role == "our" and tag == "NEW":
                our_new_paths.add(chosen)

        if not paths:
            self._step0_log("Передавать нечего: в папке описаний не найдено PDF", level=log_mode.LEVEL_ERROR)
            self._set_status("Шаг 0: в папке описаний нет файлов для Шага 1.", error=True)
            return
        self._step0_log(
            f"Отправляю в Шаг 1 ({len(paths)} PDF): "
            + "; ".join(path.name for path in paths),
            level=log_mode.LEVEL_KEY,
        )
        items, errors = self._make_pdf_set_items(paths)
        if our_new_paths:
            for item in items:
                item.is_main = item.path in our_new_paths
        for error in errors:
            self._step0_log(f"Файл не открылся для Шага 1: {error}", level=log_mode.LEVEL_ERROR)
            self._log_line(f"! Шаг 0 → Шаг 1: {error}")
        if not items:
            self._set_status(
                f"Шаг 0: файлы не открылись — {errors[0] if errors else 'нечего открывать'}",
                error=True,
            )
            return
        self._open_pdf_set_dialog(items, fresh=True, notes=errors)

    def _build_step2_page(self) -> None:
        """Страница «Шаг 2. Автоподписание объектов (D-TOPO-TEXT)»."""
        page = ctk.CTkFrame(self, fg_color=THEME["bg_root"], corner_radius=0)
        page.grid(row=2, column=0, sticky="nsew")
        page.grid_columnconfigure(0, weight=0)
        page.grid_columnconfigure(1, weight=1)
        page.grid_rowconfigure(0, weight=1)
        self.step2_page = page

        # --- Левая карточка: параметры и запуск ------------------------------
        panel = ctk.CTkFrame(
            page,
            width=460,
            fg_color=THEME["bg_card"],
            corner_radius=10,
            border_width=1,
            border_color=THEME["border_card"],
        )
        panel.grid(row=0, column=0, sticky="nsw", padx=(16, 8), pady=(4, 12))
        panel.grid_propagate(False)
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(11, weight=1)

        ctk.CTkLabel(
            panel,
            text=STEP_TAB_LABELS,
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=THEME["text_main"],
            anchor="w",
            justify="left",
            wraplength=420,
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 4))
        ctk.CTkLabel(
            panel,
            text=(
                "Выполняется после посадки границы участка и до оффсетов. Программа читает "
                "активный чертёж AutoCAD, находит участок и в зоне «участок + буфер» "
                "распознаёт заборы, строения, крыльца, ступени и дорожки — по кодам "
                "геодезических точек (POINT_LABELS) и по именам слоёв — и подписывает их "
                f"в слое {self._feature_output_layer()}."
            ),
            font=ctk.CTkFont(size=11),
            text_color=THEME["text_muted"],
            anchor="w",
            justify="left",
            wraplength=420,
        ).grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 10))

        house_card = ctk.CTkFrame(
            panel,
            fg_color=THEME["bg_field"],
            corner_radius=8,
            border_width=1,
            border_color=THEME["border_subtle"],
        )
        house_card.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 8))
        house_card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            house_card,
            text="Текст для основного дома (например: 2 STY. BRK. & FR.)",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=THEME["text_main"],
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 2))
        self.house_text_entry = ctk.CTkEntry(
            house_card,
            textvariable=self.house_text_var,
            height=30,
            fg_color=THEME["bg_editor"],
            border_color=THEME["border_card"],
            placeholder_text="например: 2 STY. BRK. & FR.",
        )
        self.house_text_entry.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 4))
        ctk.CTkLabel(
            house_card,
            text=(
                "Ставится по центру контура дома (BUILDING_OUTLINE). Вторая строка — через "
                "« / », например: 2 STY. FRAME / No. 87. Пусто — дом не подписывается."
            ),
            font=ctk.CTkFont(size=11),
            text_color=THEME["text_dim"],
            anchor="w",
            justify="left",
            wraplength=400,
        ).grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 8))

        self.skip_existing_labels_check = ctk.CTkCheckBox(
            panel,
            text="Не дублировать уже существующие на чертеже надписи",
            variable=self.skip_existing_labels_var,
            checkbox_width=18,
            checkbox_height=18,
            fg_color=THEME["btn_primary"],
            hover_color=THEME["btn_primary_hover"],
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=THEME["text_main"],
        )
        self.skip_existing_labels_check.grid(row=3, column=0, sticky="w", padx=16, pady=(2, 2))
        self.step2_dedup_hint = ctk.CTkLabel(
            panel,
            text=self._feature_dedup_hint(),
            font=ctk.CTkFont(size=11),
            text_color=THEME["text_dim"],
            anchor="w",
            justify="left",
            wraplength=420,
        )
        self.step2_dedup_hint.grid(row=4, column=0, sticky="ew", padx=16, pady=(0, 4))
        self.hide_field_notes_check = ctk.CTkCheckBox(
            panel,
            text="Выключить слои полевых заметок после подписания",
            variable=self.hide_field_notes_var,
            checkbox_width=18,
            checkbox_height=18,
            fg_color=THEME["btn_primary"],
            hover_color=THEME["btn_primary_hover"],
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=THEME["text_main"],
        )
        self.hide_field_notes_check.grid(row=5, column=0, sticky="w", padx=16, pady=(2, 2))
        self.step2_notes_hint = ctk.CTkLabel(
            panel,
            text=self._feature_notes_hint(),
            font=ctk.CTkFont(size=11),
            text_color=THEME["text_dim"],
            anchor="w",
            justify="left",
            wraplength=420,
        )
        self.step2_notes_hint.grid(row=6, column=0, sticky="ew", padx=16, pady=(0, 8))

        height_row = ctk.CTkFrame(panel, fg_color="transparent")
        height_row.grid(row=7, column=0, sticky="ew", padx=16, pady=(0, 10))
        height_row.grid_columnconfigure(2, weight=1)
        ctk.CTkLabel(
            height_row,
            text="Высота текста подписей, ft:",
            font=ctk.CTkFont(size=12),
            text_color=THEME["text_main"],
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.step2_height_entry = ctk.CTkEntry(
            height_row,
            textvariable=self.feature_text_height_var,
            width=90,
            height=28,
            fg_color=THEME["bg_editor"],
            border_color=THEME["border_card"],
            placeholder_text="авто",
        )
        self.step2_height_entry.grid(row=0, column=1, sticky="w")
        for event_name in ("<KeyRelease>", "<FocusOut>"):
            self.step2_height_entry.bind(
                event_name, lambda _event: self._refresh_step2_height_label()
            )
        self.step2_height_label = ctk.CTkLabel(
            height_row,
            text="",
            font=ctk.CTkFont(size=11),
            text_color=THEME["sky"],
            anchor="w",
            justify="left",
            wraplength=420,
        )
        self.step2_height_label.grid(
            row=1, column=0, columnspan=3, sticky="ew", pady=(4, 0)
        )

        self.feature_auto_button = ctk.CTkButton(
            panel,
            text="🔎  Автопоиск участка и подписание объектов",
            height=42,
            fg_color=THEME["btn_primary"],
            hover_color=THEME["btn_primary_hover"],
            font=ctk.CTkFont(size=13, weight="bold"),
            command=lambda: self._start_feature_labeling("auto"),
        )
        self.feature_auto_button.grid(row=8, column=0, sticky="ew", padx=16, pady=(0, 8))
        self.feature_pick_button = ctk.CTkButton(
            panel,
            text="🖱  Указать границу участка в AutoCAD и подписать объекты",
            height=38,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            font=ctk.CTkFont(size=12, weight="bold"),
            command=lambda: self._start_feature_labeling("pick"),
        )
        self.feature_pick_button.grid(row=9, column=0, sticky="ew", padx=16, pady=(0, 8))
        ctk.CTkLabel(
            panel,
            text=(
                "Автопоиск берёт замкнутый контур в слоях границы (PROPERTY_LINE, PL, "
                "M-TAX-LINE…). Если контуров несколько или граница не найдена — укажите её "
                "в AutoCAD: выберите полилинию или все стороны участка и нажмите Enter. "
                "Все подписи одного запуска отменяются в AutoCAD одним Ctrl+Z."
            ),
            font=ctk.CTkFont(size=11),
            text_color=THEME["text_dim"],
            anchor="w",
            justify="left",
            wraplength=420,
        ).grid(row=10, column=0, sticky="ew", padx=16, pady=(0, 8))

        # --- Правая карточка: счётчики и подробный отчёт ----------------------
        report_card = ctk.CTkFrame(
            page,
            fg_color=THEME["bg_card"],
            corner_radius=10,
            border_width=1,
            border_color=THEME["border_card"],
        )
        report_card.grid(row=0, column=1, sticky="nsew", padx=(8, 16), pady=(4, 12))
        report_card.grid_columnconfigure(0, weight=1)
        report_card.grid_rowconfigure(2, weight=1)

        stats = ctk.CTkFrame(report_card, fg_color="transparent")
        stats.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        self.step2_stat_vars: dict[str, Any] = {}
        stat_defs = (
            ("notes", "По заметкам", THEME["sky"]),
            ("codes", "По кодам точек", THEME["sky"]),
            ("layers", "По слоям", THEME["sky"]),
            ("labeled", "Подписано", THEME["ok"]),
            ("skipped", "Уже были подписаны", THEME["warn"]),
        )
        for column, (key, title, accent) in enumerate(stat_defs):
            stats.grid_columnconfigure(column, weight=1, uniform="step2_stats")
            card = ctk.CTkFrame(
                stats,
                fg_color=THEME["bg_field"],
                corner_radius=8,
                border_width=1,
                border_color=THEME["border_subtle"],
            )
            card.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 6, 0))
            card.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(
                card,
                text=title,
                font=ctk.CTkFont(size=11),
                text_color=THEME["text_muted"],
                anchor="w",
            ).grid(row=0, column=0, sticky="ew", padx=10, pady=(7, 0))
            var = ctk.StringVar(value="—")
            self.step2_stat_vars[key] = var
            ctk.CTkLabel(
                card,
                textvariable=var,
                font=ctk.CTkFont(size=20, weight="bold"),
                text_color=accent,
                anchor="w",
            ).grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 7))

        report_head = ctk.CTkFrame(report_card, fg_color="transparent")
        report_head.grid(row=1, column=0, sticky="ew", padx=12, pady=(4, 4))
        report_head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            report_head,
            text="Отчёт Шага 2 (дублируется в «🧾 Журнал» на вкладке Шага 1)",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=THEME["text_main"],
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            report_head,
            text="Копировать",
            width=96,
            height=26,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            font=ctk.CTkFont(size=11),
            command=self._copy_step2_report,
        ).grid(row=0, column=1, sticky="e", padx=(0, 6))
        ctk.CTkButton(
            report_head,
            text="Очистить",
            width=84,
            height=26,
            fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"],
            font=ctk.CTkFont(size=11),
            command=self._clear_step2_report,
        ).grid(row=0, column=2, sticky="e")

        self.step2_report_box = ctk.CTkTextbox(
            report_card,
            wrap="word",
            fg_color=THEME["bg_editor"],
            border_width=1,
            border_color=THEME["border_subtle"],
            corner_radius=8,
            text_color=THEME["text_main"],
            font=self._result_fonts["log"],
        )
        self.step2_report_box.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self._set_step2_report(STEP2_EMPTY_REPORT)
        self._refresh_step2_height_label()

    def _feature_output_layer(self) -> str:
        return str(feature_settings(self.config_data).get("label_output_layer") or "D-TOPO-TEXT")

    def _feature_dedup_hint(self) -> str:
        fcfg = feature_settings(self.config_data)
        layers = ", ".join(str(name) for name in fcfg.get("existing_label_layers") or [])
        radius = float(fcfg.get("duplicate_radius_ft") or 0.0)
        return (
            f"Перед подписанием читаются готовые подписи слоёв {layers}: если рядом с объектом "
            f"(в радиусе {radius:g} ft) уже стоит аналогичная надпись, дубликат не создаётся "
            "(повторный запуск ничего не удваивает)."
        )

    def _feature_notes_hint(self) -> str:
        fcfg = feature_settings(self.config_data)
        layers = ", ".join(str(name) for name in fcfg.get("field_note_layers") or [])
        return (
            f"Заметки слоёв {layers} — источник подписей: они переводятся в формулировки "
            "заказчика и в итоговый чертёж не идут. Слои только выключаются (LayerOn), "
            "объекты не удаляются."
        )

    def _feature_text_height_override(self) -> float | None:
        """Высота текста Шага 2 из поля вкладки: ``None`` — авто, ``ValueError`` — не число."""
        raw = str(self.feature_text_height_var.get() or "").strip().replace(",", ".")
        raw = raw.replace("'", "").strip()
        if not raw or raw.lower() in {"авто", "auto"}:
            return None
        value = float(raw)
        if not (0.05 <= value <= 20.0):
            raise ValueError(raw)
        return value

    def _refresh_step2_height_label(self) -> None:
        label = getattr(self, "step2_height_label", None)
        if label is None:
            return
        scale = self._current_scale()
        try:
            manual = self._feature_text_height_override()
        except ValueError:
            label.configure(
                text="Высота текста: введите число в футах от 0.05 до 20 (например 0.5) "
                     "или оставьте поле пустым.",
                text_color=THEME["warn"],
            )
            return
        try:
            auto = AcadBuilder(self.config_data).feature_text_height(scale)
        except Exception:  # noqa: BLE001 - некорректное значение в настройках
            return
        fcfg = feature_settings(self.config_data)
        if manual is not None:
            house = house_label_height(manual, fcfg)
            text = (
                f"Подписи будут высотой {manual:.3f} ft (задано вручную; авто по масштабу "
                f"1\" = {scale:g}' — {auto:.3f} ft). Описание дома — {house:.3f} ft."
            )
        else:
            house = house_label_height(auto, fcfg)
            text = (
                f"Поле пустое — высота {auto:.3f} ft по масштабу 1\" = {scale:g}' из шапки "
                f"окна (как у подписей границы); описание дома — {house:.3f} ft, стиль RDS. "
                "Нужен текст мельче или крупнее — впишите высоту в футах."
            )
        label.configure(text=text, text_color=THEME["sky"])

    def _select_step_tab(self, label: str) -> None:
        self.step_tab_var.set(label)
        self._switch_step_tab()

    def _switch_step_tab(self) -> None:
        """
        Переключает глобальные вкладки: Шаг 0 (документы) | рабочая зона Шага 1 |
        страница Шага 2.
        """
        tab = str(self.step_tab_var.get() or STEP_TAB_BOUNDARY)
        on_step2 = tab == STEP_TAB_LABELS
        on_step0 = tab == STEP_TAB_DEEDS
        if on_step2 or on_step0:
            self.main_wrap.grid_remove()
            # Кнопки отрисовки границы относятся только к Шагу 1.
            self.add_neighbor_button.grid_remove()
            self.draw_button.grid_remove()
        else:
            self.main_wrap.grid(row=2, column=0, sticky="nsew")
            self.add_neighbor_button.grid(row=0, column=2, padx=(8, 6))
            self.draw_button.grid(row=0, column=3, padx=(6, 16), pady=11, sticky="e")
        if on_step2:
            self.step2_page.grid(row=2, column=0, sticky="nsew")
            self._refresh_step2_height_label()
        else:
            self.step2_page.grid_remove()
        if on_step0:
            self.step0_page.grid(row=2, column=0, sticky="nsew")
        else:
            self.step0_page.grid_remove()
        for label, button in getattr(self, "step_tab_buttons", {}).items():
            active = label == tab
            button.configure(
                fg_color=THEME["btn_primary"] if active else THEME["bg_field"],
                hover_color=THEME["btn_primary_hover"] if active else THEME["btn_secondary_hover"],
                border_color=THEME["border_active"] if active else THEME["border_subtle"],
                text_color=THEME["text_main"] if active else THEME["text_muted"],
            )

    def _is_step2_tab(self) -> bool:
        var = getattr(self, "step_tab_var", None)
        return var is not None and str(var.get()) == STEP_TAB_LABELS

    def _set_step2_report(self, content: str) -> None:
        box = getattr(self, "step2_report_box", None)
        if box is None:
            return
        self._step2_report_text = content
        target = getattr(box, "_textbox", box)
        try:
            target.configure(state="normal")
        except Exception:  # noqa: BLE001
            pass
        box.delete("1.0", "end")
        box.insert("1.0", self._step2_report_view(content))
        try:
            target.configure(state="disabled")
        except Exception:  # noqa: BLE001
            pass

    def _step2_report_view(self, content: str) -> str:
        """Отчёт Шага 2 для показа в выбранном режиме; копирование берёт полный текст."""
        if content == STEP2_EMPTY_REPORT:
            return content
        return "\n".join(log_mode.filter_step2_lines(content.splitlines(), self._log_mode()))

    def _clear_step2_report(self) -> None:
        self.feature_label_result = None
        for var in getattr(self, "step2_stat_vars", {}).values():
            var.set("—")
        self._set_step2_report(STEP2_EMPTY_REPORT)

    def _copy_step2_report(self) -> None:
        text = str(getattr(self, "_step2_report_text", "") or "").strip()
        if not text or text == STEP2_EMPTY_REPORT:
            self._set_status("Отчёт Шага 2 пуст — нечего копировать.")
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"Не удалось скопировать отчёт: {exc}", error=True)
            return
        self._set_status("Отчёт Шага 2 скопирован в буфер обмена.")

    # ------------------------------------- Шаг 2: автоподписание объектов ---
    def _start_feature_labeling(self, mode: str) -> None:
        """Запускает Шаг 2: ``auto`` — автопоиск участка, ``pick`` — граница указывается в AutoCAD."""
        if self._busy or getattr(self, "_source_role_dialog", None) is not None:
            return
        mode = "pick" if mode == "pick" else "auto"
        house_text = normalize_house_text(self.house_text_var.get())
        skip_existing = bool(self.skip_existing_labels_var.get())
        hide_notes = bool(self.hide_field_notes_var.get())
        scale = self._current_scale()
        try:
            text_height = self._feature_text_height_override()
        except ValueError:
            self._refresh_step2_height_label()
            self._set_step2_report(
                "ШАГ 2 НЕ ВЫПОЛНЕН\n\nВысота текста подписей указана неверно: "
                f"«{self.feature_text_height_var.get()}». Введите число в футах от 0.05 до 20 "
                "(например 0.5) или оставьте поле пустым — тогда высота берётся по масштабу."
            )
            self._set_status("Шаг 2 не выполнен: неверная высота текста подписей", error=True)
            return
        cfg, _provider, _model, _key, _secret = self._build_runtime_config()
        self._busy = True
        self._set_controls_busy(True)
        self.progress.start()
        self._set_step2_report(
            "Выполняется…\n\n"
            + (
                "Перейдите в AutoCAD: выберите линии границы участка (замкнутую полилинию "
                "или все стороны) и нажмите Enter. Esc — отмена."
                if mode == "pick"
                else "Читаю активный чертёж AutoCAD и ищу участок по слоям границы."
            )
        )
        self._set_status(
            "Шаг 2: жду выбора границы участка в AutoCAD (выберите линии и нажмите Enter)…"
            if mode == "pick"
            else "Шаг 2: автопоиск участка и объектов в активном чертеже AutoCAD…"
        )
        self._start_background(
            self._feature_label_worker,
            args=(cfg, mode, house_text, skip_existing, scale, hide_notes, text_height),
        )

    def _feature_label_worker(
        self,
        cfg: dict[str, Any],
        mode: str,
        house_text: str,
        skip_existing: bool,
        scale: float,
        hide_field_notes: bool = False,
        text_height: float | None = None,
    ) -> None:
        post = self._worker_poster()
        result: FeatureLabelResult | None = None
        error = ""
        try:
            if sys.platform != "win32":
                raise AcadConnectionError(
                    "Подписание объектов возможно только в Windows с запущенным AutoCAD "
                    f"(текущая ОС: {sys.platform})"
                )
            builder = AcadBuilder(cfg)
            # Тот же замок, что и у отрисовки границы: два задания не пишут в чертёж сразу.
            with self._draw_lock:
                if not self._worker_is_current():
                    return
                result = builder.label_features(
                    mode=mode,
                    house_text=house_text,
                    skip_existing=skip_existing,
                    hide_field_notes=hide_field_notes,
                    scale=scale,
                    text_height=text_height,
                    progress=lambda message: post("progress", message),
                    should_continue=self._worker_is_current,
                )
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
        post("feature_labeled", (result, error, mode))
        post("done", None)

    def _handle_feature_label_result(
        self, result: FeatureLabelResult | None, error: str, mode: str
    ) -> None:
        """Показывает итог Шага 2: счётчики, подробный отчёт, строки журнала и статус."""
        if error or result is None:
            message = error or "неизвестная ошибка"
            hint = (
                "" if mode == "pick" else
                "\n\nЕсли участок не находится автоматически, нажмите "
                "«Указать границу участка в AutoCAD и подписать объекты»."
            )
            self._set_step2_report(f"ШАГ 2 НЕ ВЫПОЛНЕН\n\n{message}{hint}")
            for var in self.step2_stat_vars.values():
                var.set("—")
            self._set_status(f"Шаг 2 не выполнен: {message}", error=True)
            return
        self.feature_label_result = result
        self.step2_stat_vars["notes"].set(str(result.found_by_notes))
        self.step2_stat_vars["codes"].set(str(result.found_by_point_codes))
        self.step2_stat_vars["layers"].set(str(result.found_by_layers))
        self.step2_stat_vars["labeled"].set(str(result.created_labels))
        self.step2_stat_vars["skipped"].set(str(result.skipped_duplicates))
        report = result.report()
        self._set_step2_report(report)
        for line in report.splitlines():
            if line.strip():
                self._log_line(f"Шаг 2 · {line.strip()}", level=log_mode.step2_line_level(line))
        self._set_status(
            result.summary(),
            error=bool(result.failed) and result.created_labels == 0,
            warn=result.has_issues,
            key=True,
        )

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
        if n_keys > 0:
            self.keys_toggle_button.configure(
                text=f"⚙ Настройки   🔑 готовы ({n_keys})",
                border_color=THEME["ok"] if not self._keys_drawer_open else THEME["border_active"],
            )
        else:
            self.keys_toggle_button.configure(
                text="⚙ Настройки   🔑 Ввести API-ключи",
                border_color=THEME["warn"],
            )
        shell = getattr(self, "settings_drawer_shell", None)
        if shell is None:
            return
        if self._keys_drawer_open:
            shell.grid(row=0, column=0, rowspan=10, sticky="nsew")
            try:
                shell.lift()
            except Exception:  # noqa: BLE001
                pass
        else:
            shell.grid_remove()

    def _on_use_llm_toggled(self) -> None:
        self._refresh_consensus_rows()
        self._refresh_engine_card()

    RUN_MODE_LABELS = ["Локально", "Эконом", "ИИ"]

    def _on_run_mode_clicked(self, label: str) -> None:
        """Три режима: Локально (бесплатно), Эконом (локально, ИИ при проблемах), ИИ."""
        if label == "ИИ":
            self.economy_var.set(False)
            self.use_llm_var.set(True)
        else:
            self.economy_var.set(label == "Эконом")
            self.use_llm_var.set(False)
        self._on_use_llm_toggled()

    def _run_mode_label(self) -> str:
        if self.use_llm_var.get():
            return "ИИ"
        return "Эконом" if self.economy_var.get() else "Локально"

    # -------------------------------------------------- Spectrum UX helpers --
    def _result_logical_pixels(self, value: float) -> float:
        reverse = getattr(self.right_panel, "_reverse_widget_scaling", None)
        return float(reverse(float(value))) if callable(reverse) else float(value)

    def _set_result_panel_width(self, width: float, *, remember: bool = True) -> None:
        """Ограничивает ширину результата, оставляя место для центрального холста."""
        wanted = max(RESULT_PANEL_MIN_WIDTH, int(round(width)))
        available = int(self._result_logical_pixels(self.main_wrap.winfo_width()))
        left = int(self._result_logical_pixels(self.left_panel.winfo_width()))
        if available > 1 and left > 1:
            max_width = max(
                RESULT_PANEL_MIN_WIDTH,
                available - left - RESULT_SPLITTER_WIDTH - CENTER_PANEL_MIN_WIDTH,
            )
            actual = min(wanted, max_width)
        else:
            actual = wanted  # размеры окна ещё не рассчитаны
        if remember:
            self._desired_result_panel_width = actual
        if actual != self._result_panel_width:
            self._result_panel_width = actual
            self.right_panel.configure(width=actual)
        self._update_result_text_size(actual)

    def _on_workspace_resized(self, _event: Any = None) -> None:
        self._set_result_panel_width(self._desired_result_panel_width, remember=False)

    def _on_result_splitter_press(self, event: Any) -> None:
        self._splitter_drag = (int(event.x_root), self._result_panel_width)
        self.result_splitter_grip.focus_set()

    def _on_result_splitter_drag(self, event: Any) -> None:
        if self._splitter_drag is None:
            return
        start_x, start_width = self._splitter_drag
        delta = self._result_logical_pixels(int(event.x_root) - start_x)
        self._set_result_panel_width(start_width - delta)

    def _adjust_result_panel_width(self, delta: int) -> str:
        self._set_result_panel_width(self._result_panel_width + delta)
        return "break"

    def _reset_result_panel_width(self, _event: Any = None) -> str:
        self._set_result_panel_width(RESULT_PANEL_DEFAULT_WIDTH)
        return "break"

    def _result_wrap_length(self) -> int:
        return max(200, self._result_panel_width - 70)

    def _update_result_text_size(self, width: int) -> None:
        """При расширении панели увеличивает шрифты ведомости, карточек и журнала."""
        scale = min(1.65, 1.0 + max(0, width - RESULT_PANEL_DEFAULT_WIDTH) / 650)
        for name, font in self._result_fonts.items():
            size = int(round(self._result_font_base[name] * scale))
            if font.cget("size") != size:
                font.configure(size=size)
        for name in ("tie_info_label", "sides_title_label"):
            label = getattr(self, name, None)
            if label is not None:
                label.configure(wraplength=self._result_wrap_length())

        def update_wrapped_labels(widget: Any) -> None:
            for child in widget.winfo_children():
                if isinstance(child, ctk.CTkLabel) and child.cget("wraplength"):
                    child.configure(wraplength=self._result_wrap_length())
                update_wrapped_labels(child)

        for name in ("checks_scroll_frame", "neighbors_scroll_frame"):
            holder = getattr(self, name, None)
            if holder is not None:
                update_wrapped_labels(holder)

    def _on_source_mode_changed(self, _label: str = "") -> None:
        """Переключает левую колонку между режимами Текст / PDF / Фото."""
        mode = str(self.source_mode_var.get() or "📝 Текст")
        frames = {
            "📝 Текст": self.text_editor_card,
            "📄 PDF": self.pdf_mode_card,
            "📷 Фото": self.visual_bar,
        }
        for label, frame in frames.items():
            if label == mode:
                rowspan = 2
                frame.grid(row=2, column=0, rowspan=rowspan, sticky="nsew" if label == "📝 Текст" else "new", padx=16)
            else:
                frame.grid_remove()
        if mode == "📝 Текст":
            self.role_bar.grid()
        else:
            self.role_bar.grid_remove()
        self._refresh_pdf_selection_ui()
        self._refresh_visual_bar()
        self._refresh_parse_actions()

    def _current_source_mode(self) -> str:
        return str(self.source_mode_var.get() or "📝 Текст")

    def _refresh_parse_actions(self) -> None:
        """Основная кнопка показывает выбранный режим, вторая запускает альтернативу."""
        if not hasattr(self, "parse_button"):
            return
        if hasattr(self, "run_mode_seg"):
            self.run_mode_seg.set(self._run_mode_label())
        use_llm = bool(self.use_llm_var.get())
        if self.pdf_set and self._current_source_mode() == "📄 PDF":
            # В наборе способ разбора выбран у каждого файла отдельно.
            paid = sum(1 for item in self.pdf_set if item.use_llm)
            economy_files = sum(1 for item in self.pdf_set if item.economy and not item.use_llm)
            if paid:
                cost = f"ИИ: {paid} файл., платно"
            elif economy_files:
                cost = f"эконом: {economy_files} файл., ИИ только при проблемах"
            else:
                cost = "бесплатно"
            self.parse_button.configure(
                text=f"▶ Разобрать набор заново ({cost}) · Ctrl+Enter",
                fg_color=THEME["btn_primary"],
                hover_color=THEME["btn_primary_hover"],
            )
            self.regex_link_button.configure(
                text="☰ Изменить набор: роли, страницы, способ разбора",
                command=self._edit_pdf_set,
                state="disabled" if self._busy else "normal",
            )
            return
        is_batch = len(self.neighbor_pdf_paths) > 1 and self._current_source_mode() == "📄 PDF"
        economy_on = bool(self.economy_var.get())
        if use_llm:
            primary = "✨ ИИ для всех PDF · платно" if is_batch else "✨ ИИ (платно) · Ctrl+Enter"
            alternative = "⚡ Локальный разбор без ИИ · бесплатно"
        elif economy_on:
            primary = "⚖ Разобрать экономно · Ctrl+Enter"
            alternative = (
                "✨ Разобрать все PDF с ИИ · платно" if is_batch
                else "✨ Разобрать с ИИ · расход токенов API"
            )
        else:
            primary = "⚡ Разобрать бесплатно · Ctrl+Enter"
            alternative = (
                "✨ Разобрать все PDF с ИИ · платно" if is_batch
                else "✨ Разобрать с ИИ · расход токенов API"
            )
        self.parse_button.configure(
            text=primary,
            fg_color=THEME["btn_primary"],
            hover_color=THEME["btn_primary_hover"],
        )
        self.regex_link_button.configure(
            text=alternative,
            command=self._run_regex_only if use_llm else self._run_llm_only,
            state="disabled" if self._busy else "normal",
        )

    def _run_regex_only(self) -> None:
        """Явный выбор бесплатного офлайн-разбора."""
        if self._busy:
            return
        self.use_llm_var.set(False)
        self._refresh_consensus_rows()
        self._start(parse_only=True)

    def _run_llm_only(self) -> None:
        """Явный выбор ИИ-разбора вместо локального."""
        if self._busy:
            return
        self.use_llm_var.set(True)
        self._refresh_consensus_rows()
        self._start(parse_only=True)

    def _refresh_engine_card(self) -> None:
        """Обновляет мини-карточку активного ИИ-движка в левой колонке."""
        if not hasattr(self, "engine_model_var"):
            return
        try:
            model = self.current_model()
        except Exception:  # noqa: BLE001
            model = "—"
        passes = self.current_passes_count()
        provider = self._pid_to_label.get(self._active_provider_id, self._active_provider_id)
        extra = f" + ещё {passes - 1}" if passes > 1 else ""
        self.engine_model_var.set(f"{model}  ·  {provider}{extra}")
        badge = getattr(self, "ask_on_doubt_badge", None)
        if badge is not None:
            if self.ask_on_doubt_var.get():
                badge.grid()
            else:
                badge.grid_remove()

    def _scene_plans(self) -> list[tuple[str, DrawPlan]]:
        """Только актуальные фигуры, которые сейчас показаны на схеме."""
        plans: list[tuple[str, DrawPlan]] = []
        if self._preview_main_plan is not None:
            name = self._plan_source_labels.get(id(self._preview_main_plan), "текст")
            plans.append((f"Основной участок: {name}", self._preview_main_plan))
        for index, plan in enumerate(self._preview_neighbor_plans, 1):
            name = self._plan_source_labels.get(id(plan), "текст")
            plans.append((f"Сосед #{index}: {name}", plan))
        return plans

    def _scene_suspicion_reports(self) -> list[tuple[str, LotSuspicionReport]]:
        return [(name, plan.suspicion) for name, plan in self._scene_plans() if plan.suspicion is not None]

    def _plan_diagnostic_items(self, plan: DrawPlan) -> list[SuspicionItem]:
        """Собирает замечания плана и исходника, не меняя геометрический отчёт."""
        items = list(plan.suspicion.items) if plan.suspicion is not None else []
        outcome = self._plan_outcomes.get(id(plan))
        if outcome is not None:
            for index, suspicion in enumerate(outcome.typo_suspicions, 1):
                summary = suspicion.short_summary()
                items.append(SuspicionItem(
                    code=f"source_typo_{index}", severity="warn",
                    title=f"Возможная опечатка: {summary}",
                    detail=suspicion.reason_ru if suspicion.reason_ru not in summary else "",
                    segment_indices=[suspicion.segment_index] if suspicion.segment_index > 0 else [],
                ))
        for index, warning in enumerate(plan.warnings, 1):
            items.append(SuspicionItem(
                code=f"plan_warning_{index}", severity="warn",
                title=f"Расчёт: {warning}", detail="",
            ))
        return items

    def _scene_suspicion_severity(self) -> str:
        plans = self._scene_plans()
        items = [item for _name, plan in plans for item in self._plan_diagnostic_items(plan)]
        if any(item.severity == "error" for item in items):
            return "error"
        return "warn" if items or any(plan.suspicion is None for _name, plan in plans) else "ok"

    def _refresh_result_tab_indicators(self) -> None:
        buttons = getattr(self, "result_tab_buttons", {})
        selected = str(self.result_tab_var.get() or "📋 Ведомость")
        severity = {
            "🛡 Проверки": self._scene_suspicion_severity() if self._scene_plans() else None,
            "🏘 Соседи": (
                self._neighbor_report.overall_status
                if self._neighbor_report is not None and self._neighbor_report.pair_reports else None
            ),
        }
        for label, button in buttons.items():
            level = severity.get(label)
            active = label == selected
            if level == "error":
                color, hover, text_color, marker = (
                    ("#dc2626" if active else "#991b1b"), "#b91c1c", "#ffffff", "⛔"
                )
            elif level == "warn":
                color, hover, text_color, marker = (
                    ("#d97706" if active else "#92400e"), "#b45309", "#ffffff", "⚠"
                )
            elif level == "ok":
                color, hover, text_color, marker = (
                    ("#059669" if active else "#065f46"), "#047857", "#ffffff", "✓"
                )
            else:
                color = THEME["seg_sel"] if active else THEME["bg_field"]
                hover, text_color, marker = THEME["btn_secondary_hover"], THEME["text_main"], ""
            # Маркер заменяет значок вкладки, чтобы подпись не обрезалась на узкой панели.
            text = f"{marker} {label.split(' ', 1)[1]}" if marker else label
            button.configure(text=text, fg_color=color, hover_color=hover, text_color=text_color)

    def _select_result_tab(self, label: str) -> None:
        self.result_tab_var.set(label)
        self._switch_result_tab()

    def _switch_result_tab(self) -> None:
        """Переключает вкладки правой колонки «Результат»."""
        tab = str(self.result_tab_var.get() or "📋 Ведомость")
        frames = {
            "📋 Ведомость": self.sheet_tab_frame,
            "🛡 Проверки": self.checks_tab_frame,
            "🏘 Соседи": self.neighbors_tab_frame,
            "🧾 Журнал": self.log_tab_frame,
        }
        for label, frame in frames.items():
            if label == tab:
                frame.grid(row=0, column=0, sticky="nsew")
            else:
                frame.grid_remove()
        self._refresh_result_tab_indicators()
        if tab == "🧾 Журнал":
            self._refresh_log_view()

    def _clear_log(self) -> None:
        """Очищает журнал операций (вкладка «Журнал»)."""
        try:
            self._log = []
        except Exception:  # noqa: BLE001
            pass
        self._refresh_log_view()

    def _refresh_log_view(self) -> None:
        box = getattr(self, "log_box", None)
        if box is None:
            return
        target = getattr(box, "_textbox", box)
        try:
            target.configure(state="normal")
        except Exception:  # noqa: BLE001
            pass
        box.delete("1.0", "end")
        lines = log_mode.filter_entries(list(getattr(self, "_log", []))[-300:], self._log_mode())
        content = "\n".join(reversed(lines)) if lines else "Журнал пуст"
        box.insert("1.0", content)
        try:
            target.configure(state="disabled")
        except Exception:  # noqa: BLE001
            pass

    def _clear_neighbors_scene(self) -> None:
        """Убирает всех соседей со сцены предпросмотра и из вкладки «Соседи»."""
        active_was_neighbor = any(p is self._last_active_plan for p in self._preview_neighbor_plans)
        for plan in self._preview_neighbor_plans:
            self._plan_outcomes.pop(id(plan), None)
            self._plan_source_labels.pop(id(plan), None)
        self._preview_neighbor_plans.clear()
        self._neighbor_plan_sources.clear()
        self._pending_neighbor_files.clear()
        self._neighbor_text_pending = False
        if self.pdf_set:
            # Соседи убраны и из набора: в нём остаётся только наш участок.
            self.pdf_set = [item for item in self.pdf_set if item.is_main]
            self._pdf_set_scene_neighbors.clear()
            self._pdf_set_analysis = None
            if not self.pdf_set:
                self._visual_source_role = None
                self._pdf_set_report_ready = False
            self._sync_pdf_set_readiness()
            self._refresh_pdf_selection_ui()
            self._refresh_parse_actions()
        self._pending_neighbor_pdfs.update(self.neighbor_pdf_paths)
        self._neighbor_parsed_pages.clear()
        self._neighbor_report = None
        self._refresh_result_tab_indicators()
        main_plan = self._preview_main_plan
        if active_was_neighbor:
            self._last_active_plan = main_plan
            self._last_suspicion_report = main_plan.suspicion if main_plan is not None else None
            self._refresh_result_stats(main_plan)
            if main_plan is None:
                self._update_closure_badge("Closure Error: —", None)
            else:
                self._update_closure_badge(main_plan.closure.short_report(), main_plan.closure.distance)
        self._refresh_neighbor_verify_button()
        self._refresh_suspicion_badge()
        self._refresh_checks_list()
        self._refresh_neighbors_list()
        if main_plan is not None:
            self._set_inspector_text(self._build_inspector_report(main_plan))
        elif not self._last_active_plan:
            self._render_empty_inspector()
        self._redraw_preview_canvas()
        self._set_status("Соседние участки убраны со сцены.")

    def _copy_neighbor_act(self) -> None:
        """Копирует акт геодезической сверки с соседями в буфер обмена."""
        rep = self._neighbor_report
        if rep is None or not getattr(rep, "pair_reports", None):
            self._set_status("Акт сверки пуст — добавьте соседние участки.")
            return
        self._copy_text_report(rep.format_report(), "📋 Акт геодезической сверки скопирован в буфер обмена.")

    def _copy_text_report(self, text: str, ok_message: str) -> None:
        clip_fn = getattr(self, "clipboard_clear", None)
        append_fn = getattr(self, "clipboard_append", None)
        if callable(clip_fn) and callable(append_fn) and text.strip():
            clip_fn()
            append_fn(text)
        self._set_status(ok_message)

    def _refresh_neighbors_list(self) -> None:
        """Перерисовывает карточки соседних участков во вкладке «Соседи»."""
        holder = getattr(self, "neighbors_scroll_frame", None)
        if holder is None:
            return
        try:
            for child in holder.winfo_children():
                child.destroy()
        except Exception:  # noqa: BLE001
            pass
        holder.grid_columnconfigure(0, weight=1)
        plans = list(self._preview_neighbor_plans)
        if not plans:
            ctk.CTkLabel(
                holder,
                text=(
                    "Соседей нет.\nДобавьте соседний участок — он встанет стенка к стенке "
                    "и сразу проверится на щели, клинья и наложения."
                ),
                font=self._result_fonts["body"],
                text_color=THEME["text_dim"],
                justify="left",
                wraplength=self._result_wrap_length(),
            ).grid(row=0, column=0, sticky="ew", pady=14)
            return
        for idx, plan in enumerate(plans):
            pair = next(
                (pr for pr in getattr(self._neighbor_report, "pair_reports", [])
                 if pr.neighbor_index == idx + 1),
                None,
            )
            level = pair.status if pair is not None else None
            colors = {
                "error": ("#2b1010", THEME["err_border"], THEME["err"]),
                "warn": (THEME["warn_bg"], THEME["warn_border"], THEME["warn"]),
                "ok": (THEME["teal_soft_bg"], THEME["btn_neighbor"], THEME["ok"]),
            }
            background, border, title_color = colors.get(
                level, (THEME["bg_inset"], THEME["border_subtle"], THEME["text_muted"])
            )
            card = ctk.CTkFrame(
                holder, fg_color=background, corner_radius=8, border_width=1, border_color=border,
            )
            card.grid(row=idx, column=0, sticky="ew", pady=(0, 6))
            card.grid_columnconfigure(0, weight=1)
            headline = (
                "Сверка не выполнена: нужен основной участок."
                if pair is None and self._preview_main_plan is None
                else ("Сверка не выполнена." if pair is None else "")
            )
            if pair is not None:
                diagnoses = [diagnosis for edge in pair.shared_edges for diagnosis in edge.diagnoses]
                headline = next(iter(pair.findings or diagnoses), "")
                if not headline:
                    headline = "✓ Общие границы проверены" if level == "ok" else "⚠ См. акт сверки"
            title = self._plan_source_labels.get(id(plan), "текст")
            ctk.CTkLabel(
                card,
                text=f"Сосед #{idx + 1}: {title}",
                font=self._result_fonts["body_bold"],
                text_color=THEME["text_main"],
                anchor="w",
                wraplength=self._result_wrap_length(),
            ).grid(row=0, column=0, sticky="w", padx=10, pady=(7, 1))
            detail = headline or (
                f"{len(plan.boundary_lines)} отр. LINE · Closure {plan.closure.distance:.3f} ft"
            )
            ctk.CTkLabel(
                card,
                text=detail,
                font=self._result_fonts["small"],
                text_color=title_color,
                anchor="w",
                justify="left",
                wraplength=self._result_wrap_length(),
            ).grid(row=1, column=0, sticky="w", padx=10, pady=(0, 8))

    def _refresh_checks_list(self) -> None:
        """Показывает диагностику каждого актуального участка, а не только последнего."""
        self._refresh_result_tab_indicators()
        holder = getattr(self, "checks_scroll_frame", None)
        if holder is None:
            return
        for child in holder.winfo_children():
            child.destroy()
        holder.grid_columnconfigure(0, weight=1)
        plans = self._scene_plans()
        if not plans:
            ctk.CTkLabel(
                holder,
                text=(
                    "Проверок пока нет — разберите описание.\n"
                    "Замыкание, сумма углов, самопересечения и потерянные точки "
                    "проверяются автоматически."
                ),
                font=self._result_fonts["body"], text_color=THEME["text_dim"],
                justify="left", wraplength=self._result_wrap_length(),
            ).grid(row=0, column=0, sticky="ew", pady=14)
            return
        row = 0
        for name, plan in plans:
            ctk.CTkLabel(
                holder, text=name, font=self._result_fonts["body_bold"],
                text_color=THEME["text_main"], anchor="w", justify="left",
                wraplength=self._result_wrap_length(),
            ).grid(row=row, column=0, sticky="ew", pady=(8, 3))
            row += 1
            rep = plan.suspicion
            if rep is None or not rep.items:
                ctk.CTkLabel(
                    holder, text="⚠ Проверка формы недоступна." if rep is None else "✓ Замечаний к форме не найдено.",
                    font=self._result_fonts["small"], text_color=THEME["warn"] if rep is None else THEME["ok"],
                    anchor="w", justify="left", wraplength=self._result_wrap_length(),
                ).grid(row=row, column=0, sticky="ew", pady=(0, 6))
                row += 1
            for item in self._plan_diagnostic_items(plan):
                is_err = getattr(item, "severity", "warn") == "error"
                card = ctk.CTkFrame(
                    holder, fg_color="#2b1010" if is_err else THEME["warn_bg"],
                    corner_radius=8, border_width=1,
                    border_color=THEME["err_border"] if is_err else THEME["warn_border"],
                )
                card.grid(row=row, column=0, sticky="ew", pady=(0, 6))
                row += 1
                card.grid_columnconfigure(0, weight=1)
                icon = "⛔" if is_err else "⚠"
                ctk.CTkLabel(
                    card, text=f"{icon} {getattr(item, 'title', 'Подозрение')}",
                    font=self._result_fonts["body_bold"], text_color=THEME["text_main"],
                    anchor="w", justify="left", wraplength=self._result_wrap_length(),
                ).grid(row=0, column=0, sticky="w", padx=10, pady=(8, 2))
                detail = str(getattr(item, "detail", "") or getattr(item, "message", "") or "")
                if detail:
                    ctk.CTkLabel(
                        card, text=detail, font=self._result_fonts["small"],
                        text_color=THEME["text_muted"], anchor="w", justify="left",
                        wraplength=self._result_wrap_length(),
                    ).grid(row=1, column=0, sticky="w", padx=10, pady=(0, 8))

    def _refresh_stepper(self) -> None:
        """Обновляет степпер 1→4 в шапке по текущему состоянию сценария."""
        if not getattr(self, "_step_chips", None):
            return
        text_filled = bool(
            str(self.text_box.get("1.0", "end") or "").strip()
            or self.pdf_path or self.neighbor_pdf_paths or self.image_paths
        )
        has_plan = self._last_active_plan is not None or self._preview_main_plan is not None
        has_conflict = self.pending_conflict is not None
        suspicious = self._scene_suspicion_severity() != "ok"
        drawn = self.draw_result is not None and not has_conflict
        states = [
            "done" if text_filled else "active",
            "done" if has_plan else ("active" if text_filled or self._busy else "todo"),
            "done" if has_plan and not has_conflict and not suspicious else ("active" if has_plan else "todo"),
            "done" if drawn else ("active" if has_plan and not has_conflict else "todo"),
        ]
        colors = {
            "done": (THEME["ok"], THEME["ok_bg"], THEME["ok"]),
            "active": ("#ffffff", THEME["blue_soft_bg"], THEME["blue_soft_fg"]),
            "todo": (THEME["text_dim"], "transparent", THEME["text_dim"]),
        }
        for idx, state in enumerate(states):
            chip, lbl = self._step_chips[idx]
            dot = self._step_dots[idx]
            fg_label, chip_bg, dot_color = colors[state]
            chip.configure(fg_color=chip_bg if state != "todo" else "transparent")
            dot.configure(
                text="✓" if state == "done" else str(idx + 1),
                fg_color=THEME["btn_emerald"] if state == "done" else (THEME["btn_primary"] if state == "active" else THEME["border_subtle"]),
                text_color="#06281c" if state == "done" else ("#ffffff" if state == "active" else THEME["text_dim"]),
            )
            lbl.configure(text_color=fg_label)

    def _refresh_result_stats(self, plan: DrawPlan | None) -> None:
        """Обновляет KPI-карточки вкладки «Ведомость» и привязку POB."""
        if not hasattr(self, "stat_closure_var"):
            return
        if plan is None:
            self.stat_closure_var.set("—")
            self.stat_area_var.set("—")
            self.stat_perimeter_var.set("—")
            self.stat_text_height_var.set("—")
            self.tie_info_var.set("Фасад: — · Угол: —\nPOB: (0.00, 0.00) ft")
            self.closure_label.configure(text_color=THEME["text_muted"])
            self._refresh_center_badges(None, None)
            return
        closure_ft = plan.closure.distance
        closed_ok = closure_ft <= 0.01
        self.stat_closure_var.set(f"{closure_ft:.3f} ft")
        self.closure_label.configure(
            text_color=THEME["ok"] if closed_ok else (THEME["warn"] if closure_ft < 0.5 else THEME["err"])
        )
        self.stat_area_var.set(f"{plan.area_sqft:,.0f} sq ft".replace(",", " "))
        self.stat_perimeter_var.set(f"{plan.description.perimeter_ft:,.2f} ft".replace(",", " "))
        self.stat_text_height_var.set(f"{plan.text_height:.2f}")
        desc = plan.description
        tie_lines = [
            f"Фасад: {desc.main_street or '—'}",
            f"Угол: {desc.cross_street or '—'} · {desc.distance_to_corner_ft:,.2f} ft".replace(",", " "),
            f"POB (X, Y): {plan.pob[0]:+.2f}, {plan.pob[1]:+.2f} ft",
        ]
        self.tie_info_var.set("\n".join(tie_lines))
        self._refresh_center_badges(plan, closure_ft)

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
        if hasattr(self, "passes_switch_mini"):
            self.passes_switch_mini.set(label)
        self._on_passes_changed(label)

    def _on_passes_changed(self, _selected_label: str = "") -> None:
        self._refresh_consensus_rows()

    def _refresh_consensus_rows(self) -> None:
        self._refresh_parse_actions()
        use_llm = bool(self.use_llm_var.get())
        count = self.current_passes_count() if use_llm else 1
        state = "normal" if use_llm else "disabled"
        self.provider_switch.configure(state=state)
        self.model_combo.configure(state=state)
        self.passes_switch.configure(state=state)
        if hasattr(self, "passes_switch_mini"):
            self.passes_switch_mini.configure(state=state)

        if not use_llm:
            self.llm_policy_row.grid_remove()
            self.pass2_row.grid_remove()
            self.pass3_row.grid_remove()
            self.engine_model_label.grid_remove()
            self.regex_mode_badge.grid(row=1, column=1, sticky="w")
            self._refresh_engine_card()
            self._refresh_stepper()
            return

        if hasattr(self, "llm_policy_row"):
            self.llm_policy_row.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        self.regex_mode_badge.grid_remove()
        self.engine_model_label.grid()
        if count >= 2:
            self.pass2_row.grid(row=3, column=0, sticky="ew", pady=(0, 6))
        else:
            self.pass2_row.grid_remove()
        if count >= 3:
            self.pass3_row.grid(row=4, column=0, sticky="ew", pady=(0, 6))
        else:
            self.pass3_row.grid_remove()
        self._refresh_engine_card()
        self._refresh_stepper()

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
        self._persist_ai_preferences()

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
        self._persist_ai_preferences()

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
        self._persist_ai_preferences()

    def _on_model_selected(self, chosen_model: str) -> None:
        clean = str(chosen_model or "").strip()
        if clean:
            self.model_var.set(clean)
            self._selected_models[self._active_provider_id] = clean
            self._persist_ai_preferences()

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
        plan = self._last_active_plan or self._preview_main_plan
        if plan is not None:
            closure_ft = plan.closure.distance
            area_txt = f"{plan.area_sqft:,.0f} sq ft".replace(",", " ")
            perim_txt = f"{plan.description.perimeter_ft:,.2f} ft".replace(",", " ")
            header = (
                "ТЕХВЕДОМОСТЬ УЧАСТКА — CAD Survey Bridge v" + str(VERSION) + "\n"
                + "─" * 60
                + "\nНевязка: "
                + f"{closure_ft:.3f} ft"
                + ("  ✓ замкнутся" if closure_ft <= 0.01 else "  ⚠ требует проверки")
                + f"\nПлощадь: {area_txt}   ·   Периметр: {perim_txt}\n"
                + f"Точка POB (X, Y): {plan.pob[0]:+.2f}, {plan.pob[1]:+.2f} ft\n"
                + "─" * 60
                + "\n\n"
            )
            txt = header + txt
        clip_fn = getattr(self, "clipboard_clear", None)
        append_fn = getattr(self, "clipboard_append", None)
        if callable(clip_fn) and callable(append_fn):
            clip_fn()
            append_fn(txt)
        self._set_status("📋 Техведомость участка (с невязкой, площадью и POB) скопирована в буфер обмена.")
        return txt

    @staticmethod
    def _clipboard_control_event(event: Any) -> str | None:
        """Recognize physical Ctrl+C/Ctrl+V keys regardless of keyboard layout."""
        state = int(getattr(event, "state", 0) or 0)
        if not state & 0x4:  # Tk ControlMask
            return None
        keysym = str(getattr(event, "keysym", "") or "").casefold()
        keycode = int(getattr(event, "keycode", 0) or 0)
        char = str(getattr(event, "char", "") or "")
        # On Windows keycodes are virtual-key codes, independent of the active layout.
        # Tk may also report Ctrl+C/V as control characters or Cyrillic keysyms.
        physical_copy = sys.platform == "win32" and keycode == 67
        physical_paste = sys.platform == "win32" and keycode == 86
        if physical_copy or char == "\x03" or keysym in {"c", "cyrillic_es", "с"}:
            return SurveyApp._copy_text_event(event)
        if physical_paste or char == "\x16" or keysym in {"v", "cyrillic_em", "м"}:
            return SurveyApp._paste_text_event(event)
        return None

    @staticmethod
    def _paste_text_event(event: Any) -> str | None:
        """Paste clipboard text into focused Tk Entry/Text widgets, replacing selection."""
        widget = getattr(event, "widget", None)
        if widget is None:
            return None
        widget_class = str(widget.winfo_class())
        if widget_class not in {"Entry", "Text"}:
            return None
        try:
            state = str(widget.cget("state"))
            if state in {"disabled", "readonly"}:
                return "break"
            text = str(widget.clipboard_get())
            if widget_class == "Entry":
                if widget.selection_present():
                    widget.delete("sel.first", "sel.last")
                widget.insert("insert", text)
            else:
                selection = widget.tag_ranges("sel")
                if selection:
                    widget.delete(selection[0], selection[1])
                widget.insert("insert", text)
            return "break"
        except Exception:  # noqa: BLE001 - empty clipboard or a widget without text support
            return "break"

    @staticmethod
    def _cut_text_event(event: Any) -> str | None:
        """Cut selected text (copy then delete) from editable Tk Entry/Text widgets."""
        widget = getattr(event, "widget", None)
        if widget is None:
            return None
        widget_class = str(widget.winfo_class())
        try:
            state = str(widget.cget("state"))
            if state in {"disabled", "readonly"}:
                return "break"
        except Exception:  # noqa: BLE001 - widgets without a "state" option
            pass
        try:
            if widget_class == "Entry":
                if not widget.selection_present():
                    return None
                text = widget.selection_get()
                widget.delete("sel.first", "sel.last")
            elif widget_class == "Text":
                selection = widget.tag_ranges("sel")
                if not selection:
                    return None
                text = widget.get(selection[0], selection[1])
                widget.delete(selection[0], selection[1])
            else:
                return None
            widget.clipboard_clear()
            widget.clipboard_append(str(text))
            return "break"
        except Exception:  # noqa: BLE001 - no selection or clipboard unavailable
            return None

    @staticmethod
    def _copy_text_event(event: Any) -> str | None:
        """Copy selected text from editable or read-only Tk Entry/Text widgets."""
        widget = getattr(event, "widget", None)
        if widget is None:
            return None
        widget_class = str(widget.winfo_class())
        try:
            if widget_class == "Entry":
                if not widget.selection_present():
                    return None
                text = widget.selection_get()
            elif widget_class == "Text":
                selection = widget.tag_ranges("sel")
                if not selection:
                    return None
                text = widget.get(selection[0], selection[1])
            else:
                return None
            widget.clipboard_clear()
            widget.clipboard_append(str(text))
            return "break"
        except Exception:  # noqa: BLE001 - no selection or clipboard unavailable
            return None

    def _bind_text_clipboard_shortcuts(self) -> None:
        """Make paste/copy shortcuts consistent in all app Entry and Text controls."""
        bind_class = getattr(self, "bind_class", None)
        if not callable(bind_class):
            return
        for class_name in ("Entry", "Text"):
            # Exact Latin-key bindings cover the usual layout; the generic physical
            # Ctrl+KeyPress handler below also recognizes Cyrillic layout keysyms.
            for sequence in ("<Control-v>", "<Control-V>", "<Shift-Insert>"):
                bind_class(class_name, sequence, SurveyApp._paste_text_event)
            for sequence in ("<Control-c>", "<Control-C>", "<Control-Insert>"):
                bind_class(class_name, sequence, SurveyApp._copy_text_event)
            bind_class(class_name, "<Control-KeyPress>", SurveyApp._clipboard_control_event)

    def _show_text_context_menu(self, event: Any) -> str | None:
        """Right-click menu with Copy/Paste (and Cut where editable) for Entry/Text."""
        widget = getattr(event, "widget", None)
        if widget is None or tk is None:
            return None
        widget_class = str(widget.winfo_class())
        if widget_class not in {"Entry", "Text"}:
            return None
        try:
            state = str(widget.cget("state"))
        except Exception:  # noqa: BLE001 - widgets without a "state" option
            state = "normal"
        editable = state not in {"disabled", "readonly"}
        try:
            if widget_class == "Entry":
                has_selection = bool(widget.selection_present())
            else:
                has_selection = bool(widget.tag_ranges("sel"))
        except Exception:  # noqa: BLE001
            has_selection = False
        try:
            widget.focus_set()
        except Exception:  # noqa: BLE001
            pass
        menu = tk.Menu(widget, tearoff=0)
        menu.add_command(
            label="Копировать",
            command=lambda: SurveyApp._copy_text_event(event),
            state="normal" if has_selection else "disabled",
        )
        if editable:
            menu.add_command(
                label="Вырезать",
                command=lambda: SurveyApp._cut_text_event(event),
                state="normal" if has_selection else "disabled",
            )
            menu.add_command(
                label="Вставить",
                command=lambda: SurveyApp._paste_text_event(event),
            )
        try:
            x_root = int(getattr(event, "x_root", 0) or 0)
            y_root = int(getattr(event, "y_root", 0) or 0)
            menu.tk_popup(x_root, y_root)
        finally:
            release = getattr(menu, "grab_release", None)
            if callable(release):
                release()
        return "break"

    def _bind_text_context_menu(self) -> None:
        """Enable a right-click Copy/Paste context menu on all Entry and Text controls."""
        if tk is None:
            return
        bind_class = getattr(self, "bind_class", None)
        if not callable(bind_class):
            return
        for class_name in ("Entry", "Text"):
            for sequence in ("<Button-3>", "<Control-Button-1>"):
                bind_class(class_name, sequence, self._show_text_context_menu)

    def _bind_model_preference_autosave(self) -> None:
        """Persist editable model names when a combobox loses focus or Enter is pressed."""
        for slot, combo, variable in (
            (1, self.model_combo, self.model_var),
            (2, self.pass2_model_combo, self.pass2_model_var),
            (3, self.pass3_model_combo, self.pass3_model_var),
        ):
            entry = getattr(combo, "_entry", None)
            binder = getattr(entry, "bind", None)
            if not callable(binder):
                continue
            callback = lambda event, slot=slot, variable=variable: self._commit_model_entry(
                slot, variable, event,
            )
            binder("<FocusOut>", callback, add="+")
            binder("<Return>", callback, add="+")

    def _commit_model_entry(self, slot: int, variable: Any, event: Any = None) -> None:
        widget = getattr(event, "widget", None)
        getter = getattr(widget, "get", None)
        value = str(getter() if callable(getter) else variable.get() or "").strip()
        if value:
            variable.set(value)
            if slot == 1:
                self._selected_models[self.current_provider_id()] = value
            self._persist_ai_preferences()

    def _persist_ai_preferences(self, *, report_error: bool = True) -> bool:
        """Save provider/model selections without touching API keys or other settings."""
        try:
            cfg = copy.deepcopy(self.config_data)
            api = cfg.setdefault("api", {})
            active_pid = self.current_provider_id()
            active_model = self.current_model()
            if not active_model:
                return True
            self._selected_models[active_pid] = active_model
            api["provider"] = active_pid
            api["model"] = active_model
            providers = api.setdefault("providers", {})
            for pid, selected in self._selected_models.items():
                model = str(selected or "").strip()
                if not model:
                    continue
                provider_cfg = providers.setdefault(pid, {})
                provider_cfg["selected_model"] = model
                models = [str(value) for value in (provider_cfg.get("models") or [])]
                if model not in models:
                    provider_cfg["models"] = [model, *models]

            pass2_pid = self.current_pass2_provider_id()
            pass2_model = self.current_pass2_model()
            pass3_pid = self.current_pass3_provider_id()
            pass3_model = self.current_pass3_model()
            for pid, model in ((pass2_pid, pass2_model), (pass3_pid, pass3_model)):
                provider_cfg = providers.setdefault(pid, {})
                models = [str(value) for value in (provider_cfg.get("models") or [])]
                if model and model not in models:
                    provider_cfg["models"] = [model, *models]
            api["consensus"] = {
                "passes": self.current_passes_count(),
                "pass2_provider": pass2_pid,
                "pass2_model": pass2_model,
                "pass3_provider": pass3_pid,
                "pass3_model": pass3_model,
            }
            save_config(cfg)
            self.config_data = cfg
            return True
        except Exception as exc:  # noqa: BLE001 - retain UI operation if disk is read-only
            if report_error:
                self._set_status(
                    f"Не удалось автоматически сохранить выбор провайдера/модели: {exc}. "
                    "Откройте настройки и нажмите «Сохранить».",
                    warn=True,
                )
            return False

    def _on_pass_model_selected(self, slot: int, chosen_model: str) -> None:
        value = str(chosen_model or "").strip()
        if value:
            (self.pass2_model_var if slot == 2 else self.pass3_model_var).set(value)
            self._persist_ai_preferences()

    def _bind_update_source_autosave(self) -> None:
        """Persist the GitHub update source as soon as the field loses focus or Enter is hit."""
        entry = getattr(self, "update_source_entry", None)
        binder = getattr(entry, "bind", None)
        if not callable(binder):
            return
        binder("<FocusOut>", self._persist_update_source, add="+")
        binder("<Return>", self._persist_update_source, add="+")

    def _persist_update_source(self, event: Any = None) -> None:
        try:
            cfg = copy.deepcopy(self.config_data)
            cfg.setdefault("update", {})["source_url"] = str(self.update_source_var.get() or "").strip()
            save_config(cfg)
            self.config_data = cfg
        except Exception as exc:  # noqa: BLE001 - retain UI operation if disk is read-only
            self._set_status(f"Не удалось сохранить ссылку обновления: {exc}", warn=True)

    def _start_update_version(self) -> None:
        """Download the configured GitHub archive and forcibly unpack it over this install."""
        if self._update_running:
            self._set_status("Обновление уже выполняется, дождитесь завершения.", warn=True)
            return
        source = str(self.update_source_var.get() or "").strip()
        if not source:
            self._set_status(
                "Укажите ссылку на GitHub (репозиторий, ветку или .zip) в настройках перед обновлением.",
                error=True,
            )
            return
        self._persist_update_source()
        confirm = getattr(self, "_confirm_update_version", None)
        if callable(confirm) and not confirm(source):
            return
        app_dir = self._update_dir()
        self._update_running = True
        self.update_version_button.configure(state="disabled", text="⏳ Обновляю…")
        self._set_rollback_enabled(False)
        self.update_status_label.configure(
            text=f"Скачиваю обновление из {source}…", text_color=THEME["text_dim"],
        )
        self._set_status(f"Обновление: скачиваю архив из {source}…")
        self._show_update_stage("download", 0.0, "подключаюсь к GitHub…")

        def progress(message: str) -> None:
            self._post("update_progress", str(message))

        def stage(name: str, fraction: float | None, detail: str) -> None:
            self._post("update_stage", (name, fraction, detail))

        def worker() -> None:
            try:
                result = app_updater.apply_update(source, app_dir, progress=progress, on_stage=stage)
            except Exception as exc:  # noqa: BLE001 - network/zip/filesystem failures
                self._post("update_done", ("error", f"{type(exc).__name__}: {exc}"))
                return
            self._post("update_done", ("ok", result))

        threading.Thread(target=worker, daemon=True).start()

    def _attach_child_window(self, win: Any) -> None:
        """
        Окно-отчёт (сверка с соседями, итог набора, журнал и т. п.) принадлежит главному окну:
        transient держит его поверх основного окна, lift поднимает при открытии.
        """
        try:
            win.transient(self)
        except Exception:  # noqa: BLE001 - платформа не поддерживает transient — окно всё равно откроется
            pass
        win.lift()

    def _show_update_stage(self, name: str, fraction: float | None, detail: str) -> None:
        """Полоса и подпись: «Скачивание: 42% — 1.0 из 2.4 МБ», «Резервная копия…», «Установка…»."""
        titles = {"download": "Скачивание", "backup": "Резервная копия", "install": "Установка"}
        title = titles.get(name, name)
        bar = self.update_progress_bar
        bar.grid()
        if fraction is None:
            text = f"{title}: {detail}"
        else:
            bar.set(max(0.0, min(1.0, float(fraction))))
            text = f"{title}: {round(max(0.0, min(1.0, float(fraction))) * 100)}% — {detail}"
        self.update_status_label.configure(text=text, text_color=THEME["text_dim"])

    def _confirm_update_version(self, source: str) -> bool:
        """Ask for confirmation before overwriting the running installation."""
        from tkinter import messagebox

        return bool(
            messagebox.askyesno(
                "Обновить версию",
                "Программа скачает архив по указанной ссылке GitHub и принудительно "
                f"заменит файлы в текущей папке программы:\n{source}\n\n"
                "Продолжить?",
            )
        )

    def _finish_update_version(self, status: str, payload: Any) -> None:
        self._update_running = False
        self.update_version_button.configure(state="normal", text="🔄 Обновить версию")
        if status == "ok":
            self.update_progress_bar.set(1.0)
        else:
            self.update_progress_bar.set(0)
            self.update_progress_bar.grid_remove()
        if status == "ok":
            result: app_updater.UpdateResult = payload
            kept_note = (
                f" Локальные настройки сохранены: {', '.join(result.protected_skipped)}."
                if result.protected_skipped
                else ""
            )
            self.update_status_label.configure(
                text=(
                    f"Обновлено файлов: {result.files_written}.{kept_note} "
                    "Перезапустите программу, чтобы применить изменения."
                ),
                text_color=THEME["ok"],
            )
            self._set_status(
                f"Обновление завершено: заменено файлов — {result.files_written}.{kept_note} "
                "Перезапустите программу.",
            )
            self._refresh_rollback_state()
            self._offer_restart(
                "Обновление завершено",
                f"Обновлено файлов: {result.files_written}.{kept_note}\n\n"
                "Перезапустить программу сейчас?",
                base_text=str(self.update_status_label.cget("text")),
            )
        else:
            self._refresh_rollback_state()
            self.update_status_label.configure(
                text=f"Обновление не удалось: {payload}", text_color=THEME["err"],
            )
            self._set_status(f"Обновление не удалось: {payload}", error=True)

    def _update_dir(self) -> Path:
        """Папка программы: обновление и откат работают только с ней."""
        return Path(__file__).resolve().parent

    def _offer_restart(self, title: str, question: str, *, base_text: str) -> None:
        """
        Спрашивает о перезапуске и, если пользователь согласен, запускает run.bat.

        Окно закрывается только после успешного запуска нового экземпляра. Иначе
        оно остаётся открытым, а причина и инструкция пишутся рядом с итогом.
        """
        from tkinter import messagebox

        try:
            was_topmost = bool(self.attributes("-topmost"))
            if was_topmost:
                self.attributes("-topmost", False)
        except Exception:  # noqa: BLE001
            was_topmost = False
        asked = messagebox.askyesno(title, question)
        if asked:
            problem = self._relaunch_after_update()
            if problem:
                text = (
                    f"{base_text} Не удалось перезапустить автоматически ({problem}): "
                    "закройте программу и запустите run.bat из её папки вручную."
                )
                self.update_status_label.configure(text=text, text_color=THEME["err"])
                self._set_status(text, error=True)
            else:
                self._exit_after_relaunch()
                return
        if was_topmost:
            try:
                self.attributes("-topmost", True)
            except Exception:  # noqa: BLE001
                pass

    def _exit_after_relaunch(self) -> None:
        """
        Завершает процесс старой версии после того, как новый уже запущен.

        Окно run.bat старой версии ждёт, пока завершится python.exe. Одного destroy()
        недостаточно: если процесс задерживается (фоновые потоки, очистка интерпретатора),
        cmd висит. Поэтому после destroy() выходим сразу с кодом 0 — run.bat тогда
        дойдёт до конца и закроет своё окно, а не предложит откат.
        """
        try:
            self.destroy()
        except Exception:  # noqa: BLE001 - выходим в любом случае
            pass
        self._exit_process(0)

    def _exit_process(self, code: int) -> None:
        """Немедленный выход процесса (отдельным методом, чтобы тесты не завершали pytest)."""
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:  # noqa: BLE001
                pass
        os._exit(code)

    def _set_rollback_enabled(self, enabled: bool) -> None:
        button = getattr(self, "rollback_button", None)
        if button is not None:
            button.configure(state="normal" if enabled else "disabled")

    def _refresh_rollback_state(self) -> None:
        """Показывает, на какую версию откатимся, или что резервных копий нет."""
        label = getattr(self, "rollback_status_label", None)
        if label is None:
            return
        try:
            backups = app_updater.list_backups(self._update_dir())
        except OSError:
            backups = []
        if not backups:
            label.configure(
                text="Резервных копий нет: откат станет доступен после первого обновления.",
                text_color=THEME["text_dim"],
            )
            self._set_rollback_enabled(False)
            return
        latest = backups[0]
        when = latest.created
        try:
            when = _dt.datetime.fromisoformat(latest.created).strftime("%d.%m.%Y %H:%M")
        except ValueError:
            pass
        label.configure(
            text=(
                f"Резерв: версия {latest.version or 'неизвестна'} (сохранена {when}). "
                "Откат вернёт её файлы; настройки и ключи не меняются."
            ),
            text_color=THEME["text_dim"],
        )
        self._set_rollback_enabled(not self._update_running)

    def _start_rollback(self) -> None:
        """Возвращает файлы прежней версии из самой свежей резервной копии."""
        if self._update_running:
            self._set_status("Дождитесь окончания обновления, затем откатывайте версию.", warn=True)
            return
        from tkinter import messagebox

        latest = app_updater.list_backups(self._update_dir())
        if not latest:
            self._refresh_rollback_state()
            return
        version = latest[0].version or "неизвестна"
        if not messagebox.askyesno(
            "Откат версии",
            f"Вернуть файлы версии {version} из резервной копии?\n\n"
            "Текущие файлы программы будут заменены. config.json, .env и локальные "
            "файлы не меняются.\n\nПродолжить?",
        ):
            return
        try:
            result = app_updater.rollback_latest(self._update_dir())
        except (app_updater.UpdateError, OSError) as exc:
            self._refresh_rollback_state()
            self._set_status(f"Откат не выполнен: {exc}", error=True)
            self.update_status_label.configure(text=f"Откат не выполнен: {exc}", text_color=THEME["err"])
            return
        text = (
            f"Откат выполнен: возвращена версия {result.version or 'неизвестна'} "
            f"(файлов восстановлено — {result.files_restored}, новых удалено — {result.files_removed})."
        )
        self.update_status_label.configure(text=text, text_color=THEME["ok"])
        self._set_status(text)
        self._refresh_rollback_state()
        self._offer_restart(
            "Откат выполнен",
            f"После перезапуска программа продолжит работу на версии "
            f"{result.version or 'неизвестна'}.\n\nПерезапустить программу сейчас?",
            base_text=text,
        )

    def _relaunch_after_update(self) -> str:
        """
        Запускает run.bat из папки программы, чтобы поднять обновлённую версию.

        Папка берётся из пути main.py (run.bat лежит рядом с ним), а не из текущей
        директории. Возвращает текст причины, если запуск не удался, иначе пустую строку.
        """
        import subprocess

        run_bat = Path(__file__).resolve().parent / "run.bat"
        if not run_bat.is_file():
            return f"не найден {run_bat.name} в папке программы"
        if sys.platform != "win32":
            return "автоматический перезапуск работает в Windows"
        try:
            subprocess.Popen(
                ["cmd", "/c", "start", "", str(run_bat)],
                cwd=str(run_bat.parent),
            )
        except OSError as exc:
            return f"{type(exc).__name__}: {exc}"
        return ""

    def _bind_studio_shortcuts(self) -> None:
        """Привязывает студийные горячие клавиши для быстрой повседневной работы."""
        bind_fn = getattr(self, "bind_all", None) or getattr(self, "bind", None)
        if not callable(bind_fn):
            return

        def _guard(action: Any, *, step1_only: bool = False) -> Any:
            def _handler(_event: Any = None) -> str | None:
                if getattr(self, "_busy", False) or getattr(self, "_source_role_dialog", None) is not None:
                    return None
                # Клавиши разбора и отрисовки границы не срабатывают на вкладках
                # Шага 2 и Шага 0: там другой работой занят экран.
                if step1_only and (self._is_step2_tab() or self._is_step0_tab()):
                    return None
                action()
                return "break"

            return _handler

        bind_fn("<Control-Return>", _guard(lambda: self._start(parse_only=True), step1_only=True))
        bind_fn("<F5>", _guard(lambda: self._start(parse_only=False), step1_only=True))
        bind_fn("<Control-o>", _guard(self._load_pdf, step1_only=True))
        bind_fn("<Control-O>", _guard(self._load_pdf, step1_only=True))
        bind_fn("<Control-i>", _guard(self._load_images, step1_only=True))
        bind_fn("<Control-I>", _guard(self._load_images, step1_only=True))
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
            win.attributes("-topmost", bool(self.topmost_var.get()))
            self._attach_child_window(win)
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
            "  F5              — 📐 Начертить готовую схему в активном чертеже AutoCAD\n"
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

    def _refresh_text_counter(self) -> None:
        lbl = getattr(self, "text_counter_label", None)
        if lbl is None:
            return
        n = len(str(self.text_box.get("1.0", "end") or "").strip())
        lbl.configure(text=f"{n} симв.")
        self._refresh_stepper()

    def _render_empty_inspector(self) -> None:
        hint = (
            "Ведомость участка пока пуста.\n\n"
            "1. Загрузите PDF (Ctrl+O), фото 80-х (Ctrl+I) или выберите пример слева.\n"
            "2. Нажмите «🔍 Проверить и показать схему» (Ctrl+Enter) для проверки\n"
            "   геометрии без запуска AutoCAD. Затем F5 начертит готовую схему.\n"
            "3. На холсте выше работают зум колесом мыши и панорама перетаскиванием."
        )
        self._set_inspector_text(hint)
        self._redraw_preview_canvas()

    def _slide_overlapping_neighbours(self) -> None:
        """
        Развод наложений в предпросмотре: сосед, лежащий поверх нашего участка, съезжает наружу
        до появления общей межи (алгоритм — neighbour_slide.py). Зазоры не трогаем.
        Сосед переносится жёстко: форма, длины и румбы не меняются, меняется только положение на
        схеме; описание (legal description) не меняется.
        """
        main = self._preview_main_plan
        neighbours = list(self._preview_neighbor_plans)
        if main is None or not neighbours:
            self._set_status("Развод: на схеме нет нашего участка или соседей", warn=True)
            return
        streets = [
            (ln.start, ln.end)
            for ln in main.lines
            if str(ln.kind).startswith(("main-street-row", "cross-street-row"))
            and not str(ln.kind).endswith("-tie")  # линия привязки от угла до POB — не улица
        ]
        moves: list[tuple[DrawPlan, float, float]] = []
        moved_count = 0
        for idx, plan in enumerate(neighbours, 1):
            others = [other.vertices for other in neighbours if other is not plan]
            res = slide_off_overlap(
                main.vertices, plan.vertices, street_lines=streets, others=others,
            )
            if res.status == "moved":
                translate_plan_in_place(plan, res.dx, res.dy)
                moves.append((plan, res.dx, res.dy))
                moved_count += 1
                self._log_line(
                    f"Развод: СОСЕД #{idx} сдвинут на {res.distance_ft:.2f} ft "
                    f"(направление {res.direction_deg:.1f}°, "
                    f"{_SLIDE_METHOD_TEXT.get(res.method, res.method)}), "
                    f"общая межа {res.shared_ft:.2f} ft; клин (расхождение сторон на дальнем конце) "
                    f"{res.wedge_ft:.2f} ft — примечание; было наложение {res.overlap_before_sqft:,.1f} кв. ft",
                )
            elif res.status == "no_overlap":
                self._log_line(f"Развод: СОСЕД #{idx} — наложения нет, не трогаем (зазор не сдвигаем)")
            else:
                self._log_line(f"Развод: СОСЕД #{idx} не сдвинут — {res.note}", level=log_mode.LEVEL_WARN)
        self._slide_undo = moves
        self._redraw_preview_canvas()
        if moved_count:
            self._set_status(f"Развод наложений: сдвинуто соседей — {moved_count}")
        else:
            self._set_status("Развод наложений: сдвигать нечего (см. журнал)", warn=True)

    def _undo_slide_neighbours(self) -> None:
        """Возвращает соседей на места, с которых их сдвинул последний «Развести наложения»."""
        moves = list(getattr(self, "_slide_undo", []) or [])
        if not moves:
            self._set_status("Отменять нечего: последний развод не сдвигал соседей", warn=True)
            return
        for plan, dx, dy in reversed(moves):
            translate_plan_in_place(plan, -dx, -dy)
        self._slide_undo = []
        self._redraw_preview_canvas()
        self._log_line(f"Развод отменён: соседей возвращено на места — {len(moves)}")
        self._set_status("Развод отменён")

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
            if self._current_source_mode() == "📷 Фото":
                self.visual_bar.grid_remove()
                self.photo_empty_card.grid(row=2, column=0, rowspan=2, sticky="new", padx=16)
            else:
                self.photo_empty_card.grid_remove()
            return
        self.photo_empty_card.grid_remove()
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
        self._refresh_auto_orient_warning()
        if self._current_source_mode() == "📷 Фото":
            self.visual_bar.grid(row=2, column=0, rowspan=2, sticky="new", padx=16)

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
        else:
            self._neighbor_report = verify_with_neighbors(
                self._preview_main_plan, self._preview_neighbor_plans
            )
        self._refresh_neighbor_verify_button()
        self._refresh_result_tab_indicators()
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
        plans = self._scene_plans()
        if not plans:
            self.suspicion_badge_button.configure(
                text="🔎 Форма: —", fg_color=THEME["btn_secondary"],
                hover_color=THEME["btn_secondary_hover"],
            )
            return
        count = sum(len(self._plan_diagnostic_items(plan)) for _name, plan in plans)
        level = self._scene_suspicion_severity()
        if level == "error":
            color, hover, icon = "#dc2626", "#b91c1c", "⛔"
        elif level == "warn":
            color, hover, icon = "#d97706", "#b45309", "⚠"
        else:
            color, hover, icon = "#059669", "#047857", "✓"
        self.suspicion_badge_button.configure(
            text=f"🔎 {icon} Фигур: {len(plans)} · замечаний: {count}",
            fg_color=color, hover_color=hover,
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
        """Подробная копируемая диагностика всех фигур на текущей схеме."""
        plans = self._scene_plans()
        if not plans:
            self._set_status(
                "Сначала разберите участок, чтобы проверить его геометрию на подозрительность."
            )
            return
        win = ctk.CTkToplevel(self)
        self._session_windows.append(win)
        win.title("Диагностика участков — Title Survey AI")
        win.geometry("860x560")
        win.configure(fg_color=THEME["bg_root"])
        win.attributes("-topmost", bool(self.topmost_var.get()))
        self._attach_child_window(win)
        win.grid_columnconfigure(0, weight=1)
        win.grid_rowconfigure(1, weight=1)

        level = self._scene_suspicion_severity()
        head_color = THEME["err"] if level == "error" else (THEME["warn"] if level == "warn" else THEME["ok"])
        item_count = sum(len(self._plan_diagnostic_items(plan)) for _name, plan in plans)
        headline = f"Фигур на схеме: {len(plans)} · замечаний: {item_count}"
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
            text=headline,
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
        sections = []
        for name, plan in plans:
            rep = plan.suspicion
            lines = [f"=== {name} ===", rep.format_report() if rep else "Проверка формы недоступна."]
            extras = self._plan_diagnostic_items(plan)[len(rep.items) if rep else 0:]
            if extras:
                lines.append("Дополнительные замечания по исходнику и расчёту:")
                for item in extras:
                    lines.append(f"  ⚠ {item.title}" + (f" — {item.detail}" if item.detail else ""))
            sections.append("\n".join(lines))
        full_txt = "\n\n".join(sections)
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
        self._slide_undo = []
        self._pdf_set_main_plan = None
        self._pdf_set_scene_neighbors.clear()
        self._pdf_set_main_pending = False
        self._pdf_set_report_ready = False
        self._pdf_set_analysis = None
        for item in self.pdf_set:
            item.on_scene = False
        self._preview_neighbor_plans.clear()
        self._neighbor_plan_sources.clear()
        self._pending_neighbor_files.clear()
        self._plan_source_labels.clear()
        self._plan_outcomes.clear()
        self._input_incomplete = False
        self._main_analysis_pending = False
        self._neighbor_text_pending = False
        self._ready_source_signature = None
        self._running_source_signature = None
        self._running_source_path = None
        self._running_source_label = None
        self._running_batch_attached = False
        self._open_neighbor_report_after_parse = False
        self._running_pdf_pages = None
        self._neighbor_parsed_pages.clear()
        self._last_active_plan = None
        self._neighbor_report = None
        self._last_suspicion_report = None
        self._refresh_result_tab_indicators()
        self._reset_viewport_state()
        self._refresh_neighbor_verify_button()
        self._refresh_suspicion_badge()
        self._refresh_result_stats(None)
        self._refresh_checks_list()
        self._refresh_neighbors_list()
        self._refresh_stepper()
        self._render_empty_inspector()

    def _record_plan_for_preview(
        self, plan: DrawPlan, *, warnings: list[str] | None = None,
        source: Path | None = None, source_label: str | None = None,
        outcome: ParseOutcome | None = None,
    ) -> None:
        self._last_active_plan = plan
        self._last_suspicion_report = plan.suspicion
        if plan.is_neighbor:
            # Разные файлы могут иметь одинаковый POB; заменяем только тот же файл.
            previous = self._neighbor_plan_sources.get(source) if source is not None else None
            replace_index = None
            for idx, existing in enumerate(self._preview_neighbor_plans):
                if previous is existing or (
                    source is None
                    and not any(stored is existing for stored in self._neighbor_plan_sources.values())
                    and math.hypot(existing.pob[0] - plan.pob[0], existing.pob[1] - plan.pob[1]) < 0.25
                ):
                    replace_index = idx
                    break
            if replace_index is None:
                self._preview_neighbor_plans.append(plan)
            else:
                old_plan = self._preview_neighbor_plans[replace_index]
                self._preview_neighbor_plans[replace_index] = plan
                if old_plan is not plan:
                    self._plan_outcomes.pop(id(old_plan), None)
                    self._plan_source_labels.pop(id(old_plan), None)
                for key, value in list(self._neighbor_plan_sources.items()):
                    if value is old_plan:
                        del self._neighbor_plan_sources[key]
            if source is not None:
                self._neighbor_plan_sources[source] = plan
        else:
            if self._preview_main_plan is not None and self._preview_main_plan is not plan:
                self._plan_outcomes.pop(id(self._preview_main_plan), None)
                self._plan_source_labels.pop(id(self._preview_main_plan), None)
            self._preview_main_plan = plan
        self._plan_source_labels[id(plan)] = (
            source_label or (source.name if source else self._plan_source_labels.get(id(plan), "текст"))
        )
        if outcome is not None:
            self._plan_outcomes[id(plan)] = outcome

        self._recompute_neighbor_verification()
        self._refresh_suspicion_badge()
        self.preview_mode_var.set("📐 Схема CAD")
        self.preview_mode_switch.set("📐 Схема CAD")
        self._set_inspector_text(self._build_inspector_report(plan, extra_warnings=warnings))
        self._refresh_result_stats(plan)
        self._refresh_checks_list()
        self._refresh_neighbors_list()
        self._refresh_stepper()
        self._refresh_engine_card()
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

        # Собираем точки участков для автоцентрирования (длинные линии улиц не ужимают масштаб)
        world_pts: list[tuple[float, float]] = []
        for pl in plans:
            world_pts.append(pl.pob)
            for bl in pl.boundary_lines:
                world_pts.append(bl.start)
                world_pts.append(bl.end)
        if not world_pts:
            world_pts.append((0.0, 0.0))
            for pl in plans:
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

        # 0. Тонкая инженерная сетка WCS: минорные риски + мажорные линии + подписи осей
        grid_step = self._pick_nice_feet_step(68.0 / max(scale, 1e-6))
        minor_step = grid_step / 5.0
        vis_min_x = cx_w + (0.0 - (w * 0.5 + self._canvas_pan_x)) / scale
        vis_max_x = cx_w + (float(w) - (w * 0.5 + self._canvas_pan_x)) / scale
        vis_min_y = cy_w - (float(h) - (h * 0.5 + self._canvas_pan_y)) / scale
        vis_max_y = cy_w - (0.0 - (h * 0.5 + self._canvas_pan_y)) / scale
        if minor_step * scale > 14:
            gx_m = math.floor(vis_min_x / minor_step) * minor_step
            guard_m = 0
            while gx_m <= vis_max_x and guard_m < 220:
                sx_m, _ = to_screen((gx_m, 0.0))
                if abs((gx_m / grid_step) - round(gx_m / grid_step)) > 1e-6:
                    canvas.create_line(sx_m, 0, sx_m, h, fill=THEME["cad_grid_minor"], width=1)
                gx_m += minor_step
                guard_m += 1
            gy_m = math.floor(vis_min_y / minor_step) * minor_step
            guard_m = 0
            while gy_m <= vis_max_y and guard_m < 220:
                _, sy_m = to_screen((0.0, gy_m))
                if abs((gy_m / grid_step) - round(gy_m / grid_step)) > 1e-6:
                    canvas.create_line(0, sy_m, w, sy_m, fill=THEME["cad_grid_minor"], width=1)
                gy_m += minor_step
                guard_m += 1
        gx_val = math.floor(vis_min_x / grid_step) * grid_step
        grid_guard = 0
        while gx_val <= vis_max_x and grid_guard < 60:
            sx_g, _ = to_screen((gx_val, 0.0))
            canvas.create_line(sx_g, 0, sx_g, h, fill=THEME["cad_grid"], width=1)
            canvas.create_text(
                sx_g,
                h - 8,
                text=f"{gx_val:g}",
                anchor="s",
                fill=THEME["grid_axis"],
                font=("Consolas", 8),
            )
            gx_val += grid_step
            grid_guard += 1
        gy_val = math.floor(vis_min_y / grid_step) * grid_step
        grid_guard = 0
        while gy_val <= vis_max_y and grid_guard < 60:
            _, sy_g = to_screen((0.0, gy_val))
            canvas.create_line(0, sy_g, w, sy_g, fill=THEME["cad_grid"], width=1)
            canvas.create_text(
                8,
                sy_g - 5,
                text=f"{gy_val:g}",
                anchor="w",
                fill=THEME["grid_axis"],
                font=("Consolas", 8),
            )
            gy_val += grid_step
            grid_guard += 1

        # 1. Пунктирные линии красных линий улиц (STREET_ROW_AI) + подписи улиц
        labeled_streets: set[str] = set()
        for pl in plans:
            for idx_rl, rl in enumerate(pl.lines):
                x1, y1 = to_screen(rl.start)
                x2, y2 = to_screen(rl.end)
                canvas.create_line(
                    x1, y1, x2, y2, fill=THEME["cad_street"], width=1.6, dash=(6, 4)
                )
                street_label = ""
                if idx_rl == 0 and getattr(pl.description, "main_street", ""):
                    street_label = str(pl.description.main_street).strip()
                elif idx_rl == 1 and getattr(pl.description, "cross_street", ""):
                    street_label = str(pl.description.cross_street).strip()
                if street_label and street_label.lower() not in labeled_streets:
                    labeled_streets.add(street_label.lower())
                    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
                    canvas.create_text(
                        mx, my - 8, text=street_label,
                        fill=THEME["cad_street"], font=("Segoe UI", 9, "bold"),
                    )

        # 2. Перекрёсток улиц (0.0, 0.0): кольцо + перекрестие + подпись
        ox, oy = to_screen((0.0, 0.0))
        canvas.create_oval(
            ox - 6, oy - 6, ox + 6, oy + 6, outline=THEME["cad_origin"], width=1
        )
        canvas.create_line(ox - 10, oy, ox + 10, oy, fill=THEME["cad_origin"], width=1.2)
        canvas.create_line(ox, oy - 10, ox, oy + 10, fill=THEME["cad_origin"], width=1.2)
        canvas.create_text(
            ox + 12,
            oy + 16,
            text="(0,0)",
            anchor="nw",
            fill=THEME["cad_origin"],
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

            create_rect_fn = getattr(canvas, "create_rectangle", None)
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
                # Узловые точки вершин: квадратики WCS (как в CADVIEW), fallback — кружочки
                if callable(create_rect_fn):
                    for vx, vy in ((x1, y1), (x2, y2)):
                        create_rect_fn(
                            vx - 3.0,
                            vy - 3.0,
                            vx + 3.0,
                            vy + 3.0,
                            fill=THEME["bg_editor"],
                            outline=seg_color,
                            width=1.4,
                        )
                else:
                    canvas.create_oval(
                        x2 - 2.3, y2 - 2.3, x2 + 2.3, y2 + 2.3, fill=line_col, outline=""
                    )
                mx, my = 0.5 * (x1 + x2), 0.5 * (y1 + y2)
                # Круглый бейдж номера стороны по центру отрезка (подсветка «⚠» при подозрении)
                is_sus = idx in susp_map
                badge_col = THEME["cad_hover"] if is_hovered else (THEME["cad_tie"] if is_sus else "#1f2c3f")
                badge_txt = THEME["bg_editor"] if (is_hovered or is_sus) else "#9fb2c9"
                canvas.create_oval(
                    mx - 8, my - 8, mx + 8, my + 8, fill=badge_col, outline=""
                )
                canvas.create_text(
                    mx,
                    my + 0.5,
                    text=f"⚠ #{idx}" if is_sus else f"#{idx}",
                    fill=badge_txt,
                    font=("Consolas", 8, "bold"),
                )

            px, py = to_screen(focus_plan.pob)
            canvas.create_oval(
                px - 4.5, py - 4.5, px + 4.5, py + 4.5, fill=THEME["cad_tie"], outline=""
            )
            canvas.create_oval(
                px - 9, py - 9, px + 9, py + 9, outline=THEME["cad_tie"], width=1
            )
            canvas.create_text(
                px + 13,
                py - 9,
                text="POB",
                anchor="sw",
                fill=THEME["cad_tie"],
                font=("Segoe UI", 9, "bold"),
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
                    fill=THEME["cad_street_text"],
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

        # 6. Динамическая масштабная линейка в футах (левый нижний угол, засечки вверх)
        ruler_ft = self._pick_nice_feet_step(110.0 / max(scale, 1e-6))
        ruler_px = min(max(ruler_ft * scale, 28.0), max(w * 0.42, 60.0))
        rx0, ry0 = 24.0, float(h) - 40.0
        canvas.create_line(rx0, ry0, rx0 + ruler_px, ry0, fill="#cbd5e1", width=2)
        canvas.create_line(rx0, ry0 - 5, rx0, ry0, fill="#cbd5e1", width=2)
        canvas.create_line(rx0 + ruler_px, ry0 - 5, rx0 + ruler_px, ry0, fill="#cbd5e1", width=2)
        canvas.create_text(
            rx0,
            ry0 - 10,
            anchor="sw",
            text=f"{ruler_ft:g} ft",
            fill="#cbd5e1",
            font=("Consolas", 9, "bold"),
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

        # 8. Компасная стрелка на Север (правый верхний угол)
        nx_c, ny_c = w - 44.0, 70.0
        canvas.create_line(nx_c, ny_c + 22, nx_c, ny_c - 14, fill="#e2e8f0", width=2)
        tri_fn = getattr(canvas, "create_polygon", None)
        if callable(tri_fn):
            tri_fn(nx_c, ny_c - 22, nx_c - 6, ny_c - 10, nx_c + 6, ny_c - 10, fill="#e2e8f0", outline="")
        else:
            canvas.create_line(nx_c - 6, ny_c - 10, nx_c, ny_c - 22, fill="#e2e8f0", width=2)
            canvas.create_line(nx_c + 6, ny_c - 10, nx_c, ny_c - 22, fill="#e2e8f0", width=2)
        canvas.create_text(
            nx_c, ny_c + 30, text="N", fill="#e2e8f0", font=("Segoe UI", 10, "bold")
        )

    # -------------------------------------------------------------- helpers --
    def _on_close(self) -> None:
        self._persist_ai_preferences(report_error=False)
        self._closing = True
        self._generation += 1
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
        self._refresh_step2_height_label()
        if (
            hasattr(self, "stat_text_height_var")
            and self._last_active_plan is None
            and self._preview_main_plan is None
        ):
            self.stat_text_height_var.set(f"{height:.2f}")

    def is_neighbor_mode(self) -> bool:
        """Роль текста задаётся переключателем, роль файлов — при их выборе."""
        if self._current_source_mode() == "📝 Текст":
            return str(self.lot_role_var.get() or "main") == "neighbor"
        return self._visual_source_role == "neighbor"

    def _current_text_source_label(self, text: str) -> str:
        if self._text_source_snapshot is not None and text != self._text_source_snapshot:
            return f"изменённый текст (ранее: {self._text_source_label})"
        return self._text_source_label

    def _source_signature(self, *, mode: str | None = None) -> tuple[Any, ...] | None:
        """Идентификатор ввода; изменения требуют нового анализа."""
        mode = mode or self._current_source_mode()
        if mode == "📝 Текст":
            text = str(self.text_box.get("1.0", "end") or "").strip()
            return (mode, text, str(self.lot_role_var.get() or "main"), self._current_text_source_label(text)) if text else None
        if mode == "📄 PDF":
            pages = str(self.pdf_pages_var.get() or "").strip()
            if self.pdf_set:
                # В наборе страницы и способ разбора заданы по каждому файлу.
                return (mode, "set", set_signature(self.pdf_set), self._visual_source_revision)
            if self.neighbor_pdf_paths:
                return (mode, "neighbor", tuple(self.neighbor_pdf_paths), pages, self._visual_source_revision)
            if self.pdf_path is not None:
                # Извлечённый текст может быть отредактирован через вкладку «Текст»;
                # в основном PDF без диапазона он становится фактическим вводом.
                pdf_text = (
                    str(self.text_box.get("1.0", "end") or "").strip()
                    if not pages and not self._pdf_needs_vision else ""
                )
                return (mode, "main", self.pdf_path, pages, tuple(self._visual_rotations),
                        self._visual_source_revision, pdf_text)
        if mode == "📷 Фото" and self.image_paths:
            return (mode, self._visual_source_role, tuple(self.image_paths),
                    tuple(self._visual_rotations), self._visual_source_revision)
        return None

    def _mark_running_source_ready(self, plan: DrawPlan) -> None:
        self._ready_source_signature = self._running_source_signature
        self._input_incomplete = False
        source = self._running_source_path
        if not plan.is_neighbor:
            self._main_analysis_pending = False
        elif source is None:
            self._neighbor_text_pending = False
        else:
            self._pending_neighbor_files.discard(source)
            if source in self.neighbor_pdf_paths:
                self._pending_neighbor_pdfs.discard(source)
                self._neighbor_parsed_pages[source] = self._running_pdf_pages
        show_report = plan.is_neighbor and self._open_neighbor_report_after_parse
        self._open_neighbor_report_after_parse = False
        if show_report:
            self._open_neighbor_report_window()

    def _refresh_pdf_selection_ui(self) -> None:
        """Показывает роль и состав последнего выбранного набора PDF."""
        if self.pdf_set:
            count = len(self.pdf_set)
            self.load_pdf_button.configure(
                text=f"⭱ Набор PDF: {count} файл.\nВыбрать другие файлы · Ctrl+O"
            )
            self.neighbor_pdf_summary.configure(text=self._pdf_set_summary_text())
            self.pdf_pages_row.grid_remove()
            self.pdf_info_label.grid_remove()
            self.load_images_button.grid_remove()
            self.neighbor_pdf_summary.grid()
            self.pdf_set_buttons.grid()
            self.pdf_set_report_button.configure(
                state="normal" if self._pdf_set_report_ready and not self._busy else "disabled"
            )
            return
        # Общее поле страниц относится только к PDF, открытым через выбор роли;
        # в наборе страницы заданы у каждого файла.
        if self.pdf_path is not None or self.neighbor_pdf_paths:
            self.pdf_pages_row.grid()
        else:
            self.pdf_pages_row.grid_remove()
        self.pdf_set_buttons.grid_remove()
        if self._visual_source_role == "neighbor" and self.neighbor_pdf_paths:
            count = len(self.neighbor_pdf_paths)
            title = (
                f"Выбрано PDF соседей: {count}\nИзменить выбор · Ctrl+O"
                if count else "Выберите один или несколько PDF соседей\nCtrl+O"
            )
            self.load_pdf_button.configure(text=f"⭱ {title}")
            if count:
                names = "\n".join(f"• {p.name}" for p in self.neighbor_pdf_paths[:3])
                if count > 3:
                    names += f"\n… и ещё {count - 3} (список в журнале)"
                self.neighbor_pdf_summary.configure(
                    text=f"Каждый файл — отдельный участок:\n{names}"
                )
                self.pdf_info_label.grid_remove()
                self.load_images_button.grid_remove()
                self.neighbor_pdf_summary.grid()
            else:
                self.pdf_info_label.grid()
                self.load_images_button.grid()
                self.neighbor_pdf_summary.grid_remove()
        else:
            self.load_pdf_button.configure(
                text="⭱ Выберите PDF: наш участок и соседи сразу\nТекст, чертёж или скан · Ctrl+O"
            )
            self.pdf_info_label.grid()
            self.load_images_button.grid()
            self.neighbor_pdf_summary.grid_remove()

    def _on_lot_role_changed(self) -> None:
        # Этот переключатель относится только к тексту; готовая схема от него не меняется.
        self._refresh_stepper()

    def _preview_text_offline_silently(self, sample_text: str) -> None:
        """Мгновенно обновляет правую схему при выборе встроенного примера."""
        try:
            desc, warns = parse_with_regex(sample_text)
            plan = AcadBuilder(self.config_data).plan(
                desc,
                scale=self._current_scale(),
                is_neighbor=self.is_neighbor_mode(),
            )
            self._record_plan_for_preview(
                plan, warnings=warns, source_label=self._current_text_source_label(sample_text.strip()),
            )
            self._ready_source_signature = self._source_signature()
            self._input_incomplete = False
            if plan.is_neighbor:
                self._neighbor_text_pending = False
            else:
                self._main_analysis_pending = False
            self._update_closure_badge(plan.closure.short_report(), plan.closure.distance)
        except Exception:  # noqa: BLE001
            pass

    def _reset_visual_sources(self) -> None:
        self.pdf_path = None
        self.pdf_set = []
        self._pdf_set_main_plan = None
        self._pdf_set_scene_neighbors = set()
        self._pdf_set_report_ready = False
        self._pdf_set_analysis = None
        self.neighbor_pdf_paths = []
        self._pending_neighbor_pdfs.clear()
        self._neighbor_parsed_pages.clear()
        self.image_paths = []
        self._visual_source_role = None
        self._visual_source_revision += 1
        self._pdf_needs_vision = False
        self._loaded_pdf_text = ""
        self._text_source_label = "текст"
        self._text_source_snapshot = None
        self._text_source_path = None
        self._text_source_role = None
        self._pdf_total_pages = 0
        self._visual_rotations = []
        self._visual_cursor = 0
        self._tk_photo_ref = None
        self.pdf_pages_var.set("")
        self.auto_orient_var.set(False)
        self._refresh_visual_bar()
        self._refresh_pdf_selection_ui()
        self._refresh_parse_actions()

    def _on_sample_selected(self, label: str) -> None:
        sample = self._sample_presets.get(label)
        if not sample:
            return
        self.text_box.delete("1.0", "end")
        self.text_box.insert("1.0", sample)
        self._refresh_text_counter()
        self._reset_visual_sources()
        self._text_source_label = f"пример: {label}"
        self._text_source_snapshot = sample.strip()
        self.source_mode_var.set("📝 Текст")
        self.source_mode_switch.set("📝 Текст")
        self._on_source_mode_changed()
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
        self.source_mode_var.set("📝 Текст")
        try:
            self.source_mode_switch.set("📝 Текст")
        except Exception:  # noqa: BLE001
            pass
        self._on_source_mode_changed()
        self._refresh_text_counter()
        self._reset_visual_sources()
        self._text_source_label = f"пример: {selected or 'базовый'}"
        self._text_source_snapshot = sample.strip()
        self.pdf_label.configure(
            text=f"Пример: {selected or 'базовый'}",
            text_color=THEME["sky"],
        )
        self._preview_text_offline_silently(sample)

    def _reset_session_clicked(self) -> None:
        from tkinter import messagebox

        question = (
            "Очистить загруженные файлы, текст, схемы, результаты, счётчики и журнал?\n\n"
            "API-ключи и настройки сохранятся. Исходные файлы и уже созданные "
            "объекты в AutoCAD не удаляются."
        )
        if self._busy:
            question += (
                "\n\nЗадание ещё выполняется: уже отправленный запрос к ИИ или вызов "
                "AutoCAD нельзя прервать; его результат будет проигнорирован."
            )
        if messagebox.askyesno("Новый расчёт", question, parent=self):
            self._reset_session()

    def _reset_session(self) -> None:
        """Очищает текущую работу, не меняя настройки, ключи и файлы на диске."""
        was_busy = self._busy
        self._generation += 1
        self._dismiss_conflict_ui()
        self._close_pdf_set_dialog()
        for win in self._session_windows:
            try:
                win.destroy()
            except Exception:  # noqa: BLE001
                pass
        self._session_windows.clear()
        while True:
            try:
                self._events.get_nowait()
            except queue.Empty:
                break
        self._finish_busy()
        self.text_box.delete("1.0", "end")
        self._reset_visual_sources()
        self.description = None
        self.draw_result = None
        self.last_usage = None
        self.batch_neighbor_results = []
        self._last_consensus_label = None
        self._session_llm_calls = 0
        self._session_prompt_tokens = 0
        self._session_completion_tokens = 0
        self.lot_role_var.set("main")
        self._on_lot_role_changed()
        self.sample_var.set("Базовый пример (TX)")
        self.source_mode_var.set("📝 Текст")
        self.source_mode_switch.set("📝 Текст")
        self._on_source_mode_changed()
        self.preview_mode_var.set("📐 Схема CAD")
        self.preview_mode_switch.set("📐 Схема CAD")
        self.result_tab_var.set("📋 Ведомость")
        self._switch_result_tab()
        self.pdf_label.configure(text="PDF не загружен", text_color=THEME["text_dim"])
        self.tokens_var.set("Токены за вызов: —")
        self._clear_preview_scene()
        self._update_closure_badge("Closure Error: —", None)
        self._refresh_text_counter()
        self._refresh_parse_actions()
        self._set_status(
            "Расчёт сброшен. Начните с загрузки PDF, фото или текста."
            + (" Отправленный запрос/вызов AutoCAD может ещё завершаться." if was_busy else ""),
            warn=was_busy,
        )
        self._clear_log()

    def _clear_text(self) -> None:
        self._dismiss_conflict_ui()
        self.text_box.delete("1.0", "end")
        self._reset_visual_sources()
        self.pdf_label.configure(
            text="PDF не загружен",
            text_color=THEME["text_muted"],
        )
        self._ready_source_signature = None
        self._input_incomplete = bool(self._scene_plans())
        self.preview_mode_var.set("📐 Схема CAD")
        self.preview_mode_switch.set("📐 Схема CAD")
        self._set_status("Исходные данные очищены; схема сохранена для просмотра. Для отрисовки разберите источник заново.")
        self._refresh_suspicion_badge()
        self._refresh_checks_list()
        self._refresh_neighbors_list()
        self._refresh_stepper()
        self._refresh_text_counter()
        self._redraw_preview_canvas()

    def _apply_loaded_images(self, paths: list[Path], *, role: str = "main") -> None:
        """Активирует набор фотографий одного основного или соседнего участка."""
        if not paths:
            return
        self._reset_visual_sources()
        self._visual_source_role = role
        if role == "main":
            self._main_analysis_pending = True
        self.text_box.delete("1.0", "end")
        self.image_paths = list(paths)
        self._visual_rotations = [0] * len(paths)
        self._visual_cursor = 0
        self.use_llm_var.set(True)
        self.source_mode_var.set("📷 Фото")
        try:
            self.source_mode_switch.set("📷 Фото")
        except Exception:  # noqa: BLE001
            pass
        self._on_source_mode_changed()
        self._refresh_consensus_rows()
        self._refresh_visual_bar()

        role_text = "сосед" if role == "neighbor" else "основной"
        if len(paths) == 1:
            badge = f"Фото Vision ({role_text}): {paths[0].name}"
        else:
            badge = f"Фото Vision ({role_text}): {len(paths)} файл. ({paths[0].name} + ещё {len(paths) - 1})"
        self.pdf_label.configure(text=badge, text_color=THEME["warn"])
        self._set_preview_mode("🖼 Исходник")
        self._set_status(
            f"Загружено фото чертежа ({len(paths)} шт.) — включён режим Vision LLM "
            "(бесплатное выравнивание EXIF и фильтр «Чёткость 80-х» активны; "
            "платный ИИ-поворот по умолчанию выключен). "
            "При необходимости поверните кадры бесплатно кнопками ⟲ / ⟳ / 180°."
        )

    def _ask_source_role(self, source_label: str) -> str | None:
        """Явный выбор роли до открытия системного диалога файлов."""
        if getattr(self, "_source_role_dialog", None) is not None:
            return None
        win = ctk.CTkToplevel(self)
        self._source_role_dialog = win
        win.title("Выберите тип участка")
        win.geometry("480x210")
        win.resizable(False, False)
        win.configure(fg_color=THEME["bg_root"])
        win.transient(self)
        win.attributes("-topmost", bool(self.topmost_var.get()))
        self._attach_child_window(win)
        win.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkLabel(
            win, text=f"Какой участок содержится в {source_label}?",
            font=ctk.CTkFont(size=15, weight="bold"), text_color=THEME["text_main"],
            wraplength=440, justify="center",
        ).grid(row=0, column=0, columnspan=2, padx=20, pady=(22, 8))
        ctk.CTkLabel(
            win, text="Выберите роль перед открытием файлов.",
            font=ctk.CTkFont(size=12), text_color=THEME["text_muted"],
        ).grid(row=1, column=0, columnspan=2, pady=(0, 12))
        choice: list[str | None] = [None]

        def choose(role: str | None) -> None:
            choice[0] = role
            win.destroy()

        main_button = ctk.CTkButton(
            win, text="Основной участок", fg_color=THEME["btn_primary"],
            hover_color=THEME["btn_primary_hover"], command=lambda: choose("main"),
        )
        main_button.grid(row=2, column=0, sticky="ew", padx=(20, 5))
        neighbor_button = ctk.CTkButton(
            win, text="Соседний участок / участки", fg_color=THEME["btn_neighbor"],
            hover_color=THEME["btn_neighbor_hover"], command=lambda: choose("neighbor"),
        )
        neighbor_button.grid(row=2, column=1, sticky="ew", padx=(5, 20))
        cancel_button = ctk.CTkButton(
            win, text="Отмена", fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"], command=lambda: choose(None),
        )
        cancel_button.grid(row=3, column=0, columnspan=2, pady=(12, 16))
        win._role_buttons = {"main": main_button, "neighbor": neighbor_button, "cancel": cancel_button}
        win.protocol("WM_DELETE_WINDOW", lambda: choose(None))
        win.bind("<Escape>", lambda _event: choose(None))
        win.grab_set()
        try:
            self.wait_window(win)
        finally:
            self._source_role_dialog = None
        return choice[0]

    def _load_images(self) -> None:
        """Выбор одной или нескольких фотографий одного участка."""
        if self._busy or getattr(self, "_source_role_dialog", None) is not None:
            return
        from tkinter import filedialog

        role = self._ask_source_role("изображениях")
        if role not in {"main", "neighbor"}:
            return
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
        pdf_only = [p for p in chosen if p.suffix.lower() == ".pdf"]
        if img_only and len(img_only) == len(chosen):
            self._apply_loaded_images(img_only, role=role)
        elif pdf_only and len(pdf_only) == len(chosen):
            if role == "neighbor":
                self._attach_neighbor_pdfs(pdf_only)
            elif len(pdf_only) == 1:
                self._open_single_pdf(pdf_only[0])
            else:
                self._set_status("Для основного участка выберите один PDF; фото можно выбрать несколько.", error=True)
        else:
            self._set_status("Выберите либо фотографии одного участка, либо PDF, без смешивания типов.", error=True)

    def _load_pdf(self) -> None:
        """
        Один выбор для всех PDF: наш участок и соседи вместе.

        Роль не спрашивается заранее — после выбора открывается список, где
        отмечается наш участок и по каждому файлу задаются страницы.
        """
        if self._busy or getattr(self, "_source_role_dialog", None) is not None:
            return
        from tkinter import filedialog

        ext_glob = " ".join(f"*{ext}" for ext in IMAGE_EXTENSIONS)
        paths = filedialog.askopenfilenames(
            title="Выберите PDF нашего участка и соседей — можно несколько сразу",
            filetypes=[
                ("PDF", "*.pdf"),
                ("PDF и фото чертежей", f"*.pdf {ext_glob}"),
                ("Все файлы", "*.*"),
            ],
        )
        chosen = [Path(p) for p in (paths or ()) if str(p).strip()]
        if not chosen:
            return
        images = [path for path in chosen if is_image_file(path)]
        if images and len(images) == len(chosen):
            # Фотографии работают по-прежнему: один участок, роль выбирается явно.
            role = self._ask_source_role("изображениях")
            if role in {"main", "neighbor"}:
                self._apply_loaded_images(images, role=role)
            return
        if images:
            self._set_status(
                "Выберите либо PDF, либо фотографии одного участка, без смешивания типов.", error=True,
            )
            return
        items, errors = self._make_pdf_set_items(chosen)
        for error in errors:
            self._log_line(f"! Не открыт: {error}")
        if not items:
            self._set_status(
                f"Не удалось открыть PDF: {errors[0] if errors else 'файлы не выбраны'}", error=True,
            )
            return
        self._open_pdf_set_dialog(items, fresh=True, notes=errors)

    def _local_ocr_available(self) -> bool:
        """
        Можно ли распознавать сканы локально (бесплатно) — настройка ``ocr.enabled``
        и установленный пакет. Без него программа работает как раньше: сканы в ИИ.
        """
        enabled = bool((self.config_data.get("ocr") or {}).get("enabled", True))
        return bool(enabled and ocr_pages.available())

    def _make_pdf_set_items(
        self, paths: list[Path], *, existing: list[Path] | None = None,
    ) -> tuple[list[PdfSetItem], list[str]]:
        """Открывает выбранные PDF; страницы берутся из имени файла (…_p17, …_p2_5)."""
        limit = int(self.config_data.get("api", {}).get("vision_max_pages") or 8)
        ocr_ok = self._local_ocr_available()
        known = set(existing or [])
        items: list[PdfSetItem] = []
        errors: list[str] = []
        for path in dict.fromkeys(paths):
            if path in known:
                continue
            try:
                items.append(
                    make_item(path, vision_max_pages=limit, ocr_available=ocr_ok)
                )
            except Exception as exc:  # noqa: BLE001 - один плохой файл не срывает выбор
                errors.append(f"{path.name}: {exc}")
        return items, errors

    def _edit_pdf_set(self) -> None:
        """Открывает список текущего набора: роли, страницы и способ остаются правимыми."""
        if self._busy or getattr(self, "_source_role_dialog", None) is not None:
            return
        if not self.pdf_set:
            self._load_pdf()
            return
        # Правятся копии: отмена окна не меняет набор, уже показанный на схеме.
        self._open_pdf_set_dialog([dataclasses.replace(item) for item in self.pdf_set], fresh=False)

    def _open_pdf_set_dialog(
        self, items: list[PdfSetItem], *, fresh: bool, notes: list[str] | None = None,
    ) -> Any:
        if getattr(self, "_source_role_dialog", None) is not None:
            return None
        dialog = PdfSetDialog(self, items, fresh=fresh, notes=notes)
        self._pdf_set_dialog = dialog
        # Открытый список блокирует горячие клавиши разбора и отрисовки.
        self._source_role_dialog = dialog.win
        return dialog

    def _close_pdf_set_dialog(self) -> None:
        dialog = self._pdf_set_dialog
        if dialog is not None:
            dialog.close()

    def _submit_pdf_set(self, items: list[PdfSetItem], *, scene_mode: str, fresh: bool) -> None:
        """
        Делает набор текущим источником и сразу разбирает все его участки.

        ``scene_mode``: ``replace`` — схема очищается; ``add`` — участки набора
        добавляются к имеющимся; ``update`` — правка уже показанного набора.
        """
        if self._busy or not items:
            return
        if self.pending_conflict is not None or self.pending_doubt is not None:
            self._set_status("Сначала разрешите конфликт или сомнение в результате разбора.", error=True)
            return
        own_main = self._pdf_set_main_plan
        own_neighbors = set(self._pdf_set_scene_neighbors)
        main_pending = self._pdf_set_main_pending
        old_paths = {item.path for item in self.pdf_set}
        if scene_mode == "replace":
            self._clear_preview_scene()
            own_main, own_neighbors, main_pending = None, set(), False
        elif fresh:
            # Прежние участки остаются на схеме как есть, новый набор их не трогает.
            if main_pending:
                # Неразобранный наш участок прежнего набора заменён новым выбором.
                self._main_analysis_pending = False
            own_main, own_neighbors, main_pending = None, set(), False
        self._reset_visual_sources()
        self._visual_source_role = "set"
        self.pdf_set = list(items)
        self._pdf_set_main_plan = own_main
        self._pdf_set_scene_neighbors = own_neighbors
        self._pdf_set_main_pending = main_pending
        # Файлы, убранные из набора, больше не блокируют отрисовку.
        self._pending_neighbor_files.difference_update(old_paths)
        self.text_box.delete("1.0", "end")
        self._refresh_text_counter()
        self.source_mode_var.set("📄 PDF")
        try:
            self.source_mode_switch.set("📄 PDF")
        except Exception:  # noqa: BLE001
            pass
        self._on_source_mode_changed()
        paid = sum(1 for item in items if item.use_llm)
        self.pdf_label.configure(
            text=f"Набор PDF: {len(items)} файл. (ИИ: {paid}, локально: {len(items) - paid})",
            text_color=THEME["warn"] if paid else THEME["ok"],
        )
        self._start_pdf_set()

    def _start_pdf_set(
        self, *, force: bool = False, ask_advisor: bool = False, join_mode: str | None = None,
    ) -> None:
        """
        Разбирает все участки набора одним заданием; AutoCAD запускается отдельно.

        ``ask_advisor`` — отправить ИИ-советнику вопрос о расхождениях заново,
        даже если ответ для этого разбора уже есть или набор разобран локально.
        """
        if self._busy or not self.pdf_set:
            return
        if self.pending_conflict is not None or self.pending_doubt is not None:
            self._set_status("Сначала разрешите конфликт или сомнение в результате разбора.", error=True)
            return
        self._dismiss_conflict_ui()
        self._select_result_tab("🧾 Журнал")  # разбор идёт — смотрим журнал, а не ведомость
        self._running_source_signature = self._source_signature(mode="📄 PDF")
        self._running_source_path = None
        self._running_source_label = None
        self._running_batch_attached = False
        self._running_pdf_pages = None
        self._ready_source_signature = None
        self._input_incomplete = True
        has_main = any(item.is_main for item in self.pdf_set)
        if has_main:
            self._main_analysis_pending = True
            self._pdf_set_main_pending = True
        elif self._pdf_set_main_pending:
            # Наш участок убран из набора: его незавершённый разбор больше не ждём.
            self._main_analysis_pending = False
            self._pdf_set_main_pending = False
        for item in self.pdf_set:
            if item.is_main:
                self._pending_neighbor_files.discard(item.path)
            else:
                self._pending_neighbor_files.add(item.path)
        jobs = [make_job(item, force=force) for item in self.pdf_set]
        cfg, provider_id, model_name, api_key, key_secret = self._build_runtime_config()
        scale = self._current_scale()
        has_vision = any(job.source is not None and "pdf_path" in job.source for job in jobs)
        do_auto_orient = self._consume_one_shot_auto_orient(has_vision)
        self._pdf_set_report_ready = False
        self._busy = True
        self._set_controls_busy(True)
        self.progress.start()
        reused = sum(1 for job in jobs if job.cached_outcome is not None)
        paid = sum(1 for job in jobs if job.use_llm and job.cached_outcome is None)
        local = len(jobs) - reused - paid
        by_ocr = sum(1 for job in jobs if job.ocr and job.cached_outcome is None)
        detail = f"ИИ: {paid}, локально: {local}"
        if by_ocr:
            detail += f" (из них OCR: {by_ocr})"
        economy_jobs = sum(1 for job in jobs if job.economy and job.cached_outcome is None)
        if economy_jobs:
            detail += f", эконом: {economy_jobs} (ИИ при проблемах)"
        if reused:
            detail += f", без изменений: {reused}"
        self._set_status(f"Разбор набора PDF: {len(jobs)} файл. ({detail})…")
        self._log_line(f"--- Набор PDF: {len(jobs)} файл. ({detail}) ---")
        for job in jobs:
            self._log_line(
                f"  {job.role_label}: {job.path.name} · {job.pages_label} · {job.mode_label}"
                + (" · результат уже есть" if job.cached_outcome is not None else "")
            )
        join_images: tuple[str, ...] = ()
        if join_mode and self._pdf_set_tax_map is not None:
            join_images = (self._pdf_set_tax_map[1],)
        self._start_background(
            self._pdf_set_worker,
            args=(
                jobs, cfg, provider_id, model_name, api_key, key_secret, scale, do_auto_orient,
                dict(self._pdf_set_advice), ask_advisor, join_mode, join_images,
            ),
        )

    def _parse_pdf_set_job(
        self, job: PdfSetJob, cfg: dict[str, Any], provider_id: str, model_name: str,
        api_key: str | None, key_secret: str, do_auto_orient: bool, progress: Any, post: Any,
    ) -> tuple[ParseOutcome, bool, list[str]]:
        """
        Разбирает один файл набора по его страницам.

        Если проходы ИИ разошлись, участок не выбрасывается: берётся проход с
        лучшим замыканием, а расхождение возвращается замечанием к участку.
        """
        source = dict(job.source or {})
        enhance = bool(cfg.get("api", {}).get("vision_enhance_scans", True))
        passes = int((cfg.get("api", {}).get("consensus") or {}).get("passes") or 1)
        auto_orient = bool(do_auto_orient and "pdf_path" in source)
        if job.use_llm and passes >= 2:
            consensus = run_multipass_consensus(
                **source, cfg=cfg, enhance_scan=enhance, auto_orient=auto_orient,
                passes=passes, primary_provider=provider_id, primary_model=model_name,
                primary_api_key=api_key, key_secret=key_secret, progress=progress,
            )
            post("round_usages", [out.usage for out in consensus.pass_outcomes])
            if consensus.agreed:
                return consensus.selected_outcome, True, []
            count = len(consensus.pass_outcomes)
            closures = [consensus.pass_closure_ft(index) for index in range(1, count + 1)]
            best = min(range(count), key=lambda index: closures[index])
            issue = (
                f"проходы ИИ разошлись ({len(consensus.discrepancies)} спорн. мест); взят "
                f"{consensus.pass_label(best + 1)} с лучшим замыканием ({closures[best]:.3f} ft) — "
                "сверьте с документом"
            )
            chosen = consensus.pass_outcomes[best]
            outcome = dataclasses.replace(
                chosen,
                warnings=[f"⚠ {issue}"] + list(chosen.warnings) + [consensus.discrepancy_report()],
                usage=consensus.total_usage,
                provider_label=f"Расхождение {count} проходов",
            )
            return outcome, True, [issue]
        # Эконом только для текстовых файлов: сканы уже идут лестницей OCR и сами зовут ИИ.
        economy_escalates = bool(
            job.economy and not job.use_llm and not job.ocr
            and _ai_key_status(cfg, provider_id, model_name, api_key, key_secret)[0]
        )
        try:
            outcome = parse_legal_description_full(
                **source, cfg=cfg, enhance_scan=enhance, auto_orient=auto_orient,
                use_llm=job.use_llm, provider=provider_id, model=model_name, api_key=api_key,
                key_secret=key_secret, allow_regex_fallback=False, progress=progress,
                escalate_to_llm=economy_escalates,
            )
        except Exception as local_exc:  # noqa: BLE001
            if job.use_llm or job.ocr or not _ai_key_status(cfg, provider_id, model_name, api_key, key_secret)[0]:
                raise
            if progress is not None:
                progress(f"локальный парсер не справился ({local_exc}) — отправляю в ИИ…")
            outcome = parse_legal_description_full(
                **source, cfg=cfg, enhance_scan=enhance, auto_orient=auto_orient,
                use_llm=True, provider=provider_id, model=model_name, api_key=api_key,
                key_secret=key_secret, allow_regex_fallback=False, progress=progress,
            )
        return outcome, False, []

    @staticmethod
    def _find_step0_cache_path(pdf_path: Path) -> Path | None:
        base_stem = re.sub(r"_p\d+(?:[-,]\d+)*$", "", pdf_path.stem)
        for candidate_stem in dict.fromkeys((pdf_path.stem, base_stem)):
            candidate = pdf_path.with_name(f"{candidate_stem}_text.txt")
            if candidate.is_file():
                return candidate
        return None

    @staticmethod
    def _shape_rank(plan: Any, issues: list[str], warnings: Any = ()) -> tuple[int, int, float]:
        if plan is None:
            return (0, -999, -9999.0, 0)
        where, _ = ocr_closure_note(plan)
        suspicion = getattr(plan, "suspicion", None)
        suspicious = bool(suspicion is not None and suspicion.is_suspicious)
        no_distance = _lacks_corner_distance(list(warnings or ()) + list(getattr(plan, "warnings", None) or ()))
        severe_count = (
            len(issues) + (1 if where == "issue" else 0)
            + (1 if suspicious else 0) + (1 if no_distance else 0)
        )
        closure = float(getattr(plan, "closure_error_ft", 0.0) or 0.0)
        # При равной фигуре больше данных о расстояниях (угол, длины сторон) — лучше.
        desc = getattr(plan, "description", None)
        info = 0
        if desc is not None:
            info += 1 if float(getattr(desc, "distance_to_corner_ft", 0.0) or 0.0) > 0 else 0
            info += sum(
                1 for seg in (getattr(desc, "segments", None) or ())
                if float(getattr(seg, "distance_ft", 0.0) or 0.0) > 0 and getattr(seg, "has_bearing", True)
            )
        return (1, -severe_count, -round(closure, 4), info)

    def _local_text_rank(
        self, job: PdfSetJob, text: str, builder: AcadBuilder, scale: float, cfg: dict[str, Any],
        provider_id: str, model_name: str, api_key: str | None, key_secret: str,
        do_auto_orient: bool, post: Any,
    ) -> tuple[int, int, float, int] | None:
        """Ранг текста, как его прочитает локальный разбор (без ИИ). None — текст не читается."""
        try:
            probe = dataclasses.replace(job, source={"text": text}, use_llm=False, ocr=True)
            outcome, _, issues = self._parse_pdf_set_job(
                probe, cfg, provider_id, model_name, api_key, key_secret, do_auto_orient,
                lambda msg: None, post,
            )
            plan = builder.plan(outcome.data, scale=scale, is_neighbor=not job.is_main)
        except Exception:  # noqa: BLE001 - нечитаемый текст просто не даёт ранга
            return None
        return self._shape_rank(plan, list(issues), outcome.warnings)

    def _write_ai_to_step0_cache(
        self, job: PdfSetJob, data: Any, *, prev_rank: tuple[int, int, float, int] | None,
        cache_path: Path | None, builder: AcadBuilder, scale: float, cfg: dict[str, Any],
        provider_id: str, model_name: str, api_key: str | None, key_secret: str,
        do_auto_orient: bool, post: Any,
    ) -> str:
        """
        Пишет ответ ИИ в кеш Шага 0, если пересобранный текст лучше прежнего уровня.

        Лучше — строго выше ранга (фигура, затем данные о расстояниях). При равенстве кеш
        не меняется. Возвращает строку для журнала.
        """
        rendered = legal_to_step0_text(data)
        if rendered is None:
            return "ответ ИИ нельзя записать текстом (сторона без румба) — кеш не изменён"
        new_rank = self._local_text_rank(
            job, rendered, builder, scale, cfg, provider_id, model_name, api_key, key_secret,
            do_auto_orient, post,
        )
        if new_rank is None:
            return "ответ ИИ не читается локально — кеш не изменён"
        if prev_rank is not None and not new_rank > prev_rank:
            return "ответ ИИ не лучше кеша — кеш не изменён"
        target = cache_path or job.path.with_name(f"{job.path.stem}_text.txt")
        try:
            target.write_text(ocr_pages.description_key_lines(rendered) or rendered, encoding="utf-8")
        except OSError as exc:
            return f"кеш не записан: {exc}"
        return f"кеш Шага 0 обновлён ответом ИИ ({target.name})"

    def _pdf_set_worker(
        self, jobs: list[PdfSetJob], cfg: dict[str, Any], provider_id: str, model_name: str,
        api_key: str | None, key_secret: str, scale: float, do_auto_orient: bool,
        advice: dict[str, AdvisorReply] | None = None, ask_advisor: bool = False,
        join_mode: str | None = None, join_images: tuple[str, ...] = (),
    ) -> None:
        """Фоновый разбор набора: ошибка одного файла не останавливает остальные."""
        post = self._worker_poster()
        builder = AcadBuilder(cfg)
        ai_ready, ai_block = _ai_key_status(cfg, provider_id, model_name, api_key, key_secret)
        results: list[PdfSetResult] = []
        total = len(jobs)
        for idx, job in enumerate(jobs, start=1):
            if not self._worker_is_current():
                return
            result = PdfSetResult(path=job.path, is_main=job.is_main, parse_key=job.parse_key)
            try:
                if job.cached_outcome is not None:
                    result.outcome = job.cached_outcome
                    result.issues = list(job.cached_issues)
                    result.pre_counted = True
                    result.cached = True
                    result.llm_used = bool(job.use_llm)
                else:
                    if job.source is None:
                        raise PDFError(job.source_error or "нет данных для разбора")
                    if job.ocr:
                        # Скан: кеш Шага 0 -> один проход локального OCR -> ИИ, только если проверка
                        # описания не пройдена (статус ok/warning/failed из description_check).
                        cache_path = self._find_step0_cache_path(job.path)
                        cached_text = ""
                        if cache_path is not None:
                            try:
                                cached_text = cache_path.read_text(encoding="utf-8-sig", errors="replace").strip()
                            except OSError:
                                cached_text = ""

                        def progress_of(msg: str, i: int = idx) -> None:
                            post("progress", f"[{i}/{total}] {msg}")

                        def judge(source: dict[str, Any], use_llm: bool, label: str = "") -> _ScanCandidate:
                            """Разбирает источник, строит план и ранжирует: сначала статус проверки, затем фигура."""
                            judged_job = dataclasses.replace(job, source=source, use_llm=use_llm)
                            o, pre, iss = self._parse_pdf_set_job(
                                judged_job, cfg, provider_id, model_name, api_key, key_secret,
                                do_auto_orient, progress_of, post,
                            )
                            d = apply_changes(o.data, fix_changes(job.fixes))
                            p = builder.plan(d, scale=scale, is_neighbor=not job.is_main)
                            shape = self._shape_rank(p, iss, o.warnings)
                            if label:
                                # Баллы фигуры: сколько обязательных значений нашлось и почему результат не ok.
                                progress_of(f"{job.path.name}: {label} — {figure_line(getattr(o, 'check', None))}")
                            return _ScanCandidate(
                                outcome=o, plan=p, issues=list(iss), pre_counted=bool(pre),
                                shape_rank=tuple(shape), status_rank=_check_status_rank(o, p),
                            )

                        best: _ScanCandidate | None = None
                        cache_candidate: _ScanCandidate | None = None
                        last_exc: Exception | None = None

                        if cached_text:
                            progress_of(f"{job.path.name}: проверяю кеш Шага 0 ({cache_path.name})…")
                            try:
                                cache_candidate = judge({"text": cached_text}, False, "кеш Шага 0")
                                best = cache_candidate
                            except Exception as exc:  # noqa: BLE001
                                last_exc = exc

                        if not _accepted_without_ai(best):
                            progress_of(f"{job.path.name}: распознаю локально, один проход (до {SCAN_OCR_DPI} dpi)…")
                            ocr = ocr_scan_once(job.path, job.source, progress=progress_of)
                            progress_of(f"{job.path.name}: OCR — {ocr.summary()}")
                            if ocr.error:
                                last_exc = PDFError(f"локальный OCR: {ocr.error}")
                            elif ocr.text:
                                # Разбор OCR-текста мог упасть: ошибка пишется в журнал, второй рендер
                                # (нативное разрешение) и слияние строк всё равно пробуются — до ИИ.
                                best_text = ocr.text
                                ocr_candidate: _ScanCandidate | None = None
                                try:
                                    ocr_candidate = judge({"text": ocr.text}, False, "OCR, 600 dpi")
                                except Exception as exc:  # noqa: BLE001
                                    last_exc = exc
                                    progress_of(f"{job.path.name}: разбор OCR-текста не удался — {describe_exc(exc)}")
                                if not _accepted_without_ai(ocr_candidate):
                                    progress_of(
                                        f"{job.path.name}: проверка не ok — дальше второй рендер "
                                        "(нативное разрешение) и слияние строк"
                                    )
                                    second = ocr_scan_once(job.path, job.source, progress=progress_of, exact=False)
                                    progress_of(f"{job.path.name}: второй рендер — {second.summary()}")
                                    if second.text:
                                        variants: list[tuple[str, _ScanCandidate]] = []
                                        if ocr_candidate is not None:
                                            variants.append((ocr.text, ocr_candidate))
                                        to_judge = [(second.text, "второй рендер")]
                                        if ocr_candidate is not None:
                                            to_judge.append(
                                                (ocr_pages.merge_line_texts(ocr.text, second.text), "слияние строк")
                                            )
                                        for text_v, text_label in to_judge:
                                            try:
                                                variants.append((text_v, judge({"text": text_v}, False, text_label)))
                                            except Exception as exc:  # noqa: BLE001
                                                last_exc = exc
                                                progress_of(f"{job.path.name}: {text_label} не разобран — {describe_exc(exc)}")
                                        if variants:
                                            text_best, cand_best = max(variants, key=lambda v: v[1].rank)
                                            if ocr_candidate is None or cand_best.rank > ocr_candidate.rank:
                                                if ocr_candidate is not None:
                                                    progress_of(
                                                        f"{job.path.name}: слияние двух рендеров дало "
                                                        f"{'ok' if cand_best.status_rank >= _STATUS_OK_RANK else 'неполный'} разбор"
                                                    )
                                                best_text, ocr_candidate = text_best, cand_best
                                if ocr_candidate is not None and (best is None or ocr_candidate.rank > best.rank):
                                    best = ocr_candidate
                                    # Новый текст лучше кеша (или кеша нет) — кеш обновляем.
                                    target_cache = cache_path or job.path.with_name(f"{job.path.stem}_text.txt")
                                    try:
                                        # В кеш — только строки описания участка (без шапки и страниц);
                                        # если блок не найден — полный текст, чтобы не потерять данные.
                                        target_cache.write_text(
                                            ocr_pages.description_key_lines(best_text) or best_text, encoding="utf-8",
                                        )
                                    except OSError:
                                        pass

                        if best is not None and best.status_rank < _STATUS_OK_RANK and _accepted_without_ai(best):
                            progress_of(f"{job.path.name}: не найдено только расстояние до угла — ИИ не вызван")
                        elif best is not None and best.status_rank < _STATUS_OK_RANK:
                            if ai_ready:
                                progress_of(f"{job.path.name}: проверка не пройдена — отправляю в ИИ…")
                                try:
                                    # Страницы приходят строкой «2» / «3-4»: разбираем их, а не индексируем.
                                    lp1, lp2 = pages_from_spec(job.source.get("ocr_pages") or job.source.get("pdf_pages") or "1")
                                    try:
                                        total_p = int(inspect_pdf(job.path).get("pages") or 0)
                                        if 1 <= total_p <= 2:
                                            lp1, lp2 = 1, total_p
                                    except Exception:  # noqa: BLE001
                                        pass
                                    clean_llm_source = {"pdf_path": job.path, "pdf_pages": (lp1, lp2)}
                                    result.llm_used = True  # ИИ запущен: токены могут быть потрачены
                                    ai = judge(clean_llm_source, True, "ИИ")
                                    post("valid_secret", key_secret)
                                    prev_shape = best.shape_rank
                                    # ИИ авторитетен при равных статусах и фигуре (решение 3.53).
                                    if ai.rank >= best.rank:
                                        best = ai
                                    ai_note = self._write_ai_to_step0_cache(
                                        job, ai.outcome.data, prev_rank=prev_shape, cache_path=cache_path,
                                        builder=builder, scale=scale, cfg=cfg, provider_id=provider_id,
                                        model_name=model_name, api_key=api_key, key_secret=key_secret,
                                        do_auto_orient=do_auto_orient, post=post,
                                    )
                                    progress_of(f"{job.path.name}: {ai_note}")
                                except Exception as exc:  # noqa: BLE001
                                    last_exc = exc
                                    best.issues.append(f"ИИ не отработал: {describe_exc(exc)}")
                            else:
                                reason = f"ИИ не вызван: {ai_block}"
                                best.issues.append(reason)
                                progress_of(f"{job.path.name}: {reason}")

                        if best is None:
                            if last_exc is not None and not isinstance(last_exc, PDFError):
                                raise PDFError(f"разбор скана не удался: {describe_exc(last_exc)}") from last_exc
                            raise last_exc or PDFError("не удалось распознать участок; введите текст вручную")
                        if not _accepted_without_ai(best):
                            best.issues.append("Требуется ручная проверка/ввод текста для этого лота")
                        result.ocr = True
                        outcome, pre_counted, issues = best.outcome, best.pre_counted, best.issues
                    else:
                        post(
                            "progress",
                            f"[{idx}/{total}] {job.role_label}: {job.path.name}, {job.pages_label} — {job.mode_label}…",
                        )
                        outcome, pre_counted, issues = self._parse_pdf_set_job(
                            job, cfg, provider_id, model_name, api_key, key_secret, do_auto_orient,
                            lambda msg, i=idx: post("progress", f"[{i}/{total}] {msg}"), post,
                        )
                        if not self._worker_is_current():
                            return
                        post(
                            "progress",
                            f"[{idx}/{total}] {job.path.name}: {figure_line(getattr(outcome, 'check', None))}",
                        )
                        result.llm_used = bool(job.use_llm or getattr(outcome, "source", "") == "llm")
                        if job.use_llm and job.scan and not job.ocr:
                            # Скан разобран ИИ сразу: его текст — в кеш, если лучше имеющегося.
                            cache_path = self._find_step0_cache_path(job.path)
                            prev_rank = None
                            if cache_path is not None:
                                try:
                                    cache_text = cache_path.read_text(encoding="utf-8-sig", errors="replace").strip()
                                except OSError:
                                    cache_text = ""
                                if cache_text:
                                    prev_rank = self._local_text_rank(
                                        job, cache_text, builder, scale, cfg, provider_id, model_name,
                                        api_key, key_secret, do_auto_orient, post,
                                    )
                            ai_note = self._write_ai_to_step0_cache(
                                job, outcome.data, prev_rank=prev_rank, cache_path=cache_path,
                                builder=builder, scale=scale, cfg=cfg, provider_id=provider_id,
                                model_name=model_name, api_key=api_key, key_secret=key_secret,
                                do_auto_orient=do_auto_orient, post=post,
                            )
                            post("progress", f"[{idx}/{total}] {job.path.name}: {ai_note}")
                        if job.use_llm or getattr(outcome, "source", "") == "llm":
                            post("valid_secret", key_secret)
                    result.outcome, result.pre_counted, result.issues = outcome, pre_counted, list(issues)
                # Принятые правки по данным соседей входят в построение, разбор не меняют.
                # Итог чтения легального описания — в журнал уровнем «ключ»: виден и в упрощённом режиме.
                post(
                    "reading_summary",
                    f"[{idx}/{total}] {job.path.name}: {reading_summary(getattr(result.outcome, 'check', None))}",
                )
                description = apply_changes(result.outcome.data, fix_changes(job.fixes))
                plan = builder.plan(description, scale=scale, is_neighbor=not job.is_main)
                if result.ocr:
                    # Геометрия — детектор ошибок распознавания, но не любой невязки:
                    # сторона без румба даёт гэп и без ошибок чтения (см. ocr_closure_note).
                    where, note = ocr_closure_note(plan)
                    if where == "issue":
                        result.issues.append(note)
                    elif where == "note":
                        plan.notes.append(note)
                for issue in result.issues:
                    if issue not in plan.warnings:
                        plan.warnings.append(issue)
                for fix in job.fixes:
                    plan.notes.append(f"Принята правка по данным соседей: {fix.title} (откуда: {fix.source})")
                result.plan = plan
            except Exception as exc:  # noqa: BLE001 - ошибка относится только к этому файлу
                result.error = str(exc) or type(exc).__name__
                result.outcome = None
                result.plan = None
            results.append(result)
        if not self._worker_is_current():
            return
        analysis = self._analyse_pdf_set_conflicts(
            jobs, results, builder, cfg, provider_id, model_name, api_key, key_secret, scale,
            advice or {}, ask_advisor, post, join_mode=join_mode, join_images=join_images,
        )
        if not self._worker_is_current():
            return
        post("pdf_set_done", (results, analysis))
        post("done", None)

    def _analyse_pdf_set_conflicts(
        self, jobs: list[PdfSetJob], results: list[PdfSetResult], builder: AcadBuilder,
        cfg: dict[str, Any], provider_id: str, model_name: str, api_key: str | None,
        key_secret: str, scale: float, advice: dict[str, AdvisorReply], ask_advisor: bool, post: Any,
        *, join_mode: str | None = None, join_images: tuple[str, ...] = (),
    ) -> ConflictAnalysis:
        """
        Сверяет описания участков набора между собой (в фоне).

        Сначала правила Python: чего не хватает одному описанию и есть ли это
        у соседа. Если серьёзный конфликт остался — платный вопрос ИИ-советнику;
        его советы тоже проверяются геометрией. Данные участков не меняются.
        """
        try:
            lots: list[LotInput] = []
            for job, res in zip(jobs, results):
                if res.outcome is None or res.plan is None:
                    continue
                outcome = res.outcome
                lots.append(LotInput(
                    key=str(job.path), name=job.path.name, is_main=job.is_main,
                    description=outcome.data, fixes=tuple(job.fixes),
                    source_text=str(getattr(outcome, "prepared_text", "") or ""),
                    images=tuple(getattr(outcome, "prepared_images", None) or ()),
                    used_ai=bool(res.llm_used),
                ))
            advisor_cfg = cfg.get("api", {}).get("conflict_advisor") or {}
            advisor = None
            note = ""
            # Платный вопрос задаётся только с разрешения пользователя: кнопкой
            # «🧠 Спросить ИИ-советника» (она спрашивает подтверждение) или
            # настройкой api.conflict_advisor.auto = true, включённой вручную.
            auto_advisor = bool(advisor_cfg.get("auto", False))
            if not ask_advisor and not auto_advisor:
                note = (
                    "не запрошен — платный вопрос задаётся только кнопкой "
                    "«🧠 Спросить ИИ-советника», с вашего подтверждения"
                )
            elif not ask_advisor and not advisor_cfg.get("enabled", True):
                note = "выключен в настройках (api.conflict_advisor.enabled)"
            elif not ask_advisor and not any(lot.used_ai for lot in lots):
                note = (
                    "не запрошен — набор разобран локально и бесплатно. "
                    "Кнопка «🧠 Спросить ИИ-советника» отправит платный запрос."
                )
            else:
                try:
                    has_key = bool(resolve_api_target(
                        cfg, provider=provider_id, model=model_name, api_key=api_key, key_secret=key_secret,
                    )["api_key"])
                except Exception:  # noqa: BLE001 - ключ не расшифровался
                    has_key = False
                if not has_key:
                    note = "не запрошен — не задан API-ключ выбранного провайдера"
                else:
                    def ask_model(system: str, user_text: str, images: list[str]) -> tuple[Any, Any]:
                        # Каждая попытка (в том числе оборванная) учитывается в журнале:
                        # токены потрачены, даже если целого ответа так и не пришло.
                        data, usage = ask_llm_json(
                            system, user_text, cfg, image_data_urls=images, api_key=api_key,
                            provider=provider_id, model=model_name, key_secret=key_secret,
                            progress=lambda message: post("progress", message), task_label="ИИ-советник",
                            attempts=int(advisor_cfg.get("attempts") or 3),
                            max_tokens=int(advisor_cfg.get("max_tokens") or 0) or None,
                            on_usage=lambda got: post("round_usages", [got]),
                        )
                        post("valid_secret", key_secret)
                        return data, usage

                    advisor = ask_model
            return analyse_conflicts(
                builder, lots, scale=scale, advisor=advisor, advisor_note=note, advisor_cache=advice,
                force_advisor=ask_advisor, max_images=int(advisor_cfg.get("max_images") or 16),
                progress=lambda message: post("progress", message),
                join_mode=join_mode if ask_advisor else None, join_images=join_images,
            )
        except Exception as exc:  # noqa: BLE001 - сбой анализа не срывает разбор набора
            return ConflictAnalysis(error=str(exc) or type(exc).__name__)

    def _drop_main_plan(self) -> None:
        plan = self._preview_main_plan
        if plan is None:
            return
        self._plan_outcomes.pop(id(plan), None)
        self._plan_source_labels.pop(id(plan), None)
        self._preview_main_plan = None
        if self._last_active_plan is plan:
            self._last_active_plan = None
            self._last_suspicion_report = None

    def _drop_neighbor_plan(self, source: Path) -> None:
        plan = self._neighbor_plan_sources.pop(source, None)
        if plan is None:
            return
        self._preview_neighbor_plans[:] = [p for p in self._preview_neighbor_plans if p is not plan]
        self._plan_outcomes.pop(id(plan), None)
        self._plan_source_labels.pop(id(plan), None)
        if self._last_active_plan is plan:
            self._last_active_plan = None
            self._last_suspicion_report = None

    def _pdf_set_plan_for(self, item: PdfSetItem) -> DrawPlan | None:
        """Участок этого файла на схеме (None, если он не построен)."""
        if not item.on_scene:
            return None
        if item.is_main:
            plan = self._pdf_set_main_plan
            return plan if plan is not None and plan is self._preview_main_plan else None
        if item.path not in self._pdf_set_scene_neighbors:
            return None
        return self._neighbor_plan_sources.get(item.path)

    def _pdf_set_issue_lines(self, item: PdfSetItem) -> list[tuple[str, str]]:
        """Замечания к одному участку набора: (серьёзность, текст)."""
        plan = self._pdf_set_plan_for(item)
        if plan is None:
            return []
        lines: list[tuple[str, str]] = []
        # Проверка описания — тот же источник, что и статус в журнале: если она нашла проблему
        # или чего-то не хватает, файл не может быть «без замечаний».
        check = getattr(item.outcome, "check", None)
        if check is not None and check.status != STATUS_OK:
            check_severity = "error" if check.status == STATUS_FAILED else "warn"
            for issue in check.issues:
                lines.append((check_severity, " ".join(str(issue.text).split())))
            for missing in (check.score.missing if check.score is not None else []):
                if missing.startswith("контур не замыкается"):
                    continue  # уже показано строкой невязки ниже
                lines.append((check_severity, f"не найдено: {missing}"))
        if plan.closure.distance > CLOSURE_LIMIT_FT:
            lines.append(("error", f"невязка {plan.closure.distance:.3f} ft — контур не замыкается"))
        for diag in self._plan_diagnostic_items(plan):
            text = diag.title + (f" — {diag.detail}" if diag.detail else "")
            text = " ".join(text.split())
            if len(text) > 260:
                text = text[:257].rstrip() + "…"
            lines.append(("error" if diag.severity == "error" else "warn", text))
        if plan.suspicion is None:
            lines.append(("warn", "проверка формы недоступна"))
        return lines

    def _sync_pdf_set_readiness(self) -> None:
        """Набор готов к отрисовке, когда каждый его файл построен при текущих страницах."""
        complete = bool(self.pdf_set) and all(
            item.on_scene and not item.error and item.has_fresh_result() for item in self.pdf_set
        )
        if complete:
            self._ready_source_signature = self._source_signature(mode="📄 PDF")
            self._input_incomplete = False
        elif self.pdf_set:
            self._ready_source_signature = None
            self._input_incomplete = True

    def _handle_pdf_set_done(self, payload: Any) -> None:
        """Ставит участки набора на схему и сообщает итог по каждому и по сверке с соседями."""
        results: list[PdfSetResult]
        analysis: ConflictAnalysis | None
        if isinstance(payload, tuple):
            results, analysis = payload
        else:
            results, analysis = payload, None
        self._pdf_set_analysis = analysis
        if analysis is not None and analysis.join_mode:
            # Соединения по карте подтверждены геометрией — применяем их сами, после завершения разбора.
            self._pending_join_accept = [
                chained
                for proposal in analysis.proposals
                if proposal.verified
                for chained in analysis.with_requirements(proposal.id)
            ]
        elif analysis is not None and analysis.advisor_status == "ok" and analysis.advisor_reply is not None:
            # Ответ советника хранится для этого разбора: правки его заново не запрашивают.
            self._pdf_set_advice[analysis.advisor_key] = analysis.advisor_reply
        by_path = {item.path: item for item in self.pdf_set}
        # Прежние участки этого набора снимаются: на схеме остаётся только текущий результат.
        for path in list(self._pdf_set_scene_neighbors):
            self._drop_neighbor_plan(path)
        self._pdf_set_scene_neighbors.clear()
        if self._pdf_set_main_plan is not None and self._preview_main_plan is self._pdf_set_main_plan:
            self._drop_main_plan()
        self._pdf_set_main_plan = None
        for item in self.pdf_set:
            item.on_scene = False

        active_plan: DrawPlan | None = None
        # Соседи первыми, наш участок последним — он остаётся активным в ведомости.
        for res in sorted(results, key=lambda r: r.is_main):
            item = by_path.get(res.path)
            if item is None:
                continue
            item.error = res.error
            item.issues = list(res.issues)
            item.ocr_used = bool(res.ocr) and not res.error
            item.llm_used = bool(res.llm_used) and not res.error
            if res.error or res.plan is None or res.outcome is None:
                item.outcome = None
                item.parsed_key = None
                if not item.error:
                    item.error = "участок не построен"
                self._log_line(f"! {item.path.name}: {item.error}")
                continue
            item.outcome = res.outcome
            item.parsed_key = res.parse_key
            if not res.cached:
                # Файл разобран заново: прежние правки относились к старому разбору.
                item.fixes = []
            if res.cached:
                self.description = res.outcome.data
            else:
                self._apply_parsed(res.outcome, already_counted=res.pre_counted)
            label = f"{item.path.name} · {format_pages(item.first_page, item.last_page)}"
            # Тот же файл мог стоять на схеме в другой роли.
            self._drop_neighbor_plan(res.path)
            self._record_plan_for_preview(
                res.plan, warnings=res.outcome.warnings, source=res.path,
                source_label=label, outcome=res.outcome,
            )
            if res.is_main:
                self._pdf_set_main_plan = res.plan
                self._main_analysis_pending = False
                self._pdf_set_main_pending = False
            else:
                self._pdf_set_scene_neighbors.add(res.path)
                self._pending_neighbor_files.discard(res.path)
            item.on_scene = True
            active_plan = res.plan

        self._sync_pdf_set_readiness()
        if active_plan is None:
            # Ни один участок набора не построен: обновляем схему без него.
            self._recompute_neighbor_verification()
            self._refresh_suspicion_badge()
            self._refresh_checks_list()
            self._refresh_neighbors_list()
            self._refresh_stepper()
            remaining = self._preview_main_plan
            self._refresh_result_stats(remaining)
            if remaining is not None:
                self._last_active_plan = remaining
                self._last_suspicion_report = remaining.suspicion
                self._set_inspector_text(self._build_inspector_report(remaining))
                self._update_closure_badge(remaining.closure.short_report(), remaining.closure.distance)
            else:
                self._render_empty_inspector()
                self._update_closure_badge("Closure Error: —", None)
            self._redraw_preview_canvas()
        else:
            self._update_closure_badge(active_plan.closure.short_report(), active_plan.closure.distance)

        total = len(self.pdf_set)
        built = [item for item in self.pdf_set if item.on_scene]
        failed = [item for item in self.pdf_set if not item.on_scene]
        flagged = [item for item in built if self._pdf_set_issue_lines(item)]
        parts = [f"Набор PDF: построено {len(built)} из {total}"]
        if flagged:
            parts.append(f"с замечаниями: {len(flagged)}")
        if failed:
            names = ", ".join(item.path.name for item in failed[:3]) + ("…" if len(failed) > 3 else "")
            parts.append(f"не разобрано: {len(failed)} ({names})")
        rep = self._neighbor_report
        verify_bad = rep is not None and bool(rep.pair_reports) and rep.has_issues
        if rep is not None and rep.pair_reports:
            parts.append(rep.headline())
        elif built and not any(item.is_main for item in self.pdf_set) and self._preview_main_plan is None:
            parts.append("для сверки добавьте наш участок")
        if analysis is not None and analysis.proposals:
            parts.append(f"💡 находок по данным соседей: {len(analysis.proposals)} — см. итог")
        hard = (failed and not built) or (rep is not None and bool(rep.pair_reports) and rep.overall_status == "error")
        self._set_status(
            "  |  ".join(parts),
            error=bool(hard),
            warn=bool(not hard and (failed or flagged or verify_bad)),
        )
        self._pdf_set_report_ready = True
        report = self._pdf_set_report_text()
        for line in report.splitlines():
            self._log_line(line, level=log_mode.report_line_level(line))
        self._refresh_pdf_selection_ui()
        self._refresh_parse_actions()
        # Окно итога — только когда нужно решение пользователя (не строится файл, есть ошибка
        # контура, расхождение с соседями или находки). Всё хорошо или почти хорошо — журнал и
        # строка состояния; окно открывается кнопкой «📋 Итог разбора».
        error_items = [
            item for item in built
            if any(severity == "error" for severity, _ in self._pdf_set_issue_lines(item))
        ]
        needs_attention = bool(hard or failed or error_items or verify_bad or (analysis is not None and analysis.proposals))
        if needs_attention:
            self._open_pdf_set_report_window()
        else:
            self._close_pdf_set_report_window()

    def _pdf_set_summary_text(self) -> str:
        """Короткий список набора для левой панели: роль, файл, страницы, состояние."""
        lines: list[str] = []
        ordered = sorted(self.pdf_set, key=lambda item: not item.is_main)
        for item in ordered[:6]:
            if item.error:
                mark = "✗"
            elif not item.on_scene:
                mark = "…"
            else:
                mark = "⚠" if self._pdf_set_issue_lines(item) else "✓"
            role = "наш" if item.is_main else "сосед"
            if item.llm_used or (item.use_llm and item.outcome is None):
                mode = "ИИ"
            elif item.ocr_used:
                mode = "локально, OCR"
            else:
                mode = "локально"
            lines.append(
                f"{mark} {role}: {item.path.name}\n     {item.pages_label()} · {mode}"
            )
        if len(ordered) > 6:
            lines.append(f"… и ещё {len(ordered) - 6} (список — «☰ Роли и страницы»)")
        if not any(item.is_main for item in self.pdf_set):
            lines.append("Нашего участка в наборе нет — все файлы соседи.")
        return "\n".join(lines)

    def _pdf_set_report_text(self) -> str:
        """Итог разбора набора: ошибки по каждому участку и нашего участка относительно соседей."""
        items = sorted(self.pdf_set, key=lambda item: not item.is_main)
        built = [item for item in items if item.on_scene]
        issue_map = {id(item): self._pdf_set_issue_lines(item) for item in items}
        flagged = [item for item in built if issue_map[id(item)]]
        failed = [item for item in items if not item.on_scene]
        lines = [
            "ИТОГ РАЗБОРА НАБОРА PDF",
            f"Файлов: {len(items)} · построено: {len(built)} · с замечаниями: {len(flagged)} "
            f"· не разобрано: {len(failed)}",
        ]
        llm_count = sum(1 for item in items if item.llm_used)
        lines.append(f"ИИ (по факту): файлов {llm_count} · локально: {len(items) - llm_count}")
        by_ocr = [item for item in items if item.ocr_used]
        if by_ocr:
            # Скан всегда читается OCR, но ИИ подключается, если проверка не пройдена:
            # «бесплатно» пишем только про тех, кто обошёлся без ИИ.
            ocr_ai = [item for item in by_ocr if item.llm_used]
            ocr_free = [item for item in by_ocr if not item.llm_used]
            line = f"Сканов через OCR: {len(by_ocr)} · без ИИ: {len(ocr_free)}"
            if ocr_ai:
                line += f" · с ИИ: {', '.join(item.path.name for item in ocr_ai[:4])}"
                line += "…" if len(ocr_ai) > 4 else ""
            lines.append(line)
        for item in items:
            plan = self._pdf_set_plan_for(item)
            if item.is_main:
                role = "НАШ УЧАСТОК"
            elif plan is not None:
                number = next(
                    (index for index, known in enumerate(self._preview_neighbor_plans, 1) if known is plan), 0,
                )
                role = f"СОСЕД #{number}" if number else "СОСЕД"
            else:
                role = "СОСЕД (не построен)"
            lines.append("")
            lines.append(f"▸ {role} — {item.path.name}")
            lines.append(f"   {item.pages_label()} · {item.mode_label()}")
            if item.ocr_used and not item.llm_used:
                lines.append(
                    "   🔍 текст страниц получен локальным OCR — платный запрос ИИ не делался"
                )
            if plan is None:
                lines.append(f"   ✗ не разобран: {' '.join((item.error or 'участок не построен').split())}")
                continue
            desc = plan.description
            facts = (
                f"сторон {len(desc.segments)}, периметр {desc.perimeter_ft:,.2f} ft, "
                f"невязка {plan.closure.distance:.3f} ft"
            )
            issues = issue_map[id(item)]
            if not issues:
                lines.append(f"   ✓ построен без замечаний: {facts}")
            else:
                lines.append(f"   ⚠ построен с замечаниями ({len(issues)}): {facts}")
                for severity, text in issues:
                    lines.append(f"      {'⛔' if severity == 'error' else '⚠'} {text}")
            for fix in item.fixes:
                lines.append(f"   ✎ принята правка по данным соседей: {fix.title}")

        lines.append("")
        lines.append("НАШ УЧАСТОК ОТНОСИТЕЛЬНО СОСЕДЕЙ")
        rep = self._neighbor_report
        if rep is not None and rep.pair_reports:
            lines.append(rep.format_report(compact=False))
        elif self._preview_main_plan is None:
            if any(item.is_main for item in items):
                lines.append("Сверка невозможна: наш участок не разобран.")
            else:
                lines.append("В наборе нет нашего участка — сверять соседей пока не с чем.")
        else:
            lines.append("Соседние участки не построены — сверять не с чем.")
        analysis = self._pdf_set_analysis
        if analysis is not None and analysis.has_content:
            lines.append("")
            lines.append("АНАЛИЗ КОНФЛИКТОВ: ЧТО МОЖНО ВЗЯТЬ ИЗ ОПИСАНИЙ СОСЕДЕЙ")
            lines.append(analysis.format_report())
        if failed:
            lines.append("")
            lines.append(
                "В AutoCAD уйдёт только полный набор. Откройте «☰ Роли и страницы»: исправьте страницы "
                "или способ разбора у неразобранных файлов либо уберите их из набора — "
                "готовые участки заново не запрашиваются."
            )
        return "\n".join(lines)

    def _pdf_set_item_by_key(self, key: str) -> PdfSetItem | None:
        return next((item for item in self.pdf_set if str(item.path) == key), None)

    def _apply_pdf_set_fix(self, proposal_id: str) -> None:
        """
        Принимает находку: участок перестраивается с данными соседа.

        Разбор файла не трогается и заново не запрашивается — правка хранится
        рядом с ним и снимается кнопкой «Вернуть как в документе».
        """
        analysis = self._pdf_set_analysis
        if self._busy or analysis is None:
            return
        chain = analysis.with_requirements(proposal_id)
        if not chain:
            return
        self._accept_pdf_set_fixes(chain)

    def _apply_all_pdf_set_fixes(self) -> None:
        """Принимает все находки, подтверждённые геометрией."""
        analysis = self._pdf_set_analysis
        if self._busy or analysis is None:
            return
        chain: list[Any] = []
        for proposal in analysis.proposals:
            if proposal.verified:
                chain.extend(analysis.with_requirements(proposal.id))
        self._accept_pdf_set_fixes(chain)

    def _accept_pdf_set_fixes(self, proposals: list[Any]) -> None:
        if self.pending_conflict is not None or self.pending_doubt is not None:
            self._set_status("Сначала разрешите конфликт или сомнение в результате разбора.", error=True)
            return
        accepted: list[str] = []
        for proposal in proposals:
            item = self._pdf_set_item_by_key(proposal.lot_key)
            if item is None or not item.has_fresh_result():
                continue
            taken = {change.slot for fix in item.fixes for change in fix.changes}
            if any(fix.id == proposal.id for fix in item.fixes) or any(c.slot in taken for c in proposal.changes):
                # Та же сторона уже исправлена другой находкой.
                continue
            item.fixes = list(item.fixes) + [proposal]
            accepted.append(f"«{item.path.name}»: {proposal.title}")
        if not accepted:
            self._set_status("Правка не применена: участок изменился или эта сторона уже исправлена.", warn=True)
            return
        for text in accepted:
            self._log_line(f"✎ Принята правка по данным соседей — {text}")
        # Участки перестраиваются из готовых результатов: платных запросов нет.
        self._start_pdf_set()

    def _revert_pdf_set_fix(self, lot_key: str, proposal_id: str) -> None:
        """Снимает принятую правку: участок снова строится как в документе."""
        item = self._pdf_set_item_by_key(lot_key)
        if self._busy or item is None:
            return
        kept = [fix for fix in item.fixes if fix.id != proposal_id]
        if len(kept) == len(item.fixes):
            return
        dropped = next(fix for fix in item.fixes if fix.id == proposal_id)
        item.fixes = kept
        self._log_line(f"↩ Правка снята — «{item.path.name}»: {dropped.title}")
        self._start_pdf_set()

    def _ask_pdf_set_join_advisor(self) -> None:
        """Платный вопрос советнику по карте: соединяет фигуры (угол к углу, общая межа)."""
        if self._busy or not self.pdf_set:
            return
        from tkinter import messagebox

        tax = f"карту «{self._pdf_set_tax_map[0]}»" if self._pdf_set_tax_map else "карту не приложили"
        mode = "перенос и поворот" if self._pdf_set_join_mode == "turn" else "только перенос"
        if not messagebox.askyesno(
            "ИИ-советник по карте",
            "Отправить нейросети схему фигур, страницы документов и " + tax + "?\n\n"
            "Это платный запрос. Советник только соединяет фигуры: угол нашего участка с углом соседа "
            "и общую межу. Длины и румбы не меняются. Режим: " + mode + ". "
            "Соединение, которое геометрия подтвердит, применяется сразу.",
        ):
            return
        self._start_pdf_set(ask_advisor=True, join_mode=self._pdf_set_join_mode)

    def _pick_pdf_set_tax_map(self) -> None:
        """Приложить карту (TAX_MAP) к запросу советнику по карте."""
        from tkinter import filedialog, messagebox

        path = filedialog.askopenfilename(
            title="Карта участка (TAX_MAP)",
            filetypes=[("Карта", "*.jpg *.jpeg *.png"), ("Все файлы", "*.*")],
        )
        if not path:
            return
        try:
            raw = Path(path).read_bytes()
        except OSError as exc:
            messagebox.showerror("Карта", f"Не удалось прочитать файл: {exc}")
            return
        mime = "image/png" if Path(path).suffix.lower() == ".png" else "image/jpeg"
        data_url = f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
        self._pdf_set_tax_map = (Path(path).name, data_url)
        if self._pdf_set_tax_button is not None:
            self._pdf_set_tax_button.configure(text=f"📎 {Path(path).name}")
        self._set_status(f"Карта «{Path(path).name}» приложена к запросу советнику по карте.")

    def _set_pdf_set_join_mode(self, label: str) -> None:
        self._pdf_set_join_mode = "shift" if label.startswith("только") else "turn"

    def _ask_pdf_set_advisor(self) -> None:
        """Платный вопрос ИИ-советнику о расхождениях набора (по кнопке — всегда заново)."""
        if self._busy or not self.pdf_set:
            return
        from tkinter import messagebox

        if not messagebox.askyesno(
            "ИИ-советник",
            "Отправить нейросети данные участков набора, их тексты или страницы и акт сверки?\n\n"
            "Это платный запрос. Советник объяснит расхождения и предложит правки; каждая "
            "проверяется геометрией и применяется только после вашего подтверждения.",
        ):
            return
        self._start_pdf_set(ask_advisor=True)

    def _close_pdf_set_report_window(self) -> None:
        """Итог всегда один: прежнее окно закрывается (и при новом разборе без вопросов тоже)."""
        previous = self._pdf_set_report_window
        if previous is None:
            return
        try:
            previous.destroy()
        except Exception:  # noqa: BLE001
            pass
        if previous in self._session_windows:
            self._session_windows.remove(previous)
        self._pdf_set_report_window = None

    def _open_pdf_set_report_window(self) -> None:
        """Окно итога: что построено, замечания по каждому участку и сверка с соседями."""
        if not self.pdf_set or not self._pdf_set_report_ready:
            return
        self._close_pdf_set_report_window()
        full_text = "\n".join(
            log_mode.filter_report_lines(self._pdf_set_report_text().splitlines(), self._log_mode()),
        )
        built = [item for item in self.pdf_set if item.on_scene]
        failed = [item for item in self.pdf_set if not item.on_scene]
        flagged = [item for item in built if self._pdf_set_issue_lines(item)]
        rep = self._neighbor_report
        verify_status = rep.overall_status if rep is not None and rep.pair_reports else "ok"
        if (failed and not built) or verify_status == "error":
            head_color = THEME["err"]
        elif failed or flagged or verify_status == "warn":
            head_color = THEME["warn"]
        else:
            head_color = THEME["ok"]
        headline = f"Построено участков: {len(built)} из {len(self.pdf_set)}"
        if flagged:
            headline += f" · с замечаниями: {len(flagged)}"
        if failed:
            headline += f" · не разобрано: {len(failed)}"
        analysis = self._pdf_set_analysis
        if analysis is not None and analysis.headline():
            headline += f"\n💡 {analysis.headline()}"

        win = ctk.CTkToplevel(self)
        self._session_windows.append(win)
        win.title("Итог разбора набора PDF — Title Survey AI")
        win.geometry("1000x760" if analysis is not None and (analysis.proposals or analysis.applied) else "1000x640")
        win.configure(fg_color=THEME["bg_root"])
        win.attributes("-topmost", bool(self.topmost_var.get()))
        self._attach_child_window(win)
        win.grid_columnconfigure(0, weight=1)
        win.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(
            win, text=headline, font=ctk.CTkFont(size=14, weight="bold"),
            text_color=head_color, anchor="w", justify="left",
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 8))
        report_box = ctk.CTkTextbox(
            win, wrap="word", fg_color=THEME["bg_editor"], text_color=THEME["text_main"],
            font=ctk.CTkFont(family="Consolas", size=12),
        )
        report_box.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 8))
        report_box.insert("1.0", full_text)
        target = getattr(report_box, "_textbox", report_box)
        target.configure(state="disabled")
        win._report_text = full_text
        win._report_headline = headline

        # Находки по данным соседей: данные участка меняются только кнопкой.
        win._fix_buttons = {}
        win._revert_buttons = {}
        fix_rows: list[tuple[str, Any]] = []
        if analysis is not None:
            fix_rows += [("apply", proposal) for proposal in analysis.proposals]
            fix_rows += [("revert", fix) for fix in analysis.applied]
        if fix_rows:
            panel = ctk.CTkScrollableFrame(
                win, fg_color=THEME["bg_card"], corner_radius=8, height=min(190, 58 * len(fix_rows)),
                label_text="Находки по данным соседей — участок меняется только после «Применить»",
                label_font=ctk.CTkFont(size=12, weight="bold"), label_text_color=THEME["sky"],
            )
            panel.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 8))
            panel.grid_columnconfigure(0, weight=1)
            for row_index, (kind, proposal) in enumerate(fix_rows):
                if kind == "apply":
                    mark = "✔" if proposal.verified else "◐"
                    text = (
                        f"[{analysis.number(proposal)}] {mark} «{proposal.lot_name}»: {proposal.title}\n"
                        f"     {proposal.verdict_label()}: {proposal.metrics_line()}"
                    )
                    color = THEME["ok"] if proposal.verified else THEME["warn"]
                else:
                    text = f"✎ принято — «{proposal.lot_name}»: {proposal.title}"
                    color = THEME["text_muted"]
                ctk.CTkLabel(
                    panel, text=text, anchor="w", justify="left", wraplength=640,
                    text_color=color, font=ctk.CTkFont(size=12),
                ).grid(row=row_index, column=0, sticky="ew", padx=(8, 8), pady=3)
                if kind == "apply":
                    button = ctk.CTkButton(
                        panel, text="Применить", width=150, height=28,
                        fg_color=THEME["btn_emerald"], hover_color=THEME["btn_emerald_hover"],
                        command=lambda pid=proposal.id: self._apply_pdf_set_fix(pid),
                    )
                    win._fix_buttons[proposal.id] = button
                else:
                    button = ctk.CTkButton(
                        panel, text="Вернуть как в документе", width=190, height=28,
                        fg_color=THEME["btn_secondary"], hover_color=THEME["btn_secondary_hover"],
                        command=lambda key=proposal.lot_key, pid=proposal.id: self._revert_pdf_set_fix(key, pid),
                    )
                    win._revert_buttons[proposal.id] = button
                button.grid(row=row_index, column=1, padx=(0, 8), pady=3)

        bar = ctk.CTkFrame(win, fg_color="transparent")
        bar.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 12))
        bar.grid_columnconfigure(0, weight=1)
        left = ctk.CTkFrame(bar, fg_color="transparent")
        left.grid(row=0, column=0, sticky="w")
        win._advisor_button = ctk.CTkButton(
            left, text="🧠 Спросить ИИ-советника", width=200, height=30,
            fg_color=THEME["btn_amber"], hover_color=THEME["btn_amber_hover"],
            command=self._ask_pdf_set_advisor,
        )
        win._advisor_button.grid(row=0, column=0, padx=(0, 8))
        win._join_advisor_button = ctk.CTkButton(
            left, text="🗺 Спросить советника по карте", width=230, height=30,
            fg_color=THEME["btn_amber"], hover_color=THEME["btn_amber_hover"],
            command=self._ask_pdf_set_join_advisor,
        )
        win._join_advisor_button.grid(row=0, column=1, padx=(0, 8))
        self._pdf_set_tax_button = ctk.CTkButton(
            left, text=(f"📎 {self._pdf_set_tax_map[0]}" if self._pdf_set_tax_map else "📎 Карта TAX_MAP…"),
            width=170, height=30,
            fg_color=THEME["btn_secondary"], hover_color=THEME["btn_secondary_hover"],
            command=self._pick_pdf_set_tax_map,
        )
        self._pdf_set_tax_button.grid(row=0, column=2, padx=(0, 8))
        win._join_mode_menu = ctk.CTkOptionMenu(
            left, values=["перенос и поворот", "только перенос"], width=170, height=30,
            command=self._set_pdf_set_join_mode,
        )
        win._join_mode_menu.set("перенос и поворот" if self._pdf_set_join_mode == "turn" else "только перенос")
        win._join_mode_menu.grid(row=0, column=3, padx=(0, 8))
        verified = [p for p in analysis.proposals if p.verified] if analysis is not None else []
        win._apply_all_button = None
        if len(verified) > 1:
            win._apply_all_button = ctk.CTkButton(
                left, text=f"Применить все подтверждённые ({len(verified)})", width=240, height=30,
                fg_color=THEME["btn_emerald"], hover_color=THEME["btn_emerald_hover"],
                command=self._apply_all_pdf_set_fixes,
            )
            win._apply_all_button.grid(row=0, column=4, padx=(0, 8))

        def _copy_report() -> None:
            self.clipboard_clear()
            self.clipboard_append(full_text)
            self._set_status("Итог разбора набора скопирован в буфер обмена.")

        ctk.CTkButton(
            bar, text="☰ Роли и страницы", width=170, height=30,
            fg_color=THEME["btn_secondary"], hover_color=THEME["btn_secondary_hover"],
            command=lambda: (win.destroy(), self._edit_pdf_set()),
        ).grid(row=0, column=1, padx=(0, 8))
        ctk.CTkButton(
            bar, text="📋 Скопировать итог", width=170, height=30,
            fg_color=THEME["btn_secondary"], hover_color=THEME["btn_secondary_hover"],
            command=_copy_report,
        ).grid(row=0, column=2, padx=(0, 8))
        ctk.CTkButton(
            bar, text="Закрыть", width=110, height=30,
            fg_color=THEME["btn_primary"], hover_color=THEME["btn_primary_hover"],
            command=win.destroy,
        ).grid(row=0, column=3)
        self._pdf_set_report_window = win
        try:
            win.lift()
            win.focus_set()
        except Exception:  # noqa: BLE001
            pass

    def _attach_neighbor_pdfs(self, paths: list[Path]) -> None:
        """Только прикрепляет PDF; API, расчёт и AutoCAD запускаются отдельно."""
        valid: list[Path] = []
        errors: list[str] = []
        failed_paths: set[Path] = set()
        legal_count = 0
        for path in dict.fromkeys(paths):
            try:
                if path.suffix.lower() != ".pdf":
                    raise ValueError("ожидается PDF")
                info = inspect_pdf(path)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{path.name}: {exc}")
                failed_paths.add(path)
                continue
            valid.append(path)
            if info.get("has_legal_text", info.get("has_text", False)):
                legal_count += 1

        # Если не открылся источник уже показанного плана, он остаётся на
        # экране, но не может считаться успешно обновлённым при смене вкладки.
        self._pending_neighbor_files.update(
            path for path in failed_paths if not valid or path in self._neighbor_plan_sources
        )
        if not valid:
            if errors:
                self._set_status(
                    f"Не удалось прикрепить PDF: {errors[0]}. "
                    "Повторите выбор или очистите соседей перед отрисовкой.", error=True,
                )
            return
        # Новым выбором можно отказаться от неразобранного PDF, которого нет на
        # схеме. Неудачное обновление уже показанного плана остаётся заблокированным.
        abandoned = {
            path for path in self.neighbor_pdf_paths
            if path not in valid and path not in self._neighbor_plan_sources
        }
        self._pending_neighbor_files.difference_update(abandoned)
        self._reset_visual_sources()
        self._visual_source_role = "neighbor"
        self.text_box.delete("1.0", "end")
        self._refresh_text_counter()
        self.neighbor_pdf_paths = valid
        self._pending_neighbor_pdfs = set(valid) | failed_paths
        # В смешанном пакете сначала предлагаем бесплатный режим; сканы требуют
        # явного переключения на Vision, а не платного запроса без ведома пользователя.
        self.use_llm_var.set(legal_count == 0)
        self._refresh_consensus_rows()
        self.source_mode_var.set("📄 PDF")
        self.source_mode_switch.set("📄 PDF")
        self._on_source_mode_changed()
        self._refresh_pdf_selection_ui()
        self.pdf_label.configure(
            text=f"PDF соседей: {len(valid)}",
            text_color=THEME["ok"] if legal_count == len(valid) else THEME["warn"],
        )
        if legal_count:
            mode = f"текст обхода найден в {legal_count} из {len(valid)}; выбран бесплатный разбор"
            if legal_count < len(valid):
                mode += "; файлы без текста потребуют ИИ/Vision"
        else:
            mode = "текст обхода не найден; выбран ИИ/Vision"
        for path in valid:
            self._log_line(f"Прикреплён сосед: {path.name}")
        for error in errors:
            self._log_line(f"! Не прикреплён: {error}")
        extra = (
            f" Не удалось открыть: {len(errors)} (см. журнал). "
            "Для отрисовки исправьте выбор PDF и исключите недоступные файлы."
            if errors else ""
        )
        self._set_status(
            f"Прикреплено PDF соседей: {len(valid)} ({mode}). "
            f"Разбор ещё не запускался.{extra}",
            warn=bool(errors or legal_count < len(valid)),
        )

    def _open_single_pdf(self, chosen: Path) -> None:
        try:
            info = inspect_pdf(chosen)
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"Не удалось прочитать PDF: {exc}", error=True)
            return
        self._reset_visual_sources()
        self._visual_source_role = "main"
        self._main_analysis_pending = True
        self.pdf_path = chosen
        self._pdf_total_pages = int(info.get("pages") or 1)
        has_legal_text = bool(info.get("has_legal_text", info["has_text"]))
        step0_text_path: Path | None = None
        step0_text = ""
        if not has_legal_text:
            base_stem = re.sub(r"_p\d+(?:[-,]\d+)*$", "", chosen.stem)
            for candidate_stem in dict.fromkeys((chosen.stem, base_stem)):
                candidate = chosen.with_name(f"{candidate_stem}_text.txt")
                if candidate.is_file():
                    try:
                        content = candidate.read_text(encoding="utf-8-sig", errors="replace").strip()
                    except OSError:
                        content = ""
                    if content:
                        step0_text_path = candidate
                        step0_text = content
                        break
        if has_legal_text or step0_text:
            text = str(info["text"]) if has_legal_text else step0_text
            self._pdf_needs_vision = False
            self.text_box.delete("1.0", "end")
            self.text_box.insert("1.0", text)
            self._loaded_pdf_text = str(self.text_box.get("1.0", "end") or "").strip()
            self._text_source_label = (
                f"текст из PDF: {chosen.name}"
                if has_legal_text
                else f"текст Шага 0: {step0_text_path.name}"
            )
            self._text_source_snapshot = self._loaded_pdf_text
            self._refresh_text_counter()
            # Если извлечён распознаваемый текст обхода, не отправляем его в ИИ по умолчанию.
            self.use_llm_var.set(False)
            self._refresh_consensus_rows()
            self.source_mode_var.set("📄 PDF")
            try:
                self.source_mode_switch.set("📄 PDF")
            except Exception:  # noqa: BLE001
                pass
            self._on_source_mode_changed()
            self._refresh_visual_bar()
            self.pdf_label.configure(
                text=f"PDF: {self.pdf_path.name} ({len(text):,} симв.)",
                text_color=THEME["ok"],
            )
            if has_legal_text:
                self._set_status(
                    f"PDF загружен: {self.pdf_path.name}. Найден текст обхода — выбран бесплатный "
                    "локальный разбор; результат нужно сверить с документом."
                )
            else:
                self._set_status(
                    f"PDF загружен: {self.pdf_path.name}. Подхвачен текст с Шага 0 ({step0_text_path.name}) — "
                    "выбран бесплатный локальный разбор; при ошибке можно перераспознать на 400 dpi или через ИИ."
                )
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
            self.source_mode_var.set("📄 PDF")
            try:
                self.source_mode_switch.set("📄 PDF")
            except Exception:  # noqa: BLE001
                pass
            self._on_source_mode_changed()
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
        self._session_windows.append(window)
        window.title("Журнал операций — Title Survey AI")
        window.geometry("820x560")
        window.configure(fg_color=THEME["bg_root"])
        window.attributes("-topmost", bool(self.topmost_var.get()))
        self._attach_child_window(window)
        text = ctk.CTkTextbox(
            window,
            wrap="word",
            fg_color=THEME["bg_editor"],
            text_color=THEME["text_main"],
            font=ctk.CTkFont(family="Consolas", size=12),
        )
        text.pack(fill="both", expand=True, padx=12, pady=12)
        text.insert("1.0", self._log_view_text() or "Лог пуст.")
        text.insert("end", "\n\n--- Исходное описание ---\n" + self.text_box.get("1.0", "end"))
        target = getattr(text, "_textbox", text)
        target.configure(state="disabled")

    def log_text(self) -> str:
        """Полный журнал операций (для сохранения); на экране — ``_log_view_text``."""
        return "\n".join(text for _level, text in getattr(self, "_log", []))

    def _log_mode(self) -> str:
        var = getattr(self, "log_mode_var", None)
        if var is None:
            return log_mode.DEFAULT_MODE
        return log_mode.FULL if bool(var.get()) else log_mode.SIMPLE

    def _log_view_text(self) -> str:
        """Журнал операций в текущем режиме (без служебных уровней)."""
        return "\n".join(log_mode.filter_entries(getattr(self, "_log", []), self._log_mode()))

    def _open_maximized(self) -> None:
        """Разворачивает окно на весь экран при запуске. Сбой не мешает работе — окно остаётся обычным."""
        try:
            self.state("zoomed")
        except Exception:  # noqa: BLE001 - платформа без zoomed (например, Linux без WM)
            try:
                self.attributes("-zoomed", True)
            except Exception:  # noqa: BLE001
                pass

    def _on_start_source_changed(self, _label: str = "") -> None:
        """Настройка «стартовая вкладка»: пишет ui.start_source в config.json.

        Текущую вкладку Шага 1 не меняет — только то, что откроется при следующем запуске.
        """
        key = start_source.key_for_label(self.start_source_var.get())
        try:
            start_source.save_start_source_to_file(CONFIG_PATH, key)
        except (OSError, ValueError) as exc:
            self._set_status(f"Стартовая вкладка не сохранена в config.json: {exc}", warn=True)
            return
        if isinstance(self.config_data, dict):
            self.config_data.setdefault("ui", {})["start_source"] = key
        self._set_status(f"Стартовая вкладка: {start_source.label_for(key)} (со следующего запуска)")

    def _apply_log_mode(self) -> None:
        """Переключатель «Подробный журнал»: сохраняет режим и перерисовывает все журналы."""
        mode = self._log_mode()
        try:
            log_mode.save_mode_to_file(CONFIG_PATH, mode)
        except (OSError, ValueError) as exc:
            self._set_status(f"Режим журнала не сохранён в config.json: {exc}", warn=True)
        if isinstance(self.config_data, dict):
            self.config_data.setdefault("ui", {})["log_mode"] = mode
        self._render_step0_log()
        self._refresh_log_view()
        content = str(getattr(self, "_step2_report_text", "") or "")
        if content:
            self._set_step2_report(content)

    def _log_line(self, message: str, level: str = log_mode.LEVEL_INFO) -> None:
        if not hasattr(self, "_log"):
            self._log = []
        stamp = __import__("datetime").datetime.now().strftime("%H:%M:%S")
        self._log.append((level, f"{stamp}  {message}"))
        del self._log[:-400]
        if hasattr(self, "result_tab_var") and str(self.result_tab_var.get()) == "🧾 Журнал":
            self._refresh_log_view()

    def _set_status(
        self, message: str, error: bool = False, *, warn: bool = False, key: bool = False,
    ) -> None:
        self.status_var.set(message)
        if error:
            color = THEME["err"]
        elif warn:
            color = THEME["warn"]
        else:
            color = THEME["text_main"]
        self.status_label.configure(text_color=color)
        dot = getattr(self, "status_dot", None)
        if dot is not None:
            dot.configure(
                text_color=THEME["err"] if error else (THEME["warn"] if warn else THEME["ok"])
            )
        level = (
            log_mode.LEVEL_ERROR if error else
            log_mode.LEVEL_WARN if warn else
            log_mode.LEVEL_KEY if key else
            log_mode.LEVEL_INFO
        )
        self._log_line(message, level=level)
        self._refresh_stepper()

    def _refresh_center_badges(self, plan: DrawPlan | None, closure_ft: float | None) -> None:
        """Обновляет бейджи консенсуса и невязки в шапке центрального холста."""
        if not hasattr(self, "center_closure_badge"):
            return
        cons = getattr(self, "_last_consensus_label", None)
        if cons and getattr(self, "consensus_badge", None) is not None:
            self.consensus_badge.configure(text=f"✨ Консенсус {cons}")
            self.consensus_badge.grid(row=0, column=2, padx=(0, 6), sticky="e")
        elif getattr(self, "consensus_badge", None) is not None:
            self.consensus_badge.grid_remove()
        if closure_ft is None:
            self.center_closure_badge.grid_remove()
            return
        ok = closure_ft <= 0.01
        self.center_closure_badge.configure(
            text=f"Closure {closure_ft:.3f} ft",
            fg_color=THEME["badge_v1_bg"] if ok else THEME["warn_bg"],
            text_color=THEME["ok"] if ok else THEME["warn"],
        )
        self.center_closure_badge.grid(row=0, column=3, padx=(0, 6), sticky="e")

    def _update_closure_badge(self, text: str, distance_ft: float | None) -> None:
        self.closure_var.set(text)
        if distance_ft is None:
            color = THEME["text_muted"]
            if hasattr(self, "stat_closure_var"):
                self.stat_closure_var.set("—")
                self._refresh_center_badges(None, None)
        else:
            if distance_ft <= 0.015:
                color = THEME["ok"]
            elif distance_ft <= 1.0:
                color = THEME["warn"]
            else:
                color = THEME["err"]
            if hasattr(self, "stat_closure_var"):
                self.stat_closure_var.set(f"{distance_ft:.3f} ft")
                self._refresh_center_badges(self._last_active_plan, distance_ft)
        if hasattr(self, "closure_label") and self.closure_label is not None:
            self.closure_label.configure(text_color=color)

    # ------------------------------------------------------- асинхронные шаги --
    def _start_background(self, target: Any, args: tuple[Any, ...] = (),
                          kwargs: dict[str, Any] | None = None) -> None:
        """Запускает задание с неизменным номером поколения текущей сессии."""
        generation = self._generation

        def run() -> None:
            self._worker_context.generation = generation
            try:
                target(*args, **(kwargs or {}))
            finally:
                del self._worker_context.generation

        threading.Thread(target=run, daemon=True).start()

    def _worker_is_current(self) -> bool:
        generation = getattr(self._worker_context, "generation", self._generation)
        return not self._closing and generation == self._generation

    def _post(self, kind: str, payload: Any, *, generation: int | None = None) -> None:
        if generation is None:
            generation = getattr(self._worker_context, "generation", self._generation)
        if not self._closing and generation == self._generation:
            self._events.put((generation, kind, payload))

    def _worker_poster(self) -> Any:
        # Привязать поколение здесь: callbacks консенсуса могут работать в других потоках.
        generation = getattr(self._worker_context, "generation", self._generation)
        return lambda kind, payload: self._post(kind, payload, generation=generation)

    def _draw_if_current(self, builder: AcadBuilder, plan: DrawPlan) -> DrawResult | None:
        # Сброс не прерывает уже выполняющийся COM-вызов, но новое построение
        # не начнётся параллельно с ним или после отмены старого задания.
        with self._draw_lock:
            if not self._worker_is_current():
                return None
            return builder.draw_plan(plan)

    def _drain_events(self) -> None:
        """Tkinter-поток: забирает сообщения из рабочего потока."""
        if getattr(self, "_closing", False):
            return
        try:
            while not self._events.empty():
                try:
                    generation, kind, payload = self._events.get_nowait()
                except queue.Empty:
                    break
                if generation != self._generation:
                    continue
                try:
                    self._handle_event(kind, payload)
                except Exception as exc:  # noqa: BLE001 - одно событие не останавливает очередь
                    self._report_event_error(kind, exc)
        finally:
            # Опрос событий перезапускается всегда, даже если обработчик упал.
            self.after(120, self._drain_events)

    def _report_event_error(self, kind: str, exc: BaseException) -> None:
        """Сообщает об ошибке обработчика события, не останавливая программу."""
        traceback.print_exc()
        text = f"Внутренняя ошибка ({kind}): {type(exc).__name__}: {exc}. Подробности — в консоли."
        try:
            self._set_status(text, error=True)
        except Exception:  # noqa: BLE001 - окно могло уже закрываться
            pass

    def _handle_event(self, kind: str, payload: Any) -> None:
        """Обработка одного сообщения из рабочего потока (тело прежнего цикла)."""
        if kind == "valid_secret":
            self._last_valid_secret = str(payload)
        elif kind == "round_usages":
            self._record_round_usages(payload)
            if self._session_llm_calls:
                total = self._session_prompt_tokens + self._session_completion_tokens
                self.tokens_var.set(
                    f"Сессия ({self._session_llm_calls} выз.): "
                    f"↑ {self._session_prompt_tokens:,} / ↓ {self._session_completion_tokens:,} "
                    f"(всего {total:,})"
                )
        elif kind == "consensus_label":
            self._last_consensus_label = payload
        elif kind == "reading_summary":
            # Информационная строка: только в подробном журнале (в упрощённом её нет по решению пользователя).
            self._log_line(str(payload), level=log_mode.LEVEL_INFO)
        elif kind == "progress":
            self._set_status(str(payload))
        elif kind == "error":
            self._finish_busy()
            self._set_status(str(payload), error=True)
            self._update_closure_badge("Closure Error: —", None)
        elif kind == "parsed":
            self._dismiss_conflict_ui()
            parsed_plan = None
            if len(payload) == 5:
                outcome, draw, pre_counted, round_usages, parsed_plan = payload
                if round_usages:
                    self._record_round_usages(round_usages)
            elif len(payload) == 4:
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
                    plan = parsed_plan or AcadBuilder(self.config_data).plan(
                        outcome.data,
                        scale=self._current_scale(),
                        is_neighbor=self.is_neighbor_mode(),
                    )
                    self._record_plan_for_preview(
                        plan, warnings=outcome.warnings, source=self._running_source_path,
                        source_label=self._running_source_label, outcome=outcome,
                    )
                    self._mark_running_source_ready(plan)
                    self._append_suspicion_to_status(plan)
                    self._append_neighbor_verification_to_status()
                    self._update_closure_badge(
                        plan.closure.short_report(), plan.closure.distance
                    )
                except Exception:  # noqa: BLE001
                    self._update_closure_badge(self._closure_text(outcome), None)
        elif kind == "scene_drawn":
            completed, error = payload
            self._finish_busy()
            draw_warnings = []
            if completed:
                self.draw_result = completed[-1][1]
                for name, result in completed:
                    self._log_line(f"{name}: {result.summary()}")
                    draw_warnings.extend(f"{name}: {warning}" for warning in result.warnings)
            if error:
                self._set_status(
                    f"Отрисовка остановлена: {error}. Уже начерчено участков: {len(completed)}. "
                    "В текущем участке могли остаться отдельные объекты; повторное нажатие может создать дубликаты.",
                    error=True,
                )
            elif draw_warnings:
                self._set_status(
                    f"Начерчено участков: {len(completed)}, предупреждений AutoCAD: {len(draw_warnings)}. "
                    f"{draw_warnings[0]} (подробности в журнале). Использованы готовые планы без запроса к ИИ.",
                    warn=True,
                )
            else:
                self._set_status(f"Начерчено участков: {len(completed)}. Использованы готовые планы, без запроса к ИИ.")
            self._refresh_stepper()
        elif kind == "conflict":
            self._finish_busy()
            self._present_conflict(payload)
        elif kind == "doubt":
            self._finish_busy()
            self._present_doubt(payload)
        elif kind == "batch_neighbors":
            items_done, batch_errors, pdf_pages_spec = payload
            self._finish_busy()
            self.batch_neighbor_results = items_done
            for source, out, res, pre_counted in items_done:
                self._apply_parsed(out, already_counted=pre_counted)
                self.draw_result = res
                self._record_plan_for_preview(res.plan, warnings=out.warnings, source=source, outcome=out)
                self._pending_neighbor_pdfs.discard(source)
                self._pending_neighbor_files.discard(source)
                self._neighbor_parsed_pages[source] = pdf_pages_spec
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
                warn=bool(batch_errors and items_done),
            )
            for _source, _out, res, _counted in items_done:
                self._append_suspicion_to_status(res.plan)
            self._append_neighbor_verification_to_status()
            if items_done:
                _source, _last_out, last_res, _counted = items_done[-1]
                self._update_closure_badge(
                    last_res.closure.short_report(), last_res.closure.distance
                )
        elif kind == "verify_neighbors_done":
            plans_done, verify_errors, open_popup, pdf_pages_spec = payload
            self._finish_busy()
            for source, out, nb_plan, pre_counted in plans_done:
                self._apply_parsed(out, already_counted=pre_counted)
                self._record_plan_for_preview(nb_plan, warnings=out.warnings, source=source, outcome=out)
                self._pending_neighbor_pdfs.discard(source)
                self._pending_neighbor_files.discard(source)
                self._neighbor_parsed_pages[source] = pdf_pages_spec
            if (self._running_batch_attached and self.neighbor_pdf_paths and not self._pending_neighbor_pdfs
                and all(path in self._neighbor_plan_sources
                        and self._neighbor_parsed_pages.get(path) == pdf_pages_spec
                        for path in self.neighbor_pdf_paths)):
                self._ready_source_signature = self._running_source_signature
                self._input_incomplete = False
            for err_line in verify_errors:
                self._log_line(f"! {err_line}")
            error_note = (
                f" Ошибок: {len(verify_errors)} (см. журнал; {verify_errors[0]})."
                " Повторите разбор проблемных файлов или очистите соседей перед отрисовкой."
                if verify_errors else ""
            )
            unopened = self._pending_neighbor_pdfs.difference(self.neighbor_pdf_paths)
            if self._running_batch_attached and unopened:
                names = ", ".join(path.name for path in sorted(unopened))
                error_note += f" Не удалось открыть PDF: {names}. Исправьте выбор файлов."
            if plans_done:
                last_plan = plans_done[-1][2]
                self._update_closure_badge(last_plan.closure.short_report(), last_plan.closure.distance)
            if plans_done and self._neighbor_report is not None and self._neighbor_report.pair_reports:
                rep = self._neighbor_report
                self._set_status(
                    f"Разобрано соседей: {len(plans_done)}. {rep.headline()}{error_note}",
                    error=(rep.overall_status == "error"),
                    warn=(bool(error_note) or rep.overall_status == "warn"),
                )
                if rep.has_issues:
                    self._append_neighbor_verification_to_status()
                if self._preview_main_plan is not None:
                    self._set_inspector_text(
                        self._build_inspector_report(self._preview_main_plan)
                    )
                if open_popup:
                    self._open_neighbor_report_window()
            elif plans_done:
                self._set_status(
                    f"Разобрано соседей: {len(plans_done)}. "
                    f"Для сверки разберите основной участок.{error_note}",
                    warn=bool(error_note),
                )
            elif verify_errors:
                self._set_status(
                    f"Не удалось разобрать файлы соседей ({len(verify_errors)} ошиб.): {verify_errors[0]}",
                    error=True,
                )
        elif kind == "step0_progress":
            text = str(payload)
            if not (text.startswith("[") and len(text) >= 10 and text[9] == "]"):
                text = self._step0_timestamp(text)
            self._step0_lines.append((log_mode.LEVEL_INFO, text))
            self._render_step0_log()
        elif kind == "step0_portal_done":
            status, portal_result = payload
            self._finish_step0_portal_lookup(status, portal_result)
        elif kind == "update_progress":
            text = str(payload)
            self.update_status_label.configure(text=text, text_color=THEME["text_dim"])
            self._set_status(f"Обновление: {text}")
        elif kind == "update_stage":
            name, fraction, detail = payload
            self._show_update_stage(str(name), fraction, str(detail))
        elif kind == "update_done":
            status, update_payload = payload
            self._finish_update_version(status, update_payload)
        elif kind == "step0_done":
            status, payload_done = payload
            self._finish_step0(status, payload_done)
        elif kind == "pdf_set_done":
            self._finish_busy()
            self._handle_pdf_set_done(payload)
        elif kind == "feature_labeled":
            label_result, label_error, label_mode = payload
            self._finish_busy()
            self._handle_feature_label_result(label_result, label_error, label_mode)
        elif kind == "done":
            self._finish_busy()
            pending, self._pending_join_accept = self._pending_join_accept, []
            if pending:
                self._accept_pdf_set_fixes(pending)

    def _set_controls_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.draw_button.configure(state=state)
        self.add_neighbor_button.configure(state=state)
        self.verify_neighbors_button.configure(state=state)
        self.batch_neighbors_button.configure(state=state)
        self.clear_neighbors_button.configure(state=state)
        self.load_pdf_button.configure(state=state)
        self.pdf_set_edit_button.configure(state=state)
        self.pdf_set_report_button.configure(
            state=state if self._pdf_set_report_ready else "disabled"
        )
        self.load_images_button.configure(state=state)
        self.photo_load_button.configure(state=state)
        self.sample_menu.configure(state=state)
        self.main_lot_radio.configure(state=state)
        self.neighbor_lot_radio.configure(state=state)
        self.parse_button.configure(state=state)
        self.regex_link_button.configure(state=state)
        self.run_mode_seg.configure(state=state)
        self.save_button.configure(state=state)
        self.save_env_button.configure(state=state)
        self.feature_auto_button.configure(state=state)
        self.feature_pick_button.configure(state=state)
        self.house_text_entry.configure(state=state)
        self.step2_height_entry.configure(state=state)
        self.skip_existing_labels_check.configure(state=state)
        self.hide_field_notes_check.configure(state=state)

    def _finish_busy(self) -> None:
        self._busy = False
        self.progress.stop()
        self.progress.set(0)
        self._set_controls_busy(False)
        self._refresh_stepper()

    def _open_neighbor_report_window(self) -> None:
        """Открывает отдельное окно подробного акта сверки с соседними участками."""
        rep = self._neighbor_report
        if rep is None or not rep.pair_reports:
            return
        win = ctk.CTkToplevel(self)
        self._session_windows.append(win)
        win.title("Акт геодезической сверки с соседями — Title Survey AI")
        win.geometry("880x620")
        win.configure(fg_color=THEME["bg_root"])
        win.attributes("-topmost", bool(self.topmost_var.get()))
        self._attach_child_window(win)
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
        if self.pending_conflict is not None or self.pending_doubt is not None:
            self._set_status("Сначала разрешите конфликт или сомнение в результате разбора.", error=True)
            return
        # Быстрый локальный анализ допустим только для выбранного текста основного участка.
        if (self._preview_main_plan is None and self._current_source_mode() == "📝 Текст"
            and not self.is_neighbor_mode()):
            raw_txt = self.text_box.get("1.0", "end").strip()
            if len(raw_txt) >= 20:
                try:
                    desc_quick, warns_quick = parse_with_regex(raw_txt)
                    plan_quick = AcadBuilder(self.config_data).plan(
                        desc_quick, scale=self._current_scale(), is_neighbor=False,
                    )
                    self._record_plan_for_preview(
                        plan_quick, warnings=warns_quick, source_label=self._current_text_source_label(raw_txt),
                    )
                    self._ready_source_signature = self._source_signature()
                    self._input_incomplete = False
                    self._main_analysis_pending = False
                except Exception:  # noqa: BLE001
                    pass

        if self._preview_main_plan is None:
            self._set_status(
                "Сначала разберите основной участок, "
                "чтобы было с чем сверять соседние участки.",
                error=True,
            )
            return

        pages = str(self.pdf_pages_var.get() or "").strip() or None
        pending_files = [
            path for path in self.neighbor_pdf_paths
            if (path in self._pending_neighbor_pdfs
                or path not in self._neighbor_plan_sources
                or self._neighbor_parsed_pages.get(path) != pages)
        ] if self._current_source_mode() == "📄 PDF" else []
        if pending_files:
            if self._current_source_mode() == "📄 PDF" and len(self.neighbor_pdf_paths) == 1:
                self._start(parse_only=True, open_neighbor_popup=True)  # одиночный файл допускает ручное разрешение конфликта
            else:
                self._start_neighbor_batch(pending_files, draw=False, open_popup=True, pdf_pages_spec=pages)
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
        """Выбирает файлы для сверки без AutoCAD."""
        self._pick_scene_files(open_popup=True)

    def _pick_scene_files(self, *, open_popup: bool) -> None:
        """Сначала спрашивает роль; затем открывает файлы для выбранного участка."""
        if self._busy or getattr(self, "_source_role_dialog", None) is not None:
            return
        if self.pending_conflict is not None or self.pending_doubt is not None:
            self._set_status("Сначала разрешите конфликт или сомнение в результате разбора.", error=True)
            return
        from tkinter import filedialog

        role = self._ask_source_role("выбранных файлах")
        if role not in {"main", "neighbor"}:
            return
        ext_glob = " ".join(f"*{ext}" for ext in IMAGE_EXTENSIONS)
        title = (
            "Выберите один PDF, фотографии одного основного участка или текстовый файл"
            if role == "main" else "Выберите файлы соседних участков (PDF, фото или TXT)"
        )
        paths = filedialog.askopenfilenames(
            title=title,
            filetypes=[
                ("PDF, фото чертежей и описания", f"*.pdf {ext_glob} *.txt"),
                ("PDF", "*.pdf"),
                ("Фотографии чертежей", ext_glob),
                ("Текст", "*.txt"),
                ("Все файлы", "*.*"),
            ],
        )
        file_list = [Path(p) for p in paths if str(p).strip()]
        if not file_list:
            return
        if role == "main":
            self._apply_main_file_selection(file_list)
            return
        if self._choose_direct_neighbor_mode(file_list):
            if len(file_list) == 1 and file_list[0].suffix.lower() == ".txt":
                # Одиночный TXT использует обычный разбор с ручным разрешением
                # конфликтов, а не пакетный путь, пропускающий конфликтный файл.
                self._pending_neighbor_files.add(file_list[0])
                if self._load_text_file(file_list[0], role="neighbor"):
                    self._start(parse_only=True, open_neighbor_popup=open_popup)
            else:
                self._start_neighbor_batch(file_list, draw=False, open_popup=open_popup)

    def _apply_main_file_selection(self, files: list[Path]) -> None:
        """Основной файл из окна сверки загружается без скрытого разбора или запроса."""
        if all(is_image_file(path) for path in files):
            self._apply_loaded_images(files, role="main")
            return
        if len(files) == 1 and files[0].suffix.lower() == ".pdf":
            self._open_single_pdf(files[0])
            return
        if len(files) == 1 and files[0].suffix.lower() == ".txt":
            self._load_text_file(files[0], role="main")
            return
        self._set_status(
            "Для основного участка выберите один PDF или TXT либо фотографии одного участка, без смешивания типов.",
            error=True,
        )

    def _load_text_file(self, path: Path, *, role: str) -> bool:
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
            if not text.strip():
                raise ValueError("файл пуст")
        except (OSError, ValueError) as exc:
            self._set_status(f"Не удалось прочитать текст «{path.name}»: {exc}", error=True)
            return False
        self._reset_visual_sources()
        if role == "main":
            self._main_analysis_pending = True
            self.use_llm_var.set(False)
        self.text_box.delete("1.0", "end")
        self.text_box.insert("1.0", text)
        self._text_source_label = f"текст: {path.name}"
        self._text_source_snapshot = text.strip()
        self._text_source_path = path
        self._text_source_role = role
        self.lot_role_var.set(role)
        self.source_mode_var.set("📝 Текст")
        self.source_mode_switch.set("📝 Текст")
        self._on_source_mode_changed()
        self._refresh_consensus_rows()
        self._refresh_text_counter()
        self.pdf_label.configure(text=f"Текст: {path.name}", text_color=THEME["ok"])
        role_name = "основного" if role == "main" else "соседнего"
        self._set_status(f"Текст {role_name} участка загружен. Разбор ещё не запускался.")
        return True

    def _choose_direct_neighbor_mode(self, files: list[Path]) -> bool:
        """Сверка из вкладки не должна незаметно отправлять текстовые PDF в API."""
        from tkinter import messagebox

        legal_count = 0
        visual_count = 0
        for path in files:
            if is_image_file(path):
                visual_count += 1
            elif path.suffix.lower() == ".pdf":
                try:
                    info = inspect_pdf(path)
                except Exception:  # отдельная ошибка файла будет выведена при разборе
                    continue
                if info.get("has_legal_text", info.get("has_text", False)):
                    legal_count += 1
                else:
                    visual_count += 1
        use_llm = bool(self.use_llm_var.get())
        if legal_count and (use_llm or visual_count):
            warning = (
                " В пакете также есть сканы/фото: локальный режим их не распознает."
                if visual_count else ""
            )
            answer = messagebox.askyesnocancel(
                "Способ разбора PDF",
                f"Найден текст обхода в PDF ({legal_count}).{warning}\n\n"
                "Да — бесплатный локальный разбор; Нет — ИИ для всех файлов "
                "(возможен расход токенов за каждый файл и проход); Отмена — не запускать.",
                default="yes", parent=self,
            )
            if answer is None:
                return False
            use_llm = not answer
        elif visual_count:
            if not messagebox.askokcancel(
                "Сканы и фото соседей",
                "Для чтения сканов и фото понадобится ИИ/Vision и могут расходоваться токены. "
                "Запустить разбор?",
                default="cancel", parent=self,
            ):
                return False
            use_llm = True
        self.use_llm_var.set(use_llm)
        self._refresh_consensus_rows()
        return True

    def _start_neighbor_batch(
        self, file_list: list[Path], *, draw: bool, open_popup: bool = False,
        pdf_pages_spec: str | None = None,
    ) -> None:
        if self._busy or not file_list:
            return
        if self.pending_conflict is not None or self.pending_doubt is not None:
            self._set_status("Сначала разрешите конфликт или сомнение в результате разбора.", error=True)
            return
        # Неудача любого выбранного файла блокирует частичную схему, даже если
        # позже переключить источник или повторно разобрать другой файл.
        self._pending_neighbor_files.update(file_list)
        attached_batch = (
            self._current_source_mode() == "📄 PDF" and bool(self.neighbor_pdf_paths)
            and set(file_list).issubset(set(self.neighbor_pdf_paths))
        )
        self._running_batch_attached = attached_batch
        if attached_batch:
            self._running_source_signature = self._source_signature()
            self._running_source_path = None
            self._running_source_label = None
            self._ready_source_signature = None
            self._input_incomplete = True
            # Повторный разбор может завершиться ошибкой: старый план файла
            # не должен считаться новым успешно обработанным результатом.
            self._pending_neighbor_pdfs.update(file_list)
            for path in file_list:
                self._neighbor_parsed_pages.pop(path, None)
        self._dismiss_conflict_ui()
        cfg, provider_id, model_name, api_key, key_secret = self._build_runtime_config()
        use_llm = bool(self.use_llm_var.get())
        scale = self._current_scale()
        has_visual = any(is_image_file(p) or p.suffix.lower() == ".pdf" for p in file_list)
        do_auto_orient = self._consume_one_shot_auto_orient(has_visual)
        self._busy = True
        self._set_controls_busy(True)
        self.progress.start()
        mode = "ИИ" if use_llm else "бесплатно, локально"
        action = "Пакетная отрисовка" if draw else "Разбор соседей без AutoCAD"
        self._set_status(f"{action}: {len(file_list)} файл., {mode}…")
        self._log_line(f"--- {action} ({len(file_list)} файл., {mode}) ---")
        args = (
            list(file_list), cfg, provider_id, model_name, api_key, key_secret,
            use_llm, scale, do_auto_orient, pdf_pages_spec,
        )
        if draw:
            self._start_background(self._batch_neighbors_worker, args=args)
        else:
            self._start_background(
                self._verify_neighbors_only_worker, args=args + (open_popup,)
            )

    def _parse_neighbor_file(
        self, fpath: Path, cfg: dict[str, Any], provider_id: str, model_name: str,
        api_key: str | None, key_secret: str, use_llm: bool, do_auto_orient: bool,
        pdf_pages_spec: str | None, progress: Any, post: Any,
    ) -> tuple[ParseOutcome, bool]:
        """Разбирает один файл пакета, сохраняя выбранное число проходов ИИ."""
        source: dict[str, Any] = {}
        if is_image_file(fpath):
            source["image_paths"] = [fpath]
        elif fpath.suffix.lower() == ".pdf":
            source["pdf_path"] = fpath
            source["pdf_pages"] = pdf_pages_spec
        else:
            source["text"] = fpath.read_text(encoding="utf-8", errors="replace")
        enhance = bool(cfg.get("api", {}).get("vision_enhance_scans", True))
        passes = int((cfg.get("api", {}).get("consensus") or {}).get("passes") or 1)
        if use_llm and passes >= 2:
            consensus = run_multipass_consensus(
                **source, cfg=cfg, enhance_scan=enhance, auto_orient=do_auto_orient,
                passes=passes, primary_provider=provider_id, primary_model=model_name,
                primary_api_key=api_key, key_secret=key_secret, progress=progress,
            )
            post("round_usages", [out.usage for out in consensus.pass_outcomes])
            if not consensus.agreed:
                retry_hint = (
                    "Откройте только этот PDF или фото в роли «Соседний участок»"
                    if fpath.suffix.lower() == ".pdf" or is_image_file(fpath)
                    else "Вставьте текст из файла во вкладку «Текст» и выберите роль «Соседний участок»"
                )
                raise LegalDescriptionError(
                    "проходы ИИ разошлись; файл не добавлен и не начерчен. "
                    f"{retry_hint}, затем повторите разбор для ручного разрешения. "
                    + consensus.discrepancy_report()
                )
            return consensus.selected_outcome, True
        outcome = parse_legal_description_full(
            **source, cfg=cfg, enhance_scan=enhance, auto_orient=do_auto_orient,
            use_llm=use_llm, provider=provider_id, model=model_name, api_key=api_key,
            key_secret=key_secret, allow_regex_fallback=False, progress=progress,
        )
        return outcome, False

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
        pdf_pages_spec: str | None = None,
        open_popup: bool = True,
    ) -> None:
        post = self._worker_poster()
        plans_done: list[tuple[Path, ParseOutcome, DrawPlan, bool]] = []
        errors: list[str] = []
        builder = AcadBuilder(cfg)
        total = len(file_list)
        for idx, fpath in enumerate(file_list, start=1):
            if not self._worker_is_current():
                return
            try:
                post(
                    "progress",
                    f"[{idx}/{total}] Сверка с соседями — читаю {fpath.name}…",
                )
                outcome, pre_counted = self._parse_neighbor_file(
                    fpath, cfg, provider_id, model_name, api_key, key_secret,
                    use_llm, do_auto_orient, pdf_pages_spec,
                    lambda msg, i=idx: post("progress", f"[{i}/{total}] {msg}"), post,
                )
                if not self._worker_is_current():
                    return
                if use_llm or is_image_file(fpath):
                    post("valid_secret", key_secret)
                nb_plan = builder.plan(outcome.data, scale=scale, is_neighbor=True)
                plans_done.append((fpath, outcome, nb_plan, pre_counted))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{fpath.name}: {exc}")
        post("verify_neighbors_done", (plans_done, errors, open_popup, pdf_pages_spec))
        post("done", None)

    def _load_neighbor_files(self) -> None:
        """Выбирает и анализирует файлы соседей; AutoCAD запускается отдельной кнопкой."""
        self._pick_scene_files(open_popup=False)

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
        pdf_pages_spec: str | None = None,
    ) -> None:
        post = self._worker_poster()
        done_items: list[tuple[Path, ParseOutcome, DrawResult, bool]] = []
        errors: list[str] = []
        builder = AcadBuilder(cfg)
        total = len(file_list)
        for idx, fpath in enumerate(file_list, start=1):
            if not self._worker_is_current():
                return
            try:
                post(
                    "progress",
                    f"[{idx}/{total}] Разбираю соседний участок: {fpath.name}…",
                )
                outcome, pre_counted = self._parse_neighbor_file(
                    fpath, cfg, provider_id, model_name, api_key, key_secret,
                    use_llm, do_auto_orient, pdf_pages_spec,
                    lambda msg, i=idx: post("progress", f"[{i}/{total}] {msg}"), post,
                )
                if not self._worker_is_current():
                    return
                if use_llm or is_image_file(fpath):
                    post("valid_secret", key_secret)

                # Пакет не должен обходить защиту от рискованной геометрии:
                # такие файлы нужно проверить отдельно, а не рисовать автоматически.
                plan = builder.plan(outcome.data, scale=scale, is_neighbor=True)
                if bool(cfg.get("api", {}).get("ask_on_doubt", True)) and (
                    outcome.typo_suspicions
                    or (plan.suspicion is not None and plan.suspicion.is_suspicious)
                    or plan.closure.distance > 0.08
                ):
                    raise LegalDescriptionError(
                        "геометрия требует подтверждения; пакетная отрисовка пропущена. "
                        "Откройте только этот PDF в роли «Соседний участок» "
                        "и повторите анализ для ручной проверки."
                    )
                if sys.platform != "win32":
                    raise AcadConnectionError(
                        "Отрисовка возможна только в Windows с запущенным AutoCAD "
                        f"(текущая ОС: {sys.platform})"
                    )
                post(
                    "progress",
                    f"[{idx}/{total}] Рисую соседа «{fpath.name}» в слое NEIGHBOR_LOT…",
                )
                draw_res = self._draw_if_current(builder, plan)
                if draw_res is None:
                    return
                done_items.append((fpath, outcome, draw_res, pre_counted))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{fpath.name}: {exc}")
        post("batch_neighbors", (done_items, errors, pdf_pages_spec))
        post("done", None)

    def _start_draw_scene(self, *, neighbors_only: bool = False) -> None:
        """Рисует только то, что уже рассчитано и видно на схеме, без вызова парсера."""
        if self._busy or getattr(self, "_source_role_dialog", None) is not None:
            return
        if self.pending_conflict is not None or self.pending_doubt is not None:
            self._set_status("Сначала разрешите конфликт или сомнение в результате разбора.", error=True)
            return
        plans = [(name, plan) for name, plan in self._scene_plans() if not neighbors_only or plan.is_neighbor]
        if not plans:
            self._set_status(
                "Нет рассчитанных соседей. Сначала разберите соседний участок."
                if neighbors_only else "Нет рассчитанной схемы. Сначала разберите исходные данные.",
                error=True,
            )
            return
        unopened = self._pending_neighbor_pdfs.difference(self.neighbor_pdf_paths)
        if unopened:
            names = ", ".join(path.name for path in sorted(unopened))
            self._set_status(f"Не удалось открыть PDF: {names}. Исправьте выбор файлов перед отрисовкой.", error=True)
            return
        failed_in_set = [item for item in self.pdf_set if item.error or not item.on_scene]
        if failed_in_set:
            names = ", ".join(item.path.name for item in failed_in_set)
            self._set_status(
                f"Не все файлы набора разобраны: {names}. Откройте «☰ Роли и страницы»: исправьте страницы "
                "или способ разбора либо уберите файл из набора — готовые участки заново не запрашиваются.",
                error=True,
            )
            return
        if self._pending_neighbor_files:
            names = ", ".join(path.name for path in sorted(self._pending_neighbor_files))
            self._set_status(
                f"Не все файлы соседей разобраны: {names}. Разберите их повторно или очистите соседей; "
                "после очистки при необходимости разберите текущий источник заново.",
                error=True,
            )
            return
        signature = self._source_signature()
        if signature is None and self._ready_source_signature is not None:
            ready_mode = str(self._ready_source_signature[0])
            if ready_mode != self._current_source_mode():
                # Переход на пустую вкладку сам по себе не меняет предыдущий ввод.
                signature = self._source_signature(mode=ready_mode)
        if (signature != self._ready_source_signature or self._input_incomplete
            or self._main_analysis_pending or self._neighbor_text_pending):
            self._set_status(
                "Исходные данные или страницы изменились. Сначала разберите их заново; "
                "в AutoCAD отправляется только рассчитанная схема.", error=True,
            )
            return
        pages = str(self.pdf_pages_var.get() or "").strip() or None
        if self.neighbor_pdf_paths and (
            self._pending_neighbor_pdfs
            or any(path not in self._neighbor_plan_sources or self._neighbor_parsed_pages.get(path) != pages
                   for path in self.neighbor_pdf_paths)
        ):
            self._set_status("Не все прикреплённые PDF соседей разобраны. Завершите разбор перед отрисовкой.", error=True)
            return

        risks: list[str] = []
        for name, plan in plans:
            outcome = self._plan_outcomes.get(id(plan))
            reasons: list[str] = []
            if outcome is not None and outcome.typo_suspicions:
                reasons.append("возможная опечатка в исходнике")
            if plan.suspicion is not None and plan.suspicion.is_suspicious:
                reasons.append(plan.suspicion.headline())
            if plan.suspicion is None:
                reasons.append("проверка формы недоступна")
            if plan.warnings:
                reasons.append(f"предупреждений расчёта: {len(plan.warnings)}")
            if plan.closure.distance > 0.08:
                reasons.append(f"невязка {plan.closure.distance:.3f} ft")
            if reasons:
                risks.append(f"• {name}: {'; '.join(reasons)}")
        if self._neighbor_report is not None and self._neighbor_report.has_issues:
            risks.append("• " + self._neighbor_report.headline())
        if risks and self.ask_on_doubt_var.get():
            from tkinter import messagebox

            if not messagebox.askyesno(
                "Подтвердите отрисовку",
                "В рассчитанной схеме есть замечания:\n\n" + "\n".join(risks)
                + "\n\nСверьте данные с документом. Начертить именно показанную схему без изменений?",
                default="no", parent=self,
            ):
                self._set_status("Отрисовка отменена; рассчитанная схема сохранена.", warn=True)
                return

        cfg, _provider, _model, _key, _secret = self._build_runtime_config()
        self._busy = True
        self._set_controls_busy(True)
        self.progress.start()
        # Черновик для печати — отдельным планом, первым: итоговым результатом остаётся основной участок.
        draw_plans = (self._print_draft_plans() + plans) if not neighbors_only else plans
        self._set_status(f"Рисую рассчитанную схему в AutoCAD: {len(plans)} уч. Без повторного разбора и запроса к ИИ…")
        self._start_background(self._draw_scene_worker, args=(draw_plans, cfg))

    def _print_draft_plans(self) -> list[tuple[str, DrawPlan]]:
        """Черновик для печати из основного участка: без соседей, на +1000 по X, отдельные слои."""
        main = self._preview_main_plan
        if main is None or main.is_neighbor:
            return []
        print_cfg = dict((self.config_data or {}).get("print_draft") or {})
        if not print_cfg.get("enabled", False):
            return []
        label = self._plan_source_labels.get(id(main), "текст")
        return [(f"Черновик для печати: {label}", make_print_draft(main, print_cfg))]

    def _draw_scene_worker(self, plans: list[tuple[str, DrawPlan]], cfg: dict[str, Any]) -> None:
        post = self._worker_poster()
        completed: list[tuple[str, DrawResult]] = []
        error = ""
        try:
            if sys.platform != "win32":
                raise AcadConnectionError(
                    "Отрисовка возможна только в Windows с запущенным AutoCAD "
                    f"(текущая ОС: {sys.platform})"
                )
            builder = AcadBuilder(cfg)
            for index, (name, plan) in enumerate(plans, 1):
                if not self._worker_is_current():
                    return
                post("progress", f"[{index}/{len(plans)}] Рисую рассчитанный план: {name}…")
                result = self._draw_if_current(builder, plan)
                if result is None:
                    return
                completed.append((name, result))
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
        post("scene_drawn", (completed, error))
        post("done", None)

    def _start(
        self, parse_only: bool, *, force_neighbor: bool = False, open_neighbor_popup: bool = False,
    ) -> None:
        if self._busy or getattr(self, "_source_role_dialog", None) is not None:
            return
        if not parse_only:
            self._start_draw_scene(neighbors_only=force_neighbor)
            return
        signature = self._source_signature()
        if signature is None:
            self._set_status("В выбранном режиме нет исходных данных для разбора.", error=True)
            return
        mode = self._current_source_mode()
        if mode == "📄 PDF" and self.pdf_set:
            # Кнопка разбора перечитывает весь набор, включая уже готовые файлы.
            self._start_pdf_set(force=True)
            return
        if mode == "📄 PDF":
            self._select_result_tab("🧾 Журнал")  # одиночный PDF / соседние файлы — тоже журнал
        # Один соседний PDF разбирается как отдельный участок, чтобы конфликты
        # проходов и опечатки можно было разрешить вручную в привычном диалоге.
        single_neighbor_pdf = (
            self.neighbor_pdf_paths[0]
            if mode == "📄 PDF" and len(self.neighbor_pdf_paths) == 1 else None
        )
        if mode == "📄 PDF" and len(self.neighbor_pdf_paths) > 1:
            pages = str(self.pdf_pages_var.get() or "").strip() or None
            self._start_neighbor_batch(
                list(self.neighbor_pdf_paths), draw=False, open_popup=open_neighbor_popup, pdf_pages_spec=pages,
            )
            return
        text = self.text_box.get("1.0", "end").strip()
        is_neighbor = bool(force_neighbor or self.is_neighbor_mode())

        self._dismiss_conflict_ui()
        self._open_neighbor_report_after_parse = open_neighbor_popup
        self._running_source_signature = signature
        if mode == "📄 PDF":
            self._running_source_path = single_neighbor_pdf or self.pdf_path
            self._running_source_label = self._running_source_path.name if self._running_source_path else "PDF"
        elif mode == "📷 Фото":
            self._running_source_path = self.image_paths[0] if self.image_paths else None
            self._running_source_label = ", ".join(path.name for path in self.image_paths)
        else:
            role = "neighbor" if is_neighbor else "main"
            self._running_source_path = self._text_source_path if self._text_source_role == role else None
            self._running_source_label = self._current_text_source_label(text)
        if mode == "📄 PDF" and self.pdf_path and not self._pdf_needs_vision and text != self._loaded_pdf_text:
            self._running_source_label += " (текст изменён)"
        self._ready_source_signature = None
        self._input_incomplete = True
        if single_neighbor_pdf is not None:
            self._pending_neighbor_pdfs.add(single_neighbor_pdf)
            self._neighbor_parsed_pages.pop(single_neighbor_pdf, None)
        cfg, provider_id, model_name, api_key, key_secret = self._build_runtime_config()
        use_llm = bool(self.use_llm_var.get())
        passes_count = self.current_passes_count() if use_llm else 1
        escalate_economy = False
        if self.economy_var.get() and not use_llm:
            economy_ready, economy_block = _ai_key_status(cfg, provider_id, model_name, api_key, key_secret)
            escalate_economy = economy_ready
            if not economy_ready:
                self._log_line(f"Эконом: ИИ не вызван при проблемах — {economy_block}")
        scale = self._current_scale()
        if not is_neighbor:
            self._main_analysis_pending = True
        elif self._running_source_path is None:
            self._neighbor_text_pending = True
        else:
            self._pending_neighbor_files.add(self._running_source_path)
        provider_title = self._pid_to_label.get(provider_id, provider_id)

        vision_images = list(self.image_paths) if mode == "📷 Фото" else []
        pdf_pages_spec = (str(self.pdf_pages_var.get() or "").strip() or None) if mode == "📄 PDF" else None
        if (mode == "📄 PDF" and self.pdf_path is not None and pdf_pages_spec
            and not self._pdf_needs_vision and text != self._loaded_pdf_text):
            self._set_status(
                "Текст PDF изменён вручную, а выбран диапазон страниц. "
                "Верните исходный текст, заново откройте PDF или разберите исправления во вкладке «Текст».",
                error=True,
            )
            return
        self._running_pdf_pages = pdf_pages_spec
        vision_pdf = single_neighbor_pdf or (
            self.pdf_path
            if (mode == "📄 PDF" and self.pdf_path and (self._pdf_needs_vision or pdf_pages_spec))
            else None
        )
        if mode == "📷 Фото":
            text = ""
        if vision_pdf and pdf_pages_spec:
            # Текст из редактора содержит все страницы; при выборе диапазона
            # передаём только PDF, чтобы не подмешивать остальные страницы.
            text = ""
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

        self._start_background(
            self._worker,
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
                escalate_economy,
            ),
        )

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
        escalate_economy: bool = False,
    ) -> None:
        post = self._worker_poster()
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
                    progress=lambda message: post("progress", message),
                )
                post("valid_secret", key_secret)
                if not consensus.agreed:
                    post("consensus_label", None)
                    post(
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
                    post("done", None)
                    return
                outcome = consensus.selected_outcome
                post("consensus_label", f"{len(consensus.pass_outcomes)}/{len(consensus.pass_outcomes)}")
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
                    progress=lambda message: post("progress", message),
                    escalate_to_llm=escalate_economy,
                )
                if use_llm:
                    post("valid_secret", key_secret)
                post("consensus_label", None)
                round_usages = []
                pre_counted = False

            if not self._worker_is_current():
                return
            builder = AcadBuilder(cfg)
            plan = builder.plan(
                outcome.data, scale=scale, is_neighbor=is_neighbor
            )
            post(
                "progress",
                f"Готово ({outcome.source}): сторон {len(outcome.data.segments)}, "
                f"POB ({plan.pob[0]:+.2f}, {plan.pob[1]:+.2f}) ft, "
                f"Closure Error {plan.closure.distance:.3f} ft",
            )
            post("reading_summary", reading_summary(getattr(outcome, "check", None)))
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
                post(
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
                post("done", None)
                return

            if parse_only:
                post("parsed", (outcome, None, pre_counted, round_usages, plan))
                post("done", None)
                return

            post(
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
            result = self._draw_if_current(builder, plan)
            if result is None:
                return
            post("parsed", (outcome, result, pre_counted, round_usages))
            post("done", None)
        except KeyProtectionError as exc:
            post("error", str(exc))
            post("done", None)
        except LLMParseError as exc:
            message = str(exc)
            if use_llm and (vision_pdf is not None or vision_images):
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
            post("error", message)
            post("done", None)
        except (LegalDescriptionError, AcadBuilderError) as exc:
            post("error", str(exc))
            post("done", None)
        except Exception as exc:  # noqa: BLE001
            detail = traceback.format_exc(limit=6)
            post("error", f"Непредвиденная ошибка: {exc}\n{detail}")
            post("done", None)

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
        self._record_plan_for_preview(
            plan, warnings=outcome.warnings, source=self._running_source_path,
            source_label=self._running_source_label, outcome=outcome,
        )
        self._ready_source_signature = self._running_source_signature
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

        self.doubt_bar.grid(row=1, column=0, sticky="ew", padx=12, pady=(6, 0))
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
        win.attributes("-topmost", bool(self.topmost_var.get()))
        self._attach_child_window(win)
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
        self._start_background(
            self._retry_doubt_worker,
            args=(ctx, next_round),
        )

    def _retry_doubt_worker(self, ctx: dict[str, Any], next_round: int) -> None:
        post = self._worker_poster()
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
                progress=lambda msg: post("progress", msg),
            )
            if not self._worker_is_current():
                return
            post("valid_secret", key_secret)
            if isinstance(res, ConsensusResult):
                if not res.agreed:
                    post(
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
                    post("done", None)
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
                post("doubt", updated_ctx)
                post("done", None)
                return

            self._finalize_worker_outcome(
                new_outcome,
                cfg=cfg,
                scale=scale,
                parse_only=parse_only,
                is_neighbor=is_neighbor,
                pre_counted=pre_counted,
                round_usages=round_usages,
                key_secret=key_secret,
            )
        except Exception as exc:  # noqa: BLE001
            post("error", f"Ошибка прицельной перепроверки зоны сомнения: {exc}")
            post("done", None)

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
        self._open_neighbor_report_after_parse = False
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

        self.conflict_bar.grid(row=0, column=0, sticky="ew", padx=12, pady=(6, 0))
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
        win.attributes("-topmost", bool(self.topmost_var.get()))
        self._attach_child_window(win)
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
        self._start_background(
            self._retry_conflict_worker,
            args=(ctx,),
        )

    def _retry_conflict_worker(self, ctx: dict[str, Any]) -> None:
        post = self._worker_poster()
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
                progress=lambda msg: post("progress", msg),
            )
            if not self._worker_is_current():
                return
            if not new_consensus.agreed:
                updated_ctx = dict(ctx)
                updated_ctx["consensus"] = new_consensus
                post("conflict", updated_ctx)
                post("done", None)
                return

            round_usages = [o.usage for o in new_consensus.pass_outcomes]
            post("progress", "✓ Модели сошлись во мнении! Завершаю построение…")
            outcome = new_consensus.selected_outcome
            self._finalize_worker_outcome(
                outcome,
                cfg=cfg,
                scale=scale,
                parse_only=parse_only,
                is_neighbor=is_neighbor,
                pre_counted=True,
                round_usages=round_usages,
                key_secret=key_secret,
            )
        except Exception as exc:  # noqa: BLE001
            post("error", f"Ошибка повторного разбора спорных мест: {exc}")
            post("done", None)

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
                self._record_plan_for_preview(
                    plan, warnings=outcome.warnings, source=self._running_source_path,
                    source_label=self._running_source_label, outcome=outcome,
                )
                self._mark_running_source_ready(plan)
                self._update_closure_badge(
                    plan.closure.short_report(), plan.closure.distance
                )
            except Exception:  # noqa: BLE001
                self._update_closure_badge(self._closure_text(outcome), None)
            return

        self._busy = True
        self._set_controls_busy(True)
        self.progress.start()
        self._start_background(
            self._finalize_worker_outcome,
            args=(outcome,),
            kwargs={
                "cfg": ctx["cfg"],
                "scale": ctx["scale"],
                "parse_only": False,
                "is_neighbor": ctx["is_neighbor"],
                "pre_counted": pre_counted,
                "round_usages": None,
                "user_confirmed": True,
                "key_secret": str(ctx.get("key_secret") or ""),
            },
        )

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
        key_secret: str = "",
    ) -> None:
        post = self._worker_poster()
        try:
            builder = AcadBuilder(cfg)
            plan = builder.plan(outcome.data, scale=scale, is_neighbor=is_neighbor)
            if (
                not user_confirmed
                and bool(cfg.get("api", {}).get("ask_on_doubt", True))
                and bool(outcome.typo_suspicions)
            ):
                post(
                    "doubt",
                    {
                        "outcome": outcome,
                        "plan": plan,
                        "cfg": cfg,
                        "provider_id": str(cfg.get("api", {}).get("provider") or "hydra"),
                        "model_name": str(cfg.get("api", {}).get("model") or "glm-5.2"),
                        "api_key": None,
                        "key_secret": key_secret,
                        "use_llm": True,
                        "scale": scale,
                        "parse_only": parse_only,
                        "is_neighbor": is_neighbor,
                        "pre_counted": pre_counted,
                        "round_usages": round_usages or [],
                        "doubt_round": 1,
                    },
                )
                post("done", None)
                return
            if parse_only:
                post("parsed", (outcome, None, pre_counted, round_usages or [], plan))
                post("done", None)
                return
            if sys.platform != "win32":
                raise AcadConnectionError(
                    "Отрисовка возможна только в Windows с запущенным AutoCAD "
                    f"(текущая ОС: {sys.platform})"
                )
            result = self._draw_if_current(builder, plan)
            if result is None:
                return
            post("parsed", (outcome, result, pre_counted, round_usages or []))
            post("done", None)
        except (LegalDescriptionError, AcadBuilderError) as exc:
            post("error", str(exc))
            post("done", None)
        except Exception as exc:  # noqa: BLE001
            detail = traceback.format_exc(limit=6)
            post("error", f"Непредвиденная ошибка: {exc}\n{detail}")
            post("done", None)

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
        self._refresh_stepper()


class PdfSetDialog:
    """
    Список выбранных PDF: какой файл наш, какие страницы и чем разбирать каждый.

    Все остальные файлы считаются соседями. Страницы подставляются из имени
    файла (``…_p17``, ``…_p2_5``) и остаются правимыми. Окно работает с
    копиями: «Отмена» ничего не меняет в программе.
    """

    MODE_LOCAL = "Локально"
    MODE_ECONOMY = "Эконом"
    MODE_LLM = "ИИ"
    NO_MAIN = "none"

    def __init__(
        self, app: "SurveyApp", items: list[PdfSetItem], *, fresh: bool,
        notes: list[str] | None = None,
    ) -> None:
        self.app = app
        self.items = list(items)
        self.fresh = fresh
        self.closed = False
        # Новый набор при непустой схеме: заменить её или добавить — спрашиваем каждый раз.
        self.ask_scene = bool(fresh and app._scene_plans())
        self.vision_limit = int(app.config_data.get("api", {}).get("vision_max_pages") or 8)
        mains = [index for index, item in enumerate(self.items) if item.is_main]
        if mains:
            initial = str(mains[0])
        elif not fresh:
            initial = self.NO_MAIN
        elif len(self.items) == 1 and app._preview_main_plan is None:
            initial = "0"
        else:
            initial = ""
        self.main_var = ctk.StringVar(value=initial)
        self.scene_var = ctk.StringVar(value="")
        self.rows: list[dict[str, Any]] = []
        self._build()
        if notes:
            self._show_message("Не открыты: " + "; ".join(notes), error=True)

    # ---- окно ----
    def _build(self) -> None:
        app = self.app
        win = ctk.CTkToplevel(app)
        self.win = win
        win.title("Набор PDF — наш участок и соседи")
        win.geometry("940x600")
        win.minsize(820, 420)
        win.configure(fg_color=THEME["bg_root"])
        win.transient(app)
        win.attributes("-topmost", bool(app.topmost_var.get()))
        win.grid_columnconfigure(0, weight=1)
        win.grid_rowconfigure(1, weight=1)

        head = ctk.CTkFrame(win, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 8))
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            head, text="Отметьте наш участок — остальные файлы будут соседями",
            font=ctk.CTkFont(size=15, weight="bold"), text_color=THEME["text_main"], anchor="w",
        ).grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(
            head,
            text=(
                "В разбор идут только указанные страницы — оставьте те, где описаны границы. "
                "Страницы из имени файла (…_p17 или …_p2_5) подставлены сами, их можно править."
            ),
            font=ctk.CTkFont(size=12), text_color=THEME["text_muted"], anchor="w",
            justify="left", wraplength=880,
        ).grid(row=1, column=0, sticky="ew", pady=(4, 0))

        self.list_frame = ctk.CTkScrollableFrame(win, fg_color=THEME["bg_card"], corner_radius=10)
        self.list_frame.grid(row=1, column=0, sticky="nsew", padx=14)
        self.list_frame.grid_columnconfigure(0, weight=1)

        foot = ctk.CTkFrame(win, fg_color="transparent")
        foot.grid(row=2, column=0, sticky="ew", padx=18, pady=(10, 14))
        foot.grid_columnconfigure(0, weight=1)
        self.no_main_radio = ctk.CTkRadioButton(
            foot, text="Нашего участка в этом наборе нет — все файлы соседи",
            variable=self.main_var, value=self.NO_MAIN, command=self._refresh_notes,
            font=ctk.CTkFont(size=12), text_color=THEME["text_main"],
        )
        self.no_main_radio.grid(row=0, column=0, sticky="w")

        self.scene_frame = ctk.CTkFrame(
            foot, fg_color=THEME["bg_field"], corner_radius=8,
            border_width=1, border_color=THEME["border_subtle"],
        )
        self.scene_frame.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        ctk.CTkLabel(
            self.scene_frame, text="На схеме уже есть участки:",
            font=ctk.CTkFont(size=12, weight="bold"), text_color=THEME["text_main"],
        ).grid(row=0, column=0, sticky="w", padx=(12, 14), pady=8)
        self.scene_replace_radio = ctk.CTkRadioButton(
            self.scene_frame, text="заменить схему этим набором",
            variable=self.scene_var, value="replace", command=self._clear_message,
            font=ctk.CTkFont(size=12), text_color=THEME["text_main"],
        )
        self.scene_replace_radio.grid(row=0, column=1, sticky="w", padx=(0, 16), pady=8)
        self.scene_add_radio = ctk.CTkRadioButton(
            self.scene_frame, text="добавить к имеющимся",
            variable=self.scene_var, value="add", command=self._clear_message,
            font=ctk.CTkFont(size=12), text_color=THEME["text_main"],
        )
        self.scene_add_radio.grid(row=0, column=2, sticky="w", padx=(0, 12), pady=8)
        if not self.ask_scene:
            self.scene_frame.grid_remove()

        self.message = ctk.CTkLabel(
            foot, text="", font=ctk.CTkFont(size=12), text_color=THEME["err"],
            anchor="w", justify="left", wraplength=880,
        )
        self.message.grid(row=2, column=0, sticky="ew", pady=(8, 0))

        buttons = ctk.CTkFrame(foot, fg_color="transparent")
        buttons.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        buttons.grid_columnconfigure(1, weight=1)
        self.add_button = ctk.CTkButton(
            buttons, text="＋ Добавить PDF…", width=150, height=34,
            fg_color=THEME["btn_secondary"], hover_color=THEME["btn_secondary_hover"],
            command=self.add_files,
        )
        self.add_button.grid(row=0, column=0, sticky="w")
        self.cancel_button = ctk.CTkButton(
            buttons, text="Отмена", width=110, height=34,
            fg_color=THEME["btn_secondary"], hover_color=THEME["btn_secondary_hover"],
            command=self.cancel,
        )
        self.cancel_button.grid(row=0, column=2, padx=(0, 8))
        self.submit_button = ctk.CTkButton(
            buttons, text="▶ Отправить на разбор", width=210, height=34,
            fg_color=THEME["btn_primary"], hover_color=THEME["btn_primary_hover"],
            font=ctk.CTkFont(size=13, weight="bold"), command=self.submit,
        )
        self.submit_button.grid(row=0, column=3)

        win.protocol("WM_DELETE_WINDOW", self.cancel)
        win.bind("<Escape>", lambda _event: self.cancel())
        self._rebuild_rows()
        self._grab_input()

    def _grab_input(self, attempt: int = 0) -> None:
        """Делает список модальным; окно может стать видимым не сразу — тогда повтор."""
        if self.closed:
            return
        try:
            self.win.grab_set()
            self.win.lift()
            self.win.focus_set()
        except Exception:  # noqa: BLE001 - без захвата окно остаётся рабочим
            if attempt < 5:
                self.win.after(120, lambda: self._grab_input(attempt + 1))

    def _rebuild_rows(self) -> None:
        for row in self.rows:
            row["frame"].destroy()
        self.rows = []
        for index, item in enumerate(self.items):
            self.rows.append(self._build_row(index, item))
        self._refresh_notes()
        self._refresh_submit_text()

    def _build_row(self, index: int, item: PdfSetItem) -> dict[str, Any]:
        frame = ctk.CTkFrame(
            self.list_frame, fg_color=THEME["bg_field"], corner_radius=8,
            border_width=1, border_color=THEME["border_subtle"],
        )
        frame.grid(row=index, column=0, sticky="ew", padx=6, pady=4)
        frame.grid_columnconfigure(1, weight=1)
        radio = ctk.CTkRadioButton(
            frame, text="Наш", width=64, variable=self.main_var, value=str(index),
            command=self._refresh_notes, font=ctk.CTkFont(size=12, weight="bold"),
            text_color=THEME["text_main"],
        )
        radio.grid(row=0, column=0, sticky="w", padx=(12, 6), pady=(10, 2))
        name = ctk.CTkLabel(
            frame, text=item.path.name, font=ctk.CTkFont(size=13, weight="bold"),
            text_color=THEME["text_main"], anchor="w", justify="left", wraplength=330,
        )
        name.grid(row=0, column=1, sticky="ew", padx=(0, 8), pady=(10, 2))

        first_var = ctk.StringVar(value=str(item.first_page))
        last_var = ctk.StringVar(value=str(item.last_page))
        ctk.CTkLabel(
            frame, text="стр. с", font=ctk.CTkFont(size=12), text_color=THEME["text_muted"],
        ).grid(row=0, column=2, padx=(0, 4), pady=(10, 2))
        first_entry = ctk.CTkEntry(
            frame, textvariable=first_var, width=48, height=28, justify="center",
            fg_color=THEME["bg_editor"], border_color=THEME["border_card"],
        )
        first_entry.grid(row=0, column=3, pady=(10, 2))
        ctk.CTkLabel(
            frame, text="по", font=ctk.CTkFont(size=12), text_color=THEME["text_muted"],
        ).grid(row=0, column=4, padx=4, pady=(10, 2))
        last_entry = ctk.CTkEntry(
            frame, textvariable=last_var, width=48, height=28, justify="center",
            fg_color=THEME["bg_editor"], border_color=THEME["border_card"],
        )
        last_entry.grid(row=0, column=5, pady=(10, 2))
        ctk.CTkLabel(
            frame, text=f"из {item.total_pages}", font=ctk.CTkFont(size=12),
            text_color=THEME["text_muted"],
        ).grid(row=0, column=6, padx=(4, 12), pady=(10, 2))

        mode_var = ctk.StringVar(value=self._mode_value(item))
        mode = ctk.CTkSegmentedButton(
            frame, values=[self.MODE_LOCAL, self.MODE_ECONOMY, self.MODE_LLM],
            variable=mode_var, height=28,
            command=lambda value, i=index: self._mode_clicked(i, value),
        )
        mode.grid(row=0, column=7, padx=(0, 8), pady=(10, 2))
        edit_text_btn = ctk.CTkButton(
            frame, text="✏ Текст", width=72, height=28, fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"], command=lambda i=index: self._edit_lot_text(i),
        )
        edit_text_btn.grid(row=0, column=8, padx=(0, 6), pady=(10, 2))
        remove = ctk.CTkButton(
            frame, text="✕", width=30, height=28, fg_color=THEME["btn_secondary"],
            hover_color=THEME["btn_secondary_hover"], command=lambda i=index: self.remove(i),
        )
        remove.grid(row=0, column=9, padx=(0, 10), pady=(10, 2))
        note = ctk.CTkLabel(
            frame, text="", font=ctk.CTkFont(size=11), text_color=THEME["text_muted"],
            anchor="w", justify="left", wraplength=840,
        )
        note.grid(row=1, column=0, columnspan=10, sticky="ew", padx=12, pady=(0, 9))
        for entry in (first_entry, last_entry):
            for event in ("<KeyRelease>", "<FocusOut>"):
                entry.bind(event, lambda _event, i=index: self.pages_edited(i))
        return {
            "frame": frame, "radio": radio, "name": name, "first": first_var, "last": last_var,
            "first_entry": first_entry, "last_entry": last_entry, "mode_var": mode_var,
            "mode": mode, "remove": remove, "note": note, "error": "",
            "default_pages": (item.first_page, item.last_page),
        }

    # ---- правка строк ----
    def pages_edited(self, index: int) -> None:
        """Поля страниц изменены: проверяем их и подстраиваем способ разбора."""
        if not 0 <= index < len(self.rows):
            return
        row, item = self.rows[index], self.items[index]
        try:
            first, last = read_page_boxes(row["first"].get(), row["last"].get(), item.total_pages)
        except ValueError as exc:
            row["error"] = str(exc)
        else:
            row["error"] = ""
            if (first, last) != (item.first_page, item.last_page):
                item.set_pages(first, last)
                wanted = self._mode_value(item)
                row["mode_var"].set(wanted)
                row["mode"].set(wanted)
        self._refresh_note(index)
        self._refresh_submit_text()

    def _mode_value(self, item: PdfSetItem) -> str:
        if item.use_llm:
            return self.MODE_LLM
        return self.MODE_ECONOMY if item.economy else self.MODE_LOCAL

    def _mode_clicked(self, index: int, value: str) -> None:
        if not 0 <= index < len(self.items):
            return
        value = str(value)
        self.items[index].set_mode(value == self.MODE_LLM, economy=value == self.MODE_ECONOMY)
        self._refresh_note(index)
        self._refresh_submit_text()

    def set_mode(self, index: int, use_llm: bool) -> None:
        """Выбор способа разбора у файла (то же, что щелчок по переключателю)."""
        value = self.MODE_LLM if use_llm else self.MODE_LOCAL
        self.rows[index]["mode_var"].set(value)
        self.rows[index]["mode"].set(value)
        self._mode_clicked(index, value)

    def _edit_lot_text(self, index: int) -> None:
        item = self.items[index]
        cache_path = self.app._find_step0_cache_path(item.path)
        target_path = cache_path or item.path.with_name(f"{item.path.stem}_text.txt")
        initial_text = ""
        if target_path.is_file():
            try:
                initial_text = target_path.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                initial_text = ""

        editor = ctk.CTkToplevel(self.win)
        editor.title(f"Текст описания — {item.path.name}")
        editor.geometry("760x520")
        editor.transient(self.win)
        editor.grid_columnconfigure(0, weight=1)
        editor.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            editor,
            text=f"Файл кеша: {target_path.name} (после сохранения нажмите «Отправить на разбор»)",
            font=ctk.CTkFont(size=12),
            text_color=THEME["text_muted"],
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 6))

        textbox = ctk.CTkTextbox(editor, font=ctk.CTkFont(family="Consolas", size=12))
        textbox.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 10))
        if initial_text:
            textbox.insert("1.0", initial_text)

        btn_row = ctk.CTkFrame(editor, fg_color="transparent")
        btn_row.grid(row=2, column=0, sticky="e", padx=14, pady=(0, 12))

        def save_and_close() -> None:
            content = str(textbox.get("1.0", "end") or "").strip()
            if content:
                target_path.write_text(content, encoding="utf-8")
                item.set_mode(False)
                self.rows[index]["mode_var"].set(self.MODE_LOCAL)
                self._refresh_notes()
                self._refresh_submit_text()
            editor.destroy()

        ctk.CTkButton(
            btn_row, text="Отмена", width=100, height=32,
            fg_color=THEME["btn_secondary"], hover_color=THEME["btn_secondary_hover"],
            command=editor.destroy,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btn_row, text="Сохранить в кеш", width=150, height=32,
            fg_color=THEME["btn_primary"], hover_color=THEME["btn_primary_hover"],
            command=save_and_close,
        ).pack(side="left")
        editor.grab_set()

    def remove(self, index: int) -> None:
        if not 0 <= index < len(self.items):
            return
        choice = str(self.main_var.get() or "")
        self._pull_pages()
        main_item = self.items[int(choice)] if choice.isdigit() and int(choice) < len(self.items) else None
        removed = self.items.pop(index)
        if main_item is removed:
            self.main_var.set("")
        elif main_item is not None:
            self.main_var.set(str(next(i for i, item in enumerate(self.items) if item is main_item)))
        self._rebuild_rows()
        self._clear_message()

    def add_files(self) -> None:
        from tkinter import filedialog

        paths = filedialog.askopenfilenames(
            title="Добавьте PDF в набор", filetypes=[("PDF", "*.pdf"), ("Все файлы", "*.*")],
            parent=self.win,
        )
        chosen = [Path(p) for p in (paths or ()) if str(p).strip()]
        if not chosen:
            return
        self._pull_pages()
        new_items, errors = self.app._make_pdf_set_items(
            chosen, existing=[item.path for item in self.items],
        )
        self.items.extend(new_items)
        self._rebuild_rows()
        if errors:
            self._show_message("Не открыты: " + "; ".join(errors), error=True)
        else:
            self._clear_message()

    def _pull_pages(self) -> None:
        """Переносит корректные значения полей в файлы перед перестройкой списка."""
        for index in range(len(self.rows)):
            self.pages_edited(index)

    # ---- подписи ----
    def _row_note(self, index: int) -> tuple[str, str]:
        row, item = self.rows[index], self.items[index]
        if row["error"]:
            return f"⚠ Страницы: {row['error']}", THEME["err"]
        pages = format_pages(item.first_page, item.last_page)
        legal = item.has_legal_text()
        if not item.use_llm and legal:
            text, color = f"✓ на {pages} найден текст обхода — бесплатный локальный разбор", THEME["ok"]
        elif not item.use_llm and item.ocr_ok:
            text, color = (
                f"🔍 скан: {pages} распознаются локально (OCR), бесплатно — около 5 с на "
                "страницу; если текст не прочитается, выберите ИИ"
            ), THEME["text_muted"]
        elif not item.use_llm:
            text, color = (
                f"⚠ на {pages} нет текста обхода (скан или чертёж) — локально не разобрать; "
                "выберите ИИ или другие страницы"
            ), THEME["warn"]
        elif legal:
            text, color = f"ИИ получит текст {pages} — платный запрос", THEME["blue_soft_fg"]
        else:
            count = item.page_count
            text, color = f"ИИ / Vision получит изображения: {count} стр. — платный запрос", THEME["blue_soft_fg"]
            if count > self.vision_limit:
                text += f"; это больше обычного лимита в {self.vision_limit} стр."
                color = THEME["warn"]
        if item.pages_note and (item.first_page, item.last_page) == row["default_pages"]:
            text += f" · {item.pages_note}"
        if item.error:
            text += f"\n✗ прошлый разбор: {' '.join(item.error.split())[:200]}"
            color = THEME["err"]
        return text, color

    def _refresh_note(self, index: int) -> None:
        text, color = self._row_note(index)
        self.rows[index]["note"].configure(text=text, text_color=color)

    def _refresh_notes(self) -> None:
        for index in range(len(self.rows)):
            self._refresh_note(index)
        self._clear_message()

    def _refresh_submit_text(self) -> None:
        paid = sum(1 for item in self.items if item.use_llm and not item.has_fresh_result())
        suffix = f" · ИИ: {paid} файл." if paid else " · бесплатно"
        self.submit_button.configure(text=f"▶ Отправить на разбор{suffix}")

    def _show_message(self, text: str, *, error: bool = True) -> None:
        self.message.configure(text=text, text_color=THEME["err"] if error else THEME["text_muted"])

    def _clear_message(self) -> None:
        if hasattr(self, "message"):
            self.message.configure(text="")

    # ---- завершение ----
    def submit(self) -> None:
        """Проверяет список и отправляет весь набор на разбор."""
        if self.closed:
            return
        self._pull_pages()
        problems = [
            f"{self.items[index].path.name}: {row['error']}"
            for index, row in enumerate(self.rows) if row["error"]
        ]
        if not self.items:
            problems.append("в наборе нет файлов — добавьте PDF")
        choice = str(self.main_var.get() or "")
        if self.items and not (choice == self.NO_MAIN or (choice.isdigit() and int(choice) < len(self.items))):
            problems.append("отметьте наш участок или пункт «Нашего участка в этом наборе нет»")
        scene_mode = str(self.scene_var.get() or "") if self.ask_scene else "update"
        if self.ask_scene and scene_mode not in {"replace", "add"}:
            problems.append("выберите, заменить схему этим набором или добавить к имеющимся участкам")
        if problems:
            self._show_message("Исправьте: " + "; ".join(problems) + ".", error=True)
            return
        for index, item in enumerate(self.items):
            item.is_main = choice == str(index)
        items = list(self.items)
        self.close()
        self.app._submit_pdf_set(items, scene_mode=scene_mode, fresh=self.fresh)

    def cancel(self) -> None:
        self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        app = self.app
        if app._pdf_set_dialog is self:
            app._pdf_set_dialog = None
        if getattr(app, "_source_role_dialog", None) is self.win:
            app._source_role_dialog = None
        try:
            self.win.grab_release()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.win.destroy()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    """Запускает окно; при сбое возвращает ненулевой код и сохраняет диагностику."""
    try:
        app = SurveyApp()
        app.mainloop()
    except Exception:  # noqa: BLE001
        report = traceback.format_exc()
        log_path: Path | None = None
        try:
            log_path = Path.home() / "tsai_crash.log"
            log_path.write_text(report, encoding="utf-8", errors="backslashreplace")
        except Exception:  # noqa: BLE001 - не скрывать исходную ошибку, если лог недоступен
            log_path = None
        print(report, file=sys.stderr)
        if log_path is not None:
            print(f"ОТЧЁТ СОХРАНЁН В ФАЙЛ: {log_path}", file=sys.stderr)
        else:
            print("Не удалось сохранить журнал ошибки.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
