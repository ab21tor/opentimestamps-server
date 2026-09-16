# Copyright (C) 2016-2017 The OpenTimestamps developers
#
# This file is part of the OpenTimestamps Server.
#
# It is subject to the license terms in the LICENSE file found in the top-level
# directory of this distribution.
#
# No part of the OpenTimestamps Server including this file, may be copied,
# modified, propagated, or distributed except according to the terms contained
# in the LICENSE file.

import binascii
import http.server
import json
import logging
import os
import socketserver
import sys
import threading
from http import HTTPStatus

from bitcoin.core import b2lx, b2x

from otsserver.stamper import make_proxy
import otsserver
from opentimestamps.core.serialize import BytesSerializationContext


def operator_lane_enabled():
    """True when OTSD_OPERATOR_LANE=1: POST /operator/digest is served

    Unset (the default) the path is an ordinary 404 and the server behaves
    exactly as before. See the "Operator lane" section of the README.
    """
    return os.getenv('OTSD_OPERATOR_LANE') == '1'


class RPCRequestHandler(http.server.BaseHTTPRequestHandler):
    MAX_DIGEST_LENGTH = 64
    """Largest digest that can be POSTed for timestamping"""

    # Socket timeout for every read on a connection: a peer that opens a
    # connection and never sends (or never finishes) its request no longer
    # pins a handler thread until it closes (full review N15, 2026-09-08).
    timeout = 60

    digest_queue = None

    # What the calendar logs about a request: a fixed route token and the
    # status code, never the peer's address and never the request line
    # (2026-09-15 review, P2 "privacy promise fails on error paths"). The
    # base class writes both to stderr, where docker/journald keeps them;
    # an unknown path can carry record content, a peer address is a client
    # identity. log_message is the base class's one sink, so every path
    # through it (log_request from send_response, log_error from
    # send_error on a malformed request, the timeout message) is closed.
    ROUTES = {'/': 'status', '/digest': 'digest', '/operator/digest': 'operator-digest', '/tip': 'tip'}

    def route_name(self):
        path = (getattr(self, 'path', '') or '').split('?', 1)[0]
        if path.startswith('/timestamp/'):
            return 'timestamp'
        return self.ROUTES.get(path, 'other')

    def log_request(self, code='-', size='-'):
        if isinstance(code, HTTPStatus):
            code = code.value
        line = "request route=%s status=%s" % (self.route_name(), code)
        if isinstance(code, int) and code >= 400:
            logging.info(line)
        else:
            logging.debug(line)

    def log_error(self, format, *args):
        # send_error's message quotes the request ("Bad request version
        # (%r)"): only the status code is kept.
        code = next((a.value if isinstance(a, HTTPStatus) else a for a in args if isinstance(a, int)), '-')
        logging.info("request refused route=%s status=%s" % (self.route_name(), code))

    def log_message(self, format, *args):
        pass

    def post_digest(self, counted=True):
        """Aggregate one digest; counted=False is the operator lane (do_POST)"""
        content_length = self.headers['Content-Length']

        # Might be missing or otherwise invalid
        try:
            content_length = int(content_length)
        except (TypeError, ValueError):
            self.send_response(400)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'invalid Content-Length')
            return

        if content_length < 1:
            self.send_response(400)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'invalid digest length')
            return

        if content_length > self.MAX_DIGEST_LENGTH:
            self.send_response(400)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'digest too long')
            return

        digest = self.rfile.read(content_length)

        try:
            timestamp = self.aggregator.submit(digest, counted=counted)
        except otsserver.calendar.AggregatorUnavailable as exp:
            # The loop is gone or wedged (calendar.Aggregator): the digest
            # was not committed. A 503 the client retries beats a request
            # that never answers.
            logging.warning("digest refused, %s" % exp)
            self.send_response(503)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Retry-After', '5')
            self.end_headers()
            self.wfile.write(b'aggregator unavailable')
            return

        ctx = BytesSerializationContext()
        timestamp.serialize(ctx)
        serialized_timestamp = ctx.getbytes()

        self.send_response(200)
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Content-Length', len(serialized_timestamp))
        self.end_headers()

        self.wfile.write(serialized_timestamp)

    def get_tip(self):
        try:
            msg = self.calendar.stamper.unconfirmed_txs[-1].tip_timestamp.msg
        except:
            self.send_response(404)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            return

        if msg is not None:
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Cache-Control', 'public, max-age=10')
            self.end_headers()
            self.wfile.write(msg)
        else:
            self.send_response(204)
            self.send_header('Cache-Control', 'public, max-age=10')
            self.end_headers()

    def get_timestamp(self):
        commitment = self.path[len('/timestamp/'):]

        try:
            commitment = binascii.unhexlify(commitment)
        except binascii.Error:
            self.send_response(400)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Cache-Control', 'public, max-age=31536000') # this will never not be an error!
            self.end_headers()
            self.wfile.write(b'commitment must be hex-encoded bytes')
            return

        try:
            timestamp = self.calendar[commitment]
        except KeyError:
            self.send_response(404)
            self.send_header('Content-Type', 'text/plain')

            # Pending?
            reason = self.calendar.stamper.is_pending(commitment)
            if reason:
                reason = reason.encode()

                # The commitment is pending, so its status will change soonish
                # as blocks are found.
                self.send_header('Cache-Control', 'public, max-age=60')

            else:
                # The commitment isn't in this calendar at all. Clients only
                # get specific commitments from servers, so in the current
                # implementation there's no reason why this response would ever
                # change.
                #
                # FIXME: unfortunately, this isn't actually true, as the
                # stamper may return `Not Found` for a commitment that was just
                # added, as commitments aren't actually added directly to the
                # pending data structure, but rather, added to the journal and
                # only then added to pending. So for now, set a reasonably
                # short cache control header.
                #
                # See https://github.com/opentimestamps/opentimestamps-server/issues/10
                # for more info.
                self.send_header('Cache-Control', 'public, max-age=60')
                reason = b'Not found'

            self.end_headers()
            self.wfile.write(reason)
            return

        self.send_response(200)

        ctx = BytesSerializationContext()
        timestamp.serialize(ctx)
        serialized_timestamp = ctx.getbytes()

        # Since only Bitcoin attestations are currently made, once a commitment
        # is timestamped by Bitcoin this response will never change.
        self.send_header('Cache-Control', 'public, max-age=31536000')

        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Content-Length', len(serialized_timestamp))
        self.end_headers()

        self.wfile.write(serialized_timestamp)

    def do_POST(self):
        if self.path == '/digest':
            self.post_digest()

        elif self.path == '/operator/digest' and operator_lane_enabled():
            # Operator lane: the box's own diary (ops/selfstamp.py) rides
            # the same per-second trees and anchors as client digests, but
            # its leaves are not records — they never reach a receipt or a
            # bill. Off unless OTSD_OPERATOR_LANE=1, when this is the 404.
            self.post_digest(counted=False)

        else:
            self.send_response(404)
            self.send_header('Content-Type', 'text/plain')

            # a 404 is only going to become not a 404 if the server is upgraded
            self.send_header('Cache-Control', 'public, max-age=3600')

            self.end_headers()
            self.wfile.write(b'not found')

    def get_status(self):
        """GET /: the calendar's status as one JSON line

        This is what the watcher (ops/watch.py), the self-stamp's float
        reader (ops/selfstamp.py) and the gateway's /health read, so its
        fields are an interface: best_block (display hex, null when the
        Bitcoin RPC path is down: the one external proof that it is alive),
        block_height, balance (the anchor wallet's confirmed sats, null
        when unreachable), anchor_receipts ("on"/"off": a calendar restarted
        with receipts off cannot anchor for free unnoticed),
        needs_attention (the deep-reorg detector's findings, empty when
        every checked anchor is where its receipt says), plus the pending
        queue and the in-flight anchor. Built in full before the response
        is committed, so a failure is a status that says so, never an
        empty 200. The donation homepage this replaced (2026-09-11) is
        gone with its qrcode/pystache/simplejson dependencies.
        """
        stamper = self.calendar.stamper
        status = {
            'version': otsserver.__version__,
            'pending_commitments': len(stamper.pending_commitments),
            'txs_waiting_for_confirmation': len(stamper.txs_waiting_for_confirmation),
            'most_recent_tx': b2lx(stamper.unconfirmed_txs[-1].tx.GetTxid()) if stamper.unconfirmed_txs else None,
            'prior_versions': max(0, len(stamper.unconfirmed_txs) - 1),
            'tip': b2x(stamper.unconfirmed_txs[-1].tip_timestamp.msg) if stamper.unconfirmed_txs else None,
            'best_block': None,
            'block_height': None,
            'balance': None,
            'anchor_receipts': 'on' if getattr(stamper, 'anchor_receipts_path', None) else 'off',
            'needs_attention': list(getattr(stamper, 'needs_attention', ())),
        }
        try:
            # Per-op socket timeout (connect + each recv): a hung-transport
            # detector, not a render budget. Must clear the Tor bridge's
            # TTFB (~1-2s) and its transient circuit stalls, which cut ~11%
            # of renders when this sat at 5s (2026-07-17). Paired with the
            # gateway health probe's read timeout (timestamp-gateway
            # main.py, timeout=(5, 45)), which must outlast a full status:
            # change the two together.
            proxy = make_proxy(timeout=30)
            status['best_block'] = b2lx(proxy.getbestblockhash())
            status['block_height'] = proxy.getblockcount()
            # minconf=1 underestimates while timestamp txs are pending, but
            # never counts coins an unconfirmed tx has tied up.
            status['balance'] = proxy.getbalance(minconf=1)
        except Exception as err:
            logging.error("status: bitcoin RPC failed: %r" % err, exc_info=True)

        body = (json.dumps(status) + '\n').encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        # Refreshed by pollers, so keep it current; 5s so a cache is still hit.
        self.send_header('Cache-Control', 'public, max-age=5')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/':
            self.get_status()

        elif self.path.startswith('/timestamp/'):
            self.get_timestamp()
        elif self.path == '/tip':
            self.get_tip()
        # Upstream's /experimental/backup/ is gone from this fork (2026-09-08):
        # it served calendar data unauthenticated, and the replication
        # tooling behind it (otsd-backup.py, otsserver/backup.py) ran nowhere.
        else:
            self.send_response(404)
            self.send_header('Content-Type', 'text/plain')

            # a 404 is only going to become not a 404 if the server is upgraded
            self.send_header('Cache-Control', 'public, max-age=3600')

            self.end_headers()
            self.wfile.write(b'Not found')


class StampServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    # Handler threads are daemons: a shutdown on a worker failure
    # (serve_until_exit) must not wait on a request that is mid-flight.
    daemon_threads = True

    def __init__(self, server_address, aggregator, calendar):

        class rpc_request_handler(RPCRequestHandler):
            pass
        rpc_request_handler.aggregator = aggregator
        rpc_request_handler.calendar = calendar

        super().__init__(server_address, rpc_request_handler)

    def attach_aggregator(self, aggregator):
        """otsd binds the listener before it starts the workers, so the
        aggregator arrives after the bind (2026-09-15/16 review F19: a
        bind failure used to leave the non-daemon worker threads alive
        behind no port). Nothing is served until serve_until_exit starts
        the serving thread, so no request can see it missing."""
        self.RequestHandlerClass.aggregator = aggregator

    def handle_error(self, request, client_address):
        # The base class prints "Exception occurred during processing of
        # request from ('IP', port)" and a traceback to stderr. Never the
        # peer: the exception class alone.
        exc = sys.exc_info()[1]
        logging.error("request handler failed: %s" % type(exc).__name__)

    def serve_forever(self):
        super().serve_forever()


def serve_until_exit(server, exit_event, workers=(), poll=0.5, join_timeout=10):
    """Serve HTTP until exit_event is set, then shut the server down

    A worker (the aggregator, the stamper) that fails past recovery sets
    exit_event; before 2026-09-15 nothing consumed it while the listener
    kept serving, so a dead worker sat behind a live port and the
    supervisor saw nothing to restart. Now the listener stops, the socket
    closes, the workers are joined (bounded) and 1 is returned: the
    process exits nonzero and the supervisor restarts it. A
    KeyboardInterrupt performs the same shutdown and propagates to the
    caller (otsd exits 0 on it).
    """
    thread = threading.Thread(target=server.serve_forever, name='http', daemon=True)
    thread.start()
    try:
        while not exit_event.wait(poll):
            pass
    finally:
        server.shutdown()
        server.server_close()
        thread.join(join_timeout)
        for worker in workers:
            worker.join(join_timeout)
    return 1

