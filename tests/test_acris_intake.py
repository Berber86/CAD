"""Шаг 0 по ACRIS: диспетчер районов и проход на настоящей странице.

Staten Island остаётся за Клерком Ричмонда; Manhattan/Bronx/Brooklyn/Queens
идёт в ACRIS: список сразу по 99 строк, выбор Deed/Mortgage тем же правилом,
document_id по CRFN, документ — фейковым загрузчиком (читалка проверяется
отдельно в tests/test_acris_viewer.py). Сети нет.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import deed_intake
import deed_pages
import rcclerk

DATA = Path(__file__).resolve().parent / "data"
RESULTS_HTML = (DATA / "acris_results_manhattan_731_53.html").read_text(encoding="utf-8")
DOC_ID = "2024101000085001"


def _downloader(row, destination):
    destination.write_bytes(b"%PDF-1.4 fake deed")
    return destination, "browser"


def _empty_pages(_pdf):
    return deed_pages.DescriptionPages()


def test_source_name_by_borough():
    assert deed_intake.step0_source_name("Staten Island") == "Richmond County Clerk"
    assert deed_intake.step0_source_name("Richmond County") == "Richmond County Clerk"
    assert deed_intake.step0_source_name("Manhattan") == "ACRIS"
    assert deed_intake.step0_source_name("Queens") == "ACRIS"
    assert deed_intake.step0_source_name("Westchester") == ""
    assert deed_intake.step0_source_name("") == ""


def test_unknown_borough_is_rejected_before_any_site(tmp_path):
    with pytest.raises(ValueError, match="ACRIS"):
        deed_intake.collect_deeds(block=1, lot=1, borough="Westchester", folder=tmp_path)
    with pytest.raises(ValueError):
        deed_intake.collect_deeds(block=1, lot=1, borough="", folder=tmp_path)


def test_acris_collect_on_real_page(tmp_path):
    calls: list[tuple[int, int, int]] = []

    def fetch_page(code, block, lot, page):
        calls.append((code, block, page))
        return RESULTS_HTML

    outcome = deed_intake.collect_deeds(
        block=731, lot=53, borough="Manhattan",
        neighbours=[54], folder=tmp_path,
        acris_fetch_page=fetch_page,
        acris_id_fetcher=lambda crfn: DOC_ID,
        acris_downloader=_downloader,
        page_finder=_empty_pages,
    )
    assert outcome.ok
    by_file = {record.file: record for record in outcome.records}
    # На странице один Deed (2024) и свежая Mortgage (2026): свежий Deed — NEW,
    # старого второго Deed нет. Сосед получает только NEW.
    assert set(by_file) == {"OUR LOT 53 NEW.pdf", "LOT 54 NEW.pdf"}
    ours = by_file["OUR LOT 53 NEW.pdf"]
    assert ours.doc_type == "DEED"
    assert ours.endorsement_id == DOC_ID
    assert ours.source_url.endswith(f"doc_id={DOC_ID}")
    assert any("CRFN 2024000265906" in note for note in ours.notes)
    # Район — кодом 1 (Manhattan), список читается один раз на лот (страниц нет дальше).
    assert calls == [(1, 731, 1), (1, 731, 1)]
    assert (tmp_path / "_manifest.json").is_file()


def test_acris_blocked_list_is_reported_not_faked(tmp_path):
    import acris

    def fetch_page(code, block, lot, page):
        raise acris.AcrisBlocked("ACRIS показал страницу отказа")

    outcome = deed_intake.collect_deeds(
        block=731, lot=53, borough="Brooklyn", folder=tmp_path,
        acris_fetch_page=fetch_page,
        acris_id_fetcher=lambda crfn: DOC_ID,
        acris_downloader=_downloader,
        page_finder=_empty_pages,
    )
    assert not outcome.ok
    assert any("страницей отказа ACRIS" in item for item in outcome.missing)


def _page_html(crfn: str, *, has_next: bool) -> str:
    nxt = '<a href="?page=2">next</a>' if has_next else ""
    return (
        "<html><body>Records 1 - 1 " + nxt + "<table>"
        "<tr><td>CRFN</td><td>Lot</td><td>Partial</td><td>Doc Date</td>"
        "<td>Recorded / Filed</td><td>Document Type</td><td>Pages</td></tr>"
        f"<tr><td>{crfn}</td><td>53</td><td>ENTIRE LOT</td><td>9/11/2024</td>"
        "<td>10/10/2024 3:00:18 PM</td><td>DEED</td><td>8</td></tr>"
        "</table></body></html>"
    )


def test_acris_rows_follow_next_pages():
    seen_pages: list[int] = []
    pages = {1: _page_html("2024000000001", has_next=True),
             2: _page_html("2024000000002", has_next=False)}

    def fetch_page(code, block, lot, page):
        seen_pages.append(page)
        return pages[page]

    rows = deed_intake._fetch_acris_rows(1, 731, 53, fetch_page)
    assert [row.crfn for row in rows] == ["2024000000001", "2024000000002"]
    assert seen_pages == [1, 2]


def test_make_acris_downloader_opens_viewer_url(tmp_path):
    seen = {}

    def viewer(url, destination, **kwargs):
        seen["url"] = url
        seen["downloads_dir"] = kwargs.get("downloads_dir")
        return Path(destination), "browser"

    download = deed_intake.make_acris_downloader(
        downloads_dir=tmp_path / "dl", viewer_download=viewer,
    )
    row = rcclerk.DeedRow(endorsement_id=DOC_ID, doc_type="DEED")
    saved, rung = download(row, tmp_path / "x.pdf")
    assert rung == "browser"
    assert seen["url"].endswith(f"DocumentImageView?doc_id={DOC_ID}")
    assert seen["downloads_dir"] == tmp_path / "dl"
