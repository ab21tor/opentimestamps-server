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

"""Regression test for the /digest handler's Content-Length lower bound.

post_digest parses Content-Length as int (400 on TypeError) and rejects
values above MAX_DIGEST_LENGTH (400), but a negative value passed both
checks — it is a valid int and it is not greater than 64 — and reached
self.rfile.read(negative), which reads to end of stream and submits
arbitrary attacker bytes as one digest. This test pins the fix: a POST to
/digest with a negative Content-Length must get a 400 and must never reach
the aggregator.

The test fails on the pre-fix code: the handler read the 4 KiB body,
submitted it, and answered from whatever the aggregator returned.

Test_post_digest_body_length: a handler that checked the declared
Content-Length and not the bytes it read would let a peer that closed
after one byte of a declared 32 have that one byte aggregated and
acknowledged as a digest. A body shorter than declared is a 400 with
nothing submitted, on a socketless handler and over a real TCP
connection half-closed by the peer.
"""

import os
import socket
import threading
import unittest
from io import BytesIO
from unittest import mock

from opentimestamps.core.notary import PendingAttestation
from opentimestamps.core.timestamp import Timestamp

import otsserver.rpc


class RecordingAggregator:
    """Records every submit(); answers a pending timestamp of the digest."""

    def __init__(self):
        self.submitted = []

    def submit(self, digest, counted=True):
        self.submitted.append(digest)
        timestamp = Timestamp(digest)
        timestamp.attestations.add(PendingAttestation('http://127.0.0.1:14788'))
        return timestamp


def drive_post_digest(content_length_header, body=b"", path="/digest"):
    """Run do_POST on a socketless handler; return (head, body, aggregator)."""
    handler_cls = otsserver.rpc.RPCRequestHandler
    handler = handler_cls.__new__(handler_cls)
    aggregator = RecordingAggregator()
    handler.aggregator = aggregator
    handler.rfile = BytesIO(body)
    handler.wfile = BytesIO()
    handler.headers = {"Content-Length": content_length_header}
    handler.path = path
    handler.command = "POST"
    handler.request_version = "HTTP/1.0"
    handler.requestline = "POST /digest HTTP/1.0"
    handler.client_address = ("127.0.0.1", 0)

    handler.do_POST()

    response = handler.wfile.getvalue()
    head, _, body_out = response.partition(b"\r\n\r\n")
    return head, body_out, aggregator


class Test_post_digest_content_length_lower_bound(unittest.TestCase):
    def test_negative_content_length_is_rejected_before_aggregation(self):
        """Content-Length: -1 passes int() and the > MAX_DIGEST_LENGTH check;
        it must be rejected with a 400 before rfile is read or the aggregator
        sees a single byte."""
        head, body, aggregator = drive_post_digest("-1", body=b"A" * 4096)

        self.assertTrue(head.startswith(b"HTTP/1.0 400"), head[:40])
        self.assertIn(b"invalid digest length", body)
        self.assertEqual(aggregator.submitted, [])


class Test_post_digest_content_length_parse(unittest.TestCase):
    def test_non_numeric_content_length_is_clean_400(self):
        """Content-Length: abc raises ValueError from int(); the handler
        caught only TypeError, so pre-fix this escaped do_POST as a handler
        thread traceback instead of a clean 400."""
        head, body, aggregator = drive_post_digest("abc", body=b"A" * 64)

        self.assertTrue(head.startswith(b"HTTP/1.0 400"), head[:40])
        self.assertIn(b"invalid Content-Length", body)
        self.assertEqual(aggregator.submitted, [])


class Test_post_digest_body_length(unittest.TestCase):
    """The bytes read, not the length declared,
    are the digest. Fault model: a body shorter than its Content-Length,
    on the socketless handler and from a real peer that half-closes."""

    def test_a_body_shorter_than_declared_is_refused_before_aggregation(self):
        head, body, aggregator = drive_post_digest("32", body=b"x")
        self.assertTrue(head.startswith(b"HTTP/1.0 400"), head[:40])
        self.assertIn(b"shorter than Content-Length", body)
        self.assertEqual(aggregator.submitted, [])

    def test_the_operator_lane_refuses_it_the_same_way(self):
        with mock.patch.dict(os.environ, {"OTSD_OPERATOR_LANE": "1"}):
            head, body, aggregator = drive_post_digest("32", body=b"x" * 31, path="/operator/digest")
        self.assertTrue(head.startswith(b"HTTP/1.0 400"), head[:40])
        self.assertEqual(aggregator.submitted, [])

    def test_the_whole_declared_body_is_the_digest(self):
        head, body, aggregator = drive_post_digest("32", body=b"x" * 32)
        self.assertTrue(head.startswith(b"HTTP/1.0 200"), head[:40])
        self.assertEqual(aggregator.submitted, [b"x" * 32])

    def over_tcp(self, body, declared):
        """POST /digest over a real connection, declaring `declared` bytes,
        sending `body`, then half-closing; returns (status line, aggregator)."""
        aggregator = RecordingAggregator()
        server = otsserver.rpc.StampServer(("127.0.0.1", 0), aggregator, None)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with socket.create_connection(server.server_address, timeout=5) as sock:
                sock.sendall(("POST /digest HTTP/1.0\r\nContent-Length: %d\r\n\r\n" % declared).encode() + body)
                sock.shutdown(socket.SHUT_WR)
                out = b""
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    out += chunk
            return out.split(b"\r\n")[0], aggregator
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_a_peer_that_closes_early_gets_a_400_and_nothing_is_aggregated(self):
        status, aggregator = self.over_tcp(b"x", 32)
        self.assertIn(b"400", status)
        self.assertEqual(aggregator.submitted, [])

    def test_a_peer_that_sends_the_whole_body_is_acknowledged(self):
        status, aggregator = self.over_tcp(b"x" * 32, 32)
        self.assertIn(b"200", status)
        self.assertEqual(aggregator.submitted, [b"x" * 32])


if __name__ == "__main__":
    unittest.main()
