"""Шаг 4: контрольные цифры на полевых парах заказчика (``docs/primer*.zip``).

Пары «до / после» и эталон Николая разбирает ``tests/test_field_dump.py``. Здесь — пороги,
ниже которых правила Шага 4 не должны опускаться: они замерены на 4.30 и подняты по замеру 4.31
и нужны, чтобы очередная правка не улучшала один чертёж за счёт остальных.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import test_field_dump as fd

DOCS = Path(__file__).resolve().parent.parent / "docs"

#: имя пары → (частей у Николая, минимум совпавших частей, максимум подписей программы)
PAIRS = {
    "primer3i.zip": (19, 14, 26),   # 192 Geldner Street (14 после 4.33: угол «F E C»)
    "primer1i.zip": (12, 9, 16),    # 24 Alvine Avenue (9 после правила 4.31 о метке в каждой строке)
    "primer2i.zip": (9, 7, 22),     # 1373 East 55th Street, Brooklyn (7 после 4.31)
    "primer4i.zip": (11, 6, 13),    # 378 Mother Gaston Blvd, Brooklyn (4.30)
}


def _measure(name: str):
    before, after = fd.load_pair(DOCS / name)
    result, expected = fd.field_plan(before, after)
    parts = fd.score(fd.plan_parts(result["plan"]), expected)
    return result, expected, parts


@pytest.mark.parametrize("name", sorted(PAIRS))
def test_field_pair_keeps_matches(name):
    expected_parts, min_parts, max_labels = PAIRS[name]
    result, expected, parts = _measure(name)
    assert len(expected) == expected_parts
    assert parts >= min_parts, f"{name}: совпало частей {parts} из {expected_parts}"
    assert len(result["plan"].items) <= max_labels


@pytest.mark.parametrize("name", sorted(PAIRS))
def test_every_label_finds_a_place(name):
    """Ни одна подпись не остаётся без места (4.33: второй заход вплотную, `LABEL_RELAXED_RETRY`)."""
    result, _expected, _parts = _measure(name)
    assert not result["plan"].warnings, result["plan"].warnings


def test_geldner_fence_labels_are_text_without_leaders():
    """У Николая на 192 Geldner все подписи по заборам — текст у линии, без стрелок."""
    result, _expected, _parts = _measure("primer3i.zip")
    fences = [item for item in result["plan"].items if item.kind in {"fence", "wall"}]
    assert fences
    assert all(item.style == "text" and item.leader is None for item in fences)


@pytest.mark.parametrize("name", sorted(PAIRS))
def test_labels_do_not_cover_existing_texts(name):
    """Подписи не накрывают тексты, которые уже стояли на чертеже (4.32: на всех четырёх парах)."""
    before, after = fd.load_pair(DOCS / name)
    result, _expected, _parts = _measure(name)
    from offsets import _rect_at, _rects_overlap, _text_rect, default_text_width

    # Габарит чужих надписей — тоже с их поворотом: вертикальное `V.FE.` занимает узкий и высокий
    # прямоугольник, а не широкий и низкий (4.33; иначе тест ловил ложные наложения).
    existing = [
        _text_rect(
            (float(e["pt"][0]), float(e["pt"][1])),
            str(e.get("text")),
            float(e.get("h") or 0.9),
            default_text_width,
            float(e.get("rot_deg") or 0.0),
        )
        for e in before.get("entities", [])
        if e.get("type") in {"Text", "MText"} and e.get("pt")
    ]
    for item in result["plan"].items:
        rect = _text_rect(
            (float(item.point[0]), float(item.point[1])),
            item.text,
            item.height_ft,
            default_text_width,
            float(getattr(item, "rotation_deg", 0.0)),
        )
        assert not any(_rects_overlap(rect, other) for other in existing)
