"""
Развод наложений (3.86–3.90): соседний участок, наложенный на наш, сдвигается в предпросмотре.

Правила (см. docs/customer_rules.md, раздел «Развод наложений»):
- Двигаем только наложения (площадь пересечения больше порога). Зазоры не трогаем.
- Сосед переносится жёстко: форма, длины и румбы не меняются, меняется только положение.
  Наш участок не меняется никогда: его румбы и длины — по нашему описанию.
- Шаг 1 (как в 3.88): обычный сдвиг наружу до появления общей межи (общая межа не короче 10%
  большей стороны, допуск параллельности 2°). Если сторона соседа параллельна нашей без клина,
  этот сдвиг принимается.
- Шаг 2 (3.89–3.90): если клин есть (стороны расходятся), применяется соединение ближайших углов
  с общей межой на всю меньшую из двух сторон. Сдвиг должен идти параллельно одной из сторон
  соседа (допуск 2°) и быть не длиннее его самой короткой стороны. Иначе сосед не трогается.
- Внутрь (через наш участок) не двигаем, если явно не разрешено.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Sequence

from shapely import affinity
from shapely.geometry import LineString, Polygon

#: Площадь пересечения, меньше которой наложением не считаем (как в сверке: «0.5 кв. ft — не наложение»).
OVERLAP_TOL_SQFT = 0.5
#: Шаг 1 (как в 3.88): общая межа не короче этой доли большей стороны.
LEGACY_SHARED_FRACTION = 0.10
#: Допуск параллельности сторон, градусов (расхождение в документах; клин войдёт в полосу).
PARALLEL_MAX_DEG = 2.0
#: Клин, футов: расхождение сторон на дальнем конце больше этого — клин есть (3.91: 0.1 ft).
WEDGE_EPS_FT = 0.1
#: Вариант «направление»: соединение углов принимается, если сдвиг отклоняется от ближайшей стороны
#: соседа не больше этого угла, градусов (3.92; число пока предварительное).
DIRECTION_TOL_DEG = 15.0
#: Вариант «углы» (3.95): соединение углов не длиннее этой доли короткой стороны соседа.
CORNER_MAX_LEN_FACTOR = 1.5
#: Варианты шага 2 (3.92): «corner» — углы важнее направления; «direction» — направление важнее.
VARIANTS = ("corner", "direction")
#: Базовая полоса допуска общей межи, футов (как JOIN_LINE_TOLERANCE_FT).
CONTACT_BAND_FT = 0.05
#: Общая межа на всю меньшую из двух сторон; допуск на концах, футов (3.89).
CORNER_SHARE_TOL_FT = 0.5
#: Предельный сдвиг соседа, футов (шаг 1).
MAX_MOVE_FT = 200.0
#: Шаг перебора сдвига вдоль стороны соседа, футов (шаг 2).
SIDE_STEP_FT = 0.05
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
    #: Клин: расхождение сторон на дальнем конце общей межи, футов (примечание в журнале).
    wedge_ft: float = 0.0
    #: Как найден сдвиг: ``3.88`` — обычный сдвиг; ``corner`` — соединение углов;
    #: ``side`` — сдвиг параллельно стороне соседа.
    method: str = ""
    note: str = ""
    #: Почему соединение углов не принято (для журнала; пусто, если углы приняты).
    corner_note: str = ""
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


def _legacy_translations(
    points_a, points_b, max_move_ft: float, sample_ft: float = 0.25, max_samples: int = 800,
) -> list[tuple[float, float, float, float, float]]:
    """
    Шаг 1 (как в 3.88): сдвиги соседа, при которых его сторона ложится на нашу с общей частью
    не короче 10% большей из двух сторон.

    Для пары сторон (наша A, соседа B) направление n — наружу от участка. Сосед сдвигается так,
    чтобы его ближайшая к нам точка стороны B легла на линию A. Сдвиг t = c·n + s·u, где u —
    направление A; общая часть линейна по s. Возвращаются (dx, dy, полоса, need, клин),
    отсортировано по длине сдвига.
    """
    A = _edges_with_points(points_a)
    B = _edges_with_points(points_b)
    outward_sign = 1.0 if Polygon(points_a).exterior.is_ccw else -1.0
    sin_par = math.sin(math.radians(PARALLEL_MAX_DEG))
    need = LEGACY_SHARED_FRACTION * max(
        max((r[4] for r in A), default=0.0), max((r[4] for r in B), default=0.0),
    )
    out: list[tuple[float, float, float, float, float]] = []
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
                    out.append((dx, dy, band, need, wedge))
    out.sort(key=lambda d: math.hypot(d[0], d[1]))
    return out


def _corner_translations(
    points_a, points_b, max_move_ft: float,
) -> list[tuple[float, float, float, float, float]]:
    """
    Соединение ближайших углов (3.89): у параллельных сторон пары (наша A, соседа B) пары концов
    сопоставляются по порядку вдоль фронта; сдвиг совмещает угол соседа с нашим углом.
    Хорда между совмещёнными углами — общая межа; расхождение на дальнем конце — клин.
    Возвращает (dx, dy, полоса, need, клин), отсортировано по длине сдвига.
    """
    A = _edges_with_points(points_a)
    B = _edges_with_points(points_b)
    sin_par = math.sin(math.radians(PARALLEL_MAX_DEG))
    out: list[tuple[float, float, float, float, float]] = []
    for ax0, ay0, ax1, ay1, la, ux, uy, _, _ in A:
        for bx0, by0, bx1, by1, lb, vx, vy, _, _ in B:
            cross = ux * vy - uy * vx
            if abs(cross) > sin_par:
                continue
            wedge = min(la, lb) * abs(cross)
            band = CONTACT_BAND_FT + wedge
            need = min(la, lb) - CORNER_SHARE_TOL_FT
            if need <= 0:
                continue
            same_dir = ux * vx + uy * vy >= 0.0
            if same_dir:
                pairs = [((ax0, ay0), (bx0, by0)), ((ax1, ay1), (bx1, by1))]
            else:
                pairs = [((ax0, ay0), (bx1, by1)), ((ax1, ay1), (bx0, by0))]
            for (pax, pay), (pbx, pby) in pairs:
                dx, dy = pax - pbx, pay - pby
                if math.hypot(dx, dy) <= max_move_ft:
                    out.append((dx, dy, band, need, wedge))
    out.sort(key=lambda d: math.hypot(d[0], d[1]))
    return out


def _side_directions(points_b) -> list[tuple[float, float]]:
    """Единичные направления сторон соседа в обе стороны (без дубликатов)."""
    dirs: list[tuple[float, float]] = []
    for *_, vx, vy, _, _ in _edges_with_points(points_b):
        for sx, sy in ((vx, vy), (-vx, -vy)):
            if not any(abs(sx - ax) + abs(sy - ay) < 1e-9 for ax, ay in dirs):
                dirs.append((sx, sy))
    return dirs


def _sidewise_translations(
    points_a, points_b, max_move_ft: float,
) -> list[tuple[float, float, float, float, float]]:
    """
    Шаг 2 (3.90): сдвиг соседа параллельно одной из его сторон, на длину не больше самой короткой
    его стороны. Для параллельной пары (наша A, соседа B) общая межа — на всю меньшую из двух.
    """
    A = _edges_with_points(points_a)
    B = _edges_with_points(points_b)
    sin_par = math.sin(math.radians(PARALLEL_MAX_DEG))
    short_nb = min((r[4] for r in B), default=0.0)
    limit = min(max_move_ft, short_nb)
    steps = int(limit / SIDE_STEP_FT)
    dirs = _side_directions(points_b)
    out: list[tuple[float, float, float, float, float]] = []
    for _ax0, _ay0, _ax1, _ay1, la, ux, uy, _, _ in A:
        for _bx0, _by0, _bx1, _by1, lb, vx, vy, _, _ in B:
            cross = ux * vy - uy * vx
            if abs(cross) > sin_par:
                continue
            wedge = min(la, lb) * abs(cross)
            band = CONTACT_BAND_FT + wedge
            need = min(la, lb) - CORNER_SHARE_TOL_FT
            if need <= 0:
                continue
            for wx, wy in dirs:
                for k in range(1, steps + 1):
                    lam = k * SIDE_STEP_FT
                    out.append((lam * wx, lam * wy, band, need, wedge))
    out.sort(key=lambda d: math.hypot(d[0], d[1]))
    return out


def _angle_to_nearest_side_deg(dx: float, dy: float, points_b) -> float:
    """Наименьший угол (0–90°) между сдвигом (dx, dy) и одной из сторон соседа."""
    norm = math.hypot(dx, dy)
    if norm <= 1e-12:
        return 90.0
    ux, uy = dx / norm, dy / norm
    best = 90.0
    for *_, vx, vy, _, _ in _edges_with_points(points_b):
        sin_a = abs(ux * vy - uy * vx)
        best = min(best, math.degrees(math.asin(min(1.0, sin_a))))
    return best


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
    max_move_ft: float = MAX_MOVE_FT,
    overlap_tol_sqft: float = OVERLAP_TOL_SQFT,
    allow_inward: bool = False,
    variant: str = "corner",
) -> SlideResult:
    """
    Сдвиг соседа ``neighbour_points`` с нашего участка ``main_points`` до появления общей межи.

    Шаг 1 — обычный сдвиг как в 3.88. Если он найден и клина нет, он принимается.
    Шаг 2 — при клине: соединение углов или сдвиг параллельно стороне соседа (не длиннее его
    короткой стороны), общая межа на всю меньшую из двух сторон.
    ``street_lines`` — отрезки красных линий улиц (пары точек), в которые сосед не заходит.
    ``others`` — контуры уже развёденных соседей, на которые сосед не должен наехать.
    """
    if variant not in VARIANTS:
        raise ValueError(f"неизвестный вариант развода: {variant!r}")
    main = _polygon(main_points)
    neighbour = _polygon(neighbour_points)
    if main is None or neighbour is None:
        return SlideResult(status="invalid", note="контур участка не строится")

    before = main.intersection(neighbour).area
    if before <= overlap_tol_sqft:
        return SlideResult(status="no_overlap", overlap_before_sqft=before)

    street = [LineString(seg) for seg in street_lines if len(seg) >= 2]  # только красные линии улиц
    other_polys = [p for p in (_polygon(o) for o in others) if p is not None]

    def check_reason(dx: float, dy: float, band: float, need: float) -> str | None:
        """None — сдвиг допустим; иначе причина отказа."""
        moved = affinity.translate(neighbour, dx, dy)
        if main.intersection(moved).area > overlap_tol_sqft:
            return "наложение с нашим"
        if any(moved.intersection(p).area > overlap_tol_sqft for p in other_polys):
            return "наложение с соседом"
        if _crosses_street(moved, street):
            return "улица"
        if _shared_length_banded(main, moved, band) + 1e-6 < need:
            return "общая межа короче меньшей стороны"
        return None

    main_c = main.centroid
    nb_c = neighbour.centroid
    away = (nb_c.x - main_c.x, nb_c.y - main_c.y)
    rejected: dict[str, int] = {
        "не наружу": 0, "наложение с нашим": 0, "наложение с соседом": 0,
        "улица": 0, "общая межа короче меньшей стороны": 0, "не вдоль стороны соседа": 0,
        "длиннее короткой стороны соседа": 0,
    }
    tried = 0

    def first_valid(options, label_of):
        nonlocal tried
        for item in options:
            dx, dy, band, need, wedge = item[:5]
            tried += 1
            if not allow_inward and dx * away[0] + dy * away[1] < -1e-9:
                rejected["не наружу"] += 1
                continue
            reason = check_reason(dx, dy, band, need)
            if reason is None:
                return item
            rejected[reason] += 1
        return None

    # Шаг 1: обычный сдвиг как в 3.88.
    legacy = first_valid(_legacy_translations(main_points, neighbour_points, max_move_ft), None)
    if legacy is not None and legacy[4] <= WEDGE_EPS_FT:
        dx, dy, band, need, wedge = legacy
        return _moved(main, neighbour, dx, dy, band, before, wedge, "3.88")

    # Шаг 2: клин есть (или обычного сдвига нет) — соединение углов или сдвиг вдоль стороны соседа.
    corners = _corner_translations(main_points, neighbour_points, max_move_ft)
    if variant == "direction":
        # Направление важнее: угловой сдвиг принимается, только если он близок к стороне соседа.
        kept = [c for c in corners if _angle_to_nearest_side_deg(c[0], c[1], neighbour_points) <= DIRECTION_TOL_DEG]
        rejected["не вдоль стороны соседа"] += len(corners) - len(kept)
    else:
        # Углы важнее: соединение углов принимается при любом направлении.
        kept = corners
    short_nb = min((r[4] for r in _edges_with_points(neighbour_points)), default=0.0)
    corner_limit = short_nb * (CORNER_MAX_LEN_FACTOR if variant == "corner" else 1.0)
    corner_part = [(c, "corner") for c in kept if math.hypot(c[0], c[1]) <= corner_limit + 1e-9]
    corner_note = _corner_diagnosis(corners, kept, corner_limit, check_reason, away, allow_inward)
    side_part = [
        (s, "side") for s in _sidewise_translations(main_points, neighbour_points, max_move_ft)
        if math.hypot(s[0], s[1]) <= short_nb + 1e-9
    ]
    if variant == "direction":
        tagged = sorted(corner_part + side_part, key=lambda t: math.hypot(t[0][0], t[0][1]))
    else:
        # Сначала соединение углов (по длине), затем сдвиг вдоль стороны соседа.
        tagged = corner_part + side_part
    found = None
    for cand, method in tagged:
        hit = first_valid([cand], None)
        if hit is not None:
            found = (hit, method)
            break

    if found is None:
        counts = ", ".join(f"{name} — {n}" for name, n in rejected.items() if n) or "кандидатов нет"
        if legacy is not None:
            head = (
                f"обычный сдвиг есть, но сосед повёрнут (клин {legacy[4]:.2f} ft на дальнем конце) — "
                "нужен сдвиг вдоль стороны соседа, не длиннее его короткой стороны "
                f"({short_nb:.2f} ft), с общей межой на всю меньшую сторону"
            )
        else:
            head = (
                f"наружу нет сдвига до {max_move_ft:.0f} ft, при котором наложения нет и общая межа "
                f"не короче меньшей из сторон (минус {CORNER_SHARE_TOL_FT:.1f} ft)"
            )
        note = f"{head}; проверено сдвигов: {tried} (отсеяно: {counts}); сосед не тронут"
        if legacy is None and not _has_parallel_edge(main_points, neighbour_points):
            note += "; сосед повёрнут относительно нашей межи — одним переносом общую межу не получить, нужен поворот"
        return SlideResult(
            status="not_found", overlap_before_sqft=before, note=note, tried=tried, rejected=rejected,
            corner_note=corner_note,
        )
    (dx, dy, band, need, wedge), method = found
    result = _moved(main, neighbour, dx, dy, band, before, wedge, method)
    return replace(result, corner_note=corner_note if method != "corner" else "")


def _corner_diagnosis(corners, kept, corner_limit, check_reason, away, allow_inward) -> str:
    """
    Для журнала: почему угловые сдвиги не приняты (ближайший по длине). Не влияет на выбор.
    """
    if not corners:
        return "углы: совмещение не строится (стороны не параллельны в пределах 2°)"
    parts = []
    for dx, dy, band, need, _wedge in corners[:2]:
        length = math.hypot(dx, dy)
        if not allow_inward and dx * away[0] + dy * away[1] < -1e-9:
            why = "не наружу"
        else:
            why = check_reason(dx, dy, band, need) or "проходит"
        if length > corner_limit + 1e-9:
            why = f"длиннее допустимого для углов ({corner_limit:.2f} ft); без этого ограничения: {why}"
        parts.append(f"сдвиг ({dx:.2f}, {dy:.2f}), длина {length:.2f} ft — {why}")
    return "углы: " + "; ".join(parts)


def _moved(main: Polygon, neighbour: Polygon, dx: float, dy: float, band: float,
           before: float, wedge: float, method: str) -> SlideResult:
    s = math.hypot(dx, dy)
    direction = math.degrees(math.atan2(dy, dx)) % 360.0 if s > 0 else 0.0
    shared = _shared_length_banded(main, affinity.translate(neighbour, dx, dy), band)
    return SlideResult(
        status="moved",
        dx=dx,
        dy=dy,
        distance_ft=s,
        direction_deg=direction,
        shared_ft=shared,
        overlap_before_sqft=before,
        wedge_ft=wedge,
        method=method,
    )
