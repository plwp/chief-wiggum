#!/usr/bin/env python3
"""business_consultant.py — orchestrator for the /business-consultant skill.

The COST-MODEL DERIVER (chief-wiggum#122, rollout steps 1+2 only): mechanically
derives a product's cost shape, per-tier unit economics, break-even + gross
margin, and a pricing-MODEL-family recommendation from:

  - the target repo's ``docs/patterns/adopted.json`` (which patterns are
    adopted + the bound ``tiered-subscription`` tier/matrix caps),
  - a cost-inputs.json (templates/cost-inputs-schema.json) — either an
    operator-authoritative document, or (documented, loudly-caveated fallback)
    the stack's illustrative seed.

Pure arithmetic, deterministic given its inputs. Renders ``docs/pricing.md``
into the target repo (5 sections: cost shape / unit economics per tier /
break-even+margin / market-comparable floor [an explicit UNRESOLVED marker —
the step-3 live-lookup seam is NOT built here] / pricing-model fit).

Writing the default in-repo artifact also registers ``docs/pricing.md`` into
the target's ``docs/quality/ratchet.json`` ``protected_paths`` — the same
`apply_pattern.py` code path, idempotent — so the protection the skill prose
claims is real, not asserted (chief-wiggum#234).

Target resolution mirrors the other skills:
  - ``owner/repo``  -> resolved & cloned via scripts/repo.py
  - ``--repo PATH`` -> a direct local path
  - neither         -> the current git repo (git rev-parse --show-toplevel)

Usage:
    python3 scripts/business_consultant.py [owner/repo] [--repo PATH] \\
        [--cost-inputs PATH] [--stack ID] [--out PATH] \\
        [--price-field FIELD] [--typical-fraction F] [--marketplace] \\
        [--dry-run] [--format text|json]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from apply_pattern import RATCHET_REL, _merge_ratchet, _meta_path  # noqa: E402
from consultant import derive, inputs, render  # noqa: E402

PRICING_REL = "docs/pricing.md"


def register_protected(target: Path) -> list[str]:
    """Register docs/pricing.md in the target's ratchet protected_paths.

    Reuses apply_pattern.py's merge (never a second implementation): idempotent
    on re-runs, and a missing ratchet.json gets the same DEFAULT_PROTECTED stub
    apply_pattern writes. Returns the globs actually added ([] when already
    registered)."""
    content, added = _merge_ratchet(target, [PRICING_REL])
    if added:
        path = _meta_path(target, RATCHET_REL)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return added


def _current_repo_root() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True, timeout=5,
        )
        return out.stdout.strip() or None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None


def resolve_target(owner_repo: str | None, repo_path: str | None) -> str:
    """Resolve the target repo to a local absolute path."""
    if repo_path:
        p = Path(repo_path).expanduser().resolve()
        if not (p / ".git").exists():
            print(f"Error: {p} is not a git repository", file=sys.stderr)
            sys.exit(1)
        return str(p)
    if owner_repo:
        from repo import resolve_repo  # local import: only needed for owner/repo
        return str(resolve_repo(owner_repo))
    root = _current_repo_root()
    if not root:
        print("Error: not inside a git repo; pass owner/repo or --repo PATH", file=sys.stderr)
        sys.exit(1)
    return root


def _summarize_text(result: dict) -> str:
    lines = [
        f"business-consultant: analysis {result['analysis_date']} (stack={result['stack_id']}, "
        f"active_tier={result['cost_shape']['active_tier']})",
        f"  cost inputs: {result['cost_inputs_source']}"
        + (" [ILLUSTRATIVE SEED -- unverified]" if result["used_illustrative_seed"] else ""),
        f"  flat nut: ${result['cost_shape']['flat_nut']:.2f}/mo",
    ]
    if result["cost_shape"]["largest_uncapped_meter"]:
        m = result["cost_shape"]["largest_uncapped_meter"]
        lines.append(f"  largest uncapped meter: {m['id']} (${m['rate']}/{m['unit']})")
    if result["cost_shape"]["first_step_jump"]:
        j = result["cost_shape"]["first_step_jump"]
        lines.append(f"  first fixed step-jump: {j['from']} -> {j['to']} (+${j['monthly_usd']:.2f}/mo)")
    for e in result["economics"]:
        if e["worst_case_unbounded"]:
            worst = "UNBOUNDED (uncapped meter)"
            flag = "  ** UNBOUNDED WORST-CASE **"
        elif e["worst_case_indeterminate"]:
            worst = "INDETERMINATE (cap not declared / unparseable)"
            flag = "  ** INDETERMINATE WORST-CASE -- fix your cost inputs **"
        else:
            worst = f"${e['worst_case_cost']:.2f}"
            flag = "  ** UNDERWATER **" if e["underwater"] is True else ""
        lines.append(
            f"  tier {e['tier']}: price={e['price']} worst_case={worst} "
            f"typical=${e['typical_cost']:.2f}{flag}"
        )
    for b in result["breakeven"]:
        if b["unbounded"]:
            lines.append(f"  break-even {b['tier']}: unbounded (uncapped meter — no finite break-even)")
            continue
        if b["indeterminate"]:
            lines.append(f"  break-even {b['tier']}: indeterminate (cap not declared — no finite break-even)")
            continue
        be = b["breakeven_tenants"] if b["breakeven_tenants"] is not None else "never"
        lines.append(f"  break-even {b['tier']}: {be} tenant(s), margin {b['gross_margin_pct']}%")
    lines.append(f"  pricing-model fit: {result['pricing_fit']['model_family']} (shape={result['pricing_fit']['cost_shape']})")
    lines.append(f"  {render.AUTHORITY_LINE}")
    return "\n".join(lines)


def run(args: argparse.Namespace) -> tuple[dict, str, list[str]]:
    target = resolve_target(args.owner_repo, args.repo)
    result = derive.run(
        target_dir=target,
        cost_inputs_path=args.cost_inputs,
        stack_id=args.stack,
        price_field=args.price_field,
        typical_fraction=args.typical_fraction,
        marketplace=args.marketplace,
        now=args.now,
        base=inputs.ROOT,
    )
    md = render.render_pricing_md(result)

    out_path = Path(args.out) if args.out else Path(target) / PRICING_REL
    protected: list[str] = []
    if not args.dry_run:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(md)
        # Protect the in-repo artifact (#234). An --out override writes
        # elsewhere — the target then has no docs/pricing.md to protect.
        if out_path == Path(target) / PRICING_REL:
            protected = register_protected(Path(target))

    return result, str(out_path), protected


def main() -> int:
    parser = argparse.ArgumentParser(
        description="/business-consultant cost-model deriver: unit economics + pricing-model fit"
    )
    parser.add_argument("owner_repo", nargs="?", default=None, help="owner/repo to resolve+clone (optional)")
    parser.add_argument("--repo", default=None, help="direct local repo path")
    parser.add_argument("--cost-inputs", default=None, help="path to an operator cost-inputs.json (templates/cost-inputs-schema.json); falls back to the stack's illustrative seed")
    parser.add_argument("--stack", default=inputs.DEFAULT_STACK, help="stack profile id (patterns/stacks/<id>)")
    parser.add_argument("--out", default=None, help="output path for the rendered report (default: <target>/docs/pricing.md)")
    parser.add_argument("--price-field", default="price_monthly_usd", help="tiered-subscription matrix key holding each tier's price")
    parser.add_argument("--typical-fraction", type=float, default=0.3, help="assumed fraction of worst-case cap used for 'typical' cost (documented assumption)")
    parser.add_argument("--marketplace", action="store_true", help="declare a take-rate/marketplace revenue model (never inferred from meter shape)")
    parser.add_argument("--now", default=None, help="ISO analysis date override (testing)")
    parser.add_argument("--dry-run", action="store_true", help="derive + print without writing docs/pricing.md")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args()

    try:
        result, out_path, protected = run(args)
    except inputs.ConsultantInputError as exc:
        print(f"business-consultant: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        print(json.dumps({"result": result, "out": out_path, "dry_run": args.dry_run,
                          "protected_registered": protected}, indent=2))
    else:
        print(_summarize_text(result))
        verb = "would write" if args.dry_run else "wrote"
        print(f"  {verb} {out_path}")
        if protected:
            print(f"  registered protected path in {RATCHET_REL}: {', '.join(protected)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
