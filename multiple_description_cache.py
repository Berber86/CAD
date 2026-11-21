"""Local diagnostics and strict cache for experimental multi-description parsing.

No image bytes, source document text, API credentials or raw provider responses
are written. This module is not wired to the existing application workflow.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

import file_metadata
from llm_parser import LegalDescription
from multiple_description_parse import (
    DescriptionItem, INSTRUCTION, PageImage, ParseResult, SCHEMA_VERSION, _items,
)

SIDECAR_SCHEMA = "titlesurveyai.multiple_description_parse.v1"


class CacheError(RuntimeError):
    pass


def diagnostics_path(pdf: str | Path) -> Path:
    path = Path(pdf)
    return path.with_name(path.name + ".multi-parse.json")


def cache_context(*, pdf_sha256: str, pages: Sequence[PageImage], image_settings: Mapping[str, Any],
                  provider: str, model: str, filename_lot: str | None, search_complete: bool,
                  image_limit: int, candidate_pages: Sequence[int] | None = None, instruction: str = INSTRUCTION,
                  response_format: str = SCHEMA_VERSION) -> dict[str, Any]:
    """Compute the full request identity; portal context is intentionally excluded.

    Image hashes also cover the exact prepared images; image parameters and
    requested candidates are retained, even when the successful attempt used a
    smaller prefix following a size rejection.
    """
    if not isinstance(pdf_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", pdf_sha256):
        raise ValueError("invalid SHA-256")
    if not isinstance(provider, str) or not provider or not isinstance(model, str) or not model:
        raise ValueError("provider and model are required")
    if (not isinstance(search_complete, bool) or isinstance(image_limit, bool)
            or not isinstance(image_limit, int) or image_limit < 1):
        raise ValueError("invalid coverage or image limit")
    if filename_lot is not None and not isinstance(filename_lot, str):
        raise ValueError("invalid filename lot")
    if not isinstance(instruction, str) or not instruction or not isinstance(response_format, str) or not response_format:
        raise ValueError("instruction and response format are required")
    if not isinstance(image_settings, Mapping):
        raise ValueError("image settings are required")
    try:
        settings = json.loads(json.dumps(dict(image_settings), sort_keys=True, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("image settings must be JSON-serializable") from exc
    nums = [p.number for p in pages]
    if not nums or nums != sorted(set(nums)) or any(isinstance(n, bool) or not isinstance(n, int) or n < 1 for n in nums):
        raise ValueError("pages must be distinct, positive and ordered")
    if any(not isinstance(p.data_url, str) or not p.data_url.startswith("data:image/") for p in pages):
        raise ValueError("invalid page image")
    candidates = list(nums if candidate_pages is None else candidate_pages)
    if (not candidates or candidates != sorted(set(candidates))
            or any(isinstance(n, bool) or not isinstance(n, int) or n < 1 for n in candidates)
            or not set(nums).issubset(candidates)):
        raise ValueError("invalid candidate pages")
    return {"pdf_sha256": pdf_sha256, "pages": nums, "candidate_pages": candidates,
            "image_sha256": [hashlib.sha256(p.data_url.encode('utf-8')).hexdigest() for p in pages],
            "image_settings": settings, "provider": provider, "model": model,
            "filename_lot": filename_lot, "search_complete": search_complete,
            "image_limit": image_limit, "instruction": instruction, "response_format": response_format,
            "description_schema_sha256": hashlib.sha256(json.dumps(
                LegalDescription.model_json_schema(), sort_keys=True, ensure_ascii=False,
                separators=(',', ':')).encode('utf-8')).hexdigest()}


def fingerprint(context: Mapping[str, Any]) -> str:
    payload = json.dumps(dict(context), ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema": SIDECAR_SCHEMA, "runs": []}
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise CacheError("Cannot read existing multi-parse diagnostics; file not overwritten.") from exc
    if not isinstance(value, dict) or value.get('schema') != SIDECAR_SCHEMA or not isinstance(value.get('runs'), list):
        raise CacheError("Incompatible multi-parse diagnostics; file not overwritten.")
    return value


def record_portal_observation(pdf: str | Path, pdf_sha256: str, lot: str | None) -> None:
    """Keep newly supplied portal identity separately from the parsed context."""
    if lot is None:
        return
    if not isinstance(lot, str):
        raise ValueError("invalid portal lot")
    if file_metadata._digest(Path(pdf)) != pdf_sha256:
        raise CacheError("PDF changed; portal observation not saved.")
    target = diagnostics_path(pdf)
    try:
        with file_metadata._LOCK:
            history = _load(target)
            observations = history.setdefault("portal_observations", [])
            if not isinstance(observations, list):
                raise CacheError("Invalid portal observations; file not overwritten.")
            if observations and not isinstance(observations[-1], dict):
                raise CacheError("Invalid portal observation; file not overwritten.")
            if not observations or observations[-1].get("lot") != lot or observations[-1].get("pdf_sha256") != pdf_sha256:
                observations.append({"created_at": datetime.now(timezone.utc).isoformat(),
                                     "pdf_sha256": pdf_sha256, "lot": lot, "source": "portal"})
                file_metadata._write_json(target, history)
    except OSError as exc:
        raise CacheError("Cannot save portal observation.") from exc


def record_failure(pdf: str | Path, pdf_sha256: str, *, reason: str, pages: Sequence[int]) -> None:
    """Record a sanitized failure when no parser request can be formed."""
    if reason not in {"images_unavailable", "processing_failed", "runtime_unavailable", "metadata_unavailable"}:
        raise ValueError("unsupported diagnostic failure")
    if file_metadata._digest(Path(pdf)) != pdf_sha256:
        raise CacheError("PDF changed; failure not saved.")
    target = diagnostics_path(pdf)
    try:
        with file_metadata._LOCK:
            history = _load(target)
            history["runs"].append({"created_at": datetime.now(timezone.utc).isoformat(),
                                    "pdf_sha256": pdf_sha256, "cacheable": False,
                                    "error": reason, "unparsed_pages": list(pages)})
            file_metadata._write_json(target, history)
    except OSError as exc:
        raise CacheError("Cannot save parse failure.") from exc


def _serialized_items(items: Sequence[DescriptionItem]) -> list[dict[str, Any]]:
    return [{"complete": i.complete, "data": i.data.model_dump(mode='json')} for i in items]


def save_result(pdf: str | Path, context: Mapping[str, Any], result: ParseResult, *,
                portal_lot: str | None) -> None:
    """Append one result or sanitized failure; do not overwrite incompatible files."""
    if portal_lot is not None and not isinstance(portal_lot, str):
        raise ValueError("invalid portal lot")
    identity = fingerprint(context)
    target = diagnostics_path(pdf)
    if file_metadata._digest(Path(pdf)) != context.get('pdf_sha256'):
        raise CacheError("PDF changed during parsing; result not saved.")
    attempts = [{"pages": list(a.pages), "item_count": len(a.items), "complete": a.complete,
                 "valid_response": a.valid_response, "issue": a.issue} for a in result.attempts]
    # Failure is recorded for diagnostics but never used as a cache hit.
    cacheable = result.selected_attempt is not None and (bool(result.items) or result.complete)
    run = {"created_at": datetime.now(timezone.utc).isoformat(), "fingerprint": identity,
           "context": dict(context), "portal_lot": portal_lot, "cacheable": cacheable,
           "items": _serialized_items(result.items), "pages": list(result.pages),
           "unparsed_pages": list(result.unparsed_pages), "complete": result.complete,
           "attempts": attempts, "selected_attempt": result.selected_attempt,
           "selection": result.selection, "error": result.error}
    try:
        with file_metadata._LOCK:
            history = _load(target)
            history['runs'].append(run)
            file_metadata._write_json(target, history)
    except OSError as exc:
        raise CacheError("Cannot save multi-parse diagnostics.") from exc


def load_result(pdf: str | Path, context: Mapping[str, Any]) -> tuple[ParseResult, str | None] | None:
    """Return (cached result, original portal lot), or None on a cache miss.

    A newer portal lot intentionally does not invalidate a matching parse; the
    caller must retain its separate source and the old actual model context.
    """
    identity = fingerprint(context)
    if file_metadata._digest(Path(pdf)) != context.get('pdf_sha256'):
        return None
    with file_metadata._LOCK:
        history = _load(diagnostics_path(pdf))
    for run in reversed(history['runs']):
        if not isinstance(run, dict) or run.get('fingerprint') != identity or run.get('context') != dict(context):
            continue
        if run.get('cacheable') is not True:
            continue
        raw_items = run.get('items')
        items, valid = _items(raw_items)
        if not valid or not isinstance(raw_items, list):
            continue
        page_list = run.get('pages')
        unparsed = run.get('unparsed_pages')
        available = context.get('pages')
        candidates = context.get('candidate_pages')
        complete = run.get('complete')
        if (not isinstance(page_list, list) or not isinstance(unparsed, list)
                or not isinstance(candidates, list) or not isinstance(available, list) or not isinstance(complete, bool)
                or not page_list or page_list != available[:len(page_list)]
                or unparsed != [n for n in candidates if n not in page_list]
                or (complete and (unparsed or not context.get('search_complete') or any(not i.complete for i in items)))
                or (not items and not complete)):
            continue
        portal = run.get('portal_lot')
        if portal is not None and not isinstance(portal, str):
            continue
        # Cache hits do not fabricate prior attempts. Their full history remains
        # in the sidecar; selection is marked as cache to the caller.
        return (ParseResult(items, tuple(page_list), tuple(unparsed), complete, (), None,
                            'cache', None), portal)
    return None
