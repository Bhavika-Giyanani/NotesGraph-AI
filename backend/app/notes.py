"""Pipeline 1: full transcript -> structured notes (LangChain structured output)."""
import json
import logging
import re
from typing import Callable, Optional

from groq import Groq
from langchain_core.prompts import ChatPromptTemplate

from . import config
from .llm import get_llm
from .schemas import Notes
from .utils import with_retry

log = logging.getLogger("notesgraph")

SYSTEM = """You are an expert teaching assistant who turns raw lecture transcripts into revision-ready notes.

Read the WHOLE transcript and sort everything the lecturer said into exactly these buckets:
1. core_explanations: the academic content actually taught, grouped by topic in the order taught.
2. announcements: logistics (assignments, submissions, test dates, schedule changes, rules, attendance).
3. exam_points: anything the professor flagged for exams: important questions, topics to focus on, marking or pattern hints, things to skip.
   Phrases like "this is important", "this will come in the exam", "remember this", "frequently asked" signal these.
4. off_topic: digressions unrelated to the subject (stories, jokes, life or career advice, news, campus talk), including non-academic ones.

Rules:
- Use only what is in the transcript. Never invent facts, dates or exam questions.
- The title must identify the specific topic, concept, or chapter taught in THIS lecture. Prefer a concise chapter/topic name such as "Deadlock Prevention" or "Process Scheduling".
- The subject hint is the broader course name, not a title suggestion. Never use only the subject or generic phrases such as "Operating Systems Lecture Notes" as the title when the transcript contains specific topics.
- Use the first substantive core_explanations topic as the title when it accurately describes the lecture's main subject.
- If the supplied title is blank or a placeholder, create the title from the lecture content.
- Never return a title such as "Untitled lecture", "Lecture notes", or "Generating title".
- If a bucket has nothing, return an empty list for it.
- Something can be both explained and flagged for the exam: explain it in core_explanations and add the exam angle to exam_points.
- The transcript may mix English, Hindi and Gujarati and may contain recognition errors. Infer the intended meaning and write the notes in clear English, keeping technical terms and names in Roman script.
- Be concise and specific; write for a student revising the night before an exam."""

HUMAN = """Lecture title (may be a placeholder): {title}
Subject hint: {subject}

TRANSCRIPT:
{transcript}"""

_prompt = ChatPromptTemplate.from_messages([("system", SYSTEM), ("human", HUMAN)])


def _is_transient_provider_error(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status in (429, 500, 502, 503, 504):
        return True
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "429",
            "500",
            "502",
            "503",
            "504",
            "unavailable",
            "resource_exhausted",
            "rate limit",
            "overloaded",
            "timed out",
            "timeout",
        )
    )


def _split_transcript(transcript: str, max_chars: int = 900) -> list[str]:
    words = transcript.split()
    chunks = []
    current: list[str] = []
    current_length = 0
    for word in words:
        if current and current_length + len(word) + 1 > max_chars:
            chunks.append(" ".join(current))
            current = []
            current_length = 0
        current.append(word)
        current_length += len(word) + 1
    if current:
        chunks.append(" ".join(current))
    return chunks


def _is_groq_json_validation_error(exc: Exception) -> bool:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("code") == "json_validate_failed":
            return True
    return "json_validate_failed" in str(exc).lower()


def _parse_groq_notes(content: str) -> Notes:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    try:
        return Notes.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValueError):
        start = text.find("{")
        if start >= 0:
            try:
                value, _ = json.JSONDecoder().raw_decode(text[start:])
                return Notes.model_validate(value)
            except (json.JSONDecodeError, ValueError):
                pass
        raise RuntimeError("Groq returned invalid JSON for structured notes.") from None


def _request_groq_notes(transcript: str, title: str, subject: str) -> Notes:
    client = Groq(api_key=config.GROQ_API_KEY)
    messages = [
        {
            "role": "system",
            "content": (
                "Extract concise, factual lecture notes from this transcript excerpt only. "
                "Do not invent information. Translate Hindi or Gujarati into clear English and "
                "retain technical terms in Roman script. Return one JSON object with exactly "
                "these keys: title (string), subject (string), summary (one concise sentence), "
                "core_explanations (array of {topic, explanation, key_points}), "
                "announcements (array of {text, deadline}), "
                "exam_points (array of {point, kind}, where kind is important_question, "
                "important_topic, or professor_hint), and off_topic (array of {summary}). "
                "Use empty arrays when a category is absent. Only include facts present in "
                "the excerpt; use a concise topic title. Keep every field brief, and include "
                "at most two items per array."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Lecture title: {title or 'unknown'}\n"
                f"Subject hint: {subject or 'unknown'}\n"
                f"Transcript excerpt:\n{transcript}"
            ),
        },
    ]

    def request(request_messages, *, json_mode: bool):
        options = {
            "model": config.GROQ_NOTES_MODEL,
            "messages": request_messages,
            "temperature": 0.2,
            "max_completion_tokens": 350,
        }
        if json_mode:
            options["response_format"] = {"type": "json_object"}
        return client.chat.completions.create(**options)

    try:
        response = with_retry(
            lambda: request(messages, json_mode=True),
            tries=5,
            base_delay=10.0,
            max_delay=60.0,
        )
    except Exception as exc:
        if not _is_groq_json_validation_error(exc):
            raise
        log.warning(
            "Groq JSON mode rejected generated notes; retrying once with a concise JSON-only prompt."
        )
        repair_messages = [
            {
                "role": "system",
                "content": (
                    "Return only one valid JSON object, with no markdown or extra text. "
                    "It must have exactly these fields and types: title string, subject string, "
                    "summary string, core_explanations array of objects {topic string, "
                    "explanation string, key_points array of strings}, announcements array of "
                    "objects {text string, deadline string}, exam_points array of objects "
                    "{point string, kind string}, and off_topic array of objects {summary string}. "
                    "Use empty strings and arrays as needed. Keep values very concise. For kind, "
                    "use only important_question, important_topic, or professor_hint."
                ),
            },
            messages[1],
        ]
        response = with_retry(
            lambda: request(repair_messages, json_mode=False),
            tries=2,
            base_delay=2.0,
            max_delay=4.0,
        )
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError("Groq returned no structured note content.")
    return _parse_groq_notes(content)


def _generate_groq_chunk(transcript: str, title: str, subject: str) -> list[Notes]:
    try:
        return [_request_groq_notes(transcript, title, subject)]
    except Exception as exc:
        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        error_text = str(exc).lower()
        can_split = (
            status == 413
            or "413" in error_text
            or "json_validate_failed" in error_text
            or "returned invalid json" in error_text
        )
        if not can_split:
            raise

        words = transcript.split()
        if len(words) < 40:
            if "json" in error_text:
                raise RuntimeError(
                    "Groq could not produce valid structured JSON for a short transcript excerpt."
                ) from exc
            raise RuntimeError(
                f"Groq rejected a small transcript excerpt for exceeding its token limit: {exc}"
            ) from exc
        midpoint = len(words) // 2
        reason = "structured JSON generation" if "json" in error_text else "token limit"
        log.warning("Groq failed during %s; splitting transcript excerpt.", reason)
        return (
            _generate_groq_chunk(" ".join(words[:midpoint]), title, subject)
            + _generate_groq_chunk(" ".join(words[midpoint:]), title, subject)
        )


def _generate_notes_with_groq(
    payload: dict, progress: Optional[Callable[[str], None]] = None
) -> Notes:
    chunks = _split_transcript(payload["transcript"])
    if not chunks:
        raise RuntimeError("Cannot generate notes from an empty transcript.")
    results: list[Notes] = []
    for index, chunk in enumerate(chunks, start=1):
        message = f"Writing notes with Groq fallback ({index}/{len(chunks)})"
        log.info("%s using %s", message, config.GROQ_NOTES_MODEL)
        if progress:
            progress(message)
        results.extend(
            _generate_groq_chunk(chunk, payload["title"], payload["subject"])
        )

    summary_parts = [re.split(r"(?<=[.!?])\s+", note.summary.strip())[0] for note in results]
    if len(summary_parts) > 5:
        selected = [
            round(index * (len(summary_parts) - 1) / 4)
            for index in range(5)
        ]
        summary_parts = [summary_parts[index] for index in selected]

    def deduplicate(items):
        unique = {}
        for item in items:
            key = json.dumps(item.model_dump(), sort_keys=True, ensure_ascii=False)
            unique[key] = item
        return list(unique.values())

    return Notes(
        title=next((note.title for note in results if note.title.strip()), "Lecture notes"),
        subject=next((note.subject for note in results if note.subject.strip()), payload["subject"]),
        summary=" ".join(part for part in summary_parts if part),
        core_explanations=deduplicate(
            [item for note in results for item in note.core_explanations]
        ),
        announcements=deduplicate(
            [item for note in results for item in note.announcements]
        ),
        exam_points=deduplicate([item for note in results for item in note.exam_points]),
        off_topic=deduplicate([item for note in results for item in note.off_topic]),
    )


def generate_notes(
    transcript: str,
    title: str = "",
    subject: str = "",
    progress: Optional[Callable[[str], None]] = None,
) -> Notes:
    payload = {"title": title, "subject": subject, "transcript": transcript}

    def run() -> Notes:
        chain = _prompt | get_llm(temperature=0.2).with_structured_output(Notes)
        result = chain.invoke(payload)
        if result is None:
            raise RuntimeError("temporarily unavailable: model returned no structured output")
        return result if isinstance(result, Notes) else Notes.model_validate(result)

    try:
        return with_retry(run, tries=3, base_delay=4.0)
    except Exception as gemini_error:
        if not config.GROQ_API_KEY or not _is_transient_provider_error(gemini_error):
            raise RuntimeError(
                f"Gemini model '{config.GEMINI_MODEL}' failed during structured note generation: "
                f"{gemini_error}"
            ) from gemini_error

        log.warning(
            "Gemini model %s is temporarily unavailable; falling back to Groq model %s",
            config.GEMINI_MODEL,
            config.GROQ_NOTES_MODEL,
        )
        try:
            notes = _generate_notes_with_groq(payload, progress)
        except Exception as groq_error:
            raise RuntimeError(
                f"Gemini model '{config.GEMINI_MODEL}' failed during structured note generation "
                f"({gemini_error}); Groq fallback model '{config.GROQ_NOTES_MODEL}' also failed: "
                f"{groq_error}"
            ) from groq_error
        log.info("Structured notes generated with Groq fallback model %s", config.GROQ_NOTES_MODEL)
        return notes
