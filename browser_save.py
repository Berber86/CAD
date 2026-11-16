"""
Шаг 0: автоматическое «Ctrl+S» для PDF, открытого в браузере (Windows).

Зачем. Проверка Cloudflare пропускает браузер, но PDF в нём открывается во
встроенном просмотрщике — файл на диск не попадает, пока человек не нажмёт
Ctrl+S. Заказчик делает именно это («не хватило лишь автоматического прожатия
ctrl+S»), поэтому программа делает это сама:

1. находит окно браузера, в заголовке которого видно PDF или адрес клерка;
2. поднимает его наверх и посылает **Ctrl+S** (обычная эмуляция клавиатуры
   Windows — та же, что делает человек);
3. если открылось окно «Сохранить как» — вписывает полный путь и жмёт Enter;
4. если браузер сохраняет молча в «Загрузки» — файл найдёт обычное ожидание
   загрузок (`rcclerk.find_new_pdf`).

Всё это необязательная надстройка: если окно не нашлось, браузер не тот или
система не Windows, шаг просто не срабатывает, и остаётся прежний путь —
человек нажимает Ctrl+S сам. Никаких «своих» файлов, кроме одного с понятным
именем ``tsai_step0_<номер>.pdf``, программа не создаёт.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

#: Имя файла, который программа просит сохранить (его же и удаляет после).
PENDING_PREFIX = "tsai_step0_"

#: Что считаем окном с PDF: заголовок с расширением, просмотрщиком или адресом клерка.
PDF_TITLE_HINTS = (".pdf", "viewcontent", ".html", "richmondcountyclerk", "document")
#: Заголовки окна сохранения на английском и русском Windows.
SAVE_DIALOG_HINTS = (
    "save as", "save a copy", "saving", "сохранить как", "сохранение", "сохранить",
)

VK_CONTROL = 0x11
VK_S = 0x53
VK_RETURN = 0x0D
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004


@dataclass(frozen=True)
class BrowserWindow:
    """Верхнее окно Windows: дескриптор и заголовок."""

    handle: int
    title: str


def looks_like_pdf_window(title: str, hints: Iterable[str] = PDF_TITLE_HINTS) -> bool:
    """Заголовок похож на открытый PDF (или на страницу клерка)?"""
    lowered = str(title or "").lower()
    return any(hint in lowered for hint in hints)


def looks_like_save_dialog(title: str) -> bool:
    """Активное окно — диалог сохранения файла?"""
    lowered = str(title or "").lower()
    return any(hint in lowered for hint in SAVE_DIALOG_HINTS)


# ---------------------------------------------------------------------------
# Тонкий слой Windows: всё, что зависит от системы, собрано здесь
# ---------------------------------------------------------------------------


def available() -> bool:
    """Есть ли Windows-API для эмуляции клавиатуры (в тестах и на Linux — нет)."""
    return os.name == "nt" and _user32() is not None


def _user32():  # noqa: ANN202 - ctypes-объект без типа
    if os.name != "nt":
        return None
    try:
        import ctypes

        return ctypes.WinDLL("user32", use_last_error=True)
    except Exception:  # noqa: BLE001 - нет windll или политика запрещает
        return None


def list_windows() -> list[BrowserWindow]:
    """Все видимые окна с заголовком (порядок — как отдаёт Windows)."""
    user32 = _user32()
    if user32 is None:
        return []
    import ctypes
    from ctypes import wintypes

    result: list[BrowserWindow] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def collect(handle, _param):  # noqa: ANN001 - сигнатура EnumWindows
        if not user32.IsWindowVisible(handle):
            return True
        length = user32.GetWindowTextLengthW(handle)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(handle, buffer, length + 1)
        if buffer.value.strip():
            result.append(BrowserWindow(handle=int(handle), title=buffer.value))
        return True

    try:
        user32.EnumWindows(callback_type(collect), 0)
    except Exception:  # noqa: BLE001
        return result
    return result


def foreground(handle: int) -> bool:
    """Поднимает окно: человек увидел бы то же самое, что при клике по панели задач."""
    user32 = _user32()
    if user32 is None:
        return False
    try:
        user32.ShowWindow(handle, 9)      # SW_RESTORE
        user32.SetForegroundWindow(handle)
        return True
    except Exception:  # noqa: BLE001
        return False


def _key(vk: int, *, up: bool) -> None:
    user32 = _user32()
    if user32 is None:
        return
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP if up else 0, 0)


def press_ctrl_s() -> bool:
    """Ctrl+S в активном окне."""
    user32 = _user32()
    if user32 is None:
        return False
    try:
        _key(VK_CONTROL, up=False)
        _key(VK_S, up=False)
        _key(VK_S, up=True)
        _key(VK_CONTROL, up=True)
        return True
    except Exception:  # noqa: BLE001
        return False


def press_enter() -> bool:
    user32 = _user32()
    if user32 is None:
        return False
    try:
        _key(VK_RETURN, up=False)
        _key(VK_RETURN, up=True)
        return True
    except Exception:  # noqa: BLE001
        return False


def type_text(text: str) -> bool:
    """Ввод строки как с клавиатуры (Unicode — путь с кириллицей тоже подойдёт)."""
    user32 = _user32()
    if user32 is None:
        return False
    try:
        for char in str(text):
            user32.keybd_event(0, ord(char), KEYEVENTF_UNICODE, 0)
            user32.keybd_event(0, ord(char), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0)
        return True
    except Exception:  # noqa: BLE001
        return False


def active_window_title() -> str:
    user32 = _user32()
    if user32 is None:
        return ""
    try:
        import ctypes

        handle = user32.GetForegroundWindow()
        length = user32.GetWindowTextLengthW(handle)
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(handle, buffer, length + 1)
        return buffer.value
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# Сам сценарий: найти окно, нажать Ctrl+S, при диалоге — вписать путь
# ---------------------------------------------------------------------------


def pending_path(downloads_dir: str | Path, endorsement_id: int | str) -> Path:
    """Куда просить браузер сохранить файл: «Загрузки» с нашим понятным именем."""
    return Path(downloads_dir) / f"{PENDING_PREFIX}{endorsement_id}.pdf"


def save_pdf_if_possible(
    target: str | Path,
    *,
    attempts: int = 3,
    wait: float = 0.7,
    windows: Optional[Callable[[], list[BrowserWindow]]] = None,
    foreground_fn: Optional[Callable[[int], bool]] = None,
    press_ctrl_s_fn: Optional[Callable[[], bool]] = None,
    dialog_open_fn: Optional[Callable[[], bool]] = None,
    type_text_fn: Optional[Callable[[str], bool]] = None,
    press_enter_fn: Optional[Callable[[], bool]] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Optional[Path]:
    """
    Просит браузер сохранить открытый PDF в ``target``.

    Возвращает путь, если файл появился, и ``None`` во всех остальных случаях
    (нет окна, браузер сохранил молча в «Загрузки», система не Windows).
    Никаких исключений наружу: это надстройка, а не обязательный шаг.
    """
    destination = Path(target)
    find_windows = windows or list_windows
    bring = foreground_fn or foreground
    press = press_ctrl_s_fn or press_ctrl_s
    dialog_open = dialog_open_fn or (lambda: looks_like_save_dialog(active_window_title()))
    write = type_text_fn or type_text
    enter = press_enter_fn or press_enter

    candidate: Optional[BrowserWindow] = None
    try:
        for window in find_windows():
            if looks_like_pdf_window(window.title):
                candidate = window
                break
    except Exception:  # noqa: BLE001
        return None
    if candidate is None:
        return None

    for _attempt in range(max(1, int(attempts))):
        try:
            bring(candidate.handle)
            press()
        except Exception:  # noqa: BLE001
            return None
        sleep(wait)
        if destination.is_file():
            return destination
        try:
            opened_dialog = dialog_open()
        except Exception:  # noqa: BLE001
            opened_dialog = False
        if opened_dialog:
            # Диалог сохранения: вписываем полный путь и подтверждаем.
            write(str(destination))
            sleep(0.2)
            enter()
            sleep(wait)
            if destination.is_file():
                return destination
    return None


def auto_save_hook(downloads_dir: str | Path):
    """
    Готовый хук для лестницы скачивания: ``auto_save(endorsement_id) -> Path | None``.

    Если Windows-API недоступно, хук сразу возвращает ``None`` — лестница работает
    как раньше, и человек при желании нажимает Ctrl+S сам.
    """

    def attempt(endorsement_id: int | str) -> Optional[Path]:
        if not available():
            return None
        target = pending_path(downloads_dir, endorsement_id)
        return save_pdf_if_possible(target)

    return attempt
