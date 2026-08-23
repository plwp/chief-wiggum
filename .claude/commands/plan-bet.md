# Plan Bet - Business Model Authoring Stage

Produce a real, typed, human-chosen business model for a bet — before any spend beyond discovery. The output is `bets/<bet-id>/business-model.json` in the portfolio repo: BMO-enum structure, Lean Canvas hypothesis-bearing fields (each `{value, status, asm_ids}`), a premortem-derived failure-mode ledger, a VPC pain↔reliever map, and (for multi-actor models) e3-value per-actor flows — hashed into the portfolio journal as a binding artifact once authored.

This stage exists because a bet's assumption ledger (chief-wiggum#236) and kill criteria (chief-wiggum#235) presuppose a business model to hang assumptions off — without one, `depends_on_element` is free text pointing at nothing, and Maurya's Fermi/reverse-income-statement math (the cheapest failing test in the whole literature) never runs before spend. `/plan-bet` is where someone actually does the arithmetic.

## Usage
```
/plan-bet <bet-id> [--directions N] [--portfolio-dir PATH]
```

## Parameters
- `bet-id`: An existing bet (`bets/<bet-id>/bet.json` already created via `bet.py create` — this stage does not create the bet record itself, only its business model)
- `--directions N`: Number of divergent business-model candidates to generate (default 3, max 4)
- `--portfolio-dir`: Portfolio repo override (default `$CHIEF_WIGGUM_PORTFOLIO` or `~/.chief-wiggum/portfolio`)

## Where it sits

```
bet.py create → /plan-bet → assumption.py add/card → channel.py brainstorm → bet.py evaluate → kill-review
                 ↑ bet-level, runs once per bet          ↑ assumption depends_on_element joins canvas/vpc field ids
```

Bet-level, once per bet (re-run via `plan_bet.py rebaseline` to revise, never a silent hand-edit). `/architect` folds `business-model.json` into an epic's contracts once a bet reaches `building` — the same "extract once, gate downstream" shape `/design`'s `design.json` uses for epics.

**Run autonomously** except for Step 3 — the human choosing a candidate is the one genuinely human checkpoint in this skill. Everything else, including the Fermi arithmetic, is mechanical.

## Workflow

### Step 0: Resolve paths and session temp

```bash
CW_HOME="${CHIEF_WIGGUM_HOME:-$HOME/repos/chief-wiggum}"
CW_HOME=$(python3 "$CW_HOME/scripts/env.py" home)
# Pin the interpreter CW scripts run under. A bare `python3` is whatever
# the shell resolves, so a Homebrew bump silently strands keyring /
# jsonschema / google-genai and kills consults mid-phase (chief-wiggum#374).
CW_PY=$(python3 "$CW_HOME/scripts/env.py" python) || CW_PY=python3
CW_TMP=$("${CW_PY:-python3}" "$CW_HOME/scripts/env.py" tmp)
PLAN_TMP="$CW_TMP/plan-bet/$bet_id" && mkdir -p "$PLAN_TMP"
PORTFOLIO_DIR="${portfolio_dir_flag:-${CHIEF_WIGGUM_PORTFOLIO:-$HOME/.chief-wiggum/portfolio}}"
```

### Step 1: Gather inputs

Read from the portfolio (never invent — every candidate must be traceable to something already on file):

- `bets/<bet-id>/bet.json` — thesis, envelope, acquisition plan, signal tier, competitor sweep. The thesis is the seed every candidate's `canvas.problem`/`value-proposition`/`competitive-position` fields are derived from, not free invention.
- `bets/<bet-id>/assumptions.json` (if present) — existing `ASM-NNN` entries and their `depends_on_element` values. These strings are load-bearing: they must resolve to `canvas`/`vpc` field ids in the authored business model (`plan_bet.py check`'s ASM↔canvas join), so read them BEFORE drafting `canvas` so the field ids line up, not after.
- `means.json` (if present) — skills/assets/network, for `structure.actor_types` and channel-type framing (an ecosystem-channel bet reads differently from a direct-sales one).
- `docs/business-factory.md` §2.2/§3 (in the chief-wiggum checkout) — the schema donors (BMO, Lean Canvas, VPC, e3-value) and `templates/business-model-schema.json`'s field-level descriptions, which document exactly which fields are grounded in which real portfolio bet vs. which complete the donor formalism's fixed grammar.

### Step 2: Divergent business-model candidates

Generate **2–3 deliberately distinct candidates** (e.g. narrow-AU-wedge / broad-market / channel-partner-led — name each and give it a one-line strategic intent). One generated model converges to the model's default framing of the thesis; distinct candidates give the human a real decision, same rationale as `/design`'s divergent directions (CLAUDE.md: "Designs are chosen, not converged").

Each candidate is a complete `bets/<bet-id>/business-model.json` draft (`templates/business-model-schema.json`), in `$PLAN_TMP/candidates/<name>/business-model.json`. Requirements:

1. **Every `canvas`/`vpc` field starts `status: "hypothesis"` with `asm_ids: []`** unless an existing `ASM-NNN` entry from Step 1 already validates/falsifies it — a candidate is never pre-validated by construction.
2. **`fermi_inputs` is mandatory on every candidate**, not optional — the whole point of divergence here is that different MSC/price/channel choices produce different Fermi arithmetic; a candidate that skips it cannot be compared on the axis that matters first.
3. **`premortem` carries ≥5 failure modes per candidate**, drafted from the bet's actual thesis and (if present) its `kill-criteria.json`/`kill-brief.md` history — never generic ("might not find customers") when the bet's own record names a real one (e.g. a discovered direct-twin competitor becomes a premortem entry, not just a `bet.py finding`).
4. **`structure.actor_types` reflects the REAL value network** named in the thesis/acquisition plan — an ecosystem-channel bet (`bet.json` `acquisition.ecosystem_channel` set) almost always has ≥2 actors (the operator's customer plus the platform); count honestly, since Decision 5's e3-value check keys off this count.

Generate each candidate with a **design-direction-shaped worker** (contract: `docs/worker-contracts.md#design-direction-worker`, applied to typed JSON instead of HTML): all workers get identical context (bet.json, assumptions.json, means.json, the schema); only the strategic brief differs. Divergence comes from the briefs, not from temperature.

**Run every candidate through the checks in report-only mode before showing the human anything:**

```bash
for f in "$PLAN_TMP"/candidates/*/business-model.json; do
  "${CW_PY:-python3}" "$CW_HOME/scripts/plan_bet.py" author "$bet_id" --file "$f" --portfolio-dir "$PORTFOLIO_DIR" --min-failure-modes 5
done
```

A candidate whose Fermi block is unconditionally BLOCKED is a genuine, useful outcome — the operator's own numbers say that framing cannot reach its MSC from its TAM. Keep it in the comparison (labelled infeasible) rather than silently dropping it; that is the arithmetic doing its job before any spend, and the operator should see it.

**Orchestrator verifies before showing the user**: read every candidate's `plan_bet.py author` output yourself (report-only findings, Fermi verdict, premortem coverage count). A candidate that fails schema validation gets fixed and re-run, never shown broken.

### Step 3: Human chooses (the checkpoint)

Present the candidates side by side: name, one-line strategic intent, Fermi viability verdict (required top-of-funnel prospects vs. TAM — the single most decision-relevant number), premortem failure-mode count, and the `canvas.value-proposition`/`competitive-position` text. Ask the user to pick one and give feedback.

Iterate the chosen candidate against the feedback — re-run `plan_bet.py author` (still against the tmp file, not yet committed) after each round — until the user approves.

This is the **only** step that blocks on the user. Do not add approval gates elsewhere.

### Step 4: Author the binding artifact

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/plan_bet.py" author "$bet_id" \
  --file "$PLAN_TMP/candidates/$CHOSEN/business-model.json" \
  --portfolio-dir "$PORTFOLIO_DIR"
```

This is the mechanical-extraction step (Decision 1): the chosen candidate is schema-validated, run through every check (premortem coverage, pain↔reliever completeness, e3-value if >2 actors, and the Fermi gate — which blocks unconditionally on an arithmetic impossibility, `--gate` or not), content-hashed, and journaled into the portfolio's hash chain as `bets/<bet-id>/business-model.json`. A REFUSED run means fix the candidate and re-author — never hand-edit the file to route around a finding; `plan_bet.py rebaseline --reason "..."` is the only path once something has been authored.

### Step 5: Report

Report to the user:
- The chosen candidate and a one-line rationale
- The structure classification (`revenue_model`, `distribution_channel_type`, `value_configuration`, actor count) and what it implies (e.g. actor count >2 → e3-value now runs on every future `check`)
- The Fermi viability numbers: required top-of-funnel prospects vs. declared TAM, and the margin
- Premortem coverage: how many failure modes, how many mapped vs. waived
- Any report-only findings left open (these will surface again on every future `plan_bet.py check`)
- What happens downstream: `assumption.py add --element <canvas-field-id>` now has real field ids to point at; `/architect` folds `business-model.json` into the epic's contracts once the bet reaches `building`.

## Key principles

- **Divergence then choice, not iteration from one attempt**: one generated business model converges to the model's default framing of the thesis.
- **The Fermi gate is not report-only**: unlike every other check in this stage (and unlike almost every other CW gate at bring-up), an arithmetic impossibility blocks unconditionally — Decision 3 is a deliberate, stated exception to the usual report-only-until-validated ramp, because the risk of a false positive here is zero: either the bet's own declared numbers reach TAM or they don't.
- **Fields are extracted from what is already on file, not invented**: the thesis, existing assumptions, and means.json ground every candidate; a canvas field with no basis in any of them is a candidate that failed to do its homework.
- **One human checkpoint**: taste/strategic framing (Step 3). Everything else, including the viability arithmetic, runs autonomously.
