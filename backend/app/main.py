"""NotesGraph API: authenticated lecture processing and owner-scoped data."""
import logging
import mimetypes
import os
import re
import asyncio
import json
import sys
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import auth, config, store
from .schemas import ChatMessage, ChatRequest, ChatTurn, FolderCreate, UploadFailure

log = logging.getLogger("notesgraph")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log.setLevel(logging.INFO)

MAX_UPLOAD_MB = 500
_processing_tasks: set[asyncio.Task] = set()

API_DESCRIPTION = """
NotesGraph API for authenticated lecture libraries, transcription, notes,
timestamp-aware lecture chat, and exports.

## Authentication

Protected `/api` endpoints require a Supabase access token:

`Authorization: Bearer <access_token>`

Use the **Authorize** button in Swagger UI to provide the access token obtained
from Supabase Auth. Do not use the Supabase publishable key or server secret as
the bearer token. The API validates the token with Supabase Auth and derives the
owner ID from the validated user; callers cannot select another owner.

## Lecture processing

Lecture creation and chat generation are asynchronous. Creation returns
`202 Accepted` with the current lecture/turn state. Poll the lecture detail or
chat messages endpoint until processing completes. Audio is limited to 500 MiB.
For browser uploads, first create a lecture using an owner-prefixed private
Storage path, upload the object to Supabase Storage, and then call
`/api/lectures/{lecture_id}/start-upload`.

## Chat sources

Source timestamps are built by the API from stored Whisper segment metadata.
They identify seconds from the beginning of the original recording. Source
objects may have `start` equal to `end` for a single moment, or an `end` greater
than `start` for a range. Conversation and general-knowledge answers are not
lecture-grounded and return no sources.

## Errors

Errors use FastAPI's standard JSON shape, generally `{"detail": "..."}`.
Protected endpoints return `401` when the bearer token is missing or invalid.
Resource lookups are owner-scoped; resources not owned by the authenticated
user are reported as not found.
"""

OPENAPI_TAGS = [
    {"name": "System", "description": "Health and deployment configuration endpoints."},
    {"name": "Subjects", "description": "Create, list, and delete per-user subject folders."},
    {"name": "Lectures", "description": "Create, inspect, process, retry, reindex, and delete lectures."},
    {"name": "Transcript", "description": "Retrieve transcript text and timed Whisper segments."},
    {"name": "Exports", "description": "Download generated lecture notes."},
    {"name": "Chat", "description": "Manage lecture chat sessions, messages, and grounded answers."},
]


@asynccontextmanager
async def lifespan(_: FastAPI):
    log.info(
        "NotesGraph backend ready (Supabase=%s, Gemini=%s, Groq=%s)",
        bool(config.SUPABASE_URL and config.SUPABASE_SECRET_KEY),
        bool(config.GOOGLE_API_KEY),
        bool(config.GROQ_API_KEY),
    )
    yield
    log.info("NotesGraph backend stopping (%d processing task(s) active)", len(_processing_tasks))


app = FastAPI(
    title="NotesGraph AI API",
    summary="Authenticated lecture processing and timestamp-aware study chat.",
    description=API_DESCRIPTION,
    version="1.0.0",
    openapi_url="/api/openapi.json",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    servers=[
        {
            "url": "https://notesgraph-ai.onrender.com",
            "description": "Production",
        },
        {"url": "http://localhost:8000", "description": "Local development"},
    ],
    openapi_tags=OPENAPI_TAGS,
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.FRONTEND_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.middleware("http")
async def log_http_requests(request, call_next):
    started = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("HTTP request failed: %s %s", request.method, request.url.path)
        raise
    message = "HTTP %s %s -> %s (%.0f ms)"
    args = (
        request.method,
        request.url.path,
        response.status_code,
        (time.monotonic() - started) * 1000,
    )
    if response.status_code >= 500:
        log.error(message, *args)
    elif response.status_code >= 400:
        log.warning(message, *args)
    else:
        log.info(message, *args)
    return response


def _schedule_processing(
    lecture_id: str, user_id: str, audio_path: Optional[Path], language: Optional[str]
) -> None:
    task = asyncio.create_task(
        asyncio.to_thread(process_lecture, lecture_id, user_id, audio_path, language),
        name=f"process-lecture-{lecture_id}",
    )
    _processing_tasks.add(task)

    def finished(completed: asyncio.Task) -> None:
        _processing_tasks.discard(completed)
        if completed.cancelled():
            log.warning("Processing task cancelled for lecture %s", lecture_id)
        elif error := completed.exception():
            log.error(
                "Unexpected processing task failure for lecture %s",
                lecture_id,
                exc_info=(type(error), error, error.__traceback__),
            )

    task.add_done_callback(finished)
    log.info("Scheduled processing for lecture %s", lecture_id)


def process_lecture(
    lecture_id: str, user_id: str, audio_path: Optional[Path], language: Optional[str]
) -> None:
    """Transcribe, summarize, and index one lecture belonging to an authenticated user."""
    started = time.monotonic()
    log.info("Lecture processing started: %s", lecture_id)
    try:
        from .audio import transcribe_audio_with_segments
        from .notes import generate_notes
        from .rag import index_lecture

        meta = store.get(lecture_id, user_id)
        if meta is None:
            raise RuntimeError("Lecture no longer exists.")
        if audio_path and audio_path.exists() and not meta.get("audio_path"):
            extension = Path(meta["source"]).suffix.lower()
            content_type = mimetypes.guess_type(meta["source"])[0] or "application/octet-stream"
            store.update(
                lecture_id,
                user_id,
                progress="Saving recording securely",
            )
            log.info("Secure audio storage started for lecture %s", lecture_id)
            object_path = store.upload_audio(
                user_id,
                lecture_id,
                str(audio_path),
                extension,
                content_type,
            )
            meta = store.update(lecture_id, user_id, audio_path=object_path)
            log.info("Secure audio storage completed for lecture %s", lecture_id)
        transcript = store.load_transcript(lecture_id, user_id)
        segments = store.load_transcript_segments(lecture_id, user_id)
        if not transcript:
            if audio_path is None or not audio_path.exists():
                audio_path = _temp_audio_from_storage(meta)
            if audio_path is None or not audio_path.exists():
                raise RuntimeError("Audio file is missing; upload the lecture again.")
            store.update(lecture_id, user_id, status="transcribing", error="")
            log.info("Transcription started for lecture %s", lecture_id)
            try:
                transcript, segments = transcribe_audio_with_segments(
                    audio_path,
                    language,
                    progress=lambda message: store.update(lecture_id, user_id, progress=message),
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Groq Whisper model '{config.WHISPER_MODEL}' failed during transcription: {exc}"
                ) from exc
            if not transcript.strip():
                raise RuntimeError("No speech was detected in the audio.")
            store.save_transcript(lecture_id, user_id, transcript, segments)
            log.info(
                "Transcription completed for lecture %s: %d characters, %d timed segments",
                lecture_id,
                len(transcript),
                len(segments),
            )

        log.info("Notes generation started for lecture %s", lecture_id)
        notes_started = time.monotonic()
        store.update(
            lecture_id, user_id, status="generating_notes", progress="Writing structured notes", error=""
        )
        try:
            notes = generate_notes(
                transcript,
                meta["title"],
                meta["subject"],
                lambda message: store.update(
                    lecture_id, user_id, progress=message
                ),
            )
        except Exception as exc:
            raise RuntimeError(f"Note generation failed: {exc}") from exc
        log.info(
            "Notes generation completed for lecture %s (%.1f s)",
            lecture_id,
            time.monotonic() - notes_started,
        )
        notes_data = notes.model_dump()
        store.save_notes(lecture_id, user_id, notes_data)
        fields = {}
        if not meta.get("title_provided"):
            fields["title"] = _allocate_title(notes_data, transcript, meta["subject"])
        if not meta["subject"] and notes.subject:
            fields["subject"] = notes.subject
        if fields:
            store.update(lecture_id, user_id, **fields)

        store.update(lecture_id, user_id, status="indexing", progress="Indexing for chat")
        log.info("Vector indexing started for lecture %s", lecture_id)
        try:
            index_lecture(lecture_id, transcript, user_id, segments)
        except Exception as exc:
            log.exception("Chat indexing failed for lecture %s; saved notes remain available", lecture_id)
            store.update(
                lecture_id,
                user_id,
                status="index_error",
                progress="Notes ready; retrying chat indexing",
                error=f"Your notes are ready, but temporary embedding service errors prevented chat indexing: {str(exc)[:350]}",
            )
            return
        store.update(lecture_id, user_id, status="done", progress="Ready", error="")
        log.info(
            "Lecture processing completed: %s (%.1f s)",
            lecture_id,
            time.monotonic() - started,
        )
    except Exception as exc:
        log.exception("Processing failed for lecture %s", lecture_id)
        try:
            failed_meta = store.get(lecture_id, user_id)
            if failed_meta and not failed_meta.get("title_provided"):
                transcript = store.load_transcript(lecture_id, user_id) or failed_meta.get("source", "")
                fallback_title = _allocate_title({}, transcript, failed_meta.get("subject", ""))
                store.update(lecture_id, user_id, title=fallback_title)
            store.update(lecture_id, user_id, status="error", progress="Failed", error=str(exc)[:500])
        except Exception:
            log.exception("Could not save processing failure for lecture %s", lecture_id)
    finally:
        if audio_path:
            audio_path.unlink(missing_ok=True)


def process_indexing(lecture_id: str, user_id: str) -> None:
    """Rebuild only the chat index for a lecture whose notes are already saved."""
    from .rag import index_lecture

    log.info("Chat indexing retry started for lecture %s", lecture_id)
    try:
        transcript = store.load_transcript(lecture_id, user_id)
        if not transcript:
            raise RuntimeError("The saved transcript is missing; re-upload this lecture.")
        segments = store.load_transcript_segments(lecture_id, user_id)
        store.update(
            lecture_id, user_id, status="indexing", progress="Retrying chat indexing", error=""
        )
        index_lecture(lecture_id, transcript, user_id, segments)
        store.update(lecture_id, user_id, status="done", progress="Ready", error="")
        log.info("Chat indexing retry completed for lecture %s", lecture_id)
    except Exception as exc:
        log.exception("Chat indexing retry failed for lecture %s", lecture_id)
        try:
            store.update(
                lecture_id,
                user_id,
                status="index_error",
                progress="Notes ready; chat indexing needs another retry",
                error=f"Your notes are ready, but chat indexing failed: {str(exc)[:350]}",
            )
        except Exception:
            log.exception("Could not save chat indexing error for lecture %s", lecture_id)


def _schedule_indexing(lecture_id: str, user_id: str, progress: str) -> dict:
    store.update(lecture_id, user_id, status="indexing", progress=progress, error="")
    task = asyncio.create_task(
        asyncio.to_thread(process_indexing, lecture_id, user_id),
        name=f"index-lecture-{lecture_id}",
    )
    _processing_tasks.add(task)

    def finished(completed: asyncio.Task) -> None:
        _processing_tasks.discard(completed)
        if completed.cancelled():
            log.warning("Chat indexing task cancelled for lecture %s", lecture_id)
        elif error := completed.exception():
            log.error(
                "Unexpected chat indexing task failure for lecture %s",
                lecture_id,
                exc_info=(type(error), error, error.__traceback__),
            )

    task.add_done_callback(finished)
    log.info("Scheduled chat indexing for lecture %s", lecture_id)
    return store.get(lecture_id, user_id)


def process_chat_answer(
    lecture_id: str,
    session_id: str,
    user_id: str,
    question: str,
    assistant_message_id: int,
    history: list[dict],
    accept_offer: bool = False,
) -> None:
    """Generate an answer for a persisted chat turn and save its terminal state."""
    from .rag import ask

    log.info("Chat answer generation started for session %s", session_id)
    try:
        result = ask(
            lecture_id,
            session_id,
            question,
            user_id=user_id,
            history=history,
            accept_offer=accept_offer,
        )
        store.complete_chat_reply(
            assistant_message_id,
            session_id,
            user_id,
            result["answer"],
            result.get("sources") or [],
            result["intent"],
            result["grounded"],
            result.get("pending_offer"),
        )
        if result.get("consumed_offer_message_id") is not None:
            store.clear_pending_offer(
                result["consumed_offer_message_id"], session_id, user_id
            )
        log.info("Chat answer generation completed for session %s", session_id)
    except Exception:
        log.exception("Chat answer generation failed for session %s", session_id)
        try:
            store.fail_chat_reply(assistant_message_id, session_id, user_id)
        except Exception:
            log.exception("Could not save chat failure state for session %s", session_id)


def _require(lecture_id: str, user_id: str) -> dict:
    meta = store.get(lecture_id, user_id)
    if meta is None:
        raise HTTPException(404, "Lecture not found")
    return meta


def _validate_storage_upload(storage_path: str, source_filename: str, user_id: str) -> str:
    owner, separator, object_name = storage_path.partition("/")
    stem, dot, extension = object_name.rpartition(".")
    if (
        not separator
        or owner != user_id
        or "/" in object_name
        or "\\" in storage_path
        or not dot
        or f".{extension.lower()}" not in config.ALLOWED_AUDIO_EXT
    ):
        raise HTTPException(400, "Invalid audio storage path.")
    try:
        uuid.UUID(stem)
    except ValueError as exc:
        raise HTTPException(400, "Invalid audio storage path.") from exc

    filename = Path(source_filename.replace("\\", "/")).name
    if not filename or Path(filename).suffix.lower() != f".{extension.lower()}":
        raise HTTPException(400, "Audio filename does not match the uploaded file.")
    return filename


def _allocate_title(notes: dict, transcript: str, subject: str) -> str:
    topics = notes.get("core_explanations") or []
    topic = str(topics[0].get("topic") or "").strip() if topics else ""
    if topic:
        return topic

    candidate = str(notes.get("title") or "").strip()
    normalized = re.sub(r"[\s.!]+", "", candidate.casefold())
    normalized_subject = re.sub(r"[\W_]+", "", subject.casefold())
    title_without_generic_suffix = re.sub(
        r"\b(lecture|notes|chapter|class|session)\b", "", candidate, flags=re.IGNORECASE
    )
    normalized_candidate_core = re.sub(r"[\W_]+", "", title_without_generic_suffix.casefold())
    if normalized not in {
        "",
        "untitled",
        "untitledlecture",
        "lecture",
        "lecturenotes",
        "notes",
        "generatingtitle",
    } and (not normalized_subject or normalized_candidate_core != normalized_subject):
        return candidate
    lines = re.sub(r"\s+", " ", transcript).strip().splitlines()
    first_line = lines[0] if lines else ""
    sentence = re.split(r"(?<=[.!?])\s+", first_line, maxsplit=1)[0]
    words = sentence.split()
    title = " ".join(words[:10]).strip(" ,.:;!?-")
    if len(title) > 68:
        title = title[:65].rsplit(" ", 1)[0] + "..."
    return title or (f"{subject.strip()} topic" if subject.strip() else "Lecture notes")


def _temp_audio_from_storage(meta: dict) -> Optional[Path]:
    if not meta.get("audio_path"):
        return None
    data = store.download_audio(meta["audio_path"])
    suffix = Path(meta["source"]).suffix.lower()
    with tempfile.NamedTemporaryFile(dir=config.UPLOADS_DIR, suffix=suffix, delete=False) as audio_file:
        audio_file.write(data)
        return Path(audio_file.name)


def _is_missing_chat_schema_column(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(
        (f"chat_messages.{column}" in message and "42703" in message)
        or f"column chat_messages.{column} does not exist" in message
        for column in ("status", "intent", "grounded", "pending_offer")
    )


def _chat_schema_migration_error() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail=(
            "The chat database schema needs an update. Run "
            "backend/supabase/migrations/20261106_chat_grounding_metadata.sql, then "
            "backend/supabase/migrations/20261106_chat_pending_offer_and_intents.sql "
            "in your Supabase SQL Editor, then retry."
        ),
    )


@app.get(
    "/api/health",
    tags=["System"],
    summary="Check API health",
    description="Public deployment health check. Provider fields indicate configured credentials, not whether they are valid.",
)
def health():
    log.info("Health check requested")
    return {
        "ok": True,
        "google_key": bool(config.GOOGLE_API_KEY),
        "groq_key": bool(config.GROQ_API_KEY),
        "supabase_configured": bool(
            config.SUPABASE_URL
            and config.SUPABASE_PUBLISHABLE_KEY
            and config.SUPABASE_SECRET_KEY
        ),
        "gemini_model": config.GEMINI_MODEL,
    }


@app.get("/runtime-config.js", include_in_schema=False)
def runtime_config():
    """Expose browser-safe deployment configuration without baking it into the image."""
    settings = {
        "supabaseUrl": os.getenv("VITE_SUPABASE_URL", ""),
        "supabasePublishableKey": os.getenv("VITE_SUPABASE_PUBLISHABLE_KEY", ""),
        "apiUrl": os.getenv("VITE_API_URL", ""),
    }
    return Response(
        f"window.NOTESGRAPH_CONFIG = {json.dumps(settings)};",
        media_type="application/javascript",
        headers={"Cache-Control": "no-store"},
    )


@app.get(
    "/api/lectures",
    tags=["Lectures"],
    summary="List your lectures",
    description="Returns lecture summaries for the authenticated user, newest first. Transcript, segment, and notes payloads are fetched from the lecture detail route.",
)
def list_lectures(user_id: str = Depends(auth.current_user)):
    return store.list_all(user_id)


@app.get(
    "/api/folders",
    tags=["Subjects"],
    summary="List your subjects",
    description="Returns subject folders belonging to the authenticated user.",
)
def list_folders(user_id: str = Depends(auth.current_user)):
    return store.list_folders(user_id)


@app.post(
    "/api/folders",
    status_code=201,
    tags=["Subjects"],
    summary="Create or reuse a subject",
    description="Normalizes whitespace and creates a subject folder for the authenticated user. A folder with the same normalized name is reused.",
)
def create_folder(payload: FolderCreate, user_id: str = Depends(auth.current_user)):
    name = " ".join(payload.name.split())
    if not name:
        raise HTTPException(422, "Subject name cannot be empty.")
    folder = store.ensure_folder(user_id, name)
    if folder is None:
        raise RuntimeError("The subject folder could not be created.")
    return folder


@app.delete(
    "/api/folders/{folder_id}",
    status_code=204,
    tags=["Subjects"],
    summary="Delete an empty subject",
    description="Deletes an owned subject folder only when it contains no lectures. Returns 409 when the folder is not empty.",
)
def delete_folder(folder_id: str, user_id: str = Depends(auth.current_user)):
    result = store.delete_folder(user_id, folder_id)
    if result == "not_found":
        raise HTTPException(404, "Subject not found")
    if result == "not_empty":
        raise HTTPException(
            409,
            "Move or delete the subject's lectures before deleting this subject.",
        )
    return Response(status_code=204)


@app.post(
    "/api/lectures",
    status_code=202,
    tags=["Lectures"],
    summary="Create and process a lecture",
    description="Accepts an audio upload, a pasted transcript, or metadata for an audio object already uploaded to the private Supabase Storage bucket. Processing runs asynchronously. Direct Storage uploads must set storage_path, source_filename, and defer_processing=true, then call start-upload after the object upload completes. Maximum audio file size is 500 MiB.",
)
async def create_lecture(
    file: Optional[UploadFile] = File(None),
    transcript_text: str = Form(""),
    title: str = Form(""),
    subject: str = Form(""),
    language: str = Form(""),
    storage_path: str = Form(""),
    source_filename: str = Form(""),
    defer_processing: bool = Form(False),
    user_id: str = Depends(auth.current_user),
):
    """Upload a recording, or paste a transcript, and start processing."""
    transcript_text = transcript_text.strip()
    storage_path = storage_path.strip()
    if (file is None or not file.filename) and not transcript_text and not storage_path:
        raise HTTPException(400, "Provide an audio file or a transcript.")
    if defer_processing and not storage_path:
        raise HTTPException(400, "Deferred processing requires a secure audio storage path.")

    audio_path: Optional[Path] = None
    if storage_path:
        if (file is not None and file.filename) or transcript_text:
            raise HTTPException(400, "Provide only one audio file or transcript.")
        filename = _validate_storage_upload(storage_path, source_filename, user_id)
        try:
            meta = store.create(
                user_id,
                title,
                subject,
                filename,
                audio_path=storage_path,
                status="uploading" if defer_processing else "queued",
                progress="Uploading recording to secure storage" if defer_processing else "Waiting to start",
            )
        except Exception:
            log.exception("Could not create lecture record for user %s", user_id)
            raise HTTPException(
                status_code=502,
                detail="The lecture could not be saved. Check the backend console logs and Supabase configuration.",
            ) from None
        log.info("Audio stored for user %s: %s", user_id, storage_path)
    elif file is not None and file.filename:
        extension = Path(file.filename).suffix.lower()
        if extension not in config.ALLOWED_AUDIO_EXT:
            raise HTTPException(
                400,
                f"Unsupported file type '{extension}'. Use: {', '.join(sorted(config.ALLOWED_AUDIO_EXT))}",
            )
        too_large = False
        with tempfile.NamedTemporaryFile(dir=config.UPLOADS_DIR, suffix=extension, delete=False) as temp:
            audio_path = Path(temp.name)
            size = 0
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_MB * 1024 * 1024:
                    too_large = True
                    break
                temp.write(chunk)
        if too_large:
            audio_path.unlink(missing_ok=True)
            raise HTTPException(413, f"File is larger than {MAX_UPLOAD_MB} MB.")
        try:
            log.info(
                "Audio received for user %s: %s (%d bytes)",
                user_id,
                extension,
                size,
            )
            meta = store.create(user_id, title, subject, Path(file.filename).name)
        except Exception:
            log.exception("Could not create lecture record for user %s", user_id)
            audio_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=502,
                detail="The lecture could not be saved. Check the backend console logs and Supabase configuration.",
            ) from None
    else:
        meta = store.create(user_id, title, subject, "pasted transcript")
        store.save_transcript(meta["id"], user_id, transcript_text)

    lang = re.sub(r"[^a-z]", "", language.lower())[:3] or None
    if not defer_processing:
        _schedule_processing(meta["id"], user_id, audio_path, lang)
    log.info("Lecture accepted: %s (user %s)", meta["id"], user_id)
    return meta


@app.post(
    "/api/lectures/{lecture_id}/start-upload",
    status_code=202,
    tags=["Lectures"],
    summary="Start processing an uploaded recording",
    description="Call after the browser finishes uploading the lecture's audio object to private Supabase Storage. Only lectures awaiting upload can be started.",
)
async def start_uploaded_lecture(lecture_id: str, user_id: str = Depends(auth.current_user)):
    meta = _require(lecture_id, user_id)
    if meta["status"] not in ("uploading", "upload_error") or not meta.get("audio_path"):
        raise HTTPException(409, "This lecture is not waiting for an audio upload.")
    updated = store.update(
        lecture_id, user_id, status="queued", progress="Waiting to start", error=""
    )
    _schedule_processing(lecture_id, user_id, None, None)
    return updated


@app.post(
    "/api/lectures/{lecture_id}/upload-failed",
    tags=["Lectures"],
    summary="Record an upload failure",
    description="Persists the failure state for an owned lecture whose audio upload did not complete.",
)
def fail_lecture_upload(
    lecture_id: str,
    payload: UploadFailure,
    user_id: str = Depends(auth.current_user),
):
    meta = _require(lecture_id, user_id)
    if meta["status"] not in ("uploading", "upload_error"):
        raise HTTPException(409, "This lecture is not currently uploading.")
    return store.update(
        lecture_id,
        user_id,
        status="upload_error",
        progress="Audio upload failed",
        error=f"Audio upload failed: {payload.error.strip()[:350]}",
    )


@app.get(
    "/api/lectures/{lecture_id}",
    tags=["Lectures"],
    summary="Get lecture details",
    description="Returns the authenticated user's lecture metadata and structured notes when available.",
)
def get_lecture(lecture_id: str, user_id: str = Depends(auth.current_user)):
    meta = _require(lecture_id, user_id)
    return {**meta, "notes": store.load_notes(lecture_id, user_id)}


@app.get(
    "/api/lectures/{lecture_id}/transcript",
    tags=["Transcript"],
    summary="Get transcript text",
    description="Returns the complete saved transcript as UTF-8 plain text. Use the segments endpoint when timed transcript rows are needed.",
)
def get_transcript(lecture_id: str, user_id: str = Depends(auth.current_user)):
    _require(lecture_id, user_id)
    text = store.load_transcript(lecture_id, user_id)
    if text is None:
        raise HTTPException(404, "Transcript not ready")
    return Response(text, media_type="text/plain; charset=utf-8")


@app.get(
    "/api/lectures/{lecture_id}/segments",
    tags=["Transcript"],
    summary="List timed transcript segments",
    description="Returns valid {start, end, text} Whisper segments for an owned lecture. Times are seconds relative to the original recording. Word-level timings are not provided.",
)
def get_transcript_segments(
    lecture_id: str, user_id: str = Depends(auth.current_user)
):
    _require(lecture_id, user_id)
    if store.load_transcript(lecture_id, user_id) is None:
        raise HTTPException(404, "Transcript not ready")
    segments = store.load_transcript_segments(lecture_id, user_id)
    return [
        {
            "start": segment["start"],
            "end": segment["end"],
            "text": segment["text"],
        }
        for segment in segments
        if isinstance(segment, dict)
        and segment.get("start") is not None
        and segment.get("end") is not None
        and isinstance(segment.get("text"), str)
    ]


@app.get(
    "/api/lectures/{lecture_id}/notes.pdf",
    tags=["Exports"],
    summary="Download notes as PDF",
    description="Renders the saved structured notes to an A4 PDF. Notes must be ready.",
)
def download_pdf(lecture_id: str, user_id: str = Depends(auth.current_user)):
    from .render import notes_to_pdf

    meta = _require(lecture_id, user_id)
    notes = store.load_notes(lecture_id, user_id)
    if not notes:
        raise HTTPException(404, "Notes not ready")
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", meta["title"]).strip("_") or "lecture_notes"
    return Response(
        notes_to_pdf(notes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{name}.pdf"'},
    )


@app.get(
    "/api/lectures/{lecture_id}/notes.md",
    tags=["Exports"],
    summary="Download notes as Markdown",
    description="Renders the saved structured notes as Markdown. Notes must be ready.",
)
def download_markdown(lecture_id: str, user_id: str = Depends(auth.current_user)):
    from .render import notes_to_markdown

    meta = _require(lecture_id, user_id)
    notes = store.load_notes(lecture_id, user_id)
    if not notes:
        raise HTTPException(404, "Notes not ready")
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", meta["title"]).strip("_") or "lecture_notes"
    return Response(
        notes_to_markdown(notes),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}.md"'},
    )


@app.post(
    "/api/lectures/{lecture_id}/retry",
    status_code=202,
    tags=["Lectures"],
    summary="Retry lecture processing",
    description="Requeues a failed lecture when its stored audio or transcript is available.",
)
async def retry_lecture(
    lecture_id: str, user_id: str = Depends(auth.current_user)
):
    meta = _require(lecture_id, user_id)
    if meta["status"] not in ("error", "queued"):
        raise HTTPException(409, "Lecture is not in a failed state.")
    if not meta.get("audio_path") and not store.load_transcript(lecture_id, user_id):
        raise HTTPException(
            409,
            "The recording was not saved successfully. Upload the audio again to retry processing.",
        )
    try:
        updated = store.update(
            lecture_id, user_id, status="queued", progress="Retrying", error=""
        )
    except Exception as exc:
        log.exception("Could not queue processing retry for lecture %s", lecture_id)
        raise HTTPException(
            status_code=503,
            detail=f"Could not queue this lecture for retry: {str(exc)[:300]}",
        ) from exc
    _schedule_processing(lecture_id, user_id, None, None)
    return updated


@app.post(
    "/api/lectures/{lecture_id}/retry-index",
    status_code=202,
    tags=["Lectures"],
    summary="Retry failed chat indexing",
    description="Rebuilds vector chunks from saved transcript data after an indexing failure; does not retranscribe the audio or regenerate notes.",
)
async def retry_index(lecture_id: str, user_id: str = Depends(auth.current_user)):
    meta = _require(lecture_id, user_id)
    if meta["status"] != "index_error":
        raise HTTPException(409, "Chat indexing does not need a retry.")
    return _schedule_indexing(lecture_id, user_id, "Retrying chat indexing")


@app.post(
    "/api/lectures/{lecture_id}/reindex",
    status_code=202,
    tags=["Lectures"],
    summary="Reindex a lecture",
    description="Rebuilds the vector index from saved transcript and Whisper segment data without retranscription. Completed or indexing-error lectures with saved segments are required.",
)
async def reindex_lecture(lecture_id: str, user_id: str = Depends(auth.current_user)):
    meta = _require(lecture_id, user_id)
    if meta["status"] not in ("done", "index_error"):
        raise HTTPException(409, "Only a completed lecture can be reindexed.")
    if not store.load_transcript(lecture_id, user_id):
        raise HTTPException(409, "The stored transcript is missing; this lecture cannot be reindexed.")
    if not store.load_transcript_segments(lecture_id, user_id):
        raise HTTPException(
            409,
            "Stored Whisper segments are missing; reindexing cannot create timestamps without retranscription.",
        )
    return _schedule_indexing(lecture_id, user_id, "Rebuilding citation index")


@app.get(
    "/api/chat-sessions",
    tags=["Chat"],
    summary="List chat sessions",
    description="Lists the authenticated user's chat sessions. Optionally filter by an owned lecture_id.",
)
def list_chat_sessions(
    lecture_id: Optional[str] = None, user_id: str = Depends(auth.current_user)
):
    if lecture_id:
        _require(lecture_id, user_id)
    return store.list_chat_sessions(user_id, lecture_id)


@app.post(
    "/api/lectures/{lecture_id}/chat-sessions",
    status_code=201,
    tags=["Chat"],
    summary="Create a chat session",
    description="Creates a persistent chat session belonging to the authenticated user and the specified owned lecture.",
)
def create_chat_session(lecture_id: str, user_id: str = Depends(auth.current_user)):
    _require(lecture_id, user_id)
    return store.create_chat_session(user_id, lecture_id)


@app.get(
    "/api/chat-sessions/{session_id}/messages",
    response_model=list[ChatMessage],
    tags=["Chat"],
    summary="List messages in a chat",
    description="Returns ordered messages, including sources, intent, grounding, pending offers, and processing status. Stale pending replies may be marked as errors when read.",
)
def get_chat_messages(session_id: str, user_id: str = Depends(auth.current_user)):
    if store.get_chat_session(session_id, user_id) is None:
        raise HTTPException(404, "Chat session not found")
    try:
        return store.get_chat_messages(session_id, user_id)
    except Exception as exc:
        if _is_missing_chat_schema_column(exc):
            raise _chat_schema_migration_error() from exc
        raise


@app.post(
    "/api/lectures/{lecture_id}/chat",
    status_code=202,
    response_model=ChatTurn,
    tags=["Chat"],
    summary="Ask a question about a lecture",
    description="Persists the user question and a pending assistant message, then generates the answer asynchronously. Poll the chat messages endpoint until the assistant status is complete or error. Conversation/general answers are not lecture-grounded and have no source chips. Set accept_offer=true to accept the immediately preceding general-answer offer.",
)
async def chat(
    lecture_id: str, req: ChatRequest, user_id: str = Depends(auth.current_user)
):
    meta = _require(lecture_id, user_id)
    if meta["status"] != "done":
        raise HTTPException(409, "This lecture is still being processed.")
    session = store.get_chat_session(req.session_id, user_id)
    if session is None or session["lecture_id"] != lecture_id:
        raise HTTPException(404, "Chat session not found")
    try:
        history = [
            row
            for row in store.get_chat_messages(req.session_id, user_id)
            if row.get("status", "complete") == "complete"
        ]
        user_message, assistant_message = store.create_chat_turn(
            req.session_id, user_id, req.message
        )
    except Exception as exc:
        log.exception("Chat question could not be saved")
        if _is_missing_chat_schema_column(exc):
            raise _chat_schema_migration_error() from exc
        raise HTTPException(503, "Your question could not be saved. Please try again.") from exc
    if session["title"] == "New chat":
        try:
            store.update_chat_title(req.session_id, user_id, req.message[:80])
        except Exception:
            log.exception("Chat question was saved, but the chat title could not be updated")

    task = asyncio.create_task(
        asyncio.to_thread(
            process_chat_answer,
            lecture_id,
            req.session_id,
            user_id,
            req.message,
            assistant_message["id"],
            history,
            req.accept_offer,
        ),
        name=f"answer-chat-{assistant_message['id']}",
    )
    _processing_tasks.add(task)

    def finished(completed: asyncio.Task) -> None:
        _processing_tasks.discard(completed)
        if completed.cancelled():
            log.warning("Chat answer task cancelled for session %s", req.session_id)
        elif error := completed.exception():
            log.error(
                "Unexpected chat answer task failure for session %s",
                req.session_id,
                exc_info=(type(error), error, error.__traceback__),
            )

    task.add_done_callback(finished)
    log.info("Accepted chat question for session %s", req.session_id)
    return {"user_message": user_message, "assistant_message": assistant_message}


@app.delete(
    "/api/chat-sessions/{session_id}/messages",
    status_code=204,
    tags=["Chat"],
    summary="Clear chat messages",
    description="Deletes all messages from an owned chat session while retaining the session itself.",
)
def reset_chat(session_id: str, user_id: str = Depends(auth.current_user)):
    if store.get_chat_session(session_id, user_id) is None:
        raise HTTPException(404, "Chat session not found")
    store.clear_chat_session(session_id, user_id)
    from .rag import clear_history

    clear_history(user_id, session_id)
    return Response(status_code=204)


@app.delete(
    "/api/chat-sessions/{session_id}",
    status_code=204,
    tags=["Chat"],
    summary="Delete a chat session",
    description="Deletes an owned chat session and its messages.",
)
def delete_chat(session_id: str, user_id: str = Depends(auth.current_user)):
    if not store.delete_chat_session(session_id, user_id):
        raise HTTPException(404, "Chat session not found")
    from .rag import clear_history

    clear_history(user_id, session_id)
    return Response(status_code=204)


@app.delete(
    "/api/lectures/{lecture_id}",
    status_code=204,
    tags=["Lectures"],
    summary="Delete a lecture",
    description="Deletes the authenticated user's lecture and cascading database records, removes its private audio object, and clears process-local chat history.",
)
def delete_lecture(lecture_id: str, user_id: str = Depends(auth.current_user)):
    _require(lecture_id, user_id)
    from .rag import clear_lecture_history

    clear_lecture_history(user_id, lecture_id)
    store.delete(lecture_id, user_id)
    return Response(status_code=204)


class SPAStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if (
                exc.status_code != 404
                or scope["path"] == "/api"
                or scope["path"].startswith("/api/")
                or scope["path"] == "/runtime-config.js"
                or Path(path).suffix
            ):
                raise
            return await super().get_response("index.html", scope)


if config.FRONTEND_DIST.is_dir():
    app.mount("/", SPAStaticFiles(directory=config.FRONTEND_DIST, html=True), name="frontend")
