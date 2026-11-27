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


@pytest.fixture(autouse=True)
def _clear_fast_ok_cache():
    acris_viewer._OK_POINT_CACHE.clear()
    yield
    acris_viewer._OK_POINT_CACHE.clear()


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


def test_already_open_viewer_skips_url_and_downloads(tmp_path):
    # Резерв для свежих документов: читалку уже открыл клик IMG, открывать
    # URL нельзя — сразу дискета → OK → PDF из «Загрузок».
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    target = tmp_path / "out" / "LOT 62 NEW.pdf"
    clock = _Clock()
    clicks: list[tuple[int, int]] = []
    state = {"dialog": False}

    def capture(_handle):
        return DIALOG_ROWS if state["dialog"] else READY_ROWS

    def click(_handle, x, y):
        clicks.append((x, y))
        if len(clicks) == 1:
            state["dialog"] = True
        else:
            (downloads / "2026111700598001.pdf").write_bytes(b"%PDF-1.4 fresh")
        return True

    saved, rung = acris_viewer.download_pdf_via_viewer(
        "https://a836-acris.nyc.gov/DS/DocumentSearch/BBL",
        target,
        already_open=True,
        downloads_dir=downloads,
        open_browser=lambda _url: pytest.fail("URL при already_open открывать нельзя"),
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


def test_pdf_wait_default_is_45_seconds():
    # Как у Клерка Ричмонда: исход ясен раньше прежних 300 с (5.26/5.27).
    import inspect

    default = inspect.signature(
        acris_viewer.download_pdf_via_viewer).parameters["download_wait_sec"].default
    assert default == 45.0


def test_pdf_timeout_takes_emergency_shot_into_destination_folder(tmp_path):
    # Диалог Save был, OK нажат, а PDF так и не упал в «Загрузки»: в папку
    # назначения падает аварийный снимок ACRIS_FAIL, путь — в тексте шага.
    clock = _Clock()
    state = {"dialog": False}
    shots = []

    def capture(_handle):
        return DIALOG_ROWS if state["dialog"] else READY_ROWS

    def click(_handle, _x, _y):
        state["dialog"] = True
        return True

    def shot(handle, folder):
        shots.append((handle, folder))
        return f" Снимок экрана при сбое: {folder}/ACRIS_FAIL_20261125_084142.png"

    target = tmp_path / "out" / "OUR LOT 53 NEW.pdf"
    with pytest.raises(rcclerk.ManualStepNeeded) as caught:
        acris_viewer.download_pdf_via_viewer(
            "https://example.invalid/doc", target,
            downloads_dir=tmp_path,
            open_browser=lambda _url: None,
            windows_fn=lambda: [_window()],
            active_fn=lambda: 7,
            bounds_fn=lambda _h: (0, 0, 1600, 900),
            capture_rows_fn=capture,
            click_fn=click,
            focus_fn=lambda _h: True,
            sleep=clock.sleep, monotonic=clock.monotonic,
            viewer_wait_sec=5, dialog_wait_sec=5, download_wait_sec=2, poll_sec=0.1,
            shot_fn=shot,
        )
    assert shots == [(7, target.parent)]
    assert "не появился" in str(caught.value) and "ACRIS_FAIL" in str(caught.value)


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


class _SaveFlow:
    """Один проход читалки: дискета открывает диалог, OK создаёт PDF."""

    def __init__(self, downloads: Path, filename: str, *, serve_dialog: bool = True,
                 ok_point: tuple[int, int] = (256, 204)):
        self.downloads = downloads
        self.filename = filename
        self.serve_dialog = serve_dialog
        self.ok_point = ok_point
        self.clock = _Clock()
        self.dialog = False
        self.save_clicked = False
        self.captures_after_save = 0
        self.click_points: list[tuple[int, int]] = []
        self.click_times: list[float] = []

    def capture(self, _handle):
        if self.save_clicked:
            self.captures_after_save += 1
        if self.dialog and self.serve_dialog:
            if self.ok_point == (256, 204):
                return DIALOG_ROWS
            return READY_ROWS + [
                ScreenText("Save", (128, 130, 169, 150)),
                ScreenText("Cancel", (192, 194, 247, 214)),
                ScreenText("OK", (282, 194, 310, 214)),
            ]
        return READY_ROWS

    def click(self, _handle, x, y):
        self.click_points.append((x, y))
        self.click_times.append(self.clock.now)
        if not self.save_clicked:
            self.save_clicked = True
            self.dialog = True
        elif (x, y) == self.ok_point:
            (self.downloads / self.filename).write_bytes(b"%PDF-1.4 fake")
        return True

    def run(self, target: Path, **kwargs):
        return acris_viewer.download_pdf_via_viewer(
            DOC_URL, target,
            downloads_dir=self.downloads,
            open_browser=lambda _url: None,
            windows_fn=lambda: [_window()],
            active_fn=lambda: 7,
            bounds_fn=lambda _h: (0, 0, 1600, 900),
            capture_rows_fn=self.capture,
            click_fn=self.click,
            focus_fn=lambda _h: True,
            sleep=self.clock.sleep, monotonic=self.clock.monotonic,
            viewer_wait_sec=5, dialog_wait_sec=5, download_wait_sec=5, poll_sec=0.1,
            **kwargs,
        )


def test_second_document_clicks_ok_after_one_second_without_dialog_capture(tmp_path):
    first_downloads = tmp_path / "DownloadsFirst"
    second_downloads = tmp_path / "DownloadsSecond"
    first_downloads.mkdir()
    second_downloads.mkdir()

    first = _SaveFlow(first_downloads, "first.pdf")
    first.run(tmp_path / "first-target.pdf")
    assert first.click_points == [(425, 103), (256, 204)]

    # Второй документ: снимок после дискеты вообще не показывает диалог.
    # Успех возможен только если OK нажат по запомненной точке через 1 с.
    second = _SaveFlow(second_downloads, "second.pdf", serve_dialog=False)
    saved, rung = second.run(tmp_path / "second-target.pdf")

    assert (saved, rung) == (tmp_path / "second-target.pdf", "browser")
    assert second.click_points == [(425, 103), (256, 204)]
    assert second.click_times[1] - second.click_times[0] == pytest.approx(1.0)
    assert second.captures_after_save == 0


def test_fast_ok_miss_is_rescued_by_one_dialog_capture_and_recalibrated(tmp_path):
    first_downloads = tmp_path / "DownloadsFirst"
    shifted_downloads = tmp_path / "DownloadsShifted"
    first_downloads.mkdir()
    shifted_downloads.mkdir()

    first = _SaveFlow(first_downloads, "first.pdf")
    first.run(tmp_path / "first-target.pdf")

    # Диалог переехал вправо: старая точка OK промахивается, PDF не появляется.
    # После короткой паузы программа один раз смотрит снимок, нажимает настоящее
    # OK и запоминает новое смещение.
    shifted = _SaveFlow(shifted_downloads, "shifted.pdf", ok_point=(296, 204))
    saved, _rung = shifted.run(
        tmp_path / "shifted-target.pdf", fast_ok_grace_sec=0.2)

    assert saved.read_bytes().startswith(b"%PDF-")
    assert shifted.click_points == [(425, 103), (256, 204), (296, 204)]
    assert shifted.captures_after_save >= 1
    point = acris_viewer._cached_ok_point(
        _window(), (0, 0, 1600, 900), (425, 103))
    assert point == (296, 204)


def test_pick_window_and_url_open_use_preferred_browser(tmp_path, monkeypatch):
    chrome = browser_save.BrowserWindow(1, "ACRIS", "chrome.exe", r"C:\Chrome\chrome.exe")
    edge = browser_save.BrowserWindow(2, "Edge", "msedge.exe", r"C:\Edge\msedge.exe")
    prev = browser_save.get_preferred_browser()
    opened: list[str] = []
    try:
        browser_save.set_preferred_browser("edge")
        assert acris_viewer._pick_window(lambda: [chrome, edge], lambda: 1) == edge

        monkeypatch.setattr(browser_save, "open_url", lambda url: opened.append(url) or True)
        clock = _Clock()
        with pytest.raises(rcclerk.ManualStepNeeded):
            acris_viewer.download_pdf_via_viewer(
                "https://example.invalid/vtu", tmp_path / "out.pdf",
                downloads_dir=tmp_path,
                windows_fn=lambda: [edge],
                active_fn=lambda: 2,
                focus_fn=lambda _h: True,
                capture_rows_fn=lambda _h: [],
                sleep=clock.sleep, monotonic=clock.monotonic,
                viewer_wait_sec=0.2, poll_sec=0.1,
            )
        assert opened == ["https://example.invalid/vtu"]
    finally:
        browser_save.set_preferred_browser(prev)
        browser_save.reset_auto_browser_pin()

