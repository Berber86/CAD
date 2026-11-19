"""
Шаг 4, правила из примеров Alvine и Brooklyn (docs/step4_rules_from_examples.md, версия 2):
- значение — до продолжения стороны (R1), в пределах LINE_EXTENSION_FT;
- объект за границей на небольшом расстоянии тоже подписывается (OUTSIDE_MAX_FT);
- при одинаковом значении после округления предпочитаем вершину внутри участка;
- вторая сторона у той же вершины — второй строкой выноски (R4), только внутри самой стороны.
"""

import math

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
    assert [item.text for item in plan.items] == ["FE.0.3'N\nFE.1.0'E"]


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
    # Вершина (50, 0.2) — 0.2 ft внутри, вершина (99.98, 0.02) лежит на линии (0.02 < MIN_OFFSET).
    # Забор подписывается по вершине внутри участка, вершина на линии — «FE.O/L» (4.24).
    fence = _fence([(99.98, 0.02), (60.0, 0.02), (50.0, 0.2)])
    plan = off.plan_offsets(SQUARE, [fence])
    # Вершина внутри участка у южной стороны: объект севернее линии, буква N.
    assert "FE.0.2'N" in [item.text for item in plan.items]


def test_fence_entirely_on_the_line_gets_online_label():
    """Забор целиком на линии: у Николая там «FE.O/L» (Geldner), числа нет (4.24)."""
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


def test_front_wall_pier_is_measured_to_the_street_line():
    """
    R17 (4.30): устой у улицы — замер до красной линии, голым числом, с выноской.

    1412 East 64th Street: два кирпичных устоя 1 × 1 ft стоят на боковых линиях в полосе улицы
    (0.08 и 0.31 ft), до `M-ROW-LINE` — 2.98 и 3.06 ft; Николай пишет `3.0'E` дважды, началом
    выноски — середину задней грани устоя. Здесь тот же случай: устой 1 × 1 ft на западной
    стороне, в 3 ft перед красной линией.
    """
    front = ((-20.0, -4.0), (120.0, -4.0))  # красная линия идёт вдоль улицы, мимо участка
    pier = off.Feature(
        kind="wall", handle="P",
        points=((-1.0, -2.0), (0.0, -2.0), (0.0, -1.0), (-1.0, -1.0)),
    )
    plan = off.plan_offsets(
        SQUARE, [pier], front=[front], survey_pts=[(0.0, -2.0), (-1.0, -1.0)],
        survey_notes=[((0.0, -2.0), "BRICK WALL"), ((-1.0, -1.0), "BRICK WALL")],
    )
    assert [item.text for item in plan.items] == ["3.0'N"], [(i.text, i.kind) for i in plan.items]
    item = plan.items[0]
    assert item.kind == "wall" and item.style == "leader" and item.leader
    # Задняя грань устоя (дальняя от улицы — здесь это y = 1.0) и середина между её вершинами.
    assert item.anchor == pytest.approx((-0.5, -1.0), abs=0.05)
    assert item.value_ft == pytest.approx(3.0, abs=0.06)


def test_front_wall_needs_survey_points_and_a_street_line():
    """R17 без точек съёмки или без красной линии не включается: стена остаётся без оффсета."""
    pier = off.Feature(
        kind="wall", handle="P",
        points=((-1.0, -2.0), (0.0, -2.0), (0.0, -1.0), (-1.0, -1.0)),
    )
    front = ((-20.0, -4.0), (120.0, -4.0))
    assert off.plan_offsets(SQUARE, [pier], front=[front]).items == []
    assert off.plan_offsets(
        SQUARE, [pier], survey_pts=[(0.0, -2.0)],
        survey_notes=[((0.0, -2.0), "BRICK WALL")],
    ).items == []


def test_house_limit_is_a_fallback_not_a_blanket():
    """
    R18: расширенный предел дома — запасной, а не общий.

    Дом с одной стеной в 2 ft и другой в 3.5 ft подписывается только по ближней стене:
    расширение включается, лишь когда **все** стены дома дальше общего предела (иначе на
    плотных чертежах подписей дома вдвое больше, чем у Николая: 1373 East 55th, 6 → 10).
    """
    house = ((10.0, 2.0), (40.0, 2.0), (40.0, 40.0), (10.0, 40.0))  # стена в 2 ft от южной
    plan = off.plan_offsets(
        SQUARE, [off.Feature("house", "h", house, closed=True)],
        survey_pts=[(10.0, 2.0), (40.0, 2.0)],
        survey_notes=[((10.0, 2.0), "1 STY BRICK"), ((40.0, 2.0), "1 STY BRICK")],
    )
    house_items = [item for item in plan.items if item.kind == "house"]
    assert house_items and all(item.value_ft <= off.EDGE_MAX_FT + 1e-6 for item in house_items)


def test_direction_letters_switch_has_three_positions():
    """
    R20 (4.33, ответ заказчика 19.11.2026, вопрос 2): буквы направления — три положения.

    ``N`` — одна буква, ``NE`` — две, ``AUTO`` — программа решает по углу между севером (стрелка
    на чертеже) и линией дороги: 22.5…67.5° — двойная, иначе одна.

    По умолчанию стоит ``N``, а не ``AUTO``: на шести парах у Николая **ни одной** двойной буквы
    (82 подписи), а AUTO по этому правилу даёт 22 двойных подписи и теряет 13 совпадений из 47
    (192 Geldner 59.1°, 18 Zoe 46.4°, 1412 East 64th 40.1° — по его же листам там одинарные).
    Замер: `docs/step4_spatial_measure.py`.
    """
    assert off.DIRECTION_LETTERS in {"N", "AUTO", "NE"}
    assert off.DIRECTION_LETTERS == "N"
    # одна буква — как раньше
    assert off.compass_letters(0.0, 1.0, 90.0) == "N"
    assert off.compass_letters(-1.0, -0.5, 90.0) == "W"
    # две буквы: сначала N/S, потом E/W (вопрос 4)
    assert off.compass_letters(1.0, 1.0, 90.0, True) == "NE"
    assert off.compass_letters(-1.0, -1.0, 90.0, True) == "SW"
    # AUTO: угол между севером и линией дороги
    road_45 = [[(0.0, 0.0), (10.0, 10.0)]]
    road_10 = [[(0.0, 0.0), (10.0, 10.0 * math.tan(math.radians(10)))]]
    saved = off.DIRECTION_LETTERS
    try:
        off.DIRECTION_LETTERS = "AUTO"
        assert off.diagonal_letters(road_45, 90.0)
        assert not off.diagonal_letters(road_10, 90.0)
        off.DIRECTION_LETTERS = "NE"
        assert off.diagonal_letters(road_10, 90.0)
        off.DIRECTION_LETTERS = "N"
        assert not off.diagonal_letters(road_45, 90.0)
    finally:
        off.DIRECTION_LETTERS = saved
    # текст с двумя буквами разбирается на часть подписи (дедуп и счёт идут по частям)
    assert off._part_keys("0.4'NE") == {(0.4, "NE")}


def test_text_goes_along_the_object_not_the_lot_side():
    """
    R21 (4.33, ответ заказчика 19.11.2026, вопрос 5): подпись без выноски идёт вдоль объекта.

    Для забора и прямой стены это совпадает со стороной участка, поэтому на шести парах правило
    не меняет ни одной подписи (78 подписей / 47 совпадений до и после). Расходится у строения,
    стоящего не параллельно границе: тогда текст идёт вдоль стены, а не вдоль стороны участка.
    """
    assert off.TEXT_ROTATION_OBJECT is True
    angle = math.radians(30.0)
    a = (0.0, 0.0)
    b = (10.0 * math.cos(angle), 10.0 * math.sin(angle))
    house = off.Feature(kind="house", handle="H1", points=(a, b, (b[0], b[1] + 10.0), (a[0], 10.0)), closed=True)
    assert off._object_edge_angle(house, a, 0.0) == pytest.approx(30.0, abs=0.5)
    fence = off.Feature(kind="fence", handle="F1", points=((0.0, 0.0), (10.0, 0.0)), closed=False)
    assert off._object_edge_angle(fence, (0.0, 0.0), 0.0) == pytest.approx(0.0, abs=0.1)
