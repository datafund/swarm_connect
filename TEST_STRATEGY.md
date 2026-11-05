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

## In CI

`.github/workflows/tests.yml` runs the default (hermetic) suite on every pull
request, and `deploy.yml` calls it as its gate, so nothing deploys unless it
passes. It runs on Python 3.12, the image's interpreter, and installs exactly
what the image ships: `pip install --no-deps --require-hashes -r
requirements.lock`, then `pip check`, which fails the run if the lock misses a
dependency or pins a conflicting one (#523). A local venv built from
`requirements.txt` resolves whatever is current, so it can differ from CI; to
reproduce CI, build a Python 3.12 venv the same way (for example `uv venv -p 3.12`,
then `uv pip install --no-deps --require-hashes -r requirements.lock` and
`uv pip check`). `tests/test_ci_workflow.py` keeps the workflow on the lock, and
CI, the Dockerfile base image and the lock on one Python version, with the base
image pinned by digest.

`.github/workflows/image.yml` builds the Docker image on every pull request
(#486): `docker build --pull --no-cache --platform linux/amd64`, so neither a
cached pip layer nor a stale base image can hide a lock that a clean build
rejects. That happened on 2026-10-05, when every staging deploy failed at the
image build and no PR check had built it (#483). linux/amd64 is what the deploy
hosts run. The same job then starts the image with only `SWARM_BEE_API_URL`
set, pointing at a port nothing listens on, with `--network none`, and fails
unless `curl -f http://localhost:8000/` (the compose healthcheck) answers 200
within 30 tries, 2 s apart: about a minute when the port refuses, and at most
about 6 minutes when the app accepts connections but never answers (each try
waits up to 10 s), inside the job's 20-minute timeout. `/` answers 200 with `status: degraded` while Bee is
unreachable, and a refused port builds the first health snapshot at once, so
the check never waits for `HEALTH_SNAPSHOT_FIRST_WAIT_SECONDS`. On failure the
job prints the container log; the container is always removed.
`deploy.yml` does not call it: each deploy host builds the image itself before
stopping the running gateway, so a broken build there already stops the deploy
safely. `tests/test_ci_workflow.py` keeps the job uncached and its smoke check
in place. To reproduce it locally:

```bash
docker build --pull --no-cache --platform linux/amd64 -t swarm_connect:ci .
docker run -d --name swarm_connect_ci --network none \
  -e SWARM_BEE_API_URL=http://localhost:1 swarm_connect:ci
docker exec swarm_connect_ci curl -fsS --max-time 10 -o /dev/null http://localhost:8000/
docker rm -f swarm_connect_ci
```

On an arm64 machine the amd64 build runs under emulation and is much slower
than in CI.

Known limits of the image job:

- A direct push to `dev` or `main` that skipped a pull request is built only
  on the deploy hosts, which reuse their Docker layer cache.
- It blocks a merge only once `docker-build` is a required status check in
  the branch protection settings; until then a red run is advisory.

## The default run is hermetic

- **No network.** Bee and chain calls are mocked. The live modules are opt-in (next
  section).
- **No writes to the working tree** (the x402 audit log still lands in `logs/` until #404 redirects it). `tests/conftest.py` points every persisted
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
explicit opt-in, on separate switches:

| variable | effect | default |
|---|---|---|
| `RUN_LIVE_TESTS=1` | run the live modules at all | off |
| `GATEWAY_URL=...` | which gateway to talk to (both modules; `TEST_GATEWAY_URL` is a legacy alias) | staging (**never** production) |
| `ALLOW_LIVE_STAMP_PURCHASE=1` | let a test have the gateway buy a postage batch (the integration fixture's fallback, and `test_free_tier_access`) | off |
| `TEST_WALLET_PRIVATE_KEY=0x...` | run `test_payment_with_x402_client`, which **pays** from that wallet for a batch | unset |

The x402 payment and free-tier tests also read `GET /health` and skip unless the
gateway reports x402 enabled (and the free tier, for the free-tier tests). With x402
off, a valid stamp request is not refused with 402; it buys a batch.

```bash
RUN_LIVE_TESTS=1 pytest tests/test_integration_gateway.py -v -s          # staging
RUN_LIVE_TESTS=1 GATEWAY_URL=http://localhost:8000 pytest ... -v -s      # a local gateway
```

`tests/test_bee_keys_drill.py`, the Bee keystore restore drill (#494), is opt-in
on its own switch, `RUN_BEE_DRILL=1`. It needs a local Docker daemon and the `age`
CLI. It uses only a throwaway identity, runs every Bee container with `--network
none`, and reaches no gateway or chain. The hermetic checks of the same scripts are
in `tests/test_backup_bee_keys.py` and run by default.

```bash
RUN_BEE_DRILL=1 pytest tests/test_bee_keys_drill.py -s
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
