"""Supabase-backed, owner-scoped persistence for lectures and chat history."""
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

from supabase import Client, create_client

from . import config
from .utils import with_retry


@lru_cache(maxsize=1)
def client() -> Client:
    if not config.SUPABASE_URL or not config.SUPABASE_SECRET_KEY:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SECRET_KEY must be configured.")
    return create_client(config.SUPABASE_URL, config.SUPABASE_SECRET_KEY)


def _one(query) -> Optional[dict]:
    result = query.limit(1).execute().data
    return result[0] if result else None


def ensure_folder(user_id: str, subject: str) -> Optional[dict]:
    name = " ".join(subject.split()).strip()
    if not name:
        return None
    result = (
        client()
        .table("folders")
        .upsert({"user_id": user_id, "name": name}, on_conflict="user_id,name")
        .execute()
    )
    if result.data:
        return result.data[0]
    return _one(client().table("folders").select("id,name").eq("user_id", user_id).eq("name", name))


def create(
    user_id: str,
    title: str,
    subject: str,
    source: str,
    audio_path: Optional[str] = None,
    status: str = "queued",
    progress: str = "Waiting to start",
) -> dict:
    folder = ensure_folder(user_id, subject)
    row = {
        "id": str(uuid4()),
        "user_id": user_id,
        "title": title.strip() or "Generating title…",
        "title_provided": bool(title.strip()),
        "subject": subject.strip(),
        "folder_id": folder["id"] if folder else None,
        "source": source,
        "audio_path": audio_path,
        "status": status,
        "progress": progress,
        "error": "",
    }
    return client().table("lectures").insert(row).execute().data[0]


def get(lecture_id: str, user_id: str) -> Optional[dict]:
    return _one(
        client().table("lectures").select("*").eq("id", lecture_id).eq("user_id", user_id)
    )


def update(lecture_id: str, user_id: str, **fields) -> dict:
    if "subject" in fields and fields["subject"]:
        folder = ensure_folder(user_id, fields["subject"])
        fields["folder_id"] = folder["id"] if folder else None
    result = (
        client()
        .table("lectures")
        .update(fields)
        .eq("id", lecture_id)
        .eq("user_id", user_id)
        .execute()
    )
    if not result.data:
        raise KeyError(lecture_id)
    return result.data[0]


def list_all(user_id: str) -> list:
    return (
        client()
        .table("lectures")
        .select("id,user_id,title,subject,folder_id,source,status,progress,error,created_at")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .execute()
        .data
    )


def list_folders(user_id: str) -> list:
    return (
        client()
        .table("folders")
        .select("id,name,created_at")
        .eq("user_id", user_id)
        .order("name")
        .execute()
        .data
    )


def save_transcript(
    lecture_id: str, user_id: str, text: str, segments: Optional[list[dict]] = None
) -> None:
    update(
        lecture_id,
        user_id,
        transcript=text,
        transcript_segments=segments or [],
    )


def load_transcript(lecture_id: str, user_id: str) -> Optional[str]:
    row = _one(
        client()
        .table("lectures")
        .select("transcript")
        .eq("id", lecture_id)
        .eq("user_id", user_id)
    )
    return row.get("transcript") if row else None


def load_transcript_segments(lecture_id: str, user_id: str) -> list[dict]:
    row = _one(
        client()
        .table("lectures")
        .select("transcript_segments")
        .eq("id", lecture_id)
        .eq("user_id", user_id)
    )
    return row.get("transcript_segments") or [] if row else []


def save_notes(lecture_id: str, user_id: str, notes: dict) -> None:
    update(lecture_id, user_id, notes=notes)


def load_notes(lecture_id: str, user_id: str) -> Optional[dict]:
    row = _one(
        client()
        .table("lectures")
        .select("notes")
        .eq("id", lecture_id)
        .eq("user_id", user_id)
    )
    return row.get("notes") if row else None


def create_chat_session(user_id: str, lecture_id: str, title: str = "New chat") -> dict:
    return (
        client()
        .table("chat_sessions")
        .insert({"user_id": user_id, "lecture_id": lecture_id, "title": title})
        .execute()
        .data[0]
    )


def update_chat_title(session_id: str, user_id: str, title: str) -> None:
    client().table("chat_sessions").update({"title": title}).eq("id", session_id).eq(
        "user_id", user_id
    ).execute()


def get_chat_session(session_id: str, user_id: str) -> Optional[dict]:
    return _one(
        client()
        .table("chat_sessions")
        .select("*")
        .eq("id", session_id)
        .eq("user_id", user_id)
    )


def list_chat_sessions(user_id: str, lecture_id: Optional[str] = None) -> list:
    query = (
        client()
        .table("chat_sessions")
        .select("id,lecture_id,title,created_at,updated_at")
        .eq("user_id", user_id)
    )
    if lecture_id:
        query = query.eq("lecture_id", lecture_id)
    return query.order("updated_at", desc=True).execute().data


def get_chat_messages(session_id: str, user_id: str) -> list:
    fail_stale_chat_replies(session_id, user_id)
    return (
        client()
        .table("chat_messages")
        .select("id,role,content,sources,status,intent,grounded,created_at")
        .eq("session_id", session_id)
        .eq("user_id", user_id)
        .order("id")
        .execute()
        .data
    )


def fail_stale_chat_replies(session_id: str, user_id: str) -> None:
    pending = (
        client()
        .table("chat_messages")
        .select("id,created_at")
        .eq("session_id", session_id)
        .eq("user_id", user_id)
        .eq("role", "assistant")
        .eq("status", "pending")
        .execute()
        .data
    )
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
    for row in pending:
        created_at = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
        if created_at < cutoff:
            fail_chat_reply(row["id"], session_id, user_id)


def create_chat_turn(
    session_id: str, user_id: str, user_message: str
) -> tuple[dict, dict]:
    result = (
        client()
        .table("chat_messages")
        .insert(
            [
                {
                    "session_id": session_id,
                    "user_id": user_id,
                    "role": "user",
                    "content": user_message,
                    "sources": [],
                    "status": "complete",
                    "intent": None,
                    "grounded": None,
                },
                {
                    "session_id": session_id,
                    "user_id": user_id,
                    "role": "assistant",
                    "content": "",
                    "sources": [],
                    "status": "pending",
                    "intent": None,
                    "grounded": None,
                },
            ]
        )
        .execute()
    )
    rows = result.data or []
    user_row = next((row for row in rows if row["role"] == "user"), None)
    assistant_row = next((row for row in rows if row["role"] == "assistant"), None)
    if user_row is None or assistant_row is None:
        raise RuntimeError("Supabase did not return the saved chat messages.")
    client().table("chat_sessions").update(
        {"updated_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", session_id).eq("user_id", user_id).execute()
    return user_row, assistant_row


def complete_chat_reply(
    message_id: int,
    session_id: str,
    user_id: str,
    answer: str,
    sources: list,
    intent: str,
    grounded: bool,
) -> None:
    result = (
        client()
        .table("chat_messages")
        .update(
            {
                "content": answer,
                "sources": sources or [],
                "intent": intent,
                "grounded": grounded,
                "status": "complete",
            }
        )
        .eq("id", message_id)
        .eq("session_id", session_id)
        .eq("user_id", user_id)
        .eq("status", "pending")
        .execute()
    )
    if not result.data:
        raise KeyError(message_id)


def fail_chat_reply(message_id: int, session_id: str, user_id: str) -> None:
    result = (
        client()
        .table("chat_messages")
        .update(
            {
                "content": "I couldn't generate an answer just now. Please try asking again.",
                "sources": [],
                "intent": None,
                "grounded": False,
                "status": "error",
            }
        )
        .eq("id", message_id)
        .eq("session_id", session_id)
        .eq("user_id", user_id)
        .eq("status", "pending")
        .execute()
    )
    if not result.data:
        raise KeyError(message_id)


def add_chat_messages(
    session_id: str,
    user_id: str,
    user_message: str,
    answer: str,
    sources: Optional[list] = None,
    intent: Optional[str] = None,
    grounded: Optional[bool] = None,
) -> None:
    supabase = client()
    normalized_sources = sources or []
    supabase.table("chat_messages").insert(
        [
            {
                "session_id": session_id,
                "user_id": user_id,
                "role": "user",
                "content": user_message,
                "sources": [],
                "intent": None,
                "grounded": None,
            },
            {
                "session_id": session_id,
                "user_id": user_id,
                "role": "assistant",
                "content": answer,
                "sources": normalized_sources,
                "intent": intent,
                "grounded": grounded,
            },
        ]
    ).execute()
    supabase.table("chat_sessions").update(
        {"updated_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", session_id).eq("user_id", user_id).execute()


def clear_chat_session(session_id: str, user_id: str) -> None:
    client().table("chat_messages").delete().eq("session_id", session_id).eq("user_id", user_id).execute()


def delete(lecture_id: str, user_id: str) -> None:
    supabase = client()
    row = get(lecture_id, user_id)
    if row and row.get("audio_path"):
        supabase.storage.from_("lecture-audio").remove([row["audio_path"]])
    supabase.table("lectures").delete().eq("id", lecture_id).eq("user_id", user_id).execute()


def upload_audio(user_id: str, lecture_id: str, path: str, extension: str, content_type: str) -> str:
    object_path = f"{user_id}/{lecture_id}{extension}"
    with open(path, "rb") as audio_file:
        client().storage.from_("lecture-audio").upload(
            object_path,
            audio_file,
            {"content-type": content_type, "upsert": "true"},
        )
    return object_path


def download_audio(object_path: str) -> bytes:
    return with_retry(
        lambda: client().storage.from_("lecture-audio").download(object_path),
        tries=3,
        base_delay=2.0,
        max_delay=8.0,
    )
