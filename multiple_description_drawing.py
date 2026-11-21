"""Experimental planning of known portions of incomplete boundary traverses.

Not connected to the application's scene or print-draft workflow yet. No COM
calls occur here. AutoCAD drawing uses the explicit open_traverse flag so an
incomplete description is never silently closed, even in polyline mode.
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
    build_side_lines, closure_error, description_uses_feet_inches, estimate_text_width, text_rotation,
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
    """Reject legacy analytic closure or an artificial closing edge."""
    if len(plan.boundary_lines) != len(item.data.segments):
        return False
    for segment, edge in zip(item.data.segments, plan.boundary_lines):
        dx, dy = segment.vector
        error = math.hypot(edge.end[0] - edge.start[0] - dx,
                           edge.end[1] - edge.start[1] - dy)
        if not math.isfinite(error) or error > 1e-6:
            return False
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


def _remove_exact_shared_streets(plans: list[DrawPlan]) -> list[DrawPlan]:
    """Remove only identical auxiliary streets in an established common group.

    Property boundaries, lot labels, and dimensions are never deduplicated.
    Partially overlapping lines are left untouched rather than changed.
    """
    seen_lines: set[tuple[Any, ...]] = set()
    seen_texts: set[tuple[str, str]] = set()
    output: list[DrawPlan] = []
    for plan in plans:
        lines = []
        for line in plan.lines:
            ends = sorted(((round(line.start[0], 6), round(line.start[1], 6)),
                           (round(line.end[0], 6), round(line.end[1], 6))))
            key = (ends[0], ends[1])
            if key not in seen_lines:
                seen_lines.add(key)
                lines.append(line)
        texts = []
        for text in plan.items:
            if text.kind in {'main-street', 'cross-street', 'main-street-former', 'cross-street-former'}:
                key = (text.kind, ' '.join(text.text.upper().split()))
                if key in seen_texts:
                    continue
                seen_texts.add(key)
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
        if anchor is None:
            # The legacy builder derives a hypothetical intersection even when
            # its input lacks an established tie. An isolated parcel must not
            # carry that invented street geometry or its associated labels.
            plan = replace(plan, lines=[], items=[t for t in plan.items
                            if 'street' not in t.kind and t.kind != 'cross-street-distance'])
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
    The GUI and general multi-group print workflow are not connected yet.
    """
    from acad_builder import make_print_draft
    ours = [entry for entry in scene.plans if entry.is_main]
    if scene.failures or len(ours) < 2:
        return DraftSceneResult((), 'unsupported_scene')
    groups = {entry.group for entry in ours}
    if (len(groups) != 1 or None in groups or any(not e.placement_known or e.plan.open_traverse for e in ours)):
        return DraftSceneResult((), 'unsupported_scene')
    streets = {(' '.join(str(e.plan.description.main_street or '').upper().split()),
                ' '.join(str(e.plan.description.cross_street or '').upper().split())) for e in ours}
    if len(streets) != 1 or not all(next(iter(streets))):
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
            if text.kind.startswith('side-') or text.kind.startswith('party-wall-'):
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
        if key[0] == 'known' and len(entries) == 1 and not entries[0].plan.open_traverse:
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
                if key[0] == 'unknown' and not (text.kind.startswith('side-') or text.kind.startswith('party-wall-')):
                    continue
                if text.kind.startswith('side-'):
                    text = replace(text, text=re.sub(r' {2,}', ' ', text.text))
                texts.append(text)
            by_index[entry.description_index] = replace(draft, items=texts,
                                                        lines=[] if key[0] == 'unknown' else draft.lines,
                                                        print_draft=True)
    return DraftSceneResult(tuple(by_index[e.description_index] for e in ours),
                            ';'.join(dict.fromkeys(notes)) or None)
