# Provenance Gateway: alpha notice

This notice covers the hosted Provenance Gateway at `provenance-gateway.datafund.io` (production) and `provenance-gateway.dev.datafund.io` (staging). There are no full service terms for now; this notice stands in for them. The software itself is MIT-licensed (see `LICENSE`).

## Alpha software, real money

- The gateway is **alpha** software, provided as is.
- **Production charges real USDC on Base mainnet** (network `base`, `eip155:8453`), through the x402 protocol. The price of each operation is shown in the HTTP 402 response before you pay.
- There are **no guarantees**: payments, storage and the service itself can fail.
- **Refunds should not be expected.** Payments settle on-chain and are final.
- **Spend small amounts only**, and set a per-payment cap in your client.
- **Test first** on the staging gateway, which takes Base Sepolia test USDC with no real value, or on the free tier (`X-Payment-Mode: free`, 3 requests per minute). Staging stores on the real Swarm network and is meant for integration testing only.

## Feedback and problem reports

Send feedback and problem reports to **info@datafund.io**. It reaches the developers and the organisation. It is a feedback contact, not a refund contact.

When you report a payment problem, include what the gateway's response gave you: the `transaction`, or the authorization `nonce`.

## What to know before you pay

- **Storage is not permanent.** Swarm keeps data only while the postage stamp it was uploaded with is valid. Upload, stamp and extension responses include `expires_at`: an **estimate** at today's storage price, which can change, so extend with a margin. After a stamp runs out, data uploaded with it can disappear from the network.
- **Uploaded data is public** to anyone who has its reference, unless you encrypt it before uploading. Do not upload data you are not entitled to share.
- **A refused request is not charged.** Payment is collected only right before the operation is carried out.
- **If the response says the outcome is unknown** (`PAYMENT_SETTLEMENT_UNAVAILABLE`, `PAYMENT_SETTLEMENT_UNKNOWN`, `PAYMENT_AUTHORIZATION_OUTCOME_UNKNOWN`, or a `500` with no JSON error code), you may have been charged. Do not pay again until you have checked on-chain whether the authorization's nonce was used. See [docs/error-codes.md](docs/error-codes.md).
- **If a payment was collected but the operation failed**, the response carries `x402_status: settled_not_delivered` and the settlement transaction. Report it with that transaction.
- Stamps obtained on the free tier are **shared**: anyone can upload with them, and their space is used up by all users. The free tier can be reduced or withdrawn at any time.
- The gateway keeps request logs (including client IP addresses) and an audit log of payments (payer address, amount, transaction, what was delivered) to operate the service.

This notice can change. The current version is linked from the gateway's `GET /` response as `terms_url`.
