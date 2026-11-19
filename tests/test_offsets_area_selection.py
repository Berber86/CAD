"""Шаг 4, 4.27: выделение областью — это полилиния участка, а объекты стоят у самой её границы."""

from __future__ import annotations

import offsets as off

RING = [(0.0, 0.0), (60.0, 0.0), (60.0, 40.0), (0.0, 40.0)]
FRONT = [[(0.0, 0.0), (60.0, 0.0)]]


def _fence_outside() -> off.Feature:
    # забор в 0.3 ft снаружи южной границы: ни одна вершина не внутри выделения
    return off.Feature(kind="fence", handle="F", points=((10.0, -0.3), (30.0, -0.3)), closed=False)


def test_fence_just_outside_the_lot_polyline_is_still_labelled():
    """Выделение = полилиния участка: забор в 0.3 ft снаружи не должен выпадать из области.

    До 4.27 объект считался попавшим в область, только если хоть одна его вершина внутри;
    на 192 Geldner так оставалось 5 подписей вместо 14.
    """
    plan = off.plan_offsets(RING, [_fence_outside()], front=FRONT, area=RING)
    assert [item.text for item in plan.items], plan.warnings
    assert plan.counts.get("outside", 0) == 0


def test_far_object_is_still_outside_the_selection(monkeypatch):
    monkeypatch.setattr(off, "AREA_EDGE_TOL_FT", 0.0)
    plan = off.plan_offsets(RING, [_fence_outside()], front=FRONT, area=RING)
    assert plan.items == [] and plan.counts.get("outside") == 1


def test_label_may_stand_slightly_outside_the_selected_polyline():
    """Николай ставит подпись и снаружи границы: текст у линии участка не должен отбрасываться."""
    plan = off.plan_offsets(RING, [_fence_outside()], front=FRONT, area=RING)
    assert plan.items
    item = plan.items[0]
    assert item.leader is None
    assert off.AREA_EDGE_TOL_FT == 6.0
