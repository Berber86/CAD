"""Телеметрия (telemetry.py): конфиг, редакция секретов, батчи, outbox.

Сеть не используется: транспорт подменяется фейком, файлы — во временных каталогах.
"""

import json
from pathlib import Path

import pytest

import telemetry


def _cfg(tmp_path: Path, **overrides) -> telemetry.TelemetryConfig:
    base = {
        "enabled": True,
        "url": "https://example.supabase.co",
        "anon_key": "test-anon-key",
    }
    base.update(overrides)
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"telemetry": base}), encoding="utf-8")
    return telemetry.load_telemetry_config(
        config_path=cfg_file,
        local_config_path=tmp_path / "missing-local.json",
        environ={},
    )


def _client(tmp_path: Path, transport, **overrides) -> telemetry.TelemetryClient:
    return telemetry.TelemetryClient(
        _cfg(tmp_path, **overrides),
        app_version="4.81",
        transport=transport,
        data_dir=tmp_path / "data",
    )


# -- redact --------------------------------------------------------------------


def test_redact_hides_secrets_and_truncates():
    assert "tsai1:" not in telemetry.redact("ключ tsai1:AbCdEfGh12345678 тут")
    assert "sk-" not in telemetry.redact("токен sk-1234567890abcdef")
    assert "Bearer" not in telemetry.redact("Bearer abcdefgh12345678")
    long_text = "x" * 5000
    assert len(telemetry.redact(long_text)) <= telemetry.MAX_MESSAGE_CHARS
    assert telemetry.redact("") == ""


def test_infer_category_from_message_prefix():
    assert telemetry.infer_category("Шаг 0: ищу адрес") == "step0"
    assert telemetry.infer_category("Шаг 1 · итог") == "step1"
    assert telemetry.infer_category("Шаг 2 · автоподписание") == "step2"
    assert telemetry.infer_category("Шаг 4 · что-то") == "step4"
    assert telemetry.infer_category("Развод наложений") == "slide"
    assert telemetry.infer_category("обычная строка") == "app"


# -- config ----------------------------------------------------------------------


def test_config_disabled_without_url_or_key(tmp_path):
    cfg = telemetry.load_telemetry_config(
        config_path=tmp_path / "nope.json",
        local_config_path=tmp_path / "nope2.json",
        environ={},
    )
    assert cfg.enabled is True  # по умолчанию включено, но не настроено
    assert not cfg.configured
    assert not cfg.active


def test_config_env_overrides_file(tmp_path):
    cfg = _cfg(tmp_path, url="https://file.example.co", anon_key="file-key")
    assert cfg.url == "https://file.example.co"
    env_cfg = telemetry.load_telemetry_config(
        config_path=tmp_path / "config.json",
        local_config_path=tmp_path / "missing-local.json",
        environ={"SUPABASE_URL": "https://env.example.co", "TSAI_TELEMETRY_ENABLED": "0"},
    )
    assert env_cfg.url == "https://env.example.co"
    assert env_cfg.enabled is False


def test_config_local_overrides_base(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps({"telemetry": {"enabled": True, "url": "https://base.co", "anon_key": "base"}}),
        encoding="utf-8",
    )
    (tmp_path / "local.json").write_text(
        json.dumps({"telemetry": {"url": "https://local.co"}}), encoding="utf-8"
    )
    cfg = telemetry.load_telemetry_config(
        config_path=tmp_path / "config.json",
        local_config_path=tmp_path / "local.json",
        environ={},
    )
    assert cfg.url == "https://local.co"
    assert cfg.anon_key == "base"


# -- client: отправка --------------------------------------------------------------


def test_disabled_client_is_noop(tmp_path):
    calls = []

    def transport(url, headers, payload, timeout):
        calls.append(url)
        return 200

    client = _client(tmp_path, transport, enabled=False)
    assert not client.active
    client.start()
    client.log("должно не уйти", telemetry.LEVEL_ERROR)
    assert client.flush() == 0
    assert calls == []


def test_client_batches_and_posts_events(tmp_path):
    calls = []

    def transport(url, headers, payload, timeout):
        calls.append((url, headers, json.loads(payload)))
        return 200

    client = _client(tmp_path, transport)
    client.start()
    client.log("Шаг 1 · готово", telemetry.LEVEL_KEY)
    client.log("ошибка разбора", telemetry.LEVEL_ERROR)
    sent = client.flush()
    assert sent == 2
    assert calls, "ожидался хотя бы один POST"
    url, headers, rows = calls[-1]
    assert url.endswith("/rest/v1/app_logs")
    assert headers["apikey"] == "test-anon-key"
    assert len(rows) == 2
    assert rows[0]["category"] == "step1"
    assert rows[0]["level"] == "key"
    assert rows[1]["level"] == "error"
    assert rows[0]["install_id"] == client.install_id
    assert rows[0]["session_id"] == client.session_id
    assert rows[0]["app_version"] == "4.81"
    client.close()


def test_failed_send_goes_to_outbox_and_retries(tmp_path):
    state = {"fail": True}
    calls = []

    def transport(url, headers, payload, timeout):
        calls.append(url)
        return 500 if state["fail"] else 200

    client = _client(tmp_path, transport)
    client.start()
    client.log("событие без сети", telemetry.LEVEL_WARN)
    assert client.flush() == 0  # не ушло
    outbox = telemetry.outbox_path(tmp_path / "data")
    assert outbox.exists()
    assert "событие без сети" in outbox.read_text(encoding="utf-8")

    state["fail"] = False
    client._flush_outbox()
    assert not outbox.exists() or outbox.read_text(encoding="utf-8").strip() == ""
    client.close()


def test_install_id_persists_across_clients(tmp_path):
    def transport(url, headers, payload, timeout):
        return 200

    first = _client(tmp_path, transport)
    second = _client(tmp_path, transport)
    assert first.install_id == second.install_id
    # session_id у разных клиентов разные
    assert first.session_id != second.session_id


def test_log_never_raises_on_garbage(tmp_path):
    def transport(url, headers, payload, timeout):
        raise RuntimeError("сеть упала")

    client = _client(tmp_path, transport)
    client.start()
    client.log(None)  # type: ignore[arg-type]
    client.log("ok", level="не-уровень")  # type: ignore[arg-type]
    assert client.flush() == 0  # ушло в outbox, но не бросило
    client.close()


def test_sql_migration_contains_rls_insert_only():
    sql = (
        Path(__file__).resolve().parent.parent
        / "supabase"
        / "migrations"
        / "20261121000000_create_telemetry.sql"
    ).read_text(encoding="utf-8")
    assert "enable row level security" in sql.lower()
    assert "for insert to anon" in sql.lower()
    # Политики чтения для anon не должны создаваться.
    assert "for select to anon" not in sql.lower()
    assert "app_logs" in sql and "clients" in sql and "sessions" in sql
