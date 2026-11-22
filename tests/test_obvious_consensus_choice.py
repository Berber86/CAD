"""Различать явное преимущество замыкания и неподдающиеся ему разногласия."""
import dataclasses
import pytest
import llm_parser as lp

TEXT = lp.ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"]


def outcomes():
    clean, _ = lp.parse_with_regex(TEXT, auto_repair_typos=False)
    bad = clean.model_copy(deep=True)
    bad.segments[0].distance_ft = 140
    return [lp.ParseOutcome(data=d, source="llm", provider="test", model=f"m{i}")
            for i, d in enumerate([clean, bad], 1)]


def consensus(outs):
    agreed, diffs = lp.compare_legal_descriptions(outs)
    return lp.ConsensusResult(agreed, outs, [], diffs, outs[0], lp.TokenUsage(), prepared_text=TEXT)


@pytest.mark.parametrize("reverse", [False, True])
def test_selects_unmodified_pass_and_reports_both(reverse):
    outs = outcomes()
    if reverse:
        outs.reverse()
    result = consensus(outs)
    assert not result.agreed
    selected = result.obvious_geometry_choice()
    assert selected.data is outs[1 if reverse else 0].data
    assert selected.data.segments[0].distance_ft == 100
    assert not selected.data.segments[0]._auto_corrections
    assert selected.usage is result.total_usage
    report = "\n".join(selected.warnings)
    assert "автоматически выбран" in report and "не консенсус" in report
    assert "40.0000" in report and "100.00" in report and "140.00" in report
    assert "сверьте" in report


@pytest.mark.parametrize("kind", ["near", "both_good", "neither_good", "street", "tie", "other_metadata", "party_wall", "relative", "solved", "side_count", "three", "invalid"])
def test_do_not_choose_from_closure_if_other_factors_are_ambiguous(kind):
    outs = outcomes()
    a, b = [o.data for o in outs]
    if kind == "near": b.segments[0].distance_ft = 100.5
    elif kind == "both_good":
        b.segments[0].distance_ft = 100.01
    elif kind == "neither_good": a.segments[0].distance_ft = 101
    elif kind == "street": b.main_street = "Other street"
    elif kind == "tie": b.distance_to_corner_ft += 5
    elif kind == "other_metadata": b.main_street_former = "Different"
    elif kind == "party_wall": b.segments[1].party_wall = True
    elif kind == "relative": a.segments[1].has_bearing = False
    elif kind == "solved": a.segments[1].bearing_solved = True
    elif kind == "side_count": b.segments.pop()
    elif kind == "three": outs.append(dataclasses.replace(outs[1]))
    elif kind == "invalid":
        # Два встречных прохода по одной линии дают нулевую площадь.
        for o in outs:
            o.data.segments = [lp.Segment(ns="N", ew="E", degrees=0, minutes=0, seconds=0, distance_ft=x) for x in (100, 100, 100)]
        outs[1].data.segments[0].distance_ft = 140
    assert consensus(outs).obvious_geometry_choice() is None


def test_chosen_pass_retains_existing_autocorrection_metadata():
    import drawing_warnings as dw
    outs = outcomes()
    segment = outs[0].data.segments[0]
    before = segment.model_copy(update={"distance_ft": 109.0})
    outs[0].data.segments[0] = dw.mark_auto_correction(before, segment)
    selected = consensus(outs).obvious_geometry_choice()
    assert selected.data.segments[0]._auto_corrections
    assert selected.data.segments[0]._auto_corrections[0].before.distance_ft == 109
