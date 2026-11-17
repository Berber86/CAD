"""Черновик для печати: наш участок, его улицы и подпись пересекающей улицы — без соседей.

Правила (docs/customer_rules.md, раздел «Черновик для печати»):
  * перекрёсток улиц в AutoCAD — (5000, 5000); основной участок строится от него;
  * черновик строится на +1000 по X и лежит на отдельных слоях;
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


def test_draft_is_shifted_one_thousand_east_of_the_intersection():
    plan = _plan("lot72_render_e600.txt")
    draft = make_print_draft(plan, _cfg()["print_draft"])
    tie = next(ln for ln in draft.lines if ln.kind == "main-street-row-tie")
    assert tie.start[0] == pytest.approx(1000.0, abs=1e-6)
    assert tie.start[1] == pytest.approx(0.0, abs=1e-6)


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
    main_lines = [ln for ln in doc.ModelSpace.lines if ln.Layer == "PROPERTY_LINE"]
    assert len(main_lines) == 4
    # Перекрёсток (0, 0) плана стоит в (5000, 5000): линия до перекрёстка начинается там.
    tie = next(ln for ln in doc.ModelSpace.lines if ln.Layer == "STREET_ROW_AI")
    assert tie.StartPoint[0] == pytest.approx(5000.0, abs=1e-6)
    assert tie.StartPoint[1] == pytest.approx(5000.0, abs=1e-6)
    # Участок LOT 72 лежит юго-западнее перекрёстка: часть координат меньше 5000, и это названо
    # в предупреждении, а не молча обрезается (см. docs/customer_rules.md, «Открыто»).
    assert min(ln.StartPoint[0] for ln in main_lines) < 5000.0
    assert any("окно листа" in w for w in main_result.warnings)

    builder2, doc2 = fake_autocad(draft)
    result = builder2.draw_plan(draft)
    assert set(result.layers) == {"print_property_line", "print_boundary_text", "print_street_row"}
    assert result.layers["print_property_line"] == "PROPERTY_LINE_PRINT"
    assert result.layers["print_street_row"] == "STREET_ROW_PRINT"
    assert result.layers["print_property_line"] != "PROPERTY_LINE"

    layers = doc2.Layers._items
    assert layers["STREET_ROW_PRINT"].color == 1  # красные линии улиц
    assert layers["PROPERTY_LINE_PRINT"].color == 7

    draft_lines = doc2.ModelSpace.lines
    print_lines = [ln for ln in draft_lines if ln.Layer == "PROPERTY_LINE_PRINT"]
    street_lines = [ln for ln in draft_lines if ln.Layer == "STREET_ROW_PRINT"]
    assert len(print_lines) == 4
    assert street_lines, "улицы черновика должны быть нарисованы"
    # Черновик на +1000 по X относительно перекрёстка (5000, 5000) → (6000, 5000).
    tie_draft = next(
        ln for ln in street_lines
        if ln.StartPoint[0] == pytest.approx(6000.0, abs=1e-6) and ln.StartPoint[1] == pytest.approx(5000.0, abs=1e-6)
    )
    assert math.dist(tie_draft.StartPoint, tie_draft.EndPoint) == pytest.approx(40.0, abs=1e-6)
    assert len(doc2.ModelSpace.texts) == 1
    assert "CARLTON" in doc2.ModelSpace.texts[0].TextString
