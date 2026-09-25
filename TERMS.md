# Provenance Gateway: service terms

> **Draft for review.** Datafund must review this before the gateway takes payments on Base mainnet. Items marked **[to confirm]** need a decision (legal entity, jurisdiction, contact channel, log retention). It is not linked from the service (`TERMS_URL` is empty) until approved.
>
> It describes the gateway as it behaves once the audit fixes it relies on are deployed: payment settled only right before the work (#398), paid and owner-checked stamp extension (#397), and the payment audit trail (#411). Do not publish it before those are live.

Version: draft · Effective: **[to confirm]**

These terms cover the hosted Provenance Gateway at `provenance-gateway.datafund.io` and its staging instance. The software itself is MIT-licensed (see `LICENSE`); these terms are about using the hosted service.

## 1. What the service is

The gateway is a convenience layer in front of the Swarm network. It buys and manages postage stamps, uploads and retrieves data, and forwards pre-stamped chunks. It is **beta** software, provided as is, without warranties of any kind.

## 2. What a payment buys

Payments are made in USDC through the x402 protocol. The price for each operation is shown in the HTTP 402 response before you pay.

| You pay for | You get |
|---|---|
| A stamp purchase | A Swarm postage batch of the depth and duration you requested, registered to the paying wallet |
| A stamp extension | Additional time on a batch registered to your wallet |
| A pooled stamp | A pre-bought batch of the size you requested, with whatever validity it has left (at least about a day) |
| An upload | The upload of your data using a stamp you own, or a shared one |
| Bandwidth credit | Prepaid bytes for forwarding pre-stamped chunks. They are spent by presenting a bearer token: anyone holding the token can spend them, so keep it secret |

Stamps obtained on the free tier are **shared**: anyone can upload with them, and the space in them is used up by all users.

**Storage is not permanent.** Swarm keeps data only while the stamp it was uploaded with is valid. Upload, stamp and extension responses include `expires_at`: an **estimate** at today's storage price, which can change, so extend with a margin. After a stamp runs out, data uploaded with it can disappear from the network. Pool stamps are handed out with at least about a day left. Free-tier and purchased stamps last for the duration requested.

## 3. When something goes wrong

- **A request that is refused** (invalid input, not your stamp, a limit reached, the node unavailable) **is not charged**. Payment is collected only right before the operation is carried out.
- **If a payment cannot be settled**, nothing is delivered and nothing is charged. The response says so (`PAYMENT_SETTLEMENT_FAILED`).
- **If a payment was collected but the operation then failed**, the response carries `x402_status: settled_not_delivered` and the settlement transaction. Contact us with that transaction for a refund of that amount. Refunds are made in USDC to the paying address. **[to confirm: contact channel and response time]**
- Refunds are not given for data that expired because its stamp was not extended.

## 4. Free tier and fair use

A free tier is offered for evaluation, and is limited per client and per day. It can be reduced or withdrawn at any time. Automated abuse, attempts to get around the limits, and use that degrades the service for others may be blocked.

## 5. Your data

Data uploaded to Swarm is public to anyone who has its reference, unless you encrypt it before uploading. Do not upload data you are not entitled to share. The gateway keeps request logs (including client IP addresses) and an audit log of payments (payer address, amount, transaction, what was delivered) for operating the service and handling refunds. Retention period: **[to confirm]**.

## 6. Changes and contact

These terms can change. The current version is linked from the gateway's `/` response (`terms_url`), once approved.

- Entity and jurisdiction: **[to confirm]**
- Contact: **[to confirm]**
