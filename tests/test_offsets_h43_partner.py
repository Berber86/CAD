"""Шаг 4, 4.26–4.28: что взято из параллельных веток (север у участка, замкнутость дома)."""

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


def test_house_deeper_than_six_feet_is_not_labelled() -> None:
    """Дом без стены ближе 6 ft не подписывается: у Николая самая дальняя подпись дома — 4.9 ft.

    До 4.28 в этом случае подпись всё равно ставилась по ближайшей вершине на любом расстоянии —
    оттуда и брались «12.2'E» и «13.1'S» на Geldner и Alvine.
    """
    deep = off.Feature(
        kind="house",
        handle="H",
        points=((15.0, 14.0), (30.0, 14.0), (30.0, 30.0), (15.0, 30.0)),
        closed=True,
    )
    plan = off.plan_offsets(LOT, [deep], north_deg=90.0)
    assert plan.items == []
    close = off.Feature(
        kind="house",
        handle="H",
        points=((15.0, 5.0), (30.0, 5.0), (30.0, 30.0), (15.0, 30.0)),
        closed=True,
    )
    assert [item.text for item in off.plan_offsets(LOT, [close], north_deg=90.0).items] == ["5.0'N"]
