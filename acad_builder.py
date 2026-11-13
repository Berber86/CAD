"""
acad_builder.py
===============
Геодезическая математика (румбы -> декартовы координаты, невязка полигона)
и отрисовка участка в уже открытом AutoCAD через ActiveX (pywin32).

Система координат: WCS, 0° = +Y (север), 90° = +X (восток), единицы — футы.
Точка начала обхода (Point of Beginning) помещается в (0, 0).

Отрисовывается:
  * замкнутая полилиния участка в слое PROPERTY_LINE;
  * подпись каждой стороны (румб + длина) вдоль линии, снаружи полигона,
    с поворотом по линии и защитой от «вверх ногами»;
  * название фасадной улицы вдоль фасадной стороны;
  * название пересекаемой улицы + расстояние до угла.

Код разделён на два слоя:
  * ``AcadBuilder.plan()`` — чистая математика и раскладка подписей (без COM);
  * ``AcadBuilder.draw()``  — подключение к AutoCAD и создание объектов.

Python: 3.11+, Windows, запущенный AutoCAD.
"""

from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable, Iterable, Sequence

from llm_parser import LegalDescription, Segment, load_config

__all__ = [
    "AcadBuilderError",
    "AcadConnectionError",
    "AcadDrawError",
    "ArcItem",
    "ClosureError",
    "DrawPlan",
    "DrawResult",
    "LineItem",
    "NeighborPairReport",
    "NeighborVerificationReport",
    "SharedEdgeCheck",
    "TextItem",
    "AcadBuilder",
    "bearing_azimuth_deg",
    "bearing_to_xy",
    "build_boundary_lines",
    "build_polygon_points",
    "build_side_label",
    "build_side_lines",
    "closure_error",
    "compute_pob_point",
    "draw_legal_description",
    "estimate_text_width",
    "format_bearing",
    "format_distance",
    "point_in_polygon",
    "polygon_centroid",
    "polygon_intersection_area",
    "polygon_signed_area",
    "resolve_analytical_right_trapezoid",
    "round_ft",
    "select_gap_vertex_index",
    "text_rotation",
    "verify_with_neighbors",
]

# --- Константы AutoCAD ActiveX (значения одинаковы во всех версиях) ---------
ACAD_PROGID = "AutoCAD.Application"
AC_ALIGNMENT_MIDDLE_CENTER = 10

_INSUNITS_NAMES = {
    0: "unitless",
    1: "inches",
    2: "feet",
    3: "miles",
    4: "millimeters",
    5: "centimeters",
    6: "meters",
    7: "kilometers",
    8: "microinches",
    9: "mils",
    10: "yards",
    13: "microns",
    14: "decimeters",
    15: "decameters",
    16: "hectometers",
}

#: допуск невязки от округления длин до 0.01 ft (ALTA/NSPS: ~0.02 ft = 1/4")
CLOSURE_TOLERANCE_FT = 0.02


# ---------------------------------------------------------------------------
# Ошибки
# ---------------------------------------------------------------------------


class AcadBuilderError(RuntimeError):
    """Базовая ошибка отрисовки."""


class AcadConnectionError(AcadBuilderError):
    """AutoCAD не найден / недоступен."""


class AcadDrawError(AcadBuilderError):
    """AutoCAD отказал при выполнении команды."""


# ---------------------------------------------------------------------------
# Геодезическая математика
# ---------------------------------------------------------------------------


def bearing_azimuth_deg(
    ns: str, degrees: float, minutes: float, seconds: float, ew: str
) -> float:
    """
    Румб -> азимут от севера по часовой стрелке (0..360°).

    N 75°20'20" E -> 75.3389°, S 45°00'00" W -> 225.0°.
    """
    angle = float(degrees) + float(minutes) / 60.0 + float(seconds) / 3600.0
    ns = str(ns).strip().upper()[:1]
    ew = str(ew).strip().upper()[:1]
    if ns == "N":
        azimuth = angle if ew == "E" else 360.0 - angle
    elif ns == "S":
        azimuth = 180.0 - angle if ew == "E" else 180.0 + angle
    else:
        raise ValueError(f"некорректная первая буква румба: {ns!r}")
    return azimuth % 360.0


def bearing_to_xy(
    ns: str,
    degrees: float,
    minutes: float,
    seconds: float,
    ew: str,
    distance_ft: float,
) -> tuple[float, float]:
    """
    Румб + длина -> (dX, dY) в футах.

    0° (север) = +Y, 90° (восток) = +X. Работает и для румбов, и для азимутов.
    """
    azimuth = math.radians(bearing_azimuth_deg(ns, degrees, minutes, seconds, ew))
    return float(distance_ft) * math.sin(azimuth), float(distance_ft) * math.cos(azimuth)


def segment_vector(segment: Segment) -> tuple[float, float]:
    """(dX, dY) стороны участка."""
    return bearing_to_xy(
        segment.ns,
        segment.degrees,
        segment.minutes,
        segment.seconds,
        segment.ew,
        segment.distance_ft,
    )


def build_polygon_points(
    segments: Sequence[Segment], start: tuple[float, float] = (0.0, 0.0)
) -> list[tuple[float, float]]:
    """
    Обход участка по сторонам.

    Возвращает (n + 1) точку: начало обхода + конец каждой стороны, включая
    конечную точку замыкания. Конечная точка совпадает с началом лишь
    с точностью до невязки — поэтому она СОХРАНЯЕТСЯ (её и измеряет
    closure_error, а вершины замкнутой полилинии берутся как points[:-1]).
    """
    x, y = float(start[0]), float(start[1])
    points: list[tuple[float, float]] = [(x, y)]
    for segment in segments:
        dx, dy = segment_vector(segment)
        x += dx
        y += dy
        points.append((x, y))
    return points


def polygon_signed_area(points: Sequence[tuple[float, float]]) -> float:
    """Площадь со знаком: > 0 — обход против часовой стрелки (CCW)."""
    area = 0.0
    count = len(points)
    for index in range(count):
        x1, y1 = points[index]
        x2, y2 = points[(index + 1) % count]
        area += x1 * y2 - x2 * y1
    return area / 2.0


def point_in_polygon(
    point: tuple[float, float], points: Sequence[tuple[float, float]]
) -> bool:
    """Ray casting: лежит ли точка внутри полигона."""
    x, y = point
    inside = False
    count = len(points)
    if count < 3:
        return False
    j = count - 1
    for i in range(count):
        xi, yi = points[i]
        xj, yj = points[j]
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def compute_pob_point(description: LegalDescription) -> tuple[float, float]:
    """
    Координаты точки начала (POB) относительно перекрёстка улиц (0.0, 0.0).

    Если участок угловой (`distance_to_corner_ft == 0`), POB совпадает с `(0.0, 0.0)`.
    Иначе от `(0.0, 0.0)` откладывается `distance_to_corner_ft` вдоль румба
    `corner_to_pob_bearing` — благодаря этому основной и соседние участки
    вдоль одной улицы встают в AutoCAD строго стенка к стенке.
    """
    dist = float(getattr(description, "distance_to_corner_ft", 0.0) or 0.0)
    tie = getattr(description, "corner_to_pob_bearing", None)
    if dist <= 1e-6 or tie is None:
        return (0.0, 0.0)
    return bearing_to_xy(
        tie.ns,
        tie.degrees,
        tie.minutes,
        tie.seconds,
        tie.ew,
        dist,
    )


def polygon_centroid(points: Sequence[tuple[float, float]]) -> tuple[float, float]:
    """
    Геометрический центроид полигона (формула Грина / Шнуровки).
    Для сильно вогнутых (Г-образных) участков, если центроид оказался снаружи,
    подбирает ближайшую внутреннюю точку на секущей через среднюю линию.
    """
    count = len(points)
    if count == 0:
        return (0.0, 0.0)
    if count < 3:
        return (
            sum(p[0] for p in points) / count,
            sum(p[1] for p in points) / count,
        )

    area2 = 0.0
    cx_acc = 0.0
    cy_acc = 0.0
    for i in range(count):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % count]
        cross = x1 * y2 - x2 * y1
        area2 += cross
        cx_acc += (x1 + x2) * cross
        cy_acc += (y1 + y2) * cross

    if abs(area2) < 1e-9:
        return (
            sum(p[0] for p in points) / count,
            sum(p[1] for p in points) / count,
        )

    cx = cx_acc / (3.0 * area2)
    cy = cy_acc / (3.0 * area2)
    if point_in_polygon((cx, cy), points):
        return (cx, cy)

    # Фолбэк для вогнутого участка: середина самой длинной горизонтальной хорды через cy.
    xs_cross: list[float] = []
    for i in range(count):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % count]
        if (y1 > cy) != (y2 > cy):
            xs_cross.append(x1 + (x2 - x1) * (cy - y1) / (y2 - y1))
    xs_cross.sort()
    best_pt = (cx, cy)
    best_span = -1.0
    for k in range(0, len(xs_cross) - 1, 2):
        span = xs_cross[k + 1] - xs_cross[k]
        mid_x = 0.5 * (xs_cross[k] + xs_cross[k + 1])
        if span > best_span and point_in_polygon((mid_x, cy), points):
            best_span = span
            best_pt = (mid_x, cy)
    return best_pt


def resolve_analytical_right_trapezoid(
    description: LegalDescription,
    segments: Sequence[Segment],
    front_index: int | None,
) -> tuple[list[Segment], str | None]:
    """
    Исключение №1 (правило точной перпендикулярной глубины лота, напр. 63-01 Borden Ave):
    если 4-угольный участок имеет:
      * фронт вдоль ``main_street`` точной длины L_f (напр. 20.72'),
      * одну боковую границу строго под 90° к ``main_street`` точной глубины L_d (напр. 90.00'),
      * заднюю границу строго параллельно ``main_street`` (PARALLEL WITH ..., напр. 26.32'),
      * вторую боковую границу под косым углом != 90° (напр. 93°33'29.5", 90.17'),
    и исходный микрогэп (<= 0.03 ft) вызван исключительно округлением L_oblique и L_rear до 0.01',
    принимает за основу L_f, L_d и все углы и вычисляет точные аналитические длины
    L_oblique* и L_rear*, замыкая контур строго в 0.0000 ft.
    """
    segs = list(segments)
    if len(segs) != 4 or front_index is None or not (0 <= front_index < 4):
        return segs, None

    raw_pts = build_polygon_points(segs)
    raw_gap = math.hypot(raw_pts[-1][0] - raw_pts[0][0], raw_pts[-1][1] - raw_pts[0][1])
    if not (1e-6 < raw_gap <= 0.03):
        return segs, None

    f_idx = front_index
    r_idx = (f_idx + 2) % 4
    adj_a = (f_idx - 1) % 4
    adj_b = (f_idx + 1) % 4

    unit_vecs: list[tuple[float, float]] = []
    for s in segs:
        dx, dy = segment_vector(s)
        length = math.hypot(dx, dy)
        if length < 1e-9:
            return segs, None
        unit_vecs.append((dx / length, dy / length))

    ufx, ufy = unit_vecs[f_idx]
    urx,ury = unit_vecs[r_idx]
    # Задняя сторона должна быть параллельна (противоположна по обходу) фасаду
    cross_fr = abs(ufx * ury - ufy * urx)
    dot_fr = ufx * urx + ufy * ury
    if cross_fr > math.sin(math.radians(0.03)) or dot_fr > -0.999:
        return segs, None

    dot_a = abs(ufx * unit_vecs[adj_a][0] + ufy * unit_vecs[adj_a][1])
    dot_b = abs(ufx * unit_vecs[adj_b][0] + ufy * unit_vecs[adj_b][1])
    tol_perp = math.sin(math.radians(0.03))
    min_oblique = math.sin(math.radians(0.08))

    if dot_a <= tol_perp and dot_b >= min_oblique:
        d_idx, s_idx = adj_a, adj_b
    elif dot_b <= tol_perp and dot_a >= min_oblique:
        d_idx, s_idx = adj_b, adj_a
    else:
        return segs, None

    usx, usy = unit_vecs[s_idx]
    det = usx * ury - usy * urx
    if abs(det) < 0.05:
        return segs, None

    lf = segs[f_idx].distance_ft
    ld = segs[d_idx].distance_ft
    rhs_x = -(lf * ufx + ld * unit_vecs[d_idx][0])
    rhs_y = -(lf * ufy + ld * unit_vecs[d_idx][1])

    ls_exact = (rhs_x * ury - rhs_y * urx) / det
    lr_exact = (usx * rhs_y - usy * rhs_x) / det
    if ls_exact <= 0.0 or lr_exact <= 0.0:
        return segs, None

    old_ls = segs[s_idx].distance_ft
    old_lr = segs[r_idx].distance_ft
    # Строгая проверка: обе аналитические длины при округлении до 0.01' совпадают с документом
    if abs(ls_exact - old_ls) > 0.01 or abs(lr_exact - old_lr) > 0.01:
        return segs, None

    refined = list(segs)
    refined[s_idx] = segs[s_idx].model_copy(update={"distance_ft": ls_exact})
    refined[r_idx] = segs[r_idx].model_copy(update={"distance_ft": lr_exact})
    street_title = description.main_street or "фасадной улицы"
    note = (
        f"Аналитическое замыкание прямоугольной трапеции: за основу приняты фронт "
        f"{format_distance(lf)} ({street_title}), перпендикулярная глубина "
        f"{format_distance(ld)} под 90° и точный косой угол; округлённые стороны уточнены "
        f"№{s_idx + 1} ({old_ls:.2f}' → {ls_exact:.4f}') и №{r_idx + 1} "
        f"({old_lr:.2f}' → {lr_exact:.4f}'), контур замкнут в 0.0000 ft"
    )
    return refined, note


def select_gap_vertex_index(
    description: LegalDescription,
    vertices: Sequence[tuple[float, float]],
    front_index: int | None,
    ccw: bool,
) -> tuple[int, str]:
    """
    Выбирает вершину для размещения открытого гэпа (без изменения длин и углов сторон):
      * Угловой участок на пересечении 2 улиц: вершина, НЕ соприкасающаяся ни с одной
        из двух улиц (внутренний задний угол — левый или правый).
      * Рядовой участок: правый угол задней стороны:
          - если дорога сверху (внешняя нормаль фасада смотрит на север / восток-запад) ->
            правый нижний угол;
          - если дорога снизу (внешняя нормаль фасада смотрит в южную полуплоскость) ->
            правый верхний угол.
    """
    n = len(vertices)
    if n == 0:
        return 0, "начальная вершина"
    f_idx = (front_index % n) if front_index is not None else 0

    street_vertices: set[int] = {f_idx, (f_idx + 1) % n}
    cross_idx = getattr(description, "cross_street_side_index", None)
    dist_corner = float(getattr(description, "distance_to_corner_ft", 0.0) or 0.0)
    is_corner_lot = bool(description.cross_street) and (
        cross_idx is not None or dist_corner <= 1e-6
    )
    if is_corner_lot:
        c_edge = (
            int(cross_idx)
            if cross_idx is not None and 0 <= int(cross_idx) < n
            else (f_idx - 1) % n
        )
        street_vertices.add(c_edge % n)
        street_vertices.add((c_edge + 1) % n)

    if 0 <= f_idx < len(description.segments):
        fdx, fdy = segment_vector(description.segments[f_idx])
    else:
        f_start = vertices[f_idx]
        f_end = vertices[(f_idx + 1) % n]
        fdx = f_end[0] - f_start[0]
        fdy = f_end[1] - f_start[1]
    flen = math.hypot(fdx, fdy)
    if flen > 1e-9:
        ux, uy = fdx / flen, fdy / flen
        nx, ny = (uy, -ux) if ccw else (-uy, ux)
    else:
        nx, ny = 0.0, 1.0

    # Вектор вглубь участка (от улицы к задней границе)
    rear_x, rear_y = -nx, -ny
    if ny >= -1e-6:
        # Дорога сверху листа -> задняя сторона снизу -> правый нижний угол
        up_x, up_y = nx, ny
        sheet_pos = "правый нижний угол (дорога сверху)"
    else:
        # Дорога снизу листа -> задняя сторона сверху -> правый верхний угол
        up_x, up_y = -nx, -ny
        sheet_pos = "правый верхний угол (дорога снизу)"
    right_x, right_y = up_y, -up_x

    candidates = [v for v in range(n) if v not in street_vertices]
    if not candidates:
        candidates = [v for v in range(n) if v != f_idx] or [0]

    if is_corner_lot and len(candidates) == 1:
        only_v = candidates[0]
        return only_v, "внутренний угол вне обеих улиц (угловой лот)"

    all_rear = [pt[0] * rear_x + pt[1] * rear_y for pt in vertices]
    min_r = min(all_rear)
    max_r = max(all_rear)
    rear_scores = {
        v: vertices[v][0] * rear_x + vertices[v][1] * rear_y for v in candidates
    }
    rear_band = [
        v for v in candidates if rear_scores[v] >= min_r + 0.45 * (max_r - min_r) - 1e-6
    ] or candidates

    chosen = max(
        rear_band,
        key=lambda v: (
            round(vertices[v][0] * right_x + vertices[v][1] * right_y, 4),
            round(rear_scores[v], 4),
        ),
    )
    if is_corner_lot:
        return chosen, f"внутренний задний угол вне обеих улиц ({sheet_pos})"
    return chosen, sheet_pos


def build_boundary_lines(
    segments: Sequence[Segment],
    pob: tuple[float, float] = (0.0, 0.0),
    *,
    gap_vertex_index: int = 0,
    leave_gap_open: bool = True,
) -> tuple[
    list[LineItem],
    list[tuple[float, float]],
    tuple[tuple[float, float], tuple[float, float]] | None,
]:
    """
    Строит N отдельных отрезков ``LineItem`` для всех сторон участка:
      * каждый отрезок имеет строго паспортную длину и направление из ``segments``;
      * вершина 0 жёстко стоит в ``pob`` (от перекрёстка ``(0, 0)``);
      * при ненулевом гэпе и ``leave_gap_open=True`` разрыв выводится ровно в
        вершину ``gap_vertex_index`` (все остальные N-1 углов состыкованы в ноль).
    """
    n = len(segments)
    if n == 0:
        return [], [], None
    k = gap_vertex_index % n
    vecs = [segment_vector(s) for s in segments]

    q_pts: list[tuple[float, float]] = [(0.0, 0.0)]
    qx, qy = 0.0, 0.0
    for step in range(n):
        dx, dy = vecs[(k + step) % n]
        qx += dx
        qy += dy
        q_pts.append((qx, qy))

    pob_step = (n - k) % n
    shift_x = float(pob[0]) - q_pts[pob_step][0]
    shift_y = float(pob[1]) - q_pts[pob_step][1]
    r_pts = [(px + shift_x, py + shift_y) for px, py in q_pts]

    gap_dist = math.hypot(r_pts[n][0] - r_pts[0][0], r_pts[n][1] - r_pts[0][1])
    lines: list[LineItem] = []
    vertices: list[tuple[float, float]] = []
    for i in range(n):
        j = (i - k) % n
        start_pt = r_pts[j]
        if not leave_gap_open and gap_dist > 1e-9 and j == n - 1:
            end_pt = r_pts[0]
        else:
            end_pt = r_pts[j + 1]
        lines.append(LineItem(start=start_pt, end=end_pt, kind=f"boundary-{i + 1}"))
        vertices.append(start_pt)

    gap_pts = (r_pts[0], r_pts[n]) if (leave_gap_open and gap_dist > 1e-7) else None
    return lines, vertices, gap_pts


def _diagnose_unresolved_gap(
    segments: Sequence[Segment],
    closure: ClosureError,
) -> str | None:
    """
    Если после всех проверок осталась крупная невязка (> 0.02 ft), проверяет,
    объясняется ли она изменением длины одной из сторон (проекция невязки вдоль курса),
    и формирует подсказку геодезисту без автоматического изменения чертежа.
    """
    if closure.distance <= 0.02 or not segments:
        return None
    ex, ey = closure.dx, closure.dy
    hints: list[str] = []
    for idx, seg in enumerate(segments):
        dx, dy = segment_vector(seg)
        length = math.hypot(dx, dy)
        if length < 1e-6:
            continue
        ux, uy = dx / length, dy / length
        perp = abs(ex * uy - ey * ux)
        if perp <= max(0.05, 0.02 * closure.distance):
            alt_len = seg.distance_ft - (ex * ux + ey * uy)
            if alt_len > 0.5:
                hints.append(
                    f"сторона №{idx + 1} ({seg.distance_ft:.2f}' → {alt_len:.2f}')"
                )
    if hints:
        return "Возможный источник невязки: " + " или ".join(hints[:2])
    return None


def _polygon_self_intersects(points: Sequence[tuple[float, float]]) -> bool:
    """Проверяет пересечение несмежных рёбер замкнутого контура (восьмёрка/бант)."""
    def ccw_turn(
        p1: tuple[float, float], p2: tuple[float, float], p3: tuple[float, float]
    ) -> float:
        return (p2[0] - p1[0]) * (p3[1] - p1[1]) - (p2[1] - p1[1]) * (p3[0] - p1[0])

    n = len(points)
    if n < 4:
        return False
    for i in range(n):
        a1, a2 = points[i], points[(i + 1) % n]
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue
            b1, b2 = points[j], points[(j + 1) % n]
            d1 = ccw_turn(a1, a2, b1)
            d2 = ccw_turn(a1, a2, b2)
            d3 = ccw_turn(b1, b2, a1)
            d4 = ccw_turn(b1, b2, a2)
            if (d1 * d2 < -1e-6) and (d3 * d4 < -1e-6):
                return True
    return False


def polyline_length(
    points: Sequence[tuple[float, float]], closed: bool = True
) -> float:
    """Длина полилинии (по умолчанию — замкнутой)."""
    total = 0.0
    for index in range(len(points) - 1):
        total += math.dist(points[index], points[index + 1])
    if closed and len(points) > 2:
        total += math.dist(points[-1], points[0])
    return total


@dataclass(frozen=True)
class ClosureError:
    """Невязка полигона: расстояние от конечной точки обхода до начала (0, 0)."""

    dx: float
    dy: float
    distance: float
    perimeter: float

    @property
    def closure_vector(self) -> tuple[float, float]:
        return self.dx, self.dy

    @property
    def precision(self) -> float | None:
        """Относительная точность 1:N (None, если невязка нулевая)."""
        if self.distance <= 1e-9:
            return None
        return self.perimeter / self.distance

    def short_report(self) -> str:
        """Компактная строка для статусной панели GUI."""
        precision = self.precision
        precision_text = (
            "точность идеальная" if precision is None else f"точность 1:{precision:,.0f}"
        )
        return (
            f"Closure Error: {self.distance:.3f} ft  |  "
            f"ΔX {self.dx:+.3f}, ΔY {self.dy:+.3f}  |  "
            f"периметр {self.perimeter:,.2f} ft  |  {precision_text}"
        )

    def report(self) -> str:
        precision = self.precision
        precision_text = (
            "идеальная (0.000 ft)" if precision is None else f"1:{precision:,.0f}"
        )
        return (
            f"Closure Error = {self.distance:.3f} ft "
            f"(ΔX {self.dx:+.3f}, ΔY {self.dy:+.3f}); "
            f"периметр {self.perimeter:,.2f} ft; точность {precision_text}"
        )


def closure_error(
    points: Sequence[tuple[float, float]],
    perimeter: float | None = None,
    start: tuple[float, float] | None = None,
) -> ClosureError:
    """
    Невязка: points — результат build_polygon_points (последняя точка =
    конец обхода, первая = начало, по умолчанию (0, 0)).
    """
    if len(points) < 2:
        raise ValueError("для расчёта невязки нужно минимум 2 точки")
    origin = start if start is not None else points[0]
    dx = points[-1][0] - origin[0]
    dy = points[-1][1] - origin[1]
    total = perimeter if perimeter is not None else polyline_length(points[:-1])
    return ClosureError(dx=dx, dy=dy, distance=math.hypot(dx, dy), perimeter=total)


# ---------------------------------------------------------------------------
# Форматирование подписей
# ---------------------------------------------------------------------------


def round_ft(value: float, decimals: int = 2) -> float:
    """Округление «половина вверх» (85.505 -> 85.51), как принято в съёмке."""
    quant = Decimal(1).scaleb(-decimals)
    return float(Decimal(str(float(value))).quantize(quant, rounding=ROUND_HALF_UP))


def format_distance(distance_ft: float, decimals: int = 2) -> str:
    """55 -> "55.00'"."""
    return f"{round_ft(distance_ft, decimals):.{decimals}f}" + "'"


def format_bearing(segment: Segment, use_control_code: bool = True) -> str:
    """
    Каноничная подпись румба: ``N 75°20'20" E``.

    В DBTEXT через COM код градуса надёжнее задавать как %%d — AutoCAD
    превратит его в символ градуса при любом стиле и кодировке.
    """
    degrees = "%%d" if use_control_code else "°"
    seconds = f"{segment.seconds:.2f}".rstrip("0").rstrip(".")
    head = f"{segment.ns} {int(segment.degrees)}{degrees}{int(segment.minutes):02d}'"
    if seconds in {"0", ""}:
        # Ровные минуты: «N 45%%d00' E» — без лишних 0".
        return f"{head} {segment.ew}"
    return f"{head}{seconds}\" {segment.ew}"


def text_rotation(line_angle_rad: float) -> float:
    """
    Угол поворота текста, чтобы надпись читалась слева направо/снизу вверх.

    Для линии с углом 90..270° прибавляется pi (разворот на 180°),
    чтобы текст не оказался вверх ногами.
    """
    # Приводим к периоду pi: текст и его разворот на 180° дают одну и ту же
    # надпись, остаётся только направление чтения.
    angle = line_angle_rad % math.pi
    if angle > math.pi / 2.0:
        angle -= math.pi
    return angle


def estimate_text_width(text: str, text_height: float, cfg: dict[str, Any]) -> float:
    """
    Оценка ширины DBTEXT в единицах чертежа (для растяжки и проверок).
    Учитывает, что управляющий код ``%%d`` / ``%%D`` в AutoCAD рендерится одним символом ``°``.
    """
    display = re.sub(r"%%[dpc]", "°", str(text), flags=re.IGNORECASE)
    char_factor = float(cfg.get("char_width_factor", 0.62))
    space_factor = float(cfg.get("space_width_factor", 0.45))
    width = 0.0
    for char in display:
        width += text_height * char_factor * (space_factor if char == " " else 1.0)
    return width


def build_side_label(
    segment: Segment,
    text_height: float,
    side_length: float,
    cfg: dict[str, Any],
) -> tuple[str, bool]:
    """
    Подпись стороны в одну строку: румб + пробелы (для длинных сторон) + длина.

    Если сторона длиннее ``long_side_threshold_ft``, количество пробелов
    подбирается так, чтобы надпись заняла ~``max_label_fill_ratio`` длины
    стороны — текст растягивается вдоль линии, как на топосъёмках.

    Возвращает (текст, была_ли_растяжка).
    """
    use_control = bool(cfg.get("degrees_as_control_code", True))
    bearing = format_bearing(segment, use_control_code=use_control)
    distance = format_distance(segment.distance_ft)
    threshold = float(cfg.get("long_side_threshold_ft", 40.0))
    if side_length <= threshold:
        return f"{bearing} {distance}", False

    target_width = side_length * float(cfg.get("max_label_fill_ratio", 0.85))
    base_width = estimate_text_width(f"{bearing}{distance}", text_height, cfg)
    space_width = text_height * float(cfg.get("char_width_factor", 0.62)) * float(
        cfg.get("space_width_factor", 0.45)
    )
    spaces = 0
    if space_width > 1e-9:
        spaces = int(max(0.0, target_width - base_width) / space_width)
    spaces = max(1, min(spaces, int(cfg.get("max_stretch_spaces", 120))))
    return f"{bearing}{' ' * spaces}{distance}", True


def format_dms_angle(angle_deg: float, use_control_code: bool = True) -> str:
    """Форматирует внутренний угол в градусах-минутах-секундах (напр. 93%%d33'29.5\")."""
    total_tenths = int(
        Decimal(str(abs(angle_deg) * 36000.0)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )
    deg, rem = divmod(total_tenths, 36000)
    minutes, tenths_sec = divmod(rem, 600)
    if tenths_sec % 10 == 0:
        sec_str = f"{tenths_sec // 10:02d}"
    else:
        sec_str = f"{tenths_sec / 10.0:04.1f}" if tenths_sec < 100 else f"{tenths_sec / 10.0:.1f}"
    deg_symbol = "%%d" if use_control_code else "°"
    return f"{deg}{deg_symbol}{minutes:02d}'{sec_str}\""


_DMS_PARSE_RE = re.compile(
    r"(\d+)\s*(?:°|%%[dD]|deg)\s*(\d+)?\s*(?:'|′)?\s*(\d+(?:\.\d+)?)?"
)


def _parse_dms_angle_deg(raw: str) -> float | None:
    match = _DMS_PARSE_RE.search(str(raw))
    if not match:
        try:
            return float(str(raw).strip().rstrip("°"))
        except ValueError:
            return None
    deg = float(match.group(1))
    minutes = float(match.group(2) or 0.0)
    seconds = float(match.group(3) or 0.0)
    return deg + minutes / 60.0 + seconds / 3600.0


def _wrap_course_note(
    note: str,
    note_height: float,
    side_length: float,
    cfg: dict[str, Any],
) -> list[str]:
    """
    Переносит длинную относительную ремарку (напр. ``PARALLEL WITH QUEENS MIDTOWN EXPY``
    на короткой стороне 26.32') на 2 сбалансированные строки, как на эталонных NYC Survey.
    На длинной стороне (90.00') строка остаётся в одну линию.
    """
    clean = " ".join(str(note).split())
    if " " not in clean:
        return [clean]
    width = estimate_text_width(clean, note_height, cfg)
    max_single_width = max(side_length * 0.55, 11.0 * note_height)
    if width <= max_single_width:
        return [clean]

    for prefix in ("PARALLEL WITH ", "AT RIGHT ANGLES TO ", "ALONG "):
        if clean.startswith(prefix) and len(clean) > len(prefix) + 3:
            head = clean[: len(prefix)].strip()
            tail = clean[len(prefix) :].strip()
            if max(
                estimate_text_width(head, note_height, cfg),
                estimate_text_width(tail, note_height, cfg),
            ) <= max(side_length * 0.85, width * 0.68):
                return [head, tail]

    words = clean.split(" ")
    best_pair = [clean]
    best_span = len(clean)
    for split_idx in range(1, len(words)):
        left = " ".join(words[:split_idx])
        right = " ".join(words[split_idx:])
        span = max(len(left), len(right))
        if span < best_span:
            best_span = span
            best_pair = [left, right]
    return best_pair


def build_side_lines(
    segment: Segment,
    text_height: float,
    side_length: float,
    cfg: dict[str, Any],
) -> tuple[list[str], bool]:
    """
    Формирует логические строки подписи стороны:
      * для сторон без компасного румба (``has_bearing=False``, Queens/Brooklyn) —
        ``[*wrapped_course_note, длина]`` (ремарка ближе к границе, длина — снаружи);
      * для узких сторон таунхаусов (``<= stack_short_side_ft``, где ``bearing + distance``
        не помещается в 1 строку, например 13.67' и 14.00') — ``[румб, длина]``;
      * для обычных/длинных сторон — 1 строка из ``build_side_label`` (с растяжкой > 40 ft).
    """
    use_control = bool(cfg.get("degrees_as_control_code", True))
    distance = format_distance(segment.distance_ft)
    if not getattr(segment, "has_bearing", True):
        note = getattr(segment, "course_note", None)
        if note:
            clean_note = str(note).upper()
            if use_control:
                clean_note = clean_note.replace("°", "%%d")
            note_h = text_height * 0.82
            wrapped = _wrap_course_note(clean_note, note_h, side_length, cfg)
            return [*wrapped, distance], False
        return [distance], False

    single_line, stretched = build_side_label(segment, text_height, side_length, cfg)
    if stretched:
        return [single_line], True

    stack_limit = float(cfg.get("stack_short_side_ft", 30.0))
    single_width = estimate_text_width(single_line, text_height, cfg)
    if side_length <= stack_limit or single_width > side_length * 0.75:
        bearing = format_bearing(segment, use_control_code=use_control)
        return [bearing, distance], False

    return [single_line], False


# ---------------------------------------------------------------------------
# Элементы чертежа
# ---------------------------------------------------------------------------


@dataclass
class TextItem:
    """Один DBTEXT: содержимое, точка вставки (центр), поворот (радианы)."""

    text: str
    point: tuple[float, float]
    rotation: float
    kind: str = "side"
    height: float | None = None


@dataclass
class LineItem:
    """Вспомогательный отрезок LINE (например продолжение красной линии улицы)."""

    start: tuple[float, float]
    end: tuple[float, float]
    kind: str = "street-row"


@dataclass
class ArcItem:
    """Дуга ARC в AutoCAD (угловая засечка внутреннего угла участка, обход CCW)."""

    center: tuple[float, float]
    radius: float
    start_angle: float
    end_angle: float
    kind: str = "angle-arc"


@dataclass
class DrawPlan:
    """Полный план отрисовки — результат чистой математики, без COM."""

    description: LegalDescription
    scale: float
    text_height: float
    vertices: list[tuple[float, float]]
    closure: ClosureError
    items: list[TextItem] = field(default_factory=list)
    lines: list[LineItem] = field(default_factory=list)
    boundary_lines: list[LineItem] = field(default_factory=list)
    arcs: list[ArcItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    ccw: bool = True
    pob: tuple[float, float] = (0.0, 0.0)
    is_neighbor: bool = False
    gap_vertex_index: int | None = None
    gap_points: tuple[tuple[float, float], tuple[float, float]] | None = None
    gap_corner_label: str = ""

    @property
    def area_sqft(self) -> float:
        return abs(polygon_signed_area(self.vertices))

    @property
    def centroid(self) -> tuple[float, float]:
        return polygon_centroid(self.vertices)


@dataclass
class DrawResult:
    """Результат отрисовки в AutoCAD — для статуса GUI и лога."""

    plan: DrawPlan
    text_count: int
    layers: dict[str, str]
    handle: str | None = None
    warnings: list[str] = field(default_factory=list)
    boundary_line_count: int = 0

    @property
    def closure(self) -> ClosureError:
        return self.plan.closure

    @property
    def vertices(self) -> list[tuple[float, float]]:
        return self.plan.vertices

    @property
    def boundary_lines(self) -> list[LineItem]:
        return self.plan.boundary_lines

    @property
    def pob(self) -> tuple[float, float]:
        return self.plan.pob

    @property
    def is_neighbor(self) -> bool:
        return self.plan.is_neighbor

    @property
    def area_sqft(self) -> float:
        return self.plan.area_sqft

    @property
    def text_height(self) -> float:
        return self.plan.text_height

    @property
    def scale(self) -> float:
        return self.plan.scale

    @property
    def segment_count(self) -> int:
        return len(self.plan.description.segments)

    def summary(self) -> str:
        role_tag = " [СОСЕДНИЙ УЧАСТОК]" if self.is_neighbor else ""
        layer_summary = (
            f"Слой соседа: {self.layers.get('neighbor_lot', 'NEIGHBOR_LOT')}"
            if self.is_neighbor
            else f"Слои: {self.layers.get('property_line')} / {self.layers.get('boundary_text')}"
        )
        n_segs = self.boundary_line_count or self.segment_count
        lines = [
            f'Масштаб 1"={self.scale:g}\'{role_tag}, высота текста {self.text_height:.3f}',
            f"POB от перекрёстка (0, 0): ({self.pob[0]:+.2f}, {self.pob[1]:+.2f}) ft",
            f"Отрезков границы (LINE): {n_segs}, подписей создано: {self.text_count}, "
            f"площадь ≈ {self.area_sqft:,.0f} sq ft",
            self.closure.report(),
            layer_summary,
        ]
        if self.plan.gap_points is not None:
            g0, g1 = self.plan.gap_points
            lines.append(
                f"Открытый гэп {self.closure.distance:.4f} ft отведён в {self.plan.gap_corner_label}: "
                f"({g0[0]:+.3f}, {g0[1]:+.3f}) ↔ ({g1[0]:+.3f}, {g1[1]:+.3f})"
            )
        for info_note in self.plan.notes:
            lines.append(f"• {info_note}")
        for warning in list(self.plan.warnings) + list(self.warnings):
            lines.append(f"! {warning}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Низкоуровневый COM-хелпер
# ---------------------------------------------------------------------------


def _co_initialize() -> bool:
    try:
        import pythoncom
    except ImportError as exc:  # pragma: no cover - Windows only
        raise AcadConnectionError(
            "pywin32 не установлен. В Windows выполните: pip install pywin32"
        ) from exc
    try:
        pythoncom.CoInitialize()
        return True
    except Exception:  # noqa: BLE001 - уже инициализирован в этом потоке
        return False


def _co_uninitialize() -> None:
    try:
        import pythoncom

        pythoncom.CoUninitialize()
    except Exception:  # noqa: BLE001
        pass


def _variant_array(values: Sequence[float]) -> Any:
    """VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8) — массив double для COM."""
    import pythoncom
    import win32com.client

    return win32com.client.VARIANT(
        pythoncom.VT_ARRAY | pythoncom.VT_R8, [float(value) for value in values]
    )


def _variant_points(points: Iterable[tuple[float, ...]]) -> Any:
    """Плоский массив координат для AddLightWeightPolyline: x1,y1,x2,y2,…"""
    flat: list[float] = []
    for point in points:
        flat.extend(float(value) for value in point)
    return _variant_array(flat)


def _variant_point(point: tuple[float, float], z: float = 0.0) -> Any:
    """Точка [x, y, z] как VARIANT(VT_ARRAY | VT_R8)."""
    return _variant_array((point[0], point[1], z))


def _com_retry(
    func: Callable[..., Any],
    *args: Any,
    attempts: int = 6,
    delay: float = 0.7,
    **kwargs: Any,
) -> Any:
    """
    Повтор вызова при RPC_E_CALL_REJECTED: AutoCAD часто «занят»
    в момент, когда пользователь что-то делает в чертеже.
    """
    try:
        import pywintypes

        com_error: type[BaseException] | tuple[type[BaseException], ...] = (
            pywintypes.com_error
        )
    except ImportError:  # не Windows — вызов всё равно упадёт, но код импортируем
        com_error = ()

    last_error: BaseException | None = None
    for attempt in range(max(1, attempts)):
        try:
            return func(*args, **kwargs)
        except com_error as exc:  # type: ignore[misc]
            last_error = exc
            if attempt == attempts - 1:
                break
            time.sleep(delay)
    raise AcadDrawError(f"AutoCAD не принял команду: {last_error}") from last_error


# ---------------------------------------------------------------------------
# Построитель
# ---------------------------------------------------------------------------


class AcadBuilder:
    """
    Отрисовка Legal Description в активном документе запущенного AutoCAD.

    ::

        builder = AcadBuilder()
        result = builder.draw(description, scale=16.0)
        print(result.summary())
    """

    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        self.cfg = cfg if cfg is not None else load_config()
        self._app: Any | None = None
        self._doc: Any | None = None
        self._com_thread_id: int | None = None
        self._com_owned: bool = False
        self._unit_warning: str | None = None
        self._unit_code: int | None = None
        self._front_warning: str | None = None

    # -- конфигурация -------------------------------------------------------
    @property
    def drawing_cfg(self) -> dict[str, Any]:
        return self.cfg.get("drawing", self.cfg)

    def layer_names(self) -> tuple[str, str]:
        layers = self.cfg.get("layers", {})
        return (
            str(layers.get("property_line", "PROPERTY_LINE")),
            str(layers.get("boundary_text", "BOUNDARY_TEXT_AI")),
        )

    @staticmethod
    def text_height_for_scale(
        scale: float, cfg: dict[str, Any] | None = None
    ) -> float:
        """0.9 * (scale / 16.0) — высота текста под выбранный масштаб."""
        cfg = cfg if cfg is not None else load_config()
        drawing = cfg.get("drawing", cfg)
        base_scale = float(drawing.get("base_scale", 16.0)) or 16.0
        base_height = float(drawing.get("base_text_height", 0.9))
        return base_height * (float(scale) / base_scale)

    # -- чистая раскладка ---------------------------------------------------
    def _outward_normal(
        self, start: tuple[float, float], end: tuple[float, float], ccw: bool
    ) -> tuple[float, float, float]:
        """
        Внешняя нормаль к стороне и её длина.

        У CCW-полигона внутренность слева от направления обхода, поэтому
        внешняя нормаль — правая: (dy, -dx). У CW — наоборот.
        """
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        length = math.hypot(dx, dy)
        if length < 1e-9:
            return 0.0, 0.0, 0.0
        ux, uy = dx / length, dy / length
        if ccw:
            return uy, -ux, length
        return -uy, ux, length

    def _vertex_interior_angle_info(
        self,
        vertex_index: int,
        points: Sequence[tuple[float, float]],
        segments: Sequence[Segment] | None = None,
    ) -> tuple[float, float, float, float, float]:
        """
        Возвращает параметры внутреннего угла в вершине ``vertex_index``:
        ``(interior_deg, bisector_angle_rad, start_arc_rad, end_arc_rad, min_adj_len)``.
        Дуга ``start_arc_rad -> end_arc_rad`` гарантированно обходится против часовой
        стрелки (CCW, конвенция AutoCAD ``AddArc``) строго внутри контура участка.
        При малой невязке округления длин (напр. 0.004 ft на 20.72') использует точные
        угловые направления сегментов, исключая паразитный сдвиг секунд замыкающей хорды.
        """
        two_pi = 2.0 * math.pi
        count = len(points)
        v_norm = vertex_index % count
        curr = points[v_norm]
        prev_pt = points[(v_norm - 1) % count]
        next_pt = points[(v_norm + 1) % count]
        dx1, dy1 = prev_pt[0] - curr[0], prev_pt[1] - curr[1]
        dx2, dy2 = next_pt[0] - curr[0], next_pt[1] - curr[1]
        len1 = math.hypot(dx1, dy1) or 1.0
        len2 = math.hypot(dx2, dy2) or 1.0
        min_len = min(len1, len2)

        a1 = math.atan2(dy1, dx1) % two_pi
        a2 = math.atan2(dy2, dx2) % two_pi
        if segments is not None and len(segments) == count:
            in_dx, in_dy = segments[(v_norm - 1) % count].vector
            out_dx, out_dy = segments[v_norm].vector
            if math.hypot(in_dx, in_dy) > 1e-9 and math.hypot(out_dx, out_dy) > 1e-9:
                seg_a1 = math.atan2(-in_dy, -in_dx) % two_pi
                seg_a2 = math.atan2(out_dy, out_dx) % two_pi
                diff1 = abs(((seg_a1 - a1 + math.pi) % two_pi) - math.pi)
                diff2 = abs(((seg_a2 - a2 + math.pi) % two_pi) - math.pi)
                if diff1 < math.radians(0.5) and diff2 < math.radians(0.5):
                    a1, a2 = seg_a1, seg_a2

        sweep_12 = (a2 - a1) % two_pi
        mid_12 = a1 + 0.5 * sweep_12
        probe_step = min(0.35, 0.1 * min_len)
        probe = (
            curr[0] + math.cos(mid_12) * probe_step,
            curr[1] + math.sin(mid_12) * probe_step,
        )
        if point_in_polygon(probe, points):
            start_arc, end_arc, sweep = a1, a2, sweep_12
            bis_angle = mid_12
        else:
            sweep_21 = (a1 - a2) % two_pi
            start_arc, end_arc, sweep = a2, a1, sweep_21
            bis_angle = a2 + 0.5 * sweep_21
        return math.degrees(sweep), bis_angle, start_arc, end_arc, min_len

    def _interior_angle_point(
        self,
        vertex_index: int,
        points: Sequence[tuple[float, float]],
        step: float,
    ) -> tuple[float, float]:
        """Точка внутри угла многоугольника по биссектрисе (для подписи 93°33'29.5")."""
        count = len(points)
        curr = points[vertex_index % count]
        _, bis_angle, _, _, _ = self._vertex_interior_angle_info(vertex_index, points)
        for factor in (1.0, 0.72, 0.5, 0.32, 0.16):
            cand = (
                curr[0] + math.cos(bis_angle) * (step * factor),
                curr[1] + math.sin(bis_angle) * (step * factor),
            )
            if point_in_polygon(cand, points):
                return cand
        return (
            curr[0] + math.cos(bis_angle) * step,
            curr[1] + math.sin(bis_angle) * step,
        )

    def _side_items(
        self,
        description: LegalDescription,
        points: Sequence[tuple[float, float]],
        text_height: float,
        ccw: bool,
        warnings: list[str],
        front_index: int | None = None,
        boundary_lines: Sequence[LineItem] | None = None,
    ) -> tuple[list[TextItem], list[ArcItem], dict[int, float]]:
        cfg = self.drawing_cfg
        offset = float(cfg.get("text_offset_factor", 1.8)) * text_height
        line_gap = float(cfg.get("stacked_line_gap_factor", 1.9)) * text_height
        use_control = bool(cfg.get("degrees_as_control_code", True))
        segments = description.segments
        items: list[TextItem] = []
        arcs: list[ArcItem] = []
        side_band_extra: dict[int, float] = {}
        count = len(points)

        for index, segment in enumerate(segments):
            if boundary_lines is not None and index < len(boundary_lines):
                start = boundary_lines[index].start
                end = boundary_lines[index].end
            else:
                start = points[index % count]
                end = points[(index + 1) % count]
            nx, ny, length = self._outward_normal(start, end, ccw)
            if length < 1e-9:
                warnings.append(f"сторона #{index + 1}: нулевая длина — пропущена")
                continue

            lines, _ = build_side_lines(segment, text_height, length, cfg)
            angle = math.atan2(end[1] - start[1], end[0] - start[0])
            rot = text_rotation(angle)
            up_x, up_y = -math.sin(rot), math.cos(rot)
            up_dot_n = up_x * nx + up_y * ny
            midpoint = ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)

            has_bearing = bool(getattr(segment, "has_bearing", True))
            if not has_bearing:
                # Эталон NYC Survey без компасных румбов (Queens / Brooklyn):
                # ремарка (PARALLEL WITH / AT RIGHT ANGLES) — мелким кеглем ближе к границе,
                # длина (26.32', 90.00', 90.17', 20.72') — крупнее снаружи ремарки.
                dist_h = round(text_height * 1.12, 4)
                note_h = round(text_height * 0.82, 4)
                note_gap = 1.48 * note_h
                note_lines = lines[:-1]
                dist_text = lines[-1]
                max_line_width = max(
                    [estimate_text_width(dist_text, dist_h, cfg)]
                    + [estimate_text_width(nl, note_h, cfg) for nl in note_lines]
                )
                if max_line_width > length > 0:
                    warnings.append(
                        f"подпись стороны #{index + 1} (≈{max_line_width:.1f} ед.) длиннее самой "
                        f"стороны ({length:.1f} ft) — уменьшите масштаб/высоту текста"
                    )
                if note_lines:
                    ordered_notes = (
                        list(reversed(note_lines)) if up_dot_n >= 0.0 else list(note_lines)
                    )
                    for r_idx, nl_text in enumerate(ordered_notes):
                        r_off = offset + r_idx * note_gap
                        suffix = "-note" if r_idx == 0 else f"-note-{r_idx + 1}"
                        items.append(
                            TextItem(
                                text=nl_text,
                                point=(midpoint[0] + nx * r_off, midpoint[1] + ny * r_off),
                                rotation=rot,
                                kind=f"side-{index + 1}{suffix}",
                                height=note_h,
                            )
                        )
                    dist_off = offset + (len(ordered_notes) - 1) * note_gap + line_gap
                    side_band_extra[index] = dist_off - offset
                else:
                    dist_off = offset
                    side_band_extra[index] = 0.0

                items.append(
                    TextItem(
                        text=dist_text,
                        point=(midpoint[0] + nx * dist_off, midpoint[1] + ny * dist_off),
                        rotation=rot,
                        kind=f"side-{index + 1}",
                        height=dist_h,
                    )
                )
            else:
                max_line_width = max(
                    estimate_text_width(line_text, text_height, cfg) for line_text in lines
                )
                if max_line_width > length > 0:
                    warnings.append(
                        f"подпись стороны #{index + 1} (≈{max_line_width:.1f} ед.) длиннее самой "
                        f"стороны ({length:.1f} ft) — уменьшите масштаб/высоту текста"
                    )
                if len(lines) == 1:
                    side_band_extra[index] = 0.0
                    items.append(
                        TextItem(
                            text=lines[0],
                            point=(midpoint[0] + nx * offset, midpoint[1] + ny * offset),
                            rotation=rot,
                            kind=f"side-{index + 1}",
                        )
                    )
                else:
                    # Двухстрочная короткая сторона с румбом (13.67', 14.00'):
                    # верхняя по чтению строка — румб, нижняя по чтению — длина.
                    bearing_text, dist_text = lines[0], lines[1]
                    if up_dot_n >= 0.0:
                        dist_off = offset
                        brg_off = offset + line_gap
                    else:
                        brg_off = offset
                        dist_off = offset + line_gap
                    side_band_extra[index] = line_gap
                    items.append(
                        TextItem(
                            text=dist_text,
                            point=(midpoint[0] + nx * dist_off, midpoint[1] + ny * dist_off),
                            rotation=rot,
                            kind=f"side-{index + 1}",
                        )
                    )
                    items.append(
                        TextItem(
                            text=bearing_text,
                            point=(midpoint[0] + nx * brg_off, midpoint[1] + ny * brg_off),
                            rotation=rot,
                            kind=f"side-{index + 1}-note",
                        )
                    )

        # -- внутренние углы (явные из описания + автовывод на участках без румбов) --
        used_vertices: set[int] = set()

        def add_corner_angle(v_idx: int, raw_label: str, kind_idx: int) -> None:
            v_norm = v_idx % count
            if v_norm in used_vertices:
                return
            used_vertices.add(v_norm)
            _, bis_angle, start_arc, end_arc, min_len = self._vertex_interior_angle_info(
                v_norm, points, segments=segments
            )
            ia_text = str(raw_label).replace("°", "%%d") if use_control else str(raw_label)
            arc_radius = min(2.5 * text_height, 0.20 * min_len)
            text_step = min(7.8 * text_height, 0.44 * min_len)
            curr = points[v_norm]
            ia_point = (
                curr[0] + math.cos(bis_angle) * text_step,
                curr[1] + math.sin(bis_angle) * text_step,
            )
            for factor in (1.0, 0.75, 0.55, 0.38, 0.22):
                cand = (
                    curr[0] + math.cos(bis_angle) * (text_step * factor),
                    curr[1] + math.sin(bis_angle) * (text_step * factor),
                )
                if point_in_polygon(cand, points):
                    ia_point = cand
                    break
            items.append(
                TextItem(
                    text=ia_text,
                    point=ia_point,
                    rotation=text_rotation(bis_angle),
                    kind=f"angle-{kind_idx}",
                    height=round(text_height * 0.88, 4),
                )
            )
            if arc_radius > 0.25 * text_height:
                arcs.append(
                    ArcItem(
                        center=curr,
                        radius=round(arc_radius, 4),
                        start_angle=start_arc,
                        end_angle=end_arc,
                        kind=f"angle-arc-{kind_idx}",
                    )
                )

        for index, segment in enumerate(segments):
            ia_raw = getattr(segment, "interior_angle", None)
            if not ia_raw:
                continue
            v_start = index % count
            v_end = (index + 1) % count
            parsed_deg = _parse_dms_angle_deg(str(ia_raw))
            chosen_v = v_start
            if parsed_deg is not None:
                deg_start = self._vertex_interior_angle_info(
                    v_start, points, segments=segments
                )[0]
                deg_end = self._vertex_interior_angle_info(
                    v_end, points, segments=segments
                )[0]
                if abs(deg_end - parsed_deg) + 0.02 < abs(deg_start - parsed_deg):
                    chosen_v = v_end
            add_corner_angle(chosen_v, str(ia_raw), index + 1)

        # Если участок задан без компасных румбов (как 63-01 Borden Ave при Vision-разборе),
        # а Vision LLM пропустил строку interior_angle, автоматически проставляем базовый
        # косой угол (напр. 93°33'29.5" на пересечении улиц), чтобы чертёж был математически полным.
        if (
            count >= 3
            and not used_vertices
            and all(not getattr(s, "has_bearing", True) for s in segments)
        ):
            cross_idx = getattr(description, "cross_street_side_index", None)
            street_edges: set[int] = set()
            if front_index is not None:
                street_edges.add(front_index % count)
            if cross_idx is not None and 0 <= int(cross_idx) < count:
                street_edges.add(int(cross_idx))
            elif (
                front_index is not None
                and description.cross_street
                and description.distance_to_corner_ft is not None
                and description.distance_to_corner_ft <= 1e-6
            ):
                street_edges.add((front_index - 1) % count)

            oblique_candidates: list[tuple[int, int, int, float]] = []
            for v_idx in range(count):
                deg_v = self._vertex_interior_angle_info(
                    v_idx, points, segments=segments
                )[0]
                if 0.08 <= abs(deg_v - 90.0) <= 88.0:
                    inc_edges = {(v_idx - 1) % count, v_idx % count}
                    street_hits = len(inc_edges & street_edges)
                    pob_bonus = 1 if v_idx == 0 else 0
                    oblique_candidates.append((street_hits, pob_bonus, -v_idx, deg_v))
            if oblique_candidates:
                oblique_candidates.sort(reverse=True)
                best_hits, _, neg_v, best_deg = oblique_candidates[0]
                best_v = -neg_v
                label_str = format_dms_angle(best_deg, use_control_code=use_control)
                add_corner_angle(best_v, label_str, best_v + 1)

        return items, arcs, side_band_extra

    def _front_side_index(
        self,
        description: LegalDescription,
        points: Sequence[tuple[float, float]],
        ccw: bool,
    ) -> int | None:
        """
        Номер стороны, выходящей на фасадную улицу.

        1. Явный ``front_segment_index`` из документа/LLM (критично для узких
           таунхаусов NYC 13'–14' × 100'–125' при наклонной сетке кварталов).
        2. Если задан ``front_side`` (N/S/E/W/NE/NW/SE/SW — куда смотрит фасад),
           выбираем сторону с максимальной сонаправленностью внешней нормали (``score``),
           исключая тыльные/боковые границы ``PARALLEL WITH`` / ``AT RIGHT ANGLES``.
        3. Иначе — сторона из конфига (``front_side_index``), по умолчанию 0.
        """
        explicit_idx = getattr(description, "front_segment_index", None)
        if explicit_idx is not None and 0 <= int(explicit_idx) < len(description.segments):
            return int(explicit_idx)

        count = len(points)
        front_side = getattr(description, "front_side", None)
        diag = math.sqrt(0.5)
        direction_vectors: dict[str, tuple[float, float]] = {
            "N": (0.0, 1.0),
            "S": (0.0, -1.0),
            "E": (1.0, 0.0),
            "W": (-1.0, 0.0),
            "NE": (diag, diag),
            "NW": (-diag, diag),
            "SE": (diag, -diag),
            "SW": (-diag, -diag),
        }
        if str(front_side) in direction_vectors:
            target = direction_vectors[str(front_side)]
            primary_candidates: list[tuple[float, float, int]] = []
            fallback_candidates: list[tuple[float, float, int]] = []
            for index, seg in enumerate(description.segments):
                nx, ny, length = self._outward_normal(
                    points[index % count], points[(index + 1) % count], ccw
                )
                if length < 1e-9:
                    continue
                score = nx * target[0] + ny * target[1]
                if score > 0.35:
                    note_up = str(getattr(seg, "course_note", "") or "").upper()
                    is_relative_back = (
                        "PARALLEL WITH" in note_up or "AT RIGHT ANGLES" in note_up
                    )
                    if is_relative_back:
                        fallback_candidates.append((score, length, index))
                    else:
                        primary_candidates.append((score, length, index))
            candidates = primary_candidates or fallback_candidates
            if candidates:
                max_score = max(c[0] for c in candidates)
                top = [c for c in candidates if c[0] >= max_score - 0.08]
                top.sort(key=lambda item: (item[1], item[0]), reverse=True)
                return top[0][2]
            self._front_warning = (
                f"фасадная сторона не найдена (front_side={front_side}) — "
                "использую сторону из настроек"
            )
            return self._configured_front_index(description)

        return self._configured_front_index(description)

    def _configured_front_index(self, description: LegalDescription) -> int:
        index = int(self.drawing_cfg.get("front_side_index", 0) or 0)
        if not 0 <= index < len(description.segments):
            return 0
        return index

    def _street_items(
        self,
        description: LegalDescription,
        points: Sequence[tuple[float, float]],
        text_height: float,
        ccw: bool,
        warnings: list[str],
        side_band_extra: dict[int, float] | None = None,
        front_index: int | None = None,
        boundary_lines: Sequence[LineItem] | None = None,
    ) -> tuple[list[TextItem], list[LineItem]]:
        cfg = self.drawing_cfg
        items: list[TextItem] = []
        row_lines: list[LineItem] = []
        draw_ext = bool(cfg.get("draw_street_extensions", True))

        if front_index is None:
            front_index = self._front_side_index(description, points, ccw)
        if front_index is None:
            warnings.append(
                "не удалось определить фасадную сторону — подписи улиц не нанесены"
            )
            return items, row_lines

        count = len(points)
        if boundary_lines is not None and front_index < len(boundary_lines):
            start = boundary_lines[front_index].start
            end = boundary_lines[front_index].end
        else:
            start = points[front_index]
            end = points[(front_index + 1) % count]
        nx, ny, length = self._outward_normal(start, end, ccw)
        if length < 1e-9:
            warnings.append("фасадная сторона имеет нулевую длину — подписи улиц пропущены")
            return items, row_lines

        ux = (end[0] - start[0]) / length
        uy = (end[1] - start[1]) / length
        angle = math.atan2(end[1] - start[1], end[0] - start[0])
        rot = text_rotation(angle)
        up_x, up_y = -math.sin(rot), math.cos(rot)
        up_dot_n = up_x * nx + up_y * ny
        midpoint = ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)

        street_h = round(float(cfg.get("street_height_factor", 1.9)) * text_height, 4)
        street_note_h = round(text_height * 1.05, 4)
        base_street_offset = float(cfg.get("street_offset_factor", 6.5)) * text_height
        front_extra = (side_band_extra or {}).get(front_index, 0.0)
        street_offset = base_street_offset + front_extra

        if description.main_street:
            street_note = getattr(description, "main_street_note", None)
            if street_note:
                note_step = 2.05 * text_height
                if up_dot_n < 0.0:
                    main_off = street_offset
                    sub_off = street_offset + note_step
                else:
                    sub_off = street_offset
                    main_off = street_offset + note_step
                items.append(
                    TextItem(
                        text=description.main_street.upper(),
                        point=(midpoint[0] + nx * main_off, midpoint[1] + ny * main_off),
                        rotation=rot,
                        kind="main-street",
                        height=street_h,
                    )
                )
                items.append(
                    TextItem(
                        text=str(street_note).upper(),
                        point=(midpoint[0] + nx * sub_off, midpoint[1] + ny * sub_off),
                        rotation=rot,
                        kind="main-street-note",
                        height=street_note_h,
                    )
                )
            else:
                items.append(
                    TextItem(
                        text=description.main_street.upper(),
                        point=(
                            midpoint[0] + nx * street_offset,
                            midpoint[1] + ny * street_offset,
                        ),
                        rotation=rot,
                        kind="main-street",
                        height=street_h,
                    )
                )
        else:
            warnings.append("main_street пуст — название фасадной улицы не нанесено")

        if not description.cross_street:
            if draw_ext and description.main_street:
                ext = max(12.0 * text_height, min(22.0 * text_height, 0.45 * length))
                row_lines.append(
                    LineItem(
                        start=(start[0] - ux * ext, start[1] - uy * ext),
                        end=start,
                        kind="main-street-row-1",
                    )
                )
                row_lines.append(
                    LineItem(
                        start=end,
                        end=(end[0] + ux * ext, end[1] + uy * ext),
                        kind="main-street-row-2",
                    )
                )
            return items, row_lines

        cross_idx = getattr(description, "cross_street_side_index", None)
        is_corner_lot = (
            cross_idx is not None
            or (
                description.distance_to_corner_ft is not None
                and description.distance_to_corner_ft <= 1e-6
            )
        )
        if is_corner_lot:
            side_idx = (
                int(cross_idx)
                if cross_idx is not None and 0 <= int(cross_idx) < count
                else (front_index - 1) % count
            )
            c_start = points[side_idx]
            c_end = points[(side_idx + 1) % count]
            cnx, cny, c_len = self._outward_normal(c_start, c_end, ccw)
            if c_len > 1e-9:
                c_angle = math.atan2(c_end[1] - c_start[1], c_end[0] - c_start[0])
                c_mid = ((c_start[0] + c_end[0]) / 2.0, (c_start[1] + c_end[1]) / 2.0)
                c_extra = (side_band_extra or {}).get(side_idx, 0.0)
                c_offset = base_street_offset + c_extra
                items.append(
                    TextItem(
                        text=description.cross_street.upper(),
                        point=(c_mid[0] + cnx * c_offset, c_mid[1] + cny * c_offset),
                        rotation=text_rotation(c_angle),
                        kind="cross-street",
                        height=street_h,
                    )
                )
                if draw_ext:
                    f_ext = max(12.0 * text_height, min(20.0 * text_height, 0.35 * length))
                    row_lines.append(
                        LineItem(
                            start=(start[0] - ux * f_ext, start[1] - uy * f_ext),
                            end=start,
                            kind="main-street-row-1",
                        )
                    )
                    row_lines.append(
                        LineItem(
                            start=end,
                            end=(end[0] + ux * f_ext, end[1] + uy * f_ext),
                            kind="main-street-row-2",
                        )
                    )
                    cux = (c_end[0] - c_start[0]) / c_len
                    cuy = (c_end[1] - c_start[1]) / c_len
                    c_ext = max(12.0 * text_height, min(20.0 * text_height, 0.22 * c_len))
                    row_lines.append(
                        LineItem(
                            start=(c_start[0] - cux * c_ext, c_start[1] - cuy * c_ext),
                            end=c_start,
                            kind="cross-street-row-1",
                        )
                    )
                    row_lines.append(
                        LineItem(
                            start=c_end,
                            end=(c_end[0] + cux * c_ext, c_end[1] + cuy * c_ext),
                            kind="cross-street-row-2",
                        )
                    )
                return items, row_lines

        # Рядовой участок: привязка вдоль фасадной улицы от реального перекрёстка (0.0, 0.0)
        # до ближайшего к углу конца фасадной границы (near_pt), либо компактная выноска
        # при сверхдальней привязке (> max_full_tie_line_ft, напр. 1,542.23').
        tie_note = getattr(description, "tie_note", None)
        dist_val = float(description.distance_to_corner_ft or 0.0)
        has_tie_label = bool(tie_note) or dist_val > 1e-6
        dist_label = ""
        dist_width = 0.0
        if has_tie_label:
            dist_label = (
                str(tie_note).upper()
                if tie_note
                else f"{format_distance(dist_val)} TO CORNER"
            )
            dist_width = estimate_text_width(dist_label, text_height, cfg)

        d_start = math.hypot(start[0], start[1])
        d_end = math.hypot(end[0], end[1])
        if d_start <= d_end + 1e-6:
            near_pt, far_pt = start, end
        else:
            near_pt, far_pt = end, start

        # Единичный вектор вдоль красной линии улицы ОТ near_pt В СТОРОНУ перекрёстка (0, 0).
        if (-near_pt[0]) * ux + (-near_pt[1]) * uy >= 0.0:
            tux, tuy = ux, uy
        else:
            tux, tuy = -ux, -uy
        fux, fuy = -tux, -tuy

        gap = float(cfg.get("corner_label_gap_factor", 2.4)) * text_height
        min_tie_span = (
            max(dist_width + 9.0 * text_height, 18.0 * text_height)
            if has_tie_label
            else max(gap + 5.0 * text_height, 11.0 * text_height)
        )
        d_near = math.hypot(near_pt[0], near_pt[1])
        perp_origin_err = abs(near_pt[0] * nx + near_pt[1] * ny)
        max_full_tie_ft = float(cfg.get("max_full_tie_line_ft", 800.0))

        if perp_origin_err <= 1.5 and min_tie_span <= d_near <= max_full_tie_ft:
            corner_pt = (0.0, 0.0)
            tie_span = d_near
        else:
            tie_span = min_tie_span
            corner_pt = (near_pt[0] + tux * tie_span, near_pt[1] + tuy * tie_span)

        if has_tie_label:
            tie_text_offset = float(cfg.get("corner_distance_offset_factor", 1.9)) * text_height
            tie_mid = (
                near_pt[0] + tux * (0.5 * tie_span) + nx * tie_text_offset,
                near_pt[1] + tuy * (0.5 * tie_span) + ny * tie_text_offset,
            )
            items.append(
                TextItem(
                    text=dist_label,
                    point=tie_mid,
                    rotation=rot,
                    kind="cross-street-distance",
                )
            )

        cross_along_shift = 2.8 * text_height
        cross_normal_shift = 0.65 * street_offset
        items.append(
            TextItem(
                text=description.cross_street.upper(),
                point=(
                    corner_pt[0] + tux * cross_along_shift + nx * cross_normal_shift,
                    corner_pt[1] + tuy * cross_along_shift + ny * cross_normal_shift,
                ),
                rotation=text_rotation(angle + math.pi / 2.0),
                kind="cross-street",
                height=street_h,
            )
        )

        if draw_ext:
            far_ext = max(10.0 * text_height, min(18.0 * text_height, 0.35 * length))
            row_lines.append(
                LineItem(
                    start=corner_pt,
                    end=near_pt,
                    kind="main-street-row-tie",
                )
            )
            row_lines.append(
                LineItem(
                    start=far_pt,
                    end=(far_pt[0] + fux * far_ext, far_pt[1] + fuy * far_ext),
                    kind="main-street-row-2",
                )
            )
            row_lines.append(
                LineItem(
                    start=(
                        corner_pt[0] - nx * (4.5 * text_height),
                        corner_pt[1] - ny * (4.5 * text_height),
                    ),
                    end=(
                        corner_pt[0] + nx * (street_offset + 5.0 * text_height),
                        corner_pt[1] + ny * (street_offset + 5.0 * text_height),
                    ),
                    kind="cross-street-row",
                )
            )
        return items, row_lines

    def _neighbor_label_item(
        self,
        description: LegalDescription,
        vertices: Sequence[tuple[float, float]],
        text_height: float,
        front_index: int | None,
        neighbor_label: str | None = None,
    ) -> TextItem:
        """Компактная подпись соседнего участка строго в его геометрическом центроиде."""
        cfg = self.drawing_cfg
        if neighbor_label and str(neighbor_label).strip():
            label = str(neighbor_label).strip()
        else:
            dist = float(description.distance_to_corner_ft or 0.0)
            if dist > 1e-6:
                d_str = (
                    f"{int(round(dist))}'"
                    if abs(dist - round(dist)) < 1e-3
                    else format_distance(dist)
                )
                label = f"NEIGHBOR ({d_str} from corner)"
            elif description.cross_street:
                label = "NEIGHBOR (CORNER)"
            else:
                label = "NEIGHBOR"

        center = polygon_centroid(vertices)
        count = len(vertices)
        longest_len = 0.0
        longest_angle = 0.0
        for idx in range(count):
            p1 = vertices[idx]
            p2 = vertices[(idx + 1) % count]
            elen = math.dist(p1, p2)
            if elen > longest_len:
                longest_len = elen
                longest_angle = math.atan2(p2[1] - p1[1], p2[0] - p1[0])

        chosen_angle = longest_angle
        chosen_span = longest_len
        if front_index is not None and 0 <= front_index < count:
            fp1 = vertices[front_index]
            fp2 = vertices[(front_index + 1) % count]
            front_len = math.dist(fp1, fp2)
            raw_w = estimate_text_width(label, text_height, cfg)
            if raw_w <= 0.82 * front_len:
                chosen_angle = math.atan2(fp2[1] - fp1[1], fp2[0] - fp1[0])
                chosen_span = front_len

        lbl_h = text_height
        est_w = estimate_text_width(label, lbl_h, cfg)
        if chosen_span > 1e-6 and est_w > 0.82 * chosen_span:
            lbl_h = max(0.35 * text_height, lbl_h * (0.82 * chosen_span / est_w))

        return TextItem(
            text=label,
            point=center,
            rotation=text_rotation(chosen_angle),
            kind="neighbor-label",
            height=lbl_h,
        )

    def plan(
        self,
        description: LegalDescription,
        scale: float | None = None,
        text_height: float | None = None,
        *,
        is_neighbor: bool = False,
        neighbor_label: str | None = None,
    ) -> DrawPlan:
        """Считает геометрию отрезков от перекрёстка (0.0, 0.0) и раскладку подписей."""
        cfg = self.drawing_cfg
        scale = float(scale if scale is not None else cfg.get("base_scale", 16.0))
        height = (
            float(text_height)
            if text_height is not None
            else self.text_height_for_scale(scale, self.cfg)
        )

        pob = compute_pob_point(description)
        orig_segments = list(description.segments)
        raw_traverse = build_polygon_points(orig_segments, start=pob)
        if len(raw_traverse) < 4:
            raise AcadDrawError(
                f"для замкнутого участка нужно минимум 3 стороны, получено {len(orig_segments)}"
            )
        raw_vertices = raw_traverse[:-1]
        warnings: list[str] = []
        notes: list[str] = []

        signed_area = polygon_signed_area(raw_vertices)
        ccw = signed_area > 0.0
        self._front_warning = None
        front_index = self._front_side_index(description, raw_vertices, ccw)

        # Исключение №1: аналитическое закрытие прямоугольной трапеции с точной
        # перпендикулярной глубиной к улице (напр. 63-01 Borden Ave: 20.72', 90.00' под 90°).
        geom_segments, trapezoid_note = resolve_analytical_right_trapezoid(
            description, orig_segments, front_index
        )
        if trapezoid_note:
            notes.append(trapezoid_note)

        traverse = build_polygon_points(geom_segments, start=pob)
        closure = closure_error(traverse)

        leave_gap_open = bool(cfg.get("leave_gap_open", True))
        rounding_tol = float(cfg.get("rounding_gap_tolerance_ft", CLOSURE_TOLERANCE_FT))
        gap_vertex_index: int | None = None
        gap_corner_label = ""

        if closure.distance > 1e-7 and leave_gap_open:
            gap_vertex_index, gap_corner_label = select_gap_vertex_index(
                description, traverse[:-1], front_index, ccw
            )
            boundary_lines, vertices, gap_points = build_boundary_lines(
                geom_segments,
                pob=pob,
                gap_vertex_index=gap_vertex_index,
                leave_gap_open=True,
            )
            notes.append(
                f"Контур построен отдельными отрезками LINE с сохранением точных длин и углов; "
                f"открытый гэп {closure.distance:.4f} ft отведён в {gap_corner_label}"
            )
        else:
            boundary_lines, vertices, gap_points = build_boundary_lines(
                geom_segments,
                pob=pob,
                gap_vertex_index=0,
                leave_gap_open=False,
            )

        if closure.distance > rounding_tol:
            if leave_gap_open:
                warnings.append(
                    f"описание не замыкается точно: невязка {closure.distance:.3f} ft — "
                    f"длины и углы сохранены строго, открытый гэп отведён в {gap_corner_label}"
                )
            else:
                warnings.append(
                    f"описание не замыкается точно: невязка {closure.distance:.3f} ft — "
                    "контур замкнут по замыкающей линии"
                )
            diag_hint = _diagnose_unresolved_gap(geom_segments, closure)
            if diag_hint:
                warnings.append(diag_hint)
        if closure.distance > 5.0:
            warnings.append(
                "невязка больше 5 ft — проверьте румбы и длины в исходном описании"
            )

        signed_area = polygon_signed_area(vertices)
        if abs(signed_area) < 0.5:
            warnings.append(
                "площадь контура близка к нулю — проверьте направления и длины сторон"
            )
        elif _polygon_self_intersects(vertices):
            warnings.append(
                "контур самопересекается — проверьте порядок обхода и знаки румбов"
            )

        ccw = signed_area > 0.0
        if is_neighbor:
            items = [
                self._neighbor_label_item(
                    description,
                    vertices,
                    height,
                    front_index=front_index,
                    neighbor_label=neighbor_label,
                )
            ]
            row_lines: list[LineItem] = []
            arcs: list[ArcItem] = []
        else:
            items, arcs, side_band_extra = self._side_items(
                description,
                vertices,
                height,
                ccw,
                warnings,
                front_index=front_index,
                boundary_lines=boundary_lines,
            )
            street_texts, row_lines = self._street_items(
                description,
                vertices,
                height,
                ccw,
                warnings,
                side_band_extra=side_band_extra,
                front_index=front_index,
                boundary_lines=boundary_lines,
            )
            items.extend(street_texts)
            if self._front_warning:
                warnings.append(self._front_warning)

        return DrawPlan(
            description=description,
            scale=scale,
            text_height=height,
            vertices=vertices,
            closure=closure,
            items=items,
            lines=row_lines,
            boundary_lines=boundary_lines,
            arcs=arcs,
            warnings=warnings,
            notes=notes,
            ccw=ccw,
            pob=pob,
            is_neighbor=bool(is_neighbor),
            gap_vertex_index=gap_vertex_index,
            gap_points=gap_points,
            gap_corner_label=gap_corner_label,
        )

    # -- подключение к AutoCAD ---------------------------------------------
    def connect(self) -> tuple[Any, Any]:
        """
        Возвращает (Application, ActiveDocument) уже запущенного AutoCAD.

        Если приложение/документ были внедрены снаружи (тесты, повторная
        отрисовка), соединение переиспользуется, а проверка единиц чертежа
        всё равно выполняется. При вызове из другого потока COM-прокси
        переподключается в текущем потоке, чтобы избежать RPC_E_WRONG_THREAD.
        """
        cur_tid = threading.get_ident()
        if self._app is not None and self._doc is not None:
            if not self._com_owned or self._com_thread_id == cur_tid:
                if self._unit_warning is None:
                    self._warn_about_units(self._doc)
                return self._app, self._doc
            self._app = None
            self._doc = None

        co_inited = _co_initialize()
        try:
            import win32com.client
        except ImportError as exc:  # pragma: no cover
            if co_inited:
                _co_uninitialize()
            raise AcadConnectionError(
                "pywin32 не установлен. В Windows выполните: pip install pywin32"
            ) from exc

        try:
            app = win32com.client.GetActiveObject(ACAD_PROGID)
        except Exception as exc:  # noqa: BLE001
            if co_inited:
                _co_uninitialize()
            raise AcadConnectionError(
                "Не удалось подключиться к запущенному AutoCAD.\n"
                "1) Откройте AutoCAD и нужный DWG (не стартовый экран);\n"
                "2) Дождитесь полной загрузки чертежа;\n"
                f"3) Повторите попытку. Детали: {exc}"
            ) from exc

        try:
            doc = app.ActiveDocument
        except Exception as exc:  # noqa: BLE001
            if co_inited:
                _co_uninitialize()
            raise AcadConnectionError(
                "AutoCAD запущен, но активный документ недоступен — откройте DWG."
            ) from exc
        if doc is None:
            if co_inited:
                _co_uninitialize()
            raise AcadConnectionError(
                "AutoCAD запущен, но активный документ недоступен — откройте DWG."
            )
        self._app, self._doc = app, doc
        self._com_thread_id = cur_tid
        self._com_owned = bool(co_inited)
        self._warn_about_units(doc)
        return app, doc

    def _warn_about_units(self, doc: Any) -> None:
        self._unit_warning = None
        self._unit_code = None
        try:
            insunits = int(doc.GetVariable("INSUNITS"))
        except Exception:  # noqa: BLE001
            return
        self._unit_code = insunits
        if insunits not in (0, 2):
            name = _INSUNITS_NAMES.get(insunits, f"код {insunits}")
            self._unit_warning = (
                f"единицы чертежа — {name} (INSUNITS={insunits}), а координаты "
                "считаются в футах: установите INSUNITS = Feet"
            )

    # -- слои и стиль -------------------------------------------------------
    def _ensure_layer(self, doc: Any, name: str, color: int) -> str:
        layer = None
        try:
            layer = doc.Layers.Item(name)
        except Exception:  # noqa: BLE001
            try:
                layer = _com_retry(doc.Layers.Add, name)
            except Exception:  # noqa: BLE001
                try:
                    layer = doc.Layers.Item(name)
                except Exception:  # noqa: BLE001
                    return "0"
        if layer is None:
            return "0"
        try:
            layer.LayerOn = True
            layer.Freeze = False
            layer.Lock = False
            layer.color = color
        except Exception:  # noqa: BLE001 - часть свойств может быть недоступна
            pass
        return name

    def _ensure_text_style(self, doc: Any) -> str:
        style_name = str(self.drawing_cfg.get("text_style") or "AI_BOUNDARY")
        try:
            styles = doc.TextStyles
            try:
                style_obj = styles.Item(style_name)
            except Exception:  # noqa: BLE001
                style_obj = _com_retry(styles.Add, style_name)
            # В AutoCAD ActiveX SHX-шрифт задаётся свойством fontFile (SetFont — только для TrueType).
            try:
                style_obj.fontFile = "romans.shx"
                try:
                    style_obj.BigFontFile = ""
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001
                try:
                    style_obj.SetFont("romans.shx", False, False, 0, 0)
                except Exception:  # noqa: BLE001
                    pass
            try:
                # Сбрасываем фиксированную высоту стиля в 0.0, чтобы работала высота из AddText.
                style_obj.Height = 0.0
                style_obj.Width = 1.0
            except Exception:  # noqa: BLE001
                pass
            return style_name
        except Exception:  # noqa: BLE001
            return "Standard"

    # -- создание объектов --------------------------------------------------
    def _add_polyline(
        self,
        model_space: Any,
        points: Sequence[tuple[float, float]],
        layer: str,
        *,
        closed: bool = True,
    ) -> Any:
        polyline = _com_retry(model_space.AddLightWeightPolyline, _variant_points(points))
        polyline.Layer = layer
        polyline.Closed = bool(closed)
        return polyline

    def _add_line(
        self, model_space: Any, item: LineItem, layer: str
    ) -> Any:
        line_obj = _com_retry(
            model_space.AddLine,
            _variant_point(item.start),
            _variant_point(item.end),
        )
        line_obj.Layer = layer
        return line_obj

    def _add_arc(
        self, model_space: Any, item: ArcItem, layer: str
    ) -> Any:
        arc_obj = _com_retry(
            model_space.AddArc,
            _variant_point(item.center),
            float(item.radius),
            float(item.start_angle),
            float(item.end_angle),
        )
        arc_obj.Layer = layer
        return arc_obj

    def _add_text(
        self,
        model_space: Any,
        item: TextItem,
        default_height: float,
        layer: str,
        style: str,
    ) -> Any:
        height = float(item.height if item.height is not None else default_height)
        text = _com_retry(
            model_space.AddText, item.text, _variant_point(item.point), height
        )
        try:
            text.StyleName = style
        except Exception:  # noqa: BLE001
            pass
        text.Layer = layer
        # Сначала выравнивание, затем точка выравнивания — иначе ACAD сбросит её.
        text.Alignment = AC_ALIGNMENT_MIDDLE_CENTER
        text.TextAlignmentPoint = _variant_point(item.point)
        text.Rotation = item.rotation
        return text

    # -- главный вход -------------------------------------------------------
    def draw(
        self,
        description: LegalDescription,
        scale: float | None = None,
        text_height: float | None = None,
        *,
        is_neighbor: bool = False,
        neighbor_label: str | None = None,
    ) -> DrawResult:
        """Планирует и рисует участок (основной или соседний) в активном документе AutoCAD."""
        plan = self.plan(
            description,
            scale=scale,
            text_height=text_height,
            is_neighbor=is_neighbor,
            neighbor_label=neighbor_label,
        )
        warnings: list[str] = []

        drawing = self.drawing_cfg
        layers_cfg = self.cfg.get("layers", {})
        line_layer_name, text_layer_name = self.layer_names()
        street_row_layer_name = str(layers_cfg.get("street_row") or "STREET_ROW_AI")
        neighbor_layer_name = str(layers_cfg.get("neighbor_lot") or "NEIGHBOR_LOT")
        colors = self.cfg.get("layer_colors", {})

        app, doc = self.connect()
        try:
            # Предупреждение о единицах чертежа появляется именно при подключении.
            if self._unit_warning:
                warnings.append(self._unit_warning)

            # Если в AutoCAD открыта вкладка листа (PaperSpace, ActiveSpace == 0),
            # переключаемся в ModelSpace (1), чтобы ZoomExtents показал нарисованный контур.
            try:
                if getattr(doc, "ActiveSpace", 1) == 0 and not getattr(doc, "MSpace", False):
                    doc.ActiveSpace = 1
            except Exception:  # noqa: BLE001
                warnings.append(
                    "активен лист PaperSpace — переключитесь на вкладку Model в AutoCAD"
                )

            try:
                model_space = doc.ModelSpace
            except Exception as exc:  # noqa: BLE001
                raise AcadDrawError(f"не удалось получить ModelSpace: {exc}") from exc

            style = self._ensure_text_style(doc)

            if is_neighbor:
                neighbor_layer = self._ensure_layer(
                    doc, neighbor_layer_name, int(colors.get("neighbor_lot", 4))
                )
                if neighbor_layer == "0":
                    warnings.append(
                        f"слой {neighbor_layer_name} недоступен — сосед нарисован в слое 0"
                    )
                poly_target_layer = neighbor_layer
                text_target_layer = neighbor_layer
                street_row_layer = neighbor_layer
            else:
                line_layer = self._ensure_layer(
                    doc, line_layer_name, int(colors.get("property_line", 1))
                )
                text_layer = self._ensure_layer(
                    doc, text_layer_name, int(colors.get("boundary_text", 3))
                )
                street_row_layer = text_layer
                if plan.lines:
                    street_row_layer = self._ensure_layer(
                        doc, street_row_layer_name, int(colors.get("street_row", 8))
                    )
                if line_layer == "0":
                    warnings.append(
                        f"слой {line_layer_name} недоступен — линия нарисована в слое 0"
                    )
                if text_layer == "0":
                    warnings.append(
                        f"слой {text_layer_name} недоступен — текст нарисован в слое 0"
                    )
                poly_target_layer = line_layer
                text_target_layer = text_layer

            if plan.lines and hasattr(model_space, "AddLine"):
                for row_line in plan.lines:
                    try:
                        self._add_line(model_space, row_line, street_row_layer)
                    except Exception as exc:  # noqa: BLE001
                        warnings.append(f"не удалось создать линию улицы ({row_line.kind}): {exc}")

            draw_as_lines = bool(drawing.get("property_as_lines", True))
            primary_entity: Any = None
            created_boundary_lines = 0
            if draw_as_lines and plan.boundary_lines and hasattr(model_space, "AddLine"):
                for b_line in plan.boundary_lines:
                    line_ent = self._add_line(model_space, b_line, poly_target_layer)
                    created_boundary_lines += 1
                    if primary_entity is None:
                        primary_entity = line_ent
            else:
                if plan.gap_points is not None and plan.boundary_lines:
                    k_idx = plan.gap_vertex_index or 0
                    n_b = len(plan.boundary_lines)
                    ordered_chain = [
                        plan.boundary_lines[(k_idx + step) % n_b] for step in range(n_b)
                    ]
                    poly_pts = [ordered_chain[0].start] + [seg_l.end for seg_l in ordered_chain]
                    primary_entity = self._add_polyline(
                        model_space, poly_pts, poly_target_layer, closed=False
                    )
                else:
                    primary_entity = self._add_polyline(
                        model_space, plan.vertices, poly_target_layer, closed=True
                    )

            if plan.arcs and hasattr(model_space, "AddArc"):
                for arc_item in plan.arcs:
                    try:
                        self._add_arc(model_space, arc_item, text_target_layer)
                    except Exception as exc:  # noqa: BLE001
                        warnings.append(f"не удалось создать дугу угла ({arc_item.kind}): {exc}")

            created = 0
            for item in plan.items:
                try:
                    self._add_text(
                        model_space, item, plan.text_height, text_target_layer, style
                    )
                    created += 1
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"не удалось создать текст «{item.text}»: {exc}")

            if drawing.get("zoom_extents", True):
                zoomed = False
                try:
                    _com_retry(app.ZoomExtents)
                    zoomed = True
                except Exception:  # noqa: BLE001 - зум не критичен
                    try:
                        doc.SendCommand("_.ZOOM\n_E\n")
                        zoomed = True
                    except Exception:  # noqa: BLE001
                        warnings.append("не удалось выполнить Zoom Extents")

                # Отступ вокруг чертежа (acZoomScaledRelative = 1), чтобы нижняя
                # плавающая командная строка и ViewCube в AutoCAD не перекрывали подписи.
                margin_scale = float(drawing.get("zoom_margin_scale", 0.84) or 1.0)
                if zoomed and 0.1 < margin_scale < 0.999 and hasattr(app, "ZoomScaled"):
                    try:
                        _com_retry(app.ZoomScaled, margin_scale, 1)
                    except Exception:  # noqa: BLE001
                        pass

            handle: str | None = None
            try:
                if primary_entity is not None:
                    handle = str(primary_entity.Handle)
            except Exception:  # noqa: BLE001
                pass

            if is_neighbor:
                result_layers = {"neighbor_lot": poly_target_layer}
            else:
                result_layers = {
                    "property_line": poly_target_layer,
                    "boundary_text": text_target_layer,
                }
                if plan.lines:
                    result_layers["street_row"] = street_row_layer

            return DrawResult(
                plan=plan,
                text_count=created,
                layers=result_layers,
                handle=handle,
                warnings=warnings,
                boundary_line_count=created_boundary_lines,
            )
        finally:
            if self._com_owned:
                self._app = None
                self._doc = None
                self._com_owned = False
                self._com_thread_id = None
                _co_uninitialize()


def draw_legal_description(
    description: LegalDescription,
    cfg: dict[str, Any] | None = None,
    scale: float | None = None,
    text_height: float | None = None,
    *,
    is_neighbor: bool = False,
    neighbor_label: str | None = None,
) -> DrawResult:
    """Отрисовать участок (основной или соседний), не создавая AcadBuilder вручную."""
    return AcadBuilder(cfg).draw(
        description,
        scale=scale,
        text_height=text_height,
        is_neighbor=is_neighbor,
        neighbor_label=neighbor_label,
    )


# ---------------------------------------------------------------------------
# Интеллектуальная сверка с соседними участками (Adjoiner Verification)
# ---------------------------------------------------------------------------

_OCR_CONFUSABLE_DIGITS: frozenset[tuple[str, str]] = frozenset(
    pair
    for a, b in (
        ("6", "9"),
        ("0", "8"),
        ("3", "8"),
        ("2", "5"),
        ("5", "6"),
        ("1", "7"),
        ("0", "6"),
        ("3", "5"),
        ("5", "9"),
        ("1", "4"),
    )
    for pair in ((a, b), (b, a))
)


def _format_arcsec_dms(arcsec: float) -> str:
    """Форматирует разницу углов в секундах дуги в читаемый вид ``D°MM'SS"``."""
    val = abs(float(arcsec))
    tenths = int(Decimal(str(val * 10.0)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    deg, rem = divmod(tenths, 36000)
    minutes, sec_tenths = divmod(rem, 600)
    if sec_tenths % 10 == 0:
        sec_txt = f"{sec_tenths // 10:02d}"
    else:
        sec_val = sec_tenths / 10.0
        sec_txt = f"{sec_val:04.1f}" if sec_tenths < 100 else f"{sec_val:.1f}"
    return f"{deg}°{minutes:02d}'{sec_txt}\""


def _detect_digit_typo(str_a: str, str_b: str) -> str | None:
    """
    Проверяет, отличаются ли две числовые записи ровно одной цифрой
    (включая типичные ошибки OCR 80-х ``6↔9``, ``0↔8``, ``3↔8``, ``2↔5``)
    или перестановкой двух соседних цифр (``14↔41``, ``05↔50``).
    """
    a = str_a.strip()
    b = str_b.strip()
    if a == b or len(a) != len(b):
        return None
    diffs = [idx for idx, (ca, cb) in enumerate(zip(a, b)) if ca != cb]
    if len(diffs) == 1:
        i = diffs[0]
        ca, cb = a[i], b[i]
        if ca.isdigit() and cb.isdigit():
            if (ca, cb) in _OCR_CONFUSABLE_DIGITS:
                return (
                    f"подмена одной цифры «{ca} ↔ {cb}» "
                    "(типичная ошибка OCR или чтения выцветшего шрифта)"
                )
            return f"различие ровно в одной цифре «{ca} ↔ {cb}» (вероятна опечатка)"
    if len(diffs) == 2 and diffs[1] == diffs[0] + 1:
        i, j = diffs
        if a[i] == b[j] and a[j] == b[i] and a[i].isdigit() and a[j].isdigit():
            return (
                f"перестановка двух соседних цифр «{a[i]}{a[j]} ↔ {b[i]}{b[j]}» "
                "(классическая опечатка при перепечатке акта)"
            )
    return None


def _clean_polygon_ring(points: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    ring: list[tuple[float, float]] = []
    for pt in points:
        p = (float(pt[0]), float(pt[1]))
        if not ring or math.hypot(p[0] - ring[-1][0], p[1] - ring[-1][1]) > 1e-7:
            ring.append(p)
    if len(ring) >= 2 and math.hypot(ring[0][0] - ring[-1][0], ring[0][1] - ring[-1][1]) <= 1e-7:
        ring.pop()
    return ring


def _clip_convex_polygons(
    subject: Sequence[tuple[float, float]],
    clipper: Sequence[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Алгоритм Сазерленда–Ходжмана для пары выпуклых CCW-многоугольников."""

    def _cross(
        o: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
    ) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    def _intersect(
        p1: tuple[float, float],
        p2: tuple[float, float],
        c1: tuple[float, float],
        c2: tuple[float, float],
    ) -> tuple[float, float]:
        dx_p, dy_p = p2[0] - p1[0], p2[1] - p1[1]
        dx_c, dy_c = c2[0] - c1[0], c2[1] - c1[1]
        denom = dx_p * dy_c - dy_p * dx_c
        if abs(denom) < 1e-12:
            return p2
        t = ((c1[0] - p1[0]) * dy_c - (c1[1] - p1[1]) * dx_c) / denom
        return (p1[0] + t * dx_p, p1[1] + t * dy_p)

    out = list(subject)
    if len(out) < 3 or len(clipper) < 3:
        return []
    for idx in range(len(clipper)):
        c1 = clipper[idx]
        c2 = clipper[(idx + 1) % len(clipper)]
        inp = out
        out = []
        if not inp:
            break
        s = inp[-1]
        s_in = _cross(c1, c2, s) >= -1e-9
        for e in inp:
            e_in = _cross(c1, c2, e) >= -1e-9
            if e_in:
                if not s_in:
                    out.append(_intersect(s, e, c1, c2))
                out.append(e)
            elif s_in:
                out.append(_intersect(s, e, c1, c2))
            s, s_in = e, e_in
    return out


def _triangulate_simple_polygon(
    points: Sequence[tuple[float, float]],
) -> list[tuple[tuple[float, float], tuple[float, float], tuple[float, float]]]:
    """Ушная триангуляция (Ear Clipping) простого многоугольника произвольной формы."""
    ring = _clean_polygon_ring(points)
    if len(ring) < 3:
        return []
    if polygon_signed_area(ring) < 0.0:
        ring.reverse()

    def _cross(
        a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]
    ) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    def _pt_in_tri(
        p: tuple[float, float],
        a: tuple[float, float],
        b: tuple[float, float],
        c: tuple[float, float],
    ) -> bool:
        return (
            _cross(a, b, p) >= -1e-9
            and _cross(b, c, p) >= -1e-9
            and _cross(c, a, p) >= -1e-9
        )

    tris: list[tuple[tuple[float, float], tuple[float, float], tuple[float, float]]] = []
    work = list(ring)
    guard = 0
    max_guard = len(work) * len(work) + 10
    while len(work) > 3 and guard < max_guard:
        guard += 1
        ear_found = False
        m = len(work)
        for i in range(m):
            prev_p = work[(i - 1) % m]
            curr_p = work[i]
            next_p = work[(i + 1) % m]
            if _cross(prev_p, curr_p, next_p) <= 1e-9:
                continue
            has_inner = False
            for j in range(m):
                if j in ((i - 1) % m, i, (i + 1) % m):
                    continue
                if _pt_in_tri(work[j], prev_p, curr_p, next_p):
                    has_inner = True
                    break
            if not has_inner:
                tris.append((prev_p, curr_p, next_p))
                del work[i]
                ear_found = True
                break
        if not ear_found:
            # Резервный веер для вырожденных самокасаний
            p0 = work[0]
            for k in range(1, len(work) - 1):
                if abs(_cross(p0, work[k], work[k + 1])) > 1e-9:
                    tris.append((p0, work[k], work[k + 1]))
            return tris
    if len(work) == 3 and abs(_cross(work[0], work[1], work[2])) > 1e-9:
        tris.append((work[0], work[1], work[2]))
    return tris


def polygon_intersection_area(
    poly_a: Sequence[tuple[float, float]],
    poly_b: Sequence[tuple[float, float]],
) -> float:
    """Точная площадь 2D-пересечения двух многоугольников (в кв. футах)."""
    tris_a = _triangulate_simple_polygon(poly_a)
    tris_b = _triangulate_simple_polygon(poly_b)
    if not tris_a or not tris_b:
        return 0.0
    total_area = 0.0
    for ta in tris_a:
        min_ax = min(p[0] for p in ta)
        max_ax = max(p[0] for p in ta)
        min_ay = min(p[1] for p in ta)
        max_ay = max(p[1] for p in ta)
        for tb in tris_b:
            if (
                max(p[0] for p in tb) < min_ax - 1e-7
                or min(p[0] for p in tb) > max_ax + 1e-7
                or max(p[1] for p in tb) < min_ay - 1e-7
                or min(p[1] for p in tb) > max_ay + 1e-7
            ):
                continue
            clipped = _clip_convex_polygons(ta, tb)
            if len(clipped) >= 3:
                total_area += abs(polygon_signed_area(clipped))
    return total_area


def _point_to_segment_dist(
    pt: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    l2 = dx * dx + dy * dy
    if l2 <= 1e-12:
        return math.hypot(pt[0] - a[0], pt[1] - a[1])
    t = max(0.0, min(1.0, ((pt[0] - a[0]) * dx + (pt[1] - a[1]) * dy) / l2))
    proj_x = a[0] + t * dx
    proj_y = a[1] + t * dy
    return math.hypot(pt[0] - proj_x, pt[1] - proj_y)


def _segments_proper_intersect(
    a1: tuple[float, float],
    a2: tuple[float, float],
    b1: tuple[float, float],
    b2: tuple[float, float],
) -> bool:
    def _orient(
        p: tuple[float, float], q: tuple[float, float], r: tuple[float, float]
    ) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    o1 = _orient(a1, a2, b1)
    o2 = _orient(a1, a2, b2)
    o3 = _orient(b1, b2, a1)
    o4 = _orient(b1, b2, a2)
    return (o1 * o2 < -1e-9) and (o3 * o4 < -1e-9)


def _segment_to_segment_dist(
    a1: tuple[float, float],
    a2: tuple[float, float],
    b1: tuple[float, float],
    b2: tuple[float, float],
) -> float:
    if _segments_proper_intersect(a1, a2, b1, b2):
        return 0.0
    return min(
        _point_to_segment_dist(a1, b1, b2),
        _point_to_segment_dist(a2, b1, b2),
        _point_to_segment_dist(b1, a1, a2),
        _point_to_segment_dist(b2, a1, a2),
    )


def _min_polygon_separation_ft(
    poly_a: Sequence[tuple[float, float]],
    poly_b: Sequence[tuple[float, float]],
) -> float:
    ring_a = _clean_polygon_ring(poly_a)
    ring_b = _clean_polygon_ring(poly_b)
    if len(ring_a) < 3 or len(ring_b) < 3:
        return 0.0
    for pa in ring_a:
        if point_in_polygon(pa, ring_b):
            return 0.0
    for pb in ring_b:
        if point_in_polygon(pb, ring_a):
            return 0.0
    best = float("inf")
    na, nb = len(ring_a), len(ring_b)
    for i in range(na):
        a1, a2 = ring_a[i], ring_a[(i + 1) % na]
        for j in range(nb):
            b1, b2 = ring_b[j], ring_b[(j + 1) % nb]
            d = _segment_to_segment_dist(a1, a2, b1, b2)
            if d < best:
                best = d
                if best <= 1e-7:
                    return 0.0
    return 0.0 if best == float("inf") else best


@dataclass
class SharedEdgeCheck:
    """Результат детальной сверки одной общей стороны основного участка и соседа."""

    main_seg_index: int
    neighbor_seg_index: int
    main_bearing: str
    neighbor_bearing: str
    main_length_ft: float
    neighbor_length_ft: float
    is_opposite_direction: bool
    angle_diff_deg: float
    angle_diff_arcsec: float
    angle_diff_dms: str
    length_diff_ft: float
    abs_length_diff_ft: float
    lateral_offset_start_ft: float
    lateral_offset_end_ft: float
    overlap_span_ft: float
    main_line: tuple[tuple[float, float], tuple[float, float]]
    neighbor_line: tuple[tuple[float, float], tuple[float, float]]
    status: str = "ok"  # "ok" | "warn" | "error"
    issue_codes: list[str] = field(default_factory=list)
    diagnoses: list[str] = field(default_factory=list)
    likely_culprit: str | None = None  # "main" | "neighbor" | None


@dataclass
class NeighborPairReport:
    """Полный отчёт сверки основного участка с одним конкретным соседом."""

    neighbor_index: int
    neighbor_title: str
    status: str = "ok"  # "ok" | "warn" | "error"
    shared_edges: list[SharedEdgeCheck] = field(default_factory=list)
    overlap_area_sqft: float = 0.0
    min_poly_distance_ft: float = 0.0
    datum_rotation_arcsec: float | None = None
    street_tie_check: str | None = None
    findings: list[str] = field(default_factory=list)


@dataclass
class NeighborVerificationReport:
    """Сводный акт сверки основного участка со всеми соседними участками."""

    overall_status: str = "ok"  # "ok" | "warn" | "error"
    pair_reports: list[NeighborPairReport] = field(default_factory=list)
    total_shared_edges: int = 0
    warning_count: int = 0
    error_count: int = 0

    @property
    def has_issues(self) -> bool:
        return self.overall_status in ("warn", "error")

    def short_badge(self) -> str:
        n_nb = len(self.pair_reports)
        if n_nb == 0:
            return "🔍 Сверка с соседями"
        if self.overall_status == "ok":
            return f"🔍 Соседи ({n_nb}): ✓ норма"
        total_probs = self.error_count + self.warning_count
        return f"🔍 Соседи ({n_nb}): ⚠ расх. ({total_probs})"

    def headline(self) -> str:
        n_nb = len(self.pair_reports)
        if n_nb == 0:
            return "Соседние участки ещё не загружены для сверки."
        if self.overall_status == "ok":
            return (
                f"✅ Сверка с соседями ({n_nb} уч., общих границ: {self.total_shared_edges}): "
                "углы, длины и стыковка границ полностью сходятся!"
            )
        sev = (
            f"критических несоответствий: {self.error_count}"
            if self.error_count
            else f"предупреждений: {self.warning_count}"
        )
        if self.error_count and self.warning_count:
            sev += f", предупреждений: {self.warning_count}"
        return f"⚠ Сверка с соседями ({n_nb} уч.): обнаружено расхождение ({sev})!"

    def format_report(self, *, compact: bool = False) -> str:
        if not self.pair_reports:
            return (
                "Для сверки границ загрузите основной участок и хотя бы один соседний участок\n"
                "(через переключатель «Соседний (NEIGHBOR_LOT)», кнопку «📂 Пакет соседей…» "
                "или «🔍 Сверка с соседями»)."
            )
        lines: list[str] = [self.headline()]
        for pr in self.pair_reports:
            icon = "✅" if pr.status == "ok" else ("🚨" if pr.status == "error" else "⚠")
            lines.append("")
            lines.append(f"{icon} Сосед #{pr.neighbor_index}: {pr.neighbor_title}")
            if pr.street_tie_check:
                lines.append(f"   • Привязка по улице: {pr.street_tie_check}")
            if pr.overlap_area_sqft > 0.5:
                lines.append(
                    f"   • 🚨 НАЛОЖЕНИЕ ГРАНИЦ (Overlap / Encroachment): площадь пересечения = "
                    f"{pr.overlap_area_sqft:.2f} sq ft!"
                )
            if pr.datum_rotation_arcsec is not None:
                sign_ch = "+" if pr.datum_rotation_arcsec >= 0 else "-"
                lines.append(
                    f"   • 🧭 Систематический поворот базиса румбов (Bearing Basis): "
                    f"{sign_ch}{_format_arcsec_dms(pr.datum_rotation_arcsec)} по всем линиям соседа"
                )
            for ec in pr.shared_edges:
                e_icon = (
                    "✓"
                    if ec.status == "ok"
                    else ("✗" if ec.status == "error" else "!")
                )
                lines.append(
                    f"   [{e_icon}] Общая межа: наша сторона #{ec.main_seg_index} "
                    f"({ec.main_bearing}, {format_distance(ec.main_length_ft)})  ↔  "
                    f"сосед #{ec.neighbor_seg_index} "
                    f"({ec.neighbor_bearing}, {format_distance(ec.neighbor_length_ft)})"
                )
                if ec.status == "ok" and compact:
                    lines.append(
                        f"       Идеальное совпадение: Δ∠ = {ec.angle_diff_dms}, "
                        f"|ΔL| = {ec.abs_length_diff_ft:.3f}', зазор = "
                        f"{max(ec.lateral_offset_start_ft, ec.lateral_offset_end_ft):.3f}'"
                    )
                else:
                    lines.append(
                        f"       Разница угла Δ∠ = {ec.angle_diff_dms} ({ec.angle_diff_arcsec:.1f}\") | "
                        f"Разница длины ΔL = {ec.length_diff_ft:+.3f}' (|{ec.abs_length_diff_ft:.3f}'|) | "
                        f"Смещение концов: {ec.lateral_offset_start_ft:.3f}' → {ec.lateral_offset_end_ft:.3f}'"
                    )
                    for diag in ec.diagnoses:
                        lines.append(f"       → {diag}")
            for extra_f in pr.findings:
                lines.append(f"   • {extra_f}")
        return "\n".join(lines)


def _neighbor_display_title(plan: DrawPlan, idx: int) -> str:
    d = plan.description
    parts: list[str] = []
    if d.lot_number:
        parts.append(f"Lot {d.lot_number}")
    if d.block_number:
        parts.append(f"Block {d.block_number}")
    if d.main_street:
        if d.distance_to_corner_ft > 1e-6:
            parts.append(f"{d.main_street} ({d.distance_to_corner_ft:g}' от угла)")
        else:
            parts.append(f"{d.main_street} (угловой POB)")
    if not parts:
        parts.append(f"POB ({plan.pob[0]:+.1f}', {plan.pob[1]:+.1f}')")
    return ", ".join(parts)


def _normalize_street_key(name: str | None) -> str:
    if not name:
        return ""
    s = re.sub(r"[^a-z0-9]+", " ", str(name).lower()).strip()
    replacements = {
        "street": "st",
        "avenue": "ave",
        "boulevard": "blvd",
        "road": "rd",
        "drive": "dr",
        "place": "pl",
        "lane": "ln",
        "parkway": "pkwy",
        "expressway": "expy",
        "court": "ct",
    }
    tokens = [replacements.get(tok, tok) for tok in s.split()]
    return " ".join(tokens)


def _signed_axis_diff_arcsec(az_main_deg: float, az_nb_deg: float) -> float:
    """Ориентированная разница направлений неориентированных прямых в секундах дуги (-324000..+324000)."""
    diff = (float(az_nb_deg) - float(az_main_deg)) % 180.0
    if diff > 90.0:
        diff -= 180.0
    return diff * 3600.0


def _segment_display_bearing(seg: Segment, line: LineItem) -> str:
    if getattr(seg, "has_bearing", True):
        return seg.bearing_label
    if seg.course_note:
        return str(seg.course_note)
    dx = line.end[0] - line.start[0]
    dy = line.end[1] - line.start[1]
    az = math.degrees(math.atan2(dx, dy)) % 360.0
    return f"азимут {az:.2f}°"


def _diagnose_angle_discrepancy(
    seg_m: Segment,
    seg_n: Segment,
    angle_diff_arcsec: float,
    shared_len_ft: float,
    is_opposite_dir: bool,
    same_winding: bool,
) -> tuple[list[str], list[str]]:
    """Интеллектуальная диагностика углового расхождения общей стороны."""
    codes: list[str] = []
    notes: list[str] = []

    # 1. Проверка встречности обхода
    if same_winding and not is_opposite_dir and angle_diff_arcsec <= 1800.0:
        codes.append("SAME_DIRECTION_VECTOR")
        notes.append(
            "Оба участка обходятся в одном направлении, но векторы общей стены смотрят "
            "в одну и ту же сторону (а не навстречу друг другу) — проверьте, не перепутаны ли "
            "буквы румба на противоположные (N↔S и E↔W)."
        )

    if angle_diff_arcsec <= 1.0:
        return codes, notes

    lateral_shift = shared_len_ft * math.sin(math.radians(angle_diff_arcsec / 3600.0))
    dms_delta = _format_arcsec_dms(angle_diff_arcsec)

    both_have_bearing = bool(
        getattr(seg_m, "has_bearing", True) and getattr(seg_n, "has_bearing", True)
    )
    if both_have_bearing:
        # Приводим ожидаемый встречный (или попутный) квадрант соседа к каноническому виду нашей стороны
        exp_ns = ("S" if seg_m.ns == "N" else "N") if is_opposite_dir else seg_m.ns
        exp_ew = ("W" if seg_m.ew == "E" else "E") if is_opposite_dir else seg_m.ew

        num_m_sec = int(round(seg_m.degrees * 3600 + seg_m.minutes * 60 + seg_m.seconds))
        num_n_sec = int(round(seg_n.degrees * 3600 + seg_n.minutes * 60 + seg_n.seconds))
        num_diff_sec = abs(num_m_sec - num_n_sec)

        # Случай А: Численный угол почти одинаковый, но перепутана буква квадранта (E↔W или N↔S)!
        if (seg_n.ns != exp_ns or seg_n.ew != exp_ew) and num_diff_sec <= 300:
            wrong_parts: list[str] = []
            if seg_n.ns != exp_ns:
                wrong_parts.append(f"«{seg_n.ns}» вместо ожидаемого «{exp_ns}»")
            if seg_n.ew != exp_ew:
                wrong_parts.append(f"«{seg_n.ew}» вместо ожидаемого «{exp_ew}»")
            codes.append("QUADRANT_LETTER_TYPO")
            notes.append(
                f"Зеркальная опечатка квадранта: численный угол румба совпадает "
                f"({seg_m.degrees}°{seg_m.minutes:02d}'{seg_m.seconds:02g}\"), но у соседа указано "
                + " и ".join(wrong_parts)
                + "!"
            )
            return codes, notes

        # Случай Б: Градусы и минуты полностью совпадают — разница ТОЛЬКО В СЕКУНДАХ!
        if (
            seg_n.ns == exp_ns
            and seg_n.ew == exp_ew
            and seg_m.degrees == seg_n.degrees
            and seg_m.minutes == seg_n.minutes
        ):
            s_m = f"{int(round(seg_m.seconds)):02d}"
            s_n = f"{int(round(seg_n.seconds)):02d}"
            sec_typo = _detect_digit_typo(s_m, s_n)
            one_rounded = (
                int(round(seg_m.seconds)) in (0, 30)
                and int(round(seg_n.seconds)) not in (0, 30)
            ) or (
                int(round(seg_n.seconds)) in (0, 30)
                and int(round(seg_m.seconds)) not in (0, 30)
            )
            codes.append("SECONDS_ONLY_DIFF")
            if sec_typo:
                notes.append(
                    f"Градусы и минуты совпадают ({seg_m.degrees}°{seg_m.minutes:02d}'), "
                    f"отличаются только секунды ({s_m}\" против {s_n}\", Δ∠ = {dms_delta} → "
                    f"уход {lateral_shift:.3f}' на длине {shared_len_ft:.1f}'): {sec_typo}."
                )
            elif one_rounded:
                notes.append(
                    f"Градусы и минуты совпадают ({seg_m.degrees}°{seg_m.minutes:02d}'), "
                    f"разница только в секундах ({s_m}\" против {s_n}\", Δ∠ = {dms_delta} → "
                    f"уход {lateral_shift:.3f}'): вероятно округление румба до целых/полуминут "
                    "на архивном чертеже 80-х гг. либо опечатка секунд."
                )
            else:
                notes.append(
                    f"Разница только в секундах угла ({s_m}\" против {s_n}\", Δ∠ = {dms_delta}, "
                    f"поперечный уход {lateral_shift:.3f}'): вероятна ошибка чтения/распознавания "
                    "мелких цифр секунд или погрешность повторного обмера."
                )
            return codes, notes

        # Случай В: Градусы совпадают, секунды близки, но отличаются МИНУТЫ
        if (
            seg_n.ns == exp_ns
            and seg_n.ew == exp_ew
            and seg_m.degrees == seg_n.degrees
            and abs(seg_m.seconds - seg_n.seconds) <= 2.0
        ):
            m_a = f"{seg_m.minutes:02d}"
            m_b = f"{seg_n.minutes:02d}"
            min_typo = _detect_digit_typo(m_a, m_b)
            codes.append("MINUTES_DIFF")
            if min_typo:
                notes.append(
                    f"Градусы ({seg_m.degrees}°) и секунды ({int(round(seg_m.seconds)):02d}\") "
                    f"совпадают, но минуты отличаются ({m_a}' против {m_b}', Δ∠ = {dms_delta}, "
                    f"уход {lateral_shift:.2f}'): {min_typo}!"
                )
                return codes, notes

        # Случай Г: Минуты и секунды совпадают, но отличаются ГРАДУСЫ!
        if (
            seg_n.ns == exp_ns
            and seg_n.ew == exp_ew
            and seg_m.minutes == seg_n.minutes
            and abs(seg_m.seconds - seg_n.seconds) <= 1.5
            and seg_m.degrees != seg_n.degrees
        ):
            d_a = f"{seg_m.degrees:02d}"
            d_b = f"{seg_n.degrees:02d}"
            deg_typo = _detect_digit_typo(d_a, d_b)
            codes.append("DEGREES_TYPO")
            detail = f" ({deg_typo})" if deg_typo else ""
            notes.append(
                f"Минуты и секунды полностью совпадают ({seg_m.minutes:02d}'{int(round(seg_m.seconds)):02d}\"), "
                f"но градусы разные ({seg_m.degrees}° против {seg_n.degrees}°){detail} — "
                "почти достоверная опечатка или ошибка OCR в числе градусов!"
            )
            return codes, notes

    # Общий геодезический вывод по угловому расхождению
    codes.append("ANGLE_MISMATCH")
    notes.append(
        f"Угол общей межи расходится на {dms_delta} ({angle_diff_arcsec:.1f}\"), что на длине "
        f"{shared_len_ft:.2f}' уводит конец линии в сторону на {lateral_shift:.3f}' "
        f"({lateral_shift * 12.0:.1f}\")."
    )
    return codes, notes


def _check_feet_inches_confusion(len_a: float, len_b: float) -> str | None:
    """
    Ловит классическую ошибку записи дюймов как десятичных долей фута:
    например ``100'-6"`` = ``100.50'``, но ошибочно записано ``100.60'`` (или ``100.06'``).
    """
    fa, fb = int(math.floor(len_a + 1e-6)), int(math.floor(len_b + 1e-6))
    if fa != fb:
        return None
    ra, rb = len_a - fa, len_b - fb
    for inches in range(1, 12):
        true_frac = inches / 12.0
        wrong_tenths = inches / 10.0 if inches < 10 else None
        wrong_hundredths = inches / 100.0
        for true_v, wrong_v, label_wrong in (
            (true_frac, wrong_tenths, f"{fa}.{inches}"),
            (true_frac, wrong_hundredths, f"{fa}.{inches:02d}"),
        ):
            if wrong_v is None or abs(true_v - wrong_v) < 0.012:
                continue
            if (abs(ra - true_v) <= 0.012 and abs(rb - wrong_v) <= 0.012) or (
                abs(rb - true_v) <= 0.012 and abs(ra - wrong_v) <= 0.012
            ):
                return (
                    f"Путаница «дюймы ↔ десятые фута»: {fa}'-{inches}\" в геодезических футах "
                    f"равно {fa + true_v:.2f}', но во втором описании записано как «{label_wrong}'»!"
                )
    return None


def _diagnose_length_discrepancy(
    len_m: float,
    len_n: float,
    *,
    main_closed: bool,
    nb_closed: bool,
    start_aligned: bool,
    angle_aligned: bool,
) -> tuple[list[str], list[str]]:
    """Интеллектуальная диагностика разницы длин общей стороны."""
    abs_diff = abs(len_m - len_n)
    if abs_diff <= 0.015:
        return [], []

    codes: list[str] = []
    notes: list[str] = []

    # 1. Микроразница округления дробных дюймов (до ~0.04' = 1/2")
    if abs_diff <= 0.045:
        codes.append("LENGTH_ROUNDING")
        notes.append(
            f"Небольшая разница длины ({abs_diff:.3f}' ≈ {abs_diff * 12.0:.2f}\") — "
            "вероятно округление дробных дюймов (1/8\"–1/2\") при переводе со старого чертежа."
        )
        return codes, notes

    # 2. Путаница десятичных футов и дюймов (100.50' vs 100.60')
    fi_msg = _check_feet_inches_confusion(len_m, len_n)
    if fi_msg:
        codes.append("FEET_INCHES_CONFUSION")
        notes.append(fi_msg)
        return codes, notes

    # 3. Посимвольная опечатка или перестановка цифр в длине (100.00 vs 108.00, 126.32 vs 129.32)
    str_m = f"{len_m:.2f}"
    str_n = f"{len_n:.2f}"
    len_typo = _detect_digit_typo(str_m, str_n)
    if not len_typo and abs(len_m - round(len_m)) < 1e-3 and abs(len_n - round(len_n)) < 1e-3:
        len_typo = _detect_digit_typo(str(int(round(len_m))), str(int(round(len_n))))
    if len_typo:
        codes.append("LENGTH_DIGIT_TYPO")
        notes.append(
            f"Длина общей стены отличается на {abs_diff:.2f}' ({len_m:.2f}' против {len_n:.2f}'): "
            f"{len_typo}!"
        )
        return codes, notes

    # 4. Оба участка замкнуты в 0, угол и начало совпадают, но глубина разная -> ступенчатая межа!
    if main_closed and nb_closed and start_aligned and angle_aligned and abs_diff >= 2.0:
        codes.append("STEPPED_BOUNDARY_DEPTH")
        notes.append(
            f"Частичное примыкание (разная глубина участков на {abs_diff:.2f}': {len_m:.2f}' против "
            f"{len_n:.2f}'): оба контура замкнуты без внутренней невязки, а линия и уличный угол "
            "совпадают. Либо участки реально имеют разную глубину (уступ задней межи), "
            "либо в одном из документов ошибочно указана глубина всего лота."
        )
        return codes, notes

    # 5. Кратность ровно целым дюймам или ровно 1 футу
    inches_diff = abs_diff * 12.0
    if abs_diff < 1.0 and abs(inches_diff - round(inches_diff)) <= 0.06:
        codes.append("EXACT_INCHES_DIFF")
        notes.append(
            f"Длина общей межи отличается ровно на {int(round(inches_diff))}\" ({abs_diff:.3f}'): "
            f"{len_m:.2f}' против {len_n:.2f}' — проверьте отсчёт дюймов на чертеже."
        )
        return codes, notes

    codes.append("LENGTH_MISMATCH")
    notes.append(
        f"Существенное расхождение длины общей межи: у нас {len_m:.2f}', у соседа {len_n:.2f}' "
        f"(разница {len_m - len_n:+.2f}' = {abs_diff * 12.0:.1f}\")."
    )
    return codes, notes


def _attribute_error_by_closure(
    main_closure_ft: float,
    nb_closure_ft: float,
    edge_vector_diff_ft: float,
) -> tuple[str | None, str | None]:
    """
    Кросс-проверка через собственную невязку полигонов: позволяет определить,
    в чьих именно данных (наших или соседских) сидит ошибка!
    """
    if edge_vector_diff_ft <= 0.04:
        return None, None
    main_ok = main_closure_ft <= 0.03
    nb_ok = nb_closure_ft <= 0.03
    if main_ok and not nb_ok and nb_closure_ft >= 0.08:
        matches_mag = abs(nb_closure_ft - edge_vector_diff_ft) <= max(0.12, 0.20 * nb_closure_ft)
        tail = (
            f", совпадающую с расхождением на этой стене ({edge_vector_diff_ft:.3f}')"
            if matches_mag
            else ""
        )
        return (
            "neighbor",
            f"Кросс-проверка замкнутости: наш участок замкнут идеально ({main_closure_ft:.3f}'), "
            f"а контур соседа имеет внутреннюю невязку {nb_closure_ft:.3f}'{tail} — "
            "вероятнее всего, ошибка/опечатка допущена в данных СОСЕДА.",
        )
    if nb_ok and not main_ok and main_closure_ft >= 0.08:
        matches_mag = abs(main_closure_ft - edge_vector_diff_ft) <= max(
            0.12, 0.20 * main_closure_ft
        )
        tail = (
            f", равную расхождению на общей меже ({edge_vector_diff_ft:.3f}')"
            if matches_mag
            else ""
        )
        return (
            "main",
            f"Кросс-проверка замкнутости: контур соседа замкнут идеально ({nb_closure_ft:.3f}'), "
            f"а НАШ основной участок имеет невязку {main_closure_ft:.3f}'{tail} — "
            "высока вероятность, что ошибка находится в НАШЕМ описании!",
        )
    return None, None


def _detect_datum_rotation_arcsec(
    main_plan: DrawPlan, nb_plan: DrawPlan
) -> float | None:
    """
    Проверяет, не повёрнут ли весь полигон соседа как жёсткое целое на постоянный угол
    (разный исходный меридиан / Magnetic North 80-х против NAD83 / True North).
    """
    signed_deltas: list[tuple[float, float]] = []  # (axis_mod_180_deg, delta_arcsec)
    for bl_m in main_plan.boundary_lines:
        dx_m, dy_m = bl_m.end[0] - bl_m.start[0], bl_m.end[1] - bl_m.start[1]
        if math.hypot(dx_m, dy_m) < 1.0:
            continue
        az_m = math.degrees(math.atan2(dx_m, dy_m)) % 180.0
        best_delta: float | None = None
        for bl_n in nb_plan.boundary_lines:
            dx_n, dy_n = bl_n.end[0] - bl_n.start[0], bl_n.end[1] - bl_n.start[1]
            if math.hypot(dx_n, dy_n) < 1.0:
                continue
            az_n = math.degrees(math.atan2(dx_n, dy_n)) % 180.0
            delta = _signed_axis_diff_arcsec(az_m, az_n)
            if abs(delta) <= 5.0 * 3600.0:  # в пределах ±5°
                if best_delta is None or abs(delta) < abs(best_delta):
                    best_delta = delta
        if best_delta is not None:
            signed_deltas.append((az_m, best_delta))

    if len(signed_deltas) < 2:
        return None

    # Убеждаемся, что проверены хотя бы 2 неколлинеарных направления (напр. улица + боковая стена)
    has_distinct_axes = False
    for idx in range(len(signed_deltas)):
        for jdx in range(idx + 1, len(signed_deltas)):
            ang_sep = abs(signed_deltas[idx][0] - signed_deltas[jdx][0]) % 180.0
            ang_sep = min(ang_sep, 180.0 - ang_sep)
            if ang_sep >= 20.0:
                has_distinct_axes = True
                break

    if not has_distinct_axes:
        return None

    vals = [d for _, d in signed_deltas]
    mean_val = sum(vals) / len(vals)
    spread = max(vals) - min(vals)
    if abs(mean_val) >= 15.0 and spread <= max(25.0, 0.15 * abs(mean_val)):
        return mean_val
    return None


def _check_street_reference_and_frontage(
    main_plan: DrawPlan, nb_plan: DrawPlan
) -> tuple[str | None, str | None]:
    """
    Проверяет единство опорного перекрёстка ``(0, 0)`` и цепочку привязок вдоль фасадной улицы.
    Возвращает ``(street_tie_check_note, critical_basis_warning)``.
    """
    dm = main_plan.description
    dn = nb_plan.description
    ms_m = _normalize_street_key(dm.main_street)
    ms_n = _normalize_street_key(dn.main_street)
    cs_m = _normalize_street_key(dm.cross_street)
    cs_n = _normalize_street_key(dn.cross_street)

    if ms_m and ms_n and ms_m == ms_n:
        if cs_m and cs_n and cs_m != cs_n:
            warn = (
                f"Оба участка выходят на «{dm.main_street}», но отсчитываются от РАЗНЫХ "
                f"пересекаемых улиц («{dm.cross_street}» против «{dn.cross_street}») — "
                "точка (0, 0) у них разная, прямое наложение координат некорректно!"
            )
            return warn, warn

        # Проверяем румб линии привязки вдоль улицы (если у обоих > 0)
        tie_m = dm.corner_to_pob_bearing
        tie_n = dn.corner_to_pob_bearing
        if (
            tie_m is not None
            and tie_n is not None
            and getattr(tie_m, "has_bearing", True)
            and getattr(tie_n, "has_bearing", True)
            and dm.distance_to_corner_ft > 1e-6
            and dn.distance_to_corner_ft > 1e-6
        ):
            az_tm = bearing_azimuth_deg(
                tie_m.ns, tie_m.degrees, tie_m.minutes, tie_m.seconds, tie_m.ew
            )
            az_tn = bearing_azimuth_deg(
                tie_n.ns, tie_n.degrees, tie_n.minutes, tie_n.seconds, tie_n.ew
            )
            tie_diff_sec = abs(_signed_axis_diff_arcsec(az_tm, az_tn))
            if tie_diff_sec > 2.0:
                msg = (
                    f"Румб красной линии улицы «{dm.main_street}» отличается на "
                    f"{_format_arcsec_dms(tie_diff_sec)} ({tie_m.bearing_label} против "
                    f"{tie_n.bearing_label}) — точки POB расходятся уже на линии улицы!"
                )
                return msg, msg
        return (
            f"единая улица «{dm.main_street}»"
            + (f" от угла «{dm.cross_street}»" if dm.cross_street else ""),
            None,
        )

    if ms_m and ms_n and cs_m and cs_n and {ms_m, cs_m} == {ms_n, cs_n}:
        return (
            f"общий перекрёсток «{dm.main_street} × {dm.cross_street}» (угловое соседство)",
            None,
        )

    if ms_m and ms_n and ms_m != ms_n and cs_m and cs_n and cs_m != cs_n:
        warn = (
            f"Внимание: у основного участка перекрёсток «{dm.main_street} × {dm.cross_street}», "
            f"а у соседа «{dn.main_street} × {dn.cross_street}» — проверьте общую привязку к (0, 0)!"
        )
        return warn, warn

    return None, None


def verify_with_neighbors(
    main_plan: DrawPlan,
    neighbor_plans: Sequence[DrawPlan],
) -> NeighborVerificationReport:
    """
    Интеллектуальная геодезическая сверка основного участка со списком соседних участков.

    Анализирует:
      * совпадение опорного перекрёстка улиц и соосность фасадной линии;
      * все смежные (общие) стороны: разницу углов до секунд (с детекцией ошибок OCR,
        округления 80-х, перепутанных квадрантов и систематического поворота меридиана),
        разницу длин (с детекцией путаницы футов/дюймов, опечаток цифр и ступенчатых границ);
      * клиновидные зазоры (Gore), параллельные сдвиги и физическое наложение площадей (Overlap);
      * кросс-проверку замкнутости (кто именно ошибается — мы или сосед).
    """
    pair_reports: list[NeighborPairReport] = []
    total_shared = 0
    warn_total = 0
    err_total = 0
    same_winding_base = main_plan.ccw

    for nb_idx, nb_plan in enumerate(neighbor_plans, start=1):
        title = _neighbor_display_title(nb_plan, nb_idx)
        street_note, basis_err = _check_street_reference_and_frontage(main_plan, nb_plan)
        datum_rot = _detect_datum_rotation_arcsec(main_plan, nb_plan)
        overlap_sqft = polygon_intersection_area(main_plan.vertices, nb_plan.vertices)
        min_sep_ft = (
            0.0
            if overlap_sqft > 1e-4
            else _min_polygon_separation_ft(main_plan.vertices, nb_plan.vertices)
        )

        # Ищем кандидатов в общие границы между основным участком и этим соседом
        candidates: list[tuple[float, float, int, int, dict[str, Any]]] = []
        continuation_bends: list[str] = []

        for i_m, bl_m in enumerate(main_plan.boundary_lines):
            dx_m = bl_m.end[0] - bl_m.start[0]
            dy_m = bl_m.end[1] - bl_m.start[1]
            len_m = math.hypot(dx_m, dy_m)
            if len_m <= 1e-6:
                continue
            ux_m, uy_m = dx_m / len_m, dy_m / len_m

            for j_n, bl_n in enumerate(nb_plan.boundary_lines):
                dx_n = bl_n.end[0] - bl_n.start[0]
                dy_n = bl_n.end[1] - bl_n.start[1]
                len_n = math.hypot(dx_n, dy_n)
                if len_n <= 1e-6:
                    continue
                ux_n, uy_n = dx_n / len_n, dy_n / len_n

                dot = max(-1.0, min(1.0, ux_m * ux_n + uy_m * uy_n))
                is_opp = dot < 0.0
                axis_deg = math.degrees(math.acos(min(1.0, abs(dot))))
                axis_arcsec = axis_deg * 3600.0

                n_start_matched = bl_n.end if is_opp else bl_n.start
                n_end_matched = bl_n.start if is_opp else bl_n.end

                t0 = (n_start_matched[0] - bl_m.start[0]) * ux_m + (
                    n_start_matched[1] - bl_m.start[1]
                ) * uy_m
                t1 = (n_end_matched[0] - bl_m.start[0]) * ux_m + (
                    n_end_matched[1] - bl_m.start[1]
                ) * uy_m
                p0 = abs(
                    (n_start_matched[0] - bl_m.start[0]) * uy_m
                    - (n_start_matched[1] - bl_m.start[1]) * ux_m
                )
                p1 = abs(
                    (n_end_matched[0] - bl_m.start[0]) * uy_m
                    - (n_end_matched[1] - bl_m.start[1]) * ux_m
                )

                overlap_1d = max(0.0, min(len_m, max(t0, t1)) - max(0.0, min(t0, t1)))
                d_start = math.hypot(
                    bl_m.start[0] - n_start_matched[0],
                    bl_m.start[1] - n_start_matched[1],
                )
                d_end = math.hypot(
                    bl_m.end[0] - n_end_matched[0],
                    bl_m.end[1] - n_end_matched[1],
                )
                min_len = min(len_m, len_n)

                # Проверка 1: обычная (или смещённая / слегка повёрнутая) общая межа
                is_collinear_shared = (
                    axis_deg <= 15.0
                    and overlap_1d >= max(2.0, 0.18 * min_len)
                    and min(p0, p1) <= max(12.0, 0.22 * min_len)
                )
                # Проверка 2: «раскрывшаяся ножницами» общая межа от общей вершины
                # (например перепутан квадрант E↔W или грубая ошибка в десятках градусов)
                hinged_at_start = d_start <= 1.5 and (t1 - t0) > 0.25 * min_len
                hinged_at_end = d_end <= 1.5 and (t1 - t0) > 0.25 * min_len
                is_hinged_shared = (
                    not is_collinear_shared
                    and (hinged_at_start or hinged_at_end)
                    and axis_deg <= 65.0
                    and min_len >= 0.70 * max(len_m, len_n)
                )

                if is_collinear_shared or is_hinged_shared:
                    score_dist = min(p0, p1, d_start, d_end)
                    candidates.append(
                        (
                            (
                                round(score_dist, 2),
                                round(axis_deg, 3),
                                round(abs(len_m - len_n), 2),
                            ),
                            -overlap_1d,
                            i_m,
                            j_n,
                            {
                                "is_opp": is_opp,
                                "axis_deg": axis_deg,
                                "axis_arcsec": axis_arcsec,
                                "d_start": d_start,
                                "d_end": d_end,
                                "overlap_1d": max(overlap_1d, min_len if is_hinged_shared else 0.0),
                                "p0": p0,
                                "p1": p1,
                            },
                        )
                    )
                    continue

                # Проверка 3: две соосные фасадные / задние стороны, стыкующиеся углом в продолжение
                corner_touch = min(
                    math.hypot(bl_m.start[0] - bl_n.end[0], bl_m.start[1] - bl_n.end[1]),
                    math.hypot(bl_m.end[0] - bl_n.start[0], bl_m.end[1] - bl_n.start[1]),
                    math.hypot(bl_m.start[0] - bl_n.start[0], bl_m.start[1] - bl_n.start[1]),
                    math.hypot(bl_m.end[0] - bl_n.end[0], bl_m.end[1] - bl_n.end[1]),
                )
                if corner_touch <= 0.5 and overlap_1d < 0.5 and 2.0 < axis_arcsec <= 10.0 * 3600.0:
                    seg_m_c = main_plan.description.segments[i_m]
                    seg_n_c = nb_plan.description.segments[j_n]
                    continuation_bends.append(
                        f"Излом на продолжении общей линии границ (наша сторона #{i_m + 1} "
                        f"«{_segment_display_bearing(seg_m_c, bl_m)}» и сторона соседа #{j_n + 1} "
                        f"«{_segment_display_bearing(seg_n_c, bl_n)}»): угловое расхождение "
                        f"{_format_arcsec_dms(axis_arcsec)}!"
                    )

        # Жадный выбор лучших попарных соответствий общих стен
        candidates.sort(key=lambda item: (item[0], item[1]))
        used_m: set[int] = set()
        used_n: set[int] = set()
        shared_checks: list[SharedEdgeCheck] = []

        for _, _, i_m, j_n, meta in candidates:
            if i_m in used_m or j_n in used_n:
                continue
            used_m.add(i_m)
            used_n.add(j_n)

            seg_m = main_plan.description.segments[i_m]
            seg_n = nb_plan.description.segments[j_n]
            bl_m = main_plan.boundary_lines[i_m]
            bl_n = nb_plan.boundary_lines[j_n]

            len_m = float(seg_m.distance_ft)
            len_n = float(seg_n.distance_ft)
            len_diff = len_m - len_n
            abs_len_diff = abs(len_diff)
            ang_arcsec = float(meta["axis_arcsec"])
            ang_deg = float(meta["axis_deg"])
            d_start = float(meta["d_start"])
            d_end = float(meta["d_end"])
            is_opp = bool(meta["is_opp"])

            issue_codes: list[str] = []
            diagnoses: list[str] = []

            # А. Диагностика угла
            ang_codes, ang_notes = _diagnose_angle_discrepancy(
                seg_m,
                seg_n,
                ang_arcsec,
                min(len_m, len_n),
                is_opp,
                same_winding=(same_winding_base == nb_plan.ccw),
            )
            issue_codes.extend(ang_codes)
            diagnoses.extend(ang_notes)

            if datum_rot is not None and ang_arcsec >= 10.0:
                if "DATUM_ROTATION" not in issue_codes:
                    issue_codes.append("DATUM_ROTATION")
                diagnoses.append(
                    f"Примечание: угловое расхождение этой стены укладывается в общий поворот "
                    f"базиса меридиана соседа ({_format_arcsec_dms(datum_rot)}) — вероятно, "
                    "разная система отсчёта румбов (эпоха съёмки), а не одиночная опечатка."
                )

            # Б. Диагностика длины
            len_codes, len_notes = _diagnose_length_discrepancy(
                len_m,
                len_n,
                main_closed=(main_plan.closure.distance <= 0.03),
                nb_closed=(nb_plan.closure.distance <= 0.03),
                start_aligned=(min(d_start, d_end) <= 0.04),
                angle_aligned=(ang_arcsec <= 30.0),
            )
            issue_codes.extend(len_codes)
            diagnoses.extend(len_notes)

            # В. Параллельный сдвиг / щель (Gore) / клин (Wedge)
            min_end = min(d_start, d_end)
            max_end = max(d_start, d_end)
            if ang_arcsec <= 15.0 and abs_len_diff <= 0.05 and min_end > 0.03:
                issue_codes.append("PARALLEL_SHIFT")
                kind_shift = (
                    "захлёст/наложение (Overlap)"
                    if overlap_sqft > 0.05
                    else "незамкнутая щель (Gore / Gap)"
                )
                diagnoses.append(
                    f"Параллельный сдвиг общей стены: сам румб и длина сходятся, но линии "
                    f"смещены друг относительно друга на {min_end:.2f}' ({min_end * 12.0:.1f}\") — "
                    f"образуется {kind_shift}. Проверьте расстояние POB от перекрёстка улиц!"
                )
            elif min_end <= 0.04 and max_end > 0.05 and ang_arcsec > 5.0:
                issue_codes.append("WEDGE_DIVERGENCE")
                wedge_kind = (
                    "клиновидный заступ внутрь участка (Encroachment)"
                    if overlap_sqft > 0.05
                    else "клиновидная щель (Gore)"
                )
                diagnoses.append(
                    f"Веерное расхождение от общей угловой точки: один конец совпадает "
                    f"({min_end:.3f}'), а второй уходит на {max_end:.2f}' ({max_end * 12.0:.1f}\"), "
                    f"формируя {wedge_kind}."
                )

            # Г. Кросс-проверка через собственную невязку участков (кто именно виноват)
            vx_m = bl_m.end[0] - bl_m.start[0]
            vy_m = bl_m.end[1] - bl_m.start[1]
            vx_n = (
                (bl_n.start[0] - bl_n.end[0])
                if is_opp
                else (bl_n.end[0] - bl_n.start[0])
            )
            vy_n = (
                (bl_n.start[1] - bl_n.end[1])
                if is_opp
                else (bl_n.end[1] - bl_n.start[1])
            )
            vec_diff = math.hypot(vx_m - vx_n, vy_m - vy_n)
            culprit, culprit_msg = _attribute_error_by_closure(
                main_plan.closure.distance, nb_plan.closure.distance, vec_diff
            )
            if culprit_msg:
                diagnoses.append(culprit_msg)

            # Итоговая оценка серьёзности для данной общей стены
            lateral_from_ang = min(len_m, len_n) * math.sin(math.radians(ang_deg))
            if (
                ang_arcsec <= 1.0
                and abs_len_diff <= 0.015
                and max_end <= 0.025
                and "SAME_DIRECTION_VECTOR" not in issue_codes
            ):
                edge_status = "ok"
                diagnoses.append("Полное геодезическое согласие угла, длины и координат стыка.")
            elif (
                ang_arcsec > 120.0
                or lateral_from_ang > 0.08
                or (abs_len_diff > 0.10 and "STEPPED_BOUNDARY_DEPTH" not in issue_codes)
                or max_end > 0.12
                or "QUADRANT_LETTER_TYPO" in issue_codes
                or "DEGREES_TYPO" in issue_codes
                or "SAME_DIRECTION_VECTOR" in issue_codes
            ):
                edge_status = "error"
            else:
                edge_status = "warn"

            shared_checks.append(
                SharedEdgeCheck(
                    main_seg_index=i_m + 1,
                    neighbor_seg_index=j_n + 1,
                    main_bearing=_segment_display_bearing(seg_m, bl_m),
                    neighbor_bearing=_segment_display_bearing(seg_n, bl_n),
                    main_length_ft=len_m,
                    neighbor_length_ft=len_n,
                    is_opposite_direction=is_opp,
                    angle_diff_deg=ang_deg,
                    angle_diff_arcsec=ang_arcsec,
                    angle_diff_dms=_format_arcsec_dms(ang_arcsec),
                    length_diff_ft=len_diff,
                    abs_length_diff_ft=abs_len_diff,
                    lateral_offset_start_ft=d_start,
                    lateral_offset_end_ft=d_end,
                    overlap_span_ft=float(meta["overlap_1d"]),
                    main_line=(bl_m.start, bl_m.end),
                    neighbor_line=(bl_n.start, bl_n.end),
                    status=edge_status,
                    issue_codes=issue_codes,
                    diagnoses=diagnoses,
                    likely_culprit=culprit,
                )
            )

        shared_checks.sort(key=lambda c: c.main_seg_index)
        total_shared += len(shared_checks)

        # Отсекаем микроскопические «волосяные» клинья (< 0.025' ≈ 0.3" эффективной толщины),
        # возникающие при секундном округлении на длинных 100-футовых стенах
        ref_span = max([c.overlap_span_ft for c in shared_checks] or [40.0])
        eff_overlap_width = overlap_sqft / max(ref_span, 10.0)
        if overlap_sqft <= 0.5 or eff_overlap_width <= 0.025:
            overlap_sqft = 0.0
            eff_overlap_width = 0.0

        findings: list[str] = []
        for bend_msg in continuation_bends:
            if bend_msg not in findings:
                findings.append(bend_msg)

        if not shared_checks:
            if overlap_sqft > 0.5:
                findings.append(
                    f"Контуры пересекаются (площадь наложения {overlap_sqft:.2f} sq ft), "
                    "но совпадающих по направлению межевых стен не найдено — проверьте привязку POB и масштаб!"
                )
            elif min_sep_ft <= 15.0:
                findings.append(
                    f"Общая стена вплотную не состыковалась: между основным участком и соседом "
                    f"зазор {min_sep_ft:.2f}' ({min_sep_ft * 12.0:.1f}\") — проверьте расстояние "
                    "привязки от перекрёстка (distance_to_corner_ft)!"
                )
            else:
                findings.append(
                    f"Участок соседа не имеет общей границы с основным (минимальное расстояние "
                    f"между контурами = {min_sep_ft:.2f}'). Возможно, между ними расположен "
                    "промежуточный участок или ошибка в расстоянии до угла улицы."
                )

        # Определяем общий статус по этому соседу
        pair_err = sum(1 for c in shared_checks if c.status == "error")
        pair_warn = sum(1 for c in shared_checks if c.status == "warn")
        if basis_err is not None or eff_overlap_width > 0.12 or overlap_sqft > 6.0:
            pair_err += 1
        elif overlap_sqft > 0.5:
            pair_warn += 1
        if continuation_bends:
            pair_warn += len(continuation_bends)
        if not shared_checks and overlap_sqft <= 0.5:
            if min_sep_ft <= 15.0:
                pair_err += 1
            else:
                pair_warn += 1

        if pair_err > 0:
            pair_status = "error"
        elif pair_warn > 0:
            pair_status = "warn"
        else:
            pair_status = "ok"

        err_total += pair_err
        warn_total += pair_warn

        pair_reports.append(
            NeighborPairReport(
                neighbor_index=nb_idx,
                neighbor_title=title,
                status=pair_status,
                shared_edges=shared_checks,
                overlap_area_sqft=overlap_sqft,
                min_poly_distance_ft=min_sep_ft,
                datum_rotation_arcsec=datum_rot,
                street_tie_check=street_note,
                findings=findings,
            )
        )

    if err_total > 0:
        overall = "error"
    elif warn_total > 0:
        overall = "warn"
    else:
        overall = "ok"

    return NeighborVerificationReport(
        overall_status=overall,
        pair_reports=pair_reports,
        total_shared_edges=total_shared,
        warning_count=warn_total,
        error_count=err_total,
    )

