"""Pipeline 1: full transcript -> structured notes (LangChain structured output)."""
import json
import logging

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


def _strict_json_schema(schema: dict) -> dict:
    definitions = schema.get("$defs", {})

    def normalize(node: dict) -> dict:
        reference = node.get("$ref")
        if reference:
            name = reference.rsplit("/", 1)[-1]
            return normalize(definitions[name])

        normalized = {}
        for key, value in node.items():
            if key in {"$defs", "$ref", "title", "default", "required"}:
                continue
            if key == "properties":
                normalized[key] = {
                    name: normalize(property_schema)
                    for name, property_schema in value.items()
                }
            else:
                normalized[key] = normalize(value) if isinstance(value, dict) else value
        if node.get("type") == "object":
            normalized["required"] = list(node.get("properties", {}))
            normalized["additionalProperties"] = False
        return normalized

    return normalize(schema)


def _generate_notes_with_groq(payload: dict) -> Notes:
    messages = [
        {
            "role": "system" if message.type == "system" else "user",
            "content": message.content,
        }
        for message in _prompt.format_messages(**payload)
    ]

    def request():
        return Groq(api_key=config.GROQ_API_KEY).chat.completions.create(
            model=config.GROQ_NOTES_MODEL,
            messages=messages,
            temperature=0.2,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "lecture_notes",
                    "strict": True,
                    "schema": _strict_json_schema(Notes.model_json_schema()),
                },
            },
        )

    response = with_retry(request, tries=3, base_delay=2.0, max_delay=8.0)
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError("Groq returned no structured note content.")
    return Notes.model_validate(json.loads(content))


def generate_notes(transcript: str, title: str = "", subject: str = "") -> Notes:
    payload = {"title": title or "unknown", "subject": subject or "unknown", "transcript": transcript}

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
            notes = _generate_notes_with_groq(payload)
        except Exception as groq_error:
            raise RuntimeError(
                f"Gemini model '{config.GEMINI_MODEL}' failed during structured note generation "
                f"({gemini_error}); Groq fallback model '{config.GROQ_NOTES_MODEL}' also failed: "
                f"{groq_error}"
            ) from groq_error
        log.info("Structured notes generated with Groq fallback model %s", config.GROQ_NOTES_MODEL)
        return notes
