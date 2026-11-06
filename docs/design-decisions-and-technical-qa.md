# Design decisions, trade-offs, and technical Q&A

This document explains the "why" behind the implementation and gives concise
answers to questions someone reviewing the system may ask.

## Major design decisions

### Supabase for identity and durable application data

Supabase Auth owns credentials and token lifecycle; the app does not store
passwords or issue its own JWT. Postgres stores account-owned records and
pgvector stores chunk embeddings in the same relational system. Storage is
private and holds large audio files. A publishable key is used in the browser;
the server-only secret key is used by FastAPI.

The server validates a bearer access token with Supabase Auth on protected API
requests, derives the owner ID from the validated user, and passes it through
store functions. The service key bypasses RLS, so explicit owner filters are
part of the server's security boundary. RLS and Storage path policies remain
defense-in-depth for clients operating with user credentials.

### Direct resumable browser uploads

Audio can be up to 500 MiB. Sending it through FastAPI would consume API
bandwidth and tie up application resources. The browser therefore uploads
directly to private Storage with tus-js-client and uses 6 MiB chunks, retry
delays, a resumable fingerprint, and an eligible small-file fallback. FastAPI
creates the owner-scoped metadata record first and starts processing only after
the browser confirms upload completion.

### Separate notes and RAG chunk sizes

RAG chunks default to about 400 characters with 80-character segment-boundary
overlap. This favors precise retrieval/citations, while the model receives
multiple snippets (10 for explain mode, up to 6 for locate mode). Notes fallback
chunks are larger (900 characters) because that task summarizes content rather
than locating a precise audio moment.

### Segment metadata is the timestamp source of truth

The answer model receives text-only numbered excerpts and returns supporting
snippet IDs. Backend code maps those IDs to stored segment metadata, constructs
source times, and merges ranges. This prevents generated/fabricated timestamps
from becoming seek targets.

### Conversation intent is separate from lecture retrieval

Chat meta questions need prior messages, not lecture excerpts; explicit
general-knowledge questions should not be mislabeled off-topic. The router
handles conversation and general-answer paths before vector retrieval, then
rewrites context-dependent lecture questions and applies similarity plus
lexical checks. Pending general-answer offers persist with the assistant row
so acceptance still works after refresh.

### Pending assistant rows and polling

The API inserts a pending assistant row before running model work in a
background thread. This gives the UI an immediate server-backed turn and lets
refresh restore pending state. The browser polls the message list while a reply
is pending. Stale pending rows become error rows after five minutes when read.

This is simple and fits a single service, but is not a durable queue. A service
restart can interrupt an in-process task; queue-backed workers would be a
future reliability improvement.

### Topic-specific lecture titles and automatic subjects

The notes model returns both a specific lecture title and broader subject.
`_allocate_title()` prefers the first core topic, then a useful model title,
then transcript/subject fallback. The `title_provided` field preserves user
intent.

When a non-empty subject is supplied, `store.create()` normalizes it and
upserts a folder unique to that user. If subject was initially blank but notes
infer one, `store.update()` performs the same ensure-folder operation and
links the lecture. This avoids duplicate subject folders for minor whitespace
differences and keeps folders isolated by owner.

Chat session titles are deliberately simple at present: the first question's
first 80 characters replace `"New chat"`. This avoids another model call and
makes the title deterministic, but can produce a long or awkward sidebar label.

## Common technical questions

### What happens from clicking "Upload" to seeing notes?

`UploadModal` validates and uploads audio directly to private Storage using a
resumable transfer. It creates a lecture record through FastAPI, then signals
completion. FastAPI retrieves the stored object, splits it with ffmpeg,
transcribes each part with Groq Whisper, and applies offsets so segment times
are relative to the original recording. It generates structured notes with
Gemini (with a conditional Groq fallback), stores them, infers titles/subjects
if needed, indexes transcript chunks, and updates a status row. The UI polls
status and shows the saved notes when ready.

### How are chunk offsets kept correct?

Whisper segment times are relative to each ffmpeg part. The transcription loop
adds the accumulated duration of all preceding parts to each segment start/end,
then advances the offset by the measured duration of the current part.

### How does RAG work here?

Timed segments are grouped into overlapping, segment-aligned documents.
Gemini embeds them into 768-dimensional vectors stored in pgvector. At question
time, the query is embedded and the SQL RPC returns nearest chunks filtered by
lecture and owner. Similarity and lexical gates remove weak matches; a
structured-output prompt decides whether snippets actually support an answer.
Backend citation code uses the supporting snippet IDs and stored segment
metadata to create timestamps.

### What does LangChain do? Does it store chat history?

LangChain is used for model adapters/prompts/output parsing, message objects,
documents, and the fallback text splitter. Supabase Postgres stores durable chat
history. LangChain's `InMemoryChatMessageHistory` is reconstructed from the
database history supplied to each answer operation; it is not the durable
source of truth.

### How are chats isolated?

Every chat session/message has an owner ID. Routes verify the bearer token and
scope chat lookups by user. The route also checks the session's lecture matches
the lecture in the URL. Database foreign keys cascade messages with a session
and sessions/chunks with a lecture.

### How does the app decide if a question is out of scope?

Conversation, general-answer, and follow-up routing run first. Lecture
questions use vector similarity and a configurable minimum; at least one
non-stopword term must also overlap. The answer model then returns a grounded
boolean and supporting snippet IDs. If retrieval fails the gates, the model
rejects grounding, or the JSON is invalid, sources are empty and the response
is treated as ungrounded.

### Can the LLM make up a citation timestamp?

No timestamp is included in the answer prompt. The model returns snippet IDs;
the backend reads segment times from metadata attached to those chunks and
builds source objects. It also checks duration and time validity before
returning a citation.

### How do source ranges get merged?

Relevant consecutive segments inside one supported chunk form a hit. For locate
answers, hits are time-sorted and merged only when the gap is no more than 20
seconds and the merged span stays within 90 seconds. At most 15 locate ranges
are returned. Explain answers return up to three moments.

### Are citations word-accurate?

No. Citations currently use Whisper **segment** timestamps and segment-level
lexical overlap, choosing the first segment in a relevant consecutive run.
The code does not request Whisper word granularity, persist word timing, or snap
a source to a matching word. A future word-timestamp feature would require a
schema/API/storage change and tests; no word data should be added to model
prompts.

### How does "Go ahead" work after an off-topic answer?

The assistant row stores `pending_offer` JSON with `type=general_answer` and
the standalone question. On the next request, the backend checks the previous
assistant row before retrieval. A short affirmative or `accept_offer=true`
answers the stored question generally, then clears the old offer. The UI also
offers an explicit "Answer generally" button.

### How do timestamps turn into playback?

Chat source buttons call `LectureView.onSeek()`. It records the active
start/end key for highlighting, seeks to 1.5 seconds before the source start
(never below zero), calls `audio.play()`, and displays a short playback status.
Transcript timestamp buttons use the same callback.

### Why store transcript text and segments separately?

The complete text is convenient for note generation and fallback chunking.
Timed segments preserve the association between transcript spans and audio
times for the transcript UI and citations. Keeping timing structured avoids
trying to reconstruct exact time positions from plain text.

### How are subjects and chat titles chosen?

Subjects are user-entered or inferred by the notes model and normalized into
per-user unique folders. Lecture titles prefer the first core topic. The first
chat question is used as its title prefix; no separate chat-title model call
exists.

### What does the free Render plan imply?

The app's state is stored in Supabase, but temporary files use `/tmp` and
processing uses in-process background tasks. The container's local disk is not
durable. Free instances may sleep, and restarts can interrupt active processing.
Reliable background work at scale would need a persistent queue/worker and
always-on infrastructure.

## Known limitations

- **Word-level timestamps are absent.** Only Whisper segment granularity is
  requested and persisted. Do not describe the current app as word-timestamped.
- **Pasted transcripts have no timing.** They can be summarized/indexed, but
  citation code skips chunks without segment metadata.
- **Jobs are in-process.** Async tasks are not persisted to a queue and may be
  lost during deployment/restart/sleep.
- **Vector rebuild replaces rows before all new batches are committed.** A
  provider or database failure mid-reindex can leave a partial index; retry
  indexing to rebuild it.
- **Authentication is SPA-based.** Supabase SDK persistence is used in the
  browser; there is no HttpOnly session cookie or application-level 401 refresh
  retry. Deploy CSP and prevent XSS.
- **Service-role operations bypass RLS.** The application must keep its
  owner-scoping predicates correct.
- **Chat titles use raw first-question text.** They are not summarized and can
  truncate mid-word.
- **Runtime health checks test configuration presence, not provider
  connectivity/credential validity.**

## Useful code-navigation answers

| Question | Start here |
|---|---|
| Where is auth enforced? | [`auth.current_user`](../backend/app/auth.py), then route dependencies in [`main.py`](../backend/app/main.py). |
| Where is the audio split and offset applied? | [`audio.py`](../backend/app/audio.py), `_split_audio()` and `transcribe_audio_with_segments()`. |
| Where is the upload client? | [`App.tsx`](../frontend/src/App.tsx), `uploadAudioToStorage()` and `UploadModal.submit()`. |
| Where are DB reads/writes? | [`store.py`](../backend/app/store.py). |
| Where is RAG indexed/retrieved? | [`rag.py`](../backend/app/rag.py), `_documents()`, `index_lecture()`, `_retrieve()`, and `ask()`. |
| Where are source timestamps produced? | [`rag.py`](../backend/app/rag.py), `_citation_runs()`, `_locate_sources()`, and `_source()`. |
| Where is the API contract? | [`schemas.py`](../backend/app/schemas.py). |
| Where are chat UI/polling actions? | [`App.tsx`](../frontend/src/App.tsx), `ChatView`. |
| Where is transcript playback highlighting? | `LectureView` and `TranscriptView` in [`App.tsx`](../frontend/src/App.tsx). |
| Where is the database schema? | [`schema.sql`](../backend/supabase/schema.sql) and its migrations. |
| Where is production configuration? | [`Dockerfile`](../Dockerfile), [`render.yaml`](../render.yaml), [`DEPLOYMENT.md`](../DEPLOYMENT.md). |
