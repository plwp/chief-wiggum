#!/usr/bin/env python3
"""
Keychain secret management for chief-wiggum.

Secrets are fetched from macOS Keychain on demand and passed directly to
API constructors. They are NEVER set as environment variables, NEVER printed,
and NEVER logged.

As a module:
    from keychain import get_secret, has_secret, set_secret
    api_key = get_secret("ANTHROPIC_API_KEY")  # returns str or None

As a CLI:
    python3 keychain.py set ANTHROPIC_API_KEY
    python3 keychain.py get ANTHROPIC_API_KEY     # prints status only, not the value
    python3 keychain.py delete ANTHROPIC_API_KEY
    python3 keychain.py list
"""

import getpass
import subprocess
import sys

SERVICE = "chief-wiggum"

KNOWN_KEYS = [
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
]


def get_secret(name: str) -> str | None:
    """Fetch a secret from macOS Keychain. Returns None if not found."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", name, "-s", SERVICE, "-w"],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def has_secret(name: str) -> bool:
    """Check if a secret exists in Keychain without retrieving it."""
    return get_secret(name) is not None


def set_secret(name: str, value: str) -> None:
    """Store a secret in macOS Keychain. Overwrites if exists."""
    # Delete existing entry (security errors on duplicate)
    subprocess.run(
        ["security", "delete-generic-password", "-a", name, "-s", SERVICE],
        capture_output=True, check=False,
    )
    subprocess.run(
        ["security", "add-generic-password", "-a", name, "-s", SERVICE, "-w", value],
        check=True,
    )


def delete_secret(name: str) -> bool:
    """Delete a secret from Keychain. Returns True if it existed."""
    result = subprocess.run(
        ["security", "delete-generic-password", "-a", name, "-s", SERVICE],
        capture_output=True, check=False,
    )
    return result.returncode == 0


def list_secrets() -> list[dict]:
    """Return status of all known keys (never the values)."""
    statuses = []
    for key in KNOWN_KEYS:
        statuses.append({
            "name": key,
            "in_keychain": has_secret(key),
        })
    return statuses


# --- CLI entrypoint ---

def main():
    if len(sys.argv) < 2:
        print("Usage: keychain.py <set|get|delete|list> [KEY_NAME]")
        print()
        print("Commands:")
        print("  set KEY_NAME      Store a key in macOS Keychain (prompts for value)")
        print("  get KEY_NAME      Check if a key exists (does NOT print the value)")
        print("  delete KEY_NAME   Remove a key from Keychain")
        print("  list              Show status of all known keys")
        print()
        print(f"Known keys: {', '.join(KNOWN_KEYS)}")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "list":
        print(f"=== Chief Wiggum Keychain (service: {SERVICE}) ===")
        print()
        for s in list_secrets():
            status = "[keychain]" if s["in_keychain"] else "[not set] "
            print(f"  {status}  {s['name']}")
        return

    if len(sys.argv) < 3:
        print(f"Usage: keychain.py {cmd} KEY_NAME", file=sys.stderr)
        sys.exit(1)

    key_name = sys.argv[2]

    if cmd == "set":
        value = getpass.getpass(f"Enter value for {key_name}: ")
        if not value:
            print("Error: empty value", file=sys.stderr)
            sys.exit(1)
        set_secret(key_name, value)
        print(f"Stored {key_name} in keychain (service: {SERVICE})")

    elif cmd == "get":
        if has_secret(key_name):
            print(f"{key_name}: stored in keychain")
        else:
            print(f"{key_name}: not found")
            sys.exit(1)

    elif cmd == "delete":
        if delete_secret(key_name):
            print(f"Deleted {key_name} from keychain")
        else:
            print(f"{key_name} not found in keychain")
            sys.exit(1)

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
