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

"""No peer address and no request line ever reach stderr or the log
(2026-09-15 review, P2 "privacy promise fails on error paths").

BaseHTTPRequestHandler writes "IP - - [date] request line status" to
stderr for every response, and send_error quotes the malformed request it
refuses; docker and journald keep stderr. The handler now logs a fixed
route token and the status code, nothing else, and the server's
handle_error names the exception class alone, never the peer. Checked on
the handler's methods, over a real socket with a malformed request, and
on the server's error path.

Fails on the pre-change code: the address and the path are on stderr.
"""

import contextlib
import io
import socket
import threading
import unittest
from unittest import mock

from otsserver.rpc import RPCRequestHandler, StampServer

PEER = '192.0.2.55'
SECRET = 'patient-Alice-result-positive'


def handler():
    h = RPCRequestHandler.__new__(RPCRequestHandler)
    h.client_address = (PEER, 1234)
    h.requestline = 'GET /%s HTTP/1.0' % SECRET
    h.path = '/' + SECRET
    h.command = 'GET'
    return h


class Test_request_logging(unittest.TestCase):
    def test_log_request_carries_the_route_and_the_status_only(self):
        h = handler()
        with contextlib.redirect_stderr(io.StringIO()) as stderr, self.assertLogs(level='DEBUG') as logs:
            h.log_request(404)
        text = stderr.getvalue() + '\n'.join(logs.output)
        self.assertNotIn(PEER, text)
        self.assertNotIn(SECRET, text)
        self.assertEqual(stderr.getvalue(), '')
        self.assertEqual(logs.output, ['INFO:root:request route=other status=404'])

    def test_log_error_never_carries_the_message(self):
        h = handler()
        with contextlib.redirect_stderr(io.StringIO()) as stderr, self.assertLogs(level='DEBUG') as logs:
            h.log_error("code %d, message %s", 400, "Bad request version ('%s')" % SECRET)
            h.log_error("Request timed out: %r", TimeoutError(SECRET))
        text = stderr.getvalue() + '\n'.join(logs.output)
        self.assertNotIn(SECRET, text)
        self.assertNotIn(PEER, text)
        self.assertEqual(logs.output, ['INFO:root:request refused route=other status=400',
                                       'INFO:root:request refused route=other status=-'])

    def test_routes_are_fixed_tokens(self):
        h = handler()
        for path, route in (('/', 'status'), ('/digest', 'digest'), ('/operator/digest', 'operator-digest'),
                            ('/tip', 'tip'), ('/timestamp/00ff', 'timestamp'), ('/timestamp/' + SECRET, 'timestamp'),
                            ('/' + SECRET, 'other'), ('/digest?' + SECRET, 'digest'), ('', 'other')):
            h.path = path
            self.assertEqual(h.route_name(), route, path)
        del h.path
        self.assertEqual(h.route_name(), 'other')

    def test_success_is_debug_and_refusal_is_info(self):
        h = handler()
        h.path = '/digest'
        with self.assertLogs(level='DEBUG') as logs:
            h.log_request(200)
            h.log_request(503)
        self.assertEqual(logs.output, ['DEBUG:root:request route=digest status=200',
                                       'INFO:root:request route=digest status=503'])


class Test_over_a_socket(unittest.TestCase):
    def setUp(self):
        self.server = StampServer(('127.0.0.1', 0), mock.Mock(), mock.Mock())
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def send(self, raw):
        with socket.create_connection(('127.0.0.1', self.port), timeout=5) as sock:
            sock.sendall(raw)
            sock.shutdown(socket.SHUT_WR)
            out = b''
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    return out
                out += chunk

    def test_a_malformed_request_leaves_no_trace_of_itself(self):
        with contextlib.redirect_stderr(io.StringIO()) as stderr, self.assertLogs(level='DEBUG') as logs:
            bad_version = self.send(('GET /%s HTTP/x.y\r\n\r\n' % SECRET).encode())
            unknown = self.send(('GET /%s HTTP/1.0\r\n\r\n' % SECRET).encode())
            garbage = self.send(b'\x00\x01 ' + SECRET.encode() + b'\r\n\r\n')
        # The standard library answers a malformed request line HTTP/0.9
        # style (no status line, the error page alone); the page quotes the
        # request back to the client who sent it, which is not a log.
        self.assertIn(b'Bad request version', bad_version)
        self.assertTrue(unknown.startswith(b'HTTP/1.0 404'), unknown[:60])
        self.assertIn(b'Bad HTTP/0.9 request type', garbage)
        text = stderr.getvalue() + '\n'.join(logs.output)
        self.assertNotIn(SECRET, text)
        self.assertNotIn('127.0.0.1', text)
        self.assertEqual(stderr.getvalue(), '')
        self.assertIn('INFO:root:request refused route=other status=400', logs.output)
        self.assertIn('INFO:root:request route=other status=404', logs.output)


class Test_handle_error(unittest.TestCase):
    def test_the_peer_is_never_named(self):
        server = StampServer.__new__(StampServer)
        with contextlib.redirect_stderr(io.StringIO()) as stderr, self.assertLogs(level='ERROR') as logs:
            try:
                raise RuntimeError(SECRET)
            except RuntimeError:
                server.handle_error(None, (PEER, 4321))
        self.assertEqual(stderr.getvalue(), '')
        self.assertEqual(logs.output, ['ERROR:root:request handler failed: RuntimeError'])


if __name__ == "__main__":
    unittest.main()
