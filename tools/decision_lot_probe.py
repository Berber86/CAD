#!/usr/bin/env python3
"""Opt-in experiment: ask decision models about specific lots in a whole PDF.

Not part of the production pipeline; supports 1–8 pages, no automatic batching.
Images are transmitted only with --live; no response bodies or images are saved.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import developer_probability as dp  # noqa: E402
from tools import decision_batch_probe as batch  # noqa: E402
from tools import jev_page_probe as common  # noqa: E402


def questions_for(pages: list[dict[str, Any]], lots: list[str]) -> dict[str, dict[str, str]]:
    numbers = [p["page"] for p in pages]
    if not 1 <= len(numbers) <= dp.ROUTERAI_IMAGE_LIMIT or len(set(numbers)) != len(numbers):
        raise ValueError("Expected one to eight distinct pages")
    if not lots or len(lots) > 10 or len(set(lots)) != len(lots):
        raise ValueError("Expected one to ten distinct lot numbers")
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,19}", lot) for lot in lots):
        raise ValueError("Unsupported lot number")
    questions = {}
    for i, lot in enumerate(lots, 1):
        context = (f"Considering all supplied PDF page images together, does the document contain "
                   f"a metes-and-bounds legal description of Lot {lot}? An address, lot reference, "
                   "map or deed title alone is not a metes-and-bounds description. "
                   "Do not infer that an unrelated parcel belongs to this lot.")
        questions[f"lot_{i}_document"] = {"type": "noul", "instructions": context}
        for n in numbers:
            questions[f"lot_{i}_page_{n}"] = {
                "type": "noul", "instructions": (
                    f"Considering all supplied PDF page images together, does PDF page {n} contain "
                    f"any part of a metes-and-bounds legal description of Lot {lot}, including "
                    "a continuation whose lot may only be identified on another supplied page? "
                    "An address, lot reference, map or deed title alone is not a description. "
                    "Do not infer that an unrelated parcel belongs to this lot.")}
    return questions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path, help="PDF of at most eight pages")
    parser.add_argument("--lot", required=True, action="append", help="Lot number; repeat for each lot")
    parser.add_argument("--model", choices=("both", "jev", "microsoft"), default="both")
    parser.add_argument("--live", action="store_true", help="send paid requests and PDF images to RouterAI")
    args = parser.parse_args(argv)
    lots = [lot.upper() for lot in args.lot]
    try:
        questions_for([{"page": 1}], lots)
    except ValueError as exc:
        parser.error(str(exc))
    if args.pdf.suffix.lower() != ".pdf":
        parser.error("expected a PDF")
    try:
        import pymupdf
        with pymupdf.open(args.pdf) as doc:
            if doc.needs_pass or not 1 <= len(doc) <= dp.ROUTERAI_IMAGE_LIMIT:
                raise ValueError("unsupported PDF")
        pages = dp.prepare_documents([(str(args.pdf), "")], "visual", provider="routerai")
        # Do not leak the actual filename (often contains a target lot) into state.
        pages = [{**page, "file": "document.pdf"} for page in pages]
        questions = questions_for(pages, lots)
    except Exception:
        print("Cannot prepare PDF; use an accessible, unencrypted PDF of 1–8 pages. No requests sent.", file=sys.stderr)
        return 2
    models = ([common.PROVIDERS["routerai"][1], common.ROUTERAI_MICROSOFT] if args.model == "both" else
              [common.PROVIDERS["routerai"][1] if args.model == "jev" else common.ROUTERAI_MICROSOFT])
    metadata = {"provider": "routerai", "pages": [p["page"] for p in pages],
                "lots": {f"lot_{i}": lot for i, lot in enumerate(lots, 1)},
                "question_count_per_request": len(questions)}
    if not args.live:
        print(json.dumps({"mode": "dry-run", **metadata, "models": models,
                          "question_ids": list(questions), "planned_requests": len(models),
                          "message": "No API calls made; --live sends all page images to each model."}, indent=2))
        return 0
    try:
        key = common.load_provider_key("routerai")
    except common.KeyUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 2
    results = [batch.probe_model(pages, model, key, send=dp.post_routerai, questions=questions)
               for model in models]
    print(json.dumps({"mode": "live", **metadata, "method": "one_request_per_model", "results": results}, indent=2))
    return 0 if all(r["status"] == "ok" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
