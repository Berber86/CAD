"""Модели Hydra с Vision+reasoning и проброс reasoning_effort в запросы.

Сеть не используется: провайдер подменяется фейковым клиентом, каталог и
миграция проверяются на локальных данных.
"""
from __future__ import annotations

import copy
import json

import llm_parser as lp
import multiple_description_transport as transport


def _cfg_with_reasoning(model: str = "grok-4.7", effort: str | None = None):
    cfg = lp.load_config()
    if effort is not None:
        cfg["api"]["providers"]["hydra"]["reasoning_by_model"] = {model: effort}
    return cfg


def test_hydra_catalog_only_vision_reasoning_models():
    models = lp.PROVIDERS_CATALOG["hydra"]["models"]
    # Каждая модель списка имеет настраиваемые рассуждения в метаданных.
    assert set(models) == set(lp.HYDRA_MODEL_REASONING)
    assert lp.PROVIDERS_CATALOG["hydra"]["default_model"] == "grok-4.7"
    for legacy in ("glm-5.2", "hydra-gemini-pro", "hydra-gemini-3-pro", "hydra-gemini"):
        assert legacy not in models
    for model in models:
        default, choices = lp.reasoning_spec("hydra", model)
        assert default in choices and len(choices) >= 3


def test_resolve_reasoning_defaults_and_stored_choice():
    cfg = _cfg_with_reasoning()
    assert lp.resolve_reasoning_effort(cfg, provider="hydra", model="grok-4.7") == "none"
    assert lp.resolve_reasoning_effort(cfg, provider="hydra", model="gemini-3.1-pro") == "low"

    cfg = _cfg_with_reasoning(effort="high")
    assert lp.resolve_reasoning_effort(cfg, provider="hydra", model="grok-4.7") == "high"

    # Невалидный сохранённый уровень заменяется дефолтом провайдера.
    cfg = _cfg_with_reasoning(effort="ultra")
    assert lp.resolve_reasoning_effort(cfg, provider="hydra", model="grok-4.7") == "none"

    # Чужие провайдеры и ручные имена моделей параметр не получают.
    assert lp.resolve_reasoning_effort(cfg, provider="openrouter", model="grok-4.7") is None
    assert lp.resolve_reasoning_effort(cfg, provider="hydra", model="manual-model") is None


def test_load_config_migrates_legacy_hydra_models(tmp_path):
    path = tmp_path / "config.json"
    raw = {
        "api": {
            "provider": "hydra",
            "model": "glm-5.2",
            "timeout_sec": 500,
            "providers": {
                "hydra": {
                    "selected_model": "glm-5.2",
                    "default_model": "glm-5.2",
                    "models": ["glm-5.2", "hydra-gemini-pro", "my-manual-model"],
                }
            },
            "consensus": {"passes": 2, "pass2_provider": "hydra", "pass2_model": "glm-5.2"},
        }
    }
    path.write_text(json.dumps(raw), encoding="utf-8")
    cfg = lp.load_config(path)
    hydra = cfg["api"]["providers"]["hydra"]
    assert "glm-5.2" not in hydra["models"]
    assert "hydra-gemini-pro" not in hydra["models"]
    assert "grok-4.7" in hydra["models"]
    # Ручные имена моделей пользователя миграция не трогает.
    assert "my-manual-model" in hydra["models"]
    assert hydra["selected_model"] == "grok-4.7"
    assert cfg["api"]["model"] == "grok-4.7"
    assert cfg["api"]["consensus"]["pass2_model"] == "grok-4.7"


_PAYLOAD = {
    "main_street": "Main Street",
    "cross_street": "Oak Avenue",
    "distance_to_corner_ft": 150.0,
    "front_side": "N",
    "segments": [
        {"ns": "N", "degrees": 75, "minutes": 20, "seconds": 20, "ew": "E",
         "distance_ft": 120.0, "bearing_label": None},
        {"ns": "S", "degrees": 14, "minutes": 39, "seconds": 40, "ew": "E",
         "distance_ft": 85.5, "bearing_label": None},
        {"ns": "S", "degrees": 75, "minutes": 20, "seconds": 20, "ew": "W",
         "distance_ft": 120.0, "bearing_label": None},
        {"ns": "N", "degrees": 14, "minutes": 39, "seconds": 40, "ew": "W",
         "distance_ft": 85.5, "bearing_label": None},
    ],
}


class _Message:
    def __init__(self, parsed=None, content=None):
        self.parsed = parsed
        self.content = content
        self.refusal = None


class _Completion:
    def __init__(self, message):
        self.choices = [type("Choice", (), {"message": message})()]
        self.usage = None


def test_extract_sends_reasoning_effort_in_extra_body(monkeypatch):
    calls: list[dict] = {}
    parsed_obj = lp.LegalDescription.model_validate(copy.deepcopy(_PAYLOAD))

    class _Parse:
        @staticmethod
        def parse(**kwargs):
            calls.update(kwargs)
            return _Completion(_Message(parsed=parsed_obj))

    fake_client = type("Client", (), {
        "beta": type("Beta", (), {"chat": type("C", (), {"completions": _Parse})()})(),
    })()
    monkeypatch.setattr(lp, "_make_client", lambda cfg, target: fake_client)

    cfg = _cfg_with_reasoning(effort="high")
    result, mode, _usage = lp._extract_with_llm(
        "Beginning at Main Street...", cfg, provider="hydra", model="grok-4.7",
    )
    assert mode == "structured-outputs"
    assert result.main_street == "Main Street"
    assert calls.get("extra_body") == {"reasoning_effort": "high"}


def test_extract_without_reasoning_spec_sends_no_extra_body(monkeypatch):
    calls: list[dict] = {}
    parsed_obj = lp.LegalDescription.model_validate(copy.deepcopy(_PAYLOAD))

    class _Parse:
        @staticmethod
        def parse(**kwargs):
            calls.update(kwargs)
            return _Completion(_Message(parsed=parsed_obj))

    fake_client = type("Client", (), {
        "beta": type("Beta", (), {"chat": type("C", (), {"completions": _Parse})()})(),
    })()
    monkeypatch.setattr(lp, "_make_client", lambda cfg, target: fake_client)

    cfg = lp.load_config()
    lp._extract_with_llm(
        "Beginning at Main Street...", cfg, provider="openrouter", model="some/free-model",
    )
    assert "extra_body" not in calls


def test_reasoning_rejection_retries_without_parameter(monkeypatch):
    seen: list[dict] = []
    parsed_obj = lp.LegalDescription.model_validate(copy.deepcopy(_PAYLOAD))

    class _Create:
        @staticmethod
        def create(**kwargs):
            seen.append(dict(kwargs))
            if "extra_body" in kwargs:
                raise RuntimeError("Unsupported parameter: reasoning_effort")
            return _Completion(_Message(content=json.dumps(_PAYLOAD)))

    fake_client = type("Client", (), {
        "beta": None,
        "chat": type("Chat", (), {"completions": _Create})(),
    })()
    monkeypatch.setattr(lp, "_make_client", lambda cfg, target: fake_client)

    cfg = _cfg_with_reasoning(effort="xhigh")
    result, mode, _usage = lp._extract_with_llm(
        "Beginning at Main Street...", cfg, provider="hydra", model="grok-4.7",
    )
    assert result.main_street == "Main Street"
    assert mode in {"json_schema", "json_object", "prompt-json"}
    assert seen[0].get("extra_body") == {"reasoning_effort": "xhigh"}
    assert "extra_body" not in seen[1]


def test_multipass_single_pass_sends_reasoning_from_config(monkeypatch):
    """Сквозной путь: выбор уровня из конфига доходит до запроса прохода."""
    calls: list[dict] = []
    parsed_obj = lp.LegalDescription.model_validate(copy.deepcopy(_PAYLOAD))

    class _Parse:
        @staticmethod
        def parse(**kwargs):
            calls.append(dict(kwargs))
            return _Completion(_Message(parsed=parsed_obj))

    fake_client = type("Client", (), {
        "beta": type("Beta", (), {"chat": type("C", (), {"completions": _Parse})()})(),
    })()
    monkeypatch.setattr(lp, "_make_client", lambda cfg, target: fake_client)

    cfg = _cfg_with_reasoning(effort="medium")
    result = lp.run_multipass_consensus(
        "Beginning at Main Street...", cfg=cfg,
        passes=1, primary_provider="hydra", primary_model="grok-4.7",
        primary_api_key="sk-test",
    )
    assert result is not None
    assert calls and calls[0].get("extra_body") == {"reasoning_effort": "medium"}


def test_multi_transport_sends_and_retries_reasoning():
    calls: list[dict] = []

    class _Create:
        @staticmethod
        def create(**kwargs):
            calls.append(dict(kwargs))
            if kwargs.get("extra_body"):
                raise RuntimeError("reasoning_effort unsupported")
            return _Completion(_Message(content="ok"))

    client = type("Client", (), {
        "chat": type("Chat", (), {"completions": _Create})(),
    })()
    send = transport.make_sender(client, "grok-4.7", "low")
    assert send([], {"filename_lot": None, "portal_lot": None}) == "ok"
    assert calls[0]["extra_body"] == {"reasoning_effort": "low"}
    assert "extra_body" not in calls[1]
