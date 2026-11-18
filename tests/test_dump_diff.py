"""Разница дампов «до»/«после» по handle (tests/dump_diff.py)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

import dump_diff as dd  # noqa: E402


def _dump(*entities):
    return {"entities": list(entities)}


def test_new_labels_are_found_by_handle_and_old_objects_are_not_new():
    line = {"type": "Line", "handle": "A1", "layer": "S-FENCE", "pts": [[0, 0], [10, 0]]}
    old_line = dict(line)
    label = {"type": "MText", "handle": "B2", "layer": "D-TOPO-TEXT", "text": "FE.0.2'N", "pt": [4.0, 5.0]}
    result = dd.diff(_dump(old_line), _dump(line, label))
    assert [r["handle"] for r in result["new"]] == ["B2"]
    assert result["changed"] == [] and result["removed"] == []


def test_changed_and_removed_objects_are_reported():
    before = _dump({"type": "Line", "handle": "A1", "layer": "S-FENCE", "pts": [[0, 0], [10, 0]]},
                   {"type": "Line", "handle": "A2", "layer": "S-FENCE", "pts": [[0, 0], [5, 0]]})
    after = _dump({"type": "Line", "handle": "A1", "layer": "S-FENCE", "pts": [[0, 0], [12, 0]]})
    result = dd.diff(before, after)
    assert len(result["changed"]) == 1
    assert [r["handle"] for r in result["removed"]] == ["A2"]


def test_report_prints_label_text_and_point():
    before = _dump()
    after = _dump({"type": "MText", "handle": "B2", "layer": "D-TOPO-TEXT", "text": "3.5'S", "pt": [1.0, 2.0]})
    text = dd.report(before, after)
    assert "3.5'S" in text and "(1.00, 2.00)" in text
