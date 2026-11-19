"""
Шаг 4, правила 4.35: осевая сторона с крошечным изломом, предел дома 7 ft, «FE.O/L» на углу.

Все три правила выведены из 18 Zoe Street (`docs/primer5i.zip`, см. `docs/step4_hypotheses.md`, 4.35):

* восточная сторона участка начерчена двумя отрезками `PL` со сдвигом 0.0015 ft на 109 ft
  (0.0008°) — из-за точного сравнения координат она не считалась стороной, и дом не подписывался;
* стена дома стоит в 6.77 ft от Zoe Street — Николай подписывает её `BLDG 6.8'S`, а предел 6 ft
  отсекал дом целиком;
* `FE.O/L` стоял в 0.85 ft от `FE.0.6'S` — две подписи на одном угле забора.
"""

from __future__ import annotations

import offsets as off

#: Участок 30 × 30; восточная сторона разбита на два отрезка со сдвигом 0.0008 ft (как на 18 Zoe).
LOT_JOG = [(0.0, 0.0), (30.0, 0.0), (30.0008, 15.0), (30.0008, 30.0), (0.0, 30.0), (0.0, 0.0)]


def _house(points, handle="H1"):
    return off.Feature(kind="house", handle=handle, points=tuple(points))


def _fence(points, handle="F1"):
    return off.Feature(kind="fence", handle=handle, points=tuple(points))


def test_lot_side_stays_axis_aligned_across_a_tiny_jog():
    """Сторона со сдвигом 0.0008 ft на 15 ft (0.003°) остаётся вертикальной, а не теряется."""
    assert off.AXIS_SIDE_TOL_DEG == 0.05
    _horizontal, vertical = off._axis_sides(LOT_JOG[:-1])
    # Западная сторона и два отрезка восточной — все вертикальные (без допуска восточная терялась).
    assert len(vertical) == 3, vertical
    assert {round(x, 4) for x, _y0, _y1 in vertical} == {0.0, 30.0004, 30.0008}


def test_house_wall_is_measured_across_a_tiny_jog_in_the_lot_side():
    """Стена дома в 6.5 ft от стороны с изломом подписывается (на 18 Zoe это `6.8'S`)."""
    house = _house([(11.5, 8.0), (23.5, 8.0), (23.5, 20.0), (11.5, 20.0)])
    plan = off.plan_offsets(LOT_JOG, [house], north_deg=90.0)
    values = [round(item.value_ft, 1) for item in plan.items if item.kind == "house"]
    assert values, plan.skipped
    assert 6.5 in values


def test_house_limit_is_seven_feet_since_4_35(monkeypatch):
    """Предел 7 ft: стена в 6.5 ft подписывается, а со старым пределом 6 ft — нет."""
    house = _house([(11.5, 8.0), (23.5, 8.0), (23.5, 20.0), (11.5, 20.0)])
    assert off.HOUSE_WALL_MAX_FT == 7.0
    plan = off.plan_offsets(LOT_JOG, [house], north_deg=90.0)
    assert [item for item in plan.items if item.kind == "house"]

    monkeypatch.setattr(off, "HOUSE_WALL_MAX_FT", 6.0)
    monkeypatch.setattr(off, "KIND_MAX_FT", 6.0)
    monkeypatch.setattr(off, "PARALLEL_MAX_FT", 6.0)
    plan = off.plan_offsets(LOT_JOG, [house], north_deg=90.0)
    assert not [item for item in plan.items if item.kind == "house"]


def test_online_label_yields_to_a_number_at_the_same_corner():
    """`FE.O/L` не пишется, если в футе на том же угле уже стоит число (18 Zoe: 0.85 ft)."""
    lot = [(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0), (0.0, 0.0)]
    # угол (10, 0) стоит на линии участка, вторая вершина — в 0.6 ft от неё и в 0.85 ft от угла.
    fence = _fence([(10.0, 0.0), (10.6, 0.6), (30.0, 0.6)])
    plan = off.plan_offsets(lot, [fence], north_deg=90.0)
    texts = [item.text for item in plan.items]
    assert "FE.O/L" not in texts, texts


def test_online_label_stays_when_the_numbered_corner_is_two_and_a_half_feet_away():
    """Рядом (2.5 ft) — это уже соседний угол: `FE.O/L` остаётся, правило 4.23 не ломается."""
    lot = [(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0), (0.0, 0.0)]
    fence = _fence([(10.0, 0.0), (12.5, 0.0), (12.5, 20.0)])
    plan = off.plan_offsets(lot, [fence], north_deg=90.0)
    texts = [item.text for item in plan.items]
    assert "FE.O/L" in texts, texts
