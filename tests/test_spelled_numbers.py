"""
Числа прописью перед «feet» и составные румбы через пробел (OCR).

LOT 72: стороны записаны как «on hundred feet» (OCR съел «one»), угол улицы — «north westerly»
через пробел. Без этого участок не строился, хотя текст был читаем.
"""
from __future__ import annotations

import pytest

import llm_parser as L

# Фрагмент OCR страницы 1 «LOT 72 NEW_p2» (400 dpi), без реквизитов сторон.
LOT72_OCR = (
    "All that certain plot, piece or parcel of land, with the buildings and improvements "
    "thereon erected, situate, lying and being in the Borough and County of Richmond, City "
    "and State of New York, bounded and described as follows: "
    "BEGINNING at a point on the north westerly side of Stafford Avenue as laid out to a "
    "width of 60 be distant 241.00 feet south westerly from the corner formed by the "
    "intersection of the north westerly side of Stafford Avenue and the south westerly side of "
    "Carlton Boulevard; "
    "RUNNING THENCE North 56° 36 minutes 12 seconds west on hundred feet to a point; "
    "RUNNING THENCE South 33° 23 minutes 48 seconds west 40 feet to a point; "
    "RUNNING THENCE South 56° 36 minutes 12 seconds east hundred feet to the north "
    "westerly side of Stafford Avenue; "
    "RUNNING THENCE North 33° 23 minutes 48 seconds east along the northwesterly "
    "Side of Stafford Avenue 40 feet to the point or place of BEGINNING."
)


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("hundred feet", 100),
        ("on hundred feet", 100),          # OCR потерял «one»
        ("one hundred feet", 100),
        ("two hundred forty-one feet", 241),
        ("one hundred and five feet", 105),
        ("forty feet", 40),
        ("twenty-five feet", 25),
    ],
)
def test_spelled_numbers_become_digits_with_a_note(phrase, expected):
    text, notes = L._spell_numbers_before_feet(f"RUNNING THENCE {phrase} to a point")
    assert f"{expected} feet" in text
    assert len(notes) == 1 and "сверьте с документом" in notes[0]


def test_numbers_with_digits_are_left_alone():
    text, notes = L._spell_numbers_before_feet("THENCE 100.00 feet to a point")
    assert text == "THENCE 100.00 feet to a point"
    assert notes == []


def test_lot72_ocr_text_gives_four_sides_and_the_corner_distance():
    model, warnings = L.parse_with_regex(LOT72_OCR)
    assert [seg.distance_ft for seg in model.segments] == [100.0, 40.0, 100.0, 40.0]
    assert model.distance_to_corner_ft == 241.0
    assert model.main_street == "Stafford Avenue"
    assert model.cross_street == "Carlton Boulevard"
    assert not any("расстояние до угла не распознано" in w for w in warnings)
    # Каждое число прописью названо в замечаниях, чтобы его проверили по документу.
    assert sum("прописью" in w for w in warnings) == 2
    # «north westerly» через пробел склеивается: без этого угол улицы не находился бы.
    assert model.cross_street == "Carlton Boulevard"

