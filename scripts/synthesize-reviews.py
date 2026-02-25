#!/usr/bin/env python3
"""
Synthesize feedback from multiple AI reviewers into a single actionable list.

Usage:
    python3 synthesize-reviews.py review1.md review2.md review3.md

Each input file should contain one AI's review output. The script produces a
merged report on stdout highlighting:
  - Points of agreement (high confidence)
  - Points of disagreement (needs human decision)
  - Unique suggestions from individual reviewers
"""

import sys
from pathlib import Path


def load_reviews(paths: list[str]) -> list[dict]:
    """Load review files and return list of {source, content}."""
    reviews = []
    for p in paths:
        path = Path(p)
        if not path.exists():
            print(f"Warning: {p} not found, skipping", file=sys.stderr)
            continue
        reviews.append({
            "source": path.stem,
            "content": path.read_text().strip(),
        })
    return reviews


def synthesize(reviews: list[dict]) -> str:
    """Produce a synthesis prompt that Claude can use to merge reviews."""
    if not reviews:
        return "No reviews to synthesize."

    parts = ["# Multi-AI Review Synthesis\n"]
    parts.append(f"**{len(reviews)} reviews received.**\n")

    for i, r in enumerate(reviews, 1):
        parts.append(f"## Review {i}: {r['source']}\n")
        parts.append(r["content"])
        parts.append("")

    parts.append("---")
    parts.append("")
    parts.append("## Synthesis Instructions")
    parts.append("")
    parts.append("Merge the above reviews into a single actionable list:")
    parts.append("")
    parts.append("### Agreed (all reviewers)")
    parts.append("Items that 2+ reviewers flagged. These are high-confidence fixes.")
    parts.append("")
    parts.append("### Disputed")
    parts.append("Items where reviewers disagree. Present both sides for human decision.")
    parts.append("")
    parts.append("### Unique Suggestions")
    parts.append("Items raised by only one reviewer. Note which reviewer and why it may matter.")
    parts.append("")

    return "\n".join(parts)


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <review1.md> [review2.md] ...", file=sys.stderr)
        sys.exit(1)

    reviews = load_reviews(sys.argv[1:])
    print(synthesize(reviews))


if __name__ == "__main__":
    main()
