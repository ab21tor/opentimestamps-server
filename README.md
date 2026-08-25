# OpenTimestamps Calendar Server

This package provides the `otsd` daemon, a calendar server which provides
aggregation, Bitcoin timestamping, and remote calendar services for
OpenTimestamps clients. You *do not* need to run a server to use the
OpenTimestamps protocol - public servers exist that are free to use. That said,
running a server locally can be useful for developers of OpenTimestamps
protocol clients, particularly with a local Bitcoin node running in regtest
mode.


## Installation

You'll need a local Bitcoin node (version 24.0 is known to work) with a wallet
with some funds in it; a pruned node is fine. While `otsd` is running the
wallet should not be used for other purposes, as currently the Bitcoin
timestamping functionality assumes that it has exclusive use of the wallet.

Install the requirements:

```
pip3 install -r requirements.txt
```

Create the calendar:
```
mkdir -p ~/.otsd/calendar/
echo "http://127.0.0.1:14788" > ~/.otsd/calendar/uri
echo "bitcoin donation address" > ~/.otsd/calendar/donation_addr
dd if=/dev/random of=~/.otsd/calendar/hmac-key bs=32 count=1
```

The URI determines what is put into the URI field of pending attestations
returned by this calendar server. For a server used for testing, the above is
fine; for production usage the URI should be set to a stable URL that
OpenTimestamps clients will be able to access indefinitely.

The donation address needs to be a valid Bitcoin address for the type of
network (mainnet, testnet, regtest) you're running otsd on. It's displayed on
the calendar info page.

The HMAC key should be kept secret. It's meant to allow for last-ditch calendar
recovery from untrusted sources, although only part of the functionality is
implemented. See the source code for more details!

To actually run the server, run the `otsd` program. Proper daemonization isn't
implemented yet, so `otsd` runs in the foreground. To run in testnet or
regtest, use the `--btc-testnet` or `--btc-regtest` options. The OpenTimestamps
protocol does *not* distinguish between mainnet, testnet, and regtest, so make
sure you don't mix them up!

To use your calendar server, tell your OpenTimestamps client to connect to it:
```
ots stamp -c http://127.0.0.1:14788 -m 1 FILE
```

OpenTimestamps clients have a whitelist of calendars they'll connect to
automatically; you'll need to manually add your new server to that whitelist to
use it when upgrading or verifying:

```
ots -l http://127.0.0.1:14788 upgrade FILE.ots
```

If your server is running on testnet or regtest, make sure to tell your client
what chain to use when verifying. For example, regtest:
```
ots --btc-regtest -l http://127.0.0.1:14788 upgrade FILE.ots
```

Tip: with regtest you can mine blocks on demand to make your timestamp confirm
with the `generate` RPC command. For example, to mine ten blocks instantly:

```
bitcoin-cli -generate 10
```

By default `otsd` binds to localhost; `otsd` is not designed to be exposed
directly to the public. Never expose the calendar's HTTP port publicly: the
homepage discloses the anchor wallet's balance and address, the pending
queue, and the complete anchor transaction history to anyone who can reach
it. Serve clients through the gateway; if the calendar itself must be
reachable, put an authenticating reverse proxy in front.

## Anchor receipts

Set the `OTSD_ANCHOR_RECEIPTS` environment variable to a file path and the
stamper appends one JSON line per anchor transaction, recording what that
anchor actually cost. The file exists for the gateway's anchor-billing
feature, which reads and dedupes it; nothing in this server ever reads it
back. Unset (the default), the feature is entirely off and the server
behaves exactly as before.

A natural home is inside the calendar directory, e.g.
`~/.otsd/calendar/anchor-receipts.jsonl`, but any writable path works.

Exactly one line is appended per anchor, at the moment the stamper's own
existing logic considers the transaction confirmed — the same point the
commitments are saved to the calendar; no new confirmation policy is
introduced. Broadcasts and RBF fee bumps write nothing; a replaced txid
never appears in the file. The file is append-only JSONL: each line is
appended atomically, and existing bytes are never rewritten or truncated.

Each line is a JSON object with exactly these fields. The format is an
interface parsed by other software — treat it as pinned:

- `txid` — the anchor transaction id; 64 hex characters, RPC display
  order.
- `fee_sats` — the actual final fee of the confirmed transaction, in
  satoshis. The stamper constructs the transaction, so this is input
  value minus output value of the version that confirmed — the fee after
  any RBF bumps, never an estimate.
- `commitments` — the number of commitments this anchor carried.
- `confirmed_height` — the height of the block containing the anchor
  transaction.
- `confirmed_at` — unix time at which the stamper deemed the transaction
  confirmed and wrote the line. Bookkeeping, not consensus data: this is
  the stamper's wall clock, not a block timestamp.
- `records` — the number of individual digest submissions whose
  commitments sit inside this transaction's anchor tree. A record is one
  submission accepted by the aggregator — one leaf of a per-second merkle
  tree. A digest re-submitted within the aggregator's dedupe horizon
  (in-memory, one hour, capped at 65536 entries, refreshed on every hit)
  attaches to the existing pending commitment and is counted once. The
  count is fixed when the anchor tree closes over the pending
  commitments; the receipt carries the count of the tree that actually
  confirmed. The counter errs low, never high, with one bounded
  exception: resubmission across a restart boundary (or past the
  horizon), where the fresh aggregator cannot know the digest was
  already counted, records the same digest twice. Otherwise miscounts
  only ever err low: a commitment whose count is unknown (recorded
  before this feature was enabled, or lost to a crash) sums as 0, with
  one warning logged per affected tree. `0` therefore means "unknown",
  not "empty" — a real tree always has at least one leaf.

The per-tree counts behind `records` live in a sidecar next to the
journal, `journal.counts`: one 4-byte big-endian integer per journal
entry index, written only after the journal entry itself is durable, so a
crash can lose counts but never invent them. The sidecar exists only when
`OTSD_ANCHOR_RECEIPTS` is set and grows at 4 bytes per journal entry —
1/11th of the journal's own growth, sharing its append-only lifecycle.
Nothing needs to be migrated when enabling the feature on an existing
calendar: older entries simply have no counts. The sidecar is per-second
activity metadata: it records how many submissions each second's tree
carried — no digests, but a traffic-volume record, sharing the calendar's
privacy posture.

Two guarantees:

- A receipt write failure never breaks anchoring. The stamper logs one
  warning and continues; the calendar save always happens.
- A crash at the wrong moment may produce a duplicate or extra line,
  never a silently missing one. The receipt is written before the
  calendar save: a missed receipt is silently lost revenue, while the
  gateway dedupes by txid, so the trade goes to the extra line.

Under records × rate billing that second guarantee has a sharper
consequence than an extra line: the commitments left unsaved by a crash
between the receipt write and the calendar save are re-anchored on restart
under a new txid, producing a second receipt and a second bill for the
same records. The ordering still goes to duplicate-over-miss — a missed
receipt is revenue nothing downstream can surface, while a duplicate is
visible on /anchor-bills and refundable, and the `records` field on every
bill lets the client reconcile records billed against records submitted.
The operator-side handling is in the timestamp-gateway operator guide,
"Anchor billing" → "The double-bill crash edge".

## Anchor cadence

Anchor departures happen only at the scheduled moments of a free-running
jittered clock: the next departure is always `min_tx_interval` × a uniform
random factor between 1 and 2 in the future, re-armed when an anchor
confirms, when the clock expires over an empty queue, and at startup. An
idle window rolls the clock silently, so the first commitment after
idleness waits for the next scheduled departure like any other. Anchor
timing is therefore independent of submission timing: a broadcast time
never reveals when a commitment arrived. There is no new configuration —
the schedule is governed by the existing `--btc-min-tx-interval` knob.

## Restart checkpoint (journal.known-good)

`<calendar>/journal.known-good` holds the journal index the stamper's
restart scan may begin at: everything below it is already anchored in the
calendar. Upstream only ever *read* this file (an operator convenience,
never written); this fork writes it after every confirmed anchor — the
lowest journal index still outstanding (pending, or riding a mined tree
that has not yet reached `--btc-min-confirmations`), or the scan cursor
itself when nothing is outstanding. Written atomically beside the journal;
a failed write warns and never interrupts anchoring. Without it a restart
re-reads the entire journal and probes the calendar once per entry — a
cost that grows with all history; with it, a restart's catch-up is one
anchor window. Deleting the file is always safe and merely restores the
slow full rescan. There is no new configuration.

## Unit tests

Test modules live under `otsserver/tests/`:

- `test_calendar.py` — inherited from upstream.
- `test_otsd_launcher.py`, `test_rpc_homepage.py`, `test_stamper_loop.py`,
  `test_anchor_receipts.py`, `test_stamper_cadence.py`,
  `test_rpc_digest.py`, `test_anchor_records.py`,
  `test_aggregator_dedupe.py`, `test_stamper_read_errors.py`,
  `test_stamper_checkpoint.py`, `test_stamper_wallet_empty.py` — regression
  tests for this branch's delta (launcher flags, homepage RPC wiring,
  stamper-loop crash fixes, anchor receipts, anchor cadence, the /digest
  Content-Length handling, anchor-receipt record counts, aggregator
  dedupe, pending-fill read-error survival, the restart checkpoint,
  empty-wallet warn-once). They stub everything external with
  `unittest.mock`: no bitcoind, no network.

No test module needs a running Bitcoin node. Every module does need the
full dependency set installed, and one dependency — `leveldb` — is a native
build: `otsserver/calendar.py` imports it at module level, so without it every
module fails at collection with `ModuleNotFoundError`.

Run the suite in the deployment-matched environment — the otsd image (base
`python:3.11-slim` plus `build-essential libleveldb-dev` and
`pip install -r requirements.txt`) — or in a Python ≤ 3.11 venv:

```
python -m unittest discover -v                       # Ran 42 tests ... OK
```

The otsd image has no pytest; where pytest is installed,
`python -m pytest otsserver/tests -q` runs the identical set.

Known limit: py-leveldb does not build on Python 3.12+ (`pip install -r
requirements.txt` fails on the `leveldb` wheel, so nothing is importable and
no test can run). Other platforms and Python versions have not been tried;
treat any claim about them as unverified until you run the command above.
