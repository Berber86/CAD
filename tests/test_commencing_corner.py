"""«COMMENCING at the intersection …» — точка начала на углу; запятая вместо точки в длинах.

Данные: tests/data/lot51_commencing_corner.txt — описание OUR LOT 51 (Mosley Avenue / Bovanizer Street),
перенесено со скриншота. Контур замыкается точно: 108.06 / 46.79 на противоположных сторонах.
"""

from pathlib import Path

import pytest

import llm_parser
import ocr_numbers
from description_check import _starts_at_corner, check_description

DATA = Path(__file__).resolve().parent / "data"


def _lot51() -> str:
    return (DATA / "lot51_commencing_corner.txt").read_text(encoding="utf-8")


def test_comma_decimal_in_length_is_read_as_point():
    model, _ = llm_parser.parse_with_regex(_lot51(), auto_repair_typos=False)
    lengths = [round(seg.distance_ft, 2) for seg in model.segments]
    assert lengths == [108.06, 46.79, 108.06, 46.79]


def test_lot51_closes_and_has_no_warnings():
    text = _lot51()
    model, _ = llm_parser.parse_with_regex(text, auto_repair_typos=False)
    check = check_description(model, text)
    assert check.status == "ok"
    assert check.closure_ft is not None and check.closure_ft < 0.01
    assert check.issues == []


def test_commencing_at_intersection_is_a_corner_start():
    assert _starts_at_corner(_lot51())
    assert _starts_at_corner("COMMENCING at the corner formed by A Street and B Avenue; running thence")


def test_separate_beginning_with_distance_still_needs_the_corner():
    """Если есть свой «BEGINNING at … distant …», COMMENCING на углу не отменяет расстояние до угла."""
    text = (
        "COMMENCING at the intersection of A Street and B Avenue; running thence North 10 degrees "
        "East 50.00 feet; BEGINNING at a point distant 30.00 feet from the corner"
    )
    assert not _starts_at_corner(text)


def test_commencing_without_corner_word_is_not_a_corner_start():
    assert not _starts_at_corner("COMMENCING at a point 100 feet north of the line; running thence")


@pytest.mark.parametrize(
    "raw, fixed",
    [
        ("Street,108,06 feet to a point", "Street,108.06 feet to a point"),  # запятая после слова
        ("46,79 feet", "46.79 feet"),
        ("1l5,15 feet", "115.15 feet"),                                     # буквы и запятая вместе
    ],
)
def test_comma_before_two_digits_before_feet_is_a_point(raw, fixed):
    assert ocr_numbers.fix_text(raw) == fixed


@pytest.mark.parametrize(
    "raw",
    [
        "consideration of $234,400.00 dollars",   # тысячи — три цифры после запятой
        "1,108 feet",                              # тысячи без дробной части
        "1,108,06 feet",                           # тысячи, за которыми дробь — не трогаем
        "Lot 12, 34 feet",                         # пробел после запятой — не число
        "108,06 minutes",                          # не длина: единица не feet
    ],
)
def test_thousands_and_other_commas_are_left_alone(raw):
    assert ocr_numbers.fix_text(raw) == raw
