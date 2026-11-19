"""AutoCAD callouts for automatic data changes, independent from log wording."""
from __future__ import annotations

import copy
import math

import pytest

import acad_builder as ab
import drawing_warnings as dw
import llm_parser as lp
from test_clear_typo_autofix import RECT
from test_print_draft import fake_autocad, _Entity  # noqa: F401


def corrected_text():
    return RECT.replace("North 31 degrees 46 minutes 56 seconds west", "North 81 degrees 46 minutes 56 seconds west")


def corrected_plan():
    out = lp.parse_legal_description_full(text=corrected_text(), use_llm=False, ask_on_doubt=True)
    return out, ab.AcadBuilder(lp.load_config()).plan(out.data)


def callouts(plan):
    return dw.plan_callouts(plan, ab.segment_distance_label)


def test_parser_records_actual_change_and_draws_full_side_with_red_digit():
    out, plan = corrected_plan()
    assert out.data.segments[0].degrees == 31
    assert out.data.segments[0]._auto_corrections[0].before.degrees == 81
    assert "_auto_corrections" not in out.data.model_dump()["segments"][0]
    notes, issues = callouts(plan)
    assert not issues and len(notes) == 1
    note = notes[0]
    assert note.side_index == 0
    assert note.plain_text == 'N 31°46\'56" W 25.00\''
    assert note.text == 'N {\\C1;3}1%%d46\'56" W 25.00\''
    edge = plan.boundary_lines[0]
    assert note.tip == pytest.approx(((edge.start[0] + edge.end[0])/2, (edge.start[1] + edge.end[1])/2))
    assert math.dist(note.tip, note.text_point) >= 4 * note.height - 1e-7


def test_length_red_digit_and_unmodified_feet_inches_source():
    text = RECT.replace("North 31 degrees 46 minutes 56 seconds west 25.00 feet", "North 31 degrees 46 minutes 56 seconds west 28.00 feet")
    out = lp.parse_legal_description_full(text=text, use_llm=False, ask_on_doubt=True)
    plan = ab.AcadBuilder(lp.load_config()).plan(out.data)
    note, = callouts(plan)[0]
    assert note.text.endswith("2{\\C1;5}.00'")
    assert note.plain_text.endswith("25.00'")
    original = out.data.segments[0]
    before = original.model_copy(update={"degrees": 81, "distance_source": "25 feet 0 inches"})
    before._auto_corrections = []
    after = original.model_copy(update={"distance_source": "25 feet 0 inches"})
    marked = dw.mark_auto_correction(before, after)
    mtext, plain = dw.corrected_side_text(marked, marked._auto_corrections[0], ab.segment_distance_label)
    assert "{\\C1;3}1" in mtext
    assert ab.segment_distance_label(marked) in plain


def test_minutes_seconds_and_quadrant_changes_highlight_only_changed_glyphs():
    base = lp.Segment(ns="N", degrees=31, minutes=46, seconds=56, ew="W", distance_ft=25)
    for updated, red in [({"minutes": 49}, "{\\C1;9}"), ({"seconds": 59.0}, "{\\C1;9}"),
                         ({"ns": "S"}, "{\\C1;S}"), ({"ew": "E"}, "{\\C1;E}")]:
        corrected = dw.mark_auto_correction(base, base.model_copy(update=updated))
        marked, plain = dw.corrected_side_text(corrected, corrected._auto_corrections[0], ab.segment_distance_label)
        assert red in marked and marked.count("\\C1;") == 1
        assert "25.00'" in plain


def test_no_callout_for_clean_neighbor_draft_or_manual_candidate():
    clean, _ = lp.parse_with_regex(RECT, auto_repair_typos=False)
    builder = ab.AcadBuilder(lp.load_config())
    assert not callouts(builder.plan(clean))[0]
    out, plan = corrected_plan()
    assert not callouts(builder.plan(out.data, is_neighbor=True))[0]
    assert not callouts(ab.make_print_draft(plan, {}))[0]
    base = clean.model_copy(deep=True)
    base.segments[0] = lp._mutate_segment_bearing(base.segments[0], degrees=81)
    candidate = lp._scan_clear_lookalike_typo(base.segments)
    assert candidate and candidate.repaired_segments
    manually_selected = base.model_copy(update={"segments": candidate.repaired_segments})
    assert not callouts(builder.plan(manually_selected))[0]


def test_translation_deepcopy_and_changed_side():
    _, plan = corrected_plan()
    note = callouts(plan)[0][0]
    shifted = ab.translate_plan(copy.deepcopy(plan), 100, 200)
    translated = callouts(shifted)[0][0]
    assert translated.tip == pytest.approx((note.tip[0] + 100, note.tip[1] + 200))
    altered = copy.deepcopy(plan)
    altered.description.segments[0].degrees += 1
    found, warnings = callouts(altered)
    assert not found and "изменилось" in warnings[0]


def test_com_draw_has_attached_white_leader_and_red_digit_on_separate_layer(fake_autocad):
    out, plan = corrected_plan()
    builder, doc = fake_autocad(plan)
    mtexts, leaders = [], []
    def add_mtext(point, width, text):
        entity = _Entity(TextString=text, InsertionPoint=point, Width=width)
        mtexts.append(entity)
        return entity
    def add_leader(coords, annotation, kind):
        entity = _Entity(Coordinates=coords, Annotation=annotation, Type=kind)
        leaders.append(entity)
        return entity
    doc.ModelSpace.AddMText = add_mtext
    doc.ModelSpace.AddLeader = add_leader
    result = builder.draw_plan(plan)
    assert not result.warnings or not any("пометк" in w for w in result.warnings)
    assert len(mtexts) == len(leaders) == 1
    assert mtexts[0].Layer == leaders[0].Layer == dw.LAYER
    assert mtexts[0].Color == leaders[0].Color == 7
    assert mtexts[0].TextString == 'N {\\C1;3}1%%d46\'56" W 25.00\''
    assert leaders[0].Annotation is mtexts[0]
    tip = callouts(plan)[0][0].tip
    assert leaders[0].Coordinates[:2] == pytest.approx([tip[0] + 5000, tip[1] + 5000])
    builder.draw_plan(ab.make_print_draft(plan, {}))
    builder.draw_plan(builder.plan(out.data, is_neighbor=True))
    assert len(mtexts) == len(leaders) == 1


def test_callout_failure_is_not_silent(fake_autocad):
    _, plan = corrected_plan()
    builder, doc = fake_autocad(plan)
    def fail(*args):
        raise RuntimeError("no mtext")
    doc.ModelSpace.AddMText = fail
    result = builder.draw_plan(plan)
    assert any("пометку автоисправления" in w and "no mtext" in w for w in result.warnings)


def test_auto_fix_record_survives_unrelated_confirmed_conflict_change():
    from conflict_resolver import Change, apply_changes
    out, _ = corrected_plan()
    description = apply_changes(out.data, [Change(target="tie", index=0, field="distance", distance_ft=20)])
    assert description.segments[0]._auto_corrections
    plan = ab.AcadBuilder(lp.load_config()).plan(description)
    assert len(callouts(plan)[0]) == 1


def test_different_unrelated_value_on_side_does_not_hide_corrected_digit():
    out, _ = corrected_plan()
    out.data.segments[0].distance_ft = 24.0
    plan = ab.AcadBuilder(lp.load_config()).plan(out.data)
    notes, warnings = callouts(plan)
    assert not warnings and len(notes) == 1
    assert "{\\C1;3}1" in notes[0].text and notes[0].plain_text.endswith("24.00'")


def test_consensus_retains_automatic_correction_for_selected_result(monkeypatch):
    raw, _ = lp.parse_with_regex(RECT, auto_repair_typos=False)
    raw.segments[0] = lp._mutate_segment_bearing(raw.segments[0], degrees=81)
    usage = lp.TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2,
                          provider="hydra", model="test", available=True)
    monkeypatch.setattr(lp, "_extract_with_llm", lambda *a, **k: (raw.model_copy(deep=True), "structured-outputs", usage))
    targets = [lp.PassTarget(pass_index=1, provider="hydra", model="test", api_key="x"),
               lp.PassTarget(pass_index=2, provider="openrouter", model="test", api_key="x")]
    consensus = lp.run_multipass_consensus(text=corrected_text(), cfg=lp.load_config(), targets=targets, ask_on_doubt=True)
    assert consensus.agreed and consensus.selected_outcome.data.segments[0].degrees == 31
    plan = ab.AcadBuilder(lp.load_config()).plan(consensus.selected_outcome.data)
    assert callouts(plan)[0][0].text.startswith('N {\\C1;3}1')


def test_geometry_error_in_annotation_does_not_remove_main_boundary(fake_autocad):
    _, plan = corrected_plan()
    builder, doc = fake_autocad(plan)
    def fail(*args):
        raise RuntimeError("no mtext")
    doc.ModelSpace.AddMText = fail
    result = builder.draw_plan(plan)
    assert result.boundary_line_count == len(plan.boundary_lines)
    assert any("пометку автоисправления" in message for message in result.warnings)


def test_callout_leader_falls_back_to_polyline(fake_autocad):
    _, plan = corrected_plan()
    builder, doc = fake_autocad(plan)
    mtexts = []
    def add_mtext(point, width, text):
        obj = _Entity(InsertionPoint=point, TextString=text)
        mtexts.append(obj)
        return obj
    doc.ModelSpace.AddMText = add_mtext
    result = builder.draw_plan(plan)
    assert len(mtexts) == len(doc.ModelSpace.polylines) == 1
    assert doc.ModelSpace.polylines[0].Layer == dw.LAYER
    assert doc.ModelSpace.polylines[0].Color == 7
    assert any("полилинией" in warning for warning in result.warnings)
    assert result.layers["risk_warnings"] == dw.LAYER
