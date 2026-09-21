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

"""ops/selfstamp.py, workflow two: the state transitions of the self-stamp
(docs/contracts.md, "Workflow 2", tables S1-S9), each pinned beside its
failure cases.

Every class says which transition it pins and which fault model it uses.
The models here are: an exception injected at a named call (a writer that
touches a book mid-read; the n-th rename, replace, unlink or fsync of a
fresh run, swept until a fresh run has no n-th call; a read that is
refused); a real process paused at a named boundary and killed with
SIGKILL; a fake calendar that commits a digest and drops the response, or
cuts a timestamp short; two real processes on the lock; a deliverer that
replaces a file's name while the run holds the file. None is a power cut,
and no test orders events with a sleep: a paused child prints where it
stands and the parent reads that line before it kills.

The legacy corpus under ops/tests/selfstamp/v2/ was written by the tool as
it stood at 3961a1f (schema selfstamp/2): two chains, one witnessing the
other. It is copied into a scratch directory before any test touches it.

The 2026-09-16 gate review's nine assertions (a replaced delivery lost, a
held proof of other bytes outranking a right one, a proof of other bytes
exported, unreadable witness evidence read as success, unrelated legacy
chains called a mismatch, paths in messages, a non-hex label accepted, a
predecessor with an unknown schema accepted, a quarantine acknowledged
before it was durable) are kept here as permanent regressions, in this
module's shape.
"""

import contextlib
import datetime
import functools
import hashlib
import io
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import unittest
from unittest import mock

from otsserver.tests.faults import fail_on_call, unreadable   # shared with the watcher's tests
from otsserver.tests.test_selfstamp import (   # the fake calendar's case class and the tool, loaded by path
    SelfstampCase, TOOL, P1, P2, P3, proof_bytes, selfstamp, sha256_hex)

FIXTURE = pathlib.Path(__file__).resolve().parents[2] / 'ops' / 'tests' / 'selfstamp' / 'v2'
UTC = datetime.timezone.utc
HEX32 = r'^[0-9a-f]{32}$'


def sha12(data):
    return hashlib.sha256(data).hexdigest()[:12]


def keys_in(value):
    """Every key name anywhere inside a parsed manifest"""
    found = set()
    if isinstance(value, dict):
        for k, v in value.items():
            found.add(k)
            found |= keys_in(v)
    elif isinstance(value, list):
        for v in value:
            found |= keys_in(v)
    return found


class Tap:
    """A binary file object that runs a hook after its first read: the
    writer that touches a book while the tool is hashing it."""

    def __init__(self, fd, hook):
        self.fd, self.hook, self.fired = fd, hook, False

    def read(self, n=-1):
        data = self.fd.read(n)
        if not self.fired:
            self.fired = True
            self.hook()
        return data

    def fileno(self):
        return self.fd.fileno()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.fd.close()


def tapped_open(target, hook):
    """Patch the tool's open() so that reading `target` runs `hook` between
    the first chunk and the rest. Every other open is untouched."""
    real_open = open

    def opener(path, mode='r', *args, **kwargs):
        fd = real_open(path, mode, *args, **kwargs)
        if 'b' in mode and os.path.abspath(str(path)) == os.path.abspath(str(target)):
            return Tap(fd, hook)
        return fd
    return mock.patch.object(selfstamp, 'open', opener, create=True)


# --- S1: the period and the observation -------------------------------------

class Test_period_and_observation(SelfstampCase):
    """S1, S3. The period names a finished UTC day; the observations are
    made at the run, whenever that is, and the manifest says so; the
    predecessor is read by the one validator. Fault model: none (clock
    arguments, a stray file)."""

    def test_a_period_that_has_not_finished_is_refused(self):
        now = datetime.datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
        for period in (datetime.date(2026, 9, 2), datetime.date(2026, 9, 3)):
            rc = self.run_tool(period, now=now)
            self.assertEqual(rc, 1, self.log)
            self.assertTrue(any('refused' in line and 'not finished' in line for line in self.log), self.log)
        self.assertEqual(self.manifests(), [])
        self.assertEqual(self.calendar.operator_posts, 0)
        # The day before is finished: written.
        self.assertEqual(self.run_tool(datetime.date(2026, 9, 1), now=now), 0, self.log)
        self.assertEqual(self.manifests(), ['2026-09-01.json'])

    def test_a_late_run_records_when_it_looked_not_the_day_it_names(self):
        """A manifest written days after its period hashes the books as they
        are now and says so in created_at; the days between are a gap the
        chain links across. Nothing is written for a day the tool did not
        see."""
        with open(self.books / 'anchor-receipts.jsonl', 'a') as fd:
            fd.write('{"txid": "later", "records": 1}\n')
        late = datetime.datetime(2026, 9, 10, 10, 0, 0, tzinfo=UTC)
        self.assertEqual(self.run_tool(P1, now=late), 0, self.log)
        m = self.manifest('2026-09-01.json')
        self.assertEqual(m['period'], '2026-09-01')
        self.assertEqual(m['created_at'], '2026-09-10T10:00:00Z')
        self.assertEqual(m['books']['receipts']['sha256'], sha256_hex(self.books / 'anchor-receipts.jsonl'))
        # The next timer run covers the day before it, not the days missed.
        self.assertEqual(self.run_tool(None, now=datetime.datetime(2026, 9, 11, 0, 30, tzinfo=UTC)), 0, self.log)
        self.assertEqual(self.manifests(), ['2026-09-01.json', '2026-09-10.json'])
        m2 = self.manifest('2026-09-10.json')
        self.assertEqual((m2['seq'], m2['prev']['file']), (2, '2026-09-01.json'))

    def test_a_predecessor_that_is_not_a_manifest_refuses_the_run(self):
        """S3. The newest file in manifests/ is read by the one validator: a
        JSON object with the right field types but an unknown schema is not
        a manifest, and nothing is chained to it (gate review, P4)."""
        manifests = self.state / 'manifests'
        manifests.mkdir(parents=True)
        (manifests / '2026-09-01.json').write_text(json.dumps({'schema': 'not-a-manifest', 'seq': 41, 'period': '2026-09-01'}))
        with self.assertRaises(ValueError):
            selfstamp.latest_manifest(manifests)
        self.assertEqual(self.run_tool(P2), 1, self.log)
        self.assertTrue(any('refused' in line and 'latest manifest unreadable' in line for line in self.log), self.log)
        self.assertEqual(self.manifests(), ['2026-09-01.json'])
        self.assertEqual(self.calendar.operator_posts, 0)
        rows = []
        self.assertFalse(selfstamp.verify_chain(manifests, log=rows.append), rows)
        self.assertTrue(any('BROKEN' in r and 'not a selfstamp manifest' in r for r in rows), rows)


# --- S2: the books --------------------------------------------------------------

class Test_input_stability(SelfstampCase):
    """S2. A book that changes while it is being read gets no digest: the
    entry says `unstable` and why. Fault model: a hook inside the tool's
    read loop plays the writer (append, truncate, rewrite, replace,
    remove); the metadata comparison is the tool's own."""

    def setUp(self):
        super().setUp()
        self.book = self.books / 'anchor-receipts.jsonl'
        self.book.write_bytes(b'x' * (selfstamp.CHUNK + 100))

    def hashed_while(self, hook):
        with tapped_open(self.book, hook):
            return selfstamp.hash_file(str(self.book))

    def test_a_stable_book_gets_a_digest(self):
        entry = self.hashed_while(lambda: None)
        self.assertEqual(entry, {'sha256': sha256_hex(self.book), 'bytes': selfstamp.CHUNK + 100})

    def test_an_append_during_the_read_is_unstable(self):
        def append():
            with open(self.book, 'ab') as fd:
                fd.write(b'{"txid": "bb"}\n')
        entry = self.hashed_while(append)
        self.assertIn('unstable', entry, entry)
        self.assertNotIn('sha256', entry)
        self.assertNotIn('bytes', entry)

    def test_a_truncation_during_the_read_is_unstable(self):
        entry = self.hashed_while(lambda: os.truncate(self.book, 10))
        self.assertIn('unstable', entry, entry)
        self.assertNotIn('sha256', entry)

    def test_a_rewrite_in_place_during_the_read_is_unstable(self):
        def rewrite():
            # Same size, different bytes. A rewrite moves mtime; the explicit
            # utime stands in for the clock tick so the test does not depend
            # on the filesystem's timestamp resolution.
            with open(self.book, 'r+b') as fd:
                fd.seek(selfstamp.CHUNK + 50)
                fd.write(b'Y')
            st = os.stat(self.book)
            os.utime(self.book, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        entry = self.hashed_while(rewrite)
        self.assertIn('unstable', entry, entry)
        self.assertNotIn('sha256', entry)

    def test_a_replacement_of_the_path_during_the_read_is_unstable(self):
        def replace():
            new = self.book.with_name('new')
            new.write_bytes(b'x' * (selfstamp.CHUNK + 100))   # even the same bytes: another file
            os.replace(new, self.book)
        entry = self.hashed_while(replace)
        self.assertIn('unstable', entry, entry)
        self.assertIn('replaced', entry['unstable'])
        self.assertNotIn('sha256', entry)

    def test_a_removal_during_the_read_is_unstable_not_missing(self):
        entry = self.hashed_while(lambda: self.book.unlink())
        self.assertIn('unstable', entry, entry)
        self.assertNotIn('missing', entry)
        self.assertNotIn('sha256', entry)

    def test_an_unstable_book_is_recorded_in_the_manifest_and_the_run_goes_on(self):
        def append():
            with open(self.book, 'ab') as fd:
                fd.write(b'more\n')
        with tapped_open(self.book, append):
            rc = self.run_tool(P1)
        self.assertEqual(rc, 0, self.log)
        m = self.manifest('2026-09-01.json')
        self.assertIn('unstable', m['books']['receipts'])
        self.assertNotIn('sha256', m['books']['receipts'])
        self.assertIn('sha256', m['books']['payer_log'], 'the other books are unaffected')
        rows = []
        self.assertTrue(selfstamp.verify_chain(self.state / 'manifests', log=rows.append), rows)

    def test_a_directory_that_changes_while_it_is_hashed_is_qualified(self):
        lims = self.root / 'lims'
        lims.mkdir()
        (lims / 'a.log').write_bytes(b'a' * 10)
        (lims / 'b.log').write_bytes(b'b' * 10)
        with tapped_open(lims / 'a.log', lambda: (lims / 'c.log').write_bytes(b'c')):
            entry = selfstamp.audit_log_entry(str(lims))
        self.assertIn('unstable', entry, entry)
        self.assertEqual(len(entry['files']), 2, 'the files that were stable are still listed')
        # And a directory nobody touches is not qualified.
        entry = selfstamp.audit_log_entry(str(lims))
        self.assertNotIn('unstable', entry)
        self.assertEqual(len(entry['files']), 3)


class Test_configuration_fingerprint(SelfstampCase):
    """S2 / S9. The manifest names the configuration the run used: the bytes
    load_config read, never a later reread of the file. Fault model: the
    file changes between load and run."""

    def test_the_fingerprint_is_of_the_bytes_that_were_loaded(self):
        cfg_path = self.root / 'config.json'
        cfg_path.write_text(json.dumps(self.cfg, indent=1))
        loaded = sha256_hex(cfg_path)
        cfg = selfstamp.load_config(cfg_path)
        cfg_path.write_text(json.dumps(dict(self.cfg, float_low_sats=5), indent=1))   # edited after the load
        self.assertNotEqual(sha256_hex(cfg_path), loaded)
        self.assertEqual(selfstamp.run(cfg, period=P1, log=self.log.append), 0, self.log)
        m = self.manifest('2026-09-01.json')
        self.assertEqual(m['config'], {'sha256': loaded})

    def test_every_manifest_carries_the_fingerprint_and_a_dict_config_hashes_its_canonical_json(self):
        expected = hashlib.sha256(json.dumps(self.cfg, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(self.run_tool(P1), 0, self.log)
        self.assertEqual(self.run_tool(P2), 0, self.log)
        self.assertEqual(self.manifest('2026-09-01.json')['config'], {'sha256': expected})
        self.assertEqual(self.manifest('2026-09-02.json')['config'], {'sha256': expected})
        self.cfg['float_low_sats'] = 7
        self.assertEqual(self.run_tool(P3), 0, self.log)
        self.assertNotEqual(self.manifest('2026-09-03.json')['config']['sha256'], expected,
                            'a changed configuration shows on the next manifest')


# --- S3: what leaves the box ------------------------------------------------------

class Test_amnesia(SelfstampCase):
    """S3. A manifest, an export, a copy name, a log line and a verify line
    name a chain by its opaque label and a book by the operator's key:
    never a host name, a path or an external file name. Fault model: none;
    a marker string is planted in every place a name could leak from."""

    MARK = 'client-acme-7731'

    def setUp(self):
        super().setUp()
        marked = self.root / self.MARK
        marked.mkdir()
        self.marked = marked
        for name in ('anchor-receipts.jsonl', 'payer.log', 'compose.yml'):
            shutil.copy(self.books / name, marked / name)
        self.cfg['books'] = {'receipts': str(marked / 'anchor-receipts.jsonl'),
                             'payer_log': str(marked / 'payer.log'),
                             'compose': str(marked / 'compose.yml'),
                             'absent': str(marked / 'not-there')}
        lims = marked / 'lims'
        lims.mkdir()
        (lims / ('%s-audit.log' % self.MARK)).write_bytes(b'row\n')
        (lims / 'archive').mkdir()
        self.cfg['audit_logs'] = {'lims': str(lims), 'gone': str(marked / 'nowhere')}
        repo = marked / 'repo'
        (repo / '.git' / 'refs' / 'heads').mkdir(parents=True)
        (repo / '.git' / 'HEAD').write_text('ref: refs/heads/calendar-ops\n')
        (repo / '.git' / 'refs' / 'heads' / 'calendar-ops').write_text('1d0fe48e589b86fb1f14299f813d0fa5d87de102\n')
        self.cfg['fork_head'] = str(repo)
        self.cfg['host'] = None
        self.outbox = self.root / 'outbox'
        self.cfg['outbox'] = str(self.outbox)

    def test_no_name_a_client_could_be_known_by_leaves_in_a_manifest_export_copy_or_log(self):
        with mock.patch('socket.gethostname', return_value=self.MARK + '-host'):
            self.assertEqual(self.run_tool(P1), 0, self.log)
            self.assertEqual(self.run_tool(P2), 0, self.log)
        for name in self.manifests():
            raw = (self.state / 'manifests' / name).read_bytes()
            self.assertNotIn(self.MARK.encode(), raw, name)
            self.assertNotIn(str(self.root).encode(), raw, 'no path of this box')
            m = json.loads(raw)
            self.assertFalse(keys_in(m) & {'host', 'path', 'dir', 'name', 'installed_at', 'commissioning'},
                             keys_in(m))
            self.assertRegex(m['chain'], HEX32)
            self.assertEqual(m['schema'], 'selfstamp/3')
            self.assertEqual(m['fork_head'], {'ref': 'refs/heads/calendar-ops',
                                              'commit': '1d0fe48e589b86fb1f14299f813d0fa5d87de102'})
        m1, m2 = self.manifest('2026-09-01.json'), self.manifest('2026-09-02.json')
        self.assertEqual(m1['chain'], m2['chain'], 'the label is stable along the chain')
        self.assertEqual(m1['books']['receipts'], {'sha256': sha256_hex(self.cfg['books']['receipts']), 'bytes': 29})
        self.assertEqual(m1['books']['absent'], {'missing': True})
        lims = m1['audit_logs']['lims']
        self.assertEqual(set(lims), {'files', 'skipped'})
        self.assertEqual(lims['files'], [{'sha256': hashlib.sha256(b'row\n').hexdigest(), 'bytes': 4,
                                          'mtime': lims['files'][0]['mtime']}])
        self.assertEqual(lims['skipped'], {'not a regular file': 1})
        self.assertEqual(m1['audit_logs']['gone'], {'missing': True})
        # Exports and the log carry the label, not the host.
        exported = sorted(p.name for p in self.outbox.iterdir())
        self.assertEqual(exported, ['%s-2026-09-01.json' % m1['chain'], '%s-2026-09-02.json' % m1['chain']])
        for line in self.log:
            self.assertNotIn(self.MARK, line, line)

    def test_an_unreadable_book_is_an_error_entry_without_its_path(self):
        restore = unreadable(self.cfg['books']['payer_log'])
        self.addCleanup(restore)
        self.assertEqual(self.run_tool(P1), 0, self.log)
        entry = self.manifest('2026-09-01.json')['books']['payer_log']
        self.assertEqual(set(entry), {'error'})
        self.assertIn('PermissionError', entry['error'])
        self.assertNotIn(self.MARK, entry['error'])
        self.assertNotIn('/', entry['error'])

    def test_a_configured_host_is_not_written_and_the_log_says_so(self):
        self.cfg['host'] = self.MARK + '-box'
        self.assertEqual(self.run_tool(P1), 0, self.log)
        raw = (self.state / 'manifests' / '2026-09-01.json').read_bytes()
        self.assertNotIn(self.MARK.encode(), raw)
        self.assertNotIn('host', json.loads(raw))
        self.assertTrue(any('host' in line and 'not written' in line for line in self.log), self.log)
        self.assertFalse(any(self.MARK in line for line in self.log), self.log)

    def test_the_genesis_is_the_commissioning_record_and_says_what_it_observed(self):
        now = datetime.datetime(2026, 9, 2, 0, 30, 7, tzinfo=UTC)
        self.assertEqual(self.run_tool(P1, now=now), 0, self.log)
        m = self.manifest('2026-09-01.json')
        self.assertEqual((m['seq'], m['prev'], m['created_at']), (1, None, '2026-09-02T00:30:07Z'))
        self.assertNotIn('installed_at', json.dumps(m))
        rows = []
        self.assertTrue(selfstamp.verify_chain(self.state / 'manifests', log=rows.append), rows)
        self.assertTrue(any(r.strip().startswith('genesis chain=%s' % m['chain']) and 'at=2026-09-02T00:30:07Z' in r
                            and 'fork=1d0fe48e' in r and 'config=%s' % m['config']['sha256'] in r for r in rows), rows)

    def test_operational_messages_name_no_path(self):
        """S1, S6, S9. A missing or unreadable inbox, a held lock, an empty or
        missing directory at verify: each is said without the path, which
        could name a client (gate review, P4)."""
        cfg = dict(self.cfg, state_dir=str(self.marked / 'state'), inbox=str(self.marked / 'no-inbox'), outbox=None)
        log = []
        self.assertEqual(selfstamp.run(cfg, period=P1, log=log.append), 1, log)
        self.assertTrue(any('inbox missing' in line for line in log), log)
        inbox = self.marked / 'inbox'
        inbox.mkdir()
        cfg['inbox'] = str(inbox)
        restore = unreadable(inbox)
        try:
            self.assertEqual(selfstamp.run(cfg, period=P2, log=log.append), 1, log)
        finally:
            restore()
        self.assertTrue(any('inbox unreadable' in line for line in log), log)
        with selfstamp.state_lock(cfg['state_dir'], 0):
            self.assertEqual(selfstamp.run(cfg, period=P3, log=log.append, lock_wait=0.2), 1, log)
        self.assertTrue(any('locked' in line for line in log), log)
        rows = []
        self.assertFalse(selfstamp.verify_chain(self.marked / 'nothing', log=rows.append), rows)
        self.assertIn('no manifests found', rows)
        manifests = self.marked / 'state' / 'manifests'
        self.assertFalse(selfstamp.verify_chain(manifests, witness=self.marked / 'nowhere', log=rows.append), rows)
        self.assertTrue(any(r.startswith('witness manifests directory missing') for r in rows), rows)
        self.assertTrue(selfstamp.cross_check(manifests, manifests, rows.append), rows)
        self.assertTrue(any(r.endswith(' not witnessed') for r in rows), rows)
        for line in log + rows:
            self.assertNotIn(self.MARK, line, line)
            self.assertNotIn(str(self.root), line, line)

    def test_a_label_that_is_not_32_hex_is_refused_at_every_seam(self):
        """S3, S6, S9. A `chain` that is not 32 hex digits is not a label: the
        validator refuses it, the inbox quarantines it, a chain does not
        continue from it, and verify calls it a break, none of them
        repeating it (gate review, P4)."""
        bad = json.dumps({'schema': 'selfstamp/3', 'chain': self.MARK, 'seq': 1, 'period': '2026-09-01'}).encode()
        with self.assertRaises(ValueError):
            selfstamp._parse_foreign_manifest(bad)
        inbox, witnessed, log = self.root / 'inbox', self.root / 'witnessed', []
        inbox.mkdir()
        (inbox / 'x.json').write_bytes(bad)
        self.assertEqual(selfstamp.witness_inbox({'inbox': str(inbox)}, witnessed, log.append), 0)
        self.assertEqual(sorted(p.name for p in (inbox / 'rejected').iterdir()), ['%s-x.json' % sha12(bad)])
        self.assertEqual(list(witnessed.glob('*.json')), [])
        self.assertTrue(any('inbox rejected' in line and 'chain label' in line for line in log), log)
        manifests = self.state / 'manifests'
        manifests.mkdir(parents=True)
        (manifests / '2026-09-01.json').write_bytes(bad)
        self.assertEqual(self.run_tool(P2), 1, self.log)
        self.assertEqual(self.manifests(), ['2026-09-01.json'])
        rows = []
        self.assertFalse(selfstamp.verify_chain(manifests, log=rows.append), rows)
        self.assertTrue(any('BROKEN' in r and 'chain label' in r for r in rows), rows)
        for line in log + self.log + rows:
            self.assertNotIn(self.MARK, line, line)


# --- S4: submission and its ambiguous outcomes --------------------------------------

class Test_submission_ambiguity(SelfstampCase):
    """S4 / S5. The calendar commits the digest and the answer is lost; the
    upgrade answer arrives cut short. Fault model: the fake calendar's
    switches; no file is touched by the fault."""

    def test_a_lost_submission_response_leaves_no_proof_and_the_next_run_resubmits(self):
        self.calendar.drop_post_responses = 1
        self.assertEqual(self.run_tool(P1), 1, self.log)
        self.assertEqual(self.manifests(), ['2026-09-01.json'])
        self.assertFalse((self.state / 'manifests' / '2026-09-01.json.ots').exists())
        self.assertEqual(len(self.calendar.known), 1, 'the calendar committed the digest')
        self.assertTrue(any('submit failed' in line for line in self.log), self.log)
        manifest_before = (self.state / 'manifests' / '2026-09-01.json').read_bytes()
        self.assertEqual(self.run_tool(P1), 0, self.log)
        self.assertEqual((self.state / 'manifests' / '2026-09-01.json').read_bytes(), manifest_before)
        proof = selfstamp.parse_ots((self.state / 'manifests' / '2026-09-01.json.ots').read_bytes())
        self.assertEqual(proof.digest, hashlib.sha256(manifest_before).digest())
        # Two submissions of one digest: the documented duplicate, never a
        # second manifest.
        self.assertEqual(self.calendar.operator_posts, 2)

    def test_a_truncated_upgrade_answer_leaves_the_pending_proof_untouched(self):
        self.assertEqual(self.run_tool(P1), 0, self.log)
        ots = self.state / 'manifests' / '2026-09-01.json.ots'
        pending = ots.read_bytes()
        self.calendar.mined_height = 965500
        self.calendar.truncate_timestamp_responses = 1
        self.assertEqual(self.run_tool(P1), 1, self.log)
        self.assertEqual(ots.read_bytes(), pending)
        self.assertTrue(any('upgrade refused' in line for line in self.log), self.log)
        self.assertEqual(self.run_tool(P1), 0, self.log)
        self.assertEqual(selfstamp.parse_ots(ots.read_bytes()).attestation, ('bitcoin', 965500))


# --- S5: proofs beside manifests -----------------------------------------------------

class Test_proof_states(SelfstampCase):
    """S5, S8. Missing, pending, bitcoin, malformed, mismatch: what the run
    does with each, what the exit code means, and what the outbox
    publishes. Fault model: files replaced between runs."""

    def test_a_proof_that_is_not_of_the_file_beside_it_is_reported_and_never_upgraded(self):
        self.assertEqual(self.run_tool(P1), 0, self.log)
        ots = self.state / 'manifests' / '2026-09-01.json.ots'
        wrong = proof_bytes(hashlib.sha256(b'another file').digest())   # pending, of other bytes
        ots.write_bytes(wrong)
        self.calendar.mined_height = 965500
        gets = self.calendar.gets
        posts = self.calendar.operator_posts
        self.assertEqual(self.run_tool(P1), 1, self.log)
        self.assertEqual(ots.read_bytes(), wrong, 'not upgraded, not replaced')
        self.assertEqual(self.calendar.gets, gets, 'not even asked about')
        self.assertEqual(self.calendar.operator_posts, posts, 'and not resubmitted: the operator decides')
        self.assertTrue(any('mismatch' in line and '2026-09-01.json.ots' in line for line in self.log), self.log)
        rows = []
        self.assertFalse(selfstamp.verify_chain(self.state / 'manifests', log=rows.append), rows)

    def test_a_malformed_proof_is_reported_and_left_in_place(self):
        """A pin, not a fix: the reader's 'malformed' is its own verdict (a
        forked proof from the ots client reads as malformed here), so the
        file is never set aside or replaced by the run."""
        self.assertEqual(self.run_tool(P1), 0, self.log)
        ots = self.state / 'manifests' / '2026-09-01.json.ots'
        ots.write_bytes(b'not a proof')
        posts = self.calendar.operator_posts
        self.assertEqual(self.run_tool(P1), 1, self.log)
        self.assertEqual(ots.read_bytes(), b'not a proof')
        self.assertEqual(self.calendar.operator_posts, posts)
        self.assertTrue(any('malformed' in line for line in self.log), self.log)

    def test_pending_is_not_a_failure_and_the_summary_says_what_is_outstanding(self):
        self.assertEqual(self.run_tool(P1), 0, self.log)
        self.assertEqual(self.run_tool(P2), 0, self.log)
        summary = [line for line in self.log if ' summary ' in line]
        self.assertTrue(summary, self.log)
        self.assertIn('pending=2', summary[-1])
        self.assertIn('bitcoin=0', summary[-1])
        self.assertIn('failures=0', summary[-1])
        self.calendar.mined_height = 965600
        self.assertEqual(self.run_tool(P2), 0, self.log)
        summary = [line for line in self.log if ' summary ' in line]
        self.assertIn('bitcoin=2', summary[-1])
        self.assertIn('pending=0', summary[-1])
        # A failure is a local matter: the calendar down at upgrade time.
        (self.state / 'manifests' / '2026-09-02.json.ots').unlink()
        self.calendar.close()
        self.assertEqual(self.run_tool(P2), 1, self.log)
        summary = [line for line in self.log if ' summary ' in line]
        self.assertIn('missing=1', summary[-1])
        self.assertIn('failures=1', summary[-1])

    def test_a_proof_not_of_its_manifest_is_never_exported(self):
        """S8. The outbox publishes a companion only when it is a proof of the
        manifest's bytes with a Bitcoin attestation: attestation presence
        alone let a proof of other bytes travel as this manifest's (gate
        review, P2)."""
        outbox = self.root / 'outbox'
        self.cfg['outbox'] = str(outbox)
        self.assertEqual(self.run_tool(P1), 0, self.log)
        chain = self.manifest('2026-09-01.json')['chain']
        ots = self.state / 'manifests' / '2026-09-01.json.ots'
        ots.write_bytes(proof_bytes(hashlib.sha256(b'other bytes').digest(), 965000))   # a Bitcoin attestation, of other bytes
        self.assertEqual(self.run_tool(P1), 1, self.log)
        self.assertEqual(sorted(p.name for p in outbox.iterdir()), ['%s-2026-09-01.json' % chain])
        self.assertTrue(any('export withheld' in line and 'mismatch' in line for line in self.log), self.log)
        # The right proof, once there, travels.
        ots.unlink()
        self.calendar.mined_height = 965500
        self.assertEqual(self.run_tool(P1), 0, self.log)
        self.assertEqual(sorted(p.name for p in outbox.iterdir()),
                         ['%s-2026-09-01.json' % chain, '%s-2026-09-01.json.ots' % chain])


# --- S6: the inbox ---------------------------------------------------------------------

class WitnessCase(SelfstampCase):
    """A witness box with an inbox, beside the source box of SelfstampCase"""

    def setUp(self):
        super().setUp()
        (self.root / 'wbooks').mkdir()
        (self.root / 'wbooks' / 'receipts.jsonl').write_text('{"txid": "cc", "records": 1}\n')
        self.wcfg = {'calendar_url': self.calendar.url,
                     'books': {'receipts': str(self.root / 'wbooks' / 'receipts.jsonl')},
                     'journal': False, 'fork_head': None}
        self.fresh_witness('0')
        self.wlog = []

    def fresh_witness(self, tag):
        """A new witness state directory and a new inbox: every sweep case
        starts from the same fixture, never from another case's repaired
        state."""
        self.wstate = self.root / ('witness-%s' % tag)
        self.inbox = self.root / ('inbox-%s' % tag)
        self.inbox.mkdir()
        self.wcfg = dict(self.wcfg, state_dir=str(self.wstate), inbox=str(self.inbox))

    def run_witness(self, period, **kwargs):
        return selfstamp.run(self.wcfg, period=period, log=self.wlog.append, **kwargs)

    def copies(self):
        d = self.wstate / 'witnessed'
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []

    def rejected(self):
        d = self.inbox / 'rejected'
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []

    def inbox_names(self):
        return sorted(p.name for p in self.inbox.iterdir() if p.name != 'rejected')

    def delivered(self):
        """The deliveries in the inbox by their delivered names, whether a
        run has claimed them (.claim-<token>-<name>) or not"""
        names = []
        for p in self.inbox.iterdir():
            if p.name == 'rejected':
                continue
            names.append(p.name.split('-', 2)[2] if p.name.startswith('.claim-') else p.name)
        return sorted(names)

    def wmanifest(self, name):
        return json.loads((self.wstate / 'manifests' / name).read_text())

    @staticmethod
    def foreign_manifest(seq=1, period='2026-09-01', chain='ab' * 16, created='2026-09-02T00:30:00Z', **extra):
        m = {'schema': 'selfstamp/3', 'chain': chain, 'seq': seq, 'period': period, 'created_at': created,
             'prev': None, 'books': {}, 'witnessed': []}
        m.update(extra)
        return (json.dumps(m, sort_keys=True, indent=2) + '\n').encode()


class Test_publication_convention(WitnessCase):
    """S6 (precondition). The lock does not cover the deliverer: a file is
    published into the inbox by writing it under a name the tool ignores
    (a leading dot, or any suffix but .json / .json.ots) and renaming it
    into place. Fault model: a delivery caught half way."""

    def test_a_temporary_name_is_never_consumed_and_the_renamed_file_is(self):
        raw = self.foreign_manifest()
        (self.inbox / '.other-2026-09-01.json').write_bytes(raw)          # dotted: still being delivered
        (self.inbox / 'other-2026-09-02.json.part').write_bytes(raw[:-20])  # suffixed: half written
        self.assertEqual(self.run_witness(P1), 0, self.wlog)
        self.assertEqual(self.copies(), [])
        self.assertEqual(self.rejected(), [])
        self.assertEqual(self.inbox_names(), ['.other-2026-09-01.json', 'other-2026-09-02.json.part'])
        os.replace(self.inbox / '.other-2026-09-01.json', self.inbox / 'other-2026-09-01.json')
        self.assertEqual(self.run_witness(P2), 0, self.wlog)
        self.assertEqual(len(self.copies()), 2, self.copies())   # the copy and its own proof
        self.assertEqual(self.inbox_names(), ['other-2026-09-02.json.part'])

    def test_a_half_written_manifest_under_its_final_name_is_quarantined_by_content(self):
        raw = self.foreign_manifest()
        (self.inbox / 'other-2026-09-01.json').write_bytes(raw[:-30])
        self.assertEqual(self.run_witness(P1), 0, self.wlog)
        kept = self.rejected()
        self.assertEqual(kept, ['%s-other-2026-09-01.json' % sha12(raw[:-30])])
        self.assertEqual((self.inbox / 'rejected' / kept[0]).read_bytes(), raw[:-30])
        self.assertTrue(any('inbox rejected' in line and kept[0] in line for line in self.wlog), self.wlog)
        # A second bad delivery under the same name keeps both.
        (self.inbox / 'other-2026-09-01.json').write_bytes(raw[:-10])
        self.assertEqual(self.run_witness(P2), 0, self.wlog)
        self.assertEqual(len(self.rejected()), 2, self.rejected())
        self.assertEqual(self.wmanifest('2026-09-02.json')['witnessed'], [])


class Test_inbox_faults(WitnessCase):
    """S6. One defective, late, replaced or unclaimable item in the inbox:
    quarantined, stored, kept or left visible, and never the end of the
    heartbeat. Fault models: files placed by hand; permission faults by
    chmod; a deliverer that replaces a name while the run holds the file;
    an OSError at the claim rename; the syscalls of a quarantine recorded
    in order."""

    def test_a_rejected_companion_is_quarantined_not_deleted(self):
        raw = self.foreign_manifest()
        digest = hashlib.sha256(raw).digest()
        wrong = proof_bytes(hashlib.sha256(b'other bytes').digest(), 965000)
        (self.inbox / 'other-2026-09-01.json').write_bytes(raw)
        (self.inbox / 'other-2026-09-01.json.ots').write_bytes(wrong)
        self.assertEqual(self.run_witness(P1), 0, self.wlog)
        name = '%s-2026-09-01-%s.json' % ('ab' * 16, digest.hex()[:12])
        self.assertEqual(self.copies(), [name, name + '.ots'])
        self.assertEqual(self.inbox_names(), [])
        kept = self.rejected()
        self.assertEqual(kept, ['%s-other-2026-09-01.json.ots' % sha12(wrong)])
        self.assertEqual((self.inbox / 'rejected' / kept[0]).read_bytes(), wrong)
        self.assertTrue(any('foreign proof rejected' in line for line in self.wlog), self.wlog)
        self.assertEqual(self.wmanifest('2026-09-01.json')['witnessed'][0]['foreign_proof'], None)

    def test_a_proof_that_arrives_after_its_manifest_was_consumed_is_stored(self):
        raw = self.foreign_manifest()
        digest = hashlib.sha256(raw).digest()
        (self.inbox / 'other-2026-09-01.json').write_bytes(raw)
        self.assertEqual(self.run_witness(P1), 0, self.wlog)
        name = '%s-2026-09-01-%s.json' % ('ab' * 16, digest.hex()[:12])
        self.assertNotIn(name + '.foreign.ots', self.copies())
        (self.inbox / 'other-2026-09-01.json.ots').write_bytes(proof_bytes(digest, 965500))   # alone
        self.assertEqual(self.run_witness(P2), 0, self.wlog)
        self.assertIn(name + '.foreign.ots', self.copies())
        self.assertEqual(selfstamp.parse_ots((self.wstate / 'witnessed' / (name + '.foreign.ots')).read_bytes()).attestation,
                         ('bitcoin', 965500))
        self.assertEqual(self.inbox_names(), [])
        self.assertTrue(any('foreign proof' in line and name in line for line in self.wlog), self.wlog)
        rows = []
        self.assertTrue(selfstamp.verify_chain(self.wstate / 'manifests', log=rows.append), rows)
        self.assertTrue(any('foreign_now=bitcoin height=965500' in r for r in rows), rows)

    def test_a_proof_whose_manifest_has_not_arrived_stays_claimed_and_is_named_every_run(self):
        raw = self.foreign_manifest()
        digest = hashlib.sha256(raw).digest()
        (self.inbox / 'other-2026-09-01.json.ots').write_bytes(proof_bytes(digest))
        for period in (P1, P2):
            self.assertEqual(self.run_witness(period), 0, self.wlog)
            self.assertEqual(self.delivered(), ['other-2026-09-01.json.ots'])
            self.assertTrue(all(n.startswith('.claim-') for n in self.inbox_names()), 'kept as a claim of this tool')
        self.assertEqual(sum('awaiting its manifest' in line for line in self.wlog), 2, self.wlog)
        self.assertEqual(sorted(p.name for p in (self.wstate / 'manifests').glob('*.json')),
                         ['2026-09-01.json', '2026-09-02.json'])
        # Its manifest arrives, under another name: both are consumed.
        (self.inbox / 'x.json').write_bytes(raw)
        self.assertEqual(self.run_witness(P3), 0, self.wlog)
        self.assertEqual(self.inbox_names(), [])
        self.assertEqual(len([c for c in self.copies() if c.endswith('.foreign.ots')]), 1)
        # A proof of bytes that never come stays visible for as long as it takes.
        (self.inbox / 'stranger.json.ots').write_bytes(proof_bytes(hashlib.sha256(b'unseen').digest()))
        self.assertEqual(self.run_witness(datetime.date(2026, 9, 4)), 0, self.wlog)
        self.assertEqual(self.delivered(), ['stranger.json.ots'])

    def test_a_malformed_orphan_proof_is_quarantined(self):
        (self.inbox / 'other-2026-09-01.json.ots').write_bytes(b'\x00garbage')
        self.assertEqual(self.run_witness(P1), 0, self.wlog)
        self.assertEqual(self.inbox_names(), [])
        self.assertEqual(self.rejected(), ['%s-other-2026-09-01.json.ots' % sha12(b'\x00garbage')])

    def test_an_unreadable_inbox_file_is_counted_and_the_rest_goes_on(self):
        good, bad = self.foreign_manifest(seq=1), self.foreign_manifest(seq=2, period='2026-09-02')
        (self.inbox / 'a-2026-09-01.json').write_bytes(good)
        (self.inbox / 'b-2026-09-02.json').write_bytes(bad)
        self.addCleanup(unreadable(self.inbox / 'b-2026-09-02.json'))
        self.assertEqual(self.run_witness(P1), 1, self.wlog)
        self.assertEqual(sorted(p.name for p in (self.wstate / 'manifests').glob('*.json')), ['2026-09-01.json'],
                         'the heartbeat was written')
        self.assertEqual(len(self.wmanifest('2026-09-01.json')['witnessed']), 1)
        self.assertEqual(self.delivered(), ['b-2026-09-02.json'], 'claimed, and left for the next run')
        self.assertTrue(any('inbox' in line and 'b-2026-09-02.json' in line and 'PermissionError' in line
                            for line in self.wlog), self.wlog)

    def test_an_unreadable_inbox_directory_is_counted_and_the_heartbeat_is_written(self):
        (self.inbox / 'a-2026-09-01.json').write_bytes(self.foreign_manifest())
        self.addCleanup(unreadable(self.inbox))
        self.assertEqual(self.run_witness(P1), 1, self.wlog)
        self.assertEqual(sorted(p.name for p in (self.wstate / 'manifests').glob('*.json')), ['2026-09-01.json'])
        self.assertTrue(any('inbox' in line and 'unreadable' in line for line in self.wlog), self.wlog)

    def test_conflicting_deliveries_for_one_seq_are_both_kept_and_both_listed(self):
        first = self.foreign_manifest(created='2026-09-02T00:30:00Z')
        second = self.foreign_manifest(created='2026-09-02T00:31:00Z')   # the same chain, seq and period; other bytes
        self.assertNotEqual(first, second)
        (self.inbox / 'v1.json').write_bytes(first)
        self.assertEqual(self.run_witness(P1), 0, self.wlog)
        (self.inbox / 'v2.json').write_bytes(second)
        self.assertEqual(self.run_witness(P2), 0, self.wlog)
        copies = [c for c in self.copies() if c.endswith('.json')]
        self.assertEqual(len(copies), 2, copies)
        entries = self.wmanifest('2026-09-01.json')['witnessed'] + self.wmanifest('2026-09-02.json')['witnessed']
        self.assertEqual(sorted(e['sha256'] for e in entries),
                         sorted(hashlib.sha256(b).hexdigest() for b in (first, second)))
        rows = []
        self.assertTrue(selfstamp.verify_chain(self.wstate / 'manifests', log=rows.append), rows)
        # Seen from the source: whichever version it holds is vouched for,
        # and the other version is named.
        for version in (first, second):
            src = self.root / 'src'
            shutil.rmtree(src, ignore_errors=True)
            src.mkdir()
            (src / '2026-09-01.json').write_bytes(version)
            rows = []
            self.assertTrue(selfstamp.cross_check(src, self.wstate / 'manifests', rows.append), rows)
            self.assertTrue(any('witnessed by' in r and '2 versions' in r for r in rows), rows)

    def test_a_delivery_replaced_under_its_name_while_being_consumed_is_kept(self):
        """The deliverer follows the convention and renames a second file
        over the first one's name while the run is copying the first: the
        second is not removed under it; it waits for the next run, which
        witnesses it too (gate review, P1)."""
        first = self.foreign_manifest(created='2026-09-02T00:30:00Z')
        second = self.foreign_manifest(created='2026-09-02T00:31:00Z')
        delivery = self.inbox / 'other-2026-09-01.json'
        delivery.write_bytes(first)
        real = selfstamp.write_atomic
        replaced = []

        def concurrent_delivery(path, data):
            real(path, data)
            if pathlib.Path(path).parent == self.wstate / 'witnessed' and not replaced:
                tmp = self.inbox / '.next.tmp'
                tmp.write_bytes(second)
                os.replace(tmp, delivery)
                replaced.append(True)
        with mock.patch.object(selfstamp, 'write_atomic', concurrent_delivery):
            self.assertEqual(self.run_witness(P1), 0, self.wlog)
        self.assertEqual(replaced, [True])
        self.assertEqual(self.delivered(), ['other-2026-09-01.json'], 'the second delivery is still there')
        self.assertEqual(self.run_witness(P2), 0, self.wlog)
        held = sorted(p.read_bytes() for p in (self.wstate / 'witnessed').glob('*.json'))
        self.assertEqual(held, sorted([first, second]))
        entries = self.wmanifest('2026-09-01.json')['witnessed'] + self.wmanifest('2026-09-02.json')['witnessed']
        self.assertEqual(sorted(e['sha256'] for e in entries),
                         sorted(hashlib.sha256(b).hexdigest() for b in (first, second)))

    def test_a_claim_that_cannot_be_made_is_counted_and_the_delivery_stays(self):
        raw = self.foreign_manifest()
        (self.inbox / 'other-2026-09-01.json').write_bytes(raw)
        with fail_on_call(selfstamp.os, 'rename', 1) as hit:
            self.assertEqual(self.run_witness(P1), 1, self.wlog)
        self.assertTrue(hit['fired'])
        self.assertEqual(self.inbox_names(), ['other-2026-09-01.json'], 'unclaimed, under its own name')
        self.assertTrue(any('inbox error' in line and 'other-2026-09-01.json' in line for line in self.wlog), self.wlog)
        self.assertEqual(self.run_witness(P2), 0, self.wlog)
        self.assertEqual(self.inbox_names(), [])
        self.assertEqual(len(self.wmanifest('2026-09-02.json')['witnessed']), 1)

    def test_a_claim_left_by_a_dead_run_is_resumed(self):
        raw = self.foreign_manifest()
        (self.inbox / '.claim-deadbeef-other-2026-09-01.json').write_bytes(raw)
        (self.inbox / '.claim-deadbeef-other-2026-09-01.json.ots').write_bytes(proof_bytes(hashlib.sha256(raw).digest(), 965500))
        self.assertEqual(self.run_witness(P1), 0, self.wlog)
        self.assertEqual(self.inbox_names(), [])
        name = '%s-2026-09-01-%s.json' % ('ab' * 16, sha12(raw))
        self.assertIn(name + '.foreign.ots', self.copies())
        self.assertEqual(self.wmanifest('2026-09-01.json')['witnessed'][0]['foreign_proof'], 'bitcoin height=965500')

    def test_a_held_foreign_proof_of_other_bytes_never_outranks_a_proof_of_the_copy(self):
        """What is held counts only if it is a proof of the copy: a held file
        with a Bitcoin attestation for other bytes is set aside, and the
        arriving pending proof of the copy is kept (gate review, P2)."""
        raw = self.foreign_manifest()
        digest = hashlib.sha256(raw).digest()
        (self.inbox / 'other-2026-09-01.json').write_bytes(raw)
        self.assertEqual(self.run_witness(P1), 0, self.wlog)
        name = '%s-2026-09-01-%s.json' % ('ab' * 16, digest.hex()[:12])
        held = self.wstate / 'witnessed' / (name + '.foreign.ots')
        wrong = proof_bytes(hashlib.sha256(b'other').digest(), 965000)
        held.write_bytes(wrong)
        rows = []
        self.assertTrue(selfstamp.verify_chain(self.wstate / 'manifests', log=rows.append), rows)
        self.assertTrue(any('foreign_now=mismatch' in r for r in rows), rows)
        correct = proof_bytes(digest)   # pending, of the copy
        (self.inbox / 'late.json.ots').write_bytes(correct)
        self.assertEqual(self.run_witness(P2), 0, self.wlog)
        self.assertEqual(held.read_bytes(), correct)
        aside = held.with_name(held.name + '.rejected-' + sha12(wrong))
        self.assertEqual(aside.read_bytes(), wrong, 'set aside, not deleted')
        self.assertTrue(any('foreign proof set aside' in line for line in self.wlog), self.wlog)
        self.assertEqual(self.inbox_names(), [])
        rows = []
        self.assertTrue(selfstamp.verify_chain(self.wstate / 'manifests', log=rows.append), rows)
        self.assertTrue(any('foreign_now=pending' in r for r in rows), rows)

    def test_quarantine_is_durable_before_it_is_acknowledged(self):
        """A rejected delivery's bytes are fsynced, the file renamed into
        rejected/, then rejected/ and the inbox fsynced, and only then is the
        rejection logged; a new rejected/ has its parent entry fsynced before
        anything is moved into it. Fault model: none; the syscalls are
        recorded in order (gate review, P5). This shows the barriers are
        there, not what a power cut would do."""
        events = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(fd):
            st = os.fstat(fd)
            events.append(('fsync', 'dir' if stat.S_ISDIR(st.st_mode) else 'file', st.st_ino))
            return real_fsync(fd)

        def replace(src, dst):
            events.append(('replace', str(dst)))
            return real_replace(src, dst)

        def logger(line):
            events.append(('log', line))
            self.wlog.append(line)
        bad, comp = b'{not a manifest', proof_bytes(hashlib.sha256(b'x').digest())
        (self.inbox / 'bad.json').write_bytes(bad)
        (self.inbox / 'bad.json.ots').write_bytes(comp)
        with mock.patch.object(selfstamp.os, 'fsync', fsync), mock.patch.object(selfstamp.os, 'replace', replace):
            self.assertEqual(selfstamp.run(self.wcfg, period=P1, log=logger), 0, self.wlog)
        rejected = self.inbox / 'rejected'
        kept = sorted(rejected.iterdir())
        self.assertEqual([k.name for k in kept], sorted(['%s-bad.json' % sha12(bad), '%s-bad.json.ots' % sha12(comp)]))
        rejected_ino, inbox_ino = os.stat(rejected).st_ino, os.stat(self.inbox).st_ino
        moves = [i for i, e in enumerate(events) if e[0] == 'replace' and e[1].startswith(str(rejected))]
        self.assertLess(events.index(('fsync', 'dir', inbox_ino)), min(moves),
                        'the new rejected/ entry is durable before anything is moved into it')
        for k in kept:
            ino = os.stat(k).st_ino
            i_file = events.index(('fsync', 'file', ino))
            i_move = events.index(('replace', str(k)))
            i_dir = next(i for i, e in enumerate(events) if i > i_move and e == ('fsync', 'dir', rejected_ino))
            i_inbox = next(i for i, e in enumerate(events) if i > i_move and e == ('fsync', 'dir', inbox_ino))
            i_log = next(i for i, e in enumerate(events) if e[0] == 'log' and 'rejected' in e[1] and k.name in e[1])
            self.assertLess(i_file, i_move, 'the bytes before the rename')
            self.assertLess(i_move, i_dir, 'the destination name after the rename')
            self.assertLess(i_move, i_inbox, 'the source directory too')
            self.assertLess(max(i_dir, i_inbox), i_log, 'acknowledged only once durable')


class Test_witnessed_copies_at_run_time(WitnessCase):
    """S7. A retained copy that cannot be read or parsed when the next
    manifest folds the vouches in: named, counted, not folded, and the
    heartbeat goes on. Fault model: chmod and an overwrite between runs."""

    def deliver_two(self):
        (self.inbox / 'a.json').write_bytes(self.foreign_manifest(seq=1))
        (self.inbox / 'b.json').write_bytes(self.foreign_manifest(seq=2, period='2026-09-02'))
        with mock.patch.object(selfstamp, 'witnessed_entries', return_value=([], 0)):
            # The copies are made, and not yet listed by any manifest.
            self.assertEqual(self.run_witness(P1), 0, self.wlog)
        return sorted((self.wstate / 'witnessed').glob('*.json'))

    def test_an_unreadable_copy_is_named_not_folded_and_folded_later(self):
        a, b = self.deliver_two()
        restore = unreadable(a)
        self.assertEqual(self.run_witness(P2), 1, self.wlog)
        listed = [e['file'] for e in self.wmanifest('2026-09-02.json')['witnessed']]
        self.assertEqual(listed, [b.name])
        self.assertTrue(any('copy unreadable' in line and a.name in line for line in self.wlog), self.wlog)
        restore()
        self.assertEqual(self.run_witness(P3), 0, self.wlog)
        self.assertEqual([e['file'] for e in self.wmanifest('2026-09-03.json')['witnessed']], [a.name])

    def test_a_copy_that_no_longer_parses_is_named_and_counted(self):
        a, b = self.deliver_two()
        a.write_bytes(b'{broken')
        self.assertEqual(self.run_witness(P2), 1, self.wlog)
        self.assertEqual([e['file'] for e in self.wmanifest('2026-09-02.json')['witnessed']], [b.name])
        self.assertTrue(any('copy unreadable' in line and a.name in line for line in self.wlog), self.wlog)


# --- S6/S7: interruption ---------------------------------------------------------------

class Test_interruption(WitnessCase):
    """S6-S8. A run that stops anywhere leaves a state the next run finishes
    from, and stopping again while it finishes changes nothing. Fault
    models: an OSError injected at the n-th call of one call family of a
    fresh run (rename: the claims; replace: every file written whole and
    every quarantine move; unlink: every removal; fsync: every durability
    barrier), swept from n = 1 until a fresh run has no n-th call, every
    case from its own fresh fixture, every injection asserted to have
    fired, and the recovery of every case interrupted once more at its
    own first call of the family; a removal that fails once; a real child
    process paused at a named boundary and killed."""

    def deliver_pair(self):
        raw = self.foreign_manifest()
        (self.inbox / 'other-2026-09-01.json').write_bytes(raw)
        (self.inbox / 'other-2026-09-01.json.ots').write_bytes(proof_bytes(hashlib.sha256(raw).digest(), 965500))
        return raw

    def converged(self, raw):
        digest = hashlib.sha256(raw).hexdigest()
        name = '%s-2026-09-01-%s.json' % ('ab' * 16, digest[:12])
        self.assertEqual(self.inbox_names(), [])
        self.assertEqual(self.rejected(), [])
        self.assertEqual([c for c in self.copies() if c.endswith('.json')], [name])
        self.assertIn(name + '.foreign.ots', self.copies())
        self.assertEqual((self.wstate / 'witnessed' / name).read_bytes(), raw)
        entries = [e for n in sorted((self.wstate / 'manifests').glob('*.json'))
                   for e in json.loads(n.read_bytes())['witnessed']]
        self.assertEqual([e['sha256'] for e in entries], [digest], 'vouched exactly once')
        rows = []
        self.assertTrue(selfstamp.verify_chain(self.wstate / 'manifests', log=rows.append), rows)

    def deliver_bad_pair(self):
        bad, comp = b'{not a manifest', proof_bytes(hashlib.sha256(b'x').digest())
        (self.inbox / 'bad.json').write_bytes(bad)
        (self.inbox / 'bad.json.ots').write_bytes(comp)
        return bad, comp

    def quarantined(self, payload):
        bad, comp = payload
        self.assertEqual(self.inbox_names(), [])
        self.assertEqual(self.rejected(), sorted(['%s-bad.json' % sha12(bad), '%s-bad.json.ots' % sha12(comp)]))
        self.assertEqual([c for c in self.copies() if c.endswith('.json')], [])
        self.assertEqual(self.wmanifest('2026-09-01.json')['witnessed'], [])
        rows = []
        self.assertTrue(selfstamp.verify_chain(self.wstate / 'manifests', log=rows.append), rows)

    def sweep(self, family, deliver, converged):
        """One fresh fixture per boundary: the run's n-th call of `family`
        raises; the injection must have fired, else a fresh run has no n-th
        call and the sweep is over; the recovery is interrupted once more at
        its own first call, then finished, and the next period run; the
        state must have converged. Returns how many boundaries fired."""
        boundaries = 0
        for n in range(1, 40):
            self.fresh_witness('%s-%d' % (family, n))
            payload = deliver()
            with fail_on_call(selfstamp.os, family, n) as hit:
                try:
                    rc = self.run_witness(P1)
                except OSError:
                    rc = 'raised'
            if not hit['fired']:
                self.assertEqual(rc, 0, (family, n, self.wlog))
                break
            boundaries += 1
            self.assertNotEqual(rc, 0, 'an injected stop (%s #%d) is never exit 0' % (family, n))
            with fail_on_call(selfstamp.os, family, 1):   # the recovery, interrupted once more
                try:
                    self.run_witness(P1)
                except OSError:
                    pass
            self.assertEqual(self.run_witness(P1), 0, (family, n, self.wlog))
            self.assertEqual(self.run_witness(P2), 0, (family, n, self.wlog))
            converged(payload)
        else:
            self.fail('%s: the sweep never ran out of boundaries' % family)
        return boundaries

    def test_every_boundary_of_a_fresh_inbox_pass_is_finished_by_the_next_run(self):
        counts = {family: self.sweep(family, self.deliver_pair, self.converged)
                  for family in ('rename', 'replace', 'unlink', 'fsync')}
        self.assertEqual(counts, {'rename': 2, 'replace': 5, 'unlink': 2, 'fsync': 13},
                         'a fresh run with one manifest and its proof: two claims; the copy, the foreign proof, '
                         'the manifest and two own proofs written whole; two removals; thirteen fsyncs')

    def test_every_boundary_of_a_quarantine_is_finished_by_the_next_run(self):
        counts = {family: self.sweep(family, self.deliver_bad_pair, self.quarantined)
                  for family in ('rename', 'replace', 'unlink', 'fsync')}
        self.assertEqual(counts, {'rename': 2, 'replace': 4, 'unlink': 0, 'fsync': 14},
                         'a fresh run with a bad pair: two claims; two moves into rejected/, the manifest and its '
                         'proof written whole; no removal; fourteen fsyncs')

    def test_a_stop_between_the_copy_and_the_removal_is_a_duplicate_next_run(self):
        raw = self.deliver_pair()
        with fail_on_call(selfstamp, '_remove', 1) as hit:
            rc = self.run_witness(P1)
        self.assertTrue(hit['fired'])
        self.assertEqual(rc, 1, self.wlog)
        self.assertEqual(self.run_witness(P1), 0, self.wlog)
        self.assertTrue(any('inbox duplicate' in line for line in self.wlog), self.wlog)
        self.converged(raw)

    CHILD = """
import datetime, importlib.util, json, sys
spec = importlib.util.spec_from_file_location('selfstamp', sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
cfg = json.load(open(sys.argv[2])); boundary = sys.argv[3]

def pause(name):
    if name == boundary:
        print('paused at ' + name, flush=True)
        sys.stdin.readline()
real_write = m.write_atomic
def write_atomic(path, data):
    real_write(path, data)
    p = str(path)
    if p.endswith('.json') and '/manifests/' in p:
        pause('manifest written')
    if p.endswith('.json') and '/witnessed/' in p:
        pause('copy written')
m.write_atomic = write_atomic
real_submit = m.submit_digest
def submit_digest(url, digest, timeout=30):
    r = real_submit(url, digest, timeout)
    pause('submitted')
    return r
m.submit_digest = submit_digest
sys.exit(m.run(cfg, period=datetime.date.fromisoformat(sys.argv[4]), log=lambda s: print(s, flush=True)))
"""

    def kill_at(self, cfg, boundary, period='2026-09-01'):
        """Run the tool in a child, let it reach `boundary`, kill -9 it.
        Deterministic: the child prints where it stands and blocks on
        stdin; the parent reads that line and only then kills."""
        cfg_path = self.root / ('child-%s.json' % boundary.replace(' ', '-'))
        cfg_path.write_text(json.dumps(cfg))
        child = subprocess.Popen([sys.executable, '-c', self.CHILD, str(TOOL), str(cfg_path), boundary, period],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        lines = []
        try:
            for line in child.stdout:
                lines.append(line.rstrip('\n'))
                if line.startswith('paused at ' + boundary):
                    break
            else:
                self.fail('the child never reached %r: %s' % (boundary, lines))
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(30)
            child.stdin.close()
            child.stdout.close()
        return lines

    def test_a_run_killed_after_writing_its_manifest_resubmits_at_the_next_start(self):
        self.kill_at(self.cfg, 'manifest written')
        self.assertEqual(self.manifests(), ['2026-09-01.json'])
        self.assertFalse((self.state / 'manifests' / '2026-09-01.json.ots').exists())
        self.assertEqual(self.calendar.operator_posts, 0)
        before = (self.state / 'manifests' / '2026-09-01.json').read_bytes()
        self.assertEqual(self.run_tool(P1, lock_wait=5), 0, self.log)   # the dead run's lock is gone
        self.assertEqual((self.state / 'manifests' / '2026-09-01.json').read_bytes(), before)
        self.assertEqual(self.calendar.operator_posts, 1)
        self.assertTrue(any('noop' in line for line in self.log) and any('submitted' in line for line in self.log), self.log)

    def test_a_run_killed_between_the_answer_and_the_proof_write_resubmits_the_same_bytes(self):
        self.kill_at(self.cfg, 'submitted')
        self.assertEqual(self.calendar.operator_posts, 1)
        self.assertFalse((self.state / 'manifests' / '2026-09-01.json.ots').exists())
        self.assertEqual(self.run_tool(P1, lock_wait=5), 0, self.log)
        self.assertEqual(self.calendar.operator_posts, 2, 'the same digest, submitted again')
        raw = (self.state / 'manifests' / '2026-09-01.json').read_bytes()
        proof = selfstamp.parse_ots((self.state / 'manifests' / '2026-09-01.json.ots').read_bytes())
        self.assertEqual(proof.digest, hashlib.sha256(raw).digest())

    def test_a_witness_killed_after_the_copy_finishes_the_delivery_at_the_next_start(self):
        raw = self.deliver_pair()
        self.kill_at(self.wcfg, 'copy written')
        name = '%s-2026-09-01-%s.json' % ('ab' * 16, sha12(raw))
        self.assertEqual([c for c in self.copies() if c.endswith('.json')], [name])
        self.assertEqual(self.delivered(), ['other-2026-09-01.json', 'other-2026-09-01.json.ots'],
                         'the claimed pair is still there: the copy came first')
        self.assertEqual(self.run_witness(P1, lock_wait=5), 0, self.wlog)
        self.converged(raw)


# --- S8: readers -------------------------------------------------------------------------

class Test_concurrent_readers(SelfstampCase):
    """S8. verify needs no lock: every file is replaced whole, so a reader
    between two writes of one run sees a chain that holds, with the proof
    not yet there. Fault model: none; the reader is called from inside
    the writer at the boundary."""

    def test_verify_between_the_manifest_and_its_proof_sees_a_whole_chain(self):
        self.assertEqual(self.run_tool(P1), 0, self.log)
        seen = []
        real = selfstamp.write_atomic

        def write_then_read(path, data):
            real(path, data)
            if str(path).endswith('2026-09-02.json'):
                rows = []
                seen.append((selfstamp.verify_chain(self.state / 'manifests', log=rows.append), rows))
        with mock.patch.object(selfstamp, 'write_atomic', write_then_read):
            self.assertEqual(self.run_tool(P2), 0, self.log)
        self.assertEqual(len(seen), 1)
        ok, rows = seen[0]
        self.assertTrue(ok, rows)
        self.assertTrue(any('2026-09-02.json' in r and 'proof=missing' in r for r in rows), rows)
        self.assertTrue(any('missing=1' in r for r in rows), rows)


# --- S9: verify ---------------------------------------------------------------------------

class Test_verify_states(WitnessCase):
    """S9. The states verify tells apart and what each means; a file it
    cannot read is a break, never a traceback; a witness directory that is
    not there is a break; witness evidence that cannot all be read is not
    a success. Fault model: chmod, absent paths, a corrupt witness file."""

    def chain_with_a_vouch(self):
        raw = self.foreign_manifest()
        (self.inbox / 'other-2026-09-01.json').write_bytes(raw)
        (self.inbox / 'other-2026-09-01.json.ots').write_bytes(proof_bytes(hashlib.sha256(raw).digest()))
        self.assertEqual(self.run_witness(P1), 0, self.wlog)
        return next((self.wstate / 'witnessed').glob('*.json'))

    def test_an_unreadable_copy_or_manifest_or_proof_is_a_break_not_a_traceback(self):
        copy = self.chain_with_a_vouch()
        for target, word in ((copy, 'copy=unreadable'),
                             (self.wstate / 'manifests' / '2026-09-01.json.ots', 'proof=unreadable'),
                             (self.wstate / 'manifests' / '2026-09-01.json', 'unreadable')):
            restore = unreadable(target)
            try:
                rows = []
                ok = selfstamp.verify_chain(self.wstate / 'manifests', log=rows.append)
            finally:
                restore()
            self.assertFalse(ok, rows)
            self.assertTrue(any(word in r for r in rows), (word, rows))
        rows = []
        self.assertTrue(selfstamp.verify_chain(self.wstate / 'manifests', log=rows.append), rows)

    def test_a_missing_witness_directory_is_a_break(self):
        self.assertEqual(self.run_tool(P1), 0, self.log)
        rows = []
        self.assertFalse(selfstamp.verify_chain(self.state / 'manifests', witness=self.root / 'nowhere', log=rows.append), rows)
        self.assertTrue(any('witness' in r and 'missing' in r for r in rows), rows)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(selfstamp.main(['verify', '--manifests', str(self.state / 'manifests'),
                                             '--witness', str(self.root / 'nowhere')]), 1)

    def test_the_states_are_named_and_attestation_presence_is_not_verification(self):
        copy = self.chain_with_a_vouch()
        rows = []
        self.assertTrue(selfstamp.verify_chain(self.wstate / 'manifests', log=rows.append), rows)
        vouch = next(r for r in rows if 'vouches for' in r)
        self.assertIn('chain=%s' % ('ab' * 16), vouch)
        self.assertIn('copy=ok', vouch)
        self.assertIn('proof=pending', vouch)
        self.assertIn('foreign_proof=pending', vouch)
        self.assertNotIn('host=', vouch)
        self.assertTrue(any('not checked against Bitcoin' in r for r in rows), rows)
        # A stronger foreign proof later: the entry keeps what it knew, the
        # line says what is held now.
        (copy.with_name(copy.name + '.foreign.ots')).write_bytes(proof_bytes(hashlib.sha256(copy.read_bytes()).digest(), 965700))
        rows = []
        self.assertTrue(selfstamp.verify_chain(self.wstate / 'manifests', log=rows.append), rows)
        vouch = next(r for r in rows if 'vouches for' in r)
        self.assertIn('foreign_proof=pending', vouch)
        self.assertIn('foreign_now=bitcoin height=965700', vouch)

    def test_unreadable_witness_evidence_is_incomplete_not_a_success(self):
        """A witness manifest that cannot be read or is not a manifest is
        named, the rest is still checked, the result is not success, and the
        last line says the check was incomplete (gate review, P3)."""
        self.assertEqual(self.run_tool(P1), 0, self.log)
        mine = self.state / 'manifests'
        raw = (mine / '2026-09-01.json').read_bytes()
        wdir = self.root / 'witness-manifests'
        wdir.mkdir()
        entry = {'chain': json.loads(raw)['chain'], 'seq': 1, 'period': '2026-09-01', 'file': 'x.json',
                 'sha256': hashlib.sha256(raw).hexdigest(), 'witnessed_at': '2026-09-02T01:00:00Z', 'foreign_proof': None}
        (wdir / '2026-09-01.json').write_bytes(self.foreign_manifest(witnessed=[entry]))
        (wdir / '2026-09-02.json').write_bytes(b'{not json')
        rows = []
        self.assertFalse(selfstamp.verify_chain(mine, witness=wdir, log=rows.append), rows)
        self.assertTrue(any('witness manifest unreadable: 2026-09-02.json' in r for r in rows), rows)
        self.assertTrue(any('witnessed by' in r for r in rows), 'the readable evidence is still used')
        self.assertTrue(any(r.startswith('witness check=incomplete') for r in rows), rows)
        (wdir / '2026-09-02.json').unlink()
        rows = []
        self.assertTrue(selfstamp.verify_chain(mine, witness=wdir, log=rows.append), rows)
        self.assertTrue(any(r.startswith('witness check=ok') for r in rows), rows)


# --- legacy: selfstamp/2 read, continued, witnessed -----------------------------------------

class Test_legacy_compatibility(WitnessCase):
    """S3 / S9. Manifests written under selfstamp/2 (ops/tests/selfstamp/v2)
    verify unchanged, continue under this code with a label, and are
    witnessed by this code without their host name being written anywhere
    new; a cross-check of an unlabelled chain says what it can and no
    more. Fault model: none."""

    def setUp(self):
        super().setUp()
        self.legacy = self.root / 'legacy'
        shutil.copytree(FIXTURE, self.legacy)

    def test_a_selfstamp_2_pair_of_chains_still_verifies_byte_for_byte(self):
        a, b = self.legacy / 'legacy-a' / 'manifests', self.legacy / 'legacy-b' / 'manifests'
        rows = []
        self.assertTrue(selfstamp.verify_chain(a, log=rows.append), rows)
        self.assertTrue(any('commissioned host=legacybox' in r for r in rows), rows)
        rows = []
        self.assertTrue(selfstamp.verify_chain(b, log=rows.append), rows)
        self.assertTrue(any('vouches for' in r and 'host=legacybox' in r and 'copy=ok' in r for r in rows), rows)
        rows = []
        self.assertTrue(selfstamp.verify_chain(a, witness=b, log=rows.append), rows)
        self.assertEqual(sum('witnessed by' in r for r in rows), 2, rows)

    def test_a_legacy_chain_continued_here_gets_a_label_and_keeps_its_links(self):
        state = self.root / 'continued'
        shutil.copytree(self.legacy / 'legacy-a', state)
        before = {p.name: sha256_hex(p) for p in (state / 'manifests').iterdir()}
        cfg = dict(self.cfg, state_dir=str(state), outbox=str(self.root / 'outbox'))
        self.assertEqual(selfstamp.run(cfg, period=P3, log=self.log.append), 0, self.log)
        m = json.loads((state / 'manifests' / '2026-09-03.json').read_text())
        self.assertEqual((m['schema'], m['seq'], m['prev']['file']), ('selfstamp/3', 3, '2026-09-02.json'))
        self.assertEqual(m['prev']['sha256'], before['2026-09-02.json'])
        self.assertRegex(m['chain'], HEX32)
        self.assertNotIn('host', m)
        self.assertEqual({p.name: sha256_hex(p) for p in (state / 'manifests').iterdir() if p.name in before}, before,
                         'anchored history is never rewritten')
        rows = []
        self.assertTrue(selfstamp.verify_chain(state / 'manifests', log=rows.append), rows)
        self.assertEqual(selfstamp.run(cfg, period=datetime.date(2026, 9, 4), log=self.log.append), 0, self.log)
        self.assertEqual(json.loads((state / 'manifests' / '2026-09-04.json').read_text())['chain'], m['chain'])
        # The outbox: legacy names for legacy manifests, the label for the new ones.
        self.assertEqual(sorted(p.name for p in (self.root / 'outbox').iterdir()),
                         ['%s-2026-09-03.json' % m['chain'], '%s-2026-09-04.json' % m['chain'],
                          'legacybox-2026-09-01.json', 'legacybox-2026-09-01.json.ots', 'legacybox-2026-09-02.json'])

    def test_a_legacy_manifest_in_the_inbox_is_recorded_without_its_host_name(self):
        for p in (self.legacy / 'outbox').iterdir():
            shutil.copy2(p, self.inbox / p.name)
        self.assertEqual(self.run_witness(P1), 0, self.wlog)
        copies = self.copies()
        genesis = (self.legacy / 'outbox' / 'legacybox-2026-09-01.json').read_bytes()
        sha = hashlib.sha256(genesis).hexdigest()
        self.assertIn('legacy-2026-09-01-%s.json' % sha[:12], copies)
        self.assertIn('legacy-2026-09-01-%s.json.foreign.ots' % sha[:12], copies)
        self.assertFalse(any('legacybox' in c for c in copies), copies)
        self.assertFalse(any('legacybox' in line for line in self.wlog), self.wlog)
        entries = self.wmanifest('2026-09-01.json')['witnessed']
        self.assertEqual(len(entries), 2)
        self.assertEqual({e['chain'] for e in entries}, {None})
        self.assertNotIn('host', entries[0])
        self.assertNotIn(b'legacybox', (self.wstate / 'manifests' / '2026-09-01.json').read_bytes())
        # The legacy source cross-checks against this witness by seq and period.
        a = self.legacy / 'legacy-a' / 'manifests'
        rows = []
        self.assertTrue(selfstamp.verify_chain(a, witness=self.wstate / 'manifests', log=rows.append), rows)
        self.assertEqual(sum('witnessed by' in r for r in rows), 2, rows)
        # Altered after the fact: its own proof no longer matches (a break),
        # and the witness holds another hash under an identity a label
        # would make unique, which is said as ambiguous, not as a mismatch.
        path = a / '2026-09-02.json'
        path.write_bytes(path.read_bytes() + b'\n')
        rows = []
        self.assertFalse(selfstamp.verify_chain(a, witness=self.wstate / 'manifests', log=rows.append), rows)
        self.assertTrue(any('mismatch' in r and '2026-09-02.json' in r and 'proof=' in r for r in rows), rows)
        self.assertTrue(any('WITNESS AMBIGUOUS' in r and '2026-09-02.json' in r for r in rows), rows)
        self.assertFalse(any('WITNESS MISMATCH' in r for r in rows), rows)

    def test_an_unrelated_legacy_chain_is_ambiguous_not_a_mismatch(self):
        """Two chains written under selfstamp/2 that began the same day share
        seq and period and, at a witness, nothing else: a cross-check of one
        against evidence about the other says AMBIGUOUS, not MISMATCH, and
        is not a break; a labelled chain in the same position is a mismatch,
        because a label names one chain (gate review, P3)."""
        def legacy(host):
            return (json.dumps({'schema': 'selfstamp/2', 'host': host, 'seq': 1, 'period': '2026-09-01', 'prev': None,
                                'commissioning': {}, 'witnessed': []}, sort_keys=True, indent=2) + '\n').encode()
        mine, witness = self.root / 'mine', self.root / 'witness-manifests'
        mine.mkdir()
        witness.mkdir()
        (mine / '2026-09-01.json').write_bytes(legacy('A'))
        entry = {'chain': None, 'seq': 1, 'period': '2026-09-01', 'file': 'legacy-2026-09-01-000000000000.json',
                 'sha256': hashlib.sha256(legacy('B')).hexdigest(), 'witnessed_at': '2026-09-02T01:00:00Z', 'foreign_proof': None}
        (witness / '2026-09-01.json').write_bytes(self.foreign_manifest(witnessed=[entry]))
        rows = []
        self.assertTrue(selfstamp.cross_check(mine, witness, rows.append), rows)
        self.assertFalse(any('WITNESS MISMATCH' in r for r in rows), rows)
        self.assertTrue(any('WITNESS AMBIGUOUS' in r for r in rows), rows)
        self.assertTrue(any(r.startswith('witness check=ok') and 'ambiguous=1' in r for r in rows), rows)
        labelled = self.root / 'labelled'
        labelled.mkdir()
        (labelled / '2026-09-01.json').write_bytes(self.foreign_manifest(chain='cd' * 16, created='2026-09-02T00:30:00Z'))
        other = self.foreign_manifest(chain='cd' * 16, created='2026-09-02T00:31:00Z')
        entry = dict(entry, chain='cd' * 16, sha256=hashlib.sha256(other).hexdigest())
        (witness / '2026-09-01.json').write_bytes(self.foreign_manifest(witnessed=[entry]))
        rows = []
        self.assertFalse(selfstamp.cross_check(labelled, witness, rows.append), rows)
        self.assertTrue(any('WITNESS MISMATCH' in r for r in rows), rows)

    def test_an_older_witness_would_quarantine_a_labelled_manifest(self):
        """What the other side of the compatibility rule looks like: the
        selfstamp/2 reader (its parser reproduced here from the fixture's
        code) refuses a selfstamp/3 manifest by schema, so an unupgraded
        witness moves it to rejected/ and loses nothing."""
        raw = json.loads(self.foreign_manifest())
        self.assertNotIn(raw['schema'], ('selfstamp/1', 'selfstamp/2'))


class Test_observation_failures(SelfstampCase):
    """S2 / S9. A journal query that fails and a float that cannot be read
    are recorded as failures in the manifest and never as values. Fault
    model: a fake journalctl that exits nonzero; the fake calendar's status
    answering 500."""

    def test_a_failed_journal_query_is_an_error_entry_not_a_digest(self):
        fake = self.root / 'journalctl'
        fake.write_text('#!/bin/sh\nexit 3\n')
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.cfg['journal'] = True
        with mock.patch.object(selfstamp, 'journal_digest',
                               functools.partial(selfstamp.journal_digest, journalctl=(str(fake),))):
            self.assertEqual(self.run_tool(P1), 0, self.log)
        j = self.manifest('2026-09-01.json')['journal']
        self.assertIn('error', j)
        self.assertNotIn('sha256', j)
        self.assertEqual(j['since'], '2026-09-01 00:00:00 UTC')
        self.assertNotIn(str(self.root), json.dumps(j), 'no path of this box in the entry')

    def test_an_unknown_float_is_neither_zero_nor_healthy(self):
        self.calendar.balance_sats = None
        self.assertEqual(self.run_tool(P1), 0, self.log)
        f = self.manifest('2026-09-01.json')['float']
        self.assertNotIn('low', f)
        self.assertNotIn('balance_sats', f)
        self.assertIn('error', f)


if __name__ == "__main__":
    unittest.main()
