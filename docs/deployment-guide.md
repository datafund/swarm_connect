# Deployment guide

How to run this gateway on a host you control.

This describes the software's requirements, not any particular installation. Where
a choice is yours to make, it says so rather than making it for you.

---

## What you are running

A gateway container, a Bee node it talks to, and a reverse proxy in front:

```
  reverse proxy (TLS)  ──>  gateway  ──>  Bee node
```

You can run more than one environment on a host by running more than one gateway
and more than one Bee node. They are separate services in the compose files and
need separate ports, keystores and data volumes.

**The Bee API has no authentication.** Do not publish it. The compose file keeps
it on the internal network only, and the gateway reaches it by service name. If
you expose it, anyone can spend your postage and read your wallet.

## Requirements

- Docker Engine and Compose v2
- A Linux host. Two light Bee nodes plus two gateways run comfortably in 8 GB RAM
  and under 20 GB of disk; a full node needs far more disk
- A funded wallet per Bee node (below)
- Outbound network access — see the egress check, it is not optional

## Configuration

The gateway requires `SWARM_BEE_API_URL`. Compose refuses to start without it and
names the variable in the error. This is deliberate: it previously defaulted to a
specific node, which is silently wrong for anyone who is not that deployment.

Optional per-node settings that need care:

| variable | why it matters |
|---|---|
| `BEE_NAT_ADDR` | must advertise the **public host:port that peers actually reach**. A mismatch leaves the node reporting `reachability: Private` even with the port open, because its own reachability probe cannot dial itself back. Since Bee 2.7.0 an invalid value fails startup outright |
| `BEE_RPC_ENDPOINT` | a Gnosis JSON-RPC endpoint. **Give each node its own** — see below |
| `BEE_PASSWORD` | the keystore password. If the only copy is lost, the wallet is unrecoverable. Back it up before funding anything |
| `COMPOSE_PROJECT_NAME` | pin it. Compose otherwise derives it from the directory name, which makes container **and volume** names depend on a filesystem path — deploying from a different directory looks for volumes that do not exist, creates empty ones, and starts Bee with no keystore: a new wallet, the funded one orphaned |

### Give each Bee node its own RPC endpoint

If you run two nodes and point them at the same endpoint, they share its rate
limit. A busy node then produces `429 Too Many Requests` that break `/chainstate`,
`/wallet` and batch creation **on both** — including the quiet one.

If your provider limits by IP rather than by key, note that nodes on the same host
share an IP, and pick providers accordingly.

## Firewall

Open only:

- your SSH port
- 80 — required for the ACME HTTP-01 challenge if you use automatic certificates
- 443
- each Bee node's p2p port, **tcp and udp both**

Omitting udp leaves a node reachable but degraded, which is harder to notice than
it being unreachable.

Publishing the gateway on loopback (`GATEWAY_BIND=127.0.0.1`) and letting the
proxy be the only public entry point is worth doing. Note that Docker publishes
ports through its own iptables chains and **bypasses ufw**, so a ufw rule alone
will not keep a published port private.

## Check egress before funding anything

A node that cannot dial out reports `network_availability: Unavailable` and will
accept uploads that never propagate — `201` responses for data that is not stored
anywhere. Some providers block outbound ranges by default.

```bash
curl -sS <your-gnosis-rpc> -X POST -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}'
```

## Fund each Bee node

Each node needs, on Gnosis:

- **xDAI** for gas. The chequebook is deployed on first start and fails without
  it, leaving the node restarting in a loop
- **xBZZ** for postage

Read the address from the node. Since its API is not published, go via the
container:

```bash
docker run --rm --network container:<bee-container> curlimages/curl:latest \
  -s http://localhost:1633/wallet
```

Wait for `chequebookContractAddress` to be populated before treating the node as
ready. Initial sync takes hours.

## Verify

```bash
curl -s https://<your-host>/health | python3 -m json.tool
```

Expect `status: ok`, and under `bee_node`: `healthy: true`, a peer count in the
hundreds, `reachability: Public`, `network_availability: Available`, and a small
`chain_sync_lag_blocks`.

| symptom | meaning |
|---|---|
| `reachability: Private` | `BEE_NAT_ADDR` does not match the published port, or the p2p port is closed — check udp |
| `network_availability: Unavailable` | the node cannot dial out. Uploads will return 201 without propagating |
| `chain_sync_lag_blocks` large and growing | the RPC endpoint is failing or rate-limiting |
| "still starting up" warning | reachable but not yet serving topology. Normal on a cold start; Bee answers `/health` before `/topology` |

## Operating notes

**The stamp pool tops up every batch it holds**, not just up to the configured
target. Lowering the target stops it *buying* replacements but does not stop it
*paying* for what it already has — the surplus has to leave the pool before the
cost drops.

**Pool inventory is owned by the gateway.** Batches it buys are registered to
itself and refused to direct callers; a caller obtains one by acquiring it, which
transfers ownership. Before that was true, anyone who read a batch id from the
stamp listing could store data on postage the gateway had paid for.

**`/metrics` is for your metrics agent, not the public.** It exposes wallet
addresses, balances, configuration flags and request volumes. Block it at your
proxy; an agent on the same host or network reaches it directly.

**Every Bee call fails with `PoolTimeout` while Bee itself answers.** The
shared HTTP client's pool can fill with slots that hold no connection: httpcore
keeps a slot when a request is cancelled (a client disconnect, a timeout) right
after the pool gave it a new connection. The gateway replaces the client by
itself when there were `BEE_HTTP_POOL_RECOVERY_THRESHOLD` `PoolTimeout`s in a
row, at least `BEE_HTTP_POOL_STUCK_FRACTION` of the pool is stuck, and Bee
answers `GET /health` on a new connection. Watch
`gateway_bee_http_pool_connections{state="stuck"}`,
`gateway_bee_http_pool_timeouts_total` and
`gateway_bee_http_client_replacements_total{result}`. Error log lines name the
exception type, so these show as `PoolTimeout` rather than an empty message.
`result="probe_failed"` means Bee does not answer on a new connection either:
look at Bee, not the gateway. A restart of the gateway container also clears
the pool.

**The shared HTTP client ignores proxy environment variables.** Bee API and
Base RPC calls go out directly even when `HTTP_PROXY`, `HTTPS_PROXY` or
`ALL_PROXY` is set in the container: the client is built with its own
transport (#587), and httpx applies those variables only to clients without
one. Bee is on the compose network and must not go through a proxy.

**Floods (#588).** Three things keep a flood of cheap requests from turning
into load on Bee:

- `/health` and `/` are answered from a snapshot of the Bee node summary and
  the wallet balances. The snapshot is rebuilt at most once every
  `HEALTH_SNAPSHOT_TTL_SECONDS` (default 15) by a single background refresh, so
  a request costs no Bee or RPC call. `snapshot.age_seconds` in the response,
  and `gateway_health_snapshot_age_seconds`, say how old it is.
  `snapshot.warnings` says when refreshes fail. One failed refresh is
  advisory; once the snapshot is also older than
  `HEALTH_SNAPSHOT_MAX_TRUSTED_AGE_SECONDS` (default 60) it is too old to
  trust, `status` is `degraded`, `bee_node.healthy` is false and
  `bee_node.api_reachable` null (the Bee state is unknown; a warning in
  `bee_node.warnings` says so), and paid requests get `503
  PAYMENTS_UNAVAILABLE` (#589). An unreachable Bee node
  (`bee_node.api_reachable: false`) is `degraded` too, never `critical`.
  `/` is the same handler with the same body, so the CLI and
  MCP checks (any 2xx) and the SDK check (`status != "critical"`) are
  unchanged. Docker's healthcheck (`curl -f http://localhost:8000/`) tests
  that the process answers. It never tested Bee: `/health` answers 200 whatever
  Bee's state. Bee reachability is watched by the external probe of
  `bee_node.healthy`, which therefore also turns false when refreshes keep
  failing, rather than repeating the last good answer.
- At most `BEE_MAX_CONCURRENT_CALLS` Bee calls are in flight at once.
  `BEE_RESERVED_BACKGROUND_CALLS` of them are kept for work outside requests
  (snapshot refresh, stamp pool, metrics poller), so a flood cannot starve it.
  Uploads and downloads made by requests hold at most
  `BEE_MAX_CONCURRENT_TRANSFERS`. Left unset, the three are 64%, 8% and 24% of
  `BEE_HTTP_MAX_CONNECTIONS`: 64/8/24 for the default 100, 26/3/10 for 40. So
  lowering the pool keeps a working gate. Set explicitly, the cap must be below
  `BEE_HTTP_MAX_CONNECTIONS`, the reserve below the cap, and transfers at most
  the cap minus the reserve; the gateway refuses to start otherwise. A request's
  Bee call that gets no slot within `BEE_CALL_SLOT_WAIT_SECONDS` (2) makes the
  request answer `503 BEE_BUSY` with `Retry-After: BEE_BUSY_RETRY_AFTER_SECONDS`
  (5). That answer says nothing was charged, so it is never given to a request
  that carries a payment, or once its settlement has started; such a request
  keeps its own answer, with its payment headers and code. Bee work of a
  request whose payment has settled waits up to
  `BEE_BACKGROUND_SLOT_WAIT_SECONDS` (30) instead; if it is still refused, the
  call never reached Bee, and the purchase is recorded for a refund, not looked
  for on-chain. Watch
  `gateway_bee_calls_in_flight{lane}` and
  `gateway_bee_call_rejections_total{lane}`. A refusal is not a Bee error: it
  is not counted in `gateway_bee_api_errors_total` or as a pool timeout. These
  sizes are a judgement, not measured. Tune them from those two series.
  `BEE_CALL_GATE_ENABLED=false` turns the gate off entirely.
- The heaviest callers per `TOP_CALLERS_WINDOW_SECONDS` (60) are in
  `gateway_top_caller_requests{rank="1".."5"}`. When the heaviest sent at least
  `TOP_CALLERS_LOG_MIN_REQUESTS` (300) requests in a window, a WARNING log line
  names the top callers by a 12-character hash tag, with their status mix and
  busiest route. The tag is a keyed hash of the caller's address (IPv6 by
  `/64`). No address appears in metrics or logs. The key is random per process,
  so tags change on restart and differ between environments. To check a
  suspected address, set `TOP_CALLERS_HASH_KEY` to a secret in the host env
  file (never in the repo). Tags are then stable, and
  `python -c "from app.services.top_callers import caller_tag; print(caller_tag('<address or /64>'))"`
  run inside the container gives the address's tag. To block it, add it to
  `X402_BLACKLIST_IPS`.

To replay a flood against a local gateway with a stub Bee, use
`scripts/flood_replay.py` and `python -m tests.tools.bee_stub` (usage in the
script's docstring).

**Diagnosing Bee with only gateway access.** `GET /api/v1/debug/bee/{path}` is a
read-only, signature-gated proxy to allow-listed Bee endpoints, for when you have
the gateway but not the node. Disabled unless `DEBUG_ALLOWED_ADDRESSES` is set.

## Configuration changes that appear to do nothing

Three traps, each of which will let a change report success and take no effect:

**`docker restart` does not reload `env_file`.** Docker reads it when it *creates*
a container. Editing the env file and restarting keeps the old values while the
container reports healthy. Use `docker compose up -d --force-recreate <service>`.

**A bind-mounted single file pins its inode.** `git pull` replaces the file rather
than editing it, so the container keeps reading the old one — and Compose
correctly does nothing, because the service definition has not changed. Mount a
directory, or recreate the container.

**Not everything in a repository is deployed.** Reverse proxy configuration and
monitoring definitions may be version-controlled for review while being applied by
hand. Check before assuming a merge changed anything, and diff before overwriting
a live config file.
