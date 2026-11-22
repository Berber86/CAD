"""
Шаг 4, черновик 4.3: геометрия оффсетов в выбранной области (без COM).

Участок — прямоугольник 100 × 50 ft, фронт (M-ROW-LINE) — нижняя сторона y = 0.
Правила заказчика: дом — текст без выноски (1.1), забор — выноска FE. (0.9); значение до 0.1 ft.
"""

import math

import pytest

import offsets as off
from acad_builder import format_bearing, format_dms_angle

RING = [(0.0, 0.0), (100.0, 0.0), (100.0, 50.0), (0.0, 50.0)]
FRONT = [[(0.0, 0.0), (100.0, 0.0)]]


def _house(x0=20.0, y0=10.0, x1=40.0, y1=30.0, handle="H1"):
    return off.Feature("house", handle, ((x0, y0), (x1, y0), (x1, y1), (x0, y1)))


def _fence(points, handle="F1", kind="fence"):
    return off.Feature(kind, handle, tuple(points))


def _inside_ring(point):
    return off.point_in_polygon(point, RING)


# ------------------------------------------------------------ значения и буквы


def test_round_offset_to_tenth_banker():
    """Геодезическое округление (5 в чётную): 0.05→0.0, 0.15→0.2, 0.25→0.2 и т.д."""
    assert off.round_offset_ft(0.44) == pytest.approx(0.4)   # 4 < 5 → вниз
    assert off.round_offset_ft(0.46) == pytest.approx(0.5)   # 6 > 5 → вверх
    assert off.round_offset_ft(14.649) == pytest.approx(14.6)  # 4 < 5 → вниз
    assert off.round_offset_ft(0.02) == pytest.approx(0.0)   # 2 < 5 → вниз
    # Сотая = 5, десятая чётная — остаёмся:
    assert off.round_offset_ft(0.05) == pytest.approx(0.0)   # 0.0 (O/L)
    assert off.round_offset_ft(0.25) == pytest.approx(0.2)   # 2 — чётная
    assert off.round_offset_ft(0.45) == pytest.approx(0.4)   # 4 — чётная
    assert off.round_offset_ft(0.65) == pytest.approx(0.6)   # 6 — чётная
    assert off.round_offset_ft(0.85) == pytest.approx(0.8)   # 8 — чётная
    # Сотая = 5, десятая нечётная — округляем до чётной:
    assert off.round_offset_ft(0.15) == pytest.approx(0.2)   # 1 → 2
    assert off.round_offset_ft(0.35) == pytest.approx(0.4)   # 3 → 4
    assert off.round_offset_ft(0.55) == pytest.approx(0.6)   # 5 → 6
    assert off.round_offset_ft(0.75) == pytest.approx(0.8)   # 7 → 8
    assert off.round_offset_ft(0.95) == pytest.approx(1.0)   # 9 → 1.0


def test_compass_letters_with_default_north_is_plus_y():
    # вектор от объекта к границе: вниз — S, вверх — N, вправо — E, влево — W
    assert off.compass_letter(0.0, -1.0) == "S"
    assert off.compass_letter(0.0, 1.0) == "N"
    assert off.compass_letter(1.0, 0.0) == "E"
    assert off.compass_letter(-1.0, 0.0) == "W"


def test_compass_letters_follow_north_direction():
    # если север повёрнут на 0° (к +X), вектор вправо — это север
    assert off.compass_letter(1.0, 0.0, north_deg=0.0) == "N"
    assert off.compass_letter(0.0, 1.0, north_deg=0.0) == "W"


def test_offset_text_forms_match_customer_examples():
    assert off.offset_text("fence", 0.4, "N") == "FE.0.4'N"
    assert off.offset_text("wall", 0.5, "E") == "WALL 0.5'E"
    assert off.offset_text("house", 3.2, "S") == "3.2'S"


def test_fence_offset_special_round_to_0_9():
    """Политически значимое округление заборов: 0.86–1.15 ft → 0.9 ft (title company)."""
    assert off.offset_text("fence", 0.86, "N") == "FE.0.9'N"
    assert off.offset_text("fence", 0.90, "N") == "FE.0.9'N"
    assert off.offset_text("fence", 0.95, "N") == "FE.0.9'N"
    assert off.offset_text("fence", 1.00, "N") == "FE.0.9'N"
    assert off.offset_text("fence", 1.15, "N") == "FE.0.9'N"
    # Вне диапазона — обычное геодезическое округление:
    assert off.offset_text("fence", 0.85, "N") == "FE.0.8'N"   # 8 — чётная
    assert off.offset_text("fence", 1.16, "N") == "FE.1.2'N"   # 6 > 5 → вверх
    assert off.offset_text("fence", 0.84, "N") == "FE.0.8'N"   # 4 < 5 → вниз
    # Не-заборы не подвержены специальному округлению (значения уже округлены round_offset_ft):
    assert off.offset_text("wall", 1.0, "E") == "WALL 1.0'E"   # 0.95 → 1.0 (банкерское)
    assert off.offset_text("house", 1.0, "S") == "1.0'S"       # 0.95 → 1.0 (банкерское)
    assert off.offset_text("wall", 0.8, "E") == "WALL 0.8'E"   # 0.85 → 0.8 (банкерское)


# ------------------------------------------------------------ план


def test_house_gets_text_without_leader_at_house_height():
    plan = off.plan_offsets(RING, [_house()], front=FRONT, scale=16)
    assert [i.text for i in plan.items] == ["10.0'N"]
    item = plan.items[0]
    assert item.style == "text"
    assert item.leader is None
    assert item.height_ft == pytest.approx(1.1)
    assert item.anchor == (20.0, 10.0)
    assert item.target == pytest.approx((20.0, 0.0))
    assert _inside_ring(item.point)


def test_fence_gets_leader_and_fence_prefix():
    plan = off.plan_offsets(RING, [_fence([(60.0, 5.0), (60.0, 20.0)])], front=FRONT, scale=16)
    assert len(plan.items) == 1
    item = plan.items[0]
    assert item.text == "FE.5.0'N"
    assert item.style == "leader"
    assert item.height_ft == pytest.approx(0.9)
    tip, end = item.leader
    assert tip == (60.0, 5.0)
    assert _inside_ring(item.point)
    assert math.hypot(end[0] - tip[0], end[1] - tip[1]) >= off.LEADER_MIN_HEIGHTS * 0.9


def test_nearest_boundary_decides_direction():
    # дальний забор на y = 30: до фронта 30 ft, до задней границы (y = 50) 20 ft; забор южнее задней линии → S
    plan = off.plan_offsets(RING, [_fence([(60.0, 30.0), (80.0, 30.0)])], front=FRONT)
    assert [i.text for i in plan.items] == ["FE.20.0'S"]


def test_only_objects_in_area_are_labelled_and_others_are_silent():
    near = _fence([(60.0, 5.0), (60.0, 20.0)], handle="NEAR")
    far = _fence([(10.0, 40.0), (30.0, 40.0)], handle="FAR")
    area = [(50.0, 0.0), (70.0, 0.0), (70.0, 25.0), (50.0, 25.0)]
    plan = off.plan_offsets(RING, [near, far], front=FRONT, area=area)
    assert [i.text for i in plan.items] == ["FE.5.0'N"]
    assert all(reason != "объект вне участка" for _, reason in plan.skipped)


def test_object_outside_property_is_skipped_with_reason():
    outside = _fence([(120.0, 5.0), (130.0, 5.0)], handle="OUT")
    plan = off.plan_offsets(RING, [outside], front=FRONT)
    assert plan.items == []
    assert ("OUT", "объект вне участка") in plan.skipped


def test_object_on_boundary_gets_online_label_not_a_number():
    """Объект на самой линии участка подписывается «FE.O/L», числа у него нет (4.24)."""
    on_line = _fence([(40.0, 0.0), (50.0, 0.0)], handle="EDGE")
    plan = off.plan_offsets(RING, [on_line], front=FRONT)
    assert [item.text for item in plan.items] == ["FE.O/L"]
    assert plan.items[0].value_ft == 0.0


def test_unsupported_kind_is_reported_inside_area_only():
    drive = off.Feature("driveway", "D1", ((30.0, 5.0), (35.0, 5.0)))
    area = [(0.0, 0.0), (100.0, 0.0), (100.0, 50.0), (0.0, 50.0)]
    plan = off.plan_offsets(RING, [drive], front=FRONT, area=area)
    assert plan.items == []
    assert plan.skipped == [("D1", "этот вид объекта в оффсетах не подписывается")]


def test_same_text_nearby_is_not_duplicated():
    existing = [("10.0'N", (22.0, 5.0), 1.1)]
    plan = off.plan_offsets(RING, [_house()], front=FRONT, texts=existing)
    assert plan.items == []
    assert any("уже стоит рядом" in reason for _, reason in plan.skipped)


def test_text_does_not_cover_existing_label():
    # стоящая подпись там, где пошёл бы текст дома: текст ставится в другое место
    blocker = [("XXXXXXXXXX", (22.3, 5.0), 1.1)]
    plan = off.plan_offsets(RING, [_house()], front=FRONT, texts=blocker, scale=16)
    assert len(plan.items) == 1
    item = plan.items[0]
    assert item.point != pytest.approx((22.31, 5.0), abs=0.05)


def test_leader_does_not_cross_building_outline():
    # забор с выноской не должен рисовать текст поверх дома
    house = _house(x0=50.0, y0=10.0, x1=70.0, y1=30.0, handle="H2")
    fence = _fence([(45.0, 5.0), (45.0, 8.0)], handle="F2")
    plan = off.plan_offsets(RING, [house, fence], front=FRONT)
    fence_items = [i for i in plan.items if i.style == "leader"]
    assert fence_items, plan.warnings
    x, y = fence_items[0].point
    assert not (50.0 - 2.0 <= x <= 70.0 + 2.0 and 10.0 - 1.0 <= y <= 30.0 + 1.0)


def test_no_room_gives_warning_not_silent_drop():
    # очень маленькая область: текст не помещается, программа предупреждает
    # область содержит вершину забора (60, 5), но текст «FE.5.0'N» в ней не умещается
    area = [(59.0, 4.0), (61.0, 4.0), (61.0, 6.0), (59.0, 6.0)]
    plan = off.plan_offsets(RING, [_fence([(60.0, 5.0), (60.0, 20.0)])], front=FRONT, area=area)
    assert plan.items == []
    assert any("нет свободного места" in w for w in plan.warnings)


def test_small_polygon_is_refused_with_warning():
    plan = off.plan_offsets([(0.0, 0.0), (1.0, 0.0)], [_house()])
    assert plan.items == [] and plan.warnings


def test_closing_point_in_ring_is_ignored():
    closed = RING + [RING[0]]
    plan = off.plan_offsets(closed, [_house()], front=FRONT)
    assert [i.text for i in plan.items] == ["10.0'N"]


def test_report_text_lists_items_and_skips():
    plan = off.plan_offsets(
        RING,
        [_house(), _fence([(60.0, 5.0), (60.0, 20.0)]), _fence([(40.0, 0.0), (50.0, 0.0)], handle="E")],
        front=FRONT,
    )
    text = off.plan_report_text(plan, 16)
    assert f"черновик {off.STEP4_VERSION}" in text
    assert "Подписей: 3 (без выноски: 1, с выноской: 2)" in text
    assert "10.0'N  (без выноски)" in text
    assert "FE.O/L  (выноска)" in text


def test_scale_changes_heights_only_not_values():
    small = off.plan_offsets(RING, [_house()], front=FRONT, scale=20)
    assert small.items[0].text == "10.0'N"
    assert small.items[0].height_ft == pytest.approx(1.375)


def test_bearing_degrees_minutes_seconds_are_two_digits():
    """Градусы, минуты и секунды в беринге — всегда две цифры с ведущим нулём."""
    from llm_parser import Segment

    # Все значения > 9 — без изменений:
    assert format_bearing(Segment(ns="N", degrees=14, minutes=39, seconds=40, ew="W", distance_ft=100)) == "N 14%%d39'40\" W"
    assert format_bearing(Segment(ns="N", degrees=14, minutes=39, seconds=40, ew="W", distance_ft=100), use_control_code=False) == "N 14°39'40\" W"

    # Значения 0–9 — с ведущим нулём, секунды 0 → 00":
    assert format_bearing(Segment(ns="N", degrees=4, minutes=9, seconds=0, ew="W", distance_ft=100)) == "N 04%%d09'00\" W"
    assert format_bearing(Segment(ns="N", degrees=4, minutes=9, seconds=0, ew="W", distance_ft=100), use_control_code=False) == "N 04°09'00\" W"

    # Ноль градусов:
    assert format_bearing(Segment(ns="S", degrees=0, minutes=5, seconds=3.5, ew="E", distance_ft=100)) == "S 00%%d05'03.5\" E"

    # format_dms_angle тоже дополняет градусы до двух цифр:
    assert format_dms_angle(4.25) == "04%%d15'00\""
    assert format_dms_angle(4.25, use_control_code=False) == "04°15'00\""
    assert format_dms_angle(0.5) == "00%%d30'00\""
