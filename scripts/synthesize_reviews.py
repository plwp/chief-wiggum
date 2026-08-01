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
    parts.append(
        "If these reviewers were run under **review lenses** (chief-wiggum#163 — "
        "config/providers.json `role.lenses`, charters in config/lenses.json), each "
        "was deliberately scoped to a different concern (e.g. one refutes soundness, "
        "one checks completeness, one prices adoption cost) over the SAME diff. "
        "Expect disjoint findings, not convergence — a lensed quorum working correctly "
        "looks like three different top findings, not three reviewers agreeing."
    )
    parts.append("")
    parts.append(
        "**Combine by union, then cross-verify only contested items — do not "
        "majority-vote.** A finding raised by exactly one reviewer is not weaker for "
        "being unique; under lenses it is often the point (the reviewer scoped to look "
        "for that class of problem is the one who should have found it). Reserve "
        "cross-verification for cases where two reviewers make CONTRADICTORY claims "
        "about the same fact — not merely where one mentions something the other "
        "didn't."
    )
    parts.append("")
    parts.append("Use a bug-first standard. Ignore nits, praise, and generic style commentary unless they point to a real defect.")
    parts.append("")
    parts.append("### High Confidence")
    parts.append("Every concrete, verifiable finding — whether raised by one reviewer or several. A single lensed reviewer's finding is retained on the same footing as one two reviewers happened to converge on.")
    parts.append("")
    parts.append("### Needs Verification")
    parts.append("Plausible issues that are worth testing locally before applying a fix.")
    parts.append("")
    parts.append("### Disputed / Low Confidence")
    parts.append("Findings that directly CONTRADICT another reviewer on the same fact, or speculative concerns with no concrete failure scenario. Being unique to one reviewer is NOT, by itself, a reason to downgrade a finding into this bucket.")
    parts.append("")
    parts.append("For each retained item, include file references if available and explain the likely failure mode briefly.")
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
