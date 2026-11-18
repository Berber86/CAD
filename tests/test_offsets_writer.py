"""
Шаг 4, черновик 4.3: запись оффсетов в чертёж и выбор области (AutoCAD COM заменён заглушкой).

Сцена — тот же синтетический участок, что в тестах Шага 2 (40 × 100 ft, дом, сарай, заборы).
"""

import sys
import types

import pytest

import acad_builder as ab
import offsets as off
from llm_parser import load_config
from test_feature_labeling import build_scene, fake_autocad

WHOLE_LOT = [(-5.0, -5.0), (50.0, -5.0), (50.0, 110.0), (-5.0, 110.0)]


@pytest.fixture()
def com(monkeypatch):
    monkeypatch.setattr(ab, "_co_initialize", lambda: None)
    monkeypatch.setattr(ab, "_co_uninitialize", lambda: None)
    monkeypatch.setattr(ab, "_variant_points", lambda points: [c for p in points for c in p])
    monkeypatch.setattr(ab, "_variant_point", lambda point, z=0.0: [point[0], point[1], z])
    monkeypatch.setattr(ab, "_null_dispatch", lambda: None)

    def connect(**kwargs):
        builder = ab.AcadBuilder(load_config())
        app, doc = fake_autocad(build_scene(with_existing=True), **kwargs)
        builder._app, builder._doc = app, doc
        return builder, app, doc

    return connect


def test_dry_run_plans_offsets_and_leaves_drawing_untouched(com):
    builder, _app, doc = com()
    before = len(doc.ModelSpace.entities)
    result = builder.build_offsets(area=WHOLE_LOT, dry_run=True)
    assert result.dry_run and result.written is None
    assert len(doc.ModelSpace.entities) == before
    assert doc.undo_marks == []
    texts = [item.text for item in result.plan.items]
    assert any(text.startswith("FE.") for text in texts)
    assert any(item.style == "text" for item in result.plan.items)
    assert result.north_deg == pytest.approx(90.0)


def test_write_creates_one_undo_group_on_offset_layer(com):
    builder, _app, doc = com()
    result = builder.build_offsets(area=WHOLE_LOT, dry_run=False)
    written = result.written
    assert written is not None
    assert written.failed == []
    assert written.created_texts + written.created_leaders == len(result.plan.items)
    assert written.layer == off.OFFSET_LAYER
    assert doc.undo_marks == ["start", "end"]
    created = [e for e in doc.ModelSpace.created if e.Layer == off.OFFSET_LAYER]
    assert created, "оффсеты не записаны в слой"
    written_texts = {getattr(e, "TextString", None) for e in created}
    assert {item.text for item in result.plan.items} <= written_texts


def test_leader_offsets_get_leader_objects_text_offsets_do_not(com):
    builder, _app, doc = com()
    result = builder.build_offsets(area=WHOLE_LOT, dry_run=False)
    leaders = [e for e in doc.ModelSpace.created if e.ObjectName == "AcDbLeader"]
    assert len(leaders) == result.written.created_leaders
    text_items = [i for i in result.plan.items if i.style == "text"]
    plain = [e for e in doc.ModelSpace.created
             if e.ObjectName == "AcDbText" and e.Layer == off.OFFSET_LAYER]
    assert len(plain) == len(text_items) == result.written.created_texts


def test_nothing_to_write_does_not_open_undo_group(com):
    builder, _app, doc = com()
    result = builder.build_offsets(area=[(200.0, 200.0), (210.0, 200.0), (210.0, 210.0), (200.0, 210.0)])
    assert result.plan.items == []
    assert result.written is None
    assert doc.undo_marks == []


def test_pick_area_returns_rectangle_from_two_corners(com, monkeypatch):
    monkeypatch.setitem(sys.modules, "pythoncom", types.SimpleNamespace(Missing=None))
    builder, app, doc = com()

    class Utility:
        def GetPoint(self, _base, _prompt):
            return (10.0, 20.0, 0.0)

        def GetCorner(self, base, _prompt):
            assert tuple(base)[:2] == (10.0, 20.0)
            return (30.0, 50.0, 0.0)

    doc.Utility = Utility()
    assert builder.pick_offset_area(app, doc) == [(10.0, 20.0), (30.0, 20.0), (30.0, 50.0), (10.0, 50.0)]


def test_pick_area_escape_is_a_clean_error(com, monkeypatch):
    monkeypatch.setitem(sys.modules, "pythoncom", types.SimpleNamespace(Missing=None))
    builder, app, doc = com()

    class Utility:
        def GetPoint(self, _base, _prompt):
            raise RuntimeError("user cancelled")

    doc.Utility = Utility()
    with pytest.raises(ab.AcadBuilderError, match="Чертёж не изменён"):
        builder.pick_offset_area(app, doc)


def test_degenerate_area_is_refused(com, monkeypatch):
    monkeypatch.setitem(sys.modules, "pythoncom", types.SimpleNamespace(Missing=None))
    builder, app, doc = com()

    class Utility:
        def GetPoint(self, _base, _prompt):
            return (10.0, 20.0, 0.0)

        def GetCorner(self, _base, _prompt):
            return (10.0, 60.0, 0.0)  # та же X: прямоугольника нет

    doc.Utility = Utility()
    with pytest.raises(ab.AcadBuilderError, match="вырождена"):
        builder.pick_offset_area(app, doc)


def test_geometry_points_of_feature_shapes():
    from shapely.geometry import LineString, Polygon

    square = Polygon([(0, 0), (2, 0), (2, 2), (0, 2)])
    assert ab._geometry_points(square) == [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]
    line = LineString([(0, 0), (5, 0)])
    assert ab._geometry_points(line) == [(0.0, 0.0), (5.0, 0.0)]
    assert ab._geometry_points(None) == []
