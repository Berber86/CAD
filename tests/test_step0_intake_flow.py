"""
Шаг 0 целиком: от block/lot до папки описаний — без сети, без браузера, без GUI.

Сеть и OCR подменены: ``fetch_list`` отдаёт HTML списка, ``downloader`` пишет
настоящий PDF (текстовый слой), ``page_finder`` возвращает готовые страницы.
Так проверяется весь проход вместе с авариями: Cloudflare, пустой список, «Стоп».
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import deed_intake
import deed_pages
import deed_store
import rcclerk

BLOCK = 6323
LOT = 32
NEIGHBOUR = 30

OUR_NEW = 2640218
OUR_OLD = 111222
NEIGHBOUR_NEW = 555777
NEIGHBOUR_OLD = 555111


def _row(endorsement_id: int, kind: str, day: str) -> str:
    month, date_, year = day.split("/")
    return (
        "<tr class='document-row'>"
        f"<td><a href='/Search/ViewDocumentInfo/{endorsement_id}'>{endorsement_id}</a></td>"
        f"<td>DEED</td><td>{kind}</td><td>{month}/{date_}/{year}</td>"
        f"<td><a href='/ViewVscmsDocument/ViewContent?p_endorsementId={endorsement_id}'>view</a></td>"
        "</tr>"
    )


def _list_html(*rows: str) -> str:
    return (
        "<!DOCTYPE html><html><body><table id='documents'><tbody>"
        + "".join(rows)
        + "</tbody></table></body></html>"
    )


MAP = {
    LOT: _list_html(
        _row(OUR_NEW, "DEED", "08/12/2021"),
        _row(111333, "MORTGAGE", "05/02/2019"),
        _row(OUR_OLD, "WAR DEED", "03/01/1974"),
    ),
    NEIGHBOUR: _list_html(
        _row(NEIGHBOUR_NEW, "DEED", "06/17/2016"),
        _row(555888, "MORTGAGE", "01/09/2002"),
        _row(NEIGHBOUR_OLD, "DEED", "11/04/1988"),
    ),
}

DESCRIPTION = (
    "BEGINNING AT A POINT on the easterly side of Alvine Avenue; RUNNING THENCE "
    "N 67 33 48 E, 174.89 feet; THENCE N 50 25 10 W, 45.30 feet; THENCE "
    "S 67 33 48 W, 153.64 feet; THENCE S 22 26 12 E, 40.00 feet to the point "
    "of BEGINNING."
)


def _pdf(path: Path) -> Path:
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    doc.new_page().insert_textbox(pymupdf.Rect(36, 36, 560, 760), "COVER", fontsize=10)
    doc.new_page().insert_textbox(pymupdf.Rect(36, 36, 560, 760), DESCRIPTION, fontsize=9)
    doc.new_page().insert_textbox(pymupdf.Rect(36, 36, 560, 760), "RP-5217", fontsize=10)
    doc.save(str(path))
    doc.close()
    return path


def _fetch(lot_map=None, *, fail_lots=()):
    table = MAP if lot_map is None else lot_map

    def fetch_list(block: int, lot: int) -> str:
        if lot in fail_lots:
            raise rcclerk.CloudflareBlocked(
                f"вместо списка пришла страница проверки Cloudflare — откройте в браузере: "
                f"{rcclerk.results_url(block, lot)}"
            )
        return table.get(lot, "<html><body>no documents</body></html>")

    return fetch_list


def _downloader(tmp_path, *, rung="direct", fail_ids=()):
    calls: list[str] = []

    def download(row: rcclerk.DeedRow, destination: Path):
        calls.append(row.endorsement_id)
        if row.endorsement_id in fail_ids:
            raise rcclerk.ManualStepNeeded(
                f"файл не появился в загрузках — откройте вручную: "
                f"{rcclerk.document_pdf_url(row.endorsement_id)}",
                rcclerk.document_pdf_url(row.endorsement_id),
            )
        return _pdf(destination), rung

    download.calls = calls  # type: ignore[attr-defined]
    return download


def _finder(pages=(2,)):
    def find(pdf: Path) -> deed_pages.DescriptionPages:
        return deed_pages.DescriptionPages(pages=list(pages), text=DESCRIPTION, scanned=0,
                                           from_text_layer=True)

    return find


def _collect(tmp_path, **kwargs):
    options = dict(
        block=BLOCK, lot=LOT, borough="Staten Island", folder=tmp_path / "01_описания",
        fetch_list=_fetch(), downloader=_downloader(tmp_path), page_finder=_finder(),
    )
    options.update(kwargs)
    return deed_intake.collect_deeds(**options)


# --------------------------------------------------------------- наш участок


def test_our_lot_gets_newest_and_oldest_deed(tmp_path):
    outcome = _collect(tmp_path)
    names = [record.file for record in outcome.records]
    assert names == ["OUR LOT 32 NEW.pdf", "OUR LOT 32 OLD.pdf"]
    assert outcome.ok
    records = {record.file: record for record in deed_store.read_records(outcome.folder)}
    assert records["OUR LOT 32 NEW.pdf"].endorsement_id == str(OUR_NEW)
    assert records["OUR LOT 32 OLD.pdf"].endorsement_id == str(OUR_OLD)
    assert records["OUR LOT 32 NEW.pdf"].pages == "2"


def test_whole_document_and_trimmed_copy_and_recognised_text_are_next_to_each_other(tmp_path):
    outcome = _collect(tmp_path)
    folder = outcome.folder
    assert (folder / "OUR LOT 32 NEW.pdf").is_file()
    assert (folder / "OUR LOT 32 NEW_p2.pdf").is_file()
    text_file = folder / "OUR LOT 32 NEW_text.txt"
    assert "174.89" in text_file.read_text(encoding="utf-8")
    record = deed_store.read_records(folder)[0]
    assert record.trimmed == "OUR LOT 32 NEW_p2.pdf"
    assert record.text_file == "OUR LOT 32 NEW_text.txt"
    assert record.source_url.endswith(f"p_endorsementId={OUR_NEW}")


def test_manifest_puts_our_lot_first_and_remembers_the_missing_items(tmp_path):
    outcome = _collect(tmp_path, neighbours=[NEIGHBOUR])
    manifest = deed_store.read_manifest(outcome.folder)
    assert manifest["block"] == str(BLOCK) and manifest["lot"] == str(LOT)
    roles = [item["role"] for item in manifest["items"]]
    assert roles[0] == "our"
    assert [item["file"] for item in manifest["items"]][:2] == ["OUR LOT 32 NEW.pdf", "OUR LOT 32 OLD.pdf"]


def test_deed_wins_over_a_newer_mortgage(tmp_path):
    outcome = _collect(tmp_path)
    newest = deed_store.read_records(outcome.folder)[0]
    assert newest.doc_type == "DEED"   # вид, а не слово сайта («WAR DEED» → DEED)
    assert newest.endorsement_id != "111333"  # ипотека 2019 не стала NEW


# ------------------------------------------------------------------- соседи


def test_neighbour_is_entered_by_hand_and_marked_as_neighbour(tmp_path):
    outcome = _collect(tmp_path, neighbours="30")
    record = next(item for item in outcome.records if item.role == "neighbour")
    assert record.file == "LOT 30 NEW.pdf"
    assert record.lot == str(NEIGHBOUR)
    assert record.pages == "2"


def test_neighbour_gets_only_the_newest_document(tmp_path):
    outcome = _collect(tmp_path, neighbours=[NEIGHBOUR])
    neighbour_files = [item.file for item in outcome.records if item.role == "neighbour"]
    assert neighbour_files == ["LOT 30 NEW.pdf"]


def test_old_document_of_a_neighbour_is_downloaded_only_when_asked(tmp_path):
    outcome = _collect(tmp_path, neighbours=[NEIGHBOUR], with_old_for_neighbour=True)
    neighbour_files = sorted(item.file for item in outcome.records if item.role == "neighbour")
    assert neighbour_files == ["LOT 30 NEW.pdf", "LOT 30 OLD.pdf"]


def test_several_neighbours_in_one_field(tmp_path):
    outcome = _collect(tmp_path, neighbours="30 28")
    assert [item.lot for item in outcome.records if item.role == "neighbour"] == [str(NEIGHBOUR)]
    assert any("лот 28" in item for item in outcome.missing)


# ------------------------------------------------------------------ аварии


def test_cloudflare_on_the_list_does_not_stop_the_other_lots(tmp_path):
    outcome = _collect(
        tmp_path, neighbours="30", fetch_list=_fetch(fail_lots={NEIGHBOUR}),
    )
    assert [item.file for item in outcome.records] == ["OUR LOT 32 NEW.pdf", "OUR LOT 32 OLD.pdf"]
    assert any("сосед (лот 30)" in item and "Cloudflare" in item for item in outcome.missing)


def test_manual_step_for_one_document_keeps_the_other_one(tmp_path):
    download = _downloader(tmp_path, fail_ids={str(OUR_NEW)})
    outcome = _collect(tmp_path, downloader=download)
    assert [item.file for item in outcome.records] == ["OUR LOT 32 OLD.pdf"]
    assert any("OUR LOT 32 NEW" in item and "p_endorsementId" in item for item in outcome.missing)
    assert not outcome.ok or outcome.our_records  # собранное не потеряно


def test_empty_list_page_is_reported_with_the_link(tmp_path):
    outcome = _collect(tmp_path, fetch_list=_fetch(lot_map={}))
    assert not outcome.ok
    assert any("нет строк документов" in item for item in outcome.missing)
    assert any(rcclerk.results_url(BLOCK, LOT) in item for item in outcome.missing)


def test_stop_interrupts_the_run_and_keeps_what_was_collected(tmp_path):
    calls: list[str] = []

    def should_stop() -> bool:
        calls.append("check")
        # Проверки идут перед участком и перед каждым документом: третий раз —
        # это уже перед старым документом, свежий к этому моменту лежит в папке.
        return len(calls) > 2

    with pytest.raises(deed_intake.IntakeCancelled):
        _collect(tmp_path, neighbours="30", should_stop=should_stop)
    assert (tmp_path / "01_описания" / "OUR LOT 32 NEW.pdf").is_file()


# ----------------------------------------------------- лестница и ручные шаги


def test_manual_file_is_named_by_the_same_scheme(tmp_path):
    source = _pdf(tmp_path / "download(3).pdf")
    record = deed_intake.copy_manual_pdf(
        tmp_path / "01_описания", source, role="neighbour", lot=NEIGHBOUR, block=BLOCK,
    )
    assert record.file == "LOT 30 NEW.pdf"
    assert any("вручную" in note for note in record.notes)
    assert (tmp_path / "01_описания" / "LOT 30 NEW.json").is_file()


def test_newest_pdf_from_downloads_is_adopted(tmp_path):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    _pdf(downloads / "old.pdf")
    newest = _pdf(downloads / "ViewContent.pdf")
    folder = tmp_path / "01_описания"
    record = deed_intake.adopt_downloads(
        folder, role="our", lot=LOT, block=BLOCK, downloads=downloads,
    )
    assert record is not None and record.file == "OUR LOT 32 NEW.pdf"
    assert any("ViewContent.pdf".replace(".pdf", "") in note for note in record.notes)
    assert (folder / "OUR LOT 32 NEW.pdf").is_file()
    assert newest.is_file()  # из загрузок файл не удаляется


def test_no_new_pdf_in_downloads_means_no_record(tmp_path):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    assert deed_intake.adopt_downloads(
        tmp_path / "01_описания", role="our", lot=LOT, downloads=downloads,
    ) is None


def test_folder_for_an_address_is_inside_the_project(tmp_path):
    folder = deed_intake.build_folder_for("24 Alvine Avenue, Staten Island", parent=tmp_path)
    assert folder == tmp_path / "24 Alvine Avenue, Staten Island" / "01_описания"
    assert deed_intake.build_folder_for("", parent=tmp_path) == tmp_path / "01_описания"


def test_report_lists_files_and_gaps(tmp_path):
    outcome = _collect(tmp_path, neighbours="30", fetch_list=_fetch(fail_lots={NEIGHBOUR}))
    report = outcome.report()
    assert "OUR LOT 32 NEW.pdf" in report and "ЛОТ" not in report
    assert "Чего не хватает" in report and "LOT 30" not in report
    assert "01_описания" in report


# ------------------------------------------------------------ номер лота поля


def test_lot_field_accepts_commas_spaces_and_semicolons():
    assert deed_intake.parse_lot_list("30, 28 35") == [28, 30, 35]
    assert deed_intake.parse_lot_list("30;30") == [30]
    assert deed_intake.parse_lot_list("") == []
    assert deed_intake.parse_lot_list([31, 29]) == [29, 31]


@pytest.mark.parametrize("bad", ["30, дом", "0", "-5"])
def test_lot_field_rejects_junk_loudly(bad):
    with pytest.raises(ValueError):
        deed_intake.parse_lot_list(bad)


# ------------------------------------------- страница списка: загрузка и разбор


class _Response:
    def __init__(self, payload: bytes, charset: str = ""):
        self._payload = payload
        self.headers = {"Content-Type": f"text/html; charset={charset}" if charset else "text/html"}

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_content_charset(self):  # noqa: D401 - как у http.client.HTTPMessage
        return None


def test_fallback_uses_ctrl_s_hook_and_adopts_the_saved_pdf(tmp_path):
    """После готовности браузерного PDF Ctrl+S сохраняет файл, затем лестница его берёт."""
    steps: list[str] = []
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    calls: list[str] = []

    def save_from_browser(endorsement_id):
        calls.append(str(endorsement_id))
        path = downloads / f"tsai_step0_{endorsement_id}.pdf"
        path.write_bytes(b"%PDF-1.7 deed")
        return path

    path, rung = rcclerk.download_with_fallback(
        OUR_NEW, tmp_path / "raw.pdf",
        opener=lambda request, timeout=0: _Response(b"<html>Just a moment...</html>", "text/html"),
        open_browser=lambda url: None, downloads_dir=downloads, watch_timeout=1.0, poll=0.01,
        auto_save=save_from_browser, on_step=steps.append,
    )
    assert rung == "browser" and path.read_bytes().startswith(b"%PDF-")
    assert calls == [str(OUR_NEW)]
    assert any("Ctrl+S в окне браузера" in step for step in steps)
    assert not (downloads / f"tsai_step0_{OUR_NEW}.pdf").exists()  # временный наш файл убран


def test_manual_step_mentions_ctrl_s_if_pdf_is_visible(tmp_path):
    with pytest.raises(rcclerk.ManualStepNeeded) as exc:
        rcclerk.download_with_fallback(
            OUR_NEW, tmp_path / "raw.pdf",
            opener=lambda request, timeout=0: _Response(b"<html></html>", "text/html"),
            open_browser=lambda url: None, downloads_dir=tmp_path / "downloads",
            watch_timeout=0.0, poll=0.01,
        )
    assert "Ctrl+S" in str(exc.value)


def test_collect_passes_the_safe_ctrl_s_hook_to_the_ladder(tmp_path, monkeypatch):
    calls: list[dict] = []

    def fake_ladder(endorsement_id, destination, **kwargs):
        calls.append(kwargs)
        Path(destination).write_bytes(b"%PDF-1.7 deed")
        return Path(destination), "browser"

    hook = lambda endorsement_id: None
    monkeypatch.setattr(rcclerk, "download_with_fallback", fake_ladder)
    outcome = _collect(
        tmp_path, downloader=None, auto_save=hook, downloads_dir=tmp_path / "downloads",
    )
    assert outcome.records
    assert calls and calls[0]["downloads_dir"] == tmp_path / "downloads"
    assert calls[0]["auto_save"] is hook


def test_list_page_is_read_with_the_site_encoding(monkeypatch):
    html = MAP[LOT].replace("DEED", "DEED")
    payload = html.encode("cp1252")
    text = rcclerk.fetch_list_html(
        BLOCK, LOT, opener=lambda request, timeout=0: _Response(payload, ""),
    )
    rows = rcclerk.parse_document_rows(text)
    assert [row.endorsement_id for row in rows] == [str(OUR_NEW), "111333", str(OUR_OLD)]


def test_cloudflare_challenge_instead_of_the_list_is_named_as_such():
    challenge = b"<html><body>Just a moment... checking your browser</body></html>"
    with pytest.raises(rcclerk.CloudflareBlocked):
        rcclerk.fetch_list_html(
            BLOCK, LOT, opener=lambda request, timeout=0: _Response(challenge),
        )


def test_real_list_page_keeps_its_rows_even_with_a_cloudflare_banner():
    # Страница с настоящими строками не должна считаться проверкой на бота.
    html = MAP[LOT].replace("<body>", "<body><div>Please enable JavaScript</div>")
    text = rcclerk.fetch_list_html(
        BLOCK, LOT, opener=lambda request, timeout=0: _Response(html.encode("utf-8")),
    )
    assert len(rcclerk.parse_document_rows(text)) == 3


def test_saved_page_from_docs_folder_parses_if_it_is_there():
    """Настоящая страница от заказчика: разбор обязан работать, когда файл появится."""
    saved = sorted(Path("docs").glob("*richmond*")) + sorted(Path("docs").glob("*.html"))
    if not saved:
        pytest.skip("настоящей страницы списка в docs пока нет")
    text = saved[0].read_text(encoding="utf-8", errors="replace")
    rows = rcclerk.parse_document_rows(text)
    assert rows, f"в {saved[0].name} не нашлось ни одной строки документа"
    assert all(row.endorsement_id.isdigit() for row in rows)


def test_sidecar_of_a_stored_document_is_readable_back(tmp_path):
    outcome = _collect(tmp_path)
    raw = json.loads((outcome.folder / "OUR LOT 32 NEW.json").read_text(encoding="utf-8"))
    assert raw["endorsement_id"] == str(OUR_NEW)
    assert raw["trimmed"] == "OUR LOT 32 NEW_p2.pdf"
    assert raw["text_file"] == "OUR LOT 32 NEW_text.txt"


def test_collect_passes_selected_dpi_to_default_page_search(tmp_path, monkeypatch):
    import deed_pages

    seen = []

    def fake_find(pdf, *, dpi, progress):
        seen.append((Path(pdf).name, dpi, callable(progress)))
        return deed_pages.DescriptionPages()

    monkeypatch.setattr(deed_pages, "find_description_pages", fake_find)
    outcome = _collect(
        tmp_path, page_finder=None, ocr_dpi=72, progress=lambda _line: None,
    )
    assert outcome.records
    assert len(seen) == 2
    assert all(dpi == 72 and has_progress for _name, dpi, has_progress in seen)


def test_old_deed_and_fresh_mortgage_flow_saves_mortgage_as_new_and_deed_as_old(tmp_path):
    """Сценарий заказчика целиком: Deed 1985 и Mortgage 2025 → OUR NEW — ипотека, OUR OLD — Deed."""
    table = {LOT: _list_html(
        _row(111333, "MORTGAGE", "03/05/2025"),
        _row(OUR_OLD, "DEED", "07/10/1985"),
    )}
    outcome = _collect(tmp_path, fetch_list=_fetch(table))
    records = {record.file: record for record in deed_store.read_records(outcome.folder)}
    assert set(records) == {"OUR LOT 32 NEW.pdf", "OUR LOT 32 OLD.pdf"}
    assert records["OUR LOT 32 NEW.pdf"].endorsement_id == "111333"
    assert records["OUR LOT 32 NEW.pdf"].doc_type == "MORTGAGE"
    assert records["OUR LOT 32 OLD.pdf"].endorsement_id == str(OUR_OLD)


def test_portal_values_are_kept_per_file_and_survive_step1_read(tmp_path):
    from types import SimpleNamespace
    import file_metadata
    import lot_identity
    from llm_parser import ParseOutcome, parse_with_regex, ASPENWALL_SAMPLES

    portal = SimpleNamespace(block=str(BLOCK), lot=str(LOT), borough="Staten Island",
                             neighbours=[str(NEIGHBOUR)], details_source="vision")
    identities = file_metadata.intake_observations(BLOCK, LOT, "Staten Island", [NEIGHBOUR],
                                                     "Example address", ("Example address", portal))
    result = _collect(tmp_path, borough="Staten Island", neighbours=[NEIGHBOUR], identity_by_lot=identities)
    records = {r.file: r for r in deed_store.read_records(result.folder)}
    ours, neighbor = records["OUR LOT 32 NEW.pdf"], records["LOT 30 NEW.pdf"]
    assert ours.borough == neighbor.borough == "Staten Island"
    assert any(e["source"] == "portal" and e["method"] == "vision" for e in ours.identity["block"])
    assert any(e["value"] == "32" and e["source"] == "portal" for e in ours.identity["lot"])
    assert any(e["value"] == "30" and "vision map" in e["method"] for e in neighbor.identity["lot"] if e["source"] == "portal")
    assert all(e["value"] != "32" for e in neighbor.identity["lot"])
    # Step 1 parses the trimmed copy; it updates the original passport and manifest.
    model, _ = parse_with_regex(ASPENWALL_SAMPLES["Aspenwall St — 365' (Сосед слева)"])
    model = model.model_copy(update={"block_number": "1234", "borough_or_city": "Queens"})
    bound = lot_identity.bind_outcome(ParseOutcome(data=model, source="llm-vision"), result.folder / neighbor.trimmed)
    passport = json.loads((result.folder / neighbor.sidecar_name()).read_text(encoding="utf-8"))
    assert passport["identity"] == bound.data._source_identity
    assert any(e["value"] == "1234" and e["source"] == "ai" for e in passport["identity"]["block"])
    assert any(e["value"] == str(BLOCK) and e["source"] == "portal" for e in passport["identity"]["block"])
    assert any(e["value"] == "Queens" and e["source"] == "ai" for e in passport["identity"]["borough"])
    manifest = deed_store.read_manifest(result.folder)
    assert next(item for item in manifest["items"] if item["file"] == neighbor.file)["identity"] == passport["identity"]


@pytest.mark.parametrize("borough", ["", "Staten Island Queens", None])
def test_unknown_borough_is_rejected_before_any_side_effect(tmp_path, borough):
    folder = tmp_path / "never-created"
    calls = []
    with pytest.raises(ValueError, match="Staten Island"):
        deed_intake.collect_deeds(block=BLOCK, lot=LOT, borough=borough, folder=folder,
                                  fetch_list=lambda *args: calls.append(args),
                                  acris_fetch_page=lambda *args: calls.append(args))
    assert not calls and not folder.exists()


@pytest.mark.parametrize("borough", ["Queens", "Brooklyn", "Bronx", "Manhattan"])
def test_other_boroughs_go_to_acris_never_to_richmond(tmp_path, borough):
    """Районы ACRIS не должны попадать на сайт Клерка Ричмонда — у них свой."""
    richmond_calls = []
    outcome = deed_intake.collect_deeds(
        block=BLOCK, lot=LOT, borough=borough, folder=tmp_path,
        fetch_list=lambda *args: richmond_calls.append(args),
        acris_fetch_page=lambda code, block, lot, page: "<html><body>нет строк</body></html>",
        acris_downloader=lambda row, dest: pytest.fail("качать нечего"),
        page_finder=lambda pdf: pytest.fail("страниц нет"),
    )
    assert richmond_calls == []
    assert not outcome.ok
    assert any("ACRIS" in item for item in outcome.missing)


def test_full_pdf_only_path_skips_legacy_search_and_cropping(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("legacy page search or crop called")
    monkeypatch.setattr(deed_pages, "trim_pdf", forbidden)
    outcome = _collect(tmp_path, neighbours=[NEIGHBOUR], search_pages=False, page_finder=forbidden)
    assert outcome.ok and not outcome.missing
    assert len(outcome.records) == 3
    for record in outcome.records:
        assert not record.pages and not record.trimmed
        with pytest.importorskip("pymupdf").open(outcome.folder / record.file) as pdf:
            assert len(pdf) == 3
    assert not list(outcome.folder.glob("*_p*.pdf"))


def test_on_record_fires_as_each_document_is_downloaded(tmp_path):
    seen = []
    outcome = _collect(tmp_path, neighbours=[NEIGHBOUR], on_record=lambda rec: seen.append(rec.file))
    assert seen == [record.file for record in outcome.records]
    assert seen[:2] == ["OUR LOT 32 NEW.pdf", "OUR LOT 32 OLD.pdf"]
