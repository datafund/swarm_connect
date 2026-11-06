# Alert rules

`alert-rules.json` is the definition of the gateway's Grafana alert rules, kept
here so they can be reviewed, diffed and restored. Grafana remains the system
that evaluates them; this directory is the record of what they should be.

## Why this exists

The rules previously existed **only** inside Grafana. That meant no review, no
history, and no way to tell a deliberate change from an accidental one. Two
concrete consequences:

- A wallet address hardcoded in three alert descriptions went stale when the Bee
  node was replaced. The alerts kept reporting correct balances while pointing at
  a decommissioned wallet — an operator following one would have sent funds to a
  dead address. Nothing flagged it, because nothing was watching the text.
- A false negative sat in `Stamp Pool Exhausted` for an unknown length of time: it
  summed pool availability across hosts, so an exhausted pool on one host could be
  masked by another. It was found only by exporting the rules and reading them.

## Export what is live

```bash
curl -s -H "Authorization: Bearer $GRAFANA_SA_TOKEN" \
  "$GRAFANA_URL/api/v1/provisioning/alert-rules" \
  | jq 'sort_by(.title) | map({title, condition, data, noDataState, execErrState,
        for, annotations, labels, isPaused, ruleGroup})' \
  > monitoring/alerting/alert-rules.json
```

Then `git diff` shows what changed in Grafana since the last export. A non-empty
diff means someone edited a rule without recording it — which is the situation
this file exists to make visible.

## Applying everything from this repo

```bash
GRAFANA_URL=https://<stack>.grafana.net \
GRAFANA_SA_TOKEN=glsa_... \
python3 scripts/apply_grafana.py
```

Applies the dashboard and every rule in `alert-rules.json`. Idempotent — rules
are matched by title and updated in place, so running it twice does not create
duplicates. `--dry-run` parses and reports without sending anything, and needs
no credentials.

A rule present in Grafana but absent from this repo is **reported, not
deleted**. It may be a deliberate hand-made addition, and removing it silently
during an unrelated run would be worse than mentioning it.

The script also waits for the stack to wake before doing anything. A sleeping
Grafana Cloud stack answers 404 or 503 with a body that reads exactly like a
wrong hostname, which has cost time before.

### The token

`GRAFANA_SA_TOKEN` must be a **service-account** token for the Grafana instance
— the `glsa_` prefix. The `GRAFANA_CLOUD_API_TOKEN` deployed on the gateway
hosts is a *different credential*: a `glc_` Cloud Access Policy token scoped to
Prometheus remote-write. It pushes metrics fine and returns
`401 Invalid API key` here. Both are called "a Grafana Cloud token", which is
how the confusion starts; the script checks the prefix and refuses early rather
than letting it fail against the API.

Create one at **Administration → Users and access → Service accounts**, with
the Editor role, then add a token. Nothing in this repo stores it.

## Apply one rule

Rules are updated individually by UID, and the update must carry the **same
provenance** as the stored rule. A `PUT` with `X-Disable-Provenance: true`
against an `api`-provenance rule returns `409 alerting.provenanceMismatch`.

```bash
curl -X PUT -H "Authorization: Bearer $GRAFANA_SA_TOKEN" \
  -H "Content-Type: application/json" -d @rule.json \
  "$GRAFANA_URL/api/v1/provisioning/alert-rules/$UID"
```

UIDs are deliberately **not** stored here: they are per-instance, so a file
carrying them would be wrong in any other Grafana. Look them up from the export
above.

## Deliberately not in this directory

- **Contact points.** They hold the Telegram bot token and chat ID. Those are
  secrets and must not be committed. The routing policy references a contact point
  by name; the contact point itself is configured out of band.
- **A service account token.** Applying these needs one with alerting permissions.

## Note on templating

Descriptions use Grafana's Go templating: `{{ $labels.wallet }}` for the wallet
to fund, and a conditional on `{{ $labels.environment }}` for the health URL.
Both come from labels on the underlying metric, so they cannot go stale the way a
hardcoded value did. A label that does not exist renders empty, so each template
carries a fallback branch rather than assuming the label is present.

## One rule fires on missing data; the others never do

Every rule here answers a question about a measured value — is the balance low, is
the pool empty, is the error rate high. None of them can answer anything when no
measurement arrives, so none of them tries: they are all set to stay silent
(`noDataState: OK`) when the query returns nothing.

Exactly one rule watches for measurements stopping:

| rule | fires when |
|---|---|
| **No Data — System May Be Down** | nothing has reported for 10 minutes |

### Why this changed

Every rule used to be set to Grafana's middle `NoData` option, and the notification
policy has a single route with no filters — everything goes to Telegram. So when
metrics stopped arriving, **seven alerts fired at once**: "Critical BZZ Balance",
"Gateway Down", "Low xDAI" and four more, repeating every four hours until someone
fixed it.

Not one of them was true. The balance was not critical; it was unknown. Seven
alarms for one fault, all of them saying the wrong thing, is how a channel becomes
something people mute.

Now one alarm fires and it says what is actually known: no data, the system may be
down, or monitoring may have broken — and it does not pretend to know which.

### The trade-off, stated plainly

If the watchdog itself ever fails, the result is silence rather than noise. The old
arrangement was noisy but hard to miss. This is a deliberate choice that one
accurate alarm beats seven misleading ones.

Two things reduce the risk. The watchdog's query, `count(up{job=~"prometheus.scrape.*"}
or bee_up)`, returns a number whenever *anything* is reporting, so it only goes
quiet when everything does. And its threshold (`< 1`) is unreachable while data
exists — a count is at least 1 when anything reported — so the alert is driven
entirely by the no-data path and cannot be suppressed by an unusual-but-healthy
reading.

## Chain-backend rules and their deploy order

Two rules watch Bee's Gnosis RPC connection. They depend on Alloy scraping the
Bee nodes, which is configured in `monitoring/alloy/config.alloy` — the metrics
do not exist until that is deployed.

| rule | fires on | `noDataState` | ships |
|---|---|---|---|
| Gnosis RPC Errors | error rate above 5% for 15m | `OK` | unpaused |
| Bee Node Not Scraped | `bee_up < 1` for 10m | `Alerting` | **paused** |

The two `noDataState` values differ on purpose, and both differ from the rest of
this file, which uses `NoData` (see #263, still open).

**Gnosis RPC Errors is `OK` on no data** so it does not double-report. If the
metrics stop arriving, that is a scrape problem, not an RPC error rate, and the
second rule already covers it. Leaving this one on `NoData` would produce two
notifications for one fault.

**Bee Node Not Scraped ships paused** because its whole purpose is to treat
absence as failure. Before Alloy is deployed with the Bee targets,
`bee_up` does not exist, so provisioning it live would fire
it immediately — before anything is wrong. The order is:

1. Merge and deploy, so Alloy picks up the Bee scrape targets.
2. Confirm the series exists: query `bee_up` in Grafana and
   check both `environment="main"` and `environment="development"` are present.
3. Unpause the rule.

Skipping step 2 and unpausing early gives a false alert on a working system,
which is the fastest way to teach people to ignore this channel.

## Threshold, and why 5%

Set from measurement, not a guess. Production sat at roughly 1.1% cumulative
over 19 hours of uptime, and both nodes measured 0 errors across several hundred
calls in steady state. The floor is not zero, so a threshold at 0 would be noise.

The number that is *not* covered by 5% is a restart: a Bee node catching up
after starting hammers its RPC endpoint and one produced 5,462 errors in its
first forty minutes, then none at all afterwards. The 15-minute `for` window
absorbs a short burst; a restart storm longer than that will alert, and should.

Revisit the threshold once there is a week of baseline on the dashboard rather
than the few hours it was set from.

## Gateway up, Bee calls failing (#586)

On 2026-11-03 staging stopped serving for over 30 minutes and nothing alerted.
The process kept running and answering `/metrics`, so `up` stayed 1 and
**Gateway Down** stayed quiet. Every call to Bee failed, `/health` took over
10 s, and the proxy answered 502. Bee itself was healthy, so **Bee Node Not
Scraped** stayed quiet too. The balance gauges kept their last value, so the
balance alerts were judging a number half an hour old. Six rules cover what
was missing, and a seventh makes sure the probe itself is not lost silently:

| rule | fires on | for | severity |
|---|---|---|---|
| Bee calls failing | `increase(gateway_bee_api_errors_total[5m])` > 20 | 5m | critical |
| Gateway 5xx responses | 5xx share of `http_requests_total` over 10m > 20%, with at least 20 requests; both leave out the health handlers `/`, `/health`, `/metrics` | 5m | critical |
| Public health check failing | no 200 from the probe for 5 minutes | 1m | critical |
| Public health check slow | probe average over 10m > 5 s | 10m | warning |
| Bee node not healthy (public /health) | `probe_failed_due_to_regex` 1 on every probe for 10m (so about 15 minutes in all) | 5m | critical |
| Public health probe missing | gateway `up` series for an environment with no probe result in the last 15m | 30m | warning |
| Balance check failing | `gateway_balance_check_ok` 0, or no successful read for 15 min | 15m | warning |

All are grouped by `environment` and `host`, and all are `noDataState: OK`.
Absence is the **No Data** watchdog's job.

**Thresholds, from measurement** (Grafana, 7 days to 2026-11-03):
- Bee call errors were about 2 per 5 minutes at p99 and at most 8.4 on
  production. The incident reached 619. Over 20 happened in only 9
  five-minute windows, all on development, during the incident week.
- 5xx share: health traffic is left out of the share and of the 20-request
  guard. Docker's healthcheck of `/` alone is a median 22.6 requests per
  10 minutes on production (19.5 on development), so it met a guard on all
  requests by itself: production passed that guard in 754 of 1,008 ten-minute
  windows, against 36 with health traffic left out. It also only dilutes the
  share: `/` and `/health` answered no 5xx in 44,600 requests over the week,
  and they were 83% of production's requests. Replayed read-only against the
  week with 1-minute steps, the rule fires once, on development, at 09:54 UTC
  on 2026-11-03 (pending from 09:49), and stays firing until the series ended
  at about 10:15. The version counting all requests also fired at 09:54, but
  fell below 20% at 10:00 and fired a second time at 10:06. It fired late:
  most failed requests in the first half of the incident were not 5xx
  (the load test's 429/422/402), and the 5xx share passed 20% only from
  about 09:50. **Bee calls failing** fired first, at 09:31 (pending from
  09:26, replayed the same way).
- Production `/health` took about 3.5 s on 2026-11-03, so the slow threshold is
  5 s, not 3 s. `/health` makes uncached upstream calls on every request
  (#588); once that is cached, the threshold can come down.

The baseline is one week that includes one incident. Revisit the thresholds
once there is more.

### The public /health probe

Alloy (`monitoring/alloy/config.alloy`) runs a blackbox probe of both public
`/health` URLs every 60 s. It uses an 8 s timeout, requires status 200, and
checks that `bee_node.healthy` is true in the body. A body regex is used because
blackbox has no portable JSON check. `tests/test_bee_failure_alerts.py` runs
the regex against the real handler's output. The series carry
`job="swarm_connect/health_probe"`. This stack already has Synthetic
Monitoring `probe_*` series for other sites, so the rules select on that job.

It also covers Docker's `unhealthy` state. The container healthcheck calls the
same handler with a 10 s timeout, so whenever Docker marks the container
unhealthy, the probe fails too. Nothing restarts the container automatically:
a restart loses any change still in memory only (**State file save failures**).

The rules have no data until Alloy runs the new config, and Alloy runs the
config of the branch deployed last: each deploy clones its own branch into the
host checkout and recreates Alloy from it. While the probe is only on `dev`, a
main deploy removes it and the next dev deploy brings it back. The probe rules
are `noDataState: OK` and go quiet when that happens. That is why **Public health
probe missing** exists: it fires per environment when the gateway reports
metrics but no probe result has arrived for 15 minutes, held for 30 minutes.
Re-check the probe series after every main deploy until the change is on
`main` (`docs/mainnet-runbook.md` section 5, "After the first deploy that adds
the probe").

### A check from outside: Grafana Synthetic Monitoring

The Alloy probe runs on the gateway host and goes out through the public
hostname, DNS, TLS and the reverse proxy. That catches a stuck gateway, a slow
upstream and a broken proxy. It cannot catch the host itself, or its network,
going away: the probe goes down with it. **Gateway Down** and **No Data** then
cover it, but only as "no data", not as "users cannot reach it".

Grafana Synthetic Monitoring is the option for a truly external check, and
this Grafana stack already uses it for other sites. An HTTP check on each
`/health` URL, run from a few public probe locations, would add:
- reachability from outside the host's network, including DNS and routing
  problems that a probe on the host does not see;
- latency as users far from the host see it;
- a JSON assertion on `bee_node.healthy` (Synthetic Monitoring HTTP checks
  support JSON path assertions), rather than a regex;
- per-location results, so one bad route is not mistaken for an outage.

It is configured in Grafana, not in this repository, so it is not reviewed or
deployed from here. Its series carry their own `job` and `probe` labels. A rule
on them should select by `job`, like the rules above, so the two sources do not
mix.
