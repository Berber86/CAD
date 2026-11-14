"""
Тесты шага 2: автоподписание объектов на участке (feature_labeler.py).

Проверяется РЕАЛЬНЫЙ сценарий полевого DWG без AutoCAD и без сети:
  * структура слоёв входящего файла из config.json (заборы, точки, строения);
  * коды точек `'2 FEC'`, `'3 FEV64'`, `'1 TTP'` → текст подписи и привязка
    к ближайшей линии забора в радиусе 3.5 ft;
  * подписи по именам слоёв (`FENCE_VINYL*`, `SUBJECT_SHED_OUTLINE`, …);
  * подсчёт проточек `*_STEP_TREADS` → «3 STEPS»;
  * параллельная подпись длинного забора посередине с отступом 0.8·h;
  * защита от перевёрнутого текста (90°..270° → +180°);
  * выноска (Leader + MText) для узких ступенек и занятого забора;
  * дедупликация существующих надписей в радиусе 6.0 ft;
  * отсутствие наложений подписей на линии, контуры и друг на друга;
  * запись подписей строго в слой D-TOPO-TEXT и отчёт для GUI.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

shapely = pytest.importorskip("shapely")

from shapely.geometry import Point, Polygon  # noqa: E402

import feature_labeler as fl  # noqa: E402
from feature_labeler import (  # noqa: E402
    CadEntitySnapshot,
    FakeCadDrawing,
    FeatureLabeler,
    LabelingOptions,
    analyze_drawing,
    classify_layer,
    dedupe_key,
    feature_text_height,
    labels_equivalent,
    layer_matches_pattern,
    normalize_layer_name,
    parse_point_code,
    point_code_family,
    read_text_rotation,
    text_box,
    text_fits_along,
)
from llm_parser import load_config  # noqa: E402


# ---------------------------------------------------------------------------
# Вспомогательная «площадка съёмки»: участок + типовые объекты DWG.
# ---------------------------------------------------------------------------

LOT = (0.0, 0.0, 100.0, 120.0)  # 100 x 120 ft


def base_site() -> FakeCadDrawing:
    """Участок 100×120 с домом, сараем, крыльцом, ступеньками и заборами."""
    fake = FakeCadDrawing()
    x0, y0, x1, y1 = LOT
    fake.add_rect(x0, y0, x1, y1, "PROPERTY_LINE")
    # Задний забор по всей ширине участка + боковой деревянный на 60 ft
    fake.add_line((x0, y1), (x1, y1), "FENCE_VINYL")
    fake.add_line((x0, y1), (x0, y0 + 20.0), "S-FENCE")
    # Код точки '2 FEC' у задней линии забора: код должен победить имя слоя
    fake.add_text("2 FEC", (30.0, y1 - 1.2), "POINT_LABELS")
    # Код точки '3 FEV64' на отдельном слое, в 2 ft от бокового забора
    fake.add_text("3 FEV64", (x0 + 2.0, 60.0), "PNT-X")
    # Дом, сарай, крыльцо, ступеньки с проточками
    fake.add_rect(12.0, 25.0, 62.0, 62.0, "BUILDING_OUTLINE")
    fake.add_rect(66.0, 22.0, 92.0, 40.0, "SUBJECT_SHED_OUTLINE")
    fake.add_rect(20.0, 66.0, 55.0, 78.0, "SIDE_PORCH")
    fake.add_rect(64.0, 90.0, 70.0, 104.0, "FRONT_STEPS")
    for y in (90.0, 94.0, 98.0, 101.0):
        fake.add_line((64.0, y), (70.0, y), "FRONT_STEP_TREADS")
    # Дорожка и клумба
    fake.add_rect(0.0, 40.0, 12.0, 48.0, "DRIVEWAY_EDGE")
    fake.add_line((70.0, 60.0), (70.0, 88.0), "PLANTER_EDGE")
    return fake


def run_labeling(
    fake: FakeCadDrawing,
    *,
    house_label: str = "2 STY. FRK. & BRK.",
    skip_existing: bool = True,
    plan_vertices: list[tuple[float, float]] | None = None,
    cfg: dict | None = None,
) -> fl.LabelingResult:
    labeler = FeatureLabeler(cfg if cfg is not None else load_config())
    return labeler.run(
        fake,
        plan_vertices=plan_vertices,
        house_label=house_label,
        skip_existing=skip_existing,
    )


# ---------------------------------------------------------------------------
# 1. Слои входящего DWG и словари из config.json
# ---------------------------------------------------------------------------


def test_config_contains_exact_incoming_dwg_layers():
    cfg = load_config()["features"]
    assert cfg["property_line_layers"] == [
        "PROPERTY_LINE", "PL", "M-TAX-LINE", "PROPERTY_LINES_APPROX",
    ]
    assert cfg["point_label_layers"] == ["POINT_LABELS", "PNT-X", "PNTS"]
    assert cfg["fence_layers"] == ["S-FENCE", "ST-WALL", "CODE_CREW"]
    assert cfg["fence_prefixes"] == ["FENCE_", "GATE_", "LOW_EDGE_"]
    assert cfg["label_output_layer"] == "D-TOPO-TEXT"
    assert cfg["step_tread_pattern"] == "*_STEP_TREADS"
    structures = cfg["structure_layers"]
    assert structures["house"] == [
        "BUILDING_OUTLINE", "ST-BLDG-LINE", "REF_VEC_BUILDING",
    ]
    assert set(structures["shed"]) == {"SUBJECT_SHED_OUTLINE", "NEIGHBOUR_SHED_OUTLINE"}
    assert "REAR_DECK_EDGE" in structures["porch"]
    assert "NEIGHBOUR_SIDE_STAIR" in structures["steps"]
    assert "GRASS_STRIP_EDGE" in structures["walk"]
    assert cfg["point_code_radius_ft"] == pytest.approx(3.5)
    assert cfg["dedupe_radius_ft"] == pytest.approx(6.0)
    assert cfg["dedupe_text_layers"] == [
        "D-TOPO-TEXT", "FENCE_NOTE", "ENTRANCE_TEXT", "SURFACE_TEXT", "CONTEXT_TEXT",
    ]
    assert cfg["point_code_dictionary"]["FEC*"] == "CHAIN LINK FENCE"
    assert cfg["point_code_dictionary"]["FEV*"] == "VINYL FENCE"
    assert cfg["point_code_dictionary"]["HYD*"] == "HYDRANT"
    assert cfg["layer_label_mapping"]["LOW_EDGE*"] == "LOW TIMBER EDGE"
    assert cfg["layer_label_mapping"]["REAR_DECK_EDGE"] == "WOOD DECK"
    assert cfg["layer_label_mapping"]["SIDE_PLANTER*"] == "PLANTER"


def test_layer_name_normalization_and_wildcards():
    assert normalize_layer_name("S-Fence") == "S_FENCE"
    assert normalize_layer_name("m-tax-line") == "M_TAX_LINE"
    assert layer_matches_pattern("S-FENCE", "S-FENCE")
    assert layer_matches_pattern("FENCE_VINYL_2", "FENCE_VINYL*")
    assert layer_matches_pattern("LOW_EDGE", "LOW_EDGE*")
    assert layer_matches_pattern("LOW_EDGE_3", "LOW_EDGE_3")
    assert not layer_matches_pattern("FENCE_NOTE", "FENCE_CHAIN*")
    assert fl.matches_tread_layer("FRONT_STEPS_TREADS", ["*_STEP_TREADS"])
    assert fl.matches_tread_layer("REAR_STEP_TREADS", ["*_STEP_TREADS"])
    assert not fl.matches_tread_layer("FRONT_STEPS", ["*_STEP_TREADS"])
    assert fl.is_note_text_layer("FENCE_NOTE")
    assert fl.is_note_text_layer("GATE_TEXT")
    assert not fl.is_note_text_layer("FENCE_VINYL")


@pytest.mark.parametrize(
    "layer,expected",
    [
        ("FENCE_CHAIN_LINK", "CHAIN LINK FENCE"),
        ("FENCE_VINYL", "VINYL FENCE"),
        ("FENCE_WOOD_6FT", "WOOD FENCE"),
        ("LOW_EDGE_3", "LOW TIMBER EDGE"),
        ("SUBJECT_SHED_OUTLINE", "SHED"),
        ("SIDE_PORCH", "PORCH"),
        ("FRONT_LANDING", "PORCH"),
        ("REAR_DECK_EDGE", "WOOD DECK"),
        ("FRONT_STEPS", "STEPS"),
        ("REAR_STEPS", "STEPS"),
        ("ST-STEP", "STEPS"),
        ("DRIVEWAY_EDGE", "DRIVEWAY"),
        ("SIDEWALK_EDGE", "CONCRETE WALK"),
        ("WALK_EDGE", "CONCRETE WALK"),
        ("PLANTER_EDGE", "PLANTER"),
        ("SIDE_PLANTER_5", "PLANTER"),
    ],
)
def test_layer_label_mapping_from_config(layer, expected):
    _category, label = classify_layer(layer, load_config()["features"])
    assert label == expected


def test_structure_and_fence_categories():
    assert classify_layer("CODE_CREW", load_config()["features"])[0] == "fence"
    assert classify_layer("ST-WALL", load_config()["features"])[0] == "fence"
    assert classify_layer("GATE_DRIVE_1", load_config()["features"])[0] == "fence"
    assert classify_layer("BUILDING_OUTLINE", load_config()["features"])[0] == "building"
    assert classify_layer("ST-BLDG-LINE", load_config()["features"])[0] == "building"
    assert classify_layer("REF_VEC_BUILDING", load_config()["features"])[0] == "building"
    assert classify_layer("NEIGHBOUR_SHED_OUTLINE", load_config()["features"])[0] == "shed"
    assert classify_layer("NEIGHBOUR_SIDE_STAIR", load_config()["features"])[0] == "steps"
    assert classify_layer("WALK_43_SKETCH", load_config()["features"])[0] == "walk"
    assert classify_layer("GRASS_STRIP_EDGE", load_config()["features"])[0] == "walk"
    assert classify_layer("FENCE_NOTE", load_config()["features"]) == ("unknown", None)


# ---------------------------------------------------------------------------
# 2. Коды точек POINT_LABELS / PNT-X / PNTS
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2 FEC", ("2", "FEC", "")),
        ("3 FEV64", ("3", "FEV", "64")),
        ("17 TTP", ("17", "TTP", "")),
        ("FEC", (None, "FEC", "")),
        ("1 fev60", ("1", "FEV", "60")),
    ],
)
def test_parse_point_code_variants(raw, expected):
    assert parse_point_code(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["PROPERTY LINE", "1/4\" IRON PIPE", "", "SEE NOTE 3", "EXIST. 4' FENCE", "2024-05-01"],
)
def test_parse_point_code_rejects_free_text(raw):
    assert parse_point_code(raw) is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2 FEC", "CHAIN LINK FENCE"),
        ("2 FEC64", "CHAIN LINK FENCE"),
        ("3 FEV64", "VINYL FENCE"),
        ("4 FEV", "VINYL FENCE"),
        ("5 FEW", "WOOD FENCE"),
        ("6 FEI", "IRON FENCE"),
        ("17 TTP", "UTILITY POLE"),
        ("8 LP", "UTILITY POLE"),
        ("9 UP", "UTILITY POLE"),
        ("10 CB", "CATCH BASIN"),
        ("11 MH", "MANHOLE"),
        ("12 HYD", "HYDRANT"),
        ("13 XYZ", None),
    ],
)
def test_point_code_dictionary(raw, expected):
    assert fl.label_for_point_code(raw, load_config()["features"]["point_code_dictionary"]) == expected


def test_point_code_family_is_fence_for_fe_codes():
    for raw in ("2 FEC", "3 FEV64", "5 FEW", "6 FEI"):
        assert point_code_family(raw) == "fence"
    assert point_code_family("17 TTP") is None


def test_point_code_beats_layer_name_on_nearest_fence():
    """Код FEC у забора слоя FENCE_VINYL подписывает его «CHAIN LINK FENCE»."""
    fake = base_site()
    result = run_labeling(fake)
    texts = result.label_texts()
    assert "CHAIN LINK FENCE" in texts
    # Второй забор (S-FENCE) получил подпись по коду FEV64 со слоя PNT-X.
    assert "VINYL FENCE" in texts
    assert result.stats.point_codes_total >= 2
    assert result.stats.point_codes_used >= 2


def test_point_code_far_from_fence_falls_back_to_layer_name():
    """Код в 12 ft от забора не привязывается: подпись берётся из имени слоя."""
    fake = FakeCadDrawing()
    fake.add_rect(0, 0, 100, 100, "PROPERTY_LINE")
    fake.add_line((0, 100), (100, 100), "FENCE_CHAIN")
    fake.add_text("2 FEV", (50.0, 88.0), "POINT_LABELS")  # 12 ft от забора
    result = run_labeling(fake)
    assert result.label_texts() == ["CHAIN LINK FENCE"]
    assert result.stats.point_codes_used == 0
    assert result.stats.point_codes_out_of_radius == 1


def test_utility_pole_code_does_not_capture_fence():
    """Код TTP не переименовывает забор: столб — точечный объект, а не линия."""
    fake = FakeCadDrawing()
    fake.add_rect(0, 0, 100, 100, "PROPERTY_LINE")
    fake.add_line((0, 100), (100, 100), "FENCE_VINYL")
    fake.add_text("17 TTP", (50.0, 99.5), "POINT_LABELS")
    result = run_labeling(fake)
    assert result.label_texts() == ["VINYL FENCE"]
    assert result.stats.point_codes_used == 0
    assert result.stats.point_codes_non_fence == 1
    # Код распознан, но относится к точечному объекту (стойка — не забор).
    assert result.stats.point_codes_total == 1


# ---------------------------------------------------------------------------
# 3. Подписи по слоям и счёт ступенек
# ---------------------------------------------------------------------------


def test_steps_labelled_with_tread_count():
    fake = FakeCadDrawing()
    fake.add_rect(0, 0, 100, 100, "PROPERTY_LINE")
    fake.add_rect(20, 20, 30, 50, "FRONT_STEPS")
    for y in (20.0, 30.0, 40.0, 50.0):
        fake.add_line((20.0, y), (30.0, y), "FRONT_STEP_TREADS")
    result = run_labeling(fake, house_label="")
    assert result.label_texts() == ["4 STEPS"]
    assert result.stats.treads_labels == 1


def test_steps_without_treads_stay_generic():
    fake = FakeCadDrawing()
    fake.add_rect(0, 0, 100, 100, "PROPERTY_LINE")
    fake.add_rect(20, 20, 40, 45, "REAR_STEPS")
    result = run_labeling(fake, house_label="")
    assert result.label_texts() == ["STEPS"]


def test_porch_deck_shed_and_driveway_labels():
    fake = FakeCadDrawing()
    fake.add_rect(0, 0, 120, 120, "PROPERTY_LINE")
    fake.add_rect(10, 10, 60, 45, "BUILDING_OUTLINE")
    fake.add_rect(70, 12, 100, 32, "SUBJECT_SHED_OUTLINE")
    fake.add_rect(20, 55, 50, 66, "SIDE_PORCH")
    fake.add_rect(60, 55, 95, 75, "REAR_DECK_EDGE")
    fake.add_rect(0, 90, 120, 98, "DRIVEWAY_EDGE")
    result = run_labeling(fake, house_label="2 STY. FRAME")
    texts = result.label_texts()
    assert "SHED" in texts
    assert "PORCH" in texts
    assert "WOOD DECK" in texts
    assert "DRIVEWAY" in texts
    assert "2 STY. FRAME" in texts


def test_low_edge_gets_leader_not_parallel_text():
    """Низкий бордюр — узкая линия: подпись ставится выноской со стрелкой."""
    fake = FakeCadDrawing()
    fake.add_rect(0, 0, 120, 120, "PROPERTY_LINE")
    fake.add_line((10.0, 60.0), (110.0, 60.0), "LOW_EDGE_2")
    result = run_labeling(fake, house_label="")
    assert result.label_texts() == ["LOW TIMBER EDGE"]
    leader_plans = [p for p in result.plans if p.mode == "leader"]
    assert leader_plans and leader_plans[0].leader_points
    assert fake.leaders, "для бордюра должна быть создана выноска AddLeader"


def test_house_without_gui_description_is_not_labelled():
    fake = FakeCadDrawing()
    fake.add_rect(0, 0, 120, 120, "PROPERTY_LINE")
    fake.add_rect(10, 10, 60, 45, "BUILDING_OUTLINE")
    result = run_labeling(fake, house_label="")
    assert result.label_texts() == []
    assert result.stats.house_labeled == 0
    assert result.stats.features_in_buffer == 1


def test_house_label_is_mtext_inside_outline():
    fake = base_site()
    result = run_labeling(fake, house_label="2 STY. BRK. & FR.")
    house_plan = next(p for p in result.plans if p.is_house)
    assert house_plan.text == "2 STY. BRK. & FR."
    assert house_plan.mode == "inside"
    outline = Polygon([(12, 25), (62, 25), (62, 62), (12, 62)])
    assert outline.contains(Point(house_plan.point))
    assert any(item["text"] == "2 STY. BRK. & FR." for item in fake.mtexts)


# ---------------------------------------------------------------------------
# 4. Параллельные подписи заборов (linemerge, отступ 0.8·h, поворот)
# ---------------------------------------------------------------------------


def test_merged_fence_segments_get_single_parallel_label():
    """12 смежных сегментов одного забора → ОДНА подпись посередине."""
    fake = FakeCadDrawing()
    fake.add_rect(0, 0, 120, 120, "PROPERTY_LINE")
    for idx in range(12):
        fake.add_line((idx * 10.0, 119.5), ((idx + 1) * 10.0, 119.5), "FENCE_WOOD")
    result = run_labeling(fake, house_label="")
    assert result.label_texts() == ["WOOD FENCE"]
    plan = result.plans[0]
    assert plan.mode == "parallel"
    assert abs(plan.point[0] - 60.0) < 1e-6, "подпись должна быть посередине забора"
    assert result.stats.parallel == 1


def test_parallel_label_offset_is_point_eight_text_height():
    fake = FakeCadDrawing()
    fake.add_rect(0, 0, 120, 120, "PROPERTY_LINE")
    fake.add_line((10.0, 100.0), (110.0, 100.0), "FENCE_CHAIN")
    labeler = FeatureLabeler(load_config())
    result = labeler.run(fake, house_label="", skip_existing=True)
    plan = result.plans[0]
    height = float(labeler.cfg["text_height"])
    box = text_box(plan.text, plan.point, height, rotation=plan.rotation)
    half_h = float(box.bounds[3] - box.bounds[1]) / 2.0
    # Текст отступает от линии забора на 0.8 высоты СИМВОЛА (требование шага 2),
    # поэтому центр текстового блока = 0.8·h + половина высоты блока.
    gap_to_text_box = abs(plan.point[1] - 100.0) - half_h
    assert gap_to_text_box == pytest.approx(0.8 * height, abs=1e-6)


@pytest.mark.parametrize("angle_deg", [0.0, 90.0, 180.0, 270.0, 135.0, 200.0])
def test_parallel_text_rotation_never_upside_down(angle_deg):
    """Поворот подписи всегда в -90°..+90°: текст не переворачивается."""
    fake = FakeCadDrawing()
    fake.add_rect(-40, -40, 160, 160, "PROPERTY_LINE")
    angle = math.radians(angle_deg)
    cx, cy = 60.0, 60.0
    half = 40.0
    dx, dy = math.cos(angle) * half, math.sin(angle) * half
    fake.add_line((cx - dx, cy - dy), (cx + dx, cy + dy), "FENCE_VINYL")
    result = run_labeling(fake, house_label="")
    plan = result.plans[0]
    assert plan.mode == "parallel"
    assert abs(math.degrees(plan.rotation)) <= 90.0 + 1e-6
    # Текст читается слева направо: cos(rotation) >= 0.
    assert math.cos(plan.rotation) >= -1e-9
    assert read_text_rotation(plan.rotation) == pytest.approx(plan.rotation)


def test_read_text_rotation_maps_upside_down_angles():
    for deg in (91.0, 120.0, 179.0, 180.0, 200.0, 269.0):
        angle = read_text_rotation(math.radians(deg))
        assert -math.pi / 2.0 - 1e-9 <= angle <= math.pi / 2.0 + 1e-9


def test_short_fence_uses_leader():
    """Забор короче ширины текста + 4 ft подписывается выноской."""
    fake = FakeCadDrawing()
    fake.add_rect(0, 0, 120, 120, "PROPERTY_LINE")
    fake.add_line((40.0, 100.0), (44.0, 100.0), "FENCE_CHAIN")  # 4 ft
    result = run_labeling(fake, house_label="")
    assert result.label_texts() == ["CHAIN LINK FENCE"]
    plan = result.plans[0]
    assert plan.mode == "leader"
    assert plan.leader_points and plan.leader_text_point is not None
    assert fake.leaders and len(fake.leaders[0]["points"]) >= 2


def test_text_fits_along_rule():
    assert text_fits_along(20.0, 10.0, 4.0)
    assert not text_fits_along(13.0, 10.0, 4.0)


def test_curved_fence_from_bulge_gets_leader():
    """Изогнутый забор (bulge) не подписывается вдоль хорды — только выноска."""
    fake = FakeCadDrawing()
    fake.add_rect(-50, -50, 200, 200, "PROPERTY_LINE")
    fake.add_polyline(
        [(10.0, 100.0), (110.0, 100.0)], "FENCE_ARC_VINYL", closed=False, bulges=[0.35]
    )
    result = run_labeling(fake, house_label="")
    plan = result.plans[0]
    assert plan.text == "VINYL FENCE"
    assert plan.mode == "leader"
    assert fake.leaders, "изогнутый забор подписывается выноской со стрелкой"


# ---------------------------------------------------------------------------
# 5. Дедупликация существующих подписей
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("layer", ["D-TOPO-TEXT", "FENCE_NOTE", "SURFACE_TEXT", "CONTEXT_TEXT", "ENTRANCE_TEXT"])
def test_existing_label_in_dedupe_layers_blocks_duplicate(layer):
    fake = FakeCadDrawing()
    fake.add_rect(0, 0, 100, 100, "PROPERTY_LINE")
    fake.add_line((10.0, 99.0), (90.0, 99.0), "FENCE_VINYL")
    fake.add_text("VINYL FENCE", (50.0, 96.0), layer)
    result = run_labeling(fake, house_label="")
    assert result.label_texts() == []
    assert result.stats.skipped_existing == 1


def test_existing_label_far_away_does_not_block():
    fake = FakeCadDrawing()
    fake.add_rect(0, 0, 100, 100, "PROPERTY_LINE")
    fake.add_line((10.0, 99.0), (90.0, 99.0), "FENCE_VINYL")
    fake.add_text("VINYL FENCE", (5.0, 20.0), "D-TOPO-TEXT")  # 79 ft от забора
    result = run_labeling(fake, house_label="")
    assert result.label_texts() == ["VINYL FENCE"]


def test_existing_label_rotation_must_match():
    """Надпись, повёрнутая на 90°, подписывает ДРУГУЮ стену, а не этот забор."""
    fake = FakeCadDrawing()
    fake.add_rect(0, 0, 100, 100, "PROPERTY_LINE")
    fake.add_line((10.0, 99.0), (90.0, 99.0), "FENCE_VINYL")
    fake.add_text("VINYL FENCE", (50.0, 97.0), "D-TOPO-TEXT", rotation=math.pi / 2.0)
    result = run_labeling(fake, house_label="")
    assert result.label_texts() == ["VINYL FENCE"]


def test_skip_existing_can_be_disabled_from_gui():
    fake = FakeCadDrawing()
    fake.add_rect(0, 0, 100, 100, "PROPERTY_LINE")
    fake.add_line((10.0, 99.0), (90.0, 99.0), "FENCE_VINYL")
    fake.add_text("VINYL FENCE", (50.0, 97.0), "D-TOPO-TEXT")
    result = run_labeling(fake, house_label="", skip_existing=False)
    assert result.label_texts() == ["VINYL FENCE"]


def test_existing_house_text_prevents_house_duplicate():
    fake = FakeCadDrawing()
    fake.add_rect(0, 0, 120, 120, "PROPERTY_LINE")
    fake.add_rect(10, 10, 60, 45, "BUILDING_OUTLINE")
    fake.add_text("2 STY. FRAME", (35.0, 28.0), "D-TOPO-TEXT")
    result = run_labeling(fake, house_label="2 STY. FRAME")
    assert result.label_texts() == []
    assert result.stats.skipped_existing == 1


def test_duplicate_labels_created_twice_are_deduplicated():
    """Два одинаковых сарая рядом: вторая подпись не дублируется в радиусе 6 ft."""
    fake = FakeCadDrawing()
    fake.add_rect(0, 0, 120, 120, "PROPERTY_LINE")
    fake.add_rect(10, 10, 30, 28, "SUBJECT_SHED_OUTLINE")
    fake.add_rect(33, 10, 52, 28, "NEIGHBOUR_SHED_OUTLINE")
    result = run_labeling(fake, house_label="")
    assert result.label_texts() == ["SHED"]
    assert result.stats.skipped_existing == 1


def test_labels_equivalence_rules():
    assert labels_equivalent("VINYL FENCE", "vinyl fence (6' HIGH)")
    assert labels_equivalent("WOOD DECK", "WOOD DECK")
    assert not labels_equivalent("3 STEPS", "2 STEPS")
    assert not labels_equivalent("WOOD FENCE", "CHAIN LINK FENCE")
    assert dedupe_key("CHAIN LINK  FENCE") == "CHAIN LINK FENCE"
    assert dedupe_key("Vinyl-Fence") == "VINYL FENCE"


def test_custom_dedupe_radius_from_config():
    fake = FakeCadDrawing()
    fake.add_rect(0, 0, 100, 100, "PROPERTY_LINE")
    fake.add_line((10.0, 99.0), (90.0, 99.0), "FENCE_VINYL")
    fake.add_text("VINYL FENCE", (50.0, 94.0), "D-TOPO-TEXT")  # 5 ft
    cfg = load_config()
    cfg["features"]["dedupe_radius_ft"] = 2.0
    result = run_labeling(fake, house_label="", cfg=cfg)
    assert result.label_texts() == ["VINYL FENCE"]


# ---------------------------------------------------------------------------
# 6. Наложения подписей: не перекрывать линии, контуры и другие тексты
# ---------------------------------------------------------------------------


def test_no_created_label_overlaps_another_label():
    fake = base_site()
    result = run_labeling(fake)
    boxes = []
    for plan in result.plans:
        boxes.append(
            text_box(
                plan.text,
                plan.point,
                plan.height,
                rotation=plan.rotation,
            )
        )
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            assert not boxes[i].intersects(boxes[j]), "подписи не должны перекрываться"


def test_parallel_label_does_not_cross_building_outline():
    fake = FakeCadDrawing()
    fake.add_rect(0, 0, 120, 120, "PROPERTY_LINE")
    fake.add_rect(10, 10, 60, 45, "BUILDING_OUTLINE")
    # Забор вплотную к дому: текст не должен попасть на контур дома
    fake.add_line((0.0, 46.0), (120.0, 46.0), "FENCE_VINYL")
    result = run_labeling(fake, house_label="2 STY. FRAME")
    plan = next(p for p in result.plans if "FENCE" in p.text)
    box = text_box(
        plan.text,
        plan.point,
        plan.height,
        rotation=plan.rotation,
    )
    building = Polygon([(10, 10), (60, 10), (60, 45), (10, 45)])
    assert not box.intersects(building)


def test_no_room_reported_inside_crowded_site():
    """Расчёт не падает и честно сообщает, если места под текст нет."""
    fake = FakeCadDrawing()
    fake.add_rect(0, 0, 30, 30, "PROPERTY_LINE")
    fake.add_rect(1, 1, 29, 29, "BUILDING_OUTLINE")
    result = run_labeling(fake, house_label="2 STY. BRK. & FR.")
    assert result.stats.features_in_buffer == 1


def test_labels_stay_near_lot_not_far_away():
    fake = base_site()
    result = run_labeling(fake)
    lot = Polygon([(0, 0), (100, 0), (100, 120), (0, 120)])
    buffer_zone = lot.buffer(60.0)
    for plan in result.plans:
        assert buffer_zone.contains(Point(plan.point)), plan.text


# ---------------------------------------------------------------------------
# 7. Запись в слой D-TOPO-TEXT и отчёт
# ---------------------------------------------------------------------------


def test_all_labels_written_to_output_layer():
    fake = base_site()
    result = run_labeling(fake)
    assert result.written == len(result.plans) > 0
    assert set(fake.labels_on("D-TOPO-TEXT")) == set(result.label_texts())
    assert all(item["layer"] == "D-TOPO-TEXT" for item in fake.mtexts)
    assert "D-TOPO-TEXT" in fake.layers


def test_parallel_fence_labels_written_as_dtext():
    fake = FakeCadDrawing()
    fake.add_rect(0, 0, 100, 100, "PROPERTY_LINE")
    fake.add_line((10.0, 99.0), (90.0, 99.0), "FENCE_VINYL")
    run_labeling(fake, house_label="")
    assert [item["text"] for item in fake.dtexts] == ["VINYL FENCE"]
    assert fake.dtexts[0]["rotation"] == pytest.approx(0.0)


def test_dry_run_does_not_create_objects():
    fake = base_site()
    result = run_labeling(fake, cfg=None) if False else analyze_drawing(
        fake.snapshot_entities(), load_config(), house_label="2 STY. FRAME"
    )
    assert result.plans
    assert result.written == 0
    assert result.stats.dry_run is True
    assert fake.mtexts == []


def test_report_contains_required_counters():
    fake = base_site()
    result = run_labeling(fake)
    report = result.summary()
    assert "АВТОПОДПИСАНИЕ ОБЪЕКТОВ (D-TOPO-TEXT)" in report
    assert "по кодам POINT_LABELS" in report
    assert "по слоям" in report
    assert "Подписано объектов" in report
    assert "Пропущено как уже подписанные" in report
    assert result.stats.features_in_buffer > 0
    assert result.stats.point_codes_used >= 1
    assert result.stats.point_codes_total == 2


def test_report_counts_layer_sourced_labels():
    fake = FakeCadDrawing()
    fake.add_rect(0, 0, 120, 120, "PROPERTY_LINE")
    fake.add_rect(70, 12, 100, 32, "SUBJECT_SHED_OUTLINE")
    fake.add_line((10.0, 99.0), (90.0, 99.0), "FENCE_VINYL")
    fake.add_text("2 FEC", (50.0, 98.0), "POINT_LABELS")
    result = run_labeling(fake, house_label="")
    assert result.stats.point_codes_used == 1
    assert result.stats.features_in_buffer == 2
    assert "по слоям — 1" in result.summary()


def test_dry_run_summary_marks_no_write():
    fake = base_site()
    result = analyze_drawing(fake.snapshot_entities(), load_config(), house_label="2 STY. FRAME")
    assert "ПРОБНЫЙ ПРОГОН" in result.summary()


# ---------------------------------------------------------------------------
# 8. Контур участка: из шага 1, из слоёв и указанный пользователем
# ---------------------------------------------------------------------------


def test_site_polygon_from_property_line_layer():
    fake = base_site()
    labeler = FeatureLabeler(load_config())
    poly, source = labeler.site_polygon(fake.snapshot_entities())
    assert poly is not None
    assert source == "слой PROPERTY_LINE"
    assert poly.area == pytest.approx(100.0 * 120.0)


def test_site_polygon_from_step_one_plan():
    labeler = FeatureLabeler(load_config())
    poly, source = labeler.site_polygon(
        [], plan_vertices=[(0, 0), (50, 0), (50, 40), (0, 40)]
    )
    assert source == "шаг 1 (граница участка)"
    assert poly.area == pytest.approx(2000.0)


def test_site_polygon_from_user_picked_polyline_wins():
    labeler = FeatureLabeler(load_config())
    poly, source = labeler.site_polygon(
        [],
        plan_vertices=[(0, 0), (500, 0), (500, 500), (0, 500)],
        picked_vertices=[(0, 0), (60, 0), (60, 80), (0, 80)],
    )
    assert source == "указанная полилиния"
    assert poly.area == pytest.approx(4800.0)


def test_missing_site_polygon_raises_clear_error():
    labeler = FeatureLabeler(load_config())
    with pytest.raises(fl.FeatureLabelerError, match="Не найден контур участка"):
        labeler.run(FakeCadDrawing(), house_label="")


def test_approximate_property_layer_supported():
    fake = FakeCadDrawing()
    fake.add_rect(0, 0, 80, 90, "PROPERTY_LINES_APPROX")
    fake.add_rect(10, 10, 40, 40, "SUBJECT_SHED_OUTLINE")
    result = run_labeling(fake, house_label="")
    assert "SHED" in result.label_texts()


def test_search_buffer_limits_features():
    fake = FakeCadDrawing()
    fake.add_rect(0, 0, 100, 100, "PROPERTY_LINE")
    fake.add_rect(300.0, 300.0, 340.0, 340.0, "SUBJECT_SHED_OUTLINE")
    result = run_labeling(fake, house_label="")
    assert result.stats.features_in_buffer == 0
    assert result.label_texts() == []


def test_inside_lot_counter():
    fake = base_site()
    result = run_labeling(fake)
    assert result.stats.inside_lot >= 5


# ---------------------------------------------------------------------------
# 9. Устойчивость чтения геометрии полевого DWG
# ---------------------------------------------------------------------------


def test_closed_polyline_is_polygonized():
    fake = FakeCadDrawing()
    fake.add_polyline([(0, 0), (100, 0), (100, 100), (0, 100)], "PROPERTY_LINE")
    fake.add_polyline([(10, 10), (50, 10), (50, 40), (10, 40)], "SUBJECT_SHED_OUTLINE")
    result = run_labeling(fake, house_label="")
    plan = result.plans[0]
    assert plan.mode == "inside"
    assert "SHED" == plan.text


def test_separate_lines_closed_by_polygonize_become_outline():
    """Контур, собранный из отдельных LINE (как в полевом DWG), тоже замыкается."""
    fake = FakeCadDrawing()
    for start, end in (
        ((0, 0), (100, 0)), ((100, 0), (100, 100)),
        ((100, 100), (0, 100)), ((0, 100), (0, 0)),
    ):
        fake.add_line(start, end, "PROPERTY_LINE")
    for start, end in (
        ((10, 10), (60, 10)), ((60, 10), (60, 40)),
        ((60, 40), (10, 40)), ((10, 40), (10, 10)),
    ):
        fake.add_line(start, end, "SIDE_PORCH")
    result = run_labeling(fake, house_label="")
    assert result.label_texts() == ["PORCH"]
    assert result.plans[0].mode == "inside"


def test_text_entities_are_not_geometry():
    fake = base_site()
    result = run_labeling(fake)
    assert result.stats.existing_texts == 0  # в слоях дедупликации текстов нет
    assert all("POINT_LABELS" not in p.feature_layer for p in result.plans)


def test_note_layers_are_ignored_as_geometry():
    fake = FakeCadDrawing()
    fake.add_rect(0, 0, 100, 100, "PROPERTY_LINE")
    fake.add_line((10.0, 50.0), (90.0, 50.0), "FENCE_NOTE")
    result = run_labeling(fake, house_label="")
    assert result.label_texts() == []


def test_empty_and_degenerate_entities_are_skipped():
    fake = FakeCadDrawing()
    fake.add_rect(0, 0, 100, 100, "PROPERTY_LINE")
    fake.add_line((10.0, 10.0), (10.0, 10.0), "FENCE_VINYL")  # нулевая длина
    result = run_labeling(fake, house_label="")
    assert result.label_texts() == []


def test_text_box_geometry():
    box = text_box("CHAIN LINK FENCE", (0.0, 0.0), 0.2)
    assert box.bounds[2] > box.bounds[0]
    assert box.bounds[3] > box.bounds[1]
    rotated = text_box("CHAIN LINK FENCE", (0.0, 0.0), 0.2, rotation=math.pi / 2.0)
    assert rotated.bounds[3] - rotated.bounds[1] > box.bounds[3] - box.bounds[1]


def test_feature_text_height_from_scale():
    cfg = load_config()["features"]
    # Явная высота из config.json главнее пересчёта по масштабу.
    assert feature_text_height(cfg, 16.0) == pytest.approx(0.16)
    cfg.pop("text_height", None)
    cfg["text_height_by_scale"] = {"factor": 0.012}
    assert feature_text_height(cfg, 16.0) == pytest.approx(0.192)
    cfg["text_height"] = 0.25
    assert feature_text_height(cfg) == pytest.approx(0.25)


def test_mtext_formatting_stripped():
    assert fl.strip_mtext_formatting(r"{\fArial|b0;VINYL FENCE}") == "VINYL FENCE"
    assert fl.strip_mtext_formatting("%%d2%%d30'12\"") == "°2°30'12\""
    assert fl.strip_mtext_formatting(r"\P2 STY. FRAME") == "2 STY. FRAME"


def test_full_field_workflow_report_and_writes():
    """Сквозной сценарий: участок, коды точек, заборы, строения, ступеньки."""
    fake = base_site()
    result = run_labeling(fake)
    texts = result.label_texts()
    assert "2 STY. FRK. & BRK." in texts
    assert "SHED" in texts
    assert "PORCH" in texts
    assert any(text.endswith(" STEPS") for text in texts)
    assert any("FENCE" in text for text in texts)
    assert "DRIVEWAY" in texts
    assert result.written == len(result.plans)
    assert result.stats.created == len(result.plans)
    assert result.stats.point_codes_used >= 1
    assert not result.warnings
    assert result.stats.layers_written == ["D-TOPO-TEXT"]
    # Отчёт копируется в журнал GUI построчно — не содержит пустых разделов.
    assert result.summary().count("Подписано объектов") == 1
