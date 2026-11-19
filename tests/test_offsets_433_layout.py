"""Шаг 4.33: «F E C», габарит чужих надписей с их поворотом и второй заход без зазора.

Три правки пришли из сверки с параллельной веткой (`arena/385e6c78-cad`, 4.25) и проверены на четырёх
полевых парах: совпавших частей 35 → 36, подписей 58 → 59, наложений на чужие тексты 0.
"""

from __future__ import annotations

import math

import pytest

import offsets as off

SQUARE = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]


def _fence(points, handle="F"):
    return off.Feature(kind="fence", handle=handle, points=tuple(points))


def test_fence_corner_described_f_e_c_is_labelled():
    # 192 Geldner: точка (4204.4, 5002.0) описана «F E C», и Николай подписал её `C.L.F. 0.6'S / 1.1'E`.
    # Точка стоит углом забора в 0.4 ft от южной стороны участка.
    assert "F E C" in off.FENCE_DESCRIPTION_WORDS
    fence = _fence([(40.0, 0.4), (70.0, 0.4)])
    plan = off.plan_offsets(
        SQUARE, [fence], north_deg=90.0, survey=[((40.0, 0.4), "F E C")],
    )
    texts = [item.text for item in plan.items if item.kind == "fence"]
    assert texts, plan.skipped
    assert texts[0].startswith("FE.0.4'N")


def test_plain_fence_word_still_works():
    fence = _fence([(40.0, 0.4), (70.0, 0.4)])
    plan = off.plan_offsets(SQUARE, [fence], north_deg=90.0, survey=[((40.0, 0.4), "WOOD FENCE")])
    assert [item.text for item in plan.items if item.kind == "fence"]


def test_existing_vertical_text_keeps_its_place():
    # Вертикальная чужая надпись занимает узкий и высокий прямоугольник. Подпись у той же точки не должна
    # встать поверх неё (4.33: габарит чужого текста считается с его поворотом).
    fence = _fence([(40.0, 0.4), (70.0, 0.4)])
    blocked = off.plan_offsets(
        SQUARE, [fence], north_deg=90.0,
        texts=[("CHAIN LINK FENCE ATOP CONC. CURB", (40.0, 1.4), 0.9, 90.0)],
    )
    item = next(item for item in blocked.items if item.kind == "fence")
    rect = off._text_rect(item.point, item.text, item.height_ft, off.default_text_width, item.rotation_deg)
    other = off._text_rect((40.0, 1.4), "CHAIN LINK FENCE ATOP CONC. CURB", 0.9, off.default_text_width, 90.0)
    assert not off._rects_overlap(rect, other)


def test_flag_is_on():
    assert off.LABEL_RELAXED_RETRY is True


def test_rotated_text_block_geometry_used_by_leader_search():
    # _place_leader проверяет место по повёрнутому габариту: у 90° длина уходит в высоту.
    def width_of(text: str, height: float) -> float:
        return 10.0 * height

    seen = []

    def ok(rect, center):
        seen.append(rect)
        return True

    off._place_leader((0.0, 0.0), (0.0, 1.0), "FE.10.0'N", 1.0, width_of, ok, 1.0, 90.0)
    first = seen[0]
    assert math.isclose(first[2] - first[0], 1.0, abs_tol=0.01)
    assert math.isclose(first[3] - first[1], 10.0, abs_tol=0.01)
