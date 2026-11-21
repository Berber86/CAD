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
    assert candidate.propose_row((src[0], record('another.pdf', True, src[1].item))) is not None
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


def test_street_names_one_letter_with_type_direction_and_number_guards():
    import street_names
    yes = [('Lindberg Avenue', 'Lindbergh Ave'), ('Hylan Boulevard', 'Hylam Blvd'),
           ('NORTH MAIN STREET', 'NORTH MAIN ST'), ('Park Street', 'Parks Street')]
    no = [('EAST MAIN STREET', 'WEST MAIN STREET'), ('N MAIN STREET', 'S MAIN STREET'),
          ('25TH STREET', '26TH STREET'), ('MAIN AVENUE', 'MAIN STREET'),
          ('LIND BERG AVENUE', 'LINDBERGH AVENUE'), ('A ROAD', 'B ROAD'),
          ('Lindberg Avenue', 'Miller Avenue'), ('', 'MAIN STREET')]
    for a, b in yes:
        assert street_names.scene_street_match(a, b) and street_names.scene_street_match(b, a)
    for a, b in no:
        assert not street_names.scene_street_match(a, b)


def test_cross_pdf_our_neighbor_requires_same_borough_and_geometry(fake_autocad):
    src = sources()
    def in_borough(source, borough, path, role):
        data = source.item.data.model_copy(deep=True, update={'borough_or_city': borough})
        return record(path, role, drawing.DescriptionItem(data, True))
    first = in_borough(src[0], 'Staten Island, NY', 'OUR 6.pdf', True)
    second = in_borough(src[1], 'Richmond County, NY', 'LOT 5.pdf', False)
    approved, pairs = candidate.auto_place_rows((first, second))
    assert pairs == 1 and approved[0].anchor.group == approved[1].anchor.group
    builder, doc = fake_autocad(None)
    scene = drawing.plan_scene(builder, approved)
    assert [e.is_main for e in scene.plans] == [True, False]
    assert [e.plan.is_neighbor for e in scene.plans] == [False, True]
    assert all(e.placement_known for e in scene.plans)
    assert next(t.text for t in scene.plans[1].plan.items if t.kind == 'neighbor-label') == 'LOT 5'
    assert second.item.data.cross_street == 'Lindbergh Avenue'
    assert candidate.auto_place_rows((first, in_borough(src[1], 'Queens', 'LOT 5.pdf', False)))[1] == 0
    assert candidate.auto_place_rows((first, in_borough(src[1], None, 'LOT 5.pdf', False)))[1] == 0
    changed = copy.deepcopy(second.item.data)
    changed.cross_street = 'Park Avenue'
    assert candidate.auto_place_rows((first, record('LOT 5.pdf', False, drawing.DescriptionItem(changed, True))))[1] == 0


def test_duplicate_candidates_and_incomplete_coverage_are_not_automerged():
    first, second = sources()
    src = (first, second, record(first.source, True, first.item))
    result, n = candidate.auto_place_rows(src)
    assert n == 0 and all(s.anchor is None for s in result)
    result, n = candidate.auto_place_rows((first, second), eligible=[True, False])
    assert n == 0 and all(s.anchor is None for s in result)
    with pytest.raises(ValueError):
        candidate.auto_place_rows((first, second), eligible=[True])


def test_explicit_ties_corroborate_candidate_and_are_not_replaced():
    from multiple_description_runtime import anchor_for
    first, second = sources()
    approved, _ = candidate.auto_place_rows((first, second))
    both = []
    for s in approved:
        anchor = anchor_for(Path(s.source), s.item)
        both.append(drawing.DescriptionSource(Path(s.source), s.is_main, s.item, anchor))
    result, count = candidate.auto_place_rows(both)
    assert count == 1
    assert result[0].item.data.corner_to_pob_bearing == both[0].item.data.corner_to_pob_bearing
    bad = copy.deepcopy(both[1].item.data)
    tie = bad.corner_to_pob_bearing
    bad.corner_to_pob_bearing = tie.model_copy(update={'ns': 'N', 'ew': 'E'})
    invalid = drawing.DescriptionSource(both[1].source, True, drawing.DescriptionItem(bad, True), None)
    assert candidate.auto_place_rows((both[0], invalid))[1] == 0


def test_one_our_plus_neighbor_has_40ft_draft(fake_autocad):
    src = sources()
    first = record('OUR 6.pdf', True, drawing.DescriptionItem(
        src[0].item.data.model_copy(deep=True, update={'borough_or_city': 'Staten Island'}), True))
    second = record('LOT 5.pdf', False, drawing.DescriptionItem(
        src[1].item.data.model_copy(deep=True, update={'borough_or_city': 'Staten Island'}), True))
    builder, _ = fake_autocad(None)
    import multiple_description_runtime as runtime
    scene, pairs = runtime._plan_with_auto_rows(builder, (first, second))
    assert pairs == 1
    drafts = drawing.make_scene_print_drafts(scene, builder_cfg=builder.cfg)
    assert len(drafts.plans) == 1 and drafts.diagnostic is None
    tie = next(l for l in drafts.plans[0].lines if l.kind == 'main-street-row-tie')
    assert math.dist(tie.start, tie.end) == pytest.approx(40)
    assert not any(t.kind == 'main-lot-label' for t in drafts.plans[0].items)


def test_failed_auto_tie_does_not_publish_a_guessed_group(fake_autocad):
    builder, _ = fake_autocad(None)
    builder.cfg['drawing']['draw_street_extensions'] = False
    import multiple_description_runtime as runtime
    scene, count = runtime._plan_with_auto_rows(builder, sources())
    assert count == 0
    assert len(scene.plans) == 2 and all(not p.placement_known for p in scene.plans)


def test_multiple_unique_pairs_keep_separate_groups_and_roles():
    first, second = sources()
    records = []
    for borough, prefix in [('Queens', 'A'), ('Brooklyn', 'B')]:
        for index, source in enumerate((first, second)):
            model = source.item.data.model_copy(deep=True, update={'borough_or_city': borough})
            records.append(record(f'{prefix}_{index}.pdf', index == 0, drawing.DescriptionItem(model, True)))
    result, count = candidate.auto_place_rows(records)
    assert count == 2
    assert result[0].anchor.group == result[1].anchor.group
    assert result[2].anchor.group == result[3].anchor.group
    assert result[0].anchor.group != result[2].anchor.group
    assert [s.is_main for s in result] == [True, False, True, False]
