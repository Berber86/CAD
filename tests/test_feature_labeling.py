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
    границы, ``with_existing`` добавляет уже стоящие на чертеже надписи.
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
        text("FENCE_NOTE", "CHAIN LINK FENCE", (2, 60), rotation=math.pi / 2)
        text("D-TOPO-TEXT", "SHED", (31, 90.5))
        text("SURFACE_TEXT", "CONC. WALK", (40, -7))
    return snap


def recognize(snap, house=HOUSE, cfg=None):
    boundary = fl.find_property_polygon(snap, cfg or {})
    return fl.recognize_features(snap, boundary, cfg or {}, house)


def labels_of(result, layer):
    return sorted(str(f.label) for f in result.features if f.layer == layer)


# ----------------------------------------------------------------- настройки

def test_shipped_config_mirrors_feature_defaults():
    """config.json содержит раздел feature_labeling, совпадающий с умолчаниями модуля."""
    shipped = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    section = shipped["feature_labeling"]
    assert section == fl.DEFAULT_FEATURE_CONFIG
    # Ключи из постановки задачи.
    assert section["property_line_layers"] == [
        "PROPERTY_LINE", "PL", "M-TAX-LINE", "PROPERTY_LINES_APPROX",
    ]
    assert section["point_label_layers"] == ["POINT_LABELS", "PNT-X", "PNTS"]
    assert section["label_output_layer"] == "D-TOPO-TEXT"
    assert section["point_to_fence_max_ft"] == 3.5
    assert section["duplicate_radius_ft"] == 6.0
    assert section["point_code_dictionary"]["FEV*"] == "VINYL FENCE"
    assert section["layer_label_mapping"]["REAR_DECK_EDGE"] == "WOOD DECK"
    assert set(section["structure_layers"]) == {"house", "shed", "porch", "steps", "surface"}
    # load_config() доносит раздел до AcadBuilder без изменений.
    assert fl.feature_settings(load_config()) == fl.DEFAULT_FEATURE_CONFIG


def test_feature_settings_override_replaces_key_and_null_keeps_default():
    cfg = {"feature_labeling": {"point_to_fence_max_ft": 5.0, "search_buffer_ft": None,
                                "layer_label_mapping": {"MY_SHED": "SHED"}}}
    settings = fl.feature_settings(cfg)
    assert settings["point_to_fence_max_ft"] == 5.0
    assert settings["search_buffer_ft"] == 50.0
    assert settings["layer_label_mapping"] == {"MY_SHED": "SHED"}
    assert fl.DEFAULT_FEATURE_CONFIG["layer_label_mapping"]["SIDE_PORCH"] == "PORCH"


def test_layer_classifier_fence_patterns_and_note_exclusion():
    classifier = fl.LayerClassifier(fl.feature_settings({}))
    for name in ("S-FENCE", "ST-WALL", "CODE_CREW", "FENCE_CHAIN_LEFT", "GATE_REAR", "low_edge_timber"):
        assert classifier.is_fence(name), name
    for name in ("FENCE_NOTE", "GATE_NOTE", "BUILDING_OUTLINE", "POINT_LABELS"):
        assert not classifier.is_fence(name), name
    assert classifier.is_point_label("pnt-x")
    assert classifier.is_existing_label("FENCE_NOTE")
    assert classifier.is_existing_label("D-TOPO-TEXT")


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
    assert fl.labels_similar("CONC. WALK", "CONCRETE WALK", synonyms)
    assert fl.labels_similar("3 STEPS", "STEPS", synonyms)
    assert not fl.labels_similar("WOOD FENCE", "CHAIN LINK FENCE", synonyms)
    assert not fl.labels_similar("SHED", "WOOD DECK", synonyms)


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

    # Источник 1: коды точек делят общий слой CODE_CREW по материалу забора.
    crew = {f.label: f for f in result.features if f.layer == "CODE_CREW"}
    assert set(crew) == {"CHAIN LINK FENCE", "VINYL FENCE"}
    assert crew["CHAIN LINK FENCE"].source == "point_code"
    assert crew["CHAIN LINK FENCE"].geometry.length == pytest.approx(97.6, abs=0.2)
    assert crew["VINYL FENCE"].geometry.length == pytest.approx(39.2, abs=0.2)
    assert {code.code for code in crew["VINYL FENCE"].codes} == {"FEV64"}

    # Источник 2: имена слоёв.
    assert labels_of(result, "FENCE_WOOD_RIGHT") == ["WOOD FENCE"]
    assert labels_of(result, "FENCE_CHAIN_FRONT") == ["CHAIN LINK FENCE"]
    assert labels_of(result, "LOW_EDGE_TIMBER") == ["LOW TIMBER EDGE"]
    assert labels_of(result, "SUBJECT_SHED_OUTLINE") == ["SHED"]
    assert labels_of(result, "SIDE_PORCH") == ["PORCH"]
    assert labels_of(result, "FRONT_LANDING") == ["PORCH"]
    assert labels_of(result, "REAR_DECK_EDGE") == ["WOOD DECK"]
    assert labels_of(result, "DRIVEWAY_EDGE") == ["DRIVEWAY"]
    assert labels_of(result, "SIDEWALK_EDGE") == ["CONCRETE WALK"]
    assert labels_of(result, "WALK_EDGE") == ["CONCRETE WALK"]
    assert labels_of(result, "PLANTER_EDGE") == ["PLANTER"]
    # Проступи *_STEP_TREADS внутри контура ступеней дают «3 STEPS».
    assert labels_of(result, "FRONT_STEPS") == ["3 STEPS"]
    assert labels_of(result, "REAR_STEPS") == ["STEPS"]
    steps = next(f for f in result.features if f.layer == "FRONT_STEPS")
    assert steps.tread_count == 3

    # Дом подписывается текстом из окна программы.
    house = next(f for f in result.features if f.layer == "BUILDING_OUTLINE")
    assert house.is_main_house and house.label == HOUSE and house.source == "user"

    # Точечные объекты по кодам; точка за буфером 50 ft не учитывается.
    points = {f.label for f in result.features if f.kind == "point"}
    assert points == {"UTILITY POLE", "MANHOLE"}
    assert result.unknown_codes == {"XYZ": 1}
    assert len(result.point_codes) == 8

    # Слои без правила найдены, но остаются без подписи.
    unlabeled = {f.layer for f in result.unlabeled}
    assert unlabeled == {
        "GATE_FRONT", "ST-WALL", "GRASS_STRIP_EDGE", "NEIGHBOUR_SHED_OUTLINE", "REF_VEC_BUILDING",
    }
    assert (result.by_point_codes, result.by_layers, result.by_user) == (4, 13, 1)
    assert len(result.labeled) == 18


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

    boxes = [item.box for item in plan.items]
    for i, first in enumerate(boxes):
        for second in boxes[i + 1:]:
            assert not first.intersects(second)

    # Новые тексты не ложатся на линии чертежа (кроме дома — он ставится принудительно).
    lines = [fl.line_to_shapely(line) for line in snap.lines]
    for item in plan.items:
        if item.feature.is_main_house:
            continue
        for geom in lines:
            assert not item.box.intersects(geom), (item.text, item.placement)

    # Текст не перевёрнут: поворот вне интервала 90°..270°.
    for item in plan.items:
        degrees = math.degrees(item.rotation) % 360.0
        assert not (90.0 + 1e-6 < degrees < 270.0 - 1e-6), (item.text, degrees)


def test_long_fence_gets_text_parallel_to_line_with_offset():
    plan = make_plan(build_scene())
    item = item_for(plan, "FENCE_WOOD_RIGHT")
    assert (item.kind, item.placement) == ("text", "along")
    assert math.degrees(item.rotation) % 180.0 == pytest.approx(90.0, abs=1e-6)
    # Отступ от линии забора = 0.8 * высоты текста, текст со стороны участка.
    assert abs(item.point[0] - 39.8) == pytest.approx(0.8, abs=1e-6)
    assert item.point[0] < 39.8
    assert 5.0 < item.point[1] < 95.0

    rear = item_for(plan, "CODE_CREW", "VINYL FENCE")
    assert (rear.kind, rear.placement) == ("text", "along")
    assert math.degrees(rear.rotation) % 180.0 == pytest.approx(0.0, abs=1e-6)
    assert rear.point[1] == pytest.approx(99.6 - 0.8, abs=1e-6)


def test_rotated_fence_text_follows_fence_angle():
    plan = make_plan(build_scene(17.0))
    item = item_for(plan, "FENCE_WOOD_RIGHT")
    assert item.placement == "along"
    assert math.degrees(item.rotation) % 180.0 == pytest.approx(107.0, abs=1e-6)
    degrees = math.degrees(item.rotation) % 360.0
    assert not (90.0 < degrees < 270.0)  # 287°, а не перевёрнутые 107°


def test_short_fence_and_narrow_steps_get_leaders():
    plan = make_plan(build_scene())
    for layer in ("FENCE_CHAIN_FRONT", "LOW_EDGE_TIMBER", "FRONT_STEPS"):
        item = item_for(plan, layer)
        assert (item.kind, item.placement) == ("leader", "leader"), layer
        assert len(item.leader_points) >= 2
        assert item.rotation == 0.0
    # Стрелка выноски стоит на самом объекте.
    steps = item_for(plan, "FRONT_STEPS")
    assert steps.text == "3 STEPS"
    tip = steps.leader_points[0]
    assert 19.5 <= tip[0] <= 24.5 and 22.0 <= tip[1] <= 26.0
    # Объекты участка подписываются на участке, а не за его границей.
    lot = recognize(build_scene()).boundary.polygon
    for layer in ("FENCE_CHAIN_FRONT", "LOW_EDGE_TIMBER", "FRONT_STEPS"):
        assert lot.contains(item_for(plan, layer).box), layer


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
        ("CODE_CREW", "FENCE_NOTE"),
        ("SUBJECT_SHED_OUTLINE", "D-TOPO-TEXT"),
        ("SIDEWALK_EDGE", "SURFACE_TEXT"),
    }
    assert len(plan.items) == 15
    # Одноимённый забор другого слоя далеко от надписи всё равно подписывается.
    assert item_for(plan, "FENCE_CHAIN_FRONT").text == "CHAIN LINK FENCE"
    assert item_for(plan, "WALK_EDGE").text == "CONCRETE WALK"


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
    """Текст стоит в стороне, но его выноска указывает на объект — дубликат не нужен."""
    snap = build_scene()
    snap.texts.append(DrawingText("ENTRANCE_TEXT", "3 STEPS", (-12, 12), height=0.9))
    snap.leaders.append(
        DrawingLine("ENTRANCE_TEXT", ((22.0, 24.0), (-5.0, 12.4), (-6.5, 12.4)), kind="AcDbLeader")
    )
    plan = make_plan(snap)
    assert [(dup.feature.layer, dup.existing_text) for dup in plan.duplicates] == [
        ("FRONT_STEPS", "3 STEPS")
    ]


def test_report_lists_counters_for_gui_log():
    plan = make_plan(build_scene(with_existing=True))
    result = ab.FeatureLabelResult(
        plan=plan, layer=plan.output_layer, created_labels=15, created_texts=2,
        created_mtexts=13, created_leaders=5,
    )
    report = result.report()
    assert "по кодам точек (POINT_LABELS): 4" in report
    assert "по именам слоёв: 13" in report
    assert "ПОДПИСАНО: 15" in report
    assert "ПРОПУЩЕНО КАК УЖЕ ПОДПИСАННЫЕ: 3" in report
    assert "AddText — 2, AddMText — 13, AddLeader — 5" in report
    assert "XYZ ×1" in report
    assert "D-TOPO-TEXT" in report
    summary = result.summary()
    assert "по кодам точек: 4" in summary and "по слоям: 13" in summary
    assert "Подписано объектов: 15" in summary
    assert (result.found_by_point_codes, result.found_by_layers, result.skipped_duplicates) == (4, 13, 3)


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
    def __init__(self, *, with_leader=True):
        self.entities = []
        self.created = []
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
        return self._created(
            FakeEntity("AcDbText", "0", TextString=text, InsertionPoint=list(point),
                       Height=height, Rotation=0.0, Alignment=None, TextAlignmentPoint=None)
        )

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
    def __init__(self, *, with_leader=True):
        self.ModelSpace = FakeModelSpace(with_leader=with_leader)
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


class FakeApp:
    def __init__(self, doc):
        self.doc = doc
        self.updated = 0

    @property
    def ActiveDocument(self):
        return self.doc

    def Update(self):
        self.updated += 1


def fake_autocad(snapshot, *, with_leader=True):
    """Переносит синтетический чертёж в заглушку ModelSpace (как объекты AutoCAD)."""
    doc = FakeDoc(with_leader=with_leader)
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
    return FakeApp(doc), doc


@pytest.fixture()
def com(monkeypatch):
    monkeypatch.setattr(ab, "_co_initialize", lambda: None)
    monkeypatch.setattr(ab, "_variant_points", lambda points: [c for p in points for c in p])
    monkeypatch.setattr(ab, "_variant_point", lambda point, z=0.0: [point[0], point[1], z])
    monkeypatch.setattr(ab, "_null_dispatch", lambda: None)

    def connect(snapshot, **kwargs):
        builder = AcadBuilder(load_config())
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
    assert (restored.by_point_codes, restored.by_layers, restored.by_user) == (4, 13, 1)


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
    assert result.by_point_codes == 4  # привязка заборов по кодам сохраняется


def test_label_features_draws_into_d_topo_text(com):
    builder, app, doc = com(build_scene())
    messages = []
    result = builder.label_features(mode="auto", house_text=HOUSE, scale=16.0, progress=messages.append)

    space = doc.ModelSpace
    assert result.layer == "D-TOPO-TEXT"
    assert result.failed == [] and result.warnings == []
    assert result.created_labels == 18
    assert (result.created_texts, result.created_mtexts, result.created_leaders) == (3, 15, 5)
    assert result.plan.text_height == pytest.approx(0.9)
    assert {entity.Layer for entity in space.created} == {"D-TOPO-TEXT"}
    assert "D-TOPO-TEXT" in doc.Layers.items
    assert doc.undo_marks == ["start", "end"]
    assert app.updated == 1
    assert any("Читаю" in message for message in messages)

    texts = [e for e in space.created if e.ObjectName == "AcDbText"]
    mtexts = [e for e in space.created if e.ObjectName == "AcDbMText"]
    leaders = [e for e in space.created if e.ObjectName == "AcDbLeader"]
    assert sorted(e.TextString for e in texts) == ["CHAIN LINK FENCE", "VINYL FENCE", "WOOD FENCE"]
    assert all(e.Alignment == ab.AC_ALIGNMENT_MIDDLE_CENTER for e in texts)
    assert all(e.Height == pytest.approx(0.9) for e in texts + mtexts)
    assert len(leaders) == 5
    assert all(e.Type == ab.AC_LEADER_LINE_WITH_ARROW for e in leaders)
    assert all(len(e.Coordinates) % 3 == 0 and len(e.Coordinates) >= 6 for e in leaders)

    house = next(e for e in mtexts if e.TextString == HOUSE)
    assert house.AttachmentPoint == ab.AC_ATTACHMENT_MIDDLE_CENTER
    assert house.InsertionPoint[:2] == pytest.approx([23.0, 46.0])
    assert house.Width == 0.0
    assert {e.TextString for e in mtexts} >= {
        "SHED", "WOOD DECK", "PORCH", "3 STEPS", "DRIVEWAY", "CONCRETE WALK", "UTILITY POLE", "MANHOLE",
    }
    # Выноска с полкой привязана к своему MText.
    steps = next(e for e in mtexts if e.TextString == "3 STEPS")
    assert any(e.Annotation is steps for e in leaders)

    report = result.report()
    assert "по кодам точек (POINT_LABELS): 4" in report
    assert "по именам слоёв: 13" in report
    assert "ПОДПИСАНО: 18" in report


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


def test_leader_falls_back_to_polyline_when_addleader_fails(com):
    builder, _app, doc = com(build_scene(), with_leader=False)
    result = builder.label_features(house_text=HOUSE)
    assert result.created_labels == 18
    assert result.created_leaders == 5
    assert result.failed == []
    assert len(result.warnings) == 1 and "полилинией" in result.warnings[0]
    arrows = [e for e in doc.ModelSpace.created if e.ObjectName == "AcDbPolyline"]
    assert len(arrows) == 5
    assert {e.Layer for e in arrows} == {"D-TOPO-TEXT"}
    assert all(e.width[1] == 0.0 and e.width[2] > 0.0 for e in arrows)  # клин-стрелка у объекта


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
