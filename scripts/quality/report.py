#!/usr/bin/env python3
"""report.py — consolidate engine JSON into a combined readout, charts, markdown.

Takes the per-engine dicts produced by the churn/complexity/trend/survival/
process/duplication engines and:
  - builds a flat ``combined.json`` summary (weighted-aggregate complexity across
    languages, churn/attribution signals, process signals),
  - renders PNG charts (trend + AI-slop signal bars) if matplotlib is present,
  - emits a literature-grounded markdown report with honest caveats.

matplotlib is optional: if absent, charts are skipped and the markdown notes it.

As a module:
    from quality import report
    combined = report.build_combined(engines)
    charts = report.render_charts(engines, out_dir)      # [] if matplotlib absent
    md = report.render_markdown(engines, combined, charts)
"""

from __future__ import annotations

import json
import os


def _weighted_complexity(battery: dict) -> dict:
    """Aggregate cyclomatic/cognitive across languages, weighted by function count."""
    tot_fn = 0
    ccn_sum = 0.0
    ccn_max = 0
    gt10_fn = 0.0
    len60_fn = 0.0
    cog_max = 0
    for _lang, e in (battery.get("languages") or {}).items():
        cc = e.get("cyclomatic_src")
        if cc and cc.get("functions"):
            f = cc["functions"]
            tot_fn += f
            ccn_sum += cc["ccn_mean"] * f
            ccn_max = max(ccn_max, cc["ccn_max"])
            gt10_fn += cc["pct_ccn_gt10"] / 100 * f
            len60_fn += cc["pct_len_gt60"] / 100 * f
        cg = e.get("cognitive_src")
        if cg and cg.get("cognitive_max"):
            cog_max = max(cog_max, cg["cognitive_max"])
    return {
        "functions": tot_fn,
        "ccn_mean": round(ccn_sum / tot_fn, 2) if tot_fn else None,
        "ccn_max": ccn_max,
        "pct_ccn_gt10": round(100 * gt10_fn / tot_fn, 1) if tot_fn else None,
        "pct_len_gt60": round(100 * len60_fn / tot_fn, 1) if tot_fn else None,
        "cog_max": cog_max,
    }


def build_combined(engines: dict) -> dict:
    """Flatten the engine outputs into a single summary row + raw pass-through."""
    churn = engines.get("churn") or {}
    battery = engines.get("complexity") or {}
    process = engines.get("process") or {}
    survival = engines.get("survival") or {}
    duplication = engines.get("duplication") or {}
    trend = engines.get("trend") or {}

    comp = _weighted_complexity(battery)
    scale = churn.get("scale") or {}
    ch = churn.get("churn") or {}
    attr = churn.get("attribution") or {}
    commits = scale.get("commits") or 0
    added = ch.get("added") or 0
    deleted = ch.get("deleted") or 0

    summary = {
        "repo": churn.get("repo") or battery.get("repo"),
        "first": scale.get("first"), "last": scale.get("last"),
        "span_days": scale.get("span_days"),
        "commits": commits,
        "src_loc": battery.get("src_loc_total"),
        "test_loc": battery.get("test_loc_total"),
        "test_ratio": battery.get("test_ratio"),
        **comp,
        "added": added, "deleted": deleted,
        "churn_per_commit": round((added + deleted) / commits) if commits else 0,
        "rework_ratio": round(deleted / added, 3) if added else 0,  # del:add
        "conv_pct": attr.get("conventional_pct"),
        "ticket_pct": attr.get("ticket_ref_pct"),
        "pr_pct": attr.get("pr_merge_pct"),
        "change_entropy": process.get("change_entropy_normalized"),
        "bus_factor_50pct": (process.get("ownership") or {}).get("bus_factor_50pct"),
        "fix_commit_pct": (process.get("defect_proxy") or {}).get("fix_commit_pct"),
        "trend_points": trend.get("points"),
    }
    surv_14 = None
    if not survival.get("skipped"):
        surv_14 = ((survival.get("survival_by_age_days") or {}).get(14)
                   or (survival.get("survival_by_age_days") or {}).get("14") or {})
        summary["survival_14d_pct"] = surv_14.get("survival_pct")
        summary["half_life_days"] = survival.get("half_life_days")
    if not duplication.get("skipped"):
        summary["duplication_pct_tokens"] = duplication.get("duplication_pct_tokens")

    return {"summary": summary, "engines": engines}


def render_charts(engines: dict, out_dir: str) -> list[str]:
    """Render trend + AI-slop charts. Returns paths written; [] if matplotlib absent."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return []

    written: list[str] = []
    trend = engines.get("trend") or {}
    series = [p for p in (trend.get("series") or []) if p.get("src_loc")]

    plt.rcParams.update({
        "font.size": 9, "axes.grid": True, "grid.alpha": 0.25,
        "figure.facecolor": "white", "axes.axisbelow": True,
    })

    if len(series) >= 2:
        fig, axes = plt.subplots(2, 2, figsize=(14, 9))
        fig.suptitle("Code-quality metrics over history", fontsize=13, fontweight="bold")
        dates = [p["date"] for p in series]

        ax = axes[0][0]
        ax.plot(dates, [p.get("test_ratio") for p in series], marker="o", ms=3, color="#2ca02c")
        ax.axhline(1.0, ls="--", c="k", lw=0.8, alpha=0.5)
        ax.set_title("Test-to-code ratio (higher = more test discipline)")
        ax.tick_params(axis="x", rotation=40, labelsize=6)

        ax = axes[0][1]
        ax.plot(dates, [p.get("pct_ccn_gt10") for p in series], marker="o", ms=3, color="#d62728")
        ax.set_title("% functions with cyclomatic >10 (McCabe risk)")
        ax.tick_params(axis="x", rotation=40, labelsize=6)

        ax = axes[1][0]
        locs = [p.get("src_loc") for p in series]
        ccns = [p.get("ccn_mean") for p in series]
        ax.plot(locs, ccns, marker="o", ms=3, color="#1f77b4")
        ax.set_title("Mean CCN vs source LOC\n(flat = complexity NOT tracking size — good)")
        ax.set_xlabel("source LOC")
        ax.set_ylabel("mean CCN / function")

        ax = axes[1][1]
        ax.plot(dates, [p.get("src_loc") for p in series], marker="o", ms=3, color="#9467bd")
        ax.set_title("Source LOC over history (scale)")
        ax.tick_params(axis="x", rotation=40, labelsize=6)

        fig.tight_layout(rect=[0, 0.01, 1, 0.95])
        p1 = os.path.join(out_dir, "trends.png")
        fig.savefig(p1, dpi=130)
        plt.close(fig)
        written.append(p1)

    # AI-slop signal bars: 14-day survival + duplication vs GitClear bands
    survival = engines.get("survival") or {}
    duplication = engines.get("duplication") or {}
    surv_val = None
    if not survival.get("skipped"):
        s14 = ((survival.get("survival_by_age_days") or {}).get(14)
               or (survival.get("survival_by_age_days") or {}).get("14") or {})
        surv_val = s14.get("survival_pct")
    dup_val = None if duplication.get("skipped") else duplication.get("duplication_pct_tokens")

    if surv_val is not None or dup_val is not None:
        fig, axes = plt.subplots(1, 2, figsize=(13, 4.2))
        fig.suptitle("AI-slop signals vs GitClear baselines", fontsize=12, fontweight="bold")
        axA, axB = axes
        if surv_val is not None:
            axA.barh(["this repo"], [surv_val], color="#1f77b4")
            axA.axvline(96.9, ls="--", c="#555", lw=1.2)
            axA.axvline(94.3, ls="--", c="#c00", lw=1.2)
            axA.set_xlim(90, 100.5)
            axA.set_title("14-day code survival (higher = less churn)\npre-AI 96.9% / AI 94.3%")
            axA.set_xlabel("% lines surviving 14 days")
        else:
            axA.text(0.5, 0.5, "survival: skipped", ha="center", va="center")
            axA.axis("off")
        if dup_val is not None:
            axB.barh(["this repo"], [dup_val], color="#d62728")
            axB.axvline(8.3, ls="--", c="#555", lw=1.2)
            axB.axvline(12.3, ls="--", c="#c00", lw=1.2)
            axB.set_title("Production duplication (lower = better)\npre-AI 8.3% / AI 12.3%")
            axB.set_xlabel("% duplicated tokens")
        else:
            axB.text(0.5, 0.5, "duplication: skipped", ha="center", va="center")
            axB.axis("off")
        fig.tight_layout(rect=[0, 0.01, 1, 0.9])
        p2 = os.path.join(out_dir, "ai_slop_signals.png")
        fig.savefig(p2, dpi=130)
        plt.close(fig)
        written.append(p2)

    return written


def _fmt(v, suffix: str = "") -> str:
    return "n/a" if v is None else f"{v}{suffix}"


def render_markdown(engines: dict, combined: dict, charts: list[str]) -> str:
    """Emit a literature-grounded markdown report with honest caveats."""
    s = combined["summary"]
    churn = engines.get("churn") or {}
    battery = engines.get("complexity") or {}
    process = engines.get("process") or {}
    survival = engines.get("survival") or {}
    duplication = engines.get("duplication") or {}

    lines: list[str] = []
    lines.append(f"# Code-Quality Metrics — {s.get('repo') or 'repo'}")
    lines.append("")
    lines.append(
        "Literature-grounded readout. Every metric is reported with its caveat; the "
        "load-bearing truth is that **most product metrics are size in disguise** — the "
        "signals that are *not* just SLOC (relative churn, duplication, survival) are the "
        "trustworthy ones. See `docs/quality-metrics.md` for citations."
    )
    lines.append("")

    # --- scale & discipline ---
    lines.append("## Scale & process discipline")
    lines.append("")
    lines.append(f"- Commits: **{_fmt(s.get('commits'))}** over {_fmt(s.get('span_days'), ' days')} "
                 f"({_fmt(s.get('first'))} → {_fmt(s.get('last'))})")
    lines.append(f"- Source LOC: **{_fmt(s.get('src_loc'))}**, test LOC: {_fmt(s.get('test_loc'))}, "
                 f"test:code ratio **{_fmt(s.get('test_ratio'))}**")
    lines.append(f"- Conventional-commit %: {_fmt(s.get('conv_pct'), '%')}, "
                 f"ticket-ref %: {_fmt(s.get('ticket_pct'), '%')}, "
                 f"PR-merge %: {_fmt(s.get('pr_pct'), '%')}")
    lines.append("")

    # --- churn (strongest signal) ---
    lines.append("## Churn (strongest, best-replicated signal — Nagappan & Ball 2005)")
    lines.append("")
    lines.append(f"- Added: {_fmt(s.get('added'))}, deleted: {_fmt(s.get('deleted'))}, "
                 f"**rework ratio (del:add): {_fmt(s.get('rework_ratio'))}** "
                 f"(lower = less churn/AI-slop)")
    lines.append(f"- Churn per commit: {_fmt(s.get('churn_per_commit'))}")
    lines.append("- Caveat: *relative/normalised* churn is the predictor; absolute churn is weak. "
                 "This is the metric where AI-generated code shows the clearest degradation.")
    hotspots = churn.get("hotspots") or []
    if hotspots:
        lines.append("")
        lines.append("Top churn hotspots (file — churn / commits):")
        for h in hotspots[:8]:
            lines.append(f"  - `{h['file']}` — {h['churn']} / {h['commits']}")
    lines.append("")

    # --- complexity ---
    lines.append("## Complexity (product metrics — read ALONGSIDE size)")
    lines.append("")
    if battery.get("skipped"):
        lines.append(f"- Skipped: {battery['skipped']}")
    else:
        lines.append(f"- Functions: {_fmt(s.get('functions'))}, mean cyclomatic **{_fmt(s.get('ccn_mean'))}**, "
                     f"max {_fmt(s.get('ccn_max'))}")
        lines.append(f"- % functions CCN>10 (McCabe risk): **{_fmt(s.get('pct_ccn_gt10'), '%')}**, "
                     f"% long methods (>60 lines): {_fmt(s.get('pct_len_gt60'), '%')}, "
                     f"cognitive max: {_fmt(s.get('cog_max'))}")
        lines.append("- Caveat: cyclomatic is ~0.7–0.9 collinear with SLOC at file level (Jay 2009) — "
                     "a 'high CC' file may just be big. Cognitive complexity proxies comprehension "
                     "*effort*, not correctness (Muñoz Barón 2020); no validated threshold. "
                     "Maintainability Index (radon) is **directional only** (van Deursen 2014).")
    lines.append("")

    # --- process ---
    lines.append("## Process/history metrics (Rahman & Devanbu 2013: > product metrics)")
    lines.append("")
    if process.get("commits_analyzed"):
        own = process.get("ownership") or {}
        cs = process.get("commit_size") or {}
        lines.append(f"- Change entropy (Hassan HCM, 0–1): {_fmt(s.get('change_entropy'))}")
        lines.append(f"- Bus factor (authors for 50% of churn): {_fmt(own.get('bus_factor_50pct'))}, "
                     f"top-author share: {_fmt(own.get('top_author_share'))}")
        lines.append(f"- Commit size: median churn {_fmt(cs.get('median_churn'))}, "
                     f"p90 {_fmt(cs.get('p90_churn'))}, large (>400 LOC) {_fmt(cs.get('pct_large_commits_gt400'), '%')}")
        lines.append(f"- Fix-commit % (SZZ-lite defect proxy): {_fmt(s.get('fix_commit_pct'), '%')}")
        lines.append("- Caveat: **bus-factor assumes human authorship** — in an agentic pipeline the "
                     "author often collapses to one operator identity, so read it as a signal about "
                     "the attribution model, not team resilience.")
    else:
        lines.append("- No non-merge commits with code changes found.")
    lines.append("")

    # --- AI-slop signals ---
    lines.append("## AI-slop signals vs GitClear baselines")
    lines.append("")
    if survival.get("skipped"):
        lines.append(f"- Code survival: skipped ({survival['skipped']}).")
    else:
        lines.append(f"- 14-day code survival: **{_fmt(s.get('survival_14d_pct'), '%')}** "
                     f"(GitClear [VENDOR] pre-AI 96.9% / AI 94.3%), half-life {_fmt(s.get('half_life_days'))} days")
    if duplication.get("skipped"):
        lines.append(f"- Duplication: skipped ({duplication['skipped']}).")
    else:
        lines.append(f"- Production duplication: **{_fmt(s.get('duplication_pct_tokens'), '%')}** tokens "
                     f"(GitClear [VENDOR] pre-AI 8.3% / AI 12.3%)")
    lines.append("- Caveat: GitClear is a **vendor** longitudinal series; the *direction* (churn & "
                 "duplication up, reuse down) is corroborated by DORA 2024, but the exact multiples "
                 "are framing-dependent — treat bands as reference, not verdicts.")
    lines.append("")

    if charts:
        lines.append("## Charts")
        lines.append("")
        for c in charts:
            lines.append(f"![{os.path.basename(c)}]({os.path.basename(c)})")
        lines.append("")
    else:
        lines.append("_Charts skipped (matplotlib not installed)._")
        lines.append("")

    return "\n".join(lines)


def write_report(engines: dict, out_dir: str) -> str:
    """Build combined.json, charts, and report.md in out_dir. Returns report.md path."""
    os.makedirs(out_dir, exist_ok=True)
    combined = build_combined(engines)
    with open(os.path.join(out_dir, "combined.json"), "w") as fh:
        json.dump(combined, fh, indent=2)
    charts = render_charts(engines, out_dir)
    md = render_markdown(engines, combined, charts)
    report_path = os.path.join(out_dir, "report.md")
    with open(report_path, "w") as fh:
        fh.write(md)
    return report_path
