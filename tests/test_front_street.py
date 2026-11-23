"""Угол фронтальной улицы: две программные оценки + JSON-кэш снимка карты.

Синтетическая «карта»: параллельные линии улиц под известным углом к
северу (север — вверх). Оба способа обязаны попасть в пару градусов.
"""
from __future__ import annotations

import json
import math

import pytest
from PIL import Image, ImageDraw

import front_street
import map_angle_cache


def _street_image(theta_deg: float, size=(900, 700), lines=9, spacing=90) -> Image.Image:
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    theta = math.radians(theta_deg)
    dx, dy = math.sin(theta), -math.cos(theta)  # по часовой от севера (вверх)
    px, py = -dy, dx
    cx, cy = size[0] / 2, size[1] / 2
    for i in range(-lines // 2, lines // 2 + 1):
        ox, oy = px * i * spacing, py * i * spacing
        draw.line((cx + ox - dx * 1200, cy + oy - dy * 1200,
                   cx + ox + dx * 1200, cy + oy + dy * 1200), fill="black", width=4)
    return image


@pytest.mark.parametrize("theta", [0, 10, 30, 60, 90, 135, 170])
def test_both_estimators_find_known_street_angle(theta):
    image = _street_image(theta)
    hough = front_street.estimate_hough_lines(image)
    tensor = front_street.estimate_structure_tensor(image)
    assert hough is not None and front_street.angle_difference(hough, theta) <= 2.0
    assert tensor is not None and front_street.angle_difference(tensor, theta) <= 2.0


def test_blank_image_has_no_angle():
    blank = Image.new("RGB", (400, 300), "white")
    assert front_street.estimate_hough_lines(blank) is None
    assert front_street.estimate_structure_tensor(blank) is None
    assert front_street.estimate_all("/nonexistent/map.jpg") == {
        "python_hough_lines": None, "python_structure_tensor": None,
    }


def test_angle_arithmetic_wraps_at_180():
    assert front_street.normalize_line_angle(190) == 10.0
    assert front_street.normalize_line_angle(-10) == 170.0
    assert front_street.angle_difference(5, 175) == 10.0
    assert front_street.angle_difference(10, 100) == 90.0


def test_cache_roundtrip_next_to_map(tmp_path):
    map_image = tmp_path / "TAX_MAP_20261123_120000.jpg"
    _street_image(30).save(map_image)
    path = map_angle_cache.write_angles(
        map_image, llm_angle=35.0,
        python_angles={"python_hough_lines": 30.0, "python_structure_tensor": 31.0},
        address="417 WEST 33RD STREET", block="731", lot="53",
    )
    assert path == tmp_path / "TAX_MAP_20261123_120000_angles.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["front_street_angle_deg"] == {
        "llm": 35.0, "python_hough_lines": 30.0, "python_structure_tensor": 31.0,
    }
    assert data["pairwise_difference_deg"]["llm vs python_hough_lines"] == 5.0
    assert data["block"] == "731" and data["sha256"]
    assert map_angle_cache.read_angles(map_image)["front_street_angle_deg"]["llm"] == 35.0
    assert map_angle_cache.fmt_angle(35.0) == "35°"
    assert map_angle_cache.fmt_angle(None) == "—"
