# Key custody and rotation

The gateway holds two signing keys, both delivered as GitHub environment secrets
(`staging` for `dev`, `production` for `main`) and written by the deploy workflow
into the host env file:

| Secret | Used for | Holds funds |
|--------|----------|-------------|
| `NOTARY_PRIVATE_KEY` | Notary signatures on uploaded documents (`/api/v1/notary/*`) | No |
| `GNOSIS_PRIVATE_KEY` | Buying batches for an external owner (Flow B) | Yes: xBZZ + xDAI on Gnosis |

The x402 `X402_PAY_TO_ADDRESS` is only an address; its key never touches the gateway.
The bundled Bee nodes keep their own keystores in the `bee-data` / `bee-dev-data` volumes
and are out of scope here.

## Where keys live on a host

| File | Contents | Mode |
|------|----------|------|
| `/opt/swarm_connect.env` | production gateway env, including both keys | `0600` |
| `/opt/swarm_connect_dev.env` | staging gateway env, including both keys | `0600` |
| `/opt/swarm_connect/.env` | compose variables, monitoring credentials | `0600` |

The deploy workflow sets these modes on every run, so a file that predates it is
narrowed on the next deploy. Verify with `stat -c '%a %U %n' /opt/swarm_connect*.env`.

The gateway container runs as UID 10001, not root, and does not need to read these
files: Docker Compose reads them and passes the values as environment variables.
Anyone who can run `docker inspect` on the host can still see them, so treat access to
the Docker group as access to the keys.

## Rotating a key

Rotate when someone with host, runner or secret access leaves, when a key may have been
exposed, or on a schedule you choose. The steps are the same for both environments;
do staging first.

1. **Generate a new key** off the host:
   `python scripts/generate_notary_key.py` (works for either key; it prints an address
   and a private key).
2. **Signer wallet only (`GNOSIS_PRIVATE_KEY`)**: fund the new address with xDAI and the
   xBZZ budget before switching, so batch purchases do not fail in between.
3. **Replace the secret**:
   `gh secret set NOTARY_PRIVATE_KEY --env production -R datafund/swarm_connect`
   (or `GNOSIS_PRIVATE_KEY`); paste the key at the prompt so it stays out of shell history.
4. **Redeploy** the branch (push, or re-run the `deploy` workflow). The env file is
   rewritten from the secrets and the gateway container is recreated.
5. **Verify**:
   - notary: `GET /api/v1/notary/info` reports the new address;
   - signer: `gateway_gnosis_signer_xbzz_balance` / `_xdai_balance` on `/metrics` show
     the new wallet's funded balances (the gauges carry no address, so compare values).
6. **Retire the old key**:
   - signer: move the remaining xBZZ and xDAI from the old address to the new one (or to
     treasury). Batches already bought stay valid, they are owned by the clients.
   - notary: publish the old address as a previous notary signer wherever the current one
     is documented. Documents signed earlier carry the old address in their signature and
     still verify; only a verifier that pins today's `/notary/info` address would reject them.

A compromised signer key is urgent. The running gateway keeps the old key loaded until
step 4 has recreated the container, so drain the old wallet to a safe address **before**
replacing the secret and redeploying, not after.
