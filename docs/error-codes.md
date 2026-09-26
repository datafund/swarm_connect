# Error codes

Error responses from the gateway carry `code` and `message` at the top level
of the JSON body. Branch on `code`, and show `message` to a person. The one
exception is the payment settlement failure described below, which has no
`code` yet.

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

Two responses keep their own shape:

- **402 Payment Required** from the x402 payment check. Its body is the x402
  payment request (`x402Version`, `error`, `accepts`, `freeTier`), currently
  under `detail`; `code` is `HTTP_402`. See
  [x402-client-integration.md](x402-client-integration.md) for how to pay it.
  To learn a price *without* triggering a 402, call `GET /api/v1/pricing`.
- **Payment settlement failure** (500) after a paid request. Its body is
  `{error, detail, x402_status: "settlement_failed", message}`, with **no
  `code`**; recognise it by `x402_status`. The request was processed, and the
  failure may be a timeout after the transfer already went through on-chain.
  **Do not retry automatically or re-pay** because the body says to: that can
  pay, and run the operation, twice. First check whether your payment
  authorization's nonce was used (or your USDC balance), and if it was, contact
  the operator with the payer address and the time of the request. Once
  `Idempotency-Key` is supported (#422), send one with every paid request, so
  that a retry returns the first result instead of charging again.

A test (`tests/test_error_codes_doc.py`) fails if a code is raised in `app/`
without being listed here, or listed here but no longer raised.

## Request and envelope errors

| Code | Status | Meaning | What to do |
|---|---|---|---|
| `VALIDATION_ERROR` | 422 | The request body, query or path failed validation. `detail` is the list of field errors. | Fix the fields named in `detail`. Do not retry unchanged. |
| `BODY_TOO_LARGE` | 413 | A JSON body is over the gateway's JSON size limit. | Send a smaller body. File uploads use multipart, which this limit does not apply to. |
| `JSON_TOO_DEEP` | 400 | A JSON body is nested too deeply. | Flatten the body. |
| `RATE_LIMIT_EXCEEDED` | 429 | Too many requests from this client (global limiter). `retry_after` and the `Retry-After` header say how long to wait. | Wait `retry_after` seconds, then retry. |
| `HTTP_<status>` | any | The endpoint raised no specific code. This includes the free-tier limit on stamps and uploads (`HTTP_429`, with `detail.payment_info`). | Act on the status: 4xx means fix the request, 429 means wait or pay, 502/503 means retry later. |

## Payments and pricing

| Code | Status | Meaning | What to do |
|---|---|---|---|
| `HTTP_402` | 402 | Payment required (x402), or the payment sent was rejected (invalid header, verification failed; see `message`). `detail.accepts` lists what to pay; `detail.freeTier` is present when the free tier is available. | Sign a payment for `accepts[0]` and retry with `X-PAYMENT`, or retry with `X-Payment-Mode: free`. |
| `PRICING_UNAVAILABLE` | 503 | `GET /api/v1/pricing` could not read the current chain price. | Retry shortly. |
| `PAYMENT_REQUIRED` | 402 | Bandwidth credit top-up was attempted on the free tier. | Pay with `X-PAYMENT`. The free tier cannot fund credit. |
| `BILLING_DISABLED` | 400 | Bandwidth credit top-up needs x402, which is off on this gateway. | Use the free tier for chunk uploads, or another gateway. |
| `TOPUP_TOO_SMALL` | 400 | The `mb` top-up is below the minimum (named in `message`). | Raise `mb` to at least the minimum. |
| `TOPUP_TOO_LARGE` | 400 (422 from `/pricing`) | The `mb` top-up is above the per-request maximum. | Split it into several top-ups. |
| `CREDIT_REQUIRED` | 402 | A chunk upload came with no credit token and no free-tier header. | Top up with `POST /api/v1/chunks/credit`, or send `X-Payment-Mode: free`. |
| `INVALID_CREDIT_TOKEN` | 402 | The bandwidth credit token is unknown. | Top up again to obtain a valid token. |
| `INSUFFICIENT_CREDIT` | 402 | The credit left is less than this chunk. | Top up, then retry the chunk. |
| `FREE_TIER_DISABLED` | 402 | The free tier is off for this operation (chunk upload, or buy-batch-for-owner). | Pay with `X-PAYMENT`, or for chunks, top up credit. |
| `FREE_QUOTA_EXCEEDED` | 429 | The free-tier chunk quota for this client is used up. | Wait for the daily quota to reset, or top up credit. |
| `PAYMENT_SETTLEMENT_UNAVAILABLE` | 502 | The payment could not be settled and **nothing was delivered**: the facilitator errored, so it is unknown whether the transfer was submitted. `detail.x402_status` is `settlement_failed`. | Retry with a fresh payment authorization — a transfer that did go through spent the nonce, so a retry cannot double-charge. If a transfer appears on-chain, contact the operator with this request's authorization for a refund. |
| `PAYMENT_SETTLEMENT_FAILED` | 402 | The facilitator refused the payment, so **nothing was delivered**. `detail.reason` gives the refusal, `detail.x402_status` is `settlement_failed`. | Fix what `reason` names (usually funds or an expired authorization) and retry. |
| `DELIVERY_FAILED_AFTER_PAYMENT` | 500 | The payment **was collected** and the request then failed. The only code here that means money moved without a result. `detail.transaction` and the `X-Payment-Transaction` header carry the transfer; `x402_status` is `settled_not_delivered`. | Do not retry blindly. Contact the operator with the transaction for a refund. |

## Stamps

| Code | Status | Meaning | What to do |
|---|---|---|---|
| `STAMP_COST_EXCEEDS_LIMIT` | 400 | The requested batch would cost more than the gateway allows per purchase. | Ask for a smaller size or a shorter duration. |
| `DAILY_SPEND_BUDGET_EXHAUSTED` | 429 | This caller's daily purchase budget is spent. | Wait until the budget resets (see `message`). |
| `DAILY_STAMP_ALLOWANCE_EXHAUSTED` | 429 | `POST /pool/acquire`: the free daily allowance for this size is used up. `detail` has `allowance`, `used`, `resets_at` and an `alternative`. | Wait for `resets_at`, or follow `detail.alternative` (pay, or buy a stamp directly). |
| `REQUESTED_SIZE_UNAVAILABLE` | 409 | `POST /pool/acquire`: no stamp of the requested size is in the pool, and although a larger one was, today's allowance for that larger size is spent. `detail.size` is what was asked for. | Retry in a few minutes once the pool refills, or buy directly with `POST /api/v1/stamps/`. |
| `EXTENSION_TOO_SMALL` | 400 | `PATCH /stamps/{id}/extend` with a legacy `amount` below what 24 hours costs at the current price. `detail.minimum_amount` is the floor in PLUR per chunk. | Send `duration_hours` instead, or raise `amount` to `minimum_amount`. |
| `DEPTH_TOO_HIGH` | 400 | Buy-batch-for-owner: depth is above the configured maximum. | Use a smaller depth. |
| `DURATION_TOO_LONG` | 400 | Buy-batch-for-owner: duration is above the configured maximum. | Use a shorter duration. |
| `OWNER_NOT_ALLOWLISTED` | 403 | Buy-batch-for-owner: the owner address is not allowed. | Ask the operator to allow-list the address. |
| `COST_TOO_HIGH` | 400 | Buy-batch-for-owner: the batch would cost more than the configured maximum. | Use a smaller depth or a shorter duration. |
| `SIGNER_INSUFFICIENT_FUNDS` | 503 | Buy-batch-for-owner: the gateway's signer wallet cannot fund the batch. | Retry later; the operator must refill the wallet. |

## Uploads

| Code | Status | Meaning | What to do |
|---|---|---|---|
| `FILE_TOO_LARGE` | 413 (422 from `/pricing`) | The upload, or `upload_bytes` on `/pricing`, is over the gateway's maximum upload size. | Split the data, or use chunk uploads. |
| `DOWNLOAD_TOO_LARGE` | 413 | The content behind the reference is larger than the gateway will fetch. `detail.max_size_mb` is the limit. | Fetch it from a Swarm node directly. |
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
