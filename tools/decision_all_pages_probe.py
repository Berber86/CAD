#!/usr/bin/env python3
"""Opt-in experiment comparing models on all boundary-description pages.

Not the production pipeline. Whole pages are sent only with --live. Outputs
contain numeric usage fields if provided, not raw provider responses or keys.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import developer_probability as dp  # noqa: E402
from tools import decision_batch_probe as batch  # noqa: E402
from tools import jev_page_probe as common  # noqa: E402

PAGE_CAP = 200  # experimental cap, not a production decision
THRESHOLD = 0.30  # exploratory, not validated for all documents


def batch_numbers(count: int) -> list[list[int]]:
    if not 1 <= count <= PAGE_CAP:
        raise ValueError("invalid covered page count")
    groups = []
    start = 1
    while start <= count:
        end = min(start + dp.ROUTERAI_IMAGE_LIMIT - 1, count)
        groups.append(list(range(start, end + 1)))
        if end == count:
            break
        start = end  # one page overlap
    return groups


def questions_for(pages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    numbers = [p["page"] for p in pages]
    if not 1 <= len(numbers) <= dp.ROUTERAI_IMAGE_LIMIT or len(set(numbers)) != len(numbers):
        raise ValueError("expected one to eight distinct pages")
    result = {f"page_{number}": {"type": "noul", "instructions": f"For PDF page {number}: {common.QUESTION}"}
              for number in numbers}
    result["count"] = {"type": "choice", "instructions": batch.COUNT_INSTRUCTIONS,
                       "criteria": batch.COUNT_CHOICES}
    return result


# Only known numeric fields; never print raw usage, response, headers or secrets.
_USAGE_FIELDS = ("input_tokens", "output_tokens", "total_tokens", "prompt_tokens",
                 "completion_tokens", "cost", "total_cost")


def safe_usage(data: dict[str, Any]) -> dict[str, float | int] | None:
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    safe = {}
    for name in _USAGE_FIELDS:
        value = usage.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
            safe[name] = value
    return safe or None


def run_batch(pages: list[dict[str, Any]], model: str, key: str) -> dict[str, Any]:
    usage = None
    def send(*args: Any) -> Any:
        nonlocal usage
        data = dp.post_routerai(*args)
        usage = safe_usage(data)
        return data
    result = batch.probe_model(pages, model, key, send=send, questions=questions_for(pages))
    result["usage"] = usage
    return {"pages": [p["page"] for p in pages], **result}


def aggregate(count: int, batches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    observations: dict[int, list[float | None]] = {n: [] for n in range(1, count + 1)}
    for item in batches:
        for n in item["pages"]:
            entry = item.get("answers", {}).get(f"page_{n}", {})
            observations[n].append(entry.get("value") if entry.get("status") == "ok" else None)
    result = []
    for n, values in observations.items():
        valid = [v for v in values if v is not None]
        maximum = max(valid) if valid else None
        missing = not values or len(valid) != len(values)
        # A positive valid observation is enough for tentative selection, but a
        # missing observation must not turn a non-positive page into a negative.
        decision = "selected" if maximum is not None and maximum >= THRESHOLD else "unknown" if missing else "not_selected"
        result.append({"page": n, "observations": values, "max_probability": maximum,
                       "decision": decision, "incomplete": missing})
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path, help="unencrypted PDF; first 200 pages at most")
    parser.add_argument("--model", choices=("both", "jev", "microsoft"), default="both")
    parser.add_argument("--live", action="store_true", help="send the batches to RouterAI; paid requests possible")
    args = parser.parse_args(argv)
    if args.pdf.suffix.lower() != ".pdf":
        parser.error("expected a PDF")
    try:
        import pymupdf
        with pymupdf.open(args.pdf) as doc:
            total = len(doc)
            if doc.needs_pass or total < 1:
                raise ValueError("inaccessible PDF")
    except Exception:
        print("Cannot open PDF; use an accessible, unencrypted PDF. No requests sent.", file=sys.stderr)
        return 2
    covered = min(total, PAGE_CAP)
    groups = batch_numbers(covered)
    models = ([common.PROVIDERS["routerai"][1], common.ROUTERAI_MICROSOFT] if args.model == "both" else
              [common.PROVIDERS["routerai"][1] if args.model == "jev" else common.ROUTERAI_MICROSOFT])
    info = {"provider": "routerai", "models": models, "pdf_pages": total, "covered_pages": covered,
            "uncovered_pages": total - covered, "batch_pages": groups,
            "threshold": THRESHOLD, "question_count_per_batch": [len(g) + 1 for g in groups],
            "count_scope": "each_batch_only_not_document_total", "planned_requests": len(groups) * len(models)}
    if not args.live:
        print(json.dumps({"mode": "dry-run", **info,
                          "message": "No API calls made; --live sends whole page images and may incur charges."}, indent=2))
        return 0
    try:
        # Render before loading credentials or sending anything. Chunks respect
        # prepare_documents' 100-image limit; reused bytes make overlap identical.
        pages = []
        for first in range(1, covered + 1, dp.MAX_IMAGES):
            last = min(first + dp.MAX_IMAGES - 1, covered)
            pages.extend(dp.prepare_documents([(str(args.pdf), f"{first}-{last}")], "visual", provider="routerai"))
        if [p["page"] for p in pages] != list(range(1, covered + 1)):
            raise ValueError("incomplete preparation")
        pages = [{**p, "file": "document.pdf"} for p in pages]
    except Exception:
        print("Cannot prepare all covered PDF pages. No requests sent.", file=sys.stderr)
        return 2
    try:
        key = common.load_provider_key("routerai")
    except common.KeyUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 2
    results = []
    for model in models:
        runs = [run_batch([pages[n - 1] for n in group], model, key) for group in groups]
        results.append({"model": model, "batches": runs, "pages": aggregate(covered, runs)})
    print(json.dumps({"mode": "live", **info, "method": "one_request_per_batch_per_model",
                      "usage_note": "Provider-reported numeric fields only; null means unavailable. Cost currency/meaning must be verified with provider billing.",
                      "results": results}, indent=2))
    return 0 if all(b["status"] == "ok" for r in results for b in r["batches"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
