"""
Пара «до оффсетов» / «завершённый» для 24 Alvine Avenue (docs/primer1i.zip, от заказчика).
Обе стадии сняты в одной системе координат (лист, 5000/5000): поворота между ними нет.
Поворот −75.394° у финальных чертежей Aspinwall/Ilion/Brooklyn — это размещение на листе перед печатью.
"""

import json
import zipfile
from pathlib import Path

import pytest

import offsets as off

ZIP = Path(__file__).resolve().parents[1] / "docs" / "primer1i.zip"
BEFORE_MARK = "перед_оффсетами"
AFTER_MARK = "ЗАВЕРШЕННЫЙ"


def _dump(mark: str) -> dict:
    with zipfile.ZipFile(ZIP) as z:
        names = [n for n in z.namelist() if mark in n and n.endswith(".final.dump.json")]
        assert len(names) == 1, names
        return json.loads(z.read(names[0]).decode("utf-8"))


@pytest.fixture(scope="module")
def pair():
    return _dump(BEFORE_MARK), _dump(AFTER_MARK)


def test_alvine_pair_is_one_frame_with_nikolai_edits(pair):
    before, after = pair
    check = off.same_frame(before, after)
    assert check.common == 278
    assert check.same == 270
    assert check.same_frame


def test_alvine_before_has_3_offsets_after_has_21(pair):
    before, after = pair
    assert len(off.parse_observed_offsets(before)) == 3
    assert len(off.parse_observed_offsets(after)) == 21


def test_alvine_added_offsets_are_18_and_split_by_height(pair):
    before, after = pair
    added = off.added_offsets(before, after)
    assert len(added) == 18
    assert sum(1 for i in added if i.height_ft == pytest.approx(1.1)) == 6
    assert sum(1 for i in added if i.height_ft == pytest.approx(0.9)) == 12


def test_alvine_house_offsets_are_1_1(pair):
    before, after = pair
    added = {i.handle: i for i in off.added_offsets(before, after)}
    house = [added[h] for h in ("260C2", "260BE")]
    assert [(p.label, p.value_ft, p.direction) for i in house for p in i.parts] == [
        ("BLDG", 2.5, "S"),
        ("BLDG", 2.6, "S"),
    ]
    assert all(i.height_ft == pytest.approx(1.1) for i in house)


def test_alvine_two_part_fence_label_keeps_its_leader(pair):
    before, after = pair
    added = {i.handle: i for i in off.added_offsets(before, after)}
    item = added["26297"]
    assert item.leader_handle == "26296"
    assert [(p.label, p.value_ft, p.direction) for p in item.parts] == [
        ("FE.", 0.4, "N"),
        ("FE.", 1.3, "W"),
    ]


def test_alvine_bare_numbers_without_label_are_kept(pair):
    # «3.2'S», «3.5'S» (1.1) и «0.6'N» (0.9) — без метки и без выноски у «3.2'S»/«3.5'S».
    before, after = pair
    added = {i.handle: i for i in off.added_offsets(before, after)}
    assert added["26283"].parts[0].label is None
    assert added["26283"].leader_handle is None
    assert added["26283"].height_ft == pytest.approx(1.1)
    assert added["26276"].parts[0].label is None
    assert added["26276"].height_ft == pytest.approx(0.9)


def test_alvine_gen_label_is_seen(pair):
    before, after = pair
    added = {i.handle: i for i in off.added_offsets(before, after)}
    assert added["262A0"].parts[0].label == "GEN."
    assert added["262A0"].parts[0].direction == "W"


def test_same_frame_still_rejects_different_systems():
    # Пара «копия» и «финал» Aspinwall не совпадает по координатам (поворот при размещении на листе).
    from pathlib import Path as _P

    docs = _P(__file__).resolve().parents[1] / "docs"
    edited = json.loads((docs / "edited for MLP 225 Aspinwall Street, Staten Island.dump.json").read_text(encoding="utf-8"))
    final = json.loads((docs / "225 Aspinwall Street, Staten Island.final.dump.json").read_text(encoding="utf-8"))
    assert not off.same_frame(edited, final).same_frame


def test_bare_text_offsets_have_their_own_style(pair):
    # Ответ заказчика (4.1): «3.2'S» без выноски — допустимая форма, для зданий и гаражей.
    before, after = pair
    added = {i.handle: i for i in off.added_offsets(before, after)}
    assert added["26283"].style == "text"
    assert added["2627E"].style == "text"
    assert added["26297"].style == "leader"


def test_front_boundary_is_m_row_line_not_widening_line(pair):
    # M-TAX-LINE — линия расширения улицы (x = 3373.39), M-ROW-LINE — фронтовая граница (x = 3368.39).
    _, after = pair
    segs = off.front_boundary_segments(after)
    assert len(segs) == 5
    vertical_front = [s for s in segs if abs(s[0][0] - 3368.3904) < 0.01 and abs(s[1][0] - 3368.3904) < 0.01]
    assert len(vertical_front) == 3
    assert not any(abs(p[0] - 3373.3904) < 0.01 for s in segs for p in s)
