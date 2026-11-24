"""Страница «Данные»: разбор имён папок-задач и раскладка по data."""
from __future__ import annotations

from pathlib import Path

import job_queue


def test_parse_job_folder_name_valid():
    assert job_queue.parse_job_folder_name("1234#417 WEST 33RD STREET") == (
        "1234", "", "417 WEST 33RD STREET")
    assert job_queue.parse_job_folder_name("1234A#417 WEST 33RD STREET") == (
        "1234", "A", "417 WEST 33RD STREET")
    assert job_queue.parse_job_folder_name("2024-17 # 88 KING ST") == (
        "2024", "-17", "88 KING ST")
    # Адрес может содержать свой '#': режем только по первому.
    assert job_queue.parse_job_folder_name("77#12 MAIN ST #4") == (
        "77", "", "12 MAIN ST #4")


def test_parse_job_folder_name_invalid():
    for name in ("417 WEST 33RD STREET", "ABC#417 WEST", "1234#", "#417 WEST", "", "1234"):
        assert job_queue.parse_job_folder_name(name) is None, name


def test_describe_and_invalid_reason(tmp_path):
    folder = tmp_path / "1234A#417 WEST 33RD STREET"
    folder.mkdir()
    info = job_queue.describe_job_folder(folder)
    assert info is not None
    assert (info.job_no, info.suffix, info.address) == ("1234", "A", "417 WEST 33RD STREET")
    assert info.title == "1234A · 417 WEST 33RD STREET"
    assert job_queue.describe_job_folder(tmp_path / "просто папка") is None
    assert "«#»" in job_queue.invalid_reason("просто папка")
    assert "номера" in job_queue.invalid_reason("ABC#417 WEST")
    assert "адрес" in job_queue.invalid_reason("1234#")


def test_scan_catalog_lists_subdirs_sorted(tmp_path):
    (tmp_path / "2000#B ST").mkdir()
    (tmp_path / "1000#A ST").mkdir()
    (tmp_path / "файл.txt").write_text("x", encoding="utf-8")
    found = job_queue.scan_catalog(tmp_path)
    assert [p.name for p in found] == ["1000#A ST", "2000#B ST"]
    assert job_queue.scan_catalog(tmp_path / "нет такой") == []


def test_data_dir_and_session_log_path(tmp_path):
    job = tmp_path / "1234#417 WEST 33RD STREET"
    assert job_queue.data_dir_for(job) == job / "data"
    assert job_queue.session_log_path(job) == job / "data" / "session_log.txt"


def _entry(source, level, text, stamp="10:00:00"):
    return (stamp, source, level, text)


def test_session_log_keeps_outcomes_warnings_errors_and_steps():
    entries = [
        _entry("шаг0", "key", "район, Block и Lot получены"),
        _entry("шаг0", "warn", "уверенность Vision низкая"),
        _entry("шаг0", "error", "LOT 62: документ не скачался"),
        _entry("шаг1", "info", "собрано документов: 3"),
        # Содержательные информационные строки остаются любой длины и вида:
        # это след хода работы, по которому видно, где всё остановилось.
        _entry("шаг0", "info", "ищу в ACRIS блок 6643, лот 61 и документ CRFN 2026000329272…"),
        _entry("шаг0", "info", "д" * 120),
    ]
    assert job_queue.session_log_lines(entries) == [
        "[10:00:00] [шаг0] район, Block и Lot получены",
        "[10:00:00] [шаг0] уверенность Vision низкая",
        "[10:00:00] [шаг0] LOT 62: документ не скачался",
        "[10:00:00] [шаг1] собрано документов: 3",
        "[10:00:00] [шаг0] ищу в ACRIS блок 6643, лот 61 и документ CRFN 2026000329272…",
        f"[10:00:00] [шаг0] {'д' * 120}",
    ]


def test_session_log_drops_only_mechanical_noise():
    entries = [
        _entry("шаг0", "info", "качаю PDF документа CRFN 2026000329272…"),
        _entry("шаг0", "info", "стр. 2 из 5 обработана"),
        _entry("шаг0", "info", "   "),
        _entry("очередь", "key", "Создан новый документ AutoCAD; рисую схему…"),
    ]
    assert job_queue.session_log_lines(entries) == [
        "[10:00:00] [очередь] Создан новый документ AutoCAD; рисую схему…"]


def test_session_log_collapses_consecutive_repeats_only():
    entries = (
        [_entry("шаг0", "info", "собрано документов: 3")] * 3
        + [_entry("шаг0", "info", "собрано документов: 4"),
           _entry("шаг0", "info", "собрано документов: 3")]
    )
    # Повтор не подряд — отдельная строка: хронология хода работы не врёт.
    assert job_queue.session_log_lines(entries) == [
        "[10:00:00] [шаг0] собрано документов: 3 (повтор ×3)",
        "[10:00:00] [шаг0] собрано документов: 4",
        "[10:00:00] [шаг0] собрано документов: 3",
    ]
