"""
Шаг 4, 4.21: что взято из двух параллельных веток (4.20 и 4.26) и проверено на своих эталонах.

Каждое правило проверяется на синтетическом чертеже: важно не только «работает», но и что
выключение возвращает прежнее поведение (иначе правило нельзя будет снять).
"""

import math

import pytest

import offsets as off


LOT = [(0.0, 0.0), (60.0, 0.0), (60.0, 100.0), (0.0, 100.0)]


def _items(plan):
    return [item for item in plan.items if item.kind == "fence"]


def test_crossing_label_at_extension_of_side_where_the_fence_passes_the_corner():
    """
    H27: забор в 0.7 ft восточнее восточной границы пересекает линию южной — подпись
    в точке пересечения, замер до параллельной (восточной) стороны. 192 Geldner: FE.0.7'E.
    """
    fence = off.Feature("fence", "f1", ((60.7, -10.0), (60.7, 50.0)))
    pts = [(60.7, -10.0), (60.7, 0.0), (60.7, 50.0)]
    plan = off.plan_offsets(LOT, [fence], survey_pts=pts,
                            survey_notes=[(p, "VINYL FENCE") for p in pts])
    texts = [(item.text, item.crossing) for item in _items(plan)]
    assert ("FE.0.7'E", True) in texts, texts


def test_crossing_label_needs_both_ends_outside_the_lot():
    """Забор, у которого один конец внутри участка, — не обход угла: подписи в пересечении нет."""
    fence = off.Feature("fence", "f1", ((60.7, -10.0), (60.7, 50.0)))
    pts = [(60.7, -10.0), (60.7, 0.0), (60.7, 50.0)]
    plan = off.plan_offsets(LOT, [fence], survey_pts=pts,
                            survey_notes=[(p, "VINYL FENCE") for p in pts])
    inside = off.Feature("fence", "f2", ((30.0, 0.2), (54.0, 0.2)))
    plan2 = off.plan_offsets(
        LOT, [inside], survey_pts=[(30.0, 0.2), (54.0, 0.2)],
        survey_notes=[((30.0, 0.2), "VINYL FENCE"), ((54.0, 0.2), "VINYL FENCE")],
    )
    assert plan.items
    assert not [item for item in plan2.items if item.crossing]


def test_long_fence_end_is_labeled_up_to_six_feet_from_the_side():
    """
    H33: конец длинного прогона забора подписывается и в 5.2 ft от стороны (192 Geldner).
    Короткий 6-футовый выступ с таким же вылетом — нет (Brooklyn).
    """
    pts = [(10.0, -5.2), (10.0, 40.0)]
    long_fence = off.Feature("fence", "long", tuple(pts))
    plan = off.plan_offsets(LOT, [long_fence], survey_pts=pts,
                            survey_notes=[(p, "VINYL FENCE") for p in pts])
    assert "FE.5.2'S" in [item.text for item in _items(plan)]

    spur = [(20.0, -5.3), (20.0, 0.7)]
    short_fence = off.Feature("fence", "short", tuple(spur))
    plan = off.plan_offsets(LOT, [short_fence], survey_pts=spur,
                            survey_notes=[(p, "VINYL FENCE") for p in spur])
    assert not [item for item in _items(plan) if item.value_ft > off.EDGE_MAX_FT]


def test_area_equal_to_lot_keeps_labels_next_to_the_boundary():
    """
    Выделение полилинии участка: забор в 0.3 ft снаружи не должен выпадать из области —
    иначе на 192 Geldner оставалось 5 подписей вместо 14.
    """
    pts = [(0.3, 20.0), (0.3, 60.0)]
    fence = off.Feature("fence", "f1", tuple(pts))
    common = dict(survey_pts=pts, survey_notes=[(p, "VINYL FENCE") for p in pts])
    whole = off.plan_offsets(LOT, [fence], **common)
    ring_area = off.plan_offsets(LOT, [fence], area=LOT, **common)
    assert [i.text for i in whole.items] == [i.text for i in ring_area.items]
    assert ring_area.items


def test_diagonal_side_letter_follows_sheet_axes():
    """
    Наклонная восточная сторона 24 Alvine: Николай пишет 1.3'W и 1.4'E, хотя при севере
    67.6° «ближайшая ось к северу» дала бы N/S.
    """
    ring = [(0.0, 0.0), (0.0, 40.0), (153.6, 40.0), (174.9, 0.0)]
    sides = off.side_lines(ring, [], 67.563)
    diagonal = [side for side in sides if abs(side.normal[0]) > 0.5 and abs(side.normal[1]) > 0.3]
    assert diagonal, "наклонной стороны нет"
    assert diagonal[0].letter == "E"

    saved = off.DIAGONAL_SHEET_AXES
    off.DIAGONAL_SHEET_AXES = False
    try:
        off_sides = off.side_lines(ring, [], 67.563)
    finally:
        off.DIAGONAL_SHEET_AXES = saved
    assert [s.letter for s in off_sides if abs(s.normal[0]) > 0.5 and abs(s.normal[1]) > 0.3] == ["N"]


def test_open_house_contour_is_not_closed():
    """
    П-образный контур здания: замыкающая «стена» стеной не является — у концов ломаной
    один край, а не два, и оффсет по несуществующей стене не ставится.
    """
    u_shape = ((10.0, 0.5), (10.0, 20.0), (40.0, 20.0), (40.0, 0.5))
    opened = off.Feature("house", "open", u_shape, closed=False)
    closed = off.Feature("house", "closed", u_shape, closed=True)
    pts, opened_flag = off.feature_contour(opened)
    _pts, closed_flag = off.feature_contour(closed)
    assert opened_flag is False and closed_flag is True
    open_dirs = off.feature_edge_directions(opened)
    closed_dirs = off.feature_edge_directions(closed)
    assert len(open_dirs[(10.0, 0.5)]) == 1
    assert len(closed_dirs[(10.0, 0.5)]) == 2
    # По умолчанию (closed не задан) дом, как и раньше, замыкается.
    _pts, default_flag = off.feature_contour(off.Feature("house", "h", u_shape))
    assert default_flag is True
    _pts, fence_flag = off.feature_contour(off.Feature("fence", "f", u_shape))
    assert fence_flag is False


def test_repeated_closing_vertex_keeps_both_edges_of_the_corner():
    """Повтор первой вершины в конце не должен отнимать у угла второй край (R11)."""
    square = [(10.0, 0.5), (30.0, 0.5), (30.0, 20.0), (10.0, 20.0), (10.0, 0.5)]
    directions = off.feature_edge_directions(off.Feature("house", "h", square, closed=True))
    assert len(directions[(10.0, 0.5)]) == 2


def test_fence_label_always_goes_with_leader():
    """У Николая каждая подпись забора — выноска со стрелкой (тип 3), не голый текст."""
    pts = [(20.0, 0.4), (50.0, 0.4)]
    fence = off.Feature("fence", "f", tuple(pts))
    plan = off.plan_offsets(LOT, [fence], survey_pts=pts,
                            survey_notes=[(p, "VINYL FENCE") for p in pts])
    items = _items(plan)
    assert items and all(item.style == "leader" for item in items)
    assert all(item.leader for item in items)


def test_part_of_a_full_label_is_not_repeated_beside_it():
    """H32: «1.2'S» отдельной подписью рядом с «0.1'E / 1.2'S» — дубль."""
    fence = off.Feature("fence", "f", ((60.2, 40.0), (60.2, 60.0)))
    plan = off.plan_offsets(
        LOT, [fence], survey_pts=[(60.2, 40.0), (60.2, 60.0)],
        survey_notes=[((60.2, 40.0), "VINYL FENCE"), ((60.2, 60.0), "VINYL FENCE")],
    )
    texts = [item.text.replace("\n", " ") for item in _items(plan)]
    parts = [text.split() for text in texts]
    assert sum(len(p) for p in parts) == len(set(c for p in parts for c in p)), texts


# ---------------------------------------------------------------- раскладка (4.22)
def _rect(item):
    width, height = off._text_block(item.text, item.height_ft, off.default_text_width)
    return off._rect_at(item.point, width, height)


def test_two_line_label_repeats_prefix_but_not_the_type_name():
    """
    «FE.» стоит в каждой строке («FE.0.4'N» / «FE.1.3'W»), имя типа — только в первой
    («BLDG» / «2.5'S», «C.L.F.» / «0.6'S»): так в дампах по трём чертежам.
    """
    # Вершина у юго-восточного угла участка: замер до восточной стороны (0.2 ft)
    # и второй — до южной (0.4 ft).
    fence = off.Feature("fence", "f", ((60.2, 0.4), (60.2, 30.0)))
    plan = off.plan_offsets(
        LOT, [fence], survey_pts=[(60.2, 0.4), (60.2, 30.0)],
        survey_notes=[((60.2, 0.4), "VINYL FENCE"), ((60.2, 30.0), "VINYL FENCE")],
    )
    two_line = [item for item in _items(plan) if "\n" in item.text]
    assert two_line, [item.text for item in plan.items]
    for item in two_line:
        first, *rest = item.text.split("\n")
        assert first.startswith("FE.")
        assert rest and all(line.startswith("FE.") for line in rest), item.text


def test_fence_on_the_line_is_marked_online_instead_of_a_number():
    """
    Забор, стоящий на самой линии участка, подписывается «FE.O/L», а не числом:
    Geldner ``FE.O/L`` — забор на контуре, Brooklyn ``BR.WALL`` / ``O/L`` — стена.
    """
    fence = off.Feature("fence", "f", ((0.0, 10.0), (0.0, 30.0)))
    pts = [(0.0, 10.0), (0.0, 30.0)]
    plan = off.plan_offsets(
        LOT, [fence], survey_pts=pts,
        survey_notes=[(p, "VINYL FENCE") for p in pts],
    )
    texts = [item.text for item in plan.items]
    assert "FE.O/L" in texts, texts
    assert not [text for text in texts if text.startswith("FE.0.")], texts

    saved = off.ONLINE_LABELS
    off.ONLINE_LABELS = False
    try:
        off_plan = off.plan_offsets(
            LOT, [fence], survey_pts=pts,
            survey_notes=[(p, "VINYL FENCE") for p in pts],
        )
    finally:
        off.ONLINE_LABELS = saved
    assert off_plan.items == []


def test_house_on_the_street_line_is_marked_online_only_there():
    """
    У дома «BLDG» / «O/L» стоит только на фронтовой линии (378 Mother Gaston: в углу на
    `M-ROW-LINE`), а на боковой линии участка Николай пишет число (``BLDG`` / ``0.2'S``).
    """
    front = [((0.0, 0.0), (60.0, 0.0))]
    house = off.Feature(
        "house", "h",
        ((10.0, 0.0), (30.0, 0.0), (30.0, 20.0), (10.0, 20.0)),
        closed=True,
    )
    pts = [(10.0, 0.0), (30.0, 0.0), (30.0, 20.0), (10.0, 20.0)]
    plan = off.plan_offsets(LOT, [house], front=front, survey_pts=pts)
    texts = [item.text for item in plan.items]
    assert "BLDG\nO/L" in texts, texts

    # Дом в 0.2 ft от боковой (западной) линии: там подпись числом, «O/L» не ставится —
    # в Gaston у такого угла стоит «BLDG» / «0.2'S», а не «O/L».
    side_house = off.Feature(
        "house", "h2",
        ((0.2, 10.0), (20.2, 10.0), (20.2, 30.0), (0.2, 30.0)),
        closed=True,
    )
    side_pts = [(0.2, 10.0), (20.2, 10.0), (20.2, 30.0), (0.2, 30.0)]
    side_plan = off.plan_offsets(LOT, [side_house], front=front, survey_pts=side_pts)
    side_texts = [item.text for item in side_plan.items]
    assert not [text for text in side_texts if "O/L" in text], side_texts
    assert [text for text in side_texts if "'E" in text], side_texts


def test_line_block_height_matches_mtext_spacing():
    """Габарит блока считается с шагом строк MText, иначе двухстрочная подпись «выше» своей рамки."""
    _width, height = off._text_block("FE.0.2'S\n0.3'W", 1.0, off.default_text_width)
    assert height == pytest.approx(1.0 + off.TEXT_LINE_SPACING, abs=1e-9)


def test_placed_labels_keep_a_clearance_between_them():
    """Подписи не сходятся вплотную: сравниваются прямоугольники, расширенные на зазор."""
    # Два разных замера рядом (0.3 и 0.6 ft): одинаковый текст отсекает R14, здесь же
    # проверяется только зазор между блоками.
    first = off.Feature("fence", "f1", ((20.0, 0.3), (24.0, 0.3)))
    second = off.Feature("fence", "f2", ((24.4, 0.6), (28.0, 0.6)))
    pts = [(20.0, 0.3), (24.0, 0.3), (24.4, 0.6), (28.0, 0.6)]
    saved_cap = off.MAX_CORNERS_PER_SIDE
    off.MAX_CORNERS_PER_SIDE = 0  # кэп «один угол на сторону» снят: проверяется только зазор
    try:
        plan = off.plan_offsets(
            LOT, [first, second], survey_pts=pts,
            survey_notes=[(p, "VINYL FENCE") for p in pts],
        )
    finally:
        off.MAX_CORNERS_PER_SIDE = saved_cap
    items = _items(plan)
    assert len(items) >= 2
    clearance = off.LABEL_CLEARANCE * items[0].height_ft
    rects = [_rect(item) for item in items]
    for i, left in enumerate(rects):
        for right in rects[i + 1:]:
            assert not off._rects_overlap(
                off._grow_rect(left, clearance * 0.9),
                off._grow_rect(right, clearance * 0.9),
            )


def test_house_text_stands_inside_the_building():
    """Подпись дома — внутри строения у стены, как у Николая, а не снаружи в заборах."""
    house = ((10.0, 0.5), (34.0, 0.5), (34.0, 26.0), (10.0, 26.0))
    pts = [(10.0, 0.5), (34.0, 0.5)]
    plan = off.plan_offsets(
        LOT, [off.Feature("house", "h", house, closed=True)], survey_pts=pts,
        survey_notes=[(p, "1 STY FRAME") for p in pts],
    )
    assert plan.items
    assert all(off.point_in_polygon(item.point, house) for item in plan.items), [
        item.point for item in plan.items
    ]

    saved = off.HOUSE_TEXT_INSIDE
    off.HOUSE_TEXT_INSIDE = False
    try:
        outside = off.plan_offsets(
            LOT, [off.Feature("house", "h", house, closed=True)], survey_pts=pts,
            survey_notes=[(p, "1 STY FRAME") for p in pts],
        )
    finally:
        off.HOUSE_TEXT_INSIDE = saved
    assert outside.items and not all(
        off.point_in_polygon(item.point, house) for item in outside.items
    )
