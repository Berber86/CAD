#!/usr/bin/env python3
"""Opt-in experiment with a 15-page PDF: two 8-page batches overlapping at 8.

Tests per-lot page questions, not the production pipeline or retry algorithm.
The generated fixture is optional but supplies the known ground truth. No live
requests without --live; all submitted images may incur charges.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import developer_probability as dp  # noqa: E402
from tools import decision_batch_probe as batch  # noqa: E402
from tools import decision_lot_probe as lot_probe  # noqa: E402
from tools import jev_page_probe as common  # noqa: E402

LOTS = ["5", "6", "999"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path, help="15-page PDF")
    parser.add_argument("--model", choices=("both", "jev", "microsoft"), default="both")
    parser.add_argument("--live", action="store_true", help="send paid requests and PDF images to RouterAI")
    args = parser.parse_args(argv)
    if args.pdf.suffix.lower() != ".pdf":
        parser.error("expected a PDF")
    try:
        import pymupdf
        with pymupdf.open(args.pdf) as doc:
            if doc.needs_pass or len(doc) != 15:
                raise ValueError("expected an unencrypted 15-page PDF")
        pages = dp.prepare_documents([(str(args.pdf), "")], "visual", provider="routerai")
        if [page["page"] for page in pages] != list(range(1, 16)):
            raise ValueError("incomplete page preparation")
        pages = [{**page, "file": "document.pdf"} for page in pages]
        batches = [pages[:8], pages[7:15]]
        questions = [lot_probe.questions_for(group, LOTS) for group in batches]
        # A batch is not the entire PDF; make the document-level question explicit.
        for qs in questions:
            for number in range(1, len(LOTS) + 1):
                entry = qs[f"lot_{number}_document"]
                entry["instructions"] = entry["instructions"].replace(
                    "does the document contain", "do the supplied pages contain")
    except Exception:
        print("Cannot prepare PDF; use an accessible, unencrypted PDF of exactly 15 pages. No requests sent.", file=sys.stderr)
        return 2
    models = ([common.PROVIDERS["routerai"][1], common.ROUTERAI_MICROSOFT] if args.model == "both" else
              [common.PROVIDERS["routerai"][1] if args.model == "jev" else common.ROUTERAI_MICROSOFT])
    info = {"provider": "routerai", "lots": {f"lot_{i}": lot for i, lot in enumerate(LOTS, 1)},
            "batch_pages": [[page["page"] for page in group] for group in batches],
            "overlap_page": 8, "question_count_per_request": [len(q) for q in questions],
            "document_answers_scope": "supplied_batch_only"}
    if not args.live:
        print(json.dumps({"mode": "dry-run", **info, "models": models,
                          "planned_requests": len(models) * len(batches),
                          "message": "No API calls made; --live sends each batch of page images to each model."}, indent=2))
        return 0
    try:
        key = common.load_provider_key("routerai")
    except common.KeyUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 2
    results = []
    for model in models:
        model_batches = []
        for group, qs in zip(batches, questions):
            result = batch.probe_model(group, model, key, send=dp.post_routerai, questions=qs)
            model_batches.append({"pages": [p["page"] for p in group], **result})
        results.append({"model": model, "batches": model_batches})
    print(json.dumps({"mode": "live", **info, "method": "one_request_per_batch_per_model",
                      "results": results}, indent=2))
    return 0 if all(b["status"] == "ok" for r in results for b in r["batches"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
