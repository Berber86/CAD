"""Безопасное взаимодействие с окнами браузера (только Windows).

Portal automation uses only a verified supported-browser window; Ctrl+S remains
stricter and additionally requires a loaded PDF title. Foxit/FinReader and other
PDF programs are never activated, and a download gets at most one Ctrl+S attempt.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

PENDING_PREFIX = "tsai_step0_"
VK_CONTROL = 0x11
VK_S = 0x53
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B
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
PREFERRED_BROWSER_OPTIONS: dict[str, str] = {
    "auto": "Авто (единый / по умолчанию)",
    "chrome": "Google Chrome",
    "edge": "Microsoft Edge",
    "yandex": "Яндекс Браузер",
    "firefox": "Mozilla Firefox",
    "brave": "Brave",
    "opera": "Opera",
}
_BROWSER_KEY_BY_EXE: dict[str, str] = {
    "chrome.exe": "chrome",
    "msedge.exe": "edge",
    "firefox.exe": "firefox",
    "brave.exe": "brave",
    "opera.exe": "opera",
    "opera_gx.exe": "opera",
    "vivaldi.exe": "vivaldi",
    "waterfox.exe": "waterfox",
}
_BROWSER_ALIASES: dict[str, str] = {
    "": "auto",
    "auto": "auto",
    "default": "auto",
    "авто": "auto",
    "chrome": "chrome",
    "google chrome": "chrome",
    "chrome.exe": "chrome",
    "edge": "edge",
    "msedge": "edge",
    "msedge.exe": "edge",
    "microsoft edge": "edge",
    "yandex": "yandex",
    "yandex browser": "yandex",
    "яндекс": "yandex",
    "яндекс браузер": "yandex",
    "firefox": "firefox",
    "mozilla firefox": "firefox",
    "firefox.exe": "firefox",
    "brave": "brave",
    "brave.exe": "brave",
    "opera": "opera",
    "opera.exe": "opera",
    "opera_gx": "opera",
    "opera_gx.exe": "opera",
    "opera gx": "opera",
}
_BROWSER_PREF_LOCK = threading.Lock()
_PREFERRED_BROWSER = "auto"
_LAST_AUTO_BROWSER = ""
CLOUDFLARE_TITLE_MARKS = (
    "just a moment", "one moment", "just a sec", "please wait", "checking your browser",
    "checking if the site", "verifying", "cloudflare", "captcha", "challenge",
    "security check", "access denied", "verify you are human", "attention required",
    "один момент", "минуточку", "секунду", "пожалуйста, подождите",
    "проверка", "проверяем", "подождите", "одну минуту", "безопасност",
    "подтвердите, что вы человек", "робот",
)
PDF_TITLE_MARKS = (".pdf", "pdf viewer", "pdf.js viewer")
SAVE_DIALOG_MARKS = (
    "save as", "save a copy", "save print output as", "сохранить как",
    "сохранить копию", "сохранение", "download file",
)
WEBPAGE_SAVE_MARKS = (
    "веб-страница", "вебстраница", "один файл", "webpage", "web page",
    "mhtml", "web archive", "веб-архив", "только html", "html only",
    # RapidOCR (ch_PP-OCRv4) reads Russian Cyrillic UI text as Latin homoglyphs
    # (e.g. "редактирование" -> "peAaKTMpoBaHMe", "Веб-страница, один файл" -> "be6-cTpaHHLa, oAHH oaNn").
    "be6-", "beb-", "be6c", "ctpah", "ctpahn", "ctpahm", "oahh", "oann", "nonhoct",
)
_SAVE_NOTE_LOCK = threading.Lock()
_LAST_SAVE_NOTE = ""


def _set_last_save_note(note: str) -> None:
    with _SAVE_NOTE_LOCK:
        global _LAST_SAVE_NOTE
        _LAST_SAVE_NOTE = str(note or "")


def last_save_note() -> str:
    with _SAVE_NOTE_LOCK:
        return _LAST_SAVE_NOTE


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


def browser_key(executable: str, image_path: str = "") -> str:
    """Каноничный код браузера ('chrome', 'edge', 'yandex', ...) или ''."""
    name = str(executable or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name in _BROWSER_KEY_BY_EXE:
        return _BROWSER_KEY_BY_EXE[name]
    if name == "browser.exe" and "yandex" in str(image_path or "").lower():
        return "yandex"
    return ""


def matches_browser(window: BrowserWindow, key: str) -> bool:
    """True, если окно относится к заданному семейству браузера (или ``auto``)."""
    target = normalize_preferred_browser(key)
    if target == "auto":
        return is_supported_browser(window.executable, window.image_path)
    return browser_key(window.executable, window.image_path) == target


def normalize_preferred_browser(raw: object) -> str:
    """Приводит строку конфига/UI к коду из ``PREFERRED_BROWSER_OPTIONS``."""
    text = str(raw or "").strip().lower()
    for code, label in PREFERRED_BROWSER_OPTIONS.items():
        if text == code or text == label.lower():
            return code
    return _BROWSER_ALIASES.get(text, "auto")


def preferred_browser_label(code: object) -> str:
    """Подпись для UI по коду браузера."""
    normalized = normalize_preferred_browser(code)
    return PREFERRED_BROWSER_OPTIONS.get(normalized, PREFERRED_BROWSER_OPTIONS["auto"])


def preferred_browser_key_for_label(label: object) -> str:
    """Обратное преобразование подписи UI в код для сохранения."""
    return normalize_preferred_browser(label)


def preferred_browser_from_config(cfg: object) -> str:
    """Читает ``deed_intake.preferred_browser`` (или ``browser.preferred``)."""
    if not isinstance(cfg, dict):
        return "auto"
    deed = cfg.get("deed_intake")
    if isinstance(deed, dict) and deed.get("preferred_browser") is not None:
        return normalize_preferred_browser(deed.get("preferred_browser"))
    browser_sec = cfg.get("browser")
    if isinstance(browser_sec, dict) and browser_sec.get("preferred") is not None:
        return normalize_preferred_browser(browser_sec.get("preferred"))
    return "auto"


def get_preferred_browser() -> str:
    with _BROWSER_PREF_LOCK:
        return _PREFERRED_BROWSER


def set_preferred_browser(code: object) -> str:
    normalized = normalize_preferred_browser(code)
    with _BROWSER_PREF_LOCK:
        global _PREFERRED_BROWSER
        _PREFERRED_BROWSER = normalized
    return normalized


def get_auto_browser_pin() -> str:
    with _BROWSER_PREF_LOCK:
        return _LAST_AUTO_BROWSER


def reset_auto_browser_pin() -> None:
    with _BROWSER_PREF_LOCK:
        global _LAST_AUTO_BROWSER
        _LAST_AUTO_BROWSER = ""


def _set_auto_browser_pin(key: str) -> None:
    if not key:
        return
    with _BROWSER_PREF_LOCK:
        global _LAST_AUTO_BROWSER
        _LAST_AUTO_BROWSER = key


def _key_from_prog_id(prog_id: str) -> str:
    lowered = str(prog_id or "").strip().lower()
    if not lowered:
        return ""
    for token, key in (
        ("chrome", "chrome"),
        ("msedge", "edge"),
        ("edge", "edge"),
        ("yandex", "yandex"),
        ("firefox", "firefox"),
        ("brave", "brave"),
        ("opera", "opera"),
        ("vivaldi", "vivaldi"),
        ("waterfox", "waterfox"),
    ):
        if token in lowered:
            return key
    return ""


def default_browser_key() -> str:
    """Определяет системный браузер по умолчанию в Windows (иначе ``""``)."""
    if os.name != "nt":
        return ""
    try:
        import winreg  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001
        return ""
    for scheme in ("https", "http"):
        subkey = (
            rf"Software\Microsoft\Windows\Shell\Associations"
            rf"\UrlAssociations\{scheme}\UserChoice"
        )
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey) as key:
                prog_id, _ = winreg.QueryValueEx(key, "ProgId")
        except OSError:
            continue
        detected = _key_from_prog_id(str(prog_id or ""))
        if detected:
            return detected
    return ""


def _windows_app_path(exe_name: str) -> str:
    if os.name != "nt":
        return ""
    try:
        import winreg  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001
        return ""
    subkey = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, subkey) as key:
                value, _ = winreg.QueryValueEx(key, None)
        except OSError:
            continue
        candidate = str(value or "").strip().strip('"')
        if candidate and Path(candidate).is_file():
            return candidate
    return ""


def find_browser_executable(
    browser_id: object,
    *,
    windows: Optional[Sequence[BrowserWindow]] = None,
) -> str:
    """Ищет исполняемый файл указанного браузера на ПК (окна → реестр → стандартные пути)."""
    target = normalize_preferred_browser(browser_id)
    if target == "auto":
        return ""

    # 1. Уже запущенное окно (подхватывает в т.ч. нестандартные/переносные установки).
    try:
        candidates = list(list_windows() if windows is None else windows)
    except Exception:  # noqa: BLE001
        candidates = []
    for window in candidates:
        if matches_browser(window, target) and window.image_path:
            try:
                if Path(window.image_path).is_file():
                    return window.image_path
            except OSError:
                continue

    # 2. Реестр Windows (App Paths) и стандартные каталоги установки.
    if os.name == "nt":
        app_path_names = {
            "chrome": ("chrome.exe",),
            "edge": ("msedge.exe",),
            "yandex": ("browser.exe",),
            "firefox": ("firefox.exe",),
            "brave": ("brave.exe",),
            "opera": ("opera.exe", "opera_gx.exe"),
        }.get(target, ())
        for exe_name in app_path_names:
            found = _windows_app_path(exe_name)
            if found and (target != "yandex" or "yandex" in found.lower()):
                return found

        roots = [
            Path(env_val)
            for env_name in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)")
            if (env_val := os.environ.get(env_name, "").strip())
        ]
        rel_candidates = {
            "chrome": ("Google/Chrome/Application/chrome.exe",),
            "edge": ("Microsoft/Edge/Application/msedge.exe",),
            "yandex": (
                "Yandex/YandexBrowser/Application/browser.exe",
                "Yandex/Yandex Browser/Application/browser.exe",
            ),
            "firefox": ("Mozilla Firefox/firefox.exe",),
            "brave": ("BraveSoftware/Brave-Browser/Application/brave.exe",),
            "opera": (
                "Programs/Opera/opera.exe",
                "Programs/Opera GX/opera.exe",
                "Opera/launcher.exe",
                "Opera/opera.exe",
            ),
        }.get(target, ())
        for root in roots:
            for rel in rel_candidates:
                candidate = root / rel
                try:
                    if candidate.is_file():
                        return str(candidate)
                except OSError:
                    continue

    # 3. PATH / кросс-платформенный запас.
    which_names = {
        "chrome": ("google-chrome", "chrome.exe", "chrome"),
        "edge": ("microsoft-edge", "msedge.exe", "msedge"),
        "yandex": ("yandex-browser",),
        "firefox": ("firefox", "firefox.exe"),
        "brave": ("brave-browser", "brave.exe", "brave"),
        "opera": ("opera", "opera.exe"),
    }.get(target, ())
    for name in which_names:
        found = shutil.which(name)
        if found:
            return found
    return ""


def open_url(
    url: str,
    *,
    preferred: Optional[object] = None,
    new: int = 2,
    windows_fn: Optional[Callable[[], list[BrowserWindow]]] = None,
    popen: Optional[Callable[..., object]] = None,
    webbrowser_open: Optional[Callable[..., object]] = None,
    default_key_fn: Optional[Callable[[], str]] = None,
    find_exe_fn: Optional[Callable[..., str]] = None,
) -> bool:
    """Открывает URL в приоритетном браузере (или в едином активном при режиме «Авто»)."""
    pref = normalize_preferred_browser(
        preferred if preferred is not None else get_preferred_browser()
    )
    list_fn = windows_fn or list_windows
    try:
        current_windows = list(list_fn())
    except Exception:  # noqa: BLE001
        current_windows = []

    target_key = ""
    if pref != "auto":
        target_key = pref
    else:
        pinned = get_auto_browser_pin()
        # В режиме «Авто» держимся того же браузера, с которым уже идёт работа в этой
        # сессии (например, где открыт NYC Portal), только пока его окно реально открыто.
        if pinned and any(matches_browser(w, pinned) for w in current_windows):
            target_key = pinned

    if target_key:
        exe = (find_exe_fn or find_browser_executable)(target_key, windows=current_windows)
        if exe:
            launcher = popen or subprocess.Popen
            launcher(
                [exe, str(url)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            if pref == "auto":
                _set_auto_browser_pin(target_key)
            return True

    fallback = webbrowser_open or webbrowser.open
    opened = bool(fallback(str(url), new=new))
    if pref == "auto":
        detected = (default_key_fn or default_browser_key)()
        if detected:
            _set_auto_browser_pin(detected)
    return opened


def pick_browser_window(
    windows: Sequence[BrowserWindow],
    active_handle: int = 0,
    *,
    preferred: Optional[object] = None,
    title_marks: Sequence[str] = (),
    remember: bool = False,
    default_key_fn: Optional[Callable[[], str]] = None,
) -> Optional[BrowserWindow]:
    """Единый детерминированный выбор окна браузера для Портала, ACRIS и Клерка."""
    supported = [
        window for window in windows
        if is_supported_browser(window.executable, window.image_path)
    ]
    if not supported:
        return None
    pref = normalize_preferred_browser(
        preferred if preferred is not None else get_preferred_browser()
    )
    if pref != "auto":
        eligible = [window for window in supported if matches_browser(window, pref)]
        if not eligible:
            return None
    else:
        eligible = supported

    chosen: Optional[BrowserWindow] = None
    marks = tuple(str(mark).lower() for mark in title_marks if str(mark).strip())
    if marks:
        marked = [
            window for window in eligible
            if any(mark in window.title.lower() for mark in marks)
        ]
        if marked:
            chosen = next((w for w in marked if w.handle == int(active_handle)), marked[0])

    if chosen is None and active_handle:
        chosen = next((w for w in eligible if w.handle == int(active_handle)), None)

    if chosen is None and pref == "auto":
        pin = get_auto_browser_pin()
        if pin:
            chosen = next((w for w in eligible if matches_browser(w, pin)), None)
        if chosen is None:
            os_default = (default_key_fn or default_browser_key)()
            if os_default:
                chosen = next((w for w in eligible if matches_browser(w, os_default)), None)

    if chosen is None:
        chosen = eligible[0]

    if remember and pref == "auto":
        _set_auto_browser_pin(browser_key(chosen.executable, chosen.image_path))
    return chosen


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
                            require_title: bool = True,
                            preferred: Optional[object] = None) -> Optional[BrowserWindow]:
    """Fail-closed: только поддерживаемый браузер с загруженным PDF, не challenge.

    ``require_title=False`` — слепая попытка в уже закреплённое хуком окно:
    заголовок просмотрщика не обязан совпасть с известными шаблонами;
    страхует не заголовок, а итоговая проверка настоящего PDF в «Загрузках».
    """
    try:
        candidates = list_windows() if windows is None else windows
    except Exception:  # noqa: BLE001
        return None
    pref = normalize_preferred_browser(
        preferred if preferred is not None else get_preferred_browser()
    )
    for window in candidates:
        if not is_supported_browser(window.executable, window.image_path):
            continue
        if pref != "auto" and not matches_browser(window, pref):
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
    windows = list_windows()
    if not any(
        window.handle == int(handle)
        and is_supported_browser(window.executable, window.image_path)
        for window in windows
    ):
        return False
    user32 = _user32()
    if user32 is None:
        return False
    # If this browser (or one of its own transient popups, e.g. an open <select>
    # dropdown) is already foreground, preserve focus inside its web page.
    # Reapplying SetFocus/SetForegroundWindow to the top-level Chrome HWND here
    # would steal activation from that popup and close it immediately.
    # A separate titled top-level window of the same browser process is NOT a
    # popup — in that case we still need to activate the target window handle.
    try:
        other_titled = {w.handle for w in windows if w.handle != int(handle)}
        if foreground_is_acceptable(handle) and (
            not other_titled or int(user32.GetForegroundWindow() or 0) not in other_titled
        ):
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


_SENDINPUT_TYPES = None


def _sendinput_types():
    """Return cached Win32 INPUT/MOUSEINPUT/KEYBDINPUT ctypes structures."""
    global _SENDINPUT_TYPES  # noqa: PLW0603
    if _SENDINPUT_TYPES is not None:
        return _SENDINPUT_TYPES
    import ctypes
    from ctypes import wintypes

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG), ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t),
        ]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class INPUTUNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("union",)
        _fields_ = [("type", wintypes.DWORD), ("union", INPUTUNION)]

    _SENDINPUT_TYPES = (INPUT, MOUSEINPUT, KEYBDINPUT)
    return _SENDINPUT_TYPES


def click_browser_window(
    handle: int, x: int, y: int, *, verify_active_after: bool = True,
) -> bool:
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

            INPUT, MOUSEINPUT, _KEYBDINPUT = _sendinput_types()
            send_input = user32.SendInput
            try:
                send_input.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
                send_input.restype = wintypes.UINT
            except (AttributeError, TypeError):
                pass

            def send_mouse(flags: int) -> int:
                inputs = (INPUT * 1)(INPUT(type=0, mi=MOUSEINPUT(dwFlags=flags)))
                return int(send_input(1, inputs, ctypes.sizeof(INPUT)))

            sendinput_available = True
            if send_mouse(MOUSEEVENTF_LEFTDOWN) != 1:
                return False  # partial click; never issue a second click as fallback
            time.sleep(0.05)
            if send_mouse(MOUSEEVENTF_LEFTUP) != 1:
                return False
            return True if not verify_active_after else (active_window_handle() == int(handle))
        except Exception:  # noqa: BLE001 - legacy fallback below
            if sendinput_available:
                return False
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.05)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        return True if not verify_active_after else (active_window_handle() == int(handle))
    except Exception:  # noqa: BLE001
        return False


def _key(vk: int, *, up: bool) -> None:
    user32 = _user32()
    if user32 is None:
        return
    scan = 0
    map_vk = getattr(user32, "MapVirtualKeyW", None)
    if callable(map_vk):
        try:
            scan = int(map_vk(int(vk), 0) or 0)
        except Exception:  # noqa: BLE001
            scan = 0
    user32.keybd_event(vk, scan, KEYEVENTF_KEYUP if up else 0, 0)


def _press_ctrl_combo(vk: int) -> bool:
    """Send Ctrl+<key> with tiny settle pauses so Chrome/RDP never drops the modifier."""
    if _user32() is None:
        return False
    try:
        _key(VK_CONTROL, up=False)
        time.sleep(0.015)
        _key(int(vk), up=False)
        time.sleep(0.015)
        _key(int(vk), up=True)
        time.sleep(0.015)
        _key(VK_CONTROL, up=True)
        return True
    except Exception:  # noqa: BLE001
        return False


def press_ctrl_s() -> bool:
    return _press_ctrl_combo(VK_S)


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


def press_escape() -> bool:
    user32 = _user32()
    if user32 is None:
        return False
    try:
        _key(VK_ESCAPE, up=False)
        _key(VK_ESCAPE, up=True)
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
    return _press_ctrl_combo(VK_L)


def press_ctrl_t() -> bool:
    """Open a new browser tab (the current tabs are left untouched)."""
    return _press_ctrl_combo(VK_T)


def press_ctrl_a() -> bool:
    """Select the focused browser field contents."""
    return _press_ctrl_combo(VK_A)


def type_text(text: str) -> bool:
    """Ввод Unicode-пути в появившийся диалог «Сохранить как» через SendInput."""
    user32 = _user32()
    if user32 is None:
        return False
    value = str(text)
    if not value:
        return True
    raw = value.encode("utf-16-le")
    units = [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]
    send_input = getattr(user32, "SendInput", None)
    if callable(send_input):
        sendinput_called = False
        try:
            import ctypes
            from ctypes import wintypes

            INPUT, _MOUSEINPUT, KEYBDINPUT = _sendinput_types()
            try:
                send_input.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
                send_input.restype = wintypes.UINT
            except (AttributeError, TypeError):
                pass
            events = []
            for code_unit in units:
                events.append(INPUT(type=1, ki=KEYBDINPUT(wVk=0, wScan=code_unit, dwFlags=KEYEVENTF_UNICODE)))
                events.append(INPUT(
                    type=1,
                    ki=KEYBDINPUT(wVk=0, wScan=code_unit, dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
                ))
            buf = (INPUT * len(events))(*events)
            sendinput_called = True
            return int(send_input(len(events), buf, ctypes.sizeof(INPUT))) == len(events)
        except Exception:  # noqa: BLE001 - fall back to keybd_event only if SendInput could not run
            if sendinput_called:
                return False
    try:
        for code_unit in units:
            user32.keybd_event(0, code_unit, KEYEVENTF_UNICODE, 0)
            user32.keybd_event(0, code_unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0)
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


def _is_pdf_file(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(5) == b"%PDF-"
    except OSError:
        return False


_WEBPAGE_PAYLOAD_PREFIXES = (
    b"from:",
    b"mime-version:",
    b"content-type:",
    b"subject:",
    b"<!doctype",
    b"<html",
    b"<head",
    b"<script",
    b"<?xml",
)


def _is_non_pdf_webpage_file(path: Path) -> bool:
    """True when a saved file is HTML/MHTML rather than a real %PDF- payload."""
    try:
        if not path.is_file():
            return False
        with path.open("rb") as handle:
            head = handle.read(512)
        if head.startswith(b"%PDF-"):
            return False
        stripped = head.lstrip().lower()
        return any(stripped.startswith(prefix) for prefix in _WEBPAGE_PAYLOAD_PREFIXES)
    except OSError:
        return False


def _is_download_arrow_cluster(
    img, x0: int, x1: int, y0: int, y1: int, *, fg_luma_thresh: int = 165,
) -> bool:
    """Match the downward-arrow-into-tray (↓) Adobe Acrobat toolbar icon."""
    width = x1 - x0 + 1
    height = y1 - y0 + 1
    if width < 10 or height < 12:
        return False
    icon_rows: list[tuple[int, list[int]]] = []
    for y in range(y0, y1 + 1):
        xs = [
            x for x in range(x0, x1 + 1)
            if (sum(img.getpixel((x, y))[:3]) // 3) < fg_luma_thresh
        ]
        if xs:
            icon_rows.append((y, xs))
    if len(icon_rows) < 10:
        return False
    iy0, iy1 = icon_rows[0][0], icon_rows[-1][0]
    ih = iy1 - iy0 + 1
    if ih < 10:
        return False
    top_limit = iy0 + max(2, int(ih * 0.30))
    top_widths = [len(xs) for y, xs in icon_rows if y <= top_limit]
    if not top_widths or max(top_widths) > max(4, int(width * 0.35)):
        return False
    bot_start = iy1 - max(2, int(ih * 0.25))
    bot_widths = [max(xs) - min(xs) + 1 for y, xs in icon_rows if y >= bot_start]
    if not bot_widths or max(bot_widths) < int(width * 0.70):
        return False
    return True


def find_adobe_download_point(image) -> tuple[int, int] | None:
    """Return window-relative (x, y) of Adobe Acrobat Chrome viewer's Download (↓) icon."""
    try:
        rgb = image.convert("RGB")
    except Exception:  # noqa: BLE001
        return None
    w, h = rgb.size
    if w < 600 or h < 300:
        return None
    y_min, y_max = int(h * 0.06), int(h * 0.24)
    x_min, x_max = int(w * 0.62), int(w * 0.99)

    def _luma(px: tuple[int, int, int]) -> int:
        return (px[0] + px[1] + px[2]) // 3

    def _is_dark_pill(px: tuple[int, int, int]) -> bool:
        return px[0] < 90 and px[1] < 90 and px[2] < 90 and (max(px) - min(px)) < 25

    lip_rows: list[tuple[int, int, int]] = []
    for y in range(y_min, y_max):
        run = 0
        best_run = (0, 0, 0)
        start = 0
        for x in range(x_min, x_max):
            if _is_dark_pill(rgb.getpixel((x, y))[:3]):
                if run == 0:
                    start = x
                run += 1
                if run > best_run[0]:
                    best_run = (run, start, x)
            else:
                run = 0
        if 55 <= best_run[0] <= 280:
            lip_rows.append((y, best_run[1], best_run[2]))
    if not lip_rows:
        return None

    for seed_y, seed_l, seed_r in lip_rows:
        mid_x = (seed_l + seed_r) // 2
        above_l = _luma(rgb.getpixel((mid_x, max(0, seed_y - 4)))[:3])
        pill_l = _luma(rgb.getpixel((mid_x, seed_y))[:3])
        if above_l < 115 or (above_l - pill_l) < 75:
            continue
        probe_l = min(seed_r, seed_l + 6)
        probe_r = max(seed_l, seed_r - 6)
        y0 = seed_y
        while y0 > y_min and (
            _is_dark_pill(rgb.getpixel((probe_l, y0 - 1))[:3])
            or _is_dark_pill(rgb.getpixel((probe_r, y0 - 1))[:3])
        ):
            y0 -= 1
        y1 = seed_y
        while y1 < y_max and (
            _is_dark_pill(rgb.getpixel((probe_l, y1 + 1))[:3])
            or _is_dark_pill(rgb.getpixel((probe_r, y1 + 1))[:3])
        ):
            y1 += 1
        pill_h = y1 - y0 + 1
        if not (16 <= pill_h <= 64):
            continue
        pill_left = seed_l
        for yy in range(y0 + 2, y1 - 1):
            x = pill_left
            while x > x_min and _is_dark_pill(rgb.getpixel((x - 1, yy))[:3]):
                x -= 1
            pill_left = min(pill_left, x)
        below_l = _luma(rgb.getpixel((mid_x, min(h - 1, y1 + 4)))[:3])
        if below_l < 115 or (below_l - pill_l) < 75:
            continue
        # Works both at full brightness and when Windows dims a parent window behind a modal.
        red_pixels = sum(
            1 for y in range(y0, y1 + 1) for x in range(0, min(120, w))
            if (px := rgb.getpixel((x, y)))[0] > 95
            and px[0] > px[1] * 2 + 20
            and px[0] > px[2] * 2 + 20
        )
        if red_pixels < 25:
            continue
        fg_thresh = max(80, int(above_l * 0.65))
        pill_cy = (y0 + y1) // 2
        scan_left = max(0, pill_left - int(pill_h * 5.5))
        cols = [
            x for x in range(scan_left, pill_left - 6)
            if any(_luma(rgb.getpixel((x, y))[:3]) < fg_thresh for y in range(y0 + 4, y1 - 3))
        ]
        clusters: list[list[int]] = []
        gap = max(5, int(pill_h * 0.25))
        for x in cols:
            if not clusters or x > clusters[-1][1] + gap:
                clusters.append([x, x])
            else:
                clusters[-1][1] = x
        for cluster in reversed(clusters):
            if _is_download_arrow_cluster(
                rgb, cluster[0], cluster[1], y0 + 3, y1 - 3, fg_luma_thresh=fg_thresh,
            ):
                return ((cluster[0] + cluster[1]) // 2, pill_cy)
    return None


def _try_click_adobe_download(handle: int) -> tuple[int, int] | None:
    """Click Adobe Acrobat Chrome viewer's ↓ Download button when visible."""
    if not handle or os.name != "nt":
        return None
    bounds = window_bounds(handle)
    if bounds is None:
        return None
    try:
        from PIL import ImageGrab

        image = ImageGrab.grab(bbox=bounds, all_screens=True)
    except Exception:  # noqa: BLE001
        return None
    rel = find_adobe_download_point(image)
    if rel is None:
        return None
    abs_x = int(bounds[0] + rel[0])
    abs_y = int(bounds[1] + rel[1])
    if click_browser_window(handle, abs_x, abs_y):
        return (abs_x, abs_y)
    return None


def _find_save_dialog_controls(dlg_handle: int) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
    """Return desktop (x, y) centers for (#32770 filename Edit, Save Button)."""
    if not dlg_handle or os.name != "nt":
        return None, None
    user32 = _user32()
    if user32 is None:
        return None, None
    try:
        import ctypes
        from ctypes import wintypes

        dlg_rect = wintypes.RECT()
        if not user32.GetWindowRect(int(dlg_handle), ctypes.byref(dlg_rect)):
            return None, None
        dlg_h = int(dlg_rect.bottom - dlg_rect.top)
        if dlg_h < 120:
            return None, None

        found_edit: list[tuple[int, int, int]] = []
        found_button: list[tuple[int, int]] = []
        cb_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        try:
            user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
            user32.GetClassNameW.restype = ctypes.c_int
            user32.EnumChildWindows.argtypes = (wintypes.HWND, cb_type, wintypes.LPARAM)
            user32.EnumChildWindows.restype = wintypes.BOOL
        except (AttributeError, TypeError):
            pass

        def collect(child, _param):  # noqa: ANN001
            if not user32.IsWindowVisible(child):
                return True
            cls_buf = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(child, cls_buf, 64)
            cls = cls_buf.value.strip()
            rect = wintypes.RECT()
            if not user32.GetWindowRect(child, ctypes.byref(rect)):
                return True
            width = int(rect.right - rect.left)
            height = int(rect.bottom - rect.top)
            if width <= 0 or height <= 0:
                return True
            cx = (int(rect.left) + int(rect.right)) // 2
            cy = (int(rect.top) + int(rect.bottom)) // 2
            if cls == "Edit" and width >= 120 and int(rect.top) >= int(dlg_rect.top + dlg_h * 0.55):
                found_edit.append((int(rect.top), cx, cy))
            elif cls == "Button":
                txt_len = user32.GetWindowTextLengthW(child)
                if 0 < txt_len < 64:
                    txt_buf = ctypes.create_unicode_buffer(txt_len + 1)
                    user32.GetWindowTextW(child, txt_buf, txt_len + 1)
                    label = txt_buf.value.replace("&", "").strip().lower()
                    if label in {"сохранить", "save"}:
                        found_button.append((cx, cy))
            return True

        user32.EnumChildWindows(int(dlg_handle), cb_type(collect), 0)
        edit_xy = (found_edit[0][1], found_edit[0][2]) if found_edit else None
        btn_xy = found_button[0] if found_button else None
        return edit_xy, btn_xy
    except Exception:  # noqa: BLE001
        return None, None


def _submit_save_dialog(
    dlg_handle: int,
    target: str | Path | None,
    *,
    live_ui: bool,
    press_ctrl_a_fn: Optional[Callable[[], bool]] = None,
    type_text_fn: Optional[Callable[[str], bool]] = None,
    press_enter_fn: Optional[Callable[[], bool]] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Fill Save As filename (if given) and submit via Save button or Enter."""
    ctrl_a = press_ctrl_a_fn or press_ctrl_a
    write = type_text_fn or type_text
    enter = press_enter_fn or press_enter

    edit_xy: tuple[int, int] | None = None
    btn_xy: tuple[int, int] | None = None
    if live_ui and os.name == "nt":
        # Common File Dialog (#32770) needs a brief settle window after HWND creation
        # before its shell folder view finishes stealing focus from the Edit box.
        sleep(0.25)
        edit_xy, btn_xy = _find_save_dialog_controls(dlg_handle)

    if target is not None:
        dest = Path(target)
        if dest.is_file() and not _is_pdf_file(dest):
            try:
                dest.unlink()
            except OSError:
                pass
        if edit_xy is not None and dlg_handle:
            click_browser_window(dlg_handle, *edit_xy)
            sleep(0.08)
        ctrl_a()
        if live_ui:
            sleep(0.05)
        if not write(str(dest)):
            return False
        sleep(0.20)

    if btn_xy is not None and dlg_handle and click_browser_window(
        dlg_handle, *btn_xy, verify_active_after=False,
    ):
        return True
    return bool(enter())


def _same_browser_or_process(
    dlg_handle: int,
    browser_handle: int,
    windows: Optional[Sequence[BrowserWindow]] = None,
) -> bool:
    """True when dialog and browser window belong to the same PID or browser family."""
    if int(dlg_handle) == int(browser_handle):
        return True
    user32 = _user32()
    if user32 is not None and _same_owning_process(user32, int(dlg_handle), int(browser_handle)):
        return True
    if windows is None:
        return False
    dlg_win = next((w for w in windows if w.handle == int(dlg_handle)), None)
    br_win = next((w for w in windows if w.handle == int(browser_handle)), None)
    if dlg_win is None or br_win is None:
        return False
    if not (is_supported_browser(dlg_win.executable, dlg_win.image_path)
            and is_supported_browser(br_win.executable, br_win.image_path)):
        return False
    dlg_key = browser_key(dlg_win.executable, dlg_win.image_path)
    br_key = browser_key(br_win.executable, br_win.image_path)
    return bool(dlg_key) and dlg_key == br_key


def dismiss_stale_save_dialogs(
    *,
    preferred: Optional[object] = None,
    windows_fn: Optional[Callable[[], list[BrowserWindow]]] = None,
    foreground_fn: Optional[Callable[[int], bool]] = None,
    press_escape_fn: Optional[Callable[[], bool]] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Close lingering browser-owned Save As dialogs so they cannot block later docs."""
    try:
        windows = (windows_fn or list_windows)()
    except Exception:  # noqa: BLE001
        return 0
    pref = normalize_preferred_browser(
        preferred if preferred is not None else get_preferred_browser()
    )
    bring = foreground_fn or foreground
    esc = press_escape_fn or press_escape
    closed = 0
    for window in windows:
        if not is_supported_browser(window.executable, window.image_path):
            continue
        if pref != "auto" and not matches_browser(window, pref):
            continue
        if not looks_like_save_dialog(window.title):
            continue
        if bring(window.handle) and esc():
            closed += 1
            sleep(0.15)
    return closed


def _save_dialog_ocr_text(handle: int) -> str:
    """Best-effort OCR of the lower half of an open Save As dialog (Type dropdown)."""
    if not handle or os.name != "nt":
        return ""
    user32 = _user32()
    if user32 is None:
        return ""
    try:
        import ctypes
        from ctypes import wintypes
        import tempfile
        from PIL import ImageGrab
        import ocr_pages

        if not ocr_pages.available():
            return ""
        rect = wintypes.RECT()
        if not user32.GetWindowRect(int(handle), ctypes.byref(rect)):
            return ""
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width < 200 or height < 120:
            return ""
        # «Имя файла» and «Тип файла» always sit in the bottom third of Save As.
        top = int(rect.top + height * 0.65)
        bbox = (int(rect.left), top, int(rect.right), int(rect.bottom))
        with tempfile.TemporaryDirectory(prefix="tsai_save_dlg_") as directory:
            img_path = Path(directory) / "dlg.png"
            image = ImageGrab.grab(bbox=bbox, all_screens=True)
            image.save(img_path, format="PNG")
            engine = ocr_pages._engine_or_raise()  # noqa: SLF001
            result = ocr_pages.run_engine(engine, str(img_path))
            if isinstance(result, tuple):
                result = result[0]
            return " ".join(str(item[1]) for item in (result or []) if len(item) >= 2).lower()
    except Exception:  # noqa: BLE001 - optional visual guard must never break saving
        return ""


def _text_indicates_webpage_save(text: str) -> bool:
    lowered = str(text or "").lower()
    if not lowered:
        return False
    return any(mark in lowered for mark in WEBPAGE_SAVE_MARKS)


def save_dialog_is_webpage(handle: int | None = None) -> bool:
    """True when an open Save As dialog offers Webpage/MHTML instead of PDF."""
    target_handle = int(handle) if handle else active_window_handle()
    if not target_handle:
        return False
    return _text_indicates_webpage_save(_save_dialog_ocr_text(target_handle))


def confirm_open_save_dialog(
    browser_handle: int,
    *,
    target: str | Path | None = None,
    seen_dialogs: set[int] | None = None,
    active_handle_fn: Optional[Callable[[], int]] = None,
    active_title_fn: Optional[Callable[[], str]] = None,
    same_process_fn: Optional[Callable[[int, int], bool]] = None,
    is_webpage_fn: Optional[Callable[[int], bool]] = None,
    press_ctrl_a_fn: Optional[Callable[[], bool]] = None,
    type_text_fn: Optional[Callable[[str], bool]] = None,
    press_enter_fn: Optional[Callable[[], bool]] = None,
    press_escape_fn: Optional[Callable[[], bool]] = None,
    windows_fn: Optional[Callable[[], Sequence[BrowserWindow]]] = None,
    foreground_fn: Optional[Callable[[int], bool]] = None,
) -> str:
    """Confirm a browser-owned PDF 'Save As' dialog, or cancel if it is 'Webpage'.

    Returns ``""`` when no matching browser Save dialog is active/unseen,
    ``"confirmed"`` after submitting the dialog, or ``"webpage_cancelled"``
    when the dialog offered Webpage/MHTML instead of PDF and was dismissed.
    """
    active_fn = active_handle_fn or active_window_handle
    title_fn = active_title_fn or active_window_title
    windows: Optional[Sequence[BrowserWindow]] = None

    def _windows() -> Sequence[BrowserWindow]:
        nonlocal windows
        if windows is None:
            if windows_fn is not None:
                windows = list(windows_fn())
            elif active_handle_fn is None and os.name == "nt":
                windows = list_windows()
            else:
                windows = []
        return windows

    def _matches_browser(handle: int) -> bool:
        if same_process_fn is not None:
            return bool(same_process_fn(handle, int(browser_handle)))
        return _same_browser_or_process(handle, int(browser_handle), _windows())

    dlg_handle = int(active_fn() or 0)
    matched = False
    if dlg_handle and looks_like_save_dialog(title_fn()):
        if (seen_dialogs is None or dlg_handle not in seen_dialogs) and _matches_browser(dlg_handle):
            matched = True

    if not matched and (windows_fn is not None or (active_handle_fn is None and os.name == "nt")):
        for win in _windows():
            if seen_dialogs is not None and win.handle in seen_dialogs:
                continue
            if not looks_like_save_dialog(win.title):
                continue
            if not _matches_browser(win.handle):
                continue
            if not (foreground_fn or foreground)(win.handle):
                continue
            dlg_handle = int(win.handle)
            matched = True
            break

    if not matched:
        return ""

    if seen_dialogs is not None:
        seen_dialogs.add(dlg_handle)

    webpage_check = is_webpage_fn or save_dialog_is_webpage
    if webpage_check(dlg_handle):
        (press_escape_fn or press_escape)()
        _set_last_save_note(
            "Chrome открыл «Сохранение» как веб-страницу («Веб-страница, один файл») "
            "вместо PDF — отключите расширение Adobe Acrobat в Chrome («Защита файла PDF») "
            "или включите «Скачивать PDF-файлы» в настройках Chrome; диалог закрыт."
        )
        return "webpage_cancelled"

    live_ui = (
        press_ctrl_a_fn is None
        and type_text_fn is None
        and press_enter_fn is None
    )
    ok = _submit_save_dialog(
        dlg_handle,
        target,
        live_ui=live_ui,
        press_ctrl_a_fn=press_ctrl_a_fn,
        type_text_fn=type_text_fn,
        press_enter_fn=press_enter_fn,
    )
    if not ok:
        if seen_dialogs is not None:
            seen_dialogs.discard(dlg_handle)
        return ""
    return "confirmed"


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
    adobe_download_fn: Optional[Callable[[int], object]] = None,
    sleep: Callable[[float], None] = time.sleep,
    wait: float = 0.8,
    require_pdf_title: bool = True,
    preferred: Optional[object] = None,
) -> Optional[Path]:
    """Одна безопасная попытка скачивания только в уже загруженном PDF браузера."""
    _set_last_save_note("")
    destination = Path(target)
    if destination.is_file():
        if _is_pdf_file(destination):
            return destination
        try:
            destination.unlink()
        except OSError:
            pass
    try:
        candidate = find_pdf_browser_window((windows or list_windows)(),
                                            require_title=require_pdf_title,
                                            preferred=preferred)
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

    live_adobe = (
        adobe_download_fn is None
        and press_ctrl_s_fn is None
        and dialog_open_fn is None
        and os.name == "nt"
    )
    adobe_fn = _try_click_adobe_download if live_adobe else adobe_download_fn
    adobe_attempts = 4 if live_adobe else (1 if adobe_fn is not None else 0)

    clicked_adobe = False
    for idx in range(adobe_attempts):
        if idx > 0:
            sleep(0.4)
            if int(active_handle()) != int(candidate.handle) and not bring(candidate.handle):
                break
        try:
            if adobe_fn(candidate.handle):  # type: ignore[misc]
                clicked_adobe = True
                break
        except Exception:  # noqa: BLE001
            pass

    if clicked_adobe:
        _set_last_save_note(
            "в окне Adobe Acrobat нажата кнопка скачивания (↓) вместо Ctrl+S"
        )
    else:
        press = press_ctrl_s_fn or press_ctrl_s
        if not press():
            return None

    dialog_open = dialog_open_fn or (lambda: looks_like_save_dialog(active_window_title()))
    step = 0.15
    first_pause = min(float(wait), step) if float(wait) > 0 else 0.0
    live_ui = (
        dialog_open_fn is None
        and type_text_fn is None
        and press_enter_fn is None
    )

    def _await_dialog_and_save(*, use_adobe_budget: bool) -> tuple[Optional[Path], str]:
        open_budget = max(float(wait), 8.0 if use_adobe_budget else 2.5)
        checks = max(1, int(round(open_budget / step)))
        opened_dialog = False
        dlg_handle = 0
        for idx in range(checks):
            sleep(first_pause if idx == 0 else step)
            if destination.is_file() and _is_pdf_file(destination):
                _set_last_save_note("")
                return destination, "ok"
            if destination.is_file() and _is_non_pdf_webpage_file(destination):
                try:
                    destination.unlink()
                except OSError:
                    pass
                return None, "mhtml"
            try:
                if dialog_open():
                    opened_dialog = True
                    dlg_handle = int(active_handle())
                    break
            except Exception:  # noqa: BLE001
                pass
            if dialog_open_fn is None and os.name == "nt" and (idx % 4 == 3 or idx == checks - 1):
                try:
                    all_wins = list_windows()
                    for win in all_wins:
                        if (
                            looks_like_save_dialog(win.title)
                            and _same_browser_or_process(win.handle, candidate.handle, all_wins)
                            and bring(win.handle)
                        ):
                            opened_dialog = True
                            dlg_handle = int(win.handle)
                            break
                    if opened_dialog:
                        break
                except Exception:  # noqa: BLE001
                    pass
        if not opened_dialog:
            return None, "no_dialog"

        if not dlg_handle:
            dlg_handle = int(active_handle())
        if dialog_open_fn is None and save_dialog_is_webpage(dlg_handle):
            press_escape()
            _set_last_save_note(
                "Chrome открыл «Сохранение» как веб-страницу («Веб-страница, один файл») "
                "вместо PDF — отключите расширение Adobe Acrobat в Chrome («Защита файла PDF») "
                "или включите «Скачивать PDF-файлы» в настройках Chrome; диалог закрыт."
            )
            return None, "webpage"

        submitted = _submit_save_dialog(
            dlg_handle,
            destination,
            live_ui=live_ui,
            press_ctrl_a_fn=(None if dialog_open_fn is None else (lambda: True)),
            type_text_fn=type_text_fn,
            press_enter_fn=press_enter_fn,
            sleep=sleep,
        )
        if not submitted:
            return None, "submit_failed"

        save_budget = max(float(wait), 2.5)
        save_checks = max(1, int(round(save_budget / step)))
        for _ in range(save_checks):
            sleep(step)
            if destination.is_file() and _is_pdf_file(destination):
                _set_last_save_note("")
                return destination, "ok"
            if destination.is_file() and _is_non_pdf_webpage_file(destination):
                try:
                    destination.unlink()
                except OSError:
                    pass
                return None, "mhtml"
        if destination.is_file() and _is_pdf_file(destination):
            _set_last_save_note("")
            return destination, "ok"
        if destination.is_file() and _is_non_pdf_webpage_file(destination):
            try:
                destination.unlink()
            except OSError:
                pass
            return None, "mhtml"
        return None, "timeout"

    saved_path, outcome = _await_dialog_and_save(use_adobe_budget=clicked_adobe)
    if saved_path is not None:
        return saved_path
    if outcome == "mhtml":
        if not clicked_adobe and adobe_fn is not None and bring(candidate.handle):
            try:
                if adobe_fn(candidate.handle):
                    _set_last_save_note(
                        "Chrome сохранил веб-страницу (MHTML) по Ctrl+S — удалена и нажата кнопка скачивания (↓) в Adobe Acrobat"
                    )
                    retry_path, _ = _await_dialog_and_save(use_adobe_budget=True)
                    if retry_path is not None:
                        return retry_path
                    return None
            except Exception:  # noqa: BLE001
                pass
        _set_last_save_note(
            "Chrome сохранил веб-страницу (MHTML) вместо PDF — проверьте расширение Adobe Acrobat / настройки PDF в Chrome."
        )
        return None
    return None


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
    preferred: Optional[object] = None,
    dismiss_fn: Optional[Callable[[], int]] = None,
):
    """Хук для лестницы: ждёт готовый PDF после старта навигации и на документ
    посылает Ctrl+S только один раз. Cloudflare-title не соответствует PDF-title.
    """
    attempted_pdf: set[str] = set()
    attempted_blind: set[str] = set()
    requested_at: dict[str, float] = {}
    baselines: dict[str, dict[int, str]] = {}
    target_handles: dict[str, int] = {}
    ready_since: dict[str, tuple[int, str, float]] = {}
    seen_save_dialogs: set[int] = set()
    dialog_attempts: dict[int, tuple[int, float]] = {}
    state: dict[str, str] = {}

    def _eligible(window: BrowserWindow) -> bool:
        if not is_supported_browser(window.executable, window.image_path):
            return False
        pref = normalize_preferred_browser(
            preferred if preferred is not None else get_preferred_browser()
        )
        return pref == "auto" or matches_browser(window, pref)

    def _cleanup_dialogs_and_pending(endorsement_id: int | str | None = None) -> None:
        if dismiss_fn is not None:
            try:
                dismiss_fn()
            except Exception:  # noqa: BLE001
                pass
        elif available() and windows_fn is list_windows:
            try:
                dismiss_stale_save_dialogs(preferred=preferred, windows_fn=windows_fn)
            except Exception:  # noqa: BLE001
                pass
        if endorsement_id is not None:
            stale = pending_path(downloads_dir, str(endorsement_id))
            if stale.is_file() and not _is_pdf_file(stale):
                try:
                    stale.unlink()
                except OSError:
                    pass

    def _prune_lingering_dialog(windows: Sequence[BrowserWindow], now: float) -> None:
        open_handles = {w.handle for w in windows if looks_like_save_dialog(w.title)}
        for hwnd in list(seen_save_dialogs):
            if hwnd not in open_handles:
                continue
            tries, tried_at = dialog_attempts.get(hwnd, (0, 0.0))
            if 0 < tries < 3 and (now - tried_at) >= 3.0:
                seen_save_dialogs.discard(hwnd)

    def _handle_open_save_dialog(
        browser_handle: int,
        target: Path,
        windows: Sequence[BrowserWindow],
        key: str,
        now: float,
    ) -> str:
        _prune_lingering_dialog(windows, now)
        before_seen = set(seen_save_dialogs)
        dlg_res = confirm_open_save_dialog(
            browser_handle,
            target=target,
            seen_dialogs=seen_save_dialogs,
            active_handle_fn=active_handle_fn,
            windows_fn=lambda: windows,
        )
        for new_hwnd in seen_save_dialogs - before_seen:
            prev_count = dialog_attempts.get(new_hwnd, (0, 0.0))[0]
            dialog_attempts[new_hwnd] = (prev_count + 1, now)
        if dlg_res == "webpage_cancelled":
            state[key] = last_save_note() or "диалог сохранения веб-страницы отменён"
        elif dlg_res == "confirmed":
            state[key] = f"диалог «Сохранить как» подтверждён автоматически -> {target.name}"
        return dlg_res

    def before_open(endorsement_id: int | str) -> None:
        """Snapshot existing browser tabs before the new document is navigated."""
        key = str(endorsement_id)
        _cleanup_dialogs_and_pending(key)
        requested_at[key] = monotonic()
        try:
            baselines[key] = {
                window.handle: window.title for window in windows_fn()
                if _eligible(window)
            }
        except Exception:  # noqa: BLE001
            baselines[key] = {}
        target_handles.pop(key, None)
        ready_since.pop(key, None)

    def cleanup(endorsement_id: int | str) -> None:
        """Dismiss stale browser Save dialogs after timeout/abort."""
        _cleanup_dialogs_and_pending(endorsement_id)

    def attempt(endorsement_id: int | str) -> Optional[Path]:
        key = str(endorsement_id)
        target = pending_path(downloads_dir, key)
        if target.is_file() and _is_pdf_file(target):
            return target
        if not available():
            state.setdefault(key, "автоматика Windows (клавиатура/окна) недоступна")
            return None
        now = monotonic()
        try:
            windows = windows_fn()
        except Exception:  # noqa: BLE001
            return None
        if key in attempted_pdf:
            pinned_done = target_handles.get(key)
            if pinned_done:
                dlg_res = _handle_open_save_dialog(pinned_done, target, windows, key, now)
                if dlg_res == "confirmed":
                    time.sleep(0.5)
                    if target.is_file() and _is_pdf_file(target):
                        return target
            return None
        requested_at.setdefault(key, now)

        # Pin the window/tab created or navigated by this document's browser
        # fallback. Never fall back to an older PDF tab in another browser window,
        # and never pin a transient modal Save As dialog as the browser tab HWND.
        pinned = target_handles.get(key)
        if pinned is None:
            baseline = baselines.setdefault(key, {})
            changed_all = [
                window for window in windows
                if _eligible(window)
                and (window.handle not in baseline or baseline[window.handle] != window.title)
            ]
            changed = [w for w in changed_all if not looks_like_save_dialog(w.title)]
            changed_dialogs = [w for w in changed_all if looks_like_save_dialog(w.title)]
            active_handle = int(active_handle_fn())
            active_changed = [window for window in changed if window.handle == active_handle]
            if len(active_changed) == 1:
                pinned = active_changed[0].handle
            elif len(changed) == 1:
                pinned = changed[0].handle
            else:
                pin = get_auto_browser_pin()
                pinned_browser_changed = (
                    [window for window in changed if matches_browser(window, pin)]
                    if pin and len({browser_key(w.executable, w.image_path) for w in changed}) > 1
                    else []
                )
                if len(pinned_browser_changed) == 1:
                    pinned = pinned_browser_changed[0].handle
                else:
                    if changed_dialogs and not changed:
                        dlg_res = _handle_open_save_dialog(
                            changed_dialogs[0].handle, target, windows, key, now,
                        )
                        if dlg_res == "confirmed":
                            time.sleep(0.8)
                            if target.is_file() and _is_pdf_file(target):
                                return target
                        return None
                    seen = "; ".join(sorted(
                        {f"[{window.executable}] {window.title!r}" for window in changed})) or "ни одного"
                    state[key] = f"окно загрузки не закреплено: изменилось окон {len(changed)}: {seen}"
                    return None
            target_handles[key] = pinned

        # If Chrome ("Ask where to save") popped a Save As dialog immediately upon
        # navigation/download, confirm it straight away instead of waiting 15 s
        # and sending Ctrl+S to the Save As dialog.
        dlg_res = _handle_open_save_dialog(pinned, target, windows, key, now)
        if dlg_res == "webpage_cancelled":
            return None
        if dlg_res == "confirmed":
            time.sleep(0.8)
            if target.is_file() and _is_pdf_file(target):
                return target
            return None

        candidate = next((window for window in windows if window.handle == pinned), None)
        if candidate is None or not _eligible(candidate):
            ready_since.pop(key, None)
            state[key] = ("закреплённое окно закрылось" if candidate is None
                          else "закреплённое окно — не поддерживаемый браузер")
            return None
        if looks_like_save_dialog(candidate.title):
            state[key] = f"в закреплённом окне открыт диалог сохранения: {candidate.title!r}"
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
            if key in attempted_blind:
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
        # A blind attempt on an unknown loading title does not consume the
        # single recognized-PDF attempt once the PDF viewer title actually loads.
        attempted_blind.add(key)
        if recognized_pdf:
            attempted_pdf.add(key)
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
        save_kwargs = {"windows": lambda: [candidate], "require_pdf_title": recognized_pdf}
        if preferred is not None:
            save_kwargs["preferred"] = preferred
        result = save_pdf_if_possible(target, **save_kwargs)
        if result is None and last_save_note():
            state[key] = last_save_note()
        return result

    def note(endorsement_id: int | str) -> str:
        return state.get(str(endorsement_id), "состояние не записано")

    attempt.before_open = before_open  # type: ignore[attr-defined]
    attempt.cleanup = cleanup  # type: ignore[attr-defined]
    attempt.state = note  # type: ignore[attr-defined]
    return attempt
