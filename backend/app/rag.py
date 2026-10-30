"""Supabase pgvector retrieval and isolated, persistent-session chat."""
from functools import lru_cache
import logging
from typing import Dict

from langchain_core.chat_history import BaseChatMessageHistory, InMemoryChatMessageHistory
from langchain_core.documents import Document
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import RunnableLambda, RunnablePassthrough
from langchain_core.runnables.history import RunnableWithMessageHistory
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
    "- Answer from the excerpts and be clear and concise.\n"
    "- When a topic is discussed in the audio, cite its exact provided audio time using [MM:SS] or [HH:MM:SS]. "
    "Use only timestamps present in excerpt labels; never estimate or invent them.\n"
    "- If the transcript has no audio timestamps, explicitly say that timestamps are unavailable.\n"
    "- If the excerpts do not contain the answer, say the lecture doesn't seem to cover it. You may then add "
    "a short general explanation, clearly labelled 'Outside the lecture:'.\n\n"
    "Lecture excerpts:\n{context}"
)


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


def _format_docs(docs) -> str:
    excerpts = []
    for doc in docs:
        start, end = doc.metadata.get("start_seconds"), doc.metadata.get("end_seconds")
        label = f"[Audio {format_timestamp(start)}-{format_timestamp(end)}] " if start is not None else ""
        excerpts.append(f"{label}{doc.page_content}")
    return "\n\n---\n\n".join(excerpts) or "(no excerpts found)"


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

    documents, text_parts = [], []
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
                    metadata={"start_seconds": start_time, "end_seconds": end_time},
                )
            )
            text_parts, char_count = [], 0
            start_time = segment["start"]
        text_parts.append(text)
        char_count += len(text)
        end_time = segment["end"]
    if text_parts:
        documents.append(
            Document(
                page_content=" ".join(text_parts),
                metadata={"start_seconds": start_time, "end_seconds": end_time},
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


def build_chain(llm, retrieve) -> RunnableWithMessageHistory:
    """retrieve(question, lecture_id, user_id) -> relevant Documents."""
    rewrite = (
        ChatPromptTemplate.from_messages(
            [("system", REWRITE_SYSTEM), MessagesPlaceholder("history"), ("human", "{input}")]
        )
        | llm
        | StrOutputParser()
    )
    answer = (
        ChatPromptTemplate.from_messages(
            [("system", ANSWER_SYSTEM), MessagesPlaceholder("history"), ("human", "{input}")]
        )
        | llm
        | StrOutputParser()
    )

    def standalone(inputs: dict) -> str:
        history = inputs.get("history") or []
        if not history:
            return inputs["input"]
        return rewrite.invoke(
            {"input": inputs["input"], "history": history[-config.HISTORY_WINDOW :]}
        ).strip()

    def fetch(inputs: dict) -> list:
        return retrieve(
            standalone(inputs), inputs["lecture_id"], inputs.get("user_id", "")
        )

    core = (
        RunnablePassthrough.assign(docs=RunnableLambda(fetch))
        | RunnablePassthrough.assign(context=lambda values: _format_docs(values["docs"]))
        | RunnablePassthrough.assign(
            answer=RunnableLambda(
                lambda values: {
                    **values,
                    "history": (values.get("history") or [])[-config.HISTORY_WINDOW :],
                }
            )
            | answer
        )
    )
    return RunnableWithMessageHistory(
        core,
        _get_history,
        input_messages_key="input",
        history_messages_key="history",
        output_messages_key="answer",
    )


def _retrieve(question: str, lecture_id: str, user_id: str) -> list[Document]:
    embedding = get_embeddings().embed_query(question)
    rows = (
        store.client()
        .rpc(
            "match_lecture_chunks",
            {
                "query_embedding": embedding,
                "match_count": config.RAG_TOP_K,
                "filter_lecture_id": lecture_id,
                "filter_user_id": user_id,
            },
        )
        .execute()
        .data
    )
    return [
        Document(page_content=row["content"], metadata={**row.get("metadata", {}), "chunk": row["id"]})
        for row in rows
    ]


@lru_cache(maxsize=1)
def _chain() -> RunnableWithMessageHistory:
    return build_chain(get_llm(temperature=0.3), _retrieve)


def ask(
    lecture_id: str,
    session_id: str,
    question: str,
    user_id: str = "",
    history: list[dict] | None = None,
    chain=None,
) -> dict:
    chain = chain or _chain()
    key = _session_key(user_id, lecture_id, session_id)
    if history is not None:
        messages = []
        for item in history:
            if item["role"] == "user":
                messages.append(HumanMessage(content=item["content"]))
            elif item["role"] == "assistant":
                messages.append(AIMessage(content=item["content"]))
        _get_history(key).messages = messages
    result = with_retry(
        lambda: chain.invoke(
            {"input": question, "lecture_id": lecture_id, "user_id": user_id},
            config={"configurable": {"session_id": key}},
        ),
        tries=1,
    )
    sources = [
        {
            "chunk": doc.metadata.get("chunk"),
            "text": doc.page_content[:300],
            "start_seconds": doc.metadata.get("start_seconds"),
            "end_seconds": doc.metadata.get("end_seconds"),
            "timestamp": (
                format_timestamp(doc.metadata["start_seconds"])
                if doc.metadata.get("start_seconds") is not None
                else None
            ),
        }
        for doc in result["docs"]
    ]
    return {"answer": result["answer"], "sources": sources}
