"""Literature-grounded code-quality metric engines for the /code-metrics skill.

Each module exposes a pure ``analyze(...)`` function returning a JSON-serialisable
dict. External-tool wrappers degrade gracefully: when a tool is absent they return
``{"skipped": "<tool> not found"}`` rather than raising, so a partial run never
crashes the orchestrator.

Engines:
  - churn:       git-history churn / attribution / hotspots (pure git)
  - complexity:  cyclomatic (lizard), cognitive (gocognit/complexipy), MI (radon)
  - trend:       complexity/scale sampled across history (git worktree + lizard)
  - survival:    code survival / 2-week churn (git-of-theseus)
  - process:     change coupling, entropy, ownership/bus-factor, commit size, fixes
  - duplication: production copy/paste ratio (jscpd)
  - report:      consolidate engine JSON + render charts + emit markdown
"""

from __future__ import annotations
