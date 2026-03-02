#!/usr/bin/env python3
"""
Generate a new Ethereum keypair for notary signing.

Usage:
    python scripts/generate_notary_key.py

    # Set directly as GitHub secret:
    python scripts/generate_notary_key.py --gh-secret production
"""
import argparse
import sys

try:
    from eth_account import Account
except ImportError:
    print("Error: eth_account not installed. Run: pip install eth-account")
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Generate a notary signing keypair")
    parser.add_argument(
        "--gh-secret",
        metavar="ENV",
        help="Also print the gh CLI command to set the secret for the given GitHub environment"
    )
    args = parser.parse_args()

    account = Account.create()
    private_key = account.key.hex()[2:]  # Strip 0x prefix

    print(f"Address:     {account.address}")
    print(f"Private key: {private_key}")
    print()
    print("Set in .env:")
    print(f"  NOTARY_PRIVATE_KEY={private_key}")

    if args.gh_secret:
        print()
        print(f"Set as GitHub secret ({args.gh_secret} environment):")
        print(f"  gh secret set NOTARY_PRIVATE_KEY --repo datafund/swarm_connect --env {args.gh_secret} --body \"<paste-key-here>\"")
        print()
        print("IMPORTANT: Copy the private key and paste it manually into the command above.")
        print("Do not pipe the output — verify the address first, then set the secret separately.")


if __name__ == "__main__":
    main()
