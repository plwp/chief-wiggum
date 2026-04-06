# Keep Going — Session Keep-Alive

Prevents Claude Code sessions from going idle during long-running workflows by scheduling a periodic nudge.

## Usage
```
/keep-going [interval]
```

## Parameters
- `interval` (optional): How often to nudge. Default: `55m`. Accepts: `Nm` (minutes), `Nh` (hours). Examples: `30m`, `1h`, `45m`.

## Behavior

When invoked, schedule a recurring cron job that sends "keep going" at the specified interval. This keeps the session alive during long background tasks (AI consultations, parallel implementations, etc.).

### Step 1: Parse interval

Parse the argument as an interval:
- If no argument, default to `55m`
- Accept `Nm` or `Nh` format
- Round to the nearest clean cron interval if needed (e.g. `55m` → `60m`)

### Step 2: Schedule

Use CronCreate with:
- `cron`: appropriate expression (pick an off-minute, not :00 or :30, to avoid fleet thundering herd)
- `prompt`: `keep going`
- `recurring`: `true`

### Step 3: Confirm

Tell the user:
- What's scheduled and at what cadence
- The job ID (for cancelling with CronDelete)
- That it auto-expires after 7 days
- That it's session-only (dies when Claude exits)

## Notes

- This is a lightweight keep-alive, not a polling mechanism. It just prevents session timeout.
- For actual recurring work, use `/loop` instead.
- The nudge only fires when the REPL is idle — it won't interrupt active work.
