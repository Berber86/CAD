"""Шаг 4, 4.26: что взято из ветки соседа (север у участка, замкнутость дома) и что отклонено."""

from __future__ import annotations

import offsets as off

LOT = [(0.0, 0.0), (40.0, 0.0), (40.0, 40.0), (0.0, 40.0), (0.0, 0.0)]


def _texts(feature: off.Feature) -> list[str]:
    return [item.text for item in off.plan_offsets(LOT, [feature], north_deg=90.0).items]


def test_open_house_contour_is_not_closed() -> None:
    """П-образный контур соседнего здания — открытая ломаная: воображаемую стену не меряем.

    Соседняя ветка передаёт из распознавания признак ``closed``; без него на 6910 15th Ave
    замыкающая сторона получала оффсет, которого у Николая нет (Alvine, `2.5'S` на доме).
    """
    points = ((3.0, 3.0), (3.0, 14.0), (20.0, 14.0), (20.0, 3.0))  # открытая «П»
    closed = off.Feature(kind="house", handle="H", points=points, closed=True)
    open_house = off.Feature(kind="house", handle="H", points=points, closed=False)
    assert "3.0'N" in _texts(closed)  # замыкающая стена y=3 даёт подпись «3.0'N»
    assert "3.0'N" not in _texts(open_house)  # а у открытого контура её нет
    # ``closed=None`` — прежнее поведение: дом считается замкнутым.
    assert "3.0'N" in _texts(off.Feature(kind="house", handle="H", points=points))


def test_gate_end_at_house_wall_is_skipped_only_when_rule_is_on(monkeypatch) -> None:
    """Внутренний конец калитки у стены дома: правило соседа работает, но по умолчанию выключено.

    На наших трёх парах оно ничего не срезает, а на Brooklyn добавляет лишнюю подпись
    (fence 4/3/2 → 5/3/3), поэтому включено только как опция для чертежей с калитками у стен.
    """
    house = off.Feature(kind="house", handle="H", points=((0.0, 6.0), (10.0, 6.0), (10.0, 16.0), (0.0, 16.0)))
    fence = off.Feature(
        kind="fence",
        handle="F",
        points=((0.6, 0.6), (3.0, 0.6), (3.0, 6.2)),
        closed=False,
    )
    survey = [((3.0, 0.6), "WOOD FENCE"), ((3.0, 6.2), "WOOD FENCE")]

    def texts() -> list[str]:
        return [
            item.text
            for item in off.plan_offsets(LOT, [fence, house], north_deg=90.0, survey=survey).items
        ]

    assert off.GATE_AT_WALL_MIN_FT == 0.0  # по умолчанию выключено
    default = texts()
    monkeypatch.setattr(off, "GATE_AT_WALL_MIN_FT", 1.2)
    assert texts() != default  # правило включено — вершина у стены дома отброшена
    assert off.FENCE_SAME_SIDE_SEP_FT == 0.0 and off.FENCE_DIFF_SIDE_SEP_FT == 0.0
    assert off.FENCE_SIMPLIFY_DEG == 0.0
