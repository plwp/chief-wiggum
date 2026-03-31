#!/usr/bin/env python3
"""
Git provenance enrichment for stitch-audit findings.

For BREAK/WARN findings, traces how each break was introduced using
git blame and GitHub API (via gh CLI). Answers the question: "Were both
sides introduced in the same PR? Same issue? Or different work streams?"

Usage:
    python3 stitch_provenance.py <findings.json> --repo <path> --gh-repo <owner/repo> [-o output.json]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class BlameInfo:
    """Result of git blame for a single line."""

    sha: str
    author: str
    summary: str


@dataclass
class PRInfo:
    """GitHub PR linked to a commit."""

    number: int
    title: str
    url: str
    linked_issues: list[str]  # Issue numbers extracted from PR body


# Cache to avoid redundant API calls
_blame_cache: dict[str, BlameInfo] = {}
_pr_cache: dict[str, PRInfo | None] = {}


def git_blame_line(repo_path: Path, file_path: str, line: int) -> BlameInfo | None:
    """Run git blame on a single line and return commit info."""
    cache_key = f"{file_path}:{line}"
    if cache_key in _blame_cache:
        return _blame_cache[cache_key]

    try:
        result = subprocess.run(
            ["git", "blame", "-L", f"{line},{line}", "--porcelain", "--", file_path],
            capture_output=True,
            text=True,
            cwd=repo_path,
            timeout=30,
        )
        if result.returncode != 0:
            return None

        lines = result.stdout.strip().split("\n")
        if not lines:
            return None

        sha = lines[0].split()[0]
        author = ""
        summary = ""
        for bl in lines:
            if bl.startswith("author "):
                author = bl[7:]
            elif bl.startswith("summary "):
                summary = bl[8:]

        info = BlameInfo(sha=sha, author=author, summary=summary)
        _blame_cache[cache_key] = info
        return info

    except (subprocess.TimeoutExpired, OSError):
        return None


def get_pr_for_commit(gh_repo: str, sha: str) -> PRInfo | None:
    """Look up the PR that introduced a commit via GitHub API."""
    if sha in _pr_cache:
        return _pr_cache[sha]

    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{gh_repo}/commits/{sha}/pulls",
             "--jq", '.[0] | {number, title, html_url, body}'],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0 or not result.stdout.strip():
            _pr_cache[sha] = None
            return None

        data = json.loads(result.stdout)
        if not data or not data.get("number"):
            _pr_cache[sha] = None
            return None

        # Extract linked issues from PR body
        linked_issues: list[str] = []
        body = data.get("body", "") or ""
        import re
        for match in re.finditer(r"(?:closes|fixes|resolves)\s+#(\d+)", body, re.IGNORECASE):
            linked_issues.append(match.group(1))

        pr_info = PRInfo(
            number=data["number"],
            title=data.get("title", ""),
            url=data.get("html_url", ""),
            linked_issues=linked_issues,
        )
        _pr_cache[sha] = pr_info
        return pr_info

    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        _pr_cache[sha] = None
        return None


def enrich_finding(
    finding: dict,
    repo_path: Path,
    gh_repo: str,
) -> dict:
    """Add git provenance to a single finding."""
    provenance: dict[str, dict] = {}

    # Enrich source side
    if finding.get("source_file") and finding.get("source_line"):
        blame = git_blame_line(repo_path, finding["source_file"], finding["source_line"])
        if blame:
            source_prov: dict = {
                "sha": blame.sha,
                "author": blame.author,
                "commit_summary": blame.summary,
            }
            pr = get_pr_for_commit(gh_repo, blame.sha)
            if pr:
                source_prov["pr_number"] = pr.number
                source_prov["pr_title"] = pr.title
                source_prov["pr_url"] = pr.url
                if pr.linked_issues:
                    source_prov["linked_issues"] = pr.linked_issues
            provenance["source"] = source_prov

    # Enrich target side
    if finding.get("target_file") and finding.get("target_line"):
        blame = git_blame_line(repo_path, finding["target_file"], finding["target_line"])
        if blame:
            target_prov: dict = {
                "sha": blame.sha,
                "author": blame.author,
                "commit_summary": blame.summary,
            }
            pr = get_pr_for_commit(gh_repo, blame.sha)
            if pr:
                target_prov["pr_number"] = pr.number
                target_prov["pr_title"] = pr.title
                target_prov["pr_url"] = pr.url
                if pr.linked_issues:
                    target_prov["linked_issues"] = pr.linked_issues
            provenance["target"] = target_prov

    # Add analysis: same PR? Same issue? Different work streams?
    if "source" in provenance and "target" in provenance:
        src = provenance["source"]
        tgt = provenance["target"]

        if src.get("sha") == tgt.get("sha"):
            provenance["analysis"] = "same_commit"
        elif src.get("pr_number") and src["pr_number"] == tgt.get("pr_number"):
            provenance["analysis"] = "same_pr"
        elif src.get("linked_issues") and tgt.get("linked_issues"):
            overlap = set(src["linked_issues"]) & set(tgt["linked_issues"])
            if overlap:
                provenance["analysis"] = f"same_issue({'#' + ', #'.join(overlap)})"
            else:
                provenance["analysis"] = "different_work_streams"
        elif src.get("author") == tgt.get("author"):
            provenance["analysis"] = "same_author_different_prs"
        else:
            provenance["analysis"] = "different_work_streams"

    if provenance:
        finding["provenance"] = provenance

    return finding


def enrich_findings(
    findings: list[dict],
    repo_path: Path,
    gh_repo: str,
) -> list[dict]:
    """Enrich BREAK/WARN findings with git provenance."""
    enriched = []
    for finding in findings:
        if finding.get("severity") in ("BREAK", "WARN"):
            finding = enrich_finding(finding, repo_path, gh_repo)
        enriched.append(finding)
    return enriched


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Git provenance enrichment for stitch-audit"
    )
    parser.add_argument("findings_json", help="Path to findings JSON file")
    parser.add_argument("--repo", required=True, help="Path to target repo")
    parser.add_argument("--gh-repo", required=True, help="GitHub owner/repo (e.g. acme/app)")
    parser.add_argument("-o", "--output", help="Write output to file")
    args = parser.parse_args()

    findings_path = Path(args.findings_json)
    if not findings_path.exists():
        print(f"Error: {findings_path} not found", file=sys.stderr)
        sys.exit(1)

    repo_path = Path(args.repo).resolve()
    if not repo_path.is_dir():
        print(f"Error: {repo_path} is not a directory", file=sys.stderr)
        sys.exit(1)

    findings = json.loads(findings_path.read_text())
    break_warn = [f for f in findings if f.get("severity") in ("BREAK", "WARN")]
    print(f"Enriching {len(break_warn)} BREAK/WARN findings with provenance", file=sys.stderr)

    enriched = enrich_findings(findings, repo_path, args.gh_repo)

    output = json.dumps(enriched, indent=2)
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output)
        print(f"OK: enriched findings written to {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
