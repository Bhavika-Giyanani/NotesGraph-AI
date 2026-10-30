"""Gemini chat model + embeddings (via LangChain)."""
from functools import lru_cache

from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings

from . import config


def _require_key() -> None:
    if not config.GOOGLE_API_KEY:
        raise RuntimeError("GOOGLE_API_KEY is not set. Add it to your .env file.")


def get_llm(temperature: float = 0.2) -> ChatGoogleGenerativeAI:
    _require_key()
    return ChatGoogleGenerativeAI(
        model=config.GEMINI_MODEL,
        google_api_key=config.GOOGLE_API_KEY,
        temperature=temperature,
        retries=1,
        request_timeout=90,
    )


@lru_cache(maxsize=1)
def get_embeddings() -> GoogleGenerativeAIEmbeddings:
    _require_key()
    model = config.EMBEDDING_MODEL
    if not model.startswith("models/"):
        model = f"models/{model}"
    return GoogleGenerativeAIEmbeddings(
        model=model, google_api_key=config.GOOGLE_API_KEY, output_dimensionality=768
    )
