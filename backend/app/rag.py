"""Supabase pgvector retrieval and isolated, persistent-session chat."""
import logging
import re
from typing import Dict

from langchain_core.chat_history import BaseChatMessageHistory, InMemoryChatMessageHistory
from langchain_core.documents import Document
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_text_splitters import RecursiveCharacterTextSplitter

from . import config, store
from .llm import get_embeddings, get_llm
from .utils import with_retry

log = logging.getLogger("notesgraph")

REWRITE_SYSTEM = (
    "Given the chat history and the student's latest message, rewrite the latest message as a "
    "standalone question that can be understood without the history. Do NOT answer it. "
    "Return only the question."
)

ANSWER_SYSTEM = (
    "You are a study assistant answering questions about ONE lecture, using the lecture excerpts below.\n"
    "- Answer only from the excerpts and be clear and concise.\n"
    "- Do not generate timestamps, citations, or source labels.\n\n"
    "Lecture excerpts:\n{context}"
)

GENERAL_SYSTEM = (
    "Answer the student's question briefly using general knowledge, not the lecture. "
    "Do not invent citations or timestamps."
)

LOCATE_PATTERNS = (
    re.compile(r"\b(?:where|when)\b", re.IGNORECASE),
    re.compile(r"\b(?:at what point|which part|what time|what timestamp)\b", re.IGNORECASE),
    re.compile(r"\b(?:locate|find)\b.{0,30}\b(?:lecture|recording|speaker|mention|discuss)\b", re.IGNORECASE),
)

GENERAL_REQUEST_PATTERN = re.compile(
    r"\b(?:in general|generally|outside (?:of )?(?:the )?lecture|general explanation|general answer)\b",
    re.IGNORECASE,
)

_STOP_WORDS = {
    "a", "about", "after", "all", "an", "and", "are", "as", "at", "be", "because",
    "before", "between", "but", "by", "can", "could", "did", "do", "does", "explain",
    "for", "from", "give", "he", "how", "i", "in", "into", "is", "it", "lecture",
    "locate", "find", "me", "mention", "mentioned", "more", "of", "on", "or", "part",
    "discuss", "cover", "talk", "say", "says",
    "she", "show", "speaker", "tell", "that", "the", "their", "them", "there", "this",
    "to", "was", "we", "what", "when", "where", "which", "who", "why", "with", "would",
}

_histories: Dict[str, InMemoryChatMessageHistory] = {}


def _session_key(user_id: str, lecture_id: str, session_id: str) -> str:
    return f"{user_id}:{lecture_id}:{session_id}" if user_id else f"{lecture_id}:{session_id}"


def _get_history(session_key: str) -> BaseChatMessageHistory:
    if session_key not in _histories:
        _histories[session_key] = InMemoryChatMessageHistory()
    return _histories[session_key]


def clear_history(user_id: str, session_id: str) -> None:
    for key in [key for key in _histories if key.startswith(f"{user_id}:") and key.endswith(f":{session_id}")]:
        del _histories[key]


def clear_lecture_history(user_id: str, lecture_id: str) -> None:
    prefix = f"{user_id}:{lecture_id}:"
    for key in [key for key in _histories if key.startswith(prefix)]:
        del _histories[key]


def format_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{secs:02}" if hours else f"{minutes:02}:{secs:02}"


def _documents(transcript: str, segments: list[dict]) -> list[Document]:
    if not segments:
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=config.RAG_CHUNK_SIZE, chunk_overlap=config.RAG_CHUNK_OVERLAP
        )
        return [Document(page_content=text, metadata={}) for text in splitter.split_text(transcript)]

    documents, text_parts, chunk_segments = [], [], []
    start_time = segments[0]["start"]
    end_time = start_time
    char_count = 0
    for segment in segments:
        text = segment["text"].strip()
        if not text:
            continue
        if text_parts and char_count + len(text) > config.RAG_CHUNK_SIZE:
            documents.append(
                Document(
                    page_content=" ".join(text_parts),
                    metadata={
                        "start_seconds": start_time,
                        "end_seconds": end_time,
                        "segments": chunk_segments,
                    },
                )
            )
            text_parts, chunk_segments, char_count = [], [], 0
            start_time = segment["start"]
        text_parts.append(text)
        char_count += len(text)
        end_time = segment["end"]
        chunk_segments.append(
            {"start": segment["start"], "end": segment["end"], "text": text}
        )
    if text_parts:
        documents.append(
            Document(
                page_content=" ".join(text_parts),
                metadata={
                    "start_seconds": start_time,
                    "end_seconds": end_time,
                    "segments": chunk_segments,
                },
            )
        )
    return documents


def index_lecture(
    lecture_id: str, transcript: str, user_id: str, segments: list[dict] | None = None
) -> int:
    """Embed a lecture into the owner's Supabase pgvector rows."""
    docs = _documents(transcript, segments or [])
    client = store.client()
    client.table("lecture_chunks").delete().eq("lecture_id", lecture_id).eq("user_id", user_id).execute()
    batch_size = 8
    for offset in range(0, len(docs), batch_size):
        batch = docs[offset : offset + batch_size]
        log.info(
            "Embedding lecture %s chunks %d-%d of %d",
            lecture_id,
            offset + 1,
            offset + len(batch),
            len(docs),
        )
        embeddings = with_retry(
            lambda batch=batch: get_embeddings().embed_documents(
                [doc.page_content for doc in batch]
            ),
            tries=6,
            base_delay=2.0,
            max_delay=30.0,
        )
        rows = [
            {
                "user_id": user_id,
                "lecture_id": lecture_id,
                "chunk_number": offset + index,
                "content": doc.page_content,
                "metadata": doc.metadata,
                "embedding": embedding,
            }
            for index, (doc, embedding) in enumerate(zip(batch, embeddings))
        ]
        with_retry(
            lambda rows=rows: client.table("lecture_chunks").insert(rows).execute(),
            tries=5,
            base_delay=5.0,
        )
    return len(docs)


def delete_lecture_vectors(lecture_id: str, user_id: str) -> None:
    store.client().table("lecture_chunks").delete().eq("lecture_id", lecture_id).eq(
        "user_id", user_id
    ).execute()


def _retrieve(
    question: str, lecture_id: str, user_id: str, match_count: int | None = None
) -> list[Document]:
    embedding = get_embeddings().embed_query(question)
    rows = (
        store.client()
        .rpc(
            "match_lecture_chunks",
            {
                "query_embedding": embedding,
                "match_count": match_count or config.RAG_TOP_K,
                "filter_lecture_id": lecture_id,
                "filter_user_id": user_id,
            },
        )
        .execute()
        .data
    )
    return [
        Document(
            page_content=row["content"],
            metadata={
                **row.get("metadata", {}),
                "chunk": row["id"],
                "similarity": float(row.get("similarity") or 0),
            },
        )
        for row in rows
    ]


def classify_intent(question: str) -> str:
    return "locate" if any(pattern.search(question) for pattern in LOCATE_PATTERNS) else "explain"


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if len(token) > 1 and token not in _STOP_WORDS
    }


def _best_segment(doc: Document, question: str) -> dict:
    segments = doc.metadata.get("segments") or [
        {
            "start": doc.metadata.get("start_seconds"),
            "end": doc.metadata.get("end_seconds"),
            "text": doc.page_content,
        }
    ]
    question_tokens = _tokens(question)
    ranked = []
    for index, segment in enumerate(segments):
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        overlap = len(question_tokens & _tokens(text))
        score = overlap / max(1, len(question_tokens))
        ranked.append((score, -index, segment))
    if not ranked:
        return {
            "start": doc.metadata.get("start_seconds"),
            "end": doc.metadata.get("end_seconds"),
            "text": doc.page_content,
            "segment_score": 0.0,
            "similarity": doc.metadata.get("similarity", 0.0),
        }
    score, _, segment = max(ranked, key=lambda item: (item[0], item[1]))
    return {
        "start": segment.get("start"),
        "end": segment.get("end"),
        "text": segment["text"].strip(),
        "segment_score": score,
        "similarity": float(doc.metadata.get("similarity") or 0),
    }


def _source(start: float, end: float, text: str) -> dict:
    start = max(0.0, float(start))
    end = max(start, float(end))
    label = format_timestamp(start)
    if end > start:
        label = f"{label}-{format_timestamp(end)}"
    return {
        "start": start,
        "end": end,
        "timestamp_label": label,
        "text_snippet": text[:300],
    }


def _explain_sources(hits: list[dict]) -> list[dict]:
    sources = []
    seen = set()
    for hit in sorted(hits, key=lambda item: item["similarity"], reverse=True):
        if hit["start"] is None:
            continue
        key = (round(float(hit["start"]), 1), hit["text"].casefold())
        if key in seen:
            continue
        seen.add(key)
        sources.append(_source(hit["start"], hit["start"], hit["text"]))
        if len(sources) == 3:
            break
    return sorted(sources, key=lambda source: source["start"])


def _locate_sources(hits: list[dict]) -> list[dict]:
    ordered = sorted(
        (hit for hit in hits if hit["start"] is not None),
        key=lambda hit: (float(hit["start"]), float(hit["end"] or hit["start"])),
    )
    merged = []
    for hit in ordered:
        start = float(hit["start"])
        end = max(start, float(hit["end"] or start))
        if merged and start <= merged[-1]["end"] + 20:
            merged[-1]["end"] = max(merged[-1]["end"], end)
            merged[-1]["count"] += 1
            if hit["text"] not in merged[-1]["texts"]:
                merged[-1]["texts"].append(hit["text"])
        else:
            merged.append(
                {"start": start, "end": end, "count": 1, "texts": [hit["text"]]}
            )
    sources = [
        _source(
            item["start"],
            item["end"] if item["count"] > 1 else item["start"],
            " ".join(item["texts"]),
        )
        for item in merged
    ]
    return sources[:15]


def _format_snippets(hits: list[dict]) -> str:
    return "\n\n---\n\n".join(
        f"Excerpt {index}: {hit['text'][:1200]}"
        for index, hit in enumerate(hits[:6], start=1)
    ) or "(no excerpts found)"


def _rewrite_question(llm, question: str, history: list) -> str:
    rewrite = (
        ChatPromptTemplate.from_messages(
            [("system", REWRITE_SYSTEM), MessagesPlaceholder("history"), ("human", "{input}")]
        )
        | llm
        | StrOutputParser()
    )
    return rewrite.invoke(
        {"input": question, "history": history[-config.HISTORY_WINDOW :]}
    ).strip()


def _answer(llm, question: str, context: str, history: list) -> str:
    answer = (
        ChatPromptTemplate.from_messages(
            [("system", ANSWER_SYSTEM), MessagesPlaceholder("history"), ("human", "{input}")]
        )
        | llm
        | StrOutputParser()
    )
    return answer.invoke(
        {
            "input": question,
            "context": context,
            "history": history[-config.HISTORY_WINDOW :],
        }
    ).strip()


def ask(
    lecture_id: str,
    session_id: str,
    question: str,
    user_id: str = "",
    history: list[dict] | None = None,
) -> dict:
    key = _session_key(user_id, lecture_id, session_id)
    if history is not None:
        messages = []
        for item in history:
            if item["role"] == "user":
                messages.append(HumanMessage(content=item["content"]))
            elif item["role"] == "assistant":
                messages.append(AIMessage(content=item["content"]))
        _get_history(key).messages = messages
    chat_history = _get_history(key).messages
    llm = None
    standalone = question
    if chat_history:
        llm = get_llm(temperature=0.3)
        standalone = with_retry(
            lambda: _rewrite_question(llm, question, chat_history), tries=1
        )

    intent = classify_intent(standalone)
    candidates = _retrieve(
        standalone,
        lecture_id,
        user_id,
        config.RAG_LOCATE_K if intent == "locate" else config.RAG_TOP_K,
    )
    hits = [
        _best_segment(doc, standalone)
        for doc in candidates
        if float(doc.metadata.get("similarity") or 0) >= config.RAG_MIN_SCORE
    ]
    if not hits:
        answer = "This lecture doesn't appear to cover that topic. Ask if you'd like a short general explanation."
        if GENERAL_REQUEST_PATTERN.search(question):
            llm = llm or get_llm(temperature=0.3)
            general = (
                ChatPromptTemplate.from_messages(
                    [("system", GENERAL_SYSTEM), ("human", "{input}")]
                )
                | llm
                | StrOutputParser()
            )
            general_answer = with_retry(
                lambda: general.invoke({"input": question}), tries=1
            ).strip()
            answer = f"{answer}\n\nOutside the lecture: {general_answer}"
        result = {
            "answer": answer,
            "sources": [],
            "intent": "off_topic",
            "grounded": False,
        }
    else:
        llm = llm or get_llm(temperature=0.3)
        answer = with_retry(
            lambda: _answer(llm, question, _format_snippets(hits), chat_history),
            tries=1,
        )
        sources = _locate_sources(hits) if intent == "locate" else _explain_sources(hits)
        result = {
            "answer": answer,
            "sources": sources,
            "intent": intent,
            "grounded": True,
        }

    _get_history(key).add_user_message(question)
    _get_history(key).add_ai_message(result["answer"])
    return result
