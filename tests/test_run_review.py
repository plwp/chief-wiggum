"""Tests for scripts/run_review.py -- ticket attribution for review-phase
consults (chief-wiggum#345 AC2).

Review-phase consults must carry the ticket they were run for. Untagged
reviewer spend is why #381's cost slice read $0.00 while two reviewer
consults ran (#345). `run_review.py`'s `execute` closure calls
`consult_provider(...)` without a `ticket=` kwarg at all -- this file pins
down that a `--ticket` flag (or the ticket-context's own `number`) reaches
every one of those calls.

These tests stub `chief_wiggum.review.run_review` itself rather than
building a real git worktree + provider-quorum config: the bug under test is
entirely inside run_review.py's own `execute` closure (whether it threads
`ticket=` into `consult_provider`), which is independent of how the review
quorum resolves providers or diffs a real repo -- `tests/test_review_pipeline.py`
already covers that machinery. Stubbing `review.run_review` to simply invoke
the `execute` callable it was handed keeps these tests deterministic and
fast, with no dependency on a real `config/providers.json` reviewer role or
installed codex/gemini CLIs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import run_review  # noqa: E402

# A distinct sentinel (never `None`) so a call that omits `ticket=` entirely
# is distinguishable from one that explicitly passes `ticket=None` -- the
# unmodified `execute` closure never passes the kwarg at all, so without this
# sentinel a "ticket should be None" assertion would pass even against
# unimplemented code, which is not a real red test.
_NOT_PASSED = object()


def _write_ticket_context(path: Path, *, number=None) -> Path:
    data = {
        "number": number,
        "title": "Do a thing",
        "body": "body text",
        "acceptance_criteria": ["AC one"],
        "comments": [],
    }
    path.write_text(json.dumps(data))
    return path


def _install_fake_run_review(monkeypatch, recorded_calls, *, providers=("codex", "gemini")):
    """Replace chief_wiggum.review.run_review with a stub that calls the real
    `execute` closure once per fake provider -- exercising run_review.py's own
    ticket-threading logic without any real git/provider-quorum machinery."""

    def fake_run_review(ticket, worktree, base, output_dir, *, template,
                        checklist=None, epic_sections=(), role="reviewer",
                        execute=None, force_fresh=False, epic_slug=None, **kwargs):
        for name in providers:
            provider = SimpleNamespace(name=name, type="tool", tool=name)
            execute(provider, f"prompt for {name}")
        return SimpleNamespace(
            ok=True,
            provider_manifest={"ok": True},
            to_dict=lambda: {"ok": True},
        )

    monkeypatch.setattr(run_review.review, "run_review", fake_run_review)

    def fake_consult_provider(provider, prompt, model, cwd, *, ticket=_NOT_PASSED,
                              timeout_override=None):
        recorded_calls.append({"provider": provider.name, "ticket": ticket})
        return "a response", {"tokens_in": 1, "tokens_out": 1}

    monkeypatch.setattr(run_review, "consult_provider", fake_consult_provider)


def test_ticket_flag_reaches_every_consult_provider_call(tmp_path, monkeypatch):
    """`--ticket 42` must reach EVERY `consult_provider` call the review
    quorum makes, not just the first / a subset."""
    ctx = _write_ticket_context(tmp_path / "ticket.json", number=None)
    recorded: list[dict] = []
    _install_fake_run_review(monkeypatch, recorded)

    rc = run_review.main([
        "--ticket-context", str(ctx),
        "--worktree", str(tmp_path),
        "--base", "main",
        "--output-dir", str(tmp_path / "reviews"),
        "--ticket", "42",
    ])

    assert rc == 0
    assert len(recorded) == 2
    assert all(c["ticket"] == "42" for c in recorded)


def test_ticket_defaults_to_the_ticket_context_number(tmp_path, monkeypatch):
    """No `--ticket` flag: fall back to `ticket.json`'s own `number` (the
    natural default -- write_ticket_context.py already populates it in the
    /implement Step 2 flow) instead of silently dropping the tag."""
    ctx = _write_ticket_context(tmp_path / "ticket.json", number=42)
    recorded: list[dict] = []
    _install_fake_run_review(monkeypatch, recorded)

    rc = run_review.main([
        "--ticket-context", str(ctx),
        "--worktree", str(tmp_path),
        "--base", "main",
        "--output-dir", str(tmp_path / "reviews"),
    ])

    assert rc == 0
    assert recorded and all(c["ticket"] == "42" for c in recorded)


def test_explicit_ticket_flag_overrides_the_ticket_context_number(tmp_path, monkeypatch):
    """The flag is an override, not merely a fallback -- an explicit --ticket
    must win over whatever ticket.json says."""
    ctx = _write_ticket_context(tmp_path / "ticket.json", number=42)
    recorded: list[dict] = []
    _install_fake_run_review(monkeypatch, recorded)

    rc = run_review.main([
        "--ticket-context", str(ctx),
        "--worktree", str(tmp_path),
        "--base", "main",
        "--output-dir", str(tmp_path / "reviews"),
        "--ticket", "99",
    ])

    assert rc == 0
    assert recorded and all(c["ticket"] == "99" for c in recorded)


def test_missing_ticket_number_is_none_not_a_crash(tmp_path, monkeypatch):
    """No --ticket flag AND ticket.json has no number: ticket=None must reach
    consult_provider explicitly (never silently omitted, never a crash) so
    the gap is visible in the ledger rather than papered over."""
    ctx = _write_ticket_context(tmp_path / "ticket.json", number=None)
    recorded: list[dict] = []
    _install_fake_run_review(monkeypatch, recorded)

    rc = run_review.main([
        "--ticket-context", str(ctx),
        "--worktree", str(tmp_path),
        "--base", "main",
        "--output-dir", str(tmp_path / "reviews"),
    ])

    assert rc == 0
    assert recorded and all(c["ticket"] is None for c in recorded)
