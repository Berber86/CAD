"""
Шаг 4 — оффсеты: подписи расстояний от дома, заборов и стен до границы участка.

Каркас (версия 4.1). Правила заказчика, которые уже учтены:

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

Чего пока нет: правил выбора объектов и способа записи. ``plan_offsets`` возвращает пустой план
с предупреждением. Правила будут добавляться по парам дампов «до» и «после», которые пришлёт заказчик.

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
from typing import Any, Sequence

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


# --------------------------------------------------------------------------- каркас плана


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


def measure_candidates(property_pts: Sequence[Point], features: Sequence[Feature]) -> list[OffsetCandidate]:
    """Для каждой вершины каждого объекта — расстояние до границы участка."""
    out: list[OffsetCandidate] = []
    for feature in features:
        for vertex in feature.points:
            distance, foot = perpendicular_distance(vertex, property_pts, closed=True)
            out.append(
                OffsetCandidate(
                    feature=feature,
                    vertex=vertex,
                    distance_ft=round(distance, 4),
                    foot=foot,
                    inside=point_in_polygon(vertex, property_pts),
                )
            )
    return out


@dataclass(frozen=True)
class OffsetItem:
    """Готовая подпись оффсета (пока не заполняется: правил нет)."""

    kind: str
    value_ft: float
    direction: str | None
    style: str  # "leader" | "dimension"
    height_ft: float
    anchor: Point
    target: Point | None = None


@dataclass
class OffsetPlan:
    items: list[OffsetItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def plan_offsets(
    property_pts: Sequence[Point],
    features: Sequence[Feature],
    *,
    scale: float = BASE_SCALE,
) -> OffsetPlan:
    """
    Шаг 4 целиком. Каркас: измерения считаются, подписи не ставятся.

    Порядок, который будет заполняться по эталонам: выбор объектов → измерение → способ записи
    (выноска или размерная подпись) → высота по ``offset_height_ft`` → размещение → проверка
    наложений. Пока на каждом этапе есть только заглушка, поэтому план пустой.
    """
    plan = OffsetPlan()
    if len(property_pts) < 3:
        plan.warnings.append("граница участка: меньше трёх вершин — оффсеты не строятся")
        return plan
    measure_candidates(property_pts, features)  # проверка, что измерение работает на входных данных
    plan.warnings.append(
        "шаг 4: правила выбора объектов и способа записи не заданы — подписи не созданы"
    )
    return plan


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
