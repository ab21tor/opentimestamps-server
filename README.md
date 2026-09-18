# OpenTimestamps Calendar Server

`otsd` is a calendar server for OpenTimestamps clients. It aggregates
submitted digests into per-second merkle trees, commits each tree's root to
an append-only journal, anchors the pending commitments in Bitcoin
transactions paid from its own wallet, and serves the completed timestamps
back to clients. Running a calendar is not required to use the protocol;
public calendars exist.

This fork (branch `calendar-ops`, on upstream v0.7.1) adds: a one-line JSON
status; anchor receipts with per-anchor record counts; a departure clock
that separates anchor timing from submission timing; a not-before bound in
every commitment; a deep-reorg detector; a restart checkpoint; an operator
lane for digests that are not records; and, under `ops/`, a self-stamp tool
that stamps a daily manifest of the host's own files, a health watcher, a
block-headers export and a stand-alone proof verifier. Upstream's donation
homepage, with the `qrcode`, `pystache`, `image` and `simplejson` packages
it needed, is removed.

## Configurations

The same code runs in each of these; none requires another.

- **Direct.** `otsd` on a host with a Bitcoin Core node, bound to
  localhost; clients reach it with `ots -c` and `ots -l` ("Install:
  direct").
- **With the client adapter on the same host.** The adapter in the
  `api-endpoint` repository, in its `CALENDAR_URL` mode, submits digests to
  the calendar on loopback and upgrades its proofs there. "Install: single
  host" is that configuration, with the optional tools.
- **Behind the gateway.** The `timestamp-gateway` repository serves clients
  through its own door and reads this calendar's receipts file for its
  anchor-billing feature; its README and operator guide are the install for
  that configuration. The adapter's `GATEWAY_URL` mode and the
  `auto-anchor` repository belong to it.
- **The tools** under `ops/`, each off unless a timer runs it: the
  self-stamp (needs `OTSD_OPERATOR_LANE=1` on the calendar), the watcher,
  the headers export. Each runs in any of the configurations above.
- **A witness.** A second host running the self-stamp with an `inbox` keeps
  copies of this host's manifests and lists them in its own chain ("Witness
  by file drop"). Optional in every configuration; two hosts can witness
  each other.

Anything that can reach the calendar's port can read the status line (the
anchor wallet's balance, the size of the pending queue, the in-flight
anchor's txid) and, with the lane on, submit uncounted digests. `otsd`
binds localhost by default and the compose files publish it on `127.0.0.1`
only; it is not designed to be exposed directly. A door in front of it (the
gateway, or an authenticating reverse proxy) decides what else is reachable.

## What the calendar guarantees

Each rule below is made by the code described in the section it names.

- The journal is append-only. Every digest the aggregator accepts becomes
  a leaf of a per-second merkle tree whose root is committed to it, and
  every journal entry the calendar does not yet hold is pending: the
  stamper re-reads the journal at every start and anchors what the
  calendar lacks ("Restart checkpoint", "Recover").
- Anchor departures happen only at the moments of the departure clock, so
  a broadcast time never reveals when a commitment arrived ("Anchor
  cadence").
- Every commitment carries the hash of the newest block the stamper had
  seen; while no block is known it carries none rather than an invented
  one ("Not-before bound").
- Exactly one receipt is written per confirmed anchor, after the calendar
  save. Every byte of the receipt is checked onto disk, file and
  directory fsynced, before its marker goes; a receipt not yet on file
  has a marker of its own, named by its anchor, that no later anchor
  touches, and is recovered from it at the next anchor or the next
  start; no crash writes two receipts for the same records. Record
  counts err low, with one
  bounded exception ("Anchor receipts").
- A digest submitted through the operator lane is aggregated and anchored
  like any other and is never counted as a record ("Operator lane").
- A receipted anchor found off the chain is reported and is never
  re-anchored automatically ("Deep-reorg detector").
- The receipts, the counts, the checkpoint, the marker, the bound and the
  detector never stop anchoring or intake: each failure is logged and the
  loop continues. A confirmed tree whose calendar save fails is kept and
  retried every pass until it lands. Two failures do stop the service,
  loudly, with a nonzero exit so the supervisor restarts it: an aggregator
  round that does not commit, and a stamper that cannot start (a
  checkpoint it cannot read, or a receipt on file for an anchor the
  database does not hold: "Anchor receipts"). Before either can happen
  the calendar itself refuses to start on storage that disagrees with
  itself ("Restart checkpoint"). A dead worker never sits behind a live
  port.
- The tools under `ops/` open no listener. Their inputs beyond the
  filesystem are the calendar on loopback, one `journalctl` call
  (self-stamp), bitcoind RPC (headers export) and, for the watcher, the
  host's own commands; the watcher's one output off-host is an optional
  ntfy post.
- A manifest holds hashes, sizes, times, counts and an opaque chain
  label, never the contents of a file it hashes and, since `selfstamp/3`,
  no host name, path or file name; it leaves the host only through the
  configured outbox or by hand ("The self-stamp").

## Requirements

- Bitcoin Core with a wallet used by nothing else while `otsd` runs (the
  stamper assumes exclusive use of it); a pruned node is enough. The RPC
  methods the calendar and the headers export call: `estimatesmartfee`,
  `getaddressinfo`, `getbalance`, `getbestblockhash`, `getblock`,
  `getblockcount`, `getblockhash`, `getblockheader`, `getnewaddress`,
  `getrawtransaction`, `gettransaction`, `gettxout`, `listunspent`,
  `sendrawtransaction`, `signrawtransactionwithwallet`. The wallet must be
  loaded on every start of the node; a wallet on a node still syncing
  reads a zero balance.
- Python with the packages in `requirements.txt`: `opentimestamps`,
  `plyvel` (the LevelDB binding, a native build against the system LevelDB
  library, `libleveldb-dev` on Debian or `leveldb` from Homebrew on macOS)
  and `python-bitcoinlib` (the image pins 0.11.2). Or the
  Docker image built from the `Dockerfile`: `python:3.13-slim` by digest
  plus `build-essential libleveldb-dev` and the same packages.
- For the tools: `python3` (standard library only); systemd user units,
  with linger enabled so they run without a login session; a persistent
  journal for the self-stamp's daily journal digest.
- For the single-host install: a Linux host with systemd, Docker Engine
  24+ with Compose v2, `git`.
- Two host matters this tree does not carry: a host firewall (the
  single-host install assumes inbound default-deny except ssh from the LAN
  and 8333, outbound default-deny with bitcoind, apt and the optional ntfy
  channel allowed by owner) and backups ("Recover").

## Install: direct

```
pip3 install -r requirements.txt
```

Create the calendar directory with its two identity files:

```
mkdir -p ~/.otsd/calendar/
echo "http://127.0.0.1:14788" > ~/.otsd/calendar/uri
dd if=/dev/random of=~/.otsd/calendar/hmac-key bs=32 count=1
```

The URI is written into the URI field of every pending attestation this
calendar returns, so clients upgrade against it; it is permanent. For a
server used for testing the loopback address is enough; a served calendar
needs a URL its clients can reach indefinitely. The HMAC key is secret; it
exists for calendar recovery from untrusted sources, of which only part is
implemented (see the source).

`otsd` runs in the foreground; there is no daemonization. `--btc-testnet`
and `--btc-regtest` select those chains. The OpenTimestamps protocol does
not distinguish mainnet, testnet and regtest, so a client verifying a
testnet or regtest proof must be told the chain (`ots --btc-regtest ...`).

Clients:

```
ots stamp -c http://127.0.0.1:14788 -m 1 FILE
ots -l http://127.0.0.1:14788 upgrade FILE.ots
ots --btc-regtest -l http://127.0.0.1:14788 upgrade FILE.ots
```

Clients connect automatically only to calendars on their whitelist;
`-l` names this one for upgrade and verify. With regtest, blocks are
mined on demand: `bitcoin-cli -generate 10`.

## Install: single host

One host runs bitcoind, the calendar in Docker (its LevelDB binding is a
native build, so the image carries its own interpreter), the client
adapter, and the three tools, which are standard-library Python on the
host's own interpreter. Nothing listens off-host: the calendar is published
on loopback 14788, the adapter's door is wherever `LISTEN_ADDR` says, and
the tools have no listener. This configuration has no gateway and none of
the gateway's parts (Tor, phoenixd, an L402 secret, a bills token), and no
payer.

| Part | Where it runs | Reads | Writes | Listens |
|---|---|---|---|---|
| bitcoind (pruned mainnet, Bitcoin Core) | host, system unit, user `bitcoin` | the network | its datadir, the anchor wallet | 8333 (p2p), RPC on loopback and the docker0 address |
| otsd (this checkout) | Docker, `docker-compose.enterprise.yml` | bitcoind RPC | `/calendar` (volume), `receipts/anchor-receipts.jsonl` | 127.0.0.1:14788 |
| api-endpoint (`CALENDAR_URL` mode) | host, user unit | the calendar on loopback | `DATA_DIR`: `debts/`, `proofs/`, `pending/`, `heartbeat`, `log` | `LISTEN_ADDR` (the one route, `POST /record`) |
| `ops/selfstamp.py` | host, user timer, 00:30 UTC | the books, `journalctl`, the calendar's operator lane | `~/selfstamp/manifests/` | none |
| `ops/watch.py` | host, user timer, every 5 min | the calendar's JSON status, units, containers, files, the journal | `~/watcher/status`, `state.json`, `watch.log`, ntfy (optional) | none |
| `ops/export_headers.py` | host, user timer, 01:00 UTC | bitcoind RPC on loopback (the calendar's credentials) | `~/claim-kit/headers.bin`, `export.log` | none |

The steps assume an operator account (here `appliance`) with its home at
`/home/appliance`, the two clones at `~/opentimestamps-server` and
`~/api-endpoint`, and adapter state under `~/appliance`. Every path is a
choice; the units and examples name these.

### 0. Host prerequisites

- The operator account runs user units without a login session:
  `sudo loginctl enable-linger appliance`.
- A persistent journal, so the self-stamp's daily journal digest covers a
  whole day: `/etc/systemd/journald.conf.d/50-persistent.conf` with
  `[Journal]`, `Storage=persistent`, `SystemMaxUse=1G`, `Compress=yes`, then
  `sudo systemctl restart systemd-journald && sudo journalctl --flush`.

### 1. bitcoind on the host

Install Bitcoin Core, create the user and directories, and write
`/etc/bitcoin/bitcoin.conf`:

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
rpcwhitelist=otsd:estimatesmartfee,getaddressinfo,getbalance,getbestblockhash,getblock,getblockcount,getblockhash,getblockheader,getnewaddress,getrawtransaction,gettransaction,gettxout,listunspent,sendrawtransaction,signrawtransactionwithwallet
natpmp=0
```

The rpcauth line and its password:

```bash
python3 -c 'import os, hmac, hashlib
s = os.urandom(16).hex(); p = os.urandom(32).hex()
print("rpcauth=otsd:%s$%s" % (s, hmac.new(s.encode(), p.encode(), hashlib.sha256).hexdigest()))
print("password", p)'
```

The unit, `/etc/systemd/system/bitcoind.service` (`Type=notify` needs
Bitcoin Core's `-startupnotify` as below):

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

Let it sync fully before step 2: a wallet on a syncing node reads a zero
balance. Then the anchor wallet, named `otsd-island` by convention, loaded
on every start:

```bash
sudo -u bitcoin bitcoin-cli -conf=/etc/bitcoin/bitcoin.conf -named createwallet wallet_name=otsd-island load_on_startup=true
sudo -u bitcoin bitcoin-cli -conf=/etc/bitcoin/bitcoin.conf -rpcwallet=otsd-island getnewaddress "" bech32
```

Fund that address on-chain. The wallet pays the anchor transactions; a
top-up is an ordinary on-chain payment to any address of this wallet from
anywhere, and nothing on the host needs to be reachable for it.

### 2. The calendar (this checkout)

```bash
git clone -b calendar-ops https://github.com/ab21tor/opentimestamps-server ~/opentimestamps-server
cd ~/opentimestamps-server
cp .env.enterprise.example .env && chmod 600 .env
# .env: BITCOIN_RPC_SERVICE_URL=http://otsd:<the password from step 1>@host.docker.internal:8332/wallet/otsd-island
#       ANCHOR_INTERVAL_SECONDS (optional: the anchor interval, default 21600)
```

First run only, the calendar's identity, the two files `otsd` refuses to
start without. The `uri` is written into every pending attestation the
calendar issues and is permanent; here it is the loopback address the
adapter and the tools use, `http://127.0.0.1:14788/`. It need not resolve
off-host: upgrades go through the adapter, or through
`ots upgrade -c http://127.0.0.1:14788` on the host.

```bash
docker compose -f docker-compose.enterprise.yml run --rm otsd sh -c \
  'echo "http://127.0.0.1:14788/" > /calendar/uri \
   && head -c 32 /dev/urandom > /calendar/hmac-key'
docker compose -f docker-compose.enterprise.yml up -d --build   # first run; plain `up -d` after a pull
docker compose -f docker-compose.enterprise.yml logs otsd       # expect: journal opened, no RPC errors
curl -s http://127.0.0.1:14788/ | python3 -m json.tool
```

The status line must show `best_block` (the calendar can see Bitcoin),
`anchor_receipts: "on"`, the wallet `balance`, and `needs_attention: []`.
`receipts/` is created on the first confirmed anchor; it is gitignored and
host-readable: one line per anchor, `records` per line.

### 3. The client adapter

```bash
git clone https://github.com/ab21tor/api-endpoint ~/api-endpoint
mkdir -p ~/appliance ~/.config/systemd/user
cp ~/api-endpoint/deploy/api-endpoint.env.example ~/appliance/api-endpoint.env && chmod 600 ~/appliance/api-endpoint.env
# edit: LISTEN_ADDR (loopback, or the LAN address the submitting systems reach), DATA_DIR (absolute)
cp ~/api-endpoint/deploy/api-endpoint.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now api-endpoint.service
printf 'hello' | curl -s --data-binary @- http://127.0.0.1:8402/record      # received <sha256 of "hello">
ls ~/appliance/endpoint-data/proofs                                          # <fp>.ots within seconds, pending
```

Every record goes to the calendar's counted `/digest`, so it appears in
the receipts' `records`; the adapter never uses the operator lane. Its
settings and its two modes: the api-endpoint README.

### 4. The self-stamp

```bash
mkdir -p ~/selfstamp && cp ops/selfstamp.config.example.json ~/selfstamp/config.json && chmod 600 ~/selfstamp/config.json
```

Books for this configuration (edit `config.json`): `receipts`
`~/opentimestamps-server/receipts/anchor-receipts.jsonl`, `compose`
`~/opentimestamps-server/docker-compose.enterprise.yml`, `env`
`~/opentimestamps-server/.env`, `endpoint_env` `~/appliance/api-endpoint.env`,
`endpoint_heartbeat` `~/appliance/endpoint-data/heartbeat`; `journal: true`;
`fork_head` `~/opentimestamps-server`; no payer books. `audit_logs`,
`outbox` and `inbox` may stay empty ("The self-stamp"). Then:

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

### 6. The headers export

```bash
mkdir -p ~/claim-kit && cp ops/systemd/headers.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now headers.timer
python3 ops/export_headers.py --env ~/opentimestamps-server/.env --rpc-host 127.0.0.1 --out ~/claim-kit/headers.bin
```

The first run exports the whole chain; the timer appends each day's
blocks ("The headers export").

## Install: behind the gateway

The `timestamp-gateway` repository's README and operator guide describe
that configuration. The gateway reads the receipts file this calendar
writes ("Anchor receipts").

## Operate

### Settings

- `--btc-min-tx-interval` (default 21600 seconds; `ANCHOR_INTERVAL_SECONDS`
  in the compose `.env`): the base of the departure clock ("Anchor
  cadence").
- `--btc-min-confirmations` (default 6): the depth at which an anchor is
  saved to the calendar and receipted.
- The fee cap (the compose files set it at 20,000 sats): the most one
  anchor cycle may spend.
- `--btc-testnet`, `--btc-regtest`: the chain.
- `OTSD_ANCHOR_RECEIPTS`: a file path; set, the stamper writes receipts
  ("Anchor receipts"). Unset, off.
- `OTSD_OPERATOR_LANE=1`: serves `POST /operator/digest` ("Operator lane").
  Unset, the path is a 404.
- `BITCOIN_RPC_SERVICE_URL`: the node's RPC URL with credentials, from the
  environment; in the compose files from the gitignored `.env`.

### The status line

`GET /` answers one JSON line, whatever `Accept` says. The fields are an
interface: the watcher, the self-stamp's float reader and the gateway's
`/health` read them.

- `best_block`, `block_height`: the node's tip as the calendar sees it;
  `null` when the Bitcoin RPC path is down (logged). This is the one
  external sign that the calendar can see Bitcoin.
- `balance`: the anchor wallet's confirmed sats, an integer; `null` with
  the RPC path down.
- `anchor_receipts`: `"on"` or `"off"` ("Anchor receipts").
- `needs_attention`: the deep-reorg detector's findings, a list of
  strings, empty when every checked anchor is where its receipt says
  ("Deep-reorg detector").
- `pending_commitments`, `txs_waiting_for_confirmation`, `most_recent_tx`,
  `prior_versions`, `tip`, `version`: the queue and the anchor in flight.

The line is built in full before the response is committed, so a failure
is a status that says so, never an empty 200.

### Anchor receipts

With `OTSD_ANCHOR_RECEIPTS` set to a file path, the stamper appends one
JSON line per anchor transaction, recording what that anchor cost. The
gateway's anchor-billing feature reads and dedupes the file; nothing in
this server reads it back. A path inside the calendar directory works
(`~/.otsd/calendar/anchor-receipts.jsonl`), as does any writable path.

Exactly one line is appended per anchor, at the moment the stamper's
confirmation logic saves the anchor to the calendar (`--btc-min-confirmations`);
no new confirmation policy is introduced. Broadcasts and RBF fee bumps
write nothing, and a replaced txid never appears in the file. The file is
append-only JSONL: each line is appended atomically, and existing bytes
are never rewritten or truncated.

Each line is a JSON object with exactly these fields. The format is an
interface parsed by other software; treat it as pinned:

- `txid`: the anchor transaction id, 64 hex characters, RPC display
  order.
- `fee_sats`: the final fee of the confirmed transaction, in satoshis.
  The stamper constructs the transaction, so this is input value minus
  output value of the version that confirmed, after any RBF bumps, never
  an estimate.
- `commitments`: the number of commitments this anchor carried.
- `confirmed_height`: the height of the block containing the anchor
  transaction.
- `confirmed_at`: unix time at which the stamper deemed the transaction
  confirmed and wrote the line; the stamper's wall clock, not a block
  timestamp.
- `records`: the number of digest submissions whose commitments sit
  inside this transaction's anchor tree. A record is one submission
  accepted by the aggregator through `/digest`, one leaf of a per-second
  merkle tree. A digest re-submitted within the aggregator's dedupe
  horizon (in-memory, one hour, capped at 65536 entries, refreshed on
  every hit) attaches to the existing pending commitment and is counted
  once. The count is fixed when the anchor tree closes over the pending
  commitments; the receipt carries the count of the tree that confirmed.
  The counter errs low, with one bounded exception: a digest resubmitted
  across a restart, or past the horizon, is counted again, because the
  fresh aggregator cannot know it was counted before. A commitment whose
  count is unknown (recorded before the feature was enabled, or lost to a
  crash) sums as 0, with one warning per affected tree. A count the
  stamper's scan read before the aggregator had written it is read again
  when the tree closes, so a busy second is not lost to that race; only a
  count still absent then is a hole. `0` therefore means "unknown", not
  "empty": a real tree has at least one leaf.

The per-tree counts behind `records` live in a sidecar beside the
journal, `journal.counts`: one 4-byte big-endian integer per journal entry
index, written only after the journal entry itself is durable, so a crash
can lose counts but never invent them. The sidecar exists only when
`OTSD_ANCHOR_RECEIPTS` is set and grows by 4 bytes per journal entry, one
eleventh of the journal's own growth, append-only like the journal.
Nothing is migrated when the feature is enabled on an existing calendar:
older entries have no counts. The sidecar records how many submissions
each second's tree carried, no digests.

Two rules the code keeps:

- A receipt write failure never breaks anchoring. The stamper logs one
  warning and continues; the calendar save always happens, and the
  receipt stays in the pending marker until it can be written.
- A confirmed tree leaves memory only once its calendar save has
  returned (LevelDB's synchronous write). A save that fails is logged
  once, the tree is kept, and every pass, new block or not, retries every
  mature unsaved tree until it lands; nothing accepted is dropped.
- No crash writes two receipts for the same records, and no receipt is
  lost: one still owed has its marker standing. The receipt is written
  after the calendar save, guarded by a marker named by the anchor:
  before the save the stamper writes `<receipts file>.pending.<txid>`,
  the receipt line plus the anchor's own key (its txid, as the saved tree
  carries it), atomically (fsynced, with its directory); after the save it
  appends the receipt,
  every byte checked (a short write is completed, never taken for a whole
  line), fsyncs the file and its directory, and only then removes that
  marker and no other. Markers are settled one at a time: one that cannot
  be settled (the receipts file unwritable) is logged and left standing,
  and never holds up another anchor's save or receipt; a receipt
  recovered later may follow later anchors' lines in the file. Before
  2026-09-16 there was one marker name, and a later anchor's success
  removed an earlier anchor's still-owed marker (2026-09-15/16 review
  F08); the old name, `<receipts file>.pending`, is still read and
  settled at the first start after the upgrade. An incomplete last line left by an
  interrupted append is dropped before the next append and its receipt
  recovered from the marker, so no completed receipt is ever duplicated
  and no outstanding marker discarded. What the tests inject is the
  failure at the write boundary (short writes, errors, a stop between the
  steps); a power cut is not simulated, and the fsyncs are what a power
  cut relies on. A marker still present at the next start, or when
  the next anchor confirms, is settled by asking the calendar whether it
  holds that anchor: its txid node, which only its own saved tree puts
  there. If it does, the save completed and the receipt is appended
  unless its txid is already on file (`recovered from the pending
  marker`); a line found on file was written, not known to be synced, so
  the file and its directory are fsynced again before the marker goes. If
  it does not, the save never happened; those commitments are still
  pending and will be re-anchored under a new txid with their own
  receipt, so this receipt is discarded (`nothing is owed for <txid>`),
  and no later anchor over the same commitments can ever make this one
  look saved: a discard whose removal fails is reported and asked again
  at the next start and before the next marker, and answered the same
  (until 2026-09-18 the question was one of the tree's commitments, which
  the next anchor's save answered for, and the receipt was written: five
  records became seven). An unreadable marker is set aside beside the
  receipts file as `.corrupt-<time>` and warned about, by the error's
  class alone. Every message names a marker by its role and its anchor's
  txid (only when its name's suffix is a txid), never by its file name:
  the receipts file is named by the operator, and its name can say whose
  calendar this is. One state is not settled: the txid on file, its
  marker standing, and the anchor absent from the calendar. No stop
  leaves that (the receipt follows the save's
  synchronous write); it means the receipts file is newer than `db/`,
  copies from different moments, and going on would anchor those records
  again and receipt them a second time. The stamper's start fails on it
  (`CALENDAR STORAGE INCONSISTENT`, the service stops, the marker stays)
  and says what to restore ("Recover").

If `OTSD_ANCHOR_RECEIPTS` is unset while the sidecar already exists, the
stamper warns at startup that receipts were on before and are off now,
and the status line's `anchor_receipts` field says `"off"`. The watcher
reads it; the gateway's `/health` reports `billing: receipts_off` from the
same field while its own billing is on.

### Anchor cadence

Anchor departures happen only at the moments of a free-running jittered
clock: the next departure is `--btc-min-tx-interval` times a uniform
random factor between 1 and 2 in the future, re-armed when an anchor
confirms, when the clock expires over an empty queue, and at startup. An
idle window rolls the clock forward silently, so the first commitment
after idleness waits for the next scheduled departure like any other.
Anchor timing is therefore independent of submission timing; a broadcast
time never reveals when a commitment arrived. There is no further
configuration.

An in-flight anchor whose input stops being a confirmed unspent output
(a shallow reorg took the parent whose change it spends, or a version
this stamper does not track, one a restart forgot, was mined) can never
be bumped again. The stamper notices the moment a bump cannot be priced,
warns once, drops the dead versions, and starts a fresh cycle from the
wallet's confirmed outputs on the next pass; the pending commitments are
untouched and anchor once, so nothing is lost and nothing is receipted
twice. A fee cap that blocks the next transaction is logged once on entry
and once on recovery, not once per loop second.

When the wallet has no spendable output the stamper logs once and
anchoring waits; intake continues ("Recover").

### Not-before bound

Every commitment the calendar issues carries, beside its time prefix, the
hash of the newest Bitcoin block the stamper had seen when the submission
was aggregated: `Calendar.submit` appends the 32-byte hash (in the byte
order an explorer shows) and applies a sha256, then prepends the time as
before. A block hash cannot be known before its block exists, so a proof
that carries block M's hash and is anchored in block N says that the
commitment formed after block M and before block N, where the anchor
alone said "before". `ots info` shows the bound as an `append <block
hash>` followed by `sha256`, two ops before the `prepend` of the time
prefix; the public `ots` client, the opentimestamps library and the
standard-library parser in `ops/selfstamp.py` (shared with the client
adapter) follow it as they follow any op. The sha256 keeps the journal
entry at its 44 bytes, so the journal format, the stamper's pipeline and
existing calendars are unchanged; entries written before the bound
existed lack it.

The bound is the stamper's view of the chain at aggregation, which lags
the network by the stamper's one-second poll and any RPC latency. A
commitment aggregated while no block is known (startup, or bitcoind
unreachable) goes out without a bound rather than with an invented one;
the calendar warns once and logs when the bound returns.

### Deep-reorg detector

A receipted anchor had `--btc-min-confirmations` blocks on top of it when
its receipt was written, and the calendar saved proofs naming that block.
A reorg deeper than that takes the block away. On its first pass and every
hour after, the stamper asks the wallet (`gettransaction`) about the last
hundred receipted txids. A confirmation count at or below zero means the
anchor left the chain (Bitcoin Core reports a conflicted transaction as a
negative count and one back in the mempool as zero); a `blockheight`
other than the receipted one means it was mined again elsewhere. Either
finding is logged at ERROR on every check while it stands, appears in the
status line's `needs_attention` (the watcher's `calendar` check alarms on
it), and is never acted on: nothing is re-anchored automatically. A
finding stands until the process restarts; a later re-mine does not
repair proofs that name the old block.

The detector reads the receipts file, so it runs only with anchor receipts
on. A txid the wallet does not know (a rebuilt wallet) or an RPC failure
is warned about, not counted as a finding.

### Restart checkpoint

`<calendar>/journal.known-good` holds the journal index the stamper's
restart scan may begin at: everything below it is already in the
calendar. Upstream only read this file; this fork writes it after every
confirmed anchor, as the lowest journal index still outstanding (pending,
or riding a mined tree that has not yet reached `--btc-min-confirmations`),
or the scan cursor itself when nothing is outstanding. It is written
atomically beside the journal; a failed write warns and never interrupts
anchoring. Without it a restart re-reads the entire journal and probes
the calendar once per entry, a cost that grows with all history; with it,
a restart's catch-up is one anchor window. Deleting the file is safe and
restores the full rescan. There is no configuration.

The file is tied to the database it describes. `db/` carries a
generation (16 random bytes, chosen when the database is created) and a
committed watermark: the checkpoint index, written in the same
synchronous LevelDB batch as the confirmed timestamps that make it true,
so the file can never claim more than the database durably holds. The
file reads `INDEX GENERATION`, and at every start the calendar checks the
pair before serving: a checkpoint whose generation is not the database's
(`db/` recreated, or restored from another lineage), or whose index is
above the database's watermark (`db/` restored from an older backup than
the checkpoint), or that cannot be read at all, stops the service with
`CALENDAR STORAGE INCONSISTENT` and the recovery text, exit 1. The
journal is checked against the checkpoint too, within bounds: it must
hold at least that many entries, and the entry just below the
checkpoint and entry 0 must be in the database, or the start is refused
the same way; a missing journal is never created beside a checkpoint
that names entries it should hold. Without this a journal lost or
restored from an older backup beside a kept checkpoint started cleanly
and took every new submission at an index below the checkpoint, where
the scan never looks (2026-09-15/16 review F02). The check is two reads
and two probes, not a proof that the journal is whole: it catches a
missing or truncated journal and one from another lineage that differs
at either probed entry, and it assumes the restore rule below. It does
not detect an older prefix-identical journal that still reaches the
checkpoint (the entries beyond it are lost, and only a sidecar from the
newer moment, below, shows it), nor one that differs only between the two
probed entries; that is why the four files are one stopped copy
("Recover"). Deleting the checkpoint (a rescan from 0) reads every entry
that is here and anchors what the database lacks; it cannot show that
entries were not lost. Nothing in this check reads a file's timestamp. Recovery is to delete the checkpoint
and start again: the stamper rescans from index 0 and re-anchors every
commitment the database lacks ("Recover" for what that costs).

Two more things are checked before the journal is opened. `db/` must
open as one database: files that are not one (a copy taken while the
calendar ran, a restore that stopped part way) or that another process
holds are refused as `db/ does not open (<the error's class>)`, with the
recovery, where LevelDB's own text would name the directory. And
`journal.counts` must not count more entries than the journal holds: a
count is written only after its entry is durable, so a sidecar that
outlasts its journal says that the journal is the older file and has
lost entries that were acknowledged.

A calendar from before this (a file holding the index alone, a database
without a generation) is refused at its first start, whatever the
database holds: an index does not say which database it describes.
`Recovery, once: delete journal.known-good … and start again`: the next
start gives the database its generation with watermark 0 and scans from
index 0 (what the database holds is skipped, what it lacks is anchored);
the deletion and the generation are once, and the scan from 0 repeats at
each start until the next confirmed anchor writes the checkpoint in the
current form. Until 2026-09-17 such a checkpoint was adopted when the
entry below it was in the database, in two writes; a stop between them
left a start refused for a reason that had not happened, and the same
rescan.

A checkpoint that cannot be read for any reason but absence (a restore
that lost its permissions) is refused the same way, by the error's class
and errno, with the recovery, at both places that read it, the
calendar's check and the stamper's open; a malformed one is described by
its shape (`14 bytes, 3 fields`; `the first field is not a decimal
number`), never quoted, the index being checked digit by digit before it
is converted. No message about any of
this names the calendar's directory or the receipts file: files are
named by their fixed names, a marker by its role and its anchor's txid.

### Operator lane

With `OTSD_OPERATOR_LANE=1` in the calendar's environment the server
serves `POST /operator/digest`. It takes what `/digest` takes (a raw
digest, the same Content-Length bounds) and answers what `/digest` answers
(the serialized pending timestamp). The digest becomes a leaf of the same
per-second merkle tree, is committed to the same journal, and rides the
same anchor transaction. The one difference: it is not a record. The
aggregator reports a round's `records` as the number of leaves that came
through `/digest`; operator leaves are excluded, so they never reach a
receipt's `records` field. `commitments` in the receipt still counts the
commitment that carries them. Unset, the path is a 404 and nothing in the
server changes.

A round whose leaves all came through the operator lane holds no record.
That is a known zero, not a hole: the record-count sidecar stores it as
the sentinel `0xFFFFFFFF`, which `RecordCounts.get` returns as `0`. Any
stored value at or above `2^31` reads as `0`: a one-second tree cannot
have that many leaves, and every torn big-endian prefix of the sentinel
(`0xFF000000`, `0xFFFF0000`, `0xFFFFFF00`) stays above the threshold, so
the rule that a torn write can only undercount still holds; values
already in the file are untouched. The stamper sums a known zero silently
instead of warning that the receipt will undercount, and a receipt whose
whole tree was operator leaves carries `records: 0`, which the gateway
leaves unbilled.

Anything that can reach the calendar's port can use the lane; the compose
files publish the port on `127.0.0.1` only ("Configurations").

### The self-stamp

`ops/selfstamp.py` writes, once a day (a user timer,
`ops/systemd/selfstamp.timer`, 00:30 UTC, `Persistent=true`), a manifest
of the host's own files for the previous UTC day, hash-chains it to the
previous manifest, and stamps the sha256 of the manifest file through the
operator lane. The proof rides whatever anchor the calendar sends next;
the tool never forces an anchor. The next run upgrades the proof to a
Bitcoin attestation with one `GET /timestamp/<commitment>`. Standard
library only, filesystem in and out, no listener; the inputs beyond files
are one `journalctl` call and the calendar on loopback. Every step, and
who owns the work between two steps, is in `docs/contracts.md`,
"Workflow 2".

```
python3 ops/selfstamp.py run     --config ~/selfstamp/config.json   # heartbeat, idempotent
python3 ops/selfstamp.py upgrade --config ~/selfstamp/config.json   # the upgrade pass alone
python3 ops/selfstamp.py verify  --manifests ~/selfstamp/manifests  # offline, no config needed
```

`run` is idempotent per period: a manifest that exists is left alone (a
second run the same day writes nothing), a manifest without a proof is
resubmitted rather than rewritten, a pending proof is asked about once
per run, a complete proof is never touched again. The period is the UTC
day before the run's clock; `--period` names another finished day, and
is refused for a day not yet over or one not after the newest manifest.
Missed days are not backfilled: a run after an outage covers the day
before it, and the chain links across the gap. A manifest written late
says when it looked (`created_at`) and never claims an observation it
did not make. `run` and `upgrade` hold an exclusive lock on the state
directory (`<state_dir>/.lock`, across processes) for their whole
duration, so the timer's run and a manual one never interleave their
writes: the second waits up to `--lock-wait` seconds (default 300), then
exits 1 with `locked`. Every file is written under a unique temporary
name, fsynced, renamed, and its directory fsynced, so `verify` and any
other reader need no lock. The trigger is the clock and only the clock:
an anchor confirming, which appends a receipt line (including for the
anchor that carried this manifest), never causes a manifest; the next
day's manifest records the new receipts hash.

Each run ends with one `summary` line: how many manifests and copies are
held, how many proofs are `bitcoin`, `pending`, `missing`, `malformed` or
`mismatch`, and how many steps failed. The exit code is about this run's
own work: 0 when every step is done, 1 when a step failed and is left for
the next run or the operator (the calendar did not answer a submission or
an upgrade, an inbox file could not be read, a proof is not of the file
beside it). A proof still pending is not a failure and has no deadline:
it stays in the summary until its anchor confirms.

Config (`ops/selfstamp.config.example.json`; `~` is expanded):

```json
{
  "state_dir": "~/selfstamp",
  "calendar_url": "http://127.0.0.1:14788",
  "books": {
    "receipts": "~/gateway/receipts/anchor-receipts.jsonl",
    "payer_log": "~/auto-anchor/logs/payer.log",
    "payer_state": "~/auto-anchor/pay-anchor-bills.state",
    "compose": "~/gateway/docker-compose.yml",
    "env": "~/gateway/.env"
  },
  "audit_logs": {},
  "float_low_sats": 100000,
  "journal": true,
  "fork_head": "~/opentimestamps-server",
  "outbox": null,
  "inbox": null
}
```

`audit_logs`, `outbox` and `inbox` are each off when empty;
`float_low_sats` is the balance below which `float.low` is true. Each is
described below. A `host` key from an older config is ignored, and the
run says so in its log.

State: `<state_dir>/manifests/<period>.json` with the proof beside it as
`<period>.json.ots`; witnessed copies under `<state_dir>/witnessed/`;
files the inbox could not use under `<inbox>/rejected/`; the unit's log
in `<state_dir>/selfstamp.log`.

The manifest is JSON with sorted keys, two-space indent and a trailing
newline, exactly `json.dumps(m, sort_keys=True, indent=2) + "\n"`, so the
stamped bytes can be re-derived. Fields (`"schema": "selfstamp/3"`; what
each discloses and is for, field by field, is in `docs/contracts.md`):

- `chain`: the chain's label, 32 hex digits drawn at random when the
  chain began and the same on every manifest after. It names the chain
  and nothing else. It is drawn once: a run whose newest manifest has no
  label reads the chain first, and if an earlier manifest has one it
  refuses ("Recover").
- `period`: the UTC day covered, `YYYY-MM-DD`; also the file name. It is
  the journal window, not when the books were looked at.
- `created_at`: the run's clock when the manifest was begun, UTC; every
  observation in it was made after that instant, within the run.
- `seq`: 1 for the first manifest, then +1 per manifest, no gaps.
- `prev`: `null` for the first manifest; otherwise `{"file", "sha256"}`,
  the previous manifest's file name and the sha256 of its bytes. This is
  the chain link, and the same digest that manifest's proof stamps.
- `config`: `{"sha256"}` of the configuration the run used: the config
  file's bytes as they were read at the start of the run, never a later
  reread (a config handed over as a dict hashes its canonical JSON).
- `books`: one entry per configured key: `{"sha256", "bytes"}` over the
  exact file bytes; `{"missing": true}` if the file is absent;
  `{"unstable": why}` if it changed while being read ("What a digest
  promises"); `{"error": class and errno}` if it could not be read.
  Recorded, never fatal, and never a path.
- `journal`: `null` when off; otherwise `{"since", "until", "command",
  "sha256", "bytes"}` over `journalctl --since '<period> 00:00:00 UTC'
  --until '<next day> 00:00:00 UTC' -o export -q`, or the same with
  `error` in place of the digest when the call failed. Reproducible by
  anyone holding that day's journal, which needs the journal to be
  persistent and still to retain the day (`SystemMaxUse` caps it), or an
  export.
- `fork_head`: `null` when off; otherwise `{"ref", "commit"}` read from
  `.git/HEAD` by file, or `{"missing": true}`.
- `audit_logs`: `null` when none is configured; otherwise one entry per
  configured key: a file as `{"sha256", "bytes", "mtime"}`, a directory
  as `{"files": [{"sha256", "bytes", "mtime"}…], "skipped": {reason:
  count}}`, with `unstable` beside them when the listing changed during
  the pass ("External audit logs").
- `float`: the anchor wallet, `{"source": "calendar status",
  "balance_sats", "low_below_sats", "low"}`, or `{"source",
  "low_below_sats", "error"}` when the calendar did not answer ("The
  float").
- `witnessed`: a list, usually empty, of the foreign manifests this host
  vouches for since its previous manifest, each `{"chain", "seq",
  "period", "file", "sha256", "witnessed_at", "foreign_proof"}`
  ("Witness by file drop").

A manifest holds hashes, sizes, times, counts, state words and the chain
label: never the contents of a file it hashes, and no host name, path or
file name of anything on the host or in an audit directory, because any
of those can name a client. The same rule holds for the outbox's file
names, the copies' names and the log. Manifests written under
`selfstamp/1` and `/2` carried `host`, a `path` in every book entry, the
directory path and file names under `audit_logs`, and (`/2`) a
`commissioning` block; `verify` reads them as they are, nothing rewrites
them, and what they disclosed stays disclosed ("Commissioning" and
"Witness by file drop" say how such chains continue and are witnessed).

#### What a digest promises

A book is read once, in chunks. The entry carries a digest only if the
file held still: the path still names the same file afterwards, and its
size, mtime and ctime from the open descriptor agree before and after the
read with the bytes counted. A file that was appended to, truncated,
rewritten in place, replaced under its path or removed while it was read
gets `{"unstable": why}` and no digest that day: a prefix of a growing
file is not the file, and a mix of two versions is nothing. Unchanged
metadata does not prove an atomic snapshot (a rewrite inside one
timestamp tick is not seen), and files that each held still do not prove
a coherent directory: a directory's entry says `unstable` only when its
listing changed during the pass. Point the tool at immutable exports, or
at files with a snapshot boundary you control; a live log is recorded
when it holds still and named unstable when it does not.

#### Commissioning

The first manifest of a chain (`seq` 1, `prev` null) is its commissioning
record. It carries what every manifest carries: the chain's label, the
calendar fork's commit, and the fingerprint of the configuration the run
used. Its `created_at` is the box's own clock when the chain began, an
observation; the block named by its proof's Bitcoin attestation is the
proven bound (the manifest existed before that block); nothing records
or proves when the software was installed. `verify` prints the record as
`genesis chain=… at=… fork=… config=…`. Every later manifest carries the
fork's commit and the config fingerprint too, so a change of either shows
on the next manifest. A chain begun under `selfstamp/2` has instead a
`commissioning` block on its genesis (`host`, `installed_at`, the fork
commit, the config's path and hash); `verify` prints it as `commissioned
host=… fork=… config=… at=…` and treats a `/2` genesis without it, or a
later `/2` manifest with one, as a break. A chain begun under `/1` has
neither.

#### The float

Each manifest records the anchor wallet's confirmed balance, read from the
calendar's status line on loopback (`GET /`), the same line the watcher
reads, so no RPC credential is needed. `float.low` is `true` below
`float_low_sats` (default 100,000 sats: five fee caps at the compose
files' 20,000-sat cap, the same figure as the gateway's float alarm and
the watcher's `CAL_MIN_SATS`; keep the two equal). The host never pauses
on it: anchoring waits when the wallet is empty, intake continues. A
status line that does not answer, or answers without a readable balance,
is recorded as `{"error": …}` with no balance and no `low` (an unknown
float is neither zero nor healthy) and never stops the run.

#### External audit logs

`audit_logs` points the manifest at files the host does not own: an
application's audit-trail export, an append-only log, a rotating file
set. Each configured key is a file or a directory. A file is hashed in
64 KiB chunks (memory stays flat whatever the size) and recorded with its
size and mtime as read from the open descriptor, under the same stability
rule as a book. A directory is hashed file by file, its regular files,
not recursed, and listed by digest, size and mtime, sorted, without the
file names: a name in someone else's audit directory can name a client.
Entries that are not regular files are counted under their reason, and a
symlink is followed only if it resolves inside the configured directory
(else counted as `symlink outside the configured dir`, never read).
Rotation needs no rule: the files are hashed as they are at the run, and
the next day's manifest shows what changed; a listing that changed during
the pass makes the entry `unstable` beside the files that held still. A
missing path is recorded as `{"missing": true}`, never fatal.

Each daily manifest then carries the sha256 of each file as it stood that
day, anchored in Bitcoin within the next anchor window. A file you hold
that hashes to a recorded digest stood in that directory that day;
whether the trail was complete or truthful when written, and what
happened between two daily hashes, the manifest does not record. The host
reads the files and keeps their hashes; it never copies them, never
serves them, and the manifest never quotes a line or a name of them.

#### Witness by file drop

A chain shows what was written and that its middle is intact; it cannot
show its own tail, and it ends with the host. A second host running this
tool can hold the tail for it, with plain files and nothing else.

On the host being witnessed, set `outbox` to a directory. After every run
the tool writes each manifest there as `<chain>-<period>.json` and, once
its proof is a proof of the manifest's bytes carrying a Bitcoin
attestation, the proof as `<chain>-<period>.json.ots` (a pending proof
names a loopback calendar nobody else can reach, so it is not exported; a
proof of other bytes, or one this reader cannot parse, is withheld and
named in the log as `export withheld`). Files already there with
identical bytes are left untouched; each is written whole, so a reader
may copy any file it sees, except dotted names, which are temporaries.
Manifests written under an older schema keep the `<host>-<period>` names
their exports already have.

On the witness, set `inbox` to a directory. How the files travel (`scp`
on a timer, a USB stick, a shared mount) is the operator's choice: there
is no network code and no listener in this tool on either host, and the
witness needs nothing from the source but the files. The tool's lock does
not cover the deliverer, so delivery has one rule: write into the inbox
under a name the tool ignores (a leading dot, or any suffix but `.json`
and `.json.ots`), then rename into place. Names beginning with `.claim-`
are the tool's own and a deliverer never writes one. A name may be used
again: before a run reads anything it renames every delivery under a
final name to `.claim-<8 hex>-<name>`, atomically, so a file renamed over
a name the run is holding is a new delivery for the next run and is never
removed under it; a claim left by a run that died is finished by the next
one. A file copied in place under its final name is read whenever the run
comes; caught half written, it is quarantined as malformed and must be
delivered again.

Each run consumes every claimed `*.json` that parses as a selfstamp
manifest (schema, `seq`, `period`, and its label, 32 hex digits, or a
host name under an older schema): it keeps a copy under
`<state_dir>/witnessed/` as `<chain>-<period>-<first 12 hex of its
sha256>.json` (`legacy-<period>-…` for a source under an older schema,
whose host name is not made into a file name here), stamps the copy's
sha256 through its own operator lane (never the counted door: a
witnessed file is not a record and never reaches a receipt), upgrades
that proof on later runs like its own, and removes the claim. The copy
is written before anything is removed, so a run interrupted anywhere
converges: a file whose bytes are already held (by content, whatever the
copy's name) is a duplicate and is removed after any proof delivered
beside it is kept. A proof delivered beside a manifest, or alone later as
`<name>.json.ots`, is kept as `<copy>.foreign.ots` if it is a proof of
exactly those bytes and says more than what is held: the source exports
its proof only once it is anchored, so the proof normally arrives on a
later pass than its manifest; a later proof replaces a pending one and
never an anchored one, and `verify` prints what is held now as
`foreign_now`. What is held counts only if it is itself a proof of the
copy: a held file that is not is set aside beside the copy as
`<copy>.foreign.ots.rejected-<12 hex>` and logged, and never outranks a
proof that is. A proof of no copy held is left as a claim and named in
the log every run (`awaiting its manifest`); there is no timeout, unless
the manifest of its name has already been quarantined, in which case it
joins it. A file that is not a manifest, or a companion that is not a
proof of its manifest, is moved to `<inbox>/rejected/<12 hex of its
sha256>-<its delivered name>` and logged, its bytes fsynced before the
rename and both directories after it, so the quarantine is durable before
it is acknowledged: kept, never deleted, and two bad deliveries under one
name are both kept. An inbox file or directory that cannot be read is
logged (by error class, never by path) and counted (exit 1) and stops
nothing else. Two different manifests claiming one chain and seq are
both copied and both vouched for; `verify --witness` says so.

The witness's next manifest lists every copy no earlier manifest listed,
as a `witnessed` entry naming the source `chain` (`null` for a source
under an older schema), `seq`, `period`, the copy's file name and sha256,
when it was witnessed, and what the foreign proof said at that moment
(`bitcoin height=N`, `pending`, or `null` when none came). `bitcoin
height=N` says an attestation is present in the bytes; nothing here
checks it against Bitcoin ("Verify"). From that manifest on, the
witness's chain vouches for that exact foreign file. A copy that cannot
be read or no longer parses when the manifest is built is named in the
log, counted, and tried again next run.

A host can be witness and witnessed at once (both keys set), and two
hosts can witness each other; neither reads anything of the other but
the files, and Bitcoin orders both chains. A witness running an older
version of this tool refuses a `selfstamp/3` manifest by schema and moves
it to its `rejected/`, losing nothing: both hosts run this version or
newer.

### The watcher

`ops/watch.py` runs once per timer tick (`ops/systemd/watcher.timer`,
every 5 minutes), standard library only, no listener. It reads the
calendar's status line on loopback, unit and container states, files and
the journal, writes `~/watcher/status`, `state.json` and `watch.log`, and
alerts through ntfy (optional) on transitions only; one line a day is the
heartbeat. Every step, every check and who owns the work between two
steps is in `docs/contracts.md`, section 8.

Every check has three answers: ok, failed, and unknown, which is what a
check says when its source could not be read, decoded or parsed, gave no
answer, or answered in a shape the check does not expect (`docker ps`
failed, a file unreadable or not UTF-8, a `journalctl` query that did not
run, a status or `/health` body that is not an object, a fresh adapter
heartbeat without its `breaker` field, no thermal reading). One bad
source stops nothing else. Unknown is never ok: the status line's word is `unknown` when nothing has failed but
something could not be read, each unknown check is named there and in
the heartbeat, and an unknown check alarms like a failure after the same
two runs, worded as unknown. A check whose unknown is another check's
doing is suspended while that check fails and the other's alarm speaks
for it: `health` behind `health_reach`, and the four journal counts
(journal errors, ssh failures, refused outbound, unexpected logins)
behind `journal_read`. A box with no thermal sensor or no readable
`/proc/meminfo` sets `TEMP_C` or `MEM_MB` empty, which skips the check
like every other empty knob.

Alerts are transitions: a check that has failed (or been unknown) on
`CONFIRM_RUNS` runs in a row joins the delivered set and one DEGRADED
message names it and what is still delivered; when the set empties after
as many ok runs, one RECOVERED message; nothing while a problem
persists, and a check that flaps under the confirmation never alarms.
The burst checks (the four journal counts) alarm on the run they are
seen and clear on the next. A RECOVERED says `all N checks ok` only when
every check is ok at that moment; otherwise it says what is not yet
clear. The delivered set lives in `state.json` (it records that an alarm
was decided and queued, not that the operator received it), so a restart
of the box or of the run loses no transition: the state, the journal
cursor and the outbox as it must now be (the queue with this run's
messages appended and the bound applied, under `owed`) are written in one
rename, and only then is that queue copied to `~/watcher/outbox.json` and
the state rewritten without it. A run that stops before that rename
recorded nothing, and the next run observes afresh and reads the journal
window again; one that stops after it, before or after the outbox write,
is finished by the next run copying the recorded queue over the outbox:
every message queued once, every discard final, the order kept.

Delivery is oldest first, one ntfy post each, stopping at the first
failure so alerts never reorder; a message whose send failed stays in
the outbox, the run exits 1, the status line carries `queued=N`, and
every later run tries again from the head. Delivery is at least once: a
stop between a send and the rewrite that removes the message sends it
again. The outbox keeps the newest 200 messages, a deliberate bound on
what a long outage can pile up; when more would be queued the oldest are
dropped and the drop is itself the first message in line, a notice
saying how many were dropped and when they were queued, so the loss is
never silent (what they said is lost, and the notice says so). With
`NTFY_URL` empty nothing is queued: the transition is recorded and the
log carries the text.

Only a missing outbox is an empty queue: one that exists but cannot be
read fails the run with nothing done, and one whose bytes are not a list
of messages with the fields delivery needs is set aside as
`outbox.json.corrupt-<12 hex of the bytes' sha256>` and reported as an
alert. The state file is read the same way: missing is a fresh start,
unreadable fails the run with nothing done, and bytes that are not a
state object, or an object whose fields are not what the run relies on,
are set aside as `state.json.corrupt-<12 hex>` and the run goes on from
a fresh state with a notice saying what a fresh state cannot know (the
checks that were failing alarm again once; the journal since the last
good run is not read again; an alert the old state still owed is in the
aside file only). A dry run sets nothing aside. A `journalctl` call that fails is a failed check
(`journal_read`) and the journal cursor stays where it was until every
journal query succeeds, so no window goes unread. A run holds an
exclusive lock on `WATCH_DIR` for its whole duration; a second run waits
up to a minute, then exits 1 with `locked`, having observed nothing,
and the window it would have read is read by the next run.

The heartbeat is one line per UTC day, on the first run at or after
`HEARTBEAT_HOUR`, worded from that run's verdicts: `ok N/N` only when
every check is ok now, `degraded:` with each failing check (`not yet
alarmed` after one the two-run guard has not confirmed) and `unknown:`
with the unknown ones, then disk, temperature, memory, anchors, the
wallet, pending updates, egress drops and uptime.
Its absence is what an outside reader notices: a box that is down or cut
off sends nothing, and nothing from the box can say so.

What leaves the box is the box's `NAME` (the hostname when unset: set
it), the checks' details and counts; no message names a path or a
configured mount (a disk check says `disk_root at 91%`), and an
unexpected ssh login goes off-box as a count while the address stays in
`watch.log`. Config: `<WATCH_DIR>/config` (`ops/watch.config.example`); a
knob left empty skips its check, so it neither fails nor counts. With
the single-host config that is sixteen checks: the calendar's status
(reachable, `best_block` set, no anchor needing attention, receipts on,
wallet above `CAL_MIN_SATS`), the containers and units, disk,
temperature, memory, the adapter's heartbeat and breaker, journal errors,
ssh failures and unexpected logins, the journal readable, the age of the
last confirmed anchor (unknown on a new host until its first receipt),
pending reboots, refused outbound packets, bitcoind's peers. The
self-stamp is watched through `selfstamp.timer` only; its summary line
and lock are not read. `watch.py --dry` makes every check and sends
nothing.

### The headers export

`ops/export_headers.py` writes `headers.bin`: every block header the
node holds, 80 bytes each from genesis, about 77 MB, for the claim-kit
verifier. It uses the calendar's own node access
(`BITCOIN_RPC_SERVICE_URL` from the compose `.env`, with `--rpc-host
127.0.0.1` on the host; the three RPCs it needs are in the whitelist of
step 1) and appends what the node has beyond the file; the first run
exports the whole chain. Before anything is written each new header must
link to the last one on file, pass Bitcoin Core's target rules (a
negative, zero, overflowing or above-powLimit bits field is refused) and
meet the proof-of-work target its own bits field encodes; a header that
fails is refused and the run exits 1 with the file untouched. A tail the node no longer agrees with (a reorg) is
cut back to the last common header and re-exported, logged. One writer
at a time: the run holds `headers.bin.lock` (across processes) and a
second run is refused with exit 1; every byte is written by a checked
loop and fsynced, and a file that ends in part of a header (an
interrupted append) is cut back to its last whole header and the run
goes on, logged `recovered`. The timer
(`ops/systemd/headers.timer`, 01:00 UTC) runs it daily into
`~/claim-kit/export.log`. Which chain the file is, the exporter does not
judge: the verifier's genesis check and a stated checkpoint do.

## Verify

### A proof, with the public client

An anchored proof verifies with `ots verify -f FILE FILE.ots` against a
Bitcoin node, or `ots --no-bitcoin verify …` to be told which block and
merkle root to check on any explorer; `ots info FILE.ots` shows the path,
including the not-before bound. Nothing in an anchored proof names a
service that must stay alive. A pending proof names this calendar's URI,
which is where it is upgraded (`ots -l <uri> upgrade`, or through the
adapter).

### The claim kit

One folder per exhibit, verified with `python3` and nothing else: no
network, no package to install, no service that has to answer. The folder
holds the exhibit, its proof (`<exhibit>.ots`), `headers.bin` ("The
headers export") and `verify_claim.py` (this tree's `ops/verify_claim.py`,
standard library only).

Building a kit, on the host:

```bash
mkdir ~/claim-kit/<exhibit-name> && cp EXHIBIT EXHIBIT.ots ~/claim-kit/headers.bin ops/verify_claim.py ~/claim-kit/<exhibit-name>/
python3 ~/claim-kit/<exhibit-name>/verify_claim.py EXHIBIT EXHIBIT.ots headers.bin      # the same run the recipient will make
```

Verifying one, anywhere:

```bash
sha256sum EXHIBIT                                   # the digest the proof must be about
python3 verify_claim.py EXHIBIT EXHIBIT.ots headers.bin
python3 verify_claim.py EXHIBIT EXHIBIT.ots headers.bin --checkpoint 959465:00000000000000000001af7d70e3b5f90888b2e3081bfbc108b47a73ad36fea8   # at or after the attested block
```

The verifier prints every step and exits 0 only if all of them hold: 1
when any check fails, 2 when every check that could run passed but
nothing ties the headers file to Bitcoin (`INCOMPLETE`, below):

1. the exhibit's sha256 is the digest the proof is about;
2. every operation in the proof is replayed from that digest: the
   aggregator's nonce, the calendar's commitment (the not-before bound
   and the calendar's clock are named as such), the anchor transaction
   (its txid is printed), the block's merkle path, to the merkle root the
   Bitcoin attestation names;
3. the block at the attested height, read from `headers.bin`, carries
   exactly that merkle root, and its own timestamp is printed;
4. `headers.bin` is one chain, checked whole: every header passes
   Bitcoin Core's target rules (a negative, zero, overflowing or
   above-powLimit bits field is refused), meets the proof-of-work target
   its bits field encodes, links to the previous header's hash, and keeps
   the difficulty rule (bits unchanged between retargets; at every
   retarget, Bitcoin's adjustment recomputed from the period's
   timestamps). Nothing is skipped: the attested block, and the block a
   not-before bound rests on, are checked like every other;
5. what ties the attested block to Bitcoin: the checkpoint stated with
   `--checkpoint HEIGHT:HASH`, which must be in the file, match, and be
   **at or after the attested block**. A checkpoint pins every header at
   or below it: each header's bytes are the preimage of the next header's
   previous-hash field, so the links back from the checkpoint
   authenticate the attested block. Headers above a checkpoint are tied
   to it only by following links forward, and a chain that follows the
   rules is not thereby Bitcoin's chain: anyone can extend a fork past a
   checkpoint (a miner at real difficulty; anyone at an easy one), so a
   checkpoint below the attested block authenticates nothing about it.
   The genesis block, hardcoded, is a checkpoint at height 0 and pins
   nothing above itself. The natural checkpoint is the newest header the
   expert compared to a public source, typically the file's last.

Then a `TRUST` block states what the verdict rests on: the checkpoint is
the one thing the tool cannot check, so it prints it for comparison with
any public source. A file given no checkpoint at or after the attested
block — none at all, or one below it — is tied to nothing that
authenticates that block: every check that can run still runs, and the
verdict is `INCOMPLETE` (exit 2), never `HOLDS`; the tool names the
header to compare and state (the file's last). A checkpoint that does not
match the file is reported as `CHECKPOINT MISMATCH` with both hashes.
The headers file's origin is not evidence and the tool says so; the
chain check is. Verification against a node (`ots verify` with bitcoind)
is the other path and needs no checkpoint. `--network regtest` exists for
chains mined at an easy difficulty (the test suite's), must be asked for
by name, and is printed as `NOT Bitcoin`: the default, mainnet, is the
only network an expert is ever handed. The test suite's
`Test_incompatible_forks` builds two regtest forks off one prefix, each
anchoring a different exhibit at the same height, and shows a checkpoint
on the prefix lets neither hold while one at a fork's tip lets exactly
one.

What the verdict states: the exhibit's bytes existed before the attested
block was mined; and, when the proof carries a not-before bound, that the
calendar's commitment to the exhibit was constructed after the bound's
block (the tool finds that block in the checked chain and says so). The
lower bound dates the construction of the commitment, not the creation
of the record: a record can be older than its bound. The block
timestamps printed are the miners' clocks: by consensus rule a block's
time may run at most about two hours ahead of the network's clocks and
must exceed the median of the previous eleven blocks, so it can lag more
than it can lead. The calendar's own clock in the proof is labelled "not
evidence". A proof still `pending` has no Bitcoin attestation and the
verifier says so (step 2 fails: upgrade it first). Proofs the ots client
upgraded keep their pending attestation beside the Bitcoin path; the
verifier follows the Bitcoin one and notes the other.

### The self-stamp chain

`ops/selfstamp.py verify --manifests DIR` checks, offline: for each
manifest oldest to newest, `prev.sha256` equals the sha256 of the file it
names, `seq` steps up by one, `period` steps forward and matches the file
name, the first manifest has `prev: null`; the chain label, once a
manifest has one, is the same on every later one; every proof present is
a well-formed proof of exactly its manifest's bytes; a `selfstamp/2`
first manifest carries its commissioning block and no later one does; and
every `witnessed` entry names a copy this host holds (by default in the
`witnessed` directory beside `manifests`; `--witnessed DIR` names
another) that hashes to the recorded sha256, whose own proof, if present,
is a proof of those bytes. Each manifest is printed as `<file> seq=N
chain=ok|BROKEN … proof=<state>`, a `selfstamp/3` genesis with its
`genesis chain=… at=… fork=… config=…` line, and each vouch as `vouches
for chain=… seq=… period=… sha256=… copy=ok|missing|unreadable|MISMATCH
proof=<state> foreign_proof=<as recorded> foreign_now=<as held>`. The
proof states are `missing`, `pending`, `bitcoin height=N`, `malformed`,
`mismatch`, `unknown` and `unreadable`; `missing` and `pending` are
reported and are not breaks, every other state but `bitcoin` is. A copy,
proof or manifest that cannot be read is a break, and so is a missing
`witnessed` directory: a vouch is for bytes this host claims to hold.
Every manifest is read by the same validator the inbox and the chain's
continuation use (a schema this tool reads, an integer `seq` from 1, a
date for `period`, a 32-hex label under `selfstamp/3` or a host under an
older schema); a file that fails it is `BROKEN` with the reason.
`--skip-witnessed` asks for the partial check without the copies; every
vouch is then labelled `SKIPPED` and the summary says `copies not
checked`. Every report ends by saying that an attestation present is not
checked against Bitcoin here: `bitcoin height=N` is a fact about the
bytes, and `verify_claim.py` (or the ots client against a node) is the
check against the chain. It exits 1 on any break, and 0 with any number
of proofs pending or missing.

With `--witness WITNESS_MANIFESTS_DIR` it reads another chain's manifests
and says for each manifest here whether the witness holds its hash
(`witnessed by <chain> seq=N`), holds a different one under the same
identity, or never saw it (`not witnessed`). The identity is the label
and seq, and another hash under it is `WITNESS MISMATCH`, a break: the
file changed after it was witnessed, or the witness saw a different
version. For a manifest without a label (an older schema) the identity
is seq and period alone, and another hash under it is `WITNESS
AMBIGUOUS`, reported and not a break: another version of this manifest,
or another unlabelled chain that began the same day, and only a label
tells them apart. Several versions held under one identity are said (`N
versions held for this seq`). A witness manifest that cannot be read or
is not a manifest is named, the rest are still used, and the check is
incomplete, which is exit 1: an absence found in evidence that could not
all be read is not established. A witness directory that is not there is
a break: nothing was checked. The check ends with its own line, `witness
check=ok|BROKEN|incomplete` and the counts. Two manifest directories and
`sha256sum` suffice: the vouched hash is in the witness's manifest, the
file is in the witnessed host's. No line of the report names a path.

The same chain checks can be made with `sha256sum` and `jq`, and each
proof with the ots client as above. Altering any byte of a past manifest
breaks both its successor's `prev.sha256` and its own proof; deleting a
day in the middle leaves the successor naming a file that is not there
and a gap in `seq`.

A manifest is an exhibit like any other, and its proof is beside it, so a
claim kit can hold one:

```bash
D=~/claim-kit/diary-2026-09-07 && mkdir -p $D
cp ~/selfstamp/manifests/2026-09-07.json ~/selfstamp/manifests/2026-09-07.json.ots ~/claim-kit/headers.bin ops/verify_claim.py $D/
cd $D && python3 verify_claim.py 2026-09-07.json 2026-09-07.json.ots headers.bin
```

The verifier recognises a manifest and names its chain (or, for a
manifest under an older schema, its host), period and seq.
Every manifest with its proof and one `headers.bin` is the whole chain,
each day anchored, verifiable with `python3` alone.

## Recover

This tree takes no backup; what follows is what a backup of it has to be,
and what the code does with one that is not (`docs/contracts.md`, section
10, has each transition and its tests).

**What to back up, and how.** The set: the calendar directory whole
(`uri`, `hmac-key`, `journal`, `journal.counts`, `db/`,
`journal.known-good`), `receipts/` with every `.pending.<txid>` marker in
it, the adapter's `DATA_DIR`, `~/selfstamp` whole (`manifests/` and
`witnessed/`: a witnessed entry whose copy is missing is a verification
break; the inbox with its `.claim-…` files), `~/watcher` (`state.json`,
`outbox.json`, `config`), the compose `.env`, the anchor wallet by Bitcoin
Core's own means, and the revisions of the code that wrote it all. It is
one set only if it comes from one moment: stop the adapter, the tools'
timers and any run of theirs in progress, then the calendar; copy; start
them again. One filesystem snapshot that covers all of it does as well.
A copy taken while anything runs is a hot copy, whatever made it and
whatever it reports, and a backup is a stopped copy that has been
restored somewhere and has started: a rehearsal. The original resumes
as soon as the copy is complete (its bytes compared with their source);
the rehearsal comes after, on a host that can reach no Bitcoin node and
no wallet (the stamper then waits, logging, and broadcasts nothing), that
no client and no witness delivers to, with the watcher's `NTFY_URL` empty
and the self-stamp's outbox delivered nowhere. Under that isolation a
rehearsal has no effect outside its own directories: the calendar starts
or refuses, the markers settle into the copy's receipts file, the entries
the database lacked are pending, `GET /timestamp` answers for the
commitments it should, `selfstamp.py verify` passes, the watcher's
`--dry` reads its state. A rehearsal that was not isolated has spent or
alarmed from the copy, and proves nothing: the copy is not called a
backup on its account. Making the restored copy the calendar for good is
"Restoring", below, and then the original never runs again.
Encrypted, off-host. The inbox's `rejected/` holds what a witness could
not use, for the operator to look at; it is not part of the chain.

**What a hot copy is to the next start.** The calendar refuses what its
own files show: a checkpoint read after the database, a journal that does
not reach the checkpoint, a sidecar read after the journal, a `db/` whose
files are not one database ("Restart checkpoint"), a receipt read after
the database while its marker stood ("Anchor receipts"). It cannot see,
and starts cleanly on: a journal that is older and still reaches the
checkpoint (what was accepted after it is gone, and its holders are
answered `Not found` where an entry that is owed says `Pending`);
receipts newer than the database once their marker has gone (those
records are anchored and receipted a second time); receipts older than
the database with no marker copied (that receipt is lost); a watcher
state read after a transition beside an outbox read before it (the
alert is in neither file, and the restored watcher owes nothing where
the live one did); a self-stamp manifest that vouches for a copy the
copied `witnessed/` did not yet hold (`verify` reports the break; nothing
rebuilds the copy); and any difference in moment between the calendar,
the adapter, the self-stamp and the watcher, because nothing identifies
the set as a whole. Behind
the gateway the backup is that repository's script, which archives the
calendar directory while `otsd` runs and whose `ok` is about its own
members: what it holds of this calendar is a hot copy.

**Restoring.** With nothing running, the whole set into place; Bitcoin
Core and its wallet; then the calendar, then the adapter, then the timers.
The old host, if it still exists, never runs as this calendar again: one
wallet and one `uri` are one calendar. Nothing in the set names a host, so
another machine is only another place for the same files: `uri`,
`hmac-key`, the wallet's keys and the self-stamp's label are carried and
never made again, and locks, the watcher's `status`, `headers.bin` and
the block chain are made on the new host. Code as new as the code that
wrote the set, or newer: an older self-stamp continues a labelled chain
without its label (the current one then refuses to go on until that
version's manifests have been moved out of `manifests/`, kept; those days
are a gap), and an older watcher neither delivers nor clears a queue the
newer one had recorded. A downgrade is not supported.

At every start the calendar checks that `db/` opens, that the sidecar
does not outlast the journal, that `journal.known-good` belongs to
`db/`, lies within what it durably holds, and describes the journal that
is there, and stops with the recovery text if not; the listener is bound
before any worker starts, so a port that cannot be bound is a start that
fails whole (exit 1, nothing running: 2026-09-15/16 review F19); then
the stamper re-reads the journal from the checkpoint (or from the
beginning without it) and anchors every entry the calendar does not hold
("Restart checkpoint"). Every pending-receipt marker left by a stop is
settled before anything else ("Anchor receipts"); the
receipts-off warning fires if the sidecar exists
without `OTSD_ANCHOR_RECEIPTS`; the deep-reorg detector's first check
runs on the first pass; the not-before bound returns with the first block
the stamper sees.

What a rebuild cannot give back: a commitment re-anchored after `db/`
was lost gets a proof naming a later block than its original anchor; the
original merkle path lived only in the database. Fully anchored proofs
already handed to clients stay valid on their own; pending proofs whose
calendar branch was lost upgrade to the later anchor. A pending proof
whose journal entry is gone (accepted after the backup was taken, or lost
with an older journal) never completes, and nothing on the box says so:
the calendar answers `Not found`, and the self-stamp and the adapter read
every 404 as pending. The self-stamp's is moved aside and the next run
stamps the manifest again; the adapter's is set aside as its own contract
describes (A4), and its next start owes the record again.

The on-disk format of `db/` is LevelDB's. A calendar written under the
previous binding (py-leveldb, before the move to plyvel) opens under the
current one unchanged. A database from before generations gets its
generation, with watermark 0, at the first start that finds no checkpoint
beside it; a checkpoint from that time is refused and deleted once, by
hand ("Restart checkpoint"). Nothing else is migrated.

When the anchor wallet runs dry the calendar keeps accepting and
anchoring waits (one error in its log; the watcher's `calendar` check
alarms first, at five fee caps); the adapter's intake never stops. Refill
is an on-chain payment to any address of the wallet.

The self-stamp needs nothing after an interrupted run: the next run
finishes from the files (a manifest without a proof is submitted, a copy
no manifest lists is listed, a delivery half consumed is a duplicate). Three
states are the operator's. A run that says `refused … the chain has a
label and its newest manifest … has none` has found manifests that an
older version of the tool wrote after this one had labelled the chain. A
label is drawn once, so it writes nothing: move those manifests and their
proofs out of `manifests/`, keep them, and the chain goes on from its last
labelled manifest with those days a gap. A proof that `run` and `verify` report as
`mismatch` (a proof, not of the file beside it) or `malformed` (this
reader cannot parse it) is reported every run with exit 1 and never
touched, because only the operator can say whether the manifest or the
proof is the damaged one: the successor's `prev.sha256` says whether the
manifest's bytes changed, and a proof upgraded with the ots client
against several calendars is `malformed` to this reader and valid. Move
the proof aside; the next run stamps the manifest as it is now, under a
later block. A copy or inbox file that cannot be read is named in the log
every run until it can; fix the permission and the next run folds or
consumes it. A `.claim-…` file in a witness's inbox is a delivery the tool
took into its keeping and will finish next run; leave it. Nothing in the
tool rewrites, renames or removes a manifest, a copy or a quarantined
file.

## What it does not do

- Nothing here states when a record was made, captured or received. The
  not-before bound says a commitment did not exist before block M; the
  anchor says it existed before block N; the calendar's clock in the
  proof is bookkeeping.
- The deep-reorg detector does not see an anchor re-mined at the same
  height after a restart, and runs only with receipts on; verifying
  anchored proofs against Bitcoin is the check that remains.
- It takes no backup, and it tells a hot copy from a stopped one only
  where the copy's own files disagree ("Recover"). Nothing identifies the
  calendar, the adapter, the self-stamp and the watcher as one set from
  one moment; a journal that is older and still reaches the checkpoint,
  and receipts that differ from the database with no marker on file, are
  not detected; a watcher state copied after a transition beside an
  outbox copied before it has lost that alert unseen; the holder of a
  pending proof whose entry is gone is never told. Versions move forward: what an older version does with
  newer state is measured in the tests and is not a refusal.
- A digest is counted as a record once per aggregator lifetime and dedupe
  horizon: resubmitted after a restart, or past the horizon, it is
  counted again ("Anchor receipts").
- A manifest chain does not show its own tail: deleting the newest days
  leaves a chain that still verifies. The cadence (a manifest is due
  every day; `seq` should reach today), off-host copies and a witness
  expose the tail, not the chain.
- A witness vouches for bytes, not for truth, and holds only what
  arrived; a foreign proof it holds is an attestation present in the
  bytes, not a check against Bitcoin.
- A manifest records a book as it was when it held still. A book that
  changes under the reader has no digest that day (`unstable`), unchanged
  metadata is not an atomic snapshot, and files that each held still are
  not a coherent directory ("What a digest promises"). Immutable exports,
  or a snapshot boundary the operator controls, are the remedy.
- The self-stamp's lock covers its own processes. Whoever delivers files
  into a witness's inbox follows the naming convention ("Witness by file
  drop"); a file copied in place under its final name and caught half
  written is quarantined as malformed, kept, and must be delivered again.
- The watcher cannot report the box's own death. A box that is down,
  powered off or cut off sends nothing; the daily heartbeat's absence is
  detectable only from outside the host, by whoever expects it. The
  watcher believes a source that answers wrongly, reads the self-stamp
  through its timer's unit state only, and sends the box's `NAME` and the
  checks' details to the operator's ntfy topic and nowhere else.
- A manifest written under `selfstamp/1` or `/2` keeps the host name,
  paths and file names it carried. Nothing rewrites anchored history; the
  naming rule holds from `selfstamp/3` on, and a witness records such a
  source without its host name. The price: a cross-check of such a chain
  can say `witnessed by`, `not witnessed` or `WITNESS AMBIGUOUS`, never
  `MISMATCH`, because without a label a witness cannot tell another
  version of a manifest from another chain that began the same day.
- The self-stamp does not cover the gateway's own database
  (`obligations.db`, in the gateway's root-owned data volume, whose bills
  endpoint has side effects when polled); the receipts file is the
  calendar's side of that record.
- A proof stays `pending` until the next anchor confirms, typically within
  a day; a pending proof is upgraded only against the calendar's URI, so
  the operator upgrades proofs before handing them over.
- The headers export does not judge which chain the node follows; the
  verifier's genesis hash and a stated checkpoint do.
- `otsd` does not daemonize, and the calendar's port is not designed to
  be exposed directly ("Configurations").

## Tests

Test modules live under `otsserver/tests/`:

- `test_calendar.py`: inherited from upstream, plus two storage pins (a
  missing commitment raises `KeyError`; a sync batch written in one
  process reads back in another).
- `test_otsd_launcher.py`, `test_rpc_status.py`, `test_stamper_loop.py`,
  `test_anchor_receipts.py`, `test_receipt_marker.py`,
  `test_aggregator_failure.py`, `test_stamper_cadence.py`,
  `test_rpc_digest.py`, `test_anchor_records.py`,
  `test_aggregator_dedupe.py`, `test_stamper_read_errors.py`,
  `test_stamper_checkpoint.py`, `test_stamper_wallet_empty.py`,
  `test_operator_lane.py`, `test_selfstamp.py`,
  `test_stamper_dead_cycle.py`, `test_stamper_fee_cap.py`,
  `test_watch.py`, `test_not_before.py`, `test_reorg_detector.py`,
  `test_claim_kit.py`, `test_stamper_save_retry.py`,
  `test_rpc_privacy.py`, `test_proof_corpus.py`,
  `test_selfstamp_workflow.py`, `test_watch_observation.py`,
  `test_restore_calendar.py`, `test_restore_tools.py`: regression tests for this branch's delta
  (launcher flags, the status line and its RPC wiring, stamper-loop
  crash fixes, anchor receipts, anchor cadence, the `/digest`
  Content-Length handling, anchor-receipt record counts and their
  close-time re-read, aggregator dedupe, pending-fill read-error
  survival, the restart checkpoint, empty-wallet warn-once, the operator
  lane and known-zero counts, the self-stamp tool against a
  standard-library fake of the calendar protocol with the proof bytes
  cross-checked against the opentimestamps library, dead-cycle recovery,
  fee-cap warn-once, the receipts-off startup warning, the watcher's 30
  fixture scenarios, the self-stamp's commissioning block, float,
  external audit logs and witness by file drop, the not-before bound as
  the opentimestamps library and the standard-library parser read it,
  the deep-reorg detector against an in-process double of a fake
  bitcoind's reorg contract, and the claim kit: two anchored proofs from
  a deployment, at blocks 959459 and 960458, against mainnet headers
  pinned under `ops/tests/claim/`, the difficulty rule against mainnet's
  retarget at 965664, a manifest kit built and tampered with, and the
  headers export against a fake node). They stub everything external
  with `unittest.mock` or standard-library fakes: no bitcoind, no
  network.

  The 2026-09-15 review's findings each have a regression that fails on
  the code before the fix: the hostile claim kit, Bitcoin Core's target
  rules and the `INCOMPLETE` verdict (`test_claim_kit.py`); the mature
  tree kept until its save is durable and retried every pass
  (`test_stamper_save_retry.py`); the receipt write-all loop, tail
  recovery and directory fsync (`test_receipt_marker.py`); the storage
  generation, the committed watermark and an older database restored
  beside a newer checkpoint (`test_calendar.py`); a stamper that cannot
  start and an aggregator that fails stopping the whole service, in
  process and at the process boundary with the real `otsd`
  (`test_stamper_checkpoint.py`, `test_aggregator_failure.py`,
  `test_otsd_launcher.py`); no peer address or request line in any log,
  over a real socket (`test_rpc_privacy.py`); the self-stamp lock across
  processes, unique temporary names, missing witnessed copies and later
  foreign proofs (`test_selfstamp.py`); the exporter's lock, write-all
  loop and tail recovery (`test_claim_kit.py`); the watcher's outbox
  (`test_watch.py`). The 2026-09-15/16 review's findings likewise: the
  journal checked against the checkpoint, with a coherent restart's
  first fill pass as the control (`test_calendar.py`); one receipt
  marker per anchor and the old name still settled
  (`test_receipt_marker.py`); a bind failure that exits with nothing
  running (`test_otsd_launcher.py`); the watcher's failed journal read,
  unreadable and corrupt outbox, and whole-run lock across two real
  processes (`test_watch.py`); and the proof corpus
  (`ops/tests/proof_corpus.py`) run against both readers under `ops/`
  and against the `opentimestamps` library as the oracle
  (`test_proof_corpus.py`; `docs/contracts.md`, "The proof parser").
  Every durability test injects failures at the write boundary or kills
  the process; none simulates a power cut.

  The self-stamp sitting of 2026-09-16 (workflow two, `docs/contracts.md`
  section 9) adds `test_selfstamp_workflow.py`: the transitions S1–S9
  with their failure cases, each class naming its fault model. A hook
  inside the tool's read loop plays a writer touching a book (append,
  truncation, rewrite, replacement, removal); the n-th rename, replace,
  unlink or fsync of a fresh run raises, swept from n = 1 until a fresh
  run has no n-th call, every case from a fresh fixture, every injection
  asserted to have fired and every recovery interrupted once more, for a
  run with one delivery pair and one with a bad pair; a deliverer renames
  a second file over a name the run is holding; a child process is
  paused at a named boundary and killed with SIGKILL (after the manifest,
  after the calendar's answer, after a copy); the fake calendar commits a
  digest and drops the answer, or cuts an upgrade short; permission
  faults come from `chmod`; the syscalls of a quarantine are recorded in
  order; and the `selfstamp/2` corpus under `ops/tests/selfstamp/v2/`
  (written by the tool at 3961a1f) is verified, continued and witnessed
  under the new code. Each defect's regression fails on the code before
  the fix: an unstable book given a digest, the config fingerprint
  reread, host names and paths in manifests, exports, copy names and
  logs, a rejected companion deleted, a proof arriving after its manifest
  ignored, an unreadable inbox file or copy ending the run, a mismatched
  proof upgraded in silence, a missing witness directory passing, an
  unreadable copy ending `verify` with a traceback, a half-delivered file
  consumed, a day not yet over accepted; and, from the sitting's gate
  review, a delivery replaced under its name lost, a held proof of other
  bytes outranking a right one, a proof of other bytes exported,
  unreadable witness evidence read as success, unrelated legacy chains
  called a mismatch, paths in messages, a label that is not 32 hex
  accepted, a predecessor with an unknown schema accepted, a quarantine
  acknowledged before it was durable.

  The watcher sitting of 2026-09-16 (workflow three, `docs/contracts.md`
  section 8) adds `test_watch_observation.py`: the observation contract
  W1–W9, each class naming its fault model (observations carrying the
  marker `observe` leaves for a source it could not read; an exception or
  a stop injected at the n-th write; chmod; a child process killed after
  the record; the lock held in-process; a sender that fails). The fault
  mechanisms shared with the self-stamp's tests live in
  `otsserver/tests/faults.py`. Each defect's regression fails on the code
  before the fix: a source that could not be read reading as ok (a
  missing or unreadable receipts file, a failed `nmcli`, an unreadable
  module list or heartbeat, a body that is not an object, the journal
  counts behind a failed query), a transition recorded after its message
  was queued so that a stop between the two alarmed twice, the outbox's
  bound dropping alerts with only a log line, a corrupt `state.json`
  stopping every run with a traceback, the heartbeat's disk figures read
  from mount points the config did not name, a login's address and the
  state directory's path in what leaves the box; and, from the sitting's
  gate review, invalid UTF-8 in the heartbeat or the receipts and a JSON
  list from `/health` stopping the run, a failed feeder tail, an empty
  heartbeat and a failed Tor warning query reading as ok, a heartbeat
  saying `ok` on a first failed observation and a RECOVERED saying all
  ok while another check was unknown, the cap decided again on replay
  after a stop, malformed fields inside valid JSON crashing every run or
  vanishing, and a configured mount in a message.

  The restore and migration sitting of 2026-09-17 (workflow four,
  `docs/contracts.md` section 10) adds `test_restore_calendar.py` and
  `test_restore_tools.py`. The first uses a real calendar on LevelDB, its
  journal and sidecar, the receipts file and its markers and a stamper
  that opens and fills from them, with Bitcoin alone doubled. A copy of
  the whole tree is kept at each boundary between the writes of one
  submission and one anchor's save, and sets are composed whose members
  come from different points: that is the sequential copy, and each set
  is started as `otsd` starts. Other fault models, each named by its
  class: a table file removed from a copied database; a member left out
  of a restore; a stop at the n-th write, truncate, fsync, unlink, rename
  or replace of every settling of a marker and of the checkpoint's
  publication, every case from a fresh copy, every injection asserted to
  have fired, every recovery stopped once more, and the number of calls
  swept asserted; an fsync that fails under the receipt's append. The
  second runs the two tools as they were before their state changed, from
  `ops/tests/migration/` (each file's sha256 checked before it is
  executed), and sweeps the first run of the current tools over restored
  state the same way. Each defect's regression fails on the code before
  the fix: a checkpoint from before generations adopted on a probe of one
  entry; a sidecar that outlasts its journal starting cleanly; a receipt
  on file for a save the database lacks discarded as `nothing is owed`; a
  database that does not open ending in a traceback that names the
  directory; a receipt found on file losing its marker before it was
  synced; the calendar's directory in every refusal and in the marker,
  tail and sidecar messages; a labelled chain that an older version had
  continued given a second label; and, from the sitting's gate review
  (2026-09-18), a marker whose discard could not be removed satisfied by
  the next anchor's save of the same commitments (five records receipted
  as seven, on the real store), a checkpoint that cannot be read ending
  in a traceback at one reader and a line with the path at the other, a
  malformed checkpoint quoted into the log, and the receipts file's name
  in the marker messages; and from the review of those corrections, a
  digit `int()` does not take (`²`) quoted by `int()` at both readers, a
  marker of bytes that are not UTF-8 quoted by the decoding error's
  repr, and a receipts file whose own name holds `.pending.` printed as
  an anchor. What the files cannot show is pinned as it is,
  and called a limit: two skews of the receipts against the database, an
  older journal that still reaches the checkpoint, a journal that differs
  between the two probes, a watcher state copied after a transition
  beside an outbox copied before it, and what the older tools do with
  newer state. A restore under another root completes the pending proofs
  and settles the owed receipt as a control: that held before. The
  boundaries swept are the named calls (write, ftruncate, fsync, unlink,
  rename, replace); the checkpoint is written through a buffered file, so
  its sweep has no `os.write` to stop at, and a stop injected as an
  exception unwinds through the code's own cleanup, which a killed
  process or a power cut does not.

No test module needs a running Bitcoin node. Every module does need the
full dependency set installed, and `plyvel` is a native build:
`otsserver/calendar.py` imports it at module level, so without it every
module fails at collection with `ModuleNotFoundError`.

Run the suite in the deployment-matched environment, the otsd image
(`python:3.13-slim` plus `build-essential libleveldb-dev` and
`pip install -r requirements.txt`), or in a venv on any current Python
with the LevelDB headers installed (`libleveldb-dev` on Debian, `leveldb`
from Homebrew on macOS):

```
python -m unittest discover -v                       # Ran 382 tests ... OK
```

The otsd image has no pytest; where pytest is installed,
`python -m pytest otsserver/tests -q` runs the identical set. Other
platforms and Python versions have not been tried; treat any claim about
them as unverified until you run the command above.
