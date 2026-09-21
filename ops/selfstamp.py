#!/usr/bin/env python3
# Copyright (C) 2026 ab21tor
#
# This file is part of the OpenTimestamps Server.
#
# It is subject to the license terms in the LICENSE file found in the top-level
# directory of this distribution.
#
# No part of the OpenTimestamps Server including this file, may be copied,
# modified, propagated, or distributed except according to the terms contained
# in the LICENSE file.

"""selfstamp — the notary notarising itself.

Once a day this writes a manifest of the box's own books (anchor receipts,
the payer's ledger, a digest of that day's journal, the fork's commit, the
anchor wallet's balance, any external audit logs the operator points it
at, the fingerprint of the configuration it ran with), hash-chains it to
the previous day's manifest, and stamps its sha256 through the calendar's
operator lane: POST /operator/digest, which the fork aggregates and
anchors like any client digest but never counts as a record, so the diary
never reaches a receipt or a bill. The proof rides whatever anchor real
traffic pays for next; the next run upgrades it to a Bitcoin attestation.
Nothing here forces an anchor, and nothing here is triggered by a change —
the trigger is the clock, so an anchor confirming (which appends a
receipt) can never cause a manifest.

Witness by file drop: with an outbox configured the box exports every
manifest (and its proof, once anchored) as plain files; with an inbox
configured it consumes manifest files another box exported, keeps a copy,
stamps each copy's sha256 through its own lane, and lists each in its next
manifest as a "witnessed" entry naming the source chain and seq. How files
travel between boxes (scp, a stick, a shared mount) is the operator's
business: there is no network code and no listener here. The one rule for
the deliverer: write into the inbox under a name the tool ignores (a
leading dot, or any suffix but .json / .json.ots) and rename into place.
A name beginning with `.claim-` is the tool's own: a delivery it has taken
into its keeping before reading it, so a deliverer that replaces a name
meanwhile loses nothing.

Names. A chain is known by an opaque label: 32 hex digits drawn at random
when the chain begins (`chain`), the same on every manifest after. A book
is known by the key the operator gave it in the config. No host name, no
path and no file name of anything hashed is written into a manifest, an
export, a copy's name or a log line, because any of them can name a
client (the eight rules: amnesia of client identity). Manifests written
under selfstamp/1 and /2 named a host and carried paths; they are read,
verified, continued and witnessed unchanged, never rewritten, and what
they disclosed stays disclosed.

Stdlib only. Filesystem in and out; the non-file inputs are one journalctl
call and the calendar on loopback. No listener. Off unless a timer runs it.

  run      --config C [--period YYYY-MM-DD] [--lock-wait S]  heartbeat:
           consume the inbox, write the period's manifest if absent,
           submit every manifest (own or witnessed) lacking a proof,
           upgrade every proof still pending, export to the outbox, and
           end with one `summary` line. Exit 0 when every step of this
           run's own work is done (a proof still pending is not a
           failure: it is named in the summary and asked about next run);
           exit 1 when a step failed and is left for the next run or the
           operator (a submission or upgrade the calendar did not answer,
           an inbox file that could not be read, a proof that is not of
           the file beside it), when the period is refused, or when the
           lock could not be had. run and upgrade hold an exclusive lock
           on the state directory (<state_dir>/.lock, across processes)
           for their whole duration, so the timer's run and a manual one
           never interleave; a second one waits up to --lock-wait seconds
           (default 300), then exits 1 with 'locked'.
  upgrade  --config C [--lock-wait S]        the upgrade pass alone.
  verify   --manifests DIR [--witnessed DIR] [--witness DIR] [--skip-witnessed] | --config C
           offline: chain, proofs, what this chain vouches for; with
           --witness, whether another chain vouches for this one. Exit 0
           when the chain, every proof present and every vouch hold
           (a proof missing or pending is reported, not a break); 1 on
           any break, including a file it cannot read. A witnessed entry
           whose copy is missing is a break, unless --skip-witnessed asks
           for the partial check, which is labelled. verify needs no
           lock: every file is replaced whole, so a reader sees the old
           file or the new one.

Manifest files live in <state_dir>/manifests/<period>.json with the proof
beside them as <period>.json.ots; witnessed copies in <state_dir>/witnessed/.
The stamped digest is the sha256 of the manifest file's bytes; the chain
link is prev.sha256 = sha256 of the previous manifest file's bytes. See the
"The self-stamp" section of the README for the format and the limits, and
docs/contracts.md ("Workflow 2") for who owns unfinished work at every step.
"""

import argparse
import collections
import contextlib
import datetime
import errno
import fcntl
import hashlib
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

SCHEMA = 'selfstamp/3'
# Manifests this tool reads: its own three schemas. A verify over a chain
# started under selfstamp/1 or /2 still passes; only the fields differ.
SCHEMAS = ('selfstamp/1', 'selfstamp/2', 'selfstamp/3')
CHUNK = 65536
# The anchor wallet's "toner low" threshold: five fee caps at the shipped
# 20,000-sat cap, the same figure the gateway's float alarm and the
# watcher's CAL_MIN_SATS use.
FLOAT_LOW_SATS = 100000

# OpenTimestamps detached-proof serialization, the subset the calendar emits.
MAGIC = b'\x00OpenTimestamps\x00\x00Proof\x00\xbf\x89\xe2\xe8\x84\xe8\x92\x94'
VERSION = 1
OP_SHA256 = 0x08
OP_APPEND = 0xf0
OP_PREPEND = 0xf1
ATTESTATION_MARKER = 0x00
FORK_MARKER = 0xff
PENDING_TAG = bytes.fromhex('83dfe30d2ef90c8e')
BITCOIN_TAG = bytes.fromhex('0588960d73d71901')
# The other two block-header attestations the public client knows
# (LitecoinBlockHeaderAttestation; EthereumBlockHeaderAttestation under
# dubious/). Their payload is one varuint height, read to its end exactly
# as the client reads it; they are not usable attestations here and read
# as unknown (2026-09-18 cold review R09: they used to be opaque, so an
# empty or trailing payload the client refuses parsed).
HEIGHT_TAGS = (BITCOIN_TAG, bytes.fromhex('06869a0d73d71b45'), bytes.fromhex('30fe8087b5c7ead7'))

# The public client's limits (opentimestamps 0.4.x), mirrored so that
# "parses" means the same here as there; the corpus in ops/tests/
# proof_corpus.py holds both to them (docs/contracts.md, "The proof
# parser"). The last one is ours: nothing valid needs more than ten bytes.
MAX_OPERAND = 4096              # Op.MAX_RESULT_LENGTH: an append/prepend operand is 1..4096 bytes
MAX_MSG = 4096                  # Op.MAX_MSG_LENGTH: no message on a path is longer
MAX_ATTESTATION_PAYLOAD = 8192  # TimeAttestation.MAX_PAYLOAD_SIZE
MAX_URI = 1000                  # PendingAttestation.MAX_URI_LENGTH
URI_CHARS = frozenset(b'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._/:')
MAX_OPS_ON_A_PATH = 255         # Timestamp.deserialize's recursion limit (256 levels)
MAX_VARUINT_BYTES = 10

Proof = collections.namedtuple('Proof', 'digest commitment attestation ops_end')


class OtsError(Exception):
    """A proof this tool cannot read or must not write: the only error the
    reader raises, whatever the bytes (2026-09-15/16 review F14: a
    UnicodeDecodeError from a foreign proof's URI escaped and stopped
    every run at the same inbox file)."""


class Locked(Exception):
    """Another run or upgrade holds the state directory's lock"""


class Unstable(Exception):
    """A file changed while it was being read: no digest describes it"""


# How long a run or upgrade waits for the lock before giving up (seconds).
LOCK_WAIT = 300.0


@contextlib.contextmanager
def state_lock(state_dir, wait=LOCK_WAIT):
    """An exclusive lock over the state directory for the whole of a run or
    an upgrade, across processes (flock on <state_dir>/.lock): the timer's
    run and a manual one can no longer interleave their manifest, proof
    and export writes (2026-09-15 review: two overlapping first runs left
    a manifest with the other run's proof). Waits up to `wait` seconds for
    the holder, then raises Locked. The lock goes with the descriptor: a
    run that dies releases it. It covers this tool's processes and nothing
    else: whoever delivers files into the inbox is not under it."""
    state = pathlib.Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    path = state / '.lock'
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exp:
                if exp.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK) or time.monotonic() >= deadline:
                    raise Locked('another run holds the state directory')
                time.sleep(0.1)
        yield
    finally:
        os.close(fd)


# --- serialization -----------------------------------------------------------

def varuint(n):
    out = bytearray()
    while True:
        byte = n & 0x7f
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def read_varuint(data, pos):
    value = 0
    shift = 0
    start = pos
    while True:
        if pos >= len(data):
            raise OtsError('truncated varuint')
        if pos - start >= MAX_VARUINT_BYTES:
            raise OtsError('varuint longer than %d bytes' % MAX_VARUINT_BYTES)
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7f) << shift
        shift += 7
        if not byte & 0x80:
            return value, pos


def read_varbytes(data, pos, max_len, min_len=0):
    length, pos = read_varuint(data, pos)
    if length > max_len:
        raise OtsError('varbytes longer than %d bytes' % max_len)
    if length < min_len:
        raise OtsError('varbytes shorter than %d byte' % min_len)
    if pos + length > len(data):
        raise OtsError('truncated varbytes')
    return data[pos:pos + length], pos + length


def read_attestation(data, pos):
    """The attestation whose marker byte was just read: (kind, value, end).
    A known payload (pending, and the three block-header tags) is consumed
    to its last byte; a pending URI is at most MAX_URI bytes of URI_CHARS;
    only Bitcoin is a usable attestation; every other tag, the Litecoin
    and Ethereum ones included, is kept as ('unknown', hex)."""
    atag = data[pos:pos + 8]
    if len(atag) != 8:
        raise OtsError('truncated attestation tag')
    pos += 8
    payload, pos = read_varbytes(data, pos, MAX_ATTESTATION_PAYLOAD)
    if atag == PENDING_TAG:
        uri, end = read_varbytes(payload, 0, MAX_URI)
        if end != len(payload):
            raise OtsError('trailing bytes in the pending attestation')
        if any(b not in URI_CHARS for b in uri):
            raise OtsError('pending uri has a character outside the allowed set')
        return 'pending', uri.decode('ascii'), pos
    if atag in HEIGHT_TAGS:
        height, end = read_varuint(payload, 0)
        if end != len(payload):
            raise OtsError('trailing bytes in the block header attestation')
        if atag == BITCOIN_TAG:
            return 'bitcoin', height, pos
    return 'unknown', atag.hex(), pos


def parse_ots(data):
    """Read a linear detached proof: header, sha256 op, 32-byte digest, a
    chain of sha256/append/prepend ops, one attestation. Returns Proof with
    the message the attestation is about (the commitment) and the offset
    of the attestation marker (the splice point for an upgrade). Anything
    else — a fork marker, an op the calendar never emits, trailing bytes —
    is refused rather than guessed at.
    """
    if data[:len(MAGIC)] != MAGIC:
        raise OtsError('not an OpenTimestamps proof (bad magic)')
    pos = len(MAGIC)
    version, pos = read_varuint(data, pos)
    if version != VERSION:
        raise OtsError('unsupported proof version %d' % version)
    if pos >= len(data) or data[pos] != OP_SHA256:
        raise OtsError('file hash op is not sha256')
    pos += 1
    digest = data[pos:pos + 32]
    if len(digest) != 32:
        raise OtsError('truncated digest')
    pos += 32
    msg = digest
    ops = 0
    while True:
        if pos >= len(data):
            raise OtsError('truncated: no attestation')
        tag = data[pos]
        if tag == ATTESTATION_MARKER:
            ops_end = pos
            kind, value, pos = read_attestation(data, pos + 1)
            if pos != len(data):
                raise OtsError('trailing bytes after the attestation')
            return Proof(digest, msg, (kind, value), ops_end)
        if tag == FORK_MARKER:
            raise OtsError('non-linear timestamp (fork marker); use the ots client')
        pos += 1
        if len(msg) > MAX_MSG:
            raise OtsError('message longer than %d bytes' % MAX_MSG)
        if tag == OP_SHA256:
            msg = hashlib.sha256(msg).digest()
        elif tag == OP_APPEND:
            operand, pos = read_varbytes(data, pos, MAX_OPERAND, min_len=1)
            msg = msg + operand
        elif tag == OP_PREPEND:
            operand, pos = read_varbytes(data, pos, MAX_OPERAND, min_len=1)
            msg = operand + msg
        else:
            raise OtsError('unsupported op 0x%02x' % tag)
        if len(msg) > MAX_OPERAND:
            raise OtsError('result longer than %d bytes' % MAX_OPERAND)
        ops += 1
        if ops > MAX_OPS_ON_A_PATH:
            raise OtsError('more than %d operations on one path' % MAX_OPS_ON_A_PATH)


def build_ots(digest, calendar_response):
    """The calendar's answer to a digest POST is the serialized timestamp of
    that digest; prefixing the detached-file header makes it a .ots file,
    byte-for-byte what the ots client writes for a single calendar."""
    ots = MAGIC + varuint(VERSION) + bytes([OP_SHA256]) + digest + calendar_response
    proof = parse_ots(ots)
    if proof.digest != digest:
        raise OtsError('digest mismatch while building the proof')
    return ots


def splice_upgrade(ots, calendar_response):
    """Replace a pending attestation with the calendar's timestamp of the
    commitment (its path up the anchor tree ending in a Bitcoin
    attestation). Refused unless the result is a complete, linear proof of
    the same digest."""
    proof = parse_ots(ots)
    if proof.attestation[0] != 'pending':
        raise OtsError('proof is not pending')
    upgraded = ots[:proof.ops_end] + calendar_response
    new = parse_ots(upgraded)
    if new.attestation[0] != 'bitcoin':
        raise OtsError('upgrade response carries no Bitcoin attestation')
    if new.digest != proof.digest:
        raise OtsError('digest changed by the upgrade')
    return upgraded


def proof_state(proof):
    """One word a manifest or a report can carry for an attestation. 'bitcoin
    height=N' says a Bitcoin attestation is present in the bytes; nothing
    here checks it against the chain (docs/contracts.md, "The proof
    parser": claim 2, never claim 3)."""
    kind, value = proof.attestation
    if kind == 'bitcoin':
        return 'bitcoin height=%d' % value
    if kind == 'pending':
        return 'pending'
    return 'unknown'


# --- the operator lane and the status page -----------------------------------

def submit_digest(calendar_url, digest, timeout=30):
    request = urllib.request.Request(
        calendar_url + '/operator/digest', data=digest, method='POST',
        headers={'Content-Type': 'application/octet-stream',
                 'User-Agent': 'selfstamp/3'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_timestamp(calendar_url, commitment, timeout=30):
    """The calendar's timestamp of a commitment, or None while pending"""
    request = urllib.request.Request(
        calendar_url + '/timestamp/' + commitment.hex(),
        headers={'User-Agent': 'selfstamp/3'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exp:
        if exp.code == 404:
            return None
        raise


def parse_sats(value):
    """The status page renders sats as '22,015'; an int passes through."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).replace(',', '').strip())
    except (TypeError, ValueError):
        return None


def read_float(calendar_url, low_below, timeout=30):
    """The anchor wallet's confirmed balance from the calendar's own JSON
    status page (GET / with Accept: application/json): no RPC credential
    leaves the calendar. Any failure is recorded in the manifest as an
    error and never as a balance — an unknown float is neither zero nor
    healthy — and is never fatal: a box whose calendar is down still
    writes its diary."""
    entry = {'source': 'calendar status', 'low_below_sats': low_below}
    request = urllib.request.Request(
        calendar_url + '/', headers={'Accept': 'application/json',
                                     'User-Agent': 'selfstamp/3'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode('utf-8'))
        balance = parse_sats(data.get('balance')) if isinstance(data, dict) else None
        if balance is None:
            raise ValueError('balance unreadable')
    except Exception as exp:
        entry['error'] = '%s: %s' % (type(exp).__name__, exp)
        return entry
    entry['balance_sats'] = balance
    entry['low'] = balance < low_below
    return entry


# --- the books ---------------------------------------------------------------

def _iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _error_text(exp):
    """An OSError as a manifest or a log records it: the class and the
    errno, never the message, which names the path."""
    text = type(exp).__name__
    if getattr(exp, 'errno', None):
        text += ' errno=%d' % exp.errno
    return text


def _reason(exp):
    """Any exception as a log line records it"""
    if isinstance(exp, OSError):
        return _error_text(exp)
    return '%s: %s' % (type(exp).__name__, exp)


def _hash_open(path):
    """(sha256 hex, bytes, mtime) of a file that held still while it was
    read, in chunks so a large audit log costs no memory. Held still means:
    the path still names the same inode afterwards, and size, mtime and
    ctime from the open descriptor agree before and after the read with
    the bytes counted. Otherwise Unstable, with why. No digest describes a
    file that changed under the reader: a prefix of an append-only file is
    not that file, and a mix of two versions is nothing. Unchanged
    metadata does not prove an atomic snapshot; it is what the filesystem
    lets an observer see."""
    with open(path, 'rb') as fd:
        before = os.fstat(fd.fileno())
        digest = hashlib.sha256()
        size = 0
        for chunk in iter(lambda: fd.read(CHUNK), b''):
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(fd.fileno())
    try:
        now = os.stat(path)
    except FileNotFoundError:
        raise Unstable('removed while being read')
    if (now.st_dev, now.st_ino) != (after.st_dev, after.st_ino):
        raise Unstable('replaced while being read')
    same = ((before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            == (after.st_size, after.st_mtime_ns, after.st_ctime_ns))
    if not same or size != after.st_size:
        raise Unstable('changed while being read (%d then %d bytes)' % (before.st_size, after.st_size))
    return digest.hexdigest(), size, _iso(after.st_mtime)


def hash_file(path, with_mtime=False):
    """The manifest's entry for one file: {"sha256", "bytes"} (and "mtime"
    for an audit log), or why there is none: {"missing": true},
    {"unstable": why} or {"error": class and errno}. Never a path: the
    operator's key names the book, and a path could name a client."""
    try:
        digest, size, mtime = _hash_open(path)
    except FileNotFoundError:
        return {'missing': True}
    except Unstable as exp:
        return {'unstable': str(exp)}
    except OSError as exp:
        return {'error': _error_text(exp)}
    entry = {'sha256': digest, 'bytes': size}
    if with_mtime:
        entry['mtime'] = mtime
    return entry


def audit_log_entry(configured):
    """An external audit trail, as found: a single file hashed with its
    size and mtime, or a directory hashed file by file (its regular files,
    not recursed), the files listed by their digests and never by name: a
    file name in someone else's audit directory can name a client. Entries
    that are not regular files are counted under their reason; a symlink
    is followed only if it resolves inside the configured directory.
    Rotation needs no rule: the files are hashed as they are at the run,
    and the next manifest shows what changed. If the listing changed while
    the files were being read the entry says so (`unstable`) beside the
    files that held still; individually stable files never prove a
    coherent snapshot of a directory, and the entry does not claim one."""
    if not os.path.exists(configured):
        return {'missing': True}
    if not os.path.isdir(configured):
        return hash_file(configured, with_mtime=True)
    root = os.path.realpath(configured)
    try:
        names = sorted(os.listdir(configured))
    except OSError as exp:
        return {'error': _error_text(exp)}
    files, skipped = [], collections.Counter()
    for name in names:
        full = os.path.join(configured, name)
        if os.path.islink(full):
            target = os.path.realpath(full)
            if target != root and not target.startswith(root + os.sep):
                skipped['symlink outside the configured dir'] += 1
                continue
        if not os.path.isfile(full):
            skipped['not a regular file'] += 1
            continue
        entry = hash_file(full, with_mtime=True)
        if entry.get('missing'):
            skipped['removed while being read'] += 1
        else:
            files.append(entry)
    entry = {'files': sorted(files, key=lambda f: json.dumps(f, sort_keys=True)),
             'skipped': dict(skipped)}
    try:
        after = sorted(os.listdir(configured))
    except OSError:
        after = None
    if after != names:
        entry['unstable'] = 'directory changed while being read (%d then %s entries)' % (
            len(names), 'unknown' if after is None else len(after))
    return entry


def git_head(repo):
    """The checked-out commit, read from .git by file — no git call:
    {"ref", "commit"}, {"missing": true} or {"error": …}. The repository's
    path is the operator's and is not written."""
    repo = pathlib.Path(repo)
    head = repo / '.git' / 'HEAD'
    try:
        if not head.exists():
            return {'missing': True}
        text = head.read_text().strip()
        if not text.startswith('ref: '):
            return {'ref': None, 'commit': text}
        ref = text[len('ref: '):]
        commit = None
        ref_file = repo / '.git' / ref
        if ref_file.exists():
            commit = ref_file.read_text().strip()
        else:
            packed = repo / '.git' / 'packed-refs'
            if packed.exists():
                for line in packed.read_text().splitlines():
                    parts = line.split()
                    if len(parts) == 2 and parts[1] == ref:
                        commit = parts[0]
    except OSError as exp:
        return {'error': _error_text(exp)}
    return {'ref': ref, 'commit': commit}


def journal_digest(period, journalctl=('journalctl',)):
    """sha256 of the day's journal in export format — reproducible by anyone
    with the journal, with the exact command recorded beside it. The query
    is made at the run, whenever that is: the journal must still hold the
    day (README, "The self-stamp"). A failed query is recorded as an error,
    never as a digest."""
    since = '%s 00:00:00 UTC' % period.isoformat()
    until = '%s 00:00:00 UTC' % (period + datetime.timedelta(days=1)).isoformat()
    args = ['--since', since, '--until', until, '-o', 'export', '-q']
    result = {'since': since, 'until': until,
              'command': 'journalctl ' + ' '.join(shlex.quote(a) for a in args)}
    try:
        output = subprocess.run(list(journalctl) + args, capture_output=True,
                                check=True).stdout
    except subprocess.CalledProcessError as exp:
        result['error'] = 'journalctl exited %d' % exp.returncode
        return result
    except OSError as exp:
        result['error'] = _error_text(exp)
        return result
    result['sha256'] = hashlib.sha256(output).hexdigest()
    result['bytes'] = len(output)
    return result


def config_fingerprint(cfg):
    """sha256 of the configuration this run used: the config file's bytes
    as load_config read them, or, for a config handed over as a dict, its
    canonical JSON. Never a later reread of the file, which may have
    changed since."""
    if cfg.get('config_sha256'):
        return cfg['config_sha256']
    public = {k: v for k, v in cfg.items() if k != 'config_sha256'}
    canonical = json.dumps(public, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(canonical).hexdigest()


# --- manifests ---------------------------------------------------------------

def load_config(path):
    with open(path, 'rb') as fd:
        data = fd.read()
    raw = json.loads(data.decode('utf-8'))
    for key in ('state_dir', 'calendar_url', 'books'):
        if key not in raw:
            raise ValueError('config lacks %r' % key)
    low = raw.get('float_low_sats', FLOAT_LOW_SATS)
    if isinstance(low, bool) or not isinstance(low, int) or low < 0:
        raise ValueError('float_low_sats must be a non-negative integer')
    cfg = {
        'config_sha256': hashlib.sha256(data).hexdigest(),
        'state_dir': os.path.expanduser(raw['state_dir']),
        'calendar_url': raw['calendar_url'].rstrip('/'),
        'host': raw.get('host'),   # written by no manifest since selfstamp/3; kept so the run can say so
        'books': {name: os.path.expanduser(p) for name, p in raw['books'].items()},
        'audit_logs': {name: os.path.expanduser(p)
                       for name, p in (raw.get('audit_logs') or {}).items()} or None,
        'float_low_sats': low,
        'journal': bool(raw.get('journal', False)),
        'fork_head': os.path.expanduser(raw['fork_head']) if raw.get('fork_head') else None,
        'outbox': os.path.expanduser(raw['outbox']) if raw.get('outbox') else None,
        'inbox': os.path.expanduser(raw['inbox']) if raw.get('inbox') else None,
    }
    return cfg


def _fsync_dir(directory):
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_file(path):
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _mkdir_durable(directory):
    """mkdir -p; when the last component is new, its parent is fsynced so
    the new entry is durable too."""
    directory = pathlib.Path(directory)
    if directory.is_dir():
        return
    directory.mkdir(parents=True, exist_ok=True)
    _fsync_dir(directory.parent)


def _move_durable(src, dst):
    """A rename whose bytes and both names are durable before it returns:
    the file's data fsynced first (whoever wrote it may not have), the
    rename, then the destination directory and, when it differs, the
    source directory. Visible at the rename, durable at the last fsync;
    an fsync that fails raises after the rename, and the caller sees a
    move that is visible but not known to be durable."""
    src, dst = pathlib.Path(src), pathlib.Path(dst)
    _fsync_file(src)
    os.replace(src, dst)
    _fsync_dir(dst.parent)
    if src.parent != dst.parent:
        _fsync_dir(src.parent)


def write_atomic(path, data):
    """A unique temporary name in the target's directory (two writers can
    never share one), fsync, rename, then fsync the directory so the new
    name is durable too. The file is visible from the rename and durable
    from the directory fsync; an fsync that fails raises after the
    rename, and the caller sees a file whose durability is not known.
    The temporary name begins with a dot and ends in random characters,
    so no reader of *.json or *.json.ots ever sees it."""
    path = pathlib.Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.' + path.name + '.')
    try:
        umask = os.umask(0)
        os.umask(umask)
        os.fchmod(fd, 0o666 & ~umask)
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def proof_path(manifest_path):
    return manifest_path.with_name(manifest_path.name + '.ots')


def safe_name(text):
    """A label or period as a file-name component: nothing but
    [A-Za-z0-9._-], never empty, bounded."""
    cleaned = re.sub(r'[^A-Za-z0-9._-]+', '_', str(text)).strip('._-')
    return (cleaned or 'unknown')[:64]


def _files(directory, suffix):
    """The regular files of a directory whose names end in suffix, sorted
    by name, never one beginning with a dot: that is a temporary file
    (this tool's own, or a delivery in progress) and nobody's record.
    Missing directory: nothing. Unreadable: OSError, the caller's."""
    directory = pathlib.Path(directory)
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir()
                  if p.name.endswith(suffix) and not p.name.startswith('.') and p.is_file())


def latest_manifest(manifests_dir):
    """(name, raw bytes, parsed) of the newest manifest, or None. ValueError
    when the newest file is not a manifest: the run refuses rather than
    chains to it."""
    files = _files(manifests_dir, '.json')
    if not files:
        return None
    raw = files[-1].read_bytes()
    try:
        parsed = _parse_foreign_manifest(raw)
    except ValueError as exp:
        raise ValueError('%s is not a manifest: %s' % (files[-1].name, exp))
    return files[-1].name, raw, parsed


LABEL = re.compile(r'[0-9a-f]{32}\Z')


def _label(m):
    """A manifest's chain label (selfstamp/3): 32 hex digits, or None for a
    manifest written under an older schema, which named a host instead.
    Anything else in the field is not a label (2026-09-16 gate review,
    P4), and the validator refuses the manifest."""
    chain = m.get('chain')
    return chain if isinstance(chain, str) and LABEL.match(chain) else None


def _labelled_before(manifests_dir):
    """Whether any manifest of the chain carries a label. Asked only when
    the newest does not, which is one of two things: a chain from before
    selfstamp/3 taking its one step to a label, or a labelled chain that an
    older version of this tool has since continued. A manifest that cannot
    be read leaves the question open, and raises."""
    return any(_label(json.loads(path.read_bytes())) for path in _files(manifests_dir, '.json'))


def _identity(m):
    """What a vouch is matched under: a labelled chain by (label, seq); an
    unlabelled one by (seq, period), its host name never being republished
    by a witness. Two unlabelled chains witnessed by one box that began on
    the same day are told apart only once they have labels."""
    label = _label(m)
    if label:
        return (label, m.get('seq'), None)
    return (None, m.get('seq'), m.get('period'))


def _folded_files(manifests_dir):
    """The witnessed copies every manifest of this chain already lists: the
    manifests are the record, so a run that stopped between writing a
    manifest and anything else re-derives the same answer."""
    folded = set()
    for path in _files(manifests_dir, '.json'):
        try:
            for entry in json.loads(path.read_bytes()).get('witnessed') or []:
                folded.add(entry.get('file'))
        except (OSError, ValueError, AttributeError):
            continue
    return folded


def _foreign_state(copy, digest):
    """What the foreign proof held beside a copy says: None when none is
    held; 'pending' or 'bitcoin height=N' for a proof of the copy;
    'malformed' when the held file does not parse; 'mismatch' when it is a
    proof of other bytes; 'unreadable' when it cannot be read."""
    foreign_path = copy.with_name(copy.name + '.foreign.ots')
    if not foreign_path.exists():
        return None
    try:
        data = foreign_path.read_bytes()
    except OSError:
        return 'unreadable'
    try:
        proof = parse_ots(data)
    except OtsError:
        return 'malformed'
    if proof.digest != digest:
        return 'mismatch'
    return proof_state(proof)


def witnessed_entries(witnessed_dir, manifests_dir, log):
    """Every retained copy no manifest of this chain lists yet, as the
    entries the next manifest folds in, and the number of copies that
    could not be folded: a copy that cannot be read or no longer parses is
    named in the log, left for the next run, and counted as a failure — it
    is a record this box claims to hold."""
    if not witnessed_dir.is_dir():
        return [], 0
    folded = _folded_files(manifests_dir)
    entries, problems = [], 0
    for copy in _files(witnessed_dir, '.json'):
        if copy.name in folded:
            continue
        try:
            raw = copy.read_bytes()
            m = _parse_foreign_manifest(raw)
        except (OSError, ValueError) as exp:
            log('%s witnessed copy unreadable file=%s reason=%s' % (_stamp(), copy.name, _reason(exp)))
            problems += 1
            continue
        digest = hashlib.sha256(raw).digest()
        entries.append({
            'chain': _label(m), 'seq': m.get('seq'), 'period': m.get('period'),
            'file': copy.name, 'sha256': digest.hex(),
            'witnessed_at': _iso(copy.stat().st_mtime), 'foreign_proof': _foreign_state(copy, digest),
        })
    return entries, problems


def build_manifest(cfg, period, now, manifests_dir, witnessed=()):
    """The manifest for `period`, as a dict and as the bytes that are
    stamped: chained to the newest manifest in manifests_dir and labelled
    with its chain's label (a new one when the chain begins, or when a
    chain begun under an older schema first runs under this one), holding
    the observations made now — the books, the audit logs, the journal
    query, the fork's commit, the float — and the given witnessed entries.
    `created_at` is the run's clock: every observation here was made after
    that instant and before the file was written, in one run under the
    lock; the period names the journal day and the file, not when the
    books were looked at."""
    latest = latest_manifest(manifests_dir)
    if latest is None:
        seq, prev, chain = 1, None, None
    else:
        name, raw, parsed = latest
        seq = parsed['seq'] + 1
        prev = {'file': name, 'sha256': hashlib.sha256(raw).hexdigest()}
        chain = _label(parsed)
    audit_logs = cfg.get('audit_logs') or None
    manifest = {
        'schema': SCHEMA,
        'chain': chain or os.urandom(16).hex(),
        'period': period.isoformat(),
        'created_at': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'seq': seq,
        'prev': prev,
        'config': {'sha256': config_fingerprint(cfg)},
        'books': {name: hash_file(path) for name, path in sorted(cfg['books'].items())},
        'audit_logs': ({name: audit_log_entry(path) for name, path in sorted(audit_logs.items())}
                       if audit_logs else None),
        'journal': journal_digest(period) if cfg.get('journal') else None,
        'fork_head': git_head(cfg['fork_head']) if cfg.get('fork_head') else None,
        'float': read_float(cfg['calendar_url'], cfg.get('float_low_sats', FLOAT_LOW_SATS)),
        'witnessed': list(witnessed),
    }
    raw = (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode()
    return manifest, raw


def _now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def _stamp():
    return _now_utc().strftime('%Y-%m-%dT%H:%M:%SZ')


# --- the witness --------------------------------------------------------------

def _check_foreign_proof(raw_ots, digest):
    """A proof that arrived for a foreign manifest is kept only if it is a
    proof of exactly that file's bytes."""
    proof = parse_ots(raw_ots)
    if proof.digest != digest:
        raise OtsError('foreign proof is not of this manifest')
    return proof


def _parse_foreign_manifest(raw):
    """The one manifest validator, applied to every manifest this tool
    reads: a delivery in the inbox, its own predecessor when it continues
    a chain, and each file verify walks. The bytes must be a JSON object
    with a schema this tool reads, an integer seq from 1, a period that is
    a date, and an identity: a 32-hex chain label under selfstamp/3, a
    host name under the older schemas. Else ValueError, saying why."""
    m = json.loads(raw)
    if not isinstance(m, dict) or m.get('schema') not in SCHEMAS:
        raise ValueError('not a selfstamp manifest')
    seq, period = m.get('seq'), m.get('period')
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise ValueError('seq is not a positive integer')
    try:
        if not isinstance(period, str) or len(period) != 10:
            raise ValueError
        datetime.date.fromisoformat(period)
    except ValueError:
        raise ValueError('period is not a date')
    if m['schema'] == SCHEMA:
        if not _label(m):
            raise ValueError('chain label is not 32 hex digits')
    elif not isinstance(m.get('host'), str):
        raise ValueError('manifest lacks host')
    return m


def _remove(path):
    try:
        path.unlink()
    except FileNotFoundError:
        pass


CLAIM = '.claim-'


def _claim(inbox, log):
    """Take every delivery under a final name into this run's keeping
    before any of it is read: an atomic rename to .claim-<8 hex>-<name>.
    A deliverer that replaces the name meanwhile (the convention allows
    it) leaves its new file to the next run, instead of having it removed
    under a name this run had already read (2026-09-16 gate review, P1).
    A stop after a claim leaves the claim, and the next run resumes every
    claim whatever run made it. Returns the number of deliveries that
    could not be claimed; one taken back before the rename is nobody's
    and is skipped."""
    token = os.urandom(4).hex()
    failures = 0
    for suffix in ('.json', '.json.ots'):
        for path in _files(inbox, suffix):
            try:
                os.rename(path, path.with_name('%s%s-%s' % (CLAIM, token, path.name)))
            except FileNotFoundError:
                continue
            except OSError as exp:
                log('%s inbox error file=%s error=%s' % (_stamp(), path.name, _error_text(exp)))
                failures += 1
    return failures


def _claims(inbox, suffix):
    """(claim path, delivered name) of every claimed delivery whose name
    ends in suffix, by any run's token, in name order"""
    found = []
    for path in inbox.iterdir():
        if path.name.startswith(CLAIM) and path.name.endswith(suffix) and path.is_file():
            found.append((path, path.name.split('-', 2)[2]))
    return sorted(found, key=lambda item: (item[1], item[0].name))


def _copy_name(m, digest):
    """witnessed/<label>-<period>-<12 hex of the sha256>.json; `legacy` for
    a manifest written under an older schema, whose host name is not made
    into a file name here."""
    label = _label(m)
    return '%s-%s-%s.json' % (safe_name(label) if label else 'legacy', safe_name(m['period']), digest.hex()[:12])


def _copies_by_digest(witnessed_dir):
    """Every retained copy by the sha256 of its bytes: the duplicate check
    and a late proof's lookup are by content, whatever the copy was named
    (names changed with selfstamp/3; the older ones stay as they are). A
    copy that cannot be read is named when the next manifest is built."""
    held = {}
    for copy in _files(witnessed_dir, '.json'):
        try:
            held[hashlib.sha256(copy.read_bytes()).digest()] = copy
        except OSError:
            continue
    return held


def _quarantine(path, name, data, inbox, log, kind, reason):
    """A file the inbox cannot use is kept, never removed: moved durably
    (_move_durable) to <inbox>/rejected/<12 hex of its sha256>-<its
    delivered name>, so two bad deliveries under one name are both kept
    and the same bytes land on the same name. Logged, with why, only once
    the move is durable. `kind` is 'manifest' or 'foreign proof'."""
    rejected = inbox / 'rejected'
    _mkdir_durable(rejected)
    kept = rejected / ('%s-%s' % (hashlib.sha256(data).hexdigest()[:12], name))
    _move_durable(path, kept)
    log('%s inbox %srejected file=%s kept=%s reason=%s' % (
        _stamp(), '' if kind == 'manifest' else kind + ' ', name, kept.name, reason))


def _keep_foreign_proof(copy, data, digest, log):
    """Store a proof delivered for a copy as <copy>.foreign.ots when it is
    a proof of exactly the copy's bytes and says more than what is held:
    nothing held, or pending held and this one carrying a Bitcoin
    attestation (the source exports its proof only once anchored, so it
    normally arrives on a later pass than its manifest). What is held
    counts only if it is itself a proof of the copy: a held file that is
    not (2026-09-16 gate review, P2) is set aside beside the copy as
    <copy>.foreign.ots.rejected-<12 hex of its sha256>, logged, and
    never outranks a proof that is. Returns the state now held ('held …'
    when the delivery said less). OtsError when the bytes delivered are
    not a proof of the copy."""
    proof = _check_foreign_proof(data, digest)
    target = copy.with_name(copy.name + '.foreign.ots')
    if target.exists():
        held_data = target.read_bytes()
        try:
            held = _check_foreign_proof(held_data, digest)
        except OtsError as exp:
            aside = target.with_name(target.name + '.rejected-' + hashlib.sha256(held_data).hexdigest()[:12])
            _move_durable(target, aside)
            log('%s foreign proof set aside copy=%s kept=%s reason=%s' % (_stamp(), copy.name, aside.name, exp))
        else:
            if held.attestation[0] == 'bitcoin' or proof.attestation[0] != 'bitcoin':
                return 'held ' + proof_state(held)
    write_atomic(target, data)
    return proof_state(proof)


def _consume_companion(copy, companion, name, digest, inbox, log):
    """The proof delivered beside a manifest: kept, superseded or
    quarantined, and either way gone from the inbox. Returns the foreign
    state for the log, or None when nothing was delivered or kept."""
    if not companion.exists():
        return None
    data = companion.read_bytes()
    try:
        state = _keep_foreign_proof(copy, data, digest, log)
    except OtsError as exp:
        _quarantine(companion, name, data, inbox, log, 'foreign proof', str(exp))
        return None
    _remove(companion)
    return state


def _companion_claim(path, name, inbox):
    """The claimed companion of a claimed manifest: the one claimed beside
    it, or, when a stop claimed the two apart, the one claimed for the
    same delivered name by any run. None when nothing was delivered."""
    same = path.with_name(path.name + '.ots')
    if same.exists():
        return same
    for claim, delivered in _claims(inbox, '.json.ots'):
        if delivered == name + '.ots':
            return claim
    return None


def _consume_manifest(path, name, inbox, witnessed_dir, held, log):
    """One claimed *.json. Not a manifest: its companion, then it, are
    quarantined (the companion first, so a stop between the two leaves the
    manifest to be found again). Bytes already held, by content, whatever
    the copy's name: the companion is consumed, the claim removed. New:
    the copy is written first (it is the record), then the companion
    consumed, then the claim removed, so a stop anywhere leaves the pair
    to be found again as a duplicate. Nothing is read from anywhere but
    the inbox."""
    raw = path.read_bytes()
    companion = _companion_claim(path, name, inbox) or path.with_name(path.name + '.ots')
    try:
        m = _parse_foreign_manifest(raw)
    except ValueError as exp:
        if companion.exists():
            _quarantine(companion, name + '.ots', companion.read_bytes(), inbox, log, 'foreign proof',
                        'its manifest was rejected')
        _quarantine(path, name, raw, inbox, log, 'manifest', str(exp))
        return
    digest = hashlib.sha256(raw).digest()
    copy = held.get(digest)
    if copy is not None:
        foreign = _consume_companion(copy, companion, name + '.ots', digest, inbox, log)
        _remove(path)
        log('%s inbox duplicate file=%s already=%s foreign_proof=%s' % (_stamp(), name, copy.name, foreign))
        return
    copy = witnessed_dir / _copy_name(m, digest)
    write_atomic(copy, raw)
    held[digest] = copy
    foreign = _consume_companion(copy, companion, name + '.ots', digest, inbox, log)
    _remove(path)
    log('%s witnessed chain=%s seq=%s period=%s file=%s sha256=%s foreign_proof=%s'
        % (_stamp(), _label(m) or 'legacy', m['seq'], m['period'], copy.name, digest.hex(), foreign))


def _manifest_was_rejected(inbox, companion_name):
    """Whether the manifest delivered under this companion's name (X.json
    for X.json.ots) sits in rejected/: quarantined by an earlier pass that
    did not have the companion yet."""
    rejected = inbox / 'rejected'
    if not rejected.is_dir():
        return False
    manifest_name = '-' + companion_name[:-len('.ots')]
    return any(p.name.endswith(manifest_name) for p in rejected.iterdir())


def _consume_orphan_proof(companion, name, inbox, held, log):
    """A claimed *.json.ots with no *.json claimed beside it: the source's
    proof arriving after its manifest was consumed (or before its manifest
    arrives). Kept beside the copy it is a proof of; quarantined when it
    is not a proof, or when the manifest of its name was quarantined by an
    earlier pass; left where it is, as a claim, and named, while no copy
    matches."""
    data = companion.read_bytes()
    try:
        proof = parse_ots(data)
    except OtsError as exp:
        _quarantine(companion, name, data, inbox, log, 'foreign proof', str(exp))
        return
    copy = held.get(proof.digest)
    if copy is None:
        if _manifest_was_rejected(inbox, name):
            _quarantine(companion, name, data, inbox, log, 'foreign proof', 'its manifest was rejected')
            return
        log('%s inbox proof awaiting its manifest file=%s' % (_stamp(), name))
        return
    state = _keep_foreign_proof(copy, data, proof.digest, log)
    _remove(companion)
    log('%s inbox foreign proof file=%s copy=%s foreign_proof=%s' % (_stamp(), name, copy.name, state))


def witness_inbox(cfg, witnessed_dir, log):
    """Consume the inbox: every delivery under a final name is claimed
    (_claim), then every claimed *.json that is a selfstamp manifest is
    copied to witnessed/ (_consume_manifest) and every claimed *.json.ots
    left alone is matched to the copy it proves (_consume_orphan_proof).
    The copy is the record: the submit pass stamps it through the
    operator lane like a manifest of our own, the next manifest lists it,
    and verify re-hashes it. The removals are made durable before the
    pass returns. Returns the number of files this run could not deal
    with and leaves for the next one; a rejection is not a failure, and
    one bad or unreadable file never stops the others or the heartbeat.
    No message here names a path: the operator knows the configured
    directories, and a path can name a client."""
    inbox = cfg.get('inbox')
    if not inbox:
        return 0
    inbox = pathlib.Path(inbox)
    if not inbox.is_dir():
        log('%s inbox missing' % _stamp())
        return 1
    try:
        _mkdir_durable(witnessed_dir)
    except OSError as exp:
        log('%s witnessed directory unusable error=%s' % (_stamp(), _error_text(exp)))
        return 1
    try:
        failures = _claim(inbox, log)
        manifests = _claims(inbox, '.json')
    except OSError as exp:
        log('%s inbox unreadable error=%s' % (_stamp(), _error_text(exp)))
        return 1
    held = _copies_by_digest(witnessed_dir)
    for path, name in manifests:
        try:
            _consume_manifest(path, name, inbox, witnessed_dir, held, log)
        except OSError as exp:
            log('%s inbox error file=%s error=%s' % (_stamp(), name, _error_text(exp)))
            failures += 1
    # The proofs left alone are listed only now: the pass above consumed
    # the companions of the manifests it dealt with.
    try:
        companions = _claims(inbox, '.json.ots')
    except OSError as exp:
        log('%s inbox unreadable error=%s' % (_stamp(), _error_text(exp)))
        return failures + 1
    for path, name in companions:
        if path.with_name(path.name[:-len('.ots')]).exists():
            continue   # its manifest's claim is still there (a failure above): next run
        try:
            _consume_orphan_proof(path, name, inbox, held, log)
        except OSError as exp:
            log('%s inbox error file=%s error=%s' % (_stamp(), name, _error_text(exp)))
            failures += 1
    try:
        _fsync_dir(inbox)   # the removals, durable before the pass is over
    except OSError as exp:
        log('%s inbox error fsync error=%s' % (_stamp(), _error_text(exp)))
        failures += 1
    return failures


def _export_file(src, dst, log):
    data = src.read_bytes()
    if dst.exists() and dst.read_bytes() == data:
        return
    write_atomic(dst, data)
    log('%s exported file=%s' % (_stamp(), dst.name))


def export_outbox(cfg, manifests_dir, log):
    """Every manifest of this chain as <label>-<period>.json, and its proof
    as <label>-<period>.json.ots once (and only once) it is a proof of the
    manifest's bytes carrying a Bitcoin attestation — a pending proof
    names a loopback calendar nobody else can reach, and a proof of other
    bytes is withheld and named in the log. A manifest written under an
    older schema keeps the <host>-<period> name its export already has.
    Byte-identical files are left untouched. Returns the number of
    manifests that could not be exported."""
    outbox = cfg.get('outbox')
    if not outbox:
        return 0
    outbox = pathlib.Path(outbox)
    failures = 0
    try:
        _mkdir_durable(outbox)
    except OSError as exp:
        log('%s export failed error=%s' % (_stamp(), _error_text(exp)))
        return 1
    for path in _files(manifests_dir, '.json'):
        try:
            raw = path.read_bytes()
            m = _parse_foreign_manifest(raw)
            prefix = _label(m) or safe_name(m.get('host'))
            _export_file(path, outbox / ('%s-%s' % (prefix, path.name)), log)
            ots = proof_path(path)
            if not ots.exists():
                continue
            # The companion is a proof of exactly this manifest's bytes with
            # a Bitcoin attestation, or it is not published (2026-09-16 gate
            # review, P2: attestation presence alone let a proof of other
            # bytes travel as this manifest's).
            text, state = _describe_proof(ots, raw)
            if state == 'bitcoin':
                _export_file(ots, outbox / ('%s-%s.ots' % (prefix, path.name)), log)
            elif state != 'pending':
                log('%s export withheld file=%s proof=%s' % (_stamp(), ots.name, text))
        except (OSError, ValueError) as exp:
            log('%s export failed file=%s error=%s' % (_stamp(), path.name, _reason(exp)))
            failures += 1
    return failures


# --- the passes ---------------------------------------------------------------

def _submit_pass(cfg, directory, log):
    """Every manifest (or witnessed copy) without a proof gets one — a
    failed submission is retried by the next run, never by rewriting the
    file. A response that never arrives after the calendar committed the
    digest is the same case: the next run submits the same digest again,
    and the calendar dedupes it or anchors it twice (contracts, C1)."""
    failures = 0
    for path in _files(directory, '.json'):
        if proof_path(path).exists():
            continue
        try:
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).digest()
            response = submit_digest(cfg['calendar_url'], digest)
            ots = build_ots(digest, response)
            write_atomic(proof_path(path), ots)
        except Exception as exp:
            log('%s submit failed file=%s error=%s' % (_stamp(), path.name, _reason(exp)))
            failures += 1
            continue
        proof = parse_ots(ots)
        log('%s submitted file=%s digest=%s commitment=%s calendar=%s'
            % (_stamp(), path.name, digest.hex(), proof.commitment.hex(),
               proof.attestation[1]))
    return failures


GOOD_PROOF_STATES = ('missing', 'pending', 'bitcoin')


def _describe_proof(path, raw):
    """(text, state) for the proof beside a manifest or a copy: missing;
    pending; bitcoin (an attestation is present — not checked against
    Bitcoin here); malformed (this reader cannot parse it); mismatch (a
    proof, but not of this file); unknown (an attestation this tool does
    not read); unreadable (the file cannot be read)."""
    if not path.exists():
        return 'missing', 'missing'
    try:
        data = path.read_bytes()
    except OSError as exp:
        return 'unreadable (%s)' % _error_text(exp), 'unreadable'
    try:
        parsed = parse_ots(data)
    except OtsError as exp:
        return 'malformed (%s)' % exp, 'malformed'
    if parsed.digest != hashlib.sha256(raw).digest():
        return 'mismatch (proof is not of this file)', 'mismatch'
    if parsed.attestation[0] in ('bitcoin', 'pending'):
        return proof_state(parsed), parsed.attestation[0]
    return 'unknown attestation %s' % parsed.attestation[1], 'unknown'


def _upgrade_pass(cfg, directory, log):
    """Every pending proof asks the calendar once; complete proofs are
    never touched or fetched again. A proof this reader cannot parse, or
    one that is not of the file beside it, is reported every run, counted
    as a failure, and never touched: 'malformed' is this reader's verdict
    (a proof upgraded by the ots client can carry a fork it refuses), and
    only the operator can say what the file should be (README, "Recover")."""
    failures = 0
    for path in _files(directory, '.json.ots'):
        manifest = path.with_name(path.name[:-len('.ots')])
        try:
            raw = manifest.read_bytes()
        except FileNotFoundError:
            log('%s orphan proof file=%s (no manifest beside it)' % (_stamp(), path.name))
            failures += 1
            continue
        except OSError as exp:
            log('%s upgrade failed file=%s error=%s' % (_stamp(), path.name, _error_text(exp)))
            failures += 1
            continue
        text, state = _describe_proof(path, raw)
        if state not in GOOD_PROOF_STATES:
            log('%s %s file=%s %s' % (_stamp(), state, path.name, text))
            failures += 1
            continue
        if state != 'pending':
            continue
        try:
            ots = path.read_bytes()
            proof = parse_ots(ots)
            response = fetch_timestamp(cfg['calendar_url'], proof.commitment)
        except Exception as exp:
            log('%s upgrade failed file=%s error=%s' % (_stamp(), path.name, _reason(exp)))
            failures += 1
            continue
        if response is None:
            log('%s pending file=%s commitment=%s' % (_stamp(), path.name, proof.commitment.hex()))
            continue
        try:
            upgraded = splice_upgrade(ots, response)
            write_atomic(path, upgraded)
        except (OtsError, OSError) as exp:
            log('%s upgrade refused file=%s error=%s' % (_stamp(), path.name, _reason(exp)))
            failures += 1
            continue
        log('%s upgraded file=%s height=%d' % (_stamp(), path.name, parse_ots(upgraded).attestation[1]))
    return failures


def _dirs(cfg):
    state = pathlib.Path(cfg['state_dir'])
    return state / 'manifests', state / 'witnessed'


def _summary(manifests_dir, witnessed_dir):
    """What is held and what is outstanding, counted fresh from the files:
    the run's exit code is about this run's work, this line is about the
    proofs, and the two are read apart."""
    counts = collections.Counter()
    sizes = {}
    for key, directory in (('manifests', manifests_dir), ('witnessed', witnessed_dir)):
        files = _files(directory, '.json')
        sizes[key] = len(files)
        for path in files:
            try:
                raw = path.read_bytes()
            except OSError:
                counts['unreadable'] += 1
                continue
            counts[_describe_proof(proof_path(path), raw)[1]] += 1
    return ('manifests=%d witnessed=%d proofs: bitcoin=%d pending=%d missing=%d malformed=%d mismatch=%d other=%d'
            % (sizes['manifests'], sizes['witnessed'], counts['bitcoin'], counts['pending'], counts['missing'],
               counts['malformed'], counts['mismatch'], counts['unknown'] + counts['unreadable']))


def run(cfg, period=None, now=None, log=None, lock_wait=LOCK_WAIT):
    """The heartbeat, under the state directory's lock. Returns the process
    exit code: 1 when the lock could not be had within lock_wait seconds
    (logged 'locked'), else the passes' verdict (module docstring)."""
    log = log or print
    try:
        with state_lock(cfg['state_dir'], lock_wait):
            return _run_locked(cfg, period, now, log)
    except Locked as exp:
        log('%s locked %s' % (_stamp(), exp))
        return 1


def _run_locked(cfg, period, now, log):
    now = now or _now_utc()
    period = period or (now.date() - datetime.timedelta(days=1))
    if period >= now.date():
        log('%s refused period=%s not finished: the UTC day must be over' % (_stamp(), period))
        return 1
    manifests_dir, witnessed_dir = _dirs(cfg)
    try:
        _mkdir_durable(manifests_dir)
    except OSError as exp:
        log('%s refused period=%s state directory unusable error=%s' % (_stamp(), period, _error_text(exp)))
        return 1
    if cfg.get('host'):
        log('%s config host is not written to manifests since selfstamp/3 (a chain is named by its label); remove the key'
            % _stamp())
    # The inbox first, so a file dropped before the timer fires is folded
    # into this period's manifest rather than tomorrow's.
    failures = witness_inbox(cfg, witnessed_dir, log)
    path = manifests_dir / ('%s.json' % period.isoformat())
    if path.exists():
        log('%s noop period=%s manifest exists' % (_stamp(), period))
    else:
        try:
            latest = latest_manifest(manifests_dir)
        except (OSError, ValueError) as exp:
            log('%s refused period=%s latest manifest unreadable: %s' % (_stamp(), period, _reason(exp)))
            return 1
        if latest is not None and latest[2].get('period', '') >= period.isoformat():
            log('%s refused period=%s not after latest manifest %s'
                % (_stamp(), period, latest[0]))
            return 1
        if latest is not None and _label(latest[2]) is None:
            # The label is drawn once. An unlabelled newest manifest gets the
            # chain its label now only if the chain never had one.
            try:
                labelled = _labelled_before(manifests_dir)
            except (OSError, ValueError, AttributeError) as exp:
                log('%s refused period=%s an earlier manifest unreadable, so whether the chain has its label is not '
                    'known: %s' % (_stamp(), period, _reason(exp)))
                return 1
            if labelled:
                log('%s refused period=%s the chain has a label and its newest manifest %s has none: an older version '
                    'of this tool wrote it, and a second label is never drawn. Move the manifests that version wrote, '
                    'and their proofs, out of manifests/ (keep them): the chain goes on from its last labelled '
                    'manifest and those days are a gap' % (_stamp(), period, latest[0]))
                return 1
        entries, problems = witnessed_entries(witnessed_dir, manifests_dir, log)
        failures += problems
        manifest, raw = build_manifest(cfg, period, now, manifests_dir, witnessed=entries)
        try:
            write_atomic(path, raw)
        except OSError as exp:
            # Visible if the rename happened, durable only if the fsync did:
            # the next run takes a file it finds as the record.
            log('%s manifest write failed file=%s error=%s' % (_stamp(), path.name, _error_text(exp)))
            return 1
        log('%s manifest file=%s seq=%d chain=%s sha256=%s witnessed=%d' % (
            _stamp(), path.name, manifest['seq'], manifest['chain'], hashlib.sha256(raw).hexdigest(),
            len(manifest['witnessed'])))
    for directory in (manifests_dir, witnessed_dir):
        failures += _submit_pass(cfg, directory, log)
    for directory in (manifests_dir, witnessed_dir):
        failures += _upgrade_pass(cfg, directory, log)
    failures += export_outbox(cfg, manifests_dir, log)
    log('%s summary %s failures=%d' % (_stamp(), _summary(manifests_dir, witnessed_dir), failures))
    return 1 if failures else 0


def upgrade(cfg, log=None, lock_wait=LOCK_WAIT):
    log = log or print
    try:
        with state_lock(cfg['state_dir'], lock_wait):
            failures = 0
            for directory in _dirs(cfg):
                failures += _upgrade_pass(cfg, directory, log)
            return 1 if failures else 0
    except Locked as exp:
        log('%s locked %s' % (_stamp(), exp))
        return 1


# --- verify -------------------------------------------------------------------

def _verify_witnessed(entries, witnessed_dir, log, skip_copies=False):
    """The vouches: each entry must name a copy this box holds whose bytes
    hash to the recorded sha256, and the copy's own proof (if present) must
    be a proof of those bytes. A copy that is not there (the file, or the
    whole directory) or cannot be read is a break: the vouch is for bytes
    this box claims to hold (2026-09-15 review: an absent directory used to
    pass). With skip_copies the copies are not looked for at all and every
    vouch is labelled SKIPPED. Each line names the copy's state (ok,
    missing, unreadable, MISMATCH, SKIPPED), this box's own proof of it,
    what the foreign proof said when the entry was written, and what is
    held now. Returns False on any break."""
    ok = True
    for entry in entries:
        if not isinstance(entry, dict):
            log('  vouches for ??? BROKEN entry is not an object')
            ok = False
            continue
        name = entry.get('file') or ''
        host = entry.get('host')   # written by selfstamp/1 and /2 witnesses: read, never rewritten
        foreign_now = None
        if skip_copies:
            copy_state, proof_text = 'SKIPPED', 'SKIPPED'
        else:
            copy = witnessed_dir / name if name else None
            raw = None
            if copy is None or not copy.is_file():
                copy_state, proof_text = 'missing', 'missing'
                ok = False
            else:
                try:
                    raw = copy.read_bytes()
                except OSError:
                    copy_state, proof_text = 'unreadable', '?'
                    ok = False
            if raw is not None:
                digest = hashlib.sha256(raw).digest()
                if digest.hex() != entry.get('sha256'):
                    copy_state = 'MISMATCH'
                    ok = False
                else:
                    copy_state = 'ok'
                proof_text, state = _describe_proof(proof_path(copy), raw)
                ok = ok and state in GOOD_PROOF_STATES
                foreign_now = _foreign_state(copy, digest)
        log('  vouches for chain=%s seq=%s period=%s sha256=%s copy=%s proof=%s foreign_proof=%s%s%s'
            % (entry.get('chain') or '-', entry.get('seq'), entry.get('period'), entry.get('sha256'),
               copy_state, proof_text, entry.get('foreign_proof'),
               ' foreign_now=%s' % foreign_now if foreign_now is not None else '',
               ' host=%s' % host if host else ''))
    return ok


def cross_check(manifests_dir, witness_dir, log):
    """Whether another chain vouches for this one. For each manifest here,
    an entry in the other chain with the same sha256 is `witnessed by`.
    Failing that, an entry under the same identity with another hash is
    WITNESS MISMATCH, a break, when the identity is a label and seq (a
    label names one chain); when it is seq and period alone (a manifest
    written under an older schema, whose host name a witness does not
    republish) it is WITNESS AMBIGUOUS, reported and not a break: another
    version of this manifest, or another unlabelled chain that began the
    same day, and only a label tells them apart. No entry is `not
    witnessed`, reported. A witness manifest that cannot be read or is
    not a manifest is named and makes the check incomplete: the other
    manifests are still checked, and the result is False, because an
    absence found in evidence that could not all be read is not
    established (2026-09-16 gate review, P3). A witness directory that is
    not there is a break: nothing was checked. The last line sums it up.
    No message names a path."""
    witness_dir = pathlib.Path(witness_dir)
    if not witness_dir.is_dir():
        log('witness manifests directory missing: nothing was checked')
        return False
    try:
        files = _files(witness_dir, '.json')
    except OSError as exp:
        log('witness manifests directory unreadable (%s): nothing was checked' % _error_text(exp))
        return False
    vouched = {}
    unreadable = 0
    for path in files:
        try:
            m = _parse_foreign_manifest(path.read_bytes())
        except (OSError, ValueError) as exp:
            log('witness manifest unreadable: %s (%s)' % (path.name, _reason(exp)))
            unreadable += 1
            continue
        for entry in m.get('witnessed') or []:
            if isinstance(entry, dict):
                vouched.setdefault(_identity(entry), []).append(
                    (entry.get('sha256'), _label(m) or m.get('host'), m.get('seq'), path.name))
    counts = collections.Counter()
    ok = unreadable == 0
    for path in _files(manifests_dir, '.json'):
        try:
            raw = path.read_bytes()
            m = _parse_foreign_manifest(raw)
        except (OSError, ValueError):
            continue   # verify_chain reports it
        mine = hashlib.sha256(raw).hexdigest()
        entries = vouched.get(_identity(m), [])
        hits = [e for e in entries if e[0] == mine]
        versions = len({e[0] for e in entries})
        if hits:
            counts['witnessed'] += 1
            log('%s witnessed by %s seq=%s (%s)%s' % (
                path.name, hits[0][1], hits[0][2], hits[0][3],
                ' (%d versions held for this seq)' % versions if versions > 1 else ''))
        elif entries and _label(m):
            counts['mismatch'] += 1
            ok = False
            log('%s WITNESS MISMATCH: %s holds a different hash for seq=%s (%s)'
                % (path.name, entries[0][1], m.get('seq'), entries[0][3]))
        elif entries:
            counts['ambiguous'] += 1
            log('%s WITNESS AMBIGUOUS: %s holds %d hash(es) for an unlabelled chain at seq=%s period=%s and none is '
                'this file\'s: another version of it, or another chain that began the same day; a label would tell'
                % (path.name, entries[0][1], versions, m.get('seq'), m.get('period')))
        else:
            counts['not_witnessed'] += 1
            log('%s not witnessed' % path.name)
    verdict = 'ok' if ok else ('BROKEN' if counts['mismatch'] else 'incomplete')
    log('witness check=%s witnessed=%d mismatch=%d ambiguous=%d not_witnessed=%d unreadable=%d%s' % (
        verdict, counts['witnessed'], counts['mismatch'], counts['ambiguous'], counts['not_witnessed'], unreadable,
        '; an absence here is not established' if unreadable else ''))
    return ok


def verify_chain(manifests_dir, log=None, witnessed=None, witness=None, skip_witnessed=False):
    """Offline check a stranger can repeat with sha256sum, jq and the ots
    client: every manifest names its predecessor by file and sha256, seq
    counts from 1 without gaps, periods increase, the chain label (once a
    manifest has one) never changes, a selfstamp/2 genesis carries its
    commissioning block, every proof present is a well-formed proof of
    exactly its manifest's bytes, and every witnessed entry names a copy
    this box holds that hashes as recorded (copies in `witnessed`, default
    the manifests dir's sibling); a copy that is not there is a break,
    unless `skip_witnessed` asks for the partial check, which every vouch
    line and the summary then label. With `witness`, the manifests of
    another chain are read to say whether they vouch for this one. Returns
    True when the chain, every present proof and every vouch hold. A
    missing or pending proof is reported, not a break: a submission may be
    a run behind, an anchor not yet made. A file that cannot be read is a
    break. Deleting the newest days leaves a chain that still verifies —
    that limit is the cadence's (and a witness's) to expose, not the
    chain's. An attestation present is not checked against Bitcoin here."""
    log = log or print
    manifests_dir = pathlib.Path(manifests_dir)
    witnessed_dir = pathlib.Path(witnessed) if witnessed else manifests_dir.parent / 'witnessed'
    try:
        files = _files(manifests_dir, '.json')
    except OSError as exp:
        log('manifests directory unreadable (%s)' % _error_text(exp))
        return False
    if not files:
        log('no manifests found')
        return False
    ok = True
    prev_name = prev_raw = prev_period = None
    chain_label = None
    counts = collections.Counter()
    vouches = 0
    for index, path in enumerate(files):   # oldest to newest
        try:
            raw = path.read_bytes()
        except OSError as exp:
            log('%s seq=? chain=BROKEN unreadable (%s) proof=?' % (path.name, _error_text(exp)))
            ok = False
            prev_name, prev_raw = path.name, None
            continue
        problems = []
        try:
            manifest = _parse_foreign_manifest(raw)
        except ValueError as exp:
            # The one validator's verdict; what can still be checked of a
            # JSON object that failed it is checked below.
            try:
                manifest = json.loads(raw)
            except ValueError:
                manifest = None
            if not isinstance(manifest, dict):
                log('%s seq=? chain=BROKEN unreadable json (%s) proof=?' % (path.name, exp))
                ok = False
                prev_name, prev_raw = path.name, raw
                continue
            problems.append(str(exp))
        schema = manifest.get('schema')
        if manifest.get('seq') != index + 1:
            problems.append('seq %r expected %d' % (manifest.get('seq'), index + 1))
        period = manifest.get('period')
        if period != path.name[:-len('.json')]:
            problems.append('period %r does not match the file name' % period)
        if prev_period is not None and not (isinstance(period, str) and period > prev_period):
            problems.append('period not after the previous manifest')
        prev = manifest.get('prev')
        if prev_name is None:
            if prev is not None:
                problems.append('genesis manifest has a prev link')
        elif not isinstance(prev, dict):
            problems.append('prev link missing')
        else:
            if prev.get('file') != prev_name:
                problems.append('prev file %r expected %r' % (prev.get('file'), prev_name))
            if prev_raw is None:
                problems.append('prev sha256 unverifiable: %s unreadable' % prev_name)
            elif prev.get('sha256') != hashlib.sha256(prev_raw).hexdigest():
                problems.append('prev sha256 mismatch for %s' % prev_name)
        label = _label(manifest)
        if label:
            if chain_label is None:
                chain_label = label
            elif label != chain_label:
                problems.append('chain label changed')
        elif chain_label is not None:
            problems.append('chain label missing after a labelled manifest')
        commissioning = manifest.get('commissioning')
        if schema == 'selfstamp/2':
            if index == 0 and not isinstance(commissioning, dict):
                problems.append('genesis lacks the commissioning block')
            if index > 0 and commissioning is not None:
                problems.append('commissioning block outside the genesis manifest')
        proof_text, state = _describe_proof(proof_path(path), raw)
        counts[state] += 1
        if state not in GOOD_PROOF_STATES:
            ok = False
        if problems:
            ok = False
        log('%s seq=%s chain=%s proof=%s' % (
            path.name, manifest.get('seq'),
            'ok' if not problems else 'BROKEN ' + '; '.join(problems), proof_text))
        if index == 0 and schema == 'selfstamp/3' and label:
            # The commissioning record: what the chain was when it began.
            # created_at is the box's own clock; the genesis proof's block
            # is the proven bound; nothing states when the software was
            # installed.
            log('  genesis chain=%s at=%s fork=%s config=%s' % (
                label, manifest.get('created_at'), (manifest.get('fork_head') or {}).get('commit'),
                (manifest.get('config') or {}).get('sha256')))
        if isinstance(commissioning, dict):
            log('  commissioned host=%s fork=%s config=%s at=%s' % (
                commissioning.get('host'), commissioning.get('fork_commit'),
                (commissioning.get('config') or {}).get('sha256'), commissioning.get('installed_at')))
        entries = manifest.get('witnessed') or []
        if entries:
            vouches += len(entries)
            if not _verify_witnessed(entries, witnessed_dir, log, skip_copies=skip_witnessed):
                ok = False
        prev_name, prev_raw, prev_period = path.name, raw, period
    bad = ''.join(' %s=%d' % (key, counts[key]) for key in ('malformed', 'mismatch', 'unknown', 'unreadable') if counts[key])
    log('manifests=%d chain=%s proofs: bitcoin=%d pending=%d missing=%d%s vouches=%d%s' % (
        len(files), 'ok' if ok else 'BROKEN', counts['bitcoin'], counts['pending'],
        counts['missing'], bad, vouches, ' (copies not checked: --skip-witnessed)' if skip_witnessed and vouches else ''))
    log('an attestation present is not checked against Bitcoin here: verify_claim.py, or the ots client against a node, does that')
    if witness:
        # Its own last line says how the witness check ended; a break or
        # an incomplete check there is exit 1 like any other.
        if not cross_check(manifests_dir, witness, log):
            ok = False
    return ok


# --- cli ---------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(prog='selfstamp', description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    default_config = os.path.expanduser('~/selfstamp/config.json')

    p_run = sub.add_parser('run', help='heartbeat: inbox, manifest, submit, upgrade, outbox (idempotent)')
    p_run.add_argument('--config', default=default_config)
    p_run.add_argument('--period', help='UTC day to cover, YYYY-MM-DD (default: yesterday; a day not yet over is refused)')
    p_run.add_argument('--lock-wait', type=float, default=LOCK_WAIT,
                       help='seconds to wait for the state directory lock (default %(default)s)')

    p_up = sub.add_parser('upgrade', help='only the upgrade pass')
    p_up.add_argument('--config', default=default_config)
    p_up.add_argument('--lock-wait', type=float, default=LOCK_WAIT,
                      help='seconds to wait for the state directory lock (default %(default)s)')

    p_ver = sub.add_parser('verify', help='offline chain, proof and vouch check')
    p_ver.add_argument('--config')
    p_ver.add_argument('--manifests', help='manifests directory (a stranger needs no config)')
    p_ver.add_argument('--witnessed', help='witnessed copies directory (default: beside manifests)')
    p_ver.add_argument('--witness', help="another chain's manifests directory: does it vouch for this one?")
    p_ver.add_argument('--skip-witnessed', action='store_true',
                       help='partial check: do not look for the witnessed copies (every vouch is labelled SKIPPED)')

    args = parser.parse_args(argv)
    if args.command == 'run':
        period = datetime.date.fromisoformat(args.period) if args.period else None
        return run(load_config(args.config), period=period, lock_wait=args.lock_wait)
    if args.command == 'upgrade':
        return upgrade(load_config(args.config), lock_wait=args.lock_wait)
    if args.manifests:
        manifests_dir = args.manifests
    else:
        cfg = load_config(args.config or default_config)
        manifests_dir = os.path.join(cfg['state_dir'], 'manifests')
    return 0 if verify_chain(manifests_dir, witnessed=args.witnessed, witness=args.witness,
                             skip_witnessed=args.skip_witnessed) else 1


if __name__ == '__main__':
    sys.exit(main())
