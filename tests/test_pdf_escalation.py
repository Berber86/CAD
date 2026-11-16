"""
Шаг 1, набор PDF: ступень 2 для скана — реальное локальное распознавание.

Что проверяется (без окна и без платного ИИ):

* ступень 2 распознаёт выбранные пользователем страницы, а не «1–2» по умолчанию;
* ошибка OCR не глотается молча: она возвращается в отчёт с типом и местом;
* если страница целиком не читается, тот же диапазон идёт кусками 2×2;
* текст из кеша помечается как «из кеша», а не выдаётся за новое распознавание.

Движок подменяется (он зависит от машины). Настоящий прогон на скане выполняется,
только если установлен rapidocr-onnxruntime.
"""

from __future__ import annotations

import pytest

import ocr_pages
import pdf_set
import test_gui_smoke as smoke
from llm_parser import has_legal_courses

gui = smoke.gui  # фикстура окна с подменённым tkinter (для сквозного теста воркера)

LEGAL = (
    "ALL THAT CERTAIN plot, piece or parcel of land. BEGINNING AT A POINT on the easterly "
    "side of Alvine Avenue. RUNNING THENCE N 22°26'12\" E, 100.05 feet; THENCE S 67°33'48\" E, "
    "40.00 feet; THENCE S 22°26'12\" W, 100.05 feet; THENCE N 67°33'48\" W, 40.00 feet to the "
    "point or place of BEGINNING."
)


@pytest.fixture()
def fake_pdf(tmp_path):
    path = tmp_path / "LOT 68 NEW_p3-4.pdf"
    path.write_bytes(b"%PDF-1.4 fake")
    return path


@pytest.fixture()
def ocr_calls(monkeypatch, tmp_path):
    """Подменяет OCR: пишет вызовы; кеш — отдельная папка в tmp."""
    monkeypatch.setenv("TITLESURVEY_OCR_CACHE", str(tmp_path / "cache"))
    calls: list[dict] = []
    state = {"plain": LEGAL, "tiled": LEGAL, "raise": None, "cached_keys": set()}

    def fake_ocr(path, first, last, *, dpi=ocr_pages.DEFAULT_DPI, progress=None,
                 use_cache=True, tile=1):
        calls.append({"first": first, "last": last, "dpi": dpi, "tile": tile})
        if state["raise"] is not None:
            raise state["raise"]
        state["cached_keys"].add((first, last, dpi, tile))
        return state["tiled"] if tile > 1 else state["plain"]

    def fake_is_cached(path, first, last, *, dpi=ocr_pages.DEFAULT_DPI, tile=1):
        return (first, last, dpi, tile) in state["cached_keys"]

    monkeypatch.setattr(pdf_set.ocr_pages, "ocr_pdf_pages", fake_ocr)
    monkeypatch.setattr(pdf_set.ocr_pages, "is_cached", fake_is_cached)
    return calls, state


def test_escalation_uses_selected_pages_not_default_one_to_two(fake_pdf, ocr_calls):
    calls, _ = ocr_calls
    source = {"ocr_path": fake_pdf, "ocr_pages": "3-4", "ocr_dpi": 400}
    result = pdf_set.escalate_ocr(fake_pdf, source)
    assert result.error == ""
    assert calls[0]["first"] == 3 and calls[0]["last"] == 4
    assert result.pages == (3, 4)
    assert has_legal_courses(result.text)


def test_escalation_runs_at_400_dpi(fake_pdf, ocr_calls):
    calls, _ = ocr_calls
    result = pdf_set.escalate_ocr(fake_pdf, {"ocr_pages": "2"})
    assert calls[0]["dpi"] == 400
    assert result.dpi == 400


def test_ocr_error_is_reported_not_swallowed(fake_pdf, ocr_calls):
    _, state = ocr_calls
    state["raise"] = IndexError("string index out of range")
    result = pdf_set.escalate_ocr(fake_pdf, {"ocr_pages": "3-4"})
    assert result.text == ""
    assert "IndexError" in result.error
    assert "string index out of range" in result.error
    assert result.error.count("(") >= 1  # место ошибки указано
    assert "ошибка" in result.summary()


def test_tiled_pass_runs_when_whole_page_has_no_courses(fake_pdf, ocr_calls):
    calls, state = ocr_calls
    state["plain"] = "COVER PAGE\nno courses here"
    result = pdf_set.escalate_ocr(fake_pdf, {"ocr_pages": "3-4"})
    assert [c["tile"] for c in calls] == [1, 2]
    assert result.tiled is True
    assert has_legal_courses(result.text)
    assert "2×2" in result.summary()


def test_cached_text_is_labelled_as_cached(fake_pdf, ocr_calls):
    _, state = ocr_calls
    state["cached_keys"].add((3, 4, 400, 1))
    result = pdf_set.escalate_ocr(fake_pdf, {"ocr_pages": "3-4"})
    assert result.cached is True
    assert "из кеша" in result.summary()


def test_fresh_recognition_is_not_labelled_as_cached(fake_pdf, ocr_calls):
    result = pdf_set.escalate_ocr(fake_pdf, {"ocr_pages": "3-4"})
    assert result.cached is False
    assert "распознано заново" in result.summary()


def test_bad_page_spec_is_reported(fake_pdf, ocr_calls):
    result = pdf_set.escalate_ocr(fake_pdf, {"ocr_pages": "abc"})
    assert result.error
    assert result.text == ""


def test_describe_exc_keeps_type_message_and_place():
    try:
        "abc"[5]  # noqa: B018 - намеренная ошибка индекса
    except IndexError as exc:
        text = pdf_set.describe_exc(exc)
    assert text.startswith("IndexError: string index out of range")
    assert "test_pdf_escalation.py:" in text


def test_real_local_ocr_on_a_scan_takes_real_time(tmp_path, monkeypatch):
    """Настоящий OCR на скане: ступень 2 не «пролетает», а распознаёт страницу."""
    pytest.importorskip("rapidocr_onnxruntime")
    pymupdf = pytest.importorskip("pymupdf")
    monkeypatch.setenv("TITLESURVEY_OCR_CACHE", str(tmp_path / "cache"))
    image_pdf = tmp_path / "scan_p1.pdf"
    src = pymupdf.open()
    page = src.new_page()
    page.insert_textbox(pymupdf.Rect(36, 36, 560, 760), LEGAL, fontsize=11)
    png = tmp_path / "scan.png"
    page.get_pixmap(dpi=150).save(str(png))
    scan = pymupdf.open()
    sp = scan.new_page(width=page.rect.width, height=page.rect.height)
    sp.insert_image(sp.rect, filename=str(png))
    scan.save(str(image_pdf))
    scan.close()
    src.close()

    result = pdf_set.escalate_ocr(image_pdf, {"ocr_pages": "1"})
    assert result.error == ""
    assert result.cached is False
    assert result.seconds > 0.5
    assert has_legal_courses(result.text)


def test_background_worker_runs_stage_two_and_reports_it(gui, tmp_path, monkeypatch):
    """Фоновый разбор скана: ступень 2 выполняется, её время и ошибки видны в журнале."""
    import types

    from pdf_set import PdfSetJob

    pdf = tmp_path / "LOT 68 NEW_p3-4.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    progress: list[str] = []
    done: dict = {}

    def fake_ocr(path, first, last, *, dpi=ocr_pages.DEFAULT_DPI, progress=None,
                 use_cache=True, tile=1):
        if progress:
            progress(f"распознаю страницу {first} из {last} (OCR, локально, {dpi} dpi)…")
        return LEGAL

    monkeypatch.setattr(pdf_set.ocr_pages, "ocr_pdf_pages", fake_ocr)
    monkeypatch.setattr(pdf_set.ocr_pages, "is_cached", lambda *a, **k: False)

    def post(kind, payload):
        if kind == "progress":
            progress.append(payload)
        elif kind == "pdf_set_done":
            done["payload"] = payload

    stub = types.SimpleNamespace(
        _worker_poster=lambda: post,
        _worker_is_current=lambda: True,
        _find_step0_cache_path=gui.SurveyApp._find_step0_cache_path,
        _has_severe_shape_issues=gui.SurveyApp._has_severe_shape_issues,
        _shape_rank=gui.SurveyApp._shape_rank,
        _analyse_pdf_set_conflicts=lambda *a, **k: None,
    )
    stub._parse_pdf_set_job = types.MethodType(gui.SurveyApp._parse_pdf_set_job, stub)
    job = PdfSetJob(
        path=pdf, is_main=True, use_llm=False, pages_label="стр. 3–4", mode_label="локально",
        parse_key=("x",), source={"ocr_path": pdf, "ocr_pages": "3-4", "ocr_dpi": 400},
        ocr=True,
    )
    from llm_parser import load_config

    gui.SurveyApp._pdf_set_worker(
        stub, [job], load_config(), "", "", None, "", 1.0, False, {}, False,
    )
    results, _analysis = done["payload"]
    assert results[0].error == ""
    assert results[0].outcome is not None
    assert results[0].plan is not None
    assert any("OCR 400 dpi —" in line and "распознано заново" in line for line in progress), progress


def test_background_worker_reports_stage_two_error_instead_of_silent_skip(gui, tmp_path, monkeypatch):
    import types

    from pdf_set import PdfSetJob

    pdf = tmp_path / "LOT 72 NEW_p2.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    progress: list[str] = []
    done: dict = {}

    def broken_ocr(*args, **kwargs):
        raise IndexError("string index out of range")

    monkeypatch.setattr(pdf_set.ocr_pages, "ocr_pdf_pages", broken_ocr)

    def post(kind, payload):
        if kind == "progress":
            progress.append(payload)
        elif kind == "pdf_set_done":
            done["payload"] = payload

    stub = types.SimpleNamespace(
        _worker_poster=lambda: post,
        _worker_is_current=lambda: True,
        _find_step0_cache_path=gui.SurveyApp._find_step0_cache_path,
        _has_severe_shape_issues=gui.SurveyApp._has_severe_shape_issues,
        _shape_rank=gui.SurveyApp._shape_rank,
        _analyse_pdf_set_conflicts=lambda *a, **k: None,
    )
    stub._parse_pdf_set_job = types.MethodType(gui.SurveyApp._parse_pdf_set_job, stub)
    job = PdfSetJob(
        path=pdf, is_main=False, use_llm=False, pages_label="стр. 2", mode_label="локально",
        parse_key=("y",), source={"ocr_path": pdf, "ocr_pages": "2", "ocr_dpi": 400},
        ocr=True,
    )
    from llm_parser import load_config

    gui.SurveyApp._pdf_set_worker(
        stub, [job], load_config(), "", "", None, "", 1.0, False, {}, False,
    )
    results, _analysis = done["payload"]
    assert results[0].outcome is None
    assert "IndexError" in results[0].error
    assert "string index out of range" in results[0].error
    assert any("OCR 400 dpi —" in line and "ошибка" in line for line in progress), progress
