"""
Шаг 1, набор PDF: локальный OCR для скана — один проход.

* Один проход OCR на максимуме, который даёт документ (SCAN_OCR_DPI, не выше нативного
  разрешения скана). Перебора 400/600/300 dpi и кусков 2×2 больше нет.
* Проход НИКОГДА не читает кеш: результат прошлого прохода не доверяем.
* Страницы — выбранные пользователем; строка «2» / «3-4» разбирается, а не индексируется.
* ИИ вызывается только если проверка описания не пройдена (статус не ok).
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
NO_TIE = CLEAN.replace(
    "distant 161.00 feet southwesterly from the corner formed by the "
    "intersection of the northwesterly side of Stafford Avenue and the "
    "southwesterly side of Carlton Boulevard",
    "at a point",
)


@pytest.fixture()
def fake_pdf(tmp_path):
    path = tmp_path / "LOT 68 NEW_p3-4.pdf"
    path.write_bytes(b"%PDF-1.4 fake")
    return path


@pytest.fixture()
def ocr_calls(monkeypatch, tmp_path):
    """Подменяет OCR и записывает каждый вызов (страницы, dpi, кеш, нарезку)."""
    monkeypatch.setenv("TITLESURVEY_OCR_CACHE", str(tmp_path / "cache"))
    calls: list[dict] = []
    state = {"text": LEGAL, "raise": None}

    def fake_ocr(path, first, last, *, dpi=ocr_pages.DEFAULT_DPI, progress=None,
                 use_cache=True, tile=1, exact_dpi=False):
        calls.append({"first": first, "last": last, "dpi": dpi, "tile": tile,
                      "use_cache": use_cache, "exact": exact_dpi})
        if state["raise"] is not None:
            raise state["raise"]
        return state["text"]

    monkeypatch.setattr(pdf_set.ocr_pages, "ocr_pdf_pages", fake_ocr)
    return calls, state


def test_single_pass_uses_scan_dpi_and_never_tiles(fake_pdf, ocr_calls):
    calls, _ = ocr_calls
    result = pdf_set.ocr_scan_once(fake_pdf, {"ocr_pages": "3-4"})
    assert len(calls) == 1, "проход один, без повтора"
    assert calls[0]["dpi"] == pdf_set.SCAN_OCR_DPI == ocr_pages.SCAN_MAX_DPI
    assert calls[0]["tile"] == 1
    assert calls[0]["exact"] is True, "рендер ровно на запрошенном dpi, не по нативному разрешению"
    assert result.dpi == pdf_set.SCAN_OCR_DPI
    assert result.text == LEGAL


def test_single_pass_does_not_retry_when_no_courses(fake_pdf, ocr_calls):
    """Раньше здесь был второй заход 2×2 и смена dpi. Теперь текст берётся как есть."""
    calls, state = ocr_calls
    state["text"] = "COVER PAGE\nno courses here"
    result = pdf_set.ocr_scan_once(fake_pdf, {"ocr_pages": "3-4"})
    assert len(calls) == 1 and calls[0]["tile"] == 1
    assert result.text == "COVER PAGE\nno courses here"


def test_single_pass_never_reads_or_writes_cache(fake_pdf, ocr_calls):
    calls, _ = ocr_calls
    pdf_set.ocr_scan_once(fake_pdf, {"ocr_pages": "3-4"})
    assert calls and all(call["use_cache"] is False for call in calls)


def test_single_pass_uses_selected_pages(fake_pdf, ocr_calls):
    calls, _ = ocr_calls
    result = pdf_set.ocr_scan_once(fake_pdf, {"ocr_pages": "3-4"})
    assert result.error == ""
    assert (calls[0]["first"], calls[0]["last"]) == (3, 4)
    assert result.pages == (3, 4)
    assert has_legal_courses(result.text)


def test_single_page_spec(fake_pdf, ocr_calls):
    calls, _ = ocr_calls
    result = pdf_set.ocr_scan_once(fake_pdf, {"ocr_pages": "2"})
    assert result.error == ""
    assert (calls[0]["first"], calls[0]["last"]) == (2, 2)


def test_ocr_error_is_reported_not_swallowed(fake_pdf, ocr_calls):
    _, state = ocr_calls
    state["raise"] = IndexError("string index out of range")
    result = pdf_set.ocr_scan_once(fake_pdf, {"ocr_pages": "3-4"})
    assert result.text == ""
    assert "IndexError" in result.error
    assert "string index out of range" in result.error
    assert "(test_pdf_escalation.py:" in result.error
    assert "ошибка" in result.summary()


def test_summary_says_fresh_recognition_at_one_dpi(fake_pdf, ocr_calls):
    result = pdf_set.ocr_scan_once(fake_pdf, {"ocr_pages": "3-4"})
    summary = result.summary()
    assert "без кеша" in summary
    assert f"рендер {pdf_set.SCAN_OCR_DPI} dpi" in summary
    assert "кусками" not in summary


def test_bad_page_spec_is_reported(fake_pdf, ocr_calls):
    result = pdf_set.ocr_scan_once(fake_pdf, {"ocr_pages": "abc"})
    assert result.error
    assert result.text == ""


def test_describe_exc_keeps_type_message_and_place():
    try:
        "abc"[5]  # noqa: B018 - намеренная ошибка индекса
    except IndexError as exc:
        text = pdf_set.describe_exc(exc)
    assert text.startswith("IndexError: string index out of range")
    assert "test_pdf_escalation.py:" in text


def test_real_ocr_ignores_a_poisoned_cache(tmp_path, monkeypatch):
    """Настоящий OCR: в кеш положен неверный текст, проход его не читает."""
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

    # Отравляем кеш под ключ, который использовал бы обычный OCR (в том числе на SCAN_OCR_DPI).
    for dpi in (400, pdf_set.SCAN_OCR_DPI):
        ocr_pages._write_cache(ocr_pages._cache_key(image_pdf, 1, 1, dpi, 1), GARBAGE_CACHE)

    result = pdf_set.ocr_scan_once(image_pdf, {"ocr_pages": "1"})
    assert result.error == ""
    # Кеш не прочитан: текст получен настоящим распознаванием, а не подставлен из кеша.
    assert result.text != GARBAGE_CACHE
    assert "BEGINNING" in result.text.upper()
    assert result.seconds > 0.5, "настоящее распознавание занимает время, а не 0.0 с"


def test_text_quality_counts_courses_when_the_parser_cannot_read_the_text():
    """Регресс LOT 72: «on hundred feet» роняет разбор, но 4 курса в тексте всё равно видны."""
    broken = CLEAN.replace("100.00 feet", "hundred feet").replace("40.00 feet", "40 feet")
    sides, has_tie, _ = pdf_set.text_quality(broken)
    assert sides == 4
    assert has_tie == 1
    assert pdf_set.text_quality("COVER PAGE\nno courses here") == (0, 0, 0)


def _worker_stub(gui_module, post, *, parse_job):
    stub = types.SimpleNamespace(
        _worker_poster=lambda: post,
        _worker_is_current=lambda: True,
        _find_step0_cache_path=gui_module.SurveyApp._find_step0_cache_path,
        _shape_rank=gui_module.SurveyApp._shape_rank,
        _analyse_pdf_set_conflicts=lambda *a, **k: None,
        _parse_pdf_set_job=parse_job,
    )
    stub._local_text_rank = lambda *a, **k: gui_module.SurveyApp._local_text_rank(stub, *a, **k)
    stub._write_ai_to_step0_cache = lambda *a, **k: gui_module.SurveyApp._write_ai_to_step0_cache(stub, *a, **k)
    return stub


def _run_worker(gui, stub, job, *, api_key=None):
    gui.SurveyApp._pdf_set_worker(stub, [job], load_config(), "p", "m", api_key, "", 1.0, False, {}, False)


def test_stage_three_gets_pages_from_the_spec_not_by_indexing(gui, tmp_path, monkeypatch):
    """Регресс: «2» падал с IndexError (string index out of range) при вызове ИИ."""
    pdf = tmp_path / "LOT 72 NEW_p2.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(pdf_set.ocr_pages, "ocr_pdf_pages", lambda *a, **k: NO_TIE)
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

    stub = _worker_stub(gui, post, parse_job=parse_job)
    job = PdfSetJob(
        path=pdf, is_main=False, use_llm=False, pages_label="стр. 2", mode_label="локально",
        parse_key=("y",), source={"ocr_path": pdf, "ocr_pages": "2", "ocr_dpi": 400}, ocr=True,
    )
    _run_worker(gui, stub, job, api_key="key")
    results, _ = done["payload"]
    llm_sources = [src for src in seen if src and "pdf_path" in src]
    assert llm_sources, "ИИ должен был вызваться: проверка не пройдена"
    assert llm_sources[0]["pdf_pages"] == (2, 2)
    assert "IndexError" not in (results[0].error or "")


def test_clean_ocr_text_skips_the_ai_even_with_a_key(gui, tmp_path, monkeypatch):
    """Чистый OCR (статус ok): ИИ не вызывается, даже если ключ задан."""
    pdf = tmp_path / "LOT 68 NEW_p3-4.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(pdf_set.ocr_pages, "ocr_pdf_pages", lambda *a, **k: CLEAN)
    used_llm: list[bool] = []
    done: dict = {}

    def parse_job(job, *args, **kwargs):
        used_llm.append(bool(job.use_llm))
        return gui.SurveyApp._parse_pdf_set_job(stub, job, *args, **kwargs)

    def post(kind, payload):
        if kind == "pdf_set_done":
            done["payload"] = payload

    stub = _worker_stub(gui, post, parse_job=parse_job)
    job = PdfSetJob(
        path=pdf, is_main=True, use_llm=False, pages_label="стр. 3–4", mode_label="локально",
        parse_key=("x",), source={"ocr_path": pdf, "ocr_pages": "3-4", "ocr_dpi": 400}, ocr=True,
    )
    _run_worker(gui, stub, job, api_key="key")
    results, _ = done["payload"]
    assert results[0].error == ""
    assert not any(used_llm), "ИИ не должен вызываться при статусе ok"
    assert results[0].llm_used is False


def test_no_key_leaves_a_visible_reason_in_the_log(gui, tmp_path, monkeypatch):
    """Проверка не пройдена, ключа нет: ИИ не вызван, и это написано в журнале и в замечаниях."""
    pdf = tmp_path / "LOT 68 NEW_p3-4.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(pdf_set.ocr_pages, "ocr_pdf_pages", lambda *a, **k: NO_TIE)
    progress: list[str] = []
    done: dict = {}

    def post(kind, payload):
        if kind == "progress":
            progress.append(payload)
        elif kind == "pdf_set_done":
            done["payload"] = payload

    stub = _worker_stub(gui, post, parse_job=None)
    stub._parse_pdf_set_job = types.MethodType(gui.SurveyApp._parse_pdf_set_job, stub)
    job = PdfSetJob(
        path=pdf, is_main=True, use_llm=False, pages_label="стр. 3–4", mode_label="локально",
        parse_key=("x",), source={"ocr_path": pdf, "ocr_pages": "3-4", "ocr_dpi": 400}, ocr=True,
    )
    _run_worker(gui, stub, job, api_key=None)
    results, _ = done["payload"]
    assert results[0].error == ""
    assert any("ИИ не вызван" in line for line in progress), progress
    assert any("ИИ не вызван" in issue for issue in results[0].issues), results[0].issues


def test_background_worker_runs_one_pass_and_reports_it(gui, tmp_path, monkeypatch):
    pdf = tmp_path / "LOT 68 NEW_p3-4.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    progress: list[str] = []
    done: dict = {}
    calls: list[int] = []

    def fake_ocr(path, first, last, *, dpi=ocr_pages.DEFAULT_DPI, progress=None,
                 use_cache=True, tile=1, exact_dpi=False):
        calls.append(tile)
        return LEGAL

    monkeypatch.setattr(pdf_set.ocr_pages, "ocr_pdf_pages", fake_ocr)

    def post(kind, payload):
        if kind == "progress":
            progress.append(payload)
        elif kind == "pdf_set_done":
            done["payload"] = payload

    stub = _worker_stub(gui, post, parse_job=None)
    stub._parse_pdf_set_job = types.MethodType(gui.SurveyApp._parse_pdf_set_job, stub)
    job = PdfSetJob(
        path=pdf, is_main=True, use_llm=False, pages_label="стр. 3–4", mode_label="локально",
        parse_key=("x",), source={"ocr_path": pdf, "ocr_pages": "3-4", "ocr_dpi": 400}, ocr=True,
    )
    _run_worker(gui, stub, job)
    results, _ = done["payload"]
    assert results[0].error == ""
    assert results[0].plan is not None
    assert calls == [1], "один проход, без кусков 2×2"
    assert any("OCR —" in line and "без кеша" in line for line in progress), progress


def test_background_worker_reports_ocr_error(gui, tmp_path, monkeypatch):
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

    stub = _worker_stub(gui, post, parse_job=None)
    stub._parse_pdf_set_job = types.MethodType(gui.SurveyApp._parse_pdf_set_job, stub)
    job = PdfSetJob(
        path=pdf, is_main=False, use_llm=False, pages_label="стр. 2", mode_label="локально",
        parse_key=("y",), source={"ocr_path": pdf, "ocr_pages": "2", "ocr_dpi": 400}, ocr=True,
    )
    _run_worker(gui, stub, job)
    results, _ = done["payload"]
    assert results[0].outcome is None
    assert "IndexError" in results[0].error
    assert any("OCR —" in line and "ошибка" in line for line in progress), progress


def test_cache_without_corner_distance_is_rechecked_locally_and_updated_when_better(gui, tmp_path, monkeypatch):
    """
    Кеш Шага 0 без расстояния до угла не принимается как есть: проход OCR перераспознаёт
    страницы, и если результат лучше — кеш обновляется на него.
    """
    pdf = tmp_path / "LOT 68 NEW_p3-4.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    cache = tmp_path / "LOT 68 NEW_text.txt"
    cache.write_text(NO_TIE, encoding="utf-8")
    progress: list[str] = []
    done: dict = {}
    ocr_calls: list[int] = []

    def by_dpi(path, first, last, *, dpi=ocr_pages.DEFAULT_DPI, progress=None,
               use_cache=True, tile=1, exact_dpi=False):
        assert use_cache is False
        ocr_calls.append(dpi)
        return CLEAN

    monkeypatch.setattr(pdf_set.ocr_pages, "ocr_pdf_pages", by_dpi)

    def post(kind, payload):
        if kind == "progress":
            progress.append(payload)
        elif kind == "pdf_set_done":
            done["payload"] = payload

    stub = _worker_stub(gui, post, parse_job=None)
    stub._parse_pdf_set_job = types.MethodType(gui.SurveyApp._parse_pdf_set_job, stub)
    job = PdfSetJob(
        path=pdf, is_main=True, use_llm=False, pages_label="стр. 3–4", mode_label="локально",
        parse_key=("x",), source={"ocr_path": pdf, "ocr_pages": "3-4", "ocr_dpi": 400}, ocr=True,
    )
    _run_worker(gui, stub, job)
    results, _ = done["payload"]
    assert ocr_calls == [pdf_set.SCAN_OCR_DPI], progress
    assert results[0].error == ""
    assert not any("расстояние от угла" in w for w in results[0].outcome.warnings)
    assert results[0].llm_used is False
    assert cache.read_text(encoding="utf-8") == CLEAN


def test_cache_is_kept_when_local_ocr_is_not_better(gui, tmp_path, monkeypatch):
    """OCR дал тот же текст без расстояния → лучше не стало, кеш не трогаем."""
    pdf = tmp_path / "LOT 68 NEW_p3-4.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    cache = tmp_path / "LOT 68 NEW_text.txt"
    cache.write_text(NO_TIE, encoding="utf-8")
    done: dict = {}
    monkeypatch.setattr(pdf_set.ocr_pages, "ocr_pdf_pages", lambda *a, **k: NO_TIE)

    def post(kind, payload):
        if kind == "pdf_set_done":
            done["payload"] = payload

    stub = _worker_stub(gui, post, parse_job=None)
    stub._parse_pdf_set_job = types.MethodType(gui.SurveyApp._parse_pdf_set_job, stub)
    job = PdfSetJob(
        path=pdf, is_main=True, use_llm=False, pages_label="стр. 3–4", mode_label="локально",
        parse_key=("x",), source={"ocr_path": pdf, "ocr_pages": "3-4", "ocr_dpi": 400}, ocr=True,
    )
    _run_worker(gui, stub, job)
    assert cache.read_text(encoding="utf-8") == NO_TIE


def test_equal_figure_with_more_distance_data_ranks_higher(gui):
    """При одинаковой фигуре больше данных о расстояниях (угол, длины сторон) — выше."""
    seg = types.SimpleNamespace(distance_ft=100.0, has_bearing=True)
    with_tie = types.SimpleNamespace(distance_to_corner_ft=161.0, segments=[seg] * 4)
    without_tie = types.SimpleNamespace(distance_to_corner_ft=0.0, segments=[seg] * 4)
    plan_a = types.SimpleNamespace(suspicion=None, warnings=[], closure=None, closure_error_ft=0.0,
                                   description=without_tie)
    plan_b = types.SimpleNamespace(suspicion=None, warnings=[], closure=None, closure_error_ft=0.0,
                                   description=with_tie)
    rank_a = gui.SurveyApp._shape_rank(plan_a, [], ())
    rank_b = gui.SurveyApp._shape_rank(plan_b, [], ())
    assert rank_b > rank_a


def _ai_write_stub(gui, post):
    stub = _worker_stub(gui, post, parse_job=None)
    stub._parse_pdf_set_job = types.MethodType(gui.SurveyApp._parse_pdf_set_job, stub)
    return stub


def test_ai_result_better_than_cache_replaces_it(gui, tmp_path):
    """Ответ ИИ с расстоянием до угла лучше кеша без него → кеш пишется текстом ИИ."""
    from acad_builder import AcadBuilder
    pdf = tmp_path / "LOT 68 NEW_p3-4.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    cache = tmp_path / "LOT 68 NEW_text.txt"
    cache.write_text(NO_TIE, encoding="utf-8")
    job = PdfSetJob(path=pdf, is_main=True, use_llm=True, pages_label="стр. 3–4", mode_label="ИИ",
                    parse_key=("x",), source={"pdf_path": pdf}, scan=True)
    cfg = load_config()
    builder = AcadBuilder(cfg)
    stub = _ai_write_stub(gui, lambda *a: None)
    prev = stub._local_text_rank(job, NO_TIE, builder, 16.0, cfg, "p", "m", None, "", False, lambda *a: None)
    ai_data, _ = smoke_parse(CLEAN)
    note = stub._write_ai_to_step0_cache(
        job, ai_data, prev_rank=prev, cache_path=cache, builder=builder, scale=16.0, cfg=cfg,
        provider_id="p", model_name="m", api_key=None, key_secret="", do_auto_orient=False,
        post=lambda *a: None,
    )
    assert "обновлён" in note, note
    assert "distant 161.00 feet" in cache.read_text(encoding="utf-8")


def test_worse_ai_result_leaves_cache_alone(gui, tmp_path):
    """Ответ ИИ не лучше кеша → кеш не меняется."""
    from acad_builder import AcadBuilder
    pdf = tmp_path / "LOT 68 NEW_p3-4.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    cache = tmp_path / "LOT 68 NEW_text.txt"
    cache.write_text(CLEAN, encoding="utf-8")
    job = PdfSetJob(path=pdf, is_main=True, use_llm=True, pages_label="стр. 3–4", mode_label="ИИ",
                    parse_key=("x",), source={"pdf_path": pdf}, scan=True)
    cfg = load_config()
    builder = AcadBuilder(cfg)
    stub = _ai_write_stub(gui, lambda *a: None)
    prev = stub._local_text_rank(job, CLEAN, builder, 16.0, cfg, "p", "m", None, "", False, lambda *a: None)
    ai_data, _ = smoke_parse(NO_TIE)
    note = stub._write_ai_to_step0_cache(
        job, ai_data, prev_rank=prev, cache_path=cache, builder=builder, scale=16.0, cfg=cfg,
        provider_id="p", model_name="m", api_key=None, key_secret="", do_auto_orient=False,
        post=lambda *a: None,
    )
    assert "не лучше" in note, note
    assert cache.read_text(encoding="utf-8") == CLEAN


def smoke_parse(text):
    import llm_parser as L
    return L.parse_with_regex(text)


def test_scan_parsed_by_ai_from_the_start_updates_weaker_cache(gui, tmp_path, monkeypatch):
    """Скан с режимом ИИ сразу: ответ ИИ лучше кеша без расстояния → кеш обновляется."""
    import llm_parser as L
    pdf = tmp_path / "LOT 68 NEW_p3-4.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    cache = tmp_path / "LOT 68 NEW_text.txt"
    cache.write_text(NO_TIE, encoding="utf-8")
    done: dict = {}

    def post(kind, payload):
        if kind == "pdf_set_done":
            done["payload"] = payload

    stub = _worker_stub(gui, post, parse_job=None)
    ai_model, _ = L.parse_with_regex(CLEAN)

    def parse_job(job, cfg, provider_id, model_name, api_key, key_secret, do_auto_orient, progress, post_):
        if job.use_llm:
            outcome = types.SimpleNamespace(data=ai_model, warnings=[], source="llm", prepared_text="",
                                            prepared_images=[], usage=None, raw_text="")
            return outcome, False, []
        return gui.SurveyApp._parse_pdf_set_job(stub, job, cfg, provider_id, model_name, api_key, key_secret,
                                                do_auto_orient, progress, post_)

    stub._parse_pdf_set_job = parse_job
    job = PdfSetJob(
        path=pdf, is_main=True, use_llm=True, pages_label="стр. 3–4", mode_label="ИИ",
        parse_key=("x",), source={"pdf_path": pdf, "pdf_pages": (3, 4)}, ocr=False, scan=True,
    )
    _run_worker(gui, stub, job, api_key="key")
    results, _ = done["payload"]
    assert results[0].error == "", results[0].error
    assert "distant 161.00 feet" in cache.read_text(encoding="utf-8")
