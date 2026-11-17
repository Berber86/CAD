"""Замечания к файлу набора берутся из проверки описания: «без замечаний» только при статусе ok."""

from types import SimpleNamespace

from test_gui_smoke import gui  # noqa: F401 — фикстура окна с подменёнными модулями

from description_check import STATUS_FAILED, STATUS_OK, STATUS_WARNING, CheckIssue, DescriptionCheck, FigureScore


class _Stub:
    """Минимум окна, нужный методу замечаний: участок построен, геометрия без невязки."""

    def _pdf_set_plan_for(self, item):
        return SimpleNamespace(closure=SimpleNamespace(distance=0.0), suspicion=object())

    def _plan_diagnostic_items(self, plan):
        return []


def _item(check):
    return SimpleNamespace(outcome=SimpleNamespace(check=check))


def test_clean_check_gives_no_remarks(gui):  # noqa: F811
    check = DescriptionCheck(status=STATUS_OK, score=FigureScore(), closure_ft=0.0)
    assert gui.SurveyApp._pdf_set_issue_lines(_Stub(), _item(check)) == []


def test_missing_corner_distance_is_a_warning_not_clean(gui):  # noqa: F811
    score = FigureScore(missing=["расстояние от угла до точки начала"])
    check = DescriptionCheck(status=STATUS_WARNING, score=score, closure_ft=0.0)
    lines = gui.SurveyApp._pdf_set_issue_lines(_Stub(), _item(check))
    assert lines == [("warn", "не найдено: расстояние от угла до точки начала")]


def test_failed_check_is_an_error_and_closure_is_not_duplicated(gui):  # noqa: F811
    score = FigureScore(missing=["контур не замыкается: невязка 3.00 ft", "румб стороны №2"])
    issue = CheckIssue(status=STATUS_FAILED, text="румб стороны №2 не прочитан", side=2)
    check = DescriptionCheck(status=STATUS_FAILED, issues=[issue], score=score, closure_ft=3.0)
    lines = gui.SurveyApp._pdf_set_issue_lines(_Stub(), _item(check))
    assert ("error", "румб стороны №2 не прочитан") in lines
    assert ("error", "не найдено: румб стороны №2") in lines
    assert not any("контур не замыкается" in text for _sev, text in lines)
