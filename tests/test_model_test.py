"""Тест моделей на построение property line: чистая логика model_test.py."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import model_test as mt


CFG = {
    "api": {
        "provider": "hydra",
        "model": "grok-4.7",
        "vision_max_pages": 8,
        "providers": {
            "hydra": {"models": ["grok-4.7", "gpt-5.5"], "selected_model": "grok-4.7"},
            "openrouter": {"models": ["or-model"], "selected_model": "or-model"},
            "routerai": {"models": ["ra-model"], "selected_model": "ra-model"},
        },
    },
    "drawing": {"base_scale": 16.0},
}


def test_default_slots_start_from_active_model():
    slots = mt.default_slots(CFG)
    assert len(slots) == mt.SLOT_COUNT == 4
    assert (slots[0].provider, slots[0].model) == ("hydra", "grok-4.7")
    providers = [s.provider for s in slots]
    assert "openrouter" in providers and "routerai" in providers


def test_load_slots_roundtrip_and_padding():
    cfg = {"model_test": {"slots": [{"provider": "Hydra", "model": "gpt-5.5",
                                     "reasoning": "high"}]}}
    slots = mt.load_slots(cfg)
    assert len(slots) == 4
    assert slots[0] == mt.ModelTestSlot("hydra", "gpt-5.5", "high")
    assert mt.slots_as_dicts(slots)[0] == {
        "provider": "hydra", "model": "gpt-5.5", "reasoning": "high"}


def test_slot_config_sets_reasoning_without_touching_source():
    slot = mt.ModelTestSlot("hydra", "gpt-5.5", "high")
    out = mt.slot_config(CFG, slot)
    assert out["api"]["provider"] == "hydra"
    assert out["api"]["model"] == "gpt-5.5"
    assert out["api"]["providers"]["hydra"]["reasoning_by_model"]["gpt-5.5"] == "high"
    assert "reasoning_by_model" not in CFG["api"]["providers"]["hydra"]


def test_prepare_input_rejects_unknown_extension(tmp_path: Path):
    bad = tmp_path / "notes.txt"
    bad.write_text("hello", encoding="utf-8")
    with pytest.raises(Exception):
        mt.prepare_input(bad, CFG)


def test_prepare_input_renders_png_as_single_image():
    png = Path(__file__).parent / "data" / "tax_map_manhattan_731_53.png"
    text, urls, notes = mt.prepare_input(png, CFG)
    assert text == ""
    assert len(urls) == 1 and urls[0].startswith("data:image/")


class _FakeBuilder:
    def __init__(self, cfg):
        self.cfg = cfg

    def plan(self, description, scale=None):
        return SimpleNamespace(
            description=description,
            vertices=[(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)],
            closure=SimpleNamespace(distance=0.012, perimeter=40.0),
            area_sqft=100.0,
        )


def _fake_outcome(data=None):
    return SimpleNamespace(
        data=data or SimpleNamespace(segments=[1, 2, 3, 4]),
        usage=SimpleNamespace(available=True, total_tokens=42),
    )


def test_run_all_slots_isolates_failures(monkeypatch):
    calls = []

    def fake_parse(**kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == "bad-model":
            raise RuntimeError("модель недоступна")
        return _fake_outcome()

    monkeypatch.setattr(mt.lp, "parse_legal_description_full", fake_parse)
    monkeypatch.setattr(mt, "AcadBuilder", _FakeBuilder)
    slots = [
        mt.ModelTestSlot("hydra", "m1"),
        mt.ModelTestSlot("hydra", "bad-model"),
        mt.ModelTestSlot("openrouter", "m3"),
        mt.ModelTestSlot("routerai", "m4"),
    ]
    seen = []
    results = mt.run_all_slots(("", ["data:image/png;base64,x"], []), slots, CFG,
                               progress=lambda i, r: seen.append(i))
    assert [r.ok for r in results] == [True, False, True, True]
    assert "модель недоступна" in results[1].error
    assert sorted(calls) == ["bad-model", "m1", "m3", "m4"]
    assert sorted(seen) == [0, 1, 2, 3]
    text = mt.metrics_text(results[0])
    assert "замыкание 0.012 ft" in text and "сторон 4" in text
    assert "ошибка" in mt.metrics_text(results[1])
