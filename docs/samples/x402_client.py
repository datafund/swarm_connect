#!/usr/bin/env python3
"""Buy a stamp, upload a file and download it again through the gateway, using
the free tier or paying with x402.

    pip install "x402==1.0.0" requests
    python x402_client.py --gateway http://localhost:8000              # free tier
    X402_PRIVATE_KEY=0x... python x402_client.py --gateway ... --paid  # pay with USDC

--paid signs an EIP-3009 USDC authorization with X402_PRIVATE_KEY for the price
the gateway quotes. --max-usd caps what it will sign. The script exits non-zero
on any failure.
"""
import argparse
import base64
import json
import os
import sys
import time

import requests

USDC_UNITS = 1_000_000  # USDC has 6 decimals: "10000" means $0.01


def payment_requirements(resp):
    """Return the `accepts` list from a 402.

    The x402 spec puts it at the top level. Older gateway versions nest the
    whole 402 body under "detail", so read the top level first and fall back.
    """
    body = resp.json()
    if "accepts" in body:
        return body["accepts"]
    return body["detail"]["accepts"]


def sign_payment(requirement, account):
    """Build an X-PAYMENT header for one entry of `accepts` (x402 v1, "exact")."""
    from x402.clients.base import x402Client
    from x402.types import PaymentRequirements
    return x402Client(account).create_payment_header(PaymentRequirements(**requirement), 1)


def call(method, url, *, paid, account, max_usd, **kwargs):
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
    requirement = payment_requirements(resp)[0]
    price = int(requirement["maxAmountRequired"]) / USDC_UNITS
    if price > max_usd:
        sys.exit(f"Price ${price:.6f} is above --max-usd ${max_usd}; not paying.")
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
    p.add_argument("--max-usd", type=float, default=0.10, help="refuse to sign above this price")
    args = p.parse_args()
    gw = args.gateway.rstrip("/")

    account = None
    if args.paid:
        from eth_account import Account
        account = Account.from_key(os.environ["X402_PRIVATE_KEY"])
        print(f"Paying from {account.address}")
    opts = dict(paid=args.paid, account=account, max_usd=args.max_usd)

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


if __name__ == "__main__":
    main()
