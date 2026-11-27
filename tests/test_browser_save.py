"""Безопасное Ctrl+S: только browser window, только после появления PDF."""
from __future__ import annotations

from pathlib import Path

import browser_save
from browser_save import BrowserWindow


def _yandex(handle: int = 10, title: str = "viewer") -> BrowserWindow:
    return BrowserWindow(
        handle, title, "browser.exe",
        r"C:\Users\Nikolay\AppData\Local\Yandex\YandexBrowser\Application\browser.exe",
    )


def test_only_known_browser_processes_are_eligible():
    assert browser_save.is_supported_browser("chrome.exe")
    assert browser_save.is_supported_browser("C:\\Program Files\\Microsoft\\Edge\\msedge.exe")
    assert browser_save.is_supported_browser("browser.exe", r"C:\Yandex\Application\browser.exe")
    assert not browser_save.is_supported_browser("browser.exe", r"C:\OtherApp\browser.exe")
    assert not browser_save.is_supported_browser("FoxitReader.exe")
    assert not browser_save.is_supported_browser("finreader.exe")


def test_pdf_title_is_ready_but_cloudflare_title_is_not():
    assert browser_save.looks_like_loaded_pdf("viewer")
    assert browser_save.looks_like_loaded_pdf("Warranty Deed 876753.pdf - Yandex Browser")
    assert not browser_save.looks_like_loaded_pdf("Just a moment...")
    assert not browser_save.looks_like_loaded_pdf("Checking your browser - Cloudflare")
    assert not browser_save.looks_like_loaded_pdf("Проверка безопасности")
    assert not browser_save.looks_like_loaded_pdf("Richmond County Clerk - Search")
    assert not browser_save.looks_like_loaded_pdf("Guide to the viewer")


def test_pdf_candidate_must_be_a_browser_not_a_pdf_reader():
    foxit = BrowserWindow(2, "viewer.pdf - Foxit Reader", "FoxitReader.exe", r"C:\Foxit\FoxitReader.exe")
    challenge = _yandex(title="Just a moment... Cloudflare")
    assert browser_save.find_pdf_browser_window([foxit, challenge]) is None
    assert browser_save.find_pdf_browser_window([foxit, _yandex()]) == _yandex()


def test_ctrl_s_is_never_sent_to_a_non_browser_window(tmp_path):
    calls: list[str] = []
    foxit = BrowserWindow(7, "Deed.pdf", "FoxitReader.exe", r"C:\Foxit\FoxitReader.exe")
    result = browser_save.save_pdf_if_possible(
        tmp_path / "out.pdf", windows=lambda: [foxit],
        foreground_fn=lambda handle: calls.append("focus") or True,
        active_handle_fn=lambda: 7,
        press_ctrl_s_fn=lambda: calls.append("ctrl+s") or True,
        sleep=lambda _seconds: None,
    )
    assert result is None
    assert calls == []


def test_focus_must_still_be_the_pdf_browser_before_ctrl_s(tmp_path):
    calls: list[str] = []
    result = browser_save.save_pdf_if_possible(
        tmp_path / "out.pdf", windows=lambda: [_yandex()],
        foreground_fn=lambda _handle: True,
        active_handle_fn=lambda: 99,
        press_ctrl_s_fn=lambda: calls.append("ctrl+s") or True,
        sleep=lambda _seconds: None,
    )
    assert result is None
    assert calls == []


def test_pdf_save_activates_browser_once_then_handles_save_dialog(tmp_path):
    target = tmp_path / "tsai_step0_876753.pdf"
    calls: list[object] = []

    def type_path(value: str) -> bool:
        calls.append(value)
        target.write_bytes(b"%PDF-1.7 deed")
        return True

    result = browser_save.save_pdf_if_possible(
        target,
        windows=lambda: [_yandex(title="viewer")],
        foreground_fn=lambda handle: calls.append(("focus", handle)) or True,
        active_handle_fn=lambda: 10,
        press_ctrl_s_fn=lambda: calls.append("ctrl+s") or True,
        dialog_open_fn=lambda: True,
        type_text_fn=type_path,
        press_enter_fn=lambda: calls.append("enter") or True,
        sleep=lambda _seconds: None,
    )
    assert result == target
    assert calls == [("focus", 10), "ctrl+s", str(target), "enter"]


def test_hook_waits_for_new_pdf_tab_and_never_uses_an_old_viewer(tmp_path, monkeypatch):
    moments = iter((0.0, 0.5, 1.5, 5.0, 6.1, 7.0))
    actions: list[tuple[int, str]] = []
    new_tab = _yandex(11, "Just a moment... Cloudflare")
    old_tab = _yandex(10, "viewer")

    def windows():
        if not hasattr(windows, "calls"):
            windows.calls = 0
        windows.calls += 1
        if windows.calls == 1:  # before_open snapshot
            return [old_tab]
        if windows.calls == 2:
            return [old_tab, new_tab]
        if windows.calls == 3:
            return [old_tab, new_tab]
        return [old_tab, _yandex(11, "viewer")]

    monkeypatch.setattr(browser_save, "available", lambda: True)

    def save(target, *, windows, **_kwargs):
        chosen = windows()[0]
        actions.append((chosen.handle, chosen.title))
        Path(target).write_bytes(b"%PDF-1.7 deed")
        return Path(target)

    monkeypatch.setattr(browser_save, "save_pdf_if_possible", save)
    hook = browser_save.auto_save_hook(
        tmp_path, ready_delay=4.0, stable_for=1.0,
        windows_fn=windows, monotonic=lambda: next(moments),
    )
    hook.before_open("876753")
    assert hook("876753") is None  # new tab is still Cloudflare
    assert hook("876753") is None
    assert hook("876753") is None  # PDF title first observed
    assert hook("876753") == tmp_path / "tsai_step0_876753.pdf"
    assert hook("876753") == tmp_path / "tsai_step0_876753.pdf"
    assert actions == [(11, "viewer")]


def test_hook_does_nothing_without_windows_keyboard_api(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_save, "available", lambda: False)
    hook = browser_save.auto_save_hook(tmp_path)
    assert hook("42") is None


def test_portal_click_requires_the_selected_browser_to_be_foreground(monkeypatch):
    calls = []

    class FakeUser32:
        def SetCursorPos(self, x, y):
            calls.append(("cursor", x, y))
            return True

        def mouse_event(self, *args):
            calls.append(("mouse", *args))

    monkeypatch.setattr(browser_save, "window_bounds", lambda handle: (0, 0, 800, 600))
    monkeypatch.setattr(browser_save, "_user32", lambda: FakeUser32())
    monkeypatch.setattr(browser_save, "active_window_handle", lambda: 10)
    assert browser_save.click_browser_window(10, 100, 200)
    assert calls[0] == ("cursor", 100, 200)

    calls.clear()
    monkeypatch.setattr(browser_save, "active_window_handle", lambda: 99)
    assert not browser_save.click_browser_window(10, 100, 200)
    assert calls == []


def test_foreground_retries_with_attached_input_queues_for_verified_browser(monkeypatch):
    import ctypes
    import browser_save

    calls = []
    active = iter((99, 99, 10))

    class User32:
        def IsIconic(self, handle):
            assert handle == 10
            return False

        def GetWindowForegroundWindow(self):
            return next(active)

        def GetForegroundWindow(self):
            return next(active)

        def GetWindowThreadProcessId(self, handle, process_id):
            process_id._obj.value = 1234 if handle == 10 else 5678
            return 40 if handle == 10 else 30

        def AttachThreadInput(self, source, target, attach):
            calls.append(("attach", source, target, attach))
            return True

        def AllowSetForegroundWindow(self, process_id):
            calls.append(("allow", process_id))
            return True

        def BringWindowToTop(self, handle):
            calls.append(("top", handle))
            return True

        def SetForegroundWindow(self, handle):
            calls.append(("activate", handle))
            return True

        def SetActiveWindow(self, handle):
            calls.append(("active", handle))

        def SetFocus(self, handle):
            calls.append(("focus", handle))

    class Kernel32:
        def GetCurrentThreadId(self):
            return 5

    monkeypatch.setattr(browser_save, "list_windows", lambda: [_yandex()])
    monkeypatch.setattr(browser_save, "_user32", lambda: User32())
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: Kernel32(), raising=False)
    monkeypatch.setattr(browser_save.time, "sleep", lambda _delay: None)
    assert browser_save.foreground(10, timeout=0.1)
    assert ("activate", 10) in calls
    assert ("attach", 5, 30, True) in calls
    assert ("attach", 5, 40, True) in calls
    assert ("attach", 5, 40, False) in calls
    assert ("attach", 5, 30, False) in calls
    assert not any(item[0] in {"active", "focus"} for item in calls)


def test_foreground_keeps_existing_focus_inside_the_browser_page(monkeypatch):
    import browser_save

    calls = []

    class User32:
        def GetForegroundWindow(self):
            calls.append("query")
            return 10

    monkeypatch.setattr(browser_save, "list_windows", lambda: [_yandex()])
    monkeypatch.setattr(browser_save, "_user32", lambda: User32())
    assert browser_save.foreground(10)
    assert calls == ["query"]  # no SetFocus on the top-level HWND; keep the page control focused


def test_foreground_keeps_an_open_dropdown_popup_from_the_same_process(monkeypatch):
    """An open <select>/autocomplete popup is its own HWND owned by the same browser
    process. foreground() must not force SetForegroundWindow back onto the parent
    tab in that case: doing so closes the popup (opens-then-shuts-instantly bug)."""
    import browser_save

    calls = []

    class User32:
        def GetForegroundWindow(self):
            calls.append("query")
            return 777  # the dropdown popup's own top-level window

        def GetWindowThreadProcessId(self, handle, process_id):
            # Popup (777) and parent tab (10) are both owned by the same browser process.
            process_id._obj.value = 4321
            return 50

        def BringWindowToTop(self, handle):
            calls.append(("top", handle))
            return True

        def SetForegroundWindow(self, handle):
            calls.append(("activate", handle))
            return True

    monkeypatch.setattr(browser_save, "list_windows", lambda: [_yandex()])
    monkeypatch.setattr(browser_save, "_user32", lambda: User32())
    assert browser_save.foreground(10)
    assert "query" in calls
    assert not any(isinstance(item, tuple) for item in calls)  # never re-activated the parent


def test_foreground_is_acceptable_matches_exact_handle_or_same_process(monkeypatch):
    import browser_save

    class User32:
        def __init__(self, current):
            self.current = current

        def GetForegroundWindow(self):
            return self.current

        def GetWindowThreadProcessId(self, handle, process_id):
            process_id._obj.value = 111 if handle == 10 else 222
            return 1

    monkeypatch.setattr(browser_save, "_user32", lambda: User32(10))
    assert browser_save.foreground_is_acceptable(10)

    # Different HWND, same owning process as the target (a popup) -> acceptable.
    monkeypatch.setattr(browser_save, "_user32", lambda: User32(10))

    class User32DifferentHandleSameProcess:
        def GetForegroundWindow(self):
            return 777

        def GetWindowThreadProcessId(self, handle, process_id):
            process_id._obj.value = 999  # same pid regardless of which hwnd is asked
            return 1

    monkeypatch.setattr(browser_save, "_user32", lambda: User32DifferentHandleSameProcess())
    assert browser_save.foreground_is_acceptable(10)

    class User32DifferentProcess:
        def GetForegroundWindow(self):
            return 888

        def GetWindowThreadProcessId(self, handle, process_id):
            process_id._obj.value = 111 if handle == 888 else 222
            return 1

    monkeypatch.setattr(browser_save, "_user32", lambda: User32DifferentProcess())
    assert not browser_save.foreground_is_acceptable(10)


def test_maximize_only_targets_a_verified_browser_window(monkeypatch):
    import browser_save

    shown = []

    class User32:
        def ShowWindow(self, handle, command):
            shown.append((handle, command))
            return True

        def GetForegroundWindow(self):
            return 10

    monkeypatch.setattr(browser_save, "list_windows", lambda: [_yandex()])
    monkeypatch.setattr(browser_save, "_user32", lambda: User32())
    assert browser_save.maximize_browser_window(10)
    assert shown == [(10, 3)]

    monkeypatch.setattr(browser_save, "list_windows", lambda: [
        BrowserWindow(10, "Other", "notepad.exe", r"C:\Windows\notepad.exe")
    ])
    assert not browser_save.maximize_browser_window(10)
    assert shown == [(10, 3)]


def test_partial_sendinput_never_falls_back_to_a_second_select_click(monkeypatch):
    calls = []

    class SendInput:
        argtypes = None
        restype = None

        def __call__(self, count, _inputs, _size):
            calls.append(("send-input", count))
            # Down вставлен, up — нет: частичный клик.
            return 1 if len([c for c in calls if c[0] == "send-input"]) == 1 else 0

    class User32:
        def SetCursorPos(self, x, y):
            calls.append(("cursor", x, y))
            return True

        def mouse_event(self, *args):
            calls.append(("fallback-mouse", *args))

    User32.SendInput = SendInput()
    monkeypatch.setattr(browser_save, "window_bounds", lambda _handle: (0, 0, 800, 600))
    monkeypatch.setattr(browser_save, "_user32", lambda: User32())
    monkeypatch.setattr(browser_save, "active_window_handle", lambda: 10)
    assert not browser_save.click_browser_window(10, 100, 200)
    assert ("send-input", 1) in calls
    assert not any(call[0] == "fallback-mouse" for call in calls)


def test_click_is_refused_when_cursor_never_settles_on_target(monkeypatch):
    """Живой сбой 25.11.2026: клик «на лету» превращался в выделение текста."""
    calls = []

    class User32:
        def SetCursorPos(self, x, y):
            calls.append(("cursor", x, y))
            return True

        def GetCursorPos(self, point_ptr):
            point = point_ptr._obj
            point.x, point.y = 5, 5  # курсор упорно не на цели
            return True

        def mouse_event(self, *args):
            calls.append(("mouse", *args))

    monkeypatch.setattr(browser_save, "window_bounds", lambda _handle: (0, 0, 800, 600))
    monkeypatch.setattr(browser_save, "_user32", lambda: User32())
    monkeypatch.setattr(browser_save, "active_window_handle", lambda: 10)
    monkeypatch.setattr(browser_save.time, "sleep", lambda _s: None)
    assert not browser_save.click_browser_window(10, 100, 200)
    assert not any(call[0] == "mouse" for call in calls)


def test_hook_blind_attempt_for_unknown_viewer_title_after_wait(tmp_path, monkeypatch):
    """Просмотрщик с незнакомым заголовком: слепой Ctrl+S после выдержки, не сразу."""
    moments = iter((0.0, 1.0, 2.0, 20.0, 21.5))
    actions: list[tuple[int, str, bool]] = []
    tab = _yandex(11, "VSCMS Document Viewer")  # ни один известный шаблон не совпал

    def windows():
        if not hasattr(windows, "calls"):
            windows.calls = 0
        windows.calls += 1
        return [] if windows.calls == 1 else [tab]

    monkeypatch.setattr(browser_save, "available", lambda: True)

    def save(target, *, windows, require_pdf_title=True, **_kwargs):
        chosen = windows()[0]
        actions.append((chosen.handle, chosen.title, require_pdf_title))
        Path(target).write_bytes(b"%PDF-1.7 deed")
        return Path(target)

    monkeypatch.setattr(browser_save, "save_pdf_if_possible", save)
    hook = browser_save.auto_save_hook(
        tmp_path, ready_delay=4.0, blind_after=15.0, stable_for=1.0,
        windows_fn=windows, monotonic=lambda: next(moments),
    )
    hook.before_open("42")
    assert hook("42") is None  # заголовок только появился
    assert hook("42") is None  # стабилен, но слепая выдержка ещё не вышла
    assert hook("42") == tmp_path / "tsai_step0_42.pdf"
    assert actions == [(11, "VSCMS Document Viewer", False)]
    assert "слепая попытка" in hook.state("42")


def test_hook_never_blind_saves_a_cloudflare_title(tmp_path, monkeypatch):
    """На странице проверки Ctrl+S сохранил бы HTML и сжёг единственную попытку."""
    moments = iter((0.0, 1.0, 2.0, 100.0, 200.0))
    tab = _yandex(11, "Just a moment...")

    def windows():
        if not hasattr(windows, "calls"):
            windows.calls = 0
        windows.calls += 1
        return [] if windows.calls == 1 else [tab]

    monkeypatch.setattr(browser_save, "available", lambda: True)
    monkeypatch.setattr(
        browser_save, "save_pdf_if_possible",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("Ctrl+S на капче запрещён")))
    hook = browser_save.auto_save_hook(
        tmp_path, ready_delay=4.0, blind_after=15.0, stable_for=1.0,
        windows_fn=windows, monotonic=lambda: next(moments),
    )
    hook.before_open("42")
    assert hook("42") is None
    assert hook("42") is None
    assert hook("42") is None
    assert "Cloudflare" in hook.state("42")


def test_hook_state_explains_unpinned_window(tmp_path, monkeypatch):
    """Диагностируемость: состояние хука называет увиденные заголовки окон."""
    moments = iter((0.0, 1.0))
    tabs = [_yandex(11, "One"), _yandex(12, "Two")]

    def windows():
        if not hasattr(windows, "calls"):
            windows.calls = 0
        windows.calls += 1
        return [] if windows.calls == 1 else tabs

    monkeypatch.setattr(browser_save, "available", lambda: True)
    hook = browser_save.auto_save_hook(
        tmp_path, windows_fn=windows, active_handle_fn=lambda: 0,
        monotonic=lambda: next(moments),
    )
    hook.before_open("42")
    assert hook("42") is None
    state = hook.state("42")
    assert "не закреплено" in state and "One" in state and "Two" in state


def test_preferred_browser_normalization_and_config_loading():
    assert browser_save.normalize_preferred_browser("chrome") == "chrome"
    assert browser_save.normalize_preferred_browser("Google Chrome") == "chrome"
    assert browser_save.normalize_preferred_browser("msedge.exe") == "edge"
    assert browser_save.normalize_preferred_browser("Яндекс Браузер") == "yandex"
    assert browser_save.normalize_preferred_browser("unknown") == "auto"
    assert browser_save.preferred_browser_from_config({"deed_intake": {"preferred_browser": "edge"}}) == "edge"
    assert browser_save.preferred_browser_from_config({}) == "auto"


def test_pick_browser_window_strictly_filters_when_preferred_is_set():
    edge = BrowserWindow(1, "Portal", "msedge.exe", r"C:\Edge\msedge.exe")
    chrome = BrowserWindow(2, "Tab", "chrome.exe", r"C:\Chrome\chrome.exe")
    yandex = _yandex(3, "Portal")

    # Even if Edge is focused and Yandex has title mark, preferred="chrome" chooses ONLY Chrome.
    picked = browser_save.pick_browser_window(
        [edge, chrome, yandex], active_handle=1, preferred="chrome", title_marks=("portal",),
    )
    assert picked == chrome

    # If preferred browser isn't running, fail closed (None) instead of hijacking Edge/Chrome.
    assert browser_save.pick_browser_window([edge, chrome], preferred="firefox") is None


def test_auto_mode_prefers_os_default_and_pinned_browser_over_z_order():
    browser_save.reset_auto_browser_pin()
    edge = BrowserWindow(1, "Edge Tab", "msedge.exe", r"C:\Edge\msedge.exe")
    chrome = BrowserWindow(2, "Chrome Tab", "chrome.exe", r"C:\Chrome\chrome.exe")
    try:
        # When neither window is active, align with Windows default browser ("chrome") instead of Z-order windows[0] ("edge").
        picked = browser_save.pick_browser_window(
            [edge, chrome], active_handle=0, preferred="auto", default_key_fn=lambda: "chrome",
        )
        assert picked == chrome

        # Remembering Chrome from a portal lookup keeps subsequent auto picks on Chrome even if Edge is windows[0].
        browser_save.pick_browser_window([chrome], preferred="auto", remember=True)
        picked_next = browser_save.pick_browser_window(
            [edge, chrome], active_handle=0, preferred="auto", default_key_fn=lambda: "edge",
        )
        assert picked_next == chrome
    finally:
        browser_save.reset_auto_browser_pin()


def test_open_url_launches_preferred_browser_directly_and_auto_stays_consistent():
    browser_save.reset_auto_browser_pin()
    launched: list[list[str]] = []
    web_opened: list[str] = []
    yandex = _yandex(10, "Property Information Portal")
    try:
        ok = browser_save.open_url(
            "https://example.test/doc",
            preferred="chrome",
            windows_fn=lambda: [yandex],
            find_exe_fn=lambda key, **_k: r"C:\Chrome\chrome.exe" if key == "chrome" else "",
            popen=lambda cmd, **_kw: launched.append(list(cmd)) or object(),
            webbrowser_open=lambda url, **_kw: web_opened.append(url) or True,
        )
        assert ok is True
        assert launched == [[r"C:\Chrome\chrome.exe", "https://example.test/doc"]]
        assert web_opened == []

        # In auto mode, once Yandex was pinned by portal/prior action AND Yandex is still open,
        # open_url stays in Yandex rather than flipping to OS default.
        launched.clear()
        browser_save.pick_browser_window([yandex], preferred="auto", remember=True)
        browser_save.open_url(
            "https://example.test/clerk",
            preferred="auto",
            windows_fn=lambda: [yandex],
            find_exe_fn=lambda key, **_k: yandex.image_path if key == "yandex" else "",
            popen=lambda cmd, **_kw: launched.append(list(cmd)) or object(),
            webbrowser_open=lambda url, **_kw: web_opened.append(url) or True,
        )
        assert launched == [[yandex.image_path, "https://example.test/clerk"]]
        assert web_opened == []

        # Once the Yandex window is closed, auto falls back to OS default browser.
        launched.clear()
        browser_save.open_url(
            "https://example.test/clerk2",
            preferred="auto",
            windows_fn=lambda: [],
            find_exe_fn=lambda key, **_k: yandex.image_path if key == "yandex" else "",
            popen=lambda cmd, **_kw: launched.append(list(cmd)) or object(),
            webbrowser_open=lambda url, **_kw: web_opened.append(url) or True,
            default_key_fn=lambda: "edge",
        )
        assert web_opened == ["https://example.test/clerk2"]
    finally:
        browser_save.reset_auto_browser_pin()


def test_auto_save_hook_ignores_other_browsers_when_preferred_is_set(tmp_path, monkeypatch):
    moments = iter((0.0, 1.0, 2.0, 7.0, 8.5))
    chrome_old = BrowserWindow(1, "New Tab", "chrome.exe", r"C:\Chrome\chrome.exe")
    chrome_new = BrowserWindow(2, "Warranty Deed.pdf", "chrome.exe", r"C:\Chrome\chrome.exe")
    edge_bg = BrowserWindow(3, "Edge Background Refresh", "msedge.exe", r"C:\Edge\msedge.exe")
    actions: list[int] = []

    calls = {"n": 0}

    def windows():
        calls["n"] += 1
        if calls["n"] == 1:
            return [chrome_old]
        return [chrome_old, edge_bg, chrome_new]

    monkeypatch.setattr(browser_save, "available", lambda: True)

    def save(target, *, windows, **_kwargs):
        actions.append(windows()[0].handle)
        Path(target).write_bytes(b"%PDF-1.7 deed")
        return Path(target)

    monkeypatch.setattr(browser_save, "save_pdf_if_possible", save)
    hook = browser_save.auto_save_hook(
        tmp_path, ready_delay=4.0, stable_for=1.0, windows_fn=windows,
        active_handle_fn=lambda: 0, monotonic=lambda: next(moments), preferred="chrome",
    )
    hook.before_open("876753")
    assert hook("876753") is None  # PDF title first seen at 1.0
    assert hook("876753") is None  # stable_for met at 2.0, ready_delay (4.0) not met yet
    assert hook("876753") == tmp_path / "tsai_step0_876753.pdf"  # ready at 7.0
    assert actions == [2]


def test_hook_never_blind_saves_russian_cloudflare_odin_moment_and_saves_after_viewer_loads(
        tmp_path, monkeypatch):
    """Live bug 2026-11-27: 'Один момент… — Яндекс Браузер' burned one-shot Ctrl+S."""
    moments = iter((0.0, 1.0, 16.0, 20.0, 21.5, 23.0))
    actions: list[str] = []

    calls = {"n": 0}

    def windows():
        calls["n"] += 1
        if calls["n"] == 1:
            return []
        if calls["n"] in (2, 3):  # still Cloudflare even past blind_after=15s
            return [_yandex(11, "Один момент… — Яндекс\xa0Браузер")]
        return [_yandex(11, "viewer — Яндекс\xa0Браузер")]

    monkeypatch.setattr(browser_save, "available", lambda: True)

    def save(target, *, windows, **_kwargs):
        title = windows()[0].title
        actions.append(title)
        Path(target).write_bytes(b"%PDF-1.7 deed")
        return Path(target)

    monkeypatch.setattr(browser_save, "save_pdf_if_possible", save)
    hook = browser_save.auto_save_hook(
        tmp_path, ready_delay=4.0, blind_after=15.0, stable_for=1.0,
        windows_fn=windows, monotonic=lambda: next(moments),
    )
    hook.before_open("58421")
    assert hook("58421") is None
    assert hook("58421") is None  # 16s >= blind_after, must NOT burn Ctrl+S on 'Один момент…'
    assert "Cloudflare" in hook.state("58421")
    assert actions == []
    assert hook("58421") is None  # 'viewer' just appeared at 20.0s
    assert hook("58421") == tmp_path / "tsai_step0_58421.pdf"
    assert actions == ["viewer — Яндекс\xa0Браузер"]


def test_hook_still_saves_recognized_pdf_after_failed_blind_attempt(tmp_path, monkeypatch):
    moments = iter((0.0, 1.0, 16.0, 20.0, 21.5, 25.0))
    actions: list[tuple[str, bool]] = []

    calls = {"n": 0}

    def windows():
        calls["n"] += 1
        if calls["n"] == 1:
            return []
        if calls["n"] in (2, 3):
            return [_yandex(11, "Please stand by")]  # unknown interstitial triggers blind once
        return [_yandex(11, "viewer")]

    monkeypatch.setattr(browser_save, "available", lambda: True)

    def save(target, *, windows, require_pdf_title=True, **_kwargs):
        title = windows()[0].title
        actions.append((title, require_pdf_title))
        if not require_pdf_title:
            return None  # blind attempt failed on interstitial
        Path(target).write_bytes(b"%PDF-1.7 deed")
        return Path(target)

    monkeypatch.setattr(browser_save, "save_pdf_if_possible", save)
    hook = browser_save.auto_save_hook(
        tmp_path, ready_delay=4.0, blind_after=15.0, stable_for=1.0,
        windows_fn=windows, monotonic=lambda: next(moments),
    )
    hook.before_open("58421")
    assert hook("58421") is None  # 1.0s
    assert hook("58421") is None  # 16.0s -> blind attempt fails, returns None
    assert hook("58421") is None  # 20.0s -> viewer just appeared
    assert hook("58421") == tmp_path / "tsai_step0_58421.pdf"  # 21.5s -> recognized PDF still saves!
    assert actions == [("Please stand by", False), ("viewer", True)]
