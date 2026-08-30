# Install runbook

How to stand up a gateway host from nothing: VM, Bee node, gateway, TLS, CI/CD.

Written from the install that is actually running, not from a plan. Where the two
diverged, this describes what exists and says why.

**No secrets, keys, addresses or hostnames appear here** — only what is needed,
where it is stored, and how it reaches the running process. Anything in angle
brackets is yours to supply.

---

## 0. What you are building

One host runs **both environments**. Production and staging each get a gateway
container and their own Bee node, on the same machine:

```
             Caddy (:80, :443)  ── the only public entry point
               │
               ├─ 127.0.0.1:8899 → provenance_gateway      → bee:1633
               └─ 127.0.0.1:8900 → provenance_gateway_dev  → bee-dev:1633

  Alloy ── scrapes all four over the compose network → Grafana Cloud
```

The gateways bind to loopback. Bee's API is not published at all — it has no
authentication, so it is reachable only inside the compose network. Only the two
p2p ports are exposed.

> **This differs from the original plan (#240)**, which called for a dedicated
> staging VM running a Sepolia *testnet* Bee. One host with two *mainnet* nodes
> was chosen instead: cheaper, and staging that exercises real chain economics
> catches things a testnet cannot — RPC rate limiting, postage minimum-validity
> floors, batch-status edge cases have all been caught this way. The cost is that
> **staging spends real BZZ**. Decide which you want before following this; the
> rest of the runbook is the same either way apart from the Bee network flag.

## 1. Provision the VM

What the running host uses. Treat as a floor, not a recommendation:

| | |
|---|---|
| 4 vCPU, 8 GB RAM, 100 GB SSD | two light Bee nodes plus two gateways sit comfortably; disk was 13% used after weeks |
| Ubuntu 24.04 LTS | |
| Docker Engine + Compose v2 | Compose v2 syntax is assumed throughout |

Firewall — exactly five rules. Nothing else should be reachable:

```bash
ufw allow 22/tcp     # ssh
ufw allow 80/tcp     # ACME http-01 challenge
ufw allow 443/tcp    # gateway
ufw allow 1634       # production Bee p2p   (tcp AND udp)
ufw allow 1734       # staging Bee p2p      (tcp AND udp)
ufw enable
```

The p2p rules must cover **both** tcp and udp; omitting udp leaves the node
reachable but degraded.

**Check egress before going further.** A node that cannot dial out reports
`networkAvailability: Unavailable` and accepts uploads that never propagate —
`201` responses for data that is not stored anywhere. Some providers block
outbound ranges by default.

```bash
curl -sS https://rpc.gnosischain.com -X POST \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}'
```

## 2. Bring up the Bee nodes

Both live in `docker-compose.host.yml` behind the `bee` compose profile, so a host
that does not want them simply omits `COMPOSE_PROFILES=bee`.

Create `/opt/swarm_connect_host.env`. **This file is host-specific and the deploy
only ever appends it to `.env` — it is never overwritten**, which is why
host-specific values belong here and nowhere else:

```bash
COMPOSE_PROFILES=bee
GATEWAY_BIND=127.0.0.1

BEE_PASSWORD=<generated>              # keystore password, production node
BEE_DEV_PASSWORD=<generated>          # keystore password, staging node
BEE_NAT_ADDR=<public-ip>:1634         # must match the published port exactly
BEE_DEV_NAT_ADDR=<public-ip>:1734
BEE_P2P_PORT=1634
BEE_DEV_P2P_PORT=1734

BEE_RPC_ENDPOINT=<gnosis-rpc-url>
BEE_DEV_RPC_ENDPOINT=<different-gnosis-rpc-url>

SWARM_BEE_API_URL=http://bee:1633
SWARM_BEE_API_URL_DEV=http://bee-dev:1633

HOST_LABEL=<short-host-name>          # distinguishes this host's metrics
```

Three of these have caused real outages:

**`BEE_NAT_ADDR` must advertise the port peers actually reach.** A mismatch
leaves the node reporting `reachability: Private` even with the port open,
because its own reachability probe cannot dial itself back. Since Bee 2.7.0 an
invalid value fails startup outright.

**The two RPC endpoints must be different.** They shared one for a while, and
therefore shared its rate limit: staging load produced `429`s that broke
`/chainstate`, `/wallet` and batch creation on **production**. If your provider
caps by IP rather than by key, note that both nodes share this host's IP — pick
providers accordingly.

**`SWARM_BEE_API_URL` is required, not defaulted.** Compose refuses to start
without it and names the variable. That is deliberate: it used to default to a
specific node, which silently became wrong once a second host existed.

Start them:

```bash
cd /opt/swarm_connect
docker compose -f docker-compose.host.yml up -d
```

### Fund each node

Each Bee node needs, on Gnosis:

- **xDAI** for gas — chequebook deployment happens on first start and fails
  without it, leaving the node in a restart loop
- **xBZZ** for postage

Read the address from each node (the API is not published, so go via the
container):

```bash
docker run --rm --network container:swarm_connect-bee-1 curlimages/curl:latest \
  -s http://localhost:1633/wallet
```

Wait for `chequebookContractAddress` to be populated before assuming the node is
ready. Sync takes hours; `/health` on the gateway reports progress once it is up.

## 3. Deploy the gateway

Deploys run on a **self-hosted GitHub Actions runner on this host**. Register one
against the repository and note its label.

> Check whether a runner already on the machine serves other repositories before
> reusing or removing it. One here also serves four unrelated repositories, and
> unregistering it would have broken all of them.

Point the workflow at it with the repository variable `DEPLOY_RUNNERS`, a JSON
array of runner labels. It is a matrix, so several hosts can be added without
touching the workflow.

The deploy writes `/opt/swarm_connect.env` and `/opt/swarm_connect_dev.env` from
GitHub environment variables (44 of them), appends `/opt/swarm_connect_host.env`,
pins `COMPOSE_PROJECT_NAME`, and recreates only the service the pushed branch
owns.

Four repository **secrets** are required: the Grafana Cloud username and token,
the notary signing key, and the Gnosis signing key. Everything else is a
non-secret variable.

> `COMPOSE_PROJECT_NAME` is pinned deliberately. By default Compose derives it
> from the directory name, which makes container **and volume** names depend on a
> filesystem path — deploying from a different directory would look for volumes
> that do not exist, create empty ones, and start Bee with **no keystore**: a new
> wallet, with the funded one orphaned.

## 4. DNS and TLS

Two A records — production and staging — pointed at the host. Then Caddy, which
terminates TLS and is the only public entry point.

`deploy/Caddyfile` is in version control but **is not deployed by CI**. Copy it
manually and reload:

```bash
diff /etc/caddy/Caddyfile deploy/Caddyfile   # always, before overwriting
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

The diff step is not ceremony. Overwriting the live file with a stale copy once
reintroduced `tls internal` and broke TLS for two minutes.

Hostnames come from a systemd drop-in rather than the file itself, so the same
Caddyfile serves any deployment — see `deploy/README.md`.

## 5. Monitoring

Alloy runs from `docker-compose.host.yml` and scrapes both gateways and both Bee
nodes over the compose network, forwarding to Grafana Cloud.

Dashboards and alert rules live in `monitoring/` and are **not** applied by the
deploy. Apply them with:

```bash
GRAFANA_URL=https://<stack>.grafana.net GRAFANA_SA_TOKEN=glsa_... \
  python3 scripts/apply_grafana.py
```

That token is a Grafana **instance service-account** token (`glsa_` prefix). The
`glc_` Cloud Access Policy token used for metrics remote-write returns 401 here;
they are different credentials and both are called "a Grafana Cloud token".

## 6. Verify

```bash
curl -s https://<host>/health | python3 -m json.tool
```

Expect `status: ok`, and under `bee_node`: `healthy: true`, a peer count in the
hundreds, `reachability: Public`, `network_availability: Available`, and a small
`chain_sync_lag_blocks`.

| symptom | meaning |
|---|---|
| `reachability: Private` | `BEE_NAT_ADDR` does not match the published port, or the p2p port is closed (check udp) |
| `network_availability: Unavailable` | the node cannot dial out — uploads will 201 without propagating |
| `chain_sync_lag_blocks` large and growing | RPC endpoint is failing or rate-limiting |
| warning that the node is "still starting up" | reachable but not yet serving topology; normal on a cold start |

## 7. Troubleshooting the failure modes we have actually hit

**A deploy succeeds and changes nothing.** See the configuration table in
`CLAUDE.md`. The three traps: `docker restart` does not reload `env_file`; a
bind-mounted single *file* pins its inode so `git pull` leaves the container
reading the old one; and the Caddyfile and Grafana JSON are never deployed at all.

**RPC rate limiting.** Bee logs `429 Too Many Requests` from
`node/listener could not get block number`. Grafana has the rate under
`bee_eth_backend_total_rpc_errors`. Usually means both nodes share an endpoint,
or the free tier is too small.

**Diagnosing Bee with only gateway access.** `GET /api/v1/debug/bee/{path}` is a
read-only, signature-gated proxy to allow-listed Bee endpoints, for when you have
the gateway but not the node. Disabled unless `DEBUG_ALLOWED_ADDRESSES` is set.

**The pool spending unexpectedly.** `GET /api/v1/pool/status` reports the target,
current levels, and any purchase errors including back-off state. Note the pool
tops up **every batch it holds**, not just up to the target — lowering the target
alone does not reduce cost, the surplus has to leave the pool.

## Mainnet vs testnet

Set `BEE_MAINNET` accordingly. The rest is identical, except that a testnet node
funds from a faucet rather than with real value — which is the whole trade-off:
cheaper to run, and blind to anything that depends on real chain economics.
