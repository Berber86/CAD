"""
telemetry.py
============
Отправка журналов программы разработчику через Supabase (PostgREST).

Архитектура:
  * ``TelemetryClient`` — фоновая очередь событий; сеть только в отдельном
    потоке, интерфейс никогда не блокируется и не падает из-за телеметрии.
  * События батчатся (по размеру и по таймеру) и отправляются одним
    ``POST /rest/v1/app_logs``; родители (``clients`` / ``sessions``)
    создаются upsert'ом при старте сессии.
  * Если сети нет или Supabase недоступен, события складываются в локальный
    outbox (JSONL) и досылаются при следующем запуске / следующей отправке.
  * Только стандартная библиотека (``urllib``) — новых зависимостей не нужно.

Конфигурация (приоритет сверху вниз):
  1. Переменные окружения ``SUPABASE_URL`` / ``SUPABASE_ANON_KEY`` /
     ``TSAI_TELEMETRY_ENABLED`` (``0``/``1``).
  2. ``config.local.json`` → раздел ``telemetry`` (не коммитится в git).
  3. ``config.json`` → раздел ``telemetry`` (для релиза сюда можно положить
     publishable-ключ: он публичный по дизайну и защищён RLS «только запись»).

Без URL/ключа или при ``enabled=false`` клиент — no-op: ничего не отправляет
и не пишет на диск. Отправка никогда не должна ломать работу программы:
все ошибки глушатся (при ``TSAI_TELEMETRY_DEBUG=1`` — печатаются в stderr).

Что отправляется (и что НЕ отправляется) — см. ``supabase/README.md``.
Коротко: уровень, категория, короткое сообщение, версия, метки времени и
анонимный ``install_id``. API-ключи, PIN и содержимое документов не отправляются:
сообщения проходят ``redact()`` перед постановкой в очередь.
"""

from __future__ import annotations

import json
import os
import platform
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "TelemetryClient",
    "TelemetryConfig",
    "get_client",
    "log",
    "redact",
    "infer_category",
    "load_telemetry_config",
    "install_id_path",
    "outbox_path",
    "reset_client_for_tests",
    "fetch_reports_for_viewer",
]

_BASE_DIR = Path(__file__).resolve().parent
_CONFIG_PATH = _BASE_DIR / "config.json"
_LOCAL_CONFIG_PATH = _BASE_DIR / "config.local.json"

# Лимиты: одно событие не больше 2000 символов, батч — до 50 событий.
MAX_MESSAGE_CHARS = 2000
DEFAULT_BATCH_SIZE = 50
DEFAULT_FLUSH_INTERVAL_SEC = 5.0
DEFAULT_HTTP_TIMEOUT_SEC = 8.0
MAX_QUEUE_SIZE = 1000
MAX_OUTBOX_LINES = 2000

# Уровни как в log_mode.py (дублируются строками, чтобы модуль не тянул GUI-зависимости).
LEVEL_INFO = "info"
LEVEL_KEY = "key"
LEVEL_WARN = "warn"
LEVEL_ERROR = "error"
_VALID_LEVELS = frozenset({LEVEL_INFO, LEVEL_KEY, LEVEL_WARN, LEVEL_ERROR})

# Маски секретов в тексте сообщений: tsai1:…, sk-…, Bearer …, длинные hex/base64-токены.
_SECRET_PATTERNS = (
    re.compile(r"tsai1:[A-Za-z0-9+/=_-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/=]{8,}", re.IGNORECASE),
    re.compile(r"\b(?:api[_-]?key|apikey|secret|password|passwd|pin)\s*[:=]\s*\S+", re.IGNORECASE),
)


def _debug_enabled() -> bool:
    return os.environ.get("TSAI_TELEMETRY_DEBUG", "").strip() in ("1", "true", "yes")


def _debug(message: str) -> None:
    if _debug_enabled():
        print(f"[telemetry] {message}", file=sys.stderr)


def redact(text: str, *, max_chars: int = MAX_MESSAGE_CHARS) -> str:
    """Вырезает секреты и обрезает сообщение до ``max_chars`` символов."""
    if text is None:
        return ""
    out = str(text)
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("[скрыто]", out)
    # Управляющие символы (кроме \n и \t) не нужны в БД и ломают JSONL.
    out = "".join(ch if (ch in ("\n", "\t") or ord(ch) >= 32) else " " for ch in out)
    out = out.strip()
    if len(out) > max_chars:
        # Оставляем место под многоточие, чтобы итог не превышал лимит.
        out = out[: max(0, max_chars - 1)].rstrip() + "…"
    return out


def infer_category(message: str) -> str:
    """Категория события по префиксу сообщения (Шаг 0/1/2/4, Развод, …)."""
    text = str(message or "").lstrip()
    for step in ("0", "1", "2", "4"):
        if text.startswith(f"Шаг {step}") or text.startswith(f"ШАГ {step}"):
            return f"step{step}"
    if text.startswith("Развод"):
        return "slide"
    if text.startswith("Набор PDF"):
        return "pdf_set"
    if text.startswith("Эконом"):
        return "economy"
    return "app"


@dataclass(frozen=True)
class TelemetryConfig:
    """Итоговая конфигурация телеметрии (файлы + окружение).

    Режим по требованию (2026-11-21): логи копятся локально и уходят только
    когда пользователь нажимает «Пожаловаться». ``auto_send=False`` означает
    «не слать фоном» — отправка только через ``send_report()``.
    """

    enabled: bool = False
    url: str = ""
    anon_key: str = ""
    auto_send: bool = False
    batch_size: int = DEFAULT_BATCH_SIZE
    flush_interval_sec: float = DEFAULT_FLUSH_INTERVAL_SEC
    http_timeout_sec: float = DEFAULT_HTTP_TIMEOUT_SEC

    @property
    def configured(self) -> bool:
        return bool(self.url.strip() and self.anon_key.strip())

    @property
    def active(self) -> bool:
        return bool(self.enabled and self.configured)


def _read_json_dict(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _deep_get_telemetry(cfg: dict[str, Any]) -> dict[str, Any]:
    section = cfg.get("telemetry") if isinstance(cfg, dict) else None
    return section if isinstance(section, dict) else {}


def load_telemetry_config(
    *,
    config_path: Path | str | None = None,
    local_config_path: Path | str | None = None,
    environ: dict[str, str] | None = None,
) -> TelemetryConfig:
    """Читает ``telemetry`` из config.json + config.local.json + окружения.

    ``environ`` нужен для тестов (по умолчанию — ``os.environ``).
    """
    env = os.environ if environ is None else environ
    base = _deep_get_telemetry(_read_json_dict(Path(config_path) if config_path else _CONFIG_PATH))
    local = _deep_get_telemetry(_read_json_dict(Path(local_config_path) if local_config_path else _LOCAL_CONFIG_PATH))
    merged: dict[str, Any] = {**base, **local}

    url = str(env.get("SUPABASE_URL") or merged.get("url") or "").strip().rstrip("/")
    anon_key = str(env.get("SUPABASE_ANON_KEY") or merged.get("anon_key") or "").strip()

    enabled_raw = env.get("TSAI_TELEMETRY_ENABLED")
    if enabled_raw is not None and str(enabled_raw).strip() != "":
        enabled = str(enabled_raw).strip().lower() in ("1", "true", "yes", "on")
    else:
        enabled = bool(merged.get("enabled", True))

    auto_send_raw = env.get("TSAI_TELEMETRY_AUTO_SEND")
    if auto_send_raw is not None and str(auto_send_raw).strip() != "":
        auto_send = str(auto_send_raw).strip().lower() in ("1", "true", "yes", "on")
    else:
        auto_send = bool(merged.get("auto_send", False))

    def _int(key: str, default: int) -> int:
        try:
            return max(1, int(merged.get(key, default)))
        except (TypeError, ValueError):
            return default

    def _float(key: str, default: float) -> float:
        try:
            return max(0.1, float(merged.get(key, default)))
        except (TypeError, ValueError):
            return default

    return TelemetryConfig(
        enabled=enabled,
        url=url,
        anon_key=anon_key,
        auto_send=auto_send,
        batch_size=_int("batch_size", DEFAULT_BATCH_SIZE),
        flush_interval_sec=_float("flush_interval_sec", DEFAULT_FLUSH_INTERVAL_SEC),
        http_timeout_sec=_float("http_timeout_sec", DEFAULT_HTTP_TIMEOUT_SEC),
    )


def _data_dir() -> Path:
    """Каталог локальных данных телеметрии (вне репозитория, не коммитится)."""
    local_app = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app:
        return Path(local_app) / "TitleSurveyAI"
    return Path.home() / ".cache" / "titlesurveyai"


def install_id_path(data_dir: Path | None = None) -> Path:
    return (data_dir or _data_dir()) / "install_id"


def outbox_path(data_dir: Path | None = None) -> Path:
    return (data_dir or _data_dir()) / "telemetry_outbox.jsonl"


def _load_or_create_install_id(path: Path) -> str:
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            uuid.UUID(existing)  # валидация формата
            return existing
    except (OSError, ValueError):
        pass
    new_id = str(uuid.uuid4())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_id + "\n", encoding="utf-8")
    except OSError as exc:  # диск недоступен — работаем с эфемерным id
        _debug(f"не удалось сохранить install_id: {exc}")
    return new_id


@dataclass
class _Event:
    level: str
    category: str
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def as_row(self, *, install_id: str, session_id: str, app_version: str) -> dict[str, Any]:
        return {
            "install_id": install_id,
            "session_id": session_id,
            "level": self.level,
            "category": self.category,
            "message": self.message,
            "metadata": self.metadata or {},
            "app_version": app_version,
            "created_at": self.created_at or datetime.now(timezone.utc).isoformat(),
        }


# Транспорт: (url, headers, payload_json_bytes, timeout) -> HTTP-статус.
# Подменяется в тестах; по умолчанию — urllib.
Transport = Callable[[str, dict[str, str], bytes, float], int]


def _urllib_transport(url: str, headers: dict[str, str], payload: bytes, timeout: float) -> int:
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        # 4xx/5xx от PostgREST — не сетевой сбой, но и не успех: возвращаем код,
        # вызывающий код решает (события уйдут в outbox, а не потеряются молча).
        return int(exc.code)


class TelemetryClient:
    """Фоновая очередь событий телеметрии. Потокобезопасна, никогда не бросает.

    ``transport`` и ``data_dir`` инъектируются для тестов; в проде — urllib и
    ``%LOCALAPPDATA%/TitleSurveyAI``.
    """

    def __init__(
        self,
        config: TelemetryConfig | None = None,
        *,
        app_version: str = "",
        transport: Transport | None = None,
        data_dir: Path | None = None,
    ) -> None:
        self.config = config if config is not None else load_telemetry_config()
        self.app_version = str(app_version or "")
        self._transport = transport or _urllib_transport
        self._data_dir = data_dir or _data_dir()
        self._install_id: str | None = None
        self._session_id: str | None = None
        self._queue: queue.Queue[_Event] = queue.Queue(maxsize=MAX_QUEUE_SIZE)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._started = False

    # -- идентификация -------------------------------------------------------
    @property
    def install_id(self) -> str:
        if self._install_id is None:
            self._install_id = _load_or_create_install_id(install_id_path(self._data_dir))
        return self._install_id

    @property
    def session_id(self) -> str:
        if self._session_id is None:
            self._session_id = str(uuid.uuid4())
        return self._session_id

    @property
    def active(self) -> bool:
        return self.config.active

    def reload_config(self, config: TelemetryConfig | None = None) -> None:
        """Перечитать конфиг (после изменения переключателя). Потокобезопасно."""
        try:
            self.config = config if config is not None else load_telemetry_config()
            if self.config.active and not self._started:
                self.start()
            elif self.config.active and self._started:
                # Уже запущен — убедимся, что воркер жив (если был no-op при disabled).
                self._ensure_worker()
        except Exception as exc:  # noqa: BLE001
            _debug(f"reload_config не удалось: {exc}")

    def set_enabled(self, enabled: bool) -> None:
        """Включить/выключить отправку без перезапуска процесса."""
        try:
            cur = self.config
            new_cfg = TelemetryConfig(
                enabled=bool(enabled),
                url=cur.url,
                anon_key=cur.anon_key,
                auto_send=cur.auto_send,
                batch_size=cur.batch_size,
                flush_interval_sec=cur.flush_interval_sec,
                http_timeout_sec=cur.http_timeout_sec,
            )
            self.reload_config(new_cfg)
        except Exception as exc:  # noqa: BLE001
            _debug(f"set_enabled не удалось: {exc}")

    # -- публичный API --------------------------------------------------------
    def start(self) -> None:
        """Старт сессии: готовит install_id/session_id. Сеть — только если auto_send."""
        if self._started:
            return
        self._started = True
        if not self.active:
            _debug("телеметрия выключена или не настроена (нет URL/ключа) — no-op")
            return
        # install_id/session_id создаются лениво, но нужны для отчёта.
        try:
            _ = self.install_id
            _ = self.session_id
        except Exception:
            pass
        # В режиме «только по кнопке Пожаловаться» сеть не трогаем до явного send_report().
        if not self.config.auto_send:
            _debug("телеметрия в режиме по требованию — фоновая отправка отключена")
            return
        self._enqueue_control("session_start")
        self._ensure_worker()
        threading.Thread(target=self._flush_outbox, name="telemetry-outbox", daemon=True).start()

    def log(
        self,
        message: str,
        level: str = LEVEL_INFO,
        *,
        category: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Поставить событие в локальный буфер. Сеть — только если auto_send или send_report()."""
        if not self.active:
            return
        try:
            clean_level = str(level or LEVEL_INFO).strip().lower()
            if clean_level not in _VALID_LEVELS:
                clean_level = LEVEL_INFO
            event = _Event(
                level=clean_level,
                category=str(category or infer_category(message)),
                message=redact(message),
                metadata=dict(metadata or {}),
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            if not event.message:
                return
            try:
                self._queue.put_nowait(event)
            except queue.Full:
                try:
                    self._queue.get_nowait()
                    self._queue.put_nowait(event)
                except queue.Empty:  # pragma: no cover
                    pass
            # В режиме по требованию воркер не запускаем — отправим пачкой при send_report().
            if self.config.auto_send:
                self._ensure_worker()
        except Exception as exc:  # noqa: BLE001
            _debug(f"log() проигнорирован: {exc}")

    @property
    def pending_count(self) -> int:
        """Сколько событий ждёт отправки (в памяти)."""
        try:
            return int(self._queue.qsize())
        except Exception:
            return 0

    def send_report(
        self,
        user_message: str = "",
        log_entries: list[tuple[str, str]] | None = None,
        step0_entries: list[tuple[str, str]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Отправить отчёт по кнопке «Пожаловаться» (только по действию пользователя).

        Собирает upsert clients/sessions + batch app_logs + одну строку feedback.
        Возвращает ``{"ok": bool, "sent_logs": int, "detail": str}``.
        Никогда не бросает.
        """
        if not self.config.configured:
            return {"ok": False, "error": "телеметрия не настроена (нет URL/ключа)", "detail": "not configured"}
        if not self.config.enabled:
            return {"ok": False, "error": "телеметрия выключена", "detail": "disabled"}
        # Убедимся, что ids есть
        try:
            iid = self.install_id
            sid = self.session_id
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"install_id: {exc}", "detail": str(exc)}

        # 1) upsert родителей (best effort, не фейлим отчёт если не вышло)
        try:
            self._post_rows("clients", [{"install_id": iid, "app_version": self.app_version or None, "platform": platform.system() or None, "first_seen": datetime.now(timezone.utc).isoformat(), "last_seen": datetime.now(timezone.utc).isoformat()}], on_conflict="install_id")
            self._post_rows("sessions", [{"session_id": sid, "install_id": iid, "app_version": self.app_version or None, "started_at": datetime.now(timezone.utc).isoformat()}], on_conflict="session_id")
        except Exception as exc:  # noqa: BLE001
            _debug(f"send_report control upsert: {exc}")

        events: list[_Event] = []

        # 2) События из внутренней очереди (через log())
        drained: list[_Event] = []
        while True:
            try:
                drained.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if drained:
            events.extend(drained)

        # 3) События напрямую из main.py (_log / _step0_lines), если переданы
        def _from_pairs(pairs: list[tuple[str, str]] | None, default_cat: str | None = None) -> None:
            if not pairs:
                return
            for lvl, msg in pairs:
                try:
                    clean_lvl = str(lvl or LEVEL_INFO).strip().lower()
                    if clean_lvl not in _VALID_LEVELS:
                        clean_lvl = LEVEL_INFO
                    # msg может быть уже с таймштампом — redact обрежет лишнее
                    events.append(_Event(level=clean_lvl, category=infer_category(str(msg)) if default_cat is None else str(default_cat), message=redact(str(msg)), metadata={}, created_at=datetime.now(timezone.utc).isoformat()))
                except Exception:
                    continue
        if log_entries is not None:
            # log_entries перекрывает drained, чтобы не дублировать — если передано, считаем что это полный набор
            # но мы уже добавили drained; чтобы не дублировать, не добавляем снова если drained не пусто и это те же объекты?
            # Проще: если log_entries передано и drained пусто — строим из него.
            if not drained:
                _from_pairs(log_entries)
            else:
                # drained уже содержит то же — пропускаем
                pass
        elif drained:
            pass
        # Step0 добавляем отдельно (если не было в log_entries)
        if step0_entries:
            # чтобы не дублировать если log_entries уже включал step0, проверим что drained пуст
            if not drained:
                _from_pairs(step0_entries, default_cat="step0")

        # Ограничим размер отчёта, чтобы не упереться в лимиты БД
        max_logs = 400
        if len(events) > max_logs:
            events = events[-max_logs:]

        sent_logs = 0
        if events:
            # Шлём батчами
            rows_all = [e.as_row(install_id=iid, session_id=sid, app_version=self.app_version) for e in events]
            ok_all = True
            for start in range(0, len(rows_all), self.config.batch_size):
                chunk = rows_all[start:start + self.config.batch_size]
                if self._post_rows("app_logs", chunk):
                    sent_logs += len(chunk)
                else:
                    ok_all = False
                    # остаток — в outbox для повторной попытки при следующем отчёте
                    remaining_events = events[start:]
                    try:
                        self._save_to_outbox(remaining_events)
                    except Exception:
                        pass
                    break
            if not ok_all and sent_logs == 0:
                return {"ok": False, "error": "не удалось отправить логи (сеть/сервер)", "detail": "app_logs POST failed", "sent_logs": 0}

        # 4) Сообщение пользователя (feedback) — даже если логов нет
        fb_msg = str(user_message or "").strip()
        if fb_msg:
            # redact не нужен — это текст пользователя, но обрежем
            if len(fb_msg) > 4000:
                fb_msg = fb_msg[:3999] + "…"
            fb_ok = self._post_rows("feedback", [{"install_id": iid, "session_id": sid, "message": fb_msg, "is_read": False}])
            if not fb_ok:
                _debug("feedback POST failed")
                # не фейлим весь отчёт из-за feedback — логи уже ушли
        elif sent_logs == 0:
            # Пустой отчёт (ни логов, ни текста) — нечего слать
            return {"ok": False, "error": "пустой отчёт", "detail": "no logs and no message"}

        # 5) Пометить сессию завершённой (best effort)
        try:
            self._post_rows("sessions", [{"session_id": sid, "install_id": iid, "ended_at": datetime.now(timezone.utc).isoformat(), "exit_clean": True}], on_conflict="session_id")
        except Exception:
            pass

        # 6) Попытка дослать старый outbox (если был)
        try:
            self._flush_outbox()
        except Exception:
            pass

        return {"ok": True, "sent_logs": sent_logs, "detail": f"отправлено логов: {sent_logs}, сообщение: {'да' if fb_msg else 'нет'}"}

    def session_end(self, *, clean: bool = True) -> None:
        """Завершение сессии — в режиме по требованию ничего не шлёт (ждём кнопку)."""
        if not self.active or not self.config.auto_send:
            return
        self.log("Сессия завершена", LEVEL_KEY, category="session", metadata={"clean_exit": bool(clean)})
        self._enqueue_control("session_end")
        try:
            self.flush(timeout_sec=1.5)
        except Exception as exc:  # noqa: BLE001
            _debug(f"flush при закрытии не удался (события в outbox): {exc}")

    def flush(self, *, timeout_sec: float | None = None) -> int:
        """Отправить накопленное сейчас. Возвращает число отправленных событий."""
        if not self.active:
            return 0
        deadline = None if timeout_sec is None else time.monotonic() + max(0.0, timeout_sec)
        sent = 0
        while True:
            batch: list[_Event] = []
            while len(batch) < self.config.batch_size:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            if not batch:
                break
            if self._send_events(batch):
                sent += len(batch)
            else:
                self._save_to_outbox(batch)
            if deadline is not None and time.monotonic() > deadline:
                break
        return sent

    def close(self) -> None:
        self._stop.set()
        worker = self._worker
        if worker is not None and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=2.0)

    # -- внутреннее ------------------------------------------------------------
    def _ensure_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop.clear()
            self._worker = threading.Thread(
                target=self._worker_loop, name="telemetry-worker", daemon=True
            )
            self._worker.start()

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                first = self._queue.get(timeout=self.config.flush_interval_sec)
            except queue.Empty:
                continue
            batch = [first]
            deadline = time.monotonic() + self.config.flush_interval_sec
            while len(batch) < self.config.batch_size and time.monotonic() < deadline:
                try:
                    batch.append(self._queue.get(timeout=max(0.0, deadline - time.monotonic())))
                except queue.Empty:
                    break
            if self._send_events(batch):
                continue
            self._save_to_outbox(batch)

    def _enqueue_control(self, kind: str) -> None:
        """Служебные upsert'ы (клиент/сессия) — вне очереди событий."""
        threading.Thread(
            target=self._send_control, args=(kind,), name=f"telemetry-{kind}", daemon=True
        ).start()

    def _send_control(self, kind: str) -> None:
        try:
            if kind == "session_start":
                self._post_rows(
                    "clients",
                    [{
                        "install_id": self.install_id,
                        "app_version": self.app_version or None,
                        "platform": platform.system() or None,
                        "first_seen": datetime.now(timezone.utc).isoformat(),
                        "last_seen": datetime.now(timezone.utc).isoformat(),
                    }],
                    on_conflict="install_id",
                )
                self._post_rows(
                    "sessions",
                    [{
                        "session_id": self.session_id,
                        "install_id": self.install_id,
                        "app_version": self.app_version or None,
                        "started_at": datetime.now(timezone.utc).isoformat(),
                    }],
                    on_conflict="session_id",
                )
            elif kind == "session_end":
                self._post_rows(
                    "sessions",
                    [{
                        "session_id": self.session_id,
                        "install_id": self.install_id,
                        "ended_at": datetime.now(timezone.utc).isoformat(),
                        "exit_clean": True,
                    }],
                    on_conflict="session_id",
                )
        except Exception as exc:  # noqa: BLE001 - best effort
            _debug(f"control {kind} не отправился: {exc}")

    def _send_events(self, events: list[_Event]) -> bool:
        rows = [
            event.as_row(
                install_id=self.install_id, session_id=self.session_id, app_version=self.app_version
            )
            for event in events
        ]
        return self._post_rows("app_logs", rows)

    def _post_rows(
        self, table: str, rows: list[dict[str, Any]], *, on_conflict: str | None = None
    ) -> bool:
        if not rows:
            return True
        url = f"{self.config.url}/rest/v1/{table}"
        headers = {
            "apikey": self.config.anon_key,
            "Authorization": f"Bearer {self.config.anon_key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal" + (f",resolution=merge-duplicates" if on_conflict else ""),
        }
        if on_conflict:
            url = f"{url}?on_conflict={on_conflict}"
        payload = json.dumps(rows, ensure_ascii=False).encode("utf-8")
        try:
            status = self._transport(url, headers, payload, self.config.http_timeout_sec)
        except Exception as exc:  # noqa: BLE001 - сеть/таймаут → outbox
            _debug(f"POST {table} упал: {exc}")
            return False
        if 200 <= status < 300:
            return True
        _debug(f"POST {table} → HTTP {status}")
        return False

    def _save_to_outbox(self, events: list[_Event]) -> None:
        path = outbox_path(self._data_dir)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            lines = [
                json.dumps(
                    event.as_row(
                        install_id=self.install_id,
                        session_id=self.session_id,
                        app_version=self.app_version,
                    ),
                    ensure_ascii=False,
                )
                for event in events
            ]
            with path.open("a", encoding="utf-8") as stream:
                for line in lines:
                    stream.write(line + "\n")
            # Не раздуваем outbox бесконечно: режем до последних строк.
            try:
                all_lines = path.read_text(encoding="utf-8").splitlines()
                if len(all_lines) > MAX_OUTBOX_LINES:
                    path.write_text("\n".join(all_lines[-MAX_OUTBOX_LINES:]) + "\n", encoding="utf-8")
            except OSError:
                pass
        except OSError as exc:
            _debug(f"outbox не записан: {exc}")

    def _flush_outbox(self) -> None:
        path = outbox_path(self._data_dir)
        try:
            if not path.exists():
                return
            lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except OSError:
            return
        if not lines:
            return
        rows: list[dict[str, Any]] = []
        for line in lines:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        if not rows:
            return
        # Отправляем пачками; что не ушло — остаётся в файле.
        sent = 0
        for start in range(0, len(rows), self.config.batch_size):
            chunk = rows[start:start + self.config.batch_size]
            if self._post_rows("app_logs", chunk):
                sent += len(chunk)
            else:
                break
        remaining = rows[sent:]
        try:
            if remaining:
                path.write_text(
                    "\n".join(json.dumps(row, ensure_ascii=False) for row in remaining) + "\n",
                    encoding="utf-8",
                )
            else:
                path.unlink(missing_ok=True)
        except OSError as exc:
            _debug(f"outbox после досылки не обновлён: {exc}")


def _service_role_key(*, config: TelemetryConfig | None = None) -> str:
    """Ключ для чтения отчётов (только у разработчика, не коммитить)."""
    # 1) env, 2) config.local.json/service_role_key, 3) config.json/service_role_key
    import os as _os
    env_key = (_os.environ.get("SUPABASE_SERVICE_KEY") or _os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if env_key:
        return env_key
    for path in (_LOCAL_CONFIG_PATH, _CONFIG_PATH):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8") or "{}")
            sec = data.get("telemetry") if isinstance(data, dict) else None
            if isinstance(sec, dict):
                v = str(sec.get("service_role_key") or sec.get("service_key") or "").strip()
                if v:
                    return v
        except Exception:
            continue
    if config is not None:
        # allow passing config with url but key stored elsewhere — try reload
        try:
            cfg2 = load_telemetry_config()
            for path in (_LOCAL_CONFIG_PATH, _CONFIG_PATH):
                try:
                    data = json.loads(Path(path).read_text(encoding="utf-8") or "{}")
                    sec = data.get("telemetry") if isinstance(data, dict) else None
                    if isinstance(sec, dict):
                        v = str(sec.get("service_role_key") or "").strip()
                        if v:
                            return v
                except Exception:
                    continue
        except Exception:
            pass
    return ""


def fetch_reports_for_viewer(limit: int = 50, *, config: TelemetryConfig | None = None) -> dict[str, Any]:
    """Загрузить последние отчёты для страницы разработчика (без SQL).

    Использует service_role ключ, если он есть локально. Для anon чтения нет —
    вернёт подсказку открыть Dashboard в браузере.
    """
    cfg = config or load_telemetry_config()
    if not cfg.url:
        return {"ok": False, "error": "телеметрия не настроена (нет URL)", "rows": []}
    svc = _service_role_key(config=cfg)
    if not svc:
        return {"ok": False, "error": "нет service_role ключа — откройте в браузере (кнопка 🌐)", "rows": []}
    # Пробуем feedback first, потом app_logs
    url = f"{cfg.url}/rest/v1/feedback?select=*&order=created_at.desc&limit={max(1, int(limit))}"
    headers = {"apikey": svc, "Authorization": f"Bearer {svc}", "Accept": "application/json"}
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")  # type: ignore[arg-type]
        with urllib.request.urlopen(req, timeout=10) as resp:  # type: ignore[arg-type]
            body = resp.read().decode("utf-8")
            rows = json.loads(body) if body else []
            # подмешаем app_logs preview для тех же install_id? пока просто feedback
            return {"ok": True, "rows": rows if isinstance(rows, list) else [], "source": "feedback"}
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8")[:500]  # type: ignore[union-attr]
        except Exception:
            detail = str(exc)
        return {"ok": False, "error": f"Supabase HTTP {exc.code}: {detail[:200]}", "rows": []}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"сеть: {exc}", "rows": []}


# -- модульный синглтон для main.py -------------------------------------------

_client: TelemetryClient | None = None
_client_lock = threading.Lock()


def get_client(*, app_version: str = "") -> TelemetryClient:
    """Ленивый синглтон. ``app_version`` учитывается только при первом создании."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = TelemetryClient(app_version=app_version)
    return _client


def log(
    message: str,
    level: str = LEVEL_INFO,
    *,
    category: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Удобная обёртка: ``telemetry.log("…", telemetry.LEVEL_ERROR)``."""
    try:
        get_client().log(message, level, category=category, metadata=metadata)
    except Exception:  # noqa: BLE001 - никогда не роняем вызывающий код
        pass


def reset_client_for_tests() -> None:
    """Сброс синглтона (только для тестов)."""
    global _client
    with _client_lock:
        old, _client = _client, None
    if old is not None:
        old.close()
