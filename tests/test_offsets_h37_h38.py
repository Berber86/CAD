"""Шаг 4, правила 4.23: H37–H38 и «FE.O/L» — текст дома у стены внутри строения и вдоль границы,
вершина забора ровно на линии получает «FE.O/L».

Геометрия синтетическая; цифры на парах «до/после» — /tmp/s4/score.py и docs/step4_hypotheses.md.
"""

import offsets as off


LOT = [(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0), (0.0, 0.0)]
HOUSE = off.Feature(
    kind="house", handle="H1",
    points=((2.0, 3.0), (20.0, 3.0), (20.0, 15.0), (2.0, 15.0)),
)
FENCE = off.Feature(kind="fence", handle="F1", points=((10.0, 0.0), (10.0, 2.0), (25.0, 2.0)))


def _texts(plan):
    return [item.text for item in plan.items]


def test_online_fence_vertex_gets_fe_o_l():
    # Вершина (10, 0) лежит ровно на южной линии участка — Николай подписывает такие «FE.O/L».
    plan = off.plan_offsets(LOT, [FENCE], north_deg=90.0)
    assert "FE.O/L" in _texts(plan)


def test_online_labels_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(off, "ONLINE_LABELS", False)
    plan = off.plan_offsets(LOT, [FENCE], north_deg=90.0)
    assert "FE.O/L" not in _texts(plan)
    # Остальные вершины забора подписаны как обычно.
    assert "FE.2.0'N" in _texts(plan)


def test_house_text_stands_outside_the_lot():
    """R16 (4.38): текст дома без выноски — снаружи контура участка (у Николая 18 из 18)."""
    assert off.HOUSE_TEXT_OUTSIDE
    plan = off.plan_offsets(LOT, [HOUSE], north_deg=90.0)
    texts = [item for item in plan.items if item.style == "text"]
    assert texts
    for item in texts:
        assert not off.point_in_polygon(item.point, LOT), item.text


def test_house_text_falls_back_inside_the_building_without_the_rule(monkeypatch):
    """Без R16 текст возвращается внутрь строения (правило H38 осталось вторым заходом)."""
    monkeypatch.setattr(off, "HOUSE_TEXT_OUTSIDE", False)
    plan = off.plan_offsets(LOT, [HOUSE], north_deg=90.0)
    inside = [
        item for item in plan.items
        if item.style == "text" and off.point_in_polygon(item.point, HOUSE.points)
    ]
    assert inside


def test_house_text_rotates_along_the_boundary():
    # Западная граница вертикальная — текст «2.0'E» поворачивается на 90°; южная горизонтальная — 0°.
    plan = off.plan_offsets(LOT, [HOUSE], north_deg=90.0)
    west = next(item for item in plan.items if item.text == "2.0'E")
    south = next(item for item in plan.items if item.text == "3.0'N")
    assert round(west.rotation_deg) == 90
    assert round(south.rotation_deg) == 0


def test_house_text_rotation_can_be_switched_off(monkeypatch):
    # С 4.31 поворот включает и общий TEXT_FOLLOWS_BOUNDARY, поэтому гасим оба флага.
    monkeypatch.setattr(off, "HOUSE_TEXT_FOLLOWS_BOUNDARY", False)
    monkeypatch.setattr(off, "TEXT_FOLLOWS_BOUNDARY", False)
    plan = off.plan_offsets(LOT, [HOUSE], north_deg=90.0)
    assert all(round(item.rotation_deg) == 0 for item in plan.items)
