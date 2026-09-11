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

Two shapes, one script, chosen by the config: the hosted shape (the Pi)
sets HEALTH_URL and the demo's feeder and tor knobs; the appliance shape
sets CALENDAR_URL and leaves what it has not got EMPTY. An empty knob skips
its check entirely — it neither fails nor counts — so "ok 15/15" on an
appliance and "ok 20/20" on the Pi both mean every configured check passed.
The script lives in the fork's ops/ (read-only on the box); config, state,
status and log live in WATCH_DIR (default ~/watcher).

Alert contract (2026-09-07): checks fail on two consecutive runs before alerting and recover on
two, except the burst checks (journal_errors, ssh_failures, egress_drops, ssh_unexpected) which
alert on the run they are seen and clear on the next. egress_drops counts kernel "egress-drop*"
lines (the host firewall's refused outbound, rate-limited at the source) since the previous run;
more than EGRESS_DROPS (10) in one run alerts, and the daily heartbeat carries the count since the
previous heartbeat. ssh_unexpected alerts once for any accepted publickey login whose source is
not in SSH_KNOWN_SOURCES. dhcp_lease, tor_circuits, btc_peers and calendar follow the two-run rule.
"""
import datetime
import glob
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

HOME = os.path.expanduser("~")
DIR = os.path.dirname(os.path.abspath(__file__))              # the script; fixtures beside it
WATCH_DIR = os.environ.get("WATCH_DIR") or os.path.join(HOME, "watcher")   # config, state, status
CONFIG = os.path.join(WATCH_DIR, "config")
STATE = os.path.join(WATCH_DIR, "state.json")
STATUS = os.path.join(WATCH_DIR, "status")
FIXTURES = os.path.join(DIR, "tests", "watch")

DEFAULTS = {
    "NTFY_URL": "",
    # The box's name in every message; empty = the hostname.
    "NAME": "",
    # Hosted shape: the gateway's /health. Empty = no gateway on this box (skipped).
    "HEALTH_URL": "http://127.0.0.1:8000/health",
    # Appliance shape: the calendar's JSON status on loopback. Empty = skipped.
    # The check fails when the calendar does not answer, is Bitcoin-blind (no
    # best_block), is not writing receipts, or its confirmed anchor-wallet
    # balance is below CAL_MIN_SATS (the gateway's float alarm: five fee caps).
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
    # 2026-09-07 egress session: refused-outbound burst, dhcp renewal, tor liveness, bitcoind peers
    "EGRESS_DROPS": "10", "DHCP_IFACE": "eth0", "DHCP_MIN_H": "6",
    "TOR_CONTAINER": "gateway-tor-1", "TOR_HB_MAX_H": "7", "TOR_WARN_MIN": "30",
    "BTC_P2P_PORT": "8333", "BTC_MIN_PEERS": "3",
    # accepted ssh logins from any source not listed here alert once (comma-separated addresses or CIDRs)
    "SSH_KNOWN_SOURCES": "",
}
BURST_CHECKS = ("journal_errors", "ssh_failures", "egress_drops", "ssh_unexpected")   # one-run events: alert at once, clear at once
ORDER = ["health_reach", "health", "calendar", "containers", "units_system", "units_user", "disk_root", "disk_boot",
         "temp", "mem", "feeder", "endpoint", "journal_errors", "ssh_failures", "anchor_age", "reboot_wanted",
         "egress_drops", "dhcp_lease", "tor_circuits", "btc_peers", "ssh_unexpected"]
# A check whose knob is empty is not configured on this box: skipped, never counted.
SKIP_WHEN_EMPTY = {"health_reach": "HEALTH_URL", "health": "HEALTH_URL", "calendar": "CALENDAR_URL",
                   "containers": "CONTAINERS", "units_system": "UNITS_SYSTEM", "units_user": "UNITS_USER",
                   "disk_root": "DISK_ROOT", "disk_boot": "DISK_BOOT", "feeder": "FEEDER_LOG",
                   "endpoint": "ENDPOINT_HEARTBEAT", "anchor_age": "RECEIPTS", "dhcp_lease": "DHCP_IFACE",
                   "tor_circuits": "TOR_CONTAINER", "btc_peers": "BTC_P2P_PORT"}


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


def run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout
    except Exception as e:
        return -1, "exc %r" % (e,)


def mtime_age(path, now):
    try:
        return now - os.path.getmtime(path)
    except OSError:
        return None


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
        cands = []
    newest = max(cands, key=key) if cands else running
    return running, newest


def pending_updates():
    """(total, security) from the apt cache only (no network); ~1 s, so heartbeat runs only."""
    rc, out = run(["apt", "list", "--upgradable"], timeout=60)
    if rc != 0:
        return None, None
    lines = [l for l in out.splitlines() if "/" in l and not l.startswith("Listing")]
    return len(lines), sum(1 for l in lines if "security" in l.split(" ")[0])


def fetch_json(url, headers=None, timeout=60):
    """(parsed body or None, reached). A 503 still carries a body (the
    gateway's degraded /health); any other failure is unreached."""
    req = urllib.request.Request(url, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode()), True
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode()), True
        except Exception:
            return None, False
    except Exception:
        return None, False


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
        rc, out = run(["docker", "ps", "--format", "{{.Names}} {{.Status}}"])
        o["containers"] = {l.split(" ", 1)[0]: l.split(" ", 1)[1] for l in out.splitlines() if " " in l} if rc == 0 else None
    o["units_system"] = {}
    for u in [u for u in cfg["UNITS_SYSTEM"].split(",") if u]:
        o["units_system"][u] = run(["systemctl", "is-active", u])[1].strip()
    o["units_user"] = {}
    for u in [u for u in cfg["UNITS_USER"].split(",") if u]:
        o["units_user"][u] = run(["systemctl", "--user", "is-active", u])[1].strip()
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
    o["feeder_age"], o["feeder_err_polls"] = None, None
    if cfg["FEEDER_LOG"]:
        o["feeder_age"] = mtime_age(cfg["FEEDER_LOG"], now)
        try:
            tail = run(["tail", "-n", cfg["FEEDER_ERR_POLLS"], cfg["FEEDER_LOG"]])[1].splitlines()
            errs = [int(l.split("errors=")[1].split()[0]) for l in tail if "errors=" in l]
            o["feeder_err_polls"] = sum(1 for e in errs if e > 0) if errs else None
        except Exception:
            o["feeder_err_polls"] = None
    o["endpoint_age"], o["endpoint_breaker"] = None, None
    if cfg["ENDPOINT_HEARTBEAT"]:
        o["endpoint_age"] = mtime_age(cfg["ENDPOINT_HEARTBEAT"], now)
        try:
            o["endpoint_breaker"] = [t.split("=", 1)[1] for t in open(cfg["ENDPOINT_HEARTBEAT"]).read().split() if t.startswith("breaker=")][0]
        except Exception:
            o["endpoint_breaker"] = None
    since = "@%d" % int(cursors.get("journal", now - 300))
    o["journal_errors"] = sum(len(run(["journalctl"] + scope + ["-p", "err", "--since", since, "-q", "--no-pager"])[1].splitlines())
                              for scope in ([], ["--user"]))
    o["ssh_failures"] = len(run(["journalctl", "-u", "ssh", "--since", since, "-q", "--no-pager", "-g",
                                 "Failed password|Invalid user|authentication failure|maximum authentication attempts"])[1].splitlines())
    # accepted ssh logins since the last run, by source address (zone suffix stripped)
    acc = run(["journalctl", "-u", "ssh", "--since", since, "-q", "--no-pager", "-g", "Accepted publickey"])[1]
    o["ssh_sources"] = sorted({l.split(" from ", 1)[1].split()[0].split("%")[0] for l in acc.splitlines() if " from " in l})
    # refused outbound since the last run: the host firewall logs "egress-drop*" at warn, rate-limited
    o["egress_drops"] = len(run(["journalctl", "-k", "--since", since, "-q", "--no-pager", "-g", "egress-drop"])[1].splitlines())
    # dhcp: hours until the lease expires (renewal happens at half-life, so under 6 h means a renewal was missed)
    o["dhcp_lease_left_h"] = None
    if cfg["DHCP_IFACE"]:
        rc, out = run(["nmcli", "-t", "-f", "DHCP4.OPTION", "dev", "show", cfg["DHCP_IFACE"]])
        for l in out.splitlines():
            if "expiry = " in l:
                try:
                    o["dhcp_lease_left_h"] = (int(l.rsplit("= ", 1)[1]) - now) / 3600.0
                except ValueError:
                    pass
    # tor: a heartbeat (every 6 h) or a bootstrap line in the window, its circuit count, and recent no-network warnings
    o["tor_alive_lines"], o["tor_circuits"], o["tor_net_warn"] = None, None, None
    if cfg["TOR_CONTAINER"]:
        rc, out = run(["docker", "logs", "--since", cfg["TOR_HB_MAX_H"] + "h", cfg["TOR_CONTAINER"]])
        if rc == 0:
            alive = [l for l in out.splitlines() if "Bootstrapped 100%" in l or "Heartbeat: Tor's uptime" in l]
            o["tor_alive_lines"] = len(alive)
            hb = [l for l in alive if "circuits open" in l]
            if hb:
                try:
                    o["tor_circuits"] = int(hb[-1].split("with ", 1)[1].split(" circuits")[0])
                except (IndexError, ValueError):
                    pass
            rc2, out2 = run(["docker", "logs", "--since", cfg["TOR_WARN_MIN"] + "m", cfg["TOR_CONTAINER"]])
            o["tor_net_warn"] = sum(1 for l in out2.splitlines() if "network activity" in l) if rc2 == 0 else None
    # bitcoind: established outbound p2p connections
    o["btc_peers"] = None
    if cfg["BTC_P2P_PORT"]:
        rc, out = run(["ss", "-Htn", "state", "established", "( dport = :%s )" % cfg["BTC_P2P_PORT"]])
        o["btc_peers"] = len(out.splitlines()) if rc == 0 else None
    o["anchors"], o["anchor_age_h"] = None, None
    if cfg["RECEIPTS"]:
        try:
            lines = [json.loads(l) for l in open(cfg["RECEIPTS"]) if l.strip()]
            o["anchors"] = len(lines)
            if lines:
                o["anchor_age_h"] = (now - lines[-1]["confirmed_at"]) / 3600.0
        except Exception:
            pass
    try:
        o["uptime_s"] = float(open("/proc/uptime").read().split()[0])
    except Exception:
        o["uptime_s"] = None
    return o


# ----------------------------------------------------------------------------- evaluate
def evaluate(o, cfg):
    """Every check -> (ok, detail). Detail is the short text an alert quotes.
    Checks whose knob is empty come out ok and are never counted (active_checks)."""
    c = {}
    c["health_reach"] = (o.get("health_reach") is True, "no answer from /health")
    h = o.get("health") or {}
    if o.get("health_reach"):
        bad = [f for f in ("payment", "otsd", "wallet", "float", "proofs", "backup", "billing") if h.get(f) not in
               (None, "ok", "unknown", "absent", "inactive", "local_only", "off", "n/a")]
        st = h.get("status")
        c["health"] = (st == "ok", "health=%s %s" % (st, " ".join("%s=%s" % (f, h.get(f)) for f in bad)))
    else:
        c["health"] = (True, "")   # reach already fails; do not double-count
    cal = o.get("calendar")
    if not o.get("calendar_reach"):
        c["calendar"] = (False, "no answer from the calendar")
    elif not isinstance(cal, dict) or not cal.get("best_block"):
        c["calendar"] = (False, "calendar is Bitcoin-blind")
    elif cal.get("anchor_receipts") != "on":
        c["calendar"] = (False, "calendar anchor receipts %s" % cal.get("anchor_receipts"))
    else:
        sats = parse_sats(cal.get("balance"))
        if sats is None:
            c["calendar"] = (False, "calendar balance unreadable")
        elif sats < int(cfg["CAL_MIN_SATS"]):
            c["calendar"] = (False, "anchor wallet %d sats < %s" % (sats, cfg["CAL_MIN_SATS"]))
        else:
            c["calendar"] = (True, "")
    cs = o.get("containers")
    if cs is None:
        c["containers"] = (False, "docker ps failed")
    else:
        down = [n for n in cfg["CONTAINERS"].split(",") if n and not cs.get(n, "").startswith("Up")]
        c["containers"] = (not down, "down: " + ",".join(down))
    for key in ("units_system", "units_user"):
        bad = [u for u, s in (o.get(key) or {}).items() if s != "active"]
        c[key] = (not bad, ",".join("%s=%s" % (u, (o.get(key) or {}).get(u)) for u in bad))
    for key, name in (("DISK_ROOT", "disk_root"), ("DISK_BOOT", "disk_boot")):
        pct = (o.get("disk") or {}).get(cfg[key])
        c[name] = (pct is not None and pct < float(cfg["DISK_PCT"]), "%s at %s%%" % (cfg[key], pct))
    t = o.get("temp_c")
    c["temp"] = (t is not None and t < float(cfg["TEMP_C"]), "temp %sC" % t)
    m = o.get("mem_avail_mb")
    c["mem"] = (m is not None and m >= int(cfg["MEM_MB"]), "mem available %sMB" % m)
    fa, fe = o.get("feeder_age"), o.get("feeder_err_polls")
    if fa is None:
        c["feeder"] = (False, "feeder log missing")
    elif fa > float(cfg["FEEDER_STALE_S"]):
        c["feeder"] = (False, "feeder log stale %dm" % (fa // 60))
    elif fe is not None and fe >= int(cfg["FEEDER_ERR_POLLS"]):
        c["feeder"] = (False, "feeder last %d polls all with errors" % fe)
    else:
        c["feeder"] = (True, "")
    ea, eb = o.get("endpoint_age"), o.get("endpoint_breaker")
    if ea is None:
        c["endpoint"] = (False, "endpoint heartbeat missing")
    elif ea > float(cfg["ENDPOINT_STALE_S"]):
        c["endpoint"] = (False, "endpoint heartbeat stale %dm" % (ea // 60))
    elif eb not in (None, "ok"):
        c["endpoint"] = (False, "endpoint breaker=%s" % eb)
    else:
        c["endpoint"] = (True, "")
    je = o.get("journal_errors") or 0
    c["journal_errors"] = (je <= int(cfg["JOURNAL_ERRORS"]), "%d journal errors since last check" % je)
    sf = o.get("ssh_failures") or 0
    c["ssh_failures"] = (sf <= int(cfg["SSH_FAILURES"]), "%d ssh auth failures since last check" % sf)
    ah = o.get("anchor_age_h")
    c["anchor_age"] = (ah is None or ah <= float(cfg["ANCHOR_MAX_H"]), "last anchor %.0fh ago" % (ah or 0))
    kr, kn = o.get("kernel_running"), o.get("kernel_newest")
    if o.get("reboot_required_file"):
        c["reboot_wanted"] = (False, "reboot wanted: /run/reboot-required is set")
    elif kr and kn and kr != kn:
        c["reboot_wanted"] = (False, "reboot wanted: kernel %s installed, running %s" % (kn.split("+")[0], kr.split("+")[0]))
    else:
        c["reboot_wanted"] = (True, "")
    ed = o.get("egress_drops") or 0
    c["egress_drops"] = (ed <= int(cfg["EGRESS_DROPS"]), "%d refused outbound packets since last check" % ed)
    dl = o.get("dhcp_lease_left_h")
    c["dhcp_lease"] = (dl is None or dl >= float(cfg["DHCP_MIN_H"]), "dhcp lease expires in %.1fh, renewal missed" % (dl or 0))
    ta, tc, tw = o.get("tor_alive_lines"), o.get("tor_circuits"), o.get("tor_net_warn") or 0
    if ta is None:
        c["tor_circuits"] = (False, "tor log unreadable")
    elif ta == 0:
        c["tor_circuits"] = (False, "no tor heartbeat in %sh" % cfg["TOR_HB_MAX_H"])
    elif tc == 0:
        c["tor_circuits"] = (False, "tor reports 0 circuits open")
    elif tw > 0:
        c["tor_circuits"] = (False, "tor: %d no-network-activity warnings in %sm" % (tw, cfg["TOR_WARN_MIN"]))
    else:
        c["tor_circuits"] = (True, "")
    known = [k.strip() for k in cfg.get("SSH_KNOWN_SOURCES", "").split(",") if k.strip()]
    unexpected = [a for a in (o.get("ssh_sources") or []) if not source_known(a, known)]
    c["ssh_unexpected"] = (not unexpected, "ssh accepted from unexpected source %s" % ",".join(unexpected))
    bp = o.get("btc_peers")
    c["btc_peers"] = (bp is not None and bp >= int(cfg["BTC_MIN_PEERS"]), "%s bitcoind peers established" % bp)
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
    joined, left = [], []
    for check in active:
        ok, detail = checks.get(check, (True, ""))
        need = 1 if check in BURST_CHECKS else confirm
        if ok:
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
    still = [n for n in active if n in delivered and n not in joined]
    if joined:
        what = "; ".join(checks[n][1] for n in joined)
        rest = "; ".join(checks[n][1] for n in still) if still else "none"
        msgs.append("%s DEGRADED: %s | still: %s" % (name, what, rest))
    if left and not delivered:
        dur = max(now - s["since"].get(n, now) for n in left)
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
    active = active_checks(cfg)
    name = box_name(cfg)
    bad = [n for n in active if n in s.get("delivered", [])]
    head = "%s heartbeat: ok %d/%d" % (name, len(active), len(active)) if not bad else \
        "%s heartbeat: still degraded: " % name + "; ".join(checks[n][1] for n in bad)
    disk = o.get("disk") or {}
    vit = "disk %s%% boot %s%% temp %sC mem %sMB" % (disk.get("/", "?"), disk.get("/boot/firmware", "?"),
                                                    o.get("temp_c", "?"), o.get("mem_avail_mb", "?"))
    anch = "anchors %s, last %s ago" % (o.get("anchors", "?"), fmt_dur((o.get("anchor_age_h") or 0) * 3600))
    cal = o.get("calendar") if isinstance(o.get("calendar"), dict) else None
    if cal is not None:
        anch += ", wallet %s sats, pending %s" % (cal.get("balance", "?"), cal.get("pending_commitments", "?"))
    up = "up %s" % fmt_dur(o.get("uptime_s") or 0)
    upd = "updates: %s pending (%s security)" % (o.get("updates_total", "?"), o.get("updates_security", "?"))
    drops = "egress drops %s" % o.get("drops_acc", o.get("egress_drops", "?"))
    return " | ".join([head, vit, anch, upd, drops, up])


def status_line(now, s, checks, cfg):
    active = active_checks(cfg)
    bad = [n for n in active if not checks[n][0]]
    word = "degraded" if bad else "ok"
    ts = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return "%s %s %d/%d%s" % (ts, word, len(active) - len(bad), len(active),
                              "".join(" %s=%s" % (n, checks[n][1].replace(" ", "_")) for n in bad))


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


def real_run(dry):
    cfg = load_config()
    os.makedirs(WATCH_DIR, exist_ok=True)
    now = time.time()
    state = {}
    if os.path.exists(STATE):
        with open(STATE) as fd:
            state = json.load(fd)
    cursors = state.get("cursors", {})
    utc = datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
    want_updates = state.get("heartbeat_day") != utc.strftime("%Y-%m-%d") and utc.hour >= int(cfg["HEARTBEAT_HOUR"])
    o = observe(cfg, now, cursors, want_updates=want_updates or dry)
    checks = evaluate(o, cfg)
    acc = int(state.get("drops_acc", 0)) + int(o.get("egress_drops") or 0)
    new_state, msgs = decide(state, checks, now, cfg)
    if new_state.pop("heartbeat_pending", False):
        o["drops_acc"] = acc
        msgs.append(heartbeat_line(new_state, checks, o, cfg))
        acc = 0
    new_state["drops_acc"] = acc
    line = status_line(now, new_state, checks, cfg)
    with open(STATUS + ".tmp", "w") as fd:
        fd.write(line + "\n")
    os.replace(STATUS + ".tmp", STATUS)
    log(line)
    delivered_all = True
    for m in msgs:
        if dry:
            log("WOULD SEND: " + m)
            continue
        ok, why = send(cfg, m)
        log(("sent: " if ok else "NOT SENT (%s): " % why) + m)
        delivered_all = delivered_all and ok
    if dry:
        return 0
    if delivered_all:
        new_state["cursors"] = {"journal": now}
        with open(STATE + ".tmp", "w") as fd:
            json.dump(new_state, fd, indent=1)
        os.replace(STATE + ".tmp", STATE)
    else:
        # keep the old state so the next run retries the same transition; advance only the cursor
        state["cursors"] = {"journal": now}
        with open(STATE + ".tmp", "w") as fd:
            json.dump(state, fd, indent=1)
        os.replace(STATE + ".tmp", STATE)
    return 0


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
