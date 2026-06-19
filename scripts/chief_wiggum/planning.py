"""Wave planning and gating for ``/implement-wave``.

Wave planning is algorithmic and risky: a mistake can launch tickets before
their dependencies have landed, or build a ticket that is blocked by a ``TBD:``
marker. This module turns the dependency graph + ticket state into a
deterministic, tested wave plan.

Inputs:
    issues   - all ticket numbers in the epic
    edges    - adjacency map ``{n: [deps...]}`` (n depends on each dep);
               typically ``DependencyGraphMetadata.edges`` from github.py
    closed   - tickets already closed (their dependents are unblocked)
    gated    - tickets that cannot be built (e.g. unresolved TBD markers or a
               failed prior implementation); they and their transitive
               dependents are held back

Output: a :class:`WavePlan` with ``waves`` (dependency-ordered, parallelizable
batches of buildable tickets), ``gated``, ``skipped`` (already closed),
``warnings``, and ``integration_risks``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field


class DependencyCycleError(ValueError):
    """Raised when the dependency graph contains a cycle (no valid ordering)."""

    def __init__(self, cycle: list[int]):
        self.cycle = cycle
        chain = " -> ".join(f"#{n}" for n in cycle)
        super().__init__(f"dependency cycle detected: {chain}")


@dataclass
class WavePlan:
    waves: list[list[int]] = field(default_factory=list)
    gated: list[int] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    integration_risks: list[str] = field(default_factory=list)
    gate_reasons: dict[int, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "waves": self.waves,
            "gated": self.gated,
            "skipped": self.skipped,
            "warnings": self.warnings,
            "integration_risks": self.integration_risks,
            "gate_reasons": {str(k): v for k, v in self.gate_reasons.items()},
        }


def _detect_cycle(nodes: set[int], deps_of: Mapping[int, list[int]]) -> list[int] | None:
    """Return a cycle as a node list if one exists among ``nodes``, else None."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in nodes}
    stack: list[int] = []

    def visit(n: int) -> list[int] | None:
        color[n] = GRAY
        stack.append(n)
        for dep in deps_of.get(n, []):
            if dep not in nodes:
                continue
            if color[dep] == GRAY:
                idx = stack.index(dep)
                return stack[idx:] + [dep]
            if color[dep] == WHITE:
                found = visit(dep)
                if found:
                    return found
        color[n] = BLACK
        stack.pop()
        return None

    for node in sorted(nodes):
        if color[node] == WHITE:
            found = visit(node)
            if found:
                return found
    return None


def plan_waves(
    issues: Iterable[int],
    edges: Mapping[int, Iterable[int]],
    *,
    closed: Iterable[int] = (),
    gated: Iterable[int] = (),
) -> WavePlan:
    """Compute a dependency-ordered wave plan.

    Raises :class:`DependencyCycleError` if the open subgraph has a cycle.
    """
    issues = set(issues)
    closed_set = set(closed)
    gated_set = set(gated)
    deps_of: dict[int, list[int]] = {n: list(dict.fromkeys(edges.get(n, []))) for n in issues}
    # Nodes include any issue referenced as a dependency too.
    nodes = set(issues) | {d for deps in deps_of.values() for d in deps}

    plan = WavePlan()

    # Cycle detection over all nodes (open or closed) — a cycle is unbuildable.
    cycle = _detect_cycle(nodes, deps_of)
    if cycle is not None:
        raise DependencyCycleError(cycle)

    # Closed tickets are done; nothing to schedule.
    open_issues = {n for n in issues if n not in closed_set}
    plan.skipped = sorted(n for n in issues if n in closed_set)

    def reason_for(n: int) -> tuple[bool, str]:
        """Is node n a *direct* block source, and why."""
        if n in gated_set:
            return True, "gated (unresolved marker or failed implementation)"
        for dep in deps_of.get(n, []):
            # A dep is satisfiable only if it is an epic ticket (will be built)
            # or already closed. Anything else is an unknown reference.
            if dep not in issues and dep not in closed_set:
                return True, f"depends on missing/unknown #{dep}"
        return False, ""

    # Seed directly-blocked nodes (gated + missing dependency refs).
    blocked: dict[int, str] = {}
    for n in sorted(open_issues):
        is_blocked, why = reason_for(n)
        if is_blocked:
            blocked[n] = why
            if "missing/unknown" in why:
                plan.warnings.append(f"#{n} {why}; holding it and its dependents")

    # Propagate blocking transitively to dependents.
    changed = True
    while changed:
        changed = False
        for n in sorted(open_issues):
            if n in blocked:
                continue
            for dep in deps_of.get(n, []):
                if dep in blocked:
                    blocked[n] = f"depends on blocked #{dep}"
                    changed = True
                    break

    buildable = {n for n in open_issues if n not in blocked}

    # Kahn-style wave assignment. A dep is satisfied if it is closed or already
    # placed in an earlier wave. Closed nodes seed the "placed" set.
    placed: set[int] = set(closed_set)
    remaining = set(buildable)
    while remaining:
        wave = sorted(
            n
            for n in remaining
            if all(dep in placed for dep in deps_of.get(n, []))
        )
        if not wave:
            # Should not happen for a cycle-free, correctly-blocked graph, but
            # guard against silent deadlock.
            for n in sorted(remaining):
                blocked[n] = "unresolved dependency (deadlock guard)"
                plan.warnings.append(f"#{n} could not be scheduled; marking blocked")
            break
        plan.waves.append(wave)
        placed.update(wave)
        remaining.difference_update(wave)

    plan.gated = sorted(blocked)
    plan.gate_reasons = dict(sorted(blocked.items()))

    # Integration risks: a ticket many others depend on (fan-in > 1) is a
    # shared dependency whose change ripples; flag it for extra integration care.
    dependents: dict[int, list[int]] = {}
    for n in issues:
        for dep in deps_of.get(n, []):
            dependents.setdefault(dep, []).append(n)
    for dep in sorted(dependents):
        if len(dependents[dep]) > 1:
            who = ", ".join(f"#{d}" for d in sorted(dependents[dep]))
            plan.integration_risks.append(
                f"#{dep} is a shared dependency of {who} (diamond / fan-out)"
            )

    return plan


def render_markdown(plan: WavePlan) -> str:
    """Render a concise human-readable wave plan report."""
    lines = ["# Wave Plan", ""]
    if plan.waves:
        for i, wave in enumerate(plan.waves):
            tickets = ", ".join(f"#{n}" for n in wave)
            lines.append(f"- **Wave {i + 1}** ({len(wave)}): {tickets}")
    else:
        lines.append("- _No buildable tickets._")
    if plan.skipped:
        lines += ["", "## Skipped (already closed)", ""]
        lines.append(", ".join(f"#{n}" for n in plan.skipped))
    if plan.gated:
        lines += ["", "## Gated / blocked", ""]
        for n in plan.gated:
            lines.append(f"- #{n}: {plan.gate_reasons.get(n, 'blocked')}")
    if plan.integration_risks:
        lines += ["", "## Integration risks", ""]
        lines += [f"- {risk}" for risk in plan.integration_risks]
    if plan.warnings:
        lines += ["", "## Warnings", ""]
        lines += [f"- {w}" for w in plan.warnings]
    return "\n".join(lines) + "\n"
