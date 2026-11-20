"""Developer-only System One comparison window; document inputs remain ephemeral."""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any

import developer_probability as dp
import llm_parser as lp

TYPES = {"Да / нет": "noul", "Шкала": "score", "Поиск страницы": "pages"}
LABELS = {value: key for key, value in TYPES.items()}
MODES = {"Локальный OCR / текст": "ocr", "Изображение (эксперимент)": "visual"}
PROVIDERS = {"Hydra · Jev + Solar": "hydra", "RouterAI · Jev + Microsoft": "routerai"}


class ProbabilityWindow:
    def __init__(self, chat: Any) -> None:
        self.chat = chat
        self.app, self.ctk, self.theme = chat.app, chat.ctk, chat.theme
        self.closed = False
        self.inflight = False
        self.events: queue.Queue = queue.Queue()
        self.files: list[tuple[str, Any]] = []
        self.rows: list[dict[str, Any]] = []
        ctk, theme = self.ctk, self.theme
        settings = dp.load_settings()
        self.win = win = ctk.CTkToplevel(self.app)
        win.title("Разработчик — вероятности System One")
        win.geometry("1120x850")
        win.minsize(720, 580)
        win.configure(fg_color=theme["bg_root"])
        win.attributes("-topmost", bool(self.app.topmost_var.get()))
        self.app._attach_child_window(win)
        win.protocol("WM_DELETE_WINDOW", self.close)
        win.grid_columnconfigure(0, weight=1)
        win.grid_rowconfigure(3, weight=1)
        ctk.CTkLabel(win, text="Проверка документа · вероятности", font=ctk.CTkFont(size=20, weight="bold")).grid(row=0, column=0, sticky="w", padx=14, pady=(10, 0))
        ctk.CTkLabel(win, text="Документ, имена файлов и вопросы отправляются выбранному провайдеру и обеим моделям; запросы платные. "
                     "Hydra: формат assets отклонён (422); графический режим не подтверждён. "
                     "RouterAI: не более 8 изображений в одном запросе. "
                     "Имена файлов также передаются. Вопросы и настройки сохраняются локально без шифрования; "
                     "документы и ответы не сохраняются. Полные ответы провайдера выводятся без маскирования: "
                     "они могут содержать документ, изображение или ключи!", wraplength=1050,
                     justify="left", text_color=theme["text_dim"]).grid(row=1, column=0, sticky="w", padx=14, pady=5)
        bar = ctk.CTkFrame(win, fg_color="transparent")
        bar.grid(row=2, column=0, sticky="ew", padx=14, pady=4)
        choices = ctk.CTkFrame(bar, fg_color="transparent")
        choices.pack(side="top", fill="x", pady=(0, 4))
        actions = ctk.CTkFrame(bar, fg_color="transparent")
        actions.pack(side="top", fill="x")
        self.provider = ctk.StringVar(value=next((k for k, v in PROVIDERS.items() if v == settings["provider"]), list(PROVIDERS)[0]))
        ctk.CTkOptionMenu(choices, values=list(PROVIDERS), variable=self.provider, command=lambda _: self._save(), width=210).pack(side="left", padx=(0, 8))
        self.mode = ctk.StringVar(value=next((k for k, v in MODES.items() if v == settings["mode"]), list(MODES)[0]))
        ctk.CTkOptionMenu(choices, values=list(MODES), variable=self.mode, command=lambda _: self._save()).pack(side="left", padx=(0, 8))
        ctk.CTkButton(actions, text="Добавить PDF / JPG / PNG", command=self.add_files).pack(side="left", padx=(0, 8))
        ctk.CTkButton(actions, text="Добавить вопрос", command=lambda: self.add_question({"type": "noul", "text": ""}),
                      fg_color=theme["btn_secondary"], hover_color=theme["btn_secondary_hover"]).pack(side="left", padx=(0, 8))
        ctk.CTkButton(actions, text="Назад в чат", command=self.close,
                      fg_color=theme["btn_secondary"], hover_color=theme["btn_secondary_hover"]).pack(side="left")
        body = ctk.CTkScrollableFrame(win, fg_color="transparent")
        body.grid(row=3, column=0, sticky="nsew", padx=14, pady=4)
        body.grid_columnconfigure(0, weight=1)
        self.files_frame = ctk.CTkFrame(body, fg_color="transparent")
        self.files_frame.grid(row=0, column=0, sticky="ew")
        self.files_frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(body, text="Вопросы · шкала: 0 и 1 или уровни через | (от 2 до 10)", anchor="w").grid(row=1, column=0, sticky="w", pady=(8, 2))
        self.questions_frame = ctk.CTkFrame(body, fg_color="transparent")
        self.questions_frame.grid(row=2, column=0, sticky="ew")
        self.questions_frame.grid_columnconfigure(0, weight=1)
        for question in settings["questions"]:
            self.add_question(question, save=False)
        self.result = ctk.CTkTextbox(win, height=200, wrap="word", fg_color=theme["bg_editor"], text_color=theme["text_main"], state="disabled")
        self.result.grid(row=4, column=0, sticky="ew", padx=14, pady=(4, 5))
        bottom = ctk.CTkFrame(win, fg_color="transparent")
        bottom.grid(row=5, column=0, sticky="ew", padx=14, pady=(0, 10))
        self.run_button = ctk.CTkButton(bottom, text="Проверить обе модели", command=self.start)
        self.run_button.pack(side="left", padx=(0, 10))
        ctk.CTkButton(bottom, text="Копировать всё", width=130, command=self.copy_result,
                      fg_color=theme["btn_secondary"], hover_color=theme["btn_secondary_hover"]).pack(side="left", padx=(0, 10))
        self.status = ctk.StringVar(value="Выберите файлы. Пустой диапазон PDF означает все страницы; пример: 1-3,5.")
        ctk.CTkLabel(bottom, textvariable=self.status, wraplength=800, justify="left", text_color=theme["text_dim"]).pack(side="left")
        self.app.after(120, self._poll)

    def add_files(self) -> None:
        from tkinter import filedialog
        paths = filedialog.askopenfilenames(parent=self.win, filetypes=[("Документы", "*.pdf *.jpg *.jpeg *.png")])
        for path in paths:
            if any(existing == path for existing, _ in self.files):
                continue
            frame = self.ctk.CTkFrame(self.files_frame, fg_color="transparent")
            frame.pack(fill="x", pady=2)
            self.ctk.CTkLabel(frame, text=Path(path).name, width=220, anchor="w").pack(side="left")
            pages = self.ctk.CTkEntry(frame, placeholder_text="Страницы PDF: все или 1-3,5", width=220)
            pages.pack(side="left", padx=5)
            self.files.append((path, pages))
            def remove(f=frame, entry=pages):
                self.files[:] = [(p, e) for p, e in self.files if e is not entry]
                f.destroy()
            self.ctk.CTkButton(frame, text="×", width=35, command=remove).pack(side="left")

    def add_question(self, question: dict[str, Any], save: bool = True) -> None:
        ctk = self.ctk
        frame = ctk.CTkFrame(self.questions_frame, fg_color=self.theme["bg_editor"])
        frame.pack(fill="x", pady=3)
        frame.grid_columnconfigure(2, weight=1)
        active = ctk.BooleanVar(value=bool(question.get("active", True)))
        ctk.CTkCheckBox(frame, text="", variable=active, width=25, command=self._save).grid(row=0, column=0, padx=5)
        kind = ctk.StringVar(value=LABELS.get(question.get("type"), "Да / нет"))
        ctk.CTkOptionMenu(frame, variable=kind, values=list(TYPES), width=145, command=lambda _: self._save()).grid(row=0, column=1, padx=5, pady=4)
        text = ctk.CTkEntry(frame, placeholder_text="Вопрос")
        text.grid(row=0, column=2, sticky="ew", padx=5, pady=4)
        text.insert(0, str(question.get("text", "")))
        row: dict[str, Any] = {"frame": frame, "active": active, "kind": kind, "text": text}
        def delete():
            self.rows.remove(row)
            frame.destroy()
            self._save()
        ctk.CTkButton(frame, text="×", width=35, command=delete).grid(row=0, column=3, padx=5)
        extra = ctk.CTkFrame(frame, fg_color="transparent")
        extra.grid(row=1, column=1, columnspan=2, sticky="ew", padx=5, pady=(0, 4))
        extra.grid_columnconfigure(2, weight=1)
        for col, (key, placeholder) in enumerate((("low", "0 означает…"), ("high", "1 означает…"), ("levels", "Или все уровни через |"))):
            field = ctk.CTkEntry(extra, placeholder_text=placeholder)
            field.grid(row=0, column=col, sticky="ew", padx=(0, 4))
            value = question.get(key, "")
            field.insert(0, " | ".join(value) if isinstance(value, list) else str(value))
            row[key] = field
        for field in (text, row["low"], row["high"], row["levels"]):
            field.bind("<KeyRelease>", lambda _e: self._save())
        self.rows.append(row)
        if save:
            self._save()

    def _questions(self) -> list[dict[str, Any]]:
        return [{"type": TYPES.get(str(row["kind"].get()), "noul"), "active": bool(row["active"].get()),
                 **{k: str(row[k].get()).strip() for k in ("text", "low", "high", "levels")}}
                for row in self.rows]

    def _save(self) -> bool:
        try:
            dp.save_settings({"mode": MODES.get(str(self.mode.get()), "ocr"), "provider": PROVIDERS.get(str(self.provider.get()), "hydra"), "questions": self._questions()})
            return True
        except (OSError, ValueError):
            if hasattr(self, "status"):
                self.status.set("Не удалось сохранить вопросы и настройки на компьютере.")
            return False

    def _write_result(self, text: str) -> None:
        self.result.configure(state="normal")
        self.result.delete("1.0", "end")
        self.result.insert("end", text)
        self.result.configure(state="disabled")

    def copy_result(self) -> None:
        text = str(self.result.get("1.0", "end-1c") or "")
        if not text:
            self.status.set("Пока нет результата для копирования.")
            return
        self.app.clipboard_clear()
        self.app.clipboard_append(text)
        self.status.set("Результат скопирован целиком, включая полные ответы провайдера.")

    def start(self) -> None:
        if self.closed or self.inflight:
            return
        questions = [q for q in self._questions() if q["active"]]
        files = [(p, str(e.get()).strip()) for p, e in self.files]
        mode = MODES.get(str(self.mode.get()), "ocr")
        provider = PROVIDERS.get(str(self.provider.get()), "hydra")
        provider_name = "RouterAI" if provider == "routerai" else "Hydra"
        try:
            dp.build_questions(questions)
            if not files:
                raise ValueError("Добавьте хотя бы один файл")
            if any(q["type"] == "pages" for q in questions) and (len(files) != 1 or Path(files[0][0]).suffix.lower() != ".pdf"):
                raise ValueError("Поиск страницы работает только для одного PDF; отключите вопросы поиска страницы для нескольких файлов")
            cfg, active_pid, _, _, secret = self.app._build_runtime_config()
            if active_pid != provider:
                cfg["api"]["api_key"] = ""
            key = lp.clean_provider_key(provider, self.app.provider_key_vars[provider].get()) or None
            provider_cfg = cfg["api"]["providers"][provider]
            env_name = str(provider_cfg.get("env_key") or "").strip()
            chosen_key = key or provider_cfg.get("api_key") or (os.environ.get(env_name) if env_name else None) or (lp.load_dotenv_dict().get(env_name) if env_name else None)
            if not chosen_key:
                raise ValueError(f"Не настроен ключ {provider_name}")
            target = lp.resolve_api_target(cfg, provider=provider, model=dp.MODELS[0] if provider == "hydra" else dp.ROUTERAI_MODELS[0], api_key=chosen_key, key_secret=secret)
            if not target["api_key"]:
                raise ValueError(f"Не настроен ключ {provider_name}")
        except (ValueError, lp.LLMParseError, lp.KeyProtectionError) as exc:
            self.status.set(str(exc) if isinstance(exc, ValueError) else f"Проверьте ключ {provider_name} и PIN в настройках.")
            return
        if not self._save():
            return
        self.inflight = True
        self.run_button.configure(state="disabled")
        self._write_result("")
        self.status.set("Подготовка документа и проверка… Закрытие окна не отменяет отправленные запросы.")
        def work() -> None:
            try:
                start = time.monotonic()
                pages = dp.prepare_documents(files, mode) if provider == "hydra" else dp.prepare_documents(files, mode, provider=provider)
                prepare_time = time.monotonic() - start
                result = (dp.run_both(target["base_url"], target["api_key"], pages, mode, questions) if provider == "hydra" else
                          dp.run_both(target["base_url"], target["api_key"], pages, mode, questions, provider=provider))
                event = (prepare_time, result, None)
            except Exception as exc:  # No raw exception, paths or secrets in GUI
                event = (0, None, dp._safe_error(exc, provider))
            self.events.put(event)
        threading.Thread(target=work, daemon=True).start()

    def _poll(self) -> None:
        if self.closed:
            return
        try:
            preparation, result, error = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            self.inflight = False
            self.run_button.configure(state="normal")
            if error:
                self.status.set(error)
            else:
                lines = [f"Подготовка документа: {preparation:.2f} с", "Результаты моделей — оценки, не доказательство. Первая и последняя страница не означают непрерывный диапазон."]
                for model in result:
                    lines.append(f"\n{model['model']} · {model['seconds']:.2f} с")
                    for item in model["entries"]:
                        lines.append("\n" + item["question"])
                        if "error" in item:
                            lines.append("Ошибка: " + item["error"])
                        else:
                            answer = item["answer"]
                            if answer["type"] in ("score", "noul"):
                                lines.append(f"Значение: {answer['value']:.4f}")
                            elif answer["type"] == "pages":
                                lines.append("Данные не обнаружены" if answer["first"] is None else f"Первая: {answer['first']}, последняя: {answer['last']}")
                            lines.append(json.dumps(answer, ensure_ascii=False, indent=2))
                    if model.get("error"):
                        lines.append("Ошибка: " + model["error"])
                    lines.append("Время запросов, с: " + ", ".join(f"{t:.2f}" for t in model["request_seconds"]))
                    lines.append("Использование API по запросам: " + (json.dumps(model["usage"], ensure_ascii=False) if any(model["usage"]) else "не сообщено"))
                    for number, raw in enumerate(model.get("provider_responses", []), 1):
                        lines.append(f"\nПолный ответ провайдера № {number} · HTTP {raw['status']} · без маскирования")
                        lines.append(f"Протокол: {raw.get('http_version', 'не указан')} · {raw.get('reason', '')}")
                        lines.append("Заголовки:")
                        lines.extend(f"{name}: {value}" for name, value in raw["headers"])
                        lines.append(f"Тело ({raw['encoding']}):")
                        lines.append(raw["body"])
                self._write_result("\n".join(lines))
                self.status.set("Готово. Сравните ответы и время; ответы и документы не сохраняются.")
        self.app.after(120, self._poll)

    def close(self) -> None:
        self._save()
        self.closed = True
        self.win.destroy()
        if getattr(self.chat, "_probability", None) is self:
            self.chat._probability = None
