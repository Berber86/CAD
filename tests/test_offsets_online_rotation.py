"""Шаг 4, правила из присланного ориентира (4.18): «FE.O/L» для забора на линии и поворот текста вдоль границы."""

import math

import pytest

import offsets as off


# Участок 20 × 40 ft, левая граница x=0.
LOT = [(0.0, 0.0), (20.0, 0.0), (20.0, 40.0), (0.0, 40.0), (0.0, 0.0)]


def _fence(points, handle="F1"):
    return off.Feature(kind="fence", handle=handle, points=tuple(points))


def _house(points, handle="H1"):
    return off.Feature(kind="house", handle=handle, points=tuple(points))


def test_online_flag_is_on_since_4_23():
    # 4.18 держал флаг выключенным: примеры противоречили. В 4.23 перепроверено — «FE.O/L» Николая
    # на 192 Geldner ложится в 0.5 ft от нашей вершины, в Brooklyn — точно (0.0 ft), заборные метрики
    # не меняются. См. docs/step4_hypotheses.md.
    assert off.ONLINE_LABELS is True
    assert off.ONLINE_FENCE_TEXT == "FE.O/L"


@pytest.fixture
def online_on(monkeypatch):
    monkeypatch.setattr(off, "ONLINE_LABELS", True)


def test_default_plan_labels_fence_on_line_since_4_23():
    fence = _fence([(0.0, 15.0), (3.0, 15.0), (3.0, 25.0)])
    plan = off.plan_offsets(LOT, [fence], north_deg=90.0)
    assert any(it.text == "FE.O/L" for it in plan.items)


def test_fence_vertex_on_lot_line_gets_online_label(online_on):
    # Один конец забора стоит на левой границе (x=0), второй — в 3 ft внутри.
    fence = _fence([(0.0, 15.0), (3.0, 15.0), (3.0, 25.0)])
    plan = off.plan_offsets(LOT, [fence], north_deg=90.0)
    texts = [it.text for it in plan.items]
    assert "FE.O/L" in texts


def test_online_label_is_text_next_to_the_fence(online_on):
    fence = _fence([(0.0, 15.0), (3.0, 15.0), (3.0, 25.0)])
    plan = off.plan_offsets(LOT, [fence], north_deg=90.0)
    online = [it for it in plan.items if it.text == "FE.O/L"]
    assert len(online) == 1
    assert online[0].style == "text" and online[0].leader is None
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


def test_house_text_follows_boundary_since_4_23():
    # Общий TEXT_FOLLOWS_BOUNDARY по-прежнему выключен (на эталонах угол следует за участком),
    # но текст дома у стены поворачивается вдоль границы отдельным правилом 4.23.
    assert off.TEXT_FOLLOWS_BOUNDARY is False
    assert off.HOUSE_TEXT_FOLLOWS_BOUNDARY is True
    house = _house([(6.0, 10.0), (14.0, 10.0), (14.0, 30.0), (6.0, 30.0)])
    plan = off.plan_offsets(LOT40, [house], north_deg=90.0)
    text_items = [it for it in plan.items if it.style == "text"]
    assert text_items
    assert all(math.isclose(abs(it.rotation_deg), 90.0, abs_tol=0.01) for it in text_items)


def test_house_text_is_parallel_to_its_boundary(rotation_on):
    # Дом в 6 ft от левой (вертикальной) границы: текст без выноски вдоль неё — 90°.
    house = _house([(6.0, 10.0), (14.0, 10.0), (14.0, 30.0), (6.0, 30.0)])
    plan = off.plan_offsets(LOT40, [house], north_deg=90.0)
    text_items = [it for it in plan.items if it.style == "text"]
    assert text_items, "дом должен получить текст без выноски"
    for it in text_items:
        assert math.isclose(abs(it.rotation_deg), 90.0, abs_tol=0.01)


def test_house_text_near_horizontal_boundary_is_horizontal(rotation_on):
    # Дом в 5 ft от нижней (горизонтальной) границы: текст 0°.
    house = _house([(10.0, 5.0), (22.0, 5.0), (22.0, 30.0), (10.0, 30.0)])
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
