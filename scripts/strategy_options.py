#!/usr/bin/env python3
"""Strategy-option divergence: convergent-labelling + entropy-injection constraints.

chief-wiggum#254, mirroring #253's naming mechanism but applied to wedge/positioning/
differentiation options rather than names. Two independent mechanisms:

1. **Convergent-labelling** (decision 2): classify wedge/positioning options generated
   independently across the `consult_ai` quorum (codex, gemini, opus,
   claude-interactive). An option proposed by >=2 providers is the CONVERGED region —
   the obvious play, therefore the contested one — but unlike a name (#253) it is
   LABELLED, not discarded: an obvious strategy can still be correct. A convergent
   option requires a stated reason the bet wins a race every competitor's model can
   also see; a divergent (single-model) option gets first-class consideration rather
   than being averaged away.

2. **Entropy injection** (decision 3): forced-constraint prompting to sample the tails
   of the option distribution rather than its mode — a random segment/cost-structure/
   distribution-channel/buyer-inversion constraint, an adversarial reframe ("argue the
   opposite thesis"), or analogical seeding from a randomly-drawn historical episode
   in docs/business-factory.md §9.4's mined corpus of moat-collapse episodes.

This script does not call any AI provider itself — it classifies already-generated
options (read from a JSON file, the same shape `consult_ai.py --role` quorum output
would be reshaped into) and prints ready-to-use constraint prompts for the *next*
generation round. Report-only tooling; the pick and the stated reason stay human.

Usage:
    strategy_options.py --quorum-file options.json               # classify
    strategy_options.py --quorum-file options.json --format json
    strategy_options.py --constraint random                       # print one constraint prompt
    strategy_options.py --constraint historical-episode --seed 7  # reproducible draw
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import divergence  # noqa: E402  (shared quorum classifier — chief-wiggum#253/#254)

# The nine moat-collapse episodes mined for docs/business-factory.md §9.4 — used here
# as analogical-seeding entropy: "argue this bet's wedge the way <episode> attacked its
# incumbent." Kept in sync with §9.4's mined-episode list; update both together.
HISTORICAL_EPISODES = [
    "generics post-patent-cliff (Paragraph IV first-filer windows)",
    "PC clones vs IBM (modularity + commodity components)",
    "May Day brokerages + the zero-commission wave (Schwab vs commissioned brokers)",
    "budget airlines vs legacy hubs (Southwest vs hub-and-spoke; every hybrid clone died)",
    "open source & the strip-mining fight (profit re-pooling to operations/assurance)",
    "Craigslist unbundling of newspaper classifieds",
    "Zoom vs neglected, Cisco-owned WebEx (quality-of-first-touch as the wedge)",
    "Atlassian's no-sales-force, procurement-threshold pricing",
    "hard discounters / private label vs national brands (cost-structure fundamentalism)",
]

# Forced-constraint categories (decision 3) — a random draw from each forces option
# generation away from the obvious, high-prior answer.
CONSTRAINT_CATEGORIES = {
    "segment": "Generate wedges for a SPECIFIC, randomly-drawn buyer segment adjacent "
               "to (not identical to) the one already under consideration.",
    "cost-structure": "Generate wedges assuming a cost structure fundamentally "
                       "different from the obvious one (no sales force / no payroll / "
                       "usage-metered / one-person-operable).",
    "distribution-channel": "Generate wedges assuming distribution happens ONLY "
                             "through a randomly-drawn single channel (an existing "
                             "ecosystem marketplace, a professional-advisor channel, a "
                             "single community) — no other channel is available.",
    "buyer-inversion": "Generate wedges assuming the buyer is NOT the obvious one — "
                       "invert who signs the cheque (e.g. the end-user's employer, "
                       "their accountant, a regulator) and re-derive the wedge.",
    "adversarial-reframe": "Argue the OPPOSITE thesis as persuasively as possible: why "
                           "this bet is a bad idea, then extract whatever in that "
                           "argument survives as a real constraint on the wedge.",
    "historical-episode": "Argue this bet's wedge the way a randomly-drawn historical "
                          "moat-collapse episode attacked its incumbent (see the drawn "
                          "episode) — analogical seeding, not literal recreation.",
}


def draw_constraint(category: str, rng: random.Random) -> dict:
    """Return a ready-to-use constraint prompt. `category` may be 'random' to draw one
    of CONSTRAINT_CATEGORIES uniformly."""
    if category == "random":
        category = rng.choice(list(CONSTRAINT_CATEGORIES))
    if category not in CONSTRAINT_CATEGORIES:
        raise SystemExit(
            f"strategy_options: unknown constraint {category!r}; choose from "
            f"{list(CONSTRAINT_CATEGORIES)} or 'random'"
        )
    out = {"category": category, "prompt": CONSTRAINT_CATEGORIES[category]}
    if category == "historical-episode":
        episode = rng.choice(HISTORICAL_EPISODES)
        out["episode"] = episode
        out["prompt"] = f"{out['prompt']} Drawn episode: {episode}."
    return out


def classify_options(by_provider: dict[str, list[str]]) -> list[dict]:
    """Convergent-labelling (decision 2) — a thin, semantically-named wrapper over the
    shared classifier: nothing is discarded, `convergent` is the label."""
    return divergence.label_convergent(divergence.classify(by_provider))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quorum-file", type=Path, default=None, metavar="JSON",
                    help="a {provider: [option, ...]} map of independently-generated "
                         "wedge/positioning options to classify")
    ap.add_argument("--constraint", default=None,
                    choices=[*CONSTRAINT_CATEGORIES, "random"],
                    help="print one entropy-injection constraint prompt instead of "
                         "classifying (decision 3)")
    ap.add_argument("--seed", type=int, default=None, help="reproducible constraint draw")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args()

    if args.constraint:
        rng = random.Random(args.seed)
        constraint = draw_constraint(args.constraint, rng)
        if args.format == "json":
            print(json.dumps(constraint, indent=2))
        else:
            print(f"constraint: {constraint['category']}")
            print(constraint["prompt"])
        return 0

    if not args.quorum_file:
        sys.exit("strategy_options: pass --quorum-file or --constraint")
    try:
        by_provider = json.loads(args.quorum_file.read_text())
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"strategy_options: cannot read --quorum-file {args.quorum_file}: {e}")
    if not isinstance(by_provider, dict):
        sys.exit("strategy_options: --quorum-file must contain a JSON object {provider: [options]}")

    entries = classify_options(by_provider)
    if args.format == "json":
        print(json.dumps({"options": entries}, indent=2))
        return 0

    n_conv = sum(1 for e in entries if e["convergent"])
    print(f"{len(entries)} option(s), {n_conv} convergent\n")
    for e in entries:
        tag = " [CONVERGENT — state why this wins a race every competitor's model can see]" if e["convergent"] else ""
        print(f"  {e['name']}  <{', '.join(e['sources'])}>{tag}")
    if n_conv:
        print(
            "\n  Convergent options are NOT discarded (chief-wiggum#254) — an obvious "
            "strategy can still be right. Each one above needs a stated reason before "
            "it is treated as a real option, not just a default."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
