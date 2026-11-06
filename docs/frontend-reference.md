# Frontend reference

The frontend is a Vite-built React/TypeScript single-page application. Most UI
and browser-side workflows live in
[`frontend/src/App.tsx`](../frontend/src/App.tsx); global styles are in
[`frontend/src/style.css`](../frontend/src/style.css).

## Main files

### `main.tsx`

Creates the React root, enables `React.StrictMode`, mounts `BrowserRouter`, and
renders `App`. Imports the global stylesheet once.

### `supabase.ts`

Reads the Supabase URL and publishable key from runtime config first and Vite
environment variables second. Computes a user-facing configuration error and
creates a Supabase JS client only when configuration is valid. The browser
does not receive or use the secret/service-role key.

### `vite-env.d.ts`

Provides TypeScript declarations for Vite environment variables and the
`window.NOTESGRAPH_CONFIG` runtime settings object used by deployments.

### `style.css`

Owns global typography, theme variables, landing/sign-in pages, dashboard,
notes, transcript, chat, dialogs, code/table overflow, responsive breakpoints,
focus styles, and reduced-motion behavior. The stylesheet includes light/dark
theme selectors, tablet/mobile media queries (notably 950px and 720px), and a
`prefers-reduced-motion` override.

## `App.tsx` data types

- `Lecture`: library/detail fields, processing state, optional notes/audio path.
- `Notes`: frontend representation of the backend structured-note object.
- `Folder`: subject ID and normalized name.
- `ChatSession`: session, lecture, title, and last-updated fields.
- `ChatSource`: source start/end, display label, and snippet.
- `TranscriptSegment`: timed segment text.
- `ChatMessage`: user/assistant content, state, sources, intent, grounding, and
  pending general-answer offer. The intent union is `locate`, `explain`,
  `off_topic`, `conversation`, or `general`.

## Top-level helpers

- `formatSourceTime(seconds)` displays `mm:ss`, or `hh:mm:ss` for long
  lectures.
- `sourceLabel(source)` uses numeric boundaries to render a moment/range, else
  falls back to the server label.
- `displaySources(message)` suppresses source chips for ungrounded,
  conversation, and general messages; it deduplicates by start/end and sorts by
  time.
- `markdownText(value)` recursively extracts plain text from React nodes for
  clipboard copy.
- `MarkdownCodeBlock` renders a code block with a clipboard action and
  transient copied/error state. Its `copyCode()` helper does the clipboard
  write.
- `apiUrl(path)` validates the configured API origin and prefixes `/api`.
- `readCache(key)` and `writeCache(key, value)` read/write non-sensitive UI
  cache values in localStorage, with read/write failure warnings.
- `audioUploadKey(userId, file)` creates a stable resume key from owner,
  filename, size, and last-modified time.
- `getAudioObjectPath(userId, file)` reuses a saved owner-prefixed object path
  or creates a new UUID path for Storage.
- `uploadAudioDirect(...)` performs the small-file direct upload fallback to
  Supabase Storage.
- `uploadAudioToStorage(...)` configures tus-js-client, resumes prior uploads,
  reports progress, retries eligible network/server errors, and uses the
  direct fallback only for eligible small files.
- `isAbortError(reason)` detects abort/timeout-like errors, including nested
  causes.
- `userFacingError(reason, fallback)` extracts a useful message or safe
  fallback.
- `api<T>(path, init)` obtains the current Supabase session, sends its access
  token, sets JSON content type if appropriate, performs the fetch, handles
  timeout/network/non-JSON/HTTP errors, and returns typed JSON.
- `useTheme()` reads/stores the light/dark preference and applies it to
  `document.documentElement.dataset.theme`.

## App components

### `App`

Initializes auth state from `getSession()`, listens for auth changes, and
declares routes. It wraps the route tree in `MotionConfig` honoring the user's
reduced-motion preference. The authenticated `Dashboard` key is the user ID so
one user's React dashboard state is not reused for another.

### `Brand` and `ThemeButton`

Reusable brand link/logo and accessible theme toggle.

### `Landing`

Renders the marketing page and a once-per-tab splash state. `sessionStorage`
marks the splash as shown so subsequent renders in the same tab skip it.

### `SignIn`

Manages sign-in/sign-up form state, validates required browser inputs, calls
Supabase Auth, displays email-confirmation notice when sign-up returns no
session, and navigates to `/app` when signed in. Its `submit()` function is the
form handler. Password visibility is only a local input display toggle.

### `LoadingScreen`

Displays the app loading state while the auth session is being resolved.

## `Dashboard`

Owns authenticated library/detail state:

- Lectures/folders are cached under keys containing the user's ID and then
  refreshed from protected API routes.
- Selected lecture, folder filter, search/sort, modal state, upload progress,
  current chat, messages, draft, selected detail tab, transcript text, and
  errors are held in React state.
- `replaceDetailQuery()` updates `tab` and `chat` URL parameters without
  creating a browser history entry; `setView()` changes active tab and URL.
- `refresh()` loads lectures and folders in parallel and updates the user-keyed
  cache.
- Route-loading effect fetches lecture detail/chat sessions and restores
  requested chat messages from `?chat=...`.
- Active-processing effect polls lecture status and refreshes list after
  terminal states.
- `activeLectureIds` memoizes the set of processing lectures.
- `filteredLectures` memoizes search, folder filtering, and sort order.
- `openLecture()` navigates to a lecture route.
- `createSubject()` posts a subject and refreshes the folder list.
- `chooseChat()` changes the active session, updates URL/history state, and
  optionally loads its messages. A request counter prevents stale chat loads
  overwriting a newer selection.
- `createChat()` creates an empty chat session and switches to its tab.
- `deleteLecture()`, `deleteSubject()`, and `deleteChat()` confirm and call
  owner-scoped delete routes, then update local state.
- `retryLecture()` retries full lecture processing.
- `retryIndex()` retries vector indexing only.
- `onLectureCreated()`, `onUploadProgress()`, `onUploadComplete()`, and
  `onUploadFailed()` reconcile asynchronous upload status with UI state.
- `resumeAudioUpload()` opens the upload modal for an existing pending file.
- `showTranscript()` retrieves the full transcript text and selects the
  transcript tab; timed transcript rows are loaded by `TranscriptView`.
- `download()` fetches a protected file and triggers a browser download.
- `signOut()` removes that user's cached lecture/folder lists, invokes Supabase
  sign-out, then navigates home.

The component also renders the responsive sidebar, search/sort library, and
passes explicit callbacks/props to `LectureView` and `UploadModal`.

## Detail components

### `LectureView`

Composes the detail header, processing/error states, audio player, notes/chat/
transcript tabs, history sidebar, and download/delete/retry actions.

- `loadAudioUrl(retry)` asks Supabase Storage for a one-hour signed URL and
  allows one retry after a playback error.
- `onSeek(startSeconds, endSeconds)` remembers the active source key, displays
  a short "Playing from" status, sets playback to 1.5 seconds before the
  citation (clamped at zero), and plays.
- `onAudioTimeUpdate()` throttles playhead state updates with
  `requestAnimationFrame`; the timestamp feeds transcript highlighting.
- An effect cleans up pending animation frames and status timers.

The active citation key is passed to chat source chips for visual active-state
highlighting.

### `TranscriptView`

Loads owner-scoped timed segment rows from the API, filters by a search input,
highlights matched text, and offers clickable time buttons. The active row
follows `currentTime`; manual scrolling pauses automatic scrolling. Its
`highlight(text)` helper escapes the user's search string before building a
case-insensitive regex.

### `NotesView`

Renders summary, core explanations, announcements, exam points, and off-topic
remarks from saved notes. "Ask about this" callbacks switch to chat and prefill
a relevant prompt without submitting it automatically.

### `NoteSection`

Reusable expandable/section presentation with section actions. Its nested
`copySection()` copies section text and shows transient copy state.

### `ChatView`

Manages chat composer, send state, local error, history dropdown, copy state,
and scrolling behavior.

- `suggestions` are built from the first core explanations and an exam point,
  with three current prompts as fallback; suggestion clicks focus the
  textarea.
- Effects auto-grow the composer, scroll new messages to bottom only if the
  reader was already near the bottom, close the history dropdown on outside
  click/Escape, and poll persisted messages while an assistant response is
  pending.
- `sendQuestion(text, acceptOffer)` appends optimistic messages, creates a
  chat lazily if necessary, posts the request, swaps in persisted rows, and
  restores the draft/marks the optimistic response failed if the request errors.
- `submit(event)` submits the form.
- `copyMessage(message)` copies assistant response text.
- The Enter shortcut does not submit during IME composition; Shift+Enter remains
  a newline.
- An explicit "Answer generally" button sends `accept_offer=true`. Error
  messages can retry the preceding user question.
- Markdown is rendered with `react-markdown` and `remark-gfm`; code blocks get
  a copy action, tables are wrapped for horizontal scrolling, and source chips
  are keyboard-accessible buttons with snippets, time labels, and active state.
- The message list uses `role="log"` and polite live updates.

`displaySources()` makes the final frontend safety check: conversation/general
and explicitly ungrounded messages show no citations even if the payload
unexpectedly contains sources.

### `UploadModal`

Handles audio or transcript mode, file validation, metadata, progress, resume,
and errors. Its `submit()` either:

1. checks session, reserves/recovers an owner-scoped object path, creates an
   upload-pending lecture, uploads resumably to Storage, tells the API to start
   processing, and clears resume metadata; or
2. posts pasted transcript text directly to the lecture API.

Callbacks keep the dashboard/library consistent as upload state changes. A
failed upload after lecture creation is reported to the backend so its state
survives refresh.

## Browser and UI state boundaries

| State | Where it lives | Persistence |
|---|---|---|
| Auth session/refresh token | Supabase JS client's browser session storage mechanism (SDK default) | Supabase SDK persistence |
| Lectures/folders | React state plus `localStorage` cache keyed by user ID | Supabase is authoritative |
| Chat messages/sessions | React state while open; fetched from API | Supabase Postgres |
| Current tab/chat | React state and `?tab=...&chat=...` query parameters | URL survives refresh |
| Theme | React state and localStorage | Browser local preference |
| TUS resume fingerprint/path | tus-js-client storage and localStorage upload key | Browser; reused on same-file retry |
| Audio playback/time | `HTMLAudioElement` ref and React state | In-memory only |
| Draft question | React state; notes CTA can prefill it | In-memory only |

## UI package roles

Versions/ranges are declared in `frontend/package.json`.

| Package | Purpose |
|---|---|
| `react`, `react-dom` | Component rendering and browser UI. |
| `react-router-dom` | Client-side URL routes, redirects, and navigation. |
| `@supabase/supabase-js` | Browser Auth, Storage signed URL creation, and Supabase client. |
| `tus-js-client` | Resumable chunked uploads to Supabase Storage. |
| `motion` | UI transitions and reduced-motion-aware animation. |
| `lucide-react` | Icon components. |
| `react-markdown`, `remark-gfm` | Safe Markdown display and GitHub-Flavored Markdown such as tables. |
| `tailwindcss`, `@tailwindcss/vite` | Vite styling integration; most current app styling is authored in `style.css`. |
| `vite`, `@vitejs/plugin-react` | Dev server, bundler, and React transform. |
| `typescript`, `@types/react`, `@types/react-dom` | Type checking and React typings. |

## Build and static assets

`vite.config.ts` enables React and Tailwind plugins, proxies `/api` to local
FastAPI, and places major dependencies into manual output chunks. `index.html`
is the browser shell. `public/notesgraph.svg` is the app mark; `public/runtime-config.js`
is a local fallback, while production runtime configuration is supplied by the
FastAPI `/runtime-config.js` endpoint.
