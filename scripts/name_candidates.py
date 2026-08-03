#!/usr/bin/env python3
"""Mechanical name-candidate generator with an external entropy source.

Names sampled from a model's priors collide in-category at a high rate — measured
2/2 on chief-wiggum's own first attempts (chief-wiggum#253). Both failures shared a
cause: the candidate came from the same distribution every competitor's model draws
from. This generator replaces that distribution with an external corpus plus a seeded
PRNG, so the output is reproducible, auditable, and *not* a resampling of priors.

Two entropy properties are distinct and both matter:

  semantic entropy      escaping the model's distribution — a random dictionary word does this
  availability entropy  escaping the squatters — a random dictionary word does NOT do this,
                        because the dictionary is finite and exhaustively registered

Hence coinage strategies (blend/mutate/compound) rather than bare dictionary picks, and
hence `--check`: availability is filtered mechanically BEFORE any human looks, so operator
attention is never spent on candidates that were never available.

Usage:
    name_candidates.py --count 40 --register neutral
    name_candidates.py --count 20 --check --tlds com,app,io
    name_candidates.py --seed 1234 --count 10        # reproducible
"""

from __future__ import annotations

import argparse
import json
import random
import re
import secrets
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import divergence  # noqa: E402  (shared quorum classifier — chief-wiggum#253/#254)

WORDLIST = Path("/usr/share/dict/words")

# Modifiers carry TWO tags: a register (how the name sounds) and a convergence risk
# (how many competitors' generators are emitting the same shape right now). These are
# not the same axis — `-pilot` and `-mind` sound current and are simultaneously the most
# saturated suffixes in the market, which is exactly the trap chief-wiggum#254 describes.
# CONVERGED sets are included because sounding like a product has real utility, but the
# tool says so rather than letting the caller ship an AI-default name unknowingly.
CONVERGED = {"conventional", "ai-era"}

PREFIXES = {
    "conventional": ["get", "try", "use", "go", "my", "hey"],          # the .io-era default
    "neutral": ["re", "co", "up", "on", "in", "over"],
    "offbeat": ["un", "mid", "half", "counter", "under"],
    "ai-era": ["neo", "auto", "self"],                                  # saturated 2025-26
    "emerging": ["proto", "para", "trans", "quasi", "post", "sub"],
    "domain": ["ledger", "tally", "audit", "clear"],                    # finance/trust flavour
}
SUFFIXES = {
    "conventional": ["ly", "ify", "io", "hub", "stack", "base", "flow", "sync"],
    "neutral": ["works", "desk", "deck", "kit", "lab", "port", "line", "mark"],
    "offbeat": ["ery", "age", "wright", "smith", "wick", "hollow", "reach", "wise"],
    # Peak saturation. NOTE: "copilot" is deliberately absent — it is Microsoft's mark,
    # and a generator that emits it is manufacturing infringement, not names.
    "ai-era": ["ai", "mind", "agent", "brain"],
    # Emerging registers, mid-2026: craft/foundry, geology/landform, infrastructure,
    # nautical, and latinate endings — the shapes displacing the .io-era vocabulary.
    "emerging": [
        "forge", "foundry", "mill", "anvil", "loom", "kiln", "bench",   # craft
        "ridge", "mesa", "quarry", "flint", "basalt", "cairn", "delta", "cove",  # landform
        "grid", "mesh", "node", "rail", "relay", "conduit", "edge",     # infrastructure
        "helm", "keel", "berth", "tide", "lock", "harbour",             # nautical
        "ora", "ium", "ia", "us", "ex",                                 # latinate
    ],
    # Domain-flavoured: for finance/accounting/trust products, where the vocabulary of
    # the register itself is the differentiator (see the ledger-editorial design direction).
    "domain": ["ledger", "tally", "reckon", "vault", "stamp", "seal", "quill",
               "abacus", "balance", "clearing", "escrow", "docket"],
}

VOWELS = set("aeiouy")

# Coinage strategies (blend/compound especially) can produce a registered trademark by
# accident — "copilot" was in an early draft of the modifier table until an operator
# caught it. Any candidate CONTAINING one of these substrings is dropped. This is a floor,
# not a trademark search: it catches the embarrassing cases mechanically and does not
# substitute for the register check in the naming protocol (chief-wiggum#249).
BRAND_TOKENS = {
    "copilot", "chatgpt", "openai", "anthropic", "claude", "gemini", "github", "gitlab",
    "google", "microsoft", "amazon", "apple", "meta", "nvidia", "oracle", "adobe",
    "salesforce", "slack", "notion", "figma", "stripe", "shopify", "atlassian", "jira",
    "confluence", "xero", "myob", "quickbooks", "intuit", "deputy", "linkedin", "twitter",
    "netflix", "spotify", "uber", "airbnb", "tesla", "paypal", "visa", "mastercard",
}


def infringing(name: str) -> str | None:
    """Return the offending brand token, or None."""
    return next((t for t in BRAND_TOKENS if t in name), None)


def load_pool(min_len: int = 4, max_len: int = 9, wordlist: Path = WORDLIST) -> list[str]:
    if not wordlist.exists():
        sys.exit(f"name_candidates: no word list at {wordlist} — supply one with --wordlist")
    words = (w.strip().lower() for w in wordlist.read_text(errors="ignore").splitlines())
    return [
        w for w in words
        if min_len <= len(w) <= max_len and re.fullmatch(r"[a-z]+", w)
        and not w.endswith(("ing", "ness", "tion", "ment", "ism", "ous"))
    ]


def pronounceable(s: str) -> bool:
    """Cheap phonotactic filter: sayable, not a keyboard mash."""
    if not (4 <= len(s) <= 12) or not any(c in VOWELS for c in s):
        return False
    if re.search(r"[bcdfghjklmnpqrstvwxz]{4}", s):      # consonant pile-up
        return False
    if re.search(r"(.)\1\1", s):                        # tripled letter
        return False
    if re.search(r"[aeiou]{4}", s):                     # vowel pile-up
        return False
    return True


def blend(a: str, b: str, rng: random.Random) -> str | None:
    """Portmanteau at a shared letter — the most reliable coinage strategy."""
    shared = [(i, j) for i, ca in enumerate(a[1:-1], 1)
              for j, cb in enumerate(b[1:-1], 1) if ca == cb]
    if not shared:
        return None
    i, j = rng.choice(shared)
    return a[:i] + b[j:]


def mutate(w: str, rng: random.Random) -> str:
    """Vowel substitution — turns a squatted real word into an unsquatted coinage."""
    idx = [i for i, c in enumerate(w) if c in VOWELS and i > 0]
    if not idx:
        return w
    i = rng.choice(idx)
    return w[:i] + rng.choice("aeiou".replace(w[i], "") or "a") + w[i + 1:]


def truncate(w: str, rng: random.Random) -> str:
    return w[: rng.randint(3, max(3, len(w) - 1))]


def generate(pool: list[str], rng: random.Random, registers: list[str], count: int) -> list[dict]:
    prefixes = [(p, r) for r in registers for p in PREFIXES[r]]
    suffixes = [(s, r) for r in registers for s in SUFFIXES[r]]
    dictionary = set(pool)
    out: dict[str, dict] = {}
    strategies = ("blend", "suffix", "prefix", "trunc-suffix", "compound", "mutate")

    attempts = 0
    while len(out) < count and attempts < count * 400:
        attempts += 1
        strat = rng.choice(strategies)
        a, b = rng.choice(pool), rng.choice(pool)

        mod_register = None
        if strat == "blend":
            cand, seed = blend(a, b, rng), f"{a}+{b}"
        elif strat == "suffix":
            s, mod_register = rng.choice(suffixes)
            cand, seed = truncate(a, rng) + s, f"{a}+-{s}"
        elif strat == "prefix":
            p, mod_register = rng.choice(prefixes)
            cand, seed = p + a, f"{p}-+{a}"
        elif strat == "trunc-suffix":
            s, mod_register = rng.choice(suffixes)
            cand, seed = a[:4] + s, f"{a}[:4]+-{s}"
        elif strat == "compound":
            cand, seed = truncate(a, rng) + truncate(b, rng), f"{a}+{b}"
        else:
            cand, seed = mutate(a, rng), f"{a}~"

        if not cand or not pronounceable(cand) or cand in out:
            continue
        # Coinage requirement: a real dictionary word has semantic entropy but no
        # availability entropy — its .com is essentially always registered.
        if cand in dictionary:
            continue
        if infringing(cand):
            continue
        out[cand] = {"name": cand, "strategy": strat, "seed_words": seed,
                     "register": mod_register or "corpus",
                     "converged": mod_register in CONVERGED}
    return list(out.values())


def quorum_classify(by_provider: dict[str, list[str]]) -> tuple[list[dict], list[dict]]:
    """Multi-model divergence with INTERSECTION-DISCARD (chief-wiggum#253 decision 2).

    Run name generation independently across the `consult_ai` quorum (codex, gemini,
    opus, claude-interactive) and pass each provider's raw output list here, keyed by
    provider name. A name proposed independently by >=2 providers is CONVERGENT — it
    identifies the high-prior region every competitor's model shares, which is exactly
    where collisions live — and is DISCARDED here, not merely flagged or averaged in.
    This inverts the usual quorum semantics (agreement usually raises confidence; here
    agreement is the failure signal) and that inversion is the point.

    Returns ``(survivors, discarded)``. Each survivor carries provenance (the single
    provider that proposed it) so a shortlist can prove it wasn't just resampled
    priors; each discarded entry carries every provider that converged on it. Matching
    is case/whitespace-normalized so "Wanderoo" and "wanderoo " count as the same name.
    Contrast chief-wiggum#254's strategy-option variant, which LABELS convergent
    options rather than discarding them — a name has no value once it collides, but an
    obvious strategy can still be the right one. Both share one classifier primitive,
    ``scripts/divergence.py`` — this function is the name-specific (discard) wrapper.
    """
    entries = divergence.classify(by_provider)
    survivors_raw, discarded_raw = divergence.discard_convergent(entries)
    survivors = [{
        "name": e["name"], "strategy": "quorum",
        "seed_words": f"proposed-by:{e['sources'][0]}",
        "register": "quorum", "converged": False,
        "sources": e["sources"],
    } for e in survivors_raw]
    discarded = [{"name": e["name"], "sources": e["sources"]} for e in discarded_raw]
    return survivors, discarded


def rdap_available(domain: str, timeout: int = 12) -> bool | None:
    """True=available, False=registered, None=unresolved. rdap.org 302s to the
    authoritative registry, so redirects MUST be followed; a non-200/404 is never
    reported as available."""
    try:
        with urllib.request.urlopen(f"https://rdap.org/domain/{domain}", timeout=timeout) as r:
            return False if r.status == 200 else None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return True
        return False if e.code == 200 else None
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--count", type=int, default=30)
    ap.add_argument("--seed", type=int, help="reproducible run (default: system CSPRNG)")
    ap.add_argument("--register", default="emerging,neutral,offbeat",
                    help="comma-separated modifier registers, or 'all'. Choices: "
                         + ", ".join(PREFIXES) + ". Default excludes the converged sets "
                         "(conventional, ai-era) — pass them explicitly if you want them.")
    ap.add_argument("--wordlist", type=Path, default=WORDLIST,
                    help=f"corpus word list (default {WORDLIST})")
    ap.add_argument("--quorum-file", type=Path, default=None, metavar="JSON",
                    help="run the multi-model intersection-discard path instead of corpus "
                         "generation (chief-wiggum#253 decision 2): a JSON object "
                         "{provider: [name, ...]} with each consult_ai quorum provider's "
                         "raw name proposals; names proposed by >=2 providers are "
                         "discarded as convergent (see quorum_classify())")
    ap.add_argument("--check", action="store_true", help="RDAP-filter to available names only")
    ap.add_argument("--tlds", default="com", help="comma-separated, checked with --check")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args()

    discarded: list[dict] = []
    if args.quorum_file is not None:
        try:
            by_provider = json.loads(args.quorum_file.read_text())
        except (OSError, json.JSONDecodeError) as e:
            sys.exit(f"name_candidates: cannot read --quorum-file {args.quorum_file}: {e}")
        if not isinstance(by_provider, dict):
            sys.exit("name_candidates: --quorum-file must contain a JSON object {provider: [names]}")
        cands, discarded = quorum_classify(by_provider)
        cands = [c for c in cands if not infringing(c["name"])]
        seed, pool, registers = None, [], ["quorum"]
    else:
        registers = list(PREFIXES) if args.register == "all" else \
            [r.strip() for r in args.register.split(",") if r.strip()]
        unknown = [r for r in registers if r not in PREFIXES]
        if unknown:
            sys.exit(f"name_candidates: unknown register(s) {unknown}; choose from {list(PREFIXES)}")
        seed = args.seed if args.seed is not None else secrets.randbits(32)
        rng = random.Random(seed)
        pool = load_pool(wordlist=args.wordlist)
        cands = generate(pool, rng, registers, args.count)

    tlds = [t.strip().lstrip(".") for t in args.tlds.split(",") if t.strip()]
    if args.check:
        kept = []
        for c in cands:
            avail = {t: rdap_available(f"{c['name']}.{t}") for t in tlds}
            c["availability"] = {t: ("available" if v is True else
                                     "registered" if v is False else "unresolved")
                                 for t, v in avail.items()}
            if any(v is True for v in avail.values()):
                kept.append(c)
        cands = kept

    if args.format == "json":
        print(json.dumps({
            "seed": seed, "corpus": str(args.wordlist) if args.quorum_file is None else None,
            "quorum_file": str(args.quorum_file) if args.quorum_file else None,
            "pool_size": len(pool), "registers": registers, "candidates": cands,
            "discarded_convergent": discarded,
        }, indent=2))
        return 0

    if args.quorum_file is not None:
        print(f"quorum-file={args.quorum_file}  providers={sorted({p for d in discarded for p in d['sources']} | {s for c in cands for s in c['sources']})}")
    else:
        print(f"seed={seed}  corpus={args.wordlist} ({len(pool)} words)  registers={','.join(registers)}")
    print(f"{len(cands)} candidate(s)" + ("  [available only]" if args.check else "") + "\n")
    for c in cands:
        flag = " ⚠converged" if c["converged"] else ""
        line = f"  {c['name']:<16} {c['strategy']:<13} {c['register']:<13} < {c['seed_words']}{flag}"
        if "availability" in c:
            line += "   " + " ".join(f"{t}:{v}" for t, v in c["availability"].items())
        print(line)
    n_conv = sum(1 for c in cands if c["converged"])
    if n_conv:
        print(f"\n  ⚠ {n_conv} candidate(s) use a CONVERGED modifier set "
              "(conventional/ai-era) — the shape every competitor's generator is also "
              "emitting right now. Prefer the unflagged ones (chief-wiggum#254).")
    if discarded:
        print(f"\n  {len(discarded)} name(s) DISCARDED as quorum-convergent (proposed by "
              ">=2 providers independently) — agreement identifies the high-prior region "
              "where collisions live (chief-wiggum#253):")
        for d in discarded:
            print(f"    {d['name']:<16} proposed by {', '.join(d['sources'])}")
    if not args.check:
        print("\n  (no availability check — rerun with --check before showing a human)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
