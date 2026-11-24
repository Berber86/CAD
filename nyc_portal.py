"""Address lookup on NYC Property Information Portal for Step 0.

The automation drives a visible supported browser. Challenge pages detected by OCR
or the browser title stop it; fast mode intentionally skips intermediate screen checks.
RapidOCR reads Borough, Block and Lot; reliable mode can send the card's left half to
Vision once if local OCR fails. A separate Vision request reads map neighbour labels.
"""
from __future__ import annotations

import hashlib
import re
import sys
import tempfile
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

import browser_save
import front_street
import map_angle_cache
import ocr_pages
import portal_layout_cache
from llm_parser import TokenUsage, ask_llm_json, render_image_files_base64

PORTAL_URL = "https://propertyinformationportal.nyc.gov/"
#: Заголовок пустой новой вкладки в разных браузерах (русский и английский интерфейс).
NEW_TAB_TITLE_MARKS = ("new tab", "new private tab", "новая вкладка", "about:blank")

PORTAL_URL_MARKS = (
    "propertyinformationportal", "property information portal", "nyc department of finance",
)
CHALLENGE_MARKS = (
    "cloudflare", "captcha", "verify you are human", "checking your browser",
    "just a moment", "one moment", "please wait", "verifying", "challenge",
    "security check", "access denied", "attention required",
    "подтвердите, что вы человек", "пройдите проверку", "проверка безопасности",
)
BOROUGH_NAMES = ("Bronx", "Brooklyn", "Manhattan", "Queens", "Staten Island")
Progress = Callable[[str], None]


class PortalLookupError(RuntimeError):
    """The portal lookup could not safely reach a usable parcel details page."""

class _ScreenNotFound(PortalLookupError):
    """Проверенная область экрана не содержит ожидаемое состояние."""


class _InitialScreenNotFound(PortalLookupError):
    """Исходная форма не найдена, можно повторить снимок после паузы."""


@dataclass(frozen=True)
class ScreenText:
    text: str
    box: tuple[int, int, int, int]
    confidence: float = 0.0

    @property
    def center(self) -> tuple[int, int]:
        x0, y0, x1, y1 = self.box
        return (x0 + x1) // 2, (y0 + y1) // 2


@dataclass(frozen=True)
class PortalCapture:
    """Locally read identifiers and the retained map-only image for Vision."""
    block: str
    lot: str
    borough: str | None
    map_image: Path
    details_source: str = "ocr"


@dataclass(frozen=True)
class ParcelLookup:
    block: str
    lot: str
    borough: str | None
    left_neighbour: str | None
    right_neighbour: str | None
    confidence: str
    notes: str
    map_image: Path
    usage: TokenUsage
    details_source: str = "ocr"
    details_usage: TokenUsage | None = None
    #: Угол фронтальной улицы к северу от Vision и программных оценок
    #: (front_street.py); значения дублируются в JSON-кэш снимка карты.
    front_street_angle_deg: float | None = None
    front_street_angles: dict[str, float | None] = field(default_factory=dict)

    @property
    def neighbours(self) -> list[str]:
        return [
            value for value in (self.left_neighbour, self.right_neighbour)
            if value is not None and value != self.lot
        ]


def _normalise(text: str) -> str:
    return "".join(char for char in str(text or "").lower() if char.isalnum())


# Обычный режим проверяет экран; быстрый режим ждёт между действиями без промежуточного OCR.
# Надёжный режим: одно чтение после действия и максимум один повтор.
RELIABLE_PORTAL_DELAY = 5.0
OPTIMAL_PORTAL_DELAY = 3.0
RELIABLE_INITIAL_DELAY = 1.0
RELIABLE_FORM_DELAY = 1.5
RELIABLE_MENU_DELAY = 1.0
RELIABLE_ADDRESS_DELAY = 1.5
RELIABLE_SUGGESTION_DELAY = 5.0
RELIABLE_CARD_DELAY = 6.0
RELIABLE_RETRY_DELAY = 2.0
OPTIMAL_MENU_DELAY = 0.7
OPTIMAL_ADDRESS_DELAY = 1.0
OPTIMAL_SUGGESTION_DELAY = 1.2
OPTIMAL_CARD_DELAY = 2.0
FAST_SCREEN_CHECK_PAUSE_SEC = 5.0
FAST_KEY_DELAY_SEC = 0.05  # только пауза между клавишами, не ожидание реакции сайта
#: Пределы паузы «быстрой работы», которую выбирает пользователь в поле на Шаге 0.
FAST_PAUSE_MIN_SEC = 1.0
FAST_PAUSE_MAX_SEC = 5.0


def parse_fast_pause(text: object) -> float | None:
    """
    Разбирает паузу «быстрой работы» из поля: число от 1 до 5 (дробные, с точкой или запятой).

    Возвращает ``None``, если это не число или оно вне пределов — такой запуск не начинаем.
    """
    raw = str(text or "").strip().replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        return None
    if not FAST_PAUSE_MIN_SEC <= value <= FAST_PAUSE_MAX_SEC:
        return None
    return value
# В обычных режимах после прокрутки карта перерисовывается не сразу: ждём 3,8 с.
MAP_REDRAW_WAIT_SEC = 0.8 + 3.0


def _start_ocr_warmup() -> Future:
    """Создаём локальную модель в фоне, параллельно с подготовкой браузера."""
    future: Future = Future()
    def load() -> None:
        try:
            future.set_result(ocr_pages._engine_or_raise())
        except Exception as exc:  # noqa: BLE001 - показываем при первой проверке
            future.set_exception(exc)
    threading.Thread(target=load, name="step0-ocr-warmup", daemon=True).start()
    return future


def _await_ocr(warmup: Future) -> None:
    try:
        warmup.result()
    except Exception as exc:  # noqa: BLE001 - ошибка инициализации видна пользователю
        raise PortalLookupError(f"Не удалось подготовить локальный OCR: {exc}") from exc


def _screen_ocr(image_path: str | Path) -> list[ScreenText]:
    """OCR an on-disk screenshot using the same optional local RapidOCR engine."""
    engine = ocr_pages._engine_or_raise()  # one shared, lazily loaded local model
    result = ocr_pages.run_engine(engine, str(image_path))
    if isinstance(result, tuple):
        result = result[0]
    rows: list[ScreenText] = []
    for item in result or []:
        try:
            points, text = item[0], str(item[1]).strip()
            confidence = float(item[2]) if len(item) > 2 else 0.0
            xs = [int(float(point[0])) for point in points]
            ys = [int(float(point[1])) for point in points]
            if text and xs and ys:
                rows.append(ScreenText(text, (min(xs), min(ys), max(xs), max(ys)), confidence))
        except (TypeError, ValueError, IndexError):
            continue
    return rows


def _screen_ocr_top_left_quarter(image_path: str | Path) -> list[ScreenText]:
    """OCR only the upper-left screen quadrant for the initial portal/page check."""
    from PIL import Image

    try:
        with Image.open(image_path) as source:
            image = source.convert("RGB")
        width, height = image.size
        if width < 2 or height < 2:
            raise PortalLookupError("Снимок браузера слишком мал для проверки страницы.")
        crop = image.crop((0, 0, width // 2, height // 2))
        with tempfile.TemporaryDirectory(prefix="tsai_pip_quadrant_") as directory:
            crop_path = Path(directory) / "top_left_quarter.png"
            crop.save(crop_path, format="PNG")
            # The crop starts at (0, 0), so OCR boxes remain window-relative.
            return _screen_ocr(crop_path)
    except PortalLookupError:
        raise
    except Exception as exc:  # noqa: BLE001 - screenshot/crop/OCR preparation
        raise PortalLookupError(f"Не удалось проверить верхнюю левую часть браузера: {exc}") from exc


# Область управления слева; координаты результатов переводятся обратно к окну.
_FORM_REGION = (0.0, 0.0, 0.65, 0.80)


def _screen_ocr_region(image_path: str | Path, region: tuple[float, float, float, float]) -> list[ScreenText]:
    from PIL import Image
    with Image.open(image_path) as source:
        image = source.convert("RGB")
    width, height = image.size
    x0, y0, x1, y1 = (round(region[0] * width), round(region[1] * height),
                       round(region[2] * width), round(region[3] * height))
    if x1 <= x0 or y1 <= y0:
        raise PortalLookupError("Окно браузера слишком мало для распознавания.")
    with tempfile.TemporaryDirectory(prefix="tsai_pip_region_") as directory:
        path = Path(directory) / "region.png"
        image.crop((x0, y0, x1, y1)).save(path)
        rows = _screen_ocr(path)
    return [ScreenText(row.text, (row.box[0] + x0, row.box[1] + y0,
                                  row.box[2] + x0, row.box[3] + y0), row.confidence)
            for row in rows]


def _reject_challenge(rows: Sequence[ScreenText]) -> None:
    if _challenge_visible(rows):
        raise PortalLookupError("На портале обнаружена проверка человека/CAPTCHA; работа остановлена.")


def _initial_screen(handle: int, *, address_region: tuple[float, float, float, float] | None = None) -> list[ScreenText]:
    """Все резервные чтения делаются с одного снимка без дополнительных опросов."""
    with tempfile.TemporaryDirectory(prefix="tsai_pip_start_") as directory:
        path = Path(directory) / "screen.png"
        _save_window_capture(handle, path)
        rows = _screen_ocr_top_left_quarter(path)
        _reject_challenge(rows)
        if any(_contains(rows, marker) for marker in ("Select", "Find Another Property", "Enter an Address")):
            return rows
        if address_region is not None:
            rows = _screen_ocr_region(path, address_region)
            _reject_challenge(rows)
            # Из узкой области можно подтвердить только саму метку адреса.
            if _contains(rows, "Enter an Address"):
                return rows
        rows = _screen_ocr(path)
        _reject_challenge(rows)
        if any(_contains(rows, marker) for marker in ("Select", "Find Another Property", "Enter an Address")):
            return rows
    raise _InitialScreenNotFound("Не удалось подтвердить страницу NYC Portal и найти форму поиска.")


def _portal_window() -> browser_save.BrowserWindow:
    windows = [
        window for window in browser_save.list_windows()
        if browser_save.is_supported_browser(window.executable, window.image_path)
    ]
    if not windows:
        raise PortalLookupError(
            "Не найдено видимое окно поддерживаемого браузера. Откройте Chrome/Edge и повторите поиск."
        )
    active = browser_save.active_window_handle()
    portal = [w for w in windows if any(mark in w.title.lower() for mark in PORTAL_URL_MARKS)]
    # Prefer a portal tab; otherwise reuse the topmost existing supported browser window
    # and navigate its current tab (no new window/tab is launched).
    return next((w for w in portal if w.handle == active), portal[0] if portal else windows[0])


def _save_window_capture(handle: int, destination: str | Path) -> Path:
    """Capture only the selected browser window (not the rest of the desktop)."""
    from PIL import ImageGrab

    bounds = browser_save.window_bounds(handle)
    if bounds is None:
        raise PortalLookupError("Не удалось получить размер окна браузера для снимка.")
    try:
        image = ImageGrab.grab(bbox=bounds, all_screens=True)
        if image.width < 400 or image.height < 300:
            raise PortalLookupError("Окно портала свёрнуто или слишком мало для чтения.")
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path, format="PNG")
        return path
    except PortalLookupError:
        raise
    except Exception as exc:  # noqa: BLE001 - platform/permissions/screen capture
        raise PortalLookupError(f"Не удалось снять экран окна браузера: {exc}") from exc


def _contains(rows: Sequence[ScreenText], phrase: str) -> bool:
    target = _normalise(phrase)
    return any(target in _normalise(row.text) for row in rows)


def _has_exact_text(rows: Sequence[ScreenText], phrase: str) -> bool:
    target = _normalise(phrase)
    return any(_normalise(row.text) == target for row in rows)


def _challenge_visible(rows: Sequence[ScreenText]) -> bool:
    joined = _normalise(" ".join(row.text for row in rows))
    return any(_normalise(mark) in joined for mark in CHALLENGE_MARKS)


def _focus(handle: int) -> None:
    window = next((w for w in browser_save.list_windows() if w.handle == handle), None)
    if window is None or not browser_save.is_supported_browser(window.executable, window.image_path):
        raise PortalLookupError(
            "Фокус больше не на выбранном поддерживаемом браузере; клавиатуру не трогаю."
        )
    if browser_save.looks_like_cloudflare_page(window.title):
        raise PortalLookupError("Браузер показывает проверку человека/CAPTCHA; работа остановлена.")
    # foreground_is_acceptable() also allows an open <select> dropdown/autocomplete
    # popup owned by the same browser process to stay focused: forcing the parent
    # tab back to the foreground here would close that popup immediately.
    if not browser_save.foreground(handle) or not browser_save.foreground_is_acceptable(handle):
        raise PortalLookupError("Не удалось безопасно активировать окно NYC Portal.")


def _click_text(handle: int, rows: Sequence[ScreenText], text: str) -> ScreenText:
    target = _normalise(text)
    found = next((row for row in rows if target in _normalise(row.text)), None)
    if found is None:
        raise PortalLookupError(f"На странице не найден элемент «{text}».")
    bounds = browser_save.window_bounds(handle)
    if bounds is None:
        raise PortalLookupError("Окно браузера изменилось, координаты мыши недоступны.")
    x0, y0, _x1, _y1 = bounds
    cx, cy = found.center
    _focus(handle)
    if not browser_save.click_browser_window(handle, x0 + cx, y0 + cy):
        raise PortalLookupError("Не удалось нажать элемент в окне NYC Portal.")
    return found


def _key(handle: int, vk: int) -> None:
    _focus(handle)
    if not browser_save.press_key(vk):
        raise PortalLookupError("Не удалось отправить клавишу в браузер.")


def _wait_screen(
    handle: int,
    predicate: Callable[[Sequence[ScreenText]], bool],
    *,
    description: str,
    first_delay: float,
    retry_delay: float = RELIABLE_RETRY_DELAY,
    region: tuple[float, float, float, float] | None = None,
    ready: Callable[[], None] | None = None,
    on_region_fallback: Callable[[], None] | None = None,
) -> list[ScreenText]:
    """После паузы один снимок, затем максимум один повтор; никаких частых циклов."""
    latest: list[ScreenText] = []
    previous_hash: bytes | None = None
    for attempt, delay in enumerate((first_delay, retry_delay)):
        time.sleep(delay)
        if ready is not None:
            ready()
            ready = None
        _focus(handle)
        with tempfile.TemporaryDirectory(prefix="tsai_pip_ocr_") as directory:
            path = Path(directory) / "screen.png"
            _save_window_capture(handle, path)
            digest = hashlib.sha256(path.read_bytes()).digest()
            if digest != previous_hash:
                latest = _screen_ocr_region(path, region) if region else _screen_ocr(path)
                previous_hash = digest
            _reject_challenge(latest)
            if predicate(latest):
                return latest
            if attempt == 1 and region:
                latest = _screen_ocr(path)  # тот же снимок; на случай сместившейся формы
                _reject_challenge(latest)
                if predicate(latest):
                    if on_region_fallback is not None:
                        on_region_fallback()
                    return latest
    raise _ScreenNotFound(f"Не дождался {description} на NYC Portal после двух проверок.")


def _type_into_address(handle: int, address: str, rows: Sequence[ScreenText]) -> None:
    # Поле ищем устойчиво к OCR: подпись над полем содержит «…Common Place
    # Name)», а плейсхолдер «Enter an address» стоит прямо внутри поля.
    # Дословная строка «enter an address» в одной OCR-строке не обязана
    # совпасть (живой сбой 3876: режим переключён, а поле «не найдено»).
    label = next(
        (row for row in rows if "commonplace" in _normalise(row.text)), None)
    placeholder = next(
        (row for row in rows if "enteranaddress" in _normalise(row.text)), None)
    anchor = label or placeholder
    if anchor is None:
        raise PortalLookupError("После переключения режима не найдено поле Address.")
    bounds = browser_save.window_bounds(handle)
    if bounds is None:
        raise PortalLookupError("Окно портала изменилось, не могу определить поле адреса.")
    x0, y0, _x1, _y1 = bounds
    x_left, y_top, x_right, y_bottom = anchor.box
    if label is not None:
        # Подпись над полем: поле ввода — сразу под ней.
        click_x = x0 + x_left + min(120, max(35, (x_right - x_left) // 3))
        click_y = y0 + y_bottom + max(24, (y_bottom - y_top) * 2)
    else:
        # Плейсхолдер внутри поля: кликаем в само поле.
        click_x = x0 + (x_left + x_right) // 2
        click_y = y0 + (y_top + y_bottom) // 2
    _focus(handle)
    if not browser_save.click_browser_window(handle, click_x, click_y):
        raise PortalLookupError("Не удалось сфокусировать поле Address.")
    if not browser_save.press_ctrl_a() or not browser_save.type_text(address):
        raise PortalLookupError("Не удалось ввести адрес в NYC Portal.")


#: Подсказку портала узнаём по её собственному тексту: «..., NY ... USA».
#: Геометрию после трёх живых остановок не используем совсем (решение
#: пользователя 2026-11-23): у поля ввода и чисел карты такого текста нет.
_SUGGESTION_ROW_RE = re.compile(r",\s*NY\b|\bUSA\b", re.IGNORECASE)


def _suggestion_row(items: Sequence[ScreenText]) -> ScreenText | None:
    """Верхняя строка выпавшего списка подсказок — по тексту, без геометрии."""
    candidates = [row for row in items if _SUGGESTION_ROW_RE.search(row.text)]
    return min(candidates, key=lambda row: (row.box[1], row.box[0])) if candidates else None


def _suggestion_screen_hint(handle: int, address: str, address_rows: Sequence[ScreenText]) -> str:
    """Что OCR видит под полем адреса при неудаче — для журнала, а не для клика."""
    try:
        _focus(handle)
        with tempfile.TemporaryDirectory(prefix="tsai_pip_hint_") as directory:
            path = Path(directory) / "screen.png"
            _save_window_capture(handle, path)
            rows = _screen_ocr_region(path, _FORM_REGION)
        suggestion = _suggestion_row(rows)
        if suggestion is not None:
            return f" На снимке первая подсказка: «{suggestion.text}»; жду «{address}»."
        digit_rows = [row.text for row in rows if re.match(r"^\s*\d", row.text)][:3]
        if digit_rows:
            return f" На снимке строки с цифрами: {'; '.join(digit_rows)}."
        return " Подсказок на снимке не видно."
    except Exception:  # noqa: BLE001 - диагностика не должна ломать остановку
        return ""


def _wait_after_address_input(handle: int, address: str, address_rows: Sequence[ScreenText], *,
                              first_delay: float = RELIABLE_SUGGESTION_DELAY) -> ScreenText | None:
    """
    Ждём, пока портал покажет список подсказок, и возвращаем первую строку.

    Что в ней написано — не проверяем (решение пользователя 2026-11-23):
    выбирается она клавишами Down+Enter, а не кликом по координатам.
    Признак списка — текст подсказки («…, NY … USA»), геометрию не смотрим:
    она трижды отсекала настоящий список на живом окне. Нет списка после
    повторных проверок — остановка с диагностикой, что OCR видит под полем.
    """
    def present(items: Sequence[ScreenText]) -> bool:
        return _suggestion_row(items) is not None

    rows: list[ScreenText] | None = None
    for attempt, delay in enumerate((first_delay, RELIABLE_SUGGESTION_DELAY)):
        try:
            rows = _wait_screen(handle, present,
                                first_delay=delay, description="список подсказок адреса",
                                region=_FORM_REGION)
            break
        except _ScreenNotFound:
            if attempt == 0:
                # Подсказки портала иногда грузятся дольше двух проверок
                # (живой случай 2026-11-23: список уже открыт, а проверки
                # успели закончиться). Ещё один цикл — и честная остановка.
                time.sleep(RELIABLE_RETRY_DELAY)
    if rows is None:
        raise PortalLookupError(
            "Не дождался список подсказок адреса на NYC Portal "
            "после повторных проверок."
            + _suggestion_screen_hint(handle, address, address_rows)
        )
    return _suggestion_row(rows)


def _type_focused_address(handle: int, address: str) -> None:
    """Быстрый путь: Tab уже перевёл фокус в поле; промежуточного OCR нет."""
    _focus(handle)
    if not browser_save.press_ctrl_a() or not browser_save.type_text(address):
        raise PortalLookupError("Не удалось ввести адрес в NYC Portal.")


def _fast_wait(handle: int, pause: float) -> None:
    time.sleep(pause)
    _focus(handle)  # в том числе проверка заголовка на видимую по нему CAPTCHA


def _label_text(rows: Sequence[ScreenText], label: str) -> str | None:
    """Read a value printed as e.g. ``Block: 5712`` or on the following OCR row."""
    pattern = re.compile(rf"\b{re.escape(label)}\b\s*[:#-]?\s*(.*)$", re.IGNORECASE)
    for index, row in enumerate(rows):
        match = pattern.search(row.text.strip())
        if not match:
            continue
        value = match.group(1).strip(" \t:;,-")
        if not value and index + 1 < len(rows):
            candidate = rows[index + 1].text.strip()
            if not re.search(r"\b(?:Borough|Block|Lot)\b", candidate, re.IGNORECASE):
                value = candidate
        if value:
            return value
    return None


def _extract_portal_details(rows: Sequence[ScreenText]) -> tuple[str, str, str | None]:
    block_raw = _label_text(rows, "Block") or ""
    lot_raw = _label_text(rows, "Lot") or ""
    block_match = re.search(r"\d[\d,]*", block_raw)
    lot_match = re.search(r"\d[\d,]*", lot_raw)
    if block_match is None or lot_match is None:
        raise PortalLookupError(
            "Локальный OCR не смог прочитать Block и Lot на карточке; Vision их не угадывал. "
            "Введите значения вручную и повторите сбор."
        )
    block = block_match.group(0).replace(",", "")
    lot = lot_match.group(0).replace(",", "")

    borough_raw = _label_text(rows, "Borough") or ""
    borough = next(
        (name for name in BOROUGH_NAMES if _normalise(name) == _normalise(borough_raw)),
        borough_raw.strip() or None,
    )
    return block, lot, borough


def _crop_tax_map(source: str | Path, rows: Sequence[ScreenText], destination: str | Path, *,
                  min_left_fraction: float = 0.40) -> Path:
    """Retain/send only the map pane; exclude the address and property-details panel."""
    from PIL import Image

    try:
        image = Image.open(source).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        raise PortalLookupError(f"Не удалось открыть снимок карты: {exc}") from exc
    width, height = image.size
    # The DOF page uses a left details pane and a right map pane. OCR anchors on
    # Borough/Block/Lot move the crop beyond recognized details when the pane shifts;
    # callers can demand a minimum right-half crop in the reliable scenario.
    metadata_right = 0
    label_re = re.compile(r"\b(?:Borough|Block|Lot)\b", re.IGNORECASE)
    for row in rows:
        if row.box[0] < width * 0.65 and label_re.search(row.text):
            metadata_right = max(metadata_right, row.box[2])
    map_left = max(round(width * min_left_fraction), metadata_right + 12)
    if width - map_left < max(280, round(width * 0.28)):
        raise PortalLookupError(
            "Карта слишком узкая для Vision. Разверните окно браузера на весь экран и повторите поиск."
        )
    map_top = round(height * 0.105)  # below the browser chrome and portal navigation bar
    if height - map_top < 280:
        raise PortalLookupError("В окне мало места для карты; увеличьте размер браузера и повторите.")
    cropped = image.crop((map_left, map_top, width, height))
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    cropped.save(target, format="PNG", optimize=True)
    return target


def _drive_portal(
    address: str, map_path: Path, progress: Progress | None = None, *, fast_mode: bool = False,
    fast_pause_sec: float = FAST_SCREEN_CHECK_PAUSE_SEC, optimal_mode: bool = False,
    details_vision: Callable[[Path], tuple[str, str, str]] | None = None,
) -> PortalCapture:
    """
    Use the current browser window to select an address and retain a map-only crop.

    В быстром режиме фиксированные паузы заменяют промежуточный OCR; финальная
    проверка и чтение идентификаторов обязательны. Риск несработавшего действия выше.
    """
    def wait(handle: int, predicate: Callable[[Sequence[ScreenText]], bool], *,
             delay: float, description: str, region: tuple[float, float, float, float] | None = _FORM_REGION,
             on_region_fallback: Callable[[], None] | None = None) -> list[ScreenText]:
        if fast_mode:
            _fast_wait(handle, fast_pause_sec)
            _await_ocr(warmup)
            with tempfile.TemporaryDirectory(prefix="tsai_pip_fast_") as directory:
                path = Path(directory) / "screen.png"
                _save_window_capture(handle, path)
                rows = _screen_ocr_region(path, region) if region else _screen_ocr(path)
            _reject_challenge(rows)
            if not predicate(rows):
                raise _ScreenNotFound(f"После паузы не найдено: {description}. Увеличьте паузу или выключите быструю работу.")
            return rows
        return _wait_screen(handle, predicate, first_delay=delay, description=description, region=region,
                            ready=lambda: _await_ocr(warmup), on_region_fallback=on_region_fallback)

    if optimal_mode and fast_mode:
        raise PortalLookupError("Выберите только один режим поиска: оптимальный или быстрый.")
    if fast_mode:
        valid_pause = parse_fast_pause(fast_pause_sec)
        if valid_pause is None:
            raise PortalLookupError("Пауза быстрой работы должна быть от 1 до 5 секунд.")
        fast_pause_sec = valid_pause
    if not browser_save.available():
        raise PortalLookupError("Автоматизация окна доступна только в Windows.")
    # OCR availability is checked before touching the browser, not midway through typing.
    if not ocr_pages.available():
        raise PortalLookupError(
            "Для управления полями сайта нужен локальный OCR. Установите requirements-ocr.txt и повторите."
        )
    warmup = _start_ocr_warmup()
    if progress:
        progress("заранее готовлю локальный OCR параллельно с открытием портала")
    window = _portal_window()
    _focus(window.handle)
    maximize = getattr(browser_save, "maximize_browser_window", None)
    if callable(maximize) and not maximize(window.handle):
        raise PortalLookupError("Не удалось развернуть окно поддерживаемого браузера на весь экран.")
    _focus(window.handle)
    # Кэш — только подсказка для OCR, никогда не используется для слепого клика.
    bounds = browser_save.window_bounds(window.handle)
    size = (bounds[2] - bounds[0], bounds[3] - bounds[1]) if bounds else None
    layout = portal_layout_cache.load(window.executable, size) if (optimal_mode or fast_mode) and size else None
    use_optimal = optimal_mode and layout is not None
    guard_layout = use_optimal
    if optimal_mode and progress:
        progress("оптимальный режим: использую сохранённую область OCR; клики проверяются по текущему экрану"
                 if use_optimal else "для этого окна нет подходящих координат — выполняю надёжный поиск")

    def check_layout_size() -> None:
        if guard_layout:
            current = browser_save.window_bounds(window.handle)
            if not current or (current[2] - current[0], current[3] - current[1]) != size:
                raise PortalLookupError("Размер окна изменился во время поиска; повторите поиск заново.")

    def fall_back_to_reliable() -> None:
        nonlocal use_optimal
        if use_optimal:
            check_layout_size()
            use_optimal = False
            if progress:
                progress("сохранённая область формы не подошла — оставшиеся действия выполняю по надёжному сценарию")

    if not any(mark in window.title.lower() for mark in PORTAL_URL_MARKS):
        if progress:
            progress("открываю NYC Property Information Portal в НОВОЙ вкладке; текущие вкладки не трогаю")
        # Ctrl+T открывает новую вкладку; текущие вкладки (например, переписка) не меняются.
        if not browser_save.press_ctrl_t():
            raise PortalLookupError("Не удалось открыть новую вкладку в браузере.")
        time.sleep(0.6)  # даём браузеру переключиться на новую вкладку
        # Печатать можно только в пустую новую вкладку. Если заголовок активной вкладки
        # не похож на пустую вкладку — ничего не печатаем, иначе текст уйдёт в чужую страницу.
        active_title = (browser_save.active_window_title() or "").lower()
        if not any(mark in active_title for mark in NEW_TAB_TITLE_MARKS):
            raise PortalLookupError(
                "После Ctrl+T активна не новая вкладка; адрес ничего не печатал. "
                "Откройте NYC Portal вручную во вкладке и повторите поиск."
            )
        # Ctrl+L выделяет адресную строку новой вкладки; Ctrl+A очищает возможный остаток.
        if (
            not browser_save.press_ctrl_l()
            or not browser_save.press_ctrl_a()
            or not browser_save.type_text(PORTAL_URL)
        ):
            raise PortalLookupError("Не удалось ввести адрес в новой вкладке браузера.")
        _key(window.handle, browser_save.VK_RETURN)
        rows = wait(
            window.handle,
            lambda screen: _contains(screen, "Select") or _contains(screen, "Find Another Property"),
            delay=OPTIMAL_PORTAL_DELAY if use_optimal else RELIABLE_PORTAL_DELAY,
            description="страницу NYC Property Information Portal",
        )
    else:
        if not fast_mode:
            time.sleep(RELIABLE_INITIAL_DELAY)
        _await_ocr(warmup)
        try:
            if fast_mode and layout is not None:
                guard_layout = True
                check_layout_size()
                if progress:
                    progress("быстрый режим: при необходимости область исходной формы возьму из общего кэша")
                rows = _initial_screen(window.handle, address_region=portal_layout_cache.label_region(layout, size))
            else:
                rows = _initial_screen(window.handle)
        except _InitialScreenNotFound:
            if fast_mode:
                raise
            time.sleep(RELIABLE_RETRY_DELAY)
            _focus(window.handle)
            rows = _initial_screen(window.handle)

    if _contains(rows, "Find Another Property"):
        check_layout_size()
        _click_text(window.handle, rows, "Find Another Property")
        rows = wait(
            window.handle, lambda screen: _contains(screen, "Select"),
            delay=RELIABLE_FORM_DELAY, description="форму поиска адреса",
        )

    if not _contains(rows, "Enter an Address"):
        # If the user left the Select menu open, do not toggle it closed. Address is
        # reached after 3 Down presses from the default selection (confirmed in the
        # field — the list has one more leading entry than it visually appears to).
        if not _has_exact_text(rows, "Address"):
            check_layout_size()
            _click_text(window.handle, rows, "Select")
            # The click opens the dropdown asynchronously (render/animation, listener
            # attachment). Sending arrow keys before it is actually open is exactly
            # how the menu was observed opening and instantly doing nothing: the
            # Down/Down/Down/Enter presses landed before anything was listening for them.
            # В обычном режиме ждём пункт списка; в быстром пользователь выбрал
            # риск и фиксированную паузу вместо промежуточной проверки.
            if fast_mode:
                _fast_wait(window.handle, fast_pause_sec)
            elif use_optimal:
                # Координаты не являются доказательством открытого меню: после выбора
                # обязательно проверяем появление метки поля до ввода адреса.
                _fast_wait(window.handle, OPTIMAL_MENU_DELAY)
                check_layout_size()
            else:
                rows = wait(
                    window.handle, lambda screen: _has_exact_text(screen, "Address"),
                    delay=RELIABLE_MENU_DELAY, description="открывшийся список Select",
                )
        _key(window.handle, browser_save.VK_DOWN)
        time.sleep(FAST_KEY_DELAY_SEC if fast_mode else 0.2)
        _key(window.handle, browser_save.VK_DOWN)
        time.sleep(FAST_KEY_DELAY_SEC if fast_mode else 0.2)
        _key(window.handle, browser_save.VK_DOWN)
        time.sleep(FAST_KEY_DELAY_SEC if fast_mode else 0.2)
        _key(window.handle, browser_save.VK_RETURN)
        if fast_mode:
            _fast_wait(window.handle, fast_pause_sec)
        else:
            rows = wait(
                window.handle, lambda screen: _contains(screen, "Enter an Address"),
                delay=OPTIMAL_ADDRESS_DELAY if use_optimal else RELIABLE_ADDRESS_DELAY, description="поля Address",
                region=portal_layout_cache.label_region(layout, size) if use_optimal else _FORM_REGION,
                on_region_fallback=fall_back_to_reliable if use_optimal else None,
            )
        # Confirming "Address" with the keyboard (not a mouse click) leaves DOM
        # focus sitting on the <select> itself even though the page has already
        # swapped to the address-entry view (confirmed by OCR in normal mode). A
        # click computed from the OCR'd label position was landing outside the
        # input — the swap can still be settling at that instant — so advance
        # focus the same way a user tabbing through the form would: Tab moves
        # off the Select and onto the address input field right next to it.
        _key(window.handle, browser_save.VK_TAB)
        time.sleep(FAST_KEY_DELAY_SEC if fast_mode else 0.2)

    check_layout_size()
    address_label = next((row for row in rows if "enter an address" in row.text.lower()), None)
    if progress:
        progress("ввожу адрес: первая подсказка не проверяется" if fast_mode else
                 "ввожу адрес в NYC Portal и жду совпадающую подсказку")
    if fast_mode and not _contains(rows, "Enter an Address"):
        _type_focused_address(window.handle, address)
    else:
        _type_into_address(window.handle, address, rows)
    if fast_mode:
        _fast_wait(window.handle, fast_pause_sec)
    else:
        picked = _wait_after_address_input(window.handle, address, rows,
                                           first_delay=OPTIMAL_SUGGESTION_DELAY if use_optimal else RELIABLE_SUGGESTION_DELAY)
        if progress and picked is not None:
            progress(f"первая подсказка портала: «{picked.text}» — выбираю её")
    check_layout_size()
    _key(window.handle, browser_save.VK_DOWN)
    time.sleep(FAST_KEY_DELAY_SEC if fast_mode else 0.2)
    _key(window.handle, browser_save.VK_RETURN)

    def card_ready(screen: Sequence[ScreenText]) -> bool:
        if not fast_mode and not use_optimal:
            # Метка карточки подтверждает её наличие, даже если OCR не прочитал числа.
            return _contains(screen, "Find Another Property")
        return all(_contains(screen, text) for text in ("Find Another Property", "Block", "Lot", "Borough"))

    def fast_card_retry() -> list[ScreenText]:
        # Только после неудачи: один отложенный снимок, без повторного ввода/кликов.
        if progress:
            progress("быстрая проверка карточки не удалась — один повтор после паузы; подсказка адреса остаётся непроверенной")
        time.sleep(RELIABLE_RETRY_DELAY)
        _focus(window.handle)
        with tempfile.TemporaryDirectory(prefix="tsai_pip_fast_retry_") as directory:
            path = Path(directory) / "screen.png"
            _save_window_capture(window.handle, path)
            retry = _screen_ocr(path)
        _reject_challenge(retry)
        if not card_ready(retry):
            raise _ScreenNotFound("Карточка не готова после повторной проверки. Увеличьте паузу или выберите надёжный режим.")
        return retry

    retried_fast_card = False
    try:
        rows = wait(
            window.handle, card_ready,
            delay=OPTIMAL_CARD_DELAY if use_optimal else RELIABLE_CARD_DELAY,
            description="карточку объекта с Borough, Block и Lot", region=None,
        )
    except _ScreenNotFound:
        if not fast_mode:
            raise
        rows = fast_card_retry()
        retried_fast_card = True
    details_source = "ocr"
    if not fast_mode and not use_optimal:
        # Сначала один финальный снимок после уменьшения карты: правая часть — карта,
        # левая часть того же снимка — OCR карточки (и, только при неудаче, Vision).
        check_layout_size()
        _zoom_out_map_pane(window.handle)
        check_layout_size()
        with tempfile.TemporaryDirectory(prefix="tsai_pip_final_") as directory:
            full_capture = Path(directory) / "portal_full.png"
            left_capture = Path(directory) / "card_left.png"
            _save_window_capture(window.handle, full_capture)
            _crop_tax_map(full_capture, rows, map_path, min_left_fraction=0.5)
            from PIL import Image  # noqa: PLC0415
            with Image.open(full_capture) as image:
                width, height = image.size
                image.crop((0, 0, width // 2, height)).convert("RGB").save(left_capture, format="PNG")
            try:
                details_rows = _screen_ocr(left_capture)
            except Exception as exc:  # noqa: BLE001 - локальный OCR сломался; допустим один Vision fallback
                details_rows = None
                ocr_error = str(exc)
            else:
                _reject_challenge(details_rows)
                ocr_error = ""
            try:
                if details_rows is None:
                    raise PortalLookupError(f"Локальный OCR не смог прочитать карточку: {ocr_error}")
                block, lot, borough = _extract_portal_details(details_rows)
                if borough not in BOROUGH_NAMES:
                    raise PortalLookupError("Локальный OCR не смог уверенно прочитать Borough на карточке.")
            except PortalLookupError as exc:
                if details_vision is None:
                    raise PortalLookupError(f"{exc} Не настроена помощь Vision для карточки.") from exc
                _focus(window.handle)  # не отправлять карточку при обнаруженной CAPTCHA
                if progress:
                    progress("локальный OCR не прочитал карточку: один запрос Vision с левой половиной снимка (возможны персональные данные)")
                block, lot, borough = details_vision(left_capture)
                details_source = "vision"
    else:
        try:
            block, lot, borough = _extract_portal_details(rows)
            if not borough:
                raise PortalLookupError("Локальный OCR не смог прочитать Borough на карточке.")
        except PortalLookupError:
            if fast_mode:
                if retried_fast_card:
                    raise
                rows = fast_card_retry()
                block, lot, borough = _extract_portal_details(rows)
                if not borough:
                    raise PortalLookupError("Локальный OCR не смог прочитать Borough на карточке.")
            else:
                if progress:
                    progress("Block/Lot не прочитаны — повторно проверяю область карточки")
                time.sleep(RELIABLE_RETRY_DELAY)
                _focus(window.handle)
                with tempfile.TemporaryDirectory(prefix="tsai_pip_bbl_retry_") as directory:
                    path = Path(directory) / "screen.png"
                    _save_window_capture(window.handle, path)
                    retry_rows = _screen_ocr_region(path, (0.0, 0.0, 0.65, 1.0))
                    _reject_challenge(retry_rows)
                    try:
                        block, lot, borough = _extract_portal_details(retry_rows)
                        if not borough:
                            raise PortalLookupError("Локальный OCR не смог прочитать Borough на карточке.")
                    except PortalLookupError:
                        # Форма могла сместиться: ещё одно чтение того же снимка, не новый снимок.
                        retry_rows = _screen_ocr(path)
                        _reject_challenge(retry_rows)
                        block, lot, borough = _extract_portal_details(retry_rows)
                        if not borough:
                            raise PortalLookupError("Локальный OCR не смог прочитать Borough на карточке.")
                # Полный предыдущий OCR сохраняет координаты для безопасной обрезки карты.
                rows = [*rows, *retry_rows]
        # Retain a map-only image: neither the saved crop nor the Vision request includes
        # the address, Block/Lot pane, owner or building details on the page's left side.
        check_layout_size()
        if fast_mode:
            _zoom_out_map_pane(window.handle, fast_pause_sec)
        else:
            _zoom_out_map_pane(window.handle)
        check_layout_size()
        with tempfile.TemporaryDirectory(prefix="tsai_pip_final_") as directory:
            full_capture = Path(directory) / "portal_full.png"
            _save_window_capture(window.handle, full_capture)
            _crop_tax_map(full_capture, rows, map_path)
    # Обновляем координаты только после успешного поиска с проверкой подсказки, а не в быстром
    # или по непроверенным предположениям. Никаких адресов/снимков в JSON нет.
    final_bounds = browser_save.window_bounds(window.handle)
    final_size = (final_bounds[2] - final_bounds[0], final_bounds[3] - final_bounds[1]) if final_bounds else None
    if not fast_mode and address_label and size and final_size == size:
        saved = portal_layout_cache.save(window.executable, size, address_label.box)
        if progress:
            progress("проверенные координаты формы сохранены для оптимального и быстрого режимов" if saved else
                     "координаты формы сохранить не удалось; поиск завершён")
    return PortalCapture(block, lot, borough, map_path, details_source)


def _zoom_out_map_pane(handle: int, redraw_wait_sec: float = MAP_REDRAW_WAIT_SEC) -> None:
    """Move cursor to the right half (map pane) and scroll down one tick to zoom out."""
    if sys.platform != "win32":
        return
    try:
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415

        user32 = ctypes.windll.user32
        rect = wintypes.RECT()
        if not user32.GetWindowRect(wintypes.HWND(handle), ctypes.byref(rect)):
            return
        width = max(1, rect.right - rect.left)
        height = max(1, rect.bottom - rect.top)
        x = rect.left + (width * 3) // 4
        y = rect.top + height // 2
        user32.SetCursorPos(int(x), int(y))
        time.sleep(0.1)
        user32.mouse_event(0x0800, 0, 0, -120, 0)  # MOUSEEVENTF_WHEEL, -120 = 1 tick back
        time.sleep(redraw_wait_sec)  # карта перерисовывается; снимок делается после этого
    except Exception:  # noqa: BLE001 - best-effort zoom out
        pass


def _integer_or_none(value: Any, name: str) -> str | None:
    if value is None or str(value).strip().lower() in {"", "null", "none", "unknown", "n/a"}:
        return None
    text = str(value).strip().replace(",", "")
    if not re.fullmatch(r"\d+", text):
        raise PortalLookupError(f"ИИ вернул нечисловое значение {name}: {text!r}.")
    return text


def _angle_or_none(value: Any) -> float | None:
    """Угол улицы из ответа Vision: число/«35°»/«35 deg» -> [0, 180), иначе None."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().lower().replace("°", " ").replace("deg", " ").strip()
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return round(front_street.normalize_line_angle(number), 1)


def parse_neighbour_result(
    data: dict[str, Any],
) -> tuple[str | None, str | None, str, str, float | None]:
    """Validate map-only Vision output: neighbour labels + front street angle."""
    left = _integer_or_none(data.get("left_neighbor_lot"), "сосед слева")
    right = _integer_or_none(data.get("right_neighbor_lot"), "сосед справа")
    if left and right and left == right:
        right = None
    confidence = str(data.get("confidence") or "не указана").strip()[:40]
    notes = str(data.get("notes") or "").strip()[:600]
    angle = _angle_or_none(data.get("front_street_angle_deg"))
    return left, right, confidence, notes, angle


VISION_SYSTEM_PROMPT = """You inspect ONLY a cropped NYC tax-map image. The selected parcel is highlighted in cyan.
Return one JSON object only with keys left_neighbor_lot, right_neighbor_lot, front_street_angle_deg, confidence, notes.
Also report front_street_angle_deg: the orientation in degrees of the street that the cyan parcel fronts onto (the street along its frontage side), measured clockwise from geographic north. North points straight up in this image. Report 0 for a street running north-south, 90 for east-west, a value from 0 to under 180; if the front street is unclear, return null for it and never guess.
Read only the lot-number labels printed inside the two parcels that directly share the opposing side/end boundaries of the cyan parcel. Report the neighbour on the visual left as left_neighbor_lot and the one on the visual right as right_neighbor_lot. Do not return Block, the selected Lot, street numbers, or any property-detail data. Do not use a rear parcel or a lot that does not share a boundary. If both side parcels are clearly identified but the map rotation makes the visual left/right assignment uncertain, still return both numbers (in either order), set confidence to low and explain that in notes. Return null only for a parcel whose label or adjacency itself is unclear. confidence is low, medium, or high. Never guess a lot number. Do not include markdown."""


DETAILS_VISION_PROMPT = """Read ONLY the visible property details on this NYC Portal card image.
Return JSON with keys borough, block, lot. Extract the exact values shown beside those labels.
Use borough's full name; block and lot must contain digits only (ignore grouping commas).
If any value is missing or unclear, return null for it. Do not guess, use map labels,
calculate, or infer values from an address. No markdown."""


def _parse_vision_details(data: dict[str, Any]) -> tuple[str, str, str]:
    if not isinstance(data, dict):
        raise PortalLookupError("Vision не вернул данные карточки в формате JSON.")
    block = _integer_or_none(data.get("block"), "Block")
    lot = _integer_or_none(data.get("lot"), "Lot")
    raw_borough = data.get("borough")
    borough = next((name for name in BOROUGH_NAMES if _normalise(name) == _normalise(raw_borough or "")), None)
    if not block or not lot or not borough:
        raise PortalLookupError("Vision не смог уверенно прочитать Borough, Block и Lot; проверьте карточку вручную.")
    return block, lot, borough


def lookup_parcel(
    address: str,
    screenshot_folder: str | Path,
    *,
    cfg: dict[str, Any],
    provider: str,
    model: str,
    api_key: str | None = None,
    key_secret: str = "",
    progress: Progress | None = None,
    fast_mode: bool = False,
    fast_pause_sec: float = FAST_SCREEN_CHECK_PAUSE_SEC,
    optimal_mode: bool = False,
) -> ParcelLookup:
    """Read BBL locally (or one card Vision fallback), then ask Vision about map neighbours."""
    clean_address = str(address or "").strip()
    if not clean_address:
        raise PortalLookupError("Введите адрес перед поиском.")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    map_path = Path(screenshot_folder) / f"TAX_MAP_{stamp}.jpg"
    request_cfg = dict(cfg)
    request_cfg["api"] = dict(cfg.get("api") or {})
    request_cfg["api"]["max_retries"] = 0
    details_usages: list[TokenUsage] = []

    def read_details_vision(left_image: Path) -> tuple[str, str, str]:
        urls, _notes = render_image_files_base64(
            [left_image], max_side_px=1800, enhance_scan=False, output_format="jpeg",
        )
        response, used = ask_llm_json(
            DETAILS_VISION_PROMPT, "Read Borough, Block and Lot from the attached card image.",
            request_cfg, image_data_urls=urls, api_key=api_key, provider=provider, model=model,
            key_secret=key_secret, progress=progress, task_label="NYC Portal: Borough, Block, Lot",
            attempts=1, single_request=True,
        )
        details_usages.append(used)
        return _parse_vision_details(response)

    driver_options = {"fast_mode": fast_mode, "fast_pause_sec": fast_pause_sec}
    if optimal_mode:
        driver_options["optimal_mode"] = True
    if not fast_mode:
        driver_options["details_vision"] = read_details_vision
    capture = _drive_portal(clean_address, map_path, progress, **driver_options)
    if progress:
        borough_text = capture.borough or "район не распознан"
        progress(
            f"{'Vision по карточке' if capture.details_source == 'vision' else 'локальный OCR'} прочитал Borough: {borough_text}, Block: {capture.block}, Lot: {capture.lot}; "
            "передаю в Vision обрезку tax map и адрес текстом"
        )
    urls, _notes = render_image_files_base64(
        [capture.map_image], max_side_px=1800, enhance_scan=False, output_format="jpeg",
    )
    response, usage = ask_llm_json(
        VISION_SYSTEM_PROMPT,
        (
            f"Search address (context only): {clean_address}. The attached image contains only the tax map.\n"
            "The selected parcel is cyan. Identify only the directly touching neighbour lot numbers "
            "on its left and right opposing side boundaries. If both are identified but left/right "
            "is uncertain due to rotation, return both anyway with confidence low. "
            "Use null only when a label or adjacency itself is unclear."
        ),
        request_cfg,
        image_data_urls=urls,
        api_key=api_key,
        provider=provider,
        model=model,
        key_secret=key_secret,
        progress=progress,
        task_label="NYC tax map: номера соседних лотов",
        attempts=1,
        single_request=True,
    )
    left, right, confidence, notes, llm_angle = parse_neighbour_result(response)
    # Тот же угол — кодом, двумя способами; все три значения клиент сравнит
    # сам (программа выбор не делает). Кэш — JSON рядом со снимком карты.
    estimates = front_street.estimate_all(capture.map_image)
    angles: dict[str, float | None] = {"llm": llm_angle, **estimates}
    cache_name = ""
    try:
        cache_path = map_angle_cache.write_angles(
            capture.map_image, llm_angle=llm_angle, python_angles=estimates,
            address=clean_address, block=capture.block, lot=capture.lot,
        )
        cache_name = cache_path.name
    except Exception:  # noqa: BLE001 - кэш не должен губить поиск участка
        cache_name = ""
    if progress:
        progress(
            "угол фронтальной улицы к северу: "
            f"ИИ — {map_angle_cache.fmt_angle(angles.get('llm'))}, "
            f"код (отрезки) — {map_angle_cache.fmt_angle(angles.get('python_hough_lines'))}, "
            f"код (градиенты) — {map_angle_cache.fmt_angle(angles.get('python_structure_tensor'))}"
            + (f"; все значения — в {cache_name}" if cache_name else "")
        )
    return ParcelLookup(
        capture.block, capture.lot, capture.borough, left, right,
        confidence, notes, capture.map_image, usage, capture.details_source,
        details_usages[0] if details_usages else None,
        llm_angle, angles,
    )
