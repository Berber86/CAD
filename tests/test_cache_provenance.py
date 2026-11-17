"""Кеш распознанных текстов PDF: записывается один раз и помнит, кто и когда его записал."""

from __future__ import annotations

import getpass
import os
import stat

import pytest


@pytest.fixture
def cache(tmp_path, monkeypatch):
    import ocr_pages

    monkeypatch.setenv("TITLESURVEY_OCR_CACHE", str(tmp_path / "cache"))
    return ocr_pages


def test_first_write_is_kept_and_second_write_does_not_overwrite(cache):
    cache._write_cache("k1", "первый текст", info={"файл": "LOT 1.pdf", "страницы": "1–1", "dpi": 400})
    cache._write_cache("k1", "второй текст")
    assert cache._read_cache("k1") == "первый текст"


def test_provenance_records_who_when_which_version(cache):
    from version import APP_VERSION

    cache._write_cache("k2", "текст", info={"файл": "LOT 2.pdf", "страницы": "3–3", "dpi": 600})
    info = cache.cache_provenance("k2")
    assert info is not None
    assert info["записал"] == getpass.getuser()
    assert info["программа"] == f"v{APP_VERSION}"
    assert info["файл"] == "LOT 2.pdf" and info["страницы"] == "3–3" and info["dpi"] == 600
    assert info["записано"] and info["компьютер"]
    line = cache.describe_provenance(info)
    assert getpass.getuser() in line and f"v{APP_VERSION}" in line


def test_cache_files_are_read_only(cache):
    if os.name == "nt":
        pytest.skip("права доступа POSIX")
    cache._write_cache("k3", "текст")
    mode = os.stat(cache._provenance_file("k3")).st_mode
    assert not mode & stat.S_IWUSR
    assert not os.stat(cache.cache_dir() / "k3.txt").st_mode & stat.S_IWUSR


def test_old_entry_without_provenance_is_reported_honestly(cache):
    folder = cache.cache_dir()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "old.txt").write_text("старый", encoding="utf-8")
    assert cache.cache_provenance("old") is None
    assert "неизвестно" in cache.describe_provenance(None)


def test_clear_removes_protected_entries_and_allows_a_new_write(cache):
    cache._write_cache("k4", "первый")
    assert cache.clear_cache() == 1
    assert cache._read_cache("k4") is None and cache.cache_provenance("k4") is None
    cache._write_cache("k4", "новый")
    assert cache._read_cache("k4") == "новый"
