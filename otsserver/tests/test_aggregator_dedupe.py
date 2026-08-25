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

"""Aggregator mempool idempotency

A digest re-submitted within the dedupe horizon must land in the pending
state (a merkle leaf, hence a billable record) exactly once; the duplicate
caller is attached to the first submission's commitment and gets the same
receipt path. The horizon map is in-memory by design, so a resubmission
that straddles a restart counts once on each side — the one bounded
exception, pinned by the second test.
"""

import hashlib
import threading
import unittest
from unittest import mock

from otsserver.calendar import Aggregator


def records_total(cal):
    """Total records the aggregator reported across all its commitments"""
    return sum(c.kwargs['records'] for c in cal.submit.call_args_list)


class Test_aggregator_dedupe(unittest.TestCase):
    def test_resubmit_within_horizon_lands_pending_once(self):
        cal = mock.Mock()
        exit_event = threading.Event()
        aggregator = Aggregator(cal, exit_event, commitment_interval=0.1)
        try:
            digest = hashlib.sha256(b'same digest twice').digest()
            ts_first = aggregator.submit(digest)
            ts_second = aggregator.submit(digest)
        finally:
            exit_event.set()
            aggregator.thread.join(5)

        self.assertEqual(records_total(cal), 1)
        # Same commitment, same receipt path — not a second pending leaf.
        self.assertIs(ts_second, ts_first)

    def test_restart_boundary_counts_twice_the_expected_residual(self):
        cal = mock.Mock()
        digest = hashlib.sha256(b'same digest across restart').digest()

        exit_a = threading.Event()
        agg_a = Aggregator(cal, exit_a, commitment_interval=0.1)
        try:
            agg_a.submit(digest)
        finally:
            exit_a.set()
            agg_a.thread.join(5)

        exit_b = threading.Event()
        agg_b = Aggregator(cal, exit_b, commitment_interval=0.1)
        try:
            agg_b.submit(digest)
        finally:
            exit_b.set()
            agg_b.thread.join(5)

        self.assertEqual(records_total(cal), 2)
