"""Offline smoke tests: no API keys or network needed.

Run:  python -m pytest -q      (pip install pytest httpx)
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.documents import Document  # noqa: E402
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402

from app.schemas import Notes  # noqa: E402

SAMPLE = {
    "title": "Deadlock in Operating Systems", "subject": "Operating Systems",
    "summary": "The class covered deadlock conditions and prevention.",
    "core_explanations": [{"topic": "Deadlock", "explanation": "Processes wait forever on each other.", "key_points": ["Mutual exclusion", "Hold and wait"]}],
    "announcements": [{"text": "Assignment 3 is due.", "deadline": "Friday"}],
    "exam_points": [{"point": "Explain the four Coffman conditions.", "kind": "important_question"}],
    "off_topic": [{"summary": "Professor shared a story about his first job."}],
}


def test_schema_roundtrip():
    assert Notes.model_validate(SAMPLE).exam_points[0].kind == "important_question"
    assert "title" in Notes.model_json_schema()["properties"]


def test_render_markdown_and_pdf():
    from app.render import notes_to_markdown, notes_to_pdf
    md = notes_to_markdown(SAMPLE)
    for heading in ("Core explanations", "Announcements", "Exam-relevant points", "Off-topic remarks"):
        assert heading in md
    assert notes_to_pdf(SAMPLE)[:4] == b"%PDF"
    empty = {**SAMPLE, "announcements": [], "exam_points": [], "off_topic": []}
    assert notes_to_pdf(empty)[:4] == b"%PDF"


def test_chat_memory_and_followup_rewrite():
    """The follow-up must be rewritten using history, and history must persist per session."""
    from app.rag import build_chain, ask
    llm = GenericFakeChatModel(messages=iter([
        AIMessage(content="Deadlock is when processes wait on each other forever."),       # answer 1
        AIMessage(content="Give an example of deadlock"),                                  # rewrite of follow-up
        AIMessage(content="Two processes each hold one lock and wait for the other."),    # answer 2
    ]))
    seen = []

    def retrieve(question, lecture_id, user_id):
        seen.append((question, lecture_id))
        return [Document(page_content="Deadlock needs four conditions.", metadata={"chunk": 0})]

    chain = build_chain(llm, retrieve)
    r1 = ask("lec1", "s1", "What is deadlock?", chain=chain)
    r2 = ask("lec1", "s1", "Can you give an example?", chain=chain)
    assert "wait" in r1["answer"] and r1["sources"][0]["chunk"] == 0
    assert seen[0][0] == "What is deadlock?"          # first question: no rewrite
    assert seen[1][0] == "Give an example of deadlock"  # follow-up: rewritten with history
    assert seen[1][1] == "lec1"
    from app.rag import _histories
    assert len(_histories["lec1:s1"].messages) == 4     # 2 human + 2 ai


def test_title_and_audio_timestamp_helpers():
    from app.main import _allocate_title
    from app.rag import _documents, _format_docs, format_timestamp

    assert _allocate_title({"title": "Untitled lecture", "core_explanations": []}, "Deadlocks prevent progress.", "") == "Deadlocks prevent progress"
    assert _allocate_title({"title": "", "core_explanations": [{"topic": "Deadlock"}]}, "text", "") == "Deadlock"
    assert _allocate_title(
        {"title": "Operating Systems Lecture Notes", "core_explanations": [{"topic": "Deadlock Prevention"}]},
        "text",
        "Operating Systems",
    ) == "Deadlock Prevention"
    assert format_timestamp(3723) == "01:02:03"
    docs = _documents("Deadlock explanation", [{"start": 60, "end": 72, "text": "Deadlock explanation"}])
    assert "01:00-01:12" in _format_docs(docs)
    assert docs[0].metadata["start_seconds"] == 60


def test_embedding_retries_transient_provider_failures(monkeypatch):
    from app import rag, utils

    calls = []

    class FakeEmbeddings:
        def embed_documents(self, texts):
            calls.append(texts)
            if len(calls) == 1:
                raise RuntimeError("503 UNAVAILABLE")
            return [[0.1, 0.2] for _ in texts]

    class Query:
        def delete(self):
            return self

        def insert(self, rows):
            self.rows = rows
            return self

        def eq(self, *_):
            return self

        def execute(self):
            return self

    class Client:
        def table(self, _):
            return Query()

    monkeypatch.setattr(rag, "_documents", lambda transcript, segments: [Document(page_content="lecture chunk")])
    monkeypatch.setattr(rag.store, "client", lambda: Client())
    monkeypatch.setattr(rag, "get_embeddings", FakeEmbeddings)
    monkeypatch.setattr(utils.time, "sleep", lambda _: None)

    assert rag.index_lecture("lecture1", "text", "user1") == 1
    assert len(calls) == 2


def test_index_failure_keeps_generated_notes_available(monkeypatch):
    from types import SimpleNamespace
    from app import main, notes, rag, store

    row = {
        "id": "lecture1",
        "title": "Generating title...",
        "title_provided": False,
        "subject": "Operating Systems",
    }
    updates = {}
    saved = {}
    monkeypatch.setattr(store, "get", lambda *_: {**row, **updates})
    monkeypatch.setattr(store, "load_transcript", lambda *_: "Deadlock prevention avoids circular wait.")
    monkeypatch.setattr(store, "load_transcript_segments", lambda *_: [])
    monkeypatch.setattr(store, "save_notes", lambda _, __, data: saved.update(data))
    monkeypatch.setattr(store, "update", lambda _, __, **fields: updates.update(fields) or {**row, **updates})
    monkeypatch.setattr(
        notes,
        "generate_notes",
        lambda *_: SimpleNamespace(model_dump=lambda: {
            **SAMPLE,
            "core_explanations": [{"topic": "Deadlock Prevention", "explanation": "Avoid circular wait.", "key_points": []}],
        }),
    )
    monkeypatch.setattr(rag, "index_lecture", lambda *_: (_ for _ in ()).throw(RuntimeError("503 UNAVAILABLE")))

    main.process_lecture("lecture1", "user1", None, None)

    assert saved["title"] == "Deadlock in Operating Systems"
    assert updates["title"] == "Deadlock Prevention"
    assert updates["status"] == "index_error"
    assert "notes are ready" in updates["error"].lower()


def test_retry_endpoint_does_not_download_audio_before_accepting(monkeypatch):
    from app import main, store

    lecture = {
        "id": "lecture1",
        "status": "error",
        "audio_path": "user1/lecture1.mp3",
        "source": "lecture1.mp3",
    }
    scheduled = []
    monkeypatch.setattr(main, "_require", lambda *_: lecture)
    monkeypatch.setattr(store, "load_transcript", lambda *_: None)
    monkeypatch.setattr(store, "update", lambda *_args, **fields: lecture.update(fields) or dict(lecture))
    monkeypatch.setattr(store, "get", lambda *_: dict(lecture))
    monkeypatch.setattr(
        main,
        "_schedule_processing",
        lambda *args: scheduled.append(args),
    )
    monkeypatch.setattr(
        main,
        "_temp_audio_from_storage",
        lambda *_: (_ for _ in ()).throw(AssertionError("download must run in worker")),
    )

    response = main.retry_lecture("lecture1", "user1")

    assert response["status"] == "queued"
    assert scheduled == [("lecture1", "user1", None, None)]


def test_chat_messages_always_include_non_null_sources(monkeypatch):
    from app import store

    captured = {}

    class Query:
        def __init__(self, table):
            self.table_name = table

        def insert(self, rows):
            captured["rows"] = rows
            return self

        def update(self, fields):
            captured["updated"] = fields
            return self

        def eq(self, *_):
            return self

        def execute(self):
            return self

    class Client:
        def table(self, name):
            return Query(name)

    monkeypatch.setattr(store, "client", lambda: Client())

    store.add_chat_messages("session1", "user1", "Question?", "Answer.", None)

    assert [row["sources"] for row in captured["rows"]] == [[], []]


def test_failed_background_chat_is_saved_as_terminal_error(monkeypatch):
    from app import main, rag, store

    failed = []
    monkeypatch.setattr(
        rag,
        "ask",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("provider unavailable")),
    )
    monkeypatch.setattr(
        store,
        "fail_chat_reply",
        lambda *args: failed.append(args),
    )

    main.process_chat_answer("lecture1", "session1", "user1", "Question?", 42, [])

    assert failed == [(42, "session1", "user1")]


def test_missing_chat_status_column_returns_migration_guidance():
    from app.main import _chat_schema_migration_error, _is_missing_chat_status_column

    error = RuntimeError(
        "{'message': 'column chat_messages.status does not exist', 'code': '42703'}"
    )

    assert _is_missing_chat_status_column(error)
    assert "20261030_chat_message_status.sql" in _chat_schema_migration_error().detail
    assert not _is_missing_chat_status_column(RuntimeError("network timeout"))


def test_supabase_new_key_client_configuration(monkeypatch):
    from app import auth, store

    created = []
    monkeypatch.setattr(auth.config, "SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setattr(auth.config, "SUPABASE_PUBLISHABLE_KEY", "sb_publishable_test")
    monkeypatch.setattr(store.config, "SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setattr(store.config, "SUPABASE_SECRET_KEY", "sb_secret_test")
    monkeypatch.setattr(auth, "create_client", lambda url, key: created.append((url, key)) or "auth-client")
    monkeypatch.setattr(store, "create_client", lambda url, key: created.append((url, key)) or "data-client")
    auth._auth_client.cache_clear()
    store.client.cache_clear()
    try:
        assert auth._auth_client() == "auth-client"
        assert store.client() == "data-client"
        assert created == [
            ("https://project.supabase.co", "sb_publishable_test"),
            ("https://project.supabase.co", "sb_secret_test"),
        ]
    finally:
        auth._auth_client.cache_clear()
        store.client.cache_clear()


def test_processing_is_scheduled_without_blocking_request(monkeypatch):
    import asyncio
    import threading
    from app import main

    finished = threading.Event()

    def fake_process(lecture_id, user_id, audio_path, language):
        time.sleep(0.2)
        finished.set()

    monkeypatch.setattr(main, "process_lecture", fake_process)

    async def verify_schedule():
        started = time.monotonic()
        main._schedule_processing("lec-test", "user-test", None, None)
        assert time.monotonic() - started < 0.1
        assert await asyncio.to_thread(finished.wait, 2)

    asyncio.run(verify_schedule())


def test_api_flow(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main, store

    lectures, transcripts, saved_notes, chat_sessions, chat_messages = {}, {}, {}, {}, {}

    def fake_create(user_id, title, subject, source):
        lecture_id = f"lecture{len(lectures) + 123}"
        row = {
            "id": lecture_id, "user_id": user_id, "title": title or "Generating title...",
            "title_provided": bool(title), "subject": subject, "folder_id": None, "source": source,
            "status": "queued", "progress": "Waiting to start", "error": "",
            "created_at": "2025-01-01T00:00:00+00:00",
        }
        lectures[lecture_id] = row
        return dict(row)

    def fake_get(lecture_id, user_id):
        row = lectures.get(lecture_id)
        return row if row and row["user_id"] == user_id else None

    def fake_update(lecture_id, user_id, **fields):
        row = fake_get(lecture_id, user_id)
        if row is None:
            raise KeyError(lecture_id)
        row.update(fields)
        return dict(row)

    def fake_process(lecture_id, user_id, audio_path, language):
        try:
            store.update(lecture_id, user_id, status="generating_notes")
            store.save_notes(lecture_id, user_id, SAMPLE)
            row = store.get(lecture_id, user_id)
            if not row["title_provided"]:
                store.update(lecture_id, user_id, title=main._allocate_title(SAMPLE, transcripts.get(lecture_id, ""), row["subject"]))
            store.update(lecture_id, user_id, status="done", progress="Ready")
        finally:
            if audio_path:
                audio_path.unlink(missing_ok=True)

    def fake_create_chat(user_id, lecture_id):
        row = {"id": "chat123", "user_id": user_id, "lecture_id": lecture_id, "title": "New chat"}
        chat_sessions[row["id"]] = row
        chat_messages[row["id"]] = []
        return dict(row)

    def fake_get_chat(session_id, user_id):
        row = chat_sessions.get(session_id)
        return row if row and row["user_id"] == user_id else None

    def fake_add_messages(session_id, user_id, user_message, answer, sources):
        chat_messages[session_id].extend([
            {"id": len(chat_messages[session_id]) + 1, "role": "user", "content": user_message, "sources": [], "status": "complete"},
            {"id": len(chat_messages[session_id]) + 2, "role": "assistant", "content": answer, "sources": sources, "status": "complete"},
        ])

    def fake_create_chat_turn(session_id, user_id, user_message):
        user_row = {"id": len(chat_messages[session_id]) + 1, "role": "user", "content": user_message, "sources": [], "status": "complete", "created_at": "2025-01-01T00:00:00Z"}
        assistant_row = {"id": user_row["id"] + 1, "role": "assistant", "content": "", "sources": [], "status": "pending", "created_at": "2025-01-01T00:00:01Z"}
        chat_messages[session_id].extend([user_row, assistant_row])
        return user_row, assistant_row

    def fake_complete_chat_reply(message_id, session_id, user_id, answer, sources):
        row = next(message for message in chat_messages[session_id] if message["id"] == message_id)
        row.update(content=answer, sources=sources, status="complete")

    def fake_fail_chat_reply(message_id, session_id, user_id):
        row = next(message for message in chat_messages[session_id] if message["id"] == message_id)
        row.update(content="I couldn't generate an answer just now. Please try asking again.", sources=[], status="error")

    monkeypatch.setattr(store, "create", fake_create)
    monkeypatch.setattr(store, "get", fake_get)
    monkeypatch.setattr(store, "update", fake_update)
    monkeypatch.setattr(store, "list_all", lambda user_id: [dict(row) for row in lectures.values() if row["user_id"] == user_id])
    monkeypatch.setattr(store, "list_folders", lambda user_id: [])
    monkeypatch.setattr(store, "save_transcript", lambda lecture_id, user_id, text, segments=None: transcripts.__setitem__(lecture_id, text))
    monkeypatch.setattr(store, "load_transcript", lambda lecture_id, user_id: transcripts.get(lecture_id))
    monkeypatch.setattr(store, "load_transcript_segments", lambda lecture_id, user_id: [])
    monkeypatch.setattr(store, "save_notes", lambda lecture_id, user_id, notes: saved_notes.__setitem__(lecture_id, notes))
    monkeypatch.setattr(store, "load_notes", lambda lecture_id, user_id: saved_notes.get(lecture_id))
    monkeypatch.setattr(store, "create_chat_session", fake_create_chat)
    monkeypatch.setattr(store, "get_chat_session", fake_get_chat)
    monkeypatch.setattr(store, "list_chat_sessions", lambda user_id, lecture_id=None: [dict(row) for row in chat_sessions.values() if row["user_id"] == user_id and (lecture_id is None or row["lecture_id"] == lecture_id)])
    monkeypatch.setattr(store, "get_chat_messages", lambda session_id, user_id: list(chat_messages.get(session_id, [])) if fake_get_chat(session_id, user_id) else [])
    monkeypatch.setattr(store, "create_chat_turn", fake_create_chat_turn)
    monkeypatch.setattr(store, "complete_chat_reply", fake_complete_chat_reply)
    monkeypatch.setattr(store, "fail_chat_reply", fake_fail_chat_reply)
    monkeypatch.setattr(store, "update_chat_title", lambda session_id, user_id, title: chat_sessions[session_id].update(title=title))
    from app import rag
    monkeypatch.setattr(rag, "ask", lambda lecture_id, session_id, message, **kwargs: {"answer": "Deadlock occurs at [01:00].", "sources": [{"timestamp": "01:00", "text": "deadlock"}]})
    monkeypatch.setattr(main, "process_lecture", fake_process)
    c = TestClient(main.app)
    assert c.get("/api/health").json()["ok"]
    headers = {"Authorization": "Bearer test-token"}
    assert TestClient(main.app).get("/api/lectures").status_code == 401
    monkeypatch.setitem(main.app.dependency_overrides, main.auth.current_user, lambda: "user-a")
    assert c.post("/api/lectures", data={}, headers=headers).status_code == 400
    assert c.post("/api/lectures", files={"file": ("x.txt", b"hi")}, headers=headers).status_code == 400

    r = c.post("/api/lectures", data={"transcript_text": "Deadlocks prevent processes from progressing."}, headers=headers)
    assert r.status_code == 202
    upload = c.post("/api/lectures", files={"file": ("sample.mp3", b"audio")}, headers=headers)
    assert upload.status_code == 202
    lid = r.json()["id"]
    time.sleep(0.2)
    d = c.get(f"/api/lectures/{lid}", headers=headers).json()
    assert d["status"] == "done" and d["title"] == "Deadlock"
    assert d["notes"]["title"].startswith("Deadlock")
    assert c.get(f"/api/lectures/{lid}/transcript", headers=headers).text == "Deadlocks prevent processes from progressing."
    assert c.get(f"/api/lectures/{lid}/notes.pdf", headers=headers).content[:4] == b"%PDF"
    assert "Exam-relevant" in c.get(f"/api/lectures/{lid}/notes.md", headers=headers).text
    chat_response = c.post(f"/api/lectures/{lid}/chat-sessions", headers=headers)
    assert chat_response.status_code == 201
    session_id = chat_response.json()["id"]
    answer = c.post(f"/api/lectures/{lid}/chat", json={"session_id": session_id, "message": "When is deadlock explained?"}, headers=headers)
    assert answer.status_code == 202
    assert answer.json()["assistant_message"]["status"] == "pending"
    for _ in range(50):
        messages = c.get(f"/api/chat-sessions/{session_id}/messages", headers=headers).json()
        if messages[-1]["status"] != "pending":
            break
        time.sleep(0.02)
    assert messages[0]["content"] == "When is deadlock explained?"
    assert messages[1]["status"] == "complete" and "01:00" in messages[1]["sources"][0]["timestamp"]
    assert c.get(f"/api/lectures/nope", headers=headers).status_code == 404
    monkeypatch.setitem(main.app.dependency_overrides, main.auth.current_user, lambda: "user-b")
    assert c.get(f"/api/lectures/{lid}", headers=headers).status_code == 404
    assert c.get(f"/api/chat-sessions/{session_id}/messages", headers=headers).status_code == 404
    monkeypatch.setitem(main.app.dependency_overrides, main.auth.current_user, lambda: "user-a")
    assert c.get("/").status_code == 200
    assert len(c.get("/api/lectures", headers=headers).json()) == 2


def test_audio_split_and_quiet_cut():
    """Needs ffmpeg. Builds ~70 s of tone with a silent gap and checks the first cut lands in the gap."""
    from pydub.generators import Sine
    from pydub import AudioSegment
    from app import audio
    tone = Sine(440).to_audio_segment(duration=28_000)
    sound = tone + AudioSegment.silent(duration=1500) + Sine(660).to_audio_segment(duration=40_000)
    path = Path(tempfile.mkdtemp()) / "t.wav"
    sound.export(path, format="wav")
    chunks = audio.split_audio(path, chunk_minutes=0.5)  # 30 s target
    assert len(chunks) >= 2
    assert 27_500 <= len(chunks[0]) <= 30_500  # first cut landed in the silent gap, not mid-tone
    assert abs(sum(len(c) for c in chunks) - len(sound)) < 50  # nothing lost


def test_groq_json_validation_retries_with_json_only_prompt(monkeypatch):
    from types import SimpleNamespace
    from app import notes

    calls = []

    class JsonValidationError(Exception):
        status_code = 400
        body = {"error": {"code": "json_validate_failed"}}

    def create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise JsonValidationError("Groq could not validate generated JSON.")
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=f"```json\n{json.dumps(SAMPLE)}\n```"
                    )
                )
            ]
        )

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    monkeypatch.setattr(notes, "Groq", lambda **_: client)

    result = notes._request_groq_notes("Deadlock prevention avoids circular wait.", "", "")

    assert result.title == SAMPLE["title"]
    assert len(calls) == 2
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in calls[1]
    assert "Return only one valid JSON object" in calls[1]["messages"][0]["content"]


def test_groq_invalid_json_retries_with_smaller_transcript_chunks(monkeypatch):
    from app import notes

    calls = []

    def request(transcript, title, subject):
        calls.append(transcript)
        if len(calls) == 1:
            raise RuntimeError("Groq returned invalid JSON for structured notes.")
        return Notes.model_validate(SAMPLE)

    monkeypatch.setattr(notes, "_request_groq_notes", request)
    results = notes._generate_groq_chunk(" ".join(["lecture"] * 80), "", "")

    assert len(calls) == 3
    assert len(results) == 2
    assert all(len(chunk.split()) == 40 for chunk in calls[1:])


def test_groq_short_invalid_json_returns_actionable_error(monkeypatch):
    from app import notes

    def invalid_response(*_):
        raise RuntimeError("Groq returned invalid JSON for structured notes.")

    monkeypatch.setattr(notes, "_request_groq_notes", invalid_response)
    try:
        notes._generate_groq_chunk("A short transcript.", "", "")
    except RuntimeError as exc:
        assert "short transcript excerpt" in str(exc)
    else:
        raise AssertionError("Expected malformed Groq JSON to be reported.")
