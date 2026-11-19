"""
Шаг 2 — автоматическое распознавание и подписание объектов (Feature Labeling).

Тесты не требуют AutoCAD: распознавание и раскладка подписей — чистая
геометрия (shapely), а COM-часть проверяется на заглушке ModelSpace.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest
from shapely.geometry import LineString
from shapely.geometry import box as shapely_box

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import acad_builder as ab  # noqa: E402
import feature_labeler as fl  # noqa: E402
from acad_builder import AcadBuilder, AcadBuilderError  # noqa: E402
from feature_labeler import DrawingLine, DrawingSnapshot, DrawingText  # noqa: E402
from llm_parser import load_config  # noqa: E402

HOUSE = "2 STY. BRK. & FR."


# ------------------------------------------------------- синтетический чертёж

def build_scene(angle_deg: float = 0.0, gap: float = 0.0, with_existing: bool = False):
    """
    Участок 40 x 100 ft «как от полевого чертёжника»: граница отдельными
    отрезками, соседние tax-линии, дом, сарай, терраса, крыльца, ступени с
    проступями, дорожки, заборы (CODE_CREW с кодами точек и FENCE_* по слоям).

    ``angle_deg`` поворачивает весь чертёж, ``gap`` оставляет разрыв в углу
    границы, ``with_existing`` добавляет уже стоящие в слое D-TOPO-TEXT подписи.
    """
    a = math.radians(angle_deg)

    def rot(p):
        x, y = p
        return (x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a))

    snap = DrawingSnapshot()

    def line(layer, p, q):
        snap.lines.append(DrawingLine(layer, (rot(p), rot(q))))

    def pline(layer, pts, closed=False):
        snap.lines.append(
            DrawingLine(layer, tuple(rot(p) for p in pts), closed=closed, kind="AcDbPolyline")
        )

    def rect(layer, x0, y0, x1, y1, as_lines=False):
        pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        if as_lines:
            for i in range(4):
                line(layer, pts[i], pts[(i + 1) % 4])
        else:
            pline(layer, pts, closed=True)

    def text(layer, value, p, h=0.9, rotation=0.0, kind="AcDbText"):
        snap.texts.append(DrawingText(layer, value, rot(p), height=h, rotation=rotation + a, kind=kind))

    # Граница Шага 1: отдельные отрезки, возможен разрыв замыкания.
    line("PROPERTY_LINE", (0, 0), (40, 0))
    line("PROPERTY_LINE", (40, 0), (40, 100 - gap))
    line("PROPERTY_LINE", (40, 100), (0, 100))
    line("PROPERTY_LINE", (0, 100), (0, 0))
    for x0 in (-40, 40):  # tax-линии соседей
        rect("M-TAX-LINE", x0, 0, x0 + 40, 100, as_lines=True)

    rect("BUILDING_OUTLINE", 12, 30, 34, 62)
    rect("REF_VEC_BUILDING", 48, 30, 72, 62)
    rect("SUBJECT_SHED_OUTLINE", 28, 86, 38, 96, as_lines=True)
    rect("NEIGHBOUR_SHED_OUTLINE", 44, 88, 52, 96)
    pline("REAR_DECK_EDGE", [(14, 62), (14, 72), (28, 72), (28, 62)])
    rect("SIDE_PORCH", 34, 40, 39, 50)
    rect("FRONT_LANDING", 18, 26, 26, 30)
    rect("FRONT_STEPS", 19.5, 22, 24.5, 26)
    for y in (23, 24, 25):
        line("FRONT_STEP_TREADS", (19.5, y), (24.5, y))
    rect("REAR_STEPS", 28, 64, 31, 68)
    line("DRIVEWAY_EDGE", (1.5, 0), (1.5, 50))
    line("DRIVEWAY_EDGE", (10.5, 0), (10.5, 50))
    line("SIDEWALK_EDGE", (-60, -4), (100, -4))
    line("SIDEWALK_EDGE", (-60, -9), (100, -9))
    line("WALK_EDGE", (20, 0), (20, 22))
    line("WALK_EDGE", (24, 0), (24, 22))
    rect("PLANTER_EDGE", 27, 26.5, 33.5, 29.5)
    line("GRASS_STRIP_EDGE", (-60, -10.5), (100, -10.5))

    # Заборы: CODE_CREW (материал задают коды точек) и слои FENCE_*/LOW_EDGE_*.
    line("CODE_CREW", (0.3, 2), (0.3, 35))
    line("CODE_CREW", (0.3, 35), (0.3, 70))
    line("CODE_CREW", (0.3, 70), (0.3, 99.6))
    line("CODE_CREW", (0.3, 99.6), (20, 99.6))
    line("CODE_CREW", (20, 99.6), (39.5, 99.6))
    line("FENCE_WOOD_RIGHT", (39.8, 5), (39.8, 95))
    line("FENCE_CHAIN_FRONT", (30, 0.5), (36, 0.5))
    line("GATE_FRONT", (36, 0.5), (39.8, 0.5))
    line("LOW_EDGE_TIMBER", (1.5, 50), (10.5, 50))
    line("ST-WALL", (45, 10), (45, 25))

    # Коды геодезических точек.
    text("POINT_LABELS", "1 TTP", (-3, -12))
    text("POINT_LABELS", "2 FEC", (0.8, 2.3))
    text("POINT_LABELS", "3 FEC", (0.8, 50.2))
    text("POINT_LABELS", "4 FEC", (0.8, 99.9))
    text("POINT_LABELS", "5 FEV64", (20.3, 100.1))
    text("PNT-X", "6 FEV64", (39.9, 100.0))
    text("PNTS", "7 MH", (20, -15))
    text("POINT_LABELS", "8 XYZ", (5, 5))
    text("POINT_LABELS", "9 HYD", (300, 300))  # далеко за буфером 50 ft

    # Подпись стороны из Шага 1 — препятствие для новых текстов.
    text("BOUNDARY_TEXT_AI", "N 00%%d00' E   100.00'", (-2.05, 35), rotation=math.pi / 2)
    if with_existing:
        # Готовые подписи заказчика (итоговый слой): повторно не ставятся.
        text("D-TOPO-TEXT", "CHAIN LINK FENCE", (2, 60), rotation=math.pi / 2)
        text("D-TOPO-TEXT", "SHED", (31, 90.5))
        text("D-TOPO-TEXT", "CONC. WALK", (40, -7))
    return snap


STAFFORD_HOUSE = "1 STY\nFRAME\n(BRICK FACE)\nW/BSMNT\nNo. 87"


def build_stafford_scene(angle_deg: float = 0.0, note_height: float = 1.3):
    a = math.radians(angle_deg)

    def rot(p):
        x, y = p
        return (x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a))

    snap = DrawingSnapshot()

    def line(layer, *pts, closed=False):
        snap.lines.append(
            DrawingLine(layer, tuple(rot(p) for p in pts), closed=closed, kind="AcDbPolyline")
        )

    def rect(layer, x0, y0, x1, y1):
        line(layer, (x0, y0), (x1, y0), (x1, y1), (x0, y1), closed=True)

    def text(layer, value, p, h=0.5):
        snap.texts.append(DrawingText(layer, value, rot(p), height=h, rotation=a))

    def note(layer, value, p, tip=None, h=note_height):
        """Заметка полевика: текст (левый нижний угол в ``p``) и выноска к ``tip``."""
        snap.texts.append(DrawingText(layer, value, rot(p), height=h, rotation=a))
        if tip is not None:
            width = 0.75 * h * len(value)
            start = (p[0] - 0.5, p[1] + h / 2) if tip[0] < p[0] else (p[0] + width + 0.5, p[1] + h / 2)
            line(layer, start, tip)

    # Участки: свой 40 x 100 и соседи (все — замкнутые контуры одного слоя).
    for x0 in (-40, 0, 40, 80):
        rect("PROPERTY_LINES_APPROX", x0, 0, x0 + 40, 100)

    # Строения.
    rect("BUILDING_OUTLINE", 4.9, 21.6, 31.9, 69.0)
    rect("BUILDING_OUTLINE", 45.4, 20.0, 70.0, 68.75)  # соседний дом
    rect("SUBJECT_SHED_OUTLINE", 30.2, 89.3, 38.5, 99.3)
    line("REAR_DECK_EDGE", (5, 69), (5, 80), (8.5, 83.4), (24.4, 83.4), (24.4, 76.1),
         (21.4, 76.1), (21.4, 69))
    rect("REAR_STEPS", 21.4, 73.0, 25.0, 76.1)
    for x in (22.3, 23.2, 24.1):
        line("REAR_STEP_TREADS", (x, 73.0), (x, 76.1))
    rect("SIDE_PLANTER_5", 2.25, 54.0, 4.9, 69.0)
    # Боковое крыльцо: площадка и ступени в одном контуре, у ступеней своего
    # контура нет — только линии проступей.
    rect("SIDE_PORCH", 1.25, 27.0, 4.9, 34.0)
    for y in (28.0, 29.0, 30.0):
        line("SIDE_STEP_TREADS", (1.25, y), (4.9, y))
    # Парадные ступени: контур с проступями.
    rect("FRONT_STEPS", 14.0, 15.8, 19.0, 21.6)
    for y in (17.0, 18.2, 19.4, 20.6):
        line("FRONT_STEP_TREADS", (14.0, y), (19.0, y))
    rect("NEIGHBOUR_SIDE_PORCH", 42.25, 30.0, 45.4, 35.0)
    rect("NEIGHBOUR_SIDE_STAIR", 42.25, 26.5, 45.4, 30.0)
    for y in (27.7, 28.9):
        line("NEIGHBOUR_STAIR_TREADS", (42.25, y), (45.4, y))
    line("LOW_EDGE_BRACKET", (24.4, 83.4), (30.2, 83.4), (30.2, 89.3))
    line("SIDEWALK_EDGE", (-20.0, -6.0), (60.0, -6.0))
    # Линии между снятыми точками без кодов заборов (углы дома и сарая).
    line("CODE_CREW", (4.9, 69.0), (31.9, 69.0))
    line("CODE_CREW", (30.2, 89.3), (38.5, 89.3))

    # Заборы (на 0.3 ft от границы) и калитки.
    line("FENCE_CHAIN_LEFT", (0.3, 54.0), (0.3, 99.7))
    line("FENCE_CHAIN_REAR", (0.3, 99.7), (39.5, 99.7))
    line("FENCE_VINYL_REAR_MEASURED", (14.5, 100.5), (38.1, 100.9))
    line("FENCE_CHAIN_RIGHT", (39.7, 47.6), (39.7, 99.7))
    # Возвраты и продолжение соседских заборов за задней границей.
    line("FENCE_CHAIN_9_NORTH", (14.0, 100.3), (13.6, 110.3))
    line("FENCE_VINYL_REAR_RETURN", (14.6, 100.5), (14.2, 110.5))
    line("FENCE_VINYL_16_EXTENSION", (40.5, 101.0), (48.5, 101.2))
    line("GATE_LEFT_INSERT", (0.3, 54.0), (4.9, 54.0))
    line("GATE_LEFT_SECOND", (-5.5, 54.0), (0.3, 54.0))
    # Линия калитки тянется за границу участка до стены соседнего дома.
    line("GATE_RIGHT_INSERT", (31.9, 47.6), (45.4, 47.6))
    line("GATE_RIGHT_CHAIN", (39.7, 68.75), (45.4, 68.75))

    # Коды точек.
    codes = [
        ("1 FEC", (0.3, 99.7)), ("2 FEC", (0.3, 75)), ("3 FEV64", (0.3, 54.0)),
        ("4 FEC", (15, 99.7)), ("5 FEV", (14.6, 100.5)), ("6 FEV", (44, 101.1)),
        ("7 FEC296", (39.7, 68.75)), ("8 FEC", (39.7, 90)), ("9 TTP", (3, 96)),
        ("10 BLC", (4.9, 69)), ("11 BLC1", (31.9, 69)), ("12 OP", (5, 80)),
        ("13 OP", (24.4, 83.4)), ("14 STEP", (21.4, 73)), ("15 SHED", (30.2, 89.3)),
        ("16 PLAN", (2.25, 54)), ("17 SW3", (2, 2)), ("18 LST", (30.2, 83.4)),
        ("19 TTP", (34.3, 80.2)),
    ]
    for value, p in codes:
        text("POINT_LABELS", value, (p[0] + 0.2, p[1] + 0.2))

    # Заметки полевого чертёжника.
    note("CONTEXT_TEXT", "GROUND", (21.0, 92.0), h=1.8)
    note("CONTEXT_TEXT", "SHED", (31.5, 93.5), h=1.8)
    note("CONTEXT_SURFACE_TEXT", "GRASS", (42.5, 85.0), h=1.8)
    note("LOW_EDGE_NOTE", "LOW TIMBER EDGE", (19.0, 85.5), tip=(27.0, 83.4), h=0.8)
    note("ENTRANCE_TEXT", "WOOD PORCH", (10.0, 76.5))
    note("ENTRANCE_TEXT", "4 WOOD STEPS", (9.0, 72.5), tip=(22.0, 74.5))
    note("SURFACE_TEXT", "PAVERS", (41.5, 79.5), tip=(34.3, 80.2))
    note("FENCE_NOTE", "CHAIN LINK FENCE", (42.5, 74.6), tip=(39.7, 75.2))
    note("FENCE_NOTE", "CHAIN LINK GATE", (46.0, 66.5), tip=(41.0, 68.75))
    note("FENCE_NOTE", "CHAIN LINK FENCE", (-17.0, 71.0), tip=(0.3, 70.4))
    note("SURFACE_TEXT", "PAVERS", (-7.5, 62.3), tip=(1.2, 62.9))
    note("SURFACE_TEXT", "PLANTER", (7.0, 62.0), tip=(4.0, 62.3))
    note("FENCE_NOTE", "VINYL GATE 7 FEET HIGH", (-26.0, 52.5), tip=(-2.5, 54.0))
    note("FENCE_NOTE", "VINYL GATE 7 FEET HIGH", (-20.0, 50.5), tip=(2.5, 54.0))
    note("FENCE_NOTE", "VINYL GATE", (41.0, 48.5), tip=(36.0, 47.6))
    note("CONTEXT_TEXT", "HOUSE", (14.0, 45.0), h=1.8)
    note("CONTEXT_TEXT", "NEIGHBOUR HOUSE", (49.0, 45.5), h=1.6)
    note("ENTRANCE_TEXT", "SIDE BRICK PORCH & 3 TREADS", (-24.0, 31.5), tip=(1.25, 32.3))
    note("ENTRANCE_TEXT", "BRICK PORCH & 3 TREADS", (47.0, 34.0), tip=(45.4, 34.5))
    note("ENTRANCE_TEXT", "5 BRICK STEPS", (22.0, 13.0), tip=(18.0, 17.5))
    note("FENCE_NOTE", "CHAIN LINK FENCE 6 FEET HIGH", (-2.0, 104.0), tip=(8.0, 99.7))
    note("FENCE_NOTE", "VINYL FENCE 7 FEET HIGH", (20.0, 106.0), tip=(28.0, 100.7))
    note("FENCE_NOTE", "CHAIN LINK FENCE", (2.0, 108.0), tip=(13.8, 105.0))
    note("SURFACE_TEXT", "CONCRETE SIDEWALK", (8.0, -15.0))
    return snap


def recognize(snap, house=HOUSE, cfg=None):
    boundary = fl.find_property_polygon(snap, cfg or {})
    return fl.recognize_features(snap, boundary, cfg or {}, house)


def labels_of(result, layer):
    return sorted(str(f.label) for f in result.features if f.layer == layer)


# ------------------------------------- масштаб типа линий заборов (3.6)

def test_fence_linetype_scale_follows_the_customer_rule():
    """Правило заказчика: короткий отрезок — по длине, 8–10 ft — половина, остальное — 5."""
    fcfg = fl.feature_settings({})
    fence = fl.fence_linetype_rule("S-FENCE", fcfg)
    rail = fl.fence_linetype_rule("s-fence-rail", fcfg)
    assert (fence["fixed"], fence["divisor"]) == (5.0, 1.0)
    assert (rail["fixed"], rail["divisor"]) == (0.62, 8.06)

    def value(length, rule=fence, **kwargs):
        return fl.fence_linetype_scale(length, rule, fcfg, base_scale=16.0, **kwargs)[0]

    # короче 5 ft — масштаб равен длине, вниз до сотой (иначе крестика не видно)
    assert value(4.999) == 4.99
    assert value(4.5) == 4.5
    assert value(0.5) == 0.5
    # от 8 до 10 ft — половина длины (при масштабе 5 видно одно перекрестие, просятся два)
    assert value(9.15) == 4.57  # 9.15 / 2 = 4.575, вниз
    assert value(8.0) == 4.0
    assert value(10.0) == 5.0
    # остальные — масштаб слоя по умолчанию, 5
    assert value(5.0) == 5.0  # ровно 5 — уже не «меньше 5»
    assert value(7.99) == 5.0
    assert value(10.01) == 5.0
    assert value(40.0) == 5.0
    # S-FENCE-RAIL: те же условия, но результат делён на 8.06 и тоже вниз до сотой
    assert value(12.0, rail) == 0.62
    assert value(4.5, rail) == 0.55  # 4.5 / 8.06 = 0.5583 → 0.55
    assert value(9.15, rail) == 0.56  # 4.57 / 8.06 = 0.567 → 0.56
    assert value(8.0, rail) == 0.49  # 4.0 / 8.06 = 0.4963 → 0.49
    # другой масштаб чертежа: всё умножено на scale / 16 (как высоты текста)
    assert value(4.999, scale=20) == 6.2375  # 4.99 × 1.25
    assert value(12.0, rail, scale=20) == 0.775  # 0.62 × 1.25
    # чужой слой правилом не считается: подписи и граница не трогаются
    assert fl.fence_linetype_rule("FENCE", fcfg) is None
    assert fl.fence_linetype_rule("PROPERTY_LINE", fcfg) is None
    # шаблон в правилах тоже работает, если его вписали в config.json
    wild = fl.feature_settings({"feature_labeling": {"linetype_scale_rules": {"S-FENCE*": {"fixed": 4.0}}}})
    assert fl.fence_linetype_rule("S-FENCE-RAIL", wild)["fixed"] == 4.0


def test_fence_linetype_plan_groups_segments_by_layer_and_reason():
    plan = fl.fence_linetype_plan(
        [
            ("S-FENCE", 4.5), ("S-FENCE", 9.15), ("S-FENCE", 30.0), ("S-FENCE", 6.0),
            ("S-FENCE-RAIL", 4.0), ("S-FENCE-RAIL", 12.0),
            ("PROPERTY_LINE", 4.5),  # не забор — правила для слоя нет
        ],
        {},
    )
    assert (plan.count("S-FENCE", "short"), plan.count("S-FENCE", "halved"),
            plan.count("S-FENCE", "fixed")) == (1, 1, 2)
    assert (plan.count("S-FENCE-RAIL", "short"), plan.count("S-FENCE-RAIL", "fixed")) == (1, 1)
    assert plan.changed == 6
    text = "\n".join(plan.lines())
    assert "МАСШТАБ ТИПА ЛИНИЙ ЗАБОРОВ" in text
    assert "слой S-FENCE: изменён у 4 отрезков" in text
    assert "короче 5 ft: 1 (4.5' → 4.5)" in text
    assert "8–10 ft: 1 (9.15' → 4.57)" in text
    assert "слой S-FENCE-RAIL: изменён у 2 отрезков" in text
    assert "будет изменён" in "\n".join(plan.lines(dry_run=True))


def test_fence_linetype_scale_is_written_into_autocad(com):
    snap = fl.DrawingSnapshot(lines=[
        fl.DrawingLine("S-FENCE", ((0.0, 0.0), (4.5, 0.0))),
        fl.DrawingLine("S-FENCE", ((0.0, 10.0), (9.15, 10.0))),
        fl.DrawingLine("S-FENCE", ((0.0, 20.0), (30.0, 20.0))),
        fl.DrawingLine("S-FENCE-RAIL", ((0.0, 30.0), (12.0, 30.0))),
        fl.DrawingLine("S-FENCE-RAIL", ((0.0, 34.0), (4.0, 34.0))),
        fl.DrawingLine("PROPERTY_LINE", ((0.0, 40.0), (3.0, 40.0))),
    ])
    builder, _app, doc = com(snap)
    plan = builder.apply_fence_linetype_scales(doc)
    values = {round(entity.LinetypeScale, 4) for entity in doc.ModelSpace.entities
              if entity.Layer == "S-FENCE"}
    assert values == {4.5, 4.57, 5.0}
    rail = sorted(round(entity.LinetypeScale, 4) for entity in doc.ModelSpace.entities
                  if entity.Layer == "S-FENCE-RAIL")
    assert rail == [0.49, 0.62]
    assert not hasattr(next(
        entity for entity in doc.ModelSpace.entities if entity.Layer == "PROPERTY_LINE"
    ), "LinetypeScale")  # чужой слой не тронут
    assert plan.changed == 5 and plan.failed == []

    # Второй прогон ничего не меняет: масштабы уже стоят нужные.
    again = builder.apply_fence_linetype_scales(doc)
    assert again.changed == 0 and again.unchanged == 5


def test_fence_linetype_scale_changes_with_the_drawing_scale(com):
    snap = fl.DrawingSnapshot(lines=[fl.DrawingLine("S-FENCE", ((0.0, 0.0), (4.5, 0.0)))])
    builder, _app, doc = com(snap)
    plan = builder.apply_fence_linetype_scales(doc, scale=20.0)
    assert plan.factor == pytest.approx(1.25)
    assert doc.ModelSpace.entities[0].LinetypeScale == pytest.approx(5.625)  # 4.5 × 1.25
    assert "рисунок ×1.25" in "\n".join(plan.lines())


def test_fence_linetype_scale_reports_a_locked_layer(com):
    snap = fl.DrawingSnapshot(lines=[fl.DrawingLine("S-FENCE", ((0.0, 0.0), (4.5, 0.0)))])

    class Locked(FakeEntity):
        def __setattr__(self, name, value):
            if name == "LinetypeScale":
                raise RuntimeError("слой заблокирован")
            super().__setattr__(name, value)

    builder, _app, doc = com(snap)
    doc.ModelSpace.entities[0].__class__ = Locked
    plan = builder.apply_fence_linetype_scales(doc)
    assert plan.changed == 0 and len(plan.failed) == 1
    assert "слой S-FENCE, отрезок 4.50 ft" in plan.failed[0]
    assert "ОШИБКА" in "\n".join(plan.lines())


def test_step2_fixes_fence_linetype_scale_and_says_so(com):
    """Шаг 2 правит масштаб типа линий заборов вместе с подписями (и в пробном расчёте)."""
    builder, app, doc = com(load_ilion())
    fences = [entity for entity in doc.ModelSpace.entities
              if entity.Layer == "S-FENCE" and entity.ObjectName == "AcDbLine"]
    assert fences, "в чертеже Ilion есть линии забора S-FENCE"
    probe = builder.label_features(house_text=ILION_HOUSE, scale=16.0, dry_run=True)
    assert probe.linetype is not None and probe.linetype.changed == len(fences)
    assert all(not hasattr(entity, "LinetypeScale") for entity in fences)  # пробный расчёт не меняет
    assert "будет изменён" in probe.report()

    result = builder.label_features(house_text=ILION_HOUSE, scale=16.0)
    assert result.failed == []
    assert result.linetype is not None and result.linetype.changed == len(fences)
    assert all(hasattr(entity, "LinetypeScale") for entity in fences)
    assert "МАСШТАБ ТИПА ЛИНИЙ ЗАБОРОВ" in result.report()
    assert "Масштаб типа линий заборов — S-FENCE:" in result.summary()


# ----------------------------------------------------------------- настройки

def test_shipped_config_mirrors_feature_defaults():
    """config.json содержит раздел feature_labeling, совпадающий с умолчаниями модуля."""
    shipped = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    section = shipped["feature_labeling"]
    assert section == fl.DEFAULT_FEATURE_CONFIG
    # Ключи из постановки задачи.
    assert section["property_line_layers"] == [
        "PROPERTY_LINE", "PL", "M-PROPERTY", "M-TAX-LINE", "PROPERTY_LINES_APPROX",
    ]
    assert section["point_label_layers"] == ["POINT_LABELS", "PNT-X", "PNTS"]
    assert section["label_output_layer"] == "D-TOPO-TEXT"
    assert section["point_to_fence_max_ft"] == 3.5
    assert section["duplicate_radius_ft"] == 6.0
    assert section["point_code_dictionary"]["FEV*"] == "VINYL FENCE"
    # Формулировки заказчика: терраса — FRAME DECK в две строки, крыльцо — O.P.
    assert section["layer_label_mapping"]["*DECK*"] == "FRAME\nDECK"
    assert section["layer_label_mapping"]["*PORCH*"] == "O.P."
    assert set(section["structure_layers"]) == {
        "house", "shed", "porch", "steps", "planter", "surface",
        "pool", "wall", "overhang", "misc",  # слои шаблона геодезиста
    }
    assert section["existing_label_layers"] == ["D-TOPO-TEXT"]
    assert section["field_note_layers"] == [
        "FENCE_NOTE", "ENTRANCE_TEXT", "SURFACE_TEXT", "CONTEXT_TEXT",
        "CONTEXT_SURFACE_TEXT", "LOW_EDGE_NOTE", "*_NOTE", "*_TEXT",
    ]
    assert section["step_tread_layers"] == ["*TREADS*"]
    assert section["code_linework_layers"] == ["CODE_CREW"]
    assert section["fence_text_offset_factor"] == 1.1
    assert section["label_short_fences_outside_lot"] is True
    assert section["label_point_features"] is False
    # load_config() доносит раздел до AcadBuilder без изменений.
    assert fl.feature_settings(load_config()) == fl.DEFAULT_FEATURE_CONFIG


def test_feature_settings_override_replaces_key_and_null_keeps_default():
    cfg = {"feature_labeling": {"point_to_fence_max_ft": 5.0, "search_buffer_ft": None,
                                "layer_label_mapping": {"MY_SHED": "SHED"}}}
    settings = fl.feature_settings(cfg)
    assert settings["point_to_fence_max_ft"] == 5.0
    assert settings["search_buffer_ft"] == 50.0
    assert settings["layer_label_mapping"] == {"MY_SHED": "SHED"}
    assert fl.DEFAULT_FEATURE_CONFIG["layer_label_mapping"]["*PORCH*"] == "O.P."


def test_layer_classifier_fence_patterns_and_note_exclusion():
    classifier = fl.LayerClassifier(fl.feature_settings({}))
    for name in ("S-FENCE", "CODE_CREW", "FENCE_CHAIN_LEFT", "GATE_REAR", "gate_left_insert"):
        assert classifier.is_fence(name), name
    # Стена — не забор: это объект со своим контуром (двойная линия со штриховкой).
    assert not classifier.is_fence("ST-WALL")
    assert classifier.structure_category("ST-WALL") == "wall"
    for name in ("FENCE_NOTE", "GATE_NOTE", "BUILDING_OUTLINE", "POINT_LABELS", "LOW_EDGE_TIMBER"):
        assert not classifier.is_fence(name), name
    assert classifier.is_gate("GATE_LEFT_SECOND") and not classifier.is_gate("FENCE_CHAIN_LEFT")
    assert classifier.is_point_label("pnt-x")
    # Заметки полевика — источник подписей, а не готовые подписи.
    assert classifier.is_field_note("FENCE_NOTE") and classifier.is_field_note("context_text")
    assert not classifier.is_existing_label("FENCE_NOTE")
    assert classifier.is_existing_label("D-TOPO-TEXT") and not classifier.is_field_note("D-TOPO-TEXT")
    assert classifier.structure_category("NEIGHBOUR_SIDE_STAIR") == "steps"
    assert classifier.structure_category("NEIGHBOUR_SHED_OUTLINE") == "shed"
    assert classifier.structure_category("SIDE_PLANTER_5") == "planter"


@pytest.mark.parametrize(
    ("raw", "number", "code"),
    [("1 TTP", "1", "TTP"), ("2 FEC", "2", "FEC"), ("3 FEV64", "3", "FEV64"), ("  17   few ", "17", "FEW")],
)
def test_parse_point_code(raw, number, code):
    parsed = fl.parse_point_code(raw)
    assert parsed is not None
    assert (parsed[0], parsed[1]) == (number, code)


def test_parse_point_code_without_number_and_without_code():
    assert fl.parse_point_code("TTP") == ("", "TTP")  # номер точки необязателен
    assert fl.parse_point_code("12 FEC 6FT") == ("12", "FEC")  # хвост описания отбрасывается
    assert fl.parse_point_code("123") is None
    assert fl.parse_point_code("") is None


def test_labels_similar_handles_abbreviations_and_synonyms():
    synonyms = fl.DEFAULT_FEATURE_CONFIG["label_synonyms"]
    assert fl.labels_similar("CHAIN LINK FENCE", "CHAIN LINK FENCE", synonyms)
    assert fl.labels_similar("6' CHAIN LINK FENCE", "CHAIN LINK FENCE", synonyms)
    assert fl.labels_similar("C.L.F.", "CHAIN LINK FENCE", synonyms)
    assert fl.labels_similar("CONCRETE WALK", "CONC. WALK", synonyms)
    assert fl.labels_similar("3 STEPS", "STEPS", synonyms)
    assert fl.labels_similar("FRAME DECK", "FRAME\nDECK", synonyms)
    assert not fl.labels_similar("WOOD FENCE", "CHAIN LINK FENCE", synonyms)
    assert not fl.labels_similar("SHED", "FRAME DECK", synonyms)
    # Подпись калитки — не подпись забора из того же материала.
    assert not fl.labels_similar("C.L.F. GATE", "CHAIN LINK FENCE", synonyms)
    assert fl.labels_similar("V.FE. GATE", "V.FE.\nGATE", synonyms)


# -------------------------------------------------------------- распознавание

def test_property_polygon_found_among_tax_lines():
    snap = build_scene()
    boundary = fl.find_property_polygon(snap, {})
    assert boundary.layer == "PROPERTY_LINE"
    assert boundary.area_sqft == pytest.approx(4000.0, abs=0.01)
    assert boundary.vertex_count == 4
    assert boundary.method == "auto"


def test_property_polygon_closes_small_gap_with_note():
    snap = build_scene(angle_deg=17.0, gap=0.4)
    boundary = fl.find_property_polygon(snap, {})
    assert boundary.area_sqft == pytest.approx(4000.0, rel=1e-3)
    assert any("разрыв" in note for note in boundary.notes)


def test_property_polygon_missing_raises_russian_message():
    snap = DrawingSnapshot()
    snap.lines.append(DrawingLine("BUILDING_OUTLINE", ((0, 0), (10, 0), (10, 10), (0, 10)), closed=True))
    with pytest.raises(fl.FeatureLabelError, match="участ"):
        fl.find_property_polygon(snap, {})


def test_property_polygon_from_selected_lines():
    snap = build_scene()
    picked = [line for line in snap.lines if line.layer == "PROPERTY_LINE"]
    boundary = fl.property_polygon_from_selection(picked, snap, {})
    assert boundary.method == "pick"
    assert boundary.area_sqft == pytest.approx(4000.0, abs=0.01)


@pytest.mark.parametrize(("angle", "gap"), [(0.0, 0.0), (17.0, 0.4), (-63.0, 0.0)])
def test_recognition_sources_and_counters(angle, gap):
    result = recognize(build_scene(angle, gap))

    # Коды точек делят общий слой CODE_CREW по материалу забора.
    crew = {f.label: f for f in result.features if f.layer == "CODE_CREW"}
    assert set(crew) == {"CHAIN LINK FENCE", "VINYL FENCE"}
    assert crew["CHAIN LINK FENCE"].source == "point_code"
    assert crew["CHAIN LINK FENCE"].geometry.length == pytest.approx(97.6, abs=0.2)
    assert crew["VINYL FENCE"].geometry.length == pytest.approx(39.2, abs=0.2)
    assert {code.code for code in crew["VINYL FENCE"].codes} == {"FEV64"}

    # Имена слоёв — в формулировках заказчика.
    assert labels_of(result, "FENCE_WOOD_RIGHT") == ["WOOD FENCE"]
    assert labels_of(result, "FENCE_CHAIN_FRONT") == ["CHAIN LINK FENCE"]
    assert labels_of(result, "SUBJECT_SHED_OUTLINE") == ["SHED"]
    assert labels_of(result, "NEIGHBOUR_SHED_OUTLINE") == ["SHED"]
    assert labels_of(result, "SIDE_PORCH") == ["O.P."]
    assert labels_of(result, "FRONT_LANDING") == ["O.P."]
    assert labels_of(result, "REAR_DECK_EDGE") == ["FRAME\nDECK"]
    assert labels_of(result, "DRIVEWAY_EDGE") == ["DRIVEWAY"]
    assert labels_of(result, "SIDEWALK_EDGE") == ["CONC. WALK"]
    assert labels_of(result, "WALK_EDGE") == ["CONC. WALK"]
    assert labels_of(result, "PLANTER_EDGE") == ["PLANTER"]
    assert labels_of(result, "GRASS_STRIP_EDGE") == ["GRASS"]
    # Заказчик не пишет число ступеней: проступи считаются только по настройке.
    assert labels_of(result, "FRONT_STEPS") == ["STEPS"]
    assert labels_of(result, "REAR_STEPS") == ["STEPS"]
    # Калитка без своего кода: сокращение материала соседнего забора + GATE.
    gate = next(f for f in result.features if f.layer == "GATE_FRONT")
    assert (gate.category, gate.label, gate.source) == ("gate", "C.L.F.\nGATE", "adjacent")
    # LOW_EDGE_* заказчик не подписывает: это только линия-препятствие.
    assert not [f for f in result.features if f.layer == "LOW_EDGE_TIMBER"]

    # Дом подписывается текстом из окна программы.
    house = next(f for f in result.features if f.layer == "BUILDING_OUTLINE")
    assert house.is_main_house and house.label == HOUSE and house.source == "user"

    # Столбы и колодцы заказчик не подписывает; точка за буфером 50 ft не читается.
    assert not [f for f in result.features if f.kind == "point"]
    assert result.unknown_codes == {"XYZ": 1}
    assert len(result.point_codes) == 8

    # Слои без правила найдены, но остаются без подписи.
    assert {f.layer for f in result.unlabeled} == {"ST-WALL"}
    # Соседний дом — не «слой без правила»: его описание вписывают вручную.
    (neighbour,) = [f for f in result.features if f.layer == "REF_VEC_BUILDING"]
    assert neighbour.label is None and neighbour.skip_reason == fl.OTHER_HOUSE_REASON
    assert (result.by_notes, result.by_point_codes, result.by_layers, result.by_user) == (0, 2, 15, 1)
    assert len(result.labeled) == 18


def test_step_count_and_point_objects_are_optional():
    cfg = {"feature_labeling": {"step_count_labels": True, "label_point_features": True}}
    result = recognize(build_scene(), cfg=cfg)
    assert labels_of(result, "FRONT_STEPS") == ["3 STEPS"]
    steps = next(f for f in result.features if f.layer == "FRONT_STEPS")
    assert steps.tread_count == 3
    # TTP — точка хода (S-CONT-SET), а не столб: подписи не получает.
    assert {f.label for f in result.features if f.kind == "point"} == {"MANHOLE"}


def test_structural_point_codes_are_not_reported_as_unknown():
    snap = build_scene()
    for index, value in enumerate(("BLC", "BLC2", "OP77", "SW3", "STEP6", "PLAN69", "ABC12", "ABC")):
        snap.texts.append(DrawingText("POINT_LABELS", f"{50 + index} {value}", (5 + index, 6), height=0.5))
    result = recognize(snap)
    # Коды углов объектов молчат; у остальных числовой хвост отбрасывается.
    assert result.unknown_codes == {"XYZ": 1, "ABC": 2}


def test_house_without_user_text_is_not_labeled():
    result = recognize(build_scene(), house="")
    house = next(f for f in result.features if f.layer == "BUILDING_OUTLINE")
    assert house.label is None
    assert result.by_user == 0


def test_point_code_beyond_3_5_ft_does_not_bind_to_fence():
    snap = DrawingSnapshot()
    for p, q in (((0, 0), (40, 0)), ((40, 0), (40, 100)), ((40, 100), (0, 100)), ((0, 100), (0, 0))):
        snap.lines.append(DrawingLine("PROPERTY_LINE", (p, q)))
    snap.lines.append(DrawingLine("S-FENCE", ((5, 50), (35, 50))))
    snap.texts.append(DrawingText("POINT_LABELS", "1 FEW", (20, 54.5), height=0.9))  # 4.5 ft
    far = recognize(snap, house="")
    fence = next(f for f in far.features if f.layer == "S-FENCE")
    assert fence.label is None
    assert len(far.unbound_fence_codes) == 1

    snap.texts[0] = DrawingText("POINT_LABELS", "1 FEW", (20, 53.0), height=0.9)  # 3.0 ft
    near = recognize(snap, house="")
    fence = next(f for f in near.features if f.layer == "S-FENCE")
    assert fence.label == "WOOD FENCE"
    assert fence.source == "point_code"
    assert near.by_point_codes == 1


def test_point_code_overrides_layer_name():
    """Рядом с FENCE_WOOD_* стоит точка FEC — приоритет у кода точки (источник 1)."""
    snap = DrawingSnapshot()
    for p, q in (((0, 0), (40, 0)), ((40, 0), (40, 100)), ((40, 100), (0, 100)), ((0, 100), (0, 0))):
        snap.lines.append(DrawingLine("PROPERTY_LINE", (p, q)))
    snap.lines.append(DrawingLine("FENCE_WOOD_REAR", ((5, 90), (35, 90))))
    snap.texts.append(DrawingText("POINT_LABELS", "4 FEC", (20, 90.5), height=0.9))
    result = recognize(snap, house="")
    fence = next(f for f in result.features if f.layer == "FENCE_WOOD_REAR")
    assert fence.label == "CHAIN LINK FENCE"
    assert fence.source == "point_code"


# ------------------------------------------------------------------ раскладка

def make_plan(snap, *, house=HOUSE, skip_existing=True, height=1.0, cfg=None):
    result = recognize(snap, house=house, cfg=cfg)
    return ab.plan_feature_labels(result, snap, cfg or {}, text_height=height, skip_existing=skip_existing)


def item_for(plan, layer, label=None):
    found = [
        item for item in plan.items
        if item.feature.layer == layer and (label is None or item.text == label)
    ]
    assert len(found) == 1, (layer, label, [i.text for i in found])
    return found[0]


@pytest.mark.parametrize("angle", [0.0, 17.0, -63.0, 118.0])
def test_plan_places_every_label_without_overlaps(angle):
    snap = build_scene(angle)
    plan = make_plan(snap)
    assert len(plan.items) == 18
    assert plan.duplicates == []
    assert plan.warnings == []
    assert plan.output_layer == "D-TOPO-TEXT"
    assert plan.count_placement("leader") == 0  # выноски — только когда рядом нет места

    boxes = [item.box for item in plan.items]
    for i, first in enumerate(boxes):
        for second in boxes[i + 1:]:
            assert not first.intersects(second)

    # Новые тексты не ложатся на линии чертежа (кроме дома и калитки поперёк линии).
    lines = [fl.line_to_shapely(line) for line in snap.lines]
    for item in plan.items:
        if item.feature.is_main_house:
            continue
        for geom in lines:
            assert not item.box.intersects(geom), (item.text, item.placement)

    # Текст не перевёрнут: горизонтальный читается слева направо, вертикальный —
    # снизу вверх (поворот в окне −60°..120°), и стоит по осям участка.
    for item in plan.items:
        degrees = math.degrees(item.rotation)
        assert -60.0 < degrees <= 120.0 + 1e-6, (item.text, degrees)
        assert (degrees - angle) % 90.0 == pytest.approx(0.0, abs=1e-4) or (
            degrees - angle
        ) % 90.0 == pytest.approx(90.0, abs=1e-4), (item.text, degrees)


def test_long_fence_gets_text_parallel_to_line_with_offset():
    plan = make_plan(build_scene())
    item = item_for(plan, "FENCE_WOOD_RIGHT")
    assert (item.kind, item.placement) == ("text", "along")
    # Вертикальный текст читается снизу вверх, как в итоговом чертеже заказчика.
    assert math.degrees(item.rotation) == pytest.approx(90.0, abs=1e-6)
    # Правило заказчика: между линией забора и буквами 0.7 ft при тексте 0.9 ft
    # (масштаб 1"=16'), для другой высоты текста — пропорционально; текст со
    # стороны участка.
    gap = 0.7 * item.height / 0.9
    assert item.box.distance(LineString([(39.8, 0.0), (39.8, 100.0)])) == pytest.approx(gap)
    assert abs(item.point[0] - 39.8) == pytest.approx(gap + item.height / 2.0, abs=1e-6)
    # Прежнее правило (доля высоты текста) остаётся настройкой.
    old = make_plan(build_scene(), cfg={"feature_labeling": {"along_line_gaps_ft": {}}})
    old_item = item_for(old, "FENCE_WOOD_RIGHT")
    assert abs(old_item.point[0] - 39.8) == pytest.approx(1.1 * old_item.height, abs=1e-6)
    assert item.point[0] < 39.8
    assert 5.0 < item.point[1] < 95.0

    rear = item_for(plan, "CODE_CREW", "VINYL FENCE")
    assert (rear.kind, rear.placement) == ("text", "along")
    assert math.degrees(rear.rotation) == pytest.approx(0.0, abs=1e-6)
    assert rear.point[1] == pytest.approx(99.6 - gap - rear.height / 2.0, abs=1e-6)


def test_vertical_text_direction_is_configurable():
    assert math.degrees(ab.label_rotation(math.pi / 2, "down")) == pytest.approx(-90.0)
    assert math.degrees(ab.label_rotation(math.radians(88.0), "down")) == pytest.approx(-92.0)
    assert math.degrees(ab.label_rotation(math.radians(92.0), "down")) == pytest.approx(-88.0)
    assert math.degrees(ab.label_rotation(math.radians(200.0), "down")) == pytest.approx(20.0)
    assert math.degrees(ab.label_rotation(math.pi / 2, "up")) == pytest.approx(90.0)
    assert math.degrees(ab.label_rotation(math.radians(272.0), "up")) == pytest.approx(92.0)
    # Итоговый чертёж Ilion Place: 90.4° вдоль боковых границ, 70.8° вдоль скошенной.
    assert math.degrees(ab.label_rotation(math.radians(270.418), "up")) == pytest.approx(90.418)
    assert math.degrees(ab.label_rotation(math.radians(250.836), "up")) == pytest.approx(70.836)
    assert math.degrees(ab.label_rotation(math.radians(180.418), "up")) == pytest.approx(0.418)
    # Без стрелки севера — снизу вверх; настройка задаёт направление жёстко.
    assert fl.DEFAULT_FEATURE_CONFIG["vertical_text_reads"] == "auto"
    plan = make_plan(build_scene())
    assert math.degrees(item_for(plan, "FENCE_WOOD_RIGHT").rotation) == pytest.approx(90.0)
    assert plan.vertical_reads == "up" and "стрелка севера не найдена" in plan.vertical_reads_note
    plan = make_plan(build_scene(), cfg={"feature_labeling": {"vertical_text_reads": "down"}})
    assert math.degrees(item_for(plan, "FENCE_WOOD_RIGHT").rotation) == pytest.approx(-90.0)
    assert plan.vertical_reads == "down" and "настройка vertical_text_reads" in plan.vertical_reads_note


def north_arrow(rotation_deg, *, name="NA", layer="M-NTH-ARROW", **kwargs):
    return fl.DrawingSymbol(
        layer=layer, name=name, point=(-40.0, 140.0), rotation=math.radians(rotation_deg), **kwargs
    )


def test_north_arrow_nearest_the_lot_is_used_when_several_stand_on_the_sheet():
    # В рабочем DWG бывает несколько заготовок листа: блоки NA повёрнуты по-разному
    # (в полном 24 Alvine — 12 блоков). Берём стрелку, ближайшую к участку, иначе север
    # зависит от порядка обхода чертежа.
    from shapely.geometry import Polygon

    far = north_arrow(0.0, name="NA", layer="0")
    far.point = (900.0, 900.0)
    near = north_arrow(270.0, name="NA", layer="0")
    near.point = (10.0, 10.0)
    snapshot = fl.DrawingSnapshot(symbols=[far, near])
    lot = Polygon([(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0)])
    assert fl.find_north_arrow(snapshot, {}).side == "up"  # без near — первый найденный
    assert fl.find_north_arrow(snapshot, {}, near=lot).side == "right"


@pytest.mark.parametrize(
    ("rotation_deg", "side", "reads"),
    [
        (0.0, "up", "up"),
        (350.326, "up", "up"),       # итоговый чертёж «12 Ilion Place»: текст 0° / 90°
        (349.908, "up", "up"),       # полевой чертёж «12 Ilion Place»
        (308.237, "right", "down"),  # полевой чертёж «6910 15th Avenue»
        (304.718, "right", "down"),  # итоговый «6910 15th Avenue»: текст 356.5° / 266.5°
        (270.0, "right", "down"),
        (226.0, "right", "down"),
        (315.0, "up", "up"),         # ровно по диагонали — как по умолчанию
        (45.0, "up", "up"),
        (46.0, "left", "up"),
        (75.339, "left", "up"),      # полевой «225 Aspinwall Street»: в итоговом 14.6° / 284.6°
        (90.0, "left", "up"),
        (180.0, "down", "up"),
    ],
)
def test_north_arrow_decides_how_vertical_text_reads(rotation_deg, side, reads):
    """Лист держат севером вверх: стрелка вправо — вертикальный текст сверху вниз."""
    snap = fl.DrawingSnapshot(symbols=[north_arrow(rotation_deg)])
    arrow = fl.find_north_arrow(snap, {})
    assert arrow.north_deg == pytest.approx((rotation_deg + 90.0) % 360.0)
    assert arrow.side == side
    found, note = fl.vertical_text_direction(snap, {})
    assert found == reads
    assert "блок NA, слой M-NTH-ARROW" in note
    assert ("сверху вниз" in note) == (reads == "down")
    # Проверка правила: после поворота листа севером вверх вертикальная подпись
    # читается слева направо (0°), а не справа налево (180°).
    if side in {"right", "left"}:
        text_deg = -90.0 if reads == "down" else 90.0
        turn_deg = 90.0 if side == "right" else -90.0  # поворот листа до «север вверху»
        assert (text_deg + turn_deg) % 360.0 == pytest.approx(0.0)


def test_north_arrow_is_found_by_block_name_or_layer():
    wup = fl.DrawingSymbol(layer="U-TRAF-IRON", name="WUP", point=(0.0, 0.0), rotation=math.radians(270.0))
    assert fl.find_north_arrow(fl.DrawingSnapshot(symbols=[wup]), {}) is None
    assert fl.vertical_text_direction(fl.DrawingSnapshot(symbols=[wup]), {})[0] == "up"
    # По имени блока в любом слое; по слою при любом имени блока.
    by_name = fl.DrawingSnapshot(symbols=[wup, north_arrow(270.0, name="North Arrow 2", layer="0")])
    assert fl.find_north_arrow(by_name, {}).side == "right"
    by_layer = fl.DrawingSnapshot(symbols=[wup, north_arrow(270.0, name="A$C1F2", layer="m-nth-arrow")])
    assert fl.find_north_arrow(by_layer, {}).side == "right"
    # Блок с именем стрелки важнее чужого блока в её слое.
    mixed = fl.DrawingSnapshot(symbols=[
        north_arrow(0.0, name="LOGO", layer="M-NTH-ARROW"), north_arrow(270.0, name="NA", layer="0"),
    ])
    assert fl.find_north_arrow(mixed, {}).name == "NA"
    # Зеркальная вставка и блок, где север нарисован вдоль +X.
    assert fl.find_north_arrow(fl.DrawingSnapshot(symbols=[north_arrow(0.0, y_scale=-0.3)]), {}).side == "down"
    assert fl.find_north_arrow(fl.DrawingSnapshot(symbols=[north_arrow(0.0, x_scale=-0.3)]), {}).side == "up"
    along_x = {"feature_labeling": {"north_arrow_block_north_deg": 0.0}}
    assert fl.find_north_arrow(fl.DrawingSnapshot(symbols=[north_arrow(0.0)]), along_x).side == "right"
    # Жёсткая настройка сильнее стрелки.
    right = fl.DrawingSnapshot(symbols=[north_arrow(270.0)])
    assert fl.vertical_text_direction(right, {"feature_labeling": {"vertical_text_reads": "up"}})[0] == "up"


def test_vertical_labels_follow_the_north_arrow_of_the_drawing():
    def fence_rotation(rotation_deg, cfg=None):
        snap = build_scene()
        snap.symbols.append(north_arrow(rotation_deg))
        plan = make_plan(snap, cfg=cfg)
        return plan, math.degrees(item_for(plan, "FENCE_WOOD_RIGHT").rotation)

    plan, rotation = fence_rotation(0.0)
    assert (plan.vertical_reads, rotation) == ("up", pytest.approx(90.0))
    plan, rotation = fence_rotation(270.0)       # север справа
    assert (plan.vertical_reads, rotation) == ("down", pytest.approx(-90.0))
    plan, rotation = fence_rotation(90.0)        # север слева
    assert (plan.vertical_reads, rotation) == ("up", pytest.approx(90.0))
    plan, rotation = fence_rotation(270.0, {"feature_labeling": {"vertical_text_reads": "up"}})
    assert (plan.vertical_reads, rotation) == ("up", pytest.approx(90.0))
    # Горизонтальные подписи от стрелки не зависят.
    for arrow_deg in (0.0, 270.0):
        plan, _rotation = fence_rotation(arrow_deg)
        rear = item_for(plan, "CODE_CREW", "VINYL FENCE")
        assert math.degrees(rear.rotation) == pytest.approx(0.0, abs=1e-6)
    # Реальные чертежи: Ilion — север вверху, Бруклин — справа (как в итоговых у заказчика).
    assert make_plan(load_ilion(), house=ILION_HOUSE, height=0.9).vertical_reads == "up"
    brooklyn = brooklyn_plan()
    assert brooklyn.vertical_reads == "down"
    assert "смотрит вправо, 52° по часовой от верха листа" in brooklyn.vertical_reads_note


def test_north_arrow_rotation_is_read_from_autocad(com):
    builder, _app, doc = com(load_brooklyn())
    read = builder.read_drawing_snapshot(doc)
    (arrow,) = [symbol for symbol in read.symbols if symbol.name == "NA"]
    assert math.degrees(arrow.rotation) == pytest.approx(308.237)
    result = builder.label_features(house_text=BROOKLYN_HOUSE)
    assert result.failed == []
    assert (
        "Направление текста: стрелка севера (блок NA, слой M-NTH-ARROW) смотрит вправо"
        in result.report()
    )
    vertical = [
        e for e in doc.ModelSpace.created
        if e.ObjectName == "AcDbText" and e.TextString in {"PARTY WALL", "C.L.F. ATOP CONC. WALL"}
    ]
    assert len(vertical) == 3
    assert all(math.degrees(e.Rotation) % 360.0 == pytest.approx(270.0, abs=0.2) for e in vertical)


def test_fence_text_moves_away_from_parallel_property_line():
    """Забор в 0.3 ft от границы: подпись не ложится ни на забор, ни на границу."""
    plan = make_plan(build_scene())
    item = item_for(plan, "CODE_CREW", "CHAIN LINK FENCE")
    assert item.placement == "along" and item.note == ""
    minx, _miny, _maxx, _maxy = item.box.bounds
    assert minx > 0.3 + 0.15  # правее забора (x = 0.3) и границы (x = 0) с зазором
    lot_line = plan.recognition.boundary.polygon.exterior
    assert not item.box.intersects(lot_line)


def test_long_fence_is_labeled_on_every_long_run_once():
    snap = build_scene()
    plan = make_plan(snap)
    # CODE_CREW/CHAIN LINK: вертикальный участок 97 ft — одна подпись.
    chain = [i for i in plan.items if i.feature.layer == "CODE_CREW" and i.text == "CHAIN LINK FENCE"]
    assert len(chain) == 1
    # Забор «Г»-образный, оба участка длиннее 25 ft: подпись на каждом.
    snap.lines.append(DrawingLine("FENCE_IRON_FAR", ((-30, 110), (30, 110), (30, 140)), kind="AcDbPolyline"))
    plan = make_plan(snap)
    iron = [i for i in plan.items if i.feature.layer == "FENCE_IRON_FAR"]
    assert sorted(round(math.degrees(i.rotation)) for i in iron) == [0, 90]
    assert {i.text for i in iron} == {"IRON FENCE"}


def test_rotated_fence_text_follows_fence_angle():
    plan = make_plan(build_scene(17.0))
    item = item_for(plan, "FENCE_WOOD_RIGHT")
    assert item.placement == "along"
    # Линия под 107°: текст вдоль неё, снизу вверх, как вдоль остальных боковых линий.
    assert math.degrees(item.rotation) == pytest.approx(107.0, abs=1e-6)


def test_short_fence_gets_abbreviation_and_steps_label_sits_beside():
    plan = make_plan(build_scene())
    # Короткий участок забора (6 ft): сокращение материала вдоль линии, без выноски.
    short = item_for(plan, "FENCE_CHAIN_FRONT")
    assert (short.text, short.kind, short.placement) == ("C.L.F.", "text", "along")
    assert short.leader_points == []
    # Ступени с проступями: подпись вплотную к контуру, со стороны от дома.
    steps = item_for(plan, "FRONT_STEPS")
    assert (steps.text, steps.kind, steps.placement) == ("STEPS", "mtext", "beside")
    assert steps.leader_points == []
    outline = next(
        f.geometry for f in plan.recognition.features if f.layer == "FRONT_STEPS"
    )
    assert not steps.box.intersects(outline)
    assert steps.box.distance(outline) == pytest.approx(0.5, abs=1e-6)  # вплотную: 0.5 высоты
    # Объекты участка подписываются на участке, а не за его границей.
    lot = plan.recognition.boundary.polygon
    for layer in ("FENCE_CHAIN_FRONT", "FRONT_STEPS", "GATE_FRONT"):
        assert lot.contains(item_for(plan, layer).box), layer


def test_gate_label_is_stacked_across_the_gate_line():
    snap = build_stafford_scene()
    plan = make_plan(snap, house=STAFFORD_HOUSE)
    gate = item_for(plan, "GATE_RIGHT_CHAIN")
    assert (gate.text, gate.kind, gate.placement) == ("C.L.F.\nGATE", "mtext", "gate")
    # Середина подписи — на линии калитки: одна строка над линией, вторая под ней.
    assert gate.point == pytest.approx(((39.7 + 45.4) / 2, 68.75), abs=1e-6)
    assert gate.rotation == pytest.approx(0.0)
    assert gate.mtext_string == "C.L.F.\\PGATE"
    # Тесное место (клумба, забор): обе строки с одной стороны линии.
    left = item_for(plan, "GATE_LEFT_INSERT")
    assert left.text == "V.FE.\nGATE" and left.placement == "gate"
    assert left.box.bounds[3] < 54.0


def test_leader_is_short_arc_when_no_room_beside():
    # Текст высотой 1.1 ft в проход между домом и границей (4.9 ft) уже не входит.
    plan = make_plan(build_stafford_scene(), house=STAFFORD_HOUSE, height=1.1)
    assert plan.count_placement("leader") == 2
    item = item_for(plan, "NEIGHBOUR_SIDE_STAIR")
    assert (item.text, item.kind, item.placement) == ("CONC.\nSTEPS", "leader", "leader")
    assert len(item.leader_points) == 2 and item.leader_bulge != 0.0
    tip, end = item.leader_points
    assert 42.25 <= tip[0] <= 45.4 and 26.5 <= tip[1] <= 30.0  # стрелка на ступенях
    assert math.hypot(end[0] - tip[0], end[1] - tip[1]) <= 12.0 * plan.text_height
    arc = ab._bulge_arc_points(tip, end, item.leader_bulge)
    assert arc[0] == tip and arc[-1] == end and len(arc) > 2


def stafford_plan(**kwargs):
    """План сцены «87 Stafford» при рабочей высоте текста 0.9 ft (масштаб 1" = 16')."""
    kwargs.setdefault("height", 0.9)
    return make_plan(build_stafford_scene(), house=STAFFORD_HOUSE, **kwargs)


def test_label_width_follows_romans_font_metrics():
    # Ширина — габарит видимого текста: сумма шагов символов минус поля крайних
    # символов (0.26 высоты). Замеры по чертежам заказчика (текст 0.9 ft):
    # SHED ~3.3 ft, CHAIN LINK FENCE ~12.6 ft, C.D. 2.36 ft, VEST. 3.34 ft.
    bearing = 0.26
    assert ab.feature_text_width("SHED", 0.9) == pytest.approx(0.9 * (82 / 21 - bearing))
    assert ab.feature_text_width("CHAIN LINK FENCE", 0.9) == pytest.approx(12.62, abs=0.01)
    assert ab.feature_text_width("C.D.", 0.9) == pytest.approx(2.36, abs=0.12)
    assert ab.feature_text_width("VEST.", 0.9) == pytest.approx(3.34, abs=0.1)
    assert ab.feature_text_width("FIRE ESC.", 0.9) == pytest.approx(6.39, abs=0.15)
    # Точки и буква I узкие: O.P. заметно короче, чем четыре «средних» символа.
    assert ab.feature_text_width("O.P.", 1.0) == pytest.approx((22 + 21) / 21 + 2 * 0.48 - bearing)
    assert ab.feature_text_width("O.P.", 1.0) < 0.8 * ab.feature_text_width("SHED", 1.0)
    assert ab.feature_text_width("SHED", 1.0, scale=1.2) == pytest.approx(1.2 * (82 / 21 - bearing))
    # Ключи раздела feature_labeling меняют ширину букв и пробела и поля.
    wide = ab.feature_text_width(
        "A A", 1.0, {"char_width_factor": 1.84, "space_width_factor": 2.0, "text_bearing_factor": 0.0}
    )
    assert wide == pytest.approx(2 * 2 * 18 / 21 + 2.0)


def test_steps_without_outline_are_labeled_beside_the_porch():
    """Боковое крыльцо: ступени — только линии проступей внутри контура крыльца."""
    plan = stafford_plan()
    porch = item_for(plan, "SIDE_PORCH")
    steps = item_for(plan, "SIDE_STEP_TREADS")
    assert (porch.text, porch.placement) == ("O.P.", "inside")
    assert porch.rotation == pytest.approx(0.0)  # горизонтально, как у заказчика
    # O.P. стоит на площадке и не задевает проступи (y = 28, 29, 30).
    assert porch.box.bounds[1] > 30.0 + 0.2 and porch.box.bounds[3] < 34.0
    assert (steps.text, steps.kind, steps.placement) == ("CONC.\nSTEPS", "mtext", "beside")
    assert steps.feature.source == "field_note"
    assert steps.feature.note_text == "SIDE BRICK PORCH & 3 TREADS"
    # CONC. STEPS — сразу за контуром крыльца со стороны ступеней, в проходе между
    # границей участка (x = 0) и стеной дома (x = 4.9), без выноски.
    minx, miny, maxx, maxy = steps.box.bounds
    assert maxy < 27.0 and maxy > 27.0 - 1.0
    assert 0.0 + 0.2 < minx and maxx < 4.9 - 0.2
    assert steps.leader_points == [] and steps.note == ""
    # Проступи — не контур ступеней: отдельным объектом они не считаются.
    layers = [f.layer for f in plan.recognition.features]
    assert "NEIGHBOUR_STAIR_TREADS" not in layers and "FRONT_STEP_TREADS" not in layers
    assert layers.count("SIDE_STEP_TREADS") == 1


def test_gate_crossing_the_lot_line_is_labeled_inside_the_lot():
    """Линия калитки тянется до стены соседа: подпись — в проёме между домом и забором."""
    plan = stafford_plan()
    gate = item_for(plan, "GATE_RIGHT_INSERT")
    assert (gate.text, gate.placement) == ("V.FE.\nGATE", "gate")
    assert gate.point == pytest.approx(((31.9 + 40.0) / 2.0, 47.6), abs=1e-6)
    minx, _miny, maxx, _maxy = gate.box.bounds
    assert 31.9 < minx and maxx < 39.7  # между стеной дома и забором, границу не задевает


def test_short_neighbour_fences_outside_the_lot_get_abbreviations():
    """Возвраты и продолжения соседских заборов — сокращением, как в итоговом чертеже."""
    outside = {"FENCE_CHAIN_9_NORTH", "FENCE_VINYL_REAR_RETURN", "FENCE_VINYL_16_EXTENSION"}
    plan = stafford_plan()
    assert plan.skipped == []
    assert {item.feature.layer: item.text for item in plan.items if item.feature.layer in outside} == {
        "FENCE_CHAIN_9_NORTH": "C.L.F.",
        "FENCE_VINYL_REAR_RETURN": "V.FE.",
        "FENCE_VINYL_16_EXTENSION": "V.FE.",
    }
    assert not [item for item in plan.items if item.feature.layer in outside and item.placement != "along"]
    # Забор соседа вдоль задней границы (в 0.5–0.9 ft от неё) подписывается полностью:
    # прямой участок 23.6 ft больше чем вдвое длиннее подписи.
    assert item_for(plan, "FENCE_VINYL_REAR_MEASURED").text == "VINYL FENCE"

    # Настройка убирает сокращения с коротких заборов за участком.
    plan = stafford_plan(cfg={"feature_labeling": {"label_short_fences_outside_lot": False}})
    assert not {item.feature.layer for item in plan.items} & outside
    assert {feature.layer for feature, _reason in plan.skipped} == outside
    assert {reason for _feature, reason in plan.skipped} == {"короткий забор за пределами участка"}
    assert not [item for item in plan.items if item.text in {"C.L.F.", "V.FE."}]
    assert item_for(plan, "FENCE_VINYL_REAR_MEASURED").text == "VINYL FENCE"


def test_fence_text_keeps_clear_of_fence_and_lot_lines():
    plan = stafford_plan()
    lot_line = plan.recognition.boundary.polygon.exterior
    for layer, fence in (
        ("FENCE_CHAIN_LEFT", LineString([(0.3, 54.0), (0.3, 99.7)])),
        ("FENCE_CHAIN_RIGHT", LineString([(39.7, 47.6), (39.7, 99.7)])),
        ("FENCE_CHAIN_REAR", LineString([(0.3, 99.7), (39.5, 99.7)])),
    ):
        item = item_for(plan, layer)
        assert item.text == "CHAIN LINK FENCE" and item.placement == "along"
        # Между линией забора и буквами — 0.7 ft, до границы участка — не меньше
        # 0.8 ft (правило заказчика для 1"=16'); текст внутри участка.
        assert item.box.distance(fence) == pytest.approx(0.7, abs=1e-6), layer
        assert item.box.distance(lot_line) >= 0.8 - 1e-6, layer
        assert plan.recognition.boundary.polygon.contains(item.box), layer


def test_code_crew_lines_without_fence_code_are_silent():
    plan = stafford_plan()
    rec = plan.recognition
    crew = [f for f in rec.features if f.layer == "CODE_CREW"]
    assert len(crew) == 2 and all(f.label is None for f in crew)
    assert all(f.skip_reason == fl.CODE_LINEWORK_REASON for f in crew)
    assert rec.unlabeled == []
    assert [f.layer for f in rec.features if f.skip_reason == fl.OTHER_HOUSE_REASON] == [
        "BUILDING_OUTLINE"  # соседний дом
    ]


def test_surface_note_replaces_layer_label_of_the_same_surface():
    """«CONCRETE SIDEWALK» в 9 ft от кромки тротуара: одна подпись CONC. WALK, а не две."""
    plan = stafford_plan()
    walks = [item for item in plan.items if item.single_line == "CONC. WALK"]
    assert len(walks) == 1
    assert walks[0].placement == "spot" and walks[0].feature.note_text == "CONCRETE SIDEWALK"
    edge = next(f for f in plan.recognition.features if f.layer == "SIDEWALK_EDGE")
    assert edge.label is None and "CONCRETE SIDEWALK" in edge.skip_reason


def test_stafford_plan_has_no_overlaps_at_any_rotation():
    for angle in (0.0, 33.0, -56.6, 123.4):
        plan = make_plan(
            build_stafford_scene(angle_deg=angle), house=STAFFORD_HOUSE, height=0.9
        )
        assert len(plan.items) == 26 and plan.warnings == [], angle
        assert plan.count_placement("leader") == 0, angle
        assert not [item for item in plan.items if item.note], angle
        boxes = [item.box for item in plan.items]
        for index, first in enumerate(boxes):
            for second in boxes[index + 1:]:
                assert not first.intersects(second), angle


def test_structures_get_mtext_centered_inside():
    plan = make_plan(build_scene())
    house = item_for(plan, "BUILDING_OUTLINE")
    assert (house.kind, house.placement) == ("mtext", "inside")
    assert house.point == pytest.approx((23.0, 46.0), abs=1e-6)
    assert house.attachment == ab.AC_ATTACHMENT_MIDDLE_CENTER
    for layer, bounds in (
        ("SUBJECT_SHED_OUTLINE", (28, 86, 38, 96)),
        ("REAR_DECK_EDGE", (14, 62, 28, 72)),
        ("SIDE_PORCH", (34, 40, 39, 50)),
    ):
        item = item_for(plan, layer)
        assert (item.kind, item.placement) == ("mtext", "inside"), layer
        minx, miny, maxx, maxy = item.box.bounds
        assert bounds[0] <= minx and maxx <= bounds[2], layer
        assert bounds[1] <= miny and maxy <= bounds[3], layer


def test_multiline_house_text_becomes_mtext_paragraphs():
    plan = make_plan(build_scene(), house="2 STY. FRAME\nNo. 87")
    house = item_for(plan, "BUILDING_OUTLINE")
    assert house.mtext_string == "2 STY. FRAME\\PNo. 87"
    assert house.single_line == "2 STY. FRAME No. 87"


def test_existing_labels_are_not_duplicated():
    snap = build_scene(with_existing=True)
    plan = make_plan(snap)
    skipped = {(dup.feature.layer, dup.existing_layer) for dup in plan.duplicates}
    assert skipped == {
        ("CODE_CREW", "D-TOPO-TEXT"),
        ("SUBJECT_SHED_OUTLINE", "D-TOPO-TEXT"),
        ("SIDEWALK_EDGE", "D-TOPO-TEXT"),
    }
    assert len(plan.items) == 15
    # Одноимённый забор другого слоя далеко от надписи всё равно подписывается.
    assert item_for(plan, "FENCE_CHAIN_FRONT").text == "C.L.F."
    assert item_for(plan, "WALK_EDGE").text == "CONC. WALK"


def test_field_drawing_tax_texts_and_overlay_path_do_not_push_labels():
    """Tax-тексты полевого чертежа и надпись-путь подложки в итог не идут — подписям не мешают."""
    baseline = make_plan(build_stafford_scene(), house=STAFFORD_HOUSE)
    deck = item_for(baseline, "REAR_DECK_EDGE")
    shed = item_for(baseline, "SUBJECT_SHED_OUTLINE")

    snap = build_stafford_scene()
    snap.texts.append(DrawingText("TAX_LOT_TEXT", "#87   LOT 70", deck.point, height=1.4))
    snap.texts.append(DrawingText("TAX_LOT_TEXT", "S 33°23'48\" W  40.00 FT", (8.0, 99.0), height=0.5))
    overlay = r"f\field-011-87stafford-clean-20261104-214406\ortho2024.png"
    snap.texts.append(DrawingText("0", overlay, (-40.0, 45.0), height=4.0))
    snap.texts.append(DrawingText("CONTEXT_TEXT", overlay, (-40.0, 60.0), height=4.0))
    plan = make_plan(snap, house=STAFFORD_HOUSE)
    assert plan.warnings == baseline.warnings == []
    assert [(i.feature.layer, i.text, i.placement) for i in plan.items] == [
        (i.feature.layer, i.text, i.placement) for i in baseline.items
    ]
    for new, old in zip(plan.items, baseline.items):
        assert new.point == pytest.approx(old.point)
    assert not [note for note in plan.recognition.field_notes if "ortho2024" in note.text]

    # Подписи границы Шага 1 и готовые подписи D-TOPO-TEXT остаются в чертеже — их обходим.
    snap = build_stafford_scene()
    snap.texts.append(DrawingText("BOUNDARY_TEXT_AI", "N 56°36'12\" W", shed.point, height=1.0))
    plan = make_plan(snap, house=STAFFORD_HOUSE)
    moved = item_for(plan, "SUBJECT_SHED_OUTLINE")
    assert moved.point != pytest.approx(shed.point)
    blocker = ab._existing_text_box(snap.texts[-1], 1.0, {})
    assert not moved.box.intersects(blocker)


def test_field_notes_are_a_source_not_a_duplicate_or_obstacle():
    """Заметка полевика рядом с объектом не отменяет подпись и не мешает ей."""
    snap = build_scene()
    snap.texts.append(DrawingText("CONTEXT_TEXT", "SHED", (30.5, 90.0), height=1.8))
    snap.texts.append(DrawingText("FENCE_NOTE", "WOOD FENCE", (37.5, 50.0), height=1.3, rotation=math.pi / 2))
    baseline = make_plan(build_scene())
    plan = make_plan(snap)
    assert plan.duplicates == []
    assert len(plan.items) == len(baseline.items) == 18
    shed = item_for(plan, "SUBJECT_SHED_OUTLINE")
    assert shed.feature.source == "field_note" and shed.feature.note_text == "SHED"
    assert shed.point == pytest.approx((33.0, 91.0), abs=1e-6)  # по центру, поверх заметки
    # Подписи стоят там же, где стояли бы без заметок.
    for layer in ("SUBJECT_SHED_OUTLINE", "FENCE_WOOD_RIGHT"):
        assert item_for(plan, layer).point == pytest.approx(item_for(baseline, layer).point)


def test_dedup_checkbox_off_labels_everything_again():
    plan = make_plan(build_scene(with_existing=True), skip_existing=False)
    assert plan.duplicates == []
    assert len(plan.items) == 18
    # Существующие надписи остаются препятствиями: новые тексты их не закрывают.
    existing = ab._existing_text_box(
        DrawingText("D-TOPO-TEXT", "SHED", (31, 90.5), height=0.9), 1.0, {}
    )
    assert not item_for(plan, "SUBJECT_SHED_OUTLINE").box.intersects(existing)


def test_existing_label_far_from_object_is_not_a_duplicate():
    snap = build_scene()
    # «SHED» в 20 ft от сарая (радиус дедупликации — 6 ft).
    snap.texts.append(DrawingText("D-TOPO-TEXT", "SHED", (12, 80), height=0.9))
    plan = make_plan(snap)
    assert plan.duplicates == []
    assert item_for(plan, "SUBJECT_SHED_OUTLINE").text == "SHED"


def test_existing_leader_arrow_marks_object_as_labeled():
    """Готовая подпись стоит в стороне, но её выноска указывает на объект — дубликат не нужен."""
    snap = build_scene()
    snap.texts.append(DrawingText("D-TOPO-TEXT", "STEPS", (-10, 12), height=0.9))
    snap.leaders.append(
        DrawingLine("D-TOPO-TEXT", ((22.0, 24.0), (-5.0, 12.4), (-6.5, 12.4)), kind="AcDbLeader")
    )
    plan = make_plan(snap)
    assert [(dup.feature.layer, dup.existing_text) for dup in plan.duplicates] == [
        ("FRONT_STEPS", "STEPS")
    ]


def test_report_lists_counters_for_gui_log():
    plan = make_plan(build_scene(with_existing=True))
    result = ab.FeatureLabelResult(
        plan=plan, layer=plan.output_layer, created_labels=15, created_texts=4,
        created_mtexts=11, created_leaders=0,
    )
    report = result.report()
    assert "по заметкам полевого чертёжника (перевод в формулировки заказчика): 0" in report
    assert "по кодам и описаниям точек съёмки: 2" in report
    assert "по именам слоёв: 15" in report
    assert "ПОДПИСАНО: 15 (вдоль линии: 4, внутри контура: 7, у объекта: 4, выноска: 0)" in report
    assert "ПРОПУЩЕНО КАК УЖЕ ПОДПИСАННЫЕ: 3" in report
    assert "AddText — 4, AddMText — 11, выносок — 0" in report
    assert "XYZ ×1" in report
    assert "D-TOPO-TEXT" in report
    summary = result.summary()
    assert "по полевым заметкам: 0" in summary
    assert "по кодам точек: 2" in summary and "по слоям: 15" in summary
    assert "Подписано объектов: 15" in summary
    assert (
        result.found_by_notes, result.found_by_point_codes,
        result.found_by_layers, result.skipped_duplicates,
    ) == (0, 2, 15, 3)


# ------------------------------------------------ геометрия чтения из AutoCAD

def test_polyline_bulge_is_expanded_into_arc_points():
    # Полуокружность (bulge = 1) от (0,0) к (2,0): вершина дуги в (1,-1).
    points = ab._expand_polyline_bulges([(0.0, 0.0), (2.0, 0.0)], [1.0, 0.0], closed=False)
    assert points[0] == (0.0, 0.0) and points[-1] == (2.0, 0.0)
    assert len(points) > 10
    for x, y in points:
        assert math.hypot(x - 1.0, y) == pytest.approx(1.0, abs=1e-9)
    assert min(y for _x, y in points) == pytest.approx(-1.0, abs=1e-9)
    # Нулевые bulge не меняют полилинию.
    square = [(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0)]
    assert ab._expand_polyline_bulges(square, [0.0] * 4, closed=True) == square


def test_arc_points_cover_sweep_counter_clockwise():
    points = ab._arc_points((0.0, 0.0), 5.0, math.radians(350.0), math.radians(20.0))
    assert points[0] == pytest.approx((5.0 * math.cos(math.radians(350.0)), 5.0 * math.sin(math.radians(350.0))))
    assert points[-1] == pytest.approx((5.0 * math.cos(math.radians(20.0)), 5.0 * math.sin(math.radians(20.0))))
    assert all(math.hypot(x, y) == pytest.approx(5.0) for x, y in points)
    assert all(x > 4.6 for x, _y in points)  # короткая дуга через 0°, а не длинная через 180°


# ------------------------------------------------------- заглушка AutoCAD COM

class FakeEntity:
    def __init__(self, object_name, layer, **attrs):
        self.ObjectName = object_name
        self.Layer = layer
        for name, value in attrs.items():
            setattr(self, name, value)


class FakeLeader(FakeEntity):
    def Update(self):
        self.updated = True


class FakeModelSpace:
    def __init__(self, *, with_leader=True, font_scale=None):
        self.entities = []
        self.created = []
        self.probes = []  # пробные тексты измерения ширины шрифта (созданы и удалены)
        self.font_scale = font_scale
        if not with_leader:
            self.AddLeader = None  # hasattr() остаётся True, вызов падает — как отказ COM

    @property
    def Count(self):
        return len(self.entities)

    def Item(self, index):
        return self.entities[index]

    def _created(self, entity):
        self.entities.append(entity)
        self.created.append(entity)
        return entity

    def AddText(self, text, point, height):
        entity = FakeEntity("AcDbText", "0", TextString=text, InsertionPoint=list(point),
                            Height=height, Rotation=0.0, Alignment=None, TextAlignmentPoint=None)

        def delete():
            self.entities.remove(entity)
            self.created.remove(entity)
            self.probes.append(entity)

        entity.Delete = delete
        if self.font_scale is not None:
            # Габарит текста, как его вернул бы AutoCAD для шрифта другой ширины.
            width = ab.feature_text_width(text, height) * self.font_scale
            entity.GetBoundingBox = lambda: (
                (point[0], point[1], 0.0), (point[0] + width, point[1] + height, 0.0)
            )
        return self._created(entity)

    def AddMText(self, point, width, text):
        return self._created(
            FakeEntity("AcDbMText", "0", TextString=text, InsertionPoint=list(point),
                       Width=width, Height=0.0, Rotation=0.0, AttachmentPoint=1)
        )

    def AddLeader(self, coordinates, annotation, leader_type):
        return self._created(
            FakeLeader("AcDbLeader", "0", Coordinates=list(coordinates),
                       Annotation=annotation, Type=leader_type)
        )

    def AddLightWeightPolyline(self, coordinates):
        entity = FakeEntity("AcDbPolyline", "0", Coordinates=list(coordinates), Closed=False)
        entity.SetWidth = lambda *args: setattr(entity, "width", args)
        entity.bulges = {}
        entity.SetBulge = lambda index, value: entity.bulges.__setitem__(index, value)
        return self._created(entity)


class FakeLayer:
    def __init__(self, name):
        self.Name = name
        self.LayerOn = True
        self.Freeze = False
        self.Lock = False
        self.color = 7


class FakeLayers:
    def __init__(self):
        self.items = {"0": FakeLayer("0")}

    def Add(self, name):
        self.items[name] = FakeLayer(name)
        return self.items[name]

    def Item(self, name):
        return self.items[name]

    def __iter__(self):
        return iter(self.items.values())


class FakeTextStyle:
    def __init__(self, name):
        self.Name = name

    def SetFont(self, *args):
        self.font = args


class FakeTextStyles:
    def __init__(self):
        self.items = {"Standard": FakeTextStyle("Standard")}

    def Add(self, name):
        self.items[name] = FakeTextStyle(name)
        return self.items[name]

    def Item(self, name):
        return self.items[name]

    def __iter__(self):
        return iter(self.items.values())


class FakeSelectionSet:
    def __init__(self, owner, name):
        self.owner = owner
        self.name = name
        self.picked = []

    def SelectOnScreen(self):
        if self.owner.cancel:
            raise RuntimeError("-2147352567: user cancelled")
        self.picked = list(self.owner.to_pick)

    @property
    def Count(self):
        return len(self.picked)

    def Item(self, index):
        return self.picked[index]

    def Delete(self):
        self.owner.sets.pop(self.name, None)
        self.owner.deleted += 1


class FakeSelectionSets:
    def __init__(self):
        self.sets = {}
        self.to_pick = []
        self.cancel = False
        self.deleted = 0

    def Item(self, name):
        return self.sets[name]

    def Add(self, name):
        self.sets[name] = FakeSelectionSet(self, name)
        return self.sets[name]


class FakeDoc:
    def __init__(self, *, with_leader=True, font_scale=None):
        self.ModelSpace = FakeModelSpace(with_leader=with_leader, font_scale=font_scale)
        self.Layers = FakeLayers()
        self.TextStyles = FakeTextStyles()
        self.SelectionSets = FakeSelectionSets()
        self.ActiveSpace = 1
        self.undo_marks = []

    def GetVariable(self, name):
        return 2  # feet

    def StartUndoMark(self):
        self.undo_marks.append("start")

    def EndUndoMark(self):
        self.undo_marks.append("end")

    def Regen(self, which):
        self.regens = getattr(self, "regens", []) + [which]


class FakeApp:
    def __init__(self, doc):
        self.doc = doc
        self.updated = 0

    @property
    def ActiveDocument(self):
        return self.doc

    def Update(self):
        self.updated += 1


def assert_house_label_on_layer_zero(created, text_height):
    """Описание дома — MText в слое 0 высотой 1.5 ft при подписях 0.9 ft; остальное — D-TOPO-TEXT."""
    on_zero = [entity for entity in created if entity.Layer == "0"]
    assert len(on_zero) == 1 and on_zero[0].ObjectName == "AcDbMText"
    assert on_zero[0].Height == pytest.approx(1.5 / 0.9 * text_height)
    assert {entity.Layer for entity in created if entity.Layer != "0"} == {"D-TOPO-TEXT"}


def fake_autocad(snapshot, *, with_leader=True, font_scale=None):
    """Переносит синтетический чертёж в заглушку ModelSpace (как объекты AutoCAD)."""
    doc = FakeDoc(with_leader=with_leader, font_scale=font_scale)
    space = doc.ModelSpace
    for line in snapshot.lines:
        doc.Layers.items.setdefault(line.layer, FakeLayer(line.layer))
        if len(line.points) == 2 and not line.closed:
            (x0, y0), (x1, y1) = line.points
            space.entities.append(
                FakeEntity("AcDbLine", line.layer, StartPoint=(x0, y0, 0.0), EndPoint=(x1, y1, 0.0))
            )
        else:
            flat = [value for point in line.points for value in point]
            space.entities.append(
                FakeEntity("AcDbPolyline", line.layer, Coordinates=tuple(flat), Closed=line.closed)
            )
    for text in snapshot.texts:
        doc.Layers.items.setdefault(text.layer, FakeLayer(text.layer))
        space.entities.append(
            FakeEntity(text.kind, text.layer, TextString=text.text,
                       InsertionPoint=(text.point[0], text.point[1], 0.0),
                       Height=text.height, Rotation=text.rotation)
        )
    for survey in snapshot.survey_points:
        doc.Layers.items.setdefault(survey.layer, FakeLayer(survey.layer))
        space.entities.append(fake_point_block(survey))
    for symbol in snapshot.symbols:
        doc.Layers.items.setdefault(symbol.layer, FakeLayer(symbol.layer))
        space.entities.append(
            FakeEntity(
                "AcDbBlockReference", symbol.layer, HasAttributes=False,
                InsertionPoint=(symbol.point[0], symbol.point[1], 0.0),
                EffectiveName=symbol.name, Rotation=symbol.rotation,
                XScaleFactor=symbol.x_scale, YScaleFactor=symbol.y_scale,
            )
        )
    return FakeApp(doc), doc


def fake_point_block(survey, tags=("PT#", "ELEV2", "DESC2")):
    """Блок точки съёмки ``srvpno3``: атрибуты номер / отметка / описание."""
    values = (survey.number, survey.elevation, survey.description)
    attributes = tuple(
        FakeEntity("AcDbAttribute", "PNTDESC", TagString=tag, TextString=value)
        for tag, value in zip(tags, values)
    )
    return FakeEntity(
        "AcDbBlockReference", survey.layer, HasAttributes=True,
        InsertionPoint=(survey.point[0], survey.point[1], 0.0),
        EffectiveName=survey.block or "srvpno3", GetAttributes=lambda: attributes,
    )


@pytest.fixture()
def com(monkeypatch):
    monkeypatch.setattr(ab, "_co_initialize", lambda: None)
    monkeypatch.setattr(ab, "_variant_points", lambda points: [c for p in points for c in p])
    monkeypatch.setattr(ab, "_variant_point", lambda point, z=0.0: [point[0], point[1], z])
    monkeypatch.setattr(ab, "_null_dispatch", lambda: None)

    def connect(snapshot, feature_cfg=None, **kwargs):
        cfg = load_config()
        if feature_cfg:
            cfg["feature_labeling"] = {**cfg["feature_labeling"], **feature_cfg}
        builder = AcadBuilder(cfg)
        app, doc = fake_autocad(snapshot, **kwargs)
        builder._app, builder._doc = app, doc
        return builder, app, doc

    return connect


def test_read_drawing_snapshot_from_fake_autocad(com):
    source = build_scene(with_existing=True)
    builder, _app, doc = com(source)
    # Объекты, которые Шаг 2 не читает: блок и штриховка.
    doc.ModelSpace.entities.append(FakeEntity("AcDbBlockReference", "PNTS"))
    doc.ModelSpace.entities.append(FakeEntity("AcDbHatch", "BUILDING_OUTLINE"))
    snapshot = builder.read_drawing_snapshot(doc)
    assert snapshot.entity_count == len(doc.ModelSpace.entities)
    assert len(snapshot.lines) == len(source.lines)
    assert len(snapshot.texts) == len(source.texts)
    assert snapshot.read_errors == 0
    restored = recognize(snapshot)
    assert (restored.by_point_codes, restored.by_layers, restored.by_user) == (2, 15, 1)


def test_hidden_layers_are_ignored_but_point_codes_still_read(com):
    builder, _app, doc = com(build_scene())
    doc.Layers.items["FENCE_WOOD_RIGHT"].Freeze = True
    doc.Layers.items["SUBJECT_SHED_OUTLINE"].LayerOn = False
    doc.Layers.items["POINT_LABELS"].LayerOn = False  # коды точек часто выключены
    snapshot = builder.read_drawing_snapshot(doc)
    layers = {line.layer for line in snapshot.lines}
    assert "FENCE_WOOD_RIGHT" not in layers and "SUBJECT_SHED_OUTLINE" not in layers
    assert snapshot.skipped_hidden == 5  # 1 забор + 4 стороны сарая
    codes = [text for text in snapshot.texts if text.layer == "POINT_LABELS"]
    assert len(codes) == 7 and all(text.hidden for text in codes)
    result = recognize(snapshot)
    assert result.by_point_codes == 2  # привязка заборов по кодам сохраняется


def test_label_features_draws_into_d_topo_text(com):
    builder, app, doc = com(build_scene())
    messages = []
    result = builder.label_features(mode="auto", house_text=HOUSE, scale=16.0, progress=messages.append)

    space = doc.ModelSpace
    assert result.layer == "D-TOPO-TEXT"
    assert result.failed == [] and result.warnings == []
    assert result.created_labels == 18
    # С 3.7 подписи объектов — однострочный TEXT, MText только описание дома (слой 0).
    assert (result.created_texts, result.created_mtexts, result.created_leaders) == (19, 1, 0)
    assert result.plan.text_height == pytest.approx(0.9)
    # Все подписи — в D-TOPO-TEXT; описание дома — в слое 0, как у заказчика.
    assert_house_label_on_layer_zero(space.created, result.plan.text_height)
    assert "D-TOPO-TEXT" in doc.Layers.items
    assert doc.undo_marks == ["start", "end"]
    assert app.updated == 1
    assert any("Читаю" in message for message in messages)
    assert result.hidden_layers == []  # заметок полевика на чертеже нет — слои не трогаются

    texts = [e for e in space.created if e.ObjectName == "AcDbText"]
    mtexts = [e for e in space.created if e.ObjectName == "AcDbMText"]
    assert sorted(set(e.TextString for e in texts)) == [
        "C.L.F.", "CHAIN LINK FENCE", "CONC. WALK", "DECK", "DRIVEWAY", "FRAME", "GATE",
        "GRASS", "O.P.", "PLANTER", "SHED", "STEPS", "VINYL FENCE", "WOOD FENCE",
    ]
    # Каждый TEXT выровнен по центру (не по левой линии, как MText в чужих CAD).
    assert all(e.Alignment == ab.AC_ALIGNMENT_MIDDLE_CENTER for e in texts)
    assert all(e.TextAlignmentPoint for e in texts)
    assert all(e.Height == pytest.approx(0.9) for e in texts + mtexts if e.Layer != "0")
    wood = next(e for e in texts if e.TextString == "WOOD FENCE")
    assert math.degrees(wood.Rotation) == pytest.approx(90.0)  # вертикальный текст — снизу вверх

    house = next(e for e in mtexts if e.TextString == HOUSE)
    assert house.AttachmentPoint == ab.AC_ATTACHMENT_MIDDLE_CENTER
    assert (house.Layer, house.Height) == ("0", pytest.approx(1.5, abs=0.01))
    assert house.InsertionPoint[:2] == pytest.approx([23.0, 46.0])
    assert house.Width == 0.0
    assert [e.Layer for e in mtexts if e is not house] == []  # MText — только дом
    # Двухстрочная подпись калитки — два TEXT друг под другом, а не один MText.
    gates = [e for e in texts if e.TextString == "GATE"]
    assert gates and all(e.Layer == "D-TOPO-TEXT" for e in gates)
    assert {e.TextString for e in texts} >= {
        "SHED", "FRAME", "DECK", "O.P.", "STEPS", "DRIVEWAY", "CONC. WALK", "PLANTER",
    }
    # Столбы, колодцы и LOW TIMBER EDGE заказчик не подписывает.
    created = {e.TextString for e in texts + mtexts}
    assert not created & {"UTILITY POLE", "MANHOLE", "LOW TIMBER EDGE"}

    report = result.report()
    assert "по кодам и описаниям точек съёмки: 2" in report
    assert "по именам слоёв: 15" in report
    assert "ПОДПИСАНО: 18" in report


def test_object_labels_are_single_line_text_centered_for_other_cad(com):
    """
    Подписи объектов — однострочный TEXT по центру (с 3.7).

    В Carlson Survey и чужих версиях AutoCAD MText с привязкой middle-center встаёт
    по левой линии и «разъезжается»; однострочный TEXT с Alignment = MiddleCenter
    стоит на месте. Двухстрочная подпись (калитка) — два TEXT друг под другом с тем
    же шагом, что у MText; описание дома остаётся MText, как в шаблоне заказчика.
    """
    builder, _app, doc = com(build_stafford_scene())
    result = builder.label_features(house_text=STAFFORD_HOUSE, scale=16.0)
    space = doc.ModelSpace
    texts = [entity for entity in space.created if entity.ObjectName == "AcDbText"]
    mtexts = [entity for entity in space.created if entity.ObjectName == "AcDbMText"]
    assert result.text_mode == "text"
    # MText остался только у описания дома (слой 0, крупный стиль RDS).
    assert [(entity.Layer, entity.TextString) for entity in mtexts] == [
        ("0", "1 STY\\PFRAME\\P(BRICK FACE)\\PW/BSMNT\\PNo. 87")
    ]
    # Каждый TEXT выровнен по центру и имеет точку выравнивания — не по левой линии.
    assert all(entity.Alignment == ab.AC_ALIGNMENT_MIDDLE_CENTER for entity in texts)
    assert all(entity.TextAlignmentPoint for entity in texts)
    # Калитка «V.FE. / GATE»: две строки на расстоянии 1.67 высоты, верхняя — V.FE.
    height = result.plan.text_height
    vinyl = next(e for e in texts if e.TextString == "V.FE." and e.Rotation == 0.0)
    gate = next(
        e for e in texts
        if e.TextString == "GATE" and abs(e.TextAlignmentPoint[0] - vinyl.TextAlignmentPoint[0]) < 1e-6
    )
    assert vinyl.TextAlignmentPoint[1] - gate.TextAlignmentPoint[1] == pytest.approx(1.67 * height)
    # Сводной строки «чем созданы подписи» в отчёте больше нет (заказчик, 15.11.2026):
    # осталась короткая строка счётчиков «создано в AutoCAD: AddText — …».
    report = result.report()
    assert "подписи объектов — однострочный TEXT по центру" not in report
    assert f"AddText — {result.created_texts}" in report

    # Прежнее поведение возвращается настройкой: вдоль линии — TEXT, остальное — MText.
    legacy, _app2, doc2 = com(build_stafford_scene(), feature_cfg={"label_text_kind": "auto"})
    old = legacy.label_features(house_text=STAFFORD_HOUSE, scale=16.0)
    assert old.text_mode == "text"  # в «auto» тоже есть TEXT — строка отчёта не врёт
    assert old.created_mtexts > result.created_mtexts
    assert "C.L.F.\\PGATE" in {e.TextString for e in doc2.ModelSpace.created
                                 if e.ObjectName == "AcDbMText"}


@pytest.mark.parametrize("kind", ["text", "mtext", "auto", "old"])
def test_leaders_keep_mtext_and_arrow_in_every_label_mode(com, kind):
    """
    Выноска — всегда MText + Leader, при любой настройке вида подписей.

    Заказчик, 15.11.2026: «на выносках оставляем MText, однозначно, иначе будет
    теряться связь стрелочки и текста». Аннотацией объекта Leader AutoCAD принимает
    только MText, поэтому даже в режиме однострочного TEXT подпись с выноской
    (``CONC. STEPS`` — ступени в проходе между домом и границей) не превращается в TEXT.
    """
    builder, _app, doc = com(build_stafford_scene(), feature_cfg={"label_text_kind": kind})
    result = builder.label_features(house_text=STAFFORD_HOUSE, text_height=1.1)
    assert result.failed == [] and result.warnings == []
    assert (result.created_leaders, result.created_leader_objects) == (2, 2)
    assert result.created_leader_attached == 2
    space = doc.ModelSpace
    mtexts = [e for e in space.created if e.ObjectName == "AcDbMText"]
    texts = [e for e in space.created if e.ObjectName == "AcDbText"]
    leaders = [e for e in space.created if e.ObjectName == "AcDbLeader"]
    assert len(leaders) == 2
    # Текст выносок — MText, и именно он привязан к стрелке.
    annotations = {id(leader.Annotation) for leader in leaders}
    assert annotations <= {id(entity) for entity in mtexts}
    assert result.created_leader_attached == len(annotations)
    # Однострочных TEXT ровно столько, сколько строк у подписей БЕЗ выноски:
    # подпись с выноской в режиме «text» в TEXT не превращается никогда.
    expected = sum(
        len([line for line in item.text.split("\n") if line.strip()]) or 1
        for item in result.plan.items
        if builder._feature_label_text_mode(item) == "text"
    )
    assert len(texts) == expected


def test_stafford_field_drawing_is_labeled_like_the_customer(com):
    """Полевой чертёж «87 Stafford»: заметки полевика -> подписи заказчика в D-TOPO-TEXT.

    Здесь проверяется состав подписей, поэтому взят режим ``auto`` (как до 3.7):
    двухстрочная подпись лежит одним MText и её текст сравнивается целиком. Вид
    объектов по умолчанию (однострочный TEXT) проверяет следующий тест.
    """
    builder, _app, doc = com(build_stafford_scene(), feature_cfg={"label_text_kind": "auto"})
    result = builder.label_features(house_text=STAFFORD_HOUSE, scale=16.0)
    assert result.failed == [] and result.warnings == []
    rec = result.recognition
    assert (rec.by_notes, rec.by_point_codes, rec.by_layers, rec.by_user) == (23, 1, 1, 1)
    assert result.skipped_duplicates == 0
    assert result.created_labels == 26
    assert result.created_leaders == 0  # при тексте 0.9 ft всё встаёт без выносок

    space = doc.ModelSpace
    texts = sorted(e.TextString for e in space.created if e.ObjectName == "AcDbText")
    mtexts = sorted(e.TextString for e in space.created if e.ObjectName == "AcDbMText")
    assert texts == ["C.L.F."] + ["CHAIN LINK FENCE"] * 3 + ["V.FE."] * 2 + ["VINYL FENCE"]
    assert mtexts == sorted([
        "1 STY\\PFRAME\\P(BRICK FACE)\\PW/BSMNT\\PNo. 87",
        "FRAME\\PDECK", "FRAME\\PSTEPS", "SHED", "PLANTER", "EARTH", "GRASS",
        "DECO. PAVERS", "DECO. PAVERS", "O.P.", "O.P.", "CONC.\\PSTEPS", "CONC.\\PSTEPS",
        "BRICK\\PSTEPS", "CONC. WALK",
        "C.L.F.\\PGATE", "V.FE.\\PGATE", "V.FE.\\PGATE", "V.FE.\\PGATE",
    ])
    assert_house_label_on_layer_zero(space.created, result.plan.text_height)
    assert not [e for e in space.created if e.ObjectName in {"AcDbLeader", "AcDbPolyline"}]
    # Пробный текст измерения ширины шрифта создан и сразу удалён.
    assert [e.TextString for e in space.probes] == [ab.TEXT_WIDTH_PROBE]
    assert result.plan.width_scale == 1.0  # заглушка не отдаёт габарит — расчётная ширина

    # Слои заметок выключены (объекты не удалены), остальные слои не тронуты.
    assert sorted(result.hidden_layers) == [
        "CONTEXT_SURFACE_TEXT", "CONTEXT_TEXT", "ENTRANCE_TEXT", "FENCE_NOTE",
        "LOW_EDGE_NOTE", "SURFACE_TEXT",
    ]
    assert all(doc.Layers.items[name].LayerOn is False for name in result.hidden_layers)
    assert doc.Layers.items["BUILDING_OUTLINE"].LayerOn is True
    assert doc.Layers.items["POINT_LABELS"].LayerOn is True
    assert doc.Layers.items["PROPERTY_LINES_APPROX"].LayerOn is True

    report = result.report()
    assert "по заметкам полевого чертёжника (перевод в формулировки заказчика): 23" in report
    assert "БЕЗ ПОДПИСИ" not in report  # ни CODE_CREW, ни соседнего дома
    assert "ДОМ НЕ ПОДПИСАН" not in report
    assert "короткий забор за пределами участка" not in report  # они подписаны сокращениями
    assert "Слои полевых заметок выключены" in report
    assert "Заметки, которые не переносятся по правилам: «HOUSE»; «LOW TIMBER EDGE»; «NEIGHBOUR HOUSE»" in report
    assert "Коды точек не из словаря" not in report  # BLC, OP, STEP, SW… — коды углов объектов
    assert "FRAME DECK — крыльцо/терраса REAR_DECK_EDGE" in report
    assert "полевая заметка «WOOD PORCH»" in report

    # Повторный запуск читает заметки из выключенных слоёв и ничего не удваивает.
    created = len(space.created)
    second = builder.label_features(house_text=STAFFORD_HOUSE, scale=16.0)
    assert second.created_labels == 0 and second.skipped_duplicates == 26
    assert second.recognition.by_notes == 23
    assert len(space.created) == created


def test_leader_is_a_spline_leader_object_like_the_customers(com):
    """
    У заказчика выноска — объект Leader (QLEADER): сплайн со стрелкой по двум
    точкам, MText привязан к выноске, стрелка — из размерного стиля чертежа.
    """
    assert fl.DEFAULT_FEATURE_CONFIG["leader_style"] == "spline"
    builder, _app, doc = com(build_stafford_scene())
    result = builder.label_features(house_text=STAFFORD_HOUSE, text_height=1.1)
    assert result.failed == [] and result.warnings == []
    assert (result.created_leaders, result.created_leader_objects) == (2, 2)
    space = doc.ModelSpace
    assert not [e for e in space.created if e.ObjectName == "AcDbPolyline"]
    leaders = [e for e in space.created if e.ObjectName == "AcDbLeader"]
    assert len(leaders) == 2
    texts = {id(e) for e in space.created if e.ObjectName == "AcDbMText"}
    for leader in leaders:
        assert leader.Type == ab.AC_LEADER_SPLINE_WITH_ARROW == 3
        assert len(leader.Coordinates) == 6  # остриё у объекта и конец у текста
        # Текст привязан к выноске (QLEADER): при переносе текста она идёт за ним.
        assert id(leader.Annotation) in texts
        assert leader.Layer == "D-TOPO-TEXT"
        # В заглушке размерного стиля нет: стрелка 1.333 высоты текста (1.2 ft при 0.9 ft).
        assert leader.ArrowheadSize == pytest.approx(1.1 * 1.333) and leader.ScaleFactor == 1.0
    assert result.created_leader_attached == 2
    assert "выносок — 2 (объектов Leader: 2, из них с привязанным текстом (QLEADER): 2)" in result.report()

    # Стрелка размерного стиля чертежа, соразмерная тексту, не трогается.
    builder, _app, doc = com(build_stafford_scene())
    original = doc.ModelSpace.AddLeader

    def styled_leader(coordinates, annotation, leader_type):
        leader = original(coordinates, annotation, leader_type)
        leader.ArrowheadSize, leader.ScaleFactor = 1.2, 1.0
        return leader

    doc.ModelSpace.AddLeader = styled_leader
    builder.label_features(house_text=STAFFORD_HOUSE, text_height=1.1)
    leaders = [e for e in doc.ModelSpace.created if e.ObjectName == "AcDbLeader"]
    assert [e.ArrowheadSize for e in leaders] == [1.2, 1.2]

    # AutoCAD не создал Leader: дуга полилинией и одно предупреждение.
    builder, _app, doc = com(build_stafford_scene(), with_leader=False)
    result = builder.label_features(house_text=STAFFORD_HOUSE, text_height=1.1)
    assert result.failed == [] and result.created_labels == 26
    assert (result.created_leaders, result.created_leader_objects) == (2, 0)
    assert len(result.warnings) == 1 and "полилинией" in result.warnings[0]
    arrows = [e for e in doc.ModelSpace.created if e.ObjectName == "AcDbPolyline"]
    assert len(arrows) == 2 and all(set(e.bulges) == {0, 1} for e in arrows)
    assert "выносок — 2 (полилиний со стрелкой: 2)" in result.report()


def test_labels_use_the_text_style_of_the_drawing(com):
    """Подписи заказчика набраны стилем ROMANS: если он есть в чертеже, свой стиль не создаётся."""
    builder, _app, doc = com(load_brooklyn())
    doc.TextStyles.Add("ROMANS")
    doc.TextStyles.Add("RDS")
    result = builder.label_features(house_text=BROOKLYN_HOUSE)
    assert result.failed == [] and (result.text_style, result.text_style_native) == ("ROMANS", True)
    created = [e for e in doc.ModelSpace.created if e.ObjectName in {"AcDbText", "AcDbMText"}]
    assert {e.StyleName for e in created if e.Layer == "D-TOPO-TEXT"} == {"ROMANS"}
    assert {e.StyleName for e in created if e.Layer == "0"} == {"RDS"}
    assert set(doc.TextStyles.items) == {"Standard", "ROMANS", "RDS"}  # свой стиль не создан
    assert "Стиль текста подписей: ROMANS (стиль чертежа)" in result.report()

    # Стиля заказчика нет — создаётся стиль программы.
    builder, _app, doc = com(load_brooklyn())
    result = builder.label_features(house_text=BROOKLYN_HOUSE)
    assert (result.text_style, result.text_style_native) == ("BOUNDARY", False)
    assert "BOUNDARY" in doc.TextStyles.items
    assert "Стиль текста подписей: BOUNDARY (создан программой" in result.report()


def test_arc_leader_is_a_polyline_with_wedge_arrow(com):
    """``leader_style: "arc"`` — дуговая полилиния с клиновидной стрелкой (AddLeader не используется)."""
    builder, _app, doc = com(build_stafford_scene(), feature_cfg={"leader_style": "arc"})
    result = builder.label_features(house_text=STAFFORD_HOUSE, text_height=1.1)
    assert result.failed == [] and result.warnings == []
    assert result.created_labels == 26 and result.created_leaders == 2
    space = doc.ModelSpace
    assert not [e for e in space.created if e.ObjectName == "AcDbLeader"]
    arrows = [e for e in space.created if e.ObjectName == "AcDbPolyline"]
    assert len(arrows) == 2
    for arrow in arrows:
        assert len(arrow.Coordinates) == 6  # остриё, шейка стрелки, конец у текста
        assert arrow.width[1] == 0.0 and arrow.width[2] > 0.0
        assert set(arrow.bulges) == {0, 1} and all(arrow.bulges.values())
        assert arrow.Layer == "D-TOPO-TEXT"


def test_text_width_is_measured_in_autocad_when_font_differs(com):
    # Шрифт стиля на четверть шире расчётного romans.shx: план строится по измеренной ширине.
    builder, _app, doc = com(build_stafford_scene(), font_scale=1.25)
    result = builder.label_features(house_text=STAFFORD_HOUSE, scale=16.0)
    assert result.plan.width_scale == pytest.approx(1.25)
    assert "ширина текста ×1.25" in result.report()
    assert [e.TextString for e in doc.ModelSpace.probes] == [ab.TEXT_WIDTH_PROBE]
    fence = next(i for i in result.plan.items if i.text == "CHAIN LINK FENCE")
    length = max(fence.box.bounds[2] - fence.box.bounds[0], fence.box.bounds[3] - fence.box.bounds[1])
    assert length == pytest.approx(1.25 * ab.feature_text_width("CHAIN LINK FENCE", 0.9))
    assert result.created_labels == 26 and result.failed == []

    # Отличие в пределах 4 % — шрифт расчётный, поправка не вводится.
    builder, _app, doc = com(build_stafford_scene(), font_scale=1.02)
    result = builder.label_features(house_text=STAFFORD_HOUSE, scale=16.0)
    assert result.plan.width_scale == 1.0
    assert "ширина текста ×" not in result.report()

    # Пробный расчёт чертёж не трогает совсем — пробного текста тоже нет.
    builder, _app, doc = com(build_stafford_scene(), font_scale=1.25)
    result = builder.label_features(house_text=STAFFORD_HOUSE, dry_run=True)
    assert doc.ModelSpace.probes == [] and result.plan.width_scale == 1.0


def test_only_layers_with_recognized_notes_are_switched_off(com):
    snap = build_stafford_scene()
    # Слой подходит под шаблон *_TEXT, но заметок полевика в нём нет.
    snap.texts.append(DrawingText("TITLE_TEXT", "SCALE: 1\"=16'", (10.0, -30.0), height=2.0))
    builder, _app, doc = com(snap)
    result = builder.label_features(house_text=STAFFORD_HOUSE, scale=16.0)
    assert "TITLE_TEXT" not in result.hidden_layers
    assert doc.Layers.items["TITLE_TEXT"].LayerOn is True
    assert {"CONTEXT_SURFACE_TEXT", "LOW_EDGE_NOTE"} <= set(result.hidden_layers)
    assert "«SCALE: 1\"=16'»" in result.report()  # заметка без правила названа в отчёте


def test_field_note_layers_stay_visible_when_checkbox_is_off(com):
    builder, _app, doc = com(build_stafford_scene())
    result = builder.label_features(house_text=STAFFORD_HOUSE, hide_field_notes=False)
    assert result.hidden_layers == []
    assert doc.Layers.items["FENCE_NOTE"].LayerOn is True
    assert "Слои полевых заметок выключены" not in result.report()


def test_second_run_creates_no_duplicates(com):
    builder, _app, doc = com(build_scene())
    first = builder.label_features(house_text=HOUSE)
    assert first.created_labels == 18
    created = len(doc.ModelSpace.created)

    second = builder.label_features(house_text=HOUSE)
    assert second.created_labels == 0
    assert second.skipped_duplicates == 18
    assert len(doc.ModelSpace.created) == created

    # С выключенной проверкой подписи создаются повторно и не ложатся на прежние.
    third = builder.label_features(house_text=HOUSE, skip_existing=False)
    assert third.created_labels == 18


def test_dry_run_does_not_touch_drawing(com):
    builder, _app, doc = com(build_scene())
    result = builder.label_features(house_text=HOUSE, dry_run=True)
    assert result.dry_run is True
    assert len(result.plan.items) == 18
    assert result.created_labels == 0
    assert doc.ModelSpace.created == []
    assert "D-TOPO-TEXT" not in doc.Layers.items
    assert "ПРОБНЫЙ РАСЧЁТ" in result.report()


def test_pick_mode_uses_selected_boundary(com):
    snap = build_scene()
    # В слоях границы нет ни одного контура: участок начерчен в постороннем слое.
    for index, line in enumerate(snap.lines):
        if line.layer in {"PROPERTY_LINE", "M-TAX-LINE"}:
            snap.lines[index] = DrawingLine("SURVEY_SKETCH", line.points, closed=line.closed)
    builder, _app, doc = com(snap)
    with pytest.raises(fl.FeatureLabelError):
        builder.label_features(mode="auto", house_text=HOUSE)
    assert doc.ModelSpace.created == []

    lot_sides = [
        entity for entity in doc.ModelSpace.entities
        if entity.Layer == "SURVEY_SKETCH"
        and {tuple(entity.StartPoint[:2]), tuple(entity.EndPoint[:2])}
        <= {(0, 0), (40, 0), (40, 100), (0, 100)}
    ]
    assert len(lot_sides) >= 4
    doc.SelectionSets.to_pick = lot_sides
    result = builder.label_features(mode="pick", house_text=HOUSE)
    assert result.mode == "pick"
    assert result.recognition.boundary.method == "pick"
    assert result.recognition.boundary.area_sqft == pytest.approx(4000.0, abs=0.01)
    assert result.created_labels == 18
    assert doc.SelectionSets.sets == {} and doc.SelectionSets.deleted == 1
    assert "указана в AutoCAD" in result.report()


def test_pick_cancel_and_empty_selection_leave_drawing_untouched(com):
    builder, _app, doc = com(build_scene())
    doc.SelectionSets.cancel = True
    with pytest.raises(AcadBuilderError, match="отмен"):
        builder.label_features(mode="pick")
    doc.SelectionSets.cancel = False
    doc.SelectionSets.to_pick = [e for e in doc.ModelSpace.entities if e.ObjectName == "AcDbText"]
    with pytest.raises(AcadBuilderError, match="не выбрано"):
        builder.label_features(mode="pick")
    assert doc.ModelSpace.created == []
    assert doc.SelectionSets.sets == {}


def test_cancelled_job_does_not_draw(com):
    builder, _app, doc = com(build_scene())
    with pytest.raises(AcadBuilderError, match="отмен"):
        builder.label_features(house_text=HOUSE, should_continue=lambda: False)
    assert doc.ModelSpace.created == []


def test_leader_style_leader_uses_addleader_and_falls_back_to_polyline(com):
    # leader_style = "leader": объект AddLeader вместо дуговой полилинии.
    # Текст 1.1 ft: двум подписям ступеней нужна выноска.
    builder, _app, doc = com(build_stafford_scene(), feature_cfg={"leader_style": "leader"})
    result = builder.label_features(house_text=STAFFORD_HOUSE, text_height=1.1)
    assert result.created_leaders == 2 and result.warnings == []
    leaders = [e for e in doc.ModelSpace.created if e.ObjectName == "AcDbLeader"]
    assert len(leaders) == 2
    assert leaders[0].Type == ab.AC_LEADER_LINE_WITH_ARROW
    assert len(leaders[0].Coordinates) in (6, 9)  # стрелка, излом (и полка, если текст сбоку)
    assert not [e for e in doc.ModelSpace.created if e.ObjectName == "AcDbPolyline"]

    # AddLeader недоступен: прямая полилиния со стрелкой и одно предупреждение.
    builder, _app, doc = com(
        build_stafford_scene(), feature_cfg={"leader_style": "leader"}, with_leader=False
    )
    result = builder.label_features(house_text=STAFFORD_HOUSE, text_height=1.1)
    assert result.created_labels == 26
    assert result.created_leaders == 2
    assert result.failed == []
    assert len(result.warnings) == 1 and "полилинией" in result.warnings[0]
    assert result.created_leader_objects == 0
    arrows = [e for e in doc.ModelSpace.created if e.ObjectName == "AcDbPolyline"]
    assert len(arrows) == 2
    assert {e.Layer for e in arrows} == {"D-TOPO-TEXT"}
    assert all(e.width[1] == 0.0 and e.width[2] > 0.0 for e in arrows)  # клин-стрелка у объекта
    assert arrows[0].bulges == {}


def test_existing_output_layer_keeps_color_and_is_thawed(com):
    builder, _app, doc = com(build_scene())
    layer = doc.Layers.Add("D-TOPO-TEXT")
    layer.color = 3
    layer.Freeze = True
    layer.LayerOn = False
    builder.label_features(house_text=HOUSE)
    assert layer.color == 3
    assert layer.Freeze is False and layer.LayerOn is True


def test_feature_text_height_follows_scale_and_config():
    cfg = load_config()
    builder = AcadBuilder(cfg)
    assert builder.feature_text_height(16.0) == pytest.approx(0.9)
    assert builder.feature_text_height(20.0) == pytest.approx(0.9 * 20.0 / 16.0)
    assert builder.feature_text_height(20.0, text_height=1.5) == pytest.approx(1.5)
    fixed = dict(cfg)
    fixed["feature_labeling"] = {"text_height_ft": 1.2}
    assert AcadBuilder(fixed).feature_text_height(30.0) == pytest.approx(1.2)


# ---------------------------------------------------------------------------
# Типовой чертёж геодезиста: описания объектов в атрибутах блоков точек
# ---------------------------------------------------------------------------

ILION_HOUSE = "2 STY\nFRAME\nNo. 12"


def load_field_fixture(name):
    """
    Полевой чертёж из сжатой записи дампа inspect_dwg.py (``tests/data/<name>``;
    запись делает ``tests/dump_to_fixture.py``).
    """
    snap = DrawingSnapshot()
    for raw in (ROOT / "tests" / "data" / name).read_text(encoding="utf-8").splitlines():
        row = raw.strip()
        if not row or row.startswith("#"):
            continue
        parts = row.split()
        if parts[0] == "L":
            x0, y0, x1, y1 = (float(value) for value in parts[2:6])
            snap.lines.append(DrawingLine(layer=parts[1], points=((x0, y0), (x1, y1))))
        elif parts[0] == "P":
            head, description = row.split("|", 1)
            _kind, layer, number, x, y = head.split()
            snap.survey_points.append(
                fl.SurveyPoint(layer=layer, number=number, description=description.strip(),
                               point=(float(x), float(y)), elevation="100.00", block="srvpno3")
            )
        elif parts[0] == "T":
            head, text = row.split("|", 1)
            _kind, layer, x, y, height, rotation = head.split()
            snap.texts.append(
                DrawingText(layer=layer, text=text.strip(), point=(float(x), float(y)),
                            height=float(height), rotation=math.radians(float(rotation)),
                            kind="AcDbMText")
            )
        elif parts[0] == "B":
            # Вставка блока без атрибутов: условный знак (столб WUP, стрелка севера).
            snap.symbols.append(
                fl.DrawingSymbol(layer=parts[1], name=parts[2],
                                 point=(float(parts[3]), float(parts[4])),
                                 rotation=math.radians(float(parts[5])) if len(parts) > 5 else 0.0)
            )
    return snap


def load_ilion():
    """Полевой чертёж «12 Ilion Place, Staten Island» (отдельно стоящий дом)."""
    return load_field_fixture("ilion_field.txt")


def load_brooklyn():
    """Полевой чертёж «6910 15th Avenue, Brooklyn» (дом блокированной застройки)."""
    return load_field_fixture("brooklyn_field.txt")


def load_aspinwall():
    """Полевой чертёж «225 Aspinwall Street, Staten Island» (блоки точек SRVPNO5, описания с «NE»)."""
    return load_field_fixture("aspinwall_field.txt")


def ilion_features(result, label, kind=None):
    return [
        f for f in result.features
        if f.flat_label == label and (kind is None or f.kind == kind)
    ]


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("VINYL FENCE", ("VINYL FENCE", "fence")),
        ("F E C", ("CHAIN LINK FENCE", "fence")),
        ("o porch", ("O.P.", "porch")),
        ("CONC-SW", ("", "")),          # кромка тротуара: материал заказчик берёт с фото
        ("CANTILEVER", ("CANTIL.", "overhang")),
        ("POOL", ("POOL\nAREA {area} Sq.Ft.", "pool")),
        ("SHED1", ("SHED", "shed")),    # номер линии Carlson в конце описания
        ("F E C 2", ("CHAIN LINK FENCE", "fence")),
        ("BL", ("", "")),               # building line: точка на стене, угол не снят
        ("EDG  DWY", ("DRIVEWAY", "surface")),
        ("BOT WALL", ("WALL", "wall")),
        ("BLC DW", ("", "")),           # угол дома: точка известна, подпись не нужна
        ("BLC5", ("", "")),
        ("SPOT GRADE PAV.", ("", "")),
        ("TTP", ("", "")),
    ],
)
def test_point_descriptions_are_translated_to_customer_wording(raw, expected):
    rules = fl.DEFAULT_FEATURE_CONFIG["point_descriptions"]
    label, category, _pattern = fl.translate_description(raw, rules)
    assert (label, category) == expected


def test_unknown_description_has_no_rule_and_normalization_ignores_punctuation():
    rules = fl.DEFAULT_FEATURE_CONFIG["point_descriptions"]
    assert fl.translate_description("GAZEBO", rules) is None
    assert fl.translate_description("", rules) is None
    assert fl.normalize_description(" conc-sw ") == "CONC SW"
    assert fl.normalize_description("SPOT GRADE PAV.") == "SPOT GRADE PAV"


def test_ilion_lot_is_closed_by_the_street_line():
    """В M-PROPERTY четыре стороны из пяти: фасад начерчен только в M-ROW-LINE."""
    snap = load_ilion()
    boundary = fl.find_property_polygon(snap, {})
    assert boundary.layer == "M-PROPERTY"
    assert boundary.vertex_count == 5
    assert boundary.area_sqft == pytest.approx(4339.0, abs=2.0)
    assert any("M-ROW-LINE" in note for note in boundary.notes)
    # Без замыкающего слоя — понятная ошибка, а не чужой контур.
    with pytest.raises(fl.FeatureLabelError):
        fl.find_property_polygon(snap, {"feature_labeling": {"property_closing_layers": []}})


def test_ilion_descriptions_become_labels():
    """Чертёж без единой подписи: всё берётся из атрибута DESC2 блоков точек."""
    snap = load_ilion()
    result = recognize(snap, house=ILION_HOUSE)
    assert len(snap.survey_points) == 93
    assert result.unknown_codes == {}
    assert result.by_notes == 0 and result.by_layers == 0

    # Бассейн и ступени к нему начерчены общими линиями, но это два объекта.
    # У бассейна заказчик пишет площадь: она считается по контуру.
    (pool,) = [f for f in result.features if f.category == "pool"]
    assert pool.label == "POOL\nAREA 349 Sq.Ft."
    assert pool.kind == "area" and pool.layer == "ST-MISC"
    assert pool.geometry.area == pytest.approx(349.0, abs=3.0)
    assert sorted(code.number for code in pool.codes) == ["51", "52", "53"]
    pool_steps = [f for f in ilion_features(result, "STEPS") if f.geometry.distance(pool.geometry) < 0.1]
    assert len(pool_steps) == 1 and pool_steps[0].geometry.area == pytest.approx(20.0, abs=1.0)

    # Крыльцо с проступями: линии слоя ST-MISC упираются в стену дома (другой
    # слой), контур не замкнут — объект один, площадь по оболочке его линий.
    (porch,) = ilion_features(result, "O.P.")
    assert porch.category == "porch" and not porch.closed
    assert porch.geometry.area == pytest.approx(31.7, abs=1.0)
    assert sorted(code.number for code in porch.codes) == ["10", "11"]

    assert len(ilion_features(result, "SHED", "area")) == 2
    # Узкая ступень (шириной до 1.5 ft) — «STEP», лестница — «STEPS».
    narrow = ilion_features(result, "STEP")
    assert len(narrow) == 2 and len(ilion_features(result, "STEPS")) == 3
    width = fl.DEFAULT_FEATURE_CONFIG["narrow_object_max_width_ft"]
    assert all(2.0 * f.geometry.area / f.geometry.length <= width for f in narrow)
    (planter,) = ilion_features(result, "PLANTER")
    assert planter.kind == "edge" and planter.geometry.length == pytest.approx(17.9, abs=0.1)
    (driveway,) = ilion_features(result, "DRIVEWAY")
    assert driveway.kind == "area" and driveway.layer == "S-ROAD-DWY"
    # Кромка тротуара подписи не получает: в итоговом чертеже её нет.
    walks = [f for f in result.features if f.layer == "S-ROAD-SW-LINE"]
    assert walks and not [f for f in walks if f.label]
    assert {f.skip_reason for f in walks} == {fl.SURFACE_EDGE_REASON}
    # Свес («CANTIL.») — на каждой стороне дома, где он начерчен.
    overhangs = ilion_features(result, "CANTIL.")
    assert len(overhangs) == 2 and {f.layer for f in overhangs} == {"ST-OHED"}
    assert not ilion_features(result, "CANTILEVER")

    # Стены под заборами отдельной подписи не получают.
    walls = [f for f in result.features if f.category == "wall"]
    assert len(walls) == 3 and {wall.layer for wall in walls} == {"ST-WALL"}
    assert not [wall for wall in walls if wall.label]
    assert {wall.skip_reason for wall in walls} == {fl.WALL_UNDER_FENCE_REASON}

    house = next(f for f in result.features if f.is_main_house)
    assert house.layer == "ST-BLDG-LINE" and house.label == ILION_HOUSE

    # Объекты без описания остаются без подписи и попадают в отчёт.
    assert sorted((f.layer, f.kind) for f in result.unlabeled) == [
        ("S-FENCE", "fence"), ("ST-MISC", "area"), ("ST-MISC", "area"),
    ]  # вторая площадка ST-MISC — лента из двух линий 13.6 ft с достроенными торцами
    # Точки, рядом с которыми в их слое нет линий.
    assert sorted(code.title for code in result.unbound_descriptions) == [
        "76 PLANTER", "91 BOT WALL",
    ]


def test_ilion_fences_follow_the_point_descriptions():
    result = recognize(load_ilion(), house=ILION_HOUSE)
    vinyl = ilion_features(result, "VINYL FENCE")
    chain = ilion_features(result, "CHAIN LINK FENCE")
    assert sorted(round(f.geometry.length, 1) for f in chain) == [0.6, 8.1, 18.6, 33.1, 35.3]
    assert sorted(round(f.geometry.length, 1) for f in vinyl) == [5.2, 6.9, 8.5, 9.4, 69.7, 83.7]
    # Точка 37 «F E C» лежит на виниловом заборе в футе от угла, но относится к
    # соседней сетке и забор не делит.
    for fence in vinyl:
        assert {code.label for code in fence.codes} == {"VINYL FENCE"}
    east_chain = next(f for f in chain if round(f.geometry.length, 1) == 33.1)
    assert sorted(code.number for code in east_chain.codes) == ["37", "39"]
    assert not result.unbound_fence_codes


def test_ilion_fence_on_a_wall_is_one_label():
    """Заказчик пишет «VINYL FENCE ATOP MASONRY WALL», а не забор и стену отдельно."""
    result = recognize(load_ilion(), house=ILION_HOUSE)
    # Материал стены в поле не снят: под забором она считается бетонной (правило заказчика).
    (vinyl,) = ilion_features(result, "VINYL FENCE ATOP CONC. WALL")
    assert vinyl.kind == "fence" and vinyl.geometry.length == pytest.approx(69.4, abs=0.2)
    assert "на стене ST-WALL" in vinyl.rule
    assert "материал стены не снят — принят CONC. WALL (проверьте по фото)" in vinyl.rule
    chain = ilion_features(result, "CHAIN LINK FENCE ATOP CONC. WALL")
    assert sorted(round(f.geometry.length, 1) for f in chain) == [14.6, 20.7]
    # 8 ft сетки над короткой стеной у сарая: общей подписи нет, стена молчит,
    # забор остаётся целым (33.1 ft).
    assert fl.DEFAULT_FEATURE_CONFIG["fence_atop_wall_label_min_ft"] == 10.0
    assert [f for f in ilion_features(result, "CHAIN LINK FENCE") if round(f.geometry.length, 1) == 33.1]

    # Настройка отключает правило: забор и стена подписываются отдельно.
    plain = recognize(load_ilion(), house=ILION_HOUSE, cfg={"feature_labeling": {"fence_atop_wall_format": ""}})
    assert not [f for f in plain.features if " ATOP " in f.flat_label]
    assert len(ilion_features(plain, "WALL")) == 3
    # Без материала по умолчанию стена пишется как снята.
    bare = recognize(load_ilion(), house=ILION_HOUSE, cfg={"feature_labeling": {"fence_atop_wall_default": ""}})
    assert len(ilion_features(bare, "VINYL FENCE ATOP WALL")) == 1
    assert "материал стены" not in ilion_features(bare, "VINYL FENCE ATOP WALL")[0].rule


#: Подписи итогового чертежа заказчика (docs/12 Ilion Place…final.dump.json),
#: которые Шаг 2 ставит тем же текстом: текст, центр рамки, поворот, допуск в ft.
ILION_FINAL_LABELS = [
    ("VINYL FENCE", (4993.2, 5001.5), 70.8, 2.5),
    ("VINYL FENCE", (5038.4, 5025.8), 90.4, 2.5),
    ("C.L.F.", (5045.1, 5027.9), 0.4, 2.5),
    ("C.L.F.", (5044.5, 5052.2), 0.4, 2.5),
    ("C.L.F.", (5041.3, 4993.7), 90.4, 2.5),
    ("C.L.F.", (5044.6, 5003.9), 0.4, 2.5),  # участок забора, уходящий за границу участка (v2.8)
    ("C.L.F.", (5042.6, 5063.6), 89.2, 5.0),
    ("V.FE.", (4982.7, 4982.9), 67.9, 2.5),
    ("V.FE.", (4983.0, 4988.7), -14.4, 2.5),
    ("CANTIL.", (5034.6, 5033.7), 90.4, 2.5),
    ("CANTIL.", (5003.8, 5033.4), 90.4, 2.5),
    ("CANTIL.", (5015.0, 5062.8), 0.4, 8.0),
    ("O.P.", (5004.4, 5044.0), 90.4, 2.5),
    ("STEP", (5012.8, 5014.1), 0.4, 2.5),
    ("PLANTER", (5038.2, 4993.0), 89.4, 4.0),
    # Забор на стене (с 3.0): «CONC. WALL», на коротком участке «C.WALL», на выноске в две
    # строки — у заказчика на этих же местах «C.L.F. ATOP C.WALL» и «C.L.F. ATOP / CONC. WALL».
    ("C.L.F. ATOP C.WALL", (4998.5, 5069.5), 90.4, 4.0),
    ("C.L.F. ATOP CONC. WALL", (5044.3, 5075.2), 0.4, 1.5),
    ("VINYL FENCE ATOP CONC. WALL", (5018.4, 4982.7), 0.4, 7.0),  # VINYL FENCE ATOP MASONRY WALL
]


def test_ilion_plan_matches_the_customer_final_drawing():
    """Те же сокращения, там же и с тем же поворотом, что в итоговом чертеже."""
    plan = make_plan(load_ilion(), house=ILION_HOUSE, height=0.9)
    for text, point, rotation, tolerance in ILION_FINAL_LABELS:
        found = [
            item for item in plan.items
            if item.single_line == text and math.dist(item.point, point) <= tolerance
        ]
        assert len(found) == 1, (text, point, [i.point for i in plan.items if i.single_line == text])
        # Полевой чертёж повёрнут относительно итогового на 0.4°.
        assert math.degrees(found[0].rotation) == pytest.approx(rotation, abs=1.5), text
    texts = [item.single_line for item in plan.items]
    assert "VINYL FENCE ATOP CONC. WALL" in texts
    assert "C.L.F. ATOP C.WALL" in texts and "C.L.F. ATOP CONC. WALL" in texts
    assert "POOL AREA 349 Sq.Ft." in texts
    # Того, чего в итоговом чертеже нет: отдельной стены, тротуара, полного «CANTILEVER».
    for absent in ("WALL", "CONC. WALK", "CANTILEVER", "CHAIN LINK FENCE"):
        assert absent not in texts, absent
    # Короткая сетка у стены: сокращение на выноске в две строки, как у заказчика.
    (atop,) = [item for item in plan.items if item.text == "C.L.F. ATOP\nCONC. WALL"]
    assert atop.placement == "leader"
    # Вдоль 20-футового участка «C.L.F. ATOP CONC. WALL» не помещается — пишется «C.WALL».
    (compact,) = [item for item in plan.items if item.text == "C.L.F. ATOP C.WALL"]
    assert compact.placement == "along" and compact.feature.geometry.length == pytest.approx(20.7, abs=0.2)


def test_ilion_plan_has_no_overlaps_and_report_names_the_source():
    snap = load_ilion()
    plan = make_plan(snap, house=ILION_HOUSE, height=0.9)
    texts = sorted(item.single_line for item in plan.items)
    for expected in ("POOL AREA 349 Sq.Ft.", "SHED", "O.P.", "PLANTER", "DRIVEWAY", "STEP", "STEPS",
                     "VINYL FENCE", "C.L.F.", "V.FE.", "CANTIL.", "2 STY FRAME No. 12"):
        assert expected in texts, expected
    # 32-я подпись — STEPS внутри лестницы бассейна: её контур замкнут (стык линий
    # сведён в общую точку), а замкнутый контур с описанием подписывается сам.
    # 33-я — вторая STEP у длинной ступени с изломом (правило заказчика).
    assert len(plan.items) == 33
    assert plan.count_placement("leader") <= 3
    assert plan.survey_point_count == 93
    boxes = [item.box for item in plan.items]
    for i, first in enumerate(boxes):
        for second in boxes[i + 1:]:
            assert not first.intersects(second)
    report = ab.FeatureLabelResult(plan=plan, layer=plan.output_layer, dry_run=True).report()
    assert "Точек съёмки с описанием в атрибутах блоков: 93" in report
    assert "76 PLANTER" in report


def test_point_blocks_are_read_from_autocad_even_with_frozen_attribute_layers(com):
    source = load_ilion()
    builder, _app, doc = com(source)
    for name in ("PNTDESC", "PNTNO", "PNTELEV", "PNTS"):
        doc.Layers.items[name] = FakeLayer(name)
        doc.Layers.items[name].Freeze = True
    space = doc.ModelSpace
    # Блоки, которые точками съёмки не являются: без атрибутов и с пустым описанием.
    space.entities.append(FakeEntity(
        "AcDbBlockReference", "0", HasAttributes=False, EffectiveName="LOGO",
        InsertionPoint=(5060.0, 5100.0, 0.0),
    ))
    space.entities.append(
        fake_point_block(fl.SurveyPoint(layer="0", number="1", description="", point=(4999.83, 4999.22)))
    )
    snapshot = builder.read_drawing_snapshot(doc)
    assert snapshot.read_errors == 0
    assert len(snapshot.survey_points) == 93
    first = snapshot.survey_points[0]
    assert (first.layer, first.number, first.description, first.block) == (
        "ST-BLDG-LINE", "7", "BLC", "srvpno3",
    )
    assert first.point == pytest.approx((5006.22, 5015.13))
    restored = recognize(snapshot, house=ILION_HOUSE)
    expected = recognize(source, house=ILION_HOUSE)
    assert sorted(f.flat_label for f in restored.labeled) == sorted(f.flat_label for f in expected.labeled)


def test_label_features_on_point_block_drawing_creates_labels(com):
    builder, _app, doc = com(load_ilion())
    result = builder.label_features(house_text=ILION_HOUSE)
    created = [e for e in doc.ModelSpace.created if e.ObjectName in {"AcDbText", "AcDbMText"}]
    assert result.created_labels == len(result.plan.items) > 20
    assert created
    assert_house_label_on_layer_zero(created, result.plan.text_height)
    # Повторный запуск ничего не добавляет.
    again = builder.label_features(house_text=ILION_HOUSE)
    assert again.created_labels == 0


def test_selecting_everything_builds_the_lot_from_property_layers_only():
    """
    Реальный запуск: границу «указали в AutoCAD» рамкой вокруг всего чертежа.
    Из смеси заборов, стен и строений получался случайный контур в 2 121 sq ft.
    """
    snap = load_ilion()
    boundary = fl.property_polygon_from_selection(list(snap.lines), snap, {})
    assert boundary.method == "pick" and boundary.layer == "M-PROPERTY"
    assert boundary.vertex_count == 5
    assert boundary.area_sqft == pytest.approx(4339.0, abs=2.0)
    assert "граница построена только по линиям слоя участка (M-PROPERTY)" in boundary.notes[0]
    result = fl.recognize_features(snap, boundary, {}, ILION_HOUSE)
    assert len(result.labeled) == len(recognize(load_ilion(), house=ILION_HOUSE).labeled) > 20

    # Выбраны только линии границы — они и берутся, без пояснений о лишних слоях.
    only = [line for line in snap.lines if line.layer in {"M-PROPERTY", "M-ROW-LINE"}]
    boundary = fl.property_polygon_from_selection(only, snap, {})
    assert boundary.area_sqft == pytest.approx(4339.0, abs=2.0)
    assert not [note for note in boundary.notes if "остальные выбранные линии" in note]


@pytest.mark.parametrize("shift, layer", [(0.0, "PROPERTY_LINE"), (300.0, "M-PROPERTY")])
def test_step1_boundary_away_from_the_survey_does_not_hide_the_surveyed_lot(shift, layer):
    """Граница Шага 1 (PROPERTY_LINE) приоритетна, только если съёмка лежит внутри неё."""
    snap = load_ilion()
    lot = fl.find_property_polygon(snap, {}).polygon
    snap.lines.append(
        DrawingLine("PROPERTY_LINE", tuple((x + shift, y) for x, y in lot.exterior.coords),
                    kind="AcDbPolyline", closed=True)
    )
    for boundary in (
        fl.find_property_polygon(snap, {}),
        fl.property_polygon_from_selection(list(snap.lines), snap, {}),
    ):
        assert boundary.layer == layer
        assert boundary.area_sqft == pytest.approx(4339.0, abs=2.0)
        # В обоих случаях участок — тот, где лежит съёмка.
        assert boundary.polygon.centroid.distance(lot.centroid) < 0.5
        skipped = [note for note in boundary.notes if "контур слоя PROPERTY_LINE пропущен" in note]
        assert bool(skipped) == bool(shift)


def test_report_names_the_version_and_blocks_without_description(com):
    builder, _app, doc = com(load_ilion())
    result = builder.label_features(house_text=ILION_HOUSE, dry_run=True)
    report = result.report()
    assert f"Версия Шага 2: {fl.STEP2_VERSION}" in report
    assert "Точек съёмки с описанием в атрибутах блоков: 93" in report
    assert "блоков с атрибутами" not in report

    # Блоки точек с другими тегами атрибутов: описаний нет — отчёт говорит почему.
    source = load_ilion()
    builder, _app, doc = com(source)
    space = doc.ModelSpace
    space.entities[:] = [e for e in space.entities if e.ObjectName != "AcDbBlockReference"]
    for survey in source.survey_points:
        space.entities.append(fake_point_block(survey, tags=("NUM", "Z", "NOTE")))
    result = builder.label_features(house_text=ILION_HOUSE, dry_run=True)
    report = result.report()
    assert "Точек съёмки с описанием в атрибутах блоков: 0" in report
    assert "блоков с атрибутами: 93, но описания точки нет ни в одном" in report
    assert "теги в блоках: NOTE, NUM, Z" in report
    assert "point_block_attribute_tags" in report


def test_short_codes_in_block_attributes_use_the_code_dictionary():
    """Атрибут с коротким кодом («FEV64») работает как текст слоя POINT_LABELS."""
    snap = DrawingSnapshot()
    for a, b in (((0, 0), (40, 0)), ((40, 0), (40, 60)), ((40, 60), (0, 60)), ((0, 60), (0, 0))):
        snap.lines.append(DrawingLine(layer="PROPERTY_LINE", points=(a, b)))
    snap.lines.append(DrawingLine(layer="S-FENCE", points=((0.5, 5), (0.5, 55))))
    for number, code, point in (("1", "FEV64", (0.5, 5)), ("2", "FEV64", (0.5, 55)),
                                ("3", "BLC", (10, 10)), ("4", "GAZEBO", (20, 20))):
        snap.survey_points.append(fl.SurveyPoint(layer="S-FENCE", number=number, description=code, point=point))
    result = recognize(snap, house="")
    (fence,) = [f for f in result.features if f.kind == "fence"]
    assert fence.label == "VINYL FENCE" and fence.source == "point_code"
    assert result.unknown_codes == {"GAZEBO": 1}


# ---------------------------------------------------------------------------
# Дом блокированной застройки: «6910 15th Avenue, Brooklyn»
# ---------------------------------------------------------------------------

BROOKLYN_HOUSE = "3 STY\nBRICK\nW/CELLAR\nNo. 6910"

#: Подписи итогового чертежа заказчика (docs/6910 15th Avenue, Brooklyn.final.dump.json),
#: пересчитанные в координаты полевого чертежа (итоговый повёрнут на −3.52°):
#: текст Шага 2, центр рамки у заказчика, допуск в ft. В комментарии — текст
#: заказчика, если он отличается (материал и отступы берутся не из съёмки).
BROOKLYN_FINAL_LABELS = [
    ("3 STY BRICK W/CELLAR No. 6910", (5003.9, 4956.2), 2.5),
    ("PARTY WALL", (4992.7, 4957.6), 1.0),
    ("PARTY WALL", (5015.7, 4957.6), 1.0),
    ("C.D.", (5010.3, 4928.7), 1.5),
    ("C.D.", (4999.0, 4987.6), 1.5),
    ("C.D.", (4991.8, 4927.4), 1.5),
    ("FIRE ESC.", (5005.7, 4987.2), 1.5),
    ("FIRE ESC.", (5021.3, 4986.8), 2.0),
    ("VEST.", (5019.3, 4990.7), 1.5),
    ("AWNING", (5017.8, 4928.5), 1.5),
    ("DECK", (4988.2, 4988.2), 3.0),                # METAL DECK
    ("CHAIN LINK FENCE", (5004.1, 5019.1), 2.0),    # CHAIN LINK FENCE 0.4'E
    ("IRON FENCE", (5003.7, 4923.9), 0.5),          # IRON FENCE 5.4'E
    ("WALL", (5015.6, 5002.6), 1.5),                # CONC. WALL ALONG LINE
    ("WALL", (5000.1, 5004.6), 3.5),                # CONC. WALL
    ("UTILITY POLE", (5002.4, 5015.2), 4.0),
    ("C.L.F. ATOP CONC. WALL", (4995.2, 5003.5), 3.0),
]


def brooklyn_plan(**kwargs):
    kwargs.setdefault("house", BROOKLYN_HOUSE)
    kwargs.setdefault("height", 0.9)
    return make_plan(load_brooklyn(), **kwargs)


def test_brooklyn_fixture_is_the_compact_copy_of_the_dump(tmp_path):
    """Запись чертежа в tests/data получена из дампа скриптом dump_to_fixture.py."""
    sys.path.insert(0, str(ROOT / "tests"))
    try:
        import dump_to_fixture
    finally:
        sys.path.pop(0)
    target = tmp_path / "brooklyn.txt"
    stats = dump_to_fixture.convert(
        ROOT / "docs" / "for MLP 6910 15th Avenue, Brooklyn.dump.json", target,
        "6910 15th Avenue, Brooklyn",
    )
    assert dict(stats) == {"L": 57, "P": 43, "Q": 5, "H": 3, "T": 1, "B": 2}
    assert target.read_text(encoding="utf-8") == (
        ROOT / "tests" / "data" / "brooklyn_field.txt"
    ).read_text(encoding="utf-8")
    snap = load_brooklyn()
    assert (len(snap.lines), len(snap.survey_points)) == (57, 43)
    assert [symbol.name for symbol in snap.symbols] == ["WUP", "NA"]


def test_brooklyn_lot_is_closed_by_the_street_line():
    boundary = fl.find_property_polygon(load_brooklyn(), {})
    assert boundary.layer == "M-PROPERTY"
    assert boundary.area_sqft == pytest.approx(1800.0, abs=0.5)  # 20 × 90 ft
    (note,) = boundary.notes
    assert "недостающая сторона (20.00 ft) идёт по линии слоя M-ROW-LINE" in note


def test_row_house_is_the_part_of_the_lot_between_the_wall_lines():
    """
    Замкнутого контура дома нет: в ST-BLDG-LINE только линии переднего и заднего
    фасадов, продолженные на соседние дома; боковые стены — общие, по границам.
    """
    result = recognize(load_brooklyn(), house=BROOKLYN_HOUSE)
    (house,) = [f for f in result.features if f.is_main_house]
    assert (house.kind, house.category, house.layer) == ("area", "house", "ST-BLDG-LINE")
    assert (house.source, house.closed) == ("user", False)
    # Участок 20 ft шириной, дом — от красной линии до заднего фасада (54.8 ft).
    assert house.geometry.area == pytest.approx(20.0 * 54.83, abs=3.0)
    assert house.geometry.bounds == pytest.approx((4994.21, 4930.36, 5014.21, 4985.23), abs=0.1)
    assert [note for note in result.notes if "блокированная застройка" in note]
    # Линии фасадов в «без подписи» не попадают; дом соседа сзади — остаётся.
    walls = [f for f in result.features if f.skip_reason == fl.ROW_HOUSE_WALL_REASON]
    assert len(walls) == 2
    assert result.unlabeled == [] and not result.house_without_text
    assert [(f.kind, f.category) for f in result.features if f.skip_reason == fl.OTHER_HOUSE_REASON] == [
        ("area", "house")
    ]

    # Без текста дома контур найден, но подписи у него нет.
    plain = recognize(load_brooklyn(), house="")
    assert not [f for f in plain.features if f.is_main_house]
    assert plain.house_without_text and plain.unlabeled == []
    (found,) = [f for f in plain.features if f.rule == fl.ROW_HOUSE_RULE]
    assert found.label is None and found.skip_reason == fl.HOUSE_TEXT_MISSING_REASON
    assert found.geometry.equals(house.geometry)


def test_row_house_needs_wall_lines_on_two_sides_and_an_empty_interior():
    lot = shapely_box(0.0, 0.0, 20.0, 90.0)
    front = LineString([(-10.0, 0.05), (30.0, 0.05)])
    rear = LineString([(-10.0, 55.0), (30.0, 55.0)])
    fence = LineString([(0.5, 56.0), (0.5, 89.0), (19.5, 89.0)])
    shed = LineString([(5.0, 70.0), (15.0, 70.0), (15.0, 80.0), (5.0, 80.0), (5.0, 70.0)])
    house = fl._row_house_polygon(lot, [front, rear], [fence, shed], min_area=50.0)
    assert house.bounds == pytest.approx((0.0, 0.05, 20.0, 55.0), abs=0.01)
    # Линии фасадов на полфута не доходят до боковых границ — участок всё равно делится.
    short = [LineString([(0.5, 0.05), (19.5, 0.05)]), LineString([(0.4, 55.0), (19.6, 55.0)])]
    assert fl._row_house_polygon(lot, short, [fence, shed], min_area=50.0).area == pytest.approx(
        20.0 * 54.95, abs=0.5
    )
    # Одна линия фасада: обе части участка ограничены ею только с одной стороны.
    assert fl._row_house_polygon(lot, [rear], [fence, shed], min_area=50.0) is None
    # Внутри «дома» сняты объекты — это двор, а не дом.
    busy = LineString([(5.0, 20.0), (15.0, 20.0)])
    assert fl._row_house_polygon(lot, [front, rear], [fence, shed, busy], min_area=50.0) is None
    # Отдельно стоящий дом: его линии участок не делят.
    detached = LineString([(5.0, 20.0), (15.0, 20.0), (15.0, 50.0), (5.0, 50.0), (5.0, 20.0)])
    assert fl._row_house_polygon(lot, [detached], [fence], min_area=50.0) is None


def test_brooklyn_plan_matches_the_customer_final_drawing():
    plan = brooklyn_plan()
    assert plan.skipped == [] and plan.warnings == []
    for text, point, tolerance in BROOKLYN_FINAL_LABELS:
        found = [
            item for item in plan.items
            if item.single_line == text and math.dist(item.box.centroid.coords[0], point) <= tolerance
        ]
        assert len(found) == 1, (
            text, point, [i.box.centroid.coords[0] for i in plan.items if i.single_line == text]
        )
    texts = sorted(item.single_line for item in plan.items)
    assert texts == sorted(
        [text for text, _point, _tolerance in BROOKLYN_FINAL_LABELS]
        + ["I.FE."]
    )
    by_text = {item.single_line: item for item in plan.items}
    # Узкая площадка соседа — в две строки, своя длинная — в одну, рядом с люком.
    lines = sorted(item.text for item in plan.items if item.single_line == "FIRE ESC.")
    assert lines == ["FIRE\nESC.", "FIRE ESC."]
    assert all(item.placement == "inside" for item in plan.items if item.single_line in {
        "C.D.", "FIRE ESC.", "VEST.", "AWNING", "DECK",
    })
    # Общие стены — вдоль границы, со стороны соседей; столб — выноской.
    party = [item for item in plan.items if item.single_line == "PARTY WALL"]
    assert sorted(round(item.point[0], 1) for item in party) == [4992.7, 5015.8]
    # Стрелка севера смотрит вправо — вертикальный текст сверху вниз (у заказчика 270°).
    assert all(math.degrees(item.rotation) == pytest.approx(-90.0, abs=0.1) for item in party)
    assert math.degrees(by_text["C.L.F. ATOP CONC. WALL"].rotation) == pytest.approx(-89.9, abs=0.2)
    pole = by_text["UTILITY POLE"]
    assert (pole.kind, pole.text) == ("leader", "UTILITY\nPOLE")
    assert math.dist(pole.leader_points[0], (4997.49, 5017.47)) < 1.5  # знак WUP
    # Забор на стене подписан вместе с ней; у стены под забором своей подписи нет.
    atop = by_text["C.L.F. ATOP CONC. WALL"]
    assert atop.feature.geometry.length == pytest.approx(34.2, abs=0.2)


def test_house_description_goes_to_layer_zero_in_a_larger_style(com):
    """Описание дома у заказчика — MText в слое 0, стиль RDS, 1.5 ft при подписях 0.9 ft."""
    builder, _app, doc = com(load_brooklyn())
    doc.TextStyles.Add("RDS")
    result = builder.label_features(house_text=BROOKLYN_HOUSE)
    assert result.failed == [] and result.created_labels == len(result.plan.items) == 18
    created = [e for e in doc.ModelSpace.created if e.ObjectName in {"AcDbText", "AcDbMText"}]
    (house,) = [e for e in created if e.Layer == "0"]
    assert house.TextString == "3 STY\\PBRICK\\PW/CELLAR\\PNo. 6910"
    assert house.Height == pytest.approx(1.5, abs=0.01) and house.StyleName == "RDS"
    assert house.InsertionPoint[:2] == pytest.approx([5004.2, 4957.8], abs=0.3)
    others = [e for e in created if e.Layer != "0"]
    assert {e.Layer for e in others} == {"D-TOPO-TEXT"}
    assert all(e.Height == pytest.approx(0.9) for e in others)
    assert "RDS" not in {e.StyleName for e in others}
    assert "«3 STY BRICK W/CELLAR No. 6910»: слой 0, высота 1.50 ft, стиль RDS" in result.report()
    # Повторный запуск видит описание дома в слое 0 и ничего не добавляет.
    again = builder.label_features(house_text=BROOKLYN_HOUSE)
    assert again.created_labels == 0 and len(again.plan.duplicates) >= 15
    assert "3 STY BRICK W/CELLAR No. 6910" in [d.existing_text for d in again.plan.duplicates]

    # Стиля RDS в чертеже нет — программа создаёт его таким, как в шаблоне заказчика
    # (ROMAND.shx, сжатие 0.8, наклон 20°); слой можно вернуть настройкой.
    builder, _app, doc = com(load_brooklyn(), feature_cfg={"house_label_layer": ""})
    result = builder.label_features(house_text=BROOKLYN_HOUSE)
    created = [e for e in doc.ModelSpace.created if e.ObjectName in {"AcDbText", "AcDbMText"}]
    assert {e.Layer for e in created} == {"D-TOPO-TEXT"}
    (house,) = [e for e in created if "6910" in e.TextString]
    assert house.Height == pytest.approx(1.5) and house.StyleName == "RDS"
    style = doc.TextStyles.items["RDS"]
    assert (style.fontFile, style.Width, style.Height) == ("romand.shx", 0.8, 0.0)
    assert math.degrees(style.ObliqueAngle) == pytest.approx(20.0)
    assert result.created_styles == ["RDS"]
    # Определение стиля убрано из настроек — описание дома пишется общим стилем подписей.
    builder, _app, doc = com(load_brooklyn(), feature_cfg={"house_label_style_definition": {}})
    result = builder.label_features(house_text=BROOKLYN_HOUSE)
    (house,) = [e for e in doc.ModelSpace.created if getattr(e, "Layer", "") == "0"]
    assert house.StyleName != "RDS" and "RDS" not in doc.TextStyles.items and result.created_styles == []


def test_symbol_blocks_are_labeled_only_on_the_lot(com):
    snap = load_brooklyn()
    snap.symbols.append(fl.DrawingSymbol(layer="U-TRAF-IRON", name="WUP", point=(5004.0, 4915.0)))
    poles = [f for f in recognize(snap, house="").features if f.category == "symbol"]
    assert [(f.kind, f.label, f.geometry.coords[0]) for f in poles] == [
        ("point", "UTILITY\nPOLE", (4997.49, 5017.47)),
    ]  # столб на улице не подписан, стрелка севера (NA) — не знак
    everywhere = recognize(snap, house="", cfg={"feature_labeling": {"symbol_label_scope": "all"}})
    assert len([f for f in everywhere.features if f.category == "symbol"]) == 2
    nowhere = recognize(snap, house="", cfg={"feature_labeling": {"symbol_label_scope": "none"}})
    assert not [f for f in nowhere.features if f.category == "symbol"]
    # Код точки «W U P» известен (в отчёте «коды не из словаря» его нет), подписи не даёт.
    assert "W U P" not in everywhere.unknown_codes and "COLUMN" not in everywhere.unknown_codes
    assert everywhere.unknown_codes == {}

    # Из AutoCAD знак читается как вставка блока без атрибутов.
    builder, _app, doc = com(load_brooklyn())
    read = builder.read_drawing_snapshot(doc)
    assert [(symbol.name, symbol.layer) for symbol in read.symbols] == [
        ("WUP", "U-TRAF-IRON"), ("NA", "M-NTH-ARROW"),
    ]
    assert len(read.survey_points) == 43


def test_party_wall_is_a_pair_of_lines_along_the_lot_line():
    lot = shapely_box(0.0, 0.0, 20.0, 90.0)
    pair = [LineString([(-0.5, 0.0), (-0.5, 55.0)]), LineString([(0.5, 0.0), (0.5, 50.0)])]
    (axis,) = fl._party_wall_axes(pair, lot, 1.6, 10.0)
    assert [value for point in axis.coords for value in point] == pytest.approx([0.0, 0.0, 0.0, 50.0])
    # Одна линия, пара в стороне от границы, слишком широкая или короткая пара — не стена.
    assert fl._party_wall_axes(pair[:1], lot, 1.6, 10.0) == []
    inside = [LineString([(9.5, 0.0), (9.5, 50.0)]), LineString([(10.5, 0.0), (10.5, 50.0)])]
    assert fl._party_wall_axes(inside, lot, 1.6, 10.0) == []
    wide = [LineString([(-1.5, 0.0), (-1.5, 50.0)]), LineString([(1.5, 0.0), (1.5, 50.0)])]
    assert fl._party_wall_axes(wide, lot, 1.6, 10.0) == []
    stub = [LineString([(-0.5, 0.0), (-0.5, 8.0)]), LineString([(0.5, 0.0), (0.5, 8.0)])]
    assert fl._party_wall_axes(stub, lot, 1.6, 10.0) == []

    result = recognize(load_brooklyn(), house="")
    walls = [f for f in result.features if f.category == "party_wall"]
    assert [(f.kind, f.layer, f.label, f.source) for f in walls] == [
        ("edge", "0", "PARTY WALL", "layer")
    ] * 2
    assert sorted(round(f.geometry.centroid.x, 2) for f in walls) == [4994.21, 5014.21]
    off = recognize(load_brooklyn(), house="", cfg={"feature_labeling": {"party_wall_layers": []}})
    assert not [f for f in off.features if f.category == "party_wall"]


def test_wall_drawn_as_two_open_lines_becomes_a_ribbon():
    """Стена у границы снята двумя сторонами без торцов: забор на ней подписан с ней вместе."""
    left = LineString([(0.0, 0.0), (0.0, 30.0)])
    right = LineString([(0.7, 0.0), (0.7, 30.0)])
    caps = fl._ribbon_caps([left, right], 1.6, 0.05)
    assert sorted(sorted(cap.coords) for cap in caps) == [
        [(0.0, 0.0), (0.7, 0.0)], [(0.0, 30.0), (0.7, 30.0)],
    ]
    # Торец уже начерчен, линии шире стены или идут в разные стороны — ничего не добавляется.
    closed = [left, right, LineString([(0.0, 0.0), (0.7, 0.0)]), LineString([(0.0, 30.0), (0.7, 30.0)])]
    assert fl._ribbon_caps(closed, 1.6, 0.05) == []
    assert fl._ribbon_caps([left, LineString([(3.0, 0.0), (3.0, 30.0)])], 1.6, 0.05) == []
    assert fl._ribbon_caps([left, LineString([(0.7, 0.0), (0.7, -30.0)])], 1.6, 0.05) == []

    result = recognize(load_brooklyn(), house="")
    walls = [f for f in result.features if f.category == "wall"]
    assert sorted((f.label or "", f.skip_reason) for f in walls) == [
        ("", fl.WALL_UNDER_FENCE_REASON), ("WALL", ""), ("WALL", ""),
    ]
    assert [f.flat_label for f in result.features if " ATOP " in f.flat_label] == [
        "CHAIN LINK FENCE ATOP CONC. WALL"
    ]
    # Забор на стене сравнивается по забору: материал стены заказчик берёт с фотографий.
    for existing in ("C.L.F. ATOP CONC. WALL", "C.L.F. ATOP C.WALL", "C.L.F. ATOP\\PCONC. WALL",
                     "C.L.F. ATOP WALL"):
        for wanted in ("CHAIN LINK FENCE ATOP CONC. WALL", "C.L.F. ATOP CONC. WALL", "C.L.F. ATOP C.WALL"):
            assert fl.labels_similar(
                existing, wanted, fl.DEFAULT_FEATURE_CONFIG["label_synonyms"]
            ), (existing, wanted)
    assert fl.labels_similar(
        "CHAIN LINK FENCE ATOP MASONRY WALL", "CHAIN LINK FENCE ATOP CONC. WALL",
        fl.DEFAULT_FEATURE_CONFIG["label_synonyms"],
    )
    assert not fl.labels_similar(
        "VINYL FENCE ATOP MASONRY WALL", "C.L.F. ATOP CONC. WALL", fl.DEFAULT_FEATURE_CONFIG["label_synonyms"]
    )
    assert not fl.labels_similar("C.L.F.", "C.L.F. ATOP CONC. WALL") and not fl.labels_similar(
        "CONC. WALL", "C.L.F. ATOP CONC. WALL"
    )
    # «C.L.F. ATOP WALL» рядом не считается подписью соседней стены «WALL».
    assert not fl.labels_similar("C.L.F. ATOP WALL", "WALL")
    assert fl.labels_similar("C.L.F. ATOP WALL", "CHAIN LINK FENCE ATOP WALL", {
        "CHAIN LINK FENCE ATOP WALL": ["C.L.F. ATOP WALL"],
    })


def test_corner_of_two_sides_is_completed_to_a_rectangle():
    """Тамбур в углу между домом и соседом снят двумя сторонами: контур — прямоугольник."""
    corner = [LineString([(0.0, 8.0), (4.0, 8.0)]), LineString([(4.0, 8.0), (4.0, 0.0)])]
    shape = fl._shape_from_open_lines(corner, 0.75)
    assert shape.area == pytest.approx(32.0) and shape.bounds == (0.0, 0.0, 4.0, 8.0)
    # Почти прямая ломаная прямоугольником не становится.
    flat = [LineString([(0.0, 0.0), (10.0, 0.0)]), LineString([(10.0, 0.0), (20.0, 1.0)])]
    assert fl._shape_from_open_lines(flat, 0.75) is None

    result = recognize(load_brooklyn(), house="")
    areas = {
        (f.label, round(f.geometry.area)) for f in result.features
        if f.kind == "area" and f.label in {"VEST.", "DECK", "AWNING"}
    }
    assert areas == {("VEST.", 31), ("DECK", 53), ("AWNING", 19)}


def test_point_description_overrides_the_linear_layer_rule():
    """ST-OHED — слой линейных навесов (CANTIL. вдоль линии), но FIRESCAPE и AWNING — площадки."""
    result = recognize(load_brooklyn(), house="")
    overhead = [f for f in result.features if f.layer == "ST-OHED"]
    assert sorted((f.kind, f.category, f.label) for f in overhead) == [
        ("area", "awning", "AWNING"),
        ("area", "fire_escape", "FIRE ESC."),
        ("area", "fire_escape", "FIRE ESC."),
    ]
    # Консоль дома в том же слое остаётся линией (12 Ilion Place).
    ilion = recognize(load_ilion(), house=ILION_HOUSE)
    assert {f.kind for f in ilion.features if f.label == "CANTIL."} == {"edge"}


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("CELDOOR", ("C.D.", "cellar_door")),
        ("VESTIBULE", ("VEST.", "porch")),
        ("FIRESCAPE", ("FIRE ESC.", "fire_escape")),
        ("AW OVERH", ("AWNING", "awning")),
        ("I P F", ("IRON FENCE", "fence")),
        ("DECK", ("DECK", "porch")),
        ("WOOD DECK", ("FRAME\nDECK", "porch")),
        ("METAL DECK", ("METAL\nDECK", "porch")),
        ("COLUMN", ("", "column")),
        ("BBC", ("", "")),
        ("W U P", ("", "")),
        ("BLC21", ("", "")),
    ],
)
def test_brooklyn_point_descriptions(raw, expected):
    label, category, _pattern = fl.translate_description(
        raw, fl.DEFAULT_FEATURE_CONFIG["point_descriptions"]
    )
    assert (label, category) == expected


def test_step1_boundary_that_disagrees_with_the_surveyed_lot_is_reported(com):
    """Граница Шага 1 посажена криво: подписи считаются по ней, но отчёт предупреждает."""
    snap = load_ilion()
    crooked = [(4975.0, 4975.0), (5075.0, 4980.0), (5070.0, 5090.0), (4970.0, 5085.0)]
    snap.lines.append(DrawingLine("PROPERTY_LINE", tuple(crooked), kind="AcDbPolyline", closed=True))
    boundary = fl.find_property_polygon(snap, {})
    assert boundary.layer == "PROPERTY_LINE"
    (note,) = boundary.notes
    assert note.startswith(
        "ВНИМАНИЕ: граница слоя PROPERTY_LINE (11 025 sq ft) расходится с участком слоя "
        "M-PROPERTY (4 339 sq ft): совпадают на 39 %"
    )
    builder, _app, _doc = com(snap)
    assert "расходится с участком слоя M-PROPERTY" in builder.label_features(
        house_text=ILION_HOUSE, dry_run=True
    ).report()

    # Граница Шага 1 точно по участку геодезиста — предупреждения нет.
    exact = load_ilion()
    lot = fl.find_property_polygon(exact, {}).polygon
    exact.lines.append(
        DrawingLine("PROPERTY_LINE", tuple(lot.exterior.coords), kind="AcDbPolyline", closed=True)
    )
    assert fl.find_property_polygon(exact, {}).notes == []


# ------------------------------------------- реальный результат в AutoCAD (дамп)

EDITED_BROOKLYN_DUMP = ROOT / "docs" / "edited for MLP 6910 15th Avenue, Brooklyn.dump.json"


def real_brooklyn_labels():
    """Подписи, которые версия 2.6 создала в AutoCAD (дамп inspect_dwg.py после Шага 2)."""
    dump = json.loads(EDITED_BROOKLYN_DUMP.read_text(encoding="utf-8"))
    return [
        entity for entity in dump["entities"]
        if entity["layer"] == "D-TOPO-TEXT" and entity["type"] in {"Text", "MText"}
    ]


def labeled_brooklyn_snapshot():
    """Полевой чертёж вместе с подписями, созданными программой в AutoCAD."""
    snap = load_brooklyn()
    for entity in real_brooklyn_labels():
        snap.texts.append(
            DrawingText(
                layer=entity["layer"], text=fl.plain_text(entity["text"]),
                point=(entity["pt"][0], entity["pt"][1]), height=entity["h"],
                rotation=math.radians(entity["rot_deg"]), bbox=tuple(entity["bbox"]),
                kind="AcDbMText" if entity["type"] == "MText" else "AcDbText",
            )
        )
    return snap


def test_text_size_model_matches_what_autocad_really_drew():
    """
    Габариты подписей, созданных программой в настоящем AutoCAD (GetBoundingBox
    из дампа), совпадают с расчётными: расстановка считается по верным размерам.
    """
    labels = real_brooklyn_labels()
    assert len(labels) == 18
    assert {entity["h"] for entity in labels} == {0.9}
    worst = 0.0
    for entity in labels:
        lines = entity["text"].split("\\P")
        expected = max(ab.feature_text_width(line, 0.9) for line in lines)
        minx, miny, maxx, maxy = entity["bbox"]
        vertical = abs(math.sin(math.radians(entity["rot_deg"]))) > 0.7
        measured = (maxy - miny) if vertical else (maxx - minx)
        across = (maxx - minx) if vertical else (maxy - miny)
        worst = max(worst, abs(measured - expected))
        assert measured == pytest.approx(expected, abs=0.15), entity["text"]
        # Высота строки 0.9 ft; у двух строк — шаг строк MText 1.5 высоты.
        assert across == pytest.approx(0.9 + 1.5 * (len(lines) - 1), abs=0.06), entity["text"]
    assert worst < 0.13
    # План по записи чертежа и результат в AutoCAD: те же тексты на тех же местах.
    # (с настройками версии 2.6, которая их создала)
    v26 = {
        "front_yard_depth_ft": 0.0, "leader_text_gap_factor": 0.5, "fence_atop_wall_default": "",
        "along_line_gaps_ft": {}, "leader_min_length_factor": 2.5,
    }
    plan = brooklyn_plan(house="", cfg={"feature_labeling": v26})
    assert sorted(item.single_line for item in plan.items) == sorted(
        entity["text"].replace("\\P", " ") for entity in labels
    )
    for item in plan.items:
        center = item.box.centroid
        nearest = min(
            math.dist(
                (center.x, center.y),
                ((e["bbox"][0] + e["bbox"][2]) / 2.0, (e["bbox"][1] + e["bbox"][3]) / 2.0),
            )
            for e in labels if e["text"].replace("\\P", " ") == item.single_line
        )
        # VEST. в AutoCAD встал на 0.5 ft выше: запись чертежа округлена до 0.01 ft.
        assert nearest < (0.6 if item.single_line == "VEST." else 0.1), item.single_line


def test_second_run_on_the_really_labeled_drawing_adds_only_the_house():
    """Повторный запуск на чертеже, уже подписанном программой в AutoCAD."""
    plan = make_plan(labeled_brooklyn_snapshot(), house="", height=0.9)
    assert plan.items == [] and plan.warnings == []
    assert len(plan.duplicates) >= 16
    assert plan.recognition.house_without_text
    # В первый раз текст дома не ввели: теперь добавляется только он.
    plan = make_plan(labeled_brooklyn_snapshot(), house=BROOKLYN_HOUSE, height=0.9)
    assert [item.single_line for item in plan.items] == ["3 STY BRICK W/CELLAR No. 6910"]
    assert (plan.items[0].layer, plan.items[0].height) == ("0", pytest.approx(1.5, abs=0.01))


def test_report_says_that_the_house_text_was_not_entered(com):
    builder, _app, _doc = com(load_brooklyn())
    result = builder.label_features(house_text="")
    report = result.report()
    assert result.failed == [] and result.created_labels == 17
    assert "ДОМ НЕ ПОДПИСАН: поле «Текст для основного дома» пустое" in report
    assert "БЕЗ ПОДПИСИ" not in report  # дом без текста и дом соседа — не «слой без правила»
    assert "подписей больше, чем объектов" not in report
    rec = result.plan.recognition
    assert rec.house_without_text and rec.unlabeled == []
    reasons = sorted(f.skip_reason for f in rec.features if f.category == "house" and f.kind == "area")
    assert reasons == sorted([fl.HOUSE_TEXT_MISSING_REASON, fl.OTHER_HOUSE_REASON])
    # С текстом дома строки нет.
    builder, _app, _doc = com(load_brooklyn())
    assert "ДОМ НЕ ПОДПИСАН" not in builder.label_features(house_text=BROOKLYN_HOUSE).report()


def test_front_yard_fence_is_the_owners_fence_labeled_from_the_street():
    """
    Ограда палисадника стоит перед красной линией, за участком, но она своя:
    одна подпись на длинной стороне, со стороны улицы (у заказчика
    «IRON FENCE 5.4'E» ниже ограды), возвраты не подписываются.
    """
    plan = brooklyn_plan()
    (fence,) = [item for item in plan.items if item.single_line == "IRON FENCE"]
    assert fence.box.centroid.y == pytest.approx(4923.9, abs=0.2)  # ограда на y = 4924.9, улица ниже
    assert math.dist(fence.box.centroid.coords[0], (5003.7, 4923.9)) < 0.5
    short = [item for item in plan.items if item.single_line == "I.FE."]
    assert len(short) == 1 and short[0].box.centroid.x < 4994.0  # только ограда соседа
    # Без правила палисадника ограда считалась соседской: сокращения на возвратах.
    old = brooklyn_plan(cfg={"feature_labeling": {"front_yard_depth_ft": 0.0}})
    assert len([item for item in old.items if item.single_line == "I.FE."]) == 2
    (inside,) = [item for item in old.items if item.single_line == "IRON FENCE"]
    assert inside.box.centroid.y > 4924.9
    # Чертёж без красной линии: правило не действует, остальное не меняется.
    assert len(make_plan(load_ilion(), house=ILION_HOUSE, height=0.9).items) == 33


# ---------------------------------------------------------------------------
# «225 Aspinwall Street, Staten Island»: блоки точек SRVPNO5, описания с «NE»,
# стрелка севера влево, стенки клумб лентами, стена с колоннами, крытое крыльцо
# ---------------------------------------------------------------------------

ASPINWALL_HOUSE = "2 STY\nFRAME\nW/CELLAR\nNo. 225"
#: Итоговый чертёж повёрнут относительно полевого на −75.394° вокруг угла участка.
ASPINWALL_TURN_DEG = -75.394
ASPINWALL_FINAL_PIVOT, ASPINWALL_FIELD_PIVOT = (915462.68, 122338.50), (915530.19, 122465.58)
ASPINWALL_FINAL_DUMP = ROOT / "docs" / "225 Aspinwall Street, Staten Island.final.dump.json"

#: Подписи итогового чертежа заказчика в координатах полевого: (текст программы,
#: текст заказчика, центр, поворот, допуск по расстоянию). Материалы (BRICK, FRAME,
#: CONC.) и слово GATE заказчик берёт с фотографий — программа пишет без них.
ASPINWALL_FINAL_LABELS = [
    ("VINYL FENCE", "VINYL FENCE", (915477.6, 122541.1), 91.9, 1.0),
    ("VINYL FENCE", "VINYL FENCE", (915501.7, 122561.5), 0.0, 6.0),
    ("VINYL FENCE", "VINYL FENCE", (915528.3, 122529.0), 90.0, 1.0),
    # у заказчика — за забором; программа ставит на участке, в 0.8 ft от границы
    ("V.FE.", "VINYL FE.", (915474.0, 122518.6), 90.0, 2.8),
    ("V.FE.", "VIN.FE.", (915533.0, 122561.4), 0.0, 1.0),
    ("V.FE.", "VIN.FE.", (915471.6, 122561.9), 0.0, 1.0),
    ("V.FE.", "VIN.FE.", (915532.8, 122496.6), 0.0, 2.5),   # у заказчика ещё GATE над линией
    ("V.FE.", "VIN.FE.", (915471.5, 122510.4), 0.0, 2.5),   # и здесь
    ("WALL W./COLUMNS", "BRICK WALL W./COLUMNS", (915501.9, 122565.1), 0.0, 8.0),
    ("ROOFED O.P.", "ROOFED O.P.", (915502.8, 122491.8), 0.0, 3.0),
    ("O.P.", "O.P.", (915523.0, 122510.6), 0.0, 1.0),
    ("O.P.", "FRAME DECK", (915502.6, 122538.7), 0.0, 2.5),  # геодезист записал O PORCH
    ("PLANT.", "PLANT.", (915492.1, 122561.1), 0.0, 3.5),
    ("PLANT.", "PLANT.", (915523.3, 122553.3), 0.0, 7.0),
    ("PLANT.", "PLANT.", (915507.9, 122487.7), 0.0, 3.5),
    ("PLANT.", "PLANT.", (915491.3, 122490.9), 0.0, 2.0),
    ("PLANT.", "PLANT.", (915485.7, 122491.5), 0.0, 8.5),
    ("STEPS", "FRAME STEPS", (915524.6, 122496.1), 0.0, 2.5),
    ("STEPS", "CONC. STEPS", (915500.8, 122484.9), 0.0, 1.0),
    ("STEP", "FRAME STEPS", (915515.2, 122540.3), 0.0, 3.5),
]


def aspinwall_to_field(point):
    turn = math.radians(-ASPINWALL_TURN_DEG)
    dx, dy = point[0] - ASPINWALL_FINAL_PIVOT[0], point[1] - ASPINWALL_FINAL_PIVOT[1]
    return (
        ASPINWALL_FIELD_PIVOT[0] + dx * math.cos(turn) - dy * math.sin(turn),
        ASPINWALL_FIELD_PIVOT[1] + dx * math.sin(turn) + dy * math.cos(turn),
    )


def aspinwall_final_texts(layer="D-TOPO-TEXT"):
    """Тексты итогового чертежа: (текст в одну строку, центр в координатах полевого, поворот)."""
    dump = json.loads(ASPINWALL_FINAL_DUMP.read_text(encoding="utf-8"))
    found = []
    for entity in dump["entities"]:
        if entity["type"] not in ("Text", "MText") or entity["layer"] != layer:
            continue
        text = " ".join(fl.plain_text(entity["text"].replace("\\P", " ")).split())
        minx, miny, maxx, maxy = entity["bbox"]
        center = aspinwall_to_field(((minx + maxx) / 2.0, (miny + maxy) / 2.0))
        found.append((text, center, (entity["rot_deg"] - ASPINWALL_TURN_DEG) % 360.0))
    return found


def aspinwall_plan(**kwargs):
    return make_plan(load_aspinwall(), house=ASPINWALL_HOUSE, height=0.9, **kwargs)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("VINYL FENCE NE", "VINYL FENCE"),
        ("WALL NE", "WALL"),
        ("O PORCH NE", "O.P."),
        ("AW OVERH NE", "AWNING"),
        ("CANTILEVER NE", "CANTIL."),
        ("COL  NE", None),       # колонна: известна, сама не подписывается
        ("EDG CONC", None),      # кромка бетона: что по какую сторону, по линии не понять
        ("DW", None),            # заезд: заказчик пишет материал покрытия (с фотографий)
        ("BC", None),            # бортовой камень
        ("NE", None),            # одно слово «NE» — не суффикс, а неизвестное описание
    ],
)
def test_no_elevation_suffix_is_not_part_of_the_object_name(raw, expected):
    """«NE» в конце описания — точка снята без отметки: к названию объекта не относится."""
    snap = DrawingSnapshot(
        lines=[DrawingLine(layer="M-PROPERTY", points=((0, 0), (50, 0), (50, 100), (0, 100)), closed=True)],
        survey_points=[fl.SurveyPoint(layer="ST-MISC", number="7", description=raw, point=(10.0, 10.0))],
    )
    result = recognize(snap, house="")
    assert [code.label for code in result.point_codes] == [expected]
    assert result.unknown_codes == ({"NE": 1} if raw == "NE" else {})
    # Суффикс настраивается: без него описание с «NE» остаётся неизвестным.
    plain = recognize(snap, house="", cfg={"feature_labeling": {"point_description_ignored_suffixes": []}})
    assert bool(plain.unknown_codes) == raw.endswith("NE")


def test_aspinwall_points_of_both_block_kinds_are_understood():
    """Блоки SRVPNO5 (без отметок, «… NE») и srvpno3 (слои «…-ELEV») читаются одинаково."""
    result = recognize(load_aspinwall(), house=ASPINWALL_HOUSE)
    assert result.unknown_codes == {}
    assert result.boundary.area_sqft == pytest.approx(55.0 * 97.5, abs=1.0)
    # Точки ступеней лежат в слое ST-STEP-ELEV, линии — в ST-STEP.
    steps = [f for f in result.features if f.category == "steps" and f.label]
    assert sorted(f.label for f in steps) == ["STEP", "STEPS", "STEPS"]
    assert {f.layer for f in steps} == {"ST-STEP"} and {f.source for f in steps} == {"point_code"}
    fences = [f for f in result.features if f.kind == "fence"]
    assert len(fences) == 6 and {f.label for f in fences} == {"VINYL FENCE"}
    # Кромки бетона и заезда известны, но не подписываются и в «без подписи» не попадают.
    surfaces = [f for f in result.features if f.category == "surface"]
    assert len(surfaces) == 5 and {f.skip_reason for f in surfaces} == {fl.SURFACE_EDGE_REASON}
    # «Без подписи» пусто: соседний дом и обрывок его контура — со своей причиной.
    assert result.unlabeled == []
    neighbours = [f for f in result.features if f.skip_reason == fl.OTHER_HOUSE_REASON]
    assert sorted(f.kind for f in neighbours) == ["area", "edge"]
    # Точки CANTILEVER сняты в стороне от линий — об этом сказано в отчёте.
    assert {code.code for code in result.unbound_descriptions} >= {"CANTILEVER"}


def test_planter_walls_drawn_as_ribbons_are_labeled_as_narrow_planters():
    """
    Стенка клумбы — две линии в 0.8 ft друг от друга без торцов (у заказчика
    заштрихована). Раньше по ним строилась выпуклая оболочка и «PLANTER»
    вставал посреди газона; теперь каждая лента — свой объект «PLANT.».
    """
    result = recognize(load_aspinwall(), house=ASPINWALL_HOUSE)
    planters = [f for f in result.features if f.category == "planter"]
    assert [f.label for f in planters] == ["PLANT."] * 5
    # Площади лент совпадают с площадями штриховок полевого чертежа (37.6, 15.3, 14.0, 7.0).
    areas = sorted(round(f.geometry.area, 1) for f in planters)
    assert areas == pytest.approx([3.4, 3.6, 13.9, 15.3, 37.5], abs=0.15)
    assert all(fl._mean_width(f.geometry) < 1.0 for f in planters)
    # Торцы достраиваются только у длинных лент: без них оболочка возвращается.
    old = recognize(
        load_aspinwall(), house=ASPINWALL_HOUSE,
        cfg={"feature_labeling": {"ribbon_cap_categories": []}},
    )
    assert max(f.geometry.area for f in old.features if f.category == "planter") > 500.0


def test_wall_with_surveyed_columns_and_its_second_line():
    result = recognize(load_aspinwall(), house=ASPINWALL_HOUSE)
    walls = [f for f in result.features if f.category == "wall"]
    (labeled,) = [f for f in walls if f.label]
    assert labeled.label == "WALL W./COLUMNS" and "колонны (5)" in labeled.rule
    reasons = sorted(f.skip_reason for f in walls if not f.label)
    assert reasons == [fl.WALL_SIDE_REASON] * 3 + [fl.WALL_UNDER_FENCE_REASON]
    columns = [f for f in result.features if f.category == "column"]
    assert len(columns) == 6 and {f.skip_reason for f in columns} == {fl.NO_LABEL_CODE_REASON}
    assert all(f.geometry.area == pytest.approx(1.0, abs=0.1) for f in columns)
    # Правило выключается: пустой шаблон — просто WALL.
    plain = recognize(
        load_aspinwall(), house=ASPINWALL_HOUSE,
        cfg={"feature_labeling": {"wall_with_columns_format": ""}},
    )
    assert [f.label for f in plain.features if f.category == "wall" and f.label] == ["WALL"]


def test_awning_over_surveyed_porch_points_is_a_roofed_porch():
    result = recognize(load_aspinwall(), house=ASPINWALL_HOUSE)
    (roofed,) = [f for f in result.features if f.label == "ROOFED\nO.P."]
    assert roofed.layer == "ST-OHED" and roofed.category == "porch"
    assert "AW OVERH" in roofed.rule and "O PORCH NE под козырьком (2 т.)" in roofed.rule
    assert not [f for f in result.features if f.label == "AWNING"]
    plain = recognize(
        load_aspinwall(), house=ASPINWALL_HOUSE, cfg={"feature_labeling": {"roofed_porch_labels": {}}}
    )
    assert [f.label for f in plain.features if f.layer == "ST-OHED" and f.label] == ["AWNING"]
    # Козырёк без точек крыльца под ним («6910 15th Avenue») остаётся AWNING.
    brooklyn = recognize(load_brooklyn(), house=BROOKLYN_HOUSE)
    assert [f.label for f in brooklyn.features if f.category == "awning"] == ["AWNING"]


def test_two_described_parallel_sides_make_one_platform():
    """Крыльцо между стеной дома и лестницей снято двумя боковыми сторонами: подпись внутри."""
    result = recognize(load_aspinwall(), house=ASPINWALL_HOUSE)
    porches = [f for f in result.features if f.label == "O.P."]
    assert sorted(f.kind for f in porches) == ["area", "area"]
    side = min(porches, key=lambda f: f.geometry.area)
    assert not side.closed and side.geometry.area == pytest.approx(22.3, abs=0.5)
    assert sorted(code.number for code in side.codes) == ["31", "32"]
    item = next(i for i in aspinwall_plan().items if i.feature is not None and i.feature.geometry.equals(side.geometry))
    assert item.placement == "inside"

    def line(points):
        return [LineString(points)]

    assert fl._opposite_sides(line([(0, 0), (4, 0)]), line([(0, 5), (4, 5)]), 12.0)
    assert not fl._opposite_sides(line([(0, 0), (4, 0)]), line([(0, 15), (4, 15)]), 12.0)   # далеко
    assert not fl._opposite_sides(line([(0, 0), (4, 0)]), line([(5, 0.2), (9, 0.2)]), 12.0)  # одна линия
    assert not fl._opposite_sides(line([(0, 0), (4, 0)]), line([(10, 5), (14, 5)]), 12.0)    # не напротив
    assert not fl._opposite_sides(line([(0, 0), (4, 0)]), line([(2, 1), (2, 6)]), 12.0)      # поперёк
    assert not fl._opposite_sides(line([(0, 0), (4, 0), (4, 3)]), line([(0, 5), (4, 5)]), 12.0)  # ломаная


def test_aspinwall_plan_matches_the_customer_final_drawing():
    """Подписи стоят там же и повёрнуты так же, как в итоговом чертеже заказчика."""
    plan = aspinwall_plan()
    assert plan.warnings == [] and plan.skipped == []
    assert len(plan.items) == 21
    assert sorted(set(item.single_line for item in plan.items)) == [
        "2 STY FRAME W/CELLAR No. 225", "O.P.", "PLANT.", "ROOFED O.P.", "STEP", "STEPS",
        "V.FE.", "VINYL FENCE", "WALL W./COLUMNS",
    ]
    final = aspinwall_final_texts()
    used = set()
    for text, customer_text, point, rotation, tolerance in ASPINWALL_FINAL_LABELS:
        # Таблица сверена с дампом итогового чертежа.
        assert any(
            found_text == customer_text and math.dist(center, point) < 0.2
            for found_text, center, _rotation in final
        ), (customer_text, point)
        found = [
            (index, item) for index, item in enumerate(plan.items)
            if index not in used and item.single_line == text
            and math.dist(item.box.centroid.coords[0], point) <= tolerance
        ]
        assert found, (text, point)
        index, item = min(found, key=lambda pair: math.dist(pair[1].box.centroid.coords[0], point))
        used.add(index)
        turn = (math.degrees(item.rotation) - rotation + 180.0) % 360.0 - 180.0
        assert abs(turn) < 2.0, (text, point, math.degrees(item.rotation))
    # Все подписи программы, кроме дома, нашли пару в итоговом чертеже.
    assert len(used) == len(plan.items) - 1 == len(ASPINWALL_FINAL_LABELS)
    (house,) = [item for item in plan.items if item.feature.is_main_house]
    (final_house,) = [
        center for text, center, _rotation in aspinwall_final_texts(layer="0") if "No. 225" in text
    ]
    assert math.dist(house.box.centroid.coords[0], final_house) < 1.0
    boxes = [item.box for item in plan.items]
    for i, first in enumerate(boxes):
        for second in boxes[i + 1:]:
            assert not first.intersects(second)


def test_north_arrow_pointing_left_keeps_vertical_text_reading_upwards():
    """
    Стрелка севера влево (165° от оси X): вертикальные подписи читаются снизу
    вверх. Итоговый чертёж заказчика повёрнут севером вверх — его «вертикальные»
    подписи в координатах полевого стоят под 90°, а не под 270°.
    """
    snap = load_aspinwall()
    arrow = fl.find_north_arrow(snap, {})
    assert arrow.north_deg == pytest.approx(165.339, abs=0.01) and arrow.side == "left"
    plan = aspinwall_plan()
    assert plan.vertical_reads == "up" and "смотрит влево" in plan.vertical_reads_note
    vertical = [item for item in plan.items if abs(math.sin(item.rotation)) > 0.7]
    assert len(vertical) == 3 and all(math.sin(item.rotation) > 0.7 for item in vertical)
    turns = [round(rotation) % 360 for _text, _center, rotation in aspinwall_final_texts()]
    assert turns.count(90) >= 15 and turns.count(270) == 0
    # В самом итоговом чертеже (север вверх) те же подписи читаются слева направо.
    dump = json.loads(ASPINWALL_FINAL_DUMP.read_text(encoding="utf-8"))
    (final_arrow,) = [e for e in dump["entities"] if e["type"] == "BlockReference" and e["name"] == "NA"]
    assert final_arrow["rot_deg"] == pytest.approx(0.0, abs=0.1) or final_arrow["rot_deg"] > 359.9
    assert all(math.cos(math.radians(turn + ASPINWALL_TURN_DEG)) > 0.2 for turn in turns if turn in (90, 92))


def test_fence_runs_leaving_the_lot_get_their_own_abbreviation():
    """Свой забор продолжается за угол участка — этот участок подписан отдельно (VIN.FE.)."""
    plan = aspinwall_plan()
    short = [item.box.centroid.coords[0] for item in plan.items if item.single_line == "V.FE."]
    assert len(short) == 5
    # Два первых — участки за углом участка: у заказчика VIN.FE. в тех же местах.
    for expected in [(915472.0, 122562.0), (915472.1, 122512.6), (915476.5, 122518.6),
                     (915532.4, 122498.7), (915532.9, 122561.5)]:
        assert any(math.dist(point, expected) < 0.2 for point in short), expected
    old = aspinwall_plan(cfg={"feature_labeling": {"fence_label_runs_beyond_lot": False}})
    assert len([item for item in old.items if item.single_line == "V.FE."]) == 3


def test_aspinwall_second_run_and_customer_labels_create_nothing_twice(com):
    """Повторный запуск: 0 новых; подписи заказчика (VIN.FE., PLANT., BRICK WALL…) — дубликаты."""
    builder, _app, doc = com(load_aspinwall())
    first = builder.label_features(house_text=ASPINWALL_HOUSE, text_height=0.9)
    assert first.created_labels == 21
    second = builder.label_features(house_text=ASPINWALL_HOUSE, text_height=0.9)
    assert second.created_labels == 0 and len(second.plan.duplicates) >= 15

    snap = load_aspinwall()
    for text, center, rotation in aspinwall_final_texts():
        width = ab.feature_text_width(text, 0.9)
        cos, sin = abs(math.cos(math.radians(rotation))), abs(math.sin(math.radians(rotation)))
        half_x, half_y = (width * cos + 0.9 * sin) / 2.0, (width * sin + 0.9 * cos) / 2.0
        snap.texts.append(
            DrawingText(
                layer="D-TOPO-TEXT", text=text, point=center, height=0.9,
                rotation=math.radians(rotation), kind="AcDbText",
                bbox=(center[0] - half_x, center[1] - half_y, center[0] + half_x, center[1] + half_y),
            )
        )
    plan = make_plan(snap, house="", height=0.9)
    # Единственное расхождение в названии: геодезист записал «O PORCH», а заказчик
    # по фотографии написал FRAME DECK — программа добавит своё O.P. рядом.
    assert [item.single_line for item in plan.items] == ["O.P."]
    assert plan.items[0].box.centroid.y == pytest.approx(122538.0, abs=0.5)
    assert len(plan.duplicates) >= 15


def test_fixture_converter_expands_arcs_like_the_autocad_reader():
    """Дуги и дуговые звенья полилиний в записи чертежа — те же точки, что читает программа."""
    sys.path.insert(0, str(ROOT / "tests"))
    import dump_to_fixture

    half = dump_to_fixture.polyline_points([(0.0, 0.0), (2.0, 0.0)], [1.0, 0.0], False)
    assert half[0] == (0.0, 0.0) and half[-1] == (2.0, 0.0) and len(half) == 19  # полуокружность, 18 хорд
    assert all(math.dist(point, (1.0, 0.0)) == pytest.approx(1.0) for point in half)
    assert min(point[1] for point in half) == pytest.approx(-1.0)  # bulge > 0 — против часовой стрелки
    assert dump_to_fixture.polyline_points([(0.0, 0.0), (2.0, 0.0)], [0.0, 0.0], False) == [(0.0, 0.0), (2.0, 0.0)]
    square = dump_to_fixture.polyline_points([(0, 0), (1, 0), (1, 1), (0, 1)], [], True)
    assert square[0] == square[-1] and len(square) == 5
    arc = dump_to_fixture.arc_points((5.0, 5.0), 2.0, 350.0, 80.0)   # через 0°: 90° дуги, 9 хорд
    assert len(arc) == 10
    assert arc[0] == pytest.approx((5.0 + 2.0 * math.cos(math.radians(350.0)), 5.0 + 2.0 * math.sin(math.radians(350.0))))
    assert arc[-1] == pytest.approx((5.0 + 2.0 * math.cos(math.radians(80.0)), 5.0 + 2.0 * math.sin(math.radians(80.0))))
    # Запись «225 Aspinwall Street» получена этим же преобразованием из дампа.
    target = ROOT / "tests" / "data" / "aspinwall_field.txt"
    copy = Path(str(target) + ".check")
    try:
        stats = dump_to_fixture.convert(
            ROOT / "docs" / "for MLP 225 Aspinwall Street, Staten Island.dump.json", copy,
            "225 Aspinwall Street, Staten Island", digits=4,
        )
        assert copy.read_text(encoding="utf-8") == target.read_text(encoding="utf-8")
    finally:
        copy.unlink(missing_ok=True)
    assert dict(stats) == {"L": 241, "P": 170, "Q": 7, "B": 1, "T": 1, "H": 6}


# ---------------------------------------------------------------------------
# Стыки линий: настоящий чертёж не совпадает «точка в точку» (версия 2.9)
# ---------------------------------------------------------------------------

def snapped(*lines):
    snap = DrawingSnapshot()
    for layer, points in lines:
        snap.lines.append(DrawingLine(layer=layer, points=tuple(points)))
    moved = fl.snap_snapshot_vertices(snap, {})
    return snap, moved


def test_line_ends_closer_than_the_tolerance_become_one_point():
    snap, moved = snapped(
        ("S-FENCE", [(0.0, 0.0), (10.0, 0.0)]),
        ("S-FENCE", [(10.00003, 0.00002), (10.0, 8.0)]),
        ("S-FENCE", [(10.0, 8.01), (0.0, 8.0)]),       # 0.01 ft — уже не «тот же» конец
    )
    assert moved == 1 and snap.vertices_snapped == 1
    assert snap.lines[1].points[0] == (10.0, 0.0)
    assert snap.lines[2].points[0] == (10.0, 8.01)
    assert len(fl.merge_lines([fl.line_to_shapely(line) for line in snap.lines], 0.0)) == 2
    # Повторный вызов ничего не меняет; допуск 0 выключает стягивание.
    assert fl.snap_snapshot_vertices(snap, {}) == 0
    raw = DrawingSnapshot(lines=[DrawingLine("S-FENCE", ((0.0, 0.0), (1.0, 0.0))),
                                 DrawingLine("S-FENCE", ((1.00001, 0.0), (2.0, 0.0)))])
    assert fl.snap_snapshot_vertices(raw, {"feature_labeling": {"vertex_snap_tolerance_ft": 0}}) == 0
    assert raw.lines[1].points[0] == (1.00001, 0.0)


def test_line_end_that_stops_short_of_another_line_gets_a_common_point():
    """Т-образный стык: конец не дошёл до линии на 0.00005 ft — как у стенки клумбы в Aspinwall."""
    snap, moved = snapped(
        ("ST-MISC", [(0.0, 0.0), (10.0, 0.0)]),
        ("ST-MISC", [(4.0, 0.00005), (4.0, 3.0)]),
        ("ST-MISC", [(6.0, -0.00004), (6.0, 3.0)]),
        ("ST-MISC", [(4.0, 3.0), (6.0, 3.0)]),
    )
    assert moved == 2
    assert snap.lines[0].points == ((0.0, 0.0), (4.0, 0.0), (6.0, 0.0), (10.0, 0.0))  # прямая осталась прямой
    assert snap.lines[1].points[0] == (4.0, 0.0) and snap.lines[2].points[0] == (6.0, 0.0)
    faces = fl._polygonize([fl.line_to_shapely(line) for line in snap.lines])
    assert len(faces) == 1 and faces[0].area == pytest.approx(6.0)
    # Без стягивания контур не замыкается.
    loose = [LineString(points) for points in (
        [(0.0, 0.0), (10.0, 0.0)], [(4.0, 0.00005), (4.0, 3.0)], [(6.0, -0.00004), (6.0, 3.0)],
        [(4.0, 3.0), (6.0, 3.0)],
    )]
    assert fl._polygonize(loose) == []


def test_lot_corners_stay_where_the_surveyor_put_them():
    """Точки границы участка не сдвигаются: к ним подтягиваются остальные линии."""
    snap, _moved = snapped(
        ("S-FENCE", [(0.003, 0.002), (0.0, 50.0)]),            # начерчен раньше границы
        ("M-PROPERTY", [(0.0, 0.0), (40.0, 0.0)]),
        ("M-PROPERTY", [(40.0, 0.0), (40.0, 100.0), (0.0, 100.0), (0.0, 0.0)]),
        ("ST-MISC", [(20.0, 0.004), (20.0, 5.0)]),              # упирается в границу
        ("M-PROPERTY", [(40.0, 60.0), (39.997, 60.0)]),         # точка границы у чужой линии
        ("S-FENCE", [(39.999, 55.0), (39.999, 65.0)]),
    )
    by_layer = {}
    for line in snap.lines:
        by_layer.setdefault(line.layer, []).append(line.points)
    assert by_layer["S-FENCE"][0][0] == (0.0, 0.0)
    assert by_layer["ST-MISC"][0][0] == (20.0, 0.0)
    assert by_layer["M-PROPERTY"][0] == ((0.0, 0.0), (20.0, 0.0), (40.0, 0.0))
    boundary = fl.find_property_polygon(snap, {})
    assert boundary.area_sqft == pytest.approx(4000.0, abs=1e-6)
    assert boundary.vertex_count == 4       # точки примыкания на прямой стороне — не углы


def test_fence_is_not_cut_where_another_fence_joins_it():
    """Забор, к которому сбоку примыкает возврат, остаётся одной линией (Aspinwall: 118.4 ft)."""
    lines = [
        LineString([(0.0, 0.0), (30.0, 0.0)]), LineString([(30.0, 0.0), (60.0, 0.5)]),
        LineString([(30.0, 0.0), (30.0, -6.0)]),
    ]
    assert sorted(round(line.length, 1) for line in fl.merge_lines(lines, 0.0)) == [6.0, 30.0, 30.0]
    through = fl.merge_lines(lines, 0.0, through_turn_deg=20.0)
    assert sorted(round(line.length, 1) for line in through) == [6.0, 60.0]
    # Угол забора (поворот 90°) с примыкающим третьим забором — три отдельные линии.
    corner = [
        LineString([(0.0, 0.0), (30.0, 0.0)]), LineString([(30.0, 0.0), (30.0, 20.0)]),
        LineString([(30.0, 0.0), (45.0, -15.0)]),
    ]
    assert len(fl.merge_lines(corner, 0.0, through_turn_deg=20.0)) == 3
    # Перекрёсток: обе линии проходят насквозь.
    cross = [LineString([(0.0, 0.0), (10.0, 0.0)]), LineString([(5.0, -5.0), (5.0, 5.0)])]
    assert sorted(round(line.length, 1) for line in fl.merge_lines(cross, 0.0, through_turn_deg=20.0)) == [10.0, 10.0]


def test_stair_drawn_as_a_comb_is_one_object_even_when_a_cell_closes():
    """
    Лестница снята «гребёнкой»: проступи упираются в боковую линию. Замкнулась
    только средняя ячейка — она не отдельный объект, а часть той же лестницы.
    """
    snap = DrawingSnapshot()
    for points in (
        [(0.0, 0.0), (40.0, 0.0), (40.0, 100.0), (0.0, 100.0), (0.0, 0.0)],
    ):
        snap.lines.append(DrawingLine("M-PROPERTY", tuple(points)))
    snap.lines.append(DrawingLine("ST-BLDG-LINE", ((5.0, 30.0), (35.0, 30.0), (35.0, 70.0), (5.0, 70.0)), closed=True))
    for points in (
        [(10.0, 29.0), (16.0, 29.0)], [(10.0, 28.0), (16.0, 28.0)], [(10.0, 27.0), (16.0, 27.0)],
        [(10.0, 26.0), (16.0, 26.0)],
        [(16.0, 30.0), (16.0, 26.0)],            # правая боковая — целиком
        [(10.0, 29.0), (10.0, 28.0)],            # левая — только у средней ячейки
    ):
        snap.lines.append(DrawingLine("ST-STEP", tuple(points)))
    for number, point in (("1", (10.0, 26.0)), ("2", (16.0, 26.0))):
        snap.survey_points.append(
            fl.SurveyPoint(layer="ST-STEP", number=number, description="STEP", point=point,
                           elevation="100.00", block="srvpno3")
        )
    result = recognize(snap, house="2 STY")
    steps = [f for f in result.features if f.category == "steps"]
    assert len(steps) == 1, [(f.kind, f.label, f.geometry.bounds) for f in steps]
    assert steps[0].kind == "area" and steps[0].flat_label == "STEPS"
    assert steps[0].geometry.bounds == pytest.approx((10.0, 26.0, 16.0, 30.0))
    assert sorted(code.number for code in steps[0].codes) == ["1", "2"]


def noisy_aspinwall(seed, amplitude=5e-5):
    """Чертёж с координатами, сдвинутыми в пятом знаке, — как их хранит AutoCAD."""
    import random

    randomizer = random.Random(seed)
    snap = load_aspinwall()
    snap.lines[:] = [
        DrawingLine(
            layer=line.layer, closed=line.closed,
            points=tuple(
                (x + randomizer.uniform(-amplitude, amplitude), y + randomizer.uniform(-amplitude, amplitude))
                for x, y in line.points
            ),
        )
        for line in snap.lines
    ]
    return snap


def plan_signature(plan):
    return sorted(
        (item.single_line, item.placement, round(item.box.centroid.x), round(item.box.centroid.y))
        for item in plan.items
    )


def test_result_does_not_depend_on_the_last_digits_of_coordinates():
    """
    Запуск 2.8 в AutoCAD дал PLANTER посреди газона и разорванный забор: в дампе
    координаты округлены до 4-го знака, в чертеже концы линий расходятся в 5-м.
    Теперь сдвиг в 5-м знаке результата не меняет.
    """
    expected = plan_signature(aspinwall_plan())
    for seed in range(4):
        plan = make_plan(noisy_aspinwall(seed), house=ASPINWALL_HOUSE, height=0.9)
        assert plan_signature(plan) == expected, seed
        assert plan.warnings == [] and plan.recognition.unlabeled == []
    # Со стягиванием, выключенным в настройках, тот же чертёж даёт то, что вышло в AutoCAD.
    off = {"feature_labeling": {"vertex_snap_tolerance_ft": 0}}
    broken = [
        make_plan(noisy_aspinwall(seed), house=ASPINWALL_HOUSE, height=0.9, cfg=off) for seed in range(4)
    ]
    assert any(plan_signature(plan) != expected for plan in broken)
    assert any("PLANTER" in [item.single_line for item in plan.items] for plan in broken)


def test_report_counts_the_joined_line_ends():
    plan = aspinwall_plan()
    assert plan.junctions_snapped > 0 and plan.junction_tolerance == pytest.approx(0.005)
    report = ab.FeatureLabelResult(plan=plan, layer=plan.output_layer, dry_run=True).report()
    assert f"Стыки линий: {plan.junctions_snapped} сведено в общие точки" in report
    assert "меньше чем на 0.005 ft" in report
    # В чертеже с точными стыками строки нет.
    clean = make_plan(build_scene(0.0))
    if not clean.junctions_snapped:
        assert "Стыки линий" not in ab.FeatureLabelResult(
            plan=clean, layer=clean.output_layer, dry_run=True
        ).report()


def test_gate_is_never_guessed_from_a_short_fence_return():
    """
    Ответ заказчика: калитку снимают как обычный забор, GATE в точках съёмки нет —
    её определяют по эскизу, фото или street view. Без явной команды GATE не ставится.
    """
    plan = aspinwall_plan()
    assert not any("GATE" in item.single_line for item in plan.items)
    assert not any(feature.category == "gate" for feature in plan.recognition.features)
    # Возвраты забора от дома к боковой линии (у заказчика VIN.FE. / GATE) — просто V.FE.
    short = [item for item in plan.items if item.single_line == "V.FE." and item.feature.geometry.length < 8.0]
    assert len(short) >= 2
    # Явная команда — полевая заметка или слой калиток — работает как раньше.
    gates = [item for item in stafford_plan().items if "GATE" in item.single_line]
    assert gates and all(item.feature.category == "gate" for item in gates)


# ---------------------------------------------------------------------------
# Правила заказчика о высотах текста и стене под забором (версия 3.0)
# ---------------------------------------------------------------------------

def test_text_heights_follow_the_customer_table_for_every_scale():
    """
    0.9 — подписи объектов, 1.1 — отступы до дома, 1.5 — описание дома при 1"=16';
    для масштабов 10, 20, 30, 40, 50, 60 — коэффициенты 0.625, 1.25, 1.875, 2.5, 3.125, 3.75.
    """
    builder = ab.AcadBuilder({})
    fcfg = fl.feature_settings({})
    assert fcfg["standard_text_heights_ft"] == {"label": 0.9, "house_offset": 1.1, "house": 1.5}
    factors = {10: 0.625, 16: 1.0, 20: 1.25, 30: 1.875, 40: 2.5, 50: 3.125, 60: 3.75}
    for scale, factor in factors.items():
        label = builder.feature_text_height(scale)
        assert label == pytest.approx(0.9 * factor)
        assert ab.house_offset_height(label, fcfg) == pytest.approx(1.1 * factor)
        assert ab.house_label_height(label, fcfg) == pytest.approx(1.5 * factor)
    # Масштабы заказчика есть в списке окна программы.
    import json

    shipped = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    assert shipped["drawing"]["available_scales"] == [10.0, 16.0, 20.0, 30.0, 40.0, 50.0, 60.0]
    # В плане: дом ровно 1.5 ft (раньше 0.9 × 1.667 = 1.5003), при масштабе 20 — 1.875 ft.
    (house,) = [item for item in aspinwall_plan().items if item.feature.is_main_house]
    assert house.height == 1.5 and (house.layer, house.style) == ("0", "RDS")
    plan20 = make_plan(load_aspinwall(), house=ASPINWALL_HOUSE, height=builder.feature_text_height(20))
    (house20,) = [item for item in plan20.items if item.feature.is_main_house]
    assert house20.height == pytest.approx(1.875)
    assert {round(item.height, 3) for item in plan20.items if not item.feature.is_main_house} == {1.125}


def small_house_scene(width, depth):
    snap = DrawingSnapshot()
    snap.lines.append(DrawingLine("M-PROPERTY", ((0.0, 0.0), (40.0, 0.0), (40.0, 100.0), (0.0, 100.0), (0.0, 0.0))))
    snap.lines.append(DrawingLine(
        "ST-BLDG-LINE", ((10.0, 40.0), (10.0 + width, 40.0), (10.0 + width, 40.0 + depth), (10.0, 40.0 + depth)),
        closed=True,
    ))
    return snap


def test_house_description_is_made_smaller_when_it_does_not_fit_the_outline():
    """«Если надпись дома плохо вмещается в контур дома, допускается уменьшать высоту»."""
    def house_item(width, depth, **cfg):
        plan = make_plan(
            small_house_scene(width, depth), house=ASPINWALL_HOUSE, height=0.9,
            cfg={"feature_labeling": cfg} if cfg else None,
        )
        (item,) = plan.items
        return plan, item

    plan, item = house_item(25.0, 30.0)
    assert item.height == 1.5 and item.note == "" and plan.warnings == []
    # Контур 10 × 9 ft: 1.5 ft не помещается — высота снижается по 0.1 ft, пока не поместится.
    plan, item = house_item(10.0, 9.0)
    assert item.height == pytest.approx(1.2) and plan.warnings == []
    assert item.note == "высота уменьшена с 1.50 до 1.20 ft: описание не помещается в контур дома"
    assert shapely_box(10.0, 40.0, 20.0, 49.0).contains(item.box)
    report = ab.FeatureLabelResult(plan=plan, layer=plan.output_layer, dry_run=True).report()
    assert "слой 0, высота 1.20 ft, стиль RDS — высота уменьшена с 1.50 до 1.20 ft" in report
    # Мельче обычной подписи (0.9 ft) описание не делается: дальше — предупреждение.
    plan, item = house_item(5.0, 5.0)
    assert item.height == pytest.approx(0.9) and len(plan.warnings) == 1
    # Правило выключается настройкой: тогда как раньше — полной высотой с предупреждением.
    plan, item = house_item(10.0, 9.0, house_label_shrink_to_fit=False)
    assert item.height == 1.5 and len(plan.warnings) == 1


def test_fence_on_a_wall_is_written_with_a_concrete_wall():
    """
    «C.L.F. ATOP WALL» пишется «C.L.F. ATOP CONC. WALL», а где места мало —
    «C.L.F. ATOP C.WALL»: стена под забором почти всегда бетонная.
    """
    def scene(length, wall_code="WALL"):
        snap = DrawingSnapshot()
        snap.lines.append(DrawingLine("M-PROPERTY", ((0.0, 0.0), (60.0, 0.0), (60.0, 100.0), (0.0, 100.0), (0.0, 0.0))))
        snap.lines.append(DrawingLine("ST-BLDG-LINE", ((15.0, 40.0), (45.0, 40.0), (45.0, 70.0), (15.0, 70.0)), closed=True))
        x0, x1 = 30.0 - length / 2.0, 30.0 + length / 2.0
        snap.lines.append(DrawingLine("ST-WALL", ((x0, 90.0), (x1, 90.0), (x1, 90.8), (x0, 90.8)), closed=True))
        snap.lines.append(DrawingLine("S-FENCE", ((x0, 90.4), (x1, 90.4))))
        for number, x, description, layer in (
            ("1", x0, "F E C", "S-FENCE"), ("2", x1, "F E C", "S-FENCE"),
            ("3", x0, wall_code, "ST-WALL"), ("4", x1, wall_code, "ST-WALL"),
        ):
            y = 90.4 if layer == "S-FENCE" else 90.0
            snap.survey_points.append(fl.SurveyPoint(
                layer=layer, number=number, description=description, point=(x, y),
                elevation="100.00", block="srvpno3",
            ))
        return snap

    def atop_texts(length, **kwargs):
        plan = make_plan(scene(length, **kwargs), house="2 STY", height=0.9)
        return [(item.text, item.placement) for item in plan.items if " ATOP" in item.text]

    # Длинный участок — полное название; короче — сокращение; ещё короче — «C.WALL»;
    # совсем коротко — выноска в две строки (на ней места хватает для «CONC. WALL»).
    assert atop_texts(56.0) == [("CHAIN LINK FENCE ATOP CONC. WALL", "along")]
    assert atop_texts(30.0) == [("C.L.F. ATOP CONC. WALL", "along")]
    assert atop_texts(17.0) == [("C.L.F. ATOP C.WALL", "along")]
    assert atop_texts(12.0) == [("C.L.F. ATOP\nCONC. WALL", "leader")]
    # Стена, названная иначе, чем просто «WALL», пишется как названа; правило выключается.
    for cfg in ({"fence_atop_plain_wall_names": []}, {"fence_atop_wall_default": ""}):
        plan = make_plan(scene(30.0), house="2 STY", height=0.9, cfg={"feature_labeling": cfg})
        assert [item.text for item in plan.items if " ATOP" in item.text] == ["C.L.F. ATOP WALL"]


def test_drawing_is_regenerated_after_labeling(com):
    """После создания подписей — то же, что _REGENALL: видны типы линий заборов."""
    builder, _app, doc = com(load_brooklyn())
    builder.label_features(house_text=BROOKLYN_HOUSE)
    assert doc.regens == [ab.AC_REGEN_ALL_VIEWPORTS] == [1]
    builder, _app, doc = com(load_brooklyn(), feature_cfg={"regen_after_labeling": False})
    builder.label_features(house_text=BROOKLYN_HOUSE)
    assert not getattr(doc, "regens", [])


# ---------------------------------------------------------------------------
# Правила заказчика от 15.11.2026 (вторая часть): просветы до линий, сторона
# подписи забора, DRIVEWAY вдоль движения, длинная ступень с изломом, O.P. по
# центру площадки, выноска QLEADER со стрелкой.
# ---------------------------------------------------------------------------

def fence_strip_scene(fence_x):
    """Участок 40 × 100 ft с домом и сетчатым забором вдоль правой границы (x = 40)."""
    snap = small_house_scene(20.0, 30.0)
    snap.lines.append(DrawingLine("FENCE_CHAIN_RIGHT", ((fence_x, 30.0), (fence_x, 80.0))))
    return snap


def chain_label(plan):
    (item,) = [i for i in plan.items if i.feature.layer == "FENCE_CHAIN_RIGHT"]
    assert item.placement == "along"
    return item


def test_text_along_a_line_keeps_the_customers_gaps():
    """
    0.7 ft от линии забора, 0.8 ft от границы участка (масштаб 1"=16', текст 0.9 ft);
    для других масштабов — пропорционально высоте текста.
    """
    assert fl.DEFAULT_FEATURE_CONFIG["along_line_gaps_ft"] == {
        "fence": 0.7, "house": 0.7, "property": 0.8, "other": 0.55,
    }
    lot_line = LineString([(40.0, 0.0), (40.0, 100.0)])
    # Забор далеко от границы: текст на стороне участка, ровно в 0.7 ft от линии.
    item = chain_label(make_plan(fence_strip_scene(35.0), house=ASPINWALL_HOUSE, height=0.9))
    fence = LineString([(35.0, 30.0), (35.0, 80.0)])
    assert item.box.centroid.x < 35.0
    assert item.box.distance(fence) == pytest.approx(0.7, abs=1e-6)
    # Забор в 0.1 ft от границы: текст отодвинут так, чтобы до границы было 0.8 ft.
    item = chain_label(make_plan(fence_strip_scene(39.9), house=ASPINWALL_HOUSE, height=0.9))
    assert item.box.centroid.x < 39.9
    assert 0.8 - 1e-6 <= item.box.distance(lot_line) < 0.8 + 0.06 * 0.9
    # Масштаб 1"=20': текст 1.125 ft, просветы 0.875 и 1.0 ft.
    height = 0.9 * 20.0 / 16.0
    item = chain_label(make_plan(fence_strip_scene(35.0), house=ASPINWALL_HOUSE, height=height))
    assert item.box.distance(fence) == pytest.approx(0.7 * 20.0 / 16.0, abs=1e-6)
    item = chain_label(make_plan(fence_strip_scene(39.9), house=ASPINWALL_HOUSE, height=height))
    assert item.box.distance(lot_line) >= 0.8 * 20.0 / 16.0 - 1e-6


def test_fence_label_is_not_squeezed_between_the_fence_and_the_lot_line():
    """
    Забор за забором: между его линией и границей участка 1.8 ft — подпись с
    просветами 0.7 и 0.8 ft туда не помещается и ставится за линию забора.
    """
    snap = fence_strip_scene(41.8)
    item = chain_label(make_plan(snap, house=ASPINWALL_HOUSE, height=0.9))
    assert item.box.bounds[0] >= 41.8 + 0.7 - 1e-6           # справа от забора, не в полосе
    # Прежнее правило ставило подпись в полосу, вплотную к обеим линиям.
    old = chain_label(make_plan(
        snap, house=ASPINWALL_HOUSE, height=0.9, cfg={"feature_labeling": {"along_line_gaps_ft": {}}},
    ))
    assert 40.0 < old.box.centroid.x < 41.8

    # «12 Ilion Place»: C.L.F. справа от дома, напротив его середины (замечание заказчика).
    plan = make_plan(load_ilion(), house=ILION_HOUSE, height=0.9)
    (label,) = [
        i for i in plan.items
        if i.single_line == "C.L.F." and i.placement == "along" and abs(i.box.centroid.y - 5040.1) < 1.0
    ]
    assert label.box.bounds[0] > 5041.8 + 0.6                # за линией забора (x ≈ 5041.8)


def test_driveway_text_runs_along_the_direction_of_travel():
    """DRIVEWAY пишется вдоль движения по заезду; не хватает места — DRIVE."""
    assert fl.DEFAULT_FEATURE_CONFIG["travel_direction_labels"] == {"DRIVEWAY": "DRIVE"}
    plan = make_plan(load_ilion(), house=ILION_HOUSE, height=0.9)
    (item,) = [i for i in plan.items if i.single_line == "DRIVEWAY"]
    # Заезд 9.7 ft поперёк и 17.4 ft вдоль: текст вертикальный (у заказчика 90.4°).
    assert item.placement == "inside"
    assert math.degrees(item.rotation) == pytest.approx(90.0, abs=1.0)
    # Без правила текст стоял по горизонтали — поперёк движения.
    old = make_plan(
        load_ilion(), house=ILION_HOUSE, height=0.9,
        cfg={"feature_labeling": {"travel_direction_labels": {}}},
    )
    (item,) = [i for i in old.items if i.single_line == "DRIVEWAY"]
    assert math.degrees(item.rotation) == pytest.approx(0.0, abs=1.0)

    def driveway_item(width, length):
        snap = small_house_scene(20.0, 30.0)
        snap.lines.append(DrawingLine(
            "DRIVEWAY_EDGE",
            ((30.0, 5.0), (30.0 + width, 5.0), (30.0 + width, 5.0 + length), (30.0, 5.0 + length)),
            closed=True,
        ))
        plan = make_plan(snap, house=ASPINWALL_HOUSE, height=0.9)
        (item,) = [i for i in plan.items if i.feature.layer == "DRIVEWAY_EDGE"]
        return item

    # Заезд 5 × 6.5 ft: вдоль движения полное слово (6.1 ft) не помещается — DRIVE.
    item = driveway_item(5.0, 6.5)
    assert item.text == "DRIVE" and math.degrees(item.rotation) == pytest.approx(90.0, abs=0.1)
    synonyms = fl.DEFAULT_FEATURE_CONFIG["label_synonyms"]
    assert fl.labels_similar("DRIVE", "DRIVEWAY", synonyms) and fl.labels_similar("DRIVEWAY", "DRIVE", synonyms)
    # Заезд 6 × 20 ft: DRIVEWAY вдоль.
    item = driveway_item(6.0, 20.0)
    assert item.text == "DRIVEWAY" and math.degrees(item.rotation) == pytest.approx(90.0, abs=0.1)


def test_long_step_with_a_jog_is_labeled_at_the_middle_of_each_long_run():
    """
    Ступень через весь задний двор: два длинных участка, соединённых коротким.
    STEP ставится дважды — у середины каждого длинного участка (у заказчика так же).
    """
    plan = make_plan(load_ilion(), house=ILION_HOUSE, height=0.9)
    long_step = [
        i for i in plan.items if i.single_line == "STEP" and i.feature.geometry.length > 80.0
    ]
    assert len(long_step) == 2 and all(i.placement == "beside" for i in long_step)
    centers = sorted(i.box.centroid.coords[0] for i in long_step)
    # Подписи заказчика: (5003.8, 5009.3) и (5025.2, 5005.7).
    assert math.dist(centers[0], (5003.8, 5009.3)) < 2.5
    assert math.dist(centers[1], (5025.2, 5005.7)) < 2.5
    step = long_step[0].feature.geometry
    for item in long_step:
        assert math.degrees(item.rotation) == pytest.approx(0.0, abs=1.0)
        assert item.box.distance(step) == pytest.approx(0.55, abs=0.05)   # «прочие линии» — 0.55 ft
    # Правило выключено — одна подпись, как раньше.
    old = make_plan(
        load_ilion(), house=ILION_HOUSE, height=0.9,
        cfg={"feature_labeling": {"arm_label_categories": []}},
    )
    assert len([
        i for i in old.items if i.single_line == "STEP" and i.feature.geometry.length > 80.0
    ]) == 1


def test_open_porch_label_sits_in_the_middle_of_the_porch_itself():
    """
    Крыльцо обведено вместе со ступенями (три проступи в том же слое): O.P.
    ставится по центру самой площадки, а не общего контура.
    """
    plan = make_plan(load_ilion(), house=ILION_HOUSE, height=0.9)
    (item,) = [i for i in plan.items if i.single_line == "O.P."]
    porch = shapely_box(5002.81, 5039.79, 5006.23, 5046.07)   # площадка без ступеней
    assert item.placement == "inside" and porch.contains(item.box)
    center = item.box.centroid
    assert (center.x, center.y) == pytest.approx((porch.centroid.x, porch.centroid.y), abs=0.1)
    # Раньше подпись стояла в центре контура «площадка + ступени», у самой проступи.
    old = make_plan(
        load_ilion(), house=ILION_HOUSE, height=0.9,
        cfg={"feature_labeling": {"main_cell_categories": []}},
    )
    (item,) = [i for i in old.items if i.single_line == "O.P."]
    assert item.box.centroid.y == pytest.approx(5044.43, abs=0.1)


def test_every_leader_is_long_enough_for_its_arrowhead_and_carries_its_text(com):
    """
    AutoCAD рисует стрелку объекта Leader, только если выноска длиннее двух
    стрелок (2 × 1.2 = 2.4 ft). У заказчика самая короткая выноска — 2.50 ft,
    текст всегда привязан к выноске (QLEADER).
    """
    for name, plan in (
        ("ilion", make_plan(load_ilion(), house=ILION_HOUSE, height=0.9)),
        ("brooklyn", brooklyn_plan()),
        ("aspinwall", aspinwall_plan()),
    ):
        leaders = [i for i in plan.items if i.kind == "leader"]
        assert leaders, name
        for item in leaders:
            length = math.dist(item.leader_points[0], item.leader_points[1])
            assert length >= 2.5, (name, item.single_line, length)
            assert item.attach_annotation, (name, item.single_line)

    # Чужая настройка с короткой выноской: стрелка уменьшается, чтобы AutoCAD её нарисовал.
    builder, _app, doc = com(load_ilion(), feature_cfg={"leader_min_length_factor": 1.5})
    original = doc.ModelSpace.AddLeader

    def styled_leader(coordinates, annotation, leader_type):
        leader = original(coordinates, annotation, leader_type)
        leader.ArrowheadSize, leader.ScaleFactor = 1.2, 1.0
        return leader

    doc.ModelSpace.AddLeader = styled_leader
    result = builder.label_features(house_text=ILION_HOUSE, text_height=0.9)
    leaders = [e for e in doc.ModelSpace.created if e.ObjectName == "AcDbLeader"]
    assert leaders and result.created_leader_attached == len(leaders)
    short = 0
    for leader in leaders:
        x0, y0, _z0, x1, y1, _z1 = leader.Coordinates
        length = math.hypot(x1 - x0, y1 - y0)
        assert length >= 2.1 * leader.ArrowheadSize - 1e-9
        short += leader.ArrowheadSize < 1.2
    assert short >= 1

    # AutoCAD не принял текст в AddLeader: Leader создаётся без него, текст
    # привязывается свойством Annotation.
    builder, _app, doc = com(load_ilion())
    original = doc.ModelSpace.AddLeader

    def picky_leader(coordinates, annotation, leader_type):
        if annotation is not None:
            raise RuntimeError("Invalid argument Annotation")
        return original(coordinates, annotation, leader_type)

    doc.ModelSpace.AddLeader = picky_leader
    result = builder.label_features(house_text=ILION_HOUSE, text_height=0.9)
    leaders = [e for e in doc.ModelSpace.created if e.ObjectName == "AcDbLeader"]
    assert result.warnings == [] and result.created_leader_attached == len(leaders) == 3
    assert all(leader.Annotation is not None for leader in leaders)

    # Привязать текст не удалось вовсе — отчёт говорит об этом и называет причину.
    class LooseLeader(FakeLeader):
        @property
        def Annotation(self):
            return None

        @Annotation.setter
        def Annotation(self, value):
            if value is not None:
                raise RuntimeError("Annotation is read-only")

    builder, _app, doc = com(load_ilion())

    def loose_leader(coordinates, annotation, leader_type):
        if annotation is not None:
            raise RuntimeError("Invalid argument Annotation")
        return doc.ModelSpace._created(
            LooseLeader("AcDbLeader", "0", Coordinates=list(coordinates), Type=leader_type)
        )

    doc.ModelSpace.AddLeader = loose_leader
    result = builder.label_features(house_text=ILION_HOUSE, text_height=0.9)
    assert (result.created_leader_objects, result.created_leader_attached) == (3, 0)
    (warning,) = [w for w in result.warnings if "Leader" in w]
    assert "не привязал к нему текст" in warning
    assert "Invalid argument Annotation" in warning and "Annotation is read-only" in warning
    assert "объектов Leader: 3, из них с привязанным текстом (QLEADER): 0" in result.report()


def test_fence_on_wall_duplicate_check_knows_abbreviations_both_ways():
    """Полное название на чертеже и сокращение в новой подписи (и наоборот) — одна и та же подпись."""
    synonyms = fl.DEFAULT_FEATURE_CONFIG["label_synonyms"]
    for existing, label in (
        ("CHAIN LINK FENCE ATOP MASONRY WALL", "C.L.F. ATOP CONC. WALL"),
        ("C.L.F. ATOP C.WALL", "CHAIN LINK FENCE ATOP CONC. WALL"),
        ("C.L.F. ATOP CONC. WALL", "C.L.F. ATOP C.WALL"),
        ("VINYL FENCE ATOP MASONRY WALL", "V.FE. ATOP CONC. WALL"),
    ):
        assert fl.labels_similar(existing, label, synonyms), (existing, label)
    assert not fl.labels_similar("VINYL FENCE ATOP MASONRY WALL", "C.L.F. ATOP CONC. WALL", synonyms)
    assert not fl.labels_similar("C.L.F. ATOP CONC. WALL", "C.L.F.", synonyms)
