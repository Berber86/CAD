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


def test_fence_label_is_text_without_leader_when_there_is_room():
    """У Николая подписи заборов — обычный текст у линии, без стрелок."""
    pts = [(20.0, 0.4), (50.0, 0.4)]
    fence = off.Feature("fence", "f", tuple(pts))
    plan = off.plan_offsets(LOT, [fence], survey_pts=pts,
                            survey_notes=[(p, "VINYL FENCE") for p in pts])
    items = _items(plan)
    assert items and all(item.style == "text" for item in items)

    saved = off.FENCE_TEXT_WITHOUT_LEADER
    off.FENCE_TEXT_WITHOUT_LEADER = False
    try:
        off_plan = off.plan_offsets(LOT, [fence], survey_pts=pts,
                                    survey_notes=[(p, "VINYL FENCE") for p in pts])
    finally:
        off.FENCE_TEXT_WITHOUT_LEADER = saved
    assert all(item.style == "leader" for item in _items(off_plan))


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
