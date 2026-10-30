create extension if not exists pgcrypto;
create extension if not exists vector with schema extensions;

create table if not exists public.folders (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  name text not null check (length(trim(name)) > 0),
  created_at timestamptz not null default now(),
  unique (user_id, name)
);

create table if not exists public.lectures (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  folder_id uuid references public.folders(id) on delete set null,
  title text not null,
  title_provided boolean not null default false,
  subject text not null default '',
  source text not null default '',
  audio_path text,
  transcript text,
  transcript_segments jsonb not null default '[]'::jsonb,
  notes jsonb,
  status text not null default 'queued',
  progress text not null default 'Waiting to start',
  error text not null default '',
  created_at timestamptz not null default now()
);

create index if not exists lectures_owner_created_idx on public.lectures(user_id, created_at desc);
create index if not exists lectures_folder_idx on public.lectures(folder_id);

create table if not exists public.chat_sessions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  lecture_id uuid not null references public.lectures(id) on delete cascade,
  title text not null default 'New chat',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists chat_sessions_owner_updated_idx on public.chat_sessions(user_id, updated_at desc);

create table if not exists public.chat_messages (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  session_id uuid not null references public.chat_sessions(id) on delete cascade,
  role text not null check (role in ('user', 'assistant')),
  content text not null,
  sources jsonb not null default '[]'::jsonb,
  status text not null default 'complete' check (status in ('pending', 'complete', 'error')),
  created_at timestamptz not null default now()
);

alter table public.chat_messages
  add column if not exists status text not null default 'complete'
  check (status in ('pending', 'complete', 'error'));

update public.chat_messages set sources = '[]'::jsonb where sources is null;
alter table public.chat_messages alter column sources set default '[]'::jsonb;
alter table public.chat_messages alter column sources set not null;

create index if not exists chat_messages_owner_session_idx on public.chat_messages(user_id, session_id, created_at);

create table if not exists public.lecture_chunks (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  lecture_id uuid not null references public.lectures(id) on delete cascade,
  chunk_number integer not null,
  content text not null,
  metadata jsonb not null default '{}'::jsonb,
  embedding extensions.vector(768) not null,
  unique (lecture_id, chunk_number)
);

create index if not exists lecture_chunks_owner_lecture_idx on public.lecture_chunks(user_id, lecture_id);
create index if not exists lecture_chunks_embedding_idx on public.lecture_chunks using hnsw (embedding extensions.vector_cosine_ops);

create or replace function public.match_lecture_chunks(
  query_embedding extensions.vector(768),
  match_count integer,
  filter_lecture_id uuid,
  filter_user_id uuid
)
returns table (id uuid, content text, metadata jsonb, similarity float)
language sql stable
as $$
  select c.id, c.content, c.metadata, 1 - (c.embedding <=> query_embedding) as similarity
  from public.lecture_chunks c
  where c.lecture_id = filter_lecture_id
    and c.user_id = filter_user_id
  order by c.embedding <=> query_embedding
  limit greatest(1, least(match_count, 20));
$$;

alter table public.folders enable row level security;
alter table public.lectures enable row level security;
alter table public.chat_sessions enable row level security;
alter table public.chat_messages enable row level security;
alter table public.lecture_chunks enable row level security;

drop policy if exists "Users manage their folders" on public.folders;
drop policy if exists "Users manage their lectures" on public.lectures;
drop policy if exists "Users manage their chat sessions" on public.chat_sessions;
drop policy if exists "Users manage their chat messages" on public.chat_messages;
drop policy if exists "Users manage their lecture chunks" on public.lecture_chunks;

create policy "Users manage their folders" on public.folders
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy "Users manage their lectures" on public.lectures
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy "Users manage their chat sessions" on public.chat_sessions
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy "Users manage their chat messages" on public.chat_messages
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy "Users manage their lecture chunks" on public.lecture_chunks
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

insert into storage.buckets (id, name, public, file_size_limit)
values ('lecture-audio', 'lecture-audio', false, 524288000)
on conflict (id) do update set public = false, file_size_limit = 524288000;

drop policy if exists "Users access their audio objects" on storage.objects;
create policy "Users access their audio objects" on storage.objects
  for all using (bucket_id = 'lecture-audio' and (storage.foldername(name))[1] = auth.uid()::text)
  with check (bucket_id = 'lecture-audio' and (storage.foldername(name))[1] = auth.uid()::text);

grant all on public.folders, public.lectures, public.chat_sessions, public.chat_messages, public.lecture_chunks to service_role;
grant usage, select on all sequences in schema public to service_role;
grant execute on function public.match_lecture_chunks(extensions.vector, integer, uuid, uuid) to service_role;
