"""Central configuration, read from environment / .env."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR.parent / "data"))
UPLOADS_DIR = DATA_DIR / "uploads"
FRONTEND_DIST = BASE_DIR.parent / "frontend" / "dist"
FONTS_DIR = BASE_DIR / "fonts"

for _d in (UPLOADS_DIR,):
    _d.mkdir(parents=True, exist_ok=True)

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_PUBLISHABLE_KEY = os.getenv("SUPABASE_PUBLISHABLE_KEY") or os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_SECRET_KEY = os.getenv("SUPABASE_SECRET_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
FRONTEND_ORIGINS = [
    origin.strip()
    for origin in os.getenv("FRONTEND_ORIGINS", "http://localhost:5173").split(",")
    if origin.strip()
]

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GROQ_NOTES_MODEL = os.getenv("GROQ_NOTES_MODEL", "openai/gpt-oss-120b")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "gemini-embedding-001")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-large-v3-turbo")
WHISPER_HINT = os.getenv(
    "WHISPER_HINT",
    "University lecture. May mix English, Hindi and Gujarati; write everything in Roman script.",
)

CHUNK_MINUTES = int(os.getenv("CHUNK_MINUTES", "10"))
RAG_CHUNK_SIZE = int(os.getenv("RAG_CHUNK_SIZE", "400"))
RAG_CHUNK_OVERLAP = int(os.getenv("RAG_CHUNK_OVERLAP", "80"))
RAG_TOP_K = int(os.getenv("RAG_TOP_K", "10"))
RAG_LOCATE_K = int(os.getenv("RAG_LOCATE_K", "20"))
RAG_MIN_SCORE = float(os.getenv("RAG_MIN_SCORE", "0.35"))
RAG_LEXICAL_MIN_MATCHES = int(os.getenv("RAG_LEXICAL_MIN_MATCHES", "1"))
DEBUG_CITATIONS = os.getenv("DEBUG_CITATIONS", "").strip().lower() in {"1", "true", "yes", "on"}
HISTORY_WINDOW = int(os.getenv("HISTORY_WINDOW", "10"))  # messages kept in the prompt

ALLOWED_AUDIO_EXT = {".mp3", ".wav", ".m4a", ".mp4", ".aac", ".ogg", ".flac", ".webm", ".opus", ".wma"}
