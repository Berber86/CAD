"""Кнопка «Эконом» в окне: переключатель видим при локальном разборе и меняет основную кнопку."""

from test_gui_smoke import gui  # noqa: F401 — фикстура окна с подменёнными модулями


def test_economy_checkbox_changes_primary_action(gui) -> None:  # noqa: F811
    app = gui.SurveyApp()
    app.use_llm_var.set(False)
    app.economy_var.set(False)
    app._refresh_parse_actions()
    assert "бесплатно" in app.parse_button.cget("text")

    app.economy_var.set(True)
    app._on_economy_toggled()
    assert "экономно" in app.parse_button.cget("text")
    assert app.economy_check.cget("state") == "normal"

    # При ИИ-разборе эконом-переключатель не нужен и выключен.
    app.use_llm_var.set(True)
    app._refresh_parse_actions()
    assert app.economy_check.cget("state") == "disabled"
    assert "экономно" not in app.parse_button.cget("text")
