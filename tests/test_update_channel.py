"""
Канал обновлений: поиск ветки по версии и запрет тестовых веток (4.42).

Заказчик меняет агента раз в пару дней, а с ним — имя ветки (`arena/<uuid>-cad`),
и каждый раз приходилось руками править ссылку в настройках. Теперь источник —
правило: «ветка с наибольшей APP_VERSION, если она не помечена как тестовая».
Состояние ветки лежит в крошечном ``branch_state.json`` в корне репозитория;
тестовая ветка доступна только с галочкой «Я разработчик».

Сети здесь нет: ``_http_get`` подменён синтетическим GitHub.
"""

from __future__ import annotations

import base64
import json
import urllib.error
from pathlib import Path

import pytest

import app_updater as up

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Синтетический GitHub
# --------------------------------------------------------------------------- #

class FakeGitHub:
    """Отвечает на запросы API так, как это делал бы GitHub для нашего репозитория."""

    def __init__(self, owner: str = "Berber86", repo: str = "CAD", branches: dict | None = None):
        self.owner = owner
        self.repo = repo
        #: name → {"version": "4.41", "state": {...}|None|"missing", "date": "2026-...", "sha": "..."}
        self.branches: dict[str, dict] = branches or {}
        self.calls: list[str] = []
        self.default_branch = "main"

    # -- helpers ----------------------------------------------------------
    def add(self, name: str, *, version: str | None = "4.41", state=None, date: str = "", sha: str = ""):
        # sha без косых черт: настоящий GitHub отдаёт 40 символов, а адрес
        # .../commits/<sha> читается по последнему сегменту пути.
        self.branches[name] = {
            "version": version,
            "state": state,
            "date": date or f"2026-11-{10 + len(self.branches):02d}T12:00:00Z",
            "sha": sha or f"{len(self.branches):040d}",
        }
        return self

    @property
    def request_count(self) -> int:
        return len(self.calls)

    # -- сеть -------------------------------------------------------------
    def __call__(self, url: str, *, timeout: float = 30.0, on_progress=None) -> bytes:
        self.calls.append(url)
        base = f"https://api.github.com/repos/{self.owner}/{self.repo}"
        if url.split("?", 1)[0] == base:
            return json.dumps({"default_branch": self.default_branch}).encode("utf-8")
        if url.startswith(f"{base}/branches"):
            if "page=2" in url:
                return b"[]"
            return json.dumps(
                [{"name": name, "commit": {"sha": data["sha"]}} for name, data in self.branches.items()]
            ).encode("utf-8")
        if url.startswith(f"{base}/contents/"):
            path, _, rest = url[len(base) + len("/contents/"):].partition("?")
            branch = ""
            if rest.startswith("ref="):
                from urllib.parse import unquote

                branch = unquote(rest[len("ref="):])
            data = self.branches.get(branch)
            if data is None:
                raise up.HttpMissing(f"404 {url}")
            if path == up.BRANCH_VERSION_FILE:
                if not data["version"]:
                    raise up.HttpMissing(f"404 {url}")
                text = f'APP_VERSION = "{data["version"]}"\nAPP_DATE = "2026-11-19"\n'
            elif path == up.BRANCH_STATE_FILE:
                if data["state"] in (None, "missing"):
                    raise up.HttpMissing(f"404 {url}")
                text = data["state"] if isinstance(data["state"], str) else json.dumps(data["state"])
            else:
                raise up.HttpMissing(f"404 {url}")
            payload = {"type": "file", "name": path,
                       "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
                       "encoding": "base64"}
            return json.dumps(payload).encode("utf-8")
        if url.startswith(f"{base}/commits/"):
            sha = url.rsplit("/", 1)[-1]
            for data in self.branches.values():
                if data["sha"] == sha:
                    return json.dumps({"commit": {"committer": {"date": data["date"]}}}).encode("utf-8")
            raise up.HttpMissing(f"404 {url}")
        raise AssertionError(f"неожиданный запрос к GitHub: {url}")


@pytest.fixture()
def github(monkeypatch):
    def install(fake: FakeGitHub) -> FakeGitHub:
        monkeypatch.setattr(up, "_http_get", fake)
        up.clear_discovery_cache()
        return fake

    yield install
    up.clear_discovery_cache()


def _repo(*args, **kwargs) -> FakeGitHub:
    fake = FakeGitHub(*args, **kwargs)
    # Типовая картина репозитория: main чуть отстаёт, рабочие ветки arena/* свежее.
    fake.add("main", version="4.36", date="2026-11-19T11:45:54Z")
    fake.add("arena/4054f7eb-cad", version="4.41", date="2026-11-19T15:31:27Z")
    fake.add("arena/3c6f7f95-cad", version="4.38", date="2026-11-19T09:13:34Z")
    return fake


# --------------------------------------------------------------------------- #
# Версия и состояние ветки
# --------------------------------------------------------------------------- #

def test_version_is_compared_as_numbers_not_as_text():
    """Без этого «4.7» оказалась бы новее «4.41» — строковое сравнение врёт."""
    assert up.parse_app_version('APP_VERSION = "4.41"') == (4, 41)
    assert up.parse_app_version('APP_VERSION = "4.7"') == (4, 7)
    assert up.version_key("4.7") < up.version_key("4.41")
    assert up.version_key("v4.41.2") == (4, 41, 2)
    assert up.parse_app_version("APP_DATE = '2026-11-19'") is None
    assert up.parse_app_version("") is None
    assert up.version_text((4, 41)) == "4.41"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"state": "test"}', up.STATE_TEST),
        ('{"state": "TEST"}', up.STATE_TEST),
        ('{"state": "stable"}', up.STATE_STABLE),
        ('{"test": true}', up.STATE_TEST),
        ('{"test": false}', up.STATE_STABLE),
        ('{"is_test": "yes"}', up.STATE_TEST),
        ('{"channel": "dev"}', up.STATE_TEST),
        ('{"channel": "release"}', up.STATE_STABLE),
        ('{"state": "черновик"}', up.STATE_TEST),
        ('{"state": "боевая"}', up.STATE_STABLE),
        ('"test"', up.STATE_TEST),
        ('{"state": "test", "note": "тяжёлый модуль"}', up.STATE_TEST),
        # Некорректный маркер нельзя принимать за стабильную ветку.
        ('{"state": "какое-то"}', up.STATE_UNKNOWN),
        ("", up.STATE_UNKNOWN),
        ("{ не json", up.STATE_UNKNOWN),
        ("{}", up.STATE_UNKNOWN),
    ],
)
def test_read_branch_state_understands_handy_forms(raw, expected):
    state, _note = up.read_branch_state(raw)
    assert state == expected


def test_read_branch_state_returns_the_note():
    state, note = up.read_branch_state('{"state": "test", "note": "переписываю Шаг 5"}')
    assert state == up.STATE_TEST
    assert note == "переписываю Шаг 5"


def test_shipped_branch_state_file_is_tiny_and_readable():
    """Файл-указатель в репозитории: крошечный, валидный, по умолчанию стабильный."""
    path = ROOT / up.BRANCH_STATE_FILE
    assert path.is_file(), "в репозитории нет branch_state.json"
    data = path.read_bytes()
    assert len(data) < 500, "файл состояния должен оставаться крошечным"
    state, _note = up.read_branch_state(data.decode("utf-8"))
    assert state in (up.STATE_STABLE, up.STATE_TEST)


# --------------------------------------------------------------------------- #
# Поиск ветки
# --------------------------------------------------------------------------- #

def test_discovery_picks_the_branch_with_the_highest_version(github):
    fake = github(_repo())
    found = up.discover_branch(fake.owner, fake.repo, installed_version="4.36")
    assert found.chosen is not None
    assert found.chosen.name == "arena/4054f7eb-cad"
    assert found.chosen.version_text == "4.41"
    assert found.is_newer is True
    assert "4.41" in found.message


def test_discovery_skips_branches_without_version_file(github):
    fake = github(_repo())
    fake.add("arena/ancient-cad", version=None)          # version.py ещё не было
    fake.add("arena/no-version-cad", version=None)
    found = up.discover_branch(fake.owner, fake.repo)
    names = [item.name for item in found.considered]
    assert "arena/ancient-cad" not in names
    assert found.chosen is not None and found.chosen.name == "arena/4054f7eb-cad"


def test_discovery_blocks_a_newer_test_branch_for_an_ordinary_user(github):
    """Главное требование: с тестовой ветки обычный пользователь не обновляется."""
    fake = github(_repo())
    fake.add("arena/heavy-module-cad", version="4.60",
             state={"state": "test", "note": "переписываю Шаг 5"})
    found = up.discover_branch(fake.owner, fake.repo, developer_mode=False,
                               installed_version="4.36")
    assert found.chosen is not None
    assert found.chosen.name == "arena/4054f7eb-cad", "тестовая ветка не должна выбираться"
    assert [item.name for item in found.blocked_test] == ["arena/heavy-module-cad"]
    assert found.is_newer is True
    assert "тестов" in found.message
    assert "Я разработчик" in found.message
    assert "4.60" not in found.message.split("Пропущены")[0], "тестовая версия не должна манить"


def test_discovery_allows_a_test_branch_in_developer_mode(github):
    fake = github(_repo())
    fake.add("arena/heavy-module-cad", version="4.60", state={"state": "test"})
    found = up.discover_branch(fake.owner, fake.repo, developer_mode=True,
                               installed_version="4.41")
    assert found.chosen is not None
    assert found.chosen.name == "arena/heavy-module-cad"
    assert found.chosen.is_test is True
    assert found.is_newer is True


def test_branch_without_state_file_counts_as_stable(github):
    """Файла состояния нет — ветка обычная (иначе все старые ветки стали бы запрещены)."""
    fake = github(_repo())
    found = up.discover_branch(fake.owner, fake.repo, installed_version="4.36")
    assert found.chosen is not None and found.chosen.state == up.STATE_STABLE
    assert "branch_state.json" in found.chosen.note


def test_discovery_breaks_version_ties_by_commit_date(github):
    fake = github(_repo())
    fake.add("arena/older-push-cad", version="4.50", date="2026-11-10T10:00:00Z")
    fake.add("arena/newer-push-cad", version="4.50", date="2026-11-19T18:00:00Z")
    found = up.discover_branch(fake.owner, fake.repo, installed_version="4.36")
    assert found.chosen is not None
    assert found.chosen.name == "arena/newer-push-cad"


def test_discovery_reports_when_nothing_is_newer(github):
    fake = github(_repo())
    found = up.discover_branch(fake.owner, fake.repo, installed_version="4.41")
    assert found.chosen is not None
    assert found.is_newer is False
    assert "не нужно" in found.message.lower()


def test_discovery_says_when_every_branch_is_test(github):
    fake = FakeGitHub()
    fake.add("arena/one-cad", version="4.50", state={"state": "test"})
    fake.add("arena/two-cad", version="4.49", state='{"test": true}')
    github(fake)
    found = up.discover_branch(fake.owner, fake.repo, developer_mode=False,
                               installed_version="4.41")
    assert found.chosen is None
    assert len(found.blocked_test) == 2
    assert "Я разработчик" in found.message


def test_discovery_uses_cache_on_the_second_call(github):
    """Лимит GitHub без токена — 60 запросов в час, поэтому результат кэшируется."""
    fake = github(_repo())
    first = up.discover_branch(fake.owner, fake.repo, installed_version="4.36")
    calls_after_first = fake.request_count
    second = up.discover_branch(fake.owner, fake.repo, installed_version="4.36")
    assert fake.request_count == calls_after_first, "кэш не сработал — снова пошли запросы"
    assert second.chosen is not None and second.chosen.name == first.chosen.name
    up.clear_discovery_cache()
    up.discover_branch(fake.owner, fake.repo, installed_version="4.36")
    assert fake.request_count > calls_after_first, "после сброса кэша запросы должны пойти снова"


def test_discovery_survives_rate_limit_message(github, monkeypatch):
    fake = github(_repo())

    def limited(url, *, timeout=30.0, on_progress=None):
        if "/branches" in url:
            raise up.UpdateError(
                f"GitHub ответил 403 для {url}. Похоже на лимит запросов без авторизации "
                "(60 в час на репозиторий) — повторите через несколько минут."
            )
        return fake(url, timeout=timeout)

    monkeypatch.setattr(up, "_http_get", limited)
    with pytest.raises(up.UpdateError, match="лимит запросов"):
        up.discover_branch(fake.owner, fake.repo)


def test_http_error_explains_404_and_403_differently():
    def error(code: int) -> urllib.error.HTTPError:
        return urllib.error.HTTPError("https://api.github.com/x", code, "err", None, None)

    assert isinstance(up._http_error(error(404), "https://api.github.com/x"), up.HttpMissing)
    forbidden = up._http_error(error(403), "https://api.github.com/x")
    assert isinstance(forbidden, up.UpdateError) and not isinstance(forbidden, up.HttpMissing)
    assert "лимит запросов" in str(forbidden)


# --------------------------------------------------------------------------- #
# Резолвер источника (то, что зовёт окно)
# --------------------------------------------------------------------------- #

def test_empty_source_resolves_through_discovery(github):
    """Пустое поле — не ошибка, а «найдите сами»: именно это убирает ручную правку ссылки."""
    fake = github(_repo())
    resolved = up.resolve_update_source("", installed_version="4.36")
    assert resolved.branch == "arena/4054f7eb-cad"
    assert resolved.zip_url == (
        "https://codeload.github.com/Berber86/CAD/zip/refs/heads/arena/4054f7eb-cad"
    )
    assert resolved.state == up.STATE_STABLE
    assert resolved.discovery is not None


def test_empty_source_refuses_a_test_branch_without_the_developer_flag(github):
    fake = FakeGitHub()
    fake.add("main", version="4.36")
    fake.add("arena/heavy-cad", version="4.70", state={"state": "test", "note": "тяжёлый модуль"})
    github(fake)
    resolved = up.resolve_update_source("", installed_version="4.20", developer_mode=False)
    assert resolved.branch == "main", "тестовая ветка не должна отдаваться обычному пользователю"
    assert resolved.state == up.STATE_STABLE


def test_explicit_test_branch_is_blocked_without_the_flag(github):
    fake = github(_repo())
    fake.branches["arena/4054f7eb-cad"]["state"] = {"state": "test", "note": "идёт тяжёлая работа"}
    source = "https://github.com/Berber86/CAD/tree/arena/4054f7eb-cad"
    with pytest.raises(up.UpdateError) as info:
        up.resolve_update_source(source, developer_mode=False)
    text = str(info.value)
    assert "тестовая" in text
    assert "Я разработчик" in text
    assert "идёт тяжёлая работа" in text


def test_explicit_test_branch_is_allowed_with_the_flag(github):
    fake = github(_repo())
    fake.branches["arena/4054f7eb-cad"]["state"] = {"state": "test"}
    source = "https://github.com/Berber86/CAD/tree/arena/4054f7eb-cad"
    resolved = up.resolve_update_source(source, developer_mode=True)
    assert resolved.branch == "arena/4054f7eb-cad"
    assert resolved.is_test is True
    assert resolved.zip_url.endswith("refs/heads/arena/4054f7eb-cad")


def test_explicit_stable_branch_passes_the_gate(github):
    fake = github(_repo())
    fake.branches["arena/4054f7eb-cad"]["state"] = {"state": "stable", "note": "релизная"}
    resolved = up.resolve_update_source(
        "https://github.com/Berber86/CAD/tree/arena/4054f7eb-cad", developer_mode=False
    )
    assert resolved.state == up.STATE_STABLE


def test_bare_branch_name_is_accepted(github):
    """В поле можно вписать одно имя ветки — репозиторий возьмётся по умолчанию."""
    fake = github(_repo())
    resolved = up.resolve_update_source("arena/4054f7eb-cad", developer_mode=False)
    assert resolved.owner == "Berber86" and resolved.repo == "CAD"
    assert resolved.branch == "arena/4054f7eb-cad"


def test_bare_repo_link_uses_the_default_branch_and_checks_it(github):
    fake = github(_repo())
    fake.default_branch = "main"
    fake.branches["main"]["state"] = {"state": "test"}
    with pytest.raises(up.UpdateError, match="тестовая"):
        up.resolve_update_source("https://github.com/Berber86/CAD")


def test_direct_zip_link_is_passed_through(github):
    github(_repo())
    url = "https://github.com/Berber86/CAD/archive/refs/heads/main.zip"
    resolved = up.resolve_update_source(url)
    assert resolved.zip_url == url
    assert resolved.branch == "main"


def test_direct_zip_link_without_a_branch_is_not_guessed(github):
    github(_repo())
    url = "https://example.invalid/build.zip"
    # Чужой хост по-прежнему запрещён.
    with pytest.raises(up.UpdateError, match="github.com"):
        up.resolve_update_source(url)


def test_resolve_zip_url_passes_codeload_links_through():
    """Уже разобранную ссылку на архив нельзя резолвить заново (иначе ветка потеряется)."""
    url = "https://codeload.github.com/Berber86/CAD/zip/refs/heads/arena/4054f7eb-cad"
    assert up.resolve_zip_url(url) == url
    assert up.branch_from_zip_url(url) == "arena/4054f7eb-cad"


def test_nothing_to_update_is_a_separate_error(github):
    github(_repo())
    with pytest.raises(up.NothingToUpdate):
        up.resolve_update_source("", installed_version="4.41")


def test_repo_can_be_overridden_by_settings(github):
    fake = FakeGitHub(owner="someone", repo="fork")
    fake.add("main", version="9.9")
    github(fake)
    resolved = up.resolve_update_source("", repo="someone/fork", installed_version="1.0")
    assert resolved.repo_slug == "someone/fork"
    assert resolved.branch == "main"


def test_split_repo_slug_rejects_garbage():
    assert up.split_repo_slug("Berber86/CAD") == ("Berber86", "CAD")
    assert up.split_repo_slug("Berber86/CAD.git") == ("Berber86", "CAD")
    with pytest.raises(up.UpdateError):
        up.split_repo_slug("https://github.com/Berber86/CAD")
    with pytest.raises(up.UpdateError):
        up.split_repo_slug("CAD")


# --------------------------------------------------------------------------- #
# Локальный канал установки
# --------------------------------------------------------------------------- #

def test_local_branch_state_reads_installed_files(tmp_path):
    (tmp_path / up.BRANCH_STATE_FILE).write_text(
        '{"state": "test", "note": "тяжёлый модуль"}', encoding="utf-8"
    )
    (tmp_path / up.BRANCH_VERSION_FILE).write_text('APP_VERSION = "4.60"\n', encoding="utf-8")
    state, note, version = up.local_branch_state(tmp_path)
    assert state == up.STATE_TEST
    assert note == "тяжёлый модуль"
    assert version == "4.60"


def test_local_branch_state_without_files_is_unknown(tmp_path):
    state, _note, version = up.local_branch_state(tmp_path)
    assert state == up.STATE_UNKNOWN
    assert version == ""


def test_local_branch_state_survives_a_broken_file(tmp_path):
    (tmp_path / up.BRANCH_STATE_FILE).write_text("{ не json", encoding="utf-8")
    state, note, _version = up.local_branch_state(tmp_path)
    assert state == up.STATE_UNKNOWN
    assert note


def test_state_file_survives_an_update_because_it_is_not_protected():
    """
    Файл состояния намеренно НЕ в списке защищённых: он должен приезжать из архива,
    чтобы показывать канал той ветки, из которой программа поставлена.
    """
    assert up.BRANCH_STATE_FILE not in up.DEFAULT_PROTECTED_FILES
    assert "config.json" in up.DEFAULT_PROTECTED_FILES


def test_old_arena_link_migrates_to_auto_once():
    old = "https://github.com/Berber86/CAD/tree/arena/old-cad"
    assert up.initial_update_source({"update": {"source_url": old}}) == ""
    assert up.initial_update_source({"update": {"source_url": old, "auto_source_migrated": True}}) == old
    other = "https://github.com/other/repo/tree/arena/old-cad"
    assert up.initial_update_source({"update": {"source_url": other}}) == other
    assert up.initial_update_source({"update": {"source_url": "https://github.com/Berber86/CAD/tree/main"}}).endswith("/main")


def test_state_lookup_error_never_assumed_stable(github, monkeypatch):
    fake = github(_repo())
    def failing(url, **kwargs):
        if up.BRANCH_STATE_FILE in url:
            raise up.UpdateError("лимит запросов")
        return fake(url, **kwargs)
    monkeypatch.setattr(up, "_http_get", failing)
    with pytest.raises(up.UpdateError, match="лимит запросов"):
        up.discover_branch(fake.owner, fake.repo)
    with pytest.raises(up.UpdateError, match="лимит запросов"):
        up.resolve_update_source("arena/4054f7eb-cad")


def test_unknown_marker_is_not_treated_as_stable(github):
    fake = github(_repo())
    fake.branches["arena/4054f7eb-cad"]["state"] = '{ broken'
    with pytest.raises(up.UpdateError, match="Некорректный"):
        up.resolve_update_source("arena/4054f7eb-cad")


def test_zip_url_checks_actual_repository_not_default(github):
    fake = FakeGitHub(owner="other", repo="fork")
    fake.add("main", state={"state": "test"})
    github(fake)
    with pytest.raises(up.UpdateError, match="тестовая"):
        up.resolve_update_source("https://github.com/other/fork/archive/refs/heads/main.zip")
    assert any("repos/other/fork/contents/branch_state.json" in url for url in fake.calls)


def test_archive_test_marker_checked_before_any_files_written(tmp_path, monkeypatch):
    import zipfile
    app = tmp_path / "app"
    app.mkdir()
    (app / "main.py").write_text("old", encoding="utf-8")
    def download(url, destination, **kwargs):
        with zipfile.ZipFile(destination, "w") as z:
            z.writestr("CAD-main/main.py", "new")
            z.writestr("CAD-main/branch_state.json", '{"state":"test"}')
        return destination
    monkeypatch.setattr(up, "download_zip", download)
    url = "https://codeload.github.com/Berber86/CAD/zip/refs/heads/main"
    with pytest.raises(up.UpdateError, match="Я разработчик"):
        up.apply_update(url, app)
    assert (app / "main.py").read_text() == "old"
    assert not (app / up.BACKUP_DIR_NAME).exists()
    up.apply_update(url, app, developer_mode=True)
    assert (app / "main.py").read_text() == "new"


def test_archive_corrupt_marker_fails_closed(tmp_path):
    import zipfile
    path = tmp_path / "update.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("CAD-main/main.py", "new")
        z.writestr("CAD-main/branch_state.json", "broken")
    with pytest.raises(up.UpdateError, match="состояние"):
        up._check_archive_channel(path, developer_mode=False)


def test_legacy_archive_clears_old_marker_and_rollback_restores_it(tmp_path, monkeypatch):
    import zipfile
    app = tmp_path / "app"
    app.mkdir()
    (app / "main.py").write_text("old", encoding="utf-8")
    marker = app / up.BRANCH_STATE_FILE
    marker.write_text('{"state":"test"}', encoding="utf-8")
    def download(url, destination, **kwargs):
        with zipfile.ZipFile(destination, "w") as z:
            z.writestr("CAD-main/main.py", "new")
        return destination
    monkeypatch.setattr(up, "download_zip", download)
    up.apply_update("https://github.com/Berber86/CAD/archive/main.zip", app)
    assert not marker.exists()
    up.rollback_latest(app)
    assert up.read_branch_state(marker.read_text(encoding="utf-8"))[0] == up.STATE_TEST
    assert (app / "main.py").read_text() == "old"
