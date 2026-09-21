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

"""ops/selfstamp.py — the notary notarising itself.

The tool is stdlib-only and lives outside the otsserver package, so it is
loaded here by path. Everything external is faked: a stdlib HTTP server
plays the calendar's operator lane (POST /operator/digest, GET
/timestamp/<hex>), books are files in a tmpdir, the journal digest is
disabled or fed by a fake journalctl. No network, no bitcoind, no real
calendar. Where the opentimestamps library is importable (it is a
dependency of this server) the hand-rolled .ots bytes are cross-checked by
deserializing them with it.

Pinned properties: the .ots layout; the manifest fields and the chain
link; heartbeat idempotency (one manifest per period, ever); the causal
tail (an anchor confirming — a receipts append — and a proof upgrade never
trigger a manifest); tamper detection (altered or deleted history fails
verify); the operator lane is the only door used.
"""

import contextlib
import datetime
import hashlib
import http.server
import importlib.util
import io
import json
import os
import pathlib
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

TOOL = pathlib.Path(__file__).resolve().parents[2] / 'ops' / 'selfstamp.py'


def load_tool():
    spec = importlib.util.spec_from_file_location('selfstamp', TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


selfstamp = load_tool()

PENDING_TAG = bytes.fromhex('83dfe30d2ef90c8e')
BITCOIN_TAG = bytes.fromhex('0588960d73d71901')
MAGIC = b'\x00OpenTimestamps\x00\x00Proof\x00\xbf\x89\xe2\xe8\x84\xe8\x92\x94'


def vu(n):
    """Independent varuint encoder so the tool's is checked, not echoed"""
    out = bytearray()
    while True:
        byte = n & 0x7f
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def vb(b):
    return vu(len(b)) + b


NONCE = b'\x11' * 16
IDX = struct.pack('>L', 1700000000)
MAC = b'\x22' * 8
SIBLING = b'\x33' * 32
URI = 'http://fakecalendar.onion/'


def pending_response(digest):
    """What the calendar answers to POST: append nonce, sha256, prepend idx,
    append mac, pending attestation. Returns (bytes, commitment)."""
    commitment = IDX + hashlib.sha256(digest + NONCE).digest() + MAC
    body = (b'\xf0' + vb(NONCE) + b'\x08' + b'\xf1' + vb(IDX) + b'\xf0' + vb(MAC)
            + b'\x00' + PENDING_TAG + vb(vb(URI.encode())))
    return body, commitment


def bitcoin_response(height):
    """What GET /timestamp/<commitment> answers once anchored"""
    return b'\xf0' + vb(SIBLING) + b'\x08' + b'\x00' + BITCOIN_TAG + vb(vu(height))


class FakeCalendar:
    """A stdlib HTTP server speaking just enough of the calendar protocol"""

    def __init__(self):
        self.known = set()
        self.mined_height = None
        self.operator_posts = 0
        self.client_posts = 0
        self.gets = 0            # GET /timestamp/<hex> only
        self.status_gets = 0     # GET / (the JSON status page)
        self.balance_sats = 212015   # None -> the status answers 500
        # Fault switches (test_selfstamp_workflow): a POST whose digest is
        # committed but whose response never arrives; a timestamp answer
        # cut short. Each counts down to zero.
        self.drop_post_responses = 0
        self.truncate_timestamp_responses = 0
        cal = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, code, body, ctype='application/octet-stream'):
                self.send_response(code)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                digest = self.rfile.read(int(self.headers['Content-Length']))
                if self.path == '/operator/digest':
                    cal.operator_posts += 1
                elif self.path == '/digest':
                    cal.client_posts += 1
                else:
                    return self._reply(404, b'not found', 'text/plain')
                body, commitment = pending_response(digest)
                cal.known.add(commitment)
                if cal.drop_post_responses > 0:
                    # Committed, never acknowledged: the connection closes
                    # without a response (contracts S4, ambiguous outcome).
                    cal.drop_post_responses -= 1
                    self.close_connection = True
                    return
                self._reply(200, body)

            def do_GET(self):
                if self.path == '/':
                    # The status line (rpc.py get_status). The real one
                    # sends balance as an int; the old page rendered it with
                    # commas, and the float reader still accepts both.
                    cal.status_gets += 1
                    if cal.balance_sats is None:
                        return self._reply(500, b'status failed', 'text/plain')
                    body = json.dumps({'best_block': '00' * 32, 'anchor_receipts': 'on',
                                       'balance': '{:,}'.format(cal.balance_sats),
                                       'pending_commitments': '3', 'most_recent_tx': 'None'})
                    return self._reply(200, body.encode(), 'application/json')
                cal.gets += 1
                if not self.path.startswith('/timestamp/'):
                    return self._reply(404, b'not found', 'text/plain')
                commitment = bytes.fromhex(self.path[len('/timestamp/'):])
                if commitment not in cal.known:
                    return self._reply(404, b'Not found', 'text/plain')
                if cal.mined_height is None:
                    return self._reply(404, b'Pending confirmation in Bitcoin blockchain',
                                       'text/plain')
                body = bitcoin_response(cal.mined_height)
                if cal.truncate_timestamp_responses > 0:
                    cal.truncate_timestamp_responses -= 1
                    body = body[:len(body) // 2]
                self._reply(200, body)

        self.server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        self.url = 'http://127.0.0.1:%d' % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


DIGEST = hashlib.sha256(b'a manifest').digest()


class Test_ots_encoding(unittest.TestCase):
    def test_varuint_matches_an_independent_encoder(self):
        for n in (0, 1, 127, 128, 255, 300, 16383, 16384, 2 ** 32, 2 ** 40 + 7):
            self.assertEqual(selfstamp.varuint(n), vu(n), n)
            value, pos = selfstamp.read_varuint(vu(n) + b'tail', 0)
            self.assertEqual((value, pos), (n, len(vu(n))))

    def test_build_ots_layout_and_parse_back(self):
        response, commitment = pending_response(DIGEST)
        ots = selfstamp.build_ots(DIGEST, response)
        self.assertEqual(ots, MAGIC + b'\x01' + b'\x08' + DIGEST + response)
        proof = selfstamp.parse_ots(ots)
        self.assertEqual(proof.digest, DIGEST)
        self.assertEqual(proof.commitment, commitment)
        self.assertEqual(proof.attestation, ('pending', URI))
        # ops_end is where the attestation marker sits: splice point for upgrade.
        self.assertEqual(ots[proof.ops_end], 0x00)
        self.assertEqual(ots[proof.ops_end + 1:proof.ops_end + 9], PENDING_TAG)

    def test_upgraded_proof_parses_to_a_bitcoin_attestation(self):
        response, _ = pending_response(DIGEST)
        ots = selfstamp.build_ots(DIGEST, response)
        upgraded = selfstamp.splice_upgrade(ots, bitcoin_response(965432))
        proof = selfstamp.parse_ots(upgraded)
        self.assertEqual(proof.digest, DIGEST)
        self.assertEqual(proof.attestation, ('bitcoin', 965432))
        # Splicing a response with no Bitcoin attestation is refused.
        with self.assertRaises(selfstamp.OtsError):
            selfstamp.splice_upgrade(ots, response)

    def test_parse_rejects_bad_magic_forks_and_truncation(self):
        response, _ = pending_response(DIGEST)
        ots = selfstamp.build_ots(DIGEST, response)
        with self.assertRaises(selfstamp.OtsError):
            selfstamp.parse_ots(b'\x01' + ots[1:])
        with self.assertRaises(selfstamp.OtsError):
            selfstamp.parse_ots(ots[:-3])
        # A fork marker (\xff) means a non-linear timestamp: out of scope,
        # refused rather than guessed at.
        forked = ots[:len(MAGIC) + 2 + 32] + b'\xff' + ots[len(MAGIC) + 2 + 32:]
        with self.assertRaises(selfstamp.OtsError):
            selfstamp.parse_ots(forked)

    def test_cross_check_with_the_opentimestamps_library(self):
        try:
            from opentimestamps.core.serialize import BytesDeserializationContext
            from opentimestamps.core.timestamp import DetachedTimestampFile
            from opentimestamps.core.notary import (PendingAttestation,
                                                    BitcoinBlockHeaderAttestation)
            from opentimestamps.core.op import OpSHA256
        except ImportError:  # pragma: no cover - library absent
            self.skipTest('opentimestamps library not importable')
        response, commitment = pending_response(DIGEST)
        ots = selfstamp.build_ots(DIGEST, response)
        detached = DetachedTimestampFile.deserialize(BytesDeserializationContext(ots))
        self.assertEqual(detached.file_hash_op, OpSHA256())
        self.assertEqual(detached.timestamp.msg, DIGEST)
        attestations = list(detached.timestamp.all_attestations())
        self.assertEqual(len(attestations), 1)
        msg, attestation = attestations[0]
        self.assertEqual(msg, commitment)
        self.assertEqual(attestation, PendingAttestation(URI))

        upgraded = selfstamp.splice_upgrade(ots, bitcoin_response(965432))
        detached = DetachedTimestampFile.deserialize(BytesDeserializationContext(upgraded))
        attestations = list(detached.timestamp.all_attestations())
        self.assertEqual(len(attestations), 1)
        self.assertEqual(attestations[0][1], BitcoinBlockHeaderAttestation(965432))
        self.assertEqual(attestations[0][0],
                         hashlib.sha256(commitment + SIBLING).digest())


def sha256_hex(path):
    with open(path, 'rb') as fd:
        return hashlib.sha256(fd.read()).hexdigest()


class SelfstampCase(unittest.TestCase):
    """A tmpdir state dir with three books and a fake calendar"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = pathlib.Path(self.tmpdir.name)
        self.state = self.root / 'selfstamp'
        self.books = self.root / 'books'
        self.books.mkdir()
        (self.books / 'anchor-receipts.jsonl').write_text(
            '{"txid": "aa", "records": 3}\n')
        (self.books / 'payer.log').write_text('state: nothing_due\n')
        (self.books / 'compose.yml').write_text('services: {}\n')
        self.calendar = FakeCalendar()
        self.addCleanup(self.calendar.close)
        self.cfg = {
            'state_dir': str(self.state),
            'calendar_url': self.calendar.url,
            'books': {
                'receipts': str(self.books / 'anchor-receipts.jsonl'),
                'payer_log': str(self.books / 'payer.log'),
                'compose': str(self.books / 'compose.yml'),
                'absent': str(self.books / 'not-there'),
            },
            'journal': False,
            'fork_head': None,
        }
        self.log = []

    def run_tool(self, period, **kwargs):
        return selfstamp.run(self.cfg, period=period, log=self.log.append, **kwargs)

    def manifests(self):
        return sorted(p.name for p in (self.state / 'manifests').glob('*.json'))

    def manifest(self, name):
        return json.loads((self.state / 'manifests' / name).read_text())


P1 = datetime.date(2026, 9, 1)
P2 = datetime.date(2026, 9, 2)
P3 = datetime.date(2026, 9, 3)


class Test_run_heartbeat(SelfstampCase):
    def test_first_run_writes_a_genesis_manifest_and_a_pending_proof(self):
        rc = self.run_tool(P1, now=datetime.datetime(2026, 9, 2, 0, 30, 7,
                                                     tzinfo=datetime.timezone.utc))
        self.assertEqual(rc, 0, self.log)
        self.assertEqual(self.manifests(), ['2026-09-01.json'])
        m = self.manifest('2026-09-01.json')
        self.assertEqual(m['schema'], 'selfstamp/3')
        self.assertRegex(m['chain'], r'^[0-9a-f]{32}$')
        self.assertNotIn('host', m)
        self.assertEqual(m['period'], '2026-09-01')
        self.assertEqual(m['created_at'], '2026-09-02T00:30:07Z')
        self.assertEqual(m['seq'], 1)
        self.assertIsNone(m['prev'])
        self.assertIsNone(m['journal'])
        self.assertIsNone(m['fork_head'])
        self.assertEqual(set(m['config']), {'sha256'})
        self.assertEqual(set(m['books']), {'receipts', 'payer_log', 'compose', 'absent'})
        self.assertEqual(m['books']['receipts'],
                         {'sha256': sha256_hex(self.books / 'anchor-receipts.jsonl'), 'bytes': 29})
        self.assertEqual(m['books']['absent'], {'missing': True})
        # Sorted keys, two-space indent, trailing newline: the bytes a
        # stranger re-hashes are exactly what json.dumps would give them.
        raw = (self.state / 'manifests' / '2026-09-01.json').read_bytes()
        self.assertEqual(raw, (json.dumps(m, sort_keys=True, indent=2) + '\n').encode())

        proof = selfstamp.parse_ots((self.state / 'manifests' / '2026-09-01.json.ots').read_bytes())
        self.assertEqual(proof.digest, hashlib.sha256(raw).digest())
        self.assertEqual(proof.attestation, ('pending', URI))
        # Only the operator lane was used, exactly once.
        self.assertEqual((self.calendar.operator_posts, self.calendar.client_posts), (1, 0))

    def test_second_run_in_the_same_period_changes_nothing(self):
        self.run_tool(P1)
        before = (self.state / 'manifests' / '2026-09-01.json').read_bytes()
        rc = self.run_tool(P1)
        self.assertEqual(rc, 0)
        self.assertEqual(self.manifests(), ['2026-09-01.json'])
        self.assertEqual((self.state / 'manifests' / '2026-09-01.json').read_bytes(), before)
        self.assertEqual(self.calendar.operator_posts, 1)
        self.assertTrue(any('noop' in line for line in self.log), self.log)

    def test_default_period_is_the_previous_utc_day(self):
        now = datetime.datetime(2026, 9, 4, 0, 30, tzinfo=datetime.timezone.utc)
        self.run_tool(None, now=now)
        self.assertEqual(self.manifests(), ['2026-09-03.json'])

    def test_an_anchor_confirming_does_not_trigger_a_manifest(self):
        """Causal tail: the receipts file changing (what an anchor
        confirmation does — including the anchor of this very manifest)
        must not produce a manifest; only the next period's run does, and
        that one records the new receipts hash."""
        self.run_tool(P1)
        with open(self.books / 'anchor-receipts.jsonl', 'a') as fd:
            fd.write('{"txid": "bb", "records": 0}\n')
        self.run_tool(P1)
        self.assertEqual(self.manifests(), ['2026-09-01.json'])
        self.assertEqual(self.calendar.operator_posts, 1)

        self.run_tool(P2)
        self.assertEqual(self.manifests(), ['2026-09-01.json', '2026-09-02.json'])
        m1 = self.manifest('2026-09-01.json')
        m2 = self.manifest('2026-09-02.json')
        self.assertEqual(m2['seq'], 2)
        self.assertEqual(m2['prev'], {'file': '2026-09-01.json',
                                      'sha256': sha256_hex(self.state / 'manifests' / '2026-09-01.json')})
        self.assertNotEqual(m1['books']['receipts']['sha256'], m2['books']['receipts']['sha256'])

    def test_upgrade_writes_only_the_proof(self):
        self.run_tool(P1)
        ots_path = self.state / 'manifests' / '2026-09-01.json.ots'
        pending = ots_path.read_bytes()
        manifest_before = (self.state / 'manifests' / '2026-09-01.json').read_bytes()

        # Still pending at the calendar: the file is byte-identical after a run.
        self.run_tool(P1)
        self.assertEqual(ots_path.read_bytes(), pending)

        self.calendar.mined_height = 965500
        rc = self.run_tool(P1)
        self.assertEqual(rc, 0, self.log)
        proof = selfstamp.parse_ots(ots_path.read_bytes())
        self.assertEqual(proof.attestation, ('bitcoin', 965500))
        self.assertEqual(proof.digest, hashlib.sha256(manifest_before).digest())
        self.assertEqual(self.manifests(), ['2026-09-01.json'])
        self.assertEqual((self.state / 'manifests' / '2026-09-01.json').read_bytes(), manifest_before)
        self.assertEqual(self.calendar.operator_posts, 1)
        # A complete proof is never fetched again.
        gets = self.calendar.gets
        self.run_tool(P1)
        self.assertEqual(self.calendar.gets, gets)

    def test_a_manifest_without_a_proof_is_resubmitted_not_rewritten(self):
        self.run_tool(P1)
        manifest_path = self.state / 'manifests' / '2026-09-01.json'
        before = manifest_path.read_bytes()
        (self.state / 'manifests' / '2026-09-01.json.ots').unlink()
        rc = self.run_tool(P1)
        self.assertEqual(rc, 0)
        self.assertEqual(manifest_path.read_bytes(), before)
        self.assertTrue((self.state / 'manifests' / '2026-09-01.json.ots').exists())
        self.assertEqual(self.calendar.operator_posts, 2)

    def test_calendar_down_keeps_the_manifest_and_exits_nonzero(self):
        self.calendar.close()
        rc = self.run_tool(P1)
        self.assertEqual(rc, 1)
        self.assertEqual(self.manifests(), ['2026-09-01.json'])
        self.assertFalse((self.state / 'manifests' / '2026-09-01.json.ots').exists())
        self.assertTrue(any('submit failed' in line for line in self.log), self.log)


class Test_verify_chain(SelfstampCase):
    def setUp(self):
        super().setUp()
        for period in (P1, P2, P3):
            with open(self.books / 'payer.log', 'a') as fd:
                fd.write('poll %s\n' % period)
            self.assertEqual(self.run_tool(period), 0)
        self.dir = self.state / 'manifests'

    def verify(self):
        rows = []
        ok = selfstamp.verify_chain(self.dir, log=rows.append)
        return ok, '\n'.join(rows)

    def test_intact_chain_verifies(self):
        ok, report = self.verify()
        self.assertTrue(ok, report)
        self.assertEqual(self.manifests(), ['2026-09-01.json', '2026-09-02.json', '2026-09-03.json'])
        self.assertEqual([self.manifest(n)['seq'] for n in self.manifests()], [1, 2, 3])
        self.assertIn('pending', report)

    def test_an_altered_byte_in_history_is_detected(self):
        path = self.dir / '2026-09-02.json'
        raw = path.read_bytes()
        tampered = raw.replace(b'"schema": "selfstamp/3"', b'"schema": "selfstamp/2"')
        self.assertNotEqual(raw, tampered)
        path.write_bytes(tampered)

        ok, report = self.verify()
        self.assertFalse(ok, report)
        # Its successor's link breaks and its own proof no longer matches.
        self.assertIn('2026-09-03.json', report)
        self.assertIn('prev', report)
        self.assertIn('mismatch', report)

        path.write_bytes(raw)
        ok, report = self.verify()
        self.assertTrue(ok, report)

    def test_a_deleted_day_in_the_middle_is_detected(self):
        moved = {}
        for name in ('2026-09-02.json', '2026-09-02.json.ots'):
            moved[name] = (self.dir / name).read_bytes()
            (self.dir / name).unlink()

        ok, report = self.verify()
        self.assertFalse(ok, report)
        self.assertIn('2026-09-02.json', report)

        for name, data in moved.items():
            (self.dir / name).write_bytes(data)
        ok, report = self.verify()
        self.assertTrue(ok, report)

    def test_a_reordered_or_spliced_tail_is_detected(self):
        # A forged tail manifest whose prev points at the wrong file.
        m = self.manifest('2026-09-03.json')
        m['prev']['file'] = '2026-09-01.json'
        (self.dir / '2026-09-03.json').write_text(json.dumps(m, sort_keys=True, indent=2) + '\n')
        ok, report = self.verify()
        self.assertFalse(ok, report)

    def test_missing_proof_is_reported_but_is_not_a_chain_break(self):
        (self.dir / '2026-09-03.json.ots').unlink()
        ok, report = self.verify()
        self.assertTrue(ok, report)
        self.assertIn('missing', report)

    def test_cli_verify_exit_codes(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(selfstamp.main(['verify', '--manifests', str(self.dir)]), 0)
            (self.dir / '2026-09-02.json').write_bytes(b'{}\n')
            self.assertEqual(selfstamp.main(['verify', '--manifests', str(self.dir)]), 1)


class Test_inputs(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = pathlib.Path(self.tmpdir.name)

    def test_journal_digest_pins_the_journalctl_command(self):
        fake = self.root / 'journalctl'
        fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        result = selfstamp.journal_digest(datetime.date(2026, 9, 3), journalctl=(str(fake),))
        args = ['--since', '2026-09-03 00:00:00 UTC', '--until', '2026-09-04 00:00:00 UTC',
                '-o', 'export', '-q']
        expected = ('\n'.join(args) + '\n').encode()
        self.assertEqual(result, {
            'since': '2026-09-03 00:00:00 UTC',
            'until': '2026-09-04 00:00:00 UTC',
            'command': "journalctl --since '2026-09-03 00:00:00 UTC' "
                       "--until '2026-09-04 00:00:00 UTC' -o export -q",
            'sha256': hashlib.sha256(expected).hexdigest(),
            'bytes': len(expected),
        })

    def test_fork_head_reads_git_by_file_not_by_command(self):
        repo = self.root / 'repo'
        (repo / '.git' / 'refs' / 'heads').mkdir(parents=True)
        (repo / '.git' / 'HEAD').write_text('ref: refs/heads/calendar-ops\n')
        (repo / '.git' / 'refs' / 'heads' / 'calendar-ops').write_text('0d1c80f90dcb003eb43a0045fb3cdb643fdf2f34\n')
        self.assertEqual(selfstamp.git_head(repo),
                         {'ref': 'refs/heads/calendar-ops', 'commit': '0d1c80f90dcb003eb43a0045fb3cdb643fdf2f34'})
        (repo / '.git' / 'HEAD').write_text('0d1c80f90dcb003eb43a0045fb3cdb643fdf2f34\n')
        self.assertEqual(selfstamp.git_head(repo)['ref'], None)
        self.assertEqual(selfstamp.git_head(self.root / 'nowhere'), {'missing': True})

    def test_load_config_expands_home_and_fills_defaults(self):
        cfg_path = self.root / 'config.json'
        cfg_path.write_text(json.dumps({
            'state_dir': '~/selfstamp',
            'calendar_url': 'http://127.0.0.1:14788/',
            'books': {'receipts': '~/gateway/receipts/anchor-receipts.jsonl'},
        }))
        cfg = selfstamp.load_config(cfg_path)
        self.assertEqual(cfg['state_dir'], os.path.expanduser('~/selfstamp'))
        self.assertEqual(cfg['calendar_url'], 'http://127.0.0.1:14788')  # no trailing slash
        self.assertEqual(cfg['books']['receipts'],
                         os.path.expanduser('~/gateway/receipts/anchor-receipts.jsonl'))
        self.assertEqual(cfg['journal'], False)
        self.assertIsNone(cfg['fork_head'])
        self.assertIsNone(cfg['host'])
        self.assertEqual(cfg['config_sha256'], sha256_hex(cfg_path))


def iso_mtime(path):
    return datetime.datetime.fromtimestamp(os.stat(path).st_mtime,
                                           datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class Test_commissioning(SelfstampCase):
    """Item 13 of the 2026-09-11 green review, as selfstamp/3 keeps it: the
    genesis manifest is the commissioning record. It carries what every
    manifest carries — the chain label, the fork's commit, the fingerprint
    of the configuration the run used — and its created_at is the box's
    own clock; the proven bound is its proof's block. A selfstamp/2
    genesis carried a `commissioning` block with a host name and an
    `installed_at`; that block is still read (test_selfstamp_workflow,
    the legacy corpus) and no longer written."""

    def fake_repo(self, commit):
        repo = self.root / 'repo'
        (repo / '.git' / 'refs' / 'heads').mkdir(parents=True)
        (repo / '.git' / 'HEAD').write_text('ref: refs/heads/calendar-ops\n')
        (repo / '.git' / 'refs' / 'heads' / 'calendar-ops').write_text(commit + '\n')
        return repo

    def test_the_genesis_names_the_fork_and_the_config_and_verify_prints_it(self):
        commit = '1d0fe48e589b86fb1f14299f813d0fa5d87de102'
        self.cfg['fork_head'] = str(self.fake_repo(commit))
        now = datetime.datetime(2026, 9, 2, 0, 30, 7, tzinfo=datetime.timezone.utc)
        self.assertEqual(self.run_tool(P1, now=now), 0, self.log)
        m = self.manifest('2026-09-01.json')
        self.assertEqual(m['schema'], 'selfstamp/3')
        self.assertNotIn('commissioning', m)
        self.assertNotIn('installed_at', json.dumps(m))
        # A config passed as a dict has no file: its fingerprint is the
        # sha256 of its canonical JSON (a file config hashes the file).
        expected_cfg = hashlib.sha256(json.dumps(
            self.cfg, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(m['config'], {'sha256': expected_cfg})
        self.assertEqual(m['fork_head'], {'ref': 'refs/heads/calendar-ops', 'commit': commit})
        self.assertEqual(self.run_tool(P2), 0, self.log)
        self.assertEqual(self.manifest('2026-09-02.json')['fork_head']['commit'], commit)
        rows = []
        self.assertTrue(selfstamp.verify_chain(self.state / 'manifests', log=rows.append), rows)
        self.assertTrue(any(r.strip().startswith('genesis chain=%s' % m['chain']) and commit[:12] in r
                            and 'at=2026-09-02T00:30:07Z' in r for r in rows), rows)
        self.assertEqual(sum(r.strip().startswith('genesis') for r in rows), 1, rows)

    def test_the_config_fingerprint_is_the_file_when_there_is_one(self):
        cfg_path = self.root / 'config.json'
        cfg_path.write_text(json.dumps(self.cfg, indent=1))
        cfg = selfstamp.load_config(cfg_path)
        self.assertEqual(cfg['config_sha256'], sha256_hex(cfg_path))
        self.assertEqual(selfstamp.run(cfg, period=P1, log=self.log.append), 0, self.log)
        m = self.manifest('2026-09-01.json')
        self.assertEqual(m['config'], {'sha256': sha256_hex(cfg_path)})
        self.assertIsNone(m['fork_head'])


class Test_float(SelfstampCase):
    """Item 7: the anchor wallet's balance, read from the calendar's own
    status page, so a witness sees "toner low" in the chain."""

    def test_manifest_records_the_balance_and_the_low_flag(self):
        self.calendar.balance_sats = 212015
        self.assertEqual(self.run_tool(P1), 0, self.log)
        self.assertEqual(self.manifest('2026-09-01.json')['float'], {
            'source': 'calendar status', 'balance_sats': 212015,
            'low_below_sats': 100000, 'low': False})
        self.calendar.balance_sats = 4000
        self.assertEqual(self.run_tool(P2), 0, self.log)
        self.assertEqual(self.manifest('2026-09-02.json')['float'], {
            'source': 'calendar status', 'balance_sats': 4000,
            'low_below_sats': 100000, 'low': True})
        self.cfg['float_low_sats'] = 3000
        self.assertEqual(self.run_tool(P3), 0, self.log)
        self.assertEqual(self.manifest('2026-09-03.json')['float'],
                         {'source': 'calendar status', 'balance_sats': 4000,
                          'low_below_sats': 3000, 'low': False})
        # One status GET per manifest built; a noop run reads nothing.
        self.assertEqual(self.calendar.status_gets, 3)
        self.run_tool(P3)
        self.assertEqual(self.calendar.status_gets, 3)

    def test_an_unreadable_balance_is_recorded_not_fatal(self):
        self.calendar.balance_sats = None
        self.assertEqual(self.run_tool(P1), 0, self.log)
        f = self.manifest('2026-09-01.json')['float']
        self.assertEqual(f['source'], 'calendar status')
        self.assertEqual(f['low_below_sats'], 100000)
        self.assertIn('error', f)
        self.assertNotIn('balance_sats', f)
        self.assertNotIn('low', f)


class Test_audit_logs(SelfstampCase):
    """Item 5: books that are large and rotating (a lab's own audit-trail
    export): hashed in chunks, file by file, with size and mtime, symlinks
    never followed outside the configured directory."""

    def setUp(self):
        super().setUp()
        self.lims = self.root / 'lims'
        self.lims.mkdir()
        self.big = os.urandom(3 * 65536 + 17)
        (self.lims / 'audit.log').write_bytes(self.big)
        (self.lims / 'audit.log.1').write_bytes(b'rotated\n')
        (self.lims / 'archive').mkdir()

    def test_a_directory_is_hashed_file_by_file_with_size_and_mtime(self):
        self.cfg['audit_logs'] = {'lims': str(self.lims)}
        self.assertEqual(self.run_tool(P1), 0, self.log)
        entry = self.manifest('2026-09-01.json')['audit_logs']['lims']
        self.assertEqual(set(entry), {'files', 'skipped'}, 'no directory path, no file names')
        big = {'sha256': hashlib.sha256(self.big).hexdigest(), 'bytes': len(self.big),
               'mtime': iso_mtime(self.lims / 'audit.log')}
        rotated = {'sha256': hashlib.sha256(b'rotated\n').hexdigest(), 'bytes': 8,
                   'mtime': iso_mtime(self.lims / 'audit.log.1')}
        self.assertEqual(entry['files'], sorted([big, rotated], key=lambda f: json.dumps(f, sort_keys=True)))
        self.assertEqual(entry['skipped'], {'not a regular file': 1})

    def test_symlinks_outside_the_directory_are_never_followed(self):
        secret = self.root / 'secret.txt'
        secret.write_bytes(b'the box must never hash this\n')
        (self.lims / 'evil').symlink_to(secret)
        (self.lims / 'inside-link').symlink_to(self.lims / 'audit.log')
        self.cfg['audit_logs'] = {'lims': str(self.lims)}
        self.assertEqual(self.run_tool(P1), 0, self.log)
        entry = self.manifest('2026-09-01.json')['audit_logs']['lims']
        self.assertEqual(entry['skipped'], {'symlink outside the configured dir': 1, 'not a regular file': 1})
        digests = [f['sha256'] for f in entry['files']]
        self.assertEqual(digests.count(hashlib.sha256(self.big).hexdigest()), 2, 'audit.log and the inside link')
        raw = (self.state / 'manifests' / '2026-09-01.json').read_bytes()
        self.assertNotIn(hashlib.sha256(secret.read_bytes()).hexdigest().encode(), raw)
        self.assertNotIn(b'evil', raw)
        self.assertNotIn(b'inside-link', raw)

    def test_a_single_file_and_a_missing_path(self):
        self.cfg['audit_logs'] = {'one': str(self.lims / 'audit.log.1'),
                                  'gone': str(self.lims / 'nowhere')}
        self.assertEqual(self.run_tool(P1), 0, self.log)
        logs = self.manifest('2026-09-01.json')['audit_logs']
        self.assertEqual(logs['one'], {
            'sha256': hashlib.sha256(b'rotated\n').hexdigest(),
            'bytes': 8, 'mtime': iso_mtime(self.lims / 'audit.log.1')})
        self.assertEqual(logs['gone'], {'missing': True})

    def test_unconfigured_is_null_and_books_stay_as_they_were(self):
        self.assertEqual(self.run_tool(P1), 0, self.log)
        m = self.manifest('2026-09-01.json')
        self.assertIsNone(m['audit_logs'])
        self.assertEqual(set(m['books']['receipts']), {'sha256', 'bytes'})


class Test_witness(SelfstampCase):
    """Item 1: witness by file drop. Box A exports its manifests (and its
    anchored proofs) to an outbox; box B finds them in an inbox, stamps
    each through its own lane, and folds each file's hash into its own
    chain. How the files travel is the operator's business."""

    def setUp(self):
        super().setUp()
        self.outbox = self.root / 'outbox'
        self.inbox = self.root / 'inbox'
        self.inbox.mkdir()
        self.cfg['outbox'] = str(self.outbox)
        self.witness_state = self.root / 'witness'
        (self.root / 'wbooks').mkdir()
        (self.root / 'wbooks' / 'receipts.jsonl').write_text('{"txid": "cc", "records": 1}\n')
        self.wcfg = {
            'state_dir': str(self.witness_state),
            'calendar_url': self.calendar.url,
            'books': {'receipts': str(self.root / 'wbooks' / 'receipts.jsonl')},
            'journal': False,
            'fork_head': None,
            'inbox': str(self.inbox),
        }
        self.wlog = []

    def run_witness(self, period):
        return selfstamp.run(self.wcfg, period=period, log=self.wlog.append)

    def a_dir(self):
        return self.state / 'manifests'

    def a_chain(self):
        return json.loads((self.a_dir() / '2026-09-01.json').read_text())['chain']

    def b_chain(self):
        return json.loads((self.b_dir() / '2026-09-01.json').read_text())['chain']

    def b_dir(self):
        return self.witness_state / 'manifests'

    def witnessed(self):
        return sorted(p.name for p in (self.witness_state / 'witnessed').glob('*') if p.is_file())

    def deliver(self):
        """The operator's business, played here by a copy."""
        for p in self.outbox.iterdir():
            shutil.copy2(p, self.inbox / p.name)

    def export_anchored(self):
        """A's genesis manifest exported with its anchored proof; the fake
        then goes back to answering pending, so the witness's own stamps
        start pending like any fresh submission."""
        self.assertEqual(self.run_tool(P1), 0, self.log)
        self.calendar.mined_height = 965500
        self.assertEqual(self.run_tool(P1), 0, self.log)
        self.calendar.mined_height = None

    def test_outbox_receives_manifests_and_anchored_proofs_only(self):
        self.assertEqual(self.run_tool(P1), 0, self.log)
        a = self.a_chain()
        self.assertEqual(sorted(p.name for p in self.outbox.iterdir()), ['%s-2026-09-01.json' % a])
        self.assertEqual((self.outbox / ('%s-2026-09-01.json' % a)).read_bytes(),
                         (self.a_dir() / '2026-09-01.json').read_bytes())
        self.calendar.mined_height = 965500
        self.assertEqual(self.run_tool(P1), 0, self.log)
        self.assertEqual(sorted(p.name for p in self.outbox.iterdir()),
                         ['%s-2026-09-01.json' % a, '%s-2026-09-01.json.ots' % a])
        exported = (self.outbox / ('%s-2026-09-01.json.ots' % a)).read_bytes()
        self.assertEqual(exported, (self.a_dir() / '2026-09-01.json.ots').read_bytes())
        self.assertEqual(selfstamp.parse_ots(exported).attestation, ('bitcoin', 965500))
        before = {p.name: p.stat().st_mtime_ns for p in self.outbox.iterdir()}
        self.assertEqual(self.run_tool(P1), 0, self.log)
        self.assertEqual({p.name: p.stat().st_mtime_ns for p in self.outbox.iterdir()}, before)

    def test_inbox_is_consumed_exactly_once_and_stamped_through_the_lane(self):
        self.export_anchored()
        self.deliver()
        a_raw = (self.a_dir() / '2026-09-01.json').read_bytes()
        a_sha = hashlib.sha256(a_raw).hexdigest()
        posts = self.calendar.operator_posts
        self.assertEqual(self.run_witness(P1), 0, self.wlog)
        self.assertEqual(sorted(p.name for p in self.inbox.iterdir()), [])
        name = '%s-2026-09-01-%s.json' % (self.a_chain(), a_sha[:12])
        self.assertEqual(self.witnessed(), [name, name + '.foreign.ots', name + '.ots'])
        wdir = self.witness_state / 'witnessed'
        self.assertEqual((wdir / name).read_bytes(), a_raw)
        own = selfstamp.parse_ots((wdir / (name + '.ots')).read_bytes())
        self.assertEqual(own.digest, bytes.fromhex(a_sha))
        self.assertEqual(own.attestation, ('pending', URI))
        # Two lane posts: the witness's own manifest and the witnessed file;
        # never the counted door.
        self.assertEqual(self.calendar.operator_posts, posts + 2)
        self.assertEqual(self.calendar.client_posts, 0)
        m = json.loads((self.b_dir() / '2026-09-01.json').read_text())
        self.assertEqual(len(m['witnessed']), 1)
        entry = m['witnessed'][0]
        self.assertEqual({k: entry[k] for k in ('chain', 'seq', 'period', 'file', 'sha256', 'foreign_proof')}, {
            'chain': self.a_chain(), 'seq': 1, 'period': '2026-09-01', 'file': name,
            'sha256': a_sha, 'foreign_proof': 'bitcoin height=965500'})
        self.assertNotIn('host', entry)
        self.assertRegex(entry['witnessed_at'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
        self.assertTrue(any('witnessed chain=%s seq=1' % self.a_chain() in line for line in self.wlog), self.wlog)
        # Consumed exactly once: a second run stamps nothing and folds nothing new.
        self.assertEqual(self.run_witness(P1), 0, self.wlog)
        self.assertEqual(self.calendar.operator_posts, posts + 2)
        self.assertEqual(self.run_witness(P2), 0, self.wlog)
        self.assertEqual(json.loads((self.b_dir() / '2026-09-02.json').read_text())['witnessed'], [])
        self.assertEqual(self.calendar.operator_posts, posts + 3)

    def test_witnessed_chain_verifies_and_names_what_it_vouches_for(self):
        self.export_anchored()
        self.deliver()
        self.assertEqual(self.run_witness(P1), 0, self.wlog)
        self.assertEqual(self.run_witness(P2), 0, self.wlog)
        rows = []
        self.assertTrue(selfstamp.verify_chain(self.b_dir(), log=rows.append), rows)
        a_sha = sha256_hex(self.a_dir() / '2026-09-01.json')
        self.assertTrue(any('vouches for chain=%s seq=1 period=2026-09-01 sha256=%s' % (self.a_chain(), a_sha) in r
                            and 'copy=ok' in r and 'proof=pending' in r for r in rows), rows)
        # The witness's proof of the foreign file anchors like any other.
        self.calendar.mined_height = 965600
        self.assertEqual(self.run_witness(P2), 0, self.wlog)
        rows = []
        self.assertTrue(selfstamp.verify_chain(self.b_dir(), log=rows.append), rows)
        self.assertTrue(any('vouches for chain=%s seq=1' % self.a_chain() in r and 'proof=bitcoin height=965600' in r
                            for r in rows), rows)

    def test_a_tampered_foreign_manifest_is_caught(self):
        self.export_anchored()
        self.deliver()
        self.assertEqual(self.run_witness(P1), 0, self.wlog)
        # Cross-check A's chain against B's: intact first.
        rows = []
        self.assertTrue(selfstamp.verify_chain(self.a_dir(), witness=self.b_dir(), log=rows.append), rows)
        self.assertTrue(any('witnessed by %s' % self.b_chain() in r and '2026-09-01.json' in r for r in rows), rows)
        # A's copy altered after the fact: the witness holds the old hash.
        path = self.a_dir() / '2026-09-01.json'
        raw = path.read_bytes()
        path.write_bytes(raw.replace(b'"records": 3', b'"records": 4') if b'"records": 3' in raw
                         else raw.replace(b'"seq": 1', b'"seq": 1 '))
        self.assertNotEqual(path.read_bytes(), raw)
        rows = []
        self.assertFalse(selfstamp.verify_chain(self.a_dir(), witness=self.b_dir(), log=rows.append), rows)
        self.assertTrue(any('2026-09-01.json' in r and 'different hash' in r for r in rows), rows)
        path.write_bytes(raw)
        self.assertTrue(selfstamp.verify_chain(self.a_dir(), witness=self.b_dir(), log=rows.append))
        # B's own copy altered: B's chain no longer verifies.
        wdir = self.witness_state / 'witnessed'
        copy = next(p for p in wdir.glob('%s-*.json' % self.a_chain()))
        copy.write_bytes(copy.read_bytes() + b'\n')
        rows = []
        self.assertFalse(selfstamp.verify_chain(self.b_dir(), log=rows.append), rows)
        self.assertTrue(any('copy=MISMATCH' in r for r in rows), rows)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(selfstamp.main(['verify', '--manifests', str(self.b_dir())]), 1)
            self.assertEqual(selfstamp.main(['verify', '--manifests', str(self.a_dir()),
                                             '--witness', str(self.b_dir())]), 0)

    def test_duplicates_and_garbage_in_the_inbox(self):
        self.export_anchored()
        self.deliver()
        self.assertEqual(self.run_witness(P1), 0, self.wlog)
        copies = self.witnessed()
        self.deliver()                                   # the same files again
        (self.inbox / 'garbage.json').write_text('not a manifest\n')
        (self.inbox / 'other.json').write_text(json.dumps({'schema': 'selfstamp/2', 'host': 'x'}) + '\n')
        posts = self.calendar.operator_posts
        self.assertEqual(self.run_witness(P2), 0, self.wlog)
        self.assertEqual(self.witnessed(), copies)
        self.assertEqual(self.calendar.operator_posts, posts + 1)   # B's own manifest only
        self.assertEqual(sorted(p.name for p in self.inbox.iterdir()), ['rejected'])
        kept = sorted(p.name for p in (self.inbox / 'rejected').iterdir())
        self.assertEqual(sorted(k.split('-', 1)[1] for k in kept), ['garbage.json', 'other.json'])
        self.assertTrue(all(len(k.split('-', 1)[0]) == 12 for k in kept), kept)   # by content, so nothing is overwritten
        self.assertEqual(json.loads((self.b_dir() / '2026-09-02.json').read_text())['witnessed'], [])
        self.assertTrue(any('inbox duplicate' in line for line in self.wlog), self.wlog)
        self.assertTrue(any('inbox rejected' in line and 'garbage.json' in line for line in self.wlog), self.wlog)


def proof_bytes(digest, height=None):
    """A minimal detached proof of digest: pending (height None) or bitcoin."""
    if height is None:
        return selfstamp.build_ots(digest, pending_response(digest)[0])
    payload = vu(height)
    return MAGIC + b'\x01\x08' + digest + b'\x00' + BITCOIN_TAG + vu(len(payload)) + payload


HOLD_SCRIPT = """
import importlib.util, sys, time
spec = importlib.util.spec_from_file_location('selfstamp', sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
with m.state_lock(sys.argv[2], 0):
    print('held', flush=True)
    time.sleep(float(sys.argv[3]))
"""


class Test_state_lock(SelfstampCase):
    """2026-09-15 review, P2 "concurrent self-stamp runs can leave an
    irrecoverably mismatched proof": run and upgrade hold an exclusive lock
    on the state directory for their whole duration, across processes."""

    def test_two_overlapping_runs_in_one_process_leave_one_manifest_with_its_own_proof(self):
        # Two runs for the same period, with different clocks (so their
        # manifests would differ), both start before either has written.
        original = selfstamp.build_manifest

        def slow_build(*args, **kwargs):
            time.sleep(0.5)
            return original(*args, **kwargs)
        codes, errors = [], []

        def run(second):
            try:
                codes.append(selfstamp.run(self.cfg, period=P1, log=self.log.append,
                                           now=datetime.datetime(2026, 9, 2, 0, 30, second, tzinfo=datetime.timezone.utc)))
            except Exception as exc:
                errors.append(exc)
        with mock.patch.object(selfstamp, 'build_manifest', side_effect=slow_build):
            threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(codes), [0, 0])
        self.assertEqual(self.manifests(), ['2026-09-01.json'])
        raw = (self.state / 'manifests' / '2026-09-01.json').read_bytes()
        proof = selfstamp.parse_ots((self.state / 'manifests' / '2026-09-01.json.ots').read_bytes())
        self.assertEqual(proof.digest, hashlib.sha256(raw).digest())
        self.assertEqual(self.calendar.operator_posts, 1, 'one manifest, one submission')
        self.assertTrue(any('noop' in line for line in self.log), self.log)
        rows = []
        self.assertTrue(selfstamp.verify_chain(self.state / 'manifests', log=rows.append), rows)

    def test_a_manual_command_overlapping_the_timer_waits_or_is_refused_across_processes(self):
        cfg_path = self.root / 'config.json'
        cfg_path.write_text(json.dumps({'state_dir': str(self.state), 'calendar_url': self.calendar.url,
                                        'books': {}}))
        cli = [sys.executable, str(TOOL)]
        holder = subprocess.Popen([sys.executable, '-c', HOLD_SCRIPT, str(TOOL), str(self.state), '4'],
                                  stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), 'held')
            # A short wait: refused, nothing written.
            refused = subprocess.run(cli + ['run', '--config', str(cfg_path), '--period', '2026-09-01', '--lock-wait', '0.5'],
                                     capture_output=True, text=True, timeout=30)
            self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
            self.assertIn('locked', refused.stdout)
            self.assertEqual(self.manifests(), [])
            refused = subprocess.run(cli + ['upgrade', '--config', str(cfg_path), '--lock-wait', '0.5'],
                                     capture_output=True, text=True, timeout=30)
            self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
            self.assertIn('locked', refused.stdout)
            # A patient one waits for the holder, then does the run.
            waited = subprocess.run(cli + ['run', '--config', str(cfg_path), '--period', '2026-09-01', '--lock-wait', '30'],
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(waited.returncode, 0, waited.stdout + waited.stderr)
            self.assertEqual(self.manifests(), ['2026-09-01.json'])
        finally:
            holder.kill()
            holder.wait()

    def test_atomic_writes_use_unique_names_and_fsync_the_directory(self):
        target = self.root / 'target'
        synced = []
        real = os.fsync

        def record(fd):
            synced.append(stat.S_ISDIR(os.fstat(fd).st_mode))
            return real(fd)
        with mock.patch.object(selfstamp.os, 'fsync', side_effect=record):
            threads = [threading.Thread(target=selfstamp.write_atomic, args=(target, b'%d' % i)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        self.assertIn(target.read_bytes(), [b'%d' % i for i in range(8)])
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.name.startswith('.target.')), [],
                         'no temporary file is left behind')
        self.assertGreaterEqual(synced.count(True), 8, 'every write fsyncs the directory')
        self.assertGreaterEqual(synced.count(False), 8)


class Test_witnessed_copies(unittest.TestCase):
    """2026-09-15 review: a vouch whose copy is absent (the file or the whole
    directory) is a break, unless the partial check is asked for by name;
    a proof delivered later for a manifest already witnessed is stored."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = pathlib.Path(self.tmpdir.name)

    def chain_with_a_vouch(self, copies):
        entry = {'host': 'other', 'seq': 1, 'period': '2026-09-01', 'file': 'other-2026-09-01-abcdef012345.json',
                 'sha256': 'ab' * 32}
        manifests = self.root / 'manifests'
        manifests.mkdir()
        (manifests / '2026-09-01.json').write_text(json.dumps({
            'schema': 'selfstamp/2', 'host': 'box', 'seq': 1, 'period': '2026-09-01', 'prev': None,
            'commissioning': {}, 'witnessed': [entry]}))
        return manifests, self.root / copies

    def test_a_missing_directory_or_copy_is_a_break(self):
        manifests, copies = self.chain_with_a_vouch('absent')
        rows = []
        self.assertFalse(selfstamp.verify_chain(manifests, witnessed=copies, log=rows.append), rows)
        self.assertTrue(any('copy=missing' in r for r in rows), rows)
        self.assertNotIn('unavailable', '\n'.join(rows))
        copies.mkdir()   # the directory without the copy: the same break
        rows = []
        self.assertFalse(selfstamp.verify_chain(manifests, witnessed=copies, log=rows.append), rows)
        self.assertTrue(any('copy=missing' in r for r in rows), rows)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(selfstamp.main(['verify', '--manifests', str(manifests), '--witnessed', str(copies)]), 1)

    def test_the_partial_check_must_be_asked_for_and_is_labelled(self):
        manifests, copies = self.chain_with_a_vouch('absent')
        rows = []
        self.assertTrue(selfstamp.verify_chain(manifests, witnessed=copies, skip_witnessed=True, log=rows.append), rows)
        self.assertTrue(any('copy=SKIPPED' in r for r in rows), rows)
        self.assertTrue(any('copies not checked' in r for r in rows), rows)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(selfstamp.main(['verify', '--manifests', str(manifests), '--witnessed', str(copies),
                                             '--skip-witnessed']), 0)
        self.assertIn('SKIPPED', out.getvalue())

    def test_a_later_foreign_proof_is_stored_and_a_lesser_one_never_replaces_a_better(self):
        raw = json.dumps({'schema': 'selfstamp/2', 'host': 'other', 'seq': 1, 'period': '2026-09-01'}).encode()
        digest = hashlib.sha256(raw).digest()
        inbox, copies = self.root / 'inbox', self.root / 'witnessed'
        inbox.mkdir()
        log = []

        def deliver(proof=None):
            (inbox / 'manifest.json').write_bytes(raw)
            if proof is not None:
                (inbox / 'manifest.json.ots').write_bytes(proof)
            selfstamp.witness_inbox({'inbox': str(inbox)}, copies, log.append)
            self.assertEqual([p.name for p in inbox.iterdir() if p.name != 'rejected'], [])

        def foreign():
            found = list(copies.glob('*.foreign.ots'))
            return found[0].read_bytes() if found else None
        deliver()                                    # the manifest first, as the outbox exports it
        self.assertIsNone(foreign())
        deliver(proof_bytes(digest))                 # a pending proof: stored
        self.assertEqual(selfstamp.parse_ots(foreign()).attestation[0], 'pending')
        deliver(proof_bytes(digest, 965500))         # anchored: replaces the pending one
        self.assertEqual(selfstamp.parse_ots(foreign()).attestation, ('bitcoin', 965500))
        deliver(proof_bytes(digest))                 # pending again: the anchored one is kept
        self.assertEqual(selfstamp.parse_ots(foreign()).attestation, ('bitcoin', 965500))
        deliver(proof_bytes(b'\x00' * 32, 1))        # a proof of other bytes: rejected, the held one kept
        self.assertEqual(selfstamp.parse_ots(foreign()).attestation, ('bitcoin', 965500))
        self.assertTrue(any('foreign proof rejected' in line for line in log), log)
        self.assertTrue(any('inbox duplicate' in line and 'foreign_proof=bitcoin height=965500' in line for line in log), log)
        self.assertEqual(sorted(p.name for p in copies.iterdir()),
                         sorted(['legacy-2026-09-01-%s.json' % digest.hex()[:12],
                                 'legacy-2026-09-01-%s.json.foreign.ots' % digest.hex()[:12]]),
                         'a selfstamp/2 source is named legacy, never by its host')
        self.assertEqual(len(list((inbox / 'rejected').iterdir())), 1, 'the proof of other bytes is kept, not deleted')


class Test_witness_delivery(Test_witness):
    """The normal two-stage delivery: the manifest today, the anchored
    proof on a later pass. The proof must be kept, and verify shows it."""

    def test_the_source_proof_that_arrives_later_is_kept_and_shown(self):
        self.assertEqual(self.run_tool(P1), 0, self.log)      # A: manifest exported, proof pending (not exported)
        self.deliver()
        self.assertEqual(self.run_witness(P1), 0, self.wlog)   # B witnesses the manifest alone
        name = next(p.name for p in (self.witness_state / 'witnessed').glob('*-2026-09-01-*.json'))
        self.assertFalse((self.witness_state / 'witnessed' / (name + '.foreign.ots')).exists())
        self.calendar.mined_height = 965500
        self.assertEqual(self.run_tool(P1), 0, self.log)      # A's proof anchors and is exported
        self.calendar.mined_height = None
        self.deliver()                                         # manifest again, now with the proof
        self.assertEqual(self.run_witness(P2), 0, self.wlog)
        foreign = self.witness_state / 'witnessed' / (name + '.foreign.ots')
        self.assertTrue(foreign.exists(), 'the later proof is stored, not deleted as a duplicate')
        self.assertEqual(selfstamp.parse_ots(foreign.read_bytes()).attestation, ('bitcoin', 965500))
        self.assertEqual(sorted(p.name for p in self.inbox.iterdir()), [])
        rows = []
        self.assertTrue(selfstamp.verify_chain(self.b_dir(), log=rows.append), rows)
        self.assertTrue(any('vouches for chain=%s seq=1' % self.a_chain() in r and 'foreign_now=bitcoin height=965500' in r
                            for r in rows), rows)
        # The manifest that witnessed it is not rewritten: its entry still
        # records what was known at the time.
        m = json.loads((self.b_dir() / '2026-09-01.json').read_text())
        self.assertIsNone(m['witnessed'][0]['foreign_proof'])


class Test_malformed_foreign_proof(SelfstampCase):
    """2026-09-15/16 review F14: a foreign proof companion whose pending URI
    held an invalid UTF-8 byte raised UnicodeDecodeError out of parse_ots,
    past the OtsError handling, before the host's own manifest was
    written; the same inbox pair stopped every later run. The reader now
    raises only OtsError, so the companion is rejected into
    <inbox>/rejected/ (kept, not deleted: workflow two), the manifest
    witnessed, and the heartbeat written."""

    def test_a_malformed_foreign_proof_is_rejected_and_the_heartbeat_goes_on(self):
        inbox = self.root / 'inbox'
        inbox.mkdir()
        cfg = dict(self.cfg, inbox=str(inbox))
        raw = json.dumps({'schema': 'selfstamp/2', 'host': 'foreign', 'seq': 1, 'period': '2026-09-01'}).encode()
        digest = hashlib.sha256(raw).digest()
        proof = selfstamp.MAGIC + b'\x01\x08' + digest + b'\x00' + selfstamp.PENDING_TAG + b'\x02\x01\xff'
        for period in (P1, P2):
            (inbox / 'manifest.json').write_bytes(raw)
            (inbox / 'manifest.json.ots').write_bytes(proof)
            self.assertEqual(selfstamp.run(cfg, period=period, log=self.log.append), 0, self.log)
            self.assertEqual(sorted(p.name for p in inbox.iterdir()), ['rejected'], 'the pair is consumed, not left to stop the next run')
        self.assertEqual(self.manifests(), ['2026-09-01.json', '2026-09-02.json'])
        self.assertTrue(any('foreign proof rejected' in line for line in self.log), self.log)
        self.assertEqual(len(list((inbox / 'rejected').iterdir())), 1, 'the same bad bytes land on the same quarantine name')
        witnessed = self.state / 'witnessed'
        self.assertEqual(len(list(witnessed.glob('legacy-*.json'))), 1)
        self.assertEqual(list(witnessed.glob('*.foreign.ots')), [], 'a proof that does not parse is never kept')

if __name__ == "__main__":
    unittest.main()
