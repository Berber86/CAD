"""Стартовая вкладка Шага 1 (ui.start_source): чтение, запись и подписи."""

import json

import start_source


def test_default_is_text_when_missing():
    assert start_source.start_source_from_config({}) == start_source.TEXT
    assert start_source.start_source_from_config(None) == start_source.TEXT
    assert start_source.start_source_from_config({"ui": {"start_source": "bogus"}}) == start_source.TEXT
    assert start_source.label_for(start_source.TEXT) == "📝 Текст"


def test_reads_pdf_from_config():
    cfg = {"ui": {"start_source": "PDF"}}
    assert start_source.start_source_from_config(cfg) == start_source.PDF
    assert start_source.label_for(start_source.PDF) == "📄 PDF"


def test_label_round_trip():
    for key in (start_source.TEXT, start_source.PDF):
        assert start_source.key_for_label(start_source.label_for(key)) == key
    assert start_source.key_for_label("неизвестно") == start_source.TEXT


def test_save_keeps_other_keys(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"api": {"x": 1}, "ui": {"log_mode": "full"}}), encoding="utf-8")
    start_source.save_start_source_to_file(path, start_source.PDF)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["api"] == {"x": 1}
    assert data["ui"] == {"log_mode": "full", "start_source": "pdf"}


def test_save_creates_file(tmp_path):
    path = tmp_path / "config.json"
    start_source.save_start_source_to_file(path, "anything")
    assert json.loads(path.read_text(encoding="utf-8")) == {"ui": {"start_source": "text"}}
