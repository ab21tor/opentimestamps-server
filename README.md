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
directly to the public and requires a reverse proxy for production usage. An
example configuration for nginx is provided under `contrib/nginx`.

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

Two guarantees:

- A receipt write failure never breaks anchoring. The stamper logs one
  warning and continues; the calendar save always happens.
- A crash at the wrong moment may produce a duplicate or extra line,
  never a silently missing one. The receipt is written before the
  calendar save: a missed receipt is silently lost revenue, while the
  gateway dedupes by txid, so the trade goes to the extra line.

## Unit tests

Test modules live under `otsserver/tests/`:

- `test_calendar.py` — inherited from upstream.
- `test_otsd_launcher.py`, `test_rpc_homepage.py`, `test_stamper_loop.py`,
  `test_anchor_receipts.py` — regression tests for this branch's delta
  (launcher flags, homepage RPC wiring, stamper-loop crash fixes, anchor
  receipts). They stub everything external with `unittest.mock`: no
  bitcoind, no network.

No test module needs a running Bitcoin node. Every test module DOES need the
full dependency set installed, and one dependency — `leveldb` — is a native
build: `otsserver/calendar.py` imports it at module level, so without it every
module (upstream-inherited and delta alike) fails at collection with
`ModuleNotFoundError`, before a single test runs.

Verified 2026-07-28 in the deployment-matched environment — the otsd image
(base `python:3.11-slim` plus `build-essential libleveldb-dev` and
`pip install -r requirements.txt`), plus `pip install pytest`:

```
python -m pytest otsserver/tests -q                  # 17 passed
python -m pytest otsserver/tests/test_otsd_launcher.py \
    otsserver/tests/test_rpc_homepage.py \
    otsserver/tests/test_stamper_loop.py \
    otsserver/tests/test_anchor_receipts.py -q       # 13 passed (branch delta)
python -m pytest otsserver/tests/test_calendar.py -q # 4 passed (upstream)
python3 -m unittest discover -v                      # Ran 17 tests ... OK
```

Verified failure mode on a clean macOS machine (Python 3.13, fresh venv):
`pip install -r requirements.txt` fails building the `leveldb` wheel, so
nothing is importable and no test can run. Other platforms and Python
versions have not been tried here; treat any claim about them as unverified
until you run the commands above.
