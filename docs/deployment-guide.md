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
