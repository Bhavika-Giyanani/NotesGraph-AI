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
    session_id: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=4000)
    accept_offer: bool = False


class PendingOffer(BaseModel):
    type: Literal["general_answer"]
    question: str


class ChatSource(BaseModel):
    start: Optional[float] = None
    end: Optional[float] = None
    timestamp_label: Optional[str] = None
    text_snippet: Optional[str] = None


class ChatMessage(BaseModel):
    id: int
    role: Literal["user", "assistant"]
    content: str
    created_at: str
    sources: List[ChatSource] = Field(default_factory=list)
    status: Optional[Literal["pending", "complete", "error"]] = None
    intent: Optional[
        Literal["locate", "explain", "off_topic", "conversation", "general"]
    ] = None
    grounded: Optional[bool] = None
    pending_offer: Optional[PendingOffer] = None


class ChatTurn(BaseModel):
    user_message: ChatMessage
    assistant_message: ChatMessage


class FolderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class UploadFailure(BaseModel):
    error: str = Field(min_length=1, max_length=500)
