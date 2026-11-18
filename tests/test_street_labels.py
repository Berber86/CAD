"""
Подписи улиц на чертеже (требования заказчика):

1. Старое название («formerly», «known as» и т. п., в скобках или без) не входит в название улицы.
2. Расстояние до перекрёстка — только значение, без «TO CORNER», со стороны блока.
3. Между словами названия улицы — два пробела.
4. Под названием улицы в скобках — ширина «(XX' WIDE)» или значение из описания.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import street_names  # noqa: E402
from acad_builder import AcadBuilder  # noqa: E402
from llm_parser import load_config, parse_with_regex  # noqa: E402

LOT32 = (ROOT / "tests" / "data" / "alvine_lot32_legal.txt").read_text(encoding="utf-8")
LOT32 = "ALL that certain" + LOT32.split("ALL that certain", 1)[1]


def _with_street(street_phrase: str) -> str:
    return LOT32.replace("easterly line of Alvine Avenue", f"easterly line of {street_phrase}", 1)


# ------------------------------------------------------------------ строковые правила


@pytest.mark.parametrize(
    ("raw", "clean", "former"),
    [
        ("Alvine Avenue (formerly Carlton Street)", "Alvine Avenue", "Carlton Street"),
        ("Alvine Avenue, formerly Carlton Street,", "Alvine Avenue", "Carlton Street"),
        ("Alvine Avenue (previously known as Carlton Street)", "Alvine Avenue", "Carlton Street"),
        ("Alvine Avenue (formerly known as Carlton Street)", "Alvine Avenue", "Carlton Street"),
        ("Alvine Avenue (also known as Carlton Street)", "Alvine Avenue", "Carlton Street"),
        ("Alvine Avenue", "Alvine Avenue", None),
    ],
)
def test_former_name_is_split_from_current_name(raw, clean, former):
    assert street_names.split_former_name(raw) == (clean, former)


def test_street_text_uses_two_spaces_between_words():
    assert street_names.street_text("dartmouth loop") == "DARTMOUTH  LOOP"
    assert street_names.street_text("Ranch   Road 620") == "RANCH  ROAD  620"


def test_width_line_uses_value_or_placeholder():
    assert street_names.width_line("38") == "(38' WIDE)"
    assert street_names.width_line(None) == "(XX' WIDE)"


def test_street_note_gives_width_and_other_remarks_separately():
    assert street_names.parse_street_note("(38' WIDE ROADWAY EASEMENT)") == (
        "38",
        ["(ROADWAY EASEMENT)"],
    )
    assert street_names.parse_street_note("(60 FEET WIDE)") == ("60", [])
    assert street_names.parse_street_note("(NOT MAPPED ON FINAL CITY PLAN)") == (
        None,
        ["(NOT MAPPED ON FINAL CITY PLAN)"],
    )
    # Старое название из примечания не выводится: оно хранится отдельно
    assert street_names.parse_street_note("(60' WIDE) (FORMERLY CARLTON STREET)") == ("60", [])
    assert street_names.parse_street_note(None) == (None, [])


def test_former_line_is_formatted_as_formerly():
    assert street_names.former_line("Carlton Street") == "(FORMERLY CARLTON  STREET)"
    assert street_names.former_line(None) is None


# ------------------------------------------------------------------ разбор описания


@pytest.mark.parametrize(
    "phrase",
    [
        "Alvine Avenue (formerly Carlton Street)",
        "Alvine Avenue, formerly Carlton Street,",
        "Alvine Avenue (previously known as Carlton Street)",
        "Alvine Avenue (formerly known as Carlton Street)",
        "Alvine Avenue (also known as Carlton Street)",
    ],
)
def test_parser_keeps_only_current_name_and_stores_former(phrase):
    model, _ = parse_with_regex(_with_street(phrase))
    assert model.main_street == "Alvine Avenue"
    assert model.main_street_former == "Carlton Street"
    assert "formerly" not in model.main_street.lower()
    assert "known" not in model.main_street.lower()


def test_parser_reads_width_from_text_next_to_the_street():
    model, _ = parse_with_regex(_with_street("Alvine Avenue, 60 feet wide,"))
    assert model.main_street == "Alvine Avenue"
    assert street_names.parse_street_note(model.main_street_note) == ("60", [])


def test_parser_keeps_width_when_old_name_is_also_given():
    model, _ = parse_with_regex(_with_street("Alvine Avenue (formerly Carlton Street) (60 feet wide)"))
    assert model.main_street_former == "Carlton Street"
    assert street_names.parse_street_note(model.main_street_note) == ("60", [])


# ------------------------------------------------------------------ чертёж


def _plan_for(street_phrase: str):
    model, _ = parse_with_regex(_with_street(street_phrase))
    return AcadBuilder(load_config()).plan(model, scale=16.0)


def test_drawn_street_block_has_width_placeholder_and_former_name():
    plan = _plan_for("Alvine Avenue (formerly Carlton Street)")
    by_kind = {item.kind: item for item in plan.items}
    assert by_kind["main-street"].text == "ALVINE  AVENUE"
    assert by_kind["main-street-note"].text == "(XX' WIDE)"
    assert by_kind["main-street-note-extra"].text == "(FORMERLY CARLTON  STREET)"
    # Название — верхняя строка группы, ниже — ширина, затем старое название
    assert by_kind["main-street"].height > by_kind["main-street-note"].height


def test_drawn_width_comes_from_description():
    plan = _plan_for("Alvine Avenue (60 feet wide)")
    by_kind = {item.kind: item for item in plan.items}
    assert by_kind["main-street-note"].text == "(60' WIDE)"
    assert "main-street-note-extra" not in by_kind


def test_cross_street_has_two_spaces_between_words():
    plan = _plan_for("Alvine Avenue")
    cross = next(item for item in plan.items if item.kind == "cross-street")
    assert cross.text == "AMBOY  ROAD"


def test_distance_label_is_value_only_on_the_block_side():
    plan = _plan_for("Alvine Avenue")
    dist = next(item for item in plan.items if item.kind == "cross-street-distance")
    assert dist.text == "200.00'"
    assert "TO CORNER" not in dist.text
    # Линия расстояния идёт вдоль фасада, поэтому сама подпись вне участка.
    # Она должна лежать со стороны блока (та же сторона, что и центр участка),
    # а название улицы — со стороны дороги (противоположная сторона).
    cx = sum(p[0] for p in plan.vertices) / len(plan.vertices)
    street = next(item for item in plan.items if item.kind == "main-street")
    assert (dist.point[0] > 0) == (cx > 0)
    assert (street.point[0] < 0) == (cx > 0)


def test_neighbour_distance_label_has_no_from_corner():
    model, _ = parse_with_regex(LOT32)
    label = AcadBuilder(load_config())._neighbor_label_item(
        model, [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)], 1.0, None, None
    )
    assert label.text == "NEIGHBOR (200')"


# ------------------------------------------------------------------ ширина улицы


@pytest.mark.parametrize(
    "phrase",
    [
        "Alvine Avenue (60 feet wide)",
        "Alvine Avenue (60' WIDE)",
        "Alvine Avenue (60 FT WIDE)",
        "Alvine Avenue (sixty feet wide)",
        "Alvine Avenue (sixty (60) feet wide)",
        "Alvine Avenue, 60-foot wide,",
        "Alvine Avenue, a 60 foot wide street,",
        "Alvine Avenue, having a width of 60 feet,",
        "Alvine Avenue, which has a width of sixty feet,",
        "Alvine Avenue (60 feet in width)",
        "Alvine Avenue (width 60 feet)",
    ],
)
def test_parser_reads_width_in_every_common_form(phrase):
    model, _ = parse_with_regex(_with_street(phrase))
    assert model.main_street == "Alvine Avenue"
    assert street_names.parse_street_note(model.main_street_note)[0] == "60"


def test_nested_parentheses_do_not_leave_number_words_on_the_drawing():
    model, _ = parse_with_regex(_with_street("Alvine Avenue (sixty (60) feet wide)"))
    assert street_names.parse_street_note(model.main_street_note) == ("60", [])


def test_width_is_not_taken_from_the_neighbouring_street():
    text = "the easterly line of Alvine Avenue; running to Amboy Road (50 feet wide)"
    assert street_names.find_width_text(text, "Alvine Avenue", stop="Amboy Road") is None


def test_width_mentioned_at_a_later_mention_of_the_street_is_found():
    text = (
        "fronting Alvine Avenue; THENCE along a line; "
        "and along the line of Alvine Avenue (45 feet wide)"
    )
    assert street_names.find_width_text(text, "Alvine Avenue") == "(45' WIDE)"


def test_no_width_in_description_gives_placeholder_on_drawing():
    plan = _plan_for("Alvine Avenue")
    note = next(item for item in plan.items if item.kind == "main-street-note")
    assert note.text == "(XX' WIDE)"


def test_ai_answer_without_width_gets_it_from_the_text():
    from llm_parser import _ensure_street_width  # noqa: PLC0415

    model, _ = parse_with_regex(_with_street("Alvine Avenue"))
    model = model.model_copy(update={"main_street_note": None})
    fixed = _ensure_street_width(model, _with_street("Alvine Avenue (60 feet wide)"))
    assert fixed.main_street_note == "(60' WIDE)"


def test_ai_width_already_in_answer_is_kept():
    from llm_parser import _ensure_street_width  # noqa: PLC0415

    model, _ = parse_with_regex(_with_street("Alvine Avenue"))
    model = model.model_copy(update={"main_street_note": "(38' WIDE)"})
    kept = _ensure_street_width(model, _with_street("Alvine Avenue (60 feet wide)"))
    assert kept.main_street_note == "(38' WIDE)"
