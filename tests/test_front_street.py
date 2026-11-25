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


def _tax_map_image(theta_deg: float, size=(1100, 800), lot_fill="white") -> Image.Image:
    """
    Похожая на настоящую tax map картинка: серая полоса улицы, участки
    чёрными межами по обе стороны (ДВА перпендикулярных семейства линий) и
    голубой «наш» участок фасадом на улицу под углом theta к северу.
    """
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    theta = math.radians(theta_deg)
    ux, uy = math.sin(theta), -math.cos(theta)   # вдоль улицы
    vx, vy = math.cos(theta), math.sin(theta)    # поперёк улицы
    cx, cy = size[0] / 2, size[1] / 2

    def point(along: float, across: float) -> tuple[float, float]:
        return (cx + ux * along + vx * across, cy + uy * along + vy * across)

    def rect(along0, along1, across0, across1, **kwargs):
        draw.polygon([point(along0, across0), point(along1, across0),
                      point(along1, across1), point(along0, across1)], **kwargs)

    # Полоса улицы (серая заливка) и ряды участков по обе стороны.
    rect(-800, 800, -45, 45, fill=(218, 218, 218))
    for side in (-1, 1):
        near = side * 45
        far = side * (45 + 150)
        for i in range(-6, 6):
            a0, a1 = i * 100, i * 100 + 100
            if side < 0 and i == 0:
                continue  # место нашего участка
            rect(a0, a1, near, far, fill=lot_fill, outline="black", width=3)
    # Наш участок: голубой, фасадом на улицу (верхняя сторона полосы).
    rect(0, 100, -195, -45, fill=(164, 227, 227), outline="black", width=3)
    return image


@pytest.mark.parametrize("theta", [35, 118])
def test_tax_map_picks_front_street_family_not_the_cross_one(theta):
    image = _tax_map_image(theta)
    hough = front_street.estimate_hough_lines(image)
    tensor = front_street.estimate_structure_tensor(image)
    assert hough is not None and front_street.angle_difference(hough, theta) <= 3.5
    assert tensor is not None and front_street.angle_difference(tensor, theta) <= 3.5


@pytest.mark.parametrize("theta", [35, 125])
def test_tax_map_with_street_gray_lots_still_picks_front(theta):
    # Живые tax map заливают участки тем же серым, что и улицу: проба
    # яркостью «улица темнее участков» там называет улицей боковые стороны
    # и уводит оценку на перпендикулярное семейство. Разделять должна
    # топология: за фасадом — одна огромная область улицы, за боками —
    # области соседних участков.
    image = _tax_map_image(theta, lot_fill=(218, 218, 218))
    hough = front_street.estimate_hough_lines(image)
    tensor = front_street.estimate_structure_tensor(image)
    assert hough is not None and front_street.angle_difference(hough, theta) <= 3.5
    assert tensor is not None and front_street.angle_difference(tensor, theta) <= 3.5


def test_real_staten_island_tax_map_matches_deed_frontage():
    # Настоящий снимок портала (Staten Island, block 4252, lot 19, фронт на
    # Reno Ave). Эталон 125° — румб фронтальной стороны из самого дида
    # (N 55°04′10″ W). Прежняя проба яркостью давала здесь 34° (боковые
    # межи), Vision на разных прогонах — 125° и 65°.
    path = __file__.replace("test_front_street.py", "data/tax_map_staten_island_4252_19.jpg")
    result = front_street.estimate_all(path)
    assert result["python_hough_lines"] is not None
    assert result["python_structure_tensor"] is not None
    assert front_street.angle_difference(result["python_hough_lines"], 125.0) <= 4.0
    assert front_street.angle_difference(result["python_structure_tensor"], 125.0) <= 4.0


def test_real_manhattan_tax_map_matches_measured_street():
    # Настоящий снимок портала (Manhattan, block 731, lot 53, фронт на W 33 ST).
    # Эталон 119.0° — замер по длинным отрезкам бордюров на том же снимке;
    # Vision на живом прогоне дала 117°, прежние оценки кодом — 105°/169°.
    path = __file__.replace("test_front_street.py", "data/tax_map_manhattan_731_53.png")
    result = front_street.estimate_all(path)
    assert result["python_hough_lines"] is not None
    assert result["python_structure_tensor"] is not None
    assert front_street.angle_difference(result["python_hough_lines"], 119.0) <= 4.0
    assert front_street.angle_difference(result["python_structure_tensor"], 119.0) <= 4.0


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
