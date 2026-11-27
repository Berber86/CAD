"""Понятные требования Николая из эскиза Шага 1."""
import math

import pytest
from shapely.geometry import Point, Polygon

import llm_parser as parser
import street_names
from acad_builder import AcadBuilder, make_print_draft, segment_distance_label


def config():
    return parser.load_config()


def test_party_label_is_inside_not_outside_and_source_plan_unchanged():
    desc, _ = parser.parse_with_regex(parser.NYC_SAMPLES["147 Dartmouth Loop (Staten Island)"])
    plan = AcadBuilder(config()).plan(desc, scale=16)
    original = list(plan.items)
    draft = make_print_draft(plan, {}, builder_cfg=config())
    polygon = Polygon(draft.vertices)
    walls = [item for item in draft.items if item.kind.startswith("party-wall-")]
    assert len(walls) == 2
    assert all(polygon.contains(Point(item.point)) for item in walls)
    assert plan.items == original
    assert not any(item.kind.startswith("party-wall-") for item in plan.items)


@pytest.mark.parametrize("phrase", [
    "PART OF THE DISTANCE THROUGH A PARTY WALL",
    "part  of the distance through the party-wall",
])
def test_prohibited_long_phrase_removed_but_other_course_notes_retained(phrase):
    desc, _ = parser.parse_with_regex(parser.NYC_SAMPLES["147 Dartmouth Loop (Staten Island)"])
    second = desc.segments[1].model_copy(update={
        "has_bearing": False, "course_note": "parallel to oak road, " + phrase,
        "party_wall": True,
    })
    desc = desc.model_copy(update={"segments": [desc.segments[0], second, *desc.segments[2:]]})
    plan = AcadBuilder(config()).plan(desc, scale=16)
    draft = make_print_draft(plan, {})
    for scene in (plan, draft):
        notes = [i.text for i in scene.items if i.kind.startswith("side-2-note")]
        assert notes and "PARALLEL TO OAK ROAD" in " ".join(notes)
        assert not any("PART OF THE DISTANCE" in i.text.upper() for i in scene.items)
    assert any(i.kind == "party-wall-2" and i.text == "PARTY WALL" for i in draft.items)


def test_feet_and_inches_round_trip_with_original_side_geometry():
    desc, _ = parser.parse_with_regex(parser.NYC_SAMPLES["283 Jamie Lane (Staten Island)"])
    assert [s.distance_source for s in desc.segments] == [
        "13 feet 8 inches", "25 feet 10 inches", "13 feet 8 inches", "25 feet 10 inches"
    ]
    plan = AcadBuilder(config()).plan(desc, scale=16)
    draft = make_print_draft(plan, {})
    for p in (plan, draft):
        by_kind = {item.kind: item.text for item in p.items}
        assert by_kind["side-1"] == "13'-8\""
        assert by_kind["side-2"] == "25'-10\""
    assert math.dist(plan.boundary_lines[0].start, plan.boundary_lines[0].end) == pytest.approx(13 + 8/12)
    cached = parser.legal_to_step0_text(desc)
    again, _ = parser.parse_with_regex(cached)
    assert [s.distance_source for s in again.segments] == ["13'-8\"", "25'-10\"", "13'-8\"", "25'-10\""]


@pytest.mark.parametrize("source,dist,expected", [
    ("25'-6 1/2\"", 25+6.5/12, "25'-6 1/2\""),
    ("14 feet 1/4 inches", 14+.25/12, "14'-1/4\""),
    ("12'-3.5\"", 12+3.5/12, "12'-3.5\""),
])
def test_fractional_inches_are_not_converted_to_decimal_feet(source, dist, expected):
    assert parser.format_feet_inches_source(source, dist) == expected
    desc, _ = parser.parse_with_regex(parser.NYC_SAMPLES["283 Jamie Lane (Staten Island)"])
    seg = desc.segments[0].model_copy(update={"distance_source": source, "distance_ft": dist})
    assert segment_distance_label(seg) == expected


def test_invalid_fraction_is_not_printed():
    assert parser.format_feet_inches_source("13 feet 1/0 inches", 13.0) is None


def test_changed_size_never_prints_stale_source_measure():
    desc, _ = parser.parse_with_regex(parser.NYC_SAMPLES["283 Jamie Lane (Staten Island)"])
    seg = desc.segments[0].model_copy(update={"distance_ft": 15.0})
    assert segment_distance_label(seg) == "15.00'"
    changed = desc.model_copy(update={"segments": [seg, *desc.segments[1:]]})
    assert any("футы/дюймы" in w and "сторона #1" in w for w in AcadBuilder(config()).plan(changed).warnings)
    assert segment_distance_label(parser._flip_segment_direction(desc.segments[0])) == "13'-8\""
    assert segment_distance_label(parser._mutate_segment_bearing(desc.segments[0])) == "13'-8\""


def test_cross_street_former_does_not_leak_to_main_and_is_on_both_plans():
    text = parser.ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"]
    text = text.replace("149th Street;", "149th Street (formerly Birch Avenue);")
    desc, _ = parser.parse_with_regex(text)
    assert desc.main_street_former is None
    assert desc.cross_street == "149th Street" and desc.cross_street_former == "Birch Avenue"
    plan = AcadBuilder(config()).plan(desc, scale=16)
    draft = make_print_draft(plan, {})
    for p in (plan, draft):
        note = next(i for i in p.items if i.kind == "cross-street-former")
        street = next(i for i in p.items if i.kind == "cross-street")
        assert note.text == "(FORMERLY BIRCH  AVENUE)"
        assert note.rotation == pytest.approx(street.rotation)
        assert 0 < math.dist(note.point, street.point) < 10
    assert next(i for i in draft.items if i.kind == "cross-street-former").height == 1.6


def test_former_for_corner_street_keeps_geometry():
    desc, _ = parser.parse_with_regex(open("tests/data/lot51_commencing_corner.txt", encoding="utf8").read())
    desc = desc.model_copy(update={"cross_street_former": "Old Avenue"})
    plan = AcadBuilder(config()).plan(desc, scale=16)
    draft = make_print_draft(plan, {})
    for p in (plan, draft):
        note = next(i for i in p.items if i.kind == "cross-street-former")
        street = next(i for i in p.items if i.kind == "cross-street")
        assert note.text == "(FORMERLY OLD  AVENUE)"
        assert note.rotation == pytest.approx(street.rotation)
        assert 0 < math.dist(note.point, street.point) < 10
    assert len(plan.street_boundary_indices) == 2


def test_no_former_name_invented_from_the_next_street():
    text = "Main Avenue intersects Oak Street (formerly Pine Road);"
    assert street_names.find_former_name(text, "Main Avenue") is None
    assert street_names.find_former_name(text, "Oak Street") == "Pine Road"
    assert street_names.find_former_name("Main Avenue (60 feet wide) (formerly Old Road);", "Main Avenue") == "Old Road"


def test_ai_missed_inches_are_restored_when_courses_match(monkeypatch):
    text = parser.NYC_SAMPLES["283 Jamie Lane (Staten Island)"]
    local, _ = parser.parse_with_regex(text)
    ai = local.model_copy(update={"segments": [s.model_copy(update={"distance_source": None}) for s in local.segments]})
    usage = parser.TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2, provider="test", model="test")
    monkeypatch.setattr(parser, "_extract_with_llm", lambda *args, **kwargs: (ai, "structured-outputs", usage))
    monkeypatch.setattr(parser, "accepted_without_ai", lambda check: False)
    out = parser.parse_legal_description_full(text=text, cfg=config(), use_llm=True, ask_on_doubt=False)
    assert out.source == "llm"
    assert [segment_distance_label(s) for s in out.data.segments] == ["13'-8\"", "25'-10\"", "13'-8\"", "25'-10\""]


def test_tie_dimension_keeps_feet_and_inches_on_original_and_draft():
    text = parser.ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"].replace(
        "420.00 feet", "420 feet 8 inches"
    )
    desc, _ = parser.parse_with_regex(text)
    assert desc.distance_to_corner_source == "420 feet 8 inches"
    plan = AcadBuilder(config()).plan(desc, scale=16)
    draft = make_print_draft(plan, {})
    assert next(i.text for i in plan.items if i.kind == "cross-street-distance") == "420'-8\""
    assert next(i.text for i in draft.items if i.kind == "cross-street-distance") == "420'-8\""
    assert math.dist(*[(next(l for l in draft.lines if l.kind == "main-street-row-tie")).start,
                       (next(l for l in draft.lines if l.kind == "main-street-row-tie")).end]) == pytest.approx(40)
    changed = desc.model_copy(update={"distance_to_corner_ft": 425})
    plan2 = AcadBuilder(config()).plan(changed, scale=16)
    assert next(i.text for i in plan2.items if i.kind == "cross-street-distance") == "425.00'"
    cached = parser.legal_to_step0_text(desc)
    after, _ = parser.parse_with_regex(cached)
    assert after.distance_to_corner_ft == pytest.approx(desc.distance_to_corner_ft)
    assert after.distance_to_corner_source == "420'-8\""


def test_cache_preserves_both_former_streets_and_party_wall():
    text = parser.NYC_SAMPLES["147 Dartmouth Loop (Staten Island)"]
    desc, _ = parser.parse_with_regex(text)
    desc = desc.model_copy(update={"main_street_former": "Old Loop", "cross_street_former": "Old Road"})
    cached = parser.legal_to_step0_text(desc)
    after, _ = parser.parse_with_regex(cached)
    assert after.main_street_former == "Old Loop"
    assert after.cross_street_former == "Old Road"
    assert [s.party_wall for s in after.segments] == [False, True, False, True]


def test_last_commencing_tie_keeps_feet_and_inches():
    text = parser.NYC_SAMPLES["147 Dartmouth Loop (Staten Island)"]
    text = text.replace("208.02 feet (DEED) / 208.22 feet (T.M.)", "208 feet 2 inches")
    desc, _ = parser.parse_with_regex(text)
    assert desc.distance_to_corner_ft == pytest.approx(208 + 2/12)
    assert desc.distance_to_corner_source == "208 feet 2 inches"
    plan = AcadBuilder(config()).plan(desc, scale=16)
    assert next(item.text for item in plan.items if item.kind == "cross-street-distance") == "208'-2\""
    draft = make_print_draft(plan, {})
    assert next(item.text for item in draft.items if item.kind == "cross-street-distance") == "208'-2\""


def test_plain_decimal_feet_source_strings_do_not_trigger_feet_inches_warnings():
    text = open("tests/data/stafford_lot68_legal.txt", encoding="utf8").read()
    desc, _ = parser.parse_with_regex(text)
    polluted = desc.model_copy(
        update={
            "distance_to_corner_source": "161.00 feet",
            "segments": [
                s.model_copy(update={"distance_source": f"{s.distance_ft:.2f} feet"})
                for s in desc.segments
            ],
        }
    )
    plan = AcadBuilder(config()).plan(polluted, scale=16)
    assert not any("футы/дюймы" in w for w in plan.warnings)

    # On mixed feet-and-inches plans, plain decimal-feet source on an integer side
    # still permits 40'-0" formatting while stale inches still warn.
    mixed = polluted.model_copy(
        update={
            "segments": [
                polluted.segments[0].model_copy(update={"distance_ft": 100 + 8 / 12, "distance_source": "100 feet 8 inches"}),
                *polluted.segments[1:],
            ]
        }
    )
    assert segment_distance_label(mixed.segments[1], feet_inches=True) == "40'-0\""
