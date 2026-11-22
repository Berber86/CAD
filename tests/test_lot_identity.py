"""Lot identities survive parsing, corrections, plotting and both advisor prompts."""
from dataclasses import replace

import pytest

import lot_identity as li
import llm_parser as lp
from acad_builder import AcadBuilder
from conflict_resolver import (LotInput, _Scene, Change, apply_changes, advisor_cache_key,
                               build_advisor_request, build_join_request)


@pytest.mark.parametrize("filename,expected", [
    ("LOT 30 NEW.pdf", "30"), ("lot30.pdf", "30"), ("LOT_12_P3.PDF", "12"),
    ("OUR 32 NEW_p4-6.pdf", "32"), ("LOT 30 OLD_p2-2.pdf", "30"),
    ("folder/56th Street LOT 12A.pdf", "12A"), (r"C:\\files\\лот-99.jpg", "99"),
    ("30.pdf", None), ("deed_LOT_20_p1.pdf", "20"), ("lot0012A_p2_5.pdf", "0012A"),
    ("main_2025_p1.pdf", None), ("bruklin250_p2_5.pdf", None),
    ("address 5th Street.pdf", None), ("LOT 30-32.pdf", None),
    ("LOT 30 LOT 31.pdf", None), ("neighbor.pdf", None),
])
def test_extract_only_unambiguous_lot_identifiers(filename, expected):
    assert li.lot_from_filename(filename) == expected


def sample():
    desc, _ = lp.parse_with_regex(lp.ASPENWALL_SAMPLES["Aspenwall St — 365' (Сосед слева)"])
    return desc


def test_filename_label_does_not_override_document_metadata_or_geometry():
    source = sample()
    assert source.lot_number == "26"
    desc = li.bind_filename(source, "LOT 30 NEW_p2.pdf")
    assert source._source_lot_number is None and desc.lot_number == "26"
    assert desc._source_lot_number == "30"
    assert "_source_lot_number" not in desc.model_dump()
    builder = AcadBuilder(lp.load_config())
    old = builder.plan(source, is_neighbor=True)
    plan = builder.plan(desc, is_neighbor=True)
    assert plan.items[0].text == "LOT 30"
    assert plan.boundary_lines == old.boundary_lines
    assert any("не совпадает" in w and "30" in w and "26" in w for w in plan.warnings)
    assert li.effective_number(desc.model_copy(deep=True)) == "30"


def test_unknown_and_document_fallback():
    builder = AcadBuilder(lp.load_config())
    assert builder.plan(sample(), is_neighbor=True).items[0].text == "LOT 26"
    missing = sample().model_copy(update={"lot_number": None})
    plan = builder.plan(li.bind_filename(missing, "neighbor.pdf"), is_neighbor=True)
    assert plan.items[0].text == "LOT ?"
    assert any("номер лота" in w for w in plan.warnings)
    assert li.canonical("00030") == li.canonical("30")
    assert not li.conflict(li.bind_filename(sample(), "LOT 026.pdf"))


def test_applied_fixes_keep_filename_identifier():
    desc = li.bind_filename(sample(), "LOT 26 NEW.pdf")
    change = Change(target="side", index=0, field="distance", old_label="100.00'",
                    new_label="101.00'", distance_ft=101.0)
    changed = apply_changes(desc, (change,))
    assert changed._source_lot_number == "26" and changed._source_lot_filename == "LOT 26 NEW.pdf"
    assert AcadBuilder(lp.load_config()).plan(changed, is_neighbor=True).items[0].text == "LOT 26"


def test_both_advisor_requests_explicitly_identify_each_lot_and_source():
    desc = sample().model_copy(update={"lot_number": None})
    main = LotInput("main", "OUR 32 NEW.pdf", True, desc)
    neighbor = LotInput("nb", "LOT 30 NEW.pdf", False, desc.model_copy(update={"lot_number": "31"}))
    scene = _Scene(AcadBuilder(lp.load_config()), [main, neighbor], 16)
    advice, _, labels = build_advisor_request(scene, [], ["overlap"], None)
    join = build_join_request(scene, [], ["overlap"], labels)
    for request in (advice, join):
        assert "lot identifier from filename: 32; lot from parser result: unknown" in request
        assert "lot identifier from filename: 30; lot from parser result: 31" in request
        assert "DISAGREEMENT" in request and "not proof" in request.lower()
    assert labels["L1"] is main and labels["L2"] is neighbor
    assert advisor_cache_key([main, neighbor]) != advisor_cache_key([main, replace(neighbor, name="LOT 31.pdf")])


@pytest.mark.parametrize("filename,deed,expected,warning", [
    ("OUR 32 NEW.pdf", None, "LOT 32", None),
    ("OUR 32 NEW.pdf", "32", "LOT 32", None),
    ("OUR 32 NEW.pdf", "26", "LOT 32", "не совпадает"),
    ("plain.pdf", "26", "LOT 26", None),
    ("plain.pdf", None, "LOT ?", "не найден"),
])
def test_our_lot_label_main_but_not_print_draft(filename, deed, expected, warning):
    from acad_builder import make_print_draft, point_in_polygon

    desc = li.bind_filename(sample().model_copy(update={"lot_number": deed}), filename)
    builder = AcadBuilder(lp.load_config())
    plan = builder.plan(desc)
    draft = make_print_draft(plan, {}, builder_cfg=lp.load_config())
    label = next(item for item in plan.items if item.kind == "main-lot-label")
    assert label.text == expected
    assert not any(item.kind == "main-lot-label" for item in draft.items)
    assert not any(item.text == expected for item in draft.items)
    assert point_in_polygon(label.point, plan.vertices)
    assert desc.lot_number == deed
    if warning:
        assert any(warning in w for w in plan.warnings)
    else:
        assert not any("номер лота" in w for w in plan.warnings)
    assert [s.distance_ft for s in plan.description.segments] == [s.distance_ft for s in desc.segments]


@pytest.mark.parametrize('path,expected', [
    ('OUR 6 NEW.pdf', True), ('our 06.pdf', True), ('Our_6_p2.pdf', True),
    ('OUR6.pdf', True), ('OUR.pdf', True), ('OUR(1).PDF', True),
    ('C:\\OUR files\\LOT 5.pdf', False), ('not OUR 6.pdf', False),
    ('ours.pdf', False), ('ourhouse.pdf', False), ('OURАллея.pdf', False),
    ('LOT 6.pdf', False), ('', False),
])
def test_our_role_prefix_is_on_the_basename_only(path, expected):
    assert li.has_our_prefix(path) is expected
