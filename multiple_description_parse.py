"""Experimental, network-agnostic orchestration for parsing all descriptions.

This module is not connected to the application or AutoCAD. Callers must supply
whole-page images and an explicit transport; no paid calls are made on import.
The existing one-description parser remains unchanged.
"""
from __future__ import annotations

from multiple_description_cancel import check

from dataclasses import dataclass
import json
import math
import re
from typing import Any, Callable, Mapping, Sequence

from llm_parser import LegalDescription, Segment
from pydantic import model_validator

MAX_IMAGES = 50
MAX_PARSE_CALLS = 4
MAX_ORDINARY_FAILURES = 3
SCHEMA_VERSION = "multiple-legal-descriptions-v3"
INSTRUCTION = (
    "Read all supplied whole PDF pages. Return every separate land boundary "
    "description as a separate item in document order, including repeated "
    "descriptions. Do not select a single lot. Keep each description's sides "
    "together and in order. Lot identifiers from the filename and portal are "
    "context only, not a reason to exclude a description. Do not invent "
    "unreadable sides or claim a cut-off description is complete. Return JSON "
    "with descriptions (each item: complete, data) and complete (boolean). "
    "For corner_to_pob_bearing give a bearing only when the document explicitly "
    "states that bearing from the intersection to POB; otherwise use null. "
    "Do not calculate or infer this bearing from a frontage or from a word direction."
)


COMPARISON_INSTRUCTION = (
    'Compare the candidate parses against the supplied whole PDF pages. '
    'Select one candidate only, do not combine them. Each candidate lists the pages '
    'it originally received. If unable to decide, return selected as null. '
    'Return only JSON {"selected": <zero-based candidate index or null>}.'
)


def system_instruction() -> str:
    """Exact static parsing prompt; changes invalidate the diagnostic cache."""
    schema_data = LegalDescription.model_json_schema()
    schema_data['properties']['corner_to_pob_bearing']['description'] = (
        'Bearing from intersection to POB only if explicitly stated in the document; otherwise null. '
        'Never infer from frontage or a word direction.'
    )
    schema = json.dumps(schema_data, ensure_ascii=False, separators=(',', ':'))
    return (INSTRUCTION + '\nEach item data must conform to this JSON schema: ' + schema +
            '\nUse exactly the keys descriptions and complete at the top level, and exactly '
            'the keys complete and data for each item. Return JSON only.')


class ParseDisabled(RuntimeError):
    """The developer switch is off."""


class ImageSizeRejected(Exception):
    """The provider explicitly rejected request size, not a general failure."""

    def __init__(self, limit: int | None = None):
        super().__init__("request size rejected")
        self.limit = limit


@dataclass(frozen=True)
class PageImage:
    number: int
    data_url: str


class PartialLegalDescription(LegalDescription):
    """A validated open traverse; never infer missing sides or a closure."""

    @model_validator(mode="after")
    def _check_geometry(self) -> "PartialLegalDescription":
        if not self.segments:
            raise ValueError("a partial description needs at least one known side")
        if self.front_segment_index is not None and self.front_segment_index >= len(self.segments):
            self.front_segment_index = None
        if self.cross_street_side_index is not None and self.cross_street_side_index >= len(self.segments):
            self.cross_street_side_index = None
        return self


@dataclass(frozen=True)
class DescriptionItem:
    data: LegalDescription
    complete: bool


@dataclass(frozen=True)
class Attempt:
    pages: tuple[int, ...]
    items: tuple[DescriptionItem, ...]
    complete: bool
    valid_response: bool
    issue: str | None


@dataclass(frozen=True)
class ComparisonDiagnostic:
    """Sanitized metadata about the optional one-shot candidate comparison."""
    status: str
    attempt_indices: tuple[int, ...]
    pages: tuple[int, ...]


@dataclass(frozen=True)
class ParseResult:
    items: tuple[DescriptionItem, ...]
    pages: tuple[int, ...]
    unparsed_pages: tuple[int, ...]
    complete: bool
    attempts: tuple[Attempt, ...]
    selected_attempt: int | None
    selection: str | None
    error: str | None
    comparison: ComparisonDiagnostic | None = None


def _validated_segment(raw: Any) -> Segment:
    segment = Segment.model_validate(raw)
    if not math.isfinite(segment.distance_ft) or not all(math.isfinite(x) for x in segment.vector):
        raise ValueError("non-finite side")
    return segment


def _validated_description(model: type[LegalDescription], raw: Any) -> LegalDescription:
    # A legible traversal may have no readable street name. This is an empty
    # metadata field, not an inferred street or a reason to discard sides.
    normalized_raw = {"main_street": "", **raw} if isinstance(raw, dict) else raw
    description = model.model_validate(normalized_raw)
    # The legacy model derives a tie from the traverse when none was supplied.
    # In this workflow such an inferred value is not a confirmed intersection:
    # never turn that default into an apparent relative position or cache it as
    # if the AI explicitly returned the tie.
    if isinstance(raw, dict) and raw.get("corner_to_pob_bearing") is None:
        description.corner_to_pob_bearing = None
    if not math.isfinite(description.distance_to_corner_ft):
        raise ValueError("non-finite tie distance")
    if description.corner_to_pob_bearing is not None:
        _validated_segment(description.corner_to_pob_bearing)
    return description


def _items(raw: Any) -> tuple[tuple[DescriptionItem, ...], bool]:
    if not isinstance(raw, list):
        return (), False
    result = []
    all_valid = True
    for entry in raw:
        if (not isinstance(entry, dict) or set(entry) != {"data", "complete"}
                or not isinstance(entry["complete"], bool)):
            all_valid = False
            continue
        try:
            # LegalDescription's legacy normalizer silently drops empty sides.
            # In this workflow that could connect non-adjacent sides, so every
            # original element must validate before model-level normalization.
            raw_data = entry["data"]
            segments = raw_data.get("segments") if isinstance(raw_data, dict) else None
            if not isinstance(segments, list) or not segments:
                raise ValueError("missing sides")
            for segment in segments:
                _validated_segment(segment)
            model = LegalDescription if entry["complete"] else PartialLegalDescription
            result.append(DescriptionItem(_validated_description(model, raw_data), entry["complete"]))
        except Exception:  # validation errors belong only to this item
            all_valid = False
            # Keep a continuous, independently valid prefix of sides if the
            # rest of the same element is damaged. Never skip an invalid side
            # and connect later sides to an invented position.
            data = entry["data"]
            segments = data.get("segments") if isinstance(data, dict) else None
            if not isinstance(segments, list):
                continue
            prefix = []
            for segment in segments:
                try:
                    _validated_segment(segment)
                except Exception:
                    break
                prefix.append(segment)
            if not prefix:
                continue
            if len(prefix) == len(segments) and (not entry["complete"] or len(prefix) >= 3):
                continue
            try:
                partial = _validated_description(PartialLegalDescription, {**data, "segments": prefix})
            except Exception:
                continue
            result.append(DescriptionItem(partial, False))
    return tuple(result), all_valid


def _recover_prefix(text: str) -> list[Any]:
    """Recover only complete top-level array items in the exact expected prefix.

    Never search for arbitrary brace fragments or repair an incomplete item.
    The strict prefix deliberately rejects ambiguous JSON property order.
    """
    match = re.match(r'\s*\{\s*"descriptions"\s*:\s*\[', text)
    if match is None:
        return []
    decoder = json.JSONDecoder()
    position = match.end()
    recovered: list[Any] = []
    while True:
        while position < len(text) and text[position].isspace():
            position += 1
        if position >= len(text) or text[position] != '{':
            break
        try:
            value, end = decoder.raw_decode(text, position)
        except (ValueError, RecursionError):
            break
        # An item is reliable only if its boundary is followed by a separator.
        after = end
        while after < len(text) and text[after].isspace():
            after += 1
        if after >= len(text) or text[after] not in ',]':
            break
        recovered.append(value)
        if text[after] == ']':
            break
        position = after + 1
    return recovered


def read_response(raw: str, pages: Sequence[int]) -> Attempt:
    """Validate entries independently; never store raw response or image data."""
    if not isinstance(raw, str):
        return Attempt(tuple(pages), (), False, False, "invalid_response")
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        items, _ = _items(_recover_prefix(raw))
        return Attempt(tuple(pages), items, False, False, "invalid_json")
    if (not isinstance(value, dict) or set(value) != {"descriptions", "complete"}
            or not isinstance(value["complete"], bool)):
        return Attempt(tuple(pages), (), False, False, "invalid_envelope")
    items, all_valid = _items(value["descriptions"])
    if not all_valid:
        return Attempt(tuple(pages), items, False, False, "invalid_item")
    if not value["complete"] and not items:
        return Attempt(tuple(pages), (), False, False, "no_usable_result")
    # A complete document cannot contain an item explicitly marked incomplete.
    if value["complete"] and any(not item.complete for item in items):
        return Attempt(tuple(pages), items, False, False, "inconsistent_completeness")
    return Attempt(tuple(pages), items, value["complete"], True, None)


def _closure_score(attempt: Attempt) -> tuple[int, float]:
    """More measurable complete contours first, then smaller worst error."""
    closures = []
    for item in attempt.items:
        if (not item.complete or len(item.data.segments) < 3
                or any(not segment.has_bearing or segment.bearing_solved for segment in item.data.segments)):
            # A nominal/closure-derived direction cannot establish a measured
            # misclosure. In particular, unknown must never rank as zero.
            continue
        vectors = [segment.vector for segment in item.data.segments]
        distance = math.hypot(sum(v[0] for v in vectors), sum(v[1] for v in vectors))
        if math.isfinite(distance):
            closures.append(distance)
    return (len(closures), -max(closures) if closures else 0.0)


def parse_pages(
    config: Mapping[str, Any] | None,
    pages: Sequence[PageImage],
    *,
    image_limit: int,
    filename_lot: str | None,
    portal_lot: str | None,
    search_complete: bool,
    candidate_pages: Sequence[int] | None = None,
    send: Callable[[Sequence[PageImage], Mapping[str, str | None]], str],
    choose: Callable[[Sequence[PageImage], Sequence[Attempt]], int] | None = None,
    current_job: Callable[[], bool] = lambda: True,
) -> ParseResult:
    """Run a single document's experimental parsing policy.

    ``send`` is one provider call, with no hidden retries. ``choose`` is an
    optional single comparison call; return an index into the tied attempts.
    Their provider, authentication and persistence are the caller's concern.
    Explicit failure returns no geometry, not a claim that descriptions are absent.
    """
    section = config.get("multiple_description_search") if isinstance(config, Mapping) else None
    if not isinstance(section, Mapping) or section.get("enabled") is not True:
        raise ParseDisabled("Developer multiple-description switch is disabled.")
    check(current_job)
    if isinstance(image_limit, bool) or not isinstance(image_limit, int) or image_limit < 1:
        raise ValueError("image_limit must be a positive integer")
    if not isinstance(search_complete, bool):
        raise ValueError("search_complete must be a boolean")
    if any(not isinstance(p, PageImage) or isinstance(p.number, bool) or not isinstance(p.number, int)
           or p.number < 1 or not isinstance(p.data_url, str) or not p.data_url.startswith("data:image/")
           for p in pages):
        raise ValueError("invalid whole-page image")
    numbers = [p.number for p in pages]
    if numbers != sorted(set(numbers)):
        raise ValueError("pages must be unique and in PDF order")
    if not pages:
        raise ValueError("no available page images")
    candidates = list(numbers if candidate_pages is None else candidate_pages)
    if (not candidates or candidates != sorted(set(candidates))
            or any(isinstance(n, bool) or not isinstance(n, int) or n < 1 for n in candidates)
            or not set(numbers).issubset(candidates)):
        raise ValueError("candidate pages must be ordered and include all available pages")
    current = list(pages[:min(MAX_IMAGES, image_limit)])
    context = {"filename_lot": filename_lot, "portal_lot": portal_lot}
    attempts: list[Attempt] = []
    ordinary_failures = 0
    reduced = False
    calls = 0
    error: str | None = None
    while calls < MAX_PARSE_CALLS and ordinary_failures < MAX_ORDINARY_FAILURES:
        check(current_job)
        calls += 1
        try:
            response = send(tuple(current), context)
        except ImageSizeRejected as exc:
            attempts.append(Attempt(tuple(p.number for p in current), (), False, False, "size_rejected"))
            if reduced or len(current) <= 1:
                error = "size_rejected"
                break
            if isinstance(exc.limit, int) and not isinstance(exc.limit, bool) and 0 < exc.limit < len(current):
                count = exc.limit
            else:
                count = math.floor(len(current) * .7)
            if count < 1 or count >= len(current):
                error = "size_rejected"
                break
            current = current[:count]
            reduced = True
            error = "size_rejected"
            continue
        except Exception:  # do not persist provider messages, keys or response bodies
            attempts.append(Attempt(tuple(p.number for p in current), (), False, False, "request_failed"))
            ordinary_failures += 1
            error = "request_failed"
            continue
        check(current_job)
        attempt = read_response(response, [p.number for p in current])
        attempts.append(attempt)
        if attempt.valid_response:
            selected = len(attempts) - 1
            rest = tuple(n for n in candidates if n not in attempt.pages)
            return ParseResult(attempt.items, attempt.pages, rest,
                               attempt.complete and search_complete and not rest,
                               tuple(attempts), selected, "valid_response", None)
        ordinary_failures += 1
        error = attempt.issue

    check(current_job)
    # Keep one attempt, never concatenate attempts or deduplicate within one.
    viable = [i for i, attempt in enumerate(attempts) if attempt.items]
    if not viable:
        return ParseResult((), (), tuple(candidates), False, tuple(attempts), None, None, error or "no_result")
    largest = max(len(attempts[i].items) for i in viable)
    tied = [i for i in viable if len(attempts[i].items) == largest]
    selection = "most_items"
    comparison = None
    if len(tied) > 1:
        chosen = None
        # Use the largest page set from a usable tied attempt, never add pages.
        longest = max((attempts[i].pages for i in tied), key=len)
        status = "unavailable"
        if choose is not None:
            page_by_number = {p.number: p for p in pages}
            try:
                index = choose(tuple(page_by_number[n] for n in longest), tuple(attempts[i] for i in tied))
                if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(tied):
                    chosen = tied[index]
                    selection = "comparison"
                    status = "selected"
                else:
                    status = "invalid_selection"
            except ImageSizeRejected:
                status = "size_rejected"
            except (ValueError, TypeError):
                status = "invalid_selection"
            except Exception:
                status = "request_failed"
        comparison = ComparisonDiagnostic(status, tuple(tied), tuple(longest) if choose is not None else ())
        check(current_job)
        if chosen is None:
            chosen = max(tied, key=lambda i: _closure_score(attempts[i]))
            best_score = _closure_score(attempts[chosen])
            selection = ("closure_fallback_uncertain" if sum(_closure_score(attempts[i]) == best_score for i in tied) > 1
                         else "closure_fallback")
    else:
        chosen = tied[0]
    selected_attempt = attempts[chosen]
    rest = tuple(n for n in candidates if n not in selected_attempt.pages)
    return ParseResult(selected_attempt.items, selected_attempt.pages, rest, False,
                       tuple(attempts), chosen, selection, error, comparison)
