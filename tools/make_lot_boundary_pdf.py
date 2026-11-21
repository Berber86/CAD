#!/usr/bin/env python3
"""Create one fictional 15-page PDF for the optional lot-boundary experiment."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.make_legal_test_pdfs import LOT5, LOT6_START, LOT6_END  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_pdf", type=Path, help="new output PDF; never overwrite")
    parser.add_argument("--start-before-overlap", action="store_true",
                        help="place the description start on page 7, outside the second batch")
    args = parser.parse_args(argv)
    if args.output_pdf.suffix.lower() != ".pdf":
        parser.error("expected a PDF filename")
    if args.output_pdf.exists():
        parser.error("output already exists; choose a new filename")
    texts = [f"FICTIONAL RECORD - PAGE {i}\nAdministrative page without boundary courses."
             for i in range(1, 16)]
    courses = LOT6_END.splitlines()
    if args.start_before_overlap:
        texts[6] = LOT5 + "\n" + LOT6_START
        texts[7] = courses[0]
        texts[8] = "\n".join(courses[1:3])
        texts[9] = courses[3]
    else:
        texts[6] = LOT5
        texts[7] = LOT6_START
        texts[8] = "\n".join(courses[:2])
        texts[9] = "\n".join(courses[2:])
    args.output_pdf.parent.mkdir(parents=True, exist_ok=True)
    # Reserve the name exclusively so an existing file cannot be overwritten.
    try:
        with args.output_pdf.open("xb") as output:
            # PyMuPDF writes to its own path; write the built PDF to bytes instead.
            import pymupdf
            with pymupdf.open() as doc:
                for text in texts:
                    page = doc.new_page()
                    page.insert_text((36, 60), text, fontsize=9)
                output.write(doc.tobytes())
    except FileExistsError:
        parser.error("output already exists; choose a new filename")
    print(json.dumps({"file": str(args.output_pdf), "pages": 15,
                      "lots": {"5": [7], "6": [7, 8, 9, 10] if args.start_before_overlap else [8, 9, 10], "999": []},
                      "positive_pages": [7, 8, 9, 10],
                      "batch_pages": [[1, 2, 3, 4, 5, 6, 7, 8],
                                      [8, 9, 10, 11, 12, 13, 14, 15]]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
