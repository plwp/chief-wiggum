#!/usr/bin/env bash
# Keychain helper — source this module, never run it directly.
# Loads API keys from macOS Keychain into environment variables.
# Secrets stay in-process and never leak to stdout/chat.
#
# Usage (from another script):
#   source "$(dirname "$0")/keychain.sh"
#   cw_load_key ANTHROPIC_API_KEY
#   cw_load_key GOOGLE_CLOUD_PROJECT
#
# To store a key:
#   bash scripts/keychain.sh set ANTHROPIC_API_KEY
#   bash scripts/keychain.sh get ANTHROPIC_API_KEY   (prints status only, not the value)
#   bash scripts/keychain.sh list
#   bash scripts/keychain.sh delete ANTHROPIC_API_KEY

CW_KEYCHAIN_SERVICE="chief-wiggum"

# All keys chief-wiggum knows about
CW_KNOWN_KEYS=(
  ANTHROPIC_API_KEY
  OPENAI_API_KEY
  GEMINI_API_KEY
  GOOGLE_CLOUD_PROJECT
  GOOGLE_CLOUD_LOCATION
)

# --- Module functions (for sourcing) ---

cw_load_key() {
  # Load a single key from keychain into an env var.
  # If the env var is already set, keychain is skipped (env takes precedence).
  # Returns 0 if key is available, 1 if not found anywhere.
  local key_name="$1"

  # Already set in environment — nothing to do
  if [ -n "${!key_name:-}" ]; then
    return 0
  fi

  # Try macOS Keychain
  local val
  val=$(security find-generic-password -a "$key_name" -s "$CW_KEYCHAIN_SERVICE" -w 2>/dev/null) || true

  if [ -n "$val" ]; then
    export "$key_name=$val"
    return 0
  fi

  return 1
}

cw_load_all() {
  # Load all known keys from keychain. Silently skips missing ones.
  local loaded=0
  for key in "${CW_KNOWN_KEYS[@]}"; do
    if cw_load_key "$key"; then
      loaded=$((loaded + 1))
    fi
  done
  return 0
}

cw_has_key() {
  # Check if a key is available (env or keychain) without printing it.
  local key_name="$1"
  if [ -n "${!key_name:-}" ]; then
    return 0
  fi
  security find-generic-password -a "$key_name" -s "$CW_KEYCHAIN_SERVICE" -w &>/dev/null
}

# --- CLI commands (for direct invocation) ---

_cw_keychain_set() {
  local key_name="$1"
  echo -n "Enter value for $key_name: "
  # Read without echoing to terminal
  read -rs val
  echo ""

  if [ -z "$val" ]; then
    echo "Error: empty value" >&2
    return 1
  fi

  # Delete existing entry if present (security errors on duplicate)
  security delete-generic-password -a "$key_name" -s "$CW_KEYCHAIN_SERVICE" &>/dev/null || true

  security add-generic-password -a "$key_name" -s "$CW_KEYCHAIN_SERVICE" -w "$val"
  echo "Stored $key_name in keychain (service: $CW_KEYCHAIN_SERVICE)"
}

_cw_keychain_get() {
  local key_name="$1"
  if security find-generic-password -a "$key_name" -s "$CW_KEYCHAIN_SERVICE" -w &>/dev/null; then
    echo "$key_name: stored in keychain"
  elif [ -n "${!key_name:-}" ]; then
    echo "$key_name: set via environment"
  else
    echo "$key_name: not found"
    return 1
  fi
}

_cw_keychain_delete() {
  local key_name="$1"
  if security delete-generic-password -a "$key_name" -s "$CW_KEYCHAIN_SERVICE" &>/dev/null; then
    echo "Deleted $key_name from keychain"
  else
    echo "$key_name not found in keychain"
    return 1
  fi
}

_cw_keychain_list() {
  echo "=== Chief Wiggum Keychain (service: $CW_KEYCHAIN_SERVICE) ==="
  echo ""
  for key in "${CW_KNOWN_KEYS[@]}"; do
    local in_keychain=false
    local in_env=false
    security find-generic-password -a "$key" -s "$CW_KEYCHAIN_SERVICE" -w &>/dev/null && in_keychain=true
    [ -n "${!key:-}" ] && in_env=true

    if $in_keychain && $in_env; then
      echo "  [keychain+env]  $key"
    elif $in_keychain; then
      echo "  [keychain]      $key"
    elif $in_env; then
      echo "  [env only]      $key"
    else
      echo "  [not set]       $key"
    fi
  done
}

# --- CLI entrypoint (only when run directly, not sourced) ---

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  case "${1:-}" in
    set)
      _cw_keychain_set "${2:?Usage: keychain.sh set KEY_NAME}"
      ;;
    get)
      _cw_keychain_get "${2:?Usage: keychain.sh get KEY_NAME}"
      ;;
    delete)
      _cw_keychain_delete "${2:?Usage: keychain.sh delete KEY_NAME}"
      ;;
    list)
      _cw_keychain_list
      ;;
    *)
      echo "Usage: keychain.sh <set|get|delete|list> [KEY_NAME]"
      echo ""
      echo "Commands:"
      echo "  set KEY_NAME      Store a key in macOS Keychain (prompts for value)"
      echo "  get KEY_NAME      Check if a key exists (does NOT print the value)"
      echo "  delete KEY_NAME   Remove a key from Keychain"
      echo "  list              Show status of all known keys"
      echo ""
      echo "Known keys: ${CW_KNOWN_KEYS[*]}"
      exit 1
      ;;
  esac
fi
