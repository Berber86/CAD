"""Шаг 4, правила 4.20: H27 (пересечения забора с границей), H28 (углы у точек съёмки), H29 (буква по осям листа).

Проверки синтетические: геометрия простая, чтобы правило было visible в чистом виде.
Цифры на реальных парах «до/после» — /tmp/s4/score.py и docs/step4_hypotheses.md.
"""

import offsets as off


# Участок 40 × 30 ft, восточная граница x=40, южная y=0.
LOT = [(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0), (0.0, 0.0)]


def _fence(points, handle="F1"):
    return off.Feature(kind="fence", handle=handle, points=tuple(points))


def test_h29_diagonal_gets_letter_of_the_dominant_sheet_axis():
    # Раньше диагональ давала «NE»; заказчик пишет одну букву по ближайшей оси листа.
    # north_deg=0: север — +X, запад — +Y.
    assert off.offset_letter(2.0, 1.0, 0.0) == "E"
    assert off.offset_letter(1.0, 2.0, 0.0) == "N"
    assert off.offset_letter(-2.0, 1.0, 0.0) == "W"
    assert off.offset_letter(1.0, -3.0, 0.0) == "S"
    # Диагональ без явного перевеса — ровно одна буква, а не «NE»/«SW».
    assert off.offset_letter(1.0, 1.0, 0.0) in {"N", "E"}


def test_h28_each_surveyed_fence_corner_gets_its_own_offset():
    fence = _fence([(39.7, 5.0), (39.4, 15.0), (39.0, 25.0)])
    survey = [
        ((39.7, 5.0), "VINYL FENCE"),
        ((39.4, 15.0), "VINYL FENCE"),
        ((39.0, 25.0), "WOOD FENCE"),
    ]
    plan = off.plan_offsets(LOT, [fence], north_deg=90.0, survey=survey)
    texts = {item.text.splitlines()[0] for item in plan.items}
    assert texts == {"FE.0.3'W", "FE.0.6'W", "FE.1.0'W"}


def test_h28_fence_without_survey_description_keeps_one_nearest_label():
    fence = _fence([(39.7, 5.0), (39.4, 15.0), (39.0, 25.0)])
    plan = off.plan_offsets(LOT, [fence], north_deg=90.0)
    assert len(plan.items) == 1
    assert plan.items[0].text.startswith("FE.0.3'W")


def test_h28_corner_at_a_gate_is_skipped():
    fence = _fence([(39.7, 5.0), (39.7, 25.0)])
    survey = [((39.7, 5.0), "VINYL FENCE"), ((39.7, 25.0), "VINYL FENCE NE GATE")]
    plan = off.plan_offsets(
        LOT, [fence], north_deg=90.0, survey=survey, gates=[(39.75, 25.0)]
    )
    assert [item.text for item in plan.items] == ["FE.0.3'W"]


def test_h27_fence_outside_the_east_side_past_the_south_corner():
    # Забор идёт вдоль восточной стороны (0.7 ft наружу) и уходит за южную: «FE.0.7'E».
    fence = _fence([(40.7, -5.0), (40.7, 10.0)])
    plan = off.plan_offsets(LOT, [fence], north_deg=90.0)
    texts = [item.text for item in plan.items]
    assert "FE.0.7'E" in texts


def test_h27_fence_leaving_the_lot_is_not_labelled_without_a_fence_description():
    # Конец «O PORCH» на Brooklyn Николай не подписывает; на Alvine «VINYL FENCE» — подписывает.
    fence = _fence([(43.0, 5.0), (38.5, 5.0)])
    plan = off.plan_offsets(LOT, [fence], north_deg=90.0)
    assert "FE.3.0'E" not in [item.text for item in plan.items]

    survey = [((43.0, 5.0), "VINYL FENCE")]
    plan = off.plan_offsets(LOT, [fence], north_deg=90.0, survey=survey)
    assert "FE.3.0'E" in [item.text for item in plan.items]


def test_h27_same_part_is_not_repeated_at_the_same_corner():
    # Пересечение даёт 0.7'E, и вторая сторона у того же угла тоже 0.7'E — часть не дублируется.
    fence = _fence([(40.7, -5.0), (40.7, 10.0), (30.0, 10.0)])
    plan = off.plan_offsets(LOT, [fence], north_deg=90.0)
    parts = [item.text.replace("\n", " ") for item in plan.items]
    assert sum(part.count("0.7'E") for part in parts) <= 1
