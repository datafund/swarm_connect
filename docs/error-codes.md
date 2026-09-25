# Error codes

Every error response from the gateway carries `code` and `message` at the top
level of the JSON body. Branch on `code`, and show `message` to a person.

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

Two responses keep their own shape in addition to `code`/`message`:

- **402 Payment Required** from the x402 payment check. Its body is the x402
  payment request (`x402Version`, `error`, `accepts`, `freeTier`), currently
  under `detail`; `code` is `HTTP_402`. See
  [x402-client-integration.md](x402-client-integration.md) for how to pay it.
  To learn a price *without* triggering a 402, call `GET /api/v1/pricing`.
- **Payment settlement failure** (500) after a paid request. Its body is
  `{error, detail, x402_status: "settlement_failed", message}`. Retry with a
  new payment.

A test (`tests/test_error_codes_doc.py`) fails if a code is raised in `app/`
without being listed here.

## Request and envelope errors

| Code | Status | Meaning | What to do |
|---|---|---|---|
| `VALIDATION_ERROR` | 422 | The request body, query or path failed validation. `detail` is the list of field errors. | Fix the fields named in `detail`. Do not retry unchanged. |
| `BODY_TOO_LARGE` | 413 | A JSON body is over the gateway's JSON size limit. | Send a smaller body. File uploads use multipart, which this limit does not apply to. |
| `JSON_TOO_DEEP` | 400 | A JSON body is nested too deeply. | Flatten the body. |
| `RATE_LIMIT_EXCEEDED` | 429 | Too many requests from this client (global limiter). `retry_after` and the `Retry-After` header say how long to wait. | Wait `retry_after` seconds, then retry. |
| `HTTP_<status>` | any | The endpoint raised no specific code. | Act on the status: 4xx means fix the request, 502/503 means retry later. |

## Payments and pricing

| Code | Status | Meaning | What to do |
|---|---|---|---|
| `HTTP_402` | 402 | Payment required (x402). `detail.accepts` lists what to pay; `detail.freeTier` is present when the free tier is available. | Sign a payment for `accepts[0]` and retry with `X-PAYMENT`, or retry with `X-Payment-Mode: free`. |
| `PRICING_UNAVAILABLE` | 503 | `GET /api/v1/pricing` could not read the current chain price. | Retry shortly. |
| `PAYMENT_REQUIRED` | 402 | Bandwidth credit top-up was attempted on the free tier. | Pay with `X-PAYMENT`. The free tier cannot fund credit. |
| `BILLING_DISABLED` | 400 | Bandwidth credit top-up needs x402, which is off on this gateway. | Use the free tier for chunk uploads, or another gateway. |
| `TOPUP_TOO_SMALL` | 400 | The `mb` top-up is below the minimum (named in `message`). | Raise `mb` to at least the minimum. |
| `TOPUP_TOO_LARGE` | 400 | The `mb` top-up is above the per-request maximum. | Split it into several top-ups. |
| `CREDIT_REQUIRED` | 402 | A chunk upload came with no credit token and no free-tier header. | Top up with `POST /api/v1/chunks/credit`, or send `X-Payment-Mode: free`. |
| `INVALID_CREDIT_TOKEN` | 402 | The bandwidth credit token is unknown. | Top up again to obtain a valid token. |
| `INSUFFICIENT_CREDIT` | 402 | The credit left is less than this chunk. | Top up, then retry the chunk. |
| `FREE_TIER_DISABLED` | 402 | The free tier is off for this operation (chunk upload, or buy-batch-for-owner). | Pay with `X-PAYMENT`, or for chunks, top up credit. |
| `FREE_QUOTA_EXCEEDED` | 429 | The free-tier chunk quota for this client is used up. | Wait for the daily quota to reset, or top up credit. |

## Stamps

| Code | Status | Meaning | What to do |
|---|---|---|---|
| `STAMP_COST_EXCEEDS_LIMIT` | 400 | The requested batch would cost more than the gateway allows per purchase. | Ask for a smaller size or a shorter duration. |
| `DAILY_SPEND_BUDGET_EXHAUSTED` | 429 | This caller's daily purchase budget is spent. | Wait until the budget resets (see `message`). |
| `DAILY_STAMP_ALLOWANCE_EXHAUSTED` | 429 | `POST /pool/acquire`: the free daily allowance for this size is used up. `detail` has `allowance`, `used`, `resets_at` and an `alternative`. | Wait for `resets_at`, or follow `detail.alternative` (pay, or buy a stamp directly). |
| `DEPTH_TOO_HIGH` | 400 | Buy-batch-for-owner: depth is above the configured maximum. | Use a smaller depth. |
| `DURATION_TOO_LONG` | 400 | Buy-batch-for-owner: duration is above the configured maximum. | Use a shorter duration. |
| `OWNER_NOT_ALLOWLISTED` | 403 | Buy-batch-for-owner: the owner address is not allowed. | Ask the operator to allow-list the address. |
| `COST_TOO_HIGH` | 400 | Buy-batch-for-owner: the batch would cost more than the configured maximum. | Use a smaller depth or a shorter duration. |
| `SIGNER_INSUFFICIENT_FUNDS` | 503 | Buy-batch-for-owner: the gateway's signer wallet cannot fund the batch. | Retry later; the operator must refill the wallet. |

## Uploads

| Code | Status | Meaning | What to do |
|---|---|---|---|
| `FILE_TOO_LARGE` | 413 | The upload is over the gateway's maximum upload size. | Split the data, or use chunk uploads. |
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
