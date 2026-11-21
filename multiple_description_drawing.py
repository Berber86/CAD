"""Experimental multi-description scene and print-draft planning.

The GUI uses these helpers only behind the disabled-by-default developer flag.
No COM calls occur here; open_traverse prevents silently closing partial sides.
"""
from __future__ import annotations

from collections import OrderedDict
import copy
from dataclasses import dataclass, replace
import math
from pathlib import Path
from typing import Any, Sequence

import lot_identity
from acad_builder import (
    AcadBuilder, DrawPlan, LineItem, TextItem, build_polygon_points,
    build_side_lines, closure_error, description_uses_feet_inches, estimate_text_width, polygon_signed_area, text_rotation,
)
from multiple_description_parse import DescriptionItem


def plan_partial(
    builder: AcadBuilder,
    item: DescriptionItem,
    *,
    source: str | Path,
    is_main: bool,
    scale: float = 16.0,
    start: tuple[float, float] = (0.0, 0.0),
) -> DrawPlan:
    """Plan only known sides; caller must place it without implying adjacency.

    The caller supplies a start position: the legacy (0, 0) inference from an
    absent street tie is not evidence of location. A lot number is from the
    filename first, otherwise from this description. No number is made up.
    """
    if item.complete:
        raise ValueError('Use the complete-description planner for complete contours')
    if not item.data.segments:
        raise ValueError('No trustworthy sides; neither contour nor label can be drawn')
    if (len(start) != 2 or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                   and math.isfinite(v) for v in start)):
        raise ValueError('Invalid start point')
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError('Invalid scale')
    description = lot_identity.bind_filename(item.data, source)
    if any(not all(math.isfinite(value) for value in segment.vector) for segment in description.segments):
        raise ValueError('A side has non-finite geometry')
    points = build_polygon_points(description.segments, start=start)
    boundary = [LineItem(start=a, end=b, kind='boundary') for a, b in zip(points, points[1:])]
    height = builder.text_height_for_scale(scale, builder.cfg)
    drawing = builder.drawing_cfg
    texts: list[TextItem] = []
    # Partial open chains have no reliable polygon interior or exterior. Use
    # a consistent side of each known edge without generating missing edges.
    if is_main:
        for index, (segment, edge) in enumerate(zip(description.segments, boundary), 1):
            dx, dy = edge.end[0] - edge.start[0], edge.end[1] - edge.start[1]
            length = math.hypot(dx, dy)
            if length <= 1e-9:
                continue
            nx, ny = dy / length, -dx / length
            midpoint = ((edge.start[0] + edge.end[0]) / 2, (edge.start[1] + edge.end[1]) / 2)
            angle = text_rotation(math.atan2(dy, dx))
            rows, _ = build_side_lines(segment, height, length, drawing,
                                       feet_inches=description_uses_feet_inches(description))
            for row, text in enumerate(rows):
                offset = height * (1.8 + row * 1.9)
                texts.append(TextItem(text=text, point=(midpoint[0] + nx * offset,
                                                       midpoint[1] + ny * offset),
                                      rotation=angle, kind=f'side-{index}' if row == 0 else f'side-{index}-note'))
    xs, ys = zip(*points)
    center = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
    label = f"LOT {lot_identity.effective_number(description) or '?'}"
    texts.append(TextItem(text=label, point=center, rotation=0.0,
                          kind='main-lot-label' if is_main else 'neighbor-label'))
    # Preserve an explicitly available frontage, but do not guess one from an
    # absent value: an open chain does not define a polygon's outside.
    front = description.front_segment_index
    street_indices = ((front,) if is_main and front is not None and 0 <= front < len(boundary) else ())
    return DrawPlan(description=description, scale=scale, text_height=height, vertices=points,
                    closure=closure_error(points), items=texts, boundary_lines=boundary,
                    street_boundary_indices=street_indices, pob=points[0],
                    is_neighbor=not is_main, open_traverse=True)


# A placement must be established by the caller; matching a filename or a
# missing tie is not sufficient evidence that two descriptions are neighbours.
@dataclass(frozen=True)
class Anchor:
    """A confirmed common coordinate frame and this description's POB in it."""
    group: str
    pob: tuple[float, float]


@dataclass(frozen=True)
class DescriptionSource:
    source: Path
    is_main: bool
    item: DescriptionItem
    anchor: Anchor | None = None
    display_main: str | None = None
    display_cross: str | None = None


@dataclass(frozen=True)
class PlannedDescription:
    source: Path
    is_main: bool
    description_index: int
    plan: DrawPlan
    group: str | None
    placement_known: bool
    diagnostic: str | None = None


def _shape_matches(plan: DrawPlan, item: DescriptionItem) -> bool:
    """Reject modified vectors or a break moved away from the end of the chain.

    The legacy planner can relocate an unclosed gap into an interior corner
    while preserving every vector. That moves later sides relative to POB and
    cannot establish their original document positions in a shared scene.
    """
    if len(plan.boundary_lines) != len(item.data.segments):
        return False
    expected_start = plan.pob
    for segment, edge in zip(item.data.segments, plan.boundary_lines):
        start_error = math.dist(expected_start, edge.start)
        if not math.isfinite(start_error) or start_error > 1e-6:
            return False
        dx, dy = segment.vector
        error = math.hypot(edge.end[0] - edge.start[0] - dx,
                           edge.end[1] - edge.start[1] - dy)
        if not math.isfinite(error) or error > 1e-6:
            return False
        expected_start = edge.end
    return True


def _plan_description(builder: AcadBuilder, record: DescriptionSource, scale: float) -> tuple[DrawPlan, str | None]:
    start = record.anchor.pob if record.anchor is not None else (0.0, 0.0)
    if not record.item.complete:
        return plan_partial(builder, record.item, source=record.source, is_main=record.is_main,
                            scale=scale, start=start), 'partial_description'
    desc = lot_identity.bind_filename(record.item.data, record.source)
    # Ensure the old drawing code cannot create a closing side. If its
    # analytical refinement changes any original side, use the exact open
    # representation instead of silently changing the legal description.
    cfg = copy.deepcopy(builder.cfg)
    (cfg['drawing'] if isinstance(cfg.get('drawing'), dict) else cfg)['leave_gap_open'] = True
    safe_builder = AcadBuilder(cfg)
    try:
        plan = safe_builder.plan(desc, scale=scale, is_neighbor=not record.is_main)
        if _shape_matches(plan, record.item):
            from acad_builder import translate_plan
            plan = translate_plan(plan, start[0] - plan.pob[0], start[1] - plan.pob[1])
            # Filename/description conflicts are preserved by the separate
            # metadata store, not turned into user-facing warnings here.
            plan.warnings = [w for w in plan.warnings if 'номер лота' not in w.lower()]
            return plan, None
    except Exception:
        pass
    exact = plan_partial(builder, DescriptionItem(record.item.data, False), source=record.source,
                         is_main=record.is_main, scale=scale, start=start)
    return exact, 'exact_sides_fallback'


def _bounds(plans: Sequence[DrawPlan]) -> tuple[float, float, float, float]:
    points = [point for plan in plans for edge in (*plan.boundary_lines, *plan.lines)
              for point in (edge.start, edge.end)]
    if not points:
        points = [point for plan in plans for point in plan.vertices]
    if not points:
        raise ValueError('No drawable points')
    # Account for text extents and arcs as well as line geometry when choosing
    # separate locations. This is conservative, not a real AutoCAD font metric.
    for plan in plans:
        for text in plan.items:
            height = float(text.height or plan.text_height)
            width = estimate_text_width(text.text, height, {})
            cosine, sine = abs(math.cos(text.rotation)), abs(math.sin(text.rotation))
            half_x = (width * cosine + height * sine) / 2.0
            half_y = (width * sine + height * cosine) / 2.0
            points.extend(((text.point[0] - half_x, text.point[1] - half_y),
                           (text.point[0] + half_x, text.point[1] + half_y)))
        for arc in plan.arcs:
            points.extend(((arc.center[0] - arc.radius, arc.center[1] - arc.radius),
                           (arc.center[0] + arc.radius, arc.center[1] + arc.radius)))
    xs, ys = zip(*points)
    return min(xs), min(ys), max(xs), max(ys)


def _street_family(kind: str) -> str | None:
    if kind.startswith('main-street-row-'):
        return 'main'
    if kind.startswith('cross-street-row'):
        return 'cross'
    return None


def _uncovered_street_parts(line: LineItem, previous: Sequence[LineItem]) -> list[LineItem]:
    """Subtract collinear same-street coverage without extending any line.

    Tight absolute tolerances deliberately leave questionable overlaps alone.
    Property boundaries are never inputs. A row tie remains intact because the
    common draft uses its original endpoints to establish the intersection.
    """
    if line.kind == 'main-street-row-tie':
        return [line]
    dx, dy = line.end[0] - line.start[0], line.end[1] - line.start[1]
    length = math.hypot(dx, dy)
    family = _street_family(line.kind)
    if family is None or length <= 1e-9:
        return [line]
    ux, uy = dx / length, dy / length
    intervals = [(0.0, length)]
    for other in previous:
        if _street_family(other.kind) != family:
            continue
        rel = [(p[0] - line.start[0], p[1] - line.start[1]) for p in (other.start, other.end)]
        if any(abs(x * uy - y * ux) > 1e-8 for x, y in rel):
            continue
        lo, hi = sorted(x * ux + y * uy for x, y in rel)
        new = []
        for a, b in intervals:
            if hi <= a or lo >= b:
                new.append((a, b))
            else:
                if lo > a + 1e-9:
                    new.append((a, min(lo, b)))
                if hi < b - 1e-9:
                    new.append((max(hi, a), b))
        intervals = new
    def point(t: float) -> tuple[float, float]:
        if t == 0.0:
            return line.start
        if t == length:
            return line.end
        return (line.start[0] + ux * t, line.start[1] + uy * t)
    return [replace(line, start=point(a), end=point(b)) for a, b in intervals if b - a > 1e-9]


def _remove_exact_shared_streets(plans: list[DrawPlan]) -> list[DrawPlan]:
    """Deduplicate safe auxiliary street coverage in one confirmed group.

    Never modify property boundaries, dimensions or tie endpoints. Identical or
    partially overlapping street extensions are clipped only when collinear and
    assigned to the same main/cross street family.
    """
    previous_lines: list[LineItem] = []
    seen_texts: dict[tuple[str, str], tuple[int, int]] = {}
    output: list[DrawPlan] = []
    placed_street_labels: list[TextItem] = []
    for plan in plans:
        lines = []
        for line in plan.lines:
            parts = _uncovered_street_parts(line, previous_lines)
            lines.extend(parts)
            previous_lines.extend(parts)
        texts = []
        for text in plan.items:
            if text.kind in {'main-street', 'cross-street', 'main-street-former', 'cross-street-former',
                             'main-street-note', 'main-street-note-extra'}:
                key = (text.kind, text.text)
                if key in seen_texts:
                    plan_index, text_index = seen_texts[key]
                    previous = texts[text_index] if plan_index == len(output) else output[plan_index].items[text_index]
                    deleted = previous.correction_deleted
                    if text.correction_deleted and text.correction_deleted != deleted:
                        deleted = '/'.join(filter(None, (deleted, text.correction_deleted)))
                    merged = replace(previous, correction_indices=tuple(sorted(set(
                        previous.correction_indices + text.correction_indices))), correction_deleted=deleted)
                    if plan_index == len(output):
                        texts[text_index] = merged
                    else:
                        output[plan_index].items[text_index] = merged
                    continue
                if text.kind in {'main-street', 'cross-street'}:
                    # Different corrected spellings may share the same corner.
                    # Offset the later label along its normal rather than paint
                    # two strings on top of each other; no geometry is changed.
                    h = float(text.height or plan.text_height)
                    ux, uy = math.cos(text.rotation), math.sin(text.rotation)
                    nx, ny = -uy, ux
                    for _ in range(len(placed_street_labels) + 1):
                        conflicts = []
                        for other in placed_street_labels:
                            if other.kind != text.kind:
                                continue
                            dx, dy = text.point[0] - other.point[0], text.point[1] - other.point[1]
                            other_h = float(other.height or plan.text_height)
                            half_width = (estimate_text_width(text.text, h, {}) +
                                          estimate_text_width(other.text, other_h, {})) / 2
                            if abs(dx * ux + dy * uy) < half_width and abs(dx * nx + dy * ny) < (h + other_h):
                                conflicts.append(other_h)
                        if not conflicts:
                            break
                        step = h + max(conflicts)
                        text = replace(text, point=(text.point[0] + nx * step, text.point[1] + ny * step))
                    placed_street_labels.append(text)
                seen_texts[key] = (len(output), len(texts))
            texts.append(text)
        output.append(replace(plan, lines=lines, items=texts))
    return output


@dataclass(frozen=True)
class PlanFailure:
    source: Path
    description_index: int
    reason: str


@dataclass(frozen=True)
class SceneResult:
    plans: tuple[PlannedDescription, ...]
    failures: tuple[PlanFailure, ...]


# The existing application treats gaps up to 0.015 ft as rounding. A near-
# closed traverse still stays open and retains every original vector.
ROUNDING_GAP_LIMIT_FT = 0.015


def _near_closed_complete(plan: DrawPlan, item: DescriptionItem) -> bool:
    edges = plan.boundary_lines
    if not item.complete or len(edges) < 3:
        return False
    gap = math.dist(edges[-1].end, edges[0].start)
    if not math.isfinite(gap) or gap > ROUNDING_GAP_LIMIT_FT:
        return False
    try:
        from shapely.geometry import Polygon
        polygon = Polygon([edge.start for edge in edges])
        return polygon.is_valid and polygon.area >= 0.5
    except Exception:
        return False


def _decorate_exact_unlocated(builder: AcadBuilder, plan: DrawPlan, item: DescriptionItem) -> DrawPlan:
    """Use ordinary side/angle layout on a near-closed exact open traverse.

    The boundary is unchanged; the small rounding gap remains at POB. No row
    extensions or intersection are constructed. Do not treat partial chains or
    substantial gaps as polygons for annotation.
    """
    if not _near_closed_complete(plan, item) or not plan.open_traverse:
        return plan
    points = [edge.start for edge in plan.boundary_lines]
    area = polygon_signed_area(points)
    if not math.isfinite(area) or abs(area) < 0.5:
        return plan
    desc = plan.description
    front = desc.front_segment_index
    if isinstance(front, bool) or not isinstance(front, int) or not 0 <= front < len(points):
        front = None
    warnings = list(plan.warnings)
    try:
        if plan.is_neighbor:
            label = builder._neighbor_label_item(desc, points, plan.text_height, front)
            items, arcs = [label], []
        else:
            items, arcs, _ = builder._side_items(desc, points, plan.text_height, area > 0,
                                                 warnings, front_index=front,
                                                 boundary_lines=plan.boundary_lines)
            label = builder._neighbor_label_item(desc, points, plan.text_height, front)
            items.append(replace(label, kind='main-lot-label'))
    except Exception:
        return plan
    return replace(plan, vertices=points, items=items, arcs=arcs, warnings=warnings, ccw=area > 0)


def _decorate_exact_anchored(builder: AcadBuilder, plan: DrawPlan, source: DescriptionSource) -> DrawPlan:
    """Recover full annotations on a confirmed tie and exact near-closed sides."""
    from acad_builder import compute_pob_point
    desc = plan.description
    if (not plan.open_traverse or not _near_closed_complete(plan, source.item)
            or source.anchor is None or not source.anchor.group.startswith(('confirmed-file:', 'auto-row:'))
            or desc.corner_to_pob_bearing is None
            or math.dist(compute_pob_point(desc), source.anchor.pob) > 1e-5):
        return plan
    plan = _decorate_exact_unlocated(builder, plan, source.item)
    if plan.is_neighbor:
        front = desc.front_segment_index
        if (isinstance(front, int) and not isinstance(front, bool)
                and 0 <= front < len(plan.boundary_lines)
                and (desc.main_street_corrected or desc.cross_street_corrected)):
            points = [edge.start for edge in plan.boundary_lines]
            try:
                street_items, _ = builder._street_items(desc, points, plan.text_height, plan.ccw, [],
                                                         front_index=front, boundary_lines=plan.boundary_lines)
                corrected = [t for t in street_items if t.correction_indices or t.correction_deleted]
                return replace(plan, items=plan.items + corrected)
            except Exception:
                pass
        return plan
    front = desc.front_segment_index
    if not isinstance(front, int) or isinstance(front, bool) or not 0 <= front < len(plan.boundary_lines):
        return plan
    points = [edge.start for edge in plan.boundary_lines]
    try:
        _, _, bands = builder._side_items(desc, points, plan.text_height, plan.ccw, [],
                                          front_index=front, boundary_lines=plan.boundary_lines)
        items, lines = builder._street_items(desc, points, plan.text_height, plan.ccw, [],
                                              side_band_extra=bands, front_index=front,
                                              boundary_lines=plan.boundary_lines)
    except Exception:
        return plan
    ties = [line for line in lines if line.kind == 'main-street-row-tie']
    if (len(ties) != 1 or math.dist(ties[0].start, (0, 0)) > ROUNDING_GAP_LIMIT_FT
            or math.dist(ties[0].end, plan.pob) > ROUNDING_GAP_LIMIT_FT):
        return plan
    # POB is the end of the legal tie. The adjacent measured frontage endpoint
    # may be separated from it by a rounding gap; do not change the boundary.
    lines = [replace(line, start=(0.0, 0.0), end=plan.pob)
             if line.kind == 'main-street-row-tie' else line for line in lines]
    return replace(plan, items=plan.items + items, lines=lines)


def _unlocated_frontage_items(builder: AcadBuilder, plan: DrawPlan, item: DescriptionItem) -> list[TextItem]:
    """Label a documented frontage without inventing a street intersection.

    A trustworthy exterior requires a complete near-closed traverse and an explicit
    frontage index. Never use the legacy builder's configured/guessed index.
    Discard all cross-street items and all extension lines: their position may
    depend on the missing intersection tie.
    """
    desc = plan.description
    front = desc.front_segment_index
    edges = plan.boundary_lines
    if (not item.complete or not str(desc.main_street or '').strip()
            or isinstance(front, bool) or not isinstance(front, int)
            or not 0 <= front < len(edges) or not _near_closed_complete(plan, item)):
        return []
    points = [edge.start for edge in edges]
    area = polygon_signed_area(points)
    if abs(area) < 0.5:
        return []
    # The legacy layout handles text sizing, rotation and street notes. Remove
    # the cross street from its input to avoid even computing a guessed corner.
    local_desc = desc.model_copy(update={'cross_street': None, 'cross_street_former': None})
    try:
        _, _, bands = builder._side_items(local_desc, points, plan.text_height, area > 0,
                                          [], front_index=front, boundary_lines=edges)
        items, _ = builder._street_items(local_desc, points, plan.text_height, area > 0,
                                        [], side_band_extra=bands, front_index=front, boundary_lines=edges)
    except Exception:
        # A label/layout failure must not hide an otherwise usable contour.
        return []
    return [text for text in items if text.kind in {'main-street', 'main-street-note', 'main-street-note-extra'}]


def plan_scene(builder: AcadBuilder, sources: Sequence[DescriptionSource], *, scale: float = 16.0,
               separation_ft: float = 200.0) -> SceneResult:
    """Plan all records; unrelated groups are laid out separately to the right.

    Each explicit anchor group retains its mutual coordinates. Unanchored
    records each form a separate group, even if their shapes or filenames
    match. Every result is returned separately, including genuine duplicates.
    """
    from acad_builder import translate_plan
    if not math.isfinite(separation_ft) or separation_ft <= 0:
        raise ValueError('separation_ft must be positive')
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError('scale must be positive')
    prepared: list[PlannedDescription] = []
    failures: list[PlanFailure] = []
    for index, source in enumerate(sources):
        anchor = source.anchor
        if anchor is not None:
            if (not isinstance(anchor.group, str) or not anchor.group.strip() or len(anchor.pob) != 2
                    or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                               for v in anchor.pob)):
                raise ValueError('Invalid placement anchor')
        try:
            plan, reason = _plan_description(builder, source, scale)
        except Exception:  # A damaged description must not hide other results.
            failures.append(PlanFailure(Path(source.source), index, 'planning_failed'))
            continue
        if anchor is not None:
            plan = _decorate_exact_anchored(builder, plan, source)
        if anchor is None:
            # The legacy builder derives a hypothetical intersection even when
            # its input lacks an established tie. An isolated parcel must not
            # carry that invented street geometry or its associated labels.
            plan = _decorate_exact_unlocated(builder, plan, source.item)
            frontage = _unlocated_frontage_items(builder, plan, source.item) if (
                source.is_main or source.item.data.main_street_corrected) else []
            if not source.is_main:
                frontage = [t for t in frontage if t.correction_indices or t.correction_deleted]
            plan = replace(plan, lines=[], items=[t for t in plan.items
                            if 'street' not in t.kind and t.kind != 'cross-street-distance'] + frontage)
        # Shared uncorrected labels are scene presentation only; the parsed
        # original and any AI corrections remain attached to their own source.
        overrides = {'main-street': source.display_main, 'cross-street': source.display_cross}
        import street_names
        plan = replace(plan, items=[
            replace(t, text=street_names.street_text(overrides[t.kind]))
            if t.kind in overrides and overrides[t.kind] is not None else t
            for t in plan.items])
        prepared.append(PlannedDescription(Path(source.source), source.is_main, index, plan,
                                           anchor.group if anchor else None, anchor is not None,
                                           reason if anchor is not None else (reason or 'unresolved_position')))
    groups: OrderedDict[tuple[str, Any], list[PlannedDescription]] = OrderedDict()
    # Explicitly located groups go first, so unlocated parts cannot determine
    # the shared origin of the primary scene.
    for entry in sorted(prepared, key=lambda entry: not entry.placement_known):
        key = ('known', entry.group) if entry.placement_known else ('unknown', entry.description_index)
        groups.setdefault(key, []).append(entry)
    result: list[PlannedDescription] = []
    right: float | None = None
    for key, group in groups.items():
        plans = [entry.plan for entry in group]
        if key[0] == 'known':
            plans = _remove_exact_shared_streets(plans)
        left, _, max_x, _ = _bounds(plans)
        dx = 0.0 if right is None else right + separation_ft - left
        for entry, plan in zip(group, plans):
            result.append(replace(entry, plan=translate_plan(plan, dx, 0.0)))
        right = max_x + dx
    return SceneResult(tuple(sorted(result, key=lambda entry: entry.description_index)), tuple(failures))


@dataclass(frozen=True)
class DraftSceneResult:
    plans: tuple[DrawPlan, ...]
    diagnostic: str | None


def draft_row_group(scene: SceneResult, *, builder_cfg: dict[str, Any]) -> DraftSceneResult:
    """Prepare one common draft for a confirmed, single-intersection row group.

    This limited helper is intentionally fail-closed: mixed/unknown groups,
    incomplete contours, corners and incompatible ties are not approximated.
    The GUI uses it through the multi-group print workflow under the flag.
    """
    from acad_builder import make_print_draft
    ours = [entry for entry in scene.plans if entry.is_main]
    if scene.failures or len(ours) < 2:
        return DraftSceneResult((), 'unsupported_scene')
    groups = {entry.group for entry in ours}
    if (len(groups) != 1 or None in groups or any(
            not e.placement_known or (e.plan.open_traverse and not (
                e.diagnostic == 'exact_sides_fallback' and len(e.plan.boundary_lines) >= 3
                and e.plan.closure.distance <= ROUNDING_GAP_LIMIT_FT)) for e in ours)):
        return DraftSceneResult((), 'unsupported_scene')
    import street_names
    first_desc = ours[0].plan.description
    first_streets = (first_desc.main_street_corrected or first_desc.main_street,
                     first_desc.cross_street_corrected or first_desc.cross_street)
    if not all(first_streets) or any(
            not street_names.scene_street_match(first_streets[0], e.plan.description.main_street_corrected or e.plan.description.main_street)
            or not street_names.scene_street_match(first_streets[1], e.plan.description.cross_street_corrected or e.plan.description.cross_street)
            for e in ours[1:]):
        return DraftSceneResult((), 'inconsistent_streets')
    if any(e.plan.description.cross_street_side_index is not None
           or len(e.plan.street_boundary_indices) > 1
           or e.plan.description.distance_to_corner_ft <= 1e-6 for e in ours):
        return DraftSceneResult((), 'corner_or_missing_tie')
    ties: list[LineItem] = []
    for entry in ours:
        tie = next((line for line in entry.plan.lines if line.kind == 'main-street-row-tie'), None)
        if tie is None or math.dist(tie.start, tie.end) < 1e-6:
            return DraftSceneResult((), 'missing_tie')
        # The approved 40 feet is from the nearest beginning point, not an
        # arbitrary point on a frontage that differs from that beginning.
        if math.dist(tie.end, entry.plan.pob) > 1e-5:
            return DraftSceneResult((), 'tie_does_not_end_at_pob')
        ties.append(tie)
    origin = ties[0].start
    if any(math.dist(tie.start, origin) > 1e-5 for tie in ties):
        return DraftSceneResult((), 'different_intersections')
    # A single street ray must support all ties. Different rays/crossings
    # cannot safely share a conditional intersection without more information.
    ax, ay = ties[0].end[0] - origin[0], ties[0].end[1] - origin[1]
    a_len = math.hypot(ax, ay)
    ux, uy = ax / a_len, ay / a_len
    for tie in ties:
        dx, dy = tie.end[0] - origin[0], tie.end[1] - origin[1]
        if dx * ux + dy * uy <= 0 or abs(dx * uy - dy * ux) > 1e-5:
            return DraftSceneResult((), 'incompatible_ties')
    nearest = min(range(len(ours)), key=lambda i: math.dist(ties[i].start, ties[i].end))
    try:
        drafts = [make_print_draft(entry.plan, {}, builder_cfg=builder_cfg) for entry in ours]
    except Exception:
        return DraftSceneResult((), 'draft_planning_failed')
    shared_tie = next((line for line in drafts[nearest].lines if line.kind == 'main-street-row-tie'), None)
    if (shared_tie is None or abs(math.dist(shared_tie.start, shared_tie.end) - 40.0) > 1e-5
            or math.dist(shared_tie.end, drafts[nearest].pob) > 1e-5):
        return DraftSceneResult((), 'draft_tie_unavailable')
    # Keep one set of shared street elements from the nearest lot. Each other
    # parcel retains its full boundary and dimension labels, but not another
    # independently shifted intersection or another copy of its streets.
    output: list[DrawPlan] = []
    shared_corner = shared_tie.start
    for index, draft in enumerate(drafts):
        if index == nearest:
            output.append(draft)
            continue
        label_items: list[TextItem] = []
        for text in draft.items:
            if (text.kind.startswith('side-') or text.kind.startswith('party-wall-')
                    or text.kind.startswith('angle-')):
                label_items.append(text)
            elif text.kind == 'cross-street-distance':
                end = (ties[index].end[0] + 1000.0, ties[index].end[1])
                vx, vy = end[0] - shared_corner[0], end[1] - shared_corner[1]
                length = math.hypot(vx, vy)
                nx, ny = (-vy / length, vx / length) if length > 1e-9 else (0.0, 0.0)
                cx, cy = draft.centroid
                if (cx - shared_corner[0]) * nx + (cy - shared_corner[1]) * ny < 0:
                    nx, ny = -nx, -ny
                label_items.append(replace(text, point=((shared_corner[0] + end[0]) / 2 + nx * 3.8,
                                                        (shared_corner[1] + end[1]) / 2 + ny * 3.8)))
        output.append(replace(draft, items=label_items, lines=[]))
    return DraftSceneResult(tuple(output), None)


def make_scene_print_drafts(scene: SceneResult, *, builder_cfg: dict[str, Any]) -> DraftSceneResult:
    """Build one composite draft from all our plans, never from neighbours.

    Known row groups use the 40-foot helper when it is applicable. Corner,
    unlocated or incomplete groups keep their physical geometry, do not invent
    an intersection, and retain a developer-only reason when a row group could
    not be resolved. Every part is shifted by the same 1000 feet.
    """
    from acad_builder import make_print_draft, translate_plan
    import re
    ours = [entry for entry in scene.plans if entry.is_main]
    if not ours:
        return DraftSceneResult((), None)
    groups: OrderedDict[tuple[str, Any], list[PlannedDescription]] = OrderedDict()
    for entry in ours:
        key = ('known', entry.group) if entry.placement_known else ('unknown', entry.description_index)
        groups.setdefault(key, []).append(entry)
    by_index: dict[int, DrawPlan] = {}
    notes: list[str] = []
    for key, entries in groups.items():
        if key[0] == 'known' and len(entries) >= 2:
            group_scene = SceneResult(tuple(entries), ())
            result = draft_row_group(group_scene, builder_cfg=builder_cfg)
            if result.diagnostic is None:
                by_index.update((entry.description_index, plan) for entry, plan in zip(entries, result.plans))
                continue
            notes.append(result.diagnostic or 'draft_group_unavailable')
        if (key[0] == 'known' and len(entries) == 1
                and (not entries[0].plan.open_traverse or (
                    entries[0].diagnostic == 'exact_sides_fallback'
                    and entries[0].plan.closure.distance <= ROUNDING_GAP_LIMIT_FT
                    and any(line.kind == 'main-street-row-tie' for line in entries[0].plan.lines)))):
            try:
                draft = make_print_draft(entries[0].plan, {}, builder_cfg=builder_cfg)
                by_index[entries[0].description_index] = draft
                continue
            except Exception:
                notes.append('draft_planning_failed')
        for entry in entries:
            # When a common intersection cannot be established, translate the
            # exact known geometry; do not invent a 40-foot tie. One common
            # shift preserves each group's mutual position.
            draft = translate_plan(entry.plan, 1000.0, 0.0)
            texts = []
            for text in draft.items:
                if text.kind in {'main-lot-label', 'neighbor-label'}:
                    continue
                if key[0] == 'unknown' and not (text.kind.startswith('side-') or text.kind.startswith('party-wall-')
                                                or text.kind.startswith('angle-')
                                                or text.kind in {'main-street', 'main-street-note', 'main-street-note-extra'}):
                    continue
                if text.kind.startswith('side-'):
                    text = replace(text, text=re.sub(r' {2,}', ' ', text.text))
                texts.append(replace(text, correction_indices=(), correction_deleted=''))
            by_index[entry.description_index] = replace(draft, items=texts,
                                                        lines=[] if key[0] == 'unknown' else draft.lines,
                                                        print_draft=True)
    return DraftSceneResult(tuple(by_index[e.description_index] for e in ours),
                            ';'.join(dict.fromkeys(notes)) or None)
