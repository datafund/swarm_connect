#!/usr/bin/env bash
# Free-tier round trip with curl and jq: see the 402, buy a stamp, upload a
# file, download it. Paying needs an EIP-3009 signature, which curl cannot make;
# use x402_client.py or x402_client.mjs with --paid for that.
#
#   ./x402_curl.sh http://localhost:8000
set -euo pipefail
GW=${1:-http://localhost:8000}

# What a paid request costs, without paying (the 402 a client would get).
# `accepts` is at the top level in the x402 spec; older gateways nest it under
# `detail`, so read either.
echo "== 402 without payment or free-tier header"
curl -s -X POST "$GW/api/v1/stamps/" -H 'Content-Type: application/json' -d '{}' \
  | jq '(.accepts // .detail.accepts)[0] | {maxAmountRequired, network, payTo, asset}'

# Buy a stamp on the free tier. Success is 201 Created. -f fails on any 4xx/5xx.
echo "== buy a stamp (free tier)"
STAMP=$(curl -sf -X POST "$GW/api/v1/stamps/" -H 'X-Payment-Mode: free' \
  -H 'Content-Type: application/json' -d '{"size": "small", "duration_hours": 25}' | jq -r .batchID)
echo "batchID $STAMP"

echo "== wait until usable"
for _ in $(seq 60); do
  [ "$(curl -sf "$GW/api/v1/stamps/$STAMP/check" | jq -r .can_upload)" = "true" ] && break
  sleep 5
done

# Upload is multipart/form-data with the file in a field named "file".
echo "== upload"
FILE=$(mktemp)
echo '{"hello": "swarm"}' > "$FILE"
REF=$(curl -sf -X POST "$GW/api/v1/data/?stamp_id=$STAMP&content_type=application/json" \
  -H 'X-Payment-Mode: free' -F "file=@$FILE;type=application/json" | jq -r .reference)
echo "reference $REF"

echo "== download"
curl -sf "$GW/api/v1/data/$REF"
