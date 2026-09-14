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

"""Deep-reorg detector (green review 2026-09-11, item 12).

A receipted anchor had min_confirmations when its receipt was written, and
the calendar saved proofs naming that block. A reorg deeper than that
takes the block away; before this the saved proofs simply went stale and
nothing noticed. Now, hourly, the stamper asks the wallet (gettransaction)
about the last hundred receipted txids. A confirmation count at or below
zero means the anchor left the chain: Bitcoin Core reports a conflicted
transaction as a negative count and one back in the mempool as zero. A
blockheight other than the receipted one means it was mined again
elsewhere. Either finding lands in needs_attention -- read by the status
line (test_rpc_status) and the watcher (test_watch) -- is logged at ERROR
on every check while it stands, and is never acted on: nothing is
re-anchored automatically.

FakeChain is the reorg contract of the billing red-team's fake bitcoind
(audit/billing-redteam-20260904/harness/fakenode.py, 2026-09-04):
gettransaction reports confirmations as tip - height + 1 for a mined
wallet transaction and 0 once x_invalidate has dropped its block and put
it back in the mempool; a txid the wallet never saw is RPC error -5. The
Core-shaped answers (negative counts, blockheight) are the same double
with core=True.

Fails on the pre-change code: Stamper has no check_anchors and no
needs_attention.
"""

import json
import os
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import bitcoin.rpc

from otsserver.stamper import OrderedSet, Stamper


def txid_of(n):
    return ('%02x' % n) * 32


class FakeChain:
    """The fake bitcoind's reorg contract, in process (see module docstring)"""

    def __init__(self, core=False):
        self.core = core
        self.tip = 0
        self.heights = {}     # txid -> height while in a block
        self.wallet = set()   # txids the wallet has seen
        self.conflicted = set()
        self.calls = []

    def mine(self, *txids):
        self.tip += 1
        for txid in txids:
            self.wallet.add(txid)
            self.heights[txid] = self.tip

    def invalidate(self, height):
        """fakenode.x_invalidate: blocks from height up are dropped, their
        transactions go back to the mempool, replacement blocks restore
        the tip."""
        removed = self.tip - height + 1
        for txid, h in list(self.heights.items()):
            if h >= height:
                del self.heights[txid]
        self.tip = height - 1
        for _ in range(removed):
            self.mine()

    def _call(self, method, *args):
        self.calls.append((method,) + args)
        assert method == 'gettransaction', method
        txid = args[0]
        if txid not in self.wallet:
            raise bitcoin.rpc.JSONRPCError({'code': -5, 'message': 'Invalid or non-wallet transaction id'})
        height = self.heights.get(txid)
        if height is None:
            confirmations = -1 if (self.core and txid in self.conflicted) else 0
        else:
            confirmations = self.tip - height + 1
        r = {'txid': txid, 'confirmations': confirmations}
        if self.core and height is not None:
            r['blockheight'] = height
        return r


def write_receipts(path, receipts):
    with open(path, 'w') as fd:
        for receipt in receipts:
            fd.write(json.dumps(receipt) + '\n')


def make_stamper(receipts_path):
    stamper = Stamper.__new__(Stamper)
    stamper.calendar = mock.Mock()
    stamper.anchor_receipts_path = receipts_path
    stamper.min_confirmations = 6
    stamper.unconfirmed_txs = []
    stamper.pending_commitments = OrderedSet()
    stamper.txs_waiting_for_confirmation = {}
    stamper.needs_attention = []
    stamper.anchor_findings = {}
    return stamper


class Test_deep_reorg_detector(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.receipts_path = os.path.join(self.tmpdir.name, 'anchor-receipts.jsonl')

    def anchored_chain(self, n, core=False):
        """n anchors, one per block from height 1, each receipted at its
        height and then buried min_confirmations deep."""
        chain = FakeChain(core=core)
        receipts = []
        for i in range(1, n + 1):
            chain.mine(txid_of(i))
            receipts.append({'txid': txid_of(i), 'fee_sats': 500, 'commitments': 3,
                             'confirmed_height': i, 'confirmed_at': 1788600000 + i, 'records': 3})
        for _ in range(6):
            chain.mine()
        write_receipts(self.receipts_path, receipts)
        return chain

    def test_anchors_where_their_receipts_say_is_quiet(self):
        chain = self.anchored_chain(3)
        stamper = make_stamper(self.receipts_path)
        with self.assertNoLogs(level='WARNING'):
            findings = stamper.check_anchors(chain)
        self.assertEqual(findings, [])
        self.assertEqual(stamper.needs_attention, [])
        self.assertEqual([c[0] for c in chain.calls], ['gettransaction'] * 3)

    def test_a_reorged_out_anchor_is_found_logged_and_left_alone(self):
        chain = self.anchored_chain(3)
        stamper = make_stamper(self.receipts_path)
        stamper.pending_commitments.add(b'\x01' * 44)
        chain.invalidate(3)     # the fake's reorg: anchor 3 back in the mempool
        with self.assertLogs(level='ERROR') as captured:
            findings = stamper.check_anchors(chain)

        self.assertEqual(len(findings), 1, findings)
        self.assertIn(txid_of(3), findings[0])
        self.assertIn('left the chain', findings[0])
        self.assertIn('confirmations 0', findings[0])
        self.assertIn('height 3', findings[0])
        self.assertEqual(stamper.needs_attention, findings)
        errors = [r for r in captured.records if r.levelname == 'ERROR']
        self.assertEqual(len(errors), 1, captured.output)
        self.assertIn(txid_of(3), errors[0].getMessage())
        self.assertIn('NEEDS ATTENTION', errors[0].getMessage())
        # Never re-anchored automatically: nothing was submitted, the
        # pending queue and the in-flight anchor are untouched.
        self.assertEqual(set(c[0] for c in chain.calls), {'gettransaction'})
        self.assertEqual(list(stamper.pending_commitments), [b'\x01' * 44])
        self.assertEqual(stamper.unconfirmed_txs, [])

    def test_the_finding_stands_across_checks_and_is_logged_each_time(self):
        chain = self.anchored_chain(3)
        stamper = make_stamper(self.receipts_path)
        chain.invalidate(2)
        with self.assertLogs(level='ERROR') as first:
            stamper.check_anchors(chain)
        # The anchors are mined again (the fake's mempool re-mined): the
        # proofs on file still name blocks that no longer hold them, so
        # the finding does not clear itself, and the next check says so
        # again.
        chain.mine(txid_of(2), txid_of(3))
        for _ in range(6):
            chain.mine()
        with self.assertLogs(level='ERROR') as again:
            findings = stamper.check_anchors(chain)
        self.assertEqual(sorted(findings), sorted(stamper.needs_attention))
        self.assertEqual(len(findings), 2, findings)
        self.assertEqual(len([r for r in first.records if r.levelname == 'ERROR']), 1)
        self.assertEqual(len([r for r in again.records if r.levelname == 'ERROR']), 1)

    def test_core_shapes_negative_count_and_another_height(self):
        chain = self.anchored_chain(3, core=True)
        stamper = make_stamper(self.receipts_path)
        # Anchor 1: conflicted after a reorg (Core: confirmations -1).
        del chain.heights[txid_of(1)]
        chain.conflicted.add(txid_of(1))
        # Anchor 2: mined again at another height (Core: blockheight).
        chain.mine(txid_of(2))
        with self.assertLogs(level='ERROR'):
            findings = stamper.check_anchors(chain)
        self.assertEqual(len(findings), 2, findings)
        one, two = sorted(findings)
        self.assertIn(txid_of(1), one)
        self.assertIn('confirmations -1', one)
        self.assertIn(txid_of(2), two)
        self.assertIn('mined again', two)
        self.assertIn('height 10', two)
        self.assertIn('receipted at height 2', two)

    def test_only_the_last_hundred_receipts_are_asked_about(self):
        chain = self.anchored_chain(130)
        stamper = make_stamper(self.receipts_path)
        stamper.check_anchors(chain)
        asked = [c[1] for c in chain.calls]
        self.assertEqual(len(asked), 100)
        self.assertEqual(asked[0], txid_of(31))
        self.assertEqual(asked[-1], txid_of(130))

    def test_unknown_txids_and_rpc_failures_are_warnings_not_findings(self):
        chain = self.anchored_chain(2)
        stamper = make_stamper(self.receipts_path)
        chain.wallet.discard(txid_of(1))    # a rebuilt wallet never saw it
        with self.assertLogs(level='WARNING') as captured:
            findings = stamper.check_anchors(chain)
        self.assertEqual(findings, [])
        self.assertTrue(any(txid_of(1) in r.getMessage() and r.levelname == 'WARNING'
                            for r in captured.records), captured.output)

        broken = types.SimpleNamespace(_call=mock.Mock(side_effect=ConnectionError('gone')))
        with self.assertLogs(level='WARNING') as captured:
            findings = stamper.check_anchors(broken)
        self.assertEqual(findings, [])
        self.assertEqual(stamper.needs_attention, [])

    def test_receipts_off_or_absent_checks_nothing(self):
        chain = FakeChain()
        self.assertEqual(make_stamper(None).check_anchors(chain), [])
        self.assertEqual(make_stamper(self.receipts_path).check_anchors(chain), [])
        self.assertEqual(chain.calls, [])

    def test_the_stamp_loop_runs_the_check_hourly_from_the_start(self):
        chain = self.anchored_chain(2)
        cal_path = os.path.join(self.tmpdir.name, 'calendar')
        os.makedirs(cal_path)
        open(os.path.join(cal_path, 'journal'), 'wb').close()
        exit_event = threading.Event()
        started = time.time()
        with mock.patch.dict(os.environ, {'OTSD_ANCHOR_RECEIPTS': self.receipts_path}), \
                mock.patch.object(Stamper, '_Stamper__do_bitcoin'), \
                mock.patch('otsserver.stamper.make_proxy', return_value=chain):
            stamper = Stamper(types.SimpleNamespace(path=cal_path), exit_event,
                              conf_target=12, relay_feerate=1, min_confirmations=6,
                              min_tx_interval=600, max_fee=1000000, max_pending=100)
            try:
                deadline = time.time() + 5
                while len(chain.calls) < 2 and time.time() < deadline:
                    time.sleep(0.05)
                time.sleep(2.2)    # two more loop passes: no second check
            finally:
                exit_event.set()
                stamper.thread.join(5)
        self.assertEqual([c[0] for c in chain.calls], ['gettransaction'] * 2)
        self.assertEqual(stamper.needs_attention, [])
        self.assertGreaterEqual(stamper.next_anchor_check, started + Stamper.ANCHOR_CHECK_INTERVAL)
        self.assertEqual(Stamper.ANCHOR_CHECK_INTERVAL, 3600)


if __name__ == "__main__":
    unittest.main()
