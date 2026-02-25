#!/usr/bin/env python3
"""
Keychain secret management for chief-wiggum.

Uses the `keyring` library for secure cross-platform secret storage.
On macOS this uses Keychain, on Linux it uses SecretService/KWallet.

Secrets are fetched on demand and passed directly to API constructors.
They are NEVER set as environment variables, NEVER printed, and NEVER logged.

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
import sys

try:
    import keyring
except ImportError:
    print(
        "Missing dependency: keyring\n"
        "Install with: pip3 install keyring",
        file=sys.stderr,
    )
    sys.exit(1)

SERVICE = "chief-wiggum"

KNOWN_KEYS = [
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
]


def get_secret(name: str) -> str | None:
    """Fetch a secret from the system keyring. Returns None if not found."""
    try:
        val = keyring.get_password(SERVICE, name)
        return val  # None if not found, str if found
    except Exception:
        return None


def has_secret(name: str) -> bool:
    """Check if a secret exists in the keyring."""
    try:
        return keyring.get_password(SERVICE, name) is not None
    except Exception:
        return False


def set_secret(name: str, value: str) -> None:
    """Store a secret in the system keyring. Overwrites if exists."""
    keyring.set_password(SERVICE, name, value)


def delete_secret(name: str) -> bool:
    """Delete a secret from the keyring. Returns True if it existed."""
    try:
        keyring.delete_password(SERVICE, name)
        return True
    except keyring.errors.PasswordDeleteError:
        return False


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
        print("  set KEY_NAME      Store a key in system keyring (prompts for value)")
        print("  get KEY_NAME      Check if a key exists (does NOT print the value)")
        print("  delete KEY_NAME   Remove a key from keyring")
        print("  list              Show status of all known keys")
        print()
        print(f"Known keys: {', '.join(KNOWN_KEYS)}")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "list":
        print(f"=== Chief Wiggum Keyring (service: {SERVICE}) ===")
        print()
        for s in list_secrets():
            status = "[stored]  " if s["in_keychain"] else "[not set] "
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
        print(f"Stored {key_name} in keyring (service: {SERVICE})")

    elif cmd == "get":
        if has_secret(key_name):
            print(f"{key_name}: stored in keyring")
        else:
            print(f"{key_name}: not found")
            sys.exit(1)

    elif cmd == "delete":
        if delete_secret(key_name):
            print(f"Deleted {key_name} from keyring")
        else:
            print(f"{key_name} not found in keyring")
            sys.exit(1)

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
