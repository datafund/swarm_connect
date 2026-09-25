// Buy a stamp, upload a file and download it again through the gateway, using
// the free tier or paying with x402. Node 18+ (global fetch, FormData, Blob).
//
//   node x402_client.mjs http://localhost:8000                 # free tier
//   npm install x402@1.2.0 viem                                # only for --paid
//   X402_PRIVATE_KEY=0x... node x402_client.mjs http://localhost:8000 --paid
//
// --paid signs an EIP-3009 USDC authorization for the price the gateway quotes,
// but only for USDC on NETWORK (base-sepolia; pass --network=base for mainnet)
// and, if PAY_TO is set, only to that address. MAX_USD caps each payment and
// MAX_TOTAL_USD the run (two payments: the stamp and the upload). Exits
// non-zero on any failure.

const gw = (process.argv[2] || "http://localhost:8000").replace(/\/$/, "");
const paid = process.argv.includes("--paid");
const NETWORK = process.argv.includes("--network=base") ? "base" : "base-sepolia";
const PAY_TO = process.env.PAY_TO; // optional: the gateway's known payee
const MAX_USD = 0.10;
const MAX_TOTAL_USD = 0.25;
const USDC_UNITS = 1_000_000; // USDC has 6 decimals: "10000" means $0.01
// The USDC contract per network. A payment is signed only for these.
const USDC = {
  "base-sepolia": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
  base: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
};
let spent = 0;

// The x402 spec puts `accepts` at the top level of the 402. Older gateway
// versions nest the whole body under `detail`, so read the top level first.
function paymentRequirements(body) {
  return body.accepts ?? body.detail.accepts;
}

// The `exact` USDC entry on NETWORK (to PAY_TO, if set), or refuse. A client
// signs whatever the 402 names; pinning the chain and token means a gateway on
// another network, or a misconfigured one, gets no signature.
function choose(accepts) {
  const a = accepts.find((r) =>
    r.scheme === "exact" && r.network === NETWORK &&
    r.asset?.toLowerCase() === USDC[NETWORK].toLowerCase() &&
    (!PAY_TO || r.payTo?.toLowerCase() === PAY_TO.toLowerCase()));
  if (!a) throw new Error(`Refusing to sign: no USDC payment on ${NETWORK}${PAY_TO ? ` to ${PAY_TO}` : ""}`);
  return a;
}

async function signPayment(requirement) {
  const { createPaymentHeader } = await import("x402/client");
  const { createSigner } = await import("x402/types");
  const signer = await createSigner(NETWORK, process.env.X402_PRIVATE_KEY);
  return createPaymentHeader(signer, 1, requirement);
}

// Send a protected request: free tier, or answer the 402 with a payment.
// Success is any 2xx (res.ok). Stamp purchase answers 201, uploads 200.
// `makeInit` builds a fresh request each time, because a body can be sent once.
async function call(url, makeInit) {
  if (!paid) {
    const init = makeInit();
    const res = await fetch(url, { ...init, headers: { ...init.headers, "X-Payment-Mode": "free" } });
    if (!res.ok) throw new Error(`${url}: HTTP ${res.status} ${await res.text()}`);
    return res;
  }
  let res = await fetch(url, makeInit());
  if (res.status === 402) {
    const requirement = choose(paymentRequirements(await res.json()));
    const price = Number(requirement.maxAmountRequired) / USDC_UNITS;
    if (price > MAX_USD) throw new Error(`Price $${price} is above $${MAX_USD}; not paying.`);
    if (spent + price > MAX_TOTAL_USD) throw new Error(`Paying $${price} would exceed $${MAX_TOTAL_USD} this run; not paying.`);
    spent += price;
    console.log(`  402: paying $${price.toFixed(6)} USDC on ${requirement.network} to ${requirement.payTo}`);
    const init = makeInit();
    res = await fetch(url, { ...init, headers: { ...init.headers, "X-PAYMENT": await signPayment(requirement) } });
    const settled = res.headers.get("X-PAYMENT-RESPONSE");
    if (settled) console.log("  settled:", JSON.parse(Buffer.from(settled, "base64").toString()));
  }
  if (!res.ok) throw new Error(`${url}: HTTP ${res.status} ${await res.text()}`);
  return res;
}

// 1. Buy a stamp. Ask for a duration; the gateway computes the amount.
let res = await call(`${gw}/api/v1/stamps/`, () => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ size: "small", duration_hours: 25 }),
}));
const stamp = (await res.json()).batchID;
console.log(`Stamp purchase: HTTP ${res.status}, batchID ${stamp}`);

// 2. A new batch takes a short while to become usable.
for (let i = 0; ; i++) {
  const check = await (await fetch(`${gw}/api/v1/stamps/${stamp}/check`)).json();
  if (check.can_upload) break;
  if (i >= 60) throw new Error("Stamp did not become usable in time");
  await new Promise((r) => setTimeout(r, 5000));
}
console.log("Stamp is usable");

// 3. Upload. The endpoint takes a multipart file named "file", not a JSON body.
const payload = JSON.stringify({ hello: "swarm", at: Date.now() });
res = await call(`${gw}/api/v1/data/?stamp_id=${stamp}&content_type=application/json`, () => {
  const form = new FormData();
  form.append("file", new Blob([payload], { type: "application/json" }), "hello.json");
  return { method: "POST", body: form };
});
const ref = (await res.json()).reference;
console.log(`Upload: HTTP ${res.status}, reference ${ref}`);

// 4. Download (always free) and compare.
res = await fetch(`${gw}/api/v1/data/${ref}`);
const text = await res.text();
if (!res.ok || text !== payload) throw new Error("download failed or differs from upload");
console.log(`Download: HTTP ${res.status}, ${text.length} bytes, matches upload`);
if (paid) console.log(`Spent $${spent.toFixed(6)} USDC on ${NETWORK}`);
