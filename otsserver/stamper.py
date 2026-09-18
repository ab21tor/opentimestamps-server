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

import collections
import errno
import json
import logging
import os
import threading
import time
import random
import bitcoin.rpc

_BITCOIN_RPC_SERVICE_URL = os.getenv("BITCOIN_RPC_SERVICE_URL")

def make_proxy(timeout=120):
    # Explicit RPC timeout: RPC calls that outlive bitcoinlib's default HTTP
    # timeout surface as RemoteDisconnected noise in the calendar's logs.
    if _BITCOIN_RPC_SERVICE_URL:
        return bitcoin.rpc.Proxy(service_url=_BITCOIN_RPC_SERVICE_URL, timeout=timeout)
    return bitcoin.rpc.Proxy(timeout=timeout)

from bitcoin.core import COIN, b2lx, b2x, x, lx, CTxIn, CTxOut, COutPoint, CTransaction, str_money_value
from bitcoin.core.script import CScript, OP_RETURN

from opentimestamps.bitcoin import cat_sha256d
from opentimestamps.core.notary import BitcoinBlockHeaderAttestation
from opentimestamps.core.op import OpPrepend, OpSHA256
from opentimestamps.core.timestamp import Timestamp, make_merkle_tree

from otsserver.calendar import Journal, RecordCounts, error_text, fsync_dir, read_checkpoint, write_checkpoint

# https://github.com/bitcoin/bitcoin/blob/master/src/policy/policy.cpp
DUST = 330

KnownBlock = collections.namedtuple('KnownBlock', ['height', 'hash'])
# records: digest submissions summed over this tx's tree at its close; 0
# when anchor receipts are off or the counts are unknown (never overcount).
TimestampTx = collections.namedtuple('TimestampTx', ['tx', 'tip_timestamp', 'commitment_timestamps', 'fee', 'height', 'records'], defaults=[0])
UnconfirmedTimestampTx = collections.namedtuple('TimestampTx', ['tx', 'tip_timestamp', 'n', 'fee', 'records'], defaults=[0])


def make_btc_block_merkle_tree(blk_txids):
    assert len(blk_txids) > 0

    digests = blk_txids
    while len(digests) > 1:
        # The famously broken Satoshi algorithm: if the # of digests at this
        # level is odd, double the last one.
        if len(digests) % 2:
            digests.append(digests[-1].msg)

        next_level = []
        for i in range(0, len(digests), 2):
            next_level.append(cat_sha256d(digests[i], digests[i + 1]))

        digests = next_level

    return digests[0]


def make_timestamp_from_block_tx(confirmed_tx, block, blockheight):

    commitment_tx = confirmed_tx.tx
    serialized_tx = commitment_tx.serialize(params={'include_witness': False})
    digest = confirmed_tx.tip_timestamp.msg

    try:
        i = serialized_tx.index(digest)
    except ValueError:
        assert False, "can't build a block_timestamp from my tx, this is not supposed to happen, exiting"

    prefix = serialized_tx[0:i]
    suffix = serialized_tx[i + len(digest):]

    digest_timestamp = Timestamp(digest)

    # Add the commitment ops necessary to go from the digest to the txid op
    prefix_stamp = digest_timestamp.ops.add(OpPrepend(prefix))
    txid_stamp = cat_sha256d(prefix_stamp, suffix)

    assert commitment_tx.GetTxid() == txid_stamp.msg

    # Create the txid list, with our commitment txid op in the appropriate
    # place
    block_txid_stamps = []
    for tx in block.vtx:
        if tx.GetTxid() != txid_stamp.msg:
            block_txid_stamps.append(Timestamp(tx.GetTxid()))
        else:
            block_txid_stamps.append(txid_stamp)

    # Build the merkle tree
    merkleroot_stamp = make_btc_block_merkle_tree(block_txid_stamps)
    assert merkleroot_stamp.msg == block.hashMerkleRoot

    attestation = BitcoinBlockHeaderAttestation(blockheight)
    merkleroot_stamp.attestations.add(attestation)

    return digest_timestamp


class OrderedSet(collections.OrderedDict):
    def add(self, item):
        self[item] = ()

    def remove(self, item):
        self.pop(item)

class KnownBlocks:
    """Maintain a list of known blocks"""

    def __init__(self):
        self.__blocks = []

    def __detect_reorgs(self, proxy):
        """Detect reorgs, rolling back if needed"""
        while self.__blocks:
            try:
                actual_blockhash = proxy.getblockhash(self.__blocks[-1].height)

                if actual_blockhash == self.__blocks[-1].hash:
                    break
            except IndexError:
                # rollback!
                pass

            logging.info("Reorg detected at height %d, rolling back block %s"
                         % (self.__blocks[-1].height, b2lx(self.__blocks[-1].hash)))
            self.__blocks.pop(-1)

    def update_from_proxy(self, proxy):
        """Update from an RPC proxy

        Returns a list of new block heights, hashes
        """
        r = []
        while not self.__blocks or proxy.getbestblockhash() != self.__blocks[-1].hash:
            self.__detect_reorgs(proxy)

            height = self.__blocks[-1].height + 1 if self.__blocks else proxy.getblockcount()

            try:
                hash = proxy.getblockhash(height)
            except IndexError:
                continue

            self.__blocks.append(KnownBlock(height, hash))
            r.append(self.__blocks[-1])

        return r

    def best_block_height(self):
        return self.__blocks[-1].height if self.__blocks else 0

    def best_block_hash(self):
        """The newest known block's hash (internal byte order), or None"""
        return self.__blocks[-1].hash if self.__blocks else None


def _get_tx_fee(tx, proxy):
    """Calculate tx fee

    Assumes inputs are confirmed
    """
    value_in = 0
    for txin in tx.vin:
        try:
            r = proxy.gettxout(txin.prevout, False)
        except IndexError:
            return None
        value_in += r['txout'].nValue

    value_out = sum(txout.nValue for txout in tx.vout)
    return value_in - value_out


def marker_path(receipts_path, txid=None):
    """The pending-receipt marker beside the receipts file, one per anchor:
    `<receipts>.pending.<txid>`. An earlier anchor's marker is never
    touched by a later anchor: each is settled on its own and removed only
    by the code that wrote its receipt or found it not owed (2026-09-15/16
    review F08: one shared name, and a later anchor's success unlinked an
    earlier anchor's still-owed marker). Without a txid: the single name
    used before 2026-09-16, which pending_markers still finds."""
    return receipts_path + '.pending' + ('.' + txid if txid else '')


def marker_role(path):
    """How a marker is named in a message: by its role and its anchor, never
    by its file name, which is made from the receipts file's name and can
    say who the calendar is run for (2026-09-18 gate review, G3). The
    anchor is named only when the suffix is a txid, 64 hex characters, as
    pending_markers requires; a receipts file whose own name holds
    `.pending.` gives its single-name marker a suffix of another kind
    (corrections review, G3b)."""
    base, _, suffix = os.path.basename(path).rpartition('.pending.')
    if base and len(suffix) == 64 and all(c in '0123456789abcdef' for c in suffix):
        return 'the pending receipt marker of anchor %s' % suffix
    return 'the pending receipt marker under the old single name'


def anchor_probe(txid):
    """The key that says an anchor's own save happened: its txid, as the
    saved tree holds it (make_timestamp_from_block_tx puts the txid node on
    the path from every commitment to the block). A commitment would
    answer for any anchor that carried it, and the same commitments are
    anchored again after a save that did not happen (2026-09-18 gate
    review, G1: a marker whose discard had failed was later satisfied by
    the next anchor's save of the same commitments, and billed twice)."""
    return lx(txid)


def pending_markers(receipts_path):
    """Every marker on file beside the receipts file, by name: the single
    old name first if present, then one per txid. Temporary and set-aside
    files are not markers."""
    directory = os.path.dirname(os.path.abspath(receipts_path)) or '.'
    base = os.path.basename(receipts_path) + '.pending'
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return []
    found = []
    for name in names:
        suffix = name[len(base):]
        if name == base or (name.startswith(base + '.') and len(suffix) == 65
                            and all(c in '0123456789abcdef' for c in suffix[1:])):
            found.append(os.path.join(directory, name))
    return sorted(found)


def _write_all(fd, data):
    """Write every byte of data to fd: os.write may write less than it was
    given (a full disk, a signal), and a short write taken for a whole one
    was the 2026-09-15 review's receipt finding. No progress is an error."""
    view = memoryview(data)
    while len(view):
        n = os.write(fd, view)
        if n <= 0:
            raise OSError(errno.EIO, 'write made no progress')
        view = view[n:]


def _write_pending_receipt(receipts_path, body):
    """Write the pending-receipt marker atomically (tmp + fsync + rename +
    directory fsync)

    body is {'receipt': <the line to append later>, 'probe': <hex of the
    key anchor_probe gives for the receipt's txid>}: enough to settle,
    after a crash, whether the calendar save the marker guards ever
    happened. The current reader asks about the txid it finds in the
    receipt; `probe` is written for the readers before 2026-09-18, which
    look up the field as it is, and now ask the same question by it. The
    marker is named by the receipt's txid.
    """
    path = marker_path(receipts_path, body['receipt']['txid'])
    tmp = path + '.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        _write_all(fd, (json.dumps(body) + '\n').encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rename(tmp, path)
    fsync_dir(path)


def _receipt_on_file(receipts_path, txid):
    """True if a complete receipt line for txid is already in the receipts
    file. A receipt is complete only with its newline: a last line without
    one is an interrupted append (_recover_receipt_tail drops it), never a
    receipt on file, however well it parses."""
    try:
        with open(receipts_path, 'rb') as fd:
            for line in fd:
                if not line.endswith(b'\n'):
                    continue
                try:
                    if json.loads(line).get('txid') == txid:
                        return True
                except ValueError:
                    continue
    except FileNotFoundError:
        pass
    return False


def _recent_receipts(path, n):
    """The last n receipt lines of the file at path, parsed

    Unparseable lines and lines without a txid are skipped; no file is no
    receipts.
    """
    try:
        with open(path, 'rb') as fd:
            lines = fd.readlines()
    except FileNotFoundError:
        return []
    receipts = []
    for line in lines[-n:]:
        if not line.endswith(b'\n'):
            continue   # an interrupted append, not a receipt
        try:
            receipt = json.loads(line)
        except ValueError:
            continue
        if isinstance(receipt, dict) and receipt.get('txid'):
            receipts.append(receipt)
    return receipts


def _recover_receipt_tail(fd):
    """Drop an incomplete last line (bytes after the final newline) left by
    an interrupted append, before anything is appended after it.

    A receipt is complete only with its newline, and its marker is removed
    only after that newline is on disk (file and directory fsynced), so an
    incomplete tail is always a receipt whose marker still stands: the
    marker recovers it in full, and nothing complete is ever touched.
    Returns the number of bytes dropped.
    """
    size = os.fstat(fd).st_size
    if size == 0 or os.pread(fd, 1, size - 1) == b'\n':
        return 0
    keep = 0
    pos = size
    while pos > 0:
        chunk_start = max(0, pos - 4096)
        chunk = os.pread(fd, pos - chunk_start, chunk_start)
        nl = chunk.rfind(b'\n')
        if nl >= 0:
            keep = chunk_start + nl + 1
            break
        pos = chunk_start
    os.ftruncate(fd, keep)
    os.fsync(fd)
    logging.warning("The receipts file ended in an incomplete receipt line (%d bytes without a newline, an interrupted "
                    "append); dropped before appending; the receipt it held is recovered from its pending marker"
                    % (size - keep))
    return size - keep


def _sync_receipts(path):
    """fsync the receipts file and its directory. A complete line found on
    file says it was written, not that it was synced: the append that
    wrote it may have stopped, or failed, at its fsync, and the marker is
    the only other copy of the receipt."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    fsync_dir(path)


class ReceiptsAheadOfDatabase(Exception):
    """A marker still stands, its receipt is on file, and the database does
    not hold the anchor's commitments. No stop can leave that: the receipt
    is appended only after the save's synchronous batch. The receipts file
    is newer than db/ (copies from different moments), and going on would
    anchor those records again and receipt them a second time."""


def _append_anchor_receipt(path, receipt):
    """Append one anchor receipt line to the JSONL file at path

    The line is an interface: the gateway's anchor billing parses these
    fields. See the "Anchor receipts" section of the README.

    An incomplete tail from an earlier interrupted append is dropped first
    (_recover_receipt_tail); then every byte of the line is written (a
    checked write-all loop, never one os.write taken on trust), the file
    is fsynced, and the directory is fsynced. Only after this returns does
    the caller remove the marker that names the receipt. Existing complete
    lines are never rewritten.
    """
    line = json.dumps(receipt) + '\n'
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        _recover_receipt_tail(fd)
        _write_all(fd, line.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    fsync_dir(path)


# not using proxy.listunspent() because it tries to convert bech32 address as base58
def listunspent(proxy, minconf=0, maxconf=999999):
    r = proxy._call('listunspent', minconf, maxconf)

    r2 = []
    for unspent in r:
        unspent['outpoint'] = COutPoint(lx(unspent['txid']), unspent['vout'])
        del unspent['txid']
        del unspent['vout']
        unspent['scriptPubKey'] = CScript(x(unspent['scriptPubKey']))
        unspent['amount'] = int(unspent['amount'] * COIN)
        r2.append(unspent)
    return r2

def find_unspent(proxy):
    def sort_filter_unspent(unspent):
        return list(reversed(sorted(filter(lambda x: x['amount'] > DUST and x['spendable'], unspent),
                      key=lambda x: x['amount'])))

    unspent = sort_filter_unspent(listunspent(proxy, 1))

    if len(unspent):
        return unspent
    else:
        logging.info("Couldn't find a confirmed output, trying unconfirmed")

        # Try again with the unconfirmed transactions to find a prior
        # unconfirmed timestamp transaction that we can safely replace.
        unconfirmed_unspent = sort_filter_unspent(listunspent(proxy, 0, 1))

        confirmed_unspent = []
        for unspent_txout in unconfirmed_unspent:
            txid = unspent_txout['outpoint'].hash
            tx = proxy.getrawtransaction(txid)

            # Unconfirmed timestamp transactions should all have a single input
            # and two outputs.
            #
            # FIXME: we should check that the second output is an op_return
            if len(tx.vin) == 1 and len(tx.vout) == 2:
                txin = tx.vin[0]
                try:
                    # Check that this output is in the UTXO set, and is thus
                    # confirmed.
                    confirmed_outpoint = proxy.gettxout(txin.prevout, includemempool=False)

                    # Make sure this txout is from a wallet transaction, which
                    # means we created it, and can spend it safely without any
                    # risk of a double-spend.
                    #
                    # This is probably overkill as a third party would have a
                    # hard time double spending a confirmed transaction too.
                    proxy.gettransaction(txin.prevout.hash)
                except IndexError:
                    continue

                confirmed_unspent.append({'outpoint': txin.prevout,
                                          'amount': confirmed_outpoint['txout'].nValue})

        return sorted(confirmed_unspent, key=lambda x: x['amount'])


class Stamper:
    """Timestamping bot"""

    # Empty-wallet warn-once flag. Class-level default (the RecordCounts
    # io_failed pattern) so test doubles built via __new__ share it; the
    # first trip shadows it with an instance attribute.
    wallet_empty_warned = False

    # Fee-cap warn-once flag, the same pattern: one ERROR on entering the
    # blocked state, one INFO when a transaction goes out again.
    fee_capped_warned = False

    # Reader for the journal.counts sidecar, set by the stamper loop when
    # anchor receipts are on; None keeps every count path inert. Class-level
    # default for the same test-double reason.
    record_counts = None

    # Deep-reorg detector (check_anchors): every ANCHOR_CHECK_INTERVAL
    # seconds the stamp loop asks the wallet about the last
    # ANCHOR_CHECK_RECEIPTS receipted anchors. needs_attention is what the
    # status line and the watcher read: one line per anchor whose saved
    # proofs name a block that no longer holds it. Class-level default for
    # the same test-double reason; the real instance owns a list.
    ANCHOR_CHECK_INTERVAL = 3600
    ANCHOR_CHECK_RECEIPTS = 100
    needs_attention = ()

    # Calendar-save warn-once flag, the same pattern: one ERROR when a
    # mature tree's save fails (the tree is kept and retried every pass),
    # one INFO when saves land again.
    save_failed_warned = False

    # Set to the reason when the stamper stopped the service (a startup it
    # cannot complete); None while it runs. otsd reads it at exit.
    failure = None

    # Blocks whose headers are known and whose bodies have not been read
    # yet, oldest first (__do_bitcoin). Class-level default for the same
    # test-double reason; the real instance owns a list.
    unprocessed_blocks = ()

    @staticmethod
    def __create_new_timestamp_tx_template(outpoint, txout_value, change_scriptPubKey):
        """Create a new timestamp transaction template

        The transaction created will have one input and two outputs, with the
        timestamp output set to an dummy OP_RETURN with an invalid amount.
        """
        return CTransaction([CTxIn(outpoint, nSequence=0xfffffffe)],
                            [CTxOut(txout_value, change_scriptPubKey),
                             CTxOut(-1, CScript([OP_RETURN, b'\x00' * 32]))])

    @staticmethod
    def __update_timestamp_tx(old_tx, new_commitment, new_min_block_height, relay_feerate):
        """Update an existing timestamp transaction

        Returns the old transaction with a new commitment, and with the fee
        bumped appropriately.
        """

        # Exact BIP141 virtual size. python-bitcoinlib 0.11.x (the deployed
        # pin) has no CTransaction.calc_weight(), but stripped serialization
        # is available, so weight = stripped*3 + total reproduces upstream's
        # calc_weight() exactly — including for non-segwit txs, where
        # stripped == total and the formula reduces to total*4.
        # Billing delta_fee on len(old_tx.serialize()) instead — total size
        # including witness — overpays 52.9% on the 1-input P2WPKH shape
        # (234 bytes vs 153 vbytes).
        old_tx_stripped_size = len(old_tx.serialize(dict(include_witness=False)))
        old_tx_weight = old_tx_stripped_size * 3 + len(old_tx.serialize())
        delta_fee = int((old_tx_weight + 3) / 4 * relay_feerate)

        old_change_txout = old_tx.vout[0]

        if old_change_txout.nValue - delta_fee > DUST:
            return CTransaction(old_tx.vin,
                                [CTxOut(old_change_txout.nValue - delta_fee, old_change_txout.scriptPubKey),
                                 CTxOut(0, CScript([OP_RETURN, new_commitment]))],
                                nLockTime=new_min_block_height)
        else:
            return CTransaction(old_tx.vin,
                                [CTxOut(0, CScript([OP_RETURN, new_commitment]))],
                                nLockTime=new_min_block_height)

    def settle_pending_receipt(self):
        """Settle every pending-receipt marker left by an earlier stop

        A marker (see __save_confirmed_timestamp_tx) holds the receipt. The
        calendar is asked for that anchor's own key (anchor_probe: its txid
        node, which only its saved tree puts there). If it is in the
        calendar the save happened and the receipt is owed: it is appended
        unless its txid is already on file. If it is not, the save never
        happened: those commitments are still pending and will be
        re-anchored under a new txid with their own receipt, so this
        receipt must not be written — writing it would bill the same
        records twice; and no later anchor over the same commitments can
        ever make this one look saved. Either way the marker is removed
        and the outcome logged. Each marker is settled on its own: one
        that cannot be settled (the receipts file unwritable, the marker
        not removable) is logged and left standing for the next start or
        the next anchor, asked about again then, and never stops the
        others or the anchor being saved. The one state that is not
        settled is a contradiction (ReceiptsAheadOfDatabase): it is raised,
        and a start stops the service on it. Runs at stamper start and
        before any new marker; a no-op when receipts are off or no marker
        exists. Messages name a marker by its role and its anchor's txid,
        never by its file name or directory.
        """
        if not self.anchor_receipts_path:
            return
        for path in pending_markers(self.anchor_receipts_path):
            try:
                self.__settle_marker(path)
            except ReceiptsAheadOfDatabase:
                raise
            except Exception as exp:
                logging.warning("%s could not be settled: %s; it stays until it can be, and is asked about again "
                                "at the next start and before the next marker" % (marker_role(path), error_text(exp)))

    def __settle_marker(self, path):
        role = marker_role(path)
        try:
            with open(path, 'rb') as fd:
                body = json.loads(fd.read())
            receipt = body['receipt']
            txid = receipt['txid']
            probe = anchor_probe(txid)
        except (ValueError, KeyError, TypeError) as exp:
            stamp = int(time.time())
            # The class only: a decoding error's repr carries the bytes it
            # refused (2026-09-18 corrections review, G3a).
            logging.warning("%s is unreadable (%s); set aside beside the receipts file with the suffix .corrupt-%d, "
                            "no receipt written" % (role, type(exp).__name__, stamp))
            os.rename(path, '%s.corrupt-%d' % (path, stamp))
            return

        on_file = _receipt_on_file(self.anchor_receipts_path, txid)
        if probe in self.calendar:
            if on_file:
                # Found, not yet known to be synced: the append that wrote
                # it may have stopped or failed at its fsync, and this
                # marker is the only other copy. Sync before it goes.
                _sync_receipts(self.anchor_receipts_path)
                logging.info("Pending anchor receipt for tx %s is already on file; marker removed" % txid)
            else:
                _append_anchor_receipt(self.anchor_receipts_path, receipt)
                logging.warning("Anchor receipt for tx %s recovered from the pending marker: the calendar "
                                "save completed before an earlier stop, the receipt had not been written" % txid)
        elif on_file:
            raise ReceiptsAheadOfDatabase(
                "CALENDAR STORAGE INCONSISTENT: the receipts file holds the receipt of anchor %s, its pending receipt "
                "marker still stands, and the database does not hold that anchor: the receipts file is newer than db/ "
                "(copies from different moments). Going on would anchor those records again and receipt them a "
                "second time. Recovery: restore db/ and the receipts file, with its markers, from one copy taken with "
                "the calendar stopped. To accept the second receipt instead, remove that marker and reconcile the two "
                "receipts by hand" % txid)
        else:
            logging.warning("Pending anchor receipt for tx %s discarded: the calendar never saved that "
                            "anchor (an earlier stop hit before the save), so its commitments will "
                            "be re-anchored and receipted under a new txid; nothing is owed for %s"
                            % (txid, txid))
        os.unlink(path)

    def check_anchors(self, proxy=None):
        """Deep-reorg detector: are the receipted anchors still where their receipts say?

        A receipted anchor had min_confirmations when its receipt was
        written, and the calendar saved proofs naming that block. This asks
        the wallet (gettransaction) about the last ANCHOR_CHECK_RECEIPTS
        receipted txids. A confirmation count at or below zero means the
        anchor left the chain: Bitcoin Core reports a conflicted
        transaction as a negative count and one back in the mempool as
        zero. A blockheight other than the receipted one means it was mined
        again elsewhere. Either way the proofs on file name a block that no
        longer holds the anchor, so the finding is kept until the operator
        acts: it lands in needs_attention (the status line and the watcher
        read it), is logged at ERROR on every check while it stands, and
        never clears itself; a later re-mine does not repair the proofs.
        Nothing is re-anchored automatically. A txid the wallet does not
        know (a rebuilt wallet) or an RPC failure is a warning, not a
        finding. Runs hourly from the stamp loop and must never break it.
        Returns the findings.
        """
        if not self.anchor_receipts_path:
            return []
        receipts = _recent_receipts(self.anchor_receipts_path, self.ANCHOR_CHECK_RECEIPTS)
        if not receipts:
            return []
        findings = getattr(self, 'anchor_findings', None)
        if findings is None:
            findings = self.anchor_findings = {}
        try:
            if proxy is None:
                proxy = make_proxy()
            for receipt in receipts:
                txid = receipt['txid']
                try:
                    r = proxy._call('gettransaction', txid)
                    confirmations = r['confirmations']
                    height = r.get('blockheight')
                except Exception as exp:
                    logging.warning("anchor check: cannot ask the wallet about anchor %s: %r" % (txid, exp))
                    continue
                if not isinstance(confirmations, int):
                    logging.warning("anchor check: no confirmation count for anchor %s: %r" % (txid, r))
                    continue
                if confirmations <= 0:
                    findings[txid] = ("anchor %s left the chain (confirmations %d, receipted at height %s)"
                                      % (txid, confirmations, receipt.get('confirmed_height')))
                elif isinstance(height, int) and height != receipt.get('confirmed_height'):
                    findings[txid] = ("anchor %s mined again at height %d, receipted at height %s"
                                      % (txid, height, receipt.get('confirmed_height')))
        except Exception as exp:
            logging.warning("anchor check failed: %r; next check in %ds" % (exp, self.ANCHOR_CHECK_INTERVAL))

        self.needs_attention = list(findings.values())
        if findings:
            logging.error("NEEDS ATTENTION: %d receipted anchor(s) no longer where the proofs say; "
                          "nothing is re-anchored automatically: %s"
                          % (len(findings), "; ".join(findings.values())))
        return self.needs_attention

    def __save_confirmed_timestamp_tx(self, confirmed_tx, watermark=None):
        """Save a fully confirmed timestamp to disk, then receipt it

        Marker before the save, receipt after it, and no save without its
        marker. A crash before the save leaves a marker whose anchor the
        calendar does not hold: the commitments re-anchor under a new txid
        with their own receipt, and the marker is discarded at the next
        start (settle_pending_receipt) — never a second bill for the same
        records. A crash after the save leaves a marker whose anchor the
        calendar holds: the receipt is recovered from it. Every marker is
        named by its anchor's txid, so an earlier anchor's receipt still
        owed (its marker standing because the receipts file could not be
        written) survives every later anchor: what a crash or a full disk
        can lose is a receipt's timeliness, never the receipt; what it can
        never do is bill twice. A marker that cannot be written at all is
        a save that waits (below).

        watermark, when given, is the journal checkpoint this save makes
        true; the calendar commits it in the same synchronous batch as the
        timestamps. A save that raises leaves the tree where it was: the
        caller (__save_mature_trees) keeps it and retries every pass.
        """
        txid = b2lx(confirmed_tx.tx.GetTxid())
        receipt = None
        if self.anchor_receipts_path:
            receipt = {'txid': txid,
                       'fee_sats': confirmed_tx.fee,
                       'commitments': len(confirmed_tx.commitment_timestamps),
                       'confirmed_height': confirmed_tx.height,
                       'confirmed_at': int(time.time()),
                       'records': confirmed_tx.records}
            # Earlier anchors' markers first, each on its own (one that
            # cannot be settled is logged and left standing); then this
            # anchor's marker. A marker that cannot be written is a save
            # that does not happen this pass: the error reaches
            # __save_mature_trees, which keeps the tree and retries every
            # pass. A receipts directory that cannot be written therefore
            # delays publication; it never retires a receipt that nothing
            # durable owns (2026-09-18 cold review R06: the failure used to
            # be logged and the save went on, and when the append after it
            # failed too the tree was retired with no marker and no
            # receipt). The other durable owner would be a record of the
            # owed receipt inside the save's own batch; the delay is the
            # smaller change and is the one the contract states (C5).
            self.settle_pending_receipt()
            _write_pending_receipt(self.anchor_receipts_path,
                                   {'receipt': receipt, 'probe': anchor_probe(txid).hex()})

        if watermark is None:
            self.calendar.add_commitment_timestamps(confirmed_tx.commitment_timestamps)
        else:
            self.calendar.add_commitment_timestamps(confirmed_tx.commitment_timestamps, watermark=watermark)
        logging.info("tx %s fully confirmed, %d timestamps added to calendar" %
                     (txid, len(confirmed_tx.commitment_timestamps)))

        if receipt is not None:
            try:
                _append_anchor_receipt(self.anchor_receipts_path, receipt)
            except Exception as exp:
                # A failed receipt write must never break the stamp loop.
                # The marker stays: the receipt is recovered from it before
                # the next anchor's marker, or at the next start.
                logging.warning("Failed to write anchor receipt for tx %s: %s; the pending marker "
                                "keeps it until it can be written" % (txid, error_text(exp)))
                return
            try:
                os.unlink(marker_path(self.anchor_receipts_path, txid))   # this anchor's marker, never another's
            except FileNotFoundError:
                pass
            except OSError as exp:
                logging.warning("Failed to remove pending anchor receipt marker for tx %s: %s" % (txid, error_text(exp)))

    def __checkpoint_after(self, confirmed_tx):
        """The journal checkpoint once confirmed_tx is saved: the lowest
        journal index still outstanding — pending commitments plus other
        mined-but-not-yet-deep trees, exactly what commitment_idxs holds
        minus this tree — or the scan cursor when nothing is. Everything
        below it is then in the calendar. None before the scan has run."""
        saved = set(ts.msg for ts in confirmed_tx.commitment_timestamps)
        remaining = [idx for msg, idx in self.commitment_idxs.items() if msg not in saved]
        return min(remaining) if remaining else self.journal_cursor

    def __write_journal_checkpoint(self, idx):
        """Persist journal.known-good after a confirmed anchor

        idx is __checkpoint_after's value, the same one the calendar just
        committed as its watermark in the save's batch, so the file can
        never claim more than the database durably holds. Written with the
        database's generation, so a checkpoint kept beside a recreated or
        older-restored database is refused at the next start
        (Calendar.verify_storage_generation); without a generation to
        write, nothing is written, since an index alone is a file the next
        start refuses. Atomic via rename: a torn write can never truncate
        an existing checkpoint. The checkpoint is a convenience: any
        failure warns and must never break the stamp loop.
        """
        try:
            generation = getattr(self.calendar, 'generation', None)
            if idx is None or not isinstance(generation, str):
                return
            write_checkpoint(self.calendar.path + '/journal.known-good', idx, generation)
        except Exception as exp:
            logging.warning("Failed to write journal checkpoint: %r" % exp)

    def __save_mature_trees(self, best_height):
        """Save every mined tree that has reached min_confirmations

        A tree leaves txs_waiting_for_confirmation only after its save has
        returned, i.e. after the calendar's synchronous write; a save that
        raises (the calendar's write, or the receipt marker before it)
        keeps the tree, logged once, and every pass retries every mature
        unsaved tree, whether or not a new block arrived, until it lands.
        Only then are its record counts and journal indexes released and
        the checkpoint advanced. Called only once every known block's body
        has been read (__do_bitcoin): a tree whose block a reorg replaced
        is back in pending before anything is called mature.
        """
        if not self.txs_waiting_for_confirmation:
            return
        due = sorted(height for height in self.txs_waiting_for_confirmation
                     if height <= best_height - self.min_confirmations + 1)
        for height in due:
            confirmed_tx = self.txs_waiting_for_confirmation[height]
            watermark = self.__checkpoint_after(confirmed_tx)
            try:
                self.__save_confirmed_timestamp_tx(confirmed_tx, watermark)
            except Exception as exp:
                if not self.save_failed_warned:
                    # The error by its class and errno, never its text: a
                    # marker that could not be written names the receipts
                    # file in it, a database error names the directory.
                    logging.error("Calendar save failed for tx %s (%d timestamps): %s; the tree is kept and "
                                  "retried every pass until it lands"
                                  % (b2lx(confirmed_tx.tx.GetTxid()), len(confirmed_tx.commitment_timestamps),
                                     error_text(exp)))
                    self.save_failed_warned = True
                continue
            if self.save_failed_warned:
                self.save_failed_warned = False
                logging.info("Calendar saves succeeding again")
            self.txs_waiting_for_confirmation.pop(height, None)
            # The anchor is final: these commitments' record counts can
            # never be summed into another tree, and their journal
            # entries are behind the checkpoint from here on.
            for commitment_timestamp in confirmed_tx.commitment_timestamps:
                self.commitment_records.pop(commitment_timestamp.msg, None)
                self.commitment_idxs.pop(commitment_timestamp.msg, None)
            self.__write_journal_checkpoint(watermark)

    def __pending_to_merkle_tree(self, n):
            # Update the most recent timestamp transaction with new commitments
            commitment_timestamps = [Timestamp(commitment) for commitment in tuple(self.pending_commitments)[0:n]]

            # Remember that commitment_timestamps contains raw commitments,
            # which are longer than necessary, so we sha256 them before passing
            # them to make_merkle_tree, which concatenates whatever it gets (or
            # for the matter, returns what it gets if there's only one item for
            # the tree!)
            commitment_digest_timestamps = [stamp.ops.add(OpSHA256()) for stamp in commitment_timestamps]

            logging.debug("Making merkle tree")
            tip_timestamp = make_merkle_tree(commitment_digest_timestamps)
            logging.debug("Done making merkle tree")

            return tip_timestamp, commitment_timestamps

    def __count_tree_records(self, commitment_timestamps):
        """Sum the per-commitment record counts for one closed tree

        This number becomes a bill: a record that can't be proven counted
        must not be charged, so a missing count sums as 0 — undercount plus
        one warning per tree is the acceptable failure direction.
        """
        records = 0
        missing = 0
        for commitment_timestamp in commitment_timestamps:
            msg = commitment_timestamp.msg
            count = self.commitment_records.get(msg)
            if count is None:
                # The scan reads each count once, the second its journal
                # entry appears -- which can be the instant before the
                # aggregator's sidecar write lands. By tree close the
                # sidecar is complete, so a count the scan missed is read
                # again here; only a count still absent is a hole.
                idx = self.commitment_idxs.get(msg)
                if self.record_counts is not None and idx is not None:
                    count = self.record_counts.get(idx)
                    if count is not None:
                        self.commitment_records[msg] = count
            if count is None:
                missing += 1
            else:
                records += count

        if missing:
            logging.warning("anchor records: %d of %d commitments in tree have no record count; "
                            "receipt will undercount" % (missing, len(commitment_timestamps)))

        return records

    def __do_bitcoin(self):
        """Do Bitcoin-related maintenance"""

        # FIXME: we shouldn't have to create a new proxy each time, but with
        # current python-bitcoinlib and the RPC implementation it seems that
        # the proxy connection can timeout w/o recovering properly.
        proxy = make_proxy()

        new_blocks = self.known_blocks.update_from_proxy(proxy)
        best_height = new_blocks[-1][0] if new_blocks else self.known_blocks.best_block_height()

        # Observed is not processed. known_blocks holds the headers seen;
        # this queue owns each block's body until it has been read and
        # every tree waiting at its height has been put back to pending. A
        # fetch that raises leaves that block and every block after it
        # queued for the next pass, and no tree is saved as mature while a
        # block is still owed (2026-09-18 cold review R01: the header
        # cursor advanced before the bodies were read, so one failed fetch
        # consumed a reorg's notification, and the next pass saved the
        # orphaned tree against the new chain's height). update_from_proxy
        # appends from the fork point up, so a block still queued at or
        # above the first new height was itself replaced: its replacement
        # is among the new blocks, and it is dropped unread.
        for (block_height, block_hash) in new_blocks:
            logging.info("New block %s at height %d" % (b2lx(block_hash), block_height))
        if new_blocks:
            fork = new_blocks[0][0]
            self.unprocessed_blocks = [b for b in self.unprocessed_blocks if b[0] < fork]
        self.unprocessed_blocks = list(self.unprocessed_blocks) + list(new_blocks)

        while self.unprocessed_blocks:
            block_height, block_hash = self.unprocessed_blocks[0]

            # If there already are txs waiting for confirmation at this
            # block_height, there was a reorg and those pending commitments now
            # need to be added back to the pool
            reorged_tx = self.txs_waiting_for_confirmation.pop(block_height, None)
            if reorged_tx is not None:
                # FIXME: the reorged transaction might get mined in another
                # block, so just adding the commitments for it back to the pool
                # isn't ideal, but it is safe
                logging.info('tx %s at height %d removed by reorg, adding %d commitments back to pending'
                             % (b2lx(reorged_tx.tx.GetTxid()), block_height, len(reorged_tx.commitment_timestamps)))
                for reorged_commitment_timestamp in reorged_tx.commitment_timestamps:
                    self.pending_commitments.add(reorged_commitment_timestamp.msg)

            # Check if this block contains any of the pending transactions
            block = None
            while block is None:
                try:
                    block = proxy.getblock(block_hash)
                except KeyError:
                    # Must have been a reorg or something, return: the block
                    # stays queued and is asked for again next pass
                    logging.error("Failed to get block")
                    return
                except BrokenPipeError:
                    logging.error("BrokenPipeError to get block")
                    time.sleep(5)
                    proxy = make_proxy()

            # Pre-compute the block txids once, rather than recalculating them
            # for each unconfirmed_tx
            block_txids = set(tx.GetTxid() for tx in block.vtx)

            # Check all potential pending txs against this block.
            #
            # We iterate in reverse order to prioritize the most recent digest,
            # which would commit to the biggest merkle tree. However, at the
            # moment this doesn't actually matter, as we are only checking
            # transactions we created, which always conflict with each other
            # due to RBF.
            for unconfirmed_tx in self.unconfirmed_txs[::-1]:

                if unconfirmed_tx.tx.GetTxid() not in block_txids:
                    continue

                confirmed_tx = unconfirmed_tx  # Success! Found tx
                block_timestamp = make_timestamp_from_block_tx(confirmed_tx, block, block_height)

                logging.info("Found commitment %s in tx %s"
                             % (b2x(confirmed_tx.tip_timestamp.msg), b2lx(confirmed_tx.tx.GetTxid())))
                # Success!
                (tip_timestamp, commitment_timestamps) = self.__pending_to_merkle_tree(confirmed_tx.n)
                mined_tx = TimestampTx(confirmed_tx.tx, tip_timestamp, commitment_timestamps,
                                       confirmed_tx.fee, block_height, confirmed_tx.records)
                assert tip_timestamp.msg == unconfirmed_tx.tip_timestamp.msg

                mined_tx.tip_timestamp.merge(block_timestamp)

                logging.debug("Removing %d commitments from pending" % (unconfirmed_tx.n))
                for commitment in tuple(self.pending_commitments)[0:unconfirmed_tx.n]:
                    self.pending_commitments.remove(commitment)

                assert self.min_confirmations > 1
                logging.info("Success! %d commitments timestamped, now waiting for %d more confirmations" %
                             (len(mined_tx.commitment_timestamps), self.min_confirmations - 1))

                # Add pending_tx to the list of timestamp transactions that
                # have been mined, and are waiting for confirmations.
                self.txs_waiting_for_confirmation[block_height] = mined_tx

                # Erase all unconfirmed txs, as they all conflict with each other
                self.unconfirmed_txs.clear()

                # Finally, schedule a new timestamp transaction.
                #
                # To help desync calendars, this time is randomized.
                self.next_timestamp_tx = time.time() + (self.min_tx_interval * random.uniform(1, 2))

                break

            # This block's body is read and its height's tree, if a reorg
            # replaced it, is back in pending: the block is done.
            del self.unprocessed_blocks[0]

        # Every known block is processed. Save every mined tree that is deep
        # enough, including any kept back by an earlier failed save (retried
        # on every pass, new block or not).
        self.__save_mature_trees(best_height)

        # If we don't have any new blocks, and we have any unconfirmed
        # transactions, wait for a new block because there is nothing useful we
        # can do as the unconfirmed txs haven't been given a chance to get
        # mined.
        if not new_blocks and len(self.unconfirmed_txs) > 0:
            return

        # We've finished dealing with the new block(s) and any transactions
        # that have confirmed. Now we handling sending new transactions, be it
        # the first transaction of a fee-bumping cycle. Or replacing previously
        # sent transactions.

        time_to_next_tx = self.next_timestamp_tx - time.time()
        if time_to_next_tx > 0:
            # Minimum interval between transactions hasn't been reached, so do nothing
            logging.debug("Waiting %ds before next tx" % time_to_next_tx)
            return

        if not self.pending_commitments:
            logging.debug("No pending commitments, no tx needed")
            # An expired departure clock over an empty queue would let the
            # first commitment after an idle stretch trigger a broadcast
            # within seconds, timestamping its own arrival on the public
            # chain. Broadcast times must depend only on the box's own
            # schedule, never on submission times, so roll the clock forward
            # exactly as a post-confirmation reschedule does.
            if not self.unconfirmed_txs:
                self.next_timestamp_tx = time.time() + (self.min_tx_interval * random.uniform(1, 2))
            return

        new_tx = False
        if self.unconfirmed_txs:
            bump_feerate = self.relay_feerate
            prev_tx = self.unconfirmed_txs[-1].tx

        # First transaction of a new cycle
        else:
            new_tx = True
            # Find the biggest unspent output that's confirmed
            unspent = find_unspent(proxy)

            if not len(unspent):
                # Warn once, not once per loop second: a drained wallet is
                # one incident, not a stream of them.
                if not self.wallet_empty_warned:
                    logging.error("Can't timestamp; no spendable outputs")
                    self.wallet_empty_warned = True
                return
            if self.wallet_empty_warned:
                self.wallet_empty_warned = False
                logging.info("Spendable outputs available again; anchoring resumes")

            change_addr = proxy._call("getnewaddress", "", "bech32")
            change_addr_info = proxy._call("getaddressinfo", change_addr)
            change_addr_script = x(change_addr_info['scriptPubKey'])

            unsigned_tx = self.__create_new_timestamp_tx_template(unspent[-1]['outpoint'], unspent[-1]['amount'],
                                                                  change_addr_script)

            # Sign the initial tx template so that fee estimation knows how big
            # it is, including the size of the signature.
            r = proxy.signrawtransactionwithwallet(unsigned_tx)
            if not r['complete']:
                logging.error("Failed to sign transaction! r = %r" % r)
                return
            prev_tx = r['tx']

            logging.debug('New timestamp tx, spending output %r, value %s' % (unspent[-1]['outpoint'],
                                                                              str_money_value(unspent[-1]['amount'])))

            # For the first transaction, use an estimated fee with confirmation
            # target as the bump_feerate. It'll get reset later.
            initial_feerate = proxy._call("estimatesmartfee", self.conf_target)
            try:
                initial_feerate = float(initial_feerate['feerate']) * COIN / 1000
            except KeyError:
                initial_feerate = self.relay_feerate

            bump_feerate = initial_feerate

        (tip_timestamp, commitment_timestamps) = self.__pending_to_merkle_tree(len(self.pending_commitments))
        # The record count is fixed here, when the tree closes over the
        # pending commitments; it rides beside fee to the receipt.
        records = self.__count_tree_records(commitment_timestamps) if self.anchor_receipts_path else 0
        logging.debug("New tip is %s" % b2x(tip_timestamp.msg))
        # make_merkle_tree() seems to take long enough on really big adds
        # that the proxy dies
        proxy = make_proxy()

        sent_tx = None
        while sent_tx is None:
            unsigned_tx = self.__update_timestamp_tx(prev_tx, tip_timestamp.msg,
                                                     proxy.getblockcount(), bump_feerate)

            # Reset now that the initial tx template has been processed.
            if new_tx:
                new_tx = False
                bump_feerate = self.relay_feerate

            fee = _get_tx_fee(unsigned_tx, proxy)
            if fee is None:
                if self.unconfirmed_txs:
                    # The in-flight anchor's input is no longer a confirmed
                    # unspent output: a shallow reorg took its parent, or a
                    # version this stamper does not track (one a restart
                    # forgot) was mined. No bump can ever be priced from
                    # here, so the cycle is dead. Abandon it: the pending
                    # commitments are untouched, and the next pass starts a
                    # fresh cycle from what the wallet actually holds.
                    logging.warning("Anchor cycle abandoned: the input of in-flight tx %s is no longer "
                                    "confirmed (a reorg, or an untracked version was mined); "
                                    "%d commitments stay pending; starting a fresh cycle"
                                    % (b2lx(prev_tx.GetTxid()), len(self.pending_commitments)))
                    self.unconfirmed_txs.clear()
                else:
                    logging.debug("Can't determine txfee of transaction; skipping")
                return
            if fee > self.max_fee:
                # Warn once, not once per loop second: a blocked cap is one
                # incident, cleared when a transaction goes out again.
                if not self.fee_capped_warned:
                    logging.error("Maximum txfee reached! fee %d > cap %d; anchoring waits for a lower feerate"
                                  % (fee, self.max_fee))
                    self.fee_capped_warned = True
                return

            r = proxy.signrawtransactionwithwallet(unsigned_tx)
            if not r['complete']:
                logging.error("Failed to sign transaction! r = %r" % r)
                return
            signed_tx = r['tx']

            try:
                proxy.sendrawtransaction(signed_tx)
            except bitcoin.rpc.JSONRPCError as err:
                if err.error['code'] == -26:
                    logging.debug("Err: %r" % err.error)
                    # Insufficient priority - basically means we didn't
                    # pay enough, so try again with a higher feerate
                    bump_feerate *= 1.25
                    continue

                else:
                    raise err  # something else, fail!

            sent_tx = signed_tx

        if self.fee_capped_warned:
            self.fee_capped_warned = False
            logging.info("Fee back under the cap; anchoring resumes")

        if self.unconfirmed_txs:
            logging.info("Sent timestamp tx %s, replacing %s; %d total commitments; %d prior tx versions" %
                         (b2lx(sent_tx.GetTxid()), b2lx(prev_tx.GetTxid()), len(commitment_timestamps),
                          len(self.unconfirmed_txs)))
        else:
            logging.info("Sent timestamp tx %s; %d total commitments" % (b2lx(sent_tx.GetTxid()),
                                                                         len(commitment_timestamps)))

        self.unconfirmed_txs.append(UnconfirmedTimestampTx(sent_tx, tip_timestamp, len(commitment_timestamps), fee,
                                                           records))

    def __fail(self, reason):
        """A start the stamper cannot complete stops the whole service: the
        reason is logged at CRITICAL, exit_event is set (otsd shuts the HTTP
        server down and exits nonzero, so the supervisor restarts it), and
        the thread returns. Never a dead thread behind a live listener."""
        self.failure = reason
        logging.critical("Stamper cannot start: %s. The service stops." % reason)
        self.exit_event.set()

    def __open(self):
        """Everything the loop needs before its first pass; raises on what
        it cannot have."""
        journal = Journal(self.calendar.path + '/journal')
        record_counts = RecordCounts(self.calendar.path + '/journal.counts') \
            if self.anchor_receipts_path else None
        # Shared with the tree close, which re-reads counts the scan missed.
        self.record_counts = record_counts

        # A marker left by a stop between an anchor's calendar save and its
        # receipt (or just before the save) is settled before anything else.
        # A receipt on file for a save the database does not hold is not a
        # stop's residue but a restore from different moments: the start
        # fails on it, before anything is anchored a second time.
        try:
            self.settle_pending_receipt()
        except ReceiptsAheadOfDatabase:
            raise
        except Exception as exp:
            logging.error("Settling the pending anchor receipt marker failed: %s; stamping continues"
                          % error_text(exp), exc_info=True)

        if record_counts is None and os.path.exists(self.calendar.path + '/journal.counts'):
            # The sidecar only ever exists because receipts were on: this
            # calendar was receipting its anchors and is now anchoring for
            # free, with nothing downstream to show it. Say so once, loudly.
            logging.warning("OTSD_ANCHOR_RECEIPTS is unset but journal.counts exists: anchor receipts "
                            "were on before and are off now; anchors will not be receipted or billed")

        try:
            checkpoint = read_checkpoint(self.calendar.path + '/journal.known-good')
        except (ValueError, OSError) as exp:
            # Its class and errno, never its text: an OSError's names the path.
            found = 'is malformed (%s)' % exp if isinstance(exp, ValueError) else 'cannot be read (%s)' % error_text(exp)
            raise ValueError("journal.known-good %s. Recovery: give the file back its permissions, or delete it to rescan "
                             "the whole journal from index 0 (safe: every commitment the calendar holds is skipped by "
                             "its membership probe), or restore it from the same backup as db/" % found)
        idx = checkpoint[0] if checkpoint else 0
        return journal, record_counts, idx

    def __loop(self):
        logging.info("Starting stamper loop")

        try:
            journal, record_counts, idx = self.__open()
        except Exception as exp:
            self.__fail('%s: %s' % (type(exp).__name__, exp))
            return

        read_failed = False

        while not self.exit_event.is_set():
            # Get all pending commitments. The reads here (journal,
            # calendar membership) sit outside the __do_bitcoin guard and
            # must never kill the thread: a failed read adds nothing this
            # round — errs low — anchoring of what is already pending
            # continues below, and the fill retries next second, warned
            # once until a read succeeds again.
            try:
                while len(self.pending_commitments) < self.max_pending:
                    try:
                        commitment = journal[idx]
                    except KeyError:
                        break

                    # Is this commitment already stamped?
                    if commitment not in self.calendar:
                        self.pending_commitments.add(commitment)
                        # setdefault: a resubmitted commitment keeps its
                        # LOWEST index, so the checkpoint can never advance
                        # past an unanchored journal entry.
                        self.commitment_idxs.setdefault(commitment, idx)
                        if record_counts is not None:
                            count = record_counts.get(idx)
                            if count is not None:
                                self.commitment_records[commitment] = count
                        if idx % 1000 == 0:
                            logging.debug('Added %s (idx %d) to pending commitments; %d total'
                                          % (b2x(commitment), idx, len(self.pending_commitments)))
                    else:
                        if idx % 10000 == 0:
                            logging.debug('Commitment at idx %d already stamped' % idx)

                    idx += 1
            except Exception as exp:
                if not read_failed:
                    logging.warning("Pending-commitment read failed: %r; "
                                    "stamping continues with %d pending; retrying every second"
                                    % (exp, len(self.pending_commitments)))
                    read_failed = True
            else:
                if read_failed:
                    read_failed = False
                    logging.info("Pending-commitment reads succeeding again")

            self.journal_cursor = idx

            try:
                self.__do_bitcoin()
                if time.time() >= self.next_anchor_check:
                    self.next_anchor_check = time.time() + self.ANCHOR_CHECK_INTERVAL
                    self.check_anchors()
            except bitcoin.rpc.InWarmupError as warmuperr:
                logging.info("Bitcoincore is warming up: %r" % warmuperr)
                time.sleep(5)
            except ValueError as err:
                # If not caused by misconfiguration this error in bitcoinlib
                # usually occurs when bitcoincore is not started
                if str(err).startswith('Cookie file unusable'):
                    logging.error("Proxy Authentication Error: Is bitcoincore running?: %r" % err)
                    time.sleep(5)
                else:
                    logging.error("__do_bitcoin() failed: %r" % err, exc_info=True)
            except Exception as exp:
                # !@#$ Python.
                #
                # Just logging errors like this is garbage, but we don't really
                # know all the ways that __do_bitcoin() will raise an exception
                # so easiest just to ignore and continue onwards.
                #
                # Mainly Bitcoin Core has been hanging up on our RPC
                # connection, and python-bitcoinlib doesn't have great handling
                # of that. In our case we should be safe to just retry as
                # __do_bitcoin() is fairly self-contained.
                logging.error("__do_bitcoin() failed: %r" % exp, exc_info=True)

            self.exit_event.wait(1)

    def is_pending(self, commitment):
        """Return whether or not a commitment is waiting to be stamped

        Returns False if not, or str reason if it is
        """
        if commitment in self.pending_commitments:
            return "Pending confirmation in Bitcoin blockchain"

        else:
            journal = Journal(self.calendar.path + '/journal')
            idx = self.journal_cursor
            while idx is not None:
                # cursor is None when stamper loop never executed once
                try:
                    recent_commitment = journal[idx]
                except KeyError:
                    break
                if recent_commitment == commitment:
                    return "Pending confirmation in Bitcoin blockchain"
                idx += 1

            # A snapshot: the stamper thread mutates this dict while an RPC
            # thread is here (N16, 2026-09-08).
            for height, ttx in list(self.txs_waiting_for_confirmation.items()):
                for commitment_timestamp in ttx.commitment_timestamps:
                    if commitment == commitment_timestamp.msg:
                        return "Timestamped by transaction %s; waiting for %d confirmations"\
                               % (b2lx(ttx.tx.GetTxid()), self.min_confirmations)

        return False

    def __init__(self, calendar, exit_event, conf_target, relay_feerate, min_confirmations, min_tx_interval, max_fee, max_pending):
        self.calendar = calendar
        self.exit_event = exit_event

        self.conf_target = conf_target
        self.relay_feerate = relay_feerate
        self.min_confirmations = min_confirmations
        if not self.min_confirmations > 1:
            # otsd refuses the flag before any socket or worker exists;
            # this guard is for every other caller, and a ValueError is
            # not stripped by -O as an assert would be.
            raise ValueError("min_confirmations must be greater than 1, got %r" % (min_confirmations,))
        self.min_tx_interval = min_tx_interval
        self.max_fee = max_fee
        self.max_pending = max_pending

        # Unset (the default) = anchor receipts entirely off; set = path to
        # the append-only JSONL receipts file the gateway's billing reads.
        self.anchor_receipts_path = os.getenv("OTSD_ANCHOR_RECEIPTS") or None

        self.known_blocks = KnownBlocks()
        self.unprocessed_blocks = []
        self.unconfirmed_txs = []

        self.pending_commitments = OrderedSet()
        self.txs_waiting_for_confirmation = {}

        # Per-commitment record counts read from the journal.counts sidecar,
        # keyed by commitment msg; populated only when anchor receipts are
        # on, released once the commitment's anchor is final.
        self.commitment_records = {}

        # Journal index per outstanding commitment (pending, or riding a
        # mined-but-not-yet-deep tree), released with commitment_records
        # once its anchor is final. min() over this is the journal
        # checkpoint: everything below it is anchored.
        self.commitment_idxs = {}

        # Arm the departure clock free-running from the first moment: an
        # expired clock at startup would let the first commitment after a
        # restart fire a broadcast within seconds.
        self.next_timestamp_tx = time.time() + (self.min_tx_interval * random.uniform(1, 2))
        self.journal_cursor = None

        # The deep-reorg detector: first check on the first pass, then
        # hourly; findings by txid, kept until the operator acts.
        self.next_anchor_check = 0
        self.anchor_findings = {}
        self.needs_attention = []

        self.thread = threading.Thread(target=self.__loop)
        self.thread.start()
