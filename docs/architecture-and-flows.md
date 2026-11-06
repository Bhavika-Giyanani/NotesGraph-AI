# Architecture and execution flows

## System at a glance

NotesGraph turns a recording or pasted transcript into saved lecture notes,
then allows the owner to ask questions and navigate to matching transcript
segments. The browser is a React/TypeScript SPA. FastAPI is the application API
and orchestration layer. Supabase provides Auth, Postgres (including pgvector),
and private object storage. Gemini is used for embeddings, chat, and primary
structured notes; Groq provides Whisper transcription and a conditional notes
fallback.

```text
Browser (React)
  | Supabase Auth session / bearer access token
  | API calls: /api/...
  +-----------------------> FastAPI (backend/app/main.py)
                               | validates token with Supabase Auth (auth.py)
                               | uses service-role client with owner filters (store.py)
                               +--> Supabase Postgres + pgvector
                               +--> private Supabase Storage
                               +--> Groq Whisper
                               +--> Gemini / Groq notes / Gemini chat
  Browser -----------------> Supabase Storage (resumable audio upload)
```

The backend uses service-role access for data operations, so its SQL/API
requests explicitly filter by the verified owner ID. The browser uses only the
publishable key; it never receives the service key. See
[authentication and data access](./data-auth-deployment.md#authentication-and-owner-isolation).

## Frontend boot and authentication

1. [`frontend/src/main.tsx`](../frontend/src/main.tsx) creates the React root,
   wraps the app in `React.StrictMode` and `BrowserRouter`, and imports
   `style.css`.
2. [`frontend/src/App.tsx`](../frontend/src/App.tsx) creates the Supabase
   session state by calling `supabase.auth.getSession()` and subscribes to
   `onAuthStateChange`.
3. Public routes render the landing/sign-in views. `/app/*` shows a loading
   screen until session initialization completes, then mounts the authenticated
   dashboard or redirects to `/sign-in`.
4. `SignIn` calls Supabase `signInWithPassword` or `signUp`. Supabase handles
   password verification, session issuance, persistence, refresh, and sign-out.
5. The shared `api()` helper obtains the current session, attaches its
   `access_token` as a bearer Authorization header, and sends requests to
   `/api/...`. Non-JSON errors and network/API errors are mapped to readable UI
   messages.

The Supabase JavaScript client is configured in
[`frontend/src/supabase.ts`](../frontend/src/supabase.ts). The SDK's default
browser auth settings persist sessions and automatically refresh access tokens.
There is no custom refresh-token endpoint in FastAPI.

## Lecture upload and processing

### Audio file route

1. The user selects a supported file in `UploadModal` in `App.tsx`. The browser
   checks the extension and the 500 MiB size limit.
2. The browser reads the session and chooses an object path under the user ID,
   using a UUID filename. That path is cached in local storage so the same file
   can resume.
3. The browser sends multipart metadata to `POST /api/lectures` with
   `storage_path`, source filename, language, and deferred-processing flag.
   FastAPI authenticates the request, checks the path belongs to the token
   owner, validates the filename/extension, creates the lecture record, and
   returns status `uploading`.
4. The browser uploads bytes directly to the private Supabase Storage bucket
   using tus-js-client resumable transfer (6 MiB chunks). For small files under
   6 MiB, a direct-upload fallback is available after a resumable network
   failure. The Storage request uses the access token plus public publishable
   key.
5. After upload succeeds, the browser calls
   `POST /api/lectures/{id}/start-upload`. The API schedules lecture processing
   in an in-process background task and returns promptly.
6. FastAPI's `process_lecture()` obtains the stored audio into a temporary file
   if it needs to transcribe. It advances the database status/progress through
   processing stages, saves transcript/segments and notes, indexes the lecture,
   and then marks it ready.

Direct browser-to-Storage upload avoids sending a large recording through the
API server. The server still downloads the private object to a temporary file
for transcription. Temporary files are cleaned up after processing.

### Pasted transcript route

`UploadModal` posts the text to `POST /api/lectures`. The API stores transcript
text, but has no Whisper-derived timing segments for pasted text. Notes and
vector chunks can still be created; precise timed citations require segment
metadata, so pasted transcripts do not produce timestamped sources unless
timed segments are otherwise present.

### Processing stages

The main stages in `process_lecture()` are:

1. Fetch the owner-scoped lecture record.
2. Persist an API-uploaded temporary file to private Storage if needed.
3. Load existing transcript/segments, or call
   `audio.transcribe_audio_with_segments()`.
4. Call `notes.generate_notes()` and save the structured `Notes`.
5. If the title was not supplied, choose a topic-specific title. If the subject
   was empty and inferred by the notes model, save it and create/reuse its
   folder.
6. Call `rag.index_lecture()` to build chunk metadata, embed text, and store
   vector rows.
7. Mark `done`; if indexing alone fails, keep notes and mark `index_error` so
   indexing can be retried without re-transcription or note generation.

The status poller in the dashboard refreshes lectures being uploaded or
processed. It polls individual active lecture records and refreshes the list
when a terminal state is observed.

## Audio upload and transcription

The production transcription path is `audio._split_audio()`, which invokes
`ffmpeg` to convert audio into mono, 16 kHz, 64 kbps MP3 files segmented at
`CHUNK_MINUTES` (default 10). `ffmpeg` resets timestamps inside each part.
`transcribe_audio_with_segments()` sends one part at a time to Groq Whisper
using `response_format="verbose_json"` and
`timestamp_granularities=["segment"]`.

Whisper returns segment times relative to the current part. The code maintains
`offset_seconds`, adds it to each returned segment's `start` and `end`, rounds
to milliseconds, then increments the offset by the measured duration of that
audio part. The resulting saved segment times therefore refer to the original
recording, not to an individual chunk. This is the timestamp offset logic in
`backend/app/audio.py`.

There is also a `split_audio()` compatibility helper that loads audio into
memory and looks for quiet points. The active transcription flow uses
`_split_audio()`/ffmpeg segmentation instead.

**Word-level timestamps are not currently implemented.** The Whisper request
asks for segment granularity only. The database stores segment JSON in
`lectures.transcript_segments`; there is no separate word table/column, and
neither the API nor prompts expose word timing data.

## RAG indexing and retrieval

### Index-time

1. `rag._documents()` groups timed Whisper segments until approximately
   `RAG_CHUNK_SIZE` characters (default 400), retaining full
   `{start, end, text}` segment metadata in each chunk.
2. When a chunk overflows, preceding segments are reused as overlap, up to
   `RAG_CHUNK_OVERLAP` characters (default 80). Boundaries stay on segment
   boundaries rather than splitting segment text.
3. `rag.index_lecture()` embeds chunk text with Gemini's
   `gemini-embedding-001`, requested at 768 dimensions. It deletes existing
   rows for that lecture/owner, then inserts new rows in batches of eight.
4. Metadata includes segment list and chunk time bounds. The row also stores
   owner ID, lecture ID, sequence number, text, and embedding.

If a transcript has no segments, indexing falls back to
`RecursiveCharacterTextSplitter`; those chunks do not have segment metadata.
They can help produce an answer, but citation building deliberately skips
chunks without timed segment metadata.

### Query-time

`rag.ask()` receives the latest user text and the completed prior messages from
the same persisted chat session. It routes in this order:

1. **Conversation intent:** deterministic chat-history phrase rules; possible
   history-meta candidates use a cheap yes/no model classifier. It answers from
   recent conversation history only and skips retrieval.
2. **Accept pending offer:** if the previous assistant message has a persisted
   general-answer offer, a short affirmative or explicit frontend
   `accept_offer=true` answers the offered question from general knowledge.
3. **Explicit general request:** explicit phrases (or a small classifier
   fallback for ambiguous phrases) skip lecture retrieval.
4. **General-thread follow-up:** a short context-dependent message after a
   general answer is optionally rewritten using recent history and answered
   generally; it does not retrieve lecture chunks. Clearly lecture-related
   questions leave general mode.
5. **Lecture follow-up rewrite:** context-dependent messages after a lecture
   response may be rewritten into a standalone question using recent history.
6. **Lecture retrieval:** deterministic rules label a question `locate` or
   `explain`. The embedding is matched against this lecture and user only.

The retrieval RPC uses cosine similarity and returns at most 20 candidates.
Locate questions request `RAG_LOCATE_K` (default 20); explain questions use
`RAG_TOP_K` (default 10). Before calling the answer model, the code drops hits
below `RAG_MIN_SCORE` (default 0.35) and requires lexical overlap with at least
`RAG_LEXICAL_MIN_MATCHES` question term (default 1). Thresholds are adjustable
in backend environment configuration.

The answer model gets numbered text snippets, never the chunk metadata,
segment list, or timestamps. Locate mode sends at most six snippets for the
written answer; explain mode sends up to ten. The model returns JSON containing
an answer, a `grounded` boolean, and supporting snippet IDs. Invalid JSON or
invalid fields result in an ungrounded response with no citations.

### Citation construction

The backend does not trust model-generated timestamps. After validating
supporting snippet IDs, it looks up those chunks' segment metadata and finds
consecutive segments that lexically overlap the standalone question. The run's
first segment is the source start; the last segment's end is its end.

- Explain answers return up to three distinct moments.
- Locate answers deduplicate, sort by time, and merge hits only if the gap is at
  most 20 seconds and the full merged range remains at most 90 seconds. They
  return at most 15 ranges.
- Invalid/out-of-duration hits and chunks without segment metadata are skipped.
- Source objects contain `start`, `end`, `timestamp_label`, and `text_snippet`.
- For a single moment, `end` equals `start`; range end comes from segment ends.

The frontend seeks to `max(0, start - 1.5)` and starts playback. The timestamp
shown in the chip still names the source's actual start.

## Chat persistence and polling

1. A user sends a question. `ChatView.sendQuestion()` appends optimistic user
   and pending-assistant bubbles immediately.
2. If there is no chat yet, the browser creates one with
   `POST /api/lectures/{lecture_id}/chat-sessions`.
3. The browser submits the turn to `POST /api/lectures/{lecture_id}/chat`.
   FastAPI checks the lecture/session ownership and relationship; reads only
   completed history; inserts a user message and pending assistant message;
   updates session activity; schedules answer generation in a thread; and
   returns both stored message rows.
4. `process_chat_answer()` calls `rag.ask()` with prior completed rows and the
   optional accept-offer flag, then updates the pending assistant row with
   answer, sources, intent, grounding, pending offer, and terminal status.
5. While a pending reply exists, the frontend polls the session messages
   endpoint. A database row left pending for over five minutes is marked as an
   error when messages are next read.
6. Refreshing or switching chats reads message rows from Supabase. Supabase is
   the durable source of truth.

LangChain's `InMemoryChatMessageHistory` is process-local helper state, not the
durable chat store. `rag.ask()` rebuilds it from the supplied database history
for the request, then appends the latest messages. Chat history sent to prompts
is limited by `HISTORY_WINDOW` (default 10 messages) or
`FOLLOWUP_HISTORY_WINDOW` (default 6).

Chat titles are currently not generated by an LLM: the first submitted
question's first 80 characters replace `"New chat"`. Subsequent questions do
not automatically rename a titled session.

## Export and transcript viewing

- Transcript tab requests `GET /api/lectures/{id}/segments`; the endpoint is
  owner-scoped and returns only valid `{start, end, text}` entries. Segment
  rows are searchable and the current row follows the audio playhead unless
  the user manually scrolls.
- Markdown/PDF actions call protected download routes. `render.py` formats the
  saved structured notes; it does not make another model call.
- The frontend SPA is built into `frontend/dist` and served by FastAPI if the
  directory exists. `SPAStaticFiles` falls back to `index.html` for client
  routes, but not for missing API routes or file paths.
