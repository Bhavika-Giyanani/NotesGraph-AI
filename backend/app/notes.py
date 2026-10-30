"""Pipeline 1: full transcript -> structured notes (LangChain structured output)."""
from langchain_core.prompts import ChatPromptTemplate

from .llm import get_llm
from .schemas import Notes
from .utils import with_retry

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


def generate_notes(transcript: str, title: str = "", subject: str = "") -> Notes:
    chain = _prompt | get_llm(temperature=0.2).with_structured_output(Notes)
    payload = {"title": title or "unknown", "subject": subject or "unknown", "transcript": transcript}

    def run() -> Notes:
        result = chain.invoke(payload)
        if result is None:
            raise RuntimeError("temporarily unavailable: model returned no structured output")
        return result if isinstance(result, Notes) else Notes.model_validate(result)

    return with_retry(run, tries=3, base_delay=4.0)
