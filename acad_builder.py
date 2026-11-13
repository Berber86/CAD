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
    "TextItem",
    "AcadBuilder",
    "bearing_azimuth_deg",
    "bearing_to_xy",
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
    "polygon_signed_area",
    "round_ft",
    "text_rotation",
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
    arcs: list[ArcItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ccw: bool = True
    pob: tuple[float, float] = (0.0, 0.0)
    is_neighbor: bool = False

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

    @property
    def closure(self) -> ClosureError:
        return self.plan.closure

    @property
    def vertices(self) -> list[tuple[float, float]]:
        return self.plan.vertices

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
        lines = [
            f'Масштаб 1"={self.scale:g}\'{role_tag}, высота текста {self.text_height:.3f}',
            f"POB от перекрёстка (0, 0): ({self.pob[0]:+.2f}, {self.pob[1]:+.2f}) ft",
            f"Сторон: {self.segment_count}, подписей создано: {self.text_count}, "
            f"площадь ≈ {self.area_sqft:,.0f} sq ft",
            self.closure.report(),
            layer_summary,
        ]
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
        """Считает геометрию от перекрёстка (0.0, 0.0) и раскладку подписей, ничего не рисуя."""
        cfg = self.drawing_cfg
        scale = float(scale if scale is not None else cfg.get("base_scale", 16.0))
        height = (
            float(text_height)
            if text_height is not None
            else self.text_height_for_scale(scale, self.cfg)
        )

        pob = compute_pob_point(description)
        segments = description.segments
        traverse = build_polygon_points(segments, start=pob)
        if len(traverse) < 4:
            raise AcadDrawError(
                f"для замкнутого участка нужно минимум 3 стороны, получено {len(segments)}"
            )
        vertices = traverse[:-1]
        closure = closure_error(traverse)
        warnings: list[str] = []

        if closure.distance > CLOSURE_TOLERANCE_FT:
            warnings.append(
                f"описание не замыкается точно: невязка {closure.distance:.3f} ft — "
                "контур замкнут по замыкающей линии"
            )
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
        self._front_warning = None
        front_index = self._front_side_index(description, vertices, ccw)

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
                description, vertices, height, ccw, warnings, front_index=front_index
            )
            street_texts, row_lines = self._street_items(
                description,
                vertices,
                height,
                ccw,
                warnings,
                side_band_extra=side_band_extra,
                front_index=front_index,
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
            arcs=arcs,
            warnings=warnings,
            ccw=ccw,
            pob=pob,
            is_neighbor=bool(is_neighbor),
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
        self, model_space: Any, points: Sequence[tuple[float, float]], layer: str
    ) -> Any:
        polyline = _com_retry(model_space.AddLightWeightPolyline, _variant_points(points))
        polyline.Layer = layer
        polyline.Closed = True
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

            polyline = self._add_polyline(model_space, plan.vertices, poly_target_layer)

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
                handle = str(polyline.Handle)
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
