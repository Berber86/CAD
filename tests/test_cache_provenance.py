"""Кеш распознанных текстов PDF: записывается один раз, в журнале видно, чем он записан."""

from __future__ import annotations

import os
import stat

import pytest


@pytest.fixture
def cache(tmp_path, monkeypatch):
    import ocr_pages

    monkeypatch.setenv("TITLESURVEY_OCR_CACHE", str(tmp_path / "cache"))
    return ocr_pages


LOCAL = {"записал": "локальный OCR: RapidOCR (локально, бесплатно)", "модель": "",
         "файл": "LOT 2.pdf", "страницы": "3–3", "dpi": 600}


def test_first_write_is_kept_and_second_write_does_not_overwrite(cache):
    cache._write_cache("k1", "первый текст", info=LOCAL)
    cache._write_cache("k1", "второй текст")
    assert cache._read_cache("k1") == "первый текст"


def test_provenance_says_which_local_reader_or_which_model_wrote_it(cache):
    from version import APP_VERSION

    cache._write_cache("k2", "текст", info=LOCAL)
    info = cache.cache_provenance("k2")
    assert info["записал"].startswith("локальный OCR")
    assert info["модель"] == ""
    assert info["программа"] == f"v{APP_VERSION}"
    assert info["файл"] == "LOT 2.pdf" and info["страницы"] == "3–3" and info["dpi"] == 600
    assert "записано" in info and "компьютер" not in info and "записал" in info
    assert "локальный OCR" in cache.describe_provenance(info)

    cache._write_cache("k3", "текст ИИ", info={"записал": "ИИ", "модель": "pro/deepseek-v4.1-flash-1017"})
    ai = cache.cache_provenance("k3")
    assert ai["записал"] == "ИИ" and ai["модель"] == "pro/deepseek-v4.1-flash-1017"
    assert "pro/deepseek-v4.1-flash-1017" in cache.describe_provenance(ai)


def test_provenance_never_enters_the_text_sent_to_the_model(cache):
    cache._write_cache("k4", "ALL THAT CERTAIN текст", info=LOCAL)
    text = cache._read_cache("k4")
    assert text == "ALL THAT CERTAIN текст"
    for word in ("записал", "программа", "RapidOCR", "записано"):
        assert word not in text


def test_cache_files_are_read_only(cache):
    if os.name == "nt":
        pytest.skip("права доступа POSIX")
    cache._write_cache("k5", "текст", info=LOCAL)
    assert not os.stat(cache._provenance_file("k5")).st_mode & stat.S_IWUSR
    assert not os.stat(cache.cache_dir() / "k5.txt").st_mode & stat.S_IWUSR


def test_old_entry_without_provenance_is_reported_honestly(cache):
    folder = cache.cache_dir()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "old.txt").write_text("старый", encoding="utf-8")
    assert cache.cache_provenance("old") is None
    assert "неизвестно" in cache.describe_provenance(None)


def test_clear_removes_protected_entries_and_allows_a_new_write(cache):
    cache._write_cache("k6", "первый", info=LOCAL)
    assert cache.clear_cache() == 1
    assert cache._read_cache("k6") is None and cache.cache_provenance("k6") is None
    cache._write_cache("k6", "новый", info=LOCAL)
    assert cache._read_cache("k6") == "новый"
