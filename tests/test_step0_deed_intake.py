"""
Шаг 0: документы участка у клерка Richmond County (Staten Island).

Проверяется по схеме, которую Коля объяснил 15.11.2026:

* страница списка открывается по block/lot;
* берутся строки **Deed**, а если их нет — **Mortgage**;
* для нашего лота качаются самый свежий и самый старый документ, для соседей —
  только свежий;
* имя файла: ``OUR LOT 32 NEW.pdf`` / ``OUR LOT 32 OLD.pdf`` (сосед — ``LOT 30 NEW.pdf``);
* скачивание честно падает, если сайт показал проверку Cloudflare, и объясняет,
  что делать руками.

Сеть не нужна: HTML и ответы подставляются.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

import deed_store
import rcclerk

BLOCK, LOT = 6323, 32
NEWEST_ID = 2640218
OLDEST_ID = 70112


def _table_html(rows: list[tuple[str, str, str]]) -> str:
    """HTML списка документов: (номер, тип, дата) — в виде таблицы, как на сайте."""
    body = "".join(
        f"<tr><td>{doc_type}</td><td>{when}</td>"
        f'<td><a href="/Search/ViewDocumentInfo/{number}">View</a></td></tr>'
        for number, doc_type, when in rows
    )
    return f"<html><body><table><tbody>{body}</tbody></table></body></html>"


# ------------------------------------------------------------------ адреса


def test_urls_match_the_ones_nikolai_uses():
    """Ровно те адреса, что в его видео: список, карточка, PDF."""
    assert rcclerk.results_url(BLOCK, LOT) == (
        "https://www.richmondcountyclerk.com/Search/ShowResultsBlocks/0"
        "?Block=6323&Lot=32&SelectedDocumentIdentifier=0"
    )
    assert rcclerk.document_info_url(NEWEST_ID) == (
        "https://www.richmondcountyclerk.com/Search/ViewDocumentInfo/2640218"
    )
    assert rcclerk.document_pdf_url(NEWEST_ID) == (
        "https://www.richmondcountyclerk.com/ViewVscmsDocument/ViewContent"
        "?p_endorsementId=2640218"
    )


# ------------------------------------------------------------- разбор строк


def test_rows_are_read_with_type_and_date():
    html = _table_html([
        (str(OLDEST_ID), "Deed", "06/14/1974"),
        (str(NEWEST_ID), "Deed", "05/14/2019"),
        ("900001", "Mortgage", "05/14/2019"),
    ])
    rows = rcclerk.parse_document_rows(html)
    assert [row.endorsement_id for row in rows] == [str(OLDEST_ID), str(NEWEST_ID), "900001"]
    assert [row.doc_type for row in rows] == ["DEED", "DEED", "MORTGAGE"]
    assert rows[0].doc_date == rcclerk.date(1974, 6, 14)
    assert rows[1].doc_date == rcclerk.date(2019, 5, 14)
    assert rows[0].is_deed and rows[2].is_mortgage


def test_rows_are_found_even_without_a_table():
    """Сайт может отдать не таблицу — номера и типы всё равно должны найтись."""
    html = (
        '<div class="row"><span>WAR DEED</span><span>03/02/1943</span>'
        '<a href="/Search/ViewDocumentInfo/1234">View</a></div>'
    )
    rows = rcclerk.parse_document_rows(html)
    assert [(row.endorsement_id, row.doc_type) for row in rows] == [("1234", "WAR DEED")]


def test_repeated_links_are_not_duplicated():
    html = (
        f'<tr><td>Deed</td><td>05/14/2019</td>'
        f'<a href="/Search/ViewDocumentInfo/{NEWEST_ID}">View</a>'
        f'<a href="/Search/ViewDocumentInfo/{NEWEST_ID}">PDF</a></tr>'
    )
    assert [row.endorsement_id for row in rcclerk.parse_document_rows(html)] == [str(NEWEST_ID)]


# ------------------------------------------------------- что именно качать


def _rows(rows):
    return rcclerk.parse_document_rows(_table_html(rows))


def test_our_lot_takes_the_newest_and_the_oldest_deed():
    rows = _rows([
        ("500", "Deed", "01/01/1961"),
        (str(OLDEST_ID), "Deed", "06/14/1974"),
        (str(NEWEST_ID), "Deed", "05/14/2019"),
        ("900001", "Mortgage", "05/14/2019"),
    ])
    chosen = rcclerk.choose_documents(rows, role="our")
    assert [(row.endorsement_id, tag) for row, tag in chosen] == [
        (str(NEWEST_ID), "NEW"), ("500", "OLD"),
    ]


def test_a_single_deed_is_only_new():
    rows = _rows([(str(NEWEST_ID), "Deed", "05/14/2019")])
    assert [(row.endorsement_id, tag) for row, tag in rcclerk.choose_documents(rows)] == [
        (str(NEWEST_ID), "NEW"),
    ]


def test_without_deeds_a_mortgage_is_taken():
    rows = _rows([
        ("900001", "Mortgage", "01/01/2010"),
        ("900002", "Mortgage", "05/14/2019"),
    ])
    chosen = rcclerk.choose_documents(rows, role="our")
    assert [(row.endorsement_id, tag) for row, tag in chosen] == [("900002", "NEW"), ("900001", "OLD")]
    # У соседа без Deed берём только свежую ипотеку.
    assert [(row.endorsement_id, tag) for row, tag in rcclerk.choose_documents(rows, role="neighbour")] == [
        ("900002", "NEW"),
    ]


def test_a_neighbour_gets_only_the_newest_unless_asked():
    rows = _rows([
        (str(OLDEST_ID), "Deed", "06/14/1974"),
        (str(NEWEST_ID), "Deed", "05/14/2019"),
    ])
    assert [tag for _row, tag in rcclerk.choose_documents(rows, role="neighbour")] == ["NEW"]
    assert [tag for _row, tag in rcclerk.choose_documents(
        rows, role="neighbour", with_old_for_neighbour=True
    )] == ["NEW", "OLD"]


def test_rows_without_dates_do_not_break_the_choice():
    html = (
        f'<tr><td>Deed</td><td>дата не напечатана</td>'
        f'<a href="/Search/ViewDocumentInfo/{OLDEST_ID}">View</a></tr>'
        f'<tr><td>Deed</td><td>05/14/2019</td>'
        f'<a href="/Search/ViewDocumentInfo/{NEWEST_ID}">View</a></tr>'
    )
    chosen = rcclerk.choose_documents(rcclerk.parse_document_rows(html), role="our")
    assert chosen[0][0].endorsement_id == str(NEWEST_ID)  # без даты — всегда «старый»
    assert [tag for _row, tag in chosen] == ["NEW", "OLD"]


# Правило Коли (уточнение 17.11.2026): приоритет Deed над Mortgage — только если Deed
# новее 2015 года. Если свежего Deed нет, а есть Mortgage с 2015 года — NEW становится
# свежая ипотека, а древний Deed — OLD (для нашего участка).


def test_old_deed_and_fresh_mortgage_make_the_mortgage_new_and_the_deed_old():
    """Сценарий заказчика: Deed 1985 и Mortgage 2025 → NEW = ипотека 2025, OLD = Deed 1985."""
    rows = _rows([
        ("351707", "Deed", "07/10/1985"),
        ("900001", "Mortgage", "03/05/2025"),
    ])
    chosen = rcclerk.choose_documents(rows, role="our")
    assert [(row.endorsement_id, tag) for row, tag in chosen] == [
        ("900001", "NEW"), ("351707", "OLD"),
    ]


def test_neighbour_gets_only_the_fresh_mortgage_when_the_deed_is_old():
    rows = _rows([
        ("351707", "Deed", "07/10/1985"),
        ("900001", "Mortgage", "03/05/2025"),
    ])
    chosen = rcclerk.choose_documents(rows, role="neighbour")
    assert [(row.endorsement_id, tag) for row, tag in chosen] == [("900001", "NEW")]


def test_fresh_deed_still_beats_a_newer_mortgage():
    rows = _rows([
        ("351707", "Deed", "07/10/1985"),
        (str(NEWEST_ID), "Deed", "05/14/2019"),
        ("900001", "Mortgage", "03/05/2025"),
    ])
    chosen = rcclerk.choose_documents(rows, role="our")
    assert [(row.endorsement_id, tag) for row, tag in chosen] == [
        (str(NEWEST_ID), "NEW"), ("351707", "OLD"),
    ]


def test_old_deed_with_an_old_mortgage_keeps_the_deed_as_new():
    """Ипотека до 2015 не даёт права стать NEW: остаётся Deed, как и раньше."""
    rows = _rows([
        ("351707", "Deed", "07/10/1985"),
        ("900001", "Mortgage", "01/09/2002"),
    ])
    chosen = rcclerk.choose_documents(rows, role="our")
    assert [(row.endorsement_id, tag) for row, tag in chosen] == [("351707", "NEW")]


def test_oldest_deed_becomes_old_when_several_old_deeds_and_a_fresh_mortgage():
    rows = _rows([
        ("500", "Deed", "01/01/1961"),
        (str(OLDEST_ID), "Deed", "06/14/1974"),
        ("900001", "Mortgage", "03/05/2025"),
    ])
    chosen = rcclerk.choose_documents(rows, role="our")
    assert [(row.endorsement_id, tag) for row, tag in chosen] == [
        ("900001", "NEW"), ("500", "OLD"),
    ]


def test_no_deeds_and_no_mortgages_means_nothing_to_download():
    rows = _rows([("1", "Satisfaction", "01/01/2000")])
    assert rcclerk.choose_documents(rows, role="our") == []


# ------------------------------------------------------------------- имена


def test_file_names_follow_the_customers_scheme():
    assert rcclerk.document_file_name("our", 32, "NEW") == "OUR LOT 32 NEW.pdf"
    assert rcclerk.document_file_name("our", 32, "OLD") == "OUR LOT 32 OLD.pdf"
    assert rcclerk.document_file_name("neighbour", 30, "NEW") == "LOT 30 NEW.pdf"


# --------------------------------------------------------------- скачивание


class _Response:
    def __init__(self, data: bytes, content_type: str):
        self._data = data
        self.headers = {"Content-Type": content_type}

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def test_pdf_is_downloaded_and_named(tmp_path):
    target = tmp_path / "raw.pdf"
    path = rcclerk.download_pdf(
        NEWEST_ID, target, opener=lambda request, timeout=0: _Response(b"%PDF-1.7 deed", "application/pdf")
    )
    assert path == target and target.read_bytes().startswith(b"%PDF-")


def test_cloudflare_page_is_reported_with_the_link_to_open(tmp_path):
    with pytest.raises(rcclerk.CloudflareBlocked) as blocked:
        rcclerk.download_pdf(
            NEWEST_ID, tmp_path / "raw.pdf",
            opener=lambda request, timeout=0: _Response(b"<html>Just a moment...</html>", "text/html"),
        )
    message = str(blocked.value)
    assert "проверка Cloudflare" in message
    assert rcclerk.document_pdf_url(NEWEST_ID) in message


def test_http_error_is_reported_as_a_cloudflare_problem(tmp_path):
    import urllib.error

    def refusing(request, timeout=0):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

    with pytest.raises(rcclerk.CloudflareBlocked, match="403"):
        rcclerk.download_pdf(NEWEST_ID, tmp_path / "raw.pdf", opener=refusing)


# ------------------------------------------------- лестница скачивания


def _browser_that_saves(downloads: Path, *, name="deed.pdf", data=b"%PDF-1.7 deed"):
    """Браузер, который «скачал» файл: кладём PDF в папку загрузок."""

    def open_it(url: str) -> None:
        (downloads / name).write_bytes(data)

    return open_it


def test_first_rung_direct_download_needs_no_browser(tmp_path):
    opened = []
    path, rung = rcclerk.download_with_fallback(
        NEWEST_ID, tmp_path / "raw.pdf",
        opener=lambda request, timeout=0: _Response(b"%PDF-1.7 deed", "application/pdf"),
        open_browser=lambda url: opened.append(url),
        downloads_dir=tmp_path / "downloads",
    )
    assert rung == "direct" and path.read_bytes().startswith(b"%PDF-")
    assert opened == []  # браузер не понадобился


def test_second_rung_opens_the_browser_and_picks_up_the_file(tmp_path):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    steps: list[str] = []
    path, rung = rcclerk.download_with_fallback(
        NEWEST_ID, tmp_path / "raw.pdf",
        opener=lambda request, timeout=0: _Response(b"<html>Just a moment...</html>", "text/html"),
        open_browser=_browser_that_saves(downloads),
        downloads_dir=downloads, watch_timeout=2.0, poll=0.05,
        on_step=steps.append,
    )
    assert rung == "browser" and path.read_bytes().startswith(b"%PDF-")
    assert any("открываю браузер" in step for step in steps)
    assert any("взят из загрузок" in step for step in steps)


def test_third_rung_is_the_manual_step_with_a_ready_link(tmp_path):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    with pytest.raises(rcclerk.ManualStepNeeded) as manual:
        rcclerk.download_with_fallback(
            NEWEST_ID, tmp_path / "raw.pdf",
            opener=lambda request, timeout=0: _Response(b"<html>Just a moment...</html>", "text/html"),
            open_browser=lambda url: None,  # браузер открылся, но файла нет
            downloads_dir=downloads, watch_timeout=0.2, poll=0.05,
        )
    assert rcclerk.document_pdf_url(NEWEST_ID) in str(manual.value)
    assert manual.value.folder == downloads


def test_make_downloader_passes_long_captcha_wait(tmp_path, monkeypatch):
    import deed_intake

    seen = {}

    def fake_fallback(endorsement_id, destination, **kwargs):
        seen.update(kwargs)
        return Path(destination), "direct"

    monkeypatch.setattr(rcclerk, "download_with_fallback", fake_fallback)
    download = deed_intake.make_downloader(watch_timeout=deed_intake.PDF_WAIT_LONG_SEC)
    row = rcclerk.DeedRow(endorsement_id="1905502")
    assert download(row, tmp_path / "x.pdf")[1] == "direct"
    assert seen["watch_timeout"] == 300.0


def test_default_watch_timeout_is_45_seconds():
    # Живой случай 3879 (25.11.2026): задание пять минут висело на одном
    # документе Клерка. Исход браузерной ступени ясен гораздо раньше.
    import inspect

    default = inspect.signature(rcclerk.download_with_fallback).parameters["watch_timeout"].default
    assert default == 45.0


def test_timeout_takes_emergency_screenshot_into_job_folder(tmp_path):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    shots = []

    def fake_shot(folder, endorsement_id):
        shots.append((folder, endorsement_id))
        return f" Снимок экрана при сбое: {folder}/CLERK_FAIL_{endorsement_id}_20261125_084142.png"

    with pytest.raises(rcclerk.ManualStepNeeded) as manual:
        rcclerk.download_with_fallback(
            NEWEST_ID, tmp_path / "raw.pdf",
            opener=lambda request, timeout=0: _Response(b"<html>Just a moment...</html>", "text/html"),
            open_browser=lambda url: None,
            downloads_dir=downloads, watch_timeout=0.2, poll=0.05,
            fail_dir=tmp_path / "data", shot_fn=fake_shot,
        )
    # Снимок пишется в папку задания (fail_dir), а не в «Загрузки».
    assert shots == [(tmp_path / "data", NEWEST_ID)]
    assert "Снимок экрана при сбое" in str(manual.value)
    assert "CLERK_FAIL" in str(manual.value)


def test_successful_browser_rung_takes_no_screenshot(tmp_path):
    downloads = tmp_path / "downloads"
    downloads.mkdir()

    def forbidden_shot(folder, endorsement_id):
        raise AssertionError("снимок при успешной загрузке не нужен")

    path, rung = rcclerk.download_with_fallback(
        NEWEST_ID, tmp_path / "raw.pdf",
        opener=lambda request, timeout=0: _Response(b"<html>Just a moment...</html>", "text/html"),
        open_browser=_browser_that_saves(downloads),
        downloads_dir=downloads, watch_timeout=2.0, poll=0.05,
        fail_dir=tmp_path, shot_fn=forbidden_shot,
    )
    assert rung == "browser" and path.is_file()


def test_a_browser_that_cannot_open_also_ends_in_the_manual_step(tmp_path):
    def broken(url):
        raise OSError("нет браузера по умолчанию")

    with pytest.raises(rcclerk.ManualStepNeeded, match="не удалось открыть браузер"):
        rcclerk.download_with_fallback(
            NEWEST_ID, tmp_path / "raw.pdf",
            opener=lambda request, timeout=0: _Response(b"<html></html>", "text/html"),
            open_browser=broken, downloads_dir=tmp_path, watch_timeout=0.1,
        )


def test_partial_and_fake_pdfs_are_not_taken_from_downloads(tmp_path):
    import os
    import time

    since = time.time() - 1
    (tmp_path / "half.pdf.crdownload").write_bytes(b"%PDF-1.7 half")
    (tmp_path / "broken.pdf").write_bytes(b"<html>error</html>")
    assert rcclerk.find_new_pdf(tmp_path, since=since) is None
    good = tmp_path / "deed.pdf"
    good.write_bytes(b"%PDF-1.7 deed")
    os.utime(good, (since + 0.5, since + 0.5))
    assert rcclerk.find_new_pdf(tmp_path, since=since) == good
    # Старый файл (до открытия браузера) не подхватывается.
    assert rcclerk.find_new_pdf(tmp_path, since=since + 5) is None


# ------------------------------------------- страницы описания и обрезка


def _text_pdf(path, pages: list[str]):
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    for content in pages:
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(36, 36, 560, 760), content, fontsize=9)
    doc.save(str(path))
    doc.close()
    return path


DESCRIPTION_TEXT = (
    "BEGINNING AT A POINT on the easterly side of Alvine Avenue, distant 200.00 feet "
    "northerly from the corner formed by the intersection of the easterly side of "
    'Alvine Avenue with the northerly side of Amboy Road; RUNNING THENCE N 67°33\'48" E, '
    '174.89 feet; THENCE N 50°25\'10" W, 45.30 feet; THENCE S 67°33\'48" W, 153.64 feet; '
    "THENCE S 22°26'12\" E, 40.00 feet to the point or place of BEGINNING."
)


def test_description_pages_are_found_in_a_text_pdf(tmp_path):
    import deed_pages

    pdf = _text_pdf(tmp_path / "deed.pdf", ["COVER PAGE", DESCRIPTION_TEXT, "RP-5217"])
    found = deed_pages.find_description_pages(pdf)
    assert found.pages == [2] and found.from_text_layer is True and found.scanned == 0
    assert "174.89" in found.text
    assert found.pages_spec == "2"


def test_description_pages_are_found_in_a_scan_without_spending_ai(tmp_path, monkeypatch):
    import deed_pages
    import ocr_pages
    from test_local_ocr import FakeEngine, _scan_pdf

    # Скан: описания в текстовом слое нет, ищем локальным OCR по страницам.
    pdf = _scan_pdf(tmp_path / "deed.pdf", DESCRIPTION_TEXT)
    monkeypatch.setenv("TITLESURVEY_OCR_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(ocr_pages, "_available", True)
    monkeypatch.setattr(ocr_pages, "_engine", FakeEngine(DESCRIPTION_TEXT))
    found = deed_pages.find_description_pages(pdf)
    assert found.pages == [1] and found.scanned >= 1
    assert "BEGINNING AT A POINT" in found.text.upper()


def test_trimmed_copy_keeps_only_the_description_pages(tmp_path):
    import deed_pages

    pdf = _text_pdf(tmp_path / "deed.pdf", ["COVER PAGE", DESCRIPTION_TEXT, "RP-5217"])
    out = deed_pages.trim_pdf(pdf, [2], tmp_path / "OUR 32 NEW_p2.pdf")
    import pymupdf

    doc = pymupdf.open(str(out))
    try:
        assert len(doc) == 1
        assert "174.89" in doc[0].get_text()
        assert "COVER PAGE" not in doc[0].get_text()
    finally:
        doc.close()
    with pytest.raises(Exception):
        deed_pages.trim_pdf(pdf, [9], tmp_path / "bad.pdf")


def test_pages_spec_is_written_the_way_step_one_reads_it():
    import deed_pages

    assert deed_pages.pages_spec([]) == ""
    assert deed_pages.pages_spec([4]) == "4"
    assert deed_pages.pages_spec([4, 5, 6]) == "4-6"
    assert deed_pages.pages_spec([7, 4, 5, 8]) == "4-5,7-8"


# ------------------------------------------------------------ папка описаний


def _pdf(tmp_path: Path, name: str = "raw.pdf") -> Path:
    path = tmp_path / name
    path.write_bytes(b"%PDF-1.7 deed")
    return path


def test_store_files_the_document_with_sidecar_and_manifest(tmp_path):
    folder = tmp_path / "01_deeds"
    trimmed = _pdf(tmp_path, "trimmed.pdf")
    record = deed_store.store_pdf(
        folder, _pdf(tmp_path),
        role="our", lot=LOT, tag="NEW", block=BLOCK,
        doc_type="DEED", doc_date="2019-05-14", endorsement_id=NEWEST_ID,
        source_url=rcclerk.document_pdf_url(NEWEST_ID), pages="4-6",
        trimmed_source=trimmed,
    )
    # Документ целиком и рядом — только страницы описания (схема «оба»).
    assert record.file == "OUR LOT 32 NEW.pdf"
    assert record.trimmed == "OUR LOT 32 NEW_p4-6.pdf"
    assert (folder / "OUR LOT 32 NEW.pdf").is_file()
    assert (folder / "OUR LOT 32 NEW_p4-6.pdf").is_file()
    sidecar = json.loads((folder / "OUR LOT 32 NEW.json").read_text(encoding="utf-8"))
    assert sidecar["endorsement_id"] == str(NEWEST_ID)
    assert sidecar["trimmed"] == "OUR LOT 32 NEW_p4-6.pdf"
    assert sidecar["source_url"].endswith(f"p_endorsementId={NEWEST_ID}")
    manifest = deed_store.read_manifest(folder)
    assert manifest["version"] == deed_store.MANIFEST_VERSION
    assert [item["file"] for item in manifest["items"]] == ["OUR LOT 32 NEW.pdf"]
    assert manifest["items"][0]["trimmed"] == "OUR LOT 32 NEW_p4-6.pdf"


def test_manifest_lists_our_lot_first_and_neighbours_after(tmp_path):
    folder = tmp_path / "01_deeds"
    for role, lot, tag, number in (("neighbour", 30, "NEW", 555), ("our", LOT, "NEW", NEWEST_ID),
                                   ("our", LOT, "OLD", OLDEST_ID)):
        deed_store.store_pdf(
            folder, _pdf(tmp_path, f"{number}.pdf"),
            role=role, lot=lot, tag=tag, block=BLOCK,
            doc_type="DEED", endorsement_id=number,
        )
    manifest = deed_store.read_manifest(folder)
    order = [(item["role"], item["lot"], item["tag"]) for item in manifest["items"]]
    assert order == [("our", "32", "NEW"), ("our", "32", "OLD"), ("neighbour", "30", "NEW")]
    assert deed_store.summary(folder).splitlines()[0] == "Документов в папке описаний: 3"


def test_the_same_document_is_not_stored_twice(tmp_path):
    folder = tmp_path / "01_deeds"
    first = deed_store.store_pdf(
        folder, _pdf(tmp_path), role="our", lot=LOT, tag="NEW",
        doc_type="DEED", endorsement_id=NEWEST_ID,
    )
    records = deed_store.read_records(folder)
    assert deed_store.duplicate_of(records, NEWEST_ID) is not None
    assert deed_store.duplicate_of(records, 999) is None
    # Повторное сохранение того же номера — не второй файл, а обновление паспорта.
    deed_store.store_pdf(
        folder, _pdf(tmp_path), role="our", lot=LOT, tag="NEW",
        doc_type="DEED", endorsement_id=NEWEST_ID, pages="2",
        trimmed_source=_pdf(tmp_path, "trim2.pdf"),
    )
    assert len(list(folder.glob("*.pdf"))) == 2  # весь документ и копия со стр. 2
    assert first.endorsement_id == str(NEWEST_ID)
    assert len(deed_store.read_records(folder)) == 1  # запись одна: документ один


def test_summary_says_what_is_missing(tmp_path):
    folder = tmp_path / "01_deeds"
    deed_store.store_pdf(
        folder, _pdf(tmp_path), role="neighbour", lot=30, tag="NEW",
        doc_type="DEED", endorsement_id=555,
    )
    text = deed_store.summary(folder)
    assert "нашего участка в папке нет" in text
    assert "⚠ не проверен" in text


# ---------------------------------------------- настоящая страница от заказчика

#: Сохранённая страница списка (Block 6323, Lot 32) — та, что прислал заказчик.
#: Её сохранил браузер с переводом на русский: «Поступок» = Deed, «Ипотека» =
#: Mortgage, «Погашение ипотеки»/«САТ»/«A/MTG» = записи о погашении и переуступке.
REAL_PAGE = Path(__file__).resolve().parents[1] / "docs" / "Клерк округа Ричмонд, штат Нью-Йорк.html"


def _real_page_rows():
    if not REAL_PAGE.is_file():
        pytest.skip("страница списка документов не приложена к репозиторию")
    return rcclerk.parse_document_rows(
        REAL_PAGE.read_text(encoding="utf-8", errors="replace")
    )


def test_real_page_gives_every_row_with_type_date_and_clerk_number():
    rows = _real_page_rows()
    assert len(rows) == 12
    first = rows[0]
    assert first.endorsement_id == "2642470"
    assert first.doc_type == "Погашение ипотеки"
    assert first.kind == rcclerk.KIND_SATISFACTION
    assert first.doc_date == date(2022, 4, 18)     # точки — день.месяц
    assert first.instrument == "882324"


def test_real_page_deed_rows_are_found_through_the_russian_translation():
    rows = {row.endorsement_id: row for row in _real_page_rows()}
    assert rows["2640217"].doc_type == "Поступок"
    assert rows["2640217"].kind == rcclerk.KIND_DEED
    assert rows["351707"].doc_type == "ПОСТУПОК"
    assert rows["351707"].kind == rcclerk.KIND_DEED
    assert rows["351707"].doc_date == date(1985, 7, 10)
    assert rows["351707"].clerk_number == "№ 17623, кн. 45, стр. 3683"
    assert rows["2640218"].kind == rcclerk.KIND_MORTGAGE


def test_real_page_satisfactions_are_not_taken_for_mortgages():
    """«Погашение ипотеки» и «A/MTG» содержат слово «ипотека», но описания в них нет."""
    rows = {row.endorsement_id: row for row in _real_page_rows()}
    for endorsement_id in ("2642470", "1775233", "1732196", "1694529", "1818080", "1497415"):
        assert rows[endorsement_id].kind == rcclerk.KIND_SATISFACTION, endorsement_id
        assert not rows[endorsement_id].is_mortgage


def test_real_page_gives_our_lot_the_oldest_and_newest_deed():
    rows = _real_page_rows()
    chosen = rcclerk.choose_documents(rows, role="our")
    assert [(row.endorsement_id, tag) for row, tag in chosen] == [
        ("2640217", "NEW"),   # Deed 2022, а не свежая ипотека 2640218
        ("351707", "OLD"),    # Deed 1985
    ]
    assert chosen[0][0].doc_date >= chosen[1][0].doc_date


def test_real_page_gives_a_neighbour_only_the_newest_deed():
    chosen = rcclerk.choose_documents(_real_page_rows(), role="neighbour")
    assert [(row.endorsement_id, tag) for row, tag in chosen] == [("2640217", "NEW")]


def test_real_page_is_recognised_as_a_list_and_not_a_cloudflare_page():
    text = REAL_PAGE.read_text(encoding="utf-8", errors="replace")
    assert "ShowResultsBlocks/0?Block=6323" in text   # сохранена именно эта страница
    assert not any(
        mark in text[:4000].lower() and "ViewDocumentInfo" not in text
        for mark in rcclerk.CLOUDFLARE_MARKS
    )


def test_mortgage_is_taken_only_when_there_is_no_deed_on_the_real_page():
    rows = [row for row in _real_page_rows() if not row.is_deed]
    chosen = rcclerk.choose_documents(rows, role="our")
    assert [(row.endorsement_id, tag) for row, tag in chosen] == [
        ("2640218", "NEW"),   # Ипотека 2022 (свежайшая из ипотек)
        ("940351", "OLD"),    # Ипотека 1985
    ]


# ------------------------------------------- типы и даты: разбор без страницы


@pytest.mark.parametrize(
    "doc_type, kind",
    [
        ("DEED", rcclerk.KIND_DEED),
        ("WAR DEED", rcclerk.KIND_DEED),
        ("Поступок", rcclerk.KIND_DEED),
        ("ПОСТУПОК", rcclerk.KIND_DEED),
        ("Акт купли-продажи", rcclerk.KIND_DEED),
        ("MORTGAGE", rcclerk.KIND_MORTGAGE),
        ("Ипотека", rcclerk.KIND_MORTGAGE),
        ("ИПОТЕКА", rcclerk.KIND_MORTGAGE),
        ("Погашение ипотеки", rcclerk.KIND_SATISFACTION),
        ("САТ", rcclerk.KIND_SATISFACTION),
        ("SAT", rcclerk.KIND_SATISFACTION),
        ("A/MTG", rcclerk.KIND_SATISFACTION),
        ("Assignment of mortgage", rcclerk.KIND_SATISFACTION),
        ("", rcclerk.KIND_OTHER),
        ("Справка", rcclerk.KIND_OTHER),
    ],
)
def test_document_kind_is_understood_in_english_and_russian(doc_type, kind):
    assert rcclerk.classify_document(doc_type) == kind


@pytest.mark.parametrize(
    "text, expected",
    [
        ("18.04.2022", date(2022, 4, 18)),   # день.месяц (переведённая страница)
        ("9/3/2022", date(2022, 9, 3)),      # сайт: месяц/день — 3 сентября
        ("03.09.2022", date(2022, 9, 3)),    # перевод: день.месяц — та же дата
        ("7/10/1985", date(1985, 7, 10)),
        ("31.02.2020", None),                # такого дня нет — не выдумываем дату
        ("без даты", None),
    ],
)
def test_registration_date_is_read_in_both_conventions(text, expected):
    row = rcclerk.parse_document_rows(
        f'<tr><td data-label="Type">DEED</td><td data-label="Recorded">{text}</td>'
        '<td data-label="Number"><a href="/Search/ViewDocumentInfo/42">42</a></td></tr>'
    )
    assert row[0].doc_date == expected


def test_description_search_stops_after_beginning_and_completion_page(tmp_path, monkeypatch):
    import deed_pages
    import ocr_pages

    pages = ["", "", "", "", "", ""]
    start = (
        "BEGINNING AT A POINT on the easterly line; THENCE N 10 E 50 feet; "
        "THENCE N 20 W 30 feet."
    )
    end = "THENCE S 10 W 50 feet to the point or place of BEGINNING."
    monkeypatch.setattr(deed_pages, "read_pdf_page_texts", lambda _pdf: pages)
    monkeypatch.setattr(ocr_pages, "available", lambda: True)
    calls = []

    def fake_ocr(_pdf, first, last, *, dpi, **_kwargs):
        calls.append((first, last, dpi))
        return {2: start, 3: end}.get(first, "")

    monkeypatch.setattr(ocr_pages, "ocr_pdf_pages", fake_ocr)
    progress = []
    found = deed_pages.find_description_pages(
        tmp_path / "deed.pdf", dpi=96, progress=progress.append,
    )
    assert found.pages == [2, 3]
    assert "BEGINNING AT A POINT" in found.text
    assert "point or place of BEGINNING" in found.text
    assert found.scanned == 3
    assert calls == [(1, 1, 96), (2, 2, 96), (3, 3, 96)]
    assert any("части 1/2" in line and "стр. 1–3" in line for line in progress)


def test_description_search_uses_second_half_only_when_first_has_no_match(tmp_path, monkeypatch):
    import deed_pages
    import ocr_pages

    pages = [""] * 5  # floor(5/2) = 2 pages in the first part
    start = "BEGINNING AT A POINT; THENCE N 10 E 20 feet."
    end = "THENCE S 10 W 20 feet to the point of BEGINNING."
    monkeypatch.setattr(deed_pages, "read_pdf_page_texts", lambda _pdf: pages)
    monkeypatch.setattr(ocr_pages, "available", lambda: True)
    calls = []

    def fake_ocr(_pdf, first, last, *, dpi, **_kwargs):
        calls.append(first)
        return {4: start, 5: end}.get(first, "")

    monkeypatch.setattr(ocr_pages, "ocr_pdf_pages", fake_ocr)
    progress = []
    found = deed_pages.find_description_pages(
        tmp_path / "deed.pdf", dpi=80, progress=progress.append,
    )
    assert found.pages == [4, 5]
    assert calls == [1, 2, 3, 4, 5]
    assert any("части 1/2" in line and "стр. 1–2" in line for line in progress)
    assert any("части 2/2" in line and "стр. 3–5" in line for line in progress)


def test_stop_interrupts_the_browser_wait_at_once(tmp_path):
    """«Стоп» во время ожидания Cloudflare/браузера прерывает ожидание, а не ждёт 300 с."""
    import time

    stop = {"flag": False}
    steps: list[str] = []

    def open_browser(url):
        stop["flag"] = True  # пользователь нажал «Стоп», пока браузер открывается

    started = time.monotonic()
    with pytest.raises(rcclerk.DownloadStopped):
        rcclerk.download_with_fallback(
            NEWEST_ID, tmp_path / "raw.pdf",
            opener=lambda request, timeout=0: _Response(b"<html>Just a moment...</html>", "text/html"),
            open_browser=open_browser,
            downloads_dir=tmp_path / "downloads",
            watch_timeout=300.0, poll=0.05,
            on_step=steps.append,
            should_stop=lambda: stop["flag"],
        )
    assert time.monotonic() - started < 5.0
    assert any("2/3" in step for step in steps)  # ступень браузера видна в журнале


def test_stop_during_the_wait_is_a_clean_cancel_for_the_intake(tmp_path, monkeypatch):
    """В сборе «Стоп» во время ожидания даёт IntakeCancelled, а не запись «не скачался»."""
    import deed_intake

    def stopping_downloader(row, destination):
        raise rcclerk.DownloadStopped("остановлено пользователем")

    outcome_missing: list[str] = []
    with pytest.raises(deed_intake.IntakeCancelled):
        deed_intake._collect_one(
            block_no=5711, lot_value=34, role="our", row=_row_for_test(), tag="",
            target_dir=tmp_path, base_url=rcclerk.BASE_URL,
            download=stopping_downloader, find_pages=lambda pdf: None, ocr_dpi=200,
            say=lambda message: None, missing=outcome_missing,
        )
    assert outcome_missing == []


def _row_for_test():
    import datetime as _dt

    return rcclerk.DeedRow(endorsement_id=NEWEST_ID, doc_type="DEED", doc_date=_dt.date(2023, 9, 5))


_CLERK_ERROR_PAGE = (
    b"<html><body>WE COULD NOT LOAD THE REQUESTED DOCUMENT IMAGE DUE TO AN UNEXPECTED ERROR: "
    b"ERROR READING JOBJECT FROM JSONREADER. PATH '', LINE 0, POSITION 0.. PLEASE TRY AGAIN.</body></html>"
)


def test_clerk_error_page_is_a_server_error_not_cloudflare(tmp_path):
    with pytest.raises(rcclerk.DocumentServerError) as caught:
        rcclerk.download_pdf(
            NEWEST_ID, tmp_path / "raw.pdf",
            opener=lambda request, timeout=0: _Response(_CLERK_ERROR_PAGE, "text/html"),
        )
    assert not isinstance(caught.value, rcclerk.CloudflareBlocked)
    assert "718-675-7700" in str(caught.value)


def test_clerk_error_retries_directly_and_never_opens_the_browser(tmp_path, monkeypatch):
    monkeypatch.setattr(rcclerk, "SERVER_ERROR_RETRY_PAUSE_SEC", 0.0)
    attempts = {"n": 0}
    opened = []

    def opener(request, timeout=0):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return _Response(_CLERK_ERROR_PAGE, "text/html")
        return _Response(b"%PDF-1.7 deed", "application/pdf")

    path, rung = rcclerk.download_with_fallback(
        NEWEST_ID, tmp_path / "raw.pdf", opener=opener,
        open_browser=lambda url: opened.append(url), downloads_dir=tmp_path / "downloads",
    )
    assert rung == "direct" and path.read_bytes().startswith(b"%PDF-")
    assert attempts["n"] == 3 and opened == []


def test_persistent_clerk_error_ends_fast_without_browser_wait(tmp_path, monkeypatch):
    import time

    monkeypatch.setattr(rcclerk, "SERVER_ERROR_RETRY_PAUSE_SEC", 0.0)
    opened = []
    started = time.monotonic()
    with pytest.raises(rcclerk.DocumentServerError):
        rcclerk.download_with_fallback(
            NEWEST_ID, tmp_path / "raw.pdf",
            opener=lambda request, timeout=0: _Response(_CLERK_ERROR_PAGE, "text/html"),
            open_browser=lambda url: opened.append(url),
            downloads_dir=tmp_path / "downloads", watch_timeout=300.0,
        )
    assert opened == []
    assert time.monotonic() - started < 5.0


def test_collect_skips_a_document_the_clerk_could_not_serve(tmp_path):
    import deed_intake

    missing: list[str] = []

    def failing_downloader(row, destination):
        raise rcclerk.DocumentServerError("сайт округа не отдал документ: ошибка на стороне сайта")

    result = deed_intake._collect_one(
        block_no=5711, lot_value=34, role="our", row=_row_for_test(), tag="",
        target_dir=tmp_path, base_url=rcclerk.BASE_URL,
        download=failing_downloader, find_pages=lambda pdf: None, ocr_dpi=200,
        say=lambda message: None, missing=missing,
    )
    assert result is None
    assert any("ошибка на стороне сайта" in item for item in missing)


def test_second_wave_after_doctor_picks_up_late_pdf(tmp_path):
    """Первая волна пуста: «доктор» позван, вторая волна дожидается файла."""
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    doctor_calls = []

    def doctor(endorsement_id):
        doctor_calls.append(endorsement_id)
        (downloads / "late.pdf").write_bytes(b"%PDF-1.7 late")

    path, rung = rcclerk.download_with_fallback(
        NEWEST_ID, tmp_path / "raw.pdf",
        opener=lambda request, timeout=0: _Response(b"<html>Just a moment...</html>", "text/html"),
        open_browser=lambda url: None,
        downloads_dir=downloads, watch_timeout=0.2, poll=0.05,
        prompt_operator=doctor,
    )
    assert rung == "browser" and path.read_bytes().startswith(b"%PDF-")
    assert doctor_calls == [NEWEST_ID]


def test_manual_step_reports_hook_state(tmp_path):
    """Журнал автоматики браузера попадает в текст ручного шага, а не в никуда."""
    downloads = tmp_path / "downloads"
    downloads.mkdir()

    def hook(endorsement_id):
        return None

    hook.state = lambda endorsement_id: "закреплённое окно ещё не PDF: [msedge.exe] заголовок 'VSCMS'"
    with pytest.raises(rcclerk.ManualStepNeeded) as manual:
        rcclerk.download_with_fallback(
            NEWEST_ID, tmp_path / "raw.pdf",
            opener=lambda request, timeout=0: _Response(b"<html>Just a moment...</html>", "text/html"),
            open_browser=lambda url: None,
            downloads_dir=downloads, watch_timeout=0.2, poll=0.05,
            auto_save=hook,
        )
    assert "Автоматика браузера докладывает" in str(manual.value)
    assert "VSCMS" in str(manual.value)


def test_make_downloader_sends_shots_and_doctor_to_job_folder(tmp_path, monkeypatch):
    import deed_intake

    seen = {}

    def fake_fallback(endorsement_id, destination, **kwargs):
        seen.update(kwargs)
        return Path(destination), "direct"

    monkeypatch.setattr(rcclerk, "download_with_fallback", fake_fallback)
    download = deed_intake.make_downloader(fail_dir=tmp_path / "job", progress=lambda _m: None)
    row = rcclerk.DeedRow(endorsement_id="1905502")
    assert download(row, tmp_path / "x.pdf")[1] == "direct"
    assert seen["fail_dir"] == tmp_path / "job"
    assert callable(seen["prompt_operator"])


def test_browser_doctor_reports_window_titles():
    """Доктор вслух перечисляет окна браузера и вердикт по заголовку."""
    import browser_save as bs

    lines: list[str] = []
    windows = [
        bs.BrowserWindow(1, "Just a moment...", "msedge.exe", r"C:\Edge\msedge.exe"),
        bs.BrowserWindow(2, "Deed.pdf", "chrome.exe", r"C:\Chrome\chrome.exe"),
    ]
    rcclerk.browser_doctor("42", emit=lines.append, windows_fn=lambda: windows)
    joined = "\n".join(lines)
    assert "Cloudflare" in joined and "PDF" in joined
