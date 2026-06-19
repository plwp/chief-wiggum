# Design - Product Design Stage

Produce a real, rendered, human-chosen visual design for the product — before any epic is architected. The output is `docs/design/` in the target repo: binding tokens (`design.json`), the approved mockups as living reference implementations, and reference screenshots that become the design-fidelity gate's comparison baseline.

This stage exists because the pipeline has a design *contract* (ui-spec `design` section) and a design *gate* (`/implement` Step 9), but a brainstorm conversation that picks a primary color is a token file, not a design. Dogeared-coach shipped with a sound token architecture and still looked like an unthemed admin tool — nobody ever designed anything. `/design` is where someone does.

## Usage
```
/design <owner/repo> [--directions N] [--skip-critique]
```

## Parameters
- `owner/repo`: Target GitHub repository
- `--directions N`: Number of divergent design directions to generate (default 3, max 4)
- `--skip-critique`: Skip the multi-AI critique step (Step 4)

## Where it sits

```
/seed → /design → /plan-epic → /architect → /implement-wave → /close-epic
         ↑ product-level, runs once               ↑ consumes docs/design/
```

Product-level, once per product. Re-run it to evolve the brand. Epics inherit it: `/architect` folds `docs/design/design.json` into each epic's ui-spec `design` section and registers the reference screenshots as `reference-screenshot` assets.

**Run autonomously** except for Step 3 — the human choosing a direction is the one genuinely human checkpoint in this skill. Taste is the user's call; everything else is yours.

## Workflow

### Step 0: Resolve paths and session temp

```bash
CW_HOME="${CHIEF_WIGGUM_HOME:-$HOME/repos/chief-wiggum}"
CW_HOME=$(python3 "$CW_HOME/scripts/env.py" home)
CW_TMP=$(python3 "$CW_HOME/scripts/env.py" tmp)
DESIGN_TMP="$CW_TMP/design" && mkdir -p "$DESIGN_TMP"
TARGET_DIR=$(python3 "$CW_HOME/scripts/repo.py" resolve "$owner_repo")
```

### Step 1: Gather inputs and pick the path

Read from the target repo:
- `docs/domain-context.md` (from `/seed` Step 2.5): audience, domain vocabulary, real use cases — these feed mock content and voice
- The seed brainstorm's **design source** decision (architecture decisions doc or seed issues): existing design system / reference product / brand kit / net-new
- The page inventory if one exists (planning docs, issues) — otherwise infer the product's screens from the domain context

Pick the **2–3 most representative screens**: where users spend the most time and where the domain shows (e.g. the dashboard and the core object's detail view — not the login page).

Then branch on the design source:

- **`existing-design-system`** — skip path. There is nothing to invent: extract tokens from the existing theme/CSS files into `design.json` (map them onto the ui-spec `design.tokens` shape by hand if they aren't CSS custom properties), screenshot the existing UI for `reference/`, generate the styleguide, and jump to Step 5's assembly + Step 6. No divergent directions.
- **`reference-product`** — capture screenshots of the reference product NOW (Playwright against its URLs, or ingest screenshots the user provided into `$DESIGN_TMP/reference-product/`). Directions in Step 2 are interpretations of that reference, not free invention.
- **`brand-kit`** — ingest the kit (palette, fonts, logo files) into `$DESIGN_TMP/brand/`. Directions must use the brand palette and type; they diverge on layout, density, and treatment.
- **`net-new`** — full divergent flow. The directions ARE the brand exploration.

### Step 2: Divergent design directions (the core)

Generate **3–4 deliberately distinct directions** (e.g. warm-and-friendly / clinical-professional / bold-editorial — name each and give it a one-line intent). One generated design converges to the model's default taste; distinct directions give the human a real decision. Two directions that look the same at thumbnail size are one direction — regenerate the duplicate.

Each direction is **mockups-as-code**: one self-contained HTML file per representative screen, in `$DESIGN_TMP/directions/<direction>/<screen>.html`. Requirements:

1. **No build step**: a single HTML file with inline CSS. Google Fonts via `<link>` is allowed; no external JS.
2. **Token convention is mandatory** — every design value is declared as a CSS custom property on `:root` and referenced via `var()`:
   - `--color-<name>` (must include `--color-primary`), `--font-<role>` (heading/body/mono), `--text-<size>`, `--space-<name>`, `--radius-<name>`, `--shadow-<name>`
   - All screens within a direction share the **identical** `:root` block — `scripts/extract_design.py` extracts the contract from it mechanically in Step 5, so a value not in `:root` does not exist.
3. **Real content from the domain**: populate with realistic data, names, and copy from `docs/domain-context.md` — never lorem ipsum, never `Item 1`. Include at least one empty state with real voice ("No sessions yet — upload your first video to get started"), not "No data".
4. **High design quality is the point**: distinctive typography pairing with a reason, a purposeful palette with rationale, deliberate spacing rhythm. If a direction looks like a component library's default theme, that direction failed — regenerate it.

Generate each direction with a **parallel sub-agent** (use Opus — design quality is the product here). All sub-agents get identical context (domain context, screens, token convention, quality bar); only the direction brief differs. Divergence comes from the briefs, not from temperature.

**Render and look.** Screenshot every mock with Playwright at desktop (1440×900) and mobile (390×844) widths:

```bash
for f in "$DESIGN_TMP"/directions/*/*.html; do
  d=$(basename "$(dirname "$f")"); s=$(basename "$f" .html)
  python3 - "$f" "$DESIGN_TMP/screenshots/$d-$s" <<'EOF'
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright
src, out = Path(sys.argv[1]).resolve(), sys.argv[2]
Path(out).parent.mkdir(parents=True, exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch()
    for name, w, h in [("desktop", 1440, 900), ("mobile", 390, 844)]:
        page = browser.new_page(viewport={"width": w, "height": h})
        page.goto(src.as_uri())
        page.screenshot(path=f"{out}-{name}.png", full_page=True)
    browser.close()
EOF
done
```

**Orchestrator verifies before showing the user**: LOOK at every screenshot yourself. A mock that rendered broken (overlapping text, missing fonts, unstyled HTML) or generic gets regenerated, not presented. Never show the user a direction you wouldn't defend.

### Step 3: Human chooses (the taste checkpoint)

Present the directions side by side: for each, the name, one-line intent, and the screenshot paths (on macOS, `open` the desktop screenshots so they're actually visible). Ask the user to pick a direction and give feedback.

Iterate the chosen direction's mocks against the feedback — re-render and re-show after each round — until the user approves. Discard the losing directions (keep their files in `$DESIGN_TMP` for the session; they are not committed).

This is the **only** step that blocks on the user. Do not add approval gates elsewhere.

### Step 4: Multi-AI design critique

Unless `--skip-critique`: send the approved direction's screenshots to the `design_critic` quorum with the **same prompt** (value is in natural divergence). The role runs its providers in parallel with retries + output validation:

```bash
python3 "$CW_HOME/scripts/consult_ai.py" --role design_critic "$DESIGN_TMP/critique-prompt.md" \
  --cwd "$DESIGN_TMP" --output-dir "$DESIGN_TMP/critique"
```

Responses land at `$DESIGN_TMP/critique/design_critic-<provider>.md` with status in `design_critic-manifest.json`.

The prompt names the screenshot files (cwd is `$DESIGN_TMP` so the CLIs can open them) and asks for critique against:
- **Audience fit**: does this read right for the audience in `docs/domain-context.md`?
- **Hierarchy**: is the most important thing on each screen visually the most important?
- **Accessibility**: text contrast (WCAG AA), touch-target size on the mobile shots, focus/state affordances
- **Consistency**: same spacing rhythm, radius, and treatment across screens
- **Voice**: copy and empty states match the intended tone

Reconcile the three critiques. Fold **high-confidence findings** (a contrast failure, an inverted hierarchy, a 28px touch target) into the mocks and re-render. If the fixes visibly change the design, show the user the updated screenshots before finalizing — silently shipping a design the user didn't approve defeats Step 3. Note speculative/taste findings in the report; don't act on them.

### Step 5: Extract the contract

Tokens are **extracted, not transcribed** — `design.json` comes mechanically from the approved mock's CSS, so the contract cannot drift from what the human approved.

```bash
python3 "$CW_HOME/scripts/extract_design.py" extract \
  "$DESIGN_TMP/directions/$CHOSEN/<primary-screen>.html" \
  --source-kind net-new \
  --out "$DESIGN_TMP/design.json"
```

- `--source-kind` matches Step 1's branch; add `--reference` flags for the reference product URLs / brand kit paths.
- Heed the warnings: a `skipped unrecognised custom property` means a token the extractor couldn't map — fix the mock's naming, don't hand-edit the output.
- Since all screens share one `:root` block, extracting from any screen yields the same tokens. Spot-check by extracting a second screen and diffing.

Then **add what CSS can't carry** by editing `design.json`:
- `component_library`: name + usage (`adopt`/`extend`/`custom`) — decide it here with rationale; the implementing agents will bind these tokens into that library's theme layer
- `assets`: logo/wordmark if one exists, and one `reference-screenshot` entry per approved screenshot with `applies_to` naming the pages it governs
- `voice`: tone + empty-state guidance, lifted from the approved mocks' actual copy
- `theme`: modes (and generate a dark `:root` variant in the mocks first if dark mode is in scope — don't promise a mode no mock demonstrates)

Validate and render the styleguide:

```bash
python3 "$CW_HOME/scripts/extract_design.py" validate "$DESIGN_TMP/design.json"
python3 "$CW_HOME/scripts/extract_design.py" styleguide "$DESIGN_TMP/design.json" --out "$DESIGN_TMP/styleguide.html"
```

Validation must pass before anything is committed. Any decision you could not make (e.g. the user has no logo yet) is recorded as `TBD:` in the relevant `notes` field — `scripts/check_unresolved.py` will gate dependent frontend tickets on it, which is correct.

### Step 6: Commit to the target repo and report

Assemble and commit (directly to the default branch — design artifacts are docs, like `/architect`'s):

```
docs/design/
├── design.json        # binding tokens + component-library + assets + voice (ui-spec design format)
├── mockups/           # the approved direction's HTML mocks — living reference implementations
├── reference/         # screenshots of the approved mocks — the design-fidelity gate's baseline
└── styleguide.html    # rendered token sheet
```

The `reference-screenshot` asset paths in `design.json` must point at the committed `docs/design/reference/*.png` files, and `source.references` at `docs/design/mockups/`.

```bash
cd "$TARGET_DIR" && git add docs/design && git commit -m "design: add product design contract — <direction name>" && git push
```

Report to the user:
- The chosen direction and a one-line rationale
- The token table (colors, fonts) and component-library decision
- Critique findings folded in vs. noted
- Any `TBD:` markers left open (these will gate frontend tickets)
- What happens downstream: `/architect` folds `docs/design/design.json` into each epic's ui-spec `design` section; `/implement` Step 9 compares built screens against `docs/design/reference/` — the design the human actually approved, not a prose description of it.

## Key principles

- **Mockups-as-code, not image generation**: HTML mocks are renderable, diffable, token-extractable, and become the gate's baseline. An image mock can do none of that.
- **Divergence then choice, not iteration from one attempt**: one generated design converges to the model's default taste.
- **Tokens are extracted, not transcribed**: the contract cannot drift from the approved mock.
- **The orchestrator looks**: never present or commit a screenshot you haven't viewed yourself.
- **One human checkpoint**: taste (Step 3). Everything else runs autonomously.
