"""Склейка по карте: советник соединяет фигуры (угол к углу, общая межа), геометрия не меняется."""

from __future__ import annotations

import math

import pytest

from llm_parser import LegalDescription, Segment
from test_gui_smoke import gui as _gui_fixture  # фикстура окна для проверки кнопок

gui = _gui_fixture


def _seg(ns, degrees, minutes, seconds, ew, length):
    return Segment(ns=ns, degrees=degrees, minutes=minutes, seconds=seconds, ew=ew, distance_ft=length)


def _rect_desc(tie_distance, cross, street, sides):
    return LegalDescription(
        main_street=street, cross_street=cross, distance_to_corner_ft=tie_distance,
        corner_to_pob_bearing=_seg("N", 45, 0, 0, "E", tie_distance), segments=sides,
    )


def _rectangle_sides():
    # 100 на север, 50 на восток, 100 на юг, 50 на запад.
    return [
        _seg("N", 0, 0, 0, "E", 100), _seg("N", 90, 0, 0, "E", 50),
        _seg("S", 0, 0, 0, "E", 100), _seg("N", 90, 0, 0, "W", 50),
    ]


def _turned(seg, add_deg):
    from conflict_resolver import _azimuth, _components

    az = (_azimuth((seg.ns, seg.degrees, seg.minutes, seg.seconds, seg.ew)) + add_deg) % 360.0
    ns, degrees, minutes, seconds, ew = _components(az)
    return Segment(ns=ns, degrees=degrees, minutes=minutes, seconds=seconds, ew=ew, distance_ft=seg.distance_ft)


def _lots(neighbour_sides=None):
    from conflict_resolver import LotInput

    ours = _rect_desc(10.0, "Beta Street", "Alpha Avenue", _rectangle_sides())
    theirs = _rect_desc(20.0, "Alpha Avenue", "Beta Street", neighbour_sides or _rectangle_sides())
    return (
        LotInput(key="OUR", name="OUR", is_main=True, description=ours),
        LotInput(key="NB", name="NB", is_main=False, description=theirs),
    )


def _scene(lots):
    from acad_builder import AcadBuilder
    from conflict_resolver import _Scene
    from llm_parser import load_config

    return _Scene(AcadBuilder(load_config()), list(lots), scale=16)



def first_join(scene, ours, theirs, corner_a, side_a, corner_b, side_b, mode):
    """Первое подтверждённое соединение: (правки, причина отказа, описание)."""
    from conflict_resolver import _join_candidates

    candidates, problem = _join_candidates(
        scene, ours, theirs, corner_a, side_a, corner_b, side_b, mode,
    )
    if not candidates:
        return [], problem, ""
    changes, summary = candidates[0]
    return changes, None, summary

def _apply(scene, neighbour, changes):
    from conflict_resolver import apply_changes

    desc = apply_changes(neighbour.description, tuple(changes))
    return scene.builder.plan(desc, scale=16, is_neighbor=True)


def test_join_matches_corner_and_shared_side_without_changing_shape():

    ours, theirs = _lots()
    scene = _scene((ours, theirs))
    changes, problem, _ = first_join(scene, ours, theirs, 3, 3, 2, 1, "turn")
    assert problem is None
    plan = _apply(scene, theirs, changes)
    corner_a = scene.plan(ours).vertices[2]
    # Угол 2 соседа встал на угол 3 нашего участка.
    assert plan.vertices[1] == pytest.approx(corner_a, abs=0.01)
    # Длины и замыкание не изменились.
    assert plan.closure.distance < 0.01
    assert [round(s.distance_ft, 2) for s in apply_segments(theirs, changes)] == [100, 50, 100, 50]


def apply_segments(lot, changes):
    from conflict_resolver import apply_changes

    return apply_changes(lot.description, tuple(changes)).segments


def test_join_turns_a_turned_neighbour_into_place():

    turned = [_turned(seg, 30.0) for seg in _rectangle_sides()]
    ours, theirs = _lots(neighbour_sides=turned)
    scene = _scene((ours, theirs))
    changes, problem, summary = first_join(scene, ours, theirs, 3, 3, 2, 1, "turn")
    assert problem is None, problem
    assert "+30.0000°" in summary
    plan = _apply(scene, theirs, changes)
    assert plan.vertices[1] == pytest.approx(scene.plan(ours).vertices[2], abs=0.01)
    assert plan.closure.distance < 0.01
    # Сторона соседа снова идёт противоположно нашей общей стороне: поворот совпал с геометрией.
    from conflict_resolver import judge

    before = scene.metrics(theirs, (), state={})
    after = scene.metrics(theirs, tuple(changes), state={})
    assert judge(before, after) == "verified"


def test_shift_only_refuses_a_turned_neighbour():

    turned = [_turned(seg, 30.0) for seg in _rectangle_sides()]
    ours, theirs = _lots(neighbour_sides=turned)
    scene = _scene((ours, theirs))
    changes, problem, _ = first_join(scene, ours, theirs, 3, 3, 2, 1, "shift")
    assert changes == []
    assert "при переносе без поворота" in problem


def test_join_refuses_sides_that_do_not_share_a_boundary():
    from conflict_resolver import advisor_verdict, _join_candidates

    ours, theirs = _lots()
    scene = _scene((ours, theirs))
    # Сосед приставлен своей северной стороной к углу 3 — лежит на продолжении нашей южной, без общей части.
    # Варианты, которые геометрия контуров пропустит (например, переворот на 180°), обязаны
    # провалиться по наложению: ни один кандидат не может стать подтверждённым соединением.
    candidates, _ = _join_candidates(scene, ours, theirs, 3, 3, 1, 1, "turn")
    before = scene.metrics(theirs, (), state={})
    for changes, _summary in candidates:
        after = scene.metrics(theirs, tuple(changes), state={})
        assert advisor_verdict(before, after, allow_stepped=True) == "rejected"
    shift, problem = _join_candidates(scene, ours, theirs, 3, 3, 1, 1, "shift")
    assert shift == [] or all(
        advisor_verdict(before, scene.metrics(theirs, tuple(c), state={}), allow_stepped=True) == "rejected"
        for c, _ in shift
    )


def test_read_join_reply_keeps_only_joins():
    from conflict_resolver import read_join_reply

    reply = read_join_reply({
        "analysis": "общая межа — сторона 3",
        "joins": [{"lot_a": "L1", "corner_a": 3, "lot_b": "L2", "corner_b": 2, "side_a": 3, "side_b": 1}],
        "proposals": [{"lot": "L1", "changes": [{"target": "tie", "distance_ft": 99}]}],
        "explanations": [{"conflict": "x", "cause": "y"}],
    })
    assert reply.analysis == "общая межа — сторона 3"
    assert len(reply.proposals) == 1 and reply.proposals[0]["corner_a"] == 3
    assert reply.explanations == []
    assert read_join_reply("не объект").proposals == []


def test_join_prompt_asks_for_joins_only():
    from conflict_resolver import JOIN_SYSTEM_PROMPT

    assert "YOUR ONLY TASK" in JOIN_SYSTEM_PROMPT
    assert "cross_street" not in JOIN_SYSTEM_PROMPT
    assert "street_alias" not in JOIN_SYSTEM_PROMPT
    assert "\"joins\"" in JOIN_SYSTEM_PROMPT


def test_sketch_is_a_png_data_url_with_numbered_figures():
    from join_sketch import as_data_url, render_sketch

    square = [(0.0, 0.0), (0.0, 100.0), (50.0, 100.0), (50.0, 0.0)]
    png = render_sketch([("L1", "OUR", True, square), ("L2", "NB", False, square)])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert as_data_url(png).startswith("data:image/png;base64,")


def test_analyse_join_round_uses_the_advisor_and_verifies_the_join():
    from acad_builder import AcadBuilder
    from conflict_resolver import analyse_conflicts
    from llm_parser import load_config

    ours, theirs = _lots()
    seen: dict = {}

    def advisor(system, user_text, images):
        seen["system"] = system
        seen["images"] = list(images)
        return {
            "analysis": "общая межа — сторона 3 нашего участка и сторона 1 соседа",
            "joins": [{
                "lot_a": "L1", "corner_a": 3, "lot_b": "L2", "corner_b": 2, "side_a": 3, "side_b": 1,
                "reason": "стороны совпадают", "source_quote": "",
            }],
        }, None

    analysis = analyse_conflicts(
        AcadBuilder(load_config()), [ours, theirs], scale=16, advisor=advisor, force_advisor=True,
        join_mode="turn", join_images=("data:image/jpeg;base64,AAAA",),
    )
    assert analysis.join_mode == "turn"
    assert analysis.advisor_status == "ok"
    assert seen["system"].startswith("You are a land surveyor's assistant")
    # Первая картинка — схема; последняя — карта, приложенная пользователем.
    assert seen["images"][0].startswith("data:image/png;base64,")
    assert seen["images"][-1] == "data:image/jpeg;base64,AAAA"
    joins = [p for p in analysis.proposals if p.kind == "advisor"]
    assert len(joins) == 1
    assert joins[0].verified
    assert joins[0].lot_key == "NB"
    assert any("длины и форма соседа не меняются" in r for r in joins[0].reasons)


def test_join_round_rejects_neighbour_to_neighbour_and_duplicates():
    from conflict_resolver import AdvisorReply, _join_proposals

    ours, theirs = _lots()
    scene = _scene((ours, theirs))
    labels = {"L1": ours, "L2": theirs}
    reply = AdvisorReply(proposals=[
        {"lot_a": "L2", "corner_a": 2, "lot_b": "L2", "corner_b": 2, "side_a": 1, "side_b": 1},
        {"lot_a": "L1", "corner_a": 3, "lot_b": "L2", "corner_b": 2, "side_a": 3, "side_b": 1},
        {"lot_a": "L1", "corner_a": 3, "lot_b": "L2", "corner_b": 2, "side_a": 3, "side_b": 1},
    ])
    accepted, rejected = _join_proposals(scene, reply, labels, "turn", ())
    assert len(accepted) == 1
    assert any("соединяем только с нашим участком" in text for text in rejected)
    assert any("соседу уже назначено соединение" in text for text in rejected)


def test_join_placement_uses_ours_as_frame_for_the_neighbour_tie():

    ours, theirs = _lots()
    scene = _scene((ours, theirs))
    changes, problem, _ = first_join(scene, ours, theirs, 3, 3, 2, 1, "turn")
    assert problem is None
    tie = changes[0]
    assert tie.target == "tie" and tie.bearing_set
    # Расстояние до POB соседа — от нашего угла, а не от его собственного (прежнее было 20 ft).
    assert tie.distance_ft == pytest.approx(math.hypot(*_plan_pob(scene, ours, theirs, changes)), abs=0.01)


def _plan_pob(scene, ours, theirs, changes):
    return _apply(scene, theirs, changes).vertices[0]


def test_join_button_settings_and_auto_accept_path(gui, monkeypatch):
    """GUI: режим переключается; подтверждённое соединение по карте ставится в очередь автоприменения;
    ответ по карте не попадает в кэш обычного ответа советника."""
    from conflict_resolver import AdvisorReply, ConflictAnalysis, Metrics, Proposal

    app = gui.SurveyApp()
    assert app._pdf_set_join_mode == "turn"
    app._set_pdf_set_join_mode("только перенос")
    assert app._pdf_set_join_mode == "shift"
    app._set_pdf_set_join_mode("перенос и поворот")
    assert app._pdf_set_join_mode == "turn"

    metrics = Metrics(closure_ft=0.0, overlap_sqft=0.0, edge_errors=0, edge_warnings=0, gap_ft=0.0)
    proposal = Proposal(
        id="join-1", lot_key="NB", lot_name="NB", kind="advisor", origin="ai", title="Склейка",
        changes=(), source="ИИ-советник по карте", reasons=(), before=metrics, after=metrics,
        verdict="verified", requires=(),
    )
    analysis = ConflictAnalysis(
        proposals=[proposal], join_mode="turn", advisor_status="ok",
        advisor_reply=AdvisorReply(analysis="соединение"), advisor_key="key-1",
    )
    app._handle_pdf_set_done(([], analysis))
    assert app._pdf_set_advice == {}
    assert [p.id for p in app._pending_join_accept] == ["join-1"]

    accepted: list[list] = []
    monkeypatch.setattr(app, "_accept_pdf_set_fixes", lambda proposals: accepted.append(list(proposals)))
    app._handle_event("done", None)
    assert app._pending_join_accept == []
    assert [[p.id for p in batch] for batch in accepted] == [["join-1"]]


def test_join_finds_the_same_direction_fit_for_an_opposite_winding_neighbour():
    """
    Сосед обойден против часовой (как LOT 47), наш участок — по часовой. Общая межа идёт
    в одну сторону; короткая сторона соседа (88) внутри нашей (100), остаток — уступ задней межи.
    """
    from conflict_resolver import _join_candidates, advisor_verdict

    ours_sides = [
        _seg("S", 56, 36, 12, "E", 100), _seg("S", 33, 23, 48, "W", 100),
        _seg("N", 56, 36, 12, "W", 100), _seg("N", 33, 23, 48, "E", 100),
    ]
    nb_sides = [
        _seg("S", 33, 23, 48, "W", 88), _seg("S", 56, 36, 12, "E", 130),
        _seg("N", 33, 23, 48, "E", 88), _seg("N", 56, 36, 12, "W", 130),
    ]
    ours = _lots()[0]
    ours = type(ours)(key="OUR", name="OUR", is_main=True,
                      description=_rect_desc(10.0, "Alpha Avenue", "Beta Street", ours_sides))
    theirs = type(ours)(key="NB", name="NB", is_main=False,
                        description=_rect_desc(20.0, "Beta Street", "Alpha Avenue", nb_sides))
    scene = _scene((ours, theirs))

    candidates, problem = _join_candidates(scene, ours, theirs, 2, 2, 1, 1, "turn")
    assert problem is None, problem
    assert candidates
    changes, summary = candidates[0]
    assert "в одном направлении" in summary
    before = scene.metrics(theirs, (), state={})
    after = scene.metrics(theirs, tuple(changes), state={})
    assert after.overlap_sqft < 0.5
    assert after.stepped_edges == 1
    assert after.closure_ft < 0.01
    assert advisor_verdict(before, after, allow_stepped=True) == "verified"
    assert advisor_verdict(before, after) == "partial"


@pytest.mark.parametrize("neighbour_side_ft, accepted", [(30.0, False), (45.0, True)])
def test_shared_boundary_must_cover_forty_percent_of_the_longer_side(neighbour_side_ft, accepted):
    """Наша сторона 100 ft: общая межа 30 ft (30%) отклоняется, 45 ft (45%) принимается."""
    from conflict_resolver import _join_candidates

    ours_sides = [
        _seg("S", 56, 36, 12, "E", 100), _seg("S", 33, 23, 48, "W", 100),
        _seg("N", 56, 36, 12, "W", 100), _seg("N", 33, 23, 48, "E", 100),
    ]
    nb_sides = [
        _seg("S", 33, 23, 48, "W", neighbour_side_ft), _seg("S", 56, 36, 12, "E", 130),
        _seg("N", 33, 23, 48, "E", neighbour_side_ft), _seg("N", 56, 36, 12, "W", 130),
    ]
    base = _lots()[0]
    ours = type(base)(key="OUR", name="OUR", is_main=True,
                      description=_rect_desc(10.0, "Alpha Avenue", "Beta Street", ours_sides))
    theirs = type(base)(key="NB", name="NB", is_main=False,
                        description=_rect_desc(20.0, "Beta Street", "Alpha Avenue", nb_sides))
    scene = _scene((ours, theirs))
    candidates, problem = _join_candidates(scene, ours, theirs, 2, 2, 1, 1, "turn")
    if accepted:
        assert candidates and problem is None
    else:
        assert candidates == []
        assert "нужно не меньше 40.00 ft" in problem
