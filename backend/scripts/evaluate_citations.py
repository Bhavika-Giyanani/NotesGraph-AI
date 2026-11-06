"""Compare citation timestamps with expected times from a JSON list.

Run from the repository root:
python backend/scripts/evaluate_citations.py --lecture-id ID --user-id ID --pairs pairs.json

Use ``--save-baseline before.json`` before reindexing and ``--baseline before.json``
after reindexing to print before/after citation errors.
"""
import argparse
import json
import logging
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config, rag  # noqa: E402


def _baseline_citation(question: str, lecture_id: str, user_id: str, expected: float):
    intent = rag.classify_intent(question)
    count = 6 if intent == "explain" else config.RAG_LOCATE_K
    docs = rag._retrieve(question, lecture_id, user_id, count)
    hits = [
        {
            "start": float(doc.metadata.get("start_seconds") or 0),
            "end": float(doc.metadata.get("end_seconds") or 0),
            "text": doc.page_content[:300],
            "score": float(doc.metadata.get("similarity") or 0),
        }
        for doc in docs
        if float(doc.metadata.get("similarity") or 0) >= config.RAG_MIN_SCORE
    ]
    if intent == "locate":
        ordered = sorted(hits, key=lambda hit: hit["start"])
        ranges = []
        for hit in ordered:
            if ranges and hit["start"] <= ranges[-1]["end"] + 20:
                ranges[-1]["end"] = max(ranges[-1]["end"], hit["end"])
            else:
                ranges.append({"start": hit["start"], "end": hit["end"]})
        cited_times = [item["start"] for item in ranges[:15]]
    else:
        cited_times = [
            hit["start"]
            for hit in sorted(hits, key=lambda hit: hit["score"], reverse=True)[:3]
        ]
    return min(cited_times, key=lambda time: abs(time - expected)) if cited_times else None


def _current_citation(
    question: str,
    lecture_id: str,
    user_id: str,
    expected: float,
    session_id: str,
):
    result = rag.ask(
        lecture_id,
        session_id,
        question,
        user_id=user_id,
    )
    starts = [
        float(source["start"])
        for source in result["sources"]
        if source.get("start") is not None
    ]
    return min(starts, key=lambda time: abs(time - expected)) if starts else None


def main() -> None:
    if config.DEBUG_CITATIONS:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lecture-id", required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--pairs", required=True, help="JSON file path, or '-' to read JSON from stdin.")
    parser.add_argument("--save-baseline", type=Path)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()

    pairs = json.load(sys.stdin) if args.pairs == "-" else json.loads(
        Path(args.pairs).read_text(encoding="utf-8")
    )
    if not isinstance(pairs, list) or not 1 <= len(pairs) <= 10:
        raise ValueError("The pairs JSON must be a list of 1 to 10 questions.")
    if args.save_baseline and args.baseline:
        raise ValueError("Choose either --save-baseline or --baseline.")

    if args.save_baseline:
        baseline = []
        for pair in pairs:
            expected = float(pair["expected_time_seconds"])
            cited = _baseline_citation(
                pair["question"], args.lecture_id, args.user_id, expected
            )
            baseline.append(
                {
                    "question": pair["question"],
                    "expected_time_seconds": expected,
                    "cited_time_seconds": cited,
                    "error_seconds": abs(cited - expected) if cited is not None else None,
                }
            )
        args.save_baseline.write_text(json.dumps(baseline, indent=2), encoding="utf-8")
        print(f"Saved baseline citations for {len(baseline)} questions to {args.save_baseline}.")
        return

    baseline_by_question = {}
    if args.baseline:
        baseline_rows = json.loads(args.baseline.read_text(encoding="utf-8"))
        baseline_by_question = {item["question"]: item for item in baseline_rows}

    before_errors = []
    after_errors = []
    print("Question | Before cited / error | Expected | After cited / error")
    for index, pair in enumerate(pairs):
        question = pair["question"]
        expected = float(pair["expected_time_seconds"])
        cited_after = _current_citation(
            question,
            args.lecture_id,
            args.user_id,
            expected,
            f"citation-eval-{index}",
        )
        after_error = abs(cited_after - expected) if cited_after is not None else None
        if after_error is not None:
            after_errors.append(after_error)

        before = baseline_by_question.get(question)
        if before:
            cited_before = before.get("cited_time_seconds")
            before_error = (
                abs(float(cited_before) - expected)
                if cited_before is not None
                else None
            )
            if before_error is not None:
                before_errors.append(before_error)
            before_label = (
                f"{rag.format_timestamp(cited_before)} / {before_error:.1f}s"
                if before_error is not None
                else "N/A"
            )
        else:
            before_label = "N/A"
        after_label = (
            f"{rag.format_timestamp(cited_after)} / {after_error:.1f}s"
            if after_error is not None
            else "N/A"
        )
        print(
            f"{question} | {before_label} | {rag.format_timestamp(expected)} | {after_label}"
        )

    if before_errors:
        print(f"Median before error: {statistics.median(before_errors):.1f}s")
    if after_errors:
        print(f"Median after error: {statistics.median(after_errors):.1f}s")


if __name__ == "__main__":
    main()
