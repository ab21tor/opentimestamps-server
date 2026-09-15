#!/usr/bin/env python3
"""Export the node's block headers to headers.bin for the claim kit; stdlib only.

    python3 export_headers.py --out ~/claim-kit/headers.bin --env ~/opentimestamps-server/.env --rpc-host 127.0.0.1
    python3 export_headers.py --out PATH --rpc-url http://user:pass@127.0.0.1:8332/

headers.bin is every block header from genesis, 80 bytes each, height =
offset / 80: what ops/verify_claim.py reads. One run appends what the node
has beyond the file (a first run writes the whole chain, ~77 MB, in
batched RPC calls); daily on a timer (ops/systemd/headers.timer). Before
anything is written, each new header must link to the last one on file,
pass Bitcoin Core's target rules and meet its own proof-of-work target,
and its bits must not move between retargets; a header that fails is
refused and the run exits 1 with the file untouched. A tail the node no
longer agrees with (a reorg) is cut back to the last common header and
re-exported, and logged.

One writer at a time: the run holds an exclusive lock (<out>.lock, flock,
across processes) and a second run, the timer's or a manual one, is
refused with exit 1. Every byte is written by a checked loop (os.write
may write less than asked: a short write used to be reported as a whole
batch), the file is fsynced after each batch and its directory after the
run. A file that ends in part of a header (an interrupted append) is cut
back to its last whole header and the run goes on from there, logged;
nothing is ever repaired by hand.

The node access is the calendar's own: BITCOIN_RPC_SERVICE_URL from the
compose .env (--env), whose host.docker.internal is the container's name
for this host, so --rpc-host 127.0.0.1 replaces it when run on the host.
The RPCs used (getblockcount, getblockhash, getblockheader) are in the
rpcwhitelist the README's bitcoin.conf grants the calendar's RPC user.

Which chain this is, the exporter does not judge: the verifier's genesis
check and the expert's checkpoint comparison do.
"""

import argparse
import base64
import errno
import fcntl
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from verify_claim import HEADER_SIZE, NETWORKS, RETARGET_INTERVAL, check_target, display, header_bits, header_fields, sha256d  # noqa: E402


def rpc_batch(url, calls):
    """One JSON-RPC batch: calls is [(method, params)]; returns results in order"""
    split = urllib.parse.urlsplit(url)
    clean = urllib.parse.urlunsplit((split.scheme, split.hostname + (':%d' % split.port if split.port else ''),
                                     split.path or '/', split.query, ''))
    body = json.dumps([{'jsonrpc': '1.0', 'id': i, 'method': m, 'params': list(p)} for i, (m, p) in enumerate(calls)]).encode()
    headers = {'Content-Type': 'application/json'}
    if split.username is not None:
        token = base64.b64encode(('%s:%s' % (urllib.parse.unquote(split.username),
                                             urllib.parse.unquote(split.password or ''))).encode()).decode()
        headers['Authorization'] = 'Basic ' + token
    request = urllib.request.Request(clean, data=body, headers=headers, method='POST')
    with urllib.request.urlopen(request, timeout=120) as response:
        replies = json.loads(response.read().decode())
    by_id = {r['id']: r for r in replies}
    out = []
    for i in range(len(calls)):
        reply = by_id.get(i)
        if reply is None or reply.get('error'):
            raise RuntimeError('rpc %s: %r' % (calls[i][0], (reply or {}).get('error', 'no reply')))
        out.append(reply['result'])
    return out


def rpc_url_from_env(path, host=None):
    with open(path) as fd:
        for line in fd:
            line = line.strip()
            if line.startswith('BITCOIN_RPC_SERVICE_URL='):
                url = line.split('=', 1)[1].strip().strip('"\'')
                if host:
                    split = urllib.parse.urlsplit(url)
                    netloc = split.netloc.replace(split.hostname, host)
                    url = urllib.parse.urlunsplit((split.scheme, netloc, split.path, split.query, split.fragment))
                return url
    raise SystemExit('no BITCOIN_RPC_SERVICE_URL in %s' % path)


def node_hashes(url, heights, batch):
    out = []
    for i in range(0, len(heights), batch):
        out += rpc_batch(url, [('getblockhash', [h]) for h in heights[i:i + batch]])
    return out


def write_all(fd, data):
    """Every byte, or an error: os.write may write less than it was given."""
    view = memoryview(data)
    while len(view):
        n = os.write(fd, view)
        if n <= 0:
            raise OSError(errno.EIO, 'write made no progress')
        view = view[n:]


def fsync_dir(path):
    fd = os.open(os.path.dirname(os.path.abspath(path)) or '.', os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def check_header(header, height, prev_hash, prev_bits, network='mainnet'):
    """None if the header links, meets Bitcoin Core's target rules and its
    own proof of work, and keeps its bits between retargets; else the reason"""
    fields = header_fields(header)
    if prev_hash is not None and fields['prev'] != prev_hash:
        return 'height %d does not link to the header before it' % height
    try:
        target = check_target(fields['bits'], network)
    except ValueError as exp:
        return 'height %d: invalid target (%s; bits 0x%08x)' % (height, exp, fields['bits'])
    if int.from_bytes(sha256d(header), 'little') > target:
        return 'height %d fails proof of work' % height
    if NETWORKS[network]['retarget'] and prev_bits is not None and height % RETARGET_INTERVAL and fields['bits'] != prev_bits:
        return 'height %d changes bits between retargets' % height
    return None


def main(argv=None, out=None):
    out = out or sys.stdout

    def log(msg):
        out.write('%s %s\n' % (time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), msg))
        out.flush()

    parser = argparse.ArgumentParser(description='Export block headers to headers.bin for the claim kit.')
    parser.add_argument('--out', required=True, help='headers.bin to create or extend')
    parser.add_argument('--rpc-url', help='http://user:pass@host:port/ of bitcoind')
    parser.add_argument('--env', help='compose .env holding BITCOIN_RPC_SERVICE_URL')
    parser.add_argument('--rpc-host', help='replace the URL\'s host (host.docker.internal -> 127.0.0.1 on the host)')
    parser.add_argument('--batch', type=int, default=500, help='RPC calls per batch (default 500)')
    parser.add_argument('--network', choices=sorted(NETWORKS), default='mainnet',
                        help='the chain the node is on (default mainnet); regtest only for easy-difficulty test chains')
    args = parser.parse_args(argv)
    if bool(args.rpc_url) == bool(args.env):
        parser.error('give --rpc-url or --env')
    url = args.rpc_url or rpc_url_from_env(args.env, args.rpc_host)
    if args.rpc_url and args.rpc_host:
        split = urllib.parse.urlsplit(url)
        url = urllib.parse.urlunsplit((split.scheme, split.netloc.replace(split.hostname, args.rpc_host),
                                       split.path, split.query, split.fragment))

    # One writer at a time, across processes: the lock goes with the
    # descriptor, so a run that dies releases it.
    lock_path = args.out + '.lock'
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log('refused: another export holds %s' % lock_path)
            return 1
        return _export(args, url, log)
    finally:
        os.close(lock_fd)


def _export(args, url, log):
    # What is on file, and whether the node still agrees with its tail. A
    # file that ends in part of a header (an interrupted append) is cut
    # back to its last whole header first.
    have = os.path.getsize(args.out) // HEADER_SIZE if os.path.exists(args.out) else 0
    if os.path.exists(args.out) and os.path.getsize(args.out) % HEADER_SIZE:
        over = os.path.getsize(args.out) % HEADER_SIZE
        with open(args.out, 'r+b') as trunc:
            trunc.truncate(have * HEADER_SIZE)
            trunc.flush()
            os.fsync(trunc.fileno())
        log('recovered: %s held %d bytes of an incomplete header after %d whole ones (an interrupted append); dropped'
            % (args.out, over, have))
    tip, = rpc_batch(url, [('getblockcount', [])])
    prev_hash = None
    prev_bits = None
    if have:
        with open(args.out, 'rb') as fd:
            keep = have
            # Walk back from the tail until the node's hash at that height matches.
            while keep > 0:
                fd.seek((keep - 1) * HEADER_SIZE)
                last = fd.read(HEADER_SIZE)
                node_hash = node_hashes(url, [keep - 1], args.batch)[0] if keep - 1 <= tip else None
                if node_hash == display(sha256d(last)):
                    break
                keep -= 1
            if keep < have:
                log('reorg: the node no longer holds the file\'s headers from height %d; dropping %d header(s) and re-exporting'
                    % (keep, have - keep))
                fd.close()
                with open(args.out, 'r+b') as trunc:
                    trunc.truncate(keep * HEADER_SIZE)
                    trunc.flush()
                    os.fsync(trunc.fileno())
                have = keep
            if have:
                with open(args.out, 'rb') as fd2:
                    fd2.seek((have - 1) * HEADER_SIZE)
                    last = fd2.read(HEADER_SIZE)
                prev_hash = sha256d(last)
                prev_bits = header_bits(last)

    appended = 0
    height = have
    while height <= tip:
        heights = list(range(height, min(tip, height + args.batch - 1) + 1))
        hashes = node_hashes(url, heights, args.batch)
        raws = rpc_batch(url, [('getblockheader', [h, False]) for h in hashes])
        chunk = bytearray()
        for h, raw in zip(heights, raws):
            header = bytes.fromhex(raw)
            if len(header) != HEADER_SIZE:
                log('refused: the node answered %d bytes for height %d' % (len(header), h))
                return 1
            reason = check_header(header, h, prev_hash, prev_bits, args.network)
            if reason:
                log('refused: %s; nothing from this batch written' % reason)
                return 1
            chunk += header
            prev_hash = sha256d(header)
            prev_bits = header_bits(header)
        fd = os.open(args.out, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            write_all(fd, bytes(chunk))
            os.fsync(fd)
        except OSError as exp:
            log('refused: writing %d headers to %s failed (%s); the file may end in an incomplete header, which the '
                'next run cuts back' % (len(heights), args.out, exp))
            return 1
        finally:
            os.close(fd)
        appended += len(heights)
        height = heights[-1] + 1
    if appended:
        fsync_dir(args.out)
    log('appended %d headers to %s; tip height %d hash %s; %d headers on file'
        % (appended, args.out, tip, display(prev_hash) if prev_hash else '-', os.path.getsize(args.out) // HEADER_SIZE))
    return 0


if __name__ == '__main__':
    sys.exit(main())
