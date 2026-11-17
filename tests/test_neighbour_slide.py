"""Развод наложений (3.86): соседи, лежащие поверх нашего участка, съезжают наружу до общей межи."""

from __future__ import annotations

import math

import pytest
from shapely import affinity
from shapely.geometry import Polygon

from neighbour_slide import SHARED_FRACTION, slide_off_overlap
from test_gui_smoke import gui as _gui_fixture  # фикстура окна для проверки кнопок

gui = _gui_fixture


def _sq(x, y, w, h):
    return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]


def _after(main_pts, nb_pts, res, band: float = 0.05):
    """Наложение и общая межа после сдвига; полоса допуска — как у алгоритма (0.05 ft + клин)."""
    moved = affinity.translate(Polygon(nb_pts), res.dx, res.dy)
    main = Polygon(main_pts)
    return main.intersection(moved).area, moved.boundary.intersection(main.boundary.buffer(band)).length


def test_shared_fraction_is_the_join_rule():
    from conflict_resolver import JOIN_MIN_SHARED_FRACTION

    assert SHARED_FRACTION == JOIN_MIN_SHARED_FRACTION


def test_gap_is_not_touched():
    main = _sq(0, 0, 100, 100)
    res = slide_off_overlap(main, _sq(110, 0, 40, 40))
    assert res.status == "no_overlap"
    assert res.dx == 0.0 and res.dy == 0.0


def test_corner_overlap_moves_out_with_a_shared_edge():
    main = _sq(0, 0, 100, 100)
    nb = _sq(90, 90, 50, 50)
    res = slide_off_overlap(main, nb)
    assert res.status == "moved"
    assert res.distance_ft == pytest.approx(10.0, abs=0.02)
    area, shared = _after(main, nb, res)
    assert area <= 0.5
    assert shared >= 10.0 - 1e-6


def test_west_overlap_slides_west_to_share_the_full_side():
    main = _sq(0, 0, 100, 100)
    nb = _sq(-20, 40, 50, 20)
    res = slide_off_overlap(main, nb)
    assert res.status == "moved"
    assert res.distance_ft == pytest.approx(30.0, abs=0.02)
    assert res.direction_deg == pytest.approx(180.0, abs=0.5)
    assert res.shared_ft == pytest.approx(20.0, abs=0.2)


def test_real_like_offset_rectangles_slide_along_the_side():
    """
    Как LOT 47 против PARCEL I: обе фигуры — прямоугольники с одинаковым поворотом, сосед 130×88.
    Один сдвиг по нормали даёт точечное касание; нужен сдвиг вдоль стороны — его и ищем.
    """
    ang = math.radians(-33.4)
    ux, uy = math.cos(ang), math.sin(ang)
    vx, vy = -uy, ux
    def rect(origin, along, across):
        ox, oy = origin
        pts = [(ox, oy), (ox + ux * along, oy + uy * along)]
        pts.append((pts[1][0] + vx * across, pts[1][1] + vy * across))
        pts.append((ox + vx * across, oy + vy * across))
        return pts
    main = rect((0.0, 0.0), 100.0, 100.0)
    nb = rect((43.0, 25.0), 130.0, 88.0)
    res = slide_off_overlap(main, nb)
    assert res.status == "moved", res.note
    area, shared = _after(main, nb, res)
    assert area <= 0.5
    assert shared >= 0.10 * 130.0 - 1e-6
    # Сдвиг наружу: от нашего центра к соседу.
    cx = sum(p[0] for p in main) / 4
    cy = sum(p[1] for p in main) / 4
    nx = sum(p[0] for p in nb) / 4
    ny = sum(p[1] for p in nb) / 4
    assert res.dx * (nx - cx) + res.dy * (ny - cy) >= 0


def test_street_line_blocks_the_only_outward_way():
    """Наружу сосед может уйти только через улицу — туда не заходим."""
    main = _sq(0, 0, 100, 100)
    nb = _sq(90, 40, 30, 30)
    street = [((110, -50), (110, 150))]
    res = slide_off_overlap(main, nb, street_lines=street)
    assert res.status == "not_found"
    assert "наружу" in res.note
    free = slide_off_overlap(main, nb, street_lines=[((130, -50), (130, 150))])
    assert free.status == "moved"


def test_other_neighbour_blocks_the_move():
    main = _sq(0, 0, 100, 100)
    nb = _sq(90, 90, 50, 50)
    other = _sq(100, 100, 40, 40)
    res = slide_off_overlap(main, nb, others=[other])
    assert res.status == "not_found"


def test_rotated_neighbour_needs_a_turn_not_a_shift():
    main = _sq(0, 0, 100, 100)
    rot = list(affinity.rotate(Polygon(_sq(80, 20, 40, 40)), 30, origin=(80, 20)).exterior.coords)[:-1]
    res = slide_off_overlap(main, rot)
    assert res.status == "not_found"
    assert "поворот" in res.note


def test_translate_plan_in_place_keeps_the_object():
    from acad_builder import AcadBuilder, translate_plan_in_place
    from llm_parser import LegalDescription, Segment, load_config

    desc = LegalDescription(
        main_street="Oak Ave",
        cross_street="1st St",
        distance_to_corner_ft=50.0,
        segments=[
            Segment(ns="N", degrees=90, ew="E", distance_ft=100.0),
            Segment(ns="S", degrees=0, ew="E", distance_ft=50.0),
            Segment(ns="S", degrees=90, ew="W", distance_ft=100.0),
            Segment(ns="N", degrees=0, ew="W", distance_ft=50.0),
        ],
    )
    plan = AcadBuilder(load_config()).plan(desc, is_neighbor=True)
    before = list(plan.vertices)
    ident = id(plan)
    translate_plan_in_place(plan, 12.5, -3.0)
    assert id(plan) == ident
    assert plan.vertices[0] == pytest.approx((before[0][0] + 12.5, before[0][1] - 3.0))
    translate_plan_in_place(plan, -12.5, 3.0)
    assert plan.vertices[0] == pytest.approx(before[0])


def test_button_moves_overlapping_neighbour_and_undo_restores_it(gui):
    from acad_builder import AcadBuilder, translate_plan
    from llm_parser import LegalDescription, Segment

    app = gui.SurveyApp()
    desc = LegalDescription(
        main_street="Oak Ave",
        cross_street="1st St",
        distance_to_corner_ft=50.0,
        segments=[
            Segment(ns="N", degrees=90, ew="E", distance_ft=100.0),
            Segment(ns="S", degrees=0, ew="E", distance_ft=100.0),
            Segment(ns="S", degrees=90, ew="W", distance_ft=100.0),
            Segment(ns="N", degrees=0, ew="W", distance_ft=100.0),
        ],
    )
    builder = AcadBuilder(app.config_data)
    main = builder.plan(desc)
    nb = translate_plan(builder.plan(desc, is_neighbor=True), 70.0, 20.0)
    app._preview_main_plan = main
    app._preview_neighbor_plans = [nb]
    original = list(nb.vertices)

    assert app.slide_overlaps_button is not None and app.slide_undo_button is not None
    app._slide_overlapping_neighbours()
    assert nb.vertices != original
    area, shared = _after(main.vertices, original, type("R", (), {"dx": nb.vertices[0][0] - original[0][0], "dy": nb.vertices[0][1] - original[0][1]})())
    assert area <= 0.5 and shared >= 10.0 - 1e-6

    app._undo_slide_neighbours()
    assert all(
        a == pytest.approx(b) for a, b in zip(nb.vertices, original)
    ) and len(nb.vertices) == len(original)


def test_not_found_names_the_blocking_reasons():
    """В журнале видно, что именно отсеяло сдвиги: улица, другой сосед, направление."""
    main = _sq(0, 0, 100, 100)
    nb = _sq(90, 40, 30, 30)
    res = slide_off_overlap(main, nb, street_lines=[((110, -50), (110, 150))])
    assert res.status == "not_found"
    assert res.tried > 0
    assert res.rejected["улица"] > 0
    assert "улица" in res.note and "проверено сдвигов" in res.note


def test_tie_line_is_not_a_street_blocker(gui):
    """
    Линия «-tie» (от угла до точки начала) — не улица: сосед через неё проходит.
    Та же линия, но с видом улицы, — блокирует.
    """
    from acad_builder import AcadBuilder, LineItem, translate_plan
    from llm_parser import LegalDescription, Segment

    app = gui.SurveyApp()
    desc = LegalDescription(
        main_street="Oak Ave",
        cross_street="1st St",
        distance_to_corner_ft=50.0,
        segments=[
            Segment(ns="N", degrees=90, ew="E", distance_ft=100.0),
            Segment(ns="S", degrees=0, ew="E", distance_ft=100.0),
            Segment(ns="S", degrees=90, ew="W", distance_ft=100.0),
            Segment(ns="N", degrees=0, ew="W", distance_ft=100.0),
        ],
    )
    builder = AcadBuilder(app.config_data)
    main = builder.plan(desc)
    nb = translate_plan(builder.plan(desc, is_neighbor=True), 70.0, 20.0)
    # Линия поперёк пути соседа (он поедет на восток через x = 160..240)
    cut = LineItem(start=(160.0, -60.0), end=(240.0, -60.0), kind="main-street-row-tie")

    main.lines = [cut]
    app._preview_main_plan = main
    app._preview_neighbor_plans = [nb]
    app._slide_overlapping_neighbours()
    assert nb.vertices[0][0] > 120.0 + 1e-6  # сдвинулся на восток, через линию привязки

    nb2 = translate_plan(builder.plan(desc, is_neighbor=True), 70.0, 20.0)
    main.lines = [LineItem(start=(160.0, -60.0), end=(240.0, -60.0), kind="main-street-row-1")]
    app._preview_neighbor_plans = [nb2]
    app._slide_overlapping_neighbours()
    assert nb2.vertices[0][0] < 160.0  # улица не пустила соседа через линию


def _frontage_neighbour(theta_deg: float):
    """Сосед 25 ft по фасаду, длиной 110, заходит на нашу верхнюю сторону; фасад повёрнут на theta."""
    base = Polygon([(20, 85), (45, 85), (45, 195), (20, 195)])
    return list(affinity.rotate(base, theta_deg, origin=(20, 85)).exterior.coords)[:-1]


@pytest.mark.parametrize("theta_deg", [0.56, 1.35])
def test_frontage_with_document_angle_mismatch_slides_out_to_the_line(theta_deg):
    """
    Как в превью: стороны расходятся на 0.56° и 1.35° (расхождение в документах). Сосед уходит
    наружу (вверх, через нашу сторону) до касания ближайшей точкой; клин остаётся в полосе допуска.
    """
    main = _sq(0, 0, 100, 100)
    nb = _frontage_neighbour(theta_deg)
    res = slide_off_overlap(main, nb)
    assert res.status == "moved", res.note
    assert res.direction_deg == pytest.approx(90.0, abs=3.0)
    wedge = 25.0 * math.sin(math.radians(theta_deg))  # клин на фасаде 25 ft
    area, shared = _after(main, nb, res, band=0.05 + wedge)
    assert area <= 0.5
    assert shared >= 11.0 - 1e-6  # 10% большей стороны (110 ft)


def test_large_angle_still_needs_a_turn():
    main = _sq(0, 0, 100, 100)
    nb = _frontage_neighbour(5.0)
    res = slide_off_overlap(main, nb)
    assert res.status == "not_found"
    assert "поворот" in res.note
