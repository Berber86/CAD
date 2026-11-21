-- Лимитер спама: 1 install_id = макс 5 feedback / час и 200 app_logs / час
-- Поставили по запросу пользователя (иначе БД заспамят дикпиками).

create or replace function public.check_feedback_rate()
returns trigger language plpgsql as $$
declare cnt int;
begin
  select count(*) into cnt from public.feedback
   where install_id = new.install_id and created_at > now() - interval '1 hour';
  if cnt >= 5 then
    raise exception 'rate limit: too many feedback from this install_id (5/hour)';
  end if;
  return new;
end;
$$;

drop trigger if exists feedback_rate_limit on public.feedback;
create trigger feedback_rate_limit
  before insert on public.feedback
  for each row execute function public.check_feedback_rate();

create or replace function public.check_applogs_rate()
returns trigger language plpgsql as $$
declare cnt int;
begin
  select count(*) into cnt from public.app_logs
   where install_id = new.install_id and created_at > now() - interval '1 hour';
  if cnt >= 200 then
    raise exception 'rate limit: too many app_logs from this install_id (200/hour)';
  end if;
  return new;
end;
$$;

drop trigger if exists applogs_rate_limit on public.app_logs;
create trigger applogs_rate_limit
  before insert on public.app_logs
  for each row execute function public.check_applogs_rate();
