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
import tempfile
import threading
import unittest

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
        self.gets = 0
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
                self._reply(200, body)

            def do_GET(self):
                cal.gets += 1
                if not self.path.startswith('/timestamp/'):
                    return self._reply(404, b'not found', 'text/plain')
                commitment = bytes.fromhex(self.path[len('/timestamp/'):])
                if commitment not in cal.known:
                    return self._reply(404, b'Not found', 'text/plain')
                if cal.mined_height is None:
                    return self._reply(404, b'Pending confirmation in Bitcoin blockchain',
                                       'text/plain')
                self._reply(200, bitcoin_response(cal.mined_height))

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
            'host': 'testbox',
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
        self.assertEqual(m['schema'], 'selfstamp/1')
        self.assertEqual(m['host'], 'testbox')
        self.assertEqual(m['period'], '2026-09-01')
        self.assertEqual(m['created_at'], '2026-09-02T00:30:07Z')
        self.assertEqual(m['seq'], 1)
        self.assertIsNone(m['prev'])
        self.assertIsNone(m['journal'])
        self.assertIsNone(m['fork_head'])
        self.assertEqual(set(m['books']), {'receipts', 'payer_log', 'compose', 'absent'})
        self.assertEqual(m['books']['receipts'],
                         {'path': str(self.books / 'anchor-receipts.jsonl'),
                          'sha256': sha256_hex(self.books / 'anchor-receipts.jsonl'),
                          'bytes': 29})
        self.assertEqual(m['books']['absent'],
                         {'path': str(self.books / 'not-there'), 'missing': True})
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
        tampered = raw.replace(b'"schema": "selfstamp/1"', b'"schema": "selfstamp/2"')
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
                         {'path': str(repo), 'ref': 'refs/heads/calendar-ops',
                          'commit': '0d1c80f90dcb003eb43a0045fb3cdb643fdf2f34'})
        (repo / '.git' / 'HEAD').write_text('0d1c80f90dcb003eb43a0045fb3cdb643fdf2f34\n')
        self.assertEqual(selfstamp.git_head(repo)['ref'], None)
        self.assertEqual(selfstamp.git_head(self.root / 'nowhere'),
                         {'path': str(self.root / 'nowhere'), 'missing': True})

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


if __name__ == "__main__":
    unittest.main()
