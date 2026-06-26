# Buy postage batch for an external owner (Flow B)

`POST /api/v1/stamps/for-owner` creates a Swarm postage batch **owned by an arbitrary
address** and returns its `batchID`. The owner can then sign its own stamps off-node
(the gateway never holds the owner's key). This is the inverse of a normal stamp
purchase, where the Bee node is always the owner.

Bee's HTTP API cannot do this — it always makes the node the owner. So the gateway
signs `approve` + `PostageStamp.createBatch(_owner=…)` directly on Gnosis with its own
funded **signer wallet** (`GNOSIS_PRIVATE_KEY`). That means **the gateway spends real
xBZZ + xDAI on every call**, which is why the feature is off by default and tightly
guarded.

> Self-custody flow A (the agent owns the stamp from the start) is preferred where the
> client can pre-fund. Flow B exists for clients who cannot transact on Gnosis themselves
> but still need to own a stamp — the gateway fronts the on-chain work for a fee.

## Request

```http
POST /api/v1/stamps/for-owner
Content-Type: application/json

{
  "owner": "0x571dEAC541E65312Bdb027E1C570e2751f8A6795",
  "size": "small",            // small=depth17 | medium=20 | large=22 (or pass "depth")
  "duration_hours": 24,        // TTL, min 24
  "immutable": false
}
```

Response `201`:

```json
{
  "batchID": "abab…",          // 64-hex, no 0x — ready for Swarm-Postage-Batch-Id
  "owner": "0x571d…6795",
  "depth": 17,
  "duration_hours": 24,
  "txHash": "0x…",
  "propagationStatus": "propagating",
  "secondsSincePurchase": 0,
  "estimatedReadyAt": "…"
}
```

The batch is created on-chain immediately but needs ~1–2 min to propagate before stamps
are usable — poll the propagation fields or `GET /api/v1/stamps/{batchID}`.

## Guards (all enforced BEFORE any on-chain spend)

| Guard | Env var | Default | Failure |
|-------|---------|---------|---------|
| Master toggle | `STAMP_PURCHASE_FOR_OTHERS_ENABLED` | `false` | `404` |
| Owner allow-list | `STAMP_FOR_OTHERS_REQUIRE_WHITELIST` + `STAMP_FOR_OTHERS_OWNER_WHITELIST` | required, empty | `403 OWNER_NOT_ALLOWLISTED` |
| Max depth | `STAMP_FOR_OTHERS_MAX_DEPTH` | `22` | `400 DEPTH_TOO_HIGH` |
| Max cost | `STAMP_FOR_OTHERS_MAX_BZZ` | `1.0` | `400 COST_TOO_HIGH` |
| Max TTL | `STAMP_FOR_OTHERS_MAX_DURATION_HOURS` | `168` | `400 DURATION_TOO_LONG` |
| Signer can fund it | preflight (`GNOSIS_XDAI_CRITICAL_THRESHOLD`) | `0.005` xDAI | `503 SIGNER_INSUFFICIENT_FUNDS` |

The cost cap is computed from the live chain price, so a depth/duration that prices above
`STAMP_FOR_OTHERS_MAX_BZZ` is rejected before the wallet is touched.

## Payment (x402)

When `X402_ENABLED=true` the endpoint is behind the `/api/v1/stamps/` payment gate: the
**caller pays the gateway** (in the configured x402 asset) for the service, priced from
the actual requested depth + duration. Free-tier creation is **off by default**
(`STAMP_FOR_OTHERS_FREE_TIER_ENABLED=false`) because real BZZ is spent — a free-tier
request returns `402 FREE_TIER_DISABLED`. Note this is independent of who *owns* the
batch: the payer (x402) and the owner (`body.owner`) are distinct.

When `X402_ENABLED=false` the endpoint works standalone (still subject to all the guards).

## Signer wallet — key custody & funding

`GNOSIS_PRIVATE_KEY` controls a hot wallet that holds **real xBZZ (to buy batches) and
xDAI (for gas)**. Treat it like the notary key:

- **Set it as a deployment secret** (`secrets.GNOSIS_PRIVATE_KEY`), never a plain var,
  never commit it, never log it. The chain client's `__repr__` is key-safe and the value
  is marked sensitive in config.
- **Keep it thin.** Fund only what a batch-buying budget needs; top up rather than
  parking a large balance on a hot key. `STAMP_FOR_OTHERS_MAX_BZZ` caps the blast radius
  per call; the allow-list caps who can trigger spend.
- **Rotate** by funding a new wallet and swapping the secret; the old batches stay valid
  (they're owned by the clients, not the signer).
- **Use a dedicated wallet** — do not reuse the notary or x402 pay-to wallet.

### Funding checklist
1. Fund the signer address with xDAI (gas) and xBZZ (batch cost) on Gnosis.
2. Confirm balances via the metrics gauges below (or `/health`).
3. Set `STAMP_FOR_OTHERS_OWNER_WHITELIST` to the owner addresses you permit.
4. Flip `STAMP_PURCHASE_FOR_OTHERS_ENABLED=true`.

### Preflight (#231)
Before each createBatch the gateway reads the signer balances and refuses (`503`) if
xDAI is below `GNOSIS_XDAI_CRITICAL_THRESHOLD` (no gas) or xBZZ can't cover the batch —
so it never burns gas on a call that would revert. Warn thresholds
(`GNOSIS_XDAI_WARN_THRESHOLD`, `GNOSIS_XBZZ_WARN_THRESHOLD`) surface in logs.

## Metrics

| Metric | Meaning |
|--------|---------|
| `gateway_for_owner_batches_total{status}` | attempts by outcome (`success`, `error`, `insufficient_funds`, `payment_required`) |
| `gateway_for_owner_bzz_spent_total` | cumulative PLUR spent creating batches |
| `gateway_gnosis_signer_xbzz_balance` | signer wallet xBZZ (BZZ) — alert when low |
| `gateway_gnosis_signer_xdai_balance` | signer wallet xDAI — alert when low |

`gateway_info` exposes `stamp_purchase_for_others_enabled`. Recommended alerts: signer
xDAI below the critical threshold, and `for_owner_batches_total{status="insufficient_funds"}`
increasing (means clients are being turned away — top up).
