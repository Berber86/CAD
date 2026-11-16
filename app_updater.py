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

import dataclasses
import datetime as _dt
import json
import re
import shutil
import ssl
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

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


def _http_get(url: str, *, timeout: float = 30.0) -> bytes:
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
            return response.read()
    except urllib.error.HTTPError as exc:
        raise UpdateError(f"GitHub ответил ошибкой {exc.code} для {url}") from exc
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
                return response.read()
        except urllib.error.HTTPError as retry_exc:
            raise UpdateError(f"GitHub ответил ошибкой {retry_exc.code} для {url}") from retry_exc
        except urllib.error.URLError as retry_exc:
            raise UpdateError(
                f"Не удалось проверить SSL-сертификат при обращении к {url} ни через системное "
                f"хранилище, ни через встроенный список корневых сертификатов: {retry_exc.reason}. "
                "Проверьте дату/время на компьютере и отключите на время обновления HTTPS-сканирование "
                "антивируса/прокси, затем повторите."
            ) from retry_exc


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
            for relative, info in entries:
                dest_path = (target_dir / relative).resolve()
                if not dest_path.is_relative_to(target_dir):
                    raise UpdateError(f"Архив содержит недопустимый путь: {info.filename}")
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, open(dest_path, "wb") as handle:
                    shutil.copyfileobj(source, handle)
                written.append(relative)
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


def create_backup(app_dir: str | Path, relatives: list[str]) -> BackupInfo:
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
        for relative in relatives:
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


def apply_update(
    source: str,
    app_dir: str | Path,
    *,
    progress: Callable[[str], None] | None = None,
    timeout: float = 60.0,
    protected: frozenset[str] = DEFAULT_PROTECTED_FILES,
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
        targets = _archive_targets(zip_path, protected=protected)
        report("сохраняю резервную копию текущей версии…")
        backup = create_backup(app_dir, targets)
        report("распаковываю архив и принудительно заменяю файлы…")
        try:
            written, skipped = extract_update(zip_path, app_dir, protected=protected)
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
    return UpdateResult(
        zip_url=zip_url,
        files_written=len(written),
        app_dir=str(app_dir),
        protected_skipped=tuple(skipped),
    )
