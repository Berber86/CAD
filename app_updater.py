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

Network access, ZIP parsing and the local file replacement are split into
small functions so tests can monkeypatch the HTTP call without touching the
network.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

USER_AGENT = "TitleSurveyAI-Updater/1.0"
_ALLOWED_HOSTS = ("github.com", "codeload.github.com", "githubusercontent.com", "api.github.com")


class UpdateError(Exception):
    """Could not resolve, download or apply an update from the given source."""


@dataclasses.dataclass
class UpdateResult:
    zip_url: str
    files_written: int
    app_dir: str


def _http_get(url: str, *, timeout: float = 30.0) -> bytes:
    """Fetch raw bytes from ``url``. The only network primitive in this module."""
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - GitHub only
            return response.read()
    except urllib.error.HTTPError as exc:
        raise UpdateError(f"GitHub ответил ошибкой {exc.code} для {url}") from exc
    except urllib.error.URLError as exc:
        raise UpdateError(f"Не удалось обратиться к {url}: {exc.reason}") from exc


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


def resolve_zip_url(source: str) -> str:
    """
    Turn a GitHub link the user pasted into a downloadable ZIP URL.

    Accepts, in order of preference:
      * a direct ``.zip`` link (used as-is);
      * ``https://github.com/<owner>/<repo>/tree/<branch>`` (and ``.../archive/refs/heads/<branch>``);
      * a bare repository link ``https://github.com/<owner>/<repo>`` — the
        default branch is resolved via the GitHub API.
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
    if text.lower().split("?", 1)[0].endswith(".zip"):
        return text

    parts = [piece for piece in parsed.path.split("/") if piece]
    if len(parts) < 2:
        raise UpdateError("Ссылка должна указывать на репозиторий GitHub вида github.com/owner/repo.")
    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]

    branch: str | None = None
    if len(parts) >= 4 and parts[2] == "tree":
        branch = "/".join(parts[3:])
    elif len(parts) >= 5 and parts[2] == "archive" and parts[3] == "refs" and parts[4] == "heads":
        branch = "/".join(parts[5:])
    elif len(parts) >= 3 and parts[2] == "archive":
        # .../archive/<branch>.zip already short-circuited above; anything else
        # under /archive/ without refs/heads is treated as a bare branch name.
        branch = "/".join(parts[3:])

    branch = (branch or "").strip("/")
    if not branch:
        branch = _default_branch(owner, repo)
    return f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{branch}"


def download_zip(url: str, destination: Path, *, timeout: float = 60.0) -> Path:
    """Download ``url`` to ``destination`` and return the path."""
    data = _http_get(url, timeout=timeout)
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


def extract_update(zip_path: Path, target_dir: Path) -> list[str]:
    """
    Forcibly overwrite files under ``target_dir`` with the archive's contents.

    Only files present in the archive are written; anything else already on
    disk (local config, user data, caches) is left alone. Returns the list of
    paths (relative to ``target_dir``) that were written.
    """
    written: list[str] = []
    target_dir = Path(target_dir).resolve()
    try:
        with zipfile.ZipFile(zip_path) as archive:
            names = archive.namelist()
            prefix = _common_root(names)
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
                dest_path = (target_dir / relative).resolve()
                if not dest_path.is_relative_to(target_dir):
                    raise UpdateError(f"Архив содержит недопустимый путь: {name}")
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, open(dest_path, "wb") as handle:
                    shutil.copyfileobj(source, handle)
                written.append(relative)
    except zipfile.BadZipFile as exc:
        raise UpdateError(f"Скачанный файл не является ZIP-архивом: {exc}") from exc
    if not written:
        raise UpdateError("В архиве не найдено ни одного файла для обновления.")
    return written


def apply_update(
    source: str,
    app_dir: str | Path,
    *,
    progress: Callable[[str], None] | None = None,
    timeout: float = 60.0,
) -> UpdateResult:
    """Resolve ``source``, download its ZIP and forcibly unpack it into ``app_dir``."""

    def report(message: str) -> None:
        if progress is not None:
            try:
                progress(message)
            except Exception:  # noqa: BLE001 - progress must never abort the update
                pass

    zip_url = resolve_zip_url(source)
    report(f"скачиваю архив обновления: {zip_url}")
    app_dir = Path(app_dir)
    with tempfile.TemporaryDirectory(prefix="tsai_update_") as tmp_name:
        zip_path = Path(tmp_name) / "update.zip"
        download_zip(zip_url, zip_path, timeout=timeout)
        report("распаковываю архив и принудительно заменяю файлы…")
        written = extract_update(zip_path, app_dir)
    report(f"обновление завершено: заменено файлов — {len(written)}")
    return UpdateResult(zip_url=zip_url, files_written=len(written), app_dir=str(app_dir))
