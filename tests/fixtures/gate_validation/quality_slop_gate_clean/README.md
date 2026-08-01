# quality_slop_gate fixture band files

`quality_slop_gate.py`'s two signals come from **external tools** —
`git-of-theseus` (code survival) and `jscpd` (duplication) — that are
non-deterministic (depend on live git history / clone state) and **not installed
in chief-wiggum's own CI**. A clean-corpus run that shelled out to them would
either silently "skip" (not a real trial) or not be reproducible.

Per CTR-fh-044, the gate-validation record pins a **fixture band file**, not a
live/AI-non-deterministic dependency. These JSON files are pre-computed
`survival.analyze()` / `duplication.analyze()` result-dict shapes. The trials
feed them to the gate's REAL pure verdict functions
(`evaluate_survival`, `evaluate_duplication`, `has_findings`), which is where the
gate's actual banding/classification claims live — the part worth validating.

Each file's top-level `survival_result` / `duplication_result` is exactly the
dict shape the corresponding engine returns (see `scripts/quality/survival.py`
and `scripts/quality/duplication.py`).

Reference bands (from `quality_slop_gate.py`): survival pre-AI 96.9% / AI 94.3%
(higher is better); duplication pre-AI 8.3% / AI 12.3% (lower is better). Only a
`past-ai` band on a `measured` signal is a finding.

Note on key representation: the real survival engine emits **integer** age keys
(`survival_by_age_days: {14: ..., 30: ...}` — `AGES` is an int list). JSON can
only serialize **string** keys, so these files carry `"14"`/`"30"`; the gate
tolerates both via `by_age.get(14) or by_age.get("14")`. The
`config-indirection` trial exercises the string-key (serialized) path and the
`direct` trial re-casts to the engine-native integer-key path, proving both
representations classify identically.
