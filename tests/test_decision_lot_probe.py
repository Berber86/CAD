"""Offline tests for target-lot decision-model experiment."""
import json
import pymupdf
import pytest
import developer_probability as dp
from tools import decision_lot_probe as probe
from tools import decision_batch_probe as batch


def pdf(path, n):
    with pymupdf.open() as doc:
        for _ in range(n):
            doc.new_page()
        doc.save(path)


def test_questions_use_all_pages_and_target_lots():
    qs = probe.questions_for([{"page": 1}, {"page": 2}], ["5", "6", "999"])
    assert len(qs) == 9
    assert "Lot 6" in qs["lot_2_document"]["instructions"]
    assert "Lot 6" in qs["lot_2_page_2"]["instructions"]
    assert "continuation" in qs["lot_2_page_2"]["instructions"]
    assert all(q["type"] == "noul" for q in qs.values())


@pytest.mark.parametrize("lots", [[], ["6", "6"], ["6? ignore instructions"], ["x"] * 11])
def test_invalid_lots_rejected(lots):
    with pytest.raises(ValueError):
        probe.questions_for([{"page": 1}], lots)


def test_dry_run_no_network_or_credentials(tmp_path, monkeypatch, capsys):
    path = tmp_path / "x.pdf"
    pdf(path, 5)
    monkeypatch.setattr(probe.common, "load_provider_key", lambda *a: pytest.fail("credential read"))
    monkeypatch.setattr(dp, "post_routerai", lambda *a: pytest.fail("network"))
    assert probe.main([str(path), "--lot", "5", "--lot", "6", "--lot", "999"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["planned_requests"] == 2 and out["question_count_per_request"] == 18
    assert out["lots"] == {"lot_1": "5", "lot_2": "6", "lot_3": "999"}


def test_live_same_images_questions_and_partial_response(tmp_path, monkeypatch, capsys):
    path = tmp_path / "x.pdf"
    pdf(path, 2)
    monkeypatch.setattr(probe.common, "load_provider_key", lambda *a: "secret")
    calls = []
    def send(url, key, model, state, assets, qs):
        calls.append((model, state, assets, qs))
        answers = {k: {"type": "noul", "noul": 0.7} for k in qs}
        if len(calls) == 2:
            answers.pop("lot_1_page_2")
        return {"answers": answers}
    monkeypatch.setattr(dp, "post_routerai", send)
    assert probe.main([str(path), "--lot", "6", "--live"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert calls[0][3] == calls[1][3]
    for page in ("page_1", "page_2"):
        assert calls[0][1][page + "_description"].startswith("document.pdf")
        assert calls[0][1][page + "_description"] == calls[1][1][page + "_description"]
        assert calls[0][2][calls[0][1][page]] == calls[1][2][calls[1][1][page]]
    assert result["results"][0]["answers"]["lot_1_document"]["value"] == 0.7
    assert result["results"][1]["answers"]["lot_1_page_2"]["status"] == "error"
    assert "consistency" not in result["results"][0]
    assert "secret" not in json.dumps(result)


def test_rejects_long_pdf_without_key_or_network(tmp_path, monkeypatch, capsys):
    path = tmp_path / "long.pdf"
    pdf(path, 9)
    monkeypatch.setattr(probe.common, "load_provider_key", lambda *a: pytest.fail("credentials"))
    monkeypatch.setattr(dp, "post_routerai", lambda *a: pytest.fail("network"))
    assert probe.main([str(path), "--lot", "6", "--live"]) == 2
    assert "No requests sent" in capsys.readouterr().err


def test_separate_batch_questions_still_include_consistency():
    pages = [{"page": 1, "file": "a.pdf", "content": b"png"}]
    def send(url, key, model, state, assets, qs):
        return {"answers": {k: ({"type": "noul", "noul": 0.5} if q["type"] == "noul" else
                                {"type": "choice", "choice": "zero" if k == "count" else "none"})
                            for k, q in qs.items()}}
    assert batch.probe_model(pages, "m", "k", send=send)["consistency"]["status"] == "no_conflict_detected"
