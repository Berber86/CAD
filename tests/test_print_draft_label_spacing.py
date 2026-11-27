"""Only the print draft compacts bearing/distance spacing; never alter survey values."""
import copy
import math
import re
from pathlib import Path

import pytest

import acad_builder as cad
from acad_builder import AcadBuilder, TextItem, make_print_draft, estimate_text_width, format_bearing
from llm_parser import load_config, parse_with_regex, Segment

DATA = Path(__file__).parent / "data"


@pytest.fixture(autouse=True)
def fake_variant_points(monkeypatch):
    monkeypatch.setattr(cad, "_variant_point", lambda point: (*point, 0.0))


def plan():
    desc, _ = parse_with_regex((DATA / "lot72_render_e600.txt").read_text(encoding="utf-8"), auto_repair_typos=False)
    cfg = load_config()
    return AcadBuilder(cfg).plan(desc), cfg


def test_print_only_reduces_stretch_without_touching_main_boundaries_or_values():
    main, cfg = plan()
    old_items = copy.deepcopy(main.items)
    original, _, _ = AcadBuilder(cfg)._side_items(main.description, main.vertices, 2.0, main.ccw, [],
                                                    front_index=main.street_boundary_indices[0],
                                                    boundary_lines=main.boundary_lines)
    draft = make_print_draft(main, {}, builder_cfg=cfg)
    originals = {item.kind: item for item in original}
    labels = {item.kind: item for item in draft.items}
    for key in ("side-1", "side-3"):
        before, after = originals[key].text, labels[key].text
        assert len(after) < len(before)
        assert re.sub(r" +", " ", after) == re.sub(r" +", " ", before)
        assert labels[key].height == 2
        index = int(key.split("-")[1]) - 1
        length = math.dist(draft.boundary_lines[index].start, draft.boundary_lines[index].end)
        assert estimate_text_width(after, 2, cfg["drawing"]) <= 0.8 * length
    assert main.items == old_items
    assert all(new.start == pytest.approx((old.start[0] + 1000, old.start[1])) and
               new.end == pytest.approx((old.end[0] + 1000, old.end[1]))
               for old, new in zip(main.boundary_lines, draft.boundary_lines))


class MeasuredText:
    """Fake styled AutoCAD text with wider spaces than the estimate."""
    def __init__(self, text, rotation):
        self.TextString = text
        self.Rotation = rotation
        self.TextAlignmentPoint = None
    def Update(self):
        pass
    def GetBoundingBox(self):
        assert self.Rotation == 0
        chars = re.sub(r"%%d", "°", self.TextString, flags=re.IGNORECASE)
        width = sum(2.0 if c == " " else 1.2 for c in chars)
        return ((0., 0., 0.), (width, 2., 0.))


def test_actual_cad_measurement_removes_only_extra_gap_and_restores_rotation():
    cfg = load_config()
    builder = AcadBuilder(cfg)
    segment = Segment(ns="N", degrees=56, minutes=36, seconds=12, ew="W", distance_ft=100)
    bearing = format_bearing(segment, use_control_code=True)
    value = "100.00'"
    item = TextItem(bearing + " " * 40 + value, (2, 3), 0.7, kind="side-1", height=2)
    entity = MeasuredText(item.text, item.rotation)
    assert builder._fit_print_bearing_text(entity, item, segment, 100, feet_inches=False) is None
    assert entity.TextString.startswith(bearing) and entity.TextString.endswith(value)
    assert re.sub(r" +", " ", entity.TextString) == re.sub(r" +", " ", item.text)
    assert len(entity.TextString) < len(item.text)
    assert entity.Rotation == pytest.approx(item.rotation)
    entity.Rotation = 0
    bbox = entity.GetBoundingBox()
    assert bbox[1][0] <= 80
    entity.Rotation = item.rotation
    assert entity.TextAlignmentPoint[:2] == pytest.approx(item.point)


def test_unfit_bearing_and_distance_are_never_truncated_or_shrunk():
    builder = AcadBuilder(load_config())
    segment = Segment(ns="N", degrees=56, minutes=36, seconds=12, ew="W", distance_ft=10)
    item = TextItem(format_bearing(segment, use_control_code=True) + " " * 6 + "10.00'", (2, 3), 0.7,
                    kind="side-1", height=2)
    entity = MeasuredText(item.text, item.rotation)
    error = builder._fit_print_bearing_text(entity, item, segment, 10, feet_inches=False)
    assert "одним пробелом" in error
    assert entity.TextString == re.sub(r"  +", " ", item.text)
    assert entity.Rotation == pytest.approx(item.rotation)


def test_failed_cad_measurement_preserves_estimate_and_reports():
    builder = AcadBuilder(load_config())
    segment = Segment(ns="N", degrees=56, minutes=36, seconds=12, ew="W", distance_ft=100)
    item = TextItem(format_bearing(segment, use_control_code=True) + " " * 20 + "100.00'", (2, 3), -0.7,
                    kind="side-1", height=2)
    entity = MeasuredText(item.text, item.rotation)
    entity.GetBoundingBox = lambda: (_ for _ in ()).throw(RuntimeError("unsupported"))
    warning = builder._fit_print_bearing_text(entity, item, segment, 100, feet_inches=False)
    assert "не удалось измерить" in warning
    assert entity.TextString == item.text
    assert entity.Rotation == pytest.approx(item.rotation)


def test_negative_rotation_normalized_by_autocad_to_0_2pi_restores_cleanly():
    class WrappingMeasuredText(MeasuredText):
        @property
        def Rotation(self):
            return self._rot

        @Rotation.setter
        def Rotation(self, value):
            self._rot = float(value) % (2.0 * math.pi)

    builder = AcadBuilder(load_config())
    segment = Segment(ns="N", degrees=56, minutes=36, seconds=12, ew="W", distance_ft=100)
    bearing = format_bearing(segment, use_control_code=True)
    item = TextItem(bearing + " " * 30 + "100.00'", (2, 3), -0.5829, kind="side-1", height=2)
    entity = WrappingMeasuredText(item.text, item.rotation)
    assert builder._fit_print_bearing_text(entity, item, segment, 100, feet_inches=False) is None
    assert entity.Rotation == pytest.approx(item.rotation % (2.0 * math.pi))


def test_failed_rotation_restore_still_reports_error():
    class StuckRotationText(MeasuredText):
        @property
        def Rotation(self):
            return 0.0

        @Rotation.setter
        def Rotation(self, value):
            pass

    builder = AcadBuilder(load_config())
    segment = Segment(ns="N", degrees=56, minutes=36, seconds=12, ew="W", distance_ft=100)
    bearing = format_bearing(segment, use_control_code=True)
    item = TextItem(bearing + " " * 30 + "100.00'", (2, 3), -0.5829, kind="side-1", height=2)
    entity = StuckRotationText(item.text, 0.0)
    warning = builder._fit_print_bearing_text(entity, item, segment, 100, feet_inches=False)
    assert warning == "не удалось восстановить положение подписи; проверьте её в AutoCAD"
