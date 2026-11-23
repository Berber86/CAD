"""Таймаут ожидания ответа LLM: дефолт 500 с и миграция старого дефолта 180.

Ответы Vision-моделей через агрегаторов (случай Николая: grok-4.7 у Hydra,
433 с) не укладывались в прежние 180 с: клиент обрывал запрос, SDK повторял
его, а провайдер списывал входные токены за каждую попытку.
"""
from __future__ import annotations

import json

from llm_parser import DEFAULT_CONFIG, load_config


def test_default_timeout_is_500_without_config(tmp_path):
    cfg = load_config(tmp_path / "nonexistent.json")
    assert cfg["api"]["timeout_sec"] == 500
    assert DEFAULT_CONFIG["api"]["timeout_sec"] == 500


def test_legacy_default_180_is_migrated_to_500(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"api": {"timeout_sec": 180}}), encoding="utf-8")
    assert load_config(path)["api"]["timeout_sec"] == 500


def test_explicit_timeout_is_preserved(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"api": {"timeout_sec": 300}}), encoding="utf-8")
    assert load_config(path)["api"]["timeout_sec"] == 300
