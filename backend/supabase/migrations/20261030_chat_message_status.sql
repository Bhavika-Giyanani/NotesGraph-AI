alter table public.chat_messages
  add column if not exists status text not null default 'complete'
  check (status in ('pending', 'complete', 'error'));

update public.chat_messages
set sources = '[]'::jsonb
where sources is null;

alter table public.chat_messages
  alter column sources set default '[]'::jsonb,
  alter column sources set not null;

notify pgrst, 'reload schema';
