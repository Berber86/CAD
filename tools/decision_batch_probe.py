#!/usr/bin/env python3
"""Opt-in experiment: compare decision models on up to eight whole PDF pages.

This is not the production pipeline or the agreed batching/retry algorithm.
No document, provider response, API key or PIN is written to disk.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx  # noqa: E402
import developer_probability as dp  # noqa: E402
from tools import jev_page_probe as common  # noqa: E402

COUNT_CHOICES = {
    "zero": "No distinct metes-and-bounds description",
    "one": "Exactly one distinct metes-and-bounds description",
    "two": "Exactly two distinct metes-and-bounds descriptions",
    "three": "Exactly three distinct metes-and-bounds descriptions",
    "four_plus": "Four or more distinct metes-and-bounds descriptions",
    "uncertain": "Cannot determine the number of distinct descriptions from these pages",
}
COUNT_INSTRUCTIONS = (
    "Count distinct metes-and-bounds descriptions of land parcels on the supplied pages. "
    "One description continued over multiple pages counts once. A title, deed cover, "
    "map, address or lot reference alone is not a separate description. "
    "Choose uncertain if you cannot determine the count."
)


def questions_for(pages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    page_numbers = [p["page"] for p in pages]
    if not 1 <= len(pages) <= dp.ROUTERAI_IMAGE_LIMIT or len(set(page_numbers)) != len(pages):
        raise ValueError("Expected one to eight distinct pages")
    qs: dict[str, dict[str, Any]] = {
        "count": {"type": "choice", "instructions": COUNT_INSTRUCTIONS, "criteria": COUNT_CHOICES}
    }
    for number in page_numbers:
        qs[f"page_{number}"] = {"type": "noul", "instructions": f"For PDF page {number}: {common.QUESTION}"}
    criteria = {f"p{n}": f"PDF page {n}" for n in page_numbers}
    criteria.update({"none": "This numbered description does not exist", "uncertain": "Cannot determine"})
    for n in (1, 2, 3):
        for boundary in ("first", "last"):
            qs[f"description_{n}_{boundary}"] = {
                "type": "choice",
                "instructions": (
                    f"Considering distinct metes-and-bounds descriptions in reading order, "
                    f"which supplied PDF page is the {boundary} page containing description number {n}? "
                    "A description continuing on another page is still the same description. "
                    "If it does not exist choose none; if unsure choose uncertain."
                ),
                "criteria": criteria,
            }
    return qs


def check_consistency(answers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Flag logical conflicts, not factual errors; keep all raw normalized choices.

    Unknown/partial answers remain unknown; a clean result cannot establish
    that the identified descriptions or page ranges actually match the PDF.
    """
    def value(key: str) -> Any:
        entry = answers.get(key, {})
        return entry.get("value") if entry.get("status") == "ok" else None

    count_value = value("count")
    known_counts = {"zero": 0, "one": 1, "two": 2, "three": 3, "four_plus": 4}
    count = known_counts.get(count_value)
    unknown = count is None
    issues = []
    previous_start: tuple[int, int] | None = None
    for n in (1, 2, 3):
        first, last = value(f"description_{n}_first"), value(f"description_{n}_last")
        if first in (None, "uncertain") or last in (None, "uncertain"):
            unknown = True
        first_page = int(first[1:]) if isinstance(first, str) and first.startswith("p") and first[1:].isdigit() else None
        last_page = int(last[1:]) if isinstance(last, str) and last.startswith("p") and last[1:].isdigit() else None
        if (first == "none" and last_page is not None) or (last == "none" and first_page is not None):
            issues.append(f"description_{n}_none_conflicts_with_page")
        if count is not None:
            if n <= count and (first == "none" or last == "none"):
                issues.append(f"description_{n}_missing_despite_count")
            if n > count and (first_page is not None or last_page is not None):
                issues.append(f"description_{n}_exceeds_count")
        if first_page is not None and last_page is not None and first_page > last_page:
            issues.append(f"description_{n}_reversed_page_range")
        if first_page is not None:
            if previous_start and first_page < previous_start[1]:
                issues.append(f"description_{n}_starts_before_description_{previous_start[0]}")
            previous_start = (n, first_page)
    return {"status": "inconsistent" if issues else "unknown" if unknown else "no_conflict_detected",
            "issues": issues}


def probe_model(pages: list[dict[str, Any]], model: str, api_key: str,
                send: Callable[..., Any] = dp.post_routerai) -> dict[str, Any]:
    questions = questions_for(pages)
    start = time.monotonic()
    result: dict[str, Any] = {"model": model, "status": "error", "seconds": None}
    try:
        state, assets = dp.make_state(pages, "visual")
        base_url = common.PROVIDERS["routerai"][0]
        data = send(base_url, api_key, model, state, assets, questions)
        answers = data.get("answers")
        if not isinstance(answers, dict):
            raise ValueError("invalid response")
        normalized: dict[str, Any] = {}
        for key, question in questions.items():
            try:
                answer = dp.normalize_answer(answers.get(key), question)
                normalized[key] = {"status": "ok", "value": answer["value"] if question["type"] == "noul" else answer["choice"]}
            except (ValueError, KeyError, TypeError):
                normalized[key] = {"status": "error", "value": None}
        result.update(status="ok" if all(a["status"] == "ok" for a in normalized.values()) else "partial",
                      answers=normalized, consistency=check_consistency(normalized))
    except httpx.HTTPStatusError as exc:
        result.update(error_kind="http_error", http_status=exc.response.status_code)
    except httpx.TimeoutException:
        result["error_kind"] = "timeout"
    except httpx.HTTPError:
        result["error_kind"] = "network_error"
    except dp.InvalidSystemOneResponse as exc:
        result.update(error_kind="invalid_response", http_status=exc.http_response.status_code)
    except Exception:  # no raw provider body, exception text or secret in output
        result["error_kind"] = "processing_error"
    result["seconds"] = round(time.monotonic() - start, 3)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path, help="PDF of at most eight pages")
    parser.add_argument("--live", action="store_true", help="send both paid requests to RouterAI")
    args = parser.parse_args(argv)
    if args.pdf.suffix.lower() != ".pdf":
        parser.error("expected a PDF")
    try:
        # Check the entire PDF count before rendering or requesting a key.
        import pymupdf
        with pymupdf.open(args.pdf) as doc:
            count = len(doc)
            if doc.needs_pass or not 1 <= count <= dp.ROUTERAI_IMAGE_LIMIT:
                raise ValueError("unsupported PDF")
        pages = dp.prepare_documents([(str(args.pdf), "")], "visual", provider="routerai")
    except Exception:
        print("Cannot prepare PDF; use an accessible, unencrypted PDF of 1–8 pages. No requests sent.", file=sys.stderr)
        return 2
    models = [common.PROVIDERS["routerai"][1], common.ROUTERAI_MICROSOFT]
    if not args.live:
        print(json.dumps({"mode": "dry-run", "provider": "routerai", "models": models,
                          "pages": [p["page"] for p in pages], "question_ids": list(questions_for(pages)),
                          "planned_requests": len(models),
                          "message": "No API calls made; --live sends all page images to each model."}, indent=2))
        return 0
    try:
        key = common.load_provider_key("routerai")
    except common.KeyUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 2
    results = [probe_model(pages, model, key, send=dp.post_routerai) for model in models]
    print(json.dumps({"mode": "live", "provider": "routerai", "method": "one_request_per_model",
                      "pages": [p["page"] for p in pages], "location_limit": 3,
                      "results": results}, indent=2))
    return 0 if all(r["status"] == "ok" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
