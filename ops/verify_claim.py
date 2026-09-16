#!/usr/bin/env python3
"""Claim kit verifier: one exhibit, its proof, a block-headers file; python3 only.

    python3 verify_claim.py EXHIBIT EXHIBIT.ots headers.bin [--start-height N]
                            [--checkpoint HEIGHT:HASH] [--network mainnet|regtest]
    python3 verify_claim.py --digest SHA256HEX PROOF.ots headers.bin ...

Steps, every one printed:
  [1] the exhibit's sha256 is the digest the proof is about;
  [2] every operation in the proof is replayed from that digest, through the
      calendar's commitment (with the not-before bound, when the proof has
      one), the anchor transaction and the block's merkle path, to the
      merkle root the Bitcoin attestation names;
  [3] the block at the attested height, read from the headers file, carries
      exactly that merkle root;
  [4] the headers file is one chain, checked whole: every header meets
      Bitcoin Core's target rules and its own proof of work, links to the
      header before it, and keeps the difficulty rule (bits unchanged
      between retargets; at each retarget, Bitcoin's adjustment from the
      period's timestamps). Nothing is skipped: the attested block and the
      block a not-before bound rests on are checked like every other;
  [5] what ties the attested block to Bitcoin: a checkpoint the expert
      states (--checkpoint HEIGHT:HASH, compared by them to a public
      source) at or after the attested height. A checkpoint pins every
      header BEFORE it: each header's bytes are the preimage of the next
      header's previous-hash field, so the links back from the checkpoint
      authenticate the attested block. Headers AFTER a checkpoint are tied
      to it only by following links forward, and a chain that follows the
      rules is not thereby Bitcoin's chain: anyone can extend a fork past
      a checkpoint (at real difficulty a miner; at regtest difficulty
      anyone), so a checkpoint below the attested block authenticates
      nothing about it. The genesis block, hardcoded here, is a checkpoint
      at height 0 and pins nothing above it. A file with no checkpoint at
      or after the attested height yields INCOMPLETE, never HOLDS.
Exit 0 only if everything holds; 1 when any check fails; 2 when every check
that could run passed but nothing ties the attested block to Bitcoin (state
a checkpoint at or after it). No network, no third-party modules. The trust
the verdict relies on is printed with it. Verification against a node
(`ots verify` with bitcoind) is the other path and needs no checkpoint.

What the verdict means: the exhibit's bytes existed before the attested
block was mined. With a not-before bound, the calendar's commitment to the
exhibit was constructed after the bound's block: that dates the
commitment, not the exhibit. Nothing here speaks to when the exhibit was
made, captured or received.

--network: mainnet (the default) is Bitcoin and the only network an
expert is ever handed. regtest is for chains mined at an easy difficulty
(the test suite's) and is never evidence; it must be asked for by name, so
an easy chain can never pass as Bitcoin by accident.

The proof parser follows the OpenTimestamps detached-proof format; the
serialization primitives are copied from ops/selfstamp.py in this tree
(2026-09-14) and extended with the fork marker, so proofs the ots client
upgraded (pending attestation kept beside the bitcoin path) parse too.
"""

import argparse
import collections
import datetime
import hashlib
import os
import struct
import sys

# --- OpenTimestamps detached proofs (primitives from ops/selfstamp.py) --------

MAGIC = b'\x00OpenTimestamps\x00\x00Proof\x00\xbf\x89\xe2\xe8\x84\xe8\x92\x94'
VERSION = 1
OP_SHA256 = 0x08
OP_APPEND = 0xf0
OP_PREPEND = 0xf1
ATTESTATION_MARKER = 0x00
FORK_MARKER = 0xff
PENDING_TAG = bytes.fromhex('83dfe30d2ef90c8e')
BITCOIN_TAG = bytes.fromhex('0588960d73d71901')
OP_NAMES = {OP_SHA256: 'sha256', OP_APPEND: 'append', OP_PREPEND: 'prepend'}
# The public client's limits (opentimestamps 0.4.x), mirrored so that
# "parses" means the same here as there; ops/tests/proof_corpus.py holds
# both to them. The last one is ours: nothing valid needs more than ten
# bytes of varuint.
MAX_OPERAND = 4096
MAX_MSG = 4096
MAX_ATTESTATION_PAYLOAD = 8192
MAX_URI = 1000
URI_CHARS = frozenset(b'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._/:')
MAX_OPS_ON_A_PATH = 255
MAX_VARUINT_BYTES = 10

# --- Bitcoin networks ------------------------------------------------------------

# Bitcoin Core's chainparams.cpp, pinned in the tests against
# python-bitcoinlib. powLimit is the consensus constant (mainnet
# 00000000ffff...ffff), not the genesis block's target (00000000ffff0000...,
# which is below it); CheckProofOfWork refuses any target above it. mainnet
# retargets every 2016 blocks; regtest has no difficulty rule at all
# (fPowNoRetargeting, min-difficulty blocks), only its powLimit.
NETWORKS = {
    'mainnet': {'genesis': '000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f',
                'pow_limit': (1 << 224) - 1,
                'retarget': True},
    'regtest': {'genesis': '0f9188f13cb7b2c71f2a335e3a4fc328bf5beb436012afca590b1a11466e2206',
                'pow_limit': (1 << 255) - 1,
                'retarget': False},
}
GENESIS_HASH = NETWORKS['mainnet']['genesis']
POW_LIMIT = NETWORKS['mainnet']['pow_limit']
RETARGET_INTERVAL = 2016
TARGET_TIMESPAN = 14 * 24 * 60 * 60
HEADER_SIZE = 80


class ProofError(Exception):
    """A proof this tool cannot read: refused, never guessed at"""


Attestation = collections.namedtuple('Attestation', 'kind height uri msg path')
Parsed = collections.namedtuple('Parsed', 'digest attestations')
# trust: 'genesis' or 'checkpoint' when the chain is tied to the network,
# None when it is internally sound but tied to nothing.
ChainResult = collections.namedtuple('ChainResult', 'ok problems checked first_hash last_hash retargets notes trust authenticated_height')


def read_varuint(data, pos):
    value = 0
    shift = 0
    start = pos
    while True:
        if pos >= len(data):
            raise ProofError('truncated varuint')
        if pos - start >= MAX_VARUINT_BYTES:
            raise ProofError('varuint longer than %d bytes' % MAX_VARUINT_BYTES)
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7f) << shift
        shift += 7
        if not byte & 0x80:
            return value, pos


def read_varbytes(data, pos, max_len, min_len=0):
    length, pos = read_varuint(data, pos)
    if length > max_len:
        raise ProofError('varbytes longer than %d bytes' % max_len)
    if length < min_len:
        raise ProofError('varbytes shorter than %d byte' % min_len)
    if pos + length > len(data):
        raise ProofError('truncated varbytes')
    return data[pos:pos + length], pos + length


def _read_attestation(data, pos, msg, path):
    """The attestation whose marker byte was just read: (Attestation, end).
    A known payload is consumed to its last byte; a pending URI is at most
    MAX_URI bytes of URI_CHARS; an unknown tag is kept as 'unknown:<hex>'."""
    atag = data[pos:pos + 8]
    if len(atag) != 8:
        raise ProofError('truncated attestation tag')
    pos += 8
    payload, pos = read_varbytes(data, pos, MAX_ATTESTATION_PAYLOAD)
    if atag == PENDING_TAG:
        uri, end = read_varbytes(payload, 0, MAX_URI)
        if end != len(payload):
            raise ProofError('trailing bytes in the pending attestation')
        if any(b not in URI_CHARS for b in uri):
            raise ProofError('pending uri has a character outside the allowed set')
        return Attestation('pending', None, uri.decode('ascii'), msg, tuple(path)), pos
    if atag == BITCOIN_TAG:
        height, end = read_varuint(payload, 0)
        if end != len(payload):
            raise ProofError('trailing bytes in the bitcoin attestation')
        return Attestation('bitcoin', height, None, msg, tuple(path)), pos
    return Attestation('unknown:' + atag.hex(), None, None, msg, tuple(path)), pos


def parse_proof(data):
    """Parsed(digest, attestations): every attestation with the op path
    ((name, operand, result) per op) that leads to it from the digest.
    Raises ProofError, and only ProofError, on anything that is not one
    whole proof by the public client's rules. The walk is a loop: a
    timestamp is zero or more fork-marked branches then a last branch;
    every fork marker promises one more branch of the same timestamp after
    the branch it opens ends, so `pending` holds, per open fork, the
    message, path and operation count the sibling branch resumes with."""
    if data[:len(MAGIC)] != MAGIC:
        raise ProofError('not an OpenTimestamps proof (bad magic)')
    pos = len(MAGIC)
    version, pos = read_varuint(data, pos)
    if version != VERSION:
        raise ProofError('unsupported proof version %d' % version)
    if pos >= len(data) or data[pos] != OP_SHA256:
        raise ProofError('file hash op is not sha256')
    pos += 1
    digest = data[pos:pos + 32]
    if len(digest) != 32:
        raise ProofError('truncated digest')
    pos += 32
    out = []
    pending = []
    msg, path, ops, after_fork = digest, [], 0, False
    while True:
        if pos >= len(data):
            raise ProofError('truncated: no attestation')
        tag = data[pos]
        pos += 1
        if tag == FORK_MARKER:
            if after_fork:
                raise ProofError('a fork marker followed by another fork marker')
            pending.append((msg, path, ops))
            after_fork = True
            continue
        after_fork = False
        if tag == ATTESTATION_MARKER:
            attestation, pos = _read_attestation(data, pos, msg, path)
            out.append(attestation)
            if not pending:
                break
            msg, path, ops = pending.pop()
            continue
        if len(msg) > MAX_MSG:
            raise ProofError('message longer than %d bytes' % MAX_MSG)
        if tag == OP_SHA256:
            new = hashlib.sha256(msg).digest()
            operand = b''
        elif tag == OP_APPEND:
            operand, pos = read_varbytes(data, pos, MAX_OPERAND, min_len=1)
            new = msg + operand
        elif tag == OP_PREPEND:
            operand, pos = read_varbytes(data, pos, MAX_OPERAND, min_len=1)
            new = operand + msg
        else:
            raise ProofError('unsupported op 0x%02x: the calendar never emits it; use the ots client' % tag)
        if len(new) > MAX_OPERAND:
            raise ProofError('result longer than %d bytes' % MAX_OPERAND)
        path = path + [(OP_NAMES[tag], operand, new)]
        msg = new
        ops += 1
        if ops > MAX_OPS_ON_A_PATH:
            raise ProofError('more than %d operations on one path' % MAX_OPS_ON_A_PATH)
    if pos != len(data):
        raise ProofError('trailing bytes after the proof')
    return Parsed(digest, out)


# --- block headers --------------------------------------------------------------

def sha256d(data):
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def header_fields(header):
    version, = struct.unpack('<l', header[:4])
    ntime, bits, nonce = struct.unpack('<LLL', header[68:80])
    return {'version': version, 'prev': header[4:36], 'merkle_root': header[36:68],
            'time': ntime, 'bits': bits, 'nonce': nonce}


def header_bits(header):
    return struct.unpack('<L', header[72:76])[0]


def decode_bits(bits):
    """The 'compact' target encoding, as Bitcoin Core's arith_uint256::SetCompact:
    the value alone. check_target applies the validity rules."""
    exponent = bits >> 24
    mantissa = bits & 0x7fffff
    if exponent <= 3:
        return mantissa >> (8 * (3 - exponent))
    return mantissa << (8 * (exponent - 3))


def check_target(bits, network='mainnet'):
    """The target a bits field encodes, under Bitcoin Core's CheckProofOfWork
    rules (pow.cpp, arith_uint256::SetCompact): a negative encoding, a zero
    target, an encoding that overflows 256 bits, and any target above the
    network's powLimit are refused. Raises ValueError naming the rule."""
    exponent = bits >> 24
    mantissa = bits & 0x7fffff
    if mantissa and bits & 0x800000:
        raise ValueError('negative target')
    if mantissa and (exponent > 34 or (mantissa > 0xff and exponent > 33) or (mantissa > 0xffff and exponent > 32)):
        raise ValueError('target overflow (more than 256 bits)')
    target = decode_bits(bits)
    if target == 0:
        raise ValueError('zero target')
    if target > NETWORKS[network]['pow_limit']:
        raise ValueError('target above the %s powLimit' % network)
    return target


def encode_bits(target):
    """arith_uint256::GetCompact"""
    size = (target.bit_length() + 7) // 8
    if size <= 3:
        compact = target << (8 * (3 - size))
    else:
        compact = target >> (8 * (size - 3))
    if compact & 0x800000:
        compact >>= 8
        size += 1
    return compact | (size << 24)


def next_bits(first_header, last_header, network='mainnet'):
    """Bitcoin's difficulty adjustment (pow.cpp CalculateNextWorkRequired):
    first_header is the period's first block (height - 2016), last_header the
    one just before the retarget (height - 1)."""
    actual = header_fields(last_header)['time'] - header_fields(first_header)['time']
    actual = max(TARGET_TIMESPAN // 4, min(TARGET_TIMESPAN * 4, actual))
    target = decode_bits(header_bits(last_header)) * actual // TARGET_TIMESPAN
    return encode_bits(min(target, NETWORKS[network]['pow_limit']))


def display(hash_bytes):
    return hash_bytes[::-1].hex()


def verify_chain(raw, start_height, checkpoint=None, wanted_hashes=(), network='mainnet'):
    """Check the whole headers file, then what ties it to the network.

    Every header: Bitcoin Core's target rules, its own proof of work, the
    link to the header before it, the difficulty rule (mainnet). Then the
    tie: the network's genesis block when the file starts at height 0, or
    the stated checkpoint = (height, hash bytes), which must be in the file
    and match. Returns ChainResult; ok says the file is one sound chain,
    trust ('genesis', 'checkpoint' or None) says whether it is tied to the
    network at all, and authenticated_height says up to which height that
    tie authenticates headers: the checkpoint's height (headers at or below
    it are pinned by the links back from it), 0 for genesis alone, None for
    no tie. Headers above authenticated_height are only chained forward —
    a sound chain, not thereby Bitcoin's — and a caller must never call a
    verdict complete on an attested block above it. wanted_hashes: hashes
    to look for on the way (the not-before bound); found ones land in
    notes."""
    params = NETWORKS[network]
    problems = []
    notes = {}
    count = len(raw) // HEADER_SIZE
    if len(raw) % HEADER_SIZE:
        problems.append('headers file is not a whole number of 80-byte headers (%d bytes over)' % (len(raw) % HEADER_SIZE))
    if count == 0:
        problems.append('headers file is empty')
        return ChainResult(False, problems, 0, None, None, 0, notes, None, None)

    trust = None
    if checkpoint is not None:
        cp_height, cp_hash = checkpoint
        if not start_height <= cp_height < start_height + count:
            problems.append('checkpoint height %d is not in the headers file (heights %d..%d)'
                            % (cp_height, start_height, start_height + count - 1))
            return ChainResult(False, problems, 0, None, None, 0, notes, None, None)
        cp_index = cp_height - start_height
        actual = sha256d(raw[cp_index * HEADER_SIZE:(cp_index + 1) * HEADER_SIZE])
        if actual != cp_hash:
            problems.append('CHECKPOINT MISMATCH at height %d: the headers file holds %s, the checkpoint says %s'
                            % (cp_height, display(actual), display(cp_hash)))
            return ChainResult(False, problems, 0, None, None, 0, notes, None, None)
        trust = 'checkpoint'

    wanted = set(wanted_hashes)
    prev_hash = None
    prev_bits = None
    first_hash = None
    retargets = 0
    checked = 0
    for i in range(count):
        height = start_height + i
        header = raw[i * HEADER_SIZE:(i + 1) * HEADER_SIZE]
        this_hash = sha256d(header)
        fields = header_fields(header)
        if first_hash is None:
            first_hash = this_hash
            if height == 0:
                if display(this_hash) != params['genesis']:
                    problems.append('header 0 is not %s\'s genesis block: %s (expected %s)'
                                    % (network, display(this_hash), params['genesis']))
                    break
                trust = 'genesis'
        elif fields['prev'] != prev_hash:
            problems.append('header at height %d does not link: its previous-hash field is %s, the header before it hashes to %s'
                            % (height, display(fields['prev']), display(prev_hash)))
            break
        try:
            target = check_target(fields['bits'], network)
        except ValueError as exp:
            problems.append('header at height %d: invalid target (%s; bits 0x%08x)' % (height, exp, fields['bits']))
            break
        if int.from_bytes(this_hash, 'little') > target:
            problems.append('header at height %d fails proof of work: hash %s exceeds the target its bits field 0x%08x encodes'
                            % (height, display(this_hash), fields['bits']))
            break
        if params['retarget'] and prev_bits is not None:
            if height % RETARGET_INTERVAL:
                if fields['bits'] != prev_bits:
                    problems.append('header at height %d changes bits between retargets: 0x%08x after 0x%08x'
                                    % (height, fields['bits'], prev_bits))
                    break
            else:
                first_index = i - RETARGET_INTERVAL
                if first_index >= 0:
                    expected = next_bits(raw[first_index * HEADER_SIZE:(first_index + 1) * HEADER_SIZE],
                                         raw[(i - 1) * HEADER_SIZE:i * HEADER_SIZE], network)
                    if fields['bits'] != expected:
                        problems.append('header at height %d: bits 0x%08x, but the retarget from the period\'s timestamps gives 0x%08x'
                                        % (height, fields['bits'], expected))
                        break
                    retargets += 1
                else:
                    notes.setdefault('unchecked_retargets', []).append(height)
        if this_hash in wanted:
            notes.setdefault('found', {})[this_hash] = (height, fields['time'])
        prev_hash = this_hash
        prev_bits = fields['bits']
        checked += 1
    ok = not problems
    authenticated = None
    if ok and trust is not None:
        # The checkpoint pins everything at or below it; genesis alone pins
        # height 0. With both, the higher of the two.
        authenticated = 0
        if checkpoint is not None:
            authenticated = max(authenticated, checkpoint[0])
    return ChainResult(ok, problems, checked, first_hash, prev_hash, retargets, notes,
                       trust if ok else None, authenticated)


# --- the verdict ------------------------------------------------------------------

def _utc(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def hash_file(path):
    h = hashlib.sha256()
    size = 0
    with open(path, 'rb') as fd:
        for chunk in iter(lambda: fd.read(65536), b''):
            h.update(chunk)
            size += len(chunk)
    return h.digest(), size


def selfstamp_manifest(path, size):
    """The parsed exhibit if it is a selfstamp manifest (ops/selfstamp.py), else None"""
    if size > 1 << 20:
        return None
    try:
        import json
        with open(path, 'rb') as fd:
            data = json.loads(fd.read().decode('utf-8'))
        return data if isinstance(data, dict) and str(data.get('schema', '')).startswith('selfstamp/') else None
    except (ValueError, UnicodeDecodeError, OSError):
        return None


def describe_path(path, write):
    """Print the replayed operations with what each stage is"""
    bound = None
    clock = None
    for index, (name, operand, result) in enumerate(path):
        # The calendar's commitment: a 32-byte append then sha256 is the
        # not-before bound; the 4-byte prepend that follows is its clock.
        if (name == 'append' and len(operand) == 32 and index + 2 < len(path)
                and path[index + 1][0] == 'sha256' and path[index + 2][0] == 'prepend'
                and len(path[index + 2][1]) == 4):
            bound = operand[::-1]      # display order in the proof, internal for lookup
            write('      %-8s %s   <- not-before bound: a block hash (checked in [4])' % (name, operand.hex()))
        elif name == 'prepend' and len(operand) == 4 and index and path[index - 1][0] in ('sha256',) \
                and (index + 1 < len(path) and path[index + 1][0] == 'append' and len(path[index + 1][1]) == 8):
            clock = struct.unpack('>L', operand)[0]
            write('      %-8s %s   <- the calendar\'s own clock, %s (not evidence)' % (name, operand.hex(), _utc(clock)))
        elif name == 'sha256':
            write('      sha256')
        else:
            shown = operand.hex() if len(operand) <= 40 else operand[:12].hex() + '...' + operand[-12:].hex() + ' (%d bytes)' % len(operand)
            write('      %-8s %s' % (name, shown))
    return bound, clock


def main(argv=None, out=None):
    out = out or sys.stdout

    def write(line=''):
        out.write(line + '\n')

    parser = argparse.ArgumentParser(description='Claim kit verifier: exhibit, proof, headers; python3 only, offline.')
    parser.add_argument('exhibit', nargs='?', help='the exhibit file (or use --digest)')
    parser.add_argument('proof', help='the exhibit\'s .ots proof')
    parser.add_argument('headers', help='headers.bin: 80-byte Bitcoin block headers, consecutive')
    parser.add_argument('--digest', help='sha256 hex of the exhibit, when the exhibit itself is not in the folder')
    parser.add_argument('--start-height', type=int, default=0,
                        help='height of the first header in the file (default 0: from genesis)')
    parser.add_argument('--checkpoint', help='HEIGHT:HASH of one header in the file, compared to a public source by you; '
                                             'pins every header at or below it, so it must be at or after the attested block')
    parser.add_argument('--network', choices=sorted(NETWORKS), default='mainnet',
                        help='the chain the headers are from (default mainnet). regtest is for chains mined at an '
                             'easy difficulty, never for evidence')
    args = parser.parse_args(argv)
    if bool(args.exhibit) == bool(args.digest):
        parser.error('give the exhibit file, or --digest, not both')
    network = args.network

    failures = []

    def step(number, ok, text, detail=None):
        write('[%d] %s ... %s' % (number, text, 'OK' if ok else 'FAIL'))
        if detail:
            write('      ' + detail)
        if not ok:
            failures.append(text)

    write('claim kit verifier (ops/verify_claim.py): python3 standard library only, no network')
    if network != 'mainnet':
        write('NETWORK   : %s. This is NOT Bitcoin: a %s verdict tests a kit, it is never evidence.' % (network, network))
    write('proof     : %s (%d bytes)' % (args.proof, os.path.getsize(args.proof)))
    raw_headers = open(args.headers, 'rb').read()
    count = len(raw_headers) // HEADER_SIZE
    write('headers   : %s (%d bytes = %d headers, heights %d..%d)'
          % (args.headers, len(raw_headers), count, args.start_height, args.start_height + count - 1))

    # [1] the exhibit
    try:
        parsed = parse_proof(open(args.proof, 'rb').read())
    except (ProofError, OSError) as exp:
        write('proof cannot be read: %s' % exp)
        write('VERDICT: FAILS (the proof is unreadable)')
        return 1
    if args.exhibit:
        digest, size = hash_file(args.exhibit)
        write('exhibit   : %s (%d bytes) sha256 %s' % (args.exhibit, size, digest.hex()))
        manifest = selfstamp_manifest(args.exhibit, size)
        if manifest:
            # selfstamp/3 names a chain by an opaque label; older manifests named a host.
            named = ('chain %s' % manifest['chain']) if manifest.get('chain') else ('host %s' % manifest.get('host'))
            write('            a selfstamp manifest (the notary\'s own diary): %s, period %s, seq %s'
                  % (named, manifest.get('period'), manifest.get('seq')))
        step(1, digest == parsed.digest, 'the exhibit\'s sha256 is the digest the proof is about',
             None if digest == parsed.digest else 'the proof is about %s' % parsed.digest.hex())
    else:
        digest = bytes.fromhex(args.digest)
        write('exhibit   : not present; exhibit not hashed here. Digest given: %s' % digest.hex())
        write('            (the expert must run: sha256sum EXHIBIT  and compare it to this digest)')
        step(1, digest == parsed.digest, 'the given digest is the digest the proof is about',
             None if digest == parsed.digest else 'the proof is about %s' % parsed.digest.hex())
    if failures:
        write('VERDICT: FAILS')
        return 1

    # [2] replay
    bitcoin = [a for a in parsed.attestations if a.kind == 'bitcoin']
    others = [a for a in parsed.attestations if a.kind != 'bitcoin']
    if not bitcoin:
        step(2, False, 'the proof carries a Bitcoin attestation',
             'only: ' + ', '.join(a.kind + (' ' + a.uri if a.uri else '') for a in others) + ' (a pending proof: upgrade it first)')
        write('VERDICT: FAILS')
        return 1
    attestation = bitcoin[0]
    write('[2] replaying %d operations from the digest to the attested merkle root:' % len(attestation.path))
    bound, clock = describe_path(attestation.path, write)
    for other in others:
        write('      (the proof also keeps a %s attestation%s; not used)' % (other.kind, ' at ' + other.uri if other.uri else ''))
    txid = None
    for index, (name, operand, result) in enumerate(attestation.path):
        # The anchor transaction: the first sha256 pair after a prepend of a
        # raw transaction prefix (version 1 or 2, one input).
        if name == 'prepend' and len(operand) > 40 and operand[:4] in (b'\x01\x00\x00\x00', b'\x02\x00\x00\x00') \
                and index + 3 < len(attestation.path) and attestation.path[index + 2][0] == 'sha256' \
                and attestation.path[index + 3][0] == 'sha256':
            txid = attestation.path[index + 3][2]
    if txid is not None:
        write('      anchor transaction id %s' % display(txid))
    step(2, True, 'every operation replayed; merkle root %s, attested at Bitcoin block %d'
         % (display(attestation.msg), attestation.height))

    # [3] the block
    height = attestation.height
    index = height - args.start_height
    if not 0 <= index < count:
        step(3, False, 'block %d is in the headers file' % height,
             'block %d is not in the headers file (heights %d..%d)' % (height, args.start_height, args.start_height + count - 1))
        write('VERDICT: FAILS')
        return 1
    header = raw_headers[index * HEADER_SIZE:(index + 1) * HEADER_SIZE]
    fields = header_fields(header)
    block_hash = sha256d(header)
    same = fields['merkle_root'] == attestation.msg
    step(3, same, 'block %d in the headers file carries the replayed merkle root' % height,
         'block %d: hash %s, merkle root %s, mined %s (the miner\'s clock)'
         % (height, display(block_hash), display(fields['merkle_root']), _utc(fields['time']))
         if same else 'block %d\'s merkle root is %s, the proof replays to %s'
         % (height, display(fields['merkle_root']), display(attestation.msg)))

    # [4] the chain, whole
    checkpoint = None
    if args.checkpoint:
        try:
            cp_height, cp_hex = args.checkpoint.split(':')
            checkpoint = (int(cp_height), bytes.fromhex(cp_hex)[::-1])
        except ValueError:
            parser.error('--checkpoint must be HEIGHT:HASH')
    result = verify_chain(raw_headers, args.start_height, checkpoint, [bound] if bound else (), network)
    last_height = args.start_height + count - 1
    if result.ok:
        rule = ('difficulty rule (%d retargets recomputed%s)'
                % (result.retargets,
                   '; %d retarget(s) before the file\'s first header not recomputable' % len(result.notes['unchecked_retargets'])
                   if result.notes.get('unchecked_retargets') else '')
                if NETWORKS[network]['retarget'] else 'no difficulty rule on %s' % network)
        detail = ('all %d headers checked, heights %d (%s) to %d (%s): target rules, proof of work, links, %s'
                  % (result.checked, args.start_height, display(result.first_hash), last_height, display(result.last_hash), rule))
    else:
        detail = '; '.join(result.problems)
    step(4, result.ok, 'the headers file is one chain, checked whole (block %d included)' % height, detail)

    # [5] what ties the attested block to the network
    incomplete = None
    if result.ok:
        tip_hash = display(result.last_hash)
        if result.authenticated_height is None:
            incomplete = ('nothing ties the headers file to %s: it starts at height %d, not genesis, and no --checkpoint was stated. '
                          'Compare a header at or after block %d to any public source — the file\'s last, height %d = %s, '
                          'is the natural one — then state it: --checkpoint %d:%s'
                          % (network, args.start_height, height, last_height, tip_hash, last_height, tip_hash))
        elif result.authenticated_height < height:
            what = ('the genesis block' if result.trust == 'genesis' and checkpoint is None
                    else 'the checkpoint you stated, %d = %s' % (checkpoint[0], display(checkpoint[1])))
            incomplete = ('%s is BELOW the attested block %d, so it does not authenticate it: headers above a checkpoint are tied '
                          'to it only by following links forward, and a chain that follows the rules is not thereby %s\'s chain '
                          '(anyone can extend a fork past a checkpoint). State a checkpoint at or after height %d, compared to a '
                          'public source — the file\'s last header, height %d = %s, is the natural one: --checkpoint %d:%s'
                          % (what, height, network, height, last_height, tip_hash, last_height, tip_hash))
        if incomplete:
            write('[5] block %d is tied to %s ... INCOMPLETE' % (height, network))
            write('      ' + incomplete)
        elif result.trust == 'genesis' and checkpoint is None:
            step(5, True, 'block %d is tied to %s' % (height, network),
                 'by the genesis block %s, hardcoded here: block %d is height 0 itself' % (NETWORKS[network]['genesis'], height))
        else:
            cp_height = checkpoint[0]
            before = ('heights %d..%d by the links back from it' % (args.start_height, cp_height - 1)
                      if cp_height > args.start_height else 'nothing before it')
            step(5, True, 'block %d is tied to %s' % (height, network),
                 'by the checkpoint you stated, %d = %s: it pins %s; block %d is %s the checkpoint and was checked like every other%s'
                 % (cp_height, display(checkpoint[1]), before, height,
                    'below' if height < cp_height else 'at',
                    '; the genesis block also matched' if result.trust == 'genesis' else ''))
    if bound:
        found = result.notes.get('found', {}).get(bound)
        if found and result.ok:
            write('      not-before bound: block %d (%s, mined %s) is in the checked chain below block %d: '
                  'the calendar\'s commitment was constructed after it (this dates the commitment, not the exhibit)'
                  % (found[0], display(bound), _utc(found[1]), height))
        else:
            write('      not-before bound: block %s is not in the checked chain (no bound established)' % display(bound))

    # trust
    write('TRUST: this verdict relies on the attested block being in the %s chain, which the tool checks by' % network)
    if result.ok and not incomplete and checkpoint is not None:
        write('       target rules, proof of work and links over every header, pinned by checkpoint %d = %s, which you'
              % (checkpoint[0], display(checkpoint[1])))
        write('       stated: compare it to any public source. Every header at or below it is a preimage on the links back')
        write('       from it, the attested block included. Headers above it are chained forward only and prove nothing.')
    elif result.ok and not incomplete:
        write('       target rules, proof of work and links from the genesis block %s, hardcoded here, which is block %d itself.'
              % (NETWORKS[network]['genesis'], height))
    elif result.ok:
        write('       NOTHING YET: the file is one sound chain, but nothing stated pins the attested block %d to %s.' % (height, network))
        write('       A sound proof-of-work chain is not Bitcoin\'s chain; state a checkpoint at or after block %d.' % height)
    write('       The file\'s origin (the box\'s own node, ops/export_headers.py) is not evidence; the chain check is.')
    write('       Nothing else: no network, no third-party code. Verification against a node (ots verify) needs no checkpoint.')

    if failures:
        write('VERDICT: FAILS (%s)' % '; '.join(failures))
        return 1
    if incomplete:
        write('VERDICT: INCOMPLETE: every check that could run passed, but nothing ties the attested block %d to %s;' % (height, network))
        write('         state --checkpoint HEIGHT:HASH at or after block %d, compared to a public source. Exit 2.' % height)
        return 2
    found = result.notes.get('found', {}).get(bound) if bound else None
    write('VERDICT: HOLDS: the exhibit\'s bytes existed before Bitcoin block %d was mined (%s by the miner\'s clock)%s.'
          % (height, _utc(fields['time']),
             '; the calendar\'s commitment to them was constructed after block %d (%s)' % (found[0], _utc(found[1])) if found else ''))
    if found:
        write('         The lower bound dates the construction of the commitment, not the creation of the exhibit.')
    write('         What this does not say: when the exhibit was made, captured or received.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
