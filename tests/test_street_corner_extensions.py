"""Street continuations stop at the intersection; lot geometry stays intact."""
import math
from pathlib import Path

import pytest

from acad_builder import AcadBuilder, LineItem, make_print_draft, polygon_signed_area
from llm_parser import load_config, parse_with_regex

DATA = Path(__file__).parent / "data"


def plan_for(name, *, extensions=True):
    description, _ = parse_with_regex((DATA / name).read_text(encoding="utf-8"), auto_repair_typos=False)
    cfg = load_config()
    cfg["drawing"]["draw_street_extensions"] = extensions
    return AcadBuilder(cfg).plan(description, scale=16)


@pytest.mark.parametrize("side_idx", [1, 3])
@pytest.mark.parametrize("clockwise", [False, True])
@pytest.mark.parametrize("angle", [0.0, 0.7])
def test_corner_only_extends_away_from_common_vertex(side_idx, clockwise, angle):
    description = plan_for("lot51_commencing_corner.txt").description.model_copy(update={
        "front_segment_index": 0, "cross_street_side_index": side_idx,
    })
    points = [(0., 0.), (12., 0.), (12., 9.), (0., 9.)]
    if clockwise:
        points = [(x, -y) for x, y in points]
    points = [(x * math.cos(angle) - y * math.sin(angle),
               x * math.sin(angle) + y * math.cos(angle)) for x, y in points]
    builder = AcadBuilder(load_config())
    warnings = []
    _, lines = builder._street_items(description, points, 1., polygon_signed_area(points) > 0,
                                     warnings, front_index=0)
    common = points[1] if side_idx == 1 else points[0]
    expected = ({"main-street-row-1", "cross-street-row-2"} if side_idx == 1 else
                {"main-street-row-2", "cross-street-row-1"})
    assert {line.kind for line in lines} == expected
    assert all(math.dist(common, endpoint) > 1e-6 for line in lines for endpoint in (line.start, line.end))
    assert not any("не соседние" in warning for warning in warnings)


def test_corner_extensions_use_actual_boundaries_without_bridging_gap():
    description = plan_for("lot51_commencing_corner.txt").description.model_copy(update={
        "front_segment_index": 0, "cross_street_side_index": 3,
    })
    points = [(0., 0.), (12., 0.), (12., 9.), (0., 9.)]
    # Hypothetical unresolved gap at the corner: never extend or snap it closed.
    boundaries = [LineItem(points[0], points[1], "boundary"),
                  LineItem(points[1], points[2], "boundary"),
                  LineItem(points[2], points[3], "boundary"),
                  LineItem((0., 9.2), (0., 0.2), "boundary")]
    _, lines = AcadBuilder(load_config())._street_items(
        description, points, 1., True, [], front_index=0, boundary_lines=boundaries,
    )
    cross = next(line for line in lines if line.kind == "cross-street-row-1")
    assert cross.end == pytest.approx((0., 9.2))
    assert all(math.dist(endpoint, (0., 0.)) > 1e-6 for line in lines for endpoint in (line.start, line.end))
    assert boundaries[3].end == (0., 0.2)


def test_corner_plan_and_draft_keep_boundary_and_far_extensions():
    plan = plan_for("lot51_commencing_corner.txt")
    without_ext = plan_for("lot51_commencing_corner.txt", extensions=False)
    assert plan.boundary_lines == without_ext.boundary_lines
    assert len(plan.street_boundary_indices) == 2
    assert len(plan.lines) == 2
    draft = make_print_draft(plan, {})
    assert len(draft.lines) == 2
    assert [(line.start, line.end) for line in draft.lines] == [
        ((line.start[0] + 1000, line.start[1]), (line.end[0] + 1000, line.end[1]))
        for line in plan.lines
    ]
    assert all(math.dist(a.start, b.start) == pytest.approx(1000) and
               math.dist(a.end, b.end) == pytest.approx(1000)
               for a, b in zip(plan.boundary_lines, draft.boundary_lines))


def test_regular_lot_cross_street_starts_at_corner_and_points_to_block():
    plan = plan_for("lot72_render_e600.txt")
    no_ext = plan_for("lot72_render_e600.txt", extensions=False)
    assert plan.boundary_lines == no_ext.boundary_lines
    tie = next(line for line in plan.lines if line.kind == "main-street-row-tie")
    cross = next(line for line in plan.lines if line.kind == "cross-street-row")
    assert cross.start == pytest.approx(tie.start)
    vx, vy = cross.end[0] - cross.start[0], cross.end[1] - cross.start[1]
    tx, ty = tie.end[0] - tie.start[0], tie.end[1] - tie.start[1]
    assert vx * tx + vy * ty == pytest.approx(0.0, abs=1e-8)
    assert vx * (plan.centroid[0] - cross.start[0]) + vy * (plan.centroid[1] - cross.start[1]) > 0
    draft = make_print_draft(plan, {})
    draft_tie = next(line for line in draft.lines if line.kind == "main-street-row-tie")
    draft_cross = next(line for line in draft.lines if line.kind == "cross-street-row")
    assert draft_cross.start == pytest.approx(draft_tie.start)
    assert math.dist(draft_cross.start, draft_cross.end) == pytest.approx(100)


def test_no_cross_street_keeps_both_single_street_extensions():
    plan = plan_for("lot72_render_e600.txt")
    desc = plan.description.model_copy(update={"cross_street": None})
    rebuilt = AcadBuilder(load_config()).plan(desc, scale=16)
    assert {line.kind for line in rebuilt.lines} == {"main-street-row-1", "main-street-row-2"}
    assert rebuilt.boundary_lines == plan.boundary_lines


def test_non_adjacent_reported_sides_are_not_forcibly_trimmed():
    plan = plan_for("lot51_commencing_corner.txt")
    desc = plan.description.model_copy(update={"front_segment_index": 0, "cross_street_side_index": 2})
    warnings = []
    _, lines = AcadBuilder(load_config())._street_items(desc, plan.vertices, 1, plan.ccw, warnings, front_index=0)
    assert len(lines) == 4
    assert any("не соседние" in warning for warning in warnings)
