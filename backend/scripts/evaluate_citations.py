"""Compare citation timestamps with expected times from a JSON list.

Run from the repository root:
python backend/scripts/evaluate_citations.py --lecture-id ID --user-id ID --pairs pairs.json

The JSON file is a list of objects with ``question`` and ``expected_time_seconds`` keys.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rag import ask, format_timestamp  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lecture-id", required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--pairs", required=True, type=Path)
    args = parser.parse_args()

    pairs = json.loads(args.pairs.read_text(encoding="utf-8"))
    if not isinstance(pairs, list) or not 5 <= len(pairs) <= 10:
        raise ValueError("The pairs JSON must be a list of 5 to 10 questions.")

    print("Question | Cited time | Expected time | Error (seconds)")
    for index, pair in enumerate(pairs):
        expected = float(pair["expected_time_seconds"])
        result = ask(
            args.lecture_id,
            f"citation-eval-{index}",
            pair["question"],
            user_id=args.user_id,
        )
        starts = [
            float(source["start"])
            for source in result["sources"]
            if source.get("start") is not None
        ]
        if starts:
            cited = min(starts, key=lambda value: abs(value - expected))
            print(
                f"{pair['question']} | {format_timestamp(cited)} | "
                f"{format_timestamp(expected)} | {abs(cited - expected):.1f}"
            )
        else:
            print(
                f"{pair['question']} | N/A | {format_timestamp(expected)} | N/A"
            )


if __name__ == "__main__":
    main()
