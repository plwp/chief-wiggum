#!/usr/bin/env python3
"""De-AI the copy: banned-construction lint + specificity floor (chief-wiggum#255).

Generated marketing copy sits in the modal region of "SaaS landing page voice" and
is now instantly recognisable as machine-written: em-dash triplets, antithesis
("it's not just X — it's Y"), tricolons of parallel short fragments, abstract
virtue nouns as headers, zero specific numbers, zero named humans. The fix is not
"write less like an AI" — it is to source voice from a real customer's own words
(a `voice-corpus`: verbatim quotes from feature-request threads, review streams,
interview transcripts, inbound emails, stored with citations in the bet's dir) and
draft against it. This script only surfaces the measurable tells; **voice is
human-judged, report-only always** (docs/gate-rollout.md) — the lint never blocks
by itself, and promotion to `--gate` follows the normal validation ramp.

Tells flagged:

- **em-dash density**: em-dashes per 100 words above a threshold (the "—" tic).
- **antithesis**: "not X — Y" / "isn't just X, it's Y" / "it's not X, it's Y".
- **tricolon**: three short (<=4-word), comma/semicolon-joined parallel fragments
  in one sentence — the "you review, you post, you ship" cadence.
- **abstract-virtue header**: a heading or short line built entirely from
  marketing-abstraction nouns/adjectives (seamless, effortless, world-class, ...)
  with no concrete noun or number.
- **specificity floor**: the share of substantive sentences carrying a number, a
  quoted source, or a capitalized named artifact — below the floor is a finding,
  not a failure; humans judge whether a low-specificity sentence is still honest
  and clear.

Usage:
    check_copy_voice.py copy.md
    check_copy_voice.py copy.md --format json
    check_copy_voice.py copy.md --gate     # exits 1 on findings (never wired anywhere yet)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

# Per-100-words em-dash rate above which the tic reads as a tell rather than
# occasional legitimate punctuation. Chosen loosely — a report-only heuristic,
# not a certified threshold; the design-derived flag applies (chief-wiggum#139
# allowance) until validated against a real corpus.
EM_DASH_RATE_THRESHOLD = 1.5

ANTITHESIS_PATTERNS = [
    re.compile(r"\bnot\s+[\w '\-]+?\s+—\s+[\w '\-]+", re.IGNORECASE),
    re.compile(r"\bisn['’]t\s+just\s+[\w '\-]+?,\s*it['’]s\s+[\w '\-]+", re.IGNORECASE),
    re.compile(r"\bit['’]s\s+not\s+[\w '\-]+?,\s*it['’]s\s+[\w '\-]+", re.IGNORECASE),
]

# A tricolon of PARALLEL, short (<=4-word) clauses joined by commas/semicolons —
# the "you review, you post, you ship" cadence. Heuristic: split a sentence on
# ,/; and flag when >=3 resulting clauses are each <=4 words.
_CLAUSE_SPLIT_RE = re.compile(r"[,;]")
TRICOLON_MAX_CLAUSE_WORDS = 4
TRICOLON_MIN_CLAUSES = 3

# Abstract-virtue vocabulary: marketing nouns/adjectives that describe nothing
# concrete about the product. A header/short line built ENTIRELY from these
# (plus stopwords/punctuation) with no digit and no other concrete word flags.
VIRTUE_WORDS = {
    "seamless", "effortless", "frictionless", "game-changing", "gamechanging",
    "revolutionary", "cutting-edge", "cuttingedge", "next-generation",
    "nextgeneration", "world-class", "worldclass", "best-in-class",
    "bestinclass", "turnkey", "holistic", "robust", "scalable", "intuitive",
    "elegant", "empowering", "transformative", "innovative", "powerful",
    "simple", "simplicity", "excellence", "clarity", "precision", "delight",
    "delightful", "magic", "magical",
}
_STOPWORDS = {
    "a", "an", "the", "for", "your", "our", "with", "and", "to", "of", "is",
    "that", "in", "on", "makes", "make", "made",
}

SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
WORD_RE = re.compile(r"[A-Za-z']+")
NUMBER_RE = re.compile(r"\d")
QUOTE_RE = re.compile(r"[\"“][^\"”]{3,}[\"”]")
# A capitalized token that is not sentence-initial — a plausible named artifact
# (a real feature name, a real integration, a real customer's product).
NAMED_ARTIFACT_RE = re.compile(r"(?<!^)(?<![.!?]\s)\b[A-Z][a-zA-Z]{2,}\b")

# Below this share of substantive sentences carrying a number/quote/named
# artifact, specificity is a finding — human-judged, not a hard failure.
SPECIFICITY_FLOOR_PCT = 30.0

ERROR = "finding"


@dataclass
class Finding:
    tell: str
    detail: str
    snippet: str

    def __str__(self) -> str:
        return f"  [{self.tell}] {self.detail}: {self.snippet!r}"


def words(text: str) -> list[str]:
    return WORD_RE.findall(text)


def sentences(text: str) -> list[str]:
    return [s.strip() for s in SENTENCE_SPLIT_RE.split(text.replace("\n", " ")) if s.strip()]


def em_dash_findings(text: str) -> list[Finding]:
    w = words(text)
    if not w:
        return []
    dashes = text.count("—")
    rate = dashes / len(w) * 100
    if rate > EM_DASH_RATE_THRESHOLD:
        return [Finding(
            "em-dash-density",
            f"{dashes} em-dash(es) in {len(w)} words ({rate:.1f}/100w, "
            f"threshold {EM_DASH_RATE_THRESHOLD}/100w)",
            text[:80],
        )]
    return []


def antithesis_findings(text: str) -> list[Finding]:
    out = []
    for pat in ANTITHESIS_PATTERNS:
        for m in pat.finditer(text):
            out.append(Finding("antithesis", "not-X-Y / isn't-just-X-it's-Y construction", m.group(0)))
    return out


def tricolon_findings(text: str) -> list[Finding]:
    out = []
    for s in sentences(text):
        clauses = [c.strip() for c in _CLAUSE_SPLIT_RE.split(s) if c.strip()]
        short = [c for c in clauses if len(words(c)) <= TRICOLON_MAX_CLAUSE_WORDS and words(c)]
        if len(short) >= TRICOLON_MIN_CLAUSES and len(short) == len(clauses):
            out.append(Finding("tricolon", f"{len(short)} short parallel clauses in one sentence", s[:100]))
    return out


def virtue_header_findings(text: str) -> list[Finding]:
    out = []
    for line in text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if not stripped or len(stripped) > 60:
            continue
        toks = [t.lower() for t in words(stripped)]
        if not toks:
            continue
        substantive = [t for t in toks if t not in _STOPWORDS]
        if not substantive:
            continue
        if NUMBER_RE.search(stripped):
            continue
        if all(t in VIRTUE_WORDS for t in substantive):
            out.append(Finding("abstract-virtue-header", "header built entirely from virtue nouns/adjectives", stripped))
    return out


def specificity_findings(text: str) -> list[Finding]:
    sents = [s for s in sentences(text) if len(words(s)) >= 4]  # substantive only
    if not sents:
        return []
    concrete = 0
    for s in sents:
        if NUMBER_RE.search(s) or QUOTE_RE.search(s) or NAMED_ARTIFACT_RE.search(s):
            concrete += 1
    pct = concrete / len(sents) * 100
    if pct < SPECIFICITY_FLOOR_PCT:
        return [Finding(
            "specificity-floor",
            f"{concrete}/{len(sents)} substantive sentences ({pct:.0f}%) carry a "
            f"number/quote/named artifact — below the {SPECIFICITY_FLOOR_PCT:.0f}% floor",
            "; ".join(sents[:2]),
        )]
    return []


def lint(text: str) -> list[Finding]:
    findings: list[Finding] = []
    findings += em_dash_findings(text)
    findings += antithesis_findings(text)
    findings += tricolon_findings(text)
    findings += virtue_header_findings(text)
    findings += specificity_findings(text)
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", type=Path, help="copy file to lint (.md/.txt/.html — read as plain text)")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    ap.add_argument("--gate", action="store_true",
                    help="exit 1 on findings (report-only by default — docs/gate-rollout.md; "
                         "NOT wired into any workflow yet — voice is human-judged)")
    args = ap.parse_args()

    if not args.path.is_file():
        sys.exit(f"check_copy_voice: file not found: {args.path}")
    text = args.path.read_text(errors="ignore")
    findings = lint(text)

    if args.format == "json":
        print(json.dumps([asdict(f) for f in findings], indent=2))
    else:
        if not findings:
            print(f"check_copy_voice: {args.path} — no tells found (report-only heuristic; "
                  "still read it aloud before shipping)")
        else:
            print(f"check_copy_voice: {args.path} — {len(findings)} finding(s) "
                  "(report-only; voice is human-judged)")
            for f in findings:
                print(f)

    return 1 if (findings and args.gate) else 0


if __name__ == "__main__":
    sys.exit(main())
