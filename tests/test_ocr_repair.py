"""
Починка хаотичного OCR-текста (OUR LOT 114, 3.56):

* слова юридического текста и числа в DMS-румбах;
* штамп на полях (REEL / PAGE, строки из цифр);
* слияние двух распознаваний одной страницы строка за строкой;
* проверка: сторона без румба никогда не даёт «ok»; арбитр «по Deed» — только предупреждение.

Реальный документ: tests/data/lot114_p1.pdf (нужен rapidocr; без него пропускается),
хаотичный кеш Шага 0: tests/data/lot114_cache_chaos.txt.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import llm_parser  # noqa: E402
import ocr_pages  # noqa: E402
from description_check import (  # noqa: E402
    STATUS_FAILED,
    STATUS_OK,
    STATUS_WARNING,
    check_description,
    standard_deed_pairs,
)
from llm_parser import parse_with_regex  # noqa: E402

DATA = ROOT / "tests" / "data"

FULL_LINES = [
    "ALL THAT CERTAIN plot, piece or parcel of land. BEGINNING at a point on the northwesterly side of Stafford Avenue,",
    "distant 161.00 feet southwesterly from the corner formed by the intersection of the northwesterly side of Stafford Avenue",
    "and the southwesterly side of Carlton Boulevard; RUNNING THENCE North 56 degrees 36 minutes 12 seconds West 100.00 feet",
    "to a point; THENCE South 33 degrees 23 minutes 48 seconds West 40.00 feet to a point;",
    "THENCE South 56 degrees 36 minutes 12 seconds East 100.00 feet to a point;",
    "THENCE North 33 degrees 23 minutes 48 seconds East 40.00 feet to the point or place of BEGINNING.",
]


# ------------------------------------------------------------------ слова и числа

@pytest.mark.parametrize(
    "garbled, fixed",
    [
        ("North 3l degrecs 46 ainutes", "North 31 degrees 46 minutes"),
        ("56 seconds wcat 25.00 feet", "56 seconds west 25.00 feet"),
        ("thence along said last mentioned land and snid", "thence along said last mentioned land and said"),
        ("Bouth 42 degrees", "South 42 degrees"),
        ("Doty Avcnue", "Doty Avenue"),
        ("East llo.68 feet", "East 110.68 feet"),
    ],
)
def test_common_ocr_garbage_is_repaired(garbled, fixed):
    assert llm_parser._normalize_legal_text(garbled).strip() == fixed


def test_vocabulary_typo_of_one_letter_is_fixed_only_for_long_words():
    assert llm_parser._fix_vocab_typos("northweaterly line") == "northwesterly line"
    # короткие слова не трогаем: «wcat» не должен стать «west» через словарь
    assert llm_parser._fix_vocab_typos("feet and land") == "feet and land"


def test_stamp_on_the_margin_is_removed_from_the_middle_of_a_line():
    text = "North 31 degrees 46 ainutes REEL 56 seconds west\n28942\n3 1 8 2 PAGE 0 0 7\nnext line"
    cleaned = llm_parser._drop_margin_noise(text)
    assert "REEL" not in cleaned and "PAGE" not in cleaned
    assert "28942" not in cleaned
    assert "56 seconds west" in cleaned and "next line" in cleaned


# ------------------------------------------------------------------ слияние строк

def test_merge_keeps_the_longer_version_of_a_truncated_line():
    import ocr_pages as op

    short = "BEGINNING at a point on the sou\nknown as Surf Avenue"
    full = "BEGINNING at a point on the southwesterly side of Doty Avenue\nknown as Surf Avenue"
    assert op.merge_line_texts(short, full).splitlines()[0].endswith("Doty Avenue")
    assert op.merge_line_texts(full, short).splitlines()[0].endswith("Doty Avenue")


def test_merge_inserts_a_line_one_render_lost():
    primary = "A line one about the start\nTHENCE along the first side North 31 degrees\nTHENCE South 56 degrees"
    secondary = "A line one about the start\nTHENCE along the first side North 31 degrees\nTHENCE along said land south 42 degrees\nTHENCE South 56 degrees"
    merged = ocr_pages.merge_line_texts(primary, secondary).splitlines()
    assert "THENCE along said land south 42 degrees" in merged
    assert merged.index("THENCE along said land south 42 degrees") < merged.index("THENCE South 56 degrees")


# ------------------------------------------------------------------ проверка

def _model_from(lines: list[str]):
    model, _ = parse_with_regex("\n".join(lines), auto_repair_typos=True)
    return model


def test_side_described_in_words_is_never_ok():
    """Регресс F: сторона без румба, но с заметкой, не даёт «ok» — контур не проверен."""
    lines = list(FULL_LINES)
    lines[4] = "THENCE along the southerly line of Carlton Boulevard 100.00 feet to a point;"
    text = "\n".join(lines)
    model, _ = parse_with_regex(text, auto_repair_typos=True)
    check = check_description(model, text)
    assert check.status != STATUS_OK


def test_missing_side_is_failed_not_ok():
    """Регресс 3.55: три стороны вместо четырёх (пропала строка) — не «ok»."""
    lines = [line for i, line in enumerate(FULL_LINES) if i != 4]
    text = "\n".join(lines)
    model, _ = parse_with_regex(text, auto_repair_typos=True)
    check = check_description(model, text)
    assert check.status == STATUS_FAILED


def test_clean_text_still_passes_as_ok():
    text = "\n".join(FULL_LINES)
    model, _ = parse_with_regex(text, auto_repair_typos=True)
    check = check_description(model, text)
    assert check.status == STATUS_OK and check.closure_ft is not None


def test_standard_deed_pairs_are_read_in_order():
    text = (
        "distant 74.96 feet U.S. Standard (75.00) feet Deed) northwesterly; "
        "THENCE east 25.41 feet U.S. Standard (25.00 feet Deed) to land"
    )
    assert standard_deed_pairs(text) == [(74.96, 75.0), (25.41, 25.0)]


def test_deed_value_that_closes_is_a_warning_not_a_silent_change():
    """
    Арбитр: измеренная длина 25.60 не даёт замыкания (невязка 0.6 ft), а по документу (25.00)
    контур замыкается. Статус — warning с пояснением; геометрия модели не меняется.
    """
    text = (
        "THENCE North 90 degrees 00 minutes 00 seconds East 100.00 feet; running "
        "THENCE South 00 degrees 00 minutes 00 seconds East 25.60 feet U.S. Standard (25.00 feet Deed); running "
        "THENCE South 90 degrees 00 minutes 00 seconds West 100.00 feet; running "
        "THENCE North 00 degrees 00 minutes 00 seconds West 25.00 feet to the point or place of BEGINNING."
    )
    model, _ = parse_with_regex(text, auto_repair_typos=True)
    before = [seg.distance_ft for seg in model.segments]
    check = check_description(model, text)
    assert [seg.distance_ft for seg in model.segments] == before, "арбитр не меняет модель"
    assert check.status == STATUS_WARNING
    assert any("по документу (Deed) 25.00" in issue.text for issue in check.issues)


def test_deed_value_is_not_suggested_when_measured_closes():
    """Если измеренные длины уже замыкаются (как у OUR LOT 114: 0.41 ft), арбитр молчит."""
    text = (
        "THENCE North 90 degrees 00 minutes 00 seconds East 100.00 feet; running "
        "THENCE South 00 degrees 00 minutes 00 seconds East 25.41 feet U.S. Standard (25.00 feet Deed); running "
        "THENCE South 90 degrees 00 minutes 00 seconds West 100.00 feet; running "
        "THENCE North 00 degrees 00 minutes 00 seconds West 25.00 feet to the point or place of BEGINNING."
    )
    model, _ = parse_with_regex(text, auto_repair_typos=True)
    check = check_description(model, text)
    assert not any("по документу (Deed)" in issue.text for issue in check.issues)


# ------------------------------------------------------------------ хаотичный кеш

def test_chaotic_cache_from_the_tile_pass_is_never_ok():
    """Кеш OUR LOT 114 (после прохода 2×2): строки обрезаны, румбы разорваны — не ok."""
    text = (DATA / "lot114_cache_chaos.txt").read_text(encoding="utf-8-sig", errors="replace")
    model, _ = parse_with_regex(text, auto_repair_typos=True)
    check = check_description(model, text)
    assert check.status == STATUS_FAILED


# ------------------------------------------------------------------ реальный документ

def test_real_lot114_page_reads_clean_with_two_renders(tmp_path):
    """Сквозной прогон на реальном PDF: первый рендер + второй рендер + слияние → ok, 74.96 ft."""
    pytest.importorskip("rapidocr_onnxruntime")
    pytest.importorskip("pymupdf")
    from pdf_set import ocr_scan_once

    pdf = tmp_path / "OUR LOT 114.pdf"
    shutil.copy(DATA / "lot114_p1.pdf", pdf)

    first = ocr_scan_once(pdf, {"ocr_pages": "1"}, exact=True)
    second = ocr_scan_once(pdf, {"ocr_pages": "1"}, exact=False)
    assert first.error == "" and second.error == ""
    merged = ocr_pages.merge_line_texts(first.text, second.text)

    model, _ = parse_with_regex(merged, auto_repair_typos=True)
    check = check_description(model, merged)
    assert len(model.segments) == 4
    assert model.distance_to_corner_ft == pytest.approx(74.96, abs=0.01)
    assert check.status in (STATUS_OK, STATUS_WARNING)
    assert check.closure_ft is not None and check.closure_ft <= 0.5


# ------------------------------------------------------------------ 3.57: реальные рендеры и вычисленный румб

RENDERS = DATA
LOT114_TRUTH_DIST = 74.96


def _read_render(name: str) -> str:
    return (RENDERS / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "garbled, expected_distance",
    [
        ("West on hundred feet", 100.0),  # «one» потеряно, «on» вместо «one»
        ("seconds east hundred feet", 100.0),  # «one» потеряно полностью
        ("40 fect to the point", 40.0),  # «feet» распознано как «fect»
    ],
)
def test_lost_number_words_and_feet_are_repaired(garbled, expected_distance):
    from llm_parser import _distance_after, _spell_numbers_before_feet

    fixed = llm_parser._normalize_legal_text(garbled)
    spelled, _ = _spell_numbers_before_feet(fixed)
    assert _distance_after(spelled) == pytest.approx(expected_distance)


def test_degree_sign_read_as_question_mark_is_repaired():
    fixed = llm_parser._normalize_legal_text("South 56? 36 minutes 12 seconds east")
    assert "56° 36" in fixed
    assert "56°36" in llm_parser._normalize_legal_text("South 56?36 minutes")


def test_distance_without_the_corner_phrase_is_read_from_the_street_reference():
    """Строка «from the corner formed by…» потеряна при OCR, остальное на месте (OUR LOT 114, native)."""
    text = _read_render("lot114_render_native.txt")
    model, _ = parse_with_regex(text, auto_repair_typos=True)
    assert model.distance_to_corner_ft == pytest.approx(LOT114_TRUTH_DIST)


def test_orphan_second_course_gets_its_bearing_from_closure():
    """
    OUR LOT 114, рендер 600 dpi: строка «THENCE along said last mentioned land south 42°03'54" east»
    потеряна целиком, остался фрагмент «east 25.41 feet». Румб вычисляется по замыканию и
    совпадает с документом; статус — warning, потому что румб не прочитан.
    """
    text = _read_render("lot114_render_e600.txt")
    model, _ = parse_with_regex(text, auto_repair_typos=True)
    assert len(model.segments) == 4
    solved = [seg for seg in model.segments if seg.bearing_solved]
    assert len(solved) == 1
    seg = solved[0]
    assert (seg.ns, seg.ew, seg.degrees, seg.minutes) == ("S", "E", 42, 3)
    assert abs(seg.seconds - 54.0) < 10.0
    assert seg.distance_ft == pytest.approx(25.41)
    check = check_description(model, text)
    assert check.status == STATUS_WARNING
    assert any("вычислен по замыканию" in issue.text for issue in check.issues)


def test_orphan_is_not_solved_when_closure_does_not_match_its_length():
    """Сумма остальных сторон не равна длине фрагмента: румб не вычисляется, сторона — «не прочитан»."""
    text = (
        "THENCE North 90 degrees 00 minutes 00 seconds East 100.00 feet; running\n"
        "THENCE South 00 degrees 00 minutes 00 seconds East 25.00 feet; running\n"
        "THENCE South 90 degrees 00 minutes 00 seconds West 100.00 feet; running\n"
        "east 30.00 feet to land now or formerly of Smith; running\n"
        "THENCE North 00 degrees 00 minutes 00 seconds West 25.00 feet to the point or place of BEGINNING."
    )
    model, _ = parse_with_regex(text, auto_repair_typos=True)
    assert not any(seg.bearing_solved for seg in model.segments)
    check = check_description(model, text)
    assert check.status == STATUS_FAILED


def test_orphan_is_not_solved_when_its_direction_word_contradicts_the_closure():
    """Замыкание даёт направление на запад, а в тексте «east»: румб не выдумываем."""
    text = (
        "THENCE North 90 degrees 00 minutes 00 seconds East 100.00 feet; running\n"
        "THENCE South 00 degrees 00 minutes 00 seconds East 25.00 feet; running\n"
        "east 100.00 feet to land now or formerly of Smith; running\n"
        "THENCE North 00 degrees 00 minutes 00 seconds West 25.00 feet to the point or place of BEGINNING."
    )
    model, _ = parse_with_regex(text, auto_repair_typos=True)
    assert not any(seg.bearing_solved for seg in model.segments)


def test_solved_bearing_is_never_ok():
    text = _read_render("lot114_render_e600.txt")
    model, _ = parse_with_regex(text, auto_repair_typos=True)
    assert check_description(model, text).status != STATUS_OK


def test_missing_corner_distance_is_not_ok_when_the_text_does_not_start_at_a_corner():
    """Строка с расстоянием потеряна (OUR 70, native): проверка не должна давать «ok» с расстоянием 0."""
    lines = [line for line in FULL_LINES if "distant" not in line and "corner" not in line]
    lines[0] = "BEGINNING at a point on the northwesterly side of Stafford Avenue, 60 feet wide,"
    text = "\n".join(lines)
    model, _ = parse_with_regex(text, auto_repair_typos=True)
    assert not model.distance_to_corner_ft
    check = check_description(model, text)
    assert check.status == STATUS_WARNING
    assert any("расстояние от угла" in issue.text for issue in check.issues)


def test_corner_start_without_distance_is_still_ok():
    """Описание начинается от угла — расстояние не нужно, ok сохраняется."""
    lines = list(FULL_LINES)
    lines[0] = "BEGINNING at the corner formed by the northwesterly side of Stafford Avenue and Carlton Boulevard,"
    lines[1] = "running"
    text = "\n".join(lines)
    model, _ = parse_with_regex(text, auto_repair_typos=True)
    check = check_description(model, text)
    assert check.status == STATUS_OK


def test_lot72_render_with_lost_words_reads_all_four_sides():
    text = _read_render("lot72_render_e600.txt")
    model, _ = parse_with_regex(text, auto_repair_typos=True)
    assert len(model.segments) == 4
    assert model.distance_to_corner_ft == pytest.approx(241.0)
    assert check_description(model, text).status == STATUS_OK
