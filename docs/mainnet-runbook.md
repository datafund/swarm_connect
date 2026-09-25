# Mainnet runbook

Operating the gateway once x402 takes real USDC on Base mainnet. It covers the cutover checklist, how to stop traffic fast, refunds, backups and restore, alerts, and keys.

Some items depend on changes delivered in separate pull requests. They are marked *(requires #N)*; until that PR is merged and deployed, the item does not apply.

The Bee side (stamps, pool, chequebook) already runs on Gnosis mainnet and spends real xBZZ/xDAI on staging and production today. This runbook is about the **incoming** side becoming real money too.

## 1. Cutover checklist

Do these in order. Rehearse on staging first, with the same settings and a tiny price.

| # | Item | How |
|---|---|---|
| 1 | **Pay-to address** | A new mainnet address you control. Ideally cold or multisig, and not shared with staging. It needs no ETH: the facilitator pays settlement gas. Set `X402_PAY_TO_ADDRESS`. |
| 2 | **Network** | `X402_NETWORK=base`. The gateway refuses to start with an unknown network name. |
| 3 | **Facilitator** | x402.org serves test networks only. Configure a mainnet facilitator: CDP credentials (`X402_FACILITATOR_CDP_API_KEY_ID` / `_SECRET` as GitHub **secrets**, with `cdp-sdk` in the image), or `X402_FACILITATOR_URL` plus `X402_FACILITATOR_BEARER_TOKEN`. First confirm the facilitator accepts the gateway's x402 protocol version (see the x402 version decision). |
| 4 | **Pricing** | Set `X402_BZZ_USD_RATE` to a reviewed value, and check `X402_MARKUP_PERCENT` and `X402_MIN_PRICE_USD`. Set `X402_BZZ_PRICE_FEED_URL` so drift is alerted *(requires #410)*. |
| 5 | **Spend ceilings** | Set `GATEWAY_DAILY_BZZ_CEILING` and `GATEWAY_DAILY_BZZ_FREE_CEILING` to what the Bee wallet can afford per day *(requires #408)*. |
| 6 | **Free tier** | Decide whether it stays on (`X402_FREE_TIER_ENABLED`) and at what rate (`X402_FREE_TIER_RATE_LIMIT`). It is bounded by the free ceiling. |
| 7 | **Backups** | Install the backup cron (section 4) and test a restore once. |
| 8 | **Alerts** | Apply the alert rules (`scripts/apply_grafana.py`) and check they reach someone (section 5). |
| 9 | **Terms** | Terms and refund policy published and linked *(requires the terms PR for #383)*. |
| 10 | **Smoke test** | One real payment of a few cents, end to end: 402 → pay → 2xx, a `payment_settled` and a `payment_delivered` audit line with the same transaction, and the USDC visible at the pay-to address. |

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
| Settlement refused | `payment_settled` with `success: false`. Nothing was delivered and nothing was collected | None; the client was told to pay again |
| Settlement error (outcome unknown) | `payment_failed` with `stage: settle`, counted as `result="error"`. Nothing delivered | **Check on-chain**: look for the payer's USDC transfer to the pay-to address around that time. If one exists, refund it |
| Paid stamp purchase, Bee response lost | The client got `202 PURCHASE_PENDING`. Later `payment_delivered` with `late: true` and the batchID (found and registered), or `payment_failed` `delivery_after_settlement` with `stamp purchase not found after settlement; label=…` | Found: none. Not found: check Bee `/stamps` for the label (or a `recovered` batch), register it with the payer or **refund**. A restart within 15 min of the 202 stops the background search: check the same way |
| **Paid, not delivered** | `payment_failed` with `stage: delivery_after_settlement` and `tx=` in the reason. The client received `x402_status: settled_not_delivered` and the transaction | **Refund** the transaction amount to the payer, or deliver manually |
| Idempotent retry answered | `payment_idempotent_replay` (the original `transaction_hash`), with no `payment_settled` of its own. The retry's payment was verified, never settled | None |
| Retry after an unanswered settlement | The client got `409 IDEMPOTENCY_KEY_SETTLEMENT_UNKNOWN` with the first `nonce`; the first request logged `payment_failed` `stage: settle` (or nothing, after a restart) | Same as "settlement error": check on-chain whether that nonce was used; if so, deliver or refund |
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

## 4. Backups and restore

`scripts/backup_state.sh` archives `/opt/swarm_connect_data` and `/opt/swarm_connect_dev_data`. They hold the ownership registry, prepaid bandwidth credit, pool state, allowance and spend counters, stored Idempotency-Key results, and the audit log. If `x402_idempotency.json` is unreadable, the gateway keeps it, saves a `.corrupt-<ts>` copy, logs an ERROR and refuses keyed paid requests (`503 IDEMPOTENCY_UNAVAILABLE`); restore it from backup, or move it away to start empty (retries of the last 24 h are then charged again). Either way, **restart the gateway** afterwards: the file is read only at startup, and until then the 503 persists.

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

If a money-bearing state file (ownership, credit) is unreadable, the gateway **refuses to start** and keeps a `.corrupt-<timestamp>` copy next to the file *(requires #395)*. The container then restart-loops until the file is restored or deliberately moved away. That's intended: starting empty would lock owners out and wipe prepaid balances.

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

## 5. Alerts that matter for money

| Alert | Meaning | First action |
|---|---|---|
| **x402 settlement problems** | `gateway_x402_settlements_total{result!="settled"}` increased: refused, error or settled_not_delivered | Read the audit log (section 3). For `settled_not_delivered`, refund |
| **BZZ pricing rate off market** *(requires #410)* | `X402_BZZ_USD_RATE` is more than 2× away from the market | Review and set the rate |
| Low BZZ / xDAI / chequebook (existing) | The Bee node can't buy or upload | Fund the node wallet on Gnosis |
| Gateway spend near its ceiling (once the spend-ceiling change is deployed) | `gateway_spend_bzz_today` approaching `gateway_spend_ceiling_bzz` | Check for abuse; raise the ceiling only deliberately |

## 6. Keys

| Key | Where | Rotation |
|---|---|---|
| Pay-to address | Receives USDC. No key on the server | Change `X402_PAY_TO_ADDRESS` and redeploy. Sweep the old address |
| Facilitator credentials | GitHub secrets → `/opt/swarm_connect*.env` | Create new credentials, update the secrets, redeploy, then revoke the old ones |
| `NOTARY_PRIVATE_KEY` | GitHub secret → env file | `scripts/generate_notary_key.py`, update the secret, redeploy. Publish the new notary address |
| `GNOSIS_PRIVATE_KEY` (for-owner) | GitHub secret → env file | New key, move funds, update the secret, redeploy |
| Bee wallet | Bee keystore in the `bee-data` volume | Bee-level procedure; out of scope here |

Keep hot wallets (the Bee node, the Gnosis signer) funded for days, not months, and let the balance alerts prompt top-ups.
