# Mainnet runbook

Operating the gateway once x402 takes real USDC on Base mainnet. It covers the cutover checklist, how to stop traffic fast, refunds, backups and restore, alerts, and keys.

Some items depend on changes delivered in separate pull requests. They are marked *(requires #N)*; until that PR is merged and deployed, the item does not apply.

The Bee side (stamps, pool, chequebook) already runs on Gnosis mainnet and spends real xBZZ/xDAI on staging and production today. This runbook is about the **incoming** side becoming real money too.

## 1. Cutover checklist

Do these in order. Rehearse on staging first, with the same settings and a tiny price.

| # | Item | How |
|---|---|---|
| 1 | **Pay-to address** | A new mainnet address you control. Ideally cold or multisig, and not shared with staging. It needs no ETH: the facilitator pays settlement gas. Set `X402_PAY_TO_ADDRESS`. |
| 2 | **Network** | `X402_NETWORK=base`. The gateway refuses to start with an unknown network name. Change it **in the same deploy** as items 1 and 3: see [Items 1–3 change in one deploy](#items-13-change-in-one-deploy). |
| 3 | **Facilitator** | x402.org serves test networks only. Configure a mainnet facilitator: CDP credentials (`X402_FACILITATOR_CDP_API_KEY_ID` / `_SECRET` as GitHub **secrets**), or `X402_FACILITATOR_URL` plus `X402_FACILITATOR_BEARER_TOKEN`. The facilitator URL setting (`X402_FACILITATOR_URL`) must be empty or exactly `https://api.cdp.coinbase.com/platform/v2/x402` when CDP credentials are set (#527): `cdp-sdk` signs each token for `/platform/v2/x402/verify` or `/settle`, and the gateway calls `{url}/verify` and `{url}/settle`, so the bare host `https://api.cdp.coinbase.com` or any other path would fail every payment. The gateway refuses to start, and the deploy-time check refuses, with any other value. `cdp-sdk` is pinned in `requirements.lock`, so the image has it. First confirm the facilitator accepts the gateway's x402 protocol version (see the x402 version decision). For CDP this is still open: CDP's facilitator route is x402 v2 and the gateway speaks x402 v1 (`x402==1.0.0`). With `cdp-sdk` installed the gateway starts and the deploy-time check passes, but that does not show CDP settles v1 payments; see #524. |
| 4 | **Pricing** | Set `X402_BZZ_USD_RATE` to a reviewed value, and check `X402_MARKUP_PERCENT` and `X402_MIN_PRICE_USD`. Set `X402_BZZ_PRICE_FEED_URL` so drift is alerted *(requires #410)*. |
| 5 | **Spend ceilings** | Set `GATEWAY_DAILY_BZZ_CEILING` and `GATEWAY_DAILY_BZZ_FREE_CEILING` to what the Bee wallet can afford per day *(requires #408)*. |
| 6 | **Free tier** | Decide whether it stays on (`X402_FREE_TIER_ENABLED`) and at what rate (`X402_FREE_TIER_RATE_LIMIT`). It is bounded by the free ceiling. |
| 7 | **Backups** | Install the backup cron (section 4) and test a restore once. |
| 8 | **Alerts** | Apply the alert rules (`scripts/apply_grafana.py`) and check they reach someone (section 5). |
| 9 | **Terms** | Terms and refund policy published and linked *(requires the terms PR for #383)*. |
| 10 | **Smoke test** | One real payment of a few cents, end to end: 402 → pay → 2xx, a `payment_settled` and a `payment_delivered` audit line with the same transaction, and the USDC visible at the pay-to address. |


### Items 1–3 change in one deploy

The network, the facilitator URL, the pay-to address and the facilitator credentials must all change **together, in the same deploy**. They cannot be changed one at a time.

The reason: x402.org serves test networks only. At startup, `validate_x402_config` (in `app/x402/facilitator.py`) refuses a mainnet `X402_NETWORK` with the x402.org facilitator. So if you set only `X402_NETWORK=base` while `X402_FACILITATOR_URL` still points at x402.org, the gateway refuses to start. It also refuses: a missing or malformed pay-to address; CDP credentials together with a bearer token; with CDP credentials, a missing `X402_FACILITATOR_CDP_API_KEY_SECRET`, a facilitator URL other than empty or exactly `https://api.cdp.coinbase.com/platform/v2/x402` (another host, the bare CDP host, another path, `http://`, a port, a query or a fragment; one trailing slash is allowed), or no `cdp-sdk` in the image; and without CDP credentials, a facilitator URL that is not `http(s)://`, or a plain `http://` URL on a mainnet or with a bearer token. All of these checks apply only when `X402_ENABLED=true`.

Set every row below **before** the deploy that ships the cutover. Then deploy once. Rehearse the same change on `staging` first. All of them are rendered into the env file by `.github/workflows/deploy.yml`: `/opt/swarm_connect.env` for `production` (`main`) and `/opt/swarm_connect_dev.env` for `staging` (`dev`).

| GitHub setting | Kind | Environment | At cutover (production) | If unset |
|---|---|---|---|---|
| `X402_ENABLED` | variable (`vars.*`) | production | `true` (already set) | `false` in both environments |
| `X402_NETWORK` | variable (`vars.*`) | production | `base` | production: **empty**, and the gateway refuses to start when `X402_ENABLED=true` (#370); staging: `base-sepolia` |
| `X402_FACILITATOR_URL` | variable (`vars.*`) | production | the mainnet facilitator's `https://` URL, for example `https://<mainnet-facilitator>/…`. With CDP credentials, leave it empty or set it to exactly `https://api.cdp.coinbase.com/platform/v2/x402`; nothing else is accepted (#527). Empty is recommended: the gateway then uses the URL `cdp-sdk` returns, which is the same one. | production: **empty**, and the gateway refuses to start when `X402_ENABLED=true`, unless CDP credentials are set; staging: `https://x402.org/facilitator` |
| `X402_PAY_TO_ADDRESS` | variable (`vars.*`) | production | `<mainnet pay-to address>`: the new address from item 1, not the staging one | empty, and the gateway refuses to start when `X402_ENABLED=true` |
| `X402_FACILITATOR_CDP_API_KEY_ID` | **secret** (`secrets.*`) | production | `<CDP API key id>` (CDP route only) | empty |
| `X402_FACILITATOR_CDP_API_KEY_SECRET` | **secret** (`secrets.*`) | production | `<CDP API key secret>` on one line (CDP route only) | empty |
| `X402_FACILITATOR_BEARER_TOKEN` | **secret** (`secrets.*`) | production | `<bearer token>` (bearer route only; leave empty with CDP) | empty |

Use **one** credential route: the two CDP secrets, or the bearer token. Do not use both. Set these settings on the `production` **environment**, not on the repository: an environment value overrides a repository value of the same name, and a repository value would also reach staging. For the staging rehearsal, set the same names on the `staging` environment.

```sh
gh variable set X402_NETWORK         --repo datafund/swarm_connect --env production --body base
gh variable set X402_FACILITATOR_URL --repo datafund/swarm_connect --env production --body '<mainnet facilitator URL>'
gh variable set X402_PAY_TO_ADDRESS  --repo datafund/swarm_connect --env production --body '<mainnet pay-to address>'
gh secret set X402_FACILITATOR_BEARER_TOKEN --repo datafund/swarm_connect --env production    # prompts for the value
```

**What a refused configuration looks like.** The deploy job runs the startup check (`python -m app.x402.check_config`) in a throwaway container. It uses the new image and the env the new container will get, and it runs **before** the running gateway is stopped (#485).

- **With the check (current `deploy.yml`):** the `deploy` run fails in its `deploy` step, with an `x402 configuration refused` error annotation (the check exited 3). The step log shows `x402 configuration check failed: Refusing to start with x402 enabled: …` and the reason. The previous container is **not** stopped or recreated, and it keeps serving with its old configuration. A refused check stops the whole `deploy` step, so the host-level updates later in that step (`docker compose -f docker-compose.host.yml up -d` and the Alloy recreate) also wait until the configuration is fixed.
- **If the check itself cannot run:** for example a compose interpolation error (a missing `SWARM_BEE_API_URL` or `SWARM_BEE_API_URL_DEV` in the host `.env` fails here, with `error while interpolating …`), a Docker daemon error, or a settings error from a malformed value such as `X402_ENABLED=maybe`. The annotation is then `x402 configuration check could not run`, with the exit code. This is not a verdict on the x402 settings, so read the output above it. A settings error appears as pydantic's traceback, which names the setting but not its value. The running gateway is not touched in this case either.
- **Without the check (deploys before #485, or a container recreated by hand):** the new container is created, refuses to start, and restarts in a loop. The `deploy` run fails at `wait for healthy`. `Refusing to start with x402 enabled: …` appears in the container log (`docker compose logs provenance_gateway`) and in the log tail of the failed step. The gateway is down until a good configuration is deployed.

**Recovering from a refused deploy.** Fix the GitHub variables and secrets, then re-run the deploy, or push again to `main`. Until then, **do not recreate the gateway by hand** (`docker compose up -d --force-recreate`, or `up -d` after a change): the deploy has already rewritten `/opt/swarm_connect.env` with the refused values, and a recreated container would read them and fail to start. `docker restart` and a host reboot keep the old values, because Docker reads `env_file` only when it creates a container.

## 2. Stopping traffic fast (kill switch)

Pick the smallest switch that stops the problem. **Do not** use `X402_ENABLED=false` as a kill switch: it turns payment *off* (everything becomes free) and removes stamp ownership enforcement.

**Setting changes: change the GitHub variable, then redeploy.** The deploy workflow rewrites `/opt/swarm_connect.env` (production) and `/opt/swarm_connect_dev.env` (staging) from GitHub variables on every deploy, so a change made only on the host is **reverted by the next deploy**. For the fastest effect, change both: first the GitHub variable in the environment (`production` / `staging`), then on the host:

```sh
cd /opt/swarm_connect
sudo sed -i 's/^X402_FREE_TIER_ENABLED=.*/X402_FREE_TIER_ENABLED=false/' /opt/swarm_connect.env    # production
docker compose up -d --force-recreate --no-deps provenance_gateway        # staging: provenance_gateway_dev and /opt/swarm_connect_dev.env
```

| To stop | Setting | Takes effect |
|---|---|---|
| Free-tier writes | `X402_FREE_TIER_ENABLED=false` | on recreate (~1 min) |
| All gateway BZZ spending | `GATEWAY_DAILY_BZZ_CEILING=0` *(requires #408)*: every purchase, extension and pool buy is refused with 503 | on recreate |
| The pool | `STAMP_POOL_ENABLED=false` | on recreate |

**Immediate, at the proxy** (no deploy involved). Edit `/etc/caddy/Caddyfile`, then validate and reload through systemd, so the environment in `/etc/default/caddy` (`GATEWAY_HOST` and friends) is used:

```caddyfile
# inside the site block: stop all API traffic, keep /health readable
respond /api/v1/* 503

# or block one abusive client
@blocked remote_ip 203.0.113.7
respond @blocked 403
```

```sh
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile && sudo systemctl reload caddy
```

## 3. Refunds and reconciliation

The audit log is `data/x402_audit.jsonl` on the persistent volume (`/opt/swarm_connect_data/` on the host). Loki also receives the gateway's log lines.

| Situation | What the log shows | Action |
|---|---|---|
| Paid and delivered | `payment_settled` (success, `transaction_hash`), then `payment_delivered` (same transaction, `resource`: batchID, reference or credited bytes) | None |
| Settlement refused | `payment_settled` with `success: false`. Nothing was delivered and nothing was collected by this request | None; the client was told to pay again. **Unless** an earlier `payment_failed` `stage: settle` record has the same `nonce`: then this refusal is probably that earlier transfer having spent the nonce (the client got `409 PAYMENT_AUTHORIZATION_OUTCOME_UNKNOWN`, not a 402). Handle it as "Settlement error (outcome unknown)" |
| Settlement error (outcome unknown) | `payment_failed` with `stage: settle` (reason `settlement interrupted (CancelledError)` when a shutdown or disconnect cut the wait short), counted as `result="error"`; a later retry of the same authorization refused by the facilitator, at verify or at settle, is counted as `result="refused_after_unknown"`. Nothing delivered (the client got `502 PAYMENT_SETTLEMENT_UNAVAILABLE`, or `502 PAYMENT_SETTLEMENT_UNKNOWN` if the route had already done its work). Every later attempt with the same authorization that is answered `409 PAYMENT_AUTHORIZATION_OUTCOME_UNKNOWN` logs `payment_failed` with `stage: outcome_unknown_retry`, the `nonce`, and in `reason` where it was stopped: `after_work` (its work had run; stopped before running again), `verify` or `settle` (the facilitator refused it, with its reason), or `in_use` (still reserved after an interrupted settlement). A parallel duplicate during a settlement is **not** one of these: it logs `stage: duplicate_in_progress` and the client got `409 PAYMENT_SETTLEMENT_IN_PROGRESS`; no action. A refusal at `settle` also logs `payment_settled` `success: false` | **Check on-chain** whether the authorization `nonce` in the `payment_failed` record was used (USDC `authorizationState(payer, nonce)`, or the payer's transfer with that nonce). **Match on the nonce, not on the time:** the documented recovery is a retry with the same authorization, which settles the same nonce. If a later `payment_settled` (`success: true`) or `payment_delivered` with the same `nonce` exists, that transfer paid for a delivery: **no refund**. Only a used nonce with no such later record is a refund |
| Purchase in flight at shutdown | `payment_failed` `delivery_after_settlement` with `Bee purchase in flight at shutdown, a batch may exist on-chain unlabelled` and the label | Look for a `recovered` batch with that depth and amount created after the `purchase_pending` start block; then as below: assign **or** refund |
| Paid stamp purchase, Bee response lost | `purchase_pending` (tx, label, depth, amount, start block, payer); the client got `202`. Later `payment_delivered` with `late: true` and the batchID (found and registered), or `payment_failed` `delivery_after_settlement` with `stamp purchase after settlement: not found`/`refused by Bee`/`already registered`/`lookup interrupted` | Found: none. Otherwise look in Bee `/stamps` for the label from `purchase_pending`. A batch Bee labelled `recovered` with that depth and amount, created after the start block, may be it: the gateway never assigns those itself, because a concurrent purchase could produce one too. Do **exactly one** of: register it to the payer (if you can tell it is theirs), or **refund**. Record which, next to the transaction in the ops log. After registering, tell the payer the batchID directly: a retry with their Idempotency-Key keeps answering `DELIVERY_FAILED_AFTER_PAYMENT` until the entry expires (24 h). To change that answer, stop the gateway, set the entry in `x402_idempotency.json` whose `transaction` matches to `"status": 201` and a base64 `{"batchID": …}` body, and start it again |
| **Paid, not delivered** | `payment_failed` with `stage: delivery_after_settlement` and `tx=` in the reason. The client received `x402_status: settled_not_delivered` and the transaction | **Refund** the transaction amount to the payer, or deliver manually |
| Idempotent retry answered | `payment_idempotent_replay` (the original `transaction_hash`), with no `payment_settled` of its own. The retry's payment was verified, never settled. Its `nonce` is the **retry's own** authorization (never settled), while its `transaction_hash` is the **original's**: match the original by `transaction_hash`, not by this `nonce` | None |
| Retry after an unanswered settlement | The client got `409 IDEMPOTENCY_KEY_SETTLEMENT_UNKNOWN` with the first `nonce`, and keeps getting it for 24 h; the first request logged `payment_failed` `stage: settle` with that `nonce` (or nothing, after a restart) | Same as "settlement error", matched on the `nonce`: the client may have retried the same authorization **without** the key (or under a new key) and been delivered, so look for a later `payment_settled` (`success: true`) or `payment_delivered` with that `nonce` first. If there is one: no refund; the 409s are expected until the entry expires. Otherwise, if the nonce was used: deliver or refund |
| Retry of a result too large to replay | The client got `409 IDEMPOTENCY_KEY_DELIVERED_NOT_STORED`; `payment_delivered` exists for the transaction | None: it was delivered |
| Retry of a paid request with no result | The client got `409 IDEMPOTENCY_KEY_SETTLED_PENDING` naming the transaction. A restart mid-request leaves no `payment_failed` line, only `payment_settled` without `payment_delivered` | Same as paid, not delivered: find what the transaction bought, deliver or **refund** |

Useful queries:

```sh
# Every payment that needs a refund
grep '"delivery_after_settlement"' /opt/swarm_connect_data/x402_audit.jsonl
# What a transaction paid for
grep '<tx hash>' /opt/swarm_connect_data/x402_audit.jsonl
```

Refunds are manual USDC transfers from the pay-to wallet to the payer address in the record. Note the refund transaction next to the record in your ops log.

**Stopping the gateway.** Paid stamp purchases can be waiting on Bee for minutes. On shutdown uvicorn stops in-flight requests after 10 s (`--timeout-graceful-shutdown 10`), then the gateway waits up to `SHUTDOWN_PENDING_PURCHASE_GRACE_SECONDS` (25) for pending purchases before cutting them off with a refund record. Keep the stop grace period above the sum: `stop_grace_period: 40s` in docker-compose, or `TimeoutStopSec=40` under systemd. Prefer deploying when `purchase_pending` has no recent unmatched entries.

**Payment state warnings in `/health`.** While the gateway cannot read or write `x402_settlement_unknown.json`, `/health` reports `status: degraded` and an `x402.warnings` entry starting `PAYMENT_STATE_UNAVAILABLE`, and every paid request gets `503 PAYMENT_STATE_UNAVAILABLE` (free tier unaffected). The warning names only the file and whether it is unreadable or unwritable; the full path, the error and any `.corrupt-<ts>` copy are in the log line (ERROR). Unreadable: repair it as below, then restart. Write failure (disk full, permissions on the data directory), also detected at startup by a test write: fix the cause; the gateway retries on the next paid request and resumes by itself, and the warning disappears. **Do not restart or redeploy before that:** entries recorded while writes failed exist only in memory and a restart loses them. Fix the cause, confirm the warning has cleared (a successful write), and only then restart. The gateway writes an entry before every settlement (write-ahead) and refuses to settle when it cannot, and removes it, also on disk, as soon as the facilitator answers definitely. So an entry left on disk after a restart is a real unknown outcome (one that got no answer, or a settlement cut off by a kill or crash), with one rare exception: a write-ahead whose request was cancelled or timed out before the facilitator was asked, and whose removal (a background write) had not finished when the process stopped or crashed. On a route that settles after its work (`after_work`), that entry refuses even the same-`X-PAYMENT` retry for 24 hours with `409 PAYMENT_AUTHORIZATION_OUTCOME_UNKNOWN`; the nonce check then shows it unused, and the client signs a new payment after `validBefore` or a `cancelAuthorization`. (Telling such an entry apart after a restart would need a further write per paid request, made before the facilitator is called; not done.) That entry only makes a refusal of that authorization a 409 instead of a 402, with no charge either way; match on the nonce as above and it shows no transfer. The idempotency store has the same kind of exception: a "settling" record whose write timed out (the client got `503 PAYMENT_STATE_UNAVAILABLE`, nothing settled) can land on disk after a crash that lost its removal; that key then answers `409 IDEMPOTENCY_KEY_SETTLEMENT_UNKNOWN` for 24 hours with nothing charged, and the nonce shows unused. A graceful restart leaves none behind for settled or refused payments. While the file cannot be written, a write is retried at most every 5 seconds (by paid requests or `/health` probes), so a failing disk is not hammered. An `x402.warnings` entry starting `IDEMPOTENCY_UNAVAILABLE` means the same for `x402_idempotency.json` (cause `unreadable`, `unwritable` or `unresponsive`), which refuses only keyed paid requests, so the status stays `ok`. Its writes run off the request path on their own I/O threads (separate from the settlement-unknown registry's), bounded by the same timeout: a write that fails or hangs refuses keyed paid requests only (`503 IDEMPOTENCY_UNAVAILABLE`), unkeyed paid requests keep working and the `/health` status stays `ok`; the gateway retries or probes in the background at most every 5 seconds. Writes of the whole file are coalesced: a burst of changes during a write costs one more write.

**Hung disk under the payment state.** Symptoms: paid requests get `503 PAYMENT_STATE_UNAVAILABLE`, the first ones about 5 seconds late (`X402_SETTLEMENT_STATE_WRITE_TIMEOUT_SECONDS`), later ones at once; `/health` reports `status: degraded` with `PAYMENT_STATE_UNAVAILABLE: x402_settlement_unknown.json is unresponsive`; the log has an ERROR "settlement-state disk I/O is not responding". Cause: writes to the data volume do not return (a stuck network or block device, a frozen filesystem); the gateway writes a record before every settlement and will not settle without it. No payment was settled for these requests. (A **full** disk is different: writes fail at once with ENOSPC, and the cause shown is `unwritable`, as described above.) Recovery: fix the storage. The first write that completes clears the warning and paid requests resume by themselves; while no write is pending the gateway probes with a write at most every 5 seconds, so the warning also clears with no paid traffic. Paid requests are refused at once while the oldest pending write is older than the timeout. A process that is stopped while a write hangs still exits (the writer threads do not hold it), and the hung write never landed, so nothing is lost. It may leave a temporary `.tmp-*.json` file next to `x402_settlement_unknown.json` in the data directory; it is incomplete and safe to delete. `X402_SETTLEMENT_STATE_WRITE_TIMEOUT_SECONDS` (default 5) must be greater than 0 and at most 60; any other value, including `nan` or `inf`, stops the gateway at startup.

## 4. Backups and restore

`scripts/backup_state.sh` archives `/opt/swarm_connect_data` and `/opt/swarm_connect_dev_data`. They hold the ownership registry, prepaid bandwidth credit, pool state, allowance and spend counters, stored Idempotency-Key results, and the audit log. If `x402_idempotency.json` is unreadable, the gateway keeps it, saves a `.corrupt-<ts>` copy, logs an ERROR and refuses keyed paid requests (`503 IDEMPOTENCY_UNAVAILABLE`); restore it from backup, or move it away to start empty (retries of the last 24 h are then charged again). `x402_settlement_unknown.json` (payment authorizations whose settlement got no answer, 24 h) is handled the same way, except that while it is unreadable **every** paid request is refused (`503 PAYMENT_STATE_UNAVAILABLE`, uncharged): restore it from backup, or move it away to start empty (a retry of an authorization from the last 24 h may then get a payable 402, or rerun work that already ran). Either way, **restart the gateway** afterwards: the file is read only at startup, and until then the 503 persists.

**The archives contain bearer credit tokens.** Treat every copy, including the off-host one, as secret, and set retention on the remote side as well: the script only prunes locally.

Install it **outside** the deployed checkout (every deploy re-clones `/opt/swarm_connect`), nightly, with an off-host copy:

```sh
# as root on each gateway host
install -m 0755 /opt/swarm_connect/scripts/backup_state.sh /usr/local/sbin/swarm_connect_backup
cat > /etc/cron.d/swarm_connect_backup <<'CRON'
15 3 * * * root BACKUP_REMOTE=backup@backuphost:/backups/swarm_connect /usr/local/sbin/swarm_connect_backup >> /var/log/swarm_connect_backup.log 2>&1
CRON
# root needs an ssh key authorised on backuphost, and backuphost in /root/.ssh/known_hosts
```

A successful run touches `/var/backups/swarm_connect/.last_success`. A run where any source failed to archive or copy exits non-zero and logs `FAILED:`.

To restore (production shown; for staging use `provenance_gateway_dev` and `/opt/swarm_connect_dev_data`):

```sh
cd /opt/swarm_connect
docker compose stop provenance_gateway
mv /opt/swarm_connect_data /opt/swarm_connect_data.broken
ls -t /var/backups/swarm_connect/swarm_connect_data-*.tar.gz | head -1        # latest local archive (or fetch from the remote)
tar -xzf /var/backups/swarm_connect/swarm_connect_data-<stamp>.tar.gz -C /opt
docker compose start provenance_gateway
curl -s localhost:8899/health
```

Then check an upload to a known owned stamp.

**State file unreadable** (#468). The other state files fail closed without stopping the gateway: it keeps the unreadable file, saves a `.corrupt-<timestamp>` copy, logs an ERROR naming both, and refuses what the file guards. `gateway_state_file_unreadable{file}` is 1 meanwhile, and the **State file unreadable** alert fires:

| `file` | What is refused | Clears |
|---|---|---|
| `pool` (`pool_state.json`) | Pool purchases and top-ups (spending paused, #547) | On the first full sync after the file is readable again; no restart needed |
| `pool_spend_times` (`pool_state_spend_times.json`, beside the pool state) | Pool purchases and top-ups: the hourly ceiling they share is treated as reached. No `.corrupt-` copy is kept for this file | By itself an hour after the start that found it unreadable; the next pool purchase or top-up then rewrites the file. A restart while it is still unreadable starts another hour |
| `settlement_unknown` (`x402_settlement_unknown.json`) | Every paid request: `503 PAYMENT_STATE_UNAVAILABLE` | After a restart |
| `idempotency` (`x402_idempotency.json`) | Paid requests with an Idempotency-Key: `503 IDEMPOTENCY_UNAVAILABLE` | After a restart |
| `spend_budget` (`stamp_spend_budget.json`) | Every stamp purchase, extension and for-owner batch whose spending limit is set: `503 SPEND_BUDGET_UNAVAILABLE`. Pool purchases and top-ups are internal: they are refused (logged, shown on `/pool/status`) with no response code | After a restart, or at the next UTC day |
| `pool_allowance` (`pool_allowance.json`) | Free pool acquires: `503 POOL_ALLOWANCE_UNAVAILABLE` (paid acquires work) | After a restart, or at the next UTC day |

Restore the file from backup, repair it, or move it away deliberately to start empty, then **restart the gateway**: all of these except the two pool files are read only at startup, so restoring the file alone changes nothing. While the spend file is unreadable, `GATEWAY_DAILY_BZZ_CEILING=-1` (on recreate) is a temporary override for spending that is charged to the gateway ceiling only: pool purchases and top-ups, and paid stamp purchases, paid extensions and paid for-owner batches **where a payment bypasses the giveaway budgets** (`paid_bypass_is_honoured()`: a mainnet network, or a testnet with `X402_ALLOW_TESTNET_PAID_BYPASS`). On a test network without that override a paid request is charged to the caller's budget and the free ceiling like an unpaid one, so it stays refused, as does all unpaid spending while those limits are set. Put the ceiling back once the file is restored. An unreadable ownership registry or bandwidth credit ledger does not show here: it stops startup, as below, and shows as **Gateway Down**.

If a money-bearing state file (ownership, credit) is unreadable, the gateway **refuses to start** and keeps a `.corrupt-<timestamp>` copy next to the file *(requires #395)*. The container then restart-loops until the file is restored or deliberately moved away. That's intended: starting empty would lock owners out and wipe prepaid balances.

**Ownership registry not saving** (#558). Before the gateway first rewrites an ownership registry from before #558 (batch ids in mixed case, or owner addresses not in lowercase), it copies the original to `<STAMP_OWNERSHIP_FILE>.pre-558-<UTC timestamp>` in the same directory (by default `stamp_owners.json.pre-558-<UTC timestamp>`). If that copy cannot be written, the gateway refuses every save of the registry, so the original is never overwritten. If the copy succeeds but the write of the registry file fails, the save fails too. In both cases the gateway keeps running, and ownership changes stay in memory only. A failed write (the second row below) can also happen on a registry that never needed migration, and the recovery is the same. While this lasts, `/health` shows one of these fixed texts in `state.warnings`, and each failed save logs an ERROR:

| Cause | `state.warnings` text | ERROR log line |
|---|---|---|
| The pre-migration copy cannot be written, so saves are refused | `ownership registry changes are not being saved: the pre-migration copy of the registry file could not be written. Changes are kept in memory only and are lost on restart. See the ERROR log and docs/mainnet-runbook.md` | `Not saving ownership state to <file>: the pre-migration file could not be copied aside (<error>); the change is kept in memory only` |
| The write of the registry file failed | `ownership registry changes are not being saved: the last write of the registry file failed. Changes are kept in memory only and are lost on restart. See the ERROR log and docs/mainnet-runbook.md` | `Failed to save ownership state to <file>: <error>` |

The error in the log line gives the cause, for example `No space left on device` or `Permission denied`. These warnings do not change `status`. `status` can still be `degraded` or `critical` for Bee node or wallet reasons, so a monitor that reads only `status` does not report a registry that is not saving. Check `state.warnings` itself.

Each failed or refused save also increments `gateway_state_save_failures_total{file="ownership"}`, and a failed write of the pool state file `gateway_state_save_failures_total{file="pool"}` (#506). `gateway_state_unsaved{file}` is 1 from the failed save until a save succeeds, and the **State file save failures** alert stays active for that time (section 5). Nothing retries a failed ownership save: it clears with the next ownership change that saves. The pool recovers the same way: fix the cause, and the next sync writes the file again.

What a restart would lose while a file is behind:
- **Ownership file behind only:** buyers lose access to batches they paid for.
- **Pool file behind only:** a batch just bought for the pool can be in neither the pool file nor the pending record, so after a restart it is paid for but not in the pool. A sold batch is still not re-imported: the ownership check (#477) stops it.
- **Both files behind:** a sold batch can come back as pool stock and be sold again.

So for either file: **do not restart** until `gateway_state_unsaved` is 0 again.

To recover:
1. **Do not restart the gateway.** A restart loses every ownership change made since it started, and the buyers of those batches lose access. The file on disk is intact. After a refused save it is the original, and the next start migrates it again.
2. Fix the cause from the log line: free disk space on the volume that holds the data directory, or fix permissions so the gateway user (uid 10001) can create files in that directory. Both the copy and the registry file are written as a temporary file in the same directory and then renamed.
3. Wait for the next ownership write: a stamp purchase, a pool acquire or a pool removal. It makes the copy first if one is still missing, then saves the whole registry from memory. There is no command to start a write.
4. Check that `state.warnings` in `/health` is empty again. After a refused save, also check that the log has `Kept the pre-migration ownership registry at <file>.pre-558-<timestamp>` and that the copy exists. Keep the copy: records merged from spellings of one batch id that differ only in case keep their `mode`, `acquired_at` and `source` only there.

**Audit log size.** `data/x402_audit.jsonl` grows with every paid request. Rotate it with logrotate, using `copytruncate` because the gateway keeps it open for appends:

```
/opt/swarm_connect_data/x402_audit.jsonl /opt/swarm_connect_dev_data/x402_audit.jsonl {
    monthly
    rotate 24
    compress
    missingok
    copytruncate
}
```

Rotated files stay on the volume, and are therefore in the backups.

### Bee node identity (keystores)

Each Bee node's identity is in `/home/bee/.bee/keys/` inside its volume: `swarm.key` (the Ethereum key, which is the node's wallet: xBZZ, xDAI, chequebook and stamp ownership), `libp2p_v2.key` and `pss.key`. The keys are encrypted with the keystore password (`BEE_PASSWORD`, `BEE_DEV_PASSWORD`). If a node is rebuilt with a new identity, it cannot spend the old wallet's funds.

This is a **separate backup class** from gateway state, with a separate script, a separate age key, a separate destination and separate cron lines:

- `scripts/backup_bee_keys.sh` reads only `keys/`. It reads the running volume read-only (`docker run --rm -v <volume>:/src:ro alpine:3.20`), so the node keeps running. It pipes the tar stream straight into `age -r <recipient>`, so no unencrypted keystore archive is ever written, locally or remotely. It does not back up the localstore, statestore or chain data.
- **Passwords are never in the backup.** The script does not read `BEE_PASSWORD` or `BEE_DEV_PASSWORD`. The passwords are kept in the secret manager (§6).
- The host holds only the **public** age recipient. The script refuses an `AGE-SECRET-KEY-…` value.
- The script also refuses to run when its destination equals or nests with the gateway-state destination (`BACKUP_REMOTE`) or its local directory overlaps `BACKUP_DIR`.

It is separate from `backup_state.sh`, not a flag on it. The wallet class needs its own recipient, destination, markers and cadence, and a separate file can be installed and permissioned on its own without touching the nightly state job. The S3 target in #493 can be added to both scripts later.

**Setup**, as root on each gateway host:

```sh
apt-get install -y age util-linux     # util-linux provides flock(1), which the script requires
install -m 0700 /opt/swarm_connect/scripts/backup_bee_keys.sh  /usr/local/sbin/swarm_connect_backup_bee_keys
install -m 0700 /opt/swarm_connect/scripts/restore_bee_keys.sh /usr/local/sbin/swarm_connect_restore_bee_keys
# settings: one root-only file, read by the script itself on every run (cron and by hand)
install -d -m 0700 /etc/swarm_connect
install -m 0600 /dev/null /etc/swarm_connect/bee_keys.env
cat > /etc/swarm_connect/bee_keys.env <<'ENV'
BEE_KEYS_REMOTE=<bee-keys-backup-user>@<backup-host>:<bee-keys-path>
BEE_KEYS_AGE_RECIPIENT=<bee-keys-age-recipient>
BACKUP_REMOTE=<the same value as the gateway-state cron line>
ENV
cat > /etc/cron.d/swarm_connect_backup_bee_keys <<'CRON'
SHELL=/bin/bash
# on change: hourly, uploads only when a keystore file changed
40 * * * * root /usr/local/sbin/swarm_connect_backup_bee_keys >> /var/log/swarm_connect_backup_bee_keys.log 2>&1
# weekly re-check by checksum: live keys and the remote copy against the record
50 4 * * 1 root /usr/local/sbin/swarm_connect_backup_bee_keys --verify >> /var/log/swarm_connect_backup_bee_keys.log 2>&1
CRON
/usr/local/sbin/swarm_connect_backup_bee_keys    # first run by hand; it must end with "copied off-host"
```

root needs an ssh key authorised on `<backup-host>` for `<bee-keys-backup-user>`, and `<backup-host>` in `/root/.ssh/known_hosts`. The copy runs with `BatchMode=yes`, so an unknown host key fails the run instead of prompting. Add the key once by hand (`ssh <bee-keys-backup-user>@<backup-host> true`, and compare the fingerprint with one obtained out of band).

The script reads `/etc/swarm_connect/bee_keys.env` on every run, so a manual run (`/usr/local/sbin/swarm_connect_backup_bee_keys`, or with `--verify`) uses exactly what cron uses; there is nothing to export first. The file is parsed, never sourced, so nothing in it runs. Its format:
- blank lines and `#` comments;
- `KEY=VALUE` lines for the known settings only (`BEE_KEYS_REMOTE`, `BEE_KEYS_AGE_RECIPIENT`, `BACKUP_REMOTE`, `BEE_KEYS_NODES`, `BEE_KEYS_DIR`, `BEE_KEYS_KEEP`, `BEE_KEYS_HELPER_IMAGE`, `BEE_KEYS_SSH_TIMEOUT`, `BACKUP_DIR`), with no `export` and no spaces around `=`;
- a value that is one word, or that is in one pair of single or double quotes. Quotes are required for several recipients, as in `BEE_KEYS_AGE_RECIPIENT="age1… age1…"`.

Values are literal. `$`, backquote and backslash are refused, and nothing is expanded. Any other line is refused (exit 2) with its line number, and its value is not echoed. That includes a key that is not exactly one of the names above, and a key set twice. The script also refuses the file if it is a symlink, is not a regular file, is not owned by root, or can be written by group or others. It refuses a parent directory not owned by root, or one that group or others can write. The directories above that, `/etc` itself, are assumed to be root-owned system paths. The values are checked too:
- `BEE_KEYS_SSH_TIMEOUT` must be 1 to 3600, and `BEE_KEYS_KEEP` a whole number with no leading zero;
- the volume half of each `BEE_KEYS_NODES` entry must be a Docker volume name, never a path;
- recipients and nodes may not contain `*`, `?` or `[`. Values in the file override the environment. `BEE_KEYS_ENV_FILE` names another file, by absolute path. The file holds only public values: the age recipient is the public half, never the `AGE-SECRET-KEY-…` identity, which the script refuses.

`BACKUP_REMOTE` is repeated in this file only so the script can refuse a shared destination. Without it, the script logs a warning. Both values must be absolute (`/path` or `[user@]host:/path`). The script refuses a relative or empty remote path, because it cannot compare one. It compares them after dropping `user@`, lower-casing the host and collapsing `//`, `/./` and a trailing `/`. The check is best effort: it cannot tell that an ssh alias and the host name it points to are the same host. Keep the two paths distinct yourself. Use a different remote path from the gateway-state archives, and if possible a different account with its own ssh key. The defaults read the compose volumes `swarm_connect_bee-data` (node `prod`) and `swarm_connect_bee-dev-data` (node `dev`). Set `BEE_KEYS_NODES` to change them.

**What a run does.** Each run starts one helper container per node. The container copies `keys/` once into its own tmpfs (memory, gone with the container), and takes both the archive and its manifest from that copy, so the two always describe the same files. tmpfs pages can be written to swap, and the gateway hosts have swap. Under a non-default Docker `default-ipc-mode: host`, `/dev/shm` would be the host's own and the copy could outlive the container. The keystore files are scrypt-encrypted with the keystore password, so the exposure is small. If it matters, run the hosts without swap (or with encrypted swap) and keep Docker's default IPC mode. The manifest is the sha256 of every regular file in `keys/`, dotfiles included; any other kind of entry fails the node. The archive is encrypted on the way out. Every helper run that carries keystore bytes or the manifest uses `--log-driver none`, because the default `json-file` driver would otherwise copy the container's stdout (the plaintext tar) to `/var/lib/docker/containers/` on the host disk, and a `journald` or `syslog` driver would keep it or ship it off the host. The run compares the manifest with the one recorded at the last successful upload (`/var/backups/swarm_connect_bee_keys/<node>.manifest`). It also compares the destination and the set of age recipients with those recorded in `<node>.last`. If none of them changed, it discards the new ciphertext and uploads nothing. A different `BEE_KEYS_REMOTE` or `BEE_KEYS_AGE_RECIPIENT` counts as a change, so moving the destination or rotating the age key uploads a fresh copy on the next run. Otherwise it keeps `bee-keys-<node>-<stamp>.tar.age` and its manifest `bee-keys-<node>-<stamp>.sha256` (checksums only, not secret), copies both off-host with rsync (or scp when rsync is missing), and only then updates the record. A failed copy is therefore retried on the next run. Old local archives are pruned to the newest `BEE_KEYS_KEEP` (default 3) after failed copies too, so a backup host that stays unreachable cannot fill the disk; the archive just written is always kept. `--verify` recomputes the live checksums and fetches the remote ciphertext and manifest back from the destination of the last upload; if `BEE_KEYS_REMOTE` now names another destination, it fails and asks for a normal run first. It compares the live checksums with the record and the ciphertext with the sha256 recorded at upload. It needs no decryption key and uploads nothing.

Only one run at a time works in `/var/backups/swarm_connect_bee_keys`. The lock is `flock -n` (util-linux) on `.lock`. A run that overlaps another fails with `FAILED: another run …`. The kernel releases the lock when the script exits, however it exits. Its children (docker, age, rsync, scp, ssh) are started without the lock's file descriptor, so a stray child that outlives the script does not keep the lock. The script refuses to run without `flock`. rsync and scp use `ssh -o ConnectTimeout=20 -o BatchMode=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=4` (`BEE_KEYS_SSH_TIMEOUT` changes the connect timeout), so an unreachable or stalled backup host fails the run instead of hanging it. If runs keep failing with `another run holds …` while no run should be active, a hung run is holding it: find it with `fuser -v /var/backups/swarm_connect_bee_keys/.lock` (package `psmisc`) or `lsof /var/backups/swarm_connect_bee_keys/.lock`, and kill that process.

Failure semantics are the same as `backup_state.sh`. Every failure logs a `FAILED:` line, and the other nodes are still processed. The exit status is 1 on any failure and 2 on a configuration refusal. `.last_success` (normal run) and `.last_verify` (`--verify`) in `/var/backups/swarm_connect_bee_keys/` are touched only when every node succeeded. Alert when either is older than its cron period plus a margin. A `checksum mismatch, live keystores differ` from `--verify` means a keystore changed without a successful backup run. Investigate it, because keys change only at provisioning.

**Record privately, per node and environment**, in the team's private ops record, never in this repository: the Ethereum address, the overlay and the chequebook address. Read them from the node's `/addresses` and `/chequebook/address`. The restore check needs the overlay and the address.

#### Restore a node with its original identity on a fresh host

What makes the overlay come back is verified in the Bee 2.8.1 source and by the drill below. The overlay is derived from the Ethereum key, the network ID and a **nonce kept in the statestore, not in `keys/`**: key `overlayV2_nonce` (`pkg/node/statestore.go:63`, `:85-101`), used in `pkg/node/node.go:315-320`.

On mainnet, a node with no stored nonce fetches a suggested neighbourhood from `--neighborhood-suggester` (default set for mainnet only, `cmd/bee/cmd/start.go:269-272`). It then mines a nonce so that its overlay lands there (`pkg/node/node.go:326-370`). The production nodes therefore almost certainly have a non-zero nonce; `check` (step 4) reports which. After a keys-only restore, the node would mine a new nonce for whatever neighbourhood is suggested that day, and get a **different overlay**. This follows from the source; the drill measures the networkless case only.

Mining is deterministic: it starts at nonce 0 and stops at the first nonce that matches (`pkg/util/nbhdutil/miner.go:17-47`). Passing `--target-neighborhood` with the first 31 bits of the recorded overlay therefore finds the original nonce. 31 is the maximum, because proximity is capped at `MaxPO` = 31 (`pkg/swarm/swarm.go`). Bee stores the found nonce in the new statestore, and later starts keep the overlay without the flag. `restore_bee_keys.sh check` does this. This recovery assumes the nonce was mined sequentially from 0, as Bee 2.x does. An identity whose nonce came from elsewhere, for example from Bee 1.x or set by hand, would show `MISMATCH`, and the hint cannot recover it. The 2.8.1 nodes this runbook covers mined their nonce with Bee 2.x.

**Nonce 0 (verified in source and by the drill).** Bee 2.8.1 writes `overlayV2_nonce` only when it mines (`pkg/node/node.go:335-370`). If an identity's overlay uses nonce 0, **no nonce is stored**, on the live node as well as after a restore. On mainnet, every start without a stored nonce and without a hint fetches a fresh neighbourhood suggestion and mines if the overlay is not in it (`pkg/node/node.go:315-370`). So a nonce-0 node can get a **different overlay on any restart with network**, live or restored, not only after a restore. This follows from the source; it is not measured, because measuring it needs the network.

`check` tells you whether an identity is nonce-0. It starts Bee on a temporary copy of `keys/` with an empty `--neighborhood-suggester`, which gives the nonce-0 overlay, and compares that with the recorded overlay. It prints either `nonce: non-zero; …` or `WARNING: this identity uses overlay nonce 0 … BEE_TARGET_NEIGHBORHOOD=<bits>`. Run it on a scratch restore from the backup (the manual drill below) to learn this for a live node without touching it.

For a nonce-0 node, every start must carry the hint. Bee 2.8.1 reads it from `BEE_TARGET_NEIGHBORHOOD` (measured locally). `docker-compose.host.yml` does not yet pass that variable to the `bee` and `bee-dev` services. That is an open follow-up, and adding it recreates the Bee containers on the next deploy. Until it is in place, avoid restarting a nonce-0 node, and do not start a restored nonce-0 node with network.

**Before you start:** step 5 is a deploy, so the new host needs a self-hosted GitHub Actions runner registered for this repository, with a label listed in the `DEPLOY_RUNNERS` repository variable (see the `build` job in `.github/workflows/deploy.yml`). Register it before step 5, and keep `COMPOSE_PROFILES=bee` out of `/opt/swarm_connect_host.env` until every node is restored.

0. **Get the restore script onto the new host without a deploy.** A deploy would create `/opt/swarm_connect`, but with `COMPOSE_PROFILES=bee` it would also start Bee on an empty volume (step 1). Install Docker, then copy the script from a checkout of this repository at the commit the gateways run:

   ```sh
   # on your machine, in a checkout at the deployed commit
   scp scripts/restore_bee_keys.sh root@<new-host>:/root/restore_bee_keys.sh
   ssh root@<new-host> 'install -m 0700 /root/restore_bee_keys.sh /usr/local/sbin/swarm_connect_restore_bee_keys && rm /root/restore_bee_keys.sh'
   ```

   The host must be able to pull `alpine:3.20`, `ethersphere/bee:<BEE_VERSION>` and `curlimages/curl:8.10.1`.
1. **Do not let Bee start on the new host with an empty volume.** Bee would generate a new identity. Restore before the first deploy that has `COMPOSE_PROFILES=bee` in `/opt/swarm_connect_host.env`. If a fresh Bee already started there, stop it and confirm that its new address holds no funds. Then remove its volume:

   ```sh
   cd /opt/swarm_connect
   docker compose -f docker-compose.host.yml rm -sf bee      # bee-dev for staging
   docker volume rm swarm_connect_bee-data                   # swarm_connect_bee-dev-data for staging
   ```

2. Pick the archive: the one named in the latest `verified <node>: … remote bee-keys-<node>-<stamp>.tar.age match the record` line of `/var/log/swarm_connect_backup_bee_keys.log`. If that log was lost with the host, use the newest `bee-keys-<node>-<stamp>.tar.age` in `<bee-keys-path>`. Fetch it and the `.sha256` with the same `<node>-<stamp>`. Copy the `.sha256` (it is not secret) to the new host as `/root/bee-keys-<node>-<stamp>.sha256`. Step 3 reads it from there. Give the archive to the age identity holder.
3. The **age identity holder** decrypts on their own machine and pipes the result straight into the restore. The identity never goes to the gateway host:

   ```sh
   age -d -i <age-identity-file> bee-keys-prod-<stamp>.tar.age \
     | ssh root@<new-host> /usr/local/sbin/swarm_connect_restore_bee_keys restore swarm_connect_bee-data /root/bee-keys-prod-<stamp>.sha256
   ```

   The manifest is required. The restore refuses any volume that is not empty, and extracts only `keys/`. The restored files must match the manifest exactly: the same names, the same checksums and no extra files. It sets the owner to the Bee image's user, with mode 700 on the directory and 600 on the files, and confirms the result. Every regular file counts, dotfiles included. On any failure, including a truncated or corrupt stream or an interrupt (Ctrl-C, a dropped ssh session), it removes the restored `keys/`, so the restore can simply be run again. Only if both sides were killed outright (`kill -9`) can a partial `keys/` remain. Clear it with `docker run --rm -v swarm_connect_bee-data:/dst alpine:3.20 rm -rf /dst/keys`. Compose prints a one-time warning that the volume was not created by Compose. That warning is harmless.
4. On the new host, prove the identity with no network. Take the password from the secret manager without putting it on a command line:

   ```sh
   read -rs BEE_PASSWORD && export BEE_PASSWORD
   /usr/local/sbin/swarm_connect_restore_bee_keys check swarm_connect_bee-data <recorded-overlay> <recorded-ethereum-address>
   unset BEE_PASSWORD
   ```

   `-e BEE_PASSWORD` puts the password into the check containers' configuration, so `docker inspect` shows it while they run. Anyone who can use Docker on the host can read it. Run `check` only on a host where only root can use Docker (no one else in the `docker` group). The script removes its containers when it exits. If it was killed hard, run `docker rm -f $(docker ps -aq --filter name=bee-identity-)`, and `docker volume rm` any `bee-identity-probe-*` volume.

   If it prints `MISMATCH` instead (a mistyped overlay, the wrong node's archive), do not deploy on that volume: Bee may have mined and stored a nonce for the wrong target in its statestore. Remove the volume (`docker volume rm swarm_connect_bee-data`), restore again from step 3, and check with the recorded values.

   This must print `identity matches`, then either `nonce: non-zero` (the original nonce is now stored in the volume) or the nonce-0 `WARNING` (see above). It uses `ethersphere/bee:${BEE_VERSION:-2.8.1}`, so export the host's `BEE_VERSION` first if it differs.
5. **Restore and check every node on the host first.** `COMPOSE_PROFILES=bee` starts both `bee` and `bee-dev`, so a node not yet restored would start on an empty volume and create a NEW identity. Repeat steps 2-4 for `swarm_connect_bee-dev-data` (node `dev`, `BEE_DEV_PASSWORD`) before going on. Then put the password into `/opt/swarm_connect_host.env` (`BEE_PASSWORD=` or `BEE_DEV_PASSWORD=`), along with the rest of the host settings (`COMPOSE_PROFILES=bee` and so on). Compose reads it from `/opt/swarm_connect/.env`, which the deploy rebuilds from `/opt/swarm_connect_host.env`, so **deploy** (re-run the deploy workflow for this host). The deploy runs `docker compose -f docker-compose.host.yml up -d`, which creates `bee` on the restored volume. If the checkout and its `.env` already exist and you cannot deploy now, this is the equivalent by hand:

   ```sh
   cd /opt/swarm_connect
   grep -E '^(BEE_PASSWORD|BEE_DEV_PASSWORD)=' /opt/swarm_connect_host.env >> .env
   docker compose -f docker-compose.host.yml up -d bee bee-dev
   ```

   The next deploy rebuilds `.env` from the host file, so the two stay consistent. Check the running node (`swarm_connect-bee-dev-1` for staging):

   ```sh
   docker run --rm --network container:swarm_connect-bee-1 curlimages/curl:8.10.1 -s localhost:1633/addresses
   ```

   It must report the recorded address and overlay.
6. Expect these consequences of a keys-only restore:
   - **Chequebook (verified in source, not exercised by the drill):** the chequebook address is in the statestore (`swap_chequebook`). When it is missing, Bee deploys a **new** chequebook, with the node's Ethereum address as issuer (`pkg/settlement/swap/chequebook/init.go:144-192`). This needs xDAI for gas. Withdrawing what is left in the **old** chequebook with the same key is an **open question**: it is not verified here. Check it against the chequebook contract before relying on it, using the recorded old chequebook address.
   - **Postage stamps (verified in source, consequence open):** batches owned by the address are re-added from chain events as "recovered" issuers with **fresh bucket counters** (`pkg/postage/service.go:171-182`, `pkg/postage/batchservice/batchservice.go:218`). The old counters were in `stamperstore/`, which is not backed up. What re-used stamp indices do to data already uploaded under those batches is an **open question**. Until it is answered, do not upload to pre-restore batches from a restored node, and buy new ones.
   - For a **planned** move to a new host, avoid both by stopping the node and moving the whole volume. The keys-only backup is for disaster recovery.

**Local drill (automated, throwaway identity):** `scripts/bee_keys_drill.sh`, or `RUN_BEE_DRILL=1 python -m pytest tests/test_bee_keys_drill.py -s`. It needs Docker and `age`. It creates an identity in `ethersphere/bee:2.8.1` with `--network none` and a mined nonce, and backs it up with the script while the node runs. It then runs `--verify` and confirms that a second run uploads nothing. Next it wipes the container and volume and restores into a fresh volume. It shows that a truncated stream fails and leaves the volume empty for a retry, and that an unhinted networkless start fails. Finally it checks the restored identity with `restore_bee_keys.sh check`, which must report a non-zero nonce, and restarts the node without the hint. It also checks the same keys against their nonce-0 overlay, which must produce the nonce-0 warning. It records every `docker` invocation and fails if any helper run that reads `keys/` or takes the decrypted tar lacks `--log-driver none`, and it shows on the local daemon that the default driver keeps a container's stdout while `none` keeps nothing. It passes only if the address and overlay match each time. On macOS, which has no `flock`, it supplies a stand-in for the run.

**Drill on the real nodes (manual; do it once per host and after every change to these scripts).** It touches only the real keys' ciphertext and a scratch volume. It never touches the live volume.

- [ ] The age identity holder and the password holder are both available. Have the recorded address and overlay of the node.
- [ ] On the gateway host, fetch the newest archive and manifest back from `<bee-keys-path>` into a root-only temp directory:

  ```sh
  install -d -m 0700 /root/beedrill && cd /root/beedrill
  newest="$(ssh <bee-keys-backup-user>@<backup-host> 'ls -1 <bee-keys-path>/bee-keys-prod-*.tar.age | sort | tail -n 1')"
  rsync -a "<bee-keys-backup-user>@<backup-host>:$newest" "<bee-keys-backup-user>@<backup-host>:${newest%.tar.age}.sha256" .
  ls -l    # bee-keys-prod-<stamp>.tar.age and bee-keys-prod-<stamp>.sha256
  ```

- [ ] On the identity holder's machine, fetch the archive (ciphertext only) and pipe the decryption into the restore. The identity never leaves that machine:

  ```sh
  scp root@<gateway-host>:/root/beedrill/bee-keys-prod-<stamp>.tar.age .
  age -d -i <age-identity-file> bee-keys-prod-<stamp>.tar.age \
    | ssh root@<gateway-host> /usr/local/sbin/swarm_connect_restore_bee_keys restore beedrill-scratch /root/beedrill/bee-keys-prod-<stamp>.sha256
  ```

  Expect `restored keys into beedrill-scratch`.
- [ ] On the host, with `BEE_PASSWORD` read as in step 4: `swarm_connect_restore_bee_keys check beedrill-scratch <recorded-overlay> <recorded-ethereum-address>`. Expect `identity matches`, and note whether it prints `nonce: non-zero` or the nonce-0 `WARNING` in the private record. The check containers have `--network none`, so the two copies of the identity never meet on the network.
- [ ] Remove the scratch volume and the fetched files: `docker volume rm beedrill-scratch && rm -rf /root/beedrill` on the gateway host. The keystore files in the volume are still encrypted with the password, but remove them anyway. On the identity holder's machine, delete the copied `bee-keys-prod-<stamp>.tar.age`.
- [ ] Record the date, the archive name and the result in the private ops record. Do not record the identifiers in this repository.

## 5. Alerts that matter for money

| Alert | Meaning | First action |
|---|---|---|
| **x402 settlement problems** | `gateway_x402_settlements_total{result!="settled"}` increased: refused, error, settled_not_delivered or refused_after_unknown | Read the audit log (section 3). For `settled_not_delivered`, refund. For `refused_after_unknown` (a retry of a payment whose settlement got no answer was refused, so the earlier transfer probably went through), handle it as "Settlement error (outcome unknown)", matched on the nonce |
| **State file save failures** | `gateway_state_save_failures_total{file}` increased in 15 minutes, or `gateway_state_unsaved{file}` is 1: the gateway could not write the pool state (`file=pool`) or the ownership registry (`file=ownership`) and the change is still in memory only | **Do not restart.** Read the ERROR log line, free disk space or fix permissions on the data directory, and wait for the next save to succeed (section 4, "Ownership registry not saving"). A restart before then loses the changes: ownership behind, buyers lose their batches; pool behind only, a just-bought pool batch is paid for but not in the pool; both behind, a sold batch can come back as pool stock |
| **State file unreadable** | `gateway_state_file_unreadable{file}` is 1: a state file that fails closed could not be read, and what it guards is refused | Restore the file from backup, repair it, or move it away deliberately, then restart (section 4, "State file unreadable") |
| **Bee HTTP pool slots stuck** (warning) | `gateway_bee_http_pool_connections{state="stuck"}` has been at least 10% of `gateway_bee_http_pool_max_connections` for 10 minutes: cancelled requests left pool slots that serve nothing (#587). Each lowers the capacity toward Bee and Base RPC; the gateway replaces the client by itself only from 25% stuck plus repeated `PoolTimeout`s | Find the source of the cancellations (load test, disconnecting clients, slow Bee answers). If the stuck count keeps rising, restart the gateway container at a quiet moment (it clears the pool, but cuts off every request in flight: uploads, downloads and paid purchases still waiting on Bee). `gateway_bee_http_client_replacements_total{result}` shows whether the self-recovery ran; `probe_failed` means Bee itself does not answer |
| **State file writes blocked** (#585) | `gateway_state_write_blocked{file,reason}` is 1 for 1 minute: a write of the settlement-unknown registry (`file=settlement_unknown`: every paid request gets 503 `PAYMENT_STATE_UNAVAILABLE`) or of the idempotency store (`file=idempotency`: paid requests with an `Idempotency-Key` get 503 `IDEMPOTENCY_UNAVAILABLE`) failed (`reason=failed`) or did not finish in time (`reason=stalled`). Nothing is charged for a refused request | Fix the cause named in the ERROR log (disk full, permissions, a hung volume). It clears by itself once a write succeeds; **no restart needed**. Restart only if `reason=stalled` stays after the disk responds again. See "Blocked state writes" below |
| **BZZ pricing rate off market** *(requires #410)* | `X402_BZZ_USD_RATE` is more than 2× away from the market | Review and set the rate |
| Low BZZ / xDAI / chequebook (existing) | The Bee node can't buy or upload | Fund the node wallet on Gnosis |
| Gateway spend near its ceiling (once the spend-ceiling change is deployed) | `gateway_spend_bzz_today` approaching `gateway_spend_ceiling_bzz` | Check for abuse; raise the ceiling only deliberately |
| **Balance check failing** (#586) | `gateway_balance_check_ok{check}` is 0, or no read of that balance succeeded for 15 minutes, for 15 minutes. The balance gauges keep their last value when a read fails, so the balance alerts above are judging an old number | Read the `Failed to check ... balance` lines in the gateway log. `xbzz`/`xdai`/`chequebook` come from the Bee node, and usually fire with **Bee calls failing**. `base_eth` comes from the Base RPC, `gnosis_signer` from the Gnosis RPC |

### Blocked state writes (#585)

The two payment state files are rewritten as paid requests go through. While a write fails or stalls, the gateway refuses the requests that need it, unpaid, rather than settle a payment it could not record. `gateway_state_file_unreadable` does not cover this: it is about a file that could not be read at startup.

- **What clears it.** A write that succeeds. A background task retries a failed or stalled write about every 5 to 10 seconds, so a recovered disk clears the flag with no paid traffic and no restart. *Tested* (`tests/test_state_write_blocked_metric.py`): the gauge reads 1 while a write fails or stalls and 0 once a write completes; a recovered disk clears both stores with no requests and no scrapes; reading the gauge never starts a write.
- **`reason=stalled` before any refusal.** It also reads 1 when a write has been pending longer than `X402_SETTLEMENT_STATE_WRITE_TIMEOUT_SECONDS` (for the registry, also when 64 writes are pending), because the next paid request is refused then. *Tested.*
- **Is a restart safe?** For payments, yes. A settlement write-ahead that fails is taken back and its request is not settled (*tested*). An idempotency key's `settling` record is written, and the write awaited, before its payment settles (*read from code*, `arecord_settling` in `app/x402/idempotency.py`). What a restart can lose: a keyed request that settled after the writes began to fail has its later record in memory only, so a retry with that key answers 409 instead of the stored result, until the key expires (24 hours) (*read from code*).
- **When a restart is required.** When `reason=stalled` stays after the disk responds again. A write stuck in the kernel keeps its worker thread, and only a completed write clears a stall (*read from code*). Shutdown waits 5 seconds for pending writes and then exits without them. If the file cannot be read after the restart, **State file unreadable** fires instead; if it can be read but not written, the registry's startup test write fails and this alert fires again (*read from code*).

### Availability alerts (#586)

`Gateway Down` reads `up`, which only says Alloy could scrape `/metrics`. `/metrics` never calls Bee. On 2026-11-03 staging answered `/metrics` for over 30 minutes while every Bee call failed, `/health` took more than 10 s and the proxy answered 502, and nothing alerted. These rules cover that case.

| Alert | Meaning | First action |
|---|---|---|
| **Bee calls failing** (critical) | Over 20 failed gateway-to-Bee calls (`gateway_bee_api_errors_total`) in 5 minutes, for 5 minutes. Normal is about 2; the incident reached 619 | Check which endpoint fails (the "Bee API Errors" panel) and whether Bee itself is up (`bee_up`, the Bee log). `PoolTimeout` in the gateway log while a fresh request to Bee works means the shared HTTP client is stuck (#587); today only a restart of the gateway container clears it. **Check "State file save failures" before restarting** |
| **Gateway 5xx responses** (critical) | Over 20% of responses to real requests were 5xx in 10 minutes, with at least 20 real requests, for 5 minutes. Health traffic (`/`, `/health`: healthchecks and the probe, about 20 per 10 minutes) is left out of both, so it cannot meet the guard on its own or dilute the share | The "HTTP Status Code Breakdown" panel and the gateway log. 502/503/504 from the gateway usually mean Bee calls are failing |
| **Public health check failing** (critical) | Alloy's probe of the public `/health` got no 200 within 8 s for 5 minutes. This also covers Docker's `unhealthy` state (below) | `curl -m 10` the URL; on the host, `docker ps` for the container's health, then the reverse proxy and the gateway log. If it has failed ever since the Alloy deploy that added it, see "After the first deploy" |
| **Public health check slow** (warning) | The probe averaged over 5 s over 10 minutes, for 10 minutes | `/health` makes uncached upstream calls to Bee and the Base RPC on every request (#588). Production measured about 3.5 s on 2026-11-03, mostly the Base RPC; #588's cached `/health` should bring it under 1 s. Check those upstreams |
| **Bee node not healthy (public /health)** (critical) | `/health` answered 200 but without `bee_node.healthy: true` on every probe for 10 minutes, then for 5 more minutes: it fires about 15 minutes after the node stops being healthy | Read `bee_node.warnings` in `/health`. "could not query the Bee node" is the gateway-to-Bee path (see Bee calls failing); "network availability" or a low peer count is the Bee node itself |
| **Public health probe missing** (warning) | The gateway reports metrics but no probe result arrived for that environment in 15 minutes, for 30 minutes. The three probe alerts are silent with no data, so without this the probe could disappear unnoticed | Usually a main deploy that removed the probe while it is only on dev (see below). Otherwise Alloy failed to load its config (`docker logs` of the Alloy container) or was not recreated |

**Docker `unhealthy`: alert only, no automatic restart.** The container healthcheck (`curl -f http://localhost:8000/`, 10 s timeout, 3 retries, every 30 s) calls the same handler as `/health`, and `/health` always answers 200 when the gateway answers at all. So Docker marks the container unhealthy only when that handler is slow or not answering, and each of those checks also fails the probe, which has the stricter 8 s timeout. **Public health check failing** therefore fires whenever the container is unhealthy. Nothing restarts the container on it, deliberately: a restart loses any change still in memory only (see **State file save failures** above). Restart by hand, once `gateway_state_unsaved` is 0.

**What the probe does not see.** It runs in Alloy on the gateway host and calls the public hostnames through DNS, TLS and the reverse proxy. If the whole host or its network goes away, the probe goes with it; **Gateway Down** and **No Data** cover that partly. `monitoring/alerting/README.md` describes Grafana Synthetic Monitoring as the option for a check from outside.

#### After the first deploy that adds the probe

**Which deploy adds the probe.** Every deploy clones *its own branch* into the host checkout (`git clone -b $GITHUB_REF_NAME`) and recreates Alloy from that checkout's `monitoring/alloy/config.alloy`. Both environments share one Alloy, so it runs the config of whichever branch deployed last. While this change is only on `dev`, a dev deploy adds the probe for both environments and **any main deploy removes it again** until the next dev deploy. The probe rules have `noDataState: OK`, so they go quiet; **Public health probe missing** fires after about 45 minutes, so the loss is not silent. Expect it to fire after each main deploy until the change reaches `main`. Then redeploy dev, or merge to main, rather than pausing it.

Apply the alert rules (`scripts/apply_grafana.py`) after the dev deploy that adds the probe. Applied earlier, **Public health probe missing** fires for both environments, correctly, because no probe is running yet.

After the dev deploy, **and after every main deploy until the change is on main**, check that the series arrive:

1. In Grafana Explore, query `probe_http_status_code{job="swarm_connect/health_probe"}`. There should be one series per environment (`environment="main"` and `"development"`), at 200, within two minutes of the deploy.
2. Query `probe_failed_due_to_regex{job="swarm_connect/health_probe"}`. It should be 0 while `/health` shows `bee_node.healthy: true`.
3. Check `probe_duration_seconds{job="swarm_connect/health_probe"}` against the 5 s slow threshold.

If the series are missing, Alloy did not load the config: `docker logs` of the Alloy container for a config error, and check the deploy recreated it. If they are at 0 (timeout or connection failure) while the URL answers from elsewhere, the host cannot reach its own public hostname from inside the Docker network (hairpin NAT). A gateway container on the deploy host reached both URLs on 2026-11-03, so this is not expected. If it happens, check it from inside the Alloy container (`docker exec <alloy> ...`; the image may lack curl or wget, so a one-off `docker run --rm --network swarm_connect_default curlimages/curl -m 10 -sS -o /dev/null -w '%{http_code}' https://<hostname>/health` on the same network is the cleaner test). Then either fix the host's NAT reflection, or pause the three probe rules in Grafana until it is fixed, and say so in the alert channel. Do not change the probe to a container address: that would skip DNS, TLS and the proxy, which are what it is for.

## 6. Keys

| Key | Where | Rotation |
|---|---|---|
| Pay-to address | Receives USDC. No key on the server | Change `X402_PAY_TO_ADDRESS` and redeploy. Sweep the old address |
| Facilitator credentials | GitHub secrets → `/opt/swarm_connect*.env` | Create new credentials, update the secrets, redeploy, then revoke the old ones |
| `NOTARY_PRIVATE_KEY` | GitHub secret → env file | `scripts/generate_notary_key.py`, update the secret, redeploy. Publish the new notary address |
| `GNOSIS_PRIVATE_KEY` (for-owner) | GitHub secret → env file | New key, move funds, update the secret, redeploy |
| Bee wallet (node identity) | Keystore in `keys/` of the `bee-data` / `bee-dev-data` volume, encrypted with `BEE_PASSWORD` / `BEE_DEV_PASSWORD` from `/opt/swarm_connect_host.env`. Backed up by `scripts/backup_bee_keys.sh` (§4) | See *Bee node identity: custody* below |

Keep hot wallets (the Bee node, the Gnosis signer) funded for days, not months, and let the balance alerts prompt top-ups.

### Bee node identity: custody

To restore a node you need three things: the encrypted archive, the age identity that decrypts it, and the keystore password. Hold them separately, so that no single place or person has all three:

| Item | Where | Who | Fill in privately |
|---|---|---|---|
| Encrypted keystore archives | `<bee-keys-path>` on `<backup-host>`, separate from the gateway-state path | Ops | `<backup-host>`, `<bee-keys-path>` |
| age identity (the decryption key for the archives) | Offline, off every gateway host. Two copies, for example a hardware-backed password manager entry and a sealed paper copy | `<identity-holder-1>`, `<identity-holder-2>` | names and storage locations |
| age recipient (public) | `BEE_KEYS_AGE_RECIPIENT` in `/etc/swarm_connect/bee_keys.env` on each host | anyone | `<bee-keys-age-recipient>` |
| Keystore passwords | Secret manager, one entry per node and environment: `<secret-manager>/swarm_connect/<environment>/<node>/bee-keystore-password`, for example `…/production/bee/…` for `BEE_PASSWORD` and `…/staging/bee-dev/…` for `BEE_DEV_PASSWORD`. Also in `/opt/swarm_connect_host.env` on the host itself | `<password-custodians>` | secret-manager location and access group |
| Address, overlay, chequebook address per node | Private ops record | Ops | |

Use a dedicated age key for keystores (`age-keygen`). It must be different from any key that encrypts gateway-state archives (#493). Never put the identity (`AGE-SECRET-KEY-…`) on a gateway host. The backup script refuses it as a recipient. Store the password and the archive under different prefixes, in different systems, under different access groups.

**If the age identity is lost** (both copies): the existing archives can no longer be decrypted.
1. While the nodes still run, generate a new identity and update `BEE_KEYS_AGE_RECIPIENT` on every host.
2. Run `/usr/local/sbin/swarm_connect_backup_bee_keys` by hand; it reads the same settings file as cron. A new recipient counts as a change, so it uploads fresh archives.
3. Run `/usr/local/sbin/swarm_connect_backup_bee_keys --verify`, then delete the old archives.

If a host was lost at the same time, its node identity is gone: its wallet funds cannot be recovered.

**If a keystore password is lost:** the running node keeps working, because it decrypted the key at start. It will not start again. Do not restart it.
- Bee 2.8.1 can send wallet funds only to addresses in `--withdrawal-addresses-whitelist` (`POST /wallet/withdraw/{coin}`, `pkg/api/router.go:572`, `cmd/bee/cmd/cmd.go:331`). Setting that flag needs a restart, so set a whitelist of team-controlled addresses **in advance**.
- Without a whitelist, the wallet's funds cannot be sent out of the node. Use them only for what the node itself spends, such as stamps, stand up a new identity, and treat the rest as lost.
- The archive alone does not help. The key in it is encrypted with the lost password.

**If an archive or the backup host is lost:** if the destination changes, the next run uploads to it on its own. If the replacement keeps the same `BEE_KEYS_REMOTE`, nothing looks changed to the script, so remove the change records (`rm /var/backups/swarm_connect_bee_keys/*.manifest`) and run `/usr/local/sbin/swarm_connect_backup_bee_keys` by hand. Then run it with `--verify`.

**Rotation.**
- *age key:* yearly, and whenever a holder leaves. Generate a new identity, switch the recipient on the hosts (the next run uploads fresh archives), and decrypt one new archive as a test. Only then destroy the old identity and the archives encrypted to it.
- *Keystore password:* this runbook does not cover changing it. Whether Bee 2.8.1 offers a supported way to re-encrypt a keystore is not verified here. If a password is exposed, treat the wallet as exposed: move funds to a newly provisioned node and retire the old identity.
- *Wallet:* if the key itself may be compromised, provision a new node identity, move funds, back the new keys up (the hourly run picks up the change), and update the private record.
