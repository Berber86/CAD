"""Размеры стен дома (R6, R8, R10): длина от угла до угла, без буквы, параллельно стене, снаружи дома."""

import math

import pytest

import offsets as off


RECT_HOUSE = [(0.0, 0.0), (26.4, 0.0), (26.4, 46.4), (0.0, 46.4), (0.0, 0.0)]


def test_dimension_labels_are_off_by_default():
    assert off.DIMENSION_LABELS is False


def test_text_has_no_direction_letter():
    dims = off.house_wall_dimensions(RECT_HOUSE)
    assert dims, "у прямоугольника должны быть размеры"
    for d in dims:
        assert d.text.endswith("'")
        assert not any(letter in d.text for letter in "NSEW")


def test_rectangle_gives_two_pairs_of_walls_with_corner_to_corner_lengths():
    dims = off.house_wall_dimensions(RECT_HOUSE)
    assert sorted(d.text for d in dims) == sorted(["26.4'", "26.4'", "46.4'", "46.4'"])


def test_text_is_inside_the_house_near_its_wall():
    # 192 Geldner: текст размера внутри дома, примерно в 1.1 ft от стены, в середине стены.
    dims = off.house_wall_dimensions(RECT_HOUSE)
    for d in dims:
        x, y = d.point
        assert 0.0 < x < 26.4 and 0.0 < y < 46.4, "размер должен стоять внутри дома"
        (ax, ay), (bx, by) = d.start, d.end
        dist = off._point_segment_distance(d.point, (d.start, d.end))
        assert math.isclose(dist, off.DIMENSION_TEXT_OFFSET_FT, abs_tol=0.01)
        mid = ((ax + bx) / 2.0, (ay + by) / 2.0)
        along = math.hypot(d.point[0] - mid[0], d.point[1] - mid[1])
        assert along <= off.DIMENSION_TEXT_OFFSET_FT + 1e-6


def test_text_is_parallel_to_its_wall():
    dims = off.house_wall_dimensions(RECT_HOUSE)
    for d in dims:
        ang = math.degrees(math.atan2(d.end[1] - d.start[1], d.end[0] - d.start[0]))
        assert math.isclose(((d.angle_deg - ang + 90.0) % 180.0) - 90.0, 0.0, abs_tol=0.01)


def test_short_walls_are_skipped():
    # Крыльцо 2 ft: короче DIMENSION_MIN_FT, размер не ставится
    house = [(0.0, 0.0), (20.0, 0.0), (20.0, 30.0), (2.0, 30.0), (2.0, 32.0), (0.0, 32.0), (0.0, 0.0)]
    values = [d.value_ft for d in off.house_wall_dimensions(house)]
    assert 2.0 not in values
    assert all(v >= off.DIMENSION_MIN_FT for v in values)


def test_closed_and_open_outline_give_same_result():
    closed = off.house_wall_dimensions(RECT_HOUSE)
    opened = off.house_wall_dimensions(RECT_HOUSE[:-1])
    assert [d.text for d in closed] == [d.text for d in opened]


def test_degenerate_outline_gives_nothing():
    assert off.house_wall_dimensions([(0.0, 0.0), (1.0, 1.0)]) == []


def test_geldner_house_walls_match_nikolai_values_where_he_wrote_them():
    """192 Geldner: Николай написал 26.5' (юг) и 46.5' (запад). Остальные две стены — 26.4' и 46.4'."""
    corners = [(4170.94, 4964.00), (4197.32, 4963.95), (4197.12, 4917.57), (4170.65, 4917.53)]
    texts = {d.text for d in off.house_wall_dimensions(corners + [corners[0]])}
    assert {"26.5'", "46.5'"} <= texts
    assert len(texts) == 4


@pytest.mark.parametrize("scale", [16.0, 32.0])
def test_scale_does_not_change_text(scale):
    a = [d.text for d in off.house_wall_dimensions(RECT_HOUSE, scale=16.0)]
    b = [d.text for d in off.house_wall_dimensions(RECT_HOUSE, scale=scale)]
    assert a == b
