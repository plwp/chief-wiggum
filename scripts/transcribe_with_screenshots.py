#!/usr/bin/env python3
import argparse
import csv
import json
import os
import subprocess
import sys
from datetime import datetime, timezone


def format_time(seconds):
    seconds = max(0, int(seconds))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def run_ffmpeg_screenshot(video_path, timestamp, out_path):
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        str(timestamp),
        "-i",
        video_path,
        "-frames:v",
        "1",
        "-q:v",
        "2",
        out_path,
        "-y",
    ]
    subprocess.run(cmd, check=True)


def load_whisper():
    try:
        import whisper  # pylint: disable=import-error
    except Exception:
        whisper = None
    return whisper


def main():
    parser = argparse.ArgumentParser(
        description="Transcribe audio and extract cross-referenced screenshots."
    )
    parser.add_argument("--audio", required=True, help="Path to audio file.")
    parser.add_argument("--video", required=True, help="Path to video file.")
    parser.add_argument(
        "--out",
        required=True,
        help="Output directory for transcript and screenshots.",
    )
    parser.add_argument(
        "--model",
        default="base",
        help="Whisper model size (tiny, base, small, medium, large).",
    )
    parser.add_argument(
        "--min-gap",
        type=float,
        default=20.0,
        help="Minimum seconds between screenshots.",
    )
    parser.add_argument(
        "--max-screens",
        type=int,
        default=80,
        help="Maximum number of screenshots to extract.",
    )
    parser.add_argument(
        "--language",
        default=None,
        help="Optional language override (e.g. en).",
    )
    args = parser.parse_args()

    whisper = load_whisper()
    if whisper is None:
        print(
            "Missing dependency: openai-whisper.\n"
            "Install with: python3 -m pip install --user openai-whisper",
            file=sys.stderr,
        )
        sys.exit(1)

    ensure_dir(args.out)
    screenshots_dir = os.path.join(args.out, "screenshots")
    ensure_dir(screenshots_dir)

    model = whisper.load_model(args.model)
    result = model.transcribe(
        args.audio,
        fp16=False,
        language=args.language,
    )

    segments = result.get("segments", [])
    screenshot_map = {}
    last_shot_time = None
    screenshots = []
    for idx, segment in enumerate(segments):
        start_time = segment.get("start", 0)
        if last_shot_time is None or (start_time - last_shot_time) >= args.min_gap:
            if len(screenshots) >= args.max_screens:
                continue
            timestamp_label = format_time(start_time).replace(":", "-")
            filename = f"t_{timestamp_label}.jpg"
            out_path = os.path.join(screenshots_dir, filename)
            run_ffmpeg_screenshot(args.video, start_time, out_path)
            screenshot_map[idx] = os.path.relpath(out_path, args.out)
            screenshots.append((start_time, screenshot_map[idx]))
            last_shot_time = start_time

    meta = {
        "audio": os.path.abspath(args.audio),
        "video": os.path.abspath(args.video),
        "model": args.model,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    json_path = os.path.join(args.out, "transcript.json")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump({"meta": meta, "segments": segments}, handle, indent=2)

    csv_path = os.path.join(args.out, "transcript.csv")
    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["start", "end", "text", "screenshot"])
        for idx, segment in enumerate(segments):
            writer.writerow(
                [
                    format_time(segment.get("start", 0)),
                    format_time(segment.get("end", 0)),
                    (segment.get("text", "") or "").strip(),
                    screenshot_map.get(idx, ""),
                ]
            )

    md_path = os.path.join(args.out, "transcript_with_screenshots.md")
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write("# Interview transcript with screenshots\n\n")
        handle.write(f"- Audio: `{meta['audio']}`\n")
        handle.write(f"- Video: `{meta['video']}`\n")
        handle.write(f"- Model: `{meta['model']}`\n")
        handle.write(f"- Generated: `{meta['generated_at']}`\n\n")
        if screenshots:
            handle.write("## Screenshot index\n\n")
            for timestamp, path in screenshots:
                handle.write(f"- [{format_time(timestamp)}] `{path}`\n")
            handle.write("\n")
        handle.write("## Transcript\n\n")
        for idx, segment in enumerate(segments):
            start = format_time(segment.get("start", 0))
            end = format_time(segment.get("end", 0))
            text = (segment.get("text", "") or "").strip()
            handle.write(f"**[{start} - {end}]** {text}\n\n")
            shot = screenshot_map.get(idx)
            if shot:
                handle.write(f"![Screenshot {start}]({shot})\n\n")

    print(f"Transcript written to: {md_path}")


if __name__ == "__main__":
    main()
