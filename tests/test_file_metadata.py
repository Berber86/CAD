"""File passports retain identity readings and their actual origins independently."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import deed_store
import file_metadata as fm
import lot_identity
from llm_parser import ParseOutcome, parse_with_regex, ASPENWALL_SAMPLES


def desc():
    description, _ = parse_with_regex(ASPENWALL_SAMPLES["Aspenwall St — 365' (Сосед слева)"])
    return description.model_copy(update={"block_number": "6000", "lot_number": "26", "borough_or_city": "Queens"})


def test_filename_is_strict_about_addresses_and_pages():
    result = fm.filename_observations("BLOCK 6323 LOT 30 BOROUGH Staten_Island_p2-4.pdf")
    assert result["block"][0]["value"] == "6323"
    assert result["lot"][0]["value"] == "30"
    assert result["borough"][0]["value"] == "Staten Island"
    assert fm.filename_observations("87 Stafford 2025_p3.pdf") == {}
    assert "block" not in fm.filename_observations("BLOCK 6-7 LOT 30.pdf")


def test_passport_collects_pdf_ai_and_filename_without_mixing_files(tmp_path):
    path = tmp_path / "BLOCK 6323 LOT 30.pdf"
    path.write_bytes(b"original pdf")
    regex = ParseOutcome(data=desc(), source="regex")
    first = lot_identity.bind_outcome(regex, path)
    llm = ParseOutcome(data=desc().model_copy(update={"lot_number": "31", "borough_or_city": "Bronx"}), source="llm-vision")
    second = lot_identity.bind_outcome(llm, path)
    identity = second.data._source_identity
    assert {(e["value"], e["source"]) for e in identity["lot"]} == {("30", "filename"), ("26", "pdf"), ("31", "ai")}
    assert {(e["value"], e["source"]) for e in identity["borough"]} == {("Queens", "pdf"), ("Bronx", "ai")}
    assert second.data.lot_number == "31" and first.data.lot_number == "26"
    assert len(identity["lot"][1]["sha256"]) == 64
    stored = json.loads(path.with_suffix(".json").read_text())
    assert stored["identity"] == identity and stored["standalone"] is True
    assert deed_store.read_records(tmp_path) == []
    # repeated parsing does not multiply identical observations
    assert lot_identity.bind_outcome(llm, path).data._source_identity == identity
    assert "_source_identity" not in second.data.model_dump()
    other = tmp_path / "LOT 40.pdf"
    other.write_bytes(b"different")
    assert all(e["value"] != "31" for e in lot_identity.bind_outcome(regex, other).data._source_identity["lot"])


def test_existing_trimmed_passport_and_manifest_keep_unknown_fields(tmp_path):
    src, trim = tmp_path / "source.pdf", tmp_path / "trim.pdf"
    src.write_bytes(b"full")
    trim.write_bytes(b"trimmed")
    folder = tmp_path / "deeds"
    record = deed_store.store_pdf(folder, src, role="our", lot=32, block=12, borough="Queens",
                                 tag="NEW", trimmed_source=trim, pages="1-2")
    passport = folder / record.sidecar_name()
    raw = json.loads(passport.read_text())
    raw["future_data"] = {"keep": True}
    raw["accepted"] = True
    raw["checks"] = {"reviewed": True}
    raw["identity"]["future_field"] = {"opaque": 7}
    passport.write_text(json.dumps(raw))
    data = deed_store.read_manifest(folder)
    data["address"] = "Example"
    data["future_manifest"] = 123
    (folder / "_manifest.json").write_text(json.dumps(data))
    processed = lot_identity.bind_outcome(ParseOutcome(data=desc(), source="llm"), folder / record.trimmed)
    stored = json.loads(passport.read_text())
    manifest = deed_store.read_manifest(folder)
    assert processed.data._source_identity == stored["identity"]
    assert stored["future_data"] == {"keep": True} and stored["accepted"] is True
    assert stored["identity"]["future_field"] == {"opaque": 7}
    assert manifest["items"][0]["identity"] == stored["identity"]
    assert manifest["future_manifest"] == 123 and manifest["address"] == "Example"
    assert not (folder / (Path(record.trimmed).stem + ".json")).exists()
    # A subsequent store/manifest rebuild keeps prior readings and extension fields.
    deed_store.store_pdf(folder, src, role="our", lot=32, block=12, tag="NEW")
    stored = json.loads(passport.read_text())
    assert stored["accepted"] and stored["checks"] == {"reviewed": True}
    assert stored["identity"]["future_field"] == {"opaque": 7} and stored["future_data"] == {"keep": True}
    assert deed_store.read_manifest(folder)["items"][0]["future_data"] == {"keep": True}


def test_legacy_passport_does_not_claim_portal(tmp_path):
    path = tmp_path / "LOT 30.pdf"
    path.write_bytes(b"pdf")
    path.with_suffix(".json").write_text(json.dumps({"file": path.name, "lot": "35", "block": "6", "unknown_field": 1}))
    got = lot_identity.bind_outcome(ParseOutcome(data=desc(), source="regex"), path)
    assert ("35", "unknown") in {(e["value"], e["source"]) for e in got.data._source_identity["lot"]}
    assert json.loads(path.with_suffix(".json").read_text())["unknown_field"] == 1


def test_unsafe_collision_and_corruption_never_overwritten(tmp_path):
    path = tmp_path / "LOT 30.pdf"
    path.write_bytes(b"pdf")
    passport = path.with_suffix(".json")
    passport.write_text('{"not_a_passport": true}')
    result = lot_identity.bind_outcome(ParseOutcome(data=desc(), source="regex"), path)
    assert "не удалось сохранить паспорт" in " ".join(result.warnings)
    assert json.loads(passport.read_text()) == {"not_a_passport": True}
    passport.write_text('{bad json')
    result = lot_identity.bind_outcome(ParseOutcome(data=desc(), source="regex"), path)
    assert "не удалось сохранить паспорт" in " ".join(result.warnings)
    assert passport.read_text() == '{bad json'


def test_cache_and_ocr_are_not_misrepresented_as_ai_or_pdf_text(tmp_path):
    path = tmp_path / "LOT 30.pdf"
    path.write_bytes(b"scan")
    cached = ParseOutcome(data=desc(), source="regex")
    cached._identity_input_method = "cache"
    result = lot_identity.bind_outcome(cached, path)
    assert any(e["source"] == "unknown" and "cache" in e["method"] for e in result.data._source_identity["block"])
    local = ParseOutcome(data=desc(), source="regex")
    local._identity_input_method = "ocr"
    result = lot_identity.bind_outcome(local, path)
    assert any(e["source"] == "pdf" and "OCR" in e["method"] for e in result.data._source_identity["block"])


def test_portal_is_used_only_for_matching_values_and_neighbours():
    portal = SimpleNamespace(block="6323", lot="32", borough="Queens", neighbours=["30", "34"], details_source="ocr")
    snapshot = ("87 Main Street", portal)
    values = fm.intake_observations(6323, 32, "Queens", [30, 40], "87 Main Street", snapshot)
    assert values["32"]["lot"][0]["source"] == "portal"
    assert values["30"]["lot"][0]["method"] == "vision map (unverified)"
    assert values["40"]["lot"][0]["source"] == "manual"
    assert "unverified" in values["30"]["borough"][0]["method"]
    edited = fm.intake_observations(6323, 33, "Queens", [30], "87 Main Street", snapshot)
    assert all(e["source"] == "manual" for fields in edited.values() for entries in fields.values() for e in entries)
    changed_address = fm.intake_observations(6323, 32, "Queens", [30], "different address", snapshot)
    assert changed_address["32"]["block"][0]["source"] == "manual"
    changed_borough = fm.intake_observations(6323, 32, "Bronx", [], "87 Main Street", snapshot)
    assert changed_borough["32"]["borough"][0]["source"] == "manual"
    portal.details_source = "vision"
    assert fm.intake_observations(6323, 32, "Queens", [], "87 Main Street", snapshot)["32"]["block"][0]["method"] == "vision"


def test_changed_pdf_retains_history_with_distinct_content_hashes(tmp_path):
    path = tmp_path / "LOT 30.pdf"
    path.write_bytes(b"old document")
    first = lot_identity.bind_outcome(ParseOutcome(data=desc(), source="regex"), path)
    path.write_bytes(b"new document")
    second = lot_identity.bind_outcome(ParseOutcome(data=desc().model_copy(update={"lot_number": "29"}), source="regex"), path)
    pdf_entries = [item for item in second.data._source_identity["lot"] if item["source"] == "pdf"]
    assert {e["value"] for e in pdf_entries} == {"26", "29"}
    assert len({e["sha256"] for e in pdf_entries}) == 2
    assert first.data._source_identity != second.data._source_identity


def test_unsupported_identity_record_is_not_overwritten(tmp_path):
    path = tmp_path / "LOT 30.pdf"
    path.write_bytes(b"pdf")
    passport = path.with_suffix(".json")
    original = {"file": path.name, "identity": {"lot": [{"future": "format"}]}}
    passport.write_text(json.dumps(original))
    outcome = lot_identity.bind_outcome(ParseOutcome(data=desc(), source="regex"), path)
    assert "не удалось сохранить паспорт" in " ".join(outcome.warnings)
    assert json.loads(passport.read_text()) == original


def test_failed_passport_write_does_not_break_parse(tmp_path, monkeypatch):
    path = tmp_path / "LOT 30.pdf"
    path.write_bytes(b"pdf")
    def fail(*args, **kwargs):
        raise PermissionError("read only")
    monkeypatch.setattr(fm, "_write_json", fail)
    result = lot_identity.bind_outcome(ParseOutcome(data=desc(), source="regex"), path)
    assert result.data.lot_number == "26" and result.data._source_lot_number == "30"
    assert any("read only" in w for w in result.warnings)


def test_observations_reach_both_advisors_and_survive_fixes(tmp_path):
    from acad_builder import AcadBuilder
    from conflict_resolver import (Change, LotInput, _Scene, apply_changes, build_advisor_request,
                                   build_join_request, advisor_cache_key)
    from llm_parser import load_config
    path = tmp_path / "LOT 30.pdf"
    path.write_bytes(b"pdf")
    result = lot_identity.bind_outcome(ParseOutcome(data=desc(), source="llm"), path)
    changed = apply_changes(result.data, (Change(target="side", index=0, field="distance", old_label="1", new_label="2", distance_ft=2),))
    assert changed._source_identity == result.data._source_identity
    assert changed._source_file_sha256 == result.data._source_file_sha256
    main = LotInput("main", "OUR 32.pdf", True, desc())
    lot = LotInput("other", path.name, False, result.data)
    scene = _Scene(AcadBuilder(load_config()), [main, lot], 16)
    advisor, _, labels = build_advisor_request(scene, [], ["overlap"], None)
    join = build_join_request(scene, [], ["overlap"], labels)
    for text in (advisor, join):
        assert 'identity observations (unverified)' in text and '"source": "ai"' in text
        assert '"borough"' in text and '"block"' in text and '"lot"' in text
        assert 'may be historical' in text and result.data._source_file_sha256 in text
    old_key = advisor_cache_key([main, lot])
    lot.description._source_identity["borough"].append({"value": "Bronx", "source": "portal"})
    assert advisor_cache_key([main, lot]) != old_key


def test_replacing_pdf_archives_approval_instead_of_reusing_it(tmp_path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"old")
    folder = tmp_path / "deeds"
    rec = deed_store.store_pdf(folder, source, role="our", lot=32, block=12, tag="NEW")
    passport = folder / rec.sidecar_name()
    data = json.loads(passport.read_text())
    data["accepted"] = True
    data["checks"] = {"reviewed": True}
    passport.write_text(json.dumps(data))
    source.write_bytes(b"new")
    deed_store.store_pdf(folder, source, role="our", lot=32, block=12, tag="NEW")
    data = json.loads(passport.read_text())
    assert data["accepted"] is False and data["checks"] == {}
    assert data["document_history"][0]["accepted"] is True
    assert data["document_history"][0]["checks"] == {"reviewed": True}
    assert len(data["document_history"][0]["content_sha256"]) == 64


def test_in_place_pdf_change_invalidates_old_approval_and_preserves_history(tmp_path):
    path = tmp_path / "LOT 30.pdf"
    path.write_bytes(b"old")
    passport = path.with_suffix(".json")
    passport.write_text(json.dumps({"file": path.name, "accepted": True, "checks": {"ok": True}}))
    lot_identity.bind_outcome(ParseOutcome(data=desc(), source="regex"), path)
    assert json.loads(passport.read_text())["accepted"] is True
    path.write_bytes(b"new")
    lot_identity.bind_outcome(ParseOutcome(data=desc(), source="regex"), path)
    data = json.loads(passport.read_text())
    assert data["accepted"] is False and data["checks"] == {}
    assert data["document_history"][0]["accepted"] is True
    assert data["document_history"][0]["changed_file"] == path.name


def test_manifest_approval_is_reset_when_trimmed_pdf_changes(tmp_path):
    source, trim = tmp_path / "source.pdf", tmp_path / "trim.pdf"
    source.write_bytes(b"full")
    trim.write_bytes(b"trim one")
    folder = tmp_path / "deeds"
    rec = deed_store.store_pdf(folder, source, role="our", lot=32, block=12, tag="NEW", pages="1", trimmed_source=trim)
    passport = folder / rec.sidecar_name()
    raw = json.loads(passport.read_text())
    raw["accepted"] = True
    raw["checks"] = {"ok": True}
    passport.write_text(json.dumps(raw))
    deed_store.write_manifest(folder)
    (folder / rec.trimmed).write_bytes(b"trim changed")
    lot_identity.bind_outcome(ParseOutcome(data=desc(), source="regex"), folder / rec.trimmed)
    raw = json.loads(passport.read_text())
    manifest = deed_store.read_manifest(folder)
    assert raw["accepted"] is False and raw["checks"] == {}
    assert manifest["items"][0]["accepted"] is False and manifest["items"][0]["checks"] == {}
    assert manifest["items"][0]["file_digests"] == raw["file_digests"]


def test_expected_digest_prevents_recording_stale_outcome(tmp_path):
    path = tmp_path / 'OUR 30.pdf'
    path.write_bytes(b'original')
    old_digest = fm._digest(path)
    path.write_bytes(b'replaced')
    outcome = ParseOutcome(data=desc(), source='llm')
    with pytest.raises(ValueError, match='changed'):
        fm.record_outcome(path, outcome, expected_digest=old_digest)
    assert not path.with_suffix('.json').exists()
    assert outcome.data._source_file_sha256 == ''
