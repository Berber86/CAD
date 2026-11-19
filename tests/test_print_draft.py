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


def test_draft_contains_our_labels_and_original_legal_tie():
    plan = _plan("lot72_render_e600.txt")
    draft = make_print_draft(plan, _cfg()["print_draft"])
    assert draft.print_draft and not draft.is_neighbor
    assert any(i.kind.startswith("side-") for i in draft.items)
    assert any(i.kind == "main-street" for i in draft.items)
    assert any(i.kind == "cross-street" and "CARLTON" in i.text for i in draft.items)
    assert next(i for i in draft.items if i.kind == "cross-street-distance").text == "241.00'"
    assert not any(i.kind.startswith("neighbor") for i in draft.items)


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
    assert {"cross-street", "main-street"} <= {item.kind for item in draft.items}


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


class _TextStyle:
    def __init__(self, name):
        self.Name = name
        self.Height = 0.0
        self.fontFile = ""

    def SetFont(self, *args):
        self.font = args


class _TextStyles:
    def __init__(self):
        self._items = {}

    def Item(self, name):
        return self._items[name]

    def Add(self, name):
        style = _TextStyle(name)
        self._items[name] = style
        return style


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
    assert len(doc.ModelSpace.texts) == len(plan.items) + len(draft.items)
    assert any("CARLTON" in item.TextString for item in doc.ModelSpace.texts)


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
    draft = make_print_draft(plan, {})
    builder.draw_plan(draft)
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
    assert len(doc.ModelSpace.texts) == len(plan.items) + len(draft.items)
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
    cross = next(line for line in draft.lines if line.kind == "cross-street-row")
    assert cross.start == pytest.approx(tie.start)
    assert math.dist(cross.start, cross.end) == pytest.approx(100)
    a = (tie.end[0] - tie.start[0], tie.end[1] - tie.start[1])
    b = (cross.end[0] - cross.start[0], cross.end[1] - cross.start[1])
    assert a[0] * b[0] + a[1] * b[1] == pytest.approx(0, abs=1e-6)


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


def test_missing_tie_is_restored_when_old_config_disabled_extensions(fake_autocad):
    import copy
    cfg = copy.deepcopy(_cfg())
    cfg["drawing"]["draw_street_extensions"] = False
    plan = AcadBuilder(cfg).plan(_model("lot72_render_e600.txt"), scale=16)
    assert not plan.lines
    plan = translate_plan(plan, 17, -23)
    draft = make_print_draft(plan, {}, builder_cfg=cfg)
    assert math.dist(_tie(draft).start, _tie(draft).end) == pytest.approx(40)
    cross = next(line for line in draft.lines if line.kind == "cross-street-row")
    assert math.dist(cross.start, cross.end) == pytest.approx(100)
    assert not plan.lines and cfg["drawing"]["draw_street_extensions"] is False


def test_missing_tie_is_reported_if_no_cross_street(fake_autocad):
    plan = _plan("lot72_render_e600.txt")
    plan.lines = [line for line in plan.lines if line.kind != "main-street-row-tie"]
    plan.description = plan.description.model_copy(update={"cross_street": None})
    draft = make_print_draft(plan, {})
    builder, _doc = fake_autocad(plan)
    result = builder.draw_plan(draft)
    assert any("привязка 40 ft не построена" in w for w in result.warnings)
    assert result.summary().count("привязка 40 ft не построена") == 1


def test_100_line_forms_l_not_cross_and_original_geometry_is_untouched():
    from shapely.geometry import LineString, Point
    plan = _plan("lot72_render_e600.txt")
    original_lines = [(line.kind, line.start, line.end) for line in plan.lines]
    draft = make_print_draft(plan, {})
    tie = _tie(draft)
    cross = next(line for line in draft.lines if line.kind == "cross-street-row")
    assert cross.start == pytest.approx(tie.start)
    assert math.dist(cross.start, cross.end) == pytest.approx(100)
    u = (tie.end[0] - tie.start[0], tie.end[1] - tie.start[1])
    v = (cross.end[0] - cross.start[0], cross.end[1] - cross.start[1])
    assert u[0] * v[0] + u[1] * v[1] == pytest.approx(0, abs=1e-7)
    assert LineString([tie.start, tie.end]).intersection(LineString([cross.start, cross.end])).distance(Point(tie.start)) < 1e-8
    cx, cy = draft.centroid
    assert (cx - tie.start[0]) * v[0] + (cy - tie.start[1]) * v[1] > 0
    assert [(line.kind, line.start, line.end) for line in plan.lines] == original_lines
    distance = next(item for item in draft.items if item.kind == "cross-street-distance")
    assert distance.text == "241.00'" and distance.height == 1.6
    mid = ((tie.start[0] + tie.end[0]) / 2, (tie.start[1] + tie.end[1]) / 2)
    assert math.dist(mid, distance.point) == pytest.approx(3.8)


def test_corner_draft_does_not_invent_tie_or_modify_street_lines():
    plan = _plan("lot51_commencing_corner.txt")
    draft = make_print_draft(plan, {})
    assert not any(line.kind == "main-street-row-tie" for line in draft.lines)
    assert len(plan.street_boundary_indices) == 2
    assert len(plan.lines) == len(draft.lines)
    for before, after in zip(plan.lines, draft.lines):
        assert after.kind == before.kind
        assert after.start == pytest.approx((before.start[0] + 1000, before.start[1]))
        assert after.end == pytest.approx((before.end[0] + 1000, before.end[1]))


def test_party_wall_in_legal_is_separate_and_only_on_correct_sides():
    text = llm_parser.NYC_SAMPLES["147 Dartmouth Loop (Staten Island)"]
    model, warnings = llm_parser.parse_with_regex(text)
    assert [s.party_wall for s in model.segments] == [False, True, False, True]
    plan = AcadBuilder(_cfg()).plan(model, scale=16)
    draft = make_print_draft(plan, {})
    items = [i for i in draft.items if i.kind.startswith("party-wall-")]
    assert [i.kind for i in items] == ["party-wall-2", "party-wall-4"]
    assert all(i.text == "PARTY WALL" and i.height == 1.6 for i in items)
    assert not any("PARTY WALL" in item.text for item in plan.items)
    assert not any("PARTY WALL" in warning for warning in warnings)
    before_bearing = llm_parser._flip_segment_direction(model.segments[1])
    assert before_bearing.party_wall
    assert llm_parser._mutate_segment_bearing(model.segments[1]).party_wall


def test_ambiguous_party_wall_is_not_guessed():
    sample = llm_parser.ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"]
    # Несколько курсов внутри одного THENCE: невозможно однозначно отнести отметку.
    sample = sample.replace("THENCE N 75°20'20\" E", "N 75°20'20\" E")
    sample = sample.replace("100.00 feet;\n", "100.00 feet through a party wall;\n", 1)
    model, warnings = llm_parser.parse_with_regex(sample)
    assert not any(s.party_wall for s in model.segments)
    assert any("PARTY WALL" in warning for warning in warnings)


def test_print_draft_fonts_heights_and_existing_styles_are_not_modified(fake_autocad):
    plan = _plan("lot72_render_e600.txt")
    draft = make_print_draft(plan, {})
    builder, doc = fake_autocad(plan)
    styles = doc.TextStyles
    street_style = styles.Add("MtXpl_Times_New_Roma")
    street_style.fontFile, street_style.Height = "customer.ttf", 0
    roman_style = styles.Add("Romans")
    roman_style.fontFile, roman_style.Height = "customer.shx", 0
    builder.draw_plan(draft)
    assert (street_style.fontFile, street_style.Height) == ("customer.ttf", 0)
    assert (roman_style.fontFile, roman_style.Height) == ("customer.shx", 0)
    assert len(doc.ModelSpace.texts) == len(draft.items)
    for spec, entity in zip(draft.items, doc.ModelSpace.texts):
        assert entity.Height == spec.height
        if spec.kind in {"main-street", "cross-street"}:
            assert entity.StyleName == "MtXpl_Times_New_Roma" and entity.Height == 4
        else:
            assert entity.StyleName == "Romans"
            if spec.kind.startswith("side-"):
                assert entity.Height == 2
            else:
                assert entity.Height == 1.6


def test_new_styles_and_missing_style_error_are_reported(fake_autocad):
    plan = _plan("lot72_render_e600.txt")
    draft = make_print_draft(plan, {})
    builder, doc = fake_autocad(plan)
    builder.draw_plan(draft)
    assert doc.TextStyles.Item("Romans").fontFile == "romans.shx"
    assert doc.TextStyles.Item("MtXpl_Times_New_Roma").font[0] == "Times New Roman"
    builder2, doc2 = fake_autocad(plan)
    class BrokenStyles:
        def Item(self, name): raise KeyError(name)
        def Add(self, name): raise RuntimeError("style unavailable")
    doc2.TextStyles = BrokenStyles()
    result = builder2.draw_plan(draft)
    assert any("MtXpl_Times_New_Roma" in w for w in result.warnings)
    assert any("Romans" in w for w in result.warnings)


def test_ai_party_wall_is_restored_from_matching_local_parse(monkeypatch):
    text = llm_parser.NYC_SAMPLES["147 Dartmouth Loop (Staten Island)"]
    local, _ = llm_parser.parse_with_regex(text)
    ai = local.model_copy(update={"segments": [s.model_copy(update={"party_wall": False}) for s in local.segments]})
    usage = llm_parser.TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2, provider="test", model="test")
    monkeypatch.setattr(llm_parser, "_extract_with_llm", lambda *args, **kwargs: (ai, "structured-outputs", usage))
    result = llm_parser.parse_legal_description_full(text=text, cfg=_cfg(), use_llm=True, ask_on_doubt=False)
    assert [s.party_wall for s in result.data.segments] == [False, True, False, True]


def test_missing_tie_distance_and_street_name_styles_do_not_change_main(fake_autocad):
    plan = _plan("lot72_render_e600.txt")
    original_items = [(i.text, i.kind, i.height) for i in plan.items]
    draft = make_print_draft(plan, {})
    assert [(i.text, i.kind, i.height) for i in plan.items] == original_items
    builder, doc = fake_autocad(plan)
    result = builder.draw_plan(plan)
    assert all(t.StyleName == "BOUNDARY" for t in doc.ModelSpace.texts)
    assert "MtXpl_Times_New_Roma" not in doc.TextStyles._items
    assert len(doc.ModelSpace.texts) == result.text_count
    assert any(i.kind == "cross-street-distance" and i.text == "241.00'" for i in draft.items)


def test_relative_course_notes_are_other_text_not_bearings():
    text = llm_parser.NYC_SAMPLES["63-01 Borden Ave (Queens — без румбов)"]
    model, _ = llm_parser.parse_with_regex(text)
    draft = make_print_draft(AcadBuilder(_cfg()).plan(model, scale=16), {})
    notes = [i for i in draft.items if i.kind.startswith("side-") and "-note" in i.kind]
    dimensions = [i for i in draft.items if i.kind.startswith("side-") and "-note" not in i.kind]
    assert notes and dimensions
    assert all(i.height == 1.6 for i in notes)
    assert all(i.height == 2.0 for i in dimensions)


def test_unassignable_print_style_is_not_silently_reported_as_success(fake_autocad):
    class RejectStyle(_Entity):
        @property
        def StyleName(self): return "Standard"
        @StyleName.setter
        def StyleName(self, value): raise RuntimeError("cannot set style")
        def Delete(self): self.deleted = True
    plan = _plan("lot72_render_e600.txt")
    draft = make_print_draft(plan, {})
    builder, doc = fake_autocad(plan)
    made = []
    def add_text(text, point, height):
        item = RejectStyle(TextString=text, InsertionPoint=point, Height=height)
        made.append(item)
        return item
    doc.ModelSpace.AddText = add_text
    result = builder.draw_plan(draft)
    assert result.text_count == 0
    assert made and all(item.deleted for item in made)
    assert any("cannot set style" in warning for warning in result.warnings)
