# Contracts

What each part of the calendar promises, who owns unfinished work at each
handoff, what durable evidence lets a part forget, and which records are
authoritative. Every statement here is made by code named in the README
section it cites; a change to any of them changes this file first
(CONTRIBUTING.md). Written 2026-09-16 against the 2026-09-15/16 reviews.

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
| `POST /operator/digest` | same as `/digest` | The same, and that the leaf is not a record. | — |
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
| whole-run process lock (`flock` on a file in the state directory, held for the run's duration; a second run waits a bounded time, then exits 1 `locked`) | tools that run from a timer and may also be run by hand, whose work is read-modify-write over files | `selfstamp.py` (`<state_dir>/.lock`), `export_headers.py` (`headers.bin.lock`), `watch.py` (`<WATCH_DIR>/.lock`) |
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

## 9. Not yet written

The self-stamp workflow, the rest of the watcher's observation
contract, and the restore and migration paths as workflows of their own
are the next sittings' work; they will be added here in the same shape
as section 4.
