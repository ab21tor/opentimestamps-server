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

"""A funded wallet must never be unable to anchor (C3).

find_unspent sorts the confirmed outputs ascending, so that __do_bitcoin's
unspent[-1] is the biggest in both of its branches: a remnant too small to
pay any fee is never chosen while a larger output can pay it. An attempt
the node refused with the input's whole value as the fee (no change output
left, so no feerate can raise it) is not sent again while that output, that
fee and the chain tip are what they were; one ERROR says so, and one INFO
when an anchor is accepted again.

Real coin selection, fee calculation and transaction construction. The
wallet, the signing and the node's policy are doubled: sendrawtransaction
answers -26 to a fee under SAT_PER_BYTE, as bitcoind does. No live Bitcoin
transaction is made.
"""

import unittest
from decimal import Decimal
from unittest import mock

import bitcoin.rpc
from bitcoin.core import CTxOut, b2lx
from bitcoin.core.script import CScript

from otsserver.tests.test_anchor_records import make_stamper, do_bitcoin

SAT_PER_BYTE = 10


class Wallet:
    """listunspent, gettxout, the signing, and a node whose policy refuses
    a fee under SAT_PER_BYTE with -26"""

    def __init__(self, values):
        self.coins = {bytes([i + 1]) * 32: v for i, v in enumerate(values)}
        self.attempts = []     # (input sats, fee sats) per sendrawtransaction
        self.accepted = []

    def _call(self, method, *args):
        if method == 'listunspent':
            return [dict(txid=b2lx(k), vout=0, scriptPubKey='51',
                         amount=Decimal(v) / Decimal(100000000), spendable=True)
                    for k, v in self.coins.items()]
        if method == 'getnewaddress':
            return 'change-address'
        if method == 'getaddressinfo':
            return {'scriptPubKey': '51'}
        if method == 'estimatesmartfee':
            return {'feerate': Decimal('0.0001')}   # 10 sat/vB
        raise AssertionError(method)

    def getblockcount(self):
        return 100

    def gettxout(self, outpoint, *args, **kwargs):
        return {'txout': CTxOut(self.coins[outpoint.hash], CScript(b'\x51'))}

    def signrawtransactionwithwallet(self, tx):
        return {'complete': True, 'tx': tx}

    def sendrawtransaction(self, tx):
        value = self.coins[tx.vin[0].prevout.hash]
        fee = value - sum(o.nValue for o in tx.vout)
        self.attempts.append((value, fee))
        if fee < len(tx.serialize()) * SAT_PER_BYTE:
            raise bitcoin.rpc.JSONRPCError({'code': -26, 'message': 'min relay fee not met'})
        self.accepted.append(tx)
        return tx.GetTxid()


def stamper_over(tip=b'a' * 32):
    s = make_stamper(None)
    s.max_fee = 20000
    s.pending_commitments.add(b'X' * 44)
    s.known_blocks.update_from_proxy.return_value = []
    s.known_blocks.best_block_height.return_value = 100
    s.known_blocks.best_block_hash.return_value = tip
    return s


def one_pass(s, wallet):
    with mock.patch('otsserver.stamper.make_proxy', return_value=wallet):
        do_bitcoin(s)


class Test_coin_selection(unittest.TestCase):
    def test_the_biggest_confirmed_output_is_spent_beside_a_remnant(self):
        wallet = Wallet([400, 100000])
        s = stamper_over()
        one_pass(s, wallet)
        self.assertEqual([value for value, _ in wallet.attempts], [100000], 'one attempt, from the refill')
        self.assertEqual(len(wallet.accepted), 1)
        self.assertEqual(len(s.unconfirmed_txs), 1)

    def test_control_one_large_output(self):
        wallet = Wallet([100000])
        s = stamper_over()
        one_pass(s, wallet)
        self.assertEqual(len(wallet.accepted), 1)
        self.assertEqual(len(s.unconfirmed_txs), 1)


class Test_refused_attempt_not_repeated(unittest.TestCase):
    def test_a_lone_remnant_is_refused_once_and_not_sent_again(self):
        wallet = Wallet([400])
        s = stamper_over()
        with self.assertLogs(level='ERROR') as captured:
            one_pass(s, wallet)
        self.assertEqual(wallet.attempts, [(400, 400)], 'one attempt, the whole remnant as the fee')
        self.assertEqual(wallet.accepted, [])
        self.assertEqual(s.unconfirmed_txs, [])
        self.assertTrue(any('cannot rise' in r.getMessage() and 'min relay fee not met' in r.getMessage()
                            for r in captured.records), captured.output)
        # The same output, the same tip: no second attempt, no second word.
        with self.assertNoLogs(level='ERROR'):
            one_pass(s, wallet)
            one_pass(s, wallet)
        self.assertEqual(len(wallet.attempts), 1)
        # A refill: the biggest output pays, and the resume is said once.
        wallet.coins[b'\x09' * 32] = 100000
        with self.assertLogs(level='INFO') as captured:
            one_pass(s, wallet)
        self.assertEqual(wallet.attempts[-1][0], 100000)
        self.assertEqual(len(wallet.accepted), 1)
        self.assertTrue(any('anchoring resumes' in r.getMessage() for r in captured.records), captured.output)
        self.assertIsNone(s.refused_anchor)

    def test_a_new_block_earns_one_more_attempt_and_no_second_alarm(self):
        wallet = Wallet([400])
        s = stamper_over()
        with self.assertLogs(level='ERROR'):
            one_pass(s, wallet)
        s.known_blocks.best_block_hash.return_value = b'b' * 32
        with self.assertNoLogs(level='ERROR'):
            one_pass(s, wallet)
        self.assertEqual(len(wallet.attempts), 2, 'one more attempt against the new tip, refused again')
        with self.assertNoLogs(level='ERROR'):
            one_pass(s, wallet)
        self.assertEqual(len(wallet.attempts), 2)
