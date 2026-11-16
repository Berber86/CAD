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
