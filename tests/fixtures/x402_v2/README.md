# x402 v2 interop fixtures (#606)

These are header values recorded from the official x402 v2 libraries. `tests/test_x402_v2_interop.py` checks the gateway's v2 codecs (`app/x402/codec_v2.py`) against them in both directions:

- **Library to gateway.** The gateway decodes the `PAYMENT-REQUIRED`, `PAYMENT-SIGNATURE` and `PAYMENT-RESPONSE` values that the libraries produced.
- **Gateway to library.** Each library decoded the gateway's encoded `PAYMENT-REQUIRED` and built a payment for it. That result is recorded here. The test checks that the gateway still encodes exactly the recorded bytes, so the recorded result stays valid.

CI has no node and does not install `x402` 2.x, so nothing here runs in the test suite. Regenerate the fixtures with the commands below when the codec or the pinned library versions change.

## Throwaway keys and placeholder addresses

- Each generator creates a new random private key on every run, uses it once in memory to sign, and never writes it anywhere. The payer addresses in `ts_*` and `py_*` (`authorization.from`, `payer`) belong to those throwaway keys. They hold no funds and are not reused anywhere.
- `payTo` is the placeholder `0x0000000000000000000000000000000000000001`.
- The asset is the public Base Sepolia USDC contract `0x036CbD53842c5426634e7929541eC2318f3dCF7e`, with the EIP-712 domain `{"name": "USDC", "version": "2"}` on `eip155:84532`.
- The host is the placeholder `gateway.example`.
- The `PAYMENT-RESPONSE` values are synthetic successes with an all-zero transaction hash. No facilitator was called and nothing was settled.

## Files

| File | Made by | What it is |
|---|---|---|
| `payment_required.json` | hand | The gateway-like offer: one `exact` offer on Base Sepolia for 20000 (0.02 USDC) |
| `gateway_payment_required.b64` | `make_gateway_header.py` (the gateway) | `PAYMENT-REQUIRED` for that offer, as the gateway encodes it |
| `ts_payment_required.b64` | `generate_ts.mjs` | `PAYMENT-REQUIRED`, from `encodePaymentRequiredHeader` in `@x402/core` 2.30.0 |
| `ts_payment_signature.b64` | `generate_ts.mjs` | `PAYMENT-SIGNATURE`, from `x402Client` plus `registerExactEvmScheme` in `@x402/evm` 2.30.0 |
| `ts_payment_response.b64` | `generate_ts.mjs` | `PAYMENT-RESPONSE`, from `encodePaymentResponseHeader` |
| `ts_decoded_gateway_payment_required.json` | `generate_ts.mjs` | The gateway's header after `decodePaymentRequiredHeader`, `PaymentRequiredV2Schema.parse` and `x402HTTPClient.getPaymentRequiredResponse`, plus the payment the client built for it |
| `py_payment_required.b64`, `py_payment_signature.b64`, `py_payment_response.b64`, `py_decoded_gateway_payment_required.json` | `generate_py.py` | The same four outputs from Python `x402` 2.27.0 (`x402ClientSync`, `register_exact_evm_client`, `x402.http.utils`) |
| `package.json` | hand | Exact versions for `generate_ts.mjs`. It is only used in a scratch directory, never installed here. |

## Regenerating

Run these from the repository root, in this order. The first step must come first, because the other two read its output.

```bash
# 1. The gateway's header. This uses the repository's environment.
python tests/fixtures/x402_v2/make_gateway_header.py

# 2. TypeScript, in a scratch directory outside the repository.
SCRATCH=$(mktemp -d)
cp tests/fixtures/x402_v2/package.json tests/fixtures/x402_v2/generate_ts.mjs "$SCRATCH/"
(cd "$SCRATCH" && npm install --no-audit --no-fund && node generate_ts.mjs "$OLDPWD/tests/fixtures/x402_v2")

# 3. Python x402 2.27.0, in a throwaway environment. Never install it into the repository's environment.
uv venv "$SCRATCH/py227" && VIRTUAL_ENV="$SCRATCH/py227" uv pip install "x402[evm]==2.27.0"
"$SCRATCH/py227/bin/python" tests/fixtures/x402_v2/generate_py.py
```

Each run uses a new key, nonce and `validBefore`. So `ts_payment_signature.b64`, `py_payment_signature.b64` and the two `PAYMENT-RESPONSE` files change on every run. The tests check their structure, and they check that each signature recovers to its `authorization.from`, not fixed values. The other files are deterministic.

The fixtures in this commit were made with node 24.7.0, `@x402/core`, `@x402/evm` and `@x402/fetch` 2.30.0, viem 2.57.16, and Python `x402` 2.27.0 with eth-account 0.14.0. The `versions` key in the two `*_decoded_gateway_payment_required.json` files records the same versions.
