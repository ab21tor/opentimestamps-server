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

"""Regression tests for the otsd launcher's ~/.otsd handling.

The launcher unconditionally created ~/.otsd even when --calendar pointed
elsewhere — a stray dotdir in whatever $HOME the daemon ran under. The mkdir must happen only when the
default calendar path is actually in use.

Driven via subprocess: otsd is a script, and both runs exit early at
calendar-identity loading (the calendar's uri missing) — which is AFTER the
mkdir — so the dotdir's presence is fully decided by exit time either way.

Test_process_boundary (2026-09-15 review): the real otsd process, with a
real calendar directory and no bitcoind, must exit 1 with its listener
closed when the aggregator fails, and refuse to start (exit 1, the
recovery text on its log) when journal.known-good is malformed or older
than db/ says. Worker failures are demonstrated here at the process
boundary, not only on the objects.
"""

import http.client
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from opentimestamps.core.timestamp import Timestamp

from otsserver.calendar import Calendar, write_checkpoint

OTSD = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'otsd')


class Test_otsd_default_dir(unittest.TestCase):
    def _run(self, home, args):
        env = dict(os.environ, HOME=home)
        return subprocess.run([sys.executable, OTSD] + args,
                              env=env, capture_output=True, timeout=60)

    def test_custom_calendar_leaves_home_untouched(self):
        with tempfile.TemporaryDirectory() as home, \
             tempfile.TemporaryDirectory() as cal:
            result = self._run(home, ['--calendar', cal])
            self.assertNotEqual(result.returncode, 0)  # exits at missing identity
            self.assertFalse(
                os.path.exists(os.path.join(home, '.otsd')),
                "custom --calendar must not create ~/.otsd")

    def test_default_calendar_creates_default_dir(self):
        """Control: with the default path the dotdir IS created, proving the
        conditional (and this test's observation point) actually runs."""
        with tempfile.TemporaryDirectory() as home:
            result = self._run(home, [])
            self.assertNotEqual(result.returncode, 0)  # exits at missing identity
            self.assertTrue(
                os.path.isdir(os.path.join(home, '.otsd')),
                "default calendar path must create ~/.otsd")


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class Test_process_boundary(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.home = os.path.join(self.tmpdir.name, 'home')   # no ~/.bitcoin: every RPC fails fast
        os.makedirs(self.home)
        self.cal = os.path.join(self.tmpdir.name, 'calendar')
        os.makedirs(self.cal)
        with open(os.path.join(self.cal, 'uri'), 'w') as fd:
            fd.write('http://127.0.0.1:14788\n')
        with open(os.path.join(self.cal, 'hmac-key'), 'wb') as fd:
            fd.write(b'\x01' * 32)
        self.port = free_port()
        self.proc = None

    def tearDown(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.communicate(timeout=10)

    def start(self):
        self.proc = subprocess.Popen(
            [sys.executable, OTSD, '--calendar', self.cal, '--rpc-address', '127.0.0.1', '--rpc-port', str(self.port)],
            env=dict(os.environ, HOME=self.home), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return self.proc

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=40)
        try:
            conn.request(method, path, body=body,
                         headers={'Content-Length': str(len(body))} if body is not None else {})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def wait_serving(self):
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.proc.poll() is not None:
                self.fail('otsd exited early: ' + self.proc.communicate()[0])
            try:
                status, _ = self.request('GET', '/')
                if status == 200:
                    return
            except OSError:
                time.sleep(0.2)
        self.fail('otsd never served')

    def test_an_aggregator_failure_closes_the_listener_and_exits_one(self):
        self.start()
        self.wait_serving()
        # The journal grows by one byte under the running server (a
        # tampered or torn journal): the round after the next lands its
        # entry off the record boundary and the aggregator refuses to
        # continue. No test hook in the server.
        with open(os.path.join(self.cal, 'journal'), 'ab') as fd:
            fd.write(b'x')
        status, _ = self.request('POST', '/digest', b'\x11' * 32)
        self.assertEqual(status, 200)
        time.sleep(1.5)
        try:
            status, _ = self.request('POST', '/digest', b'\x22' * 32)
            self.assertEqual(status, 503)
        except OSError:
            pass   # the listener may already be gone
        out, _ = self.proc.communicate(timeout=40)
        self.assertEqual(self.proc.returncode, 1, out)
        self.assertIn('Aggregator round failed', out)
        self.assertIn('stopped the service; exiting 1', out)
        with self.assertRaises(OSError):
            socket.create_connection(('127.0.0.1', self.port), timeout=1).close()

    def test_a_malformed_checkpoint_refuses_to_start(self):
        with open(os.path.join(self.cal, 'journal.known-good'), 'w') as fd:
            fd.write('torn-or-corrupt\n')
        self.start()
        out, _ = self.proc.communicate(timeout=60)
        self.assertEqual(self.proc.returncode, 1, out)
        self.assertIn('CALENDAR STORAGE INCONSISTENT', out)
        self.assertIn('malformed', out)
        self.assertIn('Recovery', out)
        with self.assertRaises(OSError):
            socket.create_connection(('127.0.0.1', self.port), timeout=1).close()

    def test_an_older_database_beside_a_newer_checkpoint_refuses_to_start(self):
        known_good = os.path.join(self.cal, 'journal.known-good')
        with open(os.path.join(self.cal, 'journal'), 'wb') as fd:
            fd.write(b'\x10' * 36 + b'\x00' * 8 + b'\x11' * 36 + b'\x00' * 8)
        cal = Calendar(self.cal)
        cal.add_commitment_timestamps([Timestamp(b'\x10' * 36)], watermark=1)
        write_checkpoint(known_good, 1, cal.generation)
        cal.db.db.close()
        backup = os.path.join(self.tmpdir.name, 'db-backup')
        shutil.copytree(os.path.join(self.cal, 'db'), backup)
        cal = Calendar(self.cal)
        cal.add_commitment_timestamps([Timestamp(b'\x11' * 36)], watermark=2)
        write_checkpoint(known_good, 2, cal.generation)
        cal.db.db.close()
        shutil.rmtree(os.path.join(self.cal, 'db'))
        shutil.copytree(backup, os.path.join(self.cal, 'db'))
        self.start()
        out, _ = self.proc.communicate(timeout=60)
        self.assertEqual(self.proc.returncode, 1, out)
        self.assertIn('older than the checkpoint', out)
        self.assertIn('Recovery', out)
        # The documented recovery, then the service starts and serves.
        os.unlink(known_good)
        self.start()
        self.wait_serving()
        self.proc.kill()
        out, _ = self.proc.communicate(timeout=10)
        self.assertNotIn('CALENDAR STORAGE INCONSISTENT', out)


if __name__ == "__main__":
    unittest.main()
