"""
Шаг 4 — оффсеты: подписи расстояний от дома, заборов и стен до границы участка.

Черновик (версия 4.5): оффсеты в области, выделенной в AutoCAD, запись в чертёж. Правила заказчика, которые уже учтены:

- граница для оффсетов на фронте — ``M-ROW-LINE`` (актуальная граница улицы и участка);
  ``M-TAX-LINE`` — линия расширения улицы по проекту, границей участка не считается;
- оффсет можно записать тремя способами: размерная подпись без выноски (``\\A2;27.6'``),
  выноска (``FE.0.6'N``), текст без стрелки (``3.2'S`` — для зданий и гаражей);
- ``FE.0.4'N`` — оффсет от забора до границы участка (property line).

Что уже есть:

- разбор эталонных дампов финального чертежа заказчика: какие оффсеты он поставил и в каком виде
  (размерная подпись ``\\A2;27.6'`` или выноска ``FE.0.6'N`` / ``BLDG 5.2'W``);
- проверка, что два дампа сняты в одной системе координат (``same_frame``): без неё сравнение
  «до» и «после» по handle не работает (handle в разных файлах переиспользуются);
- измерение расстояния от вершин объектов до границы участка (``measure_candidates``);
- высоты текста оффсетов по масштабу (``offset_height_ft``).

Черновик 4.3 (первая версия, без правил по 2-му и 3-му дампам):

- геодезист заранее выделяет в AutoCAD область (рамкой или замкнутой полилинией), затем нажимает кнопку
  программы; оффсеты ставятся только для объектов внутри области;
- дом и сарай — текст без выноски (``3.2'S``, высота 1.1), а если места нет — выноска ``BLDG 2.0'E``;
- забор и стена — выноска ``FE.0.4'N`` / ``WALL 0.5'E`` (высота 0.9);
- расстояние — до контура участка и до фронтовых линий ``M-ROW-LINE``; ``M-TAX-LINE`` не граница;
- текст не накрывает линии объектов, границы и другие подписи; одинаковая подпись рядом не дублируется;
- запись — слой D-TOPO-TEXT одной отменяемой группой (один Undo).

Пока не сделано: размерные подписи (``27.6'``), сверка с дампами «до/после», проблема полной границы
участка (отложена по решению пользователя).

Запуск разбора эталона из корня репозитория::

    python offsets.py "docs/225 Aspinwall Street, Staten Island.final.dump.json"
"""

from __future__ import annotations

import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

#: Базовый масштаб заказчика (1"=16'); высоты текста ниже даны для него.
BASE_SCALE = 16.0
#: Подписи объектов внутри property.
LABEL_HEIGHT_FT = 0.9
#: Оффсеты на дом (правило заказчика: 1.1 вместо 0.9).
HOUSE_OFFSET_HEIGHT_FT = 1.1
#: Слой, на котором заказчик пишет оффсеты (в финальных дампах).
OFFSET_LAYER = "D-TOPO-TEXT"
#: Фронтовая граница участка — актуальная граница улицы и участка (ответ заказчика, 4.1).
FRONT_BOUNDARY_LAYER = "M-ROW-LINE"
#: Линия расширения улицы по проекту — НЕ граница участка для оффсетов (ответ заказчика, 4.1).
WIDENING_LINE_LAYER = "M-TAX-LINE"
#: Версия каркаса шага 4 (для окна программы).
STEP4_VERSION = "4.14"
#: Допуск совпадения координат двух дампов, футы.
SAME_FRAME_TOL_FT = 0.01

Point = tuple[float, float]


# --------------------------------------------------------------------------- тексты и высоты


def offset_height_ft(kind: str, scale: float = BASE_SCALE) -> float:
    """Высота текста оффсета: 1.1 на дом, 0.9 на остальное; пересчёт по масштабу пропорционально."""
    base = HOUSE_OFFSET_HEIGHT_FT if kind == "house" else LABEL_HEIGHT_FT
    return round(base * scale / BASE_SCALE, 4)


_CODE_ALIGN_RE = re.compile(r"\\A\d+;")
_CODE_PARA_RE = re.compile(r"\\p[^;]*;")
_CODE_FORMAT_RE = re.compile(r"\\[HhWwFfCcQqTtLlOo][^;]*;")


def clean_text(raw: str) -> str:
    """Убирает служебные коды AutoCAD MText (``\\A2;``, ``\\pxqc;``, ``\\P``, ``{\\H1.2x;…}``)."""
    text = _CODE_ALIGN_RE.sub("", raw)
    text = _CODE_PARA_RE.sub("", text)
    text = text.replace("\\P", " ")
    text = _CODE_FORMAT_RE.sub("", text)
    text = text.replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", text).strip()


#: «FE.0.6'N», «WALL 0.1'E», «BLDG 5.2'W», «FE.0.5'W FE.0.9'N» — метка, расстояние в футах, сторона.
_PART_RE = re.compile(r"(?P<label>[A-Z][A-Z./]*)?\s*(?P<value>\d+\.\d+)'\s*(?P<dir>[NSEW])\b")
_DIM_RE = re.compile(r"^(?P<value>\d+(?:\.\d+)?)'$")


@dataclass(frozen=True)
class OffsetPart:
    """Одна часть подписи: метка (FE., WALL, BLDG), расстояние и сторона."""

    label: str | None
    value_ft: float | None
    direction: str | None


@dataclass(frozen=True)
class ObservedOffset:
    """Оффсет, который заказчик уже поставил на финальном чертеже (эталон для правил)."""

    handle: str
    style: str  # "dimension" — размерная подпись \A2;N'; "leader" — с выноской; "text" — без выноски (текстом)
    text: str  # очищенный текст
    raw: str  # как в дампе
    parts: tuple[OffsetPart, ...]
    height_ft: float
    leader_handle: str | None
    pt: Point
    rot_deg: float
    line_mark: bool = False  # «O/L» — подпись «по линии», без числа


def _leader_map(entities: Sequence[dict[str, Any]]) -> dict[str, str]:
    """Текст (handle подписи) → handle выноски, которая к нему ведёт."""
    out: dict[str, str] = {}
    for e in entities:
        if e.get("type") == "Leader" and e.get("annotation"):
            out[str(e["annotation"])] = str(e.get("handle", ""))
    return out


def parse_observed_offsets(dump: dict[str, Any]) -> list[ObservedOffset]:
    """Все подписи оффсетов в дампе: размерные и с выноской. Слой ``D-TOPO-TEXT``."""
    entities = dump.get("entities", [])
    leaders = _leader_map(entities)
    found: list[ObservedOffset] = []
    for e in entities:
        if e.get("layer") != OFFSET_LAYER or e.get("type") not in ("MText", "Text"):
            continue
        raw = str(e.get("text") or "")
        text = clean_text(raw)
        handle = str(e.get("handle", ""))
        height = float(e.get("h") or 0.0)
        pt = tuple(e.get("pt") or (0.0, 0.0))
        rot = float(e.get("rot_deg") or 0.0)
        leader = leaders.get(handle)
        dim = _DIM_RE.match(text) if "\\A" in raw else None
        if dim:
            part = OffsetPart(label=None, value_ft=float(dim["value"]), direction=None)
            found.append(
                ObservedOffset(handle, "dimension", text, raw, (part,), height, leader, pt, rot)
            )
            continue
        parts = tuple(
            OffsetPart(
                label=(m["label"] or None),
                value_ft=float(m["value"]),
                direction=m["dir"],
            )
            for m in _PART_RE.finditer(text)
        )
        if parts:
            # Без выноски — тоже допустимая форма (ответ заказчика, 4.1): текст без стрелки,
            # обычно для зданий и гаражей, например «3.2'S» на углу соседнего дома.
            found.append(
                ObservedOffset(
                    handle, "leader" if leader else "text", text, raw, parts, height, leader, pt, rot,
                    line_mark="O/L" in text,
                )
            )
    return found


# --------------------------------------------------------------------------- кадр двух дампов


#: Доля объектов, совпавших по координатам, при которой дампы считаются одной системой.
#: Остальные объекты могли быть передвинуты самим заказчиком (в Alvine — 8 из 278).
SAME_FRAME_SHARE = 0.9


@dataclass(frozen=True)
class FrameCheck:
    """Сравнение двух дампов по handle: сняты ли они в одной системе координат."""

    common: int  # объектов с одним handle и координатами
    same: int  # из них совпали по координатам
    different: int

    @property
    def same_frame(self) -> bool:
        return self.common > 0 and self.same / self.common >= SAME_FRAME_SHARE


def _first_point(entity: dict[str, Any]) -> Point | None:
    pts = entity.get("pts")
    if pts:
        return (float(pts[0][0]), float(pts[0][1]))
    pt = entity.get("pt")
    if pt:
        return (float(pt[0]), float(pt[1]))
    return None


def same_frame(before: dict[str, Any], after: dict[str, Any]) -> FrameCheck:
    """Есть ли у объектов с одинаковым handle совпадающие координаты в двух дампах."""
    index = {str(e.get("handle")): e for e in before.get("entities", []) if e.get("handle")}
    common = same = different = 0
    for e in after.get("entities", []):
        other = index.get(str(e.get("handle")))
        if other is None:
            continue
        p, q = _first_point(other), _first_point(e)
        if p is None or q is None:
            continue
        common += 1
        if math.dist(p, q) <= SAME_FRAME_TOL_FT:
            same += 1
        else:
            different += 1
    return FrameCheck(common=common, same=same, different=different)


def front_boundary_segments(dump: dict[str, Any]) -> list[tuple[Point, Point]]:
    """Отрезки фронтовой границы участка (слой ``M-ROW-LINE``) из дампа."""
    out: list[tuple[Point, Point]] = []
    for e in dump.get("entities", []):
        if e.get("layer") == FRONT_BOUNDARY_LAYER and e.get("type") == "Line" and e.get("pts"):
            a, b = e["pts"][0], e["pts"][1]
            out.append(((float(a[0]), float(a[1])), (float(b[0]), float(b[1]))))
    return out


def added_offsets(before: dict[str, Any], after: dict[str, Any]) -> list[ObservedOffset]:
    """Оффсеты, которых не было в дампе «до» (по handle подписи). Это то, что поставил заказчик."""
    known = {str(e.get("handle")) for e in before.get("entities", []) if e.get("handle")}
    return [item for item in parse_observed_offsets(after) if item.handle not in known]


# --------------------------------------------------------------------------- геометрия


def _segments(polyline: Sequence[Point], closed: bool) -> list[tuple[Point, Point]]:
    pts = list(polyline)
    if len(pts) < 2:
        return []
    segs = [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    if closed and len(pts) > 2:
        segs.append((pts[-1], pts[0]))
    return segs


def _closest_on_segment(p: Point, a: Point, b: Point) -> Point:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    if length2 == 0:
        return a
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2))
    return (a[0] + t * dx, a[1] + t * dy)


def perpendicular_distance(point: Point, polyline: Sequence[Point], *, closed: bool = True) -> tuple[float, Point]:
    """Расстояние от точки до ближайшей точки ломаной (границы) и сама эта точка (основание)."""
    best: tuple[float, Point] | None = None
    for a, b in _segments(polyline, closed):
        foot = _closest_on_segment(point, a, b)
        d = math.dist(point, foot)
        if best is None or d < best[0]:
            best = (d, foot)
    if best is None:
        raise ValueError("граница участка: меньше двух вершин")
    return best


def _side_projections(
    point: Point, polyline: Sequence[Point], closed: bool, *, extension_ft: float | None = None,
) -> list[tuple[float, Point, int]]:
    """
    Для каждой стороны ломаной: перпендикуляр от точки до её прямой, основание на прямой и номер стороны.
    Основание может лежать за концом стороны, но не дальше ``extension_ft`` (0 — только внутри стороны).
    """
    if extension_ft is None:
        extension_ft = LINE_EXTENSION_FT
    out: list[tuple[float, Point, int]] = []
    for index, (a, b) in enumerate(_segments(polyline, closed)):
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        if length < 1e-12:
            continue
        t = ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / (length * length)
        beyond = max(0.0, -t * length, (t - 1.0) * length)
        if beyond > extension_ft:
            continue
        foot = (a[0] + t * dx, a[1] + t * dy)
        out.append((math.dist(point, foot), foot, index))
    return out


def perpendicular_line_distance(point: Point, polyline: Sequence[Point], *, closed: bool = True) -> tuple[float, Point]:
    """
    Расстояние от точки до прямой стороны (до продолжения стороны, в пределах ``LINE_EXTENSION_FT``).
    Если подходящей стороны нет, возвращается расстояние до ближайшей точки границы.
    """
    best = min(_side_projections(point, polyline, closed), key=lambda item: item[0], default=None)
    if best is None:
        return perpendicular_distance(point, polyline, closed=closed)
    return best[0], best[1]


def point_in_polygon(point: Point, polygon: Sequence[Point]) -> bool:
    """Лучевой метод: точка внутри замкнутого контура."""
    x, y = point
    inside = False
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            x_cross = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < x_cross:
                inside = not inside
    return inside


# --------------------------------------------------------------------------- план (черновик 4.5)

#: Объекты, от которых ставим оффсет. Дом и сарай — текст без выноски; забор и стена — выноска.
OFFSET_KINDS = ("house", "fence", "wall")
#: Меньше этого расстояния объект считаем стоящим на границе (оффсета нет), футы.
MIN_OFFSET_FT = 0.05
#: подпись может стоять снаружи участка (в яру, у ворот): у заказчика 4 из 8 подписей вне линии участка
LABEL_MUST_BE_INSIDE = False
#: стены подписываются оффсетом по тому же правилу, что заборы (отсутствие подписи у заказчика — не запрет)
WALL_OFFSETS = False
#: Расстояние от объекта до текста выноски при 1"=16' (в порядке попыток), футы.
LEADER_DISTANCES_FT = (4.0, 5.5, 7.0)
#: Направления выноски относительно линии измерения, градусы; перпендикуляр пробуем первым.
LEADER_ANGLES_DEG = (90, -90, 60, -60, 120, -120, 30, -30, 150, -150, 0, 180)
#: Зазор текста до линии и выноски, доля высоты текста.
TEXT_GAP_FACTOR = 0.3
#: Если рядом (футы при 1"=16') уже есть такая же подпись, оффсет не дублируем.
DUPLICATE_RADIUS_FT = 10.0
#: Минимальная длина выноски в высотах текста: короче AutoCAD не нарисует стрелку.
LEADER_MIN_HEIGHTS = 2.0
#: Основание перпендикуляра может лежать на продолжении стороны за её концом не дальше этого, футы.
#: Заказчик меряет до продолжения линии: «FE.1.8'S» — 1.84 ft до продолжения северной стороны (Alvine).
LINE_EXTENSION_FT = 5.0
#: Объект за границей участка подписываем, если он не дальше этого от неё, футы.
#: Заказчик подписывает и «3.2'S» у строения за южной линией (Alvine), и «FE.0.2'S» в 0.24 ft снаружи.
OUTSIDE_MAX_FT = 5.0
#: Вторая сторона у той же вершины: если она ближе этого, пишем её второй строкой той же выноски.
#: Примеры: «FE.1.8'S \\P FE.1.4'E» (1.84 и 1.44), «FE.0.1'S \\P FE.0.5'W» (0.07 и 0.52).
SECOND_SIDE_FT = 2.0

@dataclass(frozen=True)
class Feature:
    """Объект, от которого может идти оффсет: дом, забор, стена, заезд."""

    kind: str  # "house" | "fence" | "wall" | "driveway" | "other"
    handle: str
    points: tuple[Point, ...]


@dataclass(frozen=True)
class OffsetCandidate:
    """Измерение: вершина объекта и её расстояние до границы. Решения о подписи здесь нет."""

    feature: Feature
    vertex: Point
    distance_ft: float
    foot: Point  # основание перпендикуляра на границе
    inside: bool  # вершина внутри участка
    outside_ft: float = 0.0  # если снаружи — расстояние до контура участка, футы; внутри 0
    segment: int = -1  # номер отрезка объекта (для параллельных углов); -1 — ближайшая сторона без отрезка


def measure_candidates(
    property_pts: Sequence[Point],
    features: Sequence[Feature],
    *,
    front: Sequence[Sequence[Point]] = (),
) -> list[OffsetCandidate]:
    """
    Для каждой вершины каждого объекта — расстояние до границы: до контура участка и до
    фронтовых линий ``M-ROW-LINE`` (``front``), берётся ближайшая из них.
    """
    out: list[OffsetCandidate] = []
    boundaries: list[tuple[Sequence[Point], bool]] = [(property_pts, True)]
    boundaries += [(line, False) for line in front if len(line) >= 2]
    for feature in features:
        for vertex in feature.points:
            distance, foot = min(
                (perpendicular_line_distance(vertex, line, closed=closed) for line, closed in boundaries),
                key=lambda pair: pair[0],
            )
            inside = point_in_polygon(vertex, property_pts)
            outside = 0.0 if inside else perpendicular_distance(vertex, property_pts, closed=True)[0]
            out.append(
                OffsetCandidate(
                    feature=feature,
                    vertex=vertex,
                    distance_ft=round(distance, 4),
                    foot=foot,
                    inside=inside,
                    outside_ft=round(outside, 4),
                )
            )
    return out


Rect = tuple[float, float, float, float]


def round_offset_ft(distance_ft: float) -> float:
    """Расстояние в подписи — до 0.1 фута, половина вверх (как у заказчика: 0.4, 3.2, 14.6)."""
    return math.floor(distance_ft * 10.0 + 0.5) / 10.0


def compass_letter(dx: float, dy: float, north_deg: float = 90.0) -> str:
    """
    Буква направления вектора ``(dx, dy)``. В оффсетах вектор идёт от границы к объекту
    (сторона, где стоит объект). ``north_deg`` — направление на север (градусы от +X против часовой), по
    умолчанию +Y. Берётся главная ось: ``N``, ``S``, ``E`` или ``W``.
    """
    rad = math.radians(north_deg)
    nx, ny = math.cos(rad), math.sin(rad)
    ex, ey = ny, -nx  # восток — север, повёрнутый по часовой на 90°
    north = dx * nx + dy * ny
    east = dx * ex + dy * ey
    scores = {"N": north, "S": -north, "E": east, "W": -east}
    return max(scores, key=lambda letter: scores[letter])


def offset_text(kind: str, value_ft: float, direction: str, *, leader: bool = False) -> str:
    """
    Текст оффсета в форме заказчика: ``FE.0.4'N``, ``WALL 0.5'E``. Дом без выноски — ``3.2'S``,
    дом с выноской — ``BLDG 2.5'S`` (так у заказчика в Alvine).
    """
    value = f"{value_ft:.1f}'{direction}"
    if kind == "fence":
        return f"FE.{value}"
    if kind == "wall":
        return f"WALL {value}"
    if leader:
        return f"BLDG {value}"
    return value


def default_text_width(text: str, height: float) -> float:
    """Грубая ширина строки, если программа не передала точную (шрифт romans, ~0.6 высоты на знак)."""
    return 0.6 * float(height) * len(text)


def _rect_at(center: Point, width: float, height: float) -> Rect:
    cx, cy = center
    return (cx - width / 2.0, cy - height / 2.0, cx + width / 2.0, cy + height / 2.0)


def _rects_overlap(a: Rect, b: Rect) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _rect_inside_polygon(rect: Rect, polygon: Sequence[Point]) -> bool:
    x0, y0, x1, y1 = rect
    return all(point_in_polygon(p, polygon) for p in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)))


def _segment_hits_rect(p: Point, q: Point, rect: Rect) -> bool:
    """Пересекает ли отрезок ``pq`` прямоугольник (алгоритм Лианга — Барски)."""
    x0, y0 = p
    x1, y1 = q
    xmin, ymin, xmax, ymax = rect
    dx, dy = x1 - x0, y1 - y0
    t_enter, t_leave = 0.0, 1.0
    for num, den in ((-dx, x0 - xmin), (dx, xmax - x0), (-dy, y0 - ymin), (dy, ymax - y0)):
        if abs(num) < 1e-12:
            if den < 0.0:
                return False
            continue
        ratio = den / num
        if num < 0.0:
            if ratio > t_leave:
                return False
            t_enter = max(t_enter, ratio)
        else:
            if ratio < t_enter:
                return False
            t_leave = min(t_leave, ratio)
    return t_enter <= t_leave


def _key(text: str) -> str:
    return re.sub(r"\s+", "", str(text)).upper()


@dataclass(frozen=True)
class OffsetItem:
    """Готовый оффсет: текст, где он стоит, и (для выноски) отрезок от объекта до текста."""

    kind: str  # "house" | "fence" | "wall"
    value_ft: float
    direction: str  # "N" | "S" | "E" | "W"
    style: str  # "text" — без выноски; "leader" — выноска
    height_ft: float
    anchor: Point  # вершина объекта, от которой меряем
    target: Point  # основание перпендикуляра на границе
    text: str
    point: Point  # центр подписи
    leader: tuple[Point, Point] | None = None  # (стрелка у объекта, конец у текста)


@dataclass
class OffsetPlan:
    items: list[OffsetItem] = field(default_factory=list)
    #: объекты в области, которым оффсет не поставлен: (объект, причина)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: счётчики для отчёта: zone — объектов в зоне участка; in_area / outside — в области и вне её;
    #: in_area_<вид> — в области по видам (house, fence, wall, other)
    counts: dict[str, int] = field(default_factory=dict)


def _place_beside_line(
    anchor: Point, target: Point, text: str, height: float, width_of: Callable[[str, float], float],
    ok: Callable[[Rect, Point], bool],
) -> Point | None:
    """Текст без выноски — рядом с линией измерения, по перпендикуляру к ней."""
    dx, dy = target[0] - anchor[0], target[1] - anchor[1]
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return None
    px, py = -dy / length, dx / length
    width = width_of(text, height)
    gap = TEXT_GAP_FACTOR * height
    mid = ((anchor[0] + target[0]) / 2.0, (anchor[1] + target[1]) / 2.0)
    half_extent = abs(px) * width / 2.0 + abs(py) * height / 2.0 + gap
    for extra in (0.0, 0.5 * height, height, 2.0 * height):
        for sign in (1.0, -1.0):
            d = half_extent + extra
            center = (mid[0] + sign * px * d, mid[1] + sign * py * d)
            rect = _rect_at(center, width, height)
            if ok(rect, center) and not _segment_hits_rect(anchor, target, rect):
                return center
    return None


def _place_leader(
    anchor: Point, target: Point, text: str, height: float, width_of: Callable[[str, float], float],
    ok: Callable[[Rect, Point], bool], factor: float,
) -> tuple[Point, Point] | None:
    """Выноска от объекта к тексту: перпендикуляр к линии измерения первым, затем косые."""
    base = math.atan2(target[1] - anchor[1], target[0] - anchor[0])
    width, block_h = _text_block(text, height, width_of)
    gap = TEXT_GAP_FACTOR * height
    for distance in LEADER_DISTANCES_FT:
        for angle in LEADER_ANGLES_DEG:
            a = base + math.radians(angle)
            d = distance * factor
            center = (anchor[0] + d * math.cos(a), anchor[1] + d * math.sin(a))
            rect = _rect_at(center, width, block_h)
            if not ok(rect, center):
                continue
            # Конец выноски — на краю текста, со стороны объекта.
            vx, vy = anchor[0] - center[0], anchor[1] - center[1]
            norm = math.hypot(vx, vy)
            if norm < 1e-9:
                continue
            ux, uy = vx / norm, vy / norm
            half_w, half_h = width / 2.0, block_h / 2.0
            reach = min(
                half_w / abs(ux) if abs(ux) > 1e-9 else math.inf,
                half_h / abs(uy) if abs(uy) > 1e-9 else math.inf,
            )
            end = (center[0] + ux * (reach + gap), center[1] + uy * (reach + gap))
            if math.hypot(end[0] - anchor[0], end[1] - anchor[1]) < LEADER_MIN_HEIGHTS * height:
                continue
            return center, end
    return None


#: каждый угол объекта подписывается отдельно; расстояние — до стороны участка, которой параллелен край объекта
CORNER_LABELS = False
#: допуск параллельности края стороне участка, градусы
PARALLEL_TOL_DEG = 3.0
#: основание перпендикуляра на параллельной стороне может лежать за её концом не дальше, футы
PARALLEL_EXTENSION_FT = 5.0
#: параллельная сторона учитывается, только если она ближе этого расстояния; дальше — ближайшая сторона
PARALLEL_MAX_FT = 6.0


def _axis_sides(ring: Sequence[Point]) -> tuple[list[tuple[float, float, float]], list[tuple[float, float, float]]]:
    """Горизонтальные стороны участка (y, x0, x1) и вертикальные (x, y0, y1)."""
    horizontal: list[tuple[float, float, float]] = []
    vertical: list[tuple[float, float, float]] = []
    for i in range(len(ring)):
        (x1, y1), (x2, y2) = ring[i], ring[(i + 1) % len(ring)]
        if abs(y1 - y2) <= 1e-6 * max(1.0, abs(x1 - x2)):
            horizontal.append((y1, min(x1, x2), max(x1, x2)))
        elif abs(x1 - x2) <= 1e-6 * max(1.0, abs(y1 - y2)):
            vertical.append((x1, min(y1, y2), max(y1, y2)))
    return horizontal, vertical


def _parallel_candidates(
    ring: Sequence[Point], feature: Feature,
) -> tuple[list[OffsetCandidate], set[Point]]:
    """
    Для каждого конца горизонтального или вертикального отрезка объекта — расстояние до параллельной
    стороны участка (горизонтальный край → N/S, вертикальный → W/E). Возвращает кандидатов и вершины,
    для которых такие кандидаты нашлись.
    """
    horizontal, vertical = _axis_sides(ring)
    tan_tol = math.tan(math.radians(PARALLEL_TOL_DEG))
    pts = list(feature.points)
    closed = feature.kind == "house" and len(pts) >= 3
    seq = pts + [pts[0]] if closed else pts
    out: list[OffsetCandidate] = []
    covered: set[Point] = set()
    seen: set[tuple[Point, Point]] = set()
    inside_cache: dict[Point, bool] = {}
    for seg_index, (a, b) in enumerate(zip(seq, seq[1:])):
        dx, dy = b[0] - a[0], b[1] - a[1]
        if math.hypot(dx, dy) < 1e-9:
            continue
        if abs(dy) <= tan_tol * abs(dx):
            kind = "H"
        elif abs(dx) <= tan_tol * abs(dy):
            kind = "V"
        else:
            continue
        for v in (a, b):
            if kind == "H":
                sides = [(abs(v[1] - y), (v[0], y), x0, x1) for y, x0, x1 in horizontal]
                options = [(d, foot) for d, foot, x0, x1 in sides if x0 - PARALLEL_EXTENSION_FT <= v[0] <= x1 + PARALLEL_EXTENSION_FT]
            else:
                sides = [(abs(v[0] - x), (x, v[1]), y0, y1) for x, y0, y1 in vertical]
                options = [(d, foot) for d, foot, y0, y1 in sides if y0 - PARALLEL_EXTENSION_FT <= v[1] <= y1 + PARALLEL_EXTENSION_FT]
            # Из двух параллельных сторон (например, N и S) — ближайшая к концу края.
            options = [pair for pair in sorted(options)[:1] if pair[0] <= PARALLEL_MAX_FT]
            for distance, foot in options:
                key = (v, foot)
                if key in seen or distance <= 0.0:
                    continue
                seen.add(key)
                if v not in inside_cache:
                    inside_cache[v] = point_in_polygon(v, ring)
                inside = inside_cache[v]
                outside = 0.0 if inside else perpendicular_distance(v, ring, closed=True)[0]
                out.append(OffsetCandidate(
                    feature=feature, vertex=v, distance_ft=round(distance, 4), foot=foot,
                    inside=inside, outside_ft=round(outside, 4), segment=seg_index,
                ))
                covered.add(v)
    return out, covered


def _point_segment_distance(p: Point, seg: tuple[Point, Point]) -> float:
    (ax, ay), (bx, by) = seg
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / length2))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


def _feature_candidates(
    ring: Sequence[Point], feature: Feature, front_lines: Sequence[Sequence[Point]],
) -> list[OffsetCandidate]:
    """Кандидаты для объекта: при CORNER_LABELS — углы по параллельным сторонам, иначе ближайшая сторона."""
    if not CORNER_LABELS or feature.kind not in ("fence", "house", "wall"):
        return measure_candidates(ring, [feature], front=front_lines)[:]
    parallel, covered = _parallel_candidates(ring, feature)
    rest = [
        c for c in measure_candidates(ring, [feature], front=front_lines)
        if c.vertex not in covered
    ]
    return parallel + rest


def plan_offsets(
    property_pts: Sequence[Point],
    features: Sequence[Feature],
    *,
    front: Sequence[Sequence[Point]] = (),
    area: Sequence[Point] | None = None,
    scale: float = BASE_SCALE,
    north_deg: float = 90.0,
    texts: Sequence[tuple[str, Point, float]] = (),
    text_width: Callable[[str, float], float] | None = None,
) -> OffsetPlan:
    """
    Шаг 4, черновик 4.5: оффсеты объектов в выбранной области.

    ``property_pts`` — граница участка (замкнутый контур); ``front`` — линии ``M-ROW-LINE``
    (фронтовая граница, ``M-TAX-LINE`` сюда не передаётся); ``area`` — прямоугольник, который
    указал геодезист (``None`` — весь участок); ``texts`` — уже стоящие подписи ``(текст, точка,
    высота)``: рядом с тем же текстом оффсет не дублируется, а текст не накрывается.

    Для каждого объекта берётся ближайшая к границе вершина внутри участка; значение — до 0.1 ft.
    Дом и сарай — текст без выноски (высота 1.1), забор и стена — выноска (0.9). Текст ставится
    там, где не накрывает линии объектов, границы и другие подписи; если места нет — предупреждение.
    """
    plan = OffsetPlan()
    ring = [(float(x), float(y)) for x, y in property_pts]
    if len(ring) >= 2 and ring[0] == ring[-1]:
        ring = ring[:-1]
    if len(ring) < 3:
        plan.warnings.append("граница участка: меньше трёх вершин — оффсеты не строятся")
        return plan
    factor = scale / BASE_SCALE if scale and scale > 0 else 1.0
    width_of = text_width or default_text_width
    area_pts = [(float(x), float(y)) for x, y in area] if area else None
    front_lines = [[(float(x), float(y)) for x, y in line] for line in front if len(line) >= 2]

    known = [(str(t), (float(p[0]), float(p[1])), float(h) or LABEL_HEIGHT_FT * factor) for t, p, h in texts]
    # Препятствия для текста: уже стоящие подписи и линии, которые текст не должен пересекать.
    text_rects = [_rect_at(p, width_of(t, h), h) for t, p, h in known]
    lines: list[tuple[Point, Point]] = []
    for i in range(len(ring)):
        lines.append((ring[i], ring[(i + 1) % len(ring)]))
    for line in front_lines:
        lines += list(zip(line, line[1:]))
    for feature in features:
        pts = list(feature.points)
        if len(pts) >= 2:
            closed = feature.kind == "house" and len(pts) >= 3
            seq = pts + [pts[0]] if closed else pts
            lines += list(zip(seq, seq[1:]))

    placed: list[Rect] = []
    dup_radius = DUPLICATE_RADIUS_FT * factor

    plan.counts["zone"] = len(features)
    for index, feature in enumerate(features):
        label = feature.handle or f"#{index}"
        in_area = area_pts is None or any(point_in_polygon(p, area_pts) for p in feature.points)
        if not in_area:
            plan.counts["outside"] = plan.counts.get("outside", 0) + 1
            continue
        plan.counts["in_area"] = plan.counts.get("in_area", 0) + 1
        kind_key = f"in_area_{feature.kind if feature.kind in OFFSET_KINDS else 'other'}"
        plan.counts[kind_key] = plan.counts.get(kind_key, 0) + 1
        if feature.kind not in OFFSET_KINDS:
            plan.skipped.append((label, "этот вид объекта в оффсетах не подписывается"))
            continue
        if feature.kind == "wall" and not WALL_OFFSETS:
            plan.skipped.append((label, "стена: у заказчика подписывается размером, не оффсетом"))
            continue
        candidates = [
            c for c in _feature_candidates(ring, feature, front_lines)
            if c.outside_ft <= OUTSIDE_MAX_FT
        ]
        if not candidates:
            plan.skipped.append((label, "объект вне участка"))
            continue
        # Вершины, которые лежат на самой линии участка, не подписываются, но не отменяют остальные вершины
        # объекта: в Brooklyn заборы с такой вершиной подписаны по другим вершинам (FE.0.2'N, FE.0.1'S).
        on_line = [c for c in candidates if c.distance_ft < MIN_OFFSET_FT]
        candidates = [c for c in candidates if c.distance_ft >= MIN_OFFSET_FT]
        if not candidates:
            plan.skipped.append((label, "стоит на границе"))
            continue
        # При одинаковом значении после округления (0.1) берём вершину внутри участка: так подписал заказчик
        # в Brooklyn (0.088 внутри вместо 0.066 снаружи, оба дают 0.1).
        candidates.sort(key=lambda c: (round_offset_ft(c.distance_ft), not c.inside, c.distance_ft))
        # Один угол — одна подпись; при CORNER_LABELS подписывается каждый угол отдельно.
        # Два конца одного отрезка на одинаковом расстоянии (прямой забор) — одна подпись. Разные значения
        # на концах одного дома (2.1 и 2.3 у одной стороны) — обе: так подписывает заказчик по углам.
        used: set[tuple] = set()
        rest_seen = False
        for best in (candidates if CORNER_LABELS else candidates[:1]):
            value = round_offset_ft(best.distance_ft)
            if value <= 0.0:
                plan.skipped.append((label, "стоит на границе"))
                continue
            # Кандидат без параллельной стороны (ближайшая сторона) — один на объект, как раньше.
            if best.segment < 0:
                if rest_seen:
                    continue
                rest_seen = True
            seg_key = ("seg", best.segment, value)
            if CORNER_LABELS and seg_key in used:
                continue
            # Буква — с какой стороны от границы стоит объект: «FE.0.4'N» — забор севернее линии.
            # Так подписывает заказчик (проверено на Alvine 8 из 8 и Brooklyn 5 из 5).
            vx, vy = best.vertex[0] - best.foot[0], best.vertex[1] - best.foot[1]
            direction = compass_letter(vx, vy, north_deg)
            height = offset_height_ft("house" if feature.kind == "house" else "fence", scale)
            text = offset_text(feature.kind, value, direction)
            extra: list[tuple[float, str]] = []
            if feature.kind in ("fence", "wall"):
                boundaries: list[tuple[Sequence[Point], bool]] = [(ring, True)]
                boundaries += [(line, False) for line in front_lines]
                taken = (value, direction)
                extra = _extra_sides(best.vertex, best.foot, taken, boundaries, north_deg)
            mid = ((best.vertex[0] + best.foot[0]) / 2.0, (best.vertex[1] + best.foot[1]) / 2.0)
            # Тот же текст рядом с вершиной, серединой выноски или той же стороной объекта — дубль.
            fpts = list(feature.points)
            seq_pts = fpts + [fpts[0]] if feature.kind == "house" and len(fpts) >= 3 else fpts
            if best.segment >= 0:
                edges = [(seq_pts[best.segment], seq_pts[best.segment + 1])]
            else:
                edges = list(zip(seq_pts, seq_pts[1:]))
            if any(
                _key(old) == _key(text) and min(
                    math.hypot(p[0] - best.vertex[0], p[1] - best.vertex[1]),
                    math.hypot(p[0] - mid[0], p[1] - mid[1]),
                    min((_point_segment_distance(p, e) for e in edges), default=math.inf),
                ) <= dup_radius
                for old, p, _h in known
            ):
                plan.skipped.append((label, f"«{text}» уже стоит рядом"))
                continue

            bounds = _area_bounds(area_pts, margin=15.0 * factor)

            def ok(rect: Rect, center: Point) -> bool:
                if area_pts is not None and not _rect_inside_polygon(rect, area_pts):
                    return False
                if LABEL_MUST_BE_INSIDE and not point_in_polygon(center, ring):
                    return False
                if any(_rects_overlap(rect, other) for other in text_rects + placed):
                    return False
                return not any(
                    _segment_hits_rect(a, b, rect)
                    for a, b in lines
                    if bounds is None or _segment_in_bounds(a, b, bounds)
                )

            item: OffsetItem | None = None
            beside = None
            if feature.kind == "house":
                beside = _place_beside_line(
                    best.vertex, best.foot, text, height, width_of, ok
                )
            if beside is not None:
                rect = _rect_at(beside, width_of(text, height), height)
                item = OffsetItem(
                    kind=feature.kind, value_ft=value, direction=direction, style="text",
                    height_ft=height, anchor=best.vertex, target=best.foot, text=text,
                    point=beside, leader=None,
                )
            else:
                # Дом без места под текст получает выноску с «BLDG»; забор и стена — выноску как есть.
                leader_text = "\n".join(
                    [offset_text(feature.kind, value, direction)]
                    + [offset_text(feature.kind, v, d) for v, d in extra]
                )
                spot = _place_leader(best.vertex, best.foot, leader_text, height, width_of, ok, factor)
                if spot is None:
                    plan.warnings.append(
                        f"{label}: нет свободного места для «{leader_text}» в области — подпись не поставлена"
                    )
                    continue
                center, end = spot
                rect_w, rect_h = _text_block(leader_text, height, width_of)
                rect = _rect_at(center, rect_w, rect_h)
                item = OffsetItem(
                    kind=feature.kind, value_ft=value, direction=direction, style="leader",
                    height_ft=height, anchor=best.vertex, target=best.foot, text=leader_text,
                    point=center, leader=(best.vertex, end),
                )
            placed.append(rect)
            plan.items.append(item)
            known.append((item.text, item.point, item.height_ft))
            used.add(seg_key)
    return plan


def _extra_sides(
    vertex: Point, best_foot: Point, taken: tuple[float, str],
    boundaries: Sequence[tuple[Sequence[Point], bool]], north_deg: float,
) -> list[tuple[float, str]]:
    """
    Другие стороны участка, к которым та же вершина ближе ``SECOND_SIDE_FT``: заказчик пишет
    их второй строкой той же выноски. Возвращает (значение, буква), ближайшая первой.
    """
    found: list[tuple[float, float, str]] = []
    for line, closed in boundaries:
        # Только основание внутри стороны: продолжение за углом даёт ложные вторые части.
        for distance, foot, _index in _side_projections(vertex, line, closed, extension_ft=0.0):
            if math.dist(foot, best_foot) < 1e-6 or distance > SECOND_SIDE_FT:
                continue
            value = round_offset_ft(distance)
            if value <= 0.0:
                continue
            direction = compass_letter(vertex[0] - foot[0], vertex[1] - foot[1], north_deg)
            found.append((distance, value, direction))
    found.sort()
    out: list[tuple[float, str]] = []
    for _distance, value, direction in found:
        pair = (value, direction)
        if pair != taken and pair not in out:
            out.append(pair)
    return out


def _text_block(text: str, height: float, width_of: Callable[[str, float], float]) -> tuple[float, float]:
    """Размер многострочной подписи (строки разделены ``\\n``): ширина самой длинной и общая высота."""
    lines = text.split("\n")
    width = max(width_of(line, height) for line in lines)
    return width, height * len(lines)


def _area_bounds(area: Sequence[Point] | None, margin: float) -> Rect | None:
    if not area:
        return None
    xs = [p[0] for p in area]
    ys = [p[1] for p in area]
    return (min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)


def _segment_in_bounds(a: Point, b: Point, bounds: Rect) -> bool:
    x0, y0, x1, y1 = bounds
    return not (max(a[0], b[0]) < x0 or min(a[0], b[0]) > x1
                or max(a[1], b[1]) < y0 or min(a[1], b[1]) > y1)


def plan_report_text(plan: OffsetPlan, scale: float = BASE_SCALE, context: Sequence[str] = ()) -> str:
    """
    Отчёт шага 4 для окна: контекст запуска (область, участок), счётчики объектов, что поставлено,
    что пропущено и почему, предупреждения. Если подписей нет — сказано, на каком шаге.
    """
    texts = [i for i in plan.items if i.style == "text"]
    leaders = [i for i in plan.items if i.style == "leader"]
    c = plan.counts
    lines = [f"Шаг 4 — оффсеты, черновик {STEP4_VERSION} (1\"={scale:g}')", *context]
    lines.append(
        f"Распознано в зоне участка: {c.get('zone', 0)}; в выделенной области: {c.get('in_area', 0)} "
        f"(дом и сарай {c.get('in_area_house', 0)}, забор {c.get('in_area_fence', 0)}, "
        f"стена {c.get('in_area_wall', 0)}, прочее {c.get('in_area_other', 0)}); "
        f"вне области: {c.get('outside', 0)}"
    )
    lines.append(f"Подписей: {len(plan.items)} (без выноски: {len(texts)}, с выноской: {len(leaders)})")
    for item in plan.items:
        shown = item.text.replace("\n", " + ")  # две стороны одной вершины — в одной строке журнала
        lines.append(f"  {shown}  ({'без выноски' if item.style == 'text' else 'выноска'})")
    if not plan.items:
        if c.get("zone", 0) == 0:
            lines.append(
                "Внимание: в зоне участка не распознано ни одного объекта. Проверьте, что участок "
                "найден (слой границы) и что чертёж тот же, что в Шаге 2."
            )
        elif c.get("in_area", 0) == 0:
            lines.append(
                "Внимание: ни один распознанный объект не попал в выделенную область. Проверьте, что "
                "область выделена на этом же чертеже и внутри участка (координаты — в строках выше)."
            )
        elif not plan.skipped and not plan.warnings:
            lines.append("Внимание: в области есть объекты, но оффсеты для них не получились — см. причины.")
    if plan.skipped:
        lines.append(f"Не подписано: {len(plan.skipped)}")
        for label, reason in plan.skipped:
            lines.append(f"  {label}: {reason}")
    for warning in plan.warnings:
        lines.append(f"Внимание: {warning}")
    return "\n".join(lines)



# --------------------------------------------------------------------------- окно программы

#: Этапы шага 4 и их состояние в каркасе (показываются на вкладке «Шаг 4. Оффсеты»).
STEP4_STAGES: tuple[tuple[str, str], ...] = (
    ("Область — выделение в AutoCAD; объекты в ней: дом, сарай, заборы, стены", "черновик 4.5"),
    ("Измерение до границы: контур участка и фронт M-ROW-LINE (M-TAX-LINE не граница)", "черновик 4.5"),
    ("Способ записи: дом — текст без выноски; забор и стена — выноска", "черновик 4.5"),
    ("Текст и высота: 1.1 на дом, 0.9 на остальное", "есть"),
    ("Размещение без наложений с подписями и линиями", "черновик 4.5"),
    ("Проверка кадра и предупреждения", "функция есть, в окне не выводится"),
)


def step4_report_text(scale: float = BASE_SCALE) -> str:
    """Текст для окна шага 4: состояние этапов и высоты текста при данном масштабе."""
    lines = [f"Шаг 4 — оффсеты (черновик {STEP4_VERSION})", ""]
    for number, (stage, status) in enumerate(STEP4_STAGES, start=1):
        lines.append(f"{number}. {stage} — {status}")
    lines += [
        "",
        f"Высота текста при 1\"={scale:g}': на дом {offset_height_ft('house', scale):.3f} ft, "
        f"остальные оффсеты {offset_height_ft('fence', scale):.3f} ft.",
        "",
        "Правила берутся из пар дампов заказчика («до» и «после»). Подробно: docs/step4_offsets_plan.md.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- CLI


def _format_observed(items: Sequence[ObservedOffset]) -> str:
    lines = [f"{'handle':>7} {'стиль':<9} {'h':>4}  {'текст':<28} {'выноска':>8}  значения"]
    for it in items:
        values = "; ".join(
            f"{(p.label or '')}{'' if p.value_ft is None else f'{p.value_ft:g}'}"
            f"{'' if p.direction is None else p.direction}"
            for p in it.parts
        )
        leader = it.leader_handle or "—"
        lines.append(
            f"{it.handle:>7} {it.style:<9} {it.height_ft:>4.2f}  {it.text[:28]:<28} {leader:>8}  {values}"
        )
    return "\n".join(lines)


def main(argv: Sequence[str]) -> int:
    if len(argv) != 2:
        print("Использование: python offsets.py <файл.dump.json>", file=sys.stderr)
        return 2
    dump = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    items = parse_observed_offsets(dump)
    print(f"Оффсетов в эталоне: {len(items)}")
    print(_format_observed(items))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
