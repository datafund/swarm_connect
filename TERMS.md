# Provenance Gateway: service terms

> **Draft for review.** Datafund must review this before the gateway takes payments on Base mainnet. Items marked **[to confirm]** need a decision (legal entity, jurisdiction, contact channel).

These terms cover the hosted Provenance Gateway at `provenance-gateway.datafund.io` and its staging instance. The software itself is MIT-licensed (see `LICENSE`); these terms are about using the hosted service.

## 1. What the service is

The gateway is a convenience layer in front of the Swarm network. It buys and manages postage stamps, uploads and retrieves data, and forwards pre-stamped chunks. It is **beta** software, provided as is, without warranties of any kind.

## 2. What a payment buys

Payments are made in USDC through the x402 protocol. The price for each operation is shown in the HTTP 402 response before you pay.

| You pay for | You get |
|---|---|
| A stamp purchase or extension | A Swarm postage batch, or additional time on one, of the depth and duration you requested |
| A pooled stamp | A pre-bought batch of the size you requested, with whatever validity it has left |
| An upload | The upload of your data using **your** stamp |
| Bandwidth credit | Prepaid bytes for forwarding pre-stamped chunks, bound to the paying wallet |

**Storage is not permanent.** Swarm keeps data only while the stamp it was uploaded with is valid. Upload and stamp responses include `expires_at`. After that time, data can disappear from the network unless the stamp is extended. Pool stamps and free-tier stamps are short-lived (about a day).

## 3. When something goes wrong

- **A request that is refused** (invalid input, not your stamp, a limit reached, the node unavailable) **is not charged**. Payment is collected only right before the operation is carried out.
- **If a payment cannot be settled**, nothing is delivered and nothing is charged. The response says so (`PAYMENT_SETTLEMENT_FAILED`).
- **If a payment was collected but the operation then failed**, the response carries `x402_status: settled_not_delivered` and the settlement transaction. Contact us with that transaction for a refund of that amount. Refunds are made in USDC to the paying address. **[to confirm: contact channel and response time]**
- Refunds are not given for data that expired because its stamp was not extended.

## 4. Free tier and fair use

A free tier is offered for evaluation, and is limited per client and per day. It can be reduced or withdrawn at any time. Automated abuse, attempts to get around the limits, and use that degrades the service for others may be blocked.

## 5. Your data

Data uploaded to Swarm is public to anyone who has its reference, unless you encrypt it before uploading. Do not upload data you are not entitled to share. The gateway keeps request logs and an audit log of payments (payer address, amount, transaction, what was delivered) for operating the service and handling refunds.

## 6. Changes and contact

These terms can change. The current version is always linked from the gateway's `/` response (`terms_url`).

- Entity and jurisdiction: **[to confirm]**
- Contact: **[to confirm]**
