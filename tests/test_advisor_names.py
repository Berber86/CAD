"""ИИ-советник: названия перекрёстка и переименования улиц принимаются только по тексту участка."""

from __future__ import annotations

from pathlib import Path

DATA = Path(__file__).parent / "data"
OUR_TEXT = (DATA / "alvine_lot32_legal.txt").read_text(encoding="utf-8")
NEIGHBOUR_TEXT = (DATA / "alvine_lot30_legal.txt").read_text(encoding="utf-8")


def _builder():
    from acad_builder import AcadBuilder
    from llm_parser import load_config

    return AcadBuilder(load_config())


def _lot(name, text, *, main=False, **update):
    from conflict_resolver import LotInput
    from llm_parser import parse_with_regex

    desc = parse_with_regex(text)[0]
    if update:
        desc = desc.model_copy(update=update)
    return LotInput(key=name, name=name, is_main=main, description=desc, source_text=text)


def _analyse(lots, **kwargs):
    from conflict_resolver import analyse_conflicts

    return analyse_conflicts(_builder(), lots, scale=16, **kwargs)


def _reply(*changes, lot="L1", quote=""):
    def advisor(_system, _user_text, _images):
        return {"proposals": [{
            "lot": lot, "title": "Название", "reason": "по тексту",
            "source_quote": quote, "changes": list(changes),
        }]}, None

    return advisor


def test_street_name_match_ignores_case_spacing_and_suffix_form():
    from conflict_resolver import _street_in_text

    assert _street_in_text("Alvine Avenue", OUR_TEXT)
    assert _street_in_text("Alvine Ave.", OUR_TEXT)
    assert _street_in_text("amboy road", OUR_TEXT)
    # Другая улица с похожим окончанием не совпадает.
    assert not _street_in_text("Alvine Street", OUR_TEXT)
    assert not _street_in_text("Doty Avenue", OUR_TEXT)
    # Текст без пробелов (так OCR склеивает строки) тоже находится.
    assert _street_in_text("Alvine Avenue", "ALVINEAVENUE(30 FEETWIDE)")


def test_advisor_cross_street_is_accepted_only_when_printed_in_the_lot_text():
    # У нашего участка перекрёсток неверный; советник возвращает напечатанный в тексте.
    lots = [_lot("our.pdf", OUR_TEXT, main=True, cross_street="Doty Avenue"), _lot("lot30.pdf", NEIGHBOUR_TEXT)]
    good = _analyse(lots, advisor=_reply(
        {"target": "cross_street", "name": "Amboy Road"}, quote="Amboy Road",
    ), force_advisor=True)
    found = next(p for p in good.proposals if p.kind == "advisor")
    assert found.verdict == "partial"
    assert found.changes[0].describe() == "перекрёсток: «Doty Avenue» → «Amboy Road»"
    assert "геометрия не меняется" in " ".join(found.reasons)
    # После принятия перекрёсток участка становится напечатанным.
    lots[0].fixes = (found,)
    from conflict_resolver import apply_changes
    assert apply_changes(lots[0].description, found.changes).cross_street == "Amboy Road"

    # Название, которого нет в тексте этого участка, отклоняется с причиной.
    bad = _analyse(lots, advisor=_reply({"target": "cross_street", "name": "Doty Avenue"}),
                   force_advisor=True)
    assert not [p for p in bad.proposals if p.kind == "advisor"]
    assert any("не напечатано в тексте" in line for line in bad.advisor_rejected)


def test_street_alias_needs_both_names_and_a_renaming_word_in_text():
    lots = [_lot("our.pdf", OUR_TEXT, main=True), _lot("lot30.pdf", NEIGHBOUR_TEXT)]
    # Обе улицы напечатаны, но слова о переименовании нет — отклоняем.
    # «commonly known as 24 Alvine Avenue» — адрес, а не переименование.
    rejected = _analyse(lots, advisor=_reply(
        {"target": "street_alias", "current": "Alvine Avenue", "old": "Amboy Road"},
    ), force_advisor=True)
    assert any("переименован" in line for line in rejected.advisor_rejected)
    # Название, которого нет в тексте, тоже отклоняется.
    missing = _analyse(lots, advisor=_reply(
        {"target": "street_alias", "current": "Alvine Avenue", "old": "Doty Avenue"},
    ), force_advisor=True)
    assert any("не напечатаны" in line for line in missing.advisor_rejected)


def test_alias_change_applies_to_street_aliases_list():
    from conflict_resolver import Change, apply_changes

    lot = _lot("our.pdf", OUR_TEXT, main=True)
    change = Change(target="street", index=-1, field="alias", new_label="Alvine Avenue = Alvine Ave")
    result = apply_changes(lot.description, [change])
    assert "Alvine Avenue = Alvine Ave" in result.street_aliases
    assert change.describe() == "название улицы: добавить «Alvine Avenue = Alvine Ave»"
    # Повторная правка не дублирует пару.
    again = apply_changes(result, [change])
    assert again.street_aliases.count("Alvine Avenue = Alvine Ave") == 1


def test_rename_marker_ignores_address_and_owner_phrases():
    from conflict_resolver import _RENAME_MARK_RE

    assert _RENAME_MARK_RE.search("Seaside Boulevard (now Father Capodanno Blvd.)")
    assert _RENAME_MARK_RE.search("Carlton Street (formerly known as Stafford Avenue)")
    assert _RENAME_MARK_RE.search("Doty Avenue f/k/a Seaside Boulevard")
    assert not _RENAME_MARK_RE.search("Premises commonly known as 24 Alvine Avenue")
    assert not _RENAME_MARK_RE.search("Lands of John Smith now or formerly, Block 3")


def test_printed_renaming_already_known_to_parser_is_not_duplicated():
    from conflict_resolver import apply_changes

    text = OUR_TEXT + "\nAmboy Road (formerly Amboy Street) is the cross street.\n"
    lots = [_lot("our.pdf", text, main=True), _lot("lot30.pdf", NEIGHBOUR_TEXT)]
    # Разбор уже увидел переименование в тексте: совет его не повторяет.
    assert "Amboy Road = Amboy Street" in lots[0].description.street_aliases
    analysis = _analyse(lots, advisor=_reply(
        {"target": "street_alias", "current": "Amboy Road", "old": "Amboy Street"},
    ), force_advisor=True)
    assert not [p for p in analysis.proposals if p.kind == "advisor"]
    assert not analysis.advisor_rejected
    # Правка того же переименования, применённая вручную, тоже не дублирует пару.
    from conflict_resolver import Change
    change = Change(target="street", index=-1, field="alias", new_label="Amboy Road = Amboy Street")
    once = apply_changes(lots[0].description, [change])
    assert once.street_aliases.count("Amboy Road = Amboy Street") == 1


def test_misread_cross_street_printed_in_own_text_becomes_a_button_without_advisor():
    # У нашего участка перекрёсток прочитан с опиской («Amboi»); в его тексте напечатано «Amboy Road»,
    # а сосед подтверждает ту же улицу. Кнопка появляется без вопроса советнику.
    lots = [_lot("our.pdf", OUR_TEXT, main=True, cross_street="Amboi Road"), _lot("lot30.pdf", NEIGHBOUR_TEXT)]
    analysis = _analyse(lots)
    found = [p for p in analysis.proposals if p.kind == "street_name"]
    assert len(found) == 1
    assert found[0].origin == "rule" and found[0].verdict == "partial"
    assert found[0].changes[0].describe() == "перекрёсток: «Amboi Road» → «Amboy Road»"
    assert "у соседа «lot30.pdf» та же улица" in " ".join(found[0].reasons)
    # Принятие: участок получает напечатанный перекрёсток, повторной находки нет.
    lots[0].fixes = (found[0],)
    assert not [p for p in _analyse(lots).proposals if p.kind == "street_name"]


def test_misread_cross_street_without_a_confirming_neighbour_gives_no_button():
    lots = [_lot("our.pdf", OUR_TEXT, main=True, cross_street="Amboi Road")]
    assert not [p for p in _analyse(lots).proposals if p.kind == "street_name"]


def test_name_already_printed_gives_no_button():
    lots = [_lot("our.pdf", OUR_TEXT, main=True), _lot("lot30.pdf", NEIGHBOUR_TEXT)]
    assert not [p for p in _analyse(lots).proposals if p.kind == "street_name"]


def test_rename_printed_in_one_lot_is_shared_by_the_whole_set():
    # Участок A напечатал переименование «Amboy Road (now Amboy Street)»; сосед B его не печатает,
    # а назвал перекрёсток старым именем. Общие синонимы делают их одним перекрёстком для всего набора.
    from acad_builder import _check_street_reference_and_frontage
    from conflict_resolver import _Scene

    text_a = OUR_TEXT + "\nAmboy Road (now Amboy Street) is the cross street.\n"
    lot_a = _lot("a.pdf", text_a, main=True)
    lot_b = _lot("b.pdf", NEIGHBOUR_TEXT, cross_street="Amboy Street")
    assert "Amboy Street = Amboy Road" in lot_a.description.street_aliases  # «Amboy Road (now Amboy Street)»
    assert not lot_b.description.street_aliases
    scene = _Scene(_builder(), [lot_a, lot_b], 16)
    desc_a = scene.description(lot_a)
    desc_b = scene.description(lot_b)
    assert "Amboy Street = Amboy Road" in desc_b.street_aliases
    # Исходные описания не меняются.
    assert not lot_b.description.street_aliases
    note, warn = _check_street_reference_and_frontage(scene.plan(lot_a), scene.plan(lot_b))
    assert not warn and "Amboy Road" in note  # общий перекрёсток: предупреждения о разных углах нет
