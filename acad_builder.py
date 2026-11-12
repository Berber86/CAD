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
import time
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable, Iterable, Sequence

from llm_parser import LegalDescription, Segment, load_config

__all__ = [
    "AcadBuilderError",
    "AcadConnectionError",
    "AcadDrawError",
    "ClosureError",
    "DrawPlan",
    "DrawResult",
    "TextItem",
    "AcadBuilder",
    "bearing_azimuth_deg",
    "bearing_to_xy",
    "build_polygon_points",
    "build_side_label",
    "build_side_lines",
    "closure_error",
    "draw_legal_description",
    "estimate_text_width",
    "format_bearing",
    "format_distance",
    "point_in_polygon",
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
    Учитывает, что управляющий код ``%%d`` в AutoCAD рендерится одним символом ``°``.
    """
    display = text.replace("%%d", "°").replace("%%p", "±").replace("%%c", "Ø")
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


def build_side_lines(
    segment: Segment,
    text_height: float,
    side_length: float,
    cfg: dict[str, Any],
) -> tuple[list[str], bool]:
    """
    Формирует 1 или 2 параллельные строки подписи стороны (от ближней к контуру к дальней):
      * для сторон без компасного румба (``has_bearing=False``, Queens/Brooklyn) —
        ``[длина]`` или ``[длина, course_note]``;
      * для узких сторон таунхаусов (``<= stack_short_side_ft``, где ``bearing + distance``
        в одну строку шире 85% стороны, например 13.67' и 14.00') —
        2 строки ``[длина, румб]``;
      * для обычных/длинных сторон — 1 строка из ``build_side_label`` (с растяжкой > 40 ft).
    """
    use_control = bool(cfg.get("degrees_as_control_code", True))
    distance = format_distance(segment.distance_ft)
    if not getattr(segment, "has_bearing", True):
        lines = [distance]
        note = getattr(segment, "course_note", None)
        if note:
            clean_note = str(note).upper()
            if use_control:
                clean_note = clean_note.replace("°", "%%d")
            lines.append(clean_note)
        return lines, False

    single_line, stretched = build_side_label(segment, text_height, side_length, cfg)
    if stretched:
        return [single_line], True

    stack_limit = float(cfg.get("stack_short_side_ft", 30.0))
    single_width = estimate_text_width(single_line, text_height, cfg)
    if side_length <= stack_limit or single_width > side_length * 0.75:
        bearing = format_bearing(segment, use_control_code=use_control)
        return [distance, bearing], False

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
class DrawPlan:
    """Полный план отрисовки — результат чистой математики, без COM."""

    description: LegalDescription
    scale: float
    text_height: float
    vertices: list[tuple[float, float]]
    closure: ClosureError
    items: list[TextItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ccw: bool = True

    @property
    def area_sqft(self) -> float:
        return abs(polygon_signed_area(self.vertices))


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
        lines = [
            f'Масштаб 1"={self.scale:g}\', высота текста {self.text_height:.3f}',
            f"Сторон: {self.segment_count}, подписей создано: {self.text_count}, "
            f"площадь ≈ {self.area_sqft:,.0f} sq ft",
            self.closure.report(),
            f"Слои: {self.layers.get('property_line')} / {self.layers.get('boundary_text')}",
        ]
        for warning in list(self.plan.warnings) + list(self.warnings):
            lines.append(f"! {warning}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Низкоуровневый COM-хелпер
# ---------------------------------------------------------------------------


def _co_initialize() -> None:
    try:
        import pythoncom
    except ImportError as exc:  # pragma: no cover - Windows only
        raise AcadConnectionError(
            "pywin32 не установлен. В Windows выполните: pip install pywin32"
        ) from exc
    try:
        pythoncom.CoInitialize()
    except Exception:  # noqa: BLE001 - уже инициализирован в этом потоке
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

    def _interior_angle_point(
        self,
        vertex_index: int,
        points: Sequence[tuple[float, float]],
        step: float,
    ) -> tuple[float, float]:
        """Точка внутри угла многоугольника по биссектрисе (для подписи 93°33'29.5")."""
        count = len(points)
        curr = points[vertex_index % count]
        prev_pt = points[(vertex_index - 1) % count]
        next_pt = points[(vertex_index + 1) % count]
        dx1, dy1 = prev_pt[0] - curr[0], prev_pt[1] - curr[1]
        dx2, dy2 = next_pt[0] - curr[0], next_pt[1] - curr[1]
        len1 = math.hypot(dx1, dy1) or 1.0
        len2 = math.hypot(dx2, dy2) or 1.0
        bx = dx1 / len1 + dx2 / len2
        by = dy1 / len1 + dy2 / len2
        blen = math.hypot(bx, by)
        if blen < 1e-6:
            bx, by = -(dy2 / len2), dx2 / len2
        else:
            bx, by = bx / blen, by / blen
        candidate = (curr[0] + bx * step, curr[1] + by * step)
        if not point_in_polygon(candidate, points):
            candidate = (curr[0] - bx * step, curr[1] - by * step)
        return candidate

    def _side_items(
        self,
        segments: Sequence[Segment],
        points: Sequence[tuple[float, float]],
        text_height: float,
        ccw: bool,
        warnings: list[str],
    ) -> tuple[list[TextItem], dict[int, int]]:
        cfg = self.drawing_cfg
        offset = float(cfg.get("text_offset_factor", 1.5)) * text_height
        line_gap = float(cfg.get("stacked_line_gap_factor", 1.45)) * text_height
        use_control = bool(cfg.get("degrees_as_control_code", True))
        items: list[TextItem] = []
        line_counts: dict[int, int] = {}
        count = len(points)
        for index, segment in enumerate(segments):
            start = points[index % count]
            end = points[(index + 1) % count]
            nx, ny, length = self._outward_normal(start, end, ccw)
            if length < 1e-9:
                warnings.append(f"сторона #{index + 1}: нулевая длина — пропущена")
                continue
            lines, _ = build_side_lines(segment, text_height, length, cfg)
            line_counts[index] = len(lines)
            max_line_width = max(
                estimate_text_width(line_text, text_height, cfg) for line_text in lines
            )
            if max_line_width > length > 0:
                warnings.append(
                    f"подпись стороны #{index + 1} (≈{max_line_width:.1f} ед.) длиннее самой "
                    f"стороны ({length:.1f} ft) — уменьшите масштаб/высоту текста"
                )
            angle = math.atan2(end[1] - start[1], end[0] - start[0])
            rot = text_rotation(angle)
            midpoint = ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)
            for row, line_text in enumerate(lines):
                row_offset = offset + row * line_gap
                kind = f"side-{index + 1}" if row == 0 else f"side-{index + 1}-note"
                items.append(
                    TextItem(
                        text=line_text,
                        point=(midpoint[0] + nx * row_offset, midpoint[1] + ny * row_offset),
                        rotation=rot,
                        kind=kind,
                    )
                )

            ia_raw = getattr(segment, "interior_angle", None)
            if ia_raw:
                ia_text = str(ia_raw).replace("°", "%%d") if use_control else str(ia_raw)
                ia_point = self._interior_angle_point(index, points, step=2.4 * text_height)
                items.append(
                    TextItem(
                        text=ia_text,
                        point=ia_point,
                        rotation=0.0,
                        kind=f"angle-{index + 1}",
                        height=round(text_height * 0.85, 4),
                    )
                )
        return items, line_counts

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
        2. Если задан ``front_side`` (N/S/E/W — куда смотрит фасад), выбираем
           сторону с максимальной сонаправленностью внешней нормали (``score``);
           длина используется только как тай-брейк при почти равном ``score``.
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
            candidates: list[tuple[float, float, int]] = []
            for index in range(len(description.segments)):
                nx, ny, length = self._outward_normal(
                    points[index % count], points[(index + 1) % count], ccw
                )
                if length < 1e-9:
                    continue
                score = nx * target[0] + ny * target[1]
                if score > 0.35:
                    candidates.append((score, length, index))
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
        line_counts: dict[int, int] | None = None,
    ) -> list[TextItem]:
        cfg = self.drawing_cfg
        items: list[TextItem] = []
        front_index = self._front_side_index(description, points, ccw)
        if front_index is None:
            warnings.append(
                "не удалось определить фасадную сторону — подписи улиц не нанесены"
            )
            return items

        count = len(points)
        start = points[front_index]
        end = points[(front_index + 1) % count]
        nx, ny, length = self._outward_normal(start, end, ccw)
        if length < 1e-9:
            warnings.append("фасадная сторона имеет нулевую длину — подписи улиц пропущены")
            return items

        angle = math.atan2(end[1] - start[1], end[0] - start[0])
        midpoint = ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)
        base_street_offset = float(cfg.get("street_offset_factor", 3.2)) * text_height
        line_gap = float(cfg.get("stacked_line_gap_factor", 1.45)) * text_height
        front_extra = max(0, (line_counts or {}).get(front_index, 1) - 1) * line_gap
        street_offset = base_street_offset + front_extra

        if description.main_street:
            items.append(
                TextItem(
                    text=description.main_street.upper(),
                    point=(
                        midpoint[0] + nx * street_offset,
                        midpoint[1] + ny * street_offset,
                    ),
                    rotation=text_rotation(angle),
                    kind="main-street",
                )
            )
            street_note = getattr(description, "main_street_note", None)
            if street_note:
                note_offset = street_offset + 1.5 * text_height
                items.append(
                    TextItem(
                        text=str(street_note).upper(),
                        point=(
                            midpoint[0] + nx * note_offset,
                            midpoint[1] + ny * note_offset,
                        ),
                        rotation=text_rotation(angle),
                        kind="main-street-note",
                        height=round(text_height * 0.82, 4),
                    )
                )
        else:
            warnings.append("main_street пуст — название фасадной улицы не нанесено")

        if not description.cross_street:
            return items

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
                c_extra = max(0, (line_counts or {}).get(side_idx, 1) - 1) * line_gap
                c_offset = base_street_offset + c_extra
                items.append(
                    TextItem(
                        text=description.cross_street.upper(),
                        point=(c_mid[0] + cnx * c_offset, c_mid[1] + cny * c_offset),
                        rotation=text_rotation(c_angle),
                        kind="cross-street",
                    )
                )
                return items

        # Рядовой участок: пересекаемая улица ориентирована перпендикулярно
        # фасадной за углом, а расстояние привязки («150.00' TO CORNER» или tie_note)
        # выносится вдоль фасадной улицы, чтобы Zoom Extents не сжимал чертёж.
        ux = (end[0] - start[0]) / length
        uy = (end[1] - start[1]) / length
        gap = float(cfg.get("corner_label_gap_factor", 2.0)) * text_height
        cross_width = estimate_text_width(
            description.cross_street.upper(), text_height, cfg
        )
        anchor = (
            start[0] - ux * (gap + text_height) + nx * street_offset,
            start[1] - uy * (gap + text_height) + ny * street_offset,
        )
        items.append(
            TextItem(
                text=description.cross_street.upper(),
                point=anchor,
                rotation=text_rotation(angle + math.pi / 2.0),
                kind="cross-street",
            )
        )
        tie_note = getattr(description, "tie_note", None)
        if tie_note or description.distance_to_corner_ft is not None:
            distance_offset = float(cfg.get("corner_distance_offset_factor", 1.7)) * text_height
            dist_label = (
                str(tie_note).upper()
                if tie_note
                else f"{format_distance(float(description.distance_to_corner_ft))} TO CORNER"
            )
            dist_shift = max(gap + cross_width * 0.35, 3.0 * text_height)
            items.append(
                TextItem(
                    text=dist_label,
                    point=(
                        start[0] - ux * dist_shift + nx * (street_offset + distance_offset),
                        start[1] - uy * dist_shift + ny * (street_offset + distance_offset),
                    ),
                    rotation=text_rotation(angle),
                    kind="cross-street-distance",
                )
            )
        return items

    def plan(
        self,
        description: LegalDescription,
        scale: float | None = None,
        text_height: float | None = None,
    ) -> DrawPlan:
        """Считает геометрию и раскладку подписей, ничего не рисуя."""
        cfg = self.drawing_cfg
        scale = float(scale if scale is not None else cfg.get("base_scale", 16.0))
        height = (
            float(text_height)
            if text_height is not None
            else self.text_height_for_scale(scale, self.cfg)
        )

        segments = description.segments
        traverse = build_polygon_points(segments)
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

        ccw = polygon_signed_area(vertices) > 0.0
        self._front_warning = None
        items, line_counts = self._side_items(segments, vertices, height, ccw, warnings)
        items.extend(
            self._street_items(
                description, vertices, height, ccw, warnings, line_counts=line_counts
            )
        )
        if self._front_warning:
            warnings.append(self._front_warning)

        return DrawPlan(
            description=description,
            scale=scale,
            text_height=height,
            vertices=vertices,
            closure=closure,
            items=items,
            warnings=warnings,
            ccw=ccw,
        )

    # -- подключение к AutoCAD ---------------------------------------------
    def connect(self) -> tuple[Any, Any]:
        """
        Возвращает (Application, ActiveDocument) уже запущенного AutoCAD.

        Если приложение/документ были внедрены снаружи (тесты, повторная
        отрисовка), соединение переиспользуется, а проверка единиц чертежа
        всё равно выполняется.
        """
        if self._app is not None and self._doc is not None:
            if self._unit_warning is None:
                self._warn_about_units(self._doc)
            return self._app, self._doc
        _co_initialize()
        try:
            import win32com.client
        except ImportError as exc:  # pragma: no cover
            raise AcadConnectionError(
                "pywin32 не установлен. В Windows выполните: pip install pywin32"
            ) from exc

        try:
            app = win32com.client.GetActiveObject(ACAD_PROGID)
        except Exception as exc:  # noqa: BLE001
            raise AcadConnectionError(
                "Не удалось подключиться к запущенному AutoCAD.\n"
                "1) Откройте AutoCAD и нужный DWG (не стартовый экран);\n"
                "2) Дождитесь полной загрузки чертежа;\n"
                f"3) Повторите попытку. Детали: {exc}"
            ) from exc

        try:
            doc = app.ActiveDocument
        except Exception as exc:  # noqa: BLE001
            raise AcadConnectionError(
                "AutoCAD запущен, но активный документ недоступен — откройте DWG."
            ) from exc
        if doc is None:
            raise AcadConnectionError(
                "AutoCAD запущен, но активный документ недоступен — откройте DWG."
            )
        self._app, self._doc = app, doc
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
        def layer_names() -> list[str]:
            return [str(layer.Name) for layer in doc.Layers]

        if name not in layer_names():
            try:
                _com_retry(doc.Layers.Add, name)
            except AcadDrawError:
                pass  # слой мог быть создан параллельно
        if name not in layer_names():
            return "0"
        layer = doc.Layers.Item(name)
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
            if style_name not in [str(style.Name) for style in styles]:
                styles.Add(style_name)
            try:
                # Romans — стандартный чертёжный шрифт для подписей линий.
                styles.Item(style_name).SetFont("romans.shx", "", 0.0, 1.0, 0.0)
            except Exception:  # noqa: BLE001 - шрифта может не быть в системе
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
    ) -> DrawResult:
        """Планирует и рисует участок в активном документе AutoCAD."""
        plan = self.plan(description, scale=scale, text_height=text_height)
        warnings: list[str] = []

        drawing = self.drawing_cfg
        line_layer_name, text_layer_name = self.layer_names()
        colors = self.cfg.get("layer_colors", {})

        app, doc = self.connect()
        # Предупреждение о единицах чертежа появляется именно при подключении.
        if self._unit_warning:
            warnings.append(self._unit_warning)
        try:
            model_space = doc.ModelSpace
        except Exception as exc:  # noqa: BLE001
            raise AcadDrawError(f"не удалось получить ModelSpace: {exc}") from exc

        line_layer = self._ensure_layer(
            doc, line_layer_name, int(colors.get("property_line", 1))
        )
        text_layer = self._ensure_layer(
            doc, text_layer_name, int(colors.get("boundary_text", 3))
        )
        if line_layer == "0":
            warnings.append(f"слой {line_layer_name} недоступен — линия нарисована в слое 0")
        if text_layer == "0":
            warnings.append(f"слой {text_layer_name} недоступен — текст нарисован в слое 0")
        style = self._ensure_text_style(doc)

        polyline = self._add_polyline(model_space, plan.vertices, line_layer)

        created = 0
        for item in plan.items:
            try:
                self._add_text(
                    model_space, item, plan.text_height, text_layer, style
                )
                created += 1
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"не удалось создать текст «{item.text}»: {exc}")

        if drawing.get("zoom_extents", True):
            try:
                _com_retry(app.ZoomExtents)
            except Exception:  # noqa: BLE001 - зум не критичен
                try:
                    doc.SendCommand("_.ZOOM\n_E\n")
                except Exception:  # noqa: BLE001
                    warnings.append("не удалось выполнить Zoom Extents")

        handle: str | None = None
        try:
            handle = str(polyline.Handle)
        except Exception:  # noqa: BLE001
            pass

        return DrawResult(
            plan=plan,
            text_count=created,
            layers={"property_line": line_layer, "boundary_text": text_layer},
            handle=handle,
            warnings=warnings,
        )


def draw_legal_description(
    description: LegalDescription,
    cfg: dict[str, Any] | None = None,
    scale: float | None = None,
    text_height: float | None = None,
) -> DrawResult:
    """Отрисовать участок, не создавая AcadBuilder вручную."""
    return AcadBuilder(cfg).draw(description, scale=scale, text_height=text_height)
