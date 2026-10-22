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
| 3 | **Facilitator** | x402.org serves test networks only. Configure a mainnet facilitator: CDP credentials (`X402_FACILITATOR_CDP_API_KEY_ID` / `_SECRET` as GitHub **secrets**, with `cdp-sdk` in the image), or `X402_FACILITATOR_URL` plus `X402_FACILITATOR_BEARER_TOKEN`. First confirm the facilitator accepts the gateway's x402 protocol version (see the x402 version decision). `cdp-sdk` is **not** in `requirements.lock` today, so the CDP route is refused at startup (`cdp-sdk package is not installed`) until it is added. |
| 4 | **Pricing** | Set `X402_BZZ_USD_RATE` to a reviewed value, and check `X402_MARKUP_PERCENT` and `X402_MIN_PRICE_USD`. Set `X402_BZZ_PRICE_FEED_URL` so drift is alerted *(requires #410)*. |
| 5 | **Spend ceilings** | Set `GATEWAY_DAILY_BZZ_CEILING` and `GATEWAY_DAILY_BZZ_FREE_CEILING` to what the Bee wallet can afford per day *(requires #408)*. |
| 6 | **Free tier** | Decide whether it stays on (`X402_FREE_TIER_ENABLED`) and at what rate (`X402_FREE_TIER_RATE_LIMIT`). It is bounded by the free ceiling. |
| 7 | **Backups** | Install the backup cron (section 4) and test a restore once. |
| 8 | **Alerts** | Apply the alert rules (`scripts/apply_grafana.py`) and check they reach someone (section 5). |
| 9 | **Terms** | Terms and refund policy published and linked *(requires the terms PR for #383)*. |
| 10 | **Smoke test** | One real payment of a few cents, end to end: 402 → pay → 2xx, a `payment_settled` and a `payment_delivered` audit line with the same transaction, and the USDC visible at the pay-to address. |


### Items 1–3 change in one deploy

The network, the facilitator URL, the pay-to address and the facilitator credentials must all change **together, in the same deploy**. They cannot be changed one at a time.

The reason: x402.org serves test networks only. At startup, `validate_x402_config` (in `app/x402/facilitator.py`) refuses a mainnet `X402_NETWORK` with the x402.org facilitator. So if you set only `X402_NETWORK=base` while `X402_FACILITATOR_URL` still points at x402.org, the gateway refuses to start. It also refuses a missing or malformed pay-to address, CDP credentials without `cdp-sdk` in the image, CDP credentials together with a bearer token, and a plain `http://` facilitator URL on a mainnet or with credentials.

Set every row below **before** the deploy that ships the cutover. Then deploy once. Rehearse the same change on `staging` first. All of them are rendered into the env file by `.github/workflows/deploy.yml`: `/opt/swarm_connect.env` for `production` (`main`) and `/opt/swarm_connect_dev.env` for `staging` (`dev`).

| GitHub setting | Kind | Environment | At cutover (production) | If unset |
|---|---|---|---|---|
| `X402_ENABLED` | variable (`vars.*`) | production | `true` (already set) | `false` in both environments |
| `X402_NETWORK` | variable (`vars.*`) | production | `base` | production: **empty**, and the gateway refuses to start (#370); staging: `base-sepolia` |
| `X402_FACILITATOR_URL` | variable (`vars.*`) | production | the mainnet facilitator's `https://` URL, for example `https://<mainnet-facilitator>/…`. With CDP credentials, leave it empty or use a URL on the CDP host. | production: **empty**, and the gateway refuses to start unless CDP credentials are set; staging: `https://x402.org/facilitator` |
| `X402_PAY_TO_ADDRESS` | variable (`vars.*`) | production | `<mainnet pay-to address>`: the new address from item 1, not the staging one | empty, and the gateway refuses to start |
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

- **With the check (current `deploy.yml`):** the `deploy` run fails in its `deploy` step, with an `x402 configuration refused` error annotation. The step log shows `x402 configuration check failed: RuntimeError: Refusing to start with x402 enabled: …` and the reason. The previous container is **not** stopped or recreated, and it keeps serving with its old configuration.
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
| Settlement refused | `payment_settled` with `success: false`. Nothing was delivered and nothing was collected | None; the client was told to pay again |
| Settlement error (outcome unknown) | `payment_failed` with `stage: settle`, counted as `result="error"`. Nothing delivered | **Check on-chain**: look for the payer's USDC transfer to the pay-to address around that time. If one exists, refund it |
| Purchase in flight at shutdown | `payment_failed` `delivery_after_settlement` with `Bee purchase in flight at shutdown, a batch may exist on-chain unlabelled` and the label | Look for a `recovered` batch with that depth and amount created after the `purchase_pending` start block; then as below: assign **or** refund |
| Paid stamp purchase, Bee response lost | `purchase_pending` (tx, label, depth, amount, start block, payer); the client got `202`. Later `payment_delivered` with `late: true` and the batchID (found and registered), or `payment_failed` `delivery_after_settlement` with `stamp purchase after settlement: not found`/`refused by Bee`/`already registered`/`lookup interrupted` | Found: none. Otherwise look in Bee `/stamps` for the label from `purchase_pending`. A batch Bee labelled `recovered` with that depth and amount, created after the start block, may be it: the gateway never assigns those itself, because a concurrent purchase could produce one too. Do **exactly one** of: register it to the payer (if you can tell it is theirs), or **refund**. Record which, next to the transaction in the ops log. After registering, tell the payer the batchID directly: a retry with their Idempotency-Key keeps answering `DELIVERY_FAILED_AFTER_PAYMENT` until the entry expires (24 h). To change that answer, stop the gateway, set the entry in `x402_idempotency.json` whose `transaction` matches to `"status": 201` and a base64 `{"batchID": …}` body, and start it again |
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

**Stopping the gateway.** Paid stamp purchases can be waiting on Bee for minutes. On shutdown uvicorn stops in-flight requests after 10 s (`--timeout-graceful-shutdown 10`), then the gateway waits up to `SHUTDOWN_PENDING_PURCHASE_GRACE_SECONDS` (25) for pending purchases before cutting them off with a refund record. Keep the stop grace period above the sum: `stop_grace_period: 40s` in docker-compose, or `TimeoutStopSec=40` under systemd. Prefer deploying when `purchase_pending` has no recent unmatched entries.

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
