"""ACRIS-читалка (Шаг 0): дискета → OK → PDF из «Загрузок», без Windows и сети.

Геометрия замерена по настоящему скриншоту пользователя 2026-11-23:
«73%» — рамка (256, 94, 291, 113), центр дискеты — (425, 104), OK — (256, 204).
"""
from __future__ import annotations

from pathlib import Path

import pytest

import acris_viewer
import browser_save
import rcclerk
from acris_viewer import ScreenText

READY_ROWS = [
    ScreenText("DOCUMENTID:", (127, 48, 225, 63)),
    ScreenText("2024101000085001", (117, 62, 235, 79)),
    ScreenText("of 13", (122, 95, 165, 113)),
    ScreenText("73%", (256, 94, 291, 113)),
]

DIALOG_ROWS = READY_ROWS + [
    ScreenText("Save", (128, 130, 169, 150)),
    ScreenText("Cancel", (152, 194, 207, 214)),
    ScreenText("OK", (242, 194, 270, 214)),
]

#: Живой прогон 2026-11-23: тулбар читалки есть, а вместо страницы — ошибка.
ERROR_ROWS = READY_ROWS + [
    ScreenText("HTTP 405 Method Not Allowed", (60, 300, 330, 318)),
]

DOC_URL = ("https://a836-acris.nyc.gov/DS/DocumentSearch/"
           "DocumentImageView?doc_id=2024101000085001")
WARM_URL = ("https://a836-acris.nyc.gov/DS/DocumentSearch/"
            "BBL?borough=1&block=731&lot=53")


def test_looks_like_viewer_and_dialog():
    assert acris_viewer.looks_like_viewer(READY_ROWS)
    assert not acris_viewer.looks_like_viewer([ScreenText("ACRIS Search", (0, 0, 90, 20))])
    assert acris_viewer.looks_like_save_dialog(DIALOG_ROWS)
    assert not acris_viewer.looks_like_save_dialog(READY_ROWS)


def test_save_button_point_measured_from_zoom_anchor():
    # Центр дискеты (425, 104): правый край «73%» (291) + 134, строка тулбара.
    assert acris_viewer.save_button_point(READY_ROWS) == (425, 103)


def test_save_button_point_falls_back_to_of_anchor():
    rows = [ScreenText("of 8", (122, 95, 150, 113))]
    point = acris_viewer.save_button_point(rows)
    assert point == (122 + acris_viewer.SAVE_DX_FROM_OF_LEFT, 104)


def test_save_button_point_without_anchors():
    assert acris_viewer.save_button_point([ScreenText("hello", (0, 0, 40, 10))]) is None


def test_ok_button_needs_dialog_marks():
    assert acris_viewer.ok_button(DIALOG_ROWS).center == (256, 204)
    # Голое «OK» без диалога Save не нажимаем.
    assert acris_viewer.ok_button([ScreenText("OK", (242, 194, 270, 214))]) is None


class _Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.05, float(seconds))


def _window() -> browser_save.BrowserWindow:
    return browser_save.BrowserWindow(handle=7, title="Untitled Document", executable="chrome.exe")


def test_full_flow_save_then_ok_then_download(tmp_path):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    target = tmp_path / "out" / "OUR LOT 53 NEW.pdf"
    clock = _Clock()
    clicks: list[tuple[int, int]] = []
    opened: list[str] = []
    state = {"dialog": False}

    def capture(_handle):
        return DIALOG_ROWS if state["dialog"] else READY_ROWS

    def click(_handle, x, y):
        clicks.append((x, y))
        if len(clicks) == 1:
            state["dialog"] = True
        else:
            pdf = downloads / "2024101000085001.pdf"
            pdf.write_bytes(b"%PDF-1.4 fake")
        return True

    saved, rung = acris_viewer.download_pdf_via_viewer(
        "https://a836-acris.nyc.gov/DS/DocumentSearch/DocumentImageView?doc_id=2024101000085001",
        target,
        downloads_dir=downloads,
        open_browser=opened.append,
        windows_fn=lambda: [_window()],
        active_fn=lambda: 7,
        bounds_fn=lambda _h: (0, 0, 1600, 900),
        capture_rows_fn=capture,
        click_fn=click,
        focus_fn=lambda _h: True,
        sleep=clock.sleep, monotonic=clock.monotonic,
        viewer_wait_sec=5, dialog_wait_sec=5, download_wait_sec=5, poll_sec=0.1,
    )
    assert (saved, rung) == (target, "browser")
    assert target.read_bytes().startswith(b"%PDF-")
    assert opened and opened[0].endswith("doc_id=2024101000085001")
    # Первый клик — дискета по калибровке, второй — OK по OCR-центру.
    assert clicks[0] == (425, 103)
    assert clicks[1] == (256, 204)


def test_manual_step_when_dialog_never_appears(tmp_path):
    clock = _Clock()
    with pytest.raises(rcclerk.ManualStepNeeded) as caught:
        acris_viewer.download_pdf_via_viewer(
            "https://example.invalid/doc", tmp_path / "x.pdf",
            downloads_dir=tmp_path,
            open_browser=lambda _url: None,
            windows_fn=lambda: [_window()],
            active_fn=lambda: 7,
            bounds_fn=lambda _h: (0, 0, 1600, 900),
            capture_rows_fn=lambda _h: READY_ROWS,
            click_fn=lambda _h, _x, _y: True,
            focus_fn=lambda _h: True,
            sleep=clock.sleep, monotonic=clock.monotonic,
            viewer_wait_sec=1, dialog_wait_sec=1, download_wait_sec=1, poll_sec=0.1,
        )
    assert caught.value.url == "https://example.invalid/doc"
    assert caught.value.folder == tmp_path
    assert "дискету" in str(caught.value)


def test_manual_step_when_viewer_never_opens(tmp_path):
    clock = _Clock()
    with pytest.raises(rcclerk.ManualStepNeeded):
        acris_viewer.download_pdf_via_viewer(
            "https://example.invalid/doc", tmp_path / "x.pdf",
            downloads_dir=tmp_path,
            open_browser=lambda _url: None,
            windows_fn=lambda: [_window()],
            active_fn=lambda: 7,
            bounds_fn=lambda _h: (0, 0, 1600, 900),
            capture_rows_fn=lambda _h: [ScreenText("Google", (0, 0, 60, 20))],
            click_fn=lambda _h, _x, _y: True,
            focus_fn=lambda _h: True,
            sleep=clock.sleep, monotonic=clock.monotonic,
            viewer_wait_sec=1, dialog_wait_sec=1, download_wait_sec=1, poll_sec=0.1,
        )


def test_http_error_text_detects_405_not_document_numbers():
    assert "405" in acris_viewer.http_error_text(ERROR_ROWS)
    assert acris_viewer.http_error_text(READY_ROWS) is None
    # Голая цифра в тексте самого документа — не серверная ошибка.
    assert acris_viewer.http_error_text([ScreenText("405", (0, 0, 30, 10))]) is None
    # OCR может разрезать фразу на две строки — склейка тоже проверяется.
    split = [ScreenText("Method", (0, 0, 60, 10)), ScreenText("Not Allowed", (70, 0, 160, 10))]
    assert acris_viewer.http_error_text(split) is not None


def test_warmup_search_recovers_from_http_405(tmp_path):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    target = tmp_path / "out" / "OUR LOT 53 NEW.pdf"
    clock = _Clock()
    opened: list[str] = []
    clicks: list[tuple[int, int]] = []
    state = {"dialog": False}

    def capture(_handle):
        if state["dialog"]:
            return DIALOG_ROWS
        # Пока документ открывали только напрямую — читалка показывает 405;
        # после прогрева поиском (третье открытие) страница живая.
        return READY_ROWS if len(opened) >= 3 else ERROR_ROWS

    def click(_handle, x, y):
        clicks.append((x, y))
        if len(clicks) == 1:
            state["dialog"] = True
        else:
            (downloads / "2024101000085001.pdf").write_bytes(b"%PDF-1.4 fake")
        return True

    saved, rung = acris_viewer.download_pdf_via_viewer(
        DOC_URL, target,
        downloads_dir=downloads, warmup_url=WARM_URL,
        open_browser=opened.append,
        windows_fn=lambda: [_window()],
        active_fn=lambda: 7,
        bounds_fn=lambda _h: (0, 0, 1600, 900),
        capture_rows_fn=capture,
        click_fn=click,
        focus_fn=lambda _h: True,
        sleep=clock.sleep, monotonic=clock.monotonic,
        viewer_wait_sec=30, dialog_wait_sec=5, download_wait_sec=30, poll_sec=0.1,
    )
    assert (saved, rung) == (target, "browser")
    assert target.read_bytes().startswith(b"%PDF-")
    # Порядок открытий: документ (405) → прогрев поиском → документ ещё раз.
    assert opened == [DOC_URL, WARM_URL, DOC_URL]
    assert clicks[0] == (425, 103)
    assert clicks[1] == (256, 204)


def test_http_405_after_warmup_goes_manual_with_search_hint(tmp_path):
    clock = _Clock()
    opened: list[str] = []
    with pytest.raises(rcclerk.ManualStepNeeded) as caught:
        acris_viewer.download_pdf_via_viewer(
            DOC_URL, tmp_path / "x.pdf",
            downloads_dir=tmp_path, warmup_url=WARM_URL,
            open_browser=opened.append,
            windows_fn=lambda: [_window()],
            active_fn=lambda: 7,
            bounds_fn=lambda _h: (0, 0, 1600, 900),
            capture_rows_fn=lambda _h: ERROR_ROWS,
            click_fn=lambda _h, _x, _y: True,
            focus_fn=lambda _h: True,
            sleep=clock.sleep, monotonic=clock.monotonic,
            viewer_wait_sec=30, dialog_wait_sec=1, download_wait_sec=1, poll_sec=0.1,
        )
    # Ручной шаг объясняет: документ открывать из результатов поиска.
    assert "405" in str(caught.value)
    assert "поиск по Block/Lot" in str(caught.value)
    assert opened == [DOC_URL, WARM_URL, DOC_URL]


def test_http_405_during_pdf_wait_stops_early(tmp_path):
    clock = _Clock()
    state = {"dialog": False, "ok": False}
    clicks = {"n": 0}

    def capture(_handle):
        if state["ok"]:
            return ERROR_ROWS
        return DIALOG_ROWS if state["dialog"] else READY_ROWS

    def click(_handle, _x, _y):
        clicks["n"] += 1
        if clicks["n"] == 1:
            state["dialog"] = True
        else:
            state["ok"] = True
        return True

    with pytest.raises(rcclerk.ManualStepNeeded) as caught:
        acris_viewer.download_pdf_via_viewer(
            DOC_URL, tmp_path / "x.pdf",
            downloads_dir=tmp_path,
            open_browser=lambda _url: None,
            windows_fn=lambda: [_window()],
            active_fn=lambda: 7,
            bounds_fn=lambda _h: (0, 0, 1600, 900),
            capture_rows_fn=capture,
            click_fn=click,
            focus_fn=lambda _h: True,
            sleep=clock.sleep, monotonic=clock.monotonic,
            viewer_wait_sec=5, dialog_wait_sec=5, download_wait_sec=300, poll_sec=0.1,
        )
    assert "405" in str(caught.value)
    # Не висели полные 300 секунд ожидания PDF.
    assert clock.now < 60


def test_stop_raises_download_stopped(tmp_path):
    with pytest.raises(rcclerk.DownloadStopped):
        acris_viewer.download_pdf_via_viewer(
            "https://example.invalid/doc", tmp_path / "x.pdf",
            downloads_dir=tmp_path,
            open_browser=lambda _url: None,
            windows_fn=lambda: [_window()],
            active_fn=lambda: 7,
            bounds_fn=lambda _h: (0, 0, 1600, 900),
            capture_rows_fn=lambda _h: READY_ROWS,
            click_fn=lambda _h, _x, _y: True,
            focus_fn=lambda _h: True,
            should_stop=lambda: True,
        )
