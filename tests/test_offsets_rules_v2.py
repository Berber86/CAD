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
    assert [item.text for item in plan.items] == ["FE.0.3'N\nFE.0.9'E"]


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


def _house_plan(far_ft: float):
    """Дом 2 ft от западной стороны, `far_ft` от восточной, фронт — южная сторона участка."""
    house = off.Feature(
        "house", "H",
        ((2.0, 3.0), (100.0 - far_ft, 3.0), (100.0 - far_ft, 20.0), (2.0, 20.0)),
        closed=True,
    )
    corners = [(2.0, 3.0), (100.0 - far_ft, 3.0), (100.0 - far_ft, 20.0), (2.0, 20.0)]
    return off.plan_offsets(
        SQUARE, [house], front=[[(0.0, 0.0), (100.0, 0.0)]],
        survey_pts=corners, survey_notes=[(p, "1 STY BRICK") for p in corners],
    )


def test_house_side_offset_goes_to_the_nearer_side_only():
    """
    R24 (4.36, ответ заказчика на вопрос 7): «даётся один боковой оффсет — на ту сторону, где
    расстояние до границы участка меньше. Но если больший оффсет имеет малое значение — меньше 5,
    то на вторую сторону тоже даётся оффсет».

    Дом в 2 ft от западной стороны и в 9 ft от восточной: подписи только у ближней боковой
    стороны (2.0'E) и у обоих фронтальных углов (3.0'N); дальней боковой (9.0'W) нет.
    """
    plan = _house_plan(9.0)
    texts = [item.text for item in plan.items]
    assert not any("9.0'W" in text for text in texts), texts
    assert sum(1 for text in texts if "3.0'N" in text) == 2, texts  # оба фронтальных
    assert sum(1 for text in texts if "2.0'E" in text) == 2, texts  # угол фасада + ближний боковой
    assert len(plan.items) == 3, texts


def test_house_second_side_is_added_when_the_larger_offset_is_under_five():
    """R24: больший боковой оффсет меньше 5 ft — подписывается и вторая боковая сторона."""
    plan = _house_plan(4.0)
    texts = [item.text for item in plan.items]
    assert any("4.0'W" in text for text in texts), texts
    assert len(plan.items) == 4, texts


def test_brick_column_is_named_like_the_customer_and_measured_to_the_far_face():
    """
    Вопрос 12 (ответ 19.11.2026): объект называется **BRICK COLUMN** (`BRICK COL.`, `BR.COL.`),
    а оффсет показывает, насколько колонна вылезла за границу участка, — меряется дальняя от
    линии дороги грань.
    """
    assert "BRICK COLUMN" in off.BRICK_COLUMN_WORDS
    assert "BRICK COL." in off.BRICK_COLUMN_WORDS and "BR.COL." in off.BRICK_COLUMN_WORDS
    # Колонна 1 × 1 ft стоит на западной линии участка в полосе улицы (как две колонны на
    # 1412 East 64th): ближняя к улице грань в 2 ft от красной линии, дальняя — в 3 ft.
    # Оффсет — 3.0 (насколько колонна вылезла за границу), а не 2.0 по ближней грани.
    front = ((-20.0, -4.0), (120.0, -4.0))
    column = off.Feature(
        kind="wall", handle="BR.COL.",
        points=((-1.0, -2.0), (0.0, -2.0), (0.0, -1.0), (-1.0, -1.0)),
    )
    plan = off.plan_offsets(
        SQUARE, [column], front=[front],
        survey_pts=[(0.0, -2.0), (-1.0, -1.0)],
        survey_notes=[((0.0, -2.0), "BRICK COL."), ((-1.0, -1.0), "BR.COL.")],
    )
    assert [item.text for item in plan.items] == ["3.0'N"], [(i.text, i.kind) for i in plan.items]
    assert plan.items[0].anchor == pytest.approx((-0.5, -1.0), abs=0.05)


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


def test_two_offsets_per_object_and_line_is_the_ceiling():
    """
    R23 (4.34, ответ заказчика 19.11.2026, вопрос 8): не более 2 оффсетов на объект и на одну
    и ту же линию границы участка.

    Потолок жёсткий — его не обходят ни перекос (`SKEW_PAIR_FT`, R13d), ни разнос углов вдоль
    стороны (`SIDE_CAP_SPACING_FT`, R13c). На шести парах он не срабатывает (подписи 78 и
    совпадения 47 до и после), но теперь он явный, а не следствие кэпа: у забора
    `MAX_CORNERS_PER_SIDE` = 1, у дома 2, а потолок — 2 на любую линию.
    """
    assert off.MAX_CORNERS_PER_SIDE_LIMIT == 2
    # Забор вдоль южной стороны: четыре снятых угла, два из них с разными замерами (перекос).
    square = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]
    fence = off.Feature(
        kind="fence", handle="F1",
        points=((5.0, 0.1), (40.0, 0.4), (70.0, 0.2), (95.0, 0.3)), closed=False,
    )
    data = off.plan_offsets(
        square, [fence], front=[], scale=16.0, north_deg=90.0,
        texts=[], survey_pts=((5.0, 0.1), (40.0, 0.4), (70.0, 0.2), (95.0, 0.3)),
        survey_notes=(),
    )
    labels = [item for item in data.items if item.kind == "fence"]
    assert 1 <= len(labels) <= off.MAX_CORNERS_PER_SIDE_LIMIT


def test_on_line_text_is_one_line_and_threshold_matches_the_customer():
    """
    R23 (4.34, ответ заказчика 19.11.2026, вопросы 9–10): `O/L` при значении 0.05 и менее; если
    оба замера `O/L` — одна строка; у дома и стены — подпись двумя строками (`BLDG` / `O/L`).
    """
    assert off.MIN_OFFSET_FT == pytest.approx(0.05)
    # забор на самой линии — одна строка с приставкой
    assert off.online_text("fence") == "FE.O/L"
    # дом и стена — две строки: тип, затем O/L (ответ на вопрос 10)
    assert off.online_text("house").splitlines() == ["BLDG", "O/L"]
    assert off.online_text("wall").splitlines() == ["WALL", "O/L"]
    # «оба замера O/L» дают ту же одну строку: второй O/L не дублируется
    assert off.online_text("fence").count("O/L") == 1


def test_whole_side_on_the_line_gets_one_line_label():
    """
    R25 (золотое правило №10, ответ заказчика 19.11.2026): «если для всей стороны дома — хоть для
    фронта, хоть для бочины дома — если он по обеим сторонам получается ``O/L``, то в одну строчку».

    Дом стоит западной стеной (50 ft) на линии участка: подпись — одна строка ``BLDG O/L`` простым
    текстом вдоль стены, без выноски и без числа, как у заказчика (378 Mother Gaston, 1412 East
    64th). Сняты только внутренние вершины, поэтому угловой подписи ``BLDG`` / ``O/L`` рядом нет и
    она не глушит сторону. Флаг по умолчанию выключен — правило замерено и не дало улучшения
    (``test_whole_side_on_the_line_rule_is_measured_and_off``).
    """
    house = off.Feature(
        kind="house", handle="H",
        points=((0.0, 10.0), (0.0, 60.0), (8.0, 60.0), (8.0, 10.0)),
    )
    inner = [(8.0, 60.0), (8.0, 10.0)]
    saved = off.ONLINE_SIDE_LABELS
    off.ONLINE_SIDE_LABELS = True
    try:
        plan = off.plan_offsets(SQUARE, [house], survey_pts=inner)
    finally:
        off.ONLINE_SIDE_LABELS = saved
    side = [item for item in plan.items if item.online_side]
    assert [item.text for item in side] == ["BLDG O/L"]
    item = side[0]
    assert item.style == "text" and item.leader is None  # простой текст, не выноска
    assert item.rotation_deg == pytest.approx(90.0, abs=0.01)  # вдоль стены (R21)
    assert item.point[1] == pytest.approx(35.0, abs=0.5)  # середина стены 10..60
    assert 0.0 < item.point[0] < 2.0  # внутри участка, в 1–2 ft от линии, как у заказчика
    # Флаг выключен — той же подписи нет.
    plain = off.plan_offsets(SQUARE, [house], survey_pts=inner)
    assert not [item for item in plain.items if item.online_side]


def test_side_label_is_not_given_to_boundary_trace_or_fence():
    """
    R25, фильтры ``online_side_walls``: контур, обведённый как объект, забор и короткий край
    стороной здания не считаются.

    В дампах заказчика граница иногда распознана как объект: 1373 Brooklyn «дом» 46.0 × 18.0 ×
    100.0 ft — все три ребра на трёх линиях участка, 24 Alvine «дом» 174.9 × 45.3 × 153.6 ft —
    тоже; подписей «в одну строчку» у него там нет. У забора форма однострочная и без R25, и
    ставит он её в точке пересечения (192 Geldner: ``FE.O/L`` в 2.8 ft от конца пролёта 59.2 ft).
    """
    sides = off.side_lines(SQUARE, [], 90.0)
    assert off.online_side_walls(off.Feature(kind="house", handle="H", points=tuple(SQUARE)), sides) == []
    fence = off.Feature(kind="fence", handle="F", points=((0.0, 10.0), (0.0, 60.0)))
    assert off.online_side_walls(fence, sides) == []
    # Край 15 ft < ONLINE_SIDE_MIN_FT — это угол, у угла форма двухстрочная.
    short = off.Feature(kind="house", handle="H", points=((0.0, 10.0), (0.0, 25.0), (8.0, 25.0), (8.0, 10.0)))
    assert off.online_side_walls(short, sides) == []
    # Здание на линии: одно ребро на линии, остальные внутри участка — самый длинный край на линию.
    house = off.Feature(kind="house", handle="H", points=((0.0, 10.0), (0.0, 60.0), (8.0, 60.0), (8.0, 10.0)))
    walls = off.online_side_walls(house, sides)
    assert len(walls) == 1
    assert walls[0][0] == ((0.0, 10.0), (0.0, 60.0))
    assert walls[0][1].letter == "W"


def test_label_without_number_is_not_a_part_of_the_neighbouring_label():
    """
    Подпись без числа — не «часть» соседней подписи (4.37).

    H32 снимает одиночную подпись, если её части входят в двухстрочную подпись того же объекта
    в 2 ft рядом. У подписи «на линии» (``FE.O/L``, ``BLDG O/L``) частей нет вовсе, а пустое
    множество — подмножество любого: без проверки ``own`` такая подпись снималась всегда, когда
    рядом стоял числовой столбик. Здесь вершина (2.0, 0.02) на линии в 1.0 ft от вершины (1.0, 0.3)
    с двумя замерами — до 4.37 ``FE.O/L`` исчезала («уже в подписи рядом»).
    """
    fence = off.Feature(kind="fence", handle="F", points=((1.0, 0.3), (2.0, 0.02), (60.0, 0.02)))
    points = [(1.0, 0.3), (2.0, 0.02), (60.0, 0.02)]
    plan = off.plan_offsets(SQUARE, [fence], survey_pts=points)
    texts = [item.text for item in plan.items]
    assert "FE.0.3'N\nFE.1.0'E" in texts
    assert "FE.O/L" in texts
    assert off._part_keys("FE.O/L") == set()


def test_fence_label_goes_across_the_lot_line_like_the_customer():
    """
    R26 (4.38): подпись забора стоит по другую сторону линии участка от объекта.

    У Николая 36 из 45 подписей забора — снаружи контура участка, край блока в 1.5–2.6 ft от
    линии (медиана 2.0); внутри — только у забора, который сам стоит снаружи линии. Забор
    в 0.2 ft внутри южной стороны: с правилом центр подписи уходит за линию, просвет от края
    блока до линии попадает в его полосу 1.4–4.6 ft; без правила блок прилипал к линии
    (просвет < 1.4) или оставался внутри.
    """
    fence = off.Feature(kind="fence", handle="F", points=((20.0, 0.2), (60.0, 0.2)))
    points = [(20.0, 0.2), (60.0, 0.2)]
    def gap_of(plan, vertex):
        item = min(plan.items, key=lambda it: math.dist(it.anchor, vertex))
        width, height = off._text_block(item.text, item.height_ft, lambda t, h: len(t) * h * 0.7)
        rect = off._rect_at(item.point, width, height)
        return item, min(abs(rect[1]), abs(rect[3]))  # южная линия участка — y = 0

    saved = off.FENCE_TEXT_ACROSS_LINE
    off.FENCE_TEXT_ACROSS_LINE = True
    try:
        plan = off.plan_offsets(SQUARE, [fence], survey_pts=points)
    finally:
        off.FENCE_TEXT_ACROSS_LINE = saved
    assert {item.text for item in plan.items} == {"FE.0.2'N"}
    item, gap = gap_of(plan, points[0])
    assert item.point[1] < 0.0  # объект внутри участка — текст снаружи линии
    assert off.FENCE_TEXT_LINE_GAP_FT[0] <= gap <= off.FENCE_TEXT_LINE_GAP_FT[1], gap
    # Без правила блок прилипает к линии: просвет меньше его полосы.
    saved = off.FENCE_TEXT_ACROSS_LINE
    off.FENCE_TEXT_ACROSS_LINE = False
    try:
        plain = off.plan_offsets(SQUARE, [fence], survey_pts=points)
    finally:
        off.FENCE_TEXT_ACROSS_LINE = saved
    plain_item, gap0 = gap_of(plain, points[0])
    assert gap0 < gap or plain_item.point[1] > 0.0, (gap0, gap)


def test_fence_label_falls_back_when_across_has_no_room():
    """
    R26 не теряет подпись: если снаружи линии места нет (там чужой текст), остаётся прежний
    круговой поиск выноски.
    """
    fence = off.Feature(kind="fence", handle="F", points=((20.0, 0.2), (60.0, 0.2)))
    points = [(20.0, 0.2), (60.0, 0.2)]
    blocker = [("NEIGHBOR TEXT BLOCK", (40.0, -2.0), 1.1), ("ANOTHER NEIGHBOR", (40.0, -3.5), 1.1)]
    plan = off.plan_offsets(SQUARE, [fence], survey_pts=points, texts=blocker)
    assert {item.text for item in plan.items} == {"FE.0.2'N"}
    assert len(plan.items) == 2  # оба угла подписаны, ни один не потерян из-за правила
    assert not plan.warnings
