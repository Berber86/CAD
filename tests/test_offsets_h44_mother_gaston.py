"""Шаг 4, 4.30: четвёртая пара заказчика — 378 Mother Gaston Boulevard (угловой участок).

Что даёт этот чертёж, чего не было на трёх прежних:

* дом стоит **на линии улицы** и подписан «BLDG O/L» (три подписи: две выноской, одна текстом) —
  числа у этой стены нет вовсе;
* **все 11** оффсетов с выносками (как и 11 из 13 подписей забора на 192 Geldner), то есть «без
  выносок» — осознанное требование заказчика, а не то, как чертит Николай;
* файл «до» снят **выделением** (191 объект из 4296) и участок разрезан межой поперёк — по такому
  дампу контур не восстанавливался, и это ломало замер (см. `test_field_dump.py`).
"""

from __future__ import annotations

import math
from pathlib import Path

import offsets as off
import test_field_dump as fd

DOCS = Path(__file__).resolve().parent.parent / "docs"

LOT = [(0.0, 0.0), (40.0, 0.0), (40.0, 60.0), (0.0, 60.0), (0.0, 0.0)]
STREET = [[(0.0, 60.0), (40.0, 60.0)]]


def test_house_on_the_street_line_is_labelled_on_line() -> None:
    """Стена дома на линии улицы подписывается «BLDG O/L», а не «0.0'N».

    378 Mother Gaston: фасад дома No. 378 стоит на линии Mother Gaston Boulevard, и Николай
    поставил у него три «BLDG O/L» — числа у этой стены нет.
    """
    house = off.Feature(
        kind="house", handle="H", points=((5.0, 55.0), (25.0, 55.0), (25.0, 60.0), (5.0, 60.0)), closed=True
    )
    plan = off.plan_offsets(LOT, [house], front=STREET, north_deg=90.0)
    texts = [item.text for item in plan.items]
    assert texts.count("BLDG O/L") == 1
    assert "5.0'S" in texts  # стена в 5 ft от улицы подписана числом, как раньше
    assert all(not text.startswith("0.0") for text in texts)


def test_house_on_the_rear_line_is_not_labelled_on_line() -> None:
    """Линия оверхата по задней меже не даёт «BLDG O/L»: там Николай подписывает забор.

    На том же чертеже линия по задней меже стоит на границе участка, но подписана забором
    («FE.0.1'W») — «O/L» он пишет только на линии улицы.
    """
    house = off.Feature(
        kind="house", handle="H", points=((5.0, 0.0), (25.0, 0.0), (25.0, 20.0), (5.0, 20.0)), closed=True
    )
    plan = off.plan_offsets(LOT, [house], front=STREET, north_deg=90.0)
    assert all("O/L" not in item.text for item in plan.items)
    assert [item.text for item in plan.items] == ["5.0'E"]


def test_house_on_the_line_without_street_lines_is_not_labelled() -> None:
    """Без линий улицы «BLDG O/L» не ставится: признак — фронт, а не просто «на границе»."""
    house = off.Feature(
        kind="house", handle="H", points=((5.0, 55.0), (25.0, 55.0), (25.0, 60.0), (5.0, 60.0)), closed=True
    )
    plan = off.plan_offsets(LOT, [house], north_deg=90.0)
    assert all("O/L" not in item.text for item in plan.items)


def _measure(name: str = "primer4i.zip"):
    before, after = fd.load_pair(DOCS / name)
    result, expected = fd.field_plan(before, after)
    return before, after, result, expected


def test_mother_gaston_lot_is_the_whole_parcel() -> None:
    """Участок — 25 × 100 ft с межой поперёк, а не половина: иначе все расстояния не те.

    Файл «до» снят выделением, и контур по нему выходил клином 1113 sq ft (южная половина).
    Геометрия берётся из полного файла «после» в системе «до» (``field_geometry``), а половины
    участка, разрезанного межой, склеиваются (``_lot_polygon``).
    """
    before, after, result, _expected = _measure()
    assert before["drawing"]["scope"] == "selection"
    assert math.isclose(result["ring"] and abs(_area(result["ring"])), 2493.0, rel_tol=0.02)


def _area(ring) -> float:
    total = 0.0
    for (x0, y0), (x1, y1) in zip(ring, list(ring)[1:] + list(ring)[:1]):
        total += x0 * y1 - x1 * y0
    return total / 2.0


def test_mother_gaston_offsets_are_compared_in_the_same_frame() -> None:
    """Подписи Николая переносятся в систему «до» обратным подобием, а не прямым.

    Файл «после» сдвинут на 1440 ft; прямое преобразование уводило вершины подписей на соседний
    квартал (все 11 подписей мимо). На 1373 East 55th ошибка не была видна: поворот около 180°
    сам себе обратный.
    """
    _before, _after, result, expected = _measure()
    assert expected, "подписи Николая не найдены"
    xs = [part["anchor"][0] for part in expected]
    assert max(xs) < 3600.0, "вершины подписей остались в системе «после»"
    assert all(3480.0 <= x <= 3530.0 for x in xs)


def test_mother_gaston_house_on_the_street_line_is_labelled_on_line() -> None:
    """На чертеже наша единственная «BLDG O/L» стоит на линии улицы, как у Николая."""
    before, _after, result, _expected = _measure()
    on_line = [item for item in result["plan"].items if item.text == "BLDG O/L"]
    assert len(on_line) == 1
    front = fd.front_lines(before) or []
    geometry_front = fd.front_lines(fd.field_geometry(before, _after))
    assert geometry_front, "линии улицы не найдены"
    point = on_line[0].anchor
    assert any(
        off.perpendicular_line_distance(point, line, closed=False)[0] <= 0.05 for line in geometry_front
    ), f"«BLDG O/L» не на линии улицы: {point}"
    assert front is not None
