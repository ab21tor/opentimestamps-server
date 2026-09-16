# Contracts

What each part of the calendar promises, who owns unfinished work at each
handoff, what durable evidence lets a part forget, and which records are
authoritative. Every statement here is made by code named in the README
section it cites; a change to any of them changes this file first
(CONTRIBUTING.md). Written 2026-09-16 against the 2026-09-15/16 reviews;
section 9, the self-stamp, added the same day (workflow two).

The words used throughout:

- **Fingerprint**: the 32-byte SHA-256 a client hands in. Two submissions
  of the same bytes are the same fingerprint.
- **Submission**: one accepted `POST /digest` (or `/operator/digest`). A
  submission is what the aggregator counts as a record; the same
  fingerprint submitted twice inside the dedupe horizon is one submission,
  outside it two ("Anchor receipts" in the README).
- **Commitment**: the 44-byte journal entry a per-second tree's root
  becomes: time prefix, root, HMAC. One per second with traffic, whatever
  the number of submissions under it.
- **Occurrence**: one leaf of one tree. The record count is a count of
  occurrences at tree close; receipts bill occurrences, and only ever
  undercount.
- **Anchor**: one Bitcoin transaction carrying the root of a tree over
  pending commitments. It is a *candidate* until it has
  `--btc-min-confirmations` blocks on top, and *saved* once its proofs are
  in the database.

## 1. What an HTTP success promises

| Request | Success | The promise | What it does not promise |
|---|---|---|---|
| `POST /digest` | 200, body = the serialized pending timestamp | The commitment carrying this digest is in the journal, fsynced, before the response is sent (`Aggregator.submit` returns only after `Calendar.submit` returns, which returns after `JournalWriter.submit` has fsynced). From here the calendar owns the obligation: the entry will be anchored, or the calendar will say why not. | Not that it rides the next anchor; not that the record count is stored (counts may be lost, never invented); not that a proof exists yet: `GET /timestamp/<commitment>` answers 404 "Pending" until the anchor is saved. |
| `POST /operator/digest` | same as `/digest` | The same, and that the leaf is not a record. The self-stamp's side of this exchange is section 9, S4. | — |
| `GET /timestamp/<commitment>` | 200, body = the timestamp | The commitment is in the database: its tree was saved after an anchor reached the confirmation depth. The bytes never change afterwards (`Cache-Control` one year). | Not that the anchor is still in the chain today: a deeper reorg is reported by the detector (`needs_attention`), never repaired here. Not that the bytes verify: that is the client's or the claim kit's check. |
| `GET /timestamp/<commitment>` | 404 "Pending confirmation in Bitcoin blockchain" | The commitment is in the journal at or after the scan cursor, or in memory in a tree not yet saved. | — |
| `GET /timestamp/<commitment>` | 404 "Not found" | The database does not hold it and the stamper does not see it pending. This is also what a commitment gets in the seconds between its journal append and the stamper's next fill pass, and after a restart until the scan reaches it; the client retries (`Cache-Control` 60 s). | — |
| `GET /` | 200, one JSON line | Built in full before the response is committed. `best_block` null means the Bitcoin RPC path is down: an answered status is not a healthy calendar. | — |

**Ambiguous outcomes of `POST /digest`.** A 503 from a stopped aggregator
means the digest was not committed. A 503 from a round that did not finish
within 30 s is ambiguous: the round may still commit after the client has
gone. A client that times out after the journal fsync and before the
response holds no proof for a commitment that exists. In both cases the
client resubmits; inside the dedupe horizon (one hour, in memory) the
resubmission attaches to the earlier commitment and is counted once;
across a restart or past the horizon it becomes a second commitment and a
second occurrence. That is the one bounded over-count and it is stated in
the README. No ambiguous outcome deletes anything: the journal only grows.

## 2. Records: authoritative, evidence, rebuildable

| Record | Kind | What it is authoritative for | Loss or damage |
|---|---|---|---|
| `journal` | authoritative | Every obligation ever accepted, in order. An entry not in the database is owed. | Lost entries are lost obligations: their pending proofs never upgrade. A journal shorter than the checkpoint claims is refused at start (section 4, C6). |
| `db/` (LevelDB) | authoritative | Every saved proof path (commitment → anchor → block), plus its own identity (`generation`) and the journal index below which it holds everything (`watermark`), committed in the same batch as the proofs that make it true. | A lost or older database beside a kept checkpoint is refused at start. Recovery: delete the checkpoint, rescan from 0, re-anchor what is missing (later blocks). |
| `journal.known-good` | rebuildable index | Where the restart scan may begin. | Deleting it costs one full rescan, never a proof. It is trusted only when its generation is the database's, its index is at or below the watermark, and the journal reaches it and agrees with the database at the entry below it. |
| `journal.counts` | auxiliary | Occurrence counts per journal entry, written after the entry is durable. | A lost count is an undercount, warned once per tree, never an overcount. |
| receipts file (`OTSD_ANCHOR_RECEIPTS`) | authoritative accounting | One line per saved anchor: what it cost and how many occurrences it carried. The gateway's billing reads it. | Append-only; a torn last line is dropped and recovered from its marker. |
| `<receipts>.pending.<txid>` | durable evidence of work in progress | A receipt owed for an anchor whose calendar save is under way or done. Settled by probing the database for one of its commitments. | One marker per anchor. A marker is removed only by the code that settled it or wrote its receipt; a later anchor never touches an earlier anchor's marker (C5). The single-name marker from before 2026-09-16 (`<receipts>.pending`) is still read and settled. |
| in-memory: `pending_commitments`, `unconfirmed_txs`, `txs_waiting_for_confirmation` | rebuildable | Nothing across a restart. | A restart rebuilds pending from the journal; an in-flight or mined-but-shallow anchor is forgotten and its commitments are re-anchored in a fresh transaction (the forgotten anchor is never receipted; its fee is spent and unaccounted). |
| the wallet (Bitcoin Core) | external | What was broadcast and what confirmed. | The stamper assumes exclusive use of it. |

## 3. Who owns unfinished work

| Handoff | Before | After | Owner of the work in between | Evidence that lets the previous owner forget |
|---|---|---|---|---|
| client → aggregator | the client holds bytes | the digest is in the round's queue (memory) | nobody durable: a crash here loses the request and the client gets no 200 | none needed: no promise was made |
| aggregator → journal | round queue | journal entry fsynced | the journal | the fsync returning; only then `done_event` fires and the 200 goes out |
| journal → stamper | journal entry | `pending_commitments` (memory) | the journal, still: the stamper's memory is a view rebuilt at every start from the checkpoint onward | never: the journal is the record until the database holds the commitment |
| stamper → Bitcoin | pending | `unconfirmed_txs` (memory) and the wallet | the journal still owns the obligation; the wallet owns the transaction | none: a restart forgets the transaction and re-anchors |
| Bitcoin → stamper | mined at height h | `txs_waiting_for_confirmation[h]` (memory) | the journal still | none until the save |
| stamper → database | mature tree | saved in one synchronous batch with the watermark | the database | the batch returning: from here `commitment in calendar` is true, the fill pass skips the entry, the checkpoint may advance past it |
| database → receipts | saved | receipt line fsynced, marker removed | the marker while it stands | the receipt line on file, fsynced with its directory |
| stamper → operator | detector finding | `needs_attention` | the operator; nothing is re-anchored | never clears itself; a restart forgets it (a limitation, README "What it does not do") |

## 4. Workflow 1: submission → journal → tree → anchor → receipt

One table per transition. "Authoritative state" is where the truth lives
at the acknowledgement point; "ambiguous outcomes" are the states a stop
can leave; "recovery" is what the next start does with them. Every
recovery here is itself interruptible and lands in one of the listed
states again.

### C1. Accept a digest

| | |
|---|---|
| Authoritative state | the journal (append-only file) |
| Preconditions | the aggregator loop is alive and has not failed; the body is 1–64 bytes |
| Side effects | one journal entry per second with traffic, fsynced; the count sidecar entry (best effort); the pending timestamp in the response |
| Acknowledgement point | the 200 is sent after `Journal.submit` returns from `os.fsync` |
| Ambiguous outcomes | 503 after a 30 s round timeout (may still commit); client timeout after the fsync (committed, unacknowledged); an aggregator round that raises: the process exits 1 and the round's commitment may or may not be in the journal (a failed fsync after the write, a partial write padded to a record at the next start, or nothing), and nothing about the failure point is known to the client |
| Recovery | the journal needs none: whatever it holds is anchored at the next start, asked for or not; the client resubmits and is deduped inside the horizon (across the restart it is a new commitment, counted again); the supervisor restarts a stopped process |
| Tests | `test_aggregator_failure`, `test_aggregator_dedupe`, `test_rpc_digest`, `test_anchor_records` |

### C2. The fill pass: journal → pending

| | |
|---|---|
| Authoritative state | the journal; the database decides which entries are already done |
| Preconditions | the storage check passed at start (C6); the scan cursor is at or after the checkpoint |
| Side effects | memory only: `pending_commitments`, `commitment_idxs` (lowest index per commitment), `commitment_records` |
| Acknowledgement point | none: this transition is invisible outside the process except through `GET /timestamp` answering "Pending" |
| Ambiguous outcomes | a read error mid-pass adds nothing this round (errs low), warned once, retried every second |
| Recovery | a restart rescans from the checkpoint |
| Tests | `test_stamper_checkpoint`, `test_stamper_read_errors` |

### C3. Departure: pending → anchor candidate

| | |
|---|---|
| Authoritative state | the wallet holds the transaction; the journal still holds the obligation |
| Preconditions | the departure clock has fired; the wallet has a spendable output; the fee is under the cap |
| Side effects | a signed transaction broadcast; `unconfirmed_txs` (memory); the tree's occurrence count fixed at close |
| Acknowledgement point | none outside the process (`GET /` shows `most_recent_tx`) |
| Ambiguous outcomes | broadcast succeeded but the process died before recording it (the transaction is in the mempool, the stamper does not know it) |
| Recovery | the next start re-reads pending and starts a fresh cycle from the wallet's confirmed outputs; if the forgotten transaction is mined the dead-cycle path abandons the fresh one and the commitments stay pending until anchored again by a tracked transaction (README "Anchor cadence"). Nothing is lost; one fee is spent for nothing and never receipted. |
| Tests | `test_stamper_cadence`, `test_stamper_dead_cycle`, `test_stamper_fee_cap`, `test_stamper_wallet_empty` |

### C4. Mined: candidate → waiting for depth

| | |
|---|---|
| Authoritative state | the chain; memory (`txs_waiting_for_confirmation[height]`) |
| Preconditions | a new block holds the latest version of the transaction |
| Side effects | the tree's commitments leave `pending_commitments`; the departure clock is re-armed |
| Acknowledgement point | none outside the process |
| Ambiguous outcomes | a reorg removes the block: the commitments go back to pending (C3 again); a restart forgets the mined tree: its commitments are read as pending again and re-anchored (a second fee, no receipt for the first) |
| Recovery | as stated; `is_pending` answers "Timestamped by transaction …; waiting for N confirmations" while the tree waits |
| Tests | `test_anchor_records`, `test_reorg_detector` |

### C5. Depth reached: save, then receipt

| | |
|---|---|
| Authoritative state | the database, one synchronous batch: the tree's timestamps and the watermark (the lowest journal index still outstanding after this save) |
| Preconditions | `best_height - height + 1 >= --btc-min-confirmations` |
| Side effects, in order | (1) every marker of an *earlier* anchor still on file is settled, each on its own (a failure leaves that marker standing and is logged); (2) this anchor's marker `<receipts>.pending.<txid>` is written atomically (receipt line plus one commitment to probe); (3) the batch is written; (4) the receipt line is appended, every byte checked, file and directory fsynced; (5) this anchor's marker, and only this anchor's, is unlinked; (6) `journal.known-good` is rewritten with the same watermark and the database's generation |
| Acknowledgement point | (3): from here `GET /timestamp` serves the proof and the fill pass skips these entries |
| Ambiguous outcomes | a stop after (2) and before (3): the marker names commitments the database lacks; a stop after (3) before (4): saved, receipt owed, marker standing; a stop after (4) before (5): receipt on file, marker standing; a failed (4): as after (3); a failed (6): the file lags the database (safe: a longer rescan) |
| Recovery | (2)-(3): the marker's probe is absent from the database, so the receipt is discarded ("nothing is owed") and the commitments re-anchor; (3)-(4): the probe is present and the txid is not on file, so the receipt is appended from the marker; (4)-(5): the txid is on file, the marker is removed; a torn last line is dropped before any append and recovered from its marker. A failed save keeps the tree in memory and every pass retries it; a stop before it lands is C4's restart case. |
| Invariants pinned | never two receipts for one txid; never a receipt for an anchor whose save did not happen; a marker is removed only after its receipt's newline is on disk or after it was found not owed; **a later anchor's failure to settle an earlier marker never removes that marker** (2026-09-15 review F08) |
| Tests | `test_receipt_marker`, `test_anchor_receipts`, `test_stamper_save_retry`, `test_stamper_checkpoint` |

### C6. Start: the storage check, then the scan

| | |
|---|---|
| Authoritative state | `db/` (generation, watermark), the journal (length, entries), `journal.known-good` (index, generation) |
| Preconditions for serving | all of: the checkpoint reads as `INDEX` or `INDEX GENERATION`; a generation on file equals the database's; the index is at or below the database's watermark; **the journal holds at least `INDEX` entries, and the entries at `INDEX-1` and at 0 are in the database** (2026-09-15 review F02). A checkpoint without a generation (the format before 2026-09-15) is adopted only if the entry below it is in the database, and rewritten. |
| What the journal check is | a bounded check, two reads and two probes: it catches a missing journal, one truncated below the checkpoint, and one from another lineage that differs at either probed position. It assumes the restore rule (`db/`, `journal`, `journal.counts`, `journal.known-good` from one snapshot). It does not detect an older prefix-identical journal that still reaches the checkpoint (the entries beyond it are lost, and no check can tell), nor one that differs only between the two probed entries. Full journal coherence is the rescan from 0, which deleting the checkpoint asks for. |
| Side effects | on a new database, its generation; on migration, the generation and watermark; nothing else is written before the checks pass, and a missing journal is not created while a checkpoint names an index above 0 |
| Acknowledgement point | the listener is bound only after the checks pass and before any worker thread starts (2026-09-15 review F19): a bind failure exits 1 with nothing running |
| Ambiguous outcomes | none by design: any disagreement stops the process with `CALENDAR STORAGE INCONSISTENT` and the recovery text, exit 1, nothing served, nothing written |
| Recovery | delete the checkpoint and start again: the scan begins at 0 and every entry the database lacks is re-anchored (later blocks than the originals). A journal restored from before the snapshot the database came from has lost the entries between, undetectably: this is why the README asks for one coherent snapshot of the four files. |
| Tests | `test_calendar` (`Test_storage_generation`), `test_otsd_launcher` (`Test_process_boundary`), `test_stamper_checkpoint` |

### C7. Anchored is not irreversible

A saved proof names a block. A reorg deeper than the confirmation depth
removes that block; the proofs already served are then wrong and the
calendar cannot repair them (the commitment path to the new block, if the
transaction is mined again, was never built). The detector reports a
receipted anchor that left the chain or moved height, in the log at ERROR
and in `needs_attention`, until the process restarts; nothing is
re-anchored automatically. This is a stated limit, not a plan: the
confirmation depth is the operator's choice of how much reorg to accept.

## 5. Invariants every contract states and every test class checks

| Invariant | Where it holds here | Where it is checked |
|---|---|---|
| Conservation of obligations | a journal entry is owned by the journal until the database holds it; a receipt is owned by its marker until the line is on file | C1, C5, C6 tests; `test_receipt_marker` |
| Ambiguity is a state | unreadable checkpoint: refused, not guessed; a journal shorter than the checkpoint: refused; a failed record count: unknown, summed as 0; a failed RPC in `GET /`: `best_block` null, not a blank 200; a failed journal read in the watcher: a failed check, cursor kept | C6; `test_anchor_records`; `test_rpc_status`; `test_watch` |
| Recovery is interruptible | the marker settle is one marker at a time and re-runnable; the migration writes the database before the file; the tail recovery is idempotent | `test_receipt_marker`, `test_calendar` |
| Concurrency preserves decisions | one stamper thread mutates the queues; RPC threads read snapshots; the receipts file has one writer; the watcher holds a whole-run lock | `test_rpc_status`, `test_watch` |
| Safety includes progress | a failed save is retried every pass; an unwritable receipts file never blocks the save; a wedged aggregator round is a 503 and then an exit, not a hang; one failed marker never blocks the next anchor's receipt | `test_stamper_save_retry`, `test_aggregator_failure`, `test_receipt_marker` |
| External effects have retry semantics | a transaction's identity is its txid; a replaced version never gets a receipt; a forgotten transaction is never receipted; the ntfy post is at-least-once from a durable outbox | `test_anchor_receipts`, `test_watch` |
| Time, capacity, observation | the departure clock is free-running; a full disk fails the round loudly; `process alive` is not `service healthy` (a dead worker stops the process; a bind failure exits) | `test_stamper_cadence`, `test_aggregator_failure`, `test_otsd_launcher` |
| Anchored is not irreversible | C7 | `test_reorg_detector` |

The same invariants in the self-stamp (section 9):

| Invariant | Where it holds in the self-stamp | Where it is checked |
|---|---|---|
| Conservation of obligations | a manifest without a proof is submitted every run until the file is there; a copy is listed by the next manifest, never dropped; a rejected file is kept | `test_selfstamp_workflow.Test_interruption`, `Test_inbox_faults` |
| Ambiguity is a state | a book that changed under the reader: `unstable`, no digest; a float that did not answer: `error`, no `low`; a proof not of its file: `mismatch`, reported, untouched; a proof not anchored: `pending`, no deadline | `Test_input_stability`, `Test_observation_failures`, `Test_proof_states` |
| Recovery is interruptible | the inbox pass claims, writes the copy, then the proof, then removes; every step re-runnable; a sweep over every rename, replace, unlink and fsync of a fresh run, each case from a fresh fixture, each injection asserted to have fired, each recovery interrupted again | `Test_interruption` |
| Concurrency preserves decisions | one lock over run and upgrade; readers need none, every file is replaced whole; the deliverer is outside the lock and inside the convention | `test_selfstamp.Test_state_lock`, `Test_concurrent_readers`, `Test_publication_convention` |
| Safety includes progress | one unreadable inbox file, directory or copy never stops the other files or the heartbeat | `Test_inbox_faults`, `Test_witnessed_copies_at_run_time` |
| External effects have retry semantics | a lost submission response: the same digest again, deduped or anchored twice, never a second manifest; an upgrade answer cut short: the pending file untouched, asked again | `Test_submission_ambiguity` |
| Time, capacity, observation | the period is a finished UTC day; `created_at` is the box's clock; missed days are gaps; a manifest names no host, path or file name | `Test_period_and_observation`, `Test_amnesia` |

## 6. The proof parser: three claims kept apart

The tools under `ops/` (`selfstamp.py`, `verify_claim.py`) and the client
adapter each carry a standard-library reader of OpenTimestamps proof
bytes. Three claims are made about bytes and never confused:

1. **Parses**: the bytes are one whole detached proof by the rules of the
   public client pinned in the tests (`opentimestamps` 0.4.x). Every byte
   is consumed; every attestation payload is consumed to its end; a
   pending URI is at most 1000 bytes of the characters
   `A–Z a–z 0–9 - . _ / :`; an operand is 1–4096 bytes; no message on any
   path exceeds 4096 bytes; a fork marker is followed by an operation or
   an attestation, never by another fork; at most 255 operations lie on
   any path from the digest to an attestation (the library's recursion
   limit). Two deliberate narrowings, stated as such: the readers accept
   only `sha256`, `append` and `prepend` (all a calendar emits) and
   refuse the other operations the public client knows; and a varuint
   longer than ten bytes is refused where the client would read it.
2. **Contains a Bitcoin attestation**: after (1), some attestation node
   carries the Bitcoin block-header tag. A structural fact about the
   file. The words for it are `bitcoin height=N` (self-stamp) and
   `bitcoin_attestation_present` (adapter). Never "anchored" as a claim
   about the chain, never "verified".
3. **Verifies against Bitcoin**: the path from the exhibit's digest
   replays to the merkle root the attestation names, and the block at
   that height, in an authenticated chain of headers, carries that root.
   Only `verify_claim.py` (with a checkpoint at or after the block) and
   the public client against a node make this claim.

The corpus that pins (1) and (2) is `ops/tests/proof_corpus.py`, run
against each reader and against the pinned library in
`otsserver/tests/test_proof_corpus.py`: accepted shapes, full
consumption, every strict prefix of every valid proof, one trailing
byte, malformed payloads, the size limits at their boundaries, unknown
attestation tags (which parse, and count as no usable attestation), and
the narrowings above. A reader that disagrees with the library on any
case the corpus does not name as a narrowing fails the suite.

## 7. Configuration and locking

Configuration reaches the calendar three ways and no more: command-line
flags parsed by `otsd`, the environment (`BITCOIN_RPC_SERVICE_URL`,
`OTSD_ANCHOR_RECEIPTS`, `OTSD_OPERATOR_LANE`), and the two identity files
in the calendar directory (`uri`, `hmac-key`). The compose file maps its
`.env` onto those; nothing reads `.env` directly. The tools each read one
config file named on their command line (`selfstamp.py --config`) or in
`WATCH_DIR` (`watch.py`), after which a setting missing from it takes
the tool's default; an empty setting means "not configured" and skips the
check that needs it.

Three exclusion tools, each used where it fits and nowhere else:

| Tool | Used for | Where |
|---|---|---|
| whole-run process lock (`flock` on a file in the state directory, held for the run's duration; a second run waits a bounded time, then exits 1 `locked`) | tools that run from a timer and may also be run by hand, whose work is read-modify-write over files. The lock covers the tool's own processes only: whoever delivers files into the self-stamp's inbox is outside it and inside the publication convention (section 9, S8) | `selfstamp.py` (`<state_dir>/.lock`), `export_headers.py` (`headers.bin.lock`), `watch.py` (`<WATCH_DIR>/.lock`) |
| per-object lock inside one process | not used in the calendar: one stamper thread owns every queue, and the RPC threads only read | — |
| database conditional update (one synchronous batch) | the truth the checkpoint depends on: the proofs and the watermark land together or not at all | `LevelDbCalendar.add_timestamps` |

Files that several runs may write are always written under a unique
temporary name in the same directory, fsynced, renamed, and the directory
fsynced.

## 8. Pulled forward: the watcher run (`ops/watch.py`)

The watcher's full observation contract is the next sittings' work; the
three transitions the 2026-09-15/16 review touched are written now.

### W1. One run: observe, decide, persist, deliver

| | |
|---|---|
| Authoritative state | `state.json` (the delivered set, the fail/ok run counts, the journal cursor); `outbox.json` (messages owed) |
| Preconditions | the whole-run lock on `WATCH_DIR/.lock` is held (a second run waits up to `LOCK_WAIT`, then exits 1 `locked`, nothing written) |
| Side effects, in order | (1) observe, every journalctl failure recorded; (2) evaluate and decide; (3) `status` written; (4) the outbox loaded (W2); (5) this run's messages appended and the outbox written atomically; (6) `state.json` written with the cursor: the run's start time if every journal query succeeded, else the cursor the run read from; (7) deliver oldest first, rewriting the outbox after each delivery, stopping at the first failure; exit 1 while anything is undelivered |
| Acknowledgement point | (5): a message is owed from the moment it is on disk |
| Ambiguous outcomes | a stop between (5) and (6): the messages are on disk and the cursor has not moved, so the next run observes the window again and may queue the same burst twice (at-least-once, by design); a stop during (7): delivered messages not yet removed are sent again; the outbox unwritable at (5): the run still tries to send, and the cursor moves only if everything was delivered |
| Recovery | the next run: same lock, same reads; nothing here is lost by a stop, and a burst is never consumed before it is on disk |
| Tests | `test_watch.Test_outbox`, `Test_observation_failure`, `Test_run_lock` |

### W2. Loading the outbox, and its recovery

| | |
|---|---|
| Authoritative state | `outbox.json` |
| Preconditions | the lock is held |
| Outcomes | missing: an empty queue; readable and a list of messages: the queue; unreadable (any error but absence): `OutboxUnreadable`, the run ends with exit 1 and touches nothing; readable but not a list of messages: the recovery below |
| Recovery, in order | (a) the bytes are copied aside as `outbox.json.corrupt-<12 hex of their sha256>`, atomically; (b) a queue holding one notice (the box name, the reason, the byte count, the aside file's name) replaces the corrupt file in one atomic rename |
| Ambiguous outcomes | a stop before (b): the corrupt file is still in place and the next run repeats (a) and (b); the same bytes give the same aside name, so no second copy; a stop after (b): the notice is on disk and owed; at no point is the corrupt file gone while the notice exists only in memory |
| What it does not do | the messages the corrupt bytes held are not recovered; the notice says they may not have been delivered, and the bytes are kept for the operator |
| Tests | `test_watch.Test_outbox_read_failure`, `Test_outbox_recovery_interrupted` |

### W3. Observation failure

| | |
|---|---|
| Authoritative state | the journal cursor in `state.json` |
| Rule | a `journalctl` call that exits nonzero (or times out) reads as no lines and is recorded; `journal_read` is then a failed check (two-run confirmation like the others), and the cursor written at W1 (6) is the one the run read from, so the window is read again next run until every query succeeds. Burst counts from a run with a failed query are not trusted for the cursor; a burst seen by a succeeding query in such a run may be counted again next run, the safe direction. |
| Tests | `test_watch.Test_observation_failure` |

## 9. Workflow 2: the self-stamp (`ops/selfstamp.py`)

Written 2026-09-16, workflow two. The words used here, beyond the ones
above:

- **Manifest**: one JSON file, `<state_dir>/manifests/<period>.json`: the
  box's own books hashed for one finished UTC day, chained to the previous
  manifest by `prev.sha256`. Its bytes are what is stamped, and they are
  never rewritten.
- **Period**: the UTC day a manifest names, and its file name. It is the
  journal window; it is not when the books were looked at (that is
  `created_at`).
- **Label**: `chain`, 32 hex digits drawn at random when a chain begins,
  the same on every manifest after (selfstamp/3). A manifest under an
  older schema has a host name instead; a witness records such a source
  with `chain: null`.
- **Copy**: a foreign manifest retained under `<state_dir>/witnessed/`,
  byte for byte: the record behind a vouch.
- **Companion**: a `.json.ots` delivered for a foreign manifest, the
  source's own proof of it.
- **Vouch, entry**: one element of a manifest's `witnessed` list: this
  chain's statement that it holds a copy with that sha256.
- **The lane**: `POST /operator/digest` (section 1): aggregated and
  anchored like a client digest, never a record.

### Records

| Record | Kind | Authoritative for | Loss or damage |
|---|---|---|---|
| `manifests/<period>.json` | authoritative | the chain: what was observed, when, after what | never rewritten by the tool; an altered byte breaks the successor's `prev.sha256` and the manifest's own proof (`mismatch`); a deleted day is a gap `verify` reports; deleted newest days are invisible to the chain (the cadence or a witness shows them) |
| `manifests/<period>.json.ots` | evidence | the calendar's commitment to those bytes, pending or with a Bitcoin attestation | missing: resubmitted next run (a later anchor); malformed, or not of the file beside it: reported every run, never touched (S5) |
| `witnessed/<copy>.json` | authoritative for the vouch | the bytes this box vouches for | missing or unreadable: the vouch is a break (S9); the tool never removes a copy |
| `witnessed/<copy>.json.ots` | evidence | this box's own stamp of the copy | as a manifest's proof |
| `witnessed/<copy>.json.foreign.ots` | evidence, replaceable | the strongest proof the source delivered for the copy | a proof with a Bitcoin attestation replaces a pending one, never the reverse; one that does not read is replaced |
| `<inbox>/.claim-<8 hex>-<name>` | the run's, in flight | a delivery taken into the tool's keeping before it is read (S6); a stale claim from a run that died is resumed by the next run | consumed by the tool and by nothing else; a deliverer never writes such a name |
| `<inbox>/rejected/<12 hex>-<name>` | quarantine | bytes the inbox could not use, kept durably (bytes fsynced, then the rename, then both directories) under a name derived from them and the delivered name | the operator's to read and remove; nothing else touches it |
| `witnessed/<copy>.json.foreign.ots.rejected-<12 hex>` | quarantine | a held foreign proof found not to be a proof of its copy, set aside when a proof of the copy arrived (S6) | the operator's |
| the outbox | rebuildable export | nothing: rewritten from `manifests/` every run | delete it and the next run exports again |
| `<state_dir>/.lock` | coordination | this tool's own processes | not the deliverer's (S8) |
| the log (`selfstamp.log`) | diagnostic | the run's own account, one `summary` line per run | nothing is derived from it |

### Who owns unfinished work

| Handoff | Before | After | Owner in between | Evidence that lets the previous owner forget |
|---|---|---|---|---|
| clock → run | a period is due | the run holds the lock | the timer (`Persistent=true` fires a missed run at boot); a missed day is a gap, never backfilled | none needed |
| books → manifest | files on disk | the manifest's entries | the run, for one read per file; a file that moved under the reader is `unstable`, not owed | the manifest on disk |
| run → manifest file | bytes in memory | `<period>.json` renamed into place, its directory fsynced | the run: a stop before the rename leaves nothing (a dotted temporary at most) and the next run builds the period again | the file: from here the period exists and is never rebuilt |
| manifest → lane | a manifest without a proof | `<name>.json.ots` on disk | the run, every run, until the proof is on disk; the calendar owns the commitment from its journal fsync (C1), acknowledged or not | the proof file |
| pending → attestation present | a pending proof | the proof replaced whole | the calendar (its anchor); the run only asks, once per run | the upgraded file; a complete proof is never asked about again |
| deliverer → inbox | a file being written | a file under its final name | the deliverer, until the rename into place (S8: a name the tool ignores until then); the deliverer may rename another file over that name later, and it is a new delivery | the rename |
| inbox → claim | a file under its final name | `.claim-<token>-<name>` | the run, from the atomic rename on: what a deliverer puts under the name afterwards is the next run's | the rename; a stale claim is resumed, not lost |
| claim → copy | a claimed manifest | `witnessed/<copy>.json` on disk | the run; the claim stays until the copy is durable | the copy; the claim is then a duplicate |
| companion → foreign proof | `<name>.json.ots` in the inbox | `<copy>.json.foreign.ots`, or `rejected/` | the run | the file gone from the inbox |
| copy → vouch | a copy no manifest lists | an entry in the next manifest | the copies directory: the unlisted set is re-derived from the manifests every run | the manifest that lists it |
| manifests → outbox | files | byte-identical files in the outbox | the run, every run | nothing: rebuildable |
| outbox → another box | files | files in its inbox | the operator's transport; not this tool | the other chain's next manifest, cross-checked (S9) |

### S1. One run

| | |
|---|---|
| Authoritative record | the state directory as a whole; `<state_dir>/.lock` coordinates this tool's processes |
| Preconditions | the lock (`flock`, waited for up to `--lock-wait`, then exit 1 `locked`, nothing written); `manifests/` made, durably (a failure: `refused … state directory unusable`, exit 1); a period that is over: by default the UTC day before the run's clock; `--period` names another and is refused when it has not ended (`refused … not finished`) or is not after the newest manifest, which the one validator must accept (`refused … latest manifest unreadable`) |
| Side effects, in order | (1) the inbox pass (S6); (2) the manifest, unless the period's file exists (`noop`): the unlisted copies gathered (S7), the observations made (S2), the file written whole (S3); (3) the submit pass over `manifests/`, then `witnessed/` (S4); (4) the upgrade pass over both (S5); (5) the export (S8); (6) one `summary` line |
| Visibility point | each file as it is renamed into place; the log as it is written |
| Acknowledgement point | the exit code the timer reads: 0 when every step of this run's own work is done, 1 when a step failed and is left for the next run or the operator. A pending proof is not a failure: it is counted in the summary and asked about next run, with no deadline. The exit code and proof completion are read apart by design. |
| Ambiguous outcomes | none for the run itself: every step is idempotent and the next run repeats whatever is not on disk. What is ambiguous is the calendar's side (S4). |
| Recovery | the next run: same lock, same passes; nothing is derived from the previous run's log or exit code |
| Postconditions tested | `test_selfstamp.Test_run_heartbeat`, `Test_state_lock` (two real processes on the lock); `test_selfstamp_workflow.Test_period_and_observation`, `Test_proof_states` (the summary against the exit code), `Test_interruption` (a child killed after the manifest, after the calendar's answer, after a copy) |
| Assumptions | the timer's `Persistent=true`; one state directory per chain; the clock is the box's |

### S2. The observations

| | |
|---|---|
| Authoritative record | the manifest's `books`, `audit_logs`, `journal`, `fork_head`, `float` and `config` entries; each is one observation made in this run, after `created_at` and before the file was written |
| Preconditions | none: an input that is missing, unreadable, unstable or unanswered is recorded as such and never stops the run |
| Side effects | reads only: one open and one pass per file; one `journalctl` call; one `GET /` on loopback; `.git/HEAD` by file |
| What a digest promises | the file's bytes did not observably change while they were read: the path named the same inode afterwards, and size, mtime and ctime from the open descriptor agreed before and after with the bytes counted. Otherwise the entry is `{"unstable": why}` and carries no digest: an append, a truncation, a rewrite in place, a replacement of the path, a removal (2026-09-16 workflow two: a file that grew under the reader used to get the digest of a prefix, unmarked) |
| What it does not promise | that the file was not rewritten between the two metadata reads inside one timestamp tick; that the files of a directory belong to one moment (each is read on its own; the directory's entry is `unstable` only when the listing itself changed); that an append-only book's prefix was recorded (it is not: the book is `unstable` that day). Books should be immutable exports, or files with a snapshot boundary the operator controls; a live log is recorded when it holds still and named unstable when it does not. |
| Time | `period` is the journal window and the file name; `created_at` is the run's clock. A run made days after its period hashes the books as they are then and says so. No manifest is written for a day the tool did not see: missed days are a gap the chain links across, and a day not yet over is refused. |
| The configuration | `config.sha256` is the sha256 of the config file's bytes as `load_config` read them, or of the canonical JSON of a config handed over as a dict; never a later reread (2026-09-16 workflow two: the file used to be reread when the manifest was built) |
| Recovery | none needed: the next day's manifest is a new observation |
| Postconditions tested | `test_selfstamp_workflow.Test_input_stability` (append, truncation, rewrite, replacement, removal, a directory that changed: a hook inside the read loop plays the writer), `Test_configuration_fingerprint`, `Test_observation_failures` (a failed journal query, an unanswered float); `test_selfstamp.Test_audit_logs`, `Test_float`, `Test_inputs` |

### S3. Publishing the manifest

| | |
|---|---|
| Authoritative record | `manifests/<period>.json` |
| Preconditions | S1's; the newest manifest read whole and parsed (one that cannot be read or is not a manifest: `refused`, exit 1, nothing written); `seq` = its `seq` + 1, `prev` = its name and sha256, `chain` = its label, or a new label when it has none |
| Side effects | one file: a dotted unique temporary in `manifests/`, fsynced, renamed, the directory fsynced |
| Visibility point | the rename: a reader sees the old set of files or the new one, never a partial file |
| Durability point | the directory fsync after the rename; the first creation of `manifests/` (as of `witnessed/`, `rejected/` and the outbox) fsyncs the parent entry too. Visible is the rename, durable is the fsync, and the two are not the same instant (the tests' fault model is injected exceptions and a killed process, not a power cut) |
| Ambiguous outcomes | a stop before the rename leaves at most a dotted temporary that no reader of `*.json` sees (residue, not cleaned) and the next run builds the period again; a stop after it is the period done; an fsync that fails after the rename leaves a file that is visible and whose durability is not known: the run logs `manifest write failed`, exits 1, and the next run takes the file it finds as the record |
| Recovery | the next run finds the file and says `noop` |
| Invariants | one manifest per period, ever; a manifest is never rewritten; `seq`, `prev` and `chain` come from the file that is there |
| Postconditions tested | `test_selfstamp.Test_run_heartbeat`, `Test_state_lock.test_atomic_writes_use_unique_names_and_fsync_the_directory`; `test_selfstamp_workflow.Test_concurrent_readers` (verify between the manifest and its proof), `Test_interruption` |

### S4. Submission through the lane

| | |
|---|---|
| Authoritative record | `<name>.json.ots` beside the manifest or the copy |
| Preconditions | a `*.json` in `manifests/` or `witnessed/` without a `.json.ots` beside it |
| Side effects, in order | (1) `POST /operator/digest` with the file's sha256; (2) the response parsed into a detached proof of exactly that digest; (3) the proof file written whole |
| Acknowledgement point | (3); the calendar's own acknowledgement point is its journal fsync (C1) |
| Ambiguous outcomes | the calendar committed the digest and the response was lost (a timeout, a connection closed): no proof file, `submit failed`, exit 1. A response that is not a proof of the digest, or a proof file that could not be written or fsynced: the same, except that a proof visible after a failed fsync is taken as there by the next run. In every other case the next run submits the same digest again: inside the calendar's dedupe horizon it is the same commitment, outside it a second one (an uncounted leaf, never a record). Never a second manifest. |
| Recovery | the next run, by the absence of the file; the manifest is never rewritten to get a new proof |
| Postconditions tested | `test_selfstamp.Test_run_heartbeat.test_a_manifest_without_a_proof_is_resubmitted_not_rewritten`, `test_calendar_down_keeps_the_manifest_and_exits_nonzero`; `test_selfstamp_workflow.Test_submission_ambiguity` (the fake commits and closes the connection), `Test_interruption` (a child killed between the answer and the write) |

### S5. Upgrade, and the proofs that are not right

| | |
|---|---|
| Authoritative record | the proof file, replaced whole when upgraded |
| Preconditions | the proof parses; it is a proof of the file beside it; its attestation is pending |
| Side effects | one `GET /timestamp/<commitment>` per pending proof per run; on a 200 the pending attestation is spliced out for the calendar's path, the result checked to be a complete linear proof of the same digest, the file replaced whole |
| Acknowledgement point | the rename; a proof with a Bitcoin attestation is never asked about or touched again |
| States | `missing` (S4's case), `pending`, `bitcoin height=N` (an attestation is present: section 6, claim 2, never claim 3), `malformed` (this reader cannot parse it), `mismatch` (a proof, not of the file beside it), `unknown` (an attestation this tool does not read), `unreadable`, and an orphan proof with no manifest beside it |
| Ambiguous outcomes | a 404 is `pending`, reported, never a failure and never a deadline: a proof not yet anchored stays visibly outstanding for as long as it takes. A response cut short or without a Bitcoin attestation: `upgrade refused`, the pending file untouched, exit 1, asked again next run. |
| Recovery | `malformed`, `mismatch`, `unknown`, `unreadable`, orphan: reported every run with exit 1 and never touched (2026-09-16 workflow two: a `mismatch` used to be upgraded without a word, and nothing but `verify` saw it). `malformed` is this reader's verdict, not a fact about the proof (a proof the ots client upgraded against several calendars carries a fork this reader refuses), and only the operator can say whether the manifest or the proof is the damaged one: the successor's `prev.sha256` says whether the manifest's bytes changed. The operator moves the proof aside; the next run stamps the manifest as it is now, under a later block (README, "Recover"). |
| Postconditions tested | `test_selfstamp.Test_run_heartbeat.test_upgrade_writes_only_the_proof`; `test_selfstamp_workflow.Test_proof_states` (a mismatch never upgraded, never fetched, never resubmitted; a malformed proof left in place; pending in the summary with exit 0), `Test_submission_ambiguity.test_a_truncated_upgrade_answer_leaves_the_pending_proof_untouched` |

### S6. The inbox

| | |
|---|---|
| Authoritative record | `witnessed/<copy>.json` (the record), `<copy>.json.foreign.ots` (the source's proof), `<inbox>/.claim-…` (deliveries in the tool's keeping), `<inbox>/rejected/` (what could not be used) |
| Preconditions | `inbox` configured and a directory (missing: `inbox missing`, exit 1, the heartbeat goes on; unreadable: `inbox unreadable`, the same; neither message names the path); `witnessed/` made durably; files under their final names: `*.json` and `*.json.ots` not beginning with a dot (S8) |
| Side effects, in order | (1) **claim**: every delivery under a final name is renamed, atomically, to `.claim-<8 hex, one token per run>-<name>`, before any of it is read. From the rename the file is the tool's; a deliverer that renames another file over the same name afterwards has made a new delivery for the next run, instead of having it removed under it (2026-09-16 gate review, P1: the consumer read A, copied it, and unlinked the name B had meanwhile been published under). A claim that cannot be made is `inbox error`, counted, and the file stays under its name. (2) per claimed `*.json`, with its companion the claim of the same token and name or, when a stop claimed the two apart, the claim of the same delivered name by any run: (a) not a manifest by the one validator: the companion, then it, moved to `rejected/<12 hex of its sha256>-<delivered name>` (the companion first, so a stop between the two leaves the manifest to be found again), logged `inbox rejected`; (b) bytes already held, by content, whatever the copy's name: the companion consumed (below), the claim removed, logged `inbox duplicate`; (c) new: the copy written whole as `<label or legacy>-<period>-<12 hex>.json`, then the companion consumed, then the claim removed, logged `witnessed chain=… seq=…`. (3) per claimed `*.json.ots` left alone: not a proof: `rejected/`; a proof of a copy held: kept by the rule below, removed, logged `inbox foreign proof`; a proof of nothing held whose manifest's name sits in `rejected/`: `rejected/` too, `its manifest was rejected`; a proof of nothing held otherwise: left as a claim and logged `awaiting its manifest` on every run, with no timeout. (4) the inbox directory fsynced, so the removals are durable before the pass returns. |
| The companion rule | kept as `<copy>.json.foreign.ots` when it is a proof of exactly the copy's bytes and says more than what is held: nothing held, or pending held and this one carrying a Bitcoin attestation; removed when it says less; moved to `rejected/` when it is not a proof of the copy (2026-09-16 workflow two: it used to be deleted, F14's residue). What is held counts only if it is itself a proof of the copy: a held file that is not (a Bitcoin attestation for other bytes among them) is set aside beside the copy as `<copy>.json.foreign.ots.rejected-<12 hex>`, logged `foreign proof set aside`, and never outranks a proof that is (2026-09-16 gate review, P2: it used to be ranked on its attestation alone, and the right proof was removed). |
| Quarantine, durably | the file's bytes fsynced (a deliverer may not have), the rename into `rejected/`, then `rejected/` and the inbox fsynced; a new `rejected/` has its entry in the inbox fsynced before anything is moved into it; the log line follows the last fsync (2026-09-16 gate review, P5: the rename used to be acknowledged with no barrier). The same holds for a held proof set aside. |
| Visibility point | each rename; the copy exists before anything about the delivery is removed |
| Ambiguous outcomes | a stop after a claim: the claim is resumed next run, whatever token it carries; a stop after the copy and before the removals: the next run finds a duplicate and finishes; a stop after the companion's quarantine and before the manifest's: the manifest is found again alone; a stop between the foreign proof and the removals: a duplicate whose companion `held` what it already holds; an fsync that fails after a rename: visible, durability unknown, counted as a failure, and the next run takes what it finds |
| One defective item | a file that cannot be read, a rename or an fsync that fails, is logged `inbox error` with the class and errno (never the path), counted (exit 1) and left for the next run; the other files and the heartbeat are not stopped (2026-09-16 workflow two: one unreadable file used to end the run before the manifest). An inbox directory that cannot be listed is one failure. Two deliveries under one name with different bytes are both kept: the quarantine name is by content. Two different manifests claiming one label and seq are both copied and both vouched for; `verify --witness` says so (S9). |
| Recovery | the next run: the same claims resumed, the same reads, the same rules. A file caught half written under its final name is quarantined as malformed, not lost; the convention (S8) is what prevents it. |
| Postconditions tested | `test_selfstamp.Test_witness`, `Test_witnessed_copies`, `Test_witness_delivery`, `Test_malformed_foreign_proof`; `test_selfstamp_workflow.Test_inbox_faults` (a rejected companion kept, a late proof stored, a proof awaiting its manifest, a malformed orphan, an unreadable file and directory, conflicting deliveries, a delivery replaced under its name while held, a claim that cannot be made, a claim left by a dead run, a held proof of other bytes set aside, the quarantine's syscalls in order), `Test_publication_convention`, `Test_interruption` (every rename, replace, unlink and fsync of a fresh run with one pair and with one bad pair, each from a fresh fixture, each injection asserted fired, each recovery interrupted again; a child killed after the copy) |

### S7. Folding the vouches

| | |
|---|---|
| Authoritative record | the `witnessed` list of the manifest that first lists a copy; the copies directory until then |
| Preconditions | a manifest is being built (S3); the copies directory exists |
| Rule | every copy that no manifest of this chain lists by file name is listed now, with the source's label (`null` for a source under an older schema: its host name is not republished), `seq`, `period`, the copy's name and sha256, when the copy was written, and what the foreign proof said at that moment (`null`, `pending`, `bitcoin height=N`, `unreadable`) |
| Side effects | reads only; the entries are part of the manifest's bytes (S3) |
| Ambiguous outcomes | a copy made after the period's manifest was written is listed by the next period's; a copy that cannot be read or no longer parses is named (`witnessed copy unreadable`), counted (exit 1), not listed, and tried again next run (2026-09-16 workflow two: an unreadable copy used to stop the run before the manifest, and a corrupt one was skipped in silence) |
| What an entry keeps | what was known when it was written: a foreign proof that arrives or strengthens later is stored beside the copy and shown by `verify` as `foreign_now`; the entry is never rewritten |
| Postconditions tested | `test_selfstamp.Test_witness.test_inbox_is_consumed_exactly_once_and_stamped_through_the_lane`; `test_selfstamp_workflow.Test_witnessed_copies_at_run_time`, `Test_interruption` (vouched exactly once across the manifests, whichever run made the copy) |

### S8. Export, and the publication convention

| | |
|---|---|
| Authoritative record | none: the outbox is rebuilt from `manifests/` every run |
| Rule | every manifest as `<label>-<period>.json` (a manifest under an older schema keeps the `<host>-<period>` name its export already has); its proof as `<label>-<period>.json.ots` once, and only once, it is a proof of the manifest's bytes carrying a Bitcoin attestation (a pending proof names a loopback calendar nobody else can reach; a proof of other bytes, or one this reader cannot parse, is withheld and logged `export withheld`: 2026-09-16 gate review, P2, attestation presence alone used to let it travel as this manifest's); byte-identical files untouched; each file written whole |
| The convention, out | a reader of the outbox may copy any file it sees: every file was renamed into place whole. Dotted names are temporaries and not for delivery. The manifest and its proof are two files, and the proof may follow days later. |
| The convention, in | the lock does not cover the deliverer. A file is published into the inbox by writing it under a name the tool ignores (a leading dot, or any suffix but `.json` and `.json.ots`) and renaming it into place; names beginning with `.claim-` are the tool's and a deliverer never writes one. A name may be used again: the tool claims a delivery before reading it, so a file renamed over a name the run holds is a new delivery for the next run. A file written in place under its final name is read whenever the run comes; caught half written, it is quarantined as malformed (kept, logged, exit 0) and the source must deliver it again. No transport is provided or assumed; there is no listener. |
| Ambiguous outcomes | a stop mid-export leaves some files exported and the next run exports the rest; a manifest that cannot be read or parsed is logged `export failed`, counted, skipped |
| Postconditions tested | `test_selfstamp.Test_witness.test_outbox_receives_manifests_and_anchored_proofs_only`; `test_selfstamp_workflow.Test_publication_convention`, `Test_amnesia` (the export names), `Test_legacy_compatibility` (older names kept) |

### S9. Verify and cross-check

| | |
|---|---|
| Authoritative record | the files as they are now; verify writes nothing and needs no lock |
| Validation | every file verify walks passes the one validator (a schema this tool reads, an integer seq from 1, a date for a period, a 32-hex label under selfstamp/3 or a host under the older schemas): one that fails it is `BROKEN` with the validator's reason, and, when it is still a JSON object, its position is checked as well; one that is not is `unreadable json`. The same validator reads a delivery at the inbox and the predecessor at continuation (2026-09-16 gate review, P4: three readers used to agree on less). |
| Chain checks | each manifest names its predecessor by file and sha256; `seq` counts from 1 without gaps; periods increase and match file names; the label, once a manifest has one, never changes and is never missing again; a selfstamp/2 genesis carries its commissioning block and no later manifest does |
| Proof states | as S5: `missing` and `pending` are reported, not breaks; every other state but `bitcoin` is a break; a proof file that cannot be read is a break |
| Vouch states | copy `ok`, `missing`, `unreadable`, `MISMATCH` (every one but `ok` a break), or `SKIPPED` under `--skip-witnessed`, labelled on every line and in the summary; this box's proof of the copy, as S5; `foreign_proof` as recorded, `foreign_now` as held (`pending`, `bitcoin height=N`, `malformed`, `mismatch`, `unreadable`) (2026-09-16 workflow two: an unreadable copy or manifest used to end verify with a traceback) |
| Cross-check (`--witness DIR`) | every witness manifest is read by the validator; one that cannot be read or is not a manifest is named (`witness manifest unreadable`), the rest are still used, and the check is incomplete: its result is False, because an absence found in evidence that could not all be read is not established (2026-09-16 gate review, P3: such files used to be skipped and the check passed). For each manifest here: an entry with the same sha256 is `witnessed by`; failing that, an entry under the same identity with another hash is `WITNESS MISMATCH`, a break, when the identity is a label and seq, and `WITNESS AMBIGUOUS`, reported and not a break, when it is seq and period alone (a manifest under an older schema: another version of it, or another unlabelled chain that began the same day, and only a label tells them apart; gate review, P3: it used to be called a mismatch); no entry is `not witnessed`, reported; several versions under one identity are said. A witness directory that is not there or cannot be listed is a break: nothing was checked. The check's last line is `witness check=` followed by `ok`, `BROKEN` or `incomplete`, with the counts. No message names a path. |
| Exit semantics | 0 when the chain, every proof present and every vouch hold, and the witness check, if asked for, was complete and found no mismatch, with any number of proofs pending or missing and any number of manifests not witnessed or ambiguous; 1 on any break or an incomplete witness check. The chain summary counts `bitcoin`, `pending`, `missing` and any bad state, and every report says that an attestation present is not checked against Bitcoin here. |
| Legacy | selfstamp/1 and /2 manifests verify as written: `host`, paths and names are printed as they are (`commissioned host=…`; `host=` on a vouch line an older witness wrote) and never rewritten; a selfstamp/3 genesis prints `genesis chain=… at=… fork=… config=…` |
| Postconditions tested | `test_selfstamp.Test_verify_chain`, `Test_witnessed_copies`, `Test_commissioning`; `test_selfstamp_workflow.Test_verify_states` (an unreadable copy, proof and manifest; a missing witness directory; the states named; witness evidence that cannot all be read), `Test_legacy_compatibility` (the selfstamp/2 corpus verified, continued and witnessed; an unrelated legacy chain ambiguous, a labelled one a mismatch), `Test_amnesia` (the genesis line; messages without paths; a label that is not 32 hex refused at every seam), `Test_period_and_observation` (a predecessor with an unknown schema refused) |

### The manifest, field by field (selfstamp/3)

| Field | Meaning | Source | Disclosure | Retention | Verification purpose |
|---|---|---|---|---|---|
| `schema` | the format, `selfstamp/3` | constant | none | forever, in the stamped bytes | which rules verify applies |
| `chain` | the chain's opaque label, 32 hex digits and nothing else (anything else in the field is refused at every seam) | drawn at random at the genesis (or at the first manifest under this schema of an older chain), then copied from the previous manifest | an identifier of this chain and nothing else; it correlates manifests and vouches | forever | continuity; the witness's key |
| `period` | the UTC day covered: the journal window and the file name | the run's clock (yesterday) or `--period`; never a day not yet over | a date | forever | ordering; the journal query |
| `created_at` | when the run began the manifest, UTC; every observation followed within the run | the run's clock | a time | forever | the observed time; the proof's block is the proven bound |
| `seq` | 1, then +1 per manifest, no gaps | the previous manifest | a count | forever | gaps |
| `prev` | `null`, or the previous manifest's file name and the sha256 of its bytes | the previous manifest | a date and a hash | forever | the chain link |
| `config` | `{"sha256"}` of the configuration this run used | the bytes `load_config` read, or the canonical JSON of a dict config | a hash | forever | which configuration produced this manifest |
| `books` | per operator key: `{"sha256","bytes"}`, `{"missing": true}`, `{"unstable": why}` or `{"error": class and errno}` | one read per file | hashes and sizes under the operator's keys; no path | forever | the file as it stood, when it held still |
| `audit_logs` | `null`, or per key: a file as `books` plus `mtime`; a directory as `{"files": [{"sha256","bytes","mtime"}…], "skipped": {reason: count}}`, sorted by the entries' JSON, plus `unstable` when the listing changed | one pass over the directory | hashes, sizes, times, counts; no directory path, no file name | forever | a file with exactly these bytes stood there |
| `journal` | `null`, or `{"since","until","command","sha256","bytes"}`, or the same with `error` in place of the digest | one `journalctl` call, at the run | the command and a hash | forever | reproducible by whoever holds the day's journal |
| `fork_head` | `null`, `{"ref","commit"}`, `{"missing": true}` or `{"error"}` | `.git/HEAD` by file | a branch name and a commit | forever | which calendar code ran |
| `float` | `{"source","low_below_sats","balance_sats","low"}` or `{"source","low_below_sats","error"}` | `GET /` on loopback | the anchor wallet's confirmed balance | forever | the box's ability to anchor that day; unknown is not zero |
| `witnessed` | entries `{"chain","seq","period","file","sha256","witnessed_at","foreign_proof"}` | the copies not yet listed (S7) | the source's label (or `null`), its seq and period, the copy's opaque name and hash, a time, a state word | forever | the vouch |

Under selfstamp/1 and /2 a manifest also carried `host` (the box's
hostname, or a configured name), a `path` in every book entry, `dir` and
file `name`s under `audit_logs`, `path` under `fork_head`, and (/2,
genesis only) `commissioning: {host, installed_at, fork_commit, config:
{path, sha256}}`; entries under `witnessed` carried the source's `host`.
Those bytes are read, verified, continued and witnessed as they are. They
are never rewritten, and they do not satisfy the naming rule above: what
they disclosed stays disclosed.

### Legacy and witness compatibility

- **Reading.** `verify`, `run` and the inbox read all three schemas. An
  older chain continued under this code gets a label at its first
  selfstamp/3 manifest and keeps it; its `prev` links hold across the
  change; its older exports keep their `<host>-<period>` names.
- **A legacy source witnessed here.** Recorded with `chain: null`, a copy
  named `legacy-<period>-<12 hex>.json`, a log line `witnessed
  chain=legacy`. Its host name is inside the copy (the source wrote it
  there) and nowhere the witness writes. Cross-check matches it by
  sha256, or by seq and period; another hash found there is `WITNESS
  AMBIGUOUS`, reported and not a break, since without a label a witness
  cannot tell another version of the manifest from another chain that
  began the same day. A label ends the ambiguity.
- **A labelled source at an older witness.** Refused by schema, moved to
  that witness's `rejected/`, logged, nothing lost. Both boxes run this
  version or newer.
- **Entries an older witness wrote** (with `host`) are verified against
  the copies named by host that it made, and printed with `host=`.
- **The `host` config key** is not written since selfstamp/3; a run with
  it set logs one line saying so, without the value.

### Assumptions and limits

- Hashing is an observation, not a snapshot. Unchanged metadata within
  one timestamp tick does not prove no rewrite; a directory's files are
  read one at a time; an append-only book that grows under the reader is
  `unstable` that day, not recorded as a prefix. The remedy is the
  operator's: immutable exports, or a snapshot boundary this tool is
  pointed at.
- Delivery is the convention above and nothing else; the tool cannot
  tell a half-written file under a final name from a malformed one.
- Time is the box's clock, in `created_at`; the proven bound is the
  proof's block; nothing records when the software was installed.
- Fault model: injected exceptions at named calls, a child killed at a
  named boundary, two real processes on the lock, a fake calendar that
  drops or truncates an answer. No power cut; nothing run on Linux this
  sitting (README, "Tests").
- Residue: a run killed between a temporary's creation and its rename
  leaves a dotted file that nothing reads and nothing removes.
- Messages: no message this tool writes names a path. The delivered name
  of a file (kept in `rejected/` and printed in the log) and the names
  inside a manifest written under an older schema are the sender's,
  retained as delivered, not produced here.
- Sweeps: the boundaries exercised are the rename, replace, unlink and
  fsync calls of a fresh run with one delivery pair (2, 5, 2 and 13 of
  them) and with one bad pair (2, 4, 0 and 14); the lock, the temporary's
  creation and the calendar's answers are covered by their own cases,
  not by a sweep.

## 10. Not yet written

The rest of the watcher's observation contract, and the restore and
migration paths as workflows of their own, are the next sittings' work;
they will be added here in the same shape as sections 4 and 9.
