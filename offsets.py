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
STEP4_VERSION = "4.27"
#: Допуск совпадения координат двух дампов, футы.
SAME_FRAME_TOL_FT = 0.01

Point = tuple[float, float]


# --------------------------------------------------------------------------- тексты и высоты


def building_height_ft(
    feature: Feature,
    texts: Sequence[tuple[str, Point, float]],
    scale: float = BASE_SCALE,
) -> float:
    """
    Высота подписи строения: 1.1 у дома, 0.9 у постройки.

    Постройка определяется по её же надписи в чертеже («FRAME GARAGE», «VINYL SHED»,
    «OPEN PORCH»): у Николая такие подписи стоят на 0.9, а подписи домов — на 1.1.
    """
    default = offset_height_ft("house" if feature.kind == "house" else "fence", scale)
    if not texts or feature.kind != "house":
        return default
    contour, closed = feature_contour(feature)
    if len(contour) < 3:
        return default
    for text, point, _height in texts:
        clean = clean_text(text).upper()
        if not any(word in clean for word in OUTBUILDING_WORDS):
            continue
        near = point_in_polygon(point, contour) or any(
            math.dist(point, vertex) <= 3.0 for vertex in contour
        )
        if near:
            return offset_height_ft("fence", scale)
    return default


def _vertex_on_segments(vertex: Point, segments: Sequence[tuple[Point, Point]], tol: float) -> bool:
    """Вершина лежит на одном из отрезков (в пределах допуска)."""
    if tol <= 0:
        return False
    return any(_point_segment_distance(vertex, seg) <= tol for seg in segments)


def _vertex_on_ring(vertex: Point, ring: Sequence[Point], tol: float) -> bool:
    """Вершина лежит на контуре участка (в пределах допуска)."""
    if len(ring) < 2 or tol <= 0:
        return False
    return _vertex_on_segments(vertex, list(zip(ring, list(ring[1:]) + [ring[0]])), tol)


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

#: Допуск области выделения: объект считается попавшим в область, если он ближе этого
#: к её границе. Оффсеты по определению стоят у границы участка, поэтому при выделении
#: самой полилинии участка (``area == ring``) забор в 0.3 ft снаружи выпадал из области:
#: на 192 Geldner оставалось 5 подписей вместо 14.
AREA_EDGE_TOL_FT = 6.0
#: подпись может стоять снаружи участка (в яру, у ворот): у заказчика 4 из 8 подписей вне линии участка
LABEL_MUST_BE_INSIDE = False
#: стены подписываются оффсетом по тому же правилу, что заборы (отсутствие подписи у заказчика — не запрет)
WALL_OFFSETS = False
#: Расстояние от объекта до текста выноски при 1"=16' (в порядке попыток), футы.
#: Добавка к расстоянию «край блока + зазор» при поиске места выноски, футы.
#: У Николая подпись стоит вплотную к концу выноски (2.7–3.5 ft от вершины при высоте 0.9),
#: поэтому лестница короткая: 0, 0.5, 1.5, 3 ft.
LEADER_DISTANCES_FT = (0.0, 0.5, 1.5, 3.0)
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
#: Допуск «вершина на контуре участка» для правила ``ONLINE_LABELS``, футы.
ONLINE_TOL_FT = 0.75
#: Вторая сторона у той же вершины: если она ближе этого, пишем её второй строкой той же выноски.
#: Примеры: «FE.1.8'S \\P FE.1.4'E» (1.84 и 1.44), «FE.0.1'S \\P FE.0.5'W» (0.07 и 0.52).
SECOND_SIDE_FT = 2.0
#: Забор, у которого вершина стоит на линии участка (до MIN_OFFSET_FT), подписывается «FE.O/L» — «на линии».
#: За: 192 Geldner, забор слева посередине (вершина в 0.001 ft от границы) — у Николая «FE.O/L».
#: Против: на Brooklyn (4.10) такая вершина не подписана «O/L», забор подписан по другим вершинам.
#: 4.24 (четвёртая пара, 378 Mother Gaston): включаем. Николай пишет «O/L» там, где объект стоит
#: на линии: Geldner ``FE.O/L``, Brooklyn ``BR.WALL`` / ``O/L``, Gaston ``BLDG`` / ``O/L`` дважды.
#: У дома и стены — только на фронтовой линии (`M-ROW-LINE`), у забора — на любом контуре:
#: на боковых линиях участка Gaston дом подписан числом (``BLDG`` / ``0.2'S``), а не «O/L».
#: Числовая подпись у такой вершины не ставится: смещения нет.
ONLINE_LABELS = True
ONLINE_FENCE_TEXT = "FE.O/L"
#: Вторая строка подписи «на линии» у дома и стены: ``BLDG`` / ``O/L`` (378 Mother Gaston).
ONLINE_TEXT = "O/L"
#: R10: текст **без выноски** поворачивается вдоль своей линии (0° у горизонтальной, 90° у вертикальной);
#: текст с выноской у Николая горизонтален, поэтому правило применяется только к ``style == "text"``.
#: Замер 4.26 по дампам:
#: * Ilion (участок 0.4°): «FE.0.1'E» — 90.4°, «FE.1.3'E» — 0.4°, «C.L.F.\P0.1'W» — 0.4° — 18 из 19
#:   подписей стоят по осям участка (одна, «FE.0.6'E» — 70.8°);
#: * Aspinwall (участок 14.6°): 14.6° и 284.6° — те же две оси участка, 0 из 24 «по сетке листа»;
#: * 6910 15th Ave: 356.5° (ось участка) у всех подписей;
#: * четыре полевые пары: из наших подписей без выносок поворот совпал с Николаем у 5 из 5
#:   (Gaston «2.4'S» — 88.6°, Geldner «0.4'W» — 90°), с выключенным правилом — у 2 из 5.
#: Раньше флаг был выключен из-за чтения тех же дампов: 284.6° принималось за «не вдоль стороны»,
#: хотя это вторая ось участка, повёрнутого на 14.6°.
TEXT_FOLLOWS_BOUNDARY = True


#: противоположная буква стороны участка (для объекта снаружи)
_OPPOSITE = {"N": "S", "S": "N", "E": "W", "W": "E"}


# --------------------------------------------------------------------------- R11–R13: правило «своей стороны»
#
# Три правила, которые подняли число совпадений с эталонами Николая (4.20):
#
# R11 (своя сторона). Оффсет меряется по нормали к СВОЕМУ краю объекта, а не к ближайшей точке
#   границы. Замер — до той стороны участка, нормаль которой параллельна нормали края (допуск
#   ``EDGE_PARALLEL_TOL_DEG``). Пример: забор вдоль западной границы, у которого дальний конец
#   отогнут на 0.04 ft в сторону, у Николая подписан «0.4'W» (нормаль края), а не «0.3'N»
#   (ближайшая точка границы). На 192 Geldner это дало 4 попадания вместо 1.
#
# R12 (две строки у вершины). Вершина, к которой сходятся ДВА края, подписывается по каждому краю:
#   основной строкой — меньший замер, второй — если он не дальше ``SECOND_EDGE_FT``.
#   Пример: «W.FE. \P 0.1'E \P 1.2'S» и «C.L.F. \P 0.6'S \P 1.1'E» (192 Geldner): 0.12 по
#   вертикальному краю (E) и 1.23 по горизонтальному (S) — это разные края одной вершины.
#   До 4.20 вторая строка искалась по той же стороне участка и терялась.
#
# R13 (какие вершины подписываются). Метка ставится только у вершины, где стоит точка съёмки
#   (``SURVEY_POINT_SNAP_FT``): Николай подписывает снятые углы, а не каждый излом линии.
#   Без этого правила на 192 Geldner выходит 27 подписей при 11 у Николая.
#
# Правила включаются флагами: выключены — поведение прежнее (4.19).
EDGE_NORMAL_OFFSETS = True
#: нормаль края считается параллельной нормали стороны участка при расхождении не более, градусы
EDGE_PARALLEL_TOL_DEG = 3.0
#: вторая строка у вершины: второй край подписывается, если он ближе этого расстояния, футы.
#: У Николая вторая строка встречается и на 3.0 ft: Brooklyn, угол дома `2.1'N` и `3.0'W`
#: (замер до западной стороны 3.0 ft — при пороге 2.0 вторая строка пропадала, и подпись
#: не совпадала с эталоном). Дальше 3 ft вторых строк в дампах нет.
SECOND_EDGE_FT = 3.0
#: метка ставится только у вершины, где есть точка съёмки (блок PNT-X), в пределах, футы
SURVEY_POINT_SNAP_FT = 0.6
#: только снятые вершины подписываются (R13)
LABEL_ONLY_SURVEYED_VERTICES = True
#: R13b: у забора/стены подписывается угол, где описание точки съёмки называет этот объект
SURVEY_DESC_RULE = True
#: не больше стольких углов одного объекта на одну сторону участка (углы берутся ближайшие):
#: у Николая на одну сторону забора приходится один угол, а «второй» оказывается углом соседнего
#: объекта в том же углу участка (Brooklyn: «0.3'W» у двух концов коротких заборов не подписаны).
#: Исключения из кэпа: углы, разнесённые вдоль стороны дальше ``SIDE_CAP_SPACING_FT`` (R13c),
#: и второй угол с другим замером — перекос стены (``SKEW_PAIR_FT``, R13d).
MAX_CORNERS_PER_SIDE = 1
#: для дома/сарая — свой предел: у Николая на сторону дома приходится до трёх углов
#: (Brooklyn: 3.5'S, 4.0'N, 3.0'W — по метке на юг, север и запад)
MAX_CORNERS_PER_SIDE_HOUSE = 2
#: R13d «перекос»: второй угол на той же стороне участка подписывается, если его замер
#: отличается от уже подписанного хотя бы на столько, футы. У Николая подписаны оба конца
#: стены, идущей вдоль границы непараллельно: на 378 Mother Gaston забор fence/4 подписан
#: `1.0'S` у северного конца и `2.2'S` у южного, на 192 Geldner — `0.6'E` рядом с `0.7'E`.
#: При равных замерах (стена параллельна границе) второй конец не дублируется: если разрешить
#: второй угол без этой разницы (``MAX_CORNERS_PER_SIDE = 2``), на Geldner появляется лишняя
#: `FE.0.3'W` при уже стоящей `FE.0.4'W`. 0 — правило выключено.
SKEW_PAIR_FT = 0.2
#: R13c: кэп «один угол на сторону» не применяется к углам, разнесённым вдоль этой стороны
#: дальше этого расстояния: у Brooklyn один забор идёт вдоль западной стороны 100 ft, и Николай
#: подписал его у двух концов («0.1'S» у 4922 и «0.1'S \P 0.5'W» у 4959). Футы.
SIDE_CAP_SPACING_FT = 30.0
#: R13b: метка ставится у вершины, где точка съёмки ОПИСЫВАЕТ этот объект (``DESC2`` про забор,
#: стену или дом). Николай подписывает не каждый снятый угол, а снятый угол этого объекта:
#: на 192 Geldner 211 точки съёмки дают 11 подписей забора — только там, где в DESC2 стоит
#: «VINYL FENCE» / «WOOD FENCE» / «F E C». Радиус привязки описания к вершине, футы.
SURVEY_DESC_TOL_FT = 0.6
#: слова описания точки, которые относят снятый угол к забору и к стене
FENCE_DESC_WORDS = ("FENCE", "F.E.C", "F E C", "FEC", "C.L.F", "CHAIN", "VINYL", "WOOD")
WALL_DESC_WORDS = ("WALL",)
#: R16: дубль, стоящий уже в «до»-файле (тот же текст рядом с той же вершиной): не повторять
EXISTING_LABEL_RADIUS_FT = 6.0
#: R14: сколько раз один и тот же замер («0.3'W») ставится у одного объекта. У Николая повтор
#: встречается у заборов (два «0.7'E» на 192 Geldner, два «0.1'S» у забора на Brooklyn),
#: но третий такой угол уже не подписан.
MAX_SAME_VALUE_PER_FEATURE = 2
#: R14: две вершины одного объекта ближе этого — один угол: у прямого забора оба конца стоят
#: у одной стороны, а стык двух заборов в углу участка даёт две вершины в 0.2 ft друг от друга
#: (Brooklyn: «0.1'S \P 0.5'W» и «0.5'W \P 0.2'S» — у Николая одна подпись).
NODE_DUP_FT = 0.35
#: R14: ту же близость применяем и без совпадения замера (это один и тот же угол чертежа)
NODE_DUP_ANY_VALUE = True
#: R11: если своей (параллельной) стороны в пределах ``EDGE_MAX_FT`` нет, мерим до ближайшей
#: стороны участка в пределах этого радиуса — забор в глубине участка подписан и у Николая
EDGE_FALLBACK_FT = 0.0
#: R11: насколько далеко от вершины может быть «своя» сторона участка, футы.
#: Замер по нормали до параллельной стороны: 0.1–3.0 ft у заборов и домов; 4.9'N (дом Geldner)
#: уже не входит — у Николая это замер до южной линии, а сторона с параллельной нормалью дальше.
EDGE_MAX_FT = 3.0
#: Часть подписи не повторяется отдельной подписью рядом с полной (H32 соседней ветки).
#: Когда у вершины стоит двухстрочная подпись «0.1'E / 1.2'S», отдельная «1.2'S» в 2 ft от
#: неё — дубль; на 192 Geldner это снимает 3 из 19 подписей по заборам, не теряя совпадений.
POST_PART_DEDUPE = True
PART_DUPLICATE_FT = 2.0

#: Забор, обходящий угол участка снаружи, получает подпись в точке, где он пересекает
#: продолжение стороны (H27 соседней ветки): 192 Geldner — FE.0.7'E и FE.1.4'E на юго-востоке,
#: где заборы идут в 0.7 и 1.4 ft восточнее восточной границы и пересекают линию южной.
#: Ставится только когда оба конца отрезка вне участка (забор обходит угол), иначе это
#: выход забора за границу — такой конец подписывается по своим вершинам.
CROSSING_LABELS = True

#: Конец длинного прогона забора подписывается и в 5–6 ft от стороны (H33 соседней ветки).
#: 192 Geldner: ``FE.5.2'S`` — забор идёт в 5.17 ft южнее южной границы, Николай его подписал.
#: Только для концов прогона не короче ``FENCE_END_MIN_LENGTH_FT``: на Brooklyn такой же
#: вылет 5.3 ft у 6-футового выступа Николай не подписал.
FENCE_END_MAX_FT = 6.0
FENCE_END_MIN_LENGTH_FT = 20.0

#: Буква диагональной стороны — по осям листа. На 24 Alvine Николай пишет ``1.3'W`` и
#: ``1.4'E`` у наклонной восточной стороны, хотя при севере 67.6° «ближайшая ось»
#: дала бы N/S. У ортогональных сторон правило ничего не меняет.
DIAGONAL_SHEET_AXES = True
DIAGONAL_TOL_DEG = 10.0

#: Шаг строк многострочной подписи в высотах текста — тот же, что у подписей объектов
#: (`mtext_line_spacing_factor` = 1.67 в настройках). С шагом 1.0 двухстрочный блок
#: считался ниже, чем он есть, и при раскладке наезжал на соседние надписи.
TEXT_LINE_SPACING = 1.67

#: Зазор между подписями при раскладке, в высотах текста. Шрифт в AutoCAD шире нашей оценки
#: (0.6 высоты на знак), поэтому блоки, проверенные «впритык», в чертеже слипались.
LABEL_CLEARANCE = 0.35

#: Если со зазором места нет вовсе — пробуем ещё раз без зазора. Иначе подпись не ставится
#: там, где Николай её поставил, потеснив соседние: у него минимальный зазор между
#: подписями 0.72–0.84 ft (0.8 высоты), и такие места строгая проверка считала занятыми.
LABEL_RELAXED_RETRY = True

#: Высота подписи постройки (гараж, сарай, крыльцо) — 0.9 вместо 1.1 у дома. По дампам:
#: Brooklyn `1.9'N`, `3.0'W`, `2.3'S` и Geldner `0.4'W`, `0.3'W`, `0.8'N` стоят на 0.9,
#: подписи самих домов (`3.5'S`, `4.0'N`, `3.2'S`, `3.5'S`, `4.9'N`) — на 1.1.
OUTBUILDING_WORDS = ("GARAGE", "SHED", "PORCH", "DECK", "CARPORT", "GREENHOUSE", "GAZEBO")

#: Текст дома — внутри строения, вплотную к стене, со сдвигом вдоль неё. Николай ставит
#: подпись дома у стены изнутри и отодвигает её от крупной надписи («1 STY FRAME»).
HOUSE_TEXT_INSIDE = True

#: Подпись забора и стены — выноской, как у Николая.
#:
#: В 4.21 было наоборот (по разбору соседней ветки «стрелок нет ни одной»), но дампы
#: «после» говорят другое: у всех 14 новых подписей забора на трёх чертежах есть
#: привязанная выноска (``Leader.annotation`` = ручка текста), все выноски по две точки
#: и тип ``leader_type`` = 3 (сплайн со стрелкой). Текст без выноски у Николая только
#: у домов и построек (``0.4'W``, ``3.5'S``).
FENCE_TEXT_WITHOUT_LEADER = False

#: вершины на самой линии участка: метка ставится по второму краю (первый даёт ноль)
ON_LINE_USES_EDGE = True

@dataclass(frozen=True)
class Feature:
    """Объект, от которого может идти оффсет: дом, забор, стена, заезд."""

    kind: str  # "house" | "fence" | "wall" | "driveway" | "other"
    handle: str
    points: tuple[Point, ...]
    #: Замкнут ли контур: ``True`` — да, ``False`` — ломаная (у здания, обведённого
    #: П-образной ломаной, замыкающая «стена» стеной не является), ``None`` — как раньше
    #: (дом замыкается, забор нет).
    closed: bool | None = None


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


def online_text(kind: str) -> str:
    """Подпись объекта, стоящего на линии участка: ``FE.O/L``, ``BLDG`` / ``O/L``, ``WALL`` / ``O/L``."""
    if kind == "fence":
        return ONLINE_FENCE_TEXT
    if kind == "wall":
        return f"WALL\n{ONLINE_TEXT}"
    return f"BLDG\n{ONLINE_TEXT}"


def offset_prefix(kind: str) -> str:
    """Префикс подписи по типу объекта: ``FE.`` у забора, ``WALL `` у стены, иначе пусто."""
    if kind == "fence":
        return "FE."
    if kind == "wall":
        return "WALL "
    return ""


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
        # У Николая тип стоит отдельной строкой, значение под ним: «BLDG» / «2.5'S».
        return f"BLDG\n{value}"
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


#: Ширина знака шрифта ROMANS в долях высоты текста. По ``bbox`` подписей Николая:
#: медиана 0.705, у подписей оффсетов 0.70–0.75 (было 0.6 — блок считался на 17% уже).
TEXT_CHAR_WIDTH = 0.70


def default_text_width(text: str, height: float) -> float:
    """Грубая ширина строки, если программа не передала точную (шрифт romans, 0.70 высоты на знак)."""
    return TEXT_CHAR_WIDTH * float(height) * len(text)


def _grow_rect(rect: Rect, amount: float) -> Rect:
    """Прямоугольник с зазором по краям — подписи не должны сходиться вплотную."""
    return (rect[0] - amount, rect[1] - amount, rect[2] + amount, rect[3] + amount)


def _rect_at(center: Point, width: float, height: float) -> Rect:
    cx, cy = center
    return (cx - width / 2.0, cy - height / 2.0, cx + width / 2.0, cy + height / 2.0)


def _rects_overlap(a: Rect, b: Rect) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _rect_inside_polygon(rect: Rect, polygon: Sequence[Point]) -> bool:
    x0, y0, x1, y1 = rect
    return all(point_in_polygon(p, polygon) for p in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)))


def _rect_near_polygon(rect: Rect, polygon: Sequence[Point], tol_ft: float) -> bool:
    """Пересекает ли прямоугольник полигон или стоит не дальше ``tol_ft`` от его границы."""
    x0, y0, x1, y1 = rect
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    if _rect_inside_polygon(rect, polygon):
        return True
    if any(point_in_polygon(p, polygon) for p in corners):
        return True
    centre = ((x0 + x1) / 2.0, (y0 + y1) / 2.0)
    if point_in_polygon(centre, polygon):
        return True
    if tol_ft <= 0.0:
        return False
    segs = [(polygon[i], polygon[(i + 1) % len(polygon)]) for i in range(len(polygon))]
    if any(_segment_hits_rect(a, b, rect) for a, b in segs):
        return True
    return any(
        _point_segment_distance(p, seg) <= tol_ft for p in corners for seg in segs
    )


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


def _part_keys(text: str) -> set[tuple[float, str]]:
    """Части подписи как (значение, буква) — порядок строк не важен (R4, R12)."""
    out: set[tuple[float, str]] = set()
    for match in _PART_RE.finditer(str(text).replace("\n", " ")):
        out.add((float(match["value"]), match["dir"]))
    return out


def _same_label(text: str, other: str) -> bool:
    """Одна и та же подпись: те же части (значение и буква), порядок и метка не важны."""
    return bool(_part_keys(text)) and _part_keys(text) == _part_keys(other)


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
    #: подпись стоит в точке пересечения забора с продолжением стороны (не вершина объекта):
    #: у неё нет точки съёмки, и правила «только снятые вершины» (R13/R13b) к ней не относятся
    crossing: bool = False


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
    ok: Callable[[Rect, Point], bool], rotation_deg: float = 0.0,
) -> Point | None:
    """Текст без выноски — рядом с линией измерения, по перпендикуляру к ней."""
    dx, dy = target[0] - anchor[0], target[1] - anchor[1]
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return None
    px, py = -dy / length, dx / length
    # Направление замера: от стороны участка к объекту и дальше. Николай ставит подпись
    # за объектом, с внешней от границы стороны (FE.0.4'W — текст западнее забора),
    # поэтому это направление проверяется первым.
    ux, uy = dx / length, dy / length
    # Габарит всего блока: у двухстрочной подписи («0.2'E» и «1.2'S») высота вдвое больше,
    # иначе блок ложился на соседние надписи (4.24 соседней ветки).
    width, block_height = _text_block(text, height, width_of, rotation_deg)
    gap = TEXT_GAP_FACTOR * height
    mid = ((anchor[0] + target[0]) / 2.0, (anchor[1] + target[1]) / 2.0)
    half_extent = abs(px) * width / 2.0 + abs(py) * block_height / 2.0 + gap
    extent_along = abs(ux) * width / 2.0 + abs(uy) * block_height / 2.0 + gap
    for extra in (0.0, 0.5 * height, height, 2.0 * height, 3.5 * height, 5.0 * height):
        candidates = []
        # 1. За объектом, по направлению замера (внешняя от границы сторона).
        d_along = extent_along + extra
        candidates.append((anchor[0] + ux * d_along, anchor[1] + uy * d_along))
        # 2. Вбок от замера, симметрично.
        d = half_extent + extra
        candidates.append((mid[0] + px * d, mid[1] + py * d))
        candidates.append((mid[0] - px * d, mid[1] - py * d))
        # 3. Между объектом и границей — в последнюю очередь, места там почти нет.
        candidates.append((mid[0] - ux * extent_along * 0.5, mid[1] - uy * extent_along * 0.5))
        for center in candidates:
            rect = _rect_at(center, width, block_height)
            if ok(rect, center) and not _segment_hits_rect(anchor, target, rect):
                return center
    return None


def _place_house_text(
    polygon: Sequence[Point], anchor: Point, target: Point, text: str, height: float,
    width_of: Callable[[str, float], float], ok: Callable[[Rect, Point], bool],
    rotation_deg: float = 0.0,
) -> Point | None:
    """
    Текст дома внутри строения, вплотную к стене (как у Николая).

    Подпись дома у Николая стоит внутри здания у стены: снаружи мешают заборы и чужие
    надписи, а внутри — только крупное имя строения («1 STY FRAME», «FRAME GARAGE»).
    Поэтому точка идёт вдоль стены шагами (середина, затем к углам) и отодвигается от
    стены на 1–4 высоты, пока блок ничего не накрывает.
    """
    if len(polygon) < 3:
        return None
    dx, dy = anchor[0] - target[0], anchor[1] - target[1]
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return None
    inward = (dx / length, dy / length)
    # Стена, от которой меряется оффсет, — ближайший отрезок контура.
    best = math.inf
    wall: tuple[Point, Point] | None = None
    seq = list(polygon) + [polygon[0]]
    for a, b in zip(seq, seq[1:]):
        distance = _point_segment_distance(anchor, (a, b))
        if distance < best:
            best, wall = distance, (a, b)
    if wall is None:
        return None
    a, b = wall
    width, block_h = _text_block(text, height, width_of, rotation_deg)
    gap = TEXT_GAP_FACTOR * height
    for extra in (0.0, 0.5 * height, height, 2.0 * height, 3.0 * height):
        depth = block_h / 2.0 + gap + extra
        for share in (0.5, 0.4, 0.6, 0.3, 0.7, 0.2, 0.8, 0.1, 0.9):
            wall_point = (a[0] + (b[0] - a[0]) * share, a[1] + (b[1] - a[1]) * share)
            center = (wall_point[0] + inward[0] * depth, wall_point[1] + inward[1] * depth)
            if not point_in_polygon(center, polygon):
                continue
            rect = _rect_at(center, width, block_h)
            corners = (
                (rect[0], rect[1]), (rect[2], rect[1]), (rect[2], rect[3]), (rect[0], rect[3])
            )
            if not all(point_in_polygon(corner, polygon) for corner in corners):
                continue
            if ok(rect, center):
                return center
    return None


def _place_leader(
    anchor: Point, target: Point, text: str, height: float, width_of: Callable[[str, float], float],
    ok: Callable[[Rect, Point], bool], factor: float,
) -> tuple[Point, Point] | None:
    """Выноска от объекта к тексту: перпендикуляр к линии измерения первым, затем косые."""
    base = math.atan2(target[1] - anchor[1], target[0] - anchor[0])
    # Текст выноски у Николая горизонтален всегда — поворот к нему не применяется (R10).
    width, block_h = _text_block(text, height, width_of)
    gap = TEXT_GAP_FACTOR * height
    for extra in LEADER_DISTANCES_FT:
        for angle in LEADER_ANGLES_DEG:
            a = base + math.radians(angle)
            ux, uy = math.cos(a), math.sin(a)
            # Расстояние до центра блока — половина его габарита в направлении выноски плюс
            # зазор: у Николая конец выноски стоит у самого края текста, и длина выноски
            # выходит 2.7–3.5 ft для подписи высотой 0.9 (по трём чертежам, среднее 3.1).
            half_extent = abs(ux) * width / 2.0 + abs(uy) * block_h / 2.0
            d = half_extent + gap + extra * factor
            center = (anchor[0] + d * ux, anchor[1] + d * uy)
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
    survey_pts: Sequence[Point] = (),
    survey_notes: Sequence[tuple[Point, str]] = (),
) -> OffsetPlan:
    """
    Шаг 4, черновик 4.5: оффсеты объектов в выбранной области.

    ``property_pts`` — граница участка (замкнутый контур); ``front`` — линии ``M-ROW-LINE``
    (фронтовая граница, ``M-TAX-LINE`` сюда не передаётся); ``area`` — прямоугольник, который
    указал геодезист (``None`` — весь участок); ``texts`` — уже стоящие подписи ``(текст, точка,
    высота)``: рядом с тем же текстом оффсет не дублируется, а текст не накрывается;
    ``survey_pts`` — точки съёмки (R13: метка ставится у снятых вершин); ``survey_notes`` —
    ``(точка, DESC2)`` (R13b: у забора — только там, где описание называет забор).

    Для каждого объекта берётся ближайшая к границе вершина внутри участка; значение — до 0.1 ft.
    Дом — текст без выноски (высота 1.1), постройка (гараж, сарай, крыльцо) — текст на 0.9,
    забор и стена — выноска (0.9). Текст ставится
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
    # Подписи, уже поставленные этим планом у вершин: R14 не даёт дважды подписать один угол.
    known_in_plan: list[tuple[str, Point, float]] = []
    lines: list[tuple[Point, Point]] = []
    for i in range(len(ring)):
        lines.append((ring[i], ring[(i + 1) % len(ring)]))
    for line in front_lines:
        lines += list(zip(line, line[1:]))
    for feature in features:
        pts, closed = feature_contour(feature)
        if len(pts) >= 2:
            seq = pts + [pts[0]] if closed else pts
            lines += list(zip(seq, seq[1:]))

    # Границы, к которым меряются оффсеты: контур участка и фронт. По ним задаётся угол текста (R10).
    boundary_segs: list[tuple[Point, Point]] = list(zip(ring, ring[1:] + ring[:1]))
    for line in front_lines:
        boundary_segs += list(zip(line, line[1:]))
    placed: list[Rect] = []
    dup_radius = DUPLICATE_RADIUS_FT * factor

    plan.counts["zone"] = len(features)
    # R11–R13: замер по нормали своего края (стороны участка), вторая строка по другому краю,
    # метка только у снятых вершин. Список сторон строится один раз на весь план.
    edge_sides: list[SideLine] = side_lines(ring, front_lines, north_deg) if EDGE_NORMAL_OFFSETS else []
    survey_points = [(float(p[0]), float(p[1])) for p in survey_pts]
    survey_notes_list = [
        ((float(point[0]), float(point[1])), str(description))
        for point, description in survey_notes
    ]
    for index, feature in enumerate(features):
        label = feature.handle or f"#{index}"
        in_area = area_pts is None or any(point_in_polygon(p, area_pts) for p in feature.points)
        if not in_area and area_pts is not None and AREA_EDGE_TOL_FT > 0:
            # Оффсеты по определению стоят у самой границы участка, а часть объектов — снаружи
            # неё. При выделении полилинии участка (``area == ring``) забор в 0.3 ft снаружи
            # выпадал из области: на 192 Geldner оставалось 5 подписей вместо 14.
            in_area = any(
                _point_segment_distance(p, (area_pts[i], area_pts[(i + 1) % len(area_pts)]))
                <= AREA_EDGE_TOL_FT
                for p in feature.points
                for i in range(len(area_pts))
            )
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
        # R11–R14 включаются только когда в чертеже есть точки съёмки: без них нечем отличить
        # снятый угол от излома линии, и правило «метка у снятой вершины» (R13) не работает —
        # тогда остаётся прежний замер до ближайшей границы.
        if EDGE_NORMAL_OFFSETS and survey_points:
            rows_by_vertex = vertex_offset_rows(
                feature, edge_sides, max_ft=EDGE_MAX_FT, ring=ring, north_deg=north_deg,
                keep_online=ONLINE_LABELS,
            )
            if feature.kind == "fence" and FENCE_END_MAX_FT > EDGE_MAX_FT:
                # Конец длинного прогона забора Николай подписывает и в 5–6 ft от стороны
                # (192 Geldner: FE.5.2'S при 5.17 ft). Короткий выступ так не подписывается.
                ends = fence_end_vertices(feature, FENCE_END_MIN_LENGTH_FT)
                if ends:
                    wide = vertex_offset_rows(
                        feature, edge_sides, max_ft=FENCE_END_MAX_FT, ring=ring,
                        north_deg=north_deg,
                    )
                    for vertex, rows in wide.items():
                        if vertex in rows_by_vertex:
                            continue
                        if any(math.dist(vertex, end) <= SURVEY_POINT_SNAP_FT for end in ends):
                            if rows[0].value_ft > EDGE_MAX_FT:
                                rows_by_vertex[vertex] = rows
            all_rows = bool(rows_by_vertex)
            if LABEL_ONLY_SURVEYED_VERTICES and survey_points:
                rows_by_vertex = {
                    v: rows for v, rows in rows_by_vertex.items()
                    if vertex_on_survey_point(v, survey_points)
                }
            if SURVEY_DESC_RULE and survey_notes_list:
                # R13b: у забора и стены — только углы, где DESC2 называет этот вид объекта.
                rows_by_vertex = {
                    v: rows for v, rows in rows_by_vertex.items()
                    if vertex_describes_object(v, survey_notes_list, feature.kind)
                }
            crossings: dict[Point, list[EdgeRow]] = {}
            if CROSSING_LABELS and feature.kind == "fence":
                # Точки, где забор снаружи обходит угол участка: там Николай тоже ставит
                # подпись (192 Geldner FE.0.7'E, FE.1.4'E). Вершинами объекта они не являются,
                # поэтому правила R13 («только снятые вершины») и кэпы к ним не применяются.
                crossings = crossing_vertex_rows(
                    feature, edge_sides, ring=ring, max_ft=EDGE_MAX_FT, north_deg=north_deg,
                )
                for vertex, rows in crossings.items():
                    rows_by_vertex.setdefault(vertex, rows)
            if not rows_by_vertex:
                plan.skipped.append(
                    (label, "стоит на границе" if not all_rows else "нет снятых вершин у границы (нет точки съёмки)")
                )
                continue
            _plan_edge_labels(
                plan, feature, label, rows_by_vertex, ring=ring, area_pts=area_pts,
                scale_heights=scale, factor=factor, north_deg=north_deg,
                height=building_height_ft(feature, known, scale),
                boundary_segs=boundary_segs, lines=lines, known=known, placed=placed,
                width_of=width_of, dup_radius=dup_radius, text_rects=text_rects,
                known_in_plan=known_in_plan, exempt=frozenset(crossings),
                front_segs=tuple(
                    (tuple(line[0]), tuple(line[-1])) for line in front if len(line) >= 2
                ),
            )
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
        if not candidates and not (ONLINE_LABELS and feature.kind == "fence" and on_line):
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
        # Цели подписи: обычные кандидаты и (для забора на линии) вершина «FE.O/L».
        targets: list[tuple[OffsetCandidate, bool]] = [
            (best, False) for best in (candidates if CORNER_LABELS else candidates[:1])
        ]
        if ONLINE_LABELS and feature.kind == "fence" and on_line:
            targets.append((on_line[0], True))
        for best, is_online in targets:
            value = round_offset_ft(best.distance_ft)
            if not is_online and value <= 0.0:
                plan.skipped.append((label, "стоит на границе"))
                continue
            # Кандидат без параллельной стороны (ближайшая сторона) — один на объект, как раньше.
            if not is_online and best.segment < 0:
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
            text = ONLINE_FENCE_TEXT if is_online else offset_text(feature.kind, value, direction)
            extra: list[tuple[float, str]] = []
            if feature.kind in ("fence", "wall") and not is_online:
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
                    rotation_deg=(
                        _boundary_angle_deg(best.foot, boundary_segs) if TEXT_FOLLOWS_BOUNDARY else 0.0
                    ),
                )
            else:
                # Дом без места под текст получает выноску с «BLDG»; забор и стена — выноску как есть.
                leader_text = "\n".join(
                    [text] + [offset_text(feature.kind, v, d) for v, d in extra]
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


def _plan_edge_labels(
    plan: OffsetPlan,
    feature: Feature,
    label: str,
    rows_by_vertex: dict[Point, list[EdgeRow]],
    *,
    ring: Sequence[Point],
    area_pts: Sequence[Point] | None,
    scale_heights: float,
    factor: float,
    north_deg: float,
    boundary_segs: Sequence[tuple[Point, Point]],
    lines: Sequence[tuple[Point, Point]],
    known: list[tuple[str, Point, float]],
    placed: list[Rect],
    width_of: Callable[[str, float], float],
    dup_radius: float,
    text_rects: Sequence[Rect] = (),
    known_in_plan: list[tuple[str, Point, float]] | None = None,
    exempt: frozenset[Point] = frozenset(),
    height: float | None = None,
    front_segs: Sequence[tuple[Point, Point]] = (),
) -> None:
    """
    Подписи объекта по правилам R11–R13: каждая снятая вершина у границы подписывается по
    нормали своего края; если к вершине сходятся два края и второй ближе ``SECOND_EDGE_FT``,
    он идёт второй строкой той же подписи.

    Дом — текст без выноски (высота 1.1), постройка (гараж, сарай, крыльцо) — текст на 0.9,
    забор и стена — выноска (0.9).
    Порядок вершин — по возрастанию замера, поэтому первыми идут самые близкие к границе углы.
    """
    heights_kind = "house" if feature.kind == "house" else "fence"
    if height is None:
        height = offset_height_ft(heights_kind, scale_heights)
    if known_in_plan is None:
        known_in_plan = []
    bounds = _area_bounds(area_pts, margin=15.0 * factor)
    blockers = list(text_rects)

    # Зазор между подписями: сравниваются прямоугольники, расширенные на LABEL_CLEARANCE.
    # Шрифт в AutoCAD шире оценки «0.6 высоты на знак», и блоки, проверенные впритык,
    # в чертеже слипались (4.25 соседней ветки).
    clearance = LABEL_CLEARANCE * height

    def makes_ok(gap: float) -> Callable[[Rect, Point], bool]:
        def ok(rect: Rect, center: Point) -> bool:
            if area_pts is not None and not _rect_near_polygon(rect, area_pts, AREA_EDGE_TOL_FT):
                # Подпись у самой линии участка может чуть выходить за выделенную полилинию:
                # Николай ставит текст и снаружи границы. Допуск — AREA_EDGE_TOL_FT.
                return False
            if LABEL_MUST_BE_INSIDE and not point_in_polygon(center, ring):
                return False
            wide = _grow_rect(rect, gap)
            if any(
                _rects_overlap(wide, _grow_rect(other, gap))
                for other in blockers + placed
            ):
                return False
            return not any(
                _segment_hits_rect(a, b, rect)
                for a, b in lines
                if bounds is None or _segment_in_bounds(a, b, bounds)
            )

        return ok

    ok_strict = makes_ok(clearance)
    # Второй заход без зазора: у Николая подписи местами стоят в 0.7–0.8 высоты друг от
    # друга, и со строгим зазором такие места считались занятыми (Geldner: FE.0.4'N).
    ok = makes_ok(0.0) if LABEL_RELAXED_RETRY else ok_strict

    cells = sorted(rows_by_vertex.items(), key=lambda cell: cell[1][0].value_ft)
    order = list(cells)
    if MAX_SAME_VALUE_PER_FEATURE > 0:
        # R14: повтор одного и того же замера у объекта — не больше двух раз.
        seen_values: dict[tuple[float, str], int] = {}
        unique: list[tuple[Point, list[EdgeRow]]] = []
        for vertex, rows in order:
            if vertex in exempt:
                unique.append((vertex, list(rows)))
                continue
            pairs = []
            for row in rows:
                value = round_offset_ft(row.value_ft)
                if value <= 0.0:
                    continue
                key = (value, row.direction)
                if seen_values.get(key, 0) >= MAX_SAME_VALUE_PER_FEATURE:
                    continue
                seen_values[key] = seen_values.get(key, 0) + 1
                pairs.append(row)
            if pairs:
                unique.append((vertex, pairs))
        order = unique
    cap = MAX_CORNERS_PER_SIDE_HOUSE if feature.kind == "house" else MAX_CORNERS_PER_SIDE
    if cap <= 0 and exempt:
        order = [
            (vertex, rows) for vertex, rows in order
        ]
    if cap > 0:
        # R13: у Николая на одну сторону участка приходится не больше двух углов объекта —
        # берём ближайшие к стороне, остальные не подписываем. Сторона — по букве: у участка
        # бывает несколько отрезков с одной и той же нормалью (запад в двух частях).
        # R13c: углы, разнесённые вдоль стороны дальше SIDE_CAP_SPACING_FT, считаются отдельно:
        # у длинной стороны забора Николай подписывает оба конца.
        per_side: dict[str, list[tuple[Point, float]]] = {}
        capped: list[tuple[Point, list[EdgeRow]]] = []
        for vertex, rows in order:
            if vertex in exempt:
                # Углы, обойдённые забором снаружи, Николай подписывает все: на 192 Geldner
                # на одной восточной стороне стоят FE.0.7'E и FE.1.4'E в 0.7 ft друг от друга.
                capped.append((vertex, rows))
                continue
            key = rows[0].direction
            taken = per_side.setdefault(key, [])
            if len(taken) >= cap and not (
                SIDE_CAP_SPACING_FT > 0
                and all(
                    math.hypot(vertex[0] - other[0], vertex[1] - other[1]) > SIDE_CAP_SPACING_FT
                    for other, _value in taken
                )
            ) and not (
                # Перекос: у стены, идущей вдоль границы не параллельно, Николай подписывает
                # ОБА конца — разными числами (0.3'W и 0.4'W у гаража). Если замер тот же,
                # второй конец не дублируется: «оба угла = 0.8'N — второй не пишут».
                SKEW_PAIR_FT > 0
                and all(
                    abs(round_offset_ft(rows[0].value_ft) - value)
                    >= SKEW_PAIR_FT
                    for _point, value in taken
                )
            ):
                continue
            taken.append((vertex, round_offset_ft(rows[0].value_ft)))
            capped.append((vertex, rows))
        order = capped
    first_item = len(plan.items)
    fresh_rects: list[Rect] = []
    for vertex, rows in order:
        primary = rows[0]
        value = round_offset_ft(primary.value_ft)
        if value <= 0.0:
            continue
        extras = [
            (round_offset_ft(row.value_ft), row.direction)
            for row in rows[1:]
            if round_offset_ft(row.value_ft) > 0.0
            and (round_offset_ft(row.value_ft), row.direction) != (value, primary.direction)
        ]
        # У Николая в двухстрочной подписи первой идёт сторона N/S, второй E/W
        # («FE.0.4'N» / «FE.1.3'W», «FE.0.1'S» / «FE.0.5'W»): 4 из 5 случаев.
        shown_value, shown_direction = value, primary.direction
        if extras and primary.direction in ("E", "W") and extras[0][1] in ("N", "S"):
            shown_value, shown_direction = extras[0]
            extras = [(value, primary.direction)]
        text = offset_text(feature.kind, shown_value, shown_direction)
        value, direction_shown = shown_value, shown_direction
        mid = ((vertex[0] + primary.foot[0]) / 2.0, (vertex[1] + primary.foot[1]) / 2.0)
        # R14: один угол чертежа — одна подпись. Стык двух объектов в углу участка даёт две
        # вершины в 0.2–0.7 ft друг от друга (Brooklyn: «0.1'S \P 0.5'W» и «0.5'W \P 0.2'S»),
        # и у Николая там одна подпись. Тот же текст у той же вершины — тоже дубль.
        same_text_near = any(
            (_key(old) == _key(text) or _same_label(old, text))
            and min(
                math.hypot(p[0] - vertex[0], p[1] - vertex[1]),
                math.hypot(p[0] - mid[0], p[1] - mid[1]),
            ) <= dup_radius
            for old, p, _h in known
        )
        same_corner = NODE_DUP_FT > 0 and vertex not in exempt and any(
            math.hypot(p[0] - vertex[0], p[1] - vertex[1]) <= NODE_DUP_FT
            for _t, p, _h in known_in_plan
        )
        if same_text_near or same_corner:
            plan.skipped.append((label, f"«{text}» уже стоит рядом"))
            continue

        item: OffsetItem | None = None
        beside = None
        # «FE.» повторяется в каждой строке, а имя типа («C.L.F.», «W.FE.», «BLDG») — только
        # в первой: так у Николая («FE.0.4'N» / «FE.1.3'W», но «BLDG» / «2.5'S»).
        continuation = offset_prefix(feature.kind)
        label_text = "\n".join(
            [text] + [f"{continuation}{v:.1f}'{d}" for v, d in extras]
        )
        # R10: подпись без выноски идёт вдоль своей линии (у Николая 0° и 90°); с выноской —
        # всегда горизонтально, поэтому поворот считается только для текста.
        rotation = primary.side_angle_deg if TEXT_FOLLOWS_BOUNDARY else 0.0
        if feature.kind == "house" and HOUSE_TEXT_INSIDE:
            # Дом — текст внутри строения у стены; снаружи мешают заборы и чужие надписи.
            beside = _place_house_text(
                feature_contour(feature)[0], vertex, primary.foot, label_text, height,
                width_of, ok_strict, rotation,
            ) or _place_house_text(
                feature_contour(feature)[0], vertex, primary.foot, label_text, height,
                width_of, ok, rotation,
            )
        if beside is None and (
            feature.kind == "house" or (
                FENCE_TEXT_WITHOUT_LEADER and feature.kind in ("fence", "wall")
            )
        ):
            # У Николая подпись забора — обычный текст у самой линии, без стрелки.
            # Выноска остаётся последним средством: она и давала «частокол» на чертеже.
            beside = _place_beside_line(
                vertex, primary.foot, label_text, height, width_of, ok_strict, rotation
            ) or _place_beside_line(
                vertex, primary.foot, label_text, height, width_of, ok, rotation
            )
        if beside is not None:
            item = OffsetItem(
                kind=feature.kind, value_ft=value, direction=direction_shown, style="text",
                height_ft=height, anchor=vertex, target=primary.foot, text=label_text,
                point=beside, leader=None,
                rotation_deg=rotation,
                crossing=vertex in exempt,
            )
            # Габарит повёрнутой подписи — по описанному прямоугольнику (R10).
            rect_w, rect_h = _text_block(label_text, height, width_of, rotation)
            rect = _rect_at(beside, rect_w, rect_h)
        else:
            leader_text = label_text
            spot = _place_leader(
                vertex, primary.foot, leader_text, height, width_of, ok_strict, factor
            ) or _place_leader(
                vertex, primary.foot, leader_text, height, width_of, ok, factor
            )
            if spot is None:
                plan.warnings.append(
                    f"{label}: нет свободного места для «{leader_text}» в области — подпись не поставлена"
                )
                continue
            center, end = spot
            rect_w, rect_h = _text_block(leader_text, height, width_of)
            item = OffsetItem(
                kind=feature.kind, value_ft=value, direction=direction_shown, style="leader",
                height_ft=height, anchor=vertex, target=primary.foot, text=leader_text,
                point=center, leader=(vertex, end), crossing=vertex in exempt,
            )
            rect = _rect_at(center, rect_w, rect_h)
        placed.append(rect)
        fresh_rects.append(rect)
        plan.items.append(item)
        known.append((item.text, item.point, item.height_ft))
        known_in_plan.append((item.text, vertex, item.height_ft))
    if ONLINE_LABELS:
        # Объект стоит на самой линии участка: смещения нет, и числовая подпись не появляется.
        # Николай пишет в таком месте «FE.O/L» у забора, «BLDG» / «O/L» у дома (378 Mother
        # Gaston, дважды) и «WALL» / «O/L» у стены (Brooklyn). «На линии» — ближайшая сторона
        # проходит по самой вершине (ближе MIN_OFFSET_FT), а не «маленькое смещение» вроде 0.1'.
        online: dict[Point, list[EdgeRow]] = {
            vertex: rows
            for vertex, rows in cells
            if rows
            and min(row.value_ft for row in rows) <= MIN_OFFSET_FT
            and _vertex_on_ring(vertex, ring, ONLINE_TOL_FT)
        }
        if feature.kind != "fence" and front_segs:
            # У дома и стены «O/L» Николай пишет только на фронтовой линии (улице): в 378
            # Mother Gaston — в углу на `M-ROW-LINE`, а на боковых линиях участка — число.
            online = {
                vertex: rows
                for vertex, rows in online.items()
                if _vertex_on_segments(vertex, front_segs, ONLINE_TOL_FT)
            }
        if online:
            # Одна подпись на объект: у Николая на четыре рядом стоящих строения в углу
            # участка приходится две подписи, а не четыре.
            vertex = min(online, key=lambda point: cells.index((point, online[point])))
            o_l_text = online_text(feature.kind)
            same_near = any(
                _key(old) == _key(o_l_text) and math.dist(p, vertex) <= dup_radius
                for old, p, _h in known
            )
            spot = None
            if not same_near:
                spot = _place_leader(
                    vertex, vertex, o_l_text, height, width_of, ok_strict, factor
                ) or _place_leader(
                    vertex, vertex, o_l_text, height, width_of, ok, factor
                )
            if spot is None:
                if not same_near:
                    plan.skipped.append((label, f"«{o_l_text}»: нет места"))
            else:
                center, foot = spot
                plan.items.append(
                    OffsetItem(
                        kind=feature.kind, value_ft=0.0, direction="", style="leader",
                        height_ft=height, anchor=vertex, target=vertex, text=o_l_text,
                        point=center, leader=(vertex, foot), rotation_deg=0.0,
                    )
                )
                rect = _rect_at(center, *_text_block(o_l_text, height, width_of))
                placed.append(rect)
                fresh_rects.append(rect)
                known.append((o_l_text, center, height))
                known_in_plan.append((o_l_text, vertex, height))
            # Числовые подписи у вершин на линии не ставим: там у Николая только «O/L»
            # (строка с нулевым замером отсекается основным циклом по value <= 0).

    if POST_PART_DEDUPE and PART_DUPLICATE_FT > 0:
        # H32: одиночная подпись не повторяет часть полной подписи того же вида объекта,
        # стоящей рядом (2 ft). Проверка идёт по уже разложенным подписям: двухстрочная
        # может встать позже одиночной, и тогда одиночная — дубль.
        fresh = plan.items[first_item:]
        full = [
            (item, _part_keys(item.text))
            for item in fresh
            if len(item.text.split("\n")) > 1
        ]
        if full:
            kept: list[OffsetItem] = []
            for item in fresh:
                own = _part_keys(item.text)
                if len(item.text.split("\n")) == 1 and any(
                    math.dist(item.anchor, other.anchor) <= PART_DUPLICATE_FT
                    and own <= parts
                    for other, parts in full
                ):
                    plan.skipped.append(
                        (label, f"«{item.text}» уже в подписи рядом")
                    )

                    continue
                kept.append(item)
            removed = [item for item in fresh if not any(item is k for k in kept)]
            if removed:
                # Снятая подпись не должна занимать место: иначе её прямоугольник остаётся
                # в `placed` и глушит подписи следующих объектов (Geldner: FE.0.4'N и
                # FE.1.1'E не находили места из-за снятых дублей).
                for offset_index, entry in enumerate(fresh):
                    if not any(entry is k for k in kept):
                        stale = fresh_rects[offset_index]
                        for position, other in enumerate(placed):
                            if other == stale:
                                del placed[position]
                                break
                del plan.items[first_item:]
                plan.items.extend(kept)
                for item in removed:
                    for index, (text, point, _h) in enumerate(known):
                        if text == item.text and math.dist(point, item.point) < 1e-9:
                            del known[index]
                            break
                    for index, (text, anchor, _h) in enumerate(known_in_plan):
                        if text == item.text and math.dist(anchor, item.anchor) < 1e-9:
                            del known_in_plan[index]
                            break


@dataclass(frozen=True)
class EdgeRow:
    """Строка оффсета у вершины: замер по нормали своего края объекта (R11)."""

    value_ft: float  # расстояние до стороны участка, футы (не округлённое)
    direction: str  # буква: сторона, где стоит объект относительно стороны участка
    foot: Point  # основание перпендикуляра на стороне участка
    edge_index: int  # 0 — «своя» сторона (нормаль края), 1 — ближайшая из остальных
    side_angle_deg: float  # угол стороны участка, градусы
    parallel: bool = True  # нормаль стороны параллельна нормали края вершины (R11)


@dataclass(frozen=True)
class SideLine:
    """Сторона участка (контур или фронтовая линия) с внешней нормалью и буквой."""

    a: Point
    b: Point
    normal: Point  # внешняя нормаль (наружу участка)
    letter: str  # буква стороны по компасу
    length_ft: float
    from_front: bool = False


def side_lines(
    ring: Sequence[Point],
    front: Sequence[Sequence[Point]] = (),
    north_deg: float = 90.0,
) -> list[SideLine]:
    """
    Стороны участка для меры оффсета: контур + фронтовые линии ``M-ROW-LINE``.
    У каждой — внешняя нормаль и буква стороны (север из стрелки чертежа).
    """
    pts = [(float(x), float(y)) for x, y in ring]
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
    out: list[SideLine] = []

    def add(a: Point, b: Point, front_flag: bool) -> None:
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        if length < 1e-9:
            return
        nx, ny = -dy / length, dx / length
        mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
        if len(pts) >= 3 and point_in_polygon((mid[0] + nx * 0.2, mid[1] + ny * 0.2), pts):
            nx, ny = -nx, -ny  # нормаль должна смотреть наружу участка
        letter = compass_letter(nx, ny, north_deg)
        if DIAGONAL_SHEET_AXES:
            # Диагональ: буква по осям листа, а не по ближайшей оси к северу. Николай на
            # 24 Alvine пишет у наклонной стороны 1.3'W и 1.4'E, хотя при севере 67.6°
            # ближайшая к нормали ось — N.
            angle = math.degrees(math.atan2(ny, nx)) % 90.0
            nearest = round(angle / 90.0) * 90.0
            if nearest > 45.0:
                nearest = 0.0
            if abs(angle - nearest) > DIAGONAL_TOL_DEG:
                letter = compass_letter(nx, ny, 90.0)
        out.append(
            SideLine(
                a=(float(a[0]), float(a[1])),
                b=(float(b[0]), float(b[1])),
                normal=(nx, ny),
                letter=letter,
                length_ft=length,
                from_front=front_flag,
            )
        )

    for i in range(len(pts)):
        add(pts[i], pts[(i + 1) % len(pts)], False)
    for line in front:
        seq = [(float(p[0]), float(p[1])) for p in line]
        for a, b in zip(seq, seq[1:]):
            # Фронтовая линия часто продублирована контуром участка: одна и та же прямая
            # с противоположными нормалями дала бы вторую букву у той же вершины.
            if any(
                _same_line(a, b, side)
                or _same_line(side.a, side.b, SideLine(a=a, b=b, normal=(0.0, 0.0), letter="?", length_ft=0.0))
                for side in out
            ):
                continue
            add(a, b, True)
    return out


def _same_line(a: Point, b: Point, side: "SideLine", tol_ft: float = 0.05) -> bool:
    """Лежат ли две стороны на одной прямой (для отбрасывания дублей контур/фронт)."""
    return (
        _point_segment_distance(a, (side.a, side.b)) <= tol_ft
        and _point_segment_distance(b, (side.a, side.b)) <= tol_ft
    )


def edge_rows(
    vertex: Point,
    edge_dirs: Sequence[Point],
    sides: Sequence[SideLine],
    *,
    max_ft: float = 6.0,
    parallel_tol_deg: float | None = None,
    extension_ft: float | None = None,
) -> list[EdgeRow]:
    """
    Замеры вершины по нормалям её краёв (R11): для каждого края берётся ближайшая сторона
    участка, нормаль которой параллельна нормали края («своя» сторона). Строки отсортированы
    по расстоянию; ``parallel`` у них всегда True.
    """
    return [
        row
        for row in side_rows(
            vertex, sides, edge_dirs=edge_dirs, max_ft=max_ft,
            parallel_tol_deg=parallel_tol_deg, extension_ft=extension_ft,
        )
        if row.parallel
    ]


def side_rows(
    vertex: Point,
    sides: Sequence[SideLine],
    *,
    edge_dirs: Sequence[Point] = (),
    max_ft: float = 6.0,
    parallel_tol_deg: float | None = None,
    extension_ft: float | None = None,
    north_deg: float = 90.0,
    allow_on_line: bool = False,
) -> list[EdgeRow]:
    """
    Все замеры вершины до сторон участка в пределах ``max_ft`` (R11): у каждой строки — значение,
    буква стороны, основание перпендикуляра и признак «своя сторона» (нормаль стороны параллельна
    нормали одного из краёв вершины). ``edge_dirs`` — единичные направления краёв в этой вершине.

    Своя сторона идёт первой при равном расстоянии: заказчик меряет оффсет по нормали своего края,
    а не по ближайшей точке границы. Если своей стороны в пределах ``max_ft`` нет — меряем до
    ближайшей стороны (забор в середине участка, короткий отрезок у чужой линии).
    """
    if parallel_tol_deg is None:
        parallel_tol_deg = EDGE_PARALLEL_TOL_DEG
    tol = math.cos(math.radians(parallel_tol_deg))
    if extension_ft is None:
        extension_ft = LINE_EXTENSION_FT
    # Вершина на самой линии участка не даёт оффсета (R13: «на линии» не подписывается).
    # Точка пересечения забора с продолжением стороны лежит на линии по определению —
    # для неё замер до этой стороны пропускается, а не отменяет все строки.
    if allow_on_line:
        def _on_line(side: SideLine) -> bool:
            return (
                abs((vertex[0] - side.a[0]) * side.normal[0] + (vertex[1] - side.a[1]) * side.normal[1])
                < MIN_OFFSET_FT
            )
    elif any(
        abs((vertex[0] - side.a[0]) * side.normal[0] + (vertex[1] - side.a[1]) * side.normal[1])
        < MIN_OFFSET_FT
        for side in sides
    ):
        return []
    normals = [(-uy, ux) for ux, uy in edge_dirs]
    rows: list[EdgeRow] = []
    seen: set[tuple[float, float]] = set()
    for side in sides:
        vx, vy = vertex[0] - side.a[0], vertex[1] - side.a[1]
        signed = vx * side.normal[0] + vy * side.normal[1]
        if allow_on_line and abs(signed) < MIN_OFFSET_FT:
            continue
        parallel = any(abs(nx * side.normal[0] + ny * side.normal[1]) >= tol for nx, ny in normals)
        cap_ft = max_ft if parallel else max(max_ft, EDGE_FALLBACK_FT)
        if abs(signed) > cap_ft:
            continue
        along = vx * (side.b[0] - side.a[0]) + vy * (side.b[1] - side.a[1])
        along /= max(side.length_ft, 1e-9)
        beyond = max(0.0, -along, along - side.length_ft)
        if beyond > extension_ft:
            continue
        foot = (vertex[0] - signed * side.normal[0], vertex[1] - signed * side.normal[1])
        key = (round(foot[0], 3), round(foot[1], 3))
        if key in seen:
            continue
        seen.add(key)
        # Буква стороны, где стоит объект: снаружи — буква самой стороны, внутри — противоположная.
        # Забор в 0.5 ft западнее западной линии участка — «0.5'W» (Brooklyn), тот же забор в 0.5 ft
        # внутри неё — «0.5'E»: подпись называет сторону, на которой объект лежит относительно линии.
        letter = side.letter if signed > 0 else _OPPOSITE[side.letter]
        rows.append(
            EdgeRow(
                value_ft=abs(signed),
                direction=letter,
                foot=foot,
                edge_index=0 if parallel else 1,
                side_angle_deg=math.degrees(
                    math.atan2(side.b[1] - side.a[1], side.b[0] - side.a[0])
                ) % 180.0,
                parallel=parallel,
            )
        )
    rows.sort(key=lambda row: (row.edge_index, row.value_ft))
    return rows


def feature_contour(feature: Feature) -> tuple[list[Point], bool]:
    """
    Вершины объекта и признак замкнутости.

    Повторяющаяся первая вершина в конце снимается: иначе в ней находился бы один край
    вместо двух и замер по нормали края (R11) терял свою сторону.
    """
    pts = [(float(x), float(y)) for x, y in feature.points]
    closed = feature.closed
    if len(pts) >= 2 and math.dist(pts[0], pts[-1]) <= 1e-6:
        pts = pts[:-1]
        if closed is None:
            closed = True
    if closed is None:
        closed = feature.kind == "house" and len(pts) >= 3
    return pts, bool(closed)


def feature_edge_directions(feature: Feature) -> dict[Point, list[tuple[float, float]]]:
    """
    Для каждой вершины объекта — единичные направления краёв, которые в ней сходятся.
    Замкнутый контур дома замыкается, ломаная забора — нет.
    """
    pts, closed = feature_contour(feature)
    out: dict[Point, list[tuple[float, float]]] = {}
    if len(pts) < 2:
        return out
    seq = pts + [pts[0]] if closed else pts
    for a, b in zip(seq, seq[1:]):
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        if length < 1e-9:
            continue
        ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
        out.setdefault(a, []).append((ux, uy))
        out.setdefault(b, []).append((ux, uy))
    return out


def _on_line_row(
    vertex: Point, sides: Sequence[SideLine], edge_dirs: Sequence[Point],
) -> EdgeRow | None:
    """Строка «вершина на стороне участка»: замер 0, буква — сторона, на которой она лежит."""
    if not sides:
        return None
    tol = math.cos(math.radians(EDGE_PARALLEL_TOL_DEG))
    normals = [(-uy, ux) for ux, uy in edge_dirs]
    best: tuple[int, float, SideLine] | None = None
    for side in sides:
        vx, vy = vertex[0] - side.a[0], vertex[1] - side.a[1]
        signed = vx * side.normal[0] + vy * side.normal[1]
        if abs(signed) >= MIN_OFFSET_FT:
            continue
        own = any(abs(nx * side.normal[0] + ny * side.normal[1]) >= tol for nx, ny in normals)
        key = (0 if own else 1, abs(signed), side)
        if best is None or key[:2] < best[:2]:
            best = key
    if best is None:
        return None
    side = best[2]
    return EdgeRow(
        value_ft=0.0, direction=side.letter, foot=vertex, edge_index=0,
        side_angle_deg=math.degrees(
            math.atan2(side.b[1] - side.a[1], side.b[0] - side.a[0])
        ),
        parallel=bool(best[0] == 0),
    )


def vertex_offset_rows(
    feature: Feature,
    sides: Sequence[SideLine],
    *,
    max_ft: float = 6.0,
    second_ft: float | None = None,
    parallel_tol_deg: float | None = None,
    ring: Sequence[Point] = (),
    north_deg: float = 90.0,
    keep_online: bool = False,
) -> dict[Point, list[EdgeRow]]:
    """
    Строки оффсета по каждой вершине объекта (R11, R12): основной замер и вторая строка
    по другому краю, если он ближе ``second_ft``.

    Вершины на линии участка (замер меньше ``MIN_OFFSET_FT``) по умолчанию остаются без строки.
    ``keep_online`` (``ONLINE_LABELS``) оставляет их: такие вершины подписываются не числом,
    а ``FE.O/L`` / ``BLDG`` / ``O/L``.
    """
    if second_ft is None:
        second_ft = SECOND_EDGE_FT
    out: dict[Point, list[EdgeRow]] = {}
    for vertex, dirs in feature_edge_directions(feature).items():
        rows = side_rows(
            vertex, sides, edge_dirs=dirs, max_ft=max_ft,
            parallel_tol_deg=parallel_tol_deg, north_deg=north_deg,
        )
        if keep_online and not rows:
            # Вершина лежит на стороне участка: ``side_rows`` такие не возвращает вовсе.
            # Оставляем строку с нулём — по ней ``_plan_edge_labels`` поставит «O/L».
            row = _on_line_row(vertex, sides, dirs)
            if row is not None:
                out[vertex] = [row]
            continue
        rows = [row for row in rows if row.value_ft >= MIN_OFFSET_FT]
        if not rows:
            continue
        primary = rows[0]
        kept = [primary]
        taken = (round_offset_ft(primary.value_ft), primary.direction)
        for row in rows[1:]:
            if row.edge_index != 0 and row.parallel is False and len(kept) == 1 and primary.parallel is False:
                # Своей стороны у вершины нет: основной замер — ближайшая сторона участка, и
                # вторая строка тогда тоже допустима (вершина в глубине участка).
                pass
            if row.value_ft > second_ft:
                continue
            pair = (round_offset_ft(row.value_ft), row.direction)
            if pair == taken or row.direction == primary.direction:
                # Вторая строка — по другому краю вершины, то есть по другой стороне участка:
                # у Николая вторая строка всегда с другой буквой (0.1'E + 1.2'S).
                continue
            kept.append(row)
        out[vertex] = kept
    if NODE_DUP_FT > 0 and len(out) > 1:
        # Две вершины рядом и с одинаковым округлённым замером — один и тот же оффсет.
        # Из пары оставляем вершину внутри участка (заказчик подписывает снятый угол в участке).
        def _inside(point: Point) -> bool:
            return bool(ring) and point_in_polygon(point, ring)

        merged: list[tuple[Point, list[EdgeRow]]] = []
        for vertex, rows in sorted(out.items(), key=lambda cell: cell[1][0].value_ft):
            twin = None
            for index, (other, other_rows) in enumerate(merged):
                if math.dist(vertex, other) > NODE_DUP_FT:
                    continue
                if NODE_DUP_ANY_VALUE or (
                    round_offset_ft(other_rows[0].value_ft) == round_offset_ft(rows[0].value_ft)
                ):
                    twin = index
                    break
            if twin is None:
                merged.append((vertex, rows))
            elif _inside(vertex) and not _inside(merged[twin][0]):
                merged[twin] = (vertex, rows)
        out = dict(merged)
    return out


def crossing_vertex_rows(
    feature: Feature,
    sides: Sequence[SideLine],
    *,
    ring: Sequence[Point],
    max_ft: float = EDGE_MAX_FT,
    parallel_tol_deg: float | None = None,
    extension_ft: float | None = None,
    north_deg: float = 90.0,
) -> dict[Point, list[EdgeRow]]:
    """
    Подписи в точках, где забор снаружи обходит угол участка (H27).

    Отрезок забора, у которого **оба конца вне участка**, пересекает продолжение стороны
    участка — Николай ставит там подпись с замером до параллельной стороны: на 192 Geldner
    забор в 0.7 ft восточнее восточной границы пересекает линию южной и получает ``FE.0.7'E``.
    Если один конец внутри участка, это не обход угла, а выход забора за границу — такие
    концы подписываются по своим вершинам (R11–R13).
    """
    if extension_ft is None:
        extension_ft = LINE_EXTENSION_FT
    pts, closed = feature_contour(feature)
    if len(pts) < 2 or len(ring) < 3:
        return {}
    seq = pts + [pts[0]] if closed else pts
    out: dict[Point, list[EdgeRow]] = {}
    for a, b in zip(seq, seq[1:]):
        if point_in_polygon(a, ring) or point_in_polygon(b, ring):
            continue
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        if length < 1e-9:
            continue
        direction = (dx / length, dy / length)
        for side in sides:
            # Пересечение отрезка забора с прямой стороны (с продолжением за угол).
            ex, ey = side.b[0] - side.a[0], side.b[1] - side.a[1]
            denom = dx * ey - dy * ex
            if abs(denom) < 1e-12:
                continue
            t = ((side.a[0] - a[0]) * ey - (side.a[1] - a[1]) * ex) / denom
            u = ((side.a[0] - a[0]) * dy - (side.a[1] - a[1]) * dx) / denom
            if not (0.0 < t < 1.0):
                continue
            if not (-1e-9 <= u <= 1.0 + 1e-9):
                # Точка пересечения за концом стороны — только в пределах продолжения.
                beyond_ft = -u * side.length_ft if u < 0 else (u - 1.0) * side.length_ft
                if beyond_ft > extension_ft:
                    continue
            point = (a[0] + t * dx, a[1] + t * dy)
            if any(math.dist(point, other) <= NODE_DUP_FT for other in out):
                continue
            rows = side_rows(
                point, sides, edge_dirs=(direction,), max_ft=max_ft,
                parallel_tol_deg=parallel_tol_deg, extension_ft=extension_ft,
                north_deg=north_deg, allow_on_line=True,
            )
            rows = [row for row in rows if row.value_ft >= MIN_OFFSET_FT]
            if rows:
                out[point] = rows
    return out


def fence_end_vertices(feature: Feature, min_run_ft: float = FENCE_END_MIN_LENGTH_FT) -> list[Point]:
    """
    Концы прогонов забора, у которых соседний отрезок не короче ``min_run_ft``.

    Конец длинного забора Николай подписывает даже в 5–6 ft от стороны участка
    (192 Geldner ``FE.5.2'S``); такой же вылет у короткого 6-футового выступа он
    не подписал (Brooklyn). Замкнутый контур концов не имеет.
    """
    pts, closed = feature_contour(feature)
    if len(pts) < 2 or closed:
        return []
    out: list[Point] = []
    first, last = pts[0], pts[-1]
    if math.dist(first, pts[1]) >= min_run_ft:
        out.append(first)
    if math.dist(last, pts[-2]) >= min_run_ft:
        out.append(last)
    return out


def vertex_on_survey_point(vertex: Point, points: Sequence[Point], snap_ft: float = SURVEY_POINT_SNAP_FT) -> bool:
    """Стоит ли в вершине точка съёмки (R13)."""
    return any(math.dist(vertex, p) <= snap_ft for p in points)


def vertex_describes_object(
    vertex: Point,
    notes: Sequence[tuple[Point, str]],
    kind: str,
    snap_ft: float = SURVEY_DESC_TOL_FT,
) -> bool:
    """
    Описывает ли точка съёмки у этой вершины данный объект (R13b).

    ``notes`` — ``(точка, DESC2)``. У забора и стены описание должно называть этот вид объекта
    (``VINYL FENCE``, ``WOOD FENCE``, ``F E C``, ``WALL``); у дома и сарая подходит любое описание
    строения (``1 STY FRAME``, ``BRICK``, ``SHED``) или его отсутствие — эталон Николая
    подписывает углы домов, где в DESC2 стоят и размеры, и материалы.
    """
    words = FENCE_DESC_WORDS if kind == "fence" else WALL_DESC_WORDS if kind == "wall" else ()
    for point, description in notes:
        if math.dist(vertex, point) > snap_ft:
            continue
        if not words:
            return True
        text = str(description or "").upper()
        if any(word in text for word in words):
            return True
    return False


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


def _text_block(
    text: str, height: float, width_of: Callable[[str, float], float], rotation_deg: float = 0.0,
) -> tuple[float, float]:
    """
    Размер многострочной подписи (строки разделены ``\\n``): ширина самой длинной и общая высота.

    ``rotation_deg`` — поворот текста вдоль своей линии (R10): возвращается габарит **повёрнутого**
    блока (описанный прямоугольник). Без этого вертикальная подпись проверялась как горизонтальная
    и накрывала соседние тексты (так было у соседа на 192 Geldner: повёрнутая подпись легла на «C.L.F.»).
    """
    lines = text.split("\n")
    width = max(width_of(line, height) for line in lines)
    # Шаг строк как у MText: с шагом 1.0 блок из двух строк считался ниже, чем он есть.
    block = height * (1.0 + TEXT_LINE_SPACING * (len(lines) - 1))
    if not rotation_deg:
        return width, block
    a = math.radians(rotation_deg)
    ca, sa = abs(math.cos(a)), abs(math.sin(a))
    return width * ca + block * sa, width * sa + block * ca


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
