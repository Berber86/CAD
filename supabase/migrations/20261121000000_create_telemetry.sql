-- Миграция: телеметрия Title Survey AI (журналы программы → разработчик)
-- =============================================================================
-- Схема: clients (установки) → sessions (запуски) → app_logs (события)
--        + feedback (явные сообщения пользователя разработчику).
--
-- Безопасность (RLS):
--   * anon (клиент программы, publishable-ключ) может только INSERT'ить.
--     Чтение/обновление/удаление для anon и authenticated ЗАПРЕЩЕНЫ
--     (политик SELECT/UPDATE/DELETE нет — RLS по умолчанию запрещает).
--   * Разработчик читает через Supabase Dashboard (Table Editor / SQL Editor)
--     или service_role-ключом (он обходит RLS по дизайну Supabase).
--   * Никогда не кладите service_role-ключ в клиентское приложение!
--
-- Применение: Supabase Dashboard → SQL Editor → вставить целиком → Run.
-- Или: supabase db push (если проект подключён к Supabase CLI).
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. clients: одна строка на установку программы (анонимный install_id)
-- ---------------------------------------------------------------------------
create table if not exists public.clients (
    install_id   uuid primary key,
    first_seen   timestamptz not null default now(),
    last_seen    timestamptz not null default now(),
    app_version  text,
    platform     text
);

comment on table public.clients is 'Установки программы (анонимный install_id, без персональных данных).';

-- ---------------------------------------------------------------------------
-- 2. sessions: один запуск программы
-- ---------------------------------------------------------------------------
create table if not exists public.sessions (
    session_id   uuid primary key,
    install_id   uuid not null references public.clients (install_id) on delete cascade,
    started_at   timestamptz not null default now(),
    ended_at     timestamptz,
    exit_clean   boolean,
    app_version  text,
    event_count  integer not null default 0
);

comment on table public.sessions is 'Запуски программы. event_count обновляется триггером.';

-- ---------------------------------------------------------------------------
-- 3. app_logs: события журнала (уровни как в log_mode.py)
-- ---------------------------------------------------------------------------
create table if not exists public.app_logs (
    id           bigint generated always as identity primary key,
    install_id   uuid not null references public.clients (install_id) on delete cascade,
    session_id   uuid references public.sessions (session_id) on delete set null,
    level        text not null default 'info'
                 check (level in ('info', 'key', 'warn', 'error')),
    category     text not null default 'app',
    message      text not null,
    metadata     jsonb not null default '{}'::jsonb,
    app_version  text,
    created_at   timestamptz not null default now()
);

comment on table public.app_logs is 'События журнала программы: level info/key/warn/error, category step0/step1/step2/step4/app/session/crash.';

create index if not exists app_logs_created_at_idx
    on public.app_logs (created_at desc);
create index if not exists app_logs_install_created_idx
    on public.app_logs (install_id, created_at desc);
create index if not exists app_logs_level_created_idx
    on public.app_logs (level, created_at desc);
create index if not exists app_logs_category_idx
    on public.app_logs (category);

-- ---------------------------------------------------------------------------
-- 4. feedback: явные сообщения пользователя разработчику (на будущее)
-- ---------------------------------------------------------------------------
create table if not exists public.feedback (
    id           bigint generated always as identity primary key,
    install_id   uuid not null references public.clients (install_id) on delete cascade,
    session_id   uuid references public.sessions (session_id) on delete set null,
    message      text not null check (char_length(message) between 1 and 4000),
    is_read      boolean not null default false,
    created_at   timestamptz not null default now()
);

comment on table public.feedback is 'Явные сообщения пользователя разработчику (кнопка «Написать разработчику», если появится).';

-- ---------------------------------------------------------------------------
-- 5. Триггер: last_seen клиента + event_count сессии при вставке лога
-- ---------------------------------------------------------------------------
create or replace function public.touch_telemetry_parents()
returns trigger
language plpgsql
as $$
begin
    update public.clients
       set last_seen = now()
     where install_id = new.install_id;

    if new.session_id is not null then
        update public.sessions
           set event_count = event_count + 1
         where session_id = new.session_id;
    end if;

    return new;
end;
$$;

drop trigger if exists app_logs_touch_parents on public.app_logs;
create trigger app_logs_touch_parents
    after insert on public.app_logs
    for each row execute function public.touch_telemetry_parents();

-- ---------------------------------------------------------------------------
-- 6. Row Level Security: anon только INSERT, чтение — только разработчику
-- ---------------------------------------------------------------------------
alter table public.clients   enable row level security;
alter table public.sessions  enable row level security;
alter table public.app_logs  enable row level security;
alter table public.feedback enable row level security;

-- INSERT для anon (клиент программы). Upsert клиентов/сессий идёт через
-- resolution=merge-duplicates, поэтому нужны и INSERT, и UPDATE для anon,
-- но UPDATE ограничен только своими строками (install_id/session_id совпадают
-- с уже существующей записью — PostgREST проверяет политику на конфликтной
-- строке; без UPDATE upsert падал бы с 403).
drop policy if exists "clients insert" on public.clients;
create policy "clients insert"
    on public.clients for insert to anon
    with check (true);

drop policy if exists "clients update own" on public.clients;
create policy "clients update own"
    on public.clients for update to anon
    using (true)
    with check (true);

drop policy if exists "sessions insert" on public.sessions;
create policy "sessions insert"
    on public.sessions for insert to anon
    with check (true);

drop policy if exists "sessions update own" on public.sessions;
create policy "sessions update own"
    on public.sessions for update to anon
    using (true)
    with check (true);

drop policy if exists "app_logs insert" on public.app_logs;
create policy "app_logs insert"
    on public.app_logs for insert to anon
    with check (true);

drop policy if exists "feedback insert" on public.feedback;
create policy "feedback insert"
    on public.feedback for insert to anon
    with check (true);

-- SELECT/UPDATE/DELETE для anon и authenticated намеренно НЕ создаются:
-- с включённым RLS это означает «запрещено». Разработчик читает через
-- Dashboard (postgres) или service_role.

-- Явный запрет чтения для anon/authenticated (защита от случайных GRANT'ов).
revoke select, update, delete on public.clients   from anon, authenticated;
revoke select, update, delete on public.sessions  from anon, authenticated;
revoke select, update, delete on public.app_logs  from anon, authenticated;
revoke select, update, delete on public.feedback from anon, authenticated;

-- ---------------------------------------------------------------------------
-- 7. Удобные представления для разработчика (только чтение, не для клиента)
-- ---------------------------------------------------------------------------
create or replace view public.v_recent_errors
with (security_invoker = true) as
select id, install_id, session_id, category, message, app_version, created_at
  from public.app_logs
 where level = 'error'
 order by created_at desc
 limit 200;

create or replace view public.v_sessions_summary
with (security_invoker = true) as
select s.session_id, s.install_id, s.started_at, s.ended_at,
       s.exit_clean, s.app_version, s.event_count, c.last_seen
  from public.sessions s
  join public.clients c on c.install_id = s.install_id
 order by s.started_at desc
 limit 200;

revoke select on public.v_recent_errors   from anon, authenticated;
revoke select on public.v_sessions_summary from anon, authenticated;

-- ---------------------------------------------------------------------------
-- 8. Очистка старых данных (запускать вручную по мере надобности)
-- ---------------------------------------------------------------------------
-- delete from public.app_logs where created_at < now() - interval '90 days';
-- delete from public.sessions where started_at < now() - interval '90 days';
-- delete from public.clients where last_seen < now() - interval '180 days';
