"""Режимы журналов: «подробный» и «упрощённый» (log_mode.py)."""

import json

import pytest

import log_mode


def test_default_mode_is_simple_when_config_has_no_key():
    assert log_mode.mode_from_config({}) == log_mode.SIMPLE
    assert log_mode.mode_from_config({"ui": {}}) == log_mode.SIMPLE
    assert log_mode.mode_from_config({"ui": {"log_mode": "full"}}) == log_mode.FULL
    assert log_mode.mode_from_config({"ui": {"log_mode": "что-то"}}) == log_mode.SIMPLE


def test_simple_mode_keeps_errors_and_key_items_and_drops_the_rest():
    long_info = "Фасад: " + "очень длинное описание " * 4
    entries = [
        (log_mode.LEVEL_INFO, "12:00:01  качаю страницу 3"),
        (log_mode.LEVEL_INFO, "12:00:02  " + long_info),
        (log_mode.LEVEL_INFO, "12:00:03  строка один\nстрока два"),
        (log_mode.LEVEL_WARN, "предупреждение"),
        (log_mode.LEVEL_KEY, "Сбор завершён"),
        (log_mode.LEVEL_ERROR, "Ошибка: сайт не ответил " + "x" * 200),
    ]
    assert log_mode.filter_entries(entries, log_mode.SIMPLE) == [
        "12:00:01  качаю страницу 3",
        "Сбор завершён",
        "Ошибка: сайт не ответил " + "x" * 200,
    ]
    assert len(log_mode.filter_entries(entries, log_mode.FULL)) == 6


def test_simple_info_limit_is_about_seventy_characters():
    stamp = "12:00:01  "
    assert log_mode.level_visible(log_mode.LEVEL_INFO, log_mode.SIMPLE, stamp + "x" * 70)
    assert not log_mode.level_visible(log_mode.LEVEL_INFO, log_mode.SIMPLE, stamp + "x" * 71)
    assert log_mode.level_visible(log_mode.LEVEL_INFO, log_mode.FULL, stamp + "x" * 500)


def test_report_line_levels_follow_the_markers():
    assert log_mode.report_line_level("⛔ не найден лот") == log_mode.LEVEL_ERROR
    assert log_mode.report_line_level("  ✗ сторона #2 не сошлась") == log_mode.LEVEL_ERROR
    assert log_mode.report_line_level("⚠ мало данных") == log_mode.LEVEL_WARN
    assert log_mode.report_line_level("▸ Сторона #1") == log_mode.LEVEL_KEY
    assert log_mode.report_line_level("ИТОГ РАЗБОРА") == log_mode.LEVEL_KEY
    assert log_mode.report_line_level("  детали") == log_mode.LEVEL_INFO


def test_step1_report_hides_warnings_and_ok_lines_in_simple_mode_only():
    lines = ["ИТОГ", "✓ сторона 1", "⚠ мало данных", "✎ правка соседей", "✗ ошибка", ""]
    assert log_mode.filter_report_lines(lines, log_mode.FULL) == lines
    simple = log_mode.filter_report_lines(lines, log_mode.SIMPLE)
    assert simple == ["ИТОГ", "✎ правка соседей", "✗ ошибка"]


@pytest.mark.parametrize(
    ("line", "level"),
    [
        ("ШАГ 2 · АВТОПОДПИСАНИЕ ОБЪЕКТОВ", log_mode.LEVEL_KEY),
        ("ПОДПИСАНО: 15 (вдоль линии: 4)", log_mode.LEVEL_KEY),
        ("  • ОШИБКА: не удалось прочитать слой", log_mode.LEVEL_ERROR),
        ("ДОМ НЕ ПОДПИСАН: поле пустое", log_mode.LEVEL_ERROR),
        ("ШАГ 2 НЕ ВЫПОЛНЕН", log_mode.LEVEL_ERROR),
        ("подписей больше, чем объектов: забор", log_mode.LEVEL_WARN),
        ("  • по именам слоёв: 14", log_mode.LEVEL_INFO),
    ],
)
def test_step2_line_levels(line, level):
    assert log_mode.step2_line_level(line) == level


def test_step2_simple_view_keeps_totals_and_errors():
    lines = [
        "ШАГ 2 · АВТОПОДПИСАНИЕ",
        "Граница участка: слой X",
        "",
        "НАЙДЕНО ОБЪЕКТОВ С ПОДПИСЬЮ: 18",
        "  • по именам слоёв: 14",
        "ПОДПИСАНО: 15 (вдоль линии: 4)",
        "  • ОШИБКА: слой D не прочитан",
    ]
    assert log_mode.filter_step2_lines(lines, log_mode.FULL) == lines
    assert log_mode.filter_step2_lines(lines, log_mode.SIMPLE) == [
        "ШАГ 2 · АВТОПОДПИСАНИЕ",
        "",
        "НАЙДЕНО ОБЪЕКТОВ С ПОДПИСЬЮ: 18",
        "ПОДПИСАНО: 15 (вдоль линии: 4)",
        "  • ОШИБКА: слой D не прочитан",
    ]


def test_step2_failure_report_is_shown_whole_with_its_reason():
    lines = ["ШАГ 2 НЕ ВЫПОЛНЕН", "", "Высота текста подписей указана неверно", "Подсказка: 3"]
    assert log_mode.filter_step2_lines(lines, log_mode.SIMPLE) == lines


def test_save_mode_changes_only_ui_log_mode(tmp_path):
    path = tmp_path / "config.json"
    original = {
        "ui": {"always_on_top": True, "theme": "dark"},
        "drawing": {"base_scale": 16.0},
    }
    path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")

    log_mode.save_mode_to_file(path, log_mode.FULL)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["ui"] == {"always_on_top": True, "theme": "dark", "log_mode": "full"}
    assert saved["drawing"] == {"base_scale": 16.0}

    log_mode.save_mode_to_file(path, log_mode.SIMPLE)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["ui"]["log_mode"] == "simple"
    assert saved["ui"]["theme"] == "dark"


def test_save_mode_creates_missing_file(tmp_path):
    path = tmp_path / "new.json"
    log_mode.save_mode_to_file(path, log_mode.FULL)
    assert json.loads(path.read_text(encoding="utf-8")) == {"ui": {"log_mode": "full"}}


def test_step1_simple_window_keeps_errors_headers_and_conflicts_only():
    lines = [
        "ИТОГ РАЗБОРА НАБОРА PDF",
        "",
        "▸ СОСЕД #1 — LOT 30.pdf",
        "   стр. 1 из 1 · локально, бесплатно",
        "   ⚠ построен с замечаниями (4): сторон 4",
        "      ⛔ невязка 0.609 ft — контур не замыкается",
        "      • Расчёт: длины и углы сохранены строго",
        "",
        "НАШ УЧАСТОК ОТНОСИТЕЛЬНО СОСЕДЕЙ",
        "   [✓] Общая межа: сторона #1 ↔ сосед #3 — совпала",
        "   [✗] Общая межа: сторона #1 ↔ сосед #3",
        "       Разница длины ΔL = -2.030' — расхождение",
        "🚨 Сосед #1: НАЛОЖЕНИЕ ГРАНИЦ (Overlap / Encroachment)",
        "[1] ✔ Сторона #4: румб S 22°26'12\" E — из описания соседа",
        "    Правка: сторона #4 — S 0°00' E → S 22°26'12\" E",
    ]
    assert log_mode.filter_report_lines(lines, log_mode.FULL) == lines
    assert log_mode.filter_report_lines(lines, log_mode.SIMPLE) == [
        "ИТОГ РАЗБОРА НАБОРА PDF",
        "",
        "▸ СОСЕД #1 — LOT 30.pdf",
        "      ⛔ невязка 0.609 ft — контур не замыкается",
        "",
        "НАШ УЧАСТОК ОТНОСИТЕЛЬНО СОСЕДЕЙ",
        "   [✗] Общая межа: сторона #1 ↔ сосед #3",
        "       Разница длины ΔL = -2.030' — расхождение",
        "🚨 Сосед #1: НАЛОЖЕНИЕ ГРАНИЦ (Overlap / Encroachment)",
        "[1] ✔ Сторона #4: румб S 22°26'12\" E — из описания соседа",
        "    Правка: сторона #4 — S 0°00' E → S 22°26'12\" E",
    ]
