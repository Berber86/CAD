"""Шаг 4, правила 4.24: подписи забора как у Николая — текст у линии без стрелок, тип забора в тексте,
значения столбиком.

Сравнение чертежей: у Николая все 13 подписей по заборам на 192 Geldner — обычный текст
(`FE.1.7'S`, `W.FE. 0.6'E`, `C.L.F. 0.6'S / 1.1'E`), ни одной выноски; у нас были выноски,
которые накрывали подписи типа забора и друг друга.
"""

import offsets as off


LOT = [(0.0, 0.0), (40.0, 0.0), (40.0, 40.0), (0.0, 40.0), (0.0, 0.0)]
FENCE = off.Feature(kind="fence", handle="F1", points=((6.0, 12.0), (6.0, 30.0)))


def _fence(points, handle="F1"):
    return off.Feature(kind="fence", handle=handle, points=tuple(points))


def test_fence_offset_has_no_leader():
    plan = off.plan_offsets(LOT, [FENCE], north_deg=90.0)
    items = [item for item in plan.items if item.kind == "fence"]
    assert items
    for item in items:
        assert item.style == "text", item.text
        assert item.leader is None
        assert item.text.startswith("FE.")


def test_fence_offset_does_not_repeat_the_type_text_standing_there():
    # 4.25: рядом уже стоит «C.L.F.», поэтому пишем общее «FE.» — иначе в 2 ft появляется второй «C.L.F.»
    # (у Николая тип в подписи стоит там, где он заменил старую надпись; мы чужие надписи не правим).
    texts = [("C.L.F.", (5.0, 12.0), 0.9)]
    plan = off.plan_offsets(LOT, [FENCE], north_deg=90.0, texts=texts)
    texts_out = [item.text for item in plan.items if item.kind == "fence"]
    assert texts_out
    assert texts_out[0].startswith("FE."), texts_out[0]


def test_type_prefix_can_be_switched_on(monkeypatch):
    monkeypatch.setattr(off, "TYPE_PREFIX_FT", 6.0)
    texts = [("C.L.F.", (5.0, 12.0), 0.9)]
    plan = off.plan_offsets(LOT, [FENCE], north_deg=90.0, texts=texts)
    texts_out = [item.text for item in plan.items if item.kind == "fence"]
    assert texts_out and texts_out[0].startswith("C.L.F. "), texts_out[0]


def test_fence_offset_keeps_fe_prefix_without_a_nearby_type_text():
    plan = off.plan_offsets(LOT, [FENCE], north_deg=90.0)
    texts_out = [item.text for item in plan.items if item.kind == "fence"]
    assert texts_out and texts_out[0].startswith("FE.")


def test_two_sides_go_in_a_column_with_one_prefix():
    # Две стороны одной вершины: тип один раз, значения — строками ниже (как «C.L.F. 0.6'S 1.1'E»).
    # Угол участка: вершина забора в 0.4 ft от южной и от западной стороны — две стороны одной вершины.
    fence = _fence([(0.4, 0.4), (10.0, 0.4)])
    plan = off.plan_offsets(LOT, [fence], north_deg=90.0)
    two_line = [item for item in plan.items if "\n" in item.text]
    assert two_line
    style, values = two_line[0].text.split("\n")[0], two_line[0].text.split("\n")[1:]
    assert style.count("FE.") == 1
    assert all("FE." not in line for line in values)
