"""Journal an experiment arm's raw results through the ratchet hash chain (#391).

The pre-registration requires raw per-task results to be immutable and
journaled, "consistent with how gate-validation records are handled (no
signing/DSSE)". That means the existing ratchet journal: an append-only hash
chain where lowering the bar is tamper-evident and fails closed.

**Why this lives outside `ratchet.py`.** The ratchet gate's `--scanner-version`
is derived from the hash of `ratchet.py` and its finding-affecting
dependencies, deliberately, so nobody has to remember to bump a constant
(INV-fh-005). Adding an unrelated event type to that file would stale the
ratchet gate's validation record and demand a re-run of its seeded-defect
trials, a real cost paid for a change that touches none of the gate's
finding logic. So the chain primitives are imported and the event is appended
from here. `ratchet.py` is unchanged and its record stays valid.

Only DIGESTS go in the chain; the per-task records live beside it. The journal
stays a tamper-evident index rather than a second copy of the data that could
drift from the first.
"""

from __future__ import annotations

import json
from pathlib import Path

from .hashing import stable_hash

# One arm's raw results in the dynamic-DAG ablation. Like `gate-authority` it
# is never `merged`, so `ratchet.derive_highwater` skips it and an experiment
# record can never move the pass-set high-water mark.
EXPERIMENT_RECORD = "experiment-record"


class ExperimentJournalError(RuntimeError):
    """A record that cannot be appended, or a chain that must not be extended."""


def append_experiment_record(journal_path: str | Path, arm: str, *,
                             results_digest: str, manifest_digest: str,
                             corpus_version: str, verifier_hash: str) -> str:
    """Append one arm's result digests to the ratchet journal.

    Every field a later reader needs to prove the arm ran under the registered
    conditions is here. Change the corpus, the verifier, the manifest or a
    single task's outcome and the digest recorded at the time no longer
    matches what is on disk.

    Refuses to append onto a broken chain, failing closed. A tampered journal
    must not be silently extended, and an event appended past a torn tail is
    an event nobody can verify later.
    """
    from ratchet import verified_prefix  # noqa: PLC0415 - avoids a cycle

    fields = {
        "arm": arm,
        "results_digest": results_digest,
        "manifest_digest": manifest_digest,
        "corpus_version": corpus_version,
        "verifier_hash": verifier_hash,
    }
    for name, value in fields.items():
        if not str(value or "").strip():
            raise ExperimentJournalError(
                f"experiment record needs a non-empty {name}; a record missing"
                " one of its digests cannot prove anything later"
            )

    path = Path(journal_path)
    # Robust broken-chain detection: compare the verified prefix against the
    # raw non-empty line count WITHOUT a full JSON parse, so a garbage tail
    # raises here rather than as a JSONDecodeError deep in the reader.
    raw_lines = ([line for line in path.read_text().splitlines() if line.strip()]
                 if path.is_file() else [])
    verified = verified_prefix(path)
    if len(verified) != len(raw_lines):
        raise ExperimentJournalError(
            f"cannot append an experiment record: {path} chain is broken"
            " (fail closed)"
        )

    previous = verified[-1]["record_hash"] if verified else "genesis"
    body = {
        "record_id": f"rec-{len(verified) + 1:05d}",
        "event": EXPERIMENT_RECORD,
        "ref": arm,
        "details": results_digest,
        "manifest_digest": manifest_digest,
        "corpus_version": corpus_version,
        "verifier_hash": verifier_hash,
        "merged": False,
    }
    body["record_hash"] = stable_hash(
        previous,
        json.dumps({key: value for key, value in body.items()
                    if key != "record_hash"}, sort_keys=True),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(body, sort_keys=True) + "\n")
    return body["record_id"]


def experiment_records(journal_path: str | Path) -> list[dict]:
    """Every verified experiment record in the journal, oldest first.

    Reads the VERIFIED prefix only: a record past a torn tail is not evidence
    of anything, and returning it would let a hand-appended result read as
    journaled.
    """
    from ratchet import verified_prefix  # noqa: PLC0415 - avoids a cycle

    return [record for record in verified_prefix(journal_path)
            if record.get("event") == EXPERIMENT_RECORD]
