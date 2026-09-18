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

"""ops/watch.py, workflow three: the watcher's observation contract
(docs/contracts.md, section 8, tables W1-W9), each transition pinned
beside its failure cases.

Every class says which transition it pins and which fault model it uses:
observations carrying the marker observe leaves when a source cannot be
read; an exception injected at the n-th call of a named function
(otsserver/tests/faults.py, shared with the self-stamp's tests); a path
made unreadable by chmod; a real child process paused at a named
boundary and killed with SIGKILL; a sender that fails. None is a power
cut, and no test orders events with a sleep.

The 2026-09-16 gate review's thirteen assertions (malformed bytes or an
unexpected shape at a source stopping the run; a failed read behind a
fresh file reading as ok; a heartbeat or a RECOVERED saying all clear
from the alarm set rather than the verdicts; the cap decided again on
replay; malformed fields inside valid JSON crashing or vanishing; a
configured mount in a message) are kept here as permanent regressions,
in this module's shape.
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from otsserver.tests.faults import Stop, fail_on_call, unreadable
from otsserver.tests.test_watch import ALL_OK, APPLIANCE, NOW, TOOL, cfg_with, review_cfg, watch, watcher_run

BURST = dict(ALL_OK, ssh_unexpected=(False, 'ssh accepted from 1 source not in SSH_KNOWN_SOURCES'))
SLOW = dict(ALL_OK, mem=(False, 'mem available 100MB'))   # a two-run check


def sent_texts(sender_calls):
    return [c.args[1] for c in sender_calls]


# --- W4: every check is an observation with an unknown state ---------------------------

class Test_unknown_is_a_state(unittest.TestCase):
    """W4. A check whose source cannot be read says unknown (verdict None),
    never ok, with a detail that says why; the same check with its source
    readable says ok. Fault model: the observation carries the marker
    observe leaves for that source (None, an error class, a failed label)."""

    def evaluate(self, **override):
        cfg = cfg_with(CALENDAR_URL='http://127.0.0.1:14788', RECEIPTS='/tmp/receipts', DHCP_IFACE='eth0')
        good = {'health_reach': True, 'health': {'status': 'ok'},
                'calendar_reach': True, 'calendar': {'best_block': '00' * 32, 'anchor_receipts': 'on', 'balance': 212015},
                'containers': {n: 'Up 1 day' for n in cfg['CONTAINERS'].split(',')},
                'units_system': {u: 'active' for u in cfg['UNITS_SYSTEM'].split(',')},
                'units_user': {u: 'active' for u in cfg['UNITS_USER'].split(',')},
                'disk': {'/': 10.0, '/boot/firmware': 20.0}, 'temp_c': 40.0, 'mem_avail_mb': 4000,
                'feeder_age': 5, 'feeder_err_polls': 0, 'endpoint_age': 5, 'endpoint_breaker': 'ok',
                'journal_errors': 0, 'ssh_failures': 0, 'journal_failed': [], 'ssh_sources': [],
                'anchors': 3, 'anchor_age_h': 2.0, 'kernel_running': 'k1', 'kernel_newest': 'k1',
                'reboot_required_file': False, 'egress_drops': 0, 'dhcp_lease_left_h': 20.0,
                'tor_alive_lines': 1, 'tor_circuits': 12, 'tor_net_warn': 0, 'btc_peers': 8}
        good.update(override)
        return watch.evaluate(good, cfg)

    CASES = [
        ('containers', {'containers': None}),
        ('units_system', {'units_system': {'docker.service': "exc FileNotFoundError(2, 'No such file or directory')"}}),
        ('units_user', {'units_user': {'selfstamp.timer': ''}}),
        ('disk_root', {'disk': {'/': None, '/boot/firmware': 20.0}}),
        ('disk_boot', {'disk': {'/': 10.0, '/boot/firmware': None}}),
        ('temp', {'temp_c': None}),
        ('mem', {'mem_avail_mb': None}),
        ('endpoint', {'endpoint_age': None, 'endpoint_breaker': None, 'endpoint_error': 'unreadable'}),
        ('endpoint', {'endpoint_age': 5, 'endpoint_breaker': None, 'endpoint_error': 'unreadable'}),
        ('anchor_age', {'anchors': None, 'anchor_age_h': None, 'receipts_error': 'missing'}),
        ('anchor_age', {'anchors': None, 'anchor_age_h': None, 'receipts_error': 'unreadable'}),
        ('anchor_age', {'anchors': None, 'anchor_age_h': None, 'receipts_error': 'unparseable'}),
        ('anchor_age', {'anchors': 0, 'anchor_age_h': None}),
        ('reboot_wanted', {'kernel_newest': None}),
        ('dhcp_lease', {'dhcp_lease_left_h': None}),
        ('tor_circuits', {'tor_alive_lines': None}),
        ('btc_peers', {'btc_peers': None}),
        ('calendar', {'calendar': 'not a status object'}),
        ('calendar', {'calendar': {'best_block': '00' * 32, 'anchor_receipts': 'on', 'balance': 'lots'}}),
        ('health', {'health_reach': True, 'health': ['not', 'an', 'object']}),
        ('health', {'health_reach': True, 'health': None}),
        ('endpoint', {'endpoint_age': 5, 'endpoint_breaker': None}),
        ('endpoint', {'endpoint_age': 5, 'endpoint_breaker': None, 'endpoint_error': 'unparseable'}),
        ('feeder', {'feeder_tail_error': 'tail failed'}),
        ('feeder', {'feeder_err_polls': None, 'feeder_tail_error': 'no poll lines'}),
        ('tor_circuits', {'tor_net_warn': None}),
        ('journal_errors', {'journal_failed': ['errors']}),
        ('ssh_failures', {'journal_failed': ['ssh-failures']}),
        ('egress_drops', {'journal_failed': ['egress']}),
        ('ssh_unexpected', {'journal_failed': ['ssh-accepted']}),
        ('health', {'health_reach': False, 'health': None}),
    ]

    def test_each_source_failure_reads_unknown_never_ok(self):
        for check, override in self.CASES:
            with self.subTest(check=check, override=override):
                verdict, detail = self.evaluate(**override)[check]
                self.assertIsNone(verdict, (check, detail))
                self.assertIn('unknown', detail, (check, detail))

    def test_the_same_checks_with_their_sources_readable_are_ok(self):
        checks = self.evaluate()
        for check, _ in self.CASES:
            self.assertIs(checks[check][0], True, (check, checks[check]))

    def test_a_real_failure_is_still_a_failure_not_unknown(self):
        checks = self.evaluate(mem_avail_mb=100, anchor_age_h=40.0, endpoint_age=None, endpoint_error='missing',
                               calendar={'best_block': None, 'anchor_receipts': 'on', 'balance': 5},
                               units_system={'docker.service': 'inactive'})
        for check in ('mem', 'anchor_age', 'endpoint', 'calendar', 'units_system'):
            self.assertIs(checks[check][0], False, (check, checks[check]))

    def test_observe_marks_a_missing_and_an_unreadable_source(self):
        """observe itself: the adapter heartbeat and the receipts file
        missing, unreadable (chmod), empty and readable. Everything else is
        switched off or stubbed; no command runs."""
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp)
            cfg = review_cfg()
            for key in watch.SKIP_WHEN_EMPTY.values():
                cfg[key] = ''
            cfg['ENDPOINT_HEARTBEAT'] = str(d / 'heartbeat')
            cfg['RECEIPTS'] = str(d / 'receipts.jsonl')
            with mock.patch.object(watch, 'run', return_value=(0, '')), \
                    mock.patch.object(watch, 'kernel_versions', return_value=('k', 'k')):
                o = watch.observe(cfg, NOW, {'journal': NOW - 300})
                self.assertEqual((o['endpoint_error'], o['receipts_error']), ('missing', 'missing'))
                (d / 'heartbeat').write_text('ts=1 breaker=ok\n')
                (d / 'receipts.jsonl').write_text('')
                o = watch.observe(cfg, NOW, {'journal': NOW - 300})
                self.assertEqual((o['endpoint_error'], o['endpoint_breaker'], o['receipts_error'], o['anchors']),
                                 (None, 'ok', None, 0))
                restore = [unreadable(d / 'heartbeat'), unreadable(d / 'receipts.jsonl')]
                try:
                    o = watch.observe(cfg, NOW, {'journal': NOW - 300})
                finally:
                    for r in restore:
                        r()
                self.assertEqual((o['endpoint_error'], o['receipts_error']), ('unreadable', 'unreadable'))
                (d / 'receipts.jsonl').write_text('{"txid": "aa", "confirmed_at": %d}\nnot json\n' % (NOW - 7200))
                o = watch.observe(cfg, NOW, {'journal': NOW - 300})
                self.assertEqual(o['receipts_error'], 'unparseable')
            checks = watch.evaluate(o, cfg)
            self.assertIsNone(checks['anchor_age'][0])

    def test_malformed_bytes_at_a_source_are_unknown_and_stop_nothing(self):
        """observe against real files: invalid UTF-8 in the adapter heartbeat
        and in the receipts is that check's unknown, the other sources are
        still observed, and a run with such a source ends normally (gate
        review, 1)."""
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp)
            cfg = review_cfg()
            for key in watch.SKIP_WHEN_EMPTY.values():
                cfg[key] = ''
            cfg['ENDPOINT_HEARTBEAT'] = str(d / 'heartbeat')
            cfg['RECEIPTS'] = str(d / 'receipts.jsonl')
            (d / 'heartbeat').write_bytes(b'\xff')
            (d / 'receipts.jsonl').write_bytes(b'\xff\n')
            with mock.patch.object(watch, 'run', return_value=(0, '')), \
                    mock.patch.object(watch, 'kernel_versions', return_value=('k', 'k')):
                o = watch.observe(cfg, NOW, {'journal': NOW - 300})
            self.assertEqual((o['endpoint_error'], o['receipts_error']), ('unparseable', 'unparseable'))
            self.assertIn('journal_since', o, 'the other sources were observed')
            checks = watch.evaluate(o, cfg)
            self.assertIsNone(checks['endpoint'][0])
            self.assertIsNone(checks['anchor_age'][0])
            self.assertIn('unparseable', checks['endpoint'][1])
            # A /health body that is JSON but not an object.
            cfg2 = cfg_with(HEALTH_URL='http://127.0.0.1:8000/health')
            self.assertIsNone(watch.evaluate({'health_reach': True, 'health': ['not', 'an', 'object']}, cfg2)['health'][0])
            # And a run through real_run with such an observation ends with the status saying unknown.
            live = dict(review_cfg(), **{k: '' for k in watch.SKIP_WHEN_EMPTY.values()})
            live['ENDPOINT_HEARTBEAT'], live['RECEIPTS'] = cfg['ENDPOINT_HEARTBEAT'], cfg['RECEIPTS']
            with watcher_run(d, watch.evaluate(o, cfg), lambda *_: (True, 'ok')):
                with mock.patch.object(watch, 'load_config', return_value=live):
                    self.assertEqual(watch.real_run(False), 0)
            self.assertIn(' unknown ', (d / 'status').read_text())

    def test_a_failed_read_behind_a_fresh_file_is_unknown_not_ok(self):
        """The marker comes from the read itself: a fresh feeder log whose
        tail fails or holds no poll line, a fresh adapter heartbeat with no
        breaker field, a Tor heartbeat whose warning-window query fails
        (gate review, 2)."""
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp)
            cfg = review_cfg()
            for key in watch.SKIP_WHEN_EMPTY.values():
                cfg[key] = ''
            cfg['FEEDER_LOG'] = str(d / 'feeder.log')
            cfg['ENDPOINT_HEARTBEAT'] = str(d / 'heartbeat')
            cfg['TOR_CONTAINER'] = 'tor'
            (d / 'feeder.log').write_text('poll ok errors=0\n')
            (d / 'heartbeat').write_text('')

            def commands(tail_rc=0, tail_out='poll ok errors=0\n', warn_rc=0):
                def command(cmd, **kwargs):
                    if cmd[0] == 'tail':
                        return tail_rc, tail_out
                    if cmd[:2] == ['docker', 'logs']:
                        if cmd[3] == cfg['TOR_HB_MAX_H'] + 'h':
                            return 0, "Heartbeat: Tor's uptime is 1 day, with 12 circuits open.\n"
                        return warn_rc, ''
                    return 0, ''
                return command

            def observe(command):
                with mock.patch.object(watch, 'run', side_effect=command), \
                        mock.patch.object(watch, 'kernel_versions', return_value=('k', 'k')):
                    return watch.observe(cfg, NOW, {'journal': NOW - 300})
            o = observe(commands(tail_rc=1))
            self.assertIsNotNone(o['feeder_age'])
            self.assertEqual(o['feeder_tail_error'], 'tail failed')
            self.assertIsNone(watch.evaluate(o, cfg)['feeder'][0])
            o = observe(commands(tail_out='started\n'))
            self.assertEqual(o['feeder_tail_error'], 'no poll lines')
            self.assertIsNone(watch.evaluate(o, cfg)['feeder'][0])
            self.assertIsNone(o['endpoint_breaker'])
            self.assertIsNone(watch.evaluate(o, cfg)['endpoint'][0], 'fresh but without the field it needs')
            o = observe(commands(warn_rc=1))
            self.assertIsNone(o['tor_net_warn'])
            self.assertIsNone(watch.evaluate(o, cfg)['tor_circuits'][0])
            # The controls: the same sources whole.
            (d / 'heartbeat').write_text('ts=1 breaker=ok\n')
            o = observe(commands())
            checks = watch.evaluate(o, cfg)
            self.assertEqual((checks['feeder'][0], checks['endpoint'][0], checks['tor_circuits'][0]), (True, True, True))

    def test_an_unknown_check_never_stops_the_run(self):
        """W1. One source unreadable: the status names it as unknown, the
        other checks are evaluated, the run exits 0 when nothing is owed."""
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp)
            checks = dict(ALL_OK, temp=(None, 'temp unknown: no thermal reading'))
            with watcher_run(d, checks, lambda *_: (True, 'ok')):
                rc = watch.real_run(False)
            self.assertEqual(rc, 0)
            status = (d / 'status').read_text()
            self.assertIn(' unknown ', status)
            self.assertIn('temp=temp_unknown', status)


class Test_empty_knobs(unittest.TestCase):
    """W4. A box without a thermal sensor or a readable meminfo is not
    degraded for ever: an empty TEMP_C or MEM_MB skips the check like every
    other empty knob, and evaluate does not choke on the empty value."""

    def test_temp_and_mem_skip_when_their_knob_is_empty(self):
        cfg = cfg_with(TEMP_C='', MEM_MB='', **APPLIANCE)
        active = watch.active_checks(cfg)
        self.assertNotIn('temp', active)
        self.assertNotIn('mem', active)
        checks = watch.evaluate({'temp_c': None, 'mem_avail_mb': None}, cfg)
        self.assertIn('temp', checks)
        state, msgs = watch.decide({}, checks, NOW, cfg)
        self.assertNotIn('temp', state['fail_runs'])


# --- W5: the alarm rule --------------------------------------------------------------------

class Test_alarm_rule(unittest.TestCase):
    """W5. DEGRADED once when a check has failed CONFIRM_RUNS runs in a row,
    silence while it persists, RECOVERED once when every check has been ok
    CONFIRM_RUNS runs in a row, and a check that flaps never alarms; the
    same across a restart of the process, since the state is a file. Fault
    model: none; decide is pure and is fed a state that has been through
    JSON between runs."""

    def run_sequence(self, verdicts, cfg=None, reload=False):
        cfg = cfg or cfg_with(NAME='box', **APPLIANCE)
        state, out = {}, []
        for i, failing in enumerate(verdicts):
            checks = {n: (True, '') for n in watch.ORDER}
            for name in failing:
                checks[name] = (False, '%s is bad' % name)
            if reload:
                state = json.loads(json.dumps(state))
            state, msgs = watch.decide(state, checks, NOW + 300 * i, cfg)
            out.append(msgs)
        return out

    def test_degraded_once_recovered_once(self):
        out = self.run_sequence([[], ['mem'], ['mem'], ['mem'], ['mem'], [], [], []])
        self.assertEqual([len(m) for m in out], [0, 0, 1, 0, 0, 0, 1, 0])
        self.assertIn('box DEGRADED: mem is bad | still: none', out[2][0])
        self.assertIn('box RECOVERED: all 16 checks ok (was: mem;', out[6][0])

    def test_a_flapping_check_never_alarms(self):
        out = self.run_sequence([['mem'], [], ['mem'], [], ['mem'], [], ['mem'], []])
        self.assertEqual([m for m in out if m], [])

    def test_a_burst_alarms_at_once_and_clears_at_once(self):
        out = self.run_sequence([['ssh_unexpected'], [], []])
        self.assertEqual([len(m) for m in out], [1, 1, 0])
        self.assertIn('DEGRADED', out[0][0])
        self.assertIn('RECOVERED', out[1][0])

    def test_the_second_failure_joins_and_names_the_rest_and_no_recovered_until_all_clear(self):
        out = self.run_sequence([['mem'], ['mem'], ['mem', 'temp'], ['mem', 'temp'], ['temp'], ['temp'], [], []])
        self.assertIn('box DEGRADED: mem is bad | still: none', out[1][0])
        self.assertIn('box DEGRADED: temp is bad | still: mem is bad', out[3][0])
        self.assertEqual(out[5], [], 'mem recovered but temp still fails: no RECOVERED yet')
        self.assertIn('RECOVERED: all 16 checks ok (was: temp;', out[7][0])

    def test_the_transition_survives_a_reload_of_the_state_between_runs(self):
        plain = self.run_sequence([['mem'], ['mem'], ['mem'], [], []])
        reloaded = self.run_sequence([['mem'], ['mem'], ['mem'], [], []], reload=True)
        self.assertEqual(plain, reloaded)


class Test_decide_with_unknown(unittest.TestCase):
    """W5. An unknown check alarms like a failure after CONFIRM_RUNS, worded
    as unknown; an unknown whose cause is another failing check (health
    behind health_reach, the journal counts behind journal_read) is
    suspended: it neither alarms nor recovers nor appears as `still`, and
    it resumes with its next real verdict. Fault model: none."""

    def test_an_unknown_check_alarms_after_confirm_and_says_unknown(self):
        cfg = cfg_with(NAME='box', **APPLIANCE)
        checks = dict({n: (True, '') for n in watch.ORDER}, temp=(None, 'temp unknown: no thermal reading'))
        state, msgs = watch.decide({}, checks, NOW, cfg)
        self.assertEqual(msgs, [])
        state, msgs = watch.decide(state, checks, NOW + 300, cfg)
        self.assertEqual(msgs, ['box DEGRADED: temp unknown: no thermal reading | still: none'])
        self.assertEqual(state['delivered'], ['temp'])

    def test_an_unknown_owned_by_a_failing_owner_is_suspended_not_ok(self):
        cfg = cfg_with(NAME='box', **APPLIANCE)
        ok = {n: (True, '') for n in watch.ORDER}
        blind = dict(ok, journal_read=(False, 'journalctl failed: errors, ssh-accepted (window kept, read again next run)'),
                     journal_errors=(None, 'journal errors unknown: journalctl failed'),
                     ssh_failures=(None, 'ssh failures unknown: journalctl failed'),
                     egress_drops=(None, 'refused outbound unknown: journalctl failed'),
                     ssh_unexpected=(None, 'ssh sources unknown: journalctl failed'))
        state, msgs = watch.decide({}, blind, NOW, cfg)
        self.assertEqual(msgs, [], 'a burst check that is unknown does not alarm at once')
        state, msgs = watch.decide(state, blind, NOW + 300, cfg)
        self.assertEqual(msgs, ['box DEGRADED: journalctl failed: errors, ssh-accepted (window kept, read again next run) | still: none'])
        self.assertEqual(state['delivered'], ['journal_read'])
        self.assertNotIn('journal_errors', state['fail_runs'], 'suspended: no counter moved')
        line = watch.status_line(NOW + 300, state, blind, cfg)
        self.assertIn(' degraded ', line)
        self.assertIn('journal_errors=journal_errors_unknown', line)
        # The journal reads again: the owner recovers over two runs, and a
        # real burst alarms at once as before.
        burst = dict(ok, ssh_unexpected=(False, 'ssh accepted from 1 source not in SSH_KNOWN_SOURCES'))
        state, msgs = watch.decide(state, burst, NOW + 600, cfg)
        self.assertEqual(msgs, ['box DEGRADED: ssh accepted from 1 source not in SSH_KNOWN_SOURCES | still: journal_read recovering'])

    def test_a_delivered_check_that_turns_unknown_is_not_reported_as_still(self):
        """The hosted shape's fixture 04 in words: health degraded, then
        /health unreachable; the DEGRADED for the reach names no stale
        health detail, and health leaves the delivered set only through
        two real ok runs."""
        cfg = cfg_with(NAME='box')
        ok = {n: (True, '') for n in watch.ORDER}
        degraded = dict(ok, health=(False, 'health=degraded billing=overdue'))
        unreachable = dict(ok, health_reach=(False, 'no answer from /health'), health=(None, 'health unknown: /health unreachable'))
        state, msgs = watch.decide({}, degraded, NOW, cfg)
        state, msgs = watch.decide(state, degraded, NOW + 300, cfg)
        self.assertEqual(msgs, ['box DEGRADED: health=degraded billing=overdue | still: none'])
        state, msgs = watch.decide(state, unreachable, NOW + 600, cfg)
        state, msgs = watch.decide(state, unreachable, NOW + 900, cfg)
        self.assertEqual(msgs, ['box DEGRADED: no answer from /health | still: none'])
        self.assertIn('health', state['delivered'], 'suspended, not silently recovered')

    def test_the_heartbeat_names_a_failing_check_before_its_alarm_is_confirmed(self):
        """A first failed observation is not a healthy reading: the alarm
        waits for the second run, the heartbeat does not (gate review, 3)."""
        cfg = cfg_with(NAME='box', **APPLIANCE)
        ok = {n: (True, '') for n in watch.ORDER}
        low = dict(ok, mem=(False, 'mem available 100MB'))
        state, msgs = watch.decide({}, low, NOW, cfg)
        self.assertEqual(msgs, [], 'the two-run guard holds')
        o = {'disk': {'/': 9.1}, 'temp_c': 40.0, 'mem_avail_mb': 100, 'anchors': 4, 'anchor_age_h': 1.0, 'uptime_s': 100}
        line = watch.heartbeat_line(state, low, o, cfg)
        self.assertNotIn('ok 16/16', line)
        self.assertTrue(line.startswith('box heartbeat: degraded: mem available 100MB (not yet alarmed)'), line)
        state, msgs = watch.decide(state, low, NOW + 300, cfg)
        self.assertEqual(len(msgs), 1)
        line = watch.heartbeat_line(state, low, o, cfg)
        self.assertTrue(line.startswith('box heartbeat: degraded: mem available 100MB |') or line.endswith('mem available 100MB'), line)
        self.assertNotIn('not yet alarmed', line)

    def test_recovered_does_not_say_all_ok_while_another_check_is_not(self):
        """The alarm set emptying is the transition and is said; "all N
        checks ok" is said only when every verdict is ok now (gate
        review, 3)."""
        cfg = cfg_with(NAME='box', **APPLIANCE)
        ok = {n: (True, '') for n in watch.ORDER}
        low = dict(ok, mem=(False, 'mem available 100MB'))
        blind = dict(ok, temp=(None, 'temp unknown: no thermal reading'))
        state, out = {}, []
        for i, checks in enumerate([low, low, ok, blind]):
            state, msgs = watch.decide(state, checks, NOW + 300 * i, cfg)
            out.append(msgs)
        self.assertEqual(len(out[3]), 1, out)
        self.assertNotIn('all 16 checks ok', out[3][0])
        self.assertIn('RECOVERED: mem ok', out[3][0])
        self.assertIn('not all clear: temp unknown: no thermal reading', out[3][0])
        # The control: the same recovery with everything ok says so.
        state, out = {}, []
        for i, checks in enumerate([low, low, ok, ok]):
            state, msgs = watch.decide(state, checks, NOW + 300 * i, cfg)
            out.append(msgs)
        self.assertIn('RECOVERED: all 16 checks ok (was: mem;', out[3][0])

    def test_status_words(self):
        cfg = cfg_with(NAME='box', **APPLIANCE)
        ok = {n: (True, '') for n in watch.ORDER}
        self.assertIn(' ok 16/16', watch.status_line(NOW, {}, ok, cfg))
        unk = dict(ok, temp=(None, 'temp unknown: no thermal reading'))
        line = watch.status_line(NOW, {}, unk, cfg)
        self.assertIn(' unknown 15/16 temp=temp_unknown:_no_thermal_reading', line)
        bad = dict(unk, mem=(False, 'mem available 100MB'))
        line = watch.status_line(NOW, {}, bad, cfg, queued=3)
        self.assertIn(' degraded 14/16 mem=mem_available_100MB temp=temp_unknown:_no_thermal_reading queued=3', line)

    def test_the_heartbeat_names_unknown_checks_and_the_configured_mounts(self):
        cfg = cfg_with(NAME='box', DISK_ROOT='/srv', DISK_BOOT='', **{k: v for k, v in APPLIANCE.items() if k != 'DISK_BOOT'})
        ok = {n: (True, '') for n in watch.ORDER}
        unk = dict(ok, temp=(None, 'temp unknown: no thermal reading'))
        o = {'disk': {'/srv': 33.3}, 'temp_c': None, 'mem_avail_mb': 4000, 'anchors': 4, 'anchor_age_h': 1.0, 'uptime_s': 100}
        line = watch.heartbeat_line({'delivered': []}, unk, o, cfg)
        self.assertTrue(line.startswith('box heartbeat: unknown: temp'), line)
        self.assertIn('disk 33.3%', line)
        self.assertNotIn('disk ?%', line)
        line = watch.heartbeat_line({'delivered': ['mem']}, dict(unk, mem=(False, 'mem available 100MB')), o, cfg)
        self.assertTrue(line.startswith('box heartbeat: degraded: mem available 100MB | unknown: temp'), line)
        line = watch.heartbeat_line({'delivered': []}, ok, dict(o, temp_c=40.0), cfg)
        self.assertTrue(line.startswith('box heartbeat: ok 16/16'), line)


# --- W7: the heartbeat --------------------------------------------------------------------------

class Test_heartbeat(unittest.TestCase):
    """W7. One heartbeat per UTC day, at the first run at or after
    HEARTBEAT_HOUR; a day the box was down gets no heartbeat, and the next
    day's comes once. Its absence is what an outside reader notices; the
    box makes no claim about its own death. Fault model: none."""

    def test_one_per_day_and_a_missed_day_is_not_made_up(self):
        cfg = cfg_with(NAME='box', HEARTBEAT_HOUR='8', **APPLIANCE)
        ok = {n: (True, '') for n in watch.ORDER}
        day = 1788566400   # 2026-09-04 00:00:00Z
        pending = []
        state = {}
        for t in (day + 7 * 3600, day + 8 * 3600, day + 9 * 3600, day + 23 * 3600,
                  day + 3 * 86400 + 8 * 3600 + 300, day + 3 * 86400 + 9 * 3600):
            state, msgs = watch.decide(state, ok, t, cfg)
            pending.append(state.pop('heartbeat_pending', False))
        self.assertEqual(pending, [False, True, False, False, True, False])


# --- W1: recording a transition, and where a stop may fall -------------------------------------

class Test_recording_boundaries(unittest.TestCase):
    """W1. A transition and the messages it produces are recorded in one
    write of state.json (the delivered set, the cursor and the messages
    owed), then copied to the outbox, then marked as copied. A stop before
    the write records nothing and the next run alarms once; a stop after
    it, before or after the outbox write, queues the message exactly once
    and never twice. Fault models: an exception injected at the n-th
    write_json_atomic of a run; a real child killed after the state write."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.dir = pathlib.Path(self.tmpdir.name)
        self.sent = []

    def sender(self, _, text):
        self.sent.append(text)
        return True, 'ok'

    def state(self):
        return json.loads((self.dir / 'state.json').read_text())

    def queued(self):
        p = self.dir / 'outbox.json'
        return json.loads(p.read_text()) if p.exists() else None

    def stop_at(self, n):
        with watcher_run(self.dir, BURST, self.sender):
            with fail_on_call(watch, 'write_json_atomic', n, exc=Stop()) as hit:
                try:
                    rc = watch.real_run(False)
                except Stop:
                    rc = 'stopped'
        return rc, hit['fired']

    def finish(self, checks=ALL_OK, now=NOW + 300):
        with watcher_run(self.dir, checks, self.sender, now=now):
            return watch.real_run(False)

    def test_a_stop_before_the_state_write_records_nothing_and_alarms_once_next_run(self):
        rc, fired = self.stop_at(1)
        self.assertTrue(fired)
        self.assertNotEqual(rc, 0)
        self.assertFalse((self.dir / 'state.json').exists())
        self.assertIsNone(self.queued())
        self.assertEqual(self.sent, [])
        self.assertEqual(self.finish(BURST), 0)
        self.assertEqual(len([t for t in self.sent if 'DEGRADED' in t]), 1, self.sent)
        self.assertEqual(self.state().get('owed', []), [])
        self.assertEqual(self.queued(), [])

    def test_a_stop_after_the_state_write_before_the_outbox_queues_exactly_once(self):
        rc, fired = self.stop_at(2)
        self.assertTrue(fired)
        self.assertNotEqual(rc, 0)
        st = self.state()
        self.assertEqual(len(st['owed']), 1, 'the message is owed in the state')
        self.assertEqual(st['cursors']['journal'], NOW, 'and the cursor moved in the same write')
        self.assertIsNone(self.queued())
        self.assertEqual(self.finish(BURST), 0)
        self.assertEqual(len([t for t in self.sent if 'DEGRADED' in t]), 1, self.sent)
        self.assertEqual(self.state().get('owed', []), [])

    def test_a_stop_after_the_outbox_write_before_the_owed_clear_queues_exactly_once(self):
        rc, fired = self.stop_at(3)
        self.assertTrue(fired)
        self.assertNotEqual(rc, 0)
        self.assertEqual(len(self.queued()), 1)
        self.assertEqual(len(self.state()['owed']), 1, 'owed and queued at once: the next run must not queue it again')
        self.assertEqual(self.finish(BURST), 0)
        self.assertEqual(len([t for t in self.sent if 'DEGRADED' in t]), 1, self.sent)
        self.assertEqual(self.state().get('owed', []), [])
        self.assertEqual(self.queued(), [])

    def test_the_cursor_moves_in_the_same_write_as_the_owed_messages_even_when_the_outbox_cannot_be_written(self):
        """The outbox write fails (ENOSPC injected for that path alone): the
        burst is durable in the state, so the cursor may move; the run still
        tries to send (at least once); the next run queues it once."""
        real = watch.write_json_atomic

        def outbox_fails(path, data):
            if path == str(self.dir / 'outbox.json'):
                raise OSError(28, 'No space left on device')
            return real(path, data)
        with watcher_run(self.dir, BURST, lambda *_: (False, 'down')):
            with mock.patch.object(watch, 'write_json_atomic', side_effect=outbox_fails):
                rc = watch.real_run(False)
        self.assertEqual(rc, 1)
        st = self.state()
        self.assertEqual(st['cursors']['journal'], NOW)
        self.assertEqual(len(st['owed']), 1)
        self.assertIsNone(self.queued())
        self.assertEqual(self.finish(BURST), 0)
        self.assertEqual(len([t for t in self.sent if 'DEGRADED' in t]), 1, self.sent)

    def test_a_stop_after_the_outbox_write_keeps_the_cap_decision_on_replay(self):
        """205 messages owed and the outbox empty: the record keeps 199 and a
        notice of 6 drops; a stop before the record is cleared must not
        make the next run drop again or reorder (gate review, 4). Fault
        model: a stop injected at the state write that clears the record."""
        owed = [{'text': 'alert %03d' % n, 'queued': '2026-09-%02dT%02d:%02d:00Z' % (1 + n // 1440, n // 60 % 24, n % 60)}
                for n in range(watch.OUTBOX_MAX + 5)]
        (self.dir / 'state.json').write_text(json.dumps({'owed': owed}))
        real = watch.write_json_atomic

        def stop_at_the_clear(path, data):
            if path == str(self.dir / 'state.json') and not data.get('owed'):
                raise Stop()
            return real(path, data)
        with watcher_run(self.dir, ALL_OK, lambda *_: (False, 'down')):
            with mock.patch.object(watch, 'write_json_atomic', side_effect=stop_at_the_clear):
                with self.assertRaises(Stop):
                    watch.real_run(False)
        first = self.queued()
        self.assertEqual((first[0]['dropped'], first[1]['text'], len(first)), (6, 'alert 006', watch.OUTBOX_MAX))
        with watcher_run(self.dir, ALL_OK, lambda *_: (False, 'down'), now=NOW + 300):
            self.assertEqual(watch.real_run(False), 1, 'nothing can be delivered: the queue stays')
        recovered = self.queued()
        self.assertEqual(recovered[0]['dropped'], 6, 'the recorded decision is copied, not made again')
        self.assertEqual(recovered[1:], first[1:], 'the same newest messages, in order')
        self.assertFalse(self.state().get('owed'))

    def test_a_stop_before_the_outbox_write_replays_the_recorded_queue(self):
        """Three old messages queued, one new: a stop between the record and
        the outbox write leaves the record holding the four; the next run
        copies it and the four are delivered once each, in order."""
        old = [{'text': 'old %d' % i, 'queued': '2026-09-01T00:0%d:00Z' % i} for i in range(3)]
        (self.dir / 'outbox.json').write_text(json.dumps(old))
        rc, fired = self.stop_at(2)
        self.assertTrue(fired)
        self.assertEqual([m['text'] for m in self.state()['owed']][:3], ['old 0', 'old 1', 'old 2'])
        self.assertEqual(len(self.state()['owed']), 4)
        self.assertEqual(len(self.queued()), 3, 'the outbox is the older queue')
        self.assertEqual(self.finish(BURST), 0)
        self.assertEqual(self.sent[:3], ['old 0', 'old 1', 'old 2'])
        self.assertEqual(len([t for t in self.sent if 'DEGRADED' in t]), 1, self.sent)
        self.assertEqual(self.queued(), [])

    CHILD = """
import importlib.util, json, sys, time
spec = importlib.util.spec_from_file_location('watch', sys.argv[1])
watch = importlib.util.module_from_spec(spec); spec.loader.exec_module(watch)
d, boundary = sys.argv[2], sys.argv[3]
checks = {k: tuple(v) for k, v in json.load(open(sys.argv[4])).items()}
cfg = json.load(open(sys.argv[5]))
watch.WATCH_DIR, watch.STATE, watch.STATUS = d, d + '/state.json', d + '/status'
watch.load_config = lambda: cfg
watch.observe = lambda *a, **k: {}
watch.evaluate = lambda o, c: checks
watch.send = lambda c, text: (True, 'ok')
watch.time.time = lambda: %d
real = watch.write_json_atomic
def write_json_atomic(path, data):
    real(path, data)
    if path == watch.STATE and data.get('owed') and boundary == 'state written':
        print('paused at state written', flush=True)
        sys.stdin.readline()
    if path == watch.outbox_path() and boundary == 'outbox written':
        print('paused at outbox written', flush=True)
        sys.stdin.readline()
watch.write_json_atomic = write_json_atomic
sys.exit(watch.real_run(False))
""" % NOW

    def kill_at(self, boundary):
        checks_path, cfg_path = self.dir / 'checks.json', self.dir / 'cfg.json'
        checks_path.write_text(json.dumps({k: list(v) for k, v in BURST.items()}))
        cfg_path.write_text(json.dumps(review_cfg()))
        child = subprocess.Popen([sys.executable, '-B', '-c', self.CHILD, str(TOOL), str(self.dir), boundary,
                                  str(checks_path), str(cfg_path)],
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

    def test_a_child_killed_after_recording_and_before_queueing_alarms_exactly_once(self):
        self.kill_at('state written')
        self.assertEqual(len(self.state()['owed']), 1)
        self.assertIsNone(self.queued())
        self.assertEqual(self.finish(BURST), 0)
        self.assertEqual(len([t for t in self.sent if 'DEGRADED' in t]), 1, self.sent)
        self.assertEqual(self.finish(ALL_OK, now=NOW + 600), 0)
        self.assertEqual(len([t for t in self.sent if 'RECOVERED' in t]), 1, self.sent)

    def test_a_child_killed_after_queueing_and_before_the_owed_clear_alarms_exactly_once(self):
        self.kill_at('outbox written')
        self.assertEqual(len(self.queued()), 1)
        self.assertEqual(len(self.state()['owed']), 1)
        self.assertEqual(self.finish(BURST), 0)
        self.assertEqual(len([t for t in self.sent if 'DEGRADED' in t]), 1, self.sent)


# --- W6: delivery, at least once, and the cap ----------------------------------------------------

class Test_delivery_and_cap(unittest.TestCase):
    """W6. A send that fails leaves the message queued, the run exits 1,
    the status line counts what is queued, and the next run retries in
    order; the queue's bound of OUTBOX_MAX is a notice at the head of the
    queue, never a silent eviction. Fault model: a sender that fails."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.dir = pathlib.Path(self.tmpdir.name)

    def queued(self):
        return json.loads((self.dir / 'outbox.json').read_text())

    def test_a_failed_send_is_queued_retried_and_reported_undelivered(self):
        sent = []
        with watcher_run(self.dir, BURST, lambda *_: (False, 'down')):
            self.assertEqual(watch.real_run(False), 1)
        self.assertEqual(len(self.queued()), 1)
        self.assertIn('queued=1', (self.dir / 'status').read_text())
        with watcher_run(self.dir, ALL_OK, lambda _, text: sent.append(text) or (True, 'ok'), now=NOW + 300):
            self.assertEqual(watch.real_run(False), 0)
        self.assertEqual([t.split(': ')[0].split(' ')[1] for t in sent], ['DEGRADED', 'RECOVERED'], 'in order')
        self.assertEqual(self.queued(), [])
        self.assertNotIn('queued=', (self.dir / 'status').read_text())

    def test_the_cap_is_a_notice_at_the_head_never_a_silent_eviction(self):
        (self.dir / 'outbox.json').write_text(json.dumps(
            [{'text': 'old %d' % i, 'queued': '2026-09-01T00:%02d:00Z' % (i % 60)} for i in range(watch.OUTBOX_MAX + 5)]))
        with watcher_run(self.dir, BURST, lambda *_: (False, 'down')):
            self.assertEqual(watch.real_run(False), 1)
        queue = self.queued()
        self.assertEqual(len(queue), watch.OUTBOX_MAX)
        self.assertTrue(queue[0].get('cap'), queue[0])
        self.assertIn('7 oldest alerts dropped', queue[0]['text'])
        self.assertIn('box WATCHER: outbox over %d' % watch.OUTBOX_MAX, queue[0]['text'])
        self.assertEqual(queue[1]['text'], 'old 7')
        self.assertIn('1 source', queue[-1]['text'])
        # A new message while still at the cap: the notice folds, the
        # count grows, and it stays first in line.
        with watcher_run(self.dir, ALL_OK, lambda *_: (False, 'down'), now=NOW + 300):
            watch.real_run(False)
        queue = self.queued()
        self.assertEqual(len(queue), watch.OUTBOX_MAX)
        self.assertTrue(queue[0].get('cap'))
        self.assertIn('8 oldest alerts dropped', queue[0]['text'])
        self.assertEqual(sum(1 for m in queue if m.get('cap')), 1)


# --- W8: the state file, unreadable or corrupt --------------------------------------------------

class Test_state_recovery(unittest.TestCase):
    """W8. A state file that exists but cannot be read fails the run with
    nothing touched; one whose bytes are not a state object is set aside
    as state.json.corrupt-<12 hex of the bytes' sha256>, the run goes on
    from a fresh state and queues a notice saying what was lost; the
    quarantine interrupted is repeated cleanly. Fault models: chmod; an
    exception injected at the aside copy's rename."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.dir = pathlib.Path(self.tmpdir.name)
        self.state = self.dir / 'state.json'

    def asides(self):
        return sorted(p for p in self.dir.iterdir() if p.name.startswith('state.json.corrupt-'))

    def test_an_unreadable_state_fails_the_run_and_touches_nothing(self):
        self.state.write_text(json.dumps({'cursors': {'journal': NOW - 300}, 'delivered': ['mem']}))
        self.addCleanup(unreadable(self.state))
        sent, logged = [], []
        with watcher_run(self.dir, BURST, lambda _, t: sent.append(t) or (True, 'ok'), quiet=False):
            with mock.patch.object(watch, 'log', side_effect=logged.append):
                rc = watch.real_run(False)
        self.assertEqual(rc, 1)
        self.assertEqual(sent, [])
        self.assertFalse((self.dir / 'outbox.json').exists())
        self.assertFalse((self.dir / 'status').exists(), 'nothing observed, nothing written')
        self.assertTrue(any('state unreadable' in l for l in logged), logged)
        self.assertFalse(any(str(self.dir) in l for l in logged), logged)

    def test_a_corrupt_state_is_set_aside_reported_and_the_run_goes_on(self):
        self.state.write_bytes(b'{"delivered": ["mem"], not json')
        sent = []
        with watcher_run(self.dir, SLOW, lambda _, t: sent.append(t) or (True, 'ok')):
            rc = watch.real_run(False)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.asides()), 1)
        self.assertEqual(self.asides()[0].read_bytes(), b'{"delivered": ["mem"], not json')
        self.assertEqual(len(sent), 1, sent)
        self.assertIn('WATCHER: state.json could not be read', sent[0])
        self.assertIn(self.asides()[0].name, sent[0])
        self.assertIn('alarm again once', sent[0])
        st = json.loads(self.state.read_text())
        self.assertEqual(st['fail_runs'].get('mem'), 1, 'the run went on from a fresh state')
        self.assertEqual(st['cursors']['journal'], NOW)
        # The transition lost with the old state is made again once, as the notice said.
        with watcher_run(self.dir, SLOW, lambda _, t: sent.append(t) or (True, 'ok'), now=NOW + 300):
            self.assertEqual(watch.real_run(False), 0)
        self.assertEqual(len([t for t in sent if 'DEGRADED' in t]), 1, sent)

    def test_a_state_object_with_a_field_of_the_wrong_shape_is_set_aside_and_reported(self):
        """Valid JSON, wrong fields: counters as a list, an owed message as a
        bare string, a cursor as text. Each is set aside with a notice, and
        a bare string under owed is never silently dropped (gate review, 5)."""
        for raw in (b'{"fail_runs": []}', b'{"owed": ["undelivered alert"]}', b'{"cursors": {"journal": "yesterday"}}'):
            with self.subTest(raw=raw):
                for p in list(self.dir.iterdir()):
                    p.unlink()
                self.state.write_bytes(raw)
                sent = []
                with watcher_run(self.dir, ALL_OK, lambda _, t: sent.append(t) or (True, 'ok')):
                    rc = watch.real_run(False)
                self.assertEqual(rc, 0, sent)
                self.assertEqual(len(self.asides()), 1, os.listdir(self.dir))
                self.assertEqual(self.asides()[0].read_bytes(), raw)
                self.assertEqual(len(sent), 1, sent)
                self.assertIn('state.json could not be read', sent[0])
                self.assertIn('alert the old state still owed is in the aside file only', sent[0])
                self.assertTrue(watch.valid_state(json.loads(self.state.read_text())))

    def test_a_message_with_a_field_of_the_wrong_shape_is_set_aside_and_reported(self):
        (self.dir / 'outbox.json').write_bytes(b'[{"text": "alert", "queued": []}]')
        sent = []
        with watcher_run(self.dir, ALL_OK, lambda _, t: sent.append(t) or (True, 'ok')):
            rc = watch.real_run(False)
        self.assertEqual(rc, 0, sent)
        asides = [p for p in self.dir.iterdir() if p.name.startswith('outbox.json.corrupt-')]
        self.assertEqual(len(asides), 1)
        self.assertEqual(len(sent), 1, sent)
        self.assertIn('outbox.json could not be read', sent[0])

    def test_a_dry_run_sets_nothing_aside(self):
        """--dry inspects: a corrupt state is named in the log and nothing is
        written but the status (gate review, documentation correction)."""
        self.state.write_bytes(b'{not json')
        logged = []
        with watcher_run(self.dir, ALL_OK, lambda *_: (True, 'ok'), quiet=False), \
                mock.patch.object(watch, 'log', side_effect=logged.append):
            self.assertEqual(watch.real_run(True), 0)
        self.assertEqual(self.asides(), [])
        self.assertEqual(self.state.read_bytes(), b'{not json')
        self.assertFalse((self.dir / 'outbox.json').exists())
        self.assertTrue(any('would set' in l and 'aside' in l for l in logged), logged)
        self.assertTrue((self.dir / 'status').exists())

    def test_the_state_quarantine_interrupted_is_repeated_cleanly(self):
        self.state.write_bytes(b'{not json')
        real_replace = os.replace
        calls = []

        def fail_first_aside(src, dst):
            if 'state.json.corrupt-' in dst and not calls:
                calls.append(dst)
                raise OSError(5, 'injected stop before the aside copy lands')
            return real_replace(src, dst)
        with watcher_run(self.dir, ALL_OK, lambda *_: (True, 'ok')):
            with mock.patch.object(watch.os, 'replace', side_effect=fail_first_aside):
                try:
                    watch.real_run(False)
                except OSError:
                    pass
        self.assertEqual(self.state.read_bytes(), b'{not json', 'the corrupt file is still there to be found')
        self.assertEqual(self.asides(), [])
        sent = []
        with watcher_run(self.dir, ALL_OK, lambda _, t: sent.append(t) or (True, 'ok')):
            self.assertEqual(watch.real_run(False), 0)
        self.assertEqual(len(self.asides()), 1)
        self.assertEqual(len(sent), 1)
        with watcher_run(self.dir, ALL_OK, lambda _, t: sent.append(t) or (True, 'ok'), now=NOW + 300):
            self.assertEqual(watch.real_run(False), 0)
        self.assertEqual(len(sent), 1, 'the notice is sent once')
        self.assertEqual(len(self.asides()), 1)


# --- W9: two runs ---------------------------------------------------------------------------------

class Test_two_runs(unittest.TestCase):
    """W9. The lock is taken before anything is observed: the run that loses
    it observes nothing, writes nothing and exits 1, and the window it did
    not read is read by the next run because the cursor did not move.
    Fault model: the lock held in-process (flock is per descriptor). The
    two-process case is test_watch.Test_run_lock."""

    def test_the_loser_observes_nothing_and_the_window_is_read_next_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp)
            (d / 'state.json').write_text(json.dumps({'cursors': {'journal': NOW - 900}}))
            observed = []

            def observe(cfg, now, cursors, want_updates=False):
                observed.append(cursors.get('journal'))
                return {'journal_since': cursors.get('journal')}
            with watcher_run(d, ALL_OK, lambda *_: (True, 'ok')), mock.patch.object(watch, 'observe', side_effect=observe), \
                    mock.patch.object(watch, 'LOCK_WAIT', 0.2):
                with watch.run_lock():
                    rc = watch.real_run(False)
                self.assertEqual(rc, 1)
                self.assertEqual(observed, [], 'the loser never observed')
                self.assertEqual(json.loads((d / 'state.json').read_text())['cursors']['journal'], NOW - 900)
                self.assertEqual(watch.real_run(False), 0)
            self.assertEqual(observed, [NOW - 900], 'the next run reads from the cursor the loser left alone')


# --- amnesia: what leaves the box --------------------------------------------------------------------

class Test_messages_name_no_identity(unittest.TestCase):
    """Every text that leaves the box (an ntfy message) or is read by
    another tool (the status line) names the box and what failed, never a
    login's source address or a path; the log on the box keeps the
    address. Fault model: none."""

    def test_an_unexpected_ssh_login_is_counted_off_box_and_named_only_in_the_log(self):
        cfg = cfg_with(NAME='box', SSH_KNOWN_SOURCES='192.168.0.0/24')
        checks = watch.evaluate({'ssh_sources': ['192.168.0.5', '203.0.113.7'], 'journal_failed': []}, cfg)
        self.assertEqual(checks['ssh_unexpected'], (False, 'ssh accepted from 1 source not in SSH_KNOWN_SOURCES'))
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp)
            logged, sent = [], []
            with watcher_run(d, BURST, lambda _, t: sent.append(t) or (True, 'ok'), quiet=False), \
                    mock.patch.object(watch, 'observe', return_value={'ssh_sources': ['203.0.113.7'], 'journal_failed': []}), \
                    mock.patch.object(watch, 'log', side_effect=logged.append):
                self.assertEqual(watch.real_run(False), 0)
            self.assertEqual(len(sent), 1)
            self.assertNotIn('203.0.113.7', sent[0])
            self.assertNotIn('203.0.113.7', (d / 'status').read_text())
            self.assertTrue(any('203.0.113.7' in l and 'ssh accepted from' in l for l in logged), logged)

    def test_a_configured_mount_never_leaves_in_a_message(self):
        """The disk checks publish their role and the measurement; the mount
        is configuration (gate review, 6)."""
        private = '/mnt/Example-Client-Laboratory/archive'
        cfg = cfg_with(NAME='box', DISK_ROOT=private, **{k: v for k, v in APPLIANCE.items()})
        checks = watch.evaluate({'disk': {private: 99.0}}, cfg)
        self.assertEqual(checks['disk_root'], (False, 'disk_root at 99.0%'))
        self.assertEqual(watch.evaluate({'disk': {}}, cfg)['disk_root'], (None, 'disk_root unknown: mount unreadable'))
        full = {n: (True, '') for n in watch.ORDER}
        full['disk_root'] = checks['disk_root']
        state, msgs = watch.decide({}, full, NOW, cfg)
        state, msgs = watch.decide(state, full, NOW + 300, cfg)
        self.assertEqual(len(msgs), 1)
        self.assertNotIn(private, msgs[0])
        self.assertNotIn(private, watch.status_line(NOW + 300, state, full, cfg))
        self.assertNotIn(private, watch.heartbeat_line(state, full, {'disk': {private: 99.0}}, cfg))

    def test_lock_and_outbox_messages_name_no_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp) / 'client-acme-7731'
            d.mkdir()
            logged = []
            with watcher_run(d, ALL_OK, lambda *_: (True, 'ok'), quiet=False), \
                    mock.patch.object(watch, 'log', side_effect=logged.append), mock.patch.object(watch, 'LOCK_WAIT', 0.2):
                with watch.run_lock():
                    self.assertEqual(watch.real_run(False), 1)
                (d / 'outbox.json').write_text('[]')
                restore = unreadable(d / 'outbox.json')
                try:
                    self.assertEqual(watch.real_run(False), 1)
                finally:
                    restore()
            self.assertTrue(any('locked' in l for l in logged), logged)
            self.assertTrue(any('outbox unreadable' in l for l in logged), logged)
            for line in logged:
                self.assertNotIn('client-acme-7731', line, line)


# --- W3: journalctl's exit status is the query's, not the match's (R10) --------------------------

class Test_journal_exit_semantics(unittest.TestCase):
    """W3 (2026-09-18 cold review R10). Under -q, `journalctl -g PATTERN`
    exits 1 when nothing matches (systemd v257, journalctl-show.c), so a
    quiet window used to read as a failed query: the cursor never moved
    and journal_read alarmed on every calm box. The patterns are matched
    here now, over the window's lines, and a nonzero exit is a failed
    query. Fault model: a command double that answers as journalctl does
    (exit 1, no output, to a -g query that matches nothing; the window's
    lines to the others); a double that fails every query."""

    SSH = ("Sep 18 10:00:01 box sshd[1]: Accepted publickey for ops from 203.0.113.7 port 5 ssh2\n"
           "Sep 18 10:00:02 box sshd[2]: Failed password for root from 198.51.100.9 port 6 ssh2\n"
           "Sep 18 10:00:03 box sshd[3]: Invalid user admin from 198.51.100.9 port 7\n"
           "Sep 18 10:00:04 box sshd[4]: Connection closed by 198.51.100.9 port 7\n")
    KERNEL = ("Sep 18 10:00:05 box kernel: egress-drop IN= OUT=eth0 DST=203.0.113.9\n"
              "Sep 18 10:00:06 box kernel: usb 1-1: new device\n")

    def journalctl_like(self, seen):
        def command(cmd, **kwargs):
            if cmd[0] != 'journalctl':
                return 0, ''
            seen.append(cmd)
            if '-g' in cmd:
                return 1, ''          # no match under -q: exit 1 and silence
            if '-u' in cmd:
                return 0, self.SSH
            if '-k' in cmd:
                return 0, self.KERNEL
            return 0, ''              # -p err: a quiet window
        return command

    def config(self):
        cfg = review_cfg(NTFY_URL='')
        for key in watch.SKIP_WHEN_EMPTY.values():
            cfg[key] = ''
        return cfg

    def test_the_patterns_are_matched_here_and_a_quiet_window_is_a_successful_read(self):
        seen = []
        with mock.patch.object(watch, 'run', side_effect=self.journalctl_like(seen)), \
                mock.patch.object(watch, 'kernel_versions', return_value=('k', 'k')):
            o = watch.observe(self.config(), NOW, {'journal': NOW - 300})
        self.assertEqual(len(seen), 5, 'every journal query was made')
        self.assertFalse(any('-g' in cmd for cmd in seen), 'no query asks journalctl to match the pattern')
        self.assertEqual(o['journal_failed'], [])
        self.assertEqual(o['journal_errors'], 0)
        self.assertEqual(o['ssh_failures'], 2)
        self.assertEqual(o['ssh_sources'], ['203.0.113.7'])
        self.assertEqual(o['egress_drops'], 1)
        checks = watch.evaluate(o, self.config())
        self.assertIs(checks['journal_read'][0], True)
        self.assertIs(checks['ssh_failures'][0], True)

    def run_three(self, command):
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp)
            (d / 'state.json').write_text(json.dumps({'cursors': {'journal': NOW - 300}}))
            with mock.patch.multiple(watch, WATCH_DIR=str(d), STATE=str(d / 'state.json'), STATUS=str(d / 'status')), \
                    mock.patch.object(watch, 'load_config', return_value=self.config()), \
                    mock.patch.object(watch, 'run', side_effect=command), \
                    mock.patch.object(watch, 'kernel_versions', return_value=('k', 'k')), \
                    mock.patch.object(watch.time, 'time', return_value=NOW), mock.patch.object(watch, 'log'):
                codes = [watch.real_run(False) for _ in range(3)]
            return codes, json.loads((d / 'state.json').read_text())

    def test_three_quiet_runs_move_the_cursor_and_alarm_nothing(self):
        codes, state = self.run_three(lambda cmd, **kw: (1, '') if cmd[0] == 'journalctl' and '-g' in cmd else (0, ''))
        self.assertEqual(codes, [0, 0, 0])
        self.assertEqual(state['cursors']['journal'], NOW, 'a quiet window is read; the cursor moves past it')
        self.assertNotIn('journal_read', state['delivered'])

    def test_a_query_that_fails_still_keeps_the_cursor(self):
        codes, state = self.run_three(lambda cmd, **kw: (2, ''))
        self.assertEqual(state['cursors']['journal'], NOW - 300, 'the unread window is read again next run')
        self.assertIn('journal_read', state['delivered'])


# --- W4: the newest confirmation, not the last line (R11) -----------------------------------------

class Test_receipt_order(unittest.TestCase):
    """W4 anchor_age (2026-09-18 cold review R11). C5 lets a receipt
    recovered from its marker be appended after later anchors' lines, and
    the check used to take the last line as the newest confirmation, so a
    recovery made a fresh anchor read as stale. Now the age is from the
    newest confirmed_at; a confirmed_at that is not a finite number makes
    the file unparseable and the check unknown. Fault model: receipts
    files written in the orders C5 allows; malformed values."""

    def observe(self, lines):
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp)
            receipts = d / 'receipts.jsonl'
            receipts.write_text(''.join(
                (l if isinstance(l, str) else json.dumps({'txid': '%064x' % i, 'confirmed_at': l})) + '\n'
                for i, l in enumerate(lines)))
            cfg = review_cfg(RECEIPTS=str(receipts))
            for key in watch.SKIP_WHEN_EMPTY.values():
                if key != 'RECEIPTS':
                    cfg[key] = ''
            with mock.patch.object(watch, 'run', return_value=(0, '')), \
                    mock.patch.object(watch, 'kernel_versions', return_value=('k', 'k')):
                o = watch.observe(cfg, NOW, {})
            return o, watch.evaluate(o, cfg)['anchor_age']

    def test_a_receipt_recovered_late_does_not_make_a_fresh_anchor_stale(self):
        o, verdict = self.observe([NOW - 3600, NOW - 50 * 3600])
        self.assertEqual(o['anchors'], 2)
        self.assertEqual(o['anchor_age_h'], 1.0, 'append order is not confirmation order')
        self.assertIs(verdict[0], True, verdict)

    def test_the_chronological_control_and_genuine_staleness(self):
        self.assertIs(self.observe([NOW - 50 * 3600, NOW - 3600])[1][0], True)
        self.assertIs(self.observe([NOW - 50 * 3600])[1][0], False)
        self.assertIs(self.observe([NOW - 50 * 3600, NOW - 40 * 3600])[1][0], False, 'the newest is still too old')

    def test_a_confirmed_at_that_is_not_a_finite_number_is_unparseable_not_an_age(self):
        for bad in ('"yesterday"', 'true', 'null', '1e309', '[1]'):
            with self.subTest(confirmed_at=bad):
                o, verdict = self.observe([NOW - 3600, '{"txid": "%064x", "confirmed_at": %s}' % (7, bad)])
                self.assertEqual(o['receipts_error'], 'unparseable')
                self.assertIsNone(o['anchor_age_h'])
                self.assertIsNone(verdict[0])
                self.assertIn('unknown', verdict[1])


# --- W8: numbers the run cannot use (R12) ----------------------------------------------------------

class Test_numeric_poison(unittest.TestCase):
    """W8 (2026-09-18 cold review R12). JSON's 1e309 decodes to infinity,
    which passed the state's type check and then failed int(cursor) on
    every run, past the quarantine; a cap notice whose `dropped` was not
    a number passed the message check and crashed the fold. Now a cursor
    or a since time must be a finite number and a cap notice's count a
    count; what is refused follows the existing aside-and-notice path.
    Fault model: state and outbox files with those values."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.dir = pathlib.Path(self.tmpdir.name)
        self.state = self.dir / 'state.json'

    def asides(self, prefix='state.json.corrupt-'):
        return sorted(p for p in self.dir.iterdir() if p.name.startswith(prefix))

    def test_an_infinite_cursor_or_since_time_is_set_aside_and_the_run_goes_on(self):
        for raw in (b'{"cursors":{"journal":1e309}}', b'{"since":{"mem":1e309},"delivered":["mem"],"fail_runs":{"mem":2}}'):
            with self.subTest(raw=raw):
                for p in list(self.dir.iterdir()):
                    p.unlink()
                self.state.write_bytes(raw)
                sent = []
                with watcher_run(self.dir, SLOW, lambda _, t: sent.append(t) or (True, 'ok')):
                    rc = watch.real_run(False)
                self.assertEqual(rc, 0)
                self.assertEqual([p.read_bytes() for p in self.asides()], [raw])
                self.assertEqual(len(sent), 1, sent)
                self.assertIn('state.json could not be read', sent[0])
                st = json.loads(self.state.read_text())
                self.assertTrue(watch.valid_state(st))
                self.assertEqual(st['cursors']['journal'], NOW)

    def test_a_finite_float_cursor_is_still_a_valid_state(self):
        self.state.write_bytes(b'{"cursors":{"journal":1788599700.5}}')
        with watcher_run(self.dir, ALL_OK, lambda *_: (True, 'ok')):
            self.assertEqual(watch.real_run(False), 0)
        self.assertEqual(self.asides(), [])
        self.assertEqual(json.loads(self.state.read_text())['cursors']['journal'], NOW)

    def test_a_cap_notice_needs_a_count(self):
        notice = {'text': 'drop notice', 'queued': 'old', 'cap': True}
        self.assertTrue(watch.valid_messages([dict(notice, dropped=2)]))
        for bad in ([], '2', True, None, 2.0):
            with self.subTest(dropped=bad):
                self.assertFalse(watch.valid_messages([dict(notice, dropped=bad)]), 'enqueue adds to dropped')
        self.assertFalse(watch.valid_messages([notice]))
        self.assertTrue(watch.valid_messages([{'text': 'alert', 'queued': 'later'}]), 'an ordinary message needs no count')

    def test_the_valid_cap_control_folds(self):
        messages = [{'text': 'drop notice', 'queued': 'old', 'cap': True, 'dropped': 2}]
        result, dropped = watch.enqueue(messages + [{'text': 'alert', 'queued': 'later'}] * 201, [], review_cfg(), 'now')
        self.assertEqual(len(result), watch.OUTBOX_MAX)
        self.assertGreater(result[0]['dropped'], 2)
        self.assertEqual(dropped, 2)

    def test_an_outbox_holding_such_a_notice_is_set_aside_with_a_notice_and_the_run_goes_on(self):
        (self.dir / 'outbox.json').write_bytes(b'[{"text": "drop notice", "queued": "old", "cap": true, "dropped": []}]')
        sent = []
        with watcher_run(self.dir, BURST, lambda _, t: sent.append(t) or (True, 'ok')):
            rc = watch.real_run(False)
        self.assertEqual(rc, 0, sent)
        self.assertEqual(len(self.asides('outbox.json.corrupt-')), 1)
        self.assertTrue(any('outbox.json could not be read' in t for t in sent), sent)
        self.assertTrue(any('DEGRADED' in t for t in sent), 'the run went on to its own alert')


if __name__ == "__main__":
    unittest.main()
