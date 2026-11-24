"""Секретное окно разработчика: тест моделей на построение property line.

Вызывается только тройным щелчком по схеме участка в главном окне; ни
кнопок, ни пунктов меню у него нет. Логика прогона — model_test.py.
"""
from __future__ import annotations

import math as _math
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog
from typing import Any

import local_settings
import llm_parser as lp
import model_test as mt
from acad_builder import AcadBuilder

NO_REASONING = "—"


class ModelTestWindow:
    def __init__(self, app: Any, ctk: Any, theme: dict[str, str]) -> None:
        self.app, self.ctk, self.theme = app, ctk, theme
        self._closed = False
        self.running = False
        self.events: queue.Queue = queue.Queue()
        self.file_path: Path | None = None
        self.results: list[mt.SlotResult | None] = [None] * mt.SLOT_COUNT
        self.selected = 0
        self.ratings = mt.RatingsBook()
        self._rating_entries: dict[int, dict[str, Any]] = {}

        cfg, *_ = app._build_runtime_config()
        self.slots = mt.load_slots(cfg)
        saved_file = str(((cfg.get("model_test") or {}).get("file") or "")).strip()
        if saved_file and Path(saved_file).exists():
            self.file_path = Path(saved_file)

        win = ctk.CTkToplevel(app)
        self.window = win
        win.title("Тест моделей · property line")
        win.geometry("1080x760")
        win.minsize(760, 560)
        win.configure(fg_color=theme["bg_root"])
        try:
            win.attributes("-topmost", bool(app.topmost_var.get()))
        except Exception:  # noqa: BLE001
            pass
        app._attach_child_window(win)
        win.protocol("WM_DELETE_WINDOW", self.close)
        win.grid_columnconfigure(0, weight=1)
        win.grid_rowconfigure(3, weight=1)

        file_row = ctk.CTkFrame(win, fg_color="transparent")
        file_row.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 4))
        file_row.grid_columnconfigure(1, weight=1)
        ctk.CTkButton(file_row, text="📄 Файл…", width=110,
                      command=self._pick_file).grid(row=0, column=0, sticky="w")
        self.file_label = ctk.CTkLabel(
            file_row, text=self.file_path.name if self.file_path else "файл не выбран",
            anchor="w")
        self.file_label.grid(row=0, column=1, sticky="ew", padx=(10, 8))
        self.run_button = ctk.CTkButton(file_row, text="▶ Тест (4 платных запроса)",
                                        width=220, command=self._start_run)
        self.run_button.grid(row=0, column=2, sticky="e")
        ctk.CTkButton(file_row, text="📊 Статистика", width=130,
                      command=self._open_stats).grid(row=0, column=3, sticky="e", padx=(8, 0))

        slots_frame = ctk.CTkFrame(win, fg_color="transparent")
        slots_frame.grid(row=1, column=0, sticky="ew", padx=14, pady=(4, 2))
        slots_frame.grid_columnconfigure(0, weight=1)
        self.slot_rows: list[dict[str, Any]] = []
        for index in range(mt.SLOT_COUNT):
            self.slot_rows.append(self._build_slot_row(slots_frame, index))

        self.metrics_label = ctk.CTkLabel(win, text="", anchor="w", justify="left")
        self.metrics_label.grid(row=2, column=0, sticky="ew", padx=14, pady=(6, 2))

        body = ctk.CTkFrame(win, fg_color="transparent")
        body.grid(row=3, column=0, sticky="nsew", padx=14, pady=(2, 8))
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(body, bg="#0f172a", highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", lambda _ev: self._draw_selected())

        self.log = ctk.CTkTextbox(win, height=110, wrap="word")
        self.log.grid(row=4, column=0, sticky="ew", padx=14, pady=(0, 12))
        self._log_line("4 слота получают один и тот же файл картинками; запуск параллельный.")
        self._drain()

    # ------------------------------------------------------------------ слоты
    def _build_slot_row(self, parent: Any, index: int) -> dict[str, Any]:
        ctk, app = self.ctk, self.app
        slot = self.slots[index]
        row = ctk.CTkFrame(parent)
        row.grid(row=index, column=0, sticky="ew", pady=(0, 6))
        row.grid_columnconfigure(3, weight=1)

        provider_var = ctk.StringVar(value=app._pid_to_label.get(slot.provider, slot.provider))
        model_var = ctk.StringVar(value=slot.model)
        reasoning_var = ctk.StringVar(value=slot.reasoning or NO_REASONING)

        provider_combo = ctk.CTkComboBox(
            row, variable=provider_var, values=list(app.provider_labels), width=170,
            command=lambda label, i=index: self._on_provider_changed(i, label))
        provider_combo.grid(row=0, column=0, padx=(8, 4), pady=6, sticky="w")
        model_combo = ctk.CTkComboBox(
            row, variable=model_var, width=230,
            values=list(app._provider_models.get(slot.provider, [slot.model])),
            command=lambda _v, i=index: self._on_model_changed(i))
        model_combo.grid(row=0, column=1, padx=4, pady=6, sticky="w")
        reasoning_combo = ctk.CTkComboBox(
            row, variable=reasoning_var, width=130,
            values=self._reasoning_values(slot.provider, slot.model),
            command=lambda _v, i=index: self._sync_slot(i))
        reasoning_combo.grid(row=0, column=2, padx=4, pady=6, sticky="w")

        status = ctk.CTkLabel(row, text=f"Слот {index + 1}: не запускался", anchor="w")
        status.grid(row=0, column=3, padx=4, pady=6, sticky="ew")

        show_button = ctk.CTkButton(row, text="Показать", width=90, state="disabled",
                                    command=lambda i=index: self._select(i))
        show_button.grid(row=0, column=4, padx=4, pady=6)
        acad_button = ctk.CTkButton(row, text="В AutoCAD", width=100, state="disabled",
                                    command=lambda i=index: self._send_to_acad(i))
        acad_button.grid(row=0, column=5, padx=(4, 8), pady=6)
        grade_var = ctk.StringVar(value=NO_REASONING)
        grade_combo = ctk.CTkComboBox(
            row, variable=grade_var, width=70, state="disabled",
            values=[NO_REASONING, *mt.GRADES],
            command=lambda grade, i=index: self._on_grade(i, grade))
        grade_combo.grid(row=0, column=6, padx=(4, 8), pady=6)
        return {
            "provider_var": provider_var, "model_var": model_var,
            "reasoning_var": reasoning_var, "model_combo": model_combo,
            "reasoning_combo": reasoning_combo, "status": status,
            "show_button": show_button, "acad_button": acad_button,
            "grade_var": grade_var, "grade_combo": grade_combo,
        }

    def _reasoning_values(self, provider: str, model: str) -> list[str]:
        spec = lp.reasoning_spec(provider, model)
        if spec is None:
            return [NO_REASONING]
        return [NO_REASONING, *spec[1]]

    def _on_provider_changed(self, index: int, label: str) -> None:
        row = self.slot_rows[index]
        pid = self.app._label_to_pid.get(label, label)
        models = list(self.app._provider_models.get(pid, []))
        if models:
            row["model_combo"].configure(values=models)
            row["model_var"].set(models[0])
        self._on_model_changed(index)

    def _on_model_changed(self, index: int) -> None:
        row = self.slot_rows[index]
        pid = self.app._label_to_pid.get(row["provider_var"].get(), row["provider_var"].get())
        values = self._reasoning_values(pid, row["model_var"].get())
        row["reasoning_combo"].configure(values=values)
        if row["reasoning_var"].get() not in values:
            row["reasoning_var"].set(NO_REASONING)
        self._sync_slot(index)

    def _sync_slot(self, index: int) -> None:
        row = self.slot_rows[index]
        reasoning = row["reasoning_var"].get()
        self.slots[index] = mt.ModelTestSlot(
            provider=self.app._label_to_pid.get(
                row["provider_var"].get(), row["provider_var"].get()).strip().lower(),
            model=row["model_var"].get().strip(),
            reasoning="" if reasoning == NO_REASONING else reasoning.strip(),
        )

    def _sync_all_slots(self) -> None:
        for index in range(mt.SLOT_COUNT):
            self._sync_slot(index)

    def _save_slots(self) -> None:
        try:
            local_settings.save_value(
                lp.LOCAL_CONFIG_PATH, "model_test", "slots",
                mt.slots_as_dicts(self.slots))
            if self.file_path is not None:
                local_settings.save_value(
                    lp.LOCAL_CONFIG_PATH, "model_test", "file", str(self.file_path))
        except Exception:  # noqa: BLE001 — настройки не должны ронять окно
            pass

    # ------------------------------------------------------------------- файл
    def _pick_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Файл для теста моделей",
            filetypes=[("PDF и фото", "*.pdf *.jpg *.jpeg *.png *.tif *.tiff *.webp *.bmp"),
                       ("Все файлы", "*.*")],
        )
        if path:
            self.file_path = Path(path)
            self.file_label.configure(text=self.file_path.name)
            self._save_slots()

    # ----------------------------------------------------------------- запуск
    def _start_run(self) -> None:
        if self.running:
            return
        if self.file_path is None:
            self._log_line("Сначала выберите файл.")
            return
        self._sync_all_slots()
        self._save_slots()
        app = self.app
        cfg, *_ = app._build_runtime_config()
        key_secret = str(app.key_secret_var.get() or "")
        scale = app._current_scale()
        file_path = self.file_path
        slots = list(self.slots)
        self.running = True
        self.run_button.configure(state="disabled")
        for index, row in enumerate(self.slot_rows):
            row["status"].configure(text=f"Слот {index + 1}: выполняется…")
            row["show_button"].configure(state="disabled")
            row["acad_button"].configure(state="disabled")
            row["grade_combo"].configure(state="disabled")
            row["grade_var"].set(NO_REASONING)
            self.results[index] = None
            self._rating_entries.pop(index, None)

        def worker() -> None:
            try:
                prepared = mt.prepare_input(file_path, cfg)
                results = mt.run_all_slots(
                    prepared, slots, cfg,
                    key_secret=key_secret, scale=scale,
                    progress=lambda i, r: self.events.put(("slot", (i, r))),
                )
                self.events.put(("done", results))
            except Exception as exc:  # noqa: BLE001
                self.events.put(("fatal", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_slot_done(self, index: int, result: mt.SlotResult) -> None:
        self.results[index] = result
        row = self.slot_rows[index]
        row["status"].configure(text=mt.metrics_text(result))
        if result.ok:
            row["show_button"].configure(state="normal")
            row["acad_button"].configure(state="normal")
            row["grade_combo"].configure(state="normal")
        self._log_line(mt.metrics_text(result))

    def _on_grade(self, index: int, grade: str) -> None:
        """Оценка результата слота: запись в книгу статистики (повтор — правка той же)."""
        result = self.results[index]
        if result is None or not result.ok or grade == NO_REASONING:
            return
        entry = self._rating_entries.get(index)
        if entry is None:
            entry = self.ratings.add(mt.make_rating_entry(
                result, grade, self.file_path.name if self.file_path else ""))
            self._rating_entries[index] = entry
            self._log_line(f"Слот {index + 1}: оценка {grade} сохранена.")
        else:
            self.ratings.update_grade(entry, grade)
            self._log_line(f"Слот {index + 1}: оценка изменена на {grade}.")

    # -------------------------------------------------------------- отображение
    def _select(self, index: int) -> None:
        self.selected = index
        self._draw_selected()

    def _draw_selected(self) -> None:
        """Полный план результата: всё, что уйдёт в AutoCAD, а не только контур."""
        canvas = self.canvas
        canvas.delete("all")
        result = self.results[self.selected] if self.results else None
        if result is None or result.plan is None:
            self.metrics_label.configure(text="Нет результата для показа.")
            return
        plan = result.plan
        self.metrics_label.configure(text=mt.metrics_text(result))
        scene = mt.plan_scene(plan)

        points: list[tuple[float, float]] = [tuple(plan.pob)]
        for start, end, _kind in scene["boundary"] + scene["lines"]:
            points.extend([start, end])
        for text, point, _kind, _rot in scene["texts"]:
            points.append(point)
        if len(points) < 2:
            return
        width = max(canvas.winfo_width(), 200)
        height = max(canvas.winfo_height(), 200)
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        span_x = max(max(xs) - min(xs), 10.0)
        span_y = max(max(ys) - min(ys), 10.0)
        margin = 46
        scale = min((width - 2 * margin) / span_x, (height - 2 * margin) / span_y)
        cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2

        def proj(point: tuple[float, float]) -> tuple[float, float]:
            return (width / 2 + (point[0] - cx) * scale,
                    height / 2 - (point[1] - cy) * scale)

        # Линии улиц и прочие вспомогательные отрезки плана.
        for start, end, _kind in scene["lines"]:
            a, b = proj(start), proj(end)
            canvas.create_line(a[0], a[1], b[0], b[1], fill="#64748b", width=1)
        # Контур участка.
        for start, end, _kind in scene["boundary"]:
            a, b = proj(start), proj(end)
            canvas.create_line(a[0], a[1], b[0], b[1], fill="#38bdf8", width=2)
        # Дуги внутренних углов.
        for center, radius, start_a, end_a in scene["arcs"]:
            x1, y1 = proj((center[0] - radius, center[1] + radius))
            x2, y2 = proj((center[0] + radius, center[1] - radius))
            canvas.create_arc(x1, y1, x2, y2, start=_math.degrees(start_a),
                              extent=_math.degrees((end_a - start_a) % (2 * _math.pi)),
                              style="arc", outline="#38bdf8", width=1)
        # Все подписи плана: улицы, ширины, стороны, лот — на своих местах.
        for text, point, kind, rotation in scene["texts"]:
            x, y = proj(point)
            color = "#fbbf24" if "street" in kind else (
                "#facc15" if "lot" in kind else "#e2e8f0")
            kwargs: dict[str, Any] = {"fill": color, "font": ("Segoe UI", 8, "bold")}
            if rotation:
                kwargs["angle"] = -_math.degrees(rotation)
            try:
                canvas.create_text(x, y, text=text, **kwargs)
            except Exception:  # noqa: BLE001 — холст без поворота текста
                kwargs.pop("angle", None)
                canvas.create_text(x, y, text=text, **kwargs)
        # Точка POB.
        px, py = proj(tuple(plan.pob))
        canvas.create_oval(px - 3, py - 3, px + 3, py + 3, fill="#f87171", outline="")

    # ---------------------------------------------------------------- AutoCAD
    def _send_to_acad(self, index: int) -> None:
        result = self.results[index]
        if result is None or result.plan is None:
            return
        cfg, *_ = self.app._build_runtime_config()
        plan = result.plan
        self._log_line(f"Слот {index + 1}: отправляю в AutoCAD…")

        def worker() -> None:
            try:
                lock = getattr(self.app, "_draw_lock", None)
                if lock is not None:
                    with lock:
                        AcadBuilder(cfg).draw_plan(plan)
                else:
                    AcadBuilder(cfg).draw_plan(plan)
                self.events.put(("acad", (index, "")))
            except Exception as exc:  # noqa: BLE001
                self.events.put(("acad", (index, str(exc))))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------ прочее
    def _log_line(self, text: str) -> None:
        try:
            self.log.insert("end", text + "\n")
            self.log.see("end")
        except Exception:  # noqa: BLE001
            pass

    def _drain(self) -> None:
        if self._closed:
            return
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "slot":
                    index, result = payload
                    self._on_slot_done(index, result)
                elif kind == "done":
                    self.running = False
                    self.run_button.configure(state="normal")
                    self._log_line("Тест завершён.")
                    for index, result in enumerate(payload):
                        if result is not None and result.ok:
                            self._select(index)
                            break
                elif kind == "fatal":
                    self.running = False
                    self.run_button.configure(state="normal")
                    self._log_line(f"Ошибка: {payload}")
                elif kind == "acad":
                    index, error = payload
                    self._log_line(
                        f"Слот {index + 1}: в AutoCAD начерчено." if not error
                        else f"Слот {index + 1}: AutoCAD — {error}")
        except queue.Empty:
            pass
        self.window.after(120, self._drain)

    def _open_stats(self) -> None:
        stats = getattr(self, "_stats_window", None)
        if stats is not None and not stats._closed:
            stats.refresh()
            stats.window.lift()
            return
        self._stats_window = ModelTestStatsWindow(self)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        stats = getattr(self, "_stats_window", None)
        if stats is not None:
            stats.close()
        self._sync_all_slots()
        self._save_slots()
        if getattr(self.app, "_model_test_window", None) is self:
            self.app._model_test_window = None
        try:
            self.window.destroy()
        except Exception:  # noqa: BLE001
            pass


class ModelTestStatsWindow:
    """Накопленная статистика оценок по моделям и уровням рассуждений."""

    def __init__(self, parent: ModelTestWindow) -> None:
        self.parent = parent
        self._closed = False
        ctk = parent.ctk
        win = ctk.CTkToplevel(parent.window)
        self.window = win
        win.title("Статистика теста моделей")
        win.geometry("860x520")
        win.minsize(560, 360)
        win.configure(fg_color=parent.theme["bg_root"])
        parent.app._attach_child_window(win)
        win.protocol("WM_DELETE_WINDOW", self.close)
        win.grid_columnconfigure(0, weight=1)
        win.grid_rowconfigure(1, weight=1)

        bar = ctk.CTkFrame(win, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 4))
        ctk.CTkButton(bar, text="↻ Обновить", width=120,
                      command=self.refresh).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(bar, text="🗑 Сбросить историю", width=170,
                      command=self._clear).grid(row=0, column=1, sticky="w", padx=(8, 0))

        self.body = ctk.CTkTextbox(win, wrap="word")
        self.body.grid(row=1, column=0, sticky="nsew", padx=14, pady=(4, 12))
        self.refresh()

    def refresh(self) -> None:
        book = self.parent.ratings
        rows = mt.aggregate_ratings(book.entries)
        lines = [f"Всего оценок: {len(book.entries)}"]
        if not rows:
            lines.append("Пока пусто: запустите тест и поставьте оценки в слотах.")
        for row in rows:
            avg = f"{row['avg_grade']:.2f}" if row["avg_grade"] is not None else "—"
            closure = (f"{row['avg_closure_ft']:.3f} ft"
                       if row["avg_closure_ft"] is not None else "—")
            reasoning = row["reasoning"] or "без уровня"
            lines.append(
                f"{row['provider']} / {row['model']} · {reasoning} — "
                f"оценок: {row['count']} · средняя: {avg} · "
                f"замыкание ср.: {closure} · последняя: {row['last_grade']}")
        try:
            self.body.delete("1.0", "end")
            self.body.insert("end", "\n".join(lines))
        except Exception:  # noqa: BLE001
            pass

    def _clear(self) -> None:
        from tkinter import messagebox

        if messagebox.askyesno("Статистика", "Удалить все накопленные оценки?"):
            self.parent.ratings.clear()
            self.refresh()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if getattr(self.parent, "_stats_window", None) is self:
            self.parent._stats_window = None
        try:
            self.window.destroy()
        except Exception:  # noqa: BLE001
            pass
