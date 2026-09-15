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
  directory fsynced, before its marker goes; a crash loses at most one
  receipt, named in that marker and recovered from it, and never writes
  two receipts for the same records. Record counts err low, with one
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
  checkpoint it cannot read, or one that disagrees with the database:
  "Restart checkpoint"). A dead worker never sits behind a live port.
- The tools under `ops/` open no listener. Their inputs beyond the
  filesystem are the calendar on loopback, one `journalctl` call
  (self-stamp), bitcoind RPC (headers export) and, for the watcher, the
  host's own commands; the watcher's one output off-host is an optional
  ntfy post.
- A manifest holds paths, hashes, sizes, times, counts and host names,
  never the contents of a file it hashes, and leaves the host only through
  the configured outbox or by hand ("The self-stamp").

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
- No crash writes two receipts for the same records, and a crash loses at
  most one receipt, named. The receipt is written after the calendar
  save, guarded by a marker: before the save the stamper writes
  `<receipts file>.pending`, the receipt line plus one commitment of the
  anchor's tree, atomically (fsynced, with its directory); after the save
  it appends the receipt, every byte checked (a short write is completed,
  never taken for a whole line), fsyncs the file and its directory, and
  only then removes the marker. An incomplete last line left by an
  interrupted append is dropped before the next append and its receipt
  recovered from the marker, so no completed receipt is ever duplicated
  and no outstanding marker discarded. What the tests inject is the
  failure at the write boundary (short writes, errors, a stop between the
  steps); a power cut is not simulated, and the fsyncs are what a power
  cut relies on. A marker still present at the next start, or when
  the next anchor confirms, is settled by asking the calendar whether it
  holds that commitment. If it does, the save completed and the receipt
  is appended unless its txid is already on file (`recovered from the
  pending marker`). If it does not, the save never happened; those
  commitments are still pending and will be re-anchored under a new txid
  with their own receipt, so this receipt is discarded (`nothing is owed
  for <txid>`). An unreadable marker is set aside as
  `<marker>.corrupt-<time>` and warned about.

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
`CALENDAR STORAGE INCONSISTENT` and the recovery text, exit 1. Nothing in
this check reads a file's timestamp. Recovery is to delete the checkpoint
and start again: the stamper rescans from index 0 and re-anchors every
commitment the database lacks ("Recover" for what that costs). Migration
of a calendar from before this (a file holding the index alone, a
database without a generation): at the first start the checkpoint is
adopted only if the journal entry just below it is in the database; the
database is then stamped with a generation and that watermark and the
file rewritten in the new form, logged at WARNING; otherwise the start is
refused with the same recovery text. To skip the adoption, delete the
checkpoint before the first start: one full rescan.

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
are one `journalctl` call and the calendar on loopback.

```
python3 ops/selfstamp.py run     --config ~/selfstamp/config.json   # heartbeat, idempotent
python3 ops/selfstamp.py upgrade --config ~/selfstamp/config.json   # the upgrade pass alone
python3 ops/selfstamp.py verify  --manifests ~/selfstamp/manifests  # offline, no config needed
```

`run` is idempotent per period: a manifest that exists is left alone (a
second run the same day writes nothing), a manifest without a proof is
resubmitted rather than rewritten, a pending proof is asked about once
per run, a complete proof is never touched again. `run` and `upgrade`
hold an exclusive lock on the state directory (`<state_dir>/.lock`,
across processes) for their whole duration, so the timer's run and a
manual one never interleave their writes: the second waits up to
`--lock-wait` seconds (default 300), then exits 1 with `locked`. Every
file is written under a unique temporary name, fsynced, renamed, and its
directory fsynced. The trigger is the
clock and only the clock: an anchor confirming, which appends a receipt
line (including for the anchor that carried this manifest), never causes
a manifest; the next day's manifest records the new receipts hash. Missed
days are not backfilled; the chain links across the gap.

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
described below.

State: `<state_dir>/manifests/<period>.json` with the proof beside it as
`<period>.json.ots`; witnessed copies under `<state_dir>/witnessed/`; the
unit's log in `<state_dir>/selfstamp.log`.

The manifest is JSON with sorted keys, two-space indent and a trailing
newline, exactly `json.dumps(m, sort_keys=True, indent=2) + "\n"`, so the
stamped bytes can be re-derived. Fields (`"schema": "selfstamp/2"`;
`verify` also reads a chain begun under `selfstamp/1`, whose manifests
lack the last four):

- `host`: the host's hostname, or the configured name.
- `period`: the UTC day covered, `YYYY-MM-DD`; also the file name.
- `created_at`: when the manifest was written, UTC.
- `seq`: 1 for the first manifest, then +1 per manifest, no gaps.
- `prev`: `null` for the first manifest; otherwise `{"file", "sha256"}`,
  the previous manifest's file name and the sha256 of its bytes. This is
  the chain link, and the same digest that manifest's proof stamps.
- `books`: one entry per configured book, `{"path", "sha256", "bytes"}`
  over the exact file bytes, or `{"path", "missing": true}` if the file
  is absent (recorded, not fatal).
- `journal`: `null` when off; otherwise `{"since", "until", "command",
  "sha256", "bytes"}` over `journalctl --since '<period> 00:00:00 UTC'
  --until '<next day> 00:00:00 UTC' -o export -q`. Reproducible by anyone
  holding that day's journal, which needs the journal to be persistent
  and still to retain the day (`SystemMaxUse` caps it), or an export.
- `fork_head`: `null` when off; otherwise `{"path", "ref", "commit"}`
  read from `.git/HEAD` by file.
- `audit_logs`: `null` when none is configured; otherwise one entry per
  configured name: a file as `{"path", "sha256", "bytes", "mtime"}`, a
  directory as `{"dir", "files": [{"name", "sha256", "bytes", "mtime"}…],
  "skipped": [{"name", "reason"}…]}` ("External audit logs").
- `float`: the anchor wallet, `{"source": "calendar status",
  "balance_sats", "low_below_sats", "low"}`, or `{"source",
  "low_below_sats", "error"}` when the calendar did not answer ("The
  float").
- `commissioning`: on the first manifest (`seq` 1) `{"host",
  "installed_at", "fork_commit", "config": {"path", "sha256"}}`; `null`
  on every later one ("The commissioning block").
- `witnessed`: a list, usually empty, of the foreign manifests this host
  vouches for since its previous manifest, each `{"host", "seq",
  "period", "file", "sha256", "witnessed_at", "foreign_proof"}`
  ("Witness by file drop").

The books and audit logs are hashes of files; the manifest holds paths,
hashes, sizes, times, counts and host names, never the contents of a file.
It leaves the host only through the configured outbox, or by hand.

#### The commissioning block

The first manifest of a chain (`seq` 1) carries a `commissioning` block:
the host's name, the moment the chain began (`installed_at`, the same
instant as that manifest's `created_at`), the calendar fork's commit, and
the sha256 of the self-stamp config file (or, for a config handed over as
a dict, of its canonical JSON). Stamped and anchored like any manifest, it
records that a host of this name, running this calendar code under this
configuration, began its chain at this time, and every later manifest
links back to it. It records nothing about the firmware, the operating
system, the hardware, or the files the host later hashes. A chain begun
under `selfstamp/1` has a first manifest without the block; its `host`
and `fork_head` fields carry the same names without a hash of the config.
`verify` prints the block as a `commissioned host=… fork=… config=… at=…`
line and treats a `selfstamp/2` first manifest without one, or a later
manifest with one, as a break.

#### The float

Each manifest records the anchor wallet's confirmed balance, read from the
calendar's status line on loopback (`GET /`), the same line the watcher
reads, so no RPC credential is needed. `float.low` is `true` below
`float_low_sats` (default 100,000 sats: five fee caps at the compose
files' 20,000-sat cap, the same figure as the gateway's float alarm and
the watcher's `CAL_MIN_SATS`; keep the two equal). The host never pauses
on it: anchoring waits when the wallet is empty, intake continues. A
status line that does not answer, or answers without a readable balance,
is recorded as `{"error": …}` and never stops the run.

#### External audit logs

`audit_logs` points the manifest at files the host does not own: an
application's audit-trail export, an append-only log, a rotating file
set. Each configured name is a file or a directory. A file is hashed in
64 KiB chunks (memory stays flat whatever the size) and recorded with its
size and mtime as read from the open descriptor. A directory is hashed
file by file, its regular files, sorted by name, not recursed, with the
same three fields each; entries that are not regular files are listed as
skipped, and a symlink is followed only if it resolves inside the
configured directory, else it is listed as skipped and never read
(`symlink outside the configured dir`). Rotation needs no rule: the files
are hashed as they are at the run, and the next day's manifest shows what
was renamed, truncated or added. A missing path is recorded as
`{"missing": true}`, never fatal.

Each daily manifest then carries the sha256 of each file as it stood that
day, anchored in Bitcoin within the next anchor window. A later copy of
the file that differs from the recorded hash has changed since; whether
the trail was complete or truthful when written, and what happened
between two daily hashes, the manifest does not record. The host reads
the files and keeps their hashes; it never copies them, never serves
them, and the manifest never quotes a line of them.

#### Witness by file drop

A chain shows what was written and that its middle is intact; it cannot
show its own tail, and it ends with the host. A second host running this
tool can hold the tail for it, with plain files and nothing else.

On the host being witnessed, set `outbox` to a directory. After every run
the tool writes each manifest there as `<host>-<period>.json` and, once
its proof carries a Bitcoin attestation, the proof as
`<host>-<period>.json.ots` (a pending proof names a loopback calendar
nobody else can reach, so it is not exported). Files already there with
identical bytes are left untouched.

On the witness, set `inbox` to a directory. Each run consumes every
`*.json` there that parses as a selfstamp manifest (schema, `host`,
`seq`, `period`): it keeps a copy under `<state_dir>/witnessed/` as
`<host>-<period>-<first 12 hex of its sha256>.json`, keeps a proof that
came beside it as `<copy>.foreign.ots` if that proof is a proof of exactly
those bytes, stamps the copy's sha256 through its own operator lane
(never the counted door: a witnessed file is not a record and never
reaches a receipt), upgrades that proof on later runs like its own, and
removes the inbox file. The copy is the record, so a run interrupted
anywhere converges: a file already held is a duplicate and is removed,
after any proof delivered beside it is kept (the source exports its
proof only once it is anchored, so the proof normally arrives on a later
pass than its manifest; a later proof replaces a pending one and never
an anchored one, and `verify` prints what is held now as `foreign_now`);
a file that is not a manifest is moved to `<inbox>/rejected/` and logged.
The witness's next manifest lists every copy no earlier manifest listed,
as a `witnessed` entry naming the source `host`, `seq`, `period`, the
copy's file name and sha256, when it was witnessed, and what the foreign
proof said (`bitcoin height=N`, `pending`, or `null` when none came).
From that manifest on, the witness's chain vouches for that exact foreign
file.

How the files travel (`scp` on a timer, a USB stick, a shared mount) is
the operator's choice: there is no network code and no listener in this
tool on either host, and the witness needs nothing from the source but
the files. A host can be witness and witnessed at once (both keys set),
and two hosts can witness each other; neither reads anything of the other
but the files, and Bitcoin orders both chains.

### The watcher

`ops/watch.py` runs once per timer tick (`ops/systemd/watcher.timer`,
every 5 minutes), standard library only, no listener. It reads the
calendar's status line on loopback, unit and container states, files and
the journal, writes `~/watcher/status`, `state.json` and `watch.log`, and
alerts through ntfy (optional) on transitions only; one line a day is the
heartbeat. Every message is written to `~/watcher/outbox.json` before
the journal cursor moves past what produced it, delivered oldest first,
and kept and retried by every later run until it is delivered (the run
exits 1 while anything is undelivered); with `NTFY_URL` empty nothing is
queued and the log carries the text. Config: `<WATCH_DIR>/config` (`ops/watch.config.example`); a
knob left empty skips its check, so it neither fails nor counts. With the
single-host config that is fifteen checks: the calendar's status
(reachable, `best_block` set, no anchor needing attention, receipts on,
wallet above `CAL_MIN_SATS`), the containers and units, disk,
temperature, memory, the adapter's heartbeat and breaker, journal errors,
ssh failures and unexpected logins, the age of the last confirmed anchor,
pending reboots, refused outbound packets, bitcoind's peers. `watch.py
--dry` makes every check and sends nothing.

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
manifest newest to oldest, `prev.sha256` equals the sha256 of the file it
names, `seq` steps down by one, `period` steps back, the first manifest
has `prev: null`; every proof present is a well-formed proof of exactly
its manifest's bytes; a `selfstamp/2` first manifest carries its
commissioning block and no later one does; and every `witnessed` entry
names a copy this host holds (by default in the `witnessed` directory
beside `manifests`; `--witnessed DIR` names another) that hashes to the
recorded sha256, whose own proof, if present, is a proof of those bytes.
Each vouch is printed as `vouches for host=… seq=… period=… sha256=…
copy=ok|missing|MISMATCH proof=…`; a missing or altered copy is a break,
and so is a missing `witnessed` directory: a vouch is for bytes this host
claims to hold. `--skip-witnessed` asks for the partial check without the
copies; every vouch is then labelled `SKIPPED` and the summary says
`copies not checked`. It exits 1 on any break.

With `--witness WITNESS_MANIFESTS_DIR` it reads another chain's manifests
and says for each manifest here whether the witness holds its hash
(`witnessed by <host> seq=N`), holds a different one (`WITNESS MISMATCH`,
a break: the file changed after it was witnessed, or the witness saw a
different version), or never saw it. Two manifest directories and
`sha256sum` suffice: the vouched hash is in the witness's manifest, the
file is in the witnessed host's.

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

The verifier recognises a manifest and names its host, period and seq.
Every manifest with its proof and one `headers.bin` is the whole chain,
each day anchored, verifiable with `python3` alone.

## Recover

What to back up: the calendar directory as one coherent snapshot (`db/`,
`journal`, `journal.counts` and `journal.known-good` taken together, with
the calendar stopped or from a filesystem snapshot: a `db/` from one
moment beside a checkpoint from another is refused at the next start,
"Restart checkpoint"), `receipts/`, the adapter's `DATA_DIR`,
`~/selfstamp` whole (`manifests/` and `witnessed/`: a witnessed entry
whose copy is missing is a verification break), the compose `.env`;
encrypted, off-host.

At every start the calendar checks that `journal.known-good` belongs to
`db/` and lies within what it durably holds, and stops with the recovery
text if not; then the stamper re-reads the journal from the checkpoint
(or from the beginning without it) and anchors every entry the calendar
does not hold ("Restart checkpoint"). A pending-receipt marker left by a
stop is settled before anything else ("Anchor receipts"); the
receipts-off warning fires if the sidecar exists
without `OTSD_ANCHOR_RECEIPTS`; the deep-reorg detector's first check
runs on the first pass; the not-before bound returns with the first block
the stamper sees.

What a rebuild cannot give back: a commitment re-anchored after `db/`
was lost gets a proof naming a later block than its original anchor; the
original merkle path lived only in the database. Fully anchored proofs
already handed to clients stay valid on their own; pending proofs whose
calendar branch was lost upgrade to the later anchor.

The on-disk format of `db/` is LevelDB's. A calendar written under the
previous binding (py-leveldb, before the move to plyvel) opens under the
current one unchanged; the generation and watermark are added at the
first start ("Restart checkpoint"), nothing else is migrated.

When the anchor wallet runs dry the calendar keeps accepting and
anchoring waits (one error in its log; the watcher's `calendar` check
alarms first, at five fee caps); the adapter's intake never stops. Refill
is an on-chain payment to any address of the wallet.

## What it does not do

- Nothing here states when a record was made, captured or received. The
  not-before bound says a commitment did not exist before block M; the
  anchor says it existed before block N; the calendar's clock in the
  proof is bookkeeping.
- The deep-reorg detector does not see an anchor re-mined at the same
  height after a restart, and runs only with receipts on; verifying
  anchored proofs against Bitcoin is the check that remains.
- A digest is counted as a record once per aggregator lifetime and dedupe
  horizon: resubmitted after a restart, or past the horizon, it is
  counted again ("Anchor receipts").
- A manifest chain does not show its own tail: deleting the newest days
  leaves a chain that still verifies. The cadence (a manifest is due
  every day; `seq` should reach today), off-host copies and a witness
  expose the tail, not the chain.
- A witness vouches for bytes, not for truth, and holds only what
  arrived.
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
  `test_rpc_privacy.py`: regression tests for this branch's delta
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
  (`test_watch.py`). Every durability test injects failures at the write
  boundary or kills the process; none simulates a power cut.

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
python -m unittest discover -v                       # Ran 153 tests ... OK
```

The otsd image has no pytest; where pytest is installed,
`python -m pytest otsserver/tests -q` runs the identical set. Other
platforms and Python versions have not been tried; treat any claim about
them as unverified until you run the command above.
