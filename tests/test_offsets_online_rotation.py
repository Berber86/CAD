"""
Шаг 4: «FE.O/L» для объекта на линии участка и поворот текста вдоль границы.

«O/L» включено в 4.24 по четвёртой паре заказчика (378 Mother Gaston): у Николая
``BLDG`` / ``O/L`` в углу на фронтовой линии, ``FE.O/L`` на Geldner, ``BR.WALL`` / ``O/L``
на Brooklyn. Поворот текста вдоль границы остался выключенным (``TEXT_FOLLOWS_BOUNDARY``).
"""

import math

import pytest

import offsets as off


# Участок 20 × 40 ft, левая граница x=0.
LOT = [(0.0, 0.0), (20.0, 0.0), (20.0, 40.0), (0.0, 40.0), (0.0, 0.0)]


def _fence(points, handle="F1"):
    return off.Feature(kind="fence", handle=handle, points=tuple(points))


def _house(points, handle="H1"):
    return off.Feature(kind="house", handle=handle, points=tuple(points))


def test_online_flag_is_on_after_the_fourth_pair():
    # 4.24: три чертежа из четырёх дают «O/L» у Николая — правило включено.
    assert off.ONLINE_LABELS is True
    assert off.ONLINE_FENCE_TEXT == "FE.O/L"
    assert off.ONLINE_TEXT == "O/L"


@pytest.fixture
def online_on(monkeypatch):
    monkeypatch.setattr(off, "ONLINE_LABELS", True)


def test_default_plan_marks_on_line_fence():
    fence = _fence([(0.0, 15.0), (3.0, 15.0), (3.0, 25.0)])
    plan = off.plan_offsets(LOT, [fence], north_deg=90.0)
    assert any(it.text == "FE.O/L" for it in plan.items)


def test_fence_vertex_on_lot_line_gets_online_label(online_on):
    # Один конец забора стоит на левой границе (x=0), второй — в 3 ft внутри.
    fence = _fence([(0.0, 15.0), (3.0, 15.0), (3.0, 25.0)])
    plan = off.plan_offsets(LOT, [fence], north_deg=90.0)
    texts = [it.text for it in plan.items]
    assert "FE.O/L" in texts


def test_online_label_is_a_leader_next_to_the_fence(online_on):
    fence = _fence([(0.0, 15.0), (3.0, 15.0), (3.0, 25.0)])
    plan = off.plan_offsets(LOT, [fence], north_deg=90.0)
    online = [it for it in plan.items if it.text == "FE.O/L"]
    assert len(online) == 1
    assert online[0].style == "leader"
    assert online[0].value_ft == 0.0


def test_online_flag_off_gives_no_online_label(monkeypatch):
    monkeypatch.setattr(off, "ONLINE_LABELS", False)
    fence = _fence([(0.0, 15.0), (3.0, 15.0), (3.0, 25.0)])
    plan = off.plan_offsets(LOT, [fence], north_deg=90.0)
    assert all(it.text != "FE.O/L" for it in plan.items)


def test_fence_not_on_line_gets_no_online_label():
    fence = _fence([(0.5, 15.0), (3.0, 15.0), (3.0, 25.0)])
    plan = off.plan_offsets(LOT, [fence], north_deg=90.0)
    assert all(it.text != "FE.O/L" for it in plan.items)


LOT40 = [(0.0, 0.0), (40.0, 0.0), (40.0, 40.0), (0.0, 40.0), (0.0, 0.0)]


@pytest.fixture
def rotation_on(monkeypatch):
    monkeypatch.setattr(off, "TEXT_FOLLOWS_BOUNDARY", True)


def test_rotation_is_on_by_default_and_off_by_flag():
    """С 4.26 поворот включён (R10), но подпись дома всегда горизонтальна (R28): 0° в обоих режимах."""
    assert off.TEXT_FOLLOWS_BOUNDARY is True
    house = _house([(6.0, 10.0), (14.0, 10.0), (14.0, 30.0), (6.0, 30.0)])
    for flag in (True, False):
        saved = off.TEXT_FOLLOWS_BOUNDARY
        off.TEXT_FOLLOWS_BOUNDARY = flag
        try:
            plan = off.plan_offsets(LOT40, [house], north_deg=90.0)
        finally:
            off.TEXT_FOLLOWS_BOUNDARY = saved
        assert all(it.rotation_deg == 0.0 for it in plan.items if it.style == "text")


def test_house_text_is_parallel_to_its_boundary(rotation_on):
    # Даже с поворотом вдоль границы (R10) текст дома всегда 0° — «как в книге» (R28).
    house = _house([(6.0, 10.0), (14.0, 10.0), (14.0, 30.0), (6.0, 30.0)])
    plan = off.plan_offsets(LOT40, [house], north_deg=90.0)
    text_items = [it for it in plan.items if it.style == "text"]
    assert text_items, "дом должен получить текст без выноски"
    for it in text_items:
        assert it.rotation_deg == 0.0


def test_house_text_near_horizontal_boundary_is_horizontal(rotation_on):
    # Дом в 10 ft от нижней (горизонтальной) границы: текст 0°.
    house = _house([(10.0, 10.0), (12.0, 10.0), (12.0, 30.0), (10.0, 30.0)])
    plan = off.plan_offsets(LOT40, [house], north_deg=90.0)
    text_items = [it for it in plan.items if it.style == "text"]
    assert text_items
    for it in text_items:
        assert math.isclose(it.rotation_deg % 180.0, 0.0, abs_tol=0.01)


@pytest.mark.parametrize(
    "point, expected",
    [((0.0, 5.0), 90.0), ((5.0, 0.0), 0.0), ((20.0, 5.0), 90.0)],
)
def test_boundary_angle_helper(point, expected):
    segments = list(zip(LOT, LOT[1:]))
    assert math.isclose(abs(off._boundary_angle_deg(point, segments)), expected, abs_tol=0.01)


def test_leader_labels_keep_zero_rotation(online_on):
    fence = _fence([(0.0, 15.0), (3.0, 15.0), (3.0, 25.0)])
    plan = off.plan_offsets(LOT, [fence], north_deg=90.0)
    for it in plan.items:
        if it.style == "leader":
            assert it.rotation_deg == 0.0
