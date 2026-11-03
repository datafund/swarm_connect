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
- `PAYMENT_SETTLEMENT_IN_PROGRESS` (409): a parallel duplicate of a payment
  being settled right now; not charged, wait for the first answer.
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

Do not pay again without first reading the advice in its row. All of these carry their fields under `detail`, with `code` and `message` also at the top level; `PAYMENT_SETTLEMENT_FAILED` form 2 and `PAYMENT_SETTLEMENT_UNKNOWN` carry all their fields at the top level too, and form 1 carries `reason` and `accepts` at both.
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
| `BEE_BUSY` | 503 | The gateway is already making as many calls to its Bee node as it allows at once (#588), and this request's own Bee call got no slot within a short wait. Also returned up front for a request to a paid route that carries `X-PAYMENT` when no slot is free. Either way, **nothing was charged**. A request that was charged keeps its own answer. `Retry-After` says how long to wait (default 5 s). | Wait `Retry-After` seconds, then retry. |
| `RATE_LIMIT_EXCEEDED` | 429 | Too many requests from this client (global limiter). `retry_after` and the `Retry-After` header say how long to wait. | Wait `retry_after` seconds, then retry. |
| `HTTP_<status>` | any | The endpoint raised no specific code. This includes the free-tier limit on stamps and uploads (`HTTP_429`, with `detail.payment_info`). | Act on the status: 4xx means fix the request, 429 means wait or pay, 502/503 means retry later. |

## Payments and pricing

| Code | Status | Meaning | What to do |
|---|---|---|---|
| `HTTP_402` | 402 | Payment required (x402), or the payment sent was rejected (invalid header, verification failed; see `message`). A verification failure reads `Payment verification failed: <reason>`, where `<reason>` is the facilitator's short code or `facilitator error; details in the gateway log`. `accepts` lists what to pay; `freeTier` is present when the free tier is available (both also under `detail`, deprecated). | Sign a payment for `accepts[0]` and retry with `X-PAYMENT`, or retry with `X-Payment-Mode: free`. |
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
| `PAYMENT_SETTLEMENT_UNAVAILABLE` | 502 | The payment could not be settled and **nothing was delivered**, but the facilitator errored, so it is unknown whether the transfer was submitted: **you may have been charged**. `detail.x402_status` is `settlement_failed`, the same value as a refusal, kept for compatibility: tell the two apart by `code`, not by `x402_status`. `detail.nonce` is this authorization's nonce. Headers: `X-Payment-Mode: failed`, `X-Payment-Status: settlement_unknown` (the header, unlike `x402_status`, says the outcome is unknown). | Do not pay again with a new authorization yet. Without an `Idempotency-Key`: retry with the **same** `X-PAYMENT` while it is still valid (before `validBefore`); the gateway releases it on this response. If the first transfer did not go through, the retry is settled once and delivered. If the facilitator refuses the retry (for instance because the first transfer spent the nonce), the answer is `409 PAYMENT_AUTHORIZATION_OUTCOME_UNKNOWN`, not a `402`. If the wait for the facilitator was interrupted (a gateway shutdown or a disconnect) rather than answered with an error, the message says so and the authorization stays reserved for up to an hour: a same-`X-PAYMENT` retry in that time gets `409 PAYMENT_AUTHORIZATION_OUTCOME_UNKNOWN` and is not charged; retry after that, or check the nonce as below. With an `Idempotency-Key`: that key stays taken for 24 hours and **cannot complete**; no retry with it is charged. A new authorization with the key gets `409 IDEMPOTENCY_KEY_SETTLEMENT_UNKNOWN`. The same `X-PAYMENT` with the key gets the same `409 IDEMPOTENCY_KEY_SETTLEMENT_UNKNOWN` (also if that authorization has since settled under another key), or `409 PAYMENT_AUTHORIZATION_OUTCOME_UNKNOWN` if the facilitator rejects the authorization at verify (verify runs before the key is looked up). Check the nonce as below, then pay again under a **new** key. In every case, before signing a **new** authorization (a new nonce, which can be charged a second time), check on-chain whether this nonce was used. If it was, contact the operator with the nonce for a refund. If it was not, the facilitator may still submit it later, so before signing a new payment either wait until its `validBefore` has passed or cancel it on-chain with EIP-3009 `cancelAuthorization`. Then pay again, with a new `Idempotency-Key` if you use one. |
| `PAYMENT_SETTLEMENT_FAILED` | 402 | The payment was not confirmed as settled, and **nothing was delivered**. `x402_status` is `settlement_failed`. The facilitator refused the payment. Two forms: **(1) Before the work:** `reason` gives the refusal, and `accepts` lets you sign a new payment; both are at the top level and under `detail`. `reason` (and the reason in `message`, in both forms) is the facilitator's code, such as `insufficient_funds` or `invalid_exact_evm_nonce_already_used`, when it gave a short code (lowercase letters, digits and `_`, at most 64 characters); any other text from the facilitator is shown as `facilitator error; details in the gateway log`, and the full text is only in the gateway's log and audit record. **(2) After the work** (a paid route that settles only after it has run): only `code`, `message` and `x402_status`, at the top level and under `detail`, and the result is withheld. If the facilitator did not answer at all, the code is `PAYMENT_SETTLEMENT_UNKNOWN` instead. | Form 1: fix what `reason` names (usually funds or an expired authorization) and retry. Form 2: fix what `message` names and retry with a new payment. **Exception:** if this answers a retry of an authorization that earlier got `PAYMENT_SETTLEMENT_UNAVAILABLE` or `PAYMENT_SETTLEMENT_UNKNOWN` (the gateway answers those with `409 PAYMENT_AUTHORIZATION_OUTCOME_UNKNOWN` for 24 hours, and forgets them after that), do not pay from it: follow that row. |
| `PAYMENT_SETTLEMENT_UNKNOWN` | 502 | The request's work **has already run**, but the facilitator did not answer when the payment was settled afterwards, so it is unknown whether the transfer went through. The result is withheld. Only on a paid route that settles after its work rather than before it; every current paid route settles first and answers `PAYMENT_SETTLEMENT_UNAVAILABLE` instead. Fields: `code`, `message`, `nonce` (this request's authorization nonce) and `x402_status: settlement_unknown`, both at the top level and under `detail`. Also returned when the wait for the facilitator was interrupted after the work ran, if a response can still be sent. Headers: `X-Payment-Mode: failed`, `X-Payment-Status: settlement_unknown`. | Do not pay again from this response. The gateway remembers this authorization for 24 hours: a retry with the same `X-PAYMENT` (with or without the same `Idempotency-Key`) gets `409 PAYMENT_AUTHORIZATION_OUTCOME_UNKNOWN` before any work, and is not run or charged (also across a gateway restart: the gateway keeps this record on disk); a retry with the same `Idempotency-Key` and a new authorization gets `409 IDEMPOTENCY_KEY_SETTLEMENT_UNKNOWN` and is not charged. Check on-chain whether `nonce` was used. If it was, contact the operator with the `nonce`. If it was not, the facilitator may still submit it later, so before signing a new payment either wait until its `validBefore` has passed or cancel it on-chain with EIP-3009 `cancelAuthorization`. Then pay again, with a new `Idempotency-Key` if you use one. |
| `PAYMENT_AUTHORIZATION_OUTCOME_UNKNOWN` | 409 | This request carried a payment authorization whose earlier settlement got no answer (after `PAYMENT_SETTLEMENT_UNAVAILABLE` or `PAYMENT_SETTLEMENT_UNKNOWN`), and either its earlier work already ran (after `PAYMENT_SETTLEMENT_UNKNOWN`: answered before any work runs), it is still reserved after an interrupted settlement (`in_use`), or the facilitator now refused it, at verify or at settle. A refusal here is **probably** because the earlier transfer spent the nonce, but the gateway cannot tell for certain, so this is not a `402` and has no `accepts`. This request was not charged; the earlier one may have been. Usually this request did nothing. On a paid route that settles after its work (the facilitator refused it at `settle` after the work ran), its work ran but the result is withheld; the message says which. `detail` carries `nonce`, `x402_status: settlement_unknown` and, when the facilitator refused, its `reason` (its code, or `facilitator error; details in the gateway log` for any other text, as for `PAYMENT_SETTLEMENT_FAILED`). Headers: `X-Payment-Mode: failed`, `X-Payment-Status: settlement_unknown`. The gateway keeps such authorizations on disk for 24 hours, across restarts. Rarely (after a crash while a write-ahead was abandoned), the earlier attempt was never actually sent; the nonce check then shows the nonce unused. After that, a refusal of the same authorization is an ordinary `402` again: do not pay from it automatically until you have checked the nonce. | Do not pay again from this response. Check on-chain whether `nonce` was used. If it was, contact the operator with the `nonce` for a refund. If it was not, the facilitator may still submit it later, so before signing a new payment either wait until its `validBefore` has passed or cancel it on-chain with EIP-3009 `cancelAuthorization`. Then pay again, with a new `Idempotency-Key` if you use one. |
| `PAYMENT_SETTLEMENT_IN_PROGRESS` | 409 | A parallel duplicate: another request with the same payment authorization is being settled right now. This request was not run and not charged (on a paid route that settles after its work: its work ran, but the result is withheld). Not an unknown outcome: an answer is coming. `detail` carries `nonce` and `x402_status: settlement_in_progress`; `code` and `message` are also at the top level. Headers: `X-Payment-Mode: failed`, `X-Payment-Status: settlement_in_progress`; deliberately no `Retry-After`. A duplicate that arrives before or after the settlement gets the replay guard's `402` "already used" instead. | **Do not resend this `X-PAYMENT`:** once the first request has settled, it is answered `402` "already used. Sign a new payment." with `accepts`, and paying from that charges twice. Wait for that request's answer. If that request was interrupted (it ended without a settled result, for example with a `500` or `502`), treat its payment as an unknown outcome: check the nonce on-chain before paying again (as in the `PAYMENT_SETTLEMENT_UNAVAILABLE` row). With an `Idempotency-Key`, get the stored result by sending the same key with a **newly signed** payment: the stored result is returned (`Idempotent-Replayed: true`) and the new payment is not charged. Without a key there is no safe way to fetch it again. Never send the same `X-PAYMENT` in parallel. |
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
| `IDEMPOTENCY_KEY_SETTLEMENT_UNKNOWN` | 409 | The first request's payment was sent for settlement and no answer came back. `detail.nonce` is that authorization's nonce. Returned to every retry with that key for 24 hours, including one that presents the first request's own `X-PAYMENT`, with two exceptions where that same `X-PAYMENT` gets `409 PAYMENT_AUTHORIZATION_OUTCOME_UNKNOWN` instead, because the authorization is checked before the key: when the first request's work had already run (it got `PAYMENT_SETTLEMENT_UNKNOWN`), and when the facilitator rejects that authorization at verify. This request was not charged. Rare exception: if the gateway timed out writing the first request's "settling" record (it answered `503 PAYMENT_STATE_UNAVAILABLE`, nothing was settled) and then crashed before taking it back, the record can still land on disk: the key then answers this `409` for 24 hours although nothing was charged. The nonce check shows the nonce unused. | Do not pay again from this response, and do not keep retrying with this key: it will not complete. Check on-chain whether the nonce was used. If it was, contact the operator with the nonce. If it was not, the facilitator may still submit it later, so before signing a new payment either wait until its `validBefore` has passed or cancel it on-chain with EIP-3009 `cancelAuthorization`. Then pay again with a **new** `Idempotency-Key`. |
| `IDEMPOTENCY_KEY_DELIVERED_NOT_STORED` | 409 | The first request succeeded and was paid once, but its response was too large to keep. | Look the result up through the resource itself. |
| `IDEMPOTENCY_UNAVAILABLE` | 503 | The gateway cannot read or write its idempotency store, or a write to it did not finish in time. Only paid requests with an `Idempotency-Key` are refused; nothing was settled. | Retry later with the same key; not charged. |
| `PAYMENTS_UNAVAILABLE` | 503 | The gateway cannot accept payments right now (#589): it cannot read its Bee node's wallet balances (`detail.reason` `balances_unreadable`), which in practice means its Bee node is unreachable, or it has not been able to check its Bee node for a while (`snapshot_too_old`). Returned before the request is priced or the payment is verified: **this request was not charged**. A retry of an earlier attempt that may have been charged, whose usual answer comes from stored state without a new settlement (same `Idempotency-Key` with a stored result; an authorization already in use, whose settlement is in progress, or whose work already ran with an unknown outcome), is not refused by this check. It goes on to its usual path, but that path still prices the request before verifying the payment, and pricing can need the Bee node: while Bee cannot price, such a retry can get `503` "Failed to calculate price" (or `BEE_BUSY`) instead of its stored result or `409`. Retry later with the same `Idempotency-Key` and `X-PAYMENT`. An authorization whose earlier settlement got no answer before any work ran (it would be settled again) is refused here like a new payment. Applies to every paid route, both to a request carrying `X-PAYMENT` and to one that would otherwise get a `402` offer, so no payment is offered that could not be delivered. Free-tier requests (`X-Payment-Mode: free`) are not affected. `/health` shows the same state as `status: degraded` with `x402.bee_gnosis_wallet.can_accept: false`. `Retry-After` is set. | Wait `Retry-After` seconds and retry, or use the free tier. Do not treat it as a payment failure: no authorization was used. |
| `PAYMENT_STATE_UNAVAILABLE` | 503 | The gateway cannot read or write (or its storage does not respond in time to) its record of payments whose settlement got no answer (it writes an entry before asking the facilitator to settle every payment, so a crash cannot lose it), so it refuses paid (`X-PAYMENT`) requests until that is repaired; a write failure clears itself once a write succeeds. Also returned to a request with an `Idempotency-Key` whose idempotency "settling" record (written before every keyed settlement) could not be written in time; that case affects keyed requests only, which get `503 IDEMPOTENCY_UNAVAILABLE` from then on until the idempotency store is written again. Free-tier requests are unaffected. Nothing was settled. Usually nothing was run either. On a paid route that settles after its work, the work ran but its result is withheld, and a retry with the same `X-PAYMENT` runs the work again: you are charged at most once, but the work may run more than once (no such route exists today). `Retry-After` is set. | Retry later with the same `X-PAYMENT`; it was not charged. |

## Stamps

`PATCH /stamps/{id}/extend` of a batch the Bee node does not list answers
`404` (`code` `HTTP_404`, `message` "Stamp batch with ID '<id>' not found on
the connected Swarm node."). With x402 enabled this comes after the request's
`422` validation and before anything else: no `402` offer, no payment verified
or charged, and the free-tier request is not counted against the rate limit.
If the gateway cannot reach the Bee node to look the batch up, the answer is
`503` (`HTTP_503`, `message` "Service temporarily unavailable", `detail.detail`
"Failed to calculate price for this operation.") rather than `404`, also
before any offer, payment or free-tier count; retry later. With x402 disabled
the route answers the same `404`, and a Bee outage there is a `502`.

| Code | Status | Meaning | What to do |
|---|---|---|---|
| `STAMP_COST_EXCEEDS_LIMIT` | 400 | The requested batch would cost more than the gateway allows per purchase. | Ask for a smaller size or a shorter duration. |
| `PURCHASE_PENDING` | 202 | Paid stamp purchase: the payment was collected but the Bee node did not confirm the purchase in time. Not an error. The body has the payment `transaction` and the batch `label`; the batch is registered to the paying wallet as soon as the node reports it. | Do not pay again. Look for the label in `GET /api/v1/stamps/?wallet=<payer>`, or retry with the same `Idempotency-Key` for the `201` once it is found. |
| `PURCHASE_CAPACITY` | 503 | Too many paid stamp purchases are waiting on the Bee node. Checked before settlement: **nothing was charged**. `Retry-After: 30`. | Retry shortly. |
| `DAILY_SPEND_BUDGET_EXHAUSTED` | 429 | This caller's daily purchase budget is spent. | Wait until the budget resets (see `message`). |
| `SPEND_BUDGET_UNAVAILABLE` | 503 | Stamp purchase or extension: the gateway could not read today's spend record, so it refuses to spend rather than count from zero. Applies to paid requests too, since the gateway's daily ceiling bounds them as well. **Nothing was charged.** | Retry after the time in `message`, or once the operator has restored the record. |
| `GATEWAY_DAILY_SPEND_CEILING` | 503 | The gateway itself has spent its daily BZZ allowance, so it is refusing to spend more for anyone. Two variants, distinguished by `message`: the overall ceiling is reached, or only the portion open to unpaid requests is. In the second case paid requests still work where x402 is enabled, on a mainnet or on a testnet with `X402_ALLOW_TESTNET_PAID_BYPASS`, and the message then says so. `detail.resets_at` is when it lifts. Applies to stamp purchase, extension and buy-batch-for-owner. | Wait for `resets_at`. If the message says paid requests still work, pay with `X-PAYMENT` and retry. This is the operator's limit, not yours — if it recurs, tell them. |
| `DAILY_STAMP_ALLOWANCE_EXHAUSTED` | 429 or 402 | `POST /pool/acquire`: the free daily allowance for this size is used up. `detail` has `allowance`, `used`, `resets_at` and an `alternative`. While the gateway cannot accept payments (#589) it is always the 429: the message says the paid acquire (`X-PAYMENT`) is temporarily refused, `alternative` has only a note, and there is no `Retry-After`, because the free allowance stays spent until `resets_at`. Where paying bypasses the allowance (x402 enabled, on a mainnet or a testnet with `X402_ALLOW_TESTNET_PAID_BYPASS`), the status is 402 and the body is also an x402 payment request (`accepts`). | Wait for `resets_at`, or follow `detail.alternative`: pay for this same request (402), or buy a stamp directly. |
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
