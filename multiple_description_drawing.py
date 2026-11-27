"""Multi-description scene and print-draft planning.

Автопоток и многодокументный режим строят здесь планы нескольких описаний,
но каждый отдельный план — ровно тот, что построил бы обычный режим по
этому описанию: тот же ``AcadBuilder.plan`` с пользовательской
конфигурацией, та же система координат от перекрёстка (0, 0), те же улицы,
привязки и подписи. Сценной геометрии поверх обычного плана нет.
No COM calls occur here; open_traverse prevents silently closing partial sides.
"""
from __future__ import annotations

from dataclasses import dataclass, replace as dc_replace
import math
from pathlib import Path
from typing import Any, Sequence

import llm_parser as lp
import lot_identity
from acad_builder import (
    AcadBuilder, DrawPlan, LineItem, TextItem, build_polygon_points,
    build_side_lines, closure_error, compute_pob_point, description_uses_feet_inches, estimate_text_width, text_rotation,
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
    """Проверить, что план построен именно из этого описания.

    Сравниваются записанные в план стороны описания и число граничных
    отрезков. Абсолютное положение отрезков не проверяется: обычный
    построитель вправе отнести невязку округления в другой угол, и план
    от этого не перестаёт быть планом этого описания — в обычном режиме
    такой план рисуется как есть.
    """
    segments = getattr(plan.description, 'segments', None)
    if segments is None or len(plan.boundary_lines) != len(item.data.segments):
        return False
    if len(segments) != len(item.data.segments):
        return False
    for recorded, parsed in zip(segments, item.data.segments):
        rx, ry = recorded.vector
        px, py = parsed.vector
        if not all(math.isfinite(v) for v in (rx, ry, px, py)):
            return False
        if math.hypot(rx - px, ry - py) > 1e-6:
            return False
    return all(math.isfinite(v) for edge in plan.boundary_lines
               for point in (edge.start, edge.end) for v in point)


def _drawing_description(record: DescriptionSource):
    """Описание для построения — ровно то, что обычный режим передаёт построителю.

    Разбор нескольких описаний честно хранит ``corner_to_pob_bearing=None``,
    если модель не дала румб привязки явно (такой выведенный румб не должен
    выглядеть в кэше как возвращённый ИИ и не становится якорем привязки
    между документами). Но обычный режим такой румб для построения
    вычисляет из обхода (``LegalDescription`` делает это в валидаторе), и
    без него построитель ставит участок прямо на перекрёсток, а дистанцию
    до него рисует условной короткой выноской — живой сбой автопотока
    25.11.2026: в кэше ``distance_to_corner_ft=80``, а перекрёстка и
    дистанции на чертеже нет. Поэтому для построения румб выводится здесь
    тем же ``infer_corner_to_pob_bearing``; исходное описание и кэш не
    меняются, якоря это не касается.
    """
    desc = lot_identity.bind_filename(record.item.data, record.source)
    if (getattr(desc, 'corner_to_pob_bearing', None) is None
            and float(getattr(desc, 'distance_to_corner_ft', 0.0) or 0.0) > 1e-6
            and getattr(desc, 'segments', None)):
        try:
            inferred = lp.infer_corner_to_pob_bearing(
                desc.segments,
                distance_to_corner_ft=desc.distance_to_corner_ft,
                front_segment_index=desc.front_segment_index,
                front_side=desc.front_side,
            )
        except Exception:  # noqa: BLE001 - построение не должно падать из-за привязки
            inferred = None
        if inferred is not None:
            desc = desc.model_copy(update={'corner_to_pob_bearing': inferred})
    return desc


def _plan_description(builder: AcadBuilder, record: DescriptionSource, scale: float) -> tuple[DrawPlan, str | None]:
    """План одного описания — тот же, что строит обычный режим.

    Тот же построитель с той же конфигурацией и без сценных поправок:
    ни принудительного ``leave_gap_open``, ни отбраковки планов, где
    построитель уточнил геометрию, ни упрощённых подписей. Только если
    обычный построитель не берётся за описание (неполное и меньше трёх
    известных сторон), рисуем открытые известные стороны — в обычном
    режиме такое описание просто не построилось бы.

    Сосед строится тем же полным построением, что и основной участок
    (улицы, привязка до перекрёстка, подписи сторон), и только потом
    помечается соседом с подписью ``neighbor-label``: урезанная ветка
    ``is_neighbor=True`` построителя — оформление одиночного соседа
    старого режима, а в сцене автопотока она прятала перекрёсток и
    дистанцию до него у всех соседних участков (живой сбой 25.11.2026).
    """
    desc = _drawing_description(record)
    try:
        plan = builder.plan(desc, scale=scale, is_neighbor=False)
    except Exception:
        if record.item.complete:
            raise
        start = record.anchor.pob if record.anchor is not None else compute_pob_point(desc)
        plan = plan_partial(builder, record.item, source=record.source, is_main=record.is_main,
                            scale=scale, start=start)
        return plan, 'partial_description'
    if not record.is_main:
        plan = dc_replace(
            plan, is_neighbor=True,
            items=[dc_replace(item, kind='neighbor-label') if item.kind == 'main-lot-label'
                   else item for item in plan.items],
        )
    return plan, None if record.item.complete else 'partial_description'


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


def plan_scene(builder: AcadBuilder, sources: Sequence[DescriptionSource], *, scale: float = 16.0,
               separation_ft: float = 200.0) -> SceneResult:
    """Спланировать все записи — каждую ровно как обычный режим.

    Никакой отдельной сценной раскладки: планы не сдвигаются в ряд, улицы
    и привязки не срезаются, общие подписи не сливаются. Все участки стоят
    в общей системе координат от перекрёстка (0, 0), как если бы
    пользователь разобрал и построил каждый вручную. Якорь остаётся
    только метаданными о подтверждённой привязке (``placement_known``,
    группировка), на геометрию плана он не влияет.
    ``separation_ft`` сохранён в сигнатуре для совместимости и не используется.
    """
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
        prepared.append(PlannedDescription(Path(source.source), source.is_main, index, plan,
                                           anchor.group if anchor else None, anchor is not None,
                                           reason if anchor is not None else (reason or 'unresolved_position')))
    return SceneResult(tuple(prepared), tuple(failures))


@dataclass(frozen=True)
class DraftSceneResult:
    plans: tuple[DrawPlan, ...]
    diagnostic: str | None


def make_scene_print_drafts(scene: SceneResult, *, builder_cfg: dict[str, Any],
                            print_cfg: dict[str, Any] | None = None) -> DraftSceneResult:
    """Печатные черновики сцены — как в обычном режиме.

    По одному ``make_print_draft`` на каждый основной участок (без
    соседей), с теми же настройками ``print_draft`` из конфигурации.
    Никакого общего композиционного черновика сцены: каждый черновик —
    ровно тот, что обычный режим построил бы по этому участку.
    """
    from acad_builder import make_print_draft
    plans: list[DrawPlan] = []
    notes: list[str] = []
    for entry in scene.plans:
        if not entry.is_main:
            continue
        try:
            plans.append(make_print_draft(entry.plan, dict(print_cfg or {}), builder_cfg=builder_cfg))
        except Exception:  # A draft failure must not hide the other drafts.
            notes.append('draft_planning_failed')
    return DraftSceneResult(tuple(plans), ';'.join(dict.fromkeys(notes)) or None)
