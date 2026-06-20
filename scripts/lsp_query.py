#!/usr/bin/env python3
"""CLI for semantic code intelligence via LSP (#35).

Queries a language server (gopls for Go, pyright for Python) for go-to-definition,
references, hover types, and live diagnostics — structured JSON an agent can
consume. Picks the server from the file extension.

**Graceful degradation**: if no server is configured/installed for the file, this
prints ``{"available": false, "reason": ...}`` and exits 0 — it never blocks the
workflow; the caller falls back to its existing behavior.

Examples:
    python3 scripts/lsp_query.py --root . diagnostics path/to/file.go
    python3 scripts/lsp_query.py --root . --line 41 --col 6 definition app/x.py
    python3 scripts/lsp_query.py --root . --line 41 --col 6 hover app/x.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chief_wiggum import lsp  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Semantic code intelligence via LSP")
    parser.add_argument("query", choices=["definition", "references", "hover", "diagnostics"])
    parser.add_argument("file", help="Source file to query")
    parser.add_argument("--root", default=".", help="Project/repo root (resolved by the server)")
    parser.add_argument("--line", type=int, default=0, help="0-based line (LSP)")
    parser.add_argument("--col", type=int, default=0, help="0-based UTF-16 character (LSP)")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)

    file_path = Path(args.file)
    server = lsp.server_for_file(file_path)
    if server is None:
        print(json.dumps({"available": False, "reason": f"no LSP server for {file_path.suffix or 'this file'}"}))
        return 0
    if not lsp.server_available(server):
        print(json.dumps({
            "available": False,
            "server": server.name,
            "reason": f"{server.command[0]} not installed; falling back to existing behavior",
        }))
        return 0

    try:
        with lsp.LspClient(server, args.root, timeout=args.timeout) as client:
            client.did_open(file_path)
            if args.query == "definition":
                result = client.definition(file_path, args.line, args.col)
            elif args.query == "references":
                result = client.references(file_path, args.line, args.col)
            elif args.query == "hover":
                result = client.hover(file_path, args.line, args.col)
            else:  # diagnostics
                result = client.diagnostics(file_path, timeout=min(args.timeout, 15))
    except lsp.LspError as exc:
        # A server error must not block the workflow either.
        print(json.dumps({"available": False, "server": server.name, "reason": str(exc)}))
        return 0

    print(json.dumps({"available": True, "server": server.name, "query": args.query, "result": result}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
