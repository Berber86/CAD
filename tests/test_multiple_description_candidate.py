"""Offline review proposal tests using the user-supplied model result."""
import copy
import math
from pathlib import Path

import pytest

import acad_builder as ab
import llm_parser
import multiple_description_candidate as candidate
import multiple_description_drawing as drawing
from test_multiple_description_drawing import deed_example_items, record, _vector
from test_print_draft import fake_autocad  # noqa: F401


def sources():
    return tuple(record('OUR 6 NEW.pdf', True, i) for i in deed_example_items())


def test_proposal_requires_confirmation_and_preserves_original_data(fake_autocad):
    src = sources()
    proposal = candidate.propose_row(src)
    assert proposal is not None
    assert (proposal.near_index, proposal.far_index) == (0, 1)
    assert (proposal.near_cross, proposal.far_cross) == ('Lindberg Avenue', 'Lindbergh Avenue')
    assert proposal.shared_length == 96.58
    assert proposal.tie_label == 'S 37°31\'21" W'
    builder, doc = fake_autocad(None)
    unconfirmed = drawing.plan_scene(builder, src)
    assert all(not e.placement_known and not e.plan.lines for e in unconfirmed.plans)
    accepted = candidate.accept_row(src, proposal)
    assert all(i.item.data.corner_to_pob_bearing is None for i in src)
    assert src[1].item.data.cross_street == 'Lindbergh Avenue'
    scene = drawing.plan_scene(builder, accepted)
    assert len(scene.plans) == 2 and all(e.placement_known for e in scene.plans)
    first, second = [e.plan for e in scene.plans]
    assert math.dist(first.pob, second.pob) == pytest.approx(44.69)
    assert math.dist(first.boundary_lines[2].end, second.boundary_lines[0].start) < .02
    assert math.dist(first.boundary_lines[2].start, second.boundary_lines[0].end) < .02
    for plan, item in zip((first, second), src):
        assert plan.open_traverse and len(plan.boundary_lines) == 4
        for edge, segment in zip(plan.boundary_lines, item.item.data.segments):
            assert _vector(edge) == pytest.approx(segment.vector, abs=1e-6)
        builder.draw_plan(plan)
    assert any('LINDBERG' in text.TextString for text in doc.ModelSpace.texts)
    assert sum('HYLAN' in text.TextString for text in doc.ModelSpace.texts) == 1
    assert not doc.ModelSpace.polylines
    draft = drawing.make_scene_print_drafts(scene, builder_cfg=builder.cfg)
    assert draft.diagnostic is None and len(draft.plans) == 2
    ties = [ln for plan in draft.plans for ln in plan.lines if ln.kind == 'main-street-row-tie']
    assert len(ties) == 1 and math.dist(ties[0].start, ties[0].end) == pytest.approx(40)
    assert all(not any(t.kind == 'main-lot-label' for t in p.items) for p in draft.plans)
    for plan in draft.plans:
        builder.draw_plan(plan)
    assert not doc.ModelSpace.polylines


def test_candidate_fails_closed_on_conflicting_or_ambiguous_input():
    src = sources()
    assert candidate.propose_row(src[:1]) is None
    assert candidate.propose_row(src + (src[0],)) is None
    assert candidate.propose_row((src[0], record('another.pdf', True, src[1].item))) is None
    assert candidate.propose_row((src[0], record(src[1].source, False, src[1].item))) is None
    for field, value in [('cross_street', None), ('main_street', 'Different'),
                         ('distance_to_corner_ft', 120), ('front_segment_index', 0),
                         ('cross_street_side_index', 1)]:
        changed = copy.deepcopy(src[1].item.data)
        setattr(changed, field, value)
        changed_item = drawing.DescriptionItem(changed, True)
        assert candidate.propose_row((src[0], record(src[1].source, True, changed_item))) is None
    changed = copy.deepcopy(src[1].item.data)
    changed.segments[0].distance_ft += .5
    assert candidate.propose_row((src[0], record(src[1].source, True, drawing.DescriptionItem(changed, True)))) is None


def test_stale_approval_is_rejected():
    src = sources()
    proposal = candidate.propose_row(src)
    with pytest.raises(ValueError, match='stale'):
        candidate.accept_row(src[::-1], proposal)
