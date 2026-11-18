"""Шаг 4: участок — контур, который касается выделенной геодезистом области."""

import pytest
from shapely.geometry import Polygon

import feature_labeler as fl
from acad_builder import _area_polygon


def _rect(snap, layer, x0, y0, x1, y1):
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    for a, b in zip(corners, corners[1:]):
        snap.lines.append(fl.DrawingLine(layer=layer, points=(a, b)))


def _two_lots():
    """Два замкнутых контура M-TAX-LINE; снятый забор — только внутри левого (он «весомее»)."""
    snap = fl.DrawingSnapshot()
    _rect(snap, "M-TAX-LINE", 0, 0, 40, 100)
    _rect(snap, "M-TAX-LINE", 200, 0, 240, 100)
    snap.lines.append(fl.DrawingLine(layer="S-FENCE", points=((2, 50), (38, 50))))
    snap.lines.append(fl.DrawingLine(layer="S-FENCE", points=((2, 60), (38, 60))))
    return snap


def test_default_picks_the_surveyed_lot():
    boundary = fl.find_property_polygon(_two_lots())
    assert boundary.polygon.bounds[0] == pytest.approx(0.0)


def test_area_over_other_lot_picks_that_lot():
    area = _area_polygon([(190, -10), (260, -10), (260, 110), (190, 110)])
    boundary = fl.find_property_polygon(_two_lots(), near=area)
    assert boundary.polygon.bounds[0] == pytest.approx(200.0)
    assert boundary.polygon.bounds[2] == pytest.approx(240.0)


def test_area_touching_no_contour_raises():
    area = _area_polygon([(500, 500), (600, 500), (600, 600), (500, 600)])
    with pytest.raises(fl.FeatureLabelError):
        fl.find_property_polygon(_two_lots(), near=area)


def test_area_polygon_none_for_missing_or_degenerate_area():
    assert _area_polygon(None) is None
    assert _area_polygon([(0, 0), (1, 1)]) is None
    assert isinstance(_area_polygon([(0, 0), (10, 0), (10, 10)]), Polygon)


def _green_lot(snap, dx=400.0):
    """Зелёный участок (цвет 3) правее контуров _two_lots: три стороны в слое дома, фасад — M-ROW-LINE."""
    def green(a, b):
        snap.lines.append(
            fl.DrawingLine(layer="ST-BLDG-LINE", points=((a[0] + dx, a[1]), (b[0] + dx, b[1])), color=3)
        )

    green((0, 0), (40, 0))
    green((40, 0), (40, 100))
    green((40, 100), (0, 100))
    snap.lines.append(
        fl.DrawingLine(layer="M-ROW-LINE", points=((dx, 100), (dx, 0)))
    )


def test_green_lot_under_area_is_the_parcel():
    snap = _two_lots()
    _green_lot(snap)
    area = _area_polygon([(390, -10), (450, -10), (450, 110), (390, 110)])
    boundary = fl.find_property_polygon(snap, near=area)
    assert boundary.polygon.bounds == pytest.approx((400.0, 0.0, 440.0, 100.0))
    assert "цвет 3" in boundary.layer


def test_area_over_m_tax_lot_still_prefers_the_m_tax_lot():
    snap = _two_lots()
    _green_lot(snap)
    area = _area_polygon([(190, -10), (260, -10), (260, 110), (190, 110)])
    boundary = fl.find_property_polygon(snap, near=area)
    assert boundary.polygon.bounds[0] == pytest.approx(200.0)


def test_green_lines_without_street_front_are_not_a_parcel():
    snap = fl.DrawingSnapshot()
    for a, b in (((400, 0), (440, 0)), ((440, 0), (440, 100)), ((440, 100), (400, 100))):
        snap.lines.append(fl.DrawingLine(layer="ST-BLDG-LINE", points=(a, b), color=3))
    area = _area_polygon([(390, -10), (450, -10), (450, 110), (390, 110)])
    with pytest.raises(fl.FeatureLabelError):
        fl.find_property_polygon(snap, near=area)
