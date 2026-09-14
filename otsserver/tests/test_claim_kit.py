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

"""The claim kit (green review 2026-09-11, item 8): ops/verify_claim.py and
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
"""

import datetime
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import struct
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

    def test_gateway_proof_959459_holds(self):
        start, headers, rows = self.headers_file('mainnet-959450-959465.txt')
        code, out = run_verifier('--digest', 'e7783786ddd776a96d7dbc2fcc628b38c0e9fd758fb5c9be08d162a1a79c96e5',
                                 FIXTURES / 'gateway-959459.ots', headers, '--start-height', start)
        self.assertEqual(code, 0, out)
        self.assertIn('VERDICT: HOLDS', out)
        self.assertIn('block 959459', out)
        self.assertIn('2b39ee255a38f17547d3267f9b6ef34fe8f22a9ccce20e969977bab345ff25b7', out)
        self.assertIn('8b558dfc958ea43d563d5b2777e3aec3b443d935cd77cb662e01d4ec1ec05273', out)
        # --digest mode says the exhibit itself was not hashed here.
        self.assertIn('exhibit not hashed', out)
        # The trust it relies on is stated: a checkpoint, compare to a public source.
        self.assertIn('TRUST', out)
        self.assertIn(rows[0][1], out)    # the file's first header, named as the checkpoint
        self.assertIn('compare', out)

    def test_record_proof_960458_holds_with_the_right_checkpoint(self):
        start, headers, rows = self.headers_file('mainnet-960450-960465.txt')
        checkpoint = '%s:%s' % (rows[0][0], rows[0][1])
        code, out = run_verifier('--digest', '5dfa4389213eac0fbfff5261f0ba2263a71b9616847e221970258bfc9e27e22e',
                                 FIXTURES / 'record-960458.ots', headers, '--start-height', start,
                                 '--checkpoint', checkpoint)
        self.assertEqual(code, 0, out)
        self.assertIn('VERDICT: HOLDS', out)
        self.assertIn('checkpoint 960450', out)
        self.assertIn('4553df11e0f654429af4293cfb2ac072cd1419e5e493e547c74c565bea6a61c4', out)

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
        self.assertEqual(verify_claim.display(result.first_hash), verify_claim.GENESIS_HASH)
        self.assertEqual(result.checked, 6)
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


class Test_worked_example(unittest.TestCase):
    """The notary's diary as an exhibit, and every tamper the kit must catch."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.kit = make_kit(cls.tmpdir.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmpdir.cleanup()

    def verify(self, exhibit=None, proof=None, headers=None, *extra):
        k = self.kit
        return run_verifier(exhibit or k['exhibit'], proof or k['proof'], headers or k['headers'],
                            '--start-height', k['checkpoint'], *extra)

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
        for needle in ('[1]', '[2]', '[3]', '[4]', 'VERDICT: HOLDS', 'block %d' % k['anchor_height'],
                       k['root'][::-1].hex(), 'not-before bound: block %d' % k['not_before'],
                       k['chain'].display(k['not_before']), 'selfstamp', 'TRUST', 'no network'):
            self.assertIn(needle, out)
        self.assertIn('checkpoint', out.lower())
        self.assertIn(k['chain'].display(k['checkpoint']), out)

    def test_the_diary_verifies_as_a_stated_checkpoint(self):
        k = self.kit
        code, out = self.verify(None, None, None, '--checkpoint',
                                '%d:%s' % (k['checkpoint'], k['chain'].display(k['checkpoint'])))
        self.assertEqual(code, 0, out)
        self.assertIn('checkpoint %d' % k['checkpoint'], out)

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
            code = export_headers.main(['--out', self.out, '--rpc-url', 'http://u:p@127.0.0.1:8332', '--batch', '5']
                                       + [str(a) for a in extra], out=log)
        return code, log.getvalue()

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

    def test_the_export_verifies_with_the_verifier(self):
        self.export()
        result = verify_claim.verify_chain(open(self.out, 'rb').read(), 0)
        # Not Bitcoin's genesis (a synthetic chain), and the verifier says so.
        self.assertFalse(result.ok)
        self.assertTrue(any('genesis' in p for p in result.problems))
        # From a stated checkpoint the synthetic chain is internally sound.
        result = verify_claim.verify_chain(open(self.out, 'rb').read(), 0, checkpoint=(3, self.chain.hash(3)))
        self.assertTrue(result.ok, result.problems)


if __name__ == "__main__":
    unittest.main()
