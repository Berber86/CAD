"""Offline correction provenance and colored AutoCAD output."""
from dataclasses import replace
import json

import pytest
from pydantic import ValidationError

import acad_builder as ab
import llm_parser
import multiple_description_candidate as candidate
import multiple_description_drawing as drawing
import street_names
from multiple_description_parse import DescriptionItem, PartialLegalDescription
from test_multiple_description_candidate import sources
from test_print_draft import _Entity, _model, fake_autocad  # noqa: F401


@pytest.mark.parametrize('source,correction,expected,changed,deleted', [
    ('Lindberg Avenue', 'Lindbergh Avenue', 'LINDBERGH  AVENUE', 'H', ''),
    ('Lindbergh Avenue', 'Lindberg Avenue', 'LINDBERG  AVENUE', '', 'H'),
    ('Hylan Boulevard', 'Hylam Boulevard', 'HYLAM  BOULEVARD', 'M', ''),
    ('63RD Street', '63rd  street', '63rd  STREET', '', ''),
    ('North Main', 'South Main', 'SOUTH  MAIN', 'SU', ''),
])
def test_marks_ignore_format_and_track_insert_replace_and_delete(source, correction, expected, changed, deleted):
    text, indices, removed = street_names.correction_marks(source, correction)
    assert text == expected
    assert ''.join(text[i] for i in indices) == changed
    assert removed == deleted


def test_json_roundtrip_and_legacy_original_requirement():
    model = _model('lot72_render_e600.txt')
    payload = model.model_dump(mode='json')
    payload.update(main_street='Lindberg Avenue', main_street_corrected='Lindbergh Avenue',
                   cross_street='Hylam Boulevard', cross_street_corrected='Hylan Boulevard')
    restored = llm_parser.LegalDescription.model_validate(payload)
    raw = json.loads(restored.to_json())
    assert raw['main_street'] == 'Lindberg Avenue'
    assert raw['main_street_corrected'] == 'Lindbergh Avenue'
    assert raw['cross_street'] == 'Hylam Boulevard'
    assert raw['cross_street_corrected'] == 'Hylan Boulevard'
    for cls in (llm_parser.LegalDescription, PartialLegalDescription):
        with pytest.raises(ValidationError, match='requires the original'):
            cls.model_validate({**payload, 'cross_street': None})
    old = {k: v for k, v in payload.items() if not k.endswith('_corrected')}
    assert llm_parser.LegalDescription.model_validate(old).main_street_corrected is None


def install_mtext(doc):
    created = []
    def add(textpoint, width, text):
        entity = _Entity(TextString=text, InsertionPoint=textpoint, Width=width)
        created.append(entity)
        return entity
    doc.ModelSpace.AddMText = add
    return created


def test_main_and_cross_are_colored_inline_but_print_draft_is_plain(fake_autocad):
    builder, doc = fake_autocad(None)
    created = install_mtext(doc)
    model = _model('lot72_render_e600.txt').model_copy(update={
        'main_street': 'Lindberg Avenue', 'main_street_corrected': 'Lindbergh Avenue',
        'cross_street': 'Hylann Boulevard', 'cross_street_corrected': 'Hylan Boulevard'})
    plan = builder.plan(model, scale=16)
    main = next(x for x in plan.items if x.kind == 'main-street')
    cross = next(x for x in plan.items if x.kind == 'cross-street')
    assert main.text == 'LINDBERGH  AVENUE' and main.correction_indices
    assert cross.text == 'HYLAN  BOULEVARD' and cross.correction_deleted == 'N'
    result = builder.draw_plan(plan)
    assert not any('выделить исправление' in w for w in result.warnings)
    assert any(t.TextString == r'LINDBERG{\C1;H}  AVENUE' for t in created)
    assert any(t.TextString == r'HYLAN  BOULEVARD {\C1;[-N]}' for t in created)
    assert all(t.AttachmentPoint == 5 for t in created)
    draft = ab.make_print_draft(plan, {}, builder_cfg=builder.cfg)
    assert all(not t.correction_indices and not t.correction_deleted for t in draft.items)
    count = len(created)
    builder.draw_plan(draft)
    assert len(created) == count
    assert any(t.TextString == 'LINDBERGH  AVENUE' for t in doc.ModelSpace.texts)
    assert any(t.TextString == 'HYLAN  BOULEVARD' for t in doc.ModelSpace.texts)


def test_com_failure_is_reported_and_text_is_not_lost(fake_autocad):
    builder, doc = fake_autocad(None)
    model = _model('lot72_render_e600.txt').model_copy(update={
        'main_street': 'Lindberg Avenue', 'main_street_corrected': 'Lindbergh Avenue'})
    result = builder.draw_plan(builder.plan(model, scale=16))
    assert any('не удалось выделить исправление' in warning for warning in result.warnings)
    assert any(t.TextString == 'LINDBERGH  AVENUE' for t in doc.ModelSpace.texts)


def test_shared_label_keeps_correction_from_second_source_and_neighbor(fake_autocad):
    first, second = sources()
    first_data = first.item.data.model_copy(update={'cross_street': 'Lindbergh Avenue'})
    second_data = second.item.data.model_copy(update={'cross_street': 'Lindberg Avenue',
                                                      'cross_street_corrected': 'Lindbergh Avenue'})
    records = (replace(first, item=DescriptionItem(first_data, True)),
               replace(second, source='neighbor.pdf', is_main=False,
                       item=DescriptionItem(second_data, True)))
    approved, count = candidate.auto_place_rows(records)
    assert count == 1
    assert approved[1].item.data.cross_street == 'Lindberg Avenue'
    builder, doc = fake_autocad(None)
    created = install_mtext(doc)
    scene = drawing.plan_scene(builder, approved)
    labels = [t for e in scene.plans for t in e.plan.items if t.kind == 'cross-street']
    # Сцена подписи не сливает: у каждого плана — своя, как в обычном режиме;
    # исправление соседа сохранено и помечено только в его плане.
    assert len(labels) == 2 and all(t.text == 'LINDBERGH  AVENUE' for t in labels)
    corrected = [t for t in labels if t.correction_indices]
    assert len(corrected) == 1
    assert ''.join(corrected[0].text[i] for i in corrected[0].correction_indices) == 'H'
    for e in scene.plans:
        builder.draw_plan(e.plan)
    assert any(r'LINDBERG{\C1;H}' in t.TextString for t in created)
    drafts = drawing.make_scene_print_drafts(scene, builder_cfg=builder.cfg)
    assert all(not t.correction_indices and not t.correction_deleted for p in drafts.plans for t in p.items)


def test_unlocated_neighbor_corrections_match_normal_mode(fake_autocad):
    source = sources()[0]
    data = source.item.data.model_copy(update={'main_street': 'Hylam Boulevard',
                                               'main_street_corrected': 'Hylan Boulevard',
                                               'cross_street_corrected': 'Lindbergh Avenue'})
    source = replace(source, is_main=False, item=DescriptionItem(data, True))
    builder, _ = fake_autocad(None)
    scene = drawing.plan_scene(builder, [source])
    plan = scene.plans[0].plan
    # Сосед без якоря оформляется как в обычном режиме: видны исправления
    # и фасадной, и пересекаемой улицы; линий улиц у соседа нет, как обычно.
    assert not plan.lines
    assert any(t.kind == 'main-street' and t.correction_indices for t in plan.items)
    assert any(t.kind == 'cross-street' and t.correction_indices for t in plan.items)


def test_multi_parse_roundtrip_keeps_corrected_and_original_and_rejects_missing_original():
    import multiple_description_parse as mp
    from test_multiple_description_parse import pages
    data = _model('lot72_render_e600.txt').model_dump(mode='json')
    data.update(main_street='Hylam Boulevard', main_street_corrected='Hylan Boulevard')
    def run(payload):
        return mp.parse_pages({'multiple_description_search': {'enabled': True}}, pages(1),
                              image_limit=50, filename_lot=None, portal_lot=None, search_complete=True,
                              send=lambda *args: json.dumps({'descriptions': [{'complete': True, 'data': payload}],
                                                            'complete': True}))
    result = run(data)
    assert result.complete and result.items[0].data.main_street == 'Hylam Boulevard'
    assert result.items[0].data.main_street_corrected == 'Hylan Boulevard'
    bad = run({**data, 'main_street': ''})
    assert not bad.complete and not bad.items


def test_unlocated_draft_has_no_correction_metadata(fake_autocad):
    first = sources()[0]
    model = first.item.data.model_copy(update={'main_street': 'Hylam Boulevard',
                                               'main_street_corrected': 'Hylan Boulevard'})
    builder, doc = fake_autocad(None)
    created = install_mtext(doc)
    scene = drawing.plan_scene(builder, [replace(first, item=DescriptionItem(model, True))])
    assert any(t.correction_indices for t in scene.plans[0].plan.items)
    drafts = drawing.make_scene_print_drafts(scene, builder_cfg=builder.cfg)
    assert all(not t.correction_indices and not t.correction_deleted for p in drafts.plans for t in p.items)
    for plan in drafts.plans:
        builder.draw_plan(plan)
    assert not created
    assert any(t.TextString == 'HYLAN  BOULEVARD' for t in doc.ModelSpace.texts)


def test_different_corrected_labels_are_kept_per_plan(fake_autocad):
    first, second = sources()
    data = second.item.data.model_copy(update={'cross_street': 'Lindbergh Avenue',
                                               'cross_street_corrected': 'Lindburg Avenue'})
    approved, count = candidate.auto_place_rows((first, replace(second, item=DescriptionItem(data, True))))
    assert count == 1
    builder, _ = fake_autocad(None)
    scene = drawing.plan_scene(builder, approved)
    labels = [t for e in scene.plans for t in e.plan.items if t.kind == 'cross-street']
    # Разные исправления не сливаются в одну подпись: у каждого плана — своя.
    assert len(labels) == 2
    assert {t.text for t in labels} == {'LINDBERG  AVENUE', 'LINDBURG  AVENUE'}
    assert any(t.correction_indices or t.correction_deleted for t in labels)
