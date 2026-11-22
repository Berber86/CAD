"""Drawing and print draft rules: mixed dimensions, frontage and street ordinals."""
import math

import pytest

import acad_builder as ab
import llm_parser as lp
import street_names


@pytest.mark.parametrize("number,suffix", [
    (1, "st"), (2, "nd"), (3, "rd"), (4, "th"), (11, "th"), (12, "th"),
    (13, "th"), (21, "st"), (22, "nd"), (23, "rd"), (111, "th"), (112, "th"),
    (113, "th"), (121, "st"),
])
def test_ordinal_numbers_and_street_abbreviation(number, suffix):
    assert street_names.street_text(f"{number} St") == f"{number}{suffix}  ST"
    assert street_names.street_text(f"{number}TH Avenue") == f"{number}{suffix}  AVENUE"
    assert street_names.ordinal_street_numbers(f"PARALLEL WITH {number} ST") == f"PARALLEL WITH {number}{suffix} ST"


def test_non_street_numbers_are_not_rewritten():
    assert street_names.street_text("Ranch Road 620") == "RANCH  ROAD  620"
    assert street_names.ordinal_street_numbers("ROUTE 20 AND 5.25 FEET, 63-01 BORDEN") == "ROUTE 20 AND 5.25 FEET, 63-01 BORDEN"
    assert street_names.former_line("11 St") == "(FORMERLY 11th  ST)"


def make_description():
    desc, _ = lp.parse_with_regex(lp.NYC_SAMPLES["63-01 Borden Ave (Queens — без румбов)"])
    # Opposite sides have opposite reading-normal signs. Do not invent relationships:
    # these notes are explicitly supplied as if extracted from the description.
    notes = ["parallel with 1 St", "parallel with 112 Street",
             "parallel with 23RD Street", "along the northerly side of Borden Avenue"]
    segments = [seg.model_copy(update={"course_note": note}) for seg, note in zip(desc.segments, notes)]
    return desc.model_copy(update={"segments": segments, "cross_street": "63 Street"})


def reading_height(item):
    return -math.sin(item.rotation) * item.point[0] + math.cos(item.rotation) * item.point[1]


def test_notes_order_and_frontage_on_plan_and_draft():
    desc = make_description()
    plan = ab.AcadBuilder(lp.load_config()).plan(desc, scale=16)
    original_notes = [s.course_note for s in desc.segments]
    draft = ab.make_print_draft(plan, {})
    for scene in (plan, draft):
        items = {i.kind: i for i in scene.items}
        assert items["cross-street"].text == "63rd  STREET"
        for i, expected in [(1, "PARALLEL WITH 1st ST"), (2, "PARALLEL WITH 112th STREET"),
                            (3, "PARALLEL WITH 23rd STREET")]:
            dim, note = items[f"side-{i}"], items[f"side-{i}-note"]
            assert note.text == expected
            assert dim.rotation == pytest.approx(note.rotation)
            assert reading_height(dim) > reading_height(note)
        assert not any(i.kind.startswith("side-4-note") and "ALONG" in i.text for i in scene.items)
    assert [s.course_note for s in desc.segments] == original_notes


def test_front_only_along_removed_other_notes_preserved():
    desc = make_description()
    segments = list(desc.segments)
    segments[0] = segments[0].model_copy(update={"course_note": "along 1 St"})
    segments[3] = segments[3].model_copy(update={"course_note": "along Borden Avenue; AT RIGHT ANGLES TO 5 ST"})
    desc = desc.model_copy(update={"segments": segments})
    plan = ab.AcadBuilder(lp.load_config()).plan(desc)
    for scene in (plan, ab.make_print_draft(plan, {})):
        # side-1 не фротальная: course_note без изменений
        assert any(i.kind.startswith("side-1-note") and "ALONG" in i.text for i in scene.items)
        # side-4 фротальная: остаётся только название улицы, все пояснения удалены
        assert any(i.kind.startswith("side-4-note") and "BORDEN AVENUE" in i.text for i in scene.items)
        assert not any(i.kind.startswith("side-4-note") and "ALONG" in i.text for i in scene.items)
        assert not any(i.kind.startswith("side-4-note") and "RIGHT ANGLES" in i.text for i in scene.items)


def test_mixed_units_keep_nonzero_decimals_and_integer_zero_inches_on_both_plans():
    text = lp.NYC_SAMPLES["283 Jamie Lane (Staten Island)"]
    text = text.replace("25 feet 10 inches", "25.25 feet", 1).replace("13 feet 8 inches", "13.00 feet", 1)
    desc, _ = lp.parse_with_regex(text)
    before = [s.distance_ft for s in desc.segments]
    plan = ab.AcadBuilder(lp.load_config()).plan(desc)
    for scene in (plan, ab.make_print_draft(plan, {})):
        by_kind = {i.kind: i.text for i in scene.items}
        assert by_kind["side-1"] == '13\'-0"'
        assert by_kind["side-2"] == "25.25'"
        assert by_kind["side-3"] == '13\'-8"'
        assert by_kind["side-4"] == '25\'-10"'
        assert [math.dist(e.start, e.end) for e in scene.boundary_lines] == pytest.approx(before)
    cached, _ = lp.parse_with_regex(lp.legal_to_step0_text(desc))
    assert [s.distance_ft for s in cached.segments] == pytest.approx(before)
    assert ab.description_uses_feet_inches(cached)


def test_integer_tie_in_inches_mode_and_default_decimal_mode():
    text = lp.NYC_SAMPLES["283 Jamie Lane (Staten Island)"]
    desc, _ = lp.parse_with_regex(text)
    desc = desc.model_copy(update={"distance_to_corner_ft": 100.0, "cross_street": "Main Road"})
    plan = ab.AcadBuilder(lp.load_config()).plan(desc)
    for scene in (plan, ab.make_print_draft(plan, {})):
        assert next(i.text for i in scene.items if i.kind == "cross-street-distance") == '100\'-0"'
    assert ab.display_distance(100.00001, feet_inches=True) == "100.00'"
    assert ab.display_distance(100, feet_inches=False) == "100.00'"
    assert ab.display_distance(100, "101 feet 2 inches", feet_inches=True) == "100.00'"


@pytest.mark.parametrize("measure,expected", [("420'-8\"", 420 + 8/12),
                                               ("420'-6 1/2\"", 420 + 6.5/12),
                                               ("1,420 feet 1/4 inches", 1420 + .25/12)])
def test_tie_fractional_inches_roundtrip(measure, expected):
    text = lp.ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"].replace("420.00 feet", measure)
    desc, _ = lp.parse_with_regex(text)
    assert desc.distance_to_corner_ft == pytest.approx(expected, abs=1e-7)
    assert desc.distance_to_corner_source is not None
    cached, _ = lp.parse_with_regex(lp.legal_to_step0_text(desc))
    assert cached.distance_to_corner_ft == pytest.approx(expected, abs=1e-7)
    assert cached.distance_to_corner_source is not None


def test_printed_bearings_and_explicit_parallel_notes_are_both_retained():
    text = lp.NYC_SAMPLES["283 Jamie Lane (Staten Island)"]
    text = text.replace("25 feet 10 inches", "parallel with 1 St, 25 feet 10 inches", 1)
    desc, _ = lp.parse_with_regex(text)
    assert desc.segments[1].has_bearing
    assert desc.segments[1].course_note == "PARALLEL WITH 1 ST"
    cached, _ = lp.parse_with_regex(lp.legal_to_step0_text(desc))
    assert cached.segments[1].course_note == "PARALLEL WITH 1 ST"
    plan = ab.AcadBuilder(lp.load_config()).plan(desc)
    for scene in (plan, ab.make_print_draft(plan, {})):
        items = {i.kind: i for i in scene.items}
        bearing, length, note = (items["side-2-bearing"], items["side-2"], items["side-2-note"])
        assert note.text == "PARALLEL WITH 1st ST"
        assert length.text == '25\'-10"'
        assert reading_height(bearing) > reading_height(length) > reading_height(note)
        if scene.print_draft:
            assert bearing.height == length.height == 2
            assert note.height == 1.6


@pytest.mark.parametrize("bearing", [False, True])
@pytest.mark.parametrize("note", ["PARALLEL WITH FIFTH AVENUE", "northerly parallel with 56 Street", "parallel to 21 St"])
def test_parallel_note_never_wraps_even_on_short_line(bearing, note):
    desc = make_description()
    segments = list(desc.segments)
    segments[1] = segments[1].model_copy(update={"has_bearing": bearing, "course_note": note})
    desc = desc.model_copy(update={"segments": segments})
    plan = ab.AcadBuilder(lp.load_config()).plan(desc)
    for scene in (plan, ab.make_print_draft(plan, {})):
        dim = next(i for i in scene.items if i.kind == "side-2")
        notes = [i for i in scene.items if i.kind.startswith("side-2-note")]
        assert len(notes) == 1
        assert notes[0].text == street_names.ordinal_street_numbers(note.upper())
        assert reading_height(dim) > reading_height(notes[0])
