"""Pydantic schemas: structured notes (LLM output) and API payloads."""
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class Concept(BaseModel):
    topic: str = Field(description="Short heading for the concept or topic taught.")
    explanation: str = Field(
        description="Clear, revision-friendly explanation (2-6 sentences). "
        "Include definitions, formulas, examples or analogies the lecturer gave."
    )
    key_points: List[str] = Field(
        default_factory=list, description="Bullet-style takeaways or key terms for quick revision."
    )


class Announcement(BaseModel):
    text: str = Field(description="Administrative or logistical announcement (assignments, submissions, tests, schedule changes, rules).")
    deadline: str = Field(
        default="", description="Date, day or deadline mentioned for this announcement. Empty string if none was said."
    )


class ExamPoint(BaseModel):
    point: str = Field(
        description="An important question, topic or tip the professor said is relevant for exams, "
        "phrased so the student can study it directly."
    )
    kind: Literal["important_question", "important_topic", "professor_hint"] = Field(
        description="important_question: a question likely to be asked. important_topic: a topic to focus on. "
        "professor_hint: a remark about exam pattern, marking or what to skip."
    )


class OffTopic(BaseModel):
    summary: str = Field(
        description="A remark outside the topic being taught: stories, life advice, jokes, "
        "news, campus talk, career tips or other non-academic digressions."
    )


class Notes(BaseModel):
    title: str = Field(description="Concise title for the lecture.")
    subject: str = Field(default="", description="Subject or course name if it can be inferred, else empty string.")
    summary: str = Field(description="3-5 sentence overview of what the lecture covered.")
    core_explanations: List[Concept] = Field(description="Main academic content, in the order it was taught.")
    announcements: List[Announcement] = Field(default_factory=list, description="Announcements made during the class.")
    exam_points: List[ExamPoint] = Field(default_factory=list, description="Exam-relevant points flagged by the professor.")
    off_topic: List[OffTopic] = Field(default_factory=list, description="Non-academic or off-topic discussion.")


class ChatRequest(BaseModel):
    session_id: str = Field(
        min_length=1,
        max_length=100,
        description="ID of the persistent chat session associated with this lecture.",
    )
    message: str = Field(
        min_length=1,
        max_length=4000,
        description="The user's message. Context-dependent follow-ups are rewritten internally; the original message is stored.",
    )
    accept_offer: bool = Field(
        default=False,
        description="Explicitly accept a pending general-answer offer from the previous assistant message.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "session_id": "f6c955d6-1f12-4f97-880f-071d96946e3b",
                    "message": "Where does the speaker explain deadlock prevention?",
                    "accept_offer": False,
                }
            ]
        }
    }


class PendingOffer(BaseModel):
    type: Literal["general_answer"] = Field(
        description="The only currently supported offer type."
    )
    question: str = Field(
        description="Standalone question stored by the backend for a later general answer."
    )


class ChatSource(BaseModel):
    start: Optional[float] = Field(
        default=None,
        description="Source start in seconds from the beginning of the original recording.",
    )
    end: Optional[float] = Field(
        default=None,
        description="Source end in seconds. Equal to start for a single moment; greater than start for a range.",
    )
    timestamp_label: Optional[str] = Field(
        default=None,
        description="Human-readable time or time range, formatted as mm:ss or hh:mm:ss.",
    )
    text_snippet: Optional[str] = Field(
        default=None,
        description="Transcript excerpt associated with this source. It is not word-level timing data.",
    )


class ChatMessage(BaseModel):
    id: int = Field(description="Persisted message identifier and ordering key.")
    role: Literal["user", "assistant"] = Field(description="Whether this row is a user or assistant message.")
    content: str = Field(description="Message text; pending assistant messages may have empty content.")
    created_at: str = Field(description="Database creation timestamp.")
    sources: List[ChatSource] = Field(
        default_factory=list,
        description="Server-built lecture sources. Empty for ungrounded, conversation, and general answers.",
    )
    status: Optional[Literal["pending", "complete", "error"]] = Field(
        default=None,
        description="Asynchronous assistant reply state.",
    )
    intent: Optional[
        Literal["locate", "explain", "off_topic", "conversation", "general"]
    ] = Field(default=None, description="Router-selected answer intent.")
    grounded: Optional[bool] = Field(
        default=None,
        description="Whether the answer is supported by retrieved lecture excerpts.",
    )
    pending_offer: Optional[PendingOffer] = Field(
        default=None,
        description="General-answer offer which can be explicitly accepted in the next turn.",
    )


class ChatTurn(BaseModel):
    user_message: ChatMessage = Field(description="Persisted user message.")
    assistant_message: ChatMessage = Field(
        description="Persisted assistant placeholder. Poll the session messages endpoint for completion."
    )


class FolderCreate(BaseModel):
    name: str = Field(
        min_length=1,
        max_length=100,
        description="Subject folder name; whitespace is normalized and matching owner folders are reused.",
        examples=["Operating Systems"],
    )


class UploadFailure(BaseModel):
    error: str = Field(
        min_length=1,
        max_length=500,
        description="Short explanation of the failed audio upload.",
        examples=["The connection to storage was interrupted."],
    )
