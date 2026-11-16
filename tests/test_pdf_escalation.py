"""
Шаг 1, набор PDF: эскалация для скана.

* Ступень 2 (локальный OCR) распознаёт выбранные страницы заново и НИКОГДА не читает
  кеш: эскалация нужна, чтобы не доверять прошлому результату.
* Ступень 3 (ИИ) получает страницы из строки «2» / «3-4», а не индексирует её.
* Ошибки OCR не глотаются: они попадают в отчёт с типом и местом.

Движок OCR подменяется, кроме одного теста, который запускает настоящий rapidocr
(он пропускается, если пакета нет).
"""

from __future__ import annotations

import types

import pytest

import ocr_pages
import pdf_set
import test_gui_smoke as smoke
from llm_parser import has_legal_courses, load_config
from pdf_set import PdfSetJob

gui = smoke.gui  # фикстура окна с подменённым tkinter (для сквозных тестов воркера)

LEGAL = (
    "ALL THAT CERTAIN plot, piece or parcel of land. BEGINNING AT A POINT on the easterly "
    "side of Alvine Avenue. RUNNING THENCE N 22°26'12\" E, 100.05 feet; THENCE S 67°33'48\" E, "
    "40.00 feet; THENCE S 22°26'12\" W, 100.05 feet; THENCE N 67°33'48\" W, 40.00 feet to the "
    "point or place of BEGINNING."
)
GARBAGE_CACHE = "COVER PAGE\nRECORDING AND ENDORSEMENT\nno courses here 1 2 3"
# Текст с расстоянием до угла (как у LOT 68) — «чистый» разбор.
CLEAN = (
    "ALL THAT CERTAIN plot, piece or parcel of land. BEGINNING at a point on the northwesterly "
    "side of Stafford Avenue, distant 161.00 feet southwesterly from the corner formed by the "
    "intersection of the northwesterly side of Stafford Avenue and the southwesterly side of "
    "Carlton Boulevard; RUNNING THENCE North 56 degrees 36 minutes 12 seconds West 100.00 feet "
    "to a point; THENCE South 33 degrees 23 minutes 48 seconds West 40.00 feet to a point; "
    "THENCE South 56 degrees 36 minutes 12 seconds East 100.00 feet to a point; THENCE North "
    "33 degrees 23 minutes 48 seconds East 40.00 feet to the point or place of BEGINNING."
)
# Те же стороны, но без расстояния до угла.
NO_TIE = CLEAN.replace("distant 161.00 feet southwesterly from the corner formed by the "
                       "intersection of the northwesterly side of Stafford Avenue and the "
                       "southwesterly side of Carlton Boulevard", "at a point")


@pytest.fixture()
def fake_pdf(tmp_path):
    path = tmp_path / "LOT 68 NEW_p3-4.pdf"
    path.write_bytes(b"%PDF-1.4 fake")
    return path


@pytest.fixture()
def ocr_calls(monkeypatch, tmp_path):
    """Подменяет OCR и записывает, с каким флагом кеша его вызвали."""
    monkeypatch.setenv("TITLESURVEY_OCR_CACHE", str(tmp_path / "cache"))
    calls: list[dict] = []
    state = {"plain": LEGAL, "tiled": LEGAL, "raise": None}

    def fake_ocr(path, first, last, *, dpi=ocr_pages.DEFAULT_DPI, progress=None,
                 use_cache=True, tile=1, exact_dpi=False):
        calls.append({"first": first, "last": last, "dpi": dpi, "tile": tile,
                      "use_cache": use_cache, "exact": exact_dpi})
        if state["raise"] is not None:
            raise state["raise"]
        return state["tiled"] if tile > 1 else state["plain"]

    monkeypatch.setattr(pdf_set.ocr_pages, "ocr_pdf_pages", fake_ocr)
    return calls, state


def test_escalation_never_reads_or_writes_cache(fake_pdf, ocr_calls):
    calls, state = ocr_calls
    state["plain"] = "COVER PAGE\nno courses"
    pdf_set.escalate_ocr(fake_pdf, {"ocr_pages": "3-4"})
    assert calls, "OCR должен был вызваться"
    assert all(call["use_cache"] is False for call in calls)


def test_escalation_uses_selected_pages(fake_pdf, ocr_calls):
    calls, _ = ocr_calls
    result = pdf_set.escalate_ocr(fake_pdf, {"ocr_pages": "3-4"})
    assert result.error == ""
    assert calls[0]["first"] == 3 and calls[0]["last"] == 4
    assert result.pages == (3, 4)
    assert has_legal_courses(result.text)


def test_escalation_single_page_spec(fake_pdf, ocr_calls):
    calls, _ = ocr_calls
    result = pdf_set.escalate_ocr(fake_pdf, {"ocr_pages": "2"})
    assert result.error == ""
    assert (calls[0]["first"], calls[0]["last"]) == (2, 2)


def test_ocr_error_is_reported_not_swallowed(fake_pdf, ocr_calls):
    _, state = ocr_calls
    state["raise"] = IndexError("string index out of range")
    result = pdf_set.escalate_ocr(fake_pdf, {"ocr_pages": "3-4"})
    assert result.text == ""
    assert "IndexError" in result.error
    assert "string index out of range" in result.error
    assert "(test_pdf_escalation.py:" in result.error
    assert "ошибка" in result.summary()


def test_tiled_pass_runs_when_whole_page_has_no_courses(fake_pdf, ocr_calls):
    calls, state = ocr_calls
    state["plain"] = "COVER PAGE\nno courses here"
    result = pdf_set.escalate_ocr(fake_pdf, {"ocr_pages": "3-4"}, dpis=(400,))
    assert [c["tile"] for c in calls] == [1, 2]
    assert all(c["use_cache"] is False for c in calls)
    assert result.tiled is True
    assert has_legal_courses(result.text)
    assert "2×2" in result.summary()


def test_escalation_renders_the_requested_dpi_not_the_native_cap(fake_pdf, ocr_calls):
    """Регресс: OUR 70 нативно 219 dpi; «400» не должно рендериться на 219."""
    calls, _ = ocr_calls
    result = pdf_set.escalate_ocr(fake_pdf, {"ocr_pages": "3-4"}, dpis=(400,))
    assert all(call["exact"] is True for call in calls)
    assert result.dpi == 400
    assert "рендер 400 dpi" in result.summary()


def test_escalation_default_tries_400_first_and_stops_on_clean_parse(fake_pdf, ocr_calls, monkeypatch):
    """Основной проход — 400 dpi (как требует задача); чистый разбор → остальные не запускаются."""
    calls, _ = ocr_calls
    monkeypatch.setattr(pdf_set.ocr_pages, "ocr_pdf_pages",
                        lambda *a, **k: (calls.append({"dpi": k.get("dpi"), "tile": k.get("tile", 1)}),
                                         CLEAN)[1])
    result = pdf_set.escalate_ocr(fake_pdf, {"ocr_pages": "3-4"})
    assert pdf_set.ESCALATION_DPIS[0] == 400
    assert [c["dpi"] for c in calls] == [400]
    assert result.dpi == 400
    assert result.tried == ("400 dpi — сторон 4",)


def test_escalation_falls_back_to_600_when_400_is_not_clean(fake_pdf, monkeypatch):
    """400 dpi без расстояния до угла → пробуем 600 dpi, он чистый → берём его."""
    calls: list[tuple[int, int]] = []
    texts = {400: NO_TIE, 600: CLEAN}

    def by_dpi(path, first, last, *, dpi=ocr_pages.DEFAULT_DPI, progress=None,
               use_cache=True, tile=1, exact_dpi=False):
        calls.append((dpi, tile))
        assert use_cache is False and exact_dpi is True
        return texts.get(dpi, "")

    monkeypatch.setattr(pdf_set.ocr_pages, "ocr_pdf_pages", by_dpi)
    result = pdf_set.escalate_ocr(fake_pdf, {"ocr_pages": "3-4"})
    assert [d for d, t in calls if t == 1] == [400, 600]
    assert result.dpi == 600
    assert result.text == CLEAN
    assert len(result.tried) == 2


def test_escalation_keeps_best_pass_when_none_is_clean(fake_pdf, monkeypatch):
    """Ни один проход не чистый → берём лучший: сначала полнота сторон, затем расстояние."""
    three_sides_with_tie = CLEAN.replace(
        " THENCE North 33 degrees 23 minutes 48 seconds East 40.00 feet to the point or place of BEGINNING.",
        " to the point or place of BEGINNING.",
    )
    texts = {400: NO_TIE, 600: three_sides_with_tie, 300: "COVER PAGE\nno courses here"}

    def by_dpi(path, first, last, *, dpi=ocr_pages.DEFAULT_DPI, progress=None,
               use_cache=True, tile=1, exact_dpi=False):
        return texts.get(dpi, "")

    monkeypatch.setattr(pdf_set.ocr_pages, "ocr_pdf_pages", by_dpi)
    result = pdf_set.escalate_ocr(fake_pdf, {"ocr_pages": "3-4"})
    # 400 dpi даёт 4 стороны (без расстояния), 600 — 3 стороны с расстоянием: полнота важнее.
    assert result.dpi == 400
    assert result.text == NO_TIE
    assert len(result.tried) == 3


def test_escalation_reports_error_when_every_pass_fails(fake_pdf, monkeypatch):
    def broken(*args, **kwargs):
        raise IndexError("string index out of range")

    monkeypatch.setattr(pdf_set.ocr_pages, "ocr_pdf_pages", broken)
    result = pdf_set.escalate_ocr(fake_pdf, {"ocr_pages": "3-4"})
    assert result.text == ""
    assert "IndexError" in result.error
    assert len(result.tried) == len(pdf_set.ESCALATION_DPIS)


def test_summary_says_fresh_recognition(fake_pdf, ocr_calls):
    result = pdf_set.escalate_ocr(fake_pdf, {"ocr_pages": "3-4"})
    assert "без кеша" in result.summary()


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


def _worker_stub(gui_module, post, *, severe: bool, parse_job):
    stub = types.SimpleNamespace(
        _worker_poster=lambda: post,
        _worker_is_current=lambda: True,
        _find_step0_cache_path=gui_module.SurveyApp._find_step0_cache_path,
        _has_severe_shape_issues=lambda plan, issues: severe,
        _shape_rank=gui_module.SurveyApp._shape_rank,
        _analyse_pdf_set_conflicts=lambda *a, **k: None,
        _parse_pdf_set_job=parse_job,
    )
    return stub


def test_stage_three_gets_pages_from_the_spec_not_by_indexing(gui, tmp_path, monkeypatch):
    """Регресс: «2» падал с IndexError (string index out of range) в ступени 3."""
    pdf = tmp_path / "LOT 72 NEW_p2.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(pdf_set.ocr_pages, "ocr_pdf_pages", lambda *a, **k: LEGAL)
    monkeypatch.setattr(gui, "inspect_pdf", lambda *a, **k: {"pages": 0})
    seen: list = []
    done: dict = {}

    def parse_job(job, *args, **kwargs):
        seen.append(job.source)
        if job.use_llm:
            raise gui.PDFError("ИИ-заглушка")
        return gui.SurveyApp._parse_pdf_set_job(stub, job, *args, **kwargs)

    def post(kind, payload):
        if kind == "pdf_set_done":
            done["payload"] = payload

    stub = _worker_stub(gui, post, severe=True, parse_job=parse_job)
    job = PdfSetJob(
        path=pdf, is_main=False, use_llm=False, pages_label="стр. 2", mode_label="локально",
        parse_key=("y",), source={"ocr_path": pdf, "ocr_pages": "2", "ocr_dpi": 400}, ocr=True,
    )
    gui.SurveyApp._pdf_set_worker(stub, [job], load_config(), "p", "m", "key", "", 1.0, False, {}, False)
    results, _ = done["payload"]
    llm_sources = [src for src in seen if src and "pdf_path" in src]
    assert llm_sources, "ступень 3 должна была вызваться"
    assert llm_sources[0]["pdf_pages"] == (2, 2)
    assert "IndexError" not in (results[0].error or "")


def test_background_worker_runs_stage_two_and_reports_it(gui, tmp_path, monkeypatch):
    pdf = tmp_path / "LOT 68 NEW_p3-4.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    progress: list[str] = []
    done: dict = {}
    monkeypatch.setattr(pdf_set.ocr_pages, "ocr_pdf_pages", lambda *a, **k: LEGAL)

    def post(kind, payload):
        if kind == "progress":
            progress.append(payload)
        elif kind == "pdf_set_done":
            done["payload"] = payload

    stub = _worker_stub(gui, post, severe=True, parse_job=None)
    stub._parse_pdf_set_job = types.MethodType(gui.SurveyApp._parse_pdf_set_job, stub)
    job = PdfSetJob(
        path=pdf, is_main=True, use_llm=False, pages_label="стр. 3–4", mode_label="локально",
        parse_key=("x",), source={"ocr_path": pdf, "ocr_pages": "3-4", "ocr_dpi": 400}, ocr=True,
    )
    # без ключа ступень 3 не запускается — проверяем только ступень 2
    gui.SurveyApp._pdf_set_worker(stub, [job], load_config(), "p", "m", None, "", 1.0, False, {}, False)
    results, _ = done["payload"]
    assert results[0].error == ""
    assert results[0].plan is not None
    assert any("OCR —" in line and "без кеша" in line for line in progress), progress


def test_background_worker_reports_stage_two_error(gui, tmp_path, monkeypatch):
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

    stub = _worker_stub(gui, post, severe=True, parse_job=None)
    stub._parse_pdf_set_job = types.MethodType(gui.SurveyApp._parse_pdf_set_job, stub)
    job = PdfSetJob(
        path=pdf, is_main=False, use_llm=False, pages_label="стр. 2", mode_label="локально",
        parse_key=("y",), source={"ocr_path": pdf, "ocr_pages": "2", "ocr_dpi": 400}, ocr=True,
    )
    gui.SurveyApp._pdf_set_worker(stub, [job], load_config(), "p", "m", None, "", 1.0, False, {}, False)
    results, _ = done["payload"]
    assert results[0].outcome is None
    assert "IndexError" in results[0].error
    assert any("OCR —" in line and "ошибка" in line for line in progress), progress


def test_real_ocr_ignores_a_poisoned_cache(tmp_path, monkeypatch):
    """Настоящий OCR: в кеш положен неверный текст, эскалация его не читает."""
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

    # Отравляем кеш ровно под тот ключ, который использовал бы обычный OCR.
    key = ocr_pages._cache_key(image_pdf, 1, 1, 400, 1)
    ocr_pages._write_cache(key, GARBAGE_CACHE)

    result = pdf_set.escalate_ocr(image_pdf, {"ocr_pages": "1"}, dpis=(400,))
    assert result.error == ""
    assert result.text != GARBAGE_CACHE
    assert has_legal_courses(result.text)
    assert result.seconds > 0.5, "настоящее распознавание занимает время, а не 0.0 с"


def test_text_quality_counts_courses_when_the_parser_cannot_read_the_text():
    """Регресс LOT 72: «on hundred feet» роняет разбор, но 4 курса в тексте всё равно видны."""
    broken = CLEAN.replace("100.00 feet", "hundred feet").replace("40.00 feet", "40 feet")
    sides, has_tie, _ = pdf_set.text_quality(broken)
    assert sides == 4
    assert has_tie == 1
    assert pdf_set.text_quality("COVER PAGE\nno courses here") == (0, 0, 0)
