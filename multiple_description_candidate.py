"""Evidence-based row matching scoped to a scene, never a street registry.

A one-letter name difference is considered only along with matching geometry,
and a unique pair. It is not evidence of an officially correct spelling.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Sequence

from shapely.geometry import Polygon
import street_names

from acad_builder import build_polygon_points, compute_pob_point
from llm_parser import _make_tie_segment_like
from multiple_description_drawing import Anchor, DescriptionSource, ROUNDING_GAP_LIMIT_FT
from multiple_description_parse import DescriptionItem

TOLERANCE_FT = 0.02


@dataclass(frozen=True)
class RowProposal:
    source: Path
    near_index: int
    far_index: int
    main_street: str
    near_cross: str
    far_cross: str
    near_distance: float
    far_distance: float
    shared_length: float
    tie_label: str


def propose_row(sources: Sequence[DescriptionSource]) -> RowProposal | None:
    """Find one simple supported pair; caller must reject ambiguous pairings."""
    if len(sources) != 2:
        return None
    a, b = sources
    if (not a.item.complete or not b.item.complete
            or (Path(a.source) == Path(b.source) and a.is_main != b.is_main)):
        return None
    descs = [a.item.data, b.item.data]
    for desc in descs:
        if (not desc.main_street or not desc.cross_street
                or desc.cross_street_side_index is not None or len(desc.segments) < 3
                or desc.front_segment_index != len(desc.segments) - 1
                or desc.distance_to_corner_ft <= 0
                or not all(s.has_bearing and not s.bearing_solved for s in desc.segments)):
            return None
        pts = build_polygon_points(desc.segments)
        if math.dist(pts[0], pts[-1]) > ROUNDING_GAP_LIMIT_FT:
            return None
    if (not street_names.scene_street_match(descs[0].main_street_corrected or descs[0].main_street,
                                            descs[1].main_street_corrected or descs[1].main_street)
            or not street_names.scene_street_match(descs[0].cross_street_corrected or descs[0].cross_street,
                                                   descs[1].cross_street_corrected or descs[1].cross_street)):
        return None
    near_index, far_index = sorted((0, 1), key=lambda i: descs[i].distance_to_corner_ft)
    near, far = descs[near_index], descs[far_index]
    delta = far.distance_to_corner_ft - near.distance_to_corner_ft
    front = near.segments[-1]
    ff = far.segments[-1]
    if front.distance_ft <= 1e-6 or ff.distance_ft <= 1e-6:
        return None
    ux, uy = (v / front.distance_ft for v in front.vector)
    fux, fuy = (v / ff.distance_ft for v in ff.vector)
    if (abs(delta - front.distance_ft) > TOLERANCE_FT
            or math.hypot(ux - fux, uy - fuy) > 1e-6):
        return None
    for source in (a, b):
        desc = source.item.data
        tie = desc.corner_to_pob_bearing
        if source.anchor is not None and (tie is None or not source.anchor.group.startswith('file:')
                                           or math.dist(source.anchor.pob, compute_pob_point(desc)) > 1e-5):
            return None
        if tie is not None:
            if not tie.has_bearing or tie.bearing_solved or tie.distance_ft <= 1e-6:
                return None
            tx, ty = tie.vector
            if math.hypot(tx / tie.distance_ft + ux, ty / tie.distance_ft + uy) > 1e-6:
                return None
    near_pts = build_polygon_points(near.segments, start=(0, 0))
    far_pts = build_polygon_points(far.segments, start=(-delta * ux, -delta * uy))
    matches = []
    for i in range(len(near.segments) - 1):
        for j in range(len(far.segments) - 1):
            if (math.dist(near_pts[i], far_pts[j+1]) <= TOLERANCE_FT
                    and math.dist(near_pts[i+1], far_pts[j]) <= TOLERANCE_FT):
                matches.append((i, j))
    if len(matches) != 1:
        return None
    # Matching sides alone can also occur on overlapping parcels. Permit only
    # a thin rounding sliver, never a substantial overlap of their interiors.
    try:
        p_near, p_far = Polygon(near_pts[:-1]), Polygon(far_pts[:-1])
        allowed_sliver = TOLERANCE_FT * near.segments[matches[0][0]].distance_ft
        if (not p_near.is_valid or not p_far.is_valid or p_near.area <= 0.5 or p_far.area <= 0.5
                or p_near.intersection(p_far).area > allowed_sliver):
            return None
    except Exception:
        return None
    tie = _make_tie_segment_like(front, distance_ft=near.distance_to_corner_ft, flip_180=True)
    return RowProposal(Path(a.source), near_index, far_index, near.main_street, near.cross_street,
                       far.cross_street, near.distance_to_corner_ft, far.distance_to_corner_ft,
                       near.segments[matches[0][0]].distance_ft, tie.bearing_label)


def accept_row(sources: Sequence[DescriptionSource], proposal: RowProposal) -> tuple[DescriptionSource, ...]:
    """Apply a revalidated geometry match in memory, without changing source data."""
    if propose_row(sources) != proposal:
        raise ValueError('stale or invalid row proposal')
    near = sources[proposal.near_index].item.data
    cross = near.cross_street
    group = f'confirmed-file:{proposal.source.resolve()}'
    output = []
    for source in sources:
        desc = source.item.data
        tie = (desc.corner_to_pob_bearing or
               _make_tie_segment_like(desc.segments[-1], distance_ft=desc.distance_to_corner_ft, flip_180=True))
        # Use the nearer description's printed label within this scene only;
        # neither spelling is claimed to have been verified as official.
        placed = desc.model_copy(update={'corner_to_pob_bearing': tie})
        # A common label is only presentation, never a change to parsed data.
        # Corrections from separate descriptions stay independently visible.
        common_main = None if any(s.item.data.main_street_corrected for s in sources) else near.main_street
        common_cross = None if any(s.item.data.cross_street_corrected for s in sources) else cross
        output.append(DescriptionSource(source.source, source.is_main, DescriptionItem(placed, True),
                                        Anchor(group, compute_pob_point(placed)), common_main, common_cross))
    return tuple(output)


def auto_place_rows(sources: Sequence[DescriptionSource], *, eligible: Sequence[bool] | None = None
                    ) -> tuple[tuple[DescriptionSource, ...], int]:
    """Place only mutually unique geometric pairs; keep duplicates and ambiguity separate."""
    if eligible is None:
        eligible = [True] * len(sources)
    if len(eligible) != len(sources):
        raise ValueError('invalid eligibility')
    matches = []
    counts = [0] * len(sources)
    for i in range(len(sources)):
        for j in range(i + 1, len(sources)):
            if not eligible[i] or not eligible[j]:
                continue
            proposal = propose_row((sources[i], sources[j]))
            if proposal is not None:
                matches.append((i, j, proposal))
                counts[i] += 1
                counts[j] += 1
    result = list(sources)
    placed = 0
    from dataclasses import replace
    for i, j, proposal in matches:
        if counts[i] != 1 or counts[j] != 1:
            continue
        pair = accept_row((sources[i], sources[j]), proposal)
        # A distinct group for each match, even when a file has several pairs.
        group = f'auto-row:{i}:{j}'
        for index, item in zip((i, j), pair):
            result[index] = replace(item, anchor=replace(item.anchor, group=group))
        placed += 1
    return tuple(result), placed
