#!/usr/bin/env python3
"""Create fictional, labelled PDF cases for optional decision-model experiments."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import pymupdf

LOT5 = """PARCEL I (LOT 5)
BEGINNING AT a point on Main Street.
THENCE North 00 degrees 00 minutes 00 seconds East 100 feet;
THENCE South 90 degrees 00 minutes 00 seconds East 40 feet;
THENCE South 00 degrees 00 minutes 00 seconds West 100 feet;
THENCE North 90 degrees 00 minutes 00 seconds West 40 feet to the point of beginning.
"""
LOT6_START = """PARCEL II (LOT 6)
BEGINNING AT a point on Main Street.
"""
LOT6_END = """THENCE North 00 degrees 00 minutes 00 seconds East 100 feet;
THENCE South 90 degrees 00 minutes 00 seconds East 60 feet;
THENCE South 00 degrees 00 minutes 00 seconds West 100 feet;
THENCE North 90 degrees 00 minutes 00 seconds West 60 feet to the point of beginning.
"""
LOT7 = """PARCEL III (LOT 7)
BEGINNING AT a point on Main Street.
THENCE North 00 degrees 00 minutes 00 seconds East 100 feet;
THENCE South 90 degrees 00 minutes 00 seconds East 70 feet;
THENCE South 00 degrees 00 minutes 00 seconds West 100 feet;
THENCE North 90 degrees 00 minutes 00 seconds West 70 feet to the point of beginning.
"""

# Each case records descriptions and their page locations independently of the
# implementation. No per-page probability or legal conclusion is fabricated.
SCENARIOS = {
    "no_description": {"pages": ["FICTIONAL COVER SHEET\nFile reference 8301.", "End of record. No survey data on this page."], "lots": {}},
    "single": {"pages": [LOT5], "lots": {"5": [1]}},
    "same_page_and_continuation": {
        "pages": [LOT5 + "\n" + LOT6_START, LOT6_END],
        "lots": {"5": [1], "6": [1, 2]},
    },
    "three_descriptions": {
        "pages": [LOT5 + "\n" + LOT6_START, LOT6_END, LOT7],
        "lots": {"5": [1], "6": [1, 2], "7": [3]},
    },
    "long_continuation": {
        "pages": [LOT6_START, LOT6_END.splitlines()[0], "\n".join(LOT6_END.splitlines()[1:3]), LOT6_END.splitlines()[3]],
        "lots": {"6": [1, 2, 3, 4]},
    },
}


def write_text_pdf(path: Path, texts: list[str]) -> None:
    with pymupdf.open() as doc:
        for text in texts:
            page = doc.new_page()
            page.insert_text((36, 60), text, fontsize=9)
        doc.save(path)



def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path, help="directory for generated fictional PDFs")
    args = parser.parse_args()
    paths = {name: args.output_dir / (name + ".pdf") for name in SCENARIOS}
    if any(path.exists() for path in paths.values()):
        parser.error("output file already exists; choose an empty directory")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, path in paths.items():
        write_text_pdf(path, SCENARIOS[name]["pages"])
    print(json.dumps({name: {"file": str(paths[name]), "lots": case["lots"],
                            "positive_pages": sorted({page for pages in case["lots"].values() for page in pages})}
                      for name, case in SCENARIOS.items()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
