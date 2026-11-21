"""Conservative *proposals*, never automatic proof of a shared intersection.

A narrow two-lot row pattern can be offered to the user for review. The
candidate's direction is derived from the frontage and corroborating shared
edge, not read from the source; acceptance is required before using it.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Sequence

from shapely.geometry import Polygon

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
    """Offer only a unique simple same-file pair, never merge on names alone."""
    if len(sources) != 2:
        return None
    a, b = sources
    if (Path(a.source) != Path(b.source) or not a.is_main or not b.is_main
            or a.anchor is not None or b.anchor is not None
            or not a.item.complete or not b.item.complete):
        return None
    descs = [a.item.data, b.item.data]
    for desc in descs:
        if (not desc.main_street or not desc.cross_street or desc.corner_to_pob_bearing is not None
                or desc.cross_street_side_index is not None or len(desc.segments) < 3
                or desc.front_segment_index != len(desc.segments) - 1
                or desc.distance_to_corner_ft <= 0
                or not all(s.has_bearing and not s.bearing_solved for s in desc.segments)):
            return None
        pts = build_polygon_points(desc.segments)
        if math.dist(pts[0], pts[-1]) > ROUNDING_GAP_LIMIT_FT:
            return None
    normalize = lambda text: ' '.join(text.upper().split())
    if normalize(descs[0].main_street) != normalize(descs[1].main_street):
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
    """Apply a freshly revalidated user-confirmed proposal without changing cached data."""
    if propose_row(sources) != proposal:
        raise ValueError('stale or invalid row proposal')
    near = sources[proposal.near_index].item.data
    cross = near.cross_street
    group = f'confirmed-file:{proposal.source.resolve()}'
    output = []
    for source in sources:
        desc = source.item.data
        tie = _make_tie_segment_like(desc.segments[-1], distance_ft=desc.distance_to_corner_ft, flip_180=True)
        # Canonical label was explicitly displayed for approval; the original
        # parsed outcome remains untouched in the handoff and its cache.
        placed = desc.model_copy(update={'corner_to_pob_bearing': tie, 'cross_street': cross})
        output.append(DescriptionSource(source.source, source.is_main, DescriptionItem(placed, True),
                                        Anchor(group, compute_pob_point(placed))))
    return tuple(output)
