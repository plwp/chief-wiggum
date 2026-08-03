#!/usr/bin/env python3
"""Shared multi-model quorum classifier: intersection-discard / convergent-labelling.

Two chief-wiggum business-factory mechanisms share one primitive. Run generation
independently across the ``consult_ai`` quorum (codex, gemini, opus, claude-interactive),
then classify each candidate by how many providers proposed it *independently*.
Agreement identifies the shared high-prior region — exactly where a model's own priors
sit, and exactly where collisions (names) or obvious plays (strategy) live:

- **Names** (chief-wiggum#253): a convergent candidate is DISCARDED outright, not
  promoted — a name every competitor's generator also emits has no value once it's
  taken, and agreement means it probably already is.
- **Strategy options** (chief-wiggum#254): a convergent option is LABELLED, not
  discarded — unlike a name, an obvious strategy can still be the *correct* one. It is
  kept but flagged ``convergent``, and the caller must state a reason the bet wins a
  race every competitor's model can also see. Divergent, single-model options get
  first-class consideration rather than being averaged away.

``classify()`` is the single primitive both call sites use; ``discard_convergent()`` and
``label_convergent()`` are thin, semantically-named wrappers so a call site reads as what
it means rather than reaching into dicts directly.
"""

from __future__ import annotations


def classify(by_provider: dict[str, list[str]]) -> list[dict]:
    """Group candidate strings by provider provenance.

    ``by_provider`` maps a quorum provider name to its raw, independently-generated
    candidate list (names or strategy options — the classifier is content-agnostic).
    Matching is case/whitespace-normalized: ``"Wanderoo"`` and ``"wanderoo "`` count as
    the same candidate. Returns one entry per distinct normalized candidate, in
    first-seen order across ``by_provider``'s iteration order::

        {"name": normalized, "sources": sorted([...providers]), "convergent": bool}

    ``convergent`` is true when >=2 DISTINCT providers proposed the candidate
    independently — the shared high-prior region, by construction.
    """
    sources: dict[str, list[str]] = {}
    order: list[str] = []
    for provider, items in by_provider.items():
        for raw in items or []:
            norm = str(raw).strip().lower()
            if not norm:
                continue
            if norm not in sources:
                sources[norm] = []
                order.append(norm)
            if provider not in sources[norm]:
                sources[norm].append(provider)
    return [
        {"name": norm, "sources": sorted(sources[norm]), "convergent": len(sources[norm]) >= 2}
        for norm in order
    ]


def discard_convergent(entries: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split ``classify()`` output into ``(survivors, discarded)`` — the #253 name
    semantics: a convergent candidate is removed from consideration entirely."""
    survivors = [e for e in entries if not e["convergent"]]
    discarded = [e for e in entries if e["convergent"]]
    return survivors, discarded


def label_convergent(entries: list[dict]) -> list[dict]:
    """Identity pass over ``classify()`` output — the #254 strategy-option semantics:
    nothing is discarded, ``convergent`` is already the label callers read directly.
    Provided so a call site reads symmetrically with ``discard_convergent()``."""
    return list(entries)
