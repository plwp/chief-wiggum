#!/usr/bin/env python3
"""
Synthesize feedback from multiple AI reviewers into a single actionable list.

Usage:
    python3 synthesize_reviews.py --manifest reviews/reviewer-manifest.json
    python3 synthesize_reviews.py review1.md review2.md review3.md

Each input file contains one AI's review output. The script produces a merged
report on stdout highlighting points of agreement, disagreement, and findings
unique to one reviewer.

**Pass `--manifest` wherever you can** (chief-wiggum#416). Counting the review
files that happen to exist cannot distinguish "three reviewers ran" from "four
were asked and one never answered", so without it the synthesis opened with a
confident `N reviews received` that silently described a narrowed quorum. The
role manifest `consult_ai.py --role` writes records who was EXPECTED and which
tier they were in, which is the only way the reconciler can be told a voice is
missing. With no manifest the header says the expected set is unknown rather
than implying completeness.

A missing OPTIONAL provider stays non-fatal — roles model that outcome
deliberately — but it is still reported, because under review lenses each
provider is scoped to findings nobody else is looking for.
"""

import argparse
import json
import sys
from pathlib import Path


def load_reviews(paths: list[str]) -> list[dict]:
    """Load review files and return list of {source, content}.

    A missing file is still skipped rather than fatal: `config/providers.json`
    distinguishes required from optional providers precisely so an optional
    one may fail without blocking the role, and that outcome is legitimate.
    What must not happen is the SYNTHESIS reporting the reduced set as though
    it were the whole picture — see `quorum_delta` and `synthesize`
    (chief-wiggum#416).
    """
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


def load_manifest(path: str | Path) -> dict:
    """Read the role manifest `consult_ai.py --role` writes beside the reviews.

    The manifest is what makes the quorum checkable: it records every provider
    the role EXPECTED, whether each was required, and what became of it. A
    synthesis that counts only the files it found can never notice a voice
    that was supposed to be there.
    """
    return json.loads(Path(path).read_text())


def quorum_delta(reviews: list[dict], manifest: dict | None) -> dict:
    """What the synthesis received, against what the role expected.

    Returns a four-state answer rather than a count. `unknown` is a real
    state: with no manifest there is no expectation to compare against, and
    printing a bare confident number in that case is the thing this exists to
    stop.
    """
    received = [r["source"] for r in reviews]
    if not manifest:
        return {
            "status": "unknown",
            "received": received,
            "received_n": len(reviews),
            "expected_n": None,
            "absent": [],
            "absent_required": [],
        }

    results = manifest.get("results") or []
    absent = [
        {
            "name": entry.get("name", "?"),
            "required": bool(entry.get("required")),
            "error": (entry.get("error") or "").strip().splitlines()[0]
            if entry.get("error") else "",
        }
        for entry in results
        if entry.get("status") != "ok"
    ]
    absent_required = [a["name"] for a in absent if a["required"]]
    return {
        "status": "complete" if not absent else (
            "degraded-required" if absent_required else "degraded-optional"),
        "received": received,
        "received_n": len(reviews),
        "expected_n": len(results),
        "absent": absent,
        "absent_required": absent_required,
        "role": manifest.get("role", ""),
    }


def _quorum_block(delta: dict) -> list[str]:
    """The header the reconciler reads before it reads a single finding."""
    if delta["status"] == "unknown":
        return [
            f"**{delta['received_n']} reviews received. The expected set is "
            "UNKNOWN** — no role manifest was supplied, so this synthesis "
            "cannot tell you whether a reviewer is missing. Treat coverage as "
            "unverified rather than complete.\n"
        ]

    expected, got = delta["expected_n"], delta["received_n"]
    if delta["status"] == "complete":
        return [f"**{got} of {expected} expected reviews received — quorum "
                f"complete.**\n"]

    lines = [f"**{got} of {expected} expected reviews received — QUORUM "
             f"INCOMPLETE.**\n"]
    if delta["absent_required"]:
        lines.append(
            "> A **required** provider is missing. The role treats it as "
            "required because the quorum is not considered sound without it, "
            "so the findings below are a NARROWED result, not a clean one. "
            "Say so in the synthesis rather than presenting it as complete.\n"
        )
    else:
        lines.append(
            "> Only optional providers are missing, which the role permits. "
            "Findings below are still narrower than a full run: under review "
            "lenses each provider is scoped to a concern nobody else covers, "
            "so an absent voice removes exactly the findings it was there to "
            "produce.\n"
        )
    lines.append("Absent:\n")
    for entry in delta["absent"]:
        tier = "required" if entry["required"] else "optional"
        reason = f" — {entry['error']}" if entry["error"] else ""
        lines.append(f"- `{entry['name']}` ({tier}){reason}")
    lines.append("")
    return lines


def synthesize(reviews: list[dict], delta: dict | None = None) -> str:
    """Produce a synthesis prompt that Claude can use to merge reviews."""
    if not reviews:
        return "No reviews to synthesize."

    if delta is None:
        delta = quorum_delta(reviews, None)

    parts = ["# Multi-AI Review Synthesis\n"]
    parts.extend(_quorum_block(delta))

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Synthesize multiple AI reviews into one actionable list.")
    parser.add_argument("reviews", nargs="*", help="review markdown files")
    parser.add_argument(
        "--manifest", default="",
        help="role manifest written by `consult_ai.py --role` (e.g."
             " reviewer-manifest.json). Supplies the EXPECTED provider set so"
             " the synthesis can report a missing voice instead of counting"
             " only the files it found. With no review paths given, the"
             " successful providers' paths are taken from it.")
    parser.add_argument(
        "--gate", action="store_true",
        help="exit 1 when a REQUIRED provider is absent (report-only by"
             " default, per docs/gate-rollout.md)")
    args = parser.parse_args(argv)

    manifest = None
    if args.manifest:
        try:
            manifest = load_manifest(args.manifest)
        except (OSError, ValueError) as exc:
            # A manifest that cannot be read is not the same as no manifest:
            # the caller asked for the quorum to be checked and it could not
            # be, which must not degrade quietly into "expected set unknown".
            print(f"Error: cannot read manifest {args.manifest}: {exc}",
                  file=sys.stderr)
            return 2

    paths = list(args.reviews)
    if not paths and manifest:
        paths = [entry["path"] for entry in (manifest.get("results") or [])
                 if entry.get("status") == "ok" and entry.get("path")]
    if not paths:
        parser.error("no review files given (pass paths, or --manifest with"
                     " at least one successful provider)")

    reviews = load_reviews(paths)
    delta = quorum_delta(reviews, manifest)
    print(synthesize(reviews, delta))

    if delta["absent_required"]:
        print(f"Quorum incomplete: required provider(s) absent: "
              f"{', '.join(delta['absent_required'])}", file=sys.stderr)
        if args.gate:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
