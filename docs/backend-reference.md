# Backend reference

The backend package is [`backend/app`](../backend/app). The FastAPI application
entry point is `backend.app.main:app`. Its implementation is intentionally
split into routes/orchestration (`main.py`), persistence (`store.py`), model
adapters (`llm.py`), and workload-specific modules.

## `main.py` - API, orchestration, and static SPA hosting

### Application setup

- `lifespan()` logs which provider credentials are configured and logs active
  in-process task count at shutdown.
- `log_http_requests()` measures request duration and logs method, path, status,
  and elapsed milliseconds. It logs stack traces for uncaught request errors.
- `app` is a FastAPI instance with CORS origins from `config.FRONTEND_ORIGINS`.
  Authorization and Content-Type headers are allowed.
- `_processing_tasks` holds references to scheduled asyncio tasks so they are
  not garbage-collected while active. These tasks are not a durable job queue.

### Background pipeline functions

- `_schedule_processing(lecture_id, user_id, audio_path, language)` dispatches
  `process_lecture()` through `asyncio.to_thread()`, tracks task completion,
  and logs cancellation or unexpected failure.
- `process_lecture(...)` executes transcription (only when saved transcript is
  absent), note generation, title/subject allocation, chunk indexing, status
  transitions, failure persistence, and temporary-file cleanup. If indexing
  alone fails, notes stay available and the lecture receives `index_error`.
- `process_indexing(lecture_id, user_id)` reloads saved transcript and segments
  and rebuilds only the RAG index. It records `done` or `index_error`.
- `_schedule_indexing(...)` sets indexing status, starts
  `process_indexing()` in a background thread, tracks it, and returns the
  updated owner-scoped lecture.
- `process_chat_answer(...)` calls `rag.ask()`, completes the pending assistant
  row, clears a consumed pending offer, and turns failures into an error row.
- `finished(...)` callbacks, declared inside scheduling functions, remove
  completed tasks from the active set and log cancellation/unhandled failures.

### Internal helpers

- `_require(lecture_id, user_id)` returns an owner-scoped lecture or raises 404.
- `_validate_storage_upload(storage_path, source_filename, user_id)` enforces
  `<owner UUID>/<file UUID>.<allowed extension>` and verifies the filename
  extension. The path's first component must match the authenticated user.
- `_allocate_title(notes, transcript, subject)` prefers the first structured
  core topic; otherwise chooses a non-generic model title; otherwise derives a
  short title from the first transcript sentence or subject.
- `_temp_audio_from_storage(meta)` downloads a private stored object and writes
  it to a temporary file for transcription.
- `_is_missing_chat_schema_column(exc)` detects known missing chat migration
  columns in Supabase/PostgREST errors.
- `_chat_schema_migration_error()` produces a 503 with the SQL migrations the
  operator must apply.

### Routes

All `/api/...` application routes below depend on `auth.current_user` either
directly or through `_require`. An access token is required.

| Route | Responsibility |
|---|---|
| `GET /api/health` | Public health/config-presence response for deployment probes. It reports booleans, not provider credential validity. |
| `GET /runtime-config.js` | Public JavaScript containing browser-safe Supabase URL/publishable key/API URL; cache disabled. No secret key. |
| `GET /api/lectures` | List the authenticated user's lectures. |
| `GET /api/folders` | List the authenticated user's subject folders. |
| `POST /api/folders` | Normalize and create/reuse a folder. Empty names are rejected. |
| `DELETE /api/folders/{folder_id}` | Delete an owner-owned empty folder; returns 409 if it contains lectures. |
| `POST /api/lectures` | Accept audio, pasted transcript, or an already uploaded Storage object; create lecture and optionally schedule processing. Maximum API-uploaded audio is 500 MiB. |
| `POST /api/lectures/{id}/start-upload` | Validate upload-pending state and schedule processing after direct Storage upload. |
| `POST /api/lectures/{id}/upload-failed` | Store upload failure state and an abbreviated error. |
| `GET /api/lectures/{id}` | Return owner-owned lecture and its notes. |
| `GET /api/lectures/{id}/transcript` | Return saved transcript as UTF-8 plain text. |
| `GET /api/lectures/{id}/segments` | Return sanitized timed segment rows; not included in lecture-list payload. |
| `GET /api/lectures/{id}/notes.pdf` | Render saved notes to PDF and return a download. |
| `GET /api/lectures/{id}/notes.md` | Render saved notes to Markdown and return a download. |
| `POST /api/lectures/{id}/retry` | Requeue a failed lecture when audio or transcript remains available. |
| `POST /api/lectures/{id}/retry-index` | Retry indexing only after `index_error`. |
| `POST /api/lectures/{id}/reindex` | Rebuild vectors from saved transcript and segment metadata; requires completed/index-error state and stored segments. |
| `GET /api/chat-sessions?lecture_id=...` | List owner-owned sessions, optionally checked/scoped to a lecture. |
| `POST /api/lectures/{id}/chat-sessions` | Create an owner-owned chat session for a lecture. |
| `GET /api/chat-sessions/{id}/messages` | Return persisted messages; also turns stale pending assistant messages into errors. |
| `POST /api/lectures/{id}/chat` | Persist a turn, schedule the answer, and return the user/pending assistant rows. |
| `DELETE /api/chat-sessions/{id}/messages` | Clear history while keeping the chat session. |
| `DELETE /api/chat-sessions/{id}` | Delete the owner-owned chat and cascading messages. |
| `DELETE /api/lectures/{id}` | Delete lecture-related data and private audio, then clear process-local chat history. |

`chat()` additionally ensures the requested chat session belongs to both the
authenticated user and URL lecture, reads only completed rows as prompt history,
and replaces the default session title with the first question's first 80
characters.

`SPAStaticFiles.get_response()` serves the built React app for unknown
non-API/non-file paths, enabling browser-side routes after refresh. At startup,
the app mounts this handler only when `frontend/dist` exists.

## `auth.py` - API token identity

- `_auth_client()` caches a Supabase client configured with project URL and
  publishable key.
- `current_user()` extracts HTTP Bearer credentials, validates the access
  token via Supabase Auth `get_user(token)`, and returns the trusted Supabase
  user UUID. Invalid tokens return 401; missing server configuration or
  upstream verification failures surface as 503.

The backend does not decode a user ID supplied by the browser and does not
implement a refresh endpoint. The browser's Supabase SDK manages its refresh
token and sends current access tokens.

## `config.py` - settings

Loads `.env`, determines working/storage paths, creates the upload scratch
directory, and reads API credentials, model names, chunk/RAG thresholds,
history window sizes, allowed audio extensions, and CORS origins.

Key current defaults: `CHUNK_MINUTES=10`, `RAG_CHUNK_SIZE=400`,
`RAG_CHUNK_OVERLAP=80`, `RAG_TOP_K=10`, `RAG_LOCATE_K=20`,
`RAG_MIN_SCORE=0.35`, `RAG_LEXICAL_MIN_MATCHES=1`, `HISTORY_WINDOW=10`, and
`FOLLOWUP_HISTORY_WINDOW=6`. See the file for the full list and fallback
environment variable names.

## `audio.py` - chunked transcription

- `_quietest_point(audio, target_ms)` searches a +/-15 second window in 250 ms
  steps for a lower-volume cut point.
- `split_audio(path, chunk_minutes)` is an in-memory compatibility helper that
  normalizes audio and cuts near quiet points; the production transcription
  function instead uses ffmpeg's time segmentation.
- `_split_audio(path, output_dir, chunk_minutes)` runs ffmpeg to produce
  mono/16 kHz MP3 parts, then ffprobe to measure each part. It fails explicitly
  if ffmpeg or duration probing fails.
- `transcribe_audio(...)` compatibility wrapper returning text only.
- `transcribe_audio_with_segments(...)` uploads each chunk to Groq Whisper with
  segment granularity, carries a short prior-text prompt, uses retry handling,
  applies `offset_seconds` to each chunk-local segment, and returns the full
  text plus original-recording `{start, end, text}` entries.

No word timestamps are requested or stored.

## `llm.py` - Gemini adapters

- `_require_key()` fails clearly if `GOOGLE_API_KEY` is absent.
- `get_llm(temperature)` builds `ChatGoogleGenerativeAI` with configured model,
  one SDK retry, and 90-second request timeout.
- `get_embeddings()` caches the Gemini embedding object and normalizes model
  names to the `models/...` form. RAG requests a 768-dimensional output.

## `notes.py` - structured note generation

- `_is_transient_provider_error(exc)` identifies provider status/message
  patterns eligible for the Groq fallback.
- `_split_transcript(transcript, max_chars=900)` groups transcript words into
  bounded text chunks. This is notes fallback chunking, separate from RAG
  chunks.
- `_is_groq_json_validation_error(exc)` recognizes Groq JSON-mode validation
  failures.
- `_parse_groq_notes(content)` strips optional JSON fences, parses the object,
  and validates it using the `Notes` Pydantic model.
- `_request_groq_notes(transcript, title, subject)` asks Groq for concise JSON;
  retries transient errors and retries once without provider JSON mode if that
  mode specifically rejects the request.
- `request(...)`, nested inside `_request_groq_notes`, assembles the Groq
  request options and performs the model call.
- `_generate_groq_chunk(...)` tries one extraction and recursively splits a
  chunk after known payload/JSON validation failures if it is large enough.
- `_generate_notes_with_groq(payload, progress)` processes chunks, aggregates
  summaries, and combines/deduplicates the structured fields.
- `deduplicate(items)`, nested inside `_generate_notes_with_groq`, removes
  identical serialized Pydantic items while preserving one copy.
- `generate_notes(...)` uses Gemini structured output first, retries
  transient errors, and falls back to Groq only when configured and the Gemini
  failure is classified as transient.
- `run()`, nested inside `generate_notes`, invokes the LangChain prompt/model
  pipeline and validates the resulting `Notes` model.

The `Notes` model fields are described in [database and API data](./data-auth-deployment.md#structured-notes-shape).

## `rag.py` - chunking, intent routing, RAG, and sources

### Indexing/retrieval helpers

- `_citation_debug(...)` emits structured citation diagnostics only when
  `DEBUG_CITATIONS` is enabled.
- `_log_segment_distribution(...)` logs min/median/max Whisper segment
  durations plus chunk settings for indexing.
- `_session_key(...)` scopes process-local history by owner, lecture, and chat.
- `_get_history(...)` returns/creates a LangChain in-memory history object.
- `clear_history(...)` deletes process-local history for one owner/chat.
- `clear_lecture_history(...)` removes process-local history for one
  owner/lecture.
- `format_timestamp(seconds)` generates `mm:ss` or `hh:mm:ss`.
- `_documents(transcript, segments)` produces segment-aligned RAG documents
  with start/end/segment metadata, or un-timed character chunks when segments
  are absent. Its nested `append_chunk()` snapshots accumulated segments into a
  `Document`.
- `index_lecture(...)` clears prior owner/lecture vectors, embeds documents in
  batches, and inserts metadata and vectors into `lecture_chunks`.
- `delete_lecture_vectors(...)` deletes vectors filtered by lecture and owner.
- `_retrieve(question, lecture_id, user_id, match_count)` embeds the question,
  calls `match_lecture_chunks`, and converts result rows to LangChain
  `Document`s with similarity metadata.
- `classify_intent(question)` deterministically returns `locate` for matching
  location/time phrase rules, otherwise `explain`.
- `_tokens(text)` extracts lower-case non-stopword tokens for lexical checks.
- `_first_matching_segment(doc, question)` returns the first segment with a
  token overlap; used for debug inspection.
- `_citation_runs(doc, question, duration)` identifies contiguous relevant
  segment runs, validates/clamps their time bounds, and skips missing metadata.
- `_source(start, end, text, is_range)` shapes a source API object and label.
- `_locate_sources(hits, duration)` deduplicates/sorts hits, merges within
  configured gap and maximum-range rules, and caps returned locate ranges.
- `_format_snippets(docs)` numbers excerpt text for the answer model.
- `_rewrite_question(llm, question, history)` rewrites a contextual follow-up
  into a standalone question using recent history.

### Intent and answer helpers

- `_is_conversation_question(question)` applies deterministic chat-meta rules.
- `_classify_conversation_question(llm, question)` classifies ambiguous
  history-meta candidates with a yes/no prompt.
- `_needs_followup_rewrite(question)` detects context-dependent language.
- `_is_general_thread_followup(question)` identifies short context-dependent
  follow-ups after a general answer.
- `_is_clearly_lecture_related(question)` lets an explicit lecture/recording/
  speaker or locate question return from general mode to lecture retrieval.
- `_classify_general_request(llm, question)` makes a yes/no decision for an
  ambiguous request to answer generally.
- `_is_explicit_general_request(question)` uses deterministic wording for
  explicit general-knowledge asks and acknowledgement-plus-question patterns.
- `_is_affirmative_offer_reply(question)` matches short affirmative offer
  responses.
- `_is_ambiguous_short_reply(question)` identifies short replies that require
  disambiguation.
- `_classify_offer_acceptance(llm, question, offered_question)` classifies
  ambiguous short replies relative to the stored offer.
- `_conversation_answer(llm, question, history)` answers only from recent
  chat history; for a first-turn meta question, returns a deterministic
  no-prior-questions response.
- `_general_answer(llm, question, history)` answers without retrieval, gives
  recent history for follow-ups, limits ordinary answers to about 150 words,
  and allows longer output for a more-detail request.
- `_answer(llm, question, context, history)` runs the numbered-snippet answer
  chain and validates the model's JSON shape. Invalid output is ungrounded.
- `ask(...)` performs ordered routing, optional query rewrite, retrieval and
  lexical/similarity gates, model answer, server-side source construction,
  pending-offer lifecycle, and updates the process-local history from the
  request.

### LangChain's role

`rag.py` uses LangChain `Document` to carry chunk text/metadata,
`ChatPromptTemplate` and `MessagesPlaceholder` to assemble prompts,
`StrOutputParser` for string model responses, and `RecursiveCharacterTextSplitter`
only when timestamped segments are unavailable. LangChain does not own the
database, API auth, vector table, or durable chat history.

## `store.py` - Supabase persistence

- `client()` caches a Supabase client using the server-only secret key.
- `_one(query)` executes a query with a one-row limit.
- `ensure_folder(user_id, subject)` normalizes whitespace and upserts a unique
  owner/name folder; blank subject means no folder.
- `create(...)` ensures a folder when appropriate, builds lecture metadata,
  and inserts the lecture row.
- `get(...)` and `update(...)` select/update by both lecture ID and owner.
  `update()` also associates the lecture with an ensured folder when subject
  changes.
- `list_all(...)` and `list_folders(...)` return owner-only library views.
- `delete_folder(...)` distinguishes missing folder, nonempty folder, and
  deleted folder.
- `save_transcript(...)`, `load_transcript(...)`, and
  `load_transcript_segments(...)` persist/retrieve transcript text and
  segment-array JSON.
- `save_notes(...)` and `load_notes(...)` persist/retrieve structured note JSON.
- `create_chat_session(...)`, `update_chat_title(...)`,
  `get_chat_session(...)`, and `list_chat_sessions(...)` manage owner-scoped
  sessions.
- `get_chat_messages(...)` first expires stale pending replies, then returns
  ordered persisted messages with grounding/source/offer fields.
- `fail_stale_chat_replies(...)` marks pending assistant rows older than five
  minutes as errors.
- `create_chat_turn(...)` inserts the user row plus pending assistant row and
  updates session `updated_at`.
- `complete_chat_reply(...)` updates only the matching pending row, scoped by
  message/session/owner/status; zero updated rows is an error.
- `clear_pending_offer(...)` clears an accepted assistant offer.
- `fail_chat_reply(...)` saves a user-facing error and empty citation state.
- `add_chat_messages(...)` is the synchronous two-row insertion helper; the
  current chat route uses the pending-turn lifecycle instead.
- `clear_chat_session(...)` removes messages but retains the session.
- `delete_chat_session(...)` verifies ownership, then deletes the session.
- `delete(...)` removes private audio when present, then deletes the lecture
  row; database foreign keys cascade related records.
- `upload_audio(...)` writes an audio file to the private bucket under
  `<user_id>/<lecture_id><extension>`.
- `download_audio(...)` downloads from Storage with bounded transient retries.

Every store operation involving a user's rows takes a `user_id`. The SQL schema
also enables RLS, but service-role clients bypass it; owner filters are
therefore essential.

## `schemas.py` - API and structured data contracts

- `Concept`: topic, explanation, key points.
- `Announcement`: announcement text and optional deadline.
- `ExamPoint`: point and constrained kind (`important_question`,
  `important_topic`, `professor_hint`).
- `OffTopic`: summary of a non-academic remark.
- `Notes`: title, subject, summary, core explanations, announcements, exam
  points, and off-topic remarks.
- `ChatRequest`: bounded `session_id`/`message`, plus `accept_offer`.
- `PendingOffer`: `general_answer` type and the standalone question to answer.
- `ChatSource`: optional start/end, timestamp label, and snippet.
- `ChatMessage`: stored message identity/content/time, source list, status,
  intent, grounded flag, and pending offer.
- `ChatTurn`: user message and pending assistant message returned by chat POST.
- `FolderCreate`: validated folder name.
- `UploadFailure`: validated upload error text.

## `render.py` - note exports

- `notes_to_markdown(notes)` converts the structured note buckets into a
  readable Markdown document.
- `_fonts()` uses bundled Noto Sans font files if both are present; otherwise
  uses ReportLab's Helvetica fonts.
- `_p(text)` XML-escapes text for ReportLab paragraphs and maps newlines to
  line breaks.
- `notes_to_pdf(notes)` builds the styled A4 PDF. Nested `heading()` adds
  colored section headings/rules; nested `bullets()` creates list flowables.

## `utils.py` - provider retries

- `with_retry(fn, tries, base_delay, max_delay)` retries only errors whose
  message matches known transient/rate-limit patterns, exponentially backs off
  up to the cap, logs retries, and re-raises permanent/final failures.

## Backend package roles

Direct Python dependencies are listed in
[`backend/requirements.txt`](../backend/requirements.txt). The deployed runtime
is Python 3.12; `audioop-lts` is conditionally installed for Python 3.13+.

| Package/tool | Purpose in this application |
|---|---|
| `fastapi` | HTTP API framework, dependency injection, multipart handling, and OpenAPI route contracts. |
| `uvicorn[standard]` | ASGI server that runs the FastAPI app locally and in Docker. |
| `python-multipart` | Parses multipart form data and file uploads. |
| `python-dotenv` | Loads local `.env` values into the process environment. |
| `pydantic` | Validates API payloads and structured model output. |
| `supabase` | Supabase Auth verification, Postgres REST queries/RPC, and private Storage access. |
| `pydub` | AudioSegment compatibility helper and in-memory audio handling. |
| `audioop-lts` | Compatibility module for audio dependencies on Python versions where `audioop` is removed. |
| `groq` | Whisper transcription and optional structured-notes fallback client. |
| `langchain-core` | Chat models' prompt composition, message types, documents, history interface, and output parsing. |
| `langchain-text-splitters` | Recursive text splitting when a transcript does not have timing segments. |
| `langchain-google-genai` | Gemini chat model and Google embedding integration. |
| `reportlab` | PDF generation for saved lecture notes. |
| `ffmpeg` / `ffprobe` (system executables) | Audio format conversion, timed splitting, and per-part duration measurement. Installed in Docker; required for local audio transcription too. |

## Scripts and tests

- `backend/scripts/reindex_lecture.py`: verifies owner-scoped lecture and saved
  transcript/segments, then rebuilds vectors without retranscription.
- `backend/scripts/evaluate_citations.py`: accepts 1-10 question/expected-time
  pairs and prints cited/expected/error times; can save a baseline and compare
  later results. It contacts configured retrieval/model services.
- `backend/tests/test_offline.py`: unit/API regression coverage using fakes and
  monkeypatching rather than requiring live Supabase, Whisper, or Gemini
  credentials. It verifies chat routing, source policies, ownership and
  pipeline behaviors.
- `backend/app/__init__.py`: package marker; contains no runtime behavior.
- `backend/supabase/schema.sql`: baseline relational schema, constraints,
  indexes, vector match function, RLS policies, and private bucket setup; see
  [data and deployment](./data-auth-deployment.md#database-and-storage).
- `backend/supabase/migrations/*.sql`: incremental compatibility changes for
  databases created from earlier schema versions.
