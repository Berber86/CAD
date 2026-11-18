"""
Шаг 4 (каркас): разбор эталонных оффсетов заказчика, проверка кадра двух дампов, геометрия.
Эталоны — docs/*.dump.json, снятые с чертежей заказчика (см. docs/step4_offsets_plan.md).
"""

import json
from pathlib import Path

import pytest

import offsets as off

DOCS = Path(__file__).resolve().parents[1] / "docs"
FINAL_ASPINWALL = DOCS / "225 Aspinwall Street, Staten Island.final.dump.json"
EDITED_ASPINWALL = DOCS / "edited for MLP 225 Aspinwall Street, Staten Island.dump.json"
FINAL_ILION = DOCS / "12 Ilion Place, Staten Island.final.dump.json"
FINAL_BROOKLYN = DOCS / "6910 15th Avenue, Brooklyn.final.dump.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- тексты и высоты


def test_clean_text_strips_mtext_codes():
    assert off.clean_text("\\A2;27.6'") == "27.6'"
    assert off.clean_text("FE.0.5'W\\PFE.0.9'N") == "FE.0.5'W FE.0.9'N"
    assert off.clean_text("\\pxqc;ROOFED\\PO.P.") == "ROOFED O.P."
    assert off.clean_text("{\\H1.2x;BLDG} 6.2'E") == "BLDG 6.2'E"


def test_offset_heights_follow_customer_rule():
    assert off.offset_height_ft("house") == pytest.approx(1.1)
    assert off.offset_height_ft("fence") == pytest.approx(0.9)
    assert off.offset_height_ft("house", 20) == pytest.approx(1.375)
    assert off.offset_height_ft("fence", 20) == pytest.approx(1.125)


# ---------------------------------------------------------------- эталоны


def test_aspinwall_final_has_16_dimension_and_8_leader_offsets():
    items = off.parse_observed_offsets(_load(FINAL_ASPINWALL))
    dims = [i for i in items if i.style == "dimension"]
    leaders = [i for i in items if i.style == "leader"]
    assert len(dims) == 16
    assert len(leaders) == 8


def test_aspinwall_leader_fence_offset_is_parsed_with_its_leader():
    items = {i.handle: i for i in off.parse_observed_offsets(_load(FINAL_ASPINWALL))}
    item = items["26128"]
    assert item.text == "FE.0.6'N"
    assert item.leader_handle == "26127"
    assert item.height_ft == pytest.approx(0.9)
    assert [(p.label, p.value_ft, p.direction) for p in item.parts] == [("FE.", 0.6, "N")]


def test_aspinwall_two_parts_in_one_text_are_split():
    items = {i.handle: i for i in off.parse_observed_offsets(_load(FINAL_ASPINWALL))}
    parts = [(p.label, p.value_ft, p.direction) for p in items["26123"].parts]
    assert parts == [("FE.", 0.5, "W"), ("FE.", 0.9, "N")]


def test_aspinwall_house_dimensions_are_1_1_high():
    dims = [i for i in off.parse_observed_offsets(_load(FINAL_ASPINWALL)) if i.style == "dimension"]
    tall = sorted(i.parts[0].value_ft for i in dims if i.height_ft == pytest.approx(1.1))
    assert tall == [4.9, 5.0, 27.6, 27.7]


def test_ilion_house_offset_is_leader_at_1_1():
    items = {i.handle: i for i in off.parse_observed_offsets(_load(FINAL_ILION))}
    item = items["28B64"]
    assert item.height_ft == pytest.approx(1.1)
    assert [(p.label, p.value_ft, p.direction) for p in item.parts] == [("BLDG", 6.2, "E")]


def test_brooklyn_house_offset_is_leader_at_1_1():
    items = {i.handle: i for i in off.parse_observed_offsets(_load(FINAL_BROOKLYN))}
    assert items["26804"].height_ft == pytest.approx(1.1)
    assert items["26804"].parts[0].direction == "W"


def test_edited_drawing_has_no_offsets_yet():
    assert off.parse_observed_offsets(_load(EDITED_ASPINWALL)) == []


# ---------------------------------------------------------------- кадр двух дампов


def test_same_dump_is_same_frame():
    dump = _load(FINAL_ASPINWALL)
    check = off.same_frame(dump, dump)
    assert check.common > 0
    assert check.same_frame


def test_edited_and_final_aspinwall_are_not_the_same_frame():
    # Handle в разных файлах переиспользуются, а финальный чертёж снят в повёрнутой системе.
    check = off.same_frame(_load(EDITED_ASPINWALL), _load(FINAL_ASPINWALL))
    assert check.common > 0
    assert check.same == 0
    assert not check.same_frame


# ---------------------------------------------------------------- геометрия


SQUARE = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]


def test_perpendicular_distance_to_side():
    distance, foot = off.perpendicular_distance((5.0, -3.0), SQUARE)
    assert distance == pytest.approx(3.0)
    assert foot == pytest.approx((5.0, 0.0))


def test_perpendicular_distance_to_corner():
    distance, foot = off.perpendicular_distance((13.0, 14.0), SQUARE)
    assert distance == pytest.approx(5.0)
    assert foot == pytest.approx((10.0, 10.0))


def test_point_in_polygon():
    assert off.point_in_polygon((5.0, 5.0), SQUARE)
    assert not off.point_in_polygon((15.0, 5.0), SQUARE)


def test_measure_candidates_reports_vertex_distance_and_side():
    house = off.Feature(kind="house", handle="H1", points=((4.0, 12.0), (6.0, 12.0)))
    candidates = off.measure_candidates(SQUARE, [house])
    assert [c.distance_ft for c in candidates] == [2.0, 2.0]
    assert all(not c.inside for c in candidates)


def test_plan_offsets_skeleton_builds_no_items_and_warns():
    house = off.Feature(kind="house", handle="H1", points=((4.0, 12.0),))
    plan = off.plan_offsets(SQUARE, [house])
    assert plan.items == []
    assert any("не заданы" in w for w in plan.warnings)


def test_plan_offsets_needs_a_closed_boundary():
    plan = off.plan_offsets([(0.0, 0.0), (1.0, 0.0)], [])
    assert plan.items == []
    assert plan.warnings


def test_cli_prints_observed_table(capsys):
    assert off.main(["offsets.py", str(FINAL_ILION)]) == 0
    out = capsys.readouterr().out
    assert "Оффсетов в эталоне: 19" in out
    assert "BLDG 6.2'E" in out


def test_leaderless_text_offsets_are_style_text():
    # Brooklyn: «BLDG 5.2'W» и «5.2'E» без выноски — стиль text, не leader.
    items = {i.handle: i for i in off.parse_observed_offsets(_load(FINAL_BROOKLYN))}
    assert items["26804"].style == "text"
    assert items["26811"].style == "text"


def test_dimension_and_leader_styles_unchanged():
    items = {i.handle: i for i in off.parse_observed_offsets(_load(FINAL_ASPINWALL))}
    assert items["26627"].style == "dimension"
    assert items["26128"].style == "leader"


def test_step4_window_report_lists_stages_and_heights():
    text = off.step4_report_text(16)
    assert text.startswith("Шаг 4 — оффсеты")
    assert "1.100 ft" in text and "0.900 ft" in text
    assert "6. Проверка кадра и предупреждения" in text
    assert "1.375 ft" in off.step4_report_text(20)
