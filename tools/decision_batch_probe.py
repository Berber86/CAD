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
                      answers=normalized)
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
