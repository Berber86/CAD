"""Шаг 4.31: два стилевых правила, найденных по дампам заказчика.

- поворот текста без выноски — вдоль своей линии (`TEXT_FOLLOWS_BOUNDARY`);
- в столбике многозначной подписи метка забора стоит в каждой строке (`FENCE_PREFIX_EACH_LINE`).

Оба правила проверены на четырёх полевых парах (`tests/test_offsets_field_pairs.py`): поворот совпал
с Николаем у 17 подписей без выносок из 18 (было 13), совпавших частей 35 против 32.
"""

from __future__ import annotations

import pytest

import offsets as off

SQUARE = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]


def _fence(points, handle="F"):
    return off.Feature(kind="fence", handle=handle, points=tuple(points))


def _texts(plan):
    return [item for item in plan.items if item.style == "text"]


def test_flags_are_on():
    assert off.TEXT_FOLLOWS_BOUNDARY is True
    assert off.FENCE_PREFIX_EACH_LINE is True


def test_text_along_a_vertical_boundary_is_rotated_90_degrees():
    # Забор в 1 ft от западной (вертикальной) стороны: текст идёт вдоль неё.
    plan = off.plan_offsets(SQUARE, [_fence([(1.0, 40.0), (1.0, 70.0)])], north_deg=90.0)
    texts = _texts(plan)
    assert texts
    assert all(abs(round(item.rotation_deg)) == 90 for item in texts)


def test_text_along_a_horizontal_boundary_stays_horizontal():
    # Забор в 1 ft от южной стороны: текст горизонтален.
    plan = off.plan_offsets(SQUARE, [_fence([(40.0, 99.0), (70.0, 99.0)])], north_deg=90.0)
    texts = _texts(plan)
    assert texts
    assert all(round(item.rotation_deg) == 0 for item in texts)


def test_rotation_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(off, "TEXT_FOLLOWS_BOUNDARY", False)
    monkeypatch.setattr(off, "HOUSE_TEXT_FOLLOWS_BOUNDARY", False)
    plan = off.plan_offsets(SQUARE, [_fence([(1.0, 40.0), (1.0, 70.0)])], north_deg=90.0)
    texts = _texts(plan)
    assert texts
    assert all(round(item.rotation_deg) == 0 for item in texts)


def test_two_sides_of_one_vertex_repeat_the_mark_on_each_line():
    # Вершина (1.0, 0.3) у угла: 0.3 до южной стороны и 1.0 до западной. Как на 24 Alvine:
    # «FE.0.4'N» и под ним «FE.1.3'W» — метка в каждой строке.
    plan = off.plan_offsets(SQUARE, [_fence([(1.0, 0.3), (30.0, 0.3)])], north_deg=90.0)
    texts = [item.text for item in _texts(plan)]
    assert texts == ["FE.0.3'N\nFE.1.0'E"]


def test_the_mark_is_written_once_when_the_flag_is_off(monkeypatch):
    monkeypatch.setattr(off, "FENCE_PREFIX_EACH_LINE", False)
    plan = off.plan_offsets(SQUARE, [_fence([(1.0, 0.3), (30.0, 0.3)])], north_deg=90.0)
    texts = [item.text for item in _texts(plan)]
    assert texts == ["FE.0.3'N\n1.0'E"]


def test_rotated_text_block_swaps_width_and_height():
    # Габарит повёрнутого на 90° текста — транспонированный: длинное значение занимает высоту, а не ширину.
    def width_of(text: str, height: float) -> float:
        return 10.0 * height

    upright = off._text_rect((0.0, 0.0), "FE.10.0'N", 1.0, width_of, 0.0)
    turned = off._text_rect((0.0, 0.0), "FE.10.0'N", 1.0, width_of, 90.0)
    assert pytest.approx(upright[2] - upright[0]) == 10.0
    assert pytest.approx(upright[3] - upright[1]) == 1.0
    assert pytest.approx(turned[2] - turned[0]) == 1.0
    assert pytest.approx(turned[3] - turned[1]) == 10.0
