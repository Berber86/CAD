"""
Развод наложений: соседний участок, наложенный на наш, сдвигается в предпросмотре до появления общей межи.

Правила (см. docs/customer_rules.md, раздел «Развод наложений (3.86)»):
- Двигаем только наложения (площадь пересечения больше порога). Зазоры не трогаем.
- Сосед переносится жёстко: форма, длины и румбы не меняются, меняется только положение.
- Направление: сдвиг только наружу — от центра нашего участка к центру соседа (сосед уже лежит
  с этой стороны, и «слезать» логично туда же). Из нормалей сторон обоих участков берётся тот
  сдвиг, после которого наложения нет, а общая межа совпала не меньше чем на 10% большей стороны.
  Побеждает самый короткий сдвиг. Внутрь (через наш участок) не двигаем, если явно не разрешено.
- Сосед не заходит на красные линии улиц (нашего участка) и не ложится на уже развёденных соседей.
- Если такого сдвига нет в пределах ``max_move_ft`` — соседа не трогаем, пишем причину.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from shapely import affinity
from shapely.geometry import LineString, Polygon

#: Площадь пересечения, меньше которой наложением не считаем (как в сверке: «0.5 кв. ft — не наложение»).
OVERLAP_TOL_SQFT = 0.5
#: Общая межа — не короче этой доли большей стороны. Должна совпадать с JOIN_MIN_SHARED_FRACTION
#: в conflict_resolver (проверяется тестом).
SHARED_FRACTION = 0.10
#: Допуск параллельности сторон, градусов. Сторона соседа может расходиться с нашей на такой угол
#: (расхождение в документах); клин от него войдёт в полосу допуска общей межи.
PARALLEL_MAX_DEG = 2.0
#: Базовая полоса допуска общей межи, футов (как JOIN_LINE_TOLERANCE_FT).
CONTACT_BAND_FT = 0.05
#: Предельный сдвиг соседа, футов.
MAX_MOVE_FT = 200.0
COARSE_STEP_FT = 0.5
REFINE_STEP_FT = 0.01
#: Ширина, с которой линия улицы считается пересечением с телом соседа (отсекает касания по границе).
STREET_CROSS_MIN_FT = 0.5


@dataclass(frozen=True)
class SlideResult:
    #: ``no_overlap`` — наложения нет (сосед не трогаем); ``moved`` — сдвинут;
    #: ``not_found`` — подходящего сдвига нет; ``invalid`` — контур не строится.
    status: str
    dx: float = 0.0
    dy: float = 0.0
    distance_ft: float = 0.0
    #: Направление сдвига, градусы от оси +X против часовой (0 — восток, 90 — север).
    direction_deg: float = 0.0
    #: Длина общей межи после сдвига, футов.
    shared_ft: float = 0.0
    #: Площадь наложения до сдвига, кв. футов.
    overlap_before_sqft: float = 0.0
    note: str = ""
    #: Сколько сдвигов проверено и почему отсеяно (для журнала: что именно блокирует).
    tried: int = 0
    rejected: dict[str, int] | None = None


def _polygon(points: Sequence[tuple[float, float]]) -> Polygon | None:
    if len(points) < 3:
        return None
    poly = Polygon(points)
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.is_empty or poly.area <= 0:
        return None
    return poly


def _edges(points: Sequence[tuple[float, float]]) -> list[tuple[float, float, float]]:
    """(ux, uy, длина) для каждой стороны замкнутого контура."""
    out: list[tuple[float, float, float]] = []
    n = len(points)
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        length = math.hypot(x2 - x1, y2 - y1)
        if length > 1e-9:
            out.append(((x2 - x1) / length, (y2 - y1) / length, length))
    return out


def _ordered_translations(
    points_a, points_b, need: float, max_move_ft: float, sample_ft: float = 0.25, max_samples: int = 800,
) -> list[tuple[float, float, float]]:
    """
    Сдвиги соседа, при которых его сторона ложится на нашу сторону с общей частью ``need``.

    Для пары сторон (наша A, соседа B) направление n — наружу от участка. Сосед сдвигается так,
    чтобы его ближайшая к нам точка стороны B легла на линию A (наложения нет). Сдвиг
    t = c·n + s·u, где u — направление A. Общая часть как функция s линейна: допустимо отрезком
    ``s ∈ [need − hi0, LA − need − lo0]`` (lo0/hi0 — проекции стороны B на u при s = 0).
    Возвращаются (dx, dy, полоса допуска), отсортировано по длине сдвига.
    Полоса = CONTACT_BAND_FT + клин от угла между сторонами на меньшей из них.
    """
    A = _edges_with_points(points_a)
    B = _edges_with_points(points_b)
    outward_sign = 1.0 if Polygon(points_a).exterior.is_ccw else -1.0
    sin_par = math.sin(math.radians(PARALLEL_MAX_DEG))
    out: list[tuple[float, float, float]] = []
    for ax0, ay0, ax1, ay1, la, ux, uy, nx0, ny0 in A:
        nx, ny = -nx0 * outward_sign, -ny0 * outward_sign  # наружу от нашего участка
        for bx0, by0, bx1, by1, lb, vx, vy, _, _ in B:
            cross = ux * vy - uy * vx
            if abs(cross) > sin_par:
                continue  # не параллельны в пределах допуска
            wedge = min(la, lb) * abs(cross)
            band = CONTACT_BAND_FT + wedge
            if lb < need - 1e-9 or la < need - 1e-9:
                continue
            d0 = (bx0 - ax0) * nx + (by0 - ay0) * ny
            d1 = (bx1 - ax0) * nx + (by1 - ay0) * ny
            c = -min(d0, d1)  # ближайшая к нам точка стороны B ложится на линию A
            p0 = (bx0 - ax0) * ux + (by0 - ay0) * uy
            p1 = (bx1 - ax0) * ux + (by1 - ay0) * uy
            lo0, hi0 = min(p0, p1), max(p0, p1)
            s_lo = need - hi0
            s_hi = la - need - lo0
            if s_lo > s_hi:
                continue
            samples = {s_lo, s_hi, min(max(0.0, s_lo), s_hi)}
            span = s_hi - s_lo
            count = min(max_samples, int(span / sample_ft) + 1)
            for k in range(count + 1):
                samples.add(s_lo + span * k / max(count, 1))
            for s in samples:
                dx = c * nx + s * ux
                dy = c * ny + s * uy
                if math.hypot(dx, dy) <= max_move_ft:
                    out.append((dx, dy, band))
    out.sort(key=lambda d: math.hypot(d[0], d[1]))
    return out


def _edges_with_points(points):
    """Для каждой стороны: концы, длина, единичное направление u и нормаль n."""
    rows = []
    n = len(points)
    for i in range(n):
        x0, y0 = points[i]
        x1, y1 = points[(i + 1) % n]
        length = math.hypot(x1 - x0, y1 - y0)
        if length <= 1e-9:
            continue
        ux, uy = (x1 - x0) / length, (y1 - y0) / length
        rows.append((x0, y0, x1, y1, length, ux, uy, -uy, ux))
    return rows


def _has_parallel_edge(points_a, points_b, tol_deg: float = PARALLEL_MAX_DEG) -> bool:
    """Есть ли у соседа сторона, параллельная одной из наших (с допуском в градусах)."""
    sin_tol = math.sin(math.radians(tol_deg))
    for ax, ay, _ in _edges(points_a):
        for bx, by, _ in _edges(points_b):
            if abs(ax * by - ay * bx) <= sin_tol:
                return True
    return False


def _longest_edge(points) -> float:
    edges = _edges(points)
    return max((length for _, _, length in edges), default=0.0)


def _shared_length(main: Polygon, moved: Polygon) -> float:
    return main.boundary.intersection(moved.boundary).length


def _shared_length_banded(main: Polygon, moved: Polygon, band: float) -> float:
    """
    Длина части границы соседа, лежащей не дальше ``band`` от нашей границы.
    Точное касание сторон даёт 0 при расходящихся сторонах; полоса учитывает клин.
    """
    near = main.boundary.buffer(band)
    return moved.boundary.intersection(near).length


def _crosses_street(moved: Polygon, street_lines: Sequence[LineString]) -> bool:
    interior = moved.buffer(-0.01)
    if interior.is_empty:
        return False
    return any(interior.intersection(line).length > STREET_CROSS_MIN_FT for line in street_lines)


def slide_off_overlap(
    main_points: Sequence[tuple[float, float]],
    neighbour_points: Sequence[tuple[float, float]],
    *,
    street_lines: Sequence[Sequence[tuple[float, float]]] = (),
    others: Sequence[Sequence[tuple[float, float]]] = (),
    shared_fraction: float = SHARED_FRACTION,
    max_move_ft: float = MAX_MOVE_FT,
    overlap_tol_sqft: float = OVERLAP_TOL_SQFT,
    allow_inward: bool = False,
) -> SlideResult:
    """
    Сдвиг соседа ``neighbour_points`` с нашего участка ``main_points`` до появления общей межи.

    ``street_lines`` — отрезки красных линий улиц (пары точек), в которые сосед не заходит.
    ``others`` — контуры уже развёденных соседей, на которые сосед не должен наехать.
    """
    main = _polygon(main_points)
    neighbour = _polygon(neighbour_points)
    if main is None or neighbour is None:
        return SlideResult(status="invalid", note="контур участка не строится")

    before = main.intersection(neighbour).area
    if before <= overlap_tol_sqft:
        return SlideResult(status="no_overlap", overlap_before_sqft=before)

    need = shared_fraction * max(_longest_edge(main_points), _longest_edge(neighbour_points))
    street = [LineString(seg) for seg in street_lines if len(seg) >= 2]  # только красные линии улиц
    other_polys = [p for p in (_polygon(o) for o in others) if p is not None]

    def check_reason(dx: float, dy: float, band: float) -> str | None:
        """None — сдвиг допустим; иначе причина отказа."""
        moved = affinity.translate(neighbour, dx, dy)
        if main.intersection(moved).area > overlap_tol_sqft:
            return "наложение с нашим"
        if any(moved.intersection(p).area > overlap_tol_sqft for p in other_polys):
            return "наложение с соседом"
        if _crosses_street(moved, street):
            return "улица"
        if _shared_length_banded(main, moved, band) + 1e-6 < need:
            return "общая межа короче порога"
        return None

    main_c = main.centroid
    nb_c = neighbour.centroid
    away = (nb_c.x - main_c.x, nb_c.y - main_c.y)
    best: tuple[float, float, float, float] | None = None  # (|t|, dx, dy, shared)
    rejected: dict[str, int] = {
        "не наружу": 0, "наложение с нашим": 0, "наложение с соседом": 0,
        "улица": 0, "общая межа короче порога": 0,
    }
    tried = 0
    for dx, dy, band in _ordered_translations(main_points, neighbour_points, need, max_move_ft):
        tried += 1
        if not allow_inward and dx * away[0] + dy * away[1] < -1e-9:
            rejected["не наружу"] += 1
            continue
        reason = check_reason(dx, dy, band)
        if reason is None:
            shared = _shared_length_banded(main, affinity.translate(neighbour, dx, dy), band)
            best = (math.hypot(dx, dy), dx, dy, shared)
            break
        rejected[reason] += 1

    if best is None:
        counts = ", ".join(f"{name} — {n}" for name, n in rejected.items() if n) or "кандидатов нет"
        note = (
            f"наружу нет сдвига до {max_move_ft:.0f} ft, при котором наложения нет и общая межа "
            f"не короче {need:.2f} ft; проверено сдвигов: {tried} (отсеяно: {counts}); сосед не тронут"
        )
        if not _has_parallel_edge(main_points, neighbour_points):
            note += (
                "; сосед повёрнут относительно нашей межи — одним переносом общую межу не получить, "
                "нужен поворот"
            )
        return SlideResult(
            status="not_found", overlap_before_sqft=before, note=note, tried=tried, rejected=rejected,
        )
    s, dx, dy, shared = best
    direction = math.degrees(math.atan2(dy, dx)) % 360.0 if s > 0 else 0.0
    return SlideResult(
        status="moved",
        dx=dx,
        dy=dy,
        distance_ft=s,
        direction_deg=direction,
        shared_ft=shared,
        overlap_before_sqft=before,
    )
