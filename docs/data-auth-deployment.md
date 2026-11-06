# Data, authentication, and deployment

## Database and storage

The baseline is [`backend/supabase/schema.sql`](../backend/supabase/schema.sql).
Existing projects must also apply the relevant files in
[`backend/supabase/migrations`](../backend/supabase/migrations); migrations are
manual SQL operations and are not automatically run by the Docker container.

### `public.folders`

| Field | Purpose |
|---|---|
| `id` | Stable UUID primary key. |
| `user_id` | Auth owner; foreign key to `auth.users`, cascades on account deletion. |
| `name` | Non-empty subject name; unique per owner so same-name subjects are reused. |
| `created_at` | Creation/audit timestamp. |

### `public.lectures`

| Field | Purpose |
|---|---|
| `id` | Stable lecture UUID. |
| `user_id` | Authenticated owner and primary tenant filter. |
| `folder_id` | Optional link to a subject folder; `ON DELETE SET NULL` preserves a lecture if a folder is removed. |
| `title` | User-entered title, generated topic title, or placeholder while processing. |
| `title_provided` | Distinguishes a user-supplied title from one the pipeline may replace. |
| `subject` | Display subject string; paired with `folder_id` for organization. |
| `source` | Original filename or the marker `pasted transcript`. |
| `audio_path` | Private Storage object path, nullable for transcript-only records. |
| `transcript` | Full recognized/pasted text used by note generation and retrieval fallback. |
| `transcript_segments` | JSON array of Whisper `{start, end, text}` segments aligned to the original audio. |
| `notes` | JSON representation of the structured `Notes` object. |
| `status` | Processing lifecycle, including queued/uploading/transcribing/generating/indexing/done/error states. |
| `progress` | Human-readable current step for polling UI. |
| `error` | Persisted operator/user-facing processing error text. |
| `created_at` | Library ordering and creation timestamp. |

Indexes support owner/creation and folder lookup.

### `public.chat_sessions`

| Field | Purpose |
|---|---|
| `id` | Stable chat UUID referenced in the URL and messages. |
| `user_id` | Chat owner. |
| `lecture_id` | Parent lecture; deletion cascades to its chats. |
| `title` | Sidebar/history label; initially `"New chat"`, then set to a prefix of the first submitted question. |
| `created_at` | Creation timestamp. |
| `updated_at` | Recent activity ordering; updated when turns are saved. |

### `public.chat_messages`

| Field | Purpose |
|---|---|
| `id` | Generated ordered message ID. |
| `user_id` | Owner filter; duplicated here to support scoped queries/RLS. |
| `session_id` | Parent chat; deleting a session cascades messages. |
| `role` | `user` or `assistant`. |
| `content` | User question, assistant answer, or failure message. |
| `sources` | JSON list of server-built timestamp sources; defaults to an empty list. |
| `intent` | Answer route: `locate`, `explain`, `off_topic`, `conversation`, or `general`; null before completion. |
| `grounded` | Whether answer is supported by lecture snippets; null before answer and false for non-lecture intents. |
| `pending_offer` | JSON for a stored general-answer offer (`type`, standalone `question`) that can be accepted by the next short reply. |
| `status` | `pending`, `complete`, or `error`; allows optimistic UI, durable in-progress indication, and stale-task recovery. |
| `created_at` | Ordering and stale pending-reply detection. |

The user and assistant rows for a new question are inserted before AI generation.
That makes pending state visible and allows the client to recover by reloading
the message list.

### `public.lecture_chunks`

| Field | Purpose |
|---|---|
| `id` | Chunk-row UUID. |
| `user_id` | Tenant filter used by retrieval and deletion. |
| `lecture_id` | Lecture whose text is embedded. |
| `chunk_number` | Stable sequence within one indexing pass; unique with lecture ID. |
| `content` | Text sent to the embedding model and later to the answer prompt. |
| `metadata` | JSON segment list and chunk time bounds used to build citations. |
| `embedding` | 768-dimensional Gemini vector indexed with HNSW cosine operator class. |

`match_lecture_chunks(query_embedding, match_count, filter_lecture_id,
filter_user_id)` returns each matched row's ID, content, metadata, and cosine
similarity (`1 - cosine_distance`). It filters by both lecture and user and
caps the candidate count at 20.

### Storage

The `lecture-audio` bucket is private and limited to 500 MiB objects. Object
names start with the owner's UUID, followed by a UUID filename and extension.
The SQL storage policy checks that the first path segment matches
`auth.uid()`. Backend service-role operations are separately scoped by the
owner/path stored on the lecture.

## Structured notes shape

The `Notes` Pydantic model (also stored in `lectures.notes`) contains:

- `title`: topic-specific generated heading.
- `subject`: broader inferred course/subject name.
- `summary`: short overview of the recording.
- `core_explanations[]`: `topic`, `explanation`, and `key_points[]`.
- `announcements[]`: `text` and `deadline`.
- `exam_points[]`: `point` and `kind`; kind is `important_question`,
  `important_topic`, or `professor_hint`.
- `off_topic[]`: `summary` for non-academic remarks.

Gemini structured output is primary. A Groq notes fallback is used only when a
Groq key is configured and the Gemini failure appears transient. The fallback
splits large transcript input into bounded text pieces and merges the resulting
structured arrays.

## Authentication and owner isolation

### Sign-in and session renewal

1. `supabase.ts` initializes `createClient(projectUrl, publishableKey)`.
2. `SignIn` calls Supabase Auth password sign-in or sign-up.
3. Supabase returns a browser session containing an access token and refresh
   token. With the SDK defaults, the session is persisted and the SDK
   automatically refreshes access tokens.
4. Before protected API calls, `api()` obtains the current session and sets
   `Authorization: Bearer <access_token>`.
5. FastAPI `auth.current_user()` calls Supabase Auth `get_user(token)` and
   returns the validated user's ID. Missing/invalid bearer credentials are
   rejected.
6. Route handlers pass that trusted ID to every store operation. The store
   uses the server-only Supabase secret key to access Postgres/Storage.
7. Sign-out calls `supabase.auth.signOut()` and clears the local library cache
   for that user.

The API does not accept a user ID as authority from request JSON or URL. It
uses the owner ID returned by Auth. There is no custom refresh endpoint; refresh
is a browser SDK responsibility.

### Defense layers and important caveat

- The publishable key is intentionally public; secret/service-role key is only
  configured on the backend.
- Tables have RLS enabled and owner policies; the audio bucket has a path-based
  owner policy.
- **The backend uses the service-role key, which bypasses RLS.** Backend
  isolation consequently relies on validation in `auth.current_user()` and
  explicit `.eq("user_id", user_id)` scoping in store methods and vector RPC
  parameters. Keep those filters when adding queries.
- CORS is not authentication. It limits browser origins but does not prevent a
  non-browser client with a valid token from calling the API.
- The frontend currently uses the Supabase SDK's default persistent browser
  session mechanism. For an SPA, protect against XSS and deploy an appropriate
  Content Security Policy. The app does not currently implement a server-side
  HttpOnly-cookie session.
- API 401 responses are reported by the shared fetch helper; there is no
  explicit application-level "refresh then retry this request" wrapper.

### RLS and schema migrations

The schema enables RLS on folders, lectures, chat sessions, messages, and
lecture chunks. The policy predicate is `auth.uid() = user_id` for reads and
writes. The Storage policy checks the first directory of the object path.

Migration purposes:

- `20261030_chat_message_status.sql`: adds pending/complete/error state and
  ensures sources are non-null with an empty-array default.
- `20261106_chat_grounding_metadata.sql`: adds status/intent/grounded chat
  fields for earlier database installations.
- `20261106_chat_pending_offer_and_intents.sql`: adds `pending_offer` and
  expands allowed intent values to include conversation/general.

Use the schema plus migrations in the order documented in
[`DEPLOYMENT.md`](../DEPLOYMENT.md), and apply them to the same Supabase project
used by the runtime environment.

## Configuration reference

Backend values are read by `backend/app/config.py` from environment and root
`.env`; browser values come from Vite env or `/runtime-config.js`.

| Variable | Consumer and purpose |
|---|---|
| `SUPABASE_URL` | Backend project URL for token validation, database, and storage. |
| `SUPABASE_PUBLISHABLE_KEY` | Backend Auth verification client; legacy anon env fallback is supported. |
| `SUPABASE_SECRET_KEY` | Backend-only privileged data/storage client; legacy service-role env fallback is supported. |
| `VITE_SUPABASE_URL` | Public browser project URL. |
| `VITE_SUPABASE_PUBLISHABLE_KEY` | Public browser key. |
| `VITE_API_URL` | Optional backend origin only, required for a split frontend/API deployment; empty for same-origin/Vite proxy. |
| `FRONTEND_ORIGINS` | Comma-separated CORS allowlist; default is local Vite origin. |
| `GOOGLE_API_KEY` | Gemini notes, chat, and embeddings. |
| `GROQ_API_KEY` | Whisper transcription and optional notes fallback. |
| `GEMINI_MODEL` | Chat and primary notes model; code default `gemini-2.5-flash`. |
| `GROQ_NOTES_MODEL` | Groq structured-note fallback model. |
| `EMBEDDING_MODEL` | Gemini embedding model; code default `gemini-embedding-001`. |
| `WHISPER_MODEL` | Groq transcription model. |
| `WHISPER_HINT` | Prompt hint to transcription model about language/domain/script. |
| `CHUNK_MINUTES` | Audio split duration; code default 10 minutes. |
| `RAG_CHUNK_SIZE` | Approximate character limit for segment-aligned vector chunks; default 400. |
| `RAG_CHUNK_OVERLAP` | Character overlap between adjacent vector chunks; default 80. |
| `RAG_TOP_K` | Explain retrieval candidate count; default 10. |
| `RAG_LOCATE_K` | Locate retrieval candidate count; default 20. |
| `RAG_MIN_SCORE` | Similarity floor; default 0.35. |
| `RAG_LEXICAL_MIN_MATCHES` | Minimum shared non-stopword terms; default 1. |
| `DEBUG_CITATIONS` | Enables detailed RAG segment/score/source diagnostics when true. |
| `HISTORY_WINDOW` | Recent messages for conversation/answer prompts; default 10. |
| `FOLLOWUP_HISTORY_WINDOW` | Recent messages for follow-up rewrite/general answer; default 6. |
| `DATA_DIR` | Local temporary upload/scratch root; Render uses `/tmp/notesgraph`. |

Never put `SUPABASE_SECRET_KEY`, `GOOGLE_API_KEY`, or `GROQ_API_KEY` in a
`VITE_*` setting or browser runtime config.

## Deployment

### Docker image

[`Dockerfile`](../Dockerfile) has two stages:

1. Node 22 builds the frontend with `npm ci` and `npm run build`.
2. Python 3.12 slim installs ffmpeg and backend requirements, copies backend
   and built frontend, and starts Uvicorn at the Render-provided port.

The API serves `frontend/dist` from the same origin when present. That avoids
cross-origin API configuration for the single-service setup.

### Render

[`render.yaml`](../render.yaml) declares a Docker web service with `/api/health`
as its health check and environment variables for the server and public browser
configuration. Provider and Supabase secrets are entered in Render's backend
environment, not committed. SQL schema/migrations are run separately in
Supabase; deploy does not migrate the database automatically.

The Blueprint uses Render's free plan. Free instances may sleep, and the
current processing tasks are in-process rather than durable queue jobs. A
restart/sleep during processing can interrupt work; use always-on capacity and
a durable external queue if reliable long-running job execution is a production
requirement.

### Local development

1. Install Python requirements and `ffmpeg`; install Node dependencies with
   `npm --prefix frontend install`.
2. Configure root `.env` and `frontend/.env` from the example files.
3. Run FastAPI at port 8000 and Vite at 5173 in separate terminals.
4. Vite proxies `/api` to FastAPI. For a production-like local static setup,
   build frontend and let FastAPI serve `frontend/dist`.

See [`README.md`](../README.md) for quick setup and
[`DEPLOYMENT.md`](../DEPLOYMENT.md) for the operator checklist.
