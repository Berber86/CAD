"""Склейка на схеме, не правка deed: только пара узлов и жёсткий перенос соседа."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, replace
from typing import Callable, Sequence

from acad_builder import DrawPlan, street_name_canon, translate_plan, verify_with_neighbors
from pdf_set import CLOSURE_LIMIT_FT

# Допуски проверки стыка в футах и площади численного пересечения в кв. футах.
JOINT_TOL_FT = 0.02
OVERLAP_TOL_SQFT = 0.01


@dataclass(frozen=True)
class Joint:
    """Номера с единицы, как в отчёте: V1 = POB, Vi = начало стороны #i."""

    vertex: int
    side: int | None = None

    def point(self, plan: DrawPlan) -> tuple[float, float]:
        if type(self.vertex) is not int or not 1 <= self.vertex <= len(plan.vertices):
            raise ValueError(f"нет вершины V{self.vertex}")
        return plan.vertices[self.vertex - 1]

    def along_side(self, plan: DrawPlan) -> tuple[float, float, float, int]:
        """Единичный вектор от выбранного конца вдоль отрезка, длина и второй угол."""
        n = len(plan.boundary_lines)
        if type(self.side) is not int or not 1 <= self.side <= n:
            raise ValueError(f"нет стороны #{self.side}")
        line = plan.boundary_lines[self.side - 1]
        next_v = self.side % n + 1
        if self.vertex == self.side:
            start, end, other_v = line.start, line.end, next_v
        elif self.vertex == next_v:
            start, end, other_v = line.end, line.start, self.side
        else:
            raise ValueError(f"V{self.vertex} не является концом стороны #{self.side}")
        if (math.dist(start, self.point(plan)) > JOINT_TOL_FT
                or math.dist(end, plan.vertices[other_v - 1]) > JOINT_TOL_FT):
            raise ValueError(f"у стороны #{self.side} открытый гэп в углу")
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = math.hypot(dx, dy)
        if length <= JOINT_TOL_FT:
            raise ValueError(f"сторона #{self.side} слишком коротка для склейки")
        return dx / length, dy / length, length, other_v


@dataclass(frozen=True)
class Stitch:
    anchor_key: str
    moving: Joint
    fixed: Joint
    dx: float
    dy: float
    contact: str
    #: Принятый сдвиг относится к конкретным описаниям, ролям и их правкам.
    basis: str

    def place(self, plan: DrawPlan) -> DrawPlan:
        return replace(translate_plan(plan, self.dx, self.dy), placement_note=(
            f"Условное размещение соседа: V{self.moving.vertex} → V{self.fixed.vertex} опорного участка; "
            f"{self.vector_label()}; {self.contact}. Исходная привязка deed не изменена."
        ))

    def vector_label(self) -> str:
        return f"ΔX {self.dx:+.3f} ft, ΔY {self.dy:+.3f} ft"


def _check_clear(plan: DrawPlan, others: Sequence[tuple[str, DrawPlan]]) -> None:
    """Не прячем новый заступ на третью фигуру за уменьшением суммарной площади."""
    from shapely.geometry import Polygon

    poly = Polygon(plan.vertices)
    if not poly.is_valid or poly.area <= OVERLAP_TOL_SQFT:
        raise ValueError("контур соседа непригоден для проверки наложения")
    for name, other in others:
        other_poly = Polygon(other.vertices)
        if not other_poly.is_valid or other_poly.area <= OVERLAP_TOL_SQFT:
            raise ValueError(f"не удалось проверить контур «{name}»")
        area = poly.intersection(other_poly).area
        if area > OVERLAP_TOL_SQFT:
            raise ValueError(f"после сдвига наложение на «{name}» {area:.2f} sq ft")


def _street_frontages(plan: DrawPlan, canon: Callable[[str | None], str]):
    """Именно стороны фасадов, не просто упоминание улицы в привязке POB."""
    desc = plan.description
    for name, index in ((desc.main_street, desc.front_segment_index),
                        (desc.cross_street, desc.cross_street_side_index)):
        if name and index is not None and 0 <= index < len(plan.boundary_lines):
            line = plan.boundary_lines[index]
            length = math.dist(line.start, line.end)
            if length > JOINT_TOL_FT:
                yield (canon(name), name, line.start, length,
                       (line.end[0] - line.start[0]) / length, (line.end[1] - line.start[1]) / length)


def _check_street_ends(placed: DrawPlan, anchor: DrawPlan, canon: Callable[[str | None], str]) -> None:
    # У короткой стены можно геометрически совместить и два уличных, и два
    # задних конца. Во втором случае фасад уедет с линии той же улицы.
    for street, name, p, length, ux, uy in _street_frontages(placed, canon):
        for other, _, q, ref_length, vx, vy in _street_frontages(anchor, canon):
            if street != other or min(length, ref_length) * abs(ux * vy - uy * vx) > JOINT_TOL_FT:
                continue  # линия улицы может поворачивать
            if (ux * vx + uy * vy > 0) != (placed.ccw == anchor.ccw):
                raise ValueError(f"фасады выходят на разные стороны улицы «{name}»")
            offset = abs((p[0] - q[0]) * vy - (p[1] - q[1]) * vx)
            if offset > JOINT_TOL_FT:
                raise ValueError(f"после сдвига линии фасадов «{name}» разнесены на {offset:.2f} ft; "
                                 "проверьте, какие концы выходят на улицу")


def try_stitch(
    neighbor: DrawPlan, anchor: DrawPlan, moving: Joint, fixed: Joint, *,
    anchor_key: str, others: Sequence[tuple[str, DrawPlan]],
) -> Stitch:
    """Испытывает предложенную пару, никогда не подбирает ближайший угол вместо неё."""
    if not neighbor.is_neighbor or anchor.is_neighbor:
        raise ValueError("сдвигать можно только соседа; опорная фигура — наш участок")
    if max(neighbor.closure.distance, anchor.closure.distance) > CLOSURE_LIMIT_FT:
        raise ValueError("сначала проверьте невязку контура")
    x, y = moving.point(neighbor)
    ax, ay = fixed.point(anchor)
    dx, dy = ax - x, ay - y
    if math.hypot(dx, dy) <= JOINT_TOL_FT:
        raise ValueError("указанные углы уже совпадают в пределах допуска")

    dn, da = neighbor.description, anchor.description
    canon = street_name_canon(dn, da)
    corner = {canon(dn.main_street), canon(dn.cross_street)}
    has_tie = dn.distance_to_corner_ft > 0 or dn.cross_street_side_index is not None
    if (has_tie and "" not in corner and len(corner) == 2
            and corner == {canon(da.main_street), canon(da.cross_street)}):
        raise ValueError("у соседа есть привязка от того же перекрёстка; проверьте её и состав "
                         "участка по deed, не убирайте разрыв сдвигом")

    contact = "проверена только пара углов; общая сторона не установлена"
    if moving.side is not None or fixed.side is not None:
        ux, uy, length, end = moving.along_side(neighbor)
        vx, vy, ref_length, ref_end = fixed.along_side(anchor)
        if (moving.side - 1 in (dn.front_segment_index, dn.cross_street_side_index)
                or fixed.side - 1 in (da.front_segment_index, da.cross_street_side_index)):
            raise ValueError("указанная сторона выходит на улицу — фасад не является межой двух лотов")
        span = min(length, ref_length)
        # Оба вектора идут ОТ совмещаемых углов. Работает и при обратном обходе;
        # просто параллельные стороны, расходящиеся от угла, общей полосы не дают.
        if span * math.hypot(ux - vx, uy - vy) > JOINT_TOL_FT:
            raise ValueError("выбранные стороны не ложатся одна на другую от указанных углов без поворота")
        contact = f"общий отрезок {span:.2f} ft"
        if abs(length - ref_length) <= JOINT_TOL_FT:
            contact += f"; второй конец: V{end} соседа → V{ref_end} нашего участка"
        else:
            inside = (f"V{end} соседа лежит на нашей стороне #{fixed.side}" if length < ref_length
                      else f"наш V{ref_end} лежит на стороне #{moving.side} соседа")
            contact += f"; частичная межа: {inside}, до дальнего угла остаётся {abs(length - ref_length):.2f} ft"
    basis = hashlib.sha256((dn.model_dump_json() + da.model_dump_json()).encode()).hexdigest()
    stitch = Stitch(anchor_key, moving, fixed, dx, dy, contact, basis)
    placed = stitch.place(neighbor)
    _check_street_ends(placed, anchor, canon)
    _check_clear(placed, [("опорный участок", anchor), *others])
    # Нулевое новое наложение ещё не успех, если ради этого оторвана другая
    # уже совпадавшая межа. Такой набор нельзя размещать по одному соседу.
    for name, other in others:
        prior = verify_with_neighbors(neighbor, [other]).pair_reports[0]
        if prior.status == "ok" and prior.shared_edges:
            after = verify_with_neighbors(placed, [other]).pair_reports[0]
            matched = {(e.main_seg_index, e.neighbor_seg_index) for e in after.shared_edges if e.status == "ok"}
            if any((e.main_seg_index, e.neighbor_seg_index) not in matched for e in prior.shared_edges):
                raise ValueError(f"сдвиг разрывает уже совпадающую межу с «{name}»")
    return stitch
