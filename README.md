# OpenTimestamps Calendar Server

This package provides the `otsd` daemon, a calendar server which provides
aggregation, Bitcoin timestamping, and remote calendar services for
OpenTimestamps clients. You *do not* need to run a server to use the
OpenTimestamps protocol - public servers exist that are free to use. That said,
running a server locally can be useful for developers of OpenTimestamps
protocol clients, particularly with a local Bitcoin node running in regtest
mode.

## Two shapes

This fork is the calendar of two deployment shapes, and it is the same code
in both:

- **Hosted**: the calendar behind the `timestamp-gateway` (an L402 door, or
  a free door with anchor billing), sold across a trust boundary; the payer
  is `auto-anchor`, the client adapter `api-endpoint` in its `GATEWAY_URL`
  mode. The gateway repo's README and operator guide are the install.
- **Appliance**: one box runs bitcoind, this calendar alone
  (`docker-compose.enterprise.yml`, loopback only), the client adapter in
  its `CALENDAR_URL` mode, the self-stamper and the watcher (both in
  `ops/`). No gateway, no Lightning, no payer, no payment anywhere; the
  receipts file is the box's logbook. The install is "The appliance
  shape" below, written to be followed by a stranger.

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
  one warning logged per affected tree. A count the stamper's scan read
  before the aggregator had written it is read again when the anchor
  tree closes, so a busy second is never lost to that race; only a count
  still absent then is a hole. `0` therefore means "unknown", not
  "empty" — a real tree always has at least one leaf.

The per-tree counts behind `records` live in a sidecar next to the
journal, `journal.counts`: one 4-byte big-endian integer per journal
entry index, written only after the journal entry itself is durable, so a
crash can lose counts but never invent them. The sidecar exists only when
`OTSD_ANCHOR_RECEIPTS` is set and grows at 4 bytes per journal entry —
1/11th of the journal's own growth, sharing its append-only lifecycle.
Nothing needs to be migrated when enabling the feature on an existing
calendar: older entries have no counts. The sidecar is per-second
activity metadata: it records how many submissions each second's tree
carried — no digests, but a traffic-volume record, sharing the calendar's
privacy posture.

Two guarantees:

- A receipt write failure never breaks anchoring. The stamper logs one
  warning and continues; the calendar save always happens, and the
  receipt is kept in the pending marker (below) until it can be written.
- No crash bills the same records twice, and a crash loses at most one
  receipt, named. The receipt is written *after* the calendar save,
  guarded by a marker: before the save the stamper writes
  `<receipts file>.pending` — the receipt line plus one commitment of the
  anchor's tree — atomically; after the save it appends the receipt and
  removes the marker. A marker still present at the next start (or when
  the next anchor confirms) is settled by asking the calendar whether it
  holds that commitment. It does: the save completed, the receipt is
  owed, and it is appended unless its txid is already on file
  (`recovered from the pending marker`). It does not: the save never
  happened, those commitments are still pending and will be re-anchored
  under a new txid with their own receipt, so this receipt is discarded
  (`nothing is owed for <txid>`) — writing it would have billed the same
  records twice. An unreadable marker is set aside as
  `<marker>.corrupt-<time>` and warned about. This replaced the earlier
  receipt-before-save ordering, whose one operator-visible failure was
  exactly that double bill (full review, 2026-09-08).

Turning the feature off again is loud. If `OTSD_ANCHOR_RECEIPTS` is unset
while the sidecar already exists, the stamper warns at startup that
receipts were on before and are off now — the calendar is anchoring for
free — and the status page's `Anchor receipts: on|off` line says which.
The gateway's `/health` reads that line and reports `billing:
receipts_off` while its own billing is on.

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

An in-flight anchor whose input stops being a confirmed unspent output —
a shallow reorg took the parent whose change it spends, or a version this
stamper does not track (one a restart forgot) was mined — can never be
bumped again. The stamper notices the moment a bump cannot be priced,
warns once, drops the dead versions, and starts a fresh cycle from the
wallet's confirmed outputs on the next pass; the pending commitments are
untouched and anchor once, so nothing is lost and nothing is billed
twice. A fee cap that blocks the next transaction is likewise logged once
on entry and once on recovery, not once per loop second.

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

## Operator lane and self-stamp (the notary notarising itself)

Two pieces, both off by default: a second digest door on the calendar that
never produces a record, and a small stdlib tool in `ops/` that uses it to
notarise the box's own books once a day.

### The operator lane: `POST /operator/digest`

Set `OTSD_OPERATOR_LANE=1` in the calendar's environment and the server
serves `POST /operator/digest`. It takes exactly what `/digest` takes (a
raw digest, the same Content-Length bounds) and answers exactly what
`/digest` answers (the serialized pending timestamp). The digest becomes a
leaf of the same per-second merkle tree, is committed to the same journal,
and rides the same anchor transaction. The one difference: it is not a
record. The aggregator reports a round's `records` as the number of leaves
that came through `/digest`; operator leaves are excluded, so they never
reach a receipt's `records` field and never become a bill. `commitments`
in the receipt still counts the commitment that carries them, because it
is a real commitment. Unset (the default), the path is an ordinary 404 and
nothing in the server changes.

A round whose leaves all came through the operator lane holds no billable
record. That is a known zero, not a hole: the record-count sidecar stores
it as the sentinel `0xFFFFFFFF`, which `RecordCounts.get` returns as `0`.
Any stored value at or above `2^31` reads as `0` — a one-second tree cannot
have that many leaves, and every torn big-endian prefix of the sentinel
(`0xFF000000`, `0xFFFF0000`, `0xFFFFFF00`) stays above the threshold, so
the sidecar's rule that a torn write can only ever undercount still holds;
values already in the file are untouched. The stamper sums a known zero
silently instead of warning that the receipt will undercount, and a
receipt whose whole tree was operator leaves carries `records: 0`, which
the gateway already leaves unbilled.

Anything that can reach the calendar's port can use the lane, so it is
for host-local operator tools behind the gateway's door: on the island
deployment otsd is published on `127.0.0.1:14788` only, and the paid door
stays the gateway.

### The self-stamp: `ops/selfstamp.py`

Once a day (a user timer, `ops/systemd/selfstamp.timer`, 00:30 UTC,
`Persistent=true`) the tool writes a manifest of the box's own books for
the previous UTC day, hash-chains it to the previous manifest, and stamps
the sha256 of the manifest file through the operator lane. The proof
rides whatever anchor real traffic pays for next — the tool never forces
an anchor — and the next run upgrades it to a Bitcoin attestation with
one `GET /timestamp/<commitment>`. Stdlib only, filesystem in and out,
no listener; the one non-file input is one `journalctl` call.

```
python3 ops/selfstamp.py run     --config ~/selfstamp/config.json   # heartbeat, idempotent
python3 ops/selfstamp.py upgrade --config ~/selfstamp/config.json   # the upgrade pass alone
python3 ops/selfstamp.py verify  --manifests ~/selfstamp/manifests  # offline, no config needed
```

`run` is idempotent per period: a manifest that exists is left alone (a
second run the same day is a no-op), a manifest without a proof is
resubmitted rather than rewritten, a pending proof is asked about once, a
complete proof is never touched again. The trigger is the clock and only
the clock: an anchor confirming — which appends a receipt line, including
the anchor that carried this very diary — never causes a manifest; the
next day's manifest records the new receipts hash. Missed days are
not backfilled; a gap in the dates is honest downtime evidence and the
chain still links across it.

Config (`ops/selfstamp.config.example.json`; `~` is expanded):

```json
{
  "state_dir": "~/selfstamp",
  "calendar_url": "http://127.0.0.1:14788",
  "host": null,
  "books": {
    "receipts": "~/gateway/receipts/anchor-receipts.jsonl",
    "payer_log": "~/auto-anchor/logs/payer.log",
    "payer_state": "~/auto-anchor/pay-anchor-bills.state",
    "compose": "~/gateway/docker-compose.yml",
    "env": "~/gateway/.env"
  },
  "journal": true,
  "fork_head": "~/opentimestamps-server"
}
```

State: `<state_dir>/manifests/<period>.json` with the proof beside it as
`<period>.json.ots`, and the unit's log in `<state_dir>/selfstamp.log`.

The manifest is JSON with sorted keys, two-space indent and a trailing
newline — exactly `json.dumps(m, sort_keys=True, indent=2) + "\n"` — so
anyone can re-derive the stamped bytes. Fields (`"schema": "selfstamp/1"`):

- `host` — the box's hostname (or the configured name).
- `period` — the UTC day covered, `YYYY-MM-DD`; also the file name.
- `created_at` — when the manifest was written, UTC.
- `seq` — 1 for the first manifest, then +1 per manifest, no gaps.
- `prev` — `null` for the first manifest; otherwise `{"file", "sha256"}`:
  the previous manifest's file name and the sha256 of its bytes. This is
  the chain link, and it is the same digest that manifest's proof stamps.
- `books` — one entry per configured book: `{"path", "sha256", "bytes"}`
  over the exact file bytes, or `{"path", "missing": true}` if the file
  is absent (recorded, not fatal).
- `journal` — `null` when off; otherwise `{"since", "until", "command",
  "sha256", "bytes"}` over `journalctl --since '<period> 00:00:00 UTC'
  --until '<next day> 00:00:00 UTC' -o export -q`. Reproducible by anyone
  holding that day's journal — which needs the journal to be persistent
  and still to retain the day (`SystemMaxUse` caps it), or an export.
- `fork_head` — `null` when off; otherwise `{"path", "ref", "commit"}`
  read from `.git/HEAD` by file.

The books are hashes of files that themselves hold hashes and operational
lines only; the manifest never contains customer data, and it never
leaves the box unless the operator hands it over.

**Verifying as a stranger, with public tools only.** Given the manifests
directory (the operator's copy), the chain is checked with `sha256sum`
and `jq`: for each manifest newest to oldest, `prev.sha256` equals the
sha256 of the file it names, `seq` steps down by one, `period` steps
back, and the first manifest has `prev: null` — `ops/selfstamp.py verify
--manifests DIR` does the same offline and exits 1 on any break. Each
proof is checked with the OpenTimestamps client: `ots verify -f
<period>.json <period>.json.ots` against a Bitcoin node, or `ots
--no-bitcoin verify …` to be told which block and merkle root to check
on any explorer; `ots info <period>.json.ots` shows the path. Altering
any byte of a past manifest breaks both its successor's `prev.sha256` and
its own proof; deleting a day in the middle leaves the successor naming a
file that is not there and a gap in `seq`.

**Limits, stated plainly.**

- v1 does not cover the gateway's `obligations.db` (the anchor-bills and
  obligations table) or the gateway's own bill ledger: they live in the
  root-owned gateway data volume, and polling `/anchor-bills` mints
  invoices as a side effect. The payer's log and state file stand in for
  the payment side; the receipts file is the calendar's side.
- Deleting the newest day or days leaves a chain that still verifies.
  The chain proves what was written and that the middle is intact; it
  cannot prove its own tail. The tail is exposed by the cadence (a
  manifest is due every day; `seq` should reach today) and by off-box
  copies, not by the chain.
- A proof stays `pending` until the next anchor confirms, typically
  within a day; a stranger handed only pending proofs would need to reach
  the calendar's URI (an onion) to upgrade them, so the operator upgrades
  before handing over.

**Measured on the first deployment.** As of 2026-09-08 the box holds six
manifests, seq 1 to 6 (2026-09-02 to 2026-09-07), every one submitted
through the operator lane by the daily timer with no hands; `verify`
reports `chain=ok` across the set, five proofs `bitcoin` at heights 965446,
965446, 965550, 965866 and 965866 (each upgraded through the box's own lane
by the timer's upgrade pass), and the newest `pending` until the next
anchor. The journal entries hold the known-zero sentinel in the
record-count sidecar. The unit tests drive the tool against a stdlib fake
of the calendar protocol and cross-check the proof bytes with the
opentimestamps library.

## The appliance shape

One box, five parts, two repos. bitcoind runs on the host; the calendar
runs in Docker (the py-leveldb dependency pins it to Python 3.11, so the
image carries its own interpreter); the client adapter, the self-stamper
and the watcher are stdlib Python and run on whatever Python the host has.
Nothing listens off-box: the calendar is published on loopback 14788, the
adapter's door is wherever `LISTEN_ADDR` says, and the three tools have no
listener at all. What the box does not have: a gateway, Tor, phoenixd, a
payer, an L402 secret, a bills token.

| Part | Where it runs | Reads | Writes | Listens |
|---|---|---|---|---|
| bitcoind (pruned mainnet, Bitcoin Core) | host, system unit, user `bitcoin` | the network | its datadir, the anchor wallet | 8333 (p2p), RPC on loopback and the docker0 address |
| otsd (this checkout) | Docker, `docker-compose.enterprise.yml` | bitcoind RPC | `/calendar` (volume), `receipts/anchor-receipts.jsonl` | 127.0.0.1:14788 |
| api-endpoint (`CALENDAR_URL` mode) | host, user unit | the calendar on loopback | `DATA_DIR`: `debts/`, `proofs/`, `pending/`, `heartbeat`, `log` | `LISTEN_ADDR` (the one route, `POST /record`) |
| `ops/selfstamp.py` | host, user timer, 00:30 UTC | the books, `journalctl`, the calendar's operator lane | `~/selfstamp/manifests/` | none |
| `ops/watch.py` | host, user timer, every 5 min | the calendar's JSON status, units, containers, files, the journal | `~/watcher/status`, `state.json`, `watch.log`, ntfy (optional) | none |

The steps below assume an operator account (here `appliance`) with its
home at `/home/appliance`, the two clones at `~/opentimestamps-server` and
`~/api-endpoint`, and adapter state under `~/appliance`. Every path is a
choice; the units and examples name these.

### 0. Host prerequisites

- A Linux host with systemd, Docker Engine 24+ with Compose v2, `git` and
  `python3` (any current version: the tools are stdlib).
- The operator account runs user units without a login session:
  `sudo loginctl enable-linger appliance`.
- A persistent journal, so the self-stamper's daily journal digest covers
  a real day: `/etc/systemd/journald.conf.d/50-persistent.conf` with
  `[Journal]`, `Storage=persistent`, `SystemMaxUse=1G`, `Compress=yes`, then
  `sudo systemctl restart systemd-journald && sudo journalctl --flush`.
- Two host matters this tree does not carry and an appliance cannot go
  without; each is its own session, named here so nothing is assumed:
  **the host firewall** (inbound default-deny except ssh from the LAN and
  8333; outbound default-deny with bitcoind, apt and the optional ntfy
  channel allowed by owner), and **backups** (the calendar directory with
  the journal above the LevelDB, `receipts/`, the adapter's `DATA_DIR`,
  `~/selfstamp/manifests`, the compose `.env`; encrypted, off-box).

### 1. bitcoind on the host

Install Bitcoin Core (the pinned reference runs 31.x), create the user and
directories, and write `/etc/bitcoin/bitcoin.conf`:

```
server=1
prune=50000          # 10000 is enough: otsd needs only recent blocks
dbcache=2000         # sizing for sync; the default (450) is fine afterwards
rpcbind=127.0.0.1
rpcbind=172.17.0.1   # docker0: what host.docker.internal resolves to in the container
rpcallowip=127.0.0.1
rpcallowip=172.30.0.0/24     # the compose subnet, pinned in docker-compose.enterprise.yml
rpcauth=otsd:<salt>$<hmac>   # generated below; the password goes into the compose .env only
rpcwhitelistdefault=0        # keeps the cookie (root) unrestricted; without it the whitelist empties every other user
rpcwhitelist=otsd:estimatesmartfee,getaddressinfo,getbalance,getbestblockhash,getblock,getblockcount,getblockhash,getblockheader,getnewaddress,getrawtransaction,gettransaction,gettxout,listtransactions,listunspent,sendrawtransaction,signrawtransactionwithwallet
natpmp=0
```

The rpcauth line and its password:

```bash
python3 -c 'import os, hmac, hashlib
s = os.urandom(16).hex(); p = os.urandom(32).hex()
print("rpcauth=otsd:%s$%s" % (s, hmac.new(s.encode(), p.encode(), hashlib.sha256).hexdigest()))
print("password", p)'
```

The unit, `/etc/systemd/system/bitcoind.service` (the reference box's,
hardened; `Type=notify` needs Bitcoin Core's `-startupnotify` as below):

```
[Unit]
Description=Bitcoin Core daemon (pruned mainnet)
After=network-online.target docker.service
Wants=network-online.target

[Service]
User=bitcoin
Group=bitcoin
ExecStart=/usr/local/bin/bitcoind -conf=/etc/bitcoin/bitcoin.conf -datadir=/var/lib/bitcoind -pid=/run/bitcoind/bitcoind.pid -startupnotify='systemd-notify --ready' -shutdownnotify='systemd-notify --stopping'
Type=notify
NotifyAccess=all
PIDFile=/run/bitcoind/bitcoind.pid
Restart=on-failure
RestartSec=30
TimeoutStartSec=infinity
TimeoutStopSec=600
RuntimeDirectory=bitcoind
RuntimeDirectoryMode=0710
StateDirectory=bitcoind
StateDirectoryMode=0710
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
PrivateDevices=yes
ProtectHome=yes
MemoryDenyWriteExecute=yes

[Install]
WantedBy=multi-user.target
```

Let it sync fully before step 2 (a wallet on a syncing node reads a zero
balance; shipping the box pre-synced, or loading an assumeutxo snapshot,
turns days into hours and is a product decision, not a step here). Then the
anchor wallet, named `otsd-island` by convention, loaded on every start:

```bash
sudo -u bitcoin bitcoin-cli -conf=/etc/bitcoin/bitcoin.conf -named createwallet wallet_name=otsd-island load_on_startup=true
sudo -u bitcoin bitcoin-cli -conf=/etc/bitcoin/bitcoin.conf -rpcwallet=otsd-island getnewaddress "" bech32
```

Fund that address on-chain: it is the anchoring float, the consumable. A
top-up is an ordinary on-chain payment to an address of this wallet from
anywhere; nothing on the box needs to be reachable for it.

### 2. The calendar (this checkout)

```bash
git clone -b calendar-ops https://github.com/ab21tor/opentimestamps-server ~/opentimestamps-server
cd ~/opentimestamps-server
cp .env.enterprise.example .env && chmod 600 .env
# .env: BITCOIN_RPC_SERVICE_URL=http://otsd:<the password from step 1>@host.docker.internal:8332/wallet/otsd-island
#       ANCHOR_INTERVAL_SECONDS (optional: the coverage tier, default 21600)
```

First run only, the calendar's identity — three files otsd refuses to
start without. The `uri` is written into every pending attestation the
calendar issues and is permanent; on the appliance it is the loopback
address the adapter and the tools use, `http://127.0.0.1:14788/` (it need
not resolve off-box: upgrades go through the adapter, or through
`ots upgrade -c http://127.0.0.1:14788` on the box). The `donation_addr`
is a fresh address of the anchor wallet, so the "donation" address on the
status page is the refill address:

```bash
docker compose -f docker-compose.enterprise.yml run --rm otsd sh -c \
  'echo "http://127.0.0.1:14788/" > /calendar/uri \
   && head -c 32 /dev/urandom > /calendar/hmac-key \
   && echo "<a bech32 address from step 1>" > /calendar/donation_addr'
docker compose -f docker-compose.enterprise.yml up -d --build   # first run; plain `up -d` after a pull
docker compose -f docker-compose.enterprise.yml logs otsd       # expect: journal opened, no RPC errors
curl -s -H 'Accept: application/json' http://127.0.0.1:14788/ | python3 -m json.tool
```

The JSON status must show `best_block` (the calendar can see Bitcoin),
`anchor_receipts: "on"`, and the wallet `balance`. `receipts/` is created
on the first confirmed anchor; it is gitignored, host-readable, and the
box's logbook: one line per anchor, `records` per line.

### 3. The client adapter

```bash
git clone https://github.com/ab21tor/api-endpoint ~/api-endpoint
mkdir -p ~/appliance ~/.config/systemd/user
cp ~/api-endpoint/deploy/api-endpoint.env.example ~/appliance/api-endpoint.env && chmod 600 ~/appliance/api-endpoint.env
# edit: LISTEN_ADDR (loopback, or the LAN address the lab's systems reach), DATA_DIR (absolute)
cp ~/api-endpoint/deploy/api-endpoint.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now api-endpoint.service
printf 'hello' | curl -s --data-binary @- http://127.0.0.1:8402/record      # received <sha256 of "hello">
ls ~/appliance/endpoint-data/proofs                                          # <fp>.ots within seconds, pending
```

Every record goes to the calendar's counted `/digest`, so it appears in
the receipts' `records`; the operator lane below is never used by the
adapter. Knobs, the two shapes and the claims: the api-endpoint README,
"The appliance shape".

### 4. The self-stamper

```bash
mkdir -p ~/selfstamp && cp ops/selfstamp.config.example.json ~/selfstamp/config.json && chmod 600 ~/selfstamp/config.json
```

Books for the appliance (edit `config.json`): `receipts`
`~/opentimestamps-server/receipts/anchor-receipts.jsonl`, `compose`
`~/opentimestamps-server/docker-compose.enterprise.yml`, `env`
`~/opentimestamps-server/.env`, `endpoint_env` `~/appliance/api-endpoint.env`,
`endpoint_heartbeat` `~/appliance/endpoint-data/heartbeat`; `journal: true`;
`fork_head` `~/opentimestamps-server`; no payer books. Then:

```bash
cp ops/systemd/selfstamp.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now selfstamp.timer
python3 ops/selfstamp.py run --config ~/selfstamp/config.json      # the genesis manifest, by hand, once
python3 ops/selfstamp.py verify --manifests ~/selfstamp/manifests
```

### 5. The watcher

```bash
mkdir -p ~/watcher && cp ops/watch.config.example ~/watcher/config && chmod 600 ~/watcher/config
# edit: NTFY_URL (or leave empty), the two absolute paths, CONTAINERS if the checkout is not named opentimestamps-server
cp ops/systemd/watcher.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now watcher.timer
WATCH_DIR=~/watcher python3 -B ops/watch.py --dry                   # every check, nothing sent
```

The appliance runs fifteen checks (the six whose knobs are empty neither
fail nor count): the calendar's status — reachable, Bitcoin-visible,
receipts on, wallet above `CAL_MIN_SATS` — the containers and units, disk,
temperature, memory, the adapter's heartbeat and breaker, journal errors,
ssh failures and unexpected logins, the age of the last confirmed anchor,
pending reboots, refused outbound packets, bitcoind's peers. One line a
day is the heartbeat; alerts go out on transitions only.

### What the box then is

Its health is three plain readings, no listener added for them: the
calendar's JSON on loopback, the adapter's `heartbeat` file, the receipts
file. When the anchor wallet runs dry the calendar keeps accepting and
anchoring waits (one warning in its log; the watcher's `calendar` check
alarms first, at five fee caps); the adapter's intake never stops and
debts are never dropped. Refill is an on-chain payment to the wallet.

An anchored proof verifies with the public client against the box's own
node — `ots verify` — or anywhere else with a Bitcoin view; nothing in it
names a service that must stay alive. A pending proof names the box's own
loopback calendar, which is where the adapter upgrades it.

Dependencies on the box, in full: the otsd image (`python:3.11-slim` by
digest; `opentimestamps`, `leveldb`, `pystache`, `qrcode`, `image`,
`simplejson`, `python-bitcoinlib 0.11.2`), Bitcoin Core, Docker, and the
host's Python for three stdlib tools. Nothing else.

## Unit tests

Test modules live under `otsserver/tests/`:

- `test_calendar.py` — inherited from upstream.
- `test_otsd_launcher.py`, `test_rpc_homepage.py`, `test_stamper_loop.py`,
  `test_anchor_receipts.py`, `test_receipt_marker.py`, `test_aggregator_failure.py`, `test_stamper_cadence.py`,
  `test_rpc_digest.py`, `test_anchor_records.py`,
  `test_aggregator_dedupe.py`, `test_stamper_read_errors.py`,
  `test_stamper_checkpoint.py`, `test_stamper_wallet_empty.py`,
  `test_operator_lane.py`, `test_selfstamp.py`,
  `test_stamper_dead_cycle.py`, `test_stamper_fee_cap.py`,
  `test_watch.py` — regression
  tests for this branch's delta (launcher flags, homepage RPC wiring and
  the receipts status line, stamper-loop crash fixes, anchor receipts,
  anchor cadence, the /digest Content-Length handling, anchor-receipt
  record counts and their close-time re-read, aggregator dedupe,
  pending-fill read-error survival, the restart checkpoint, empty-wallet
  warn-once, the operator lane and known-zero counts, the self-stamp
  tool, dead-cycle recovery, fee-cap warn-once, the receipts-off startup
  warning, the watcher's 29 fixture scenarios and its appliance-shape
  additions). They stub everything external with `unittest.mock` or
  stdlib fakes: no bitcoind, no network.

No test module needs a running Bitcoin node. Every module does need the
full dependency set installed, and one dependency — `leveldb` — is a native
build: `otsserver/calendar.py` imports it at module level, so without it every
module fails at collection with `ModuleNotFoundError`.

Run the suite in the deployment-matched environment — the otsd image (base
`python:3.11-slim` plus `build-essential libleveldb-dev` and
`pip install -r requirements.txt`) — or in a Python ≤ 3.11 venv:

```
python -m unittest discover -v                       # Ran 100 tests ... OK
```

The otsd image has no pytest; where pytest is installed,
`python -m pytest otsserver/tests -q` runs the identical set.

Known limit: py-leveldb does not build on Python 3.12+ (`pip install -r
requirements.txt` fails on the `leveldb` wheel, so nothing is importable and
no test can run). Other platforms and Python versions have not been tried;
treat any claim about them as unverified until you run the command above.
