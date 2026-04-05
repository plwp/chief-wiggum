#!/bin/bash
# Pre-merge check: auto-detects project layers and runs their test/lint/build
# commands. Language-agnostic — works for Go, Node, Python, Rust, or mixed repos.
#
# Usage: bash scripts/pre-merge-check.sh
#
# Detection rules (per directory):
#   go.mod          → go test ./... && golangci-lint run ./... && go build ./...
#   package.json    → npm test (or vitest/jest if configured) && npm run lint
#   pyproject.toml  → pytest && ruff check (or flake8)
#   Cargo.toml      → cargo test && cargo clippy
#   Makefile w/ ci  → make ci
#
# Add a .pre-merge-check file to any directory to override with custom commands.

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOTAL=0
FAILED=0
SKIPPED=0

run_check() {
    local name="$1"
    shift
    TOTAL=$((TOTAL + 1))
    printf "${YELLOW}▶ %s${NC}\n" "$name"
    if eval "$@" > /dev/null 2>&1; then
        printf "${GREEN}  ✓ %s${NC}\n" "$name"
    else
        printf "${RED}  ✗ %s${NC}\n" "$name"
        FAILED=$((FAILED + 1))
    fi
}

skip_check() {
    SKIPPED=$((SKIPPED + 1))
    printf "${YELLOW}  ⚠ %s (skipped — tool not found)${NC}\n" "$1"
}

# Scan a directory for a custom override file first, then auto-detect.
scan_dir() {
    local dir="$1"
    local label="$2"

    # Custom override: .pre-merge-check file with one command per line
    if [ -f "$dir/.pre-merge-check" ]; then
        printf "\n${YELLOW}── %s (custom)${NC}\n" "$label"
        while IFS= read -r cmd; do
            [ -z "$cmd" ] && continue
            [[ "$cmd" == \#* ]] && continue
            run_check "$label: $cmd" "cd '$dir' && $cmd"
        done < "$dir/.pre-merge-check"
        return
    fi

    # Makefile with 'ci' target — use it as the single source of truth
    if [ -f "$dir/Makefile" ] && grep -q '^ci:' "$dir/Makefile" 2>/dev/null; then
        printf "\n${YELLOW}── %s (Makefile ci)${NC}\n" "$label"
        run_check "$label: make ci" "cd '$dir' && make ci"
        return
    fi

    # Go
    if [ -f "$dir/go.mod" ]; then
        printf "\n${YELLOW}── %s (Go)${NC}\n" "$label"
        run_check "$label: tests" "cd '$dir' && go test ./... -count=1"
        if command -v golangci-lint &>/dev/null; then
            run_check "$label: lint" "cd '$dir' && golangci-lint run ./..."
        else
            skip_check "$label: lint (golangci-lint)"
        fi
        run_check "$label: build" "cd '$dir' && go build ./..."
    fi

    # Node (package.json)
    if [ -f "$dir/package.json" ]; then
        printf "\n${YELLOW}── %s (Node)${NC}\n" "$label"

        # Test: prefer vitest > jest > npm test
        if grep -q '"vitest"' "$dir/package.json" 2>/dev/null; then
            run_check "$label: tests" "cd '$dir' && npx vitest run"
        elif grep -q '"jest"' "$dir/package.json" 2>/dev/null; then
            run_check "$label: tests" "cd '$dir' && npx jest --passWithNoTests"
        elif grep -q '"test"' "$dir/package.json" 2>/dev/null; then
            run_check "$label: tests" "cd '$dir' && npm test -- --passWithNoTests 2>/dev/null || npm test"
        fi

        # Lint: prefer biome > eslint > npm run lint
        if grep -q '"lint"' "$dir/package.json" 2>/dev/null; then
            run_check "$label: lint" "cd '$dir' && npm run lint"
        fi

        # Build (if script exists)
        if grep -q '"build"' "$dir/package.json" 2>/dev/null; then
            run_check "$label: build" "cd '$dir' && npm run build"
        fi
    fi

    # Python
    if [ -f "$dir/pyproject.toml" ] || [ -f "$dir/setup.py" ] || [ -f "$dir/requirements.txt" ]; then
        printf "\n${YELLOW}── %s (Python)${NC}\n" "$label"
        if command -v pytest &>/dev/null; then
            run_check "$label: tests" "cd '$dir' && pytest"
        elif [ -f "$dir/pyproject.toml" ] && grep -q 'pytest' "$dir/pyproject.toml" 2>/dev/null; then
            run_check "$label: tests" "cd '$dir' && python -m pytest"
        fi
        if command -v ruff &>/dev/null; then
            run_check "$label: lint" "cd '$dir' && ruff check ."
        elif command -v flake8 &>/dev/null; then
            run_check "$label: lint" "cd '$dir' && flake8 ."
        fi
    fi

    # Rust
    if [ -f "$dir/Cargo.toml" ]; then
        printf "\n${YELLOW}── %s (Rust)${NC}\n" "$label"
        run_check "$label: tests" "cd '$dir' && cargo test"
        run_check "$label: lint" "cd '$dir' && cargo clippy -- -D warnings"
    fi
}

echo "═══════════════════════════════════════"
echo "  Pre-merge checks (auto-detected)"
echo "═══════════════════════════════════════"

# Scan repo root for top-level projects
for marker in go.mod package.json pyproject.toml Cargo.toml; do
    if [ -f "$REPO_ROOT/$marker" ]; then
        scan_dir "$REPO_ROOT" "root"
        break
    fi
done

# Scan immediate subdirectories for additional project layers
for subdir in "$REPO_ROOT"/*/; do
    [ -d "$subdir" ] || continue
    dirname="$(basename "$subdir")"

    # Skip common non-project dirs
    case "$dirname" in
        .git|.github|node_modules|vendor|dist|build|.claude|docs|templates|terraform) continue ;;
    esac

    for marker in go.mod package.json pyproject.toml Cargo.toml .pre-merge-check Makefile; do
        if [ -f "$subdir/$marker" ]; then
            scan_dir "$subdir" "$dirname"
            break
        fi
    done
done

echo ""
echo "═══════════════════════════════════════"
printf "  Ran: %d  Passed: %d  Failed: %d  Skipped: %d\n" "$TOTAL" "$((TOTAL - FAILED))" "$FAILED" "$SKIPPED"
if [ $FAILED -eq 0 ]; then
    printf "${GREEN}  All checks passed. Safe to merge.${NC}\n"
else
    printf "${RED}  %d check(s) failed. Do NOT merge.${NC}\n" "$FAILED"
    exit 1
fi
