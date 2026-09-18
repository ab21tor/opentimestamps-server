# The tools as they were, for the migration tests

Two files of this repository at earlier revisions, byte for byte, kept as
`.txt` so that test discovery does not import them. They are here because
"what does an older version of the tool do with state a newer one wrote"
is a question about code that cannot be changed any more, and the honest
way to answer it is to run that code.
`otsserver/tests/test_restore_tools.py` executes each in a module of its
own, against temporary state, with observation and delivery stubbed; it
checks the file's SHA-256 first and runs nothing else. No git checkout
and no network is needed at test time.

| File | Source | SHA-256 |
|---|---|---|
| `selfstamp-3961a1f.py.txt` | `git show 3961a1f:ops/selfstamp.py` (the last `selfstamp/2` writer) | `191c94c2cb07d76dfc9ba445c95ee1883266e6a8040693748fb3628ae887d768` |
| `watch-ad64300.py.txt` | `git show ad64300:ops/watch.py` (the last watcher without `owed`) | `410ac24d317ddbea4d5df9850a668285e3d3aeeb38c3d3929055d3c578866c9d` |

What the tests find, and `docs/contracts.md` (section 10, R4) states:

- The older self-stamp refuses a `selfstamp/3` chain in `verify` and at
  its inbox (kept in `rejected/`, nothing lost). Its writer does not: it
  continues a labelled chain with an unlabelled `selfstamp/2` manifest.
  The current tool then calls the chain broken at that manifest and
  refuses to draw a second label.
- The older watcher ignores `owed`, delivers none of it and reports
  success; an alert it queues meanwhile is dropped when the current tool
  returns and copies the recorded queue over the outbox.

Neither file is a claim that an old program became safer when the current
one changed. A downgrade is unsupported; these are its measured effects.
