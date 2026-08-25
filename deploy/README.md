# Deployment configuration

Configuration for the host that runs the gateway, kept here so it can be
reviewed and restored rather than existing only on one machine.

## `Caddyfile`

The reverse proxy. The gateway containers bind to loopback (`GATEWAY_BIND=127.0.0.1`),
so this is the single entry point and terminates TLS.

Hostnames come from the environment, so the same file serves any deployment.
They are supplied by a systemd drop-in rather than written into the file:

```
# /etc/systemd/system/caddy.service.d/hostnames.conf
[Service]
Environment=GATEWAY_HOST=provenance-gateway.example.io
Environment=GATEWAY_DEV_HOST=provenance-gateway.dev.example.io
Environment=ACME_EMAIL=ops@example.io
```

Apply:

```bash
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Then confirm both hostnames return 200 with a valid certificate — a reload that
leaves Caddy running is not by itself evidence that it is serving correctly:

```bash
curl -s -o /dev/null -w "%{http_code} verify=%{ssl_verify_result}\n" https://$GATEWAY_HOST/health
```

### Two things that have already gone wrong here

**Uploading a stale local copy over the live file.** A change made directly on
the host (removing `tls internal` at cutover) was reverted by a later upload from
a copy that predated it, and the site served self-signed certificates until it
was noticed. Edit the file in this repository and deploy it; do not edit the host
copy.

**Losing the access log.** Without a `log` directive Caddy records no requests at
all. That left us unable to answer "was anyone calling this gateway?" when
diagnosing unexplained load. Logging goes to stdout because writing under
`/var/log/caddy` fails with permission denied under the packaged unit's
sandboxing.

## Not here

TLS certificates and account keys — Caddy manages those under
`/var/lib/caddy` and they must not be committed.
