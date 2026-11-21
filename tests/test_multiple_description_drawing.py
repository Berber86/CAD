"""Offline planning and fake-AutoCAD rendering of partial boundary chains."""
import copy
import math

import pytest

import acad_builder as ab
import llm_parser
import multiple_description_drawing as drawing
import multiple_description_parse as parse
from test_multiple_description_parse import answer, item
from test_print_draft import fake_autocad  # noqa: F401 -- shared AutoCAD fixture


def partial(count=2, lot='5'):
    entry = item(lot, complete=False)
    entry['data']['segments'] = entry['data']['segments'][:count]
    result = parse.read_response(answer([entry], complete=False), [2])
    assert result.valid_response and len(result.items) == 1
    return result.items[0]


def test_one_and_two_known_sides_are_open_with_bbox_label():
    builder = ab.AcadBuilder(llm_parser.load_config())
    for count in (1, 2):
        original = partial(count)
        plan = drawing.plan_partial(builder, original, source='OUR 6 NEW.pdf', is_main=True, start=(5, 8))
        assert plan.open_traverse and len(plan.boundary_lines) == count and len(plan.vertices) == count + 1
        assert len(plan.description.segments) == count and plan.area_sqft == 0
        assert plan.items[-1].text == 'LOT 6'
        assert plan.description.lot_number == '5'  # deed identity is not replaced
        xs, ys = zip(*plan.vertices)
        assert plan.items[-1].point == pytest.approx(((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2))
        assert any(x.kind.startswith('side-') for x in plan.items)
        assert plan.items[-1].kind == 'main-lot-label'
        assert len(plan.boundary_lines) == len(original.data.segments)


def test_neighbor_uses_filename_then_description_then_question_mark():
    builder = ab.AcadBuilder(llm_parser.load_config())
    plan = drawing.plan_partial(builder, partial(), source='LOT 8 NEW.pdf', is_main=False)
    assert plan.is_neighbor and plan.items[-1].text == 'LOT 8'
    plan = drawing.plan_partial(builder, partial(), source='document.pdf', is_main=False)
    assert plan.items[-1].text == 'LOT 5'
    unknown = partial()
    unknown = parse.DescriptionItem(unknown.data.model_copy(update={'lot_number': None}), False)
    plan = drawing.plan_partial(builder, unknown, source='document.pdf', is_main=False)
    assert plan.items[-1].text == 'LOT ?'
    assert [i.kind for i in plan.items] == ['neighbor-label']


@pytest.mark.parametrize('is_main', [True, False])
@pytest.mark.parametrize('as_lines', [True, False])
def test_fake_autocad_never_closes_partial_even_in_polyline_mode(fake_autocad, is_main, as_lines):
    # The plan is drawn to a fake COM document; no real AutoCAD is contacted.
    builder, doc = fake_autocad(None)
    builder.cfg['drawing']['property_as_lines'] = as_lines
    plan = drawing.plan_partial(builder, partial(), source='OUR 6 NEW.pdf', is_main=is_main)
    result = builder.draw_plan(plan)
    assert result.boundary_line_count == 2
    assert len(doc.ModelSpace.lines) == 2
    assert not doc.ModelSpace.polylines
    assert all(line.Layer == ('PL' if is_main else 'NEIGHBOR_LOT') for line in doc.ModelSpace.lines)
    assert any(text.TextString == 'LOT 6' for text in doc.ModelSpace.texts)
    assert not any('фасад' in warning.lower() for warning in result.warnings)


def test_partial_translation_keeps_every_side_and_bbox_label():
    builder = ab.AcadBuilder(llm_parser.load_config())
    plan = drawing.plan_partial(builder, partial(), source='OUR 6 NEW.pdf', is_main=True)
    moved = ab.translate_plan(plan, 123, -45)
    assert moved.open_traverse and moved.area_sqft == 0
    assert moved.centroid == pytest.approx((plan.centroid[0] + 123, plan.centroid[1] - 45))
    for orig, new in zip(plan.boundary_lines, moved.boundary_lines):
        assert math.dist(orig.start, orig.end) == pytest.approx(math.dist(new.start, new.end))
    assert moved.items[-1].point == pytest.approx((plan.items[-1].point[0] + 123, plan.items[-1].point[1] - 45))


def test_partial_planner_rejects_complete_and_invalid_placement():
    builder = ab.AcadBuilder(llm_parser.load_config())
    v = partial()
    with pytest.raises(ValueError):
        drawing.plan_partial(builder, parse.DescriptionItem(v.data, True), source='x.pdf', is_main=True)
    with pytest.raises(ValueError):
        drawing.plan_partial(builder, v, source='x.pdf', is_main=True, start=(float('nan'), 0))


def full(lot='5'):
    result = parse.read_response(answer([item(lot)], complete=True), [2, 3])
    return result.items[0]


def record(path, is_main, value, group=None, pos=(0.0, 0.0)):
    anchor = drawing.Anchor(group, pos) if group is not None else None
    return drawing.DescriptionSource(source=path, is_main=is_main, item=value, anchor=anchor)


def _vector(edge):
    return edge.end[0] - edge.start[0], edge.end[1] - edge.start[1]


def test_scene_keeps_both_our_descriptions_and_filename_labels():
    builder = ab.AcadBuilder(llm_parser.load_config())
    entries = [record('OUR 6 NEW.pdf', True, full('5'), 'intersection', (55.42, 0)),
               record('OUR 6 NEW.pdf', True, full('6'), 'intersection', (100.11, 0))]
    result = drawing.plan_scene(builder, entries)
    assert not result.failures and len(result.plans) == 2
    assert [p.plan.description.lot_number for p in result.plans] == ['5', '6']
    assert all(not p.plan.is_neighbor for p in result.plans)
    assert all(any(t.text == 'LOT 6' and t.kind == 'main-lot-label' for t in p.plan.items)
               for p in result.plans)
    assert result.plans[1].plan.pob[0] - result.plans[0].plan.pob[0] == pytest.approx(44.69)
    assert all(any(t.kind.startswith('side-') for t in p.plan.items) for p in result.plans)


def test_scene_duplicates_and_different_roles_are_not_suppressed():
    builder = ab.AcadBuilder(llm_parser.load_config())
    records = [record('OUR 6 NEW.pdf', True, full('5'), 'intersection', (50, 0)),
               record('LOT 5 NEW.pdf', False, full('5'), 'intersection', (50, 0))]
    result = drawing.plan_scene(builder, records)
    assert len(result.plans) == 2 and not result.failures
    a, b = result.plans
    for first, second in zip(a.plan.boundary_lines, b.plan.boundary_lines):
        assert _vector(first) == pytest.approx(_vector(second))
    assert a.plan.pob == pytest.approx(b.plan.pob)
    assert not a.plan.is_neighbor and b.plan.is_neighbor
    assert any(t.text == 'LOT 6' for t in a.plan.items)
    assert any(t.text == 'LOT 5' for t in b.plan.items)


def test_unlocated_contours_are_separate_and_shape_never_changes():
    builder = ab.AcadBuilder(llm_parser.load_config())
    values = [record('OUR 6.pdf', True, full('5'), 'known', (0, 0)),
              record('OUR 6.pdf', True, full('6')),
              record('LOT 2.pdf', False, partial(2))]
    result = drawing.plan_scene(builder, values, separation_ft=100)
    assert len(result.plans) == 3 and not result.failures
    assert [p.placement_known for p in result.plans] == [True, False, False]
    for original, planned in zip(values, result.plans):
        for edge, seg in zip(planned.plan.boundary_lines, original.item.data.segments):
            assert _vector(edge) == pytest.approx(seg.vector, abs=1e-6)
    bounds = [drawing._bounds([v.plan]) for v in result.plans]
    assert bounds[1][0] >= bounds[0][2] + 100 - 1e-6
    assert bounds[2][0] >= bounds[1][2] + 100 - 1e-6
    assert result.plans[2].plan.open_traverse
    assert result.plans[1].diagnostic == 'unresolved_position'


def test_exact_shared_street_items_are_deduplicated_only_for_known_group():
    builder = ab.AcadBuilder(llm_parser.load_config())
    records = [record('OUR 6.pdf', True, full('5'), 'known', (0, 0)),
               record('OUR 6.pdf', True, full('6'), 'known', (0, 0))]
    scene = drawing.plan_scene(builder, records)
    a, b = [p.plan for p in scene.plans]
    assert len(a.boundary_lines) == len(b.boundary_lines) == 4
    # Street labels are auxiliary; the actual parcel labels are kept for both.
    assert sum(i.kind == 'main-street' for p in (a, b) for i in p.items) <= 1
    assert sum(i.kind == 'main-lot-label' for p in (a, b) for i in p.items) == 2


def test_legacy_geometry_repair_never_changes_recorded_sides(monkeypatch):
    builder = ab.AcadBuilder(llm_parser.load_config())
    original = ab.AcadBuilder.plan
    def altered(self, description, *args, **kwargs):
        plan = original(self, description, *args, **kwargs)
        edge = plan.boundary_lines[0]
        plan.boundary_lines[0] = ab.LineItem(edge.start, (edge.end[0] + 3, edge.end[1]), edge.kind)
        return plan
    monkeypatch.setattr(ab.AcadBuilder, 'plan', altered)
    source = record('OUR 6.pdf', True, full())
    scene = drawing.plan_scene(builder, [source])
    result = scene.plans[0]
    assert result.plan.open_traverse and result.diagnostic == 'exact_sides_fallback'
    for edge, seg in zip(result.plan.boundary_lines, source.item.data.segments):
        assert _vector(edge) == pytest.approx(seg.vector, abs=1e-6)


def test_one_description_failure_does_not_drop_other_plans(monkeypatch):
    builder = ab.AcadBuilder(llm_parser.load_config())
    original = drawing._plan_description
    def sometimes_fail(builder, source, scale):
        if source.source == 'bad.pdf':
            raise ValueError('sensitive provider data')
        return original(builder, source, scale)
    monkeypatch.setattr(drawing, '_plan_description', sometimes_fail)
    result = drawing.plan_scene(builder, [record('bad.pdf', True, full()), record('OUR 6.pdf', True, full())])
    assert len(result.plans) == 1 and result.plans[0].description_index == 1
    assert len(result.failures) == 1 and result.failures[0].reason == 'planning_failed'


def test_scene_fake_autocad_draws_all_contours_without_dropping_duplicates(fake_autocad):
    builder, doc = fake_autocad(None)
    entries = [record('OUR 6 NEW.pdf', True, full('5'), 'intersection', (0, 0)),
               record('OUR 6 NEW.pdf', True, full('6'), 'intersection', (10, 0)),
               record('LOT 5 NEW.pdf', False, full('5'), 'intersection', (0, 0))]
    scene = drawing.plan_scene(builder, entries)
    results = [builder.draw_plan(entry.plan) for entry in scene.plans]
    assert len(results) == 3
    assert [r.boundary_line_count for r in results] == [4, 4, 4]
    text = [t.TextString for t in doc.ModelSpace.texts]
    assert text.count('LOT 6') == 2 and text.count('LOT 5') == 1
    assert not scene.failures


def test_common_row_draft_has_one_40ft_intersection_and_no_our_numbers():
    from pathlib import Path
    model, _ = llm_parser.parse_with_regex(
        Path('tests/data/lot72_render_e600.txt').read_text(encoding='utf-8'), auto_repair_typos=False)
    second = model.model_copy(deep=True, update={'distance_to_corner_ft': model.distance_to_corner_ft + 40})
    first_item = parse.DescriptionItem(model, True)
    second_item = parse.DescriptionItem(second, True)
    cfg = llm_parser.load_config()
    builder = ab.AcadBuilder(cfg)
    first_pos = ab.compute_pob_point(model)
    second_pos = ab.compute_pob_point(second)
    records = [record('OUR 6.pdf', True, first_item, 'intersection', first_pos),
               record('OUR 6.pdf', True, second_item, 'intersection', second_pos)]
    scene = drawing.plan_scene(builder, records)
    assert not scene.failures
    draft = drawing.draft_row_group(scene, builder_cfg=cfg)
    assert draft.diagnostic is None and len(draft.plans) == 2
    ties = [line for plan in draft.plans for line in plan.lines if line.kind == 'main-street-row-tie']
    assert len(ties) == 1 and math.dist(ties[0].start, ties[0].end) == pytest.approx(40)
    assert ties[0].end == pytest.approx(draft.plans[0].pob)
    assert not any(t.kind in {'main-lot-label', 'neighbor-label'} for p in draft.plans for t in p.items)
    assert all(any(t.kind.startswith('side-') for t in p.items) for p in draft.plans)
    assert sum(t.kind == 'cross-street' for p in draft.plans for t in p.items) == 1
    assert sum(t.kind == 'cross-street-distance' for p in draft.plans for t in p.items) == 2
    for original, printed in zip(scene.plans, draft.plans):
        assert printed.print_draft
        for src, dst in zip(original.plan.boundary_lines, printed.boundary_lines):
            assert _vector(src) == pytest.approx(_vector(dst))
            assert dst.start == pytest.approx((src.start[0] + 1000, src.start[1]))
    assert (draft.plans[1].pob[0] - draft.plans[0].pob[0], draft.plans[1].pob[1] - draft.plans[0].pob[1]) == pytest.approx(
        (scene.plans[1].plan.pob[0] - scene.plans[0].plan.pob[0], scene.plans[1].plan.pob[1] - scene.plans[0].plan.pob[1]))


def test_common_row_draft_refuses_ambiguous_or_unlocated_scenes():
    cfg = llm_parser.load_config()
    builder = ab.AcadBuilder(cfg)
    result = drawing.plan_scene(builder, [record('OUR 6.pdf', True, full()),
                                          record('OUR 6.pdf', True, full())])
    draft = drawing.draft_row_group(result, builder_cfg=cfg)
    assert draft.plans == () and draft.diagnostic == 'unsupported_scene'


def test_layout_bounds_include_distant_label_extents():
    builder = ab.AcadBuilder(llm_parser.load_config())
    plan = drawing.plan_partial(builder, partial(1), source='OUR 6.pdf', is_main=True)
    plan.items.append(ab.TextItem('VERY LONG TEXT' * 30, (1500, 10), 0, height=2))
    left, bottom, right, top = drawing._bounds([plan])
    assert left < 0 and right > 1500 and top > 10


def test_common_draft_can_be_drawn_as_one_scene_with_fake_autocad(fake_autocad):
    from pathlib import Path
    model, _ = llm_parser.parse_with_regex(
        Path('tests/data/lot72_render_e600.txt').read_text(encoding='utf-8'), auto_repair_typos=False)
    other = model.model_copy(deep=True, update={'distance_to_corner_ft': model.distance_to_corner_ft + 40})
    builder, doc = fake_autocad(None)
    records = [record('OUR 6.pdf', True, parse.DescriptionItem(m, True), 'x', ab.compute_pob_point(m))
               for m in (model, other)]
    scene = drawing.plan_scene(builder, records)
    draft = drawing.draft_row_group(scene, builder_cfg=builder.cfg)
    assert draft.diagnostic is None
    drawn = [builder.draw_plan(p) for p in draft.plans]
    assert [x.boundary_line_count for x in drawn] == [4, 4]
    assert not any(t.TextString.startswith('LOT ') for t in doc.ModelSpace.texts)
    expected = next(line for p in draft.plans for line in p.lines if line.kind == 'main-street-row-tie')
    ties = [line for line in doc.ModelSpace.lines
            if tuple(line.StartPoint[:2]) == pytest.approx((expected.start[0] + 5000, expected.start[1] + 5000))
            and tuple(line.EndPoint[:2]) == pytest.approx((expected.end[0] + 5000, expected.end[1] + 5000))]
    assert len(ties) == 1 and math.dist(ties[0].StartPoint, ties[0].EndPoint) == pytest.approx(40)


def test_scene_print_draft_keeps_unlocated_and_partial_parts_without_numbers():
    builder = ab.AcadBuilder(llm_parser.load_config())
    entries = [record('OUR 6.pdf', True, full()), record('OUR 6.pdf', True, partial(1)),
               record('LOT 5.pdf', False, full())]
    scene = drawing.plan_scene(builder, entries)
    draft = drawing.make_scene_print_drafts(scene, builder_cfg=builder.cfg)
    assert len(draft.plans) == 2
    assert all(p.print_draft and not p.is_neighbor for p in draft.plans)
    assert draft.plans[1].open_traverse
    for orig, printed in zip([x.plan for x in scene.plans[:2]], draft.plans):
        assert not any(t.kind in {'main-lot-label', 'neighbor-label'} for t in printed.items)
        assert not printed.lines  # unknown placement must not acquire a fake intersection
        for edge, copied in zip(orig.boundary_lines, printed.boundary_lines):
            assert copied.start == pytest.approx((edge.start[0] + 1000, edge.start[1]))
            assert _vector(copied) == pytest.approx(_vector(edge))


def test_scene_print_common_row_group_has_single_intersection():
    from pathlib import Path
    model, _ = llm_parser.parse_with_regex(Path('tests/data/lot72_render_e600.txt').read_text(encoding='utf-8'),
                                   auto_repair_typos=False)
    other = model.model_copy(deep=True, update={'distance_to_corner_ft': model.distance_to_corner_ft + 40})
    cfg = llm_parser.load_config()
    builder = ab.AcadBuilder(cfg)
    scene = drawing.plan_scene(builder, [record('OUR 6.pdf', True, parse.DescriptionItem(m, True),
                                              'x', ab.compute_pob_point(m)) for m in (model, other)])
    draft = drawing.make_scene_print_drafts(scene, builder_cfg=cfg)
    assert len(draft.plans) == 2 and draft.diagnostic is None
    ties = [l for p in draft.plans for l in p.lines if l.kind == 'main-street-row-tie']
    assert len(ties) == 1 and math.dist(ties[0].start, ties[0].end) == pytest.approx(40)


def test_unlocated_full_contour_has_no_guessed_street_intersection():
    from pathlib import Path
    desc, _ = llm_parser.parse_with_regex(Path('tests/data/lot72_render_e600.txt').read_text(encoding='utf-8'),
                                          auto_repair_typos=False)
    builder = ab.AcadBuilder(llm_parser.load_config())
    scene = drawing.plan_scene(builder, [record('OUR 6.pdf', True, parse.DescriptionItem(desc, True))])
    entry = scene.plans[0]
    assert entry.diagnostic == 'unresolved_position'
    assert not entry.plan.lines
    assert any(t.kind == 'main-street' for t in entry.plan.items)
    assert not any(t.kind.startswith('cross-street') for t in entry.plan.items)
    draft = drawing.make_scene_print_drafts(scene, builder_cfg=builder.cfg).plans[0]
    assert any(t.kind == 'main-street' for t in draft.items)
    assert not draft.lines
    assert any(t.kind == 'main-lot-label' and t.text == 'LOT 6' for t in entry.plan.items)
    for edge, segment in zip(entry.plan.boundary_lines, desc.segments):
        assert _vector(edge) == pytest.approx(segment.vector, abs=1e-6)


def test_shared_street_extensions_clip_overlap_but_preserve_ties_and_other_lines():
    line = ab.LineItem
    previous = [line((2, 0), (8, 0), 'main-street-row-1')]
    parts = drawing._uncovered_street_parts(line((10, 0), (0, 0), 'main-street-row-2'), previous)
    assert [(p.start, p.end) for p in parts] == [((10, 0), (8, 0)), ((2, 0), (0, 0))]
    tie = line((0, 0), (10, 0), 'main-street-row-tie')
    assert drawing._uncovered_street_parts(tie, previous) == [tie]
    for different in [line((0, 1e-5), (10, 1e-5), 'main-street-row-1'),
                      line((0, 0), (10, 0), 'cross-street-row'),
                      line((0, 0), (10, 0), 'custom')]:
        assert drawing._uncovered_street_parts(different, previous) == [different]


def test_shared_equal_ties_remain_available_for_common_draft():
    from pathlib import Path
    model, _ = llm_parser.parse_with_regex(
        Path('tests/data/lot72_render_e600.txt').read_text(encoding='utf-8'), auto_repair_typos=False)
    cfg = llm_parser.load_config()
    builder = ab.AcadBuilder(cfg)
    pos = ab.compute_pob_point(model)
    records = [record('OUR 6.pdf', True, parse.DescriptionItem(model, True), 'intersection', pos)
               for _ in range(2)]
    scene = drawing.plan_scene(builder, records)
    assert all(any(line.kind == 'main-street-row-tie' for line in entry.plan.lines) for entry in scene.plans)
    result = drawing.draft_row_group(scene, builder_cfg=cfg)
    assert result.diagnostic is None and len(result.plans) == 2


@pytest.mark.parametrize('is_main', [True, False])
@pytest.mark.parametrize('as_lines', [True, False])
def test_unclosed_full_description_keeps_continuous_sides_from_pob(fake_autocad, is_main, as_lines):
    builder, doc = fake_autocad(None)
    builder.cfg['drawing']['property_as_lines'] = as_lines
    parsed = parse.read_response(answer([item('5', delta=3)]), [1]).items[0]
    source = record('OUR 6.pdf', is_main, parsed, 'shared', (55, 10))
    entry, = drawing.plan_scene(builder, [source]).plans
    plan = entry.plan
    assert entry.diagnostic == 'exact_sides_fallback' and plan.open_traverse
    assert plan.pob == pytest.approx((55, 10))
    expected = plan.pob
    for edge, seg in zip(plan.boundary_lines, parsed.data.segments):
        assert edge.start == pytest.approx(expected)
        assert _vector(edge) == pytest.approx(seg.vector)
        expected = edge.end
    assert math.dist(expected, plan.pob) == pytest.approx(3)
    result = builder.draw_plan(plan)
    assert result.boundary_line_count == len(parsed.data.segments)
    assert not doc.ModelSpace.polylines


def test_unlocated_frontage_never_uses_a_guessed_side_or_open_geometry():
    from pathlib import Path
    desc, _ = llm_parser.parse_with_regex(Path('tests/data/lot72_render_e600.txt').read_text(encoding='utf-8'),
                                          auto_repair_typos=False)
    builder = ab.AcadBuilder(llm_parser.load_config())
    for changed, complete in [
        (desc.model_copy(deep=True, update={'front_segment_index': None}), True),
        (desc.model_copy(deep=True, update={'main_street': ''}), True),
        (desc, False),
    ]:
        scene = drawing.plan_scene(builder, [record('OUR 6.pdf', True, parse.DescriptionItem(changed, complete))])
        assert len(scene.plans) == 1
        assert not any('street' in t.kind for t in scene.plans[0].plan.items)
        assert not scene.plans[0].plan.lines


def test_unlocated_frontage_does_not_fabricate_closure():
    from pathlib import Path
    desc, _ = llm_parser.parse_with_regex(Path('tests/data/lot72_render_e600.txt').read_text(encoding='utf-8'),
                                          auto_repair_typos=False)
    desc = desc.model_copy(deep=True)
    desc.segments[-1].distance_ft += 1
    scene = drawing.plan_scene(ab.AcadBuilder(llm_parser.load_config()),
                               [record('OUR 6.pdf', True, parse.DescriptionItem(desc, True))])
    assert len(scene.plans) == 1
    assert not any(t.kind == 'main-street' for t in scene.plans[0].plan.items)
    assert not scene.plans[0].plan.lines
