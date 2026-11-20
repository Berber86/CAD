"""Per-document parcel observations in the existing JSON passport.

Observations are not verified facts: independent and conflicting readings are kept.
No arbitrary JSON file is replaced, and a trimmed PDF is linked only by an explicit
passport reference, never by guessing a name.
"""
from __future__ import annotations

import copy
import hashlib
import functools
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Mapping

_LOCK = threading.RLock()
FIELDS = ("block", "lot", "borough")


def observation(value: Any, source: str, *, method: str = "", file: str = "", digest: str = "") -> dict[str, str] | None:
    value = str(value or "").strip()
    if not value:
        return None
    result = {"value": value, "source": source}
    if method:
        result["method"] = method
    if file:
        result["file"] = file
    if digest:
        result["sha256"] = digest
    return result


def add(identity: dict[str, list[dict[str, str]]], field: str, entry: dict[str, str] | None) -> None:
    if field not in FIELDS or entry is None:
        return
    bucket = identity.setdefault(field, [])
    if entry not in bucket:
        bucket.append(entry)


def combine(*identities: Any) -> dict[str, list[dict[str, str]]]:
    result: dict[str, Any] = {}
    for identity in identities:
        if not isinstance(identity, Mapping):
            continue
        for field, value in identity.items():
            if field not in FIELDS:
                # Unknown extension fields belong to a newer schema: keep them intact.
                if field not in result:
                    result[field] = copy.deepcopy(value)
                continue
            if not isinstance(value, list):
                raise ValueError(f"неподдерживаемый формат identity.{field}")
            for entry in value:
                if not isinstance(entry, dict) or not entry.get("value") or not entry.get("source"):
                    raise ValueError(f"неподдерживаемая запись identity.{field}")
                add(result, field, copy.deepcopy(entry))
    return result


def with_lock(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        with _LOCK:
            return fn(*args, **kwargs)
    return wrapped


def filename_observations(path: str | Path) -> dict[str, list[dict[str, str]]]:
    from lot_identity import lot_from_filename

    name = Path(path).name
    result: dict[str, list[dict[str, str]]] = {}
    add(result, "lot", observation(lot_from_filename(name), "filename", file=name))
    stem = Path(name).stem
    blocks = re.findall(r"(?<![A-Za-z0-9])BLOCK[\s_#№:-]*(\d+[A-Za-z]?)(?![A-Za-z0-9]|[-–]\d)", stem, re.I)
    if blocks and len({x.upper().lstrip('0') for x in blocks}) == 1:
        add(result, "block", observation(blocks[0].upper(), "filename", file=name))
    boroughs = re.findall(r"(?<![A-Za-z0-9])BOROUGH[\s_:-]+(STATEN[\s_]+ISLAND|MANHATTAN|BROOKLYN|QUEENS|(?:THE[\s_]+)?BRONX)(?![A-Za-z])", stem, re.I)
    if boroughs and len({b.replace('_', ' ').upper() for b in boroughs}) == 1:
        add(result, "borough", observation(boroughs[0].replace('_', ' ').title(), "filename", file=name))
    return result


def _read_json(path: Path) -> dict[str, Any]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"не удалось прочитать паспорт {path.name}: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError(f"паспорт {path.name} не является JSON-объектом")
    return obj


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False, prefix=".passport-", suffix=".tmp") as handle:
            temp_name = handle.name
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def _passport(path: Path) -> tuple[Path, dict[str, Any]]:
    matches = []
    for candidate in path.parent.glob("*.json"):
        if candidate.name == "_manifest.json":
            continue
        try:
            raw = _read_json(candidate)
        except ValueError:
            # A broken unrelated JSON is not a match. A colliding target is checked below.
            continue
        if path.name in (raw.get("file"), raw.get("trimmed")):
            matches.append((candidate, raw))
    if len(matches) > 1:
        raise ValueError(f"несколько паспортов ссылаются на {path.name}; нужно сверить вручную")
    if matches:
        return matches[0]
    candidate = path.with_suffix(".json")
    if candidate.exists():
        raise ValueError(f"{candidate.name} уже существует и не принадлежит {path.name}")
    return candidate, {"file": path.name, "standalone": True}


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def record(path: str | Path, observations: Mapping[str, Any], *, digest: str = "") -> dict[str, list[dict[str, str]]]:
    """Merge evidence into a document passport; sync matching manifest entry only."""
    path = Path(path)
    if not path.is_file():
        return combine(observations)
    with _LOCK:
        passport, raw = _passport(path)
        if "identity" in raw and (not isinstance(raw["identity"], dict) or
                                  any(not isinstance(raw["identity"].get(field, []), list) for field in FIELDS)):
            raise ValueError(f"формат identity в {passport.name} не поддерживается; паспорт не изменён")
        hashes = raw.get("file_digests", {})
        if not isinstance(hashes, dict) or not isinstance(raw.get("document_history", []), list):
            raise ValueError(f"формат истории в {passport.name} не поддерживается; паспорт не изменён")
        if digest:
            old_hash = str(hashes.get(path.name) or "")
            if not old_hash:
                for field in FIELDS:
                    for entry in raw.get("identity", {}).get(field, []):
                        if isinstance(entry, dict) and entry.get("file") == path.name and entry.get("sha256"):
                            old_hash = entry["sha256"]
            if old_hash and old_hash != digest:
                snapshot = {key: copy.deepcopy(value) for key, value in raw.items() if key != "document_history"}
                snapshot["content_sha256"] = old_hash
                snapshot["changed_file"] = path.name
                raw["document_history"] = [*raw.get("document_history", []), snapshot]
                if "accepted" in raw:
                    raw["accepted"] = False
                if "checks" in raw:
                    raw["checks"] = {}
            raw["file_digests"] = {**hashes, path.name: digest}
        # A legacy block/lot has unknown origin, never assume it came from the portal.
        legacy: dict[str, list[dict[str, str]]] = {}
        if "identity" not in raw:
            for field in FIELDS:
                add(legacy, field, observation(raw.get(field), "unknown", method="legacy passport"))
        raw["identity"] = combine(raw.get("identity"), legacy, observations)
        _write_json(passport, raw)
        manifest = path.parent / "_manifest.json"
        if manifest.is_file():
            data = _read_json(manifest)
            items = data.get("items")
            if isinstance(items, list):
                changed = False
                for item in items:
                    if isinstance(item, dict) and item.get("file") == raw.get("file"):
                        for key in ("identity", "file_digests", "document_history", "accepted", "checks"):
                            if key in raw:
                                item[key] = copy.deepcopy(raw[key])
                        changed = True
                if changed:
                    _write_json(manifest, data)
        return raw["identity"]


def record_outcome(path: str | Path, outcome: Any) -> dict[str, list[dict[str, str]]]:
    path = Path(path)
    entries = filename_observations(path)
    source = str(getattr(outcome, "source", "") or "").lower()
    if source.startswith("llm"):
        origin, method = "ai", source
    elif source == "regex":
        hint = str(getattr(outcome, "_identity_input_method", "") or "")
        if hint == "cache":
            origin, method = "unknown", "step 0 text cache; original extraction unknown"
        else:
            origin = "pdf" if path.suffix.lower() == ".pdf" else "file"
            method = "local OCR and parser" if hint == "ocr" else "local parser"
    else:
        origin, method = "unknown", source
    digest = _digest(path) if path.is_file() else ""
    desc = outcome.data
    desc._source_file_sha256 = digest
    for field, attr in (("block", "block_number"), ("lot", "lot_number"), ("borough", "borough_or_city")):
        add(entries, field, observation(getattr(desc, attr, None), origin, method=method, file=path.name, digest=digest))
    return record(path, entries, digest=digest)


def intake_observations(block: int, lot: int, borough: str, neighbours: list[int],
                        address: str, snapshot: tuple[str, Any] | None) -> dict[str, dict]:
    """Match a portal lookup to the still-current UI values; edited fields are manual."""
    portal = snapshot[1] if snapshot and snapshot[0].strip().casefold() == address.strip().casefold() else None
    same = bool(portal and str(block) == str(portal.block) and str(lot) == str(portal.lot))
    method = ("vision" if same and portal.details_source == "vision" else "local OCR")
    result: dict[str, dict] = {}
    for current in [lot, *neighbours]:
        values: dict[str, list[dict[str, str]]] = {}
        add(values, "block", observation(block, "portal" if same else "manual", method=method if same else ""))
        on_map = same and str(current) in portal.neighbours
        if current == lot and same:
            add(values, "lot", observation(current, "portal", method=method))
        elif on_map:
            add(values, "lot", observation(current, "portal", method="vision map (unverified)"))
        else:
            add(values, "lot", observation(current, "manual"))
        borough_matches = same and borough.strip().casefold() == str(portal.borough or "").strip().casefold()
        if borough:
            # The card's borough describes the selected parcel. For neighbours it is
            # shared search context, not proof that their parcels belong there.
            borough_method = method if current == lot else f"{method}; shared search context, unverified"
            add(values, "borough", observation(borough, "portal" if borough_matches else "manual",
                                                method=borough_method if borough_matches else ""))
        result[str(current)] = values
    return result
