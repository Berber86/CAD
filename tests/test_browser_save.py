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
