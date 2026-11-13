"""
llm_parser.py
=============
Разбор текста Legal Description (Title Survey, США) в строгую Pydantic-модель.

Источники текста:
  * обычная строка (вставка из буфера / OCR);
  * PDF-файл (PyMuPDF, только текстовый слой — без OCR).

Основной путь — LLM через OpenAI-совместимый API и Structured Outputs
(`client.beta.chat.completions.parse`). Если провайдер/модель не поддерживает
`parse`, автоматически пробуются:
  1) `response_format={"type": "json_schema", ...}`;
  2) `response_format={"type": "json_object"}` + валидация Pydantic.

Дополнительно есть полностью локальный детерминированный regex-парсер
(`parse_with_regex`) для работы без сети: он разбирает типовые формулировки
deed'ов (румбы DMS/десятичные, длины в футах, фасадная и пересекаемая улицы,
расстояние до угла). GUI включает его осознанно (выключатель «Использовать LLM»),
а не подставляет молча вместо ответа модели: ошибка LLM должна быть видна.

Автор: Senior Python / AutoCAD Automation
Python: 3.11+
"""

from __future__ import annotations

import base64
import copy
import getpass
import hashlib
import hmac
import itertools
import json
import math
import os
import platform
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "ConfigError",
    "KeyProtectionError",
    "LegalDescriptionError",
    "LLMParseError",
    "PDFError",
    "CONFIG_PATH",
    "DEFAULT_CONFIG",
    "ENV_PATH",
    "LOCAL_CONFIG_PATH",
    "NYC_SAMPLES",
    "PROTECTED_KEY_PREFIX",
    "PROVIDER_ORDER",
    "PROVIDERS_CATALOG",
    "LegalDescription",
    "ParseOutcome",
    "Segment",
    "TokenUsage",
    "bearing_label",
    "clean_provider_key",
    "dms_to_degrees",
    "inspect_pdf",
    "is_protected_key",
    "load_config",
    "load_dotenv_dict",
    "make_strict_schema",
    "parse_legal_description",
    "parse_legal_description_full",
    "parse_with_regex",
    "protect_api_key",
    "read_pdf_text",
    "render_pdf_pages_base64",
    "resolve_api_target",
    "save_config",
    "save_protected_env",
    "unprotect_api_key",
]


# ---------------------------------------------------------------------------
# Ошибки
# ---------------------------------------------------------------------------


class LegalDescriptionError(RuntimeError):
    """Базовая ошибка разбора Legal Description."""


class LLMParseError(LegalDescriptionError):
    """LLM недоступна или вернула некорректный ответ."""


class PDFError(LegalDescriptionError):
    """PDF не читается / в нём нет текстового слоя."""


class ConfigError(LegalDescriptionError):
    """config.json повреждён."""


class KeyProtectionError(LegalDescriptionError):
    """Защищённый API-ключ не удалось пересчитать (чужой ПК или неверный PIN)."""


# ---------------------------------------------------------------------------
# Конфигурация и каталог провайдеров (Hydra AI / OpenRouter / RouterAI)
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).resolve().with_name("config.json")
LOCAL_CONFIG_PATH = Path(__file__).resolve().with_name("config.local.json")
ENV_PATH = Path(__file__).resolve().with_name(".env")
PROTECTED_KEY_PREFIX = "tsai1:"

PROVIDER_ORDER: tuple[str, ...] = ("hydra", "openrouter", "routerai")

PROVIDERS_CATALOG: dict[str, dict[str, Any]] = {
    "hydra": {
        "label": "Hydra AI",
        "base_url": "https://api.hydraai.ru/v1",
        "api_key": "",
        "env_key": "HYDRA_API_KEY",
        "default_model": "glm-5.2",
        "selected_model": "glm-5.2",
        "models": [
            "glm-5.2",
            "hydra-gemini-pro",
            "hydra-gemini-3-pro",
            "hydra-gemini",
        ],
    },
    "openrouter": {
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "",
        "env_key": "OPENROUTER_API_KEY",
        "default_model": "nvidia/nemotron-3.5-super-vl:free",
        "selected_model": "nvidia/nemotron-3.5-super-vl:free",
        "models": [
            "nvidia/nemotron-3.5-super-vl:free",
            "nvidia/nemotron-3-super-120b-a12b:free",
            "nvidia/nemotron-3-nano-30b-a3b:free",
            "stepfun/step-3.5-flash:free",
            "z-ai/glm-4.5-air:free",
            "arcee-ai/trinity-large-preview:free",
            "mistralai/devstral-2512:free",
        ],
    },
    "routerai": {
        "label": "RouterAI",
        "base_url": "https://routerai.ru/api/v1",
        "api_key": "",
        "env_key": "ROUTERAI_API_KEY",
        "default_model": "openai/gpt-4.1",
        "selected_model": "openai/gpt-4.1",
        "models": [
            "openai/gpt-4.1",
            "openai/gpt-4.1-mini",
            "openai/gpt-4o",
            "openai/gpt-4o-mini",
            "google/gemini-2.5-pro",
            "google/gemini-2.5-flash",
            "anthropic/claude-sonnet-4.5",
            "deepseek/deepseek-chat",
        ],
    },
}

DEFAULT_CONFIG: dict[str, Any] = {
    "api": {
        "provider": "hydra",
        "api_key": "",
        "base_url": "https://api.hydraai.ru/v1",
        "model": "glm-5.2",
        "temperature": 0.0,
        "timeout_sec": 180,
        "max_retries": 2,
        "max_input_chars": 60000,
        "vision_dpi": 220,
        "vision_max_pages": 4,
        "providers": copy.deepcopy(PROVIDERS_CATALOG),
    },
    "drawing": {
        "base_scale": 16.0,
        "base_text_height": 0.9,
        "available_scales": [10.0, 16.0, 20.0, 30.0],
        "text_offset_factor": 1.8,
        "street_offset_factor": 6.5,
        "street_height_factor": 1.9,
        "corner_label_gap_factor": 2.4,
        "corner_distance_offset_factor": 1.9,
        "long_side_threshold_ft": 40.0,
        "stack_short_side_ft": 30.0,
        "stacked_line_gap_factor": 1.9,
        "char_width_factor": 0.70,
        "space_width_factor": 0.75,
        "max_label_fill_ratio": 0.72,
        "max_stretch_spaces": 65,
        "front_side_index": 0,
        "degrees_as_control_code": True,
        "text_style": "AI_BOUNDARY",
        "draw_street_extensions": True,
        "zoom_extents": True,
        "zoom_margin_scale": 0.84,
    },
    "layers": {
        "property_line": "PROPERTY_LINE",
        "boundary_text": "BOUNDARY_TEXT_AI",
        "street_row": "STREET_ROW_AI",
    },
    "layer_colors": {
        "property_line": 1,
        "boundary_text": 3,
        "street_row": 8,
    },
}


def _deep_update(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """Рекурсивно накладывает extra на base (base меняется на месте)."""
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
    return base


def is_protected_key(value: str | None) -> bool:
    """Проверяет, хранится ли ключ в защищённом (пересчитываемом) виде tsai1:…."""
    return str(value or "").strip().startswith(PROTECTED_KEY_PREFIX)


def clean_provider_key(provider: str, raw_key: str | None) -> str:
    """
    Очищает API-ключ провайдера.

    В contextual-chatbot ключ Hydra AI может храниться с суффиксом ``$$``
    (маркер Full Hydra Mode) — срезаем его, чтобы такой ключ работал без правок.
    Защищённые токены ``tsai1:…`` возвращаются без модификации до момента пересчёта.
    """
    key = str(raw_key or "").strip()
    if is_protected_key(key):
        return key
    if provider.strip().lower() == "hydra" and key.endswith("$$"):
        key = key[:-2].strip()
    return key


def _machine_fingerprint() -> bytes:
    """
    Аппаратно-системный отпечаток текущего ПК и учётной записи ОС (32 байта SHA-256).

    Никогда не сохраняется в .json/.env и не попадает в git: украденные файлы
    на компьютере злоумышленника имеют другой MachineGuid/machine-id и не
    проходят проверку целостности HMAC / Windows DPAPI.
    """
    parts: list[str] = []
    if sys.platform == "win32":  # pragma: no cover - Windows registry
        try:
            import winreg

            access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
                0,
                access,
            ) as reg_key:
                guid, _ = winreg.QueryValueEx(reg_key, "MachineGuid")
                if guid:
                    parts.append(str(guid).strip())
        except Exception:  # noqa: BLE001
            parts.append(os.environ.get("COMPUTERNAME", ""))
        parts.append(os.environ.get("USERNAME", ""))
    else:
        for mid_path in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
            if mid_path.exists():
                try:
                    mid = mid_path.read_text(encoding="utf-8").strip()
                except OSError:
                    mid = ""
                if mid:
                    parts.append(mid)
                    break
        if not parts:
            parts.append(platform.node())
        try:
            parts.append(getpass.getuser())
        except Exception:  # noqa: BLE001
            parts.append(str(os.getuid()) if hasattr(os, "getuid") else "user")

    parts.append(str(uuid.getnode()))
    raw = "tsai-hw-v1|" + "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).digest()


def _dpapi_protect(data: bytes, entropy: bytes) -> bytes | None:
    """Дополнительная обёртка Windows DPAPI (привязка к учётной записи Windows)."""
    if sys.platform != "win32":
        return None
    try:  # pragma: no cover - Windows Crypt32
        import ctypes
        import ctypes.wintypes

        class _DataBlob(ctypes.Structure):
            _fields_ = [
                ("cbData", ctypes.wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char)),
            ]

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.c_wchar_p,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptProtectData.restype = ctypes.wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p

        in_buf = ctypes.create_string_buffer(data, len(data))
        ent_buf = ctypes.create_string_buffer(entropy, len(entropy))
        in_blob = _DataBlob(len(data), in_buf)
        ent_blob = _DataBlob(len(entropy), ent_buf)
        out_blob = _DataBlob()

        ok = crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            "TitleSurveyAI",
            ctypes.byref(ent_blob),
            None,
            None,
            0x01,  # CRYPTPROTECT_UI_FORBIDDEN
            ctypes.byref(out_blob),
        )
        if not ok:
            return None
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            kernel32.LocalFree(out_blob.pbData)
    except Exception:  # noqa: BLE001
        return None


def _dpapi_unprotect(data: bytes, entropy: bytes) -> bytes:
    """Распаковка Windows DPAPI."""
    if sys.platform != "win32":
        raise KeyProtectionError(
            "ключ защищён через Windows DPAPI и может быть пересчитан только на том же ПК Windows"
        )
    try:  # pragma: no cover - Windows Crypt32
        import ctypes
        import ctypes.wintypes

        class _DataBlob(ctypes.Structure):
            _fields_ = [
                ("cbData", ctypes.wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char)),
            ]

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptUnprotectData.restype = ctypes.wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p

        in_buf = ctypes.create_string_buffer(data, len(data))
        ent_buf = ctypes.create_string_buffer(entropy, len(entropy))
        in_blob = _DataBlob(len(data), in_buf)
        ent_blob = _DataBlob(len(entropy), ent_buf)
        out_blob = _DataBlob()

        ok = crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,
            ctypes.byref(ent_blob),
            None,
            None,
            0x01,  # CRYPTPROTECT_UI_FORBIDDEN
            ctypes.byref(out_blob),
        )
        if not ok:
            raise KeyProtectionError("Windows DPAPI отклонил расшифровку (чужой ПК/профиль или неверный PIN)")
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            kernel32.LocalFree(out_blob.pbData)
    except KeyProtectionError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise KeyProtectionError(f"ошибка Windows DPAPI при пересчёте ключа: {exc}") from exc


def _derive_protection_keys(provider: str, salt: bytes, secret: str) -> tuple[bytes, bytes]:
    master = (
        _machine_fingerprint()
        + b"|"
        + provider.strip().lower().encode("utf-8")
        + b"|"
        + str(secret or "").strip().encode("utf-8")
    )
    derived = hashlib.pbkdf2_hmac("sha256", master, salt, 200_000, dklen=64)
    return derived[:32], derived[32:]


def _xor_keystream(data: bytes, enc_key: bytes, salt: bytes) -> bytes:
    out = bytearray(len(data))
    offset = 0
    counter = 0
    while offset < len(data):
        block = hmac.new(
            enc_key, salt + counter.to_bytes(4, "big"), hashlib.sha256
        ).digest()
        chunk_len = min(len(block), len(data) - offset)
        for idx in range(chunk_len):
            out[offset + idx] = data[offset + idx] ^ block[idx]
        offset += chunk_len
        counter += 1
    return bytes(out)


def protect_api_key(
    provider: str,
    raw_key: str | None,
    *,
    secret: str = "",
    unlock_secret: str | None = None,
) -> str:
    """
    Преобразует реальный API-ключ в защищённый хранимый токен ``tsai1:…``.

    В файлах (.json / .env) сохраняется только этот токен: он привязан к
    аппаратному отпечатку ПК, Windows DPAPI (в Windows) и опциональному PIN-коду
    в памяти, поэтому украденные конфиги бесполезны для злоумышленника.
    """
    cleaned = clean_provider_key(provider, raw_key)
    if not cleaned:
        return ""
    if is_protected_key(cleaned):
        # Уже защищённый токен: сначала раскрываем реальный ключ, чтобы не шифровать дважды.
        prev_secret = secret if unlock_secret is None else unlock_secret
        try:
            cleaned = unprotect_api_key(provider, cleaned, secret=secret)
        except KeyProtectionError:
            if prev_secret != secret:
                cleaned = unprotect_api_key(provider, cleaned, secret=prev_secret)
            else:
                raise

    salt = os.urandom(16)
    enc_key, mac_key = _derive_protection_keys(provider, salt, secret)
    ciphertext = _xor_keystream(cleaned.encode("utf-8"), enc_key, salt)
    tag = hmac.new(mac_key, b"tsai1:v1|" + salt + ciphertext, hashlib.sha256).digest()[:16]
    payload = salt + tag + ciphertext

    entropy = hashlib.sha256(
        f"dpapi|{provider.strip().lower()}|{str(secret or '').strip()}".encode("utf-8")
    ).digest()
    dpapi_blob = _dpapi_protect(payload, entropy)
    if dpapi_blob is not None:
        b64 = base64.urlsafe_b64encode(dpapi_blob).decode("ascii").rstrip("=")
        return f"{PROTECTED_KEY_PREFIX}dpapi:{b64}"

    b64 = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{PROTECTED_KEY_PREFIX}v1:{b64}"


def unprotect_api_key(provider: str, stored_key: str | None, *, secret: str = "") -> str:
    """
    Пересчитывает реальный API-ключ в оперативной памяти из хранимого ``tsai1:…``.
    Обычные (незащищённые) строки возвращает после стандартной очистки.
    """
    token = str(stored_key or "").strip()
    if not token:
        return ""
    if not is_protected_key(token):
        return clean_provider_key(provider, token)

    parts = token.split(":", 2)
    if len(parts) != 3 or parts[1] not in {"v1", "dpapi"} or not parts[2]:
        raise KeyProtectionError(f"повреждён формат защищённого ключа ({provider})")

    scheme, b64_data = parts[1], parts[2]
    padded = b64_data + "=" * (-len(b64_data) % 4)
    try:
        raw_bytes = base64.urlsafe_b64decode(padded.encode("ascii"))
    except Exception as exc:  # noqa: BLE001
        raise KeyProtectionError(f"повреждён защищённый ключ ({provider}): {exc}") from exc

    if scheme == "dpapi":
        entropy = hashlib.sha256(
            f"dpapi|{provider.strip().lower()}|{str(secret or '').strip()}".encode("utf-8")
        ).digest()
        payload = _dpapi_unprotect(raw_bytes, entropy)
    else:
        payload = raw_bytes

    if len(payload) <= 32:
        raise KeyProtectionError(f"повреждён защищённый ключ ({provider}): слишком короткий блок")

    salt, tag, ciphertext = payload[:16], payload[16:32], payload[32:]
    enc_key, mac_key = _derive_protection_keys(provider, salt, secret)
    expected_tag = hmac.new(
        mac_key, b"tsai1:v1|" + salt + ciphertext, hashlib.sha256
    ).digest()[:16]
    if not hmac.compare_digest(tag, expected_tag):
        raise KeyProtectionError(
            f"не удалось пересчитать защищённый API-ключ ({provider}): "
            "введите верный «Код пересчёта (PIN)» или вставьте ключ заново "
            "(ключ привязан к конкретному ПК и PIN-коду)"
        )
    try:
        real_key = _xor_keystream(ciphertext, enc_key, salt).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise KeyProtectionError(f"ошибка декодирования ключа ({provider})") from exc
    return clean_provider_key(provider, real_key)


def load_dotenv_dict(path: str | Path | None = None) -> dict[str, str]:
    """Читает пары KEY=VALUE из .env (без записи открытых ключей в os.environ)."""
    env_path = Path(path) if path is not None else ENV_PATH
    if not env_path.exists():
        return {}
    result: dict[str, str] = {}
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            result[key] = value
    return result


def save_protected_env(
    keys_by_provider: dict[str, str],
    *,
    secret: str = "",
    unlock_secret: str | None = None,
    path: str | Path | None = None,
) -> Path:
    """
    Сохраняет API-ключи провайдеров в .env ТОЛЬКО в защищённом виде ``tsai1:…``,
    сохраняя прочие строки и комментарии существующего .env.
    """
    env_path = Path(path) if path is not None else ENV_PATH
    env_updates: dict[str, str] = {}
    for pid, raw_val in keys_by_provider.items():
        p_cfg = PROVIDERS_CATALOG.get(pid, {})
        env_name = str(p_cfg.get("env_key") or f"{pid.upper()}_API_KEY").strip()
        val = str(raw_val or "").strip()
        if val:
            env_updates[env_name] = protect_api_key(
                pid, val, secret=secret, unlock_secret=unlock_secret
            )

    existing_lines: list[str] = []
    if env_path.exists():
        existing_lines = env_path.read_text(encoding="utf-8").splitlines()

    written: set[str] = set()
    output_lines: list[str] = []
    if not existing_lines:
        output_lines.append(
            "# Защищённые API-ключи Title Survey AI (реальный ключ пересчитывается в RAM)"
        )
    for raw_line in existing_lines:
        stripped = raw_line.strip()
        check = stripped[7:].strip() if stripped.lower().startswith("export ") else stripped
        if check and not check.startswith("#") and "=" in check:
            k_name = check.split("=", 1)[0].strip()
            if k_name in env_updates:
                output_lines.append(f"{k_name}={env_updates[k_name]}")
                written.add(k_name)
                continue
        output_lines.append(raw_line)

    for k_name, protected_val in env_updates.items():
        if k_name not in written:
            output_lines.append(f"{k_name}={protected_val}")

    env_path.write_text("\n".join(output_lines).rstrip() + "\n", encoding="utf-8")
    return env_path


def _read_json_dict(cfg_path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{cfg_path.name} повреждён ({exc}); путь: {cfg_path}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{cfg_path.name} должен содержать JSON-объект; путь: {cfg_path}")
    return raw


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """
    Читает config.json (а при вызове по умолчанию также config.local.json и .env)
    и дополняет значениями по умолчанию.

    Защищённые токены ``tsai1:…`` остаются в зашифрованном виде до вызова
    ``resolve_api_target()``, где пересчитываются в реальный ключ в памяти.
    """
    cfg_path = Path(path) if path is not None else CONFIG_PATH
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if cfg_path.exists():
        _deep_update(cfg, _read_json_dict(cfg_path))
    if path is None and LOCAL_CONFIG_PATH.exists():
        _deep_update(cfg, _read_json_dict(LOCAL_CONFIG_PATH))

    dotenv_vars = load_dotenv_dict() if path is None else {}
    api = cfg["api"]
    providers = api.setdefault("providers", copy.deepcopy(PROVIDERS_CATALOG))
    for pid, p_cfg in providers.items():
        if not isinstance(p_cfg, dict):
            continue
        raw_key = clean_provider_key(pid, p_cfg.get("api_key"))
        env_name = str(p_cfg.get("env_key") or "").strip()
        if not raw_key and env_name:
            raw_key = clean_provider_key(
                pid, os.environ.get(env_name, "") or dotenv_vars.get(env_name, "")
            )
        p_cfg["api_key"] = raw_key

    active_pid = str(api.get("provider") or "hydra").strip().lower()
    active_key = ""
    if isinstance(providers.get(active_pid), dict):
        active_key = str(providers[active_pid].get("api_key") or "").strip()

    if not str(api.get("api_key") or "").strip():
        api["api_key"] = (
            active_key
            or os.environ.get("OPENAI_API_KEY", "").strip()
            or dotenv_vars.get("OPENAI_API_KEY", "").strip()
            or os.environ.get("CAD_LLM_API_KEY", "").strip()
            or dotenv_vars.get("CAD_LLM_API_KEY", "").strip()
        )
    return cfg


def resolve_api_target(
    cfg: dict[str, Any],
    *,
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    key_secret: str = "",
) -> dict[str, str]:
    """
    Определяет итоговые параметры вызова LLM и пересчитывает защищённый ключ в RAM.
    """
    api_cfg = cfg.get("api", {})
    providers = api_cfg.get("providers") or {}
    cfg_provider = str(api_cfg.get("provider") or "hydra").strip().lower()
    provider_id = str(provider or cfg_provider or "hydra").strip().lower()
    p_cfg = providers.get(provider_id) if isinstance(providers, dict) else None
    if not isinstance(p_cfg, dict):
        p_cfg = {}

    provider_label = str(p_cfg.get("label") or provider_id or "LLM")
    env_key = str(p_cfg.get("env_key") or "").strip()
    dotenv_vars = load_dotenv_dict()

    if provider is not None and provider_id != cfg_provider:
        base_url = str(p_cfg.get("base_url") or api_cfg.get("base_url") or "").strip()
        chosen_model = str(
            model
            or p_cfg.get("selected_model")
            or p_cfg.get("default_model")
            or api_cfg.get("model")
            or "glm-5.2"
        ).strip()
    else:
        base_url = str(p_cfg.get("base_url") or api_cfg.get("base_url") or "").strip()
        chosen_model = str(
            model
            or api_cfg.get("model")
            or p_cfg.get("selected_model")
            or p_cfg.get("default_model")
            or "glm-5.2"
        ).strip()

    raw_key = (
        api_key
        or p_cfg.get("api_key")
        or (os.environ.get(env_key, "") if env_key else "")
        or (dotenv_vars.get(env_key, "") if env_key else "")
        or api_cfg.get("api_key")
        or os.environ.get("OPENAI_API_KEY", "")
        or dotenv_vars.get("OPENAI_API_KEY", "")
        or os.environ.get("CAD_LLM_API_KEY", "")
        or dotenv_vars.get("CAD_LLM_API_KEY", "")
    )
    real_key = unprotect_api_key(provider_id, raw_key, secret=key_secret)
    return {
        "provider": provider_id,
        "provider_label": provider_label,
        "base_url": base_url,
        "model": chosen_model,
        "api_key": real_key,
        "env_key": env_key,
    }


def save_config(cfg: dict[str, Any], path: str | Path | None = None) -> Path:
    """Сохраняет конфигурацию (используется кнопкой «Сохранить» в GUI)."""
    cfg_path = Path(path) if path is not None else CONFIG_PATH
    cfg_path.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return cfg_path


# ---------------------------------------------------------------------------
# Геодезические румбы: DMS <-> десятичные градусы, каноничная подпись
# ---------------------------------------------------------------------------


def dms_to_degrees(degrees: float, minutes: float, seconds: float) -> float:
    """75°20'20" -> 75.338888..."""
    return float(degrees) + float(minutes) / 60.0 + float(seconds) / 3600.0


def _carry_dms(degrees: int, minutes: int, seconds: float) -> tuple[int, int, float]:
    """Нормализует DMS: 75°59'59.999" -> 75°59'60" -> 76°00'00"."""
    d, m, s = int(degrees), int(minutes), round(float(seconds), 2)
    if s >= 60.0:
        s -= 60.0
        m += 1
    if m >= 60:
        m -= 60
        d += 1
    if s < 0:  # защита от мусорного ввода
        s = 0.0
    return d, m, s


def _fmt_seconds(seconds: float) -> str:
    text = f"{seconds:.2f}".rstrip("0").rstrip(".")
    return text if text else "0"


def bearing_label(ns: str, degrees: int, minutes: int, seconds: float, ew: str) -> str:
    """
    Каноничная подпись румба: ``N 75°20'20" E``.

    Для ровных минут секунды не печатаются: ``N 45°00' E``.
    """
    degrees = int(degrees)
    minutes = int(minutes)
    if round(float(seconds), 2) == 0.0:
        return f"{ns} {degrees}°{minutes:02d}' {ew}"
    return f"{ns} {degrees}°{minutes:02d}'{_fmt_seconds(seconds)}\" {ew}"


# ---------------------------------------------------------------------------
# Pydantic-модели (Structured Outputs)
# ---------------------------------------------------------------------------


_FEET_INCHES_STR_RE = re.compile(
    r"(?P<ft>\d+(?:\.\d+)?)\s*(?:feet|foot|ft\.?|')\s*(?:and\s+)?"
    r"(?P<in>\d+(?:\.\d+)?)(?:\s+(?P<fnum>\d+)\s*/\s*(?P<fden>\d+))?\s*"
    r"(?:inches|inch|in\.?|\")",
    re.IGNORECASE,
)


def _coerce_float(value: Any) -> float | None:
    """'1,250.50 ft' -> 1250.5; '13 feet 8 inches' -> 13.6667; '' / None -> None."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("ожидалось число, получено bool")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("\u00a0", " ")
    if not text or text in {"-", "—", "n/a", "N/A", "none", "null"}:
        return None
    fi = _FEET_INCHES_STR_RE.search(text)
    if fi:
        feet = float(fi.group("ft"))
        inches = float(fi.group("in"))
        if fi.group("fnum") and fi.group("fden"):
            den = float(fi.group("fden"))
            if den > 0:
                inches += float(fi.group("fnum")) / den
        return feet + inches / 12.0
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        raise ValueError(f"не удалось прочитать число из {value!r}")
    return float(match.group(0))


def _clean_name(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    text = text.strip(" ,;:")
    return text or None


class Segment(BaseModel):
    """
    Одна сторона участка («call» в legal description), по порядку обхода.

    Румб задаётся компонентами (ns/degrees/minutes/seconds/ew) и подписью
    `bearing_label`, которая всегда пересчитывается в каноничный вид —
    так подпись гарантированно совпадает с геометрией.

    Для участков Нью-Йорка (Queens/Brooklyn/Bronx/Manhattan), где стороны
    описаны через «PARALLEL WITH …» / «AT RIGHT ANGLES TO …» / «INTERIOR ANGLE»
    без компасного румба, геометрия всё равно хранится через вычисленный румб,
    а `has_bearing=False` убирает искусственный DMS-румб с чертежа, оставляя
    длину и `course_note` / `interior_angle`.
    """

    model_config = ConfigDict(extra="forbid")

    ns: Literal["N", "S"] = Field(description="Первая буква румба: N или S")
    degrees: int = Field(ge=0, le=90, description="Градусы румба, 0..90")
    minutes: int = Field(ge=0, le=59, description="Минуты, 0..59")
    seconds: float = Field(ge=0, le=60, description="Секунды, 0..60")
    ew: Literal["E", "W"] = Field(description="Вторая буква румба: E или W")
    distance_ft: float = Field(gt=0, description="Длина стороны в футах")
    bearing_label: str | None = Field(
        default=None,
        description='Готовая подпись румба, например N 75°20\'20" E',
    )
    has_bearing: bool = Field(
        default=True,
        description=(
            "True, если в документе/на чертеже явно указан компасный румб (N/S deg E/W); "
            "False, если направление задано относительно улиц (PARALLEL WITH / AT RIGHT ANGLES / вдоль улицы)"
        ),
    )
    course_note: str | None = Field(
        default=None,
        description=(
            "Текстовая пометка вдоль стороны без румба, например "
            "'PARALLEL WITH QUEENS MIDTOWN EXPY' или 'AT RIGHT ANGLES TO QUEENS MIDTOWN EXPRESSWAY'"
        ),
    )
    interior_angle: str | None = Field(
        default=None,
        description='Внутренний угол в начальной вершине этой стороны, например 93°33\'29.5"',
    )

    @model_validator(mode="before")
    @classmethod
    def _preprocess_segment_raw(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        item = dict(data)

        # Нормализация кардинальных сокращений от LLM (напр. ns="E", degrees=0 -> N 90° E)
        raw_ns = str(item.get("ns") or "").strip().upper()[:1]
        raw_ew = str(item.get("ew") or "").strip().upper()[:1]
        deg_f = _coerce_float(item.get("degrees"))
        min_f = _coerce_float(item.get("minutes"))
        sec_f = _coerce_float(item.get("seconds"))
        if raw_ns in {"E", "W"} and (deg_f is None or abs(deg_f) < 1e-9):
            item["ns"] = "N"
            item["ew"] = raw_ns
            deg_f = 90.0
            item["degrees"] = 90
        elif raw_ns in {"N", "S"} and raw_ew in {"N", "S"} and (deg_f is None or abs(deg_f) < 1e-9):
            item["ew"] = "E"

        # Раскладываем дробные градусы/минуты (напр. degrees=75.5 -> 75°30'00")
        if deg_f is not None:
            m_val = min_f if min_f is not None else 0.0
            s_val = sec_f if sec_f is not None else 0.0
            deg_frac = abs(deg_f) - int(abs(deg_f))
            if deg_frac > 1e-7 and abs(m_val) < 1e-9 and abs(s_val) < 1e-9:
                sign_d = -1 if deg_f < 0 else 1
                whole_d = int(abs(deg_f))
                rem_m = (abs(deg_f) - whole_d) * 60.0
                whole_m = int(rem_m + 1e-9)
                rem_s = max(0.0, (rem_m - whole_m) * 60.0)
                deg_f = float(sign_d * whole_d)
                m_val = float(whole_m)
                s_val = round(rem_s, 2)
            else:
                min_frac = abs(m_val) - int(abs(m_val))
                if min_frac > 1e-7 and abs(s_val) < 1e-9:
                    whole_m = int(abs(m_val))
                    rem_s = (abs(m_val) - whole_m) * 60.0
                    m_val = float(-whole_m if m_val < 0 else whole_m)
                    s_val = round(rem_s, 2)

            if deg_f >= 0 and m_val >= 0 and s_val >= 0:
                c_deg, c_min, c_sec = _carry_dms(int(round(deg_f)), int(round(m_val)), s_val)
                item["degrees"] = c_deg
                item["minutes"] = c_min
                item["seconds"] = c_sec
        return item

    @field_validator("ns", "ew", mode="before")
    @classmethod
    def _normalize_quadrant(cls, value: Any) -> str:
        if isinstance(value, str):
            letter = value.strip().upper()[:1]
            if letter in {"N", "S", "E", "W"}:
                return letter
        raise ValueError(f"некорректная буква румба: {value!r}")

    @field_validator("degrees", "minutes", mode="before")
    @classmethod
    def _normalize_int(cls, value: Any) -> int:
        number = _coerce_float(value)
        if number is None:
            raise ValueError("пустое значение в румбе")  # pragma: no cover
        return int(round(number))

    @field_validator("seconds", mode="before")
    @classmethod
    def _normalize_seconds(cls, value: Any) -> float:
        number = _coerce_float(value)
        if number is None:
            return 0.0
        return round(number, 2)

    @field_validator("distance_ft", mode="before")
    @classmethod
    def _normalize_distance(cls, value: Any) -> float:
        number = _coerce_float(value)
        if number is None:
            raise ValueError("не указана длина стороны")
        if number <= 0:
            raise ValueError(f"длина стороны должна быть > 0, получено {number}")
        return number

    @field_validator("bearing_label", "course_note", "interior_angle", mode="before")
    @classmethod
    def _normalize_label(cls, value: Any) -> str | None:
        return _clean_name(value)

    @field_validator("has_bearing", mode="before")
    @classmethod
    def _normalize_has_bearing(cls, value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() not in {"false", "0", "no", "none"}
        return bool(value)

    @model_validator(mode="after")
    def _finalize(self) -> "Segment":
        d, m, s = _carry_dms(self.degrees, self.minutes, self.seconds)
        if d > 90 or (d == 90 and (m > 0 or s > 0)):
            raise ValueError("угол румба не может превышать 90°00'00\"")
        self.degrees, self.minutes, self.seconds = d, m, s
        self.bearing_label = bearing_label(self.ns, d, m, s, self.ew)
        return self

    # Удобные производные величины -----------------------------------------
    @property
    def azimuth_deg(self) -> float:
        """Азимут от севера по часовой стрелке, градусы."""
        angle = dms_to_degrees(self.degrees, self.minutes, self.seconds)
        if self.ns == "N":
            az = angle if self.ew == "E" else 360.0 - angle
        else:
            az = 180.0 - angle if self.ew == "E" else 180.0 + angle
        return az % 360.0

    @property
    def vector(self) -> tuple[float, float]:
        """(dX, dY) в футах: 0° = +Y (север), 90° = +X (восток)."""
        rad = math.radians(self.azimuth_deg)
        return self.distance_ft * math.sin(rad), self.distance_ft * math.cos(rad)


class LegalDescription(BaseModel):
    """Результат разбора документа целиком (Schedule A / Deed / Survey PDF)."""

    model_config = ConfigDict(extra="forbid")

    main_street: str = Field(
        description="Улица, на которую выходит фасад участка (fronting ...)"
    )
    main_street_note: str | None = Field(
        default=None,
        description=(
            "Пояснение под названием фасадной улицы, например "
            "'(38\\' WIDE ROADWAY EASEMENT)' или '(NOT MAPPED ON FINAL CITY PLAN)'"
        ),
    )
    cross_street: str | None = Field(
        default=None,
        description="Пересекаемая улица, от которой отмеряется расстояние до угла",
    )
    distance_to_corner_ft: float | None = Field(
        default=None,
        description=(
            "Расстояние вдоль фасадной улицы до угла/пересечения, футы "
            "(0 для углового участка; null если не указано)"
        ),
    )
    tie_note: str | None = Field(
        default=None,
        description=(
            "Дополнительная подпись привязки к углу квартала (например, "
            "'208.02\\' (DEED)' при многозвенной привязке COMMENCING AT)"
        ),
    )
    front_side: Literal["N", "S", "E", "W", "NE", "NW", "SE", "SW"] | None = Field(
        default=None,
        description=(
            "Направление света (N/S/E/W/NE/NW/SE/SW), куда смотрит фасадная граница "
            "(наружу от участка к улице). Если участок на South side улицы -> N; "
            "если на Northwesterly side улицы -> SE"
        ),
    )
    front_segment_index: int | None = Field(
        default=None,
        description=(
            "0-based индекс элемента в segments, являющегося фасадной границей "
            "вдоль main_street (null если неизвестно)"
        ),
    )
    cross_street_side_index: int | None = Field(
        default=None,
        description=(
            "Для углового участка: 0-based индекс элемента в segments, "
            "выходящего на cross_street (null для рядового участка)"
        ),
    )
    block_number: str | None = Field(
        default=None,
        description="Номер квартала (Block), например '3960' или '2366'",
    )
    lot_number: str | None = Field(
        default=None,
        description="Номер лота (Lot), например '129' или '59'",
    )
    borough_or_city: str | None = Field(
        default=None,
        description="Район/город/округ, например 'Staten Island, NY' или 'Queens, NY'",
    )
    segments: list[Segment] = Field(
        min_length=1,
        description="Стороны участка по порядку обхода (каждый call = один элемент)",
    )

    @field_validator("main_street", mode="before")
    @classmethod
    def _normalize_main_street(cls, value: Any) -> str:
        return _clean_name(value) or ""

    @field_validator(
        "main_street_note",
        "cross_street",
        "tie_note",
        "block_number",
        "lot_number",
        "borough_or_city",
        mode="before",
    )
    @classmethod
    def _normalize_optional_text(cls, value: Any) -> str | None:
        return _clean_name(value)

    @field_validator("distance_to_corner_ft", mode="before")
    @classmethod
    def _normalize_corner_distance(cls, value: Any) -> float | None:
        return _coerce_float(value)

    @field_validator("front_segment_index", "cross_street_side_index", mode="before")
    @classmethod
    def _normalize_optional_index(cls, value: Any) -> int | None:
        num = _coerce_float(value)
        if num is None:
            return None
        idx = int(round(num))
        return idx if idx >= 0 else None

    @field_validator("front_side", mode="before")
    @classmethod
    def _normalize_front_side(cls, value: Any) -> str | None:
        if value is None:
            return None
        token = str(value).strip().upper()
        if token in {"N", "S", "E", "W", "NE", "NW", "SE", "SW"}:
            return token
        mapping = {
            "NORTH": "N",
            "SOUTH": "S",
            "EAST": "E",
            "WEST": "W",
            "NORTHEAST": "NE",
            "NORTHWEST": "NW",
            "SOUTHEAST": "SE",
            "SOUTHWEST": "SW",
        }
        if token in mapping:
            return mapping[token]
        letter = token[:1]
        return letter if letter in {"N", "S", "E", "W"} else None

    @field_validator("segments", mode="before")
    @classmethod
    def _drop_empty_segments(cls, value: Any) -> Any:
        if value is None:
            return []
        if not isinstance(value, Iterable) or isinstance(value, (str, bytes)):
            return value
        cleaned = []
        for item in value:
            if item is None:
                continue
            if isinstance(item, dict) and not any(
                v not in (None, "", 0, 0.0, False) for v in item.values()
            ):
                continue
            cleaned.append(item)
        return cleaned

    @model_validator(mode="after")
    def _check_geometry(self) -> "LegalDescription":
        if not self.segments:
            raise ValueError("в описании не найдено ни одной стороны участка")
        if len(self.segments) < 3:
            raise ValueError(
                f"для замкнутого участка нужно минимум 3 стороны, найдено {len(self.segments)}"
            )
        if self.front_segment_index is not None and self.front_segment_index >= len(self.segments):
            self.front_segment_index = None
        if (
            self.cross_street_side_index is not None
            and self.cross_street_side_index >= len(self.segments)
        ):
            self.cross_street_side_index = None
        return self

    @property
    def perimeter_ft(self) -> float:
        return sum(seg.distance_ft for seg in self.segments)

    def to_json(self, indent: int = 2) -> str:
        return self.model_dump_json(indent=indent)


# ---------------------------------------------------------------------------
# Чтение и рендер PDF (текстовый слой + Vision PNG для векторных CAD/сканов)
# ---------------------------------------------------------------------------


def _open_fitz_doc(pdf_path: str | Path) -> tuple[Any, Path]:
    path = Path(pdf_path)
    if not path.exists():
        raise PDFError(f"файл не найден: {path}")
    if path.suffix.lower() != ".pdf":
        raise PDFError(f"ожидался PDF, получено: {path.name}")

    try:  # PyMuPDF >= 1.24 : современное имя модуля
        import pymupdf as fitz
    except ImportError:  # pragma: no cover - старые версии PyMuPDF
        try:
            import fitz  # type: ignore[no-redef]
        except ImportError as exc:
            raise PDFError(
                "PyMuPDF не установлен. В Windows выполните: pip install pymupdf"
            ) from exc

    try:
        doc = fitz.open(path)
    except Exception as exc:  # noqa: BLE001 - библиотека кидает разные типы
        raise PDFError(f"не удалось открыть PDF: {exc}") from exc
    return doc, path


def inspect_pdf(pdf_path: str | Path, max_pages: int = 0) -> dict[str, Any]:
    """
    Проверяет PDF без падения на векторных CAD-чертежах и сканах без текста.

    Возвращает ``{"pages": int, "has_text": bool, "text": str}``.
    """
    doc, _ = _open_fitz_doc(pdf_path)
    try:
        total_pages = len(doc)
        pages = range(total_pages) if not max_pages else range(min(max_pages, total_pages))
        chunks: list[str] = []
        for index in pages:
            page = doc.load_page(index)
            text = page.get_text("text") or ""
            if text.strip():
                chunks.append(text)
    finally:
        doc.close()

    joined = "\n".join(chunks)
    has_text = len(joined.strip()) >= 20
    has_legal_text = bool(has_text and _has_min_courses(_normalize_legal_text(joined)))
    return {
        "pages": total_pages,
        "has_text": has_text,
        "has_legal_text": has_legal_text,
        "text": joined,
    }


def read_pdf_text(pdf_path: str | Path, max_pages: int = 0) -> str:
    """
    Извлекает текстовый слой PDF через PyMuPDF (fitz).

    max_pages = 0 — читать все страницы. Векторные CAD-чертежи (SHX-шрифты в
    кривых) и сканы без OCR не имеют текстового слоя — в этом случае
    поднимается PDFError; для их разбора используйте ``render_pdf_pages_base64``
    или ``parse_legal_description_full(..., use_llm=True)`` (режим Vision).
    """
    info = inspect_pdf(pdf_path, max_pages=max_pages)
    if not info["has_text"]:
        raise PDFError(
            "в PDF нет текстового слоя (векторный CAD-чертёж или скан). "
            "Включите режим LLM с Vision-моделью или вставьте текст вручную."
        )
    return str(info["text"])


def render_pdf_pages_base64(
    pdf_path: str | Path,
    *,
    dpi: int = 220,
    max_pages: int = 4,
    max_side_px: int = 3200,
) -> list[str]:
    """
    Рендерит страницы PDF в PNG и возвращает список ``data:image/png;base64,...``
    для отправки векторных CAD-чертежей (Title Survey) и сканов в Vision LLM.
    Для крупноформатных листов (ARCH C/D/E: 18"×24", 24"×36", 36"×48") адаптивно
    ограничивает DPI по ``max_side_px``, исключая переполнение памяти и HTTP 413.
    """
    doc, _ = _open_fitz_doc(pdf_path)
    safe_dpi = max(96, min(int(dpi or 220), 350))
    safe_max_px = max(1200, int(max_side_px or 3200))
    limit = len(doc) if not max_pages or max_pages <= 0 else min(int(max_pages), len(doc))
    urls: list[str] = []
    try:
        for index in range(limit):
            page = doc.load_page(index)
            rect = page.rect
            max_inches = max(float(rect.width), float(rect.height)) / 72.0
            effective_dpi = safe_dpi
            if max_inches > 0:
                effective_dpi = min(safe_dpi, max(72, int(safe_max_px / max_inches)))
            pix = page.get_pixmap(dpi=effective_dpi, alpha=False)
            png_bytes = pix.tobytes("png")
            encoded = base64.b64encode(png_bytes).decode("ascii")
            urls.append(f"data:image/png;base64,{encoded}")
    finally:
        doc.close()
    if not urls:
        raise PDFError("PDF не содержит страниц для рендера")
    return urls


# ---------------------------------------------------------------------------
# Промпт для LLM (текст Schedule A / Deed + Vision-чертежи NYC Title Survey)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a senior licensed New York / USA land-survey technician.
You extract closed lot boundary geometry from a metes-and-bounds legal description (Schedule A / Deed) OR from an image of an existing Title / Architectural Survey plat into strict JSON.

CRITICAL SURVEY RULES
1. LOT BOUNDARY vs. TIE LINES ("COMMENCING AT"):
   - NEVER include tie/commencing calls from a distant street corner to the Point of Beginning (POB) in `segments`!
   - Example: if a deed or survey shows 1–3 tie calls from an intersection (e.g. 510.52', 433.78', 208.02' from Mill Rd & New Dorp Ln, or 101.42', 17.49', 1542.23' from Arthur Kill Rd) leading TO the lot corner (POB), EXCLUDE those tie calls from `segments`.
   - Put the immediate distance along `main_street` to the nearest corner into `distance_to_corner_ft` (use 0 for a corner lot), and put any compound tie or dual Deed/Tax-Map note (e.g. "208.02' (DEED) / 208.22' (T.M.)") into `tie_note`.
2. IGNORE BUILDING & FENCE OFFSETS:
   - Never confuse building wall dimensions (e.g. 13.8', 24.3', 50.7' inside 2-sty frame/masonry house) or fence/setback offsets ("FE. 0.1'W", "1.5'E", "25.4'") with lot boundary lines.
   - When both "(DEED)" and "(T.M.)" / "(TAX MAP)" values appear, prefer the "(DEED)" / record survey dimension.
3. QUADRANT BEARINGS vs. RELATIVE NYC COURSES (Queens / Brooklyn / Bronx / Manhattan):
   - Every element of `segments` must represent one lot boundary side in continuous loop order around the parcel, forming a CLOSED polygon (sum of dX ≈ 0, sum of dY ≈ 0). On survey plats, verify each bearing's N/S and E/W direction matches your chosen CW/CCW loop direction (flip N<->S AND E<->W if a plat labeled a wall in the opposite travel direction).
   - When explicit quadrant bearings are given, split into `ns` ("N"/"S"), `degrees` (0..90), `minutes` (0..59), `seconds` (decimal), `ew` ("E"/"W"), `bearing_label` ("N 75°20'20\" E"), and set `has_bearing = true`.
   - When NO compass bearings are printed (common in NYC surveys using "PARALLEL WITH ...", "AT RIGHT ANGLES TO ...", cardinal directions along streets, and/or an `interior_angle` like `93°33'29.5"`):
     * Align the primary East-West street to N 90°00' E / N 90°00' W (or North-South street to N 0°00' E / S 0°00' E) and apply exact interior/right angles so the polygon closes mathematically;
     * Set `has_bearing = false` on every course without an explicit compass bearing;
     * Put exact directional notes printed on that side verbatim (e.g. "PARALLEL WITH QUEENS MIDTOWN EXPY", "AT RIGHT ANGLES TO QUEENS MIDTOWN EXPRESSWAY") into `course_note`;
     * ALWAYS look inside the parcel corners for any printed vertex angle (e.g. `93°33'29.5"`) and record it in `interior_angle` of the segment starting at that corner!
4. DISTANCES:
   - `distance_ft` is in decimal feet. Convert feet-and-inches ("13 feet 8 inches" -> 13.6667, "25' 10\"" -> 25.8333, "55' 6\"" -> 55.5), varas (×2.77778), or chains (×66) to decimal feet.
5. STREETS, FRONTAGE & NYC METADATA:
   - `main_street`: primary street the lot fronts on (name only, e.g. "Dartmouth Loop", "Jamie Lane", "Borden Avenue").
   - `main_street_note`: parenthetical subtitle if shown, e.g. "(38' WIDE ROADWAY EASEMENT)", "(NOT MAPPED ON FINAL CITY PLAN)", "(QUEENS MIDTOWN EXPRESSWAY)", else null.
   - `front_segment_index`: 0-based index (0 = first segment) in `segments` of the lot boundary that fronts `main_street` (never pick a rear/side wall labeled PARALLEL WITH or AT RIGHT ANGLES!).
   - `front_side`: compass direction ("N", "S", "E", "W", "NE", "NW", "SE", "SW") pointing OUTWARD from the lot toward `main_street`. If the lot is on the North/Northerly side of Main Street -> "S"; if on the Northwesterly side -> "SE".
   - `cross_street`: intersecting street (e.g. "63rd Street", "Mill Road", "Kenilworth Avenue"), else null.
   - `cross_street_side_index`: 0-based index in `segments` of the side abutting `cross_street` ONLY if this is a corner lot (`distance_to_corner_ft == 0`), else null.
   - `block_number`, `lot_number`, `borough_or_city`: extract NYC/county Block, Lot, and Borough/City if present, else null.
6. Curves: use chord bearing + chord distance as one straight segment. Never invent calls; return strict JSON only."""

USER_PROMPT_TEMPLATE = """LEGAL DESCRIPTION:
\"\"\"
{text}
\"\"\"

Extract the lot geometry and survey metadata into JSON."""

VISION_USER_PROMPT = """Examine the attached survey / title document page(s) carefully.
1. Read the exact lot boundary dimensions and bearings (or NYC relative notes like PARALLEL WITH ... / AT RIGHT ANGLES TO ...).
2. Check inside the lot corners for any printed interior angle (such as 93°33'29.5") and put it in `interior_angle`!
3. Read street names, street subtitles in parentheses, corner tie distances, and Block/Lot/Borough.
4. Walk the parcel boundary in one continuous closed loop (sum of dX ≈ 0, sum of dY ≈ 0), excluding COMMENCING / corner tie lines and building/fence offsets from `segments`."""


def _messages(
    text: str,
    image_data_urls: Sequence[str] | None = None,
    *,
    system_override: str | None = None,
) -> list[dict[str, Any]]:
    sys_content = system_override if system_override is not None else SYSTEM_PROMPT
    if not image_data_urls:
        return [
            {"role": "system", "content": sys_content},
            {"role": "user", "content": USER_PROMPT_TEMPLATE.format(text=text)},
        ]

    prompt_parts = [VISION_USER_PROMPT]
    if text and text.strip():
        prompt_parts.append(f"ADDITIONAL NOTES / TEXT:\n\"\"\"\n{text.strip()}\n\"\"\"")
    user_blocks: list[dict[str, Any]] = [
        {"type": "text", "text": "\n\n".join(prompt_parts)}
    ]
    for url in image_data_urls:
        user_blocks.append({"type": "image_url", "image_url": {"url": url}})
    return [
        {"role": "system", "content": sys_content},
        {"role": "user", "content": user_blocks},
    ]


# ---------------------------------------------------------------------------
# Работа с LLM и учёт токенов
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenUsage:
    """Статистика отправленных и полученных токенов за вызов LLM."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    provider: str = ""
    provider_label: str = ""
    model: str = ""
    available: bool = True

    def short_report(self) -> str:
        target = " / ".join(
            part for part in (self.provider_label or self.provider, self.model) if part
        )
        badge = f"  [{target}]" if target else ""
        if not self.available:
            return f"Токены за вызов: провайдер не вернул usage{badge}"
        return (
            f"Токены за вызов: ↑ ушло {self.prompt_tokens:,}  |  "
            f"↓ пришло {self.completion_tokens:,}  |  "
            f"всего {self.total_tokens:,}{badge}"
        )


def _extract_usage_counts(completion: Any) -> tuple[int, int, int] | None:
    """Достаёт (prompt_tokens, completion_tokens, total_tokens) из ответа SDK."""
    raw = (
        completion.get("usage")
        if isinstance(completion, dict)
        else getattr(completion, "usage", None)
    )
    if raw is None:
        return None

    def _get(obj: Any, *names: str) -> int | None:
        for name in names:
            val = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                return int(val)
        return None

    prompt = _get(raw, "prompt_tokens", "input_tokens")
    completion_tok = _get(raw, "completion_tokens", "output_tokens")
    total = _get(raw, "total_tokens")
    if prompt is None and completion_tok is None and total is None:
        return None
    p_val = int(prompt or 0)
    c_val = int(completion_tok or 0)
    t_val = int(total) if total is not None and total > 0 else (p_val + c_val)
    return p_val, c_val, t_val


def _make_client(
    api_cfg: dict[str, Any],
    *,
    target: dict[str, str],
) -> Any:
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover
        raise LLMParseError(
            "пакет openai не установлен. Выполните: pip install openai"
        ) from exc

    key = target["api_key"]
    provider_label = target["provider_label"]
    env_hint = f" (или переменную {target['env_key']})" if target.get("env_key") else ""
    if not key:
        raise LLMParseError(
            f"не задан API-ключ для провайдера {provider_label}: "
            f"впишите его в поле «{provider_label}» в окне программы, "
            f"в config.json{env_hint}"
        )
    base_url = target["base_url"] or None
    return OpenAI(
        api_key=key,
        base_url=base_url,
        timeout=float(api_cfg.get("timeout_sec") or 180),
        max_retries=int(api_cfg.get("max_retries") or 0),
        default_headers={
            "HTTP-Referer": "https://github.com/Berber86/CAD",
            "X-Title": "Title Survey AI",
        },
    )


def _is_auth_error(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None)
    if status in (401, 403):
        return True
    msg = str(exc).lower()
    return "401" in msg and ("unauthorized" in msg or "invalid api key" in msg or "auth" in msg)


def _call_with_temperature(func: Callable[..., Any], **kwargs: Any) -> Any:
    """Некоторые модели (o-series, gpt-5, reasoning) не принимают temperature."""
    try:
        return func(**kwargs)
    except Exception as exc:  # noqa: BLE001
        message = str(exc).lower()
        if "temperature" in message and "temperature" in kwargs:
            kwargs.pop("temperature")
            return func(**kwargs)
        raise


def _unwrap_llm_payload(obj: Any) -> Any:
    """Снимает лишнюю обёртку вроде ``[{"main_street": ...}]`` или ``{"result": {...}}``."""
    if isinstance(obj, list) and len(obj) == 1 and isinstance(obj[0], dict):
        obj = obj[0]
    if isinstance(obj, dict) and "segments" not in obj:
        for val in obj.values():
            if isinstance(val, dict) and "segments" in val:
                return val
    return obj


def _json_from_text(raw: str) -> Any:
    """Достаёт JSON-объект из ответа модели (снимает <think>, ```json и фигурные скобки в прозе)."""
    text = raw.strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
    if "</think>" in text.lower():
        idx = text.lower().rfind("</think>")
        text = text[idx + len("</think>") :].strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    try:
        return _unwrap_llm_payload(json.loads(text))
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for idx, ch in enumerate(text):
            if ch not in "{[":
                continue
            try:
                parsed, _ = decoder.raw_decode(text, idx)
                unwrapped = _unwrap_llm_payload(parsed)
                if isinstance(unwrapped, dict):
                    return unwrapped
            except json.JSONDecodeError:
                continue
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return _unwrap_llm_payload(json.loads(text[start : end + 1]))
        raise


def make_strict_schema(model_cls: type[BaseModel]) -> dict[str, Any]:
    """
    JSON-схема в формате strict Structured Outputs: additionalProperties=false
    и все свойства в required. Использует нормализатор из openai SDK, если он
    доступен, иначе применяет эквивалентное преобразование вручную.
    """
    try:
        from openai.lib._pydantic import to_strict_json_schema

        return to_strict_json_schema(model_cls)
    except Exception:  # noqa: BLE001 - старые версии SDK
        raw_schema = copy.deepcopy(model_cls.model_json_schema())

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            for key in ("$defs", "definitions"):
                if isinstance(node.get(key), dict):
                    node[key] = {name: walk(value) for name, value in node[key].items()}
            if node.get("type") == "object":
                node.setdefault("additionalProperties", False)
                properties = node.get("properties")
                if isinstance(properties, dict):
                    node["required"] = list(properties)
                    node["properties"] = {
                        name: walk(value) for name, value in properties.items()
                    }
            if isinstance(node.get("items"), dict):
                node["items"] = walk(node["items"])
            if isinstance(node.get("anyOf"), list):
                node["anyOf"] = [walk(value) for value in node["anyOf"]]
            if isinstance(node.get("allOf"), list):
                node["allOf"] = [walk(value) for value in node["allOf"]]
            if node.get("default", "keep") is None:
                node.pop("default", None)
        return node

    return walk(raw_schema)


def _extract_with_llm(
    text: str,
    cfg: dict[str, Any],
    api_key: str | None = None,
    progress: Callable[[str], None] | None = None,
    *,
    provider: str | None = None,
    model: str | None = None,
    key_secret: str = "",
    image_data_urls: Sequence[str] | None = None,
) -> tuple[LegalDescription, str, TokenUsage]:
    """
    Возвращает (Pydantic-модель, режим Structured Outputs, TokenUsage).
    Поддерживает как текстовый разбор Schedule A/Deed, так и мультимодальный
    Vision-разбор отрендеренных страниц PDF (image_data_urls).
    """
    api_cfg = cfg["api"]
    target = resolve_api_target(
        cfg, provider=provider, model=model, api_key=api_key, key_secret=key_secret
    )
    model_name = target["model"]
    provider_id = target["provider"]
    provider_label = target["provider_label"]
    temperature = api_cfg.get("temperature", 0.0)
    client = _make_client(api_cfg, target=target)
    messages = _messages(text, image_data_urls=image_data_urls)

    if progress:
        mode_tag = f" (Vision, {len(image_data_urls)} стр.)" if image_data_urls else ""
        progress(f"Запрос к {provider_label}: {model_name}{mode_tag} …")

    errors: list[str] = []
    prompt_acc = 0
    completion_acc = 0
    total_acc = 0
    saw_usage = False

    def record_usage(completion_obj: Any) -> None:
        nonlocal prompt_acc, completion_acc, total_acc, saw_usage
        counts = _extract_usage_counts(completion_obj)
        if counts is not None:
            saw_usage = True
            prompt_acc += counts[0]
            completion_acc += counts[1]
            total_acc += counts[2]

    def build_usage() -> TokenUsage:
        return TokenUsage(
            prompt_tokens=prompt_acc,
            completion_tokens=completion_acc,
            total_tokens=total_acc,
            provider=provider_id,
            provider_label=provider_label,
            model=model_name,
            available=saw_usage,
        )

    # 1) Самый надёжный путь: SDK сам строит strict-схему и валидирует ответ.
    try:
        beta_parse = getattr(getattr(client, "beta", None), "chat", None)
        if beta_parse is not None:
            completion = _call_with_temperature(
                client.beta.chat.completions.parse,
                model=model_name,
                messages=messages,
                response_format=LegalDescription,
                temperature=temperature,
            )
            record_usage(completion)
            message = completion.choices[0].message
            refusal = getattr(message, "refusal", None)
            if refusal:
                raise LLMParseError(f"модель отказалась отвечать: {refusal}")
            parsed = getattr(message, "parsed", None)
            if parsed is not None:
                return parsed, "structured-outputs", build_usage()
            raise LLMParseError("модель вернула пустой structured-ответ")
    except LLMParseError:
        raise
    except Exception as exc:  # noqa: BLE001
        if _is_auth_error(exc):
            raise LLMParseError(
                f"{provider_label}: ошибка авторизации (проверьте API-ключ): {exc}"
            ) from exc
        errors.append(f"parse(): {exc}")

    # 2) Ручная JSON-схема (совместимые провайдеры: Hydra, OpenRouter, RouterAI…)
    schema = make_strict_schema(LegalDescription)
    try:
        completion = _call_with_temperature(
            client.chat.completions.create,
            model=model_name,
            messages=messages,
            temperature=temperature,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "legal_description",
                    "strict": True,
                    "schema": schema,
                },
            },
        )
        record_usage(completion)
        content = completion.choices[0].message.content or ""
        return (
            LegalDescription.model_validate(_json_from_text(content)),
            "json_schema",
            build_usage(),
        )
    except Exception as exc:  # noqa: BLE001
        if _is_auth_error(exc):
            raise LLMParseError(
                f"{provider_label}: ошибка авторизации (проверьте API-ключ): {exc}"
            ) from exc
        errors.append(f"json_schema: {exc}")

    schema_hint = json.dumps(schema, ensure_ascii=False)
    guide = (
        "Return ONLY a JSON object that validates against this JSON Schema:\n"
        f"{schema_hint}"
    )
    guided_messages = _messages(
        text,
        image_data_urls=image_data_urls,
        system_override=SYSTEM_PROMPT + "\n\n" + guide,
    )

    # 3) Обычный JSON-mode + инструкция со схемой.
    try:
        completion = _call_with_temperature(
            client.chat.completions.create,
            model=model_name,
            messages=guided_messages,
            temperature=temperature,
            response_format={"type": "json_object"},
        )
        record_usage(completion)
        content = completion.choices[0].message.content or ""
        return (
            LegalDescription.model_validate(_json_from_text(content)),
            "json_object",
            build_usage(),
        )
    except Exception as exc:  # noqa: BLE001
        if _is_auth_error(exc):
            raise LLMParseError(
                f"{provider_label}: ошибка авторизации (проверьте API-ключ): {exc}"
            ) from exc
        errors.append(f"json_object: {exc}")

    # 4) Запрос без response_format — для бесплатных/reasoning-моделей OpenRouter/Hydra,
    #    которые отклоняют параметр response_format целиком.
    try:
        completion = _call_with_temperature(
            client.chat.completions.create,
            model=model_name,
            messages=guided_messages,
            temperature=temperature,
        )
        record_usage(completion)
        content = completion.choices[0].message.content or ""
        return (
            LegalDescription.model_validate(_json_from_text(content)),
            "prompt-json",
            build_usage(),
        )
    except Exception as exc:  # noqa: BLE001
        if _is_auth_error(exc):
            raise LLMParseError(
                f"{provider_label}: ошибка авторизации (проверьте API-ключ): {exc}"
            ) from exc
        errors.append(f"prompt-json: {exc}")

    detail = " | ".join(errors[-3:])
    vision_hint = (
        " (для PDF без текстового слоя убедитесь, что выбрана Vision-модель: "
        "например hydra-gemini-pro, nvidia/nemotron-3.5-super-vl:free или openai/gpt-4.1)"
        if image_data_urls
        else ""
    )
    raise LLMParseError(
        f"не удалось получить корректный JSON от {provider_label} ({model_name}){vision_hint}: {detail}"
    )


# ---------------------------------------------------------------------------
# Локальный regex-парсер (аварийный путь без LLM)
# ---------------------------------------------------------------------------

_NS = r"N(?:orth)?\.?"
_EW = r"E(?:ast)?\.?"

_DMS_BEARING_RE = re.compile(
    rf"(?P<ns>(?:{_NS})|(?:S(?:outh)?\.?))\s*,?\s*"
    r"(?P<deg>\d{1,3})\s*(?:°|º|deg(?:rees?)?\.?)\s*"
    r"(?P<min>\d{1,2})\s*(?:'|′|’|min(?:utes?)?\.?)\s*"
    r"(?P<sec>\d{1,2}(?:\.\d+)?)\s*(?:\"|″|”|''|sec(?:onds?)?\.?)\s*"
    rf"(?P<ew>(?:{_EW})|(?:W(?:est)?\.?))(?![A-Za-z])",
    re.IGNORECASE,
)

_DECIMAL_BEARING_RE = re.compile(
    rf"(?P<ns>{_NS}|S(?:outh)?\.?)\s*,?\s*(?P<ddeg>\d{{1,3}}\.\d+)\s*°?\s*"
    rf"(?P<ew>{_EW}|W(?:est)?\.?)(?![A-Za-z])",
    re.IGNORECASE,
)

# Румб без символов: «N 45 03 12 E» (частая ошибка OCR) — только как последняя
# попытка, иначе «N 45 E …» в посторонних фразах даёт ложные срабатывания.
_SPACED_BEARING_RE = re.compile(
    rf"(?P<ns>{_NS}|S(?:outh)?\.?)\s*,?\s*(?P<deg>\d{{1,3}})\s+"
    rf"(?P<min>\d{{1,2}})\s+(?P<sec>\d{{1,2}}(?:\.\d+)?)\s*"
    rf"(?P<ew>{_EW}|W(?:est)?\.?)(?![A-Za-z])",
    re.IGNORECASE,
)

_DISTANCE_RE = re.compile(
    r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?:'|′|ft\b\.?|feet\b|foot\b|foot|f\.)",
    re.IGNORECASE,
)

_STREET_SUFFIX = (
    r"(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Boulevard|Blvd|Lane|Ln|Way|Court|Ct|"
    r"Place|Pl|Parkway|Pkwy|Highway|Hwy|Circle|Cir|Trail|Trl|Terrace|Ter|Loop|Pike|"
    r"Expressway|Freeway|Alley|Row|Square|Sq|Crossing|Xing|Belt|Turnpike|Tpke|Bend|"
    r"Pass|Path|Run|Cove|Point|Ridge|Route|Rte|Farm[- ]to[- ]Market|Fm|County Road|"
    r"Cr|Street)\.?"
)

_STREET_RE = re.compile(
    rf"(?P<name>(?:[A-Za-z0-9][\w.'’-]*\s+){{0,3}}{_STREET_SUFFIX})(?![A-Za-z])",
    re.IGNORECASE,
)

_STOP_WORDS = {
    "the", "of", "and", "to", "from", "line", "lines", "side", "sides", "being",
    "feet", "foot", "ft", "a", "an", "at", "on", "in", "with", "along", "same",
    "right", "way", "thereof", "said", "east", "west", "north", "south",
    "easterly", "westerly", "northerly", "southerly", "beginning", "point",
    "corner", "intersection", "centerline", "center", "property", "lot", "block",
    "tract", "addition", "survey", "abstract", "county", "state", "city",
}


def _normalize_legal_text(text: str) -> str:
    """Приводит типографику к единому виду (градусы, минуты, секунды)."""
    replacements = {
        "\u00ba": "°", "\u02da": "°", "\u00b0": "°",
        "\u2032": "'", "\u2019": "'", "\u2018": "'", "\u02b9": "'",
        "\u2033": '"', "\u201d": '"', "\u201c": '"', "\u02ba": '"',
        "\u2013": "-", "\u2014": "-", "\u00a0": " ",
        "\r\n": "\n", "\r": "\n",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def _parse_quadrant(ns: str, ew: str) -> tuple[str, str]:
    return ns.strip().upper()[:1], ew.strip().upper()[:1]


def _distance_after(chunk: str) -> float | None:
    """
    Читает длину стороны: сначала проверяет связку «13 feet 8 inches» / «13' 8"»,
    затем явную пометку ``(DEED)``, затем обычное число в футах.
    """
    fi = _FEET_INCHES_STR_RE.search(chunk)
    deed_match = re.search(
        r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?:'|′|ft\b\.?|feet\b|foot\b)\s*\(\s*DEED\s*\)",
        chunk,
        re.IGNORECASE,
    )
    plain = _DISTANCE_RE.search(chunk)
    if fi and (not plain or fi.start() <= plain.start()):
        feet = float(fi.group("ft"))
        inches = float(fi.group("in"))
        if fi.group("fnum") and fi.group("fden") and float(fi.group("fden")) > 0:
            inches += float(fi.group("fnum")) / float(fi.group("fden"))
        return feet + inches / 12.0
    if deed_match and plain and deed_match.start() <= plain.start() + 30:
        return float(deed_match.group("num").replace(",", ""))
    if plain:
        return float(plain.group("num").replace(",", ""))
    return None


def _has_min_courses(body: str, minimum: int = 3) -> bool:
    if len(_bearing_matches(body)) >= minimum:
        return True
    return len(re.findall(r"\bTHENCE\b", body, flags=re.IGNORECASE)) >= minimum


def _split_preamble_and_body(text: str) -> tuple[str, str]:
    """
    Отделяет многозвенную привязку ``COMMENCING AT … TO THE POINT OR PLACE OF BEGINNING``
    (или ``distant the following … courses and distances … RUNNING THENCE``) от
    собственно обхода границ участка, чтобы курсы привязки к углу квартала не
    попадали в ``segments``.

    Важно: в некоторых техасских deed'ах сам обход участка начинается со слова
    ``COMMENCING at a point …`` и заканчивается единственным ``to the POINT OF BEGINNING``
    в самом конце документа — поэтому отрезаем преамбулу ТОЛЬКО если после первого
    ``BEGINNING`` действительно остаётся минимум 3 курса обхода участка.
    """
    commencing = re.search(
        r"\bCOMMENCING\b.*?\bto\s+(?:the\s+)?(?:true\s+)?"
        r"(?:point(?:\s+or\s+place)?\s+of\s+|place\s+of\s+)?BEGINNING\b[;,.:\s]*",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if commencing and _has_min_courses(text[commencing.end() :]):
        return text[: commencing.end()], text[commencing.end() :]

    multi_tie = re.search(
        r"\b(?:distant|measured)\s+(?:the\s+)?following\s+(?:\w+\s+)?courses?\s+"
        r"and\s+distances?\b.*?(?=\bRUNNING\s+THENCE\b)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if multi_tie and _has_min_courses(text[multi_tie.end() :]):
        return text[: multi_tie.end()], text[multi_tie.end() :]

    pob = re.search(r"\bBEGINNING\b", text, re.IGNORECASE)
    if pob:
        head = text[: pob.start()]
        tail = text[pob.start() :]
        if _bearing_matches(head) and _has_min_courses(tail):
            return head, tail
    return "", text


def _bearing_matches(text: str) -> list[re.Match[str]]:
    """Сначала румбы с символами °/', затем десятичные, затем «N 45 03 12 E»."""
    for pattern in (_DMS_BEARING_RE, _DECIMAL_BEARING_RE, _SPACED_BEARING_RE):
        matches = list(pattern.finditer(text))
        if matches:
            return matches
    return []


def _azimuth_to_quadrant_dict(azimuth_deg: float) -> dict[str, Any]:
    """Переводит азимут 0..360° от севера по часовой стрелке в поля Segment."""
    az = azimuth_deg % 360.0
    if az <= 90.0:
        ns, ew, angle = "N", "E", az
    elif az <= 180.0:
        ns, ew, angle = "S", "E", 180.0 - az
    elif az < 270.0:
        ns, ew, angle = "S", "W", az - 180.0
    else:
        ns, ew, angle = "N", "W", 360.0 - az
    deg = int(angle + 1e-9)
    rem_min = max(0.0, (angle - deg) * 60.0)
    minutes = int(rem_min + 1e-9)
    seconds = round(max(0.0, (rem_min - minutes) * 60.0), 2)
    deg, minutes, seconds = _carry_dms(deg, minutes, seconds)
    return {
        "ns": ns,
        "degrees": deg,
        "minutes": minutes,
        "seconds": seconds,
        "ew": ew,
    }


def _angle_diff_deg(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


_CARDINAL_AZIMUTH: dict[str, float] = {
    "north": 0.0,
    "northerly": 0.0,
    "northeast": 45.0,
    "northeasterly": 45.0,
    "east": 90.0,
    "easterly": 90.0,
    "southeast": 135.0,
    "southeasterly": 135.0,
    "south": 180.0,
    "southerly": 180.0,
    "southwest": 225.0,
    "southwesterly": 225.0,
    "west": 270.0,
    "westerly": 270.0,
    "northwest": 315.0,
    "northwesterly": 315.0,
}

_INTERIOR_ANGLE_RE = re.compile(
    r"interior\s+angle\s+of\s+"
    r"(?P<deg>\d{1,3})\s*(?:°|deg(?:rees?)?\.?)\s*"
    r"(?:(?P<min>\d{1,2})\s*(?:'|min(?:utes?)?\.?)\s*)?"
    r"(?:(?P<sec>\d{1,2}(?:\.\d+)?)\s*(?:\"|''|sec(?:onds?)?\.?))?",
    re.IGNORECASE,
)


def _extract_course_note(clause: str) -> str | None:
    parallel = re.search(
        r"\bparallel\s+with\s+(?:the\s+(?:[A-Za-z]+\s+){0,2}?side\s+of\s+)?"
        r"(?P<target>[A-Za-z0-9 .'-]+?)(?:\s*\((?P<paren>[^)]+)\))?(?=\s*(?:,|;|\b\d|\bfeet\b|\bft\b))",
        clause,
        re.IGNORECASE,
    )
    if parallel:
        label = (parallel.group("paren") or parallel.group("target") or "").strip(" ,;.")
        if label:
            return f"PARALLEL WITH {label.upper()}"
    right_ang = re.search(
        r"\b(?:at\s+right\s+angles\s+to|perpendicular\s+to)\s+"
        r"(?:the\s+(?:[A-Za-z]+\s+){0,2}?side\s+of\s+)?"
        r"(?P<target>[A-Za-z0-9 .'-]+?)(?:\s*\((?P<paren>[^)]+)\))?(?=\s*(?:,|;|\b\d|\bfeet\b|\bft\b))",
        clause,
        re.IGNORECASE,
    )
    if right_ang:
        label = (right_ang.group("paren") or right_ang.group("target") or "").strip(" ,;.")
        if label:
            return f"AT RIGHT ANGLES TO {label.upper()}"
    return None


def _segments_from_relative_nyc(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Разбирает описания участков Нью-Йорка (Queens / Brooklyn / Bronx / Manhattan)
    без компасных румбов — на кардинальных направлениях вдоль улиц, ``PARALLEL WITH``,
    ``AT RIGHT ANGLES TO`` и внутренних углах (``interior angle of 93°33'29.5"``).
    """
    parts = re.split(r"\b(?:RUNNING\s+)?THENCE\b", text, flags=re.IGNORECASE)
    if len(parts) < 4:
        return [], []
    clauses = parts[1:]

    parsed_clauses: list[dict[str, Any]] = []
    for clause in clauses:
        dist = _distance_after(clause)
        if dist is None:
            continue
        dir_match = re.search(
            r"\b(north(?:east|west)?(?:erly)?|south(?:east|west)?(?:erly)?|"
            r"east(?:erly)?|west(?:erly)?)\b",
            clause,
            re.IGNORECASE,
        )
        if not dir_match:
            continue
        nominal_az = _CARDINAL_AZIMUTH[dir_match.group(1).lower()]
        ia_match = _INTERIOR_ANGLE_RE.search(clause)
        ia_deg: float | None = None
        ia_label: str | None = None
        if ia_match:
            d = int(ia_match.group("deg"))
            m = int(ia_match.group("min") or 0)
            s = float(ia_match.group("sec") or 0.0)
            ia_deg = dms_to_degrees(d, m, s)
            sec_str = _fmt_seconds(s)
            ia_label = f'{d}°{m:02d}\'{sec_str}"' if s > 0 else f"{d}°{m:02d}'"
        parsed_clauses.append(
            {
                "clause": clause,
                "nominal_az": nominal_az,
                "distance_ft": dist,
                "ia_deg": ia_deg,
                "ia_label": ia_label,
                "right_angle": bool(
                    re.search(r"\b(?:at\s+right\s+angles|perpendicular)\b", clause, re.IGNORECASE)
                ),
                "course_note": _extract_course_note(clause),
            }
        )

    if len(parsed_clauses) < 3:
        return [], []

    # Замыкающий курс возвращается в POB вдоль базовой улицы; луч из POB внутрь
    # участка направлен противоположно замыкающему курсу (+180°).
    closing_az = parsed_clauses[-1]["nominal_az"]
    baseline_ray = (closing_az + 180.0) % 360.0
    street_azimuths: dict[str, float] = {}
    closing_along = re.search(
        r"\balong\s+(?:the\s+(?:[A-Za-z]+\s+){0,2}?side\s+of\s+)?(?P<st>[^,;:(]+)",
        parsed_clauses[-1]["clause"],
        re.IGNORECASE,
    )
    if closing_along:
        st_name = (_street_from_block(closing_along.group("st")) or "").lower()
        if st_name:
            street_azimuths[st_name] = baseline_ray

    segments: list[dict[str, Any]] = []
    prev_az: float | None = None
    for idx, item in enumerate(parsed_clauses):
        nom = item["nominal_az"]
        clause_str = item["clause"]
        ref_street_az: float | None = None
        rel_target = re.search(
            r"\b(?:parallel\s+with|at\s+right\s+angles\s+to|perpendicular\s+to)\s+"
            r"(?:the\s+(?:[A-Za-z]+\s+){0,2}?side\s+of\s+)?(?P<st>[^,;:(]+)",
            clause_str,
            re.IGNORECASE,
        )
        if rel_target:
            target_name = (_street_from_block(rel_target.group("st")) or "").lower()
            if target_name and target_name in street_azimuths:
                ref_street_az = street_azimuths[target_name]

        if item["ia_deg"] is not None:
            ref_ray = baseline_ray if idx == 0 or prev_az is None else (prev_az + 180.0) % 360.0
            cand_a = (ref_ray + item["ia_deg"]) % 360.0
            cand_b = (ref_ray - item["ia_deg"]) % 360.0
            az = cand_a if _angle_diff_deg(cand_a, nom) <= _angle_diff_deg(cand_b, nom) else cand_b
        elif item["right_angle"]:
            base_ref = ref_street_az if ref_street_az is not None else baseline_ray
            candidates_ra = [(base_ref + 90.0) % 360.0, (base_ref - 90.0) % 360.0]
            if ref_street_az is None and prev_az is not None:
                candidates_ra.extend([(prev_az + 90.0) % 360.0, (prev_az - 90.0) % 360.0])
            az = min(candidates_ra, key=lambda c: _angle_diff_deg(c, nom))
        elif ref_street_az is not None:
            cand_a = ref_street_az % 360.0
            cand_b = (ref_street_az + 180.0) % 360.0
            az = cand_a if _angle_diff_deg(cand_a, nom) <= _angle_diff_deg(cand_b, nom) else cand_b
        else:
            az = nom

        along_match = re.search(
            r"\balong\s+(?:the\s+(?:[A-Za-z]+\s+){0,2}?side\s+of\s+)?(?P<st>[^,;:(]+)",
            clause_str,
            re.IGNORECASE,
        )
        if along_match:
            along_name = (_street_from_block(along_match.group("st")) or "").lower()
            if along_name:
                street_azimuths[along_name] = az

        prev_az = az
        seg = _azimuth_to_quadrant_dict(az)
        seg["distance_ft"] = item["distance_ft"]
        seg["has_bearing"] = False
        seg["course_note"] = item["course_note"]
        seg["interior_angle"] = item["ia_label"]
        segments.append(seg)

    return segments, []


def _segments_from_regex(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    matches = _bearing_matches(text)
    if not matches:
        rel_segments, rel_warnings = _segments_from_relative_nyc(text)
        if rel_segments:
            return rel_segments, rel_warnings
        return [], ["в тексте не найдено ни одного румба (N 45°00'00\" E) — формат не распознан"]

    raw_segments: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        chunk = text[match.end() : end]
        distance = _distance_after(chunk)
        if distance is None:
            warnings.append(
                f"сторона #{index + 1} ({match.group(0).strip()}): не найдена длина — пропущена"
            )
            continue
        groups = match.groupdict()
        if groups.get("ddeg") is not None:
            deg_float = float(groups["ddeg"])
            degrees = int(deg_float)
            minutes_f = (deg_float - degrees) * 60
            minutes = int(minutes_f)
            seconds = round((minutes_f - minutes) * 60, 2)
        else:
            degrees = int(groups["deg"])
            minutes = int(groups["min"])
            seconds = float(groups["sec"])

        ns, ew = _parse_quadrant(groups["ns"], groups["ew"])
        if ns not in {"N", "S"} or ew not in {"E", "W"}:
            warnings.append(f"сторона #{index + 1}: некорректный румб — пропущена")
            continue
        raw_segments.append(
            {
                "ns": ns,
                "degrees": degrees,
                "minutes": minutes,
                "seconds": seconds,
                "ew": ew,
                "distance_ft": distance,
                "has_bearing": True,
            }
        )

    return raw_segments, warnings


def _street_mentions(text: str) -> list[tuple[int, str]]:
    """Все упоминания улиц в тексте: [(позиция, «Main Street»), …]."""
    mentions: list[tuple[int, str]] = []
    for match in _STREET_RE.finditer(text):
        name = _clean_street_candidate(match.group("name"))
        if name:
            mentions.append((match.start(), name))
    return mentions


def _title_case_if_lower(text: str) -> str:
    return text.title() if text.islower() else text


_SIZE_WORDS = {
    "north", "south", "east", "west", "old", "new", "big", "little", "upper",
    "lower", "great", "grand",
}
_NUMBER_WORDS = {
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
    "eighty", "ninety", "hundred", "first", "second", "third", "fourth", "fifth",
    "sixth", "seventh", "eighth", "ninth", "tenth",
}


def _clean_street_candidate(candidate: str) -> str | None:
    """
    Приводит фразу вроде «the South side of Ranch Road 620» к «Ranch Road 620»:
    срезает служебные слова/направление, сохраняет номер в конце.
    """
    text = re.sub(r"\s+", " ", candidate).strip(" ,;:-.")
    if not text:
        return None
    words = text.split()
    # Срезаем «the North side of», «East line of», «Southerly line of» с начала.
    while words:
        head = words[0].lower()
        if head in {"the", "of", "a", "an", "with", "by", "intersection", "corner", "formed"} or head in _DIRECTION_WORDS or head in {
            "side", "line", "lines", "property", "said", "fronting", "along",
        }:
            words.pop(0)
        else:
            break
    # Срезаем хвост из служебных слов («... Street, and being»).
    while words and words[-1].lower().strip(".,") in {
        "and", "being", "thereof", "of", "the", "same", "more", "less",
    }:
        words.pop()

    keep: list[str] = []
    for index, word in enumerate(words):
        cleaned = word.strip(".,;:")
        if not cleaned:
            continue
        keep.append(cleaned)
        if index == len(words) - 1:
            break
        # После названия улицы идёт номер («Ranch Road 620») — забираем его.
        if re.fullmatch(_STREET_SUFFIX, cleaned, re.IGNORECASE):
            rest = [w.strip(".,;:") for w in words[index + 1 :]]
            if rest and re.fullmatch(r"\d{1,4}[A-Za-z]?", rest[0]):
                keep.append(rest[0])
            elif rest and rest[0].lower() in _NUMBER_WORDS:
                keep.append(rest[0])
            break
    if not keep:
        return None
    return _title_case_if_lower(" ".join(keep))


_DIRECTION_WORDS = {
    "north", "south", "east", "west", "northeast", "northwest", "southeast",
    "southwest", "n", "s", "e", "w", "ne", "nw", "se", "sw",
    "northerly", "southerly", "easterly", "westerly",
    "northeasterly", "northwesterly", "southeasterly", "southwesterly",
}

#: «fronting the South side of …», «along the North line of …», «fronts on …»
_FRONT_RE = re.compile(
    r"(?:fronting|fronts?|facing|faces|abuts?|abutting)\s+"
    r"(?:(?:the|a|an)\s+)?(?:(?:[A-Za-z]+ly|[A-Za-z]+)\s+)?"
    r"(?:side|line|lines|right[- ]of[- ]way)?\s*"
    r"(?:of|on|to)?\s*"
    r"(?P<block>[^.;:]{0,80}?(?:%s|%s)[^.;:]{0,20})"
    % (_STREET_SUFFIX, r"\b\d{1,4}\b"),
    re.IGNORECASE,
)


def _street_from_block(block: str) -> str | None:
    """
    Достаёт название улицы из фразы. Номер маршрута, стоящий сразу после
    типа улицы («Ranch Road 620», «Highway 71»), приклеивается к названию.
    """
    mention = _STREET_RE.search(block)
    if not mention:
        return _clean_street_candidate(block)
    name = _clean_street_candidate(mention.group("name"))
    if not name:
        return None
    tail = re.match(r"\s*#?\s*(\d{1,4}[A-Za-z]?)\b", block[mention.end() :])
    if tail:
        return f"{name} {tail.group(1)}"
    return name


def _front_direction(prefix: str) -> str | None:
    """«fronts on the South side of» -> 'N' (направление наружу к улице)."""
    match = re.search(
        r"\b(?:the\s+)?([A-Za-z]+)\s+(?:side|line|lines|right[- ]of[- ]way)\b",
        prefix,
        re.IGNORECASE,
    )
    if not match:
        return None
    word = match.group(1).lower()
    mapping = {
        "north": "S",
        "northerly": "S",
        "south": "N",
        "southerly": "N",
        "east": "W",
        "easterly": "W",
        "west": "E",
        "westerly": "E",
        "northwest": "SE",
        "northwesterly": "SE",
        "northeast": "SW",
        "northeasterly": "SW",
        "southwest": "NE",
        "southwesterly": "NE",
        "southeast": "NW",
        "southeasterly": "NW",
    }
    return mapping.get(word)


def _extract_nyc_metadata(
    full_text: str,
    lot_body: str,
    main_street: str,
    cross_street: str | None,
    distance_to_corner: float | None,
    raw_segments: list[dict[str, Any]],
) -> dict[str, Any]:
    """Извлекает Block/Lot/Borough, примечания к улице и индексы примыкающих сторон."""
    meta: dict[str, Any] = {
        "main_street_note": None,
        "tie_note": None,
        "front_segment_index": None,
        "cross_street_side_index": None,
        "block_number": None,
        "lot_number": None,
        "borough_or_city": None,
    }
    blk = re.search(r"\bBlock\s*#?\s*(\d+[A-Za-z-]*)\b", full_text, re.IGNORECASE)
    if blk:
        meta["block_number"] = blk.group(1)
    lot = re.search(r"\bLot\s*#?\s*(\d+[A-Za-z-]*)\b", full_text, re.IGNORECASE)
    if lot:
        meta["lot_number"] = lot.group(1)
    boro = re.search(
        r"\bBorough\s+(?:and\s+County\s+)?of\s+"
        r"(Staten\s+Island|Queens|Brooklyn|Manhattan|Bronx|The\s+Bronx)\b",
        full_text,
        re.IGNORECASE,
    )
    if boro:
        meta["borough_or_city"] = f"{boro.group(1).strip()}, NY"

    if main_street:
        note_re = re.compile(
            re.escape(main_street) + r"\s*(\([^)]{3,80}\))",
            re.IGNORECASE,
        )
        nm = note_re.search(full_text)
        if nm:
            meta["main_street_note"] = nm.group(1).strip().upper()

    is_corner = distance_to_corner is not None and distance_to_corner <= 1e-6
    clauses = re.split(r"\b(?:RUNNING\s+)?THENCE\b", lot_body, flags=re.IGNORECASE)[1:]
    if len(clauses) == len(raw_segments):
        for idx, clause in enumerate(clauses):
            if (
                main_street
                and re.search(
                    r"\balong\b[^.;]{0,50}?" + re.escape(main_street),
                    clause,
                    re.IGNORECASE,
                )
                and meta["front_segment_index"] is None
            ):
                meta["front_segment_index"] = idx
            if (
                is_corner
                and cross_street
                and re.search(
                    r"\balong\b[^.;]{0,50}?" + re.escape(cross_street),
                    clause,
                    re.IGNORECASE,
                )
                and meta["cross_street_side_index"] is None
            ):
                meta["cross_street_side_index"] = idx

    return meta


def _extract_streets(
    text: str,
    *,
    preamble: str = "",
) -> tuple[
    str,
    str | None,
    float | None,
    "Literal['N','S','E','W'] | None",
    str | None,
    list[str],
]:
    """Возвращает (main_street, cross_street, distance_to_corner, front_side, tie_note, warnings)."""
    warnings: list[str] = []
    main_street = ""
    cross_street: str | None = None
    distance_to_corner: float | None = None
    front_side: str | None = None
    tie_note: str | None = None
    body = text[len(preamble) :] if preamble and text.startswith(preamble) else text

    # 0. Угловой участок NYC: «BEGINNING at the corner formed by the intersection of
    #    the northerly side of Borden Avenue ... with the easterly side of 63rd Street».
    corner_pob = re.search(
        r"\bBEGINNING\s+at\s+(?:a\s+point\s+at\s+|the\s+)?corner\s+formed\s+by\s+"
        r"the\s+intersection\s+of\s+(?P<first>[^;]+?)\s+(?:with|and)\s+(?P<second>[^;:]+)",
        body,
        re.IGNORECASE,
    )
    if corner_pob:
        s1 = _street_from_block(corner_pob.group("first"))
        s2 = _street_from_block(corner_pob.group("second"))
        if s1:
            main_street = s1
            front_side = _front_direction(corner_pob.group("first"))
        if s2 and s2.lower() != main_street.lower():
            cross_street = s2
        distance_to_corner = 0.0

    # 1. Явная фраза «fronting / fronts on …» (сначала в теле участка, затем во всём тексте).
    if not main_street:
        fronting = _FRONT_RE.search(body) or _FRONT_RE.search(text)
        if fronting:
            src = body if fronting.string is body else text
            block = fronting.group("block")
            prefix = src[fronting.start() : fronting.start("block")] + block
            front_side = _front_direction(prefix)
            main_street = _street_from_block(block) or ""

    # 2. Конец привязки COMMENCING: «... to (a point on) the northwesterly side of Dartmouth Loop ...,
    #    the POINT OR PLACE OF BEGINNING».
    if not main_street and preamble:
        to_pob_street = re.search(
            r"\bto\s+(?:a\s+point\s+on\s+)?(?P<prefix>(?:the\s+)?[A-Za-z]+\s+"
            r"(?:side|line|right[- ]of[- ]way)\s+of\s+)(?P<block>[^,;:]{0,80})",
            preamble,
            re.IGNORECASE,
        )
        if to_pob_street:
            front_side = _front_direction(to_pob_street.group("prefix"))
            main_street = _street_from_block(to_pob_street.group("block")) or ""

    # 3. «BEGINNING at a point on the northwesterly side of Dartmouth Loop …»
    if not main_street:
        pob_on = re.search(
            r"\bBEGINNING\s+at\s+a\s+point\s+on\s+(?P<prefix>(?:the\s+)?[A-Za-z]+\s+"
            r"(?:side|line|right[- ]of[- ]way)\s+of\s+)(?P<block>[^,;:]{0,80})",
            text,
            re.IGNORECASE,
        )
        if pob_on:
            front_side = _front_direction(pob_on.group("prefix"))
            main_street = _street_from_block(pob_on.group("block")) or ""

    # 4. В теле участка: «along (the ... side of) <Street>».
    if not main_street:
        along_body = re.search(
            r"\balong\s+(?P<prefix>(?:(?:the\s+)?(?:[A-Za-z]+\s+)?(?:side|line|right[- ]of[- ]way)\s+of\s+)?)"
            r"(?P<block>[^,;:]{0,80})",
            body,
            re.IGNORECASE,
        )
        if along_body:
            cand = _street_from_block(along_body.group("block"))
            if cand:
                front_side = front_side or _front_direction(along_body.group("prefix"))
                main_street = cand

    # 5. Общий случай: «... North line of Main Street» / первое упоминание улицы (сначала в body).
    if not main_street:
        side_of = re.search(
            r"(?:side|line|lines|right[- ]of[- ]way)\s+(?:of|at)\s+(?:the\s+)?(?P<block>[^.;:]{0,80})",
            body,
            re.IGNORECASE,
        ) or re.search(
            r"(?:side|line|lines|right[- ]of[- ]way)\s+(?:of|at)\s+(?:the\s+)?(?P<block>[^.;:]{0,80})",
            text,
            re.IGNORECASE,
        )
        if side_of:
            main_street = _street_from_block(side_of.group("block")) or ""
    if not main_street:
        mentions = _street_mentions(body) or _street_mentions(text)
        if mentions:
            main_street = mentions[0][1]

    # 6. Пересекаемая улица: «... feet East of the East line of Oak Avenue» или угол из COMMENCING.
    if not cross_street:
        cross_patterns = [
            r"(?:feet|ft|')\s+(?:East|West|North|South|Easterly|Westerly|Northerly|Southerly)?\s*"
            r"(?:of|from)\s+the\s+(?:[A-Za-z]+\s+){0,2}?"
            r"(?:line|side|right[- ]of[- ]way|centerline|corner)\s+of\s+(?P<block>[^.;:]{0,80})",
            r"\bintersection\s+of\s+(?:the\s+(?:[A-Za-z]+\s+){0,2}?side\s+of\s+)?(?P<block>[^.;:]{0,80})",
            r"(?:line|side|right[- ]of[- ]way)\s+of\s+(?P<block>[^.;:]{0,80})",
        ]
        for pattern in cross_patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                candidate = _street_from_block(match.group("block"))
                if candidate and candidate.lower() != main_street.lower():
                    cross_street = candidate
                    break
            if cross_street:
                break

    # 7. Расстояние до угла: обычное («150.00 feet East of ...») или последнее звено привязки COMMENCING.
    if distance_to_corner is None:
        distance_match = re.search(
            r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?:feet|ft|')\s*"
            r"(?:\([^)]*\)\s*)?"
            r"(?:East|West|North|South|Easterly|Westerly|Northerly|Southerly)?\s*"
            r"(?:of|from)\s+the\s+",
            text,
            re.IGNORECASE,
        )
        if distance_match:
            distance_to_corner = float(distance_match.group("num").replace(",", ""))
        elif preamble:
            tie_dists = list(_DISTANCE_RE.finditer(preamble))
            if tie_dists:
                last_tie = float(tie_dists[-1].group("num").replace(",", ""))
                distance_to_corner = last_tie
                deed_vs_tm = re.search(
                    r"(\d[\d,]*(?:\.\d+)?)\s*(?:feet|ft|')\s*\(\s*DEED\s*\)"
                    r"[^.;]{0,30}?(\d[\d,]*(?:\.\d+)?)\s*(?:feet|ft|')?\s*\(\s*T\.?M\.?\s*\)",
                    preamble,
                    re.IGNORECASE,
                )
                if deed_vs_tm:
                    distance_to_corner = float(deed_vs_tm.group(1).replace(",", ""))
                    tie_note = f"{distance_to_corner:.2f}' (DEED) / {float(deed_vs_tm.group(2).replace(',', '')):.2f}' (T.M.)"

    if not main_street:
        warnings.append("фасадная улица не распознана — подпись улицы не будет нанесена")
    if cross_street and distance_to_corner is None:
        warnings.append("пересекаемая улица найдена, но расстояние до угла не распознано")
    return main_street, cross_street, distance_to_corner, front_side, tie_note, warnings


def parse_with_regex(text: str) -> tuple[LegalDescription, list[str]]:
    """
    Локальный разбор без LLM. Возвращает (модель, список предупреждений).
    Поддерживает:
      * классические румбы DMS / десятичные градусы;
      * футы с дюймами («13 feet 8 inches»);
      * отсечение многозвенной привязки «COMMENCING AT … TO POB»;
      * относительные курсы Нью-Йорка («PARALLEL WITH …», «AT RIGHT ANGLES TO …»,
        «interior angle of 93°33'29.5"») без компасных румбов.
    """
    normalized = _normalize_legal_text(text)
    preamble, lot_body = _split_preamble_and_body(normalized)
    raw_segments, warnings = _segments_from_regex(lot_body)
    if not raw_segments:
        raise LegalDescriptionError(
            "локальный парсер не нашёл сторон участка: "
            + ("; ".join(warnings) or "текст не распознан")
        )
    (
        main_street,
        cross_street,
        distance_to_corner,
        front_side,
        tie_note,
        street_warnings,
    ) = _extract_streets(normalized, preamble=preamble)
    warnings.extend(street_warnings)

    meta = _extract_nyc_metadata(
        normalized,
        lot_body,
        main_street,
        cross_street,
        distance_to_corner,
        raw_segments,
    )
    if tie_note and not meta.get("tie_note"):
        meta["tie_note"] = tie_note

    try:
        model = LegalDescription.model_validate(
            {
                "main_street": main_street,
                "main_street_note": meta["main_street_note"],
                "cross_street": cross_street,
                "distance_to_corner_ft": distance_to_corner,
                "tie_note": meta["tie_note"],
                "front_side": front_side,
                "front_segment_index": meta["front_segment_index"],
                "cross_street_side_index": meta["cross_street_side_index"],
                "block_number": meta["block_number"],
                "lot_number": meta["lot_number"],
                "borough_or_city": meta["borough_or_city"],
                "segments": raw_segments,
            }
        )
    except Exception as exc:  # noqa: BLE001
        raise LegalDescriptionError(
            f"локальный парсер вернул некорректные данные: {exc}"
        ) from exc
    return model, warnings


# ---------------------------------------------------------------------------
# Эталонные реальные примеры NYC Title Survey (для кнопки «Пример» и тестов)
# ---------------------------------------------------------------------------

NYC_SAMPLES: dict[str, str] = {
    "147 Dartmouth Loop (Staten Island)": (
        "ALL that certain plot, piece or parcel of land, situate, lying and being "
        "in the Borough of Staten Island, County of Richmond, City and State of New York, "
        "known as Block 3960 Lot 129:\n"
        "COMMENCING at the corner formed by the intersection of the northeasterly side of "
        "Mill Road (80' adopted) with the northwesterly side of New Dorp Lane; "
        "THENCE N 34°55'50\" E along Mill Road 510.52 feet; "
        "THENCE N 55°04'10\" W 433.78 feet; "
        "THENCE S 33°58'44\" W 208.02 feet (DEED) / 208.22 feet (T.M.) to a point on the "
        "northwesterly side of Dartmouth Loop (38' wide roadway easement), the POINT OR PLACE OF BEGINNING;\n"
        "RUNNING THENCE S 33°58'44\" W along Dartmouth Loop, 14.00 feet;\n"
        "THENCE N 56°01'16\" W part of the distance through a party wall, 125.00 feet;\n"
        "THENCE N 33°58'44\" E, 14.00 feet;\n"
        "THENCE S 56°01'16\" E part of the distance through a party wall, 125.00 feet "
        "to the northwesterly side of Dartmouth Loop, the POINT OR PLACE OF BEGINNING."
    ),
    "283 Jamie Lane (Staten Island)": (
        "ALL that certain plot, piece or parcel of land, situate, lying and being "
        "in the Borough of Staten Island, County of Richmond, City and State of New York, "
        "known as Block 5744 Lot 112:\n"
        "COMMENCING at the monumented intersection of Kenilworth Avenue and Arthur Kill Road (80' adopted); "
        "THENCE S 73°57'40\" W 101.42 feet; "
        "THENCE S 09°22'42\" E 17.49 feet; "
        "THENCE S 04°43'12\" W 1,542.23 feet to the westerly side of Jamie Lane "
        "(Not Mapped on Final City Plan), the POINT OR PLACE OF BEGINNING;\n"
        "RUNNING THENCE S 07°13'39\" E along Jamie Lane, 13 feet 8 inches;\n"
        "THENCE S 82°46'21\" W, 25 feet 10 inches;\n"
        "THENCE N 07°13'39\" W, 13 feet 8 inches;\n"
        "THENCE N 82°46'21\" E, 25 feet 10 inches to the POINT OR PLACE OF BEGINNING."
    ),
    "63-01 Borden Ave (Queens — без румбов)": (
        "ALL that certain plot, piece or parcel of land, situate, lying and being "
        "in the Borough and County of Queens, City and State of New York, "
        "known as Block 2366 Lot 59:\n"
        "BEGINNING at the corner formed by the intersection of the northerly side of "
        "Borden Avenue (Queens Midtown Expressway) with the easterly side of 63rd Street (60' wide);\n"
        "RUNNING THENCE northerly along the easterly side of 63rd Street, 90.17 feet, "
        "forming an interior angle of 93°33'29.5\" with the northerly side of Borden Avenue;\n"
        "THENCE easterly parallel with Borden Avenue (Queens Midtown Expressway), 26.32 feet;\n"
        "THENCE southerly at right angles to Borden Avenue (Queens Midtown Expressway), 90.00 feet;\n"
        "THENCE westerly along the northerly side of Borden Avenue, 20.72 feet "
        "to the corner, the point or place of BEGINNING."
    ),
}


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------


@dataclass
class ParseOutcome:
    """Результат разбора + метаданные для GUI/логов."""

    data: LegalDescription
    source: str
    warnings: list[str] = field(default_factory=list)
    usage: TokenUsage | None = None
    provider: str = ""
    provider_label: str = ""
    model: str = ""
    llm_mode: str = ""


def _flip_segment_direction(seg: Segment) -> Segment:
    ns_flipped: Literal["N", "S"] = "S" if seg.ns == "N" else "N"
    ew_flipped: Literal["E", "W"] = "W" if seg.ew == "E" else "E"
    return Segment(
        ns=ns_flipped,
        degrees=seg.degrees,
        minutes=seg.minutes,
        seconds=seg.seconds,
        ew=ew_flipped,
        distance_ft=seg.distance_ft,
        has_bearing=seg.has_bearing,
        course_note=seg.course_note,
        interior_angle=seg.interior_angle,
    )


def _proper_edge_intersect(
    a1: tuple[float, float],
    a2: tuple[float, float],
    b1: tuple[float, float],
    b2: tuple[float, float],
) -> bool:
    def ccw_turn(p1: tuple[float, float], p2: tuple[float, float], p3: tuple[float, float]) -> float:
        return (p2[0] - p1[0]) * (p3[1] - p1[1]) - (p2[1] - p1[1]) * (p3[0] - p1[0])

    d1 = ccw_turn(a1, a2, b1)
    d2 = ccw_turn(a1, a2, b2)
    d3 = ccw_turn(b1, b2, a1)
    d4 = ccw_turn(b1, b2, a2)
    return (d1 * d2 < -1e-6) and (d3 * d4 < -1e-6)


def _trace_closure_and_validity(segments: Sequence[Segment]) -> tuple[float, bool]:
    x, y = 0.0, 0.0
    pts: list[tuple[float, float]] = [(0.0, 0.0)]
    for seg in segments:
        dx, dy = seg.vector
        x += dx
        y += dy
        pts.append((x, y))
    err = math.hypot(x, y)
    verts = pts[:-1]
    n = len(verts)
    area2 = 0.0
    for i in range(n):
        x1, y1 = verts[i]
        x2, y2 = verts[(i + 1) % n]
        area2 += x1 * y2 - x2 * y1
    if abs(area2) < 2.0:
        return err, False
    for i in range(n):
        a1, a2 = verts[i], verts[(i + 1) % n]
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue
            b1, b2 = verts[j], verts[(j + 1) % n]
            if _proper_edge_intersect(a1, a2, b1, b2):
                return err, False
    return err, True


def _repair_llm_geometry(
    model: LegalDescription,
    *,
    from_vision: bool = False,
) -> tuple[LegalDescription, list[str]]:
    """
    Упреждающая санитарная проверка ответа LLM / Vision-разбора чертежа:
      1) не допускает привязки main_street к тыльной/боковой границе PARALLEL WITH / AT RIGHT ANGLES;
      2) не допускает углового cross_street_side_index на рядовом участке (distance_to_corner_ft > 0.5);
      3) если на чертеже Survey подписи румбов были сняты без учёта направления обхода
         (или в произвольном порядке сторон) и контур разомкнут (> 0.5 ft), проверяет,
         существует ли перестановка/разворот румбов на 180°, дающий точное замыкание
         (<= 0.05 ft) простого многоугольника без самопересечений. Реальные ошибки длин
         в deed'е (например 130' вместо 120') не маскируются, т.к. не дают <= 0.05 ft.
    """
    notes: list[str] = []
    updates: dict[str, Any] = {}

    f_idx = model.front_segment_index
    if f_idx is not None and 0 <= f_idx < len(model.segments):
        c_note = (model.segments[f_idx].course_note or "").upper()
        if "PARALLEL WITH" in c_note or "AT RIGHT ANGLES" in c_note:
            updates["front_segment_index"] = None
            notes.append(
                "индекс фасада указывал на относительную тыльную/боковую границу — пересчитан по геометрии"
            )

    if (
        model.cross_street_side_index is not None
        and model.distance_to_corner_ft is not None
        and model.distance_to_corner_ft > 0.5
    ):
        updates["cross_street_side_index"] = None

    segs = list(model.segments)
    n = len(segs)
    err0, _ = _trace_closure_and_validity(segs)
    if 3 <= n <= 8 and err0 > 0.5:
        best_err = err0
        best_order: tuple[int, ...] | None = None
        best_flips: tuple[bool, ...] | None = None

        orders: list[tuple[int, ...]] = [tuple(range(n))]
        if from_vision and n <= 6:
            orders.extend((0, *perm) for perm in itertools.permutations(range(1, n)))

        for order in orders:
            ordered_base = [segs[i] for i in order]
            for flips in itertools.product((False, True), repeat=n):
                if order == tuple(range(n)) and not any(flips):
                    continue
                candidate = [
                    _flip_segment_direction(s) if fl else s
                    for s, fl in zip(ordered_base, flips)
                ]
                err, valid = _trace_closure_and_validity(candidate)
                if valid and err <= 0.05 and err < best_err:
                    best_err = err
                    best_order = order
                    best_flips = flips
            if best_order is not None and best_err <= 0.01:
                break

        if best_order is not None and best_flips is not None:
            repaired_segs = [
                _flip_segment_direction(segs[idx]) if fl else segs[idx]
                for idx, fl in zip(best_order, best_flips)
            ]
            updates["segments"] = repaired_segs
            old_to_new = {old_i: new_i for new_i, old_i in enumerate(best_order)}
            cur_f = updates.get("front_segment_index", model.front_segment_index)
            if cur_f is not None and cur_f in old_to_new:
                updates["front_segment_index"] = old_to_new[cur_f]
            cur_c = updates.get("cross_street_side_index", model.cross_street_side_index)
            if cur_c is not None and cur_c in old_to_new:
                updates["cross_street_side_index"] = old_to_new[cur_c]
            notes.append(
                f"скорректирован порядок/направление обхода сторон по замыканию контура "
                f"(невязка {err0:.2f} ft → {best_err:.3f} ft)"
            )

    if updates:
        model = model.model_copy(update=updates)
    return model, notes


def parse_legal_description_full(
    text: str | None = None,
    pdf_path: str | Path | None = None,
    cfg: dict[str, Any] | None = None,
    *,
    use_llm: bool = True,
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    key_secret: str = "",
    allow_regex_fallback: bool = False,
    progress: Callable[[str], None] | None = None,
) -> ParseOutcome:
    """
    Полный разбор: PDF (текстовый ИЛИ векторный/скан через Vision) / текст ->
    LLM (или локальный парсер) -> LegalDescription.
    """
    from pathlib import Path as _Path

    cfg = copy.deepcopy(cfg) if cfg is not None else load_config()
    warnings: list[str] = []
    image_data_urls: list[str] = []

    pdf_text = ""
    if pdf_path:
        p_name = _Path(pdf_path).name
        if progress:
            progress(f"Читаю PDF: {p_name}")
        pdf_info = inspect_pdf(pdf_path)
        if pdf_info["has_text"]:
            pdf_text = str(pdf_info["text"])
        if not pdf_info.get("has_legal_text", pdf_info["has_text"]):
            if use_llm:
                dpi = int(cfg["api"].get("vision_dpi") or 220)
                max_p = int(cfg["api"].get("vision_max_pages") or 4)
                reason = (
                    "нет текстового слоя"
                    if not pdf_info["has_text"]
                    else "чертёж Survey без текстового обхода румбов"
                )
                if progress:
                    progress(
                        f"PDF «{p_name}» ({reason}) — рендерю страницы ({dpi} DPI) для Vision LLM…"
                    )
                image_data_urls = render_pdf_pages_base64(
                    pdf_path, dpi=dpi, max_pages=max_p
                )
                warnings.append(
                    f"PDF «{p_name}» ({reason}) — "
                    f"распознан через Vision LLM ({len(image_data_urls)} стр.)"
                )
            elif not pdf_info["has_text"] and not (text and text.strip()):
                raise PDFError(
                    f"в PDF «{p_name}» нет текстового слоя (векторный чертёж AutoCAD или скан). "
                    "Включите «Использовать LLM» и выберите Vision-модель "
                    "(например, hydra-gemini-pro, nvidia/nemotron-3.5-super-vl:free или openai/gpt-4.1)."
                )

    combined = "\n\n".join(part for part in (pdf_text, text or "") if part and part.strip())
    combined = _normalize_legal_text(combined).strip()
    if not image_data_urls and len(combined) < 20:
        raise LegalDescriptionError("текст Legal Description пуст или слишком короткий")
    max_chars = int(cfg["api"].get("max_input_chars") or 60000)
    if len(combined) > max_chars:
        warnings.append(
            f"текст длиннее {max_chars} символов — обрезан до {max_chars} для запроса"
        )
        combined = combined[:max_chars]

    llm_error: Exception | None = None
    if use_llm:
        try:
            parsed_model, mode, usage = _extract_with_llm(
                combined,
                cfg,
                api_key=api_key,
                progress=progress,
                provider=provider,
                model=model,
                key_secret=key_secret,
                image_data_urls=image_data_urls or None,
            )
            parsed_model, repair_notes = _repair_llm_geometry(
                parsed_model, from_vision=bool(image_data_urls)
            )
            warnings.extend(repair_notes)
            if mode != "structured-outputs":
                warnings.append(f"провайдер не поддержал Structured Outputs, режим: {mode}")
            return ParseOutcome(
                data=parsed_model,
                source="llm-vision" if image_data_urls else "llm",
                warnings=warnings,
                usage=usage,
                provider=usage.provider,
                provider_label=usage.provider_label,
                model=usage.model,
                llm_mode=mode,
            )
        except Exception as exc:  # noqa: BLE001
            llm_error = exc
            if not allow_regex_fallback or image_data_urls:
                raise
            warnings.append(f"LLM недоступна ({exc}); использую локальный парсер")

    if progress:
        progress("Локальный разбор текста (regex)…")
    parsed_model, regex_warnings = parse_with_regex(combined)
    warnings.extend(regex_warnings)

    if llm_error is not None:
        warnings.insert(0, "геометрия получена локальным парсером — проверьте размеры вручную")
    return ParseOutcome(
        data=parsed_model,
        source="llm" if not llm_error and use_llm else "regex",
        warnings=warnings,
    )


def parse_legal_description(
    text: str | None = None,
    pdf_path: str | Path | None = None,
    cfg: dict[str, Any] | None = None,
    **kwargs: Any,
) -> LegalDescription:
    """Упрощённый вход: возвращает только Pydantic-модель."""
    return parse_legal_description_full(
        text=text, pdf_path=pdf_path, cfg=cfg, **kwargs
    ).data
