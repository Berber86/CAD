"""Offline tests: boundary fixture and opt-in batch experiment."""
import json
import pymupdf
import pytest
import developer_probability as dp
from tools import decision_lot_boundary_probe as probe
from tools import make_lot_boundary_pdf as fixture


def make(path, count=15):
    if count == 15:
        assert fixture.main([str(path)]) == 0
    else:
        with pymupdf.open() as doc:
            for _ in range(count):
                doc.new_page()
            doc.save(path)


def test_fixture_page_ground_truth_and_no_overwrite(tmp_path, capsys):
    path = tmp_path / "fixture.pdf"
    make(path)
    info = json.loads(capsys.readouterr().out)
    assert info["lots"] == {"5": [7], "6": [8, 9, 10], "999": []}
    original = path.read_bytes()
    with pymupdf.open(path) as doc:
        assert len(doc) == 15
        texts = [p.get_text() for p in doc]
    assert "LOT 5" in texts[6] and "LOT 6" in texts[7]
    assert all("THENCE" in texts[n - 1] for n in (9, 10))
    assert "point of beginning" in texts[9]
    assert not any("THENCE" in t for i, t in enumerate(texts, 1) if i not in (7, 9, 10))
    with pytest.raises(SystemExit):
        fixture.main([str(path)])
    assert path.read_bytes() == original


def test_dry_run_fixed_overlap_and_no_network(tmp_path, capsys, monkeypatch):
    path = tmp_path / "fixture.pdf"
    make(path)
    capsys.readouterr()
    monkeypatch.setattr(probe.common, "load_provider_key", lambda *a: pytest.fail("credentials"))
    monkeypatch.setattr(dp, "post_routerai", lambda *a: pytest.fail("network"))
    assert probe.main([str(path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["batch_pages"] == [list(range(1, 9)), list(range(8, 16))]
    assert out["planned_requests"] == 4 and out["question_count_per_request"] == [27, 27]
    assert out["document_answers_scope"] == "supplied_batch_only"


def test_live_mocks_identical_batches_and_keeps_partial(tmp_path, capsys, monkeypatch):
    path = tmp_path / "lot6-secret-filename.pdf"
    make(path)
    capsys.readouterr()
    monkeypatch.setattr(probe.common, "load_provider_key", lambda *a: "secret")
    calls = []
    def send(url, key, model, state, assets, qs):
        calls.append((model, state, assets, qs))
        answers = {k: {"type": "noul", "noul": 0.7} for k in qs}
        if len(calls) == 2:
            answers.pop("lot_2_page_8")
        return {"answers": answers}
    monkeypatch.setattr(dp, "post_routerai", send)
    assert probe.main([str(path), "--live"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert len(calls) == 4
    assert [b["status"] for r in out["results"] for b in r["batches"]] == ["ok", "partial", "ok", "ok"]
    assert out["results"][0]["batches"][1]["answers"]["lot_2_page_8"]["status"] == "error"
    for a, b in ((calls[0], calls[2]), (calls[1], calls[3])):
        assert a[3] == b[3] and len(a[2]) == len(b[2]) == 8
        for i in range(1, 9):
            label = f"page_{i}"
            assert a[1][label + "_description"] == b[1][label + "_description"]
            assert a[1][label + "_description"].startswith("document.pdf")
            assert a[2][a[1][label]] == b[2][b[1][label]]
        assert "supplied pages contain" in a[3]["lot_2_document"]["instructions"]
    assert calls[0][2][calls[0][1]["page_8"]] == calls[1][2][calls[1][1]["page_1"]]
    assert "PDF page 8" in calls[0][3]["lot_2_page_8"]["instructions"]
    assert "PDF page 8" in calls[1][3]["lot_2_page_8"]["instructions"]
    assert "PDF page 15" in calls[1][3]["lot_2_page_15"]["instructions"]
    assert "secret" not in json.dumps(out)


def test_wrong_size_rejected_without_network_or_key(tmp_path, capsys, monkeypatch):
    path = tmp_path / "small.pdf"
    make(path, 8)
    monkeypatch.setattr(probe.common, "load_provider_key", lambda *a: pytest.fail("credentials"))
    monkeypatch.setattr(dp, "post_routerai", lambda *a: pytest.fail("network"))
    assert probe.main([str(path), "--live"]) == 2
    assert "No requests sent" in capsys.readouterr().err


def test_jev_only_dry_run_two_requests(tmp_path, capsys):
    path = tmp_path / "fixture.pdf"
    make(path)
    capsys.readouterr()
    assert probe.main([str(path), "--model", "jev"]) == 0
    assert json.loads(capsys.readouterr().out)["planned_requests"] == 2


def test_failed_request_does_not_suppress_remaining_batches(tmp_path, capsys, monkeypatch):
    import httpx
    path = tmp_path / "fixture.pdf"
    make(path)
    capsys.readouterr()
    monkeypatch.setattr(probe.common, "load_provider_key", lambda *a: "secret")
    calls = []
    def send(url, key, model, state, assets, qs):
        calls.append(qs)
        if len(calls) == 1:
            raise httpx.TimeoutException("private information")
        return {"answers": {k: {"type": "noul", "noul": 0.4} for k in qs}}
    monkeypatch.setattr(dp, "post_routerai", send)
    assert probe.main([str(path), "--live"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert len(calls) == 4
    assert out["results"][0]["batches"][0]["error_kind"] == "timeout"
    assert out["results"][0]["batches"][1]["status"] == "ok"
    assert "private information" not in json.dumps(out)
