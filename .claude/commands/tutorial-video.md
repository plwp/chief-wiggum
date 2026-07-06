# Tutorial Video - Narrated Click-Through Production

Produce a narrated tutorial video for a feature of the target repo: write a
narration script, record a click-through of the running app with a visible
cursor, generate voice narration, and assemble them into an `.mp4` with an
`.srt` caption sidecar.

## Usage
```
/tutorial-video owner/repo --feature "Owner sets pricing" [--url http://localhost:3000] [--engine auto|openai|say] [--voice alloy]
```

## Parameters
- `owner/repo`: Target repository
- `--feature`: What the tutorial should teach (a user-facing flow, not a code path)
- `--url`: Base URL of the running app. If omitted, start the target repo's dev server yourself — never ask the user to start it
- `--engine`: TTS engine. `auto` (default) uses `openai` when `OPENAI_API_KEY` is in the keyring, else falls back to the offline macOS `say` engine
- `--voice`: Narration voice (see `models.md` → Text-to-Speech)

## Output

Everything lands in `docs/tutorials/<slug>/` in the target repo:

```
docs/tutorials/owner-sets-pricing/
├── script.md          # Human-readable narration script (committed)
├── storyboard.json    # Scene-by-scene narration + actions — the source of truth (committed)
├── tutorial.mp4       # Final narrated video (commit only if the repo handles binaries, e.g. LFS)
└── tutorial.srt       # Captions (committed)
```

The storyboard is the durable artifact: anyone can regenerate the video from
it with one command, so it must stay accurate as the UI evolves.

## Workflow

### Step 0: Resolve environment

```bash
CW_HOME="${CHIEF_WIGGUM_HOME:-$HOME/repos/chief-wiggum}"
CW_HOME=$(python3 "$CW_HOME/scripts/env.py" home)
CW_TMP=$(python3 "$CW_HOME/scripts/env.py" tmp)
TARGET=$(python3 "$CW_HOME/scripts/repo.py" resolve owner/repo)
python3 "$CW_HOME/scripts/check_deps.py" --for tutorial-video
```

If dependencies are missing, install them (`python3 "$CW_HOME/scripts/install_deps.py"`)
— do not punt to the user.

### Step 1: Get the app running

A tutorial records the real app, never mockups.

- If `--url` was given, verify it responds (`curl -sI <url>`).
- Otherwise, find how the target repo starts its dev server (README, `package.json`
  scripts, `docker-compose.yml`, Makefile) and start it. Seed demo data if the
  flow needs it (a tutorial of empty screens teaches nothing).
- Confirm the app is actually serving the feature before writing anything.

### Step 2: Learn the flow

Read the feature's ground truth before scripting it:

- `docs/epics/*/` contracts and ui-specs for the feature, if they exist
- `docs/design/design.json` for product voice and terminology
- The live app itself: walk the flow once and note the exact selectors,
  labels, and page states at each step

Never guess a selector. Every selector in the storyboard must be one you have
observed in the running app. Prefer user-facing selectors (`text=`, roles,
labels) over brittle CSS paths.

### Step 3: Write the script and storyboard

Write two files in `$CW_TMP/tutorial-<slug>/`:

**`script.md`** — the narration script for human review: one section per scene
with the narration text and a one-line description of what is on screen.

Narration guidelines:
- Second person, present tense: "Click **Save** to publish your prices."
- 2–4 short sentences per scene; a scene's narration should run 8–20 seconds
- Explain *why* the user does something, not just *what* to click
- Match the product's own terminology (from `design.json` voice, if present)
- No filler ("simply", "just", "as you can see")

**`storyboard.json`** — the executable version. Schema:

```json
{
  "title": "Owner sets pricing",
  "base_url": "http://localhost:3000",
  "viewport": {"width": 1280, "height": 720},
  "scenes": [
    {
      "id": "open-pricing",
      "title": "Open the pricing page",
      "narration": "From your dashboard, open Settings and choose Pricing.",
      "actions": [
        {"type": "goto", "url": "/"},
        {"type": "click", "selector": "text=Settings"},
        {"type": "click", "selector": "text=Pricing"},
        {"type": "wait_for", "selector": "h1:has-text('Pricing')"}
      ]
    }
  ]
}
```

Action types: `goto` (url), `click`/`hover`/`wait_for` (selector), `fill`
(selector, value), `press` (selector, key), `select` (selector, value),
`scroll` (selector or y), `wait` (seconds). The first action of the first
scene must be a `goto`.

Validate the schema:

```bash
python3 "$CW_HOME/scripts/tutorial_video.py" validate "$CW_TMP/tutorial-<slug>/storyboard.json"
```

### Step 4: Dry-run the storyboard

Execute every action against the live app without recording:

```bash
python3 "$CW_HOME/scripts/tutorial_video.py" record "$CW_TMP/tutorial-<slug>/storyboard.json" \
  --out "$CW_TMP/tutorial-<slug>/dry" --dry-run
```

If any action fails, fix the storyboard (or the app-start / seed-data step)
and dry-run again. Do not proceed to production with a failing action —
validate before acting.

### Step 5: Produce

```bash
python3 "$CW_HOME/scripts/tutorial_video.py" produce "$CW_TMP/tutorial-<slug>/storyboard.json" \
  --out-dir "$CW_TMP/tutorial-<slug>/out"
```

This narrates each scene (TTS), records the click-through paced so every scene
lasts at least as long as its narration, and assembles `tutorial.mp4` +
`tutorial.srt`. If the resolved engine was the offline `say` fallback, note
the voice downgrade in your summary.

### Step 6: Verify the video — not negotiable

Never ship a video you have not looked at. Verify it yourself:

```bash
python3 "$CW_HOME/scripts/tutorial_video.py" probe "$CW_TMP/tutorial-<slug>/out/tutorial.mp4" \
  --frames-dir "$CW_TMP/tutorial-<slug>/frames" --frame-interval 5
```

Check all of:
1. **Audio stream exists** and total duration ≈ sum of scene narration durations
2. **Read the extracted frames** and compare each against the storyboard: is
   the right screen visible during each scene? Any error pages, empty states,
   loading spinners, or dev-tool artifacts?
3. **Captions**: `tutorial.srt` cue times fall within the video duration and
   texts match the script

If a frame shows the wrong state, fix the storyboard (usually a missing
`wait_for`) and re-run Step 5. Ask yourself: would I be proud to publish this
video? If not, fix it.

### Step 7: Deliver

1. Copy `script.md`, `storyboard.json`, `tutorial.mp4`, and `tutorial.srt`
   into `docs/tutorials/<slug>/` in the target repo.
2. Commit `script.md`, `storyboard.json`, and `tutorial.srt` on a branch and
   open a PR (never commit directly to main). Include the `.mp4` only if the
   repo already tracks binaries (e.g. git LFS); otherwise report its local
   path in the PR body and summary.
3. Summarize: feature covered, scene count, video duration, engine/voice
   used, and anything the user should re-record later (e.g. seeded demo data
   visible on screen).
