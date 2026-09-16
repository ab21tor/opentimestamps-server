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

"""ops/watch.py — the box health watcher, loaded by path like selfstamp.

The fixture scenarios under ops/tests/watch/ (26 written for the Pi's hosted
shape on 2026-09-04 and 2026-09-07, three for the appliance shape) drive
evaluate/decide/heartbeat with the sender stubbed, exactly as
`watch.py --test` does. The unit tests pin what changed when the script moved
into this fork (2026-09-11): an empty knob skips its check and its count,
the calendar check's verdicts, the sats parser, and the box name in every
message. Nothing here touches the network, docker, systemd or the journal.
"""

import contextlib
import errno
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TOOL = pathlib.Path(__file__).resolve().parents[2] / 'ops' / 'watch.py'


def load_tool():
    spec = importlib.util.spec_from_file_location('watch', TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


watch = load_tool()

APPLIANCE = {"HEALTH_URL": "", "CALENDAR_URL": "http://127.0.0.1:14788",
             "CONTAINERS": "appliance-otsd-1",
             "UNITS_SYSTEM": "bitcoind.service,docker.service,nftables.service",
             "UNITS_USER": "api-endpoint.service,selfstamp.timer",
             "FEEDER_LOG": "", "TOR_CONTAINER": "", "DISK_BOOT": "", "DHCP_IFACE": ""}


def cfg_with(**over):
    cfg = dict(watch.DEFAULTS)
    cfg.update(over)
    return cfg


class Test_fixtures(unittest.TestCase):
    def test_every_fixture_scenario_passes(self):
        lines = []
        total, failed = watch.run_fixtures(out=lines.append)
        self.assertGreaterEqual(total, 30)
        self.assertEqual(failed, 0, "\n".join(lines))


class Test_active_checks(unittest.TestCase):
    def test_empty_knob_skips_its_check_and_its_count(self):
        active = watch.active_checks(cfg_with(**APPLIANCE))
        for skipped in ("health_reach", "health", "feeder", "tor_circuits", "disk_boot", "dhcp_lease"):
            self.assertNotIn(skipped, active)
        self.assertIn("calendar", active)
        self.assertEqual(len(active), 16)

    def test_pi_defaults_keep_their_twenty_one_and_no_calendar(self):
        active = watch.active_checks(cfg_with())
        self.assertEqual(len(active), 21)
        self.assertNotIn("calendar", active)
        self.assertEqual([n for n in watch.ORDER if n != "calendar"], active)

    def test_a_skipped_check_never_alerts_or_counts(self):
        # The appliance has no feeder: a missing feeder log is not a failure.
        cfg = cfg_with(**APPLIANCE)
        o = {"calendar_reach": True,
             "calendar": {"best_block": "00" * 32, "anchor_receipts": "on", "balance": "212,015"},
             "containers": {"appliance-otsd-1": "Up 2 days"},
             "units_system": {u: "active" for u in cfg["UNITS_SYSTEM"].split(",")},
             "units_user": {u: "active" for u in cfg["UNITS_USER"].split(",")},
             "disk": {"/": 9.1}, "temp_c": 40.0, "mem_avail_mb": 4000,
             "endpoint_age": 5, "endpoint_breaker": "ok", "journal_errors": 0, "ssh_failures": 0,
             "anchors": 3, "anchor_age_h": 2.0, "btc_peers": 8, "uptime_s": 1000}
        checks = watch.evaluate(o, cfg)
        self.assertFalse(checks["feeder"][0])          # computed, but ...
        state, msgs = watch.decide({}, checks, 1788600000, cfg)
        state, msgs = watch.decide(state, checks, 1788600300, cfg)
        self.assertEqual(msgs, [])                     # ... never alerted
        self.assertEqual(state["delivered"], [])
        self.assertIn("ok 16/16", watch.status_line(1788600300, state, checks, cfg))


class Test_calendar_check(unittest.TestCase):
    def verdict(self, reach, cal, min_sats="100000"):
        cfg = cfg_with(CALENDAR_URL="http://127.0.0.1:14788", CAL_MIN_SATS=min_sats)
        return watch.evaluate({"calendar_reach": reach, "calendar": cal}, cfg)["calendar"]

    def test_verdicts(self):
        ok = {"best_block": "00" * 32, "anchor_receipts": "on", "balance": "22,015"}
        self.assertEqual(self.verdict(True, ok, "20000"), (True, ""))
        self.assertEqual(self.verdict(False, None), (False, "no answer from the calendar"))
        self.assertEqual(self.verdict(True, {}), (False, "calendar is Bitcoin-blind"))
        self.assertEqual(self.verdict(True, dict(ok, best_block=None)), (False, "calendar is Bitcoin-blind"))
        self.assertEqual(self.verdict(True, dict(ok, anchor_receipts="off")),
                         (False, "calendar anchor receipts off"))
        # The deep-reorg detector's finding outranks everything but reach
        # and Bitcoin-blindness: the proofs on file name a block that no
        # longer holds the anchor.
        gone = "anchor 3f3f left the chain (confirmations 0, receipted at height 965866)"
        self.assertEqual(self.verdict(True, dict(ok, needs_attention=[gone]), "20000"),
                         (False, "calendar needs attention: " + gone))
        self.assertEqual(self.verdict(True, dict(ok, needs_attention=[gone, "anchor 4a4a mined again"]),
                                      "20000"),
                         (False, "calendar needs attention: " + gone + "; anchor 4a4a mined again"))
        self.assertEqual(self.verdict(True, dict(ok, needs_attention=[]), "20000"), (True, ""))
        self.assertEqual(self.verdict(True, ok), (False, "anchor wallet 22015 sats < 100000"))
        self.assertEqual(self.verdict(True, dict(ok, balance="lots")), (False, "calendar balance unreadable"))

    def test_parse_sats(self):
        self.assertEqual(watch.parse_sats("22,015"), 22015)
        self.assertEqual(watch.parse_sats(5), 5)
        self.assertEqual(watch.parse_sats(" 7 "), 7)
        self.assertIsNone(watch.parse_sats("lots"))
        self.assertIsNone(watch.parse_sats(None))
        self.assertIsNone(watch.parse_sats(True))


class Test_names(unittest.TestCase):
    def test_messages_carry_the_configured_name_or_the_hostname(self):
        import socket
        cfg = cfg_with(**APPLIANCE)
        checks = {n: (True, "") for n in watch.ORDER}
        checks["calendar"] = (False, "no answer from the calendar")
        state, msgs = watch.decide({}, checks, 1788600000, cfg_with(NAME="box7", **APPLIANCE))
        state, msgs = watch.decide(state, checks, 1788600300, cfg_with(NAME="box7", **APPLIANCE))
        self.assertEqual(msgs, ["box7 DEGRADED: no answer from the calendar | still: none"])
        state, msgs = watch.decide({}, checks, 1788600000, cfg)
        state, msgs = watch.decide(state, checks, 1788600300, cfg)
        self.assertTrue(msgs[0].startswith(socket.gethostname() + " DEGRADED: "), msgs)

    def test_heartbeat_names_the_wallet_when_the_calendar_is_observed(self):
        cfg = cfg_with(NAME="box7", **APPLIANCE)
        checks = {n: (True, "") for n in watch.ORDER}
        o = {"calendar": {"balance": "22,015", "pending_commitments": "1,204"}, "anchors": 4, "anchor_age_h": 1.5,
             "disk": {"/": 9.1}, "temp_c": 40.0, "mem_avail_mb": 4000, "uptime_s": 100}
        line = watch.heartbeat_line({"delivered": []}, checks, o, cfg)
        self.assertTrue(line.startswith("box7 heartbeat: ok 16/16"), line)
        self.assertIn("wallet 22,015 sats, pending 1,204", line)
        line = watch.heartbeat_line({"delivered": []}, checks, dict(o, calendar=None), cfg)
        self.assertNotIn("wallet", line)


class Test_outbox(unittest.TestCase):
    """2026-09-15 review, P2 "a failed one-shot security alert is
    permanently lost": every message is written to the outbox before the
    journal cursor moves, delivered oldest first, retried by every later
    run until it lands; the run exits 1 while anything is undelivered."""

    ALL_OK = {name: (True, '') for name in watch.ORDER}
    BURST = dict(ALL_OK, ssh_unexpected=(False, 'ssh accepted from unexpected source 203.0.113.7'))

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.dir = pathlib.Path(self.tmpdir.name)
        self.state = self.dir / 'state.json'
        self.outbox = self.dir / 'outbox.json'

    def runs(self, cfg, evaluations, sends, extra=None):
        """real_run once per evaluation; returns (exit codes, the sender mock)."""
        patches = [mock.patch.multiple(watch, WATCH_DIR=str(self.dir), STATE=str(self.state), STATUS=str(self.dir / 'status')),
                   mock.patch.object(watch, 'load_config', return_value=cfg),
                   mock.patch.object(watch, 'observe', return_value={}),
                   mock.patch.object(watch, 'evaluate', side_effect=evaluations),
                   mock.patch.object(watch, 'log')] + (extra or [])
        for p in patches:
            p.start()
        try:
            with mock.patch.object(watch, 'send', side_effect=sends) as sent:
                codes = [watch.real_run(False) for _ in evaluations]
        finally:
            for p in patches:
                p.stop()
        return codes, sent

    def cfg(self, **over):
        cfg = dict(watch.DEFAULTS, HEARTBEAT_HOUR='99', NAME='box', NTFY_URL='http://ntfy.invalid/box')
        cfg.update(over)
        return cfg

    def queued(self):
        return json.loads(self.outbox.read_text()) if self.outbox.exists() else None

    def test_a_failed_alert_is_kept_and_retried_in_order(self):
        codes, sent = self.runs(self.cfg(), [self.BURST, self.ALL_OK, self.ALL_OK],
                                [(False, 'down'), (False, 'down'), (True, 'http 200'), (True, 'http 200')])
        self.assertEqual(codes, [1, 1, 0])
        self.assertEqual(sent.call_count, 4)
        texts = [c.args[1] for c in sent.call_args_list]
        self.assertIn('203.0.113.7', texts[0])
        self.assertEqual(texts[0], texts[1], 'the lost burst is retried')
        self.assertEqual(texts[2], texts[0])
        self.assertIn('RECOVERED', texts[3])
        self.assertEqual(self.queued(), [])
        self.assertIn('journal', json.loads(self.state.read_text())['cursors'])

    def test_the_cursor_moves_only_once_the_burst_is_on_disk(self):
        real = watch.write_json_atomic

        def outbox_fails(path, data):
            if path == str(self.outbox):
                raise OSError(28, 'No space left on device')
            return real(path, data)
        codes, sent = self.runs(self.cfg(), [self.BURST], [(False, 'down')],
                                extra=[mock.patch.object(watch, 'write_json_atomic', side_effect=outbox_fails)])
        self.assertEqual(codes, [1])
        self.assertEqual(sent.call_count, 1, 'still tried directly')
        state = json.loads(self.state.read_text())
        self.assertNotIn('cursors', state, 'the burst is neither on disk nor delivered: the cursor stays')
        self.assertIsNone(self.queued())
        # Delivered directly, the cursor moves even without an outbox.
        codes, sent = self.runs(self.cfg(), [self.BURST], [(True, 'http 200')],
                                extra=[mock.patch.object(watch, 'write_json_atomic', side_effect=outbox_fails)])
        self.assertEqual(codes, [0])
        self.assertIn('journal', json.loads(self.state.read_text())['cursors'])

    def test_status_only_mode_queues_nothing(self):
        codes, sent = self.runs(self.cfg(NTFY_URL=''), [self.BURST, self.ALL_OK], [])
        self.assertEqual(codes, [0, 0])
        self.assertEqual(sent.call_count, 0)
        self.assertIsNone(self.queued())
        self.assertIn('journal', json.loads(self.state.read_text())['cursors'])

    def test_the_outbox_is_bounded(self):
        self.outbox.write_text(json.dumps([{'text': 'old %d' % i, 'queued': ''} for i in range(watch.OUTBOX_MAX + 5)]))
        codes, sent = self.runs(self.cfg(), [self.BURST], [(False, 'down')])
        self.assertEqual(codes, [1])
        queued = self.queued()
        self.assertEqual(len(queued), watch.OUTBOX_MAX)
        self.assertIn('203.0.113.7', queued[-1]['text'])
        self.assertEqual(queued[0]['text'], 'old 6')


NOW = 1788600000
ALL_OK = {name: (True, '') for name in watch.ORDER}


def review_cfg(**over):
    cfg = dict(watch.DEFAULTS, HEARTBEAT_HOUR='99', NAME='box', NTFY_URL='http://ntfy.invalid/box')
    cfg.update(over)
    return cfg


@contextlib.contextmanager
def watcher_run(directory, checks, send, now=NOW, quiet=True):
    """real_run against a state directory, with observation and evaluation
    replaced by the given checks and the sender by `send`."""
    patches = [mock.patch.multiple(watch, WATCH_DIR=str(directory), STATE=str(directory / 'state.json'),
                                   STATUS=str(directory / 'status')),
               mock.patch.object(watch, 'load_config', return_value=review_cfg()),
               mock.patch.object(watch, 'observe', return_value={}),
               mock.patch.object(watch, 'evaluate', return_value=checks),
               mock.patch.object(watch, 'send', side_effect=send),
               mock.patch.object(watch.time, 'time', return_value=now)]
    if quiet:
        patches.append(mock.patch.object(watch, 'log'))
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


class Test_observation_failure(unittest.TestCase):
    """2026-09-15/16 review F09: a journalctl call that fails used to read
    as zero events, and the cursor moved past the window nobody read. Now
    a failed read is a failed check (journal_read) and the cursor stays
    where it was until every journal query succeeds."""

    def observe_with(self, journal_rc):
        config = review_cfg()
        for key in watch.SKIP_WHEN_EMPTY.values():
            config[key] = ''
        seen = []

        def command(cmd, **kwargs):
            if cmd[0] == 'journalctl':
                seen.append(cmd)
                return journal_rc, ''
            return 0, ''
        with mock.patch.object(watch, 'run', side_effect=command), \
                mock.patch.object(watch, 'kernel_versions', return_value=('same', 'same')):
            observation = watch.observe(config, NOW, {'journal': NOW - 300})
        self.assertEqual(len(seen), 5, 'every journal query was made')
        return config, observation

    def run_with(self, config, observation):
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp)
            (d / 'state.json').write_text(json.dumps({'cursors': {'journal': NOW - 300}}))
            with mock.patch.multiple(watch, WATCH_DIR=str(d), STATE=str(d / 'state.json'), STATUS=str(d / 'status')), \
                    mock.patch.object(watch, 'load_config', return_value=config), \
                    mock.patch.object(watch, 'observe', return_value=observation), \
                    mock.patch.object(watch, 'send', return_value=(True, 'ok')), \
                    mock.patch.object(watch, 'log'), mock.patch.object(watch.time, 'time', return_value=NOW):
                rc = watch.real_run(False)
            return rc, json.loads((d / 'state.json').read_text())

    def test_a_failed_journal_read_is_a_failed_check_and_keeps_the_cursor(self):
        config, observation = self.observe_with(journal_rc=1)
        checks = watch.evaluate(observation, config)
        self.assertFalse(checks['journal_read'][0], checks['journal_read'])
        self.assertIn('journalctl', checks['journal_read'][1])
        self.assertIn('journal_read', watch.active_checks(config))
        rc, state = self.run_with(config, observation)
        self.assertEqual(state['cursors']['journal'], NOW - 300, 'the unread window is read again next run')

    def test_a_successful_journal_read_advances_the_cursor(self):
        config, observation = self.observe_with(journal_rc=0)
        checks = watch.evaluate(observation, config)
        self.assertTrue(checks['journal_read'][0])
        rc, state = self.run_with(config, observation)
        self.assertEqual(rc, 0)
        self.assertEqual(state['cursors']['journal'], NOW)


class Test_outbox_read_failure(unittest.TestCase):
    """2026-09-15/16 review F10: an outbox that could not be read used to
    become an empty queue, and the next write replaced the undelivered
    messages. Now only a missing file is an empty queue: an unreadable one
    fails the run with nothing touched, and bytes that are not a message
    list are set aside for inspection and reported as an alert."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.dir = pathlib.Path(self.tmpdir.name)
        self.outbox = self.dir / 'outbox.json'
        self.state = self.dir / 'state.json'
        self.state.write_text(json.dumps({'cursors': {'journal': NOW - 300}}))

    def test_an_unreadable_outbox_fails_the_run_and_touches_nothing(self):
        if os.geteuid() == 0:
            self.skipTest('root reads a mode-000 file; the failure cannot be produced')
        original = json.dumps([{'text': 'security alert never delivered', 'queued': 'old'}])
        self.outbox.write_text(original)
        self.outbox.chmod(0)
        self.addCleanup(self.outbox.chmod, 0o600)
        with self.assertRaises(PermissionError):
            self.outbox.read_bytes()
        sent = []
        with watcher_run(self.dir, ALL_OK, lambda _, text: sent.append(text) or (True, 'ok')):
            rc = watch.real_run(False)
        self.assertEqual(rc, 1)
        self.assertEqual(sent, [])
        self.outbox.chmod(0o600)
        self.assertEqual(self.outbox.read_text(), original, 'the undelivered queue is untouched')
        self.assertEqual(json.loads(self.state.read_text())['cursors']['journal'], NOW - 300,
                         'the state did not move')

    def test_a_corrupt_outbox_is_set_aside_and_reported(self):
        self.outbox.write_bytes(b'{not a message list')
        sent = []
        with watcher_run(self.dir, ALL_OK, lambda _, text: sent.append(text) or (True, 'ok')):
            rc = watch.real_run(False)
        self.assertEqual(rc, 0)
        aside = [p for p in self.dir.iterdir() if p.name.startswith('outbox.json.corrupt-')]
        self.assertEqual(len(aside), 1, os.listdir(self.dir))
        self.assertEqual(aside[0].read_bytes(), b'{not a message list', 'the bytes are kept for inspection')
        self.assertEqual(len(sent), 1, sent)
        self.assertIn('outbox', sent[0])
        self.assertIn(aside[0].name, sent[0])
        self.assertEqual(json.loads(self.outbox.read_text()), [])


class Test_run_lock(unittest.TestCase):
    """2026-09-15/16 review F11: two overlapping runs used to read the same
    queue and the later writer replaced the other's messages. Now a run
    holds an exclusive lock on the state directory for its whole duration;
    a second run waits up to watch.LOCK_WAIT seconds, then exits 1 with
    'locked' and nothing written. Two real processes; the first is paused
    inside its sender while the second tries."""

    def test_a_second_run_cannot_interleave_and_no_alert_is_lost(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp)
            first = dict(ALL_OK, ssh_unexpected=(False, 'login A'))
            child = {}

            def send_a(_, text):
                # The lock is held here. A second run must wait, give up, and change nothing.
                p = subprocess.run([sys.executable, '-B', __file__, '--child', str(d), '0.5'],
                                   capture_output=True, text=True, timeout=60,
                                   env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
                child['rc'], child['out'] = p.returncode, p.stdout + p.stderr
                child['outbox'] = json.loads((d / 'outbox.json').read_text())
                return True, 'delivered A'
            with watcher_run(d, first, send_a):
                rc = watch.real_run(False)
            self.assertEqual(rc, 0)
            self.assertEqual(child['rc'], 1, child['out'])
            self.assertIn('locked', child['out'])
            self.assertEqual([m['text'] for m in child['outbox']], [m['text'] for m in child['outbox'] if 'login A' in m['text']],
                             'while the first run held the lock only its own message was on file')
            self.assertEqual(json.loads((d / 'outbox.json').read_text()), [])
            # The lock is free: the second run now observes B, cannot send it, and B is on disk.
            p = subprocess.run([sys.executable, '-B', __file__, '--child', str(d), '5'],
                               capture_output=True, text=True, timeout=60,
                               env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            self.assertNotIn('locked', p.stdout + p.stderr)
            queued = json.loads((d / 'outbox.json').read_text())
            self.assertTrue(any('new security burst B' in m['text'] for m in queued), queued)
            self.assertEqual(json.loads((d / 'state.json').read_text())['cursors']['journal'], NOW + 300)


def _child_run(directory, lock_wait):
    """The second watcher process of Test_run_lock: a burst B, a sender that
    is offline, the given lock wait; exits with real_run's code."""
    watch.LOCK_WAIT = lock_wait
    checks = dict(ALL_OK, journal_errors=(False, 'new security burst B'))
    with watcher_run(pathlib.Path(directory), checks, lambda *_: (False, 'offline B'), now=NOW + 300, quiet=False):
        return watch.real_run(False)


class Test_outbox_recovery_interrupted(unittest.TestCase):
    """The corrupt-outbox recovery is itself interruptible (close-gate
    correction, 2026-09-16). The first version moved the corrupt file
    aside and only later persisted the notice in the replacement queue;
    a stop between the two left an aside file nobody reports and an empty
    queue. Now the aside copy is written first, under a name derived from
    the bytes, and the notice replaces the corrupt file in one rename: a
    stop before the rename leaves the corrupt file to be found again, a
    stop after it leaves the notice on disk. Exception injection at the
    rename; not a power cut."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.dir = pathlib.Path(self.tmpdir.name)
        self.outbox = self.dir / 'outbox.json'
        self.patches = [mock.patch.multiple(watch, WATCH_DIR=str(self.dir), STATE=str(self.dir / 'state.json'),
                                            STATUS=str(self.dir / 'status')),
                        mock.patch.object(watch, 'log')]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def asides(self):
        return sorted(p for p in self.dir.iterdir() if p.name.startswith('outbox.json.corrupt-'))

    def test_a_stop_right_after_the_quarantine_keeps_the_notice(self):
        self.outbox.write_bytes(b'{not a message list')
        first = watch.load_outbox(review_cfg())          # the run stops here, before it writes anything else
        self.assertEqual(len(first), 1)
        self.assertIn('outbox', first[0]['text'])
        again = watch.load_outbox(review_cfg())          # the next run
        self.assertEqual([m['text'] for m in again], [first[0]['text']],
                         'the notice is on disk the moment the corrupt file is gone')
        self.assertEqual(len(self.asides()), 1)
        self.assertEqual(self.asides()[0].read_bytes(), b'{not a message list')
        self.assertIn(self.asides()[0].name, first[0]['text'])

    def test_a_stop_between_the_aside_copy_and_the_replacement_is_repeated_cleanly(self):
        self.outbox.write_bytes(b'{not a message list')
        real_replace = os.replace
        calls = []

        def fail_once(src, dst):
            if dst == str(self.outbox) and not calls:
                calls.append(dst)
                raise OSError(errno.EIO, 'injected stop before the replacement lands')
            return real_replace(src, dst)
        with mock.patch.object(watch.os, 'replace', side_effect=fail_once):
            with self.assertRaises(OSError):
                watch.load_outbox(review_cfg())
        self.assertEqual(self.outbox.read_bytes(), b'{not a message list', 'the corrupt file is still there to be found')
        self.assertEqual(len(self.asides()), 1, 'the aside copy was made before the replacement')
        queue = watch.load_outbox(review_cfg())
        self.assertEqual(len(queue), 1)
        self.assertEqual(len(self.asides()), 1, 'the same bytes get the same aside name: no second copy')
        self.assertEqual(watch.load_outbox(review_cfg()), queue, 'and the notice stays until delivered')

if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == '--child':
        sys.exit(_child_run(sys.argv[2], float(sys.argv[3])))
    unittest.main()
