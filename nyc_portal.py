"""Address lookup on NYC Property Information Portal for Step 0.

The automation is intentionally fail-closed: it only drives a visible window owned
by a supported browser and never follows a challenge page. RapidOCR reads Block,
Lot and Borough locally. Vision receives a cropped tax-map image only, for the two
neighbour-lot labels.
"""
from __future__ import annotations

import re
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

import browser_save
import ocr_pages
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

    @property
    def neighbours(self) -> list[str]:
        return [
            value for value in (self.left_neighbour, self.right_neighbour)
            if value is not None and value != self.lot
        ]


def _normalise(text: str) -> str:
    return "".join(char for char in str(text or "").lower() if char.isalnum())


# Пауза между проверками экрана (OCR всего окна). В «быстрой работе» проверок меньше.
SCREEN_CHECK_PAUSE_SEC = 0.6
FAST_SCREEN_CHECK_PAUSE_SEC = 5.0
#: Пределы паузы «быстрой работы», которую выбирает пользователь рядом с галочкой.
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
# После прокрутки колёсиком карта перерисовывается не сразу: снимок делаем через 0.8 с + 3 с.
MAP_REDRAW_WAIT_SEC = 0.8 + 3.0


def _screen_ocr(image_path: str | Path) -> list[ScreenText]:
    """OCR an on-disk screenshot using the same optional local RapidOCR engine."""
    engine = ocr_pages._engine_or_raise()  # one shared, lazily loaded local model
    result = engine(str(image_path))
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
    timeout: float,
    description: str,
    pause: float = SCREEN_CHECK_PAUSE_SEC,
) -> list[ScreenText]:
    """Ждёт, пока экран не удовлетворит ``predicate``; между проверками — ``pause`` секунд."""
    deadline = time.monotonic() + timeout
    latest: list[ScreenText] = []
    while True:
        _focus(handle)
        with tempfile.TemporaryDirectory(prefix="tsai_pip_ocr_") as directory:
            path = Path(directory) / "screen.png"
            _save_window_capture(handle, path)
            latest = _screen_ocr(path)
        if _challenge_visible(latest):
            raise PortalLookupError(
                "На портале отображается проверка человека/CAPTCHA. Остановился, не нажимал и не отправлял снимок в ИИ."
            )
        if predicate(latest):
            return latest
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(pause, remaining))
    raise PortalLookupError(f"Не дождался {description} на NYC Portal.")


def _type_into_address(handle: int, address: str, rows: Sequence[ScreenText]) -> None:
    # The portal label sits directly above its address autocomplete input.
    label = next((row for row in rows if "enter an address" in row.text.lower()), None)
    if label is None:
        raise PortalLookupError("После переключения режима не найдено поле Address.")
    bounds = browser_save.window_bounds(handle)
    if bounds is None:
        raise PortalLookupError("Окно портала изменилось, не могу определить поле адреса.")
    x0, y0, _x1, _y1 = bounds
    x_left, y_top, x_right, y_bottom = label.box
    click_x = x0 + x_left + min(120, max(35, (x_right - x_left) // 3))
    click_y = y0 + y_bottom + max(24, (y_bottom - y_top) * 2)
    _focus(handle)
    if not browser_save.click_browser_window(handle, click_x, click_y):
        raise PortalLookupError("Не удалось сфокусировать поле Address.")
    if not browser_save.press_ctrl_a() or not browser_save.type_text(address):
        raise PortalLookupError("Не удалось ввести адрес в NYC Portal.")


def _wait_after_address_input(handle: int, delay: float = 3.0) -> None:
    """Wait a fixed interval for autocomplete, without requiring OCR to see a suggestion.

    Take one local screenshot after the delay solely to avoid sending keyboard input to a
    human-verification/CAPTCHA screen. Whether an address suggestion is visible is not
    checked; after this guard the caller selects the first result with Down and Enter.
    """
    time.sleep(delay)
    _focus(handle)
    with tempfile.TemporaryDirectory(prefix="tsai_pip_after_input_") as directory:
        path = Path(directory) / "screen.png"
        _save_window_capture(handle, path)
        rows = _screen_ocr(path)
    if _challenge_visible(rows):
        raise PortalLookupError("Портал показал проверку/CAPTCHA; остановился без взаимодействия.")


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
        (name for name in BOROUGH_NAMES if _normalise(name) in _normalise(borough_raw)),
        borough_raw.strip() or None,
    )
    return block, lot, borough


def _crop_tax_map(source: str | Path, rows: Sequence[ScreenText], destination: str | Path) -> Path:
    """Retain/send only the map pane; exclude the address and property-details panel."""
    from PIL import Image

    try:
        image = Image.open(source).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        raise PortalLookupError(f"Не удалось открыть снимок карты: {exc}") from exc
    width, height = image.size
    # The DOF page uses a left details pane and a right map pane. OCR anchors on
    # Borough/Block/Lot ensure the crop starts beyond the details text even if the
    # responsive divider moves slightly; the 40% fallback matches the desktop portal.
    metadata_right = 0
    label_re = re.compile(r"\b(?:Borough|Block|Lot)\b", re.IGNORECASE)
    for row in rows:
        if row.box[0] < width * 0.65 and label_re.search(row.text):
            metadata_right = max(metadata_right, row.box[2])
    map_left = max(round(width * 0.40), metadata_right + 12)
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
    fast_pause_sec: float = FAST_SCREEN_CHECK_PAUSE_SEC,
) -> PortalCapture:
    """
    Use the current browser window to select an address and retain a map-only crop.

    ``fast_mode`` — «быстрая работа»: между проверками экрана пауза ``fast_pause_sec``
    (по умолчанию 5 с, пользователь выбирает от 1 до 5) вместо 0.6 с, то есть OCR всего окна
    запускается заметно реже.
    """
    pause = fast_pause_sec if fast_mode else SCREEN_CHECK_PAUSE_SEC

    def wait(handle: int, predicate: Callable[[Sequence[ScreenText]], bool], *,
             timeout: float, description: str) -> list[ScreenText]:
        return _wait_screen(
            handle, predicate, timeout=timeout, description=description, pause=pause,
        )

    if not browser_save.available():
        raise PortalLookupError("Автоматизация окна доступна только в Windows.")
    # OCR availability is checked before touching the browser, not midway through typing.
    if not ocr_pages.available():
        raise PortalLookupError(
            "Для управления полями сайта нужен локальный OCR. Установите requirements-ocr.txt и повторите."
        )
    window = _portal_window()
    _focus(window.handle)
    maximize = getattr(browser_save, "maximize_browser_window", None)
    if callable(maximize) and not maximize(window.handle):
        raise PortalLookupError("Не удалось развернуть окно поддерживаемого браузера на весь экран.")
    _focus(window.handle)
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
            timeout=45,
            description="страницу NYC Property Information Portal",
        )
    else:
        with tempfile.TemporaryDirectory(prefix="tsai_pip_start_") as directory:
            path = Path(directory) / "screen.png"
            _save_window_capture(window.handle, path)
            # Before hovering/clicking the Select control, cheaply confirm we are
            # already on the portal (and not a CAPTCHA) by OCR-ing only the upper
            # left quarter of the screen; this avoids the cost of a full-page OCR
            # for the common failure cases (challenge page, wrong tab/page).
            quick_rows = _screen_ocr_top_left_quarter(path)
            if _challenge_visible(quick_rows) or browser_save.looks_like_cloudflare_page(window.title):
                raise PortalLookupError(
                    "На портале отображается проверка человека/CAPTCHA. "
                    "Остановился, не нажимал и не отправлял снимок в ИИ."
                )
            if not any(
                _contains(quick_rows, marker)
                for marker in ("Select", "Find Another Property", "Enter an Address")
            ):
                raise PortalLookupError(
                    "По верхней левой четверти экрана не удалось подтвердить страницу NYC Portal; "
                    "ничего не нажимал. Разверните страницу портала и повторите."
                )
            # Confirmed we are on the portal: OCR the full page to get accurate
            # coordinates for the Select control and any other on-screen text.
            rows = _screen_ocr(path)

    if _contains(rows, "Find Another Property"):
        _click_text(window.handle, rows, "Find Another Property")
        rows = wait(
            window.handle, lambda screen: _contains(screen, "Select"),
            timeout=12, description="форму поиска адреса",
        )

    if not _contains(rows, "Enter an Address"):
        # If the user left the Select menu open, do not toggle it closed. Address is
        # reached after 3 Down presses from the default selection (confirmed in the
        # field — the list has one more leading entry than it visually appears to).
        if not _has_exact_text(rows, "Address"):
            _click_text(window.handle, rows, "Select")
            # The click opens the dropdown asynchronously (render/animation, listener
            # attachment). Sending arrow keys before it is actually open is exactly
            # how the menu was observed opening and instantly doing nothing: the
            # Down/Down/Down/Enter presses landed before anything was listening for them.
            # Confirm the option list is visible (its "Address" row appears) before
            # driving it with the keyboard, instead of guessing with a fixed delay.
            rows = wait(
                window.handle, lambda screen: _has_exact_text(screen, "Address"),
                timeout=5, description="открывшийся список Select",
            )
        _key(window.handle, browser_save.VK_DOWN)
        time.sleep(0.2)
        _key(window.handle, browser_save.VK_DOWN)
        time.sleep(0.2)
        _key(window.handle, browser_save.VK_DOWN)
        time.sleep(0.2)
        _key(window.handle, browser_save.VK_RETURN)
        rows = wait(
            window.handle, lambda screen: _contains(screen, "Enter an Address"),
            timeout=8, description="поля Address",
        )
        # Confirming "Address" with the keyboard (not a mouse click) leaves DOM
        # focus sitting on the <select> itself even though the page has already
        # swapped to the address-entry view (just confirmed above by OCR). A
        # click computed from the OCR'd label position was landing outside the
        # input — the swap can still be settling at that instant — so advance
        # focus the same way a user tabbing through the form would: Tab moves
        # off the Select and onto the address input field right next to it.
        _key(window.handle, browser_save.VK_TAB)
        time.sleep(0.2)

    if progress:
        progress("ввожу адрес в NYC Portal и жду совпадающую подсказку")
    _type_into_address(window.handle, address, rows)
    _wait_after_address_input(window.handle)
    _key(window.handle, browser_save.VK_DOWN)
    time.sleep(0.2)
    _key(window.handle, browser_save.VK_RETURN)
    rows = wait(
        window.handle,
        lambda screen: (
            _contains(screen, "Find Another Property")
            and _contains(screen, "Block") and _contains(screen, "Lot")
            and _contains(screen, "Borough")
        ),
        timeout=30,
        description="карточку объекта с Borough, Block и Lot",
    )
    block, lot, borough = _extract_portal_details(rows)
    # Retain a map-only image: neither the saved crop nor the Vision request includes
    # the address, Block/Lot pane, owner or building details on the page's left side.
    _zoom_out_map_pane(window.handle)
    with tempfile.TemporaryDirectory(prefix="tsai_pip_final_") as directory:
        full_capture = Path(directory) / "portal_full.png"
        _save_window_capture(window.handle, full_capture)
        _crop_tax_map(full_capture, rows, map_path)
    return PortalCapture(block, lot, borough, map_path)


def _zoom_out_map_pane(handle: int) -> None:
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
        time.sleep(MAP_REDRAW_WAIT_SEC)  # карта перерисовывается; снимок делается после этого
    except Exception:  # noqa: BLE001 - best-effort zoom out
        pass


def _integer_or_none(value: Any, name: str) -> str | None:
    if value is None or str(value).strip().lower() in {"", "null", "none", "unknown", "n/a"}:
        return None
    text = str(value).strip().replace(",", "")
    if not re.fullmatch(r"\d+", text):
        raise PortalLookupError(f"ИИ вернул нечисловое значение {name}: {text!r}.")
    return text


def parse_neighbour_result(
    data: dict[str, Any],
) -> tuple[str | None, str | None, str, str]:
    """Validate map-only Vision output: two adjacent lot labels, without BBL fields."""
    left = _integer_or_none(data.get("left_neighbor_lot"), "сосед слева")
    right = _integer_or_none(data.get("right_neighbor_lot"), "сосед справа")
    if left and right and left == right:
        right = None
    confidence = str(data.get("confidence") or "не указана").strip()[:40]
    notes = str(data.get("notes") or "").strip()[:600]
    return left, right, confidence, notes


VISION_SYSTEM_PROMPT = """You inspect ONLY a cropped NYC tax-map image. The selected parcel is highlighted in cyan.
Return one JSON object only with keys left_neighbor_lot, right_neighbor_lot, confidence, notes.
Read only the lot-number labels printed inside the two parcels that directly share the opposing side/end boundaries of the cyan parcel. Report the neighbour on the visual left as left_neighbor_lot and the one on the visual right as right_neighbor_lot. Do not return Block, the selected Lot, street numbers, or any property-detail data. Do not use a rear parcel or a lot that does not share a boundary. If the map is rotated or labels/boundaries are unclear, return null for uncertain neighbours and explain briefly in notes. confidence is low, medium, or high. Never guess. Do not include markdown."""


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
) -> ParcelLookup:
    """Navigate the portal, read BBL locally, then ask Vision only about map neighbours."""
    clean_address = str(address or "").strip()
    if not clean_address:
        raise PortalLookupError("Введите адрес перед поиском.")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    map_path = Path(screenshot_folder) / f"TAX_MAP_{stamp}.jpg"
    capture = _drive_portal(
        clean_address, map_path, progress, fast_mode=fast_mode, fast_pause_sec=fast_pause_sec,
    )
    if progress:
        borough_text = capture.borough or "район не распознан"
        progress(
            f"локальный OCR прочитал Borough: {borough_text}, Block: {capture.block}, Lot: {capture.lot}; "
            "передаю в Vision только обрезку tax map"
        )
    urls, _notes = render_image_files_base64(
        [capture.map_image], max_side_px=1800, enhance_scan=False, output_format="jpeg",
    )
    request_cfg = dict(cfg)
    request_cfg["api"] = dict(cfg.get("api") or {})
    request_cfg["api"]["max_retries"] = 0
    response, usage = ask_llm_json(
        VISION_SYSTEM_PROMPT,
        (
            f"Search address (context only): {clean_address}. The attached image contains only the tax map.\n"
            "The selected parcel is cyan. Identify only the directly touching neighbour lot numbers "
            "on its left and right opposing side boundaries. If a label or adjacency is unclear, use null."
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
    left, right, confidence, notes = parse_neighbour_result(response)
    return ParcelLookup(
        capture.block, capture.lot, capture.borough, left, right,
        confidence, notes, capture.map_image, usage,
    )
