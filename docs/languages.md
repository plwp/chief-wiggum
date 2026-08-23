# Language Support Matrix

Generated from `config/languages.json` by `scripts/render_languages_doc.py` (#162) — do not hand-edit this file; edit the config and re-run the script (wired into `/update`).

Consumed by `check_deps.py` (`--list-languages`, the `language-tier-1` dependency profile) and by `scripts/emitters/` (the per-language emitter fallback chain: language-specific emitter -> generic regex tier -> skip-with-warning). See `docs/single-writer.md` / `docs/traceability.md` for what the emitters feed.

## Languages

| Language | Tier | Status | Extensions | LSP | Emitters | Test parser | Extractor | func_regex |
|---|---|---|---|---|---|---|---|---|
| go | 1 | supported | `.go` | gopls | writer, trace | go test -json / gotestsum JUnit XML (scripts/ratchet.py) | go_mongo (scripts/extractors/go_mongo.py, stitch-audit) | yes |
| python | 1 | supported | `.py` | pyright | writer, trace | pytest --junitxml (JUnit XML, scripts/ratchet.py) | — | yes |
| typescript | 1 | supported | `.ts`, `.tsx`, `.js`, `.jsx` | — | writer, trace | jest/vitest JUnit reporter | typescript (scripts/extractors/typescript.py, stitch-audit) | yes |
| csharp | 2 | partial: scanned + measured, no dedicated emitter/LSP | `.cs` | — | generic | dotnet test --logger trx (TRX results dir, scripts/ratchet.py `trx` parser) + .sln/.csproj autodetect | — | yes |
| rust | designed | designed, unbuilt | `.rs` | rust-analyzer | — | cargo nextest (JUnit XML) + Cargo.toml autodetect | — | no |

## Generic regex tier

Extensions with no dedicated per-language emitter module, scanned by the generic (language-agnostic) regex tier (`scripts/emitters/generic.py`) — the pre-#162 behavior of `check_single_writer.py` / `check_traceability.py`:

`.cs`, `.java`, `.rb`, `.rs`

## Recognized-but-unsupported extensions

Encountering one of these during a full-repo scan is NEVER a silent skip — `check_single_writer.py` / `check_traceability.py` surface an explicit coverage warning (`unsupported_extension_counts`) in both `--gate` and plain (query) output:

`.c`, `.cc`, `.clj`, `.cljs`, `.cpp`, `.dart`, `.erl`, `.ex`, `.exs`, `.groovy`, `.h`, `.hpp`, `.hs`, `.jl`, `.kt`, `.kts`, `.lua`, `.m`, `.mm`, `.php`, `.pl`, `.r`, `.scala`, `.swift`

### Pending tier decision: `.lua` (chief-wiggum#413)

`.lua` sits in the list above **deliberately**, and not because the scanner cannot read it.

**The emitter already works.** chief-wiggum#377 added Redis write-command detection, `KIND_SCRIPT` emission and the `--` comment marker, so feeding a `.lua` file to the write-site emitter produces facts today. The reported case (inline Lua inside a scanned Go file) is fixed. A *standalone* `.lua` file is surfaced as a coverage gap — and if a single-write-path field's only writer lives in one, `check_single_writer` reports the field `blind` rather than finding the writer.

**The blocker is the shared tier table, not the emitter.** Moving `.lua` into `generic_tier` was tried during #377 and broke four tests in `tests/test_external_links.py`, which used `hook.lua` as its canonical *unscannable* language. That fixture encoded the premise of external links — the feature exists for languages CW cannot resolve — in a real extension's tier, so promoting a language silently redefined a whole subsystem's assumption as a side effect of a write-detection fix.

That coupling is now gone: those tests use a deliberately fictional `.cwnolang` extension, which no language will ever claim and therefore cannot be promoted. The subsystem no longer has an opinion on which real languages are scannable, so a future tier change cannot break it the same way.

**What promoting `.lua` still costs**, per `docs/gate-rollout.md` and `docs/gate-validation.md`:

- a clean-corpus run with coverage evidence, because the gate would start producing findings from files it has never looked at — Lua's assignment syntax (`t.field = v`, `t["field"] = v`) is *plausibly* close enough to the generic regex patterns, and plausible is not measured
- re-deriving `check_single_writer`'s validation record (`gate_validation_designer.py revalidate check_single_writer`) rather than hand-editing it, since the scanned population changes

Changing an extension's tier is a cross-subsystem decision, not a per-gate one. Until that decision is taken with the corpus evidence behind it, `.lua` stays recognized-but-unsupported — a loud coverage gap, never a silent skip.

## Partially supported (tier 2)

These languages ARE scanned and measured — the quality/debt population, clone detection and the ratchet pass-set all see them — but no dedicated emitter module or LSP exists, so write-site/trace facts come from the generic regex tier. Listed here with exactly what is still missing for tier 1.

### Csharp

Trigger: first .NET target repo adopted (#259)

Missing for tier 1:

- a C#/Roslyn LSP entry in scripts/chief_wiggum/lsp.py SERVERS (csharp-ls or Microsoft.CodeAnalysis.LanguageServer)
- a C#-specific write-site emitter under scripts/emitters/ (property setters, object initializers, EF Core / Dapper write calls)
- cognitive-complexity tooling for C# in scripts/quality/complexity.py (lizard already supplies cyclomatic)

Tier 2 means MEASURED, not fully emitted: .cs enters the quality/debt population (scripts/quality/complexity.py EXT_LANG), jscpd clone detection, the ratchet pass-set via the TRX parser, and write-site/trace facts via the generic regex tier with a real C# method regex behind enclosing-symbol resolution (CS_FUNC_RE). What tier 1 would add is a dedicated emitter module (C#-aware write shapes), an LSP, and a dead-code tier. Added because a 12,551-file .NET monolith adopted with an EMPTY ratchet baseline and a 0-item debt inventory that both rendered as passes (#259).


## Designed, unbuilt slots

### Rust

Trigger: first real Rust target repo

Requires when triggered:

- rust-analyzer entry in scripts/chief_wiggum/lsp.py SERVERS
- cargo nextest JUnit XML output + Cargo.toml autodetect wired into scripts/ratchet.py's test-result parser
- fn regex for enclosing-symbol resolution (scripts/chief_wiggum/write_emission.py _enclosing_symbol)
- writer patterns for struct literals + sqlx macros (a Rust-specific write-site emitter under scripts/emitters/)

The .rs extension is already scanned today by the generic regex tier (see generic_tier below) — write-site/trace-annotation facts ARE emitted for Rust files, just without a dedicated func_regex for enclosing-symbol resolution. 'designed, unbuilt' means the TIER-1 emitter (rust-analyzer + cargo nextest + fn regex + sqlx-aware writer patterns) is not built, not that Rust is unscanned.
