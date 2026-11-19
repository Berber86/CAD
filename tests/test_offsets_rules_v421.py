"""Шаг 4, версия 4.21: модульные и эталонные тесты правил H25, H27–H38 (адаптация и уточнение правил)."""

from __future__ import annotations

import math

import pytest

import offsets as off
from test_offsets_rules_v420 import five_surveys, _item_parts  # noqa: F401


LOT40 = [(0.0, 0.0), (40.0, 0.0), (40.0, 40.0), (0.0, 40.0)]
LOT100 = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]


def _fence(pts, handle="F1"):
    return off.Feature(kind="fence", handle=handle, points=tuple(pts), closed=False)


def _house(pts, handle="H1", closed=True):
    return off.Feature(kind="house", handle=handle, points=tuple(pts), closed=closed)


def test_h25_online_fence_leader_and_deduplication():
    """Забор на границе получает выноску FE.O/L (style='leader'), а соседний дубль в 6 ft подавляется."""
    f1 = _fence([(10.0, 0.01), (35.0, 0.02)], handle="F1")
    f2 = _fence([(35.0, 0.02), (38.0, 0.01)], handle="F2")
    plan = off.plan_offsets(LOT40, [f1, f2])
    online = [it for it in plan.items if it.text == "FE.O/L"]
    assert len(online) == 1
    assert online[0].style == "leader"
    assert online[0].leader is not None
    assert online[0].value_ft == 0.0


def test_h27_h33_collinear_fence_fusion_and_exterior_crossing_end():
    """Разбитый на части прямой забор склеивается без лишних стыков и даёт пересечение + внешний конец <= 6 ft."""
    # Восточная граница x=40; единый забор x=40.7 разбит на 3 куска и уходит на 5.2 ft южнее y=0.
    f_a = _fence([(40.7, -5.2), (40.7, 12.0)], handle="FA")
    f_b = _fence([(40.7, 12.0), (40.7, 26.0)], handle="FB")
    f_c = _fence([(40.7, 26.0), (40.7, 38.0)], handle="FC")
    survey = [((40.7, -5.2), "C.L.F. SE"), ((40.7, 38.0), "C.L.F. NE")]
    plan = off.plan_offsets(LOT40, [f_a, f_b, f_c], survey=survey)
    texts = [it.text for it in plan.items]
    # Точки стыка y=12 и y=26 не дают лишних выносок; есть пересечение 0.7'E и южный внешний конец 5.2'S.
    assert "FE.5.2'S" in texts
    assert any("0.7'E" in t for t in texts)
    assert all(it.anchor[1] not in (12.0, 26.0) for it in plan.items)

    # Короткий 6-футовый отросток дальше 5.0 ft снаружи (5.3 ft) не подписывается.
    stub = _fence([(20.0, 0.2), (20.0, -5.3)], handle="STUB")
    plan_stub = off.plan_offsets(LOT40, [stub])
    assert all(it.value_ft < 5.0 for it in plan_stub.items)


def test_h28_gate_and_survey_filtering_preserves_sole_interior_corner():
    """Угол у ворот пропускается при наличии других внутренних углов, но единственный внутренний угол сохраняется."""
    # Забор с двумя внутренними концами: один у ворот (x=0.5, y=10), другой свободный (x=0.4, y=30).
    f_two = _fence([(0.5, 10.0), (0.4, 30.0)], handle="F_TWO")
    plan_two = off.plan_offsets(LOT40, [f_two], gates=[(0.5, 10.5)])
    assert [it.text for it in plan_two.items] == ["FE.0.4'E"]

    # Единственный внутренний конец у ворот (как FE.0.2'N у «FEC NE GATE» на East 55th) НЕ теряется.
    f_sole = _fence([(40.0, 25.0), (39.8, 12.0)], handle="F_SOLE")
    plan_sole = off.plan_offsets(LOT40, [f_sole], gates=[(39.8, 12.0)])
    assert any(it.text == "FE.0.2'W" for it in plan_sole.items)


def test_h29_multiline_text_spacing_and_clearance():
    """Двухстрочный блок учитывает шаг строк AutoCAD MText (1 + 1.67 = 2.67 высоты), а подписи не слипаются."""
    w, h = off._text_block("FE.0.4'N\nFE.1.3'W", 1.0, lambda s, hh: len(s) * 0.6 * hh)
    assert h == pytest.approx(1.0 + off.TEXT_LINE_SPACING)
    assert w == pytest.approx(8 * 0.6)

    # Два близких дома не накладывают рамки подписей друг на друга с учётом LABEL_CLEARANCE.
    h1 = _house([(2.0, 10.0), (12.0, 10.0), (12.0, 20.0), (2.0, 20.0)], handle="H1")
    h2 = _house([(2.2, 20.5), (12.0, 20.5), (12.0, 30.5), (2.2, 30.5)], handle="H2")
    plan = off.plan_offsets(LOT40, [h1, h2])
    boxes = [
        off._rect_at(it.point, *off._rotated_box(*off._text_block(it.text, it.height_ft, off.default_text_width), it.rotation_deg))
        for it in plan.items
    ]
    assert len(boxes) == 2
    assert not off._rects_overlap(boxes[0], boxes[1])


def test_h30_diagonal_axis_letters_snap_to_lot_orthogonal_axes():
    """Перпендикуляр скошенной боковой стороны около 45° к северу получает букву оси лота (W/E на 24 Alvine)."""
    # Повёрнутый север 67.6° (как на 24 Alvine); перпендикуляр скошенной восточной стороны ~208° (~40° от ЮВ диагонали)
    # при отвязке к осям лота (90°/0°) даёт W вместо южной четверти.
    rad = math.radians(208.0)
    letter = off.offset_letter(math.cos(rad), math.sin(rad), 67.56, lot_north_deg=90.0)
    assert letter == "W"
    # Для обычного перпендикуляра вдоль оси лота буква совпадает с compass_letter.
    assert off.offset_letter(1.0, 0.0, 90.0, lot_north_deg=90.0) == "E"
    assert off.offset_letter(0.0, 1.0, 90.0, lot_north_deg=90.0) == "N"


def test_h31_shallow_outward_jog_skipped_while_local_min_and_90deg_corner_kept():
    """Пологий изгиб наружу пропускается, а минимум у границы и реальный угол 90° сохраняются."""
    # Пологий изгиб от границы x=40 внутрь (x=39.5 -> 39.0 -> 39.5): средняя вершина дальше от границы -> пропуск.
    jog_out = _fence([(39.5, 5.0), (39.0, 15.0), (39.5, 25.0)], handle="JOG_OUT")
    plan_out = off.plan_offsets(LOT40, [jog_out])
    assert all(it.anchor != (39.0, 15.0) for it in plan_out.items)

    # Обратный изгиб к границе (x=39.0 -> 39.5 -> 39.0): средняя вершина — локальный минимум 0.5'W -> сохраняется!
    jog_in = _fence([(39.0, 5.0), (39.5, 15.0), (39.0, 25.0)], handle="JOG_IN")
    plan_in = off.plan_offsets(LOT40, [jog_in])
    assert any(it.text == "FE.0.5'W" and it.anchor == (39.5, 15.0) for it in plan_in.items)

    # Прямой угол 90° снаружи северной границы, заходящий обратно в участок (как 24 Alvine FE.3.2'N), сохраняется!
    corner90 = _fence([(38.6, 38.2), (15.0, 43.2), (15.0, 37.5)], handle="CORNER90")
    plan_90 = off.plan_offsets(LOT40, [corner90])
    assert any(it.text == "FE.3.2'N" for it in plan_90.items)


def test_h32_part_deduplication_and_post_part_dedupe():
    """Уже стоящая часть оффсета в 2 ft не дублируется, а двухстрочный FE. повторяет префикс на второй строке."""
    fence = _fence([(0.4, 1.3), (0.4, 20.0)], handle="F1")
    # Без существующих текстов — двухстрочная выноска с префиксом FE. на обеих строках.
    plan_full = off.plan_offsets(LOT40, [fence])
    assert [it.text for it in plan_full.items] == ["FE.0.4'E\nFE.1.3'N"]

    # Если «C.L.F. 0.4'E» уже стоит в 1 ft от вершины, остаётся только недостающая часть «FE.1.3'N».
    plan_trim = off.plan_offsets(LOT40, [fence], texts=[("C.L.F. 0.4'E", (0.8, 1.5), 0.9)])
    assert [it.text for it in plan_trim.items] == ["FE.1.3'N"]

    # Если второй забор у того же угла (в 1.5 ft) даёт одиночную «FE.0.4'E», POST_PART_DEDUPE убирает её дубль.
    f_dup = _fence([(0.4, 2.8), (0.4, 12.0)], handle="F2")
    plan_both = off.plan_offsets(LOT40, [fence, f_dup])
    assert [it.text for it in plan_both.items] == ["FE.0.4'E\nFE.1.3'N"]


def test_h34_h38_house_inside_middle_rotation_short_porch_skip_and_cap():
    """Дом: текст внутри контура у середины стены параллельно границе, короткий уступ < 8 ft пропускается, лимит 4."""
    # Вертикальная стена длиной 20 ft в 4.0 ft от левой границы x=0.
    house = _house([(4.0, 10.0), (16.0, 10.0), (16.0, 30.0), (4.0, 30.0)], handle="H_MAIN")
    plan = off.plan_offsets(LOT40, [house])
    assert len(plan.items) == 1
    it = plan.items[0]
    assert it.text == "4.0'E"
    assert it.style == "text"
    assert math.isclose(abs(it.rotation_deg), 90.0, abs_tol=0.01)
    assert off.point_in_polygon(it.point, house.points)
    assert it.point[1] == pytest.approx(20.0, abs=1.0)
    assert it.anchor in {(4.0, 10.0), (4.0, 30.0)}

    # Короткое крыльцо 6 ft (< HOUSE_WALL_MIN_FT = 8 ft) не перебивает основную стену 20 ft.
    house_porch = _house(
        [(4.0, 10.0), (16.0, 10.0), (16.0, 36.0), (4.0, 36.0), (4.0, 30.0), (2.5, 30.0), (2.5, 24.0), (4.0, 24.0)],
        handle="H_PORCH",
    )
    plan_p = off.plan_offsets(LOT100, [house_porch])
    assert [i.text for i in plan_p.items] == ["4.0'E"]


def test_real_five_surveys_fe_ol_and_house_inside_styles(five_surveys):
    """На 5 реальных дампах «до» FE.O/L строится в тех же 4 съёмках, что у Николая, а дома ставятся текстом внутри."""
    expected_ol = {
        "192 Geldner": 1,
        "24 Alvine": 0,
        "1373 East 55th": 1,
        "225 Aspinwall": 1,
        "6910 15th Ave": 2,
    }
    for name, (plan_wide, _plan_lot, _nik) in five_surveys.items():
        ol = [it for it in plan_wide.items if it.text == "FE.O/L"]
        assert len(ol) == expected_ol[name], name
        assert all(it.style == "leader" for it in ol)

    # На East 55th все 5 подписей домов стоят текстом внутри контуров зданий (5 vs 5 у Николая, 1.00x).
    e55_houses = [it for it in five_surveys["1373 East 55th"][0].items if it.kind == "house"]
    assert len(e55_houses) == 5
    assert all(it.style == "text" for it in e55_houses)

    # На 192 Geldner южный внешний конец 86-футового восточного забора «FE.5.2'S» теперь восстановлен.
    geldner_texts = {it.text for it in five_surveys["192 Geldner"][0].items}
    assert "FE.5.2'S" in geldner_texts
