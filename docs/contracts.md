# Contracts

What each part of the calendar promises, who owns unfinished work at each
handoff, what durable evidence lets a part forget, and which records are
authoritative. Every statement here is made by code named in the README
section it cites; a change to any of them changes this file first
(CONTRIBUTING.md). Written 2026-09-16 against the 2026-09-15/16 reviews;
section 9, the self-stamp (workflow two), and section 8 as the watcher's
full contract (workflow three) added the same day; section 10, restore
and migration (workflow four), on 2026-09-17; the 2026-09-18 cold
review's corrections (R01, R06, R09, R10, R11, R12, R17, R18) the same
day, each named where it changed a table.

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
| `journal.counts` | auxiliary | Occurrence counts per journal entry, written after the entry is durable. | A lost count is an undercount, warned once per tree, never an overcount. A sidecar that counts more entries than the journal holds is refused at start: the journal is the older of the two (C6, R1). |
| receipts file (`OTSD_ANCHOR_RECEIPTS`) | authoritative accounting | One line per saved anchor: what it cost and how many occurrences it carried. The gateway's billing reads it. | Append-only; a torn last line is dropped and recovered from its marker. |
| `<receipts>.pending.<txid>` | durable evidence of work in progress | A receipt owed for an anchor whose calendar save is under way or done. Settled by asking the database for the anchor's own key, its txid node, which only its saved tree carries: no later anchor over the same commitments answers for it (2026-09-18 gate review, G1). | One marker per anchor. A marker is removed only by the code that settled it or wrote its receipt; a later anchor never touches an earlier anchor's marker (C5). The single-name marker from before 2026-09-16 (`<receipts>.pending`) is still read and settled. |
| in-memory: `pending_commitments`, `unconfirmed_txs`, `txs_waiting_for_confirmation`, `unprocessed_blocks` | rebuildable | Nothing across a restart. `unprocessed_blocks` is the blocks whose headers the stamper has seen and whose bodies it has not yet read (C4). | A restart rebuilds pending from the journal; an in-flight or mined-but-shallow anchor is forgotten and its commitments are re-anchored in a fresh transaction (the forgotten anchor is never receipted; its fee is spent and unaccounted); the block queue is forgotten with the trees it would have invalidated. |
| the wallet (Bitcoin Core) | external | What was broadcast and what confirmed. | The stamper assumes exclusive use of it. |

## 3. Who owns unfinished work

| Handoff | Before | After | Owner of the work in between | Evidence that lets the previous owner forget |
|---|---|---|---|---|
| client → aggregator | the client holds bytes | the digest is in the round's queue (memory) | nobody durable: a crash here loses the request and the client gets no 200 | none needed: no promise was made |
| aggregator → journal | round queue | journal entry fsynced | the journal | the fsync returning; only then `done_event` fires and the 200 goes out |
| journal → stamper | journal entry | `pending_commitments` (memory) | the journal, still: the stamper's memory is a view rebuilt at every start from the checkpoint onward | never: the journal is the record until the database holds the commitment |
| stamper → Bitcoin | pending | `unconfirmed_txs` (memory) and the wallet | the journal still owns the obligation; the wallet owns the transaction | none: a restart forgets the transaction and re-anchors |
| Bitcoin → stamper | the headers seen, as one discovery (`known_blocks`: advanced with the list it returns, or not at all) | the block's body read and every tree waiting at its height put back to pending (`unprocessed_blocks` emptied); a mined tree in `txs_waiting_for_confirmation[h]` (memory) | the block queue, in memory, until the body is read: no tree is called mature while a block is owed; the journal still owns the commitments | none until the save |
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
| Preconditions | the aggregator loop is alive and has not failed; `Content-Length` is 1–64 and the body arrives whole: the bytes read are the digest, and a body shorter than declared is a 400 with nothing committed (2026-09-18 cold review R17: the shorter body used to be aggregated and acknowledged as a digest of its own length) |
| Side effects | one journal entry per second with traffic, fsynced; the count sidecar entry (best effort); the pending timestamp in the response |
| Acknowledgement point | the 200 is sent after `Journal.submit` returns from `os.fsync` |
| Ambiguous outcomes | 503 after a 30 s round timeout (may still commit); client timeout after the fsync (committed, unacknowledged); an aggregator round that raises: the process exits 1 and the round's commitment may or may not be in the journal (a failed fsync after the write, a partial write padded to a record at the next start, or nothing), and nothing about the failure point is known to the client. Not ambiguous: a peer that closes before its declared body has arrived gets a 400 and owes and is owed nothing |
| Recovery | the journal needs none: whatever it holds is anchored at the next start, asked for or not; the client resubmits and is deduped inside the horizon (across the restart it is a new commitment, counted again); the supervisor restarts a stopped process |
| Tests | `test_aggregator_failure`, `test_aggregator_dedupe`, `test_rpc_digest` (with `Test_post_digest_body_length`: the short body on the socketless handler and from a real peer that half-closes), `test_anchor_records` |

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
| Preconditions | the departure clock has fired; the wallet has a spendable output, and the biggest confirmed one is chosen (a remnant is never spent while a larger output is there); the fee is under the cap; the attempt is not one the node refused against this chain tip with the input's whole value as the fee |
| Side effects | a signed transaction broadcast; `unconfirmed_txs` (memory); the tree's occurrence count fixed at close |
| Acknowledgement point | none outside the process (`GET /` shows `most_recent_tx`) |
| Ambiguous outcomes | broadcast succeeded but the process died before recording it (the transaction is in the mempool, the stamper does not know it); a broadcast the node refuses (-26) with a change output left is bumped and tried again in the same pass; one refused with no change output left (the input's whole value is the fee, which no feerate can raise) is not tried again while that output, that fee and the chain tip stand: one ERROR names the node's reason, the pass returns, and later passes return quietly until a refill changes the output or a block the tip (then one more attempt, no second alarm); an accepted anchor logs the resume once |
| Recovery | the next start re-reads pending and starts a fresh cycle from the wallet's confirmed outputs; if the forgotten transaction is mined the dead-cycle path abandons the fresh one and the commitments stay pending until anchored again by a tracked transaction (README "Anchor cadence"). Nothing is lost; one fee is spent for nothing and never receipted. A wallet whose only spendable output cannot pay the fee stays unable to anchor until it is funded, and funding it is enough: the refill is chosen the pass it is confirmed. |
| Tests | `test_stamper_cadence`, `test_stamper_dead_cycle`, `test_stamper_fee_cap`, `test_stamper_wallet_empty`, `test_stamper_coin_selection` (the biggest output chosen beside a remnant; a lone remnant refused once, not sent again, one more attempt per block, the refill anchoring and the resume said once) |

### C4. Mined: candidate → waiting for depth

| | |
|---|---|
| Authoritative state | the chain; memory (`txs_waiting_for_confirmation[height]`, and `unprocessed_blocks`: the headers seen whose bodies are not yet read) |
| Preconditions | a new block holds the latest version of the transaction |
| Side effects, in order per block | the block is queued when discovery returns it (`known_blocks.update_from_proxy`: all or nothing, the remembered tip advancing with the list it returns and put back where it was when any read of the scan raises, so no block is seen without being queued); its body is read; every tree waiting at its height (a block a reorg replaced) has its commitments put back to `pending_commitments`; a mined anchor's commitments leave `pending_commitments` and the departure clock is re-armed; the block leaves the queue. Only when the queue is empty is any tree called mature (C5) |
| Acknowledgement point | none outside the process |
| Ambiguous outcomes | a reorg removes the block: the commitments go back to pending (C3 again); a body fetch that raises (a transient RPC error, which the loop logs and survives): that block and every block after it stay queued, the pass ends without a save, and the next pass reads them before anything is saved (2026-09-18 cold review R01: the headers used to count as processed the moment they were seen, so one failed fetch consumed the reorg's notification and the next pass saved the orphaned tree against the new chain's height, receipted it, and owed nothing); a block queued and then itself replaced during the stall: dropped unread, its replacement being among the new blocks; a read that raises inside discovery (the tip, a hash, the reorg check): the tip is put back, nothing is queued, and the next pass discovers the same blocks again and reads them before anything is saved (2026-09-21 year-of-operation review, scenario 24: the tip used to advance header by header while the list was local to the call, so a raise after the last header and before the return left the tip advanced with nothing returned, and the next pass, finding no new blocks, saved the orphaned tree against the new chain's height, receipted it and owed nothing: R01 had covered the body fetch, not this boundary); a restart forgets the queue and the mined tree alike: the commitments are read as pending again and re-anchored (a second fee, no receipt for the first) |
| Recovery | as stated; `is_pending` answers "Timestamped by transaction …; waiting for N confirmations" while the tree waits |
| Tests | `test_anchor_records`, `test_reorg_detector`, `test_stamper_block_queue` (the fetch that raises after a reorg, on the real store: continued execution, a second reorg during the stall, the restart, the healthy control, and the commitment anchored again on the new chain with a proof the public library accepts; `Test_header_discovery_is_all_or_nothing`: the tip read that raises once the last replacement header is appended, the tip put back and no body read, the next pass reading all seven and saving nothing, the healthy control, and each read of the scan raising once on `KnownBlocks` alone) |

### C5. Depth reached: save, then receipt

| | |
|---|---|
| Authoritative state | the database, one synchronous batch: the tree's timestamps and the watermark (the lowest journal index still outstanding after this save) |
| Preconditions | `best_height - height + 1 >= --btc-min-confirmations` |
| Side effects, in order | (1) every marker of an *earlier* anchor still on file is settled, each on its own (a failure leaves that marker standing and is logged); (2) this anchor's marker `<receipts>.pending.<txid>` is written atomically (the receipt line, and the anchor's own key: its txid, as the saved tree carries it); a marker that cannot be written is a save that does not happen this pass (below); (3) the batch is written; (4) the receipt line is appended, every byte checked, file and directory fsynced; (5) this anchor's marker, and only this anchor's, is unlinked; (6) `journal.known-good` is rewritten with the same watermark and the database's generation |
| Acknowledgement point | (3): from here `GET /timestamp` serves the proof and the fill pass skips these entries |
| Ambiguous outcomes | a stop after (2) and before (3): the marker names commitments the database lacks; a stop after (3) before (4): saved, receipt owed, marker standing; a stop after (4) before (5): receipt on file, marker standing; a failed (4): as after (3); a failed (6): the file lags the database (safe: a longer rescan). A failed (2) is not ambiguous: nothing is saved, the tree is kept and retried every pass exactly as after a failed (3), and a receipts directory that cannot be written therefore delays publication until it can. That delay is the trade-off chosen (2026-09-18 cold review R06): before it the marker's failure was logged and the save went on, and when (4) then failed as well the tree was retired with no marker and no receipt while the message said the marker kept it. The other way to keep publication independent of the receipts store, a record of the owed receipt inside the save's own batch, needs a second durable owner and is not built |
| Recovery | the question every settling asks is whether the database holds the anchor's own txid node, which only that anchor's saved tree puts there. (2)-(3): it does not, so the receipt is discarded ("nothing is owed") and the commitments re-anchor under another txid; a discard whose unlink fails leaves the marker, reported, asked again at the next start and before the next marker, and answered the same, since the next anchor's tree carries its own node and not this one's (2026-09-18 gate review, G1: the question used to be one of the tree's commitments, which the next anchor's save answered for, and the receipt was written too: five records accepted, seven receipted); (3)-(4): the node is present and the txid is not on file, so the receipt is appended from the marker; (4)-(5): the txid is on file, and the file and its directory are fsynced again before the marker goes: a line found on file was written, not known to be synced, since the append that wrote it may have stopped or failed at its fsync, and the marker is the receipt's only other copy (2026-09-17 workflow four: the marker used to go on sight). The unlink itself has no fsync after it: a marker found again is settled again, the same way. A torn last line is dropped before any append and recovered from its marker. A marker that does not parse is set aside as `<marker>.corrupt-<time>`, warned about, and no receipt is guessed from it. A failed save keeps the tree in memory and every pass retries it; a stop before it lands is C4's restart case. |
| The state no stop leaves | the txid on file, its marker standing, and the anchor's node **absent** from the database. The receipt is appended only after the save's synchronous batch, so this is not a stop's residue: the receipts file is newer than `db/`, copies from different moments (R1). The stamper's open raises it, the service stops (`CALENDAR STORAGE INCONSISTENT`, exit 1) before anything is anchored, and the marker stays: going on would anchor those records again and receipt them a second time (2026-09-17 workflow four: the marker used to be discarded as `nothing is owed`, beside the receipt). |
| Invariants pinned | never two receipts for one txid; never a receipt for an anchor whose save did not happen; never a save without its marker on file first; a marker is settled by its own anchor's save and by nothing else; a marker is removed only after its receipt's line is on file with the file and its directory fsynced by the code that removes it, or after it was found not owed; **a later anchor's failure to settle an earlier marker never removes that marker** (2026-09-15 review F08) |
| Tests | `test_receipt_marker` (with `test_a_marker_whose_discard_failed_is_never_satisfied_by_a_later_anchor`, and `Test_marker_before_save`: the receipts directory unwritable on the real store, the tree kept across passes and saved with marker and receipt once it is writable again, the restart that owes nothing, the append-failure control), `test_anchor_receipts`, `test_stamper_save_retry`, `test_stamper_checkpoint`; `test_restore_calendar.Test_a_recovery_that_is_stopped_again` (every named write, truncate, fsync, unlink and rename call of the settling, from each state a marker can be found in; a discard whose unlink is refused, followed by the next anchor, on the real store), `Test_a_copy_taken_while_the_calendar_writes` (the state no stop leaves) |

### C6. Start: the storage check, then the scan

| | |
|---|---|
| Authoritative state | `db/` (generation, watermark), the journal (length, entries), `journal.known-good` (index, generation) |
| Preconditions for serving | all of: `db/` opens as one database; `journal.known-good` can be read, or is absent (an error other than absence, a restore that lost its permissions, is refused by the error's class and errno, with the recovery, at both readers: 2026-09-18 gate review, G2; a malformed file is described by its shape and never quoted, and no exception's own text is passed on: the index is checked digit by digit, in ASCII, before it is converted, since `int()` quotes what it refuses: corrections review, G2a); `journal.counts` counts no more entries than the journal holds (the sidecar is written only after its entry is durable, so more means the journal is the older file: R1); the checkpoint is absent or reads as `INDEX GENERATION`; a generation on file equals the database's; the index is at or below the database's watermark; **the journal holds at least `INDEX` entries, and the entries at `INDEX-1` and at 0 are in the database** (2026-09-15 review F02). A checkpoint that is an index alone, the form before generations, is refused whatever the database holds, with the one-time rescan named; it is never adopted (R3). |
| What the journal check is | a bounded check, two reads and two probes: it catches a missing journal, one truncated below the checkpoint, and one from another lineage that differs at either probed position. It assumes the restore rule (`db/`, `journal`, `journal.counts`, `journal.known-good` from one stopped copy). It does not detect an older prefix-identical journal that still reaches the checkpoint (the entries beyond it are lost; only a sidecar from the newer moment can tell, and only when it is there), nor one that differs only between the two probed entries. A rescan from 0 reads every entry that is here and anchors what the database lacks; it cannot show that entries were not lost or replaced, and without a checkpoint nothing about the journal is checked at all. R2 pins each of these. |
| Side effects | on a new database, or one from before generations with no checkpoint on file, its generation with watermark 0, in one synchronous batch; nothing else is written before the checks pass (LevelDB's own open may replay its log into a table), a refused start rewrites no file, and a missing journal is not created while a checkpoint names an index above 0 |
| Acknowledgement point | every flag is checked before anything else: a `--btc-min-confirmations` below 2 exits 2 from argparse with nothing bound and no worker started (2026-09-18 cold review R18: it used to fail in the stamper's constructor after the aggregator thread had started and the port was bound, and the non-daemon worker kept alive a process that served nothing, which a supervisor never restarts); the listener is bound only after the storage checks pass and before any worker thread starts (2026-09-15 review F19): a bind failure exits 1 with nothing running; a worker whose constructor raises after another has started stops what started, closes the listener and exits 1 (a guard no shipped flag reaches, and not driven by a test). The receipts are checked a moment later, when the stamper opens (C5, "the state no stop leaves"): a digest accepted in between is in the journal and is anchored once the set is made coherent |
| Ambiguous outcomes | none that the files can show: any disagreement named here stops the process with `CALENDAR STORAGE INCONSISTENT` and the recovery text, exit 1, nothing served. What the files cannot show is R1's and R2's list. No message names the calendar's directory: files are named by their fixed names (2026-09-17 workflow four) |
| Recovery | delete the checkpoint and start again: the scan begins at 0 and every entry the database lacks is re-anchored (later blocks than the originals). A journal restored from before the copy the database came from has lost the entries between: this is why the README asks for one stopped copy of the set. |
| Tests | `test_calendar` (`Test_storage_generation`, `Test_journal_boundary`), `test_otsd_launcher` (`Test_process_boundary`, `Test_invalid_configuration`: depth 1 exits with nothing bound, depth 2 serves), `test_stamper_checkpoint`; `test_restore_calendar` (every refusal, each limit, the messages) |

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
| Conservation of obligations | a journal entry is owned by the journal until the database holds it; a receipt is owned by its marker until the line is on file, and no anchor is saved before its marker is; a block's body is owned by the stamper's queue until it is read, and no tree is called mature before then | C1, C4, C5, C6 tests; `test_receipt_marker`, `test_stamper_block_queue` |
| Ambiguity is a state | unreadable checkpoint: refused, not guessed; a checkpoint that is an index alone: refused, not adopted on a probe; a journal shorter than the checkpoint, or than its own sidecar: refused; a database that does not open: refused with the recovery, not a traceback; a receipt on file for a save the database lacks: the service stops; a failed record count: unknown, summed as 0; a failed RPC in `GET /`: `best_block` null, not a blank 200; a `POST /digest` body shorter than declared: a 400, never a digest of another length; a block body the node did not return: the block stays owed, not processed; a failed journal read in the watcher: a failed check, cursor kept | C1, C4, C5, C6, section 10; `test_restore_calendar`; `test_anchor_records`; `test_rpc_status`; `test_rpc_digest`; `test_stamper_block_queue`; `test_watch` |
| Recovery is interruptible | the marker settle is one marker at a time and re-runnable from a stop at any of its writes; a database from before generations gets its generation in one batch, and a start stopped on either side of it adopts once; the checkpoint is published by one rename and either file starts; the tail recovery is idempotent | `test_receipt_marker`, `test_calendar`, `test_restore_calendar.Test_a_recovery_that_is_stopped_again`, `Test_checkpoint_from_before_generations` |
| Concurrency preserves decisions | one stamper thread mutates the queues; RPC threads read snapshots; the receipts file has one writer; the watcher holds a whole-run lock | `test_rpc_status`, `test_watch` |
| Safety includes progress | a failed save is retried every pass; an unwritable receipts file (the append, C5 step 4) never blocks the save, while a marker that cannot be written (step 2) holds the save back until it can be, by choice (C5); a wedged aggregator round is a 503 and then an exit, not a hang; one failed marker never blocks the next anchor's receipt | `test_stamper_save_retry`, `test_aggregator_failure`, `test_receipt_marker` |
| External effects have retry semantics | a transaction's identity is its txid; a replaced version never gets a receipt; a forgotten transaction is never receipted; the ntfy post is at-least-once from a durable outbox | `test_anchor_receipts`, `test_watch` |
| Time, capacity, observation | the departure clock is free-running; a full disk fails the round loudly; `process alive` is not `service healthy` (a dead worker stops the process; a bind failure exits; a flag the stamper would refuse is refused before any socket or worker exists) | `test_stamper_cadence`, `test_aggregator_failure`, `test_otsd_launcher` |
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

The same invariants in the watcher (section 8):

| Invariant | Where it holds in the watcher | Where it is checked |
|---|---|---|
| Conservation of obligations | a message is owed from the record on and stays in the state until the outbox holds it, in the outbox until ntfy took it; the cap's drop is itself a message | `test_watch_observation.Test_recording_boundaries`, `Test_delivery_and_cap` |
| Ambiguity is a state | a source that cannot be read is an unknown verdict, named in the status line and the heartbeat, never ok; a corrupt state is a notice, not a silent fresh start | `Test_unknown_is_a_state`, `Test_state_recovery` |
| Recovery is interruptible | the record is one rename; the owed list is copied once whatever run does it; the two quarantines copy aside first | `Test_recording_boundaries`, `Test_state_recovery`, `test_watch.Test_outbox_recovery_interrupted` |
| Concurrency preserves decisions | one run at a time, the loser observing nothing | `Test_two_runs`, `test_watch.Test_run_lock` |
| Safety includes progress | one unreadable source never stops the run; an undeliverable message never blocks observation | `Test_unknown_is_a_state.test_an_unknown_check_never_stops_the_run`, `Test_delivery_and_cap` |
| External effects have retry semantics | delivery is at least once, in order, from a durable queue | `Test_delivery_and_cap`, `test_watch.Test_outbox` |
| Time, capacity, observation | the journal cursor moves only in the write that records the burst; the heartbeat is once a day and its absence is the outside's to see; the queue's bound is named | `Test_recording_boundaries`, `Test_heartbeat`, `Test_delivery_and_cap` |

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
   The attestation tags the client knows are four, and the readers read
   each payload as the client does: pending (a URI), and the block-header
   attestations of Bitcoin, Litecoin and Ethereum (one varuint height,
   read to the payload's end). Any other tag is unknown and its payload
   opaque, up to 8192 bytes. That is the supported subset, stated: parity
   with the client on what parses, and Bitcoin alone on what counts.
   `selfstamp.py`'s reader is linear only (one attestation, no fork
   marker), by design; `verify_claim.py`'s reads forks. (2026-09-18 cold
   review R09: the Litecoin and Ethereum tags used to be read as opaque
   unknowns, so an empty or trailing payload the client refuses parsed
   here, alone and beside a Bitcoin node.)
2. **Contains a Bitcoin attestation**: after (1), some attestation node
   carries the Bitcoin block-header tag. A structural fact about the
   file. The words for it are `bitcoin height=N` (self-stamp) and
   `bitcoin_attestation_present` (adapter). Never "anchored" as a claim
   about the chain, never "verified". A Litecoin or Ethereum attestation
   is not one: it reads as unknown, no usable attestation, and no claim
   about that chain is made or checked.
3. **Verifies against Bitcoin**: the path from the exhibit's digest
   replays to the merkle root the attestation names, and the block at
   that height, in an authenticated chain of headers, carries that root.
   Only `verify_claim.py` (with a checkpoint at or after the block) and
   the public client against a node make this claim.

The corpus that pins (1) and (2) is `ops/tests/proof_corpus.py`, run
against each reader and against the pinned library in
`otsserver/tests/test_proof_corpus.py`: accepted shapes, full
consumption, every strict prefix of every valid proof, one trailing
byte, malformed payloads (for each of the four known tags: a valid, an
empty, a trailing and an unterminated payload, alone and beside a
Bitcoin node), the size limits at their boundaries, unknown attestation
tags (which parse, and count as no usable attestation), and the
narrowings above. A reader that disagrees with the library on any case
the corpus does not name as a narrowing fails the suite; the library is
the oracle and the suite needs it.

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

## 8. Workflow 3: the watcher (`ops/watch.py`)

Written 2026-09-16 (workflow three), on the three tables the 2026-09-15/16
review left here. The words used:

- **Check**: one named question about the box (`ORDER`), asked every run;
  a check whose knob is empty is not asked and not counted.
- **Verdict**: what a check says this run: **ok** (True), **failed**
  (False) or **unknown** (None: the source could not be read or gave no
  answer). Unknown is never ok.
- **Owner**: the check whose failure is the reason another check is
  unknown (`OWNED_BY`: `health` behind `health_reach`, the four journal
  counts behind `journal_read`). While the owner fails the owned check is
  *suspended*.
- **Transition**: a check entering the delivered set (DEGRADED) or the set
  emptying (RECOVERED). The **delivered set** is the checks whose alarm
  transition has been recorded and not yet undone; the name is the
  state's. It says a message was decided and queued, not that the
  operator received it: receipt is the outbox's business, at least once.
- **Burst check**: a check about a window of the journal
  (`BURST_CHECKS`): it alarms on the run it is seen and clears on the
  next; every other check needs `CONFIRM_RUNS` runs both ways (the
  flapping guard).
- **Cursor**: the journal time the next run reads from.
- **Owed**: the outbox as it must now be, recorded in the state (with
  this run's messages appended and the cap applied) before it is copied
  to the outbox; absent once the outbox has it.
- **Heartbeat**: the one message a day that says the box is there.

### Records

| Record | Kind | Authoritative for | Loss or damage |
|---|---|---|---|
| `state.json` | authoritative | the delivered set, the per-check run counters and `since` times, the heartbeat day, the egress-drop accumulator, the cursor, and, while the outbox has not been brought into line, the queue owed (the outbox as it must be) | missing: a fresh start (checks failing now alarm once, the journal is read from five minutes back); unreadable: the run does nothing, exit 1; not a state object, or one whose fields are not what the run relies on: set aside as `state.json.corrupt-<12 hex>`, a fresh start with a notice (W8) |
| `outbox.json` | the delivery queue | the queue as last recorded, less what has been delivered since, oldest first | missing: empty; unreadable: the run does nothing, exit 1; not a list of messages with the two fields delivery needs (and, for a cap notice, its count): set aside, a notice queued (W2) |
| `status` | derived | one line: the time, ok/unknown/degraded, the counts, each failed and unknown check, what is queued | rewritten every run; nothing reads it back |
| `.lock` | coordination | one run at a time | goes with the descriptor |
| `watch.log` | diagnostic | the run's account, the addresses of unexpected logins, what was sent and not | nothing is derived from it |
| `state.json.corrupt-<12 hex>`, `outbox.json.corrupt-<12 hex>` | quarantine | bytes that could not be used | the operator's |

### Who owns unfinished work

| Handoff | Before | After | Owner in between | Evidence that lets the previous owner forget |
|---|---|---|---|---|
| timer → run | a tick | the lock held | nobody: a tick that finds the lock held exits 1 having observed nothing, and the window it did not read is read by the next run because the cursor did not move | none needed |
| source → observation | files, commands, two HTTP answers | the observation dict, each source's failure marked | the run; a source that cannot be read is an unknown, not an answer | none: observations are made again every run |
| observation → verdict → transition | the dict | `decide`'s new state and messages, in memory | the run | none yet |
| transition → record | memory | `state.json` renamed into place with the delivered set, the cursor and the queue owed (the outbox as it must be, this run's messages appended and the cap applied) | the run until the rename; nothing before it is anybody's | the file: from here the transition happened once and every message in the recorded queue is queued once |
| owed → outbox | `owed` in the state | `outbox.json` holding that queue, the state rewritten without it | the state: a stop leaves the queue owed, and the next run copies it over the outbox, discards and order included | the second state write |
| outbox → operator | a message in the outbox | a 200 from ntfy, the outbox rewritten without it | the outbox; at least once | the rewrite |
| the day → the heartbeat | a day begun | `heartbeat_day` in the state, the line owed | the run; a stop before the record makes the next run send it | the record |

### W1. One run: observe, decide, record, queue, deliver

| | |
|---|---|
| Authoritative record | `state.json` (W8); `outbox.json` (W2) for delivery |
| Preconditions | the whole-run lock on `WATCH_DIR/.lock` (a second run waits up to `LOCK_WAIT`, then exits 1 `locked`, having observed nothing); the state read (W8) and, with `NTFY_URL` set, the outbox read (W2), both before anything is observed: a file that cannot be read ends the run with nothing done |
| Side effects, in order | (1) observe: every source once, each failure marked (W4); (2) evaluate and decide (W5), the heartbeat line if due (W7); (3) the queue prepared: the queue as loaded (or, when the state still owes one, the recorded queue) with this run's messages appended and the cap applied (W6); `status` written with its length; (4) **the record**: `state.json` written once with the new delivered set and counters, the cursor (the run's start if every journal query succeeded, else the cursor it read from: W3) and the prepared queue under `owed`; (5) that queue written to the outbox; (6) `state.json` rewritten without `owed`; (7) delivery oldest first, the outbox rewritten after each, stopping at the first failure; (8) the status rewritten with what is still queued when that changed; exit 1 while anything is undelivered. With `NTFY_URL` empty: `status`, (4) with nothing owed, the messages in the log. With `--dry`: (1)–(2), `status`, the messages in the log as `WOULD SEND`, nothing else written and nothing set aside |
| Visibility point | each rename: the status at (3), the record at (4), the queue at (5). A file is visible from its rename and durable from the directory fsync that follows; an fsync that fails raises after the rename, and the run stops there with the file visible and its durability not known: the next run takes what it finds |
| Acknowledgement point | (4): one rename. Before it nothing was recorded: a stop leaves no transition, the cursor does not move, and the next run observes afresh and reads the journal window again; a transient condition seen only by the stopped run may have passed, in which case there was nothing to record. After it the transition is recorded and every message in the recorded queue is queued exactly once, whatever happens next (2026-09-16 workflow three: the record used to be two writes, the outbox then the state, and a stop between them left the message on disk and the transition unrecorded, so the next run queued it again) |
| Ambiguous outcomes | a stop between (4) and (5): the queue is owed and the outbox is older: the next run copies the recorded queue over it; between (5) and (6): owed and queued alike: the next run copies the same queue again, and nothing is added or dropped a second time (the cap decision is part of the record; 2026-09-16 gate review: a replay used to re-append the discarded messages and drop newer ones); during (7): a message delivered and not yet removed is sent again (at least once, by design); (5) fails (the outbox cannot be written): the queue stays owed, the run still delivers from it directly and rewrites the record after each success, and stops delivering when that rewrite fails so no unrecorded delivery is repeated; (6) fails: nothing is delivered this run, and the next run copies the record again |
| Recovery | the next run: same lock, same reads; nothing recorded is lost by a stop, nothing is alarmed twice for one transition, and a message discarded by the cap does not come back |
| Postconditions tested | `test_watch.Test_outbox`, `Test_observation_failure`, `Test_run_lock`; `test_watch_observation.Test_recording_boundaries` (a stop before the record, after it, after the outbox write, by injection and by a child killed), `Test_delivery_and_cap`, `Test_two_runs` |

### W2. Loading the outbox, and its recovery

| | |
|---|---|
| Authoritative record | `outbox.json` |
| Preconditions | the lock is held; read before anything is observed |
| Outcomes | missing: an empty queue; readable and a list of messages, each with a `text` and a `queued` string (a cap notice among them with a count under `dropped`, which the fold adds to: 2026-09-18 cold review R12): the queue; unreadable (any error but absence): `OutboxUnreadable`, the run ends with exit 1 having done nothing (the message names the error class, not the path); readable but anything else, a field of the wrong shape included: the recovery below (2026-09-16 gate review: a message whose `queued` was a list used to stop every run) |
| Recovery, in order | (a) the bytes are copied aside as `outbox.json.corrupt-<12 hex of their sha256>`, atomically; (b) a queue holding one notice (the box name, the reason, the byte count, the aside file's name) replaces the corrupt file in one atomic rename |
| Ambiguous outcomes | a stop before (b): the corrupt file is still in place and the next run repeats (a) and (b); the same bytes give the same aside name, so no second copy; a stop after (b): the notice is on disk and owed; at no point is the corrupt file gone while the notice exists only in memory |
| What it does not do | the messages the corrupt bytes held are not recovered; the notice says they may not have been delivered, and the bytes are kept for the operator |
| Tests | `test_watch.Test_outbox_read_failure`, `Test_outbox_recovery_interrupted` |

### W3. Observation failure: the journal

| | |
|---|---|
| Authoritative record | the journal cursor in `state.json` |
| Rule | a `journalctl` call that exits nonzero (or times out) reads as no lines and is recorded by label; `journal_read` is then a failed check (two-run confirmation like the others), and the cursor written at W1 (4) is the one the run read from, so the window is read again next run until every query succeeds. The three pattern searches (ssh failures, accepted logins, egress drops) are made by the run over the window's lines, never by `journalctl -g`: under `-q` that flag exits 1 when nothing matches, which read as a failed query, so a quiet window froze the cursor and alarmed `journal_read` on a calm box (2026-09-18 cold review R10; systemd v257 `journalctl-show.c`). The four checks that count that window (`journal_errors`, `ssh_failures`, `egress_drops`, `ssh_unexpected`) are unknown when their query failed and suspended behind `journal_read` (W5): they neither read as ok (2026-09-16 workflow three: they used to count zero) nor alarm on their own. A burst seen by a succeeding query in such a run may be counted again next run, the safe direction. |
| Tests | `test_watch.Test_observation_failure`; `test_watch_observation.Test_unknown_is_a_state`, `Test_decide_with_unknown`, `Test_journal_exit_semantics` (a quiet window against a `journalctl` double that exits 1 to a `-g` query: no query carries `-g`, the patterns are counted here, the cursor moves; a failing query keeps it) |

### W4. The observations, check by check

Every check is an observation with three answers. The source is read
once per run; a source that cannot be read, decoded or parsed, or that
answers in a shape the check does not expect, marks the observation (a
`None`, an error class, a failed label) and `evaluate` turns the mark
into unknown, never into ok, and never into an exception: one bad source
stops nothing else (2026-09-16 gate review: invalid UTF-8 in the
heartbeat or the receipts, and a JSON list from `/health`, used to stop
the run). A positive verdict needs the fields it is about: a fresh file
without them is unknown. Every check's failed and unknown answers are
shown in the status line and the heartbeat; unknown alarms like failed
after the same runs (W5), except where an owner speaks for it. The
`anchor_age` row's order rule is pinned by
`test_watch_observation.Test_receipt_order`.

| Check | Source | ok | failed | unknown | Owner | Runs |
|---|---|---|---|---|---|---|
| `health_reach` | `GET HEALTH_URL` | an answer with a body (200 or 503) | no answer | — | — | 2 |
| `health` | that body | `status` ok | `status` not ok, the failing fields quoted | `/health` unreachable; a body that is not JSON or not an object | `health_reach` (unreachable only) | 2 |
| `calendar` | `GET CALENDAR_URL/`, `Accept: application/json` | `best_block` set, `needs_attention` empty, `anchor_receipts` on, balance ≥ `CAL_MIN_SATS` | no answer; Bitcoin-blind; needs attention (the detector's text quoted); receipts off; wallet low | the body is not an object; the balance cannot be read | — | 2 |
| `containers` | `docker ps` | every configured container `Up` | one is not | `docker ps` failed | — | 2 |
| `units_system`, `units_user` | `systemctl is-active` per unit | `active` | any other state systemd names | an answer systemd does not give (the command failed) | — | 2 |
| `disk_root`, `disk_boot` | `statvfs` of the mount | under `DISK_PCT` | at or over (the detail names the role and the figure, never the mount: it is configuration) | `statvfs` failed | — | 2; an empty knob skips |
| `temp` | `/sys/class/thermal/thermal_zone0/temp` | under `TEMP_C` | at or over | no reading | — | 2; an empty `TEMP_C` skips |
| `mem` | `/proc/meminfo` `MemAvailable` | at least `MEM_MB` | less | unreadable | — | 2; an empty `MEM_MB` skips |
| `feeder` | `FEEDER_LOG` mtime and tail | fresh, and the tail's poll lines not all errors | missing; stale; errors | unreadable; the tail failed; no poll line in the tail | — | 2 |
| `endpoint` | `ENDPOINT_HEARTBEAT` mtime and content | fresh, `breaker=ok` | missing; stale; breaker not ok | unreadable; not UTF-8; fresh but without a `breaker` field | — | 2 |
| `journal_errors` | `journalctl -p err` since the cursor, system and user | at most `JOURNAL_ERRORS` | more | the query failed | `journal_read` | burst |
| `ssh_failures` | `journalctl -u ssh` since the cursor, failures | at most `SSH_FAILURES` | more | the query failed | `journal_read` | burst |
| `journal_read` | every `journalctl` query of the run | all succeeded | one failed (named; the window is kept) | — | — | 2 |
| `anchor_age` | every line of `RECEIPTS`: the newest `confirmed_at`, since a receipt recovered from its marker is appended after later anchors' lines (C5; 2026-09-18 cold review R11: the last line used to be taken for the newest, and a recovery made a fresh anchor read as stale) | the newest `confirmed_at` within `ANCHOR_MAX_H` | older | file missing; unreadable; not UTF-8 or not JSON lines; a `confirmed_at` that is not a finite number; no receipt yet (a new host, until its first anchor) | — | 2 |
| `reboot_wanted` | `/run/reboot-required`; `/lib/modules` against `uname` | no file, newest kernel running | the file is set; a newer kernel installed | the module list unreadable | — | 2 |
| `egress_drops` | `journalctl -k` `egress-drop` since the cursor | at most `EGRESS_DROPS` | more | the query failed | `journal_read` | burst |
| `dhcp_lease` | `nmcli` `DHCP4.OPTION` expiry | at least `DHCP_MIN_H` left | less | no expiry from `nmcli` | — | 2 |
| `tor_circuits` | `docker logs` of `TOR_CONTAINER`, twice | a heartbeat with circuits, no no-network warnings | no heartbeat; zero circuits; warnings | the log unreadable; the warning-window query failed | — | 2 |
| `btc_peers` | `ss` established to `BTC_P2P_PORT` | at least `BTC_MIN_PEERS` | fewer | `ss` failed | — | 2 |
| `ssh_unexpected` | `journalctl -u ssh` accepted logins since the cursor | every source in `SSH_KNOWN_SOURCES` | one is not: the message carries the count, the log on the box the address | the query failed | `journal_read` | burst |

Not checks, carried by the heartbeat only: pending package updates (`apt
list --upgradable`, heartbeat runs and `--dry` only; `?` when the
command fails), uptime, the egress drops since the previous heartbeat,
the anchor wallet's balance and pending count when the calendar is
observed. The self-stamp is observed through `selfstamp.timer` in
`UNITS_USER` only: its summary line and its lock are not read (an
assumption stated in "Assumptions and limits").

### W5. The alarm rule

| | |
|---|---|
| Authoritative record | `state.json`: `delivered`, `fail_runs`, `ok_runs`, `since` |
| Preconditions | the verdicts of this run; the state as last recorded |
| Rule | a check joins the delivered set after `CONFIRM_RUNS` failed-or-unknown runs in a row (one for a burst check) and leaves it after as many ok runs in a row; one DEGRADED message names what joined and what is still delivered (a delivered check that is ok again but not yet confirmed is named as recovering); one RECOVERED message when the set empties, naming what left and how long the oldest had been failing, and saying `all N checks ok` only when every verdict is ok now, else `not all clear:` with what is failing or unknown, unconfirmed as it may be (2026-09-16 gate review: it used to say all ok from the alarm set alone); nothing while a problem persists; a check that flaps under the confirmation never alarms. An owned check whose verdict is unknown while its owner is not ok is suspended: no counter moves, it is not `still`, and its owner's message speaks for it; it resumes with its next verdict of its own. The alarm set decides when to speak; what is said about health comes from the verdicts. |
| What it never does | report healthy on a failed read: an unknown verdict is never counted ok, the status word is `unknown` and the check alarms or is suspended behind an alarm; collapse unknown into ok: the status line and the heartbeat name every unknown check; lose a transition across a restart: the delivered set lives in `state.json`, written in the same rename as the messages it produced (W1), and a run that stops before that rename leaves nothing, so the next run decides the same transition once; alarm twice for one transition; alarm on a one-run blip other than a burst |
| Ambiguous outcomes | none in `decide`, which is pure; W1 names the stops around the record |
| Postconditions tested | the 30 fixture scenarios (`test_watch.Test_fixtures`); `test_watch_observation.Test_alarm_rule` (once each way, the guard, the second failure, the reload between runs), `Test_decide_with_unknown` (unknown alarms after confirm and is worded so; an owned unknown suspended; the status words; the heartbeat's unknown list) |

### W6. Delivery, at least once, and the cap

| | |
|---|---|
| Authoritative record | `outbox.json` |
| Rule | oldest first, one `POST` to `NTFY_URL` each, the outbox rewritten after each success, stopping at the first failure so alerts never reorder; a message whose send failed stays, the run exits 1 and the status line carries `queued=N`, and every later run tries again from the head. Delivery is at least once: a stop between a send and the rewrite sends that message again; so does an outbox that could not be written when the state could not be rewritten either. |
| The cap | the outbox keeps `OUTBOX_MAX` (200) entries: a deliberate bound on what a long outage can accumulate. When more would be queued the oldest are dropped and the drop is itself the first message in line: a notice (`cap`) saying how many alerts were dropped and the `queued` times of the first and last, folded into the notice already at the head when there is one, so the queue is never silently shorter than what was owed (2026-09-16 workflow three: the drop used to be a log line). The decision is made once, when the queue is prepared, and recorded with the transition (W1): a replay copies it and never drops or reorders again. What the dropped alerts said is lost; the notice says so. |
| Postconditions tested | `test_watch.Test_outbox`; `test_watch_observation.Test_delivery_and_cap` |

### W7. The heartbeat

| | |
|---|---|
| Authoritative record | `heartbeat_day` in `state.json`, written with the record (W1) |
| Rule | one line per UTC day, on the first run at or after `HEARTBEAT_HOUR` that finds `heartbeat_day` behind; it is worded from this run's verdicts: `ok N/N` only when every check is ok now; `degraded:` with every failing check's detail, `(not yet alarmed)` after one whose alarm is not yet confirmed; `unknown:` with the unknown ones (2026-09-16 gate review: it used to say `ok` from the alarm set while a first failed observation stood); then the vitals (disk at the configured mounts by role, temperature, memory, anchors, wallet, updates, egress drops, uptime). A day the box was off gets no heartbeat and the next day's comes once; the timer's `Persistent=false` makes up no missed ticks. |
| What it does not claim | a box that is down, powered off or cut off sends nothing, and this tool cannot say so: the heartbeat's absence is detectable only from outside the host, by whoever expects it, and no message from the box ever reports the box's own death |
| Postconditions tested | `test_watch.Test_fixtures` (00, 06, 07, 18, 26); `test_watch_observation.Test_heartbeat`, `Test_decide_with_unknown.test_the_heartbeat_names_unknown_checks…` |

### W8. The state file

| | |
|---|---|
| Authoritative record | `state.json` |
| Outcomes at load | missing: a fresh state; unreadable (any error but absence): `StateUnreadable`, the run does nothing and exits 1 (the message names the error class, not the path); not a JSON object, or an object whose fields are not what the run relies on (`valid_state`: `delivered` a list of names, `fail_runs` and `ok_runs` counters by name, `since` finite numbers by name, `heartbeat_day` a string, `drops_acc` a count, `cursors.journal` a finite number, `owed` a list of messages with a count under `dropped` on a cap notice; a field may be absent, never of another shape; finite means an int, or a float that is neither infinity nor NaN: JSON's `1e309` reads as infinity, passed the type check and failed `int()` on every run past the quarantine, 2026-09-18 cold review R12): the bytes are copied aside as `state.json.corrupt-<12 hex of their sha256>` (first, a name from the bytes so a repeat writes the same file), the run goes on from a fresh state, and one notice is owed saying what a fresh state cannot know: the checks that were failing (they alarm again once), the journal since the last good run (read from five minutes back, not from where it stopped), and any alert the old state still owed, which is in the aside file only (2026-09-16 gate review: a counter of the wrong shape used to stop every run, and a malformed owed entry used to be dropped without a word). A dry run sets nothing aside: it names the problem in the log and goes on from an empty state in memory. |
| Ambiguous outcomes | a stop after the aside copy and before the record: the corrupt file is still in place and the next run repeats the copy (same name) and the notice; the record replaces the corrupt file, so the notice is owed exactly once (2026-09-16 workflow three: a corrupt state used to stop every run with a traceback, which nothing but the heartbeat's absence would show) |
| Postconditions tested | `test_watch_observation.Test_state_recovery`, `Test_numeric_poison` (an infinite cursor or since time set aside and the run going on; a finite float cursor kept; a cap notice without a count refused at both files) |

### W9. Two runs

| | |
|---|---|
| Rule | the lock is taken before the state is read and before anything is observed: the run that loses it observes nothing, writes nothing and exits 1 `locked`; nothing of its is lost because it made nothing, and the window it would have read is read by the next run, whose cursor is where the winner left it |
| Postconditions tested | `test_watch.Test_run_lock` (two real processes); `test_watch_observation.Test_two_runs` (the lock held in-process, the loser's observe never called) |

### Assumptions and limits

- Off-box goes only what `NTFY_URL` receives: the box's `NAME` (the
  hostname when unset: set it), the checks' details, counts. No message
  names a path, and an unexpected login's address stays in the log on
  the box. The ntfy topic is the operator's channel and their choice.
- The self-stamp is watched through its timer's unit state only; its
  summary line and its lock are not read. Reading them would need a knob
  for its state directory, a new feature this workflow did not add.
- Sources are read once per run; a value that changes between two reads
  of the same run is not detected. A source that answers wrongly (a
  container listed `Up` that is wedged) is believed.
- A new host's `anchor_age` is unknown until its first receipt, and says
  so; the first heartbeat carries it.
- The heartbeat's absence is the outside's to notice; nothing here
  reports the box's own death.
- A stop before the record leaves nothing to recover; the next run
  observes afresh. What survives a stop is what was recorded, and the
  journal window is the one thing the next run can read again; a
  condition that showed only to the stopped run may be gone.
- Fault model: injected exceptions and stops at named calls, chmod, a
  child killed after the record, two real processes on the lock, a
  sender that fails. No power cut. Verified on this Mac: `observe` runs
  for real only against files and a stubbed command runner; docker,
  systemd, journalctl, nmcli, ss and the thermal file are exercised by the
  fixtures and by no live command here.

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
| pending → attestation present | a pending proof | the proof replaced whole | the calendar (its anchor); the run only asks, once per run | the upgraded file, its barrier repeated every run; a complete proof is never asked about again |
| deliverer → inbox | a file being written | a file under its final name | the deliverer, until the rename into place (S8: a name the tool ignores until then); the deliverer may rename another file over that name later, and it is a new delivery | the rename |
| inbox → claim | a file under its final name | `.claim-<token>-<name>` | the run, from the atomic rename on: what a deliverer puts under the name afterwards is the next run's | the rename; a stale claim is resumed, not lost |
| claim → copy | a claimed manifest | `witnessed/<copy>.json` on disk | the run; the claim stays until the copy is durable | the copy, its barrier repeated by the run that finds the claim standing; the claim is then a duplicate |
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
| Preconditions | S1's; the newest manifest read whole and parsed (one that cannot be read or is not a manifest: `refused`, exit 1, nothing written); `seq` = its `seq` + 1, `prev` = its name and sha256, `chain` = its label, or a new label when it has none and no manifest of the chain has one. A newest manifest without a label beside an earlier one with a label was written by an older version of the tool: `refused … the chain has a label and its newest manifest … has none`, exit 1, no manifest written, and the message says what the operator moves aside; a manifest that cannot be read leaves the question open and is refused too (R4; 2026-09-17 workflow four: such a chain used to be given a second label) |
| Side effects | one file: a dotted unique temporary in `manifests/`, fsynced, renamed, the directory fsynced |
| Visibility point | the rename: a reader sees the old set of files or the new one, never a partial file |
| Durability point | the directory fsync after the rename; the first creation of `manifests/` (as of `witnessed/`, `rejected/` and the outbox) fsyncs the parent entry too. Visible is the rename, durable is the fsync, and the two are not the same instant (the tests' fault model is injected exceptions and a killed process, not a power cut) |
| Ambiguous outcomes | a stop before the rename leaves at most a dotted temporary that no reader of `*.json` sees (residue, not cleaned) and the next run builds the period again; a stop after it is the period done; an fsync that fails after the rename leaves a file that is visible and whose durability is not known: the run logs `manifest write failed`, exits 1, and the next run takes the file it finds as the record |
| Recovery | the next run finds the file and says `noop` |
| Invariants | one manifest per period, ever; a manifest is never rewritten; `seq`, `prev` and `chain` come from the file that is there; a label is drawn once per chain |
| Postconditions tested | `test_selfstamp.Test_run_heartbeat`, `Test_state_lock.test_atomic_writes_use_unique_names_and_fsync_the_directory`; `test_selfstamp_workflow.Test_concurrent_readers` (verify between the manifest and its proof), `Test_interruption`; `test_restore_tools.Test_what_older_tools_do_with_newer_state` (never a second label), `Test_a_restored_self_stamp` (the label drawn once across a stop at every write of the run that draws it; the label waiting on a manifest that cannot be read) |

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
| Acknowledgement point | the rename; a proof with a Bitcoin attestation is never asked about again, and its barrier (the file, then its directory) is repeated every run before it is called complete, because nothing on disk says whether the run that renamed it got its directory fsync back |
| States | `missing` (S4's case), `pending`, `bitcoin height=N` (an attestation is present: section 6, claim 2, never claim 3), `malformed` (this reader cannot parse it), `mismatch` (a proof, not of the file beside it), `unknown` (an attestation this tool does not read), `unreadable`, and an orphan proof with no manifest beside it |
| Ambiguous outcomes | a 404 is `pending`, reported, never a failure and never a deadline: a proof not yet anchored stays visibly outstanding for as long as it takes. A response cut short or without a Bitcoin attestation: `upgrade refused`, the pending file untouched, exit 1, asked again next run. The directory fsync after the rename failing: the upgraded proof is visible, its durability unknown, `upgrade refused`, exit 1; the next run repeats the barrier, and one that fails again is `proof not durable file=… error=<class and errno>`, exit 1, repeated next run. |
| Recovery | `malformed`, `mismatch`, `unknown`, `unreadable`, orphan: reported every run with exit 1 and never touched (2026-09-16 workflow two: a `mismatch` used to be upgraded without a word, and nothing but `verify` saw it). `malformed` is this reader's verdict, not a fact about the proof (a proof the ots client upgraded against several calendars carries a fork this reader refuses), and only the operator can say whether the manifest or the proof is the damaged one: the successor's `prev.sha256` says whether the manifest's bytes changed. The operator moves the proof aside; the next run stamps the manifest as it is now, under a later block (README, "Recover"). |
| Postconditions tested | `test_selfstamp.Test_run_heartbeat.test_upgrade_writes_only_the_proof`; `test_selfstamp_workflow.Test_proof_states` (a mismatch never upgraded, never fetched, never resubmitted; a malformed proof left in place; pending in the summary with exit 0), `Test_submission_ambiguity.test_a_truncated_upgrade_answer_leaves_the_pending_proof_untouched`, `Test_barriers_repeated` (a complete proof called complete only after its barrier, a barrier that fails again a failure again, the barrier repeated on the real directory) |

### S6. The inbox

| | |
|---|---|
| Authoritative record | `witnessed/<copy>.json` (the record), `<copy>.json.foreign.ots` (the source's proof), `<inbox>/.claim-…` (deliveries in the tool's keeping), `<inbox>/rejected/` (what could not be used) |
| Preconditions | `inbox` configured and a directory (missing: `inbox missing`, exit 1, the heartbeat goes on; unreadable: `inbox unreadable`, the same; neither message names the path); `witnessed/` made durably; files under their final names: `*.json` and `*.json.ots` not beginning with a dot (S8) |
| Side effects, in order | (1) **claim**: every delivery under a final name is renamed, atomically, to `.claim-<8 hex, one token per run>-<name>`, before any of it is read. From the rename the file is the tool's; a deliverer that renames another file over the same name afterwards has made a new delivery for the next run, instead of having it removed under it (2026-09-16 gate review, P1: the consumer read A, copied it, and unlinked the name B had meanwhile been published under). A claim that cannot be made is `inbox error`, counted, and the file stays under its name. (2) per claimed `*.json`, with its companion the claim of the same token and name or, when a stop claimed the two apart, the claim of the same delivered name by any run: (a) not a manifest by the one validator: the companion, then it, moved to `rejected/<12 hex of its sha256>-<delivered name>` (the companion first, so a stop between the two leaves the manifest to be found again), logged `inbox rejected`; (b) bytes already held, by content, whatever the copy's name: the copy's barrier repeated (the file, then `witnessed/`: the run that wrote it may have failed at that fsync and left this claim standing for that reason), then the companion consumed (below), then the claim removed, logged `inbox duplicate`; (c) new: the copy written whole as `<label or legacy>-<period>-<12 hex>.json`, then the companion consumed, then the claim removed, logged `witnessed chain=… seq=…`. (3) per claimed `*.json.ots` left alone: not a proof: `rejected/`; a proof of a copy held: kept by the rule below, removed, logged `inbox foreign proof`; a proof of nothing held whose manifest's name sits in `rejected/`: `rejected/` too, `its manifest was rejected`; a proof of nothing held otherwise: left as a claim and logged `awaiting its manifest` on every run, with no timeout. (4) the inbox directory fsynced, so the removals are durable before the pass returns. |
| The companion rule | kept as `<copy>.json.foreign.ots` when it is a proof of exactly the copy's bytes and says more than what is held: nothing held, or pending held and this one carrying a Bitcoin attestation; removed when it says less, after the held file's barrier is repeated (it then answers for the delivery); moved to `rejected/` when it is not a proof of the copy (2026-09-16 workflow two: it used to be deleted, F14's residue). What is held counts only if it is itself a proof of the copy: a held file that is not (a Bitcoin attestation for other bytes among them) is set aside beside the copy as `<copy>.json.foreign.ots.rejected-<12 hex>`, logged `foreign proof set aside`, and never outranks a proof that is (2026-09-16 gate review, P2: it used to be ranked on its attestation alone, and the right proof was removed). |
| Quarantine, durably | the file's bytes fsynced (a deliverer may not have), the rename into `rejected/`, then `rejected/` and the inbox fsynced; a new `rejected/` has its entry in the inbox fsynced before anything is moved into it; the log line follows the last fsync (2026-09-16 gate review, P5: the rename used to be acknowledged with no barrier). The same holds for a held proof set aside. |
| Visibility point | each rename; the copy exists before anything about the delivery is removed |
| Ambiguous outcomes | a stop after a claim: the claim is resumed next run, whatever token it carries; a stop after the copy and before the removals: the next run finds a duplicate, repeats the copy's barrier, and finishes; a stop after the companion's quarantine and before the manifest's: the manifest is found again alone; a stop between the foreign proof and the removals: a duplicate whose companion `held` what it already holds; an fsync that fails after a rename: visible, durability unknown, counted as a failure (`inbox error`), the claim kept, and the next run repeats the barrier before the claim goes |
| One defective item | a file that cannot be read, a rename or an fsync that fails, is logged `inbox error` with the class and errno (never the path), counted (exit 1) and left for the next run; the other files and the heartbeat are not stopped (2026-09-16 workflow two: one unreadable file used to end the run before the manifest). An inbox directory that cannot be listed is one failure. Two deliveries under one name with different bytes are both kept: the quarantine name is by content. Two different manifests claiming one label and seq are both copied and both vouched for; `verify --witness` says so (S9). |
| Recovery | the next run: the same claims resumed, the same reads, the same rules. A file caught half written under its final name is quarantined as malformed, not lost; the convention (S8) is what prevents it. |
| Postconditions tested | `test_selfstamp.Test_witness`, `Test_witnessed_copies`, `Test_witness_delivery`, `Test_malformed_foreign_proof`; `test_selfstamp_workflow.Test_inbox_faults` (a rejected companion kept, a late proof stored, a proof awaiting its manifest, a malformed orphan, an unreadable file and directory, conflicting deliveries, a delivery replaced under its name while held, a claim that cannot be made, a claim left by a dead run, a held proof of other bytes set aside, the quarantine's syscalls in order), `Test_publication_convention`, `Test_interruption` (every rename, replace, unlink and fsync of a fresh run with one pair and with one bad pair, each from a fresh fixture, each injection asserted fired, each recovery interrupted again; a child killed after the copy), `Test_barriers_repeated` (the copy's barrier, and a held proof's, repeated before the claim goes; the claim kept when the barrier fails again) |

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
  change; its older exports keep their `<host>-<period>` names. The step
  is one way: a labelled chain that an older version of the tool has
  continued is broken at that manifest for `verify`, and refused by
  `run` until the operator has moved that version's manifests aside
  (section 10, R4).
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

## 10. Workflow 4: restore and migration

Written 2026-09-17, workflow four, and corrected 2026-09-18 after its
gate review (G1 to G5, named where they apply): what happens to the state
of sections 2, 8 and 9 when it is copied, restored, carried to another
host, or read by another version of the code, and what each of those
recoveries does when it is itself stopped. The words used here, beyond
the ones above:

- **The set**: what must come from one moment to be one appliance's
  state. The calendar's own: the calendar directory (`uri`, `hmac-key`,
  `journal`, `journal.counts`, `db/`, `journal.known-good`) and the
  receipts directory (the file and its markers). Beside it: the
  self-stamp's state directory, the watcher's directory, the adapter's
  `DATA_DIR`, the configuration, the anchor wallet.
- **Stopped copy**: a copy of the set made while every writer of it is
  stopped, or one filesystem snapshot that covers all of it. It is the
  only thing this document calls a **backup**, and only once a restore of
  it has started cleanly somewhere (R2).
- **Hot copy**: any other copy. Its members were read at different
  moments, whatever the tool that made it reports.
- **Generation**: the database's identity (section 2). It ties the
  checkpoint to the database and nothing else to anything: no identifier
  spans the set.
- **Older, newer**: a version of this code from before, or after, the
  one that wrote the state it is given.

Nothing in this tree takes a backup (README, "Requirements": backups are
the host's matter), so nothing here can call a copy one. What this code
does is at the other end: it refuses, when it next starts, a set whose own
files show that they were read at different moments, and it says which
skews its files cannot show.

### The set, member by member

| Member | Writer | Carried to another host, or made there | A copy of it taken while its writer runs |
|---|---|---|---|
| `uri`, `hmac-key` | the operator, once | carried, never made again: the URI is inside every pending proof, and a pending proof is upgraded only against it | whole: never rewritten |
| `journal` | `otsd` | carried | a prefix of itself, its last entry possibly partial (padded at the next open into an entry nobody holds, anchored like any other) |
| `journal.counts` | `otsd`, after the entry | carried | whole counts up to some entry; read after the journal it counts an entry that copy of the journal lacks: refused (R1) |
| `db/` | `otsd` | carried. Made again only as later anchors (C6, "Recovery") | LevelDB has no online copy. A copy of the whole directory taken after a synchronous batch, with nothing else running, opened and held the batch (the crash image these tests take); a copy missing a table file did not open, and was refused. What a copy of files read at different moments during a compaction opens as, if it opens, is not established here: the watermark's batch orders a crash, not a copier (2026-09-18 gate review) |
| `journal.known-good` | `otsd`, by rename | carried, or left out: one rescan | whole; read after the database it names more than the database holds: refused |
| the receipts file, `<receipts>.pending.<txid>` | `otsd` | carried, the markers with the file | the file a prefix, its last line possibly partial (dropped before the next append); each marker whole. Read after the database: R1 |
| the self-stamp's `manifests/`, `witnessed/`, inbox (with `.claim-…` and `rejected/`) | `selfstamp.py` | carried whole: the label is in the manifests. The outbox is made again every run | whole files from different moments. A manifest without its proof, a copy no manifest lists: what a stop leaves, and S1's next run finishes. A manifest that vouches for a copy the copied `witnessed/` did not yet hold (`witnessed/` read before the copy arrived, `manifests/` after it was listed): a break `verify` reports (S9), and nothing rebuilds the copy (2026-09-18 gate review, G5) |
| the watcher's `state.json`, `outbox.json`, `config` | `watch.py` | carried. `status`, `.lock` and the log are made again | whole files from different moments. A state that still owes its queue beside an older outbox: W1's replay copies the record over. A state older than its outbox alarms once more for the same transition (at least once). A state read after a transition beside an outbox read before it, once `owed` was cleared: the alert is in neither file, the restored run sends nothing and exits 0, and nothing in either file shows what the other lacked; the live watcher still owes it. Not detected, and not recovered (2026-09-18 gate review, G5) |
| the adapter's `DATA_DIR` | the adapter | carried whole | its contract, A4: the reconciliation before its door |
| the anchor wallet | Bitcoin Core | its keys carried, and never run in two places | Bitcoin Core's own rule (`backupwallet`), not this code's |
| code and configuration | the operator | the revisions that wrote the state, or newer (R4); `.env`, the tools' configs, the units | — |
| not part of the set | — | made on the host: every `.lock`, the watcher's `status`, heartbeat files, LevelDB's `LOCK` and `LOG`, `headers.bin` (from the node), the block chain (from the network), users, units, mounts, the firewall, disk encryption | — |

### Who owns unfinished work

| Handoff | Before | After | Owner in between | Evidence that lets the previous owner forget |
|---|---|---|---|---|
| live host → backup | the live set | a stopped copy that has started elsewhere | the live host: it is the record, and the previous backup is kept, until then | the restored copy's clean start (R2) |
| backup → restored host | a stopped copy | the set in place, nothing running | the operator | the copy complete, compared with its source |
| restored files → calendar | files | the storage check passed, the listener bound | the operator, until the check; then the journal, as in section 3 | the check (C6) |
| marker in the copy → receipt | `<receipts>.pending.<txid>` | the line on file, synced | the marker | C5 |
| entry in the copy → anchor | a journal entry the database lacks | saved | the journal | C5's batch |
| pending proof → its holder | a pending proof (the adapter's, the self-stamp's, a client's) | the same proof with a Bitcoin attestation | the holder, who asks; the calendar answers for as long as its journal holds the entry | the upgraded file |
| older form → newer form | state as an older version wrote it | the newer form | the newer code, in the one write that publishes the new form | that write (R3, R4) |
| old host → new host | two machines that could be this calendar | one | the operator: the old one never runs as this calendar again | none in the files: nothing records which host runs a set |

### R1. Taking a backup

| | |
|---|---|
| Authoritative record | the live set, until a stopped copy of it has started cleanly elsewhere |
| Owner of unfinished work | the operator; no code here copies anything |
| Preconditions | every writer of the set stopped, and seen to be: the adapter (so nothing is accepted that the copy will not hold), the tools' timers and any run in progress, whoever delivers into a witness inbox, then `otsd`, then Bitcoin Core if its wallet is copied as files. Stopping a timer does not stop a run already started |
| Side effects, in order | (1) the writers stopped; (2) the set copied whole into a new, empty place; (3) the revisions of the code that wrote it recorded beside it; (4) the writers started again, as soon as the copy's bytes have been compared with their source; (5) the rehearsal: the copy restored somewhere else and started under the isolation below, before it is called a backup; the previous backup kept until then. Encryption and the off-host copy are the operator's tools |
| Visibility and durability | a copy is visible file by file as it is made and is nothing until it is complete; its durability is the destination's. Work accepted after step (1) is not in it |
| The rehearsal's isolation | the original is running again, so the rehearsal must have no effect outside its own directories: a host that reaches no Bitcoin node and no wallet (the stamper waits, logging, and broadcasts nothing), that no client and no witness delivers to, the watcher with `NTFY_URL` empty, the self-stamp's outbox delivered nowhere. What it shows: the calendar starts or refuses (C6), the markers settle into the copy's receipts file (C5), the entries the database lacked are pending, `GET /timestamp` answers for the commitments it should, `verify` passes on the manifests, the watcher's `--dry` reads its state. A rehearsal without that isolation has spent from the wallet, anchored the same commitments twice, alarmed or delivered from the copy: it proves nothing, and the copy is not called a backup on its account. Making a restored copy the calendar for good is R5, and the original then never runs again (2026-09-18 gate review, G4: the first close resumed the original and started the copy with nothing said between) |
| Ambiguous outcomes | a copy that was interrupted: incomplete, and never completed from a later moment (that is a hot copy); a writer that ran during the copy: a hot copy |
| What the next start does with a hot copy | refused by the storage check (C6), before the listener: a checkpoint read after the database (`older than the checkpoint`); a journal read before a checkpoint it does not reach; a sidecar read after the journal (`journal.counts holds a count for journal entry N, but the journal holds …`: 2026-09-17 workflow four, the start used to be clean and the stale count waited for the next entry N); a `db/` that does not open (`db/ does not open (CorruptionError)`: it used to be a traceback carrying LevelDB's text, which names the directory, and no recovery). Refused by the stamper's open, a moment after the listener: a receipt read after the database with its marker standing (C5, "the state no stop leaves"). Coherent, shown as controls: any copy of the whole tree at one instant, the database copied open after its synchronous batch included (a crash image); a journal newer than the database (the rest is pending); a checkpoint older than the database (a longer scan); a marker in the copy beside a database that has the save (the receipt is recovered, once) |
| Intended guarantee | a backup is a stopped copy that has started cleanly elsewhere; a hot copy is refused at its next start wherever its own files show the skew, and is never called a backup by anything here |
| Current defects | (a) no identity spans the set: a calendar, an adapter and a self-stamp copied at different moments each start cleanly, and the skew shows only as R2's `Not found`. (b) Two skews inside the calendar's own set are not detected, because once a marker has gone nothing on file ties a receipt to a commitment: receipts read after their marker went, beside an older database (the records of that anchor are anchored and receipted a second time); and a database read after the save, beside receipts read before the marker was written (the receipt is lost, an undercount). (c) In the configuration behind the gateway the backup is the gateway repository's `ops/backup-live-state.sh` (read at 713269e, not changed here: it is the payment session's). It stops nothing, archives the calendar and receipts directories with one `tar` while `otsd` runs, and reports `ok` on the strength of its own members (the obligations snapshot, the encryption, the push). Its guide, `ops/BACKUP-RECOVERY.md`, says of `db/` that the copy "is not a consistent snapshot of any instant and may not open"; its list of the calendar's files predates the checkpoint and the markers, which travel because the directory is archived whole; it has no member for the adapter's, the self-stamp's or the watcher's state. Its `ok` is not a statement about this calendar: what it archives is a hot copy, to be treated as R2 treats one |
| Operating assumptions | the operator stops every writer and sees that they stopped; the destination keeps the bytes it was given; one host, one set |
| Postconditions tested | `test_restore_calendar.Test_a_copy_taken_while_the_calendar_writes`: a copy of each member taken at each point of one submission and one anchor's save (the boundaries between the writes), composed into sets; every refusal above, each coherent control, and both undetected skews pinned as they are |

### R2. Restoring the set

| | |
|---|---|
| Authoritative record | the backup until the restored set has started; from then the restored set, with every obligation the copy held: journal entries the database lacks, markers, manifests without proofs, messages owed, debts |
| Owner of unfinished work | the operator until the storage check passes; then each record's owner as sections 3, 8 and 9 name it |
| Preconditions | the destination runs nothing; the old host, if it still exists, does not run as this calendar (R5); the code is the revisions that wrote the set, or newer (R4); every member is there. A member left out is not filled in with a default that works: a missing `journal` or `db/` beside a checkpoint is refused, a missing `uri` or `hmac-key` exits 1. A missing checkpoint is a rescan; a missing sidecar is unknown counts, receipts that undercount; a missing receipts directory is R1's undercount, not detected |
| Side effects, in order | (1) the set copied into place, whole, with nothing running; (2) Bitcoin Core started, its wallet loaded; (3) `otsd` started: the storage check (C6) before the listener is bound, so a compatible generation is established before the calendar's intake opens; then the stamper's open settles every marker (C5) or stops the service on the state no stop leaves; then the fill pass reads the journal from the checkpoint; (4) this host's anchors save what the database lacked, and the pending proofs complete; (5) the adapter started: its reconciliation runs before its door (its contract, A4), and its upgrader asks this calendar, at its configured `CALENDAR_URL`, for each pending proof; (6) the tools' timers: the self-stamp's next run finishes from its files (S1), the watcher's copies a recorded queue to its outbox and delivers (W1) |
| Visibility and durability | each component's own (C5, S3, W1): a restore adds no write of its own. There is no record that a restore happened or finished |
| What the holder of a pending proof is told | `200` when the database holds the commitment (saved before the copy, or by this host since); `404 Pending confirmation in Bitcoin blockchain` while the journal here holds it and no anchor has saved it; `404 Not found` when the journal here does not hold it: accepted after the copy was taken, or lost with an older journal. `Not found` is also the answer for the seconds between an entry's append and the stamper's next fill pass, and during a scan that has not reached the entry; past that, it does not change |
| What the journal check cannot detect | an older journal that is a prefix of the true one and still reaches the checkpoint: the start is clean and the entries beyond it are gone; a journal that differs only between the two probed entries; and, with no checkpoint on file, anything at all about the journal (the rescan reads what is here). A sidecar from the newer moment shows the first of these, when there is one; a sidecar copied with its journal does not |
| Recovery | a refused start: restore the members from one stopped copy; or take the way on that the refusal names (delete the checkpoint: one rescan, later blocks; delete `journal.counts`: unknown counts; move `db/` aside: every commitment anchored again; remove a contradicted marker: a second receipt, reconciled by hand). A restore that stopped part way: finish the copy; a start refused meanwhile wrote nothing that the finished copy trips over |
| Intended guarantee | every obligation the backup held is met by the restored host; nothing is anchored, receipted or alarmed twice because of the restore; work accepted after the backup was taken is lost, and is said to be |
| Current defects | the tools read every `404` as pending, with no deadline (S5; the adapter's A3): a proof whose entry is gone stays pending for ever and nothing on the box says so. The operator's way on: for the self-stamp, the proof moved aside, and the next run stamps the manifest again under a later block (README, "Recover"); for the adapter, the proof set aside as its contract's A4 names it, so that its next start recreates the debt |
| Operating assumptions | the backup is a stopped copy; Bitcoin is doubled in every test here, and no restore was made on a second machine |
| Postconditions tested | `test_restore_calendar.Test_a_restore_onto_a_fresh_host` (a set with a receipt owed and an entry pending, copied under another root: the receipt settled once, the saved proofs complete from the restored database, the pending one by this host's next anchor, the generation carried, a restart receipting nothing twice); `Test_an_older_journal_beside_a_newer_database` (detected twice, not detected three ways, `Not found` against its control `Pending`); `Test_a_restore_that_stopped_part_way` (each member missing; a restore finished after a refused start); `test_restore_tools.Test_a_restored_self_stamp`, `Test_a_restored_watcher` |

### R3. A checkpoint from before generations

| | |
|---|---|
| Authoritative record | the journal and the database. The checkpoint is a rebuildable index (section 2) and was never authority for what the database holds |
| Owner of unfinished work | the operator, for one step; then the journal |
| The state | `journal.known-good` holding an index alone (upstream's form, and this fork's before fcbafb6, 2026-09-15), beside a database with no generation |
| Rule | refused, whatever the database holds: an index does not say which database it describes, and a probe of the one entry below it is a guess at the rest. `CALENDAR STORAGE INCONSISTENT: journal.known-good holds an index alone (N) … Recovery, once: delete journal.known-good … and start again`, exit 1; the refusal writes nothing: the database is not stamped, the file is not rewritten. (fcbafb6 adopted such a checkpoint when the entry below it was in the database, in two writes, the database's generation and then the file; a stop between them left a start that was refused as `db/ was recreated, or an older checkpoint was restored`, which had not happened, and ended in this same rescan. 2026-09-17 workflow four took the adoption out) |
| Side effects, in order | (1) the operator deletes the file, once; (2) the next start finds no checkpoint and gives the database its generation with watermark 0, in the one synchronous batch a new database gets its own; (3) the scan starts at 0: what the database holds is skipped by its membership probe, what it lacks is pending; (4) the next confirmed anchor writes the checkpoint in the current form. The deletion and the generation are once; the scan from 0 repeats at every start until (4), and at every start of a database that already holds every entry until an anchor confirms |
| Visibility and durability | the generation: LevelDB's synchronous batch. The checkpoint: visible at its rename, durable at the directory fsync after it |
| Ambiguous outcomes | a stop before the batch: the next start adopts; a stop after it: the next start finds the generation and adopts nothing; a stop anywhere in the checkpoint's publication leaves the old file or the new, both at or below the watermark, and either starts. No checkpoint is ever written without the database's generation: an index alone is a file the next start refuses |
| Older code, given the current form | refuses, and not cleanly: before fcbafb6 the stamper read the file as `int(text.strip())` with only a missing file caught, so `INDEX GENERATION` raises in the stamper's thread behind a listener that stays up (the defect fcbafb6 fixed). Quoted from c5f6545, not run here |
| Operating assumptions | the rescan's cost is one membership probe per journal entry per start, until the checkpoint is written |
| Postconditions tested | `test_restore_calendar.Test_checkpoint_from_before_generations` (refused with the rescan named and nothing written, the entry below the index being held; the rescan performed once and losing nothing; a stop on either side of the batch; the old reader's expression against the current form); `test_calendar.Test_storage_generation`; `test_stamper_checkpoint.Test_checkpoint_written` (without a generation no checkpoint is written); `test_restore_calendar.Test_a_recovery_that_is_stopped_again.test_every_write_of_the_checkpoint` |

### R4. The other changes of form

Each is one way. The newer code takes the step once, in a write it would
have made anyway, and a stop at any of that write's calls is finished by
the next run. What older code does with the newer form is measured, not
assumed; it refuses cleanly in one place only, and a downgrade is
therefore not supported.

| Step | The newer code, once | Stopped part way | Older code, given the newer form |
|---|---|---|---|
| the checkpoint gains the generation (fcbafb6) | R3 | R3 | R3 |
| one receipt marker name becomes one per anchor, `<receipts>.pending.<txid>` (3961a1f) | reads both names and settles each as C5 does; nothing is renamed, and nothing writes the old name any more | every write of settling a marker under the old name, swept | fcbafb6 reads and removes the one old name and no other (read from its source): a per-anchor marker is invisible to it, stays on file, and is settled when the newer code is back. Neither a refusal nor a loss |
| `selfstamp/2` becomes `selfstamp/3`: the chain gets its label (ad64300) | the first manifest written under this code carries a new label and links to the older chain by `prev`; every later one copies it. **A label is drawn once**: when the newest manifest has none, the run reads the chain, and if any manifest has one it refuses (`refused … the chain has a label and its newest manifest … has none`), exit 1, no manifest written; a manifest it cannot read leaves the question open and is refused too (2026-09-17 workflow four: a labelled chain that an older version had continued used to be given a second label). Older manifests are never rewritten | every rename, replace, unlink and fsync of that run, swept: the manifest once renamed into place is never built again, and the label it carries is the chain's | 3961a1f refuses cleanly where it validates: `verify` (`schema 'selfstamp/3'`, a break) and its inbox (`rejected/`, kept). Its writer does not: `run` reads its predecessor's `seq` and hash and continues a labelled chain with an unlabelled `selfstamp/2` manifest. The current `verify` then says `chain label missing after a labelled manifest`, and the current `run` refuses as above until the operator has moved that version's manifests out of `manifests/` (kept; those days are a gap) |
| the watcher's state gains `owed` (4ace673) | an older state has no such key and needs no step: every field may be absent (W8) | every rename, replace, unlink and fsync of the first run over an older state and over one with `owed`, swept: the restore is not a transition, the message is delivered once | ad64300 ignores the key, delivers none of it, exits 0 and leaves it in place. On the way back the record is the queue (W1), so an alert that version queued meanwhile is dropped unsent; it cannot be told from one already delivered from the record. `owed` stands only between two writes of one run: let a current run finish before an older one is run |
| the adapter's event word `anchored` becomes `bitcoin_attestation_present` (its repository, 6328e93) | a word in its log, which is never rewritten: a reader of older lines knows both. No file of its `DATA_DIR` changed name or form (`pending/<fp>` and `.built` are as 84d9aa0 made them; the marker that was renamed is the calendar's, above), and its index is rebuilt at every start (A4) | its contract, A4 | nothing for it to refuse: the form did not change |

| | |
|---|---|
| Intended guarantee | a step is taken once and survives a stop at any of its writes; state an older version cannot read is refused by it, not half understood |
| Current defects | older versions refuse only where the table says. They were written before there was a rule, and nothing here can change them. The watcher's state carries no mark of its form: a future change to it must add one, since today's code, like ad64300, ignores a key it does not know |
| Operating assumptions | versions move forward; a way back is a backup taken before the step, with what was accepted since given up and said to be |
| Postconditions tested | `test_restore_tools.Test_what_older_tools_do_with_newer_state` (the two tools as they were, run from `ops/tests/migration/`, each file's sha256 checked first), `Test_a_restored_self_stamp`, `Test_a_restored_watcher`; `test_restore_calendar.Test_a_recovery_that_is_stopped_again` (the old marker name); `test_selfstamp_workflow.Test_legacy_compatibility`, `Test_period_and_observation` (a predecessor under an unknown schema refused) |

### R5. Another host

| | |
|---|---|
| Authoritative record | the backup, and the proofs already in their holders' hands |
| Owner of unfinished work | the operator, who carries the set and sees that the old host never runs as this calendar again: two hosts on one wallet spend each other's outputs, and of two behind one `uri` each lacks the other's journal entries |
| Rule | nothing in the set names a host. A proof is a path from a digest to a block; a pending proof names the calendar's `uri`, which is carried. The calendar's messages name its files by their fixed names; LevelDB writes no path into its files; a `selfstamp/3` manifest holds no host name or path (section 9), and its chain's label is in the manifests, which are carried; the watcher's `NAME` is configuration |
| Carried, and made again | the table above. Carried and never made again: `uri`, `hmac-key`, the wallet's keys, the chain label. Where the `uri` is an onion address, the hidden-service key is part of the identity and is carried with it |
| Ambiguous outcomes | none in the files: the set does not know which host it is on. A manifest written under `selfstamp/1` or `/2` still names the host it was written on; nothing rewrites it |
| Operating assumptions | the new host's clock, Bitcoin node and disk are its own to qualify; disk encryption sealed to the old hardware is recovered by the operator's own means |
| Postconditions tested | `test_restore_calendar.Test_a_restore_onto_a_fresh_host` (the set under another root, the first gone: R2's postconditions; no file of the set contains the path it was made under; the pending proof names the `uri`); `test_restore_tools.Test_a_restored_self_stamp` (the chain under another directory keeps its label; a carried pending proof completed where it lands) |

### R6. A recovery that is stopped again

| | |
|---|---|
| Authoritative record | what is on file when the next start looks; never a previous run's memory, log or exit code |
| Rule | every recovery above is a sequence of writes each of which leaves a state the same recovery starts from: a refusal writes nothing; a settling appends, syncs, then unlinks, and a discard whose unlink fails is asked again and answered the same (C5); the adoption is one batch; the checkpoint and every file of the tools are published by one rename |
| Death during a restore | a member not yet copied: refused where the files show it (`journal`, `db/`), a rescan or unknown counts where that is safe (checkpoint, sidecar), R1's undercount for the receipts directory; the copy finished afterwards starts |
| Death during a migration | R3's batch, on either side; R4's first labelled run and the watcher's first run, at every call |
| Death during the first run after either | the settling of every marker state a stop or a restore can leave: the save not made, the receipt owed, the receipt on file, the append torn, the marker under its old name, a marker that does not parse; the checkpoint's publication; and beside the stops, a failure the settling handles and goes on from: a discard whose unlink is refused, then the next anchor over the same commitments |
| Visibility and durability | a file is visible from its rename and durable from the fsync after it. A stop injected after a rename leaves the file; a power cut may not. The stops are injected as exceptions at named calls, and an exception unwinds through the code's own cleanup (`finally`, the closing of files), which a killed process or a power cut does not: the child-process kills of workflows two and three are the nearer model, and none is a power cut |
| Postconditions tested | `test_restore_calendar.Test_a_recovery_that_is_stopped_again` (a stop at every named call of each settling and of the checkpoint's publication: `write`, `ftruncate`, `fsync`, `unlink`, `rename`, `replace`, as many of each as a clean recovery of that fixture makes, from a fresh copy of each fixture, each injection asserted to have fired, each recovery stopped once more at its first remaining call of the kind, then finished: one receipt per saved anchor, no marker, every entry saved or pending, and a further start adding nothing; the count of calls swept is asserted, so the coverage claimed is the coverage there is, and the checkpoint's sweep has no `write` to stop at, its file being written buffered; the discard whose unlink is refused, on the real store), `Test_checkpoint_from_before_generations`, `Test_a_restore_that_stopped_part_way`; `test_restore_tools` (the same sweep over the tools' first runs; the watcher's mixed copy pinned as its loss) |

### Messages

No message of a refusal, a settling or a migration names the directory
the calendar or its receipts are in, nor the receipts file's name: the
path and the name are the operator's and can name a client. Files are
named by their fixed names (`journal.known-good`, `db/`, `journal.counts`,
`uri`), a marker by its role and its anchor's txid (`the pending receipt
marker of anchor …` only when the marker's suffix is a txid, 64 hex
characters; else `under the old single name`, which a receipts file
whose own name holds `.pending.` also gets: corrections review, G3b), an
error by its class and errno (an `OSError`'s text carries the path,
LevelDB's the directory, and a decoding error's repr the bytes it
refused: corrections review, G3a), a malformed checkpoint by its shape,
never its bytes and never `int()`'s own words for them. Before
2026-09-17 every refusal, the marker and tail messages and the sidecar's
warnings carried the path; the first close of workflow four named a
marker by its file name, let a checkpoint that could not be read escape
as a traceback at one reader and as a line carrying the path at the
other, and quoted a malformed checkpoint's bytes (2026-09-18 gate review,
G2 and G3). The tools already kept to this (sections 8 and 9); the
watcher's four storage-failure lines log an `OSError` with `%r`, which is
its class, errno and text and never the file name, pinned as a control. A
traceback, which Python prints for an error nothing expected, names
source files and is not one of these messages.
`test_restore_calendar.Test_messages_name_no_path`;
`test_restore_tools.Test_a_restored_watcher.test_a_storage_error_is_logged_without_the_file_it_names`.

### Assumptions and limits

- None of this is proof against a power cut. The fault models are a
  stop or an error injected at a named call, a member of the set taken
  from another moment or left out, a table file removed from a copied
  database, and the older tools run as they were. A stop injected after
  a rename leaves the file in place; a power cut may not.
- Nothing ran on Linux, no restore was made on a second machine, and
  Bitcoin is doubled throughout.
- No identity spans the set (R1): the calendar's generation ties one
  file to one database.
- Two skews of the receipts against the database are not detected (R1);
  a lost journal tail is detected only by a sidecar from the newer moment
  (R2); a holder of a proof whose entry is gone is never told (R2); a
  watcher state copied after a transition beside an outbox copied before
  it has lost that alert, unseen; a self-stamp manifest copied after it
  vouched for a copy that was not copied is a break `verify` reports and
  nothing rebuilds (G5).
- What a LevelDB directory copied file by file during a compaction opens
  as, if it opens, is not established here; the crash image, the whole
  directory after a synchronous batch, is what was copied.
- A downgrade is not supported, and what it does is R4's last column.
- The hosted configuration's backup is a hot copy of this calendar (R1),
  made by another repository's script, which this workflow read and did
  not change.
