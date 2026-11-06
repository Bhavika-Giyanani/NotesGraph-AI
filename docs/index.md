# NotesGraph AI: documentation map

This documentation is an implementation guide for the repository as it exists
today. It describes current behavior, including limitations; it is not a promise
that a planned feature is already implemented.

## Read in this order

1. [System architecture and execution flows](./architecture-and-flows.md) -
   how a browser action travels through React, FastAPI, Supabase, and AI
   providers.
2. [Backend reference](./backend-reference.md) - backend files, functions,
   routes, audio processing, notes, and RAG/chat logic.
3. [Frontend reference](./frontend-reference.md) - React components, state,
   browser APIs, uploads, player, transcript, notes, and chat UI.
4. [Data, authentication, and deployment](./data-auth-deployment.md) -
   database fields and their purposes, ownership boundaries, configuration,
   setup, and deployment.
5. [Design decisions and technical Q&A](./design-decisions-and-technical-qa.md) -
   rationale, trade-offs, limitations, and common technical questions.

## Quick project map

```text
backend/
  app/
    audio.py       Audio preparation and Groq Whisper transcription
    auth.py        Supabase access-token validation for API requests
    config.py      Environment-backed configuration
    llm.py         Gemini chat model and embedding factories
    main.py        FastAPI app, routes, background pipeline orchestration
    notes.py       Structured lecture-note generation and fallback
    rag.py         Chunking, vector indexing, retrieval, chat routing/citations
    render.py      Markdown and PDF export
    schemas.py     Pydantic request, response, and notes models
    store.py       Owner-scoped Supabase database and storage access
    utils.py       Bounded transient-error retry helper
  scripts/
    evaluate_citations.py  Compare returned citation times to expected times
    reindex_lecture.py     Rebuild vector chunks from saved transcript data
  supabase/
    schema.sql    Initial database, pgvector, RLS, and storage setup
    migrations/   Incremental SQL changes for existing projects
  tests/
    test_offline.py  Offline backend regression tests with provider fakes
frontend/
  src/
    App.tsx       Main React app, routes, dashboard, and feature views
    main.tsx      React entry point and router mount
    style.css     Global component styling, themes, breakpoints, motion rules
    supabase.ts   Browser-safe Supabase client configuration
    vite-env.d.ts Vite/browser global TypeScript declarations
  public/
    runtime-config.js  Local browser configuration fallback
  package.json   Frontend scripts and direct dependencies
Dockerfile       Multi-stage frontend build and Python API runtime
render.yaml      Render web service Blueprint
README.md        Setup and short project introduction
DEPLOYMENT.md    Render and Supabase deployment instructions
```

## Environment and accuracy

The backend defaults are defined in
[`backend/app/config.py`](../backend/app/config.py). The root
[`.env.example`](../.env.example) is a deployment/developer template and may
show suggested values different from code defaults. For example, code currently
defaults `GEMINI_MODEL` to `gemini-2.5-flash`, while the root example lists
`gemini-3.1-flash-lite`. An explicitly set environment variable wins.

The current transcription requests Whisper **segment** timestamps. There is no
word-timestamp field, table, or word-snap feature in the current schema or
application code. See [audio and citation flow](./architecture-and-flows.md#audio-upload-and-transcription)
and [current boundaries](./design-decisions-and-technical-qa.md#known-limitations).

## Local commands

From the repository root:

```powershell
python -m uvicorn backend.app.main:app --reload --port 8000
npm --prefix frontend run dev
python -m pytest backend/tests/test_offline.py -q
npm --prefix frontend run build
```

The backend needs its Python dependencies and an installed `ffmpeg` executable.
The frontend dev server proxies `/api` to `http://localhost:8000`.

## Interactive API reference

The deployed API is available at
[Swagger UI](https://notesgraph-ai.onrender.com/api/docs), the
[OpenAPI JSON](https://notesgraph-ai.onrender.com/api/openapi.json), and
[ReDoc](https://notesgraph-ai.onrender.com/api/redoc). For local development,
the same interfaces are served at `http://localhost:8000/api/docs`,
`http://localhost:8000/api/openapi.json`, and `http://localhost:8000/api/redoc`.
Protected routes require a Supabase access token; use Swagger's **Authorize**
button with the access token only, not a Supabase API key.
