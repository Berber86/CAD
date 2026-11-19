"""Шаг 4, правила 4.22: H34–H36 — дом подписывается по стенам, а не одной ближайшей вершиной.

Геометрия синтетическая; цифры на парах «до/после» — /tmp/s4/score.py и docs/step4_hypotheses.md.
"""

import math

import offsets as off


LOT = [(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0), (0.0, 0.0)]


def _house(points, handle="H1"):
    return off.Feature(kind="house", handle=handle, points=tuple(points))


def _texts(plan):
    return [item.text for item in plan.items]


def test_h34_house_gets_one_label_per_wall_not_one_per_house():
    # Две стены на разном расстоянии от границы (2 ft запад и 3 ft юг) — обе подписаны.
    house = _house([(2.0, 3.0), (20.0, 3.0), (20.0, 15.0), (2.0, 15.0)])
    plan = off.plan_offsets(LOT, [house], north_deg=90.0)
    assert "2.0'E" in _texts(plan)
    assert "3.0'N" in _texts(plan)


def test_h35_label_cap_keeps_the_number_of_house_labels_bounded(monkeypatch):
    house = _house([(2.0, 3.0), (20.0, 3.0), (20.0, 15.0), (2.0, 15.0)])
    monkeypatch.setattr(off, "HOUSE_MAX_LABELS", 1)
    plan = off.plan_offsets(LOT, [house], north_deg=90.0)
    assert len(plan.items) == 1


def test_h35_house_text_stands_at_the_wall_middle_but_anchor_is_the_corner():
    house = _house([(2.0, 3.0), (20.0, 3.0), (20.0, 15.0), (2.0, 15.0)])
    plan = off.plan_offsets(LOT, [house], north_deg=90.0)
    item = next(i for i in plan.items if i.text == "3.0'N")
    assert item.anchor in house.points
    # Текст стоит у середины южной стены, а не у её угла (2,3).
    assert math.dist(item.point, (11.0, 3.0)) < 9.0
    assert math.dist(item.point, item.anchor) > 1.0


def test_h36_short_wall_between_long_ones_is_not_labelled():
    # Выступ 4 ft на расстоянии 3.5 ft: это не стена дома, а эркер/крыльцо.
    house = _house([(2.0, 3.0), (20.0, 3.0), (20.0, 3.5), (24.0, 3.5), (24.0, 9.0), (2.0, 9.0)])
    plan = off.plan_offsets(LOT, [house], north_deg=90.0)
    assert "3.5'N" not in _texts(plan)


def test_h36_short_wall_is_labelled_if_the_limit_is_removed(monkeypatch):
    house = _house([(2.0, 3.0), (20.0, 3.0), (20.0, 3.5), (24.0, 3.5), (24.0, 9.0), (2.0, 9.0)])
    monkeypatch.setattr(off, "HOUSE_WALL_MIN_FT", 0.0)
    plan = off.plan_offsets(LOT, [house], north_deg=90.0)
    assert "3.5'N" in _texts(plan)
