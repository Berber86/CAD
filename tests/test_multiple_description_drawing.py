"""Offline planning and fake-AutoCAD rendering of partial boundary chains."""
import copy
import math

import pytest

import acad_builder as ab
import llm_parser
import lot_identity
import multiple_description_drawing as drawing
import multiple_description_parse as parse
from test_multiple_description_parse import answer, item
from test_print_draft import fake_autocad  # noqa: F401 -- shared AutoCAD fixture


def normal_plan(builder, source, value, is_main, scale=16.0):
    """Эталон обычного режима: тот же вызов построителя, что и при ручном разборе."""
    desc = lot_identity.bind_filename(value.data, source)
    return builder.plan(desc, scale=scale, is_neighbor=not is_main)


def assert_same_as_normal(actual, expected):
    """План сцены обязан совпадать с планом обычного режима поле в поле."""
    assert actual.is_neighbor == expected.is_neighbor
    assert actual.scale == expected.scale and actual.text_height == expected.text_height
    assert actual.pob == pytest.approx(expected.pob)
    assert len(actual.vertices) == len(expected.vertices)
    assert all(math.dist(a, b) < 1e-9 for a, b in zip(actual.vertices, expected.vertices))
    assert [(e.start, e.end, e.kind) for e in actual.boundary_lines] == \
           [(e.start, e.end, e.kind) for e in expected.boundary_lines]
    assert [(e.start, e.end, e.kind) for e in actual.lines] == \
           [(e.start, e.end, e.kind) for e in expected.lines]
    assert [(t.text, t.point, t.rotation, t.kind, t.height, t.correction_indices, t.correction_deleted)
            for t in actual.items] == \
           [(t.text, t.point, t.rotation, t.kind, t.height, t.correction_indices, t.correction_deleted)
            for t in expected.items]
    assert [(a.center, a.radius, a.start_angle, a.end_angle, a.kind) for a in actual.arcs] == \
           [(a.center, a.radius, a.start_angle, a.end_angle, a.kind) for a in expected.arcs]
    assert actual.street_boundary_indices == expected.street_boundary_indices
    assert actual.open_traverse == expected.open_traverse
    assert actual.warnings == expected.warnings


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
    # Каждый план сцены — один в один план обычного режима по этому описанию.
    for entry, planned in zip(entries, result.plans):
        assert_same_as_normal(planned.plan, normal_plan(builder, entry.source, entry.item, True))
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


def test_unlocated_contours_are_not_moved_or_stripped():
    builder = ab.AcadBuilder(llm_parser.load_config())
    values = [record('OUR 6.pdf', True, full('5'), 'known', (0, 0)),
              record('OUR 6.pdf', True, full('6')),
              record('LOT 2.pdf', False, partial(2))]
    result = drawing.plan_scene(builder, values, separation_ft=100)
    assert len(result.plans) == 3 and not result.failures
    assert [p.placement_known for p in result.plans] == [True, False, False]
    # Ни сдвига в ряд, ни срезанных улиц: полные планы обычного режима.
    assert_same_as_normal(result.plans[0].plan, normal_plan(builder, 'OUR 6.pdf', values[0].item, True))
    assert_same_as_normal(result.plans[1].plan, normal_plan(builder, 'OUR 6.pdf', values[1].item, True))
    for original, planned in zip(values, result.plans):
        for edge, seg in zip(planned.plan.boundary_lines, original.item.data.segments):
            assert _vector(edge) == pytest.approx(seg.vector, abs=1e-6)
    # Неполное описание из двух сторон обычный построитель не берёт: открытые стороны.
    assert result.plans[2].plan.open_traverse
    assert result.plans[1].diagnostic == 'unresolved_position'


def test_shared_street_items_are_kept_per_plan_like_normal_mode():
    builder = ab.AcadBuilder(llm_parser.load_config())
    records = [record('OUR 6.pdf', True, full('5'), 'known', (0, 0)),
               record('OUR 6.pdf', True, full('6'), 'known', (0, 0))]
    scene = drawing.plan_scene(builder, records)
    a, b = [p.plan for p in scene.plans]
    assert len(a.boundary_lines) == len(b.boundary_lines) == 4
    # Сцена ничего не сливает и не срезает: у каждого плана — свои подписи улиц,
    # как если бы участки строили вручную по одному.
    assert sum(i.kind == 'main-street' for p in (a, b) for i in p.items) == 2
    assert sum(i.kind == 'main-lot-label' for p in (a, b) for i in p.items) == 2
    for entry, planned in zip(records, scene.plans):
        assert_same_as_normal(planned.plan, normal_plan(builder, entry.source, entry.item, True))


def test_scene_uses_builder_plan_verbatim_even_if_it_adjusts_geometry(monkeypatch):
    # Эталон — обычный режим: что построитель вернул, то и рисуется. Сцена
    # не отбраковывает план за «исправленную» построителем геометрию.
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
    assert not result.plan.open_traverse and result.diagnostic == 'unresolved_position'
    edge = result.plan.boundary_lines[0]
    assert _vector(edge) == pytest.approx((source.item.data.segments[0].vector[0] + 3,
                                           source.item.data.segments[0].vector[1]), abs=1e-6)


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


def test_scene_print_drafts_are_normal_drafts_with_40ft_ties_and_no_our_numbers():
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
    # Черновики сцены — по одному обычному make_print_draft на участок,
    # как если бы каждый участок строили вручную.
    draft = drawing.make_scene_print_drafts(scene, builder_cfg=cfg, print_cfg={})
    assert draft.diagnostic is None and len(draft.plans) == 2
    expected = [ab.make_print_draft(entry.plan, {}, builder_cfg=cfg) for entry in scene.plans]
    for printed, wanted in zip(draft.plans, expected):
        assert_same_as_normal(printed, wanted)
        assert printed.print_draft
    ties = [line for plan in draft.plans for line in plan.lines if line.kind == 'main-street-row-tie']
    assert len(ties) == 2 and all(math.dist(t.start, t.end) == pytest.approx(40) for t in ties)
    assert not any(t.kind in {'main-lot-label', 'neighbor-label'} for p in draft.plans for t in p.items)
    assert all(any(t.kind.startswith('side-') for t in p.items) for p in draft.plans)
    for original, printed in zip(scene.plans, draft.plans):
        for src, dst in zip(original.plan.boundary_lines, printed.boundary_lines):
            assert _vector(src) == pytest.approx(_vector(dst))
            assert dst.start == pytest.approx((src.start[0] + 1000, src.start[1]))


def test_scene_print_drafts_do_not_refuse_unlocated_scenes():
    cfg = llm_parser.load_config()
    builder = ab.AcadBuilder(cfg)
    result = drawing.plan_scene(builder, [record('OUR 6.pdf', True, full()),
                                          record('OUR 6.pdf', True, full())])
    draft = drawing.make_scene_print_drafts(result, builder_cfg=cfg, print_cfg={})
    assert draft.diagnostic is None and len(draft.plans) == 2
    assert all(p.print_draft and not p.is_neighbor for p in draft.plans)


def test_layout_bounds_include_distant_label_extents():
    builder = ab.AcadBuilder(llm_parser.load_config())
    plan = drawing.plan_partial(builder, partial(1), source='OUR 6.pdf', is_main=True)
    plan.items.append(ab.TextItem('VERY LONG TEXT' * 30, (1500, 10), 0, height=2))
    left, bottom, right, top = drawing._bounds([plan])
    assert left < 0 and right > 1500 and top > 10


def test_scene_drafts_can_be_drawn_with_fake_autocad(fake_autocad):
    from pathlib import Path
    model, _ = llm_parser.parse_with_regex(
        Path('tests/data/lot72_render_e600.txt').read_text(encoding='utf-8'), auto_repair_typos=False)
    other = model.model_copy(deep=True, update={'distance_to_corner_ft': model.distance_to_corner_ft + 40})
    builder, doc = fake_autocad(None)
    records = [record('OUR 6.pdf', True, parse.DescriptionItem(m, True), 'x', ab.compute_pob_point(m))
               for m in (model, other)]
    scene = drawing.plan_scene(builder, records)
    draft = drawing.make_scene_print_drafts(scene, builder_cfg=builder.cfg, print_cfg={})
    assert draft.diagnostic is None and len(draft.plans) == 2
    drawn = [builder.draw_plan(p) for p in draft.plans]
    assert [x.boundary_line_count for x in drawn] == [4, 4]
    assert not any(t.TextString.startswith('LOT ') for t in doc.ModelSpace.texts)
    # У каждого черновика — своя обычная привязка 40 ft, как в обычном режиме.
    expected = [line for p in draft.plans for line in p.lines if line.kind == 'main-street-row-tie']
    assert len(expected) == 2
    for tie in expected:
        drawn_ties = [line for line in doc.ModelSpace.lines
                      if tuple(line.StartPoint[:2]) == pytest.approx((tie.start[0] + 5000, tie.start[1] + 5000))
                      and tuple(line.EndPoint[:2]) == pytest.approx((tie.end[0] + 5000, tie.end[1] + 5000))]
        assert len(drawn_ties) == 1
        assert math.dist(drawn_ties[0].StartPoint, drawn_ties[0].EndPoint) == pytest.approx(40)


def test_scene_print_drafts_cover_unlocated_and_partial_mains_without_numbers():
    builder = ab.AcadBuilder(llm_parser.load_config())
    entries = [record('OUR 6.pdf', True, full()), record('OUR 6.pdf', True, partial(1)),
               record('LOT 5.pdf', False, full())]
    scene = drawing.plan_scene(builder, entries)
    draft = drawing.make_scene_print_drafts(scene, builder_cfg=builder.cfg, print_cfg={})
    assert len(draft.plans) == 2
    assert all(p.print_draft and not p.is_neighbor for p in draft.plans)
    assert draft.plans[1].open_traverse
    # Каждый черновик — обычный make_print_draft соответствующего плана.
    expected = [ab.make_print_draft(entry.plan, {}, builder_cfg=builder.cfg)
                for entry in scene.plans if entry.is_main]
    for printed, wanted in zip(draft.plans, expected):
        assert_same_as_normal(printed, wanted)
        assert not any(t.kind in {'main-lot-label', 'neighbor-label'} for t in printed.items)
        for edge, copied in zip(wanted.boundary_lines, printed.boundary_lines):
            assert copied.start == pytest.approx(edge.start)
            assert _vector(copied) == pytest.approx(_vector(edge))


def test_scene_print_drafts_each_have_their_own_40ft_tie():
    from pathlib import Path
    model, _ = llm_parser.parse_with_regex(Path('tests/data/lot72_render_e600.txt').read_text(encoding='utf-8'),
                                   auto_repair_typos=False)
    other = model.model_copy(deep=True, update={'distance_to_corner_ft': model.distance_to_corner_ft + 40})
    cfg = llm_parser.load_config()
    builder = ab.AcadBuilder(cfg)
    scene = drawing.plan_scene(builder, [record('OUR 6.pdf', True, parse.DescriptionItem(m, True),
                                              'x', ab.compute_pob_point(m)) for m in (model, other)])
    draft = drawing.make_scene_print_drafts(scene, builder_cfg=cfg, print_cfg={})
    assert len(draft.plans) == 2 and draft.diagnostic is None
    ties = [l for p in draft.plans for l in p.lines if l.kind == 'main-street-row-tie']
    assert len(ties) == 2 and all(math.dist(t.start, t.end) == pytest.approx(40) for t in ties)


def test_unlocated_full_contour_matches_normal_mode():
    from pathlib import Path
    desc, _ = llm_parser.parse_with_regex(Path('tests/data/lot72_render_e600.txt').read_text(encoding='utf-8'),
                                          auto_repair_typos=False)
    builder = ab.AcadBuilder(llm_parser.load_config())
    scene = drawing.plan_scene(builder, [record('OUR 6.pdf', True, parse.DescriptionItem(desc, True))])
    entry = scene.plans[0]
    assert entry.diagnostic == 'unresolved_position'
    # Без якоря план всё равно полный, как в обычном режиме: улицы, привязка,
    # пересекаемая улица — всё, что построитель даёт по этому описанию.
    assert_same_as_normal(entry.plan, normal_plan(builder, 'OUR 6.pdf', parse.DescriptionItem(desc, True), True))
    assert entry.plan.lines and any(t.kind.startswith('cross-street') for t in entry.plan.items)
    assert any(t.kind == 'main-lot-label' and t.text == 'LOT 6' for t in entry.plan.items)
    draft = drawing.make_scene_print_drafts(scene, builder_cfg=builder.cfg, print_cfg={}).plans[0]
    assert_same_as_normal(draft, ab.make_print_draft(entry.plan, {}, builder_cfg=builder.cfg))


def test_shared_equal_ties_remain_available_for_drafts():
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
    result = drawing.make_scene_print_drafts(scene, builder_cfg=cfg, print_cfg={})
    assert result.diagnostic is None and len(result.plans) == 2
    assert all(any(line.kind == 'main-street-row-tie' for line in p.lines) for p in result.plans)


@pytest.mark.parametrize('is_main', [True, False])
@pytest.mark.parametrize('as_lines', [True, False])
def test_unclosed_full_description_matches_normal_mode(fake_autocad, is_main, as_lines):
    builder, doc = fake_autocad(None)
    builder.cfg['drawing']['property_as_lines'] = as_lines
    parsed = parse.read_response(answer([item('5', delta=3)]), [1]).items[0]
    source = record('OUR 6.pdf', is_main, parsed, 'shared', (55, 10))
    entry, = drawing.plan_scene(builder, [source]).plans
    plan = entry.plan
    # Незамыкающееся описание сцена строит тем же построителем, что и обычный
    # режим: невязка и открытый гэп — по правилам построителя, без сценной цепочки.
    assert entry.diagnostic is None
    assert_same_as_normal(plan, normal_plan(builder, 'OUR 6.pdf', parsed, is_main))
    for edge, seg in zip(plan.boundary_lines, parsed.data.segments):
        assert _vector(edge) == pytest.approx(seg.vector)
    assert plan.closure.distance == pytest.approx(3)
    result = builder.draw_plan(plan)
    if is_main or as_lines:
        assert result.boundary_line_count == len(parsed.data.segments)
        assert not doc.ModelSpace.polylines
    else:
        # Сосед полилинией — обычное правило построителя при property_as_lines=False.
        assert result.boundary_line_count == 0 and len(doc.ModelSpace.polylines) == 1


def test_unlocated_variants_all_match_normal_mode():
    from pathlib import Path
    desc, _ = llm_parser.parse_with_regex(Path('tests/data/lot72_render_e600.txt').read_text(encoding='utf-8'),
                                          auto_repair_typos=False)
    builder = ab.AcadBuilder(llm_parser.load_config())
    for changed, complete in [
        (desc.model_copy(deep=True, update={'front_segment_index': None}), True),
        (desc.model_copy(deep=True, update={'main_street': ''}), True),
        (desc, False),
    ]:
        value = parse.DescriptionItem(changed, complete)
        scene = drawing.plan_scene(builder, [record('OUR 6.pdf', True, value)])
        assert len(scene.plans) == 1
        # И без фасада, и без названия улицы, и при неполной пометке план —
        # ровно план обычного режима по этому описанию.
        assert_same_as_normal(scene.plans[0].plan, normal_plan(builder, 'OUR 6.pdf', value, True))


def test_unlocated_gap_keeps_builder_closure_rules():
    from pathlib import Path
    desc, _ = llm_parser.parse_with_regex(Path('tests/data/lot72_render_e600.txt').read_text(encoding='utf-8'),
                                          auto_repair_typos=False)
    desc = desc.model_copy(deep=True)
    desc.segments[-1].distance_ft += 1
    builder = ab.AcadBuilder(llm_parser.load_config())
    scene = drawing.plan_scene(builder, [record('OUR 6.pdf', True, parse.DescriptionItem(desc, True))])
    assert len(scene.plans) == 1
    # Большая невязка не повод срезать улицы: план — как в обычном режиме.
    assert_same_as_normal(scene.plans[0].plan,
                          normal_plan(builder, 'OUR 6.pdf', parse.DescriptionItem(desc, True), True))


def deed_example_items():
    """Actual supplied model output, reconstructed offline; no PDF/AI calls."""
    values = [
        ('5', 'Lindberg Avenue', 55.42, [
            ('N', 55, 4, 10, 'W', 98.60), ('S', 34, 55, 50, 'W', 44.64),
            ('S', 55, 4, 10, 'E', 96.58), ('N', 37, 31, 21, 'E', 44.69)]),
        ('6', 'Lindbergh Avenue', 100.11, [
            ('N', 55, 4, 10, 'W', 96.58), ('S', 34, 55, 50, 'W', 100.00),
            ('S', 55, 4, 10, 'E', 92.06), ('N', 37, 31, 21, 'E', 100.10)])]
    entries = []
    for lot, cross, distance, sides in values:
        segments = [dict(ns=ns, degrees=deg, minutes=minutes, seconds=seconds, ew=ew, distance_ft=length)
                    for ns, deg, minutes, seconds, ew, length in sides]
        segments[-1]['course_note'] = 'along the northwesterly side of Hylan Boulevard'
        entries.append({'complete': True, 'data': {'main_street': 'Hylan Boulevard', 'cross_street': cross,
                        'distance_to_corner_ft': distance, 'distance_to_corner_source': f'{distance:.2f} feet',
                        'corner_to_pob_bearing': None, 'borough_or_city': 'Staten Island, NY',
                        'front_side': 'SE', 'front_segment_index': 3, 'lot_number': lot, 'segments': segments}})
    return parse.read_response(answer(entries), [2, 3]).items


def test_supplied_deed_matches_normal_mode(fake_autocad):
    items = deed_example_items()
    assert len(items) == 2 and all(i.data.corner_to_pob_bearing is None for i in items)
    builder, doc = fake_autocad(None)
    scene = drawing.plan_scene(builder, [record('OUR 6 NEW.pdf', True, item) for item in items])
    assert len(scene.plans) == 2
    drafts = drawing.make_scene_print_drafts(scene, builder_cfg=builder.cfg, print_cfg={})
    assert len(drafts.plans) == 2 and drafts.diagnostic is None
    for entry, original, draft in zip(scene.plans, items, drafts.plans):
        plan = entry.plan
        assert not entry.placement_known and entry.diagnostic == 'unresolved_position'
        # Реальный дид пользователя: план сцены — один в один обычный режим
        # (улицы, пересекаемая улица, привязка, подписи сторон и углов).
        assert_same_as_normal(plan, normal_plan(builder, 'OUR 6 NEW.pdf', original, True))
        assert not plan.open_traverse and 0 < plan.closure.distance < 0.015
        assert len(plan.boundary_lines) == 4 and plan.street_boundary_indices == (3,)
        assert plan.lines and any(t.kind.startswith('cross-street') for t in plan.items)
        assert any(t.kind == 'main-street' and 'HYLAN' in t.text for t in plan.items)
        assert_same_as_normal(draft, ab.make_print_draft(plan, {}, builder_cfg=builder.cfg))
        for edge, segment in zip(plan.boundary_lines, original.data.segments):
            assert _vector(edge) == pytest.approx(segment.vector, abs=1e-6)
        builder.draw_plan(plan)
    assert sum('HYLAN' in text.TextString for text in doc.ModelSpace.texts) == 2
    expected_lines = sum(len(e.plan.boundary_lines) + len(e.plan.lines) for e in scene.plans)
    assert len(doc.ModelSpace.lines) == expected_lines and not doc.ModelSpace.polylines


def test_non_rounding_gap_still_matches_normal_mode():
    import copy
    original = deed_example_items()[0]
    changed = copy.deepcopy(original.data)
    changed.segments[-1].distance_ft += 0.1
    builder = ab.AcadBuilder(llm_parser.load_config())
    value = parse.DescriptionItem(changed, True)
    entry = drawing.plan_scene(builder, [record('OUR 6.pdf', True, value)]).plans[0]
    assert entry.plan.closure.distance > drawing.ROUNDING_GAP_LIMIT_FT
    assert_same_as_normal(entry.plan, normal_plan(builder, 'OUR 6.pdf', value, True))
