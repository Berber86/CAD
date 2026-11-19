"""Временный чат разработчика: каждый запрос содержит только текущий текст."""
from __future__ import annotations

import queue
import threading
import time
from typing import Any

import llm_parser as lp


def ask_once(text: str, cfg: dict[str, Any], *, provider: str, model: str,
             api_key: str | None = None, key_secret: str = "") -> tuple[str, lp.TokenUsage]:
    """Один OpenAI-совместимый запрос, без системного промпта и истории."""
    if not text.strip() or not model.strip():
        raise ValueError("Введите текст и модель")
    target = lp.resolve_api_target(cfg, provider=provider, model=model, api_key=api_key, key_secret=key_secret)
    client = lp._make_client(cfg.get("api", {}), target=target)
    try:
        result = client.chat.completions.create(
            model=target["model"], messages=[{"role": "user", "content": text}],
        )
        choices = getattr(result, "choices", None) or []
        message = choices[0].message if choices else None
        response = getattr(message, "content", None)
        if not isinstance(response, str) or not response.strip():
            if getattr(message, "refusal", None):
                raise ValueError("Модель отказалась отвечать")
            raise ValueError("Модель вернула пустой текстовый ответ")
        counts = lp._extract_usage_counts(result)
        usage = lp.TokenUsage(
            prompt_tokens=counts[0] if counts else 0,
            completion_tokens=counts[1] if counts else 0,
            total_tokens=counts[2] if counts else 0,
            available=counts is not None,
            provider=target["provider"], provider_label=target["provider_label"], model=target["model"],
        )
        return response, usage
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


def _safe_error(exc: Exception) -> str:
    """Не выводим тело ошибки SDK: оно может содержать запрос или секреты."""
    if isinstance(exc, lp.KeyProtectionError):
        return "Не удалось разблокировать API-ключ. Проверьте PIN в настройках."
    if isinstance(exc, lp.LLMParseError):
        return "Не задан или недоступен API-ключ. Проверьте настройки провайдера и PIN."
    if isinstance(exc, ValueError):
        return "Пустой ответ или отказ модели. Проверьте запрос и модель."
    status = getattr(exc, "status_code", None)
    if status in (401, 403):
        return "Ошибка авторизации. Проверьте API-ключ выбранного провайдера."
    if status == 429:
        return "Провайдер ограничил запросы или баланс. Попробуйте позже."
    if isinstance(status, int) and 400 <= status <= 599:
        return f"Ошибка провайдера (HTTP {status}). Проверьте модель и настройки."
    return "Запрос не выполнен. Проверьте сеть, провайдера и модель."


class DeveloperChat:
    """Отдельное окно, временные виджеты и независимая очередь фонового запроса."""

    def __init__(self, app: Any, ctk: Any, theme: dict[str, str]) -> None:
        self.app, self.ctk, self.theme = app, ctk, theme
        self._closed = False
        self._inflight = False
        self._discard_reply = False
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue()
        self.last_reply = ""
        self.window = win = ctk.CTkToplevel(app)
        win.title("Разработчик — чат с нейросетями")
        win.geometry("850x700")
        win.minsize(610, 480)
        win.configure(fg_color=theme["bg_root"])
        win.attributes("-topmost", bool(app.topmost_var.get()))
        win.protocol("WM_DELETE_WINDOW", self.close)
        app._attach_child_window(win)
        win.grid_columnconfigure(0, weight=1)
        win.grid_rowconfigure(2, weight=1)
        ctk.CTkLabel(win, text="Чат разработчика", font=ctk.CTkFont(size=20, weight="bold"),
                     text_color=theme["text_main"]).grid(row=0, column=0, sticky="w", padx=18, pady=(14, 0))
        ctk.CTkLabel(win, text="Каждый запрос независимый: предыдущая переписка не отправляется. "
                     "Диалог не сохраняется. Запросы могут быть платными.",
                     wraplength=800, justify="left", text_color=theme["text_dim"]).grid(
                         row=1, column=0, sticky="w", padx=18, pady=(3, 8))
        self.transcript = ctk.CTkTextbox(win, wrap="word", fg_color=theme["bg_editor"],
                                         text_color=theme["text_main"], state="disabled")
        self.transcript.grid(row=2, column=0, sticky="nsew", padx=18, pady=(0, 8))
        controls = ctk.CTkFrame(win, fg_color="transparent")
        controls.grid(row=3, column=0, sticky="ew", padx=18)
        controls.grid_columnconfigure(1, weight=1)
        pid = app.current_provider_id()
        self.provider_var = ctk.StringVar(value=app._pid_to_label[pid])
        self.model_var = ctk.StringVar(value=app.current_model())
        self._provider_id = pid
        self._models = dict(app._selected_models)
        self._models[pid] = app.current_model()
        self.provider_menu = ctk.CTkOptionMenu(controls, values=app.provider_labels, variable=self.provider_var,
                                               command=self._on_provider, width=170)
        self.provider_menu.grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.model_combo = ctk.CTkComboBox(controls, values=list(app._provider_models[pid]), variable=self.model_var)
        self.model_combo.grid(row=0, column=1, sticky="ew")
        self.input = ctk.CTkTextbox(win, height=105, wrap="word", fg_color=theme["bg_editor"],
                                    text_color=theme["text_main"])
        self.input.grid(row=4, column=0, sticky="ew", padx=18, pady=(8, 6))
        self.input.bind("<Control-Return>", self._send_shortcut)
        buttons = ctk.CTkFrame(win, fg_color="transparent")
        buttons.grid(row=5, column=0, sticky="ew", padx=18, pady=(0, 4))
        self.send_button = ctk.CTkButton(buttons, text="Отправить  Ctrl+Enter", command=self.send)
        self.send_button.pack(side="left", padx=(0, 8))
        ctk.CTkButton(buttons, text="Копировать ответ", command=self.copy_reply,
                      fg_color=theme["btn_secondary"], hover_color=theme["btn_secondary_hover"]).pack(side="left", padx=(0, 8))
        ctk.CTkButton(buttons, text="Очистить", command=self.clear,
                      fg_color=theme["btn_secondary"], hover_color=theme["btn_secondary_hover"]).pack(side="left")
        self.status = ctk.StringVar(value="Готово. Для новой строки нажмите Enter.")
        ctk.CTkLabel(win, textvariable=self.status, text_color=theme["text_dim"],
                     wraplength=800, justify="left").grid(row=6, column=0, sticky="w", padx=18, pady=(0, 12))
        app.after(120, self._poll)

    def _on_provider(self, label: str) -> None:
        self._models[self._provider_id] = str(self.model_var.get() or "").strip()
        pid = self.app._label_to_pid.get(label)
        if not pid:
            return
        self._provider_id = pid
        self.provider_var.set(label)
        self.model_combo.configure(values=list(self.app._provider_models[pid]))
        self.model_var.set(self._models.get(pid) or self.app._provider_models[pid][0])

    def _append(self, text: str) -> None:
        self.transcript.configure(state="normal")
        self.transcript.insert("end", text)
        self.transcript.configure(state="disabled")
        try:
            self.transcript.see("end")
        except AttributeError:
            pass

    def _send_shortcut(self, _event: Any = None) -> str:
        self.send()
        return "break"

    def send(self) -> None:
        if self._closed or self._inflight:
            return
        text = str(self.input.get("1.0", "end") or "").strip()
        model = str(self.model_var.get() or "").strip()
        pid = self._provider_id
        if not text or not model:
            self.status.set("Введите сообщение и название модели.")
            return
        cfg, active_pid, _, _, secret = self.app._build_runtime_config()
        # Общий ключ из настроек другого провайдера не должен переехать к выбранному.
        if pid != active_pid:
            cfg["api"]["api_key"] = ""
        key = lp.clean_provider_key(pid, self.app.provider_key_vars[pid].get()) or None
        self.input.delete("1.0", "end")
        self._append(f"Вы  ·  {self.app._pid_to_label[pid]} / {model}\n{text}\n\n")
        self._inflight = True
        self._discard_reply = False
        self.send_button.configure(state="disabled")
        self.status.set("Ожидание ответа… Закрытие окна не отменяет уже отправленный запрос.")
        start = time.monotonic()
        def work() -> None:
            try:
                reply, usage = ask_once(text, cfg, provider=pid, model=model, api_key=key, key_secret=secret)
                event = (reply, usage, time.monotonic() - start, None)
            except Exception as exc:  # noqa: BLE001 - ответ SDK может содержать секреты
                event = (None, None, time.monotonic() - start, _safe_error(exc))
            with self._lock:
                if not self._closed:
                    self._queue.put(event)
        threading.Thread(target=work, daemon=True).start()

    def _poll(self) -> None:
        if self._closed:
            return
        try:
            reply, usage, seconds, error = self._queue.get_nowait()
        except queue.Empty:
            pass
        else:
            self._inflight = False
            self.send_button.configure(state="normal")
            if self._discard_reply:
                self.status.set("Ответ отброшен после очистки. Готово к новому запросу.")
            elif error:
                self._append(f"Ошибка: {error}\n\n")
                self.status.set(error)
            else:
                self.last_reply = reply
                self._append(f"Ответ\n{reply}\n\n")
                tokens = (f"токены: {usage.prompt_tokens} → {usage.completion_tokens} (всего {usage.total_tokens})"
                          if usage.available else "токены провайдер не сообщил")
                self.status.set(f"Ответ получен за {seconds:.1f} с · {tokens}")
        self.app.after(120, self._poll)

    def clear(self) -> None:
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        self.transcript.configure(state="disabled")
        self.input.delete("1.0", "end")
        self.last_reply = ""
        if self._inflight:
            self._discard_reply = True
            self.status.set("Очищено. Отправленный запрос завершится, но его ответ будет отброшен.")
        else:
            self.status.set("Переписка очищена.")

    def copy_reply(self) -> None:
        if not self.last_reply:
            self.status.set("Пока нет ответа для копирования.")
            return
        self.app.clipboard_clear()
        self.app.clipboard_append(self.last_reply)
        self.status.set("Последний ответ скопирован в буфер обмена.")

    def close(self) -> None:
        with self._lock:
            self._closed = True
            while not self._queue.empty():
                self._queue.get_nowait()
        self.last_reply = ""
        self.window.destroy()
        if getattr(self.app, "_developer_chat", None) is self:
            self.app._developer_chat = None
