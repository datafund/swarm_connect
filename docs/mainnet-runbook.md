# Mainnet runbook

Operating the gateway once x402 takes real USDC on Base mainnet. It covers the cutover checklist, how to stop traffic fast, refunds, backups and restore, alerts, and keys.

The Bee side (stamps, pool, chequebook) already runs on Gnosis mainnet and spends real xBZZ/xDAI on staging and production today. This runbook is about the **incoming** side becoming real money too.

## 1. Cutover checklist

Do these in order. Rehearse on staging first, with the same settings and a tiny price.

| # | Item | How |
|---|---|---|
| 1 | **Pay-to address** | A new mainnet address you control. Ideally cold or multisig, and not shared with staging. It needs no ETH: the facilitator pays settlement gas. Set `X402_PAY_TO_ADDRESS`. |
| 2 | **Network** | `X402_NETWORK=base`. The gateway refuses to start with an unknown network name. |
| 3 | **Facilitator** | x402.org serves test networks only. Configure a mainnet facilitator: CDP credentials (`X402_FACILITATOR_CDP_API_KEY_ID` / `_SECRET` as GitHub **secrets**, with `cdp-sdk` in the image), or `X402_FACILITATOR_URL` plus `X402_FACILITATOR_BEARER_TOKEN`. First confirm the facilitator accepts the gateway's x402 protocol version (see the x402 version decision). |
| 4 | **Pricing** | Set `X402_BZZ_USD_RATE` to a reviewed value, and check `X402_MARKUP_PERCENT` and `X402_MIN_PRICE_USD`. Set `X402_BZZ_PRICE_FEED_URL` so drift is alerted. |
| 5 | **Spend ceilings** | Set `GATEWAY_DAILY_BZZ_CEILING` and `GATEWAY_DAILY_BZZ_FREE_CEILING` to what the Bee wallet can afford per day. |
| 6 | **Free tier** | Decide whether it stays on (`X402_FREE_TIER_ENABLED`) and at what rate (`X402_FREE_TIER_RATE_LIMIT`). It is bounded by the free ceiling. |
| 7 | **Backups** | Install the backup cron (section 4) and test a restore once. |
| 8 | **Alerts** | Apply the alert rules (`scripts/apply_grafana.py`) and check they reach someone (section 5). |
| 9 | **Terms** | Terms and refund policy published and linked (see `TERMS.md`). |
| 10 | **Smoke test** | One real payment of a few cents, end to end: 402 → pay → 2xx, a `payment_settled` and a `payment_delivered` audit line with the same transaction, and the USDC visible at the pay-to address. |

## 2. Stopping traffic fast (kill switch)

Pick the smallest switch that stops the problem. **Do not** use `X402_ENABLED=false` as a kill switch: it turns payment *off* (everything becomes free) and removes stamp ownership enforcement.

| To stop | Do | Takes effect |
|---|---|---|
| **Everything on one host, now** | In the Caddyfile, add `respond /api/v1/* 503` to the site block, then `caddy reload`. Reads of `/health` still work. | Seconds |
| **Free-tier writes** | `X402_FREE_TIER_ENABLED=false`, then redeploy (or edit `/opt/swarm_connect.env` and `docker compose up -d --force-recreate provenance_gateway`). | ~1 min |
| **All gateway BZZ spending** | `GATEWAY_DAILY_BZZ_CEILING=0`, then redeploy or recreate. Every purchase, extension and pool buy is refused with 503. | ~1 min |
| **The pool** | `STAMP_POOL_ENABLED=false`, then redeploy or recreate. | ~1 min |
| **One abusive IP** | Block it in Caddy (`@blocked remote_ip <ip>` then `respond @blocked 403`), then `caddy reload`. | Seconds |

## 3. Refunds and reconciliation

The audit log is `data/x402_audit.jsonl` on the persistent volume (`/opt/swarm_connect_data/` on the host). Loki also receives the gateway's log lines.

| Situation | What the log shows | Action |
|---|---|---|
| Paid and delivered | `payment_settled` (success, `transaction_hash`), then `payment_delivered` (same transaction, `resource`: batchID, reference or credited bytes) | None |
| Settlement refused | `payment_settled` with `success: false`. Nothing was delivered and nothing was collected | None; the client was told to pay again |
| Settlement error (outcome unknown) | `payment_failed` with `stage: settle`. Nothing delivered | Check the payer's USDC transfers to the pay-to address around that time. If a transfer exists, refund it |
| **Paid, not delivered** | `payment_failed` with `stage: delivery_after_settlement` and `tx=` in the reason. The client received `x402_status: settled_not_delivered` and the transaction | **Refund** the transaction amount to the payer, or deliver manually |

Useful queries:

```sh
# Every payment that needs a refund
grep '"delivery_after_settlement"' /opt/swarm_connect_data/x402_audit.jsonl
# What a transaction paid for
grep '<tx hash>' /opt/swarm_connect_data/x402_audit.jsonl
```

Refunds are manual USDC transfers from the pay-to wallet to the payer address in the record. Note the refund transaction next to the record in your ops log.

## 4. Backups and restore

`scripts/backup_state.sh` archives `/opt/swarm_connect_data` and `/opt/swarm_connect_dev_data`. They hold the ownership registry, prepaid bandwidth credit, pool state, allowance and spend counters, and the audit log. Install it nightly, with an **off-host** copy:

```sh
# as root on each gateway host
cat > /etc/cron.d/swarm_connect_backup <<'CRON'
15 3 * * * root BACKUP_REMOTE=backup@backuphost:/backups/swarm_connect /opt/swarm_connect/scripts/backup_state.sh >> /var/log/swarm_connect_backup.log 2>&1
CRON
```

To restore, for example after a lost disk or an unreadable state file:

1. Stop the gateway: `docker compose stop provenance_gateway`.
2. Move the current directory aside: `mv /opt/swarm_connect_data /opt/swarm_connect_data.broken`.
3. Unpack the latest archive: `tar -xzf swarm_connect_data-<stamp>.tar.gz -C /opt`.
4. Start the gateway and check `/health`, plus an upload to a known owned stamp.

If a money-bearing state file (ownership, credit) is unreadable, the gateway **refuses to start** and keeps a `.corrupt-<timestamp>` copy next to the file. The container then restart-loops until the file is restored or deliberately moved away. That's intended: starting empty would lock owners out and wipe prepaid balances.

## 5. Alerts that matter for money

| Alert | Meaning | First action |
|---|---|---|
| **x402 settlement problems** | `gateway_x402_settlements_total{result!="settled"}` increased: refused, error or settled_not_delivered | Read the audit log (section 3). For `settled_not_delivered`, refund |
| **BZZ pricing rate off market** | `X402_BZZ_USD_RATE` is more than 2× away from the market | Review and set the rate |
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
