"""Disabled-by-default, developer-only search for every legal-description page.

This module does not invoke the existing parser or drawing pipeline. It sends
whole page images to RouterAI only when explicitly enabled by its caller.
Diagnostics and strictly matched search results are kept separately from the
PDF passport; no images, OCR text, provider bodies or credentials are stored.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx
import developer_probability as dp
import file_metadata

PRIMARY = "microsoft/microsoft-decision-1.1"
FALLBACK = "typesafe/jev-1.14"
PROVIDER = "routerai"
BASE_URL = "https://routerai.ru/api/v1"
PAGE_CAP = 200
THRESHOLD = 0.30
SCHEMA = "titlesurveyai.legal_page_search.v1"
QUESTION = ("Does this page contain any part of a legal description of a land parcel, "
            "including the beginning or continuation of boundary courses?")
COUNT_INSTRUCTIONS = ("Count distinct metes-and-bounds descriptions of land parcels on the supplied pages. "
                      "One description continued over multiple pages counts once. A title, deed cover, "
                      "map, address or lot reference alone is not a separate description. "
                      "Choose uncertain if you cannot determine the count.")
COUNT_CHOICES = {"zero": "No distinct metes-and-bounds description",
                 "one": "Exactly one distinct metes-and-bounds description",
                 "two": "Exactly two distinct metes-and-bounds descriptions",
                 "three": "Exactly three distinct metes-and-bounds descriptions",
                 "four_plus": "Four or more distinct metes-and-bounds descriptions",
                 "uncertain": "Cannot determine the number of distinct descriptions from these pages"}


class SearchDisabled(RuntimeError):
    """The developer switch is off; no PDF or network access has occurred."""


class SearchError(RuntimeError):
    """Generic, non-sensitive search failure."""


def enabled(config: Mapping[str, Any] | None) -> bool:
    section = config.get("multiple_description_search") if isinstance(config, Mapping) else None
    return isinstance(section, Mapping) and section.get("enabled") is True


def batch_numbers(count: int) -> list[list[int]]:
    if isinstance(count, bool) or not 1 <= count <= PAGE_CAP:
        raise ValueError("invalid page count")
    result = []
    first = 1
    while True:
        last = min(first + dp.ROUTERAI_IMAGE_LIMIT - 1, count)
        result.append(list(range(first, last + 1)))
        if last == count:
            return result
        first = last


def questions_for(numbers: list[int], *, count: bool) -> dict[str, dict[str, Any]]:
    if not numbers or len(numbers) > dp.ROUTERAI_IMAGE_LIMIT or len(set(numbers)) != len(numbers):
        raise ValueError("expected one to eight distinct pages")
    questions = {f"page_{n}": {"type": "noul", "instructions": f"For PDF page {n}: {QUESTION}"}
                 for n in numbers}
    if count:
        questions["count"] = {"type": "choice", "instructions": COUNT_INSTRUCTIONS, "criteria": COUNT_CHOICES}
    return questions


def _fingerprint(digest: str, pages: list[dict[str, Any]], questions: dict[str, Any],
                 model: str, image_side: int, base_url: str) -> str:
    # The exact image bytes and ordered questions/context are part of validity.
    payload = {"pdf_sha256": digest, "pages": [p["page"] for p in pages],
               "image_sha256": [hashlib.sha256(p["content"]).hexdigest() for p in pages],
               "image_side": image_side, "image_format": "png", "provider": PROVIDER,
               "base_url": base_url, "model": model, "questions": questions}
    return hashlib.sha256(json.dumps(payload, sort_keys=False, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def diagnostics_path(pdf: Path) -> Path:
    return pdf.with_name(pdf.name + ".page-search.json")


def _load_history(target: Path) -> dict[str, Any]:
    if not target.exists():
        return {"schema": SCHEMA, "runs": []}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SearchError("Cannot read existing search diagnostics; file was not overwritten.") from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or not isinstance(data.get("runs"), list):
        raise SearchError("Existing search diagnostics have an incompatible format; file was not overwritten.")
    return data


def _cached(history: dict[str, Any], fingerprint: str, questions: dict[str, Any], model: str) -> dict[str, Any]:
    """Reuse only individually valid answers attached to the exact request fingerprint."""
    found: dict[str, Any] = {}
    for run in reversed(history["runs"]):
        if not isinstance(run, dict):
            continue
        stored_attempts = run.get("attempts", [])
        if not isinstance(stored_attempts, list):
            continue
        for attempt in reversed(stored_attempts):
            if not isinstance(attempt, dict) or attempt.get("fingerprint") != fingerprint or attempt.get("model") != model:
                continue
            answers = attempt.get("answers")
            if not isinstance(answers, dict):
                continue
            for key, q in questions.items():
                entry = answers.get(key)
                if key in found or not isinstance(entry, dict) or entry.get("status") != "ok":
                    continue
                value = entry.get("value")
                if q["type"] == "noul":
                    valid = isinstance(value, (float, int)) and not isinstance(value, bool) and 0 <= value <= 1 and math.isfinite(value)
                else:
                    valid = isinstance(value, str) and value in q["criteria"]
                if valid:
                    found[key] = value
    return found


def _request(pages: list[dict[str, Any]], questions: dict[str, Any], model: str,
             key: str, base_url: str, send: Callable[..., Any]) -> tuple[dict[str, Any], str | None, float, dict[str, float | int] | None]:
    start = time.monotonic()
    answers: dict[str, Any] = {}
    error = None
    usage = None
    try:
        state, assets = dp.make_state(pages, "visual")
        data = send(base_url, key, model, state, assets, questions)
        if isinstance(data, dict) and isinstance(data.get("usage"), dict):
            usage = {}
            for field in ("input_tokens", "output_tokens", "total_tokens", "prompt_tokens", "completion_tokens", "cost", "total_cost"):
                value = data["usage"].get(field)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1e18 and math.isfinite(value):
                    usage[field] = value
            usage = usage or None
        raw = data.get("answers") if isinstance(data, dict) else None
        if not isinstance(raw, dict):
            raise ValueError("invalid response")
        for name, question in questions.items():
            try:
                result = dp.normalize_answer(raw.get(name), question)
                answers[name] = {"status": "ok", "value": result["value"] if question["type"] == "noul" else result["choice"]}
            except (ValueError, TypeError, KeyError):
                answers[name] = {"status": "error", "value": None}
    except httpx.HTTPStatusError as exc:
        error = f"http_{exc.response.status_code}"
    except httpx.TimeoutException:
        error = "timeout"
    except httpx.HTTPError:
        error = "network_error"
    except dp.InvalidSystemOneResponse:
        error = "invalid_response"
    except Exception:
        error = "processing_error"
    if error:
        answers = {name: {"status": "error", "value": None} for name in questions}
    return answers, error, round(time.monotonic() - start, 3), usage


def search_pdf(path: str | Path, config: Mapping[str, Any] | None, *, api_key: str,
               base_url: str = BASE_URL, image_side: int = dp.PDF_IMAGE_MAX_SIDE,
               send: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Evaluate up to 200 pages and save a diagnostic sidecar; never build geometry.

    Do not call from the live workflow yet. An unknown page with an available
    image is included in candidate_pages, not classified as empty.
    """
    if not enabled(config):
        raise SearchDisabled("Developer page search is disabled.")
    if not api_key:
        raise SearchError("RouterAI key is unavailable; no requests sent.")
    dp.valid_pdf_image_side(image_side)
    if not base_url.startswith("https://"):
        raise SearchError("An HTTPS provider endpoint is required.")
    path = Path(path)
    if path.suffix.lower() != ".pdf":
        raise SearchError("Expected a PDF.")
    target = diagnostics_path(path)
    # Read and validate the sidecar before any paid request.
    with file_metadata._LOCK:
        history = _load_history(target)
    try:
        import pymupdf
        digest = file_metadata._digest(path)
        with pymupdf.open(path) as doc:
            total = len(doc)
            if doc.needs_pass or total < 1:
                raise ValueError("unreadable PDF")
    except Exception as exc:
        raise SearchError("Cannot open the PDF; no requests sent.") from exc
    covered = min(total, PAGE_CAP)
    groups = batch_numbers(covered)
    rendered: dict[int, dict[str, Any]] = {}
    render_errors = []
    for n in range(1, covered + 1):
        try:
            page = dp.prepare_documents([(str(path), str(n))], "visual", provider=PROVIDER,
                                        pdf_image_side=image_side)
            if len(page) != 1 or page[0]["page"] != n or not isinstance(page[0]["content"], bytes):
                raise ValueError("invalid render")
            rendered[n] = {**page[0], "file": "document.pdf"}
        except Exception:
            render_errors.append(n)
    if file_metadata._digest(path) != digest:
        raise SearchError("PDF changed while preparing pages; no requests sent.")
    send = send or dp.post_routerai
    attempts: list[dict[str, Any]] = []
    results = []
    for group in groups:
        available = [n for n in group if n in rendered]
        pending = list(available)
        observed: dict[int, dict[str, Any]] = {}
        count_result: dict[str, Any] = {"status": "unknown", "value": None}

        def attempt(numbers: list[int], model: str, include_count: bool) -> None:
            nonlocal count_result
            pages = [rendered[n] for n in numbers]
            questions = questions_for(numbers, count=include_count)
            fp = _fingerprint(digest, pages, questions, model, image_side, base_url)
            cached = _cached(history, fp, questions, model)
            if cached:
                answers = {name: ({"status": "ok", "value": cached[name]} if name in cached else
                                  {"status": "error", "value": None}) for name in questions}
                error, seconds, source, usage = None, 0.0, "cache", None
            else:
                answers, error, seconds, usage = _request(pages, questions, model, api_key, base_url, send)
                source = "network"
            attempts.append({"batch_pages": group, "pages": numbers, "model": model,
                             "fingerprint": fp, "source": source, "seconds": seconds, "usage": usage,
                             "error_kind": error, "answers": answers})
            for n in numbers:
                answer = answers[f"page_{n}"]
                if answer["status"] == "ok" and n not in observed:
                    observed[n] = {"value": answer["value"], "model": model, "source": source}
            if include_count and count_result["status"] != "ok" and answers["count"]["status"] == "ok":
                count_result = {"status": "ok", "value": answers["count"]["value"],
                                "model": model, "source": source}

        if pending:
            # A complete initial batch may use Jev cache when its precise request
            # context matches, even though Microsoft is the primary model.
            initial_qs = questions_for(pending, count=len(available) == len(group))
            initial_pages = [rendered[n] for n in pending]
            used_cache = False
            for model in (PRIMARY, FALLBACK):
                fp = _fingerprint(digest, initial_pages, initial_qs, model, image_side, base_url)
                if _cached(history, fp, initial_qs, model):
                    # Keep original context for every cache lookup; do not replace
                    # a previously accepted answer with a later cached answer.
                    attempt(available, model, len(available) == len(group))
                    used_cache = True
            pending = [n for n in available if n not in observed]
            if not used_cache:
                attempt(pending, PRIMARY, len(available) == len(group))
                pending = [n for n in pending if n not in observed]
            # A cache read costs no live attempt. When some cached answers are
            # missing, ask only those pages, without retrying count by itself.
            microsoft_attempts = 0 if used_cache else 1
            while pending and microsoft_attempts < 3:
                attempt(pending, PRIMARY, False)
                microsoft_attempts += 1
                pending = [n for n in pending if n not in observed]
            for _ in range(3):
                if not pending:
                    break
                attempt(pending, FALLBACK, False)
                pending = [n for n in pending if n not in observed]
        results.append({"pages": group, "count": count_result,
                        "observations": {str(n): observed.get(n) for n in group}})
    page_results = []
    for n in range(1, covered + 1):
        obs = [b["observations"][str(n)] for b in results if n in b["pages"]]
        valid = [x["value"] for x in obs if x is not None]
        incomplete = any(x is None for x in obs)
        maximum = max(valid) if valid else None
        candidate = n in rendered and (incomplete or (maximum is not None and maximum >= THRESHOLD))
        page_results.append({"page": n, "observations": obs, "max_probability": maximum,
                             "incomplete": incomplete, "image_available": n in rendered,
                             "candidate": candidate})
    run = {"created_at": datetime.now(timezone.utc).isoformat(), "pdf_sha256": digest,
           "source_name": path.name, "pdf_pages": total, "covered_pages": covered,
           "uncovered_pages": total - covered, "render_errors": render_errors,
           "coverage_complete": total == covered and not any(p["incomplete"] for p in page_results),
           "threshold": THRESHOLD, "batches": results, "pages": page_results,
           "candidate_pages": [p["page"] for p in page_results if p["candidate"]],
           "count_scope": "document" if len(groups) == 1 and total == covered else "batch_only",
           "attempts": attempts}
    try:
        if file_metadata._digest(path) != digest:
            raise SearchError("PDF changed during search; results were not saved.")
        with file_metadata._LOCK:
            current = _load_history(target)
            current["runs"].append(run)
            file_metadata._write_json(target, current)
    except SearchError:
        raise
    except OSError as exc:
        raise SearchError("Cannot save search diagnostics.") from exc
    return copy.deepcopy(run)
