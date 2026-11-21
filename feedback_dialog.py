"""
feedback_dialog.py
==================
Окно «Сообщить о проблеме» — единственное место, где логи уходят разработчику.

Поведение по требованию пользователя (2026-11-21):
  * Логи копятся локально во время работы (в памяти, без сети).
  * Отправка происходит только когда пользователь нажимает «Пожаловаться» →
    «Отправить отчёт». До этого ничего никуда не уходит.
  * В отчёт входит: анонимный install_id, версия, платформа, последние события
    журнала (уже очищенные от ключей) и текст пользователя.
"""

from __future__ import annotations

import threading
import webbrowser
from pathlib import Path
from typing import Any

import customtkinter as ctk

import ui_theme


def _open_supabase_dashboard() -> None:
    # Прямая ссылка на Table Editor проекта без SQL.
    url = "https://supabase.com/dashboard/project/hggwxxpnkibhvnvwnenw/editor"
    try:
        webbrowser.open(url, new=2)
    except Exception:
        pass


class FeedbackDialog:
    """Модальное окно обратной связи. Создаётся через ``open_feedback_dialog(parent)``."""

    def __init__(self, parent: Any) -> None:
        self.parent = parent
        try:
            import telemetry as app_telemetry  # noqa: PLC0415
            from version import APP_VERSION  # noqa: PLC0415
        except Exception:
            app_telemetry = None  # type: ignore[assignment]
            APP_VERSION = "?"
        self._telemetry = app_telemetry
        self._app_version = APP_VERSION

        self.win = ctk.CTkToplevel(parent)
        self.win.title("Сообщить о проблеме — Title Survey AI")
        self.win.geometry("720x640")
        self.win.minsize(640, 520)
        try:
            self.win.attributes("-topmost", False)
        except Exception:
            pass
        self.win.transient(parent)
        self.win.grab_set()
        self.win.focus_set()
        self._attach_child_window(self.win)

        # --- шапка
        head = ctk.CTkFrame(self.win, fg_color="transparent")
        head.pack(fill="x", padx=18, pady=(16, 8))
        ctk.CTkLabel(
            head,
            text="🐛  Сообщить о проблеме",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color="#ffffff",
            anchor="w",
        ).pack(anchor="w")
        ctk.CTkLabel(
            head,
            text=(
                "Опишите, что пошло не так. К отчёту автоматически приложится журнал последних действий "
                "(без ваших API-ключей и без содержимого документов целиком). Ничего не отправляется, "
                "пока вы не нажмёте «Отправить»."
            ),
            wraplength=680,
            justify="left",
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color=ui_theme.PALETTES[ui_theme.from_config(getattr(parent, 'config_data', {}) or {})].get("text_dim", "#94a3b8") if hasattr(ui_theme, "PALETTES") else "#94a3b8",
        ).pack(anchor="w", pady=(6, 0))

        # --- текст пользователя
        ctk.CTkLabel(
            self.win,
            text="Что случилось? (необязательно, но очень помогает)",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#e2e8f0",
            anchor="w",
        ).pack(anchor="w", padx=18, pady=(10, 4))
        self.message_box = ctk.CTkTextbox(
            self.win,
            height=110,
            fg_color=ui_theme.PALETTES[ui_theme.from_config(getattr(parent, 'config_data', {}) or {})].get("bg_field", "#1e293b") if hasattr(ui_theme, "PALETTES") else "#1e293b",
            text_color="#e2e8f0",
            font=ctk.CTkFont(size=12),
            wrap="word",
        )
        self.message_box.pack(fill="x", padx=18, pady=(0, 8))
        self.message_box.insert("1.0", "")
        try:
            self.message_box.focus_set()
        except Exception:
            pass

        # --- опции
        opts = ctk.CTkFrame(self.win, fg_color="transparent")
        opts.pack(fill="x", padx=18, pady=(0, 6))
        self.include_logs_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            opts,
            text="Приложить журналы последних действий (рекомендуется)",
            variable=self.include_logs_var,
            checkbox_width=18,
            checkbox_height=18,
            font=ctk.CTkFont(size=11),
            command=self._refresh_preview,
        ).pack(anchor="w", pady=(0, 4))
        self.count_label = ctk.CTkLabel(
            opts,
            text="",
            font=ctk.CTkFont(size=11),
            text_color="#94a3b8",
            anchor="w",
            justify="left",
            wraplength=680,
        )
        self.count_label.pack(anchor="w", pady=(2, 0))

        # --- превью логов
        preview_head = ctk.CTkFrame(self.win, fg_color="transparent")
        preview_head.pack(fill="x", padx=18, pady=(6, 2))
        preview_head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            preview_head,
            text="Предпросмотр того, что уйдёт (первые 60 строк):",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color="#94a3b8",
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            preview_head,
            text="Копировать",
            width=90,
            height=24,
            fg_color="transparent",
            hover_color="#334155",
            border_width=1,
            border_color="#334155",
            font=ctk.CTkFont(size=11),
            command=self._copy_preview,
        ).grid(row=0, column=1, sticky="e", padx=(8, 0))

        self.preview_box = ctk.CTkTextbox(
            self.win,
            fg_color="#0f172a",
            text_color="#cbd5e1",
            font=ctk.CTkFont(family="Consolas", size=11),
            wrap="none",
            height=180,
        )
        self.preview_box.pack(fill="both", expand=True, padx=18, pady=(0, 8))
        self._refresh_preview()

        # --- статус + кнопки
        self.status_var = ctk.StringVar(value="Нажмите «Отправить», чтобы поделиться отчётом. Отчёты хранятся анонимно.")
        ctk.CTkLabel(
            self.win,
            textvariable=self.status_var,
            font=ctk.CTkFont(size=11),
            text_color="#94a3b8",
            anchor="w",
            wraplength=680,
            justify="left",
        ).pack(anchor="w", padx=18, pady=(0, 8))

        btn_row = ctk.CTkFrame(self.win, fg_color="transparent")
        btn_row.pack(fill="x", padx=18, pady=(0, 16))
        btn_row.grid_columnconfigure(0, weight=1)
        ctk.CTkButton(
            btn_row,
            text="Отмена",
            width=120,
            height=34,
            fg_color="#334155",
            hover_color="#475569",
            font=ctk.CTkFont(size=12),
            command=self.close,
        ).grid(row=0, column=0, sticky="w")
        self.send_button = ctk.CTkButton(
            btn_row,
            text="📨  Отправить отчёт",
            width=180,
            height=34,
            fg_color="#2563eb",
            hover_color="#1d4ed8",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._on_send,
        )
        self.send_button.grid(row=0, column=1, sticky="e", padx=(12, 0))

        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.closed = False

    # -- helpers
    def _attach_child_window(self, win: Any) -> None:
        parent = self.parent
        if hasattr(parent, "_attach_child_window") and callable(getattr(parent, "_attach_child_window")):
            try:
                parent._attach_child_window(win)
            except Exception:
                pass

    def _collect_log_lines(self) -> list[str]:
        parent: Any = self.parent
        lines: list[str] = []
        # Основной журнал (правый столбец → вкладка Журнал)
        for _level, text in getattr(parent, "_log", []) or []:
            lines.append(str(text))
        # Журнал Шага 0 (портал)
        for _level, text in getattr(parent, "_step0_lines", []) or []:
            lines.append(str(text))
        return lines

    def _preview_text(self) -> str:
        lines = self._collect_log_lines()
        if not lines:
            return "Журнал пока пуст — будет отправлено только ваше сообщение и версия программы."
        # redact уже применяется при отправке, но для предпросмотра покажем как есть (но без ключей)
        try:
            import telemetry as t  # noqa: PLC0415
            redacted = [t.redact(s) for s in lines]
        except Exception:
            redacted = lines
        head = redacted[-120:] if len(redacted) > 120 else redacted
        preview = "\n".join(head[:60])
        if len(head) > 60:
            preview += f"\n… и ещё {len(head)-60} строк"
        return preview

    def _refresh_preview(self) -> None:
        include = bool(self.include_logs_var.get())
        lines = self._collect_log_lines()
        if include:
            txt = self._preview_text()
            self.count_label.configure(text=f"К отчёту будет приложено строк журнала: {len(lines)} (отправятся анонимно, без API-ключей).")
        else:
            txt = "Журналы не будут приложены — отправится только ваше сообщение."
            self.count_label.configure(text="Вы выбрали не прикладывать журналы.")
        try:
            self.preview_box.configure(state="normal")
            self.preview_box.delete("1.0", "end")
            self.preview_box.insert("1.0", txt)
            # запретить редактирование — но оставить выделение
            target = getattr(self.preview_box, "_textbox", self.preview_box)
            try:
                target.configure(state="disabled")
            except Exception:
                pass
        except Exception:
            pass

    def _copy_preview(self) -> None:
        try:
            txt = self.preview_box.get("1.0", "end")
            self.win.clipboard_clear()
            self.win.clipboard_append(txt)
            self.status_var.set("Скопировано в буфер обмена.")
        except Exception:
            pass

    def _on_send(self) -> None:
        msg = self.message_box.get("1.0", "end").strip()
        include = bool(self.include_logs_var.get())
        if not msg and not include:
            self.status_var.set("Напишите хотя бы пару слов или включите журналы — иначе отправлять нечего.")
            return
        # Блокируем кнопку на время отправки
        self.send_button.configure(state="disabled", text="Отправляю…")
        self.status_var.set("Отправляю отчёт… (обычно 1–2 сек, без зависания окна)")
        threading.Thread(target=self._do_send, args=(msg, include), daemon=True).start()

    def _do_send(self, user_message: str, include_logs: bool) -> None:
        # Сбор + отправка в фоне — UI не зависает.
        success = False
        detail = ""
        try:
            import telemetry as app_telemetry  # noqa: PLC0415
            from version import APP_VERSION  # noqa: PLC0415
            client = app_telemetry.get_client(app_version=APP_VERSION)
            # Если клиент ещё не инициализирован из main.py — он создастся здесь.
            # Собираем логи из родителя напрямую, чтобы не зависеть от внутренней очереди telemetry.
            parent: Any = self.parent
            log_lines: list[tuple[str, str]] = []
            if include:
                log_lines = list(getattr(parent, "_log", []) or [])
                # Добавляем Step0 как отдельные события уровня info
                step0 = getattr(parent, "_step0_lines", None)
                if step0:
                    log_lines.extend(list(step0))
            # Отправляем одним вызовом (внутри — upsert clients/sessions + batch app_logs + feedback)
            result = client.send_report(
                user_message=user_message,
                log_entries=log_lines if include else [],
                step0_entries=list(getattr(parent, "_step0_lines", []) or []) if include else [],
                metadata={"source": "feedback_dialog", "app_version": str(APP_VERSION)},
            )
            success = bool(result.get("ok"))
            detail = str(result.get("detail") or result.get("error") or "")
        except Exception as exc:  # noqa: BLE001
            success = False
            detail = f"{exc}"
        # Обратно в главный поток
        try:
            self.win.after(0, lambda: self._after_send(success, detail, user_message, include_logs=include))
        except Exception:
            pass

    def _after_send(self, success: bool, detail: str, user_message: str, include_logs: bool) -> None:
        if success:
            self.status_var.set("Готово! Отчёт отправлен — спасибо, это помогает делать программу лучше.")
            self.send_button.configure(state="normal", text="✓ Отправлено")
            # Через 1.2с закрываем диалог
            try:
                self.win.after(1200, self.close)
            except Exception:
                pass
            try:
                parent: Any = self.parent
                if hasattr(parent, "_set_status"):
                    parent._set_status("Отчёт отправлен разработчику — спасибо!", key=True)
            except Exception:
                pass
        else:
            self.send_button.configure(state="normal", text="📨  Отправить отчёт")
            if "not configured" in detail.lower() or "no url" in detail.lower():
                self.status_var.set("Не настроен адрес отчётов (Supabase). Попросите разработчика проверить config.json → telemetry.")
            elif "network" in detail.lower() or "timeout" in detail.lower() or "resolve" in detail.lower():
                self.status_var.set(f"Не удалось связаться с сервером отчётов: {detail[:180]}. Проверьте интернет и попробуйте ещё раз.")
            else:
                self.status_var.set(f"Не отправилось: {detail[:200] or 'неизвестная ошибка'}. Попробуйте ещё раз.")

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.win.grab_release()
        except Exception:
            pass
        try:
            self.win.destroy()
        except Exception:
            pass


def open_feedback_dialog(parent: Any) -> FeedbackDialog:
    """Открыть окно «Сообщить о проблеме» (модально)."""
    return FeedbackDialog(parent)


# ---------------------------------------------------------------------------
# Страница разработчика — просмотр отчётов без командной строки
# ---------------------------------------------------------------------------

class DeveloperReportsView:
    """Отдельная страница для разработчика-нуба: таблица отчётов без SQL.

    Открывается из настроек → кнопка «Открыть страницу отчётов».
    Если в программе есть service_role ключ — грузит напрямую.
    Если нет — показывает web-ссылку на Supabase Dashboard (кликами, без команд).
    """

    def __init__(self, parent: Any) -> None:
        self.parent = parent
        self.win = ctk.CTkToplevel(parent)
        self.win.title("Отчёты от пользователей — Title Survey AI")
        self.win.geometry("980x680")
        self.win.minsize(860, 520)
        try:
            self.win.attributes("-topmost", False)
        except Exception:
            pass
        self.win.transient(parent)
        self.win.grab_set()
        self._attach_child_window(self.win)

        head = ctk.CTkFrame(self.win, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(14, 8))
        ctk.CTkLabel(
            head,
            text="📥  Отчёты от пользователей",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color="#ffffff",
            anchor="w",
        ).pack(anchor="w")
        ctk.CTkLabel(
            head,
            text="Здесь видны все нажатия кнопки «Пожаловаться» от пользователей. Никаких команд — просто таблица.",
            font=ctk.CTkFont(size=11),
            text_color="#94a3b8",
            anchor="w",
            wraplength=940,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

        btn_row = ctk.CTkFrame(head, fg_color="transparent")
        btn_row.pack(fill="x", pady=(8, 0))
        ctk.CTkButton(
            btn_row,
            text="🔄 Обновить",
            width=120,
            height=30,
            fg_color="#2563eb",
            hover_color="#1d4ed8",
            command=self.refresh,
        ).pack(side="left")
        ctk.CTkButton(
            btn_row,
            text="🌐 Открыть в браузере (Supabase)",
            width=240,
            height=30,
            fg_color="#334155",
            hover_color="#475569",
            command=_open_supabase_dashboard,
        ).pack(side="left", padx=(8, 0))
        ctk.CTkButton(
            btn_row,
            text="✕ Закрыть",
            width=90,
            height=30,
            fg_color="transparent",
            hover_color="#475569",
            border_width=1,
            border_color="#475569",
            command=self.close,
        ).pack(side="right")

        self.status_var = ctk.StringVar(value="Загружаю…")
        ctk.CTkLabel(
            self.win,
            textvariable=self.status_var,
            font=ctk.CTkFont(size=11),
            text_color="#94a3b8",
            anchor="w",
            wraplength=940,
            justify="left",
        ).pack(anchor="w", padx=16, pady=(4, 0))

        self.text = ctk.CTkTextbox(
            self.win,
            fg_color="#0f172a",
            text_color="#e2e8f0",
            font=ctk.CTkFont(family="Consolas", size=11),
            wrap="word",
        )
        self.text.pack(fill="both", expand=True, padx=16, pady=(8, 16))

        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.closed = False
        self.refresh()

    def _attach_child_window(self, win: Any) -> None:
        parent = self.parent
        if hasattr(parent, "_attach_child_window") and callable(getattr(parent, "_attach_child_window")):
            try:
                parent._attach_child_window(win)
            except Exception:
                pass

    def _set_text(self, content: str) -> None:
        try:
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            self.text.insert("1.0", content)
            target = getattr(self.text, "_textbox", self.text)
            try:
                target.configure(state="disabled")
            except Exception:
                pass
        except Exception:
            pass

    def refresh(self) -> None:
        self.status_var.set("Загружаю отчёты…")
        self._set_text("Загрузка…")
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self) -> None:
        content = ""
        status = ""
        try:
            import telemetry as t  # noqa: PLC0415
            cfg = t.load_telemetry_config()
            # Пытаемся загрузить напрямую, если есть service_role/anon (но чтения нет для anon)
            # Используем helper, который пробует service_role, иначе показывает инструкцию.
            res = t.fetch_reports_for_viewer(limit=50)
            if res.get("ok"):
                rows = res.get("rows") or []
                if not rows:
                    content = "Пока нет отчётов. Когда пользователь нажмёт «Пожаловаться» — они появятся здесь.\n\nПодсказка: для просмотра напрямую в программе положите service_role ключ в config.local.json → telemetry.service_role_key (только у разработчика, не коммитить)."
                    status = f"Отчётов: 0 · Проект: {cfg.url or 'не настроен'}"
                else:
                    lines = []
                    for r in rows:
                        ts = (r.get("created_at") or "")[:19].replace("T", " ")
                        lvl = r.get("level") or r.get("is_read")  # feedback vs app_logs
                        msg = (r.get("message") or "")[:220].replace("\n", " ")
                        ver = r.get("app_version") or ""
                        iid = str(r.get("install_id") or "")[:8]
                        lines.append(f"[{ts}] v{ver} {iid} · {msg}")
                    content = "\n".join(lines)
                    status = f"Показано отчётов: {len(rows)} · Обновите кнопкой «Обновить»"
            else:
                err = res.get("error") or "неизвестная ошибка"
                if "service_role" in err.lower() or "not configured" in err.lower():
                    content = (
                        "Прямая загрузка в программе требует service_role ключ (только у разработчика).\n\n"
                        "БЕЗ КОМАНД — откройте отчёты в браузере:\n"
                        "1) Нажмите «🌐 Открыть в браузере» выше — попадёте в Supabase Dashboard.\n"
                        "2) Слева выберите Table Editor → feedback (сообщения) и app_logs (журналы).\n"
                        "3) Сортируйте по created_at ↓ — новые сверху.\n\n"
                        "Чтобы включить просмотр прямо здесь:\n"
                        "— Project Settings → API → service_role (secret) → скопируйте.\n"
                        "— Вставьте в config.local.json (рядом с exe):\n"
                        '  {\"telemetry\": {\"service_role_key\": \"sb_secret_…\"}}\n'
                        "— Перезапустите программу и снова откройте эту страницу.\n\n"
                        f"Диагностика: {err}"
                    )
                    status = "Web-просмотр доступен — нажмите «Открыть в браузере». Прямо в программе нужен service_role."
                else:
                    content = f"Не удалось загрузить: {err}\n\nПопробуйте «Открыть в браузере» — там всё видно без настроек."
                    status = f"Ошибка загрузки: {err[:120]}"
        except Exception as exc:  # noqa: BLE001
            content = f"Ошибка: {exc}\n\nНажмите «Открыть в браузере» — отчёты там точно видны."
            status = f"Ошибка: {exc}"
        try:
            self.win.after(0, lambda c=content, s=status: (self.status_var.set(s), self._set_text(c)))
        except Exception:
            pass

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.win.grab_release()
        except Exception:
            pass
        try:
            self.win.destroy()
        except Exception:
            pass


def open_developer_reports(parent: Any) -> DeveloperReportsView:
    return DeveloperReportsView(parent)

