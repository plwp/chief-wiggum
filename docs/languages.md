# Language Support Matrix

Generated from `config/languages.json` by `scripts/render_languages_doc.py` (#162) — do not hand-edit this file; edit the config and re-run the script (wired into `/update`).

Consumed by `check_deps.py` (`--list-languages`, the `language-tier-1` dependency profile) and by `scripts/emitters/` (the per-language emitter fallback chain: language-specific emitter -> generic regex tier -> skip-with-warning). See `docs/single-writer.md` / `docs/traceability.md` for what the emitters feed.

## Languages

| Language | Tier | Status | Extensions | LSP | Emitters | Test parser | Extractor | func_regex |
|---|---|---|---|---|---|---|---|---|
| go | 1 | supported | `.go` | gopls | writer, trace | go test -json / gotestsum JUnit XML (scripts/ratchet.py) | go_mongo (scripts/extractors/go_mongo.py, stitch-audit) | yes |
| python | 1 | supported | `.py` | pyright | writer, trace | pytest --junitxml (JUnit XML, scripts/ratchet.py) | — | yes |
| typescript | 1 | supported | `.ts`, `.tsx`, `.js`, `.jsx` | — | writer, trace | jest/vitest JUnit reporter | typescript (scripts/extractors/typescript.py, stitch-audit) | yes |
| rust | designed | designed, unbuilt | `.rs` | rust-analyzer | — | cargo nextest (JUnit XML) + Cargo.toml autodetect | — | no |

## Generic regex tier

Extensions with no dedicated per-language emitter module, scanned by the generic (language-agnostic) regex tier (`scripts/emitters/generic.py`) — the pre-#162 behavior of `check_single_writer.py` / `check_traceability.py`:

`.java`, `.rb`, `.rs`

## Recognized-but-unsupported extensions

Encountering one of these during a full-repo scan is NEVER a silent skip — `check_single_writer.py` / `check_traceability.py` surface an explicit coverage warning (`unsupported_extension_counts`) in both `--gate` and plain (query) output:

`.c`, `.cc`, `.clj`, `.cljs`, `.cpp`, `.cs`, `.dart`, `.erl`, `.ex`, `.exs`, `.groovy`, `.h`, `.hpp`, `.hs`, `.jl`, `.kt`, `.kts`, `.lua`, `.m`, `.mm`, `.php`, `.pl`, `.r`, `.scala`, `.swift`

## Designed, unbuilt slots

### Rust

Trigger: first real Rust target repo

Requires when triggered:

- rust-analyzer entry in scripts/chief_wiggum/lsp.py SERVERS
- cargo nextest JUnit XML output + Cargo.toml autodetect wired into scripts/ratchet.py's test-result parser
- fn regex for enclosing-symbol resolution (scripts/chief_wiggum/write_emission.py _enclosing_symbol)
- writer patterns for struct literals + sqlx macros (a Rust-specific write-site emitter under scripts/emitters/)

The .rs extension is already scanned today by the generic regex tier (see generic_tier below) — write-site/trace-annotation facts ARE emitted for Rust files, just without a dedicated func_regex for enclosing-symbol resolution. 'designed, unbuilt' means the TIER-1 emitter (rust-analyzer + cargo nextest + fn regex + sqlx-aware writer patterns) is not built, not that Rust is unscanned.
