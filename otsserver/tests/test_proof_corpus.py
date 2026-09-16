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

"""The parser corpus (ops/tests/proof_corpus.py) against the two readers
under ops/ and the public client. docs/contracts.md, "The proof parser":
parses, contains a Bitcoin attestation, verifies against Bitcoin are
three claims; the corpus pins the first two. The library's verdict is
computed here, never assumed."""

import importlib.util
import io
import pathlib
import unittest

from opentimestamps.core.notary import BitcoinBlockHeaderAttestation, PendingAttestation
from opentimestamps.core.serialize import StreamDeserializationContext
from opentimestamps.core.timestamp import DetachedTimestampFile

OPS = pathlib.Path(__file__).resolve().parents[2] / 'ops'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


corpus = load('proof_corpus', OPS / 'tests' / 'proof_corpus.py')
selfstamp = load('selfstamp', OPS / 'selfstamp.py')
claim = load('verify_claim', OPS / 'verify_claim.py')


def library_verdict(data):
    try:
        f = DetachedTimestampFile.deserialize(StreamDeserializationContext(io.BytesIO(data)))
    except Exception as exc:
        return 'invalid', type(exc).__name__
    out = []
    for _, att in f.timestamp.all_attestations():
        if isinstance(att, PendingAttestation):
            out.append(('pending', att.uri))
        elif isinstance(att, BitcoinBlockHeaderAttestation):
            out.append(('bitcoin', att.height))
        else:
            out.append(('unknown', att.TAG.hex()))
    return 'parses', sorted(out, key=repr)


def selfstamp_verdict(data):
    try:
        proof = selfstamp.parse_ots(data)
    except selfstamp.OtsError as exc:
        return 'invalid', str(exc)
    return 'parses', [proof.attestation]


def claim_verdict(data):
    try:
        parsed = claim.parse_proof(data)
    except claim.ProofError as exc:
        return 'invalid', str(exc)
    out = []
    for att in parsed.attestations:
        if att.kind == 'pending':
            out.append(('pending', att.uri))
        elif att.kind == 'bitcoin':
            out.append(('bitcoin', att.height))
        else:
            out.append(('unknown', att.kind.split(':', 1)[1]))
    return 'parses', sorted(out, key=repr)


class Test_corpus_against_the_readers(unittest.TestCase):
    def test_every_case(self):
        for name, data, verdict, shape, attestations in corpus.cases():
            with self.subTest(name):
                tree = claim_verdict(data)
                linear = selfstamp_verdict(data)
                if verdict == 'parses':
                    self.assertEqual(tree, ('parses', sorted(attestations, key=repr)))
                    if shape == 'linear':
                        self.assertEqual(linear, ('parses', attestations))
                    else:
                        self.assertEqual(linear[0], 'invalid', 'the linear reader refuses forks')
                else:
                    self.assertEqual(tree[0], 'invalid', tree)
                    self.assertEqual(linear[0], 'invalid', linear)

    def test_every_prefix_and_extension_is_invalid(self):
        for name, data in list(corpus.prefixes()) + list(corpus.trailing()):
            with self.subTest(name):
                self.assertEqual(claim_verdict(data)[0], 'invalid')
                self.assertEqual(selfstamp_verdict(data)[0], 'invalid')

    def test_the_readers_never_raise_anything_but_their_own_error(self):
        """A reader that lets IndexError or UnicodeDecodeError out turns a
        bad input into a crash of whatever called it (2026-09-15/16 review
        F13, F14). Only the reader's own error class may escape."""
        import random
        rng = random.Random(20260916)
        shapes = [data for _, data, verdict, _, _ in corpus.cases() if verdict == 'parses']
        for _ in range(2000):
            data = bytearray(rng.choice(shapes))
            for _ in range(rng.randint(1, 3)):
                pos = rng.randrange(len(data)) if data else 0
                roll = rng.random()
                if roll < 0.5 and data:
                    data[pos] = rng.randrange(256)
                elif roll < 0.8:
                    data.insert(pos, rng.randrange(256))
                elif data:
                    del data[pos]
            data = bytes(data)
            try:
                claim.parse_proof(data)
            except claim.ProofError:
                pass
            try:
                selfstamp.parse_ots(data)
            except selfstamp.OtsError:
                pass


class Test_corpus_against_the_library(unittest.TestCase):
    def test_parses_and_invalid_agree_with_the_public_client(self):
        for name, data, verdict, shape, attestations in corpus.cases():
            with self.subTest(name):
                lib = library_verdict(data)
                if verdict == 'parses':
                    self.assertEqual(lib, ('parses', sorted(attestations, key=repr)))
                elif verdict == 'invalid':
                    self.assertEqual(lib[0], 'invalid', lib)
                else:
                    self.assertEqual(lib[0], 'parses', 'a narrowing is something the client reads: ' + repr(lib))
                    self.assertEqual(claim_verdict(data)[0], 'invalid')
                    self.assertEqual(selfstamp_verdict(data)[0], 'invalid')

    def test_prefixes_and_extensions_agree(self):
        for name, data in list(corpus.prefixes()) + list(corpus.trailing()):
            with self.subTest(name):
                self.assertEqual(library_verdict(data)[0], 'invalid')


if __name__ == "__main__":
    unittest.main()
