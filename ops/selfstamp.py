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
the fork's commit), hash-chains it to the previous day's manifest, and
stamps its sha256 through the calendar's operator lane: POST
/operator/digest, which the fork aggregates and anchors like any client
digest but never counts as a record, so the diary never reaches a receipt
or a bill. The proof rides whatever anchor real traffic pays for next; the
next run upgrades it to a Bitcoin attestation. Nothing here forces an
anchor, and nothing here is triggered by a change — the trigger is the
clock, so an anchor confirming (which appends a receipt) can never cause
a manifest.

Stdlib only. Filesystem in and out; the one non-file input is one
journalctl call. No listener. Off unless a timer runs it.

  run      --config C [--period YYYY-MM-DD]  heartbeat: write the period's
           manifest if absent, submit every manifest lacking a proof,
           upgrade every proof still pending. Idempotent.
  upgrade  --config C                        the upgrade pass alone.
  verify   --manifests DIR | --config C      offline: chain and proofs.

Manifest files live in <state_dir>/manifests/<period>.json with the proof
beside them as <period>.json.ots. The stamped digest is the sha256 of the
manifest file's bytes; the chain link is prev.sha256 = sha256 of the
previous manifest file's bytes. See the "Operator lane and self-stamp"
section of the README for the format and the limits.
"""

import argparse
import collections
import datetime
import hashlib
import json
import os
import pathlib
import shlex
import socket
import subprocess
import sys
import urllib.error
import urllib.request

SCHEMA = 'selfstamp/1'

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


# --- the operator lane -------------------------------------------------------

def submit_digest(calendar_url, digest, timeout=30):
    request = urllib.request.Request(
        calendar_url + '/operator/digest', data=digest, method='POST',
        headers={'Content-Type': 'application/octet-stream',
                 'User-Agent': 'selfstamp/1'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_timestamp(calendar_url, commitment, timeout=30):
    """The calendar's timestamp of a commitment, or None while pending"""
    request = urllib.request.Request(
        calendar_url + '/timestamp/' + commitment.hex(),
        headers={'User-Agent': 'selfstamp/1'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exp:
        if exp.code == 404:
            return None
        raise


# --- the books ---------------------------------------------------------------

def hash_file(path):
    try:
        with open(path, 'rb') as fd:
            data = fd.read()
    except FileNotFoundError:
        return {'path': path, 'missing': True}
    except OSError as exp:
        return {'path': path, 'error': '%s: %s' % (type(exp).__name__, exp)}
    return {'path': path, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}


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


# --- manifests ---------------------------------------------------------------

def load_config(path):
    with open(path) as fd:
        raw = json.load(fd)
    for key in ('state_dir', 'calendar_url', 'books'):
        if key not in raw:
            raise ValueError('config lacks %r' % key)
    cfg = {
        'state_dir': os.path.expanduser(raw['state_dir']),
        'calendar_url': raw['calendar_url'].rstrip('/'),
        'host': raw.get('host'),
        'books': {name: os.path.expanduser(p) for name, p in raw['books'].items()},
        'journal': bool(raw.get('journal', False)),
        'fork_head': os.path.expanduser(raw['fork_head']) if raw.get('fork_head') else None,
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


def latest_manifest(manifests_dir):
    """(name, raw bytes, parsed) of the newest manifest, or None"""
    files = sorted(manifests_dir.glob('*.json'))
    if not files:
        return None
    raw = files[-1].read_bytes()
    return files[-1].name, raw, json.loads(raw)


def build_manifest(cfg, period, now, manifests_dir):
    latest = latest_manifest(manifests_dir)
    if latest is None:
        seq, prev = 1, None
    else:
        name, raw, parsed = latest
        seq = parsed['seq'] + 1
        prev = {'file': name, 'sha256': hashlib.sha256(raw).hexdigest()}
    manifest = {
        'schema': SCHEMA,
        'host': cfg.get('host') or socket.gethostname(),
        'period': period.isoformat(),
        'created_at': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'seq': seq,
        'prev': prev,
        'books': {name: hash_file(path) for name, path in sorted(cfg['books'].items())},
        'journal': journal_digest(period) if cfg.get('journal') else None,
        'fork_head': git_head(cfg['fork_head']) if cfg.get('fork_head') else None,
    }
    raw = (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode()
    return manifest, raw


def _now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def _stamp():
    return _now_utc().strftime('%Y-%m-%dT%H:%M:%SZ')


def _submit_pass(cfg, manifests_dir, log):
    """Every manifest without a proof gets one — a failed submission is
    retried by the next run, never by rewriting the manifest."""
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


def run(cfg, period=None, now=None, log=None):
    """The heartbeat. Returns the process exit code."""
    log = log or print
    now = now or _now_utc()
    period = period or (now.date() - datetime.timedelta(days=1))
    manifests_dir = pathlib.Path(cfg['state_dir']) / 'manifests'
    manifests_dir.mkdir(parents=True, exist_ok=True)
    path = manifests_dir / ('%s.json' % period.isoformat())
    rc = 0
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
        manifest, raw = build_manifest(cfg, period, now, manifests_dir)
        write_atomic(path, raw)
        log('%s manifest file=%s seq=%d sha256=%s' % (
            _stamp(), path.name, manifest['seq'], hashlib.sha256(raw).hexdigest()))
    if _submit_pass(cfg, manifests_dir, log):
        rc = 1
    if _upgrade_pass(cfg, manifests_dir, log):
        rc = 1
    return rc


def upgrade(cfg, log=None):
    log = log or print
    manifests_dir = pathlib.Path(cfg['state_dir']) / 'manifests'
    return 1 if _upgrade_pass(cfg, manifests_dir, log) else 0


def verify_chain(manifests_dir, log=None):
    """Offline check a stranger can repeat with sha256sum, jq and the ots
    client: every manifest names its predecessor by file and sha256, seq
    counts from 1 without gaps, periods increase, and every proof present
    is a well-formed proof of exactly its manifest's bytes. Returns True
    when the chain and every present proof hold. A missing proof is
    reported, not a break: a submission may be a run behind. Deleting the
    newest days leaves a chain that still verifies — that limit is the
    cadence's to expose, not the chain's."""
    log = log or print
    manifests_dir = pathlib.Path(manifests_dir)
    files = sorted(manifests_dir.glob('*.json'))
    if not files:
        log('no manifests found in %s' % manifests_dir)
        return False
    ok = True
    prev_name = prev_raw = prev_period = None
    counts = {'bitcoin': 0, 'pending': 0, 'missing': 0}
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
        if manifest.get('schema') != SCHEMA:
            problems.append('schema %r' % manifest.get('schema'))
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
        ots_path = proof_path(path)
        if not ots_path.exists():
            proof = 'missing'
            counts['missing'] += 1
        else:
            try:
                parsed = parse_ots(ots_path.read_bytes())
            except OtsError as exp:
                proof = 'malformed (%s)' % exp
                ok = False
            else:
                if parsed.digest != hashlib.sha256(raw).digest():
                    proof = 'mismatch (proof is not of this file)'
                    ok = False
                elif parsed.attestation[0] == 'bitcoin':
                    proof = 'bitcoin height=%d' % parsed.attestation[1]
                    counts['bitcoin'] += 1
                elif parsed.attestation[0] == 'pending':
                    proof = 'pending calendar=%s' % parsed.attestation[1]
                    counts['pending'] += 1
                else:
                    proof = 'unknown attestation %s' % parsed.attestation[1]
                    ok = False
        if problems:
            ok = False
        log('%s seq=%s chain=%s proof=%s' % (
            path.name, manifest.get('seq'),
            'ok' if not problems else 'BROKEN ' + '; '.join(problems), proof))
        prev_name, prev_raw, prev_period = path.name, raw, period
    log('manifests=%d chain=%s proofs: bitcoin=%d pending=%d missing=%d' % (
        len(files), 'ok' if ok else 'BROKEN', counts['bitcoin'], counts['pending'],
        counts['missing']))
    return ok


# --- cli ---------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(prog='selfstamp', description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    default_config = os.path.expanduser('~/selfstamp/config.json')

    p_run = sub.add_parser('run', help='heartbeat: manifest, submit, upgrade (idempotent)')
    p_run.add_argument('--config', default=default_config)
    p_run.add_argument('--period', help='UTC day to cover, YYYY-MM-DD (default: yesterday)')

    p_up = sub.add_parser('upgrade', help='only the upgrade pass')
    p_up.add_argument('--config', default=default_config)

    p_ver = sub.add_parser('verify', help='offline chain and proof check')
    p_ver.add_argument('--config')
    p_ver.add_argument('--manifests', help='manifests directory (a stranger needs no config)')

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
    return 0 if verify_chain(manifests_dir) else 1


if __name__ == '__main__':
    sys.exit(main())
