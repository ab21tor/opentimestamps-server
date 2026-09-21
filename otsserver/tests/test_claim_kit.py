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

"""The claim kit: ops/verify_claim.py and
ops/export_headers.py, loaded by path like the other ops tools.

Real data: two anchored proofs from the reference deployment -- the first
gateway proof of the 2026-07 stranger run (block 959459) and a client
adapter record (block 960458) -- verified against real mainnet headers
fetched once from a public source and pinned under ops/tests/claim/; the
genesis run 0..5; and the retarget at 965664. The exhibits behind those
digests are not on file, so they run in --digest mode, which the verifier
says out loud.

Synthetic data, the worked example: a selfstamp manifest (the notary's own
diary) as the exhibit, stamped through the real Calendar (so the proof
carries the not-before bound), anchored by hand into a chain of headers
mined in the test at an easy difficulty from a stated checkpoint. That kit
is then tampered with in every way the verifier must catch.

Fails on the pre-change code: neither tool exists.

Fabricated evidence must not receive a valid verdict and impossible
targets must not be accepted: the chain is checked whole,
Bitcoin Core's target rules are enforced, easy-difficulty chains need
--network regtest by name, and a file tied to neither genesis nor a stated
checkpoint is INCOMPLETE (exit 2), never HOLDS. The hostile kit (a
genuine later checkpoint over an unmined, substituted block-1 header) is
the regression Test_hostile_kits keeps.

A checkpoint pins
only the headers at or below it — headers above it are chained forward
only, and a chain that follows the rules is not thereby Bitcoin's chain.
So the stated checkpoint must be at or after the attested block, and a
kit without one is INCOMPLETE. Test_incompatible_forks builds two regtest
forks off one common prefix, each anchoring a different exhibit at the
same height, and shows that a checkpoint on the common prefix lets neither
HOLD, while a checkpoint at the fork's own tip lets exactly one.
"""

import datetime
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from bitcoin.core import lx
from opentimestamps.core.notary import BitcoinBlockHeaderAttestation
from opentimestamps.core.op import OpAppend, OpPrepend, OpSHA256
from opentimestamps.core.serialize import StreamSerializationContext
from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp

from otsserver.calendar import Calendar
from otsserver.stamper import KnownBlocks

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
OPS = ROOT / 'ops'
FIXTURES = OPS / 'tests' / 'claim'


def load(name):
    spec = importlib.util.spec_from_file_location(name, OPS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verify_claim = load('verify_claim')
export_headers = load('export_headers')
selfstamp = load('selfstamp')


def sha256d(data):
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def read_fixture_headers(name):
    """(start height, raw headers) from a 'height hash hex' text fixture"""
    rows = [line.split() for line in (FIXTURES / name).read_text().splitlines() if line.strip()]
    return int(rows[0][0]), b''.join(bytes.fromhex(r[2]) for r in rows), rows


def run_verifier(*argv):
    out = io.StringIO()
    code = verify_claim.main([str(a) for a in argv], out=out)
    return code, out.getvalue()


# --- a chain mined in the test ------------------------------------------------

EASY_BITS = 0x1f00ffff          # ~65k hashes per header: seconds, not hours
EASY_TARGET = 0xffff << (8 * (0x1f - 3))


def mine(prev_hash, merkle_root, ntime, bits=EASY_BITS):
    nonce = 0
    while True:
        header = struct.pack('<L', 0x20000000) + prev_hash + merkle_root + struct.pack('<LLL', ntime, bits, nonce)
        if int.from_bytes(sha256d(header), 'little') <= EASY_TARGET:
            return header
        nonce += 1


class SyntheticChain:
    """Headers from a checkpoint height, mined here; a header's merkle root
    can be chosen (the anchor block's) or is a hash of its height."""

    def __init__(self, start, count, first_prev=b'\x11' * 32, base_time=1788600000):
        self.start = start
        self.headers = []
        prev = first_prev
        for i in range(count):
            self.extend(hashlib.sha256(b'block %d' % (start + i)).digest(), base_time + 600 * i, prev)
            prev = sha256d(self.headers[-1])

    def extend(self, merkle_root, ntime=None, prev=None):
        if prev is None:
            prev = sha256d(self.headers[-1])
        if ntime is None:
            ntime = struct.unpack('<L', self.headers[-1][68:72])[0] + 600
        self.headers.append(mine(prev, merkle_root, ntime))

    def hash(self, height):
        return sha256d(self.headers[height - self.start])

    def display(self, height):
        return self.hash(height)[::-1].hex()

    def raw(self):
        return b''.join(self.headers)


class ChainProxy:
    """Enough of bitcoind for KnownBlocks: the synthetic chain as it stands"""

    def __init__(self, chain):
        self.chain = chain

    def getblockcount(self):
        return self.chain.start + len(self.chain.headers) - 1

    def getbestblockhash(self):
        return self.chain.hash(self.getblockcount())

    def getblockhash(self, height):
        if self.chain.start <= height <= self.getblockcount():
            return self.chain.hash(height)
        raise IndexError(height)


# --- the worked example: the diary as an exhibit --------------------------------

def make_kit(tmp, anchor_height=965870, checkpoint=965860):
    """A claim-kit folder: a selfstamp manifest, its proof (real calendar
    ops, not-before bound, a hand-built bitcoin path), headers.bin from a
    checkpoint with the anchor block six deep. Returns the folder and the
    facts a test compares against."""
    kit = pathlib.Path(tmp) / 'kit'
    kit.mkdir()
    books = kit / 'books'
    books.mkdir()
    (books / 'receipts.jsonl').write_text('{"txid": "aa", "records": 3}\n')
    cfg = {'state_dir': str(kit / 'state'), 'calendar_url': 'http://127.0.0.1:9', 'host': 'appliance-1',
           'books': {'receipts': str(books / 'receipts.jsonl')}, 'audit_logs': None,
           'float_low_sats': 100000, 'journal': False, 'fork_head': None, 'outbox': None, 'inbox': None}
    manifests = kit / 'state' / 'manifests'
    manifests.mkdir(parents=True)
    period = datetime.date(2026, 9, 13)
    now = datetime.datetime(2026, 9, 14, 0, 30, tzinfo=datetime.timezone.utc)
    _, raw = selfstamp.build_manifest(cfg, period, now, manifests)
    exhibit = kit / '2026-09-13.json'
    exhibit.write_bytes(raw)
    digest = hashlib.sha256(raw).digest()

    # Headers up to the block before the anchor: the calendar's best block.
    chain = SyntheticChain(checkpoint, anchor_height - checkpoint)
    cal_dir = pathlib.Path(tmp) / 'calendar'
    cal_dir.mkdir()
    (cal_dir / 'uri').write_text('http://127.0.0.1:14788\n')
    (cal_dir / 'hmac-key').write_bytes(b'\x01' * 32)
    calendar = Calendar(str(cal_dir))

    class St:
        known_blocks = KnownBlocks()
    calendar.stamper = St()
    St.known_blocks.update_from_proxy(ChainProxy(chain))

    # The aggregator's nonce, then the calendar's commitment path.
    leaf = Timestamp(digest)
    tip = leaf.ops.add(OpAppend(b'\x99' * 16)).ops.add(OpSHA256())
    calendar.submit(tip)
    commitment = tip
    while commitment.ops:
        (_, commitment), = commitment.ops.items()
    commitment.attestations.clear()   # the upgrade replaces the pending attestation

    # The stamper's bitcoin path: sha256 of the journal entry is the OP_RETURN
    # payload; a tx around it; txid; a two-level merkle path; the root.
    prefix = bytes.fromhex('0100000001' + '33' * 32 + '00000000' + '00' + 'feffffff' + '01' + '0000000000000000' + '22' + '6a20')
    suffix = bytes.fromhex('00000000')
    txid = commitment.ops.add(OpSHA256()).ops.add(OpPrepend(prefix)).ops.add(OpAppend(suffix)).ops.add(OpSHA256()).ops.add(OpSHA256())
    level1 = txid.ops.add(OpAppend(b'\x44' * 32)).ops.add(OpSHA256()).ops.add(OpSHA256())
    root = level1.ops.add(OpPrepend(b'\x55' * 32)).ops.add(OpSHA256()).ops.add(OpSHA256())
    root.attestations.add(BitcoinBlockHeaderAttestation(anchor_height))

    chain.extend(root.msg)                 # the anchor block carries the root
    for _ in range(6):
        chain.extend(hashlib.sha256(b'after').digest())
    (kit / 'headers.bin').write_bytes(chain.raw())

    proof = kit / '2026-09-13.json.ots'
    with open(proof, 'wb') as fd:
        DetachedTimestampFile(OpSHA256(), leaf).serialize(StreamSerializationContext(fd))
    return {'kit': kit, 'exhibit': exhibit, 'proof': proof, 'headers': kit / 'headers.bin',
            'chain': chain, 'root': root.msg, 'anchor_height': anchor_height,
            'checkpoint': checkpoint, 'not_before': anchor_height - 1}


class Test_real_proofs(unittest.TestCase):
    """The reference deployment's proofs against real mainnet headers."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def headers_file(self, fixture):
        start, raw, rows = read_fixture_headers(fixture)
        path = os.path.join(self.tmpdir.name, fixture.replace('.txt', '.bin'))
        with open(path, 'wb') as fd:
            fd.write(raw)
        return start, path, rows

    def test_gateway_proof_959459_is_incomplete_without_a_checkpoint_and_holds_with_one(self):
        start, headers, rows = self.headers_file('mainnet-959450-959465.txt')
        # A file that starts after genesis, with no checkpoint stated: every
        # check runs and passes, and the verdict is INCOMPLETE (exit 2),
        # never HOLDS; the file's first header is named for the expert to
        # compare and state.
        code, out = run_verifier('--digest', 'e7783786ddd776a96d7dbc2fcc628b38c0e9fd758fb5c9be08d162a1a79c96e5',
                                 FIXTURES / 'gateway-959459.ots', headers, '--start-height', start)
        self.assertEqual(code, 2, out)
        self.assertIn('VERDICT: INCOMPLETE', out)
        self.assertNotIn('VERDICT: HOLDS', out)
        self.assertIn('[5]', out)
        self.assertIn('nothing ties', out)
        self.assertIn(rows[0][1], out)    # the file's first header, named for comparison
        # The tool proposes the file's LAST header (at or after the anchor).
        self.assertIn('--checkpoint %s:%s' % (rows[-1][0], rows[-1][1]), out)
        # --digest mode says the exhibit itself was not hashed here.
        self.assertIn('exhibit not hashed', out)
        self.assertIn('TRUST', out)
        # A checkpoint on the file's first header, BELOW the anchor, pins
        # nothing above itself: still INCOMPLETE, and the tool says why.
        code, out = run_verifier('--digest', 'e7783786ddd776a96d7dbc2fcc628b38c0e9fd758fb5c9be08d162a1a79c96e5',
                                 FIXTURES / 'gateway-959459.ots', headers, '--start-height', start,
                                 '--checkpoint', '%s:%s' % (rows[0][0], rows[0][1]))
        self.assertEqual(code, 2, out)
        self.assertIn('VERDICT: INCOMPLETE', out)
        self.assertNotIn('VERDICT: HOLDS', out)
        self.assertIn('BELOW the attested block 959459', out)
        self.assertIn('not thereby mainnet\'s chain', out)
        self.assertIn('--checkpoint %s:%s' % (rows[-1][0], rows[-1][1]), out)
        # Stated at or after the anchor, the same file HOLDS.
        code, out = run_verifier('--digest', 'e7783786ddd776a96d7dbc2fcc628b38c0e9fd758fb5c9be08d162a1a79c96e5',
                                 FIXTURES / 'gateway-959459.ots', headers, '--start-height', start,
                                 '--checkpoint', '%s:%s' % (rows[9][0], rows[9][1]))
        self.assertEqual(code, 0, out)
        self.assertEqual(rows[9][0], '959459')
        self.assertIn('VERDICT: HOLDS', out)
        self.assertIn('block 959459 is at the checkpoint', out)
        self.assertIn('2b39ee255a38f17547d3267f9b6ef34fe8f22a9ccce20e969977bab345ff25b7', out)
        self.assertIn('8b558dfc958ea43d563d5b2777e3aec3b443d935cd77cb662e01d4ec1ec05273', out)
        self.assertIn('all 16 headers checked', out)
        self.assertIn('compare', out)

    def test_a_checkpoint_after_the_attested_block_still_checks_that_block(self):
        # The expert states the newest header they compared (959465, six
        # blocks past the anchor): the anchor block is authenticated by the
        # links back from it, and the tool says so.
        start, headers, rows = self.headers_file('mainnet-959450-959465.txt')
        code, out = run_verifier('--digest', 'e7783786ddd776a96d7dbc2fcc628b38c0e9fd758fb5c9be08d162a1a79c96e5',
                                 FIXTURES / 'gateway-959459.ots', headers, '--start-height', start,
                                 '--checkpoint', '%s:%s' % (rows[-1][0], rows[-1][1]))
        self.assertEqual(code, 0, out)
        self.assertIn('VERDICT: HOLDS', out)
        self.assertIn('all 16 headers checked', out)
        self.assertIn('heights 959450..959464 by the links back from it', out)
        self.assertIn('block 959459 is below the checkpoint', out)
        self.assertIn('Headers above it are chained forward only and prove nothing', out)

    def test_record_proof_960458_holds_with_the_right_checkpoint(self):
        start, headers, rows = self.headers_file('mainnet-960450-960465.txt')
        checkpoint = '%s:%s' % (rows[-1][0], rows[-1][1])
        code, out = run_verifier('--digest', '5dfa4389213eac0fbfff5261f0ba2263a71b9616847e221970258bfc9e27e22e',
                                 FIXTURES / 'record-960458.ots', headers, '--start-height', start,
                                 '--checkpoint', checkpoint)
        self.assertEqual(code, 0, out)
        self.assertIn('VERDICT: HOLDS', out)
        self.assertIn('checkpoint 960465', out)
        self.assertIn('4553df11e0f654429af4293cfb2ac072cd1419e5e493e547c74c565bea6a61c4', out)
        # The file's first header as the checkpoint is below the anchor: INCOMPLETE.
        code, out = run_verifier('--digest', '5dfa4389213eac0fbfff5261f0ba2263a71b9616847e221970258bfc9e27e22e',
                                 FIXTURES / 'record-960458.ots', headers, '--start-height', start,
                                 '--checkpoint', '%s:%s' % (rows[0][0], rows[0][1]))
        self.assertEqual(code, 2, out)
        self.assertIn('VERDICT: INCOMPLETE', out)

    def test_checkpoint_mismatch_is_reported_not_hidden(self):
        start, headers, rows = self.headers_file('mainnet-960450-960465.txt')
        wrong = '%s:%s' % (rows[0][0], '00' * 32)
        code, out = run_verifier('--digest', '5dfa4389213eac0fbfff5261f0ba2263a71b9616847e221970258bfc9e27e22e',
                                 FIXTURES / 'record-960458.ots', headers, '--start-height', start,
                                 '--checkpoint', wrong)
        self.assertEqual(code, 1, out)
        self.assertIn('CHECKPOINT MISMATCH', out)
        self.assertIn(rows[0][1], out)      # what the file actually holds
        self.assertIn('00' * 32, out)       # what was claimed
        self.assertIn('VERDICT: FAILS', out)

    def test_wrong_digest_fails_at_the_first_step(self):
        start, headers, _ = self.headers_file('mainnet-959450-959465.txt')
        code, out = run_verifier('--digest', 'ab' * 32, FIXTURES / 'gateway-959459.ots', headers,
                                 '--start-height', start)
        self.assertEqual(code, 1, out)
        self.assertIn('[1]', out)
        self.assertIn('FAIL', out)
        self.assertIn('VERDICT: FAILS', out)

    def test_block_outside_the_headers_file_is_a_clear_failure(self):
        start, headers, _ = self.headers_file('mainnet-960450-960465.txt')
        code, out = run_verifier('--digest', 'e7783786ddd776a96d7dbc2fcc628b38c0e9fd758fb5c9be08d162a1a79c96e5',
                                 FIXTURES / 'gateway-959459.ots', headers, '--start-height', start)
        self.assertEqual(code, 1, out)
        self.assertIn('959459', out)
        self.assertIn('not in the headers file', out)


class Test_headers_chain(unittest.TestCase):
    def test_genesis_run_verifies_from_the_real_genesis_block(self):
        start, raw, rows = read_fixture_headers('mainnet-0-5.txt')
        self.assertEqual(start, 0)
        result = verify_claim.verify_chain(raw, 0)
        self.assertTrue(result.ok, result.problems)
        self.assertEqual(result.trust, 'genesis')
        self.assertEqual(result.authenticated_height, 0)   # genesis pins nothing above itself
        self.assertEqual(verify_claim.display(result.first_hash), verify_claim.GENESIS_HASH)
        self.assertEqual(result.checked, 6)
        # The same file from height 1, tied to nothing: sound, not trusted.
        result = verify_claim.verify_chain(raw[80:], 1)
        self.assertTrue(result.ok, result.problems)
        self.assertIsNone(result.trust)
        # A checkpoint on the last header pins the whole file, including
        # every header before it.
        _, _, rows = read_fixture_headers('mainnet-0-5.txt')
        result = verify_claim.verify_chain(raw[80:], 1, checkpoint=(5, bytes.fromhex(rows[5][1])[::-1]))
        self.assertTrue(result.ok, result.problems)
        self.assertEqual(result.trust, 'checkpoint')
        self.assertEqual(result.checked, 5)
        self.assertEqual(result.authenticated_height, 5)
        # Genesis plus a checkpoint: the checkpoint's height is what is pinned.
        result = verify_claim.verify_chain(raw, 0, checkpoint=(3, bytes.fromhex(rows[3][1])[::-1]))
        self.assertEqual((result.trust, result.authenticated_height), ('genesis', 3))
        # Any other first header is not Bitcoin.
        tampered = bytearray(raw)
        tampered[76] ^= 1
        result = verify_claim.verify_chain(bytes(tampered), 0)
        self.assertFalse(result.ok)
        self.assertTrue(any('genesis' in p for p in result.problems), result.problems)

    def test_retarget_rule_reproduces_mainnet_at_965664(self):
        _, _, rows = read_fixture_headers('mainnet-retarget-965664.txt')
        by_height = {int(r[0]): bytes.fromhex(r[2]) for r in rows}
        first, last, new = by_height[963648], by_height[965663], by_height[965664]
        expected = verify_claim.next_bits(first, last)
        self.assertEqual(expected, verify_claim.header_bits(new))
        self.assertNotEqual(expected, verify_claim.header_bits(last))   # the difficulty did move
        # Between retargets the bits must not move at all.
        self.assertEqual(verify_claim.header_bits(by_height[965665]), verify_claim.header_bits(new))

    def test_compact_encoding_round_trips(self):
        for bits in (0x1d00ffff, 0x1b0404cb, 0x1703a30c, 0x1f00ffff, 0x207fffff):
            self.assertEqual(verify_claim.encode_bits(verify_claim.decode_bits(bits)), bits, hex(bits))

    def test_network_constants_are_bitcoin_cores(self):
        # Pinned against python-bitcoinlib's chain parameters (a
        # dependency of the server, never of the verifier).
        from bitcoin.core import CoreMainParams, CoreRegTestParams, b2lx
        for name, params in (('mainnet', CoreMainParams), ('regtest', CoreRegTestParams)):
            self.assertEqual(verify_claim.NETWORKS[name]['genesis'], b2lx(params.GENESIS_BLOCK.GetHash()), name)
            self.assertEqual(verify_claim.NETWORKS[name]['pow_limit'], params.PROOF_OF_WORK_LIMIT, name)

    def test_target_rules_match_bitcoin_core(self):
        """pow.cpp CheckProofOfWork: negative, zero, overflow and above
        powLimit are refused; a genuine mainnet bits field is not."""
        def header(bits):
            return struct.pack('<L', 1) + bytes(64) + struct.pack('<LLL', 1231006505, bits, 0)
        cases = (
            (0x1d80ffff, 'negative target'),          # sign bit set, mantissa nonzero
            (0x1d000000, 'zero target'),              # mantissa zero
            (0x00800000, 'zero target'),              # sign bit with a zero mantissa is zero, not negative (Core)
            (0x2200ffff, 'target overflow'),          # > 256 bits
            (0x2301ffff, 'target overflow'),
            (0x1e00ffff, 'above the mainnet powLimit'),
        )
        for bits, rule in cases:
            with self.assertRaises(ValueError, msg=hex(bits)) as caught:
                verify_claim.check_target(bits)
            self.assertIn(rule, str(caught.exception), hex(bits))
            result = verify_claim.verify_chain(header(bits), 1)
            self.assertFalse(result.ok, hex(bits))
            self.assertTrue(any(rule in p and 'height 1' in p for p in result.problems), result.problems)
        # The genesis target is below powLimit (Core: 00000000ffff0000... < 00000000ffff...ffff).
        self.assertEqual(verify_claim.check_target(0x1d00ffff), verify_claim.decode_bits(0x1d00ffff))
        self.assertLess(verify_claim.check_target(0x1d00ffff), verify_claim.POW_LIMIT)
        self.assertEqual(verify_claim.POW_LIMIT, int('00000000' + 'ff' * 28, 16))
        self.assertEqual(verify_claim.check_target(0x1703a30c), verify_claim.decode_bits(0x1703a30c))
        # An easy target is above mainnet's powLimit and within regtest's.
        with self.assertRaises(ValueError):
            verify_claim.check_target(EASY_BITS)
        self.assertEqual(verify_claim.check_target(EASY_BITS, 'regtest'), EASY_TARGET)
        # The exporter refuses by the same rules.
        self.assertIn('target overflow', export_headers.check_header(header(0x2200ffff), 1, None, None))
        self.assertIn('powLimit', export_headers.check_header(header(EASY_BITS), 1, None, None))


class Test_worked_example(unittest.TestCase):
    """The notary's diary as an exhibit, and every tamper the kit must catch."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.kit = make_kit(cls.tmpdir.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmpdir.cleanup()

    def verify(self, exhibit=None, proof=None, headers=None, *extra, checkpoint=True, network='regtest'):
        """The kit's chain is mined at an easy difficulty, so it is checked
        as regtest (by name: as mainnet it fails the powLimit rule), with
        its tip stated as the checkpoint — at or after the anchor, as the
        rule requires (without one the verdict is INCOMPLETE)."""
        k = self.kit
        tip = k['anchor_height'] + 6
        args = ['--start-height', k['checkpoint'], '--network', network]
        if checkpoint:
            args += ['--checkpoint', '%d:%s' % (tip, k['chain'].display(tip))]
        return run_verifier(exhibit or k['exhibit'], proof or k['proof'], headers or k['headers'], *args, *extra)

    def copy(self, path, mutate):
        data = bytearray(path.read_bytes())
        mutate(data)
        out = pathlib.Path(self.tmpdir.name) / ('tampered-' + path.name)
        out.write_bytes(bytes(data))
        return out

    def test_the_diary_verifies_with_every_step_shown(self):
        code, out = self.verify()
        self.assertEqual(code, 0, out)
        k = self.kit
        for needle in ('[1]', '[2]', '[3]', '[4]', '[5]', 'VERDICT: HOLDS', 'block %d' % k['anchor_height'],
                       k['root'][::-1].hex(), 'not-before bound: block %d' % k['not_before'],
                       k['chain'].display(k['not_before']), 'selfstamp', 'TRUST', 'no network',
                       'NETWORK   : regtest', 'never evidence'):
            self.assertIn(needle, out)
        self.assertIn('checkpoint', out.lower())
        self.assertIn(k['chain'].display(k['anchor_height'] + 6), out)
        # The lower bound dates the construction of the commitment, never
        # the creation of the exhibit, and the verdict says so in words.
        self.assertIn("the calendar's commitment was constructed after it", out)
        self.assertIn("commitment to them was constructed after block %d" % k['not_before'], out)
        self.assertIn('dates the construction of the commitment, not the creation of the exhibit', out)
        self.assertNotIn('existed before Bitcoin block %d was mined (%s by the miner\'s clock); and after' % (k['anchor_height'], ''), out)

    def test_without_a_stated_checkpoint_the_verdict_is_incomplete(self):
        code, out = self.verify(checkpoint=False)
        self.assertEqual(code, 2, out)
        self.assertIn('VERDICT: INCOMPLETE', out)
        self.assertNotIn('VERDICT: HOLDS', out)
        self.assertIn('[4]', out)   # the chain itself was checked whole ...
        self.assertIn('INCOMPLETE', out.split('[5]')[1])   # ... but nothing ties it
        tip = self.kit['anchor_height'] + 6
        self.assertIn('--checkpoint %d:%s' % (tip, self.kit['chain'].display(tip)), out)

    def test_an_easy_chain_is_refused_as_mainnet(self):
        # The default network is mainnet; the kit's easy targets are above
        # mainnet's powLimit, so without --network regtest it FAILS.
        code, out = self.verify(network='mainnet')
        self.assertEqual(code, 1, out)
        self.assertIn('VERDICT: FAILS', out)
        self.assertIn('powLimit', out)
        self.assertNotIn('NETWORK   : regtest', out)

    def test_a_checkpoint_past_the_anchor_authenticates_the_anchor_and_catches_a_forged_one(self):
        k = self.kit
        tip = k['anchor_height'] + 6
        later = ['--checkpoint', '%d:%s' % (tip, k['chain'].display(tip))]
        code, out = self.verify(None, None, None, *later)
        self.assertEqual(code, 0, out)
        self.assertIn('VERDICT: HOLDS', out)
        self.assertIn('block %d is below the checkpoint' % k['anchor_height'], out)
        self.assertIn('not-before bound: block %d' % k['not_before'], out)
        # The attack: substitute the anchor header for one that
        # carries the exhibit's root without mining it, and hand over a
        # genuine later checkpoint. Every header is checked, so it FAILS.
        offset = (k['anchor_height'] - k['checkpoint']) * 80 + 36

        def flip(data):
            data[offset] ^= 0x01
        code, out = self.verify(None, None, self.copy(k['headers'], flip), *later)
        self.assertEqual(code, 1, out)
        self.assertIn('VERDICT: FAILS', out)
        self.assertIn('%d' % k['anchor_height'], out)
        self.assertTrue('proof of work' in out or 'merkle root' in out, out)

    def test_a_checkpoint_below_the_anchor_is_incomplete_not_holds(self):
        # The file's first header stated as the checkpoint: below the anchor,
        # so it pins nothing about it. Every check still runs; the verdict
        # is INCOMPLETE and names the rule and the header to state instead.
        k = self.kit
        code, out = self.verify(None, None, None, checkpoint=False)
        self.assertEqual(code, 2, out)
        code, out = run_verifier(k['exhibit'], k['proof'], k['headers'], '--start-height', k['checkpoint'],
                                 '--network', 'regtest',
                                 '--checkpoint', '%d:%s' % (k['checkpoint'], k['chain'].display(k['checkpoint'])))
        self.assertEqual(code, 2, out)
        self.assertIn('VERDICT: INCOMPLETE', out)
        self.assertNotIn('VERDICT: HOLDS', out)
        self.assertIn('[4]', out)
        self.assertIn('BELOW the attested block %d' % k['anchor_height'], out)
        self.assertIn('not thereby regtest\'s chain', out)
        # At the anchor itself is enough.
        code, out = run_verifier(k['exhibit'], k['proof'], k['headers'], '--start-height', k['checkpoint'],
                                 '--network', 'regtest',
                                 '--checkpoint', '%d:%s' % (k['anchor_height'], k['chain'].display(k['anchor_height'])))
        self.assertEqual(code, 0, out)
        self.assertIn('block %d is at the checkpoint' % k['anchor_height'], out)

    def test_tampered_exhibit_fails_at_step_one(self):
        def flip(data):
            data[10] ^= 0x01
        code, out = self.verify(self.copy(self.kit['exhibit'], flip))
        self.assertEqual(code, 1, out)
        self.assertIn('[1]', out)
        self.assertIn('FAIL', out)
        self.assertIn('VERDICT: FAILS', out)

    def test_tampered_proof_fails_at_the_merkle_root(self):
        proof = self.kit['proof'].read_bytes()
        at = proof.index(b'\x44' * 32)

        def flip(data):
            data[at] ^= 0x01
        code, out = self.verify(None, self.copy(self.kit['proof'], flip))
        self.assertEqual(code, 1, out)
        self.assertIn('merkle root', out)
        self.assertIn('FAIL', out)
        self.assertIn('VERDICT: FAILS', out)

    def test_tampered_header_fails_proof_of_work(self):
        k = self.kit
        offset = (k['anchor_height'] - k['checkpoint']) * 80 + 36    # the anchor header's merkle field

        def flip(data):
            data[offset] ^= 0x01
        code, out = self.verify(None, None, self.copy(k['headers'], flip))
        self.assertEqual(code, 1, out)
        self.assertIn('%d' % k['anchor_height'], out)
        self.assertTrue('proof of work' in out or 'merkle root' in out, out)
        self.assertIn('VERDICT: FAILS', out)

    def test_broken_chain_is_caught(self):
        k = self.kit
        # Re-mine a header after the anchor on a previous hash that is not
        # its predecessor's: proof of work holds, the link does not.
        height = k['anchor_height'] + 2
        index = height - k['checkpoint']
        stray = mine(b'\x77' * 32, hashlib.sha256(b'stray').digest(), 1788600000)

        def splice(data):
            data[index * 80:(index + 1) * 80] = stray
        code, out = self.verify(None, None, self.copy(k['headers'], splice))
        self.assertEqual(code, 1, out)
        self.assertIn('%d' % height, out)
        self.assertIn('does not link', out)
        self.assertIn('VERDICT: FAILS', out)

    def test_checkpoint_mismatch_on_the_synthetic_chain(self):
        k = self.kit
        code, out = self.verify(None, None, None, '--checkpoint', '%d:%s' % (k['checkpoint'], 'ff' * 32))
        self.assertEqual(code, 1, out)
        self.assertIn('CHECKPOINT MISMATCH', out)


class Test_constitution(unittest.TestCase):
    def test_the_verifier_has_no_network_and_no_third_party_imports(self):
        source = (OPS / 'verify_claim.py').read_text()
        for name in ('socket', 'urllib', 'http', 'requests', 'bitcoin', 'opentimestamps', 'subprocess'):
            self.assertNotRegex(source, r'^\s*(import|from)\s+%s\b' % name, name)


class Test_proof_parser(unittest.TestCase):
    def test_forked_proof_yields_every_attestation(self):
        # The real gateway proof keeps its pending attestation beside the
        # bitcoin path (a fork marker): both must come out, the bitcoin one
        # with the merkle path that leads to it.
        data = (FIXTURES / 'gateway-959459.ots').read_bytes()
        parsed = verify_claim.parse_proof(data)
        kinds = sorted(a.kind for a in parsed.attestations)
        self.assertEqual(kinds, ['bitcoin', 'pending'])
        bitcoin = [a for a in parsed.attestations if a.kind == 'bitcoin'][0]
        self.assertEqual(bitcoin.height, 959459)
        self.assertEqual(bitcoin.msg[::-1].hex(), '2b39ee255a38f17547d3267f9b6ef34fe8f22a9ccce20e969977bab345ff25b7')
        self.assertEqual(parsed.digest.hex(), 'e7783786ddd776a96d7dbc2fcc628b38c0e9fd758fb5c9be08d162a1a79c96e5')

    def test_unknown_op_is_refused_not_guessed(self):
        data = bytearray((FIXTURES / 'record-960458.ots').read_bytes())
        head = len(verify_claim.MAGIC) + 1 + 1 + 32
        data[head] = 0x03   # ripemd160: the calendar never emits it
        with self.assertRaises(verify_claim.ProofError):
            verify_claim.parse_proof(bytes(data))


class FakeNode:
    """A bitcoind for the exporter: batch RPC over a synthetic chain"""

    def __init__(self, chain):
        self.chain = chain
        self.calls = []

    def batch(self, url, calls):
        self.calls.append([m for m, _ in calls])
        out = []
        for method, params in calls:
            if method == 'getblockcount':
                out.append(len(self.chain.headers) - 1)
            elif method == 'getblockhash':
                out.append(self.chain.display(params[0]))
            elif method == 'getblockheader':
                height = [h for h in range(len(self.chain.headers)) if self.chain.display(h) == params[0]][0]
                out.append(self.chain.headers[height].hex())
            else:
                raise AssertionError(method)
        return out


class Test_export_headers(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.out = os.path.join(self.tmpdir.name, 'headers.bin')
        self.chain = SyntheticChain(0, 12)
        self.node = FakeNode(self.chain)

    def export(self, *extra):
        log = io.StringIO()
        with mock.patch.object(export_headers, 'rpc_batch', self.node.batch):
            code = export_headers.main(['--out', self.out, '--rpc-url', 'http://u:p@127.0.0.1:8332', '--batch', '5',
                                        '--network', 'regtest'] + [str(a) for a in extra], out=log)
        return code, log.getvalue()

    def test_an_easy_chain_is_refused_unless_the_network_is_stated(self):
        log = io.StringIO()
        with mock.patch.object(export_headers, 'rpc_batch', self.node.batch):
            code = export_headers.main(['--out', self.out, '--rpc-url', 'http://u:p@127.0.0.1:8332', '--batch', '5'],
                                       out=log)
        self.assertEqual(code, 1, log.getvalue())
        self.assertIn('powLimit', log.getvalue())
        self.assertFalse(os.path.exists(self.out))

    def test_first_export_writes_the_whole_chain_and_says_so(self):
        code, log = self.export()
        self.assertEqual(code, 0, log)
        self.assertEqual(open(self.out, 'rb').read(), self.chain.raw())
        self.assertIn('appended 12 headers', log)
        self.assertIn('tip height 11', log)
        self.assertIn(self.chain.display(11), log)

    def test_second_run_appends_only_what_is_new(self):
        self.export()
        self.chain.extend(hashlib.sha256(b'new').digest())
        self.chain.extend(hashlib.sha256(b'newer').digest())
        code, log = self.export()
        self.assertEqual(code, 0, log)
        self.assertEqual(open(self.out, 'rb').read(), self.chain.raw())
        self.assertIn('appended 2 headers', log)
        code, log = self.export()
        self.assertEqual(code, 0, log)
        self.assertIn('appended 0 headers', log)

    def test_a_reorg_at_the_tail_truncates_and_reappends(self):
        self.export()
        # The node now disagrees from height 10 on.
        self.chain.headers = self.chain.headers[:10]
        self.chain.extend(hashlib.sha256(b'reorg 10').digest())
        self.chain.extend(hashlib.sha256(b'reorg 11').digest())
        self.chain.extend(hashlib.sha256(b'reorg 12').digest())
        code, log = self.export()
        self.assertEqual(code, 0, log)
        self.assertEqual(open(self.out, 'rb').read(), self.chain.raw())
        self.assertIn('reorg', log)
        self.assertIn('height 10', log)

    def test_a_header_that_does_not_link_or_mine_is_refused_unwritten(self):
        self.export()
        before = open(self.out, 'rb').read()
        # A node answer whose header is not mined: nothing is appended.
        self.chain.headers.append(b'\x00' * 80)
        code, log = self.export()
        self.assertEqual(code, 1, log)
        self.assertEqual(open(self.out, 'rb').read(), before)
        self.assertIn('refused', log)

    def test_short_writes_are_completed(self):
        # os.write's count must not be ignored: 81 bytes of a batch must not
        # be reported as the whole batch. Every byte lands.
        real = os.write
        with mock.patch.object(export_headers.os, 'write', side_effect=lambda fd, data: real(fd, data[:81])):
            code, log = self.export()
        self.assertEqual(code, 0, log)
        self.assertEqual(open(self.out, 'rb').read(), self.chain.raw())
        self.assertIn('appended 12 headers', log)

    def test_an_incomplete_tail_is_cut_back_and_the_export_continues(self):
        self.export()
        with open(self.out, 'ab') as fd:
            fd.write(b'\x00' * 37)   # an interrupted append
        self.chain.extend(hashlib.sha256(b'new').digest())
        code, log = self.export()
        self.assertEqual(code, 0, log)
        self.assertIn('recovered', log)
        self.assertIn('37 bytes', log)
        self.assertEqual(open(self.out, 'rb').read(), self.chain.raw())
        code, log = self.export()
        self.assertEqual(code, 0, log)
        self.assertIn('appended 0 headers', log)

    def test_a_write_that_makes_no_progress_is_refused_and_the_next_run_recovers(self):
        with mock.patch.object(export_headers.os, 'write', return_value=0):
            code, log = self.export()
        self.assertEqual(code, 1, log)
        self.assertIn('refused', log)
        self.assertIn('failed', log)
        code, log = self.export()
        self.assertEqual(code, 0, log)
        self.assertEqual(open(self.out, 'rb').read(), self.chain.raw())

    def test_a_second_writer_is_refused_across_processes(self):
        holder = subprocess.Popen(
            [sys.executable, '-c',
             "import fcntl, os, sys, time\n"
             "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)\n"
             "fcntl.flock(fd, fcntl.LOCK_EX)\n"
             "print('held', flush=True)\n"
             "time.sleep(float(sys.argv[2]))\n", self.out + '.lock', '30'],
            stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), 'held')
            code, log = self.export()
            self.assertEqual(code, 1, log)
            self.assertIn('another export holds', log)
            self.assertFalse(os.path.exists(self.out))
        finally:
            holder.kill()
            holder.wait()
        code, log = self.export()
        self.assertEqual(code, 0, log)
        self.assertEqual(open(self.out, 'rb').read(), self.chain.raw())

    def test_the_export_verifies_with_the_verifier(self):
        self.export()
        raw = open(self.out, 'rb').read()
        # Not Bitcoin's genesis (a synthetic chain), and the verifier says so
        # before anything else.
        result = verify_claim.verify_chain(raw, 0)
        self.assertFalse(result.ok)
        self.assertTrue(any('genesis' in p for p in result.problems))
        self.assertEqual(len(result.problems), 1, result.problems)
        # The file past its first header, from a stated checkpoint, as
        # regtest: internally sound and tied to the checkpoint. As mainnet
        # its easy targets fail the powLimit rule.
        result = verify_claim.verify_chain(raw[80:], 1, checkpoint=(3, self.chain.hash(3)), network='regtest')
        self.assertTrue(result.ok, result.problems)
        self.assertEqual(result.trust, 'checkpoint')
        result = verify_claim.verify_chain(raw[80:], 1, checkpoint=(3, self.chain.hash(3)))
        self.assertFalse(result.ok)
        self.assertTrue(any('powLimit' in p for p in result.problems), result.problems)


def bare_proof(digest, height):
    """A proof with no ops: the attested merkle root is the digest itself"""
    payload = selfstamp.varuint(height)
    return verify_claim.MAGIC + b'\x01\x08' + digest + b'\x00' + verify_claim.BITCOIN_TAG + selfstamp.varuint(len(payload)) + payload


class Test_incompatible_forks(unittest.TestCase):
    """Two regtest forks off one common prefix (heights 100..103), each
    mined on past it with its own exhibit anchored at height 105. Both are
    sound proof-of-work chains; at most one can be the chain the expert's
    public source shows. A checkpoint on the common prefix cannot tell
    them apart, so it must not let either HOLD (before this fix it let
    both); a checkpoint at a fork's own tip lets exactly that fork's
    exhibit HOLD and fails the other with CHECKPOINT MISMATCH."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.dir = pathlib.Path(self.tmpdir.name)
        common = SyntheticChain(100, 4)
        self.kits = {}
        for name in ('a', 'b'):
            exhibit = self.dir / ('exhibit-' + name)
            exhibit.write_bytes(b'exhibit %s, written during the test' % name.encode())
            digest = hashlib.sha256(exhibit.read_bytes()).digest()
            fork = SyntheticChain(100, 0)
            fork.headers = list(common.headers)
            fork.extend(hashlib.sha256(b'fork %s 104' % name.encode()).digest())   # 104: the forks diverge here
            fork.extend(digest)                                                    # 105: the anchor block
            fork.extend(hashlib.sha256(b'fork %s 106' % name.encode()).digest())   # 106: the tip
            (self.dir / ('headers-%s.bin' % name)).write_bytes(fork.raw())
            (self.dir / ('proof-%s.ots' % name)).write_bytes(bare_proof(digest, 105))
            self.kits[name] = fork
        self.assertNotEqual(self.kits['a'].hash(104), self.kits['b'].hash(104))
        self.assertEqual(self.kits['a'].hash(103), self.kits['b'].hash(103))

    def verify(self, name, headers_of, checkpoint_height, checkpoint_of=None):
        checkpoint_of = checkpoint_of or headers_of
        return run_verifier(self.dir / ('exhibit-' + name), self.dir / ('proof-%s.ots' % name),
                            self.dir / ('headers-%s.bin' % headers_of), '--start-height', 100, '--network', 'regtest',
                            '--checkpoint', '%d:%s' % (checkpoint_height, self.kits[checkpoint_of].display(checkpoint_height)))

    def test_a_checkpoint_on_the_common_prefix_lets_neither_fork_hold(self):
        for name in ('a', 'b'):
            code, out = self.verify(name, name, 103)
            self.assertEqual(code, 2, out)
            self.assertIn('VERDICT: INCOMPLETE', out)
            self.assertNotIn('HOLDS', out)
            self.assertIn('[4]', out)                       # the chain itself is sound ...
            self.assertIn('BELOW the attested block 105', out)   # ... but the checkpoint pins nothing above 103
            self.assertIn('anyone can extend a fork past a checkpoint', out)
            self.assertIn('--checkpoint 106:%s' % self.kits[name].display(106), out)

    def test_a_checkpoint_at_a_forks_tip_authenticates_that_fork_alone(self):
        for height in (105, 106):
            code, out = self.verify('a', 'a', height)
            self.assertEqual(code, 0, out)
            self.assertIn('VERDICT: HOLDS', out)
            # The other fork's exhibit against fork A's checkpoint: its
            # file disagrees at the checkpoint height.
            code, out = self.verify('b', 'b', height, checkpoint_of='a')
            self.assertEqual(code, 1, out)
            self.assertIn('CHECKPOINT MISMATCH', out)
            self.assertNotIn('HOLDS', out)
        # And with fork B's headers under fork A's proof, the merkle root is
        # not there at all: FAILS at step 3.
        code, out = self.verify('a', 'b', 106)
        self.assertEqual(code, 1, out)
        self.assertIn('[3]', out)
        self.assertIn('FAIL', out)

    def test_genesis_alone_does_not_authenticate_a_later_block(self):
        # A regtest chain from its real genesis, no checkpoint: the file is
        # tied to regtest at height 0 and nothing above, so an anchor at
        # height 1 is INCOMPLETE — the rule is the same as for mainnet.
        genesis_hash = bytes.fromhex(verify_claim.NETWORKS['regtest']['genesis'])[::-1]
        exhibit = self.dir / 'exhibit-g'
        exhibit.write_bytes(b'anchored right after genesis')
        digest = hashlib.sha256(exhibit.read_bytes()).digest()
        header1 = mine(genesis_hash, digest, 1296688702 + 600)
        # The real regtest genesis header, from python-bitcoinlib's chain params.
        from bitcoin.core import CoreRegTestParams
        genesis = CoreRegTestParams.GENESIS_BLOCK.get_header().serialize()
        self.assertEqual(sha256d(genesis), genesis_hash)
        (self.dir / 'headers-g.bin').write_bytes(genesis + header1)
        (self.dir / 'proof-g.ots').write_bytes(bare_proof(digest, 1))
        code, out = run_verifier(exhibit, self.dir / 'proof-g.ots', self.dir / 'headers-g.bin', '--network', 'regtest')
        self.assertEqual(code, 2, out)
        self.assertIn('VERDICT: INCOMPLETE', out)
        self.assertIn('the genesis block is BELOW the attested block 1', out)
        code, out = run_verifier(exhibit, self.dir / 'proof-g.ots', self.dir / 'headers-g.bin', '--network', 'regtest',
                                 '--checkpoint', '1:' + verify_claim.display(sha256d(header1)))
        self.assertEqual(code, 0, out)
        self.assertIn('the genesis block also matched', out)


class Test_hostile_kits(unittest.TestCase):
    """A hostile kit: a new exhibit, its digest substituted into the
    genuine block-1 header without mining it, and genuine mainnet block 5
    handed over as the checkpoint. Were only block 5 checked, the exhibit
    would have "existed" in 2009."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.dir = pathlib.Path(self.tmpdir.name)
        _, _, rows = read_fixture_headers('mainnet-0-5.txt')
        self.rows = rows
        self.headers = [bytes.fromhex(r[2]) for r in rows]
        self.exhibit = self.dir / 'exhibit'
        self.exhibit.write_bytes(b'This statement was created during the review, not in 2009.')
        self.digest = hashlib.sha256(self.exhibit.read_bytes()).digest()
        forged = bytearray(self.headers[1])
        forged[36:68] = self.digest
        self.headers[1] = bytes(forged)
        self.assertGreater(int.from_bytes(sha256d(self.headers[1]), 'little'),
                           verify_claim.decode_bits(verify_claim.header_bits(self.headers[1])),
                           'the substituted header is not mined')
        payload = selfstamp.varuint(1)
        (self.dir / 'proof.ots').write_bytes(verify_claim.MAGIC + b'\x01\x08' + self.digest + b'\x00'
                                             + verify_claim.BITCOIN_TAG + selfstamp.varuint(len(payload)) + payload)

    def test_forged_block_one_fails_under_a_genuine_later_checkpoint(self):
        (self.dir / 'headers.bin').write_bytes(b''.join(self.headers))
        code, out = run_verifier(self.exhibit, self.dir / 'proof.ots', self.dir / 'headers.bin',
                                 '--checkpoint', '5:' + self.rows[5][1])
        self.assertEqual(code, 1, out)
        self.assertIn('VERDICT: FAILS', out)
        self.assertNotIn('HOLDS', out)
        self.assertIn('height 1', out)
        self.assertIn('proof of work', out)

    def test_forged_block_one_fails_when_the_file_starts_after_genesis_too(self):
        # No genesis in the file: the checkpoint on block 5 pins block 1
        # by the links back from it, and the substitute is caught.
        (self.dir / 'headers.bin').write_bytes(b''.join(self.headers[1:]))
        code, out = run_verifier(self.exhibit, self.dir / 'proof.ots', self.dir / 'headers.bin',
                                 '--start-height', 1, '--checkpoint', '5:' + self.rows[5][1])
        self.assertEqual(code, 1, out)
        self.assertIn('VERDICT: FAILS', out)
        self.assertIn('height 1', out)
        self.assertIn('proof of work', out)
        # And without any checkpoint it is not HOLDS either: the chain check
        # itself fails first.
        code, out = run_verifier(self.exhibit, self.dir / 'proof.ots', self.dir / 'headers.bin', '--start-height', 1)
        self.assertEqual(code, 1, out)
        self.assertNotIn('HOLDS', out)


if __name__ == "__main__":
    unittest.main()
