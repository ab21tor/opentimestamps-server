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

"""Regression tests for the homepage handler's Bitcoin RPC wiring.

The homepage commits its 200 status line and headers before touching Bitcoin
(rpc.py send_response/end_headers), so a proxy constructed without
BITCOIN_RPC_SERVICE_URL support fails silently in cookie-less deployments and
the page renders as an empty 200 with no Best-block marker. These tests pin
the fix: the handler must construct its proxy via otsserver.stamper's
make_proxy() (patched here), and a construction failure must be logged, not
swallowed.

Both tests fail on the pre-fix code: otsserver.rpc had no make_proxy name to
patch (mock.patch.object raises AttributeError), and the pre-fix handler
logged nothing on construction failure.
"""

import types
import unittest
from io import BytesIO
from unittest import mock

from bitcoin.core import lx
from bitcoin.wallet import CBitcoinAddress

import otsserver.rpc

BEST_HEIGHT = 900123
BEST_HASH_DISPLAY = "0" * 50 + "%014x" % BEST_HEIGHT


class FakeProxy:
    """Canned subset of bitcoin.rpc.Proxy used by the homepage handler."""

    def getbalance(self, minconf=1):
        return 1500000  # satoshis, as bitcoinlib's getbalance returns int(r*COIN)

    def _call(self, service_name, *args):
        assert service_name == "listtransactions"
        return []

    def getbestblockhash(self):
        return lx(BEST_HASH_DISPLAY)

    def getblockcount(self):
        return BEST_HEIGHT


def drive_homepage(handler_cls):
    """Run do_GET('/') on a socketless handler instance; return (head, body)."""
    handler = handler_cls.__new__(handler_cls)
    handler.rfile = BytesIO()
    handler.wfile = BytesIO()
    handler.headers = {"Accept": "text/html"}
    handler.path = "/"
    handler.command = "GET"
    handler.request_version = "HTTP/1.0"
    handler.requestline = "GET / HTTP/1.0"
    handler.client_address = ("127.0.0.1", 0)

    handler.do_GET()

    response = handler.wfile.getvalue()
    head, _, body = response.partition(b"\r\n\r\n")
    return head, body


def make_handler_cls(anchor_receipts_path=None):
    class TestHandler(otsserver.rpc.RPCRequestHandler):
        pass

    TestHandler.calendar = types.SimpleNamespace(
        stamper=types.SimpleNamespace(
            pending_commitments=set(),
            txs_waiting_for_confirmation={},
            unconfirmed_txs=[],
            anchor_receipts_path=anchor_receipts_path,
        )
    )
    TestHandler.lightning_invoice_file = None
    TestHandler.donation_addr = CBitcoinAddress("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa")
    TestHandler.explorer_url = "https://mempool.space"
    return TestHandler


class Test_homepage_bitcoin_wiring(unittest.TestCase):
    def test_homepage_renders_best_block_via_make_proxy(self):
        """Happy path: proxy comes from make_proxy(); Best-block renders."""
        with mock.patch.object(otsserver.rpc, "make_proxy",
                               lambda timeout=None: FakeProxy()):
            head, body = drive_homepage(make_handler_cls())

        self.assertTrue(head.startswith(b"HTTP/1.0 200"), head[:40])
        self.assertIn(b"Best-block", body)
        self.assertIn(BEST_HASH_DISPLAY.encode(), body)
        self.assertIn(str(BEST_HEIGHT).encode(), body)
        self.assertIn(b"1,500,000", body)  # canned balance flows through str_sat

    def test_homepage_construction_failure_is_logged_not_silent(self):
        """Construction failure: still a header-only 200 (marker absent, so
        health probes correctly read Bitcoin-blind), but now logged loudly."""
        def raising_make_proxy(timeout=None):
            raise ValueError("Cookie file unusable (test) and rpcpassword not specified")

        with mock.patch.object(otsserver.rpc, "make_proxy", raising_make_proxy):
            with self.assertLogs(level="ERROR") as captured:
                head, body = drive_homepage(make_handler_cls())

        self.assertTrue(head.startswith(b"HTTP/1.0 200"), head[:40])
        self.assertEqual(body, b"")
        self.assertNotIn(b"Best-block", body)
        self.assertTrue(
            any("failed to construct bitcoin RPC proxy" in line
                for line in captured.output),
            captured.output,
        )

    def test_homepage_states_whether_anchors_are_receipted(self):
        """The gateway's /health reads this line: a calendar restarted with
        receipts off must say "off" where it used to say nothing."""
        with mock.patch.object(otsserver.rpc, "make_proxy",
                               lambda timeout=None: FakeProxy()):
            _, off_body = drive_homepage(make_handler_cls(None))
            _, on_body = drive_homepage(make_handler_cls("/receipts/anchor-receipts.jsonl"))
        self.assertIn(b"Anchor receipts: off", off_body)
        self.assertIn(b"Anchor receipts: on", on_body)
        self.assertNotIn(b"Anchor receipts: on", off_body)

    def test_homepage_rpc_timeout_is_thirty_seconds(self):
        """The homepage per-op RPC timeout is a hung-transport detector, not a
        render budget: it must clear the Tor bridge's transient circuit stalls
        (which cut ~11% of renders while this sat at 5s, 2026-07-17) and stays
        paired with the gateway probe's 45s read timeout, which must outlast a
        full render. Pin the value so neither moves alone."""
        seen = []

        def capturing_make_proxy(timeout=None):
            seen.append(timeout)
            return FakeProxy()

        with mock.patch.object(otsserver.rpc, "make_proxy", capturing_make_proxy):
            head, body = drive_homepage(make_handler_cls())

        self.assertEqual(seen, [30])
        self.assertIn(b"Best-block", body)


if __name__ == "__main__":
    unittest.main()
