"""Rules and layout for temporary, removable risk callouts in a CAD drawing.

No AutoCAD/COM calls here. Only automatic, recorded typo corrections are covered
by the first rule; additional kinds of risks require their own explicit rules.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable


LAYER = "TSAI-WARNINGS"


@dataclass(frozen=True)
class SideValues:
    ns: str
    degrees: int
    minutes: int
    seconds: float
    ew: str
    distance_ft: float

    @classmethod
    def of(cls, segment: Any) -> "SideValues":
        return cls(segment.ns, segment.degrees, segment.minutes, segment.seconds,
                   segment.ew, segment.distance_ft)


@dataclass(frozen=True)
class AutoCorrection:
    before: SideValues
    after: SideValues


def mark_auto_correction(before: Any, after: Any) -> Any:
    """Tag the actual corrected segment, not candidates or user-confirmed edits."""
    result = after.model_copy(deep=True)
    original = SideValues.of(before)
    prior = before._auto_corrections
    if prior and prior[-1].after == original:
        original = prior[-1].before
    result._auto_corrections = [AutoCorrection(original, SideValues.of(after))]
    return result


def _seconds(value: float) -> str:
    result = f"{value:05.2f}".rstrip("0").rstrip(".")
    return result.zfill(2)


def _red_changes(before: str, after: str) -> str:
    """Inline ACI 1 for changed glyphs; braces restore the parent entity colour."""
    if len(before) != len(after):
        before = before.zfill(len(after))
        after = after.zfill(len(before))
    return "".join(c if a == c else "{\\C1;" + c + "}" for a, c in zip(before, after))


def corrected_side_text(segment: Any, correction: AutoCorrection, distance_label: Callable[[Any], str]) -> tuple[str, str]:
    """Full bearing and distance, with the changed glyphs in red (MTEXT)."""
    before, expected = correction.before, correction.after
    after = SideValues.of(segment)
    # Fields not changed by the auto-fix can be edited later without turning red.
    before = SideValues(*(getattr(before, name) if getattr(before, name) != getattr(expected, name)
                          else getattr(after, name) for name in SideValues.__dataclass_fields__))
    fields = [
        (_red_changes(before.ns, after.ns), after.ns),
        (_red_changes(f"{before.degrees:02d}", f"{after.degrees:02d}"), f"{after.degrees:02d}"),
        (_red_changes(f"{before.minutes:02d}", f"{after.minutes:02d}"), f"{after.minutes:02d}"),
        (_red_changes(_seconds(before.seconds), _seconds(after.seconds)), _seconds(after.seconds)),
        (_red_changes(before.ew, after.ew), after.ew),
    ]
    if abs(before.distance_ft - after.distance_ft) > 1e-7:
        old, new = f"{before.distance_ft:.2f}", f"{after.distance_ft:.2f}"
        dist_mtext, dist_plain = _red_changes(old, new) + "'", new + "'"
    else:
        dist_mtext = dist_plain = distance_label(segment)
    marked = f'{fields[0][0]} {fields[1][0]}%%d{fields[2][0]}\'{fields[3][0]}" {fields[4][0]} {dist_mtext}'
    plain = f'{fields[0][1]} {fields[1][1]}°{fields[2][1]}\'{fields[3][1]}" {fields[4][1]} {dist_plain}'
    return marked, plain


@dataclass(frozen=True)
class RiskCallout:
    side_index: int
    tip: tuple[float, float]
    text_point: tuple[float, float]
    attachment: int  # AutoCAD middle-left 4 / middle-right 6
    text: str
    plain_text: str
    height: float


def plan_callouts(plan: Any, distance_label: Callable[[Any], str]) -> tuple[list[RiskCallout], list[str]]:
    """Choose a low-collision position inside or outside using known plan geometry.

    Layout is best-effort against planned lines and text, not the rest of the DWG.
    Points are based on the actual boundary line, including any displaced gap.
    """
    from shapely.geometry import LineString, box

    if plan.is_neighbor or plan.print_draft:
        return [], []
    output: list[RiskCallout] = []
    warnings: list[str] = []
    height = max(float(plan.text_height), 0.1)
    obstacles = []
    for line in [*plan.boundary_lines, *plan.lines]:
        obstacles.append(LineString([line.start, line.end]).buffer(height * 0.15))
    for item in plan.items:
        h = float(item.height or height)
        w = max(h, len(item.text) * h * 0.7)
        # Conservative axis-aligned box for rotated existing text.
        c, s = abs(math.cos(item.rotation)), abs(math.sin(item.rotation))
        dx, dy = (w*c + h*s)/2, (w*s + h*c)/2
        x, y = item.point
        obstacles.append(box(x-dx, y-dy, x+dx, y+dy).buffer(height * 0.25))
    for index, seg in enumerate(plan.description.segments):
        corrections = getattr(seg, "_auto_corrections", ())
        if not corrections:
            continue
        correction = corrections[-1]
        current = SideValues.of(seg)
        if any(getattr(correction.before, name) != getattr(correction.after, name)
               and getattr(current, name) != getattr(correction.after, name)
               for name in SideValues.__dataclass_fields__):
            warnings.append(f"пометка автоисправления стороны #{index + 1} пропущена: исправленное значение изменилось")
            continue
        if index >= len(plan.boundary_lines):
            warnings.append(f"пометка автоисправления стороны #{index + 1} не создана: линия не найдена")
            continue
        line = plan.boundary_lines[index]
        dx, dy = line.end[0] - line.start[0], line.end[1] - line.start[1]
        length = math.hypot(dx, dy)
        if length < 1e-9:
            warnings.append(f"пометка автоисправления стороны #{index + 1} не создана: нулевая линия")
            continue
        tip = ((line.start[0] + line.end[0])/2, (line.start[1] + line.end[1])/2)
        nx, ny = -dy / length, dx / length
        marked, plain = corrected_side_text(seg, correction, distance_label)
        width = max(height, len(plain) * height * 0.7)
        candidates = []
        for distance_factor in (4, 6, 9, 13):
            for sign in (1, -1):
                x, y = tip[0] + nx*height*distance_factor*sign, tip[1] + ny*height*distance_factor*sign
                rightward = x >= tip[0]
                text_box = box(x, y-height/2, x+width, y+height/2) if rightward else box(x-width, y-height/2, x, y+height/2)
                leader = LineString([tip, (x, y)]).difference(LineString([line.start, line.end]).buffer(height * 0.3))
                score = sum(3 * text_box.buffer(height * 0.25).intersects(o) + leader.intersects(o) for o in obstacles)
                candidates.append((score, distance_factor, sign, (x, y), 4 if rightward else 6, text_box))
        _, _, _, point, attachment, chosen_box = min(candidates, key=lambda c: (c[0], c[1], -c[2]))
        obstacles.append(chosen_box.buffer(height * 0.5))
        obstacles.append(LineString([tip, point]).buffer(height * 0.15))
        output.append(RiskCallout(index, tip, point, attachment, marked, plain, height))
    return output, warnings
