"""
Файлы для pip и запуска не должны зависеть от кодировки системы.

Причина теста — реальная ошибка на машине геодезиста (Windows, русская локаль):

    [2/3] Проверяю зависимости ...
    UnicodeDecodeError: 'charmap' codec can't decode byte 0x98 in position 967
    decoding with 'cp1251' codec failed

pip декодирует `requirements*.txt` в кодировке системы (на русской Windows это
cp1251), а не в UTF-8. Русский комментарий в UTF-8 ломает установку целиком —
поэтому в файлах для pip только ASCII, а объяснения по-русски живут в README.
"""

from __future__ import annotations

import codecs
import locale
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _pip_decode(data: bytes) -> str:
    """
    Как pip читает requirements-файл: BOM, иначе — локаль системы.

    Повторяем ровно то поведение, из-за которого упала установка.
    """
    for bom, encoding in ((codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16"),
                          (codecs.BOM_UTF16_BE, "utf-16")):
        if data.startswith(bom):
            return data[len(bom):].decode(encoding)
    return data.decode(locale.getpreferredencoding(False))


def test_requirements_files_are_pure_ascii():
    """Ни BOM, ни кириллицы: иначе pip на русской Windows падает с UnicodeDecodeError."""
    files = sorted(ROOT.glob("requirements*.txt"))
    assert files, "файлы зависимостей не найдены"
    problems = []
    for file in files:
        data = file.read_bytes()
        if data.startswith(codecs.BOM_UTF8):
            problems.append(f"{file.name}: файл с UTF-8 BOM")
            continue
        try:
            data.decode("ascii")
        except UnicodeDecodeError as exc:
            line = data[: exc.start].count(b"\n") + 1
            problems.append(
                f"{file.name}: не-ASCII байт 0x{data[exc.start]:02x} в строке {line} "
                f"({data[max(0, exc.start - 20):exc.start + 20]!r})"
            )
    assert not problems, (
        "pip читает requirements*.txt в кодировке системы (cp1251 на русской Windows) — "
        "комментарии должны быть ASCII, русские пояснения — в README.md: " + "; ".join(problems)
    )


@pytest.mark.parametrize("encoding", ["cp1251", "cp866", "utf-8"])
def test_requirements_survive_every_windows_encoding(encoding):
    """Даже если локаль окажется другой, установка не должна спотыкаться."""
    for file in sorted(ROOT.glob("requirements*.txt")):
        file.read_bytes().decode(encoding)  # ASCII читается любой однобайтовой кодировкой
        assert _pip_decode(file.read_bytes())


def test_local_ocr_requirements_are_optional_in_installation():
    """.bat ставит OCR необязательно: его провал не должен срывать запуск."""
    text = (ROOT / "run.bat").read_text(encoding="utf-8")
    assert "requirements-ocr.txt" in text
    lines = [line.strip() for line in text.splitlines()]
    installs = [line for line in lines if "requirements-ocr.txt" in line and "pip install" in line]
    assert installs, "run.bat не пытается установить локальный OCR"
    for line in installs:
        assert "goto :error" not in line
    # Подсказка, если OCR не встал, а не «проверьте Python».
    assert any("errorlevel 1" in line and "OCR" in line for line in lines)


def test_readme_explains_the_ascii_rule():
    """Правило записано там, где его прочтут: рядом с установкой."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "ASCII" in readme and "cp1251" in readme
