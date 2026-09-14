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

"""GET / is one JSON line: the calendar's status (green review 2026-09-11,
item 17, which dropped the donation homepage and its qrcode, pystache and
simplejson dependencies).

The line is what the watcher (ops/watch.py), the self-stamp's float reader
(ops/selfstamp.py) and the gateway's /health read: best_block is the proof
that the Bitcoin RPC path is alive, anchor_receipts says whether anchors
are receipted, balance is the anchor wallet's confirmed sats, and
needs_attention is the deep-reorg detector's state. It is built in full
before the response is committed, so an RPC failure is a status that says
so (best_block null, logged), never an empty 200. The old page's Best-block
marker rendered only after two RPCs succeeded; the same fact now has a
name. Whatever Accept the caller sends, the answer is the JSON.

Fails on the pre-change code: the handler still renders the page.
"""

import json
import sys
import types
import unittest
from io import BytesIO
from unittest import mock

from bitcoin.core import lx

import otsserver.rpc

BEST_HEIGHT = 900123
BEST_HASH_DISPLAY = "0" * 50 + "%014x" % BEST_HEIGHT


class FakeProxy:
    """Canned subset of bitcoin.rpc.Proxy used by the status handler."""

    def getbalance(self, minconf=1):
        return 1500000  # satoshis, as bitcoinlib's getbalance returns int(r*COIN)

    def getbestblockhash(self):
        return lx(BEST_HASH_DISPLAY)

    def getblockcount(self):
        return BEST_HEIGHT


def drive_status(handler_cls, accept="text/html"):
    """Run do_GET('/') on a socketless handler instance; return (head, body)."""
    handler = handler_cls.__new__(handler_cls)
    handler.rfile = BytesIO()
    handler.wfile = BytesIO()
    handler.headers = {"Accept": accept} if accept else {}
    handler.path = "/"
    handler.command = "GET"
    handler.request_version = "HTTP/1.0"
    handler.requestline = "GET / HTTP/1.0"
    handler.client_address = ("127.0.0.1", 0)

    handler.do_GET()

    response = handler.wfile.getvalue()
    head, _, body = response.partition(b"\r\n\r\n")
    return head, body


def make_handler_cls(anchor_receipts_path=None, needs_attention=()):
    class TestHandler(otsserver.rpc.RPCRequestHandler):
        pass

    TestHandler.calendar = types.SimpleNamespace(
        stamper=types.SimpleNamespace(
            pending_commitments=set([b'a', b'b']),
            txs_waiting_for_confirmation={},
            unconfirmed_txs=[],
            anchor_receipts_path=anchor_receipts_path,
            needs_attention=list(needs_attention),
        )
    )
    return TestHandler


def with_proxy(fn):
    return mock.patch.object(otsserver.rpc, "make_proxy", lambda timeout=None: fn())


class Test_status(unittest.TestCase):
    def test_status_is_one_json_line_with_the_health_fields(self):
        with with_proxy(FakeProxy):
            head, body = drive_status(make_handler_cls())

        self.assertTrue(head.startswith(b"HTTP/1.0 200"), head[:40])
        self.assertIn(b"Content-Type: application/json", head)
        self.assertIn(b"Content-Length: %d" % len(body), head)
        self.assertTrue(body.endswith(b"\n") and body.count(b"\n") == 1, body)
        status = json.loads(body)
        self.assertEqual(status["best_block"], BEST_HASH_DISPLAY)
        self.assertEqual(status["block_height"], BEST_HEIGHT)
        self.assertEqual(status["balance"], 1500000)
        self.assertEqual(status["pending_commitments"], 2)
        self.assertEqual(status["anchor_receipts"], "off")
        self.assertEqual(status["needs_attention"], [])
        self.assertEqual(status["version"], otsserver.__version__)
        for gone in ("address", "address_qr", "transactions", "lightning_invoice", "explorer_url"):
            self.assertNotIn(gone, status)
        self.assertNotIn(b"<html", body)

    def test_rpc_failure_is_a_status_that_says_so_and_is_logged(self):
        def raising():
            raise ValueError("Cookie file unusable (test) and rpcpassword not specified")

        with with_proxy(raising):
            with self.assertLogs(level="ERROR") as captured:
                head, body = drive_status(make_handler_cls("/receipts/r.jsonl"))

        self.assertTrue(head.startswith(b"HTTP/1.0 200"), head[:40])
        status = json.loads(body)
        self.assertIsNone(status["best_block"])
        self.assertIsNone(status["balance"])
        # The rest of the status is still told: the calendar is up, its
        # receipts are on, the chain is what it cannot see.
        self.assertEqual(status["anchor_receipts"], "on")
        self.assertTrue(any("bitcoin RPC" in line for line in captured.output), captured.output)

    def test_status_states_whether_anchors_are_receipted(self):
        with with_proxy(FakeProxy):
            _, off_body = drive_status(make_handler_cls(None))
            _, on_body = drive_status(make_handler_cls("/receipts/anchor-receipts.jsonl"))
        self.assertEqual(json.loads(off_body)["anchor_receipts"], "off")
        self.assertEqual(json.loads(on_body)["anchor_receipts"], "on")

    def test_status_carries_the_reorg_detectors_state(self):
        reason = "anchor 11aa left the chain (confirmations 0, receipted at height 900000)"
        with with_proxy(FakeProxy):
            _, body = drive_status(make_handler_cls("/r.jsonl", needs_attention=[reason]))
        self.assertEqual(json.loads(body)["needs_attention"], [reason])

    def test_accept_header_does_not_matter(self):
        with with_proxy(FakeProxy):
            bodies = [drive_status(make_handler_cls(), accept)[1]
                      for accept in ("text/html", "application/json", None)]
        self.assertEqual(len(set(bodies)), 1, bodies)
        json.loads(bodies[0])

    def test_status_rpc_timeout_is_thirty_seconds(self):
        """The per-op RPC timeout is a hung-transport detector, not a render
        budget: it must clear the Tor bridge's transient circuit stalls
        (which cut ~11% of renders while this sat at 5s, 2026-07-17) and
        stays paired with the gateway probe's 45s read timeout, which must
        outlast a full status. Pin the value so neither moves alone."""
        seen = []

        def capturing_make_proxy(timeout=None):
            seen.append(timeout)
            return FakeProxy()

        with mock.patch.object(otsserver.rpc, "make_proxy", capturing_make_proxy):
            drive_status(make_handler_cls())

        self.assertEqual(seen, [30])

    def test_the_page_dependencies_are_gone(self):
        """Nothing in the server imports what the page needed."""
        import otsserver.calendar, otsserver.stamper  # noqa: F401 (already imported; explicit)
        for name in ("qrcode", "pystache", "simplejson", "PIL"):
            self.assertIsNone(sys.modules.get(name), name)


if __name__ == "__main__":
    unittest.main()
