from types import SimpleNamespace
import pytest
import developer_chat as dc
import llm_parser as lp


def test_independent_requests_send_only_current_message_and_use_selected_provider(monkeypatch):
    calls = []
    closed = []
    class Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ответ"))],
                usage=SimpleNamespace(prompt_tokens=4, completion_tokens=2, total_tokens=6))
        def close(self):
            closed.append(True)
    selected = []
    def make(api_cfg, *, target):
        selected.append(target)
        return Client()
    monkeypatch.setattr(lp, "_make_client", make)
    cfg = lp.load_config()
    a, usage = dc.ask_once("первое", cfg, provider="hydra", model="model1", api_key="key1")
    b, _ = dc.ask_once("второе", cfg, provider="openrouter", model="model2", api_key="key2")
    assert (a, b) == ("ответ", "ответ")
    assert calls == [{"model":"model1", "messages":[{"role":"user", "content":"первое"}]},
                     {"model":"model2", "messages":[{"role":"user", "content":"второе"}]}]
    assert [t["provider"] for t in selected] == ["hydra", "openrouter"]
    assert [t["api_key"] for t in selected] == ["key1", "key2"]
    assert [t["base_url"] for t in selected] == [cfg["api"]["providers"][p]["base_url"] for p in ("hydra", "openrouter")]
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (4, 2, 6)
    assert len(closed) == 2


def test_unsupported_and_refused_answers_fail_without_private_details(monkeypatch):
    for message in [SimpleNamespace(content=None, refusal="private refusal"), SimpleNamespace(content="")]:
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: SimpleNamespace(choices=[SimpleNamespace(message=message)]))), close=lambda: None)
        monkeypatch.setattr(lp, "_make_client", lambda *args, **kwargs: client)
        with pytest.raises(ValueError):
            dc.ask_once("hello", lp.load_config(), provider="hydra", model="x", api_key="key")
    err = Exception("secret from request and provider")
    assert "secret" not in dc._safe_error(err)


def test_no_usage_is_shown_as_unavailable(monkeypatch):
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]))), close=lambda: None)
    monkeypatch.setattr(lp, "_make_client", lambda *args, **kwargs: client)
    _, usage = dc.ask_once("hi", lp.load_config(), provider="hydra", model="x", api_key="key")
    assert usage.available is False


def test_protected_provider_key_is_unlocked_for_that_provider_only(monkeypatch):
    protected = lp.protect_api_key("openrouter", "my-router-key", secret="1234")
    cfg = lp.load_config()
    cfg["api"]["providers"]["openrouter"]["api_key"] = protected
    seen = []
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]))), close=lambda: None)
    monkeypatch.setattr(lp, "_make_client", lambda api_cfg, *, target: seen.append(target) or client)
    dc.ask_once("hello", cfg, provider="openrouter", model="model", key_secret="1234")
    assert seen[0]["api_key"] == "my-router-key"
    with pytest.raises(lp.KeyProtectionError):
        dc.ask_once("hello", cfg, provider="openrouter", model="model", key_secret="incorrect")
    assert len(seen) == 1
