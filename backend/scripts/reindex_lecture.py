"""Rebuild one lecture's vector chunks from saved transcript and Whisper segments.

Run from the repository root:
python backend/scripts/reindex_lecture.py --lecture-id ID --user-id ID
"""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import rag, store  # noqa: E402


def main() -> None:
    if rag.config.DEBUG_CITATIONS:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lecture-id", required=True)
    parser.add_argument("--user-id", required=True)
    args = parser.parse_args()

    lecture = store.get(args.lecture_id, args.user_id)
    if lecture is None:
        raise ValueError("Lecture not found for the specified owner.")
    transcript = store.load_transcript(args.lecture_id, args.user_id)
    segments = store.load_transcript_segments(args.lecture_id, args.user_id)
    if not transcript or not segments:
        raise ValueError(
            "Saved transcript and Whisper segments are required; this command does not retranscribe."
        )

    count = rag.index_lecture(
        args.lecture_id, transcript, args.user_id, segments
    )
    print(f"Reindexed {lecture['title']}: {count} chunks from {len(segments)} saved segments.")


if __name__ == "__main__":
    main()
