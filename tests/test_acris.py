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
