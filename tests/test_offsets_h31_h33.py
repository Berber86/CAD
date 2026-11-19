"""Шаг 4, правила 4.21: H31 (местный минимум угла), H32 (часть не дублируется короткой подписью),
H33 (конец длинного обследованного забора до 6 ft снаружи).

Геометрия синтетическая; цифры на парах «до/после» — /tmp/s4/score.py и docs/step4_hypotheses.md.
"""

import offsets as off


LOT = [(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0), (0.0, 0.0)]
EAST = 40.0


def _fence(points, handle="F1"):
    return off.Feature(kind="fence", handle=handle, points=tuple(points))


def _texts(plan):
    return [item.text for item in plan.items]


def test_h31_far_side_of_a_jog_is_not_labelled():
    # Забор идёт вдоль восточной стороны: 0.5, 0.8, 0.5 ft. Средняя вершина дальше обеих соседних —
    # это не угол забора, а изгиб наружу; Николай такие не подписывает.
    fence = _fence([(39.5, 5.0), (39.8, 15.0), (39.5, 25.0)])
    survey = [((x, y), "VINYL FENCE") for x, y in fence.points]
    plan = off.plan_offsets(LOT, [fence], north_deg=90.0, survey=survey)
    assert "FE.0.8'W" not in _texts(plan)
    assert "FE.0.5'W" in _texts(plan)


def test_h31_nearest_side_of_a_jog_is_labelled():
    # Обратный случай: средняя вершина ближе к стороне (0.2 против 0.6) — это угол, он подписывается.
    fence = _fence([(39.4, 5.0), (39.8, 15.0), (39.4, 25.0)])
    survey = [((x, y), "VINYL FENCE") for x, y in fence.points]
    plan = off.plan_offsets(LOT, [fence], north_deg=90.0, survey=survey)
    assert "FE.0.2'W" in _texts(plan)


def test_h32_short_label_is_dropped_when_a_full_label_has_the_same_part():
    # Пересечение даёт «FE.0.7'E» у угла; рядом обследованный угол даёт «FE.0.7'E / 0.3'S».
    # Короткая подпись повторяет часть полной — не ставится.
    crossing = _fence([(40.7, -5.0), (40.7, 10.0)], handle="cross")
    corner = _fence([(40.7, 0.3), (35.0, 0.3)], handle="corner")
    survey = [((40.7, 0.3), "VINYL FENCE")]
    plan = off.plan_offsets(LOT, [crossing, corner], north_deg=90.0, survey=survey)
    with_part = [text for text in _texts(plan) if "0.7'E" in text]
    assert len(with_part) == 1
    assert "\n" in with_part[0]


def test_h33_long_fence_end_beyond_five_feet_is_labelled():
    # Конец 25-футового забора в 5.2 ft южнее южной границы: 192 Geldner «FE.5.2'S».
    fence = _fence([(40.7, -5.2), (40.7, 20.0)])
    survey = [((40.7, -5.2), "VINYL FENCE")]
    plan = off.plan_offsets(LOT, [fence], north_deg=90.0, survey=survey)
    assert "FE.5.2'S" in _texts(plan)


def test_h33_short_stub_beyond_five_feet_is_not_labelled():
    # Тот же вылет, но соседний отрезок всего 6 ft (Brooklyn): Николай такой конец не подписал.
    fence = _fence([(40.7, -5.3), (40.7, 0.7)])
    survey = [((40.7, -5.3), "VINYL FENCE")]
    plan = off.plan_offsets(LOT, [fence], north_deg=90.0, survey=survey)
    assert "FE.5.3'S" not in _texts(plan)
