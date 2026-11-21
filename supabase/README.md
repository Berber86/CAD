# Телеметрия: отчёты по кнопке «Пожаловаться» → Supabase

Программа **не шлёт ничего фоном**. Логи копятся локально и уходят разработчику
только когда пользователь сам нажимает кнопку **«🐛 Пожаловаться»** рядом с
**⚙ Настройки** (в шапке) или внутри панели настроек → блок «🆘 ПОМОЩЬ».

## Для пользователя (что видит ваш клиент)

1. В шапке программы — кнопка **«🐛 Пожаловаться»**.
2. При клике открывается окно:
   - поле «Что случилось?» (свободный текст),
   - галочка «Приложить журналы» (вкл. по умолчанию, без API-ключей),
   - предпросмотр первых 60 строк того, что уйдёт,
   - кнопка **«📨 Отправить отчёт»**.
3. До нажатия «Отправить» **ничего никуда не уходит**. Это можно показать клиенту.

## Для разработчика-нуба (без командной строки)

### Где смотреть отчёты — 2 клика мышкой:

1. Открой **Supabase Dashboard** → **Table Editor**:
   **https://supabase.com/dashboard/project/hggwxxpnkibhvnvwnenw/editor**
   (логин — твой аккаунт Supabase).
2. Слева выбери таблицы:
   - `feedback` — сообщения пользователей («Что случилось?»),
   - `app_logs` — приложенные строки журнала (`install_id` связывает их),
   - `sessions` / `clients` — кто и когда жаловался.

**Никаких SQL-команд не нужно** — просто кликай таблицы и сортируй по `created_at ↓`.

### Прямо в программе (отдельная страница):

- **⚙ Настройки** → внизу блок **«🆘 ПОМОЩЬ И ОБРАТНАЯ СВЯЗЬ»** → кнопка
  **«📥 Открыть страницу отчётов (для разработчика)»**.
- Откроется окно «📥 Отчёты от пользователей»:
  - Кнопка **«🔄 Обновить»** — подгружает последние 50 отчётов.
  - Кнопка **«🌐 Открыть в браузере»** — сразу в Supabase Table Editor.
  - Если хочешь видеть отчёты прямо в программе без браузера:
    1. Supabase → Project Settings → API → `service_role` → Copy.
    2. Создай рядом с exe файл `config.local.json`:
       ```json
       {"telemetry": {"service_role_key": "sb_secret_…сюда…"}}
       ```
       (файл в `.gitignore`, не коммитить!).
    3. Перезапусти программу → страница отчётов покажет таблицу внутри.

## Схема БД

| Таблица | Что хранит |
|---|---|
| `clients` | Одна строка на установку: `install_id` (UUID), `first_seen/last_seen`, `app_version`, `platform`. |
| `sessions` | Один запуск: `session_id`, `install_id`, `started_at/ended_at`, `exit_clean`, `event_count`. |
| `app_logs` | Строки журнала из отчётов: `level` `info/key/warn/error`, `category` `step0/step1/step2/step4/app/crash`, `message` (≤2000), `metadata`, `app_version`, `created_at`. |
| `feedback` | Текст пользователя из окна «Пожаловаться» (1–4000 символов). |

Триггер `touch_telemetry_parents` обновляет `last_seen`/`event_count`.
Представления `v_recent_errors` / `v_sessions_summary` — для быстрого просмотра (требуют service_role).

## Безопасность (RLS)

- Ключ в программе — `publishable` (anon) — может **только INSERT** в `clients/sessions/app_logs/feedback`.
  Читать/удалять не может (политик `SELECT/DELETE` для `anon` нет → RLS запрещает).
- Разработчик читает через Dashboard (роль `postgres`) или `service_role` ключом.
- `service_role` никогда не клади в `config.json` и не отправляй пользователям.

## Установка (уже выполнена для этого проекта)

- Проект: **TitleSurveyAI-telemetry** `hggwxxpnkibhvnvwnenw` в `eu-west-1`, URL `https://hggwxxpnkibhvnvwnenw.supabase.co`
- Миграция `supabase/migrations/20261121000000_create_telemetry.sql` уже применена.
- `config.json → telemetry` содержит `url`+`anon_key` (публичный) и `auto_send=false` (только по кнопке).
- Пользовательский код: `telemetry.py` (буфер + `send_report()`) + `feedback_dialog.py` (окна) + `main.py` (кнопки).

## Как протестировать

1. Запусти программу, сделай пару действий (чтобы появился журнал).
2. Нажми **«🐛 Пожаловаться»**, напиши «тест» и отправь.
3. Открой Dashboard → `feedback` — должна появиться строка, в `app_logs` — строки журнала с тем же `install_id`.

## Чистка старых данных

В Dashboard → SQL Editor (по желанию, раз в пару месяцев):

```sql
delete from public.app_logs where created_at < now() - interval '90 days';
delete from public.sessions where started_at < now() - interval '90 days';
delete from public.clients where last_seen < now() - interval '180 days';
```
