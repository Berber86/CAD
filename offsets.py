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
STEP4_VERSION = "4.20"
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
LEADER_DISTANCES_FT = (4.0, 5.5, 7.0, 9.0, 11.0)
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
#: Заказчик подписывает «3.2'S» (Alvine), «FE.5.2'S» (Geldner) и «IRON FENCE 5.4'E» (Brooklyn).
OUTSIDE_MAX_FT = 5.5
#: Вторая сторона у той же вершины: если она ближе этого, пишем её второй строкой той же выноски.
#: Примеры: «FE.1.8'S \\P FE.1.4'E» (1.84 и 1.44), «FE.0.1'S \\P FE.0.5'W» (0.07 и 0.52).
SECOND_SIDE_FT = 2.0
#: У общего угла участка (в радиусе CORNER_SHARED_RADIUS_FT) вторая сторона может измеряться до её
#: короткого продолжения (тупые и наружные углы: Alvine «1.8'S / 1.4'E», Geldner «0.1'E / 1.2'S»).
CORNER_SECOND_EXTENSION_FT = 3.5
CORNER_SHARED_RADIUS_FT = 4.0
#: Для строений подписывается каждая сближенная сторона участка до этого порога, футы; если все стены
#: дальше — остаётся один ближайший оффсет внутри участка.
HOUSE_SIDE_MAX_FT = 6.2
#: Если строение сближено ровно с одной стороной границы ближе этого порога на протяжении >= 15 ft
#: и два угла дают разные округлённые значения, подписываются оба угла (Geldner 0.3'W/0.4'W, Alvine 2.5'S/2.6'S).
HOUSE_TWO_CORNER_MAX_FT = 3.6
HOUSE_TWO_CORNER_SPAN_FT = 15.0
#: Минимальный шаг между несколькими выносками одного длинного/многостороннего забора, футы.
FENCE_SAME_SIDE_SEP_FT = 22.0
FENCE_DIFF_SIDE_SEP_FT = 12.0
FENCE_PARALLEL_TOL_DEG = 12.0
FENCE_PARALLEL_MIN_LEN_FT = 4.0
#: Забор, у которого вершина стоит на линии участка (до MIN_OFFSET_FT), подписывается «FE.O/L» — «на линии».
#: За: 192 Geldner, забор слева посередине (вершина в 0.001 ft от границы) — у Николая «FE.O/L».
#: Против: на Brooklyn (4.10) такая вершина не подписана «O/L», забор подписан по другим вершинам.
#: Противоречивые эталоны — выключено по умолчанию, как CORNER_LABELS и WALL_OFFSETS.
ONLINE_LABELS = False
ONLINE_FENCE_TEXT = "FE.O/L"
#: Текст без выноски поворачивается вдоль ближайшей границы (R10). Выключено: на эталонах угол следует за
#: ориентацией участка, а не за стороной. Ilion: «FE.0.1'E» — 90.4°, «FE.1.3'E» — 0.4° (участок 0.4°).
#: Aspinwall: «WALL 0.1'E» — 284.6° (участок 14.6°). Правило «вдоль границы» не подтверждено.
TEXT_FOLLOWS_BOUNDARY = False

@dataclass(frozen=True)
class Feature:
    """Объект, от которого может идти оффсет: дом, забор, стена, заезд."""

    kind: str  # "house" | "fence" | "wall" | "driveway" | "other"
    handle: str
    points: tuple[Point, ...]
    closed: bool | None = None

    @property
    def is_closed(self) -> bool:
        if self.closed is not None:
            return bool(self.closed)
        return self.kind == "house" and len(self.points) >= 3


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


def compass_axis_pair(
    dx1: float, dy1: float, dx2: float, dy2: float, north_deg: float = 90.0,
) -> tuple[str, str]:
    """
    Две буквы направления для угловой двухчастной выноски: если две стороны угла не параллельны
    (> 20°), одна часть получает ось ``N/S``, а вторая — ортогональную ``E/W`` (так у Николая во
    всех 9 двухчастных выносках на 6 эталонах, включая косой угол Alvine «1.8'S / 1.4'E»).
    """
    rad = math.radians(north_deg)
    nx, ny = math.cos(rad), math.sin(rad)
    ex, ey = ny, -nx

    def _components(dx: float, dy: float) -> tuple[float, float, float, float]:
        n = dx * nx + dy * ny
        e = dx * ex + dy * ey
        norm = math.hypot(n, e) or 1.0
        return n, e, abs(n) / norm, abs(e) / norm

    n1, e1, rn1, re1 = _components(dx1, dy1)
    n2, e2, rn2, re2 = _components(dx2, dy2)
    d1 = ("N" if n1 >= 0 else "S") if abs(n1) >= abs(e1) else ("E" if e1 >= 0 else "W")
    d2 = ("N" if n2 >= 0 else "S") if abs(n2) >= abs(e2) else ("E" if e2 >= 0 else "W")
    if (d1 in "NS") == (d2 in "NS"):
        l1, l2 = math.hypot(dx1, dy1), math.hypot(dx2, dy2)
        if l1 > 1e-9 and l2 > 1e-9 and abs(dx1 * dy2 - dy1 * dx2) / (l1 * l2) > math.sin(math.radians(20.0)):
            if (rn1 - re1) >= (rn2 - re2):
                d1 = "N" if n1 >= 0 else "S"
                d2 = "E" if e2 >= 0 else "W"
            else:
                d2 = "N" if n2 >= 0 else "S"
                d1 = "E" if e1 >= 0 else "W"
    return d1, d2


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


#: Размеры стен дома (R6, R8, R10): текст — длина стены между её углами, без буквы, параллельно стене,
#: снаружи дома. Выключено: какие стены заказчик размеряет, не подтверждено. На 192 Geldner он размерил
#: 2 стены из 4 (26.5' и 46.5'), а 26.4' и 46.4' не подписал; правило выбора стен он не назвал.
DIMENSION_LABELS = False
#: Стены короче этого (футы) не размеряются: там отрезки крыльца и ступеней.
DIMENSION_MIN_FT = 5.0
#: Текст размера ставится внутри дома, на это расстояние от стены, футы (192 Geldner: 1.1 и 1.5 у двух стен,
#: 1.0 у размера 7.0'). Текст — в середине размерной линии (4 из 4).
DIMENSION_TEXT_OFFSET_FT = 1.1


@dataclass(frozen=True)
class WallDimension:
    """Размер стены дома: текст без буквы, центр текста и угол поворота вдоль стены."""

    text: str
    value_ft: float
    start: Point
    end: Point
    point: Point
    angle_deg: float


def wall_dimension_text(length_ft: float) -> str:
    """Текст размера стены: ``26.5'`` — без буквы направления (R8)."""
    return f"{round_offset_ft(length_ft):.1f}'"


def house_wall_dimensions(
    house_pts: Sequence[Point],
    *,
    scale: float = BASE_SCALE,
    min_length_ft: float = DIMENSION_MIN_FT,
) -> list[WallDimension]:
    """
    Размеры каждой стены дома от угла до угла (R6). Текст стоит внутри дома, у стены, параллельно ей (R10).
    ``house_pts`` — контур дома; если он незамкнутый, замыкается сам. Стены короче ``min_length_ft`` пропускаются.
    Сторона определяется проверкой точки на шаг внутрь: контур может быть невыпуклым.
    """
    pts = [(float(x), float(y)) for x, y in house_pts]
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if len(pts) < 3:
        return []
    factor = scale / BASE_SCALE if scale and scale > 0 else 1.0
    height = LABEL_HEIGHT_FT * factor
    offset = DIMENSION_TEXT_OFFSET_FT * factor
    out: list[WallDimension] = []
    for i in range(len(pts)):
        a, b = pts[i], pts[(i + 1) % len(pts)]
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        if length < min_length_ft or length < 1e-9:
            continue
        nx, ny = -dy / length, dx / length  # перпендикуляр к стене
        mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
        probe = (mid[0] + nx * 0.5, mid[1] + ny * 0.5)
        if not point_in_polygon(probe, pts):
            nx, ny = -nx, -ny  # наружу дома — меняем на внутрь: текст размера ставится внутри
        center = (mid[0] + nx * offset, mid[1] + ny * offset)
        angle = math.degrees(math.atan2(dy, dx))
        while angle <= -90.0:
            angle += 180.0
        while angle > 90.0:
            angle -= 180.0
        out.append(
            WallDimension(
                text=wall_dimension_text(length),
                value_ft=round_offset_ft(length),
                start=a,
                end=b,
                point=center,
                angle_deg=round(angle, 2),
            )
        )
    return out


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
    #: поворот текста, градусы: у текста без выноски — вдоль границы, у выноски — 0 (R10)
    rotation_deg: float = 0.0


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


def _boundary_angle_deg(point: Point, segments: Sequence[tuple[Point, Point]]) -> float:
    """
    Угол текста вдоль границы в точке ``point`` (градусы, от -90 до 90]: ближайший к точке отрезок границы.
    Горизонтальная граница — 0, вертикальная — 90 (R10: текст параллелен размеряемой линии).
    """
    best: tuple[float, float] | None = None
    for a, b in segments:
        d = _point_segment_distance(point, (a, b))
        if best is None or d < best[0]:
            best = (d, math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])))
    if best is None:
        return 0.0
    angle = best[1]
    while angle <= -90.0:
        angle += 180.0
    while angle > 90.0:
        angle -= 180.0
    return round(angle, 2)


def _merged_ring(ring: Sequence[Point], tol_deg: float = 2.0) -> list[Point]:
    """Склеивает промежуточные коллинеарные вершины замкнутого контура участка в цельные стороны."""
    pts = [(float(x), float(y)) for x, y in ring]
    if len(pts) >= 2 and math.dist(pts[0], pts[-1]) < 1e-6:
        pts = pts[:-1]
    sin_tol = math.sin(math.radians(tol_deg))
    changed = True
    while changed and len(pts) >= 4:
        changed = False
        for i in range(len(pts)):
            a, b, c = pts[(i - 1) % len(pts)], pts[i], pts[(i + 1) % len(pts)]
            v1 = (b[0] - a[0], b[1] - a[1])
            v2 = (c[0] - b[0], c[1] - b[1])
            l1, l2 = math.hypot(*v1), math.hypot(*v2)
            if l1 < 1e-6 or l2 < 1e-6:
                pts.pop(i)
                changed = True
                break
            cross = abs(v1[0] * v2[1] - v1[1] * v2[0]) / (l1 * l2)
            dot = (v1[0] * v2[0] + v1[1] * v2[1]) / (l1 * l2)
            if dot > 0.0 and cross <= sin_tol:
                pts.pop(i)
                changed = True
                break
    return pts


def _simplify_polyline(points: Sequence[Point], *, closed: bool, tol_deg: float = 5.0) -> list[Point]:
    """Убирает промежуточные почти коллинеарные точки съёмки вдоль прямых пролётов забора/стены."""
    pts = [(float(x), float(y)) for x, y in points]
    if len(pts) >= 2 and math.dist(pts[0], pts[-1]) < 1e-6:
        pts = pts[:-1]
        closed = True
    if closed:
        return _merged_ring(pts, tol_deg=tol_deg)
    if len(pts) <= 2:
        return pts
    sin_tol = math.sin(math.radians(tol_deg))
    out = [pts[0]]
    for i in range(1, len(pts) - 1):
        a, b, c = out[-1], pts[i], pts[i + 1]
        v1 = (b[0] - a[0], b[1] - a[1])
        v2 = (c[0] - b[0], c[1] - b[1])
        l1, l2 = math.hypot(*v1), math.hypot(*v2)
        if l1 < 1e-6 or l2 < 1e-6:
            continue
        cross = abs(v1[0] * v2[1] - v1[1] * v2[0]) / (l1 * l2)
        dot = (v1[0] * v2[0] + v1[1] * v2[1]) / (l1 * l2)
        if dot > 0.0 and cross <= sin_tol:
            continue
        out.append(b)
    out.append(pts[-1])
    return out


def _are_parallel(a1: Point, a2: Point, b1: Point, b2: Point, tol_deg: float = FENCE_PARALLEL_TOL_DEG) -> bool:
    dx1, dy1 = a2[0] - a1[0], a2[1] - a1[1]
    dx2, dy2 = b2[0] - b1[0], b2[1] - b1[1]
    l1, l2 = math.hypot(dx1, dy1), math.hypot(dx2, dy2)
    if l1 < 1e-6 or l2 < 1e-6:
        return False
    return abs(dx1 * dy2 - dy1 * dx2) / (l1 * l2) <= math.sin(math.radians(tol_deg))


def _seg_line_proj(pt: Point, sa: Point, sb: Point) -> tuple[float, Point, float] | None:
    dx, dy = sb[0] - sa[0], sb[1] - sa[1]
    length = math.hypot(dx, dy)
    if length < 1e-12:
        return None
    t = ((pt[0] - sa[0]) * dx + (pt[1] - sa[1]) * dy) / (length * length)
    beyond = max(0.0, -t * length, (t - 1.0) * length)
    foot = (sa[0] + t * dx, sa[1] + t * dy)
    return math.dist(pt, foot), foot, beyond


def _seg_crosses_or_starts_on_side(a: Point, b: Point, sa: Point, sb: Point) -> bool:
    """Пересекает ли непараллельный отрезок ``ab`` сторону ``sa-sb`` или опирается на неё концом."""
    if _are_parallel(a, b, sa, sb, tol_deg=25.0):
        return False
    pa, pb = _seg_line_proj(a, sa, sb), _seg_line_proj(b, sa, sb)
    if pa is not None and pa[2] <= 0.5 and pa[0] <= 0.15:
        return True
    if pb is not None and pb[2] <= 0.5 and pb[0] <= 0.15:
        return True
    dx1, dy1 = b[0] - a[0], b[1] - a[1]
    dx2, dy2 = sb[0] - sa[0], sb[1] - sa[1]
    denom = dx1 * dy2 - dy1 * dx2
    if abs(denom) < 1e-9:
        return False
    t = ((sa[0] - a[0]) * dy2 - (sa[1] - a[1]) * dx2) / denom
    u = ((sa[0] - a[0]) * dy1 - (sa[1] - a[1]) * dx1) / denom
    return -0.02 <= t <= 1.02 and -0.05 <= u <= 1.05


def _boundary_sides(mring: Sequence[Point], front_lines: Sequence[Sequence[Point]]) -> list[tuple[Point, Point]]:
    """Стороны склеенного контура участка плюс не дублирующие их отрезки фронта ``M-ROW-LINE``."""
    sides: list[tuple[Point, Point]] = list(zip(mring, list(mring[1:]) + list(mring[:1])))
    for fln in front_lines:
        for a, b in zip(fln, fln[1:]):
            if math.dist(a, b) < 1e-6:
                continue
            dup = False
            for sa, sb in sides:
                if _are_parallel(a, b, sa, sb, tol_deg=2.0):
                    pa, pb = _seg_line_proj(a, sa, sb), _seg_line_proj(b, sa, sb)
                    if pa and pb and pa[0] < 0.05 and pb[0] < 0.05 and pa[2] <= 0.05 and pb[2] <= 0.05:
                        dup = True
                        break
            if not dup:
                sides.append((a, b))
    return sides


def _same_polygon(area_pts: Sequence[Point] | None, ring: Sequence[Point], tol: float = 0.25) -> bool:
    """Совпадает ли выделенная область с самим контуром участка (геодезист выделил полилинию участка)."""
    if not area_pts or len(area_pts) < 3 or len(ring) < 3:
        return False
    clean_area = list(area_pts[:-1]) if math.dist(area_pts[0], area_pts[-1]) < 1e-6 else list(area_pts)
    if len(clean_area) < 3:
        return False
    return (
        all(perpendicular_distance(p, ring, closed=True)[0] <= tol for p in clean_area)
        and all(perpendicular_distance(p, clean_area, closed=True)[0] <= tol for p in ring)
    )


def _fence_modern_targets(
    mring: Sequence[Point],
    sides: Sequence[tuple[Point, Point]],
    boundaries: Sequence[tuple[Sequence[Point], bool]],
    feature: Feature,
    house_edges: Sequence[tuple[Point, Point]],
    north_deg: float,
) -> tuple[list[OffsetCandidate], list[OffsetCandidate], str | None]:
    """
    Правило 1 (модернизация H11/H14/H18): узлы поворота/концов упрощённой ломаной забора и точки
    пересечения с поперечной границей/продолжением за углом, с приоритетом параллельной стороны,
    фильтрацией внутренних примыканий к зданиям и выбором нескольких выносок на длинный/многосторонний забор.
    """
    on_line: list[OffsetCandidate] = []
    any_near = False
    for raw_v in feature.points:
        inside_v = point_in_polygon(raw_v, mring)
        out_v = 0.0 if inside_v else perpendicular_distance(raw_v, mring, closed=True)[0]
        if out_v <= OUTSIDE_MAX_FT:
            any_near = True
            d_near, foot_near = min(
                (perpendicular_line_distance(raw_v, line, closed=closed) for line, closed in boundaries),
                key=lambda pair: pair[0],
            )
            if d_near < MIN_OFFSET_FT:
                on_line.append(
                    OffsetCandidate(
                        feature=feature, vertex=raw_v, distance_ft=round(d_near, 4),
                        foot=foot_near, inside=inside_v, outside_ft=round(out_v, 4),
                    )
                )
    if not any_near:
        return [], [], "объект вне участка"

    spts = _simplify_polyline(feature.points, closed=False, tol_deg=5.0)
    segs = list(zip(spts, spts[1:]))
    total_len = sum(math.dist(a, b) for a, b in segs)
    if total_len < 5.0 and all(
        not point_in_polygon(p, mring) and perpendicular_distance(p, mring, closed=True)[0] > 1.5
        for p in spts
    ):
        return [], on_line, "объект вне участка"

    def _touches_building_away_from_lot(pt: Point) -> bool:
        if not house_edges:
            return False
        if perpendicular_distance(pt, mring, closed=True)[0] < 1.2:
            return False
        return any(_point_segment_distance(pt, edge) <= 0.5 for edge in house_edges)

    cand_pts: list[tuple[Point, list[tuple[float, Point, int, float]], list[tuple[Point, Point]]]] = []
    for v in spts:
        if _touches_building_away_from_lot(v):
            continue
        inc_segs = [(a, b) for a, b in segs if math.dist(v, a) < 1e-3 or math.dist(v, b) < 1e-3]
        pref_sides: list[tuple[float, Point, int, float]] = []
        for a, b in inc_segs:
            if math.dist(a, b) >= FENCE_PARALLEL_MIN_LEN_FT:
                mid_ab = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
                for s_idx, (sa, sb) in enumerate(sides):
                    if _are_parallel(a, b, sa, sb):
                        pr_mid = _seg_line_proj(mid_ab, sa, sb)
                        pr = _seg_line_proj(v, sa, sb)
                        if (
                            pr is not None
                            and pr_mid is not None
                            and pr_mid[2] <= 1.0
                            and pr[2] <= 2.5
                            and MIN_OFFSET_FT <= pr[0] <= PARALLEL_MAX_FT
                        ):
                            pref_sides.append((pr[0], pr[1], s_idx, pr[2]))
        cand_pts.append((v, pref_sides, inc_segs))

    # Точки пересечения длинного параллельного пролёта забора с поперечной границей или её коротким
    # продолжением у угла участка (Geldner: FE.1.7'S, FE.0.7'E, FE.1.4'E; Brooklyn: 0.4'E).
    for a, b in segs:
        dx1, dy1 = b[0] - a[0], b[1] - a[1]
        l1 = math.hypot(dx1, dy1)
        if l1 < FENCE_PARALLEL_MIN_LEN_FT:
            continue
        for sa, sb in sides:
            dx2, dy2 = sb[0] - sa[0], sb[1] - sa[1]
            l2 = math.hypot(dx2, dy2)
            denom = dx1 * dy2 - dy1 * dx2
            if l2 < 1e-6 or abs(denom) / (l1 * l2) < 0.5:
                continue
            t = ((sa[0] - a[0]) * dy2 - (sa[1] - a[1]) * dx2) / denom
            u = ((sa[0] - a[0]) * dy1 - (sa[1] - a[1]) * dx1) / denom
            beyond_u = max(0.0, -u * l2, (u - 1.0) * l2)
            if 0.0 < t < 1.0 and beyond_u <= 2.0:
                cp = (a[0] + t * dx1, a[1] + t * dy1)
                pref_cp: list[tuple[float, Point, int, float]] = []
                for s_idx, (pa, pb) in enumerate(sides):
                    if _are_parallel(a, b, pa, pb):
                        pr = _seg_line_proj(cp, pa, pb)
                        if pr is not None and pr[2] <= 2.0 and MIN_OFFSET_FT <= pr[0] <= PARALLEL_MAX_FT:
                            pref_cp.append((pr[0], pr[1], s_idx, pr[2]))
                if pref_cp:
                    cand_pts.append((cp, pref_cp, [(a, b)]))

    measured: list[tuple[float, bool, bool, float, Point, Point, int, float]] = []
    for v, pref, inc_segs in cand_pts:
        inside = point_in_polygon(v, mring)
        out_ft = 0.0 if inside else perpendicular_distance(v, mring, closed=True)[0]
        if out_ft > OUTSIDE_MAX_FT:
            continue
        if pref:
            pref.sort(key=lambda x: (round_offset_ft(x[0]), x[3] > 0.01, x[0]))
            d, foot, s_idx, bey = pref[0]
        else:
            if not inside and out_ft > 0.8:
                continue
            raw_projs: list[tuple[float, Point, int, float]] = []
            on_any_side = False
            crossed_any_side = False
            for s_idx, (sa, sb) in enumerate(sides):
                pr = _seg_line_proj(v, sa, sb)
                if pr is not None and pr[2] <= LINE_EXTENSION_FT:
                    if pr[0] < MIN_OFFSET_FT and pr[2] <= 0.05:
                        on_any_side = True
                        break
                    if pr[0] > 0.8 and inc_segs and all(
                        _seg_crosses_or_starts_on_side(a, b, sa, sb) for a, b in inc_segs
                    ):
                        crossed_any_side = True
                        continue
                    if pr[0] >= MIN_OFFSET_FT:
                        raw_projs.append((pr[0], pr[1], s_idx, pr[2]))
            if on_any_side:
                continue
            if crossed_any_side:
                raw_projs = [p for p in raw_projs if p[0] <= PARALLEL_MAX_FT]
            if not raw_projs and inside and not crossed_any_side:
                # Внутренний объект далеко от коротких сторон: запасное расстояние до ближайшего отрезка.
                d_fb, foot_fb = min(
                    (perpendicular_line_distance(v, line, closed=closed) for line, closed in boundaries),
                    key=lambda pair: pair[0],
                )
                if d_fb >= MIN_OFFSET_FT:
                    raw_projs.append((d_fb, foot_fb, -1, 0.0))
            if not raw_projs:
                continue
            raw_projs.sort(key=lambda x: (round_offset_ft(x[0]), x[3] > 0.01, x[0]))
            d, foot, s_idx, bey = raw_projs[0]
        val = round_offset_ft(d)
        if val <= 0.0:
            continue
        measured.append((val, not inside, bey > 0.01, d, v, foot, s_idx, out_ft))

    if not measured:
        return [], on_line, ("стоит на границе" if on_line else "объект вне участка")

    measured.sort(key=lambda m: (m[0], m[1], m[2], m[3]))
    multi_seg_fence = len(spts) >= 3
    chosen: list[tuple[float, bool, Point, Point, int, OffsetCandidate]] = []
    for val, not_inside, _bey_flag, d, v, foot, s_idx, out_ft in measured:
        if chosen and d > PARALLEL_MAX_FT:
            continue
        dir1 = compass_letter(v[0] - foot[0], v[1] - foot[1], north_deg)
        has_extra = bool(_extra_sides(v, foot, (val, dir1), boundaries, north_deg))
        too_close = False
        for c_val, c_extra, c_v, _c_foot, c_sidx, _c_cand in chosen:
            dist_vv = math.dist(v, c_v)
            min_sep = FENCE_SAME_SIDE_SEP_FT if s_idx == c_sidx else FENCE_DIFF_SIDE_SEP_FT
            if dist_vv < min_sep:
                too_close = True
                break
            if s_idx == c_sidx and val == c_val:
                if not (multi_seg_fence and has_extra != c_extra):
                    too_close = True
                    break
        if not too_close:
            cand = OffsetCandidate(
                feature=feature,
                vertex=v,
                distance_ft=round(d, 4),
                foot=foot,
                inside=not not_inside,
                outside_ft=round(out_ft, 4),
                segment=s_idx if s_idx >= 0 else 0,
            )
            chosen.append((val, has_extra, v, foot, s_idx, cand))

    return [item[5] for item in chosen], on_line, None


def _house_modern_targets(
    mring: Sequence[Point],
    sides: Sequence[tuple[Point, Point]],
    boundaries: Sequence[tuple[Sequence[Point], bool]],
    feature: Feature,
) -> tuple[list[OffsetCandidate], str | None]:
    """
    Правило 3 (модернизация R5/H18): по одному прямому оффсету на каждую сближенную сторону участка
    (до ``HOUSE_SIDE_MAX_FT``), а если строение подходит только к одной границе на протяжении >= 15 ft
    с двумя разными значениями <= 3.6 ft — оба угла этой стены; для внутренних домов дальше 6.2 ft —
    один ближайший оффсет.
    """
    closed = feature.is_closed
    spts = _simplify_polyline(feature.points, closed=closed, tol_deg=3.0)
    if len(spts) < 2:
        return [], "стоит на границе"
    if all(perpendicular_distance(p, mring, closed=True)[0] < MIN_OFFSET_FT for p in spts):
        return [], "стоит на границе"

    any_near = False
    any_inside = False
    min_out = math.inf
    for p in feature.points:
        inside_p = point_in_polygon(p, mring)
        any_inside = any_inside or inside_p
        out_p = 0.0 if inside_p else perpendicular_distance(p, mring, closed=True)[0]
        min_out = min(min_out, out_p)
        if out_p <= OUTSIDE_MAX_FT:
            any_near = True
    if not any_near:
        return [], "объект вне участка"
    span_x = max(p[0] for p in spts) - min(p[0] for p in spts)
    span_y = max(p[1] for p in spts) - min(p[1] for p in spts)
    if not any_inside and min_out > 1.5 and max(span_x, span_y) < 10.0:
        return [], "объект вне участка"

    b_edges = _segments(spts, closed)
    by_side: dict[int, list[tuple[float, bool, bool, float, Point, Point, float]]] = {}
    for s_idx, (sa, sb) in enumerate(sides):
        # Если стена строения (>= 5 ft) уже лежит прямо по этой границе (party wall / по линии) или
        # непараллельная стена пересекает эту границу (таунхаус) — оффсет к этой стороне не ставится.
        crosses_or_on_side = False
        for ba, bb in b_edges:
            if _are_parallel(ba, bb, sa, sb, tol_deg=15.0):
                if math.dist(ba, bb) >= 5.0:
                    pa, pb = _seg_line_proj(ba, sa, sb), _seg_line_proj(bb, sa, sb)
                    if pa is not None and pb is not None and pa[0] < MIN_OFFSET_FT and pb[0] < MIN_OFFSET_FT:
                        crosses_or_on_side = True
                        break
                continue
            dx1, dy1 = bb[0] - ba[0], bb[1] - ba[1]
            dx2, dy2 = sb[0] - sa[0], sb[1] - sa[1]
            denom = dx1 * dy2 - dy1 * dx2
            if abs(denom) > 1e-6:
                t = ((sa[0] - ba[0]) * dy2 - (sa[1] - ba[1]) * dx2) / denom
                u = ((sa[0] - ba[0]) * dy1 - (sa[1] - ba[1]) * dx1) / denom
                if 0.0 <= t <= 1.0 and 0.02 < u < 0.98:
                    # Если сама стена (или соседние точки контура у пересечения) уходит по обе стороны
                    # границы дальше 0.5 ft — это сквозной фасад/задняя стена таунхауса, а не угол дома.
                    signed = [
                        (p[0] - sa[0]) * dy2 - (p[1] - sa[1]) * dx2
                        for p in spts
                        if (_seg_line_proj(p, sa, sb) or (0.0, p, 99.0))[2] <= 0.5
                    ]
                    if signed and min(signed) / math.hypot(dx2, dy2) < -0.5 and max(signed) / math.hypot(dx2, dy2) > 0.5:
                        crosses_or_on_side = True
                        break
        if crosses_or_on_side:
            continue
        cands: list[tuple[float, bool, bool, float, Point, Point, float]] = []
        for v in spts:
            inside = point_in_polygon(v, mring)
            out_ft = 0.0 if inside else perpendicular_distance(v, mring, closed=True)[0]
            if out_ft > OUTSIDE_MAX_FT:
                continue
            if perpendicular_distance(v, mring, closed=True)[0] < MIN_OFFSET_FT:
                continue
            pr = _seg_line_proj(v, sa, sb)
            if pr is not None and pr[2] <= 0.25 and MIN_OFFSET_FT <= pr[0] <= HOUSE_SIDE_MAX_FT:
                val = round_offset_ft(pr[0])
                if val > 0.0:
                    cands.append((val, not inside, pr[2] > 0.01, pr[0], v, pr[1], out_ft))
        if cands:
            cands.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
            by_side[s_idx] = cands

    if by_side:
        picked: list[OffsetCandidate] = []
        for s_idx, cands in by_side.items():
            best = cands[0]
            picked.append(
                OffsetCandidate(
                    feature=feature, vertex=best[4], distance_ft=round(best[3], 4),
                    foot=best[5], inside=not best[1], outside_ft=round(best[6], 4), segment=s_idx,
                )
            )
            if len(by_side) == 1 and best[0] <= HOUSE_TWO_CORNER_MAX_FT:
                far = [
                    c for c in cands[1:]
                    if c[0] <= HOUSE_TWO_CORNER_MAX_FT
                    and c[0] != best[0]
                    and math.dist(c[4], best[4]) >= HOUSE_TWO_CORNER_SPAN_FT
                ]
                if far:
                    sec = far[0]
                    picked.append(
                        OffsetCandidate(
                            feature=feature, vertex=sec[4], distance_ft=round(sec[3], 4),
                            foot=sec[5], inside=not sec[1], outside_ft=round(sec[6], 4), segment=s_idx,
                        )
                    )
        picked.sort(key=lambda c: (round_offset_ft(c.distance_ft), not c.inside, c.distance_ft))
        return picked, None

    # Строение внутри участка дальше HOUSE_SIDE_MAX_FT от всех сторон (например дом в глубине двора):
    # один ближайший прямой оффсет, без ложных боковых продолжений соседских строений.
    fallback = [
        c for c in measure_candidates(mring, [feature], front=[list(s) for s in sides[len(mring):]])
        if c.inside and c.distance_ft >= MIN_OFFSET_FT and round_offset_ft(c.distance_ft) > 0.0
    ]
    if not fallback:
        return [], "стоит на границе"
    fallback.sort(key=lambda c: (round_offset_ft(c.distance_ft), not c.inside, c.distance_ft))
    return fallback[:1], None


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
    Шаг 4, версия 4.20: оффсеты объектов в выбранной области.

    ``property_pts`` — граница участка (замкнутый контур); ``front`` — линии ``M-ROW-LINE``
    (фронтовая граница, ``M-TAX-LINE`` сюда не передаётся); ``area`` — прямоугольник или полигон,
    который указал геодезист (``None`` — весь участок); ``texts`` — уже стоящие подписи
    ``(текст, точка, высота)``: рядом с тем же текстом оффсет не дублируется, а текст не накрывается.
    """
    plan = OffsetPlan()
    ring = [(float(x), float(y)) for x, y in property_pts]
    if len(ring) >= 2 and ring[0] == ring[-1]:
        ring = ring[:-1]
    if len(ring) < 3:
        plan.warnings.append("граница участка: меньше трёх вершин — оффсеты не строятся")
        return plan
    mring = _merged_ring(ring)
    factor = scale / BASE_SCALE if scale and scale > 0 else 1.0
    width_of = text_width or default_text_width
    area_pts = [(float(x), float(y)) for x, y in area] if area else None
    area_is_lot = _same_polygon(area_pts, ring)
    front_lines = [[(float(x), float(y)) for x, y in line] for line in front if len(line) >= 2]
    sides = _boundary_sides(mring, front_lines)
    boundaries: list[tuple[Sequence[Point], bool]] = [(mring, True)]
    boundaries += [(line, False) for line in front_lines]

    known = [(str(t), (float(p[0]), float(p[1])), float(h) or LABEL_HEIGHT_FT * factor) for t, p, h in texts]
    # Препятствия для текста: уже стоящие подписи и линии, которые текст не должен пересекать.
    text_rects = [_rect_at(p, width_of(t, h), h) for t, p, h in known]
    lines: list[tuple[Point, Point]] = []
    for i in range(len(ring)):
        lines.append((ring[i], ring[(i + 1) % len(ring)]))
    for line in front_lines:
        lines += list(zip(line, line[1:]))
    house_edges: list[tuple[Point, Point]] = []
    for feature in features:
        pts = list(feature.points)
        if len(pts) >= 2:
            closed = feature.is_closed
            seq = pts + [pts[0]] if closed else pts
            f_segs = list(zip(seq, seq[1:]))
            lines += f_segs
            if feature.kind == "house":
                house_edges.extend(f_segs)

    # Границы, к которым меряются оффсеты: контур участка и фронт. По ним задаётся угол текста (R10).
    boundary_segs: list[tuple[Point, Point]] = list(zip(mring, mring[1:] + mring[:1]))
    for line in front_lines:
        boundary_segs += list(zip(line, line[1:]))
    placed: list[Rect] = []
    dup_radius = DUPLICATE_RADIUS_FT * factor

    plan.counts["zone"] = len(features)
    for index, feature in enumerate(features):
        label = feature.handle or f"#{index}"
        if area_pts is None:
            in_area = True
        elif area_is_lot:
            in_area = any(
                point_in_polygon(p, ring) or perpendicular_distance(p, ring, closed=True)[0] <= OUTSIDE_MAX_FT
                for p in feature.points
            )
        else:
            in_area = any(point_in_polygon(p, area_pts) for p in feature.points)
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

        used: set[tuple] = set()
        rest_seen = False
        if CORNER_LABELS:
            candidates = [
                c for c in _feature_candidates(ring, feature, front_lines)
                if c.outside_ft <= OUTSIDE_MAX_FT
            ]
            if not candidates:
                plan.skipped.append((label, "объект вне участка"))
                continue
            on_line = [c for c in candidates if c.distance_ft < MIN_OFFSET_FT]
            candidates = [c for c in candidates if c.distance_ft >= MIN_OFFSET_FT]
            if not candidates and not (ONLINE_LABELS and feature.kind == "fence" and on_line):
                plan.skipped.append((label, "стоит на границе"))
                continue
            candidates.sort(key=lambda c: (round_offset_ft(c.distance_ft), not c.inside, c.distance_ft))
            targets: list[tuple[OffsetCandidate, bool]] = [(best, False) for best in candidates]
            if ONLINE_LABELS and feature.kind == "fence" and on_line:
                targets.append((on_line[0], True))
        elif feature.kind in ("fence", "wall"):
            chosen_cands, on_line, skip_reason = _fence_modern_targets(
                mring, sides, boundaries, feature, house_edges, north_deg
            )
            targets = [(best, False) for best in chosen_cands]
            if ONLINE_LABELS and feature.kind == "fence" and on_line:
                targets.append((on_line[0], True))
            if not targets:
                plan.skipped.append((label, skip_reason or "стоит на границе"))
                continue
        else:
            house_cands, skip_reason = _house_modern_targets(mring, sides, boundaries, feature)
            if not house_cands:
                plan.skipped.append((label, skip_reason or "стоит на границе"))
                continue
            targets = [(best, False) for best in house_cands]

        for best, is_online in targets:
            value = round_offset_ft(best.distance_ft)
            if not is_online and value <= 0.0:
                plan.skipped.append((label, "стоит на границе"))
                continue
            if CORNER_LABELS and not is_online and best.segment < 0:
                if rest_seen:
                    continue
                rest_seen = True
            seg_key = ("seg", best.segment, value)
            if CORNER_LABELS and seg_key in used:
                continue
            vx, vy = best.vertex[0] - best.foot[0], best.vertex[1] - best.foot[1]
            direction = compass_letter(vx, vy, north_deg)
            height = offset_height_ft("house" if feature.kind == "house" else "fence", scale)
            extra_raw: list[tuple[float, str, Point]] = []
            if feature.kind in ("fence", "wall") and not is_online:
                extra_raw = _extra_sides_with_feet(best.vertex, best.foot, (value, direction), boundaries, north_deg)
                if extra_raw:
                    sec_val, _sec_dir, sec_foot = extra_raw[0]
                    direction, sec_dir = compass_axis_pair(
                        vx, vy, best.vertex[0] - sec_foot[0], best.vertex[1] - sec_foot[1], north_deg
                    )
                    extra = [(sec_val, sec_dir)] + [(v, d) for v, d, _f in extra_raw[1:]]
                else:
                    extra = []
            else:
                extra = []
            text = ONLINE_FENCE_TEXT if is_online else offset_text(feature.kind, value, direction)
            leader_head = ONLINE_FENCE_TEXT if is_online else offset_text(feature.kind, value, direction, leader=True)
            full_leader_text = "\n".join(
                [leader_head] + [offset_text(feature.kind, v, d) for v, d in extra]
            )
            check_text = text if feature.kind == "house" else full_leader_text
            mid = ((best.vertex[0] + best.foot[0]) / 2.0, (best.vertex[1] + best.foot[1]) / 2.0)
            fpts = list(feature.points)
            seq_pts = fpts + [fpts[0]] if feature.is_closed else fpts
            if CORNER_LABELS and 0 <= best.segment < len(seq_pts) - 1:
                edges = [(seq_pts[best.segment], seq_pts[best.segment + 1])]
            else:
                edges = list(zip(seq_pts, seq_pts[1:]))
            if any(
                _key(old) in {_key(text), _key(check_text)} and min(
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
                if area_pts is not None and not area_is_lot and not _rect_inside_polygon(rect, area_pts):
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
                    rotation_deg=(
                        _boundary_angle_deg(best.foot, boundary_segs) if TEXT_FOLLOWS_BOUNDARY else 0.0
                    ),
                )
            else:
                # Дом без места под текст получает выноску с «BLDG»; забор и стена — выноску как есть.
                leader_text = full_leader_text
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


def _extra_sides_with_feet(
    vertex: Point, best_foot: Point, taken: tuple[float, str],
    boundaries: Sequence[tuple[Sequence[Point], bool]], north_deg: float,
) -> list[tuple[float, str, Point]]:
    """
    Правило 2 (модернизация R4/H16): другие стороны участка ближе ``SECOND_SIDE_FT``.
    Для обычных сторон основание должно лежать внутри отрезка (``extension_ft=0``), а для сторон,
    образующих общий угол участка рядом с вершиной (в радиусе ``CORNER_SHARED_RADIUS_FT``),
    допускается короткое продолжение за углом (``CORNER_SECOND_EXTENSION_FT``) — это ловит тупые и
    наружные углы (Alvine «1.8'S / 1.4'E», Geldner «0.1'E / 1.2'S»).
    """
    all_segs: list[tuple[Point, Point]] = []
    for line, closed in boundaries:
        all_segs.extend(_segments(line, closed))
    primary_segs = [
        (sa, sb) for sa, sb in all_segs
        if _point_segment_distance(best_foot, (sa, sb)) <= 0.1
        or (_seg_line_proj(best_foot, sa, sb) is not None and _seg_line_proj(best_foot, sa, sb)[0] <= 0.05)
    ]

    found: list[tuple[float, float, str, Point]] = []
    for sa, sb in all_segs:
        if _are_parallel(sa, sb, *primary_segs[0], tol_deg=15.0) if primary_segs else False:
            if len(primary_segs) == 1:
                continue
        shares_near_corner = any(
            math.dist(c1, c2) <= 0.5 and math.dist(vertex, c1) <= CORNER_SHARED_RADIUS_FT
            for pa, pb in primary_segs
            for c1 in (pa, pb)
            for c2 in (sa, sb)
        )
        max_ext = CORNER_SECOND_EXTENSION_FT if shares_near_corner else 0.0
        pr = _seg_line_proj(vertex, sa, sb)
        if pr is None:
            continue
        distance, foot, beyond = pr
        if beyond > max_ext:
            continue
        if math.dist(foot, best_foot) < 1e-6 or distance < MIN_OFFSET_FT or distance > SECOND_SIDE_FT:
            continue
        value = round_offset_ft(distance)
        if value <= 0.0:
            continue
        direction = compass_letter(vertex[0] - foot[0], vertex[1] - foot[1], north_deg)
        found.append((distance, value, direction, foot))
    found.sort(key=lambda item: item[0])
    out: list[tuple[float, str, Point]] = []
    seen_pairs: set[tuple[float, str]] = set()
    for _distance, value, direction, foot in found:
        pair = (value, direction)
        if pair != taken and pair not in seen_pairs:
            seen_pairs.add(pair)
            out.append((value, direction, foot))
    return out


def _extra_sides(
    vertex: Point, best_foot: Point, taken: tuple[float, str],
    boundaries: Sequence[tuple[Sequence[Point], bool]], north_deg: float,
) -> list[tuple[float, str]]:
    """Другие стороны участка ближе ``SECOND_SIDE_FT``: пары ``(значение, буква)``."""
    return [(v, d) for v, d, _foot in _extra_sides_with_feet(vertex, best_foot, taken, boundaries, north_deg)]


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
