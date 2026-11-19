"""
Шаг 4, черновик 4.3: запись оффсетов в чертёж и выбор области (AutoCAD COM заменён заглушкой).

Сцена — тот же синтетический участок, что в тестах Шага 2 (40 × 100 ft, дом, сарай, заборы).
"""

import math

import pytest

import acad_builder as ab
import offsets as off
from llm_parser import load_config
from test_feature_labeling import FakeEntity, build_scene, fake_autocad

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
    # Многострочная подпись (две стороны у одной вершины) уходит в MText как \P.
    assert {item.text.replace("\n", "\\P") for item in result.plan.items} <= written_texts


def test_leader_offsets_get_leader_objects_text_offsets_do_not(com):
    builder, _app, doc = com()
    result = builder.build_offsets(area=WHOLE_LOT, dry_run=False)
    leaders = [e for e in doc.ModelSpace.created if e.ObjectName == "AcDbLeader"]
    assert len(leaders) == result.written.created_leaders
    text_items = [i for i in result.plan.items if i.style == "text"]
    plain = [e for e in doc.ModelSpace.created
             if e.ObjectName == "AcDbText" and e.Layer == off.OFFSET_LAYER]
    assert len(plain) == len(text_items) == result.written.created_texts


def test_leader_text_is_attached_away_from_the_arrow(com):
    """
    У Николая MText подписи стоит сбоку от выноски и растёт от неё: вставка — на ближнем
    к стрелке краю, привязка 4 (влево-середина) справа от объекта и 6 (вправо-середина)
    слева. Текст не накрывает стрелку, конец выноски — во вставке текста.
    """
    builder, _app, doc = com()
    result = builder.build_offsets(area=WHOLE_LOT, dry_run=False)
    leaders = [e for e in doc.ModelSpace.created if e.ObjectName == "AcDbLeader"]
    assert leaders
    checked = 0
    for leader in leaders:
        mtext = leader.Annotation
        if mtext is None:
            continue
        attach = int(mtext.AttachmentPoint)
        assert attach in (4, 6), mtext.TextString
        coordinates = list(leader.Coordinates)
        tip = (coordinates[0], coordinates[1])
        end = (coordinates[-3], coordinates[-2])
        insert = mtext.InsertionPoint
        assert float(insert[1]) == pytest.approx(float(end[1]))
        # Стрелка не попадает внутрь блока текста: выноска кончается у его края.
        lines = str(mtext.TextString).split("\\P")
        fcfg = ab.feature_settings(builder.cfg)
        width = max(
            ab.feature_text_width(line, float(mtext.Height), fcfg) for line in lines
        )
        block_h = float(mtext.Height) * (1.0 + off.TEXT_LINE_SPACING * (len(lines) - 1))
        left = float(insert[0]) if attach == 4 else float(insert[0]) - width
        bottom = float(insert[1]) - block_h / 2.0
        inside = (
            left - 1e-6 <= tip[0] <= left + width + 1e-6
            and bottom - 1e-6 <= tip[1] <= bottom + block_h + 1e-6
        )
        assert not inside, (mtext.TextString, tip, insert, attach)
        checked += 1
    assert checked


def test_nothing_to_write_does_not_open_undo_group(com):
    builder, _app, doc = com()
    result = builder.build_offsets(area=[(200.0, 200.0), (210.0, 200.0), (210.0, 210.0), (200.0, 210.0)])
    assert result.plan.items == []
    assert result.written is None
    assert doc.undo_marks == []


class _Selection:
    """Набор выделения AutoCAD: Count и Item, как у COM-коллекции."""

    def __init__(self, *entities):
        self._items = list(entities)

    @property
    def Count(self):
        return len(self._items)

    def Item(self, index):
        return self._items[index]


def _window_object(x0, y0, x1, y1, kind="AcDbLine"):
    entity = FakeEntity(kind, "CODE_CREW")
    entity.GetBoundingBox = lambda: ((x0, y0, 0.0), (x1, y1, 0.0))
    return entity


def _closed_polyline(points):
    flat = tuple(value for point in points for value in point)
    return FakeEntity("AcDbPolyline", "AREA", Coordinates=flat, Closed=True)


def test_area_from_window_selection_is_bounding_box(com):
    builder, _app, doc = com()
    doc.PickfirstSelectionSet = _Selection(
        _window_object(10.0, 20.0, 30.0, 25.0),
        _window_object(12.0, 40.0, 28.0, 50.0),
    )
    assert builder.read_offset_area(doc) == [(10.0, 20.0), (30.0, 20.0), (30.0, 50.0), (10.0, 50.0)]


def test_area_from_single_closed_polyline_is_its_outline(com):
    builder, _app, doc = com()
    square = [(0.0, 0.0), (20.0, 0.0), (20.0, 30.0), (0.0, 30.0)]
    doc.PickfirstSelectionSet = _Selection(_closed_polyline(square))
    assert builder.read_offset_area(doc) == square


def test_no_selection_is_a_clean_error_and_leaves_drawing(com):
    builder, _app, doc = com()
    before = len(doc.ModelSpace.entities)
    doc.PickfirstSelectionSet = _Selection()
    with pytest.raises(ab.AcadBuilderError, match="не выделена в AutoCAD"):
        builder.read_offset_area(doc)
    assert len(doc.ModelSpace.entities) == before
    assert doc.undo_marks == []


def test_single_line_selection_is_degenerate(com):
    builder, _app, doc = com()
    doc.PickfirstSelectionSet = _Selection(_window_object(0.0, 5.0, 30.0, 5.0))
    with pytest.raises(ab.AcadBuilderError, match="вырождена"):
        builder.read_offset_area(doc)


def test_build_offsets_reads_selection_when_no_area_is_given(com):
    # Сценарий пользователя: область выделена заранее, кнопка программы строит оффсеты в ней.
    builder, _app, doc = com()
    doc.PickfirstSelectionSet = _Selection(_closed_polyline(WHOLE_LOT))
    result = builder.build_offsets(dry_run=False)
    assert result.area == WHOLE_LOT
    assert result.written is not None and result.written.created_texts + result.written.created_leaders == len(result.plan.items)
    assert doc.undo_marks == ["start", "end"]


def test_geometry_points_of_feature_shapes():
    from shapely.geometry import LineString, Polygon

    square = Polygon([(0, 0), (2, 0), (2, 2), (0, 2)])
    assert ab._geometry_points(square) == [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]
    line = LineString([(0, 0), (5, 0)])
    assert ab._geometry_points(line) == [(0.0, 0.0), (5.0, 0.0)]
    assert ab._geometry_points(None) == []


def test_report_explains_zero_when_area_misses_objects(com):
    # Область выделена не там, где объекты: отчёт называет шаг, на котором всё отсеялось.
    builder, _app, _doc = com()
    result = builder.build_offsets(area=[(500.0, 500.0), (510.0, 500.0), (510.0, 510.0), (500.0, 510.0)],
                                   dry_run=True)
    text = result.report()
    assert "Подписей: 0" in text
    assert "в выделенной области: 0" in text
    assert "ни один распознанный объект не попал в выделенную область" in text
    assert "Участок: слой" in text and "Фронт M-ROW-LINE" in text


def test_report_counts_selection_and_area_bounds(com):
    builder, _app, doc = com()
    doc.PickfirstSelectionSet = _Selection(_closed_polyline(WHOLE_LOT))
    result = builder.build_offsets(dry_run=True)
    text = result.report()
    assert "выделено в AutoCAD объектов 1" in text
    assert "x -5.0…50.0, y -5.0…110.0 ft" in text
    assert "в выделенной области:" in text and "Подписей:" in text


def test_two_line_offset_is_written_as_mtext_with_pline_breaks(com):
    """
    Многострочная подпись — один MText с ``\\P`` (``FE.0.6'S\\PFE.1.1'E``,
    ``BLDG\\P2.5'S``): так у Николая. Однострочная (``0.4'W``, ``3.5'S``) — TEXT.
    """
    from types import SimpleNamespace

    builder, _app, doc = com()
    item = SimpleNamespace(
        text="FE.0.2'S\nFE.0.3'W", point=(10.0, 20.0), height_ft=0.9, rotation_deg=0.0,
    )
    created = builder._add_offset_texts(doc.ModelSpace, item, off.OFFSET_LAYER, "Standard", 1.67)
    assert created == 1
    mtexts = [e for e in doc.ModelSpace.created if e.ObjectName == "AcDbMText"]
    assert len(mtexts) == 1
    assert mtexts[0].TextString == "FE.0.2'S\\PFE.0.3'W"
    assert not [e for e in doc.ModelSpace.created if e.ObjectName == "AcDbText"]
    assert mtexts[0].AttachmentPoint == 5  # middle-center: блок стоит по центру точки
    assert float(mtexts[0].InsertionPoint[1]) == pytest.approx(20.0)

    single = SimpleNamespace(text="0.4'W", point=(10.0, 20.0), height_ft=0.9, rotation_deg=0.0)
    assert builder._add_offset_texts(
        doc.ModelSpace, single, off.OFFSET_LAYER, "Standard", 1.67
    ) == 1
    texts = [e for e in doc.ModelSpace.created if e.ObjectName == "AcDbText"]
    assert [e.TextString for e in texts] == ["0.4'W"]


def test_text_rotation_is_written_to_the_offset_labels(com):
    """R10: подпись без выноски пишется повёрнутой — угол из плана доходит до ``Rotation``."""
    from types import SimpleNamespace

    builder, _app, doc = com()
    item = SimpleNamespace(
        text="2.4'S", point=(10.0, 20.0), height_ft=1.1, rotation_deg=90.0,
    )
    assert builder._add_offset_texts(
        doc.ModelSpace, item, off.OFFSET_LAYER, "Standard", 1.67
    ) == 1
    texts = [e for e in doc.ModelSpace.created if e.ObjectName == "AcDbText"]
    assert len(texts) == 1
    assert float(texts[0].Rotation) == pytest.approx(math.pi / 2, abs=1e-6)
