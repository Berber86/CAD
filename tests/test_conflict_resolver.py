"""
Шаг 1: конфликты между участками набора и данные, взятые из описаний соседей.

Пример заказчика (Staten Island, Alvine Avenue): в нашем описании курс вдоль
улицы задан только словом — «southerly along the easterly line of Alvine
Avenue 40 feet», румба нет. Участок строился по S 0° E, не замыкался на
15.57 ft и накладывался на соседа. В описании соседа линия той же улицы
имеет румб S 22°26'12" E.

Проверяется то, о чём договорились с заказчиком:

* находку делает Python и проверяет её геометрией (невязка, наложение);
* данные участка меняются только после «Применить», правку можно вернуть;
* если конфликт остался — платный вопрос ИИ-советнику; его советы тоже
  проверяются геометрией, а ответ для того же разбора заново не запрашивается;
* двойные длины (deed / tax map) программа сама не переключает.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import test_gui_smoke as smoke

gui = smoke.gui  # фикстура окна с подменённым tkinter
_write_test_pdf = smoke._write_test_pdf
_wait = smoke._wait_for_gui_worker

DATA = Path(__file__).parent / "data"
OUR_TEXT = (DATA / "alvine_lot32_legal.txt").read_text(encoding="utf-8")
NEIGHBOUR_TEXT = (DATA / "alvine_lot30_legal.txt").read_text(encoding="utf-8")
STREET_BEARING = "South 22 degrees 26 minutes 12 seconds East"


def _builder():
    from acad_builder import AcadBuilder
    from llm_parser import load_config

    return AcadBuilder(load_config())


def _lot(name, text, *, main=False):
    from conflict_resolver import LotInput
    from llm_parser import parse_with_regex

    return LotInput(key=name, name=name, is_main=main, description=parse_with_regex(text)[0], source_text=text)


def _analyse(lots, **kwargs):
    from conflict_resolver import analyse_conflicts

    return analyse_conflicts(_builder(), lots, scale=16, **kwargs)


# ---------------------------------------------------------------- без окна


def test_local_parser_keeps_the_street_course_that_has_no_bearing():
    from llm_parser import parse_with_regex

    desc, _ = parse_with_regex(OUR_TEXT)
    # Раньше курс без румба терялся: оставалось три стороны и невязка 40 ft.
    assert [seg.has_bearing for seg in desc.segments] == [True, True, True, False]
    last = desc.segments[3]
    assert last.bearing_label == "S 0°00' E" and last.distance_ft == 40.0
    assert last.course_note == "southerly along the easterly line of Alvine Avenue"
    assert desc.front_segment_index == 3 and desc.main_street == "Alvine Avenue"
    # Привязка идёт по той же линии улицы и тоже известна только словом.
    tie = desc.corner_to_pob_bearing
    assert tie.has_bearing is False and tie.distance_ft == 200.0 and tie.azimuth_deg % 360 == 0
    # Из двух длин берётся первая (deed) — как и прежде.
    assert [seg.distance_ft for seg in desc.segments[:3]] == [174.89, 45.30, 153.64]

    plan = _builder().plan(desc, scale=16)
    assert plan.closure.distance == pytest.approx(15.570, abs=0.001)  # как на экране заказчика
    assert plan.pob == pytest.approx((0.0, 200.0))

    neighbour, _ = parse_with_regex(NEIGHBOUR_TEXT)
    assert all(seg.has_bearing for seg in neighbour.segments) and len(neighbour.segments) == 4
    assert (neighbour.block_number, neighbour.lot_number) == ("6323", "30")
    # Курс с румбом и словом «along» остаётся обычной стороной, а не дублируется.
    assert neighbour.segments[3].bearing_label == "S 22°26'12\" E"


def test_street_bearing_is_taken_from_the_neighbour_and_checked_by_geometry():
    from conflict_resolver import apply_changes

    ours, neighbour = _lot("our.pdf", OUR_TEXT, main=True), _lot("lot30.pdf", NEIGHBOUR_TEXT)
    analysis = _analyse([ours, neighbour])
    assert [p.kind for p in analysis.proposals] == ["street_bearing", "printed_alternate"]
    found = analysis.proposals[0]
    assert (found.kind, found.origin, found.verdict, found.lot_key) == ("street_bearing", "rule", "verified", "our.pdf")
    assert "S 22°26'12\" E" in found.title and "«lot30.pdf»" in found.title
    side, tie = found.changes
    assert (side.target, side.index, side.new_label) == ("side", 3, "S 22°26'12\" E")
    assert "в документе румба нет" in side.old_label
    # Привязка к углу лежит на той же линии улицы и поворачивается вместе с ней.
    assert (tie.target, tie.new_label) == ("tie", "N 22°26'12\" W")
    # Проверка геометрией: невязка и наложение на соседа.
    assert found.before.closure_ft == pytest.approx(15.570, abs=0.001)
    assert found.after.closure_ft < 0.01
    assert found.before.overlap_sqft > 1500 and found.after.overlap_sqft == 0
    # Откуда значение и независимая проверка по собственным сторонам участка.
    assert "сторона #4 — S 22°26'12\" E" in found.source
    reasons = " ".join(found.reasons)
    assert "S 22°26'38\" E, 40.004 ft (в документе 40.00 ft)" in reasons and "26″" in reasons
    assert "(0.00, 200.00) → (-76.33, 184.86)" in reasons

    # Находка — только предложение: исходные данные не тронуты.
    assert ours.description.segments[3].has_bearing is False
    fixed = apply_changes(ours.description, found.changes)
    assert fixed.segments[3].has_bearing and fixed.segments[3].bearing_label == "S 22°26'12\" E"
    assert fixed.corner_to_pob_bearing.bearing_label == "N 22°26'12\" W"
    assert ours.description.segments[3].bearing_label == "S 0°00' E"

    # Двойные длины программа сама не переключает: общая межа остаётся в остатке,
    # а второе напечатанное значение — только отдельной находкой (см. ниже).
    remaining = " ".join(analysis.remaining)
    assert "174.89'" in remaining and "176.92'" in remaining and analysis.remaining_hard
    assert all(change.field == "bearing" for change in found.changes)
    report = analysis.format_report()
    assert "[1] ✔" in report and "Данные участков НЕ изменены" in report and "После находок остаётся" in report

    # После принятия правки она больше не предлагается: в остатке только двойные
    # длины — их отдельная находка ждёт решения геодезиста.
    ours.fixes = (found,)
    again = _analyse([ours, neighbour])
    assert [p.kind for p in again.proposals] == ["printed_alternate"] and again.applied == [found]
    assert "Принятые правки" in again.format_report()


def test_without_a_neighbour_the_bearing_is_computed_from_closure_and_marked_as_computed():
    analysis = _analyse([_lot("our.pdf", OUR_TEXT, main=True)])
    assert [p.kind for p in analysis.proposals] == ["closing_course"]
    found = analysis.proposals[0]
    assert found.kind == "closing_course" and found.verified
    assert found.changes[0].new_label == "S 22°26'38\" E" and found.after.closure_ft < 0.01
    reasons = " ".join(found.reasons)
    assert "расчётное" in reasons and "расчёт" in found.source
    # Соседей в наборе нет — так и говорим, а не «в описаниях соседей не найден»
    # (заказчик, 15.11.2026: «он рассказал, что не в порядке, подсмотрев у соседей,
    # хотя соседей я не загружал»).
    assert "соседей в наборе нет" in reasons and "их описания не загружены" in reasons
    assert "в описаниях соседей румб этой линии не найден" not in reasons
    assert "не подтверждается замыканием" not in reasons

    # Румб соседа, который не сходится с нашим контуром, не выдаётся за подтверждённый.
    odd = NEIGHBOUR_TEXT.replace(STREET_BEARING, "South 10 degrees 26 minutes 12 seconds East")
    assert odd != NEIGHBOUR_TEXT
    analysis = _analyse([_lot("our.pdf", OUR_TEXT, main=True), _lot("lot30.pdf", odd)])
    assert [(p.kind, p.verdict) for p in analysis.proposals] == [
        ("closing_course", "verified"), ("printed_alternate", "partial"), ("street_bearing", "partial"),
    ]
    assert "не подтверждается замыканием" in " ".join(analysis.proposals[0].reasons)
    # Слово «southerly» не допускает румб поперёк улицы: такой сосед не используется.
    across = NEIGHBOUR_TEXT.replace(STREET_BEARING, "South 80 degrees 26 minutes 12 seconds East")
    analysis = _analyse([_lot("our.pdf", OUR_TEXT, main=True), _lot("lot30.pdf", across)])
    assert [p.kind for p in analysis.proposals] == ["closing_course"]


def test_the_gap_may_be_in_the_neighbour_and_the_value_in_our_lot():
    # Роли наоборот: румба нет у соседа, а в нашем описании он есть.
    analysis = _analyse([_lot("lot30.pdf", NEIGHBOUR_TEXT, main=True), _lot("lot32.pdf", OUR_TEXT)])
    assert [p.kind for p in analysis.proposals] == ["street_bearing", "printed_alternate"]
    found = analysis.proposals[0]
    assert found.lot_key == "lot32.pdf" and found.verified and "«lot30.pdf»" in found.title


def test_shared_boundary_value_is_offered_only_when_it_closes_the_lot():
    from llm_parser import ASPENWALL_SAMPLES

    main_text = ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"]
    left_text = ASPENWALL_SAMPLES["Aspenwall St — 365' (Сосед слева)"]
    # Опечатка в общей стене нашего участка: 100.00 → 108.00.
    typo = main_text.replace("RUNNING THENCE S 14°39'40\" E, 100.00 feet", "RUNNING THENCE S 14°39'40\" E, 108.00 feet")
    assert typo != main_text
    analysis = _analyse([_lot("main.pdf", typo, main=True), _lot("left.pdf", left_text)])
    assert len(analysis.proposals) == 1
    found = analysis.proposals[0]
    assert found.kind == "neighbor_value" and found.verified
    change = found.changes[0]
    assert (change.index, change.field, change.old_label, change.new_label) == (0, "distance", "108.00'", "100.00'")
    assert found.before.closure_ft == pytest.approx(8.0, abs=0.01) and found.after.closure_ft < 0.01
    assert "«left.pdf»: сторона #3" in found.source
    # Без ошибки находок нет, и участки не трогаются.
    clean = _analyse([_lot("main.pdf", main_text, main=True), _lot("left.pdf", left_text)])
    assert clean.proposals == [] and clean.remaining == [] and not clean.has_content


def test_judge_accepts_only_what_improves_the_geometry():
    from conflict_resolver import Metrics, judge

    bad = Metrics(15.57, 1536.4, 2, 0)
    assert judge(bad, Metrics(0.006, 0.0, 1, 0)) == "verified"
    assert judge(bad, Metrics(8.4, 75.0, 2, 0)) == "partial"
    assert judge(bad, Metrics(15.0, 1500.0, 2, 0)) == "rejected"  # почти ничего не изменилось
    assert judge(bad, None) == "rejected"  # участок с правкой не строится
    good = Metrics(0.006, 0.0, 1, 0)
    assert judge(good, Metrics(2.03, 0.0, 0, 0)) == "rejected"  # контур перестал замыкаться
    assert judge(good, Metrics(0.006, 300.0, 0, 0)) == "rejected"  # появилось наложение
    assert judge(good, Metrics(0.01, 0.0, 0, 0)) == "verified"  # ушла ошибка общей границы


def test_second_lengths_printed_in_the_deed_are_read_for_each_side():
    from conflict_resolver import printed_alternate_lengths
    from llm_parser import parse_with_regex

    desc, _ = parse_with_regex(OUR_TEXT)
    assert printed_alternate_lengths(desc, OUR_TEXT) == {
        0: (176.92, "tax map", "174.89 feet (deed) 176.92 feet (tax map)"),
        1: (45.42, "tax map", "45.30 feet (deed) 45.42 feet (tax map)"),
        2: (155.33, "tax map", "153.64 feet (deed) 155.33 feet (tax map)"),
    }
    # «198.83 feet (196.60 feet U.S. Standard)» — второе значение в скобках.
    neighbour, _ = parse_with_regex(NEIGHBOUR_TEXT)
    assert printed_alternate_lengths(neighbour, NEIGHBOUR_TEXT)[0] == (
        196.6, "U.S. Standard", "198.83 feet (196.60 feet U.S. Standard)",
    )
    # Без второго значения находки нет: длина одна, переключать нечего.
    plain = NEIGHBOUR_TEXT.replace(" (196.60 feet U.S. Standard)", "")
    assert 0 not in printed_alternate_lengths(neighbour, plain)


def test_tax_map_lengths_join_the_lot_with_the_neighbour_wall_to_wall():
    """Общая межа 174.89' против 176.92': берём tax map — участки стыкуются."""
    from acad_builder import verify_with_neighbors
    from conflict_resolver import apply_changes, fix_changes

    ours, neighbour = _lot("our.pdf", OUR_TEXT, main=True), _lot("lot30.pdf", NEIGHBOUR_TEXT)
    analysis = _analyse([ours, neighbour])
    doubles = analysis.proposals[1]
    assert (doubles.kind, doubles.verdict, doubles.lot_key) == ("printed_alternate", "partial", "our.pdf")
    # Меняются ровно те стороны, у которых напечатано второе значение.
    assert [(c.index, c.old_label, c.new_label) for c in doubles.changes] == [
        (0, "174.89'", "176.92'"), (1, "45.30'", "45.42'"), (2, "153.64'", "155.33'"),
    ]
    # Одна общая межа без остальных сторон контур не замыкает — так и написано.
    assert "одна только общая межа даёт невязку 2.025 ft" in " ".join(doubles.reasons)
    assert "у соседа в его описании невязка 0.609 ft" in " ".join(doubles.reasons)
    assert "deed и tax map" in " ".join(doubles.reasons)
    # Находка опирается на правку румба улицы: без неё POB стоит не на улице.
    assert doubles.requires == (analysis.proposals[0].id,)
    assert "в нашем описании" in doubles.source and "«LOT 30.pdf»" not in doubles.source
    assert "176.92 feet (tax map)" in doubles.source

    # Обе правки вместе: общая межа совпадает, участки встают стенка к стенке.
    builder = _builder()
    fixed = apply_changes(ours.description, fix_changes((analysis.proposals[0], doubles)))
    main_plan = builder.plan(fixed, scale=16)
    nb_plan = builder.plan(neighbour.description, scale=16, is_neighbor=True)
    assert main_plan.closure.distance == pytest.approx(0.300, abs=0.01)
    report = verify_with_neighbors(main_plan, [nb_plan])
    edge = report.pair_reports[0].shared_edges[0]
    assert edge.abs_length_diff_ft < 0.01 and edge.angle_diff_arcsec < 1.0
    assert edge.lateral_offset_start_ft < 0.01 and edge.lateral_offset_end_ft < 0.01
    assert edge.status == "ok"
    # Тонкая полоса вдоль межи — отвод гэпа в угол, а не наложение участков.
    pair = report.pair_reports[0]
    assert pair.overlap_area_sqft == 0 and report.overall_status == "ok"
    assert any("отвод открытого гэпа" in text for text in pair.findings)


def test_empty_tie_bearing_from_the_model_is_put_on_the_street_line():
    """Модель вернула N 0°00' E вместо null — POB уезжал с улицы на 61 ft."""
    from acad_builder import verify_with_neighbors
    from conflict_resolver import apply_changes
    from llm_parser import align_corner_tie_to_street, parse_with_regex

    street = _analyse([_lot("our.pdf", OUR_TEXT, main=True), _lot("lot30.pdf", NEIGHBOUR_TEXT)]).proposals[0]
    neighbour, _ = parse_with_regex(NEIGHBOUR_TEXT)
    empty = neighbour.segments[0].model_copy(update={
        "ns": "N", "degrees": 0, "minutes": 0, "seconds": 0.0, "ew": "E",
        "distance_ft": 160.0, "has_bearing": True, "bearing_label": None,
    })
    broken = neighbour.model_copy(update={"corner_to_pob_bearing": empty})
    fixed, note = align_corner_tie_to_street(broken)
    assert fixed.corner_to_pob_bearing.bearing_label == "N 22°26'12\" W"
    assert "не напечатан" in note and "S 22°26'12\" E" in note

    builder = _builder()
    # Наш участок — уже с принятой правкой румба улицы, как после «Применить».
    fixed_ours = apply_changes(_lot("our.pdf", OUR_TEXT, main=True).description, street.changes)
    ours = builder.plan(fixed_ours, scale=16)
    broken_plan = builder.plan(broken, scale=16, is_neighbor=True)
    assert broken_plan.pob == pytest.approx((0.0, 160.0))  # как было до правки
    report = verify_with_neighbors(ours, [broken_plan])
    assert "разнесены на 61.07'" in report.pair_reports[0].street_tie_check
    assert "у соседа румб привязки не напечатан" in report.pair_reports[0].street_tie_check
    # После правки привязка соседа стоит на линии улицы и участки стыкуются.
    good_plan = builder.plan(fixed, scale=16, is_neighbor=True)
    assert good_plan.pob == pytest.approx((-61.07, 147.89), abs=0.01)
    assert "единая улица" in verify_with_neighbors(ours, [good_plan]).pair_reports[0].street_tie_check


def test_printed_tie_bearing_is_left_alone():
    """POB может быть привязан курсом от дальнего перекрёстка — такой румб не трогаем."""
    from llm_parser import NYC_SAMPLES, align_corner_tie_to_street, parse_with_regex

    desc, warnings = parse_with_regex(NYC_SAMPLES["283 Jamie Lane (Staten Island)"])
    assert warnings == []
    assert desc.corner_to_pob_bearing.bearing_label == "S 4°43'12\" W"
    assert align_corner_tie_to_street(desc) == (desc, None)


@pytest.mark.parametrize(
    "text, label",
    [
        ("S 22°26'12\" E", "S 22°26'12\" E"),
        ("South 22 degrees 26 minutes 12 seconds East", "S 22°26'12\" E"),
        ("n67°33'48\"e", "N 67°33'48\" E"),
        ("N 0°00' E", "N 0°00' E"),
        ({"ns": "S", "degrees": 50, "minutes": 25, "seconds": 10, "ew": "W"}, "S 50°25'10\" W"),
        ("southerly", None),
        ("N 95°00'00\" E", None),
        ("", None),
    ],
)
def test_advisor_bearings_are_read_from_free_text(text, label):
    from conflict_resolver import parse_bearing_text
    from llm_parser import bearing_label

    parts = parse_bearing_text(text)
    assert (bearing_label(*parts) if parts else None) == label


def test_advisor_is_asked_only_about_what_python_left_and_its_advice_is_checked():
    from conflict_resolver import ADVISOR_SYSTEM_PROMPT

    calls = []

    def advisor(system, user_text, images):
        calls.append((system, user_text, images))
        return {
            "analysis": "Длины  даны в двух системах: deed и tax map.",
            "explanations": [{"conflict": "общая граница 174.89 / 176.92", "cause": "deed против tax map"}],
            "proposals": [
                {  # переключение на tax map ломает замыкание — отклоняется геометрией
                    "lot": "L1", "title": "Сторона 1 по tax map", "source_lot": "L2",
                    "changes": [{"target": "side", "side": 1, "bearing": None, "distance_ft": 176.92}],
                },
                {"lot": "L1", "title": "Нет такой стороны", "changes": [{"target": "side", "side": 9, "distance_ft": 5}]},
                {"lot": "L7", "title": "Нет такого участка", "changes": []},
                {  # повтор находки Python — не дублируется
                    "lot": "L1", "title": "Румб улицы",
                    "changes": [{"target": "side", "side": 4, "bearing": "S 22°26'12\" E"}],
                },
            ],
        }, None

    lots = [_lot("our.pdf", OUR_TEXT, main=True), _lot("lot30.pdf", NEIGHBOUR_TEXT)]
    cache = {}
    analysis = _analyse(lots, advisor=advisor, advisor_cache=cache)
    assert len(calls) == 1 and analysis.advisor_status == "ok"
    system, user_text, images = calls[0]
    assert system == ADVISOR_SYSTEM_PROMPT and images == []
    # Советник получает данные уже с находкой Python и только оставшиеся расхождения.
    assert '[L1] OUR LOT — file "our.pdf"' in user_text and "[L2] NEIGHBOUR LOT" in user_text
    assert "no bearing printed in this document; was S 0°00' E; taken by a verified finding" in user_text
    assert "PYTHON'S VERIFIED FINDINGS (already included above" in user_text
    assert "174.89 feet (deed) 176.92 feet (tax map)" in user_text  # текст документа
    assert "closure error 0.006 ft" in user_text and "15.570" in user_text
    assert "накладываются" not in user_text  # наложение Python уже снял
    # Правила для модели: не выдумывать числа и не менять систему длин, которую
    # геометрия отклонила; переход на второе напечатанное значение — только его.
    assert "Never invent numbers" in system and "Never switch a lot to an alternate value" in system

    assert [p.kind for p in analysis.proposals] == ["street_bearing", "printed_alternate"]
    rejected = " ".join(analysis.advisor_rejected)
    assert "Сторона 1 по tax map" in rejected and "невязка 0.006 → 2.025 ft" in rejected
    assert "стороны #9 у участка нет" in rejected and "не указан участок" in rejected
    report = analysis.format_report()
    assert "ИИ-СОВЕТНИК" in report and "Длины даны в двух системах" in report and "deed против tax map" in report

    # Тот же разбор: ответ берётся из памяти, платного запроса нет — даже после принятия правки.
    cache[analysis.advisor_key] = analysis.advisor_reply
    lots[0].fixes = (analysis.proposals[0],)
    again = _analyse(lots, advisor=advisor, advisor_cache=cache)
    assert len(calls) == 1 and again.advisor_status == "cached"
    assert "получено ранее" in again.format_report()
    # По просьбе пользователя вопрос отправляется заново.
    _analyse(lots, advisor=advisor, advisor_cache=cache, force_advisor=True)
    assert len(calls) == 2


def test_advisor_finding_becomes_a_proposal_only_after_the_geometry_check():
    # В нашем описании неверно прочитана буква: «east» вместо «west» у стороны 2.
    misread = OUR_TEXT.replace(
        "north 50 degrees 25 minutes 10 seconds west", "north 50 degrees 25 minutes 10 seconds east",
    )
    assert misread != OUR_TEXT

    def advisor(_system, _user_text, _images):
        return {
            "analysis": "В стороне 2 перепутана буква.",
            "proposals": [{
                "lot": "L1", "title": "Сторона 2: N 50°25'10\" W", "reason": "у соседа задняя линия идёт на запад",
                "source_lot": "L2", "source_quote": "North 50 degrees 25 minutes 10 seconds West",
                "changes": [{"target": "side", "side": 2, "bearing": "North 50 degrees 25 minutes 10 seconds West"}],
            }],
        }, None

    lots = [_lot("our.pdf", misread, main=True), _lot("lot30.pdf", NEIGHBOUR_TEXT)]
    # Правила Python здесь бессильны: сторона не общая с соседом, а курсов с пробелом два вида.
    assert _analyse(lots).proposals == []
    analysis = _analyse(lots, advisor=advisor)
    assert len(analysis.proposals) == 1
    found = analysis.proposals[0]
    assert (found.origin, found.kind, found.verdict) == ("ai", "advisor", "partial")
    assert found.before.closure_ft == pytest.approx(54.639, abs=0.01)
    assert found.after.closure_ft == pytest.approx(15.570, abs=0.01)
    assert "цитата: «North 50 degrees 25 minutes 10 seconds West»" in found.source
    assert "есть в разобранных данных — «lot30.pdf», сторона #2" in " ".join(found.reasons)
    # После этой правки остаётся знакомый пробел, который закрывает правило Python.
    lots[0].fixes = (found,)
    after = _analyse(lots)
    assert [(p.kind, p.verified) for p in after.proposals] == [
        ("street_bearing", True), ("printed_alternate", False),
    ]

    # Совет без опоры в данных участков помечается для сверки с документом.
    def inventing(_system, _user_text, _images):
        return {"proposals": [{
            "lot": "L1", "title": "Сторона 2", "changes": [{"target": "side", "side": 2, "bearing": "N 50°25'12\" W"}],
        }]}, None

    lots[0].fixes = ()
    invented = _analyse(lots, advisor=inventing).proposals[0]
    assert "нет в разобранных данных участков" in " ".join(invented.reasons)


def test_advisor_failure_or_absence_does_not_break_the_analysis():
    lots = [_lot("our.pdf", OUR_TEXT, main=True), _lot("lot30.pdf", NEIGHBOUR_TEXT)]

    def failing(*_args):
        raise RuntimeError("timeout")

    analysis = _analyse(lots, advisor=failing)
    assert analysis.advisor_status == "error" and len(analysis.proposals) == 2
    assert "ИИ-советник: запрос не удался — timeout" in analysis.format_report()
    analysis = _analyse(lots, advisor=None, advisor_note="не запрошен — не задан API-ключ")
    assert analysis.advisor_status == "skipped" and "не задан API-ключ" in analysis.format_report()
    # Страницы сканов уходят советнику, пока укладываются в лимит изображений.
    seen = []
    lots[0].images = ("data:image/png;base64,AAA", "data:image/png;base64,BBB")
    lots[1].images = ("data:image/png;base64,CCC",)
    _analyse(lots, advisor=lambda s, u, i: (seen.append((u, i)) or ({}, None)), max_images=2)
    user_text, images = seen[0]
    assert images == ["data:image/png;base64,AAA", "data:image/png;base64,BBB"]
    assert "document pages: attached images 1–2" in user_text
    assert "to a point or the easterly side of Alvine Avenue" in user_text  # сосед — текстом


def test_a_cut_off_advisor_answer_is_reported_and_python_findings_stay():
    from llm_parser import LLMParseError

    lots = [_lot("our.pdf", OUR_TEXT, main=True), _lot("lot30.pdf", NEIGHBOUR_TEXT)]

    def cut_off(*_args):
        raise LLMParseError(
            "ИИ отвечает криво: за 3 попытки целого JSON не пришло "
            "(json_object: ответ оборвался (finish_reason=length, 812 символов))"
        )

    analysis = _analyse(lots, advisor=cut_off)
    assert analysis.advisor_status == "error"
    report = analysis.format_report()
    # Сказано прямо, без двойного «запрос не удался», и находки Python на месте.
    assert "ИИ-советник: отвечает криво: за 3 попытки целого JSON не пришло" in report
    assert "запрос не удался" not in report
    assert "Находки Python от советника не зависят" in report
    assert [p.kind for p in analysis.proposals] == ["street_bearing", "printed_alternate"]


def test_json_question_to_the_model_reads_wrapped_answers(monkeypatch):
    import openai

    import llm_parser

    seen = []

    class _Completions:
        def create(self, **kwargs):
            seen.append(kwargs)
            if "response_format" in kwargs:
                raise RuntimeError("response_format is not supported")
            message = type("M", (), {"content": '<think>…</think>Вот ответ:\n```json\n{"analysis": "ok", "proposals": []}\n```'})
            usage = type("U", (), {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
            return type("C", (), {"choices": [type("Ch", (), {"message": message})], "usage": usage})

    class _Client:
        def __init__(self, **_kwargs):
            self.chat = type("Chat", (), {"completions": _Completions()})()

    monkeypatch.setattr(openai, "OpenAI", lambda *a, **kw: _Client(**kw))
    cfg = llm_parser.load_config()
    data, usage = llm_parser.ask_llm_json(
        "system", "question", cfg, image_data_urls=["data:image/png;base64,AAA"], api_key="test-key",
    )
    assert data == {"analysis": "ok", "proposals": []}
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (10, 5, 15)
    # Сначала JSON-режим, затем обычный запрос для моделей, которые его не принимают.
    assert [("response_format" in call) for call in seen] == [True, False]
    blocks = seen[-1]["messages"][1]["content"]
    assert blocks[0] == {"type": "text", "text": "question"} and blocks[1]["type"] == "image_url"
    assert seen[-1]["messages"][0] == {"role": "system", "content": "system"}


def _fake_json_client(monkeypatch, replies):
    """Подставной клиент OpenAI: отдаёт заданные ответы по порядку и запоминает запросы."""
    import openai

    calls = []

    class _Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            content, finish = replies[min(len(calls) - 1, len(replies) - 1)]
            message = type("M", (), {"content": content})
            choice = type("Ch", (), {"message": message, "finish_reason": finish})
            usage = type("U", (), {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
            return type("C", (), {"choices": [choice], "usage": usage})

    class _Client:
        def __init__(self, **_kwargs):
            self.chat = type("Chat", (), {"completions": _Completions()})()

    monkeypatch.setattr(openai, "OpenAI", lambda *a, **kw: _Client(**kw))
    return calls


def test_a_cut_off_advisor_reply_is_asked_again_and_the_retry_is_shorter(monkeypatch):
    import llm_parser

    calls = _fake_json_client(monkeypatch, [
        ('{"analysis": "Сосед построен по tax map, у нас deed — поэт', "length"),
        ('{"analysis": "ok", "proposals": []}', "stop"),
    ])
    cfg = llm_parser.load_config()
    seen: list[str] = []
    data, usage = llm_parser.ask_llm_json(
        "system", "question", cfg, api_key="test-key",
        progress=seen.append, task_label="ИИ-советник",
    )
    assert data == {"analysis": "ok", "proposals": []}
    assert usage.total_tokens == 30  # оба запроса посчитаны, деньги не теряются
    # Первый ответ оборвался — повторяем с просьбой ответить короче и с запасом длины.
    assert len(calls) == 2
    assert calls[0]["messages"][1]["content"] == "question"
    assert "previous answer was cut off" in calls[1]["messages"][1]["content"]
    assert calls[1]["max_tokens"] == llm_parser.JSON_RETRY_MAX_TOKENS
    assert "ответ оборвался на попытке 1 из 3" in " ".join(seen)


def test_three_cut_off_replies_say_plainly_that_the_ai_answers_badly(monkeypatch):
    import llm_parser

    calls = _fake_json_client(monkeypatch, [('{"analysis": "начал и не', "length")])
    cfg = llm_parser.load_config()
    usages = []
    with pytest.raises(llm_parser.LLMParseError) as excinfo:
        llm_parser.ask_llm_json(
            "system", "question", cfg, api_key="test-key",
            attempts=3, on_usage=usages.append,
        )
    message = str(excinfo.value)
    assert message.startswith("ИИ отвечает криво: за 3 попытки целого JSON не пришло")
    assert "finish_reason=length" in message
    assert len(calls) == 3  # ровно три попытки, не больше
    assert len(usages) == 3  # потраченные токены записаны по каждой попытке


def test_a_reply_that_ends_mid_json_is_not_taken_for_an_answer():
    import llm_parser

    assert llm_parser._reply_looks_cut('{"analysis": "всё хорошо", "proposals": [')
    assert llm_parser._reply_looks_cut("```json\n{\"analysis\": \"обрыв")
    assert not llm_parser._reply_looks_cut('{"analysis": "ok", "proposals": []}')
    assert not llm_parser._reply_looks_cut('Вот ответ:\n```json\n{"analysis": "ok"}\n```')


# ---------------------------------------------------------------- окно


def _alvine_set(tmp_path):
    ours = tmp_path / "OUR LOT 32.pdf"
    neighbour = tmp_path / "LOT 30.pdf"
    _write_test_pdf(ours, [OUR_TEXT])
    _write_test_pdf(neighbour, [NEIGHBOUR_TEXT])
    return ours, neighbour


def _open_set(app, monkeypatch, *paths):
    from tkinter import filedialog

    monkeypatch.setattr(filedialog, "askopenfilenames", lambda **_: tuple(str(p) for p in paths))
    app._load_pdf()
    dialog = app._pdf_set_dialog
    assert dialog is not None, app.status_var.get()
    return dialog


def test_set_report_offers_the_neighbour_bearing_and_changes_the_lot_only_on_request(gui, tmp_path, monkeypatch):
    from acad_builder import DrawResult
    from tkinter import messagebox

    ours, neighbour = _alvine_set(tmp_path)
    app = gui.SurveyApp()
    monkeypatch.setattr(gui, "ask_llm_json", lambda *a, **k: pytest.fail("платный запрос без ИИ в наборе"))
    parses = []
    real = gui.parse_legal_description_full
    monkeypatch.setattr(gui, "parse_legal_description_full", lambda **kw: parses.append(kw) or real(**kw))

    dialog = _open_set(app, monkeypatch, ours, neighbour)
    assert [row["mode_var"].get() for row in dialog.rows] == ["Локально", "Локально"]
    dialog.rows[0]["radio"].select()
    dialog.submit()
    _wait(app)

    # Участок построен как в документе: находка только предложена.
    main_plan = app._preview_main_plan
    assert main_plan.closure.distance == pytest.approx(15.570, abs=0.001)
    assert main_plan.pob == pytest.approx((0.0, 200.0))
    analysis = app._pdf_set_analysis
    assert [p.kind for p in analysis.proposals] == ["street_bearing", "printed_alternate"]
    found = analysis.proposals[0]
    doubles = analysis.proposals[1]
    assert found.verified and not doubles.verified
    win = app._pdf_set_report_window
    report = win._report_text
    assert "АНАЛИЗ КОНФЛИКТОВ: ЧТО МОЖНО ВЗЯТЬ ИЗ ОПИСАНИЙ СОСЕДЕЙ" in report
    assert "[1] ✔ Сторона #4" in report and "румб S 22°26'12\" E — из описания «LOT 30.pdf»" in report
    assert "невязка 15.570 → 0.006 ft" in report and "1,536.4 → 0.0 sq ft" in report
    assert "[2] ◐ Длины по tax map: #1 174.89' → 176.92'" in report
    assert "общая межа сходится с «LOT 30.pdf»" in report
    assert "ИИ-советник: не запрошен — платный вопрос задаётся только кнопкой" in report
    assert "Находок по данным соседей: 2 (подтверждено геометрией: 1)" in win._report_headline
    assert "находок по данным соседей: 2" in app.status_var.get()
    assert list(win._fix_buttons) == [found.id, doubles.id] and win._revert_buttons == {}
    assert win._fix_buttons[found.id].cget("text") == "Применить" and win._apply_all_button is None
    assert len(parses) == 2

    # «Применить»: участок перестроен с румбом соседа, файлы заново не разбираются.
    win._fix_buttons[found.id].cget("command")()
    _wait(app)
    assert len(parses) == 2
    main_plan = app._preview_main_plan
    assert main_plan.closure.distance < 0.01
    assert main_plan.pob == pytest.approx((-76.33, 184.86), abs=0.01)
    side = main_plan.description.segments[3]
    assert side.has_bearing and side.bearing_label == "S 22°26'12\" E"
    assert main_plan.description.corner_to_pob_bearing.bearing_label == "N 22°26'12\" W"
    assert any("Принята правка по данным соседей" in note for note in main_plan.notes)
    item = next(entry for entry in app.pdf_set if entry.is_main)
    assert [fix.id for fix in item.fixes] == [found.id]
    assert item.outcome.data.segments[3].has_bearing is False  # результат разбора не изменён
    pair = app._neighbor_report.pair_reports[0]
    assert pair.overlap_area_sqft == 0
    win = app._pdf_set_report_window
    report = win._report_text
    assert "✎ принята правка по данным соседей: Сторона #4" in report
    assert "Принятые правки (участки построены с ними; каждую можно вернуть)" in report
    assert "[1] ✔" not in report and "[1] ◐ Длины по tax map" in report
    assert "После находок остаётся" in report
    # Двойные длины остаются отдельной находкой: она ждёт решения геодезиста.
    assert list(win._fix_buttons) == [doubles.id] and list(win._revert_buttons) == [found.id]
    assert "Принята правка по данным соседей" in app.log_text()

    # В AutoCAD уходит рассчитанная схема — уже с принятой правкой.
    drawn = []
    monkeypatch.setattr(
        gui.AcadBuilder, "draw_plan",
        lambda _b, plan: drawn.append(plan) or DrawResult(plan=plan, text_count=0, layers={}),
    )
    monkeypatch.setattr(gui.sys, "platform", "win32")
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **kw: True)
    app._start(parse_only=False)
    _wait(app)
    assert drawn and drawn[0] is app._preview_main_plan
    assert drawn[0].description.segments[3].bearing_label == "S 22°26'12\" E"

    # «Вернуть как в документе»: участок снова строится по документу, находка предложена заново.
    win._revert_buttons[found.id].cget("command")()
    _wait(app)
    assert len(parses) == 2 and item.fixes == []
    assert app._preview_main_plan.closure.distance == pytest.approx(15.570, abs=0.001)
    assert list(app._pdf_set_report_window._fix_buttons) == [found.id, doubles.id]
    assert "Правка снята" in app.log_text()

    # Новый разбор файла (другие страницы или способ) сбрасывает правки этого файла.
    app._pdf_set_report_window._fix_buttons[found.id].cget("command")()
    _wait(app)
    assert [fix.id for fix in item.fixes] == [found.id]
    app._start_pdf_set(force=True)
    _wait(app)
    assert len(parses) == 4 and item.fixes == []
    assert app._preview_main_plan.closure.distance == pytest.approx(15.570, abs=0.001)
    app._on_close()


def test_advisor_is_asked_only_after_the_button_is_confirmed(gui, tmp_path, monkeypatch):
    from tkinter import messagebox

    from llm_parser import ParseOutcome, TokenUsage, parse_with_regex

    ours = tmp_path / "our scan.pdf"
    neighbour = tmp_path / "LOT 30.pdf"
    _write_test_pdf(ours, [None, None])  # скан без текста — разбирает ИИ
    _write_test_pdf(neighbour, [NEIGHBOUR_TEXT])
    our_desc, _ = parse_with_regex(OUR_TEXT)
    app = gui.SurveyApp()
    app.select_passes(1)
    real = gui.parse_legal_description_full

    def fake_parse(**kwargs):
        if kwargs["use_llm"]:
            return ParseOutcome(
                data=our_desc, source="llm-vision",
                prepared_images=["data:image/png;base64,PAGE1", "data:image/png;base64,PAGE2"],
            )
        return real(**kwargs)

    asked = []

    def fake_ask(system, user_text, cfg, **kwargs):
        asked.append((user_text, kwargs))
        usage = TokenUsage(prompt_tokens=900, completion_tokens=100, total_tokens=1000, model="advisor-model")
        return {
            "analysis": "Наш участок построен по длинам deed, сосед — по своей записи; это не ошибка.",
            "explanations": [{"conflict": "174.89 и 176.92", "cause": "deed и tax map"}],
            "proposals": [],
        }, usage

    monkeypatch.setattr(gui, "parse_legal_description_full", fake_parse)
    monkeypatch.setattr(gui, "ask_llm_json", fake_ask)
    monkeypatch.setattr(gui, "resolve_api_target", lambda *a, **k: {"api_key": "test-key"})

    dialog = _open_set(app, monkeypatch, ours, neighbour)
    assert [row["mode_var"].get() for row in dialog.rows] == ["ИИ", "Локально"]
    dialog.rows[0]["radio"].select()
    dialog.submit()
    _wait(app)

    # Даже набор, разобранный через ИИ, сам платный вопрос не задаёт: сначала
    # разрешение пользователя.
    assert asked == []
    analysis = app._pdf_set_analysis
    assert analysis.advisor_status == "skipped"
    report = app._pdf_set_report_window._report_text
    assert "не запрошен — платный вопрос задаётся только кнопкой" in report
    assert len(analysis.proposals) == 1  # находка Python от советника не зависит

    button = app._pdf_set_report_window._advisor_button
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **kw: False)
    button.cget("command")()
    assert not app._busy and asked == []  # отказались — запроса нет

    # Согласились — один платный запрос, советник виден в итоге и в журнале.
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **kw: True)
    button.cget("command")()
    _wait(app)
    assert len(asked) == 1
    user_text, kwargs = asked[0]
    assert kwargs["image_data_urls"] == ["data:image/png;base64,PAGE1", "data:image/png;base64,PAGE2"]
    assert kwargs["task_label"] == "ИИ-советник"
    assert "document pages: attached images 1–2" in user_text and "176.92" in user_text
    analysis = app._pdf_set_analysis
    assert analysis.advisor_status == "ok" and len(analysis.proposals) == 1
    report = app._pdf_set_report_window._report_text
    assert "ИИ-СОВЕТНИК (advisor-model):" in report and "это не ошибка" in report and "deed и tax map" in report
    assert "advisor-model" in app.log_text()  # токены советника учтены в журнале

    # Принятие правки перестраивает участок, но советника заново не спрашивает.
    found = analysis.proposals[0]
    app._pdf_set_report_window._fix_buttons[found.id].cget("command")()
    _wait(app)
    assert len(asked) == 1 and app._preview_main_plan.closure.distance < 0.01
    assert app._pdf_set_analysis.advisor_status == "cached"
    assert "получено ранее для этого же разбора" in app._pdf_set_report_window._report_text
    app._on_close()


def test_advisor_asks_nothing_without_a_key_or_when_switched_off(gui, tmp_path, monkeypatch):
    from tkinter import messagebox

    from llm_parser import ParseOutcome, parse_with_regex

    ours = tmp_path / "our scan.pdf"
    neighbour = tmp_path / "LOT 30.pdf"
    _write_test_pdf(ours, [None])
    _write_test_pdf(neighbour, [NEIGHBOUR_TEXT])
    our_desc, _ = parse_with_regex(OUR_TEXT)
    app = gui.SurveyApp()
    app.select_passes(1)
    real = gui.parse_legal_description_full
    monkeypatch.setattr(
        gui, "parse_legal_description_full",
        lambda **kw: ParseOutcome(data=our_desc, source="llm-vision") if kw["use_llm"] else real(**kw),
    )
    monkeypatch.setattr(gui, "ask_llm_json", lambda *a, **k: pytest.fail("советник без ключа"))
    monkeypatch.setattr(gui, "resolve_api_target", lambda *a, **k: {"api_key": ""})
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: True)  # согласие есть, ключа нет
    dialog = _open_set(app, monkeypatch, ours, neighbour)
    dialog.rows[0]["radio"].select()
    dialog.submit()
    _wait(app)
    app._pdf_set_report_window._advisor_button.cget("command")()
    _wait(app)
    assert app._pdf_set_analysis.advisor_status == "skipped"
    assert "ИИ-советник: не запрошен — не задан API-ключ" in app._pdf_set_report_window._report_text
    assert len(app._pdf_set_analysis.proposals) == 1  # находка Python от советника не зависит

    # Выключатель в настройках действует и на автоматический вопрос (auto = true).
    monkeypatch.setattr(gui, "resolve_api_target", lambda *a, **k: {"api_key": "test-key"})
    real_config = app._build_runtime_config

    def switched_off():
        cfg, *rest = real_config()
        cfg["api"]["conflict_advisor"] = {"enabled": False, "auto": True}
        return (cfg, *rest)

    monkeypatch.setattr(app, "_build_runtime_config", switched_off)
    app._start_pdf_set()
    _wait(app)
    assert "ИИ-советник: выключен в настройках" in app._pdf_set_report_window._report_text
    assert "ИИ-советник: не запрошен — не задан API-ключ" not in app._pdf_set_report_window._report_text
    app._on_close()



# ------------------------------------------- привязка к углу улицы (LOT 68 / OUR 70)

STAFFORD_68 = (DATA / "stafford_lot68_legal.txt").read_text(encoding="utf-8")
STAFFORD_70 = (DATA / "stafford_lot70_legal.txt").read_text(encoding="utf-8")
STAFFORD_TIE_70 = (
    "distant 201.00 feet southwesterly from the corner formed by the intersection of the "
    "northwesterly side of Stafford Avenue and the southwesterly side of Carlton Boulevard"
)


def test_advisor_can_propose_the_distance_from_the_corner_and_the_gap_closes():

    # OCR потерял расстояние до угла у нашего участка: участок построен от угла улицы.
    lost_tie = STAFFORD_70.replace(STAFFORD_TIE_70, "at a point")
    assert lost_tie != STAFFORD_70
    assert _lot("our70", lost_tie, main=True).description.distance_to_corner_ft == 0.0

    def advisor(_system, _user_text, _images):
        return {
            "analysis": "У участка L1 нет привязки к углу улицы.",
            "proposals": [{
                "lot": "L1", "title": "Привязка L1: 201.00 ft от угла",
                "reason": "сосед L2 стоит на 161 ft, наш фасад начинается после него",
                "source_lot": None, "source_quote": None,
                "changes": [{"target": "tie", "bearing": "S 33°23'48\" W", "distance_ft": 201.0}],
            }],
        }, None

    lots = [_lot("our70.pdf", lost_tie, main=True), _lot("lot68.pdf", STAFFORD_68)]
    # Совет нужен явно: остаток здесь мягкий (расхождение румба), без force он не запрашивается.
    before = _analyse(lots, advisor=advisor, force_advisor=True)
    assert len(before.proposals) == 1
    found = before.proposals[0]
    assert (found.origin, found.kind, found.verdict) == ("ai", "advisor", "partial")
    assert any("201" in c.describe() for c in found.changes), [c.describe() for c in found.changes]
    assert found.before.overlap_sqft == pytest.approx(0.0, abs=0.5)
    assert found.after.overlap_sqft == pytest.approx(0.0, abs=0.5)
    # Разрыв до соседа уменьшился: это и есть основание принять совет.
    assert found.after.gap_ft < found.before.gap_ft - 1.0

    # Принятая правка действительно ставит расстояние и румб, а разрыв исчезает.
    lots[0].fixes = (found,)
    after = _analyse(lots)
    assert not any("не имеет общей границы" in line for line in after.remaining), after.remaining
    assert not any("минимальное расстояние" in line for line in after.remaining), after.remaining


def test_distance_fix_is_applied_to_the_tie_not_to_a_side():
    from conflict_resolver import Change, apply_changes

    lost_tie = STAFFORD_70.replace(STAFFORD_TIE_70, "at a point")
    model = _lot("our70", lost_tie, main=True).description
    change = Change(target="tie", index=-1, field="distance", distance_ft=201.0)
    fixed = apply_changes(model, [change])
    assert fixed.distance_to_corner_ft == pytest.approx(201.0)
    # Стороны участка не трогаются, исходное описание тоже.
    assert [s.bearing_label for s in fixed.segments] == [s.bearing_label for s in model.segments]
    assert model.distance_to_corner_ft == 0.0


def test_ten_arcseconds_on_a_short_line_is_a_note_not_a_conflict():
    """Регресс: 10″ на стороне 40 ft (0.002 ft) и на линии улицы 161 ft (0.008 ft) — пояснение, не ошибка."""
    lots = [_lot("our70.pdf", STAFFORD_70, main=True), _lot("lot68.pdf", STAFFORD_68)]
    analysis = _analyse(lots)
    bend_or_tie = [line for line in analysis.remaining if "Излом" in line or "Румб" in line or "расходятся" in line]
    # Если такие строки и остались в списке, они обязаны быть пояснениями «незначимо», а не ошибками.
    assert all("незначимо" in line for line in bend_or_tie), bend_or_tie
    assert not any("расходятся уже на линии улицы" in line for line in analysis.remaining), analysis.remaining
    assert not any("не имеет общей границы" in line for line in analysis.remaining), analysis.remaining
    assert not analysis.remaining_hard
