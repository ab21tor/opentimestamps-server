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

"""ops/watch.py — the box health watcher, loaded by path like selfstamp.

The fixture scenarios under ops/tests/watch/ (26 written for the Pi's hosted
shape on 2026-09-04 and 2026-09-07, three for the appliance shape) drive
evaluate/decide/heartbeat with the sender stubbed, exactly as
`watch.py --test` does. The unit tests pin what changed when the script moved
into this fork (2026-09-11): an empty knob skips its check and its count,
the calendar check's verdicts, the sats parser, and the box name in every
message. Nothing here touches the network, docker, systemd or the journal.
"""

import importlib.util
import pathlib
import unittest

TOOL = pathlib.Path(__file__).resolve().parents[2] / 'ops' / 'watch.py'


def load_tool():
    spec = importlib.util.spec_from_file_location('watch', TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


watch = load_tool()

APPLIANCE = {"HEALTH_URL": "", "CALENDAR_URL": "http://127.0.0.1:14788",
             "CONTAINERS": "appliance-otsd-1",
             "UNITS_SYSTEM": "bitcoind.service,docker.service,nftables.service",
             "UNITS_USER": "api-endpoint.service,selfstamp.timer",
             "FEEDER_LOG": "", "TOR_CONTAINER": "", "DISK_BOOT": "", "DHCP_IFACE": ""}


def cfg_with(**over):
    cfg = dict(watch.DEFAULTS)
    cfg.update(over)
    return cfg


class Test_fixtures(unittest.TestCase):
    def test_every_fixture_scenario_passes(self):
        lines = []
        total, failed = watch.run_fixtures(out=lines.append)
        self.assertGreaterEqual(total, 29)
        self.assertEqual(failed, 0, "\n".join(lines))


class Test_active_checks(unittest.TestCase):
    def test_empty_knob_skips_its_check_and_its_count(self):
        active = watch.active_checks(cfg_with(**APPLIANCE))
        for skipped in ("health_reach", "health", "feeder", "tor_circuits", "disk_boot", "dhcp_lease"):
            self.assertNotIn(skipped, active)
        self.assertIn("calendar", active)
        self.assertEqual(len(active), 15)

    def test_pi_defaults_keep_their_twenty_and_no_calendar(self):
        active = watch.active_checks(cfg_with())
        self.assertEqual(len(active), 20)
        self.assertNotIn("calendar", active)
        self.assertEqual([n for n in watch.ORDER if n != "calendar"], active)

    def test_a_skipped_check_never_alerts_or_counts(self):
        # The appliance has no feeder: a missing feeder log is not a failure.
        cfg = cfg_with(**APPLIANCE)
        o = {"calendar_reach": True,
             "calendar": {"best_block": "00" * 32, "anchor_receipts": "on", "balance": "212,015"},
             "containers": {"appliance-otsd-1": "Up 2 days"},
             "units_system": {u: "active" for u in cfg["UNITS_SYSTEM"].split(",")},
             "units_user": {u: "active" for u in cfg["UNITS_USER"].split(",")},
             "disk": {"/": 9.1}, "temp_c": 40.0, "mem_avail_mb": 4000,
             "endpoint_age": 5, "endpoint_breaker": "ok", "journal_errors": 0, "ssh_failures": 0,
             "anchors": 3, "anchor_age_h": 2.0, "btc_peers": 8, "uptime_s": 1000}
        checks = watch.evaluate(o, cfg)
        self.assertFalse(checks["feeder"][0])          # computed, but ...
        state, msgs = watch.decide({}, checks, 1788600000, cfg)
        state, msgs = watch.decide(state, checks, 1788600300, cfg)
        self.assertEqual(msgs, [])                     # ... never alerted
        self.assertEqual(state["delivered"], [])
        self.assertIn("ok 15/15", watch.status_line(1788600300, state, checks, cfg))


class Test_calendar_check(unittest.TestCase):
    def verdict(self, reach, cal, min_sats="100000"):
        cfg = cfg_with(CALENDAR_URL="http://127.0.0.1:14788", CAL_MIN_SATS=min_sats)
        return watch.evaluate({"calendar_reach": reach, "calendar": cal}, cfg)["calendar"]

    def test_verdicts(self):
        ok = {"best_block": "00" * 32, "anchor_receipts": "on", "balance": "22,015"}
        self.assertEqual(self.verdict(True, ok, "20000"), (True, ""))
        self.assertEqual(self.verdict(False, None), (False, "no answer from the calendar"))
        self.assertEqual(self.verdict(True, {}), (False, "calendar is Bitcoin-blind"))
        self.assertEqual(self.verdict(True, dict(ok, best_block=None)), (False, "calendar is Bitcoin-blind"))
        self.assertEqual(self.verdict(True, dict(ok, anchor_receipts="off")),
                         (False, "calendar anchor receipts off"))
        self.assertEqual(self.verdict(True, ok), (False, "anchor wallet 22015 sats < 100000"))
        self.assertEqual(self.verdict(True, dict(ok, balance="lots")), (False, "calendar balance unreadable"))

    def test_parse_sats(self):
        self.assertEqual(watch.parse_sats("22,015"), 22015)
        self.assertEqual(watch.parse_sats(5), 5)
        self.assertEqual(watch.parse_sats(" 7 "), 7)
        self.assertIsNone(watch.parse_sats("lots"))
        self.assertIsNone(watch.parse_sats(None))
        self.assertIsNone(watch.parse_sats(True))


class Test_names(unittest.TestCase):
    def test_messages_carry_the_configured_name_or_the_hostname(self):
        import socket
        cfg = cfg_with(**APPLIANCE)
        checks = {n: (True, "") for n in watch.ORDER}
        checks["calendar"] = (False, "no answer from the calendar")
        state, msgs = watch.decide({}, checks, 1788600000, cfg_with(NAME="box7", **APPLIANCE))
        state, msgs = watch.decide(state, checks, 1788600300, cfg_with(NAME="box7", **APPLIANCE))
        self.assertEqual(msgs, ["box7 DEGRADED: no answer from the calendar | still: none"])
        state, msgs = watch.decide({}, checks, 1788600000, cfg)
        state, msgs = watch.decide(state, checks, 1788600300, cfg)
        self.assertTrue(msgs[0].startswith(socket.gethostname() + " DEGRADED: "), msgs)

    def test_heartbeat_names_the_wallet_when_the_calendar_is_observed(self):
        cfg = cfg_with(NAME="box7", **APPLIANCE)
        checks = {n: (True, "") for n in watch.ORDER}
        o = {"calendar": {"balance": "22,015", "pending_commitments": "1,204"}, "anchors": 4, "anchor_age_h": 1.5,
             "disk": {"/": 9.1}, "temp_c": 40.0, "mem_avail_mb": 4000, "uptime_s": 100}
        line = watch.heartbeat_line({"delivered": []}, checks, o, cfg)
        self.assertTrue(line.startswith("box7 heartbeat: ok 15/15"), line)
        self.assertIn("wallet 22,015 sats, pending 1,204", line)
        line = watch.heartbeat_line({"delivered": []}, checks, dict(o, calendar=None), cfg)
        self.assertNotIn("wallet", line)


if __name__ == "__main__":
    unittest.main()
