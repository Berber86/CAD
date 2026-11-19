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
from dataclasses import dataclass, field, replace
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
#: Допуск области выделения, футы: объект и подпись считаются попавшими в область, если они ближе
#: этого к её границе. Оффсеты по определению стоят у границы участка, поэтому при выделении самой
#: полилинии участка (``area == ring``) забор в 0.3 ft снаружи выпадал из области, а подпись у самой
#: линии не проходила проверку «целиком внутри» (на 192 Geldner оставалось 5 подписей вместо 14).
AREA_EDGE_TOL_FT = 6.0
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
#: Забор, у которого вершина стоит на линии участка (до MIN_OFFSET_FT), подписывается «FE.O/L» — «на линии».
#: За: 192 Geldner, забор слева посередине (вершина в 0.001 ft от границы) — у Николая «FE.O/L».
#: Против: на Brooklyn (4.10) такая вершина не подписана «O/L», забор подписан по другим вершинам.
#: Противоречивые эталоны — выключено по умолчанию, как CORNER_LABELS и WALL_OFFSETS.
ONLINE_LABELS = True
ONLINE_FENCE_TEXT = "FE.O/L"
#: Текст без выноски поворачивается вдоль ближайшей границы (R10). Выключено: на эталонах угол следует за
#: ориентацией участка, а не за стороной. Ilion: «FE.0.1'E» — 90.4°, «FE.1.3'E» — 0.4° (участок 0.4°).
#: Aspinwall: «WALL 0.1'E» — 284.6° (участок 14.6°). Правило «вдоль границы» не подтверждено.
TEXT_FOLLOWS_BOUNDARY = False
#: Шаг строк многострочной подписи в высотах текста (как mtext_line_spacing_factor в acad_builder).
TEXT_LINE_SPACING = 1.67
#: Подпись забора ставится текстом без выноски, как у Николая. В 192 Geldner все его подписи по заборам —
#: обычный текст у линии (`FE.1.7'S`, `FE.0.7'E`), стрелок нет ни одной; наши выноски на этом чертеже
#: накрывают подписи типа забора (`C.L.F.`, `W.FE.`) и друг друга.
FENCE_TEXT_WITHOUT_LEADER = True
#: Значения в подписи идут столбиком, как у Николая: тип забора строкой, под ним значения
#: (`C.L.F.` / `0.6'S` / `1.1'E`). Одной строкой блок шире и накрывает соседние подписи.
FENCE_TEXT_ONE_LINE = False
#: Повторять ли тип забора в подписи (`C.L.F. 0.2'S` вместо `FE.0.2'S`).
#: Выключено по снимкам 4.25: у Николая тип в подписи стоит там, где он **заменил** старую надпись типа
#: (`C.L.F.` → `C.L.F. 0.6'S 1.1'E`). Мы чужие надписи не правим, поэтому рядом с уже стоящим `C.L.F.`
#: наш `C.L.F. 0.2'S` — это второй `C.L.F.` в двух футах. Пишем общее `FE.`, как его же
#: `FE.1.4'E`, `FE.5.2'S`, `FE.0.1'W` на том же чертеже.
TYPE_PREFIX_FT = 0.0
#: Вершина забора у стены дома — внутренний конец калитки, а не оффсет: не подписывается, если она
#: дальше этого от границы участка, футы (H25, 6910/East 55th: `FE.7.9'E`, `FE.2.5'S` — мусор).
GATE_AT_WALL_MIN_FT = 0.0
#: …и ближе этого к стене дома, футы. По нашим трём парам правило ничего не срезает, а на Brooklyn
#: добавляет лишнюю подпись (4/3/2 → 5/3/3), поэтому по умолчанию выключено; у соседа проверено на
#: 6910 15th Ave и East 55th, которых в нашей выборке нет.
GATE_AT_WALL_FT = 0.5
#: Изгиб забора не больше этого — не вершина (H25 соседа). Выключено: упрощение ломаной сдвигает
#: индексы вершин и добавляет подписи (Geldner 8 → 9 лишних, Alvine 2 → 3, Brooklyn 2 → 3).
FENCE_SIMPLIFY_DEG = 0.0
#: Две подписи одного забора ближе этого вдоль одной стороны не ставятся, футы (H25 соседа).
#: На наших трёх парах шаг снимает одну верную подпись на Geldner (13 → 12 совпадений) и не убирает
#: лишние, поэтому по умолчанию выключено; включается, если у заказчика появятся многосторонние заборы.
FENCE_SAME_SIDE_SEP_FT = 0.0
#: …и ближе этого на разных сторонах участка, футы.
FENCE_DIFF_SIDE_SEP_FT = 0.0
#: Не больше стольких подписей забора на одну сторону участка (0 — без предела). Углы, разнесённые
#: вдоль стороны дальше ``SIDE_CAP_SPACING_FT``, считаются разными сторонами: на Brooklyn забор идёт
#: вдоль запада 100 ft, и Николай подписал оба его конца.
#: На наших трёх парах предел снимает верную подпись на Geldner (13 → 12 совпадений) и лишние не
#: убирает, поэтому по умолчанию выключен; у соседа правило проверено на его парах.
FENCE_SIDE_CAP = 0
SIDE_CAP_SPACING_FT = 30.0
#: Подписи одной вершины ближе этого не ставятся. Выключено: у Николая на одном углу стоят ДВЕ подписи
#: в 1.5 ft друг от друга (`FE.1.7'S` и `C.L.F. 0.2'S` на 192 Geldner), и правило резало верные подписи
#: (совпадений 13 → 7 на Geldner, 3 → 1 на Brooklyn).
SAME_CORNER_FT = 0.0
#: Зазор между подписями при раскладке, в высотах текста: с нулём блоки сходились вплотную и в AutoCAD
#: накрывали друг друга (шрифт шире нашей оценки).
LABEL_CLEARANCE = 0.35

# --------------------------------------------------------------------------- правила 4.20 (H27–H29)
#: Опорная точка для буквы на диагональной стороне: вектор оффсета, у которого обе составляющие больше
#: этой доли длины, считается диагональным. Для него буква берётся по осям листа (вверх = N, вправо = E),
#: а не по повороту стрелки севера. Осевые стороны дают ту же букву при любом севере, поэтому правило
#: ничего не меняет для них. 4.20: Alvine «FE.1.3'W» и «FE.1.4'E» — при северe 67.563° компас даёт N/S,
#: оси листа — W/E, как у Николая (2 из 2).
DIAGONAL_AXIS_LETTERS = True
#: Доля длины вектора, выше которой составляющая считается существенной (диагональ).
DIAGONAL_SHARE = 0.25
#: Подписи в местах, где линия забора пересекает сторону участка (H27). Николай: 192 Geldner
#: «FE.0.7'E», «FE.1.4'E» (забор идёт вдоль восточной стороны и уходит за южную) и «FE.5.2'S»
#: (внешний конец того же забора). 3 из 3 на 192 Geldner, 1 из 1 на Alvine (1.8'S).
CROSSING_LABELS = True
#: Смещение от точки пересечения до перпендикулярной стороны, при котором ставится подпись, футы.
CROSSING_MAX_FT = 2.0
#: Внешний конец пересекающего забора подписывается, если он ушёл за сторону больше этого, футы.
CROSSING_OUTSIDE_MIN_FT = 0.5
#: Угол между забором и перпендикулярной стороной, при котором пересечение считается «забором вдоль
#: стороны», градусы. Без него на Brooklyn появляются чужие «3.6'» и «3.3'» на косых пересечениях.
CROSSING_PARALLEL_TOL_DEG = 10.0
#: Углы забора, стоящие у точек съёмки с описанием забора, подписываются отдельно (H28). Николай
#: подписывает не все заборы, а углы обследованных заборов: VINYL FENCE / WOOD FENCE / C.L.F.
#: На 192 Geldner это 9 совпадений из 10 против 3 из 10 у правила «одна вершина на забор».
FENCE_CORNER_LABELS = True
#: Точка съёмки считается принадлежащей углу забора, если она не дальше этого, футы.
SURVEY_POINT_FT = 0.35
#: Слова в описании точки съёмки, по которым угол считается углом забора. «F E C» (fence encroachment)
#: в список не входит: на 192 Geldner Николай оффсеты у таких точек не ставит (4 из 4).
FENCE_DESCRIPTION_WORDS = ("FENCE", "FE.", "C.L.F", "CLF")
#: Угол забора у ворот не подписывается: у Николая на Brooklyn 2 из 2 угла с GATE без оффсета.
GATE_SKIP_FT = 4.0
#: Угол забора подписывается, если он не дальше этого от стороны участка, футы.
CORNER_MAX_FT = 6.0
#: Часть подписи (значение + буква) не дублируется у того же угла ближе этого расстояния, футы.
PART_DUPLICATE_FT = 2.0
#: Угол забора подписывается только если он ближе к стороне, чем соседние вершины (H31).
CORNER_LOCAL_MIN = True
#: Насколько соседняя вершина должна быть дальше, чтобы угол считался местным минимумом, футы.
CORNER_LOCAL_MIN_FT = 0.15
#: Конец забора дальше ``OUTSIDE_MAX_FT`` снаружи подписывается, если соседний отрезок забора не короче
#: этого, футы. 192 Geldner «FE.5.2'S» — конец 33-футового забора; на Brooklyn такой же конец
#: 6-футового выступа (5.3) Николай не подписал (H33).
FENCE_END_MIN_LENGTH_FT = 20.0
#: Короткая подпись у того же угла, чья часть уже есть в полной («FE.1.2'S» рядом с
#: «FE.0.1'E / 1.2'S»), не ставится: это одна и та же привязка (H32).
POST_PART_DEDUPE = True
#: Дом подписывается по стенам, параллельным стороне участка (H34), а не одной ближайшей вершиной.
HOUSE_WALL_LABELS = True
#: Наибольшее расстояние от стены дома до границы, которое подписывается, футы.
#: Больше ``PARALLEL_MAX_FT`` (6 ft) смысла нет: дальние стены отсекает сам ``_parallel_candidates``.
HOUSE_WALL_MAX_FT = 6.0
#: Стена короче этого — не стена дома, а выступ/эркер; не подписывается (H36), футы.
HOUSE_WALL_MIN_FT = 8.0
#: Не больше стольких стен на одно направление (N/E/S/W) у одного дома (H35).
HOUSE_WALLS_PER_SIDE = 2
#: Не больше стольких подписей у одного дома (H35).
HOUSE_MAX_LABELS = 4
#: Текст оффсета дома ставится у середины стены, как у Николая, а не у её конца (H35).
HOUSE_TEXT_AT_WALL_MIDDLE = True
#: Текст оффсета дома ставится внутри строения у стены, как у Николая, а не между стеной и границей (H38).
HOUSE_TEXT_INSIDE = True
#: Текст оффсета дома поворачивается вдоль границы: 0° у горизонтальной, 90° у вертикальной (H37).
#: Николай: 4.9'N — 90° при вертикальной восточной границе, 3.2'S и 3.5'S — 0° при южной (3 из 3).
HOUSE_TEXT_FOLLOWS_BOUNDARY = True
#: Вторая сторона у той же вершины ищется и на продолжении стороны (H29), не дальше этого, футы.
#: Николай: «W.FE. 0.1'E / 1.2'S» (северная сторона продолжена за восточный угол), «C.L.F. 0.6'S / 1.1'E».
SECOND_SIDE_EXTENSION_FT = 5.0

@dataclass(frozen=True)
class Feature:
    """Объект, от которого может идти оффсет: дом, забор, стена, заезд."""

    kind: str  # "house" | "fence" | "wall" | "driveway" | "other"
    handle: str
    points: tuple[Point, ...]
    #: контур замкнут (дом) или это открытая ломаная (П-образный контур соседнего здания, забор).
    #: ``None`` — по умолчанию: дом считается замкнутым, остальное — нет.
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
    #: До какого расстояния снаружи объект ещё подписывается: у обследованного конца забора — 6 ft (H33).
    allowed_outside_ft: float = OUTSIDE_MAX_FT
    #: Вершина для записи «якоря» подписи, если текст ставится не у неё (дом: текст у середины стены).
    anchor_override: Point | None = None


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


def offset_letter(dx: float, dy: float, north_deg: float = 90.0) -> str:
    """
    Буква направления оффсета с учётом диагональных сторон (правило 4.20).

    Для осевых сторон (вектор вдоль оси листа) буква по компасу и по осям листа совпадает. Для
    диагональной стороны компас даёт N/S, а Николай пишет E/W (Alvine «FE.1.3'W», «FE.1.4'E» при
    северe 67.563°). Поэтому диагональный вектор получает букву по осям листа: вверх = N, вправо = E.
    """
    length = math.hypot(dx, dy)
    if (
        DIAGONAL_AXIS_LETTERS
        and length > 1e-12
        and abs(dx) > DIAGONAL_SHARE * length
        and abs(dy) > DIAGONAL_SHARE * length
    ):
        return compass_letter(dx, dy, 90.0)
    return compass_letter(dx, dy, north_deg)


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


def _rect_near_polygon(rect: Rect, polygon: Sequence[Point], tol: float) -> bool:
    """Прямоугольник целиком внутри полигона или не дальше ``tol`` от его границы."""
    if _rect_inside_polygon(rect, polygon):
        return True
    x0, y0, x1, y1 = rect
    corners = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
    edges = [(polygon[i], polygon[(i + 1) % len(polygon)]) for i in range(len(polygon))]
    for corner in corners:
        if point_in_polygon(corner, polygon):
            return True
        if any(_point_segment_distance(corner, edge) <= tol for edge in edges):
            return True
    return False


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


def _normalize_type(text: str) -> str:
    """«C.L.F.», «C.L.F», «C.L.F» → «C.L.F» — для сравнения подписи типа забора."""
    return re.sub(r"[\s.]+", "", str(text)).upper()


#: Типы забора, которые Николай пишет в своих подписях (`C.L.F. 0.2'S`, `W.FE. 0.6'E`, `V.FE. 0.7'E`).
_FENCE_TYPE_TEXTS = ("C.L.F", "CLF", "W.FE", "WFE", "V.FE", "VFE", "WOODFE", "FE")


def _near_type_prefix(
    known: Sequence[tuple[str, Point, float]], point: Point, radius: float,
) -> str | None:
    """Тип забора из уже стоящей рядом подписи («C.L.F.», «W.FE.», «V.FE.»), иначе None."""
    best: tuple[float, str] | None = None
    for text, spot, _height in known:
        cleaned = _normalize_type(text)
        if any(ch.isdigit() for ch in cleaned) or cleaned not in _FENCE_TYPE_TEXTS:
            continue
        distance = math.dist(point, spot)
        if distance <= radius and (best is None or distance < best[0]):
            best = (distance, str(text).strip())
    return best[1] if best is not None else None


def _text_parts(text: str) -> list[tuple[float, str]]:
    """Части подписи «значение + буква» (``0.4'N``, ``FE.1.2'S``) — для дедупликации по частям."""
    out: list[tuple[float, str]] = []
    for line in str(text).replace("\\P", "\n").split("\n"):
        for match in _PART_RE.finditer(line):
            out.append((float(match.group("value")), match.group("dir")))
    return out


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
    width, block_h = _text_block(text, height, width_of)
    gap = TEXT_GAP_FACTOR * height
    mid = ((anchor[0] + target[0]) / 2.0, (anchor[1] + target[1]) / 2.0)
    half_extent = abs(px) * width / 2.0 + abs(py) * block_h / 2.0 + gap
    for extra in (0.0, 0.5 * height, height, 2.0 * height, 3.2 * height, 4.0 * height, 5.0 * height):
        for sign in (1.0, -1.0):
            d = half_extent + extra
            center = (mid[0] + sign * px * d, mid[1] + sign * py * d)
            rect = _rect_at(center, width, block_h)
            if ok(rect, center) and not _segment_hits_rect(anchor, target, rect):
                return center
    return None


def _place_house_text(
    polygon: Sequence[Point], anchor: Point, target: Point, text: str, height: float,
    width_of: Callable[[str, float], float], ok: Callable[[Rect, Point], bool],
) -> Point | None:
    """
    Текст без выноски внутри строения у стены (H38). Николай ставит его вплотную к стене изнутри и
    сдвигает вдоль стены, чтобы не накрыть крупную надпись здания («1 STY FRAME», «FRAME GARAGE»).
    """
    if len(polygon) < 3:
        return None
    dx, dy = anchor[0] - target[0], anchor[1] - target[1]
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return None
    inward = (dx / length, dy / length)
    # Стена, на которой стоит anchor: ближайший отрезок контура.
    a = b = None
    best = math.inf
    seq = list(polygon) + [polygon[0]]
    for p, q in zip(seq, seq[1:]):
        d = _point_segment_distance(anchor, (p, q))
        if d < best:
            best, a, b = d, p, q
    if a is None or b is None:
        return None
    width = width_of(text, height)
    gap = TEXT_GAP_FACTOR * height
    for offset in (0.8, 1.2, 1.8, 2.5, 3.2, 4.0):
        distance = height / 2.0 + gap * offset
        for share in (0.5, 0.4, 0.6, 0.3, 0.7, 0.2, 0.8, 0.1, 0.9):
            wall_point = (
                a[0] + (b[0] - a[0]) * share,
                a[1] + (b[1] - a[1]) * share,
            )
            center = (
                wall_point[0] + inward[0] * distance,
                wall_point[1] + inward[1] * distance,
            )
            if not point_in_polygon(center, polygon):
                continue
            rect = _rect_at(center, width, height)
            corners = [
                (rect[0], rect[1]), (rect[2], rect[1]), (rect[2], rect[3]), (rect[0], rect[3])
            ]
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
    closed = (
        feature.kind == "house" and len(pts) >= 3
        if feature.closed is None
        else bool(feature.closed)
    )
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


def _simplify_polyline(points: Sequence[Point], *, tol_deg: float = 5.0) -> list[Point]:
    """Убирает промежуточные почти коллинеарные точки забора: изгиб до 5° — не вершина для подписи."""
    pts = [(float(x), float(y)) for x, y in points]
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


def _feature_candidates(
    ring: Sequence[Point], feature: Feature, front_lines: Sequence[Sequence[Point]],
    *,
    survey: Sequence[tuple[Point, str]] = (),
    gates: Sequence[Point] = (),
    north_deg: float = 90.0,
    house_edges: Sequence[tuple[Point, Point]] = (),
) -> list[OffsetCandidate]:
    """Кандидаты для объекта: при CORNER_LABELS — углы по параллельным сторонам, иначе ближайшая сторона."""
    if FENCE_SIMPLIFY_DEG and feature.kind == "fence" and len(feature.points) > 2:
        # Почти прямые изгибы (≤ 5°) — не вершины: без них на длинном прогоне появляются лишние подписи.
        feature = replace(feature, points=tuple(_simplify_polyline(feature.points, tol_deg=FENCE_SIMPLIFY_DEG)))
    base = measure_candidates(ring, [feature], front=front_lines)
    if feature.kind not in ("fence", "house", "wall"):
        return base[:]

    def nearest() -> list[OffsetCandidate]:
        if not base:
            return []
        # Вершина на самой линии не подписывается; если у объекта есть другие вершины, берём их.
        usable = [c for c in base if c.distance_ft >= MIN_OFFSET_FT] or base
        return [
            min(
                usable,
                key=lambda c: (round_offset_ft(c.distance_ft), not c.inside, c.distance_ft),
            )
        ]

    if feature.kind == "fence":
        # 4.20: углы обследованных заборов вместо одной ближайшей вершины (H28) + пересечения (H27).
        chosen: list[OffsetCandidate] = []
        if FENCE_CORNER_LABELS:
            chosen = _surveyed_fence_corners(
                ring, feature, survey, gates, north_deg, house_edges
            )
        if not chosen:
            chosen = nearest()
        # Вершины на самой линии не подписываются, но нужны для «FE.O/L» (ONLINE_LABELS).
        chosen = chosen + [c for c in base if c.distance_ft < MIN_OFFSET_FT]
        if CROSSING_LABELS:
            for candidate in _crossing_candidates(ring, feature, north_deg, survey, gates):
                value = round_offset_ft(candidate.distance_ft)
                duplicate = any(
                    math.dist(candidate.vertex, other.vertex) < 2.0
                    and value == round_offset_ft(other.distance_ft)
                    for other in chosen
                )
                if not duplicate:
                    chosen.append(candidate)
        return chosen

    if feature.kind == "house" and HOUSE_WALL_LABELS:
        points = list(feature.points)
        closed = len(points) >= 3 if feature.closed is None else bool(feature.closed)
        seq = points + [points[0]] if closed else points
        walls, _covered = _parallel_candidates(ring, feature)
        by_key: dict[tuple[float, str], OffsetCandidate] = {}
        for candidate in walls:
            if not (MIN_OFFSET_FT <= candidate.distance_ft <= HOUSE_WALL_MAX_FT):
                continue
            if candidate.outside_ft > OUTSIDE_MAX_FT:
                continue
            if not (0 <= candidate.segment < len(seq) - 1):
                continue
            if math.dist(seq[candidate.segment], seq[candidate.segment + 1]) < HOUSE_WALL_MIN_FT:
                continue
            letter = offset_letter(
                candidate.vertex[0] - candidate.foot[0],
                candidate.vertex[1] - candidate.foot[1],
                north_deg,
            )
            key = (round_offset_ft(candidate.distance_ft), letter)
            best = by_key.get(key)
            if best is None or candidate.distance_ft < best.distance_ft:
                by_key[key] = candidate
        if by_key:
            per_side: dict[str, list[tuple[float, OffsetCandidate]]] = {}
            for (value, letter), candidate in by_key.items():
                per_side.setdefault(letter, []).append((candidate.distance_ft, candidate))
            chosen: list[OffsetCandidate] = []
            for letter, items in per_side.items():
                items.sort(key=lambda pair: pair[0])
                chosen += [candidate for _d, candidate in items[:HOUSE_WALLS_PER_SIDE]]
            chosen.sort(key=lambda c: c.distance_ft)
            chosen = chosen[:HOUSE_MAX_LABELS]
            if HOUSE_TEXT_AT_WALL_MIDDLE:
                moved: list[OffsetCandidate] = []
                for candidate in chosen:
                    if not (0 <= candidate.segment < len(seq) - 1):
                        moved.append(candidate)
                        continue
                    a, b = seq[candidate.segment], seq[candidate.segment + 1]
                    middle = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
                    shift = (middle[0] - candidate.vertex[0], middle[1] - candidate.vertex[1])
                    moved.append(
                        replace(
                            candidate,
                            vertex=middle,
                            foot=(candidate.foot[0] + shift[0], candidate.foot[1] + shift[1]),
                            anchor_override=candidate.vertex,
                        )
                    )
                chosen = moved
            return chosen

    if not CORNER_LABELS:
        return nearest()
    parallel, covered = _parallel_candidates(ring, feature)
    rest = [c for c in base if c.vertex not in covered]
    return parallel + rest


def _survey_description(point: Point, survey: Sequence[tuple[Point, str]]) -> str:
    """Описание точки съёмки в этом месте (ближайшая точка не дальше ``SURVEY_POINT_FT``)."""
    best: tuple[float, str] | None = None
    for spot, description in survey:
        distance = math.dist(point, spot)
        if distance <= SURVEY_POINT_FT and (best is None or distance < best[0]):
            best = (distance, description)
    return str(best[1]) if best else ""


def _is_fence_description(description: str) -> bool:
    text = str(description).upper()
    return any(word in text for word in FENCE_DESCRIPTION_WORDS)


def _segment_angle(a: Point, b: Point) -> float:
    return math.atan2(b[1] - a[1], b[0] - a[0])


def _angle_diff(a: float, b: float) -> float:
    diff = abs(a - b) % math.pi
    return min(diff, math.pi - diff)


def _vertex_candidates(
    ring: Sequence[Point], feature: Feature,
) -> list[tuple[OffsetCandidate, bool]]:
    """
    Для каждой вершины забора — ближайшая сторона участка и признак «соседний отрезок параллелен
    этой стороне». ``segment`` — номер отрезка объекта (для записи подписи у нужной линии).
    """
    out: list[tuple[OffsetCandidate, bool]] = []
    points = list(feature.points)
    for index, vertex in enumerate(points):
        projections = _side_projections(vertex, ring, True)
        if not projections:
            continue
        distance, foot, side = min(projections, key=lambda item: item[0])
        inside = point_in_polygon(vertex, ring)
        outside = 0.0 if inside else perpendicular_distance(vertex, ring, closed=True)[0]
        side_angle = _segment_angle(ring[side], ring[(side + 1) % len(ring)])
        parallel = False
        for edge in (index - 1, index):
            if 0 <= edge < len(points) - 1:
                a, b = points[edge], points[edge + 1]
                if math.dist(a, b) > 1e-9 and _angle_diff(_segment_angle(a, b), side_angle) <= math.radians(
                    PARALLEL_TOL_DEG
                ):
                    parallel = True
        candidate = OffsetCandidate(
            feature=feature, vertex=vertex, distance_ft=round(distance, 4), foot=foot,
            inside=inside, outside_ft=round(outside, 4),
            segment=min(index, max(0, len(points) - 2)),
        )
        out.append((candidate, parallel))
    return out


def _touches_building_away_from_lot(
    point: Point, ring: Sequence[Point], house_edges: Sequence[tuple[Point, Point]],
) -> bool:
    """Вершина забора стоит у стены дома и далеко от границы — это конец калитки, а не оффсет."""
    if not house_edges or GATE_AT_WALL_MIN_FT <= 0.0:
        return False
    if perpendicular_distance(point, ring, closed=True)[0] < GATE_AT_WALL_MIN_FT:
        return False
    return any(_point_segment_distance(point, edge) <= GATE_AT_WALL_FT for edge in house_edges)


def _surveyed_fence_corners(
    ring: Sequence[Point], feature: Feature,
    survey: Sequence[tuple[Point, str]], gates: Sequence[Point], north_deg: float,
    house_edges: Sequence[tuple[Point, Point]] = (),
) -> list[OffsetCandidate]:
    """
    Углы забора, стоящие у точек съёмки с описанием забора (H28).

    Николай подписывает не все заборы: на 192 Geldner из 10 подписей по правилу «ближайшая вершина»
    совпали 3, а по этому правилу — 9. Углы у ворот и точки «F E C» (fence encroachment) он пропускает:
    на Brooklyn 2 из 2 углов с GATE без оффсета. Один и тот же текст на одном заборе не повторяется.
    """
    if not survey:
        return []
    corners: list[OffsetCandidate] = []
    seen: set[tuple[float, str]] = set()
    points = list(feature.points)
    for index, (candidate, _parallel) in enumerate(_vertex_candidates(ring, feature)):
        if candidate.outside_ft > CORNER_MAX_FT:
            continue
        # Внутренний конец калитки у стены дома (H25) — не оффсет.
        if _touches_building_away_from_lot(candidate.vertex, ring, house_edges):
            continue
        value = round_offset_ft(candidate.distance_ft)
        if value < 0.1 or candidate.distance_ft > CORNER_MAX_FT:
            continue
        # Концы забора подписываются всегда; внутренние вершины сравниваются с геометрическими соседями.
        if 0 < index < len(points) - 1:
            if CORNER_LOCAL_MIN:
                prev_d = min(
                    (d for d, _f, _s in _side_projections(points[index - 1], ring, True)),
                    default=0.0,
                )
                next_d = min(
                    (d for d, _f, _s in _side_projections(points[index + 1], ring, True)),
                    default=0.0,
                )
                if (
                    prev_d < candidate.distance_ft + CORNER_LOCAL_MIN_FT
                    and next_d < candidate.distance_ft + CORNER_LOCAL_MIN_FT
                ):
                    continue
        description = _survey_description(candidate.vertex, survey)
        if not _is_fence_description(description):
            continue
        if any(math.dist(candidate.vertex, gate) <= GATE_SKIP_FT for gate in gates):
            continue
        letter = offset_letter(
            candidate.vertex[0] - candidate.foot[0],
            candidate.vertex[1] - candidate.foot[1],
            north_deg,
        )
        key = (value, letter)
        if key in seen:
            continue
        seen.add(key)
        # H33: конец обследованного забора подписывается и в 5–6 ft снаружи, если это конец длинного
        # прогона (192 Geldner «FE.5.2'S»), а не короткий выступ (Brooklyn 5.3 у 6-футового отрезка).
        if candidate.outside_ft > OUTSIDE_MAX_FT:
            lengths = []
            if index > 0:
                lengths.append(math.dist(points[index - 1], candidate.vertex))
            if index < len(points) - 1:
                lengths.append(math.dist(candidate.vertex, points[index + 1]))
            if min(lengths, default=0.0) < FENCE_END_MIN_LENGTH_FT:
                continue
            candidate = replace(candidate, allowed_outside_ft=CORNER_MAX_FT)
        corners.append(candidate)
    corners.sort(
        key=lambda c: (round_offset_ft(c.distance_ft), not c.inside, c.distance_ft)
    )
    if FENCE_SIDE_CAP:
        # Не больше ``FENCE_SIDE_CAP`` подписей на одну букву стороны (R13c соседа).
        per_letter: dict[str, list[OffsetCandidate]] = {}
        capped: list[OffsetCandidate] = []
        for candidate in corners:
            letter = offset_letter(
                candidate.vertex[0] - candidate.foot[0],
                candidate.vertex[1] - candidate.foot[1],
                north_deg,
            )
            same = per_letter.setdefault(letter, [])
            if len(same) >= FENCE_SIDE_CAP and all(
                math.dist(candidate.vertex, other.vertex) < SIDE_CAP_SPACING_FT for other in same
            ):
                continue
            same.append(candidate)
            capped.append(candidate)
        corners = capped
    # Шаг между подписями одного забора (H25 соседа): вдоль одной стороны 22 ft, на разных сторонах 12 ft.
    spaced: list[OffsetCandidate] = []
    for candidate in corners:
        separation = (
            FENCE_SAME_SIDE_SEP_FT
            if any(candidate.segment == other.segment for other in spaced)
            else FENCE_DIFF_SIDE_SEP_FT
        )
        if any(math.dist(candidate.vertex, other.vertex) < separation for other in spaced):
            continue
        spaced.append(candidate)
    return spaced


def _crossing_candidates(
    ring: Sequence[Point], feature: Feature, north_deg: float,
    survey: Sequence[tuple[Point, str]] = (), gates: Sequence[Point] = (),
) -> list[OffsetCandidate]:
    """
    Подписи в местах, где линия забора пересекает сторону участка (H27).

    Забор, идущий вдоль одной стороны и уходящий за другую, получает подпись в точке пересечения:
    расстояние до перпендикулярной стороны («FE.0.7'E», «FE.1.4'E» на 192 Geldner). Внешний конец
    того же забора получает расстояние до пересечённой стороны («FE.5.2'S»). Подпись ставится только
    если забор идёт вдоль перпендикулярной стороны: косые пересечения на Brooklyn дают чужие «3.6'».
    """
    if not CROSSING_LABELS:
        return []
    out: list[OffsetCandidate] = []
    points = list(feature.points)
    segments = list(zip(ring, list(ring[1:]) + [ring[0]]))
    for edge, (a, b) in enumerate(zip(points, points[1:])):
        rx, ry = b[0] - a[0], b[1] - a[1]
        length = math.hypot(rx, ry)
        if length < 1e-9:
            continue
        for side, (c, d) in enumerate(segments):
            sx, sy = d[0] - c[0], d[1] - c[1]
            den = rx * sy - ry * sx
            if abs(den) < 1e-12:
                continue
            wx, wy = c[0] - a[0], c[1] - a[1]
            t = (wx * sy - wy * sx) / den
            u = (wx * ry - wy * rx) / den
            side_length = math.hypot(sx, sy)
            # Сторона может быть продолжена за угол: забор идёт вдоль восточной стороны и уходит
            # за южную (192 Geldner «FE.0.7'E»). Пересечение — на продолжении стороны не дальше
            # ``LINE_EXTENSION_FT``.
            slack = LINE_EXTENSION_FT / side_length if side_length > 1e-9 else 0.0
            if not (-1e-9 <= t <= 1.0 + 1e-9 and -slack <= u <= 1.0 + slack):
                continue
            point = (a[0] + t * rx, a[1] + t * ry)
            inside_a = point_in_polygon(a, ring)
            inside_b = point_in_polygon(b, ring)
            # Сторона, перпендикулярная пересечённой, вдоль которой идёт забор (забор параллелен ей).
            best: tuple[float, Point, int] | None = None
            if not inside_a and not inside_b:
                for other, (e, f) in enumerate(segments):
                    if other == side:
                        continue
                    if _angle_diff(_segment_angle(a, b), _segment_angle(e, f)) > math.radians(
                        CROSSING_PARALLEL_TOL_DEG
                    ):
                        continue
                    distance, foot = perpendicular_line_distance(point, [e, f], closed=False)
                    if distance > CROSSING_MAX_FT:
                        continue
                    if best is None or distance < best[0]:
                        best = (distance, foot, other)
            if best is not None:
                distance, foot, _other = best
                value = round_offset_ft(distance)
                if value >= 0.1:
                    out.append(
                        OffsetCandidate(
                            feature=feature, vertex=point, distance_ft=round(distance, 4),
                            foot=foot, inside=False, outside_ft=0.0,
                            segment=min(edge, max(0, len(points) - 2)),
                        )
                    )
            # Внешний конец пересекающего забора — расстояние до пересечённой стороны.
            if inside_a != inside_b:
                outside_end = a if not inside_a else b
                # Внешний конец подписывается только у обследованного забора: на Brooklyn концы
                # «O PORCH» и «VINYL FENCE GATE» Николай не подписал (2 из 2), на Alvine «VINYL FENCE»
                # подписал (3.2'N), на 192 Geldner — 5.2'S.
                if not _is_fence_description(_survey_description(outside_end, survey)):
                    continue
                if any(math.dist(outside_end, gate) <= GATE_SKIP_FT for gate in gates):
                    continue
                distance, foot = perpendicular_line_distance(outside_end, [c, d], closed=False)
                if CROSSING_OUTSIDE_MIN_FT < distance <= OUTSIDE_MAX_FT:
                    out.append(
                        OffsetCandidate(
                            feature=feature, vertex=outside_end, distance_ft=round(distance, 4),
                            foot=foot, inside=False, outside_ft=round(distance, 4),
                            segment=min(edge, max(0, len(points) - 2)),
                        )
                    )
    return out


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
    survey: Sequence[tuple[Point, str]] = (),
    gates: Sequence[Point] = (),
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
            closed = (
                feature.kind == "house" and len(pts) >= 3
                if feature.closed is None
                else bool(feature.closed)
            )
            seq = pts + [pts[0]] if closed else pts
            lines += list(zip(seq, seq[1:]))

    # Стены домов: по ним отсекаются внутренние концы калиток (H25).
    house_edges: list[tuple[Point, Point]] = []
    for house in features:
        if house.kind != "house" or len(house.points) < 2:
            continue
        hpts = list(house.points)
        hclosed = house.closed if house.closed is not None else len(hpts) >= 3
        hseq = hpts + [hpts[0]] if hclosed else hpts
        house_edges += list(zip(hseq, hseq[1:]))

    # Границы, к которым меряются оффсеты: контур участка и фронт. По ним задаётся угол текста (R10).
    boundary_segs: list[tuple[Point, Point]] = list(zip(ring, ring[1:] + ring[:1]))
    for line in front_lines:
        boundary_segs += list(zip(line, line[1:]))
    placed: list[Rect] = []
    dup_radius = DUPLICATE_RADIUS_FT * factor

    plan.counts["zone"] = len(features)
    for index, feature in enumerate(features):
        label = feature.handle or f"#{index}"
        in_area = area_pts is None or any(point_in_polygon(p, area_pts) for p in feature.points)
        if not in_area and area_pts is not None and AREA_EDGE_TOL_FT > 0:
            # Объекты оффсетов стоят у самой границы участка, часть — снаружи неё.
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
        feature_candidates = _feature_candidates(
            ring, feature, front_lines, survey=survey, gates=gates, north_deg=north_deg,
            house_edges=house_edges,
        )
        candidates = [c for c in feature_candidates if c.outside_ft <= c.allowed_outside_ft]
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
        # У забора 4.20 и у дома 4.22 candidates уже выбраны правилами (H27/H28 и H34–H36) — каждый
        # кандидат даёт свою подпись. Стена по-прежнему получает одну подпись на объект.
        multi = (
            CORNER_LABELS
            or (feature.kind == "house" and HOUSE_WALL_LABELS)
            or (feature.kind == "fence" and (FENCE_CORNER_LABELS or CROSSING_LABELS))
        )
        targets: list[tuple[OffsetCandidate, bool]] = [
            (best, False) for best in (candidates if multi else candidates[:1])
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
            direction = offset_letter(vx, vy, north_deg)
            height = offset_height_ft("house" if feature.kind == "house" else "fence", scale)
            text = ONLINE_FENCE_TEXT if is_online else offset_text(feature.kind, value, direction)
            # Тип забора из стоящей рядом подписи: «C.L.F. 0.2'S» вместо «FE.0.2'S» — как у Николая.
            type_prefix = ""
            if TYPE_PREFIX_FT and feature.kind == "fence" and not is_online:
                type_prefix = _near_type_prefix(known, best.vertex, TYPE_PREFIX_FT) or ""
                if type_prefix:
                    text = f"{type_prefix} {value:.1f}'{direction}"
            extra: list[tuple[float, str]] = []
            if feature.kind in ("fence", "wall") and not is_online:
                boundaries: list[tuple[Sequence[Point], bool]] = [(ring, True)]
                boundaries += [(line, False) for line in front_lines]
                taken = (value, direction)
                extra = _extra_sides(best.vertex, best.foot, taken, boundaries, north_deg)
            mid = ((best.vertex[0] + best.foot[0]) / 2.0, (best.vertex[1] + best.foot[1]) / 2.0)

            # Одна подпись на вершину/калитку: вторая в трёх футах — уже наложение.
            if feature.kind == "fence" and any(
                math.dist(best.vertex, other.anchor) <= SAME_CORNER_FT
                for other in plan.items
                if other.kind == "fence"
            ):
                plan.skipped.append((label, "у этой вершины подпись уже стоит"))
                continue
            # Тот же текст у той же вершины или середины выноски — дубль.
            if any(
                _key(old) == _key(text)
                and min(
                    math.hypot(p[0] - best.vertex[0], p[1] - best.vertex[1]),
                    math.hypot(p[0] - mid[0], p[1] - mid[1]),
                ) <= dup_radius
                for old, p, _h in known
            ):
                plan.skipped.append((label, f"«{text}» уже стоит рядом"))
                continue

            # Дедупликация по частям, а не по целой строке: в одной выноске бывает две части
            # («FE.0.1'E / 1.2'S»), и вторая не должна появляться отдельной подписью у того же угла.
            def part_near(value_ft: float, letter: str) -> bool:
                for old_text, point, _height in known:
                    if (value_ft, letter) in _text_parts(old_text) and min(
                        math.hypot(point[0] - best.vertex[0], point[1] - best.vertex[1]),
                        math.hypot(point[0] - mid[0], point[1] - mid[1]),
                    ) <= PART_DUPLICATE_FT:
                        return True
                return False

            own = _text_parts(text)
            if own and all(part_near(value_ft, letter) for value_ft, letter in own):
                plan.skipped.append((label, f"«{text}» уже стоит рядом"))
                continue
            extra = [pair for pair in extra if not part_near(*pair)]

            bounds = _area_bounds(area_pts, margin=15.0 * factor)

            clearance = LABEL_CLEARANCE * height

            def ok(rect: Rect, center: Point) -> bool:
                if area_pts is not None and not _rect_near_polygon(rect, area_pts, AREA_EDGE_TOL_FT):
                    return False
                if LABEL_MUST_BE_INSIDE and not point_in_polygon(center, ring):
                    return False
                wide = _grow_rect(rect, clearance)
                if any(
                    _rects_overlap(wide, _grow_rect(other, clearance))
                    for other in text_rects + placed
                ):
                    return False
                return not any(
                    _segment_hits_rect(a, b, rect)
                    for a, b in lines
                    if bounds is None or _segment_in_bounds(a, b, bounds)
                )

            # Значения одной строкой: «W.FE. 0.1'E 1.2'S» (Николай) вместо двух строк выноски.
            # Префикс типа (FE., C.L.F., W.FE.) стоит один раз в начале строки.
            display_parts = [text] + [f"{v:.1f}'{d}" for v, d in extra]
            line_text = " ".join(display_parts) if FENCE_TEXT_ONE_LINE else "\n".join(display_parts)
            item: OffsetItem | None = None
            beside = None
            if feature.kind == "house":
                beside = None
                if HOUSE_TEXT_INSIDE:
                    beside = _place_house_text(
                        feature.points, best.vertex, best.foot, text, height, width_of, ok
                    )
                if beside is None:
                    beside = _place_beside_line(
                        best.vertex, best.foot, text, height, width_of, ok
                    )
                # У середины стены места может не быть — пробуем у исходного угла (текст без выноски,
                # как у Николая), и только потом выноску.
                if beside is None and best.anchor_override is not None:
                    corner = best.anchor_override
                    corner_foot = (
                        corner[0] + best.foot[0] - best.vertex[0],
                        corner[1] + best.foot[1] - best.vertex[1],
                    )
                    beside = _place_beside_line(
                        corner, corner_foot, text, height, width_of, ok
                    )
            if beside is None and feature.kind in ("fence", "wall") and FENCE_TEXT_WITHOUT_LEADER:
                # Забор и стена: текст у линии без выноски — как у Николая — и только потом выноска.
                beside = _place_beside_line(best.vertex, best.foot, line_text, height, width_of, ok)
                if beside is None:
                    # У самой линии места нет (текст накрыл бы забор) — берём свободное место рядом,
                    # но по-прежнему без выноски: стрелок у Николая нет.
                    spot = _place_leader(
                        best.vertex, best.foot, line_text, height, width_of, ok, factor
                    )
                    if spot is not None:
                        beside = spot[0]
                if beside is not None:
                    text = line_text
            if beside is not None:
                rect = _rect_at(beside, *_text_block(text, height, width_of))
                item = OffsetItem(
                    kind=feature.kind, value_ft=value, direction=direction, style="text",
                    height_ft=height, anchor=best.anchor_override or best.vertex, target=best.foot, text=text,
                    point=beside, leader=None,
                    rotation_deg=(
                        _boundary_angle_deg(best.foot, boundary_segs)
                        if TEXT_FOLLOWS_BOUNDARY
                        or (feature.kind == "house" and HOUSE_TEXT_FOLLOWS_BOUNDARY)
                        else 0.0
                    ),
                )
            else:
                # Дом без места под текст получает выноску с «BLDG»; забор и стена — выноску как есть.
                leader_text = line_text
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
                    height_ft=height, anchor=best.anchor_override or best.vertex, target=best.foot, text=leader_text,
                    point=center, leader=(best.vertex, end),
                )
            placed.append(rect)
            plan.items.append(item)
            known.append((item.text, item.point, item.height_ft))
            used.add(seg_key)

    if POST_PART_DEDUPE and plan.items:
        # Часть, которая уже стоит в полной выноске у того же угла, отдельной подписью не повторяется.
        kept: list[OffsetItem] = []
        for item in plan.items:
            parts = _text_parts(item.text)
            redundant = len(parts) == 1 and any(
                len(_text_parts(other.text)) > 1
                and parts[0] in _text_parts(other.text)
                and math.dist(item.anchor, other.anchor) <= PART_DUPLICATE_FT
                for other in plan.items
                if other is not item and other.kind == item.kind
            )
            if redundant:
                plan.skipped.append((item.kind, f"«{item.text}» уже есть в полной подписи рядом"))
                continue
            kept.append(item)
        plan.items = kept
    return plan


def _line_projections(
    point: Point, line: Sequence[Point], closed: bool, extension_ft: float,
) -> list[tuple[float, Point]]:
    """Расстояние до каждой стороны как до прямой, с продолжением за концы на ``extension_ft``."""
    out: list[tuple[float, Point]] = []
    segments = list(zip(line, line[1:]))
    if closed and len(line) > 2:
        segments.append((line[-1], line[0]))
    for a, b in segments:
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        if length <= 1e-9:
            continue
        t = ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / (length * length)
        if not (-extension_ft / length <= t <= 1.0 + extension_ft / length):
            continue
        foot = (a[0] + t * dx, a[1] + t * dy)
        distance = abs((point[0] - a[0]) * dy - (point[1] - a[1]) * dx) / length
        out.append((distance, foot))
    return out


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
        # H29: основание может лежать на продолжении стороны за углом (не дальше SECOND_SIDE_EXTENSION_FT).
        # Пример: «W.FE. 0.1'E / 1.2'S» — северная сторона продолжена за восточный угол участка.
        for distance, foot in _line_projections(
            vertex, line, closed, SECOND_SIDE_EXTENSION_FT
        ):
            if math.dist(foot, best_foot) < 1e-6 or distance > SECOND_SIDE_FT:
                continue
            value = round_offset_ft(distance)
            if value <= 0.0:
                continue
            direction = offset_letter(vertex[0] - foot[0], vertex[1] - foot[1], north_deg)
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
    # Шаг строк тот же, что у подписей объектов (mtext_line_spacing_factor = 1.67).
    return width, height * (1.0 + TEXT_LINE_SPACING * (len(lines) - 1))


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
