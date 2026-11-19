"""Шаг 4: контрольные цифры на полевых парах заказчика (``docs/primer*.zip``).

Пары «до / после» и эталон Николая разбирает ``tests/test_field_dump.py``. Здесь — пороги,
ниже которых правила Шага 4 не должны опускаться: они замерены на 4.26/4.27 и нужны, чтобы
очередная правка не улучшала один чертёж за счёт двух других.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import test_field_dump as fd

DOCS = Path(__file__).resolve().parent.parent / "docs"

#: имя пары → (частей у Николая, минимум совпавших частей, максимум подписей программы)
PAIRS = {
    "primer3i.zip": (19, 12, 26),   # 192 Geldner Street
    "primer1i.zip": (12, 6, 16),    # 24 Alvine Avenue
    "primer2i.zip": (9, 3, 22),     # Brooklyn
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


def test_geldner_fence_labels_are_text_without_leaders():
    """У Николая на 192 Geldner все подписи по заборам — текст у линии, без стрелок."""
    result, _expected, _parts = _measure("primer3i.zip")
    fences = [item for item in result["plan"].items if item.kind in {"fence", "wall"}]
    assert fences
    assert all(item.style == "text" and item.leader is None for item in fences)


def test_geldner_labels_do_not_cover_existing_texts():
    """Подписи не накрывают тексты, которые уже стояли на чертеже."""
    before, after = fd.load_pair(DOCS / "primer3i.zip")
    result, _expected, _parts = _measure("primer3i.zip")
    from offsets import _rect_at, _rects_overlap, default_text_width

    existing = [
        _rect_at((float(e["pt"][0]), float(e["pt"][1])), default_text_width(str(e.get("text")), float(e.get("h") or 0.9)), float(e.get("h") or 0.9))
        for e in before.get("entities", [])
        if e.get("type") in {"Text", "MText"} and e.get("pt")
    ]
    for item in result["plan"].items:
        rect = _rect_at(
            (float(item.point[0]), float(item.point[1])),
            default_text_width(item.text, item.height_ft),
            item.height_ft,
        )
        assert not any(_rects_overlap(rect, other) for other in existing)
