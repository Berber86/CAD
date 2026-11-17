"""Числа с буквами вместо цифр (ocr_numbers): правила, которые не должны портить описание.

Положительные случаи — реальные и синтетические порчи сканов. Отрицательные — то, что трогать нельзя:
направления (S, N, W), даты, адреса, слова. Эталонные значения участков (POB 201.00, 161.00, 241.00)
проверяются тестами разбора, здесь — только сами токены.
"""

import pytest

import llm_parser
import ocr_numbers
import ocr_pages


@pytest.mark.parametrize(
    "raw, fixed",
    [
        ("North 3l° 46' 5l\"", "North 31° 46' 51\""),          # знаки градуса и дюйма после порчи
        ("North 3l degrees 46 minutes 5l seconds west", "North 31 degrees 46 minutes 51 seconds west"),
        ("25.4l feet", "25.41 feet"),                           # буква в дробной части
        ("B5.00 feet", "85.00 feet"),                           # B, Z, G — между цифрами и в начале
        ("Z5.00 feet", "25.00 feet"),
        ("1O0 feet", "100 feet"),
        ("THENCE 2l4.0O feet", "THENCE 214.00 feet"),          # O в дробной части
        ("N 8l degrees 46 minutes", "N 81 degrees 46 minutes"),
        ("l2 feet", "12 feet"),                                 # одна ведущая буква без точки
        ("Iio.00 feet", "110.00 feet"),
        ("North 3lo degrees", "North 310 degrees"),
        ("Avenue,40.O0 feet", "Avenue,40.00 feet"),
        ("241.O feet", "241.0 feet"),
    ],
)
def test_lookalike_letters_inside_numbers_are_fixed(raw, fixed):
    assert ocr_numbers.fix_text(raw) == fixed


@pytest.mark.parametrize(
    "raw",
    [
        "South 56 degrees 48 minutes 40 seconds west",        # реальные цифры не трогаем
        "S56°48'40\"W",                                         # S в начале — направление, не 5
        "S6.00 feet",                                           # S вне цифр не меняем
        "on the 1l day of October",                             # дата: нет единицы и точки
        "36o Burgher Avenue",                                   # адрес: нет единицы и точки
        "llo68 feet",                                           # без точки и с двумя ведущими буквами — не угадываем
        "North 1O0 minutes",                                    # минуты не могут быть 100
        "thence along the southwesterly side",                  # слова без цифр
        "lo feet",                                              # одна буква-двойник без цифры
    ],
)
def test_text_that_is_not_a_number_is_left_alone(raw):
    assert ocr_numbers.fix_text(raw) == raw


def test_number_key_is_the_same_for_both_readings():
    assert ocr_numbers.number_key("llo.68") == "110.68"
    assert ocr_numbers.number_key("110.68") == "110.68"
    assert ocr_numbers.number_key("3l") == "31"
    assert ocr_numbers.number_key("thc") is None
    assert ocr_numbers.number_key("lo") is None


def test_normalizer_of_the_parser_uses_the_same_rules():
    assert llm_parser._normalize_legal_text("East llo.68 feet") == "East 110.68 feet"
    assert llm_parser._normalize_legal_text("North 3l degreca 46 ainutes") == "North 31 degrees 46 minutes"


def test_vote_uses_the_numeric_key():
    assert ocr_pages.vote_tokens("2l4.0O", "214.00") == "214.00"
    assert ocr_pages.vote_tokens("3l", "31") == "31"
