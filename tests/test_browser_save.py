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


def test_save_pdf_polls_for_slow_chrome_save_dialog(tmp_path):
    target = tmp_path / "tsai_step0_814433.pdf"
    checks = {"n": 0}

    def dialog_open():
        checks["n"] += 1
        return checks["n"] >= 5

    def type_path(value: str) -> bool:
        target.write_bytes(b"%PDF-1.7 chrome")
        return True

    result = browser_save.save_pdf_if_possible(
        target,
        windows=lambda: [BrowserWindow(10, "viewer - Google Chrome", "chrome.exe", r"C:\Chrome\chrome.exe")],
        foreground_fn=lambda _handle: True,
        active_handle_fn=lambda: 10,
        press_ctrl_s_fn=lambda: True,
        dialog_open_fn=dialog_open,
        type_text_fn=type_path,
        press_enter_fn=lambda: True,
        sleep=lambda _seconds: None,
        wait=0.8,
    )
    assert result == target
    assert checks["n"] == 5


def test_save_pdf_rejects_non_pdf_file_payload(tmp_path):
    target = tmp_path / "tsai_step0_814433.pdf"

    def write_mhtml(value: str) -> bool:
        Path(value).write_text("From: <Saved by Blink>\n<html></html>", encoding="utf-8")
        return True

    result = browser_save.save_pdf_if_possible(
        target,
        windows=lambda: [BrowserWindow(10, "RCC-121449.pdf", "chrome.exe", r"C:\Chrome\chrome.exe")],
        foreground_fn=lambda _handle: True,
        active_handle_fn=lambda: 10,
        press_ctrl_s_fn=lambda: True,
        dialog_open_fn=lambda: True,
        type_text_fn=write_mhtml,
        press_enter_fn=lambda: True,
        sleep=lambda _seconds: None,
    )
    assert result is None


def test_confirm_open_save_dialog_confirms_pdf_and_cancels_webpage(tmp_path):
    events: list[object] = []
    seen: set[int] = set()
    target = tmp_path / "tsai_step0_814433.pdf"

    # Different process -> ignored completely.
    assert browser_save.confirm_open_save_dialog(
        10,
        target=target,
        seen_dialogs=seen,
        active_handle_fn=lambda: 500,
        active_title_fn=lambda: "Сохранение",
        same_process_fn=lambda _d, _b: False,
        is_webpage_fn=lambda _d: False,
        press_enter_fn=lambda: events.append("enter") or True,
    ) == ""
    assert events == []

    # Chrome Save As offering «Веб-страница, один файл» -> cancelled via Esc!
    assert browser_save.confirm_open_save_dialog(
        10,
        target=target,
        seen_dialogs=seen,
        active_handle_fn=lambda: 501,
        active_title_fn=lambda: "Сохранение",
        same_process_fn=lambda _d, _b: True,
        is_webpage_fn=lambda _d: True,
        press_escape_fn=lambda: events.append("esc") or True,
        press_enter_fn=lambda: events.append("enter") or True,
    ) == "webpage_cancelled"
    assert events == ["esc"]
    assert "Веб-страница" in browser_save.last_save_note()

    # Genuine PDF Save dialog -> confirmed once, and second call skips already-seen HWND.
    events.clear()
    assert browser_save.confirm_open_save_dialog(
        10,
        target=target,
        seen_dialogs=seen,
        active_handle_fn=lambda: 502,
        active_title_fn=lambda: "Сохранение",
        same_process_fn=lambda _d, _b: True,
        is_webpage_fn=lambda _d: False,
        press_ctrl_a_fn=lambda: events.append("ctrl+a") or True,
        type_text_fn=lambda text: events.append(("type", text)) or True,
        press_enter_fn=lambda: events.append("enter") or True,
    ) == "confirmed"
    assert events == ["ctrl+a", ("type", str(target)), "enter"]
    assert browser_save.confirm_open_save_dialog(
        10,
        target=target,
        seen_dialogs=seen,
        active_handle_fn=lambda: 502,
        active_title_fn=lambda: "Сохранение",
        same_process_fn=lambda _d, _b: True,
        is_webpage_fn=lambda _d: False,
        press_enter_fn=lambda: events.append("enter2") or True,
    ) == ""


def test_hook_handles_immediate_save_dialog_from_chrome_ask_where_to_save(tmp_path, monkeypatch):
    moments = iter((0.0, 1.0, 2.0))
    chrome_dlg = BrowserWindow(55, "Сохранение", "chrome.exe", r"C:\Chrome\chrome.exe")
    calls = {"n": 0}

    def windows():
        calls["n"] += 1
        return [] if calls["n"] == 1 else [chrome_dlg]

    monkeypatch.setattr(browser_save, "available", lambda: True)
    monkeypatch.setattr(
        browser_save, "save_pdf_if_possible",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("Ctrl+S must not run on Save As dialog")),
    )
    monkeypatch.setattr(browser_save.time, "sleep", lambda _s: None)

    def fake_confirm(browser_handle, *, target=None, seen_dialogs=None, **_kwargs):
        assert browser_handle == 55
        Path(target).write_bytes(b"%PDF-1.7 auto-confirmed")
        return "confirmed"

    monkeypatch.setattr(browser_save, "confirm_open_save_dialog", fake_confirm)
    hook = browser_save.auto_save_hook(
        tmp_path, blind_after=15.0, windows_fn=windows,
        active_handle_fn=lambda: 55, monotonic=lambda: next(moments), preferred="chrome",
    )
    hook.before_open("814433")
    assert hook("814433") == tmp_path / "tsai_step0_814433.pdf"


def test_foreground_switches_to_target_window_when_same_browser_has_two_titled_windows(monkeypatch):
    calls = []
    win1 = BrowserWindow(10, "New Tab - Google Chrome", "chrome.exe", r"C:\Chrome\chrome.exe")
    win2 = BrowserWindow(20, "Chat - Google Chrome", "chrome.exe", r"C:\Chrome\chrome.exe")
    fg_state = {"hwnd": 20}

    class User32:
        def IsIconic(self, _h):
            return False

        def GetForegroundWindow(self):
            return fg_state["hwnd"]

        def GetWindowThreadProcessId(self, handle, process_id):
            process_id._obj.value = 999  # both windows share the same chrome.exe PID
            return 50

        def AttachThreadInput(self, _a, _b, _attach):
            return True

        def AllowSetForegroundWindow(self, _pid):
            return True

        def BringWindowToTop(self, handle):
            calls.append(("top", handle))
            return True

        def SetForegroundWindow(self, handle):
            calls.append(("activate", handle))
            fg_state["hwnd"] = handle
            return True

    class Kernel32:
        def GetCurrentThreadId(self):
            return 5

    import ctypes
    monkeypatch.setattr(browser_save, "list_windows", lambda: [win1, win2])
    monkeypatch.setattr(browser_save, "_user32", lambda: User32())
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: Kernel32(), raising=False)
    monkeypatch.setattr(browser_save.time, "sleep", lambda _s: None)
    assert browser_save.foreground(10, timeout=0.1)
    assert ("activate", 10) in calls


def _make_adobe_toolbar_image():
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (1616, 876), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    # Gray document viewport below the toolbar (y >= 155)
    draw.rectangle((0, 155, 1615, 875), fill=(235, 235, 235))
    # Red Adobe Acrobat badge on left of toolbar (y = 107..138)
    draw.rectangle((16, 111, 34, 134), fill=(220, 35, 35))
    # Dark "Общий доступ" / "Share" pill on right of toolbar (y = 107..138, x = 1426..1546)
    draw.rectangle((1426, 107, 1546, 138), fill=(44, 44, 44))
    # Printer icon at x = 1351..1368 (wide top paper + wide bottom)
    draw.rectangle((1353, 115, 1366, 118), fill=(50, 50, 50))
    draw.rectangle((1351, 118, 1368, 127), fill=(50, 50, 50))
    draw.rectangle((1353, 128, 1366, 130), fill=(50, 50, 50))
    # Download arrow (↓) into tray at x = 1316..1331, y = 115..128 (center ≈ (1323, 122))
    draw.rectangle((1323, 115, 1324, 124), fill=(50, 50, 50))  # narrow top shaft
    draw.polygon([(1319, 121), (1328, 121), (1323, 125)], fill=(50, 50, 50))  # arrowhead
    draw.rectangle((1316, 124, 1317, 128), fill=(50, 50, 50))  # left tray wall
    draw.rectangle((1330, 124, 1331, 128), fill=(50, 50, 50))  # right tray wall
    draw.rectangle((1316, 127, 1331, 128), fill=(50, 50, 50))  # bottom tray bar
    return img


def test_find_adobe_download_point_detects_toolbar_and_ignores_plain_pages():
    from PIL import Image

    adobe_img = _make_adobe_toolbar_image()
    pt = browser_save.find_adobe_download_point(adobe_img)
    assert pt is not None
    assert 1316 <= pt[0] <= 1331
    assert 115 <= pt[1] <= 130

    # Plain webpage without Adobe toolbar must return None
    plain = Image.new("RGB", (1616, 876), (255, 255, 255))
    assert browser_save.find_adobe_download_point(plain) is None


def test_save_pdf_prefers_adobe_download_button_over_ctrl_s(tmp_path):
    target = tmp_path / "tsai_step0_1799244.pdf"
    calls: list[object] = []

    # Case 1: Chrome auto-downloads RCC-*.pdf (no Save dialog) after clicking ↓ ->
    # save_pdf_if_possible must NOT send Ctrl+S and leaves an informative note.
    res = browser_save.save_pdf_if_possible(
        target,
        windows=lambda: [BrowserWindow(10, "viewer.pdf - Google Chrome", "chrome.exe", r"C:\Chrome\chrome.exe")],
        foreground_fn=lambda _h: True,
        active_handle_fn=lambda: 10,
        press_ctrl_s_fn=lambda: calls.append("ctrl+s") or True,
        dialog_open_fn=lambda: False,
        adobe_download_fn=lambda h: calls.append(("adobe", h)) or (1323, 122),
        sleep=lambda _s: None,
    )
    assert res is None
    assert calls == [("adobe", 10)]
    assert "Adobe Acrobat" in browser_save.last_save_note()

    # Case 2: Ask-where-to-save is ON, so clicking ↓ opens Save As -> confirmed cleanly.
    calls.clear()
    res2 = browser_save.save_pdf_if_possible(
        target,
        windows=lambda: [BrowserWindow(10, "viewer.pdf - Google Chrome", "chrome.exe", r"C:\Chrome\chrome.exe")],
        foreground_fn=lambda _h: True,
        active_handle_fn=lambda: 10,
        press_ctrl_s_fn=lambda: calls.append("ctrl+s") or True,
        dialog_open_fn=lambda: True,
        type_text_fn=lambda p: Path(p).write_bytes(b"%PDF-1.7 real") or True,
        press_enter_fn=lambda: True,
        adobe_download_fn=lambda h: calls.append(("adobe", h)) or (1323, 122),
        sleep=lambda _s: None,
    )
    assert res2 == target
    assert calls == [("adobe", 10)]


def test_save_pdf_unlinks_mhtml_webpage_payload_and_falls_back_to_adobe(tmp_path):
    target = tmp_path / "tsai_step0_1712449.pdf"
    events: list[object] = []
    adobe_calls = {"n": 0}

    def fake_adobe(handle: int):
        adobe_calls["n"] += 1
        events.append(("adobe", handle))
        if adobe_calls["n"] == 1:
            return None  # Pretend pre-Ctrl+S Adobe toolbar was missed once
        return (1323, 122)

    dialog_checks = {"n": 0}

    def fake_dialog():
        dialog_checks["n"] += 1
        # Open on Ctrl+S (checks 1), closed/no second Save dialog after Adobe ↓ click
        return dialog_checks["n"] == 1

    def write_mhtml(value: str) -> bool:
        events.append(("write", value))
        Path(value).write_text("From: <Saved by Blink>\n<html></html>", encoding="utf-8")
        return True

    res = browser_save.save_pdf_if_possible(
        target,
        windows=lambda: [BrowserWindow(10, "viewer.pdf - Google Chrome", "chrome.exe", r"C:\Chrome\chrome.exe")],
        foreground_fn=lambda _h: True,
        active_handle_fn=lambda: 10,
        press_ctrl_s_fn=lambda: events.append("ctrl+s") or True,
        dialog_open_fn=fake_dialog,
        type_text_fn=write_mhtml,
        press_enter_fn=lambda: True,
        adobe_download_fn=fake_adobe,
        sleep=lambda _s: None,
    )
    assert res is None
    assert not target.exists()  # Fake MHTML named .pdf must be cleaned up!
    assert adobe_calls["n"] == 2
    assert "MHTML" in browser_save.last_save_note()


def test_text_indicates_webpage_save_matches_rapidocr_homoglyphs():
    assert browser_save._text_indicates_webpage_save("Веб-страница, один файл")
    assert browser_save._text_indicates_webpage_save("Webpage, Single File (*.mhtml)")
    assert browser_save._text_indicates_webpage_save("be6-cTpaHHLa, oAHH oaNn")
    assert not browser_save._text_indicates_webpage_save("Документ PDF (*.pdf)")
    assert not browser_save._text_indicates_webpage_save("Adobe Acrobat Document (*.pdf)")


def test_find_adobe_download_point_matches_modal_dimmed_toolbar():
    from PIL import Image, ImageDraw

    # Modal-dimmed Adobe Acrobat window behind a Windows Save As dialog (exact
    # palette observed on 2559052: toolbar white dimmed to ~153, red icon ~150,
    # dark "Общий доступ" pill ~26, icons ~30).
    img = Image.new("RGB", (1616, 876), (153, 153, 153))
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 155, 1615, 875), fill=(140, 140, 140))
    draw.rectangle((16, 111, 34, 134), fill=(150, 40, 35))
    draw.rectangle((1426, 107, 1546, 138), fill=(26, 26, 26))
    draw.rectangle((1353, 115, 1366, 118), fill=(30, 30, 30))
    draw.rectangle((1351, 118, 1368, 127), fill=(30, 30, 30))
    draw.rectangle((1353, 128, 1366, 130), fill=(30, 30, 30))
    draw.rectangle((1323, 115, 1324, 124), fill=(30, 30, 30))
    draw.polygon([(1319, 121), (1328, 121), (1323, 125)], fill=(30, 30, 30))
    draw.rectangle((1316, 124, 1317, 128), fill=(30, 30, 30))
    draw.rectangle((1330, 124, 1331, 128), fill=(30, 30, 30))
    draw.rectangle((1316, 127, 1331, 128), fill=(30, 30, 30))

    pt = browser_save.find_adobe_download_point(img)
    assert pt is not None
    assert 1316 <= pt[0] <= 1331
    assert 115 <= pt[1] <= 130


def test_confirm_open_save_dialog_focuses_unfocused_same_browser_dialog(tmp_path):
    target = tmp_path / "tsai_step0_591091.pdf"
    chrome_win = BrowserWindow(10, "viewer - Google Chrome", "chrome.exe", r"C:\Chrome\chrome.exe")
    save_win = BrowserWindow(20, "Сохранение", "chrome.exe", r"C:\Chrome\chrome.exe")
    events: list[object] = []

    def fake_fg(hwnd: int) -> bool:
        events.append(("fg", hwnd))
        return True

    def fake_type(text: str) -> bool:
        events.append(("type", text))
        Path(text).write_bytes(b"%PDF-1.7 real")
        return True

    res = browser_save.confirm_open_save_dialog(
        10,
        target=target,
        seen_dialogs=set(),
        active_handle_fn=lambda: 10,  # Chrome main tab is focused, Save As modal is NOT
        active_title_fn=lambda: chrome_win.title,
        is_webpage_fn=lambda _h: False,
        press_ctrl_a_fn=lambda: events.append("ctrl+a") or True,
        type_text_fn=fake_type,
        press_enter_fn=lambda: events.append("enter") or True,
        windows_fn=lambda: [chrome_win, save_win],
        foreground_fn=fake_fg,
    )
    assert res == "confirmed"
    assert ("fg", 20) in events
    assert ("type", str(target)) in events
    assert target.read_bytes() == b"%PDF-1.7 real"


def test_type_text_uses_sendinput_utf16_for_cyrillic_and_falls_back(monkeypatch):
    sent_units: list[tuple[int, int]] = []
    batch_sizes: list[int] = []

    class FakeSendInput:
        def __call__(self, count, inputs, _size):
            batch_sizes.append(count)
            for idx in range(count):
                sent_units.append((int(inputs[idx].ki.wScan), int(inputs[idx].ki.dwFlags)))
            return count

    class User32WithSendInput:
        SendInput = FakeSendInput()

    monkeypatch.setattr(browser_save.time, "sleep", lambda _s: None)
    monkeypatch.setattr(browser_save, "_user32", lambda: User32WithSendInput())
    sample = r"C:\Users\Николай\Загрузки\doc.pdf"
    assert browser_save.type_text(sample)
    raw = sample.encode("utf-16-le")
    expected_units = [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]
    assert [u for u, flags in sent_units if flags == browser_save.KEYEVENTF_UNICODE] == expected_units
    assert any(u > 255 for u, _ in sent_units)  # Cyrillic units NOT truncated to 8-bit BYTE
    assert all(n == 1 for n in batch_sizes)  # Paced 1 event per SendInput call, never bulk burst

    # Hybrid Windows case (both SendInput and keybd_event present):
    # ASCII <= 0xFF uses proven keybd_event path; > 0xFF Cyrillic uses SendInput(1, ...).
    hybrid_events: list[tuple[str, int, int]] = []

    class HybridSendInput:
        def __call__(self, count, inputs, _size):
            assert count == 1
            hybrid_events.append(("sendinput", int(inputs[0].ki.wScan), int(inputs[0].ki.dwFlags)))
            return 1

    class User32Windows:
        SendInput = HybridSendInput()

        def keybd_event(self, _vk, scan, flags, _extra):
            hybrid_events.append(("keybd", scan, flags))

    monkeypatch.setattr(browser_save, "_user32", lambda: User32Windows())
    assert browser_save.type_text("AЯ")
    assert hybrid_events == [
        ("keybd", ord("A"), browser_save.KEYEVENTF_UNICODE),
        ("keybd", ord("A"), browser_save.KEYEVENTF_UNICODE | browser_save.KEYEVENTF_KEYUP),
        ("sendinput", ord("Я"), browser_save.KEYEVENTF_UNICODE),
        ("sendinput", ord("Я"), browser_save.KEYEVENTF_UNICODE | browser_save.KEYEVENTF_KEYUP),
    ]

    # Legacy fallback when SendInput attribute is absent
    legacy_calls: list[tuple[int, int, int]] = []

    class User32Legacy:
        def keybd_event(self, vk, scan, flags, _extra):
            legacy_calls.append((vk, scan, flags))

    monkeypatch.setattr(browser_save, "_user32", lambda: User32Legacy())
    assert browser_save.type_text("ab")
    assert legacy_calls == [
        (0, ord("a"), browser_save.KEYEVENTF_UNICODE),
        (0, ord("a"), browser_save.KEYEVENTF_UNICODE | browser_save.KEYEVENTF_KEYUP),
        (0, ord("b"), browser_save.KEYEVENTF_UNICODE),
        (0, ord("b"), browser_save.KEYEVENTF_UNICODE | browser_save.KEYEVENTF_KEYUP),
    ]


def test_click_browser_window_verify_active_after_flag(monkeypatch):
    class FakeUser32:
        def SetCursorPos(self, _x, _y):
            return True

        def mouse_event(self, *_args):
            # Simulate Save button click immediately closing the modal dialog
            fg["hwnd"] = 10
            return True

    fg = {"hwnd": 20}
    monkeypatch.setattr(browser_save, "window_bounds", lambda _h: (0, 0, 800, 600))
    monkeypatch.setattr(browser_save, "_user32", lambda: FakeUser32())
    monkeypatch.setattr(browser_save, "active_window_handle", lambda: fg["hwnd"])

    # Default (verify_active_after=True) sees focus changed -> False
    assert not browser_save.click_browser_window(20, 100, 200)
    fg["hwnd"] = 20
    # Button-submit mode (verify_active_after=False) treats closed dialog click as True
    assert browser_save.click_browser_window(20, 100, 200, verify_active_after=False)


def test_auto_save_hook_pins_tab_over_save_dialog_retries_and_cleans_up(tmp_path, monkeypatch):
    prior_tab = BrowserWindow(10, "Richmond County Clerk - Google Chrome", "chrome.exe", r"C:\Chrome\chrome.exe")
    chrome_tab = BrowserWindow(10, "viewer - Google Chrome", "chrome.exe", r"C:\Chrome\chrome.exe")
    save_dlg = BrowserWindow(20, "Сохранение", "chrome.exe", r"C:\Chrome\chrome.exe")
    # Put a stale MHTML payload at pending_path first; cleanup/submit must clear it
    stale = browser_save.pending_path(tmp_path, "591091")
    stale.write_text("From: <Saved by Blink>\n<html></html>", encoding="utf-8")

    now = {"t": 0.0}
    dismissed: list[str] = []
    confirmed_calls: list[int] = []

    monkeypatch.setattr(browser_save, "available", lambda: True)
    monkeypatch.setattr(browser_save.time, "sleep", lambda _s: None)

    def fake_confirm(browser_handle, *, target=None, seen_dialogs=None, **_kwargs):
        if seen_dialogs is not None and 20 in seen_dialogs:
            return ""
        confirmed_calls.append(browser_handle)
        if seen_dialogs is not None:
            seen_dialogs.add(20)
        return "confirmed"

    monkeypatch.setattr(browser_save, "confirm_open_save_dialog", fake_confirm)

    current_wins = [prior_tab]
    hook = browser_save.auto_save_hook(
        tmp_path,
        windows_fn=lambda: list(current_wins),
        active_handle_fn=lambda: 20,  # Save dialog is active when poll runs
        monotonic=lambda: now["t"],
        dismiss_fn=lambda: dismissed.append("called") or 1,
    )
    hook.before_open("591091")
    assert dismissed == ["called"]  # before_open cleans lingering dialogs
    assert not stale.exists()  # stale MHTML pending file was unlinked

    # Simulate browser opening viewer + Save As modal after before_open()
    current_wins[:] = [chrome_tab, save_dlg]

    # Poll #1 (t=0.5): both chrome_tab and save_dlg are new; must pin chrome_tab (10), NOT save_dlg (20)
    now["t"] = 0.5
    assert hook("591091") is None
    assert confirmed_calls == [10]

    # Poll #2 (t=1.0): dialog still open within 3s retry cooldown -> skips re-confirm
    now["t"] = 1.0
    assert hook("591091") is None
    assert confirmed_calls == [10]

    # Poll #3 (t=4.0): >= 3s elapsed while dialog lingers -> retries!
    now["t"] = 4.0
    assert hook("591091") is None
    assert confirmed_calls == [10, 10]

    # Poll #4 (t=4.5): write real PDF -> attempt returns it!
    stale.write_bytes(b"%PDF-1.7 saved")
    assert hook("591091") == stale

    # cleanup() always dismisses lingering dialogs via dismiss_fn
    hook.cleanup("591091")
    assert dismissed == ["called", "called"]


def test_title_looks_like_new_tab_rules():
    assert browser_save.title_looks_like_new_tab("Новая\xa0вкладка — Яндекс\xa0Браузер", browser="yandex")
    assert browser_save.title_looks_like_new_tab("Быстрый доступ — Яндекс Браузер", browser="yandex")
    assert browser_save.title_looks_like_new_tab("Яндекс\xa0Браузер", browser="yandex")
    assert not browser_save.title_looks_like_new_tab("viewer — Яндекс\xa0Браузер", browser="yandex")
    assert not browser_save.title_looks_like_new_tab("Some Tab - Google Chrome", browser="chrome")
    assert browser_save.title_looks_like_new_tab("New Tab - Google Chrome", browser="chrome")


def test_looks_like_yandex_new_tab_screen_ocr_and_visual_diff():
    from PIL import Image, ImageDraw

    def make_viewer() -> Image.Image:
        img = Image.new("RGB", (1600, 900), (245, 245, 245))
        d = ImageDraw.Draw(img)
        d.rectangle((0, 0, 1600, 92), fill=(224, 222, 232))
        # Dark URL characters in left omnibox (x=180..620, y=54..72)
        for x in range(180, 620, 6):
            d.rectangle((x, 54, x + 3, 72), fill=(35, 35, 45))
        return img

    def make_tablo() -> Image.Image:
        img = Image.new("RGB", (1600, 900), (195, 160, 210))
        d = ImageDraw.Draw(img)
        d.rectangle((0, 0, 1600, 92), fill=(224, 222, 232))
        # Placeholder text only on left omnibox (x=180..360, y=56..70), right half clean uniform
        for x in range(180, 360, 8):
            d.rectangle((x, 56, x + 3, 70), fill=(110, 110, 125))
        d.rounded_rectangle((360, 340, 1240, 405), radius=24, fill=(255, 255, 255))
        return img

    viewer = make_viewer()
    tablo = make_tablo()

    # 1. Fail closed when nothing changed (Ctrl+T was dropped on viewer)
    assert not browser_save.looks_like_yandex_new_tab_screen(viewer, before_image=viewer)

    # 2. Visual before->after transition succeeds even without OCR rows
    assert browser_save.looks_like_yandex_new_tab_screen(tablo, before_image=viewer)

    # 3. Omnibox URL present -> strictly fails closed even if viewport changed
    assert not browser_save.looks_like_yandex_new_tab_screen(
        tablo,
        before_image=viewer,
        ocr_rows=[("https://richmondcountyclerk.com/viewer", (180, 52, 540, 74))],
    )

    # 4. Placeholder / RapidOCR homoglyph in omnibox proves New Tab even without before_image
    assert browser_save.looks_like_yandex_new_tab_screen(
        tablo,
        ocr_rows=[("BBeAnTe 3anpoc nnn aApec", (180, 54, 420, 74))],
    )

    # 5. Background "viewer" tab in top tab strip (y < 0.045*h) must NOT poison Табло body OCR
    assert browser_save.looks_like_yandex_new_tab_screen(
        tablo,
        ocr_rows=[
            ("viewer", (620, 12, 680, 32)),
            ("Экспертный режим", (620, 430, 780, 455)),
            ("Алиса AI", (1100, 430, 1180, 455)),
        ],
    )



