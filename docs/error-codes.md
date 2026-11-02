# Error codes

Error responses from the gateway carry `code` and `message` at the top level
of the JSON body. Branch on `code`, and show `message` to a person.
Two kinds of crash have no `code`. A crash here means the server raised an
unexpected error, not a handled one. Handled errors, such as a 502 from the Bee
node, keep `code` and `message` even on a paid request.

- A paid request that crashes before its payment is settled returns 500
  `{"detail": "Internal server error. You were not charged."}`. Nothing was
  charged, so you can retry with the same payment authorization.
- Any other crash returns 500 with the plain text `Internal Server Error`.

```json
{
  "detail": { "code": "FILE_TOO_LARGE", "message": "Upload exceeds maximum size of 10 MB." },
  "code": "FILE_TOO_LARGE",
  "message": "Upload exceeds maximum size of 10 MB."
}
```

`detail` is unchanged from earlier releases, so existing clients that read
`detail` or `detail.code` keep working. Some errors put extra fields in `detail`
(for example `suggestion`, `stamp_status`, `resets_at`). They are listed below
where they matter.

When an endpoint does not raise its own code, `code` is `HTTP_<status>`, for
example `HTTP_404` or `HTTP_502`, and `message` is the error text.

One response keeps its own shape:

- **402 Payment Required** from the x402 payment check. Its body is the x402
  payment request (`x402Version`, `error`, `accepts`, `freeTier`) at the top
  level, as x402 v1 specifies, next to `code` (`HTTP_402`) and `message`. The
  same request is also under `detail` for older clients; that copy is
  deprecated. See
  [x402-client-integration.md](x402-client-integration.md) for how to pay it.
  To learn a price *without* triggering a 402, call `GET /api/v1/pricing`.

A failed payment settlement has its own codes, listed under
[Payments and pricing](#payments-and-pricing):

- `PAYMENT_SETTLEMENT_FAILED` (402): the payment was refused.
- `PAYMENT_SETTLEMENT_UNAVAILABLE` (502): the outcome of the payment is unknown,
  and the request did nothing.
- `PAYMENT_SETTLEMENT_UNKNOWN` (502): the outcome of the payment is unknown,
  and the request's work has already run.
- `PAYMENT_AUTHORIZATION_OUTCOME_UNKNOWN` (409): a retry of an authorization
  whose earlier settlement got no answer; that earlier transfer may have been
  collected.
- `DELIVERY_FAILED_AFTER_PAYMENT` (500): you paid and got nothing.

Only `PAYMENT_SETTLEMENT_FAILED` means you were certainly not charged. Never
pay again from a response that carries `x402_status: settlement_unknown` or
the `PAYMENT_SETTLEMENT_UNAVAILABLE` code. **Branch on `code`, not on
`x402_status`:** `PAYMENT_SETTLEMENT_UNAVAILABLE` keeps `x402_status:
settlement_failed`, the same value as a refusal, for compatibility, although
its outcome is unknown.

Do not pay again without first reading the advice in its row.
Send an `Idempotency-Key` with every paid request, so that a retry returns the
first result instead of charging again (see below).

A test (`tests/test_error_codes_doc.py`) fails if a code is raised in `app/`
without being listed here, or listed here but no longer raised.

## Request and envelope errors

| Code | Status | Meaning | What to do |
|---|---|---|---|
| `ACCESS_BLOCKED` | 403 | The operator has blocked this address. Applied before any other handling, so it is the one refusal that says nothing about the request itself. IPv6 is matched by its `/64` and IPv4-mapped IPv6 by the IPv4 address, so switching addresses within one allocation does not evade it. | Nothing the caller can change. Contact the operator if you believe it is wrong. |
| `VALIDATION_ERROR` | 422 | The request body, query or path failed validation. `detail` is the list of field errors. | Fix the fields named in `detail`. Do not retry unchanged. |
| `BODY_TOO_LARGE` | 413 | A JSON body is over the gateway's JSON size limit. | Send a smaller body. File uploads use multipart, which this limit does not apply to. |
| `JSON_TOO_DEEP` | 400 | A JSON body is nested too deeply. | Flatten the body. |
| `RATE_LIMIT_EXCEEDED` | 429 | Too many requests from this client (global limiter). `retry_after` and the `Retry-After` header say how long to wait. | Wait `retry_after` seconds, then retry. |
| `HTTP_<status>` | any | The endpoint raised no specific code. This includes the free-tier limit on stamps and uploads (`HTTP_429`, with `detail.payment_info`). | Act on the status: 4xx means fix the request, 429 means wait or pay, 502/503 means retry later. |

## Payments and pricing

| Code | Status | Meaning | What to do |
|---|---|---|---|
| `HTTP_402` | 402 | Payment required (x402), or the payment sent was rejected (invalid header, verification failed; see `message`). `accepts` lists what to pay; `freeTier` is present when the free tier is available (both also under `detail`, deprecated). | Sign a payment for `accepts[0]` and retry with `X-PAYMENT`, or retry with `X-Payment-Mode: free`. |
| `PRICING_UNAVAILABLE` | 503 | `GET /api/v1/pricing` could not read the current chain price. | Retry shortly. |
| `PAYMENT_REQUIRED` | 402 | Bandwidth credit top-up was attempted on the free tier. | Pay with `X-PAYMENT`. The free tier cannot fund credit. |
| `BILLING_DISABLED` | 400 | Bandwidth credit top-up needs x402, which is off on this gateway. | Use the free tier for chunk uploads, or another gateway. |
| `TOPUP_TOO_SMALL` | 400 | The `mb` top-up is below the minimum (named in `message`). | Raise `mb` to at least the minimum. |
| `TOPUP_TOO_LARGE` | 400 (422 from `/pricing`) | The `mb` top-up is above the per-request maximum. | Split it into several top-ups. |
| `CREDIT_REQUIRED` | 402 | A chunk upload came with no credit token and no free-tier header. | Top up with `POST /api/v1/chunks/credit`, or send `X-Payment-Mode: free`. |
| `INVALID_CREDIT_TOKEN` | 402 | The bandwidth credit token is unknown. | Top up again to obtain a valid token. |
| `TOKEN_ROTATION_FAILED` | 503 | The bandwidth credit token could not be rotated. **The current token is unchanged and still valid**, so nothing is lost. | Keep using the existing token and retry the rotation later. |
| `INSUFFICIENT_CREDIT` | 402 | The credit left is less than this chunk. | Top up, then retry the chunk. |
| `FREE_TIER_DISABLED` | 402 | The free tier is off for this operation (chunk upload, or buy-batch-for-owner). | Pay with `X-PAYMENT`, or for chunks, top up credit. |
| `FREE_QUOTA_EXCEEDED` | 429 | The free-tier chunk quota for this client is used up. | Wait for the daily quota to reset, or top up credit. |
| `PAYMENT_SETTLEMENT_UNAVAILABLE` | 502 | The payment could not be settled and **nothing was delivered**, but the facilitator errored, so it is unknown whether the transfer was submitted: **you may have been charged**. `detail.x402_status` is `settlement_failed`, the same value as a refusal, kept for compatibility: tell the two apart by `code`, not by `x402_status`. `detail.nonce` is this authorization's nonce. Headers: `X-Payment-Mode: failed`, `X-Payment-Status: settlement_unknown` (the header, unlike `x402_status`, says the outcome is unknown). | Do not pay again with a new authorization yet. Without an `Idempotency-Key`: retry with the **same** `X-PAYMENT` while it is still valid (before `validBefore`); the gateway releases it on this response. If the first transfer did not go through, the retry is settled once and delivered. If the facilitator refuses the retry (for instance because the first transfer spent the nonce), the answer is `409 PAYMENT_AUTHORIZATION_OUTCOME_UNKNOWN`, not a `402`. With an `Idempotency-Key`: that key stays taken for 24 hours and **cannot complete**; no retry with it is charged. A new authorization with the key gets `409 IDEMPOTENCY_KEY_SETTLEMENT_UNKNOWN`. The same `X-PAYMENT` with the key gets the same `409 IDEMPOTENCY_KEY_SETTLEMENT_UNKNOWN` (also if that authorization has since settled under another key), or `409 PAYMENT_AUTHORIZATION_OUTCOME_UNKNOWN` if the facilitator rejects the authorization at verify (verify runs before the key is looked up). Check the nonce as below, then pay again under a **new** key. In every case, before signing a **new** authorization (a new nonce, which can be charged a second time), check on-chain whether this nonce was used. If it was, contact the operator with the nonce for a refund. If it was not, the facilitator may still submit it later, so before signing a new payment either wait until its `validBefore` has passed or cancel it on-chain with EIP-3009 `cancelAuthorization`. Then pay again, with a new `Idempotency-Key` if you use one. |
| `PAYMENT_SETTLEMENT_FAILED` | 402 | The payment was not confirmed as settled, and **nothing was delivered**. `x402_status` is `settlement_failed`. The facilitator refused the payment. Two forms: **(1) Before the work:** `reason` gives the refusal, and `accepts` lets you sign a new payment; both are at the top level and under `detail`. **(2) After the work** (a paid route that settles only after it has run): only `code`, `message` and `x402_status`, at the top level, and the result is withheld. If the facilitator did not answer at all, the code is `PAYMENT_SETTLEMENT_UNKNOWN` instead. | Form 1: fix what `reason` names (usually funds or an expired authorization) and retry. Form 2: fix what `message` names and retry with a new payment. **Exception:** if this answers a retry of an authorization that earlier got `PAYMENT_SETTLEMENT_UNAVAILABLE` or `PAYMENT_SETTLEMENT_UNKNOWN` (the gateway answers those with `409 PAYMENT_AUTHORIZATION_OUTCOME_UNKNOWN` for 24 hours, and forgets them after that), do not pay from it: follow that row. |
| `PAYMENT_SETTLEMENT_UNKNOWN` | 502 | The request's work **has already run**, but the facilitator did not answer when the payment was settled afterwards, so it is unknown whether the transfer went through. The result is withheld. Only on a paid route that settles after its work rather than before it; every current paid route settles first and answers `PAYMENT_SETTLEMENT_UNAVAILABLE` instead. Top level: `code`, `message`, `nonce` (this request's authorization nonce) and `x402_status: settlement_unknown`. Headers: `X-Payment-Mode: failed`, `X-Payment-Status: settlement_unknown`. | Do not pay again from this response. The gateway remembers this authorization for 24 hours: a retry with the same `X-PAYMENT` (with or without the same `Idempotency-Key`) gets `409 PAYMENT_AUTHORIZATION_OUTCOME_UNKNOWN` before any work, and is not run or charged (also across a gateway restart: the gateway keeps this record on disk); a retry with the same `Idempotency-Key` and a new authorization gets `409 IDEMPOTENCY_KEY_SETTLEMENT_UNKNOWN` and is not charged. Check on-chain whether `nonce` was used. If it was, contact the operator with the `nonce`. If it was not, the facilitator may still submit it later, so before signing a new payment either wait until its `validBefore` has passed or cancel it on-chain with EIP-3009 `cancelAuthorization`. Then pay again, with a new `Idempotency-Key` if you use one. |
| `PAYMENT_AUTHORIZATION_OUTCOME_UNKNOWN` | 409 | This request carried a payment authorization whose earlier settlement got no answer (after `PAYMENT_SETTLEMENT_UNAVAILABLE` or `PAYMENT_SETTLEMENT_UNKNOWN`), and either its earlier work already ran (after `PAYMENT_SETTLEMENT_UNKNOWN`: answered before any work runs), it is in use by a concurrent request, or the facilitator now refused it, at verify or at settle. A refusal here is **probably** because the earlier transfer spent the nonce, but the gateway cannot tell for certain, so this is not a `402` and has no `accepts`. This request did nothing and was not charged; the earlier one may have been. `detail` carries `nonce`, `x402_status: settlement_unknown` and, when the facilitator refused, its `reason`. Headers: `X-Payment-Mode: failed`, `X-Payment-Status: settlement_unknown`. The gateway keeps such authorizations on disk for 24 hours, across restarts. After that, a refusal of the same authorization is an ordinary `402` again: do not pay from it automatically until you have checked the nonce. | Do not pay again from this response. Check on-chain whether `nonce` was used. If it was, contact the operator with the `nonce` for a refund. If it was not, the facilitator may still submit it later, so before signing a new payment either wait until its `validBefore` has passed or cancel it on-chain with EIP-3009 `cancelAuthorization`. Then pay again, with a new `Idempotency-Key` if you use one. |
| `DELIVERY_FAILED_AFTER_PAYMENT` | 500 | The payment **was collected** and the request then failed. The only code here that is certain money moved without a result. Also returned by `POST /pool/acquire` when the payment settled but the reserved batch could not be handed over. `transaction` carries the transfer and `x402_status` is `settled_not_delivered`. Where those fields are depends on the path. **Pool acquire:** under `detail`. **A paid request that crashed after payment:** at the top level, with no `detail`. **A stamp purchase that answered `202 PURCHASE_PENDING` and whose batch was never found**, retried with the same `Idempotency-Key`: at the top level, with no `detail`. All three set the `X-Payment-Transaction` header. The first two also set `X-Payment-Status: settled_not_delivered`; the stamp-purchase retry does not. | Do not retry blindly. Contact the operator with the transaction for a refund. |

## Idempotency-Key

Paid requests may carry an `Idempotency-Key`; see
[x402-client-integration.md](x402-client-integration.md#retries-and-idempotency-key).
None of these responses charges the new payment.

| Code | Status | Meaning | What to do |
|---|---|---|---|
| `IDEMPOTENCY_KEY_INVALID` | 400 | The key is empty, longer than 255 characters, or not printable ASCII. | Send a valid key (a UUID). |
| `IDEMPOTENCY_KEY_IN_PROGRESS` | 409 | A request with this key is still being processed. `Retry-After: 5`. | Retry with the same key. |
| `IDEMPOTENCY_KEY_REUSED` | 422 | The key was already used for a different request (method, path, body or query). | Use a new key. |
| `IDEMPOTENCY_KEY_SETTLED_PENDING` | 409 | The first request with this key was paid, but its result is not available (it failed or was interrupted afterwards). `detail.transaction` names the payment. | Do not pay again. Contact the operator with the transaction for the result or a refund. |
| `IDEMPOTENCY_KEY_SETTLEMENT_UNKNOWN` | 409 | The first request's payment was sent for settlement and no answer came back. `detail.nonce` is that authorization's nonce. Returned to every retry with that key for 24 hours, including one that presents the first request's own `X-PAYMENT`. This request was not charged. | Do not pay again from this response, and do not keep retrying with this key: it will not complete. Check on-chain whether the nonce was used. If it was, contact the operator with the nonce. If it was not, the facilitator may still submit it later, so before signing a new payment either wait until its `validBefore` has passed or cancel it on-chain with EIP-3009 `cancelAuthorization`. Then pay again with a **new** `Idempotency-Key`. |
| `IDEMPOTENCY_KEY_DELIVERED_NOT_STORED` | 409 | The first request succeeded and was paid once, but its response was too large to keep. | Look the result up through the resource itself. |
| `IDEMPOTENCY_UNAVAILABLE` | 503 | The gateway cannot read its idempotency store. | Retry later with the same key. |
| `PAYMENT_STATE_UNAVAILABLE` | 503 | The gateway cannot read its record of payments whose settlement got no answer, so it refuses every paid (`X-PAYMENT`) request until an operator repairs it. Nothing was verified, settled or run. `Retry-After` is set. | Retry later with the same `X-PAYMENT`; it was not charged. |

## Stamps

| Code | Status | Meaning | What to do |
|---|---|---|---|
| `STAMP_COST_EXCEEDS_LIMIT` | 400 | The requested batch would cost more than the gateway allows per purchase. | Ask for a smaller size or a shorter duration. |
| `PURCHASE_PENDING` | 202 | Paid stamp purchase: the payment was collected but the Bee node did not confirm the purchase in time. Not an error. The body has the payment `transaction` and the batch `label`; the batch is registered to the paying wallet as soon as the node reports it. | Do not pay again. Look for the label in `GET /api/v1/stamps/?wallet=<payer>`, or retry with the same `Idempotency-Key` for the `201` once it is found. |
| `PURCHASE_CAPACITY` | 503 | Too many paid stamp purchases are waiting on the Bee node. Checked before settlement: **nothing was charged**. `Retry-After: 30`. | Retry shortly. |
| `DAILY_SPEND_BUDGET_EXHAUSTED` | 429 | This caller's daily purchase budget is spent. | Wait until the budget resets (see `message`). |
| `SPEND_BUDGET_UNAVAILABLE` | 503 | Stamp purchase or extension: the gateway could not read today's spend record, so it refuses to spend rather than count from zero. Applies to paid requests too, since the gateway's daily ceiling bounds them as well. **Nothing was charged.** | Retry after the time in `message`, or once the operator has restored the record. |
| `GATEWAY_DAILY_SPEND_CEILING` | 503 | The gateway itself has spent its daily BZZ allowance, so it is refusing to spend more for anyone. Two variants, distinguished by `message`: the overall ceiling is reached, or only the portion open to unpaid requests is. In the second case paid requests still work where x402 is enabled, on a mainnet or on a testnet with `X402_ALLOW_TESTNET_PAID_BYPASS`, and the message then says so. `detail.resets_at` is when it lifts. Applies to stamp purchase, extension and buy-batch-for-owner. | Wait for `resets_at`. If the message says paid requests still work, pay with `X-PAYMENT` and retry. This is the operator's limit, not yours — if it recurs, tell them. |
| `DAILY_STAMP_ALLOWANCE_EXHAUSTED` | 429 or 402 | `POST /pool/acquire`: the free daily allowance for this size is used up. `detail` has `allowance`, `used`, `resets_at` and an `alternative`. Where paying bypasses the allowance (x402 enabled, on a mainnet or a testnet with `X402_ALLOW_TESTNET_PAID_BYPASS`), the status is 402 and the body is also an x402 payment request (`accepts`). | Wait for `resets_at`, or follow `detail.alternative`: pay for this same request (402), or buy a stamp directly. |
| `DAILY_STAMP_ALLOWANCE_PER_CLIENT_EXHAUSTED` | 429 or 402 | `POST /pool/acquire`: this client (IPv4 address, or IPv6 /64) has taken its free batches of this size for today within the application's allowance (`POOL_ALLOWANCE_PER_IP`). Same body and 402 rule as above. | As above. |
| `POOL_ALLOWANCE_UNAVAILABLE` | 503 | `POST /pool/acquire`: the gateway could not read today's allowance record, so free acquires are paused. Where x402 is enabled, on a mainnet or on a testnet with `X402_ALLOW_TESTNET_PAID_BYPASS`, paid acquires bypass the allowance and still work. | Buy a stamp directly with `POST /api/v1/stamps/`, or retry after the time in `message`. Where paid acquires still work (see the previous column), you can also pay for the acquire. |
| `REQUESTED_SIZE_UNAVAILABLE` | 409 | `POST /pool/acquire`: no stamp of the requested size is in the pool, and although a larger one was, today's allowance for that larger size is spent. `detail.size` is what was asked for. | Retry in a few minutes once the pool refills, or buy directly with `POST /api/v1/stamps/`. |
| `EXTENSION_TOO_SMALL` | 400 | `PATCH /stamps/{id}/extend` with a legacy `amount` below what 24 hours costs at the current price. `detail.minimum_amount` is the floor in PLUR per chunk. | Send `duration_hours` instead, or raise `amount` to `minimum_amount`. |
| `DEPTH_TOO_HIGH` | 400 | Buy-batch-for-owner: depth is above the configured maximum. | Use a smaller depth. |
| `DURATION_TOO_LONG` | 400 | Buy-batch-for-owner: duration is above the configured maximum. | Use a shorter duration. |
| `OWNER_NOT_ALLOWLISTED` | 403 | Buy-batch-for-owner: the owner address is not allowed. | Ask the operator to allow-list the address. |
| `COST_TOO_HIGH` | 400 | Buy-batch-for-owner: the batch would cost more than the configured maximum. | Use a smaller depth or a shorter duration. |
| `SIGNER_INSUFFICIENT_FUNDS` | 503 | Buy-batch-for-owner: the gateway's signer wallet cannot fund the batch. | Retry later; the operator must refill the wallet. |
| `SIGNER_BUSY` | 503 | Buy-batch-for-owner: another batch creation holds the signer, or an earlier transaction from it is still unconfirmed. **Nothing was sent and nothing was charged** — the gateway serialises signer use so two creations cannot share a nonce. | Retry in a minute. If it persists, an earlier transaction is stuck; the operator should check the signer wallet. |
| `CREATE_BATCH_FAILED` | 502 | Buy-batch-for-owner: the on-chain createBatch did not succeed. The reason is deliberately **not** in the response — it is RPC and contract internals, logged for the operator only. No BZZ moved for a reverted or unsent transaction; a receipt timeout answers 202 instead, not this. | Retry. If it repeats, the operator has the detail in the logs. |

## Uploads

| Code | Status | Meaning | What to do |
|---|---|---|---|
| `FILE_TOO_LARGE` | 413 (422 from `/pricing`) | The upload, or `upload_bytes` on `/pricing`, is over the gateway's maximum upload size. | Split the data, or use chunk uploads. |
| `DOWNLOAD_TOO_LARGE` | 413 | The content behind the reference is larger than the gateway will fetch. `detail.max_size_mb` is the limit. | Fetch it from a Swarm node directly. |
| `OWNER_PROOF_INVALID` | 401 | An upload to an owned stamp carried an owner proof (`X-Owner-Timestamp`, `X-Owner-Signature`) that was incomplete, malformed, expired, in use by another request, already used, or not a valid signature. `message` says which. See [x402-client-integration.md](x402-client-integration.md#uploading-to-your-own-stamp). | Sign a fresh proof for this stamp and upload again. Each proof buys one upload, and is used up only when Bee stores it. |
| `STAMP_OWNERSHIP_DENIED` | 403 | The stamp belongs to another payer, or is pool inventory that was not handed to you. | Use a stamp you bought or acquired. |
| `NOTARY_NOT_ENABLED` | 400 | `sign=notary` was requested, but the notary is off. | Upload without `sign`. |
| `NOTARY_NOT_CONFIGURED` | 400 | `sign=notary` was requested, but the notary has no key. | Upload without `sign`, or ask the operator. |
| `INVALID_DOCUMENT_FORMAT` | 400 | `sign=notary` needs a JSON document in the expected shape. | Fix the document (see [notary-guide.md](notary-guide.md)). |
| `INVALID_SIGN_OPTION` | 400 | `sign` has an unknown value. | Use `sign=notary`, or leave it out. |

## Stamp problems on upload

These come back as 400 when an upload fails because of its stamp, either from
`validate_stamp=true` before the upload or from diagnosing a failed upload.
`detail` also has `suggestion`, `stamp_id` and usually `stamp_status`.
`GET /api/v1/stamps/{id}/check` reports the same codes in its `errors` and
`warnings` lists, with status 200.

| Code | Meaning | What to do |
|---|---|---|
| `NOT_FOUND` | The stamp does not exist on this gateway's Bee node. | Check the ID, or buy a stamp here. |
| `NOT_LOCAL` | The stamp exists, but this node does not own it and cannot sign with it. | Use a stamp bought through this gateway. |
| `EXPIRED` | The stamp has expired. | Buy a new stamp. |
| `NOT_USABLE` | The stamp is not usable yet (a new batch takes 30-90 seconds to propagate). | Wait and retry, polling `GET /api/v1/stamps/{id}/check`. |
| `FULL` | The stamp has no capacity left. | Buy a new stamp. |
| `NEARLY_FULL` | The stamp is nearly full; large uploads may fail. A warning on `/check`. | Buy a new stamp soon. |
| `HIGH_UTILIZATION` | The stamp is over 80% used. A warning on `/check` only. | Plan a new stamp. |
| `LOW_TTL` | The stamp expires within the hour. A warning on `/check` only. | Extend it, or buy a new one. |
| `API_ERROR` | `/check` could not read stamp data from the Bee node. | Retry later. |

## Chunk uploads

| Code | Status | Meaning | What to do |
|---|---|---|---|
| `MISSING_STAMP` | 400 | The `Swarm-Postage-Stamp` header is missing. | Send the marshaled stamp for the chunk. |
| `INVALID_STAMP` | 400 | `Swarm-Postage-Stamp` is not a 113-byte hex-encoded marshaled stamp. | Fix the encoding. |
| `EMPTY_CHUNK` | 400 | The chunk body is empty. | Send the chunk bytes. |
| `CHUNK_TOO_LARGE` | 413 | The chunk is over the maximum chunk size. | Chunks are at most 4096 bytes of payload plus the span. |

## Diagnostics

| Code | Status | Meaning | What to do |
|---|---|---|---|
| `PATH_NOT_ALLOWED` | 403 | The debug proxy does not serve that Bee path. `detail.allowed` lists the ones it does. | Use a listed path. |
