#!/bin/sh
# Back up the gateway's persistent state (#375): ownership registry, prepaid
# bandwidth credit, pool state, allowance and spend counters, and the x402
# audit log. Run from cron on the host, e.g. nightly:
#
#   15 3 * * *  /opt/swarm_connect/scripts/backup_state.sh >> /var/log/swarm_connect_backup.log 2>&1
#
# Environment (all optional):
#   BACKUP_SOURCES  directories to archive      (default: /opt/swarm_connect_data /opt/swarm_connect_dev_data)
#   BACKUP_DIR      where archives are written  (default: /var/backups/swarm_connect)
#   BACKUP_KEEP_DAYS  local retention in days   (default: 14)
#   BACKUP_REMOTE   rsync/scp destination for an OFF-HOST copy, e.g. user@host:/backups/swarm_connect
#                   (strongly recommended: a copy on the same disk does not survive losing the host)
set -eu

SOURCES="${BACKUP_SOURCES:-/opt/swarm_connect_data /opt/swarm_connect_dev_data}"
DIR="${BACKUP_DIR:-/var/backups/swarm_connect}"
KEEP="${BACKUP_KEEP_DAYS:-14}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

umask 077
mkdir -p "$DIR"
for src in $SOURCES; do
  [ -d "$src" ] || { echo "skip: $src does not exist"; continue; }
  name="$(basename "$src")"
  out="$DIR/${name}-${STAMP}.tar.gz"
  tar -czf "$out" -C "$(dirname "$src")" "$name"
  echo "backed up $src -> $out ($(du -h "$out" | cut -f1))"
  if [ -n "${BACKUP_REMOTE:-}" ]; then
    if command -v rsync >/dev/null 2>&1; then rsync -a "$out" "$BACKUP_REMOTE/"; else scp -q "$out" "$BACKUP_REMOTE/"; fi
    echo "copied off-host to $BACKUP_REMOTE"
  fi
done
find "$DIR" -name '*.tar.gz' -mtime +"$KEEP" -print -delete
