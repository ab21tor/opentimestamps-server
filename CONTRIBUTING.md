# Contributing

This code is free, open-source and public. Every change must read
plainly to a stranger: the code says what it does, the README says what
it promises, and `docs/contracts.md` says who owns unfinished work at
every handoff and what the tests check.

## The standing rule

Any change to persistence, acknowledgements, identity, retries or money
updates the transition contract (`docs/contracts.md`: the promise, the
state table, the invariants) and its failure tests in the same commit.
"Persistence" is anything written to `journal`, `db/`, `journal.counts`,
`journal.known-good`, the receipts file or its markers, or the tools'
state directories. "Acknowledgement" is any HTTP success, any log line
another tool reads, and any exit code a timer reads. "Identity" is the
generation, the URI, the HMAC key, a txid. "Retries" is any loop that
tries again after a failure. "Money" is anything that becomes a receipt.

A fix starts with the failing test: the defect reproduced under the
assertion the contract requires, red on the code before the fix, green
after. Interruption and concurrency cases sit beside the happy path for
every transition that has one. A test that injects an exception says so;
it is not a power cut, and no test here is described as one.

## Running the suite

```
python -m unittest discover -v
```

in an environment with `requirements.txt` installed (`plyvel` is a
native build against the system LevelDB; README "Tests"). Search with
`git grep`; every negative claim in a review ("X is absent") comes with a
positive control showing the search works.

## Commits

One change per commit, a message that says what changed and why, no
trailers.
