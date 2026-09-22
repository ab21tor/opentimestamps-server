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

"""Workflow four, the tools' half (docs/contracts.md, section 10): the
self-stamp and the watcher on state that was restored, carried to another
directory, or written by another version of the tool (R2, R4, R5), and
their first run after a restore stopped at each of its writes (R6). The
calendar's half is test_restore_calendar.

Old code is run as it was: ops/tests/migration/ holds the two tools at the
revisions before their state changed, byte for byte (each file's sha256
is checked before it is executed), so what an older tool does with newer
state is observed here, not described. Fault models, named again by each
class: faults.Stop at the n-th call of one call family of the first run
over a restored fixture, each case from a fresh copy, each injection
asserted to have fired, each recovery stopped once more; a storage error
shaped as the operating system shapes one (the class, the errno, the file
name as an attribute). The self-stamp talks to the loopback fake calendar
test_selfstamp has. None of it is a power cut."""

import datetime
import errno
import hashlib
import json
import os
import pathlib
import shutil
import tempfile
import types
import unittest
from unittest import mock

from otsserver.tests.faults import Stop, fail_on_call, unreadable
from otsserver.tests.test_selfstamp import SelfstampCase, P3, TOOL, selfstamp
from otsserver.tests.test_watch import ALL_OK, NOW, review_cfg, watch, watcher_run

OPS_TESTS = TOOL.parent / 'tests'
LEGACY = OPS_TESTS / 'selfstamp' / 'v2' / 'legacy-a'        # a selfstamp/2 chain of two days, written at 3961a1f
UTC = datetime.timezone.utc
FAMILIES = ('rename', 'replace', 'unlink', 'fsync')

# The tools as they were before their state changed (ops/tests/migration/README.md).
HISTORICAL = {
    'selfstamp-3961a1f.py.txt': '191c94c2cb07d76dfc9ba445c95ee1883266e6a8040693748fb3628ae887d768',
    'watch-ad64300.py.txt': '410ac24d317ddbea4d5df9850a668285e3d3aeeb38c3d3929055d3c578866c9d',
}


def historical(name):
    """The old tool as a module, from the bytes this repository had at that
    revision; bytes that are not those are not run."""
    source = (OPS_TESTS / 'migration' / name).read_bytes()
    if hashlib.sha256(source).hexdigest() != HISTORICAL[name]:
        raise AssertionError('%s is not the file that revision had' % name)
    module = types.ModuleType('historical_' + name.split('.')[0].replace('-', '_'))
    module.__file__ = str(OPS_TESTS / 'migration' / name)
    exec(compile(source, module.__file__, 'exec'), module.__dict__)
    return module


def files_under(directory):
    return {str(p.relative_to(directory)): p.read_bytes() for p in sorted(directory.rglob('*'))
            if p.is_file() and not p.name.startswith('.')}


class Test_what_older_tools_do_with_newer_state(unittest.TestCase):
    """R4, the other direction. An older tool either refuses state it does
    not know, and these pin where it does, or it does not, and these pin
    that too: the older tools were written before there was a rule, they
    cannot be changed from here, and a downgrade is therefore unsupported.
    What each does is stated so that nobody takes its silence for a
    refusal."""

    LABELLED = json.dumps({'schema': 'selfstamp/3', 'chain': 'ab' * 16, 'seq': 1, 'period': '2026-09-01',
                           'prev': None, 'witnessed': []}).encode()

    def test_the_older_self_stamp_refuses_a_labelled_chain_at_verify_and_at_its_inbox(self):
        old = historical('selfstamp-3961a1f.py.txt')
        with self.assertRaisesRegex(ValueError, 'not a selfstamp manifest'):
            old._parse_foreign_manifest(self.LABELLED)
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            manifests, inbox = root / 'manifests', root / 'inbox'
            manifests.mkdir()
            inbox.mkdir()
            (manifests / '2026-09-01.json').write_bytes(self.LABELLED)
            rows = []
            self.assertFalse(old.verify_chain(manifests, log=rows.append))
            self.assertTrue(any("schema 'selfstamp/3'" in row for row in rows), rows)
            (inbox / 'other-2026-09-01.json').write_bytes(self.LABELLED)
            self.assertEqual(old.witness_inbox({'inbox': str(inbox)}, root / 'witnessed', rows.append), 0)
            self.assertEqual(list(inbox.glob('*.json')), [])
            self.assertIn(self.LABELLED, [p.read_bytes() for p in (inbox / 'rejected').iterdir()],
                          'refused, kept, nothing lost')

    def continued_by_the_older_writer(self, root):
        """A labelled chain of one day, then a day written by `run` at
        3961a1f. Returns the config both versions are given."""
        old = historical('selfstamp-3961a1f.py.txt')
        (root / 'manifests').mkdir()
        (root / 'manifests' / '2026-09-01.json').write_bytes(self.LABELLED)
        cfg = {'state_dir': str(root), 'calendar_url': 'http://127.0.0.1:1', 'host': 'older-box',
               'books': {}, 'journal': False, 'fork_head': None}
        with mock.patch.object(old, 'read_float', return_value={}), \
                mock.patch.object(old, '_submit_pass', return_value=0), \
                mock.patch.object(old, '_upgrade_pass', return_value=0):
            self.assertEqual(old.run(cfg, period=datetime.date(2026, 9, 2), log=lambda _: None), 0)
        return cfg

    def test_the_older_self_stamp_writer_does_not_refuse_and_the_chain_says_so_afterwards(self):
        """The limit: `run` at 3961a1f reads its predecessor's seq and hash
        and nothing else, so it continues a labelled chain with a manifest
        that has a host name and no label. It cannot be made to refuse.
        What the current tool then says is what is pinned: verify calls
        the chain broken at that manifest, and nothing rewrites it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.continued_by_the_older_writer(root)
            written = json.loads((root / 'manifests' / '2026-09-02.json').read_bytes())
            self.assertEqual((written['schema'], written['host'], 'chain' in written), ('selfstamp/2', 'older-box', False))
            self.assertEqual(written['prev']['sha256'], hashlib.sha256(self.LABELLED).hexdigest())
            rows = []
            self.assertFalse(selfstamp.verify_chain(root / 'manifests', log=rows.append))
            self.assertTrue(any('2026-09-02.json' in row and 'chain label missing after a labelled manifest' in row
                                for row in rows), rows)

    def test_a_chain_the_older_writer_continued_is_never_given_a_second_label(self):
        """The label is drawn once. The current tool, back on such a chain,
        finds its newest manifest unlabelled and an earlier one labelled:
        it refuses, writes nothing, and says what the operator does.
        (Before workflow four it took the unlabelled predecessor for a
        chain from before selfstamp/3 and drew a second label.)"""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            cfg = self.continued_by_the_older_writer(root)
            before = files_under(root)
            rows = []
            rc = selfstamp.run(cfg, period=P3, now=datetime.datetime(2026, 9, 4, tzinfo=UTC), log=rows.append)
            self.assertEqual(files_under(root), before, 'nothing is written')
            self.assertEqual(rc, 1)
            text = '\n'.join(rows)
            self.assertIn('refused period=2026-09-03', text)
            self.assertIn('an older version of this tool', text)
            self.assertNotIn(str(root), text)
            # What the message says to do: the older version's manifests moved out, kept.
            aside = root / 'written-by-the-older-version'
            aside.mkdir()
            for path in (root / 'manifests').glob('2026-09-02.json*'):
                path.rename(aside / path.name)
            rows = []
            with mock.patch.object(selfstamp, '_submit_pass', return_value=0), \
                    mock.patch.object(selfstamp, '_upgrade_pass', return_value=0), \
                    mock.patch.object(selfstamp, 'read_float', return_value={}):
                self.assertEqual(selfstamp.run(cfg, period=P3, now=datetime.datetime(2026, 9, 4, tzinfo=UTC),
                                               log=rows.append), 0, rows)
            continued = json.loads((root / 'manifests' / '2026-09-03.json').read_bytes())
            self.assertEqual((continued['chain'], continued['seq']), ('ab' * 16, 2), 'the same label, the next seq')

    def test_the_older_watcher_neither_refuses_nor_delivers_what_the_record_owes(self):
        """The limit: the watcher at ad64300 reads state.json as a dict and
        ignores `owed`, the queue a newer run recorded and had not yet
        copied to the outbox. It delivers none of it, reports success, and
        leaves the key where it was, for the newer tool to finish. `owed`
        stands only between two writes of one run, so this needs a newer
        run stopped there and an older tool run next."""
        old = historical('watch-ad64300.py.txt')
        owed = [{'text': 'owed by the restored record', 'queued': '2026-09-01T00:00:00Z'}]
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / 'state.json').write_text(json.dumps({'owed': owed}))
            with mock.patch.multiple(old, WATCH_DIR=str(root), STATE=str(root / 'state.json'), STATUS=str(root / 'status')), \
                    mock.patch.object(old, 'load_config', return_value=review_cfg()), \
                    mock.patch.object(old, 'observe', return_value={}), \
                    mock.patch.object(old, 'evaluate', return_value=ALL_OK), \
                    mock.patch.object(old, 'log'), mock.patch.object(old, 'send') as sender:
                self.assertEqual(old.real_run(False), 0)
            sender.assert_not_called()
            self.assertEqual(json.loads((root / 'outbox.json').read_text()), [])
            self.assertEqual(json.loads((root / 'state.json').read_text())['owed'], owed)

            # And the cost of that on the way back. While `owed` stands the
            # record is the queue (W1), so the current tool copies it over
            # the outbox: an alert the older tool queued there meanwhile is
            # dropped unsent. It cannot be told from a message already
            # delivered from the record, which is dropped the same way.
            (root / 'outbox.json').write_text(json.dumps([{'text': 'queued by the older tool', 'queued': '2026-09-02T00:00:00Z'}]))
            sent = []
            with watcher_run(root, ALL_OK, lambda _, text: sent.append(text) or (True, 'ok')):
                self.assertEqual(watch.real_run(False), 0)
            self.assertEqual(sent, ['owed by the restored record'])
            self.assertEqual(json.loads((root / 'outbox.json').read_text()), [])


class Test_a_restored_self_stamp(SelfstampCase):
    """R2, R4, R5 and R6 for the self-stamp. The fixture is the selfstamp/2
    corpus copied under another directory, which is all a restore or
    another host is to this tool. The first run after it is the one-way
    step of R4: the chain gets its label, once. Fault model: faults.Stop
    at the n-th rename, replace, unlink or fsync of that run."""

    def restored(self, name):
        state = self.root / name
        shutil.copytree(LEGACY, state)
        return dict(self.cfg, state_dir=str(state))

    def first_run(self, cfg):
        return selfstamp.run(cfg, period=P3, now=datetime.datetime(2026, 9, 4, tzinfo=UTC), log=self.log.append)

    def converged(self, cfg, carried, published):
        """The step is taken once, whatever stopped it: what was carried is
        untouched, the period's manifest is the one that was published (if
        the stop came after its rename), it carries the label and links to
        the older chain, further runs change nothing and resubmit nothing,
        and the next period reuses the label."""
        state = pathlib.Path(cfg['state_dir'])
        self.assertEqual(self.first_run(cfg), 0, self.log)
        for name, raw in carried.items():
            self.assertEqual((state / name).read_bytes(), raw, name)
        path = state / 'manifests' / '2026-09-03.json'
        raw = path.read_bytes()
        if published is not None:
            self.assertEqual(raw, published, 'a manifest once renamed into place is never built again')
        manifest = json.loads(raw)
        self.assertEqual((manifest['schema'], manifest['seq']), ('selfstamp/3', 3))
        self.assertRegex(manifest['chain'], r'^[0-9a-f]{32}$')
        self.assertNotIn('host', manifest)
        self.assertEqual(manifest['prev'], {'file': '2026-09-02.json',
                                            'sha256': hashlib.sha256(carried['manifests/2026-09-02.json']).hexdigest()})
        proof = selfstamp.parse_ots(path.with_name(path.name + '.ots').read_bytes())
        self.assertEqual((proof.digest, proof.attestation[0]), (hashlib.sha256(raw).digest(), 'pending'))
        files, posts = files_under(state), self.calendar.operator_posts
        self.assertEqual(self.first_run(cfg), 0, self.log)
        self.assertEqual(files_under(state), files)
        self.assertEqual(self.calendar.operator_posts, posts, 'a proof on file is not asked for again')
        self.assertEqual(selfstamp.run(cfg, period=datetime.date(2026, 9, 4),
                                       now=datetime.datetime(2026, 9, 5, tzinfo=UTC), log=self.log.append), 0)
        later = json.loads((state / 'manifests' / '2026-09-04.json').read_bytes())
        self.assertEqual((later['chain'], later['prev']['sha256']), (manifest['chain'], hashlib.sha256(raw).hexdigest()))
        rows = []
        self.assertTrue(selfstamp.verify_chain(state / 'manifests', log=rows.append), rows)

    def test_every_write_of_the_run_that_labels_the_chain(self):
        swept = {}
        for family in FAMILIES:
            with mock.patch.object(selfstamp.os, family, wraps=getattr(os, family)) as counted:
                self.assertEqual(self.first_run(self.restored('count-' + family)), 0, self.log)
            swept[family] = counted.call_count
            for n in range(1, counted.call_count + 1):
                with self.subTest(family=family, n=n):
                    cfg = self.restored('%s-%d' % (family, n))
                    state = pathlib.Path(cfg['state_dir'])
                    carried = files_under(state)
                    with fail_on_call(selfstamp.os, family, n, exc=Stop()) as hit:
                        with self.assertRaises(Stop):
                            self.first_run(cfg)
                    self.assertTrue(hit['fired'])
                    path = state / 'manifests' / '2026-09-03.json'
                    published = path.read_bytes() if path.exists() else None
                    # The recovery stopped once more, if it still makes such a call (measured on a copy).
                    probe = self.root / ('probe-%s-%d' % (family, n))
                    shutil.copytree(state, probe)
                    with mock.patch.object(selfstamp.os, family, wraps=getattr(os, family)) as remaining:
                        self.assertEqual(self.first_run(dict(cfg, state_dir=str(probe))), 0, self.log)
                    if remaining.call_count:
                        with fail_on_call(selfstamp.os, family, 1, exc=Stop()) as again:
                            with self.assertRaises(Stop):
                                self.first_run(cfg)
                        self.assertTrue(again['fired'])
                    self.converged(cfg, carried, published)
        self.assertEqual(swept, {'rename': 0, 'replace': 2, 'unlink': 0, 'fsync': 6},
                         'the manifest and its proof written whole, each with its file and directory fsync, and '
                         'the barrier of the one complete proof carried (2026-09-01) repeated, file then directory (S5)')

    def test_the_label_waits_while_an_earlier_manifest_cannot_be_read(self):
        """Whether the chain already has its label is read from its
        manifests. One that cannot be read (chmod) leaves that open: the
        run refuses rather than draw a label that may be a second one, and
        takes the step once the file reads again."""
        cfg = self.restored('unreadable')
        state = pathlib.Path(cfg['state_dir'])
        carried = files_under(state)
        restore = unreadable(state / 'manifests' / '2026-09-01.json')
        try:
            self.assertEqual(self.first_run(cfg), 1)
        finally:
            restore()
        self.assertTrue(any('refused period=2026-09-03 an earlier manifest unreadable' in row
                            and 'PermissionError' in row and str(state) not in row for row in self.log), self.log)
        self.assertEqual(files_under(state), carried, 'no manifest is written')
        self.converged(cfg, carried, None)

    def test_a_pending_proof_that_was_carried_is_completed_where_it_lands(self):
        cfg = self.restored('carried')
        self.assertEqual(self.first_run(cfg), 0, self.log)
        elsewhere = self.root / 'another-host' / 'selfstamp'
        shutil.copytree(cfg['state_dir'], elsewhere)
        shutil.rmtree(cfg['state_dir'])
        cfg = dict(cfg, state_dir=str(elsewhere))
        manifests = {p.name: p.read_bytes() for p in (elsewhere / 'manifests').glob('*.json')}
        self.calendar.mined_height = 965432
        self.assertEqual(selfstamp.upgrade(cfg, log=self.log.append), 0, self.log)
        proof = elsewhere / 'manifests' / '2026-09-03.json.ots'
        self.assertEqual(selfstamp.parse_ots(proof.read_bytes()).attestation, ('bitcoin', 965432))
        self.assertEqual({p.name: p.read_bytes() for p in (elsewhere / 'manifests').glob('*.json')}, manifests,
                         'the proof is replaced whole; no manifest is touched')


class Test_a_restored_watcher(unittest.TestCase):
    """R2, R4 and R6 for the watcher. Two restored states: one as ad64300
    wrote it (the alarm recorded, its message in the outbox, no `owed`),
    and one a newer run left between its record and its outbox (the
    message under `owed`, the outbox older). Either way the restore is not
    another transition: the message is delivered once. Fault model:
    faults.Stop at the n-th rename, replace, unlink or fsync of the first
    run, the sender failing so that the run exits 1 with the message
    kept."""

    CHECKS = dict(ALL_OK, mem=(False, 'mem available 100MB'))
    MESSAGE = {'text': 'box DEGRADED: mem available 100MB', 'queued': '2026-09-01T00:00:00Z'}

    def fixture(self, root, owed):
        root.mkdir()
        state = {'delivered': ['mem'], 'fail_runs': {'mem': 2}, 'since': {'mem': NOW - 600},
                 'cursors': {'journal': NOW - 300}}
        if owed:
            state['owed'] = [self.MESSAGE]
        (root / 'state.json').write_text(json.dumps(state))
        (root / 'outbox.json').write_text(json.dumps([] if owed else [self.MESSAGE]))

    def first_run(self, root):
        with watcher_run(root, self.CHECKS, lambda *_: (False, 'offline')):
            return watch.real_run(False)

    def test_every_write_of_the_first_run_over_either_state(self):
        swept = {}
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            for owed in (False, True):
                for family in FAMILIES:
                    tag = '%s-%s' % ('owed' if owed else 'older', family)
                    self.fixture(base / ('count-' + tag), owed)
                    with mock.patch.object(watch.os, family, wraps=getattr(os, family)) as counted:
                        self.assertEqual(self.first_run(base / ('count-' + tag)), 1)
                    swept[tag] = counted.call_count
                    for n in range(1, counted.call_count + 1):
                        with self.subTest(state=tag, n=n):
                            root = base / ('%s-%d' % (tag, n))
                            self.fixture(root, owed)
                            for attempt in (n, 1):      # the stop, then the recovery stopped at its first such call
                                with fail_on_call(watch.os, family, attempt, exc=Stop()) as hit:
                                    with self.assertRaises(Stop):
                                        self.first_run(root)
                                self.assertTrue(hit['fired'])
                            self.assertEqual(self.first_run(root), 1)
                            state = json.loads((root / 'state.json').read_text())
                            self.assertEqual((state['delivered'], state['since']['mem'], state['cursors']['journal']),
                                             (['mem'], NOW - 600, NOW))
                            self.assertNotIn('owed', state)
                            self.assertEqual(json.loads((root / 'outbox.json').read_text()), [self.MESSAGE])
                            sent = []
                            for _ in range(2):
                                with watcher_run(root, self.CHECKS, lambda _, text: sent.append(text) or (True, 'ok')):
                                    self.assertEqual(watch.real_run(False), 0)
                            self.assertEqual(sent, [self.MESSAGE['text']], 'delivered once: a restore is not a transition')
        self.assertEqual(swept, {'older-rename': 0, 'older-replace': 4, 'older-unlink': 0, 'older-fsync': 8,
                                 'owed-rename': 0, 'owed-replace': 4, 'owed-unlink': 0, 'owed-fsync': 8},
                         'the status, the record, the outbox, the record again: each a replace with two fsyncs')

    def test_a_state_read_after_its_outbox_loses_the_alert_unseen(self):
        """The limit the gate review named (2026-09-18, G5), pinned as it
        is. A sequential copy reads the outbox before a transition and the
        state after it, once `owed` has been cleared: the state says the
        alarm was recorded, the outbox holds nothing. The restored run
        sends nothing, exits 0, and nothing in either file can show what
        the other lacks. W1's replay recovers what the record still owes,
        and this record owes nothing; the live watcher does."""
        with tempfile.TemporaryDirectory() as tmp:
            live = pathlib.Path(tmp) / 'live'
            live.mkdir()
            (live / 'state.json').write_text(json.dumps({'fail_runs': {'mem': 1}}))
            (live / 'outbox.json').write_text('[]')
            older_outbox = (live / 'outbox.json').read_bytes()            # the copier reads the outbox first
            with watcher_run(live, self.CHECKS, lambda *_: (False, 'offline')):
                self.assertEqual(watch.real_run(False), 1)                 # the transition: recorded, queued, not sent
            self.assertEqual(len(json.loads((live / 'outbox.json').read_text())), 1)
            later_state = (live / 'state.json').read_bytes()               # and the state after
            self.assertNotIn('owed', json.loads(later_state))
            restored = pathlib.Path(tmp) / 'restored'
            restored.mkdir()
            (restored / 'state.json').write_bytes(later_state)
            (restored / 'outbox.json').write_bytes(older_outbox)
            sent = []
            with watcher_run(restored, self.CHECKS, lambda _, text: sent.append(text) or (True, 'ok')):
                self.assertEqual(watch.real_run(False), 0)
            self.assertEqual(sent, [], 'the alert the live watcher still owes is in neither copied file')
            self.assertEqual(json.loads((restored / 'state.json').read_text())['delivered'], ['mem'])

    def test_a_storage_error_is_logged_without_the_file_it_names(self):
        """Control; nothing here was wrong. The four handoffs between the
        record and the outbox log a storage error with %r, and the repr of
        an OSError is its class, errno and text, never the file name the
        operating system attaches. Pinned so that it stays so: the state
        directory's path can name a client."""
        for boundary in ('the outbox', 'the record cleared', 'the outbox after a delivery', 'the record after a delivery'):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory(prefix='client-acme-private-') as tmp:
                root = pathlib.Path(tmp)
                (root / 'outbox.json').write_text(json.dumps([self.MESSAGE]))
                real, calls = watch.write_json_atomic, {'state.json': 0, 'outbox.json': 0}

                def write(path, data):
                    name = pathlib.Path(path).name
                    calls[name] += 1
                    if {'the outbox': name == 'outbox.json',
                            'the record cleared': name == 'state.json' and calls[name] == 2,
                            'the outbox after a delivery': name == 'outbox.json' and calls[name] == 2,
                            'the record after a delivery': name == 'outbox.json' or (name == 'state.json' and calls[name] == 2),
                            }[boundary]:
                        raise OSError(errno.EIO, os.strerror(errno.EIO), str(path))
                    return real(path, data)
                delivered = boundary.endswith('after a delivery')
                with watcher_run(root, ALL_OK, lambda *_: (delivered, 'offline'), quiet=False), \
                        mock.patch.object(watch, 'write_json_atomic', side_effect=write), \
                        mock.patch.object(watch, 'log') as logged:
                    watch.real_run(False)
                text = '\n'.join(call.args[0] for call in logged.call_args_list)
                self.assertIn('write failed', text)
                self.assertIn("OSError(5, 'Input/output error')", text)
                self.assertNotIn('client-acme-private', text)


if __name__ == '__main__':
    unittest.main()
