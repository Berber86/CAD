"""
Однозначная опечатка похожих цифр исправляется сама, без решения человека.

Правило (выбор пользователя): если ровно одна замена цифры (3↔8, 6↔9 и т.п.) в румбе или длине
стороны закрывает контур (невязка ≤ 0.5 ft), программа правит её, даже при включённой настройке
«Спрашивать при сомнении». Непохожие цифры и неоднозначные случаи по-прежнему не правятся.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import llm_parser  # noqa: E402
from description_check import STATUS_OK, check_description  # noqa: E402
from llm_parser import parse_legal_description_full  # noqa: E402

# Геометрия Doty Avenue: четыре разных румба, поэтому одна верная замена цифры однозначна.
# (Прямоугольник не годится: там симметричная замена противоположной стороны тоже замыкает контур.)
RECT = """ALL that certain plot of land, situate in the Borough of Richmond, bounded and described as follows:
BEGINNING at a point on the southwesterly side of Doty Avenue (formerly known as Surf Avenue), distant 74.96 feet U.S. Standard (75.00 feet Deed) northwesterly from the corner formed by the intersection of the southwesterly side of Doty Avenue with the northwesterly side of Father Capodanno Boulevard, running
THENCE along the southwesterly side of Doty Avenue, North 31 degrees 46 minutes 56 seconds west 25.00 feet; running
THENCE South 56 degrees 48 minutes 40 seconds west 115.15 feet to land now or formerly of Emily Rodgers; running
THENCE along said last mentioned land south 42 degrees 03 minutes 54 seconds east 25.41 feet U.S. Standard (25.00 feet Deed) to land now or formerly of the City of New York; running
THENCE along the northwesterly line of said last mentioned land, North 56 degrees 57 minutes 43 seconds East 110.68 feet to the point or place of BEGINNING."""


def _ask_on_doubt_true(text: str):
    return parse_legal_description_full(text=text, use_llm=False, ask_on_doubt=True)


def test_clean_rectangle_is_ok_and_untouched():
    out = _ask_on_doubt_true(RECT)
    assert out.check.status == STATUS_OK
    assert out.data.segments[0].degrees == 31
    assert not any("опечатк" in w for w in out.warnings)


def test_bearing_8_to_3_that_closes_contour_is_fixed_without_asking():
    """Пример пользователя: N 81°46'56" W → N 31°46'56" W, контур замыкается."""
    bad = RECT.replace("North 31 degrees 46 minutes 56 seconds west", "North 81 degrees 46 minutes 56 seconds west")
    assert bad != RECT
    out = _ask_on_doubt_true(bad)
    assert out.check.status == STATUS_OK
    assert out.data.segments[0].degrees == 31
    assert out.data.segments[0].minutes == 46
    assert out.typo_suspicions == []
    notes = [w for w in out.warnings if "очевидная опечатка" in w]
    assert notes and "8→3" in notes[0] and "N 81°46'56\" W → N 31°46'56\" W" in notes[0]


def test_length_5_to_8_that_closes_contour_is_fixed_without_asking():
    bad = RECT.replace("North 31 degrees 46 minutes 56 seconds west 25.00 feet", "North 31 degrees 46 minutes 56 seconds west 28.00 feet")
    assert bad != RECT
    out = _ask_on_doubt_true(bad)
    assert out.check.status == STATUS_OK
    assert out.data.segments[0].distance_ft == 25.0
    assert any("в длине" in w and "очевидная опечатка" in w and "28.00' → 25.00'" in w for w in out.warnings)


def test_unlike_digits_are_never_fixed_automatically():
    """3 → 6 не похожие цифры: даже если замена замкнула бы контур, правка не делается."""
    bad = RECT.replace("North 31 degrees 46 minutes 56 seconds west", "North 61 degrees 46 minutes 56 seconds west")
    assert bad != RECT
    out = _ask_on_doubt_true(bad)
    assert out.data.segments[0].degrees == 61
    assert check_description(out.data, bad).status != STATUS_OK


def test_consensus_of_two_llm_passes_blocks_the_fix():
    clean, _ = llm_parser.parse_with_regex(RECT, auto_repair_typos=False)
    segs = list(clean.segments)
    segs[0] = llm_parser._mutate_segment_bearing(segs[0], degrees=81)
    assert segs[0] is not None
    assert llm_parser._scan_clear_lookalike_typo(segs, unanimous_llm_passes=2) is None
    found = llm_parser._scan_clear_lookalike_typo(segs, unanimous_llm_passes=0)
    assert found is not None
    assert found.segment_index == 1
    assert found.repaired_segments is not None
    assert found.repaired_segments[0].degrees == 31


def test_contour_that_is_already_ok_is_not_touched():
    model, _ = llm_parser.parse_with_regex(RECT, auto_repair_typos=False)
    assert llm_parser._scan_clear_lookalike_typo(model.segments) is None


def test_ambiguous_109_and_100_do_not_get_auto_fixed_even_when_not_asking():
    base = llm_parser.ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"]
    changed = base.replace('S 14°39\'40" E, 100.00 feet', 'S 14°39\'40" E, 109.00 feet')
    for ask in (True, False):
        outcome = parse_legal_description_full(text=changed, use_llm=False, ask_on_doubt=ask)
        assert outcome.data.segments[0].distance_ft == 109.0
        assert outcome.data.segments[2].distance_ft == 100.0
        assert not any(seg._auto_corrections for seg in outcome.data.segments)
        assert outcome.check.closure_ft > 8.9
