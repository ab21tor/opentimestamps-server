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

"""The aggregator must not die silently.

One exception in Aggregator.__loop (a journal fsync failing on a full
disk, for one) would otherwise kill the aggregator thread while the HTTP
server kept accepting digests: every later submit() waiting on a
done_event nobody would ever set, each RPC thread pinned forever, and the
status still showing a best block, so the gateway's /health would say the
calendar was fine. A failed round is logged and exit_event is set so the
process leaves (the container restarts it), submit() waits at most
SUBMIT_TIMEOUT seconds and raises AggregatorUnavailable, and /digest
answers 503 instead of hanging.

An exit event that nothing consumed while the listener kept serving would
stop the workers and leave the serving process alive. The launcher runs
otsserver.rpc.serve_until_exit, which shuts the HTTP server down, joins
the workers and returns nonzero the moment a worker sets the event; otsd
exits 1 and the supervisor restarts the service. The process boundary
itself is exercised in test_otsd_launcher.
"""

import hashlib
import socket
import threading
import time
import unittest
from io import BytesIO
from unittest import mock

import otsserver.rpc
from otsserver.calendar import Aggregator, AggregatorUnavailable
from otsserver.rpc import StampServer


class RaisingCalendar:
    calls = 0

    def submit(self, commitment, records=None):
        RaisingCalendar.calls += 1
        raise OSError(28, 'No space left on device')


class Test_aggregator_failure(unittest.TestCase):
    def test_failed_round_logs_and_sets_exit_event(self):
        exit_event = threading.Event()
        aggregator = Aggregator(RaisingCalendar(), exit_event, commitment_interval=0.1)
        try:
            with self.assertLogs(level='ERROR') as captured:
                with self.assertRaises(AggregatorUnavailable):
                    aggregator.submit(hashlib.sha256(b'first').digest())
            self.assertTrue(exit_event.is_set(), 'a dead aggregator must stop the process')
            self.assertTrue(any('aggregator' in line.lower() and 'No space left' in line
                                for line in captured.output), captured.output)
        finally:
            exit_event.set()
            aggregator.thread.join(5)
        self.assertFalse(aggregator.thread.is_alive())

    def test_submit_is_bounded_when_the_loop_is_gone(self):
        exit_event = threading.Event()
        aggregator = Aggregator(RaisingCalendar(), exit_event, commitment_interval=0.1)
        try:
            with self.assertRaises(AggregatorUnavailable):
                aggregator.submit(hashlib.sha256(b'first').digest())
            aggregator.thread.join(5)
            # The loop is gone; a second caller must not wait forever.
            aggregator.submit_timeout = 0.5
            t0 = time.time()
            with self.assertRaises(AggregatorUnavailable):
                aggregator.submit(hashlib.sha256(b'second').digest())
            self.assertLess(time.time() - t0, 5)
        finally:
            exit_event.set()

    def test_rpc_answers_503_not_a_hang(self):
        class DeadAggregator:
            def submit(self, digest, counted=True):
                raise AggregatorUnavailable('aggregator loop has stopped')
        handler_cls = otsserver.rpc.RPCRequestHandler
        handler = handler_cls.__new__(handler_cls)
        handler.aggregator = DeadAggregator()
        body = hashlib.sha256(b'x').digest()
        handler.rfile = BytesIO(body)
        handler.wfile = BytesIO()
        handler.headers = {"Content-Length": str(len(body))}
        handler.path = "/digest"
        handler.command = "POST"
        handler.request_version = "HTTP/1.0"
        handler.requestline = "POST /digest HTTP/1.0"
        handler.client_address = ("127.0.0.1", 0)
        handler.do_POST()
        head, _, out = handler.wfile.getvalue().partition(b"\r\n\r\n")
        self.assertTrue(head.startswith(b"HTTP/1.0 503"), head[:40])
        self.assertIn(b"aggregator unavailable", out)


class Test_launcher(unittest.TestCase):
    def test_a_failed_aggregator_shuts_the_server_down_and_the_launcher_returns_nonzero(self):
        exit_event = threading.Event()
        aggregator = Aggregator(RaisingCalendar(), exit_event, commitment_interval=0.01)
        server = StampServer(('127.0.0.1', 0), aggregator, RaisingCalendar())
        port = server.server_address[1]
        result = {}
        launcher = threading.Thread(
            target=lambda: result.setdefault('rc', otsserver.rpc.serve_until_exit(server, exit_event, [aggregator.thread])))
        launcher.start()
        try:
            socket.create_connection(('127.0.0.1', port), timeout=2).close()   # serving
            with self.assertRaises(AggregatorUnavailable):
                aggregator.submit(hashlib.sha256(b'x').digest())
            launcher.join(10)
            self.assertFalse(launcher.is_alive(), 'the launcher returns once a worker has failed')
            self.assertEqual(result.get('rc'), 1)
            self.assertFalse(aggregator.thread.is_alive())
            with self.assertRaises(OSError):
                socket.create_connection(('127.0.0.1', port), timeout=1).close()
        finally:
            exit_event.set()
            if launcher.is_alive():
                server.shutdown()
                launcher.join(5)

    def test_the_launcher_returns_when_the_caller_sets_the_event(self):
        exit_event = threading.Event()
        aggregator = Aggregator(RaisingCalendar(), exit_event, commitment_interval=0.05)
        server = StampServer(('127.0.0.1', 0), aggregator, RaisingCalendar())
        result = {}
        launcher = threading.Thread(
            target=lambda: result.setdefault('rc', otsserver.rpc.serve_until_exit(server, exit_event, [aggregator.thread])))
        launcher.start()
        exit_event.set()
        launcher.join(10)
        self.assertFalse(launcher.is_alive())
        self.assertFalse(aggregator.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
