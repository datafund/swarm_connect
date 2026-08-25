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
