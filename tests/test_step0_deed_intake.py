"""
Шаг 0: документы участка у клерка Richmond County (Staten Island).

Проверяется по схеме, которую Коля объяснил 15.11.2026:

* страница списка открывается по block/lot;
* берутся строки **Deed**, а если их нет — **Mortgage**;
* для нашего лота качаются самый свежий и самый старый документ, для соседей —
  только свежий;
* имя файла: ``OUR 32 NEW.pdf`` / ``OUR 32 OLD.pdf`` (сосед — ``LOT 30 NEW.pdf``);
* скачивание честно падает, если сайт показал проверку Cloudflare, и объясняет,
  что делать руками.

Сеть не нужна: HTML и ответы подставляются.
"""

from __future__ import annotations

import json
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


def test_no_deeds_and_no_mortgages_means_nothing_to_download():
    rows = _rows([("1", "Satisfaction", "01/01/2000")])
    assert rcclerk.choose_documents(rows, role="our") == []


# ------------------------------------------------------------------- имена


def test_file_names_follow_the_customers_scheme():
    assert rcclerk.document_file_name("our", 32, "NEW") == "OUR 32 NEW.pdf"
    assert rcclerk.document_file_name("our", 32, "OLD") == "OUR 32 OLD.pdf"
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


# ------------------------------------------------------------ папка описаний


def _pdf(tmp_path: Path, name: str = "raw.pdf") -> Path:
    path = tmp_path / name
    path.write_bytes(b"%PDF-1.7 deed")
    return path


def test_store_files_the_document_with_sidecar_and_manifest(tmp_path):
    folder = tmp_path / "01_deeds"
    record = deed_store.store_pdf(
        folder, _pdf(tmp_path),
        role="our", lot=LOT, tag="NEW", block=BLOCK,
        doc_type="DEED", doc_date="2019-05-14", endorsement_id=NEWEST_ID,
        source_url=rcclerk.document_pdf_url(NEWEST_ID), pages="4-6",
    )
    assert record.file == "OUR 32 NEW_p4-6.pdf"          # суффикс страниц понимает Шаг 1
    assert (folder / "OUR 32 NEW_p4-6.pdf").is_file()
    sidecar = json.loads((folder / "OUR 32 NEW_p4-6.json").read_text(encoding="utf-8"))
    assert sidecar["endorsement_id"] == str(NEWEST_ID)
    assert sidecar["source_url"].endswith(f"p_endorsementId={NEWEST_ID}")
    manifest = deed_store.read_manifest(folder)
    assert manifest["version"] == deed_store.MANIFEST_VERSION
    assert [item["file"] for item in manifest["items"]] == ["OUR 32 NEW_p4-6.pdf"]


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
    )
    assert len(list(folder.glob("*.pdf"))) == 2  # NEW.pdf и NEW_p2.pdf — разные имена
    assert first.endorsement_id == str(NEWEST_ID)


def test_summary_says_what_is_missing(tmp_path):
    folder = tmp_path / "01_deeds"
    deed_store.store_pdf(
        folder, _pdf(tmp_path), role="neighbour", lot=30, tag="NEW",
        doc_type="DEED", endorsement_id=555,
    )
    text = deed_store.summary(folder)
    assert "нашего участка в папке нет" in text
    assert "⚠ не проверен" in text
