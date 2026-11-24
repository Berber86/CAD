"""Браузерный резерв ACRIS: форма BBL → список → клик IMG → читалка.

Свежий документ, которого ещё нет в NYC Open Data (живой случай 3876:
CRFN 2026000329272), открывается как у человека: кликом IMG в результатах.
Экран — фейковый браузер с состояниями; сети и настоящего окна нет.
"""
from __future__ import annotations

import itertools

import pytest

import acris_browser
import acris_viewer
import browser_save

CRFN = "2026000329272"


def _row(text, box):
    return acris_viewer.ScreenText(text, box)


FORM = [
    _row("Select Borough/County:", (100, 100, 320, 120)),
    _row("Enter Block:", (400, 100, 520, 120)),
    _row("Enter Lot:", (700, 100, 800, 120)),
    _row("Search", (100, 300, 180, 326)),
]
RESULTS = [
    _row("Search Results By Parcel Identifier", (30, 60, 500, 90)),
    _row(CRFN, (150, 200, 320, 222)),
    _row("MORTGAGE", (700, 200, 800, 222)),
]
VIEWER = [
    _row("DOCUMENT ID: 2026111700598001", (100, 100, 400, 124)),
    _row("1 of 11", (60, 150, 140, 172)),
]
DETAILS = [_row("Detailed Document Information", (30, 60, 500, 90))]


class FakeBrowser:
    def __init__(self, crfn_on_results: bool = True, first_click_misses: bool = False):
        self.state = "blank"
        self.clicks: list[tuple[int, int]] = []
        self.keys: list[int] = []
        self.typed: list[str] = []
        self.crfn_on_results = crfn_on_results
        self.first_click_misses = first_click_misses
        self._img_clicks = 0

    # -- примитивы, подставляемые в open_document_image --
    def open_form(self, _handle):
        self.state = "form"

    def capture(self, _handle):
        if self.state == "form":
            return list(FORM)
        if self.state == "results":
            return list(RESULTS if self.crfn_on_results else [RESULTS[0], RESULTS[2]])
        if self.state == "viewer":
            return list(VIEWER)
        if self.state == "details":
            return list(DETAILS)
        return []

    def click(self, _handle, x, y):
        self.clicks.append((x, y))
        if self.state == "form" and (x, y) == FORM[3].center:
            self.state = "results"
        elif self.state == "results" and y == RESULTS[1].center[1]:
            self._img_clicks += 1
            missed = self.first_click_misses and self._img_clicks == 1
            self.state = "details" if missed else "viewer"
        return True

    def key(self, vk):
        self.keys.append(vk)
        return True

    def back(self):
        if self.state == "details":
            self.state = "results"
        return True

    def type_text(self, text):
        self.typed.append(text)
        return True


def _run(fake: FakeBrowser, monkeypatch, **overrides):
    window = browser_save.BrowserWindow(
        handle=7, title="ACRIS - Chrome", executable="chrome.exe",
        image_path=r"C:\Chrome\chrome.exe")
    monkeypatch.setattr(browser_save, "list_windows", lambda: [window])
    monkeypatch.setattr(browser_save, "active_window_handle", lambda: 7)
    monkeypatch.setattr(browser_save, "available", lambda: True)
    monkeypatch.setattr(browser_save, "window_bounds", lambda _h: (0, 0, 1600, 900))
    ticks = itertools.count(0, 10)
    options = dict(
        capture_rows_fn=fake.capture, click_fn=fake.click, focus_fn=lambda _h: True,
        key_fn=fake.key, ctrl_a_fn=lambda: True, type_fn=fake.type_text,
        open_form_fn=fake.open_form, back_fn=fake.back, sleep=lambda _s: None,
        monotonic=lambda: next(ticks),
    )
    options.update(overrides)
    return acris_browser.open_document_image(CRFN, 3, 6643, 62, **options)


def test_browser_fallback_opens_viewer_by_img_click(monkeypatch):
    fake = FakeBrowser()
    _run(fake, monkeypatch)
    assert fake.state == "viewer"
    assert fake.typed == ["6643", "62"]
    # Район выбран клавишами: HOME + 2×Down (Brooklyn — код 3) + Enter.
    assert fake.keys.count(browser_save.VK_DOWN) == 2
    assert fake.keys[-1] == browser_save.VK_RETURN


def test_browser_fallback_retries_img_click_wider_after_det(monkeypatch):
    fake = FakeBrowser(first_click_misses=True)
    _run(fake, monkeypatch)
    assert fake.state == "viewer"
    result_clicks = [c for c in fake.clicks if c[1] == RESULTS[1].center[1]]
    assert len(result_clicks) == 2 and result_clicks[1][0] > result_clicks[0][0]


def test_browser_fallback_reports_missing_crfn_row(monkeypatch, tmp_path):
    fake = FakeBrowser(crfn_on_results=False)
    with pytest.raises(acris_browser.AcrisBrowserError, match="не появилась"):
        _run(fake, monkeypatch, fail_dir=tmp_path)
