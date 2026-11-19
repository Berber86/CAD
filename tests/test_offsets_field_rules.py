"""
Шаг 4, правила 4.20 (R11–R14) против настоящих оффсетов Николая на трёх полевых чертежах.

Данные — архивы заказчика ``docs/primer*.zip`` (те же, что в ``test_offsets_alvine_pair.py``):
пара «до выставления оффсетов» / «после» для 24 Alvine Avenue, 1373 East 55th Street (Brooklyn),
192 Geldner Avenue и 378 Mother Gaston Boulevard (Brooklyn). Программа видит только файл «до»; эталон — то, что Николай ДОБАВИЛ
в файл «после». Оценка — в ``tests/test_field_dump.py``.

Что проверяется:

* R11 «своя сторона»: оффсет меряется по нормали своего края объекта — до стороны участка
  с параллельной нормалью, а не до ближайшей точки границы;
* R12 «две строки у вершины»: у вершины с двумя краями пишутся оба замера, и буквы разные;
* R13 «снятые вершины»: метка ставится у вершины с точкой съёмки;
* R13b «описание угла»: у забора и стены — только там, где ``DESC2`` называет этот объект
  (VINYL FENCE, WOOD FENCE, FEC, WALL), иначе подписывался бы каждый снятый угол участка;
* R14 «один угол — одна подпись»: повтор замера у объекта не больше двух раз, две вершины
  ближе ``NODE_DUP_FT`` — один угол, вершина на самой линии участка не подписывается;
* H17: лишних подписей — не больше 50% от подписей Николая того же вида (в сумме по трём файлам).
"""

import math
import statistics

import pytest

import offsets as off
from tests.test_field_dump import (
    _lot_polygon,
    features,
    field_plan,
    front_lines,
    load_pair,
    nikolai_labels,
    north_deg,
    plan_labels,
    score,
    score_labels,
    survey_notes,
    survey_points,
)

PRIMER3 = "docs/primer3i.zip"  # 192 Geldner Avenue
PRIMER2 = "docs/primer2i.zip"  # 1373 East 55th Street, Brooklyn
PRIMER1 = "docs/primer1i.zip"  # 24 Alvine Avenue
PRIMER4 = "docs/primer4i.zip"  # 378 Mother Gaston Boulevard, Brooklyn
PRIMER5 = "docs/primer5i.zip"  # 18 Zoe Street, Staten Island (с PDF-эталоном и PL-слоем)
PRIMER7 = "docs/primer7i.zip"  # 1412 East 64th Street, Brooklyn (25 × 100 ft, рядный дом)

FILES = {
    "Geldner": PRIMER3,
    "Brooklyn": PRIMER2,
    "Alvine": PRIMER1,
    "Gaston": PRIMER4,
}


@pytest.fixture(scope="module")
def pairs():
    return {name: load_pair(path) for name, path in FILES.items()}


@pytest.fixture(scope="module")
def results(pairs):
    out = {}
    for name, (before, after) in pairs.items():
        data, nikolai = field_plan(before, after)
        out[name] = {
            "labels": plan_labels(data["plan"]),
            "nikolai_labels": nikolai_labels(before, after),
            "parts": data["parts"],
            "nikolai": nikolai,
        }
    return out


# ------------------------------------------------------------------ R11: своя сторона


def test_r11_side_rows_prefers_parallel_normal_over_nearest_side():
    # Участок 40 × 30; забор идёт вдоль западной стороны, край — на север.
    ring = [(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0)]
    sides = off.side_lines(ring, [], 90.0)
    rows = off.side_rows((0.5, 10.0), sides, edge_dirs=[(0.0, 1.0)])
    assert [round(row.value_ft, 2) for row in rows] == [0.5]
    # Забор внутри западной линии — подпись называет сторону участка: «0.5'E» (Brooklyn).
    assert rows[0].direction == "E"
    assert rows[0].parallel
    assert rows[0].foot == pytest.approx((0.0, 10.0))
    # Тот же забор снаружи участка — «0.5'W».
    outside = off.side_rows((-0.5, 10.0), sides, edge_dirs=[(0.0, 1.0)])
    assert outside[0].direction == "W"
    assert outside[0].parallel


def test_r11_diagonal_edge_measures_to_the_side_with_matching_normal():
    ring = [(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0)]
    sides = off.side_lines(ring, [], 90.0)
    # Край повёрнут на 45°: параллельной стороны у прямоугольника нет.
    rows = off.side_rows((5.0, 5.0), sides, edge_dirs=[(0.7071, 0.7071)], max_ft=10.0)
    assert rows and not any(row.parallel for row in rows)


def test_r11_geldner_fence_beats_nearest_point_rule(results):
    """На 192 Geldner правило «своей стороны» даёт больше совпадений, чем ближайшая граница."""
    fence_expected = [p for p in results["Geldner"]["nikolai"] if p["kind"] == "fence"]
    fence_produced = [p for p in results["Geldner"]["parts"] if p["kind"] == "fence"]
    assert score(fence_produced, fence_expected) >= 7

    saved = off.EDGE_NORMAL_OFFSETS
    off.EDGE_NORMAL_OFFSETS = False
    try:
        before, after = load_pair(PRIMER3)
        data, nikolai = field_plan(before, after)
    finally:
        off.EDGE_NORMAL_OFFSETS = saved
    old_expected = [p for p in nikolai if p["kind"] == "fence"]
    old_produced = [p for p in data["parts"] if p["kind"] == "fence"]
    assert score(fence_produced, fence_expected) > score(old_produced, old_expected)


# ------------------------------------------------------------------ R12: две строки


def test_r12_two_edges_of_a_vertex_give_two_rows_of_different_sides():
    ring = [(0.0, 0.0), (60.0, 0.0), (60.0, 40.0), (0.0, 40.0)]
    sides = off.side_lines(ring, [], 90.0)
    # Г-образный забор у западной стороны: 1.5 до южной линии (край вдоль неё) и 0.2 до западной.
    feature = off.Feature("fence", "F", ((0.2, 1.5), (30.0, 1.5), (30.0, 20.0)))
    rows = off.vertex_offset_rows(feature, sides, max_ft=3.0, ring=ring)[(0.2, 1.5)]
    assert [round(row.value_ft, 1) for row in rows] == [1.5, 0.2]
    assert {row.direction for row in rows} == {"N", "E"}


def test_r12_second_row_is_dropped_beyond_the_threshold():
    ring = [(0.0, 0.0), (60.0, 0.0), (60.0, 40.0), (0.0, 40.0)]
    sides = off.side_lines(ring, [], 90.0)
    # Второй замер вершины (0.2 до запада) ближе SECOND_EDGE_FT — две строки; с порогом 0.1 —
    # только основной, 1.5 до юга.
    feature = off.Feature("fence", "F", ((0.2, 1.5), (30.0, 1.5), (30.0, 20.0)))
    both = off.vertex_offset_rows(feature, sides, max_ft=3.0, second_ft=2.0, ring=ring)
    assert [round(row.value_ft, 1) for row in both[(0.2, 1.5)]] == [1.5, 0.2]
    only_first = off.vertex_offset_rows(feature, sides, max_ft=3.0, second_ft=0.1, ring=ring)
    assert [round(row.value_ft, 1) for row in only_first[(0.2, 1.5)]] == [1.5]


def test_r12_geldner_vertex_with_two_edges_is_one_two_line_label(results):
    texts = [item["text"] for item in results["Geldner"]["labels"]]
    assert any("0.1'E" in text and "1.2'S" in text for text in texts)


# ------------------------------------------------------------------ R13 / R13b: снятые вершины


def test_r13_vertex_without_survey_point_is_not_labelled():
    ring = [(0.0, 0.0), (60.0, 0.0), (60.0, 40.0), (0.0, 40.0)]
    front = [[(0.0, 0.0), (60.0, 0.0)]]
    fence = off.Feature("fence", "F", ((20.0, 0.4), (50.0, 0.4)))
    plan = off.plan_offsets(ring, [fence], front=front, survey_pts=[(20.0, 0.4)])
    assert [item.text for item in plan.items] == ["FE.0.4'N"]


def test_r13b_fence_vertex_needs_a_description_that_names_the_fence():
    ring = [(0.0, 0.0), (60.0, 0.0), (60.0, 40.0), (0.0, 40.0)]
    fence = off.Feature("fence", "F", ((20.0, 0.4), (50.0, 0.4)))
    points = [(20.0, 0.4), (50.0, 0.4)]
    # Точки съёмки есть, но описание говорит про дом — у забора подписи нет.
    no_fence = [(p, "1 STY FRAME") for p in points]
    plan = off.plan_offsets(ring, [fence], survey_pts=points, survey_notes=no_fence)
    assert plan.items == []
    with_fence = [(points[0], "VINYL FENCE"), (points[1], "1 STY FRAME")]
    plan = off.plan_offsets(ring, [fence], survey_pts=points, survey_notes=with_fence)
    assert [item.text for item in plan.items] == ["FE.0.4'N"]


def test_r13b_descriptions_are_read_from_the_dump(pairs):
    before, after = pairs["Geldner"]
    descriptions = {
        str((attribute or {}).get("text", "")).upper()
        for entity in before.get("entities", [])
        for attribute in (entity.get("attributes") or [])
        if str(attribute.get("tag")) == "DESC2"
    }
    assert any("FENCE" in text for text in descriptions)


def test_r13b_fence_labels_need_a_fence_description_on_all_three_files(pairs):
    """
    R13b решает, быть ли подписи: если ни одна точка съёмки у забора не называет забор
    (``VINYL FENCE``, ``WOOD FENCE``, ``F E C``), подписей забора не остаётся вовсе.
    """
    for name, (before, _after) in pairs.items():
        ring = [(float(x), float(y)) for x, y in _lot_polygon(before).exterior.coords]
        common = dict(
            front=front_lines(before),
            scale=16.0,
            north_deg=north_deg(before),
            texts=[
                (str(e.get("text")), tuple(e["pt"][:2]), float(e.get("h") or 0.0))
                for e in before.get("entities", [])
                if e.get("type") in {"Text", "MText"} and e.get("pt")
            ],
            survey_pts=survey_points(before),
        )
        real = off.plan_offsets(
            ring, features(before), survey_notes=survey_notes(before), **common
        )
        fake = off.plan_offsets(
            ring,
            features(before),
            survey_notes=[(point, "1 STY FRAME") for point, _text in survey_notes(before)],
            **common,
        )
        assert [item.text for item in real.items if "FE" in item.text], name
        # Точки пересечения забора с продолжением стороны (H27) — не вершины объекта,
        # у них точки съёмки не бывает; правило R13b к ним не относится.
        assert not [
            item.text for item in fake.items if "FE" in item.text and not item.crossing
        ], name
        # Без описаний вообще правило не применяется — как в 4.19.
        plain = off.plan_offsets(ring, features(before), survey_notes=[], **common)
        assert len([i for i in plain.items if "FE" in i.text]) == len(
            [i for i in real.items if "FE" in i.text]
        ), name


# ------------------------------------------------------------------ R14: один угол — одна подпись


def test_r14_same_measurement_is_repeated_at_most_twice_per_feature():
    ring = [(0.0, 0.0), (60.0, 0.0), (60.0, 40.0), (0.0, 40.0)]
    sides = off.side_lines(ring, [], 90.0)
    # Углы на 15 ft друг от друга: ближние дубли отсекает отдельное правило (радиус 10 ft).
    fence = off.Feature(
        "fence", "F", ((5.0, 0.35), (20.0, 0.35), (35.0, 0.35), (50.0, 0.35))
    )
    rows = off.vertex_offset_rows(fence, sides, max_ft=3.0, ring=ring)
    # Кэп на сторону здесь снят: проверяется именно повтор одного замера у объекта.
    saved = off.MAX_CORNERS_PER_SIDE
    off.MAX_CORNERS_PER_SIDE = 0
    try:
        plan = off.plan_offsets(ring, [fence], survey_pts=[vertex for vertex in rows])
    finally:
        off.MAX_CORNERS_PER_SIDE = saved
    texts = [item.text for item in plan.items]
    assert off.MAX_SAME_VALUE_PER_FEATURE == 2
    assert texts.count("FE.0.4'N") == off.MAX_SAME_VALUE_PER_FEATURE


def test_r13c_cap_per_side_does_not_merge_far_apart_corners():
    """Углы одной стороны, разнесённые дальше SIDE_CAP_SPACING_FT, подписываются оба."""
    ring = [(0.0, 0.0), (60.0, 0.0), (60.0, 40.0), (0.0, 40.0)]
    sides = off.side_lines(ring, [], 90.0)
    # Два угла вдоль южной стороны: 0.2 и 0.9 — на 50 ft друг от друга.
    fence = off.Feature("fence", "F", ((5.0, 0.2), (55.0, 0.9)))
    rows = off.vertex_offset_rows(fence, sides, max_ft=3.0, ring=ring)
    plan = off.plan_offsets(ring, [fence], survey_pts=[vertex for vertex in rows])
    assert sorted(item.text for item in plan.items) == ["FE.0.2'N", "FE.0.9'N"]
    # Те же два угла в 5 ft друг от друга при ОДНОМ замере — один угол, вторая метка
    # не ставится (перекос, R13d, сюда не относится: замеры равны).
    close = off.Feature("fence", "F", ((20.0, 0.2), (25.0, 0.2)))
    rows = off.vertex_offset_rows(close, sides, max_ft=3.0, ring=ring)
    plan = off.plan_offsets(ring, [close], survey_pts=[vertex for vertex in rows])
    assert [item.text for item in plan.items] == ["FE.0.2'N"]


def test_r10_text_without_leader_runs_along_its_line():
    """R10: подпись без выноски повёрнута вдоль своей линии — 0° у горизонтальной, 90° у вертикальной."""
    ring = [(0.0, 0.0), (60.0, 0.0), (60.0, 40.0), (0.0, 40.0)]
    sides = off.side_lines(ring, [], 90.0)
    # Дом внутри участка у западной (вертикальной) стороны: текст встаёт внутри строения.
    house = off.Feature("house", "H", ((0.4, 5.0), (20.0, 5.0), (20.0, 25.0), (0.4, 25.0)))
    assert off.TEXT_FOLLOWS_BOUNDARY is True
    rows = off.vertex_offset_rows(house, sides, max_ft=3.0, ring=ring)
    plan = off.plan_offsets(ring, [house], survey_pts=list(rows))
    text_items = [item for item in plan.items if item.style == "text"]
    assert text_items, plan.items
    assert all(abs(item.rotation_deg - 90.0) < 1.0 for item in text_items), text_items
    # Флаг снят — подпись горизонтальна: поворот идёт от правила, а не от раскладки.
    saved = off.TEXT_FOLLOWS_BOUNDARY
    off.TEXT_FOLLOWS_BOUNDARY = False
    try:
        plan = off.plan_offsets(ring, [house], survey_pts=list(rows))
    finally:
        off.TEXT_FOLLOWS_BOUNDARY = saved
    assert all(item.rotation_deg == 0.0 for item in plan.items if item.style == "text")


def test_r10_geldner_house_text_is_vertical_like_nikolai():
    """На 192 Geldner подписи дома без выносок стоят у вертикальных стен — у Николая 90°."""
    before, after = load_pair(PRIMER3)
    data, _nikolai = field_plan(before, after)
    items = [item for item in data["plan"].items if item.kind == "house" and item.style == "text"]
    assert items
    assert all(abs(item.rotation_deg - 90.0) < 1.0 for item in items), [
        (item.text, item.rotation_deg) for item in items
    ]


def test_r13d_second_corner_of_the_same_side_is_labelled_when_the_wall_is_skewed():
    """R13d: второй угол стороны подписывается, если замер другой (стена идёт с перекосом)."""
    ring = [(0.0, 0.0), (60.0, 0.0), (60.0, 40.0), (0.0, 40.0)]
    sides = off.side_lines(ring, [], 90.0)
    # Два угла южной стороны в 5 ft друг от друга: 0.2 и 0.9 — у Николая (Gaston) так
    # подписаны оба конца стены, идущей вдоль границы с перекосом.
    skewed = off.Feature("fence", "F", ((20.0, 0.2), (20.0, 0.9), (25.0, 0.9)))
    rows = off.vertex_offset_rows(skewed, sides, max_ft=3.0, ring=ring)
    plan = off.plan_offsets(ring, [skewed], survey_pts=[vertex for vertex in rows])
    assert sorted(item.text for item in plan.items) == ["FE.0.2'N", "FE.0.9'N"]
    # Флаг в ноль — второй угол стороны не ставится, остаётся ближайший.
    saved = off.SKEW_PAIR_FT
    off.SKEW_PAIR_FT = 0.0
    try:
        plan = off.plan_offsets(ring, [skewed], survey_pts=[vertex for vertex in rows])
    finally:
        off.SKEW_PAIR_FT = saved
    assert [item.text for item in plan.items] == ["FE.0.2'N"]


def test_r13d_mother_gaston_skewed_fence_corner_is_found(results):
    """На 378 Mother Gaston перекос даёт вторую подпись забора: `FE.2.2'S` у южного конца."""
    texts = [item["text"] for item in results["Gaston"]["labels"] if item["kind"] == "fence"]
    assert "FE.2.2'S" in texts
    saved = off.SKEW_PAIR_FT
    off.SKEW_PAIR_FT = 0.0
    try:
        data, _nikolai = field_plan(*load_pair(PRIMER4))
    finally:
        off.SKEW_PAIR_FT = saved
    assert "FE.2.2'S" not in [p["text"] for p in plan_labels(data["plan"]) if p["kind"] == "fence"]


def test_r12_second_row_up_to_three_feet_is_kept():
    """R12: вторая строка у вершины пишется и на 3.0 ft — Brooklyn, угол дома `2.1'N` и `3.0'W`."""
    ring = [(0.0, 0.0), (60.0, 0.0), (60.0, 40.0), (0.0, 40.0)]
    sides = off.side_lines(ring, [], 90.0)
    feature = off.Feature("house", "H", ((3.0, 2.1), (20.0, 2.1), (20.0, 20.0)))
    rows = off.vertex_offset_rows(feature, sides, max_ft=5.0, second_ft=off.SECOND_EDGE_FT, ring=ring)
    assert [round(row.value_ft, 1) for row in rows[(3.0, 2.1)]] == [2.1, 3.0]
    assert off.SECOND_EDGE_FT == 3.0


def test_r14_two_ends_of_one_short_fence_are_one_offset():
    ring = [(0.0, 0.0), (60.0, 0.0), (60.0, 40.0), (0.0, 40.0)]
    sides = off.side_lines(ring, [], 90.0)
    fence = off.Feature("fence", "F", ((50.0, -0.07), (50.0, 0.09)))
    rows = off.vertex_offset_rows(fence, sides, max_ft=3.0, ring=ring)
    assert len(rows) == 1


def test_r14_vertex_on_the_lot_line_is_not_an_offset():
    ring = [(0.0, 0.0), (60.0, 0.0), (60.0, 40.0), (0.0, 40.0)]
    sides = off.side_lines(ring, [], 90.0)
    fence = off.Feature("fence", "F", ((10.0, 0.02), (40.0, 0.02)))
    assert off.vertex_offset_rows(fence, sides, max_ft=3.0, ring=ring) == {}


def test_r14_two_objects_meeting_at_the_same_lot_corner_share_one_label():
    ring = [(0.0, 0.0), (60.0, 0.0), (60.0, 40.0), (0.0, 40.0)]
    left = off.Feature("fence", "A", ((0.3, 5.0), (0.3, 20.0)))
    bottom = off.Feature("fence", "B", ((0.35, 5.0), (20.0, 5.0)))
    plan = off.plan_offsets(ring, [left, bottom], survey_pts=[(0.3, 5.0), (0.35, 5.0)])
    assert len(plan.items) == 1


def test_r14_per_side_cap_keeps_the_closest_corner():
    ring = [(0.0, 0.0), (60.0, 0.0), (60.0, 40.0), (0.0, 40.0)]
    sides = off.side_lines(ring, [], 90.0)
    # Три угла вдоль западной стороны на разном удалении с ОДНИМ замером (0.5) — на сторону
    # не больше одного угла: перекос (R13d) здесь не срабатывает, замеры равны.
    fence = off.Feature(
        "fence", "F",
        ((0.5, 2.0), (0.5, 12.0), (0.5, 22.0)),
    )
    rows = off.vertex_offset_rows(fence, sides, max_ft=3.0, ring=ring)
    plan = off.plan_offsets(ring, [fence], survey_pts=[vertex for vertex in rows])
    assert len(plan.items) == off.MAX_CORNERS_PER_SIDE


def test_r14_rules_apply_only_when_the_drawing_has_survey_points():
    """Без точек съёмки остаётся прежнее правило: замер до ближайшей границы."""
    ring = [(0.0, 0.0), (60.0, 0.0), (60.0, 40.0), (0.0, 40.0)]
    fence = off.Feature("fence", "F", ((20.0, 0.4), (50.0, 0.4)))
    plan = off.plan_offsets(ring, [fence])
    assert [item.text for item in plan.items] == ["FE.0.4'N"]


# ------------------------------------------------------------------ H17 на трёх файлах


def test_h17_extra_labels_are_within_half_of_nikolai_in_total(results):
    """Лишних подписей — не больше 50% от подписей Николая, по каждому виду объекта."""
    for kind in ("fence", "house"):
        expected = sum(
            1 for file in results.values() for p in file["nikolai_labels"] if p["kind"] == kind
        )
        produced = sum(1 for file in results.values() for p in file["labels"] if p["kind"] == kind)
        hits = sum(
            score_labels(
                [p for p in file["labels"] if p["kind"] == kind],
                [p for p in file["nikolai_labels"] if p["kind"] == kind],
            )
            for file in results.values()
        )
        extra = produced - hits
        assert extra <= 0.5 * expected, (
            f"{kind}: программа поставила {produced} подписей, совпало {hits}, лишних {extra} "
            f"при {expected} у Николая"
        )


def test_h17_rules_find_more_of_nikolai_than_4_19():
    """
    Правила 4.21–4.25 находят больше подписей Николая, чем прежний замер до ближайшей границы.

    Считаются подписи по четырём парам: забор 9 → 23, дом 7 → 11, лишние у дома 16 → 10
    (было 17 до 4.32: с пределом «подпись не дальше ``TEXT_MAX_FROM_ANCHOR_FT`` от своей вершины»
    одна лишняя подпись дома ставится выноской у самой вершины и дублирует соседнюю).
    Рост по заборам — точки пересечения забора с продолжением стороны (0.7'E, 1.4'E),
    конец длинного прогона до 6 ft (5.2'S) и второй угол стены с перекосом (4.25, R13d);
    по дому — вторая строка у вершины до 3 ft (4.25, R12) и запасной предел 4 ft для дома,
    который стоит дальше общего предела целиком (4.31, взято из ветки соседа 4.37: 24 Alvine,
    `3.5'S`).
    """
    saved = off.EDGE_NORMAL_OFFSETS
    off.EDGE_NORMAL_OFFSETS = False
    try:
        old = _label_totals()
    finally:
        off.EDGE_NORMAL_OFFSETS = saved
    new = _label_totals()
    assert old["fence"][2] == 9 and new["fence"][2] == 23
    assert old["house"][2] == 7 and new["house"][2] == 11
    assert old["house"][3] == 16 and new["house"][3] == 10
    for kind in ("fence", "house"):
        assert new[kind][2] > old[kind][2], kind
        # H17: лишних не больше половины подписей Николая того же вида.
        assert new[kind][3] <= 0.5 * new[kind][0], kind


def test_fourth_pair_mother_gaston_scores_by_the_same_rules(results):
    """
    378 Mother Gaston (4-я пара заказчика): лист «после» сдвинут на 1440 ft и повёрнут на
    0.03° относительно «до», поэтому эталон переводится в систему «до» по общим ручкам.
    Пока перевод шёл в обратную сторону (``_to_before`` применял подобие «до → после»),
    с этим чертежом сравнивать было нечем: 0 совпадений. После исправления правила
    находят 5 подписей забора из 6 (пятая — ``FE.2.2'S``, второй угол стены с перекосом, 4.25)
    и 2 подписи дома из 5.

    У дома на этом листе правила ставят 6 подписей при 5 у Николая (лишних 4 — 80% эталона
    этого файла; суммарно по четырём парам H17 выполняется: 7 из 20). Причина — плотная
    городская застройка: куски стен и навесы (`ST-OHED`) распознаются как дом, и подпись
    ложится рядом с чужой вершиной.
    """
    file = results["Gaston"]
    assert len([p for p in file["nikolai_labels"] if p["kind"] == "fence"]) == 6
    assert len([p for p in file["nikolai_labels"] if p["kind"] == "house"]) == 5
    for kind, expected in (("fence", 5), ("house", 2)):
        got = [p for p in file["labels"] if p["kind"] == kind]
        exp = [p for p in file["nikolai_labels"] if p["kind"] == kind]
        assert score_labels(got, exp) == expected, kind
    assert len([p for p in file["labels"] if p["kind"] == "house"]) == 6


def test_after_frame_is_translated_back_into_before():
    """Точка листа «после» переводится в систему «до» обратным подобием, а не прямым."""
    from tests.test_field_dump import _frame_transform, _to_before

    before, after = load_pair(PRIMER4)
    transform = _frame_transform(before, after)
    old = {str(e.get("handle")): e for e in before.get("entities", []) if e.get("handle")}
    checked = 0
    for entity in after.get("entities", []):
        twin = old.get(str(entity.get("handle")))
        if twin is None or entity.get("type") != twin.get("type"):
            continue
        if not entity.get("pt") or not twin.get("pt"):
            continue
        moved = _to_before(transform, entity["pt"][:2])
        assert pytest.approx(moved[0], abs=0.15) == twin["pt"][0]
        assert pytest.approx(moved[1], abs=0.15) == twin["pt"][1]
        checked += 1
        if checked >= 20:
            break
    assert checked >= 5


def _label_totals() -> dict[str, list[int]]:
    """(эталон, подписей, совпало, лишних) по видам объектов — на текущих правилах."""
    totals = {kind: [0, 0, 0, 0] for kind in ("fence", "house")}
    for path in FILES.values():
        before, after = load_pair(path)
        data, _nikolai = field_plan(before, after)
        produced = plan_labels(data["plan"])
        expected = nikolai_labels(before, after)
        for kind in ("fence", "house"):
            got = [p for p in produced if p["kind"] == kind]
            exp = [p for p in expected if p["kind"] == kind]
            hits = score_labels(got, exp)
            for index, value in enumerate((len(exp), len(got), hits, len(got) - hits)):
                totals[kind][index] += value
    return totals


# ------------------------------------------------------------------ пятая пара: 18 Zoe Street


@pytest.fixture(scope="module")
def zoe():
    before, after = load_pair(PRIMER5)
    data, _nikolai = field_plan(before, after)
    return {
        "before": before,
        "labels": plan_labels(data["plan"]),
        "nikolai_labels": nikolai_labels(before, after),
        "ring": data["ring"],
        "plan": data["plan"],
    }


def test_fifth_pair_zoe_property_line_is_the_new_pl_layer(zoe):
    """
    18 Zoe Street: заказчик сменил стиль — граница участка лежит в новом слое ``PL``.

    Площадь контура 4469.9 sq ft совпадает с эталоном на PDF («TOTAL AREA: 4,470.0 Sq. Feet»).
    Слой ``PL`` — три отрезка (запад, юг, восток), фасад замыкается красной линией улицы
    (``M-ROW-LINE``), как и у прежних пар.
    """
    from tests.test_field_dump import _lot_polygon

    ring = _lot_polygon(zoe["before"])
    assert ring is not None
    assert ring.area == pytest.approx(4469.9, abs=2.0)
    layers = {str(item.get("layer")) for item in zoe["before"]["entities"]
              if item.get("type") in {"Line", "Polyline"}}
    assert "PL" in layers and "M-ROW-LINE" in layers


def test_fifth_pair_zoe_scores_by_the_same_rules(zoe):
    """
    Первый замер новой пары: забор 7 эталон / 8 подписей / 3 совпало, дом 2 / 0 / 0.

    Совпали `FE.0.3'N`, `FE.0.5'N` и двухчастная `FE.0.4'E` / `FE.0.8'N` у юго-западного угла.
    Не сходится: `FE.0.3'S` / `FE.0.1'W` у юго-восточного угла (у нас `FE.1.2'E` — мерим до
    восточной стороны, он до южной и западной), `FE.5.4'W` и `FE.5.3'W` (забор у фасада),
    `BLDG` / `6.8'S` (дом стоит в 6.6–6.8 ft от восточной стороны — дальше `EDGE_MAX_FT`, а
    поднимать общий предел нельзя: на четырёх парах это даёт +8…+17 лишних подписей дома),
    `2.0'N` (сарай). Все — кандидаты следующей версии.
    """
    for kind, (expected, produced, hits) in (
        ("fence", (7, 8, 3)),
        ("house", (2, 0, 0)),
    ):
        got = [p for p in zoe["labels"] if p["kind"] == kind]
        exp = [p for p in zoe["nikolai_labels"] if p["kind"] == kind]
        assert (len(exp), len(got), score_labels(got, exp)) == (expected, produced, hits), kind
    texts = [p["text"] for p in zoe["labels"] if p["kind"] == "fence"]
    assert "FE.0.5'N" in texts
    assert any("0.4'S" in text and "0.8'N" in text for text in texts)


def test_house_standing_beyond_the_common_limit_is_labelled(results):
    """
    R18 (4.31): дом, который стоит дальше общего предела целиком, меряется до ``HOUSE_EDGE_MAX_FT``.

    Взято из ветки соседа (`arena/3c6f7f95-cad`, 4.37: предел дома 7 ft); у нас правило работает
    как запасной предел и только для дома, у которого нет ни одной вершины ближе ``EDGE_MAX_FT``.
    24 Alvine: стена дома 3.5 ft от южной стороны, у Николая `3.5'S`, у нас `3.4'S` (0.1 ft).
    С выключенным правилом подписи нет, а счёт по четырём парам падает на два совпадения.
    """
    alvine = [p for p in results["Alvine"]["labels"] if p["kind"] == "house"]
    assert any(abs(part[0] - 3.4) < 0.11 for p in alvine for part in p["parts"]), [
        p["text"] for p in alvine
    ]
    saved = off.HOUSE_EDGE_MAX_FT
    off.HOUSE_EDGE_MAX_FT = 0.0
    try:
        before, after = load_pair(PRIMER1)
        data, _nikolai = field_plan(before, after)
        narrow = [p for p in plan_labels(data["plan"]) if p["kind"] == "house"]
    finally:
        off.HOUSE_EDGE_MAX_FT = saved
    assert len(alvine) > len(narrow)


# --------------------------------------- шестая пара: 1412 East 64th Street, Brooklyn


@pytest.fixture(scope="module")
def east64():
    before, after = load_pair(PRIMER7)
    data, _nikolai = field_plan(before, after)
    return {
        "before": before,
        "labels": plan_labels(data["plan"]),
        "nikolai_labels": nikolai_labels(before, after),
        "ring": data["ring"],
        "plan": data["plan"],
    }


def test_sixth_pair_east_64th_property_line_is_a_25_by_100_rectangle(east64):
    """
    1412 East 64th Street: узкий рядный участок 25.00' × 100.00' (2500 sq ft) — самый узкий в наборе.

    Граница снова в слое ``PL`` (три отрезка: запад, юг, восток), фасад замыкает ``M-ROW-LINE``;
    площадь контура 2500.0 sq ft совпадает с листом («25.00'» сверху и внизу, «100.00'» по бокам).
    В отличие от Zoe, здесь у дома есть собственные линии ``ST-BLDG-LINE`` (восточная стена в 0.3 ft
    от границы) — проверка, что имя ``PL`` по-прежнему выигрывает у старого слоя.
    """
    ring = _lot_polygon(east64["before"])
    assert ring is not None
    assert ring.area == pytest.approx(2500.0, abs=2.0)
    lines = [item for item in east64["before"]["entities"]
             if item.get("type") in {"Line", "Polyline"}]
    layers = {str(item.get("layer")) for item in lines}
    assert "PL" in layers and "M-ROW-LINE" in layers
    assert sum(1 for item in lines if str(item.get("layer")) == "PL") == 3


def test_sixth_pair_east_64th_scores_by_the_same_rules(east64):
    """
    Замер шестой пары: забор 8 эталон / 7 наших / 7 совпало, дом 6 / 7 / 3.

    Дом вырос после 4.29: R17 ставит две подписи устоев (`3.0'E` и `3.1'E` — у Николая `3.0'E`
    дважды), и обе считаются подписями дома, потому что у устоя число голое, без приставки.

    Забор почти весь сходится: `FE.0.2'N`, `FE.0.8'S`, `FE.1.0'S`, `FE.0.9'N`, `FE.1.7'N`,
    `FE.1.2'N` (у нас `FE.1.3'N` — 0.1 ft, в допуске) и `FE.0.2'N`; его `FE.0.1'S` у фасада у нас
    нет (забор фасада меряется к красной линии, а не к боковой стороне). Дом — снова слабое место:
    совпала одна `0.2'S` (у нас `0.3'S`, 0.1 ft), лишние `0.1'N|1.3'W`, `0.1'S`, `0.1'N`, `3.0'S`
    (он `3.2'S`), и нет обеих его `3.0'E` у фасада.

    Новое для набора: (1) он пишет подпись дома **снаружи** участка (под задней линией, над
    описанием соседа `BLDG 0.1'W`), а у нас `_place_house_text` держит текст внутри; (2) у фасада
    стоят две `3.0'E` — замер от красной линии до стены/тротуара, которых в «до» нет объектом;
    (3) `0.3'S` против `0.2'S` — расстояние по геометрии ровно 0.300 ft, то есть расходится не
    округление, а предмет замера (у него — стена соседа `C.L.F.`, у нас — восточная сторона).
    """
    for kind, (expected, produced, hits) in (
        ("fence", (8, 7, 7)),
        ("house", (6, 7, 3)),
    ):
        got = [p for p in east64["labels"] if p["kind"] == kind]
        exp = [p for p in east64["nikolai_labels"] if p["kind"] == kind]
        assert (len(exp), len(got), score_labels(got, exp)) == (expected, produced, hits), kind
    texts = [p["text"] for p in east64["labels"] if p["kind"] == "fence"]
    for text in ("FE.1.0'S", "FE.1.7'N", "FE.0.2'N"):
        assert any(text in line for line in texts), text
    # R17: два устоя у улицы — голое число до красной линии, ровно у его подписей.
    walls = [item for item in east64["plan"].items if item.kind == "wall"]
    assert [item.text for item in walls] == ["3.0'E", "3.1'E"]
    assert walls[0].anchor == pytest.approx((4981.42, 5085.75), abs=0.05)
    assert walls[1].anchor == pytest.approx((5005.69, 5085.83), abs=0.05)
    assert all(abs(item.anchor[1] - 5085.8) < 0.1 for item in walls)


def _touch_stats(paths):
    """Сколько подписей стоит ближе 0.1 ft к линии чертежа и насколько близко самая тесная."""
    from shapely.geometry import LineString, box

    close = total = 0
    worst = {}
    for path in paths:
        before, after = load_pair(path)
        lines = [
            LineString([tuple(p[:2]) for p in entity["pts"]])
            for entity in before["entities"]
            if entity.get("type") in {"Line", "Polyline"} and len(entity.get("pts") or []) > 1
        ]
        data, _ = field_plan(before, after)
        margins = []
        for item in data["plan"].items:
            width, height = off._text_block(item.text, item.height_ft, off.default_text_width)
            x, y = item.point
            rect = box(x - width / 2, y - height / 2, x + width / 2, y + height / 2)
            margins.append(min(rect.distance(line) for line in lines))
        total += len(margins)
        close += sum(1 for margin in margins if margin < 0.1)
        worst[path] = min(margins)
    return close, total, worst


def test_line_clearance_flag_keeps_text_off_the_lines():
    """
    `LINE_CLEARANCE_FT` (4.28) — просвет от текста подписи до линий чертежа, в высотах текста.

    У Николая текст оффсета стоит от ближайшей линии на 0.71 (дом) и 1.60 (забор); у нас почти
    треть подписей прижимается к линиям ближе 0.1 ft. Правило **выключено** (0.0): с ним прижатых
    подписей меньше (19 → 14), но одна подпись перестаёт влезать (63 → 62, совпадений 35 → 34).
    Замер: `docs/step4_spatial_measure.py`.
    """
    assert off.LINE_CLEARANCE_FT == 0.0
    paths = [PRIMER1, PRIMER2, PRIMER3, PRIMER4, PRIMER5]
    loose_close, loose_total, loose_worst = _touch_stats(paths)
    try:
        off.LINE_CLEARANCE_FT = 0.5
        tight_close, tight_total, tight_worst = _touch_stats(paths)
    finally:
        off.LINE_CLEARANCE_FT = 0.0
    assert loose_close > 15, loose_close
    assert tight_close < loose_close
    assert tight_total <= loose_total
    # Ни на одном чертеже подпись не стала стоять ближе к линии, чем без правила.
    for path in paths:
        assert tight_worst[path] >= loose_worst[path] - 1e-9, path


def test_leader_length_matches_the_customer():
    """
    R15 (4.28): длинная выноска оффсета пробуется первой.

    У Николая длина выноски от острия стрелки до края блока — 2.61 ft по 33 подписям забора
    (p10 2.06, p90 3.23, max 3.91) и 2.59 по шести подписям дома. Прежняя лестница начиналась с
    0 ft и давала медиану 2.01: подписи жались к объекту, а слева-справа от них оставались
    линии. Короткие варианты остались в конце — на тесные места, иначе две подписи на пяти
    парах перестают влезать (63 → 61). Замер: docs/step4_spatial_measure.py.
    """
    assert off.LEADER_DISTANCES_FT[0] >= 2.0
    assert off.LEADER_DISTANCES_FT[-1] == 0.0 or 0.0 in off.LEADER_DISTANCES_FT
    lengths = []
    for path in (PRIMER1, PRIMER2, PRIMER3, PRIMER4, PRIMER5):
        before, after = load_pair(path)
        data, _ = field_plan(before, after)
        lengths += [math.dist(item.leader[0], item.leader[1])
                    for item in data["plan"].items if item.leader]
    # Медиана у него 2.61; допускаем 2.3–3.3, лишь бы не возвращаться к 2.0.
    assert 2.3 <= statistics.median(lengths) <= 3.3
    # У него десятая часть выносок короче 2.06 ft — у нас короткие тоже должны оставаться.
    assert sum(1 for value in lengths if value < 2.2) / len(lengths) < 0.25


def _anchor_spread(paths):
    """Наибольшее расстояние от центра подписи до её вершины (футы) и число подписей, по чертежам."""
    worst, counts = {}, {}
    for path in paths:
        before, after = load_pair(path)
        data, _ = field_plan(before, after)
        items = data["plan"].items
        worst[path] = max((math.dist(item.point, item.anchor) for item in items), default=0.0)
        counts[path] = len(items)
    return worst, counts


def test_label_stays_near_its_vertex():
    """
    R19 (4.32): подпись оффсета не уходит далеко от своей вершины.

    Николай ставит подпись у замкнутой точки съёмки: центр блока — 4.7 ft от вершины
    (медиана по шести парам, p90 5.3), а если рядом места нет, он рисует выноску, а не ищет
    место в другом конце строения. У нас без правила подпись дома гуляла вдоль стены внутрь
    строения: на 1412 East 64th — `0.1'N|1.3'W` в **29.6 ft** от своего угла (у него там
    `BLDG 0.1'W` у самой вершины), на 24 Alvine — `2.5'S` в 12.3, на 192 Geldner — 8.6,
    на 378 Mother Gaston — 8.2 ft. Предел ``TEXT_MAX_FROM_ANCHOR_FT`` = 6 ft: подписи на шести
    парах те же (78), совпадения те же (47), предупреждение одно — дальняя подпись просто
    ставится выноской. Замер: `docs/step4_spatial_measure.py`.
    """
    assert off.TEXT_MAX_FROM_ANCHOR_FT == pytest.approx(6.0)
    paths = [PRIMER1, PRIMER2, PRIMER3, PRIMER4, PRIMER5, PRIMER7]
    near, counts = _anchor_spread(paths)
    saved = off.TEXT_MAX_FROM_ANCHOR_FT
    try:
        off.TEXT_MAX_FROM_ANCHOR_FT = 0.0
        far, far_counts = _anchor_spread(paths)
    finally:
        off.TEXT_MAX_FROM_ANCHOR_FT = saved
    # Без правила подпись уезжала на 8–30 ft; самая дальняя — East 64th.
    assert far[PRIMER7] > 20.0, far[PRIMER7]
    assert far[PRIMER3] > 8.0 and far[PRIMER1] > 10.0
    for path in paths:
        assert near[path] <= 6.5, (path, near[path])
        assert near[path] <= far[path] + 1e-9, path
        assert counts[path] == far_counts[path], path


def _lot_gaps(paths):
    """Просвет от блока подписи до ближайшей стороны участка (футы), по всем подписям."""
    from shapely.geometry import box
    gaps = []
    for path in paths:
        before, after = load_pair(path)
        polygon = _lot_polygon(before)
        data, _ = field_plan(before, after)
        for item in data["plan"].items:
            width, height = off._text_block(item.text, item.height_ft, off.default_text_width)
            x, y = item.point
            gaps.append(box(x - width / 2, y - height / 2, x + width / 2, y + height / 2)
                        .distance(polygon.exterior))
    return gaps


def test_text_is_kept_off_the_lot_sides():
    """
    R19 (4.32): просвет до сторон участка — первая ступень раскладки, с откатом.

    По скриншотам Николая с 1412 East 64th его текст стоит от границы на 0.6–0.8 ft: конец
    выноски — у самого края блока, а сам блок отведён от линии. У нас пять подписей из 78
    прижимались к границе ближе 0.05 ft (Zoe `FE.5.2'N` — 0.04, Geldner `FE.5.2'S` — 0.02).
    Жёсткое требование не годилось (`LINE_CLEARANCE_FT`: 78 → 77 подписей, 1 → 4 предупреждения),
    поэтому просвет ``TEXT_LOT_GAP_FT`` = 0.6 — **первая ступень с откатом**: подпись, которой
    просвета не хватило, ставится как раньше. Итог шести пар: подписи 78 и совпадения 47 —
    как без правила, медиана просвета 0.65 → 0.78 ft (у Николая 0.63–0.79).
    Замер: `docs/step4_spatial_measure.py`.
    """
    assert off.TEXT_LOT_GAP_FT > 0.0
    paths = [PRIMER1, PRIMER2, PRIMER3, PRIMER4, PRIMER5, PRIMER7]
    off_gaps = _lot_gaps(paths)
    saved = off.TEXT_LOT_GAP_FT
    try:
        off.TEXT_LOT_GAP_FT = 0.0
        plain_gaps = _lot_gaps(paths)
    finally:
        off.TEXT_LOT_GAP_FT = saved
    assert len(off_gaps) == len(plain_gaps)
    # Подписи не теряются: у всех, кому просвет не достался, остаётся прежнее место.
    assert statistics.median(off_gaps) > statistics.median(plain_gaps) + 0.05
    assert statistics.median(off_gaps) >= 0.7
    # Прижатых к границе подписей не становится больше.
    assert sum(1 for gap in off_gaps if gap < 0.05) <= sum(1 for gap in plain_gaps if gap < 0.05)
