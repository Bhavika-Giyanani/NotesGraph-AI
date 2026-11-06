alter table public.chat_messages
  add column if not exists status text not null default 'complete'
  check (status in ('pending', 'complete', 'error'));

alter table public.chat_messages
  add column if not exists intent text
  check (intent in ('locate', 'explain', 'off_topic')),
  add column if not exists grounded boolean;

notify pgrst, 'reload schema';
