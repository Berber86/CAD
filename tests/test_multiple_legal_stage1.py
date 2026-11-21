"""Stage-one fixtures and known legacy failures; no Jev or AutoCAD is called.

The synthetic cases have known answers.  The real PDF is deliberately retained
with the owner's permission; no OCR transcript is stored here.  Xfails document
known legacy failures rather than pretending the new pipeline exists.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pymupdf
import pytest

import deed_pages
import llm_parser

REAL_PDF = Path(__file__).parent / "data" / "multiple_legal" / "OUR 6 NEW.pdf"
REAL_PDF_SHA256 = "c4a5a43f129419d51b43fde4f77709e9fdfb5897166a94c88cd9e216dbfca777"

from tools.make_legal_test_pdfs import SCENARIOS, write_text_pdf


def test_authorized_real_fixture_is_original_five_page_scan():
    assert hashlib.sha256(REAL_PDF.read_bytes()).hexdigest() == REAL_PDF_SHA256
    with pymupdf.open(REAL_PDF) as doc:
        assert len(doc) == 5
        assert all(not page.get_text("text").strip() for page in doc)
        assert all(page.get_images(full=True) for page in doc)


@pytest.mark.parametrize("name", SCENARIOS)
def test_synthetic_scenario_pdf_and_page_ground_truth(name, tmp_path):
    case = SCENARIOS[name]
    path = tmp_path / f"{name}.pdf"
    write_text_pdf(path, case["pages"])
    with pymupdf.open(path) as doc:
        assert len(doc) == len(case["pages"])
        assert all(doc[p - 1].get_text().strip() for positions in case["lots"].values() for p in positions)
    assert all(1 <= p <= len(case["pages"]) for positions in case["lots"].values() for p in positions)


def test_synthetic_generator_writes_expected_pdfs_without_overwriting(tmp_path, monkeypatch, capsys):
    import json
    import sys
    from tools import make_legal_test_pdfs

    output = tmp_path / "synthetic"
    monkeypatch.setattr(sys, "argv", ["make_legal_test_pdfs.py", str(output)])
    assert make_legal_test_pdfs.main() == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["same_page_and_continuation"]["lots"] == {"5": [1], "6": [1, 2]}
    assert manifest["no_description"]["positive_pages"] == []
    for name in SCENARIOS:
        with pymupdf.open(output / f"{name}.pdf") as doc:
            assert len(doc) == len(SCENARIOS[name]["pages"])
    with pytest.raises(SystemExit):
        make_legal_test_pdfs.main()


def test_legacy_pipeline_baseline_for_one_synthetic_description(tmp_path):
    case = SCENARIOS["single"]
    path = tmp_path / "single.pdf"
    write_text_pdf(path, case["pages"])
    found = deed_pages.find_description_pages(path)
    assert found.pages == [1]
    parsed, _ = llm_parser.parse_with_regex(found.text, auto_repair_typos=False)
    assert parsed.lot_number == "5" and len(parsed.segments) == 4


def test_legacy_step0_does_not_yet_include_second_parcel_continuation(tmp_path):
    case = SCENARIOS["same_page_and_continuation"]
    path = tmp_path / "OUR 6 NEW.pdf"
    write_text_pdf(path, case["pages"])
    result = deed_pages.find_description_pages(path)
    assert result.pages, "Fixture should be recognisable by the current page finder"
    if result.pages != case["lots"]["6"]:
        pytest.xfail("Known legacy limitation: Step 0 stops at the first complete description")
    assert result.pages == case["lots"]["6"]


def test_legacy_parser_does_not_yet_select_second_lot_from_whole_pages():
    case = SCENARIOS["same_page_and_continuation"]
    parsed, _ = llm_parser.parse_with_regex("\n".join(case["pages"]), auto_repair_typos=False)
    assert parsed.segments, "Fixture should produce boundary segments"
    if parsed.lot_number != "6":
        pytest.xfail("Known legacy limitation: local parser selects the first lot")
    assert parsed.lot_number == "6"
    # The exact geometry acceptance test and tolerances will be chosen later.
