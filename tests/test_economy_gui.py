"""Окно: переключатель «Локально / Эконом / ИИ» и окно набора с третьим вариантом."""

from test_gui_smoke import gui  # noqa: F401 — фикстура окна с подменёнными модулями


def test_run_mode_switch_selects_all_three_modes(gui) -> None:  # noqa: F811
    app = gui.SurveyApp()
    # По умолчанию — ИИ, переключатель показывает его и доступен.
    assert app.run_mode_seg.get() == "ИИ"

    app._on_run_mode_clicked("Локально")
    assert app.use_llm_var.get() is False and app.economy_var.get() is False
    assert "бесплатно" in app.parse_button.cget("text")

    app._on_run_mode_clicked("Эконом")
    assert app.use_llm_var.get() is False and app.economy_var.get() is True
    assert "экономно" in app.parse_button.cget("text")
    assert app.run_mode_seg.get() == "Эконом"

    app._on_run_mode_clicked("ИИ")
    assert app.use_llm_var.get() is True and app.economy_var.get() is False
    assert "экономно" not in app.parse_button.cget("text")


def test_pdf_set_dialog_offers_economy_mode(gui) -> None:  # noqa: F811
    dialog_cls = gui.PdfSetDialog
    assert dialog_cls.MODE_ECONOMY == "Эконом"
    from pdf_set import PdfSetItem  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    item = PdfSetItem(path=Path("/tmp/x.pdf"), page_texts=["text"], first_page=1, last_page=1)
    assert dialog_cls._mode_value(dialog_cls, item) == dialog_cls.MODE_LOCAL
    item.set_mode(False, economy=True)
    assert dialog_cls._mode_value(dialog_cls, item) == dialog_cls.MODE_ECONOMY
    item.set_mode(True)
    assert dialog_cls._mode_value(dialog_cls, item) == dialog_cls.MODE_LLM
