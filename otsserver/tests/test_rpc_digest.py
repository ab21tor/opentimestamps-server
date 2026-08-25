# Copyright (C) 2026 The OpenTimestamps developers
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
"""

import unittest
from io import BytesIO

import otsserver.rpc


class RecordingAggregator:
    """Records every submit(); the test asserts none happen."""

    def __init__(self):
        self.submitted = []

    def submit(self, digest):
        self.submitted.append(digest)


def drive_post_digest(content_length_header, body=b""):
    """Run do_POST('/digest') on a socketless handler; return (head, body, aggregator)."""
    handler_cls = otsserver.rpc.RPCRequestHandler
    handler = handler_cls.__new__(handler_cls)
    aggregator = RecordingAggregator()
    handler.aggregator = aggregator
    handler.rfile = BytesIO(body)
    handler.wfile = BytesIO()
    handler.headers = {"Content-Length": content_length_header}
    handler.path = "/digest"
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


if __name__ == "__main__":
    unittest.main()
