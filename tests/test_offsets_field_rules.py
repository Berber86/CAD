"""
Шаг 4, правила 4.20 (R11–R14) против настоящих оффсетов Николая на трёх полевых чертежах.

Данные — архивы заказчика ``docs/primer*.zip`` (те же, что в ``test_offsets_alvine_pair.py``):
пара «до выставления оффсетов» / «после» для 24 Alvine Avenue, 1373 East 55th Street (Brooklyn)
и 192 Geldner Avenue. Программа видит только файл «до»; эталон — то, что Николай ДОБАВИЛ
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

FILES = {"Geldner": PRIMER3, "Brooklyn": PRIMER2, "Alvine": PRIMER1}


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
        assert not [item.text for item in fake.items if "FE" in item.text], name
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
    # Те же два угла в 5 ft друг от друга — один угол, вторая метка не ставится.
    close = off.Feature("fence", "F", ((20.0, 0.2), (25.0, 0.9)))
    rows = off.vertex_offset_rows(close, sides, max_ft=3.0, ring=ring)
    plan = off.plan_offsets(ring, [close], survey_pts=[vertex for vertex in rows])
    assert [item.text for item in plan.items] == ["FE.0.2'N"]


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
    # Три угла вдоль западной стороны на разном удалении; на сторону — не больше одного.
    fence = off.Feature(
        "fence", "F",
        ((0.9, 2.0), (0.2, 12.0), (0.7, 22.0)),
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
    Правила R11–R14 находят больше подписей Николая, чем прежний замер до ближайшей границы.

    Считаются подписи: забор 7 → 14 (вдвое), дом 4 → 6 (+50%), лишние у дома 14 → 4.
    """
    saved = off.EDGE_NORMAL_OFFSETS
    off.EDGE_NORMAL_OFFSETS = False
    try:
        old = _label_totals()
    finally:
        off.EDGE_NORMAL_OFFSETS = saved
    new = _label_totals()
    assert old["fence"][2] == 7 and new["fence"][2] == 14
    assert old["house"][2] == 4 and new["house"][2] == 6
    assert old["house"][3] == 14 and new["house"][3] == 4
    for kind in ("fence", "house"):
        assert new[kind][2] > old[kind][2], kind
        assert new[kind][3] <= old[kind][3], kind


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
