// Generates the TypeScript side of the x402 v2 interop fixtures (#606).
//
// Run it outside the repository, in a scratch directory holding a copy of this
// file and package.json (see README.md):
//
//   npm install && node generate_ts.mjs <path to tests/fixtures/x402_v2>
//
// It reads payment_required.json and gateway_payment_required.b64 from that
// directory and writes:
//   ts_payment_required.b64   PAYMENT-REQUIRED from encodePaymentRequiredHeader
//   ts_payment_signature.b64  PAYMENT-SIGNATURE: a payment for accepts[0],
//                             signed by @x402/evm with a throwaway key made here
//   ts_payment_response.b64   PAYMENT-RESPONSE from encodePaymentResponseHeader
//                             (a synthetic success; no facilitator is called)
//   ts_decoded_gateway_payment_required.json
//                             what the client makes of the gateway's header
//
// The key is generated on each run, used once and never written anywhere.
// Nothing here touches a network.
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { x402Client, x402HTTPClient } from "@x402/core/client";
import {
  decodePaymentRequiredHeader,
  encodePaymentRequiredHeader,
  encodePaymentResponseHeader,
  encodePaymentSignatureHeader,
} from "@x402/core/http";
import { PaymentRequiredV2Schema } from "@x402/core/schemas";
import { registerExactEvmScheme } from "@x402/evm/exact/client";
import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";

const out = process.argv[2];
if (!out) {
  console.error("usage: node generate_ts.mjs <fixtures dir>");
  process.exit(2);
}
const here = dirname(fileURLToPath(import.meta.url));
const versions = Object.fromEntries(
  ["@x402/core", "@x402/evm", "@x402/fetch", "viem"].map((p) => [
    p,
    JSON.parse(readFileSync(join(here, "node_modules", p, "package.json"), "utf8")).version,
  ]),
);
const write = (name, text) => writeFileSync(join(out, name), text + "\n");

// A throwaway signer: random, in memory only.
const account = privateKeyToAccount(generatePrivateKey());
const client = new x402Client();
registerExactEvmScheme(client, { signer: account });
const http = new x402HTTPClient(client);

// 1. PAYMENT-REQUIRED built by @x402/core from the gateway-like offer.
const offer = JSON.parse(readFileSync(join(out, "payment_required.json"), "utf8"));
const required = encodePaymentRequiredHeader(PaymentRequiredV2Schema.parse(offer));
write("ts_payment_required.b64", required);

// 2. PAYMENT-SIGNATURE: what a client sends after reading that 402.
const paymentRequired = http.getPaymentRequiredResponse((n) => (n.toUpperCase() === "PAYMENT-REQUIRED" ? required : null));
const payment = await client.createPaymentPayload(paymentRequired);
write("ts_payment_signature.b64", encodePaymentSignatureHeader(payment));

// 3. PAYMENT-RESPONSE for a synthetic success (placeholder transaction hash).
write(
  "ts_payment_response.b64",
  encodePaymentResponseHeader({
    success: true,
    transaction: "0x" + "00".repeat(32),
    network: "eip155:84532",
    payer: account.address,
  }),
);

// 4. The gateway's own PAYMENT-REQUIRED, read the way a client reads a 402.
const gatewayHeader = readFileSync(join(out, "gateway_payment_required.b64"), "utf8").trim();
const result = { versions, header: gatewayHeader };
try {
  const decoded = decodePaymentRequiredHeader(gatewayHeader);
  result.decoded = decoded;
  result.schemaParsed = PaymentRequiredV2Schema.parse(decoded);
  result.clientParsed = http.getPaymentRequiredResponse((n) => (n.toUpperCase() === "PAYMENT-REQUIRED" ? gatewayHeader : null));
  // The client can pay it: it builds a payment for the gateway's offer.
  const paid = await client.createPaymentPayload(result.clientParsed);
  result.paymentAccepted = paid.accepted;
  result.paymentResource = paid.resource;
  result.ok = true;
} catch (err) {
  result.ok = false;
  result.error = String(err);
}
write("ts_decoded_gateway_payment_required.json", JSON.stringify(result, null, 2));
console.log(`wrote TypeScript fixtures with ${JSON.stringify(versions)}; gateway header ok=${result.ok}`);
