#!/usr/bin/env python3
"""Produce a narrated click-through tutorial video from a storyboard.

The storyboard is a JSON file: an ordered list of scenes, each pairing
narration text with browser actions. Production is three deterministic stages,
each runnable on its own or together via ``produce``:

  narrate   TTS each scene's narration to an audio file (OpenAI TTS via the
            system keyring, or the offline macOS ``say`` engine) and record
            per-scene durations in a manifest.
  record    Drive the flow with Playwright in one continuously-recorded page,
            pacing each scene so it lasts at least as long as its narration,
            and write scene start/end markers.
  assemble  Overlay each scene's narration onto the recording at its marker
            offset with ffmpeg, encode an .mp4, and write an .srt sidecar.

Sync strategy: narration is generated *first* so the recorder knows how long
to hold each scene, then audio is placed at the measured scene-start offsets.
Nothing is trimmed, so action timing inside a scene is preserved exactly.

Usage:
    python3 tutorial_video.py validate storyboard.json
    python3 tutorial_video.py narrate storyboard.json --out out/narration
    python3 tutorial_video.py record storyboard.json --out out/recording \
        [--narration out/narration] [--dry-run] [--headed]
    python3 tutorial_video.py assemble storyboard.json --workdir out --out out/tutorial.mp4
    python3 tutorial_video.py produce storyboard.json --out-dir out
    python3 tutorial_video.py probe out/tutorial.mp4 [--frames-dir dir] [--frame-interval 5]

Secrets: the OpenAI engine fetches OPENAI_API_KEY from the system keyring at
call time and passes it directly in the request header. It is never placed in
an environment variable, never printed, never logged.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

DEFAULT_VIEWPORT = {"width": 1280, "height": 720}
DEFAULT_PACE = 0.9  # seconds between actions, so viewers can follow
SCENE_SETTLE = 0.6  # seconds after page open before the first scene
NARRATION_TAIL = 0.7  # seconds of quiet held after narration ends
OPENAI_TTS_URL = "https://api.openai.com/v1/audio/speech"
DEFAULT_TTS_MODEL = "gpt-4o-mini-tts"
DEFAULT_TTS_VOICE = "alloy"

ACTION_TYPES = {
    "goto": {"url"},
    "click": {"selector"},
    "fill": {"selector", "value"},
    "press": {"selector", "key"},
    "hover": {"selector"},
    "select": {"selector", "value"},
    "scroll": set(),  # needs "selector" or "y"
    "wait": {"seconds"},
    "wait_for": {"selector"},
}

# Visible cursor + click ripple, injected before any page script so the
# recording shows where the mouse is. Playwright's synthetic input fires real
# mousemove/mousedown events, which this listens for at the document level.
CURSOR_OVERLAY_JS = """
(() => {
  const ensure = () => {
    if (!document.body || document.getElementById('__cw_cursor')) return;
    const c = document.createElement('div');
    c.id = '__cw_cursor';
    c.style.cssText = [
      'position:fixed', 'z-index:2147483647', 'width:22px', 'height:22px',
      'border-radius:50%', 'background:rgba(255,90,60,0.40)',
      'border:2px solid rgba(255,90,60,0.95)', 'pointer-events:none',
      'transform:translate(-50%,-50%)', 'left:-100px', 'top:-100px',
      'transition:left 0.06s linear, top 0.06s linear',
    ].join(';');
    document.body.appendChild(c);
  };
  document.addEventListener('mousemove', (e) => {
    ensure();
    const c = document.getElementById('__cw_cursor');
    if (c) { c.style.left = e.clientX + 'px'; c.style.top = e.clientY + 'px'; }
  }, true);
  document.addEventListener('mousedown', () => {
    ensure();
    const c = document.getElementById('__cw_cursor');
    if (!c) return;
    c.style.background = 'rgba(255,90,60,0.85)';
    setTimeout(() => { c.style.background = 'rgba(255,90,60,0.40)'; }, 250);
  }, true);
  if (document.readyState !== 'loading') ensure();
  else document.addEventListener('DOMContentLoaded', ensure);
})();
"""


# --- storyboard ---------------------------------------------------------------


def load_storyboard(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def validate_storyboard(board: dict) -> list[str]:
    """Return a list of human-readable schema errors (empty when valid)."""
    errors: list[str] = []
    if not isinstance(board, dict):
        return ["storyboard must be a JSON object"]
    if not (board.get("title") or "").strip():
        errors.append("missing top-level 'title'")
    scenes = board.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        errors.append("'scenes' must be a non-empty list")
        return errors
    seen_ids: set[str] = set()
    for i, scene in enumerate(scenes):
        where = f"scenes[{i}]"
        if not isinstance(scene, dict):
            errors.append(f"{where} must be an object")
            continue
        sid = scene.get("id")
        if not sid or not isinstance(sid, str):
            errors.append(f"{where} missing string 'id'")
        elif sid in seen_ids:
            errors.append(f"{where} duplicate scene id '{sid}'")
        else:
            seen_ids.add(sid)
        if not (scene.get("narration") or "").strip():
            errors.append(f"{where} missing 'narration' text")
        actions = scene.get("actions")
        if not isinstance(actions, list) or not actions:
            errors.append(f"{where} 'actions' must be a non-empty list")
            continue
        for j, action in enumerate(actions):
            errors.extend(validate_action(action, f"{where}.actions[{j}]"))
    first_actions = scenes[0].get("actions") if isinstance(scenes[0], dict) else None
    if isinstance(first_actions, list) and first_actions:
        first = first_actions[0]
        if isinstance(first, dict) and first.get("type") != "goto":
            errors.append("scenes[0].actions[0] should be a 'goto' so the recording starts on a page")
    return errors


def validate_action(action: dict, where: str) -> list[str]:
    if not isinstance(action, dict):
        return [f"{where} must be an object"]
    atype = action.get("type")
    if atype not in ACTION_TYPES:
        return [f"{where} unknown action type '{atype}' (known: {', '.join(sorted(ACTION_TYPES))})"]
    errors = [
        f"{where} ({atype}) missing '{field}'"
        for field in ACTION_TYPES[atype]
        if field not in action
    ]
    if atype == "scroll" and "selector" not in action and "y" not in action:
        errors.append(f"{where} (scroll) needs 'selector' or 'y'")
    if atype == "wait" and not isinstance(action.get("seconds"), (int, float)):
        errors.append(f"{where} (wait) 'seconds' must be a number")
    return errors


def resolve_url(url: str, base_url: str | None) -> str:
    """Resolve a possibly-relative storyboard URL against the base URL."""
    if urllib.parse.urlparse(url).scheme:
        return url
    if not base_url:
        raise ValueError(f"relative url '{url}' needs a top-level 'base_url'")
    return urllib.parse.urljoin(base_url.rstrip("/") + "/", url.lstrip("/"))


# --- narration ----------------------------------------------------------------


def synthesize_openai(text: str, out_path: Path, model: str, voice: str) -> None:
    from keychain import get_secret

    api_key = get_secret("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit(
            "OPENAI_API_KEY not in keyring. Store it with:\n"
            "  python3 scripts/keychain.py set OPENAI_API_KEY\n"
            "or use the offline engine: --engine say"
        )
    payload = json.dumps(
        {"model": model, "voice": voice, "input": text, "response_format": "mp3"}
    ).encode("utf-8")
    request = urllib.request.Request(
        OPENAI_TTS_URL,
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            out_path.write_bytes(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise SystemExit(f"OpenAI TTS failed ({exc.code}): {detail}") from exc


def synthesize_say(text: str, out_path: Path, voice: str | None) -> None:
    """Offline macOS fallback: `say` renders AIFF, ffmpeg converts to WAV."""
    if not shutil.which("say"):
        raise SystemExit("--engine say requires macOS (`say` not found); use --engine openai")
    aiff = out_path.with_suffix(".aiff")
    cmd = ["say", "-o", str(aiff)]
    if voice:
        cmd += ["-v", voice]
    subprocess.run(cmd + [text], check=True)
    run_ffmpeg(["-i", str(aiff), "-ar", "44100", str(out_path)])
    aiff.unlink()


def resolve_engine(engine: str) -> str:
    """Resolve --engine auto: OpenAI when the key is in the keyring, else `say`."""
    if engine != "auto":
        return engine
    from keychain import has_secret

    if has_secret("OPENAI_API_KEY"):
        return "openai"
    if shutil.which("say"):
        print("  engine auto: OPENAI_API_KEY not in keyring, using offline `say`")
        return "say"
    raise SystemExit(
        "No TTS engine available: OPENAI_API_KEY is not in the keyring and "
        "`say` is not on this platform. Store a key with:\n"
        "  python3 scripts/keychain.py set OPENAI_API_KEY"
    )


def cmd_narrate(args) -> None:
    board = load_storyboard(args.storyboard)
    require_valid(board)
    engine = resolve_engine(args.engine)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, dict] = {}
    for scene in board["scenes"]:
        text = scene["narration"].strip()
        if engine == "openai":
            audio = out_dir / f"scene-{scene['id']}.mp3"
            synthesize_openai(text, audio, args.tts_model, args.voice or DEFAULT_TTS_VOICE)
        else:
            audio = out_dir / f"scene-{scene['id']}.wav"
            synthesize_say(text, audio, args.voice)
        duration = ffprobe_duration(audio)
        manifest[scene["id"]] = {"file": audio.name, "duration": duration}
        print(f"  narrated {scene['id']}: {duration:.1f}s ({audio.name})")
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Narration manifest: {manifest_path}")


# --- recording ----------------------------------------------------------------


def scene_hold_seconds(elapsed: float, narration_duration: float, tail: float = NARRATION_TAIL) -> float:
    """How long to keep holding a scene so it covers its narration."""
    return max(0.0, narration_duration + tail - elapsed)


def run_action(page, action: dict, base_url: str | None, pace: float) -> None:
    atype = action["type"]
    if atype == "goto":
        page.goto(resolve_url(action["url"], base_url), wait_until="load")
    elif atype == "click":
        locator = page.locator(action["selector"]).first
        locator.hover()
        page.wait_for_timeout(300)
        locator.click(delay=120)
    elif atype == "fill":
        locator = page.locator(action["selector"]).first
        locator.click()
        locator.fill("")  # replace, don't append to, any existing value
        locator.press_sequentially(str(action["value"]), delay=45)
    elif atype == "press":
        page.locator(action["selector"]).first.press(action["key"])
    elif atype == "hover":
        page.locator(action["selector"]).first.hover()
    elif atype == "select":
        page.locator(action["selector"]).first.select_option(str(action["value"]))
    elif atype == "scroll":
        if "selector" in action:
            page.locator(action["selector"]).first.scroll_into_view_if_needed()
        else:
            page.mouse.wheel(0, float(action["y"]))
    elif atype == "wait":
        page.wait_for_timeout(float(action["seconds"]) * 1000)
    elif atype == "wait_for":
        page.locator(action["selector"]).first.wait_for(
            state=action.get("state", "visible"),
            timeout=float(action.get("timeout", 15)) * 1000,
        )
    page.wait_for_timeout(pace * 1000)


def cmd_record(args) -> None:
    board = load_storyboard(args.storyboard)
    require_valid(board)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit(
            "playwright not installed. Install with:\n"
            "  python3 scripts/install_deps.py --for tutorial-video"
        )

    durations: dict[str, float] = {}
    if args.narration:
        manifest = json.loads((Path(args.narration) / "manifest.json").read_text(encoding="utf-8"))
        durations = {sid: entry["duration"] for sid, entry in manifest.items()}

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    viewport = board.get("viewport") or DEFAULT_VIEWPORT
    pace = float(board.get("pace", DEFAULT_PACE))
    base_url = board.get("base_url")
    markers: list[dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        context_kwargs: dict = {"viewport": viewport}
        if not args.dry_run:
            context_kwargs["record_video_dir"] = str(out_dir)
            context_kwargs["record_video_size"] = viewport
        context = browser.new_context(**context_kwargs)
        context.add_init_script(CURSOR_OVERLAY_JS)
        page = context.new_page()
        t0 = time.monotonic()
        page.wait_for_timeout(SCENE_SETTLE * 1000)

        for scene in board["scenes"]:
            start = time.monotonic() - t0
            print(f"  scene {scene['id']} @ {start:.1f}s")
            for action in scene["actions"]:
                run_action(page, action, base_url, 0.1 if args.dry_run else pace)
            if not args.dry_run:
                elapsed = (time.monotonic() - t0) - start
                hold = scene_hold_seconds(elapsed, durations.get(scene["id"], 0.0))
                if hold:
                    page.wait_for_timeout(hold * 1000)
            markers.append(
                {
                    "id": scene["id"],
                    "title": scene.get("title", scene["id"]),
                    "start": round(start, 3),
                    "end": round(time.monotonic() - t0, 3),
                    "narration_duration": durations.get(scene["id"], 0.0),
                }
            )

        video = page.video
        context.close()  # flushes the recording to disk
        browser.close()
        if not args.dry_run and video:
            recording = out_dir / "recording.webm"
            Path(video.path()).rename(recording)
            print(f"Recording: {recording}")

    (out_dir / "markers.json").write_text(json.dumps(markers, indent=2), encoding="utf-8")
    print(f"Markers: {out_dir / 'markers.json'}")
    if args.dry_run:
        print("Dry run OK: every action executed against the live app.")


# --- assembly -----------------------------------------------------------------


def build_audio_filter(offsets_ms: list[int]) -> str:
    """ffmpeg filter_complex placing narration inputs 1..N at their offsets."""
    chains = [
        f"[{i + 1}:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,"
        f"adelay={ms}:all=1[a{i + 1}]"
        for i, ms in enumerate(offsets_ms)
    ]
    labels = "".join(f"[a{i + 1}]" for i in range(len(offsets_ms)))
    if len(offsets_ms) == 1:
        return f"{chains[0]};[a1]anull[aout]"
    mix = f"{labels}amix=inputs={len(offsets_ms)}:duration=longest:normalize=0[aout]"
    return ";".join(chains) + ";" + mix


def srt_timestamp(seconds: float) -> str:
    ms = max(0, int(round(seconds * 1000)))
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def build_srt(markers: list[dict], narration_by_id: dict[str, str]) -> str:
    cues = []
    for n, marker in enumerate(markers, start=1):
        start = marker["start"]
        end = start + (marker["narration_duration"] or (marker["end"] - marker["start"]))
        text = narration_by_id.get(marker["id"], marker.get("title", "")).strip()
        cues.append(f"{n}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n{text}\n")
    return "\n".join(cues)


def cmd_assemble(args) -> None:
    board = load_storyboard(args.storyboard)
    require_valid(board)
    workdir = Path(args.workdir)
    narration_dir = workdir / "narration"
    recording_dir = workdir / "recording"
    recording = recording_dir / "recording.webm"
    if not recording.is_file():
        raise SystemExit(f"missing {recording} — run `record` first")
    manifest = json.loads((narration_dir / "manifest.json").read_text(encoding="utf-8"))
    markers = json.loads((recording_dir / "markers.json").read_text(encoding="utf-8"))

    audio_inputs: list[str] = []
    offsets_ms: list[int] = []
    for marker in markers:
        entry = manifest.get(marker["id"])
        if not entry:
            raise SystemExit(f"scene '{marker['id']}' has no narration — re-run `narrate`")
        audio_inputs.append(str(narration_dir / entry["file"]))
        offsets_ms.append(int(marker["start"] * 1000))

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["-i", str(recording)]
    for audio in audio_inputs:
        cmd += ["-i", audio]
    cmd += [
        "-filter_complex", build_audio_filter(offsets_ms),
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(out_path),
    ]
    run_ffmpeg(cmd)

    narration_by_id = {s["id"]: s["narration"] for s in board["scenes"]}
    srt_path = out_path.with_suffix(".srt")
    srt_path.write_text(build_srt(markers, narration_by_id), encoding="utf-8")
    print(f"Video: {out_path}")
    print(f"Captions: {srt_path}")


def cmd_produce(args) -> None:
    out_dir = Path(args.out_dir)
    narrate_args = argparse.Namespace(
        storyboard=args.storyboard, out=str(out_dir / "narration"),
        engine=args.engine, voice=args.voice, tts_model=args.tts_model,
    )
    record_args = argparse.Namespace(
        storyboard=args.storyboard, out=str(out_dir / "recording"),
        narration=str(out_dir / "narration"), headed=args.headed, dry_run=False,
    )
    assemble_args = argparse.Namespace(
        storyboard=args.storyboard, workdir=str(out_dir),
        out=str(out_dir / "tutorial.mp4"),
    )
    print("[1/3] narrate")
    cmd_narrate(narrate_args)
    print("[2/3] record")
    cmd_record(record_args)
    print("[3/3] assemble")
    cmd_assemble(assemble_args)


# --- probing / QA ---------------------------------------------------------


def cmd_probe(args) -> None:
    info = json.loads(
        subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", args.video],
            capture_output=True, text=True, check=True,
        ).stdout
    )
    duration = float(info["format"]["duration"])
    print(f"duration: {duration:.1f}s")
    for stream in info["streams"]:
        if stream["codec_type"] == "video":
            print(f"video: {stream['codec_name']} {stream['width']}x{stream['height']}")
        elif stream["codec_type"] == "audio":
            print(f"audio: {stream['codec_name']} {stream.get('sample_rate')}Hz")
    if not any(s["codec_type"] == "audio" for s in info["streams"]):
        raise SystemExit("FAIL: no audio stream — narration missing")

    if args.frames_dir:
        frames = Path(args.frames_dir)
        frames.mkdir(parents=True, exist_ok=True)
        run_ffmpeg([
            "-i", args.video, "-vf", f"fps=1/{args.frame_interval}",
            "-q:v", "2", str(frames / "frame-%03d.jpg"),
        ])
        count = len(list(frames.glob("frame-*.jpg")))
        print(f"frames: {count} extracted to {frames} — review them against the storyboard")


# --- shared helpers -----------------------------------------------------------


def require_valid(board: dict) -> None:
    errors = validate_storyboard(board)
    if errors:
        for error in errors:
            print(f"  INVALID: {error}", file=sys.stderr)
        raise SystemExit(f"storyboard has {len(errors)} schema error(s)")


def run_ffmpeg(cmd_args: list[str]) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *cmd_args],
        check=True,
    )


def ffprobe_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def cmd_validate(args) -> None:
    require_valid(load_storyboard(args.storyboard))
    print("storyboard OK")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="Check storyboard schema")
    p_validate.add_argument("storyboard")
    p_validate.set_defaults(func=cmd_validate)

    p_narrate = sub.add_parser("narrate", help="Generate narration audio per scene")
    p_narrate.add_argument("storyboard")
    p_narrate.add_argument("--out", required=True)
    p_narrate.add_argument("--engine", choices=["auto", "openai", "say"], default="auto")
    p_narrate.add_argument("--voice", default=None)
    p_narrate.add_argument("--tts-model", default=DEFAULT_TTS_MODEL)
    p_narrate.set_defaults(func=cmd_narrate)

    p_record = sub.add_parser("record", help="Record the click-through with Playwright")
    p_record.add_argument("storyboard")
    p_record.add_argument("--out", required=True)
    p_record.add_argument("--narration", default=None, help="Narration dir (for pacing)")
    p_record.add_argument("--dry-run", action="store_true", help="Execute actions without video")
    p_record.add_argument("--headed", action="store_true")
    p_record.set_defaults(func=cmd_record)

    p_assemble = sub.add_parser("assemble", help="Mux narration over the recording")
    p_assemble.add_argument("storyboard")
    p_assemble.add_argument("--workdir", required=True, help="Dir holding narration/ and recording/")
    p_assemble.add_argument("--out", required=True)
    p_assemble.set_defaults(func=cmd_assemble)

    p_produce = sub.add_parser("produce", help="narrate + record + assemble")
    p_produce.add_argument("storyboard")
    p_produce.add_argument("--out-dir", required=True)
    p_produce.add_argument("--engine", choices=["auto", "openai", "say"], default="auto")
    p_produce.add_argument("--voice", default=None)
    p_produce.add_argument("--tts-model", default=DEFAULT_TTS_MODEL)
    p_produce.add_argument("--headed", action="store_true")
    p_produce.set_defaults(func=cmd_produce)

    p_probe = sub.add_parser("probe", help="Inspect the final video for QA")
    p_probe.add_argument("video")
    p_probe.add_argument("--frames-dir", default=None)
    p_probe.add_argument("--frame-interval", type=int, default=5)
    p_probe.set_defaults(func=cmd_probe)

    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
