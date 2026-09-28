#!/usr/bin/env python3
"""Buy a stamp, upload a file and download it again through the gateway, using
the free tier or paying with x402.

    pip install "x402==1.0.0" requests
    python x402_client.py --gateway http://localhost:8000              # free tier
    X402_PRIVATE_KEY=0x... python x402_client.py --gateway ... --paid  # pay with USDC

--paid signs an EIP-3009 USDC authorization with X402_PRIVATE_KEY for the price
the gateway quotes, but only for USDC on --network (base-sepolia unless you pass
--network base) and, with --pay-to, only to that address. --max-usd caps each
payment and --max-total-usd the whole run (a run makes two payments: the stamp
and the upload). The script exits non-zero on any failure.
"""
import argparse
import base64
import json
import os
import sys
import time

import requests

USDC_UNITS = 1_000_000  # USDC has 6 decimals: "10000" means $0.01
# The USDC contract per network. A payment is signed only for these.
USDC = {
    "base-sepolia": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
    "base": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
}


def payment_requirements(resp):
    """Return the `accepts` list from a 402.

    The x402 spec puts it at the top level. Older gateway versions nest the
    whole 402 body under "detail", so read the top level first and fall back.
    """
    body = resp.json()
    if "accepts" in body:
        return body["accepts"]
    return body["detail"]["accepts"]


def choose(accepts, network, pay_to=None):
    """The `exact` USDC entry on `network` (to `pay_to`, if given), or exit.

    A client signs whatever the 402 names. Pinning the chain and token here
    means a gateway on another network, or a misconfigured one, gets no
    signature instead of a payment the user did not intend.
    """
    for a in accepts:
        if (a.get("scheme") == "exact" and a.get("network") == network
                and a.get("asset", "").lower() == USDC[network].lower()
                and (pay_to is None or a.get("payTo", "").lower() == pay_to.lower())):
            return a
    offered = [(a.get("network"), a.get("asset"), a.get("payTo")) for a in accepts]
    sys.exit(f"Refusing to sign: no USDC payment on {network}"
             + (f" to {pay_to}" if pay_to else "") + f". Offered: {offered}")


def sign_payment(requirement, account):
    """Build an X-PAYMENT header for one entry of `accepts` (x402 v1, "exact")."""
    from x402.clients.base import x402Client
    from x402.types import PaymentRequirements
    return x402Client(account).create_payment_header(PaymentRequirements(**requirement), 1)


class Budget:
    """Caps per payment and per run."""

    def __init__(self, max_usd, max_total_usd):
        self.max_usd, self.max_total_usd, self.spent = max_usd, max_total_usd, 0.0

    def charge(self, price):
        if price > self.max_usd:
            sys.exit(f"Price ${price:.6f} is above --max-usd ${self.max_usd}; not paying.")
        if self.spent + price > self.max_total_usd:
            sys.exit(f"Paying ${price:.6f} would take this run past --max-total-usd "
                     f"${self.max_total_usd} (spent ${self.spent:.6f}); not paying.")
        self.spent += price


def call(method, url, *, paid, account, budget, network, pay_to, **kwargs):
    """Send a protected request: free tier, or answer the 402 with a payment.

    Success is any 2xx. Stamp purchase answers 201, uploads answer 200; treating
    only 200 as success sends a successful purchase down the payment path.
    """
    if not paid:
        headers = {**kwargs.pop("headers", {}), "X-Payment-Mode": "free"}
        resp = requests.request(method, url, headers=headers, **kwargs)
        resp.raise_for_status()
        return resp

    resp = requests.request(method, url, **kwargs)
    if resp.status_code != 402:
        resp.raise_for_status()
        return resp
    requirement = choose(payment_requirements(resp), network, pay_to)
    price = int(requirement["maxAmountRequired"]) / USDC_UNITS
    budget.charge(price)
    print(f"  402: paying ${price:.6f} USDC on {requirement['network']} to {requirement['payTo']}")
    headers = {**kwargs.pop("headers", {}), "X-PAYMENT": sign_payment(requirement, account)}
    resp = requests.request(method, url, headers=headers, **kwargs)
    resp.raise_for_status()
    settled = resp.headers.get("X-PAYMENT-RESPONSE")
    if settled:
        print(f"  settled: {json.loads(base64.b64decode(settled))}")
    return resp


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--gateway", default="http://localhost:8000")
    p.add_argument("--paid", action="store_true", help="pay with x402 instead of the free tier")
    p.add_argument("--network", choices=sorted(USDC), default="base-sepolia",
                   help="only sign USDC payments on this network (default: base-sepolia)")
    p.add_argument("--pay-to", help="only sign payments to this address")
    p.add_argument("--max-usd", type=float, default=0.10, help="refuse any single payment above this")
    p.add_argument("--max-total-usd", type=float, default=0.25,
                   help="refuse to spend more than this in the whole run (two payments)")
    args = p.parse_args()
    gw = args.gateway.rstrip("/")

    account = None
    if args.paid:
        from eth_account import Account
        account = Account.from_key(os.environ["X402_PRIVATE_KEY"])
        print(f"Paying from {account.address}")
    opts = dict(paid=args.paid, account=account, network=args.network, pay_to=args.pay_to,
                budget=Budget(args.max_usd, args.max_total_usd))

    # 1. Buy a stamp. The body sets a duration; the gateway computes the amount,
    #    so it can never be below the minimum a batch needs.
    r = call("POST", f"{gw}/api/v1/stamps/", json={"size": "small", "duration_hours": 25}, **opts)
    stamp = r.json()["batchID"]
    print(f"Stamp purchase: HTTP {r.status_code}, batchID {stamp}")

    # 2. A new batch takes a short while to become usable.
    for _ in range(60):
        check = requests.get(f"{gw}/api/v1/stamps/{stamp}/check").json()
        if check.get("can_upload"):
            break
        time.sleep(5)
    else:
        sys.exit("Stamp did not become usable in time")
    print("Stamp is usable")

    # 3. Upload. The endpoint takes a multipart file named "file", not a JSON body.
    payload = json.dumps({"hello": "swarm", "at": time.time()}).encode()
    r = call("POST", f"{gw}/api/v1/data/", params={"stamp_id": stamp, "content_type": "application/json"},
             files={"file": ("hello.json", payload, "application/json")}, **opts)
    ref = r.json()["reference"]
    print(f"Upload: HTTP {r.status_code}, reference {ref}")

    # 4. Download (always free) and compare.
    r = requests.get(f"{gw}/api/v1/data/{ref}")
    r.raise_for_status()
    assert r.content == payload, "downloaded bytes differ from uploaded bytes"
    print(f"Download: HTTP {r.status_code}, {len(r.content)} bytes, matches upload")
    if args.paid:
        print(f"Spent ${opts['budget'].spent:.6f} USDC on {args.network}")


if __name__ == "__main__":
    main()
