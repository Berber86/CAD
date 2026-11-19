"""
app_updater.py
==============

Self-update for Title Survey AI: the user pastes a full GitHub link (repository,
branch/tree link, or a direct ``.zip`` archive link) that points at where the
current pushes land, the program downloads the matching ZIP snapshot and
forcibly unpacks it over the folder it is currently running from, overwriting
any file the archive also contains.

Files that only exist locally and are *not* part of the archive (typically
``config.local.json`` and ``.env`` with the user's own API keys) are left
untouched: the extractor only ever writes files present in the ZIP, it never
deletes anything.

``config.json`` is different: it ships with repository defaults *and* is where
the running program persists the user's live choices (selected AI provider and
models, consensus settings, this very update source URL, …). If the archive
were allowed to overwrite it, every update would silently reset those choices
back to the repo defaults. ``extract_update``/``apply_update`` therefore always
skip a small set of root-level settings/secrets files — see
``DEFAULT_PROTECTED_FILES`` — even though the archive contains them.

Network access, ZIP parsing and the local file replacement are split into
small functions so tests can monkeypatch the HTTP call without touching the
network.

``_http_get`` also works around a real failure seen on a Windows machine
where the OS certificate store rejected GitHub's TLS certificate
(``CERTIFICATE_VERIFY_FAILED: unable to get issuer certificate``) even though
the same link opened fine in a browser: on that specific failure it retries
once against the bundled ``certifi`` CA file before giving up.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime as _dt
import json
import re
import shutil
import ssl
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote, urlparse

try:  # pragma: no cover - certifi is an existing transitive dependency (openai/httpx)
    import certifi
except ImportError:  # pragma: no cover - degrade gracefully if it's ever absent
    certifi = None  # type: ignore[assignment]

USER_AGENT = "TitleSurveyAI-Updater/1.0"
_ALLOWED_HOSTS = ("github.com", "codeload.github.com", "githubusercontent.com", "api.github.com")

# Root-level settings/secrets that an update must never overwrite, even when the
# archive also contains a same-named file. ``config.json`` is tracked in the
# repository (it ships defaults), but on disk it also holds the user's live
# provider/model selections, consensus settings and this update source URL —
# replacing it would reset all of that on every update. ``config.local.json``
# and ``.env`` hold the user's own API keys and are git-ignored, so they would
# not normally be in the archive at all, but are listed here for safety in case
# someone's fork ever tracks them.
DEFAULT_PROTECTED_FILES = frozenset({"config.json", "config.local.json", ".env"})


class UpdateError(Exception):
    """Could not resolve, download or apply an update from the given source."""


class HttpMissing(UpdateError):
    """GitHub ответил 404: такой ветки или файла нет. Это отсутствие, а не поломка.

    Отдельный класс нужен поиску ветки: у части веток нет ``version.py`` или
    ``branch_state.json``, и это нормальный случай — ветка просто пропускается.
    Подкласс ``UpdateError``, поэтому прежние обработчики продолжают работать.
    """


class NothingToUpdate(UpdateError):
    """Найденная ветка не новее установленной версии: обновлять нечего."""


@dataclasses.dataclass
class UpdateResult:
    zip_url: str
    files_written: int
    app_dir: str
    protected_skipped: tuple[str, ...] = ()


def _is_certificate_verify_failure(reason: object) -> bool:
    """True when ``reason`` (``URLError.reason``) is an SSL verification failure."""
    if isinstance(reason, ssl.SSLCertVerificationError):
        return True
    return isinstance(reason, ssl.SSLError) and "CERTIFICATE_VERIFY_FAILED" in str(reason)


def _certifi_ssl_context() -> ssl.SSLContext | None:
    """A TLS context pinned to the bundled certifi CA file, if it's installed."""
    if certifi is None:
        return None
    return ssl.create_default_context(cafile=certifi.where())


_READ_CHUNK = 64 * 1024


def _read_body(response: Any, on_progress: Callable[[int, int], None] | None) -> bytes:
    """
    Читает тело ответа. Без колбэка — как раньше, одним ``read()``.

    С колбэком читает кусками и сообщает ``(получено, всего)``; «всего» берётся из
    ``Content-Length`` и равно 0, если сервер его не прислал.
    """
    if on_progress is None:
        return response.read()
    headers = getattr(response, "headers", None)
    try:
        total = int((headers.get("Content-Length") if headers is not None else 0) or 0)
    except (TypeError, ValueError):
        total = 0
    chunks: list[bytes] = []
    received = 0
    while True:
        chunk = response.read(_READ_CHUNK)
        if not chunk:
            break
        chunks.append(chunk)
        received += len(chunk)
        try:
            on_progress(received, total)
        except Exception:  # noqa: BLE001 - индикатор не должен обрывать скачивание
            pass
    return b"".join(chunks)


def _http_get(
    url: str, *, timeout: float = 30.0, on_progress: Callable[[int, int], None] | None = None,
) -> bytes:
    """
    Fetch raw bytes from ``url``. The only network primitive in this module.

    Tries the OS trust store first (``urlopen``'s own default context — on
    Windows this is the system certificate store). Some Windows machines fail
    every HTTPS request from Python with ``CERTIFICATE_VERIFY_FAILED: unable
    to get issuer certificate`` even though the same link opens fine in a
    browser — typically a stale system clock, antivirus HTTPS scanning, or a
    corporate proxy leaving the system store without the issuing certificate
    GitHub's CDN uses. When that happens, retry once against the bundled
    ``certifi`` CA file, which ships its own independently maintained root
    store. Only a certificate-verification failure triggers the retry; any
    other network error is reported immediately.
    """
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - GitHub only
            return _read_body(response, on_progress)
    except urllib.error.HTTPError as exc:
        raise _http_error(exc, url) from exc
    except urllib.error.URLError as exc:
        if not _is_certificate_verify_failure(exc.reason):
            raise UpdateError(f"Не удалось обратиться к {url}: {exc.reason}") from exc
        context = _certifi_ssl_context()
        if context is None:
            raise UpdateError(
                f"Не удалось проверить SSL-сертификат при обращении к {url}: {exc.reason}. "
                "Возможные причины: неверные дата/время на компьютере, антивирус с проверкой "
                "HTTPS-трафика или корпоративный прокси. Проверьте системные часы и, если "
                "используется антивирус с SSL-сканированием, на время обновления отключите его."
            ) from exc
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=context) as response:  # noqa: S310
                return _read_body(response, on_progress)
        except urllib.error.HTTPError as retry_exc:
            raise _http_error(retry_exc, url) from retry_exc
        except urllib.error.URLError as retry_exc:
            raise UpdateError(
                f"Не удалось проверить SSL-сертификат при обращении к {url} ни через системное "
                f"хранилище, ни через встроенный список корневых сертификатов: {retry_exc.reason}. "
                "Проверьте дату/время на компьютере и отключите на время обновления HTTPS-сканирование "
                "антивируса/прокси, затем повторите."
            ) from retry_exc


def _http_error(exc: urllib.error.HTTPError, url: str) -> UpdateError:
    """Текст ошибки HTTP: 404 — отсутствие, 403/429 — вероятный лимит запросов."""
    if exc.code == 404:
        return HttpMissing(f"GitHub ответил 404 (нет такого файла или ветки) для {url}")
    if exc.code in (403, 429):
        return UpdateError(
            f"GitHub ответил {exc.code} для {url}. Похоже на лимит запросов без авторизации "
            "(обычно 60 в час на IP-адрес) — повторите через несколько минут."
        )
    return UpdateError(f"GitHub ответил ошибкой {exc.code} для {url}")


def _default_branch(owner: str, repo: str, *, timeout: float = 30.0) -> str:
    """Ask the GitHub API for the repository's default branch."""
    raw = _http_get(f"https://api.github.com/repos/{owner}/{repo}", timeout=timeout)
    try:
        info = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - malformed/unexpected API response
        raise UpdateError(f"Не удалось разобрать ответ GitHub API для {owner}/{repo}: {exc}") from exc
    branch = str((info or {}).get("default_branch") or "").strip()
    if not branch:
        raise UpdateError(f"GitHub API не сообщил ветку по умолчанию для {owner}/{repo}.")
    return branch


@dataclasses.dataclass(frozen=True)
class GitHubSource:
    """Разобранная ссылка GitHub: репозиторий, ветка (если указана) и вид ссылки."""

    owner: str
    repo: str
    branch: str | None = None
    #: ``"zip"`` — прямая ссылка на архив, ``"branch"`` — ветка, ``"repo"`` — просто репозиторий.
    kind: str = "repo"
    #: Нормализованный исходный текст (нужен для прямых ``.zip``-ссылок).
    url: str = ""

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


def split_repo_slug(slug: str) -> tuple[str, str]:
    """``"Berber86/CAD"`` → ``("Berber86", "CAD")``; ``.git`` в конце отбрасывается."""
    text = str(slug or "").strip().strip("/")
    if text.lower().startswith(("http://", "https://", "git@", "ssh://")):
        raise UpdateError(f"Ожидался репозиторий вида owner/repo, а дано: {slug!r}")
    parts = [piece for piece in text.split("/") if piece]
    if len(parts) != 2:
        raise UpdateError(f"Ожидался репозиторий вида owner/repo, а дано: {slug!r}")
    owner, repo = parts
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    return owner, repo


def split_github_source(source: str) -> GitHubSource:
    """
    Разбирает то, что пользователь вставил в поле источника обновления.

    Понимает прямую ссылку на ``.zip`` (включая ``codeload.github.com``),
    ``.../tree/<ветка>``, ``.../archive/refs/heads/<ветка>``, голую ссылку на
    репозиторий и — вдобавок к прежним форматам — архив по ``codeload``.
    """
    text = str(source or "").strip()
    if not text:
        raise UpdateError("Укажите ссылку на GitHub, откуда скачивать обновление.")
    if not text.lower().startswith(("http://", "https://")):
        text = f"https://{text}"
    parsed = urlparse(text)
    host = parsed.netloc.lower()
    if not any(host == allowed or host.endswith(f".{allowed}") for allowed in _ALLOWED_HOSTS):
        raise UpdateError("Укажите ссылку на github.com: репозиторий, ветку или .zip архив.")
    path = parsed.path
    if text.lower().split("?", 1)[0].endswith(".zip") or host == "codeload.github.com":
        # Прямая ссылка на архив: ветку из неё (если получится) достанет вызывающий.
        parts = [unquote(piece) for piece in path.split("/") if piece]
        owner, repo = (parts[0], parts[1]) if len(parts) >= 2 else ("", "")
        return GitHubSource(owner=owner, repo=repo, branch=None, kind="zip", url=text)

    parts = [piece for piece in path.split("/") if piece]
    if len(parts) < 2:
        raise UpdateError("Ссылка должна указывать на репозиторий GitHub вида github.com/owner/repo.")
    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]

    branch: str | None = None
    if len(parts) >= 4 and parts[2] == "tree":
        branch = unquote("/".join(parts[3:]))
    elif len(parts) >= 5 and parts[2] == "archive" and parts[3] == "refs" and parts[4] == "heads":
        branch = unquote("/".join(parts[5:]))
    elif len(parts) >= 3 and parts[2] == "archive":
        # .../archive/<branch>.zip уже отсеян выше; всё остальное под /archive/ — имя ветки.
        branch = unquote("/".join(parts[3:]))

    branch = (branch or "").strip("/") or None
    return GitHubSource(owner=owner, repo=repo, branch=branch,
                        kind="branch" if branch else "repo", url=text)


def initial_update_source(settings: dict) -> str:
    """Один раз перевести старую ссылку на рабочую ветку CAD в автопоиск.

    Настроенные вручную другие репозитории и последующий явный выбор сохраняются.
    """
    section = settings.get("update") or {}
    text = str(section.get("source_url") or "").strip()
    if section.get("auto_source_migrated") or not text:
        return text
    try:
        parsed = split_github_source(text)
    except UpdateError:
        return text
    branch = parsed.branch or (branch_from_zip_url(parsed.url) if parsed.kind == "zip" else "")
    if parsed.slug.lower() == "berber86/cad" and branch and branch.startswith("arena/"):
        return ""
    return text


def codeload_zip_url(owner: str, repo: str, branch: str) -> str:
    """Ссылка на архив ветки (то, что реально скачивается)."""
    return f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{branch}"


def branch_from_zip_url(url: str) -> str | None:
    """Имя ветки из прямой ссылки на архив, если оно там есть (для проверки состояния)."""
    text = str(url or "")
    for marker in ("/zip/refs/heads/", "/archive/refs/heads/", "/zip/", "/archive/"):
        index = text.find(marker)
        if index < 0:
            continue
        tail = text[index + len(marker):]
        tail = tail.split("?", 1)[0].split("#", 1)[0]
        if tail.lower().endswith(".zip"):
            tail = tail[: -len(".zip")]
        tail = unquote(tail.strip("/"))
        if tail:
            return tail
    return None


def resolve_zip_url(source: str) -> str:
    """
    Turn a GitHub link the user pasted into a downloadable ZIP URL.

    Accepts, in order of preference:
      * a direct ``.zip`` link (used as-is);
      * ``https://github.com/<owner>/<repo>/tree/<branch>`` (and ``.../archive/refs/heads/<branch>``);
      * a bare repository link ``https://github.com/<owner>/<repo>`` — the
        default branch is resolved via the GitHub API.
    """
    parsed = split_github_source(source)
    if parsed.kind == "zip":
        return parsed.url
    branch = parsed.branch or _default_branch(parsed.owner, parsed.repo)
    return codeload_zip_url(parsed.owner, parsed.repo, branch)


# ---------------------------------------------------------------------------
# Канал обновлений: выбор ветки по версии и состояние ветки (стабильная/тестовая)
# ---------------------------------------------------------------------------
#
# Заказчик меняет агента раз в пару дней, а с ним — имя ветки (arena/<uuid>-cad),
# и каждый раз приходилось руками править ссылку в настройках. Поэтому источник
# больше не адрес, а правило: «ветка с наибольшей APP_VERSION, если она не
# помечена как тестовая» (вариант 2 из обсуждения 19.11.2026).
#
# Состояние ветки лежит в крошечном файле BRANCH_STATE_FILE в корне репозитория:
# {"state": "test"} — с ветки обновляться нельзя, пока в настройках не включена
# галочка «Я разработчик». Файла нет или в нём «stable» — ветка обычная.

#: Репозиторий по умолчанию: используется, если поле источника пустое.
DEFAULT_UPDATE_REPO = "Berber86/CAD"
#: Файл-указатель состояния ветки (в корне репозитория, свой в каждой ветке).
BRANCH_STATE_FILE = "branch_state.json"
#: Файл, из которого берётся версия ветки.
BRANCH_VERSION_FILE = "version.py"
STATE_STABLE = "stable"
STATE_TEST = "test"
STATE_UNKNOWN = "unknown"
#: Слова, которые считаются «ветка тестовая» (в любом регистре, включая русские).
TEST_STATE_WORDS = frozenset({
    "test", "testing", "tests", "dev", "develop", "development", "draft",
    "wip", "beta", "alpha", "internal", "тест", "тестовая", "тестовое",
    "черновик", "разработка", "рабочая",
})
#: Слова, которые считаются «ветка стабильная».
STABLE_STATE_WORDS = frozenset({
    "stable", "release", "released", "prod", "production", "public",
    "ok", "main", "master", "стабильная", "стабильно", "релиз", "боевая",
})
#: Сколько секунд результат поиска веток считается свежим (экономим лимит GitHub).
DISCOVERY_CACHE_TTL_SEC = 600.0
#: Сколько страниц веток читать максимум (по 100 на страницу).
MAX_BRANCH_PAGES = 3

_VERSION_RE = re.compile(r"""APP_VERSION\s*=\s*["\']([^"\']+)["\']""")
_VERSION_PART_RE = re.compile(r"\d+")

_DISCOVERY_CACHE: dict[tuple[str, str, bool], tuple[float, "DiscoveryResult"]] = {}


def parse_app_version(text: str) -> tuple[int, ...] | None:
    """
    Версия из текста ``version.py`` как кортеж чисел: ``"4.41"`` → ``(4, 41)``.

    Числовое сравнение обязательно: как строки ``"4.7" > "4.41"``, и поиск выбрал
    бы старую ветку. ``None`` — версии в тексте нет (ветка пропускается).
    """
    match = _VERSION_RE.search(str(text or ""))
    if not match:
        return None
    return version_key(match.group(1))


def version_key(value: str) -> tuple[int, ...]:
    """``"4.41"``/``"4.41.2"``/``"v4.41"`` → ``(4, 41)``/``(4, 41, 2)``/``(4, 41)``."""
    parts = tuple(int(piece) for piece in _VERSION_PART_RE.findall(str(value or "")))
    return parts or (0,)


def version_text(value: tuple[int, ...]) -> str:
    """Обратно в человекочитаемый вид: ``(4, 41)`` → ``"4.41"``."""
    return ".".join(str(part) for part in value) if value else ""


def read_branch_state(raw: Any) -> tuple[str, str]:
    """
    Состояние ветки из содержимого ``branch_state.json`` → ``(state, note)``.

    Файл крошечный и правится руками, поэтому понятны разные формы записи:
    ``{"state": "test"}``, ``{"test": true}``, ``{"channel": "dev"}``,
    ``"test"`` (голая строка). Отсутствующий файл считается стабильным отдельно; неверный маркер —
    неизвестным, чтобы случайно не пропустить тестовую ветку.
    """
    note = ""
    data: Any = raw
    if isinstance(raw, (bytes, bytearray)):
        try:
            data = raw.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - байты не декодируются
            return STATE_STABLE, ""
    if isinstance(data, str):
        text = data.strip()
        if not text:
            return STATE_UNKNOWN, "пустой файл состояния"
        try:
            data = json.loads(text)
        except Exception:  # noqa: BLE001 - битый JSON блокирует обновление
            return STATE_UNKNOWN, f"не разобрать {BRANCH_STATE_FILE}: {text[:80]}"
    if isinstance(data, list) and data:
        data = data[0]

    if isinstance(data, str):
        word = data.strip().lower()
        if word in TEST_STATE_WORDS:
            return STATE_TEST, ""
        if word in STABLE_STATE_WORDS:
            return STATE_STABLE, ""
        return STATE_UNKNOWN, f"непонятное состояние {data.strip()[:40]!r}"

    if not isinstance(data, dict):
        return STATE_UNKNOWN, "неверный формат состояния"

    note = str(data.get("note") or data.get("comment") or data.get("причина") or "").strip()
    for key in ("test", "is_test", "testing", "developer_only", "тест", "тестовая"):
        if key in data:
            flag = data.get(key)
            if isinstance(flag, str):
                flag = flag.strip().lower() in {"1", "true", "yes", "on", "да"}
            return (STATE_TEST if flag else STATE_STABLE), note
    for key in ("state", "channel", "status", "состояние", "канал"):
        if key in data and data.get(key) is not None:
            word = str(data.get(key)).strip().lower()
            if word in TEST_STATE_WORDS:
                return STATE_TEST, note
            if word in STABLE_STATE_WORDS:
                return STATE_STABLE, note
            return STATE_UNKNOWN, note or f"непонятное состояние {word[:40]!r}"
    return STATE_UNKNOWN, note or "состояние не указано"


@dataclasses.dataclass
class BranchCandidate:
    """Ветка-кандидат на обновление: версия и состояние (стабильная/тестовая)."""

    name: str
    sha: str = ""
    version_text: str = ""
    version: tuple[int, ...] = ()
    state: str = STATE_UNKNOWN
    note: str = ""
    commit_date: str = ""

    @property
    def is_test(self) -> bool:
        return self.state == STATE_TEST

    def label(self) -> str:
        """«arena/4054f7eb-cad · 4.41 · стабильная» — для статуса и журнала."""
        state_word = {
            STATE_TEST: "тестовая",
            STATE_STABLE: "стабильная",
        }.get(self.state, "состояние неизвестно")
        version = self.version_text or "версия неизвестна"
        return f"{self.name} · {version} · {state_word}"


@dataclasses.dataclass
class DiscoveryResult:
    """Итог поиска ветки: что выбрали, что пропустили и как это объяснить человеку."""

    owner: str
    repo: str
    developer_mode: bool = False
    installed_version: tuple[int, ...] = ()
    chosen: BranchCandidate | None = None
    #: Все ветки с прочитанной версией, по убыванию версии.
    considered: list[BranchCandidate] = dataclasses.field(default_factory=list)
    #: Тестовые ветки новее выбранной — они и есть причина отказа.
    blocked_test: list[BranchCandidate] = dataclasses.field(default_factory=list)
    requests: int = 0
    message: str = ""

    @property
    def repo_slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def is_newer(self) -> bool:
        """Выбранная ветка новее установленной версии (или установленная неизвестна)."""
        if self.chosen is None:
            return False
        if not self.installed_version:
            return True
        return self.chosen.version > self.installed_version


def _github_json(url: str, *, timeout: float = 30.0) -> Any:
    """JSON из GitHub API (единственная точка входа в сеть для поиска веток)."""
    raw = _http_get(url, timeout=timeout)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - ответ не JSON
        raise UpdateError(f"Не удалось разобрать ответ GitHub API для {url}: {exc}") from exc


def _github_json_or_none(url: str, *, timeout: float = 30.0) -> Any | None:
    """То же, но 404 (нет файла/ветки) даёт ``None`` вместо ошибки."""
    try:
        return _github_json(url, timeout=timeout)
    except HttpMissing:
        return None


def _contents_url(owner: str, repo: str, branch: str, path: str) -> str:
    ref = quote(str(branch), safe="")
    return f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={ref}"


def _decode_contents(payload: Any) -> str | None:
    """Текст файла из ответа ``/contents`` (GitHub отдаёт его в base64)."""
    if not isinstance(payload, dict):
        return None
    if payload.get("type") not in (None, "file"):
        return None
    content = payload.get("content")
    if content is None:
        return None
    try:
        return base64.b64decode(str(content)).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - повреждённый base64
        return None


def list_branches(owner: str, repo: str, *, timeout: float = 30.0) -> list[BranchCandidate]:
    """Все ветки репозитория (имя + sha последнего коммита)."""
    found: list[BranchCandidate] = []
    for page in range(1, MAX_BRANCH_PAGES + 1):
        url = f"https://api.github.com/repos/{owner}/{repo}/branches?per_page=100&page={page}"
        payload = _github_json(url, timeout=timeout)
        if not isinstance(payload, list) or not payload:
            break
        for item in payload:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            commit = item.get("commit") or {}
            sha = str(commit.get("sha") or "") if isinstance(commit, dict) else ""
            found.append(BranchCandidate(name=name, sha=sha))
        if len(payload) < 100:
            break
    return found


def fetch_branch_version(
    owner: str, repo: str, branch: str, *, timeout: float = 30.0
) -> tuple[tuple[int, ...] | None, str]:
    """Версия ветки из её ``version.py`` → ``((4, 41), "4.41")``; ``(None, "")`` если её нет."""
    payload = _github_json_or_none(_contents_url(owner, repo, branch, BRANCH_VERSION_FILE),
                                   timeout=timeout)
    text = _decode_contents(payload)
    if text is None:
        return None, ""
    match = _VERSION_RE.search(text)
    if not match:
        return None, ""
    raw = match.group(1)
    return version_key(raw), raw


def fetch_branch_state(
    owner: str, repo: str, branch: str, *, timeout: float = 30.0
) -> tuple[str, str]:
    """Состояние ветки из её ``branch_state.json``; нет файла — значит стабильная."""
    payload = _github_json_or_none(_contents_url(owner, repo, branch, BRANCH_STATE_FILE),
                                   timeout=timeout)
    text = _decode_contents(payload)
    if payload is None:
        return STATE_STABLE, f"{BRANCH_STATE_FILE} в ветке нет — считаю стабильной"
    if text is None:
        raise UpdateError(f"Не удалось прочитать {BRANCH_STATE_FILE} в ветке {branch}.")
    state, note = read_branch_state(text)
    if state == STATE_UNKNOWN:
        raise UpdateError(f"Некорректный {BRANCH_STATE_FILE} в ветке {branch}: {note}")
    return state, note


def fetch_commit_date(owner: str, repo: str, sha: str, *, timeout: float = 30.0) -> str:
    """Дата коммита (для выбора между ветками с одинаковой версией); "" если не узнать."""
    if not sha:
        return ""
    payload = _github_json_or_none(f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}",
                                   timeout=timeout)
    try:
        return str(((payload or {}).get("commit") or {}).get("committer", {}).get("date") or "")
    except Exception:  # noqa: BLE001 - дата не критична
        return ""


def clear_discovery_cache() -> None:
    """Сбрасывает кэш поиска веток (кнопка «Найти ветку» делает это сама)."""
    _DISCOVERY_CACHE.clear()


def describe_discovery(result: DiscoveryResult) -> str:
    """Человеческий итог поиска — в статус окна и в журнал."""
    installed = version_text(result.installed_version)
    if result.chosen is None:
        if result.blocked_test:
            newest = result.blocked_test[0]
            why = f" ({newest.note})" if newest.note else ""
            dev = ("Галочка «Я разработчик» включена, но стабильных веток не нашлось вовсе."
                   if result.developer_mode else
                   "Обновиться с неё можно, только включив галочку «Я разработчик» в настройках.")
            return (
                f"Обновлений нет: самая свежая ветка {newest.label()}{why} — тестовая. {dev}"
            )
        return f"В {result.repo_slug} не нашлось ни одной ветки с {BRANCH_VERSION_FILE}."
    if not result.is_newer:
        text = (
            f"Обновление не нужно: самая свежая подходящая ветка {result.chosen.label()}"
            + (f", а установлена {installed}" if installed else "")
            + "."
        )
    else:
        text = f"Найдена ветка {result.chosen.label()}"
        if installed:
            text += f", установлено {installed}"
        text += "."
    if result.blocked_test and not result.developer_mode:
        names = ", ".join(
            f"{item.name} ({item.version_text or '?'})" for item in result.blocked_test[:3]
        )
        text += (
            f" Пропущены тестовые ветки: {names} — с них обновляются только "
            "с галочкой «Я разработчик»."
        )
    return text


def discover_branch(
    owner: str,
    repo: str,
    *,
    developer_mode: bool = False,
    installed_version: tuple[int, ...] | str | None = None,
    timeout: float = 30.0,
    use_cache: bool = True,
    progress: Callable[[str], None] | None = None,
) -> DiscoveryResult:
    """
    Находит ветку для обновления: наибольшая ``APP_VERSION``, состояние — не «тест».

    Порядок: список веток → ``version.py`` каждой (кто без версии — пропускается) →
    сортировка по версии (при равенстве — по дате коммита) → ``branch_state.json``
    у кандидатов сверху вниз, пока не найдётся разрешённая. Состояния читаются
    лениво, поэтому обычно хватает одного лишнего запроса.

    ``developer_mode=True`` разрешает тестовые ветки (галочка «Я разработчик»).
    Результат кэшируется на ``DISCOVERY_CACHE_TTL_SEC``, чтобы повторные нажатия
    не тратили лимит запросов GitHub (без токена — 60 в час).
    """

    def report(message: str) -> None:
        if progress is not None:
            try:
                progress(message)
            except Exception:  # noqa: BLE001 - индикатор не должен ронять поиск
                pass

    if isinstance(installed_version, str):
        installed = version_key(installed_version) if installed_version.strip() else ()
    elif installed_version:
        installed = tuple(int(part) for part in installed_version)
    else:
        installed = ()

    key = (owner.lower(), repo.lower(), bool(developer_mode))
    if use_cache:
        cached = _DISCOVERY_CACHE.get(key)
        if cached is not None and (time.monotonic() - cached[0]) < DISCOVERY_CACHE_TTL_SEC:
            stored = cached[1]
            fresh = dataclasses.replace(
                stored,
                installed_version=installed,
                considered=list(stored.considered),
                blocked_test=list(stored.blocked_test),
            )
            fresh.message = describe_discovery(fresh)
            report("список веток взят из кэша (не старше 10 минут)")
            return fresh

    requests = 0
    report(f"читаю список веток {owner}/{repo}…")
    branches = list_branches(owner, repo, timeout=timeout)
    requests += 1
    if not branches:
        raise UpdateError(f"В {owner}/{repo} нет ни одной ветки (или репозиторий недоступен).")

    candidates: list[BranchCandidate] = []
    for index, branch in enumerate(branches, start=1):
        report(f"читаю версии веток: {index} из {len(branches)}…")
        try:
            version, version_raw = fetch_branch_version(owner, repo, branch.name, timeout=timeout)
            requests += 1
        except UpdateError as exc:
            raise UpdateError(f"Не удалось проверить версию ветки {branch.name}: {exc}") from exc
        if version is None:
            continue
        branch.version = version
        branch.version_text = version_raw
        candidates.append(branch)

    if not candidates:
        result = DiscoveryResult(owner=owner, repo=repo, developer_mode=developer_mode,
                                installed_version=installed, requests=requests)
        result.message = describe_discovery(result)
        return result

    # Сортировка по убыванию версии; при равенстве — свежий коммит выше.
    dated = [item for item in candidates if _ties_exist(candidates, item)]
    for item in dated:
        item.commit_date = fetch_commit_date(owner, repo, item.sha, timeout=timeout)
        requests += 1
    candidates.sort(key=lambda item: (item.version, item.commit_date), reverse=True)

    chosen: BranchCandidate | None = None
    blocked: list[BranchCandidate] = []
    for item in candidates:
        report(f"проверяю состояние ветки {item.name}…")
        state, note = fetch_branch_state(owner, repo, item.name, timeout=timeout)
        requests += 1
        item.state = state
        item.note = note
        if state == STATE_TEST:
            blocked.append(item)
            if developer_mode:
                chosen = item  # разработчику можно и тестовую — она всё равно свежайшая
                break
            continue
        chosen = item
        break

    if chosen is None and developer_mode and candidates:
        # Галочка включена, но все прочитанные состояния оказались тестовыми:
        # берём свежайшую — разработчик именно это и просил.
        chosen = blocked[0] if blocked else candidates[0]

    result = DiscoveryResult(
        owner=owner,
        repo=repo,
        developer_mode=developer_mode,
        installed_version=installed,
        chosen=chosen,
        considered=candidates,
        blocked_test=blocked,
        requests=requests,
    )
    result.message = describe_discovery(result)
    if use_cache:
        _DISCOVERY_CACHE[key] = (time.monotonic(), result)
    report(result.message)
    return result


def _ties_exist(candidates: list[BranchCandidate], item: BranchCandidate) -> bool:
    """Есть ли другая ветка с той же версией (тогда нужна дата коммита для выбора)."""
    return any(other is not item and other.version == item.version for other in candidates)


@dataclasses.dataclass(frozen=True)
class ResolvedSource:
    """Откуда качать обновление: ссылка на архив и всё, что о ветке известно."""

    zip_url: str
    owner: str
    repo: str
    branch: str
    state: str = STATE_UNKNOWN
    note: str = ""
    description: str = ""
    discovery: DiscoveryResult | None = None

    @property
    def repo_slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def is_test(self) -> bool:
        return self.state == STATE_TEST


def resolve_update_source(
    source: str,
    *,
    developer_mode: bool = False,
    installed_version: str = "",
    repo: str | None = None,
    timeout: float = 30.0,
    use_cache: bool = True,
    progress: Callable[[str], None] | None = None,
) -> ResolvedSource:
    """
    Превращает поле источника в ссылку на архив, попутно проверяя состояние ветки.

    * поле пустое → поиск ветки по наибольшей версии (``discover_branch``);
    * голое имя ветки (``arena/4054f7eb-cad``, ``main``) → ветка репозитория по
      умолчанию (``repo`` либо ``DEFAULT_UPDATE_REPO``);
    * ссылка на репозиторий/ветку/.zip → как раньше, но с проверкой состояния.

    Тестовая ветка без ``developer_mode`` не отдаётся: поднимается ``UpdateError``
    с объяснением про галочку «Я разработчик». Если подходящая ветка не новее
    установленной версии — ``NothingToUpdate``.
    """
    default_owner, default_repo = split_repo_slug(repo or DEFAULT_UPDATE_REPO)
    text = str(source or "").strip()

    if not text:
        discovery = discover_branch(
            default_owner, default_repo,
            developer_mode=developer_mode,
            installed_version=installed_version,
            timeout=timeout,
            use_cache=use_cache,
            progress=progress,
        )
        if discovery.chosen is None:
            raise UpdateError(discovery.message or "Не удалось найти ветку для обновления.")
        if not discovery.is_newer:
            raise NothingToUpdate(discovery.message)
        chosen = discovery.chosen
        return ResolvedSource(
            zip_url=codeload_zip_url(default_owner, default_repo, chosen.name),
            owner=default_owner,
            repo=default_repo,
            branch=chosen.name,
            state=chosen.state,
            note=chosen.note,
            description=discovery.message,
            discovery=discovery,
        )

    lowered = text.lower()
    if "://" not in lowered and "." not in text.split("/", 1)[0]:
        # Голое имя ветки: «arena/4054f7eb-cad» или «main» — репозиторий по умолчанию.
        branch = text.strip("/")
        return _gate_branch(default_owner, default_repo, branch, developer_mode=developer_mode,
                            timeout=timeout, progress=progress)

    parsed = split_github_source(text)
    if parsed.kind == "zip":
        branch = branch_from_zip_url(parsed.url)
        if not branch:
            # Прямая ссылка на архив без имени ветки: проверить состояние негде.
            return ResolvedSource(
                zip_url=parsed.url, owner=parsed.owner, repo=parsed.repo, branch="",
                state=STATE_UNKNOWN,
                description="Прямая ссылка на архив: состояние ветки не проверялось.",
            )
        owner, repo_name = (parsed.owner, parsed.repo) if parsed.owner else (default_owner, default_repo)
        return _gate_branch(owner, repo_name, branch, developer_mode=developer_mode,
                            timeout=timeout, progress=progress, zip_url=parsed.url)

    branch = parsed.branch or _default_branch(parsed.owner, parsed.repo)
    return _gate_branch(parsed.owner, parsed.repo, branch, developer_mode=developer_mode,
                        timeout=timeout, progress=progress)


def _gate_branch(
    owner: str,
    repo: str,
    branch: str,
    *,
    developer_mode: bool,
    timeout: float = 30.0,
    progress: Callable[[str], None] | None = None,
    zip_url: str | None = None,
) -> ResolvedSource:
    """Проверяет состояние конкретной ветки и не пускает на тестовую без галочки."""
    if progress is not None:
        try:
            progress(f"проверяю состояние ветки {branch}…")
        except Exception:  # noqa: BLE001
            pass
    state, note = fetch_branch_state(owner, repo, branch, timeout=timeout)
    if state == STATE_TEST and not developer_mode:
        why = f" Причина в файле: {note}" if note else ""
        raise UpdateError(
            f"Ветка {branch} в {owner}/{repo} помечена как тестовая.{why} Обновление с неё "
            "доступно только с включённой галочкой «Я разработчик» в настройках программы."
        )
    return ResolvedSource(
        zip_url=zip_url or codeload_zip_url(owner, repo, branch),
        owner=owner,
        repo=repo,
        branch=branch,
        state=state,
        note=note,
        description=f"Ветка {branch}: версия не сравнивалась (указана вручную). "
                    f"Состояние — {note or ('тестовая' if state == STATE_TEST else 'стабильная')}.",
    )


def local_branch_state(app_dir: str | Path) -> tuple[str, str, str]:
    """
    Состояние и версия, с которыми программа установлена сейчас → ``(state, note, version)``.

    Читает локальные ``branch_state.json`` и ``version.py``: после обновления они
    приходят из архива, поэтому по ним видно, на каком канале работает установка.
    """
    folder = Path(app_dir)
    state, note = STATE_UNKNOWN, ""
    version = ""
    try:
        state_file = folder / BRANCH_STATE_FILE
        if state_file.is_file():
            state, note = read_branch_state(state_file.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        state, note = STATE_UNKNOWN, "файл состояния не читается"
    try:
        version_file = folder / BRANCH_VERSION_FILE
        if version_file.is_file():
            match = _VERSION_RE.search(version_file.read_text(encoding="utf-8", errors="replace"))
            version = match.group(1) if match else ""
    except OSError:
        version = ""
    return state, note, version


def download_zip(
    url: str, destination: Path, *, timeout: float = 60.0,
    on_progress: Callable[[int, int], None] | None = None,
) -> Path:
    """Download ``url`` to ``destination`` and return the path."""
    if on_progress is None:
        data = _http_get(url, timeout=timeout)
    else:
        data = _http_get(url, timeout=timeout, on_progress=on_progress)
    if not data:
        raise UpdateError("GitHub вернул пустой архив.")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return destination


def _common_root(names: list[str]) -> str:
    """GitHub archives wrap everything in a single ``repo-branch/`` folder."""
    roots = {name.split("/", 1)[0] for name in names if "/" in name}
    if len(roots) == 1:
        return next(iter(roots)) + "/"
    return ""


def _is_protected_file(relative: str, protected: frozenset[str]) -> bool:
    """
    True for root-level settings/secrets an update must leave alone.

    Only applies at the root of the installation: ``config.json``,
    ``config.local.json`` and ``.env`` are all resolved next to ``main.py`` via
    ``Path(__file__).with_name(...)``, never inside a subfolder, so a file with
    one of these names deeper in the archive (if that ever happened) is not
    protected by this check.
    """
    if "/" in relative:
        return False
    if relative in protected:
        return True
    # Mirrors the project's own "<name>.local.json" convention for machine-local
    # overrides that must never come from a shared update archive.
    return relative.endswith(".local.json")


def _archive_entries(archive: zipfile.ZipFile, protected: frozenset[str]) -> tuple[list[tuple[str, zipfile.ZipInfo]], list[str]]:
    """
    Файлы архива, которые будут записаны: ``[(путь относительно папки, элемент ZIP)]``,
    и защищённые пути, которые архив содержит, но мы их не трогаем.
    """
    names = archive.namelist()
    prefix = _common_root(names)
    entries: list[tuple[str, zipfile.ZipInfo]] = []
    skipped: list[str] = []
    for info in archive.infolist():
        name = info.filename
        if prefix:
            if not name.startswith(prefix):
                continue
            relative = name[len(prefix):]
        else:
            relative = name
        if not relative or relative.endswith("/"):
            continue
        if _is_protected_file(relative, protected):
            skipped.append(relative)
            continue
        entries.append((relative, info))
    return entries, skipped


def _check_archive_channel(zip_path: Path, *, developer_mode: bool) -> None:
    """Проверить маркер в реально скачанном архиве до резервирования и записи файлов."""
    try:
        with zipfile.ZipFile(zip_path) as archive:
            entries, _ = _archive_entries(archive, frozenset())
            markers = [info for relative, info in entries if relative == BRANCH_STATE_FILE]
            if len(markers) > 1:
                raise UpdateError("В архиве несколько файлов состояния ветки.")
            if not markers:  # Старые версии не содержали указателя.
                return
            if markers[0].file_size > 65536:
                raise UpdateError("Файл состояния ветки в архиве слишком велик.")
            state, note = read_branch_state(archive.read(markers[0]))
    except zipfile.BadZipFile as exc:
        raise UpdateError(f"Скачанный файл не является ZIP-архивом: {exc}") from exc
    if state == STATE_UNKNOWN:
        raise UpdateError(f"Не удалось проверить состояние ветки в архиве: {note}")
    if state == STATE_TEST and not developer_mode:
        raise UpdateError("Архив помечен как тестовый. Обновление доступно только с галочкой «Я разработчик».")


def _archive_targets(zip_path: Path, *, protected: frozenset[str] = DEFAULT_PROTECTED_FILES) -> list[str]:
    """Пути, которые обновление собирается записать (без защищённых). Ошибки — как у распаковки."""
    try:
        with zipfile.ZipFile(zip_path) as archive:
            entries, _ = _archive_entries(archive, protected)
    except zipfile.BadZipFile as exc:
        raise UpdateError(f"Скачанный файл не является ZIP-архивом: {exc}") from exc
    if not entries:
        raise UpdateError("В архиве не найдено ни одного файла для обновления.")
    return [relative for relative, _ in entries]


def extract_update(
    zip_path: Path,
    target_dir: Path,
    *,
    protected: frozenset[str] = DEFAULT_PROTECTED_FILES,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[list[str], list[str]]:
    """
    Forcibly overwrite files under ``target_dir`` with the archive's contents.

    Only files present in the archive are written; anything else already on
    disk (local config, user data, caches) is left alone. Files matching
    ``protected`` (see ``DEFAULT_PROTECTED_FILES``) are skipped even when the
    archive contains them, so the user's live settings/secrets on disk survive
    an update untouched.

    Returns ``(written, skipped)``: paths (relative to ``target_dir``) that
    were written, and protected paths the archive contained but left alone.
    """
    written: list[str] = []
    skipped: list[str] = []
    target_dir = Path(target_dir).resolve()
    try:
        with zipfile.ZipFile(zip_path) as archive:
            entries, skipped = _archive_entries(archive, protected)
            total_entries = len(entries)
            for index, (relative, info) in enumerate(entries, 1):
                dest_path = (target_dir / relative).resolve()
                if not dest_path.is_relative_to(target_dir):
                    raise UpdateError(f"Архив содержит недопустимый путь: {info.filename}")
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, open(dest_path, "wb") as handle:
                    shutil.copyfileobj(source, handle)
                written.append(relative)
                if on_progress is not None:
                    on_progress(index, total_entries)
    except zipfile.BadZipFile as exc:
        raise UpdateError(f"Скачанный файл не является ZIP-архивом: {exc}") from exc
    if not written:
        raise UpdateError("В архиве не найдено ни одного файла для обновления.")
    return written, skipped


# ----------------------------------------------------------------- резервные копии

#: Папка с резервными копиями — внутри папки программы, в git не попадает.
BACKUP_DIR_NAME = "backups"
#: Сколько последних резервов хранить; откат идёт с самого свежего.
BACKUPS_KEEP = 3
_MANIFEST = "manifest.json"


@dataclasses.dataclass(frozen=True)
class BackupInfo:
    """Резервная копия перед обновлением: что в ней и какая версия была установлена."""

    path: str
    created: str
    version: str
    files: tuple[str, ...]  # существовавшие файлы: вернём их содержимое
    new_files: tuple[str, ...]  # появились при обновлении: при откате удалим


@dataclasses.dataclass(frozen=True)
class RollbackResult:
    version: str
    files_restored: int
    files_removed: int


def _safe_join(root: Path, relative: str) -> Path:
    dest = (root / relative).resolve()
    if not dest.is_relative_to(root):
        raise UpdateError(f"недопустимый путь в резервной копии: {relative}")
    return dest


def _installed_version(app_dir: Path) -> str:
    """Версия из version.py текущей установки (пусто, если не найдена)."""
    try:
        text = (app_dir / "version.py").read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r"APP_VERSION\s*=\s*[\"']([^\"']+)[\"']", text)
    return match.group(1) if match else ""


def backups_root(app_dir: str | Path) -> Path:
    return Path(app_dir).resolve() / BACKUP_DIR_NAME


def list_backups(app_dir: str | Path) -> list[BackupInfo]:
    """Резервные копии, самая свежая первой. Повреждённые пропускаются."""
    root = backups_root(app_dir)
    if not root.is_dir():
        return []
    found: list[BackupInfo] = []
    for folder in sorted(root.iterdir(), reverse=True):
        manifest = folder / _MANIFEST
        if not (folder.is_dir() and manifest.is_file()):
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            found.append(BackupInfo(
                path=str(folder),
                created=str(data.get("created", "")),
                version=str(data.get("version", "")),
                files=tuple(data.get("files", [])),
                new_files=tuple(data.get("new_files", [])),
            ))
        except (OSError, ValueError, TypeError):
            continue
    return found


def create_backup(
    app_dir: str | Path, relatives: list[str],
    *, on_progress: Callable[[int, int], None] | None = None,
) -> BackupInfo:
    """Копирует файлы, которые обновление перезапишет, и запоминает, каких не было."""
    app_dir = Path(app_dir).resolve()
    root = backups_root(app_dir)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = root / stamp
    counter = 1
    while folder.exists():
        counter += 1
        folder = root / f"{stamp}-{counter}"
    folder.mkdir(parents=True)
    files_dir = folder / "files"
    files: list[str] = []
    new_files: list[str] = []
    try:
        for index, relative in enumerate(relatives, 1):
            source = _safe_join(app_dir, relative)
            if source.is_file():
                target = files_dir / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                files.append(relative)
            elif not source.exists():
                new_files.append(relative)
            else:
                raise UpdateError(f"на месте файла «{relative}» лежит папка — резерв невозможен")
            if on_progress is not None:
                on_progress(index, len(relatives))
        info = BackupInfo(
            path=str(folder),
            created=_dt.datetime.now().isoformat(timespec="seconds"),
            version=_installed_version(app_dir),
            files=tuple(files),
            new_files=tuple(new_files),
        )
        (folder / _MANIFEST).write_text(
            json.dumps({
                "created": info.created, "version": info.version,
                "files": list(info.files), "new_files": list(info.new_files),
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    _prune_backups(app_dir, keep=BACKUPS_KEEP)
    return info


def _prune_backups(app_dir: Path, *, keep: int) -> None:
    for old in list_backups(app_dir)[keep:]:
        shutil.rmtree(old.path, ignore_errors=True)


def restore_backup(app_dir: str | Path, backup: BackupInfo) -> RollbackResult:
    """
    Возвращает файлы из резерва и удаляет новые файлы обновления.

    Сначала проверяется, что все файлы резерва на месте, и только потом что-либо
    меняется. Защищённые файлы (настройки и ключи) не трогаются никогда.
    Резерв удаляется только после полного успеха.
    """
    app_dir = Path(app_dir).resolve()
    files_dir = Path(backup.path) / "files"
    to_restore = [rel for rel in backup.files if not _is_protected_file(rel, DEFAULT_PROTECTED_FILES)]
    to_remove = [rel for rel in backup.new_files if not _is_protected_file(rel, DEFAULT_PROTECTED_FILES)]
    for relative in to_restore:
        if not (files_dir / relative).is_file():
            raise UpdateError(f"в резервной копии нет файла «{relative}» — откат невозможен")
    restored = 0
    for relative in to_restore:
        dest = _safe_join(app_dir, relative)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(files_dir / relative, dest)
        restored += 1
    removed = 0
    for relative in to_remove:
        dest = _safe_join(app_dir, relative)
        if dest.is_file():
            dest.unlink()
            removed += 1
    shutil.rmtree(backup.path, ignore_errors=True)
    return RollbackResult(version=backup.version, files_restored=restored, files_removed=removed)


def rollback_latest(app_dir: str | Path) -> RollbackResult:
    """Откатывает программу на самую свежую резервную копию."""
    backups = list_backups(app_dir)
    if not backups:
        raise UpdateError("резервной копии нет — откатывать некуда")
    return restore_backup(app_dir, backups[0])


def _progress_kwargs(on_stage: Any, callback: Callable[[int, int], None]) -> dict[str, Any]:
    """Колбэк счётчика передаётся только когда индикатор действительно подключён."""
    return {"on_progress": callback} if on_stage is not None else {}


def _megabytes(value: int) -> str:
    return f"{value / (1024 * 1024):.1f}"


def apply_update(
    source: str,
    app_dir: str | Path,
    *,
    progress: Callable[[str], None] | None = None,
    timeout: float = 60.0,
    protected: frozenset[str] = DEFAULT_PROTECTED_FILES,
    on_stage: Callable[[str, float | None, str], None] | None = None,
    developer_mode: bool = False,
) -> UpdateResult:
    """
    Resolve ``source``, download its ZIP and forcibly unpack it into ``app_dir``.

    ``on_stage(stage, fraction, detail)`` получает числовой ход работы: ``stage`` —
    ``"download"``, ``"backup"`` или ``"install"``; ``fraction`` — от 0 до 1 или ``None``,
    если размер заранее неизвестен; ``detail`` — текст для человека.
    """

    def report(message: str) -> None:
        if progress is not None:
            try:
                progress(message)
            except Exception:  # noqa: BLE001 - progress must never abort the update
                pass

    def stage(name: str, fraction: float | None, detail: str) -> None:
        if on_stage is not None:
            try:
                on_stage(name, fraction, detail)
            except Exception:  # noqa: BLE001 - индикатор не должен прерывать обновление
                pass

    def on_download(received: int, total: int) -> None:
        if total > 0:
            stage("download", min(1.0, received / total),
                  f"{_megabytes(received)} из {_megabytes(total)} МБ")
        else:
            stage("download", None, f"получено {_megabytes(received)} МБ")

    def on_backup(done: int, total: int) -> None:
        stage("backup", done / total if total else 1.0, f"файлов: {done} из {total}")

    def on_install(done: int, total: int) -> None:
        stage("install", done / total if total else 1.0, f"файлов: {done} из {total}")

    zip_url = resolve_zip_url(source)
    report(f"скачиваю архив обновления: {zip_url}")
    app_dir = Path(app_dir)
    with tempfile.TemporaryDirectory(prefix="tsai_update_") as tmp_name:
        zip_path = Path(tmp_name) / "update.zip"
        stage("download", 0.0, "подключаюсь к GitHub…")
        download_zip(
            zip_url, zip_path, timeout=timeout,
            on_progress=on_download if on_stage is not None else None,
        )
        _check_archive_channel(zip_path, developer_mode=developer_mode)
        targets = _archive_targets(zip_path, protected=protected)
        # Старый архив без маркера — стабильная ветка. Удаляем оставшийся от
        # предыдущей установки маркер, иначе интерфейс показал бы неверный канал.
        # Добавляем файл в резерв, чтобы откат восстановил прежний маркер.
        old_marker = app_dir / BRANCH_STATE_FILE
        remove_old_marker = (BRANCH_STATE_FILE not in targets and old_marker.is_file()
                             and not _is_protected_file(BRANCH_STATE_FILE, protected))
        if remove_old_marker:
            targets.append(BRANCH_STATE_FILE)
        report("сохраняю резервную копию текущей версии…")
        backup = create_backup(app_dir, targets, **_progress_kwargs(on_stage, on_backup))
        report("распаковываю архив и принудительно заменяю файлы…")
        stage("install", 0.0, "распаковываю архив…")
        try:
            written, skipped = extract_update(
                zip_path, app_dir, protected=protected, **_progress_kwargs(on_stage, on_install),
            )
            if remove_old_marker:
                old_marker.unlink()
        except BaseException as exc:
            # Распаковка оборвалась на полпути: возвращаем всё как было до обновления.
            try:
                restore_backup(app_dir, backup)
            except Exception as restore_exc:  # noqa: BLE001
                raise UpdateError(
                    f"обновление не удалось ({exc}), и автоматический возврат не выполнен "
                    f"({restore_exc}). Резервная копия: {backup.path}"
                ) from exc
            raise
    if skipped:
        report(
            "локальные настройки сохранены без изменений: "
            + ", ".join(sorted(skipped))
        )
    report(f"обновление завершено: заменено файлов — {len(written)}")
    stage("install", 1.0, f"готово: заменено файлов — {len(written)}")
    return UpdateResult(
        zip_url=zip_url,
        files_written=len(written),
        app_dir=str(app_dir),
        protected_skipped=tuple(skipped),
    )
