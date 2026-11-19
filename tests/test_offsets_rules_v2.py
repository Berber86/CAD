"""
Шаг 4, правила из примеров Alvine и Brooklyn (docs/step4_rules_from_examples.md, версия 2):
- значение — до продолжения стороны (R1), в пределах LINE_EXTENSION_FT;
- объект за границей на небольшом расстоянии тоже подписывается (OUTSIDE_MAX_FT);
- при одинаковом значении после округления предпочитаем вершину внутри участка;
- вторая сторона у той же вершины — второй строкой выноски (R4), только внутри самой стороны.
"""

import pytest

import offsets as off

SQUARE = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]


def _fence(points, handle="F"):
    return off.Feature(kind="fence", handle=handle, points=tuple(points))


def test_distance_is_to_side_extension_beyond_corner():
    # Точка (13, 14) за углом (10, 10): до продолжения правой стороны 3.0, а не до угла 5.0.
    ten = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    distance, foot = off.perpendicular_line_distance((13.0, 14.0), ten, closed=True)
    assert distance == pytest.approx(3.0)
    assert foot == pytest.approx((10.0, 14.0))


def test_distance_extension_is_limited_then_falls_back_to_segment():
    # Основание на продолжении стороны дальше LINE_EXTENSION_FT: берём расстояние до самого отрезка.
    distance, _foot = off.perpendicular_line_distance((8.0, 1.0), [(0.0, 0.0), (1.0, 0.0)], closed=False)
    assert distance == pytest.approx(((8.0 - 1.0) ** 2 + 1.0) ** 0.5)


def test_object_just_outside_boundary_is_labelled_with_side():
    # Забор на 0.2 ft south of the south side: объект снаружи, буква S (как у заказчика «FE.0.2'S»).
    fence = _fence([(50.0, -0.2), (80.0, -0.2)])
    plan = off.plan_offsets(SQUARE, [fence])
    assert [item.text for item in plan.items] == ["FE.0.2'S"]


def test_two_sides_of_one_vertex_are_two_lines_of_one_label():
    # Вершина (1.0, 0.3) у угла: 0.3 до южной стороны (N) и 1.0 до западной (E). Один забор.
    fence = _fence([(1.0, 0.3), (30.0, 0.3)])
    plan = off.plan_offsets(SQUARE, [fence])
    assert [item.text for item in plan.items] == ["FE.0.3'N\n1.0'E"]


def test_second_side_is_not_added_beyond_threshold():
    fence = _fence([(50.0, 3.0), (80.0, 3.0)])
    plan = off.plan_offsets(SQUARE, [fence])
    assert [item.text for item in plan.items] == ["FE.3.0'N"]


def test_tie_after_rounding_prefers_vertex_inside_lot():
    # Вершины (50, -0.066) снаружи и (50, 0.088) внутри: оба дают 0.1. Берём внутреннюю: «FE.0.1'N».
    fence = _fence([(50.0, -0.066), (50.0, 0.088)])
    plan = off.plan_offsets(SQUARE, [fence])
    assert [item.text for item in plan.items] == ["FE.0.1'N"]


def test_vertex_on_the_line_is_ignored_not_the_whole_fence():
    # Вершина (99.98, 0.02) лежит на линии (0.02 < MIN_OFFSET), вершина (50, 0.2) — 0.2 ft внутри.
    # Раньше забор целиком отбрасывался как «стоит на границе»; теперь подписывается по второй вершине.
    fence = _fence([(99.98, 0.02), (60.0, 0.02), (50.0, 0.2)])
    plan = off.plan_offsets(SQUARE, [fence])
    # Вершина внутри участка у южной стороны: объект севернее линии, буква N.
    assert [item.text for item in plan.items] == ["FE.0.2'N", "FE.O/L"]


def test_fence_entirely_on_the_line_gets_fe_o_l_since_4_23():
    # 4.23: забор целиком на линии не отбрасывается, а подписывается «FE.O/L».
    fence = _fence([(10.0, 0.01), (60.0, 0.02)])
    plan = off.plan_offsets(SQUARE, [fence])
    assert [item.text for item in plan.items] == ["FE.O/L"]


def test_label_may_stand_outside_the_lot_flag():
    # Заказчик ставит подписи в яру, снаружи участка (4 из 8 на Brooklyn). Правило включено флагом.
    assert off.LABEL_MUST_BE_INSIDE is False


def test_walls_are_not_labelled_by_default():
    # 4.14: стены не подписываются оффсетом. У Николая в эталонах оффсетов по стенам нет (0 подписей),
    # а мы ставим лишние — по критерию «лишних не больше 50% от его подписей» правило не проходит.
    wall = off.Feature(kind="wall", handle="W", points=((50.0, -2.0), (80.0, -2.0)))
    plan = off.plan_offsets(SQUARE, [wall])
    assert plan.items == []


def test_walls_labelled_when_switched_on(monkeypatch):
    monkeypatch.setattr(off, "WALL_OFFSETS", True)
    wall = off.Feature(kind="wall", handle="W", points=((50.0, -2.0), (80.0, -2.0)))
    plan = off.plan_offsets(SQUARE, [wall])
    assert [item.text for item in plan.items] == ["WALL 2.0'S"]


def test_each_house_corner_gets_its_own_label_by_parallel_side(monkeypatch):
    monkeypatch.setattr(off, "CORNER_LABELS", True)
    # Дом у западной стороны: два угла на разном расстоянии (2.3 и 2.1 — как у Николая в Brooklyn).
    # Каждый угол подписывается до параллельной стороны участка, а не только ближайший.
    rect = [(0.0, 0.0), (100.0, 0.0), (100.0, 50.0), (0.0, 50.0)]
    house = off.Feature("house", "H2", ((2.3, 10.0), (10.0, 10.0), (10.0, 20.0), (2.1, 20.0)))
    plan = off.plan_offsets(rect, [house], front=[[(0.0, 0.0), (100.0, 0.0)]])
    values = sorted(item.value_ft for item in plan.items if item.anchor[0] < 3.0)
    assert values == [2.1, 2.3]


def test_corner_rule_keeps_one_label_for_both_ends_of_a_straight_fence(monkeypatch):
    monkeypatch.setattr(off, "CORNER_LABELS", True)
    # Два конца прямого забора на одинаковом расстоянии от стороны — одна подпись, не две.
    plan = off.plan_offsets(SQUARE, [_fence([(50.0, -0.066), (50.0, 0.088)])])
    assert len(plan.items) == 1
