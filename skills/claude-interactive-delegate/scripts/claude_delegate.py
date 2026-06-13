#!/usr/bin/env python3
"""Drive an interactive Claude Code session through tmux with file handoff."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

DEFAULT_SESSION = "cw-claude"
DEFAULT_TASK_ROOT = Path.home() / ".chief-wiggum" / "delegates" / "claude"
DEFAULT_TIMEOUT_SECONDS = 30 * 60
DEFAULT_CLAUDE_CMD = os.environ.get(
    "CLAUDE_DELEGATE_CMD",
    "claude --dangerously-skip-permissions",
)


def require_tool(name: str) -> None:
    if not shutil.which(name):
        raise SystemExit(
            f"Missing required tool: {name}. Install it and retry. "
            f"On macOS: brew install {name}"
        )


def run(cmd: list[str], *, check: bool = True, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        input=input_text,
        text=True,
        capture_output=True,
        check=check,
    )


def has_session(session: str) -> bool:
    result = run(["tmux", "has-session", "-t", session], check=False)
    return result.returncode == 0


def start_session(session: str, claude_cmd: str) -> None:
    require_tool("tmux")
    require_tool("claude")
    if has_session(session):
        print(f"OK: tmux session already exists: {session}")
        return
    run(["tmux", "new-session", "-d", "-s", session, claude_cmd])
    print(f"OK: started tmux session: {session}")


def send_text(session: str, text: str) -> None:
    if not has_session(session):
        raise SystemExit(f"tmux session not found: {session}. Run start first.")
    buffer_name = f"cw-{uuid.uuid4().hex}"
    run(["tmux", "load-buffer", "-b", buffer_name, "-"], input_text=text)
    run(["tmux", "paste-buffer", "-d", "-b", buffer_name, "-t", session])
    run(["tmux", "send-keys", "-t", session, "Enter"])


def make_task_dir(task_root: Path, task_id: str | None) -> tuple[str, Path]:
    task_id = task_id or time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    task_dir = (task_root / task_id).expanduser().resolve()
    task_dir.mkdir(parents=True, exist_ok=False)
    return task_id, task_dir


def build_delegate_message(task_dir: Path, cwd: str | None) -> str:
    prompt_path = task_dir / "prompt.md"
    result_path = task_dir / "result.md"
    done_path = task_dir / "DONE"
    error_path = task_dir / "ERROR"
    cwd_line = f"\nWork from this repository/directory when relevant:\n{Path(cwd).expanduser().resolve()}\n" if cwd else ""
    return f"""Delegated Chief Wiggum task.

The operator has authorized this interactive Claude Code session to handle bounded delegated engineering tasks from Codex.

Read the task prompt:
{prompt_path}
{cwd_line}
Write your final answer as Markdown to:
{result_path}

When complete, run:
touch {done_path}

If you are blocked, write a concise reason to:
{error_path}

Then stop working on this delegated task.

Important boundaries:
- Do not answer login, billing, subscription, payment, or API-credit consent prompts. If one appears, write ERROR if possible and stop.
- Do not create, push, merge, or close PRs unless the prompt explicitly asks for it.
- Be concrete and cite files/commands you inspected.
"""


def submit(args: argparse.Namespace) -> int:
    start_session(args.session, args.claude_cmd)
    task_root = Path(args.task_root).expanduser()
    task_id, task_dir = make_task_dir(task_root, args.task_id)

    if args.prompt_file:
        prompt = Path(args.prompt_file).expanduser().read_text()
    elif args.prompt:
        prompt = args.prompt
    else:
        raise SystemExit("submit requires --prompt-file or --prompt")

    (task_dir / "prompt.md").write_text(prompt)
    send_text(args.session, build_delegate_message(task_dir, args.cwd))

    print(f"TASK_ID={task_id}")
    print(f"TASK_DIR={task_dir}")
    print(f"PROMPT={task_dir / 'prompt.md'}")
    print(f"RESULT={task_dir / 'result.md'}")
    print(f"DONE={task_dir / 'DONE'}")
    print(f"ERROR={task_dir / 'ERROR'}")

    if args.wait:
        return wait_for_task(task_dir, args.timeout_seconds)
    return 0


def wait_for_task(task_dir: Path, timeout_seconds: int) -> int:
    deadline = time.time() + timeout_seconds
    done = task_dir / "DONE"
    error = task_dir / "ERROR"
    while time.time() < deadline:
        if done.exists():
            print(f"OK: task complete: {task_dir}")
            result = task_dir / "result.md"
            if result.exists():
                print(f"RESULT={result}")
            return 0
        if error.exists():
            print(f"ERROR: task blocked: {task_dir}", file=sys.stderr)
            print(error.read_text(), file=sys.stderr)
            return 2
        time.sleep(2)
    print(f"TIMEOUT: no DONE or ERROR after {timeout_seconds}s: {task_dir}", file=sys.stderr)
    return 3


def status(args: argparse.Namespace) -> int:
    require_tool("tmux")
    if has_session(args.session):
        print(f"OK: tmux session exists: {args.session}")
        return 0
    print(f"MISSING: tmux session not found: {args.session}")
    return 1


def stop(args: argparse.Namespace) -> int:
    require_tool("tmux")
    if not has_session(args.session):
        print(f"OK: tmux session already absent: {args.session}")
        return 0
    run(["tmux", "kill-session", "-t", args.session])
    print(f"OK: stopped tmux session: {args.session}")
    return 0


def capture(args: argparse.Namespace) -> int:
    require_tool("tmux")
    if not has_session(args.session):
        print(f"MISSING: tmux session not found: {args.session}", file=sys.stderr)
        return 1
    result = run(["tmux", "capture-pane", "-pt", args.session, "-S", str(args.lines * -1)])
    print(result.stdout)
    return 0


def attach(args: argparse.Namespace) -> int:
    require_tool("tmux")
    os.execvp("tmux", ["tmux", "attach", "-t", args.session])
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", default=DEFAULT_SESSION, help="tmux session name")
    parser.add_argument("--task-root", default=str(DEFAULT_TASK_ROOT), help="delegate task root")
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="start the interactive Claude session")
    start.add_argument("--claude-cmd", default=DEFAULT_CLAUDE_CMD, help="command to run in tmux")

    sub.add_parser("status", help="check whether the tmux session exists")
    sub.add_parser("stop", help="stop the tmux session")

    submit_parser = sub.add_parser("submit", help="submit a delegated task")
    submit_parser.add_argument("--prompt-file", help="path to task prompt")
    submit_parser.add_argument("--prompt", help="inline task prompt")
    submit_parser.add_argument("--cwd", help="target repo/directory for Claude to use")
    submit_parser.add_argument("--task-id", help="stable task ID")
    submit_parser.add_argument("--claude-cmd", default=DEFAULT_CLAUDE_CMD, help="command to run if session starts")
    submit_parser.add_argument("--wait", action="store_true", help="wait for DONE or ERROR")
    submit_parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)

    wait_parser = sub.add_parser("wait", help="wait for a submitted task")
    wait_parser.add_argument("--task-id", required=True)
    wait_parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)

    capture_parser = sub.add_parser("capture", help="print recent tmux pane output")
    capture_parser.add_argument("--lines", type=int, default=120)

    sub.add_parser("attach", help="attach to the tmux session")

    args = parser.parse_args()
    if args.command == "start":
        start_session(args.session, args.claude_cmd)
        return 0
    if args.command == "status":
        return status(args)
    if args.command == "stop":
        return stop(args)
    if args.command == "submit":
        return submit(args)
    if args.command == "wait":
        return wait_for_task(Path(args.task_root).expanduser() / args.task_id, args.timeout_seconds)
    if args.command == "capture":
        return capture(args)
    if args.command == "attach":
        return attach(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
