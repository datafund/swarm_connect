# Stamp Pool Guide

This document describes the Stamp Pool feature for low-latency stamp provisioning.

## Overview

The Stamp Pool feature maintains a reserve of pre-purchased postage stamps that can be released to clients immediately, without waiting for blockchain confirmation time (which typically takes >1 minute).

### Problem

Purchasing a postage stamp on Swarm requires a blockchain transaction on Gnosis Chain. Due to:
- Blockchain confirmation time (~1 minute)
- Swarm network propagation (30-90 seconds until usable)

The total latency from request to usable stamp is **>1 minute**, which is unacceptable for real-time applications.

### Solution

The gateway maintains a **reserve pool** of pre-purchased stamps:

```
[Gateway Stamp Pool]
├── Reserve stamps (pre-purchased, available)
│   ├── Depth 17 (small): [stamp1, stamp2]
│   ├── Depth 20 (medium): [stamp3]
│   └── Depth 22 (large): [stamp4]
├── Released stamps (no longer tracked by pool)
└── Pool Manager (background service)
    ├── Monitors reserve levels
    ├── Tops up expiring stamps
    └── Purchases new stamps when reserve low
```

When a client requests a stamp from the pool:
1. Gateway checks pool for matching depth
2. If available: **immediately returns stampId** (<5 seconds)
3. Stamp removed from pool (released to client)
4. Background task replenishes reserve asynchronously

## Configuration

### Environment Variables

```env
# Master switch
STAMP_POOL_ENABLED=false  # Set to 'true' to enable

# Reserve levels by size
STAMP_POOL_RESERVE_SMALL=1   # Depth 17 stamps to keep
STAMP_POOL_RESERVE_MEDIUM=1  # Depth 20 stamps to keep
STAMP_POOL_RESERVE_LARGE=0   # Depth 22 stamps to keep

# Monitoring settings
STAMP_POOL_CHECK_INTERVAL_SECONDS=900  # Check every 15 minutes
STAMP_POOL_MIN_TTL_HOURS=24            # Top up if TTL below 24 hours
STAMP_POOL_TOPUP_HOURS=168             # Add 1 week when topping up
STAMP_POOL_LOW_RESERVE_THRESHOLD=1     # Alert when reserve at this level

# Duration for new pool stamps
STAMP_POOL_DEFAULT_DURATION_HOURS=168  # 1 week
```

### Size Presets

| Size | Depth | Use Case |
|------|-------|----------|
| small | 17 | One small document |
| medium | 20 | Several medium documents |
| large | 22 | Several large documents |

## API Endpoints

All endpoints are under `/api/v1/pool/`.

### GET /api/v1/pool/status

Get current pool status.

**Response:**
```json
{
  "enabled": true,
  "reserve_config": {"17": 1, "20": 1},
  "current_levels": {"17": 1, "20": 0},
  "available_stamps": {"17": ["abc123..."]},
  "total_stamps": 1,
  "low_reserve_warning": true,
  "last_check": "2026-01-21T10:00:00Z",
  "next_check": "2026-01-21T10:15:00Z",
  "errors": []
}
```

### POST /api/v1/pool/acquire

Acquire a stamp from the pool for immediate use.

**Request:**
```json
{
  "size": "small"
}
```
Or with explicit depth:
```json
{
  "depth": 17
}
```

**Response (success):**
```json
{
  "success": true,
  "batch_id": "abc123def456...",
  "depth": 17,
  "size_name": "small",
  "message": "Stamp acquired from pool",
  "fallback_used": false
}
```

**Response (pool exhausted):**
```json
{
  "success": false,
  "batch_id": null,
  "depth": null,
  "size_name": null,
  "message": "No stamp available for depth 17 (size: small). Pool may be exhausted.",
  "fallback_used": false
}
```

**Response (fallback to larger):**
```json
{
  "success": true,
  "batch_id": "xyz789...",
  "depth": 20,
  "size_name": "medium",
  "message": "Requested size not available. Stamp acquired from pool (depth=20, size=medium) (larger than requested)",
  "fallback_used": true
}
```

### GET /api/v1/pool/available

List all available stamps in the pool.

**Response:**
```json
[
  {
    "batch_id": "abc123...",
    "depth": 17,
    "size_name": "small",
    "created_at": "2026-01-21T09:00:00Z",
    "ttl_at_creation": 604800
  }
]
```

### POST /api/v1/pool/check

Manually trigger pool maintenance (sync, purchase, top-up).

**Response:**
```json
{
  "checked_at": "2026-01-21T10:05:00Z",
  "stamps_purchased": 1,
  "stamps_topped_up": 0,
  "stamps_synced": 2,
  "errors": []
}
```

## How It Works

### Startup

When the gateway starts with `STAMP_POOL_ENABLED=true`:

1. Background task starts
2. Syncs existing stamps from Bee node to pool
3. Checks reserve levels
4. Purchases stamps if below target
5. Schedules periodic checks

### Background Task

The background task runs every `STAMP_POOL_CHECK_INTERVAL_SECONDS`:

1. **Sync**: Find existing stamps on Bee node that match pool criteria
2. **Update TTLs**: Remove expired/unusable stamps from pool
3. **Purchase**: Buy new stamps if below reserve levels
4. **Top-up**: Extend stamps with TTL below `STAMP_POOL_MIN_TTL_HOURS`

### Stamp Release

When a client acquires a stamp:

1. Stamp is immediately returned
2. Stamp is removed from pool tracking
3. Client is now responsible for the stamp
4. Background task will eventually replenish the reserve

## Post-Release Handling

Once a stamp is released from the pool:

- Gateway stops tracking it
- Recipient is responsible for:
  - Managing utilization
  - Extending TTL if needed
  - Monitoring capacity

## Monitoring

### Low Reserve Warning

When `low_reserve_warning: true` in status response:
- At least one depth has fewer stamps than configured
- Consider manual intervention or adjusting reserve levels

### Errors

Check the `errors` array in status response for:
- Failed stamp purchases (insufficient BZZ balance)
- Failed top-ups
- Bee node connectivity issues

### Health Check Integration

The pool status is included in `/health` when enabled:

```json
{
  "status": "ok",
  "stamp_pool": {
    "enabled": true,
    "total_stamps": 2,
    "low_reserve_warning": false
  }
}
```

## Business Considerations

### Who Pays?

The gateway operator bears the upfront cost of:
- Purchasing reserve stamps
- Maintaining stamp TTLs

This cost should be factored into service pricing (e.g., via x402 payments or subscription fees).

### Risks

1. **Unused stamps expire**: TTL costs are ongoing even if stamps aren't used
2. **Pool exhaustion**: High demand can drain the pool faster than replenishment
3. **BZZ volatility**: Stamp costs vary with network conditions

### Mitigation

- Set appropriate reserve levels based on expected demand
- Monitor pool status and adjust configuration
- Keep sufficient BZZ balance for replenishment
- Consider tiered access (premium customers get pool access)

## Troubleshooting

### Pool Not Replenishing

1. Check BZZ wallet balance on Bee node
2. Check Bee node connectivity
3. Check `errors` in pool status
4. Manually trigger `/api/v1/pool/check`

### Stamps Not Becoming Usable

New stamps take 30-90 seconds to propagate on the Swarm network. The pool manager waits for this automatically.

### High Latency on Acquire

The acquire endpoint should return in <5 seconds. If slow:
- Check Bee node responsiveness
- Pool may be empty (check status)

## Related Documentation

- [Swarm Postage Stamps](https://docs.ethswarm.org/docs/concepts/stamps)
- [Gateway Operator Guide](./x402-operator-guide.md)
- [Issue #63](https://github.com/datafund/swarm_connect/issues/63)
