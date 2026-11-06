alter table public.chat_messages
  add column if not exists pending_offer jsonb;

alter table public.chat_messages
  drop constraint if exists chat_messages_intent_check;

alter table public.chat_messages
  add constraint chat_messages_intent_check
  check (
    intent is null
    or intent in ('locate', 'explain', 'off_topic', 'conversation', 'general')
  );

notify pgrst, 'reload schema';
