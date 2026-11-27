"""Experimental, network-agnostic orchestration for parsing all descriptions.

This module is not connected to the application or AutoCAD. Callers must supply
whole-page images and an explicit transport; no paid calls are made on import.
The existing one-description parser remains unchanged.
"""
from __future__ import annotations

from multiple_description_cancel import check
from multiple_description_progress import emit, page_list, waiting
from multiple_description_errors import classify_exception, describe_failure

from dataclasses import dataclass, replace
import json
import math
import re
import time
from typing import Any, Callable, Mapping, Sequence

from llm_parser import LegalDescription, Segment
from pydantic import model_validator
from legal_street_policy import NYC_STREET_INSTRUCTION

MAX_IMAGES = 50
MAX_PARSE_CALLS = 4
MAX_ORDINARY_FAILURES = 3
SCHEMA_VERSION = "multiple-legal-descriptions-v4"
INSTRUCTION = (
    "You are a senior licensed New York / USA land-survey technician. "
    "Read all supplied whole PDF pages. Return every separate land boundary "
    "description as a separate item in document order, including repeated "
    "descriptions. Do not select a single lot. Keep each description's sides "
    "together and in continuous loop order. Lot identifiers from the filename "
    "and portal are context only, not a reason to exclude a description. Do not "
    "invent unreadable sides or claim a cut-off description is complete. Return "
    "JSON with descriptions (each item: complete, data) and complete (boolean). "
    "For corner_to_pob_bearing give a bearing only when the document explicitly "
    "states that bearing from the intersection to POB; otherwise use null. "
    "Do not calculate or infer this bearing from a frontage or from a word direction. "
    "SURVEY EXTRACTION RULES: "
    "(1) Orientation & North arrow first: if a scan or survey plat is tilted, sideways "
    "(90°/270°), or upside-down (180°), determine true orientation from the title block, "
    "street labels, and drawn NORTH ARROW before determining front_side or directions; "
    "double-check upside-down or faded 1980s glyphs (6<->9, 3<->8, 5<->6, 1<->7, 0<->8). "
    "(2) Tie lines & offsets: NEVER include COMMENCING / corner tie calls from a street "
    "intersection to POB in segments (record the immediate corner distance along main_street "
    "in distance_to_corner_ft, 0.0 for a corner lot, and compound/dual ties in tie_note); "
    "NEVER include building wall dimensions or fence/setback offsets (FE. 0.1'W, GARAGE, "
    "SHED, 2-sty frame) in segments. "
    "(3) Strict verbatim rule: ALWAYS keep the exact printed numbers, bearings, degrees, "
    "minutes, seconds, and quadrant letters in segments even when a polygon does not close; "
    "NEVER silently substitute digits (such as changing 54 to 56) to force closure — report "
    "any suspected typo or blurred glyph in Russian in anomaly_note (otherwise null). "
    "(4) Courses, relative NYC directions & inches: use has_bearing=false when a side uses "
    "cardinal/relative wording (PARALLEL WITH ..., AT RIGHT ANGLES TO ..., interior_angle) "
    "without a printed compass bearing; capture exact vertex interior_angle when shown; "
    "convert feet-and-inches and fractional inches accurately to decimal feet (e.g. "
    "25'-6 1/2\" -> 25.5417) while keeping the printed form in distance_source; set "
    "party_wall=true only when explicitly stated as a party wall; use chord bearing + "
    "chord distance for curved boundaries. "
    "(5) Streets & frontage: keep main_street and cross_street verbatim (putting parenthetical "
    "easements/widths in main_street_note and former/a/k/a street names in main_street_former "
    "/ cross_street_former); pick front_segment_index strictly on main_street (never a "
    "PARALLEL WITH / AT RIGHT ANGLES rear/side wall); for the frontage segment ONLY, set "
    "course_note to the clean printed street name without 'along the northerly side of' or "
    "direction phrases, while keeping non-frontage course_note values verbatim."
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
    return (INSTRUCTION + '\n' + NYC_STREET_INSTRUCTION + '\nEach item data must conform to this JSON schema: ' + schema +
            '\nUse exactly the keys descriptions and complete at the top level, and exactly '
            'the keys complete and data for each item. Return JSON only.')


class ParseDisabled(RuntimeError):
    """The developer switch is off."""


class ImageSizeRejected(Exception):
    """The provider explicitly rejected request size, not a general failure."""

    def __init__(self, limit: int | None = None, *, status_code: int | None = None):
        super().__init__("request size rejected")
        self.limit = limit
        self.status_code = status_code


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
    failure_kind: str | None = None
    elapsed_seconds: float | None = None


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


def extract_json_text(raw: str) -> str:
    """Strip reasoning tags and Markdown fences around a JSON reply."""
    text = raw.strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
    lower = text.lower()
    if "</think>" in lower:
        idx = lower.rfind("</think>")
        text = text[idx + len("</think>") :].strip()
    fences = re.findall(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE)
    for inner in fences:
        stripped = inner.strip()
        if stripped.startswith("{"):
            return stripped
    if fences:
        return fences[0].strip()
    open_fence = re.search(r"```(?:json)?\s*([\s\S]+)$", text, flags=re.IGNORECASE)
    if open_fence:
        return open_fence.group(1).strip()
    return text


def decode_json_object(raw: str, *, expected_keys: frozenset[str] | None = None) -> tuple[Any, str]:
    """Decode a complete JSON value from wrapped/prose text, returning (value, cleaned_text)."""
    cleaned = extract_json_text(raw)
    try:
        return json.loads(cleaned), cleaned
    except (ValueError, RecursionError):
        pass
    decoder = json.JSONDecoder()
    if cleaned.startswith("{"):
        value, end = decoder.raw_decode(cleaned, 0)
        if not isinstance(value, dict) or cleaned[end:].strip().startswith("{"):
            raise ValueError("invalid json envelope")
        return value, cleaned
    fallback_value: Any = None
    has_fallback = False
    pos = cleaned.find("{")
    while pos != -1:
        try:
            value, _end = decoder.raw_decode(cleaned, pos)
        except (ValueError, RecursionError):
            pos = cleaned.find("{", pos + 1)
            continue
        if isinstance(value, dict):
            if expected_keys is None or set(value) == expected_keys:
                return value, cleaned
            if not has_fallback:
                fallback_value = value
                has_fallback = True
        pos = cleaned.find("{", pos + 1)
    if has_fallback:
        return fallback_value, cleaned
    raise ValueError("no json object found")


def read_response(raw: str, pages: Sequence[int]) -> Attempt:
    """Validate entries independently; never store raw response or image data."""
    if not isinstance(raw, str):
        return Attempt(tuple(pages), (), False, False, "invalid_response")
    cleaned = extract_json_text(raw)
    try:
        value, cleaned = decode_json_object(raw, expected_keys=frozenset({"descriptions", "complete"}))
    except (ValueError, RecursionError):
        recovery_text = cleaned
        if not recovery_text.startswith("{"):
            match = re.search(r'\{\s*"descriptions"\s*:\s*\[', recovery_text)
            if match is not None:
                recovery_text = recovery_text[match.start():]
        items, _ = _items(_recover_prefix(recovery_text))
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
    progress: Callable[[str], None] | None = None,
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
        emit(progress, f"Разбор: запрос {calls}, страницы {page_list([p.number for p in current])}; ожидаем ответ.")
        check(current_job)
        started = time.monotonic()
        try:
            with waiting(progress):
                response = send(tuple(current), context)
        except ImageSizeRejected as exc:
            elapsed = round(time.monotonic() - started, 2)
            kind = classify_exception(exc)
            kind = kind if kind.startswith('http_') else 'size_rejected'
            emit(progress, f"Разбор: провайдер отклонил размер запроса, {elapsed:.1f} с.")
            attempts.append(Attempt(tuple(p.number for p in current), (), False, False, "size_rejected", kind, elapsed))
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
        except Exception as exc:  # never persist provider messages, keys or response bodies
            elapsed = round(time.monotonic() - started, 2)
            kind = classify_exception(exc)
            emit(progress, f"Разбор: запрос не выполнен ({describe_failure(kind)}), {elapsed:.1f} с.")
            attempts.append(Attempt(tuple(p.number for p in current), (), False, False, "request_failed", kind, elapsed))
            ordinary_failures += 1
            error = "request_failed"
            if kind in ("http_401", "http_403"):
                emit(progress, "Разбор: доступ отклонён провайдером (ошибка авторизации) — повторы остановлены.")
                break
            continue
        check(current_job)
        attempt = read_response(response, [p.number for p in current])
        attempt = replace(attempt, elapsed_seconds=round(time.monotonic() - started, 2))
        attempts.append(attempt)
        issues = {"invalid_json": "некорректный JSON", "invalid_envelope": "неверная структура ответа",
                  "invalid_item": "повреждено описание", "no_usable_result": "нет пригодных сторон",
                  "inconsistent_completeness": "противоречие в признаке полноты"}
        emit(progress, f"Разбор: получено описаний {len(attempt.items)}, "
                       + ("ответ принят." if attempt.valid_response else
                          f"ответ неполный или повреждён: {issues.get(attempt.issue, 'неясный ответ')}."))
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
            emit(progress, f"Сравнение: запрос выбора из {len(tied)} вариантов; ожидаем ответ.")
            page_by_number = {p.number: p for p in pages}
            check(current_job)
            try:
                with waiting(progress):
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
        result_text = {"selected": "вариант выбран", "invalid_selection": "ответ неясен",
                       "size_rejected": "запрос слишком велик", "request_failed": "запрос не выполнен",
                       "unavailable": "сравнение недоступно"}
        emit(progress, f"Сравнение: {result_text[status]}.")
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
