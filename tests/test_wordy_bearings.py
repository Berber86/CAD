"""
Румбы словами с запятыми: «North 84 degrees, 09 minutes 22 seconds West» (SAI9262, Crystal Avenue).
До исправления регекс не видел ни одного румба: все стороны получали 0°00' и строились в одну линию.
"""

import llm_parser as lp

CRYSTAL = """BEGINNING at a point on the westerly side of Crystal Avenue, distant 221.69 feet southerly from the corner formed by the intersection of said westerly side of Crystal Avenue with the southerly side of Caswell Avenue;

RUNNING THENCE North 84 degrees, 09 minutes 22 seconds West, 100 feet to a point;

THENCE South 05 degrees, 50 minutes, 38 seconds West, 20 feet to a point;

THENCE South 84 degrees, 09 minutes, 22 seconds East, 100 feet to a point;

THENCE Northerly along the westerly side of Crystal Avenue, North 05 degrees, 50 minutes 38 seconds East 20 feet to the point or place of BEGINNING.
"""


def test_wordy_bearings_with_commas_are_read():
    matches = lp._bearing_matches(CRYSTAL)
    assert len(matches) == 4


def test_crystal_avenue_courses_are_the_rectangle_100_by_20():
    desc, warnings = lp.parse_with_regex(CRYSTAL)
    assert not warnings
    got = [(s.ns, s.degrees, s.minutes, s.seconds, s.ew, s.distance_ft) for s in desc.segments]
    assert got == [
        ("N", 84, 9, 22.0, "W", 100.0),
        ("S", 5, 50, 38.0, "W", 20.0),
        ("S", 84, 9, 22.0, "E", 100.0),
        ("N", 5, 50, 38.0, "E", 20.0),
    ]
    assert all(s.has_bearing for s in desc.segments)
    assert desc.main_street == "Crystal Avenue"
    assert desc.cross_street == "Caswell Avenue"
    assert desc.distance_to_corner_ft == 221.69


def test_crystal_avenue_plan_is_a_closed_rectangle():
    import math

    from acad_builder import AcadBuilder

    desc, _ = lp.parse_with_regex(CRYSTAL)
    plan = AcadBuilder(lp.load_config()).plan(desc)
    pts = list(plan.vertices)
    assert len(pts) == 4
    sides = [math.hypot(pts[(i + 1) % 4][0] - pts[i][0], pts[(i + 1) % 4][1] - pts[i][1]) for i in range(4)]
    assert sides[0] == sides[2] and abs(sides[0] - 100.0) < 0.05
    assert sides[1] == sides[3] and abs(sides[1] - 20.0) < 0.05
