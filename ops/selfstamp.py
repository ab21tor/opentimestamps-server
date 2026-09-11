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
the payer's ledger, the config fingerprint, a digest of that day's journal,
the fork's commit, the anchor wallet's balance, any external audit logs
the operator points it at), hash-chains it to the previous day's manifest,
and stamps its sha256 through the calendar's operator lane: POST
/operator/digest, which the fork aggregates and anchors like any client
digest but never counts as a record, so the diary never reaches a receipt
or a bill. The proof rides whatever anchor real traffic pays for next; the
next run upgrades it to a Bitcoin attestation. Nothing here forces an
anchor, and nothing here is triggered by a change — the trigger is the
clock, so an anchor confirming (which appends a receipt) can never cause
a manifest.

Witness by file drop: with an outbox configured the box exports every
manifest (and its proof, once anchored) as plain files; with an inbox
configured it consumes manifest files another box exported, keeps a copy,
stamps each copy's sha256 through its own lane, and lists each in its next
manifest as a "witnessed" entry naming the source host and seq. How files
travel between boxes (scp, a stick, a shared mount) is the operator's
business: there is no network code and no listener here.

Stdlib only. Filesystem in and out; the non-file inputs are one journalctl
call and the calendar on loopback. No listener. Off unless a timer runs it.

  run      --config C [--period YYYY-MM-DD]  heartbeat: consume the inbox,
           write the period's manifest if absent, submit every manifest
           (own or witnessed) lacking a proof, upgrade every proof still
           pending, export to the outbox. Idempotent.
  upgrade  --config C                        the upgrade pass alone.
  verify   --manifests DIR [--witnessed DIR] [--witness DIR] | --config C
           offline: chain, proofs, commissioning, what this chain vouches
           for; with --witness, whether another chain vouches for this one.

Manifest files live in <state_dir>/manifests/<period>.json with the proof
beside them as <period>.json.ots; witnessed copies in <state_dir>/witnessed/.
The stamped digest is the sha256 of the manifest file's bytes; the chain
link is prev.sha256 = sha256 of the previous manifest file's bytes. See the
"Operator lane and self-stamp" section of the README for the format and the
limits.
"""

import argparse
import collections
import datetime
import hashlib
import json
import os
import pathlib
import re
import shlex
import socket
import subprocess
import sys
import urllib.error
import urllib.request

SCHEMA = 'selfstamp/2'
# Manifests this tool reads: its own two schemas. A verify over a chain
# started under selfstamp/1 still passes; only the fields differ.
SCHEMAS = ('selfstamp/1', 'selfstamp/2')
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

Proof = collections.namedtuple('Proof', 'digest commitment attestation ops_end')


class OtsError(Exception):
    """A proof this tool cannot read or must not write"""


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
    while True:
        if pos >= len(data):
            raise OtsError('truncated varuint')
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7f) << shift
        shift += 7
        if not byte & 0x80:
            return value, pos


def read_varbytes(data, pos):
    length, pos = read_varuint(data, pos)
    if pos + length > len(data):
        raise OtsError('truncated varbytes')
    return data[pos:pos + length], pos + length


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
    while True:
        if pos >= len(data):
            raise OtsError('truncated: no attestation')
        tag = data[pos]
        if tag == ATTESTATION_MARKER:
            ops_end = pos
            pos += 1
            atag = data[pos:pos + 8]
            if len(atag) != 8:
                raise OtsError('truncated attestation tag')
            pos += 8
            payload, pos = read_varbytes(data, pos)
            if atag == PENDING_TAG:
                uri, _ = read_varbytes(payload, 0)
                attestation = ('pending', uri.decode('utf-8'))
            elif atag == BITCOIN_TAG:
                height, _ = read_varuint(payload, 0)
                attestation = ('bitcoin', height)
            else:
                attestation = ('unknown', atag.hex())
            if pos != len(data):
                raise OtsError('trailing bytes after the attestation')
            return Proof(digest, msg, attestation, ops_end)
        if tag == FORK_MARKER:
            raise OtsError('non-linear timestamp (fork marker); use the ots client')
        pos += 1
        if tag == OP_SHA256:
            msg = hashlib.sha256(msg).digest()
        elif tag == OP_APPEND:
            operand, pos = read_varbytes(data, pos)
            msg = msg + operand
        elif tag == OP_PREPEND:
            operand, pos = read_varbytes(data, pos)
            msg = operand + msg
        else:
            raise OtsError('unsupported op 0x%02x' % tag)


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
    """One word a manifest or a report can carry for an attestation"""
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
                 'User-Agent': 'selfstamp/2'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_timestamp(calendar_url, commitment, timeout=30):
    """The calendar's timestamp of a commitment, or None while pending"""
    request = urllib.request.Request(
        calendar_url + '/timestamp/' + commitment.hex(),
        headers={'User-Agent': 'selfstamp/2'})
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
    leaves the calendar. Any failure is recorded in the manifest, never
    fatal — a box whose calendar is down still writes its diary."""
    entry = {'source': 'calendar status', 'low_below_sats': low_below}
    request = urllib.request.Request(
        calendar_url + '/', headers={'Accept': 'application/json',
                                     'User-Agent': 'selfstamp/2'})
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


def _hash_open(path):
    """(sha256 hex, bytes, mtime) of the bytes the file holds now, read in
    chunks so a large audit log costs no memory; size and mtime come from
    the open descriptor, so they describe the bytes that were hashed."""
    digest = hashlib.sha256()
    size = 0
    with open(path, 'rb') as fd:
        for chunk in iter(lambda: fd.read(CHUNK), b''):
            digest.update(chunk)
            size += len(chunk)
        st = os.fstat(fd.fileno())
    return digest.hexdigest(), size, _iso(st.st_mtime)


def hash_file(path, with_mtime=False):
    try:
        digest, size, mtime = _hash_open(path)
    except FileNotFoundError:
        return {'path': path, 'missing': True}
    except OSError as exp:
        return {'path': path, 'error': '%s: %s' % (type(exp).__name__, exp)}
    entry = {'path': path, 'sha256': digest, 'bytes': size}
    if with_mtime:
        entry['mtime'] = mtime
    return entry


def audit_log_entry(configured):
    """An external audit trail, as found: a single file hashed with its
    size and mtime, or a directory hashed file by file (its regular files,
    sorted by name, not recursed). Rotation needs no rule: the files are
    hashed as they are at the run, and the next manifest shows what moved.
    A symlink is followed only if it resolves inside the configured
    directory; anything else is listed as skipped, never read."""
    if not os.path.exists(configured):
        return {'path': configured, 'missing': True}
    if not os.path.isdir(configured):
        return hash_file(configured, with_mtime=True)
    root = os.path.realpath(configured)
    try:
        names = sorted(os.listdir(configured))
    except OSError as exp:
        return {'dir': configured, 'error': '%s: %s' % (type(exp).__name__, exp)}
    files, skipped = [], []
    for name in names:
        full = os.path.join(configured, name)
        if os.path.islink(full):
            target = os.path.realpath(full)
            if target != root and not target.startswith(root + os.sep):
                skipped.append({'name': name, 'reason': 'symlink outside the configured dir'})
                continue
        if not os.path.isfile(full):
            skipped.append({'name': name, 'reason': 'not a regular file'})
            continue
        entry = hash_file(full, with_mtime=True)
        entry.pop('path', None)
        files.append(dict(name=name, **entry))
    return {'dir': configured, 'files': files, 'skipped': skipped}


def git_head(repo):
    """The checked-out commit, read from .git by file — no git call"""
    repo = pathlib.Path(repo)
    head = repo / '.git' / 'HEAD'
    if not head.exists():
        return {'path': str(repo), 'missing': True}
    text = head.read_text().strip()
    if not text.startswith('ref: '):
        return {'path': str(repo), 'ref': None, 'commit': text}
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
    return {'path': str(repo), 'ref': ref, 'commit': commit}


def journal_digest(period, journalctl=('journalctl',)):
    """sha256 of the day's journal in export format — reproducible by anyone
    with the journal, with the exact command recorded beside it"""
    since = '%s 00:00:00 UTC' % period.isoformat()
    until = '%s 00:00:00 UTC' % (period + datetime.timedelta(days=1)).isoformat()
    args = ['--since', since, '--until', until, '-o', 'export', '-q']
    result = {'since': since, 'until': until,
              'command': 'journalctl ' + ' '.join(shlex.quote(a) for a in args)}
    try:
        output = subprocess.run(list(journalctl) + args, capture_output=True,
                                check=True).stdout
    except (OSError, subprocess.CalledProcessError) as exp:
        result['error'] = '%s: %s' % (type(exp).__name__, exp)
        return result
    result['sha256'] = hashlib.sha256(output).hexdigest()
    result['bytes'] = len(output)
    return result


def config_fingerprint(cfg):
    """What the commissioning block certifies the box was configured as: the
    sha256 of the config file's bytes when there is one, else of the
    config's canonical JSON (a config handed over as a dict)."""
    path = cfg.get('config_path')
    if path:
        return {'path': path, 'sha256': hash_file(path).get('sha256')}
    public = {k: v for k, v in cfg.items() if k != 'config_path'}
    canonical = json.dumps(public, sort_keys=True, separators=(',', ':')).encode()
    return {'path': None, 'sha256': hashlib.sha256(canonical).hexdigest()}


# --- manifests ---------------------------------------------------------------

def load_config(path):
    with open(path) as fd:
        raw = json.load(fd)
    for key in ('state_dir', 'calendar_url', 'books'):
        if key not in raw:
            raise ValueError('config lacks %r' % key)
    low = raw.get('float_low_sats', FLOAT_LOW_SATS)
    if isinstance(low, bool) or not isinstance(low, int) or low < 0:
        raise ValueError('float_low_sats must be a non-negative integer')
    cfg = {
        'config_path': str(path),
        'state_dir': os.path.expanduser(raw['state_dir']),
        'calendar_url': raw['calendar_url'].rstrip('/'),
        'host': raw.get('host'),
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


def write_atomic(path, data):
    tmp = path.with_name(path.name + '.tmp')
    with open(tmp, 'wb') as fd:
        fd.write(data)
        fd.flush()
        os.fsync(fd.fileno())
    os.replace(tmp, path)


def proof_path(manifest_path):
    return manifest_path.with_name(manifest_path.name + '.ots')


def safe_name(text):
    """A host or period as a file-name component: nothing but
    [A-Za-z0-9._-], never empty, bounded."""
    cleaned = re.sub(r'[^A-Za-z0-9._-]+', '_', str(text)).strip('._-')
    return (cleaned or 'unknown')[:64]


def box_host(cfg):
    return cfg.get('host') or socket.gethostname()


def latest_manifest(manifests_dir):
    """(name, raw bytes, parsed) of the newest manifest, or None"""
    files = sorted(manifests_dir.glob('*.json'))
    if not files:
        return None
    raw = files[-1].read_bytes()
    return files[-1].name, raw, json.loads(raw)


def _folded_files(manifests_dir):
    """The witnessed copies every manifest of this chain already lists: the
    manifests are the record, so a run that stopped between writing a
    manifest and anything else re-derives the same answer."""
    folded = set()
    for path in manifests_dir.glob('*.json'):
        try:
            for entry in json.loads(path.read_bytes()).get('witnessed') or []:
                folded.add(entry.get('file'))
        except (ValueError, AttributeError):
            continue
    return folded


def witnessed_entries(witnessed_dir, manifests_dir):
    """Every witnessed copy not yet listed by a manifest of this chain, as
    the entries the next manifest folds in."""
    if not witnessed_dir.is_dir():
        return []
    folded = _folded_files(manifests_dir)
    entries = []
    for copy in sorted(witnessed_dir.glob('*.json')):
        if copy.name in folded:
            continue
        raw = copy.read_bytes()
        try:
            m = json.loads(raw)
        except ValueError:
            continue
        digest = hashlib.sha256(raw).digest()
        foreign = None
        foreign_path = copy.with_name(copy.name + '.foreign.ots')
        if foreign_path.exists():
            try:
                foreign = proof_state(_check_foreign_proof(foreign_path.read_bytes(), digest))
            except OtsError:
                foreign = 'unreadable'
        entries.append({
            'host': m.get('host'), 'seq': m.get('seq'), 'period': m.get('period'),
            'file': copy.name, 'sha256': digest.hex(),
            'witnessed_at': _iso(copy.stat().st_mtime), 'foreign_proof': foreign,
        })
    return entries


def build_manifest(cfg, period, now, manifests_dir, witnessed_dir=None):
    latest = latest_manifest(manifests_dir)
    if latest is None:
        seq, prev = 1, None
    else:
        name, raw, parsed = latest
        seq = parsed['seq'] + 1
        prev = {'file': name, 'sha256': hashlib.sha256(raw).hexdigest()}
    host = box_host(cfg)
    created_at = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    fork_head = git_head(cfg['fork_head']) if cfg.get('fork_head') else None
    audit_logs = cfg.get('audit_logs') or None
    manifest = {
        'schema': SCHEMA,
        'host': host,
        'period': period.isoformat(),
        'created_at': created_at,
        'seq': seq,
        'prev': prev,
        'books': {name: hash_file(path) for name, path in sorted(cfg['books'].items())},
        'audit_logs': ({name: audit_log_entry(path) for name, path in sorted(audit_logs.items())}
                       if audit_logs else None),
        'journal': journal_digest(period) if cfg.get('journal') else None,
        'fork_head': fork_head,
        'float': read_float(cfg['calendar_url'], cfg.get('float_low_sats', FLOAT_LOW_SATS)),
        # The commissioning certificate: what the box was when its chain
        # began. Present on the genesis manifest only.
        'commissioning': ({'host': host, 'installed_at': created_at,
                           'fork_commit': (fork_head or {}).get('commit'),
                           'config': config_fingerprint(cfg)}
                          if seq == 1 else None),
        'witnessed': (witnessed_entries(witnessed_dir, manifests_dir)
                      if witnessed_dir is not None else []),
    }
    raw = (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode()
    return manifest, raw


def _now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def _stamp():
    return _now_utc().strftime('%Y-%m-%dT%H:%M:%SZ')


# --- the witness --------------------------------------------------------------

def _check_foreign_proof(raw_ots, digest):
    """A proof that arrived beside a foreign manifest is kept only if it is
    a proof of exactly that file's bytes."""
    proof = parse_ots(raw_ots)
    if proof.digest != digest:
        raise OtsError('foreign proof is not of this manifest')
    return proof


def _parse_foreign_manifest(raw):
    m = json.loads(raw)
    if not isinstance(m, dict) or m.get('schema') not in SCHEMAS:
        raise ValueError('not a selfstamp manifest')
    host, seq, period = m.get('host'), m.get('seq'), m.get('period')
    if (not isinstance(host, str) or isinstance(seq, bool) or not isinstance(seq, int)
            or not isinstance(period, str)):
        raise ValueError('manifest lacks host, seq or period')
    return m


def _remove(path):
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def witness_inbox(cfg, witnessed_dir, log):
    """Consume the inbox: every *.json that parses as a selfstamp manifest
    is copied to witnessed/<host>-<period>-<sha12>.json (a proof that came
    beside it, if it is a proof of those bytes, to <copy>.foreign.ots), and
    the inbox file is removed. The copy is the record: the submit pass
    stamps it through the operator lane like a manifest of our own, the
    next manifest lists it, and verify re-hashes it. A duplicate (bytes
    already held) is simply removed; a file that is not a manifest goes
    to <inbox>/rejected/. Nothing is read from anywhere but the inbox."""
    inbox = cfg.get('inbox')
    if not inbox:
        return 0
    inbox = pathlib.Path(inbox)
    if not inbox.is_dir():
        log('%s inbox missing dir=%s' % (_stamp(), inbox))
        return 1
    witnessed_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(inbox.glob('*.json')):
        raw = path.read_bytes()
        companion = path.with_name(path.name + '.ots')
        try:
            m = _parse_foreign_manifest(raw)
        except (ValueError, UnicodeDecodeError) as exp:
            rejected = inbox / 'rejected'
            rejected.mkdir(exist_ok=True)
            os.replace(path, rejected / path.name)
            if companion.exists():
                os.replace(companion, rejected / companion.name)
            log('%s inbox rejected file=%s reason=%s' % (_stamp(), path.name, exp))
            continue
        digest = hashlib.sha256(raw).digest()
        name = '%s-%s-%s.json' % (safe_name(m['host']), safe_name(m['period']), digest.hex()[:12])
        copy = witnessed_dir / name
        if copy.exists() and copy.read_bytes() == raw:
            log('%s inbox duplicate file=%s already=%s' % (_stamp(), path.name, name))
            _remove(path)
            _remove(companion)
            continue
        foreign = None
        if companion.exists():
            foreign_raw = companion.read_bytes()
            try:
                foreign = proof_state(_check_foreign_proof(foreign_raw, digest))
                write_atomic(copy.with_name(name + '.foreign.ots'), foreign_raw)
            except OtsError as exp:
                log('%s inbox foreign proof rejected file=%s reason=%s'
                    % (_stamp(), companion.name, exp))
        write_atomic(copy, raw)
        _remove(path)
        _remove(companion)
        log('%s witnessed host=%s seq=%s period=%s file=%s sha256=%s foreign_proof=%s'
            % (_stamp(), m['host'], m['seq'], m['period'], name, digest.hex(), foreign))
    return 0


def _export_file(src, dst, log):
    data = src.read_bytes()
    if dst.exists() and dst.read_bytes() == data:
        return
    write_atomic(dst, data)
    log('%s exported file=%s' % (_stamp(), dst.name))


def export_outbox(cfg, manifests_dir, log):
    """Every manifest of this chain as <host>-<period>.json, and its proof
    as <host>-<period>.json.ots once (and only once) it carries a Bitcoin
    attestation — a pending proof names a loopback calendar nobody else
    can reach. Byte-identical files are left untouched."""
    outbox = cfg.get('outbox')
    if not outbox:
        return
    outbox = pathlib.Path(outbox)
    outbox.mkdir(parents=True, exist_ok=True)
    prefix = safe_name(box_host(cfg))
    for path in sorted(manifests_dir.glob('*.json')):
        _export_file(path, outbox / ('%s-%s' % (prefix, path.name)), log)
        ots = proof_path(path)
        if not ots.exists():
            continue
        try:
            anchored = parse_ots(ots.read_bytes()).attestation[0] == 'bitcoin'
        except OtsError:
            anchored = False
        if anchored:
            _export_file(ots, outbox / ('%s-%s.ots' % (prefix, path.name)), log)


# --- the passes ---------------------------------------------------------------

def _submit_pass(cfg, manifests_dir, log):
    """Every manifest (or witnessed copy) without a proof gets one — a
    failed submission is retried by the next run, never by rewriting the
    file."""
    failures = 0
    for path in sorted(manifests_dir.glob('*.json')):
        if proof_path(path).exists():
            continue
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).digest()
        try:
            response = submit_digest(cfg['calendar_url'], digest)
            ots = build_ots(digest, response)
        except Exception as exp:
            log('%s submit failed file=%s error=%r' % (_stamp(), path.name, exp))
            failures += 1
            continue
        write_atomic(proof_path(path), ots)
        proof = parse_ots(ots)
        log('%s submitted file=%s digest=%s commitment=%s calendar=%s'
            % (_stamp(), path.name, digest.hex(), proof.commitment.hex(),
               proof.attestation[1]))
    return failures


def _upgrade_pass(cfg, manifests_dir, log):
    """Every pending proof asks the calendar once; complete proofs are
    never touched or fetched again."""
    failures = 0
    for path in sorted(manifests_dir.glob('*.json.ots')):
        ots = path.read_bytes()
        try:
            proof = parse_ots(ots)
        except OtsError as exp:
            log('%s malformed file=%s error=%s' % (_stamp(), path.name, exp))
            failures += 1
            continue
        if proof.attestation[0] != 'pending':
            continue
        try:
            response = fetch_timestamp(cfg['calendar_url'], proof.commitment)
        except Exception as exp:
            log('%s upgrade failed file=%s error=%r' % (_stamp(), path.name, exp))
            failures += 1
            continue
        if response is None:
            log('%s pending file=%s commitment=%s' % (_stamp(), path.name, proof.commitment.hex()))
            continue
        try:
            upgraded = splice_upgrade(ots, response)
        except OtsError as exp:
            log('%s upgrade refused file=%s error=%s' % (_stamp(), path.name, exp))
            failures += 1
            continue
        write_atomic(path, upgraded)
        log('%s upgraded file=%s height=%d' % (_stamp(), path.name, parse_ots(upgraded).attestation[1]))
    return failures


def _dirs(cfg):
    state = pathlib.Path(cfg['state_dir'])
    return state / 'manifests', state / 'witnessed'


def run(cfg, period=None, now=None, log=None):
    """The heartbeat. Returns the process exit code."""
    log = log or print
    now = now or _now_utc()
    period = period or (now.date() - datetime.timedelta(days=1))
    manifests_dir, witnessed_dir = _dirs(cfg)
    manifests_dir.mkdir(parents=True, exist_ok=True)
    path = manifests_dir / ('%s.json' % period.isoformat())
    rc = 0
    # The inbox first, so a file dropped before the timer fires is folded
    # into this period's manifest rather than tomorrow's.
    if witness_inbox(cfg, witnessed_dir, log):
        rc = 1
    if path.exists():
        log('%s noop period=%s manifest exists' % (_stamp(), period))
    else:
        try:
            latest = latest_manifest(manifests_dir)
        except (OSError, ValueError, KeyError) as exp:
            log('%s refused period=%s latest manifest unreadable: %r' % (_stamp(), period, exp))
            return 1
        if latest is not None and latest[2].get('period', '') >= period.isoformat():
            log('%s refused period=%s not after latest manifest %s'
                % (_stamp(), period, latest[0]))
            return 1
        manifest, raw = build_manifest(cfg, period, now, manifests_dir, witnessed_dir)
        write_atomic(path, raw)
        log('%s manifest file=%s seq=%d sha256=%s witnessed=%d' % (
            _stamp(), path.name, manifest['seq'], hashlib.sha256(raw).hexdigest(),
            len(manifest['witnessed'])))
    for directory in (manifests_dir, witnessed_dir):
        if directory.is_dir() and _submit_pass(cfg, directory, log):
            rc = 1
    for directory in (manifests_dir, witnessed_dir):
        if directory.is_dir() and _upgrade_pass(cfg, directory, log):
            rc = 1
    export_outbox(cfg, manifests_dir, log)
    return rc


def upgrade(cfg, log=None):
    log = log or print
    failures = 0
    for directory in _dirs(cfg):
        if directory.is_dir():
            failures += _upgrade_pass(cfg, directory, log)
    return 1 if failures else 0


# --- verify -------------------------------------------------------------------

def _describe_proof(path, raw):
    """(text, ok, counted) for a manifest's or a copy's own proof"""
    if not path.exists():
        return 'missing', True, 'missing'
    try:
        parsed = parse_ots(path.read_bytes())
    except OtsError as exp:
        return 'malformed (%s)' % exp, False, None
    if parsed.digest != hashlib.sha256(raw).digest():
        return 'mismatch (proof is not of this file)', False, None
    if parsed.attestation[0] in ('bitcoin', 'pending'):
        return proof_state(parsed), True, parsed.attestation[0]
    return 'unknown attestation %s' % parsed.attestation[1], False, None


def _verify_witnessed(entries, witnessed_dir, log):
    """The vouches: each entry must name a copy this box holds whose bytes
    hash to the recorded sha256, and the copy's own proof (if present) must
    be a proof of those bytes. Returns False on any break."""
    ok = True
    for entry in entries:
        if not isinstance(entry, dict):
            log('  vouches for ??? BROKEN entry is not an object')
            ok = False
            continue
        name = entry.get('file') or ''
        copy_state, proof_text = 'unavailable', 'unavailable'
        if witnessed_dir is not None and witnessed_dir.is_dir():
            copy = witnessed_dir / name
            if not name or not copy.exists():
                copy_state, proof_text = 'missing', 'missing'
                ok = False
            else:
                raw = copy.read_bytes()
                if hashlib.sha256(raw).hexdigest() != entry.get('sha256'):
                    copy_state = 'MISMATCH'
                    ok = False
                else:
                    copy_state = 'ok'
                proof_text, proof_ok, _ = _describe_proof(proof_path(copy), raw)
                ok = ok and proof_ok
        log('  vouches for host=%s seq=%s period=%s sha256=%s copy=%s proof=%s foreign_proof=%s'
            % (entry.get('host'), entry.get('seq'), entry.get('period'), entry.get('sha256'),
               copy_state, proof_text, entry.get('foreign_proof')))
    return ok


def cross_check(manifests_dir, witness_dir, log):
    """Whether another chain vouches for this one: for each manifest here,
    the witness chain must list an entry for (host, seq) whose sha256 is
    the sha256 of the file as it is now. Returns False on any mismatch; a
    manifest the witness never saw is reported, not a break."""
    vouched = {}
    for path in sorted(pathlib.Path(witness_dir).glob('*.json')):
        try:
            m = json.loads(path.read_bytes())
        except ValueError:
            continue
        for entry in m.get('witnessed') or []:
            if isinstance(entry, dict):
                vouched.setdefault((entry.get('host'), entry.get('seq')), []).append(
                    (entry.get('sha256'), m.get('host'), m.get('seq'), path.name))
    ok = True
    for path in sorted(pathlib.Path(manifests_dir).glob('*.json')):
        raw = path.read_bytes()
        try:
            m = json.loads(raw)
        except ValueError:
            continue
        mine = hashlib.sha256(raw).hexdigest()
        entries = vouched.get((m.get('host'), m.get('seq')))
        if not entries:
            log('%s not witnessed by %s' % (path.name, witness_dir))
            continue
        hits = [e for e in entries if e[0] == mine]
        if hits:
            log('%s witnessed by %s seq=%s (%s)' % (path.name, hits[0][1], hits[0][2], hits[0][3]))
        else:
            ok = False
            log('%s WITNESS MISMATCH: %s holds a different hash for host=%s seq=%s (%s)'
                % (path.name, entries[0][1], m.get('host'), m.get('seq'), entries[0][3]))
    return ok


def verify_chain(manifests_dir, log=None, witnessed=None, witness=None):
    """Offline check a stranger can repeat with sha256sum, jq and the ots
    client: every manifest names its predecessor by file and sha256, seq
    counts from 1 without gaps, periods increase, every proof present is a
    well-formed proof of exactly its manifest's bytes, the genesis of a
    selfstamp/2 chain carries its commissioning block, and every witnessed
    entry names a copy this box holds that hashes as recorded (copies in
    `witnessed`, default the manifests dir's sibling). With `witness`, the
    manifests of another chain are read to say whether they vouch for this
    one. Returns True when the chain, every present proof and every vouch
    hold. A missing proof is reported, not a break: a submission may be a
    run behind. Deleting the newest days leaves a chain that still
    verifies — that limit is the cadence's (and a witness's) to expose,
    not the chain's."""
    log = log or print
    manifests_dir = pathlib.Path(manifests_dir)
    witnessed_dir = pathlib.Path(witnessed) if witnessed else manifests_dir.parent / 'witnessed'
    files = sorted(manifests_dir.glob('*.json'))
    if not files:
        log('no manifests found in %s' % manifests_dir)
        return False
    ok = True
    prev_name = prev_raw = prev_period = None
    counts = {'bitcoin': 0, 'pending': 0, 'missing': 0}
    vouches = 0
    for index, path in enumerate(files):
        raw = path.read_bytes()
        problems = []
        try:
            manifest = json.loads(raw)
            if not isinstance(manifest, dict):
                raise ValueError('not an object')
        except ValueError as exp:
            log('%s seq=? chain=BROKEN unreadable json (%s) proof=?' % (path.name, exp))
            ok = False
            prev_name, prev_raw = path.name, raw
            continue
        schema = manifest.get('schema')
        if schema not in SCHEMAS:
            problems.append('schema %r' % schema)
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
            if prev.get('sha256') != hashlib.sha256(prev_raw).hexdigest():
                problems.append('prev sha256 mismatch for %s' % prev_name)
        commissioning = manifest.get('commissioning')
        if schema == 'selfstamp/2':
            if index == 0 and not isinstance(commissioning, dict):
                problems.append('genesis lacks the commissioning block')
            if index > 0 and commissioning is not None:
                problems.append('commissioning block outside the genesis manifest')
        proof, proof_ok, counted = _describe_proof(proof_path(path), raw)
        if counted:
            counts[counted] += 1
        if not proof_ok:
            ok = False
        if problems:
            ok = False
        log('%s seq=%s chain=%s proof=%s' % (
            path.name, manifest.get('seq'),
            'ok' if not problems else 'BROKEN ' + '; '.join(problems), proof))
        if isinstance(commissioning, dict):
            log('  commissioned host=%s fork=%s config=%s at=%s' % (
                commissioning.get('host'), commissioning.get('fork_commit'),
                (commissioning.get('config') or {}).get('sha256'), commissioning.get('installed_at')))
        entries = manifest.get('witnessed') or []
        if entries:
            vouches += len(entries)
            if not _verify_witnessed(entries, witnessed_dir, log):
                ok = False
        prev_name, prev_raw, prev_period = path.name, raw, period
    log('manifests=%d chain=%s proofs: bitcoin=%d pending=%d missing=%d vouches=%d' % (
        len(files), 'ok' if ok else 'BROKEN', counts['bitcoin'], counts['pending'],
        counts['missing'], vouches))
    if witness:
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
    p_run.add_argument('--period', help='UTC day to cover, YYYY-MM-DD (default: yesterday)')

    p_up = sub.add_parser('upgrade', help='only the upgrade pass')
    p_up.add_argument('--config', default=default_config)

    p_ver = sub.add_parser('verify', help='offline chain, proof, commissioning and vouch check')
    p_ver.add_argument('--config')
    p_ver.add_argument('--manifests', help='manifests directory (a stranger needs no config)')
    p_ver.add_argument('--witnessed', help='witnessed copies directory (default: beside manifests)')
    p_ver.add_argument('--witness', help="another chain's manifests directory: does it vouch for this one?")

    args = parser.parse_args(argv)
    if args.command == 'run':
        period = datetime.date.fromisoformat(args.period) if args.period else None
        return run(load_config(args.config), period=period)
    if args.command == 'upgrade':
        return upgrade(load_config(args.config))
    if args.manifests:
        manifests_dir = args.manifests
    else:
        cfg = load_config(args.config or default_config)
        manifests_dir = os.path.join(cfg['state_dir'], 'manifests')
    return 0 if verify_chain(manifests_dir, witnessed=args.witnessed, witness=args.witness) else 1


if __name__ == '__main__':
    sys.exit(main())
