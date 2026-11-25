"""ACRIS (Шаг 0): построение запроса и разбор настоящих страниц.

Сеть не используется: разбор проверяется на странице результатов, которую
пользователь сохранил из своего браузера (Manhattan, block 731, lot 53).
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

import acris
import rcclerk

DATA = Path(__file__).resolve().parent / "data"


@pytest.fixture(scope="module")
def results_html() -> str:
    return (DATA / "acris_results_manhattan_731_53.html").read_text(encoding="utf-8")


def test_borough_codes_and_names():
    assert acris.borough_code(1) == 1
    assert acris.borough_code("Manhattan") == 1
    assert acris.borough_code("BRONX") == 2
    assert acris.borough_code("Brooklyn") == 3
    assert acris.borough_code("Kings County") == 3
    assert acris.borough_code("Queens") == 4
    with pytest.raises(ValueError):
        acris.borough_code("Staten Island")  # это rcclerk, не ACRIS
    with pytest.raises(ValueError):
        acris.borough_code("Richmond County")
    with pytest.raises(ValueError):
        acris.borough_code("Westchester")


def test_search_post_data_asks_for_99_rows_at_once():
    data = acris.search_post_data("Brooklyn", 731, 53)
    assert data["hid_borough"] == "3"
    assert data["hid_borough_name"] == "BROOKLYN / KINGS"
    assert data["hid_block"] == "731"
    assert data["hid_lot"] == "53"
    # Главное против ручного клика: все строки сразу, максимум списка — 99.
    assert data["hid_max_rows"] == "99"
    assert data["hid_page"] == "1"
    assert data["hid_SearchType"] == "BBL"
    assert acris.search_post_data(4, 100, 5, page=2)["hid_page"] == "2"


def test_parse_real_results_page(results_html):
    page = acris.parse_result_page(results_html)
    assert page.borough_name == "MANHATTAN / NEW YORK"
    assert page.block == 731
    assert page.lot == 53
    assert (page.records_first, page.records_last) == (1, 6)
    assert page.has_next is False
    assert len(page.rows) == 6

    first = page.rows[0]
    assert first.crfn == "2026000324679"
    assert first.doc_type == "MORTGAGE"
    assert first.kind == rcclerk.KIND_MORTGAGE
    assert first.pages == 31
    assert first.doc_date == date(2026, 11, 5)
    assert first.recorded == datetime(2026, 11, 17, 10, 22, 4)
    assert first.party1 == "THE CHURCH OF SAINT MICHAEL"
    assert first.party2 == "MADISON INVESTOR I, LLC"
    assert first.amount == "58,538,870"
    kinds = [row.kind for row in page.rows]
    assert rcclerk.KIND_MORTGAGE in kinds


def test_search_form_fixture_has_expected_fields():
    form = (DATA / "acris_search_form.html").read_text(encoding="utf-8")
    for field_name in ("borough", "edt_block", "edt_lot", "edt_unit", "cmb_date", "Submit2"):
        assert f'name="{field_name}"' in form
    for code in ("1", "2", "3", "4", "5"):
        assert f'<option value="{code}">' in form


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload
        self.headers = self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_content_charset(self):
        return "utf-8"

    def read(self):
        return self._payload


def test_fetch_blocked_page_raises_acris_blocked():
    blocked = b"<html><title>ACRIS Bandwidth Notice</title>Further access to ACRIS is denied"
    with pytest.raises(acris.AcrisBlocked):
        acris.fetch_results_html("Queens", 100, 5, opener=lambda req, timeout: _FakeResponse(blocked))


def test_fetch_with_fake_opener_returns_html(results_html):
    seen = {}

    def opener(request, timeout):
        seen["url"] = request.full_url
        seen["body"] = request.data.decode("utf-8")
        return _FakeResponse(results_html.encode("utf-8"))

    html = acris.fetch_results_html("Manhattan", 731, 53, opener=opener)
    assert "hid_max_rows=99" in seen["body"]
    assert "hid_borough=1" in seen["body"]
    assert acris.parse_result_rows(html)[0].crfn == "2026000324679"


_MASTER_JSON = b'[{"crfn": "2024000265906", "document_id": "2024101000085001"}]'


def test_search_form_url_is_browser_warmup_deep_link():
    # Ссылка прогрева сессии для читалки: поиск по BBL прямо в браузере.
    assert acris.search_form_url(1, 731, 53) == (
        "https://a836-acris.nyc.gov/DS/DocumentSearch/BBL?borough=1&block=731&lot=53"
    )
    assert acris.search_form_url("Queens", "3123", "40").endswith(
        "BBL?borough=4&block=3123&lot=40")
    with pytest.raises(ValueError):
        acris.search_form_url("Staten Island", 100, 1)


def test_document_image_url():
    assert acris.document_image_url("2024101000085001") == (
        "https://a836-acris.nyc.gov/DS/DocumentSearch/DocumentImageView"
        "?doc_id=2024101000085001"
    )


def test_fetch_document_id_via_open_data():
    seen = {}

    def opener(request, timeout):
        seen["url"] = request.full_url
        return _FakeResponse(_MASTER_JSON)

    assert acris.fetch_document_id("2024000265906", opener=opener) == "2024101000085001"
    assert "crfn" in seen["url"]


def test_attach_document_ids_to_real_rows(results_html):
    rows = acris.parse_result_rows(results_html)

    def opener(request, timeout):
        return _FakeResponse(_MASTER_JSON)

    acris.attach_document_ids(rows, opener=opener)
    deed = [r for r in rows if r.crfn == "2024000265906"]
    assert len(deed) == 1
    assert deed[0].document_id == "2024101000085001"
    assert deed[0].image_url.endswith("doc_id=2024101000085001")
    # Остальных CRFN в фейковом Master нет — честно остаются без id.
    assert all(r.document_id == "" for r in rows if r.crfn != "2024000265906")


def test_image_view_fixture_points_to_vtu_with_doc_id():
    page = (DATA / "acris_document_image_view.html").read_text(encoding="utf-8")
    assert "2024101000085001" in page  # doc_id в адресе обёртки документа
    assert 'iframe name="mainframe"' in page  # внутри — читалка Acordex VTU


def test_live_header_spelling_still_gives_dates(results_html):
    """Живой POST может писать заголовки иначе («Recorded/Filed», «DocDate»)."""
    variant = results_html.replace("Recorded / Filed", "Recorded/Filed").replace(
        "Doc Date", "DocDate")
    page = acris.parse_result_page(variant)
    assert len(page.rows) == 6
    assert page.rows[0].doc_date == date(2026, 11, 5)
    assert page.rows[0].recorded == datetime(2026, 11, 17, 10, 22, 4)
    assert page.rows[0].raw.startswith("DET IMG") or page.rows[0].raw


def test_parse_date_formats():
    assert acris._parse_date("11/5/2026") == date(2026, 11, 5)
    assert acris._parse_date("2026-11-05") == date(2026, 11, 5)
    assert acris._parse_date("05.11.2026") == date(2026, 11, 5)
    assert acris._parse_date("Nov 5, 2026") == date(2026, 11, 5)
    assert acris._parse_date("5 November 2026") == date(2026, 11, 5)
    assert acris._parse_date("без даты") is None


def test_open_data_retries_with_certifi_on_cert_failure(monkeypatch):
    import ssl
    import urllib.error
    import urllib.request

    attempts = {"n": 0}

    def flaky(request, timeout=None, context=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise urllib.error.URLError(ssl.SSLCertVerificationError(
                "certificate verify failed: unable to get local issuer certificate"))
        assert context is not None  # повтор — уже с certifi CA
        return _FakeResponse(_MASTER_JSON)

    monkeypatch.setattr(urllib.request, "urlopen", flaky)
    assert acris.fetch_document_id("2024000265906") == "2024101000085001"
    assert attempts["n"] == 2


def test_sort_widget_without_tbody_keeps_doc_date():
    """Виджет сортировки в заголовке может быть и без <tbody> — даты целы."""
    html = (
        "<html><body>Records 1 - 1 <table><tr>"
        "<td>CRFN</td><td>Lot</td><td>Partial</td>"
        '<td><table><tr><td><font>Doc Date</font></td>'
        '<td><img src="s.png"></td></tr></table></td>'
        "<td>Recorded/Filed</td><td>Document Type</td><td>Pages</td></tr>"
        "<tr><td>2024000000001</td><td>53</td><td>ENTIRE LOT</td><td>9/11/2024</td>"
        "<td>10/10/2024 3:00:18 PM</td><td>DEED</td><td>8</td></tr>"
        "</table></body></html>"
    )
    page = acris.parse_result_page(html)
    assert len(page.rows) == 1
    assert page.rows[0].doc_date == date(2024, 9, 11)
    assert page.rows[0].recorded == datetime(2024, 10, 10, 15, 0, 18)
