"""Offline tests: multi-page decision-model experiment, no live API."""
import json
from pathlib import Path

import httpx
import pymupdf
import pytest

import developer_probability as dp
from tools import decision_batch_probe as batch


def make_pdf(path: Path, count: int) -> None:
    with pymupdf.open() as doc:
        for _ in range(count):
            doc.new_page()
        doc.save(path)


def answer_for(questions):
    return {key: ({"type": "noul", "noul": 0.75} if question["type"] == "noul" else
                  {"type": "choice", "choice": ("two" if key == "count" else "p1")})
            for key, question in questions.items()}


def test_structured_page_count_and_description_location_questions():
    pages = [{"page": n, "file": "a.pdf", "content": b"png"} for n in [1, 2, 3]]
    qs = batch.questions_for(pages)
    assert len(qs) == 10
    assert {"zero", "one", "two", "three", "four_plus", "uncertain"} == set(qs["count"]["criteria"])
    assert all(qs[f"page_{n}"]["type"] == "noul" for n in (1, 2, 3))
    assert set(qs["description_2_last"]["criteria"]) == {"p1", "p2", "p3", "none", "uncertain"}
    seen = []
    def send(base, key, model, state, assets, questions):
        seen.append((state, assets, questions))
        return {"answers": answer_for(questions)}
    result = batch.probe_model(pages, "model", "key", send=send)
    assert len(seen) == 1 and len(seen[0][1]) == 3
    assert result["status"] == "ok"
    assert result["answers"]["count"]["value"] == "two"
    assert result["answers"]["description_2_last"]["value"] == "p1"
    assert result["answers"]["page_3"]["value"] == 0.75


def test_partial_answer_does_not_discard_valid_values_or_leak_secrets():
    pages = [{"page": 1, "file": "a.pdf", "content": b"png"}]
    def send(base, key, model, state, assets, qs):
        answers = answer_for(qs)
        answers["count"] = {"type": "choice", "choice": "private-key"}
        del answers["description_2_last"]
        return {"answers": answers, "private": "private-key"}
    result = batch.probe_model(pages, "model", "private-key", send=send)
    assert result["status"] == "partial"
    assert result["answers"]["count"] == {"status": "error", "value": None}
    assert result["answers"]["description_2_last"]["status"] == "error"
    assert result["answers"]["page_1"]["value"] == 0.75
    assert "private-key" not in json.dumps(result)


def test_two_models_receive_identical_page_images_and_questions_despite_error(tmp_path, monkeypatch, capsys):
    path = tmp_path / "fixture.pdf"
    make_pdf(path, 2)
    monkeypatch.setattr(batch.common, "load_provider_key", lambda p: "secret")
    calls = []
    def post(url, **kwargs):
        body = json.loads(kwargs["content"])
        calls.append(body)
        request = httpx.Request("POST", url)
        if len(calls) == 1:
            return httpx.Response(422, request=request, text="private data")
        return httpx.Response(200, request=request, json={"answers": answer_for(body["questions"])})
    monkeypatch.setattr(dp.httpx, "post", post)
    assert batch.main([str(path), "--live"]) == 1
    output = json.loads(capsys.readouterr().out)
    assert [x["status"] for x in output["results"]] == ["error", "ok"]
    assert output["results"][0]["http_status"] == 422
    assert calls[0]["state"] == calls[1]["state"]
    assert calls[0]["questions"] == calls[1]["questions"]
    assert len([x for x in calls[0]["state"] if x["type"] == "image_url"]) == 2
    assert "private" not in json.dumps(output) and "secret" not in json.dumps(output)


def test_dry_run_does_not_read_key_or_call_network(tmp_path, monkeypatch, capsys):
    path = tmp_path / "fixture.pdf"
    make_pdf(path, 5)
    monkeypatch.setattr(batch.common, "load_provider_key", lambda *a: pytest.fail("no credentials"))
    monkeypatch.setattr(dp, "post_routerai", lambda *a: pytest.fail("no network"))
    assert batch.main([str(path)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["planned_requests"] == 2 and data["pages"] == [1, 2, 3, 4, 5]
    assert len(data["question_ids"]) == 12


def test_rejects_long_pdf_before_key_or_network(tmp_path, monkeypatch, capsys):
    path = tmp_path / "long.pdf"
    make_pdf(path, 9)
    monkeypatch.setattr(batch.common, "load_provider_key", lambda *a: pytest.fail("too many pages"))
    monkeypatch.setattr(dp, "post_routerai", lambda *a: pytest.fail("no network"))
    assert batch.main([str(path), "--live"]) == 2
    assert "No requests sent" in capsys.readouterr().err


def test_missing_key_rejects_without_network(tmp_path, monkeypatch, capsys):
    path = tmp_path / "small.pdf"
    make_pdf(path, 1)
    def no_key(provider):
        raise batch.common.KeyUnavailable("No key. No requests sent.")
    monkeypatch.setattr(batch.common, "load_provider_key", no_key)
    monkeypatch.setattr(dp, "post_routerai", lambda *a: pytest.fail("no network"))
    assert batch.main([str(path), "--live"]) == 2
    assert "No requests sent" in capsys.readouterr().err


def normalized_choices(count, pairs):
    values = {"count": count}
    for n, (first, last) in enumerate(pairs, 1):
        values[f"description_{n}_first"] = first
        values[f"description_{n}_last"] = last
    return {k: {"status": "ok", "value": v} for k, v in values.items()}


@pytest.mark.parametrize("count,pairs,expected", [
    ("one", [("p1", "p4"), ("none", "none"), ("p3", "p3")],
     "description_3_exceeds_count"),
    ("two", [("p2", "p3"), ("p2", "p3"), ("none", "p3")],
     "description_3_none_conflicts_with_page"),
    ("one", [("p1", "none"), ("none", "none"), ("none", "none")],
     "description_1_none_conflicts_with_page"),
    ("two", [("none", "p1"), ("p1", "p2"), ("none", "none")],
     "description_1_missing_despite_count"),
    ("one", [("p3", "p1"), ("none", "none"), ("none", "none")],
     "description_1_reversed_page_range"),
    ("two", [("p3", "p3"), ("p1", "p2"), ("none", "none")],
     "description_2_starts_before_description_1"),
])
def test_reports_actual_model_contradictions_and_impossible_ranges(count, pairs, expected):
    check = batch.check_consistency(normalized_choices(count, pairs))
    assert check["status"] == "inconsistent" and expected in check["issues"]


def test_no_conflict_is_not_claim_of_factual_accuracy():
    # Both descriptions start on page 1: possible, even if in the labelled
    # fixture the first actually ends on page 1 rather than page 2.
    answers = normalized_choices("three", [("p1", "p2"), ("p1", "p2"), ("p3", "p3")])
    assert batch.check_consistency(answers) == {"status": "no_conflict_detected", "issues": []}


def test_unknown_and_partial_values_do_not_fabricate_absence():
    answers = normalized_choices("uncertain", [("uncertain", "uncertain"), ("none", "none"), ("none", "none")])
    assert batch.check_consistency(answers) == {"status": "unknown", "issues": []}
    del answers["description_2_last"]
    assert batch.check_consistency(answers) == {"status": "unknown", "issues": []}


def test_count_four_plus_does_not_require_or_invent_fourth_location():
    answers = normalized_choices("four_plus", [("p1", "p1"), ("p1", "p2"), ("p3", "p3")])
    assert batch.check_consistency(answers) == {"status": "no_conflict_detected", "issues": []}
    answers["description_2_first"]["value"] = "none"
    assert "description_2_missing_despite_count" in batch.check_consistency(answers)["issues"]


def test_probe_reports_logical_conflicts_without_discarding_valid_answers():
    pages = [{"page": n, "file": "demo.pdf", "content": b"png"} for n in (1, 2, 3)]
    def send(base, key, model, state, assets, qs):
        answers = answer_for(qs)
        answers["description_3_first"] = {"type": "choice", "choice": "none"}
        answers["description_3_last"] = {"type": "choice", "choice": "p3"}
        return {"answers": answers}
    result = batch.probe_model(pages, "model", "key", send=send)
    assert result["status"] == "ok"  # format only, intentionally not changed
    assert result["consistency"]["status"] == "inconsistent"
    assert "description_3_none_conflicts_with_page" in result["consistency"]["issues"]
    assert result["answers"]["description_3_last"]["value"] == "p3"
