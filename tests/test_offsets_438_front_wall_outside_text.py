"""
Шаг 4, правила 4.38 — взято из ветки `arena/385e6c78-cad` (их версия 4.30) и проверено на шести парах.

* R17 — устой у улицы: кирпичный столб, стоящий на боковой линии участка в полосе улицы,
  подписывается замером **до красной линии** голым числом (`3.0'E`), по задней грани.
  На 1412 East 64th Street это единственные подписи стен: +2 подписи, +2 совпадения, лишних 0.
  `WALL_OFFSETS` целиком (все стены) остаётся выключенным — он дал +15 подписей и 0 совпадений (4.36).
* R16 — текст дома без выноски ставится **снаружи контура участка**: у Николая все 18 текстов дома
  без выноски стоят вне участка, у нас до 4.38 внутри стояли 16 из 36; стало 2 из 37.
"""

from __future__ import annotations

import offsets as off

LOT = [(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0), (0.0, 0.0)]
FRONT = [((0.0, 30.0), (40.0, 30.0))]


def _wall(points, handle="W1"):
    return off.Feature(kind="wall", handle=handle, points=tuple(points))


def _house(points, handle="H1"):
    return off.Feature(kind="house", handle=handle, points=tuple(points))


def test_front_wall_pier_gets_bare_number_to_the_red_line():
    """Устой в 3 ft от красной линии — `3.0'S` без приставки `WALL`, задней гранью."""
    assert off.FRONT_WALL_OFFSETS
    pier = _wall([(0.0, 27.0), (1.0, 27.0)])
    plan = off.plan_offsets(LOT, [pier], front=FRONT, north_deg=90.0)
    assert [item.text for item in plan.items] == ["3.0'S"], plan.skipped
    item = plan.items[0]
    assert item.kind == "wall"
    assert item.value_ft == 3.0


def test_front_wall_pier_is_labelled_once_per_pier():
    """Устой начерчен двумя ломаными (оголовок и ствол) — подпись одна, по дальней грани."""
    cap = _wall([(0.0, 27.0), (1.0, 27.0)], handle="cap")
    shaft = _wall([(0.0, 28.0), (1.0, 28.0)], handle="shaft")
    plan = off.plan_offsets(LOT, [cap, shaft], front=FRONT, north_deg=90.0)
    assert [item.text for item in plan.items] == ["3.0'S"], plan.items
    assert any("стена" in reason for _label, reason in plan.skipped)


def test_front_wall_needs_the_side_line_and_three_feet():
    """Дальше 3 ft от красной линии или не на боковой линии — стена не подписывается."""
    far = _wall([(0.0, 25.0), (1.0, 25.0)], handle="far")
    plan = off.plan_offsets(LOT, [far], front=FRONT, north_deg=90.0)
    assert not plan.items, plan.items

    inside = _wall([(5.0, 27.0), (6.0, 27.0)], handle="inside")
    plan = off.plan_offsets(LOT, [inside], front=FRONT, north_deg=90.0)
    assert not plan.items, plan.items


def test_front_wall_rule_needs_the_street_line():
    """Без линии улицы устой остаётся без подписи — как все стены (WALL_OFFSETS = False)."""
    pier = _wall([(0.0, 27.0), (1.0, 27.0)])
    plan = off.plan_offsets(LOT, [pier], north_deg=90.0)
    assert not plan.items, plan.items
    assert plan.skipped


def test_house_text_stands_outside_the_lot_before_inside():
    """R16: текст дома без выноски — снаружи контура, а не внутри строения."""
    assert off.HOUSE_TEXT_OUTSIDE
    house = _house([(10.0, 10.0), (30.0, 10.0), (30.0, 25.0), (10.0, 25.0)])
    plan = off.plan_offsets(LOT, [house], north_deg=90.0)
    texts = [item for item in plan.items if item.style == "text"]
    assert texts, plan.skipped
    for item in texts:
        assert not off.point_in_polygon(item.point, LOT), item.text
