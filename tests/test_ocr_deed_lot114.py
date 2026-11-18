"""
Участок OUR LOT 114 (Doty Avenue, Staten Island; скан 1991 года).

Линия участка «кривилась», потому что локальный OCR портил текст описания:
буквы вместо цифр в длинах («1l5.15», «lio.68»), искажённые слова («degrecs», «eide»),
строки-помехи с полей скана («7331»), а «Described premises» принималось за улицу.
Фикстура tests/data/lot114_ocr_p1.txt — текст OCR 400 dpi без правок.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from acad_builder import build_polygon_points, closure_error  # noqa: E402
from llm_parser import _normalize_legal_text, parse_with_regex  # noqa: E402

OCR_TEXT = (ROOT / "tests" / "data" / "lot114_ocr_p1.txt").read_text(encoding="utf-8")


def test_ocr_text_gives_four_sides_with_bearings_and_true_lengths():
    model, _ = parse_with_regex(OCR_TEXT, auto_repair_typos=True)
    assert [(s.ns, s.ew, s.degrees, s.distance_ft) for s in model.segments] == [
        ("N", "W", 31, 25.0),
        ("S", "W", 56, 115.15),
        ("S", "E", 42, 25.41),
        ("N", "E", 56, 110.68),
    ]
    assert all(s.has_bearing for s in model.segments)


def test_ocr_text_closes_the_lot():
    model, _ = parse_with_regex(OCR_TEXT, auto_repair_typos=True)
    assert closure_error(build_polygon_points(model.segments)).distance < 1.0


def test_ocr_text_streets_are_doty_and_father_capodanno():
    model, _ = parse_with_regex(OCR_TEXT, auto_repair_typos=True)
    assert model.main_street == "Doty Avenue"
    assert model.cross_street == "Father Capodanno Boulevard"
    assert model.main_street_former == "Surf Avenue"
    assert model.main_street_note == "(FORMERLY KNOWN AS SURF AVENUE)"


@pytest.mark.parametrize(
    ("raw", "fixed"),
    [
        ("1l5.15 feet", "115.15 feet"),
        ("lio.68 feet", "110.68 feet"),
        ("North 31 degrecs 46 minutes", "North 31 degrees 46 minutes"),
        ("southwesterly eide of Doty", "southwesterly side of Doty"),
        ("North 56 degrees 57 minutes 43 seconds", "North 56 degrees 57 minutes 43 seconds"),
    ],
)
def test_ocr_number_and_word_errors_are_repaired(raw, fixed):
    assert _normalize_legal_text(raw) == fixed


def test_margin_line_numbers_do_not_break_a_bearing():
    raw = "North 31 degrees 46 minutes\n7331\n56 seconds west 25.00 feet; running"
    assert "minutes\n56 seconds" in _normalize_legal_text(raw)


def test_described_premises_is_not_taken_for_a_street():
    text = (
        "BEGINNING at a point on the southwesterly side of Doty Avenue, distant 74.96 feet "
        "U.S. Standard (75.00 feet Deed) northwesterly from the corner formed by the intersection "
        "of the southwesterly side of Doty Avenue with the northwesterly side of Father Capodanno "
        "Boulevard, running THENCE North 31 degrees 46 minutes 56 seconds west 25.00 feet; running "
        "THENCE South 56 degrees 48 minutes 40 seconds west 115.15 feet to land; running "
        "THENCE South 42 degrees 03 minutes 54 seconds east 25.41 feet U.S. Standard (25.00 feet "
        "Deed) to land; running THENCE North 56 degrees 57 minutes 43 seconds East 110.68 feet to "
        "the point or place of BEGINNING. TOGETHER with all right, title and interest in and to any "
        "streets and roads abutting the above described premises to the center lines thereof."
    )
    model, _ = parse_with_regex(text, auto_repair_typos=True)
    assert model.main_street == "Doty Avenue"
    assert model.distance_to_corner_ft == pytest.approx(74.96)


def test_distance_to_corner_skips_us_standard_and_deed_remark():
    text = (
        "BEGINNING at a point on the southwesterly side of Doty Avenue, distant 74.96 feet "
        "U.S. Standard (75.00 feet Deed) northwesterly from the corner formed by the intersection "
        "of the southwesterly side of Doty Avenue with the northwesterly side of Father Capodanno "
        "Boulevard, running THENCE North 31 degrees 46 minutes 56 seconds west 25.00 feet; running "
        "THENCE South 56 degrees 48 minutes 40 seconds west 115.15 feet to land; running "
        "THENCE South 42 degrees 03 minutes 54 seconds east 25.41 feet to land; running "
        "THENCE North 56 degrees 57 minutes 43 seconds East 110.68 feet to the point of BEGINNING."
    )
    model, _ = parse_with_regex(text, auto_repair_typos=True)
    assert model.distance_to_corner_ft == pytest.approx(74.96)
