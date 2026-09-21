#!/usr/bin/env python3
"""Box health watcher: one run per timer tick, stdlib only, no listener.

Reads what already exists — the calendar's own JSON status on loopback
(CALENDAR_URL), or the gateway's /health (HEALTH_URL) on the hosted shape,
unit and container states, disk, temperature, memory, the feeder log and
endpoint heartbeat mtimes, journal error and ssh-failure counts since the
last run, the receipts file, refused-outbound log lines, the dhcp lease, tor
and bitcoind liveness — writes <WATCH_DIR>/status (one line), and alerts
off-box through ntfy ONLY on transitions of the set of failing checks, plus
one heartbeat per UTC day. A persisting problem never re-alarms.

    watch.py            one real run (the timer's ExecStart)
    watch.py --dry      real observations, print what would be sent, send nothing
    watch.py --test     fixture scenarios under tests/watch/, sender stubbed, exit 1 on any deviation

What a run writes, and in what order (docs/contracts.md, section 8, W1):
the status line; then one record, state.json, holding the delivered set
and counters, the journal cursor, and, under "owed", the outbox as it must
now be (what was queued, what this run adds, the cap applied); then that
queue copied to outbox.json and the record rewritten without it; then
delivery oldest first, the outbox rewritten after each success. A stop
before the record leaves nothing to recover: the next run observes afresh
and the journal window is read again because the cursor did not move. A
stop after it is finished by the next run copying the recorded queue over
the outbox, so nothing is queued twice and nothing already discarded comes
back. An outbox that cannot be read (a permission error) ends the run
before anything is observed; one whose bytes are not a message list is set
aside as outbox.json.corrupt-<12 hex of the bytes' sha256> and reported
as an alert; the same for state.json. A run holds an exclusive lock on
WATCH_DIR for its whole duration (<WATCH_DIR>/.lock), a second run waiting
up to LOCK_WAIT seconds and then exiting 1 with 'locked', having observed
nothing. Every file is written under a unique temporary name. "Delivered"
in the state means an alarm transition was recorded, not that the operator
received anything: that is the outbox's business, at least once.

Two shapes, one script, chosen by the config: the hosted shape (the Pi)
sets HEALTH_URL and the demo's feeder and tor knobs; the appliance shape
sets CALENDAR_URL and leaves what it has not got EMPTY. An empty knob skips
its check entirely — it neither fails nor counts — so "ok 16/16" on an
appliance and "ok 21/21" on the Pi both mean every configured check passed.
The script lives in the fork's ops/ (read-only on the box); config, state,
status and log live in WATCH_DIR (default ~/watcher).

Alert contract: checks fail on two consecutive runs before alerting and recover on
two, except the burst checks (journal_errors, ssh_failures, egress_drops, ssh_unexpected) which
alert on the run they are seen and clear on the next. egress_drops counts kernel "egress-drop*"
lines (the host firewall's refused outbound, rate-limited at the source) since the previous run;
more than EGRESS_DROPS (10) in one run alerts, and the daily heartbeat carries the count since the
previous heartbeat. ssh_unexpected alerts once for any accepted publickey login whose source is
not in SSH_KNOWN_SOURCES; the message off-box carries the count, the log on the box the address.
dhcp_lease, tor_circuits, btc_peers and calendar follow the two-run rule.

Four rules (docs/contracts.md, section 8):

- Unknown is a state. A check whose source could not be read or gave no
  answer has the verdict None, never True: the status line and the
  heartbeat name it as unknown, and it alarms like a failure after the
  same two runs, worded as unknown. A check whose unknown is another
  check's doing (health behind health_reach, the four journal counts
  behind journal_read; OWNED_BY) is suspended while the owner fails: it
  neither alarms nor recovers nor is listed as still failing, and it
  resumes with its next real verdict. An empty TEMP_C or MEM_MB skips
  that check like every other empty knob.
- The record is one write. A transition, the journal cursor and the
  outbox as it must now be (under "owed": the queue with this run's
  messages appended and the cap applied) land in state.json together, and
  only then is that queue copied to the outbox and the state rewritten
  without it. A stop before that write records nothing; a stop after it,
  on either side of the outbox write, is finished by the next run copying
  the recorded queue over the outbox: every message queued once, every
  discard final, the order kept.
- The cap is a message. The outbox keeps OUTBOX_MAX entries; when it
  would hold more, the oldest are dropped and the drop is the first
  message in line, a notice saying how many were dropped and when they
  were queued, folded into the notice already there when there is one.
  The decision is part of the record, so a replay never drops twice.
- The state file is read like the outbox: missing is a fresh start;
  unreadable fails the run with nothing done; bytes that are not a state
  object, or an object whose fields are not what the run relies on, are
  set aside as state.json.corrupt-<12 hex of their sha256> and the run
  goes on from a fresh state with a notice saying what the fresh state
  cannot know. A dry run sets nothing aside and only says what a real
  run would do. No message names a path or a configured mount.
"""
import contextlib
import datetime
import errno
import fcntl
import glob
import json
import math
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HOME = os.path.expanduser("~")
DIR = os.path.dirname(os.path.abspath(__file__))              # the script; fixtures beside it
WATCH_DIR = os.environ.get("WATCH_DIR") or os.path.join(HOME, "watcher")   # config, state, status
CONFIG = os.path.join(WATCH_DIR, "config")
STATE = os.path.join(WATCH_DIR, "state.json")
STATUS = os.path.join(WATCH_DIR, "status")
FIXTURES = os.path.join(DIR, "tests", "watch")
OUTBOX_MAX = 200    # undelivered messages kept; older ones are dropped, logged
LOCK_WAIT = 60.0    # seconds a run waits for another run's lock before exiting 1 'locked'


def outbox_path():
    return os.path.join(WATCH_DIR, "outbox.json")


def lock_path():
    return os.path.join(WATCH_DIR, ".lock")


class Locked(Exception):
    """Another run holds the state directory's lock"""


class OutboxUnreadable(Exception):
    """The outbox exists and could not be read: the run must not go on"""


@contextlib.contextmanager
def run_lock(wait=None):
    """An exclusive lock on WATCH_DIR for the whole run, across processes
    (flock on <WATCH_DIR>/.lock), so two runs never read and rewrite the
    same outbox and state. Waits up to LOCK_WAIT
    seconds for the holder, then raises Locked. The lock goes with the
    descriptor: a run that dies releases it."""
    wait = LOCK_WAIT if wait is None else wait
    fd = os.open(lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK) or time.monotonic() >= deadline:
                    raise Locked("another run holds the state directory")
                time.sleep(0.1)
        yield
    finally:
        os.close(fd)

DEFAULTS = {
    "NTFY_URL": "",
    # The box's name in every message; empty = the hostname.
    "NAME": "",
    # Hosted shape: the gateway's /health. Empty = no gateway on this box (skipped).
    "HEALTH_URL": "http://127.0.0.1:8000/health",
    # Appliance shape: the calendar's JSON status on loopback. Empty = skipped.
    # The check fails when the calendar does not answer, is Bitcoin-blind (no
    # best_block), reports an anchor needing attention (its deep-reorg
    # detector: a receipted anchor left the chain), is not writing receipts,
    # or its confirmed anchor-wallet balance is below CAL_MIN_SATS (the
    # gateway's float alarm: five fee caps).
    "CALENDAR_URL": "", "CAL_MIN_SATS": "100000",
    "CONTAINERS": "gateway-gateway-1,gateway-otsd-1,gateway-tor-1",
    "UNITS_SYSTEM": "kiosk.service,bitcoind.service,docker.service,nftables.service",
    "UNITS_USER": "aircraft-endpoint.service,aircraft-feeder.service,phoenixd.service,anchor-payer.timer,selfstamp.timer",
    "DISK_ROOT": "/", "DISK_BOOT": "/boot/firmware", "DISK_PCT": "85",
    "TEMP_C": "75", "MEM_MB": "256",
    "FEEDER_LOG": HOME + "/aircraft-demo/feeder.log", "FEEDER_STALE_S": "600", "FEEDER_ERR_POLLS": "5",
    "ENDPOINT_HEARTBEAT": HOME + "/aircraft-demo/endpoint-data/heartbeat", "ENDPOINT_STALE_S": "180",
    "JOURNAL_ERRORS": "20", "SSH_FAILURES": "10",
    "RECEIPTS": HOME + "/gateway/receipts/anchor-receipts.jsonl", "ANCHOR_MAX_H": "36",
    "HEARTBEAT_HOUR": "8", "CONFIRM_RUNS": "2",
    # Refused-outbound burst, dhcp renewal, tor liveness, bitcoind peers.
    "EGRESS_DROPS": "10", "DHCP_IFACE": "eth0", "DHCP_MIN_H": "6",
    "TOR_CONTAINER": "gateway-tor-1", "TOR_HB_MAX_H": "7", "TOR_WARN_MIN": "30",
    "BTC_P2P_PORT": "8333", "BTC_MIN_PEERS": "3",
    # accepted ssh logins from any source not listed here alert once (comma-separated addresses or CIDRs)
    "SSH_KNOWN_SOURCES": "",
}
BURST_CHECKS = ("journal_errors", "ssh_failures", "egress_drops", "ssh_unexpected")   # one-run events: alert at once, clear at once
ORDER = ["health_reach", "health", "calendar", "containers", "units_system", "units_user", "disk_root", "disk_boot",
         "temp", "mem", "feeder", "endpoint", "journal_errors", "ssh_failures", "journal_read", "anchor_age",
         "reboot_wanted", "egress_drops", "dhcp_lease", "tor_circuits", "btc_peers", "ssh_unexpected"]
# A check whose knob is empty is not configured on this box: skipped, never counted.
SKIP_WHEN_EMPTY = {"health_reach": "HEALTH_URL", "health": "HEALTH_URL", "calendar": "CALENDAR_URL",
                   "containers": "CONTAINERS", "units_system": "UNITS_SYSTEM", "units_user": "UNITS_USER",
                   "disk_root": "DISK_ROOT", "disk_boot": "DISK_BOOT", "temp": "TEMP_C", "mem": "MEM_MB",
                   "feeder": "FEEDER_LOG", "endpoint": "ENDPOINT_HEARTBEAT", "anchor_age": "RECEIPTS",
                   "dhcp_lease": "DHCP_IFACE", "tor_circuits": "TOR_CONTAINER", "btc_peers": "BTC_P2P_PORT"}
# A check whose unknown is another check's failure: while the owner fails
# the check is suspended (decide), and the owner's alarm speaks for it.
OWNED_BY = {"health": "health_reach", "journal_errors": "journal_read", "ssh_failures": "journal_read",
            "egress_drops": "journal_read", "ssh_unexpected": "journal_read"}
# What `systemctl is-active` can say; anything else is not an answer.
SYSTEMD_STATES = {"active", "inactive", "failed", "activating", "deactivating", "reloading", "maintenance",
                  "refreshing", "unknown"}


def active_checks(cfg):
    """The checks this box has configured, in ORDER."""
    return [n for n in ORDER if n not in SKIP_WHEN_EMPTY or cfg.get(SKIP_WHEN_EMPTY[n])]


def box_name(cfg):
    return cfg.get("NAME") or socket.gethostname()


def parse_sats(v):
    """The calendar renders sats as '22,015' (its str_sat); ints pass through."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    try:
        return int(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG):
        with open(CONFIG) as fd:
            for line in fd:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip()
    return cfg


def run_command(cmd, timeout=30):
    try:
        p = subprocess.run_command(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout
    except Exception as e:
        return -1, "exc %r" % (e,)


def file_age(path, now):
    """(seconds since the file last changed, error): error is None,
    'missing' or 'unreadable'. A file that cannot be stat'ed is not a
    stale file, and evaluate must not read it as one."""
    try:
        return now - os.stat(path).st_mtime, None
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "unreadable"


# ----------------------------------------------------------------------------- observe
def kernel_versions():
    """(running, newest installed for the running flavour) e.g. 6.18.34+rpt-rpi-2712"""
    running = os.uname().release
    flavour = running[running.find("+"):] if "+" in running else ""
    def key(v):
        return tuple(int(x) if x.isdigit() else 0 for x in v.split("+")[0].replace("-", ".").split("."))
    try:
        cands = [d for d in os.listdir("/lib/modules") if d.endswith(flavour)]
    except OSError:
        return running, None          # unknown: evaluate says so, never "up to date"
    newest = max(cands, key=key) if cands else running
    return running, newest


def pending_updates():
    """(total, security) from the apt cache only (no network); ~1 s, so heartbeat runs only."""
    rc, out = run_command(["apt", "list", "--upgradable"], timeout=60)
    if rc != 0:
        return None, None
    lines = [l for l in out.splitlines() if "/" in l and not l.startswith("Listing")]
    return len(lines), sum(1 for l in lines if "security" in l.split(" ")[0])


def fetch_json(url, headers=None, timeout=60):
    """(parsed body or None, reached). A 503 still carries a body (the
    gateway's degraded /health). An answer whose body is not JSON is
    reached with no body (evaluate calls that unknown, not absent); no
    answer at all is unreached."""
    req = urllib.request.Request(url, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
        except Exception:
            return None, False
    except Exception:
        return None, False
    try:
        return json.loads(raw.decode("utf-8")), True
    except (ValueError, UnicodeDecodeError):
        return None, True


def observe(cfg, now, cursors, want_updates=False):
    """Raw observations only; no judgement here (evaluate does that). A knob
    left empty is not observed at all."""
    o = {"now": now}
    o["kernel_running"], o["kernel_newest"] = kernel_versions()
    o["reboot_required_file"] = os.path.exists("/run/reboot-required")
    o["updates_total"], o["updates_security"] = pending_updates() if want_updates else (None, None)
    o["health"], o["health_reach"] = None, None
    if cfg["HEALTH_URL"]:
        o["health"], o["health_reach"] = fetch_json(cfg["HEALTH_URL"])
    o["calendar"], o["calendar_reach"] = None, None
    if cfg["CALENDAR_URL"]:
        o["calendar"], o["calendar_reach"] = fetch_json(cfg["CALENDAR_URL"].rstrip("/") + "/",
                                                        {"Accept": "application/json"})
    o["containers"] = None
    if cfg["CONTAINERS"]:
        rc, out = run_command(["docker", "ps", "--format", "{{.Names}} {{.Status}}"])
        o["containers"] = {l.split(" ", 1)[0]: l.split(" ", 1)[1] for l in out.splitlines() if " " in l} if rc == 0 else None
    o["units_system"] = {}
    for u in [u for u in cfg["UNITS_SYSTEM"].split(",") if u]:
        o["units_system"][u] = run_command(["systemctl", "is-active", u])[1].strip()
    o["units_user"] = {}
    for u in [u for u in cfg["UNITS_USER"].split(",") if u]:
        o["units_user"][u] = run_command(["systemctl", "--user", "is-active", u])[1].strip()
    o["disk"] = {}
    for key in ("DISK_ROOT", "DISK_BOOT"):
        if not cfg[key]:
            continue
        try:
            st = os.statvfs(cfg[key])
            o["disk"][cfg[key]] = round(100.0 * (1 - st.f_bavail / st.f_blocks), 1)
        except OSError:
            o["disk"][cfg[key]] = None
    try:
        o["temp_c"] = int(open("/sys/class/thermal/thermal_zone0/temp").read().strip()) / 1000.0
    except Exception:
        o["temp_c"] = None
    try:
        mem = dict(l.split(":") for l in open("/proc/meminfo").read().splitlines() if ":" in l)
        o["mem_avail_mb"] = int(mem["MemAvailable"].split()[0]) // 1024
    except Exception:
        o["mem_avail_mb"] = None
    o["feeder_age"], o["feeder_err_polls"], o["feeder_error"], o["feeder_tail_error"] = None, None, None, None
    if cfg["FEEDER_LOG"]:
        o["feeder_age"], o["feeder_error"] = file_age(cfg["FEEDER_LOG"], now)
        rc, out = run_command(["tail", "-n", cfg["FEEDER_ERR_POLLS"], cfg["FEEDER_LOG"]])
        if rc != 0:
            o["feeder_tail_error"] = "tail failed"
        else:
            try:
                errs = [int(l.split("errors=")[1].split()[0]) for l in out.splitlines() if "errors=" in l]
            except (IndexError, ValueError):
                errs = []
            if errs:
                o["feeder_err_polls"] = sum(1 for e in errs if e > 0)
            else:
                o["feeder_tail_error"] = "no poll lines"
    o["endpoint_age"], o["endpoint_breaker"], o["endpoint_error"] = None, None, None
    if cfg["ENDPOINT_HEARTBEAT"]:
        o["endpoint_age"], o["endpoint_error"] = file_age(cfg["ENDPOINT_HEARTBEAT"], now)
        if o["endpoint_error"] is None:
            try:
                with open(cfg["ENDPOINT_HEARTBEAT"], "rb") as fd:
                    tokens = fd.read().decode("utf-8").split()
            except OSError:
                o["endpoint_error"] = "unreadable"
            except UnicodeDecodeError:
                o["endpoint_error"] = "unparseable"
            else:
                breakers = [t.split("=", 1)[1] for t in tokens if t.startswith("breaker=")]
                o["endpoint_breaker"] = breakers[0] if breakers else None
    # The journal since the last run's cursor. A query that fails (a nonzero
    # exit, a timeout) is recorded in journal_failed and reads as no lines,
    # never as no events: evaluate turns the list into the journal_read
    # check, and real_run keeps the cursor at journal_since until every
    # query succeeds. The pattern searches are made here, over the
    # window's lines, not by journalctl's -g: with -q that flag exits 1
    # when nothing matches (systemd v257, journalctl-show.c), so a quiet
    # window would read as a failed query, the cursor would never move
    # and journal_read would alarm.
    o["journal_since"] = int(cursors.get("journal", now - 300))
    since = "@%d" % o["journal_since"]
    o["journal_failed"] = []

    def journal(label, *args, pattern=None):
        rc, out = run_command(["journalctl"] + list(args))
        if rc != 0:
            o["journal_failed"].append(label)
            return ""
        if pattern is None:
            return out
        return "\n".join(l for l in out.splitlines() if re.search(pattern, l))
    o["journal_errors"] = sum(len(journal("errors" + ("(user)" if scope else ""),
                                          *(scope + ["-p", "err", "--since", since, "-q", "--no-pager"])).splitlines())
                              for scope in ([], ["--user"]))
    o["ssh_failures"] = len(journal("ssh-failures", "-u", "ssh", "--since", since, "-q", "--no-pager",
                                    pattern="Failed password|Invalid user|authentication failure|maximum authentication attempts").splitlines())
    # accepted ssh logins since the last run, by source address (zone suffix stripped)
    acc = journal("ssh-accepted", "-u", "ssh", "--since", since, "-q", "--no-pager", pattern="Accepted publickey")
    o["ssh_sources"] = sorted({l.split(" from ", 1)[1].split()[0].split("%")[0] for l in acc.splitlines() if " from " in l})
    # refused outbound since the last run: the host firewall logs "egress-drop*" at warn, rate-limited
    o["egress_drops"] = len(journal("egress", "-k", "--since", since, "-q", "--no-pager", pattern="egress-drop").splitlines())
    # dhcp: hours until the lease expires (renewal happens at half-life, so under 6 h means a renewal was missed)
    o["dhcp_lease_left_h"] = None
    if cfg["DHCP_IFACE"]:
        rc, out = run_command(["nmcli", "-t", "-f", "DHCP4.OPTION", "dev", "show", cfg["DHCP_IFACE"]])
        for l in out.splitlines():
            if "expiry = " in l:
                try:
                    o["dhcp_lease_left_h"] = (int(l.rsplit("= ", 1)[1]) - now) / 3600.0
                except ValueError:
                    pass
    # tor: a heartbeat (every 6 h) or a bootstrap line in the window, its circuit count, and recent no-network warnings
    o["tor_alive_lines"], o["tor_circuits"], o["tor_net_warn"] = None, None, None
    if cfg["TOR_CONTAINER"]:
        rc, out = run_command(["docker", "logs", "--since", cfg["TOR_HB_MAX_H"] + "h", cfg["TOR_CONTAINER"]])
        if rc == 0:
            alive = [l for l in out.splitlines() if "Bootstrapped 100%" in l or "Heartbeat: Tor's uptime" in l]
            o["tor_alive_lines"] = len(alive)
            hb = [l for l in alive if "circuits open" in l]
            if hb:
                try:
                    o["tor_circuits"] = int(hb[-1].split("with ", 1)[1].split(" circuits")[0])
                except (IndexError, ValueError):
                    pass
            rc2, out2 = run_command(["docker", "logs", "--since", cfg["TOR_WARN_MIN"] + "m", cfg["TOR_CONTAINER"]])
            o["tor_net_warn"] = sum(1 for l in out2.splitlines() if "network activity" in l) if rc2 == 0 else None
    # bitcoind: established outbound p2p connections
    o["btc_peers"] = None
    if cfg["BTC_P2P_PORT"]:
        rc, out = run_command(["ss", "-Htn", "state", "established", "( dport = :%s )" % cfg["BTC_P2P_PORT"]])
        o["btc_peers"] = len(out.splitlines()) if rc == 0 else None
    o["anchors"], o["anchor_age_h"], o["receipts_error"] = None, None, None
    if cfg["RECEIPTS"]:
        try:
            with open(cfg["RECEIPTS"], "rb") as fd:
                raw = fd.read()
        except FileNotFoundError:
            o["receipts_error"] = "missing"
        except OSError:
            o["receipts_error"] = "unreadable"
        else:
            # The newest confirmation, not the last line: a receipt
            # recovered from its marker is appended after later anchors'
            # lines (docs/contracts.md, C5), so the last line can make a
            # fresh anchor read as stale. A confirmed_at that is not a
            # finite number makes the file unparseable.
            try:
                lines = [json.loads(l) for l in raw.decode("utf-8").splitlines() if l.strip()]
                o["anchors"] = len(lines)
                if lines:
                    times = [l["confirmed_at"] for l in lines]
                    if not all(_finite(t) for t in times):
                        raise ValueError("confirmed_at is not a finite number")
                    o["anchor_age_h"] = (now - max(times)) / 3600.0
            except (ValueError, KeyError, TypeError, UnicodeDecodeError):
                o["anchors"], o["anchor_age_h"], o["receipts_error"] = None, None, "unparseable"
    try:
        o["uptime_s"] = float(open("/proc/uptime").read().split()[0])
    except Exception:
        o["uptime_s"] = None
    return o


# ----------------------------------------------------------------------------- evaluate
def unexpected_sources(o, cfg):
    """The accepted-login sources of this window that SSH_KNOWN_SOURCES does not name"""
    known = [k.strip() for k in cfg.get("SSH_KNOWN_SOURCES", "").split(",") if k.strip()]
    return [a for a in (o.get("ssh_sources") or []) if not source_known(a, known)]


def evaluate(o, cfg):
    """Every check -> (verdict, detail). The verdict is True (ok), False
    (failed) or None (unknown: the source could not be read or gave no
    answer, which is never ok). Detail is the short text an alert quotes;
    an unknown's detail says unknown and why. Checks whose knob is empty
    are computed but never counted (active_checks)."""
    c = {}
    reach = o.get("health_reach")
    c["health_reach"] = (reach is True, "no answer from /health")
    h = o.get("health")
    if not reach:
        c["health"] = (None, "health unknown: /health unreachable")   # health_reach speaks for it
    elif not isinstance(h, dict):
        c["health"] = (None, "health unknown: body is not an object")
    else:
        bad = [f for f in ("payment", "otsd", "wallet", "float", "proofs", "backup", "billing") if h.get(f) not in
               (None, "ok", "unknown", "absent", "inactive", "local_only", "off", "n/a")]
        st = h.get("status")
        c["health"] = (st == "ok", "health=%s %s" % (st, " ".join("%s=%s" % (f, h.get(f)) for f in bad)))
    cal = o.get("calendar")
    if not o.get("calendar_reach"):
        c["calendar"] = (False, "no answer from the calendar")
    elif not isinstance(cal, dict):
        c["calendar"] = (None, "calendar unknown: status unreadable")
    elif not cal.get("best_block"):
        c["calendar"] = (False, "calendar is Bitcoin-blind")
    elif cal.get("needs_attention"):
        c["calendar"] = (False, "calendar needs attention: " + "; ".join(str(f) for f in cal["needs_attention"]))
    elif cal.get("anchor_receipts") != "on":
        c["calendar"] = (False, "calendar anchor receipts %s" % cal.get("anchor_receipts"))
    else:
        sats = parse_sats(cal.get("balance"))
        if sats is None:
            c["calendar"] = (None, "calendar unknown: balance unreadable")
        elif sats < int(cfg["CAL_MIN_SATS"]):
            c["calendar"] = (False, "anchor wallet %d sats < %s" % (sats, cfg["CAL_MIN_SATS"]))
        else:
            c["calendar"] = (True, "")
    cs = o.get("containers")
    if cs is None:
        c["containers"] = (None, "containers unknown: docker ps failed")
    else:
        down = [n for n in cfg["CONTAINERS"].split(",") if n and not cs.get(n, "").startswith("Up")]
        c["containers"] = (not down, "down: " + ",".join(down))
    for key in ("units_system", "units_user"):
        states = o.get(key) or {}
        odd = [u for u, st in states.items() if st not in SYSTEMD_STATES]
        if odd:
            c[key] = (None, "%s unknown: systemctl gave no state for %s" % (key.replace("_", " "), ",".join(odd)))
        else:
            bad = [u for u, st in states.items() if st != "active"]
            c[key] = (not bad, ",".join("%s=%s" % (u, states.get(u)) for u in bad))
    for key, name in (("DISK_ROOT", "disk_root"), ("DISK_BOOT", "disk_boot")):
        # The role and the measurement leave the box; the mount is configuration.
        pct = (o.get("disk") or {}).get(cfg[key])
        if pct is None:
            c[name] = (None, "%s unknown: mount unreadable" % name)
        else:
            c[name] = (pct < float(cfg["DISK_PCT"]), "%s at %s%%" % (name, pct))
    t = o.get("temp_c")
    if t is None:
        c["temp"] = (None, "temp unknown: no thermal reading")
    else:
        c["temp"] = (not cfg["TEMP_C"] or t < float(cfg["TEMP_C"]), "temp %sC" % t)
    m = o.get("mem_avail_mb")
    if m is None:
        c["mem"] = (None, "mem unknown: meminfo unreadable")
    else:
        c["mem"] = (not cfg["MEM_MB"] or m >= int(cfg["MEM_MB"]), "mem available %sMB" % m)
    fa, fe = o.get("feeder_age"), o.get("feeder_err_polls")
    if o.get("feeder_error") == "unreadable":
        c["feeder"] = (None, "feeder unknown: log unreadable")
    elif fa is None:
        c["feeder"] = (False, "feeder log missing")
    elif fa > float(cfg["FEEDER_STALE_S"]):
        c["feeder"] = (False, "feeder log stale %dm" % (fa // 60))
    elif o.get("feeder_tail_error") or fe is None:
        c["feeder"] = (None, "feeder unknown: %s" % (o.get("feeder_tail_error") or "no poll lines"))
    elif fe >= int(cfg["FEEDER_ERR_POLLS"]):
        c["feeder"] = (False, "feeder last %d polls all with errors" % fe)
    else:
        c["feeder"] = (True, "")
    ea, eb = o.get("endpoint_age"), o.get("endpoint_breaker")
    if o.get("endpoint_error") in ("unreadable", "unparseable"):
        c["endpoint"] = (None, "endpoint unknown: heartbeat %s" % o["endpoint_error"])
    elif ea is None:
        c["endpoint"] = (False, "endpoint heartbeat missing")
    elif ea > float(cfg["ENDPOINT_STALE_S"]):
        c["endpoint"] = (False, "endpoint heartbeat stale %dm" % (ea // 60))
    elif eb is None:
        c["endpoint"] = (None, "endpoint unknown: heartbeat has no breaker field")
    elif eb != "ok":
        c["endpoint"] = (False, "endpoint breaker=%s" % eb)
    else:
        c["endpoint"] = (True, "")
    failed = o.get("journal_failed") or []
    c["journal_read"] = (not failed, "journalctl failed: %s (window kept, read again next run)" % ", ".join(failed))
    if "errors" in failed or "errors(user)" in failed:
        c["journal_errors"] = (None, "journal errors unknown: journalctl failed")
    else:
        je = o.get("journal_errors") or 0
        c["journal_errors"] = (je <= int(cfg["JOURNAL_ERRORS"]), "%d journal errors since last check" % je)
    if "ssh-failures" in failed:
        c["ssh_failures"] = (None, "ssh failures unknown: journalctl failed")
    else:
        sf = o.get("ssh_failures") or 0
        c["ssh_failures"] = (sf <= int(cfg["SSH_FAILURES"]), "%d ssh auth failures since last check" % sf)
    if "egress" in failed:
        c["egress_drops"] = (None, "refused outbound unknown: journalctl failed")
    else:
        ed = o.get("egress_drops") or 0
        c["egress_drops"] = (ed <= int(cfg["EGRESS_DROPS"]), "%d refused outbound packets since last check" % ed)
    if "ssh-accepted" in failed:
        c["ssh_unexpected"] = (None, "ssh sources unknown: journalctl failed")
    else:
        # The count leaves the box; the addresses stay in the log on it.
        unexpected = unexpected_sources(o, cfg)
        c["ssh_unexpected"] = (not unexpected, "ssh accepted from %d source%s not in SSH_KNOWN_SOURCES"
                               % (len(unexpected), "" if len(unexpected) == 1 else "s"))
    err, anchors, ah = o.get("receipts_error"), o.get("anchors"), o.get("anchor_age_h")
    if err:
        c["anchor_age"] = (None, "anchor age unknown: receipts %s" % err)
    elif anchors is None:
        c["anchor_age"] = (None, "anchor age unknown: receipts not observed")
    elif not anchors or ah is None:
        c["anchor_age"] = (None, "anchor age unknown: no receipt yet")
    else:
        c["anchor_age"] = (ah <= float(cfg["ANCHOR_MAX_H"]), "last anchor %.0fh ago" % ah)
    kr, kn = o.get("kernel_running"), o.get("kernel_newest")
    if o.get("reboot_required_file"):
        c["reboot_wanted"] = (False, "reboot wanted: /run/reboot-required is set")
    elif kr is None or kn is None:
        c["reboot_wanted"] = (None, "reboot unknown: kernel list unreadable")
    elif kr != kn:
        c["reboot_wanted"] = (False, "reboot wanted: kernel %s installed, running %s" % (kn.split("+")[0], kr.split("+")[0]))
    else:
        c["reboot_wanted"] = (True, "")
    dl = o.get("dhcp_lease_left_h")
    if dl is None:
        c["dhcp_lease"] = (None, "dhcp unknown: no lease expiry from nmcli")
    else:
        c["dhcp_lease"] = (dl >= float(cfg["DHCP_MIN_H"]), "dhcp lease expires in %.1fh, renewal missed" % dl)
    ta, tc, tw = o.get("tor_alive_lines"), o.get("tor_circuits"), o.get("tor_net_warn")
    if ta is None:
        c["tor_circuits"] = (None, "tor unknown: log unreadable")
    elif ta == 0:
        c["tor_circuits"] = (False, "no tor heartbeat in %sh" % cfg["TOR_HB_MAX_H"])
    elif tc == 0:
        c["tor_circuits"] = (False, "tor reports 0 circuits open")
    elif tw is None:
        c["tor_circuits"] = (None, "tor unknown: the warning window could not be read")
    elif tw > 0:
        c["tor_circuits"] = (False, "tor: %d no-network-activity warnings in %sm" % (tw, cfg["TOR_WARN_MIN"]))
    else:
        c["tor_circuits"] = (True, "")
    bp = o.get("btc_peers")
    if bp is None:
        c["btc_peers"] = (None, "bitcoind peers unknown: ss failed")
    else:
        c["btc_peers"] = (bp >= int(cfg["BTC_MIN_PEERS"]), "%s bitcoind peers established" % bp)
    return c


# ----------------------------------------------------------------------------- decide
def decide(state, checks, now, cfg):
    """Pure: (state, checks, now) -> (new_state, messages). Transition-only alerts,
    two-run confirmation both ways (burst checks one run), one heartbeat per UTC day.
    Only the checks this box has configured take part."""
    s = json.loads(json.dumps(state)) if state else {}
    s.setdefault("delivered", [])
    s.setdefault("fail_runs", {})
    s.setdefault("ok_runs", {})
    s.setdefault("since", {})
    confirm = int(cfg["CONFIRM_RUNS"])
    active = active_checks(cfg)
    name = box_name(cfg)
    delivered = set(s["delivered"])
    joined, left, suspended = [], [], []
    for check in active:
        ok, detail = checks.get(check, (True, ""))
        owner = OWNED_BY.get(check)
        if ok is None and owner and checks.get(owner, (True, ""))[0] is not True:
            # Unknown because the owner failed: the owner's alarm speaks for
            # it; nothing here moves until it has a verdict of its own.
            suspended.append(check)
            continue
        need = 1 if check in BURST_CHECKS else confirm
        if ok is True:
            s["fail_runs"][check] = 0
            s["ok_runs"][check] = s["ok_runs"].get(check, 0) + 1
            if check in delivered and s["ok_runs"][check] >= need:
                delivered.discard(check)
                left.append(check)
        else:
            s["ok_runs"][check] = 0
            s["fail_runs"][check] = s["fail_runs"].get(check, 0) + 1
            s["since"].setdefault(check, now)
            if check not in delivered and s["fail_runs"][check] >= need:
                delivered.add(check)
                joined.append(check)
    msgs = []
    still = [n for n in active if n in delivered and n not in joined and n not in suspended]
    if joined:
        what = "; ".join(checks[n][1] for n in joined)
        rest = "; ".join(checks[n][1] if checks[n][0] is not True else "%s recovering" % n for n in still) if still else "none"
        msgs.append("%s DEGRADED: %s | still: %s" % (name, what, rest))
    if left and not delivered:
        dur = max(now - s["since"].get(n, now) for n in left)
        not_clear = [n for n in active if checks.get(n, (True, ""))[0] is not True]
        if not_clear:
            # The alarm set emptied, and that is said; "all ok" is not, because it is not so.
            msgs.append("%s RECOVERED: %s ok (was failing %s); not all clear: %s" % (
                name, ", ".join(left), fmt_dur(dur), "; ".join(checks[n][1] for n in not_clear)))
        else:
            msgs.append("%s RECOVERED: all %d checks ok (was: %s; %s)" % (name, len(active), ", ".join(left), fmt_dur(dur)))
    for n in left:
        s["since"].pop(n, None)
    s["delivered"] = [n for n in active if n in delivered]
    today = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%d")
    hour = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).hour
    if s.get("heartbeat_day") != today and hour >= int(cfg["HEARTBEAT_HOUR"]):
        s["heartbeat_day"] = today
        s["heartbeat_pending"] = True
    return s, msgs


def source_known(addr, known):
    """addr matches an entry exactly, or falls inside a CIDR entry."""
    import ipaddress
    try:
        a = ipaddress.ip_address(addr)
    except ValueError:
        return addr in known
    for k in known:
        try:
            if "/" in k and a in ipaddress.ip_network(k, strict=False):
                return True
            if "/" not in k and a == ipaddress.ip_address(k):
                return True
        except ValueError:
            continue
    return False


def fmt_dur(sec):
    sec = int(sec)
    if sec < 3600:
        return "%dm" % (sec // 60)
    if sec < 86400:
        return "%dh%02dm" % (sec // 3600, (sec % 3600) // 60)
    return "%dd %dh" % (sec // 86400, (sec % 86400) // 3600)


def heartbeat_line(s, checks, o, cfg):
    """One line a day, worded from this run's verdicts: `ok N/N` only when
    every check is ok now; a failing check is named whether or not its
    alarm has been confirmed (an unconfirmed one says so); an unknown one
    is named as unknown. The alarm set decides notifications, never this
    wording."""
    active = active_checks(cfg)
    name = box_name(cfg)
    delivered = set(s.get("delivered", []))
    failed = [n for n in active if checks[n][0] is False]
    unknown = [n for n in active if checks[n][0] is None]
    if not failed and not unknown:
        head = "%s heartbeat: ok %d/%d" % (name, len(active), len(active))
    else:
        parts = []
        if failed:
            parts.append("degraded: " + "; ".join(checks[n][1] + ("" if n in delivered else " (not yet alarmed)") for n in failed))
        if unknown:
            parts.append("unknown: " + ", ".join(unknown))
        head = "%s heartbeat: " % name + " | ".join(parts)
    disk = o.get("disk") or {}
    vit = "disk %s%%" % disk.get(cfg.get("DISK_ROOT") or "/", "?")
    if cfg.get("DISK_BOOT"):
        vit += " boot %s%%" % disk.get(cfg["DISK_BOOT"], "?")
    vit += " temp %sC mem %sMB" % (o.get("temp_c", "?"), o.get("mem_avail_mb", "?"))
    anch = "anchors %s, last %s ago" % (o.get("anchors", "?"), fmt_dur((o.get("anchor_age_h") or 0) * 3600))
    cal = o.get("calendar") if isinstance(o.get("calendar"), dict) else None
    if cal is not None:
        anch += ", wallet %s sats, pending %s" % (cal.get("balance", "?"), cal.get("pending_commitments", "?"))
    up = "up %s" % fmt_dur(o.get("uptime_s") or 0)
    upd = "updates: %s pending (%s security)" % (o.get("updates_total", "?"), o.get("updates_security", "?"))
    drops = "egress drops %s" % o.get("drops_acc", o.get("egress_drops", "?"))
    return " | ".join([head, vit, anch, upd, drops, up])


def status_line(now, s, checks, cfg, queued=0):
    """One line: the time, ok|unknown|degraded, ok-count/active, then each
    failed and each unknown check with its detail, then queued=N while
    messages wait for delivery."""
    active = active_checks(cfg)
    failed = [n for n in active if checks[n][0] is False]
    unknown = [n for n in active if checks[n][0] is None]
    word = "degraded" if failed else ("unknown" if unknown else "ok")
    ts = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts = "".join(" %s=%s" % (n, checks[n][1].replace(" ", "_")) for n in failed + unknown)
    if queued:
        parts += " queued=%d" % queued
    return "%s %s %d/%d%s" % (ts, word, len(active) - len(failed) - len(unknown), len(active), parts)


# ----------------------------------------------------------------------------- send + main
def send(cfg, text):
    if not cfg.get("NTFY_URL"):
        return False, "no NTFY_URL"
    req = urllib.request.Request(cfg["NTFY_URL"], data=text.encode(), method="POST",
                                 headers={"Title": box_name(cfg), "Content-Type": "text/plain"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200, "http %s" % r.status
    except Exception as e:
        return False, "send failed: %r" % (e,)


def log(msg):
    print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg, flush=True)


def write_bytes_atomic(path, data, mode=0o644):
    """A unique temporary name in the same directory, fsync, rename, fsync
    the directory: the file is whole or absent, and two writers never share
    a temporary name."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=os.path.basename(path) + ".")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dfd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def write_text_atomic(path, text, mode=0o644):
    write_bytes_atomic(path, text.encode("utf-8"), mode)


def write_json_atomic(path, data):
    write_bytes_atomic(path, json.dumps(data, indent=1).encode("utf-8"), mode=0o600)


def load_outbox(cfg=None):
    """The undelivered messages on file. Only a missing file is an empty
    queue. A file that cannot be read raises OutboxUnreadable: the run
    fails with nothing touched. Bytes that are not a list of messages are
    set aside for inspection as outbox.json.corrupt-<12 hex of their
    sha256>, and the queue becomes one alert saying so.

    The recovery is itself interruptible: the aside copy is written first
    (a name from the bytes, so a repeat writes the same file), and the
    notice replaces the corrupt file in one rename. A stop before the
    rename leaves the corrupt file to be found and handled again; a stop
    after it leaves the notice on disk, owed like any other message. At
    no point is the corrupt file gone while the notice is only in
    memory."""
    path = outbox_path()
    try:
        with open(path, "rb") as fd:
            raw = fd.read()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise OutboxUnreadable(type(exc).__name__)
    try:
        data = json.loads(raw.decode("utf-8"))
        if not valid_messages(data):
            raise ValueError("not a list of messages")
    except (ValueError, UnicodeDecodeError) as exc:
        import hashlib
        aside = "%s.corrupt-%s" % (path, hashlib.sha256(raw).hexdigest()[:12])
        write_bytes_atomic(aside, raw, mode=0o600)
        name = box_name(cfg) if cfg is not None else socket.gethostname()
        notice = [{"text": "%s WATCHER: outbox.json could not be read (%s); %d bytes set aside as %s; alerts "
                           "queued before this run may not have been delivered"
                           % (name, exc, len(raw), os.path.basename(aside)),
                   "queued": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "notice": True}]
        write_json_atomic(path, notice)     # one rename: the corrupt file leaves as the notice lands
        log("outbox unreadable (%s); %d bytes set aside as %s; the notice is queued" % (exc, len(raw), os.path.basename(aside)))
        return notice
    return data


class StateUnreadable(Exception):
    """The state file exists and could not be read: the run must not go on"""


def valid_messages(data):
    """A list of messages, each with the two fields delivery relies on,
    and a cap notice with the count enqueue adds to: a `dropped` that is
    not a number would crash the fold."""
    return isinstance(data, list) and all(
        isinstance(m, dict) and isinstance(m.get("text"), str) and isinstance(m.get("queued"), str)
        and (not m.get("cap") or _count(m.get("dropped"))) for m in data)


def _count(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _finite(v):
    """A number the run can do arithmetic on and convert: an int, or a
    float that is finite. JSON's 1e309 decodes to infinity, which passes
    a type check and then fails int()."""
    if isinstance(v, bool):
        return False
    return isinstance(v, int) or (isinstance(v, float) and math.isfinite(v))


def valid_state(data):
    """A state object whose fields, where present, are what the run relies
    on: a list of check names delivered; run counters by check; `since`
    times by check, finite; a heartbeat day; the drop accumulator; the
    cursors, finite; the owed queue as a list of messages. Older states
    may lack any of them; a field of the wrong shape makes the file
    corrupt, and it is set aside rather than half read."""
    if not isinstance(data, dict):
        return False
    if not isinstance(data.get("delivered", []), list) or not all(isinstance(n, str) for n in data.get("delivered", [])):
        return False
    for key in ("fail_runs", "ok_runs"):
        runs = data.get(key, {})
        if not isinstance(runs, dict) or not all(isinstance(k, str) and _count(v) for k, v in runs.items()):
            return False
    since = data.get("since", {})
    if not isinstance(since, dict) or not all(isinstance(k, str) and _finite(t) for k, t in since.items()):
        return False
    if "heartbeat_day" in data and not isinstance(data["heartbeat_day"], str):
        return False
    if not _count(data.get("drops_acc", 0)):
        return False
    cursors = data.get("cursors", {})
    if not isinstance(cursors, dict):
        return False
    if "journal" in cursors and not _finite(cursors["journal"]):
        return False
    if data.get("owed") is not None and not valid_messages(data["owed"]):
        return False
    return True


def load_state(cfg, aside=True):
    """(state, notices). Missing: a fresh state. Unreadable (any error but
    absence): StateUnreadable, and the run does nothing. Bytes that are
    not a state object, or an object whose fields are not what the run
    relies on (valid_state): set aside as state.json.corrupt-<12 hex of
    their sha256> (the aside copy first, its name from the bytes, so a
    repeat writes the same file), and the run goes on from a fresh state
    with one notice to queue that says what the fresh state cannot know:
    the checks that were failing (they will alarm again once), the
    journal since the last good run (not read again) and any messages
    the old state still owed. The corrupt file itself is replaced when
    the run commits; a stop before that leaves it to be found and set
    aside again, under the same name. With aside=False (a dry run)
    nothing is written: the log says what a real run would do."""
    try:
        with open(STATE, "rb") as fd:
            raw = fd.read()
    except FileNotFoundError:
        return {}, []
    except OSError as exc:
        raise StateUnreadable(type(exc).__name__)
    try:
        data = json.loads(raw.decode("utf-8"))
        if not valid_state(data):
            raise ValueError("not a state object with the fields the run relies on")
    except (ValueError, UnicodeDecodeError) as exc:
        import hashlib
        aside_name = "%s.corrupt-%s" % (os.path.basename(STATE), hashlib.sha256(raw).hexdigest()[:12])
        if not aside:
            log("state unreadable (%s); a real run would set %d bytes aside as %s and queue a notice" % (exc, len(raw), aside_name))
            return {}, []
        write_bytes_atomic(os.path.join(os.path.dirname(STATE), aside_name), raw, mode=0o600)
        text = ("%s WATCHER: state.json could not be read (%s); %d bytes set aside as %s; starting afresh: checks "
                "failing now will alarm again once, the journal since the last good run was not read, and any "
                "alert the old state still owed is in the aside file only"
                % (box_name(cfg), exc, len(raw), aside_name))
        log("state unreadable (%s); %d bytes set aside as %s; starting afresh" % (exc, len(raw), aside_name))
        return {}, [text]
    return data, []


def enqueue(queue, new, cfg, stamp):
    """The queue with this run's messages appended, then the bound: the
    queue keeps OUTBOX_MAX entries, the oldest are dropped beyond that,
    and the drop is itself the first message in line, a notice that says
    how many were dropped and when they were queued, folded into the
    notice already at the head when there is one. Returns (queue, number
    dropped this time). The result is what the record holds, so a replay
    copies the decision instead of making it again."""
    queue = list(queue) + list(new)
    dropped = 0
    if len(queue) > OUTBOX_MAX:
        notice = queue[0] if queue[0].get("cap") else None
        rest = queue[1:] if notice else queue
        keep = OUTBOX_MAX - 1
        lost, rest = rest[:len(rest) - keep], rest[len(rest) - keep:]
        dropped = len(lost)
        total = (notice or {}).get("dropped", 0) + dropped
        first = (notice or {}).get("first") or lost[0].get("queued", "")
        last = lost[-1].get("queued", "")
        notice = {"cap": True, "dropped": total, "first": first, "last": last, "queued": stamp,
                  "text": "%s WATCHER: outbox over %d: %d oldest alerts dropped (queued %s to %s); their text is lost"
                          % (box_name(cfg), OUTBOX_MAX, total, first, last)}
        queue = [notice] + rest
    return queue, dropped


def real_run(dry):
    cfg = load_config()
    os.makedirs(WATCH_DIR, exist_ok=True)
    try:
        with run_lock():
            return _run_locked(cfg, dry)
    except Locked as exc:
        log("locked: %s; nothing done, exit 1" % exc)
        return 1


def _run_locked(cfg, dry):
    now = time.time()
    try:
        state, notices = load_state(cfg, aside=not dry)
    except StateUnreadable as exc:
        log("state unreadable (%s); nothing done, exit 1" % exc)
        return 1
    queue = None
    if cfg.get("NTFY_URL") and not dry:
        # Read before anything is observed: an outbox that cannot be read
        # ends the run with nothing done.
        try:
            loaded = load_outbox(cfg)
        except OutboxUnreadable as exc:
            log("outbox unreadable (%s); nothing done, exit 1" % exc)
            return 1
        owed = state.get("owed")
        if owed is not None:
            # A run stopped between its record and the outbox: the record
            # is the queue. What the outbox holds is that queue or older;
            # only a recovery notice the loader just made is carried over.
            queue = list(owed) + [m for m in loaded if m.get("notice") and m not in owed]
        else:
            queue = loaded
    cursors = state.get("cursors", {})
    utc = datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
    stamp = utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    want_updates = state.get("heartbeat_day") != utc.strftime("%Y-%m-%d") and utc.hour >= int(cfg["HEARTBEAT_HOUR"])
    o = observe(cfg, now, cursors, want_updates=want_updates or dry)
    checks = evaluate(o, cfg)
    for addr in unexpected_sources(o, cfg):
        log("ssh accepted from %s (not in SSH_KNOWN_SOURCES)" % addr)   # the log keeps the address; the message counts
    acc = int(state.get("drops_acc", 0)) + int(o.get("egress_drops") or 0)
    new_state, msgs = decide(state, checks, now, cfg)
    if new_state.pop("heartbeat_pending", False):
        o["drops_acc"] = acc
        msgs.append(heartbeat_line(new_state, checks, o, cfg))
        acc = 0
    new_state["drops_acc"] = acc
    msgs = notices + msgs
    new = [{"text": m, "queued": stamp} for m in msgs]

    def commit(st, owed):
        # The cursor moves past the window only when every journal query
        # read it; a failed read keeps the earliest unread cursor. `owed`
        # is the outbox as it must be, or None when the outbox has it.
        st["cursors"] = {"journal": o["journal_since"] if o.get("journal_failed") else now}
        if owed is None:
            st.pop("owed", None)
        else:
            st["owed"] = owed
        write_json_atomic(STATE, st)

    if dry:
        write_text_atomic(STATUS, status_line(now, new_state, checks, cfg, queued=len(new)) + "\n")
        log(status_line(now, new_state, checks, cfg, queued=len(new)))
        for m in msgs:
            log("WOULD SEND: " + m)
        return 0
    if not cfg.get("NTFY_URL"):
        # Status-only mode: nothing to deliver to, so nothing is owed; the
        # state records the transition and the log carries the text.
        write_text_atomic(STATUS, status_line(now, new_state, checks, cfg) + "\n")
        log(status_line(now, new_state, checks, cfg))
        for m in msgs:
            log("NOT SENT (no NTFY_URL): " + m)
        commit(new_state, None)
        return 0

    prepared, dropped = enqueue(queue, new, cfg, stamp)
    if dropped:
        log("outbox over %d messages: %d oldest dropped; the notice at the head says so" % (OUTBOX_MAX, dropped))
    line = status_line(now, new_state, checks, cfg, queued=len(prepared))
    write_text_atomic(STATUS, line + "\n")
    log(line)
    # The record, in one write: the transition, the cursor and the outbox
    # as it must now be. From here every message in it is queued, once,
    # whatever happens next; before here nothing happened.
    commit(new_state, prepared)
    try:
        write_json_atomic(outbox_path(), prepared)
        persisted = True
    except OSError as e:
        log("outbox write failed: %r; the record holds the queue and the next run writes it" % (e,))
        persisted = False
    if persisted:
        try:
            commit(new_state, None)          # the outbox has it: the record no longer carries the queue
        except OSError as e:
            log("state write failed after the outbox: %r; nothing delivered this run, the next run copies the record again" % (e,))
            return 1
    # Oldest first; stop at the first failure so alerts never reorder.
    queue = list(prepared)
    while queue:
        ok, why = send(cfg, queue[0]["text"])
        if not ok:
            log("NOT SENT (%s), kept in the outbox (%d queued): %s" % (why, len(queue), queue[0]["text"]))
            break
        log("sent: " + queue[0]["text"])
        queue.pop(0)
        if persisted:
            try:
                write_json_atomic(outbox_path(), queue)
            except OSError as e:
                log("outbox write failed after a delivery: %r; that message may be sent again" % (e,))
        else:
            try:
                commit(new_state, queue if queue else None)   # the record shrinks with each delivery
            except OSError as e:
                log("state write failed after a delivery: %r; stopping, so no unrecorded delivery is repeated" % (e,))
                break
    if len(queue) != len(prepared):
        # What is queued changed with the deliveries: the status line says so.
        write_text_atomic(STATUS, status_line(now, new_state, checks, cfg, queued=len(queue)) + "\n")
    return 1 if queue else 0


def run_fixtures(fixtures_dir=FIXTURES, out=print):
    """Each fixture: {"name", "cfg"?: {...}, "base"?: {...}, "runs": [{"t": seconds, "obs": {...}}...],
    "expect": [[msg substrings] per run]}. The fixtures were written for the Pi, so NAME
    defaults to "pi5" here unless a fixture's cfg says otherwise. Returns (total, failed)."""
    fails = 0
    paths = sorted(glob.glob(os.path.join(fixtures_dir, "*.json")))
    for path in paths:
        with open(path) as fd:
            fx = json.load(fd)
        cfg = dict(DEFAULTS)
        cfg["NAME"] = "pi5"
        cfg.update(fx.get("cfg", {}))
        state = {}
        got_all = []
        for i, r in enumerate(fx["runs"]):
            o = dict(fx.get("base", {}))
            o.update(r.get("obs", {}))
            checks = evaluate(o, cfg)
            state, msgs = decide(state, checks, r["t"], cfg)
            if state.pop("heartbeat_pending", False):
                msgs.append(heartbeat_line(state, checks, o, cfg))
            got_all.append(msgs)
        ok = True
        for i, exp in enumerate(fx["expect"]):
            got = got_all[i] if i < len(got_all) else []
            if len(got) != len(exp) or not all(e in g for e, g in zip(exp, got)):
                ok = False
                out("  run %d: expected %r\n         got      %r" % (i, exp, got))
        out("%-40s %s" % (fx["name"], "ok" if ok else "FAIL"))
        fails += 0 if ok else 1
    out("fixtures: %d, failed: %d" % (len(paths), fails))
    return len(paths), fails


def test_mode():
    return 1 if run_fixtures()[1] else 0


if __name__ == "__main__":
    if "--test" in sys.argv:
        sys.exit(test_mode())
    sys.exit(real_run("--dry" in sys.argv))
