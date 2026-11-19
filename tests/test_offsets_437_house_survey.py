"""
Шаг 4, правила 4.37: пятая подпись дома, стена 7 ft, радиус точки съёмки 0.6 ft.

Все три правки выведены из полевых пар (см. `docs/step4_hypotheses.md`, 4.37) и проверены замером
по шести парам: совпавших частей 53 против 50, подписей 99 против 94, предупреждений раскладки 0.

* 1373 East 55th Street — у Николая пять подписей дома (`3.5'S`, `4.0'N`, `1.9'N`, `3.0'W`, `2.3'S`),
  пятая — стена в 3.0 ft от северной стороны; предел четырёх подписей её отбрасывал;
* 24 Alvine Avenue — стена дома длиной 7.1 ft подписана (`3.5'S`); при `HOUSE_WALL_MIN_FT` = 8 ft
  она считалась выступом и терялась;
* 18 Zoe Street — угол забора стоит в 0.44 ft от точки съёмки «VINYL FENCE», у Николая это
  `FE.0.7'E / FE.0.3'N`; при радиусе 0.35 ft угол не признавался обследованным.
"""

from __future__ import annotations

import offsets as off

LOT = [(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0), (0.0, 0.0)]


def _house(points, handle="H1"):
    return off.Feature(kind="house", handle=handle, points=tuple(points))


def _fence(points, handle="F1"):
    return off.Feature(kind="fence", handle=handle, points=tuple(points))


def _house_texts(plan):
    return [item.text for item in plan.items if item.kind == "house"]


def test_house_gets_five_labels_since_4_37(monkeypatch):
    """Пять стен на разном расстоянии — пять подписей; с прежним пределом 4 — четыре."""
    assert off.HOUSE_MAX_LABELS == 5
    monkeypatch.setattr(off, "HOUSE_WALL_MAX_FT", 40.0)
    monkeypatch.setattr(off, "KIND_MAX_FT", 40.0)
    monkeypatch.setattr(off, "PARALLEL_MAX_FT", 40.0)
    # Шесть стен на разном расстоянии дают шесть кандидатов: 2'N, 2'W, 9'N, 10'E, 10'W.
    house = _house(
        [(1.0, 2.0), (10.0, 2.0), (10.0, 16.0), (38.0, 16.0), (38.0, 9.0), (30.0, 9.0), (30.0, 2.0)]
    )
    plan = off.plan_offsets(LOT, [house], north_deg=90.0)
    assert len(_house_texts(plan)) == 5, _house_texts(plan)

    monkeypatch.setattr(off, "HOUSE_MAX_LABELS", 4)
    plan = off.plan_offsets(LOT, [house], north_deg=90.0)
    assert len(_house_texts(plan)) == 4, _house_texts(plan)


def test_house_wall_of_seven_feet_is_labelled_since_4_37(monkeypatch):
    """Стена 7.1 ft — это стена дома (24 Alvine, `3.5'S`); 6.9 ft — ещё выступ."""
    assert off.HOUSE_WALL_MIN_FT == 7.0
    long_wall = _house([(5.0, 3.0), (12.1, 3.0), (12.1, 10.0), (5.0, 10.0)])
    plan = off.plan_offsets(LOT, [long_wall], north_deg=90.0)
    assert "3.0'N" in _house_texts(plan), _house_texts(plan)

    short_wall = _house([(5.0, 3.0), (11.9, 3.0), (11.9, 10.0), (5.0, 10.0)])
    plan = off.plan_offsets(LOT, [short_wall], north_deg=90.0)
    assert "3.0'N" not in _house_texts(plan), _house_texts(plan)

    monkeypatch.setattr(off, "HOUSE_WALL_MIN_FT", 8.0)
    plan = off.plan_offsets(LOT, [long_wall], north_deg=90.0)
    assert "3.0'N" not in _house_texts(plan), _house_texts(plan)


def test_survey_point_is_matched_at_point_four_feet_since_4_37(monkeypatch):
    """Угол забора в 0.44 ft от точки съёмки попадает в подпись (18 Zoe, `FE.0.3'N`), в 0.35 — нет."""
    assert off.SURVEY_POINT_FT == 0.6
    # Угол (0.44, 12) — внутренняя вершина: обычное правило подписало бы вершину (0.2, 6), что ближе к меже.
    fence = _fence([(0.2, 6.0), (0.44, 12.0), (18.0, 18.0)])
    survey = [((0.0, 12.0), "VINYL FENCE")]
    plan = off.plan_offsets(LOT, [fence], north_deg=90.0, survey=survey)
    anchors = [tuple(round(v, 2) for v in item.anchor) for item in plan.items]
    assert (0.44, 12.0) in anchors, anchors

    monkeypatch.setattr(off, "SURVEY_POINT_FT", 0.35)
    plan = off.plan_offsets(LOT, [fence], north_deg=90.0, survey=survey)
    anchors = [tuple(round(v, 2) for v in item.anchor) for item in plan.items]
    assert (0.44, 12.0) not in anchors, anchors
    assert (0.2, 6.0) in anchors, anchors
