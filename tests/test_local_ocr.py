"""
Шаг 1: скан без текстового слоя распознаётся локально (бесплатно), без ИИ.

Договорённость: пока приходит скан (документ ACRIS — это картинка), программа не
должна сразу звать платный Vision. Сначала бесплатный локальный OCR; ИИ — только
если распознать не удалось или геометрия показала, что цифры прочитаны неверно.

Движок в тестах подменяется: наличие `rapidocr-onnxruntime` зависит от машины, а
поведение программы — нет. Настоящий OCR проверяется одним отдельным тестом,
который пропускается, если пакета нет.
"""

from __future__ import annotations

import pytest

import ocr_pages
import pdf_set
from llm_parser import PDFError, has_legal_courses

DESCRIPTION = (
    "ALL THAT CERTAIN plot, piece or parcel of land, situate, lying and being in the "
    "Borough of Richmond, County of Richmond, City and State of New York, bounded and "
    "described as follows: BEGINNING AT A POINT on the easterly side of Alvine Avenue, "
    "distant 176.92 feet northerly from the corner formed by the intersection of the "
    "easterly side of Alvine Avenue with the northerly side of Stafford Avenue; "
    'RUNNING THENCE N 22°26\'12" E, 100.05 feet; THENCE S 67°33\'48" E, 40.00 feet; '
    'THENCE S 22°26\'12" W, 100.05 feet; '
    'THENCE N 67°33\'48" W, 40.00 feet to the point or place of BEGINNING.'
)


def _scan_pdf(path, text, *, fontsize=11, dpi=150):
    """PDF, в котором есть только картинка страницы (как документ из ACRIS)."""
    pymupdf = pytest.importorskip("pymupdf")
    source = pymupdf.open()
    page = source.new_page()
    page.insert_textbox(pymupdf.Rect(36, 36, 560, 760), text, fontsize=fontsize)
    pixmap = page.get_pixmap(dpi=dpi)
    image = path.with_suffix(".png")
    pixmap.save(str(image))
    scan = pymupdf.open()
    scan_page = scan.new_page(width=page.rect.width, height=page.rect.height)
    scan_page.insert_image(scan_page.rect, filename=str(image))
    scan.save(str(path))
    scan.close()
    source.close()
    check = pymupdf.open(str(path))
    try:
        assert not check[0].get_text().strip(), "в тестовом скане появился текстовый слой"
    finally:
        check.close()
    return path


class SizeAwareEngine:
    """Как настоящий OCR «не справляется» с плотной страницей, но читает куски."""

    def __init__(self, text, *, wide_from=1500):
        self.text = text
        self.wide_from = wide_from
        self.calls = 0
        self.widths = []

    def __call__(self, image_path):
        from PIL import Image

        self.calls += 1
        with Image.open(image_path) as image:
            width = image.width
        self.widths.append(width)
        source = "COVER PAGE\nno courses here" if width >= self.wide_from else self.text
        lines = [line for line in source.splitlines() if line.strip()]
        result = [
            ([(0.0, float(index * 20)), (100.0, float(index * 20)),
              (100.0, float(index * 20 + 12)), (0.0, float(index * 20 + 12))], line, 0.99)
            for index, line in enumerate(lines)
        ]
        return result, 0.0


def _text_pdf(path, text):
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(36, 36, 560, 760), text, fontsize=9)
    doc.save(str(path))
    doc.close()
    return path


class FakeEngine:
    """Подменяет RapidOCR: возвращает заданные строки, считает вызовы."""

    def __init__(self, text):
        self.text = text
        self.calls = 0

    def __call__(self, _image_path):
        self.calls += 1
        lines = [line for line in self.text.splitlines() if line.strip()]
        result = [
            ([(0.0, float(index * 20)), (100.0, float(index * 20)),
              (100.0, float(index * 20 + 12)), (0.0, float(index * 20 + 12))], line, 0.99)
            for index, line in enumerate(lines)
        ]
        return result, 0.0


@pytest.fixture()
def ocr_ready(monkeypatch, tmp_path):
    """Локальный OCR «установлен»: движок подменён, кэш — в папке теста."""
    monkeypatch.setenv("TITLESURVEY_OCR_CACHE", str(tmp_path / "ocr_cache"))
    monkeypatch.setattr(ocr_pages, "_available", True)
    monkeypatch.setattr(ocr_pages, "_engine_error", "")
    engine = FakeEngine(DESCRIPTION)
    monkeypatch.setattr(ocr_pages, "_engine", engine)
    return engine


# --------------------------------------------------------------- без окна


def test_scan_becomes_a_free_source_when_ocr_is_available(tmp_path, ocr_ready):
    from pdf_set import make_item

    scan = _scan_pdf(tmp_path / "deed.pdf", DESCRIPTION)
    item = make_item(scan, ocr_available=True)
    # Способ разбора сам стал локальным: скан больше не уходит в платный Vision.
    assert item.use_llm is False and item.ocr_ok is True and item.uses_ocr() is True
    assert item.mode_label() == "локальный OCR, бесплатно"
    assert item.parse_source() == {
        "ocr_path": scan, "ocr_pages": "1", "ocr_dpi": pdf_set.SCAN_OCR_DPI,
    }


def test_scan_without_local_ocr_still_goes_to_the_ai(tmp_path, monkeypatch):
    from pdf_set import make_item

    monkeypatch.setattr(ocr_pages, "_available", False)
    scan = _scan_pdf(tmp_path / "deed.pdf", DESCRIPTION)
    item = make_item(scan)  # по умолчанию OCR не обещан — как до этой версии
    assert item.use_llm is True and item.ocr_ok is False and item.uses_ocr() is False
    assert item.mode_label() == "ИИ / Vision по изображению страниц"
    assert item.parse_source() == {"pdf_path": scan, "pdf_pages": "1"}


def test_text_pdf_is_not_sent_to_ocr(tmp_path, ocr_ready):
    from pdf_set import make_item

    pdf = _text_pdf(tmp_path / "deed.pdf", DESCRIPTION)
    item = make_item(pdf, ocr_available=True)
    assert item.use_llm is False and item.uses_ocr() is False
    assert item.mode_label() == "локально, бесплатно"
    assert set(item.parse_source()) == {"text"}


def test_resolve_ocr_source_turns_pages_into_parser_text(tmp_path, ocr_ready):
    from pdf_set import resolve_ocr_source

    scan = _scan_pdf(tmp_path / "deed_p2.pdf", DESCRIPTION)
    source = {"ocr_path": scan, "ocr_pages": "1", "ocr_dpi": 300}
    resolved = resolve_ocr_source(source)
    assert resolved["ocr"] is True
    assert has_legal_courses(resolved["text"])
    assert "176.92" in resolved["text"] and "22°26" in resolved["text"]
    # Обычный источник не трогается: OCR включается только по отметке.
    assert resolve_ocr_source({"text": DESCRIPTION}) == {"text": DESCRIPTION}


def test_resolve_ocr_source_says_plainly_what_to_do(tmp_path, monkeypatch):
    from pdf_set import resolve_ocr_source

    monkeypatch.setenv("TITLESURVEY_OCR_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(ocr_pages, "_available", True)
    monkeypatch.setattr(ocr_pages, "_engine", FakeEngine("COVER PAGE\nno courses here"))
    scan = _scan_pdf(tmp_path / "cover.pdf", "COVER PAGE")
    with pytest.raises(PDFError, match="не нашёл обхода границ"):
        resolve_ocr_source({"ocr_path": scan, "ocr_pages": "1"})
    with pytest.raises(PDFError, match="Переключите файл на ИИ"):
        resolve_ocr_source({"ocr_path": scan, "ocr_pages": "1"})

    # Движка нет вовсе — тоже понятный текст, а не трассировка (и не из кэша).
    monkeypatch.setattr(ocr_pages, "_available", False)
    monkeypatch.setattr(ocr_pages, "_engine", None)
    other = _scan_pdf(tmp_path / "cover2.pdf", "COVER PAGE")
    with pytest.raises(PDFError, match="локальный OCR недоступен"):
        resolve_ocr_source({"ocr_path": other, "ocr_pages": "1"})


def test_recognised_pages_are_cached_and_not_read_twice(tmp_path, ocr_ready):
    from pdf_set import resolve_ocr_source

    scan = _scan_pdf(tmp_path / "deed.pdf", DESCRIPTION)
    source = {"ocr_path": scan, "ocr_pages": "1"}
    first = resolve_ocr_source(source)["text"]
    second = resolve_ocr_source(source)["text"]
    assert first == second
    # Второй раз страница не рендерилась и не распознавалась: текст из кэша.
    assert ocr_ready.calls == 1
    assert (tmp_path / "ocr_cache").is_dir()


def test_render_dpi_honours_low_request_and_keeps_200_floor_for_normal_scan_ocr(tmp_path, ocr_ready):
    """Явный low-DPI запрос соблюдается; обычный OCR сохраняет floor 200 для мелкого скана."""
    pymupdf = pytest.importorskip("pymupdf")

    assert ocr_pages.DEFAULT_DPI == 400
    low = _scan_pdf(tmp_path / "low.pdf", DESCRIPTION, dpi=150)  # ≈150 dpi картинки
    high = _scan_pdf(tmp_path / "high.pdf", DESCRIPTION, dpi=350)  # ≈350 dpi картинки
    doc = pymupdf.open(str(low))
    try:
        assert ocr_pages.page_render_dpi(doc[0], 400) == 200
        assert ocr_pages.page_render_dpi(doc[0], 600) == 200
        assert ocr_pages.page_render_dpi(doc[0], 120) == 120
        assert ocr_pages.page_render_dpi(doc[0], 80) == 80
    finally:
        doc.close()
    doc = pymupdf.open(str(high))
    try:
        # Просили 400 и 600 — рендерим 350: больше в файле нет.
        assert ocr_pages.page_render_dpi(doc[0], 400) == 350
        assert ocr_pages.page_render_dpi(doc[0], 600) == 350
        assert ocr_pages.page_render_dpi(doc[0], 250) == 250  # меньше — можно
    finally:
        doc.close()
    # Векторная страница: картинок нет, берём запрошенное разрешение.
    text_pdf = _text_pdf(tmp_path / "vector.pdf", DESCRIPTION)
    doc = pymupdf.open(str(text_pdf))
    try:
        assert ocr_pages.native_dpi(doc[0]) == 0.0
        assert ocr_pages.page_render_dpi(doc[0], 400) == 400
        assert ocr_pages.page_render_dpi(doc[0], 80) == 80
        assert ocr_pages.page_render_dpi(doc[0], 10) == ocr_pages.MIN_DPI
        assert ocr_pages.page_render_dpi(doc[0], 5000) == ocr_pages.MAX_DPI
    finally:
        doc.close()


def test_unreadable_page_is_read_once_without_tiles(tmp_path, monkeypatch):
    """Один проход на максимальном dpi: плотная страница не перечитывается кусками 2×2."""
    from pdf_set import resolve_ocr_source

    monkeypatch.setenv("TITLESURVEY_OCR_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(ocr_pages, "_available", True)
    engine = SizeAwareEngine(DESCRIPTION, wide_from=1500)
    monkeypatch.setattr(ocr_pages, "_engine", engine)
    scan = _scan_pdf(tmp_path / "dense.pdf", DESCRIPTION, dpi=200)  # 1700 px по ширине

    with pytest.raises(PDFError):
        resolve_ocr_source({"ocr_path": scan, "ocr_pages": "1"})
    assert engine.calls == 1, "второго захода кусками 2×2 быть не должно"
    assert min(engine.widths) >= 1500


def test_ocr_sorts_lines_top_down():
    shuffled = [
        ([(0.0, 300.0), (100.0, 300.0), (100.0, 312.0), (0.0, 312.0)], "THENCE S 67", 0.9),
        ([(0.0, 100.0), (100.0, 100.0), (100.0, 112.0), (0.0, 112.0)], "BEGINNING AT A POINT", 0.9),
        ([(0.0, 200.0), (100.0, 200.0), (100.0, 212.0), (0.0, 212.0)], 'N 22°26\'12" E', 0.9),
        (None, "", 0.0),  # пустая строка не ломает разбор
    ]
    text = ocr_pages._lines_to_text(shuffled)
    assert text.splitlines() == [
        "BEGINNING AT A POINT", 'N 22°26\'12" E', "THENCE S 67",
    ]


def test_cache_key_notices_a_changed_file(tmp_path, ocr_ready, monkeypatch):
    from pdf_set import resolve_ocr_source

    scan = _scan_pdf(tmp_path / "deed.pdf", DESCRIPTION)
    source = {"ocr_path": scan, "ocr_pages": "1"}
    resolve_ocr_source(source)
    assert ocr_ready.calls == 1
    # Файл перезаписан — кэш не должен отдавать старое распознавание.
    _scan_pdf(tmp_path / "deed.pdf", DESCRIPTION + " SUBJECT to covenants.")
    resolve_ocr_source(source)
    assert ocr_ready.calls == 2


# ------------------------------------------------------------------- окно

gui = __import__("test_gui_smoke", fromlist=["gui"]).gui


def test_scan_in_the_set_is_read_locally_and_built(gui, tmp_path, monkeypatch):
    """Скан в наборе распознаётся локально и строится без платного запроса."""
    import ocr_pages as local_ocr
    from llm_parser import ASPENWALL_SAMPLES

    samples = ASPENWALL_SAMPLES
    main_text, neighbor_text = samples[
        "Aspenwall St — 420' (Основной участок)"
    ], samples["Aspenwall St — 475' (Сосед справа)"]
    main_pdf = _text_pdf(tmp_path / "main.pdf", main_text)
    scan = _scan_pdf(tmp_path / "scan.pdf", neighbor_text)

    monkeypatch.setenv("TITLESURVEY_OCR_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(local_ocr, "_available", True)
    monkeypatch.setattr(local_ocr, "_engine", FakeEngine(neighbor_text))
    monkeypatch.setattr(gui.ocr_pages, "available", lambda: True)

    calls = []
    real = gui.parse_legal_description_full
    monkeypatch.setattr(
        gui, "parse_legal_description_full",
        lambda **kwargs: calls.append(kwargs) or real(**kwargs),
    )
    app = gui.SurveyApp()
    app.log_mode_var.set(True)  # этот тест проверяет подробный текст отчёта
    from tkinter import filedialog

    monkeypatch.setattr(
        filedialog, "askopenfilenames", lambda **_: (str(main_pdf), str(scan)),
    )
    app._load_pdf()
    dialog = app._pdf_set_dialog
    assert dialog is not None, app.status_var.get()
    # В списке у скана честно написано, что будет локальное распознавание.
    assert "распознаются локально (OCR)" in dialog.rows[1]["note"].cget("text")
    assert dialog.rows[1]["mode_var"].get() == dialog.MODE_LOCAL
    dialog.rows[0]["radio"].select()
    dialog.submit()
    __import__("test_gui_smoke", fromlist=["_wait_for_gui_worker"])._wait_for_gui_worker(app)

    item = app.pdf_set[1]
    assert item.ocr_used is True and item.on_scene is True and not item.error
    assert item.mode_label() == "локальный OCR, распознано (бесплатно)"
    # Платного запроса по скану не было: в парсер ушёл текст, а не изображения.
    assert len(calls) == 2
    assert all("pdf_path" not in kwargs for kwargs in calls)
    report = app._pdf_set_report_window._report_text
    assert "Сканов распознано локально (OCR, бесплатно): 1" in report
    assert "текст страниц получен локальным OCR — платный запрос ИИ не делался" in report
    assert "локально, OCR" in app.neighbor_pdf_summary.cget("text")


def _plan_stub(distance, bearings):
    from types import SimpleNamespace

    return SimpleNamespace(
        closure=SimpleNamespace(distance=distance),
        description=SimpleNamespace(
            segments=[SimpleNamespace(has_bearing=flag) for flag in bearings]
        ),
        notes=[],
    )


def test_ocr_warning_does_not_blame_the_scanner_for_a_side_without_bearing(gui):
    """
    «24 Alvine Avenue»: сторона #4 записана словами («southerly along the easterly
    line of Alvine Avenue»), поэтому контур и не замыкается — невязка 15.57 ft.
    Это не ошибка распознавания: цифры прочитаны верно, румб закрывает правило 1.
    """
    where, note = gui.ocr_closure_note(_plan_stub(15.5696, [True, True, True, False]))
    assert where == "note"
    assert "#4" in note and "так написано в документе" in note
    assert "платный ИИ не нужен" in note
    assert "вероятно, ошибка чтения цифр" not in note

    # Все стороны с румбами, а контур не сходится — вот теперь повод проверить OCR.
    where, note = gui.ocr_closure_note(_plan_stub(3.41, [True, True, True, True]))
    assert where == "issue"
    assert "переключите файл на ИИ" in note and "вероятно, ошибка чтения цифр" in note

    # Всё сошлось — молчим.
    assert gui.ocr_closure_note(_plan_stub(0.006, [True, True, True, False])) == ("", "")


def test_scan_with_a_bearingless_side_is_explained_not_blamed(gui, tmp_path, monkeypatch):
    """Сквозная проверка: скан «нашего» участка с такой стороной — пояснение, не замечание."""
    import ocr_pages as local_ocr
    from test_conflict_resolver import OUR_TEXT

    monkeypatch.setenv("TITLESURVEY_OCR_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(local_ocr, "_available", True)
    monkeypatch.setattr(local_ocr, "_engine", FakeEngine(OUR_TEXT))
    monkeypatch.setattr(gui.ocr_pages, "available", lambda: True)
    scan = _scan_pdf(tmp_path / "alvine.pdf", OUR_TEXT, fontsize=9)

    app = gui.SurveyApp()
    from tkinter import filedialog

    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **_: (str(scan),))
    app._load_pdf()
    dialog = app._pdf_set_dialog
    assert dialog is not None, app.status_var.get()
    dialog.rows[0]["radio"].select()
    dialog.submit()
    __import__("test_gui_smoke", fromlist=["_wait_for_gui_worker"])._wait_for_gui_worker(app, timeout=20)

    item = app.pdf_set[0]
    assert item.ocr_used is True and item.on_scene is True
    plan = app._pdf_set_main_plan
    notes = " ".join(plan.notes)
    assert "от стороны без румба" in notes and "платный ИИ не нужен" in notes
    assert not any("вероятно, ошибка чтения цифр" in issue for issue in item.issues)


@pytest.mark.skipif(not ocr_pages.available(), reason="локальный OCR не установлен")
def test_real_engine_reads_bearings_and_distances(tmp_path, monkeypatch):
    """Настоящий движок: румбы и длины читаются (фразы и регистр нам не важны)."""
    monkeypatch.setenv("TITLESURVEY_OCR_CACHE", str(tmp_path / "cache"))
    scan = _scan_pdf(tmp_path / "deed.pdf", DESCRIPTION, fontsize=13)
    text = ocr_pages.ocr_pdf_pages(scan, 1, 1)
    compact = "".join(text.split())
    for probe in ("176.92", "100.05", "40.00", "22°26"):
        assert probe in compact, (probe, text)


def test_street_name_split_by_a_dot_or_dash_is_joined():
    """Регресс OUR 70: «Carlton. Boulevard» и «Carlton-Boulevard» из OCR — одно название улицы."""
    assert "Carlton Boulevard" in ocr_pages.restore_word_breaks("southwesterly side of Carlton. Boulevard; running")
    assert "Carlton Boulevard" in ocr_pages.restore_word_breaks("Carlton-Boulevard; running")
    # Обычная фраза со строчным словом не склеивается.
    assert "the end. Street was" in ocr_pages.restore_word_breaks("the end. Street was")
