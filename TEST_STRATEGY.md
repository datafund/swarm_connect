# Testing Strategy

The suite in `tests/` is the source of truth for what is covered: one
`test_<area>.py` module per feature (stamps, data, pool, chunks, notary, x402,
for-owner, metrics, …). This file does not list tests or counts — those went stale
the last time it did. It records the rules the suite follows and the reasons for
them.

## Running the suite

```bash
python -m pytest tests/ -q                      # everything (hermetic, see below)
python -m pytest tests/test_stamp_pool.py -v    # one area
```

## The default run is hermetic

- **No network.** Bee and chain calls are mocked. The live modules are opt-in (next
  section).
- **No writes to the working tree.** `tests/conftest.py` points every persisted
  state file (pool inventory, pool allowance, stamp ownership, bandwidth credit,
  spend budget) at a temporary directory before the app is imported, because those
  services are module-level singletons that read their paths at import time (#335).
  A test that constructs its own service with a mocked `settings` must pass an
  explicit path (`tmp_path`), otherwise the path is the mock's `repr` and a
  `MagicMock/` directory appears in the repo. `data/`, `logs/` and `MagicMock/` are
  also in `.gitignore` as a backstop, not as the fix.
- **No rate limits or spend caps deciding unrelated outcomes.** `conftest.py`
  disables the global limiter and resets the daily spend budget per test; tests
  about those features configure them explicitly.

## Live gateway tests are opt-in

`pytest tests/` is hermetic. Nothing in it reaches a live gateway unless you say so.

`tests/test_integration_gateway.py` and `tests/test_x402_live.py` both require an
explicit opt-in, on three separate switches:

| variable | effect | default |
|---|---|---|
| `RUN_LIVE_TESTS=1` | run the live modules at all | off |
| `GATEWAY_URL=...` | which gateway to talk to | staging (**never** production) |
| `ALLOW_LIVE_STAMP_PURCHASE=1` | let a fixture buy a postage batch | off |

```bash
RUN_LIVE_TESTS=1 pytest tests/test_integration_gateway.py -v -s          # staging
RUN_LIVE_TESTS=1 GATEWAY_URL=http://localhost:8000 pytest ... -v -s      # a local gateway
```

### Why staging, and why not the branch you are on

Production is never a default and has to be named. That is the property that
matters. Localhost was the wrong way to get it, though: someone opting in to live
tests rarely has a gateway and a Bee node running locally, so the useful case
needed configuration before it worked at all.

Deriving the target from the current git branch was considered and rejected. Being
on `main` locally would point these at production — the exact hazard this module
already had once. And the branch carries no useful signal: these tests exercise a
**deployed** gateway, and the branch in your working tree is by definition not
deployed, so matching it would test the environment you are about to deploy into
rather than the change you are making.

### Why three switches rather than one

`test_integration_gateway.py` used to default `GATEWAY_URL` to the **production**
gateway and skip only when that host was unreachable. Production is normally up, so
the mandatory pre-PR run fired real traffic at it every time (#233).

That caused 429 failures — the free tier allows three writes a minute, and the
built-in pacer does not fully absorb it when local timing shifts — which made the
one gate everything else depends on fail for reasons unrelated to the change.

It could also **spend money**. The `usable_stamp` fixture purchases a postage batch
when it cannot find a usable local one. That is real BZZ, on production, from a
routine local test run; it stayed harmless only because production happens to hold
usable stamps.

Reachability is not a gate. Whether production is up says nothing about whether this
run intended to touch it. And opting in to live tests is not the same decision as
opting in to spending, which is why the purchase has its own switch.

## Stamp ownership: test the lock AND the door

`tests/test_stamp_ownership.py` covers enforcement — untracked batches refused,
pool-owned batches refused to paid, free-tier and anonymous callers alike, and
the `STAMP_OWNERSHIP_ALLOW_UNTRACKED` escape hatch not reaching pool inventory.

`tests/test_stamp_pool.py::TestPoolInventoryIsRegisteredAsOwned` covers the other
half: that the pool actually registers what it buys, that a registration failure
does not lose a batch already paid for, and that sync adopts inventory bought
before the change.

Both halves are needed, and the second is the one easy to omit. Enforcement tests
construct registry state by hand, so they pass whether or not any code registers
anything. That asymmetry is how #312 stayed open: the check was fine, nothing
was claiming ownership of the pool's own batches, and every test agreed.
