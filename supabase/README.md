# Телеметрия: журналы программы → Supabase

Небольшая БД для сбора журналов действий Title Survey AI у разработчика.
Клиент (`telemetry.py`) отправляет события из `_log_line` / `_step0_log`
в Supabase PostgREST; разработчик читает их в Dashboard.

## Схема

| Таблица | Что хранит |
|---|---|
| `clients` | Одна строка на установку: анонимный `install_id` (UUID), `first_seen`/`last_seen`, `app_version`, `platform`. Персональных данных нет. |
| `sessions` | Один запуск: `session_id`, `started_at`/`ended_at`, `exit_clean`, `event_count` (считается триггером). |
| `app_logs` | События журнала: `level` (`info`/`key`/`warn`/`error`), `category` (`step0`/`step1`/`step2`/`step4`/`app`/`session`/`crash`…), `message` (до 2000 символов), `metadata` (jsonb), `app_version`, `created_at`. |
| `feedback` | Явные сообщения пользователя разработчику (задел на будущее). |

Индексы по `created_at`, `(install_id, created_at)`, `(level, created_at)`, `category`.
Триггер `touch_telemetry_parents` обновляет `clients.last_seen` и `sessions.event_count`.

## Безопасность (RLS)

- Включ клиента (publishable/anon) может **только INSERT** (и upsert своих
  `clients`/`sessions` при старте). Политики `SELECT`/`DELETE` для `anon` и
  `authenticated` **нет** — с включённым RLS это «запрещено». Плюс явный `REVOKE`.
- Разработчик читает через **Dashboard → Table Editor / SQL Editor** (роль `postgres`)
  или **service_role**-ключом. `service_role` обходит RLS — **никогда не кладите его
  в клиентское приложение и не коммитьте в git**.
- Publishable-ключ по дизайну публичный: его можно положить в `config.json`
  (раздел `telemetry`) для релиза. URL проекта тоже не секрет.

## Готовый проект (этот репозиторий)

- Проект: **TitleSurveyAI-telemetry** (`hggwxxpnkibhvnvwnenw`) в `eu-west-1`
- URL: `https://hggwxxpnkibhvnvwnenw.supabase.co`
- Publishable ключ уже в `config.json` → `telemetry.anon_key` (публичный, можно коммитить).
- Миграция уже применена (таблицы `clients`, `sessions`, `app_logs`, `feedback` + RLS + триггер + 2 view).
- Смотреть логи: **Supabase Dashboard → Table Editor → app_logs** или SQL Editor (примеры ниже).

## Установка (если делаете заново)

1. Создайте проект в Supabase (или возьмите существующий).
2. **Dashboard → SQL Editor** → вставьте целиком
   `supabase/migrations/20261121000000_create_telemetry.sql` → **Run**.
   (Или `supabase db push`, если проект подключён к CLI.)
3. **Project Settings → API** → скопируйте:
   - `Project URL` (например `https://abcdef.supabase.co`)
   - `publishable` (anon) ключ.
4. Положите их в **`config.local.json`** (не коммитится) — раздел `telemetry`:

   ```json
   {
     "telemetry": {
       "enabled": true,
       "url": "https://abcdef.supabase.co",
       "anon_key": "sb_publishable_…"
     }
   }
   ```

   Для релиза те же значения можно положить в `config.json` (anon-ключ публичный).
   Переменные окружения `SUPABASE_URL` / `SUPABASE_ANON_KEY` /
   `TSAI_TELEMETRY_ENABLED=0|1` имеют приоритет над файлами.
5. Перезапустите программу. В шапке окна есть переключатель
   **«Журналы разработчику»** — им можно выключить отправку.

## Что отправляется, а что нет

Отправляется: уровень, категория, короткое сообщение журнала, версия,
метки времени, анонимный `install_id`/`session_id`, платформа.
Сообщения проходят `redact()`: вырезаются `tsai1:…`, `sk-…`, `Bearer …`,
`api_key=…` и подобные секреты; длина ограничена 2000 символами.

Не отправляется: API-ключи LLM, PIN, содержимое документов целиком
(только строки журнала, которые программа и так показывает на экране),
имя пользователя Windows, IP-адрес (Supabase может логировать его на своей
стороне — это стандартная практика хостинга).

Если сети нет, события копятся в локальном outbox
(`%LOCALAPPDATA%/TitleSurveyAI/telemetry_outbox.jsonl`) и досылаются
при следующем запуске. Телеметрия никогда не блокирует интерфейс и не
роняет программу: все ошибки глушатся.

## Как смотреть логи (разработчику)

- **Dashboard → Table Editor → `app_logs`** (сортировка по `created_at` desc).
- **SQL Editor**, примеры:

  ```sql
  -- Последние ошибки
  select created_at, category, message, app_version, install_id
    from public.app_logs
   where level = 'error'
   order by created_at desc
   limit 50;

  -- Всё за один день по одному пользователю
  select created_at, level, category, message
    from public.app_logs
   where install_id = '…' and created_at > now() - interval '1 day'
   order by created_at;

  -- Кто запускал и сколько событий
  select * from public.v_sessions_summary limit 20;
  ```

- Старые данные чистить вручную (см. конец миграции): логи старше 90 дней,
  сессии старше 90 дней, клиенты без активности старше 180 дней.
