"""Безопасное взаимодействие с окнами браузера (только Windows).

Portal automation uses only a verified supported-browser window; Ctrl+S remains
stricter and additionally requires a loaded PDF title. Foxit/FinReader and other
PDF programs are never activated, and a download gets at most one Ctrl+S attempt.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

PENDING_PREFIX = "tsai_step0_"
VK_CONTROL = 0x11
VK_S = 0x53
VK_RETURN = 0x0D
VK_TAB = 0x09
VK_DOWN = 0x28
VK_A = 0x41
VK_T = 0x54
VK_L = 0x4C
VK_DELETE = 0x2E
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
KEYEVENTF_UNICODE = 0x0004

# Только реальные браузеры. Yandex использует browser.exe; узнаём его по пути.
BROWSER_EXECUTABLES = {
    "chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "opera_gx.exe",
    "brave.exe", "vivaldi.exe", "waterfox.exe",
}
CLOUDFLARE_TITLE_MARKS = (
    "just a moment", "one moment", "please wait", "checking your browser",
    "verifying", "cloudflare", "captcha", "challenge", "security check",
    "access denied", "verify you are human", "attention required",
    "проверка", "проверяем", "подождите", "одну минуту", "безопасност",
    "подтвердите, что вы человек", "робот",
)
PDF_TITLE_MARKS = (".pdf", "pdf viewer", "pdf.js viewer")
SAVE_DIALOG_MARKS = (
    "save as", "save a copy", "save print output as", "сохранить как",
    "сохранить копию", "сохранение", "download file",
)


@dataclass(frozen=True)
class BrowserWindow:
    handle: int
    title: str
    executable: str = ""
    image_path: str = ""


def is_supported_browser(executable: str, image_path: str = "") -> bool:
    """Узнаёт браузер по exe, а Yandex Browser — по его установочному пути."""
    name = str(executable or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name in BROWSER_EXECUTABLES:
        return True
    return name == "browser.exe" and "yandex" in str(image_path or "").lower()


def looks_like_cloudflare_page(title: str) -> bool:
    lowered = str(title or "").lower()
    return any(mark in lowered for mark in CLOUDFLARE_TITLE_MARKS)


def looks_like_loaded_pdf(title: str) -> bool:
    """Заголовок PDF-вкладки. Вьюер клерка на скриншоте Николая называется viewer."""
    lowered = str(title or "").strip().lower()
    if not lowered or looks_like_cloudflare_page(lowered):
        return False
    if any(mark in lowered for mark in PDF_TITLE_MARKS):
        return True
    # На скриншоте Yandex заголовок вкладки ровно «viewer»; допускаем добавленное
    # браузером имя только после разделителя, не любое упоминание слова в заголовке.
    if lowered == "viewer" or lowered.startswith(("viewer - ", "viewer — ", "viewer | ")):
        return True
    return False


def looks_like_save_dialog(title: str) -> bool:
    lowered = str(title or "").lower()
    return any(mark in lowered for mark in SAVE_DIALOG_MARKS)


def available() -> bool:
    return os.name == "nt" and _user32() is not None


def _user32():  # noqa: ANN202 - ctypes object
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        # Declare pointer-sized HWND signatures explicitly: ctypes otherwise assumes
        # a 32-bit int return/argument on 64-bit Windows and can truncate window handles.
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        user32.ShowWindow.restype = wintypes.BOOL
        user32.IsIconic.argtypes = (wintypes.HWND,)
        user32.IsIconic.restype = wintypes.BOOL
        user32.BringWindowToTop.argtypes = (wintypes.HWND,)
        user32.BringWindowToTop.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.SetActiveWindow.argtypes = (wintypes.HWND,)
        user32.SetActiveWindow.restype = wintypes.HWND
        user32.SetFocus.argtypes = (wintypes.HWND,)
        user32.SetFocus.restype = wintypes.HWND
        user32.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
        user32.AttachThreadInput.restype = wintypes.BOOL
        user32.AllowSetForegroundWindow.argtypes = (wintypes.DWORD,)
        user32.AllowSetForegroundWindow.restype = wintypes.BOOL
        user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = (wintypes.HWND,)
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
        user32.SetCursorPos.restype = wintypes.BOOL
        user32.mouse_event.argtypes = (
            wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t,
        )
        user32.keybd_event.argtypes = (wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t)
        return user32
    except Exception:  # noqa: BLE001
        return None


def _process_image_path(pid: int) -> str:
    """Полный exe-путь процесса, владеющего окном; при недоступности — пусто."""
    if os.name != "nt":
        return ""
    import ctypes
    from ctypes import wintypes

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.QueryFullProcessImageNameW.argtypes = (
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
        )
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        process = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not process:
            return ""
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
                return buffer.value
        finally:
            kernel32.CloseHandle(process)
    except Exception:  # noqa: BLE001 - no process access means fail closed
        return ""
    return ""


def list_windows() -> list[BrowserWindow]:
    """Видимые top-level окна с заголовком и владельцем-процессом."""
    user32 = _user32()
    if user32 is None:
        return []
    import ctypes
    from ctypes import wintypes

    result: list[BrowserWindow] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = (callback_type, wintypes.LPARAM)
    user32.EnumWindows.restype = wintypes.BOOL

    def collect(handle, _param):  # noqa: ANN001 - EnumWindows callback
        if not user32.IsWindowVisible(handle):
            return True
        length = user32.GetWindowTextLengthW(handle)
        if length <= 0:
            return True
        title_buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(handle, title_buffer, length + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        path = _process_image_path(pid.value)
        if title_buffer.value.strip() and path:
            result.append(BrowserWindow(
                handle=int(handle), title=title_buffer.value,
                executable=Path(path).name, image_path=path,
            ))
        return True

    try:
        user32.EnumWindows(callback_type(collect), 0)
    except Exception:  # noqa: BLE001
        return result
    return result


def find_pdf_browser_window(windows: Optional[list[BrowserWindow]] = None, *,
                            require_title: bool = True) -> Optional[BrowserWindow]:
    """Fail-closed: только поддерживаемый браузер с загруженным PDF, не challenge.

    ``require_title=False`` — слепая попытка в уже закреплённое хуком окно:
    заголовок просмотрщика не обязан совпасть с известными шаблонами;
    страхует не заголовок, а итоговая проверка настоящего PDF в «Загрузках».
    """
    try:
        candidates = list_windows() if windows is None else windows
    except Exception:  # noqa: BLE001
        return None
    for window in candidates:
        if not is_supported_browser(window.executable, window.image_path):
            continue
        if require_title and not looks_like_loaded_pdf(window.title):
            continue
        return window
    return None


def _same_owning_process(user32, handle_a: int, handle_b: int) -> bool:
    """True when both window handles belong to the same OS process."""
    import ctypes
    from ctypes import wintypes

    pid_a = wintypes.DWORD()
    pid_b = wintypes.DWORD()
    user32.GetWindowThreadProcessId(handle_a, ctypes.byref(pid_a))
    user32.GetWindowThreadProcessId(handle_b, ctypes.byref(pid_b))
    return bool(pid_a.value) and pid_a.value == pid_b.value


def foreground_is_acceptable(handle: int) -> bool:
    """
    True when ``handle`` is foreground, or the current foreground window belongs
    to the same process (an open ``<select>`` dropdown, autocomplete list or
    similar transient popup renders as its own top-level window owned by the
    same browser process). Callers that only need to know whether it is safe to
    keep sending input should use this instead of an exact handle comparison,
    which would otherwise treat a just-opened popup as "not focused".
    """
    user32 = _user32()
    if user32 is None:
        return False
    try:
        current = int(user32.GetForegroundWindow() or 0)
    except Exception:  # noqa: BLE001
        return False
    if current == int(handle):
        return True
    return bool(current) and _same_owning_process(user32, current, int(handle))


def foreground(handle: int, *, timeout: float = 1.5) -> bool:
    """Activate the exact supported browser and wait for Windows to grant focus."""
    if not any(
        window.handle == int(handle)
        and is_supported_browser(window.executable, window.image_path)
        for window in list_windows()
    ):
        return False
    user32 = _user32()
    if user32 is None:
        return False
    # If this browser (or one of its own transient popups, e.g. an open <select>
    # dropdown) is already foreground, preserve focus inside its web page.
    # Reapplying SetFocus/SetForegroundWindow to the top-level Chrome HWND here
    # would steal activation from that popup and close it immediately.
    try:
        if foreground_is_acceptable(handle):
            return True
    except Exception:  # noqa: BLE001
        pass
    attached_threads: list[int] = []
    try:
        import ctypes
        from ctypes import wintypes

        # Do not restore a maximized browser to its small remembered window size on
        # every keystroke. Restore only when minimized.
        if user32.IsIconic(handle):
            user32.ShowWindow(handle, 9)  # SW_RESTORE

        foreground_handle = int(user32.GetForegroundWindow() or 0)
        foreground_pid = wintypes.DWORD()
        foreground_thread = (
            int(user32.GetWindowThreadProcessId(foreground_handle, ctypes.byref(foreground_pid)))
            if foreground_handle else 0
        )
        target_pid = wintypes.DWORD()
        target_thread = int(user32.GetWindowThreadProcessId(handle, ctypes.byref(target_pid)))
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        try:
            kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        except AttributeError:  # simple callable doubles in non-Windows tests
            pass
        current_thread = int(kernel32.GetCurrentThreadId())

        # The portal runs from a worker thread. Temporarily attach its input queue to
        # the UI/foreground and browser queues so Windows permits a deliberate,
        # verified foreground switch; always detach before returning.
        for thread_id in dict.fromkeys((foreground_thread, target_thread)):
            if thread_id and thread_id != current_thread and user32.AttachThreadInput(
                current_thread, thread_id, True,
            ):
                attached_threads.append(thread_id)

        # Let the target browser process become foreground when the current app owns
        # the active window; this does not send keys or activate any other process.
        if foreground_handle:
            user32.AllowSetForegroundWindow(int(target_pid.value))

        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            user32.BringWindowToTop(handle)
            user32.SetForegroundWindow(handle)
            if int(user32.GetForegroundWindow() or 0) == int(handle):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
    except Exception:  # noqa: BLE001
        return False
    finally:
        for thread_id in reversed(attached_threads):
            try:
                user32.AttachThreadInput(current_thread, thread_id, False)
            except Exception:  # noqa: BLE001 - best-effort input queue cleanup
                pass


def maximize_browser_window(handle: int) -> bool:
    """Maximize a verified supported-browser window without opening another tab/window."""
    if not any(
        window.handle == int(handle)
        and is_supported_browser(window.executable, window.image_path)
        for window in list_windows()
    ):
        return False
    user32 = _user32()
    if user32 is None:
        return False
    try:
        user32.ShowWindow(handle, 3)  # SW_MAXIMIZE
        return int(user32.GetForegroundWindow()) == int(handle)
    except Exception:  # noqa: BLE001
        return False


def active_window_handle() -> int:
    user32 = _user32()
    if user32 is None:
        return 0
    try:
        return int(user32.GetForegroundWindow())
    except Exception:  # noqa: BLE001
        return 0


def window_bounds(handle: int) -> tuple[int, int, int, int] | None:
    """Bounds of the exact supported-browser window, in desktop coordinates."""
    if not any(
        window.handle == int(handle)
        and is_supported_browser(window.executable, window.image_path)
        for window in list_windows()
    ):
        return None
    user32 = _user32()
    if user32 is None:
        return None
    try:
        import ctypes
        from ctypes import wintypes
        rect = wintypes.RECT()
        if not user32.GetWindowRect(int(handle), ctypes.byref(rect)):
            return None
        if rect.right <= rect.left or rect.bottom <= rect.top:
            return None
        return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
    except Exception:  # noqa: BLE001 - fail closed if window geometry is unavailable
        return None


def click_browser_window(handle: int, x: int, y: int) -> bool:
    """Click only inside the currently foreground supported browser window."""
    if active_window_handle() != int(handle) or window_bounds(handle) is None:
        return False
    user32 = _user32()
    if user32 is None:
        return False
    try:
        target_x, target_y = int(x), int(y)
        if user32.SetCursorPos(target_x, target_y) is False:
            return False
        # Курсор обязан реально стоять на цели до нажатия. На удалённом
        # рабочем столе и при «плавном» указателе SetCursorPos может
        # отставать: нажатие в движении превращается в протаскивание и
        # выделение текста вместо клика (живой сбой 25.11.2026: клик по
        # форме портала выделял всю панель синим). Курсор не на месте —
        # клика нет вовсе, вызывающий код попробует другую стратегию и
        # точно не нажмёт Ctrl+A вслепую.
        get_cursor_pos = getattr(user32, "GetCursorPos", None)
        if callable(get_cursor_pos):
            import ctypes
            from ctypes import wintypes

            class POINT(ctypes.Structure):
                _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

            def cursor_at_target() -> bool:
                point = POINT()
                try:
                    if not get_cursor_pos(ctypes.byref(point)):
                        return False
                except Exception:  # noqa: BLE001
                    return False
                return abs(int(point.x) - target_x) <= 2 and abs(int(point.y) - target_y) <= 2

            settled = False
            for attempt_no in range(4):
                if cursor_at_target():
                    settled = True
                    break
                if attempt_no == 2:
                    user32.SetCursorPos(target_x, target_y)
                time.sleep(0.02)
            if not settled:
                return False
        # SendInput is more reliable than the legacy mouse_event API for modern
        # Chromium windows. Down и up уходят раздельными событиями с паузой:
        # пара «в одном пакете» на медленной сессии может слипнуться с ещё
        # движущимся курсором в выделение. Do not fall back after a partial
        # SendInput: that could turn one intended click into a down/up pair
        # that toggles a Select twice.
        sendinput_available = False
        try:
            import ctypes
            from ctypes import wintypes

            class MOUSEINPUT(ctypes.Structure):
                _fields_ = [
                    ("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t),
                ]

            class INPUTUNION(ctypes.Union):
                _fields_ = [("mi", MOUSEINPUT)]

            class INPUT(ctypes.Structure):
                _anonymous_ = ("union",)
                _fields_ = [("type", wintypes.DWORD), ("union", INPUTUNION)]

            send_input = user32.SendInput
            send_input.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
            send_input.restype = wintypes.UINT

            def send_mouse(flags: int) -> int:
                inputs = (INPUT * 1)(INPUT(type=0, mi=MOUSEINPUT(dwFlags=flags)))
                return int(send_input(1, inputs, ctypes.sizeof(INPUT)))

            sendinput_available = True
            if send_mouse(MOUSEEVENTF_LEFTDOWN) != 1:
                return False  # partial click; never issue a second click as fallback
            time.sleep(0.05)
            if send_mouse(MOUSEEVENTF_LEFTUP) != 1:
                return False
            return active_window_handle() == int(handle)
        except Exception:  # noqa: BLE001 - legacy fallback below
            if sendinput_available:
                return False
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.05)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        return active_window_handle() == int(handle)
    except Exception:  # noqa: BLE001
        return False


def _key(vk: int, *, up: bool) -> None:
    user32 = _user32()
    if user32 is not None:
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP if up else 0, 0)


def press_ctrl_s() -> bool:
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


def press_key(vk: int) -> bool:
    """Send one key to the foreground window; callers must validate its process first."""
    if _user32() is None:
        return False
    try:
        _key(int(vk), up=False)
        _key(int(vk), up=True)
        return True
    except Exception:  # noqa: BLE001
        return False


def press_ctrl_l() -> bool:
    """Focus the address bar in the foreground browser tab."""
    if _user32() is None:
        return False
    try:
        _key(VK_CONTROL, up=False)
        _key(VK_L, up=False)
        _key(VK_L, up=True)
        _key(VK_CONTROL, up=True)
        return True
    except Exception:  # noqa: BLE001
        return False


def press_ctrl_t() -> bool:
    """Open a new browser tab (the current tabs are left untouched)."""
    if _user32() is None:
        return False
    try:
        _key(VK_CONTROL, up=False)
        _key(VK_T, up=False)
        _key(VK_T, up=True)
        _key(VK_CONTROL, up=True)
        return True
    except Exception:  # noqa: BLE001
        return False


def press_ctrl_a() -> bool:
    """Select the focused browser field contents."""
    if _user32() is None:
        return False
    try:
        _key(VK_CONTROL, up=False)
        _key(VK_A, up=False)
        _key(VK_A, up=True)
        _key(VK_CONTROL, up=True)
        return True
    except Exception:  # noqa: BLE001
        return False


def type_text(text: str) -> bool:
    """Ввод Unicode-пути в появившийся диалог «Сохранить как»."""
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


def pending_path(downloads_dir: str | Path, endorsement_id: int | str) -> Path:
    return Path(downloads_dir) / f"{PENDING_PREFIX}{endorsement_id}.pdf"


def save_pdf_if_possible(
    target: str | Path,
    *,
    windows: Optional[Callable[[], list[BrowserWindow]]] = None,
    foreground_fn: Optional[Callable[[int], bool]] = None,
    active_handle_fn: Optional[Callable[[], int]] = None,
    press_ctrl_s_fn: Optional[Callable[[], bool]] = None,
    dialog_open_fn: Optional[Callable[[], bool]] = None,
    type_text_fn: Optional[Callable[[str], bool]] = None,
    press_enter_fn: Optional[Callable[[], bool]] = None,
    sleep: Callable[[float], None] = time.sleep,
    wait: float = 0.8,
    require_pdf_title: bool = True,
) -> Optional[Path]:
    """Одна безопасная попытка Ctrl+S только в уже загруженном PDF браузера."""
    destination = Path(target)
    if destination.is_file():
        return destination
    try:
        candidate = find_pdf_browser_window((windows or list_windows)(),
                                            require_title=require_pdf_title)
    except Exception:  # noqa: BLE001
        return None
    if candidate is None:
        return None

    bring = foreground_fn or foreground
    if not bring(candidate.handle):
        return None
    # Защита от смены фокуса между активацией браузера и эмуляцией клавиш.
    active_handle = active_handle_fn or active_window_handle
    if int(active_handle()) != int(candidate.handle):
        return None
    press = press_ctrl_s_fn or press_ctrl_s
    if not press():
        return None
    sleep(wait)
    if destination.is_file():
        return destination

    dialog_open = dialog_open_fn or (lambda: looks_like_save_dialog(active_window_title()))
    try:
        opened_dialog = dialog_open()
    except Exception:  # noqa: BLE001
        opened_dialog = False
    if not opened_dialog:
        return None

    write = type_text_fn or type_text
    enter = press_enter_fn or press_enter
    if not write(str(destination)):
        return None
    sleep(0.2)
    if not enter():
        return None
    sleep(max(wait, 1.0))
    return destination if destination.is_file() else None


def auto_save_hook(
    downloads_dir: str | Path,
    *,
    on_attempt: Optional[Callable[[str], None]] = None,
    ready_delay: float = 4.0,
    blind_after: float = 15.0,
    stable_for: float = 1.0,
    windows_fn: Callable[[], list[BrowserWindow]] = list_windows,
    active_handle_fn: Callable[[], int] = active_window_handle,
    monotonic: Callable[[], float] = time.monotonic,
):
    """Хук для лестницы: ждёт готовый PDF после старта навигации и на документ
    посылает Ctrl+S только один раз. Cloudflare-title не соответствует PDF-title.
    """
    attempted: set[str] = set()
    requested_at: dict[str, float] = {}
    baselines: dict[str, dict[int, str]] = {}
    target_handles: dict[str, int] = {}
    ready_since: dict[str, tuple[int, str, float]] = {}
    state: dict[str, str] = {}

    def before_open(endorsement_id: int | str) -> None:
        """Snapshot existing browser tabs before the new document is navigated."""
        key = str(endorsement_id)
        requested_at[key] = monotonic()
        try:
            baselines[key] = {
                window.handle: window.title for window in windows_fn()
                if is_supported_browser(window.executable, window.image_path)
            }
        except Exception:  # noqa: BLE001
            baselines[key] = {}
        target_handles.pop(key, None)
        ready_since.pop(key, None)

    def attempt(endorsement_id: int | str) -> Optional[Path]:
        key = str(endorsement_id)
        target = pending_path(downloads_dir, key)
        if target.is_file():
            return target
        if key in attempted:
            return None
        if not available():
            state.setdefault(key, "автоматика Windows (клавиатура/окна) недоступна")
            return None
        now = monotonic()
        requested_at.setdefault(key, now)
        try:
            windows = windows_fn()
        except Exception:  # noqa: BLE001
            return None

        # Pin the window/tab created or navigated by this document's browser
        # fallback. Never fall back to an older PDF tab in another browser window.
        pinned = target_handles.get(key)
        if pinned is None:
            baseline = baselines.setdefault(key, {})
            changed = [
                window for window in windows
                if is_supported_browser(window.executable, window.image_path)
                and (window.handle not in baseline or baseline[window.handle] != window.title)
            ]
            active_handle = int(active_handle_fn())
            active_changed = [window for window in changed if window.handle == active_handle]
            if len(active_changed) == 1:
                pinned = active_changed[0].handle
            elif len(changed) == 1:
                pinned = changed[0].handle
            else:
                seen = "; ".join(sorted(
                    {f"[{window.executable}] {window.title!r}" for window in changed})) or "ни одного"
                state[key] = f"окно загрузки не закреплено: изменилось окон {len(changed)}: {seen}"
                return None
            target_handles[key] = pinned

        candidate = next((window for window in windows if window.handle == pinned), None)
        if candidate is None or not is_supported_browser(candidate.executable, candidate.image_path):
            ready_since.pop(key, None)
            state[key] = ("закреплённое окно закрылось" if candidate is None
                          else "закреплённое окно — не поддерживаемый браузер")
            return None
        recognized_pdf = looks_like_loaded_pdf(candidate.title)
        if not recognized_pdf:
            if looks_like_cloudflare_page(candidate.title):
                # На странице проверки Ctrl+S сохранит HTML, а не PDF, и сожжёт
                # единственную попытку — ждём, пока человек пройдёт капчу.
                state[key] = (f"в закреплённом окне идёт проверка Cloudflare: "
                              f"[{candidate.executable}] заголовок {candidate.title!r}")
                ready_since.pop(key, None)
                return None
            state[key] = (f"закреплённое окно ещё не опознано как PDF: [{candidate.executable}] "
                          f"заголовок {candidate.title!r}")
        signature = (candidate.handle, candidate.title)
        prior = ready_since.get(key)
        if prior is None or prior[:2] != signature:
            ready_since[key] = (*signature, now)
            return None
        if now - prior[2] < max(0.0, stable_for):
            return None
        if recognized_pdf:
            if now - requested_at[key] < max(0.0, ready_delay):
                return None
        elif now - requested_at[key] < max(0.0, blind_after):
            # Слепая попытка — только когда просмотрщик с незнакомым заголовком
            # уже давно стабилен: раньше легко попасть в ещё грузящуюся страницу.
            return None

        # Mark before touching the UI: retries must never spam/focus any window.
        attempted.add(key)
        state[key] = (f"Ctrl+S отправлен, заголовок [{candidate.executable}] "
                      f"{candidate.title!r} (опознан как PDF: "
                      f"{'да' if recognized_pdf else 'нет — слепая попытка'})")
        if on_attempt is not None:
            try:
                on_attempt(candidate.title)
            except Exception:  # noqa: BLE001
                pass
        # Даже с незнакомым заголовком пытаемся: окно уже закреплено за этим
        # документом и проверяем хвост (капча/Getty/JPEG в «Загрузках»).
        result = save_pdf_if_possible(
            target, windows=lambda: [candidate], require_pdf_title=recognized_pdf)
        return result

    def note(endorsement_id: int | str) -> str:
        return state.get(str(endorsement_id), "состояние не записано")

    attempt.before_open = before_open  # type: ignore[attr-defined]
    attempt.state = note  # type: ignore[attr-defined]
    return attempt
