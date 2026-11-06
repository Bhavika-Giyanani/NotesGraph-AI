"""Supabase pgvector retrieval and isolated, persistent-session chat."""
import json
import logging
import re
import statistics
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
    "You are a study assistant answering questions about ONE lecture, using the numbered snippets below.\n"
    "- Return only valid JSON with exactly these fields: answer (string), grounded (boolean), "
    "supporting_snippet_ids (array of integer snippet IDs).\n"
    "- Set grounded=true only if the snippets directly answer the student's question. "
    "Select only IDs for snippets that support your answer. If none answer it, set grounded=false "
    "and supporting_snippet_ids=[].\n"
    "- When grounded=false, say the lecture doesn't appear to cover the question. "
    "Give general knowledge only when the student explicitly asks for it.\n"
    "- Never include timestamps, citations, or source labels. Do not invent snippet IDs.\n\n"
    "Lecture excerpts:\n{context}"
)

GENERAL_SYSTEM = (
    "Answer the student's question in at most 150 words using general knowledge, not the lecture. "
    "Do not invent citations or timestamps."
)

CONVERSATION_SYSTEM = (
    "Answer the student's question only from the conversation history in this chat. "
    "Do not use the lecture or outside knowledge. If the history does not contain the answer, say so."
)

CONVERSATION_CLASSIFIER_SYSTEM = (
    "Decide whether the student's latest message asks about the chat history itself, such as "
    "their earlier questions or what was said in this conversation. Reply with exactly YES or NO."
)

OFFER_ACCEPTANCE_SYSTEM = (
    "Decide whether the student's short reply accepts the assistant's offer to answer the "
    "stored question using general knowledge. Reply with exactly YES or NO. Treat topic changes, "
    "new questions, rejection, or uncertainty as NO."
)

LOCATE_PATTERNS = (
    re.compile(r"\b(?:where|when)\b", re.IGNORECASE),
    re.compile(r"\b(?:at what point|which part|what time|what timestamp)\b", re.IGNORECASE),
    re.compile(r"\b(?:locate|find)\b.{0,30}\b(?:lecture|recording|speaker|mention|discuss)\b", re.IGNORECASE),
)

CONVERSATION_PATTERNS = (
    re.compile(r"\bwhat\s+(?:did|have)\s+i\s+ask(?:ed)?\b", re.IGNORECASE),
    re.compile(r"\bmy\s+(?:previous|last|earlier)\s+question\b", re.IGNORECASE),
    re.compile(r"\b(?:summari[sz]e|recap)\s+(?:our|this|the)\s+conversation\b", re.IGNORECASE),
    re.compile(r"\brepeat\s+(?:that|it|what\s+(?:you|i)\s+said)\b", re.IGNORECASE),
)
CONVERSATION_CANDIDATE_PATTERN = re.compile(
    r"\b(?:previous|earlier|before|last|chat|conversation|recap|repeat|remember|"
    r"asked|question|said)\b",
    re.IGNORECASE,
)

FOLLOWUP_PATTERNS = (
    re.compile(r"\b(?:it|that|this|these|those|they|them|he|she)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:more simply|in simpler terms|give (?:me )?(?:an )?example|"
        r"(?:another|one more) example|explain (?:it|that|this) simply)\b",
        re.IGNORECASE,
    ),
)

OFFER_AFFIRMATIVE_PATTERN = re.compile(
    r"^(?:go ahead|yes|sure|ok(?:ay)?|please do|yes please|tell me|"
    r"please|do it|sounds good|that would be great)[.! ]*$",
    re.IGNORECASE,
)
OFFER_NEGATIVE_PATTERN = re.compile(
    r"^(?:no|no thanks|not now|never mind|don't|do not|skip it)[.! ]*$",
    re.IGNORECASE,
)
OFFER_SUBSTANTIVE_PATTERN = re.compile(
    r"\b(?:what|why|how|where|when|who|which|explain|define|describe|compare|"
    r"summarize|tell me about)\b|[?()]",
    re.IGNORECASE,
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


def _citation_debug(message: str, *args) -> None:
    if config.DEBUG_CITATIONS:
        log.info("DEBUG_CITATIONS " + message, *args)


def _log_segment_distribution(lecture_id: str, segments: list[dict]) -> None:
    durations = [
        float(segment["end"]) - float(segment["start"])
        for segment in segments
        if segment.get("start") is not None
        and segment.get("end") is not None
        and float(segment["end"]) >= float(segment["start"])
    ]
    distribution = (
        {
            "min": min(durations),
            "median": statistics.median(durations),
            "max": max(durations),
        }
        if durations
        else {"min": None, "median": None, "max": None}
    )
    _citation_debug(
        "lecture=%s segment_duration_count=%d distribution_seconds=%s "
        "CHUNK_MINUTES=%s RAG_CHUNK_SIZE=%s RAG_CHUNK_OVERLAP=%s",
        lecture_id,
        len(durations),
        distribution,
        config.CHUNK_MINUTES,
        config.RAG_CHUNK_SIZE,
        config.RAG_CHUNK_OVERLAP,
    )


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

    documents: list[Document] = []
    chunk_segments: list[dict] = []
    char_count = 0

    def append_chunk() -> None:
        if not chunk_segments:
            return
        copied_segments = [dict(segment) for segment in chunk_segments]
        documents.append(
            Document(
                page_content=" ".join(segment["text"] for segment in copied_segments),
                metadata={
                    "start_seconds": copied_segments[0]["start"],
                    "end_seconds": copied_segments[-1]["end"],
                    "segments": copied_segments,
                },
            )
        )

    for segment in segments:
        text = segment["text"].strip()
        if not text:
            continue
        if chunk_segments and char_count + len(text) + 1 > config.RAG_CHUNK_SIZE:
            append_chunk()
            overlap: list[dict] = []
            overlap_chars = 0
            for previous in reversed(chunk_segments):
                contribution = len(previous["text"]) + (1 if overlap else 0)
                if overlap and overlap_chars + contribution > config.RAG_CHUNK_OVERLAP:
                    break
                if not overlap and len(previous["text"]) > config.RAG_CHUNK_OVERLAP:
                    break
                overlap.insert(0, previous)
                overlap_chars += contribution
            chunk_segments = overlap
            char_count = sum(len(item["text"]) for item in chunk_segments) + max(
                0, len(chunk_segments) - 1
            )
        chunk_segments.append(
            {"start": segment["start"], "end": segment["end"], "text": text}
        )
        char_count += len(text) + (1 if len(chunk_segments) > 1 else 0)
    append_chunk()
    return documents


def index_lecture(
    lecture_id: str, transcript: str, user_id: str, segments: list[dict] | None = None
) -> int:
    """Embed a lecture into the owner's Supabase pgvector rows."""
    _log_segment_distribution(lecture_id, segments or [])
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
                [doc.page_content for doc in batch], output_dimensionality=768
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
    embedding = get_embeddings().embed_query(question, output_dimensionality=768)
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


def _first_matching_segment(doc: Document, question: str) -> dict | None:
    terms = _tokens(question)
    segments = doc.metadata.get("segments")
    if not isinstance(segments, list):
        return None
    for segment in segments:
        text = str(segment.get("text") or "").strip()
        if terms & _tokens(text):
            return segment
    return None


def _citation_runs(doc: Document, question: str, duration: float | None) -> list[dict]:
    segments = doc.metadata.get("segments")
    if not isinstance(segments, list) or not segments:
        log.warning(
            "Skipping citation for chunk %s: segment metadata is missing",
            doc.metadata.get("chunk"),
        )
        return []
    if duration is None or duration <= 0:
        log.warning(
            "Skipping citation for chunk %s: lecture duration is unavailable",
            doc.metadata.get("chunk"),
        )
        return []

    terms = _tokens(question)
    runs: list[list[dict]] = []
    current: list[dict] = []
    previous_index: int | None = None
    for index, segment in enumerate(segments):
        text = str(segment.get("text") or "").strip()
        start, end = segment.get("start"), segment.get("end")
        segment_terms = terms & _tokens(text)
        if not segment_terms or start is None or end is None:
            if current:
                runs.append(current)
                current = []
            previous_index = None
            continue
        start, end = float(start), float(end)
        if start < 0 or end <= start or start >= duration:
            if current:
                runs.append(current)
                current = []
            previous_index = None
            continue
        segment = {
            "start": start,
            "end": min(end, duration),
            "text": text,
        }
        if current and previous_index == index - 1:
            current.append(segment)
        else:
            if current:
                runs.append(current)
            current = [segment]
        previous_index = index
    if current:
        runs.append(current)

    hits = []
    for run in runs:
        start = run[0]["start"]
        end = run[-1]["end"]
        if not start < end <= duration:
            continue
        _citation_debug(
            "chunk=%s chosen_segment_run=(%s,%s,%r)",
            doc.metadata.get("chunk"),
            start,
            end,
            " ".join(segment["text"] for segment in run)[:300],
        )
        hits.append(
            {
                "start": start,
                "end": end,
                "is_range": len(run) > 1,
                "text": " ".join(segment["text"] for segment in run),
            }
        )
    return hits


def _source(start: float, end: float, text: str, is_range: bool) -> dict:
    label = format_timestamp(start)
    if is_range and end > start:
        label = f"{label}-{format_timestamp(end)}"
    return {
        "start": start,
        "end": end if is_range else start,
        "timestamp_label": label,
        "text_snippet": text[:300],
    }


def _locate_sources(hits: list[dict], duration: float | None) -> list[dict]:
    if duration is None or duration <= 0:
        return []
    unique_hits = {
        (hit["start"], hit["end"], hit["text"].casefold()): hit for hit in hits
    }
    ordered = sorted(unique_hits.values(), key=lambda hit: (hit["start"], hit["end"]))
    merged = []
    for hit in ordered:
        start = float(hit["start"])
        end = min(float(hit["end"]), duration)
        if start < 0 or start >= duration or end <= start:
            continue
        if (
            merged
            and start - merged[-1]["end"] <= 20
            and max(merged[-1]["end"], end) - merged[-1]["start"] <= 90
        ):
            merged[-1]["end"] = max(merged[-1]["end"], end)
            merged[-1]["is_range"] = True
            if hit["text"] not in merged[-1]["texts"]:
                merged[-1]["texts"].append(hit["text"])
        else:
            merged.append(
                {
                    "start": start,
                    "end": end,
                    "is_range": hit["is_range"],
                    "texts": [hit["text"]],
                }
            )
    sources = [
        _source(
            item["start"],
            item["end"],
            " ".join(item["texts"]),
            item["is_range"],
        )
        for item in merged
    ]
    return sources[:15]


def _format_snippets(docs: list[Document]) -> str:
    return "\n\n---\n\n".join(
        f"Snippet {index}: {doc.page_content[:config.RAG_CHUNK_SIZE]}"
        for index, doc in enumerate(docs)
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
        {"input": question, "history": history[-config.FOLLOWUP_HISTORY_WINDOW :]}
    ).strip()


def _is_conversation_question(question: str) -> bool:
    return any(pattern.search(question) for pattern in CONVERSATION_PATTERNS)


def _classify_conversation_question(llm, question: str) -> bool:
    chain = ChatPromptTemplate.from_messages(
        [
            ("system", CONVERSATION_CLASSIFIER_SYSTEM),
            ("human", "{input}"),
        ]
    ) | llm | StrOutputParser()
    answer = chain.invoke({"input": question}).strip()
    return answer.casefold() == "yes"


def _needs_followup_rewrite(question: str) -> bool:
    return any(pattern.search(question) for pattern in FOLLOWUP_PATTERNS)


def _is_affirmative_offer_reply(question: str) -> bool:
    return bool(OFFER_AFFIRMATIVE_PATTERN.fullmatch(question.strip()))


def _is_ambiguous_short_reply(question: str) -> bool:
    return (
        0 < len(question.split()) <= 4
        and not OFFER_NEGATIVE_PATTERN.fullmatch(question.strip())
        and not OFFER_SUBSTANTIVE_PATTERN.search(question)
    )


def _classify_offer_acceptance(llm, question: str, offered_question: str) -> bool:
    chain = ChatPromptTemplate.from_messages(
        [
            ("system", OFFER_ACCEPTANCE_SYSTEM),
            (
                "human",
                "Offered question: {offered_question}\nStudent's reply: {reply}",
            ),
        ]
    ) | llm | StrOutputParser()
    answer = chain.invoke(
        {"offered_question": offered_question, "reply": question}
    ).strip()
    return answer.casefold() == "yes"


def _conversation_answer(llm, question: str, history: list) -> str:
    if not any(isinstance(item, HumanMessage) for item in history):
        return "You haven't asked any earlier questions in this chat."
    chain = (
        ChatPromptTemplate.from_messages(
            [
                ("system", CONVERSATION_SYSTEM),
                MessagesPlaceholder("history"),
                ("human", "{input}"),
            ]
        )
        | llm
        | StrOutputParser()
    )
    return chain.invoke(
        {"input": question, "history": history[-config.HISTORY_WINDOW :]}
    ).strip()


def _general_answer(llm, question: str) -> str:
    chain = ChatPromptTemplate.from_messages(
        [("system", GENERAL_SYSTEM), ("human", "{input}")]
    ) | llm | StrOutputParser()
    answer = chain.invoke({"input": question}).strip()
    return " ".join(answer.split()[:150])


def _answer(llm, question: str, context: str, history: list) -> dict:
    answer = (
        ChatPromptTemplate.from_messages(
            [("system", ANSWER_SYSTEM), MessagesPlaceholder("history"), ("human", "{input}")]
        )
        | llm
        | StrOutputParser()
    )
    raw = answer.invoke(
        {
            "input": question,
            "context": context,
            "history": history[-config.HISTORY_WINDOW :],
        }
    ).strip()
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        log.warning("Chat answer returned malformed grounding JSON")
        return {"answer": "I couldn't verify an answer from the lecture excerpts.", "grounded": False, "supporting_snippet_ids": []}
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("answer"), str)
        or not isinstance(payload.get("grounded"), bool)
        or not isinstance(payload.get("supporting_snippet_ids"), list)
        or any(
            not isinstance(snippet_id, int) or isinstance(snippet_id, bool)
            for snippet_id in payload["supporting_snippet_ids"]
        )
    ):
        log.warning("Chat answer returned invalid grounding JSON fields")
        return {"answer": "I couldn't verify an answer from the lecture excerpts.", "grounded": False, "supporting_snippet_ids": []}
    payload["supporting_snippet_ids"] = list(dict.fromkeys(payload["supporting_snippet_ids"]))
    return payload


def ask(
    lecture_id: str,
    session_id: str,
    question: str,
    user_id: str = "",
    history: list[dict] | None = None,
    accept_offer: bool = False,
) -> dict:
    key = _session_key(user_id, lecture_id, session_id)
    history_rows = history or []
    if history is not None:
        messages = []
        for item in history:
            if item.get("role") == "user":
                messages.append(HumanMessage(content=item.get("content", "")))
            elif item.get("role") == "assistant":
                messages.append(AIMessage(content=item.get("content", "")))
        _get_history(key).messages = messages
    chat_history = _get_history(key).messages
    llm = None

    is_conversation_question = _is_conversation_question(question)
    if (
        not is_conversation_question
        and CONVERSATION_CANDIDATE_PATTERN.search(question)
    ):
        llm = get_llm(temperature=0)
        is_conversation_question = with_retry(
            lambda: _classify_conversation_question(llm, question),
            tries=1,
        )
    if is_conversation_question:
        recent_history = chat_history[-config.HISTORY_WINDOW :]
        if any(isinstance(item, HumanMessage) for item in recent_history):
            llm = llm or get_llm(temperature=0.3)
            answer = with_retry(
                lambda: _conversation_answer(llm, question, recent_history),
                tries=1,
            )
        else:
            answer = _conversation_answer(None, question, recent_history)
        result = {
            "answer": answer,
            "sources": [],
            "intent": "conversation",
            "grounded": False,
        }
        _get_history(key).add_user_message(question)
        _get_history(key).add_ai_message(answer)
        return result

    previous_assistant = next(
        (item for item in reversed(history_rows) if item.get("role") == "assistant"),
        None,
    )
    pending_offer = (
        previous_assistant.get("pending_offer")
        if previous_assistant is not None
        else None
    )
    offer_is_pending = (
        isinstance(pending_offer, dict)
        and pending_offer.get("type") == "general_answer"
        and isinstance(pending_offer.get("question"), str)
    )
    accepted_offer = accept_offer or (
        offer_is_pending and _is_affirmative_offer_reply(question)
    )
    if (
        offer_is_pending
        and not accepted_offer
        and _is_ambiguous_short_reply(question)
    ):
        llm = get_llm(temperature=0)
        accepted_offer = with_retry(
            lambda: _classify_offer_acceptance(
                llm, question, pending_offer["question"]
            ),
            tries=1,
        )
    if offer_is_pending and accepted_offer:
        llm = get_llm(temperature=0.3)
        answer = with_retry(
            lambda: _general_answer(llm, pending_offer["question"]), tries=1
        )
        result = {
            "answer": answer,
            "sources": [],
            "intent": "general",
            "grounded": False,
            "consumed_offer_message_id": previous_assistant.get("id"),
        }
        _get_history(key).add_user_message(question)
        _get_history(key).add_ai_message(answer)
        return result

    standalone = question
    if chat_history and _needs_followup_rewrite(question):
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
    question_terms = _tokens(standalone)
    scored_docs = []
    lexical_matches = set()
    for doc in candidates:
        score = float(doc.metadata.get("similarity") or 0)
        matched_terms = question_terms & _tokens(doc.page_content)
        chosen = _first_matching_segment(doc, standalone)
        _citation_debug(
            "chunk=%s score=%.5f chunk_start=%s chunk_end=%s has_segments=%s "
            "lexical_terms=%s chosen_segment=(%s,%s,%r)",
            doc.metadata.get("chunk"),
            score,
            doc.metadata.get("start_seconds"),
            doc.metadata.get("end_seconds"),
            bool(doc.metadata.get("segments")),
            sorted(matched_terms),
            chosen.get("start") if chosen else None,
            chosen.get("end") if chosen else None,
            str(chosen.get("text") or "")[:300] if chosen else "",
        )
        if score >= config.RAG_MIN_SCORE:
            scored_docs.append((doc, matched_terms, score))
            lexical_matches.update(matched_terms)

    _citation_debug(
        "question=%r standalone=%r intent=%s RAG_MIN_SCORE=%s "
        "RAG_LEXICAL_MIN_MATCHES=%s lexical_matches=%s "
        "RAG_TOP_K=%s RAG_LOCATE_K=%s RAG_CHUNK_SIZE=%s RAG_CHUNK_OVERLAP=%s",
        question,
        standalone,
        intent,
        config.RAG_MIN_SCORE,
        config.RAG_LEXICAL_MIN_MATCHES,
        sorted(lexical_matches),
        config.RAG_TOP_K,
        config.RAG_LOCATE_K,
        config.RAG_CHUNK_SIZE,
        config.RAG_CHUNK_OVERLAP,
    )
    grounded_docs = [
        doc for doc, terms, _ in scored_docs if terms
    ]
    if (
        not scored_docs
        or len(lexical_matches) < config.RAG_LEXICAL_MIN_MATCHES
        or not grounded_docs
    ):
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
            "pending_offer": {
                "type": "general_answer",
                "question": question,
            },
        }
    else:
        llm = llm or get_llm(temperature=0.3)
        prompt_docs = grounded_docs[: (10 if intent == "explain" else 6)]
        parsed = with_retry(
            lambda: _answer(llm, standalone, _format_snippets(prompt_docs), chat_history),
            tries=1,
        )
        valid_support_ids = [
            snippet_id
            for snippet_id in parsed["supporting_snippet_ids"]
            if 0 <= snippet_id < len(prompt_docs)
        ]
        grounded = parsed["grounded"] and bool(valid_support_ids)
        answer = parsed["answer"]
        if not grounded:
            answer = (
                "This lecture doesn't appear to cover that topic. "
                "Ask if you'd like a short general explanation."
            )
            if GENERAL_REQUEST_PATTERN.search(question):
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

        lecture_segments = store.load_transcript_segments(lecture_id, user_id)
        duration = max(
            (
                float(segment["end"])
                for segment in lecture_segments
                if segment.get("end") is not None
            ),
            default=None,
        )
        citation_hits = []
        if grounded:
            for snippet_id in valid_support_ids:
                doc = prompt_docs[snippet_id]
                for hit in _citation_runs(doc, standalone, duration):
                    hit["similarity"] = float(doc.metadata.get("similarity") or 0)
                    citation_hits.append(hit)
        if intent == "locate":
            sources = _locate_sources(citation_hits, duration) if grounded else []
        else:
            selected_hits = sorted(
                citation_hits, key=lambda hit: hit["similarity"], reverse=True
            )
            sources = []
            seen = set()
            for hit in selected_hits:
                source_key = (
                    round(hit["start"], 3),
                    round(hit["end"], 3),
                    hit["text"].casefold(),
                )
                if source_key in seen:
                    continue
                seen.add(source_key)
                sources.append(
                    _source(hit["start"], hit["end"], hit["text"], hit["is_range"])
                )
                if len(sources) == 3:
                    break
            sources.sort(key=lambda source: source["start"])
        if not grounded:
            sources = []
        result = {
            "answer": answer,
            "sources": sources,
            "intent": intent if grounded else "off_topic",
            "grounded": grounded,
        }
        if not grounded:
            result["pending_offer"] = {
                "type": "general_answer",
                "question": question,
            }

    _citation_debug("final_sources=%s grounded=%s", result["sources"], result["grounded"])
    _get_history(key).add_user_message(question)
    _get_history(key).add_ai_message(result["answer"])
    return result
