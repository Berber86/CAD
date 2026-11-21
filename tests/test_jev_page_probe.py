"""Offline verification of the independent Jev experiment, never a live API."""
from __future__ import annotations

import json

import httpx
import pytest

import developer_probability as dp
from tools import jev_page_probe as probe


@pytest.mark.parametrize("provider", ["hydra", "routerai"])
def test_each_page_sent_separately_in_visual_mode_and_model_scores_preserved(provider):
    calls = []
    pages = [{"file": "synthetic.pdf", "page": p, "content": b"image" + bytes([p])}
             for p in (1, 2, 3)]
    def send(url, key, model, state, assets, questions):
        calls.append((state, assets, questions))
        assert len(assets) == 1 and list(questions) == ["q1"]
        assert list(questions.values())[0]["type"] == "noul"
        return {"answers": {"q1": {"type": "noul", "noul": [0.1, 0.8, 0.4][len(calls) - 1]}}}
    result = probe.probe_pages(pages, provider=provider, api_key="test", base_url="https://example.test",
                               model="model", send=send)
    assert len(calls) == 3
    assert [x["probability"] for x in result["pages"]] == [0.1, 0.8, 0.4]
    assert result["all_selected_pages_succeeded"] is True


def test_missing_invalid_or_failed_answers_are_not_interpreted_as_zero():
    pages = [{"file": "x.pdf", "page": p, "content": b"image"} for p in range(1, 5)]
    responses = iter([
        {"answers": {"q1": {"type": "noul", "noul": 0.4}}},
        {"answers": {}},
        {"answers": {"q1": {"type": "noul", "noul": 1.5}}},
        httpx.TimeoutException("private info"),
    ])
    def send(*args):
        value = next(responses)
        if isinstance(value, Exception):
            raise value
        return value
    result = probe.probe_pages(pages, provider="routerai", api_key="test", base_url="https://example.test",
                               model="model", send=send)
    assert [r["status"] for r in result["pages"]] == ["ok", "error", "error", "error"]
    assert [r["probability"] for r in result["pages"]] == [0.4, None, None, None]
    assert not result["all_selected_pages_succeeded"]
    assert [r.get("error_kind") for r in result["pages"]] == [None, "invalid_answer", "invalid_answer", "timeout"]
    assert "private info" not in json.dumps(result)


def test_http_422_and_invalid_json_do_not_leak_provider_bodies_and_next_page_runs(monkeypatch):
    pages = [{"file": "x.pdf", "page": p, "content": b"image"} for p in range(1, 4)]
    def post(url, **kwargs):
        post.count += 1
        request = httpx.Request("POST", url)
        if post.count == 1:
            return httpx.Response(422, request=request, text="sensitive provider response")
        if post.count == 2:
            return httpx.Response(200, request=request, text="not json and sensitive")
        return httpx.Response(200, request=request, json={"answers": {"q1": {"type": "noul", "noul": 0.7}}})
    post.count = 0
    monkeypatch.setattr(dp.httpx, "post", post)
    result = probe.probe_pages(pages, provider="routerai", api_key="fake-key",
                               base_url="https://example.test", model="model", send=dp.post_routerai)
    assert [{k: v for k, v in page.items() if k != "seconds"} for page in result["pages"]] == [
        {"page": 1, "status": "error", "probability": None, "http_status": 422, "error_kind": "http_error"},
        {"page": 2, "status": "error", "probability": None, "http_status": 200, "error_kind": "invalid_response"},
        {"page": 3, "status": "ok", "probability": 0.7},
    ]
    assert all(page["seconds"] >= 0 for page in result["pages"])
    assert post.count == 3
    assert "sensitive" not in json.dumps(result)


def test_experiment_requires_opt_in_and_has_no_network_in_dry_run(tmp_path, monkeypatch, capsys):
    import pymupdf
    path = tmp_path / "demo.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page()
        pdf.save(path)
    monkeypatch.setattr(dp, "post_routerai", lambda *a, **kw: pytest.fail("network call in dry run"))
    assert probe.main([str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "dry-run" and result["planned_requests"] == 2
    assert result["models"] == ["typesafe/jev-1.14", probe.ROUTERAI_MICROSOFT]
    assert result["planned_requests_per_model"] == 1


def test_both_models_receive_identical_inputs_even_when_first_model_fails(tmp_path, monkeypatch, capsys):
    import pymupdf
    path = tmp_path / "demo.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page()
        pdf.save(path)
    monkeypatch.setenv("ROUTERAI_API_KEY", "secret")
    bodies = []
    def post(url, **kwargs):
        body = json.loads(kwargs["content"])
        bodies.append(body)
        request = httpx.Request("POST", url)
        if body["model"] == "typesafe/jev-1.14":
            return httpx.Response(422, request=request, text="private")
        return httpx.Response(200, request=request, json={"answers": {"q1": {"type": "noul", "noul": 0.8}}})
    monkeypatch.setattr(dp.httpx, "post", post)
    assert probe.main([str(path), "--live"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert [r["model"] for r in result["results"]] == ["typesafe/jev-1.14", probe.ROUTERAI_MICROSOFT]
    assert [r["pages"][0]["status"] for r in result["results"]] == ["error", "ok"]
    assert bodies[0]["state"] == bodies[1]["state"]
    assert bodies[0]["questions"] == bodies[1]["questions"]
    assert "secret" not in json.dumps(result) and "private" not in json.dumps(result)


@pytest.mark.parametrize("provider", ["hydra", "routerai"])
def test_real_scan_pages_travel_through_each_adapter_with_mock_http(provider, monkeypatch):
    from pathlib import Path
    pdf = Path(__file__).parent / "data" / "multiple_legal" / "OUR 6 NEW.pdf"
    pages = dp.prepare_documents([(str(pdf), "")], "visual", provider=provider)
    calls = []
    # Stub probabilities are artificial. This exercises transport only, not Jev accuracy.
    fake_scores = [0.01, 0.85, 0.75, 0.10, 0.20]
    def post(url, **kwargs):
        body = json.loads(kwargs["content"])
        calls.append((url, body, kwargs["headers"]))
        return httpx.Response(200, request=httpx.Request("POST", url),
                              json={"answers": {"q1": {"type": "noul", "noul": fake_scores[len(calls) - 1]}}})
    monkeypatch.setattr(dp.httpx, "post", post)
    base, model, _ = probe.PROVIDERS[provider]
    send = dp.post_routerai if provider == "routerai" else dp.post_systemone
    result = probe.probe_pages(pages, provider=provider, api_key="fake-key", base_url=base, model=model, send=send)
    assert [p["page"] for p in result["pages"]] == [1, 2, 3, 4, 5]
    assert [p["probability"] for p in result["pages"]] == fake_scores
    assert all(len(call[1]["questions"]) == 1 for call in calls)
    for url, body, headers in calls:
        if provider == "routerai":
            assert url.endswith("/decisions") and "assets" not in body
            assert any(part.get("type") == "image_url" for part in body["state"])
        else:
            assert url.endswith("/systemone") and len(body["assets"]) == 1
            assert headers["TypeSafe-Danger-Zone-Images"] == "true"
    assert "fake-key" not in json.dumps(result)


def test_hydra_cannot_silently_substitute_another_model(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        probe.main([str(tmp_path / "input.pdf"), "--provider", "hydra", "--model", "both"])
    assert exc.value.code == 2
    assert "Microsoft comparison requires RouterAI" in capsys.readouterr().err


def test_live_without_configured_key_is_rejected_before_any_requests(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ROUTERAI_API_KEY", raising=False)
    monkeypatch.setattr(dp, "prepare_documents", lambda *a, **kw: pytest.fail("must stop before preparation"))
    assert probe.main([str(tmp_path / "input.pdf"), "--live"]) == 2
    assert "No requests sent" in capsys.readouterr().err
