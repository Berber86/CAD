"""Черновик для печати: наш участок, его улицы и подпись пересекающей улицы — без соседей.

Правила (docs/customer_rules.md, раздел «Черновик для печати»):
  * перекрёсток улиц в AutoCAD — (5000, 5000); основной участок строится от него;
  * контур черновика строится на +1000 по X в тех же слоях PL и M-ROW-LINE;
  * линия от перекрёстка до фасада в черновике — 40 ft (физический размер; описание не меняется);
  * в черновике только контур участка, красные линии улиц и подпись пересекающей улицы.

Данные: tests/data/lot72_render_e600.txt (участок вдали от угла, линия до перекрёстка 241 ft)
и tests/data/lot51_commencing_corner.txt (угловой участок, линии до перекрёстка нет).
"""

import math
from pathlib import Path

import pytest

import acad_builder as module
import llm_parser
from acad_builder import AcadBuilder, make_print_draft, translate_plan

DATA = Path(__file__).resolve().parent / "data"
ORIGIN = (5000.0, 5000.0)


def _model(name: str):
    text = (DATA / name).read_text(encoding="utf-8")
    model, _ = llm_parser.parse_with_regex(text, auto_repair_typos=False)
    return model


def _cfg():
    return llm_parser.load_config()


def _plan(name: str):
    return AcadBuilder(_cfg()).plan(_model(name), scale=16.0)


def _tie(plan):
    return next(ln for ln in plan.lines if ln.kind == "main-street-row-tie")


# --- расчёт черновика -------------------------------------------------------


def test_draft_tie_line_is_40_ft_and_legal_description_is_unchanged():
    plan = _plan("lot72_render_e600.txt")
    assert plan.description.distance_to_corner_ft == pytest.approx(241.0)
    before = _tie(plan)
    draft = make_print_draft(plan, _cfg()["print_draft"])

    tie = next(ln for ln in draft.lines if ln.kind == "main-street-row-tie")
    assert math.dist(tie.start, tie.end) == pytest.approx(40.0, abs=1e-6)
    # Исходный план не меняется: линия по-прежнему 241 ft, описание тоже.
    assert math.dist(before.start, before.end) == pytest.approx(241.0, abs=1e-6)
    assert plan.description.distance_to_corner_ft == pytest.approx(241.0)


def test_draft_keeps_lot_shape_and_side_lengths():
    plan = _plan("lot72_render_e600.txt")
    draft = make_print_draft(plan, _cfg()["print_draft"])
    sides_plan = [math.dist(s.start, s.end) for s in plan.boundary_lines]
    sides_draft = [math.dist(s.start, s.end) for s in draft.boundary_lines]
    assert sides_draft == pytest.approx(sides_plan, abs=1e-6)
    assert math.dist(draft.vertices[0], draft.vertices[1]) == pytest.approx(
        math.dist(plan.vertices[0], plan.vertices[1]), abs=1e-6
    )


def test_draft_contains_only_lot_streets_and_cross_street_label():
    plan = _plan("lot72_render_e600.txt")
    draft = make_print_draft(plan, _cfg()["print_draft"])
    assert draft.print_draft is True
    assert {item.kind for item in draft.items} == {"cross-street"}
    assert len(draft.items) == 1
    assert "CARLTON" in draft.items[0].text
    # Ни соседей, ни подписей сторон, ни расстояния до угла в черновике нет.
    assert all(item.kind == "cross-street" for item in draft.items)
    assert not any(item.kind == "cross-street-distance" for item in draft.items)


def test_draft_lot_is_shifted_one_thousand_and_tie_is_40():
    plan = _plan("lot72_render_e600.txt")
    draft = make_print_draft(plan, _cfg()["print_draft"])
    tie = next(ln for ln in draft.lines if ln.kind == "main-street-row-tie")
    assert tie.end == pytest.approx((_tie(plan).end[0] + 1000, _tie(plan).end[1]))
    assert math.dist(tie.start, tie.end) == pytest.approx(40.0)
    assert all(new.start == pytest.approx((old.start[0] + 1000, old.start[1]))
               and new.end == pytest.approx((old.end[0] + 1000, old.end[1]))
               for old, new in zip(plan.boundary_lines, draft.boundary_lines))


def test_corner_lot_draft_has_no_tie_and_no_warning():
    plan = _plan("lot51_commencing_corner.txt")
    assert not any(ln.kind == "main-street-row-tie" for ln in plan.lines)
    draft = make_print_draft(plan, _cfg()["print_draft"])
    assert not any("линии до перекрёстка" in w for w in draft.warnings)
    assert {item.kind for item in draft.items} == {"cross-street"}


def test_translate_plan_moves_everything_and_keeps_original():
    plan = _plan("lot72_render_e600.txt")
    moved = translate_plan(plan, 5000.0, 5000.0)
    assert moved.vertices[0] == pytest.approx((plan.vertices[0][0] + 5000, plan.vertices[0][1] + 5000))
    assert moved.boundary_lines[0].start == pytest.approx(
        (plan.boundary_lines[0].start[0] + 5000, plan.boundary_lines[0].start[1] + 5000)
    )
    assert plan.vertices[0] != moved.vertices[0]


# --- отрисовка на фейковом AutoCAD -----------------------------------------


class _Entity:
    def __init__(self, **kwargs):
        self.Layer = "0"
        for key, value in kwargs.items():
            setattr(self, key, value)


class _ModelSpace:
    def __init__(self):
        self.lines: list[_Entity] = []
        self.texts: list[_Entity] = []
        self.polylines: list[_Entity] = []
        self.arcs: list[_Entity] = []

    def AddLine(self, start, end):
        item = _Entity(StartPoint=start, EndPoint=end)
        self.lines.append(item)
        return item

    def AddLightWeightPolyline(self, coordinates):
        item = _Entity(Coordinates=coordinates)
        self.polylines.append(item)
        return item

    def AddText(self, text, point, height):
        item = _Entity(TextString=text, InsertionPoint=point, Height=height)
        self.texts.append(item)
        return item

    def AddArc(self, center, radius, start, end):
        item = _Entity(Center=center)
        self.arcs.append(item)
        return item


class _Layer:
    def __init__(self, name):
        self.Name = name
        self.color = 7
        self.LayerOn = True
        self.Freeze = False
        self.Lock = False


class _Layers:
    def __init__(self):
        self._items = {"0": _Layer("0")}

    def Item(self, name):
        return self._items[name]

    def Add(self, name):
        self._items[name] = _Layer(name)
        return self._items[name]


class _TextStyles:
    def Item(self, name):
        raise KeyError(name)

    def Add(self, name):
        return _Entity(Name=name)


class _Doc:
    def __init__(self):
        self.ModelSpace = _ModelSpace()
        self.Layers = _Layers()
        self.TextStyles = _TextStyles()
        self.ActiveSpace = 1

    def GetVariable(self, name):
        return 2


class _App:
    def __init__(self, doc):
        self.doc = doc

    @property
    def ActiveDocument(self):
        return self.doc

    def ZoomExtents(self):
        return None

    def ZoomScaled(self, scale, scale_type):
        return None


@pytest.fixture
def fake_autocad(monkeypatch):
    monkeypatch.setattr(module, "_co_initialize", lambda: None)
    monkeypatch.setattr(module, "_co_uninitialize", lambda: None)
    monkeypatch.setattr(module, "_variant_points", lambda points: [c for p in points for c in p])
    monkeypatch.setattr(module, "_variant_point", lambda point, z=0.0: [point[0], point[1], z])
    monkeypatch.setattr(module, "_add_text_style_font", lambda *a, **k: None, raising=False)

    def make(plan_source):
        builder = AcadBuilder(_cfg())
        doc = _Doc()
        builder._app, builder._doc = _App(doc), doc
        return builder, doc

    return make


def test_main_lot_is_drawn_at_the_intersection_and_draft_next_to_it(fake_autocad):
    plan = _plan("lot72_render_e600.txt")
    draft = make_print_draft(plan, _cfg()["print_draft"])

    builder, doc = fake_autocad(plan)
    main_result = builder.draw_plan(plan)
    main_lines = [ln for ln in doc.ModelSpace.lines if ln.Layer == "PL"]
    assert len(main_lines) == len(plan.boundary_lines) - len(plan.street_boundary_indices)
    assert main_result.boundary_line_count == len(plan.boundary_lines)
    tie = next(ln for ln in doc.ModelSpace.lines if ln.Layer == "M-ROW-LINE")
    assert tie.StartPoint[:2] == pytest.approx(ORIGIN)
    result = builder.draw_plan(draft)
    assert result.layers["print_property_line"] == "PL"
    assert result.layers["print_street_row"] == "M-ROW-LINE"
    draft_tie = next(ln for ln in doc.ModelSpace.lines
                     if _matches_line(ln, _tie(plan).start, _tie(plan).end))
    assert math.dist(draft_tie.StartPoint, draft_tie.EndPoint) == pytest.approx(241.0)
    expected = _tie(draft)
    actual = [ln for ln in doc.ModelSpace.lines if _matches_line(ln, expected.start, expected.end)]
    assert len(actual) == 1 and math.dist(actual[0].StartPoint, actual[0].EndPoint) == pytest.approx(40.0)
    assert len(doc.ModelSpace.texts) == len(plan.items) + 1
    assert "CARLTON" in doc.ModelSpace.texts[-1].TextString


def _matches_line(entity, start, end, dx=0.0):
    return (entity.StartPoint == pytest.approx((start[0] + 5000 + dx, start[1] + 5000, 0))
            and entity.EndPoint == pytest.approx((end[0] + 5000 + dx, end[1] + 5000, 0)))


def test_exact_property_copy_and_colors_survive_legacy_config(fake_autocad):
    """В одной сцене: исходный контур и точная копия только его границы по WCS X."""
    plan = _plan("lot72_render_e600.txt")
    builder, doc = fake_autocad(plan)
    builder.cfg["layers"]["property_line"] = "ST-BLDG-LINE"
    builder.cfg["layers"]["street_row"] = "OLD-ROAD"
    builder.cfg["layer_colors"]["street_row"] = 5
    builder.cfg["drawing"]["property_as_lines"] = False
    pl = doc.Layers.Add("PL")
    pl.color, pl.Linetype = 6, "EXISTING-PL-TYPE"
    row = doc.Layers.Add("M-ROW-LINE")
    row.color, row.Linetype = 5, "EXISTING-ROAD-TYPE"
    result = builder.draw_plan(plan)
    assert result.boundary_line_count == len(plan.boundary_lines)
    builder.draw_plan(make_print_draft(plan, {}))
    assert len(doc.ModelSpace.polylines) == 0
    assert (pl.color, pl.Linetype) == (6, "EXISTING-PL-TYPE")
    assert (row.color, row.Linetype) == (5, "EXISTING-ROAD-TYPE")
    for index, edge in enumerate(plan.boundary_lines):
        is_front = index in plan.street_boundary_indices
        for dx in (0.0, 1000.0):
            matches = [entity for entity in doc.ModelSpace.lines
                       if _matches_line(entity, edge.start, edge.end, dx)]
            assert len(matches) == 1
            assert matches[0].Layer == ("M-ROW-LINE" if is_front else "PL")
            assert matches[0].Color == (1 if is_front else 3)
            assert matches[0].Linetype == "ByLayer"
    assert len(doc.ModelSpace.texts) == len(plan.items) + 1  # только название пересекающей улицы
    assert len(doc.ModelSpace.arcs) == 2 * len(plan.arcs)



def test_corner_lot_has_two_red_frontages_in_both_copies(fake_autocad):
    plan = _plan("lot51_commencing_corner.txt")
    assert len(plan.street_boundary_indices) == 2
    builder, doc = fake_autocad(plan)
    builder.draw_plan(plan)
    builder.draw_plan(make_print_draft(plan, {}))
    for index in plan.street_boundary_indices:
        edge = plan.boundary_lines[index]
        for dx in (0.0, 1000.0):
            matches = [entity for entity in doc.ModelSpace.lines
                       if _matches_line(entity, edge.start, edge.end, dx)]
            assert len(matches) == 1
            assert matches[0].Layer == "M-ROW-LINE" and matches[0].Color == 1


def test_copy_preserves_open_gap_without_forced_closure(fake_autocad):
    plan = _plan("lot72_render_e600.txt")
    # Тестируем именно геометрию без замыкания: укоротим одну сторону и сохраним разрыв.
    from dataclasses import replace
    line = plan.boundary_lines[0]
    plan.boundary_lines[0] = replace(line, end=(line.end[0] - 0.2, line.end[1]))
    plan.gap_points = (line.end, plan.boundary_lines[0].end)
    builder, doc = fake_autocad(plan)
    result = builder.draw_plan(plan)
    builder.draw_plan(make_print_draft(plan, {}))
    short = plan.boundary_lines[0]
    assert sum(_matches_line(obj, short.start, short.end, 1000) for obj in doc.ModelSpace.lines) == 1


def test_new_pl_layer_loads_reference_linetype_and_missing_type_is_visible(fake_autocad):
    plan = _plan("lot72_render_e600.txt")
    builder, doc = fake_autocad(plan)
    class Linetypes:
        def __init__(self):
            self.loaded = []
        def Item(self, name):
            if not self.loaded:
                raise KeyError(name)
            return name
        def Load(self, name, filename):
            self.loaded.append((name, filename))
    doc.Linetypes = Linetypes()
    result = builder.draw_plan(plan)
    assert doc.Linetypes.loaded == [("ACAD_ISO10W100", "acadiso.lin")]
    assert doc.Layers.Item("PL").Linetype == "ACAD_ISO10W100"
    assert not any("Не удалось задать тип линии" in w for w in result.warnings)

    builder2, doc2 = fake_autocad(plan)
    result2 = builder2.draw_plan(plan)  # Нет коллекции типов линий.
    assert any("ACAD_ISO10W100" in w and "Проверьте" in w for w in result2.warnings)


def test_step4_area_selects_original_or_copy_after_step1(fake_autocad):
    from shapely.geometry import box
    from feature_labeler import DrawingLine, DrawingSnapshot, find_property_polygon
    plan = _plan("lot72_render_e600.txt")
    builder, doc = fake_autocad(plan)
    builder.draw_plan(plan)
    builder.draw_plan(make_print_draft(plan, {}))
    lines = [DrawingLine(obj.Layer, (tuple(obj.StartPoint[:2]), tuple(obj.EndPoint[:2])))
             for obj in doc.ModelSpace.lines]
    snapshot = DrawingSnapshot(lines=lines)
    original_center = (plan.centroid[0] + 5000, plan.centroid[1] + 5000)
    original = find_property_polygon(snapshot, builder.cfg,
                                     near=box(original_center[0]-1, original_center[1]-1,
                                              original_center[0]+1, original_center[1]+1))
    copied = find_property_polygon(snapshot, builder.cfg,
                                   near=box(original_center[0]+999, original_center[1]-1,
                                            original_center[0]+1001, original_center[1]+1))
    assert original.polygon.centroid.x == pytest.approx(original_center[0], abs=0.1)
    assert copied.polygon.centroid.x == pytest.approx(original.polygon.centroid.x + 1000, abs=0.1)
    assert copied.area_sqft == pytest.approx(original.area_sqft, abs=0.1)


def test_corner_frontages_are_usable_for_step4_on_each_copy(fake_autocad):
    from shapely.geometry import box
    from feature_labeler import DrawingLine, DrawingSnapshot, find_property_polygon
    plan = _plan("lot51_commencing_corner.txt")
    builder, doc = fake_autocad(plan)
    builder.draw_plan(plan)
    builder.draw_plan(make_print_draft(plan, {}))
    snap = DrawingSnapshot(lines=[
        DrawingLine(obj.Layer, (tuple(obj.StartPoint[:2]), tuple(obj.EndPoint[:2])))
        for obj in doc.ModelSpace.lines
    ])
    x, y = plan.centroid[0] + 5000, plan.centroid[1] + 5000
    for dx in (0, 1000):
        found = find_property_polygon(snap, builder.cfg, near=box(x+dx-1, y-1, x+dx+1, y+1))
        assert found.area_sqft == pytest.approx(plan.area_sqft, rel=1e-5)


def test_legacy_draft_settings_cannot_move_copy_or_change_tie():
    plan = translate_plan(_plan("lot72_render_e600.txt"), 17, -23)
    cfg = {"enabled": False, "shift_ft": [42, 3], "tie_length_ft": 25}
    draft = make_print_draft(plan, cfg)
    for original, copy in zip(plan.boundary_lines, draft.boundary_lines):
        assert copy.start == pytest.approx((original.start[0] + 1000, original.start[1]))
        assert copy.end == pytest.approx((original.end[0] + 1000, original.end[1]))
    tie = _tie(draft)
    assert tie.end == pytest.approx((_tie(plan).end[0] + 1000, _tie(plan).end[1]))
    assert math.dist(tie.start, tie.end) == pytest.approx(40)
    for original, copy in zip(plan.lines, draft.lines):
        if original.kind.startswith("cross-street-row"):
            dx = tie.start[0] - _tie(plan).start[0]
            dy = tie.start[1] - _tie(plan).start[1]
            assert copy.start == pytest.approx((original.start[0] + dx, original.start[1] + dy))
            assert copy.end == pytest.approx((original.end[0] + dx, original.end[1] + dy))
    item = next(i for i in plan.items if i.kind == "cross-street")
    assert draft.items[0].point == pytest.approx((item.point[0] + dx, item.point[1] + dy))


def test_full_scene_has_one_draft_for_our_lot_and_no_neighbor_copy(fake_autocad):
    from llm_parser import ASPENWALL_SAMPLES, parse_with_regex
    plan = _plan("lot72_render_e600.txt")
    builder, doc = fake_autocad(plan)
    neighbor_desc, _ = parse_with_regex(ASPENWALL_SAMPLES["Aspenwall St — 365' (Сосед слева)"])
    neighbor = builder.plan(neighbor_desc, scale=16, is_neighbor=True)
    builder.draw_plan(make_print_draft(plan, {"enabled": False}))
    builder.draw_plan(plan)
    builder.draw_plan(neighbor)
    for edge in plan.boundary_lines:
        for dx in (0, 1000):
            assert sum(_matches_line(o, edge.start, edge.end, dx) for o in doc.ModelSpace.lines) == 1
    for edge in neighbor.boundary_lines:
        assert sum(o.Layer == "NEIGHBOR_LOT" and _matches_line(o, edge.start, edge.end)
                   for o in doc.ModelSpace.lines) == 1
        assert not any(o.Layer == "NEIGHBOR_LOT" and _matches_line(o, edge.start, edge.end, 1000)
                       for o in doc.ModelSpace.lines)


def test_missing_draft_tie_is_reported_in_draw_result(fake_autocad):
    plan = _plan("lot72_render_e600.txt")
    plan.lines = [line for line in plan.lines if line.kind != "main-street-row-tie"]
    draft = make_print_draft(plan, {})
    builder, _doc = fake_autocad(plan)
    result = builder.draw_plan(draft)
    assert any("привязка 40 ft не построена" in w for w in result.warnings)
    assert result.summary().count("привязка 40 ft не построена") == 1
