"""Tests for app_updater: URL resolution, ZIP extraction and the update flow.

Network access is never exercised here: ``_http_get`` is monkeypatched, and the
ZIP fixtures are built in-memory with the standard library's ``zipfile``.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

import app_updater


def _zip_bytes(files: dict[str, bytes], *, root: str | None = "CAD-main") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            full_name = f"{root}/{name}" if root else name
            archive.writestr(full_name, data)
    return buffer.getvalue()


def test_resolve_zip_url_passes_direct_zip_links_through():
    url = "https://github.com/Berber86/CAD/archive/refs/heads/main.zip"
    assert app_updater.resolve_zip_url(url) == url


def test_resolve_zip_url_converts_tree_link_to_codeload():
    url = app_updater.resolve_zip_url("https://github.com/Berber86/CAD/tree/arena/53e1068f-cad")
    assert url == "https://codeload.github.com/Berber86/CAD/zip/refs/heads/arena/53e1068f-cad"


def test_resolve_zip_url_looks_up_default_branch_for_bare_repo_link(monkeypatch):
    monkeypatch.setattr(
        app_updater, "_http_get",
        lambda url, timeout=30.0: json.dumps({"default_branch": "main"}).encode("utf-8"),
    )
    url = app_updater.resolve_zip_url("https://github.com/Berber86/CAD")
    assert url == "https://codeload.github.com/Berber86/CAD/zip/refs/heads/main"


def test_resolve_zip_url_rejects_non_github_hosts():
    with pytest.raises(app_updater.UpdateError, match="github.com"):
        app_updater.resolve_zip_url("https://example.com/owner/repo")


def test_resolve_zip_url_rejects_empty_source():
    with pytest.raises(app_updater.UpdateError):
        app_updater.resolve_zip_url("   ")


def test_resolve_zip_url_rejects_repo_only_path_without_owner():
    with pytest.raises(app_updater.UpdateError, match="owner/repo"):
        app_updater.resolve_zip_url("https://github.com/Berber86")


def test_download_zip_writes_bytes_from_http_get(tmp_path, monkeypatch):
    monkeypatch.setattr(app_updater, "_http_get", lambda url, timeout=30.0: b"zip-bytes")
    destination = tmp_path / "nested" / "update.zip"
    result = app_updater.download_zip("https://codeload.github.com/owner/repo/zip/refs/heads/main", destination)
    assert result == destination
    assert destination.read_bytes() == b"zip-bytes"


def test_download_zip_rejects_an_empty_response(tmp_path, monkeypatch):
    monkeypatch.setattr(app_updater, "_http_get", lambda url, timeout=30.0: b"")
    with pytest.raises(app_updater.UpdateError, match="пустой архив"):
        app_updater.download_zip("https://codeload.github.com/owner/repo/zip/refs/heads/main", tmp_path / "u.zip")


def test_extract_update_overwrites_existing_files_and_adds_new_ones(tmp_path):
    target = tmp_path / "app"
    target.mkdir()
    (target / "main.py").write_text("old main", encoding="utf-8")
    (target / "config.local.json").write_text('{"secret": "keep-me"}', encoding="utf-8")

    archive_path = tmp_path / "update.zip"
    archive_path.write_bytes(_zip_bytes({
        "main.py": b"new main",
        "version.py": b"APP_VERSION = '9.9'",
        "sub/dir/module.py": b"print('hi')",
    }))

    written, skipped = app_updater.extract_update(archive_path, target)

    assert sorted(written) == ["main.py", "sub/dir/module.py", "version.py"]
    assert skipped == []
    assert (target / "main.py").read_text(encoding="utf-8") == "new main"
    assert (target / "version.py").read_text(encoding="utf-8") == "APP_VERSION = '9.9'"
    assert (target / "sub" / "dir" / "module.py").read_text(encoding="utf-8") == "print('hi')"
    # Local, non-archived files are never touched or deleted.
    assert (target / "config.local.json").read_text(encoding="utf-8") == '{"secret": "keep-me"}'


def test_extract_update_works_without_a_common_root_folder(tmp_path):
    target = tmp_path / "app"
    target.mkdir()
    archive_path = tmp_path / "update.zip"
    archive_path.write_bytes(_zip_bytes({"main.py": b"flat main"}, root=None))

    written, skipped = app_updater.extract_update(archive_path, target)

    assert written == ["main.py"]
    assert skipped == []
    assert (target / "main.py").read_text(encoding="utf-8") == "flat main"


def test_extract_update_never_overwrites_config_json_even_though_the_repo_tracks_it(tmp_path):
    """config.json ships repo defaults but also stores the user's live provider/model
    choices and this very update source URL; the update must leave it alone."""
    target = tmp_path / "app"
    target.mkdir()
    (target / "config.json").write_text('{"api": {"provider": "openrouter"}}', encoding="utf-8")

    archive_path = tmp_path / "update.zip"
    archive_path.write_bytes(_zip_bytes({
        "main.py": b"new main",
        "config.json": b'{"api": {"provider": "hydra"}}',
    }))

    written, skipped = app_updater.extract_update(archive_path, target)

    assert written == ["main.py"]
    assert skipped == ["config.json"]
    assert (target / "config.json").read_text(encoding="utf-8") == '{"api": {"provider": "openrouter"}}'


def test_extract_update_never_overwrites_local_config_or_env_secrets(tmp_path):
    target = tmp_path / "app"
    target.mkdir()
    (target / "config.local.json").write_text('{"secret": "keep-me"}', encoding="utf-8")
    (target / ".env").write_text("TSAI1_HYDRA_KEY=keep-me", encoding="utf-8")

    archive_path = tmp_path / "update.zip"
    archive_path.write_bytes(_zip_bytes({
        "main.py": b"new main",
        "config.local.json": b'{"secret": "overwritten"}',
        ".env": b"TSAI1_HYDRA_KEY=overwritten",
        "other.local.json": b"{}",
    }))

    written, skipped = app_updater.extract_update(archive_path, target)

    assert written == ["main.py"]
    assert sorted(skipped) == [".env", "config.local.json", "other.local.json"]
    assert (target / "config.local.json").read_text(encoding="utf-8") == '{"secret": "keep-me"}'
    assert (target / ".env").read_text(encoding="utf-8") == "TSAI1_HYDRA_KEY=keep-me"
    assert not (target / "other.local.json").exists()


def test_extract_update_only_protects_settings_files_at_the_archive_root(tmp_path):
    """A nested file that happens to share a protected name is not special;
    only the root-level config.json/.env/*.local.json matter here."""
    target = tmp_path / "app"
    target.mkdir()

    archive_path = tmp_path / "update.zip"
    archive_path.write_bytes(_zip_bytes({
        "docs/config.json": b"not the real config",
    }))

    written, skipped = app_updater.extract_update(archive_path, target)

    assert written == ["docs/config.json"]
    assert skipped == []


def test_extract_update_rejects_zip_slip_paths(tmp_path):
    target = tmp_path / "app"
    target.mkdir()
    archive_path = tmp_path / "evil.zip"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("CAD-main/../../escape.py", b"evil")
    archive_path.write_bytes(buffer.getvalue())

    with pytest.raises(app_updater.UpdateError, match="недопустимый путь"):
        app_updater.extract_update(archive_path, target)


def test_extract_update_rejects_a_non_zip_file(tmp_path):
    target = tmp_path / "app"
    target.mkdir()
    bogus = tmp_path / "update.zip"
    bogus.write_bytes(b"not a zip")
    with pytest.raises(app_updater.UpdateError, match="ZIP"):
        app_updater.extract_update(bogus, target)


def test_extract_update_rejects_an_archive_with_no_files(tmp_path):
    target = tmp_path / "app"
    target.mkdir()
    archive_path = tmp_path / "empty.zip"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("CAD-main/", b"")
    archive_path.write_bytes(buffer.getvalue())
    with pytest.raises(app_updater.UpdateError, match="не найдено"):
        app_updater.extract_update(archive_path, target)


def test_apply_update_downloads_and_extracts_reporting_progress(tmp_path, monkeypatch):
    target = tmp_path / "app"
    target.mkdir()
    zip_bytes = _zip_bytes({"main.py": b"updated"})
    monkeypatch.setattr(app_updater, "_http_get", lambda url, timeout=30.0: zip_bytes)

    messages = []
    result = app_updater.apply_update(
        "https://github.com/Berber86/CAD/archive/refs/heads/main.zip",
        target,
        progress=messages.append,
    )

    assert (target / "main.py").read_text(encoding="utf-8") == "updated"
    assert result.files_written == 1
    assert result.app_dir == str(target)
    assert any("скачиваю" in message for message in messages)
    assert any("завершено" in message for message in messages)


def test_apply_update_protects_config_json_and_reports_it(tmp_path, monkeypatch):
    target = tmp_path / "app"
    target.mkdir()
    (target / "config.json").write_text('{"api": {"provider": "openrouter"}}', encoding="utf-8")
    zip_bytes = _zip_bytes({
        "main.py": b"updated",
        "config.json": b'{"api": {"provider": "hydra"}}',
    })
    monkeypatch.setattr(app_updater, "_http_get", lambda url, timeout=30.0: zip_bytes)

    messages = []
    result = app_updater.apply_update(
        "https://github.com/Berber86/CAD/archive/refs/heads/main.zip",
        target,
        progress=messages.append,
    )

    assert (target / "main.py").read_text(encoding="utf-8") == "updated"
    assert (target / "config.json").read_text(encoding="utf-8") == '{"api": {"provider": "openrouter"}}'
    assert result.files_written == 1
    assert result.protected_skipped == ("config.json",)
    assert any("config.json" in message and "сохранены" in message for message in messages)


def test_apply_update_surfaces_download_errors_without_touching_disk(tmp_path, monkeypatch):
    target = tmp_path / "app"
    target.mkdir()
    (target / "main.py").write_text("original", encoding="utf-8")

    def boom(url, timeout=30.0):
        raise app_updater.UpdateError("сеть недоступна")

    monkeypatch.setattr(app_updater, "_http_get", boom)
    with pytest.raises(app_updater.UpdateError, match="сеть недоступна"):
        app_updater.apply_update("https://github.com/Berber86/CAD", target)
    assert (target / "main.py").read_text(encoding="utf-8") == "original"
